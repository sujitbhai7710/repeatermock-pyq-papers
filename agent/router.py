"""Provider routing + the GLOBAL HALT rule.

Provider order
--------------
1. **agentrouter ar-rotator** – ``https://ar-rotator.opencode-5a3.workers.dev/v1``
   (models ``deepseek-v4-flash`` / ``gpt-5.6-sol``)
2. **jw-rotator** – ``https://jw-rotator.opencode-5a3.workers.dev/v1`` (fallback)

Keys come from the environment only (``OPENAI_KEYS``, ``DEEPSEEK_KEYS``,
``AR_PROXY_TOKEN``, ``JW_PROXY_TOKEN``); they are never hardcoded and never
logged.

GLOBAL HALT
-----------
* every failure increments a consecutive-failure counter;
* **one** success resets it to zero;
* at ``rate_limit.consecutive_failure_threshold`` (default 10) consecutive
  failures, **or as soon as any worker response carries a rate-limit /
  exhaustion signal**, the router sets a global halt flag, refuses all further
  requests, lets the in-flight item finish, and the caller checkpoints and exits
  with ``status=rate_limited``.
"""

from __future__ import annotations

import threading
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from . import llm
from .config import Settings
from .util import Log, now_iso

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
    """Thread-safe router with key rotation, fallback and the global halt."""

    def __init__(self, settings: Settings, *, log: Optional[Log] = None) -> None:
        self.settings = settings
        self.log = log or Log("router")
        self.policy = settings.rate_limit
        self.stats = RouterStats()
        self._lock = threading.RLock()
        self._halt = threading.Event()
        self._key_cursor: Dict[str, int] = {name: 0 for name in PROVIDER_BY_NAME}

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
            consecutive = self.stats.consecutive_failures
            if rate_limited:
                self.stats.rate_limit_signals += 1

        if rate_limited and self.policy.halt_on_any_rate_limit_signal:
            self.halt(f"rate-limit/exhaustion signal from {provider}: {detail}")
            return
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
        """Send one completion, honouring provider order and the halt rule."""

        self.raise_if_halted()
        order = list(provider_order or [p["name"] for p in PROVIDERS])
        last_error: Optional[Exception] = None

        for name in order:
            provider = PROVIDER_BY_NAME.get(name)
            if provider is None:
                continue
            api_key = self._next_key(name)
            if not api_key:
                continue
            extra = {"response_format": {"type": "json_object"}} if json_mode else None
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
                )
            except llm.LlmRateLimited as exc:
                self._record_failure(name, True, str(exc))
                last_error = exc
                self.raise_if_halted()
                continue
            except llm.LlmError as exc:
                self._record_failure(name, False, str(exc))
                last_error = exc
                self.raise_if_halted()
                continue
            result.provider = name
            self._record_success(name)
            return result

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
