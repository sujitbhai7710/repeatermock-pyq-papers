"""Phase 0 – the full distribution.

Produces ``state/distribution.json`` and ``database/_meta/distribution.md``:
counts over exam × year × shift × subject × chapter × topic × concept, the
last-2-year delta, an importance score and priority lists.

Importance score (documented in ``docs/DATA_MODEL.md``)::

    recency  = sum over questions of W(year)      # 1.00 / 0.75 / 0.50 / 0.30
    delta_2y = count(2024,2025) - count(2022,2023)
    trend    = clamp(delta_2y / max(1, count(2022,2023)), -1, 1)

    score = 100 * (0.50 * recency/max_recency
                 + 0.35 * frequency/max_frequency
                 + 0.15 * (trend + 1)/2)

Weights are fixed constants so a re-run on the same data produces byte-identical
output.
"""

from __future__ import annotations

from collections import Counter, defaultdict
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

from . import paths
from .util import Log, human_int, pct, write_json, write_text

RECENCY_WEIGHTS = {0: 1.00, 1: 0.75, 2: 0.50}
RECENCY_DEFAULT = 0.30
LAST_2Y_SPAN = 2

WEIGHT_RECENCY = 0.50
WEIGHT_FREQUENCY = 0.35
WEIGHT_TREND = 0.15


def recency_weight(year: int, latest: int) -> float:
    gap = latest - year
    if gap < 0:
        return RECENCY_WEIGHTS[0]
    return RECENCY_WEIGHTS.get(gap, RECENCY_DEFAULT)


@dataclass
class Counter5:
    """Nested counters collapsed into flat keys joined by ``|``."""

    data: Counter

    @classmethod
    def new(cls) -> "Counter5":
        return cls(Counter())

    def add(self, key: Tuple[Any, ...], amount: int = 1) -> None:
        self.data[key] += amount


def _top(counter: Counter, limit: int) -> List[Dict[str, Any]]:
    return [
        {"key": k, "count": v}
        for k, v in sorted(counter.items(), key=lambda kv: (-kv[1], str(kv[0])))
    ][:limit]


def build_distribution(
    records: Sequence[Dict[str, Any]],
    settings: Any,
    paper_reports: Sequence[Dict[str, Any]],
    coverage: Dict[str, Any],
    *,
    log: Optional[Log] = None,
) -> Dict[str, Any]:
    logger = log or Log("distribution")

    latest_year = settings.year_range.max
    years = list(range(settings.year_range.min, settings.year_range.max + 1))
    last2 = {latest_year, latest_year - 1}
    prev2 = {latest_year - 2, latest_year - 3}

    def blank() -> Dict[str, Any]:
        return {
            "questions": 0,
            "recency": 0.0,
            "y2022_2023": 0,
            "y2024_2025": 0,
        }

    by_exam: Dict[str, Counter] = defaultdict(Counter)
    by_year: Counter = Counter()
    by_exam_year: Counter = Counter()
    by_exam_shift: Counter = Counter()
    by_subject: Counter = Counter()
    by_exam_subject: Counter = Counter()
    by_chapter: Counter = Counter()
    by_topic: Counter = Counter()
    by_concept: Counter = Counter()

    # node statistics keyed by (subject, chapter) and (subject, chapter, topic) and concept
    node: Dict[Tuple[str, ...], Dict[str, Any]] = defaultdict(blank)

    for record in records:
        subject = record.get("subject") or "UNKNOWN"
        chapter = record.get("chapter") or "_unclassified"
        topic = record.get("topic") or ""
        concept = record.get("concept") or "_unclassified"
        exam = record.get("exam") or "UNKNOWN"
        year = int(record.get("year") or 0)
        shift = record.get("shift") or 0
        weight = recency_weight(year, latest_year)

        by_exam[exam]["questions"] += 1
        by_exam[exam][f"year:{year}"] += 1
        by_year[year] += 1
        by_exam_year[(exam, year)] += 1
        by_exam_shift[(exam, year, shift)] += 1
        by_subject[subject] += 1
        by_exam_subject[(exam, subject)] += 1
        by_chapter[(subject, chapter)] += 1
        by_topic[(subject, chapter, topic)] += 1
        by_concept[(subject, chapter, topic, concept)] += 1

        for key in (
            ("subject", subject),
            ("chapter", subject, chapter),
            ("topic", subject, chapter, topic),
            ("concept", subject, chapter, topic, concept),
        ):
            entry = node[key]
            entry["questions"] += 1
            entry["recency"] += weight
            if year in last2:
                entry["y2024_2025"] += 1
            if year in prev2:
                entry["y2022_2023"] += 1

    max_scores = {"recency": 0.0, "frequency": 0}
    for entry in node.values():
        max_scores["recency"] = max(max_scores["recency"], entry["recency"])
        max_scores["frequency"] = max(max_scores["frequency"], entry["questions"])

    def finalise(key: Tuple[str, ...], entry: Dict[str, Any]) -> Dict[str, Any]:
        prev = entry["y2022_2023"]
        delta = entry["y2024_2025"] - prev
        trend = delta / max(1, prev)
        trend = max(-1.0, min(1.0, trend))
        recency_norm = entry["recency"] / max_scores["recency"] if max_scores["recency"] else 0.0
        freq_norm = entry["questions"] / max_scores["frequency"] if max_scores["frequency"] else 0.0
        score = 100.0 * (
            WEIGHT_RECENCY * recency_norm
            + WEIGHT_FREQUENCY * freq_norm
            + WEIGHT_TREND * ((trend + 1) / 2)
        )
        return {
            "kind": key[0],
            "path": list(key[1:]),
            "subject": key[1],
            "chapter": key[2] if len(key) > 2 else None,
            "topic": key[3] if len(key) > 3 else None,
            "concept": key[4] if len(key) > 4 else None,
            "questions": entry["questions"],
            "recency": round(entry["recency"], 4),
            "last_2y": entry["y2024_2025"],
            "prev_2y": prev,
            "delta_2y": delta,
            "trend": round(trend, 4),
            "importance": round(score, 4),
        }

    nodes: List[Dict[str, Any]] = [finalise(k, v) for k, v in node.items()]
    nodes.sort(key=lambda n: (-n["importance"], -n["questions"], n["path"]))

    def priority(kind: str, subject: Optional[str] = None, limit: int = 30) -> List[Dict[str, Any]]:
        rows = [
            n
            for n in nodes
            if n["kind"] == kind and (subject is None or n["subject"] == subject)
        ]
        rows.sort(key=lambda n: (-n["importance"], -n["questions"], n["path"]))
        return rows[:limit]

    subjects = sorted(by_subject)
    distribution: Dict[str, Any] = {
        "version": 1,
        "year_range": [settings.year_range.min, settings.year_range.max],
        "latest_year": latest_year,
        "last_2y_years": sorted(last2, reverse=True),
        "weights": {
            "recency": WEIGHT_RECENCY,
            "frequency": WEIGHT_FREQUENCY,
            "trend": WEIGHT_TREND,
            "recency_by_year": {str(k): v for k, v in RECENCY_WEIGHTS.items()},
            "recency_default": RECENCY_DEFAULT,
        },
        "totals": {
            "questions_indexed": len(records),
            "by_subject": dict(sorted(by_subject.items())),
            "by_year": {str(k): v for k, v in sorted(by_year.items())},
        },
        "coverage": coverage,
        "by_exam": {exam: dict(sorted(by_exam[exam].items())) for exam in sorted(by_exam)},
        "by_exam_year": {f"{e}|{y}": c for (e, y), c in sorted(by_exam_year.items())},
        "by_exam_shift": {f"{e}|{y}|{s}": c for (e, y, s), c in sorted(by_exam_shift.items())},
        "by_exam_subject": {f"{e}|{s}": c for (e, s), c in sorted(by_exam_subject.items())},
        "top_chapters": _top(by_chapter, 100),
        "top_topics": _top(by_topic, 100),
        "top_concepts": _top(by_concept, 200),
        "priority": {
            "overall": priority("chapter", None, 40),
            "concepts": priority("concept", None, 60),
            "topics": priority("topic", None, 60),
            "by_subject": {s: priority("chapter", s, 20) for s in subjects},
        },
        "papers": {
            "total": len(paper_reports),
            "validated": coverage.get("papers_validated", 0),
            "flagged": coverage.get("papers_flagged", 0),
            "placeable": coverage.get("papers_placeable", 0),
        },
    }
    logger.info(
        "distribution: {n} questions over {s} subjects, {c} chapters".format(
            n=len(records), s=len(by_subject), c=len({k[1] for k in by_chapter})
        )
    )
    return distribution


# ---------------------------------------------------------------------------
# markdown report
# ---------------------------------------------------------------------------


def _table(headers: Sequence[str], rows: Iterable[Sequence[Any]]) -> str:
    out = ["| " + " | ".join(headers) + " |", "|" + "|".join(["---"] * len(headers)) + "|"]
    for row in rows:
        out.append("| " + " | ".join(str(c) for c in row) + " |")
    return "\n".join(out)


def render_distribution_md(distribution: Dict[str, Any]) -> str:
    totals = distribution["totals"]
    coverage = distribution["coverage"]
    lines: List[str] = []
    add = lines.append

    add("# PYQ Distribution (Phase 0)")
    add("")
    add(
        "Generated by `python -m agent.cli phase0`. Every number below is derived "
        "from the sharded question index (`state/index/<subject>.<n>.jsonl`, "
        "in-scope, non-Hindi questions only)."
    )
    add("")
    add("## Corpus coverage")
    add("")
    add(f"- Papers in scope: **{human_int(coverage['papers'])}**")
    add(f"- Questions in scope: **{human_int(coverage['questions'])}**")
    add(f"- Papers signature-validated (section-level agreement >= 95%): **{human_int(coverage['papers_validated'])}**")
    add(f"- Papers flagged NEEDS_AI_REVIEW: **{human_int(coverage['papers_flagged'])}**")
    add("")
    add("```text")
    add(
        "placed + skipped_hindi + unclassified + flagged_papers_questions "
        f"= {coverage['placed']} + {coverage['skipped_hindi']} + {coverage['unclassified']}"
        f" + {coverage['flagged_papers_questions']} = {coverage['accounted']}"
    )
    add(f"in-scope questions                                        = {coverage['questions']}")
    add(f"balanced: {coverage['balanced']}")
    add("```")
    add("")

    add("## Questions by exam")
    add("")
    rows = []
    for exam, data in distribution["by_exam"].items():
        questions = data.get("questions", 0)
        rows.append(
            [
                exam,
                human_int(questions),
                pct(questions, totals["questions_indexed"]).__round__(2),
            ]
        )
    add(_table(["Exam", "Questions", "% of index"], rows))
    add("")

    add("## Questions by subject")
    add("")
    add(
        _table(
            ["Subject", "Questions", "%"],
            [
                [s, human_int(n), round(pct(n, totals["questions_indexed"]), 2)]
                for s, n in totals["by_subject"].items()
            ],
        )
    )
    add("")

    add("## Questions by year")
    add("")
    add(_table(["Year", "Questions"], [[y, human_int(n)] for y, n in totals["by_year"].items()]))
    add("")

    add("## Exam x subject matrix")
    add("")
    matrix: Dict[str, Dict[str, int]] = defaultdict(dict)
    subject_set: set = set()
    for key, count in distribution["by_exam_subject"].items():
        exam, subject = key.split("|", 1)
        matrix[exam][subject] = count
        subject_set.add(subject)
    subjects_sorted = sorted(subject_set)
    add(
        _table(
            ["Exam"] + subjects_sorted + ["Total"],
            [
                [exam]
                + [human_int(matrix[exam].get(s, 0)) for s in subjects_sorted]
                + [human_int(sum(matrix[exam].values()))]
                for exam in sorted(matrix)
            ],
        )
    )
    add("")

    add("## Top chapters by importance")
    add("")
    add(
        _table(
            ["#", "Subject", "Chapter", "Questions", "Last 2y", "Prev 2y", "Delta", "Importance"],
            [
                [
                    i + 1,
                    n["subject"],
                    n["chapter"],
                    human_int(n["questions"]),
                    n["last_2y"],
                    n["prev_2y"],
                    f"{n['delta_2y']:+d}",
                    n["importance"],
                ]
                for i, n in enumerate(distribution["priority"]["overall"])
            ],
        )
    )
    add("")

    add("## Top concepts by importance")
    add("")
    add(
        _table(
            ["#", "Subject", "Concept", "Questions", "Last 2y", "Prev 2y", "Delta", "Importance"],
            [
                [
                    i + 1,
                    n["subject"],
                    n["concept"],
                    human_int(n["questions"]),
                    n["last_2y"],
                    n["prev_2y"],
                    f"{n['delta_2y']:+d}",
                    n["importance"],
                ]
                for i, n in enumerate(distribution["priority"]["concepts"][:60])
            ],
        )
    )
    add("")

    add("## Priority chapters per subject")
    for subject, rows_data in distribution["priority"]["by_subject"].items():
        if not rows_data:
            continue
        add("")
        add(f"### {subject}")
        add("")
        add(
            _table(
                ["#", "Chapter", "Questions", "Last 2y", "Delta", "Importance"],
                [
                    [
                        i + 1,
                        n["chapter"],
                        human_int(n["questions"]),
                        n["last_2y"],
                        f"{n['delta_2y']:+d}",
                        n["importance"],
                    ]
                    for i, n in enumerate(rows_data)
                ],
            )
        )
    add("")
    add("---")
    add("")
    add(
        "Importance = `100 * (0.50*recency/max_recency + 0.35*frequency/max_frequency "
        "+ 0.15*(trend+1)/2)`; `trend = clamp((count(2024,2025) - count(2022,2023)) "
        "/ max(1,count(2022,2023)), -1, 1)`."
    )
    add("")
    return "\n".join(lines)


def write_distribution_md(distribution: Dict[str, Any], path: Optional[Path] = None) -> Path:
    target = path or (paths.META_DB_DIR / "distribution.md")
    return write_text(target, render_distribution_md(distribution))


def write_distribution_json(distribution: Dict[str, Any], path: Optional[Path] = None) -> Path:
    return write_json(path or paths.DISTRIBUTION_JSON, distribution)
