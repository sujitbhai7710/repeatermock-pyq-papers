"""Pipeline phases.

Fixed order: ``phase0`` -> ``phase1`` -> ``phase2`` -> ``phase3`` -> ``phase4``
-> ``phase5``.  Each phase follows the same shape:

1. **python extract** – deterministic classification of the questions in its
   slice, driven only by the source JSON plus the taxonomy/alias map;
2. **AI verify** – every extracted item is sent to the two-model debate
   (``agent.debate``) for confirmation or correction;
3. **apply corrections** – accepted corrections are written back;
4. **write DB** – the phase's slice of ``database/`` is (re)generated.

Step 2 is skipped with an explicit note when no API keys are configured, so a
full offline run still produces a complete database.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List

from .. import paths
from ..checkpoint import WorkWindow
from ..classify import Classifier
from ..config import ExamTable, Settings
from ..util import Log

PHASE_ORDER = ("phase0", "phase1", "phase2", "phase3", "phase4", "phase5")

PHASE_TITLES = {
    "phase0": "Distribution & index",
    "phase1": "English",
    "phase2": "GK / GS",
    "phase3": "Maths",
    "phase4": "Reasoning",
    "phase5": "Computer",
}

PHASE_SUBJECTS = {
    "phase0": (),
    "phase1": ("ENG",),
    "phase2": ("GK",),
    "phase3": ("MATH",),
    "phase4": ("REAS",),
    "phase5": ("COMPUTER",),
}


@dataclass
class PhaseResult:
    phase: str
    status: str = "ok"
    counters: Dict[str, Any] = field(default_factory=dict)
    notes: List[str] = field(default_factory=list)
    files: List[Path] = field(default_factory=list)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "phase": self.phase,
            "status": self.status,
            "counters": self.counters,
            "notes": self.notes,
            "files": [paths.rel(p) for p in self.files],
        }


@dataclass
class PipelineContext:
    settings: Settings
    exams: ExamTable
    log: Log
    window: WorkWindow
    run_id: str
    taxonomy: Dict[str, Any]
    alias_map: Dict[str, Any]
    classifier: Classifier
    records: List[Dict[str, Any]] = field(default_factory=list)
    paper_reports: List[Dict[str, Any]] = field(default_factory=list)
    papers: List[Any] = field(default_factory=list)
    out_of_scope: List[Dict[str, Any]] = field(default_factory=list)
    coverage: Dict[str, Any] = field(default_factory=dict)
    distribution: Dict[str, Any] = field(default_factory=dict)
    llm_available: bool = False
    dry_ai: bool = False
    no_ai: bool = False

    def expired(self) -> bool:
        return self.window.expired()

    def subject_records(self, subject: str) -> List[Dict[str, Any]]:
        return [r for r in self.records if r.get("subject") == subject]


def phase_module(name: str):
    from importlib import import_module

    return import_module(f"agent.phases.{name}")
