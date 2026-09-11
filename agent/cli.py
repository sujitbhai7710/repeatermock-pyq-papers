"""Command line interface: ``python -m agent.cli <command>``.

Commands
--------
``taxonomy``    parse the four taxonomy markdown files -> ``state/taxonomy.json``
                + ``state/alias_map.json``
``phase0``      discovery, Hindi split, signature validation, index, distribution
``phase N``     run one pipeline phase (0-5)
``run``         full pipeline honouring checkpoint/resume and the work window
``verify-db``   AI re-check of every python-extracted item (resumable)
``audit``       deterministic structural self-check of ``database/``
``stats``       per-exam/per-subject counts and the top concepts
``mocks``       regenerate the mock-pack catalogue
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import checkpoint, config, paths, taxonomy, tracking
from .classify import Classifier, collect_raw_labels
from .config import ExamTable, Settings
from .util import Log, human_int, pct, read_json

EXIT_OK = 0
EXIT_USAGE = 2
EXIT_RATE_LIMITED = 3
EXIT_TIME_LIMIT = 4
EXIT_ERROR = 1

# ---------------------------------------------------------------------------
# shared setup
# ---------------------------------------------------------------------------

def load_classifier(log: Optional[Log] = None) -> Classifier:
    logger = log or Log("cli")
    tax = read_json(paths.TAXONOMY_JSON, default=None)
    alias = read_json(paths.ALIAS_MAP_JSON, default=None)
    if tax is None or alias is None:
        logger.warn("taxonomy/alias map missing – run `python -m agent.cli taxonomy` first")
        tax = tax or {"subjects": {}}
        alias = alias or {"map": {}}
    return Classifier(tax, alias)

def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="python -m agent.cli",
        description="SSC previous-year-question database agent.",
    )
    parser.add_argument("--quiet", action="store_true", help="only log errors")
    sub = parser.add_subparsers(dest="command")

    p_tax = sub.add_parser("taxonomy", help="parse chapter-and-topic/*.md")
    p_tax.add_argument(
        "--from-cache",
        action="store_true",
        help="reuse state/labels.json instead of re-reading every paper",
    )

    sub.add_parser("phase0", help="distribution, index and signature validation")

    p_phase = sub.add_parser("phase", help="run a single phase")
    p_phase.add_argument("number", type=int, choices=[0, 1, 2, 3, 4, 5])

    p_run = sub.add_parser("run", help="run the full pipeline")
    p_run.add_argument("--phase", type=int, default=None, choices=[0, 1, 2, 3, 4, 5])
    p_run.add_argument("--fresh", action="store_true", help="ignore the checkpoint")
    p_run.add_argument("--no-ai", action="store_true", help="skip every LLM step")

    p_verify = sub.add_parser("verify-db", help="AI re-check of extracted items")
    p_verify.add_argument("--phase", default=None, help="limit to one phase (phase1..phase5)")
    p_verify.add_argument("--limit", type=int, default=0, help="max items this run (0 = all)")
    p_verify.add_argument("--batch-size", type=int, default=None)
    p_verify.add_argument("--report-only", action="store_true", help="apply nothing")

    p_stats = sub.add_parser("stats", help="counts and top concepts")
    p_stats.add_argument("--top", type=int, default=20)
    p_stats.add_argument("--json", action="store_true")

    p_audit = sub.add_parser(
        "audit", help="deterministic structural self-check of database/"
    )
    p_audit.add_argument("--json", action="store_true", help="machine-readable report")
    p_audit.add_argument(
        "--database", default=None, help="audit another database directory"
    )

    p_mocks = sub.add_parser("mocks", help="regenerate the mock catalogue")
    p_mocks.add_argument("--min-size", type=int, default=None)
    p_mocks.add_argument("--max-packs", type=int, default=0)
    p_mocks.add_argument(
        "--pack-size",
        type=int,
        default=None,
        help="questions per pack (default 100; 0 = every question)",
    )
    return parser

# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_taxonomy(args: argparse.Namespace, settings: Settings, exams: ExamTable, log: Log) -> int:
    from . import discover

    paths.ensure_dir(paths.STATE_DIR)
    tax = taxonomy.build_taxonomy()
    taxonomy.write_taxonomy(tax)
    log.info(
        "taxonomy: "
        + ", ".join(
            f"{subject}: {body['chapters']} chapters / {body['topics']} topics / {body['concepts']} concepts"
            for subject, body in tax["counts"].items()
        )
    )

    cache = paths.STATE_DIR / "labels.json"
    if args.from_cache and cache.is_file():
        labels = read_json(cache, default=[])
        log.info(f"labels: reusing {len(labels)} cached raw concepts/tags")
    else:
        papers, _ = discover.discover(settings, exams)
        labels = collect_raw_labels(papers)
        from .util import write_json

        write_json(cache, labels)
        log.info(f"labels: collected {len(labels)} raw concepts/tags from {len(papers)} papers")

    alias = taxonomy.build_alias_map(tax, labels)
    taxonomy.write_alias_map(alias)
    stats = alias["stats"]
    log.info(
        "alias map: {total} keys (curated={curated} taxonomy={taxonomy} observed={observed} "
        "unmapped={unmapped})".format(total=len(alias["map"]), **stats)
    )

    print(f"taxonomy.json  : {paths.rel(paths.TAXONOMY_JSON)}")
    for subject, body in sorted(tax["counts"].items()):
        print(
            f"  {subject:6s} chapters={body['chapters']:4d} topics={body['topics']:5d} "
            f"concepts={body['concepts']:6d}"
        )
    print(f"alias_map.json : {paths.rel(paths.ALIAS_MAP_JSON)} ({len(alias['map'])} keys)")
    print(
        "  sources      : "
        + "curated={curated} taxonomy={taxonomy} observed={observed} unmapped={unmapped}".format(**stats)
    )
    tracking.journal("taxonomy.complete", tax["counts"])
    tracking.record_files("taxonomy", [paths.TAXONOMY_JSON, paths.ALIAS_MAP_JSON, cache])
    return EXIT_OK

def _make_context(settings: Settings, exams: ExamTable, log: Log, classifier: Classifier, run_id: str, window=None):
    from .phases import PipelineContext

    return PipelineContext(
        settings=settings,
        exams=exams,
        log=log,
        window=window or checkpoint.make_window(settings),
        run_id=run_id,
        taxonomy=read_json(paths.TAXONOMY_JSON, default={"subjects": {}}),
        alias_map=read_json(paths.ALIAS_MAP_JSON, default={"map": {}}),
        classifier=classifier,
        llm_available=bool(os_environ_keys()),
    )

def os_environ_keys() -> List[str]:
    import os

    found: List[str] = []
    for name in ("OPENAI_KEYS", "DEEPSEEK_KEYS", "AR_PROXY_TOKEN", "JW_PROXY_TOKEN"):
        if os.environ.get(name, "").strip():
            found.append(name)
    return found

def cmd_phase(
    number: int,
    settings: Settings,
    exams: ExamTable,
    log: Log,
    run_id: str,
    window=None,
) -> int:
    from .phases import phase_module

    name = f"phase{number}"
    classifier = load_classifier(log)
    ctx = _make_context(settings, exams, log, classifier, run_id, window=window)
    module = phase_module(name)
    result = module.run(ctx)
    tracking.save_progress(
        tracking.record_phase(name, result.status, result.counters, notes=result.notes)
    )
    tracking.write_progress_md(extra=_coverage_extra(ctx))
    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    return exit_code_for_status(result.status)

def exit_code_for_status(status: str) -> int:
    """Phase/verify status -> process exit code.

    ``rate_limited`` (3) and ``time_limit`` (4) are **soft stops**: the phase
    checkpointed and published, so the shell must not treat them as failures.
    """

    if status in ("ok", "done", "complete"):
        return EXIT_OK
    if status == checkpoint.STATUS_RATE_LIMITED:
        return EXIT_RATE_LIMITED
    if status == checkpoint.STATUS_TIME_LIMIT:
        return EXIT_TIME_LIMIT
    return EXIT_ERROR

def _coverage_extra(ctx) -> Optional[Dict[str, Any]]:
    coverage = getattr(ctx, "coverage", None)
    if not coverage:
        # phases 1-5 do not re-run discovery; read the phase-0 counters instead
        # so PROGRESS.md always shows the coverage identity.
        phases = tracking.load_progress().get("phases", {})
        counters = (phases.get("phase0") or {}).get("counters") or {}
        if not counters:
            return None
        return {
            "papers": counters.get("papers_in_scope"),
            "questions": counters.get("questions_in_scope"),
            "placed": counters.get("placed"),
            "unclassified": counters.get("unclassified"),
            "skipped_hindi": counters.get("skipped_hindi"),
            "flagged_papers_questions": counters.get("flagged_papers_questions"),
            "identity": (
                f"placed({counters.get('placed')}) + skipped_hindi({counters.get('skipped_hindi')}) "
                f"+ unclassified({counters.get('unclassified')}) + "
                f"flagged({counters.get('flagged_papers_questions')}) = "
                f"{(counters.get('placed') or 0) + (counters.get('skipped_hindi') or 0) + (counters.get('unclassified') or 0) + (counters.get('flagged_papers_questions') or 0)}"
            ),
            "papers_validated": counters.get("papers_validated"),
            "papers_flagged": counters.get("papers_flagged"),
        }
    return {
        "papers": coverage.get("papers"),
        "questions": coverage.get("questions"),
        "placed": coverage.get("placed"),
        "unclassified": coverage.get("unclassified"),
        "skipped_hindi": coverage.get("skipped_hindi"),
        "flagged_papers_questions": coverage.get("flagged_papers_questions"),
        "identity": (
            f"placed({coverage.get('placed')}) + skipped_hindi({coverage.get('skipped_hindi')}) "
            f"+ unclassified({coverage.get('unclassified')}) + "
            f"flagged({coverage.get('flagged_papers_questions')}) = {coverage.get('accounted')}"
        ),
        "papers_validated": coverage.get("papers_validated"),
        "papers_flagged": coverage.get("papers_flagged"),
    }

def cmd_phase0(args: argparse.Namespace, settings: Settings, exams: ExamTable, log: Log, run_id: str) -> int:
    return cmd_phase(0, settings, exams, log, run_id)

def cmd_run(args: argparse.Namespace, settings: Settings, exams: ExamTable, log: Log) -> int:
    from .phases import PHASE_ORDER

    for directory in (paths.STATE_DIR, paths.DATABASE_DIR, paths.REPORT_DIR):
        paths.ensure_dir(directory)

    run_id = checkpoint.run_id()
    resume = not args.fresh
    stored = checkpoint.load_checkpoint() if resume else None
    completed: List[str] = []
    if stored:
        completed = list(stored.get("cursor", {}).get("completed", []))
        log.info(
            f"resuming run {stored.get('run_id')} at phase {stored.get('phase')} "
            f"(completed: {completed or 'none'})"
        )

    phases = [f"phase{args.phase}"] if args.phase is not None else list(PHASE_ORDER)
    # One work window spans the whole run (not one per phase): MAX_WORK_SECONDS
    # bounds the job, and a spent window stops the run cleanly with exit code 4.
    window = checkpoint.make_window(settings)
    exit_code = EXIT_OK

    for name in phases:
        if name in completed and resume:
            log.info(f"{name}: already complete (checkpoint), skipping")
            continue
        if window.expired():
            note = f"work window expired after {window.elapsed():.0f}s – phase not started"
            log.warn(f"{name}: {note} – checkpointing")
            checkpoint.save_checkpoint(
                checkpoint.new_checkpoint(
                    name,
                    run_id,
                    {"completed": completed, "next": name},
                    window,
                    status=checkpoint.STATUS_TIME_LIMIT,
                    notes=[note],
                ),
                paths.CHECKPOINT_JSON,
            )
            tracking.save_progress(tracking.record_phase(name, checkpoint.STATUS_TIME_LIMIT, notes=[note]))
            tracking.write_progress_md(extra=None)
            exit_code = EXIT_TIME_LIMIT
            break
        checkpoint.save_checkpoint(
            checkpoint.new_checkpoint(name, run_id, {"completed": completed}, window),
            paths.CHECKPOINT_JSON,
        )
        code = cmd_phase(int(name[-1]), settings, exams, log, run_id, window=window)
        if code != EXIT_OK:
            exit_code = code
            soft_status = {
                EXIT_RATE_LIMITED: checkpoint.STATUS_RATE_LIMITED,
                EXIT_TIME_LIMIT: checkpoint.STATUS_TIME_LIMIT,
            }.get(code, checkpoint.STATUS_ERROR)
            checkpoint.save_checkpoint(
                checkpoint.new_checkpoint(
                    name,
                    run_id,
                    {"completed": completed, "next": name},
                    window,
                    status=soft_status,
                ),
                paths.CHECKPOINT_JSON,
            )
            tracking.write_progress_md(extra=None)
            break
        completed.append(name)
        checkpoint.save_checkpoint(
            checkpoint.new_checkpoint(
                name, run_id, {"completed": completed, "next": _next_phase(completed)}, window
            ),
            paths.CHECKPOINT_JSON,
        )
        tracking.write_progress_md(extra=None)

    # The mock catalogue is derived from the finished index; only build it when
    # the whole pipeline ran and every phase succeeded.
    if exit_code == EXIT_OK and args.phase is None and _next_phase(completed) is None:
        from . import mockdata

        summary = mockdata.write_mock_catalogue(log=log)
        tracking.record_phase("mocks", "ok", {"packs": summary.get("packs", 0)})
        tracking.record_files(
            "mocks",
            [paths.MOCKS_DIR / "index.json"]
            + sorted(p for p in (paths.MOCKS_DIR / "packs").rglob("*.json") if p.is_file()),
        )
        tracking.write_progress_md(extra=None)

    return exit_code

def _next_phase(completed: Sequence[str]) -> Optional[str]:
    from .phases import PHASE_ORDER

    for name in PHASE_ORDER:
        if name not in completed:
            return name
    return None

def cmd_verify_db(args: argparse.Namespace, settings: Settings, exams: ExamTable, log: Log, run_id: str) -> int:
    from . import verify

    result = verify.verify_database(
        settings,
        phase=args.phase,
        limit=args.limit,
        batch_size=args.batch_size,
        report_only=args.report_only,
        log=log,
        window=checkpoint.make_window(settings),
    )
    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    # ``skipped_no_keys`` is an expected, successful outcome (python extraction is
    # kept); only a real failure or a halt is an error for the shell.
    if result.status in ("ok", verify.STATUS_SKIPPED):
        return EXIT_OK
    return exit_code_for_status(result.status)

def cmd_audit(args: argparse.Namespace, settings: Settings, exams: ExamTable, log: Log) -> int:
    """``tools/audit_db.py`` — structural self-check; non-zero exit on violation."""

    if str(paths.PROJECT_ROOT) not in sys.path:
        sys.path.insert(0, str(paths.PROJECT_ROOT))
    from tools import audit_db

    database = Path(args.database) if getattr(args, "database", None) else None
    report = audit_db.run_audit(database)
    if getattr(args, "json", False):
        print(json.dumps(report.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(audit_db.format_report(report))
    tracking.journal(
        "audit.complete",
        {"ok": report.ok, "violations": len(report.issues), "records": report.records},
    )
    return EXIT_OK if report.ok else EXIT_ERROR

def cmd_stats(args: argparse.Namespace, settings: Settings, exams: ExamTable, log: Log) -> int:
    from . import indexer

    records = indexer.read_index()
    if not records:
        print("no index found – run `python -m agent.cli phase0` first", file=sys.stderr)
        return EXIT_ERROR

    coverage = indexer.coverage(records, read_json(paths.PAPERS_JSON, default=[]))
    by_exam_subject: Dict[str, Dict[str, int]] = {}
    by_exam: Dict[str, int] = {}
    by_subject: Dict[str, int] = {}
    concepts: Dict[str, int] = {}
    for record in records:
        exam = record.get("exam") or "UNKNOWN"
        subject = record.get("subject") or "UNKNOWN"
        by_exam_subject.setdefault(exam, {})
        by_exam_subject[exam][subject] = by_exam_subject[exam].get(subject, 0) + 1
        by_exam[exam] = by_exam.get(exam, 0) + 1
        by_subject[subject] = by_subject.get(subject, 0) + 1
        concept = record.get("concept") or "_unclassified"
        concepts[concept] = concepts.get(concept, 0) + 1

    top = sorted(concepts.items(), key=lambda kv: (-kv[1], kv[0]))[: args.top]

    if args.json:
        print(
            json.dumps(
                {
                    "coverage": coverage,
                    "by_exam": by_exam,
                    "by_subject": by_subject,
                    "by_exam_subject": by_exam_subject,
                    "top_concepts": [{"concept": k, "questions": v} for k, v in top],
                },
                indent=2,
                ensure_ascii=False,
            )
        )
        return EXIT_OK

    subjects = ["REAS", "GK", "MATH", "ENG", "COMPUTER"]
    print("=" * 78)
    print("SSC PYQ INDEX — per exam / per subject question counts")
    print("=" * 78)
    header = f"{'exam':16s}" + "".join(f"{s:>10s}" for s in subjects) + f"{'total':>10s}"
    print(header)
    print("-" * len(header))
    for exam in sorted(by_exam_subject):
        row = f"{exam:16s}"
        for subject in subjects:
            row += f"{by_exam_subject[exam].get(subject, 0):>10,d}"
        row += f"{by_exam[exam]:>10,d}"
        print(row)
    print("-" * len(header))
    row = f"{'TOTAL':16s}"
    for subject in subjects:
        row += f"{by_subject.get(subject, 0):>10,d}"
    row += f"{sum(by_exam.values()):>10,d}"
    print(row)
    print()
    print(f"papers in scope          : {human_int(coverage['papers'])}")
    print(f"papers validated         : {human_int(coverage['papers_validated'])}"
          f"  ({pct(coverage['papers_validated'], coverage['papers']):.1f}%)"
          f"  [section-level agreement >= 95%]")
    print(f"papers flagged           : {human_int(coverage['papers_flagged'])}"
          f"  ({pct(coverage['papers_flagged'], coverage['papers']):.1f}%)")
    print()
    print(f"questions in scope       : {human_int(coverage['questions'])}")
    print(f"  placed                 : {human_int(coverage['placed'])}")
    print(f"  unclassified           : {human_int(coverage['unclassified'])}")
    print(f"  skipped_hindi          : {human_int(coverage['skipped_hindi'])}")
    print(f"  flagged_papers_questions: {human_int(coverage['flagged_papers_questions'])}")
    print(f"  identity balanced      : {coverage['balanced']}")
    print()
    print(f"top {args.top} concepts")
    print("-" * 60)
    for i, (concept, count) in enumerate(top, 1):
        print(f"{i:3d}. {concept[:48]:50s} {count:>8,d}")
    return EXIT_OK

def cmd_mocks(args: argparse.Namespace, settings: Settings, exams: ExamTable, log: Log) -> int:
    from . import mockdata

    kwargs = {
        "max_packs": args.max_packs,
        "log": log,
    }
    if args.min_size is not None:
        kwargs["min_size"] = args.min_size
    if args.pack_size is not None:
        kwargs["pack_size"] = args.pack_size
    summary = mockdata.write_mock_catalogue(**kwargs)
    print(json.dumps(summary, indent=2, ensure_ascii=False))
    return EXIT_OK

# ---------------------------------------------------------------------------
# main
# ---------------------------------------------------------------------------

def main(argv: Optional[Sequence[str]] = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    log = Log("agent", quiet=bool(getattr(args, "quiet", False)))

    settings = config.settings()
    exams = config.exams()
    run_id = checkpoint.run_id()

    if args.command is None:
        parser.print_help()
        return EXIT_USAGE

    try:
        if args.command == "taxonomy":
            return cmd_taxonomy(args, settings, exams, log)
        if args.command == "phase0":
            return cmd_phase0(args, settings, exams, log, run_id)
        if args.command == "phase":
            return cmd_phase(args.number, settings, exams, log, run_id)
        if args.command == "run":
            return cmd_run(args, settings, exams, log)
        if args.command == "verify-db":
            return cmd_verify_db(args, settings, exams, log, run_id)
        if args.command == "audit":
            return cmd_audit(args, settings, exams, log)
        if args.command == "stats":
            return cmd_stats(args, settings, exams, log)
        if args.command == "mocks":
            return cmd_mocks(args, settings, exams, log)
    except KeyboardInterrupt:
        log.warn("interrupted")
        return EXIT_ERROR
    except Exception:  # noqa: BLE001 - CLI boundary
        traceback.print_exc()
        return EXIT_ERROR

    parser.print_help()
    return EXIT_USAGE

if __name__ == "__main__":
    raise SystemExit(main())
