"""Progress, journal and manifest bookkeeping.

* ``state/progress.json``  – per-phase status + counters (rewritten atomically)
* ``state/journal.jsonl``  – append-only event log
* ``state/manifest.json``  – every file produced per phase with size + sha256
* ``database/_meta/PROGRESS.md`` – human-readable roll-up of the above
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

    if checkpoint:
        add("## Last checkpoint")
        add("")
        add(f"- run id: `{checkpoint.get('run_id')}`")
        add(f"- phase: `{checkpoint.get('phase')}`")
        add(f"- status: `{checkpoint.get('status')}`")
        add(f"- updated: {checkpoint.get('updated_at')}")
        cursor = checkpoint.get("cursor") or {}
        if cursor:
            add(f"- cursor: `{cursor}`")
        add("")

    add("## Phases")
    add("")
    add("| Phase | Title | Status | Updated | Counters |")
    add("|---|---|---|---|---|")
    phases = progress.get("phases", {})
    for phase in PHASES:
        entry = phases.get(phase)
        title = PHASE_TITLES.get(phase, phase)
        if not entry:
            add(f"| `{phase}` | {title} | pending | - | - |")
            continue
        counters = entry.get("counters") or {}
        counter_text = ", ".join(f"{k}={v}" for k, v in sorted(counters.items())) or "-"
        add(
            f"| `{phase}` | {title} | {entry.get('status', '?')} | "
            f"{entry.get('updated_at', '-')} | {counter_text} |"
        )
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
