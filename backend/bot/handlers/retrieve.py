"""
.preview <code> / .retrieve <code> / .r <code> — Show stored metadata.
.send <code>                                 — Forward the saved asset.

Business logic lives in backend.services.retrieve_service.
"""
import logging
from datetime import datetime
from telethon import events
from backend.bot.handlers.guard import is_owner
from backend.services import retrieve_service
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    register_input,
    send_inline_panel,
    render,
    to_edit_buttons,
)
from backend.db import client as db_client
from backend.helper.client import get_client

logger = logging.getLogger(__name__)


async def _preview_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client, _owner_id
    result = await retrieve_service.do_preview(_self_client, _owner_id, text)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("preview inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _send_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client, _owner_id
    result = await retrieve_service.do_send(_self_client, _owner_id, text, chat_id)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("send inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _preview_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    builder = InlinePanelBuilder()
    builder.add_row("Enter Code", "input:preview:code")
    return "Preview", "Enter a save code to preview:", builder.build()


async def _preview_inline_builder(event, extra: str) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("Enter Code", "input:preview:code")
    return [render("Preview", "Enter a save code to preview:", builder.build())]


async def _send_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    builder = InlinePanelBuilder()
    builder.add_row("Enter Code", "input:send:code")
    return "Send", "Enter a save code to forward to this chat:", builder.build()


async def _send_inline_builder(event, extra: str) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("Enter Code", "input:send:code")
    return [render("Send", "Enter a save code to forward to this chat:", builder.build())]


_SAVED_PER_PAGE = 10


def _format_saved_date(created_at: str | None) -> str:
    if not created_at:
        return "—"
    try:
        dt = datetime.fromisoformat(created_at.replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return created_at[:16] if len(created_at) >= 16 else created_at


def _save_mode_label(save_type: str | None) -> str:
    if save_type == "forward":
        return "Fast"
    if save_type == "deep":
        return "Deep"
    return "—"


def _file_type_label(item: dict) -> str:
    media_type = item.get("media_type")
    if media_type:
        return media_type
    mime_type = item.get("mime_type")
    if mime_type:
        return mime_type
    return "—"


def _build_saved_items_body(items: list, page: int, total_pages: int, total: int) -> str:
    if not items:
        return "**Saved Items**\n\n_No saved items found._"
    lines = ["**Saved Items**", f"_{total} items · page {page}/{total_pages}_", ""]
    for item in items:
        code = item.get("short_code") or item.get("save_code") or "—"
        mode = _save_mode_label(item.get("save_type"))
        date = _format_saved_date(item.get("created_at"))
        ftype = _file_type_label(item)
        lines.append(f"`{code}` · {mode} · {date} · {ftype}")
    return "\n".join(lines)


def _build_saved_items_buttons(page: int, total_pages: int, total: int) -> list:
    builder = InlinePanelBuilder()
    if total > 0:
        row = []
        if page > 1:
            row.append(("◀ Previous", f"panel:retrieve_saved:page:{page - 1}"))
        row.append((f"{page}/{total_pages}", f"panel:retrieve_saved:page:{page}"))
        if page < total_pages:
            row.append(("▶ Next", f"panel:retrieve_saved:page:{page + 1}"))
        builder.add_buttons(*row)
    builder.add_buttons(("🏠 Home", "panel:_nav:home"), ("❌ Close", "panel:_nav:close"))
    return builder.build()


async def _retrieve_saved_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    page = 1
    if extra.startswith("page:"):
        page_str = extra[5:]
        if page_str.isdigit():
            page = max(1, int(page_str))
    per_page = _SAVED_PER_PAGE
    offset = (page - 1) * per_page
    items, total = db_client.list_saves(_owner_id, limit=per_page, offset=offset)
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
        offset = (page - 1) * per_page
        items, total = db_client.list_saves(_owner_id, limit=per_page, offset=offset)
    body = _build_saved_items_body(items, page, total_pages, total)
    buttons = _build_saved_items_buttons(page, total_pages, total)
    return "Saved Items", body, buttons


async def _retrieve_saved_inline_builder(event, extra: str) -> list:
    result = await _retrieve_saved_panel_handler(event, extra)
    if result is None:
        return [render("Saved Items", "No saved items.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


def register(client, owner_id: int):
    register_panel("preview", _preview_panel_handler)
    register_panel("send", _send_panel_handler)
    register_inline_builder("preview", _preview_inline_builder)
    register_inline_builder("send", _send_inline_builder)
    register_panel("retrieve_saved", _retrieve_saved_panel_handler)
    register_inline_builder("retrieve_saved", _retrieve_saved_inline_builder)
    register_input("preview", "code", {
        "handler": _preview_input_handler,
        "prompt": "**Preview**\n\nEnter save code (e.g. S0001):\n\n_Reply with the code below._",
    })
    register_input("send", "code", {
        "handler": _send_input_handler,
        "prompt": "**Send**\n\nEnter save code (e.g. S0001):\n\n_Reply with the code below._",
    })

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.(?:preview|retrieve|r)\s+(\S+)$"))
    async def preview(event):
        if not is_owner(event, owner_id):
            return
        save_code = event.pattern_match.group(1).upper()
        result = await retrieve_service.do_preview(client, owner_id, save_code)
        await event.edit(result)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.(?:preview|retrieve|r)$"))
    async def preview_panel(event):
        if not is_owner(event, owner_id):
            return
        helper = get_client()
        if helper is None:
            await event.edit("⚠️ Inline mode requires the helper bot (BOT_TOKEN).")
            return
        try:
            await event.delete()
            await send_inline_panel(client, event.chat_id, "preview")
        except Exception as exc:
            logger.warning("preview inline send failed: %s", exc)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.send\s+(\S+)$"))
    async def send_cmd(event):
        if not is_owner(event, owner_id):
            return
        save_code = event.pattern_match.group(1).upper()
        result = await retrieve_service.do_send(client, owner_id, save_code, event.chat_id)
        if result.startswith("✅"):
            await event.delete()
        else:
            await event.edit(result)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.send$"))
    async def send_panel(event):
        if not is_owner(event, owner_id):
            return
        helper = get_client()
        if helper is None:
            await event.edit("⚠️ Inline mode requires the helper bot (BOT_TOKEN).")
            return
        try:
            await event.delete()
            await send_inline_panel(client, event.chat_id, "send")
        except Exception as exc:
            logger.warning("send inline send failed: %s", exc)
