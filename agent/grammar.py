"""Grammar question -> rule mapping over the 129 numbered grammar rules.

Each of the 129 rules of ``chapter-and-topic/english-grammar-rules.md`` becomes
a browsable database node carrying the questions that test it.  A question is
routed in two passes:

1. **deterministic** (:func:`match_rule`) — scores the question text, its
   options and its question type against the *whole* rule, not just the title:
   the parsed body, the ✗/✓ examples and the quoted signal words all contribute
   (:mod:`agent.grammar_rules`).  A minimum score is required and the most
   specific rule wins an otherwise equal contest.
2. **AI** (:func:`run_ai_pass`) — whatever the deterministic pass cannot place
   is offered to the two-model debate: **deepseek-v4-flash proposes** a rule and
   **gpt-5.6-sol confirms or overrides** it (the judge's answer is final).  The
   pass is batched, resumable, and stops softly (``ai_unavailable``) when no
   route can serve a role — the questions then stay in ``unassigned.jsonl``
   instead of being guessed onto a rule.

Output (``database/english/_analysis/grammar/``) is the analysis view;
``database/english/grammar/<NN>-<slug>/`` is the browsable per-rule node with
its own ``index.md`` + ``questions.jsonl``, and ``database/english/grammar/
index.md`` carries the rule matrix.  The rule node is the **one** home of the
questions it owns: the English concept tree is re-filed with those qids skipped
(:func:`assigned_qids`, audit rule 5), so a leaf such as
``english/active-and-passive-voice`` no longer repeats what rule 17 already
carries.  Questions no rule claims stay where they are.
"""

from __future__ import annotations

import hashlib
import json
import os
import re
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from . import build_db
from . import debate as debate_mod
from . import errors
from . import grammar_rules, llm, paths, router as router_mod
from .config import Settings
from .grammar_rules import RuleText
from .sections import iter_options
from .util import (
    Log,
    append_jsonl,
    chunked,
    human_int,
    norm_key,
    norm_space,
    now_iso,
    pct,
    read_json,
    slugify,
    write_json,
    write_jsonl,
    write_text,
)

# ---------------------------------------------------------------------------
# question types
# ---------------------------------------------------------------------------

GRAMMAR_TYPES = (
    "Error Detection",
    "Sentence Improvement",
    "Fill in the Blanks",
    "Sentence Structure",
    "Shuffling of Sentence parts",
    "Shuffling of Sentences in a passage",
    "Direct and Indirect Speech",
    "Active and Passive Voice",
    "Modal",
    "Phrasal Verb",
)

#: ordered: the first pattern that matches wins, so the narrow question shapes
#: come before the wide ones (a passive-voice prompt must not be read as a
#: generic sentence-improvement one).
TYPE_PATTERNS: Tuple[Tuple[str, "re.Pattern[str]"], ...] = (
    ("Phrasal Verb", re.compile(r"phrasal verb", re.I)),
    ("Modal", re.compile(r"\bmodal(?:s| verb| verbs)?\b", re.I)),
    (
        "Direct and Indirect Speech",
        re.compile(
            r"direct and indirect|indirect (?:speech|form|voice|narration)"
            r"|direct (?:speech|form|voice)"
            r"|(?:indirect|direct) form of the (?:given )?sentence"
            r"|narration"
            r"|reported speech",
            re.I,
        ),
    ),
    (
        "Active and Passive Voice",
        re.compile(
            r"passive (?:voice|form|construction|transformation)"
            r"|active (?:voice|form|transformation)"
            r"|into (?:the )?(?:passive|active)\b"
            r"|(?:passive|active) voice of the following",
            re.I,
        ),
    ),
    (
        "Shuffling of Sentences in a passage",
        re.compile(
            r"(?:four|4) jumbled sentences"
            r"|sentences? of a paragraph .{0,40}jumbled"
            r"|jumbled sentences"
            r"|given below in jumbled order"
            r"|meaningful and coherent paragraph"
            r"|paragraph (?:has been|is) .{0,30}jumbled",
            re.I,
        ),
    ),
    (
        "Shuffling of Sentence parts",
        re.compile(
            r"parts? of (?:the |a |the following )?sentence .{0,40}(?:jumbled|options|given)"
            r"|arrange the parts"
            r"|parts are given below in jumbled order"
            r"|rearrange the parts of the sentence",
            re.I,
        ),
    ),
    (
        "Error Detection",
        re.compile(
            r"error (?:detection|spotting)"
            r"|spot the error"
            r"|contains? (?:a |an |the )?(?:grammatical )?error"
            r"|has an error in it"
            r"|incorrect part"
            r"|identify the (?:incorrect|wrong) (?:sentence|part|segment)"
            r"|(?:split|divided) into (?:four |4 )?(?:segments|parts)"
            r"|part that contains the error",
            re.I,
        ),
    ),
    (
        "Sentence Improvement",
        re.compile(
            r"sentence improvement"
            r"|improve the (?:sentence|underlined|highlighted)"
            r"|phrase replacement"
            r"|substitute(?:s|d)? (?:the )?(?:underlined|bracketed|highlighted)"
            r"|replace(?:s|d)? the (?:underlined|bracketed|highlighted)"
            r"|substitutes? \(replaces\)"
            r"|no improvement",
            re.I,
        ),
    ),
    (
        "Fill in the Blanks",
        re.compile(
            r"fill in the blanks?"
            r"|blank(?:s)? (?:has been|is|are) (?:given|to be filled)"
            r"|blank to be filled"
            r"|complete the (?:sentence|paragraph)"
            r"|with a blank to be filled",
            re.I,
        ),
    ),
    ("Sentence Structure", re.compile(r"sentence structure|structurally", re.I)),
)

#: the label of a question *is* the question type — no prompt inspection needed
LABEL_TO_TYPE: Dict[str, str] = {
    "error detection": "Error Detection",
    "error spotting": "Error Detection",
    "spotting errors": "Error Detection",
    "sentence improvement": "Sentence Improvement",
    "fill in the blanks": "Fill in the Blanks",
    "fill in the blank": "Fill in the Blanks",
    "sentence structure": "Sentence Structure",
    "shuffling of sentence parts": "Shuffling of Sentence parts",
    "shuffling of sentences in a passage": "Shuffling of Sentences in a passage",
    "para jumbles": "Shuffling of Sentences in a passage",
    "para jumble": "Shuffling of Sentences in a passage",
    "direct and indirect speech": "Direct and Indirect Speech",
    "direct & indirect speech": "Direct and Indirect Speech",
    "narration": "Direct and Indirect Speech",
    "active and passive voice": "Active and Passive Voice",
    "active & passive voice": "Active and Passive Voice",
    "voice": "Active and Passive Voice",
    "modal": "Modal",
    "modal verbs": "Modal",
    "phrasal verb": "Phrasal Verb",
    "phrasal verbs": "Phrasal Verb",
}

#: a rule whose *title* names the question type outright (strong hint: it may
#: outrank a weak keyword hit on another rule)
TYPE_RULE_HINTS: Dict[str, Tuple[str, ...]] = {
    "Active and Passive Voice": ("Active and Passive Voice",),
    "Modal": ("Modal Verbs",),
}

#: related rules (weak hint: only reinforces an already-scoring rule)
TYPE_RULE_RELATED: Dict[str, Tuple[str, ...]] = {
    "Phrasal Verb": ("Fixed Preposition Combinations", "No Extra Preposition", "Prepositions"),
    "Shuffling of Sentence parts": ("Inversion", "Parallel Structure", "Pronoun Order", "Adjective Order"),
    "Shuffling of Sentences in a passage": ("Correlative Conjunctions", "Parallel Structure", "Correct Conjunctions with 'Reason' and 'Supposing'"),
    "Sentence Structure": ("Parallel Structure", "Inversion", "Pronoun Order", "Word Order"),
}

#: labels that are never grammar (handled by agent.vocab / the verbal chapters)
NON_GRAMMAR_LABEL_RE = re.compile(
    r"synonym|antonym|idiom|ows\b|one word substitution|spelling|homophone|homonym"
    r"|cloze|para jumble|reading comprehension|match the following",
    re.I,
)

#: labels that mean "grammar" without naming a grammar point
GENERIC_GRAMMAR_RE = re.compile(r"^grammar(\s*,.*)?$|^grammar\b|\bgrammar$", re.I)

#: Words removed from the **term bags** (question and rule alike).
#:
#: Only two kinds of word belong here: the task vocabulary that every SSC
#: prompt repeats (``select``, ``option``, ``underlined`` …) and function words
#: that occur in almost every rule title (``a``, ``of``, ``and`` …).  Grammar
#: words are deliberately **kept** — ``which``, ``whom``, ``than``, ``if``,
#: ``when``, ``each``, ``since`` … are exactly what distinguishes one rule from
#: another, and the signal tables (which are never filtered) depend on them.
STOPWORDS = {
    # articles / conjunctions / prepositions / auxiliaries seen in every title
    "a", "an", "the", "of", "and", "or", "in", "on", "at", "by", "with", "into",
    "is", "are", "was", "were", "be", "being", "am", "as", "it", "its",
    "this", "these", "those", "they", "them", "their", "you", "your", "we", "our",
    "he", "she", "his", "her", "him", "i", "me", "us", "my", "do", "does", "did",
    "has", "have", "had", "will", "would", "shall", "should", "can", "could",
    "may", "might", "must", "also", "very", "more", "less", "vs",
    # task vocabulary of the question paper
    "select", "choose", "correct", "incorrect", "given", "following", "sentence",
    "sentences", "word", "words", "part", "parts", "option", "options", "question",
    "improve", "improvement", "error", "errors", "detect", "detection", "spot",
    "below", "above", "underlined", "highlighted", "bracketed", "meaning",
    "meaningfully", "grammatically", "phrase", "phrases", "one", "two", "three",
    "four", "most", "appropriate", "best", "expresses", "expressed", "replacement",
    "replace", "replaces", "replaced", "substitute", "substitutes", "using", "use",
    "used", "uses", "blank", "blanks", "fill", "passage", "jumbled", "rearrange",
    "arranged", "arrange", "sequence", "answer", "statements", "statement",
    "segment", "segments", "completes", "complete", "completing", "identify",
    "find", "containing", "contains", "contain", "contained", "therein",
}

#: The one-word signals that are also task vocabulary: a rule may legitimately
#: teach *use* / *one* / *form*, so they survive in a rule's own tables even
#: though they are filtered out of the noisy question bag.
GRAMMAR_KEEP = {"use", "used", "uses", "using", "one", "correct", "form", "forms", "part"}

#: terms used to build the *question* bag of words
TOKEN_RE = re.compile(r"[a-zA-Z][a-zA-Z'\-]+")
#: image placeholders must never leak into the term bag
IMAGE_RE = re.compile(r"\[IMAGE:\s*[^\]]+\]")


def tokens(text: str) -> List[str]:
    return [t.lower() for t in TOKEN_RE.findall(IMAGE_RE.sub(" ", text or ""))]


def content_terms(text: str, *, keep: Iterable[str] = (), min_len: int = 3) -> Set[str]:
    """Term bag of *text*.

    *keep* lets a caller pull specific words back in (a rule may legitimately
    teach *use* / *one* / *form*, which are noise inside a question), and
    *min_len* is 3 for question text (a stray ``to`` proves nothing there) but 2
    when reading a rule, where ``if`` / ``to`` / ``no`` **are** the rule.
    """

    allowed = set(keep)
    return {
        token
        for token in tokens(text)
        if len(token) >= min_len and (token not in STOPWORDS or token in allowed)
    }


def clean_prompt(text: Any) -> str:
    """Prompt without markdown emphasis and image placeholders (terms only)."""

    value = IMAGE_RE.sub(" ", str(text or ""))
    value = re.sub(r"[*_#>`]+", " ", value)
    return norm_space(value)


# ---------------------------------------------------------------------------
# rule index
# ---------------------------------------------------------------------------


@dataclass
class GrammarRule:
    """One of the 129 rules, with every term set the matcher uses."""

    number: int
    title: str
    topic: str = ""
    #: parsed rule text (body / examples / quotes)
    text: Optional[RuleText] = None
    title_terms: Set[str] = field(default_factory=set)
    topic_terms: Set[str] = field(default_factory=set)
    body_terms: Set[str] = field(default_factory=set)
    example_terms: Set[str] = field(default_factory=set)
    signal_words: Set[str] = field(default_factory=set)
    signal_phrases: Set[str] = field(default_factory=set)

    @property
    def slug(self) -> str:
        return f"{self.number:02d}-{slugify(self.title)}"

    @property
    def term_count(self) -> int:
        """How many terms the rule carries — the *specificity* tie-break.

        A rule that matches with a smaller vocabulary is the more specific one:
        ``Since, For, and From`` (1 title term) beats the ten-term
        ``Because, Since, and As for Reason`` on the bare word *since*.
        """

        return (
            len(self.title_terms)
            + len(self.topic_terms)
            + len(self.signal_words)
            + len(self.signal_phrases)
        )

    def as_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.number,
            "title": self.title,
            "topic": self.topic,
            "slug": self.slug,
            "title_terms": sorted(self.title_terms),
            "topic_terms": sorted(self.topic_terms),
            "signal_words": sorted(self.signal_words),
            "signal_phrases": sorted(self.signal_phrases),
            "body": self.text.body if self.text else "",
            "examples": list(self.text.examples) if self.text else [],
            "quoted": list(self.text.quoted) if self.text else [],
        }


def _chapter_titles(taxonomy: Dict[str, Any]) -> Dict[int, Tuple[str, str]]:
    """``number -> (title, topic)`` from the parsed taxonomy."""

    out: Dict[int, Tuple[str, str]] = {}
    body = (taxonomy.get("subjects") or {}).get("ENG", {})
    for chapter in body.get("chapters", []):
        if chapter.get("kind") == "supplementary":
            continue
        try:
            number = int(chapter.get("id", "0"))
        except (TypeError, ValueError):
            continue
        out[number] = (chapter.get("name", ""), chapter.get("topic", "") or "")
    return out


def build_rule_index(
    taxonomy: Dict[str, Any],
    *,
    rule_texts: Optional[Sequence[RuleText]] = None,
) -> List[GrammarRule]:
    """Every rule of the taxonomy, enriched with its parsed full text.

    Nothing is trimmed here: which terms are too common to count is decided per
    corpus by :class:`CorpusStats`, because the same word can be decisive in one
    corpus and noise in another.
    """

    texts = {text.number: text for text in (rule_texts if rule_texts is not None else grammar_rules.load_rule_texts())}
    rules: List[GrammarRule] = []
    for number, (title, topic) in sorted(_chapter_titles(taxonomy).items()):
        text = texts.get(number)
        if text is None:
            # the taxonomy declares a rule the markdown does not spell out: keep
            # the rule, it simply has no body/examples to match against
            text = RuleText(number=number, title=title, topic=topic)

        title_terms = content_terms(title, keep=GRAMMAR_KEEP, min_len=2)
        topic_terms = content_terms(topic, keep=GRAMMAR_KEEP, min_len=2) - title_terms
        body_terms = content_terms(text.body, keep=GRAMMAR_KEEP, min_len=2)
        example_terms: Set[str] = set()
        for example in text.examples:
            example_terms |= content_terms(example, keep=GRAMMAR_KEEP, min_len=2)
        example_terms -= title_terms | topic_terms | body_terms
        # signal tables are curated/extracted on purpose: they are the evidence
        signals = {grammar_rules.clean_signal(word) for word in grammar_rules.signal_words(text)}
        signals |= {
            grammar_rules.clean_signal(value)
            for value in text.quoted
            if " " not in grammar_rules.clean_signal(value)
        }
        rules.append(
            GrammarRule(
                number=number,
                title=title,
                topic=topic,
                text=text,
                title_terms=title_terms,
                topic_terms=topic_terms,
                body_terms=body_terms,
                example_terms=example_terms,
                signal_words={word for word in signals if word},
                signal_phrases=set(grammar_rules.all_signal_phrases(text)),
            )
        )
    rules.sort(key=lambda rule: rule.number)
    return rules


def rule_vocabulary(rules: Sequence[GrammarRule]) -> List[str]:
    """The 129 labels handed to the model (``NN. Title — Topic``)."""

    return [f"{rule.number}. {rule.title} — {rule.topic or '-'}" for rule in rules]


# ---------------------------------------------------------------------------
# type detection
# ---------------------------------------------------------------------------


def _label_blob(concept: Optional[str], tags: Optional[Sequence[str]]) -> str:
    return " ; ".join([norm_space(concept)] + [norm_space(t) for t in (tags or [])])


def detect_type_from_label(concept: Optional[str], tags: Optional[Sequence[str]]) -> Optional[str]:
    """Question type the concept/tags label alone declares."""

    parts: List[str] = []
    for raw in [concept, *(tags or [])]:
        for piece in norm_space(raw).split(","):
            value = piece.strip().lower()
            if value and value not in parts:
                parts.append(value)
    for value in parts:
        if value in LABEL_TO_TYPE:
            return LABEL_TO_TYPE[value]
    for value in parts:
        if NON_GRAMMAR_LABEL_RE.search(value):
            return None
    return None


def detect_type(concept: Optional[str], tags: Optional[Sequence[str]], prompt: str) -> Optional[str]:
    """Question type from the label, falling back to the prompt phrasing."""

    label_type = detect_type_from_label(concept, tags)
    if label_type:
        return label_type
    text = clean_prompt(prompt)
    for name, pattern in TYPE_PATTERNS:
        if pattern.search(text):
            return name
    return None


def is_grammar_question(
    concept: Optional[str],
    tags: Optional[Sequence[str]],
    prompt: str,
    topic: Optional[str] = None,
) -> bool:
    """Widened grammar test.

    A question is grammar-shaped when

    * its concept/tags name a grammar question type (:data:`LABEL_TO_TYPE`),
    * its concept/tags are ``Grammar`` / ``Grammar, X``, or
    * its prompt is phrased as a grammar task (:data:`TYPE_PATTERNS`).

    Vocabulary labels (Synonym/Antonym/Idioms/OWS/Spelling/Homophones), the
    passage-comprehension labels (Cloze Test, Reading Comprehension, Para
    Jumbles are routed by their *prompt*: only the rearrangement phrasing counts)
    and the whole vocabulary topic stay out.
    """

    blob = _label_blob(concept, tags)
    lowered = blob.lower()
    if topic and norm_space(topic).lower() == "vocabulary":
        return False
    if NON_GRAMMAR_LABEL_RE.search(lowered):
        # a *pure* vocabulary label; "Grammar, Vocabulary" is still grammar
        if not GENERIC_GRAMMAR_RE.search(lowered):
            return False
    if detect_type_from_label(concept, tags) is not None:
        return True
    if GENERIC_GRAMMAR_RE.search(lowered):
        return True
    if re.search(r"grammar|sentence|voice|narration|parts of speech|tense|article|preposition", lowered, re.I):
        return True
    return detect_type(concept, tags, prompt) is not None


# ---------------------------------------------------------------------------
# scoring
# ---------------------------------------------------------------------------

#: Table weights: the signal tables are the most trustworthy evidence, the body
#: prose the least (it is prose, and it mentions neighbouring rules too).
W_PHRASE = 6
W_SIGNAL = 3
W_TITLE = 3
W_TOPIC = 2
W_BODY = 1
W_EXAMPLE = 1
W_HINT_STRONG = 6
W_HINT_WEAK = 2

#: the loose prose tables are capped, so a long rule cannot out-score a precise one
CAP_LOOSE = 4

#: a question must reach this to be attached to a rule without the AI pass
MIN_SCORE = 4

#: …and must clear the runner-up by this factor.  A sentence like *"Success
#: depends ___ hard work"* scores on `hard` for Rule 25 and on `work`/`depends`
#: elsewhere; when two rules read the same question equally well the keyword
#: evidence has not actually decided anything, and the question is handed to the
#: AI pass instead of being given to the alphabetically luckier rule.
MATCH_MARGIN = 1.5

#: scoring version — bump when the weights change so a stored AI decision is not
#: silently mixed with a new keyword scheme
SCORE_VERSION = 5

#: **Rarity buckets.**  A term that shows up in a large share of the questions
#: proves nothing: ``from``/``that``/``for`` sit in the *instruction* of almost
#: every paper ("select the most appropriate option **from** the given options")
#: and would otherwise drag a thousand unrelated questions onto Rule 1 (*Since,
#: For, and From*) and Rule 105 (*Because, Since, and As for Reason*).  Every
#: matched term is therefore multiplied by how rare it is in this corpus.
#:
#: ``share <= 0.01 -> 3`` · ``<= 0.04 -> 2`` · ``<= 0.12 -> 1`` · above ``-> 0``
RARITY_BUCKETS: Tuple[Tuple[float, int], ...] = ((0.01, 3), (0.04, 2), (0.12, 1))
RARITY_DEFAULT = 3  # a term the corpus never saw is as rare as it gets


def rarity_of(share: float) -> int:
    """Multiplier of a term that occurs in *share* of the corpus."""

    for limit, value in RARITY_BUCKETS:
        if share <= limit:
            return value
    return 0


@dataclass
class CorpusStats:
    """Per-corpus frequency table that turns "matched" into "matched how strongly".

    Built from the grammar questions themselves, so the matcher adapts to the
    paper style instead of trusting a hand-written stop list.
    """

    total: int = 0
    terms: Counter = field(default_factory=Counter)
    phrases: Counter = field(default_factory=Counter)

    def term_rarity(self, term: str) -> int:
        if self.total <= 0:
            return RARITY_DEFAULT
        return rarity_of(self.terms.get(term, 0) / self.total)

    def phrase_rarity(self, phrase: str) -> int:
        if self.total <= 0:
            return RARITY_DEFAULT
        return rarity_of(self.phrases.get(norm_key(phrase), 0) / self.total)

    def share(self, term: str) -> float:
        return (self.terms.get(term, 0) / self.total) if self.total else 0.0

    def as_dict(self, *, limit: int = 40) -> Dict[str, Any]:
        return {
            "documents": self.total,
            "most_common_terms": [
                {"term": term, "share": round(count / self.total, 4), "rarity": rarity_of(count / self.total)}
                for term, count in self.terms.most_common(limit)
            ]
            if self.total
            else [],
            "common_phrases": [
                {"phrase": phrase, "count": count}
                for phrase, count in self.phrases.most_common(20)
            ],
        }


def corpus_stats(texts: Sequence[str], rules: Sequence[GrammarRule]) -> CorpusStats:
    """Term + phrase document frequencies of the grammar corpus."""

    terms: Counter = Counter()
    for text in texts:
        for term in content_terms(text):
            terms[term] += 1
    phrases: Set[str] = set()
    for rule in rules:
        phrases |= set(rule.signal_phrases)
    return CorpusStats(
        total=len(texts),
        terms=terms,
        phrases=phrase_document_frequency(texts, phrases),
    )


def phrase_document_frequency(texts: Sequence[str], phrases: Iterable[str]) -> Counter:
    """How many documents contain each multi-word signal phrase.

    The phrases are indexed by their first word, so a document only tests the
    handful of phrases that can actually start in it — a full cross product
    (~1.5k phrases x ~12k questions) would dominate the run.
    """

    by_first: Dict[str, List[str]] = defaultdict(list)
    for phrase in phrases:
        key = norm_key(phrase)
        if not key or " " not in key:
            continue
        by_first[key.split(" ", 1)[0]].append(key)
    out: Counter = Counter()
    for text in texts:
        padded = f" {norm_key(text)} "
        seen: Set[str] = set()
        for token in set(padded.split()):
            for key in by_first.get(token, ()):
                if key in seen:
                    continue
                if f" {key} " in padded:
                    seen.add(key)
                    out[key] += 1
    return out


@dataclass
class RuleMatch:
    rule: Optional[int]
    title: str
    score: int
    matched_terms: List[str]
    hint: bool
    breakdown: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        payload: Dict[str, Any] = {
            "rule": self.rule,
            "rule_title": self.title,
            "score": self.score,
            "matched_terms": self.matched_terms,
            "type_hint": self.hint,
        }
        if self.breakdown:
            payload["match_breakdown"] = self.breakdown
        return payload


@dataclass
class QuestionMatch:
    """One scored rule for one question (kept for the AI payload)."""

    rule: int
    title: str
    score: int
    matched_terms: List[str]
    breakdown: Dict[str, Any]

    def as_dict(self) -> Dict[str, Any]:
        return {
            "rule": self.rule,
            "title": self.title,
            "score": self.score,
            "matched_terms": self.matched_terms[:8],
        }


def _phrase_hits(phrases: Iterable[str], haystack: str) -> List[str]:
    """Multi-word signals found in *haystack* (normalised word-boundary match)."""

    found: List[str] = []
    padded = f" {haystack} "
    for phrase in phrases:
        key = norm_key(phrase)
        if not key or " " not in key:
            continue
        if f" {key} " in padded:
            found.append(phrase)
    return found


def score_rule(
    rule: GrammarRule,
    *,
    question_terms: Set[str],
    question_text: str,
    qtype: Optional[str],
    stats: Optional[CorpusStats] = None,
) -> Tuple[int, List[str], Dict[str, Any]]:
    """Score one rule against one question; ``(score, terms, breakdown)``."""

    stats = stats or CorpusStats()
    title_hits = sorted(rule.title_terms & question_terms)
    topic_hits = sorted(rule.topic_terms & question_terms)
    signal_hits = sorted(rule.signal_words & question_terms)
    body_hits = sorted(rule.body_terms & question_terms)
    example_hits = sorted(rule.example_terms & question_terms)
    # the phrase table is a set, so sort the hits: the record must be identical
    # on a re-run (and the score is a sum, it does not depend on the order)
    phrase_hits = sorted(_phrase_hits(rule.signal_phrases, question_text))

    # a term that several of the rule's tables name is still one piece of
    # evidence: keep its strongest table, never the sum of them
    strong: Dict[str, int] = {}
    for table, weight in (
        (signal_hits, W_SIGNAL),
        (title_hits, W_TITLE),
        (topic_hits, W_TOPIC),
    ):
        for term in table:
            strong[term] = max(strong.get(term, 0), weight)
    strong_score = sum(weight * stats.term_rarity(term) for term, weight in strong.items())

    # the loose prose tables share one cap: many weak words are still weak
    loose = (set(body_hits) | set(example_hits)) - set(strong)
    loose_score = min(
        CAP_LOOSE,
        sum(W_BODY * stats.term_rarity(term) for term in loose),
    )

    phrase_score = sum(W_PHRASE * stats.phrase_rarity(phrase) for phrase in phrase_hits)
    score = strong_score + loose_score + phrase_score

    hint = False
    if qtype:
        if any(norm_key(name) == norm_key(rule.title) for name in TYPE_RULE_HINTS.get(qtype, ())):
            score += W_HINT_STRONG
            hint = True
        elif score > 0 and any(
            norm_key(name) in norm_key(rule.title) or norm_key(rule.title) in norm_key(name)
            for name in TYPE_RULE_RELATED.get(qtype, ())
        ):
            score += W_HINT_WEAK
            hint = True

    matched = sorted(set(phrase_hits) | set(strong))
    breakdown = {
        "phrases": phrase_hits[:6],
        "signals": signal_hits[:8],
        "title": title_hits[:6],
        "topic": topic_hits[:6],
        "body": body_hits[:6],
        "examples": example_hits[:6],
        "hint": hint,
        "strong_score": strong_score,
        "loose_score": loose_score,
    }
    return score, matched, breakdown


def match_rule(
    rules: Sequence[GrammarRule],
    *,
    concept: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    prompt: str = "",
    options: Sequence[str] = (),
    qtype: Optional[str] = None,
    question_text_value: Optional[str] = None,
    stats: Optional[CorpusStats] = None,
) -> RuleMatch:
    """Best rule for a question, or ``None`` when nothing clears :data:`MIN_SCORE`.

    ``question_text_value`` short-circuits the text assembly when the caller
    already built it (:func:`question_text`) — the two must stay identical, so
    callers inside the pipeline pass it and never re-derive it.
    """

    text = (
        question_text_value
        if question_text_value is not None
        else question_text(concept, tags, prompt, options)
    )
    terms = content_terms(text)
    scored: List[Tuple[Tuple[int, int, int], RuleMatch]] = []
    for rule in rules:
        score, matched, breakdown = score_rule(
            rule, question_terms=terms, question_text=text, qtype=qtype, stats=stats
        )
        if score < MIN_SCORE:
            continue
        # rank: score desc, then the *more specific* rule (fewer terms), then the
        # lower rule number — the historical tie-break
        scored.append(
            (
                (score, -rule.term_count, -rule.number),
                RuleMatch(
                    rule=rule.number,
                    title=rule.title,
                    score=score,
                    matched_terms=matched,
                    hint=bool(breakdown.get("hint")),
                    breakdown=breakdown,
                ),
            )
        )
    if not scored:
        return RuleMatch(rule=None, title="", score=0, matched_terms=[], hint=False)
    scored.sort(key=lambda item: item[0], reverse=True)
    best = scored[0][1]
    if len(scored) > 1 and best.score < scored[1][1].score * MATCH_MARGIN:
        # two rules read the question equally well: the keyword evidence did not
        # decide, so leave it to the AI pass (the contenders stay on the record
        # for the judge to see)
        return RuleMatch(
            rule=None,
            title="",
            score=0,
            matched_terms=[],
            hint=False,
            breakdown={
                "ambiguous": [
                    {
                        "rule": candidate.rule,
                        "title": candidate.title,
                        "score": candidate.score,
                        "matched_terms": candidate.matched_terms[:6],
                    }
                    for _rank, candidate in scored[:3]
                ]
            },
        )
    return best


def ranked_matches(
    rules: Sequence[GrammarRule],
    *,
    concept: Optional[str] = None,
    tags: Optional[Sequence[str]] = None,
    prompt: str = "",
    options: Sequence[str] = (),
    qtype: Optional[str] = None,
    question_text_value: Optional[str] = None,
    stats: Optional[CorpusStats] = None,
    top: int = 3,
) -> List[QuestionMatch]:
    """Top-*top* rules for a question — the shortlist handed to the AI judge."""

    text = (
        question_text_value
        if question_text_value is not None
        else question_text(concept, tags, prompt, options)
    )
    terms = content_terms(text)
    scored: List[Tuple[Tuple[int, int, int], QuestionMatch]] = []
    for rule in rules:
        score, matched, breakdown = score_rule(
            rule, question_terms=terms, question_text=text, qtype=qtype, stats=stats
        )
        if score <= 0:
            continue
        scored.append(
            (
                (score, -rule.term_count, -rule.number),
                QuestionMatch(
                    rule=rule.number,
                    title=rule.title,
                    score=score,
                    matched_terms=matched,
                    breakdown=breakdown,
                ),
            )
        )
    scored.sort(key=lambda item: item[0], reverse=True)
    return [item[1] for item in scored[:top]]


# ---------------------------------------------------------------------------
# the grammar question pool
# ---------------------------------------------------------------------------


@dataclass
class GrammarQuestion:
    """One grammar question with its deterministic verdict."""

    qid: str
    exam: str
    year: int
    paper_path: str
    ordinal: Any
    concept_raw: Any
    qtype: Optional[str]
    prompt: str
    options: List[str]
    #: the filing the question came from; the rule leaf repeats the subject and a
    #: concept on every pointer record (audit rule 4) — a question the source
    #: never classified keeps the rule it was filed under
    subject: Any = ""
    concept: Any = None
    #: prompt + options + label, cleaned once (the matcher and the IDF table
    #: must read exactly the same text)
    text: str = ""
    match: RuleMatch = field(default_factory=lambda: RuleMatch(None, "", 0, [], False))
    candidates: List[QuestionMatch] = field(default_factory=list)

    @property
    def assigned(self) -> bool:
        return self.match.rule is not None

    def link(self) -> Dict[str, Any]:
        return {
            "qid": self.qid,
            "subject": self.subject,
            "exam": self.exam,
            "year": self.year,
            "paper_path": self.paper_path,
            "ordinal": self.ordinal,
            "concept": self.concept,
            "concept_raw": self.concept_raw,
            "question_type": self.qtype,
        }


def question_text(
    concept: Optional[str], tags: Optional[Sequence[str]], prompt: str, options: Sequence[str]
) -> str:
    """The single text a question is matched on."""

    return clean_prompt(
        " ".join(
            [
                norm_space(concept),
                " ".join(norm_space(t) for t in (tags or [])),
                prompt,
                " ".join(options or ()),
            ]
        )
    )


def gather_grammar_questions(
    records: Sequence[Dict[str, Any]],
    questions_by_qid: Dict[str, Dict[str, Any]],
) -> Tuple[List[GrammarQuestion], Counter]:
    """Pass 1 — every grammar-shaped English question, before any scoring.

    No rule is needed here: this pass builds the corpus whose term frequencies
    decide which words are too common to carry evidence
(see :class:`CorpusStats`).
    """

    out: List[GrammarQuestion] = []
    type_counts: Counter = Counter()
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
        out.append(
            GrammarQuestion(
                qid=str(record.get("qid")),
                subject=str(record.get("subject") or ""),
                exam=str(record.get("exam") or ""),
                year=int(record.get("year") or 0),
                paper_path=str(record.get("paper_path") or ""),
                ordinal=record.get("ordinal"),
                concept=record.get("concept"),
                concept_raw=record.get("concept_raw"),
                qtype=qtype,
                prompt=prompt,
                options=options,
                text=question_text(question.get("concept"), question.get("tags"), prompt, options),
            )
        )
    out.sort(key=lambda item: (item.exam, item.year, str(item.ordinal), item.qid))
    return out, type_counts


def _stats_for(pool: Sequence[GrammarQuestion], rules: Sequence[GrammarRule]) -> CorpusStats:
    """The rarity table of *pool*, given the phrase inventory of *rules*."""

    return corpus_stats([item.text for item in pool], rules)


def score_grammar_questions(
    items: Sequence[GrammarQuestion],
    rules: Sequence[GrammarRule],
    *,
    stats: Optional[CorpusStats] = None,
) -> List[GrammarQuestion]:
    """Pass 2 — attach the deterministic verdict to every question in place."""

    stats = stats or _stats_for(items, rules)
    for item in items:
        item.match = match_rule(
            rules,
            question_text_value=item.text,
            qtype=item.qtype,
            stats=stats,
        )
        if item.match.rule is None:
            # the closest rules travel with the question so the AI judge sees
            # the keyword evidence that was not quite enough
            item.candidates = ranked_matches(
                rules, question_text_value=item.text, qtype=item.qtype, stats=stats, top=3
            )
    return list(items)


def collect_grammar_questions(
    records: Sequence[Dict[str, Any]],
    questions_by_qid: Dict[str, Dict[str, Any]],
    rules: Sequence[GrammarRule],
) -> Tuple[List[GrammarQuestion], Counter]:
    """Gather + score (kept as one call for callers that already have rules)."""

    items, type_counts = gather_grammar_questions(records, questions_by_qid)
    return score_grammar_questions(items, rules), type_counts


# ---------------------------------------------------------------------------
# AI pass
# ---------------------------------------------------------------------------

AI_TASK = "grammar_rule"
AI_BATCH_SIZE = 20

STATUS_OK = "ok"
STATUS_SKIPPED_NO_KEYS = "skipped_no_keys"
STATUS_AI_UNAVAILABLE = "ai_unavailable"
STATUS_TIME_LIMIT = "time_limit"

#: why a question never reached a rule (``unassigned.jsonl`` reason codes)
REASON_NO_MATCH = "no_deterministic_match"
REASON_AI_UNAVAILABLE = "ai_unavailable"
REASON_AI_NOT_ATTEMPTED = "ai_not_attempted"
REASON_AI_NO_RULE = "ai_no_rule"
REASON_AI_INVALID = "ai_invalid_rule"
REASON_AI_UNPARSED = "ai_unparsed_reply"


@dataclass
class AiPassResult:
    status: str
    counters: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    decisions: Dict[str, Dict[str, Any]] = field(default_factory=dict)
    examples: List[Dict[str, Any]] = field(default_factory=list)
    routes: Dict[str, Any] = field(default_factory=dict)


def ai_state_path() -> Path:
    return paths.STATE_DIR / "grammar_ai_state.json"


def load_ai_state(path: Optional[Path] = None) -> Dict[str, Any]:
    data = read_json(Path(path) if path is not None else ai_state_path(), default=None)
    if not isinstance(data, dict):
        return {"version": 1, "score_version": SCORE_VERSION, "created_at": now_iso(), "batches": {}, "questions": {}}
    data.setdefault("batches", {})
    data.setdefault("questions", {})
    if int(data.get("score_version") or 0) != SCORE_VERSION:
        # the matcher changed under the stored decisions: keep them (they are
        # still the AI's verdict) but re-open every batch so the new pending set
        # is scored again
        data["batches"] = {}
        data["stale_score_version"] = data.get("score_version")
    data["score_version"] = SCORE_VERSION
    return data


def save_ai_state(state: Dict[str, Any], path: Optional[Path] = None) -> Path:
    state["updated_at"] = now_iso()
    return write_json(Path(path) if path is not None else ai_state_path(), state)


def batch_fingerprint(items: Sequence[GrammarQuestion]) -> str:
    digest = hashlib.sha256()
    digest.update(f"v{SCORE_VERSION}".encode("utf-8"))
    for item in items:
        digest.update(item.qid.encode("utf-8"))
        digest.update(b"\n")
    return digest.hexdigest()[:32]


def _ai_payload(items: Sequence[GrammarQuestion]) -> Dict[str, Any]:
    questions: List[Dict[str, Any]] = []
    for item in items:
        entry: Dict[str, Any] = {
            "qid": item.qid,
            "question": clean_prompt(item.prompt)[:600],
        }
        if item.options:
            entry["options"] = [clean_prompt(option)[:160] for option in item.options]
        if item.concept_raw:
            entry["concept"] = str(item.concept_raw)
        if item.qtype:
            entry["question_type"] = item.qtype
        if item.candidates:
            entry["keyword_candidates"] = [candidate.as_dict() for candidate in item.candidates[:3]]
        questions.append(entry)
    return {"questions": questions}


def parse_ai_reply(final: Dict[str, Any], items: Sequence[GrammarQuestion], rules_count: int) -> Dict[str, Dict[str, Any]]:
    """``qid -> decision`` from a debate verdict (tolerant of shape drift)."""

    rows = final.get("items") if isinstance(final, dict) else None
    if not isinstance(rows, list) and isinstance(final, dict) and "rule" in final and len(items) == 1:
        rows = [dict(final, qid=items[0].qid)]
    out: Dict[str, Dict[str, Any]] = {}
    if not isinstance(rows, list):
        return out
    valid = {item.qid for item in items}
    for row in rows:
        if not isinstance(row, dict):
            continue
        qid = str(row.get("qid") or "")
        if qid not in valid:
            continue
        raw_rule = row.get("rule")
        rule: Optional[int]
        if raw_rule in (None, "", "null"):
            rule = None
        else:
            try:
                rule = int(raw_rule)
            except (TypeError, ValueError):
                rule = None
                out[qid] = {
                    "rule": None,
                    "confidence": 0.0,
                    "reason": str(row.get("reason") or "unparsable rule value")[:400],
                    "reason_code": REASON_AI_INVALID,
                }
                continue
            if not (1 <= rule <= rules_count):
                out[qid] = {
                    "rule": None,
                    "confidence": 0.0,
                    "reason": f"rule {rule} is outside 1..{rules_count}: {str(row.get('reason') or '')[:300]}",
                    "reason_code": REASON_AI_INVALID,
                }
                continue
        try:
            confidence = float(row.get("confidence") or 0.0)
        except (TypeError, ValueError):
            confidence = 0.0
        out[qid] = {
            "rule": rule,
            "confidence": max(0.0, min(1.0, confidence)),
            "reason": str(row.get("reason") or "")[:400],
            "reason_code": REASON_AI_NO_RULE if rule is None else "",
        }
    return out


def run_ai_pass(
    settings: Settings,
    rules: Sequence[GrammarRule],
    pending: Sequence[GrammarQuestion],
    *,
    batch_size: int = AI_BATCH_SIZE,
    limit: int = 0,
    log: Optional[Log] = None,
    window: Any = None,
    state_path: Optional[Path] = None,
    max_examples: int = 8,
) -> AiPassResult:
    """Ask the two-model debate to place every question the matcher could not.

    *deepseek-v4-flash* proposes one of the 129 rules per question, *gpt-5.6-sol*
    confirms or overrides it; the judge's answer is authoritative.  The pass is
    resumable (``state/grammar_ai_state.json``) and **never fails the run**: when
    no route can serve a role the status is ``ai_unavailable`` and the questions
    stay unassigned.
    """

    logger = log or Log("grammar-ai")
    target = Path(state_path) if state_path is not None else ai_state_path()
    router = router_mod.get_router(settings, log=logger)
    if not llm.keys_available(router.providers):
        logger.warn("no API keys in the environment – grammar AI pass skipped")
        return AiPassResult(
            STATUS_SKIPPED_NO_KEYS,
            notes=[
                "no API keys found ("
                + " / ".join(llm.keys_env_names(router.providers))
                + "); the deterministic result stands and the questions stay unassigned"
            ],
            routes=router.provider_report(),
        )

    state = load_ai_state(target)
    decisions: Dict[str, Dict[str, Any]] = dict(state.get("questions") or {})
    batches: Dict[str, Any] = dict(state.get("batches") or {})
    vocabulary = rule_vocabulary(rules)

    outstanding = [item for item in pending if item.qid not in decisions]
    if limit:
        outstanding = outstanding[:limit]
    groups = list(chunked(outstanding, batch_size))

    def _settled(entry: Any) -> bool:
        """A batch counts as done only when the judge actually answered it.

        Zero parsed verdicts means the critic had no healthy route
        (``served_by.judge`` shows ``… via -``).  Marking such a batch done would
        strand its questions forever, so it stays in the queue and is retried.
        Entries written before ``answered`` existed default to settled.
        """
        if not isinstance(entry, dict):
            return False
        try:
            return int(entry.get("answered", 1)) > 0
        except (TypeError, ValueError):
            return True

    todo = [group for group in groups if not _settled(batches.get(batch_fingerprint(group)))]

    logger.info(
        f"grammar AI pass: {len(pending)} pending, {len(pending) - len(outstanding)} already decided, "
        f"{len(todo)} batch(es) to send ({batch_size} questions each)"
    )
    if not todo:
        return AiPassResult(STATUS_OK, {"batches_total": 0, "decisions": len(decisions)}, decisions, [], router.provider_report())

    roles = ("proposer", "critic")
    health = router.roles_available(roles)
    if not all(health.values()):
        missing = [role for role in roles if not health[role]]
        reasons = "; ".join(
            f"{key}: {entry['why']}"
            for role in missing
            for key, entry in router.role_health(role).items()
            if not entry["healthy"]
        )
        logger.warn(f"grammar AI unavailable – no healthy route for the {', '.join(missing)} role(s): {reasons}")
        errors.record_unavailable(
            f"no healthy route for the {', '.join(missing)} role(s): {reasons}",
            phase="grammar",
            task=AI_TASK,
            item_ids=[item.qid for item in outstanding],
            scope=errors.SCOPE_PHASE,
            log=logger,
        )
        return AiPassResult(
            STATUS_AI_UNAVAILABLE,
            {"batches_total": len(todo), "batches_done": 0},
            [f"no healthy route for the {', '.join(missing)} role(s)", "questions left unassigned"],
            decisions,
            [],
            router.provider_report(),
        )

    session = debate_mod.Debate(settings, router, log=logger)
    counters: Counter = Counter()
    counters["batches_total"] = len(todo)
    examples: List[Dict[str, Any]] = []
    status = STATUS_OK
    notes: List[str] = []
    proposer_model = settings.debate.proposer_model
    critic_model = settings.debate.critic_model

    # ``PYQ_AI_WORKERS`` > 1 runs the batches concurrently.  Batches are independent
    # (each is keyed by its own fingerprint), so the only shared state is the state
    # file, which is written by this thread alone.
    max_workers = max(1, int(os.environ.get("PYQ_AI_WORKERS", "1") or 1))

    def _merge(group, fingerprint, outcome, done: int) -> None:
        """Fold one finished batch into the counters/decisions (main thread only)."""
        final = outcome.final if isinstance(outcome.final, dict) else {}
        parsed = parse_ai_reply(final, group, len(rules))
        provenance = outcome.provenance or {}
        proposer = (provenance.get("proposer") or {})
        final_call = (provenance.get("final") or provenance.get("critic") or {})
        served = {
            "proposer": f"{proposer.get('model') or proposer_model} via {proposer.get('provider') or '-'}",
            "judge": f"{final_call.get('model') or critic_model} via {final_call.get('provider') or '-'}",
            "same_model_fallback": bool(provenance.get("same_model_fallback")),
        }
        counters["batches_verified"] += 1
        counters["questions_sent"] += len(group)
        if served["same_model_fallback"]:
            counters["batches_same_model_fallback"] += 1
        batches[fingerprint] = {
            "at": now_iso(),
            "items": [item.qid for item in group],
            "verdict": outcome.verdict,
            "rounds": outcome.rounds,
            "served_by": served,
            "answered": len(parsed),
        }
        for item in group:
            decision = parsed.get(item.qid)
            if decision is None:
                counters["unparsed"] += 1
                continue
            if decision.get("rule") is None:
                counters["no_rule"] += 1
            else:
                counters["assigned"] += 1
            record = {
                **decision,
                "qid": item.qid,
                "batch": fingerprint,
                "at": now_iso(),
                "proposer_model": proposer.get("model") or proposer_model,
                "proposer_provider": proposer.get("provider") or "",
                "judge_model": final_call.get("model") or critic_model,
                "judge_provider": final_call.get("provider") or "",
                "same_model_fallback": served["same_model_fallback"],
                "verdict": outcome.verdict,
                "rounds": outcome.rounds,
            }
            decisions[item.qid] = record
            if len(examples) < max_examples:
                examples.append(record)
        logger.info(
            f"grammar AI pass: batch {done}/{len(todo)} — {served['proposer']} proposed, "
            f"{served['judge']} judged, {len(parsed)}/{len(group)} answered"
        )
        if not parsed:
            # every route answered, yet no verdict came back: a reply the parser
            # could not read (or a judge that never ran).  Without this row the
            # batch looks like a plain "left unassigned" with no reason.
            errors.record(
                kind=errors.KIND_BAD_JSON,
                phase="grammar",
                task=AI_TASK,
                batch_id=fingerprint,
                provider=str(final_call.get("provider") or ""),
                model=str(final_call.get("model") or critic_model),
                attempt=int(outcome.rounds or 0),
                message=(
                    f"no verdict parsed for {len(group)} question(s); "
                    f"verdict={outcome.verdict} served_by={served['judge']}"
                ),
                retryable=True,
                item_ids=[item.qid for item in group],
                scope=errors.SCOPE_BATCH,
                log=logger,
            )

    def _persist() -> None:
        state["questions"] = decisions
        state["batches"] = batches
        state["score_version"] = SCORE_VERSION
        save_ai_state(state, target)

    def _call(group):
        """Run one batch.

        With several workers each thread builds **its own** router so the breaker /
        cooldown bookkeeping of concurrent calls cannot race on shared state.
        """
        fingerprint = batch_fingerprint(group)
        if max_workers > 1:
            worker_router = router_mod.get_router(settings, log=logger)
            worker_session = debate_mod.Debate(settings, worker_router, log=logger)
        else:
            worker_session = session
        # bind the batch before the call: every row the router appends to the
        # error ledger then carries the phase, batch fingerprint and item ids
        with errors.ai_context(
            phase="grammar",
            task=AI_TASK,
            batch_id=fingerprint,
            item_ids=[item.qid for item in group],
        ):
            outcome = worker_session.run(
                item_id=fingerprint,
                task=AI_TASK,
                payload=_ai_payload(group),
                vocabulary=vocabulary,
                max_tokens=600 + 160 * len(group),
            )
        return fingerprint, outcome

    def _guards(group):
        """Shared stop conditions.  Returns True when the loop must break."""
        nonlocal status
        if window is not None and getattr(window, "expired", lambda: False)():
            notes.append(f"work window expired after {len(group)} question(s) stayed pending")
            logger.warn("grammar AI pass: work window expired – stopping (resumable)")
            status = STATUS_TIME_LIMIT
            return True
        if "*" in (router.halt_report() or {}):
            notes.append("router halted mid-run; questions left unassigned")
            status = STATUS_AI_UNAVAILABLE
            return True
        down = [role for role in roles if not router.role_available(role)]
        if down:
            notes.append(f"no healthy route for the {', '.join(down)} role(s) mid-run")
            logger.warn(f"grammar AI pass: {notes[-1]} – stopping")
            status = STATUS_AI_UNAVAILABLE
            return True
        return False

    if max_workers == 1:
        for index, group in enumerate(todo):
            if _guards(group):
                break
            try:
                fingerprint, outcome = _call(group)
            except router_mod.GlobalHalt as exc:
                status = STATUS_AI_UNAVAILABLE
                notes.append(f"router halted: {exc}")
                break
            except llm.LlmError as exc:
                counters["batches_failed"] += 1
                logger.warn(f"grammar AI pass: batch {index} failed: {exc}")
                continue
            _merge(group, fingerprint, outcome, index + 1)
            _persist()
    else:
        logger.info(
            f"grammar AI pass: {len(todo)} batch(es) over {max_workers} parallel worker(s)"
        )
        with ThreadPoolExecutor(max_workers=max_workers) as pool:
            futures = {pool.submit(_call, group): group for group in todo}
            done = 0
            for future in as_completed(futures):
                group = futures[future]
                done += 1
                if _guards(group):
                    for pending in futures:
                        pending.cancel()
                    break
                try:
                    fingerprint, outcome = future.result()
                except router_mod.GlobalHalt as exc:
                    status = STATUS_AI_UNAVAILABLE
                    notes.append(f"router halted: {exc}")
                    break
                except llm.LlmError as exc:
                    counters["batches_failed"] += 1
                    logger.warn(f"grammar AI pass: batch failed: {exc}")
                    continue
                except Exception as exc:  # one worker must never kill the whole pass
                    counters["batches_failed"] += 1
                    logger.warn(f"grammar AI pass: batch failed: {exc}")
                    continue
                _merge(group, fingerprint, outcome, done)
                _persist()

    state["questions"] = decisions
    state["batches"] = batches
    state["score_version"] = SCORE_VERSION
    save_ai_state(state, target)

    counters["decisions"] = len(decisions)
    if any(record.get("same_model_fallback") for record in decisions.values()):
        notes.append(
            "same_model_fallback: true – at least one batch had only one working model for both roles"
        )
    return AiPassResult(status, dict(counters), notes, decisions, examples, router.provider_report())


# ---------------------------------------------------------------------------
# rendering
# ---------------------------------------------------------------------------


def _md_table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out)


def _trend(current: int, previous: int) -> str:
    if current == previous:
        return "→ flat" if current else "→ 0"
    if previous == 0:
        return "▲ new"
    change = current - previous
    arrow = "▲" if change > 0 else "▼"
    return f"{arrow} {change:+d}"


@dataclass
class RuleLeaf:
    """Per-rule roll-up used for the leaves, the matrix and ``rules.json``."""

    rule: GrammarRule
    questions: List[GrammarQuestion] = field(default_factory=list)
    sources: Counter = field(default_factory=Counter)

    @property
    def count(self) -> int:
        return len(self.questions)

    def exams(self) -> Counter:
        return Counter(item.exam for item in self.questions)

    def years(self) -> Counter:
        return Counter(item.year for item in self.questions)

    def top_exam(self) -> str:
        exams = self.exams()
        return exams.most_common(1)[0][0] if exams else ""

    def trend(self, recent: Sequence[int]) -> Dict[str, Any]:
        years = self.years()
        values = [years.get(year, 0) for year in recent]
        return {
            "years": list(recent),
            "counts": values,
            "delta": (values[-1] - values[0]) if len(values) >= 2 else 0,
            "label": _trend(values[-1], values[0]) if len(values) >= 2 else "→",
        }


def recent_years(all_years: Iterable[int], span: int = 2) -> List[int]:
    years = sorted({int(year) for year in all_years if year})
    return years[-span:] if len(years) >= span else years


def render_rule_leaf(leaf: RuleLeaf, recent: Sequence[int], span: Sequence[int] = ()) -> str:
    """One browsable rule node: the rule text + its questions.

    *span* is the corpus year span (``(2019, 2025)``).  A rule the corpus never
    asks about gets an explicit ``No PYQ in scope`` marker instead of a silently
    empty ``questions.jsonl`` — ``tools/audit_db.py`` (rule 9) fails an empty leaf
    without that marker, so "no question exists" and "the questions were never
    written" can never be confused again.
    """

    rule = leaf.rule
    out: List[str] = []
    add = out.append
    add(f"# Rule {rule.number}: {rule.title}")
    add("")
    add(f"- **Topic**: {rule.topic or '-'}")
    add(f"- **Questions**: {human_int(leaf.count)}")
    add(f"- **Top exam**: {leaf.top_exam() or '-'}")
    if rule.text and rule.text.sources:
        add(f"- **Source**: {rule.text.sources}")
    add("- **Analysis view**: `database/english/_analysis/grammar/questions.jsonl` (field `rule`)")
    add("")
    if leaf.count == 0:
        years = f" {min(span)}–{max(span)}" if len(span or ()) >= 2 else ""
        add(f"**No PYQ in scope{years}** — the corpus contains no question for this rule.")
        add(
            "The empty `questions.jsonl` is intentional: this leaf is kept so the rule "
            "matrix stays complete, and the marker tells the audit it is not a missing write."
        )
        add("")
    if rule.text and rule.text.body:
        add("## Rule")
        add("")
        add(rule.text.body)
        add("")
    if rule.text and rule.text.examples:
        add("## Examples")
        add("")
        for example in rule.text.examples:
            add(f"- {example}")
        add("")
    if rule.signal_phrases or rule.signal_words:
        add("## Signal words")
        add("")
        if rule.signal_phrases:
            add("Phrases: " + ", ".join(f"`{value}`" for value in sorted(rule.signal_phrases)))
            add("")
        if rule.signal_words:
            add("Words: " + ", ".join(f"`{value}`" for value in sorted(rule.signal_words)))
            add("")
    exams = leaf.exams()
    add("## By exam")
    add("")
    if exams:
        add(_md_table(["Exam", "Questions"], [[name, human_int(count)] for name, count in exams.most_common()]))
    else:
        add("_No question is attached to this rule yet._")
    add("")
    years = leaf.years()
    add("## By year")
    add("")
    if years:
        add(_md_table(["Year", "Questions"], [[year, human_int(years[year])] for year in sorted(years)]))
    else:
        add("_No question is attached to this rule yet._")
    add("")
    if recent:
        trend = leaf.trend(recent)
        add("## Trend (last years in the corpus)")
        add("")
        add(
            _md_table(
                ["Year", "Questions"],
                [[year, human_int(value)] for year, value in zip(trend["years"], trend["counts"])],
            )
        )
        add("")
        add(f"Trend: **{trend['label']}**")
        add("")
    add("## Questions")
    add("")
    if not leaf.questions:
        add("_None._")
    else:
        rows = []
        for item in leaf.questions:
            rows.append(
                [
                    item.qid,
                    item.exam,
                    item.year,
                    f"`{item.paper_path}` #{item.ordinal}",
                    item.qtype or "-",
                    item.match.breakdown.get("source", "keyword"),
                ]
            )
        add(_md_table(["qid", "Exam", "Year", "Paper", "Type", "Via"], rows))
    add("")
    add("Resolve any id with `python tools/resolve.py <qid>`.")
    add("")
    return "\n".join(out)


def render_grammar_matrix(
    rules: Sequence[GrammarRule],
    leaves: Dict[int, RuleLeaf],
    *,
    recent: Sequence[int],
    total_questions: int,
    assigned: int,
    ai_assigned: int,
    unassigned: int,
) -> str:
    """The rule matrix appended to ``database/english/grammar/index.md``."""

    out: List[str] = []
    add = out.append
    add("## Grammar rules — question distribution")
    add("")
    add(
        f"All **{human_int(total_questions)}** grammar-shaped English questions are partitioned over the "
        f"**{len(rules)}** numbered rules of `chapter-and-topic/english-grammar-rules.md`: "
        f"**{human_int(assigned)}** assigned ({human_int(ai_assigned)} of them by the AI pass), "
        f"**{human_int(unassigned)}** in `_unassigned/`."
    )
    add("")
    trend_head = " / ".join(str(year) for year in recent) if recent else "-"
    add(
        _md_table(
            ["Rule", "Title", "Topic", "Questions", "Top exam", trend_head, "Trend"],
            [
                [
                    f"[{rule.number:03d}]({rule.slug}/index.md)",
                    rule.title,
                    rule.topic or "-",
                    human_int(leaves[rule.number].count) if rule.number in leaves else "0",
                    leaves[rule.number].top_exam() if rule.number in leaves and leaves[rule.number].count else "-",
                    " / ".join(
                        human_int(value)
                        for value in (leaves[rule.number].trend(recent)["counts"] if rule.number in leaves else [0] * len(recent))
                    ),
                    leaves[rule.number].trend(recent)["label"] if rule.number in leaves else "→ 0",
                ]
                for rule in rules
            ],
        )
    )
    add("")
    return "\n".join(out)


def render_rules_md(
    rules: Sequence[GrammarRule],
    leaves: Dict[int, RuleLeaf],
    type_counts: Counter,
    unassigned: int,
    *,
    recent: Sequence[int],
    ai_assigned: int = 0,
) -> str:
    lines: List[str] = []
    add = lines.append
    total = sum(leaf.count for leaf in leaves.values())
    add("# Grammar questions mapped to the 129 rules")
    add("")
    add(
        f"- Rules available: **{len(rules)}**\n"
        f"- Grammar questions matched: **{human_int(total)}** "
        f"({human_int(ai_assigned)} placed by the AI pass)\n"
        f"- Grammar questions unassigned: **{human_int(unassigned)}**"
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
            ["Rule", "Title", "Topic", "Questions", "Top exam", "Trend"],
            [
                [
                    rule.number,
                    rule.title,
                    rule.topic or "-",
                    human_int(leaves[rule.number].count) if rule.number in leaves else "0",
                    leaves[rule.number].top_exam() if rule.number in leaves else "-",
                    leaves[rule.number].trend(recent)["label"] if rule.number in leaves else "→ 0",
                ]
                for rule in sorted(
                    rules,
                    key=lambda rule: (-(leaves[rule.number].count if rule.number in leaves else 0), rule.number),
                )
            ],
        )
    )
    add("")
    add("## Rules with no matched questions")
    add("")
    empty = [rule for rule in rules if not (leaves.get(rule.number) and leaves[rule.number].count)]
    if empty:
        add(", ".join(f"{rule.number}. {rule.title}" for rule in empty))
    else:
        add("_None — every rule has at least one question._")
    add("")
    return "\n".join(lines)


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


def rule_leaf_dir(rule: GrammarRule, base: Optional[Path] = None) -> Path:
    root = Path(base) if base is not None else paths.GRAMMAR_RULES_DB_DIR
    return root / rule.slug


def _write_rule_leaf(
    leaf: RuleLeaf,
    *,
    base: Optional[Path],
    recent: Sequence[int],
    files: List[Path],
    span: Sequence[int] = (),
) -> None:
    directory = rule_leaf_dir(leaf.rule, base)
    paths.ensure_dir(directory)
    index = directory / "index.md"
    write_text(index, render_rule_leaf(leaf, recent, span))
    files.append(index)

    records = []
    for item in sorted(leaf.questions, key=lambda q: (q.exam, q.year, str(q.ordinal), q.qid)):
        record = {**item.link(), **item.match.as_dict()}
        record["assigned_by"] = item.match.breakdown.get("source", "keyword")
        # As far as ``tools/audit_db.py`` is concerned a rule leaf *is* a
        # question-tree leaf, so every pointer record names its subject and a
        # concept: a question the source never classified is filed under the rule
        # rather than claim the ``_unclassified`` placeholder level (rule 4).
        record["subject"] = record.get("subject") or "ENG"
        record["concept"] = record.get("concept") or leaf.rule.title
        records.append(record)
    questions = directory / "questions.jsonl"
    write_jsonl(questions, records)
    files.append(questions)

    marker = directory / "rule.json"
    trend = leaf.trend(recent)
    write_json(
        marker,
        {
            "version": 1,
            "rule": leaf.rule.number,
            "title": leaf.rule.title,
            "topic": leaf.rule.topic,
            "slug": leaf.rule.slug,
            "count": leaf.count,
            "questionIds": [item.qid for item in leaf.questions],
            "top_exam": leaf.top_exam(),
            "exams": dict(sorted(leaf.exams().items())),
            "years": {str(year): value for year, value in sorted(leaf.years().items())},
            "trend": trend,
            "signal_words": sorted(leaf.rule.signal_words),
            "signal_phrases": sorted(leaf.rule.signal_phrases),
        },
    )
    files.append(marker)


def _merge_matrix(index_md: Path, section: str) -> None:
    """Insert/replace the rule matrix section in the chapter ``index.md``.

    ``agent.build_db`` owns that page and rewrites it on every tree rebuild, so
    the matrix is fenced by markers and replaced in place — the page stays the
    tree's node page *and* the browsable entry point of the rule matrix.
    """

    start = "<!-- grammar-rules:start -->"
    end = "<!-- grammar-rules:end -->"
    body = index_md.read_text(encoding="utf-8") if index_md.is_file() else "# grammar\n"
    block = f"{start}\n{section}\n{end}\n"
    if start in body and end in body:
        head, _, rest = body.partition(start)
        _middle, _, tail = rest.partition(end)
        body = f"{head}{block}{tail.lstrip(chr(10))}"
    else:
        body = body.rstrip("\n") + "\n\n" + block
    write_text(index_md, body)


def build_grammar_db(
    records: Sequence[Dict[str, Any]],
    questions_by_qid: Dict[str, Dict[str, Any]],
    taxonomy: Dict[str, Any],
    *,
    out_dir: Optional[Path] = None,
    log: Optional[Log] = None,
    assignments: Optional[Dict[str, Dict[str, Any]]] = None,
    rule_texts: Optional[Sequence[RuleText]] = None,
    rules_dir: Optional[Path] = None,
    chapter_index: Optional[Path] = None,
    collected: Optional[Tuple[List[GrammarQuestion], Counter]] = None,
    ai_counters: Optional[Dict[str, Any]] = None,
    ai_status: str = "",
    database_dir: Optional[Path] = None,
) -> GrammarResult:
    """Partition every grammar question over the 129 rules and write the views.

    ``database_dir`` names the database root whose English concept tree is
    re-filed without the rule-owned questions (:func:`assigned_qids`); pass
    ``None`` to write the grammar views only and leave the question tree alone.
    """

    logger = log or Log("grammar")
    target = out_dir or paths.GRAMMAR_DB_DIR
    if collected is not None:
        pool, type_counts = collected
        rules = build_rule_index(taxonomy, rule_texts=rule_texts)
    else:
        raw, type_counts = gather_grammar_questions(records, questions_by_qid)
        rules = build_rule_index(taxonomy, rule_texts=rule_texts)
        pool = score_grammar_questions(raw, rules)
    if not rules:
        logger.warn("no grammar rules found in the taxonomy")
        return GrammarResult("ok", {"rules": 0}, [], ["taxonomy has no ENG rules"])

    decisions = assignments or {}

    leaves: Dict[int, RuleLeaf] = {rule.number: RuleLeaf(rule=rule) for rule in rules}
    unassigned: List[Dict[str, Any]] = []
    ai_assigned = 0
    ai_used = 0

    for item in pool:
        decision = decisions.get(item.qid) or {}
        if item.match.rule is not None:
            item.match.breakdown["source"] = "keyword"
        elif decision.get("rule") is not None:
            rule_number = int(decision["rule"])
            rule = leaves.get(rule_number)
            if rule is not None:
                item.match = RuleMatch(
                    rule=rule_number,
                    title=rule.rule.title,
                    score=int(decision.get("confidence", 0) * 100),
                    matched_terms=[],
                    hint=False,
                    breakdown={
                        "source": "ai",
                        "confidence": decision.get("confidence"),
                        "reason": decision.get("reason"),
                        "proposer_model": decision.get("proposer_model"),
                        "judge_model": decision.get("judge_model"),
                        "same_model_fallback": decision.get("same_model_fallback"),
                    },
                )
                ai_assigned += 1
                ai_used += 1
        if item.match.rule is not None and item.match.rule in leaves:
            leaves[item.match.rule].questions.append(item)
            leaves[item.match.rule].sources[item.match.breakdown.get("source", "keyword")] += 1
            continue
        reason = str(decision.get("reason_code") or "")
        if not reason:
            reason = REASON_NO_MATCH if not ai_status else (
                REASON_AI_UNAVAILABLE if ai_status == STATUS_AI_UNAVAILABLE else REASON_AI_NOT_ATTEMPTED
            )
        unassigned.append(
            {
                **item.link(),
                "rule": None,
                "rule_title": "",
                "score": 0,
                "matched_terms": [],
                "type_hint": False,
                "reason": reason,
                "detail": str(decision.get("reason") or "")[:400],
                "ai_rule": decision.get("rule"),
                "top_candidates": [candidate.as_dict() for candidate in item.candidates[:3]],
            }
        )

    recent = recent_years(item.year for item in pool)
    # the corpus year span, so an empty rule leaf can say "No PYQ in scope 2019–2025"
    pool_years = sorted({int(item.year) for item in pool if getattr(item, "year", None)})
    span = (pool_years[0], pool_years[-1]) if pool_years else ()
    for leaf in leaves.values():
        leaf.questions.sort(key=lambda q: (q.exam, q.year, str(q.ordinal), q.qid))

    paths.ensure_dir(target)
    files: List[Path] = []

    rules_json = target / "rules.json"
    write_json(
        rules_json,
        {
            "version": 2,
            "generated_by": "agent.grammar",
            "score_version": SCORE_VERSION,
            "min_score": MIN_SCORE,
            "rule_count": len(rules),
            "rules": [
                {
                    **rule.as_dict(),
                    "count": leaves[rule.number].count,
                    "questionIds": [item.qid for item in leaves[rule.number].questions],
                    "top_exam": leaves[rule.number].top_exam(),
                    "exams": dict(sorted(leaves[rule.number].exams().items())),
                    "years": {str(year): value for year, value in sorted(leaves[rule.number].years().items())},
                    "trend": leaves[rule.number].trend(recent),
                    "assigned_by": dict(sorted(leaves[rule.number].sources.items())),
                }
                for rule in rules
            ],
            "recent_years": list(recent),
            "totals": {
                "grammar_questions": len(pool),
                "assigned": len(pool) - len(unassigned),
                "assigned_by_ai": ai_assigned,
                "unassigned": len(unassigned),
                "rules_with_questions": sum(1 for leaf in leaves.values() if leaf.count),
                "rules_without_questions": sum(1 for leaf in leaves.values() if not leaf.count),
            },
        },
    )
    files.append(rules_json)

    questions_jsonl = target / "questions.jsonl"
    write_jsonl(
        questions_jsonl,
        [
            {**item.link(), **item.match.as_dict(), **_ai_fields(decisions.get(item.qid))}
            for item in sorted(pool, key=lambda q: (q.exam, q.year, str(q.ordinal), q.qid))
            if item.match.rule is not None
        ],
    )
    files.append(questions_jsonl)

    unassigned_jsonl = target / "unassigned.jsonl"
    write_jsonl(unassigned_jsonl, unassigned)
    files.append(unassigned_jsonl)

    md = target / "rules.md"
    write_text(
        md,
        render_rules_md(rules, leaves, type_counts, len(unassigned), recent=recent, ai_assigned=ai_assigned),
    )
    files.append(md)

    # --- the question tree must not also file what the rules own ---------
    # A rule-owned question lives in exactly one leaf: its rule leaf below.  The
    # English concept tree is therefore re-filed with those qids skipped — the set
    # is read back from the output above rather than recomputed — which also drops
    # a leaf whose questions are all owned by the grammar tree.  Unassigned grammar
    # questions keep the leaf they sit in, and a question whose rule no longer
    # holds (the AI verdict changed) returns to the tree.
    tree_excluded = 0
    if database_dir is not None:
        english = [record for record in records if record.get("subject") == "ENG"]
        if english:
            summary = build_db.write_question_tree(
                english,
                database_dir=Path(database_dir),
                log=logger,
                exclude_qids=assigned_qids(target),
            )
            tree_excluded = int(summary.get("excluded", 0))

    # --- per-rule browsable leaves -------------------------------------
    base = Path(rules_dir) if rules_dir is not None else paths.GRAMMAR_RULES_DB_DIR
    _prune_rule_leaves(base, {rule.slug for rule in rules})
    for rule in rules:
        _write_rule_leaf(leaves[rule.number], base=base, recent=recent, files=files, span=span)

    # --- the chapter page carries the matrix ---------------------------
    index_md = Path(chapter_index) if chapter_index is not None else paths.GRAMMAR_CHAPTER_INDEX
    if index_md.parent.is_dir():
        _merge_matrix(
            index_md,
            render_grammar_matrix(
                rules,
                leaves,
                recent=recent,
                total_questions=len(pool),
                assigned=len(pool) - len(unassigned),
                ai_assigned=ai_assigned,
                unassigned=len(unassigned),
            ),
        )
        files.append(index_md)

    reason_counts = Counter(record["reason"] for record in unassigned)
    counters = {
        "rules": len(rules),
        "grammar_questions": len(pool),
        "grammar_questions_matched": len(pool) - len(unassigned),
        "grammar_questions_assigned_by_ai": ai_assigned,
        "grammar_questions_unassigned": len(unassigned),
        "rules_with_questions": sum(1 for leaf in leaves.values() if leaf.count),
        "rules_without_questions": sum(1 for leaf in leaves.values() if not leaf.count),
        "coverage_pct": round(pct(len(pool) - len(unassigned), len(pool)), 2),
        "rule_leaves": len(rules),
        "tree_excluded": tree_excluded,
        **{f"reason:{key}": value for key, value in sorted(reason_counts.items())},
        **{f"type:{key}": value for key, value in sorted(type_counts.items())},
    }
    if ai_counters:
        counters.update({f"ai_{key}": value for key, value in ai_counters.items()})
    notes: List[str] = []
    if ai_status:
        notes.append(f"ai_status={ai_status}")
        counters["ai_status"] = ai_status
    logger.info(
        f"grammar: {counters['grammar_questions_matched']} assigned "
        f"({ai_assigned} by AI) / {len(unassigned)} unassigned across "
        f"{counters['rules_with_questions']}/{len(rules)} rules"
    )
    return GrammarResult("ok", counters, files, notes)


def _ai_fields(decision: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    if not decision:
        return {}
    return {
        "ai_confidence": decision.get("confidence"),
        "ai_reason": decision.get("reason"),
        "ai_proposer_model": decision.get("proposer_model"),
        "ai_judge_model": decision.get("judge_model"),
    }


def _prune_rule_leaves(base: Path, keep: Set[str]) -> None:
    """Remove rule leaves of a previous revision (a renamed/removed rule)."""

    if not base.is_dir():
        return
    import shutil

    for child in sorted(base.iterdir()):
        if not child.is_dir() or child.name in keep:
            continue
        if (child / "rule.json").is_file():
            shutil.rmtree(child, ignore_errors=True)


def assigned_qids(path: Optional[Path] = None) -> Set[str]:
    """The qids the grammar pass owns, read from its output.

    ``rules.json`` lists the per-rule ``questionIds``; the flat
    ``questions.jsonl`` is the fallback.  The question tree consumes this set so
    that a rule-owned question is not *also* filed under its concept leaf — the
    rule leaf is its one home (audit rule 5).  Reading the output keeps the two
    views in step: whatever the pass assigned is what the tree skips.
    """

    target = Path(path) if path is not None else paths.GRAMMAR_DB_DIR
    rules_json = target / "rules.json"
    if rules_json.is_file():
        data = read_json(rules_json, default={})
        owned = {
            str(qid)
            for rule in data.get("rules") or ()
            for qid in (rule or {}).get("questionIds") or ()
            if qid
        }
        if owned:
            return owned
    questions = target / "questions.jsonl"
    if not questions.is_file():
        return set()
    out: Set[str] = set()
    with questions.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except ValueError:
                continue
            if record.get("rule") is not None and record.get("qid"):
                out.add(str(record["qid"]))
    return out


# ---------------------------------------------------------------------------
# orchestrator
# ---------------------------------------------------------------------------


@dataclass
class AiOptions:
    """How the AI pass should behave for one build."""

    enabled: bool = False
    limit: int = 0
    batch_size: int = AI_BATCH_SIZE
    state_path: Optional[Path] = None
    #: run the pass but keep the stored verdicts (nothing new is applied)
    report_only: bool = False


def build_grammar_view(
    records: Sequence[Dict[str, Any]],
    questions_by_qid: Dict[str, Dict[str, Any]],
    taxonomy: Dict[str, Any],
    *,
    settings: Optional[Settings] = None,
    window: Any = None,
    ai: Optional[AiOptions] = None,
    log: Optional[Log] = None,
    out_dir: Optional[Path] = None,
    rules_dir: Optional[Path] = None,
    chapter_index: Optional[Path] = None,
    rule_texts: Optional[Sequence[RuleText]] = None,
    database_dir: Optional[Path] = None,
) -> GrammarResult:
    """Deterministic pass -> AI pass -> write every grammar view.

    The deterministic result is always written; the AI pass only fills the
    questions it left over.  A soft stop (no keys, no healthy route) leaves them
    in ``unassigned.jsonl`` with the matching reason code.  ``database_dir`` is
    passed on to :func:`build_grammar_db`: the English concept tree is re-filed so
    that a rule-owned question is only filed under its rule leaf.
    """

    logger = log or Log("grammar")
    # pass 1: which questions are grammar-shaped (no rules needed yet)
    raw, type_counts = gather_grammar_questions(records, questions_by_qid)
    rules = build_rule_index(taxonomy, rule_texts=rule_texts)
    if not rules:
        logger.warn("no grammar rules found in the taxonomy")
        return GrammarResult("ok", {"rules": 0}, [], ["taxonomy has no ENG rules"])
    # the corpus itself decides which words are too common to prove anything,
    # so pass 2 must run through the rarity table built from pass 1
    stats = _stats_for(raw, rules)
    pool = score_grammar_questions(raw, rules, stats=stats)
    pending = [item for item in pool if not item.assigned]
    logger.info(
        f"grammar: {len(pool)} grammar-shaped questions, {len(pool) - len(pending)} placed by the "
        f"keyword matcher, {len(pending)} pending"
    )

    stored = load_ai_state(ai.state_path if ai else None)
    decisions: Dict[str, Dict[str, Any]] = dict(stored.get("questions") or {})
    ai_status = ""
    ai_counters: Dict[str, Any] = {}
    notes: List[str] = []

    if ai and ai.enabled:
        if settings is None:
            notes.append("AI pass requested without settings – skipped")
            ai_status = STATUS_SKIPPED_NO_KEYS
        else:
            outcome = run_ai_pass(
                settings,
                rules,
                pending,
                batch_size=ai.batch_size,
                limit=ai.limit,
                log=logger,
                window=window,
                state_path=ai.state_path,
            )
            ai_status = outcome.status
            ai_counters = outcome.counters
            notes.extend(outcome.notes)
            if ai.report_only:
                logger.info("grammar AI pass ran in report-only mode – stored verdicts kept")
            else:
                decisions = outcome.decisions
    else:
        ai_status = STATUS_SKIPPED_NO_KEYS if not decisions else ""

    result = build_grammar_db(
        records,
        questions_by_qid,
        taxonomy,
        out_dir=out_dir,
        log=logger,
        assignments=decisions,
        rule_texts=rule_texts,
        rules_dir=rules_dir,
        chapter_index=chapter_index,
        collected=(pool, type_counts),
        ai_counters=ai_counters,
        ai_status=ai_status,
        database_dir=database_dir,
    )
    result.notes.extend(notes)
    return result


# ---------------------------------------------------------------------------
# helpers kept for the phase / tools
# ---------------------------------------------------------------------------


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


def coverage_line(result: GrammarResult) -> str:
    counters = result.counters
    return (
        f"{human_int(counters.get('grammar_questions_matched', 0))} of "
        f"{human_int(counters.get('grammar_questions', 0))} grammar questions assigned "
        f"({counters.get('coverage_pct', 0)}%), "
        f"{human_int(counters.get('grammar_questions_unassigned', 0))} unassigned"
    )
