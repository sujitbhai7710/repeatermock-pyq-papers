"""Phase 5 – Computer Knowledge.

Follows the shared four-step shape (python extract -> AI verify -> apply
corrections -> write DB) implemented in :mod:`agent.phases.subject_phase`.
"""

from __future__ import annotations

from typing import Any

from . import PhaseResult
from .subject_phase import run_subject_phase

SUBJECT = "COMPUTER"
PHASE = "phase5"
TITLE = "Computer Knowledge"


def run(ctx: Any) -> PhaseResult:
    return run_subject_phase(ctx, PHASE, SUBJECT)
