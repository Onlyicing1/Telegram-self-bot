"""
.db clean   — Remove orphan DB rows.
.db stats   — Display database statistics.
.db vacuum  — Run orphan cleanup + index optimization.
.db        — Inline panel: choose database action.

Business logic lives in backend.services.database_service.
"""
import logging
from telethon import events
from backend.bot.handlers.guard import is_owner
from backend.services import database_service
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


async def _db_clean_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _self_client, _owner_id
    result = await database_service.do_clean(_self_client, _owner_id)
    return "Database", result, []


async def _db_stats_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    from backend.bot.handlers.misc import _resolve_tz
    result = await database_service.do_stats(_owner_id, _resolve_tz())
    return "Database", result, []


async def _db_vacuum_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _self_client, _owner_id
    result = await database_service.do_vacuum(_self_client, _owner_id)
    return "Database", result, []


async def _db_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    builder = InlinePanelBuilder()
    builder.add_row("📊 Statistics", "action:db_stats")
    builder.add_row("🧹 Clean Orphans", "action:db_clean")
    builder.add_row("⚙️ Vacuum", "action:db_vacuum")
    return "Database", "Choose an action:", builder.build()


async def _db_inline_builder(event, extra: str) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("📊 Statistics", "action:db_stats")
    builder.add_row("🧹 Clean Orphans", "action:db_clean")
    builder.add_row("⚙️ Vacuum", "action:db_vacuum")
    return [render("Database", "Choose an action:", builder.build())]


def register(client, owner_id: int, tz_str: str):

    register_panel("db", _db_panel_handler)
    register_inline_builder("db", _db_inline_builder)
    register_action("db_clean", _db_clean_action)
    register_action("db_stats", _db_stats_action)
    register_action("db_vacuum", _db_vacuum_action)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.db\s+(clean|stats|vacuum)$"))
    async def db_cmd(event):
        if not is_owner(event, owner_id):
            return
        action = event.pattern_match.group(1)
        if action == "clean":
            result = await database_service.do_clean(client, owner_id)
        elif action == "stats":
            result = await database_service.do_stats(owner_id, tz_str)
        elif action == "vacuum":
            result = await database_service.do_vacuum(client, owner_id)
        await event.edit(result)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.db$"))
    async def db_panel(event):
        if not is_owner(event, owner_id):
            return
        helper = get_client()
        if helper is None:
            await event.edit("⚠️ Inline mode requires the helper bot (BOT_TOKEN).")
            return
        try:
            await event.delete()
            await send_inline_panel(client, event.chat_id, "db")
        except Exception as exc:
            logger.warning("db inline send failed: %s", exc)
