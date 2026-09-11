"""Full-text parsing of the 129 numbered grammar rules.

``agent.taxonomy`` only lifts the *headline* of every rule out of
``chapter-and-topic/english-grammar-rules.md`` (``## Rule N: Title`` +
``**Topic:**``).  The interesting material for routing a question to a rule
lives in the rest of the block:

* the **body** prose — it names the tested forms (``present perfect``,
  ``past participle``, ``intransitive verb``);
* the **examples** — every rule ships ✗/✓ sentence pairs, i.e. the exact
  mistakes SSC asks about;
* the **quoted expressions** — the rule author literally quotes the signal
  words it distinguishes (``"due to"`` vs ``"because of"``, ``'since'``,
  ``"each of"``).

This module turns each of those into term sets, so the matcher in
:mod:`agent.grammar` scores a question against the *whole* rule instead of its
title alone.  Nothing here writes to the raw taxonomy file — the parser is
read-only and pure, and the extracted text is stored in ``rules.json`` so a
rule can always be read back next to the questions it received.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import paths

#: ``## Rule 1: Since, For, and From`` — the rule header
RULE_HEADER_RE = re.compile(r"^##\s+Rule\s+(\d+)\s*:\s*(.+?)\s*$", re.M)
TOPIC_RE = re.compile(r"^\*\*Topic:\*\*\s*(.+?)\s*$", re.M)
SOURCES_RE = re.compile(r"^\*\*Sources:\*\*\s*(.+?)\s*$", re.M)
EXAMPLES_RE = re.compile(r"^\*\*Examples\*\*\s*$", re.M)
BULLET_RE = re.compile(r"^\s*[-*]\s+(?P<body>.+?)\s*$", re.M)

#: quoted material: “due to” / "because of" / 'since' / `for the past two weeks`
QUOTED_RES = (
    re.compile(r"\u201c([^\u201d]{2,60})\u201d"),  # “ ”
    re.compile(r"\u2018([^\u2019]{2,60})\u2019"),  # ‘ ’
    re.compile(r'"([^"\n]{2,60})"'),
    re.compile(r"'([^'\n]{2,60})'"),
    re.compile(r"`([^`\n]{2,60})`"),
)

#: ✗ marks a wrong example, ✓ the corrected one
WRONG_MARKERS = ("\u2717", "\u2718", "\u00d7")
RIGHT_MARKERS = ("\u2713", "\u2714")

#: trailing punctuation that sticks to a quoted expression (``“caused by,”``)
QUOTE_TRIM = " \t\r\n.,;:!?\u2019\u201d\"')]"

#: quoted material that is a *cross reference*, not a signal word
_QUOTE_STOPWORDS = {
    "verb",
    "verbs",
    "noun",
    "nouns",
    "sentence",
    "sentences",
    "example",
    "examples",
    "subject",
    "object",
    "gerund",
    "infinitive",
    "participle",
    "clause",
    "phrase",
    "tense",
    "voice",
    "passive",
    "active",
    "singular",
    "plural",
}


@dataclass
class RuleText:
    """One ``## Rule`` block, split into its parts."""

    number: int
    title: str
    topic: str = ""
    sources: str = ""
    body: str = ""
    examples: Tuple[str, ...] = ()
    wrong_examples: Tuple[str, ...] = ()
    right_examples: Tuple[str, ...] = ()
    #: quoted expressions found in the body (and in the examples)
    quoted: Tuple[str, ...] = ()

    def as_dict(self) -> Dict[str, object]:
        return {
            "rule": self.number,
            "title": self.title,
            "topic": self.topic,
            "sources": self.sources,
            "body": self.body,
            "examples": list(self.examples),
            "wrong_examples": list(self.wrong_examples),
            "right_examples": list(self.right_examples),
            "quoted": list(self.quoted),
        }


def default_rules_md() -> Path:
    """The taxonomy markdown that declares the 129 rules."""

    return paths.TAXONOMY_DIR / paths.TAXONOMY_FILES["ENG"]


def _trim_quote(value: str) -> str:
    return value.strip().strip(QUOTE_TRIM).strip()


def extract_quoted(text: str) -> List[str]:
    """Every quoted expression of *text*, de-duplicated, in order of appearance.

    The rule bodies quote the exact forms they teach — those quotes are the
    single richest source of signal words (``“due to”``, ``“ought to”``,
    ``"either...or"``).  Cross-references to grammar categories (``“verb”``)
    are dropped: they say what a thing *is*, not what a question looks like.
    """

    out: List[str] = []
    seen: Set[str] = set()
    spans: List[Tuple[int, str]] = []
    for pattern in QUOTED_RES:
        for match in pattern.finditer(text or ""):
            value = _trim_quote(match.group(1))
            if not value:
                continue
            spans.append((match.start(), value))
    for _position, value in sorted(spans, key=lambda item: item[0]):
        key = value.lower()
        if key in seen:
            continue
        seen.add(key)
        if key in _QUOTE_STOPWORDS:
            continue
        out.append(value)
    return out


def _split_sections(block: str) -> Tuple[str, str]:
    """``(prose, examples)`` of one rule block."""

    match = EXAMPLES_RE.search(block)
    if match is None:
        return block.strip(), ""
    return block[: match.start()].strip(), block[match.end() :].strip()


def _classify_examples(section: str) -> Tuple[List[str], List[str], List[str]]:
    """``(all, wrong, right)`` bullet lines of the ``**Examples**`` section."""

    all_examples: List[str] = []
    wrong: List[str] = []
    right: List[str] = []
    for match in BULLET_RE.finditer(section or ""):
        body = match.group("body").strip()
        if not body:
            continue
        stripped = body.lstrip("*_ ")
        all_examples.append(body)
        if stripped.startswith(RIGHT_MARKERS):
            right.append(stripped[1:].strip())
        elif stripped.startswith(WRONG_MARKERS):
            wrong.append(stripped[1:].strip())
    return all_examples, wrong, right


def parse_rules_md(text: str) -> List[RuleText]:
    """Parse the rule markdown into :class:`RuleText` records, ordered by number."""

    headers = list(RULE_HEADER_RE.finditer(text or ""))
    out: List[RuleText] = []
    for index, header in enumerate(headers):
        end = headers[index + 1].start() if index + 1 < len(headers) else len(text)
        block = text[header.end() : end]
        topic_match = TOPIC_RE.search(block)
        sources_match = SOURCES_RE.search(block)
        prose, examples_section = _split_sections(block)
        # drop the metadata lines that precede the prose
        prose = TOPIC_RE.sub("", prose)
        prose = SOURCES_RE.sub("", prose).strip()
        all_examples, wrong, right = _classify_examples(examples_section)
        quoted = extract_quoted(prose)
        known = {value.lower() for value in quoted}
        for value in extract_quoted(" \n".join(all_examples)):
            if value.lower() not in known:
                known.add(value.lower())
                quoted.append(value)
        out.append(
            RuleText(
                number=int(header.group(1)),
                title=header.group(2).strip(),
                topic=topic_match.group(1).strip() if topic_match else "",
                sources=sources_match.group(1).strip() if sources_match else "",
                body=prose,
                examples=tuple(all_examples),
                wrong_examples=tuple(wrong),
                right_examples=tuple(right),
                quoted=tuple(quoted),
            )
        )
    out.sort(key=lambda rule: rule.number)
    return out


def load_rule_texts(path: Optional[Path] = None) -> List[RuleText]:
    """Read + parse the taxonomy markdown; ``[]`` when the file is missing."""

    target = Path(path) if path is not None else default_rules_md()
    if not target.is_file():
        return []
    return parse_rules_md(target.read_text(encoding="utf-8"))


# ---------------------------------------------------------------------------
# signal words
# ---------------------------------------------------------------------------

#: Curated signal words per rule — the words whose *presence in the question*
#: is close to proof of the rule, including the ones the rule text mentions only
#: indirectly.  Kept deliberately small and explicit: a wrong signal costs more
#: than a missing one, because the AI pass picks up whatever this misses.
CURATED_SIGNALS: Dict[int, Tuple[str, ...]] = {
    1: ("since", "for", "from", "till", "until"),
    2: ("each", "every", "either", "neither", "none", "every one of"),
    3: ("who", "whom", "whose", "which", "that", "who's"),
    4: ("can", "could", "may", "might", "must", "should", "would", "ought to", "used to", "had better", "would rather", "need not", "dare not"),
    5: ("furniture", "advice", "information", "equipment", "news", "mathematics", "cattle", "police", "people", "scissors", "trousers", "sheep", "deer", "aircraft", "mice", "teeth", "passers-by"),
    6: ("gerund", "infinitive", "to", "-ing"),
    7: ("gerund", "enjoy", "avoid", "mind", "suggest", "look forward to"),
    8: ("participle", "participles", "present participle", "past participle", "-ing", "v3"),
    9: ("the", "a", "an", "articles"),
    10: ("a", "an", "the", "articles"),
    11: ("the", "superlative", "ordinal", "first", "second"),
    12: ("the", "a", "an", "articles"),
    13: ("the", "a", "an", "articles"),
    14: ("some", "any", "much", "many", "few", "little", "a lot of", "lots of", "enough"),
    15: ("a number of", "the number of", "amount of", "quantity of"),
    16: ("more", "most", "better", "best", "double comparative", "than"),
    17: ("passive", "active", "voice", "by", "v3", "past participle", "be", "been"),
    18: ("as well as", "along with", "together with", "besides", "accompanied by", "in addition to"),
    19: ("not only but also", "either or", "neither nor", "both and", "correlative"),
    20: ("no sooner", "hardly", "scarcely", "than", "when"),
    21: ("verb forms", "v1", "v2", "v3", "participle"),
    22: ("quite", "quiet", "quitting"),
    23: ("late", "lately", "later", "latest"),
    24: ("look", "feel", "taste", "smell", "sound", "seem", "appear", "become", "linking verb"),
    25: ("fast", "hard", "late", "early", "long", "high", "low", "near", "deep", "wide"),
    26: ("hard", "hardly", "hardly any"),
    27: ("farther", "further", "furthest"),
    28: ("too much", "much too", "too many", "far too"),
    29: ("-ly", "friendly", "lovely", "costly", "orderly", "adjective"),
    30: ("one of", "with", "along with", "including", "as well as", "head subject"),
    31: ("much", "very", "far", "a lot", "slightly", "any", "no", "than"),
    32: ("redundancy", "repeat", "again", "return back", "discuss about", "revert back", "more better"),
    33: ("if", "unless", "were", "had", "would have", "conditional"),
    34: ("two", "three", "repeated noun", "singular"),
    35: ("between", "among", "amongst"),
    36: ("beside", "besides"),
    37: ("preposition", "discuss", "order", "reach", "discuss about", "order for", "reach at"),
    38: ("each other", "one another"),
    39: ("you and i", "i and you", "he and i", "pronoun order", "231", "123"),
    40: ("unless", "until", "till", "not"),
    41: ("one", "one's", "ones", "one of"),
    42: ("not only but also", "both and", "either or", "neither nor", "no sooner than", "hardly when", "scarcely when", "such as"),
    43: ("though", "although", "but", "because", "so", "therefore"),
    44: ("its", "it's", "their", "there", "whose", "who's", "your", "you're"),
    45: ("hardly", "scarcely", "never", "seldom", "no sooner", "rarely", "inversion"),
    46: ("isn't it", "aren't they", "question tag", "everyone", "nobody", "somebody", "anybody"),
    47: ("advice", "advise", "practice", "practise", "effect", "affect", "noun verb"),
    48: ("that", "what", "which"),
    49: ("two", "three", "hundred", "thousand", "million", "numeral", "compound modifier"),
    50: ("if", "whether", "that"),
    51: ("the", "articles", "definite article"),
    52: ("the", "articles", "and"),
    53: ("at home", "in bed", "on foot", "by bus", "articles", "idiom"),
    54: ("'s", "apostrophe", "possessive", "mothers-in-law", "passers-by"),
    55: ("not only", "rather than", "both", "parallel", "and", "or", "parallelism"),
    56: ("make", "get", "have", "let", "causative", "help"),
    57: ("half", "quarter", "two-thirds", "percent", "fraction", "of"),
    58: ("first", "second", "last", "numeral", "order"),
    59: ("beautiful", "big", "old", "round", "wooden", "italian", "adjective order", "osascomp"),
    60: ("made of", "made from", "made with", "made by"),
    61: ("some", "any"),
    62: ("myself", "yourself", "himself", "herself", "itself", "ourselves", "themselves", "reflexive"),
    63: ("who", "which", "that", "antecedent", "relative pronoun"),
    64: ("me", "him", "her", "us", "them", "between you and", "preposition"),
    65: ("that of", "those of", "than that of", "comparison"),
    66: ("than i", "than me", "than he", "than him", "pronoun case"),
    67: ("good", "better", "best", "bad", "worse", "worst", "degree"),
    68: ("fall", "fell", "rise", "raise", "lie", "lay", "lain", "laid"),
    69: ("than", "compared", "group", "other"),
    70: ("perfect", "unique", "complete", "full", "empty", "round", "absolute"),
    71: ("stative", "know", "believe", "understand", "want", "like", "own", "belong", "progressive"),
    72: ("regard", "consider", "call", "name", "appoint", "elect", "as"),
    73: ("always", "never", "often", "seldom", "rarely", "usually", "sometimes", "adverb of frequency"),
    74: ("much", "many", "little", "few", "uncountable", "countable"),
    75: ("information", "advice", "furniture", "luggage", "baggage", "equipment", "scenery"),
    76: ("used to", "be used to", "get used to", "-ing"),
    77: ("a", "an", "the", "articles"),
    78: ("one of", "the rich", "the poor", "agreement"),
    79: ("gerund", "infinitive", "-ing", "to", "subject"),
    80: ("yet", "already", "just", "just now", "so far", "lately", "recently", "of late", "before", "after"),
    81: ("by the time", "had", "will have"),
    82: ("question tag", "isn't it", "aren't", "present continuous", "present simple"),
    83: ("tense", "meaning"),
    84: ("question tag", "yesterday", "tomorrow", "last", "next"),
    85: ("because", "because of", "supposing", "since", "as"),
    86: ("such as", "such that", "so as"),
    87: ("because of", "in case", "unless", "because"),
    88: ("angry", "annoyed", "vexed", "disgusted", "at", "with"),
    89: ("and", "preposition", "different prepositions"),
    90: ("to", "for", "suggest to", "explain to", "announce to"),
    91: ("supply", "supply with", "supply to"),
    92: ("preposition", "fixed preposition", "comprise", "composed of", "superior to", "inferior to", "prefer to"),
    93: ("to", "infinitive", "noun", "pronoun"),
    94: ("some", "any"),
    95: ("i wish", "were", "had", "wish"),
    96: ("bring", "take", "brought", "took"),
    97: ("-ing", "present participle", "subject", "dangling"),
    98: ("than", "then"),
    99: ("although", "though", "even though", "but", "yet"),
    100: ("even if", "even though"),
    101: ("as if", "as though", "were"),
    102: ("so that", "in order that", "that"),
    103: ("so", "such", "so that", "such that"),
    104: ("while", "when", "whereas"),
    105: ("because", "since", "as", "for"),
    106: ("as", "like", "as if", "such as"),
    107: ("due to", "because of"),
    108: ("all", "whole", "the whole"),
    109: ("one of the most", "plural noun", "superlative"),
    110: ("who", "which", "that", "one of those", "agreement"),
    111: ("so that", "in order that", "purpose"),
    112: ("lest", "should", "not"),
    113: ("as if", "as though", "were"),
    114: ("not", "never", "no", "hardly", "scarcely", "barely", "without", "double negative"),
    115: ("past perfect", "had", "before", "after", "two past actions"),
    116: ("because of", "due to"),
    117: ("despite", "although", "however", "in spite of", "though"),
    118: ("did", "does", "do", "emphatic"),
    119: ("by", "until", "till"),
    120: ("even", "even though", "even if"),
    121: ("hope", "wish", "hoped", "wished"),
    122: ("sometime", "sometimes", "some time"),
    123: ("else", "other", "another", "others"),
    124: ("hardly", "barely", "scarcely", "not", "no", "never"),
    125: ("yet", "still", "already"),
    126: ("at", "on", "in", "time", "date"),
    127: ("in case", "if", "unless"),
    128: ("myself", "yourself", "himself", "herself", "itself", "themselves", "emphatic"),
    129: ("when", "while", "as", "time conjunction"),
}


def curated_signals(number: int) -> Tuple[str, ...]:
    return CURATED_SIGNALS.get(int(number), ())


def clean_signal(value: str) -> str:
    """Normalise a signal term (trim quotes/punctuation, collapse space)."""

    return re.sub(r"\s+", " ", value.strip().strip(QUOTE_TRIM)).strip().lower()


def signal_phrases(text: RuleText) -> Tuple[str, ...]:
    """Quoted multi-word expressions of a rule — the *strongest* signal.

    ``“due to”``, ``“because of”``, ``“not only...but also”``: a question that
    contains such a phrase is almost certainly about this rule.  Ellipsis
    (``not only...but also``) is expanded into its concrete collocations so the
    phrase can actually be found in a question.
    """

    out: List[str] = []
    for value in text.quoted:
        cleaned = clean_signal(value)
        if not cleaned:
            continue
        if "..." in cleaned:
            head, _, tail = cleaned.partition("...")
            joined = f"{head.strip()} {tail.strip()}".strip()
            if joined:
                out.append(joined)
            continue
        if " " in cleaned or "-" in cleaned:
            out.append(cleaned)
    return tuple(dict.fromkeys(out))


def signal_words(text: RuleText) -> Tuple[str, ...]:
    """Single-token signals: curated table + every single-word quote."""

    out: List[str] = []
    for value in curated_signals(text.number):
        cleaned = clean_signal(value)
        if cleaned and " " not in cleaned:
            out.append(cleaned)
    for value in text.quoted:
        cleaned = clean_signal(value)
        if cleaned and " " not in cleaned and len(cleaned) > 2:
            out.append(cleaned)
    return tuple(dict.fromkeys(out))


def all_signal_phrases(text: RuleText) -> Tuple[str, ...]:
    """Curated multi-word signals + quoted multi-word signals."""

    out: List[str] = []
    for value in curated_signals(text.number):
        cleaned = clean_signal(value)
        if cleaned and (" " in cleaned or "-" in cleaned):
            out.append(cleaned)
    out.extend(signal_phrases(text))
    return tuple(dict.fromkeys(out))


def iter_rule_texts(texts: Iterable[RuleText]) -> Iterable[Tuple[int, RuleText]]:
    for text in texts:
        yield text.number, text


def body_sentences(body: str) -> Tuple[str, ...]:
    """The body prose split into sentences (used for the rule page + terms)."""

    flat = re.sub(r"\s+", " ", body or "").strip()
    if not flat:
        return ()
    parts = re.split(r"(?<=[.!?])\s+", flat)
    return tuple(part.strip() for part in parts if part.strip())
