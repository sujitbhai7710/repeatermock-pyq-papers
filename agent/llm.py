"""OpenAI-compatible chat client (stdlib only).

No key ever reaches a log line: keys are masked to ``<provider>#<n>`` in every
message.  Rate-limit / quota signals are turned into :class:`LlmRateLimited`,
which the router treats as a provider-level outage (circuit breaker + failover);
only a fully exhausted provider chain becomes a global halt.

Reasoning models (``deepseek-v4-flash``) may spend the whole completion budget
on ``reasoning_content`` and truncate before writing ``content`` — such a reply
is retried with a larger ``max_tokens`` (see :func:`chat_completion`) instead of
being reported as a provider failure.
"""

from __future__ import annotations

import json
import os
import re
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass, field
from typing import Any, Dict, List, Optional, Sequence

from .util import norm_space, user_agent

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


def provider_keys() -> Dict[str, List[str]]:
    """Collect credentials from the environment without ever logging them.

    * ``OPENAI_KEYS``   – comma separated key pool for the agentrouter provider
    * ``DEEPSEEK_KEYS`` – comma separated key pool for the jw provider
    * ``AR_PROXY_TOKEN`` / ``JW_PROXY_TOKEN`` – single bearer tokens
    """

    agentrouter = parse_keys(os.environ.get("OPENAI_KEYS"))
    ar_token = os.environ.get("AR_PROXY_TOKEN", "").strip()
    if ar_token:
        agentrouter.append(ar_token)

    jw = parse_keys(os.environ.get("DEEPSEEK_KEYS"))
    jw_token = os.environ.get("JW_PROXY_TOKEN", "").strip()
    if jw_token:
        jw.append(jw_token)

    return {"agentrouter": agentrouter, "jw": jw}


def keys_available() -> bool:
    return any(provider_keys().values())


def detect_rate_limit(status: Optional[int], body: str) -> bool:
    if status in RATE_LIMIT_STATUSES:
        return True
    lowered = (body or "").lower()
    return any(signal in lowered for signal in RATE_LIMIT_SIGNALS)


def _post_chat(
    *,
    base_url: str,
    payload: Dict[str, Any],
    api_key: str,
    timeout: int,
) -> tuple:
    """One ``POST /chat/completions``; returns ``(status, body)``.

    Raises :class:`LlmRateLimited` on a rate-limit signal and :class:`LlmError`
    for every other HTTP/transport failure.
    """

    url = base_url.rstrip("/") + "/chat/completions"
    data = json.dumps(payload).encode("utf-8")
    request = urllib.request.Request(url, data=data, method="POST")
    request.add_header("Content-Type", "application/json")
    request.add_header("Authorization", f"Bearer {api_key}")
    request.add_header("Accept", "application/json")
    # Cloudflare answers the stock Python user agent with 403 error 1010, so the
    # workers are called with a browser user agent (see agent.util.user_agent).
    request.add_header("User-Agent", user_agent())

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
) -> ChatResult:
    """POST ``/chat/completions`` and return the assistant text.

    Raises :class:`LlmRateLimited` when a rate-limit signal is seen and
    :class:`LlmError` for any other failure.

    When a reasoning model truncates before emitting any ``content``, the call
    is repeated with a larger ``max_tokens`` (at most *truncation_retries*
    extra attempts) instead of being reported as a provider failure.
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

        status, body = _post_chat(
            base_url=base_url, payload=payload, api_key=api_key, timeout=timeout
        )

        if detect_rate_limit(status, body):
            raise LlmRateLimited(f"HTTP {status}: {_short(body)}", status=status)

        try:
            parsed = json.loads(body)
        except json.JSONDecodeError as exc:
            raise LlmError(f"invalid JSON from {base_url}: {_short(body)}") from exc

        if isinstance(parsed, dict) and parsed.get("error"):
            message = json.dumps(parsed["error"])[:400]
            if detect_rate_limit(status, message):
                raise LlmRateLimited(message, status=status)
            raise LlmError(message, status=status)

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
