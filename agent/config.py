"""Configuration loading.

``config/settings.json`` holds runtime settings, ``config/exams.json`` holds the
per-exam paper layout table.  A handful of environment variables override the
file values so the GitHub Action can steer a run without editing the repo.
"""

from __future__ import annotations

import json
import os
import re
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, List, Optional, Tuple

from . import paths


def _read_json(path: Path) -> Dict[str, Any]:
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def _env_int(name: str) -> Optional[int]:
    raw = os.environ.get(name)
    if raw is None or raw.strip() == "":
        return None
    try:
        return int(float(raw.strip()))
    except ValueError:
        return None


@dataclass(frozen=True)
class YearRange:
    min: int
    max: int

    def contains(self, year: Optional[int]) -> bool:
        return year is not None and self.min <= year <= self.max


@dataclass(frozen=True)
class RateLimitPolicy:
    consecutive_failure_threshold: int = 10
    #: when true (default) a rate-limit/exhaustion signal trips the *provider's*
    #: circuit breaker and the request fails over; a global halt only happens
    #: once no healthy provider is left (or the consecutive-failure threshold is
    #: reached).  When false the provider stays in rotation and only the
    #: consecutive-failure counter applies.
    halt_on_any_rate_limit_signal: bool = True
    request_timeout_seconds: int = 120
    max_retries_per_request: int = 1
    #: circuit-breaker cooldown for an exhausted provider (doubles per
    #: consecutive trip, capped by ``provider_cooldown_max_seconds``)
    provider_cooldown_seconds: int = 300
    provider_cooldown_max_seconds: int = 3600


@dataclass(frozen=True)
class DebatePolicy:
    max_rounds: int = 1
    proposer_model: str = "deepseek-v4-flash"
    critic_model: str = "gpt-5.6-sol"
    proposer_provider_order: Tuple[str, ...] = ("agentrouter", "jw")
    critic_provider_order: Tuple[str, ...] = ("agentrouter", "jw")


@dataclass(frozen=True)
class VerifyPolicy:
    batch_size: int = 20
    enabled: bool = True
    require_keys: bool = True
    max_items_per_run: int = 0


@dataclass(frozen=True)
class HindiPolicy:
    regex: str = r"[\u0900-\u097F]"
    scope: Tuple[str, ...] = ("question", "options")
    include_solution: bool = False
    exclude_punctuation: bool = False


@dataclass(frozen=True)
class SignaturePolicy:
    min_agreement: float = 0.95
    #: primary check: share of declared sections whose modal keyword subject
    #: equals the declared subject (see agent.sections.validate_sections)
    min_section_agreement: float = 0.95
    allow_detected_layout_fallback: bool = True


@dataclass(frozen=True)
class Settings:
    year_range: YearRange
    work_window_seconds: int
    checkpoint_interval_seconds: int
    rate_limit: RateLimitPolicy
    debate: DebatePolicy
    verify: VerifyPolicy
    hindi: HindiPolicy
    signature: SignaturePolicy
    vocab: Dict[str, Any] = field(default_factory=dict)
    paths_cfg: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    # convenience -----------------------------------------------------------
    @property
    def max_work_seconds(self) -> int:
        return self.work_window_seconds


def load_settings(path: Optional[Path] = None) -> Settings:
    path = path or paths.SETTINGS_FILE
    raw = _read_json(path) if path.is_file() else {}

    yr = raw.get("year_range", {})
    year_range = YearRange(int(yr.get("min", 2019)), int(yr.get("max", 2025)))

    work_window = int(raw.get("work_window_seconds", 19800))
    env_window = _env_int("MAX_WORK_SECONDS")
    if env_window is not None:
        work_window = env_window

    checkpoint_interval = int(raw.get("checkpoint_interval_seconds", 1800))
    env_interval = _env_int("CHECKPOINT_INTERVAL_SECONDS")
    if env_interval is not None:
        checkpoint_interval = env_interval

    rl = raw.get("rate_limit", {})
    threshold = int(rl.get("consecutive_failure_threshold", 10))
    env_threshold = _env_int("RATE_LIMIT_THRESHOLD")
    if env_threshold is not None:
        threshold = env_threshold
    cooldown = int(rl.get("provider_cooldown_seconds", 300))
    env_cooldown = _env_int("PROVIDER_COOLDOWN_SECONDS")
    if env_cooldown is not None:
        cooldown = env_cooldown
    cooldown_max = int(rl.get("provider_cooldown_max_seconds", 3600))
    env_cooldown_max = _env_int("PROVIDER_COOLDOWN_MAX_SECONDS")
    if env_cooldown_max is not None:
        cooldown_max = env_cooldown_max
    rate_limit = RateLimitPolicy(
        consecutive_failure_threshold=threshold,
        halt_on_any_rate_limit_signal=bool(rl.get("halt_on_any_rate_limit_signal", True)),
        request_timeout_seconds=int(rl.get("request_timeout_seconds", 120)),
        max_retries_per_request=int(rl.get("max_retries_per_request", 1)),
        provider_cooldown_seconds=cooldown,
        provider_cooldown_max_seconds=cooldown_max,
    )

    db = raw.get("debate", {})
    debate = DebatePolicy(
        max_rounds=int(db.get("max_rounds", 1)),
        proposer_model=str(db.get("proposer_model", "deepseek-v4-flash")),
        critic_model=str(db.get("critic_model", "gpt-5.6-sol")),
        proposer_provider_order=tuple(db.get("proposer_provider_order", ["agentrouter", "jw"])),
        critic_provider_order=tuple(db.get("critic_provider_order", ["agentrouter", "jw"])),
    )

    vf = raw.get("verify", {})
    verify = VerifyPolicy(
        batch_size=int(vf.get("batch_size", 20)),
        enabled=bool(vf.get("enabled", True)),
        require_keys=bool(vf.get("require_keys", True)),
        max_items_per_run=int(vf.get("max_items_per_run", 0)),
    )

    hi = raw.get("hindi", {})
    hindi = HindiPolicy(
        regex=str(hi.get("regex", r"[\u0900-\u097F]")),
        scope=tuple(hi.get("scope", ["question", "options"])),
        include_solution=bool(hi.get("include_solution", False)),
        exclude_punctuation=bool(hi.get("exclude_punctuation", False)),
    )

    sg = raw.get("signature", {})
    signature = SignaturePolicy(
        min_agreement=float(sg.get("min_agreement", 0.95)),
        min_section_agreement=float(sg.get("min_section_agreement", 0.95)),
        allow_detected_layout_fallback=bool(sg.get("allow_detected_layout_fallback", True)),
    )

    return Settings(
        year_range=year_range,
        work_window_seconds=work_window,
        checkpoint_interval_seconds=checkpoint_interval,
        rate_limit=rate_limit,
        debate=debate,
        verify=verify,
        hindi=hindi,
        signature=signature,
        vocab=raw.get("vocab", {}),
        paths_cfg=raw.get("paths", {}),
        raw=raw,
    )


# ---------------------------------------------------------------------------
# exams / paper layouts
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class LayoutSpan:
    subject: str
    start: int
    end: int

    def contains(self, ordinal: int) -> bool:
        return self.start <= ordinal <= self.end


@dataclass(frozen=True)
class LayoutRule:
    """One row of the layout table, optionally filtered by year and/or length."""

    years: Optional[frozenset]
    lengths: Optional[frozenset]
    spans: Tuple[LayoutSpan, ...]

    def matches(self, year: Optional[int], length: Optional[int]) -> bool:
        if self.years is not None and (year is None or year not in self.years):
            return False
        if self.lengths is not None and (length is None or length not in self.lengths):
            return False
        return True


@dataclass(frozen=True)
class PaperKind:
    id: str
    label: str
    path_includes: Tuple[str, ...]
    path_excludes: Tuple[str, ...]
    rules: Tuple[LayoutRule, ...]
    detect: bool
    exam: str
    path_includes_longest: int = 0

    def spans_for(self, length: int, year: Optional[int] = None) -> Optional[Tuple[LayoutSpan, ...]]:
        """First matching layout rule, or ``None`` when the kind is detect-only."""

        for rule in self.rules:
            if rule.matches(year, length):
                return rule.spans
        return None

    def expected_lengths(self) -> List[int]:
        lengths: set = set()
        for rule in self.rules:
            if rule.lengths:
                lengths.update(rule.lengths)
            elif rule.spans:
                lengths.add(max(s.end for s in rule.spans))
        return sorted(lengths)


@dataclass(frozen=True)
class Exam:
    code: str
    label: str
    root: str
    series_slug: str
    kinds: Tuple[PaperKind, ...]


def _spans(items: Any) -> Tuple[LayoutSpan, ...]:
    out: List[LayoutSpan] = []
    for entry in items or []:
        subject, start, end = entry[0], int(entry[1]), int(entry[2])
        out.append(LayoutSpan(subject=subject, start=start, end=end))
    return tuple(out)


class ExamTable:
    """Loaded ``config/exams.json`` with path -> paper kind resolution."""

    def __init__(self, data: Dict[str, Any]) -> None:
        self.raw = data
        self.subjects: Tuple[str, ...] = tuple(data.get("subjects", []))
        self.subject_labels: Dict[str, str] = dict(data.get("subject_labels", {}))
        exams: Dict[str, Exam] = {}
        for code, body in (data.get("exams") or {}).items():
            kinds: List[PaperKind] = []
            for kind in body.get("paper_kinds", []):
                rules: List[LayoutRule] = []
                for rule in kind.get("layout_rules", []):
                    years = rule.get("years")
                    lengths = rule.get("length")
                    rules.append(
                        LayoutRule(
                            years=frozenset(int(y) for y in years) if years else None,
                            lengths=frozenset(int(x) for x in ([lengths] if isinstance(lengths, int) else (lengths or []))) or None,
                            spans=_spans(rule.get("layout")),
                        )
                    )
                includes = tuple(kind.get("path_includes", []))
                kinds.append(
                    PaperKind(
                        id=kind["id"],
                        label=kind.get("label", kind["id"]),
                        path_includes=includes,
                        path_excludes=tuple(kind.get("path_excludes", [])),
                        rules=tuple(rules),
                        detect=bool(kind.get("detect", False)),
                        exam=code,
                        path_includes_longest=max((len(i) for i in includes), default=0),
                    )
                )
            exams[code] = Exam(
                code=code,
                label=body.get("label", code),
                root=body["root"],
                series_slug=body.get("series_slug", ""),
                kinds=tuple(kinds),
            )
        self.exams = exams
        self.provenance = dict(data.get("provenance", {}))

    # -- lookups -----------------------------------------------------------
    def exam_for_root(self, root_name: str) -> Optional[Exam]:
        for exam in self.exams.values():
            if exam.root == root_name:
                return exam
        return None

    def exam_codes(self) -> Tuple[str, ...]:
        return tuple(sorted(self.exams))

    def label(self, subject: str) -> str:
        return self.subject_labels.get(subject, subject)

    def kind_for_path(self, path: str) -> Tuple[Optional[Exam], Optional[PaperKind]]:
        """Resolve ``SSC-XXX/folder/.../file.json`` to (exam, paper kind).

        The longest matching ``path_includes`` entry wins so that more specific
        kinds (``English_`` before generic Tier-II) are picked correctly.
        """

        normalised = path.replace("\\", "/")
        parts = normalised.split("/")
        if not parts:
            return None, None
        exam = self.exam_for_root(parts[0])
        if exam is None:
            return None, None

        best: Optional[PaperKind] = None
        best_score = -1
        for kind in exam.kinds:
            if any(excl in normalised for excl in kind.path_excludes):
                continue
            if not all(inc in normalised for inc in kind.path_includes):
                continue
            if kind.path_includes_longest > best_score:
                best, best_score = kind, kind.path_includes_longest
        return exam, best


def load_exams(path: Optional[Path] = None) -> ExamTable:
    path = path or paths.EXAMS_FILE
    return ExamTable(_read_json(path))


# ---------------------------------------------------------------------------
# cached singletons
# ---------------------------------------------------------------------------

_SETTINGS: Optional[Settings] = None
_EXAMS: Optional[ExamTable] = None


def settings() -> Settings:
    global _SETTINGS
    if _SETTINGS is None:
        _SETTINGS = load_settings()
    return _SETTINGS


def exams() -> ExamTable:
    global _EXAMS
    if _EXAMS is None:
        _EXAMS = load_exams()
    return _EXAMS


def reset_cache() -> None:
    global _SETTINGS, _EXAMS
    _SETTINGS = None
    _EXAMS = None


YEAR_IN_TITLE_RE = re.compile(r"20\d\d")
