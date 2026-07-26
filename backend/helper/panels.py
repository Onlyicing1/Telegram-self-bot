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

Every callback resets the panel auto-delete timer.
Close immediately deletes the inline message, cancels the timer,
and clears TargetContext and input state.
"""
import logging
from typing import Awaitable, Callable, Any

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.helper.context import truncate_callback_data
from backend.helper.input_state import set_pending, clear_pending
from backend.helper.panel_timer import reset_timer, delete_panel, stop_timer
from backend.helper.panel_render import to_edit_buttons
from backend.helper.target_context import clear_target
from backend.helper.inline_engine import get_self_client

logger = logging.getLogger(__name__)

PanelHandler = Callable[[events.CallbackQuery.Event, str], Awaitable[None]]
ActionHandler = Callable[[events.CallbackQuery.Event, str], Awaitable[tuple]]
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

        if chat_id and msg_id:
            reset_timer(self_client, chat_id, msg_id)

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

            if data.endswith(":noop"):
                return

            if data.startswith("panel:"):
                await _handle_panel(event, data[6:])
            elif data.startswith("action:"):
                await _handle_action(event, data[7:])
            elif data.startswith("input:"):
                await _handle_input(event, data[6:], owner_id)
            else:
                logger.warning("Unknown callback data: %s", data)
        except Exception:
            logger.exception("Callback router error (data='%s')", data)


async def _handle_close(self_client, chat_id: int, msg_id: int, owner_id: int) -> None:
    """Close handler: delete panel, cancel timer, clear all state."""
    clear_pending(owner_id)
    clear_target(owner_id)
    if chat_id and msg_id:
        await delete_panel(self_client, chat_id, msg_id)


async def _handle_panel(event, remainder: str) -> None:
    parts = remainder.split(":", 1)
    panel_id = parts[0]
    extra = parts[1] if len(parts) > 1 else ""

    handler = get_panel(panel_id)
    if handler is None:
        logger.warning("No panel handler for panel_id='%s'", panel_id)
        return

    try:
        await handler(event, extra)
    except Exception:
        logger.exception("Panel handler '%s' failed", panel_id)


async def _handle_action(event, remainder: str) -> None:
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
        if isinstance(result, tuple):
            text, buttons = result
        else:
            text, buttons = result, []
        if text:
            try:
                await event.edit(text, buttons=to_edit_buttons(buttons))
            except Exception as exc:
                logger.warning("Action result edit failed: %s", exc)
    except Exception:
        logger.exception("Action handler '%s' failed", action_id)


async def _handle_input(event, remainder: str, owner_id: int) -> None:
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

    chat_id = event.chat_id
    inline_msg_id = event.message_id or 0

    set_pending(
        owner_id, panel_id, handler, chat_id, prompt,
        inline_chat_id=chat_id, inline_msg_id=inline_msg_id,
    )

    builder = InlinePanelBuilder()
    builder.add_row("Cancel", f"panel:{panel_id}")

    try:
        await event.edit(prompt, buttons=to_edit_buttons(builder.build()))
    except Exception as exc:
        logger.warning("Input prompt edit failed: %s", exc)
