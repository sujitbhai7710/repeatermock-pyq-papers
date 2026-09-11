"""English vocabulary mechanics (pure python, no AI).

Every English question in scope is classified into a *kind* (synonym, antonym,
one-word-substitution, idiom, spelling, homonym) and the word/phrase that the
question is actually about is extracted from the prompt.  From that we build the
four synonym/antonym buckets plus the OWS/idiom/spelling/homonym tables.

Bucket definitions
------------------
``asMain``
    the word/phrase is the thing being asked about (``Select the synonym of X``).
``asOption``
    the word/phrase appears among the four options of another question.  The four
    options of a question are its *meaning options*, so a question never counts
    its own main term as an option; an option that appears twice in one option
    set is counted once ("count the word only").
``correct``
    the occurrence was the correct answer.

Ranking: ``importance = 3 * as_main + as_option`` (being asked about a word is a
stronger signal than appearing as a distractor), ties broken alphabetically so
output is byte-stable.

Formats handled (all observed in the corpus)::

    **Select the correct synonym of the given word.**\\n\\nScintillating
    Select the antonym of the given word.\\n\\nVICIOUS
    Select one word for the following group of words.\\n\\nA period of ten years
    Select the most appropriate meaning of the given idiom\\n\\nA snake in the grass
    Rectify the sentence by selecting the correct spelling of the underlined word.
"""

from __future__ import annotations

import re
import string
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import paths
from .sections import iter_options
from .util import Log, fold_key, human_int, norm_space, write_json, write_text

# ---------------------------------------------------------------------------
# instruction / payload split
# ---------------------------------------------------------------------------

INSTRUCTION_SPLIT_RE = re.compile(r"\n\s*\n")
EMPHASIS_RE = re.compile(r"\*{1,2}(.+?)\*{1,2}")
UNDERLINE_RE = re.compile(r"_{2,}(.+?)_{2,}")
IMAGE_MARK_RE = re.compile(r"\[IMAGE:[^\]]*\]")

SYNONYM_INSTRUCTION_RE = re.compile(r"synonym", re.I)
ANTONYM_INSTRUCTION_RE = re.compile(r"antonym|opposite", re.I)
OWS_INSTRUCTION_RE = re.compile(
    r"one word (?:for|substitution|substitute)|one-?word substitution|substitute the group", re.I
)
IDIOM_INSTRUCTION_RE = re.compile(r"meaning of the (?:given )?(?:idiom|phrase)|idiom/phrase", re.I)
SPELLING_INSTRUCTION_RE = re.compile(r"spelling", re.I)
HOMONYM_INSTRUCTION_RE = re.compile(r"homophone|homonym", re.I)

#: a line that can never be the asked word/phrase
NON_TERM_RE = re.compile(
    r"\boptions?\b|four alternatives|each sentence is followed|given below|choose the correct"
    r"|select the (?:correct|most)|fill in the blank|in the following question|direction"
    r"|misspell|misspelt|misspelled|spelled incorrectly|spelling error|spelt|incorrectly"
    r"|correctly spelt|word is spelled|which (?:one|part|word)",
    re.I,
)

#: for spelling/homonym questions the asked entry must be a short word
SHORT_TERM_KINDS = frozenset({"spelling", "homonym"})
SHORT_TERM_MAX_WORDS = 3

#: boilerplate options that are never vocabulary entries
BOILERPLATE_OPTION_RE = re.compile(
    r"^\s*(?:no|none of|all of|not?)\b.*\b(?:improvement|substitution|error|correction|required|these|above|change)\b"
    r"|^\s*none of these\s*$"
    r"|^\s*no error\s*$"
    r"|^\s*no improvement\s*$"
    r"|^\s*no substitution required\s*$",
    re.I,
)

KINDS = ("synonym", "antonym", "ows", "idiom", "spelling", "homonym")

MIN_TERM_LEN = 2
MAX_TERM_WORDS = 12

#: lines that are an instruction rather than the asked word/phrase
INSTRUCTION_LINE_RE = re.compile(
    r"select|choose|identify|pick|find|out of the four|in the following question"
    r"|give the|what is the|direction|rectify|improve|fill in",
    re.I,
)
DIRECTION_PREFIX_RE = re.compile(r"^\s*directions?\s*[:\-–]\s*", re.I)


def split_prompt(text: str) -> Tuple[str, str]:
    """Return ``(instruction, payload)`` for a question prompt."""

    cleaned = IMAGE_MARK_RE.sub(" ", text or "")
    parts = INSTRUCTION_SPLIT_RE.split(norm_space_lines(cleaned), maxsplit=1)
    if len(parts) == 1:
        return parts[0].strip(), ""
    return parts[0].strip(), parts[1].strip()


def norm_space_lines(text: str) -> str:
    """Collapse blank lines to exactly one but keep them as separators."""

    text = (text or "").replace("\r\n", "\n").replace("\r", "\n")
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r"\n{2,}", "\n\n", text)
    return text.strip()


def strip_markup(text: str) -> str:
    value = IMAGE_MARK_RE.sub(" ", text or "")
    value = UNDERLINE_RE.sub(r"\1", value)
    value = EMPHASIS_RE.sub(r"\1", value)
    value = value.replace("**", "").replace("__", "")
    return norm_space(value)


def _clean_term(value: str) -> str:
    term = strip_markup(value)
    term = term.strip(string.whitespace + "\"'“”‘’.,;:!?()[]")
    term = re.sub(r"\s+", " ", term)
    return term


def _looks_like_term(value: str) -> bool:
    if not value:
        return False
    if len(value) < MIN_TERM_LEN:
        return False
    if len(value.split()) > MAX_TERM_WORDS:
        return False
    if value.endswith((".", "?", "!")) and len(value.split()) > 8:
        return False
    if NON_TERM_RE.search(value):
        return False
    return True


def last_payload_term(payload: str) -> str:
    """The trailing line of the payload is the asked word/phrase."""

    for line in reversed([l for l in payload.split("\n") if l.strip()]):
        candidate = _clean_term(line)
        if _looks_like_term(candidate):
            return candidate
    return ""


def _is_short_term(value: str) -> bool:
    if not value or len(value) < MIN_TERM_LEN:
        return False
    if len(value.split()) > SHORT_TERM_MAX_WORDS:
        return False
    return not NON_TERM_RE.search(value)


def asked_term(prompt: str) -> str:
    """Extract the word/phrase the question is about.

    Handles both layouts observed in the corpus::

        **Select the correct synonym of the given word.**\\n\\nScintillating
        Direction: Select the most appropriate synonym of the underlined word
        ABANDON

    i.e. the term is the first content line *after* the instruction line, or the
    part of the instruction line that follows the instruction itself.  Instruction
    text is never returned as a term.
    """

    lines = [line for line in norm_space_lines(prompt).split("\n") if line.strip()]
    if not lines:
        return ""

    for index, line in enumerate(lines):
        if not INSTRUCTION_LINE_RE.search(strip_markup(line)):
            continue
        # content after the instruction inside the same line
        stripped = DIRECTION_PREFIX_RE.sub("", strip_markup(line))
        sentences = re.split(r"(?<=[.:?])\s+", stripped)
        candidates = list(reversed(sentences[1:])) + lines[index + 1 :]
        for candidate in candidates:
            cleaned = _clean_term(candidate)
            if not _looks_like_term(cleaned):
                continue
            if INSTRUCTION_LINE_RE.search(cleaned) and len(cleaned.split()) > 4:
                continue
            return cleaned
        break

    # no instruction line: the underlined token, else the last line
    underlined = UNDERLINE_RE.findall(prompt)
    if underlined:
        candidate = _clean_term(underlined[-1])
        if _looks_like_term(candidate):
            return candidate
    return last_payload_term(prompt)


# ---------------------------------------------------------------------------
# kind detection
# ---------------------------------------------------------------------------


def detect_kind(
    concept: Optional[str],
    tags: Optional[Sequence[str]],
    instruction: str,
) -> Optional[str]:
    """Classify an English question into one of :data:`KINDS`."""

    label = norm_space(concept).lower()
    tag_text = " ; ".join(norm_space(t) for t in (tags or [])).lower()
    blob = f"{label} | {tag_text}"

    if "homophone" in blob or "homonym" in blob or HOMONYM_INSTRUCTION_RE.search(instruction):
        return "homonym"
    if "spelling" in blob or SPELLING_INSTRUCTION_RE.search(instruction):
        return "spelling"
    if "ows" in blob or "one word substitution" in blob:
        return "ows"
    if OWS_INSTRUCTION_RE.search(instruction):
        return "ows"
    if "idiom" in blob or IDIOM_INSTRUCTION_RE.search(instruction):
        return "idiom"
    if "synonym" in blob and "antonym" not in blob:
        return "synonym"
    if "antonym" in blob:
        return "antonym"
    if SYNONYM_INSTRUCTION_RE.search(instruction) and not ANTONYM_INSTRUCTION_RE.search(instruction):
        return "synonym"
    if ANTONYM_INSTRUCTION_RE.search(instruction):
        return "antonym"
    return None


# ---------------------------------------------------------------------------
# extraction
# ---------------------------------------------------------------------------


@dataclass
class VocabularyQuestion:
    qid: str
    kind: str
    term: str
    options: List[str] = field(default_factory=list)
    correct_option: str = ""
    exam: str = ""
    year: int = 0
    paper_path: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {
            "qid": self.qid,
            "kind": self.kind,
            "term": self.term,
            "options": list(self.options),
            "correct": self.correct_option,
            "exam": self.exam,
            "year": self.year,
            "paper_path": self.paper_path,
        }


def extract_question(question: Dict[str, Any], record: Dict[str, Any]) -> Optional[VocabularyQuestion]:
    """Extract a vocabulary item from a question, or ``None`` if not applicable."""

    prompt = str(question.get("question") or "")
    instruction, payload = split_prompt(prompt)
    kind = detect_kind(question.get("concept"), question.get("tags"), instruction)
    if kind is None:
        return None

    options: List[str] = []
    correct_label = str(question.get("correct") or "")
    correct_option = ""
    for option in iter_options(question):
        text = _clean_term(str(option.get("text") or ""))
        if not text or BOILERPLATE_OPTION_RE.match(text):
            continue
        options.append(text)
        if str(option.get("label")) == correct_label:
            correct_option = text

    if kind in ("synonym", "antonym", "ows", "idiom", "homonym"):
        term = asked_term(prompt)
    else:
        term = extract_spelling_target(instruction, payload)

    if kind in SHORT_TERM_KINDS:
        # spelling/homonym entries are single words; fall back to the correct
        # option (the correctly spelt candidate) when the prompt has no clean term
        if not _is_short_term(term):
            term = correct_option if _is_short_term(correct_option) else ""
    elif INSTRUCTION_LINE_RE.search(term) and len(term.split()) > 4:
        term = ""

    if not term:
        return None

    return VocabularyQuestion(
        qid=str(question.get("qid", "")),
        kind=kind,
        term=term,
        options=options,
        correct_option=correct_option,
        exam=str(record.get("exam") or ""),
        year=int(record.get("year") or 0),
        paper_path=str(record.get("paper_path") or ""),
    )


def extract_spelling_target(instruction: str, payload: str) -> str:
    """Best-effort target for spelling/homonym questions.

    ``Rectify the sentence by selecting the correct spelling of the underlined
    word.`` → the underlined token; otherwise the first option-like token of the
    payload, else the whole payload (kept short by :func:`_looks_like_term`).
    """

    underlined = UNDERLINE_RE.findall(payload) or UNDERLINE_RE.findall(instruction)
    if underlined:
        candidate = _clean_term(underlined[0])
        if _looks_like_term(candidate):
            return candidate
    term = last_payload_term(payload)
    if term:
        return term
    return _clean_term(instruction) if _looks_like_term(_clean_term(instruction)) else ""


# ---------------------------------------------------------------------------
# bucket building
# ---------------------------------------------------------------------------


@dataclass
class WordStats:
    as_main: int = 0
    as_option: int = 0
    as_option_correct: int = 0
    exams: Counter = field(default_factory=Counter)
    years: Counter = field(default_factory=Counter)
    variants: Set[str] = field(default_factory=set)

    @property
    def total(self) -> int:
        return self.as_main + self.as_option

    @property
    def importance(self) -> int:
        return 3 * self.as_main + self.as_option

    def as_dict(self) -> Dict[str, Any]:
        return {
            "asMain": self.as_main,
            "asOption": self.as_option,
            "asOptionCorrect": self.as_option_correct,
            "total": self.total,
            "importance": self.importance,
            "exams": dict(sorted(self.exams.items())),
            "years": {str(k): v for k, v in sorted(self.years.items())},
            "variants": sorted(self.variants),
        }


#: idiom keys also ignore a leading ``to `` ("To spill the beans" == "spill the beans")
TO_STRIP_FAMILIES = frozenset({"idioms"})


def entry_key(table: str, text: str) -> str:
    """Case/diacritic/whitespace-insensitive key for one vocabulary table.

    The corpus spells the same entry in several ways (``Abandon`` / ``abandon``
    / ``ABANDON``, ``To spill the beans`` / ``spill the beans``); counting them
    as separate rows inflated every table and hid the real repeat counts.  The
    key is only used for grouping — the table still shows a human-readable
    display form (the first spelling seen) and lists every variant in
    ``variants``.
    """

    value = fold_key(text)
    if table in TO_STRIP_FAMILIES:
        value = re.sub(r"^to\s+", "", value)
    return value


def build_buckets(items: Sequence[VocabularyQuestion]) -> Dict[str, Any]:
    """Build the four synonym/antonym buckets plus OWS / idiom / spelling tables.

    Entries are grouped by :func:`entry_key`, so two spellings that differ only
    in letter case (or an idiom's leading ``to``) share one row; the first
    spelling encountered is kept as the display form.
    """

    # kind family: synonym/antonym get their own 4 buckets; ows/idiom get asMain/asOption
    family = {
        "synonym": "synonym",
        "antonym": "antonym",
        "ows": "ows",
        "idiom": "idioms",
        "spelling": "spelling",
        "homonym": "homonyms",
    }

    #: table -> entry key -> {"display": str, "stats": WordStats}
    entries: Dict[str, Dict[str, Dict[str, Any]]] = {
        name: {} for name in ("synonym", "antonym", "ows", "idioms", "spelling", "homonyms")
    }

    def bucket(table: str, text: str) -> WordStats:
        key = entry_key(table, text)
        record = entries[table].get(key)
        if record is None:
            record = {"display": norm_space(text), "stats": WordStats()}
            entries[table][key] = record
        record["stats"].variants.add(norm_space(text))
        return record["stats"]

    for item in items:
        table = family[item.kind]

        main = item.term
        main_key = entry_key(table, main)
        stats = bucket(table, main)
        stats.as_main += 1
        stats.exams[item.exam] += 1
        stats.years[item.year] += 1

        # option occurrences: the four options of the question are its meaning
        # options, so the question's own main term is never counted; duplicates
        # inside one option set count once ("count the word only").
        seen_in_question = set()
        for option in item.options:
            key = entry_key(table, option)
            if not key or key == main_key or key in seen_in_question:
                continue
            seen_in_question.add(key)
            opt_stats = bucket(table, option)
            opt_stats.as_option += 1
            opt_stats.exams[item.exam] += 1
            opt_stats.years[item.year] += 1
            if item.correct_option and key == entry_key(table, item.correct_option):
                opt_stats.as_option_correct += 1

    def ranked(table: str) -> List[Dict[str, Any]]:
        rows = []
        for record in entries[table].values():
            rows.append({"word": record["display"], **record["stats"].as_dict()})
        rows.sort(key=lambda r: (-r["importance"], -r["total"], fold_key(r["word"]), r["word"]))
        for index, row in enumerate(rows, 1):
            row["rank"] = index
        return rows

    tables = {name: ranked(name) for name in entries}
    repeats = {
        name: {
            row["word"]: row["total"]
            for row in tables[name]
            if row["total"] > 1
        }
        for name in entries
    }
    return {
        "tables": tables,
        "repeats": repeats,
        "counts": {name: len(tables[name]) for name in tables},
        "items": [item.as_dict() for item in items],
    }


# ---------------------------------------------------------------------------
# markdown output
# ---------------------------------------------------------------------------

TABLE_TITLES = {
    "synonym": ("Synonyms", "Words asked as synonyms and words offered as synonym options"),
    "antonym": ("Antonyms", "Words asked as antonyms and words offered as antonym options"),
    "ows": ("One Word Substitution", "Phrases asked for one-word substitution and the substitute words offered"),
    "idioms": ("Idioms & Phrases", "Idioms asked and the meaning options offered"),
    "spelling": ("Spelling", "Words/phrases in spelling questions with their repeat counts"),
    "homonyms": ("Homonyms / Homophones", "Homonym and homophone items with their repeat counts"),
}


def _md_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out)


def render_table_md(name: str, rows: Sequence[Dict[str, Any]], items: Sequence[Dict[str, Any]]) -> str:
    title, blurb = TABLE_TITLES[name]
    lines: List[str] = []
    add = lines.append
    add(f"# {title}")
    add("")
    add(blurb + ".")
    add("")
    add(
        f"- Distinct entries: **{human_int(len(rows))}**\n"
        f"- Questions processed: **{human_int(len([i for i in items if i['kind'] in KIND_TO_TABLE.get(name, [])]))}**"
    )
    add("")
    add("`asMain` = the entry was asked about; `asOption` = it appeared among another question's options; "
        "`asOptionCorrect` = it was the answer in that question. A question never counts its own main term "
        "as an option (its options are that question's meaning options), and duplicates inside one option set "
        "count once. Ranked by `importance = 3*asMain + asOption`. Entries are grouped case- and "
        "diacritic-insensitively (and, for idioms, ignoring a leading `to `), so `Abandon`/`abandon` and "
        "`To spill the beans`/`spill the beans` are one row; the first spelling seen is shown.")
    add("")
    if not rows:
        add("_No entries._")
        add("")
        return "\n".join(lines)
    add(
        _md_table(
            ["#", "Entry", "asMain", "asOption", "asOption correct", "Total", "Importance"],
            [
                [
                    r["rank"],
                    r["word"],
                    r["asMain"],
                    r["asOption"],
                    r["asOptionCorrect"],
                    r["total"],
                    r["importance"],
                ]
                for r in rows
            ],
        )
    )
    add("")
    return "\n".join(lines)


KIND_TO_TABLE = {
    "synonym": ["synonym"],
    "antonym": ["antonym"],
    "ows": ["ows"],
    "idioms": ["idiom"],
    "spelling": ["spelling"],
    "homonyms": ["homonym"],
}


# ---------------------------------------------------------------------------
# entry point
# ---------------------------------------------------------------------------


@dataclass
class VocabResult:
    status: str
    counters: Dict[str, Any]
    files: List[Path]
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "counters": self.counters,
            "files": [paths.rel(p) for p in self.files],
            "notes": self.notes,
        }


def build_vocabulary_db(
    records: Sequence[Dict[str, Any]],
    questions_by_qid: Dict[str, Dict[str, Any]],
    *,
    out_dir: Optional[Path] = None,
    log: Optional[Log] = None,
) -> VocabResult:
    """Build ``database/english/vocabulary/*.md`` + ``all.json``.

    ``questions_by_qid`` supplies the raw question dicts (the index only stores
    pointer records).
    """

    logger = log or Log("vocab")
    target = out_dir or paths.VOCAB_DB_DIR
    items: List[VocabularyQuestion] = []
    kind_counts: Counter = Counter()

    for record in records:
        if record.get("subject") != "ENG":
            continue
        question = questions_by_qid.get(str(record.get("qid")))
        if question is None:
            continue
        item = extract_question(question, record)
        if item is None:
            continue
        items.append(item)
        kind_counts[item.kind] += 1

    if not items:
        logger.warn("no vocabulary questions found")
        return VocabResult("ok", {"items": 0}, [], ["no vocabulary questions found"])

    buckets = build_buckets(items)
    paths.ensure_dir(target)

    all_payload = {
        "version": 1,
        "generated_by": "agent.vocab",
        "question_counts": {k: kind_counts[k] for k in KINDS},
        "distinct_entries": buckets["counts"],
        "tables": buckets["tables"],
        "repeats": buckets["repeats"],
        "items": buckets["items"],
    }
    write_json(target / "all.json", all_payload)

    files: List[Path] = [target / "all.json"]
    filenames = {
        "synonym": "synonyms.md",
        "antonym": "antonyms.md",
        "ows": "one-word-substitution.md",
        "idioms": "idioms.md",
        "spelling": "spelling.md",
        "homonyms": "homonyms.md",
    }
    for name, filename in filenames.items():
        rows = buckets["tables"][name]
        path = target / filename
        write_text(path, render_table_md(name, rows, buckets["items"]))
        files.append(path)

    own_notes: List[str] = []
    missing = sum(1 for r in records if r.get("subject") == "ENG" and str(r.get("qid")) not in questions_by_qid)
    if missing:
        own_notes.append(f"{missing} English records had no resolvable source question and were skipped")

    counters = {
        "vocabulary_items": len(items),
        "synonym_questions": kind_counts["synonym"],
        "antonym_questions": kind_counts["antonym"],
        "ows_questions": kind_counts["ows"],
        "idiom_questions": kind_counts["idiom"],
        "spelling_questions": kind_counts["spelling"],
        "homonym_questions": kind_counts["homonym"],
        "distinct_synonym_words": buckets["counts"]["synonym"],
        "distinct_antonym_words": buckets["counts"]["antonym"],
        "distinct_ows_entries": buckets["counts"]["ows"],
        "distinct_idioms": buckets["counts"]["idioms"],
    }
    logger.info(
        "vocabulary: {i} items (syn {s} / ant {a} / ows {o} / idiom {d})".format(
            i=len(items),
            s=kind_counts["synonym"],
            a=kind_counts["antonym"],
            o=kind_counts["ows"],
            d=kind_counts["idiom"],
        )
    )
    return VocabResult("ok", counters, files, own_notes)
