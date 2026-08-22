"""
Ghost Room handler — panels, incoming listener, and actions.

Panels:
  ghost         — list of private chats (from ghost_chats table)
  ghost_chat    — five-message page for a specific chat

Actions:
  ghost_open:<chat_id>    — open a chat (resets unread, loads page 0)
  ghost_toggle:<msg_id>   — toggle selection of a message
  ghost_page:<dir>        — prev / next page (dir = prev | next)
  ghost_clear             — clear selection
  ghost_ai_single         — AI with exactly one selected message
  ghost_ai_multi          — AI with one or more selected messages
  ghost_back              — back to chat list

Inputs:
  ghost_reply             — quote-reply with typed text
  ghost_reply_no_quote    — reply without quote
  ghost_ai_input          — AI prompt text input

Incoming listener:
  One events.NewMessage(incoming=True) listener that upserts ghost_chats rows
  for private incoming messages. Lightweight, guarded, never raises into the
  dispatch loop.
"""
from __future__ import annotations

import logging
from typing import Any

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.helper.panels import (
    InlinePanelBuilder,
    register_panel,
    register_action,
    register_input,
)

logger = logging.getLogger(__name__)

_store_owner_id: int = 0
_store_tz_str: str = "UTC"
_self_client: Any = None


def configure(client, owner_id: int, tz_str: str) -> None:
    global _store_owner_id, _store_tz_str, _self_client
    _store_owner_id = owner_id
    _store_tz_str = tz_str
    _self_client = client


# ── helpers ──

def _nav_buttons(builder: InlinePanelBuilder) -> None:
    builder.add_row("🏠 Home", "panel:_nav:home")
    builder.add_row("⬅ Back", "action:ghost_back")


def _ghost_room_id() -> str:
    import os
    return os.getenv("GHOST_ROOM_ID", "")


def _resolve_ghost_destination() -> int | None:
    """Resolve the GHOST_ROOM_ID env var to a valid Telegram chat ID.

    This is the SINGLE authoritative destination for all Ghost Room output.
    Returns None when GHOST_ROOM_ID is missing, empty, or non-numeric —
    callers must fail closed and never fall back to another chat.
    """
    raw = _ghost_room_id()
    if not raw:
        return None
    try:
        val = int(raw)
    except ValueError:
        logger.warning("Ghost Room: GHOST_ROOM_ID=%r is not a valid integer", raw)
        return None
    if val < 0:
        logger.warning("Ghost Room: GHOST_ROOM_ID=%d is negative", val)
        return None
    return val


def _is_ghost_enabled() -> bool:
    return bool(_ghost_room_id())


# ── panel: ghost (chat list) ──


async def _ghost_list_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.services import settings_service
    from backend.services.ghost_seen_service import (
        apply_retention,
        delete_expired_rows,
        format_chat_list_item,
        read_registry_rows,
    )

    rows = await read_registry_rows()
    _, expired_ids = apply_retention(rows, settings_service.ghost_seen_retention_days())
    if expired_ids:
        await delete_expired_rows(expired_ids)

    lines = ["**👻 Ghost Seen**\n"]
    if not rows:
        lines.append("_No private chats yet._")
        lines.append("Incoming messages from private chats appear here automatically.")
        builder = InlinePanelBuilder()
        _nav_buttons(builder)
        return "👻 Ghost Seen", "\n".join(lines), builder.build()

    builder = InlinePanelBuilder()
    for row in rows:
        label = format_chat_list_item(row).split("\n")[0][:64]
        chat_id = row.get("chat_id", 0)
        builder.add_row(label, f"action:ghost_open:{chat_id}")

    lines.append(f"_{len(rows)} chats_")
    _nav_buttons(builder)
    return "👻 Ghost Seen", "\n".join(lines), builder.build()


# ── panel: ghost_chat (five-message page) ──


async def _ghost_chat_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.services.ghost_seen_service import (
        fetch_chunk,
        get_page,
        get_selection,
        format_chat_view_item,
        count_selected,
    )

    parts = extra.split(":", 1) if extra else []
    chat_id = int(parts[0]) if parts else 0
    if not chat_id:
        builder = InlinePanelBuilder()
        _nav_buttons(builder)
        return "👻 Chat", "No chat selected.", builder.build()

    page = get_page(chat_id)
    msgs = await fetch_chunk(_self_client, chat_id, page)
    selected = get_selection(chat_id)

    lines = [f"**Chat {chat_id}** — page {page + 1}\n"]
    for i, msg in enumerate(msgs):
        seq = page * 5 + i + 1
        is_sel = msg.get("id", 0) in selected
        lines.append(format_chat_view_item(msg, is_sel, seq, _store_owner_id))

    builder = InlinePanelBuilder()
    for msg in msgs:
        mid = msg.get("id", 0)
        if not mid:
            continue
        is_sel = mid in selected
        mark = "✓" if is_sel else "○"
        builder.add_row(f"{mark} #{int(mid)}", f"action:ghost_toggle:{mid}")

    builder.add_buttons(
        ("◀ Prev", "action:ghost_page:prev"),
        ("Next ▶", "action:ghost_page:next"),
    )
    n_sel = count_selected(chat_id)
    if n_sel > 0:
        builder.add_row(f"🔍 AI ({n_sel})", "action:ghost_ai_multi")
        if n_sel == 1:
            builder.add_row("🤖 AI Single", "action:ghost_ai_single")
        builder.add_row("✕ Clear", "action:ghost_clear")
        builder.add_row("💬 Reply", "input:ghost_reply")
        builder.add_row("💬 No Quote", "input:ghost_reply_no_quote")
    builder.add_row("⬅ Back", "action:ghost_back")
    return f"Chat {chat_id}", "\n".join(lines), builder.build()


# ── actions ──


async def _ghost_open_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services.ghost_seen_service import set_page
    try:
        target = int(extra)
    except ValueError:
        builder = InlinePanelBuilder()
        _nav_buttons(builder)
        return "👻 Chat", "Invalid chat id.", builder.build()

    set_page(target, 0)
    from backend.services.ghost_seen_service import clear_unread as clear_registry_unread
    await clear_registry_unread(target)
    return await _ghost_chat_panel_handler(event, str(target))


async def _ghost_toggle_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services.ghost_seen_service import toggle_selection
    try:
        msg_id = int(extra)
    except ValueError:
        return await _ghost_chat_panel_handler(event, _current_chat_str())

    panel_chat = _current_chat()
    toggle_selection(panel_chat, msg_id)
    return await _ghost_chat_panel_handler(event, str(panel_chat))


async def _ghost_page_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services.ghost_seen_service import get_page, set_page
    panel_chat = _current_chat()
    page = get_page(panel_chat)
    if extra == "prev":
        set_page(panel_chat, max(0, page - 1))
    elif extra == "next":
        set_page(panel_chat, page + 1)
    return await _ghost_chat_panel_handler(event, str(panel_chat))


async def _ghost_clear_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services.ghost_seen_service import clear_selection
    panel_chat = _current_chat()
    clear_selection(panel_chat)
    return await _ghost_chat_panel_handler(event, str(panel_chat))


async def _ghost_back_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    return await _ghost_list_panel_handler(event, extra)


async def _ghost_ai_single_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services.ghost_seen_service import get_selection, execute_ghost_seen_ai
    panel_chat = _current_chat()
    sel = get_selection(panel_chat)
    if len(sel) != 1:
        builder = InlinePanelBuilder()
        builder.add_row("⬅ Back", "action:ghost_back")
        return "👻 AI", "Select exactly one message for single-message AI.", builder.build()

    return await _return_ghost_list_after_action(event)


async def _ghost_ai_multi_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services.ghost_seen_service import get_selection
    panel_chat = _current_chat()
    sel = get_selection(panel_chat)
    if not sel:
        builder = InlinePanelBuilder()
        builder.add_row("⬅ Back", "action:ghost_back")
        return "👻 AI", "Select at least one message.", builder.build()

    return await _return_ghost_list_after_action(event)


# ── reply inputs ──


async def _ghost_reply_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.services.ghost_seen_service import get_selection, clear_selection
    dst = _resolve_ghost_destination()
    if dst is None:
        logger.warning("Ghost Room: reply blocked — GHOST_ROOM_ID missing or invalid")
        return
    panel_chat = _current_chat()
    sel = get_selection(panel_chat)
    if not sel or not _self_client:
        return
    first_id = sorted(sel)[0]
    try:
        await _self_client.send_message(dst, text, reply_to=first_id)
    except Exception as exc:
        logger.warning("Ghost Room: reply failed: %s", exc)
    clear_selection(panel_chat)


async def _ghost_reply_no_quote_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.services.ghost_seen_service import get_selection, clear_selection
    dst = _resolve_ghost_destination()
    if dst is None:
        logger.warning("Ghost Room: reply blocked — GHOST_ROOM_ID missing or invalid")
        return
    panel_chat = _current_chat()
    sel = get_selection(panel_chat)
    if not sel or not _self_client:
        return
    try:
        await _self_client.send_message(dst, text)
    except Exception as exc:
        logger.warning("Ghost Room: reply failed: %s", exc)
    clear_selection(panel_chat)


async def _ghost_ai_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.services.ghost_seen_service import (
        get_selection,
        execute_ghost_seen_ai,
        clear_selection,
    )
    dst = _resolve_ghost_destination()
    if dst is None:
        logger.warning("Ghost Room: AI blocked — GHOST_ROOM_ID missing or invalid")
        return
    panel_chat = _current_chat()
    sel = get_selection(panel_chat)
    if not sel:
        return

    # Build selected message payloads
    sel_list = sorted(sel)
    selected_msgs: list[dict[str, Any]] = []
    try:
        from backend.telegram_api.messages import get_messages
        raw = await get_messages(_self_client, panel_chat, sel_list)
        selected_msgs = raw
    except Exception as exc:
        logger.warning("Ghost Room: fetch selected messages failed: %s", exc)

    ok, response = await execute_ghost_seen_ai(
        _store_owner_id, dst, text,
        selected_msgs, tz_str=_store_tz_str,
    )
    if ok and _self_client:
        try:
            await _self_client.send_message(dst, response)
        except Exception as exc:
            logger.warning("Ghost Room: AI response delivery failed: %s", exc)
    clear_selection(panel_chat)


# ── incoming listener ──


def _register_incoming_listener(client, owner_id: int) -> None:
    @client.on(events.NewMessage(incoming=True))
    async def _ghost_incoming_listener(event):
        try:
            if not event.is_private:
                return
            sender = await event.get_sender()
            sender_id = getattr(sender, "id", 0)
            if sender_id == owner_id:
                return

            text = event.raw_text or ""
            preview = text[:160] if text else ""
            display_name = ""
            if sender:
                first = getattr(sender, "first_name", "") or ""
                last = getattr(sender, "last_name", "") or ""
                display_name = f"{first} {last}".strip()
                if not display_name:
                    display_name = getattr(sender, "username", "") or ""
            if not display_name:
                display_name = str(sender_id)

            ts = ""
            try:
                dt = getattr(event, "date", None)
                if dt:
                    if dt.tzinfo is None:
                        from datetime import timezone as tz
                        dt = dt.replace(tzinfo=tz.utc)
                    ts = dt.isoformat()
            except Exception:
                pass

            from backend.services.ghost_seen_service import upsert_source_chat
            await upsert_source_chat(
                event.chat_id or 0,
                display_name,
                preview,
                ts,
            )
        except Exception as exc:
            logger.warning("Ghost Room: incoming listener error: %s", exc)


# ── registration ──


_current_panel_chat: int = 0


def _set_current_chat(chat_id: int) -> None:
    global _current_panel_chat
    _current_panel_chat = chat_id


def _current_chat() -> int:
    return _current_panel_chat


def _current_chat_str() -> str:
    return str(_current_panel_chat)


async def _return_ghost_list_after_action(event) -> tuple[str, str, list] | None:
    return await _ghost_list_panel_handler(event, "")


def register(client, owner_id: int, tz_str: str) -> None:
    configure(client, owner_id, tz_str)

    try:
        register_panel("ghost_seen", _ghost_list_panel_handler, parent="menu", title="👻 Ghost Seen")
        register_panel("ghost_chat", _ghost_chat_panel_handler, parent="ghost_seen", title="Chat")
        register_action("ghost_open", _ghost_open_action)
        register_action("ghost_toggle", _ghost_toggle_action)
        register_action("ghost_page", _ghost_page_action)
        register_action("ghost_clear", _ghost_clear_action)
        register_action("ghost_back", _ghost_back_action)
        register_action("ghost_ai_single", _ghost_ai_single_action)
        register_action("ghost_ai_multi", _ghost_ai_multi_action)
        register_input("ghost_chat", "reply", {
            "handler": _ghost_reply_input,
            "prompt": "Type your reply below. It will be sent as a quote reply.",
        })
        register_input("ghost_chat", "reply_no_quote", {
            "handler": _ghost_reply_no_quote_input,
            "prompt": "Type your reply below. It will be sent without quoting.",
        })
        register_input("ghost_chat", "ai_prompt", {
            "handler": _ghost_ai_input,
            "prompt": "Type your AI prompt. Selected message(s) will be included as context.",
        })
        _register_incoming_listener(client, owner_id)
        logger.info("Ghost Room registered OK")
    except Exception as exc:
        logger.error("Ghost Room registration FAILED: %s", exc)