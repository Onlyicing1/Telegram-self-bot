"""
Bio tools — wrap ``bio_service`` functions.

These tools let the AI manage the bio engine: set template, set text,
set mood, turn on/off, and show current state.
"""
from __future__ import annotations

from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext


class BioSetTemplateTool(Tool):
    """Set the bio template."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "bio_set_template"

    @property
    def description(self) -> str:
        return "Set the bio template. Supports {time}, {mood}, {text} tokens."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "template": {
                "type": "string",
                "description": "Bio template with {time}, {mood}, {text} tokens.",
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
        from backend.services import bio_service

        template = arguments.get("template")
        if not template:
            return ToolResult(success=False, message="Missing template argument.")
        try:
            result = await bio_service.do_template(context.owner_id, template)
            return ToolResult(success=True, message=result)
        except Exception as exc:
            return ToolResult(success=False, message=f"Bio template set failed: {exc}")


class BioSetTextTool(Tool):
    """Set the bio {text} token value."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "bio_set_text"

    @property
    def description(self) -> str:
        return "Set the bio {text} token value."

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
        from backend.services import bio_service

        text = arguments.get("text", "")
        try:
            result = await bio_service.do_text(context.owner_id, text)
            return ToolResult(success=True, message=result)
        except Exception as exc:
            return ToolResult(success=False, message=f"Bio text set failed: {exc}")


class BioSetMoodTool(Tool):
    """Set the bio {mood} token value."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "bio_set_mood"

    @property
    def description(self) -> str:
        return "Set the bio {mood} token value."

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
        from backend.services import bio_service

        mood = arguments.get("mood", "")
        try:
            result = await bio_service.do_mood(context.owner_id, mood)
            return ToolResult(success=True, message=result)
        except Exception as exc:
            return ToolResult(success=False, message=f"Bio mood set failed: {exc}")


class BioOnTool(Tool):
    """Turn on the bio cron engine."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "bio_on"

    @property
    def description(self) -> str:
        return "Turn on the bio sync engine."

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
        from backend.services import bio_service

        try:
            result = await bio_service.do_on(context.client, context.owner_id, context.tz_str)
            return ToolResult(success=True, message=result)
        except Exception as exc:
            return ToolResult(success=False, message=f"Bio on failed: {exc}")


class BioOffTool(Tool):
    """Turn off the bio cron engine."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "bio_off"

    @property
    def description(self) -> str:
        return "Turn off the bio sync engine."

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
        from backend.services import bio_service

        try:
            result = await bio_service.do_off(context.owner_id)
            return ToolResult(success=True, message=result)
        except Exception as exc:
            return ToolResult(success=False, message=f"Bio off failed: {exc}")


class BioShowTool(Tool):
    """Show the current bio engine state."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "bio_show"

    @property
    def description(self) -> str:
        return "Show the current bio engine state: status, template, mood, text, last bio."

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
        return "ToolResult with bio state text in message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import bio_service

        try:
            result = await bio_service.do_show(context.owner_id, context.tz_str)
            return ToolResult(success=True, message=result)
        except Exception as exc:
            return ToolResult(success=False, message=f"Bio show failed: {exc}")
