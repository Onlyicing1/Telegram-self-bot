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

SESSION TRACING:
  Every panel creation gets a unique PANEL-SESSION-NNNNNN ID.
  Every callback logs session_id, chat_id, msg_id, callback_data.
  Every mutation logs before/after state with object ids and dict keys.
  This proves exactly where nav_stack changes.
"""
import logging
import time
import uuid as _uuid
from datetime import datetime, timezone
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


def _now_ms() -> float:
    return time.monotonic() * 1000.0


# ── Session / identity tracing ──
# Every panel creation gets a unique session ID and panel UUID.
# Every callback logs the session state so we can prove whether
# the Close callback operates on the same message that was created.
_session_counter: int = 0
# Key: (chat_id, msg_id) -> session metadata dict
_sessions: dict[tuple[int, int], dict] = {}


def _create_session(chat_id: int, msg_id: int, panel_type: str = "unknown") -> str:
    """Create a new panel session with a unique ID and UUID."""
    global _session_counter
    _session_counter += 1
    sid = f"PANEL-SESSION-{_session_counter:06d}"
    panel_uuid = str(_uuid.uuid4())
    key = (chat_id, msg_id)
    _sessions[key] = {
        "session_id": sid,
        "panel_uuid": panel_uuid,
        "chat_id": chat_id,
        "msg_id": msg_id,
        "panel_type": panel_type,
        "created_at": datetime.now(timezone.utc).isoformat(),
    }
    logger.info(
        "[PANEL] SESSION CREATED session=%s panel_uuid=%s "
        "chat_id=%s msg_id=%s panel_type='%s' key=%s",
        sid, panel_uuid, chat_id, msg_id, panel_type, key,
    )
    return sid


def _get_session(chat_id: int | None, msg_id: int | None) -> dict | None:
    """Look up session metadata by (chat_id, msg_id)."""
    if chat_id is None or msg_id is None:
        logger.info(
            "[PANEL] SESSION LOOKUP skipped: chat_id=%s msg_id=%s "
            "all_keys=%s",
            chat_id, msg_id, list(_sessions.keys()),
        )
        return None
    key = (chat_id, msg_id)
    session = _sessions.get(key)
    logger.info(
        "[PANEL] SESSION LOOKUP key=%s found=%s "
        "session_id=%s panel_uuid=%s "
        "all_keys=%s",
        key, session is not None,
        session.get("session_id", "N/A") if session else "N/A",
        session.get("panel_uuid", "N/A") if session else "N/A",
        list(_sessions.keys()),
    )
    return session


def clear_session(chat_id: int | None, msg_id: int | None) -> None:
    """Remove session metadata for (chat_id, msg_id)."""
    if chat_id is None or msg_id is None:
        return
    _sessions.pop((chat_id, msg_id), None)


def _log_callback(session_id: str, chat_id: int | None, msg_id: int | None,
                  callback_data: str, label: str,
                  inline_message_id: str | None = None) -> None:
    """Log full callback state at a boundary."""
    session = _get_session(chat_id, msg_id)
    panel_uuid = session.get("panel_uuid", "N/A") if session else "N/A"
    logger.info(
        "[PANEL] %s session=%s panel_uuid=%s "
        "chat=%s msg=%s callback_data='%s' "
        "inline_message_id=%s session_found=%s",
        label, session_id, panel_uuid,
        chat_id, msg_id, callback_data,
        inline_message_id,
        session is not None,
    )


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
    logger.info("HELP STEP 12 - callback handler registered: owner_id=%s", owner_id)

    @client.on(events.CallbackQuery())
    async def _callback_router(event):
        t_enter = _now_ms()
        chat_id, msg_id, inline_msg_id = resolve_callback_message(event)
        sender_id = event.sender_id
        data_raw = event.data
        data = data_raw.decode("utf-8") if data_raw else ""

        logger.info(
            "[PANEL] CALLBACK RESOLVED event_type=%s chat_id=%s msg_id=%s "
            "inline_message_id=%s peer=%s",
            type(event).__name__, chat_id, msg_id, inline_msg_id,
            getattr(event, "peer_id", None),
        )
        logger.info(
            "HELP STEP 13 - callback received: data=%s, sender_id=%s, msg_id=%s, chat_id=%s",
            data_raw, sender_id, msg_id, chat_id,
        )

        if chat_id is None and inline_msg_id is None:
            logger.warning(
                "[PANEL] CALLBACK UNRESOLVABLE: no chat_id and no inline_message_id — "
                "returning safely (data='%s')",
                data,
            )
            return

        # Session trace — log full state at callback entry
        session = _get_session(chat_id, msg_id)
        session_id = session.get("session_id", "NO-SESSION") if session else "NO-SESSION"
        _log_callback(session_id, chat_id, msg_id, data, "CALLBACK ENTRY", inline_msg_id)

        if not is_owner(event, owner_id):
            t_after = _now_ms()
            logger.warning(
                "[TIMING] _callback_router owner check FAILED: elapsed=%.1fms, "
                "sender_id=%s, owner_id=%s",
                t_after - t_enter, sender_id, owner_id,
            )
            logger.warning(
                "HELP STEP 13 - callback owner check FAILED: sender_id=%s, owner_id=%s",
                sender_id, owner_id,
            )
            return

        t_after_owner = _now_ms()
        logger.info("[TIMING] _callback_router owner check PASS: elapsed=%.1fms", t_after_owner - t_enter)
        logger.info("HELP STEP 13 - callback owner check passed")

        if not data:
            logger.warning("HELP STEP 13 - empty callback data — ignoring")
            return

        logger.info("HELP STEP 13 - callback dispatching: data='%s'", data)
        _log_callback(session_id, chat_id, msg_id, data, "BEFORE DISPATCH", inline_msg_id)
        try:
            if data.startswith("panel:"):
                logger.info("HELP STEP 13 - callback → _handle_panel(remainder='%s')", data[6:])
                await _handle_panel(event, data[6:])
            elif data.startswith("action:"):
                logger.info("HELP STEP 13 - callback → _handle_action(remainder='%s')", data[7:])
                await _handle_action(event, data[7:])
            elif data.startswith("input:"):
                logger.info("HELP STEP 13 - callback → _handle_input(remainder='%s')", data[6:])
                await _handle_input(event, data[6:], owner_id)
            else:
                logger.warning("HELP STEP 13 - callback unknown prefix in data='%s'", data)
            t_dispatched = _now_ms()
            logger.info("[TIMING] _callback_router dispatch DONE: elapsed=%.1fms", t_dispatched - t_enter)
            _log_callback(session_id, chat_id, msg_id, data, "AFTER DISPATCH", inline_msg_id)
        except Exception:
            t_err = _now_ms()
            logger.info("[TIMING] _callback_router EXCEPTION: elapsed=%.1fms", t_err - t_enter)
            logger.exception("HELP STEP 13 - callback router error (data='%s')", data)


async def _handle_panel(event, remainder: str) -> None:
    parts = remainder.split(":", 1)
    panel_id = parts[0]
    extra = parts[1] if len(parts) > 1 else ""

    logger.info("[CALLBACK] _handle_panel: panel_id='%s', extra='%s'", panel_id, extra)

    handler = get_panel(panel_id)
    if handler is None:
        logger.warning(
            "[CALLBACK] no panel registered for id='%s' (registered: %s)",
            panel_id, list(_panels.keys()),
        )
        return

    logger.info("[CALLBACK] panel handler found — invoking")
    t_before = _now_ms()
    try:
        await handler(event, extra)
        t_after = _now_ms()
        logger.info(
            "[TIMING] _handle_panel handler DONE: elapsed=%.1fms, panel_id='%s'",
            t_after - t_before, panel_id,
        )
        logger.info("[CALLBACK] panel handler completed")
    except Exception:
        t_after = _now_ms()
        logger.info(
            "[TIMING] _handle_panel handler EXCEPTION: elapsed=%.1fms, panel_id='%s'",
            t_after - t_before, panel_id,
        )
        logger.exception("[CALLBACK] panel handler '%s' FAILED", panel_id)


async def _handle_action(event, remainder: str) -> None:
    parts = remainder.split(":", 1)
    action_id = parts[0]
    extra = parts[1] if len(parts) > 1 else ""

    logger.info("[CALLBACK] _handle_action: action_id='%s', extra='%s'", action_id, extra)

    handler = get_action(action_id)
    if handler is None:
        logger.warning(
            "[CALLBACK] no action registered for id='%s' (registered: %s)",
            action_id, list(_actions.keys()),
        )
        return

    logger.info("[CALLBACK] action handler found — invoking")
    t_before = _now_ms()
    try:
        result = await handler(event, extra)
        t_after_handler = _now_ms()
        logger.info(
            "[TIMING] _handle_action handler DONE: elapsed=%.1fms, action_id='%s'",
            t_after_handler - t_before, action_id,
        )
        logger.info("[CALLBACK] action handler returned: type=%s", type(result).__name__)

        if result is None:
            return
        if isinstance(result, tuple):
            text, buttons = result
        else:
            text, buttons = result, []
        if text:
            t_before_edit = _now_ms()
            try:
                await event.edit(text, buttons=buttons)
                t_after_edit = _now_ms()
                logger.info("[TIMING] _handle_action edit DONE: elapsed=%.1fms", t_after_edit - t_before_edit)
                logger.info("[CALLBACK] action result edit succeeded")
            except Exception as exc:
                t_after_edit = _now_ms()
                logger.info("[TIMING] _handle_action edit FAILED: elapsed=%.1fms", t_after_edit - t_before_edit)
                logger.warning("[CALLBACK] action result edit failed: %s", exc)
    except Exception:
        t_after = _now_ms()
        logger.info(
            "[TIMING] _handle_action EXCEPTION: elapsed=%.1fms, action_id='%s'",
            t_after - t_before, action_id,
        )
        logger.exception("[CALLBACK] action handler '%s' FAILED", action_id)


async def _handle_input(event, remainder: str, owner_id: int) -> None:
    parts = remainder.split(":", 1)
    panel_id = parts[0]
    input_id = parts[1] if len(parts) > 1 else ""

    logger.info("[CALLBACK] _handle_input: panel_id='%s', input_id='%s'", panel_id, input_id)

    input_cfg = get_input(panel_id, input_id)
    if input_cfg is None:
        logger.warning(
            "[CALLBACK] no input registered: panel='%s', input_id='%s' (registered: %s)",
            panel_id, input_id, list(_inputs.keys()),
        )
        return

    prompt = input_cfg.get("prompt", "Enter input:")
    handler = input_cfg.get("handler")

    if handler is None:
        logger.warning("[CALLBACK] input config has no handler")
        return

    chat_id, msg_id, inline_msg_id = resolve_callback_message(event)
    logger.info(
        "[CALLBACK] setting pending: owner_id=%s, panel_id='%s', chat_id=%s, inline_msg_id=%s",
        owner_id, panel_id, chat_id, inline_msg_id,
    )
    set_pending(
        owner_id, panel_id, handler, chat_id or 0, prompt,
        inline_chat_id=chat_id or 0, inline_msg_id=msg_id or 0,
    )

    builder = InlinePanelBuilder()
    builder.add_row("Cancel", f"panel:{panel_id}")

    t_before = _now_ms()
    try:
        await event.edit(prompt, buttons=builder.build())
        t_after = _now_ms()
        logger.info("[TIMING] _handle_input edit DONE: elapsed=%.1fms", t_after - t_before)
        logger.info("[CALLBACK] input prompt edit succeeded")
    except Exception as exc:
        t_after = _now_ms()
        logger.info("[TIMING] _handle_input edit FAILED: elapsed=%.1fms", t_after - t_before)
        logger.warning("[CALLBACK] input prompt edit failed: %s", exc)
