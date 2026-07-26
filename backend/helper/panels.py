"""
Inline panel system — clean minimal UX.

Design:
  - Every panel has ONE footer: [ ← Back ] [ ✕ Close ]
  - No per-panel auto-close buttons. Auto-close is global (panel_settings).
  - Navigation stack: Back returns to previous panel, never rebuilds.
  - Timer edits the same message every 30s (no second message).
  - Handlers return (title, body, buttons) WITHOUT footer — the router
    appends the footer automatically.

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


async def _apply_result(event, chat_id: int, msg_id: int, title: str, body: str, buttons: list) -> None:
    """Edit the inline message with the panel content + footer."""
    timer_set_content(chat_id, msg_id, title, body, buttons)
    text, btns = _build_message(title, body, buttons)
    try:
        await event.edit(text, buttons=btns)
    except Exception as exc:
        logger.warning("Panel edit failed: %s", exc)


def register_callback_handlers(client, owner_id: int) -> None:
    """Wire the callback query router onto the helper bot client."""

    @client.on(events.CallbackQuery())
    async def _callback_router(event):
        if not is_owner(event, owner_id):
            return

        self_client = get_self_client()
        if self_client is None:
            self_client = client

        chat_id = event.chat_id
        msg_id = event.message_id or 0

        try:
            await event.answer()
        except Exception:
            pass

        data = event.data.decode("utf-8") if event.data else ""
        if not data:
            return

        try:
            if data == "nav:close":
                await _handle_close(self_client, chat_id, msg_id, owner_id)
                return

            if data == "nav:back":
                await _handle_back(event, self_client, chat_id, msg_id)
                return

            if data.endswith(":noop"):
                return

            if data.startswith("panel:"):
                await _handle_panel(event, chat_id, msg_id, data[6:])
            elif data.startswith("action:"):
                await _handle_action(event, chat_id, msg_id, data[7:])
            elif data.startswith("input:"):
                await _handle_input(event, chat_id, msg_id, data[6:], owner_id)
            else:
                logger.warning("Unknown callback data: %s", data)
        except Exception:
            logger.exception("Callback router error (data='%s')", data)


async def _handle_close(self_client, chat_id: int, msg_id: int, owner_id: int) -> None:
    """Close: delete panel, cancel timer, clear all state."""
    clear_pending(owner_id)
    clear_target(owner_id)
    _clear_nav(chat_id, msg_id)
    timer_destroy(self_client, chat_id, msg_id)


async def _handle_back(event, self_client, chat_id: int, msg_id: int) -> None:
    """Back: pop navigation stack and re-render previous panel."""
    prev = _pop_nav(chat_id, msg_id)
    if prev is None:
        await _handle_close(self_client, chat_id, msg_id, 0)
        return
    panel_id, extra = prev
    handler = get_panel(panel_id)
    if handler is None:
        await _handle_close(self_client, chat_id, msg_id, 0)
        return
    try:
        result = await handler(event, extra)
        if result is None:
            return
        title, body, buttons = result if isinstance(result, tuple) else (result, "", [])
        await _apply_result(event, chat_id, msg_id, title, body, buttons)
    except Exception:
        logger.exception("Back handler '%s' failed", panel_id)


async def _handle_panel(event, chat_id: int, msg_id: int, remainder: str) -> None:
    parts = remainder.split(":", 1)
    panel_id = parts[0]
    extra = parts[1] if len(parts) > 1 else ""

    handler = get_panel(panel_id)
    if handler is None:
        logger.warning("No panel handler for panel_id='%s'", panel_id)
        return

    if panel_id != "help":
        _push_nav(chat_id, msg_id, panel_id, extra)

    try:
        result = await handler(event, extra)
        if result is None:
            return
        title, body, buttons = result if isinstance(result, tuple) else (result, "", [])
        await _apply_result(event, chat_id, msg_id, title, body, buttons)
    except Exception:
        logger.exception("Panel handler '%s' failed", panel_id)


async def _handle_action(event, chat_id: int, msg_id: int, remainder: str) -> None:
    parts = remainder.split(":", 1)
    action_id = parts[0]
    extra = parts[1] if len(parts) > 1 else ""

    handler = get_action(action_id)
    if handler is None:
        logger.warning("No action handler for action_id='%s'", action_id)
        return

    try:
        result = await handler(event, extra)
        if result is None:
            return
        title, body, buttons = result if isinstance(result, tuple) else (result, "", [])
        await _apply_result(event, chat_id, msg_id, title, body, buttons)
    except Exception:
        logger.exception("Action handler '%s' failed", action_id)


async def _handle_input(event, chat_id: int, msg_id: int, remainder: str, owner_id: int) -> None:
    parts = remainder.split(":", 1)
    panel_id = parts[0]
    input_id = parts[1] if len(parts) > 1 else ""

    input_cfg = get_input(panel_id, input_id)
    if input_cfg is None:
        logger.warning("No input config for panel='%s' input='%s'", panel_id, input_id)
        return

    prompt = input_cfg.get("prompt", "Enter input:")
    handler = input_cfg.get("handler")
    if handler is None:
        return

    _push_nav(chat_id, msg_id, panel_id, "")

    set_pending(
        owner_id, panel_id, handler, chat_id, prompt,
        inline_chat_id=chat_id, inline_msg_id=msg_id,
    )

    text, btns = _build_message(panel_id.title(), prompt, [])
    try:
        await event.edit(text, buttons=btns)
    except Exception as exc:
        logger.warning("Input prompt edit failed: %s", exc)
