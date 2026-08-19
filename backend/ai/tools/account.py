"""
Account identity tool — read the authenticated self account's profile.

Answers natural-language requests like "وضعیت اسم اکانتم رو بگو" /
"what is my account name?" using the Telegram account already available
through the authenticated self client (``TelegramAPI.get_me()``). It is
READ_ONLY and performs no mutation. No Telegram internals or secrets are
ever exposed to the AI — only the plain identity fields.
"""
from __future__ import annotations

from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext


class AccountShowTool(Tool):
    """Return the current Telegram account identity."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "account_show"

    @property
    def description(self) -> str:
        return (
            "Show the current Telegram account identity: first name, last "
            "name, full name, @username, phone, and account ID."
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
        return "ToolResult with account identity fields in message and data"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        if context.telegram is None:
            return ToolResult(success=False, message="Telegram is not available.")

        try:
            me = await context.telegram.get_me()
        except Exception as exc:
            return ToolResult(success=False, message=f"Could not read account identity: {exc}")

        if not me:
            return ToolResult(success=False, message="Account identity is unavailable.")

        full_name = me.get("full_name") or ""
        first_name = me.get("first_name") or ""
        last_name = me.get("last_name") or ""
        username = me.get("username") or ""
        phone = me.get("phone") or ""
        account_id = me.get("id")

        lines = [
            f"👤 Name: {full_name or '—'}",
        ]
        if first_name or last_name:
            lines.append(f"   First: {first_name or '—'} · Last: {last_name or '—'}")
        lines.append(f"   Username: @{username}" if username else "   Username: —")
        if phone:
            lines.append(f"   Phone: +{phone}")
        lines.append(f"   ID: {account_id}")

        return ToolResult(
            success=True,
            message="\n".join(lines),
            data={
                "id": account_id,
                "first_name": first_name,
                "last_name": last_name,
                "full_name": full_name,
                "username": username,
                "phone": phone,
            },
        )
