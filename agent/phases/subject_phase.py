"""Shared subject-phase implementation.

``phase1`` (ENG), ``phase2`` (GK), ``phase3`` (MATH), ``phase4`` (REAS) and
``phase5`` (COMPUTER) all follow the same four steps:

1. **python extract** – take the subject's records from the phase-0 index and
   refine chapter/topic with the alias map + fallback ladder (already applied in
   phase 0; re-checked here so a phase can be re-run after the taxonomy changes);
2. **AI verify** – optional two-model confirmation of each batch
   (:mod:`agent.verify`), skipped with a note when no keys are configured;
3. **apply corrections** – corrections are written back to the index;
4. **write DB** – the subject's slice of ``database/`` plus, for English, the
   vocabulary and grammar analyses.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, List, Sequence

from .. import build_db, grammar, indexer, paths, tracking, vocab
from ..checkpoint import STATUS_RATE_LIMITED, STATUS_TIME_LIMIT
from ..util import Log, human_int, write_json
from . import PhaseResult

SUBJECT_LABELS = {
    "ENG": "English Language / Verbal Ability",
    "GK": "General Awareness / General Studies",
    "MATH": "Quantitative Aptitude / Mathematical Abilities",
    "REAS": "Reasoning / General Intelligence",
    "COMPUTER": "Computer Knowledge",
}

def run_subject_phase(ctx, phase: str, subject: str) -> PhaseResult:
    log: Log = ctx.log
    result = PhaseResult(phase=phase)
    records = ctx.records or indexer.read_index()
    if not records:
        result.status = "error"
        result.notes.append("index is empty – run `python -m agent.cli phase0` first")
        return result

    subset = [r for r in records if r.get("subject") == subject]
    log.step(f"{phase}: {human_int(len(subset))} {subject} questions")

    # -- 1. python extract -------------------------------------------------
    unclassified = sum(1 for r in subset if r.get("status") == "unclassified")
    with_image = sum(1 for r in subset if r.get("has_image"))
    chapters = len({r.get("chapter") for r in subset if r.get("chapter")})
    log.info(
        f"{phase}: extract -> {human_int(len(subset))} questions, {chapters} chapters, "
        f"{human_int(unclassified)} unclassified, {human_int(with_image)} with images"
    )

    # -- 2. AI verify ------------------------------------------------------
    corrections_applied = 0
    verification_status = "skipped"
    if ctx.expired():
        result.status = STATUS_TIME_LIMIT
        result.notes.append("work window expired before AI verification")
    elif getattr(ctx, "no_ai", False):
        result.notes.append("AI verification skipped (--no-ai)")
    else:
        from .. import verify as verify_mod

        verification = verify_mod.verify_database(
            ctx.settings, phase=phase, log=log
        )
        verification_status = verification.status
        corrections_applied = int(verification.counters.get("index_updated", 0))
        result.notes.extend(verification.notes)
        if verification.status == STATUS_RATE_LIMITED:
            result.status = STATUS_RATE_LIMITED
            log.warn(f"{phase}: halted (rate limited) – checkpointing")
        if corrections_applied:
            records = indexer.read_index()
            subset = [r for r in records if r.get("subject") == subject]

    result.counters.update(
        {
            f"{subject.lower()}_questions": len(subset),
            f"{subject.lower()}_chapters": chapters,
            f"{subject.lower()}_unclassified": unclassified,
            f"{subject.lower()}_with_image": with_image,
            "verification": verification_status,
            "corrections_applied": corrections_applied,
        }
    )

    # -- 3. apply corrections (already merged into the index) --------------

    # -- 4. write DB -------------------------------------------------------
    tree = build_db.write_question_tree(subset, log=log)
    result.files.extend(_tree_files(subset))
    result.counters["db_files"] = tree.get("files", 0)

    if subject == "ENG":
        result.files.extend(_write_english_analysis(ctx, records, log))
    if subject in ("GK", "MATH", "REAS", "COMPUTER"):
        result.files.extend(_write_subject_analysis(phase, subject, subset, log))

    tracking.record_files(phase, result.files)
    tracking.journal(f"{phase}.complete", result.counters)
    if result.status == "ok":
        log.info(f"{phase}: database slice written ({len(result.files)} files)")
    return result

def _tree_files(records: Sequence[Dict[str, Any]]) -> List[Path]:
    """Existing files under the subjects touched by *records*."""

    subjects = sorted({build_db.subject_dir(r.get("subject")) for r in records})
    out: List[Path] = []
    for subject_dir in subjects:
        base = paths.DATABASE_DIR / subject_dir
        if base.is_dir():
            out.extend(sorted(p for p in base.rglob("*") if p.is_file()))
    return out

def _write_english_analysis(ctx, records: Sequence[Dict[str, Any]], log: Log) -> List[Path]:
    questions = _questions_for(records)

    vocab_result = vocab.build_vocabulary_db(records, questions, log=log)
    grammar_result = grammar.build_grammar_db(
        records, questions, ctx.taxonomy, log=log
    )

    solved = paths.DATABASE_DIR / "english" / "solved-items.json"
    write_json(
        solved,
        {
            "version": 1,
            "generated_by": "agent.phases.phase1",
            "vocabulary": vocab_result.as_dict(),
            "grammar": grammar_result.as_dict(),
        },
    )
    log.info(
        "phase1: vocabulary {v} items, grammar {g} mapped / {u} unmapped".format(
            v=vocab_result.counters.get("vocabulary_items", 0),
            g=grammar_result.counters.get("grammar_questions_matched", 0),
            u=grammar_result.counters.get("grammar_questions_unmapped", 0),
        )
    )
    return list(vocab_result.files) + list(grammar_result.files) + [solved]

def _write_subject_analysis(
    phase: str, subject: str, records: Sequence[Dict[str, Any]], log: Log
) -> List[Path]:
    """Per-subject chapter roll-up written next to the question tree."""

    from collections import Counter

    chapters: Counter = Counter()
    topics: Counter = Counter()
    concepts: Counter = Counter()
    for record in records:
        chapters[record.get("chapter") or "_unclassified"] += 1
        if record.get("topic"):
            topics[record["topic"]] += 1
        if record.get("concept"):
            concepts[record["concept"]] += 1

    target = paths.DATABASE_DIR / build_db.subject_dir(subject) / "analysis.json"
    write_json(
        target,
        {
            "version": 1,
            "generated_by": f"agent.phases.{phase}",
            "subject": subject,
            "label": SUBJECT_LABELS.get(subject, subject),
            "questions": len(records),
            "chapters": dict(sorted(chapters.items())),
            "topics": dict(topics.most_common(200)),
            "concepts": dict(concepts.most_common(400)),
        },
    )
    log.info(f"{subject}: analysis.json written ({len(chapters)} chapters)")
    return [target]

def _questions_for(records: Sequence[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    """Resolve raw questions for the given index records from the source papers."""

    wanted_paths = {str(r.get("paper_path")) for r in records}
    needed_qids = {str(r.get("qid")) for r in records}
    out: Dict[str, Dict[str, Any]] = {}
    if not wanted_paths:
        return out

    from ..config import exams as _exams

    table = _exams()
    from ..discover import load_paper

    for rel in sorted(wanted_paths):
        path = paths.PROJECT_ROOT / rel
        if not path.is_file():
            continue
        paper = load_paper(path, table)
        if paper is None:
            continue
        for question in paper.questions:
            qid = str(question.get("qid", ""))
            if qid in needed_qids:
                out[qid] = question
    return out
