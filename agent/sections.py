"""Subject assignment: positional map, keyword map, signature validation, Hindi detection.

Two independent signals are computed for every paper:

* **positional guess** – the canonical layout from ``config/exams.json`` applied
  to the question's *global ordinal* (index in ``questions[]`` + 1).  The source
  ``n`` field restarts at 1 for every section, so it cannot be used directly as
  a position; ordinals are derived from the array order, which is the order the
  sections appear in the paper.
* **keyword guess** – :mod:`agent.keywords` applied to ``concept``/``tags``.

The two are compared per question.  ``agreement = hits / comparable`` where
*comparable* counts questions that produced a usable keyword guess (ambiguous
and empty labels are excluded rather than counted as disagreement).  A paper
below ``signature.min_agreement`` (default 0.95) is flagged
``NEEDS_AI_REVIEW`` and its layout is re-derived from the paper's own section
structure (``n`` resets + per-section keyword majority).  Papers where the
re-derived layout is also not confident are not placed at all — their questions
are reported as ``flagged_papers_questions`` instead of being silently
mis-assigned.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import keywords
from .config import Exam, ExamTable, PaperKind, SignaturePolicy, LayoutSpan
from .util import norm_space

# ---------------------------------------------------------------------------
# Hindi detection
# ---------------------------------------------------------------------------

_DANDA = "\u0964\u0965"


@dataclass(frozen=True)
class HindiPolicyLocal:
    pattern: "re.Pattern[str]"
    include_options: bool
    include_solution: bool

    def text_of(self, question: Dict[str, Any]) -> str:
        parts: List[str] = []
        parts.append(str(question.get("question") or ""))
        if self.include_options:
            for opt in _iter_options(question):
                parts.append(str(opt.get("text") or ""))
        if self.include_solution:
            parts.append(str(question.get("solution") or ""))
        return " ".join(parts)

    def is_hindi(self, question: Dict[str, Any]) -> bool:
        return bool(self.pattern.search(self.text_of(question)))


def iter_options(question: Dict[str, Any]) -> List[Dict[str, Any]]:
    """Normalise ``options`` (dict-of-dicts or list) to a list of dicts."""

    return _iter_options(question)


def _iter_options(question: Dict[str, Any]) -> List[Dict[str, Any]]:
    raw = question.get("options")
    out: List[Dict[str, Any]] = []
    if isinstance(raw, dict):
        items = raw.items()
        for key, value in items:
            if isinstance(value, dict):
                label = str(value.get("label", key))
                out.append({"label": label, "text": value.get("text", "")})
            else:
                out.append({"label": str(key), "text": value})
    elif isinstance(raw, list):
        for value in raw:
            if isinstance(value, dict):
                out.append({"label": str(value.get("label", "")), "text": value.get("text", "")})
            else:
                out.append({"label": "", "text": value})
    return out


def make_hindi_policy(cfg: Any) -> HindiPolicyLocal:
    """Build the Hindi detector from settings.

    ``cfg`` is typically :class:`agent.config.HindiPolicy`.
    """

    pattern_src = getattr(cfg, "regex", r"[\u0900-\u097F]")
    pattern = re.compile(pattern_src)
    if getattr(cfg, "exclude_punctuation", False):
        pattern = re.compile(r"[\u0900-\u0963\u0966-\u097F]")
    scope = set(getattr(cfg, "scope", ("question", "options")))
    return HindiPolicyLocal(
        pattern=pattern,
        include_options="options" in scope,
        include_solution=bool(getattr(cfg, "include_solution", False)) or "solution" in scope,
    )


def is_devanagari_prefix(text: str) -> bool:
    from .keywords import HINDI_PATTERN  # noqa: F401  (documentation aid)

    return bool(re.search(r"[\u0900-\u097F]", text or ""))


# ---------------------------------------------------------------------------
# positional layout
# ---------------------------------------------------------------------------


def ordinal_of(index: int) -> int:
    """Global 1-based ordinal of a question inside its paper."""

    return index + 1


def subject_at(spans: Sequence[LayoutSpan], ordinal: int) -> Optional[str]:
    for span in spans:
        if span.contains(ordinal):
            return span.subject
    return None


def spans_to_map(spans: Sequence[LayoutSpan], length: int) -> List[Optional[str]]:
    return [subject_at(spans, ordinal_of(i)) for i in range(length)]


# ---------------------------------------------------------------------------
# section structure (n resets)
# ---------------------------------------------------------------------------


def split_sections(questions: Sequence[Dict[str, Any]]) -> List[List[int]]:
    """Split question *indices* into sections wherever ``n`` does not increase.

    This is structural and content independent: the source papers restart ``n``
    at 1 for each section.
    """

    sections: List[List[int]] = []
    last_n: Optional[int] = None
    for index, question in enumerate(questions):
        n = question.get("n")
        try:
            n_val = int(n)
        except (TypeError, ValueError):
            n_val = None
        if not sections or n_val is None or last_n is None or n_val <= last_n:
            sections.append([])
        sections[-1].append(index)
        if n_val is not None:
            last_n = n_val
    return [s for s in sections if s]


def section_is_clean(questions: Sequence[Dict[str, Any]], section: Sequence[int]) -> bool:
    ns = [questions[i].get("n") for i in section]
    try:
        return [int(x) for x in ns] == list(range(1, len(section) + 1))
    except (TypeError, ValueError):
        return False


# ---------------------------------------------------------------------------
# keyword guess
# ---------------------------------------------------------------------------


def keyword_subject(question: Dict[str, Any]) -> Optional[str]:
    """Subject implied by the question's ``concept`` then ``tags``."""

    concept = question.get("concept")
    subject = keywords.subject_for_text(concept)
    if subject is not None:
        return subject
    tags = question.get("tags")
    if tags:
        return keywords.subject_for_text(tags)
    return None


# ---------------------------------------------------------------------------
# signature validation
# ---------------------------------------------------------------------------


@dataclass
class SignatureResult:
    agreement: float
    comparable: int
    hits: int
    total: int
    positional_known: int
    keyword_known: int
    ok: bool
    needs_ai_review: bool
    mismatches: List[Dict[str, Any]] = field(default_factory=list)
    # section-level evidence (the primary check, see ``validate_sections``)
    section_agreement: Optional[float] = None
    section_comparable: int = 0
    section_hits: int = 0
    section_total: int = 0
    section_mismatches: List[Dict[str, Any]] = field(default_factory=list)
    question_ok: bool = False
    basis: str = "question"  # "section" | "question"

    @property
    def evidence_free(self) -> bool:
        """No usable subject signal at all (every label is ``Unidentified``)."""

        return self.section_comparable == 0 and self.comparable == 0

    def as_dict(self) -> Dict[str, Any]:
        return {
            "ok": self.ok,
            "basis": self.basis,
            "agreement": round(self.agreement, 6),
            "comparable": self.comparable,
            "hits": self.hits,
            "total": self.total,
            "positional_known": self.positional_known,
            "keyword_known": self.keyword_known,
            "question_ok": self.question_ok,
            "section_agreement": None
            if self.section_agreement is None
            else round(self.section_agreement, 6),
            "section_comparable": self.section_comparable,
            "section_hits": self.section_hits,
            "section_total": self.section_total,
            "needs_ai_review": self.needs_ai_review,
        }


def validate(
    positional: Sequence[Optional[str]],
    question_indices: Sequence[int],
    questions: Sequence[Dict[str, Any]],
    min_agreement: float,
    *,
    max_mismatch_samples: int = 3,
) -> SignatureResult:
    """Compare the positional guess with the keyword guess.

    ``question_indices`` lists the questions that participate (i.e. everything
    except skipped Hindi questions).
    """

    comparable = 0
    hits = 0
    positional_known = 0
    keyword_known = 0
    mismatches: List[Dict[str, Any]] = []
    for index in question_indices:
        pos = positional[index] if index < len(positional) else None
        if pos is not None:
            positional_known += 1
        kw = keyword_subject(questions[index])
        if kw is None:
            continue
        keyword_known += 1
        if pos is None:
            continue
        comparable += 1
        if pos == kw:
            hits += 1
        elif len(mismatches) < max_mismatch_samples:
            mismatches.append(
                {
                    "ordinal": ordinal_of(index),
                    "positional": pos,
                    "keyword": kw,
                    "concept": norm_space(questions[index].get("concept")),
                }
            )

    agreement = (hits / comparable) if comparable else 0.0
    ok = comparable > 0 and agreement >= min_agreement
    return SignatureResult(
        agreement=agreement,
        comparable=comparable,
        hits=hits,
        total=len(question_indices),
        positional_known=positional_known,
        keyword_known=keyword_known,
        ok=ok,
        question_ok=ok,
        needs_ai_review=not ok,
        mismatches=mismatches,
    )


def validate_sections(
    questions: Sequence[Dict[str, Any]],
    spans: Optional[Sequence[LayoutSpan]],
    question_indices: Sequence[int],
    min_agreement: float,
    *,
    max_mismatch_samples: int = 3,
) -> Tuple[Optional[float], int, int, int, List[Dict[str, Any]]]:
    """Section-level agreement between a declared layout and the labels.

    For every *positional section* of the declared layout the modal keyword
    subject of its participating questions is compared with the subject the
    layout declares.  This is the primary evidence: a section is the unit the
    layout table actually describes, so one or two noisy source labels inside a
    section cannot flip the verdict (the per-question metric is kept as the
    secondary check, see :func:`validate`).

    Returns ``(agreement, comparable_sections, hits, total_sections,
    mismatches)``.  ``agreement`` is ``None`` when the paper has no declared
    layout or no section produced any usable vote.
    """

    if not spans:
        return None, 0, 0, 0, []

    participating = set(question_indices)
    hits = 0
    comparable = 0
    mismatches: List[Dict[str, Any]] = []
    for span in spans:
        votes: Dict[str, int] = {}
        for index in participating:
            if not span.contains(ordinal_of(index)):
                continue
            subject = keyword_subject(questions[index])
            if subject is not None:
                votes[subject] = votes.get(subject, 0) + 1
        modal = _majority(votes)
        if modal is None:
            continue
        comparable += 1
        if modal == span.subject:
            hits += 1
        elif len(mismatches) < max_mismatch_samples:
            mismatches.append(
                {
                    "span": [span.subject, span.start, span.end],
                    "modal": modal,
                    "votes": dict(sorted(votes.items())),
                }
            )

    agreement = (hits / comparable) if comparable else None
    return agreement, comparable, hits, len(spans), mismatches


# ---------------------------------------------------------------------------
# layout detection (fallback + per-paper detection)
# ---------------------------------------------------------------------------


@dataclass
class DetectedLayout:
    spans: List[LayoutSpan]
    section_subjects: List[Optional[str]]
    section_votes: List[Dict[str, int]]
    confidence: float  # share of participating questions that produced a vote
    ok: bool
    note: str = ""

    def as_dict(self, length: int) -> Dict[str, Any]:
        return {
            "spans": [[s.subject, s.start, s.end] for s in self.spans],
            "section_subjects": list(self.section_subjects),
            "section_votes": list(self.section_votes),
            "confidence": round(self.confidence, 6),
            "ok": self.ok,
            "note": self.note,
        }


def _majority(votes: Dict[str, int]) -> Optional[str]:
    if not votes:
        return None
    best = sorted(votes.items(), key=lambda kv: (-kv[1], kv[0]))
    if len(best) > 1 and best[0][1] == best[1][1]:
        # deterministic tie break: preferred order REAS, GK, MATH, ENG, COMPUTER, HINDI
        order = {s: i for i, s in enumerate(keywords.ALL_SUBJECTS)}
        best.sort(key=lambda kv: (-kv[1], order.get(kv[0], 99)))
    return best[0][0]


#: a section label counts as *decisive* when it has at least this many votes and
#: the winner holds at least this share of them.
DECISIVE_MIN_VOTES = 3
DECISIVE_MIN_SHARE = 0.60


def _decisive(votes: Dict[str, int]) -> bool:
    if not votes:
        return True  # nothing to contradict
    total = sum(votes.values())
    if total < DECISIVE_MIN_VOTES:
        return False
    top = max(votes.values())
    return (top / total) >= DECISIVE_MIN_SHARE


def detect_layout(
    questions: Sequence[Dict[str, Any]],
    *,
    exclude: Iterable[int] = (),
    min_confidence: float = 0.60,
) -> DetectedLayout:
    """Derive a layout from the paper's own section structure and content.

    Sections come from ``n`` resets.  Each section is labelled by the keyword
    majority of its participating questions.  Adjacent sections that receive the
    same label are merged (the corpus contains a few papers whose sections are
    split by stray ``n`` values).  Sections without any vote inherit the nearest
    labelled neighbour.

    The result is accepted when

    * every section that produced votes has a *decisive* majority (>= 3 votes and
      a >= 60% winner), **and**
    * either the vote coverage reaches ``min_confidence``, or the paper has a
      single section (a one-section paper — CGL Tier-II AAO/Statistics — has no
      competing structure, so a clear majority on the few labelled questions is
      the best evidence available).
    """

    excluded = set(exclude)
    sections = split_sections(questions)
    section_subjects: List[Optional[str]] = []
    section_votes: List[Dict[str, int]] = []
    participating = 0
    voted = 0

    for section in sections:
        votes: Dict[str, int] = {}
        for index in section:
            if index in excluded:
                continue
            participating += 1
            subject = keyword_subject(questions[index])
            if subject is None:
                continue
            voted += 1
            votes[subject] = votes.get(subject, 0) + 1
        section_votes.append(dict(sorted(votes.items())))
        section_subjects.append(_majority(votes))

    # forward/backward fill for sections without votes
    filled = list(section_subjects)
    for i, subject in enumerate(filled):
        if subject is not None:
            continue
        prev_subject = next((s for s in reversed(filled[:i]) if s is not None), None)
        next_subject = next((s for s in filled[i + 1 :] if s is not None), None)
        filled[i] = prev_subject or next_subject

    # merge adjacent sections with identical labels
    spans: List[LayoutSpan] = []
    cursor = 0
    for section, subject in zip(sections, filled):
        start = cursor + 1
        end = cursor + len(section)
        cursor = end
        if subject is None:
            continue
        if spans and spans[-1].subject == subject:
            spans[-1] = LayoutSpan(subject, spans[-1].start, end)
        else:
            spans.append(LayoutSpan(subject, start, end))

    confidence = (voted / participating) if participating else 0.0
    sections_with_votes = [v for v in section_votes if v]
    decisive = all(_decisive(votes) for votes in sections_with_votes)
    single_section = len(sections) == 1
    enough_coverage = confidence >= min_confidence or (single_section and voted >= DECISIVE_MIN_VOTES)
    ok = bool(spans) and decisive and enough_coverage and cursor == len(questions)
    if ok:
        note = "single-section paper accepted on a decisive majority" if (
            single_section and confidence < min_confidence
        ) else ""
    elif not decisive:
        note = "detected layout rejected: no decisive keyword majority in every section"
    else:
        note = "detected layout below confidence threshold"
    return DetectedLayout(
        spans=spans,
        section_subjects=filled,
        section_votes=section_votes,
        confidence=confidence,
        ok=ok,
        note=note,
    )


# ---------------------------------------------------------------------------
# per-paper analysis
# ---------------------------------------------------------------------------


@dataclass
class PaperAnalysis:
    exam: str
    kind_id: str
    kind_label: str
    length: int
    hindi_count: int
    participating: List[int]
    canonical_spans: Optional[List[LayoutSpan]]
    positional: List[Optional[str]]
    signature: SignatureResult
    detected: Optional[DetectedLayout]
    layout_source: str  # "canonical" | "detected" | "none"
    effective_spans: List[LayoutSpan]
    subjects: List[Optional[str]]
    section_sizes: List[int]
    flagged: bool
    placeable: bool
    layout_agreement: Optional[float] = None

    def as_dict(self) -> Dict[str, Any]:
        return {
            "exam": self.exam,
            "paper_kind": self.kind_id,
            "paper_kind_label": self.kind_label,
            "length": self.length,
            "hindi_count": self.hindi_count,
            "section_sizes": list(self.section_sizes),
            "layout_source": self.layout_source,
            "layout_agreement": None
            if self.layout_agreement is None
            else round(self.layout_agreement, 6),
            "canonical_spans": None
            if self.canonical_spans is None
            else [[s.subject, s.start, s.end] for s in self.canonical_spans],
            "effective_spans": [[s.subject, s.start, s.end] for s in self.effective_spans],
            "detected": None if self.detected is None else self.detected.as_dict(self.length),
            "signature": self.signature.as_dict(),
            "flagged": self.flagged,
            "placeable": self.placeable,
        }


def _expected_span_length(spans: Sequence[LayoutSpan]) -> Optional[int]:
    return max((s.end for s in spans), default=None)


def analyze_paper(
    questions: Sequence[Dict[str, Any]],
    exam: Exam,
    kind: Optional[PaperKind],
    signature_cfg: SignaturePolicy,
    hindi: HindiPolicyLocal,
    year: Optional[int] = None,
) -> PaperAnalysis:
    """Run Hindi detection, signature validation and layout selection."""

    length = len(questions)
    hindi_flags = [hindi.is_hindi(q) for q in questions]
    hindi_count = sum(1 for flag in hindi_flags if flag)
    participating = [i for i, flag in enumerate(hindi_flags) if not flag]

    canonical_spans: Optional[List[LayoutSpan]] = None
    if kind is not None:
        spans = kind.spans_for(length, year)
        if spans:
            canonical_spans = list(spans)

    positional = (
        spans_to_map(canonical_spans, length) if canonical_spans else [None] * length
    )

    signature = validate(
        positional,
        participating,
        questions,
        signature_cfg.min_agreement,
    )

    # Primary check: section-level agreement (modal subject per declared
    # section).  The per-question metric above stays as the secondary signal;
    # individual source labels are noisy, whole sections are not.
    (
        section_agreement,
        section_comparable,
        section_hits,
        section_total,
        section_mismatches,
    ) = validate_sections(
        questions,
        canonical_spans,
        participating,
        signature_cfg.min_section_agreement,
    )
    if section_agreement is not None:
        section_ok = section_agreement >= signature_cfg.min_section_agreement
        signature.section_agreement = section_agreement
        signature.section_comparable = section_comparable
        signature.section_hits = section_hits
        signature.section_total = section_total
        signature.section_mismatches = section_mismatches
        signature.ok = section_ok
        signature.basis = "section"
    else:
        signature.section_comparable = 0
        signature.section_total = section_total
        signature.section_mismatches = section_mismatches
        signature.ok = signature.question_ok
        signature.basis = "question"
    signature.needs_ai_review = not signature.ok

    detected: Optional[DetectedLayout] = None
    needs_detection = canonical_spans is None or not signature.ok or (
        kind is not None and kind.detect
    )
    if needs_detection:
        detected = detect_layout(questions, exclude=(i for i, f in enumerate(hindi_flags) if f))
        if not detected.ok and canonical_spans is not None and signature.ok:
            detected = None

    # No usable signal anywhere means there is no evidence of mis-assignment,
    # only an absence of evidence: keep the canonical layout, still flagged.
    unvalidated = signature.evidence_free

    layout_source = "none"
    effective_spans: List[LayoutSpan] = []
    if canonical_spans is not None and (signature.ok or unvalidated):
        layout_source = "canonical" if signature.ok else "canonical-unverified"
        effective_spans = canonical_spans
    elif (
        detected is not None
        and detected.ok
        and signature_cfg.allow_detected_layout_fallback
    ):
        layout_source = "detected"
        effective_spans = detected.spans
    elif canonical_spans is not None and kind is not None and not kind.detect:
        # No alternative available: keep the canonical guess but mark it flagged.
        layout_source = "canonical"
        effective_spans = canonical_spans

    subjects = (
        spans_to_map(effective_spans, length) if effective_spans else [None] * length
    )

    # Section-level evidence: does the layout derived from the paper's own
    # structure land on the same subject sequence as the declared layout?
    layout_agreement: Optional[float] = None
    if detected is not None and canonical_spans is not None:
        detected_map = spans_to_map(detected.spans, length)
        same = sum(
            1
            for i in participating
            if detected_map[i] is not None and detected_map[i] == positional[i]
        )
        layout_agreement = (same / len(participating)) if participating else None

    flagged = signature.needs_ai_review
    placeable = bool(effective_spans) and all(
        subjects[i] is not None for i in participating
    )

    return PaperAnalysis(
        exam=exam.code,
        kind_id=kind.id if kind else "unknown",
        kind_label=kind.label if kind else "unknown",
        length=length,
        hindi_count=hindi_count,
        participating=participating,
        canonical_spans=canonical_spans,
        positional=positional,
        signature=signature,
        detected=detected,
        layout_source=layout_source,
        effective_spans=effective_spans,
        subjects=subjects,
        section_sizes=[len(s) for s in split_sections(questions)],
        flagged=flagged,
        placeable=placeable,
        layout_agreement=layout_agreement,
    )


def exam_kind_for_path(exam_table: ExamTable, path: str) -> Tuple[Optional[Exam], Optional[PaperKind]]:
    return exam_table.kind_for_path(path)
