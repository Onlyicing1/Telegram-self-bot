"""
.list [n]       — Show recent saved items (newest first, default 10).
.find <text>    — Search saved items by short code, filename, caption, or mime type.
.list           — Inline panel: show recent saves.
.find           — Inline panel: input prompt for search text.

Business logic lives in backend.services.discover_service.
"""
import logging
from telethon import events
from backend.bot.handlers.guard import is_owner
from backend.services import discover_service
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    register_input,
    send_inline_panel,
    render,
    to_edit_buttons,
)
from backend.helper.client import get_client

logger = logging.getLogger(__name__)


async def _list_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    from backend.bot.handlers.misc import _resolve_tz
    tz_str = _resolve_tz()
    limit = 10
    if extra and extra.isdigit():
        limit = min(int(extra), 50)
    text = await discover_service.do_list(_owner_id, limit, tz_str)
    return "Recent Saves", text, []


async def _list_inline_builder(event, extra: str) -> list:
    from backend.helper.inline_engine import _owner_id
    from backend.bot.handlers.misc import _resolve_tz
    tz_str = _resolve_tz()
    limit = 10
    if extra and extra.isdigit():
        limit = min(int(extra), 50)
    text = await discover_service.do_list(_owner_id, limit, tz_str)
    return [render("Recent Saves", text, [])]


async def _find_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _owner_id, _self_client
    from backend.bot.handlers.misc import _resolve_tz
    result = await discover_service.do_find(_owner_id, text, _resolve_tz())
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("find inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _find_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    builder = InlinePanelBuilder()
    builder.add_row("Enter Search Text", "input:find:query")
    return "Search", "Enter search text:", builder.build()


async def _find_inline_builder(event, extra: str) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("Enter Search Text", "input:find:query")
    return [render("Search", "Enter search text:", builder.build())]


def register(client, owner_id: int, tz_str: str):

    register_panel("list", _list_panel_handler, parent="menu", title="📋 List")
    register_panel("find", _find_panel_handler, parent="menu", title="🔍 Find")
    register_inline_builder("list", _list_inline_builder)
    register_inline_builder("find", _find_inline_builder)
    register_input("find", "query", {
        "handler": _find_input_handler,
        "prompt": "**Search**\n\nEnter search text (filename, caption, code, or MIME):\n\n_Reply below._",
    })

