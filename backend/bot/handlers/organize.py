"""
.organize list   — Structured overview of LifeOS data.
.organize clean  — Purge transient bot_logs older than 7 days.
.organize        — Inline panel: choose list or clean.

Business logic lives in backend.services.organize_service.
"""
import logging
from telethon import events
from backend.bot.handlers.guard import is_owner
from backend.services import organize_service
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    register_action,
    send_inline_panel,
    render,
    to_edit_buttons,
)
from backend.helper.client import get_client

logger = logging.getLogger(__name__)


async def _organize_list_action(event, extra: str) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    result = await organize_service.do_list(_owner_id)
    return "Organizer", result, []


async def _organize_clean_action(event, extra: str) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    result = await organize_service.do_clean(_owner_id)
    return "Organizer", result, []


async def _organize_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    builder = InlinePanelBuilder()
    builder.add_row("📋 Data Overview", "action:organize_list")
    builder.add_row("🧹 Clean Old Logs", "action:organize_clean")
    return "Organizer", "Choose an action:", builder.build()


async def _organize_inline_builder(event, extra: str) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("📋 Data Overview", "action:organize_list")
    builder.add_row("🧹 Clean Old Logs", "action:organize_clean")
    return [render("Organizer", "Choose an action:", builder.build())]


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
            result = await organize_service.do_list(owner_id)
        else:
            result = await organize_service.do_clean(owner_id)
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
