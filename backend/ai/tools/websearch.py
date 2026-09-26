"""
Web search tool — lets the AI fetch fresh web/news results.

The AI invokes this when a request needs current information. The tool is
a thin wrapper over ``web_search_service`` which routes through the
existing ProviderManager retrieval capability (You.com Search). The
model reasons over the returned sources; the Self Bot remains the only
execution authority.
"""
from __future__ import annotations

from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext


def _top_result_projection(data: dict[str, Any]) -> dict[str, str]:
    """Bounded, chainable identity of the FIRST search result.

    A whole result list is not a bounded value, so only the first result's
    title and URL are exposed as top-level ``data`` keys — the fields a later
    action in the same durable chain may reference. A missing field is
    omitted, never invented; the full result list stays where it always was.
    """
    results = data.get("results")
    if not isinstance(results, list):
        return {}
    for item in results:
        if not isinstance(item, dict):
            continue
        projection: dict[str, str] = {}
        title = item.get("title")
        url = item.get("url")
        if isinstance(title, str) and title.strip():
            projection["top_title"] = title
        if isinstance(url, str) and url.strip():
            projection["top_url"] = url
        return projection
    return {}


class WebSearchTool(Tool):
    """Search the live web for current information via You.com."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "web_search"

    @property
    def required_arguments(self) -> tuple[str, ...]:
        return ("query",)

    @property
    def description(self) -> str:
        return (
            "Search the live web for current information (news, prices, "
            "versions, recent events). Use when the answer depends on "
            "up-to-date facts you cannot know from training data."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "query": {
                "type": "string",
                "description": "Web search query.",
            },
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": 100,
                "default": 10,
                "description": "Number of results to request.",
            },
            "freshness": {
                "type": "string",
                "enum": ["day", "week", "month", "year"],
                "description": "Restrict results to a recent window.",
            },
            "include_domains": {
                "type": "array",
                "items": {"type": "string"},
                "description": "Only include results from these domains.",
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_ONLY

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return (
            "ToolResult with formatted web/news results in message and the "
            "normalized source list (kind/title/url/description/snippets/"
            "page_age) in data, plus top_title/top_url for the first result"
        )

    @property
    def consumable_output_fields(self) -> tuple[str, ...]:
        return ("top_title", "top_url")

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import web_search_service

        query = str(arguments.get("query") or "").strip()
        if not query:
            return ToolResult(success=False, message="Missing query argument.")

        try:
            count = int(arguments.get("count") or 10)
        except (TypeError, ValueError):
            count = 10
        count = max(1, min(100, count))

        freshness = arguments.get("freshness")
        if freshness not in ("day", "week", "month", "year"):
            freshness = None
        domains = arguments.get("include_domains")
        if not isinstance(domains, list):
            domains = None

        runtime_manager = None
        if context is not None and context.extra:
            runtime_manager = context.extra.get("provider_manager")

        try:
            ok, text, data = await web_search_service.do_web_search(
                query, count=count, freshness=freshness,
                include_domains=domains,
                provider_manager=runtime_manager,
            )
        except Exception as exc:
            reason = web_search_service.sanitize_reason(exc)
            return ToolResult(success=False, message=f"❌ Web search failed: {reason}.")
        if ok and isinstance(data, dict):
            data = {**data, **_top_result_projection(data)}
        return ToolResult(success=ok, message=text, data=data)
