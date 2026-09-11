"""Verification: AI re-check of every python-extracted item.

Design
------
* Work is driven by the question index (``state/index/<subject>.<n>.jsonl``, the
  python extraction result) and **batched** (``settings.verify.batch_size``,
  default 20) so the number of requests is proportional to items / batch.
* Every batch goes through :class:`agent.debate.Debate` (proposer + critic).
* Resumable and idempotent: ``state/verify_state.json`` stores, per phase, the
  set of already-verified batch fingerprints, the cursor and the work-item
  progress (total / done / this run / ETA).  Re-running skips finished batches,
  and a correction is a *set* operation on the index record
  (``concept``/``chapter``/``topic``), so applying the same correction twice is a
  no-op.
* Corrections are appended to ``state/corrections.jsonl`` and applied to the
  index in place; ``database/`` is rebuilt from the corrected index.
* The work window is checked before every batch and a checkpoint is written
  every ``checkpoint_interval_seconds`` (900 s) while the phase runs, so a killed
  job resumes from ``state/checkpoint.json`` instead of starting over.
* Progress (items total / done / this run / remaining / ETA) and the per
  ``(provider, model)`` route health are published to ``state/progress.json`` and
  ``database/_meta/PROGRESS.md``.

When the AI cannot be reached the step **never fails the run**:

``skipped_no_keys``
    no credentials are configured at all (offline / dry run).
``ai_unavailable``
    routes exist but none can serve a role: **every candidate model** of the
    proposer or the critic (``debate.proposer_models`` / ``debate.critic_models``)
    has only cooling-down, failing or unkeyed routes, or the router halted
    outright.  Python extraction results are kept unchanged, the phase still
    writes its database slice, and the run exits 0.
``time_limit``
    the work window expired – the only condition that still stops the pipeline
    (soft stop, exit code 4, resumes next run).

If the AI only survives by serving both roles with the same model, the run
proceeds but says so: ``same_model_fallback: true`` is written to the batch
provenance, the phase counters and ``PROGRESS.md``.
"""

from __future__ import annotations

import hashlib
import json
from collections import Counter
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence

from . import checkpoint as ckpt
from . import debate as debate_mod
from . import gitpush, indexer, llm, paths, router as router_mod, tracking
from .checkpoint import WorkWindow
from .config import Settings
from .util import Log, append_jsonl, chunked, monotonic, now_iso, read_json, write_json

STATUS_OK = "ok"
STATUS_SKIPPED = "skipped_no_keys"
STATUS_AI_UNAVAILABLE = "ai_unavailable"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_TIME_LIMIT = "time_limit"
STATUS_ERROR = "error"

PHASE_SUBJECTS = {
    "phase1": ["ENG"],
    "phase2": ["GK"],
    "phase3": ["MATH"],
    "phase4": ["REAS"],
    "phase5": ["COMPUTER"],
}


@dataclass
class VerifyResult:
    status: str
    counters: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    corrections_file: Optional[Path] = None
    ai_status: str = ""
    routes: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "status": self.status,
            "ai_status": self.ai_status or self.status,
            "counters": self.counters,
            "notes": self.notes,
            "corrections_file": paths.rel(self.corrections_file) if self.corrections_file else None,
            "router": None,
            "routes": self.routes,
        }


@dataclass
class PhaseProgress:
    """Work-item accounting for one phase (drives PROGRESS.md + the ETA)."""

    phase: str
    items_total: int
    items_done_before: int
    batches_total: int
    batches_done_before: int
    batch_size: int
    items_this_run: int = 0
    batches_this_run: int = 0
    batches_failed: int = 0
    items_corrected: int = 0
    started: float = field(default_factory=monotonic)

    @property
    def items_done(self) -> int:
        return min(self.items_total, self.items_done_before + self.items_this_run)

    @property
    def items_remaining(self) -> int:
        return max(0, self.items_total - self.items_done)

    @property
    def batches_done(self) -> int:
        return self.batches_done_before + self.batches_this_run

    def eta_seconds(self) -> Optional[float]:
        """Linear extrapolation from this run's throughput (``None`` = unknown)."""

        elapsed = monotonic() - self.started
        if self.items_this_run <= 0 or elapsed <= 0 or self.items_remaining <= 0:
            return 0.0 if self.items_remaining <= 0 else None
        per_item = elapsed / self.items_this_run
        return self.items_remaining * per_item

    def as_payload(self, *, status: str, elapsed_total: float) -> Dict[str, Any]:
        return {
            "status": status,
            "items_total": self.items_total,
            "items_done": self.items_done,
            "items_done_before": self.items_done_before,
            "items_this_run": self.items_this_run,
            "items_remaining": self.items_remaining,
            "items_corrected": self.items_corrected,
            "batches_total": self.batches_total,
            "batches_done": self.batches_done,
            "batches_this_run": self.batches_this_run,
            "batches_failed": self.batches_failed,
            "batch_size": self.batch_size,
            "eta_seconds": self.eta_seconds(),
            "elapsed_seconds": round(elapsed_total, 1),
            "phase_seconds": round(monotonic() - self.started, 1),
        }


def _run_id() -> str:
    """Reuse the checkpoint's run id so verify checkpoints belong to the run."""

    stored = ckpt.load_checkpoint() or {}
    return str(stored.get("run_id") or ckpt.run_id())


def _router_wide_halt(router: "router_mod.Router") -> bool:
    """True only for a router-wide halt (``halt()`` called without a model).

    A single model going dark is **not** a stop condition any more: the request
    falls back to the next candidate model of its role, and only a role with no
    healthy candidate stops the AI step (``router.role_available``).
    """

    try:
        return "*" in router.halt_report()
    except Exception:  # noqa: BLE001 - a report must never break the loop
        return False


def _checkpoint(
    *,
    phase: str,
    run_id: str,
    window: Optional[WorkWindow],
    progress: PhaseProgress,
    status: str,
    notes: Sequence[str] = (),
    batch: int = 0,
    extra_cursor: Optional[Dict[str, Any]] = None,
) -> None:
    """Write the run checkpoint (+ progress roll-up) without losing the cursor."""

    stored = ckpt.load_checkpoint() or {}
    cursor = dict(stored.get("cursor") or {})
    verify_cursor = dict(cursor.get("verify") or {})
    verify_cursor.update(
        {
            "phase": phase,
            "batch": batch,
            "batches_done": progress.batches_done,
            "items_done": progress.items_done,
            "items_total": progress.items_total,
            "state_file": paths.rel(paths.VERIFY_STATE_JSON),
        }
    )
    if extra_cursor:
        verify_cursor.update(extra_cursor)
    cursor["verify"] = verify_cursor
    ckpt.save_checkpoint(
        ckpt.new_checkpoint(phase, run_id, cursor, window, status=status, notes=list(notes)),
        paths.CHECKPOINT_JSON,
    )
    tracking.record_ai(
        phase,
        progress.as_payload(status=status, elapsed_total=window.elapsed() if window else 0.0),
        status=status,
    )
    tracking.write_progress_md(extra=None)
    # A killed job used to lose every checkpoint since the last phase boundary:
    # publish the generated tree on the checkpoint tick as well (no-op unless
    # PYQ_GIT_PUSH is set, throttled to ``CHECKPOINT_INTERVAL_SECONDS``, and never
    # fatal – a failed commit must not abort the run).
    gitpush.maybe_publish(
        phase=phase,
        done=progress.items_done,
        total=progress.items_total,
    )


def batch_fingerprint(records: Sequence[Dict[str, Any]]) -> str:
    h = hashlib.sha256()
    for record in records:
        h.update(str(record.get("qid")).encode("utf-8"))
        h.update(b"|")
        h.update(str(record.get("concept")).encode("utf-8"))
        h.update(b"|")
        h.update(str(record.get("chapter")).encode("utf-8"))
        h.update(b"\n")
    return h.hexdigest()[:32]


def load_state(path: Optional[Path] = None) -> Dict[str, Any]:
    data = read_json(path or paths.VERIFY_STATE_JSON, default=None)
    if data is None:
        return {"version": 1, "created_at": now_iso(), "phases": {}}
    return data


def save_state(state: Dict[str, Any], path: Optional[Path] = None) -> Path:
    state["updated_at"] = now_iso()
    return write_json(path or paths.VERIFY_STATE_JSON, state)


def _vocabulary_for(taxonomy: Dict[str, Any], subjects: Sequence[str]) -> List[str]:
    out: List[str] = []
    for subject, body in taxonomy.get("subjects", {}).items():
        if subject not in subjects:
            continue
        for chapter in body.get("chapters", []):
            chapter_name = chapter.get("name", "")
            if chapter_name:
                out.append(f"{subject}: {chapter_name}")
            for topic in chapter.get("topics", []):
                topic_name = topic.get("name", "")
                if topic_name:
                    out.append(f"{subject}: {chapter_name} > {topic_name}")
    return out


def _item_payload(record: Dict[str, Any], question: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    payload = {
        "qid": record.get("qid"),
        "subject": record.get("subject"),
        "exam": record.get("exam"),
        "year": record.get("year"),
        "concept_raw": record.get("concept_raw"),
        "tags": record.get("tags"),
        "python_result": {
            "concept": record.get("concept"),
            "chapter": record.get("chapter"),
            "topic": record.get("topic"),
            "class_source": record.get("class_source"),
        },
    }
    if question is not None:
        payload["question"] = str(question.get("question") or "")[:1200]
    return payload


def _parse_verify_reply(reply: Dict[str, Any]) -> Optional[Dict[str, Any]]:
    if not isinstance(reply, dict):
        return None
    if "ok" not in reply and "concept" not in reply:
        return None
    return {
        "ok": bool(reply.get("ok", True)),
        "concept": reply.get("concept"),
        "chapter": reply.get("chapter"),
        "topic": reply.get("topic"),
        "reason": str(reply.get("reason") or "")[:400],
    }


def verify_database(
    settings: Settings,
    *,
    phase: Optional[str] = None,
    limit: int = 0,
    batch_size: Optional[int] = None,
    report_only: bool = False,
    log: Optional[Log] = None,
    papers: Optional[Sequence[Any]] = None,
    window: Optional[WorkWindow] = None,
) -> VerifyResult:
    """Re-check python-extracted items; resumable and idempotent.

    Statuses (all of them keep the python extraction results):

    ``ok``                every selected batch was verified
    ``skipped_no_keys``   no credentials configured – the AI step is a no-op
    ``ai_unavailable``    routes exist but none can serve a model right now; the
                          caller carries on with its deterministic work and the
                          run still exits 0
    ``time_limit``        the work window expired (soft stop, exit code 4)

    When *window* is given the budget is checked **before every batch**: once it
    is spent the loop stops cleanly with ``status=time_limit`` so the caller
    checkpoints instead of being killed mid-run.  A batch that has started is
    always allowed to finish.
    """

    logger = log or Log("verify")
    if not settings.verify.enabled:
        return VerifyResult(STATUS_SKIPPED, notes=["verify disabled in settings"])

    router = router_mod.get_router(settings, log=logger)
    if not llm.keys_available(router.providers):
        logger.warn("no API keys in the environment – AI verification skipped")
        return VerifyResult(
            STATUS_SKIPPED,
            notes=[
                "no API keys found ("
                + " / ".join(llm.keys_env_names(router.providers))
                + "); python extraction results were kept unchanged"
            ],
            routes=router.provider_report(),
        )

    proposer_model = settings.debate.proposer_model
    critic_model = settings.debate.critic_model
    #: a role is served while **any** of its candidate models has a healthy route
    #: (``debate.proposer_models`` / ``debate.critic_models``), not just the
    #: primary one: on a runner where every route for ``deepseek-v4-flash`` is
    #: blocked, ``gpt-5.6-sol`` may still serve the proposer.
    roles = ("proposer", "critic")
    # A spent run budget is checked first: it is a *run* constraint, so a run that
    # has no time left stops with time_limit (exit 4, resumable) even when the AI
    # happens to be down as well.
    if window is None or not window.expired():
        health = router.roles_available(roles)
        if not all(health.values()):
            missing = [role for role in roles if not health[role]]
            reason = (
                "no healthy route for any candidate model of the "
                + ", ".join(missing)
                + " role(s) at startup: "
                + "; ".join(
                    f"{key}: {entry['why']}"
                    for role in missing
                    for key, entry in router.role_health(role).items()
                    if not entry["healthy"]
                )
            )
            logger.warn(f"AI unavailable – {reason}")
            _record_unavailable(router, window, reason)
            return VerifyResult(
                STATUS_AI_UNAVAILABLE,
                counters={"items_total": 0, "items_done": 0, "items_this_run": 0},
                notes=[reason, "python extraction results were kept unchanged"],
                ai_status=STATUS_AI_UNAVAILABLE,
                routes=router.provider_report(),
            )

    records = indexer.read_index()
    if not records:
        return VerifyResult(STATUS_SKIPPED, notes=["index is empty – run `python -m agent.cli phase0`"])

    phases = [phase] if phase else list(PHASE_SUBJECTS)
    batch = batch_size or settings.verify.batch_size
    state = load_state()
    corrections: List[Dict[str, Any]] = []
    counters: Counter = Counter()
    halted_reason = ""
    time_reason = ""
    unavailable_reason = ""
    same_model_noted = False
    same_model_note = ""
    run_id = _run_id()
    started_wall = now_iso()
    progress: Optional[PhaseProgress] = None

    session = debate_mod.Debate(settings, router, log=logger)
    taxonomy = read_json(paths.TAXONOMY_JSON, default={"subjects": {}})

    for phase_name in phases:
        subjects = PHASE_SUBJECTS.get(phase_name)
        if not subjects:
            continue
        phase_state = state.setdefault("phases", {}).setdefault(phase_name, {"done": [], "cursor": 0})
        # the monotonic clock is per-process: a checkpoint timer restored from a
        # previous process would be meaningless
        phase_state.pop("last_checkpoint_monotonic", None)
        done = set(phase_state.get("done", []))
        subset = [r for r in records if r.get("subject") in subjects]
        subset.sort(key=lambda r: (str(r.get("exam")), int(r.get("year") or 0), int(r.get("ordinal") or 0), str(r.get("qid"))))

        if limit:
            subset = subset[:limit]

        batches = list(chunked(subset, batch))
        pending = [group for group in batches if batch_fingerprint(group) not in done]
        progress = PhaseProgress(
            phase=phase_name,
            items_total=len(subset),
            items_done_before=sum(len(group) for group in batches if batch_fingerprint(group) in done),
            batches_total=len(batches),
            batches_done_before=len(batches) - len(pending),
            batch_size=batch,
        )
        logger.info(
            f"{phase_name}: {len(subset)} items in {len(batches)} batches "
            f"({progress.batches_done_before} already verified, {len(pending)} pending)"
        )
        vocabulary = _vocabulary_for(taxonomy, subjects)[:200]

        for index, group in enumerate(batches):
            if batch_fingerprint(group) in done:
                counters["batches_skipped"] += 1
                continue
            if window is not None and window.expired():
                time_reason = (
                    f"work window expired after {window.elapsed():.0f}s "
                    f"(limit {window.max_seconds}s) – {len(pending) - progress.batches_this_run} "
                    "batch(es) not attempted"
                )
                counters["batches_not_attempted"] = len(pending) - progress.batches_this_run
                logger.warn(f"{phase_name}: {time_reason}")
                break
            if _router_wide_halt(router):
                halted_reason = router.halt_report().get("*") or router.stats.halt_reason
                break
            down = [role for role in roles if not router.role_available(role)]
            if down:
                unavailable_reason = (
                    "no healthy route for any candidate model of the "
                    + ", ".join(down)
                    + " role(s) mid-run"
                )
                logger.warn(f"{phase_name}: {unavailable_reason} – stopping the AI step")
                break
            fingerprint = batch_fingerprint(group)
            payloads = [_item_payload(r, None) for r in group]
            try:
                outcome = session.run(
                    item_id=fingerprint,
                    task="verify",
                    payload={"batch": payloads},
                    vocabulary=vocabulary,
                )
            except router_mod.GlobalHalt as exc:
                halted_reason = str(exc)
                break
            except llm.LlmError as exc:
                logger.warn(f"{phase_name}: batch {index} failed: {exc}")
                counters["batches_failed"] += 1
                progress.batches_failed += 1
                continue

            counters["batches_verified"] += 1
            progress.batches_this_run += 1
            progress.items_this_run += len(group)
            phase_state["cursor"] = index + 1
            done.add(fingerprint)
            phase_state["done"] = sorted(done)

            if outcome.provenance.get("same_model_fallback"):
                # the run's AI survived by using one model for both roles: say so
                # in PROGRESS.md instead of reporting a two-model agreement
                counters["batches_same_model_fallback"] += 1
                if not same_model_noted:
                    same_model_noted = True
                    same_model_note = (
                        "same_model_fallback: true – proposer and critic were both served by "
                        f"{_provenance_summary(outcome).get('final_author') or 'the same model'}; "
                        "no second model had a working route"
                    )
                    tracking.record_ai(
                        phase_name,
                        {"same_model_fallback": True, "same_model_fallback_at": now_iso()},
                        notes=[same_model_note],
                    )
                    logger.warn(f"{phase_name}: {same_model_note}")

            final = outcome.final if isinstance(outcome.final, dict) else {}
            results = final.get("items") if isinstance(final.get("items"), list) else None
            if results is None and len(group) == 1 and ("ok" in final or "concept" in final):
                # a single-item batch: accept a bare verdict (no ``items`` wrapper)
                results = [dict(final, qid=group[0].get("qid"))]
            if results:
                by_qid = {str(r.get("qid")): r for r in group}
                for item in results:
                    parsed = _parse_verify_reply(item if isinstance(item, dict) else {})
                    if parsed is None:
                        continue
                    qid = str(item.get("qid")) if isinstance(item, dict) else ""
                    target = by_qid.get(qid)
                    if target is None:
                        continue
                    if parsed["ok"]:
                        counters["items_confirmed"] += 1
                        continue
                    counters["items_corrected"] += 1
                    progress.items_corrected += 1
                    correction = {
                        "ts": now_iso(),
                        "phase": phase_name,
                        "qid": qid,
                        "before": {
                            "concept": target.get("concept"),
                            "chapter": target.get("chapter"),
                            "topic": target.get("topic"),
                        },
                        "after": {
                            "concept": parsed["concept"],
                            "chapter": parsed["chapter"],
                            "topic": parsed["topic"],
                        },
                        "reason": parsed["reason"],
                        "provenance": outcome.provenance,
                        "applied": not report_only,
                    }
                    corrections.append(correction)
            else:
                counters["batches_without_items"] += 1

            verdict_line = _provenance_line(phase_name, index, outcome, group)
            logger.info(verdict_line)
            phase_state["last_verdict"] = {
                "batch": index,
                "at": now_iso(),
                "items": len(group),
                "verdict": outcome.verdict,
                "rounds": outcome.rounds,
                "agreed": outcome.agreed,
                "provenance": _provenance_summary(outcome),
            }
            phase_state["progress"] = progress.as_payload(
                status=STATUS_OK, elapsed_total=window.elapsed() if window else 0.0
            )
            save_state(state)

            # the only clock-driven work in the AI step: checkpoint every
            # ``checkpoint_interval_seconds`` (900 s) so a killed job resumes here
            if _should_checkpoint(window, phase_state):
                _checkpoint(
                    phase=phase_name,
                    run_id=run_id,
                    window=window,
                    progress=progress,
                    status=STATUS_OK,
                    batch=index + 1,
                    extra_cursor={"started_wall": started_wall},
                )
                tracking.record_routes(router.provider_report())
                phase_state["last_checkpoint_at"] = now_iso()
                logger.info(
                    f"{phase_name}: checkpoint at batch {index + 1} "
                    f"({progress.items_done}/{progress.items_total} items, "
                    f"ETA {_fmt_eta(progress.eta_seconds())})"
                )

        phase_state["progress"] = progress.as_payload(
            status=STATUS_OK, elapsed_total=window.elapsed() if window else 0.0
        )
        save_state(state)
        tracking.record_ai(
            phase_name,
            progress.as_payload(
                status=STATUS_OK, elapsed_total=window.elapsed() if window else 0.0
            ),
        )
        if halted_reason or time_reason or unavailable_reason:
            break

    corrections_file: Optional[Path] = None
    if corrections:
        append_jsonl(paths.STATE_DIR / "corrections.jsonl", corrections)
        corrections_file = paths.STATE_DIR / "corrections.jsonl"
        if not report_only:
            counters["index_updated"] = apply_corrections(records, corrections)

    save_state(state)
    counters["corrections"] = len(corrections)
    if progress is not None:
        payload = progress.as_payload(
            status=STATUS_OK, elapsed_total=window.elapsed() if window else 0.0
        )
        for key in ("items_total", "items_done", "items_this_run", "items_remaining", "eta_seconds"):
            counters[key] = payload.get(key)
    if halted_reason or unavailable_reason:
        status = STATUS_AI_UNAVAILABLE
    elif time_reason:
        status = STATUS_TIME_LIMIT
    else:
        status = STATUS_OK
    notes: List[str] = []
    if halted_reason:
        notes.append(f"halted (AI unavailable): {halted_reason}")
    if unavailable_reason:
        notes.append(unavailable_reason)
    if same_model_note:
        notes.append(same_model_note)
    if status == STATUS_AI_UNAVAILABLE:
        notes.append(
            "python extraction results were kept unchanged; the deterministic "
            "pipeline (database + mocks) still completes (exit 0)"
        )
    if time_reason:
        notes.append(time_reason)
    routes = router.provider_report()
    tracking.record_routes(routes)
    if progress is not None and status in (STATUS_AI_UNAVAILABLE, STATUS_TIME_LIMIT):
        _checkpoint(
            phase=progress.phase,
            run_id=run_id,
            window=window,
            progress=progress,
            status=status,
            notes=notes,
            extra_cursor={"started_wall": started_wall},
        )
    notes.append(f"router: {json.dumps(router.stats.as_dict(), sort_keys=True)}")
    notes.append(f"routes: {json.dumps(routes, sort_keys=True)}")
    return VerifyResult(status, dict(counters), notes, corrections_file, ai_status=status, routes=routes)


def _provenance_summary(outcome: Any) -> Dict[str, Any]:
    """Which model, on which route, produced the proposer/critic/final answer."""

    prov = outcome.provenance if isinstance(getattr(outcome, "provenance", None), dict) else {}
    summary: Dict[str, Any] = {}
    for role in ("proposer", "critic", "rebuttal", "final"):
        call = prov.get(role)
        if isinstance(call, dict):
            summary[role] = {
                "model": call.get("model"),
                "provider": call.get("provider"),
                "latency_ms": call.get("latency_ms"),
            }
    if prov.get("final_author"):
        summary["final_author"] = prov["final_author"]
    # both opinions came from one model (no second model had a working route)
    summary["same_model_fallback"] = bool(prov.get("same_model_fallback"))
    return summary


def _provenance_line(phase: str, index: int, outcome: Any, group: Sequence[Dict[str, Any]]) -> str:
    """One log line per batch naming both models and the routes that served them."""

    summary = _provenance_summary(outcome)
    proposer = summary.get("proposer") or {}
    critic = summary.get("critic") or {}
    final = summary.get("final") or {}
    return (
        f"{phase}: batch {index} items={len(group)} verdict={outcome.verdict} "
        f"rounds={outcome.rounds} agreed={outcome.agreed} "
        f"proposer={proposer.get('model')}@{proposer.get('provider')} "
        f"critic={critic.get('model')}@{critic.get('provider')}"
        + (
            f" final={final.get('model')}@{final.get('provider')}"
            if final and (final.get("model"), final.get("provider"))
            != (critic.get("model"), critic.get("provider"))
            else ""
        )
        + (" same_model_fallback=true" if summary.get("same_model_fallback") else "")
    )


def _should_checkpoint(window: Optional[WorkWindow], phase_state: Dict[str, Any]) -> bool:
    if window is None:
        return False
    last = phase_state.get("last_checkpoint_monotonic")
    if last is None:
        phase_state["last_checkpoint_monotonic"] = monotonic()
        return False
    return window.should_checkpoint(float(last))


def _record_unavailable(router: "router_mod.Router", window: Optional[WorkWindow], reason: str) -> None:
    """Record an ``ai_unavailable`` run in the checkpoint and PROGRESS.md."""

    tracking.record_routes(router.provider_report())
    stored = ckpt.load_checkpoint() or {}
    cursor = dict(stored.get("cursor") or {})
    cursor["verify"] = {"ai_unavailable": True, "reason": reason[:400]}
    ckpt.save_checkpoint(
        ckpt.new_checkpoint(
            str(stored.get("phase") or "phase1"),
            str(stored.get("run_id") or ckpt.run_id()),
            cursor,
            window,
            status=ckpt.STATUS_AI_UNAVAILABLE,
            notes=[reason],
        ),
        paths.CHECKPOINT_JSON,
    )
    tracking.record_ai(
        str(stored.get("phase") or "phase1"),
        {"status": STATUS_AI_UNAVAILABLE, "items_total": 0, "items_done": 0, "items_this_run": 0},
        status=STATUS_AI_UNAVAILABLE,
        notes=[reason],
    )
    tracking.write_progress_md(extra=None)


def _fmt_eta(seconds: Optional[float]) -> str:
    if seconds is None:
        return "unknown"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def apply_corrections(records: Sequence[Dict[str, Any]], corrections: Sequence[Dict[str, Any]]) -> int:
    """Apply corrections to index records in place; returns the number applied.

    Idempotent: setting the same field values twice has no additional effect, and
    only non-null corrected values overwrite the python result.
    """

    by_qid = {str(r.get("qid")): r for r in records}
    applied = 0
    changed = False
    for correction in corrections:
        record = by_qid.get(str(correction.get("qid")))
        if record is None:
            continue
        after = correction.get("after") or {}
        touched = False
        for field in ("concept", "chapter", "topic"):
            value = after.get(field)
            if value is None:
                continue
            value = str(value).strip()
            if not value:
                continue
            if record.get(field) != value:
                record[field] = value
                touched = True
        if touched:
            record["class_source"] = "ai_verified"
            record["class_confidence"] = "high"
            record["status"] = "placed" if record.get("chapter") else "unclassified"
            applied += 1
            changed = True
    if changed:
        indexer.write_index(records)
    return applied
