"""
Bio tools — wrap ``bio_service`` functions.

These tools let the AI manage the bio engine: set template, set text,
set mood, turn on/off, and show current state.
"""
from __future__ import annotations

from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult, result_from_service
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
            return result_from_service(result)
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
            return result_from_service(result)
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
            return result_from_service(result)
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
            result = await bio_service.do_on(context.telegram.client, context.owner_id, context.tz_str)
            return result_from_service(result)
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
            return result_from_service(result)
        except Exception as exc:
            return ToolResult(success=False, message=f"Bio off failed: {exc}")


class BioGetTool(Tool):
    """Read the CURRENT Telegram bio (the account's actual 'about' text).

    This is the real bio retrieval operation: it reads the authenticated
    self account through ``TelegramAPI.get_me()`` and returns ONLY the bio
    text — never engine config, phone, account ID, or other metadata.
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "get_bio"

    @property
    def description(self) -> str:
        return (
            "Read the current Telegram account bio (the 'about' text). "
            "Answers requests like 'my bio' / 'بیوم چیه' / 'what is my bio?'. "
            "Returns only the bio text."
        )

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
        return "ToolResult with only the bio text in message and data"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        if context.telegram is None:
            return ToolResult(success=False, message="Telegram is not available.")

        try:
            me = await context.telegram.get_me()
        except Exception as exc:
            return ToolResult(success=False, message=f"Could not read the bio: {exc}")

        if not me:
            return ToolResult(success=False, message="Bio is unavailable.")

        # Data minimization: only the bio itself is returned — never phone,
        # account ID, session, or other account metadata.
        bio = (me.get("about") or "").strip()
        if not bio:
            return ToolResult(success=True, message="📝 Bio: —", data={"bio": ""})
        return ToolResult(success=True, message=f"📝 Bio: {bio}", data={"bio": bio})


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
            return result_from_service(result)
        except Exception as exc:
            return ToolResult(success=False, message=f"Bio show failed: {exc}")
