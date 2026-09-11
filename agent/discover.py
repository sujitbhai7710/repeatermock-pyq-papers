"""Discovery: walk the paper tree, apply the year filter, yield normalised records.

Year rule (as specified): the deepest folder segment that matches ``20\\d\\d``
wins, and the first match inside that segment is the year; if no folder matches,
the first ``20\\d\\d`` in ``title`` is used.  Folder names such as
``2019_-_2020`` therefore resolve to 2019 and ``2022-23`` falls through to the
title.  This rule reproduces the expected corpus exactly: 1,322 papers /
142,090 questions inside 2019-2025.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterator, List, Optional, Tuple

from . import paths
from .config import ExamTable, Settings
from .util import file_sha256, norm_space

YEAR_RE = re.compile(r"20\d\d")
SHIFT_RE = re.compile(r"shift[\s\-_]*(\d+)", re.I)
SET_RE = re.compile(r"\bset[\s\-_]*([0-9]+)\b", re.I)
HELD_ON_RE = re.compile(
    r"held\s*on\s*:?\s*([0-9]{1,2})\s*"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s*([0-9]{4})",
    re.I,
)
DATE_DMY_RE = re.compile(
    r"([0-9]{1,2})\s*(?:st|nd|rd|th)?\s+"
    r"(jan|feb|mar|apr|may|jun|jul|aug|sep|oct|nov|dec)[a-z]*\.?,?\s*([0-9]{4})",
    re.I,
)
MONTHS = {
    "jan": 1, "feb": 2, "mar": 3, "apr": 4, "may": 5, "jun": 6,
    "jul": 7, "aug": 8, "sep": 9, "oct": 10, "nov": 11, "dec": 12,
}


@dataclass
class PaperRecord:
    path: str
    abs_path: Path
    test_id: str
    title: str
    exam: str
    exam_label: str
    kind_id: str
    kind_label: str
    year: int
    year_source: str
    section: str
    subsection: str
    source_subject: str
    duration_min: Any
    max_marks: Any
    declared_question_count: Any
    languages: List[str]
    scraped_at: str
    shift: Optional[int]
    set_index: Optional[int]
    held_on: str
    held_on_iso: str
    questions: List[Dict[str, Any]] = field(default_factory=list)
    sha256: str = ""

    @property
    def length(self) -> int:
        return len(self.questions)

    @property
    def shift_key(self) -> str:
        return f"Shift {self.shift}" if self.shift else "NA"

    def as_dict(self, *, with_questions: bool = False) -> Dict[str, Any]:
        data: Dict[str, Any] = {
            "path": self.path,
            "test_id": self.test_id,
            "title": self.title,
            "exam": self.exam,
            "exam_label": self.exam_label,
            "paper_kind": self.kind_id,
            "paper_kind_label": self.kind_label,
            "year": self.year,
            "year_source": self.year_source,
            "section": self.section,
            "subsection": self.subsection,
            "source_subject": self.source_subject,
            "duration_min": self.duration_min,
            "max_marks": self.max_marks,
            "declared_question_count": self.declared_question_count,
            "languages": self.languages,
            "scraped_at": self.scraped_at,
            "shift": self.shift,
            "set_index": self.set_index,
            "held_on": self.held_on,
            "held_on_iso": self.held_on_iso,
            "question_count": self.length,
            "sha256": self.sha256,
        }
        if with_questions:
            data["questions"] = self.questions
        return data


def detect_year(folder_parts: List[str], title: str) -> Tuple[Optional[int], str]:
    for segment in reversed(folder_parts):
        match = YEAR_RE.search(segment)
        if match:
            return int(match.group(0)), f"folder:{segment}"
    match = YEAR_RE.search(title or "")
    if match:
        return int(match.group(0)), "title"
    return None, "none"


def parse_held_on(title: str) -> Tuple[str, str]:
    match = HELD_ON_RE.search(title or "")
    if not match:
        return "", ""
    day, month, year = match.group(1), MONTHS[match.group(2).lower()], match.group(3)
    return match.group(0), f"{year}-{month:02d}-{int(day):02d}"


def iter_paper_paths(root: Optional[Path] = None) -> Iterator[Path]:
    """Yield every ``*.json`` under the project root, skipping generated trees."""

    base = Path(root) if root is not None else paths.PAPERS_ROOT
    skip_dirs = {
        ".git",
        "state",
        "database",
        "reports",
        "node_modules",
        "__pycache__",
        ".github",
        "docs",
        ".agents",
    }
    for dirpath, dirnames, filenames in os.walk(base):
        dirnames[:] = sorted(d for d in dirnames if d not in skip_dirs and not d.startswith("."))
        for name in sorted(filenames):
            if name.lower().endswith(".json"):
                yield Path(dirpath) / name


def load_paper(path: Path, exam_table: ExamTable) -> Optional[PaperRecord]:
    try:
        with path.open("r", encoding="utf-8") as fh:
            raw = json.load(fh)
    except (OSError, json.JSONDecodeError):
        return None
    if not isinstance(raw, dict) or "questions" not in raw:
        return None

    rel = paths.rel(path)
    folder_parts = rel.split("/")[:-1]
    title = norm_space(raw.get("title"))
    year, year_source = detect_year(folder_parts, title)
    exam, kind = exam_table.kind_for_path(rel)

    shift_match = SHIFT_RE.search(title)
    set_match = SET_RE.search(title)
    held_on, held_on_iso = parse_held_on(title)

    return PaperRecord(
        path=rel,
        abs_path=path,
        test_id=str(raw.get("test_id", "")),
        title=title,
        exam=exam.code if exam else (folder_parts[0] if folder_parts else "UNKNOWN"),
        exam_label=exam.label if exam else "Unknown exam",
        kind_id=kind.id if kind else "unknown",
        kind_label=kind.label if kind else "unknown",
        year=int(year) if year is not None else -1,
        year_source=year_source,
        section=norm_space(raw.get("section")),
        subsection=norm_space(raw.get("subsection")),
        source_subject=norm_space(raw.get("subject")),
        duration_min=raw.get("duration_min"),
        max_marks=raw.get("max_marks"),
        declared_question_count=raw.get("question_count"),
        languages=list(raw.get("languages") or []),
        scraped_at=norm_space(raw.get("scraped_at")),
        shift=int(shift_match.group(1)) if shift_match else None,
        set_index=int(set_match.group(1)) if set_match else None,
        held_on=held_on,
        held_on_iso=held_on_iso,
        questions=list(raw.get("questions") or []),
    )


def discover(
    settings: Settings,
    exam_table: ExamTable,
    *,
    root: Optional[Path] = None,
    with_hashes: bool = False,
) -> Tuple[List[PaperRecord], List[Dict[str, Any]]]:
    """Return ``(in_scope_papers, out_of_scope_info)``.

    Out-of-scope papers are reported (path + detected year) so the run log shows
    exactly what was dropped and why.
    """

    in_scope: List[PaperRecord] = []
    skipped: List[Dict[str, Any]] = []
    for path in iter_paper_paths(root):
        record = load_paper(path, exam_table)
        if record is None:
            continue
        if not settings.year_range.contains(record.year):
            skipped.append(
                {
                    "path": record.path,
                    "year": record.year if record.year >= 0 else None,
                    "year_source": record.year_source,
                    "reason": "year_out_of_range",
                }
            )
            continue
        if with_hashes:
            record.sha256 = file_sha256(path)
        in_scope.append(record)

    in_scope.sort(key=lambda r: (r.exam, r.year, r.shift or 0, r.path))
    skipped.sort(key=lambda r: r["path"])
    return in_scope, skipped


def in_scope_stats(papers: List[PaperRecord]) -> Dict[str, Any]:
    by_exam: Dict[str, Dict[str, int]] = {}
    total = 0
    for paper in papers:
        bucket = by_exam.setdefault(paper.exam, {"papers": 0, "questions": 0})
        bucket["papers"] += 1
        bucket["questions"] += paper.length
        total += paper.length
    return {"papers": len(papers), "questions": total, "by_exam": by_exam}
