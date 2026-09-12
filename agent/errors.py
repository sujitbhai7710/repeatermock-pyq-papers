"""Append-only ledger of AI failures — ``state/errors.jsonl``.

An AI outage must never be silent.  Every phase that degrades to
``ai_unavailable`` writes *why* here, so ``python -m agent.cli errors`` can
answer "which route failed, with which kind of failure, in which phase" without
digging through a log that is gone by the next run.

One JSON object per line::

    {"ts": "2026-09-11T20:14:19Z", "phase": "grammar", "batch_id": "...",
     "provider": "tokenharbor", "model": "deepseek-v4-flash", "kind": "rate_limit",
     "http_status": 402, "attempt": 1, "message": "HTTP 402 from ... balance is at $0",
     "retryable": true, "item_ids": ["5e87..."], "scope": "route", "task": "grammar_rule"}

Guarantees:

* **append-only** — the ledger is only ever appended to (:func:`record`); the
  summary readers never rewrite it, so history survives across runs;
* **bounded rows** — ``message`` is truncated to :data:`MESSAGE_LIMIT` characters
  and ``item_ids`` to :data:`ITEM_ID_LIMIT` entries;
* **no secrets** — API keys, bearer tokens and query keys are redacted before a
  message is written, and full prompts are never accepted here (only the
  provider's own error text is logged);
* **never raises** — a broken/unwritable ledger must not take a run down, so
  every write is best-effort.

``kind`` is the compact vocabulary the CLI summarises over:
``auth`` | ``rate_limit`` | ``transport`` | ``bad_json`` | ``http`` | ``other``.
"""

from __future__ import annotations

import contextlib
import contextvars
import json
import os
import re
from collections import Counter, OrderedDict
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List, Optional, Sequence, Tuple

from . import paths
from .util import Log, append_jsonl, now_iso

#: the compact failure vocabulary (see module docstring)
KINDS: Tuple[str, ...] = ("auth", "rate_limit", "transport", "bad_json", "http", "other")

KIND_AUTH = "auth"
KIND_RATE_LIMIT = "rate_limit"
KIND_TRANSPORT = "transport"
KIND_BAD_JSON = "bad_json"
KIND_HTTP = "http"
KIND_OTHER = "other"

#: granularity of one row: a single route attempt, a whole batch, or a phase
SCOPE_ROUTE = "route"
SCOPE_BATCH = "batch"
SCOPE_PHASE = "phase"

#: ``message`` is cut here before it is written
MESSAGE_LIMIT = 300
#: at most this many qids are stored on one row (the batch is identified by its id)
ITEM_ID_LIMIT = 50

#: the credential shapes that must never reach the ledger
_REDACTIONS: Tuple[Tuple[re.Pattern, str], ...] = (
    (re.compile(r"\bsk-[A-Za-z0-9_\-]{4,}"), "sk-<redacted>"),
    (re.compile(r"\bBearer\s+[A-Za-z0-9._\-]{4,}", re.I), "Bearer <redacted>"),
    (
        re.compile(
            r"\b(api[_-]?key|apikey|token|access[_-]?token|secret)\b\s*[:=]\s*[\"']?[^\"'\s,}]{6,}",
            re.I,
        ),
        r"\1=<redacted>",
    ),
    (re.compile(r"([?&](?:key|api_key|access_token)=)[^&\s]+", re.I), r"\1<redacted>"),
    (re.compile(r"\b[A-Za-z0-9_\-]{40,}\b"), "<redacted-token>"),
)

#: statuses that mean "the credential is wrong", not "try again later"
_AUTH_STATUSES = (401, 403)
#: statuses that are a quota/balance/rate answer rather than a real HTTP error
_QUOTA_STATUSES = (402, 408, 429)
#: permanent provider answers: retrying the same key changes nothing
_PERMANENT_SIGNALS = (
    "balance",
    "insufficient",
    "quota",
    "invalid api key",
    "invalid_api_key",
    "no credit",
    "exceeded your current quota",
    "account suspended",
)


# ---------------------------------------------------------------------------
# context: which phase / batch is talking to the AI right now
# ---------------------------------------------------------------------------


@dataclass(frozen=True)
class AiContext:
    """The AI work item a failure belongs to (phase + batch + its questions)."""

    phase: str = ""
    task: str = ""
    batch_id: str = ""
    item_ids: Tuple[str, ...] = ()
    attempt: int = 0

    def with_attempt(self, attempt: int) -> "AiContext":
        return AiContext(
            phase=self.phase,
            task=self.task,
            batch_id=self.batch_id,
            item_ids=self.item_ids,
            attempt=attempt,
        )


#: a *context variable* (not a global), so parallel batch threads each see their
#: own phase/batch while calls made outside any binding still see the phase
#: default set by :func:`bind_phase`.
_CONTEXT: contextvars.ContextVar[AiContext] = contextvars.ContextVar("pyq_ai_context", default=AiContext())


def current_context() -> AiContext:
    """The context bound to the running code (empty when nothing is bound)."""

    return _CONTEXT.get()


def bind_phase(phase: str, *, task: str = "") -> "contextvars.Token":
    """Set the phase for every AI call made afterwards in this context."""

    return _CONTEXT.set(AiContext(phase=phase or "", task=task or ""))


def reset(token: "contextvars.Token") -> None:
    with contextlib.suppress(ValueError, LookupError):
        _CONTEXT.reset(token)


@contextlib.contextmanager
def ai_context(
    *,
    phase: str = "",
    task: str = "",
    batch_id: str = "",
    item_ids: Sequence[Any] = (),
) -> Iterator[AiContext]:
    """Bind one AI batch to *phase*/*batch_id*/*item_ids* for the block.

    Usable from a worker thread (each thread owns its context), which is how the
    parallel grammar batches stay distinguishable in the ledger.
    """

    parent = _CONTEXT.get()
    context = AiContext(
        phase=phase or parent.phase,
        task=task or parent.task,
        batch_id=str(batch_id or ""),
        item_ids=tuple(str(item) for item in (item_ids or ())),
    )
    token = _CONTEXT.set(context)
    try:
        yield context
    finally:
        with contextlib.suppress(ValueError, LookupError):
            _CONTEXT.reset(token)


# ---------------------------------------------------------------------------
# classification
# ---------------------------------------------------------------------------


def redact(message: Any) -> str:
    """Strip credential shapes and cut the text to :data:`MESSAGE_LIMIT`."""

    text = " ".join(str(message or "").split())
    for pattern, replacement in _REDACTIONS:
        text = pattern.sub(replacement, text)
    if len(text) > MESSAGE_LIMIT:
        text = text[: MESSAGE_LIMIT - 3].rstrip() + "..."
    return text


def status_of(exc: Optional[BaseException], *, status: Optional[int] = None) -> Optional[int]:
    """The HTTP status of a failure (``None`` for a transport-level error)."""

    if status is not None:
        return int(status)
    raw = getattr(exc, "status", None)
    if raw is None:
        return None
    try:
        return int(raw)
    except (TypeError, ValueError):
        return None


def classify(
    exc: Optional[BaseException] = None,
    *,
    status: Optional[int] = None,
    message: str = "",
    kind: Optional[str] = None,
) -> str:
    """Map a failure onto :data:`KINDS`.

    An explicit *kind* that is already in the vocabulary wins (the caller knows
    best, e.g. the grammar pass reporting a bad JSON reply).  Otherwise the
    signal order is: auth status -> rate limit/quota -> WAF/HTML answer ->
    invalid JSON -> transport -> any other HTTP status -> ``other``.
    """

    code = status_of(exc, status=status)
    text = f"{message or ''} {exc or ''}".lower()
    if kind and kind in KINDS:
        return kind
    if code in _AUTH_STATUSES:
        return KIND_AUTH
    if isinstance(exc, _rate_limited_type()) or code in _QUOTA_STATUSES:
        return KIND_RATE_LIMIT
    if any(signal in text for signal in _signals("RATE_LIMIT_SIGNALS", RATE_LIMIT_SIGNALS)):
        return KIND_RATE_LIMIT
    if any(signal in text for signal in _signals("WAF_SIGNALS", WAF_SIGNALS)):
        return KIND_HTTP
    if "invalid json" in text or "bad json" in text or "json decode" in text:
        return KIND_BAD_JSON
    if "transport error" in text or "timed out" in text or "timeout" in text:
        return KIND_TRANSPORT
    if code:
        return KIND_HTTP
    if "empty completion" in text:
        return KIND_BAD_JSON
    return KIND_OTHER


def is_retryable(kind: str, *, status: Optional[int] = None, message: str = "") -> bool:
    """Whether an identical retry could plausibly succeed.

    A wrong credential or an exhausted balance is **not** retryable: retrying it
    only burns the work window.
    """

    if kind == KIND_AUTH:
        return False
    text = (message or "").lower()
    if any(signal in text for signal in _PERMANENT_SIGNALS):
        return False
    if status == 402:
        return False
    return True


def _rate_limited_type() -> type:
    from . import llm

    return llm.LlmRateLimited


def _signals(name: str, fallback: Tuple[str, ...]) -> Tuple[str, ...]:
    """Rate-limit / WAF signals from :mod:`agent.llm`, plus local fallbacks.

    Imported lazily so this module never participates in an import cycle with the
    transport layer.
    """

    try:
        from . import llm

        return tuple(getattr(llm, name, ())) + fallback
    except Exception:  # pragma: no cover - llm is always importable in practice
        return fallback


#: fallbacks used when ``agent.llm`` does not define the list (kept in sync with
#: the transport layer's own vocabulary)
RATE_LIMIT_SIGNALS: Tuple[str, ...] = (
    "rate limit",
    "rate-limit",
    "rate_limited",
    "too many requests",
    "insufficient",
    "quota",
    "balance",
)
WAF_SIGNALS: Tuple[str, ...] = ("aliyun_waf", "doctype html", "cloudflare", "browser integrity")


# ---------------------------------------------------------------------------
# writing
# ---------------------------------------------------------------------------


def ledger_path() -> Path:
    """``state/errors.jsonl`` (the one ledger every phase appends to).

    ``PYQ_ERRORS_LEDGER`` redirects it — used by the tests so a suite run can
    never append to the real ledger.
    """

    override = os.environ.get("PYQ_ERRORS_LEDGER", "").strip()
    if override:
        return Path(override)
    return paths.STATE_DIR / "errors.jsonl"


def record(
    *,
    kind: Optional[str] = None,
    provider: str = "",
    model: str = "",
    http_status: Optional[int] = None,
    attempt: int = 0,
    message: str = "",
    retryable: Optional[bool] = None,
    item_ids: Sequence[Any] = (),
    batch_id: str = "",
    phase: str = "",
    task: str = "",
    scope: str = SCOPE_ROUTE,
    exc: Optional[BaseException] = None,
    path: Optional[Path] = None,
    log: Optional[Log] = None,
) -> Dict[str, Any]:
    """Append one failure to the ledger and return the row that was written.

    Everything not passed explicitly is taken from the bound :class:`AiContext`
    (:func:`ai_context` / :func:`bind_phase`).  Never raises: a ledger that
    cannot be written must not abort a run.
    """

    context = _CONTEXT.get()
    status = status_of(exc, status=http_status)
    text = message or (str(exc) if exc is not None else "")
    resolved_kind = classify(exc, status=status, message=text, kind=kind)
    row: Dict[str, Any] = OrderedDict(
        (
            ("ts", now_iso()),
            ("phase", phase or context.phase),
            ("batch_id", str(batch_id or context.batch_id)),
            ("provider", provider or ""),
            ("model", model or ""),
            ("kind", resolved_kind),
            ("http_status", status),
            ("attempt", int(attempt or context.attempt or 0)),
            (
                "message",
                redact(text),
            ),
            ("retryable", is_retryable(resolved_kind, status=status, message=text) if retryable is None else bool(retryable)),
            ("item_ids", [str(item) for item in list(item_ids or context.item_ids)[:ITEM_ID_LIMIT]]),
            ("scope", scope),
            ("task", task or context.task),
        )
    )
    target = Path(path) if path is not None else ledger_path()
    try:
        append_jsonl(target, [row])
    except OSError as error:  # pragma: no cover - only on a read-only/broken disk
        if log is not None:
            log.warn(f"could not append to the error ledger {target}: {error}")
    return row


def record_failure(
    exc: Optional[BaseException] = None,
    *,
    provider: str = "",
    model: str = "",
    attempt: int = 0,
    message: str = "",
    kind: Optional[str] = None,
    retryable: Optional[bool] = None,
    item_ids: Sequence[Any] = (),
    batch_id: str = "",
    phase: str = "",
    task: str = "",
    scope: str = SCOPE_ROUTE,
    path: Optional[Path] = None,
    log: Optional[Log] = None,
) -> Dict[str, Any]:
    """Record an *attempt* failure (the route/provider that just said no)."""

    return record(
        kind=kind,
        provider=provider,
        model=model,
        attempt=attempt,
        message=message,
        retryable=retryable,
        item_ids=item_ids,
        batch_id=batch_id,
        phase=phase,
        task=task,
        scope=scope,
        exc=exc,
        path=path,
        log=log,
    )


def record_unavailable(
    reason: str,
    *,
    provider: str = "",
    model: str = "",
    phase: str = "",
    task: str = "",
    item_ids: Sequence[Any] = (),
    batch_id: str = "",
    scope: str = SCOPE_PHASE,
    path: Optional[Path] = None,
    log: Optional[Log] = None,
) -> Dict[str, Any]:
    """Record the aggregate "no route could serve this work" verdict.

    This is the row that answers *why* a phase reported ``ai_unavailable`` when
    no single route attempt was logged (all routes skipped, no credentials, a
    global halt, ...).
    """

    return record(
        provider=provider,
        model=model,
        attempt=0,
        message=reason,
        retryable=False,
        item_ids=item_ids,
        batch_id=batch_id,
        phase=phase,
        task=task,
        scope=scope,
        path=path,
        log=log,
    )


# ---------------------------------------------------------------------------
# reading / summarising
# ---------------------------------------------------------------------------


def iter_errors(path: Optional[Path] = None) -> Iterator[Dict[str, Any]]:
    """Yield every ledger row, oldest first (a corrupt line is skipped)."""

    target = Path(path) if path is not None else ledger_path()
    if not target.is_file():
        return
    with target.open("r", encoding="utf-8") as handle:
        for line in handle:
            line = line.strip()
            if not line:
                continue
            try:
                row = json.loads(line)
            except ValueError:
                continue
            if isinstance(row, dict):
                yield row


def _share(value: int, total: int) -> float:
    return round(100.0 * value / total, 1) if total else 0.0


def summarise(top: int = 20, path: Optional[Path] = None) -> Dict[str, Any]:
    """Aggregate the ledger: totals, kinds, and the *top* phase/route groups.

    Each group carries its newest message, so the report says not only how often
    something failed but what the provider actually answered.
    """

    target = Path(path) if path is not None else ledger_path()
    rows = list(iter_errors(target))
    kinds: Counter = Counter()
    phases: Counter = Counter()
    groups: Dict[Tuple[str, str, str, str], Dict[str, Any]] = {}
    retryable = 0
    for row in rows:
        kind = str(row.get("kind") or KIND_OTHER)
        phase = str(row.get("phase") or "-")
        provider = str(row.get("provider") or "-")
        model = str(row.get("model") or "-")
        kinds[kind] += 1
        phases[phase] += 1
        if row.get("retryable"):
            retryable += 1
        key = (phase, kind, provider, model)
        bucket = groups.get(key)
        if bucket is None:
            bucket = {
                "phase": phase,
                "kind": kind,
                "provider": provider,
                "model": model,
                "count": 0,
                "first_ts": row.get("ts"),
                "last_ts": row.get("ts"),
                "retryable": 0,
                "http_status": row.get("http_status"),
                "message": row.get("message") or "",
                "item_ids": [],
            }
            groups[key] = bucket
        bucket["count"] += 1
        bucket["last_ts"] = row.get("ts")
        if row.get("retryable"):
            bucket["retryable"] += 1
        message = str(row.get("message") or "")
        if message:
            bucket["message"] = message
            bucket["http_status"] = row.get("http_status")
        if not bucket["item_ids"]:
            bucket["item_ids"] = [str(item) for item in (row.get("item_ids") or [])]

    ordered = sorted(groups.values(), key=lambda item: (-item["count"], item["phase"], item["provider"]))
    limit = max(1, int(top or 20))
    stamps = [str(row.get("ts") or "") for row in rows if row.get("ts")]
    return {
        "path": paths.rel(target),
        "exists": target.is_file(),
        "total": len(rows),
        "first_ts": min(stamps) if stamps else "",
        "last_ts": max(stamps) if stamps else "",
        "retryable": retryable,
        "not_retryable": len(rows) - retryable,
        "by_kind": dict(kinds.most_common()),
        "by_phase": dict(phases.most_common()),
        "top": ordered[:limit],
        "groups": len(ordered),
    }


def format_summary(summary: Dict[str, Any], *, width: int = 78) -> str:
    """Human report for ``python -m agent.cli errors``."""

    lines: List[str] = []
    add = lines.append
    add("=" * width)
    add("AI ERROR LEDGER")
    add("=" * width)
    add(f"ledger             : {summary.get('path')}")
    if not summary.get("exists"):
        add("")
        add("no ledger yet — every AI call the agent made has succeeded")
        add("=" * width)
        return "\n".join(lines)
    add(f"rows               : {summary.get('total', 0)}")
    add(f"window             : {summary.get('first_ts') or '-'} .. {summary.get('last_ts') or '-'}")
    add(f"retryable          : {summary.get('retryable', 0)}")
    add(f"not retryable      : {summary.get('not_retryable', 0)}")
    add("")
    add("by kind")
    by_kind = summary.get("by_kind") or {}
    if by_kind:
        for kind, count in by_kind.items():
            add(f"  {kind:12s} {count:6d}")
    else:
        add("  (none)")
    add("")
    add("by phase")
    by_phase = summary.get("by_phase") or {}
    if by_phase:
        for phase, count in by_phase.items():
            add(f"  {phase:12s} {count:6d}")
    else:
        add("  (none)")
    add("")
    top = summary.get("top") or []
    add(f"top {len(top)} group(s) of {summary.get('groups', 0)}")
    if top:
        add("  " + f"{'count':>6}  {'phase':8s} {'kind':11s} {'provider':16s} {'model':26s} retry")
        for group in top:
            add(
                "  {count:6d}  {phase:8s} {kind:11s} {provider:16s} {model:26s} {retry}".format(
                    count=group.get("count", 0),
                    phase=str(group.get("phase"))[:8],
                    kind=str(group.get("kind"))[:11],
                    provider=str(group.get("provider"))[:16],
                    model=str(group.get("model"))[:26],
                    retry=f"{group.get('retryable', 0)}/{group.get('count', 0)}",
                )
            )
            message = str(group.get("message") or "")
            if message:
                status = group.get("http_status")
                prefix = f"http {status}: " if status else ""
                add(f"          last: {prefix}{message[:150]}")
    else:
        add("  (none)")
    add("=" * width)
    return "\n".join(lines)
