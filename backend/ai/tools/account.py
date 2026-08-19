"""
Account identity tool — read the authenticated self account's profile.

Answers natural-language requests like "وضعیت اسم اکانتم رو بگو" /
"what is my account name?" using the Telegram account already available
through the authenticated self client (``TelegramAPI.get_me()``). It is
READ_ONLY and performs no mutation.

DATA MINIMIZATION: the tool returns ONLY the identity fields the request
needs. The caller passes ``fields`` (an allowlist of ``first_name``,
``last_name``, ``full_name``, ``username``); the default is the minimal
safe pair (first name + username). Phone number and account ID are NEVER
returned — they are private/internal identifiers no LifeOS intent needs.
No Telegram internals or secrets are ever exposed to the AI.
"""
from __future__ import annotations

from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext

# The only identity fields that may ever be returned. Anything else
# (phone, account id, session data, credentials) is rejected or dropped.
_ACCOUNT_FIELDS: tuple[str, ...] = ("first_name", "last_name", "full_name", "username")
_DEFAULT_FIELDS: tuple[str, ...] = ("first_name", "username")

_FIELD_LABELS: dict[str, str] = {
    "first_name": "First Name",
    "last_name": "Last Name",
    "full_name": "Full Name",
    "username": "Username",
}


def _normalize_fields(raw: Any) -> list[str] | None:
    """Validate/normalize the ``fields`` argument against the allowlist.

    Returns ``None`` for an invalid value (wrong type, empty, or an
    unknown field name) so the caller can reject it deterministically.
    """
    if raw is None:
        return list(_DEFAULT_FIELDS)
    if isinstance(raw, str):
        raw = [raw]
    if not isinstance(raw, list) or not raw:
        return None
    out: list[str] = []
    for f in raw:
        if not isinstance(f, str) or f not in _ACCOUNT_FIELDS:
            return None
        if f not in out:
            out.append(f)
    return out


class AccountShowTool(Tool):
    """Return the requested Telegram account identity fields (never phone/ID)."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "account_show"

    @property
    def description(self) -> str:
        return (
            "Show the current Telegram account identity. Pass 'fields' to "
            "request only what the owner asked for: first_name (the account "
            "display/first name — what casual Persian 'یوزرنیم' means in this "
            "project), last_name, full_name, or username (the real Telegram "
            "@username). Defaults to first_name + username. Phone and account "
            "ID are never returned."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "fields": {
                "type": "array",
                "items": {
                    "type": "string",
                    "enum": list(_ACCOUNT_FIELDS),
                },
                "description": (
                    "Optional identity fields to return: first_name, "
                    "last_name, full_name, username. Defaults to "
                    "first_name + username."
                ),
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
        return "ToolResult with only the requested identity fields in message and data"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        if context.telegram is None:
            return ToolResult(success=False, message="Telegram is not available.")

        fields = _normalize_fields(arguments.get("fields"))
        if fields is None:
            return ToolResult(
                success=False,
                message="Invalid 'fields' argument. Allowed values: first_name, last_name, full_name, username.",
            )

        try:
            me = await context.telegram.get_me()
        except Exception as exc:
            return ToolResult(success=False, message=f"Could not read account identity: {exc}")

        if not me:
            return ToolResult(success=False, message="Account identity is unavailable.")

        values: dict[str, str] = {
            "first_name": me.get("first_name") or "",
            "last_name": me.get("last_name") or "",
            "full_name": me.get("full_name") or "",
            "username": me.get("username") or "",
        }

        # Build ONLY the requested fields — phone, id, and every other
        # internal identifier are never serialized into message or data.
        lines: list[str] = []
        data: dict[str, Any] = {}
        for f in fields:
            value = values[f]
            data[f] = value
            if f == "username":
                lines.append(f"   Username: @{value}" if value else "   Username: —")
            else:
                lines.append(f"   {_FIELD_LABELS[f]}: {value or '—'}")
        message = "👤 " + "\n".join(lines) if lines else "👤 Account identity"

        return ToolResult(success=True, message=message, data=data)
