"""
Settings tools — wrap ``settings_service`` get/set functions.

``SettingsGetTool`` is READ_ONLY. ``SettingsSetTool`` is ADMIN_ONLY —
the AI must ask the owner for confirmation before changing any setting.
"""
from __future__ import annotations

from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext


class SettingsGetTool(Tool):
    """Read a bot setting value by key."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "settings_get"

    @property
    def description(self) -> str:
        return "Read a bot setting value by key."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "key": {
                "type": "string",
                "description": "The setting key to read.",
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
        return "ToolResult with setting value in message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import settings_service

        key = arguments.get("key")
        if not key:
            return ToolResult(success=False, message="Missing key argument.")
        try:
            value = settings_service.get_setting(key)
            return ToolResult(success=True, message=f"{key} = {value}", data={"key": key, "value": str(value)})
        except Exception as exc:
            return ToolResult(success=False, message=f"Settings get failed: {exc}")


class SettingsSetTool(Tool):
    """Write a bot setting value by key. Admin Only — requires confirmation."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "settings_set"

    @property
    def description(self) -> str:
        return "Set a bot setting value by key. Requires owner confirmation."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "key": {
                "type": "string",
                "description": "The setting key to write.",
            },
            "value": {
                "type": "string",
                "description": "The value to set.",
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.ADMIN_ONLY

    @property
    def safe(self) -> bool:
        return False

    @property
    def return_type(self) -> str:
        return "ToolResult with confirmation message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import settings_service

        key = arguments.get("key")
        value = arguments.get("value")
        if not key or value is None:
            return ToolResult(success=False, message="Missing key or value argument.")
        try:
            success = settings_service.set_setting(key, value)
            if success:
                return ToolResult(success=True, message=f"Setting '{key}' updated to '{value}'.", data={"key": key, "value": str(value)})
            return ToolResult(success=False, message=f"Failed to set '{key}'. Value may be invalid.")
        except Exception as exc:
            return ToolResult(success=False, message=f"Settings set failed: {exc}")
