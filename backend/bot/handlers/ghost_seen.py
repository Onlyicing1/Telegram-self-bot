"""
Ghost Seen handler — panels, incoming listener, and manual reply actions.

Ghost Seen provides a private-chat registry, passive message inspection,
selection, pagination, and manual quote/no-quote replies. AI reply generation
is intentionally not part of this surface.
"""
from __future__ import annotations

import logging
from typing import Any

from telethon import events

from backend.helper.panels import InlinePanelBuilder, register_panel, register_action, register_input

logger = logging.getLogger(__name__)

_store_owner_id: int = 0
_self_client: Any = None


def configure(client, owner_id: int, tz_str: str = "UTC") -> None:
    global _store_owner_id, _self_client
    _store_owner_id = owner_id
    _self_client = client


def _nav_buttons(builder: InlinePanelBuilder) -> None:
    builder.add_row("🏠 Home", "panel:_nav:home")
    builder.add_row("⬅ Back", "action:ghost_back")


def _ghost_seen_env_id() -> str:
    import os
    return os.getenv("GHOST_ROOM_ID", "")


def _is_ghost_enabled() -> bool:
    return bool(_ghost_seen_env_id())


def _resolve_ghost_destination() -> int | None:
    raw = _ghost_seen_env_id()
    if not raw:
        return None
    try:
        value = int(raw)
    except ValueError:
        logger.warning("Ghost Seen: GHOST_ROOM_ID is not a valid integer")
        return None
    if value < 0:
        logger.warning("Ghost Seen: GHOST_ROOM_ID must not be negative")
        return None
    return value


async def _ghost_list_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.services import settings_service
    from backend.services.ghost_seen_service import apply_retention, delete_expired_rows, format_chat_list_item, read_registry_rows

    rows = await read_registry_rows()
    _, expired_ids = apply_retention(rows, settings_service.ghost_seen_retention_seconds())
    if expired_ids:
        await delete_expired_rows(expired_ids)
    lines = ["**👻 Ghost Seen**\n"]
    builder = InlinePanelBuilder()
    if not rows:
        lines.extend(["_No private chats yet._", "Incoming messages from private chats appear here automatically."])
    else:
        for row in rows:
            label = format_chat_list_item(row).split("\n")[0][:64]
            builder.add_row(label, f"action:ghost_open:{row.get('chat_id', 0)}")
        lines.append(f"_{len(rows)} chats_")
    _nav_buttons(builder)
    return "👻 Ghost Seen", "\n".join(lines), builder.build()


async def _ghost_chat_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.services.ghost_seen_service import fetch_chunk, get_page, get_selection, format_chat_view_item, count_selected
    try:
        chat_id = int(extra.split(":", 1)[0]) if extra else 0
    except ValueError:
        chat_id = 0
    builder = InlinePanelBuilder()
    if not chat_id:
        _nav_buttons(builder)
        return "👻 Chat", "No chat selected.", builder.build()

    msgs, error = await fetch_chunk(_self_client, chat_id, get_page(chat_id))
    selected = get_selection(chat_id)
    lines = [f"**Chat {chat_id}** — page {get_page(chat_id) + 1}\n"]
    if error == "entity":
        lines.append("_This conversation is temporarily unavailable._")
        _nav_buttons(builder)
        return f"Chat {chat_id}", "\n".join(lines), builder.build()
    if error == "fetch":
        lines.append("_Could not load messages right now. Try again._")
        builder.add_row("🔄 Retry", f"action:ghost_open:{chat_id}")
        _nav_buttons(builder)
        return f"Chat {chat_id}", "\n".join(lines), builder.build()
    if not msgs:
        lines.append("_No messages in this conversation yet._")
        _nav_buttons(builder)
        return f"Chat {chat_id}", "\n".join(lines), builder.build()

    for index, msg in enumerate(msgs):
        lines.append(format_chat_view_item(msg, msg.get("id", 0) in selected, get_page(chat_id) * 5 + index + 1, _store_owner_id))
        mid = msg.get("id", 0)
        if mid:
            mark = "✓" if mid in selected else "○"
            builder.add_row(f"{mark} #{int(mid)}", f"action:ghost_toggle:{mid}")
    builder.add_buttons(("◀ Prev", "action:ghost_page:prev"), ("Next ▶", "action:ghost_page:next"))
    if count_selected(chat_id) == 1:
        builder.add_row("⚡ Reply / Actions", "action:ghost_actions")
    if selected:
        builder.add_row("✕ Clear", "action:ghost_clear")
    builder.add_row("🗑 Remove from list", "action:ghost_remove")
    builder.add_row("⬅ Back", "action:ghost_back")
    return f"Chat {chat_id}", "\n".join(lines), builder.build()


_current_panel_chat = 0


def _set_current_chat(chat_id: int) -> None:
    global _current_panel_chat
    _current_panel_chat = chat_id


def _current_chat() -> int:
    return _current_panel_chat


def current_chat_id() -> int:
    return _current_panel_chat


def _current_chat_str() -> str:
    return str(_current_panel_chat)


async def _ghost_open_action(event, extra: str, chat_id: int):
    from backend.services.ghost_seen_service import clear_selection, set_page
    try:
        target = int(extra)
    except ValueError:
        builder = InlinePanelBuilder(); _nav_buttons(builder)
        return "👻 Chat", "Invalid chat id.", builder.build()
    previous = _current_chat()
    if previous:
        clear_selection(previous)
    clear_selection(target)
    set_page(target, 0)
    _set_current_chat(target)
    from backend.services.ghost_seen_service import clear_unread
    await clear_unread(target)
    return await _ghost_chat_panel_handler(event, str(target))


async def _ghost_toggle_action(event, extra: str, chat_id: int):
    from backend.services.ghost_seen_service import toggle_selection
    try:
        msg_id = int(extra)
    except ValueError:
        return await _ghost_chat_panel_handler(event, _current_chat_str())
    source_chat = _current_chat() or chat_id
    toggle_selection(source_chat, msg_id)
    return await _ghost_chat_panel_handler(event, str(source_chat))


async def _ghost_page_action(event, extra: str, chat_id: int):
    from backend.services.ghost_seen_service import get_page, set_page
    source_chat = _current_chat() or chat_id
    page = get_page(source_chat)
    if extra == "prev":
        set_page(source_chat, max(0, page - 1))
    elif extra == "next":
        set_page(source_chat, page + 1)
    return await _ghost_chat_panel_handler(event, str(source_chat))


async def _ghost_clear_action(event, extra: str, chat_id: int):
    from backend.services.ghost_seen_service import clear_selection
    source_chat = _current_chat() or chat_id
    clear_selection(source_chat)
    return await _ghost_chat_panel_handler(event, str(source_chat))


async def _ghost_back_action(event, extra: str, chat_id: int):
    from backend.services.ghost_seen_service import clear_selection
    source_chat = _current_chat() or chat_id
    clear_selection(source_chat)
    return await _ghost_list_panel_handler(event, extra)


async def _ghost_actions_action(event, extra: str, chat_id: int):
    from backend.services.ghost_seen_service import get_selection, format_reply_target, ensure_entity
    source_chat = _current_chat() or chat_id
    selected = get_selection(source_chat)
    if len(selected) != 1 or not _self_client:
        builder = InlinePanelBuilder(); builder.add_row("⬅ Back", "action:ghost_back")
        return "👻 Reply / Actions", "Select exactly one message first.", builder.build()
    await ensure_entity(_self_client, source_chat)
    anchor_id = sorted(selected)[0]
    anchor = {}
    try:
        from backend.telegram_api.messages import get_messages
        fetched = await get_messages(_self_client, source_chat, [anchor_id])
        if fetched:
            anchor = fetched[0] or {}
    except Exception as exc:
        logger.warning("Ghost Seen: anchor fetch failed: %s", exc)
    builder = InlinePanelBuilder()
    builder.add_row("💬 Reply myself (quote)", "input:ghost_chat:reply")
    builder.add_row("💬 Reply myself (no quote)", "input:ghost_chat:reply_no_quote")
    builder.add_row("⬅ Back", "action:ghost_back")
    banner = format_reply_target(anchor, _store_owner_id) if anchor else f"↩ **Reply target:** #{anchor_id}"
    return "👻 Reply / Actions", banner, builder.build()


async def _ghost_remove_action(event, extra: str, chat_id: int):
    from backend.services.ghost_seen_service import remove_chat
    await remove_chat(_current_chat() or chat_id)
    return await _ghost_list_panel_handler(event, "")


async def _ghost_reply_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.services.ghost_seen_service import get_selection, clear_selection
    destination = _resolve_ghost_destination()
    source_chat = _current_chat() or chat_id
    selected = get_selection(source_chat)
    if destination is None or not selected or not _self_client:
        return
    try:
        await _self_client.send_message(destination, text, reply_to=sorted(selected)[0])
    except Exception as exc:
        logger.warning("Ghost Seen: reply failed: %s", exc)
    clear_selection(source_chat)


async def _ghost_reply_no_quote_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.services.ghost_seen_service import get_selection, clear_selection
    destination = _resolve_ghost_destination()
    source_chat = _current_chat() or chat_id
    if destination is None or not get_selection(source_chat) or not _self_client:
        return
    try:
        await _self_client.send_message(destination, text)
    except Exception as exc:
        logger.warning("Ghost Seen: reply failed: %s", exc)
    clear_selection(source_chat)


def _register_incoming_listener(client, owner_id: int) -> None:
    @client.on(events.NewMessage(incoming=True))
    async def _ghost_incoming_listener(event):
        try:
            if not event.is_private:
                return
            sender = await event.get_sender()
            from backend.services.ghost_seen_service import validate_private_source
            display_name = validate_private_source(event.chat_id, sender, owner_id)
            if display_name is None:
                return
            dt = getattr(event, "date", None)
            timestamp = dt.isoformat() if dt else ""
            from backend.services.ghost_seen_service import upsert_source_chat
            await upsert_source_chat(event.chat_id or 0, display_name, (event.raw_text or "")[:160], timestamp)
        except Exception as exc:
            logger.warning("Ghost Seen: incoming listener error: %s", exc)


def register(client, owner_id: int, tz_str: str = "UTC") -> None:
    configure(client, owner_id, tz_str)
    register_panel("ghost_seen", _ghost_list_panel_handler, parent="menu", title="👻 Ghost Seen")
    register_panel("ghost_chat", _ghost_chat_panel_handler, parent="ghost_seen", title="Chat")
    register_action("ghost_open", _ghost_open_action)
    register_action("ghost_toggle", _ghost_toggle_action)
    register_action("ghost_page", _ghost_page_action)
    register_action("ghost_clear", _ghost_clear_action)
    register_action("ghost_back", _ghost_back_action)
    register_action("ghost_actions", _ghost_actions_action)
    register_action("ghost_remove", _ghost_remove_action)
    register_input("ghost_chat", "reply", {"handler": _ghost_reply_input, "prompt": "Type your reply below. It will be sent as a quote reply."})
    register_input("ghost_chat", "reply_no_quote", {"handler": _ghost_reply_no_quote_input, "prompt": "Type your reply below. It will be sent without quoting."})
    _register_incoming_listener(client, owner_id)
    logger.info("Ghost Seen registered OK")
