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
``routes``      the ``(provider, model)`` candidate matrix; ``--probe`` tests
                every route and prints OK / failure class
``publish``     one checkpoint commit + push to the ``pyq-db`` branch
"""

from __future__ import annotations

import argparse
import json
import sys
import traceback
from dataclasses import dataclass
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import checkpoint, config, paths, taxonomy, tracking
from .classify import Classifier, collect_raw_labels
from .config import ExamTable, Settings
from .util import Log, human_int, monotonic, pct, read_json

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

    p_routes = sub.add_parser(
        "routes", help="show the (provider, model) route matrix — and optionally probe it"
    )
    p_routes.add_argument(
        "--probe",
        action="store_true",
        help="send one tiny completion to every configured (provider, model) candidate",
    )
    p_routes.add_argument("--json", action="store_true", help="machine-readable report")
    p_routes.add_argument(
        "--role", choices=["proposer", "critic", "all"], default="all", help="which role's candidates"
    )
    p_routes.add_argument(
        "--provider", action="append", default=None, help="limit to a provider (repeatable)"
    )
    p_routes.add_argument(
        "--model", action="append", default=None, help="limit to a model (repeatable)"
    )
    p_routes.add_argument(
        "--timeout", type=int, default=30, help="per-probe timeout in seconds (default 30)"
    )
    p_routes.add_argument("--max-tokens", type=int, default=64, help="probe completion budget")

    p_publish = sub.add_parser(
        "publish", help="commit + push the generated tree to the checkpoint branch"
    )
    p_publish.add_argument(
        "--force", action="store_true", help="publish even when PYQ_GIT_PUSH is not set"
    )
    p_publish.add_argument("--json", action="store_true")

    p_audit = sub.add_parser(
        "audit", help="deterministic structural self-check of database/"
    )
    p_audit.add_argument("--json", action="store_true", help="machine-readable report")
    p_audit.add_argument(
        "--database", default=None, help="audit another database directory"
    )

    p_errors = sub.add_parser(
        "errors", help="summarise the AI failure ledger (state/errors.jsonl)"
    )
    p_errors.add_argument("--top", type=int, default=20, help="how many groups to list")
    p_errors.add_argument("--json", action="store_true", help="machine-readable summary")
    p_errors.add_argument(
        "--ledger", default=None, help="read another ledger file (default: state/errors.jsonl)"
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

    p_grammar = sub.add_parser(
        "grammar",
        help="route grammar questions over the 129 rules (keyword matcher + AI pass)",
    )
    p_grammar.add_argument(
        "--ai",
        action="store_true",
        help="run the deepseek-proposes / gpt-5.6-sol-judges pass over the residual",
    )
    p_grammar.add_argument(
        "--limit",
        type=int,
        default=0,
        help="max questions handed to the AI this run (0 = every pending question)",
    )
    p_grammar.add_argument(
        "--batch-size", type=int, default=None, help="questions per AI call (default 20)"
    )
    p_grammar.add_argument(
        "--report-only",
        action="store_true",
        help="run the AI pass but keep the stored verdicts (nothing new is applied)",
    )
    p_grammar.add_argument(
        "--no-write", action="store_true", help="do not rewrite the database views"
    )
    p_grammar.add_argument("--json", action="store_true", help="machine-readable result")
    return parser

# ---------------------------------------------------------------------------
# commands
# ---------------------------------------------------------------------------

def cmd_taxonomy(args: argparse.Namespace, settings: Settings, exams: ExamTable, log: Log) -> int:
    paths.ensure_dir(paths.STATE_DIR)
    tax, alias, cache, labels = _build_taxonomy(
        settings, exams, log, from_cache=bool(getattr(args, "from_cache", False))
    )
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

def _build_taxonomy(settings: Settings, exams: ExamTable, log: Log, *, from_cache: bool):
    """Parse ``chapter-and-topic/*.md`` -> taxonomy + alias map (+ label cache)."""

    from . import discover
    from .util import write_json

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
    if from_cache and cache.is_file():
        labels = read_json(cache, default=[])
        log.info(f"labels: reusing {len(labels)} cached raw concepts/tags")
    else:
        papers, _ = discover.discover(settings, exams)
        labels = collect_raw_labels(papers)
        write_json(cache, labels)
        log.info(f"labels: collected {len(labels)} raw concepts/tags from {len(papers)} papers")

    alias = taxonomy.build_alias_map(tax, labels)
    taxonomy.write_alias_map(alias)
    return tax, alias, cache, labels

def _ensure_taxonomy(settings: Settings, exams: ExamTable, log: Log) -> bool:
    """Build the taxonomy before a fresh run needs it.

    ``state/`` is generated and gitignored, so a fresh CI checkout has no
    ``taxonomy.json``: without it every question would fall through to
    ``unclassified``.  Returns ``True`` when the taxonomy was (re)built.
    """

    if paths.TAXONOMY_JSON.is_file() and paths.ALIAS_MAP_JSON.is_file():
        return False
    log.step("taxonomy missing – rebuilding it from chapter-and-topic/*.md")
    tax, alias, cache, _labels = _build_taxonomy(settings, exams, log, from_cache=True)
    tracking.journal("taxonomy.complete", tax["counts"])
    tracking.record_files("taxonomy", [paths.TAXONOMY_JSON, paths.ALIAS_MAP_JSON, cache])
    log.info(f"taxonomy: rebuilt ({len(alias['map'])} alias keys)")
    return True

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

def os_environ_keys(settings: Optional[Settings] = None) -> List[str]:
    """Every supported credential variable that is present in the environment.

    Derived from the *shipped* route table (:func:`agent.llm.keys_env_names` over
    ``config/settings.json`` ``providers``), so adding a provider cannot silently
    leave this preflight behind — ``GROQ_API_KEY``, ``ZEN_PROXY_TOKEN``,
    ``AGENTROUTER_KEYS``, ``JUSTWOKER_KEYS``, ``AR_PROXY_TOKEN`` and
    ``JW_PROXY_TOKEN`` are all covered — plus the non-provider credentials the
    agent uses (``MONID_API_KEY`` for web search).
    """

    import os

    from . import llm

    try:
        providers = llm.resolve_providers(settings if settings is not None else config.load_settings())
    except Exception:  # pragma: no cover - a broken settings file must not break preflight
        providers = None
    names = list(llm.keys_env_names(providers))
    for name in ("MONID_API_KEY",):
        if name not in names:
            names.append(name)
    return [name for name in names if os.environ.get(name, "").strip()]

def outage_reason(log: Optional[Log] = None, *, path: Optional[Path] = None) -> str:
    """The machine-readable reason behind an ``ai_unavailable`` status.

    Derived from the error ledger so ``progress.json`` / ``manifest.json`` /
    ``PROGRESS.md`` can answer *why* the AI step was skipped instead of only
    saying that it was.  Best-effort: a broken ledger must not fail a run.
    """

    from . import errors as errors_mod

    logger = log or Log("cli")
    try:
        code, evidence = errors_mod.outage_reason(
            path, keys_configured=bool(os_environ_keys())
        )
    except Exception as error:  # pragma: no cover - defensive: never fail a run here
        logger.warn(f"could not derive the ai_unavailable reason: {error}")
        return ""
    logger.warn(
        f"ai_unavailable reason: {code} "
        f"({evidence.get('rows', 0)} ledger row(s), kinds={evidence.get('kinds', {})})"
    )
    return code

def record_run_status(
    status: str,
    *,
    note: Optional[str] = None,
    log: Optional[Log] = None,
) -> str:
    """``tracking.record_status`` plus the reason code for an AI outage."""

    from . import errors as errors_mod

    reason = outage_reason(log) if status == checkpoint.STATUS_AI_UNAVAILABLE else ""
    if reason and note and errors_mod.REASON_MEANINGS.get(reason):
        note = f"{note} [reason: {reason}]"
    elif reason:
        note = f"reason: {errors_mod.format_reason(reason)}"
    tracking.record_status(status, note=note or None, reason=reason or None)
    return reason

def cmd_phase(
    number: int,
    settings: Settings,
    exams: ExamTable,
    log: Log,
    run_id: str,
    window=None,
    *,
    no_ai: bool = False,
) -> int:
    from .phases import phase_module

    name = f"phase{number}"
    classifier = load_classifier(log)
    ctx = _make_context(settings, exams, log, classifier, run_id, window=window)
    ctx.no_ai = no_ai
    module = phase_module(name)
    result = module.run(ctx)
    notes = _ai_notes(result)
    tracking.save_progress(
        tracking.record_phase(name, result.status, result.counters, notes=notes or None)
    )
    _record_phase_ai(name, result)
    tracking.write_progress_md(extra=_coverage_extra(ctx))
    print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    return exit_code_for_status(result.status)

def _ai_notes(result) -> List[str]:
    """Notes that belong next to the AI status rather than the phase counters."""

    notes: List[str] = []
    for note in result.notes:
        if note.startswith("halted (AI unavailable)") or note.startswith("no healthy route"):
            notes.append(note)
    return notes

def _record_phase_ai(phase: str, result) -> None:
    """Publish the phase's AI work-item progress + route health (R6)."""

    counters = result.counters or {}
    if counters.get("ai_items_total") is not None:
        from . import router as router_mod

        settings = config.settings()
        router = router_mod.get_router(settings)
        tracking.record_ai(
            phase,
            {
                "items_total": counters.get("ai_items_total"),
                "items_done": counters.get("ai_items_done"),
                "items_this_run": counters.get("ai_items_this_run"),
                "items_remaining": counters.get("ai_items_remaining"),
                "eta_seconds": counters.get("ai_eta_seconds"),
                "batches_verified": counters.get("ai_batches_verified"),
                "batches_failed": counters.get("ai_batches_failed"),
            },
            status=str(counters.get("verification") or ""),
        )
        tracking.record_routes(router.provider_report())

def exit_code_for_status(status: str) -> int:
    """Phase/verify status -> process exit code.

    ``rate_limited`` (3) and ``time_limit`` (4) are **soft stops**: the phase
    checkpointed and published, so the shell must not treat them as failures.
    ``ai_unavailable`` (and ``skipped_no_keys``) are not even soft stops — the
    deterministic pipeline finished, so they exit **0**.
    """

    if status in ("ok", "done", "complete", "skipped_no_keys", checkpoint.STATUS_AI_UNAVAILABLE):
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

def _publish_checkpoint(phase: str, log: Log, *, done: int = 0, total: int = 0) -> None:
    """Publish ``state/`` + ``database/`` at a checkpoint boundary.

    Throttled to ``CHECKPOINT_INTERVAL_SECONDS`` and disabled unless
    ``PYQ_GIT_PUSH`` is set, so a local run never pushes; failures are logged and
    never abort the run (see :mod:`agent.gitpush`).
    """

    from . import gitpush

    result = gitpush.maybe_publish(phase=phase, done=done, total=total, log=log)
    if result.reason == gitpush.REASON_OK:
        log.info(f"{phase}: checkpoint published to the {result.branch} branch")
    elif result.attempted and result.reason not in (gitpush.REASON_NOTHING, gitpush.REASON_THROTTLED):
        log.warn(f"{phase}: checkpoint publish skipped – {result.reason}")

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

    # ``state/`` is generated: a fresh tree (CI) has no taxonomy, and without it
    # phase 0 would classify nothing.
    if args.phase in (None, 0):
        _ensure_taxonomy(settings, exams, log)

    phases = [f"phase{args.phase}"] if args.phase is not None else list(PHASE_ORDER)
    # One work window spans the whole run (not one per phase): MAX_WORK_SECONDS
    # bounds the job, and a spent window stops the run cleanly with exit code 4.
    window = checkpoint.make_window(settings)
    exit_code = EXIT_OK
    #: ``ok`` / ``ai_unavailable`` — a phase-level AI outage degrades the run
    #: status but never stops the deterministic pipeline (R4).
    run_status = checkpoint.STATUS_OK
    no_ai = bool(getattr(args, "no_ai", False))

    for name in phases:
        if name in completed and resume:
            log.info(f"{name}: already complete (checkpoint), skipping")
            continue
        if window.expired():
            note = f"work window expired after {window.elapsed():.0f}s – phase not started"
            log.warn(f"{name}: {note} – checkpointing")
            _save_status(
                checkpoint.STATUS_TIME_LIMIT,
                name,
                run_id,
                {"completed": completed, "next": name},
                window,
                note=note,
                log=log,
            )
            tracking.write_progress_md(extra=None)
            _publish_checkpoint(name, log, done=len(completed), total=len(phases))
            exit_code = EXIT_TIME_LIMIT
            run_status = checkpoint.STATUS_TIME_LIMIT
            break
        checkpoint.save_checkpoint(
            checkpoint.new_checkpoint(name, run_id, {"completed": completed}, window),
            paths.CHECKPOINT_JSON,
        )
        code = cmd_phase(int(name[-1]), settings, exams, log, run_id, window=window, no_ai=no_ai)
        if code not in (EXIT_OK,):
            exit_code = code
            soft_status = {
                EXIT_RATE_LIMITED: checkpoint.STATUS_RATE_LIMITED,
                EXIT_TIME_LIMIT: checkpoint.STATUS_TIME_LIMIT,
            }.get(code, checkpoint.STATUS_ERROR)
            note = (
                f"{name}: stopped with status={soft_status} "
                f"(exit {code}) – resumes from state/checkpoint.json"
            )
            _save_status(
                soft_status,
                name,
                run_id,
                {"completed": completed, "next": name},
                window,
                note=note,
                log=log,
            )
            tracking.write_progress_md(extra=None)
            _publish_checkpoint(name, log, done=len(completed), total=len(phases))
            run_status = soft_status
            break
        completed.append(name)
        phase_status = _phase_status(name, checkpoint.STATUS_OK)
        if phase_status == "ai_unavailable":
            # The AI step was skipped, the phase itself is deterministic and
            # complete: keep going, the run is still a success (exit 0).
            run_status = checkpoint.STATUS_AI_UNAVAILABLE
            log.warn(
                f"{name}: AI unavailable – database slice written, continuing with the "
                "deterministic pipeline (run status=ai_unavailable, exit 0)"
            )
        checkpoint.save_checkpoint(
            checkpoint.new_checkpoint(
                name,
                run_id,
                {"completed": completed, "next": _next_phase(completed)},
                window,
                status=run_status,
            ),
            paths.CHECKPOINT_JSON,
        )
        tracking.write_progress_md(extra=None)
        _publish_checkpoint(name, log, done=len(completed), total=len(phases))

    # The mock catalogue is derived from the finished index; build it whenever the
    # pipeline ran to the end without a hard error — an AI outage must not cost
    # the run its mock packs.
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

    if exit_code == EXIT_OK:
        note = tracking.STATUS_NOTES.get(run_status, "")
        # an AI outage must say *why*: `status_reason` is derived from the ledger
        record_run_status(run_status, note=note or None, log=log)
        checkpoint.save_checkpoint(
            checkpoint.new_checkpoint(
                phases[-1] if phases else "phase0",
                run_id,
                {"completed": completed, "next": _next_phase(completed)},
                window,
                status=run_status,
                notes=[note] if note else None,
            ),
            paths.CHECKPOINT_JSON,
        )
        tracking.write_progress_md(extra=None)
        log.info(f"run complete: status={run_status} (exit 0)")
    return exit_code

def _save_status(
    status: str,
    phase: str,
    run_id: str,
    cursor: Dict[str, Any],
    window,
    *,
    note: Optional[str] = None,
    log: Optional[Log] = None,
) -> None:
    """Persist a run status to the checkpoint, manifest and progress roll-up."""

    if log is not None:
        log.warn(f"run status={status} at {phase}")
    checkpoint.save_checkpoint(
        checkpoint.new_checkpoint(
            phase, run_id, cursor, window, status=status, notes=[note] if note else None
        ),
        paths.CHECKPOINT_JSON,
    )
    tracking.record_phase(phase, status, notes=[note] if note else None)
    record_run_status(status, note=note, log=log)

def _phase_status(phase: str, default: str) -> str:
    """The status the phase last recorded in ``state/progress.json``."""

    entry = (tracking.load_progress().get("phases") or {}).get(phase) or {}
    return str(entry.get("status") or default)

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
    # a standalone verification run is a run: record its status so the job summary
    # and PROGRESS.md do not keep showing the previous run's outcome
    if result.status not in ("ok", verify.STATUS_SKIPPED):
        record_run_status(result.status, note="; ".join(result.notes[:2]) or None, log=log)
        tracking.write_progress_md(extra=_coverage_extra(None))
    # ``skipped_no_keys`` (offline) and ``ai_unavailable`` (every route down) are
    # expected, successful outcomes: the python extraction is kept and the run
    # does not fail because a provider is having a bad day.
    if result.status in ("ok", verify.STATUS_SKIPPED, verify.STATUS_AI_UNAVAILABLE):
        return EXIT_OK
    return exit_code_for_status(result.status)

# ---------------------------------------------------------------------------
# routes — the (provider, model) matrix
# ---------------------------------------------------------------------------

#: cell codes of the route matrix (probe = one tiny completion per candidate)
ROUTE_OK = "ok"
ROUTE_COOLDOWN = "cooldown"
ROUTE_NO_KEY = "nokey"


@dataclass
class ProbeOutcome:
    """Result of probing one ``(provider, model)`` candidate."""

    provider: str
    model: str
    ok: bool
    status: str  # "ok" or a llm.failure_class token
    detail: str = ""
    latency_ms: int = 0
    keys: int = 0
    error: bool = False

    def as_dict(self) -> Dict[str, Any]:
        return {
            "provider": self.provider,
            "model": self.model,
            "ok": self.ok,
            "status": self.status,
            "detail": self.detail,
            "latency_ms": self.latency_ms,
            "keys": self.keys,
            "error": self.error,
        }


def _probe_messages() -> List[Any]:
    from . import llm

    return [
        llm.ChatMessage("system", "Reply with JSON only."),
        llm.ChatMessage("user", 'Reply with the single JSON object {"ok": true}.'),
    ]


def _probe_route(
    provider: Any,
    pool: Sequence[str],
    model: str,
    *,
    timeout: int,
    max_tokens: int,
) -> ProbeOutcome:
    """One tiny completion against a single route; never raises, never logs a key."""

    from . import llm

    if not pool:
        return ProbeOutcome(provider.name, model, False, ROUTE_NO_KEY, "no credentials", keys=0)
    last: Optional[ProbeOutcome] = None
    # the router rotates keys; probing only the first one would report a pool as
    # dead when a second key still answers, so try up to two
    for index, key in enumerate(pool[:2]):
        started = monotonic()
        try:
            result = llm.chat_completion(
                base_url=provider.base_url,
                api_key=key,
                model=model,
                messages=_probe_messages(),
                timeout=timeout,
                temperature=0.0,
                max_tokens=max_tokens,
                auth_style=provider.auth_style,
                user_agent_value=provider.user_agent_value(),
                path=provider.request_path(),
                # a reasoning model can spend the whole probe budget on its
                # reasoning and answer with empty content: escalate once (the
                # router does the same) instead of reporting a working route dead
                truncation_retries=1,
            )
        except llm.LlmError as exc:
            last = ProbeOutcome(
                provider.name,
                model,
                False,
                llm.failure_class(exc),
                str(exc)[:200],
                int((monotonic() - started) * 1000),
                keys=len(pool),
            )
            if index == 0 and len(pool) > 1 and (getattr(exc, "status", None) or 0) in (401, 402, 403, 429):
                last.detail += " (retrying with the next key of the pool)"
                continue
            return last
        except Exception as exc:  # noqa: BLE001 - a probe must never raise
            return ProbeOutcome(
                provider.name,
                model,
                False,
                "error",
                f"{type(exc).__name__}: {exc}"[:200],
                int((monotonic() - started) * 1000),
                keys=len(pool),
                error=True,
            )
        return ProbeOutcome(
            provider.name,
            model,
            True,
            ROUTE_OK,
            "",
            result.latency_ms,
            keys=len(pool),
        )
    return last or ProbeOutcome(provider.name, model, False, ROUTE_NO_KEY, "no credentials")


def _route_cell(row: Dict[str, Any], *, probe: bool) -> str:
    if not row.get("configured", True):
        return ROUTE_NO_KEY
    if probe:
        return str(row.get("status") or "-")
    return ROUTE_OK if row.get("healthy") else ROUTE_COOLDOWN


def _print_route_matrix(
    rows: List[Dict[str, Any]],
    models: Sequence[str],
    providers: Sequence[str],
    *,
    probe: bool,
    log: Log,
) -> None:
    """Provider × model table (one short cell per route)."""

    by_pair = {(row["provider"], row["model"]): row for row in rows}
    width = max([len(m) for m in models] + [8])
    header = f"{'provider':12s}" + "".join(f"{model:>{width + 2}s}" for model in models)
    print("=" * len(header))
    print(f"PYQ route matrix — {'probed' if probe else 'configured/health'} (provider × model)")
    print("=" * len(header))
    print(header)
    print("-" * len(header))
    for provider in providers:
        line = f"{provider:12s}"
        for model in models:
            row = by_pair.get((provider, model))
            cell = "-" if row is None else _route_cell(row, probe=probe)
            line += f"{cell:>{width + 2}s}"
        print(line)
    print("-" * len(header))
    print(
        "cells: ok = route answered"
        + (
            " | <class> = probe failure (http_403, rate_limited:503, waf_html, bad_json, transport, nokey)"
            if probe
            else " | cooldown = breaker open | nokey = no credentials configured | - = not a candidate"
        )
    )
    print()

    failures = [row for row in rows if probe and not row["ok"]]
    if failures:
        print("probe failures")
        print("-" * 78)
        for row in failures:
            detail = row.get("detail") or ""
            print(f"  {row['provider']}/{row['model']}: {row['status']}" + (f" — {detail}" if detail else ""))
        print()

    for role in ("proposer", "critic"):
        eligible = [row for row in rows if role in row.get("roles", [])]
        working = [row for row in eligible if row["ok"]] if probe else [
            row for row in eligible if row.get("healthy")
        ]
        label = f"{role}-eligible working route(s)"
        if working:
            print(
                f"{label}: "
                + ", ".join(f"{row['provider']}/{row['model']}" for row in working)
            )
        else:
            print(f"{label}: none")
    print()


def cmd_routes(args: argparse.Namespace, settings: Settings, exams: ExamTable, log: Log) -> int:
    """``python -m agent.cli routes [--probe]`` — the route matrix.

    Without ``--probe`` it reports the configured candidates and their breaker
    state; with ``--probe`` it sends one tiny completion to every
    ``(provider, model)`` candidate and prints OK / failure class.  Keys are read
    from the environment and never printed (a route's key is never echoed, only
    its pool size).
    """

    from . import llm
    from . import router as router_mod

    router = router_mod.get_router(settings, log=log)
    roles = ("proposer", "critic") if args.role == "all" else (args.role,)
    providers = llm.resolve_providers(settings)
    by_name = llm.provider_index(providers)
    pools = llm.provider_keys(providers)

    wanted_providers = set(args.provider or ())
    wanted_models = set(args.model or ())

    rows: List[Dict[str, Any]] = []
    for role in roles:
        for name, model in router.role_routes(role):
            if wanted_providers and name not in wanted_providers:
                continue
            if wanted_models and model not in wanted_models:
                continue
            row = next(
                (r for r in rows if r["provider"] == name and r["model"] == model), None
            )
            if row is None:
                healthy, why = router.route_healthy(name, model)
                row = {
                    "provider": name,
                    "model": model,
                    "configured": bool(pools.get(name)),
                    "healthy": healthy and not router.is_halted(model),
                    "why": why,
                    "halted": router.is_halted(model),
                    "status": "",
                    "detail": "",
                    "latency_ms": 0,
                    "keys": len(pools.get(name, ())),
                    "error": False,
                    "roles": [],
                }
                rows.append(row)
            if role not in row["roles"]:
                row["roles"].append(role)

    if not rows:
        print("no route candidates matched the filter", file=sys.stderr)
        return EXIT_USAGE

    if args.probe:
        for row in rows:
            provider = by_name.get(row["provider"])
            if provider is None:
                row["status"] = "unknown-provider"
                row["error"] = True
                continue
            log.info(f"probing {row['provider']}/{row['model']} …")
            outcome = _probe_route(
                provider,
                pools.get(row["provider"], []),
                row["model"],
                timeout=args.timeout,
                max_tokens=args.max_tokens,
            )
            row.update(outcome.as_dict())
            row["roles"] = row["roles"]
            row["configured"] = bool(pools.get(row["provider"]))
            log.info(
                f"{row['provider']}/{row['model']}: {outcome.status}"
                + (f" ({outcome.latency_ms} ms)" if outcome.ok else f" — {outcome.detail[:120]}")
            )

    models = sorted({row["model"] for row in rows}, key=lambda m: next(
        i for i, r in enumerate(rows) if r["model"] == m
    ))
    providers_seen = sorted({row["provider"] for row in rows}, key=lambda p: next(
        i for i, r in enumerate(rows) if r["provider"] == p
    ))

    summary = {
        "probe": bool(args.probe),
        "timeout_seconds": args.timeout,
        "candidate_models": {
            role: list(settings.debate.candidates_for(role)) for role in roles
        },
        "providers": list(providers_seen),
        "models": list(models),
        "routes": rows,
        "working": {
            role: [
                f"{row['provider']}/{row['model']}"
                for row in rows
                if role in row["roles"] and (row["ok"] if args.probe else row["healthy"])
            ]
            for role in roles
        },
    }

    if args.json:
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        _print_route_matrix(rows, models, providers_seen, probe=bool(args.probe), log=log)

    tracking.journal(
        "routes.probe" if args.probe else "routes.report",
        {
            "routes": len(rows),
            "working": {role: values for role, values in summary["working"].items()},
            "failures": {
                f"{row['provider']}/{row['model']}": row["status"]
                for row in rows
                if args.probe and not row["ok"]
            },
        },
    )
    if not args.probe:
        return EXIT_OK
    # a probe is only green when both roles have at least one route that answered
    return EXIT_OK if all(summary["working"].get(role) for role in roles) else EXIT_ERROR


def cmd_publish(args: argparse.Namespace, settings: Settings, exams: ExamTable, log: Log) -> int:
    """``python -m agent.cli publish`` — one checkpoint commit + push.

    This is the same publisher the 15-minute checkpoint tick uses
    (:mod:`agent.gitpush`); ``--force`` publishes even when ``PYQ_GIT_PUSH`` is
    not set.  A failed publish never fails the caller: the status is reported and
    the exit code stays 0 unless the publish was explicitly requested with
    ``--force`` and failed.
    """

    from . import gitpush

    result = gitpush.publisher(log=log).publish(
        phase="manual",
        done=0,
        total=0,
        force=bool(args.force),
    )
    if args.json:
        print(json.dumps(result.as_dict(), indent=2, ensure_ascii=False))
    else:
        print(f"publish: {result.reason}")
        if result.message:
            print(f"message: {result.message}")
        if result.branch:
            print(f"branch : {result.branch}")
    if result.reason == gitpush.REASON_DISABLED:
        print(
            "PYQ_GIT_PUSH is not enabled – re-run with --force to publish anyway",
            file=sys.stderr,
        )
        return EXIT_OK
    if result.ok:
        return EXIT_OK
    return EXIT_ERROR if args.force else EXIT_OK


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

def cmd_errors(args: argparse.Namespace, settings: Settings, exams: ExamTable, log: Log) -> int:
    """``errors`` — summarise the append-only AI failure ledger.

    Exit code 0 whether or not the ledger has rows: an AI outage is a degraded
    (not failed) run, and this command only reports.  A missing ledger prints the
    "no AI failure recorded" report.
    """

    from . import errors as errors_mod

    ledger = Path(args.ledger) if getattr(args, "ledger", None) else None
    summary = errors_mod.summarise(top=int(getattr(args, "top", 20) or 20), path=ledger)
    if getattr(args, "json", False):
        print(json.dumps(summary, indent=2, ensure_ascii=False))
    else:
        print(errors_mod.format_summary(summary))
    tracking.journal(
        "errors.summary",
        {"total": summary["total"], "groups": summary["groups"], "path": summary["path"]},
    )
    return EXIT_OK

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


def cmd_grammar(args: argparse.Namespace, settings: Settings, exams: ExamTable, log: Log) -> int:
    """Route every grammar question to one of the 129 rules and write the views."""

    from . import grammar as grammar_mod
    from . import indexer

    records = indexer.read_index()
    if not records:
        print("no index found – run `python -m agent.cli phase0` first", file=sys.stderr)
        return EXIT_ERROR
    taxonomy = read_json(paths.TAXONOMY_JSON, default=None)
    if not taxonomy:
        print("taxonomy missing – run `python -m agent.cli taxonomy` first", file=sys.stderr)
        return EXIT_ERROR

    eng = [r for r in records if r.get("subject") == "ENG"]
    from .phases.subject_phase import _questions_for

    questions = _questions_for(eng)
    log.info(f"grammar: {human_int(len(eng))} English records, {human_int(len(questions))} raw questions")

    window = checkpoint.make_window(settings)
    result = grammar_mod.build_grammar_view(
        records,
        questions,
        taxonomy,
        settings=settings,
        window=window,
        ai=grammar_mod.AiOptions(
            enabled=bool(args.ai),
            limit=int(args.limit or 0),
            batch_size=int(args.batch_size or grammar_mod.AI_BATCH_SIZE),
            report_only=bool(args.report_only),
        ),
        database_dir=paths.DATABASE_DIR,
        log=log,
    )
    payload = result.as_dict()
    payload["coverage"] = grammar_mod.coverage_line(result)
    if args.json:
        print(json.dumps(payload, indent=2, ensure_ascii=False))
    else:
        counters = result.counters
        print("=" * 78)
        print("GRAMMAR -> 129 RULES")
        print("=" * 78)
        print(f"grammar questions   : {human_int(counters.get('grammar_questions', 0))}")
        print(
            f"assigned            : {human_int(counters.get('grammar_questions_matched', 0))} "
            f"({counters.get('coverage_pct', 0)}%)"
        )
        print(f"  by keyword matcher: {human_int(counters.get('grammar_questions_matched', 0) - counters.get('grammar_questions_assigned_by_ai', 0))}")
        print(f"  by the AI pass    : {human_int(counters.get('grammar_questions_assigned_by_ai', 0))}")
        print(f"unassigned          : {human_int(counters.get('grammar_questions_unassigned', 0))}")
        print(
            f"rules with questions: {human_int(counters.get('rules_with_questions', 0))}"
            f" / {human_int(counters.get('rules', 0))}"
        )
        excluded = int(counters.get("tree_excluded", 0))
        if excluded:
            print(
                f"tree re-filed       : {human_int(excluded)} rule-owned questions "
                "moved out of the concept tree"
            )
        ai_status = counters.get("ai_status")
        if ai_status:
            print(f"ai status           : {ai_status}")
        reasons = {k: v for k, v in counters.items() if k.startswith("reason:")}
        if reasons:
            print("unassigned reasons  : " + ", ".join(f"{k.split(':', 1)[1]}={v}" for k, v in sorted(reasons.items())))
        for note in result.notes:
            print(f"note                : {note}")
        print("=" * 78)
    if not args.no_write:
        tracking.journal(
            "grammar.complete",
            {
                "questions": result.counters.get("grammar_questions", 0),
                "assigned": result.counters.get("grammar_questions_matched", 0),
                "unassigned": result.counters.get("grammar_questions_unassigned", 0),
                "rules_with_questions": result.counters.get("rules_with_questions", 0),
            },
        )
    if result.counters.get("ai_status") == grammar_mod.STATUS_AI_UNAVAILABLE:
        # the AI is a soft stop, exactly like in the verify step: the
        # deterministic result stands and the run is not a failure
        return EXIT_OK
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
        if args.command == "errors":
            return cmd_errors(args, settings, exams, log)
        if args.command == "stats":
            return cmd_stats(args, settings, exams, log)
        if args.command == "mocks":
            return cmd_mocks(args, settings, exams, log)
        if args.command == "grammar":
            return cmd_grammar(args, settings, exams, log)
        if args.command == "routes":
            return cmd_routes(args, settings, exams, log)
        if args.command == "publish":
            return cmd_publish(args, settings, exams, log)
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
