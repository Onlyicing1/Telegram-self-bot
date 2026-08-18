"""
Save Engine — Deep Save only.

Business logic lives in backend.services.save_service.execute_save, the
single authoritative Deep Save pipeline (download → re-upload as a NEW
Saved Messages message). This handler is only the Glass UI wiring:

    .menu → Save → Deep Save → Reply Mode → reply to a message

The reply's ``reply_to_msg_id`` is resolved to the exact target message,
which is passed into the shared Save Engine.
"""
import logging
import os

from backend.services import save_service
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    register_input,
    register_action,
    render,
)
from backend.helper.client import get_client

logger = logging.getLogger(__name__)


async def _save_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    if extra.startswith("type:"):
        builder = InlinePanelBuilder()
        builder.add_row("💬 Reply Mode", "action:save_reply")
        builder.add_row("🔗 Save using a link", "input:save:link")
        return "Deep Save", "Choose a source:", builder.build()

    builder = InlinePanelBuilder()
    builder.add_row("⬇️ Deep Save", "panel:save:type:d")
    builder.add_row("🔍 Retrieve", "panel:retrieve")
    return (
        "Save",
        "Deep Save downloads the message and re-uploads it as a new Saved Messages message.",
        builder.build(),
    )


async def _save_inline_builder(event, extra: str) -> list:
    if extra.startswith("type:"):
        builder = InlinePanelBuilder()
        builder.add_row("💬 Reply Mode", "action:save_reply")
        builder.add_row("🔗 Save using a link", "input:save:link")
        return [render("Deep Save", "Choose a source:", builder.build())]

    builder = InlinePanelBuilder()
    builder.add_row("⬇️ Deep Save", "panel:save:type:d")
    builder.add_row("🔍 Retrieve", "panel:retrieve")
    return [
        render(
            "Save",
            "Deep Save downloads the message and re-uploads it as a new Saved Messages message.",
            builder.build(),
        )
    ]


async def _save_reply_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    from backend.helper.input_state import set_pending

    owner_id = _owner_id

    if not chat_id:
        return "Deep Save", "⚠️ Could not determine the current chat. Please try again.", []

    wait_text = (
        "**Deep Save — Reply Mode**\n\n"
        "Waiting for your reply...\n"
        "Reply to any message to save it (download → re-upload)."
    )

    set_pending(
        owner_id, "save_reply", _save_reply_wait_handler,
        chat_id, wait_text,
        inline_chat_id=chat_id,
        inline_msg_id=getattr(event, "message_id", 0) or 0,
        extra="",
        timeout=None,
    )

    return "Deep Save", wait_text, []


async def _save_reply_wait_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.helper.input_state import clear_pending

    owner_id = _owner_id
    client = _self_client
    clear_pending(owner_id)

    try:
        # Resolve the user's outgoing reply, then the exact message it
        # replied to. That target — never the reply itself — is Deep Saved.
        reply_msg = await client.get_messages(chat_id, ids=msg_id)
        if reply_msg and reply_msg.reply_to_msg_id:
            target_id = reply_msg.reply_to_msg_id
            target_msg = await client.get_messages(chat_id, ids=target_id)
            if target_msg is None:
                result = "⚠️ The replied message no longer exists."
            else:
                result = await save_service.execute_save(
                    client, owner_id, target_msg, os.getenv("TZ", "Asia/Tehran")
                )
        elif reply_msg:
            result = "⚠️ Your message was not a reply. Please reply to a message to select what to save."
        else:
            result = "⚠️ Could not find your reply message. Please try again."
    except Exception as exc:
        result = f"❌ Deep Save failed: {exc}"

    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=[])
        except Exception as exc:
            logger.warning("save reply result edit failed: %s", exc)

    if client:
        try:
            await client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _save_link_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.helper.input_state import clear_pending

    owner_id = _owner_id
    client = _self_client
    clear_pending(owner_id)

    link = text.strip()
    if not link:
        result = "⚠️ Link cannot be empty."
    else:
        result = await save_service.execute_link_save(
            client, owner_id, link, os.getenv("TZ", "Asia/Tehran")
        )

    logger.info("[LINK_SAVE] handler result: %s", result)

    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=[])
        except Exception as exc:
            logger.warning("[LINK_SAVE] result edit failed: %s", exc)

    if client:
        try:
            await client.delete_messages(chat_id, [msg_id])
        except Exception as exc:
            logger.warning("[LINK_SAVE] delete trigger msg failed: %s", exc)


def register(client, owner_id: int, tz_str: str) -> None:
    register_panel("save", _save_panel_handler, parent="menu", title="📥 Save")
    register_inline_builder("save", _save_inline_builder)
    register_action("save_reply", _save_reply_action)
    register_input("save", "link", {
        "handler": _save_link_input_handler,
        "prompt": "**Save by Link**\n\nSend a Telegram message link:\n`https://t.me/channel/123`\n`https://t.me/c/123/456`\n\n_Reply with the link below._",
    })
