"""
.organize list   — Structured overview of LifeOS data (saves, logs, bio).
.organize clean  — Purge transient bot_logs older than 7 days.
.organize        — Inline panel: choose list or clean.

Inline Mode:
  - .organize (no args) → inline panel with List/Clean buttons.
  - .organize list / .organize clean still work as edit-in-place (backward compat).
"""
import asyncio
import logging
from telethon import events
from backend.bot.handlers.guard import is_owner
from backend.db import client as db_client
from backend.diagnostics import record_event
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    register_action,
    send_inline_panel,
)
from backend.helper.client import get_client

logger = logging.getLogger(__name__)


async def _do_list(owner_id: int) -> str:
    t0 = asyncio.get_event_loop().time()
    try:
        total = db_client.count_saves(owner_id)
        fwd = db_client.count_saves(owner_id, "forward")
        deep = db_client.count_saves(owner_id, "deep")
        logs = db_client.count_logs(owner_id)
        bio = db_client.get_bio_state(owner_id)
        record_event("organize", "list", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")

        bio_status = "OFF"
        bio_template = "—"
        if bio:
            bio_status = "ON" if bio.get("is_active") else "OFF"
            bio_template = bio.get("template", "—")

        lines = [
            "**LifeOS Status**\n",
            f"📦 **Saves**",
            f"  Total: `{total}`",
            f"  Forward: `{fwd}`",
            f"  Deep: `{deep}`\n",
            f"📋 **Logs**",
            f"  Entries: `{logs}`\n",
            f"🧬 **Bio Engine**",
            f"  Status: `{bio_status}`",
            f"  Template: `{bio_template}`",
        ]
        return "\n".join(lines)
    except Exception as exc:
        logger.error("organize list failed: %s", exc)
        record_event("organize", "list", 0, "ERROR", str(exc))
        return f"❌ Error: {exc}"


async def _do_clean(owner_id: int) -> str:
    t0 = asyncio.get_event_loop().time()
    try:
        deleted = db_client.clean_logs(owner_id, days=7)
        record_event("organize", "clean", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
        return f"🧹 Cleaned `{deleted}` log entries older than 7 days."
    except Exception as exc:
        logger.error("organize clean failed: %s", exc)
        record_event("organize", "clean", 0, "ERROR", str(exc))
        return f"❌ Error: {exc}"


async def _organize_list_action(event, extra: str) -> tuple:
    from backend.helper.inline_engine import _owner_id
    result = await _do_list(_owner_id)
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:organize")
    builder.add_row("Close", "panel:help:close")
    return result, builder.build()


async def _organize_clean_action(event, extra: str) -> tuple:
    from backend.helper.inline_engine import _owner_id
    result = await _do_clean(_owner_id)
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:organize")
    builder.add_row("Close", "panel:help:close")
    return result, builder.build()


async def _organize_panel_handler(event, extra: str) -> None:
    text = "**Organizer**\n\nChoose an action:"
    builder = InlinePanelBuilder()
    builder.add_row("📋 Data Overview", "action:organize_list")
    builder.add_row("🧹 Clean Old Logs", "action:organize_clean")
    builder.add_row("Close", "panel:help:close")
    await event.edit(text, buttons=builder.build())


async def _organize_inline_builder(event, extra: str) -> list:
    from telethon.tl import types
    text = "**Organizer**\n\nChoose an action:"
    builder = InlinePanelBuilder()
    builder.add_row("📋 Data Overview", "action:organize_list")
    builder.add_row("🧹 Clean Old Logs", "action:organize_clean")
    builder.add_row("Close", "panel:help:close")
    buttons = builder.build()
    msg = types.InputBotInlineMessageText(
        message=text,
        reply_markup=types.ReplyInlineMarkup(rows=buttons) if buttons else None,
    )
    result = types.InputBotInlineResult(
        id="0",
        type="article",
        title="Organizer",
        send_message=msg,
    )
    return [result]


def register(client, owner_id: int):

    register_panel("organize", _organize_panel_handler)
    register_inline_builder("organize", _organize_inline_builder)
    register_action("organize_list", _organize_list_action)
    register_action("organize_clean", _organize_clean_action)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.organize\s+(list|clean)$"))
    async def organize(event):
        if not is_owner(event, owner_id):
            return
        action = event.pattern_match.group(1)
        if action == "list":
            result = await _do_list(owner_id)
        else:
            result = await _do_clean(owner_id)
        await event.edit(result)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.organize$"))
    async def organize_panel(event):
        if not is_owner(event, owner_id):
            return
        helper = get_client()
        if helper is None:
            await event.edit("⚠️ Inline mode requires the helper bot (BOT_TOKEN).")
            return
        try:
            await event.delete()
            await send_inline_panel(client, event.chat_id, "organize")
        except Exception as exc:
            logger.warning("organize inline send failed: %s", exc)
