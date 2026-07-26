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
Close immediately deletes the inline message (never edits it).
"""
import logging
from typing import Awaitable, Callable, Any

from telethon import events
from telethon.tl.custom import Button

from backend.bot.handlers.guard import is_owner
from backend.helper.context import truncate_callback_data
from backend.helper.input_state import set_pending
from backend.helper.panel_timer import reset_timer, delete_panel

logger = logging.getLogger(__name__)

PanelHandler = Callable[[events.CallbackQuery.Event, str], Awaitable[None]]
ActionHandler = Callable[[events.CallbackQuery.Event, str], Awaitable[tuple]]
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

        from backend.helper.client import get_client
        helper = get_client()

        chat_id = event.chat_id
        msg_id = event.message_id or 0

        if helper and chat_id and msg_id:
            reset_timer(helper, chat_id, msg_id)

        try:
            await event.answer()
        except Exception:
            pass

        data = event.data.decode("utf-8") if event.data else ""
        if not data:
            return

        try:
            if data == "panel:help:close":
                if helper and chat_id and msg_id:
                    await delete_panel(helper, chat_id, msg_id)
                return

            if data.startswith("panel:"):
                await _handle_panel(event, data[6:])
            elif data.startswith("action:"):
                await _handle_action(event, data[7:])
            elif data.startswith("input:"):
                await _handle_input(event, data[6:], owner_id)
        except Exception:
            logger.exception("Callback router error (data='%s')", data)


async def _handle_panel(event, remainder: str) -> None:
    parts = remainder.split(":", 1)
    panel_id = parts[0]
    extra = parts[1] if len(parts) > 1 else ""

    handler = get_panel(panel_id)
    if handler is None:
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
                await event.edit(text, buttons=buttons)
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
        await event.edit(prompt, buttons=builder.build())
    except Exception as exc:
        logger.warning("Input prompt edit failed: %s", exc)
