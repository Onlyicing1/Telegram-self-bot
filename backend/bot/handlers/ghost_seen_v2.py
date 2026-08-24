"""Ghost Seen v2 Stage 1 browser, Stage 2 viewer, and Stage 4 actions."""
from __future__ import annotations

import logging
from urllib.parse import quote, unquote

from backend.helper import InlinePanelBuilder, register_action, register_inline_builder, register_input, register_panel, render
from backend.helper import inline_engine
from backend.helper.client import get_client
from backend.helper.lifecycle import get_lifecycle
from backend.services.ghost_seen_v2 import (
    BrowserPage, MessageViewerPage, action_menu_state, action_placeholder,
    clear_selection, get_selected_ids, is_private_user_entity, load_private_chats,
    load_viewer_messages, render_browser, render_message_viewer,
)

logger = logging.getLogger(__name__)
_PANEL_ID = "ghost_seen_v2"
_VIEWER_ID = "ghost_seen_v2_viewer"
_ACTION_ID = "ghost_seen_v2_actions"
_SEARCH_INPUT_ID = "search"


def _decode_state(value: str) -> tuple[int, str]:
    page, query = 1, ""
    for part in (value or "").split("&"):
        key, separator, val = part.partition("=")
        if key == "p" and separator and val.isdigit():
            page = max(1, int(val))
        elif key == "q" and separator:
            query = unquote(val)
    return page, query


def _event_ids(event) -> tuple[int, int]:
    return int(getattr(event, "chat_id", 0) or 0), int(getattr(event, "message_id", 0) or 0)


def _session_extra(chat_id: int, msg_id: int, panel_id: str) -> str:
    nav = get_lifecycle().sessions.current_nav(chat_id, msg_id)
    return nav[1] if nav and nav[0] == panel_id else ""


def _set_extra(chat_id: int, msg_id: int, extra: str) -> None:
    get_lifecycle().sessions.set_current_extra(chat_id, msg_id, extra)


def _browser_buttons(view: BrowserPage) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("🔎 Search", f"input:{_PANEL_ID}:{_SEARCH_INPUT_ID}")
    for chat in view.chats:
        builder.add_row(f"💬 {chat.display_name}", f"action:ghost_seen_v2_open:{chat.chat_id}")
    if view.total_pages > 1:
        nav = []
        if view.page > 1:
            nav.append(("‹", f"action:ghost_seen_v2_page:{view.page - 1}"))
        nav.append((f"{view.page}/{view.total_pages}", "panel:_nav:noop"))
        if view.page < view.total_pages:
            nav.append(("›", f"action:ghost_seen_v2_page:{view.page + 1}"))
        builder.add_buttons(*nav)
    return builder.build()


def _viewer_buttons(view: MessageViewerPage) -> list:
    builder = InlinePanelBuilder()
    for message in view.messages:
        label = "✓ Selected" if message.message_id in view.selected_ids else "Select"
        builder.add_row(label, f"action:ghost_seen_v2_select:{view.source_chat_id}|{message.message_id}")
    if view.selected_ids:
        builder.add_row(f"Clear ({len(view.selected_ids)})", f"action:ghost_seen_v2_clear:{view.source_chat_id}")
        builder.add_row(f"Actions ({len(view.selected_ids)})", f"action:ghost_seen_v2_actions:{view.source_chat_id}")
    builder.add_row("‹ Back to chats", "panel:ghost_seen_v2")
    if view.total_pages > 1:
        nav = []
        if view.page > 1:
            nav.append(("‹ Older", f"action:ghost_seen_v2_viewer_page:{view.page - 1}"))
        nav.append((f"{view.page}/{view.total_pages}", "panel:_nav:noop"))
        if view.page < view.total_pages:
            nav.append(("Newer ›", f"action:ghost_seen_v2_viewer_page:{view.page + 1}"))
        builder.add_buttons(*nav)
    return builder.build()


def _action_buttons(source: int, selected: tuple[int, ...]) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("Reply", f"action:ghost_seen_v2_placeholder:reply:{source}")
    builder.add_row("AI Reply", f"action:ghost_seen_v2_placeholder:ai_reply:{source}")
    builder.add_row("‹ Back", f"action:ghost_seen_v2_actions_back:{source}")
    return builder.build()


async def _render_browser(owner_id: int, page: int = 1, query: str = ""):
    chats = await load_private_chats(inline_engine.get_self_client(), owner_id)
    body, view = render_browser(chats, page, query, len(chats))
    return "👻 Ghost Seen", body, _browser_buttons(view)


async def _browser_panel_handler(event, extra: str, owner_id: int):
    return await _render_browser(owner_id, *_decode_state(extra))


async def _browser_inline_builder(event, extra: str):
    title, body, buttons = await _render_browser(inline_engine.get_owner_id(), *_decode_state(extra))
    return [render(title, body, buttons)]


async def _page_action(event, extra: str, chat_id: int):
    page, query = _decode_state(_session_extra(chat_id, _event_ids(event)[1], _PANEL_ID))
    page = max(1, int(extra)) if str(extra).isdigit() else page
    _set_extra(chat_id, _event_ids(event)[1], f"p={page}&q={quote(query, safe='')}")
    return await _render_browser(inline_engine.get_owner_id(), page, query)


async def _retry_action(event, extra: str, chat_id: int):
    return await _render_browser(inline_engine.get_owner_id(), *_decode_state(_session_extra(chat_id, _event_ids(event)[1], _PANEL_ID)))


async def _open_chat_action(event, extra: str, chat_id: int):
    selected_id = int(extra) if str(extra).lstrip("-").isdigit() else 0
    chats = await load_private_chats(inline_engine.get_self_client(), inline_engine.get_owner_id())
    selected = next((chat for chat in chats if chat.chat_id == selected_id), None)
    if selected is None:
        return "👀 Ghost Seen", "That private chat is no longer available.", []
    clear_selection(selected.chat_id)
    viewer = await load_viewer_messages(inline_engine.get_self_client(), selected.chat_id)
    msg_id = _event_ids(event)[1]
    session = get_lifecycle().sessions.get(chat_id, msg_id)
    if session is not None:
        session.nav_stack.append((_VIEWER_ID, f"source={selected.chat_id}&name={quote(selected.display_name, safe='')}&page={viewer.page}"))
    return "👀 Ghost Seen", render_message_viewer(selected.display_name, viewer), _viewer_buttons(viewer)


async def _viewer_page_action(event, extra: str, chat_id: int):
    msg_id = _event_ids(event)[1]
    source, name, _, current = _parse_viewer_state(_session_extra(chat_id, msg_id, _VIEWER_ID))
    page = max(1, int(extra)) if str(extra).isdigit() else current
    viewer = await load_viewer_messages(inline_engine.get_self_client(), source, page)
    _set_extra(chat_id, msg_id, f"source={source}&name={quote(name, safe='')}&page={viewer.page}")
    return "👀 Ghost Seen", render_message_viewer(name, viewer), _viewer_buttons(viewer)


async def _select_action(event, extra: str, chat_id: int):
    parts = str(extra).split("|")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return "👀 Ghost Seen", "Invalid message selection.", []
    source, message_id = map(int, parts)
    state_source, name, _, page = _parse_viewer_state(_session_extra(chat_id, _event_ids(event)[1], _VIEWER_ID))
    if source != state_source:
        return "👀 Ghost Seen", "That message is no longer available.", []
    from backend.services.ghost_seen_v2 import toggle_selection
    toggle_selection(source, message_id)
    viewer = await load_viewer_messages(inline_engine.get_self_client(), source, page)
    return "👀 Ghost Seen", render_message_viewer(name, viewer), _viewer_buttons(viewer)


async def _clear_action(event, extra: str, chat_id: int):
    source = int(extra) if str(extra).isdigit() else 0
    clear_selection(source)
    state = _session_extra(chat_id, _event_ids(event)[1], _VIEWER_ID)
    _, name, _, page = _parse_viewer_state(state)
    viewer = await load_viewer_messages(inline_engine.get_self_client(), source, page)
    return "👀 Ghost Seen", render_message_viewer(name, viewer), _viewer_buttons(viewer)


async def _actions_action(event, extra: str, chat_id: int):
    source = int(extra) if str(extra).isdigit() else 0
    selected = action_menu_state(source)
    if not selected:
        return "👀 Ghost Seen", "No messages are selected.", []
    state_source, name, _, _ = _parse_viewer_state(_session_extra(chat_id, _event_ids(event)[1], _VIEWER_ID))
    if source != state_source:
        return "👀 Ghost Seen", "That selection is no longer available.", []
    session = get_lifecycle().sessions.get(chat_id, _event_ids(event)[1])
    if session is not None and session.nav_stack:
        session.nav_stack.append((_ACTION_ID, f"source={source}&name={quote(name, safe='')}&ids={','.join(map(str, selected))}"))
    return "👀 Ghost Seen", f"💬 {name} · {len(selected)} selected\n\nChoose an action.", _action_buttons(source, selected)


async def _placeholder_action(event, extra: str, chat_id: int):
    parts = str(extra).split(":")
    if len(parts) != 2 or not parts[1].isdigit():
        return "👀 Ghost Seen", "That selection is no longer available.", []
    message = action_placeholder(parts[0], int(parts[1]), get_selected_ids(int(parts[1])))
    return "👀 Ghost Seen", message, []


async def _actions_back(event, extra: str, chat_id: int):
    source = int(extra) if str(extra).isdigit() else 0
    selected = get_selected_ids(source)
    viewer = await load_viewer_messages(inline_engine.get_self_client(), source)
    state = _session_extra(chat_id, _event_ids(event)[1], _ACTION_ID)
    _, name, _, _ = _parse_viewer_state(state)
    return "👀 Ghost Seen", render_message_viewer(name, viewer), _viewer_buttons(viewer)


def _parse_viewer_state(value: str) -> tuple[int, str, int, int]:
    values = {}
    for part in (value or "").split("&"):
        key, separator, val = part.partition("=")
        if separator:
            values[key] = unquote(val)
    return int(values.get("source", 0) or 0), values.get("name", "Unknown"), 0, int(values.get("page", 1) or 1)


async def _search_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    title, body, buttons = await _render_browser(inline_engine.get_owner_id(), 1, (text or "").strip())
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        from backend.helper.panel_render import render_edit
        rendered, built = render_edit(title, body, buttons)
        await helper.edit_message(inline_chat_id, inline_msg_id, message=rendered, buttons=built)
    client = inline_engine.get_self_client()
    if client is not None:
        await client.delete_messages(chat_id, [msg_id])


def register(client, owner_id: int, tz_str: str = "UTC") -> None:
    register_panel(_PANEL_ID, lambda event, extra: _browser_panel_handler(event, extra, owner_id), parent="menu", title="👻 Ghost Seen")
    register_panel(_VIEWER_ID, lambda event, extra: _viewer_panel_handler(event, extra, owner_id), parent=_PANEL_ID, title="👀 Ghost Seen")
    register_panel(_ACTION_ID, lambda event, extra: _actions_panel_handler(event, extra, owner_id), parent=_VIEWER_ID, title="👀 Ghost Seen")
    register_inline_builder(_PANEL_ID, _browser_inline_builder)
    register_action("ghost_seen_v2_page", _page_action)
    register_action("ghost_seen_v2_retry", _retry_action)
    register_action("ghost_seen_v2_open", _open_chat_action)
    register_action("ghost_seen_v2_viewer_page", _viewer_page_action)
    register_action("ghost_seen_v2_select", _select_action)
    register_action("ghost_seen_v2_clear", _clear_action)
    register_action("ghost_seen_v2_actions", _actions_action)
    register_action("ghost_seen_v2_placeholder", _placeholder_action)
    register_action("ghost_seen_v2_actions_back", _actions_back)
    register_input(_PANEL_ID, _SEARCH_INPUT_ID, {"handler": _search_input_handler, "prompt": "**Search private chats**\n\nSearch by first name, last name, or username:\n\n_Reply below._"})


async def _viewer_panel_handler(event, extra: str, owner_id: int):
    source, name, _, page = _parse_viewer_state(extra)
    viewer = await load_viewer_messages(inline_engine.get_self_client(), source, page)
    return "👀 Ghost Seen", render_message_viewer(name, viewer), _viewer_buttons(viewer)


async def _actions_panel_handler(event, extra: str, owner_id: int):
    source, name, _, _ = _parse_viewer_state(extra)
    selected = action_menu_state(source)
    if not selected:
        return "👀 Ghost Seen", "No messages are selected.", []
    return "👀 Ghost Seen", f"💬 {name} · {len(selected)} selected\n\nChoose an action.", _action_buttons(source, selected)
