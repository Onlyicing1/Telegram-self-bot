"""
Retrieve — unified file-browser experience (Glass UI only, no dot commands).

Panel IDs (single deterministic workflow):
  retrieve       — Main menu: Saved Items + Retrieve by Code
  retrieve_saved — Paginated Saved Items browser
  retrieve_item  — Item preview panel (full metadata + action buttons)
  retrieve_code  — Manual code entry (secondary path)

Actions:
  retrieve_item     — Retrieve the file to current chat
  retrieve_rename   — Rename the item (input prompt)
  retrieve_move     — Move to a folder (input prompt)
  retrieve_delete   — Delete the item (with confirmation)

Inputs:
  retrieve:code     — Manual save code entry
  retrieve_item:rename — New filename
  retrieve_item:move   — Folder name
"""
import logging
from datetime import datetime

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.services import retrieve_service
from backend.db import client as db_client
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    register_action,
    register_input,
    send_inline_panel,
    render,
    to_edit_buttons,
)
from backend.helper.client import get_client

logger = logging.getLogger(__name__)

_SAVED_PER_PAGE = 8


# ── Utility ──

def _parse_extra_id(extra: str) -> str | None:
    """Extract item save_code from extra string like 'id:S0042'."""
    if extra.startswith("id:"):
        return extra[3:]
    return None


# ── Panel: retrieve (main menu) ──

async def _retrieve_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    total = await db_client.count_saves(_owner_id)
    builder = InlinePanelBuilder()
    builder.add_row(f"📋 Saved Items ({total})", "panel:retrieve_saved")
    builder.add_row("🔍 Retrieve by Code", "panel:retrieve_code")
    return "Retrieve", "Browse your saved files or retrieve by code.", builder.build()


async def _retrieve_inline_builder(event, extra: str) -> list:
    from backend.helper.inline_engine import _owner_id
    total = await db_client.count_saves(_owner_id)
    builder = InlinePanelBuilder()
    builder.add_row(f"📋 Saved Items ({total})", "panel:retrieve_saved")
    builder.add_row("🔍 Retrieve by Code", "panel:retrieve_code")
    return [render("Retrieve", "Browse your saved files or retrieve by code.", builder.build())]


# ── Panel: retrieve_saved (paginated browser) ──

async def _retrieve_saved_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    page = 1
    if extra.startswith("page:"):
        page_str = extra[5:]
        if page_str.isdigit():
            page = max(1, int(page_str))
    per_page = _SAVED_PER_PAGE
    offset = (page - 1) * per_page
    items, total = await db_client.list_saves(_owner_id, limit=per_page, offset=offset)
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
        offset = (page - 1) * per_page
        items, total = await db_client.list_saves(_owner_id, limit=per_page, offset=offset)

    if not items:
        return "Saved Items", "_No saved items found._\n\nSave something first from the LifeOS menu (📥 Save → Deep Save).", []

    body = f"_{total} items · page {page}/{total_pages}_"

    builder = InlinePanelBuilder()
    for item in items:
        code = item.get("save_code")
        if code:
            icon = retrieve_service._type_icon(item)
            name = retrieve_service._display_name(item)
            if len(name) > 20:
                name = name[:17] + "…"
            builder.add_row(f"{icon} {name} · {code}", f"panel:retrieve_item:id:{code}")

    nav_row = []
    if page > 1:
        nav_row.append(("◀ Prev", f"panel:retrieve_saved:page:{page - 1}"))
    nav_row.append((f"{page}/{total_pages}", "panel:_nav:noop"))
    if page < total_pages:
        nav_row.append(("Next ▶", f"panel:retrieve_saved:page:{page + 1}"))
    builder.add_buttons(*nav_row)

    return "Saved Items", body, builder.build()


async def _retrieve_saved_inline_builder(event, extra: str) -> list:
    result = await _retrieve_saved_panel_handler(event, extra)
    if result is None:
        return [render("Saved Items", "No saved items.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


# ── Panel: retrieve_item (preview + actions) ──

async def _retrieve_item_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    code = _parse_extra_id(extra)
    if not code:
        return "Item", "Item not found.", []
    row = await db_client.query_save(code)
    if not row:
        return "Item", f"❌ No item found for `{code}`", []

    body = retrieve_service.format_preview(row)
    builder = InlinePanelBuilder()
    builder.add_row("⬇ Retrieve", f"action:retrieve_item_exec:{code}")
    builder.add_row("✏ Rename", f"input:retrieve_item:rename:{code}")
    builder.add_row("📂 Move", f"input:retrieve_item:move:{code}")
    builder.add_row("🗑 Delete", f"action:retrieve_item_delete:{code}")
    return "Item Preview", body, builder.build()


async def _retrieve_item_inline_builder(event, extra: str) -> list:
    result = await _retrieve_item_panel_handler(event, extra)
    if result is None:
        return [render("Item", "Not found.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


# ── Panel: retrieve_code (manual entry) ──

async def _retrieve_code_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    builder = InlinePanelBuilder()
    builder.add_row("Enter Code", "input:retrieve:code")
    return "Retrieve by Code", "Enter a save code to preview:", builder.build()


async def _retrieve_code_inline_builder(event, extra: str) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("Enter Code", "input:retrieve:code")
    return [render("Retrieve by Code", "Enter a save code to preview:", builder.build())]


# ── Actions ──

async def _retrieve_item_exec_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _self_client, _owner_id
    code = extra.strip()
    if not code:
        return "Retrieve", "❌ No code specified.", []
    if not chat_id:
        logger.error("[RETRIEVE] invalid target chat from callback: chat_id=%r", chat_id)
        return "Retrieve", "❌ Cannot determine target chat.", []
    result = await retrieve_service.do_retrieve(_self_client, _owner_id, code, chat_id)
    row = await db_client.query_save(code)
    if row:
        return "Retrieve", result, _retrieve_item_buttons(code)
    return "Retrieve", result, []


def _retrieve_item_buttons(code: str) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("⬇ Retrieve Again", f"action:retrieve_item_exec:{code}")
    builder.add_row("‹ Back to Item", f"panel:retrieve_item:id:{code}")
    return builder.build()


async def _retrieve_item_delete_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _self_client, _owner_id
    code = extra.strip()
    if not code:
        return "Delete", "❌ No code specified.", []
    result = await retrieve_service.do_delete(_self_client, _owner_id, code)
    builder = InlinePanelBuilder()
    builder.add_row("‹ Back to Saved Items", "panel:retrieve_saved")
    return "Delete", result, builder.build()


# ── Input handlers ──

async def _retrieve_code_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client, _owner_id
    result = await retrieve_service.do_preview(_self_client, _owner_id, text)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("retrieve code inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _retrieve_rename_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.helper.input_state import get_pending, clear_pending
    pending = get_pending(_owner_id) or {}
    code = pending.get("extra", "")
    clear_pending(_owner_id)
    text_stripped = text.strip()
    if not text_stripped:
        result = "⚠️ Filename cannot be empty."
    elif not code:
        result = "⚠️ No item selected."
    else:
        result = await retrieve_service.do_rename(_owner_id, code, text_stripped)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("rename inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _retrieve_move_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.helper.input_state import get_pending, clear_pending
    pending = get_pending(_owner_id) or {}
    code = pending.get("extra", "")
    clear_pending(_owner_id)
    text_stripped = text.strip()
    if not code:
        result = "⚠️ No item selected."
    elif not text_stripped:
        result = "⚠️ Folder name cannot be empty. Enter 'unfiled' to remove folder."
    else:
        folder = None if text_stripped.lower() == "unfiled" else text_stripped
        result = await retrieve_service.do_move(_owner_id, code, folder)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("move inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


# ── Registration ──

def register(client, owner_id: int):
    register_panel("retrieve", _retrieve_panel_handler, parent="save", title="🔍 Retrieve")
    register_panel("retrieve_saved", _retrieve_saved_panel_handler, parent="retrieve", title="Saved Items")
    register_panel("retrieve_item", _retrieve_item_panel_handler, parent="retrieve_saved", title="Item Detail")
    register_panel("retrieve_code", _retrieve_code_panel_handler, parent="retrieve", title="Retrieve by Code")
    register_inline_builder("retrieve", _retrieve_inline_builder)
    register_inline_builder("retrieve_saved", _retrieve_saved_inline_builder)
    register_inline_builder("retrieve_item", _retrieve_item_inline_builder)
    register_inline_builder("retrieve_code", _retrieve_code_inline_builder)
    register_action("retrieve_item_exec", _retrieve_item_exec_action)
    register_action("retrieve_item_delete", _retrieve_item_delete_action)
    register_input("retrieve", "code", {
        "handler": _retrieve_code_input_handler,
        "prompt": "**Retrieve by Code**\n\nEnter save code (e.g. S0042):\n\n_Reply with the code below._",
    })
    register_input("retrieve_item", "rename", {
        "handler": _retrieve_rename_input_handler,
        "prompt": "**Rename Item**\n\nEnter the new filename:\n\n_Reply below._",
    })
    register_input("retrieve_item", "move", {
        "handler": _retrieve_move_input_handler,
        "prompt": "**Move Item**\n\nEnter the folder name (or 'unfiled' to remove):\n\n_Reply below._",
    })
