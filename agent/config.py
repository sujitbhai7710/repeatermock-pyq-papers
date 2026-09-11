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
    #: when true (default) a rate-limit/exhaustion signal trips the *route's*
    #: circuit breaker and the request fails over; a global halt only happens
    #: once no healthy route is left for the requested model (or the
    #: consecutive-failure threshold is reached).  When false the route stays in
    #: rotation and only the consecutive-failure counter applies.
    halt_on_any_rate_limit_signal: bool = True
    request_timeout_seconds: int = 120
    max_retries_per_request: int = 1
    #: circuit-breaker cooldown for an exhausted route (doubles per consecutive
    #: trip, capped by ``provider_cooldown_max_seconds``)
    provider_cooldown_seconds: int = 300
    provider_cooldown_max_seconds: int = 3600
    #: consecutive non-rate-limit failures (HTTP 4xx/5xx, transport errors) after
    #: which a route's breaker opens as well (0 disables)
    provider_error_strike_limit: int = 3


@dataclass(frozen=True)
class ProviderSpec:
    """One route of the provider chain.

    Loaded from the ``providers.routes`` block of ``config/settings.json`` so a
    breaking route can be repaired without touching code:

    * ``auth_style`` – ``bearer`` (``Authorization: Bearer <key>``, OpenAI chat
      completions) or ``anthropic`` (``x-api-key`` + ``anthropic-version``,
      Anthropic Messages);
    * ``user_agent`` – ``browser`` (Cloudflare-fronted workers), the literal
      ``cline/2.0.0`` the direct agentrouter endpoint requires, or any other
      literal;
    * ``env_keys`` – environment variables whose comma-separated values form the
      route's key pool (never logged).
    """

    name: str
    base_url: str
    auth_style: str = "bearer"
    user_agent: str = "browser"
    env_keys: Tuple[str, ...] = ()
    label: str = ""
    #: relative request path; empty = the auth style's default
    path: str = ""


#: default route order: direct agentrouter first, then the two workers, then the
#: direct Anthropic-compatible endpoint
DEFAULT_PROVIDER_ORDER: Tuple[str, ...] = ("agentrouter", "ar-worker", "jw-worker", "justwoker")

#: built-in routes used when ``config/settings.json`` has no ``providers`` block
DEFAULT_PROVIDERS: Tuple[ProviderSpec, ...] = (
    ProviderSpec(
        name="agentrouter",
        base_url="https://agentrouter.org/v1",
        auth_style="bearer",
        user_agent="cline/2.0.0",
        env_keys=("AGENTROUTER_KEYS", "OPENAI_KEYS"),
        label="agentrouter.org (direct)",
    ),
    ProviderSpec(
        name="ar-worker",
        base_url="https://ar-rotator.opencode-5a3.workers.dev/v1",
        auth_style="bearer",
        user_agent="browser",
        env_keys=("AR_PROXY_TOKEN",),
        label="ar-rotator worker",
    ),
    ProviderSpec(
        name="jw-worker",
        base_url="https://jw-rotator.opencode-5a3.workers.dev/v1",
        auth_style="bearer",
        user_agent="browser",
        env_keys=("JW_PROXY_TOKEN",),
        label="jw-rotator worker",
    ),
    ProviderSpec(
        name="justwoker",
        base_url="https://api.justwoker.icu/v1",
        auth_style="anthropic",
        user_agent="browser",
        env_keys=("JUSTWOKER_KEYS", "DEEPSEEK_KEYS"),
        label="api.justwoker.icu (direct, Anthropic Messages API)",
    ),
)


#: candidate models per role, in failover order.  The first entry is the primary
#: model (``debate.proposer_model`` / ``debate.critic_model``); the rest are tried
#: in order when **no provider** can serve a model — the case that used to skip
#: the whole AI step: ``deepseek-v4-flash`` had zero routes (agentrouter blocked
#: by the Aliyun WAF on the runner, both workers out of credits, justwoker 403)
#: while ``gpt-5.6-sol`` was still served by ``jw-worker``.
DEFAULT_PROPOSER_MODELS: Tuple[str, ...] = (
    "deepseek-v4-flash",
    "deepseek-v4-pro",
    "gpt-5.6-sol",
    "claude-sonnet-5",
    "glm-5.3",
)

DEFAULT_CRITIC_MODELS: Tuple[str, ...] = (
    "gpt-5.6-sol",
    "claude-opus-5",
    "claude-sonnet-5",
    "gemini-3.5-flash",
    "grok-4.6",
)


@dataclass(frozen=True)
class DebatePolicy:
    max_rounds: int = 1
    proposer_model: str = "deepseek-v4-flash"
    critic_model: str = "gpt-5.6-sol"
    #: ordered fallback candidates per role (``debate.proposer_models`` /
    #: ``debate.critic_models``).  A route is a ``(provider, model)`` pair, so a
    #: model with no working route anywhere must be able to fall back to another
    #: *model* — otherwise a provider outage that only spares ``gpt-5.6-sol``
    #: leaves the proposer with zero routes and skips the whole AI step.
    proposer_models: Tuple[str, ...] = DEFAULT_PROPOSER_MODELS
    critic_models: Tuple[str, ...] = DEFAULT_CRITIC_MODELS
    #: ordered route list used when a request does not carry its own order
    provider_order: Tuple[str, ...] = DEFAULT_PROVIDER_ORDER
    proposer_provider_order: Tuple[str, ...] = DEFAULT_PROVIDER_ORDER
    critic_provider_order: Tuple[str, ...] = DEFAULT_PROVIDER_ORDER
    #: optional per-model overrides, e.g. ``{"gpt-5.6-sol": ["jw-worker"]}``
    model_provider_orders: Dict[str, Tuple[str, ...]] = field(default_factory=dict)

    def primary_model(self, role: str = "proposer") -> str:
        return self.critic_model if role == "critic" else self.proposer_model

    def candidates_for(self, role: str = "proposer") -> Tuple[str, ...]:
        """Ordered model candidates for *role*: the primary model first.

        ``proposer_model`` / ``critic_model`` stay authoritative for the *first*
        attempt; the declared list only supplies the fallbacks (and the primary is
        de-duplicated into its declared position when it appears there).
        """

        primary = self.primary_model(role)
        declared = self.critic_models if role == "critic" else self.proposer_models
        out: List[str] = []
        for model in (primary, *(declared or ())):
            name = str(model).strip()
            if name and name not in out:
                out.append(name)
        return tuple(out)

    def all_models(self) -> Tuple[str, ...]:
        """Every candidate model of both roles (the ``routes`` probe matrix)."""

        out: List[str] = []
        for role in ("proposer", "critic"):
            for model in self.candidates_for(role):
                if model not in out:
                    out.append(model)
        return tuple(out)

    def order_for(self, model: str, *, role: str = "proposer") -> Tuple[str, ...]:
        override = self.model_provider_orders.get(model)
        if override:
            return override
        return self.critic_provider_order if role == "critic" else self.proposer_provider_order


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
    #: the provider chain (``providers.routes``); empty = keep the code default
    providers: Tuple[ProviderSpec, ...] = DEFAULT_PROVIDERS
    #: ordered route names (``providers.order``)
    provider_order: Tuple[str, ...] = DEFAULT_PROVIDER_ORDER

    # convenience -----------------------------------------------------------
    @property
    def max_work_seconds(self) -> int:
        return self.work_window_seconds

    def provider_by_name(self, name: str) -> Optional[ProviderSpec]:
        for provider in self.providers:
            if provider.name == name:
                return provider
        return None


def _provider_specs(config: Any) -> Tuple[ProviderSpec, ...]:
    """Parse the ``providers`` block into specs, preserving the declared order.

    ``providers.routes`` is a name -> spec mapping, ``providers.order`` lists the
    names in failover order (unnamed routes are appended in declaration order).
    """

    if not isinstance(config, dict):
        return DEFAULT_PROVIDERS
    routes = config.get("routes")
    if not isinstance(routes, dict) or not routes:
        return DEFAULT_PROVIDERS
    order = [str(name) for name in (config.get("order") or [])]
    names = [name for name in order if name in routes]
    names += [name for name in routes if name not in names]

    specs: List[ProviderSpec] = []
    for name in names:
        body = routes[name]
        if not isinstance(body, dict):
            continue
        specs.append(
            ProviderSpec(
                name=name,
                base_url=str(body.get("base_url", "")),
                auth_style=str(body.get("auth_style", "bearer")),
                user_agent=str(body.get("user_agent", "browser")),
                env_keys=tuple(str(env) for env in (body.get("env_keys") or ())),
                label=str(body.get("label", name)),
                path=str(body.get("path", "")),
            )
        )
    return tuple(specs) or DEFAULT_PROVIDERS


def _provider_order(config: Any, specs: Tuple[ProviderSpec, ...]) -> Tuple[str, ...]:
    if isinstance(config, dict):
        declared = [str(name) for name in (config.get("order") or [])]
        present = {spec.name for spec in specs}
        ordered = tuple(name for name in declared if name in present)
        if ordered:
            return ordered
    return tuple(spec.name for spec in specs) or DEFAULT_PROVIDER_ORDER


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
    strike_limit = int(rl.get("provider_error_strike_limit", 3))
    env_strikes = _env_int("PROVIDER_ERROR_STRIKE_LIMIT")
    if env_strikes is not None:
        strike_limit = env_strikes
    rate_limit = RateLimitPolicy(
        consecutive_failure_threshold=threshold,
        halt_on_any_rate_limit_signal=bool(rl.get("halt_on_any_rate_limit_signal", True)),
        request_timeout_seconds=int(rl.get("request_timeout_seconds", 120)),
        max_retries_per_request=int(rl.get("max_retries_per_request", 1)),
        provider_cooldown_seconds=cooldown,
        provider_cooldown_max_seconds=cooldown_max,
        provider_error_strike_limit=strike_limit,
    )

    db = raw.get("debate", {})

    provider_specs = _provider_specs(raw.get("providers"))
    provider_order = _provider_order(raw.get("providers"), provider_specs)

    def _order(key: str, default: Tuple[str, ...]) -> Tuple[str, ...]:
        value = db.get(key)
        if not value:
            return default
        return tuple(str(name) for name in value)

    def _models(key: str, env_name: str, default: Tuple[str, ...]) -> Tuple[str, ...]:
        """Candidate model list: environment (comma separated) beats the file."""

        raw_env = os.environ.get(env_name, "")
        if raw_env.strip():
            parsed = tuple(chunk.strip() for chunk in raw_env.split(",") if chunk.strip())
            if parsed:
                return parsed
        value = db.get(key)
        if isinstance(value, str):
            value = [chunk for chunk in value.split(",")]
        if not value:
            return default
        names = tuple(str(name).strip() for name in value if str(name).strip())
        return names or default

    model_orders = {
        str(model): tuple(str(name) for name in names)
        for model, names in (db.get("model_provider_orders") or {}).items()
    }
    debate = DebatePolicy(
        max_rounds=int(db.get("max_rounds", 1)),
        proposer_model=str(db.get("proposer_model", "deepseek-v4-flash")),
        critic_model=str(db.get("critic_model", "gpt-5.6-sol")),
        proposer_models=_models("proposer_models", "PYQ_PROPOSER_MODELS", DEFAULT_PROPOSER_MODELS),
        critic_models=_models("critic_models", "PYQ_CRITIC_MODELS", DEFAULT_CRITIC_MODELS),
        provider_order=_order("provider_order", provider_order),
        proposer_provider_order=_order("proposer_provider_order", provider_order),
        critic_provider_order=_order("critic_provider_order", provider_order),
        model_provider_orders=model_orders,
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
        providers=provider_specs,
        provider_order=provider_order,
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
