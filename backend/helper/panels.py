"""
Inline panel system — clean minimal UX with full callback tracing.

Design:
  - Every panel has ONE footer: [ ← Back ] [ ✕ Close ]
  - No per-panel auto-close buttons. Auto-close is global (panel_settings).
  - Navigation stack: Back returns to previous panel, never rebuilds.
  - Timer edits the same message every 30s (no second message).
  - Handlers return (title, body, buttons) WITHOUT footer — the router
    appends the footer automatically.
  - Every callback step is traced and forwarded to Saved Messages.

Callback data format:
  - Panel navigation:  panel:<panel_id>:<extra>
  - Action execution:  action:<action_id>:<extra>
  - Input request:    input:<panel_id>:<input_id>
  - Back navigation:  nav:back
  - Close:            nav:close
"""
import logging
from datetime import datetime, timezone
from typing import Awaitable, Callable, Any

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.helper.context import truncate_callback_data
from backend.helper.input_state import set_pending, clear_pending
from backend.helper.panel_timer import (
    init_panel,
    set_content as timer_set_content,
    destroy as timer_destroy,
    stop_timer,
    has_timer,
)
from backend.helper.panel_render import to_edit_buttons
from backend.helper.target_context import clear_target
from backend.helper.inline_engine import get_self_client
from backend.helper.callback_trace import (
    next_trace_id,
    start_trace,
    step,
    fail,
    log_exception,
    finish_trace,
)

logger = logging.getLogger(__name__)

PanelHandler = Callable[[events.CallbackQuery.Event, str], Awaitable[tuple[str, str, list] | None]]
ActionHandler = Callable[[events.CallbackQuery.Event, str], Awaitable[tuple[str, str, list] | None]]
InputConfig = dict[str, Any]

_panels: dict[str, PanelHandler] = {}
_actions: dict[str, ActionHandler] = {}
_inputs: dict[str, dict[str, InputConfig]] = {}

# Navigation stack: chat_id:msg_id -> list of (panel_id, extra) for Back
_nav_stack: dict[str, list[tuple[str, str]]] = {}

# ── Session tracing ──
# Every panel creation gets a unique session ID. Every callback logs
# session_id, owner_id, chat_id, msg_id, callback_data, nav_stack,
# id(nav_stack), id(_nav_stack), and all dict keys. Every mutation
# logs before AND after state. This proves exactly where nav_stack
# transitions from non-empty to empty.
_session_counter: int = 0
_sessions: dict[str, dict] = {}  # nav_key -> {session_id, chat_id, msg_id, created_at}


def _nav_key(chat_id: int, msg_id: int) -> str:
    return f"{chat_id}:{msg_id}"


def _get_or_create_session(chat_id: int, msg_id: int) -> str:
    """Get or create a panel session ID for this chat_id:msg_id key."""
    global _session_counter
    k = _nav_key(chat_id, msg_id)
    if k not in _sessions:
        _session_counter += 1
        sid = f"PANEL-SESSION-{_session_counter:06d}"
        _sessions[k] = {
            "session_id": sid,
            "chat_id": chat_id,
            "msg_id": msg_id,
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        logger.info(
            "[PANEL] SESSION CREATED session=%s key=%s chat_id=%s msg_id=%s "
            "nav_stack_dict_id=0x%x nav_stack_keys=%s",
            sid, k, chat_id, msg_id,
            id(_nav_stack), list(_nav_stack.keys()),
        )
    return _sessions[k]["session_id"]


def _log_session(session_id: str, chat_id: int, msg_id: int,
                 callback_data: str, label: str) -> None:
    """Log full session state at a callback boundary."""
    k = _nav_key(chat_id, msg_id)
    stack = _nav_stack.get(k)
    stack_copy = list(stack) if stack else []
    logger.info(
        "[PANEL] %s session=%s chat=%s msg=%s "
        "callback_data='%s' key=%s stack=%s "
        "stack_obj=0x%x nav_stack_dict_id=0x%x "
        "nav_stack_keys=%s",
        label, session_id, chat_id, msg_id,
        callback_data, k, stack_copy,
        id(stack) if stack else 0, id(_nav_stack),
        list(_nav_stack.keys()),
    )


def _push_nav(chat_id: int, msg_id: int, panel_id: str, extra: str) -> None:
    k = _nav_key(chat_id, msg_id)
    before = list(_nav_stack.get(k, []))
    if k not in _nav_stack:
        _nav_stack[k] = []
    _nav_stack[k].append((panel_id, extra))
    after = list(_nav_stack[k])
    logger.info(
        "[PANEL] PUSH key=%s before=%s after=%s pushed=(%s,%s) "
        "stack_obj=0x%x dict_id=0x%x dict_keys=%s",
        k, before, after, panel_id, extra,
        id(_nav_stack[k]), id(_nav_stack),
        list(_nav_stack.keys()),
    )


def _pop_nav(chat_id: int, msg_id: int) -> tuple[str, str] | None:
    k = _nav_key(chat_id, msg_id)
    before = list(_nav_stack.get(k, []))
    stack = _nav_stack.get(k)
    if not stack:
        logger.info(
            "[PANEL] POP key=%s before=%s after=[] popped=None "
            "stack_obj=0x0 dict_id=0x%x dict_keys=%s",
            k, before, id(_nav_stack),
            list(_nav_stack.keys()),
        )
        return None
    result = stack.pop()
    after = list(_nav_stack.get(k, []))
    logger.info(
        "[PANEL] POP key=%s before=%s after=%s popped=%s "
        "stack_obj=0x%x dict_id=0x%x dict_keys=%s",
        k, before, after, result,
        id(stack), id(_nav_stack),
        list(_nav_stack.keys()),
    )
    return result


def _clear_nav(chat_id: int, msg_id: int) -> None:
    k = _nav_key(chat_id, msg_id)
    before = list(_nav_stack.get(k, []))
    _nav_stack.pop(k, None)
    logger.info(
        "[PANEL] CLEAR key=%s before=%s after=REMOVED "
        "dict_id=0x%x dict_keys=%s",
        k, before, id(_nav_stack),
        list(_nav_stack.keys()),
    )


def _get_nav_stack(chat_id: int, msg_id: int) -> list[tuple[str, str]]:
    k = _nav_key(chat_id, msg_id)
    stack = _nav_stack.get(k)
    result = list(stack) if stack else []
    logger.info(
        "[PANEL] LOOKUP key=%s found=%s result=%s "
        "stack_obj=0x%x dict_id=0x%x dict_keys=%s",
        k, stack is not None, result,
        id(stack) if stack else 0, id(_nav_stack),
        list(_nav_stack.keys()),
    )
    return result


def _append_footer(buttons: list) -> list:
    """Append the single footer row: [ ← Back ] [ ✕ Close ]"""
    result = list(buttons) if buttons else []
    result.append([("← Back", "nav:back"), ("✕ Close", "nav:close")])
    return result


def _build_message(title: str, body: str, buttons: list) -> tuple[str, list]:
    """Build the full message text and button layout with footer."""
    if title and body:
        text = f"**{title}**\n\n{body}"
    elif title:
        text = f"**{title}**"
    else:
        text = body or ""
    footer_buttons = _append_footer(buttons)
    return text, to_edit_buttons(footer_buttons)


async def _apply_result(event, chat_id: int, msg_id: int, title: str, body: str, buttons: list, trace_id: str) -> None:
    """Edit the inline message with the panel content + footer."""
    step(trace_id, 6, f"Panel state: title='{title}', body_len={len(body)}, buttons={len(buttons) if buttons else 0}")
    timer_set_content(chat_id, msg_id, title, body, buttons)
    text, btns = _build_message(title, body, buttons)
    step(trace_id, 8, f"Edit call: client=helper(event), chat_id={chat_id}, msg_id={msg_id}")
    step(trace_id, 9, f"RPC: event.edit(text_len={len(text)}, buttons_rows={len(btns) if btns else 0})")
    try:
        await event.edit(text, buttons=btns)
        step(trace_id, 10, "RPC response: success")
    except Exception as exc:
        log_exception(trace_id, exc)
        raise


def register_callback_handlers(client, owner_id: int) -> None:
    """Wire the callback query router onto the helper bot client."""

    @client.on(events.CallbackQuery())
    async def _callback_router(event):
        trace_id = next_trace_id()
        start_trace(trace_id)

        # Step 1: callback received
        chat_id = event.chat_id
        msg_id = event.message_id or 0
        sender_id = event.sender_id
        data = event.data.decode("utf-8") if event.data else ""
        step(trace_id, 1, f"callback received: data='{data}', chat_id={chat_id}, msg_id={msg_id}, sender_id={sender_id}")

        # Session trace — log full state at callback entry
        session_id = _get_or_create_session(chat_id, msg_id)
        _log_session(session_id, chat_id, msg_id, data, "CALLBACK ENTRY")

        # Step 2: router entered
        step(trace_id, 2, f"router entered: _callback_router (helper bot client)")

        # Step 4: permission check
        is_own = is_owner(event, owner_id)
        if not is_own:
            step(trace_id, 4, f"permission: REJECTED (sender_id={sender_id} != owner_id={owner_id})", ok=False)
            finish_trace(trace_id)
            return
        step(trace_id, 4, f"permission: ACCEPTED (sender_id={sender_id} == owner_id={owner_id})")

        self_client = get_self_client()
        if self_client is None:
            self_client = client
        step(trace_id, 2, f"handler selected: self_client={'set' if self_client else 'fallback to helper'}")

        try:
            await event.answer()
        except Exception:
            pass

        if not data:
            step(trace_id, 2, "no callback data — returning", ok=False)
            finish_trace(trace_id)
            return

        # Step 5: panel state
        nav_before = _get_nav_stack(chat_id, msg_id)
        step(trace_id, 5, f"panel state: nav_stack_before={nav_before}")
        _log_session(session_id, chat_id, msg_id, data, "BEFORE DISPATCH")

        try:
            if data == "nav:close":
                step(trace_id, 3, "Close handler entered")
                _log_session(session_id, chat_id, msg_id, data, "CLOSE ENTER")
                await _handle_close(self_client, chat_id, msg_id, owner_id, trace_id)
                _log_session(session_id, chat_id, msg_id, data, "CLOSE EXIT")
                finish_trace(trace_id)
                return

            if data == "nav:back":
                step(trace_id, 3, "Back handler entered")
                _log_session(session_id, chat_id, msg_id, data, "BACK ENTER")
                await _handle_back(event, self_client, chat_id, msg_id, trace_id)
                _log_session(session_id, chat_id, msg_id, data, "BACK EXIT")
                finish_trace(trace_id)
                return

            if data.endswith(":noop"):
                step(trace_id, 2, "noop callback — returning")
                finish_trace(trace_id)
                return

            if data.startswith("panel:"):
                step(trace_id, 2, f"handler selected: _handle_panel (panel navigation)")
                _log_session(session_id, chat_id, msg_id, data, "PANEL ENTER")
                await _handle_panel(event, chat_id, msg_id, data[6:], trace_id)
                _log_session(session_id, chat_id, msg_id, data, "PANEL EXIT")
            elif data.startswith("action:"):
                step(trace_id, 2, f"handler selected: _handle_action (action execution)")
                _log_session(session_id, chat_id, msg_id, data, "ACTION ENTER")
                await _handle_action(event, chat_id, msg_id, data[7:], trace_id)
                _log_session(session_id, chat_id, msg_id, data, "ACTION EXIT")
            elif data.startswith("input:"):
                step(trace_id, 2, f"handler selected: _handle_input (input request)")
                _log_session(session_id, chat_id, msg_id, data, "INPUT ENTER")
                await _handle_input(event, chat_id, msg_id, data[6:], owner_id, trace_id)
                _log_session(session_id, chat_id, msg_id, data, "INPUT EXIT")
            else:
                step(trace_id, 2, f"unknown callback data: {data}", ok=False)
                logger.warning("Unknown callback data: %s", data)

            # Step 11: handler finished
            nav_after = _get_nav_stack(chat_id, msg_id)
            step(trace_id, 11, f"handler finished: success, nav_stack_after={nav_after}")
            _log_session(session_id, chat_id, msg_id, data, "AFTER DISPATCH")
        except Exception as exc:
            log_exception(trace_id, exc)
            logger.exception("Callback router error (data='%s', trace=%s)", data, trace_id)
        finally:
            finish_trace(trace_id)


async def _handle_close(self_client, chat_id: int, msg_id: int, owner_id: int, trace_id: str) -> None:
    """Close: delete panel, cancel timer, clear all state."""
    step(trace_id, 5, f"Close: clearing state for chat_id={chat_id}, msg_id={msg_id}")

    # Step 6: TargetContext
    from backend.helper.target_context import get_target
    ctx = get_target(owner_id)
    step(trace_id, 6, f"TargetContext: {'loaded' if ctx else 'missing (none set)'}")
    clear_pending(owner_id)
    clear_target(owner_id)
    _clear_nav(chat_id, msg_id)

    # Step 7: Timer state
    timer_was_active = has_timer(chat_id, msg_id)
    step(trace_id, 7, f"Timer: active={timer_was_active}, cancelling")
    timer_destroy(self_client, chat_id, msg_id)

    # Step 8-10: delete RPC
    step(trace_id, 8, f"Delete call: client=self_client, chat_id={chat_id}, msg_id={msg_id}")
    step(trace_id, 9, f"RPC: self_client.delete_messages(chat_id={chat_id}, ids=[{msg_id}])")
    try:
        await self_client.delete_messages(chat_id, [msg_id])
        step(trace_id, 10, "RPC response: delete success")
    except Exception as exc:
        log_exception(trace_id, exc)
        step(trace_id, 10, f"RPC response: exception type={type(exc).__name__}, text={exc}", ok=False)


async def _handle_back(event, self_client, chat_id: int, msg_id: int, trace_id: str) -> None:
    """Back: pop navigation stack and re-render previous panel."""
    nav_before = _get_nav_stack(chat_id, msg_id)
    step(trace_id, 5, f"Back: navigation stack BEFORE = {nav_before}")

    prev = _pop_nav(chat_id, msg_id)
    nav_after_pop = _get_nav_stack(chat_id, msg_id)
    step(trace_id, 5, f"Back: popped={prev}, stack AFTER pop = {nav_after_pop}")

    if prev is None:
        step(trace_id, 5, "Back: stack empty — falling through to close", ok=False)
        await _handle_close(self_client, chat_id, msg_id, 0, trace_id)
        return

    panel_id, extra = prev
    handler = get_panel(panel_id)
    if handler is None:
        step(trace_id, 5, f"Back: no handler for panel_id='{panel_id}' — closing", ok=False)
        await _handle_close(self_client, chat_id, msg_id, 0, trace_id)
        return

    step(trace_id, 2, f"Back: re-rendering panel='{panel_id}', extra='{extra}'")
    try:
        result = await handler(event, extra)
        if result is None:
            step(trace_id, 11, "Back: handler returned None — nothing to render")
            return
        title, body, buttons = result if isinstance(result, tuple) else (result, "", [])
        step(trace_id, 5, f"Back: panel rendered: title='{title}'")
        await _apply_result(event, chat_id, msg_id, title, body, buttons, trace_id)
        step(trace_id, 11, "Back: message edited successfully")
    except Exception as exc:
        log_exception(trace_id, exc)
        raise


async def _handle_panel(event, chat_id: int, msg_id: int, remainder: str, trace_id: str) -> None:
    parts = remainder.split(":", 1)
    panel_id = parts[0]
    extra = parts[1] if len(parts) > 1 else ""

    step(trace_id, 2, f"Panel: parsing remainder='{remainder}' -> panel_id='{panel_id}', extra='{extra}'")

    handler = get_panel(panel_id)
    if handler is None:
        step(trace_id, 2, f"Panel: no handler for panel_id='{panel_id}'", ok=False)
        return

    # Push the CURRENT panel (source) onto the stack so Back returns to it.
    # This must happen for ALL panels including "help" — the help panel IS
    # the main menu and must be on the stack so sub-categories can go back to it.
    _push_nav(chat_id, msg_id, panel_id, extra)
    step(trace_id, 5, f"Panel: pushed (panel_id='{panel_id}', extra='{extra}'), stack={_get_nav_stack(chat_id, msg_id)}")

    try:
        result = await handler(event, extra)
        if result is None:
            step(trace_id, 11, "Panel: handler returned None — nothing to render")
            return
        title, body, buttons = result if isinstance(result, tuple) else (result, "", [])
        await _apply_result(event, chat_id, msg_id, title, body, buttons, trace_id)
        step(trace_id, 11, f"Panel: handler finished: success, title='{title}'")
    except Exception as exc:
        log_exception(trace_id, exc)
        raise


async def _handle_action(event, chat_id: int, msg_id: int, remainder: str, trace_id: str) -> None:
    parts = remainder.split(":", 1)
    action_id = parts[0]
    extra = parts[1] if len(parts) > 1 else ""

    step(trace_id, 2, f"Action: parsing remainder='{remainder}' -> action_id='{action_id}', extra='{extra}'")

    handler = get_action(action_id)
    if handler is None:
        step(trace_id, 2, f"Action: no handler for action_id='{action_id}'", ok=False)
        return

    try:
        result = await handler(event, extra)
        if result is None:
            step(trace_id, 11, "Action: handler returned None — nothing to render")
            return
        title, body, buttons = result if isinstance(result, tuple) else (result, "", [])
        await _apply_result(event, chat_id, msg_id, title, body, buttons, trace_id)
        step(trace_id, 11, f"Action: handler finished: success, title='{title}'")
    except Exception as exc:
        log_exception(trace_id, exc)
        raise


async def _handle_input(event, chat_id: int, msg_id: int, remainder: str, owner_id: int, trace_id: str) -> None:
    parts = remainder.split(":", 1)
    panel_id = parts[0]
    input_id = parts[1] if len(parts) > 1 else ""

    step(trace_id, 2, f"Input: parsing remainder='{remainder}' -> panel_id='{panel_id}', input_id='{input_id}'")

    input_cfg = get_input(panel_id, input_id)
    if input_cfg is None:
        step(trace_id, 2, f"Input: no config for panel='{panel_id}' input='{input_id}'", ok=False)
        return

    prompt = input_cfg.get("prompt", "Enter input:")
    handler = input_cfg.get("handler")
    if handler is None:
        step(trace_id, 2, "Input: no handler in config", ok=False)
        return

    _push_nav(chat_id, msg_id, panel_id, "")
    step(trace_id, 5, f"Input: pushed nav (panel_id='{panel_id}'), stack={_get_nav_stack(chat_id, msg_id)}")

    set_pending(
        owner_id, panel_id, handler, chat_id, prompt,
        inline_chat_id=chat_id, inline_msg_id=msg_id,
    )

    text, btns = _build_message(panel_id.title(), prompt, [])
    step(trace_id, 8, f"Input: edit call, chat_id={chat_id}, msg_id={msg_id}")
    step(trace_id, 9, f"RPC: event.edit(text_len={len(text)}, buttons_rows={len(btns) if btns else 0})")
    try:
        await event.edit(text, buttons=btns)
        step(trace_id, 10, "RPC response: input prompt edit success")
    except Exception as exc:
        log_exception(trace_id, exc)
        raise
