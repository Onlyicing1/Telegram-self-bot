"""
Username tools — wrap ``username_service`` functions.

These tools let the AI manage the username engine: set template, set
text, set mood, turn on/off, and show current state.
"""
from __future__ import annotations

from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult, result_from_service
from backend.ai.tools.context import ToolContext


class UsernameSetTemplateTool(Tool):
    """Set the username template."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "username_set_template"

    @property
    def description(self) -> str:
        return "Set the username template. Supports {time}, {mood}, {text} tokens."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "template": {
                "type": "string",
                "description": "Username template with {time}, {mood}, {text} tokens.",
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
        return "ToolResult with confirmation message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import username_service

        template = arguments.get("template")
        if not template:
            return ToolResult(success=False, message="Missing template argument.")
        try:
            result = await username_service.do_template(context.owner_id, template)
            return result_from_service(result)
        except Exception as exc:
            return ToolResult(success=False, message=f"Username template set failed: {exc}")


class UsernameSetTextTool(Tool):
    """Set the username {text} token value."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "username_set_text"

    @property
    def description(self) -> str:
        return "Set the username {text} token value."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "text": {
                "type": "string",
                "description": "The text value for the {text} token.",
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
        return "ToolResult with confirmation message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import username_service

        text = arguments.get("text", "")
        try:
            result = await username_service.do_text(context.owner_id, text)
            return result_from_service(result)
        except Exception as exc:
            return ToolResult(success=False, message=f"Username text set failed: {exc}")


class UsernameSetMoodTool(Tool):
    """Set the username {mood} token value."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "username_set_mood"

    @property
    def description(self) -> str:
        return "Set the username {mood} token value."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "mood": {
                "type": "string",
                "description": "The mood value for the {mood} token.",
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
        return "ToolResult with confirmation message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import username_service

        mood = arguments.get("mood", "")
        try:
            result = await username_service.do_mood(context.owner_id, mood)
            return result_from_service(result)
        except Exception as exc:
            return ToolResult(success=False, message=f"Username mood set failed: {exc}")


class UsernameOnTool(Tool):
    """Turn on the username cron engine."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "username_on"

    @property
    def description(self) -> str:
        return "Turn on the username sync engine."

    @property
    def parameters(self) -> dict[str, Any]:
        return {}

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_WRITE

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with confirmation message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import username_service

        try:
            result = await username_service.do_on(context.telegram.client, context.owner_id, context.tz_str)
            return result_from_service(result)
        except Exception as exc:
            return ToolResult(success=False, message=f"Username on failed: {exc}")


class UsernameOffTool(Tool):
    """Turn off the username cron engine."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "username_off"

    @property
    def description(self) -> str:
        return "Turn off the username sync engine."

    @property
    def parameters(self) -> dict[str, Any]:
        return {}

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_WRITE

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with confirmation message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import username_service

        try:
            result = await username_service.do_off(context.owner_id)
            return result_from_service(result)
        except Exception as exc:
            return ToolResult(success=False, message=f"Username off failed: {exc}")


class UsernameShowTool(Tool):
    """Show the current username engine state."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "username_show"

    @property
    def description(self) -> str:
        return "Show the current username engine state: status, template, mood, text."

    @property
    def parameters(self) -> dict[str, Any]:
        return {}

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_ONLY

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with username state text in message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import username_service

        try:
            result = await username_service.do_show(context.owner_id, context.tz_str)
            return result_from_service(result)
        except Exception as exc:
            return ToolResult(success=False, message=f"Username show failed: {exc}")
