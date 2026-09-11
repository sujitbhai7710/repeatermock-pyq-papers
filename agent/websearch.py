"""Monid TinyFish web search / fetch client with an on-disk cache.

Requests
--------
search::

    POST https://api.monid.ai/v1/run
    {"provider":"tinyfish","endpoint":"/search",
     "input":{"queryParams":{"query":"..."}}}

fetch::

    POST https://api.monid.ai/v1/run
    {"provider":"tinyfish","endpoint":"/fetch",
     "input":{"body":{"urls":[...],"format":"markdown"}}}

Auth is ``Authorization: Bearer $MONID_API_KEY``.  Every response is cached in
``state/webcache.json`` keyed by ``sha256(endpoint + sorted(payload))`` so a
re-run (and the GitHub Action's next run) never repeats a query.
"""

from __future__ import annotations

import hashlib
import json
import socket
import urllib.error
import urllib.request
from dataclasses import dataclass
from typing import Any, Dict, Iterable, List, Optional, Sequence

from . import paths
from .util import Log, now_iso, read_json, user_agent, write_json

BASE_URL = "https://api.monid.ai/v1/run"
PROVIDER = "tinyfish"
SEARCH_ENDPOINT = "/search"
FETCH_ENDPOINT = "/fetch"
FORMAT_MARKDOWN = "markdown"
MAX_CACHE_ENTRIES = 5000


class WebSearchError(RuntimeError):
    pass


@dataclass
class WebResult:
    ok: bool
    cached: bool
    endpoint: str
    data: Any = None
    error: str = ""

    def as_dict(self) -> Dict[str, Any]:
        return {"ok": self.ok, "cached": self.cached, "endpoint": self.endpoint, "data": self.data, "error": self.error}


class WebCache:
    def __init__(self, path: Optional[paths.Path] = None) -> None:
        self.path = path or paths.WEBCACHE_JSON
        data = read_json(self.path, default=None)
        if not isinstance(data, dict):
            data = {"version": 1, "created_at": now_iso(), "entries": {}}
        data.setdefault("entries", {})
        self.data = data

    def key(self, endpoint: str, payload: Dict[str, Any]) -> str:
        blob = json.dumps(
            {"endpoint": endpoint, "payload": payload}, sort_keys=True, ensure_ascii=False
        )
        return hashlib.sha256(blob.encode("utf-8")).hexdigest()

    def get(self, key: str) -> Optional[Any]:
        entry = self.data["entries"].get(key)
        if entry is None:
            return None
        return entry.get("response")

    def put(self, key: str, endpoint: str, payload: Dict[str, Any], response: Any) -> None:
        self.data["entries"][key] = {
            "ts": now_iso(),
            "endpoint": endpoint,
            "payload": payload,
            "response": response,
        }
        if len(self.data["entries"]) > MAX_CACHE_ENTRIES:
            keys = sorted(self.data["entries"], key=lambda k: self.data["entries"][k].get("ts", ""))
            for old in keys[: len(keys) - MAX_CACHE_ENTRIES]:
                self.data["entries"].pop(old, None)

    def save(self) -> paths.Path:
        self.data["updated_at"] = now_iso()
        return write_json(self.path, self.data)

    def stats(self) -> Dict[str, Any]:
        return {"entries": len(self.data["entries"]), "path": paths.rel(self.path)}


class TinyFishClient:
    """Thin wrapper over the Monid ``/v1/run`` endpoint."""

    def __init__(
        self,
        api_key: Optional[str] = None,
        *,
        cache: Optional[WebCache] = None,
        timeout: int = 60,
        log: Optional[Log] = None,
    ) -> None:
        import os

        self.api_key = (api_key if api_key is not None else os.environ.get("MONID_API_KEY", "")).strip()
        self.cache = cache or WebCache()
        self.timeout = timeout
        self.log = log or Log("websearch")
        self.requests = 0
        self.cache_hits = 0

    def available(self) -> bool:
        return bool(self.api_key)

    # -- transport --------------------------------------------------------
    def _run(self, endpoint: str, payload: Dict[str, Any]) -> WebResult:
        if not self.api_key:
            return WebResult(False, False, endpoint, error="MONID_API_KEY is not set")

        key = self.cache.key(endpoint, payload)
        cached = self.cache.get(key)
        if cached is not None:
            self.cache_hits += 1
            return WebResult(True, True, endpoint, data=cached)

        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        request = urllib.request.Request(BASE_URL, data=body, method="POST")
        request.add_header("Content-Type", "application/json")
        request.add_header("Authorization", f"Bearer {self.api_key}")
        request.add_header("Accept", "application/json")
        # Cloudflare fronts api.monid.ai: the default Python user agent gets
        # HTTP 403 error 1010, a browser user agent does not.
        request.add_header("User-Agent", user_agent())

        self.requests += 1
        try:
            with urllib.request.urlopen(request, timeout=self.timeout) as response:
                raw = response.read().decode("utf-8", errors="replace")
        except urllib.error.HTTPError as exc:
            detail = ""
            try:
                detail = exc.read().decode("utf-8", errors="replace")[:300]
            except Exception:  # noqa: BLE001
                pass
            return WebResult(False, False, endpoint, error=f"HTTP {exc.code}: {detail}")
        except (urllib.error.URLError, socket.timeout, TimeoutError, OSError) as exc:
            return WebResult(False, False, endpoint, error=f"transport error: {exc}")

        try:
            parsed = json.loads(raw)
        except json.JSONDecodeError:
            return WebResult(False, False, endpoint, error=f"invalid JSON: {raw[:200]}")

        self.cache.put(key, endpoint, payload, parsed)
        self.cache.save()
        return WebResult(True, False, endpoint, data=parsed)

    # -- public API -------------------------------------------------------
    def search(self, query: str) -> WebResult:
        payload = {
            "provider": PROVIDER,
            "endpoint": SEARCH_ENDPOINT,
            "input": {"queryParams": {"query": query}},
        }
        return self._run(SEARCH_ENDPOINT, payload)

    def fetch(self, urls: Sequence[str], *, fmt: str = FORMAT_MARKDOWN) -> WebResult:
        payload = {
            "provider": PROVIDER,
            "endpoint": FETCH_ENDPOINT,
            "input": {"body": {"urls": list(urls), "format": fmt}},
        }
        return self._run(FETCH_ENDPOINT, payload)

    def stats(self) -> Dict[str, Any]:
        return {
            "requests": self.requests,
            "cache_hits": self.cache_hits,
            "cache": self.cache.stats(),
            "available": self.available(),
        }


def search_many(queries: Iterable[str], *, client: Optional[TinyFishClient] = None) -> List[WebResult]:
    active = client or TinyFishClient()
    return [active.search(query) for query in queries]
