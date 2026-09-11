"""Small shared helpers: atomic writes, JSON/JSONL IO, normalisation, logging."""

from __future__ import annotations

import io
import json
import os
import re
import sys
import tempfile
import time
import unicodedata
from pathlib import Path
from typing import Any, Dict, Iterable, Iterator, List

from . import paths

# ---------------------------------------------------------------------------
# time
# ---------------------------------------------------------------------------


def now_iso() -> str:
    """UTC timestamp in ISO-8601 with a trailing ``Z`` (deterministic format)."""

    return time.strftime("%Y-%m-%dT%H:%M:%SZ", time.gmtime())


def monotonic() -> float:
    return time.monotonic()


# ---------------------------------------------------------------------------
# atomic file writes (temp file + os.replace)
# ---------------------------------------------------------------------------


def write_text(path: Path, text: str) -> Path:
    paths.ensure_dir(path.parent)
    fd, tmp = tempfile.mkstemp(dir=str(path.parent), prefix=".tmp-", suffix=path.suffix + ".part")
    try:
        with os.fdopen(fd, "w", encoding="utf-8", newline="\n") as fh:
            fh.write(text)
        os.replace(tmp, path)
    except BaseException:
        try:
            os.unlink(tmp)
        except OSError:
            pass
        raise
    return path


def write_json(path: Path, data: Any, *, indent: int = 2) -> Path:
    return write_text(path, json.dumps(data, ensure_ascii=False, indent=indent, sort_keys=False) + "\n")


def write_jsonl(path: Path, records: Iterable[Dict[str, Any]]) -> Path:
    buf = io.StringIO()
    for rec in records:
        buf.write(json.dumps(rec, ensure_ascii=False, sort_keys=True))
        buf.write("\n")
    return write_text(path, buf.getvalue())


def append_jsonl(path: Path, records: Iterable[Dict[str, Any]]) -> Path:
    paths.ensure_dir(path.parent)
    with path.open("a", encoding="utf-8", newline="\n") as fh:
        for rec in records:
            fh.write(json.dumps(rec, ensure_ascii=False, sort_keys=True))
            fh.write("\n")
    return path


def read_json(path: Path, default: Any = None) -> Any:
    if not path.is_file():
        return default
    with path.open("r", encoding="utf-8") as fh:
        return json.load(fh)


def read_jsonl(path: Path) -> Iterator[Dict[str, Any]]:
    if not path.is_file():
        return
    with path.open("r", encoding="utf-8") as fh:
        for line in fh:
            line = line.strip()
            if line:
                yield json.loads(line)


def file_sha256(path: Path) -> str:
    import hashlib

    h = hashlib.sha256()
    with path.open("rb") as fh:
        for chunk in iter(lambda: fh.read(1 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


# ---------------------------------------------------------------------------
# normalisation
# ---------------------------------------------------------------------------

_WS_RE = re.compile(r"\s+")
_NON_ALNUM_RE = re.compile(r"[^a-z0-9]+")


def norm_space(text: Any) -> str:
    """Collapse runs of whitespace and strip."""

    if text is None:
        return ""
    return _WS_RE.sub(" ", str(text)).strip()


def norm_key(text: Any) -> str:
    """Case/whitespace/punctuation-insensitive lookup key.

    ``"Profit and Loss"`` / ``"Profit & Loss"`` / ``"profit-loss"`` all become
    ``"profit and loss"``-style keys via the alias layer; this function only does
    the mechanical part (casefold, ``&`` -> ``and``, punctuation -> space).
    """

    value = norm_space(text).lower()
    if not value:
        return ""
    value = value.replace("&", " and ")
    value = value.replace("/", " ")
    value = value.replace("-", " ")
    value = value.replace(".", " ")
    value = _WS_RE.sub(" ", value)
    return value.strip()


def fold_key(text: Any) -> str:
    """Case/diacritic/whitespace-insensitive label key.

    Used wherever two labels must be recognised as the *same* label although
    they are spelled in different letter case or carry different Unicode
    composition (``"Abandon"`` / ``"abandon"`` / ``"ABANDON"`` -> ``abandon``).
    Punctuation is deliberately **kept** here (unlike :func:`norm_key`, which is
    the alias/taxonomy lookup key): the vocabulary tables must not merge
    ``"A bed of roses"`` with ``"a bed-of-roses"``.
    """

    value = norm_space(text)
    if not value:
        return ""
    value = unicodedata.normalize("NFKD", value)
    value = "".join(ch for ch in value if not unicodedata.combining(ch))
    return norm_space(value).casefold()


def tokens(text: Any, *, min_len: int = 1) -> List[str]:
    """Lower-cased alphanumeric tokens of *text* (punctuation becomes a break)."""

    return [t for t in _NON_ALNUM_RE.split(norm_key(text)) if len(t) >= min_len]


def contains_phrase(haystack: str, needle: str) -> bool:
    """Word-boundary-aware containment on normalised text.

    ``"ratio"`` is *not* contained in ``"mensuration"`` (the old raw substring
    test matched it, filing the concept *Mensuration* under the maths chapter
    *Ratio*), while ``"time and distance"`` **is** contained in
    ``"speed time and distance"``.
    """

    if not haystack or not needle:
        return False
    return f" {needle} " in f" {haystack} "


def slugify(text: Any, *, fallback: str = "unclassified") -> str:
    value = norm_space(text).lower()
    value = value.replace("&", " and ")
    value = _NON_ALNUM_RE.sub("-", value).strip("-")
    return value or fallback


def compact_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


# ---------------------------------------------------------------------------
# misc
# ---------------------------------------------------------------------------


def chunked(items: List[Any], size: int) -> Iterator[List[Any]]:
    if size <= 0:
        yield list(items)
        return
    for i in range(0, len(items), size):
        yield items[i : i + size]


# ---------------------------------------------------------------------------
# HTTP identity
# ---------------------------------------------------------------------------

#: The worker endpoints (agentrouter / jw-rotator / api.monid.ai) sit behind
#: Cloudflare, which answers the default ``Python-urllib/3.x`` user agent with
#: ``HTTP 403 error 1010`` ("browser integrity check").  Every worker call must
#: therefore send a browser user agent.  Override with ``PYQ_USER_AGENT``.
BROWSER_USER_AGENT = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/126.0.0.0 Safari/537.36"
)


def user_agent() -> str:
    """Browser user agent used for every HTTP call the agent makes."""

    return os.environ.get("PYQ_USER_AGENT", "").strip() or BROWSER_USER_AGENT


def pct(numerator: float, denominator: float) -> float:
    if not denominator:
        return 0.0
    return 100.0 * numerator / denominator


def human_int(value: int) -> str:
    return f"{value:,}"


class Log:
    """Minimal stderr logger (no third-party dependency)."""

    def __init__(self, name: str = "agent", *, quiet: bool = False) -> None:
        self.name = name
        self.quiet = quiet
        self._started = monotonic()

    def _emit(self, level: str, message: str) -> None:
        if self.quiet and level != "ERROR":
            return
        elapsed = monotonic() - self._started
        sys.stderr.write(f"[{elapsed:8.1f}s] {level:5s} {self.name}: {message}\n")
        sys.stderr.flush()

    def info(self, message: str) -> None:
        self._emit("INFO", message)

    def warn(self, message: str) -> None:
        self._emit("WARN", message)

    def error(self, message: str) -> None:
        self._emit("ERROR", message)

    def step(self, message: str) -> None:
        self._emit("STEP", message)
