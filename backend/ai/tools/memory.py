"""
Memory tools — the AI's bounded write and read interface to its own memory.

These tools connect the implemented three-tier memory subsystem
(``backend/ai/memory``) to the AI tool surface for the first time:
``memory_store`` persists a fact (long or permanent tier), ``memory_list``
reads entries back. Both resolve the SAME manager instance the engine and
dispatcher already use — there is no second memory authority, no second
memory state.

The write path is bounded like the dispatcher's bounded read path
(``MEMORY_READ_TIMEOUT_S``): ``memory_store`` runs the synchronous
repository store through ``asyncio.to_thread`` with
``MEMORY_WRITE_TIMEOUT_S`` so a slow store degrades to an honest failure
instead of stalling the request.

Both tools are owner-scoped: entries are always written and read under
``context.owner_id`` — a caller can never touch another owner's memory.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.ai.memory.limits import MAX_MEMORY_ENTRY_CHARS, MEMORY_WRITE_TIMEOUT_S
from backend.ai.memory.types import MemoryCategory, MemoryQuery, MemoryTier
from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext

logger = logging.getLogger(__name__)

_VALID_CATEGORIES = ("fact", "preference", "context", "summary", "instruction")
_PERMANENT_CATEGORIES = frozenset({"fact", "preference", "instruction"})


def _resolve_memory_manager():
    """Resolve the engine-owned MemoryManager.

    Single authority: the manager built by ``Engine`` (repository-backed)
    and shared with the dispatcher's bounded read path. Falls back to a
    standalone in-memory manager only if the engine cannot be reached, so
    the tool still works in tool-only test harnesses.
    """
    try:
        from backend.ai.engine.engine import get_engine

        manager = get_engine().memory_manager
        if manager is not None:
            return manager
    except Exception as exc:  # noqa: BLE001
        logger.warning("memory tool: engine manager unavailable (%s); using in-memory fallback", exc)
    from backend.ai.memory.manager import MemoryManager

    return MemoryManager()


class MemoryStoreTool(Tool):
    """Persist a durable fact into long or permanent memory."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "memory_store"

    @property
    def description(self) -> str:
        return (
            "Store a fact about the owner in your long-term memory so you can "
            "recall it in later conversations. Use tier='permanent' only for "
            "stable, always-relevant facts (name, core preferences, standing "
            "instructions); use tier='long' for ordinary context and summaries. "
            "Never store secrets (passwords, tokens, API keys)."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "content": {
                "type": "string",
                "description": "The fact to remember, as one self-contained sentence.",
            },
            "tier": {
                "type": "string",
                "enum": ["long", "permanent"],
                "description": "Memory tier: 'long' (default, 90-day retention) or 'permanent' (always in prompt).",
            },
            "category": {
                "type": "string",
                "enum": list(_VALID_CATEGORIES),
                "description": "Semantic category: fact, preference, context, summary, or instruction.",
            },
            "importance": {
                "type": "number",
                "description": "Relevance score between 0.0 and 1.0 (higher = more relevant).",
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_WRITE

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult confirming the stored memory with its tier and category"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        content = str(arguments.get("content", "") or "").strip()
        if not content:
            return ToolResult(success=False, message="Missing content to remember.")
        if len(content) > MAX_MEMORY_ENTRY_CHARS:
            return ToolResult(
                success=False,
                message=(
                    f"Content is too long ({len(content)} chars; "
                    f"max {MAX_MEMORY_ENTRY_CHARS})."
                ),
            )

        tier_arg = str(arguments.get("tier", "long") or "long").strip().lower()
        if tier_arg not in ("long", "permanent"):
            return ToolResult(success=False, message=f"Unknown tier '{tier_arg}'. Use 'long' or 'permanent'.")

        category_arg = str(arguments.get("category", "") or "").strip().lower()
        if category_arg and category_arg not in _VALID_CATEGORIES:
            return ToolResult(
                success=False,
                message=f"Unknown category '{category_arg}'. Valid categories: {', '.join(_VALID_CATEGORIES)}.",
            )
        if tier_arg == "permanent" and category_arg and category_arg not in _PERMANENT_CATEGORIES:
            return ToolResult(
                success=False,
                message=(
                    f"Category '{category_arg}' is not valid for the permanent tier "
                    f"(use: {', '.join(sorted(_PERMANENT_CATEGORIES))})."
                ),
            )

        try:
            importance = float(arguments.get("importance", 0.5))
        except (TypeError, ValueError):
            return ToolResult(success=False, message="Importance must be a number between 0.0 and 1.0.")
        if not 0.0 <= importance <= 1.0:
            return ToolResult(success=False, message="Importance must be between 0.0 and 1.0.")

        category = MemoryCategory(category_arg) if category_arg else (
            MemoryCategory.FACT if tier_arg == "permanent" else MemoryCategory.SUMMARY
        )
        manager = _resolve_memory_manager()
        owner_id = context.owner_id
        try:
            entry = await asyncio.wait_for(
                asyncio.to_thread(
                    manager.store_permanent if tier_arg == "permanent" else manager.store_long,
                    owner_id,
                    content,
                    category,
                    importance,
                ),
                timeout=MEMORY_WRITE_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, asyncio.TimeoutError):
                return ToolResult(success=False, message="Storing the memory timed out — try again later.")
            return ToolResult(success=False, message=f"Memory store failed: {exc}")

        if entry is None:
            return ToolResult(
                success=False,
                message="The memory could not be stored (rejected or persistence failed).",
            )
        return ToolResult(
            success=True,
            message=f"Remembered ({tier_arg}, {category.value}): {content}",
            data={
                "id": entry.id,
                "tier": tier_arg,
                "category": category.value,
                "importance": importance,
            },
        )


class MemoryListTool(Tool):
    """List the AI's stored memories for the owner."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "memory_list"

    @property
    def description(self) -> str:
        return (
            "List your stored memories (what you already remember about the "
            "owner), optionally filtered by tier and a text query."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "tier": {
                "type": "string",
                "enum": ["long", "permanent"],
                "description": "Filter by tier. Omit to list both tiers.",
            },
            "query": {
                "type": "string",
                "description": "Optional text filter matched against memory content.",
            },
            "limit": {
                "type": "integer",
                "description": "Maximum entries per tier to return (1-20, default 10).",
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
        return "ToolResult with memory entries in message and structured data"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        tier_arg = str(arguments.get("tier", "") or "").strip().lower()
        if tier_arg and tier_arg not in ("long", "permanent"):
            return ToolResult(success=False, message=f"Unknown tier '{tier_arg}'. Use 'long', 'permanent', or omit it.")
        try:
            limit = int(arguments.get("limit", 10))
        except (TypeError, ValueError):
            return ToolResult(success=False, message="Limit must be an integer between 1 and 20.")
        limit = max(1, min(20, limit))
        query_text = str(arguments.get("query", "") or "").strip()

        manager = _resolve_memory_manager()
        owner_id = context.owner_id

        def _collect():
            permanent = manager.permanent.retrieve_all(owner_id)
            long_entries = manager.long.retrieve(
                MemoryQuery(
                    owner_id=owner_id,
                    tier=MemoryTier.LONG,
                    query_text=query_text,
                    limit=limit,
                )
            )
            return permanent, long_entries

        try:
            permanent, long_entries = await asyncio.wait_for(
                asyncio.to_thread(_collect),
                timeout=MEMORY_WRITE_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            if isinstance(exc, asyncio.TimeoutError):
                return ToolResult(success=False, message="Reading memories timed out — try again later.")
            return ToolResult(success=False, message=f"Memory list failed: {exc}")

        def _render(label: str, entries) -> tuple[str, list[str]]:
            rows: list[str] = []
            for entry in entries:
                if query_text and query_text.lower() not in entry.content.lower():
                    continue
                if entry.is_expired():
                    continue
                rows.append(f"  - [{entry.category.value}, importance={entry.importance:.1f}] {entry.content}")
            if not rows:
                return "", []
            return f"{label} ({len(rows)}):", rows

        include_permanent = tier_arg in ("", "permanent")
        include_long = tier_arg in ("", "long")
        permanent_header, permanent_rows = (
            _render("Permanent", permanent) if include_permanent else ("", [])
        )
        long_header, long_rows = _render("Long", long_entries) if include_long else ("", [])

        if not permanent_rows and not long_rows:
            scope = f" ({tier_arg})" if tier_arg else ""
            suffix = f" Matching '{query_text}'." if query_text else ""
            return ToolResult(
                success=True,
                message=f"No memories stored{scope}.{suffix}",
                data={"permanent": [], "long": []},
            )

        lines = ([permanent_header, *permanent_rows] if permanent_header else []) + (
            [long_header, *long_rows] if long_header else []
        )
        return ToolResult(
            success=True,
            message="Your memories:\n" + "\n".join(lines),
            data={"permanent": permanent_rows, "long": long_rows},
        )


__all__ = ["MemoryStoreTool", "MemoryListTool"]
