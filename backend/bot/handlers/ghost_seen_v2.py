"""Ghost Seen v2 Stage 1: private-chat browser wiring."""
from __future__ import annotations

import logging
from urllib.parse import quote, unquote

from telethon import events
from telethon.tl.custom import Button

from backend.bot.handlers.guard import is_owner
from backend.helper import (
    InlinePanelBuilder,
    register_action,
    register_inline_builder,
    register_input,
    register_panel,
    render,
)
from backend.helper import inline_engine
from backend.helper.client import get_client
from backend.helper.lifecycle import get_lifecycle
from backend.services.ghost_seen_v2 import (
    BrowserPage,
    PrivateChat,
    is_private_user_entity,
    load_private_chats,
    page_items,
    render_browser,
)

logger = logging.getLogger(__name__)

_PANEL_ID = "ghost_seen_v2"
_SEARCH_INPUT_ID = "search"
_STATE_PREFIX = "p="


def _encode_state(page: int, query: str) -> str:
    return f"{_STATE_PREFIX}{max(1, int(page))}&q={quote(query or '', safe='')}"


def _decode_state(value: str) -> tuple[int, str]:
    raw = value or ""
    if raw.startswith(_STATE_PREFIX):
        raw = raw[len(_STATE_PREFIX):]
    page = 1
    query = ""
    for part in raw.split("&"):
        key, separator, val = part.partition("=")
        if key == "p" and separator and val.isdigit():
            page = max(1, int(val))
        elif key == "q" and separator:
            query = unquote(val)
    return page, query


def _callback_message_id(event) -> int:
    return int(getattr(event, "message_id", 0) or 0)


def _session_state(chat_id: int, msg_id: int) -> tuple[int, str]:
    if not chat_id or not msg_id:
        return 1, ""
    session = get_lifecycle().sessions.get(chat_id, msg_id)
    if session is None:
        return 1, ""
    nav = get_lifecycle().sessions.current_nav(chat_id, msg_id)
    if nav is None or nav[0] != _PANEL_ID:
        return 1, ""
    return _decode_state(nav[1])


def _set_session_state(chat_id: int, msg_id: int, page: int, query: str) -> None:
    if not chat_id or not msg_id:
        return
    sessions = get_lifecycle().sessions
    session = sessions.get(chat_id, msg_id)
    if session is None:
        return
    nav = sessions.current_nav(chat_id, msg_id)
    if nav is not None and nav[0] == _PANEL_ID:
        sessions.set_current_extra(chat_id, msg_id, _encode_state(page, query))


def _buttons(view: BrowserPage) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("🔎 Search", f"input:{_PANEL_ID}:{_SEARCH_INPUT_ID}")

    if view.chats:
        for chat in view.chats:
            builder.add_row(f"💬 {chat.display_name}", f"action:ghost_seen_v2_open:{chat.chat_id}")

    if view.total_pages > 1:
        navigation = []
        if view.page > 1:
            navigation.append(("‹", f"action:ghost_seen_v2_page:{view.page - 1}"))
        navigation.append((f"{view.page}/{view.total_pages}", "panel:_nav:noop"))
        if view.page < view.total_pages:
            navigation.append(("›", f"action:ghost_seen_v2_page:{view.page + 1}"))
        builder.add_buttons(*navigation)
    return builder.build()


async def _render_browser(owner_id: int, page: int = 1, query: str = "") -> tuple[str, str, list]:
    try:
        chats = await load_private_chats(inline_engine.get_self_client(), owner_id)
        body, view = render_browser(
            chats,
            page=page,
            query=query,
            watcher_count=len(chats),
        )
        return "👻 Ghost Seen", body, _buttons(view)
    except Exception:
        logger.exception("Ghost Seen v2: dialog load failed")
        return (
            "👻 Ghost Seen",
            "I lost sight of the whispers for a moment.",
            [[Button.inline("Retry", "action:ghost_seen_v2_retry")]],
        )


async def _browser_panel_handler(event, extra: str, owner_id: int) -> tuple[str, str, list] | None:
    page, query = _decode_state(extra)
    if not extra and event is not None:
        page, query = _session_state(
            int(getattr(event, "chat_id", 0) or 0),
            _callback_message_id(event),
        )
    return await _render_browser(owner_id, page, query)


async def _browser_inline_builder(event, extra: str) -> list:
    page, query = _decode_state(extra)
    title, body, buttons = await _render_browser(
        inline_engine.get_owner_id(), page, query,
    )
    return [render(title, body, buttons)]


async def _page_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    try:
        page = max(1, int(extra))
    except (TypeError, ValueError):
        page = 1
    msg_id = _callback_message_id(event)
    _, query = _session_state(chat_id, msg_id)
    _set_session_state(chat_id, msg_id, page, query)
    return await _render_browser(inline_engine.get_owner_id(), page, query)


async def _retry_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    page, query = _session_state(chat_id, _callback_message_id(event))
    return await _render_browser(inline_engine.get_owner_id(), page, query)


async def _open_chat_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    try:
        selected_id = int(extra)
    except (TypeError, ValueError):
        return "👻 Ghost Seen", "That whisper is no longer available.", []
    if selected_id <= 0:
        return "👻 Ghost Seen", "That whisper is no longer available.", []

    chats = await load_private_chats(inline_engine.get_self_client(), inline_engine.get_owner_id())
    selected = next((chat for chat in chats if chat.chat_id == selected_id), None)
    if selected is None:
        return "👻 Ghost Seen", "That whisper is no longer available.", []

    builder = InlinePanelBuilder()
    builder.add_row("‹ Back to chats", "panel:ghost_seen_v2")
    return (
        "👻 Ghost Seen",
        f"**{selected.display_name}**\n\nMessage Viewer will open here in Stage 2.",
        builder.build(),
    )


async def _search_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    owner_id = inline_engine.get_owner_id()
    query = (text or "").strip()
    try:
        chats = await load_private_chats(inline_engine.get_self_client(), owner_id)
        body, view = render_browser(
            chats,
            page=1,
            query=query,
            watcher_count=len(chats),
        )
        buttons = _buttons(view)
        _set_session_state(inline_chat_id, inline_msg_id, 1, query)
    except Exception:
        logger.exception("Ghost Seen v2: search load failed")
        body = "I lost sight of the whispers for a moment."
        buttons = [[Button.inline("Retry", "action:ghost_seen_v2_retry")]]

    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            from backend.helper.panel_render import render_edit

            text_to_edit, built_buttons = render_edit("👻 Ghost Seen", body, buttons)
            await helper.edit_message(
                inline_chat_id,
                inline_msg_id,
                message=text_to_edit,
                buttons=built_buttons,
            )
            get_lifecycle().set_render(
                inline_chat_id,
                inline_msg_id,
                text_to_edit,
                (text_to_edit, tuple()),
            )
        except Exception as exc:
            logger.warning("Ghost Seen v2: search panel edit failed: %s", exc)

    client = inline_engine.get_self_client()
    if client is not None:
        try:
            await client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _refresh_open_panels(client, owner_id: int) -> None:
    helper = get_client()
    if helper is None:
        return
    sessions = get_lifecycle().sessions
    pending = __import__("backend.helper.input_state", fromlist=["get_pending"]).get_pending(owner_id)
    if pending and pending.get("panel_id") == _PANEL_ID:
        return

    chats = await load_private_chats(client, owner_id)
    for panel_chat_id, panel_msg_id in sessions.all_keys():
        nav = sessions.current_nav(panel_chat_id, panel_msg_id)
        if nav is None or nav[0] != _PANEL_ID:
            continue
        page, query = _decode_state(nav[1])
        body, view = render_browser(
            chats,
            page=page,
            query=query,
            watcher_count=len(chats),
        )
        buttons = _buttons(view)
        try:
            from backend.helper.panels import _buttons_repr, _finalize_panel
            from backend.helper.panel_render import render_edit

            title, body, buttons = _finalize_panel(
                "👻 Ghost Seen",
                body,
                buttons,
                _PANEL_ID,
                panel_chat_id,
                panel_msg_id,
            )
            text_to_edit, built_buttons = render_edit(title, body, buttons)
            await helper.edit_message(
                panel_chat_id,
                panel_msg_id,
                message=text_to_edit,
                buttons=built_buttons,
            )
            sessions.set_current_extra(panel_chat_id, panel_msg_id, _encode_state(page, query))
            get_lifecycle().set_render(
                panel_chat_id,
                panel_msg_id,
                text_to_edit,
                (text_to_edit, _buttons_repr(built_buttons)),
            )
        except Exception as exc:
            logger.warning(
                "Ghost Seen v2: automatic panel refresh failed chat=%s msg=%s: %s",
                panel_chat_id,
                panel_msg_id,
                exc,
            )


def register(client, owner_id: int, tz_str: str = "UTC") -> None:
    register_panel(_PANEL_ID, lambda event, extra: _browser_panel_handler(event, extra, owner_id), parent="menu", title="👻 Ghost Seen")
    register_inline_builder(_PANEL_ID, _browser_inline_builder)
    register_action("ghost_seen_v2_page", _page_action)
    register_action("ghost_seen_v2_retry", _retry_action)
    register_action("ghost_seen_v2_open", _open_chat_action)
    register_input(_PANEL_ID, _SEARCH_INPUT_ID, {
        "handler": _search_input_handler,
        "prompt": "**Search private chats**\n\nSearch by first name, last name, or username:\n\n_Reply below._",
    })

    @client.on(events.NewMessage(incoming=True))
    async def ghost_seen_v2_incoming(event):
        try:
            if not getattr(event, "is_private", False):
                return
            entity = await event.get_chat()
            if not is_private_user_entity(entity, owner_id, user_hint=True):
                return
            await _refresh_open_panels(client, owner_id)
        except Exception:
            logger.exception("Ghost Seen v2: incoming update failed")
