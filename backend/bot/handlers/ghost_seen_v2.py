"""Ghost Seen v2 — browser, viewer, actions, reply modes, and privacy."""
from __future__ import annotations

import asyncio
import logging
import uuid
from dataclasses import dataclass
from urllib.parse import quote, unquote

from backend.helper import (
    InlinePanelBuilder, register_action, register_inline_builder,
    register_input, register_panel, render,
)
from backend.helper import inline_engine
from backend.helper.client import get_client
from backend.helper.lifecycle import get_lifecycle
from backend.services import ghost_seen_v2 as service
from backend.services.ghost_seen_v2 import (
    BrowserPage, MessageViewerPage, _ensure_allowed_loaded_async,
    action_menu_state, action_placeholder, allow_chat, begin_reply,
    clear_reply, clear_selection, consume_reply, disallow_chat,
    get_allowed_chats, get_selected_ids, is_chat_allowed,
    is_private_user_entity, build_ai_reply_prompt, load_context_messages, load_manage_directory, load_viewer_messages,

    manage_page_items, reply_mode, reply_target, render_browser,
    render_message_viewer, resolve_allowed_chats, send_message_plain,
    send_reply, truncate_preview,
)

logger = logging.getLogger(__name__)
_PANEL_ID = "ghost_seen_v2"
_VIEWER_ID = "ghost_seen_v2_viewer"
_ACTION_ID = "ghost_seen_v2_actions"
_MANAGE_ID = "ghost_seen_v2_manage"
_AI_TIMEOUT_S = 45.0
_TELEGRAM_TEXT_LIMIT = 4096
_ai_states: dict[int, dict] = {}
_ai_locks: dict[int, asyncio.Lock] = {}
_SEARCH_INPUT_ID = "search"


@dataclass(frozen=True)
class _AIReplyOperation:
    panel_chat_id: int
    source_chat_id: int
    selected_message_id: int
    context_count: int
    disclosure: bool
    request_id: str


def _ai_lock(panel_chat_id: int) -> asyncio.Lock:
    return _ai_locks.setdefault(int(panel_chat_id), asyncio.Lock())


def _clear_ai_state(panel_chat_id: int, source_chat_id: int | None = None) -> None:
    _ai_states.pop(int(panel_chat_id), None)
    if source_chat_id is not None:
        clear_selection(int(source_chat_id))


def _valid_context_count(value: int) -> bool:
    return int(value) in {1, 5, 10, 20}


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


def _parse_viewer_state(value: str) -> tuple[int, str, int, int]:
    values = {}
    for part in (value or "").split("&"):
        key, separator, val = part.partition("=")
        if separator:
            values[key] = unquote(val)
    return (
        int(values.get("source", 0) or 0),
        values.get("name", "Unknown"),
        0,
        int(values.get("page", 1) or 1),
    )


# ── Browser ──


def _browser_buttons(view: BrowserPage) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("🔎 Search", f"input:{_PANEL_ID}:{_SEARCH_INPUT_ID}")
    builder.add_row("⚙ Manage", f"panel:{_MANAGE_ID}")
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


async def _render_browser(owner_id: int, page: int = 1, query: str = ""):
    chats = await resolve_allowed_chats(inline_engine.get_self_client(), owner_id)
    watcher_count = len(get_allowed_chats())
    body, view = render_browser(chats, page, query, watcher_count)
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


# ── Viewer ──


def _viewer_buttons(view: MessageViewerPage) -> list:
    builder = InlinePanelBuilder()
    for message in view.messages:
        is_selected = message.message_id in view.selected_ids
        preview_snip = message.text[:20] + ("…" if len(message.text) > 20 else "") if message.text else "Media"
        label = f"✓ {preview_snip}" if is_selected else f"Select {preview_snip}"
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


async def _open_chat_action(event, extra: str, chat_id: int):
    selected_id = int(extra) if str(extra).lstrip("-").isdigit() else 0
    if not is_chat_allowed(selected_id):
        return "👀 Ghost Seen", "This chat is not allowed. Use ⚙ Manage to enable it.", []
    chats = await resolve_allowed_chats(inline_engine.get_self_client(), inline_engine.get_owner_id())
    selected = next((chat for chat in chats if chat.chat_id == selected_id), None)
    if selected is None:
        return "👀 Ghost Seen", "That private chat is no longer available.", []
    clear_selection(selected.chat_id)
    clear_reply(chat_id)
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


# ── Selection ──


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


# ── Action Menu ──


def _action_buttons(source: int, selected: tuple[int, ...]) -> list:
    builder = InlinePanelBuilder()
    if len(selected) == 1:
        builder.add_row("Reply", f"action:ghost_seen_v2_reply:{source}")
        builder.add_row("Send without reply", f"action:ghost_seen_v2_send_plain:{source}")
    if len(selected) == 1:
        builder.add_row("AI Reply", f"action:ghost_seen_v2_ai_reply:{source}")
    builder.add_row("‹ Back", f"action:ghost_seen_v2_actions_back:{source}")
    return builder.build()


async def _actions_action(event, extra: str, chat_id: int):
    source = int(extra) if str(extra).isdigit() else 0
    if not is_chat_allowed(source):
        return "👀 Ghost Seen", "That chat is no longer allowed.", []
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


async def _ai_reply_action(event, extra: str, chat_id: int):
    source = int(extra) if str(extra).isdigit() else 0
    target_id = reply_target(source)
    state_source, name, _, _ = _parse_viewer_state(_session_extra(chat_id, _event_ids(event)[1], _ACTION_ID))
    if target_id is None or source != state_source or not is_chat_allowed(source):
        return "👀 Ghost Seen", "AI Reply needs exactly one allowed selected message.", []
    return "👀 Ghost Seen · AI Reply", f"💬 {name}\n\nHow much conversation should Ghost use?", _context_buttons(source)


def _context_buttons(source: int) -> list:
    builder = InlinePanelBuilder()
    for count in (1, 5, 10, 20):
        builder.add_row(str(count), f"action:ghost_seen_v2_ai_generate:{source}:{count}")
    builder.add_row("← Back", f"action:ghost_seen_v2_actions_back:{source}")
    return builder.build()


async def _ai_generate_action(event, extra: str, chat_id: int):
    parts = str(extra).split(":")
    if len(parts) != 2 or not all(part.isdigit() for part in parts):
        return "👀 Ghost Seen", "Invalid context selection.", []
    source, count = map(int, parts)
    if not _valid_context_count(count):
        return "👀 Ghost Seen", "Invalid context selection.", []
    target_id = reply_target(source)
    state_source, name, _, _ = _parse_viewer_state(_session_extra(chat_id, _event_ids(event)[1], _ACTION_ID))
    if target_id is None or source != state_source or not is_chat_allowed(source):
        return "👀 Ghost Seen", "That AI selection is no longer available.", []
    context = await load_context_messages(inline_engine.get_self_client(), source, target_id, count)
    target = await _load_target_message(inline_engine.get_self_client(), source, target_id)
    if target is None:
        target = service.ViewerMessage(target_id, source, "Target message")
    _ai_states[chat_id] = {"panel_chat_id": chat_id, "source_chat_id": source, "selected_message_id": target_id, "context_count": count, "name": name}
    builder = InlinePanelBuilder()
    builder.add_row("Yes", f"action:ghost_seen_v2_ai_disclosure:yes:{source}")
    builder.add_row("No", f"action:ghost_seen_v2_ai_disclosure:no:{source}")
    builder.add_row("← Back", f"action:ghost_seen_v2_ai_reply:{source}")
    return "👀 Ghost Seen · AI Reply", "Should the recipient be told this reply was written by AI?", builder.build()


async def _load_target_message(client, source: int, target_id: int):
    if client is None:
        return None
    async for item in client.iter_messages(source, limit=1, min_id=target_id - 1, max_id=target_id + 1):
        if int(getattr(item, "id", 0) or 0) == target_id:
            return service.ViewerMessage(target_id, source, service._message_preview(item) or "Unsupported message", getattr(item, "date", None))
    return None


async def _ai_disclosure_action(event, extra: str, chat_id: int):
    parts = str(extra).split(":")
    if len(parts) != 2 or parts[0] not in {"yes", "no"} or not parts[1].isdigit():
        return "👀 Ghost Seen", "Invalid disclosure choice.", []
    disclosure, source = parts[0] == "yes", int(parts[1])
    state = _ai_states.get(chat_id)
    if not state or state.get("source_chat_id") != source:
        return "👀 Ghost Seen", "That AI selection is no longer available.", []
    return await _run_ai_reply(chat_id, source, int(state["selected_message_id"]), int(state["context_count"]), disclosure, str(state.get("name", "Unknown")))


async def _run_ai_reply(chat_id: int, source: int, target_id: int, count: int, disclosure: bool, name: str):
    if not _valid_context_count(count) or not is_chat_allowed(source) or reply_target(source) != target_id:
        return "👀 Ghost Seen", "That AI selection is no longer available.", []
    lock = _ai_lock(chat_id)
    if lock.locked():
        return "👀 Ghost Seen", "That AI reply is already being processed.", []
    async with lock:
        if not is_chat_allowed(source) or reply_target(source) != target_id:
            _clear_ai_state(chat_id, source)
            return "👀 Ghost Seen", "That AI selection is no longer available.", []
        context = await load_context_messages(inline_engine.get_self_client(), source, target_id, count)
        target = await _load_target_message(inline_engine.get_self_client(), source, target_id)
        if target is None:
            _clear_ai_state(chat_id, source)
            return "👀 Ghost Seen", "The selected message is no longer available.", []
        operation = _AIReplyOperation(chat_id, source, target_id, count, disclosure, uuid.uuid4().hex)
        _ai_states[chat_id] = {**operation.__dict__, "status": "generating", "name": name}
        prompt = build_ai_reply_prompt(context, target, inline_engine.get_owner_id())
        engine = __import__("backend.ai.engine.engine", fromlist=["get_engine"]).get_engine()
        from backend.ai.session.request import AIRequest
        try:
            result = await asyncio.wait_for(
                engine.execute(AIRequest(session_id=operation.request_id, request_id=operation.request_id, user_message=prompt, owner_id=inline_engine.get_owner_id(), chat_id=source, message_id=target_id)),
                timeout=_AI_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            _clear_ai_state(chat_id, source)
            raise
        except asyncio.TimeoutError:
            _clear_ai_state(chat_id, source)
            return "👀 Ghost Seen", "✕ AI generation timed out.", []
        except Exception:
            _clear_ai_state(chat_id, source)
            return "👀 Ghost Seen", "✕ Couldn't generate the reply.", []
        response = getattr(result, "response", "")
        if not getattr(result, "success", False) or not isinstance(response, str) or not response.strip():
            _clear_ai_state(chat_id, source)
            return "👀 Ghost Seen", "✕ Couldn't generate the reply.", []
        reply_text = str(result.response).strip()
        if disclosure:
            reply_text += "\n\n— Written with AI assistance."
        if len(reply_text) > _TELEGRAM_TEXT_LIMIT:
            _clear_ai_state(chat_id, source)
            return "👀 Ghost Seen", "✕ The generated reply is too long to send.", []
        if not is_chat_allowed(source) or reply_target(source) != target_id:
            _clear_ai_state(chat_id, source)
            return "👀 Ghost Seen", "✕ The AI selection changed before delivery.", []
        try:
            await send_reply(inline_engine.get_self_client(), source, target_id, reply_text)
        except asyncio.CancelledError:
            _clear_ai_state(chat_id, source)
            raise
        except Exception:
            _clear_ai_state(chat_id, source)
            return "👀 Ghost Seen", "✕ Couldn't send the reply.", []
        _clear_ai_state(chat_id, source)
        return "👀 Ghost Seen", "✓ Reply sent.", []


async def _placeholder_action(event, extra: str, chat_id: int):
    parts = str(extra).split(":")
    if len(parts) != 2 or not parts[1].isdigit():
        return "👀 Ghost Seen", "That selection is no longer available.", []
    message = action_placeholder(parts[0], int(parts[1]), get_selected_ids(int(parts[1])))
    return "👀 Ghost Seen", message, []


# ── Reply / Send modes ──


def _begin_input_for(source: int, chat_id: int, msg_id: int, name: str, mode: str) -> tuple[str, str, list]:
    from backend.helper.input_state import clear_pending, set_pending
    owner = inline_engine.get_owner_id()
    message_id = reply_target(source)
    if message_id is None:
        return "👀 Ghost Seen", "This action needs exactly one selected message.", _action_buttons(source, get_selected_ids(source))
    state_source, _, _, _ = _parse_viewer_state(_session_extra(chat_id, msg_id, _ACTION_ID))
    if source != state_source:
        return "👀 Ghost Seen", "That selection is no longer available.", []
    if not is_chat_allowed(source):
        return "👀 Ghost Seen", "That chat is no longer allowed.", []
    if not begin_reply(chat_id, source, message_id, msg_id, mode=mode):
        return "👀 Ghost Seen", "That selection is no longer available.", []
    clear_pending(owner)
    if mode == "reply":
        prompt = f"**👻 Ghost Seen — Reply**\n💬 {name}\n\nType your reply below.\nIt will be sent as a reply to the selected message."
    else:
        prompt = f"**👻 Ghost Seen — Send**\n💬 {name}\n\nType your message below.\nIt will be sent without a reply quote."
    set_pending(
        owner, _PANEL_ID, _ghost_reply_input_handler,
        chat_id, prompt,
        inline_chat_id=chat_id, inline_msg_id=msg_id,
        extra=f"source={source}", timeout=120.0,
    )
    builder = InlinePanelBuilder()
    builder.add_row("✖ Cancel", f"action:ghost_seen_v2_reply_cancel:{source}")
    return "👀 Ghost Seen", prompt, builder.build()


async def _reply_action(event, extra: str, chat_id: int):
    msg_id = _event_ids(event)[1]
    source = int(extra) if str(extra).isdigit() else 0
    if reply_target(source) is None:
        if action_menu_state(source) is None:
            viewer = await load_viewer_messages(inline_engine.get_self_client(), source)
            state = _session_extra(chat_id, msg_id, _ACTION_ID)
            _, name, _, _ = _parse_viewer_state(state)
            return "👀 Ghost Seen", render_message_viewer(name, viewer), _viewer_buttons(viewer)
        return "👀 Ghost Seen", "Reply needs exactly one selected message.", _action_buttons(source, get_selected_ids(source))
    state_source, name, _, _ = _parse_viewer_state(_session_extra(chat_id, msg_id, _ACTION_ID))
    return _begin_input_for(source, chat_id, msg_id, name, mode="reply")


async def _send_plain_action(event, extra: str, chat_id: int):
    msg_id = _event_ids(event)[1]
    source = int(extra) if str(extra).isdigit() else 0
    if reply_target(source) is None:
        return "👀 Ghost Seen", "Send needs exactly one selected message.", _action_buttons(source, get_selected_ids(source))
    state_source, name, _, _ = _parse_viewer_state(_session_extra(chat_id, msg_id, _ACTION_ID))
    return _begin_input_for(source, chat_id, msg_id, name, mode="send")


async def _ghost_reply_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client

    state = consume_reply(chat_id)
    if state is None:
        result = "👻 The input window expired. Select a message and try again."
    else:
        source, message_id, _panel_msg_id, mode = state
        if reply_target(source) != message_id:
            result = "✏️ The selected message changed. Nothing was sent."
        elif not (text or "").strip():
            result = "✏ Empty messages are not sent. Select the message and try again."
        else:
            try:
                if mode == "send":
                    await send_message_plain(_self_client, source, text)
                else:
                    await send_reply(_self_client, source, message_id, text)
                clear_selection(source)
                result = "✅ Sent." if mode == "send" else "✅ Reply sent."
            except Exception as exc:
                result = f"❌ Send failed: {exc}"
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=[])
        except Exception as exc:
            logger.warning("reply result edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _reply_cancel_action(event, extra: str, chat_id: int):
    from backend.helper.input_state import clear_pending
    clear_pending(inline_engine.get_owner_id())
    clear_reply(chat_id)
    source = int(extra) if str(extra).isdigit() else 0
    selected = action_menu_state(source)
    if not selected:
        viewer = await load_viewer_messages(inline_engine.get_self_client(), source)
        state = _session_extra(chat_id, _event_ids(event)[1], _ACTION_ID)
        _, name, _, _ = _parse_viewer_state(state)
        return "👀 Ghost Seen", render_message_viewer(name, viewer), _viewer_buttons(viewer)
    state_source, name, _, _ = _parse_viewer_state(_session_extra(chat_id, _event_ids(event)[1], _ACTION_ID))
    return "👀 Ghost Seen", f"💬 {name} · {len(selected)} selected\n\nChoose an action.", _action_buttons(source, selected)


async def _actions_back(event, extra: str, chat_id: int):
    source = int(extra) if str(extra).isdigit() else 0
    clear_reply(chat_id)
    viewer = await load_viewer_messages(inline_engine.get_self_client(), source)
    state = _session_extra(chat_id, _event_ids(event)[1], _ACTION_ID)
    _, name, _, _ = _parse_viewer_state(state)
    return "👀 Ghost Seen", render_message_viewer(name, viewer), _viewer_buttons(viewer)


# ── Manage Permissions (bounded, paginated, searchable) ──

_MANAGE_SEARCH_ID = "search"


def _manage_buttons(view: BrowserPage) -> list:
    """Manage keyboard with at most ``MANAGE_PAGE_SIZE`` chat rows plus
    navigation, search, and Back — never the full private-chat list."""
    builder = InlinePanelBuilder()
    for index, chat in enumerate(view.chats, start=1):
        state = "ON" if is_chat_allowed(chat.chat_id) else "OFF"
        builder.add_row(f"{index}. 💬 {chat.display_name}  {state}", f"action:ghost_seen_v2_toggle:{chat.chat_id}")
    if view.total_pages > 1:
        nav = []
        if view.page > 1:
            nav.append(("‹ Previous", f"action:ghost_seen_v2_manage_page:{view.page - 1}"))
        nav.append((f"{view.page}/{view.total_pages}", "panel:_nav:noop"))
        if view.page < view.total_pages:
            nav.append(("Next ›", f"action:ghost_seen_v2_manage_page:{view.page + 1}"))
        builder.add_buttons(*nav)
    builder.add_row("🔎 Search", f"input:{_MANAGE_ID}:{_MANAGE_SEARCH_ID}")
    builder.add_row("← Back", "panel:ghost_seen_v2")
    return builder.build()


def _render_manage(chats: list, page: int = 1, query: str = "") -> tuple[str, str, list]:
    view = manage_page_items(chats, page, query)
    body = "Choose which private chats Ghost Seen is allowed to watch."
    if view.query:
        body = f"{body}\n\nSearch: {truncate_preview(view.query, 32)}"
    if not view.chats:
        body = "No private chats match." if view.query else "No private chats found."
    return "⚙ Ghost Seen Permissions", body, _manage_buttons(view)


async def _manage_panel_handler(event, extra: str, owner_id: int):
    page, query = _decode_state(extra)
    chats = await load_manage_directory(inline_engine.get_self_client(), owner_id)
    return _render_manage(chats, page, query)


async def _manage_inline_builder(event, extra: str):
    page, query = _decode_state(extra)
    chats = await load_manage_directory(inline_engine.get_self_client(), inline_engine.get_owner_id())
    title, body, buttons = _render_manage(chats, page, query)
    return [render(title, body, buttons)]


async def _manage_page_action(event, extra: str, chat_id: int):
    page, query = _decode_state(_session_extra(chat_id, _event_ids(event)[1], _MANAGE_ID))
    page = max(1, int(extra)) if str(extra).isdigit() else page
    _set_extra(chat_id, _event_ids(event)[1], f"p={page}&q={quote(query, safe='')}")
    chats = await load_manage_directory(inline_engine.get_self_client(), inline_engine.get_owner_id())
    return _render_manage(chats, page, query)


async def _toggle_permission_action(event, extra: str, chat_id: int):
    await _ensure_allowed_loaded_async()
    source = int(extra) if str(extra).lstrip("-").isdigit() else 0
    if source <= 0:
        return "⚙ Permissions", "Invalid chat.", []
    if is_chat_allowed(source):
        disallow_chat(source)
    else:
        allow_chat(source)
    page, query = _decode_state(_session_extra(chat_id, _event_ids(event)[1], _MANAGE_ID))
    chats = await load_manage_directory(inline_engine.get_self_client(), inline_engine.get_owner_id())
    return _render_manage(chats, page, query)


async def _manage_search_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    chats = await load_manage_directory(inline_engine.get_self_client(), inline_engine.get_owner_id())
    title, body, buttons = _render_manage(chats, 1, (text or "").strip())
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        from backend.helper.panel_render import render_edit
        rendered, built = render_edit(title, body, buttons)
        await helper.edit_message(inline_chat_id, inline_msg_id, rendered, buttons=built)
    client = inline_engine.get_self_client()
    if client is not None:
        await client.delete_messages(chat_id, [msg_id])


# ── Search ──


async def _search_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    title, body, buttons = await _render_browser(inline_engine.get_owner_id(), 1, (text or "").strip())
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        from backend.helper.panel_render import render_edit
        rendered, built = render_edit(title, body, buttons)
        await helper.edit_message(inline_chat_id, inline_msg_id, rendered, buttons=built)
    client = inline_engine.get_self_client()
    if client is not None:
        await client.delete_messages(chat_id, [msg_id])


# ── Registration ──


def register(client, owner_id: int, tz_str: str = "UTC") -> None:
    # Preload the privacy allow-list in the background so the first Browser
    # open never performs a DB read on the render path.
    try:
        from backend.runtime.task_guard import guarded_create_task
        guarded_create_task(_ensure_allowed_loaded_async(), name="ghost_seen_v2:preload_allowed")
    except Exception:
        pass
    register_panel(_PANEL_ID, lambda event, extra: _browser_panel_handler(event, extra, owner_id), parent="menu", title="👻 Ghost Seen")
    register_panel(_VIEWER_ID, lambda event, extra: _viewer_panel_handler(event, extra, owner_id), parent=_PANEL_ID, title="👀 Ghost Seen")
    register_panel(_ACTION_ID, lambda event, extra: _actions_panel_handler(event, extra, owner_id), parent=_VIEWER_ID, title="👀 Ghost Seen")
    register_panel(_MANAGE_ID, lambda event, extra: _manage_panel_handler(event, extra, owner_id), parent=_PANEL_ID, title="⚙ Permissions")
    register_inline_builder(_PANEL_ID, _browser_inline_builder)
    register_inline_builder(_MANAGE_ID, _manage_inline_builder)
    register_action("ghost_seen_v2_page", _page_action)
    register_action("ghost_seen_v2_retry", _retry_action)
    register_action("ghost_seen_v2_open", _open_chat_action)
    register_action("ghost_seen_v2_viewer_page", _viewer_page_action)
    register_action("ghost_seen_v2_select", _select_action)
    register_action("ghost_seen_v2_clear", _clear_action)
    register_action("ghost_seen_v2_actions", _actions_action)
    register_action("ghost_seen_v2_placeholder", _placeholder_action)
    register_action("ghost_seen_v2_ai_reply", _ai_reply_action)
    register_action("ghost_seen_v2_ai_generate", _ai_generate_action)
    register_action("ghost_seen_v2_ai_disclosure", _ai_disclosure_action)
    register_action("ghost_seen_v2_reply", _reply_action)
    register_action("ghost_seen_v2_send_plain", _send_plain_action)
    register_action("ghost_seen_v2_reply_cancel", _reply_cancel_action)
    register_action("ghost_seen_v2_toggle", _toggle_permission_action)
    register_action("ghost_seen_v2_manage_page", _manage_page_action)
    register_action("ghost_seen_v2_actions_back", _actions_back)
    register_input(_PANEL_ID, _SEARCH_INPUT_ID, {"handler": _search_input_handler, "prompt": "**Search private chats**\n\nSearch by first name, last name, or username:\n\n_Reply below._"})
    register_input(_MANAGE_ID, _MANAGE_SEARCH_ID, {"handler": _manage_search_input_handler, "prompt": "**Search private chats**\n\nSearch by first name, last name, or username:\n\n_Reply below._"})


# ── Panel handlers (registered, called by panels.py) ──


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
