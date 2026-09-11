"""Provider routing + the GLOBAL HALT rule.

Routes
------
A **route** is a ``(provider, model)`` pair.  Providers are declared in the
``providers`` block of ``config/settings.json`` (``providers.routes`` +
``providers.order``; :data:`agent.llm.PROVIDERS` holds the built-in defaults) and
the order a request walks is ``agentrouter`` → ``ar-worker`` → ``jw-worker`` →
``justwoker`` — override per role with ``debate.proposer_provider_order`` /
``debate.critic_provider_order`` or per model with
``debate.model_provider_orders``.

===============  ==========================================  ==================
provider         base_url                                    auth / user agent
===============  ==========================================  ==================
``agentrouter``  ``https://agentrouter.org/v1`` (direct)     bearer / ``cline/2.0.0``
``ar-worker``    ``https://ar-rotator.opencode-5a3.workers.dev/v1``  bearer / browser
``jw-worker``    ``https://jw-rotator.opencode-5a3.workers.dev/v1``  bearer / browser
``justwoker``    ``https://api.justwoker.icu/v1`` (direct)   Anthropic Messages / browser
===============  ==========================================  ==================

Keys come from the environment only (``AGENTROUTER_KEYS``, ``AR_PROXY_TOKEN``,
``JW_PROXY_TOKEN``, ``JUSTWOKER_KEYS``, with ``OPENAI_KEYS`` / ``DEEPSEEK_KEYS``
kept as aliases); they are never hardcoded and never logged.

Model-aware failover
--------------------
Every request walks the configured order for **its model** and stops at the
first route that answers.  A route that answers with a rate-limit / exhaustion
signal (HTTP 402/429/503, or a body such as ``all_keys_exhausted`` / "all
upstream keys failed") is a **route-level outage**:

* its circuit breaker trips (default cooldown 300 s, doubling per consecutive
  trip up to 3,600 s) and the request immediately fails over to the next route;
* while the breaker is open the route is skipped entirely — the exhausted
  worker is not hammered once per item;
* after the cooldown the route is probed **once**; a success closes the breaker,
  a failure re-opens it with a doubled cooldown;
* repeated non-rate-limit failures (HTTP 401/403/500, transport errors) trip the
  same breaker after ``rate_limit.provider_error_strike_limit`` consecutive
  strikes, so a dead route cannot slow every single request down.

Breakers are keyed by ``(provider, model)``, never by provider alone: a provider
may serve one model and 503 for another (``jw-rotator`` does exactly that), and
a failure for ``deepseek-v4-flash`` must not take the provider out of rotation
for ``gpt-5.6-sol``.

Model-level failover
--------------------
Breakers alone are not enough: if **no** provider can serve the proposer's model
the request has nowhere to go, even when another model still has a working
route.  On a GitHub runner that is exactly the live situation — the direct
``agentrouter`` endpoint is blocked by the Aliyun WAF, both workers report
``all_keys_exhausted`` for ``deepseek-v4-flash`` and 403/503 elsewhere, so
``deepseek-v4-flash`` has zero routes while ``gpt-5.6-sol`` is still served by
``jw-worker``.

Each role therefore carries an ordered **candidate model** list
(``debate.proposer_models`` / ``debate.critic_models``).  A request walks the
candidates outermost-first — for every candidate model, every provider in that
model's order — and is served by the first ``(provider, model)`` that answers; the
serving pair is recorded on the :class:`~agent.llm.ChatResult` (``provider`` and
``model``) and counted in :attr:`RouterStats.model_fallbacks`.  The single-model
call ``chat(model=..., provider_order=...)`` is unchanged: pass *models* to opt
into the candidate walk.

GLOBAL HALT
-----------
``GlobalHalt`` is raised only when there is no healthy route left **for any
candidate model of the request**:

* every ``(provider, model)`` candidate is exhausted / cooling down / without
  credentials, or
* ``rate_limit.consecutive_failure_threshold`` (default 10) consecutive
  failures for each of them were recorded and no success interrupted them.

A rate limit from one route therefore never halts a run while another route (or
another candidate model) can serve the request.  Once halted, the router refuses
all further requests; the caller checkpoints, finishes its deterministic work and
exits with ``status=ai_unavailable`` (exit code 0 — an unavailable AI never fails
a run).
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import llm
from .config import Settings
from .util import Log, monotonic, now_iso

#: ordered default route list (see :data:`agent.llm.PROVIDERS`)
PROVIDER_ORDER: Tuple[str, ...] = llm.DEFAULT_PROVIDER_ORDER

STATUS_OK = "ok"
STATUS_RATE_LIMITED = "rate_limited"

#: consecutive non-rate-limit failures that trip a route's breaker
DEFAULT_ERROR_STRIKE_LIMIT = 3


class GlobalHalt(RuntimeError):
    """Raised once the router has halted; no further requests are made."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


def route_key(provider: str, model: str) -> str:
    return f"{provider}/{model}"


@dataclass
class RouteState:
    """Circuit-breaker state of one ``(provider, model)`` route (per run)."""

    provider: str
    model: str
    cooldown_seconds: float = 0.0
    open_until: float = 0.0
    trips: int = 0
    probes: int = 0
    error_strikes: int = 0
    last_reason: str = ""
    last_failure_at: str = ""

    @property
    def key(self) -> str:
        return route_key(self.provider, self.model)

    def cooling_down(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else monotonic()) < self.open_until

    def as_dict(self, now: Optional[float] = None) -> Dict[str, Any]:
        moment = now if now is not None else monotonic()
        return {
            "provider": self.provider,
            "model": self.model,
            "state": "open" if self.cooling_down(moment) else "closed",
            "trips": self.trips,
            "probes": self.probes,
            "error_strikes": self.error_strikes,
            "cooldown_seconds": round(self.cooldown_seconds, 1),
            "retry_in_seconds": round(max(0.0, self.open_until - moment), 1),
            "last_reason": self.last_reason,
            "last_failure_at": self.last_failure_at,
        }


@dataclass
class ModelAttempt:
    """Outcome of walking every provider of **one candidate model**.

    Returned instead of raised so the caller can carry on with the next
    candidate model: only ``halted`` (no healthy route left *and* credentials
    exist for it) is a dead end for that model.
    """

    model: str
    served: bool = False
    halted: bool = False
    result: Optional[llm.ChatResult] = None
    error: Optional[Exception] = None
    #: halt reason, or the ``_no_route_reason`` summary when not halted
    reason: str = ""
    #: human-readable summary of why this model could not serve
    summary: str = ""
    attempted: List[str] = field(default_factory=list)
    skipped: List[str] = field(default_factory=list)
    unavailable: Dict[str, str] = field(default_factory=dict)


@dataclass
class RouterStats:
    requests: int = 0
    successes: int = 0
    failures: int = 0
    rate_limit_signals: int = 0
    halted: bool = False
    halt_reason: str = ""
    halted_at: str = ""
    #: model -> reason, for a halt that only affects one model
    halted_models: Dict[str, str] = field(default_factory=dict)
    #: consecutive failures of the model of the most recent request
    consecutive_failures: int = 0
    last_model: str = ""
    consecutive_by_model: Dict[str, int] = field(default_factory=dict)
    #: ``provider/model`` -> {ok, fail, rate_limited, trips}
    per_route: Dict[str, Dict[str, int]] = field(default_factory=dict)
    #: model -> how often it served a request after an earlier candidate model
    #: of the same request failed (``same_model_fallback`` in the verdict)
    model_fallbacks: Dict[str, int] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "requests": self.requests,
            "successes": self.successes,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "consecutive_by_model": dict(sorted(self.consecutive_by_model.items())),
            "rate_limit_signals": self.rate_limit_signals,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "halted_at": self.halted_at,
            "halted_models": dict(sorted(self.halted_models.items())),
            "per_route": {k: dict(v) for k, v in sorted(self.per_route.items())},
            "model_fallbacks": dict(sorted(self.model_fallbacks.items())),
        }


class Router:
    """Thread-safe router with key rotation, per-model route failover and breakers."""

    def __init__(self, settings: Settings, *, log: Optional[Log] = None) -> None:
        self.settings = settings
        self.log = log or Log("router")
        self.policy = settings.rate_limit
        self.debate = settings.debate
        #: provider chain resolved from ``config/settings.json`` (``providers``
        #: block); the code defaults in :mod:`agent.llm` are the fallback
        self.providers: Tuple[llm.Provider, ...] = llm.resolve_providers(settings)
        self._provider_by_name: Dict[str, llm.Provider] = llm.provider_index(self.providers)
        self.provider_order: Tuple[str, ...] = (
            tuple(settings.provider_order)
            if getattr(settings, "provider_order", None)
            else tuple(p.name for p in self.providers)
        )
        self.stats = RouterStats()
        self._lock = threading.RLock()
        self._halted_models: Dict[str, str] = {}
        self._halt_all = False
        self._key_cursor: Dict[str, int] = {p.name: 0 for p in self.providers}
        self._routes: Dict[str, RouteState] = {}

    # -- providers ---------------------------------------------------------
    def provider(self, name: str) -> Optional[llm.Provider]:
        return self._provider_by_name.get(name)

    @property
    def error_strike_limit(self) -> int:
        return int(getattr(self.policy, "provider_error_strike_limit", DEFAULT_ERROR_STRIKE_LIMIT))

    def order_for(self, model: str, provider_order: Optional[Sequence[str]] = None) -> List[str]:
        """Ordered provider list for *model*.

        An explicit *provider_order* wins; then a per-model override from
        ``debate.model_provider_orders``; then ``debate.provider_order``; then
        ``providers.order`` from ``config/settings.json``; then the built-in
        default.  Unknown provider names are dropped.
        """

        candidates: Sequence[str]
        if provider_order:
            candidates = provider_order
        else:
            candidates = (
                self.debate.model_provider_orders.get(model)
                or self.debate.provider_order
                or self.provider_order
                or PROVIDER_ORDER
            )
        out: List[str] = []
        for name in candidates:
            if name in self._provider_by_name and name not in out:
                out.append(name)
        return out

    # -- role candidates (model-level failover) ----------------------------
    def role_models(self, role: str) -> Tuple[str, ...]:
        """Ordered candidate models of *role* (proposer / critic)."""

        return self.debate.candidates_for(role)

    def role_provider_order(self, role: str, model: str) -> List[str]:
        """Provider walk order for one role's *model* (per-model override wins)."""

        return self.order_for(model, self.debate.order_for(model, role=role))

    def role_routes(self, role: str) -> List[Tuple[str, str]]:
        """Every ``(provider, model)`` candidate of *role*, in walk order."""

        out: List[Tuple[str, str]] = []
        for model in self.role_models(role):
            for name in self.role_provider_order(role, model):
                pair = (name, model)
                if pair not in out:
                    out.append(pair)
        return out

    def all_route_candidates(self) -> List[Tuple[str, str]]:
        """The ``(provider, model)`` matrix of both roles (used by ``cli routes``)."""

        out: List[Tuple[str, str]] = []
        for role in ("proposer", "critic"):
            for pair in self.role_routes(role):
                if pair not in out:
                    out.append(pair)
        return out

    def role_available(self, role: str) -> bool:
        """True while at least one ``(provider, model)`` candidate can serve *role*."""

        for name, model in self.role_routes(role):
            if self.is_halted(model):
                continue
            if self.route_healthy(name, model)[0]:
                return True
        return False

    def roles_available(self, roles: Sequence[str] = ("proposer", "critic")) -> Dict[str, bool]:
        return {role: self.role_available(role) for role in roles}

    def role_health(self, role: str) -> Dict[str, Dict[str, Any]]:
        """``{"provider/model": {healthy, why, halted}}`` for one role's candidates."""

        report: Dict[str, Dict[str, Any]] = {}
        for name, model in self.role_routes(role):
            healthy, why = self.route_healthy(name, model)
            report[route_key(name, model)] = {
                "provider": name,
                "model": model,
                "healthy": healthy and not self.is_halted(model),
                "halted": self.is_halted(model),
                "why": why,
            }
        return report

    # -- halt handling ----------------------------------------------------
    @property
    def halted(self) -> bool:
        """True once the router halted for *any* model (run-level AI outage)."""

        return self.is_halted()

    def is_halted(self, model: Optional[str] = None) -> bool:
        """``True`` when requests for *model* (or any model) are refused."""

        with self._lock:
            if self._halt_all:
                return True
            if model is None:
                return bool(self._halted_models)
            return model in self._halted_models

    def halt(self, reason: str, *, model: Optional[str] = None) -> None:
        """Stop serving *model* (or every model when *model* is ``None``).

        The halt is keyed by model: a total ``deepseek-v4-flash`` outage must not
        refuse ``gpt-5.6-sol`` requests, which may well have a healthy route of
        their own (``jw-rotator`` serves one model and 503s the other).
        """

        with self._lock:
            if model is None:
                if self._halt_all:
                    return
                self._halt_all = True
                self._halted_models.setdefault("*", reason)
            elif model in self._halted_models:
                return
            else:
                self._halted_models[model] = reason
            self.stats.halted = True
            self.stats.halt_reason = reason
            self.stats.halted_at = now_iso()
            self.stats.halted_models = dict(self._halted_models)
        self.log.warn(
            f"GLOBAL HALT ({model or 'all models'}): {reason}"
        )

    def halt_reason(self, model: Optional[str] = None) -> str:
        with self._lock:
            if model is not None and model in self._halted_models:
                return self._halted_models[model]
            if self._halt_all:
                return self._halted_models.get("*", "")
            return next(iter(self._halted_models.values()), "")

    def raise_if_halted(self, model: Optional[str] = None) -> None:
        if self.is_halted(model):
            raise GlobalHalt(self.halt_reason(model) or "halted")

    # -- route circuit breakers -------------------------------------------
    def route_state(self, provider: str, model: str) -> RouteState:
        key = route_key(provider, model)
        with self._lock:
            state = self._routes.get(key)
            if state is None:
                state = RouteState(provider=provider, model=model)
                self._routes[key] = state
            return state

    def route_healthy(self, provider: str, model: str) -> Tuple[bool, str]:
        """``(healthy, why)`` for one route — cheap and side-effect free."""

        if provider not in self._provider_by_name:
            return False, "unknown provider"
        if not self._keys(provider):
            return False, "no credentials"
        state = self.route_state(provider, model)
        remaining = state.open_until - monotonic()
        if remaining > 0:
            return False, f"cooling down for {remaining:.0f}s after {state.last_reason}"
        return True, "probe" if state.trips else "closed"

    def routes_health(self, model: str, order: Optional[Sequence[str]] = None) -> Dict[str, Dict[str, Any]]:
        """``{provider: {healthy, why}}`` for every route of *model*."""

        report: Dict[str, Dict[str, Any]] = {}
        for name in self.order_for(model, order):
            healthy, why = self.route_healthy(name, model)
            report[name] = {"healthy": healthy, "why": why, "model": model}
        return report

    def model_available(self, model: str, order: Optional[Sequence[str]] = None) -> bool:
        """True while at least one route for *model* can serve a request."""

        if self.is_halted(model):
            return False
        return any(self.route_healthy(name, model)[0] for name in self.order_for(model, order))

    def models_available(self, models: Sequence[str]) -> Dict[str, bool]:
        return {model: self.model_available(model) for model in models}

    def halt_report(self) -> Dict[str, str]:
        """``{model: reason}`` for every halted model (``*`` = every model)."""

        with self._lock:
            return dict(self._halted_models)

    def _trip_route(self, provider: str, model: str, reason: str, *, strike: bool = False) -> float:
        """Open (or re-open) the route's circuit breaker with an increased cooldown."""

        base = max(1.0, float(self.policy.provider_cooldown_seconds))
        cap = max(base, float(self.policy.provider_cooldown_max_seconds))
        with self._lock:
            state = self.route_state(provider, model)
            if strike:
                state.error_strikes += 1
            state.trips += 1
            state.last_reason = reason
            state.last_failure_at = now_iso()
            cooldown = base if state.cooldown_seconds <= 0 else min(state.cooldown_seconds * 2, cap)
            state.cooldown_seconds = cooldown
            state.open_until = monotonic() + cooldown
            bucket = self.stats.per_route.setdefault(state.key, {"ok": 0, "fail": 0})
            bucket["trips"] = bucket.get("trips", 0) + 1
        self.log.warn(
            f"route {state.key}: circuit open for {cooldown:.0f}s ({reason})"
        )
        return cooldown

    def _close_route(self, provider: str, model: str) -> None:
        with self._lock:
            state = self.route_state(provider, model)
            if state.trips or state.open_until or state.error_strikes:
                self.log.info(f"route {state.key}: healthy again – circuit closed")
            state.trips = 0
            state.probes = 0
            state.error_strikes = 0
            state.cooldown_seconds = 0.0
            state.open_until = 0.0
            state.last_reason = ""

    def _note_probe(self, provider: str, model: str) -> None:
        """Count a request sent to a previously tripped route (half-open probe)."""

        with self._lock:
            state = self.route_state(provider, model)
            if state.trips:
                state.probes += 1

    def _reopen_probe(self, provider: str, model: str) -> None:
        """A half-open probe failed for a non-rate-limit reason: keep it open."""

        with self._lock:
            state = self.route_state(provider, model)
            if state.cooldown_seconds > 0:
                state.open_until = monotonic() + state.cooldown_seconds

    def _strike_error(self, provider: str, model: str, detail: str) -> bool:
        """Count a non-rate-limit failure; trip the route at the strike limit."""

        limit = self.error_strike_limit
        with self._lock:
            state = self.route_state(provider, model)
            if state.cooldown_seconds > 0:
                # already open (half-open probe) — just extend the cooldown
                state.open_until = monotonic() + state.cooldown_seconds
                return False
            state.error_strikes += 1
            strikes = state.error_strikes
        if limit > 0 and strikes >= limit:
            self._trip_route(
                provider,
                model,
                f"{strikes} consecutive errors: {detail}",
                strike=False,
            )
            return True
        return False

    # -- stats ------------------------------------------------------------
    def _record_success(self, provider: str, model: str) -> None:
        with self._lock:
            self.stats.requests += 1
            self.stats.successes += 1
            self.stats.last_model = model
            self.stats.consecutive_by_model[model] = 0
            self.stats.consecutive_failures = 0
            bucket = self.stats.per_route.setdefault(route_key(provider, model), {"ok": 0, "fail": 0})
            bucket["ok"] += 1

    def _record_failure(self, provider: str, model: str, rate_limited: bool, detail: str) -> int:
        with self._lock:
            self.stats.requests += 1
            self.stats.failures += 1
            self.stats.last_model = model
            consecutive = self.stats.consecutive_by_model.get(model, 0) + 1
            self.stats.consecutive_by_model[model] = consecutive
            self.stats.consecutive_failures = consecutive
            bucket = self.stats.per_route.setdefault(route_key(provider, model), {"ok": 0, "fail": 0})
            bucket["fail"] += 1
            if rate_limited:
                bucket["rate_limited"] = bucket.get("rate_limited", 0) + 1
                self.stats.rate_limit_signals += 1

        if consecutive >= self.policy.consecutive_failure_threshold:
            self.halt(
                f"{consecutive} consecutive failures for {model} (threshold "
                f"{self.policy.consecutive_failure_threshold}); last: {detail}",
                model=model,
            )
        return consecutive

    # -- key pool ---------------------------------------------------------
    def key_pools(self) -> Dict[str, List[str]]:
        """Credential pool per provider, read from the environment (never logged)."""

        return llm.provider_keys(self.providers)

    def _keys(self, provider_name: str) -> List[str]:
        return self.key_pools().get(provider_name, [])

    def _next_key(self, provider_name: str) -> Optional[str]:
        keys = self._keys(provider_name)
        if not keys:
            return None
        with self._lock:
            index = self._key_cursor.get(provider_name, 0) % len(keys)
            self._key_cursor[provider_name] = (index + 1) % len(keys)
        return keys[index]

    def available(self) -> bool:
        return any(self.key_pools().values())

    def configured_providers(self) -> List[str]:
        pools = self.key_pools()
        return [p.name for p in self.providers if pools.get(p.name)]

    def route_report(self) -> Dict[str, Any]:
        """Snapshot of every route's breaker state, keyed by ``provider/model``."""

        moment = monotonic()
        out: Dict[str, Any] = {}
        with self._lock:
            states = list(self._routes.values())
        configured = set(self.configured_providers())
        for state in sorted(states, key=lambda s: s.key):
            entry = state.as_dict(moment)
            entry.update(self.stats.per_route.get(state.key, {}))
            entry["configured"] = state.provider in configured
            out[state.key] = entry
        return out

    #: kept for callers that only want "how is each provider doing"
    def provider_report(self) -> Dict[str, Any]:
        """Health of every ``(provider, model)`` candidate of both roles.

        The report covers the whole candidate matrix (not just the two primary
        models) so ``PROGRESS.md`` shows which fallback model a run actually has
        available.
        """

        report = self.route_report()
        for role in ("proposer", "critic"):
            for name, model in self.role_routes(role):
                key = route_key(name, model)
                if key in report:
                    continue
                healthy, why = self.route_healthy(name, model)
                state = self.route_state(name, model).as_dict()
                state.update(
                    {
                        "healthy": healthy and not self.is_halted(model),
                        "why": why,
                        "configured": bool(self._keys(name)),
                        "role": role,
                    }
                )
                report[key] = state
        return dict(sorted(report.items()))

    # -- dispatch ---------------------------------------------------------
    def _candidates(self, model: str, models: Optional[Sequence[str]]) -> Tuple[str, ...]:
        """Ordered candidate models of one request.

        An explicit *models* list wins (that is how a caller steers the order);
        *model* is appended when it is missing so a request can never lose its own
        model, and the single-model call ``chat(model=...)`` keeps its exact
        behaviour.
        """

        ordered = [str(name).strip() for name in (models or ()) if str(name).strip()]
        if not ordered:
            ordered = [str(model)]
        elif model and model not in ordered:
            ordered.append(str(model))
        out: List[str] = []
        for name in ordered:
            if name and name not in out:
                out.append(name)
        return tuple(out)

    def _provider_order_for(
        self,
        model: str,
        provider_order: Optional[Sequence[str]],
        role: str,
    ) -> Optional[Sequence[str]]:
        """Provider walk order for one candidate model.

        An explicit *provider_order* wins; a role-aware request resolves the order
        **per candidate model** (``debate.model_provider_orders`` override first,
        then the role's order) so a fallback model keeps its own route chain.
        """

        if provider_order:
            return provider_order
        if role:
            return self.role_provider_order(role, model)
        return None

    def _chat_model(
        self,
        *,
        model: str,
        messages: Sequence[llm.ChatMessage],
        provider_order: Optional[Sequence[str]],
        temperature: float,
        max_tokens: Optional[int],
        json_mode: bool,
    ) -> ModelAttempt:
        """Walk every provider of *model*; never raises for a route outage."""

        order = self.order_for(model, provider_order)
        last_error: Optional[Exception] = None
        attempted: List[str] = []
        skipped: List[str] = []
        unavailable: Dict[str, str] = {}

        for name in order:
            provider = self.provider(name)
            if provider is None:
                continue
            ready, why = self.route_healthy(name, model)
            if not ready:
                unavailable[name] = why
                skipped.append(name)
                self.log.info(f"route {name}/{model}: skipped ({why})")
                continue
            api_key = self._next_key(name)
            if not api_key:
                unavailable[name] = "no credentials"
                continue

            extra = {"response_format": {"type": "json_object"}} if json_mode else None
            attempted.append(name)
            self._note_probe(name, model)
            try:
                result = llm.chat_completion(
                    base_url=provider.base_url,
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    timeout=self.policy.request_timeout_seconds,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    extra=extra,
                    # reasoning models may truncate before writing ``content``;
                    # retry the same route with a bigger budget instead of
                    # reporting a route failure.
                    truncation_retries=self.policy.max_retries_per_request + 1,
                    auth_style=provider.auth_style,
                    user_agent_value=provider.user_agent_value(),
                    path=provider.request_path(),
                )
            except llm.LlmRateLimited as exc:
                detail = str(exc)
                if self.policy.halt_on_any_rate_limit_signal:
                    self._trip_route(name, model, detail)
                self._record_failure(name, model, True, detail)
                self.log.warn(
                    f"route {name}/{model}: rate-limited ({detail[:160]}) – failing over"
                )
                last_error = exc
                if self.is_halted(model):
                    break
                continue
            except llm.LlmError as exc:
                detail = str(exc)
                self._reopen_probe(name, model)
                self._strike_error(name, model, detail)
                self._record_failure(name, model, False, detail)
                self.log.warn(
                    f"route {name}/{model}: failed ({detail[:160]}) – failing over"
                )
                last_error = exc
                if self.is_halted(model):
                    break
                continue
            self._close_route(name, model)
            self._record_success(name, model)
            result.provider = name
            self.log.info(f"route {name}/{model}: ok ({result.latency_ms} ms, {result.key_ref})")
            if attempted[:-1] or skipped:
                self.log.info(
                    f"failover {model}: {name} served the request after "
                    f"{len(attempted) - 1} failed and {len(skipped)} skipped route(s)"
                    + (f" [skipped: {', '.join(skipped)}]" if skipped else "")
                )
            return ModelAttempt(
                model=model,
                served=True,
                result=result,
                attempted=attempted,
                skipped=skipped,
                unavailable=unavailable,
            )

        if self.is_halted(model):
            reason = self.halt_reason(model) or f"{model} halted"
            return ModelAttempt(
                model=model,
                halted=True,
                error=last_error,
                reason=reason,
                summary=reason,
                attempted=attempted,
                skipped=skipped,
                unavailable=unavailable,
            )
        summary, should_halt = self._no_route_reason(model, order, unavailable, attempted)
        if should_halt:
            self.halt(summary, model=model)
            return ModelAttempt(
                model=model,
                halted=True,
                error=last_error,
                reason=summary,
                summary=summary,
                attempted=attempted,
                skipped=skipped,
                unavailable=unavailable,
            )
        # not a halt: either credentials are missing entirely (an unconfigured
        # environment must not look like a provider outage) or a route is still
        # healthy and the caller may retry
        return ModelAttempt(
            model=model,
            halted=False,
            error=last_error or llm.LlmError(summary),
            reason=summary,
            summary=summary,
            attempted=attempted,
            skipped=skipped,
            unavailable=unavailable,
        )

    def chat(
        self,
        *,
        model: str,
        messages: Sequence[llm.ChatMessage],
        provider_order: Optional[Sequence[str]] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
        models: Optional[Sequence[str]] = None,
        role: str = "",
    ) -> llm.ChatResult:
        """Send one completion, honouring the candidate-model + route order.

        With the default ``models=None`` this is the original single-model call:
        walk the providers of *model* and stop at the first that answers.

        With *models* given, the candidates are walked **outermost-first** — for
        each candidate model, every provider in that model's order — and the
        request is served by the first ``(provider, model)`` that answers.  Which
        pair served it is recorded on the result (``ChatResult.provider`` /
        ``ChatResult.model``) and counted in
        :attr:`RouterStats.model_fallbacks` when it was not the first candidate.

        :class:`GlobalHalt` is raised only when **no** ``(provider, model)``
        candidate is healthy; a dead model then costs one candidate instead of
        skipping the whole AI step.
        """

        candidates = self._candidates(model, models)
        first = candidates[0]
        attempts: List[ModelAttempt] = []
        errors: List[Exception] = []

        for index, candidate in enumerate(candidates):
            if self.is_halted(candidate):
                # a halted model refuses every further request: skip the candidate
                # instead of asking its routes again
                reason = self.halt_reason(candidate) or f"{candidate} halted"
                attempts.append(
                    ModelAttempt(model=candidate, halted=True, reason=reason, summary=reason)
                )
                if index < len(candidates) - 1:
                    self.log.info(f"model {candidate}: halted ({reason}) – next candidate")
                continue
            attempt = self._chat_model(
                model=candidate,
                messages=messages,
                provider_order=self._provider_order_for(candidate, provider_order, role),
                temperature=temperature,
                max_tokens=max_tokens,
                json_mode=json_mode,
            )
            attempts.append(attempt)
            if attempt.served and attempt.result is not None:
                result = attempt.result
                if candidate != first:
                    with self._lock:
                        self.stats.model_fallbacks[candidate] = (
                            self.stats.model_fallbacks.get(candidate, 0) + 1
                        )
                    self.log.warn(
                        f"model fallback{f' ({role})' if role else ''}: "
                        f"{result.model}@{result.provider} served the request "
                        f"(candidates: {', '.join(candidates)}; "
                        f"{candidates.index(candidate)} earlier candidate(s) failed)"
                    )
                return result
            if attempt.error is not None:
                errors.append(attempt.error)
            if attempt.halted and index < len(candidates) - 1:
                self.log.warn(
                    f"model {candidate}: no healthy route – trying the next candidate"
                )

        last_error = errors[-1] if errors else None
        # A candidate that is merely unhealthy-but-not-halted (a route is still
        # healthy, or no credentials are configured at all) means "not an AI
        # outage": hand the underlying error to the caller so it may retry.
        transient = [a for a in attempts if not a.halted]
        if transient or not attempts:
            if last_error is not None:
                raise last_error
            raise llm.LlmError(
                "; ".join(a.summary for a in attempts)
                or f"no candidate model could serve {model}"
            )

        reason = (
            f"no healthy route for any candidate model of "
            f"{role or 'the request'} ({', '.join(candidates)}): "
            + "; ".join(f"{a.model}: {a.summary}" for a in attempts)
        )
        raise GlobalHalt(reason)

    def _no_route_reason(
        self,
        model: str,
        order: Sequence[str],
        unavailable: Dict[str, str],
        attempted: Sequence[str],
    ) -> Tuple[str, bool]:
        """Why no route could serve *model*, and whether that is a halt.

        A halt means "the AI is unavailable" for this model: every configured
        route failed or is cooling down.  Two cases are *not* a halt:

        * a route is still healthy (the failure was transient) — the caller gets
          the underlying :class:`~agent.llm.LlmError` and may retry;
        * no credentials are configured at all — an unconfigured environment must
          not look like a provider outage.
        """

        details = []
        healthy_now = []
        for name in order:
            healthy, why = self.route_healthy(name, model)
            if healthy:
                healthy_now.append(name)
            details.append(f"{name}: {unavailable.get(name) or why}")
        summary = (
            f"no healthy route for {model} after {len(attempted)} attempt(s)"
            + (f" (tried {', '.join(attempted)})" if attempted else "")
            + ("; " + "; ".join(details) if details else "; no providers configured")
        )
        if not any(self._keys(name) for name in order):
            return f"no provider credentials configured for {model}", False
        if healthy_now:
            return summary + f"; still healthy: {', '.join(healthy_now)}", False
        return summary, True


_router: Optional[Router] = None
_router_lock = threading.Lock()


def get_router(settings: Settings, log: Optional[Log] = None) -> Router:
    global _router
    with _router_lock:
        if _router is None:
            _router = Router(settings, log=log)
        return _router


def reset_router() -> None:
    global _router
    with _router_lock:
        _router = None


def model_for(task: str, settings: Settings, *, role: str = "proposer") -> str:
    """Task -> model mapping.

    ``role="proposer"`` uses the DeepSeek V4 Flash model, ``role="critic"`` uses
    GPT-5.6 Sol.
    """

    if role == "critic":
        return settings.debate.critic_model
    return settings.debate.proposer_model
