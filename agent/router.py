"""Provider routing + the GLOBAL HALT rule.

Provider order
--------------
1. **agentrouter ar-rotator** – ``https://ar-rotator.opencode-5a3.workers.dev/v1``
   (models ``deepseek-v4-flash`` / ``gpt-5.6-sol``)
2. **jw-rotator** – ``https://jw-rotator.opencode-5a3.workers.dev/v1`` (fallback)

Keys come from the environment only (``OPENAI_KEYS``, ``DEEPSEEK_KEYS``,
``AR_PROXY_TOKEN``, ``JW_PROXY_TOKEN``); they are never hardcoded and never
logged.

Provider-aware failover
-----------------------
Every request walks the configured provider order.  A provider that answers with
a rate-limit / exhaustion signal (HTTP 402/429/503, or a body such as
``all_keys_exhausted`` / "all upstream keys failed") is treated as a
**provider-level outage**:

* its circuit breaker trips (default cooldown 300 s, doubling per consecutive
  trip up to 3,600 s) and the request immediately fails over to the next
  configured provider;
* while the breaker is open the provider is skipped entirely — the exhausted
  worker is not hammered once per item;
* after the cooldown the provider is probed **once**; a success closes the
  breaker, a failure re-opens it with a doubled cooldown.

GLOBAL HALT
-----------
``GlobalHalt`` is raised only when there is no healthy provider left:

* every configured provider is exhausted / cooling down / without credentials, or
* ``rate_limit.consecutive_failure_threshold`` (default 10) consecutive
  failures were recorded and no success interrupted them.

A rate limit from one provider therefore never halts a run while another
provider is healthy: the "stop, don't keep hammering" rule is applied at the
level of the *provider chain*.  Once halted, the router refuses all further
requests, the in-flight item finishes, and the caller checkpoints and exits with
``status=rate_limited``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import llm
from .config import Settings
from .util import Log, monotonic, now_iso

PROVIDERS: Tuple[Dict[str, Any], ...] = (
    {
        "name": "agentrouter",
        "base_url": "https://ar-rotator.opencode-5a3.workers.dev/v1",
        "env_keys": ("OPENAI_KEYS", "AR_PROXY_TOKEN"),
    },
    {
        "name": "jw",
        "base_url": "https://jw-rotator.opencode-5a3.workers.dev/v1",
        "env_keys": ("DEEPSEEK_KEYS", "JW_PROXY_TOKEN"),
    },
)

PROVIDER_BY_NAME = {p["name"]: p for p in PROVIDERS}

STATUS_OK = "ok"
STATUS_RATE_LIMITED = "rate_limited"


class GlobalHalt(RuntimeError):
    """Raised once the router has halted; no further requests are made."""

    def __init__(self, reason: str) -> None:
        super().__init__(reason)
        self.reason = reason


@dataclass
class ProviderState:
    """Circuit-breaker state of one provider (per run)."""

    name: str
    cooldown_seconds: float = 0.0
    open_until: float = 0.0
    trips: int = 0
    probes: int = 0
    last_reason: str = ""

    def cooling_down(self, now: Optional[float] = None) -> bool:
        return (now if now is not None else monotonic()) < self.open_until

    def as_dict(self, now: Optional[float] = None) -> Dict[str, Any]:
        moment = now if now is not None else monotonic()
        return {
            "state": "open" if self.cooling_down(moment) else "closed",
            "trips": self.trips,
            "probes": self.probes,
            "cooldown_seconds": round(self.cooldown_seconds, 1),
            "retry_in_seconds": round(max(0.0, self.open_until - moment), 1),
            "last_reason": self.last_reason,
        }


@dataclass
class RouterStats:
    requests: int = 0
    successes: int = 0
    failures: int = 0
    consecutive_failures: int = 0
    rate_limit_signals: int = 0
    halted: bool = False
    halt_reason: str = ""
    halted_at: str = ""
    per_provider: Dict[str, Dict[str, int]] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "requests": self.requests,
            "successes": self.successes,
            "failures": self.failures,
            "consecutive_failures": self.consecutive_failures,
            "rate_limit_signals": self.rate_limit_signals,
            "halted": self.halted,
            "halt_reason": self.halt_reason,
            "halted_at": self.halted_at,
            "per_provider": self.per_provider,
        }


class Router:
    """Thread-safe router with key rotation, provider failover and circuit breakers."""

    def __init__(self, settings: Settings, *, log: Optional[Log] = None) -> None:
        self.settings = settings
        self.log = log or Log("router")
        self.policy = settings.rate_limit
        self.stats = RouterStats()
        self._lock = threading.RLock()
        self._halt = threading.Event()
        self._key_cursor: Dict[str, int] = {name: 0 for name in PROVIDER_BY_NAME}
        self._providers: Dict[str, ProviderState] = {
            name: ProviderState(name=name) for name in PROVIDER_BY_NAME
        }

    # -- halt handling ----------------------------------------------------
    @property
    def halted(self) -> bool:
        return self._halt.is_set()

    def halt(self, reason: str) -> None:
        with self._lock:
            if self._halt.is_set():
                return
            self._halt.set()
            self.stats.halted = True
            self.stats.halt_reason = reason
            self.stats.halted_at = now_iso()
            self.log.warn(f"GLOBAL HALT: {reason}")

    def raise_if_halted(self) -> None:
        if self._halt.is_set():
            raise GlobalHalt(self.stats.halt_reason or "halted")

    # -- provider circuit breakers ---------------------------------------
    def provider_state(self, name: str) -> ProviderState:
        return self._providers.setdefault(name, ProviderState(name=name))

    def provider_ready(self, name: str) -> Tuple[bool, str]:
        """``(ready, why)`` for one provider — cheap and side-effect free."""

        if not self._keys(name):
            return False, "no credentials"
        state = self.provider_state(name)
        remaining = state.open_until - monotonic()
        if remaining > 0:
            return False, f"cooling down for {remaining:.0f}s after {state.last_reason}"
        return True, "probe" if state.trips else "closed"

    def _record_trip(self, name: str, reason: str) -> None:
        """Open (or re-open) the circuit breaker with an increased cooldown."""

        base = max(1.0, float(self.policy.provider_cooldown_seconds))
        cap = max(base, float(self.policy.provider_cooldown_max_seconds))
        with self._lock:
            state = self.provider_state(name)
            state.trips += 1
            state.last_reason = reason
            cooldown = base if state.cooldown_seconds <= 0 else min(state.cooldown_seconds * 2, cap)
            state.cooldown_seconds = cooldown
            state.open_until = monotonic() + cooldown
            bucket = self.stats.per_provider.setdefault(name, {"ok": 0, "fail": 0})
            bucket["trips"] = bucket.get("trips", 0) + 1
        self.log.warn(
            f"provider {name}: rate-limit/exhaustion signal – circuit open for "
            f"{cooldown:.0f}s ({reason})"
        )

    def _close_circuit(self, name: str) -> None:
        with self._lock:
            state = self.provider_state(name)
            if state.trips or state.open_until:
                self.log.info(f"provider {name}: healthy again – circuit closed")
            state.trips = 0
            state.probes = 0
            state.cooldown_seconds = 0.0
            state.open_until = 0.0
            state.last_reason = ""

    def _note_probe(self, name: str) -> None:
        """Count a request sent to a previously tripped provider (half-open probe)."""

        with self._lock:
            state = self.provider_state(name)
            if state.trips:
                state.probes += 1

    def _reopen_probe(self, name: str) -> None:
        """A half-open probe failed for a non-rate-limit reason: keep it open."""

        with self._lock:
            state = self.provider_state(name)
            if state.cooldown_seconds > 0:
                state.open_until = monotonic() + state.cooldown_seconds

    # -- stats ------------------------------------------------------------
    def _record_success(self, provider: str) -> None:
        with self._lock:
            self.stats.requests += 1
            self.stats.successes += 1
            self.stats.consecutive_failures = 0
            bucket = self.stats.per_provider.setdefault(provider, {"ok": 0, "fail": 0})
            bucket["ok"] += 1

    def _record_failure(self, provider: str, rate_limited: bool, detail: str) -> None:
        with self._lock:
            self.stats.requests += 1
            self.stats.failures += 1
            self.stats.consecutive_failures += 1
            bucket = self.stats.per_provider.setdefault(provider, {"ok": 0, "fail": 0})
            bucket["fail"] += 1
            if rate_limited:
                bucket["rate_limited"] = bucket.get("rate_limited", 0) + 1
                self.stats.rate_limit_signals += 1
            consecutive = self.stats.consecutive_failures

        if consecutive >= self.policy.consecutive_failure_threshold:
            self.halt(
                f"{consecutive} consecutive failures (threshold "
                f"{self.policy.consecutive_failure_threshold}); last: {detail}"
            )

    # -- key pool ---------------------------------------------------------
    def _keys(self, provider_name: str) -> List[str]:
        pool = llm.provider_keys().get(provider_name, [])
        return pool

    def _next_key(self, provider_name: str) -> Optional[str]:
        keys = self._keys(provider_name)
        if not keys:
            return None
        with self._lock:
            index = self._key_cursor.get(provider_name, 0) % len(keys)
            self._key_cursor[provider_name] = (index + 1) % len(keys)
        return keys[index]

    def available(self) -> bool:
        return any(self._keys(name) for name in PROVIDER_BY_NAME)

    def provider_report(self) -> Dict[str, Any]:
        """Snapshot of every provider's breaker state (for logs/progress)."""

        moment = monotonic()
        return {name: state.as_dict(moment) for name, state in self._providers.items()}

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
        """Send one completion, honouring provider order, failover and the halt rule."""

        self.raise_if_halted()
        order = list(provider_order or [p["name"] for p in PROVIDERS])
        last_error: Optional[Exception] = None
        attempted: List[str] = []
        unavailable: Dict[str, str] = {}

        for name in order:
            provider = PROVIDER_BY_NAME.get(name)
            if provider is None:
                continue
            ready, why = self.provider_ready(name)
            if not ready:
                unavailable[name] = why
                continue
            api_key = self._next_key(name)
            if not api_key:
                unavailable[name] = "no credentials"
                continue

            extra = {"response_format": {"type": "json_object"}} if json_mode else None
            attempted.append(name)
            self._note_probe(name)
            try:
                result = llm.chat_completion(
                    base_url=provider["base_url"],
                    api_key=api_key,
                    model=model,
                    messages=messages,
                    timeout=self.policy.request_timeout_seconds,
                    temperature=temperature,
                    max_tokens=max_tokens,
                    extra=extra,
                    # reasoning models may truncate before writing ``content``;
                    # retry the same provider with a bigger budget instead of
                    # reporting a provider failure.
                    truncation_retries=self.policy.max_retries_per_request + 1,
                )
            except llm.LlmRateLimited as exc:
                detail = str(exc)
                if self.policy.halt_on_any_rate_limit_signal:
                    self._record_trip(name, detail)
                self._record_failure(name, True, detail)
                last_error = exc
                self.raise_if_halted()
                continue
            except llm.LlmError as exc:
                detail = str(exc)
                self._reopen_probe(name)
                self._record_failure(name, False, detail)
                last_error = exc
                self.raise_if_halted()
                continue
            self._close_circuit(name)
            self._record_success(name)
            result.provider = name
            return result

        if not attempted:
            summary = "; ".join(f"{name}: {why}" for name, why in unavailable.items()) or "none configured"
            if any("cooling down" in why for why in unavailable.values()):
                self.halt(f"all providers exhausted ({summary})")
            raise llm.LlmError(f"no provider available ({summary})")

        self.raise_if_halted()
        ready_report = {name: self.provider_ready(name) for name in order if name in PROVIDER_BY_NAME}
        healthy = [name for name, (ready, _why) in ready_report.items() if ready]
        if not healthy:
            summary = "; ".join(
                f"{name}: {unavailable.get(name) or why}"
                for name, (_ready, why) in ready_report.items()
            )
            self.halt(
                f"all providers exhausted after {len(attempted)} attempt(s): "
                f"tried {', '.join(attempted)}"
                + (f"; {summary}" if summary else "")
            )
            self.raise_if_halted()
        if last_error is not None:
            raise last_error
        raise llm.LlmError("no provider credentials configured")


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
