#!/usr/bin/env python
"""Print the CI job-summary status block (stdout, markdown).

Usage
-----
::

    python -m tools.ci_summary [--state-dir state] [--no-routes]

Reads ``state/checkpoint.json`` and ``state/progress.json`` and prints the parts a
reviewer looks at first:

* the run status (``ok`` / ``ai_unavailable`` / ``rate_limited`` / ``time_limit``)
  with the reason it was recorded;
* the last checkpoint time and the work window (``CHECKPOINT_INTERVAL_SECONDS``);
* per phase: items total / done / % / this run / remaining / ETA;
* per ``(provider, model)`` route: ok / fail / rate-limited / cooldown state.

Everything is read-only: the job summary must work even for a run that died
mid-phase.  Exit codes: ``0`` ok (also when the state is missing), ``2`` usage.
"""

from __future__ import annotations

import argparse
import json
import sys
from pathlib import Path
from typing import Any, Dict, List, Optional

STATUS_MEANING = {
    "ok": "run complete",
    "ai_unavailable": "no AI route available – deterministic pipeline still completed (exit 0)",
    "rate_limited": "every route answered with a rate-limit signal – soft stop, resumes next run",
    "time_limit": "work window expired – soft stop, resumes next run",
    "error": "genuine code/data error",
}

PHASES = ("phase0", "phase1", "phase2", "phase3", "phase4", "phase5")


def _read_json(path: Path) -> Dict[str, Any]:
    try:
        return json.loads(path.read_text(encoding="utf-8"))
    except (OSError, json.JSONDecodeError):
        return {}


def _human(value: Any) -> str:
    if isinstance(value, (int, float)):
        return f"{int(value):,}"
    return "-"


def _eta(seconds: Any) -> str:
    if not isinstance(seconds, (int, float)):
        return "-"
    if seconds < 90:
        return f"{seconds:.0f}s"
    if seconds < 5400:
        return f"{seconds / 60:.0f}m"
    return f"{seconds / 3600:.1f}h"


def _pct(done: Any, total: Any) -> str:
    if not isinstance(total, (int, float)) or not total:
        return "-"
    if not isinstance(done, (int, float)):
        return "-"
    return f"{100.0 * done / total:.1f}%"


def render(state_dir: Path, *, include_routes: bool = True) -> str:
    checkpoint = _read_json(state_dir / "checkpoint.json")
    progress = _read_json(state_dir / "progress.json")
    routes_block = progress.get("routes") or {}
    routes = routes_block.get("report") or {}

    lines: List[str] = []
    status = progress.get("status") or checkpoint.get("status")
    if status:
        lines.append(f"- status: **`{status}`** — {STATUS_MEANING.get(str(status), '')}".rstrip(" —"))
    else:
        lines.append("- status: `in progress`")

    if checkpoint:
        window = checkpoint.get("window") or {}
        lines.append(
            f"- last checkpoint: {checkpoint.get('updated_at', '-')} "
            f"(phase `{checkpoint.get('phase', '-')}`, run `{checkpoint.get('run_id', '-')}`)"
        )
        if window:
            lines.append(
                f"- work window: elapsed {window.get('elapsed_seconds', '-')}s of "
                f"{window.get('max_seconds', '-')}s, checkpoint every "
                f"{window.get('interval_seconds', '-')}s"
            )
    if progress.get("status_note"):
        lines.append(f"- note: {progress['status_note']}")
    if routes_block.get("updated_at"):
        lines.append(f"- route health last updated: {routes_block['updated_at']}")
    lines.append("")

    lines.append("| Phase | Status | AI status | Items | Done | % | This run | Remaining | ETA |")
    lines.append("|---|---|---|---:|---:|---:|---:|---:|---:|")
    phases = progress.get("phases") or {}
    for phase in PHASES:
        entry = phases.get(phase) or {}
        ai = entry.get("ai") or {}
        total = ai.get("items_total")
        done = ai.get("items_done")
        lines.append(
            f"| `{phase}` | {entry.get('status', 'pending')} | {entry.get('ai_status', '-')} | "
            f"{_human(total)} | {_human(done)} | {_pct(done, total)} | "
            f"{_human(ai.get('items_this_run'))} | {_human(ai.get('items_remaining'))} | "
            f"{_eta(ai.get('eta_seconds'))} |"
        )
    lines.append("")

    if include_routes and routes:
        lines.append("| Route | State | OK | Fail | Rate-limited | Cooldown |")
        lines.append("|---|---|---:|---:|---:|---:|")
        for key, entry in sorted(routes.items()):
            if not isinstance(entry, dict):
                continue
            state = str(entry.get("state") or "closed")
            if entry.get("healthy") is False and state == "closed":
                state = "unavailable"
            if entry.get("configured") is False and state == "closed":
                state = "unconfigured"
            lines.append(
                f"| `{key}` | {state} | {_human(entry.get('ok', 0))} | "
                f"{_human(entry.get('fail', 0))} | {_human(entry.get('rate_limited', 0))} | "
                f"{_eta(entry.get('cooldown_seconds'))} |"
            )
        lines.append("")
    return "\n".join(lines)


def main(argv: Optional[List[str]] = None) -> int:
    parser = argparse.ArgumentParser(prog="python -m tools.ci_summary")
    parser.add_argument("--state-dir", default=None, help="default: <repo>/state")
    parser.add_argument("--no-routes", action="store_true", help="omit the route table")
    args = parser.parse_args(argv)

    if args.state_dir:
        state_dir = Path(args.state_dir)
    else:
        # import lazily so the tool works from any cwd inside the repo
        sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
        from agent import paths  # noqa: WPS433

        state_dir = paths.STATE_DIR

    sys.stdout.write(render(state_dir, include_routes=not args.no_routes))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
