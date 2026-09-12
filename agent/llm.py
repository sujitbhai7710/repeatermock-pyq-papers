"""Chat client for every configured route (stdlib only).

Two wire formats are supported:

``bearer``
    OpenAI Chat Completions — ``POST <base_url>/chat/completions`` with an
    ``Authorization: Bearer <key>`` header.  Used by ``agentrouter.org``, the
    ``ar-rotator`` worker and the ``jw-rotator`` worker.
``anthropic``
    Anthropic Messages — ``POST <base_url>/messages`` with ``x-api-key`` +
    ``anthropic-version``.  The OpenAI chat payload is translated to the
    Messages shape and the reply is translated back, so callers always see a
    :class:`ChatResult`.  Used by ``api.justwoker.icu``.

Every route also carries the *user agent* it requires: the workers sit behind
Cloudflare (browser UA) while the direct ``agentrouter.org`` endpoint answers
``cline/2.0.0`` (and rejects anything else with ``401 unauthorized client
detected``).

No key ever reaches a log line: keys are masked to a ``...abcd`` suffix in every
message.  Rate-limit / quota signals are turned into :class:`LlmRateLimited`,
which the router treats as a route-level outage (circuit breaker + failover);
only a fully exhausted route chain becomes a global halt.

Reasoning models (``deepseek-v4-flash``) may spend the whole completion budget
on ``reasoning_content`` and truncate before writing ``content`` — such a reply
is retried with a larger ``max_tokens`` (see :func:`chat_completion`) instead of
being reported as a route failure.
"""

from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence, Tuple

from .util import BROWSER_USER_AGENT, norm_space, user_agent

#: substrings (lower-cased) that indicate rate limiting or quota exhaustion
RATE_LIMIT_SIGNALS = (
    "rate limit",
    "rate-limit",
    "ratelimit",
    "too many requests",
    "quota",
    "exhausted",
    "insufficient_quota",
    "insufficient balance",
    "no available",
    "capacity",
    "overloaded",
    "temporarily unavailable",
    "try again later",
    "concurrency",
    "429",
)

#: HTTP statuses treated as a hard rate-limit / exhaustion signal
RATE_LIMIT_STATUSES = frozenset({402, 429, 503})

# ---------------------------------------------------------------------------
# providers (routes)
# ---------------------------------------------------------------------------

#: OpenAI-compatible ``POST /chat/completions`` with ``Authorization: Bearer``
AUTH_BEARER = "bearer"
#: Anthropic ``POST /messages`` with ``x-api-key`` + ``anthropic-version``
AUTH_ANTHROPIC = "anthropic"

ANTHROPIC_VERSION = "2023-06-01"

#: sentinel meaning "send the browser user agent" (``util.user_agent()``)
UA_BROWSER = "browser"
#: the direct agentrouter endpoint rejects every other user agent
CLINE_USER_AGENT = "cline/2.0.0"

#: Anthropic requires ``max_tokens``; used when the caller did not set one
DEFAULT_ANTHROPIC_MAX_TOKENS = 1024


@dataclass(frozen=True)
class Provider:
    """One route: where to send the request, how to authenticate, what to send as UA."""

    name: str
    base_url: str
    auth_style: str
    user_agent: str
    env_keys: Tuple[str, ...]
    label: str = ""
    #: relative request path (``""`` = the auth style's default)
    path: str = ""

    def user_agent_value(self) -> str:
        """The literal ``User-Agent`` header this route needs."""

        if self.user_agent == UA_BROWSER:
            return user_agent()
        if self.user_agent == CLINE_USER_AGENT:
            override = os.environ.get("PYQ_CLINE_USER_AGENT", "").strip()
            return override or CLINE_USER_AGENT
        return self.user_agent or BROWSER_USER_AGENT

    def request_path(self) -> str:
        if self.path:
            return self.path
        return "/messages" if self.auth_style == AUTH_ANTHROPIC else "/chat/completions"

    def describe(self) -> str:
        return f"{self.name} ({self.base_url}, {self.auth_style}, ua={self.user_agent})"


#: Ordered default route list — the order a request walks when nothing else is
#: configured.  ``agentrouter`` (direct) first, then the two workers, then the
#: direct Anthropic-compatible endpoint.
PROVIDERS: Tuple[Provider, ...] = (
    Provider(
        name="agentrouter",
        base_url="https://agentrouter.org/v1",
        auth_style=AUTH_BEARER,
        user_agent=CLINE_USER_AGENT,
        env_keys=("AGENTROUTER_KEYS", "OPENAI_KEYS"),
        label="agentrouter.org (direct)",
    ),
    Provider(
        name="ar-worker",
        base_url="https://ar-rotator.opencode-5a3.workers.dev/v1",
        auth_style=AUTH_BEARER,
        user_agent=UA_BROWSER,
        env_keys=("AR_PROXY_TOKEN",),
        label="ar-rotator worker",
    ),
    Provider(
        name="jw-worker",
        base_url="https://jw-rotator.opencode-5a3.workers.dev/v1",
        auth_style=AUTH_BEARER,
        user_agent=UA_BROWSER,
        env_keys=("JW_PROXY_TOKEN",),
        label="jw-rotator worker",
    ),
    Provider(
        name="justwoker",
        base_url="https://api.justwoker.icu/v1",
        auth_style=AUTH_ANTHROPIC,
        user_agent=UA_BROWSER,
        env_keys=("JUSTWOKER_KEYS", "DEEPSEEK_KEYS"),
        label="api.justwoker.icu (direct, Anthropic Messages API)",
    ),
)

PROVIDER_BY_NAME: Dict[str, Provider] = {p.name: p for p in PROVIDERS}

DEFAULT_PROVIDER_ORDER: Tuple[str, ...] = tuple(p.name for p in PROVIDERS)


def provider_from_spec(spec: Any) -> Provider:
    """Build a :class:`Provider` from a settings spec (mapping or object).

    The spec is duck-typed on purpose: :class:`agent.config.ProviderSpec` and the
    ``providers.routes`` entries of ``config/settings.json`` both work without
    making this module depend on :mod:`agent.config`.
    """

    if isinstance(spec, dict):
        get = spec.get
    else:
        def get(name: str, default: Any = None) -> Any:  # noqa: ANN001
            return getattr(spec, name, default)

    env_keys = tuple(str(env) for env in (get("env_keys") or ()))
    return Provider(
        name=str(get("name") or ""),
        base_url=str(get("base_url") or ""),
        auth_style=str(get("auth_style") or AUTH_BEARER),
        user_agent=str(get("user_agent") or UA_BROWSER),
        env_keys=env_keys,
        label=str(get("label") or get("name") or ""),
        path=str(get("path") or ""),
    )


def resolve_providers(settings: Any = None) -> Tuple[Provider, ...]:
    """The provider chain to use: ``settings.providers`` when present, else defaults."""

    specs = getattr(settings, "providers", None) if settings is not None else None
    if not specs:
        return PROVIDERS
    resolved: List[Provider] = []
    seen: set = set()
    for spec in specs:
        provider = provider_from_spec(spec)
        if not provider.name or not provider.base_url or provider.name in seen:
            continue
        seen.add(provider.name)
        resolved.append(provider)
    return tuple(resolved) or PROVIDERS


def provider_index(providers: Optional[Sequence[Provider]] = None) -> Dict[str, Provider]:
    return {p.name: p for p in (providers if providers is not None else PROVIDERS)}


@dataclass
class ChatMessage:
    role: str
    content: str

    def as_dict(self) -> Dict[str, str]:
        return {"role": self.role, "content": self.content}


@dataclass
class ChatResult:
    text: str
    model: str
    provider: str
    key_ref: str
    latency_ms: int
    usage: Dict[str, Any] = field(default_factory=dict)
    raw: Dict[str, Any] = field(default_factory=dict)

    def as_dict(self) -> Dict[str, Any]:
        return {
            "model": self.model,
            "provider": self.provider,
            "key_ref": self.key_ref,
            "latency_ms": self.latency_ms,
            "usage": self.usage,
        }


class LlmError(RuntimeError):
    """Transient or permanent failure that is not a rate-limit signal."""

    def __init__(self, message: str, *, status: Optional[int] = None) -> None:
        super().__init__(message)
        self.status = status


class LlmRateLimited(LlmError):
    """The provider is rate limited or out of quota – triggers a global halt."""


def _mask(key: str) -> str:
    if not key:
        return "<none>"
    return f"...{key[-4:]}" if len(key) > 8 else "***"


def parse_keys(value: Optional[str]) -> List[str]:
    """Split a keys environment variable (comma / whitespace / newline separated)."""

    if not value:
        return []
    out: List[str] = []
    for chunk in re.split(r"[,\s]+", value.strip()):
        chunk = chunk.strip().strip('"').strip("'")
        if chunk:
            out.append(chunk)
    return out


def provider_keys(providers: Optional[Sequence[Provider]] = None) -> Dict[str, List[str]]:
    """Collect credentials from the environment without ever logging them.

    Every provider declares the environment variables that make up its key pool
    (see :data:`PROVIDERS`); older variables keep working as aliases:

    * ``AGENTROUTER_KEYS`` – comma separated pool for the direct agentrouter
      endpoint (``OPENAI_KEYS`` is the historical alias)
    * ``AR_PROXY_TOKEN``   – bearer token for the ``ar-rotator`` worker
    * ``JW_PROXY_TOKEN``   – bearer token for the ``jw-rotator`` worker
    * ``JUSTWOKER_KEYS``   – comma separated pool for ``api.justwoker.icu``
      (``DEEPSEEK_KEYS`` is the historical alias)

    Values are deduplicated, order-preserving, and never written anywhere.
    """

    pools: Dict[str, List[str]] = {}
    for provider in providers if providers is not None else PROVIDERS:
        values: List[str] = []
        for env_name in provider.env_keys:
            for key in parse_keys(os.environ.get(env_name)):
                if key not in values:
                    values.append(key)
        pools[provider.name] = values
    return pools


def keys_available(providers: Optional[Sequence[Provider]] = None) -> bool:
    return any(provider_keys(providers).values())


def keys_env_names(providers: Optional[Sequence[Provider]] = None) -> List[str]:
    """Every environment variable that could carry a credential (for preflight)."""

    out: List[str] = []
    for provider in providers if providers is not None else PROVIDERS:
        for env_name in provider.env_keys:
            if env_name not in out:
                out.append(env_name)
    return out


def detect_rate_limit(status: Optional[int], body: str) -> bool:
    if status in RATE_LIMIT_STATUSES:
        return True
    if status not in (None, 200):
        lowered = (body or "").lower()
        return any(signal in lowered for signal in RATE_LIMIT_SIGNALS)
    # a 200 whose body is a well-formed chat completion is a success even when
    # the *content* mentions "rate limit" — a reasoning model can echo the words
    # inside its chain of thought (Groq's gpt-oss-120b emits a `reasoning`
    # field), and that must not look like a provider outage
    try:
        parsed = json.loads(body or "")
    except json.JSONDecodeError:
        parsed = None
    if isinstance(parsed, dict) and isinstance(parsed.get("choices"), list) and parsed["choices"]:
        return False
    lowered = (body or "").lower()
    return any(signal in lowered for signal in RATE_LIMIT_SIGNALS)


#: markers of an HTML challenge page — a WAF (Cloudflare / Aliyun) answered
#: instead of the API.  The live case: a runner gets
#: ``invalid JSON from https://agentrouter.org/v1: <!doctype html> ...
#: <meta name="aliyun_waf_aa" ...>`` while the same endpoint works from a normal
#: machine, so a bare "invalid JSON" hides the real cause.
WAF_SIGNALS = (
    "aliyun_waf",
    "doctype html",
    "browser integrity",
    "error 1010",
    "cf-error",
    "cloudflare",
    "waf_",
)


def failure_class(exc: BaseException) -> str:
    """Compact class of a route failure for the ``routes`` report (no secrets).

    ``ok`` / ``rate_limited:503`` / ``http_403`` / ``waf_html`` / ``bad_json`` /
    ``transport`` / ``empty_completion`` / ``error``.
    """

    text = str(exc).lower()
    status = getattr(exc, "status", None)
    if any(signal in text for signal in WAF_SIGNALS):
        return f"waf_html:{status}" if status else "waf_html"
    if isinstance(exc, LlmRateLimited):
        return f"rate_limited:{status}" if status else "rate_limited"
    if status:
        return f"http_{status}"
    if "invalid json" in text:
        return "bad_json"
    if "transport error" in text:
        return "transport"
    if "empty completion" in text:
        return "empty_completion"
    return "error"


def _auth_headers(api_key: str, auth_style: str, ua: str) -> Dict[str, str]:
    headers = {
        "Content-Type": "application/json",
        "Accept": "application/json",
        # Cloudflare answers the stock Python user agent with 403 error 1010, so
        # the workers are called with a browser user agent (agent.util) while the
        # direct agentrouter endpoint requires ``cline/<version>``.
        "User-Agent": ua,
    }
    if auth_style == AUTH_ANTHROPIC:
        headers["x-api-key"] = api_key
        headers["anthropic-version"] = ANTHROPIC_VERSION
    else:
        headers["Authorization"] = f"Bearer {api_key}"
    return headers


def _post_chat(
    *,
    base_url: str,
    payload: Dict[str, Any],
    api_key: str,
    timeout: int,
    auth_style: str = AUTH_BEARER,
    user_agent_value: Optional[str] = None,
    path: Optional[str] = None,
) -> tuple:
    """One completion POST; returns ``(status, body)``.

    Raises :class:`LlmRateLimited` on a rate-limit signal and :class:`LlmError`
    for every other HTTP/transport failure.
    """

    suffix = path or ("/messages" if auth_style == AUTH_ANTHROPIC else "/chat/completions")
    url = base_url.rstrip("/") + suffix
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    for name, value in _auth_headers(api_key, auth_style, user_agent_value or user_agent()).items():
        request.add_header(name, value)

    try:
        with urllib.request.urlopen(request, timeout=timeout) as response:
            body = response.read().decode("utf-8", errors="replace")
            return response.status, body
    except urllib.error.HTTPError as exc:  # noqa: PERF203
        body = ""
        try:
            body = exc.read().decode("utf-8", errors="replace")
        except Exception:  # noqa: BLE001
            pass
        if detect_rate_limit(exc.code, body):
            raise LlmRateLimited(
                f"HTTP {exc.code} from {base_url}: {_short(body)}", status=exc.code
            ) from exc
        raise LlmError(f"HTTP {exc.code} from {base_url}: {_short(body)}", status=exc.code) from exc
    except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
        raise LlmError(f"transport error to {base_url}: {exc}") from exc


# ---------------------------------------------------------------------------
# Anthropic Messages translation
# ---------------------------------------------------------------------------


def anthropic_payload(payload: Dict[str, Any]) -> Dict[str, Any]:
    """Translate an OpenAI chat payload to the Anthropic Messages shape.

    ``system`` messages become the top-level ``system`` string, ``user`` /
    ``assistant`` messages are kept in order, and ``max_tokens`` is required by
    the API (a default is substituted when the caller did not set one).
    ``response_format`` has no Anthropic equivalent and is dropped — the debate
    prompts already demand JSON-only replies.
    """

    system_parts: List[str] = []
    messages: List[Dict[str, str]] = []
    for message in payload.get("messages") or []:
        if not isinstance(message, dict):
            continue
        role = str(message.get("role") or "user")
        content = message.get("content")
        if not isinstance(content, str):
            content = json.dumps(content)
        if role == "system":
            system_parts.append(content)
            continue
        messages.append({"role": "assistant" if role == "assistant" else "user", "content": content})

    translated: Dict[str, Any] = {
        "model": payload.get("model"),
        "messages": messages or [{"role": "user", "content": ""}],
        "max_tokens": int(payload.get("max_tokens") or DEFAULT_ANTHROPIC_MAX_TOKENS),
    }
    if system_parts:
        translated["system"] = "\n\n".join(part for part in system_parts if part)
    temperature = payload.get("temperature")
    if temperature is not None:
        # Anthropic accepts 0.0-1.0; the agent always asks for 0.0
        translated["temperature"] = max(0.0, min(1.0, float(temperature)))
    return translated


def anthropic_to_openai(parsed: Dict[str, Any], model: str) -> Dict[str, Any]:
    """Translate an Anthropic Messages reply back to the OpenAI chat shape."""

    text = ""
    blocks = parsed.get("content")
    if isinstance(blocks, list):
        text = "\n".join(
            str(block.get("text") or "")
            for block in blocks
            if isinstance(block, dict) and block.get("type") in (None, "text")
        ).strip()
    elif isinstance(blocks, str):
        text = blocks.strip()

    usage_in = parsed.get("usage") if isinstance(parsed.get("usage"), dict) else {}
    prompt_tokens = usage_in.get("input_tokens")
    completion_tokens = usage_in.get("output_tokens")
    usage: Dict[str, Any] = {
        "prompt_tokens": prompt_tokens,
        "completion_tokens": completion_tokens,
        "total_tokens": (prompt_tokens or 0) + (completion_tokens or 0)
        if prompt_tokens is not None or completion_tokens is not None
        else None,
    }
    return {
        "model": parsed.get("model") or model,
        "choices": [
            {
                "finish_reason": parsed.get("stop_reason") or "stop",
                "message": {"role": "assistant", "content": text},
            }
        ],
        "usage": {k: v for k, v in usage.items() if v is not None},
    }


def error_message(parsed: Any) -> str:
    """Human-readable error text from an OpenAI *or* Anthropic error body."""

    if not isinstance(parsed, dict):
        return ""
    error = parsed.get("error")
    if isinstance(error, dict):
        return str(error.get("message") or json.dumps(error))[:400]
    if isinstance(error, str):
        return error[:400]
    if parsed.get("type") == "error":
        return json.dumps(parsed)[:400]
    return ""


#: hard cap for the escalated retry budget of a truncated reasoning reply
MAX_ESCALATED_TOKENS = 16384


def _truncated_without_text(parsed: Any) -> bool:
    """True when the completion hit the token cap before writing any content.

    ``deepseek-v4-flash`` is a reasoning model: it streams its chain of thought
    into ``reasoning_content`` and only then writes ``content``.  With a small
    ``max_tokens`` the whole budget can be spent on the reasoning and the
    message comes back with ``content == ""``, ``finish_reason == "length"`` —
    a client-side truncation, not a provider failure.
    """

    if not isinstance(parsed, dict):
        return False
    choices = parsed.get("choices")
    if not isinstance(choices, list) or not choices:
        return False
    first = choices[0]
    if not isinstance(first, dict) or str(first.get("finish_reason") or "") != "length":
        return False
    message = first.get("message")
    if not isinstance(message, dict):
        return False
    content = message.get("content")
    return not (isinstance(content, str) and content.strip())


def _escalated_budget(current: Optional[int]) -> int:
    base = int(current or 700)
    return min(max(base * 4, 4096), MAX_ESCALATED_TOKENS)


def chat_completion(
    *,
    base_url: str,
    api_key: str,
    model: str,
    messages: Sequence[ChatMessage],
    timeout: int = 120,
    temperature: float = 0.0,
    max_tokens: Optional[int] = None,
    extra: Optional[Dict[str, Any]] = None,
    truncation_retries: int = 0,
    auth_style: str = AUTH_BEARER,
    user_agent_value: Optional[str] = None,
    path: Optional[str] = None,
) -> ChatResult:
    """POST one completion and return the assistant text.

    *auth_style* selects the wire format: ``bearer`` posts the payload as-is to
    ``/chat/completions``, ``anthropic`` translates it to the Messages shape for
    ``/messages`` and translates the reply back.  *user_agent_value* is the
    ``User-Agent`` header the route requires (the workers need a browser UA, the
    direct agentrouter endpoint needs ``cline/<version>``).

    Raises :class:`LlmRateLimited` when a rate-limit signal is seen and
    :class:`LlmError` for any other failure.

    When a reasoning model truncates before emitting any ``content``, the call
    is repeated with a larger ``max_tokens`` (at most *truncation_retries*
    extra attempts) instead of being reported as a route failure.
    """

    import time

    budget = max_tokens
    attempt = 0
    started = time.monotonic()
    while True:
        payload: Dict[str, Any] = {
            "model": model,
            "messages": [m.as_dict() for m in messages],
            "temperature": temperature,
        }
        if budget:
            payload["max_tokens"] = budget
        if extra:
            payload.update(extra)
        wire = anthropic_payload(payload) if auth_style == AUTH_ANTHROPIC else payload

        status, body = _post_chat(
            base_url=base_url,
            payload=wire,
            api_key=api_key,
            timeout=timeout,
            auth_style=auth_style,
            user_agent_value=user_agent_value,
            path=path,
        )

        if detect_rate_limit(status, body):
            raise LlmRateLimited(f"HTTP {status}: {_short(body)}", status=status)

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LlmError(f"invalid JSON from {base_url}: {_short(body)}") from exc

        if isinstance(parsed, dict) and parsed.get("error"):
            message = error_message(parsed) or json.dumps(parsed["error"])[:400]
            if detect_rate_limit(status, message):
                raise LlmRateLimited(message, status=status)
            raise LlmError(message, status=status)

        if auth_style == AUTH_ANTHROPIC and isinstance(parsed, dict):
            parsed = anthropic_to_openai(parsed, model)

        text = extract_text(parsed)
        if not text:
            if attempt < truncation_retries and _truncated_without_text(parsed):
                attempt += 1
                budget = _escalated_budget(budget)
                continue
            raise LlmError(
                "empty completion"
                + (" (reasoning truncated at max_tokens)" if budget else ""),
                status=status,
            )

        latency_ms = int((time.monotonic() - started) * 1000)
        usage = {}
        if isinstance(parsed, dict) and isinstance(parsed.get("usage"), dict):
            usage = parsed["usage"]
        return ChatResult(
            text=text,
            model=model,
            provider="",
            key_ref=_mask(api_key),
            latency_ms=latency_ms,
            usage=usage,
            raw=parsed if isinstance(parsed, dict) else {},
        )


def extract_text(parsed: Dict[str, Any]) -> str:
    choices = parsed.get("choices")
    if isinstance(choices, list) and choices:
        first = choices[0]
        if isinstance(first, dict):
            message = first.get("message")
            if isinstance(message, dict):
                content = message.get("content")
                if isinstance(content, str):
                    return content.strip()
                if isinstance(content, list):
                    parts = [
                        str(part.get("text") or "")
                        for part in content
                        if isinstance(part, dict)
                    ]
                    return "\n".join(p for p in parts if p).strip()
            if isinstance(first.get("text"), str):
                return first["text"].strip()
    if isinstance(parsed.get("output_text"), str):
        return parsed["output_text"].strip()
    return ""


def _short(body: str, limit: int = 300) -> str:
    value = norm_space(body)
    return value[:limit]


# ---------------------------------------------------------------------------
# JSON helpers
# ---------------------------------------------------------------------------

FENCE_RE = re.compile(r"```(?:json)?\s*(.+?)```", re.S)


def extract_json(text: str) -> Optional[Any]:
    """Best-effort JSON extraction from a model reply."""

    if not text:
        return None
    fenced = FENCE_RE.search(text)
    candidates: List[str] = []
    if fenced:
        candidates.append(fenced.group(1))
    candidates.append(text)
    for candidate in candidates:
        candidate = candidate.strip()
        try:
            return json.loads(candidate)
        except json.JSONDecodeError:
            pass
        start = candidate.find("{")
        end = candidate.rfind("}")
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue
        start = candidate.find("[")
        end = candidate.rfind("]")
        if start != -1 and end > start:
            try:
                return json.loads(candidate[start : end + 1])
            except json.JSONDecodeError:
                continue
    return None


def dumps_compact(value: Any, limit: int = 0) -> str:
    text = json.dumps(value, ensure_ascii=False, separators=(",", ":"), sort_keys=True)
    if limit and len(text) > limit:
        return text[:limit]
    return text
