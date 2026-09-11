"""Progress, journal and manifest bookkeeping.

* ``state/progress.json``  – per-phase status + counters + AI progress + route health
* ``state/journal.jsonl``  – append-only event log
* ``state/manifest.json``  – every file produced per phase with size + sha256 (+ run status)
* ``database/_meta/PROGRESS.md`` – human-readable roll-up of the above

``PROGRESS.md`` is the only window into a 5.5 h CI run, so it carries, per phase:
total items, items done, %, items this run, remaining items and an ETA, plus the
health of every ``(provider, model)`` route and the last checkpoint time.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional

from . import paths
from .util import (
    append_jsonl,
    file_sha256,
    human_int,
    now_iso,
    read_json,
    write_json,
    write_text,
)

PHASES = ("phase0", "phase1", "phase2", "phase3", "phase4", "phase5")

PHASE_TITLES = {
    "phase0": "Distribution & index",
    "phase1": "English (vocabulary, grammar, verbal)",
    "phase2": "GK / GS",
    "phase3": "Maths",
    "phase4": "Reasoning",
    "phase5": "Computer",
    "verify": "AI verification of python-extracted items",
    "taxonomy": "Taxonomy + alias map",
}

def load_progress(path: Optional[Path] = None) -> Dict[str, Any]:
    data = read_json(path or paths.PROGRESS_JSON, default=None)
    if data is None:
        return {
            "version": 1,
            "created_at": now_iso(),
            "updated_at": now_iso(),
            "phases": {},
            "totals": {},
        }
    return data

def save_progress(progress: Dict[str, Any], path: Optional[Path] = None) -> Path:
    progress["updated_at"] = now_iso()
    return write_json(path or paths.PROGRESS_JSON, progress)

def record_phase(
    phase: str,
    status: str,
    counters: Optional[Dict[str, Any]] = None,
    *,
    notes: Optional[List[str]] = None,
    progress: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    data = progress if progress is not None else load_progress()
    entry = data.setdefault("phases", {}).setdefault(phase, {})
    entry.setdefault("started_at", now_iso())
    entry["status"] = status
    entry["updated_at"] = now_iso()
    if counters:
        entry.setdefault("counters", {}).update(counters)
    if notes:
        entry.setdefault("notes", []).extend(notes)
    if status in ("ok", "done", "complete"):
        entry["finished_at"] = now_iso()
    if progress is None:
        save_progress(data)
    return data

def record_ai(
    phase: str,
    payload: Dict[str, Any],
    *,
    status: Optional[str] = None,
    notes: Optional[List[str]] = None,
    progress: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    """Store this phase's AI work-item progress (items total/done/this run/ETA)."""

    data = progress if progress is not None else load_progress()
    entry = data.setdefault("phases", {}).setdefault(phase, {})
    block = entry.setdefault("ai", {})
    block.update(payload)
    block["updated_at"] = now_iso()
    if status:
        entry["ai_status"] = status
    if notes:
        entry.setdefault("notes", []).extend(notes)
    entry["updated_at"] = now_iso()
    if progress is None:
        save_progress(data)
    return data

def record_routes(report: Dict[str, Any], *, progress: Optional[Dict[str, Any]] = None) -> Dict[str, Any]:
    """Store the router's per-route health snapshot for PROGRESS.md."""

    data = progress if progress is not None else load_progress()
    data["routes"] = {"updated_at": now_iso(), "report": report}
    if progress is None:
        save_progress(data)
    return data

def record_status(
    status: str,
    *,
    note: Optional[str] = None,
    progress: Optional[Dict[str, Any]] = None,
    manifest: Optional[Dict[str, Any]] = None,
) -> None:
    """Record the run status in ``progress.json`` and ``manifest.json``."""

    data = progress if progress is not None else load_progress()
    data["status"] = status
    data["status_at"] = now_iso()
    if note:
        data["status_note"] = note
    if progress is None:
        save_progress(data)

    files = manifest if manifest is not None else load_manifest()
    files["status"] = status
    files["status_at"] = data["status_at"]
    if note:
        files["status_note"] = note
    if manifest is None:
        save_manifest(files)

def journal(event: str, payload: Optional[Dict[str, Any]] = None, path: Optional[Path] = None) -> None:
    append_jsonl(
        path or paths.JOURNAL_JSONL,
        [{"ts": now_iso(), "event": event, **(payload or {})}],
    )

# ---------------------------------------------------------------------------
# manifest
# ---------------------------------------------------------------------------

def load_manifest(path: Optional[Path] = None) -> Dict[str, Any]:
    data = read_json(path or paths.MANIFEST_JSON, default=None)
    if data is None:
        return {"version": 1, "updated_at": now_iso(), "phases": {}}
    return data

def record_files(
    phase: str,
    files: Iterable[Path],
    *,
    with_hash: bool = False,
    manifest: Optional[Dict[str, Any]] = None,
) -> Dict[str, Any]:
    data = manifest if manifest is not None else load_manifest()
    bucket = data.setdefault("phases", {}).setdefault(phase, {"files": []})
    known = {f["path"] for f in bucket["files"]}
    for path in files:
        if not path.is_file():
            continue
        rel = paths.rel(path)
        if rel in known:
            continue
        entry = {"path": rel, "bytes": path.stat().st_size}
        if with_hash:
            entry["sha256"] = file_sha256(path)
        bucket["files"].append(entry)
    bucket["files"].sort(key=lambda f: f["path"])
    bucket["count"] = len(bucket["files"])
    bucket["bytes"] = sum(int(f.get("bytes", 0)) for f in bucket["files"])
    data["updated_at"] = now_iso()
    if manifest is None:
        write_json(paths.MANIFEST_JSON, data)
    return data

def save_manifest(manifest: Dict[str, Any], path: Optional[Path] = None) -> Path:
    manifest["updated_at"] = now_iso()
    return write_json(path or paths.MANIFEST_JSON, manifest)

# ---------------------------------------------------------------------------
# PROGRESS.md
# ---------------------------------------------------------------------------

STATUS_NOTES = {
    "ok": "run complete",
    "rate_limited": "every route answered with a rate-limit signal – soft stop, resumes next run",
    "time_limit": "work window expired – soft stop, resumes next run",
    "ai_unavailable": (
        "no AI route available – the deterministic pipeline (phase0 + python extraction + "
        "database + mocks) completed anyway; only the AI verification step was skipped"
    ),
    "error": "genuine code/data error",
}

def _fmt_eta(seconds: Any) -> str:
    try:
        value = float(seconds)
    except (TypeError, ValueError):
        return "-"
    if value < 0:
        return "-"
    if value < 90:
        return f"{value:.0f}s"
    if value < 5400:
        return f"{value / 60:.0f}m"
    return f"{value / 3600:.1f}h"

def _pct(done: int, total: int) -> str:
    if not total:
        return "-"
    return f"{100.0 * done / total:.1f}%"

def _phase_row(phase: str, entry: Dict[str, Any]) -> str:
    """One row of the phase table: status + AI work-item progress."""

    ai = entry.get("ai") if isinstance(entry.get("ai"), dict) else {}
    counters = entry.get("counters") or {}
    total = ai.get("items_total")
    done = ai.get("items_done")
    this_run = ai.get("items_this_run")
    if total is None:
        # phases without AI work still report their deterministic item count
        total = counters.get("questions_in_scope")
        if total is not None and phase == "phase0":
            accounted = (
                (counters.get("placed") or 0)
                + (counters.get("skipped_hindi") or 0)
                + (counters.get("unclassified") or 0)
                + (counters.get("flagged_papers_questions") or 0)
            )
            done = accounted
            this_run = accounted
    total_i = int(total) if isinstance(total, (int, float)) else None
    done_i = int(done) if isinstance(done, (int, float)) else None
    remaining = (total_i - done_i) if (total_i is not None and done_i is not None) else None
    eta = _fmt_eta(ai.get("eta_seconds")) if ai else "-"
    if remaining == 0:
        eta = "0s"
    return (
        f"| `{phase}` | {entry.get('status', '?')} | "
        f"{human_int(total_i) if total_i is not None else '-'} | "
        f"{human_int(done_i) if done_i is not None else '-'} | "
        f"{_pct(done_i or 0, total_i or 0) if total_i is not None else '-'} | "
        f"{human_int(int(this_run)) if isinstance(this_run, (int, float)) else '-'} | "
        f"{human_int(remaining) if remaining is not None else '-'} | "
        f"{eta} | "
        f"{entry.get('updated_at', '-')} |"
    )

def _route_rows(report: Dict[str, Any]) -> List[str]:
    rows: List[str] = []
    for key, entry in sorted(report.items()):
        if not isinstance(entry, dict):
            continue
        state = str(entry.get("state") or "closed")
        if entry.get("healthy") is False and state == "closed":
            state = "unavailable"
        if not entry.get("configured", True) and state == "closed":
            state = "unconfigured"
        cooldown = entry.get("cooldown_seconds")
        retry = entry.get("retry_in_seconds")
        reason = str(entry.get("last_reason") or entry.get("why") or "-")
        rows.append(
            f"| `{key}` | {state} | {int(entry.get('ok', 0))} | {int(entry.get('fail', 0))} | "
            f"{int(entry.get('rate_limited', 0))} | {int(entry.get('trips', 0))} | "
            f"{_fmt_eta(cooldown) if cooldown else '-'} | "
            f"{(_fmt_eta(retry) if retry else '-')} | {reason[:90]} |"
        )
    return rows

def render_progress_md(
    progress: Dict[str, Any],
    manifest: Dict[str, Any],
    checkpoint: Optional[Dict[str, Any]] = None,
    extra: Optional[Dict[str, Any]] = None,
) -> str:
    lines: List[str] = []
    add = lines.append
    add("# PYQ Agent — Progress")
    add("")
    add(f"_generated {now_iso()}_")
    add("")

    status = progress.get("status") or (checkpoint or {}).get("status")
    add("## Run status")
    add("")
    if status:
        add(f"- status: **`{status}`**")
        note = progress.get("status_note") or STATUS_NOTES.get(str(status))
        if note:
            add(f"- meaning: {note}")
        if progress.get("status_at"):
            add(f"- recorded: {progress['status_at']}")
    else:
        add("- status: `in progress`")
    routes = progress.get("routes") or {}
    add(f"- route health last updated: {routes.get('updated_at', '-')}")
    add("")

    if checkpoint:
        add("## Last checkpoint")
        add("")
        add(f"- run id: `{checkpoint.get('run_id')}`")
        add(f"- phase: `{checkpoint.get('phase')}`")
        add(f"- status: `{checkpoint.get('status')}`")
        add(f"- last checkpoint at: {checkpoint.get('updated_at')}")
        window = checkpoint.get("window") or {}
        if window:
            add(
                f"- work window: elapsed {window.get('elapsed_seconds')}s of "
                f"{window.get('max_seconds')}s (checkpoint every "
                f"{window.get('interval_seconds')}s)"
            )
        cursor = checkpoint.get("cursor") or {}
        if cursor:
            add(f"- cursor: `{cursor}`")
        add("")

    add("## Phases (work items)")
    add("")
    add("| Phase | Status | Items | Done | % | This run | Remaining | ETA | Updated |")
    add("|---|---|---:|---:|---:|---:|---:|---:|---|")
    phases = progress.get("phases", {})
    for phase in PHASES:
        entry = phases.get(phase) or {"status": "pending"}
        add(_phase_row(phase, entry))
    add("")
    add("_Items = questions the phase must cover (AI verification for phases 1-5, "
        "extracted questions for phase0); ETA extrapolates this run's throughput._")
    add("")

    notes: List[str] = []
    for phase in PHASES:
        for note in (phases.get(phase) or {}).get("notes", []) or []:
            notes.append(f"- `{phase}`: {note}")
    if notes:
        add("## Notes")
        add("")
        lines.extend(notes[-20:])
        add("")

    route_report = routes.get("report") or {}
    if route_report:
        add(f"## Provider health (per provider + model, {routes.get('updated_at', '-')})")
        add("")
        add("| Route | State | OK | Fail | Rate-limited | Trips | Cooldown | Retry in | Last reason |")
        add("|---|---|---:|---:|---:|---:|---:|---:|---|")
        lines.extend(_route_rows(route_report))
        add("")

    if extra:
        add("## Coverage")
        add("")
        for key, value in extra.items():
            add(f"- {key}: {value}")
        add("")

    add("## Artifacts per phase")
    add("")
    add("| Phase | Files | Bytes |")
    add("|---|---:|---:|")
    for phase, bucket in sorted(manifest.get("phases", {}).items()):
        add(
            f"| `{phase}` | {human_int(int(bucket.get('count', 0)))} | "
            f"{human_int(int(bucket.get('bytes', 0)))} |"
        )
    add("")
    return "\n".join(lines)

def write_progress_md(
    progress: Optional[Dict[str, Any]] = None,
    manifest: Optional[Dict[str, Any]] = None,
    checkpoint: Optional[Dict[str, Any]] = None,
    *,
    extra: Optional[Dict[str, Any]] = None,
    path: Optional[Path] = None,
) -> Path:
    target = path or (paths.META_DB_DIR / "PROGRESS.md")
    text = render_progress_md(
        progress if progress is not None else load_progress(),
        manifest if manifest is not None else load_manifest(),
        checkpoint if checkpoint is not None else __import__("agent.checkpoint", fromlist=["load_checkpoint"]).load_checkpoint(),
        extra=extra,
    )
    return write_text(target, text)
