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

The callback router dispatches based on the prefix. Panel handlers
edit the inline message in-place. Action handlers execute logic and
then edit the message with the result. Input handlers set a pending
input state and edit the message to show a prompt.
"""
import logging
from typing import Awaitable, Callable, Any

from telethon import events
from telethon.tl.custom import Button

from backend.bot.handlers.guard import is_owner
from backend.helper.context import truncate_callback_data
from backend.helper.input_state import set_pending

logger = logging.getLogger(__name__)


def resolve_callback_message(event) -> tuple[int | None, int | None, str | None]:
    """Safely resolve (chat_id, msg_id, inline_message_id) from any callback event.

    Telethon's CallbackQuery.Event does NOT have a ``msg_id`` attribute.
    Depending on the callback source:
      - Bot messages in chats: ``event.message_id`` is set, ``event.inline_message_id`` is None.
      - Inline messages (sent via inline mode): ``event.inline_message_id`` is set,
        ``event.message_id`` may be None.

    This helper never raises — it returns (None, None, None) if nothing can be resolved.
    """
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


_session_counter: int = 0
_sessions: dict[tuple[int, int], dict] = {}


def _create_session(chat_id: int, msg_id: int, panel_type: str = "unknown") -> str:
    global _session_counter
    _session_counter += 1
    sid = f"PANEL-SESSION-{_session_counter:06d}"
    _sessions[(chat_id, msg_id)] = {
        "session_id": sid,
        "chat_id": chat_id,
        "msg_id": msg_id,
        "panel_type": panel_type,
    }
    return sid


def _get_session(chat_id: int | None, msg_id: int | None) -> dict | None:
    if chat_id is None or msg_id is None:
        return None
    return _sessions.get((chat_id, msg_id))


def clear_session(chat_id: int | None, msg_id: int | None) -> None:
    if chat_id is None or msg_id is None:
        return
    _sessions.pop((chat_id, msg_id), None)


def clear_all_sessions() -> None:
    _sessions.clear()


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
    logger.info("[PANEL] Registered: id='%s' (total=%d)", panel_id, len(_panels))


def get_panel(panel_id: str) -> PanelHandler | None:
    return _panels.get(panel_id)


def register_action(action_id: str, handler: ActionHandler) -> None:
    """Register an action handler for Type A (immediate execution) commands.

    The handler receives the callback event and extra data, and returns
    a result string to display in the panel.
    """
    _actions[action_id] = handler
    logger.info("[ACTION] Registered: id='%s' (total=%d)", action_id, len(_actions))


def get_action(action_id: str) -> ActionHandler | None:
    return _actions.get(action_id)


def register_input(panel_id: str, input_id: str, handler: InputConfig) -> None:
    """Register an input handler for Type B (requires user input) commands.

    The ``handler`` dict contains:
      - ``handler``: async callable(text, chat_id, msg_id) -> None
      - ``prompt``: str to display when waiting for input
    """
    if panel_id not in _inputs:
        _inputs[panel_id] = {}
    _inputs[panel_id][input_id] = handler
    logger.info("[INPUT] Registered: panel='%s', input_id='%s'", panel_id, input_id)


def get_input(panel_id: str, input_id: str) -> InputConfig | None:
    return _inputs.get(panel_id, {}).get(input_id)


def register_callback_handlers(client, owner_id: int) -> None:
    """Wire the callback query router onto the helper bot client.

    Dispatches based on callback data prefix:
      - ``panel:`` → panel navigation handler
      - ``action:`` → action execution handler (Type A)
      - ``input:`` → input state setup (Type B)
    """
    logger.info("[PANEL] callback handler registered: owner_id=%s", owner_id)

    @client.on(events.CallbackQuery())
    async def _callback_router(event):
        chat_id, msg_id, inline_msg_id = resolve_callback_message(event)
        sender_id = event.sender_id
        data_raw = event.data
        data = data_raw.decode("utf-8") if data_raw else ""

        if chat_id is None and inline_msg_id is None:
            logger.warning("[PANEL] callback unresolvable: no chat_id and no inline_message_id")
            return

        if not is_owner(event, owner_id):
            return

        if not data:
            return

        try:
            if data.startswith("panel:"):
                await _handle_panel(event, data[6:])
            elif data.startswith("action:"):
                await _handle_action(event, data[7:])
            elif data.startswith("input:"):
                await _handle_input(event, data[6:], owner_id)
            else:
                logger.warning("[PANEL] unknown callback prefix: '%s'", data)
        except Exception:
            logger.exception("[PANEL] callback router error (data='%s')", data)


async def _handle_panel(event, remainder: str) -> None:
    parts = remainder.split(":", 1)
    panel_id = parts[0]
    extra = parts[1] if len(parts) > 1 else ""

    handler = get_panel(panel_id)
    if handler is None:
        logger.warning("[CALLBACK] no panel registered for id='%s'", panel_id)
        return

    try:
        result = await handler(event, extra)
        if result is None:
            return
        title, body, buttons = result
        from backend.helper.panel_render import render_edit
        text, built_buttons = render_edit(title, body, buttons)
        try:
            await event.edit(text, buttons=built_buttons)
        except Exception as exc:
            logger.warning("[CALLBACK] panel edit failed: %s", exc)
    except Exception:
        logger.exception("[CALLBACK] panel handler '%s' FAILED", panel_id)


async def _handle_action(event, remainder: str) -> None:
    parts = remainder.split(":", 1)
    action_id = parts[0]
    extra = parts[1] if len(parts) > 1 else ""

    handler = get_action(action_id)
    if handler is None:
        logger.warning("[CALLBACK] no action registered for id='%s'", action_id)
        return

    try:
        result = await handler(event, extra)
        if result is None:
            return
        if isinstance(result, tuple):
            if len(result) == 3:
                title, body, buttons = result
            else:
                title, body, buttons = result[0], result[1] if len(result) > 1 else "", result[2] if len(result) > 2 else []
        else:
            title, body, buttons = result, "", []
        from backend.helper.panel_render import render_edit
        text, built_buttons = render_edit(title, body, buttons)
        try:
            await event.edit(text, buttons=built_buttons)
        except Exception as exc:
            logger.warning("[CALLBACK] action edit failed: %s", exc)
    except Exception:
        logger.exception("[CALLBACK] action handler '%s' FAILED", action_id)


async def _handle_input(event, remainder: str, owner_id: int) -> None:
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

    chat_id, msg_id, inline_msg_id = resolve_callback_message(event)
    set_pending(
        owner_id, panel_id, handler, chat_id or 0, prompt,
        inline_chat_id=chat_id or 0, inline_msg_id=msg_id or 0,
    )

    builder = InlinePanelBuilder()
    builder.add_row("Cancel", f"panel:{panel_id}")

    try:
        await event.edit(prompt, buttons=builder.build())
    except Exception as exc:
        logger.warning("[CALLBACK] input prompt edit failed: %s", exc)
