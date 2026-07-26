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


class InlinePanelBuilder:
    """Builds inline keyboard layouts. Tuples: ("Text", "callback_data")."""

    def __init__(self):
        self._rows: list[list[tuple[str, str]]] = []

    def add_row(self, text: str, callback_data: str) -> "InlinePanelBuilder":
        self._rows.append([(text, callback_data)])
        return self

    def add_buttons(self, *buttons: tuple[str, str]) -> "InlinePanelBuilder":
        self._rows.append(list(buttons))
        return self

    def add_url(self, text: str, url: str) -> "InlinePanelBuilder":
        self._rows.append(("__url__", text, url))
        return self

    def build(self) -> list[list[tuple[str, str]]]:
        return self._rows


def register_panel(panel_id: str, handler: PanelHandler) -> None:
    _panels[panel_id] = handler


def get_panel(panel_id: str) -> PanelHandler | None:
    return _panels.get(panel_id)


def register_action(action_id: str, handler: ActionHandler) -> None:
    _actions[action_id] = handler


def get_action(action_id: str) -> ActionHandler | None:
    return _actions.get(action_id)


def register_input(panel_id: str, input_id: str, config: InputConfig) -> None:
    if panel_id not in _inputs:
        _inputs[panel_id] = {}
    _inputs[panel_id][input_id] = config


def get_input(panel_id: str, input_id: str) -> InputConfig | None:
    return _inputs.get(panel_id, {}).get(input_id)


def _nav_key(chat_id: int, msg_id: int) -> str:
    return f"{chat_id}:{msg_id}"


def _push_nav(chat_id: int, msg_id: int, panel_id: str, extra: str) -> None:
    k = _nav_key(chat_id, msg_id)
    if k not in _nav_stack:
        _nav_stack[k] = []
    _nav_stack[k].append((panel_id, extra))


def _pop_nav(chat_id: int, msg_id: int) -> tuple[str, str] | None:
    k = _nav_key(chat_id, msg_id)
    stack = _nav_stack.get(k)
    if not stack:
        return None
    return stack.pop()


def _clear_nav(chat_id: int, msg_id: int) -> None:
    _nav_stack.pop(_nav_key(chat_id, msg_id), None)


def _get_nav_stack(chat_id: int, msg_id: int) -> list[tuple[str, str]]:
    return list(_nav_stack.get(_nav_key(chat_id, msg_id), []))


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

        try:
            if data == "nav:close":
                step(trace_id, 3, "Close handler entered")
                await _handle_close(self_client, chat_id, msg_id, owner_id, trace_id)
                finish_trace(trace_id)
                return

            if data == "nav:back":
                step(trace_id, 3, "Back handler entered")
                await _handle_back(event, self_client, chat_id, msg_id, trace_id)
                finish_trace(trace_id)
                return

            if data.endswith(":noop"):
                step(trace_id, 2, "noop callback — returning")
                finish_trace(trace_id)
                return

            if data.startswith("panel:"):
                step(trace_id, 2, f"handler selected: _handle_panel (panel navigation)")
                await _handle_panel(event, chat_id, msg_id, data[6:], trace_id)
            elif data.startswith("action:"):
                step(trace_id, 2, f"handler selected: _handle_action (action execution)")
                await _handle_action(event, chat_id, msg_id, data[7:], trace_id)
            elif data.startswith("input:"):
                step(trace_id, 2, f"handler selected: _handle_input (input request)")
                await _handle_input(event, chat_id, msg_id, data[6:], owner_id, trace_id)
            else:
                step(trace_id, 2, f"unknown callback data: {data}", ok=False)
                logger.warning("Unknown callback data: %s", data)

            # Step 11: handler finished
            nav_after = _get_nav_stack(chat_id, msg_id)
            step(trace_id, 11, f"handler finished: success, nav_stack_after={nav_after}")
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
