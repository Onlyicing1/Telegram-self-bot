"""
Web search service — You.com Search results for the AI pipeline.

Thin bridge between the ToolRegistry and the ProviderManager's retrieval
capability. The provider never touches Telegram; this module only formats
the normalized result into text the reasoning model can cite honestly.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

_MAX_LISTED = 8
_MAX_SNIPPET = 220


async def do_web_search(
    query: str,
    count: int = 10,
    freshness: str | None = None,
    include_domains: list[str] | None = None,
    provider_manager: Any | None = None,
) -> tuple[bool, str, dict[str, Any]]:
    """Execute a web search via the existing provider architecture.

    Returns ``(success, human_readable_text, normalized_data)``. Source
    URLs are preserved verbatim; nothing is fabricated when fields are
    missing.
    """
    if provider_manager is None:
        from backend.ai.engine.engine import get_engine

        engine = get_engine()
        if engine is None:
            return False, "❌ Web search unavailable — AI engine is not running.", {}
        provider_manager = engine.provider_manager

    try:
        result = await provider_manager.web_search(
            query,
            count=count,
            freshness=freshness,
            include_domains=include_domains,
        )
    except Exception as exc:
        logger.warning("web_search: manager call failed: %s", type(exc).__name__)
        return False, "❌ Web search failed.", {}

    if not isinstance(result, dict):
        return False, "❌ Web search returned an invalid result.", {}

    if not result.get("success"):
        error = str(result.get("error") or "unknown error")
        return False, f"⚠️ Web search failed: {error}", dict(result)

    return True, _format_results(result), dict(result)


def _format_results(result: dict[str, Any]) -> str:
    query = str(result.get("query") or "")
    items = [r for r in (result.get("results") or []) if isinstance(r, dict)]
    lines = [f'🌐 Web results for "{query}":', ""]
    if not items:
        lines.append("_No results found._")
        return "\n".join(lines)

    web = [r for r in items if r.get("kind") != "news"]
    news = [r for r in items if r.get("kind") == "news"]

    def _entry(r: dict[str, Any], idx: int) -> list[str]:
        title = str(r.get("title") or "(untitled)")
        url = str(r.get("url") or "")
        head = f"{idx}. [{r.get('kind', 'web')}] {title}"
        if url:
            head += f"\n   {url}"
        body = str(r.get("description") or "")
        snippets = r.get("snippets") or []
        if not body and snippets:
            body = str(snippets[0])
        if body:
            body = body[:_MAX_SNIPPET].replace("\n", " ")
            head += f"\n   {body}"
        age = r.get("page_age")
        if age:
            head += f"\n   Published: {age}"
        return [head, ""]

    idx = 1
    for r in web[:_MAX_LISTED]:
        lines.extend(_entry(r, idx))
        idx += 1
    for r in news[: max(0, _MAX_LISTED - len(web))]:
        lines.extend(_entry(r, idx))
        idx += 1

    shown = min(len(items), _MAX_LISTED)
    if len(items) > shown:
        lines.append(f"_…and {len(items) - shown} more results._")
    meta = result.get("metadata") or {}
    latency = meta.get("latency")
    tail = f"({shown} of {len(items)} results"
    if isinstance(latency, (int, float)):
        tail += f" · {latency:.2f}s"
    lines.append(tail + ") — cite sources when you use them.")
    return "\n".join(lines).rstrip()
