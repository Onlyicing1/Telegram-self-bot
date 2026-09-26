"""
Save tool — wraps ``save_service.execute_save``.

The AI calls this tool to Deep-Save a message to Saved Messages. The tool
delegates entirely to the existing save service. No logic is duplicated.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult, result_from_service
from backend.ai.tools.context import ToolContext

logger = logging.getLogger(__name__)


def _request_text(context: ToolContext) -> str:
    """The owner's own request text for this turn (trusted runtime context)."""
    extra = context.extra or {}
    return str(extra.get("request_text") or "")


def _save_metadata(
    context: ToolContext, arguments: dict[str, Any]
) -> tuple["SaveMetadata | None", str]:
    """Build the SHARED ``SaveMetadata`` from the model's optional arguments.

    Returns ``(metadata, "")`` on success, or ``(None, reason)`` when the
    request cannot be honoured — the caller then reports the reason and saves
    nothing (no download, no upload, no row). Bounds, whitespace, dedupe and
    the tag/name limits are the shared ``save_service`` rules; this adapter
    never re-implements them.

    An explicit decline in the OWNER's own words is authoritative: it forces an
    empty tag list, so a model proposal can never re-add tags the owner asked
    not to have.
    """
    from backend.ai.actions import explicit_no_tags_requested
    from backend.services.save_service import SaveMetadata

    try:
        metadata = SaveMetadata.from_raw(
            arguments.get("display_name"), arguments.get("tags")
        )
    except ValueError as exc:
        return None, str(exc)
    if explicit_no_tags_requested(_request_text(context)):
        metadata = SaveMetadata.from_raw(metadata.display_name, ())
    return metadata, ""


def _save_data(result: Any, *, mode: str, **extra: Any) -> dict[str, Any]:
    """The structured half of a Deep Save result.

    ``save_code`` is the item's identity and is exposed whenever the pipeline
    reported one (``save_service.SaveOutcome``); a mocked/legacy string simply
    carries none, and nothing is ever scraped out of the human-readable text.
    ``mode`` stays the two-token contract it always was.
    """
    data: dict[str, Any] = {"mode": mode}
    data.update(extra)
    save_code = getattr(result, "save_code", "")
    if isinstance(save_code, str) and save_code:
        data["save_code"] = save_code
    return data


class SaveTool(Tool):
    """Deep-save a replied message to Saved Messages.

    Downloads the source content and re-uploads it as a NEW message. Deep
    Save is the only save method — there is no Forward Save.
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "save"

    @property
    def requires_reply_context(self) -> bool:
        return True

    @property
    def description(self) -> str:
        return (
            "Deep-save a message to Saved Messages by downloading and "
            "re-uploading it as a new message. Requires a replied message. "
            "Optionally records the owner's own name and tags for the item, "
            "but ONLY when the owner explicitly asked for them."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "display_name": {
                "type": "string",
                "description": (
                    "Optional name for the saved item, taken from the owner's "
                    "own request (e.g. 'save this as University Schedule'). "
                    "Omit it entirely when the owner did not name the item — "
                    "never invent a name."
                ),
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional owner tags, ONLY when the owner explicitly asked "
                    "to tag the item (e.g. 'tag it university semester-2'). "
                    "Pass an empty array when the owner explicitly declined "
                    "('save this without tags'). Omit it when they said nothing "
                    "— never invent a tag."
                ),
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
        return "ToolResult with the new item's save_code in data plus the confirmation message"

    @property
    def consumable_output_fields(self) -> tuple[str, ...]:
        return ("save_code",)

    @property
    def long_running(self) -> bool:
        return True

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import save_service

        reply_meta = context.extra.get("reply_msg") if context.extra else None
        if reply_meta is None:
            return ToolResult(success=False, message="No replied message to save.")

        # Metadata is validated BEFORE the reply is fetched from Telegram: an
        # unhonourable name/tag must refuse with no download, no upload and no
        # row — it must not depend on a network round-trip to be rejected.
        metadata, reason = _save_metadata(context, arguments)
        if metadata is None:
            return ToolResult(success=False, message=f"Nothing was saved: {reason}")

        reply_msg = await self._resolve_reply_message(context, reply_meta)
        if reply_msg is None:
            return ToolResult(
                success=False,
                message="Could not fetch the replied message from Telegram to save it.",
            )

        try:
            result = await save_service.execute_save(
                context.telegram.client, context.owner_id, reply_msg, context.tz_str,
                metadata=metadata,
            )
            # Services report failures as "❌ ..."/"⚠️ ..." strings — only a
            # success string means the save actually happened.
            return result_from_service(result, data=_save_data(result, mode="deep"))
        except Exception as exc:
            return ToolResult(success=False, message=f"Save failed: {exc}")

    async def _resolve_reply_message(self, context: ToolContext, meta: dict[str, Any]):
        """Resolve the real Telethon Message for the reply metadata.

        The dispatcher carries reply metadata (chat_id + message_id) in
        ``context.extra``; the service layer needs the actual Message
        object. We fetch it through the SAME client the runtime already
        injected — never a second client, never fake values.
        """
        client = None
        if context.telegram is not None:
            client = getattr(context.telegram, "client", None)
        if client is None:
            client = context.client
        if client is None:
            return None

        chat_id = meta.get("chat_id")
        message_id = meta.get("message_id")
        if not chat_id or not message_id:
            return None
        try:
            return await client.get_messages(chat_id, ids=message_id)
        except Exception as exc:
            logger.warning("SaveTool: could not fetch reply message %s/%s: %s", chat_id, message_id, exc)
            return None


class SaveByLinkTool(Tool):
    """Deep-save a Telegram message resolved from a t.me / telegram.me link.

    Reuses ``save_service.execute_link_save`` — the SAME Deep Save pipeline
    as ``SaveTool`` (download → re-upload as a NEW Saved Messages message).
    The link is resolved deterministically by the service; the model never
    rewrites the URL.
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "save_by_link"

    @property
    def required_arguments(self) -> tuple[str, ...]:
        return ("link",)

    @property
    def description(self) -> str:
        return (
            "Deep-save a Telegram message given its t.me / telegram.me message "
            "link. Resolves the linked message and runs the existing Deep Save "
            "pipeline (download → re-upload as a NEW Saved Messages message). "
            "Optionally records the owner's own name and tags for the item, but "
            "ONLY when the owner explicitly asked for them."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "link": {
                "type": "string",
                "description": (
                    "Exact Telegram message link, e.g. https://t.me/channel/123 "
                    "or https://t.me/c/123456789/42. Preserve it verbatim."
                ),
            },
            "display_name": {
                "type": "string",
                "description": (
                    "Optional name for the saved item, taken from the owner's "
                    "own request. Omit it entirely when the owner did not name "
                    "the item — never invent a name."
                ),
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "Optional owner tags, ONLY when the owner explicitly asked "
                    "to tag the item. Pass an empty array when they explicitly "
                    "declined; omit it when they said nothing — never invent a tag."
                ),
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
        return "ToolResult with the new item's save_code in data plus the confirmation message"

    @property
    def consumable_output_fields(self) -> tuple[str, ...]:
        return ("save_code",)

    @property
    def long_running(self) -> bool:
        return True

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import save_service

        link = str(arguments.get("link", "") or "").strip()
        if not link:
            return ToolResult(success=False, message="No link provided.")
        if not link.lower().startswith("http"):
            link = "https://" + link

        channel, chat_id, _msg_id = save_service.parse_telegram_link(link)
        if not channel and not chat_id:
            return ToolResult(
                success=False,
                message="That does not look like a valid Telegram message link.",
            )

        client = None
        if context.telegram is not None:
            client = getattr(context.telegram, "client", None)
        if client is None:
            client = context.client
        if client is None:
            return ToolResult(success=False, message="No Telegram client available.")

        metadata, reason = _save_metadata(context, arguments)
        if metadata is None:
            return ToolResult(success=False, message=f"Nothing was saved: {reason}")

        try:
            result = await save_service.execute_link_save(
                client, context.owner_id, link, context.tz_str, metadata=metadata
            )
            return result_from_service(
                result, data=_save_data(result, mode="deep", source="telegram_link")
            )
        except Exception as exc:
            return ToolResult(success=False, message=f"Link save failed: {exc}")
