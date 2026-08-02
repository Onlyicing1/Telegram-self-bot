"""
Save Engine
  .save f / .s f  — Forward save
  .save d / .s d  — Deep save
  .save            — Inline panel: choose Forward or Deep save.

Business logic lives in backend.services.save_service.
This handler is only the Telethon wiring + panel rendering.
"""
import logging
import os

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.services import save_service
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    register_input,
    register_action,
    send_inline_panel,
    render,
    TargetContext,
    set_target,
    get_target,
)
from backend.helper.client import get_client

logger = logging.getLogger(__name__)


async def _save_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    if extra.startswith("type:"):
        mode = extra[5:]
        builder = InlinePanelBuilder()
        builder.add_row("Reply to a message", f"action:save_reply:{mode}")
        builder.add_row("Save using a link", "input:save:link")
        return "Save", "Choose a source:", builder.build()

    builder = InlinePanelBuilder()
    builder.add_row("📦 Forward Save", "panel:save:type:f")
    builder.add_row("⬇️ Deep Save", "panel:save:type:d")
    builder.add_row("🔍 Retrieve", "panel:retrieve")
    return "Save", "Choose a save type:", builder.build()


async def _save_inline_builder(event, extra: str) -> list:
    if extra.startswith("type:"):
        mode = extra[5:]
        builder = InlinePanelBuilder()
        builder.add_row("Reply to a message", f"action:save_reply:{mode}")
        builder.add_row("Save using a link", "input:save:link")
        return [render("Save", "Choose a source:", builder.build())]

    builder = InlinePanelBuilder()
    builder.add_row("📦 Forward Save", "panel:save:type:f")
    builder.add_row("⬇️ Deep Save", "panel:save:type:d")
    builder.add_row("🔍 Retrieve", "panel:retrieve")
    return [render("Save", "Choose a save type:", builder.build())]


async def _save_reply_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.helper.input_state import set_pending

    owner_id = _owner_id
    mode = extra.strip() if extra else "f"

    if not chat_id:
        return "Save", "⚠️ Could not determine the current chat. Please try again.", []

    mode_label = "Forward" if mode == "f" else "Deep"
    wait_body = (
        f"Waiting for your reply...\n"
        f"Reply to any message to save it ({mode_label} Save)."
    )

    set_pending(
        owner_id, "save_reply", _save_reply_wait_handler,
        chat_id, wait_body,
        inline_chat_id=chat_id,
        inline_msg_id=getattr(event, "message_id", 0) or 0,
        extra=mode,
    )

    return "Save", wait_body, []


async def _save_reply_wait_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.helper.input_state import get_pending, clear_pending

    owner_id = _owner_id
    client = _self_client

    pending = get_pending(owner_id) or {}
    mode = pending.get("extra", "f")
    clear_pending(owner_id)

    try:
        reply_msg = await client.get_messages(chat_id, ids=msg_id)
        if reply_msg and reply_msg.reply_to_msg_id:
            target_id = reply_msg.reply_to_msg_id
            target_msg = await client.get_messages(chat_id, ids=target_id)
            if target_msg is None:
                result = "⚠️ The replied message no longer exists."
            else:
                result = await save_service.execute_save(client, owner_id, target_msg, mode, os.getenv("TZ", "Asia/Tehran"))
        elif reply_msg:
            result = "⚠️ Your message was not a reply. Please reply to a message to select what to save."
        else:
            result = "⚠️ Could not find your reply message. Please try again."
    except Exception as exc:
        result = f"❌ Save failed: {exc}"

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
    from backend.helper.input_state import get_pending, clear_pending

    owner_id = _owner_id
    client = _self_client
    clear_pending(owner_id)

    link = text.strip()
    if not link:
        result = "⚠️ Link cannot be empty."
    else:
        progress_msg = None
        if inline_chat_id and inline_msg_id:
            try:
                progress_msg = await client.get_messages(inline_chat_id, ids=inline_msg_id)
            except Exception:
                pass
        result = await save_service.execute_link_save(
            client, owner_id, link, "UTC", progress_msg=progress_msg,
        )

    logger.info("[LINK_SAVE] handler result: %s", result)

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

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.(?:save|s) (f|d)$"))
    async def save_cmd(event) -> None:
        if not is_owner(event, owner_id):
            return

        mode = event.pattern_match.group(1)
        reply = await event.message.get_reply_message()
        if not reply:
            await event.edit("⚠️ Reply to a message to save it.")
            return

        result = await save_service.execute_save(client, owner_id, reply, mode, tz_str)
        await event.edit(result)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.(?:save|s)$"))
    async def save_panel_cmd(event) -> None:
        if not is_owner(event, owner_id):
            return

        reply = await event.message.get_reply_message()
        if not reply:
            await event.edit("⚠️ Reply to a message to save it.")
            return

        helper = get_client()
        if helper is None:
            await event.edit("⚠️ Inline mode requires the helper bot (BOT_TOKEN).")
            return

        set_target(owner_id, TargetContext(
            owner_id=owner_id,
            kind="reply",
            reply_chat_id=reply.chat_id,
            reply_msg_id=reply.id,
            tz_str=tz_str,
        ))

        try:
            await event.delete()
            await send_inline_panel(client, event.chat_id, "save")
        except Exception as exc:
            logger.warning("save inline send failed: %s", exc)
            try:
                await event.edit(f"⚠️ Inline panel failed: {exc}")
            except Exception:
                pass
