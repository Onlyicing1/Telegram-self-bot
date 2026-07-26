"""
Save Engine
  .save f / .s f  — Forward save
  .save d / .s d  — Deep save
  .save            — Inline panel: choose Forward or Deep save.

Business logic lives in backend.services.save_service.
This handler is only the Telethon wiring + panel rendering.
"""
import logging

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.services import save_service
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    send_inline_panel,
    render,
    render_edit,
    TargetContext,
    set_target,
    get_target,
)
from backend.helper.client import get_client

logger = logging.getLogger(__name__)


async def _save_panel_handler(event, extra: str) -> None:
    from backend.helper.inline_engine import _self_client, _owner_id

    client = _self_client
    owner_id = _owner_id

    if extra.startswith("exec:"):
        mode = extra[5:]
        ctx = get_target(owner_id)
        if not ctx or ctx.kind != "reply":
            text, buttons = render_edit("Save Engine", "Reply context expired. Use `.save` while replying to a message.",
                                        [[("Close", "panel:help:close")]])
            await event.edit(text, buttons=buttons)
            return

        reply_msg = await ctx.resolve(client)
        if reply_msg is None:
            text, buttons = render_edit("Save Engine", "Reply message no longer exists.",
                                        [[("Close", "panel:help:close")]])
            await event.edit(text, buttons=buttons)
            return

        result = await save_service.execute_save(client, owner_id, reply_msg, mode, ctx.tz_str)
        builder = InlinePanelBuilder()
        builder.add_row("Close", "panel:help:close")
        text, buttons = render_edit("Save Engine", result, builder.build())
        await event.edit(text, buttons=buttons)
        return

    await event.edit("⚠️ Unknown save action.")


async def _save_inline_builder(event, extra: str) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("📦 Forward Save", "panel:save:exec:f")
    builder.add_row("⬇️ Deep Save", "panel:save:exec:d")
    builder.add_row("Close", "panel:help:close")
    return [render("Save Engine", "Choose a save mode:", builder.build())]


def register(client, owner_id: int, tz_str: str) -> None:
    register_panel("save", _save_panel_handler)
    register_inline_builder("save", _save_inline_builder)

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
