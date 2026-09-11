"""Checkpointing and the work window.

A run is bounded by ``MAX_WORK_SECONDS`` (default 19,800 s = 5.5 h) and writes a
checkpoint every ``CHECKPOINT_INTERVAL_SECONDS`` (default 900 s = 15 min).  All
writes go through :mod:`agent.util` (temp file + ``os.replace``) so a killed run
never leaves a half-written JSON file behind.

Statuses
--------
``ok``
    the phase / run finished.
``time_limit``
    the work window expired — soft stop, exit code 4, resume from the checkpoint.
``rate_limited``
    every route answered with a rate-limit signal — soft stop, exit code 3.
``ai_unavailable``
    no route could serve a model (all cooling down / failing / unkeyed).  The
    deterministic pipeline still runs to completion, so this is a **success**
    (exit code 0) that merely records that the AI step was skipped.
``error``
    a genuine code/data failure.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional

from . import paths
from .config import Settings
from .util import monotonic, now_iso, read_json, write_json


@dataclass
class WorkWindow:
    """Tracks elapsed time and decides when to stop / checkpoint."""

    max_seconds: int
    interval_seconds: int
    started: float = field(default_factory=monotonic)

    def elapsed(self) -> float:
        return monotonic() - self.started

    def remaining(self) -> float:
        return max(0.0, self.max_seconds - self.elapsed())

    def expired(self) -> bool:
        return self.elapsed() >= self.max_seconds

    def should_checkpoint(self, last_checkpoint_at: float) -> bool:
        return (monotonic() - last_checkpoint_at) >= self.interval_seconds

    def as_dict(self) -> Dict[str, Any]:
        return {
            "max_seconds": self.max_seconds,
            "interval_seconds": self.interval_seconds,
            "elapsed_seconds": round(self.elapsed(), 3),
            "remaining_seconds": round(self.remaining(), 3),
        }


STATUS_OK = "ok"
STATUS_RATE_LIMITED = "rate_limited"
STATUS_TIME_LIMIT = "time_limit"
STATUS_AI_UNAVAILABLE = "ai_unavailable"
STATUS_ERROR = "error"

#: statuses that are a *soft stop*: the run checkpointed and published its work
SOFT_STATUSES = (STATUS_OK, STATUS_RATE_LIMITED, STATUS_TIME_LIMIT, STATUS_AI_UNAVAILABLE)

PHASES = ("phase0", "phase1", "phase2", "phase3", "phase4", "phase5")


@dataclass
class Checkpoint:
    run_id: str
    phase: str
    cursor: Dict[str, Any]
    status: str
    started_at: str
    updated_at: str
    window: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "version": 1,
            "run_id": self.run_id,
            "phase": self.phase,
            "cursor": self.cursor,
            "status": self.status,
            "started_at": self.started_at,
            "updated_at": self.updated_at,
            "window": self.window,
            "notes": self.notes,
        }


def load_checkpoint(path: Optional[Path] = None) -> Optional[Dict[str, Any]]:
    return read_json(path or paths.CHECKPOINT_JSON, default=None)


def save_checkpoint(checkpoint: Checkpoint, path: Optional[Path] = None) -> Path:
    checkpoint.updated_at = now_iso()
    return write_json(path or paths.CHECKPOINT_JSON, checkpoint.as_dict())


def new_checkpoint(
    phase: str,
    run_id: str,
    cursor: Optional[Dict[str, Any]] = None,
    window: Optional[WorkWindow] = None,
    status: str = STATUS_OK,
    notes: Optional[List[str]] = None,
) -> Checkpoint:
    stamp = now_iso()
    return Checkpoint(
        run_id=run_id,
        phase=phase,
        cursor=cursor or {},
        status=status,
        started_at=stamp,
        updated_at=stamp,
        window=window.as_dict() if window else {},
        notes=list(notes or []),
    )


def make_window(settings: Settings) -> WorkWindow:
    return WorkWindow(
        max_seconds=settings.work_window_seconds,
        interval_seconds=settings.checkpoint_interval_seconds,
    )


def run_id() -> str:
    import os
    import uuid

    explicit = os.environ.get("PYQ_RUN_ID")
    if explicit:
        return explicit
    return f"run-{now_iso().replace(':', '').replace('-', '')}-{uuid.uuid4().hex[:8]}"
