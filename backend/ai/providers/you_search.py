"""
YouSearchProvider — You.com Web Search API adapter (retrieval, not chat).

This is NOT an LLM provider: it never generates text. It queries

    POST https://ydc-index.io/v1/search        (header: X-API-Key)

and returns normalized structured results that the existing AI pipeline
reasons over via the ToolRegistry/ToolExecutor path.

Normalized result shape (the only contract callers see):

    {
        "success":  bool,
        "query":    str,
        "results": [
            {
                "kind":        "web" | "news",
                "title":       str,          # when provided
                "url":         str,          # when provided
                "description": str,          # when provided
                "snippets":    [str, ...],   # when provided
                "page_age":    str,          # news, when provided
            }, ...
        ],
        "metadata": {"search_uuid", "query", "latency"},   # passthrough
        "error":    str,      # "" on success; category-honest on failure
        "failure_type"/"http_status"/"retry_after" live in metadata so the
        ProviderManager can reuse its normal failure classification.
    }

Missing response fields are omitted/empty — nothing is fabricated.
The API key is read from ProviderConfig only and NEVER appears in logs,
errors, metadata, or telemetry.
"""
from __future__ import annotations

import logging
import re
import time
from typing import Any

import httpx

from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import (
    NOT_IMPLEMENTED,
    BaseProvider,
    ProviderResponse,
)
from backend.ai.providers.base.defaults import get_provider_default

logger = logging.getLogger(__name__)

_SEARCH_ENDPOINT = "/v1/search"
_FRESHNESS_SINGLE = frozenset({"day", "week", "month", "year"})
_FRESHNESS_RANGE = re.compile(r"^\d{4}-\d{2}-\d{2}to\d{4}-\d{2}-\d{2}$")
_MAX_COUNT = 100
_DEFAULT_COUNT = 10


class YouSearchProvider(BaseProvider):
    """Web-search capability backed by the You.com Search API."""

    PROVIDER_NAME = "you"
    PROVIDER_VERSION = "1.0.0"
    CAPABILITY_KIND = "web_search"

    def __init__(self, config: ProviderConfig | None = None) -> None:
        if config is None:
            config = get_provider_default("you")
        super().__init__(config)
        self._http_client: httpx.AsyncClient | None = None
        # A successful search is the ONLY proof of operational availability;
        # a configured key alone is reported as configured-but-unverified.
        self._last_success_monotonic: float | None = None

    # ── Capability identity ──

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_web_search=True)

    @property
    def display_name(self) -> str:
        return "You.com Search"

    # ── Lifecycle ──

    def initialize(self) -> None:
        pass

    def shutdown(self) -> None:
        self._http_client = None

    async def _get_client(self) -> httpx.AsyncClient:
        if self._http_client is None:
            self._http_client = httpx.AsyncClient(
                timeout=self._config.timeout or 30,
                headers={
                    "X-API-Key": self._config.api_key,
                    "Content-Type": "application/json",
                },
            )
        return self._http_client

    # ── Health ──

    def health(self) -> dict[str, Any]:
        if not self._config.api_key:
            return {
                "healthy": False,
                "provider": self.name,
                "kind": self.CAPABILITY_KIND,
                "configured": False,
                "enabled": bool(self._config.enabled),
                "reason": "no API key",
            }
        verified = self._last_success_monotonic is not None
        return {
            "healthy": verified and bool(self._config.enabled),
            "provider": self.name,
            "kind": self.CAPABILITY_KIND,
            "configured": True,
            "enabled": bool(self._config.enabled),
            # Honest wording: key present ≠ proven working.
            "reason": "" if verified else "configured — not verified yet",
        }

    # ── Non-LLM surface: chat is intentionally not supported ──

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        return ProviderResponse(
            text=NOT_IMPLEMENTED,
            provider_name=self.name,
            success=False,
            metadata={
                "reason": "web-search capability, not a chat provider",
                "failure_type": "request",
            },
        )

    def count_tokens(self, text: str) -> int:
        return max(1, len(text or "") // 4)

    # ── Search ──

    async def search(self, query: str, **kwargs: Any) -> dict[str, Any]:
        query = (query or "").strip()
        if not query:
            return self._error_result(query or "", "empty query", "request")

        try:
            count = int(kwargs.get("count") or _DEFAULT_COUNT)
        except (TypeError, ValueError):
            count = _DEFAULT_COUNT
        count = max(1, min(_MAX_COUNT, count))

        payload: dict[str, Any] = {"query": query, "count": count}
        domains = kwargs.get("include_domains")
        if isinstance(domains, (list, tuple, set)) and domains:
            cleaned = [str(d).strip() for d in domains if str(d).strip()]
            if cleaned:
                payload["include_domains"] = cleaned
        freshness = kwargs.get("freshness")
        if freshness:
            fresh = str(freshness).strip()
            if fresh in _FRESHNESS_SINGLE or _FRESHNESS_RANGE.match(fresh):
                payload["freshness"] = fresh

        url = f"{self._config.base_url.rstrip('/')}{_SEARCH_ENDPOINT}"
        client = await self._get_client()
        started = time.perf_counter()
        try:
            resp = await client.post(url, json=payload)
        except httpx.TimeoutException:
            logger.warning("%s: search timed out for %d-char query", self.name, len(query))
            return self._error_result(query, "request timed out", "timeout")
        except httpx.HTTPError as exc:
            # exc message may contain the URL/host — never credentials.
            logger.warning("%s: network failure during search: %s", self.name, type(exc).__name__)
            return self._error_result(query, f"network failure ({type(exc).__name__})", "network")

        latency = time.perf_counter() - started

        if resp.status_code == 429:
            try:
                retry_after = max(0.0, float(resp.headers.get("retry-after", "0")))
            except (TypeError, ValueError):
                retry_after = 0.0
            meta = {"failure_type": "rate_limited", "http_status": 429}
            if retry_after:
                meta["retry_after"] = retry_after
            result = self._error_result(query, "rate limited", "rate_limited")
            result["metadata"].update(meta)
            return result
        if resp.status_code in (401, 403):
            return self._error_result(query, f"unauthorized (HTTP {resp.status_code})",
                                      "auth", http_status=resp.status_code)
        if resp.status_code == 422:
            return self._error_result(query, "invalid search request (HTTP 422)",
                                      "request", http_status=422)
        if resp.status_code >= 500:
            return self._error_result(query, f"provider error (HTTP {resp.status_code})",
                                      "server", http_status=resp.status_code)
        if resp.status_code != 200:
            return self._error_result(query, f"unexpected HTTP {resp.status_code}",
                                      "request", http_status=resp.status_code)

        try:
            data = resp.json()
        except ValueError:
            return self._error_result(query, "malformed JSON response", "malformed")

        if not isinstance(data, dict):
            return self._error_result(query, "malformed response envelope", "malformed")

        raw_results = data.get("results") or {}
        results: list[dict[str, Any]] = []
        if isinstance(raw_results, dict):
            for entry in raw_results.get("web") or []:
                item = self._normalize_entry(entry, kind="web")
                if item:
                    results.append(item)
            for entry in raw_results.get("news") or []:
                item = self._normalize_entry(entry, kind="news")
                if item:
                    results.append(item)
        elif isinstance(raw_results, list):
            # Tolerate a flat list shape: treat every entry as a web result.
            for entry in raw_results:
                item = self._normalize_entry(entry, kind="web")
                if item:
                    results.append(item)

        raw_meta = data.get("metadata") or {}
        out_meta: dict[str, Any] = {}
        if isinstance(raw_meta, dict):
            for key in ("search_uuid", "query", "latency"):
                if raw_meta.get(key) is not None:
                    out_meta[key] = raw_meta[key]

        self._last_success_monotonic = time.monotonic()
        logger.info(
            "%s: search ok (%d results, %.2fs)", self.name, len(results), latency,
        )
        return {
            "success": True,
            "query": query,
            "results": results,
            "metadata": out_meta,
            "error": "",
        }

    # ── Normalization helpers ──

    @staticmethod
    def _normalize_entry(entry: Any, kind: str) -> dict[str, Any] | None:
        if not isinstance(entry, dict):
            return None
        item: dict[str, Any] = {"kind": kind}
        for src, dst in (("title", "title"), ("url", "url"),
                         ("description", "description"), ("page_age", "page_age")):
            value = entry.get(src)
            if isinstance(value, str) and value.strip():
                item[dst] = value.strip()
        snippets = entry.get("snippets")
        if isinstance(snippets, list):
            cleaned = [s.strip() for s in snippets if isinstance(s, str) and s.strip()]
            if cleaned:
                item["snippets"] = cleaned
        return item if len(item) > 1 else None

    @staticmethod
    def _error_result(query: str, error: str, failure_type: str,
                      http_status: int | None = None) -> dict[str, Any]:
        meta: dict[str, Any] = {"failure_type": failure_type}
        if http_status is not None:
            meta["http_status"] = http_status
        return {
            "success": False,
            "query": query,
            "results": [],
            "metadata": meta,
            "error": error,
        }
