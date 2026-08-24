"""
Ghost Seen handler — panels, incoming listener, and actions.

Panels:
  ghost_seen    — list of private chats (from ghost_chats table)
  ghost_chat    — five-message page for a specific chat

Actions:
  ghost_open:<chat_id>    — open a chat (resets unread, loads page 0)
  ghost_toggle:<msg_id>   — toggle selection of a message
  ghost_page:<dir>        — prev / next page (dir = prev | next)
  ghost_clear             — clear selection
  ghost_actions           — reply/action menu for the single selected
                            message (REPLY TARGET banner + explicit
                            choices; nothing executes on selection)
  ghost_ctx[:<n>]         — AI Reply: no argument shows the context-size
                            menu (1 / 5 / 10 / 20 messages, target
                            included); choosing a size opens the disclosure
                            choice. <n> travels in the callback data and is
                            re-validated by the service allow-list.
  ghost_inform:<yes|no>   — record disclosure choice and immediately
                            generate/deliver the fixed AI reply.
  ghost_remove            — remove the open conversation from the
                            Ghost Seen registry (registry row ONLY —
                            Telegram data is never touched)
  ghost_back              — back to chat list

Inputs:
  ghost_chat:reply          — quote-reply with typed text
  ghost_chat:reply_no_quote — reply without quote
  ghost_chat:ai_prompt      — legacy multi-select path only: an explicit
                              typed instruction over multiple selected messages;
                              never exposed for a single selection.

Incoming listener:
  One events.NewMessage(incoming=True) listener that upserts ghost_chats
  rows for private one-to-one HUMAN conversations only (bots, self-chat,
  and non-private sources are rejected at the registry boundary via the
  service validator). Lightweight, guarded, never raises into the
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


def _ghost_seen_env_id() -> str:
    import os
    return os.getenv("GHOST_ROOM_ID", "")


def _resolve_ghost_destination() -> int | None:
    """Resolve the GHOST_ROOM_ID env var to a valid Telegram chat ID.

    This is the SINGLE authoritative destination for all Ghost Seen output.
    Returns None when GHOST_ROOM_ID is missing, empty, or non-numeric —
    callers must fail closed and never fall back to another chat.
    """
    raw = _ghost_seen_env_id()
    if not raw:
        return None
    try:
        val = int(raw)
    except ValueError:
        logger.warning("Ghost Seen: GHOST_ROOM_ID=%r is not a valid integer", raw)
        return None
    if val < 0:
        logger.warning("Ghost Seen: GHOST_ROOM_ID=%d is negative", val)
        return None
    return val


def _is_ghost_enabled() -> bool:
    return bool(_ghost_seen_env_id())


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
    _, expired_ids = apply_retention(rows, settings_service.ghost_seen_retention_seconds())
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
    try:
        chat_id = int(parts[0]) if parts else 0
    except ValueError:
        chat_id = 0
    if not chat_id:
        builder = InlinePanelBuilder()
        _nav_buttons(builder)
        return "👻 Chat", "No chat selected.", builder.build()

    page = get_page(chat_id)
    msgs, error = await fetch_chunk(_self_client, chat_id, page)
    selected = get_selection(chat_id)

    lines = [f"**Chat {chat_id}** — page {page + 1}\n"]
    if error == "entity":
        lines.append("_This conversation is temporarily unavailable — its "
                     "contact could not be resolved in this session. "
                     "Try again later._")
        builder = InlinePanelBuilder()
        _nav_buttons(builder)
        return f"Chat {chat_id}", "\n".join(lines), builder.build()
    if error == "fetch":
        lines.append("_Could not load messages right now. Try again._")
        builder = InlinePanelBuilder()
        builder.add_row("🔄 Retry", f"action:ghost_open:{chat_id}")
        _nav_buttons(builder)
        return f"Chat {chat_id}", "\n".join(lines), builder.build()
    if not msgs:
        lines.append("_No messages in this conversation yet._")
        builder = InlinePanelBuilder()
        _nav_buttons(builder)
        return f"Chat {chat_id}", "\n".join(lines), builder.build()

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
    if n_sel == 1:
        builder.add_row("⚡ Reply / Actions", "action:ghost_actions")
    elif n_sel > 1:
        builder.add_row(f"🤖 AI on selection ({n_sel})", "input:ghost_chat:ai_prompt")
    if n_sel > 0:
        builder.add_row("✕ Clear", "action:ghost_clear")
    builder.add_row("🗑 Remove from list", "action:ghost_remove")
    builder.add_row("⬅ Back", "action:ghost_back")
    return f"Chat {chat_id}", "\n".join(lines), builder.build()


# ── actions ──


async def _ghost_open_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services.ghost_seen_service import cancel_reply_flow, clear_selection, set_page
    try:
        target = int(extra)
    except ValueError:
        builder = InlinePanelBuilder()
        _nav_buttons(builder)
        return "👻 Chat", "Invalid chat id.", builder.build()

    previous = _current_chat()
    if previous:
        clear_selection(previous)
        cancel_reply_flow(previous)
    clear_selection(target)
    cancel_reply_flow(target)
    set_page(target, 0)
    _set_current_chat(target)
    from backend.services.ghost_seen_service import clear_unread as clear_registry_unread
    await clear_registry_unread(target)
    return await _ghost_chat_panel_handler(event, str(target))


async def _ghost_toggle_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services.ghost_seen_service import toggle_selection
    try:
        msg_id = int(extra)
    except ValueError:
        return await _ghost_chat_panel_handler(event, _current_chat_str())

    panel_chat = _current_chat() or chat_id
    toggle_selection(panel_chat, msg_id)
    from backend.services.ghost_seen_service import cancel_reply_flow
    cancel_reply_flow(panel_chat)
    return await _ghost_chat_panel_handler(event, str(panel_chat))


async def _ghost_page_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services.ghost_seen_service import get_page, set_page
    panel_chat = _current_chat() or chat_id
    page = get_page(panel_chat)
    if extra == "prev":
        set_page(panel_chat, max(0, page - 1))
    elif extra == "next":
        set_page(panel_chat, page + 1)
    return await _ghost_chat_panel_handler(event, str(panel_chat))


async def _ghost_clear_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services.ghost_seen_service import cancel_reply_flow, clear_selection
    panel_chat = _current_chat() or chat_id
    clear_selection(panel_chat)
    cancel_reply_flow(panel_chat)
    return await _ghost_chat_panel_handler(event, str(panel_chat))


async def _ghost_back_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services.ghost_seen_service import clear_selection, cancel_reply_flow

    panel_chat = _current_chat() or chat_id
    clear_selection(panel_chat)
    cancel_reply_flow(panel_chat)
    return await _ghost_list_panel_handler(event, extra)


async def _ghost_actions_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Reply/action menu for the single selected message (the anchor).

    Shows an unambiguous REPLY TARGET banner and defers every action to an
    explicit choice — nothing executes merely because something is selected.
    """
    from backend.services.ghost_seen_service import (
        get_selection,
        start_reply_flow,
        format_reply_target,
    )

    panel_chat = _current_chat() or chat_id
    sel = get_selection(panel_chat)
    if len(sel) != 1 or not _self_client:
        builder = InlinePanelBuilder()
        builder.add_row("⬅ Back", "action:ghost_back")
        return "👻 Reply / Actions", "Select exactly one message first.", builder.build()

    from backend.services.ghost_seen_service import ensure_entity
    await ensure_entity(_self_client, panel_chat)
    anchor_id = sorted(sel)[0]
    anchor: dict[str, Any] = {}
    try:
        from backend.telegram_api.messages import get_messages as fetch_by_ids
        fetched = await fetch_by_ids(_self_client, panel_chat, [anchor_id])
        if fetched:
            anchor = fetched[0] or {}
    except Exception as exc:
        logger.warning("Ghost Seen: anchor fetch failed: %s", exc)

    start_reply_flow(panel_chat, anchor_id)
    if anchor:
        banner = format_reply_target(anchor, _store_owner_id)
    else:
        banner = (
            f"↩ **Reply target:** #{anchor_id}\n"
            "_Content unavailable right now._"
        )

    builder = InlinePanelBuilder()
    builder.add_row("💬 Reply myself (quote)", "input:ghost_chat:reply")
    builder.add_row("💬 Reply myself (no quote)", "input:ghost_chat:reply_no_quote")
    builder.add_row("🤖 AI Reply", "action:ghost_ctx")
    builder.add_row("⬅ Back", "action:ghost_back")
    return "👻 Reply / Actions", banner, builder.build()


async def _ghost_ctx_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Render and validate the explicit AI context-size choice."""
    from backend.services.ghost_seen_service import (
        get_reply_flow,
        set_reply_context_count,
        cancel_reply_flow,
        ALLOWED_CONTEXT_COUNTS,
    )

    panel_chat = _current_chat()
    flow = get_reply_flow(panel_chat)
    fail_builder = InlinePanelBuilder()
    fail_builder.add_row("⬅ Back", "action:ghost_back")
    if not flow:
        return "👻 AI Reply", "Select a reply target first.", fail_builder.build()

    if not extra:
        builder = InlinePanelBuilder()
        for n in ALLOWED_CONTEXT_COUNTS:
            label = "1 message" if n == 1 else f"{n} messages"
            builder.add_row(label, f"action:ghost_ctx:{n}")
        builder.add_row("⬅ Back", "action:ghost_actions")
        body = (
            "How much context should the AI receive?\n\n"
            "The selected target is always included and is the final "
            "message in the exact context window."
        )
        return "👻 AI Reply — context", body, builder.build()

    try:
        n = int(extra)
    except ValueError:
        n = -1
    if not set_reply_context_count(panel_chat, n):
        cancel_reply_flow(panel_chat)
        builder = InlinePanelBuilder()
        builder.add_row("⬅ Back", "action:ghost_actions")
        allowed = ", ".join(str(v) for v in ALLOWED_CONTEXT_COUNTS)
        return "👻 AI Reply", f"Invalid context size. Allowed: {allowed}.", builder.build()

    builder = InlinePanelBuilder()
    builder.add_row("🤖 Inform recipient", "action:ghost_inform:yes")
    builder.add_row("🔒 Don't inform recipient", "action:ghost_inform:no")
    builder.add_row("⬅ Back", "action:ghost_ctx")
    body = (
        f"Context: **{n}** message(s), ending at the selected reply target.\n\n"
        "Should the recipient be informed that the reply was generated by AI?"
    )
    return "👻 AI Reply — disclosure", body, builder.build()


async def _execute_single_ghost_ai_reply(panel_chat: int) -> tuple[bool, str]:
    """Run the fixed single-message Ghost Seen reply after disclosure."""
    from backend.services.ghost_seen_service import (
        consume_reply_flow,
        fetch_context_window,
        execute_ghost_seen_ai,
        AI_DISCLOSURE_SUFFIX,
        clear_selection,
    )

    flow = consume_reply_flow(panel_chat)
    if not flow:
        clear_selection(panel_chat)
        return False, "AI reply state expired — select the message again."
    dst = _resolve_ghost_destination()
    if dst is None:
        clear_selection(panel_chat)
        return False, "AI reply blocked — GHOST_ROOM_ID is missing or invalid."
    try:
        context_msgs = await fetch_context_window(
            _self_client, panel_chat, flow["anchor"], flow["context_n"],
        )
    except Exception as exc:
        logger.warning("Ghost Seen: context fetch failed: %s", exc)
        context_msgs = []
    if not context_msgs or not _self_client:
        clear_selection(panel_chat)
        return False, "AI reply blocked — reply-target context is unavailable."
    if context_msgs[-1].get("out") is True:
        clear_selection(panel_chat)
        return False, "AI reply blocked — selected target is not a recipient message."

    ok, response = await execute_ghost_seen_ai(
        _store_owner_id, panel_chat, "", context_msgs, tz_str=_store_tz_str,
    )
    if not ok:
        clear_selection(panel_chat)
        return False, response or "AI reply generation failed."
    final_text = response + AI_DISCLOSURE_SUFFIX if flow["informed"] else response
    try:
        await _self_client.send_message(dst, final_text)
    except Exception as exc:
        clear_selection(panel_chat)
        logger.warning("Ghost Seen: AI response delivery failed: %s", exc)
        return False, "AI reply delivery failed."
    clear_selection(panel_chat)
    return True, "AI reply generated and delivered."


async def _ghost_inform_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Record disclosure choice and immediately execute the fixed AI reply."""
    from backend.services.ghost_seen_service import set_reply_disclosure, cancel_reply_flow

    panel_chat = _current_chat() or chat_id
    if extra not in ("yes", "no"):
        cancel_reply_flow(panel_chat)
        builder = InlinePanelBuilder()
        builder.add_row("⬅ Back", "action:ghost_actions")
        return "👻 AI Reply — disclosure", "Invalid disclosure choice.", builder.build()
    if not set_reply_disclosure(panel_chat, extra == "yes"):
        cancel_reply_flow(panel_chat)
        builder = InlinePanelBuilder()
        builder.add_row("⬅ Back", "action:ghost_back")
        return "👻 AI Reply", "AI reply state expired — select the message again.", builder.build()

    ok, status = await _execute_single_ghost_ai_reply(panel_chat)
    builder = InlinePanelBuilder()
    builder.add_row("⬅ Back", "action:ghost_back")
    return "👻 AI Reply", f"{'✅' if ok else '❌'} {status}", builder.build()


async def _ghost_remove_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Remove the open conversation from the Ghost Seen registry.

    Registry row and local UI state only — Telegram chats, messages, and
    read state are never touched.
    """
    from backend.services.ghost_seen_service import remove_chat

    panel_chat = _current_chat() or chat_id
    await remove_chat(panel_chat)
    return await _return_ghost_list_after_action(event)


# ── reply inputs ──


async def _ghost_reply_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.services.ghost_seen_service import get_selection, clear_selection
    dst = _resolve_ghost_destination()
    if dst is None:
        logger.warning("Ghost Seen: reply blocked — GHOST_ROOM_ID missing or invalid")
        return
    panel_chat = _current_chat() or chat_id
    sel = get_selection(panel_chat)
    if not sel or not _self_client:
        return
    first_id = sorted(sel)[0]
    try:
        await _self_client.send_message(dst, text, reply_to=first_id)
    except Exception as exc:
        logger.warning("Ghost Seen: reply failed: %s", exc)
    clear_selection(panel_chat)


async def _ghost_reply_no_quote_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.services.ghost_seen_service import get_selection, clear_selection
    dst = _resolve_ghost_destination()
    if dst is None:
        logger.warning("Ghost Seen: reply blocked — GHOST_ROOM_ID missing or invalid")
        return
    panel_chat = _current_chat() or chat_id
    sel = get_selection(panel_chat)
    if not sel or not _self_client:
        return
    try:
        await _self_client.send_message(dst, text)
    except Exception as exc:
        logger.warning("Ghost Seen: reply failed: %s", exc)
    clear_selection(panel_chat)


async def _ghost_ai_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    """Legacy multi-select path; a single selection fails closed."""
    from backend.services.ghost_seen_service import (
        get_selection,
        clear_selection,
        execute_ghost_seen_ai,
    )
    panel_chat = _current_chat() or chat_id
    dst = _resolve_ghost_destination()
    if dst is None:
        logger.warning("Ghost Seen: AI blocked — GHOST_ROOM_ID missing or invalid")
        return

    sel = get_selection(panel_chat)
    if len(sel) < 2 or not _self_client:
        logger.warning("Ghost Seen: legacy AI prompt blocked for single selection")
        clear_selection(panel_chat)
        return
    sel_list = sorted(sel)
    try:
        from backend.telegram_api.messages import get_messages
        context_msgs = await get_messages(_self_client, panel_chat, sel_list)
    except Exception as exc:
        logger.warning("Ghost Seen: fetch selected messages failed: %s", exc)
        context_msgs = []
    if not context_msgs:
        return

    ok, response = await execute_ghost_seen_ai(
        _store_owner_id, panel_chat, text,
        context_msgs, tz_str=_store_tz_str,
    )
    if ok and _self_client:
        try:
            await _self_client.send_message(dst, response)
        except Exception as exc:
            logger.warning("Ghost Seen: AI response delivery failed: %s", exc)
    clear_selection(panel_chat)


# ── incoming listener ──


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

            text = event.raw_text or ""
            preview = text[:160] if text else ""

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
            logger.warning("Ghost Seen: incoming listener error: %s", exc)


# ── registration ──


_current_panel_chat: int = 0


def _set_current_chat(chat_id: int) -> None:
    global _current_panel_chat
    _current_panel_chat = chat_id


def _current_chat() -> int:
    return _current_panel_chat


def current_chat_id() -> int:
    """Return the source private chat currently shown by Ghost Seen."""
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
        register_action("ghost_actions", _ghost_actions_action)
        register_action("ghost_ctx", _ghost_ctx_action)
        register_action("ghost_inform", _ghost_inform_action)
        register_action("ghost_remove", _ghost_remove_action)
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
            "prompt": "Type your instruction for the selected messages.",
        })
        _register_incoming_listener(client, owner_id)
        logger.info("Ghost Seen registered OK")
    except Exception as exc:
        logger.error("Ghost Seen registration FAILED: %s", exc)