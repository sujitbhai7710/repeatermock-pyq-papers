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

GLOBAL HALT
-----------
``GlobalHalt`` is raised only when there is no healthy route left **for the
model being requested**:

* every route for that model is exhausted / cooling down / without credentials, or
* ``rate_limit.consecutive_failure_threshold`` (default 10) consecutive
  failures for that model were recorded and no success interrupted them.

A rate limit from one route therefore never halts a run while another route can
serve the model.  Once halted, the router refuses all further requests; the
caller checkpoints, finishes its deterministic work and exits with
``status=ai_unavailable`` (exit code 0 — an unavailable AI never fails a run).
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
        report = self.route_report()
        models = [self.debate.proposer_model, self.debate.critic_model]
        for role, model in (("proposer", models[0]), ("critic", models[1])):
            for name in self.order_for(model, self.debate.order_for(model, role=role)):
                key = route_key(name, model)
                if key in report:
                    continue
                healthy, why = self.route_healthy(name, model)
                state = self.route_state(name, model).as_dict()
                state.update(
                    {"healthy": healthy, "why": why, "configured": bool(self._keys(name))}
                )
                report[key] = state
        return dict(sorted(report.items()))

    # -- dispatch ---------------------------------------------------------
    def chat(
        self,
        *,
        model: str,
        messages: Sequence[llm.ChatMessage],
        provider_order: Optional[Sequence[str]] = None,
        temperature: float = 0.0,
        max_tokens: Optional[int] = None,
        json_mode: bool = False,
    ) -> llm.ChatResult:
        """Send one completion, honouring the model's route order and failover."""

        self.raise_if_halted(model)
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
                self.raise_if_halted(model)
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
                self.raise_if_halted(model)
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
            return result

        self.raise_if_halted(model)
        summary, should_halt = self._no_route_reason(model, order, unavailable, attempted)
        if should_halt:
            self.halt(summary, model=model)
            self.raise_if_halted(model)
        if last_error is not None:
            raise last_error
        raise llm.LlmError(summary)

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
