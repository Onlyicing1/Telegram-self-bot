"""
ToolRegistry — the single public access point for all AI tool operations.

The registry stores tool instances by name and provides lookup, listing,
and existence checking. It is constructed once by the runtime supervisor
and injected wherever needed. No globals, no singletons.

Public API:
    register(tool)          — add a tool (duplicate name raises ValueError)
    unregister(name)        — remove a tool by name
    get(name)               — retrieve a tool by name (returns None if absent)
    has(name)               — check if a tool is registered
    list()                  — return all registered tool instances
    list_names()            — return all registered tool names
    list_schemas()         — return compact schemas for the Prompt Builder

The registry also provides ``create_default_registry(context)``, a factory
that constructs a registry pre-populated with every built-in tool, all
wired to the existing service layer via the injected ``ToolContext``.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext

logger = logging.getLogger(__name__)


class ToolRegistry:
    """Central registry of all AI-callable tools.

    The registry is the ONLY public access point for future AI. The AI
    Core calls ``registry.get(tool_name)`` and then ``tool.execute()``.
    It never imports service modules directly.

    Thread-safety: the registry is used within a single asyncio event
    loop. No locking is needed.
    """

    __slots__ = ("_tools",)

    def __init__(self) -> None:
        self._tools: dict[str, Tool] = {}

    def register(self, tool: Tool) -> None:
        """Register a tool instance.

        Raises ``ValueError`` if a tool with the same name is already
        registered (duplicate protection).
        """
        name = tool.name
        if name in self._tools:
            raise ValueError(f"ToolRegistry: duplicate tool name '{name}'")
        self._tools[name] = tool
        logger.info("ToolRegistry: registered '%s' (total=%d)", name, len(self._tools))

    def unregister(self, name: str) -> bool:
        """Remove a tool by name. Returns True if it existed."""
        if name in self._tools:
            del self._tools[name]
            logger.info("ToolRegistry: unregistered '%s'", name)
            return True
        return False

    def get(self, name: str) -> Tool | None:
        """Look up a tool by name. Returns None if not found."""
        return self._tools.get(name)

    def has(self, name: str) -> bool:
        """Check whether a tool with the given name is registered."""
        return name in self._tools

    def list(self) -> list[Tool]:
        """Return all registered tool instances."""
        return list(self._tools.values())

    def list_names(self) -> list[str]:
        """Return all registered tool names."""
        return list(self._tools.keys())

    def list_schemas(self) -> list[dict[str, Any]]:
        """Return compact tool schemas for the Prompt Builder.

        Each entry contains: name, description, parameters,
        permission_level, safe, return_type.
        """
        return [
            {
                "name": t.name,
                "description": t.description,
                "parameters": t.parameters,
                "permission_level": t.permission_level.value,
                "safe": t.safe,
                "return_type": t.return_type,
            }
            for t in self._tools.values()
        ]

    def is_empty(self) -> bool:
        """True if no tools are registered."""
        return len(self._tools) == 0


def create_default_registry(context: ToolContext) -> ToolRegistry:
    """Factory: build a registry pre-populated with all built-in tools.

    This is called once at startup by the runtime supervisor. Every
tool receives the same ``ToolContext`` (telegram, owner_id, tz_str).

    If ``context.telegram`` is None but ``context.client`` is set, a
    ``TelegramAPI`` facade is constructed automatically.

    To add a new tool:
        1. Write a tool class in a new file under backend/ai/tools/.
        2. Import it here.
        3. Instantiate and register it in this function.

    No other file needs to change.
    """
    from backend.ai.tools.save import SaveTool, SaveByLinkTool
    from backend.ai.tools.delete import (
        DeleteTool,
        DeleteByIdTool,
        DeleteRepliedTool,
        DeleteMessageByIdTool,
    )
    from backend.ai.tools.semantic import ListRecentMessagesTool, DeleteMessagesByIdsTool
    from backend.ai.tools.bio import (
        BioSetTemplateTool, BioSetTextTool, BioSetMoodTool,
        BioOnTool, BioOffTool, BioShowTool,
    )
    from backend.ai.tools.username import (
        UsernameSetTemplateTool, UsernameSetTextTool, UsernameSetMoodTool,
        UsernameOnTool, UsernameOffTool, UsernameShowTool,
    )
    from backend.ai.tools.account import AccountShowTool
    from backend.ai.tools.retrieve import SearchTool, ListSavesTool
    from backend.ai.tools.database import DatabaseStatsTool
    from backend.ai.tools.settings import SettingsGetTool, SettingsSetTool
    from backend.ai.tools.organize import OrganizeListTool, OrganizeCleanTool

    if context.telegram is None and context.client is not None:
        from backend.telegram_api import TelegramAPI
        context = ToolContext(
            telegram=TelegramAPI(context.client),
            owner_id=context.owner_id,
            tz_str=context.tz_str,
            client=context.client,
            extra=context.extra,
        )

    registry = ToolRegistry()

    registry.register(SaveTool(context))
    registry.register(SaveByLinkTool(context))
    registry.register(DeleteTool(context))
    registry.register(DeleteByIdTool(context))
    registry.register(DeleteRepliedTool(context))
    registry.register(DeleteMessageByIdTool(context))
    registry.register(ListRecentMessagesTool(context))
    registry.register(DeleteMessagesByIdsTool(context))
    registry.register(BioSetTemplateTool(context))
    registry.register(BioSetTextTool(context))
    registry.register(BioSetMoodTool(context))
    registry.register(BioOnTool(context))
    registry.register(BioOffTool(context))
    registry.register(BioShowTool(context))
    registry.register(UsernameSetTemplateTool(context))
    registry.register(UsernameSetTextTool(context))
    registry.register(UsernameSetMoodTool(context))
    registry.register(UsernameOnTool(context))
    registry.register(UsernameOffTool(context))
    registry.register(UsernameShowTool(context))
    registry.register(SearchTool(context))
    registry.register(ListSavesTool(context))
    registry.register(DatabaseStatsTool(context))
    registry.register(AccountShowTool(context))
    registry.register(SettingsGetTool(context))
    registry.register(SettingsSetTool(context))
    registry.register(OrganizeListTool(context))
    registry.register(OrganizeCleanTool(context))

    return registry
