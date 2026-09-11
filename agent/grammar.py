"""Grammar question -> rule mapping.

The English taxonomy file holds **129 numbered grammar rules** (``## Rule N:
Title`` with a ``**Topic:**`` line).  Every grammar question in scope is mapped
to the best-matching rule using

* the question type (Sentence Improvement, Error Detection, Shuffling of Sentence
  parts, Shuffling of Sentences in a passage, Sentence Structure, Modal, Phrasal
  Verb) detected from ``concept``/``tags``/prompt, and
* a weighted keyword overlap between the prompt and each rule's title + topic.

Scoring (deterministic)::

    score = 3 * |prompt_terms ∩ title_terms|
          + 2 * |prompt_terms ∩ topic_terms|
          + 4 (curated type hint matches the rule)

Highest score wins; ties break on the lower rule number.  A question with
``score == 0`` is written to ``unmapped.jsonl`` and reported as unclassified
rather than being attached to an arbitrary rule.
"""

from __future__ import annotations

import re
from collections import Counter, defaultdict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import paths
from .sections import iter_options
from .util import Log, human_int, norm_key, norm_space, write_json, write_jsonl, write_text

GRAMMAR_TYPES = (
    "Error Detection",
    "Sentence Improvement",
    "Shuffling of Sentence parts",
    "Shuffling of Sentences in a passage",
    "Sentence Structure",
    "Modal",
    "Phrasal Verb",
)

TYPE_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("Error Detection", re.compile(r"error (?:detection|spotting)|spot the error|incorrect part", re.I)),
    (
        "Sentence Improvement",
        re.compile(r"sentence improvement|improve the (?:sentence|underlined)|phrase replacement|substitute the underlined", re.I),
    ),
    (
        "Shuffling of Sentence parts",
        re.compile(r"parts of the (?:following )?sentence|jumbled|rearrange the parts|segments? .*rearrange", re.I),
    ),
    (
        "Shuffling of Sentences in a passage",
        re.compile(r"passage|paragraph", re.I),
    ),
    ("Sentence Structure", re.compile(r"sentence structure|structurally", re.I)),
    ("Modal", re.compile(r"\bmodal\b", re.I)),
    ("Phrasal Verb", re.compile(r"phrasal verb", re.I)),
)

#: curated hints from question type to rule titles (matched case-insensitively)
TYPE_RULE_HINTS: Dict[str, Tuple[str, ...]] = {
    "Modal": ("Modal Verbs", "Conditional Tense Patterns", "Correct Verb Forms"),
    "Phrasal Verb": ("Phrasal Verbs", "No Extra Preposition", "Prepositions"),
    "Shuffling of Sentence parts": (
        "Sentence Structure",
        "Correlative Conjunctions",
        "Pronoun Order",
        "Word Order",
    ),
    "Shuffling of Sentences in a passage": ("Sentence Structure", "Cohesion", "Connectors"),
    "Sentence Structure": ("Sentence Structure", "Correlative Conjunctions", "Pronoun Order"),
}

STOPWORDS = {
    "a", "an", "the", "of", "and", "or", "to", "in", "on", "for", "with", "is", "are",
    "was", "were", "be", "by", "at", "as", "it", "its", "this", "that", "these", "those",
    "from", "into", "than", "then", "if", "not", "no", "do", "does", "did", "has", "have",
    "had", "will", "would", "shall", "should", "can", "could", "may", "might", "must",
    "select", "choose", "correct", "incorrect", "given", "following", "sentence",
    "sentences", "word", "words", "part", "parts", "option", "options", "question",
    "improve", "improvement", "error", "errors", "detect", "spot", "below", "above",
    "underlined", "meaning", "phrase", "phrases", "one", "two", "three", "four",
    "most", "appropriate", "best", "expresses", "replacement", "replace", "using",
    "use", "used", "each", "all", "some", "any", "more", "less", "very", "also",
    "blank", "blanks", "fill", "blanks", "passage", "jumbled", "rearrange", "sequence",
    "option", "answer", "statements", "statement",
}

TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z'\-]+")

def tokens(text: str) -> List[str]:
    return [t.lower() for t in TOKEN_RE.findall(text or "")]

def content_terms(text: str) -> Set[str]:
    return {t for t in tokens(text) if t not in STOPWORDS and len(t) > 2}

# ---------------------------------------------------------------------------
# rule index
# ---------------------------------------------------------------------------

@dataclass
class GrammarRule:
    number: int
    title: str
    topic: str = ""
    title_terms: Set[str] = field(default_factory=set)
    topic_terms: Set[str] = field(default_factory=set)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.number,
            "title": self.title,
            "topic": self.topic,
            "title_terms": sorted(self.title_terms),
            "topic_terms": sorted(self.topic_terms),
        }

def build_rule_index(taxonomy: Dict[str, Any]) -> List[GrammarRule]:
    body = taxonomy.get("subjects", {}).get("ENG", {})
    rules: List[GrammarRule] = []
    for chapter in body.get("chapters", []):
        if chapter.get("kind") == "supplementary":
            continue
        try:
            number = int(chapter.get("id", "0"))
        except (TypeError, ValueError):
            continue
        title = chapter.get("name", "")
        topic = chapter.get("topic", "") or ""
        rules.append(
            GrammarRule(
                number=number,
                title=title,
                topic=topic,
                title_terms=content_terms(title),
                topic_terms=content_terms(topic),
            )
        )
    rules.sort(key=lambda r: r.number)
    return rules

# ---------------------------------------------------------------------------
# type detection & scoring
# ---------------------------------------------------------------------------

def detect_type(concept: Optional[str], tags: Optional[Sequence[str]], prompt: str) -> Optional[str]:
    blob = " ; ".join([norm_space(concept)] + [norm_space(t) for t in (tags or [])])
    if re.search(r"shuffl", blob, re.I):
        if re.search(r"passage|paragraph", blob, re.I):
            return "Shuffling of Sentences in a passage"
        return "Shuffling of Sentence parts"
    if re.search(r"error", blob, re.I):
        return "Error Detection"
    if re.search(r"sentence improvement|phrase replacement", blob, re.I):
        return "Sentence Improvement"
    if re.search(r"\bmodal\b", blob, re.I):
        return "Modal"
    if re.search(r"phrasal", blob, re.I):
        return "Phrasal Verb"
    if re.search(r"sentence structure", blob, re.I):
        return "Sentence Structure"
    for name, pattern in TYPE_PATTERNS:
        if pattern.search(prompt or ""):
            return name
    return None

def is_grammar_question(
    concept: Optional[str], tags: Optional[Sequence[str]], prompt: str, topic: Optional[str]
) -> bool:
    """Grammar questions are English questions whose label is a grammar label.

    Vocabulary-style labels (Synonym/Antonym/Idioms/OWS/Spelling/Homophones) and
    comprehension/verbal-ability labels are excluded — they are handled by
    :mod:`agent.vocab` and the verbal-ability chapters.
    """

    label = norm_space(concept).lower()
    tag_text = " ; ".join(norm_space(t) for t in (tags or [])).lower()
    blob = f"{label} {tag_text}"
    if topic and norm_space(topic).lower() == "vocabulary":
        return False
    if re.search(r"synonym|antonym|idiom|ows|one word substitution|spelling|homophone|homonym|cloze|para jumble|reading comprehension", blob, re.I):
        return False
    if label in ("vocabulary", "verbal ability", "cloze test", "para jumbles", "reading comprehension", "fill in the blanks"):
        return False
    if detect_type(concept, tags, prompt) is not None:
        return True
    return bool(re.search(r"grammar|sentence|voice|narration|parts of speech|tense|article|preposition", blob, re.I))

@dataclass
class RuleMatch:
    rule: Optional[int]
    title: str
    score: int
    matched_terms: List[str]
    hint: bool

    def as_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule,
            "rule_title": self.title,
            "score": self.score,
            "matched_terms": self.matched_terms,
            "type_hint": self.hint,
        }

def match_rule(
    rules: Sequence[GrammarRule],
    *,
    concept: Optional[str],
    tags: Optional[Sequence[str]],
    prompt: str,
    options: Sequence[str],
    qtype: Optional[str],
) -> RuleMatch:
    terms = content_terms(
        " ".join(
            [
                norm_space(concept),
                " ".join(norm_space(t) for t in (tags or [])),
                prompt,
                " ".join(options or ()),
            ]
        )
    )
    hints = TYPE_RULE_HINTS.get(qtype or "", ())
    best: Optional[RuleMatch] = None
    for rule in rules:
        title_hits = rule.title_terms & terms
        topic_hits = rule.topic_terms & terms
        score = 3 * len(title_hits) + 2 * len(topic_hits)
        hint = False
        for hint_title in hints:
            if norm_key(hint_title) == norm_key(rule.title) or norm_key(hint_title) in norm_key(rule.title):
                score += 4
                hint = True
                break
        matched = sorted(title_hits | topic_hits)
        if score <= 0:
            continue
        candidate = RuleMatch(rule=rule.number, title=rule.title, score=score, matched_terms=matched, hint=hint)
        if best is None or (candidate.score, -candidate.rule) > (best.score, -best.rule):
            best = candidate
    if best is None:
        return RuleMatch(rule=None, title="", score=0, matched_terms=[], hint=False)
    return best

# ---------------------------------------------------------------------------
# build
# ---------------------------------------------------------------------------

@dataclass
class GrammarResult:
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

def _md_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out)

def render_rules_md(
    rules: Sequence[GrammarRule],
    per_rule: Dict[int, Dict[str, Any]],
    type_counts: Counter,
    unmapped: int,
) -> str:
    lines: List[str] = []
    add = lines.append
    add("# Grammar questions mapped to the 129 rules")
    add("")
    total = sum(per_rule.get(r.number, {}).get("questions", 0) for r in rules)
    add(
        f"- Rules available: **{len(rules)}**\n"
        f"- Grammar questions matched: **{human_int(total)}**\n"
        f"- Grammar questions unmapped: **{human_int(unmapped)}**"
    )
    add("")
    add("## Questions by question type")
    add("")
    add(
        _md_table(
            ["Question type", "Questions"],
            [[name, human_int(type_counts[name])] for name in GRAMMAR_TYPES if type_counts[name]],
        )
    )
    add("")
    add("## Rules ranked by question volume")
    add("")
    add(
        _md_table(
            ["Rule", "Title", "Topic", "Questions", "Top exam"],
            [
                [
                    r.number,
                    r.title,
                    r.topic or "-",
                    human_int(per_rule.get(r.number, {}).get("questions", 0)),
                    (per_rule.get(r.number, {}).get("top_exam") or "-"),
                ]
                for r in sorted(
                    rules, key=lambda r: (-per_rule.get(r.number, {}).get("questions", 0), r.number)
                )
            ],
        )
    )
    add("")
    add("## Rules with no matched questions")
    add("")
    empty = [r for r in rules if not per_rule.get(r.number, {}).get("questions")]
    if empty:
        add(", ".join(f"{r.number}. {r.title}" for r in empty))
    else:
        add("_None — every rule has at least one question._")
    add("")
    return "\n".join(lines)

def build_grammar_db(
    records: Sequence[Dict[str, Any]],
    questions_by_qid: Dict[str, Dict[str, Any]],
    taxonomy: Dict[str, Any],
    *,
    out_dir: Optional[Path] = None,
    log: Optional[Log] = None,
) -> GrammarResult:
    logger = log or Log("grammar")
    target = out_dir or paths.GRAMMAR_DB_DIR
    rules = build_rule_index(taxonomy)
    if not rules:
        logger.warn("no grammar rules found in the taxonomy")
        return GrammarResult("ok", {"rules": 0}, [], ["taxonomy has no ENG rules"])

    matched: List[Dict[str, Any]] = []
    unmapped: List[Dict[str, Any]] = []
    type_counts: Counter = Counter()
    per_rule: Dict[int, Dict[str, Any]] = defaultdict(lambda: {"questions": 0, "exams": Counter()})

    for record in records:
        if record.get("subject") != "ENG":
            continue
        question = questions_by_qid.get(str(record.get("qid")))
        if question is None:
            continue
        prompt = str(question.get("question") or "")
        if not is_grammar_question(
            question.get("concept"), question.get("tags"), prompt, record.get("topic")
        ):
            continue
        qtype = detect_type(question.get("concept"), question.get("tags"), prompt)
        if qtype:
            type_counts[qtype] += 1
        options = [str(o.get("text") or "") for o in iter_options(question)]
        result = match_rule(
            rules,
            concept=question.get("concept"),
            tags=question.get("tags"),
            prompt=prompt,
            options=options,
            qtype=qtype,
        )
        payload = {
            "qid": record.get("qid"),
            "exam": record.get("exam"),
            "year": record.get("year"),
            "paper_path": record.get("paper_path"),
            "ordinal": record.get("ordinal"),
            "concept_raw": record.get("concept_raw"),
            "question_type": qtype,
            **result.as_dict(),
        }
        if result.rule is None:
            unmapped.append(payload)
            continue
        matched.append(payload)
        entry = per_rule[result.rule]
        entry["questions"] += 1
        entry["exams"][record.get("exam")] += 1

    for entry in per_rule.values():
        exams = entry.get("exams") or Counter()
        entry["top_exam"] = exams.most_common(1)[0][0] if exams else ""

    paths.ensure_dir(target)
    files: List[Path] = []
    rules_json = target / "rules.json"
    write_json(
        rules_json,
        {
            "version": 1,
            "generated_by": "agent.grammar",
            "rule_count": len(rules),
            "rules": [r.as_dict() for r in rules],
            "per_rule": {
                str(number): {
                    "questions": entry["questions"],
                    "top_exam": entry.get("top_exam", ""),
                    "exams": dict(sorted((entry.get("exams") or {}).items())),
                }
                for number, entry in sorted(per_rule.items())
            },
        },
    )
    files.append(rules_json)

    questions_jsonl = target / "questions.jsonl"
    write_jsonl(questions_jsonl, sorted(matched, key=lambda r: (str(r["exam"]), int(r["year"] or 0), str(r["qid"]))))
    files.append(questions_jsonl)

    unmapped_jsonl = target / "unmapped.jsonl"
    write_jsonl(unmapped_jsonl, sorted(unmapped, key=lambda r: (str(r["exam"]), int(r["year"] or 0), str(r["qid"]))))
    files.append(unmapped_jsonl)

    md = target / "rules.md"
    write_text(md, render_rules_md(rules, per_rule, type_counts, len(unmapped)))
    files.append(md)

    counters = {
        "rules": len(rules),
        "grammar_questions_matched": len(matched),
        "grammar_questions_unmapped": len(unmapped),
        "rules_with_questions": len(per_rule),
        **{f"type:{k}": v for k, v in sorted(type_counts.items())},
    }
    logger.info(
        f"grammar: {len(matched)} mapped / {len(unmapped)} unmapped across {len(per_rule)} rules"
    )
    return GrammarResult("ok", counters, files)

def resolve_questions_for_records(
    papers: Sequence[Any], records: Sequence[Dict[str, Any]]
) -> Dict[str, Dict[str, Any]]:
    """Map ``qid -> raw question`` for the given index records.

    Only the papers referenced by *records* are read.
    """

    needed = {str(r.get("qid")) for r in records}
    wanted_paths = {str(r.get("paper_path")) for r in records}
    out: Dict[str, Dict[str, Any]] = {}
    for paper in papers:
        if paper.path not in wanted_paths:
            continue
        for question in paper.questions:
            qid = str(question.get("qid", ""))
            if qid in needed:
                out[qid] = question
    return out
