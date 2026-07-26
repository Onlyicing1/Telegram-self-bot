"""
Inline panel system for the helper bot.

Provides:
  - InlinePanelBuilder — builds inline keyboards with rows of buttons.
  - register_panel(panel_id, handler) — registers a callback handler.
  - register_action(action_id, handler) — registers an action handler (Type A).
  - register_input(panel_id, input_id, handler, prompt) — registers input (Type B).

Callback data format:
  - Panel navigation:  panel:<panel_id>:<extra>
  - Action execution:  action:<action_id>:<extra>
  - Input request:    input:<panel_id>:<input_id>
  - Timer toggle:     timer:toggle

Every panel handler returns (title, body, buttons) and the router
wraps the result with the auto-close toggle and Close button.
Close immediately deletes the inline message, cancels the timer,
and clears TargetContext and input state.
"""
import logging
from typing import Awaitable, Callable, Any

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.helper.context import truncate_callback_data
from backend.helper.input_state import set_pending, clear_pending
from backend.helper.panel_timer import (
    toggle as timer_toggle,
    get_state as timer_get_state,
    get_countdown_text,
    get_toggle_button_text,
    destroy as timer_destroy,
    set_content as timer_set_content,
    TimerState,
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


class InlinePanelBuilder:
    """Builds inline keyboard layouts for the helper bot.

    All methods store tuples: ("Text", "callback_data")
    build() returns list[list[(text, data)]].
    The renderer normalizes tuples to Button objects.
    """

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


def _wrap_buttons(chat_id: int, msg_id: int, content_buttons: list) -> list:
    """Append the toggle and Close buttons to the handler's button rows."""
    result = list(content_buttons) if content_buttons else []
    toggle_text = get_toggle_button_text(chat_id, msg_id)
    result.append([(toggle_text, "timer:toggle")])
    result.append([("Close", "panel:help:close")])
    return result


def _build_message(chat_id: int, msg_id: int, title: str, body: str, buttons: list) -> tuple[str, list]:
    """Wrap a handler result with the countdown header and toggle/close buttons."""
    countdown = get_countdown_text(chat_id, msg_id)
    header = f"**{title}**\n\n{countdown}"
    if body:
        full_text = f"{header}\n\n{body}"
    else:
        full_text = header
    wrapped = _wrap_buttons(chat_id, msg_id, buttons)
    return full_text, to_edit_buttons(wrapped)


async def _apply_result(event, chat_id: int, msg_id: int, title: str, body: str, buttons: list) -> None:
    """Edit the inline message with the wrapped panel content."""
    timer_set_content(chat_id, msg_id, title, body, buttons)
    text, btns = _build_message(chat_id, msg_id, title, body, buttons)
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
            if data == "panel:help:close":
                await _handle_close(self_client, chat_id, msg_id, owner_id)
                return

            if data == "timer:toggle":
                await _handle_timer_toggle(self_client, chat_id, msg_id, event)
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


async def _handle_timer_toggle(self_client, chat_id: int, msg_id: int, event) -> None:
    """Toggle auto-close state and re-render the panel."""
    new_state = timer_toggle(self_client, chat_id, msg_id)
    if new_state == TimerState.PAUSED:
        try:
            await event.edit(
                f"**LifeOS**\n\nAuto Close\nDisabled",
                buttons=to_edit_buttons(_wrap_buttons(chat_id, msg_id, [])),
            )
        except Exception as exc:
            logger.warning("Timer toggle (pause) edit failed: %s", exc)
    else:
        countdown = get_countdown_text(chat_id, msg_id)
        try:
            await event.edit(
                f"**LifeOS**\n\n{countdown}",
                buttons=to_edit_buttons(_wrap_buttons(chat_id, msg_id, [])),
            )
        except Exception as exc:
            logger.warning("Timer toggle (enable) edit failed: %s", exc)


async def _handle_close(self_client, chat_id: int, msg_id: int, owner_id: int) -> None:
    """Close handler: delete panel, cancel timer, clear all state."""
    clear_pending(owner_id)
    clear_target(owner_id)
    timer_destroy(self_client, chat_id, msg_id)


async def _handle_panel(event, chat_id: int, msg_id: int, remainder: str) -> None:
    parts = remainder.split(":", 1)
    panel_id = parts[0]
    extra = parts[1] if len(parts) > 1 else ""

    handler = get_panel(panel_id)
    if handler is None:
        logger.warning("No panel handler for panel_id='%s'", panel_id)
        return

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

    set_pending(
        owner_id, panel_id, handler, chat_id, prompt,
        inline_chat_id=chat_id, inline_msg_id=msg_id,
    )

    builder = InlinePanelBuilder()
    builder.add_row("Cancel", f"panel:{panel_id}")

    try:
        await event.edit(prompt, buttons=to_edit_buttons(builder.build()))
    except Exception as exc:
        logger.warning("Input prompt edit failed: %s", exc)
