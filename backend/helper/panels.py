"""
Inline panel system for the helper bot.

Provides:
  - ``InlinePanelBuilder`` — builds inline keyboards with rows of buttons.
  - ``register_panel(panel_id, handler)`` — registers a callback handler
    for a panel ID.
  - ``get_panel(panel_id)`` — retrieves a registered panel handler.
  - ``register_action(action_id, handler)`` — registers an action handler
    for immediate execution (Type A commands).
  - ``register_input(panel_id, input_id, handler, prompt)`` — registers
    an input handler for Type B commands requiring user text input.
  - ``register_callback_handlers(client, owner_id)`` — wires the callback
    query router onto the helper bot client.

Callback data format:
  - Panel navigation:  ``panel:<panel_id>:<extra>``
  - Action execution:  ``action:<action_id>:<extra>``
  - Input request:    ``input:<panel_id>:<input_id>``
  - Close:            ``panel:_nav:close``
  - Home:             ``panel:help``
  - Back:             ``panel:_nav:back``

The callback router dispatches based on the prefix. Panel handlers
edit the inline message in-place. Action handlers execute logic and
then edit the message with the result. Input handlers set a pending
input state and edit the message to show a prompt.

Every panel render automatically appends navigation buttons:
  - Back (returns to the panel that opened this one, or Home if none)
  - Home (returns to the main menu)
  - Close (terminates the panel session)

SESSION LIFECYCLE:
  Create → Render → Wait → Action/Input → Update → Back/Home/Close → Destroy
  Nothing survives after Destroy. No leaked session, timer, callback, or input.
"""
import asyncio
import logging
from typing import Awaitable, Callable, Any

from telethon import events
from telethon.tl.custom import Button

from backend.bot.handlers.guard import is_owner
from backend.helper.context import truncate_callback_data
from backend.helper.input_state import set_pending, clear_pending, get_pending
from backend.helper.panel_timer import set_content, stop_timer, destroy as timer_destroy
from backend.helper.session_manager import (
    create_session as _create_session,
    get_session as _get_session,
    push_nav as _push_nav,
    pop_nav as _pop_nav,
    current_nav as _current_nav,
    clear_session,
    clear_all_sessions,
)

logger = logging.getLogger(__name__)

_last_render: dict[tuple[int | None, int | None], tuple[str, tuple]] = {}


def _buttons_repr(buttons: list) -> tuple[tuple[str, ...], ...]:
    """Create a comparable representation of button callback data."""
    result = []
    for row in buttons:
        row_data = []
        if isinstance(row, list):
            for btn in row:
                data = getattr(btn, "data", None)
                if isinstance(data, bytes):
                    row_data.append(data.decode("utf-8", errors="replace"))
                elif isinstance(data, str):
                    row_data.append(data)
                else:
                    row_data.append(str(data))
        result.append(tuple(row_data))
    return tuple(result)


async def _safe_edit(event, text: str, buttons: list, chat_id: int | None, msg_id: int | None) -> bool:
    """Edit the inline message, skipping silently if content hasn't changed."""
    key = (chat_id, msg_id)
    new_repr = (text, _buttons_repr(buttons))
    last = _last_render.get(key)
    if last == new_repr:
        return False
    try:
        await event.edit(text, buttons=buttons if buttons else [])
        _last_render[key] = new_repr
        return True
    except Exception as exc:
        msg_lower = str(exc).lower()
        if "not modified" in msg_lower:
            _last_render[key] = new_repr
            return False
        logger.warning("[CALLBACK] edit failed: %s", exc)
        return False


def resolve_callback_message(event) -> tuple[int | None, int | None, str | None]:
    """Safely resolve (chat_id, msg_id, inline_message_id) from any callback event."""
    chat_id = None
    msg_id = None
    inline_message_id = None

    try:
        chat_id = getattr(event, "chat_id", None)
    except Exception:
        pass

    try:
        msg_id = getattr(event, "message_id", None)
    except Exception:
        pass

    try:
        inline_message_id = getattr(event, "inline_message_id", None)
    except Exception:
        pass

    if msg_id is None and inline_message_id is None:
        try:
            orig = getattr(event, "original_update", None)
            if orig is not None:
                msg_id = getattr(orig, "msg_id", None)
                if msg_id is None:
                    raw_msg_id = getattr(orig, "msg_id", None)
                    if hasattr(raw_msg_id, "id"):
                        msg_id = raw_msg_id.id
        except Exception:
            pass

    return chat_id, msg_id, inline_message_id


PanelHandler = Callable[[events.CallbackQuery.Event, str], Awaitable[None]]
ActionHandler = Callable[[events.CallbackQuery.Event, str], Awaitable[str]]
InputConfig = dict[str, Any]

_panels: dict[str, PanelHandler] = {}
_actions: dict[str, ActionHandler] = {}
_inputs: dict[str, dict[str, InputConfig]] = {}


class InlinePanelBuilder:
    """Builds inline keyboard layouts for the helper bot."""

    def __init__(self):
        self._rows: list[list[Any]] = []

    def add_row(self, text: str, callback_data: str) -> "InlinePanelBuilder":
        self._rows.append([Button.inline(text, truncate_callback_data(callback_data))])
        return self

    def add_buttons(self, *buttons: tuple[str, str]) -> "InlinePanelBuilder":
        row = [Button.inline(text, truncate_callback_data(data)) for text, data in buttons]
        self._rows.append(row)
        return self

    def add_url(self, text: str, url: str) -> "InlinePanelBuilder":
        self._rows.append([Button.url(text, url)])
        return self

    def build(self) -> list[list[Any]]:
        return [list(row) for row in self._rows]


def _add_nav_buttons(builder: InlinePanelBuilder, panel_id: str) -> None:
    """Append Back, Home, and Close buttons to every panel."""
    builder.add_buttons(
        ("‹ Back", "panel:_nav:back"),
        ("🏠 Home", "panel:help"),
        ("✕ Close", "panel:_nav:close"),
    )


def _has_nav_buttons(buttons: list) -> bool:
    """Check if the button list already contains navigation buttons."""
    for row in buttons:
        if isinstance(row, list):
            for btn in row:
                data = getattr(btn, "data", None)
                if data is None:
                    continue
                if isinstance(data, bytes):
                    if b"panel:_nav:" in data:
                        return True
                elif isinstance(data, str):
                    if "panel:_nav:" in data:
                        return True
    return False


def _finalize_panel(title: str, body: str, buttons: list | None, panel_id: str) -> tuple[str, str, list]:
    """Ensure every panel has navigation buttons. Always builds a fresh keyboard — never mutates the input list."""
    if buttons is None:
        buttons = []
    if _has_nav_buttons(buttons):
        return title, body, [list(row) if isinstance(row, list) else [row] for row in buttons]
    builder = InlinePanelBuilder()
    for row in buttons:
        if isinstance(row, list):
            builder._rows.append(list(row))
        else:
            builder._rows.append([row])
    _add_nav_buttons(builder, panel_id)
    return title, body, builder.build()


def register_panel(panel_id: str, handler: PanelHandler) -> None:
    _panels[panel_id] = handler
    logger.info("[PANEL] Registered: id='%s' (total=%d)", panel_id, len(_panels))


def get_panel(panel_id: str) -> PanelHandler | None:
    return _panels.get(panel_id)


def register_action(action_id: str, handler: ActionHandler) -> None:
    _actions[action_id] = handler
    logger.info("[ACTION] Registered: id='%s' (total=%d)", action_id, len(_actions))


def get_action(action_id: str) -> ActionHandler | None:
    return _actions.get(action_id)


def register_input(panel_id: str, input_id: str, handler: InputConfig) -> None:
    if panel_id not in _inputs:
        _inputs[panel_id] = {}
    _inputs[panel_id][input_id] = handler
    logger.info("[INPUT] Registered: panel='%s', input_id='%s'", panel_id, input_id)


def get_input(panel_id: str, input_id: str) -> InputConfig | None:
    return _inputs.get(panel_id, {}).get(input_id)


async def _safe_answer(event) -> None:
    """Answer the callback query to remove the loading spinner."""
    try:
        await event.answer()
    except Exception:
        pass


def _sync_timer(chat_id: int, msg_id: int, title: str, body: str, buttons: list) -> None:
    """Sync panel content to the timer so countdown re-renders show current state."""
    try:
        set_content(chat_id, msg_id, title, body, buttons)
    except Exception:
        pass


def _extract_render_result(result) -> tuple[str, str, list]:
    """Normalize a handler return value into (title, body, buttons)."""
    if result is None:
        return "", "", []
    if isinstance(result, tuple):
        if len(result) == 3:
            return result[0], result[1], result[2]
        elif len(result) == 2:
            return result[0], result[1], []
        else:
            return (result[0] if result else ""), (result[1] if len(result) > 1 else ""), (result[2] if len(result) > 2 else [])
    return str(result), "", []


async def _render_and_edit(event, result, panel_id: str, chat_id: int | None, msg_id: int | None) -> None:
    """Finalize panel buttons, sync timer, and edit the message."""
    title, body, buttons = _extract_render_result(result)
    if not title and not body and not buttons:
        return
    title, body, buttons = _finalize_panel(title, body, buttons, panel_id)
    from backend.helper.panel_render import render_edit
    text, built_buttons = render_edit(title, body, buttons)
    _sync_timer(chat_id or 0, msg_id or 0, title, body, buttons)
    await _safe_edit(event, text, built_buttons, chat_id, msg_id)


def register_callback_handlers(client, owner_id: int) -> None:
    """Wire the callback query router onto the helper bot client."""
    logger.info("[PANEL] callback handler registered: owner_id=%s", owner_id)

    @client.on(events.CallbackQuery())
    async def _callback_router(event):
        chat_id, msg_id, inline_msg_id = resolve_callback_message(event)
        data_raw = event.data
        data = data_raw.decode("utf-8") if data_raw else ""

        if chat_id is None and inline_msg_id is None:
            logger.warning("[PANEL] callback unresolvable: no chat_id and no inline_message_id")
            return

        if not is_owner(event, owner_id):
            await _safe_answer(event)
            return

        if not data:
            await _safe_answer(event)
            return

        if _get_session(chat_id, msg_id) is None:
            await _safe_answer(event)
            return

        try:
            if data.startswith("panel:"):
                await _handle_panel(event, data[6:], chat_id, msg_id, owner_id)
            elif data.startswith("action:"):
                await _handle_action(event, data[7:], chat_id, msg_id, owner_id)
            elif data.startswith("input:"):
                await _handle_input(event, data[6:], owner_id, chat_id, msg_id)
            else:
                logger.warning("[PANEL] unknown callback prefix: '%s'", data)
        except Exception:
            logger.exception("[PANEL] callback router error (data='%s')", data)
        finally:
            await _safe_answer(event)


async def _handle_panel(event, remainder: str, chat_id: int, msg_id: int, owner_id: int) -> None:
    parts = remainder.split(":", 1)
    panel_id = parts[0]
    extra = parts[1] if len(parts) > 1 else ""

    if panel_id == "_nav":
        await _handle_navigation(event, extra, chat_id, msg_id, owner_id)
        return

    handler = get_panel(panel_id)
    if handler is None:
        logger.warning("[CALLBACK] no panel registered for id='%s'", panel_id)
        return

    if extra != "back":
        _push_nav(chat_id, msg_id, panel_id)

    clear_pending(owner_id)

    try:
        result = await handler(event, extra)
        await _render_and_edit(event, result, panel_id, chat_id, msg_id)
    except Exception:
        logger.exception("[CALLBACK] panel handler '%s' FAILED", panel_id)


async def _handle_navigation(event, action: str, chat_id: int, msg_id: int, owner_id: int) -> None:
    """Handle Back, Home, and Close navigation buttons."""
    if action == "close":
        clear_pending(owner_id)
        stop_timer(chat_id, msg_id)
        clear_session(chat_id, msg_id)
        _last_render.pop((chat_id, msg_id), None)
        try:
            await event.edit("✕ **Panel closed**", buttons=[])
            _last_render[(chat_id, msg_id)] = ("✕ **Panel closed**", ())
        except Exception:
            pass
        return

    if action == "back":
        prev_panel = _pop_nav(chat_id, msg_id)
        clear_pending(owner_id)
        if prev_panel is None:
            prev_panel = "help"
        handler = get_panel(prev_panel)
        if handler is None:
            prev_panel = "help"
            handler = get_panel("help")
        if handler is None:
            return
        _push_nav(chat_id, msg_id, prev_panel)
        try:
            result = await handler(event, "")
            await _render_and_edit(event, result, prev_panel, chat_id, msg_id)
        except Exception:
            logger.exception("[CALLBACK] back navigation FAILED")
        return

    if action == "noop":
        return


async def _handle_action(event, remainder: str, chat_id: int, msg_id: int, owner_id: int) -> None:
    parts = remainder.split(":", 1)
    action_id = parts[0]
    extra = parts[1] if len(parts) > 1 else ""

    handler = get_action(action_id)
    if handler is None:
        logger.warning("[CALLBACK] no action registered for id='%s'", action_id)
        return

    clear_pending(owner_id)

    try:
        result = await handler(event, extra)
        current_panel = _current_nav(chat_id, msg_id) or action_id
        await _render_and_edit(event, result, current_panel, chat_id, msg_id)
    except Exception:
        logger.exception("[CALLBACK] action handler '%s' FAILED", action_id)


async def _handle_input(event, remainder: str, owner_id: int, chat_id: int, msg_id: int) -> None:
    parts = remainder.split(":", 1)
    panel_id = parts[0]
    input_id = parts[1] if len(parts) > 1 else ""

    input_cfg = get_input(panel_id, input_id)
    if input_cfg is None:
        logger.warning("[CALLBACK] no input registered: panel='%s', input_id='%s'", panel_id, input_id)
        return

    prompt = input_cfg.get("prompt", "Enter input:")
    handler = input_cfg.get("handler")

    if handler is None:
        logger.warning("[CALLBACK] input config has no handler")
        return

    clear_pending(owner_id)
    set_pending(
        owner_id, panel_id, handler, chat_id or 0, prompt,
        inline_chat_id=chat_id or 0, inline_msg_id=msg_id or 0,
    )

    builder = InlinePanelBuilder()
    builder.add_row("Cancel", f"panel:{panel_id}")
    _add_nav_buttons(builder, panel_id)

    built = builder.build()
    _sync_timer(chat_id or 0, msg_id or 0, panel_id, prompt, built)
    await _safe_edit(event, prompt, built, chat_id, msg_id)
