"""
Inline panel system for the helper bot.

ROOT MENU rules:
  - Root menu (nav_stack length 1) has ONLY Close — never Back or Home.
  - Submenus (nav_stack length > 1) have Back, Home, and Close.

SESSION LIFECYCLE:
  All lifecycle management goes through the PanelLifecycleManager.
  This module handles callback routing and rendering only.
"""
import asyncio
import logging
import time
from typing import Awaitable, Callable, Any

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.helper import callback_gate
from backend.helper.context import truncate_callback_data
from backend.helper.input_state import set_pending, clear_pending, get_pending
from backend.helper.lifecycle import get_lifecycle
from backend.helper.panel_registry import registry as get_registry
from backend.helper.panel_render import _callback, _url
from backend.helper.session_manager import Session
from backend.helper.styles import PRIMARY
from backend.helper import symbols as sym

logger = logging.getLogger(__name__)


def _buttons_repr(buttons: list) -> tuple[tuple[str, ...], ...]:
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
    lifecycle = get_lifecycle()
    key = (chat_id, msg_id)
    new_repr = (text, _buttons_repr(buttons))
    last = lifecycle.get_render(chat_id or 0, msg_id or 0)
    if last == new_repr:
        logger.info("[CALLBACK] edit skipped — content unchanged (key=%s:%s)", chat_id, msg_id)
        return False
    started = time.monotonic()
    try:
        await event.edit(text, buttons=buttons if buttons else [])
        lifecycle.set_render(chat_id or 0, msg_id or 0, text, new_repr)
        logger.info("[CALLBACK] edit OK (key=%s:%s)", chat_id, msg_id)
        return True
    except Exception as exc:
        msg_lower = str(exc).lower()
        if "not modified" in msg_lower:
            lifecycle.set_render(chat_id or 0, msg_id or 0, text, new_repr)
            logger.info("[CALLBACK] edit not-modified (key=%s:%s)", chat_id, msg_id)
            return False
        logger.warning("[CALLBACK] edit FAILED: %s (key=%s:%s)", exc, chat_id, msg_id)
        return False
    finally:
        # One Telegram round trip, attributed to the click that caused it.
        callback_gate.note_edit((time.monotonic() - started) * 1000.0)


def _progress_buttons() -> list:
    """Minimal nav row for transient states (progress, working panels)."""
    builder = InlinePanelBuilder()
    builder.add_row(sym.label(sym.OP_HOME, "Home"), "panel:_nav:home")
    return builder.build()


async def _edit_panel_message(
    event, chat_id: int, msg_id: int, text: str, buttons: list,
) -> bool:
    """Edit a Glass UI panel message through the production edit path.

    Same dedupe + error handling as the callback render path so background
    flows (model test progress) never spam Telegram with identical edits.
    """
    return await _safe_edit(event, text, buttons, chat_id or None, msg_id or None)


def resolve_callback_message(event) -> tuple[int | None, int | None, str | None]:
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
ActionHandler = Callable[[events.CallbackQuery.Event, str, int], Awaitable[str]]
InputConfig = dict[str, Any]

_actions: dict[str, ActionHandler] = {}
_inputs: dict[str, dict[str, InputConfig]] = {}


def _styled_button(text: str, callback_data: str, style: str | None = None):
    """Panel button that may carry a named style.

    Both styled and unstyled buttons are real ``KeyboardButtonCallback``
    objects (data already encoded), preserving the builder's existing contract.
    When no style is supplied the button defaults to ``primary`` (blue), so a
    caller that omits the argument never produces a silently-unstyled button;
    unsupported styles fall back to an unstyled button.
    """
    return _callback(text, truncate_callback_data(callback_data), style if style is not None else PRIMARY)


class InlinePanelBuilder:
    def __init__(self):
        self._rows: list[list[Any]] = []

    def add_row(self, text: str, callback_data: str, style: str | None = None) -> "InlinePanelBuilder":
        self._rows.append([_styled_button(text, callback_data, style)])
        return self

    def add_buttons(self, *buttons: tuple) -> "InlinePanelBuilder":
        row = [_styled_button(text, data, style) for text, data, style in
               ((*b, None) if len(b) == 2 else b for b in buttons)]
        self._rows.append(row)
        return self

    def add_url(self, text: str, url: str, style: str | None = None) -> "InlinePanelBuilder":
        self._rows.append([_url(text, url, style if style is not None else PRIMARY)])
        return self

    def build(self) -> list[list[Any]]:
        return [list(row) for row in self._rows]


_NAV_ACTIONS = {"back", "home", "close"}

#: Panels that render their OWN complete button set and must never receive the
#: automatic Back/Home/Close navigation row. Used by the Emoji diagnostic
#: panel, whose primary view must expose exactly one application-level button.
_NAV_FREE_PANELS: set[str] = set()


def set_panel_nav_free(panel_id: str, enabled: bool = True) -> None:
    """Opt a panel out of (or back into) the automatic navigation row.

    Only panels explicitly listed here are affected; every other panel keeps
    the standard Back/Home/Close behaviour.
    """
    if enabled:
        _NAV_FREE_PANELS.add(panel_id)
    else:
        _NAV_FREE_PANELS.discard(panel_id)


def _add_close_button(builder: InlinePanelBuilder) -> None:
    builder.add_row(sym.label(sym.OP_CLOSE, "Close"), "panel:_nav:close")


def _add_nav_buttons(builder: InlinePanelBuilder) -> None:
    builder.add_buttons(
        (sym.label(sym.OP_BACK, "Back"), "panel:_nav:back"),
        (sym.label(sym.OP_HOME, "Home"), "panel:_nav:home"),
    )
    builder.add_row(sym.label(sym.OP_CLOSE, "Close"), "panel:_nav:close")


def _has_nav_buttons(buttons: list) -> bool:
    for row in buttons:
        if isinstance(row, list):
            for btn in row:
                data = getattr(btn, "data", None)
                if data is None:
                    continue
                decoded = ""
                if isinstance(data, bytes):
                    decoded = data.decode("utf-8", errors="replace")
                elif isinstance(data, str):
                    decoded = data
                if decoded.startswith("panel:_nav:"):
                    action = decoded[len("panel:_nav:"):]
                    if action in _NAV_ACTIONS:
                        return True
    return False


def _finalize_panel(
    title: str, body: str, buttons: list | None, panel_id: str,
    chat_id: int | None = None, msg_id: int | None = None,
) -> tuple[str, str, list]:
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

    # A nav-free panel renders its own complete button set (exactly the buttons
    # it returned) — no automatic Back/Home/Close is appended.
    if panel_id in _NAV_FREE_PANELS:
        return title, body, builder.build()

    is_root = True
    if chat_id is not None and msg_id is not None:
        is_root = get_lifecycle().sessions.is_root_view(chat_id, msg_id)

    if is_root:
        _add_close_button(builder)
    else:
        _add_nav_buttons(builder)

    return title, body, builder.build()


def register_panel(
    panel_id: str,
    handler: PanelHandler,
    parent: str = "menu",
    title: str = "",
) -> None:
    get_registry().register(panel_id, handler, parent=parent, title=title)


def get_panel(panel_id: str) -> PanelHandler | None:
    return get_registry().get_handler(panel_id)


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
    try:
        await event.answer()
    except Exception:
        pass


def _sync_timer(chat_id: int, msg_id: int, title: str, body: str, buttons: list) -> None:
    try:
        get_lifecycle().timers.set_content(chat_id, msg_id, title, body, buttons)
    except Exception:
        pass


def _extract_render_result(result) -> tuple[str, str, list]:
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
    title, body, buttons = _extract_render_result(result)
    if not title and not body and not buttons:
        logger.warning("[CALLBACK] render_and_edit: empty result for panel='%s'", panel_id)
        return
    title, body, buttons = _finalize_panel(title, body, buttons, panel_id, chat_id, msg_id)
    from backend.helper.panel_render import render_edit
    text, built_buttons = render_edit(title, body, buttons)
    _sync_timer(chat_id or 0, msg_id or 0, title, body, buttons)
    await _present(event, text, built_buttons, chat_id, msg_id)


def _resolve_session(chat_id: int | None, msg_id: int | None, inline_message_id: str | None) -> tuple[Session | None, int, int]:
    sessions = get_lifecycle().sessions
    session = sessions.get(chat_id, msg_id)
    if session is not None:
        logger.info(
            "[CALLBACK] session lookup OK by (chat_id=%s, msg_id=%s) → session_id=%s",
            chat_id, msg_id, session.session_id,
        )
        return session, chat_id or 0, msg_id or 0

    if not chat_id and msg_id:
        session = sessions.find_by_msg_id(msg_id)
        if session is not None:
            real_chat = session.chat_id or 0
            real_msg = session.msg_id or msg_id
            logger.info(
                "[CALLBACK] session lookup OK by msg_id=%s → resolved (chat_id=%s, msg_id=%s) session_id=%s",
                msg_id, real_chat, real_msg, session.session_id,
            )
            return session, real_chat, real_msg

    if inline_message_id:
        session = sessions.get_by_inline_id(inline_message_id)
        if session is not None:
            real_chat = session.chat_id or 0
            real_msg = session.msg_id or 0
            logger.info(
                "[CALLBACK] session lookup OK by inline_message_id='%s' → resolved (chat_id=%s, msg_id=%s) session_id=%s",
                inline_message_id, real_chat, real_msg, session.session_id,
            )
            return session, real_chat, real_msg

    logger.warning(
        "[CALLBACK] session lookup FAILED: chat_id=%s msg_id=%s inline_message_id='%s' — "
        "callback will be dropped",
        chat_id, msg_id, inline_message_id or "",
    )
    return None, chat_id or 0, msg_id or 0


async def _present(event, text: str, buttons: list, chat_id: int | None, msg_id: int | None) -> None:
    """Paint one view through the panel's single writer, outside the turn.

    The newest view wins: a view superseded while another paint is in flight is
    simply replaced in the slot, so N queued clicks cost at most two Telegram
    round trips instead of N, and the clicks behind them never wait for an edit
    they are about to overwrite.
    """
    painter = callback_gate.painter(chat_id, msg_id)

    async def _paint(view) -> None:
        if painter.closed:
            return
        view_text, view_buttons = view
        await _safe_edit(event, view_text, view_buttons, chat_id, msg_id)

    if painter.publish((text, buttons)):
        await painter.drain(_paint)
    else:
        callback_gate.note_coalesced()
        logger.info(
            "[CALLBACK] view superseded before painting (key=%s:%s)", chat_id, msg_id
        )


def register_callback_handlers(client, owner_id: int) -> None:
    logger.info("[PANEL] callback handler registered: owner_id=%s client_id=%s", owner_id, id(client))

    @client.on(events.CallbackQuery())
    async def _callback_router(event):
        from backend.services import settings_service
        from backend.health import set_last_callback

        try:
            set_last_callback()
        except Exception:
            pass

        chat_id, msg_id, inline_msg_id = resolve_callback_message(event)
        data_raw = event.data
        data = data_raw.decode("utf-8") if data_raw else ""

        # Admission and acknowledgement come FIRST, before any session lookup,
        # database read, render or edit. The answer is what the owner's client
        # is waiting on, and Telegram expires it long before a slow handler
        # returns; everything below is allowed to take its time, the answer is
        # not. A query delivered twice is answered and then dropped: its
        # handler already ran once and must not run (or repeat its side
        # effects) again.
        query_id = callback_gate.query_id_of(event)
        admission = callback_gate.admit(data, query_id)
        if admission is None:
            logger.info(
                "[CALLBACK] duplicate query ignored (query_id=%s data='%s')",
                query_id, data[:64],
            )
            await _safe_answer(event)
            return
        await callback_gate.ack(event, admission)
        # From here on every observed cost (database round trips, panel edits,
        # state mutation) is attributed to THIS click's timing record.
        callback_gate.begin(admission)

        sender_id = getattr(event, "sender_id", None)

        debug = settings_service.is_debug_callbacks()
        if debug:
            logger.info(
                "[CALLBACK] ─── INCOMING ─── data='%s' sender_id=%s owner_id=%s "
                "chat_id=%s msg_id=%s inline_msg_id='%s'",
                data, sender_id, owner_id,
                chat_id, msg_id, inline_msg_id or "",
            )

        if chat_id is None and not inline_msg_id:
            if debug:
                logger.warning("[CALLBACK] REJECT: unresolvable — no chat_id and no inline_message_id")
            callback_gate.finish(admission, "rejected:no-coordinates")
            return

        if settings_service.is_owner_only() and not is_owner(event, owner_id):
            if debug:
                logger.info("[CALLBACK] REJECT: not owner (sender_id=%s owner_id=%s)", sender_id, owner_id)
            callback_gate.finish(admission, "rejected:not-owner")
            return

        if not data:
            if debug:
                logger.warning("[CALLBACK] REJECT: empty callback data")
            callback_gate.finish(admission, "rejected:empty-data")
            return

        session, real_chat_id, real_msg_id = _resolve_session(chat_id, msg_id, inline_msg_id)
        if session is None:
            if debug:
                logger.warning(
                    "[CALLBACK] REJECT: no session for chat_id=%s msg_id=%s inline_msg_id='%s'",
                    chat_id, msg_id, inline_msg_id or "",
                )
            callback_gate.finish(admission, "rejected:no-session")
            return

        chat_id = real_chat_id
        msg_id = real_msg_id

        # A click on a panel a background flow is still editing (Deep Save
        # progress) makes this click its owner: the older flow must stop
        # writing instead of overwriting the state this click produces. A
        # no-op for every panel without a background operation.
        from backend.helper.panel_edit import supersede_panel_operation

        if supersede_panel_operation(chat_id, msg_id):
            logger.info(
                "[CALLBACK] superseded a background panel writer (chat=%s msg=%s)",
                chat_id, msg_id,
            )

        # One turn per panel: clicks on the SAME message run in arrival order,
        # clicks on different messages never wait for each other. The turn now
        # covers ONLY the handler (state change + building the view): painting
        # is a projection of that state and goes through the panel's single
        # writer afterwards, so a queued click never waits for an edit it is
        # about to supersede.
        turn = await callback_gate.acquire_turn(chat_id, msg_id)
        if turn is None:
            callback_gate.finish(admission, "dropped:panel-busy")
            logger.warning(
                "[CALLBACK] panel turn unavailable after %.0fs — click dropped "
                "data='%s' chat=%s msg=%s",
                callback_gate.DEFAULT_TURN_WAIT_S, data[:64], chat_id, msg_id,
            )
            return

        paint: tuple[str, object] | None = None
        outcome = "completed"
        try:
            try:
                # A click that queued behind another click may have been aimed
                # at a panel that click CLOSED: re-check before touching the
                # message, so a queued click can never edit a panel that is gone.
                if get_lifecycle().sessions.get(chat_id, msg_id) is None:
                    outcome = "dropped:panel-closed"
                    logger.info(
                        "[CALLBACK] panel closed while this click waited — dropped "
                        "data='%s' chat=%s msg=%s", data[:64], chat_id, msg_id,
                    )
                    return

                if debug:
                    logger.info("[CALLBACK] DISPATCH: data='%s' session_id=%s chat_id=%s msg_id=%s", data, session.session_id, chat_id, msg_id)

                admission.mark_dispatch()
                if data.startswith("panel:"):
                    if debug:
                        logger.info("[CALLBACK] → panel handler: remainder='%s'", data[6:])
                    paint = await _handle_panel(event, data[6:], chat_id, msg_id, owner_id, inline_msg_id)
                elif data.startswith("action:"):
                    if debug:
                        logger.info("[CALLBACK] → action handler: remainder='%s'", data[7:])
                    paint = await _handle_action(event, data[7:], chat_id, msg_id, owner_id)
                elif data.startswith("input:"):
                    if debug:
                        logger.info("[CALLBACK] → input handler: remainder='%s'", data[6:])
                    await _handle_input(event, data[6:], owner_id, chat_id, msg_id)
                else:
                    outcome = "rejected:unknown-prefix"
                    if debug:
                        logger.warning("[CALLBACK] unknown callback prefix: '%s'", data)
            finally:
                # The turn is released BEFORE the paint: the handler's state
                # change is complete, and the next accepted click must not wait
                # for a Telegram edit it is going to supersede.
                admission.mark_handled()
                callback_gate.release_turn(chat_id, msg_id, turn)

            if paint is not None and outcome == "completed":
                panel_id, result = paint
                await _render_and_edit(event, result, panel_id, chat_id, msg_id)
        except asyncio.CancelledError:
            outcome = "cancelled"
            raise
        except Exception as exc:
            outcome = "failed"
            logger.exception("[CALLBACK] callback router error (data='%s'): %s", data, exc)
        finally:
            admission.mark_rendered()
            callback_gate.finish(admission, outcome)


async def _handle_panel(event, remainder: str, chat_id: int, msg_id: int, owner_id: int, inline_msg_id: str | None = None) -> tuple[str, object] | None:
    """Run one panel query and RETURN what to paint; the router paints it."""
    parts = remainder.split(":", 1)
    panel_id = parts[0]
    extra = parts[1] if len(parts) > 1 else ""

    logger.info(
        "[CALLBACK] _handle_panel: panel_id='%s' extra='%s' chat_id=%s msg_id=%s",
        panel_id, extra, chat_id, msg_id,
    )

    if panel_id == "_nav":
        logger.info("[CALLBACK] → navigation: action='%s'", extra)
        return await _handle_navigation(event, extra, chat_id, msg_id, owner_id)

    handler = get_panel(panel_id)
    if handler is None:
        logger.warning(
            "[CALLBACK] _handle_panel: NO HANDLER for panel_id='%s' — "
            "registered panels: %s",
            panel_id, get_registry().all_ids(),
        )
        return None

    logger.info("[CALLBACK] _handle_panel: handler found for '%s', calling...", panel_id)

    lifecycle = get_lifecycle()
    lifecycle.sessions.push_nav(chat_id, msg_id, panel_id, extra)
    clear_pending(owner_id)

    result = await handler(event, extra)
    logger.info("[CALLBACK] _handle_panel: handler returned (panel='%s')", panel_id)
    return panel_id, result


async def dispatch_click(
    event,
    data: str,
    chat_id: int,
    msg_id: int,
    owner_id: int,
    inline_msg_id: str | None = None,
) -> None:
    """Run one already-admitted callback and paint its result.

    The same two phases the gated router performs (handler, then paint) without
    the admission gates and the per-panel turn — for callers that already own
    the click, so a dispatch + render can never be written twice and drift.
    """
    paint = None
    if data.startswith("panel:"):
        paint = await _handle_panel(event, data[6:], chat_id, msg_id, owner_id, inline_msg_id)
    elif data.startswith("action:"):
        paint = await _handle_action(event, data[7:], chat_id, msg_id, owner_id)
    elif data.startswith("input:"):
        await _handle_input(event, data[6:], owner_id, chat_id, msg_id)
        return
    else:
        raise ValueError(f"unknown callback prefix: {data[:64]}")
    if paint is not None:
        panel_id, result = paint
        await _render_and_edit(event, result, panel_id, chat_id, msg_id)


async def close_panel(event, chat_id: int, msg_id: int, owner_id: int) -> None:
    """Delegate to the lifecycle manager for cleanup."""
    logger.info("[CLOSE] close_panel: chat_id=%s msg_id=%s", chat_id, msg_id)
    try:
        await get_lifecycle().close_panel(chat_id, msg_id, event=event)
    finally:
        # A closed panel has nothing left to paint: drop any pending view so a
        # paint in flight cannot edit the closed message afterwards.
        callback_gate.forget_painter(chat_id, msg_id)


async def _handle_navigation(event, action: str, chat_id: int, msg_id: int, owner_id: int) -> tuple[str, object] | None:
    logger.info("[CALLBACK] _handle_navigation: action='%s' chat_id=%s msg_id=%s", action, chat_id, msg_id)

    if action == "close":
        await close_panel(event, chat_id, msg_id, owner_id)
        return None

    if action == "home":
        lifecycle = get_lifecycle()
        lifecycle.sessions.reset_nav(chat_id, msg_id, "menu", "")
        clear_pending(owner_id)
        handler = get_panel("menu")
        if handler is None:
            logger.warning("[CALLBACK] _handle_navigation: no 'menu' panel registered")
            return None
        result = await handler(event, "")
        logger.info("[CALLBACK] _handle_navigation: home handled")
        return "menu", result

    if action == "back":
        lifecycle = get_lifecycle()
        nav = lifecycle.sessions.current_nav(chat_id, msg_id)
        current = nav[0] if nav else "menu"
        prev = lifecycle.sessions.pop_nav(chat_id, msg_id)
        clear_pending(owner_id)
        if prev is None:
            parent = get_registry().get_parent(current)
            prev = (parent, "")
        prev_panel, prev_extra = prev
        if prev_panel == current:
            parent = get_registry().get_parent(prev_panel)
            prev = (parent, "")
            prev_panel, prev_extra = prev
        logger.info("[CALLBACK] _handle_navigation: back → panel='%s' extra='%s'", prev_panel, prev_extra)
        handler = get_panel(prev_panel)
        if handler is None:
            prev_panel = "menu"
            prev_extra = ""
            handler = get_panel("menu")
        if handler is None:
            logger.warning("[CALLBACK] _handle_navigation: back — no handler for '%s'", prev_panel)
            return None
        result = await handler(event, prev_extra)
        logger.info("[CALLBACK] _handle_navigation: back handled")
        return prev_panel, result

    if action == "noop":
        return None


async def _handle_action(event, remainder: str, chat_id: int, msg_id: int, owner_id: int) -> tuple[str, object] | None:
    parts = remainder.split(":", 1)
    action_id = parts[0]
    extra = parts[1] if len(parts) > 1 else ""

    logger.info("[CALLBACK] _handle_action: action_id='%s' extra='%s'", action_id, extra)

    handler = get_action(action_id)
    if handler is None:
        logger.warning(
            "[CALLBACK] _handle_action: NO HANDLER for action_id='%s' — "
            "registered actions: %s",
            action_id, list(_actions.keys()),
        )
        return None

    clear_pending(owner_id)

    result = await handler(event, extra, chat_id)
    nav = get_lifecycle().sessions.current_nav(chat_id, msg_id)
    current_panel = nav[0] if nav else action_id
    logger.info("[CALLBACK] _handle_action: handled action='%s'", action_id)
    return current_panel, result


async def _handle_input(event, remainder: str, owner_id: int, chat_id: int, msg_id: int) -> None:
    parts = remainder.split(":", 2)
    panel_id = parts[0]
    input_id = parts[1] if len(parts) > 1 else ""
    extra = parts[2] if len(parts) > 2 else ""

    logger.info("[CALLBACK] _handle_input: panel_id='%s' input_id='%s' extra='%s'", panel_id, input_id, extra)

    input_cfg = get_input(panel_id, input_id)
    if input_cfg is None:
        logger.warning(
            "[CALLBACK] _handle_input: NO INPUT registered for panel='%s' input_id='%s' — "
            "registered inputs: %s",
            panel_id, input_id, {k: list(v.keys()) for k, v in _inputs.items()},
        )
        return

    prompt = input_cfg.get("prompt", "Enter input:")
    handler = input_cfg.get("handler")

    if handler is None:
        logger.warning("[CALLBACK] _handle_input: config has no handler")
        return

    # The inline identifier this click carried is the only handle that can
    # still edit the panel once the input arrives, so it travels with the
    # pending entry (empty for an ordinary message click).
    from backend.helper.panel_edit import inline_message_id_from_event

    clear_pending(owner_id)
    set_pending(
        owner_id, panel_id, handler, chat_id or 0, prompt,
        inline_chat_id=chat_id or 0, inline_msg_id=msg_id or 0,
        inline_id=inline_message_id_from_event(event),
        extra=extra,
    )

    # The prompt is a SUB-VIEW of the panel. Its first row re-opens the panel
    # that owns the input, which renders its CURRENT step from the live state
    # (for the Taskloom wizard: the same field step with the draft intact).
    # A generic nav Back here popped the panel stack instead (live: a Taskloom
    # wizard input's Back jumped to the Taskloom home/list), so the row is a
    # plain panel query and is LABELLED "Back" — it preserves state, which is
    # what a field input's Back must do. Panels whose input also needs a real
    # discard declare it as an explicit extra row (see ``extra_rows``).
    # Home/Close are still offered as explicit navigation.
    builder = InlinePanelBuilder()
    builder.add_row(sym.label(sym.OP_BACK, "Back"), input_cfg.get("back") or f"panel:{panel_id}")
    for extra_row in input_cfg.get("extra_rows") or ():
        label, data = extra_row[0], extra_row[1]
        style = extra_row[2] if len(extra_row) > 2 else None
        builder.add_row(str(label), str(data), style)
    builder.add_row(sym.label(sym.OP_HOME, "Home"), "panel:_nav:home")
    builder.add_row(sym.label(sym.OP_CLOSE, "Close"), "panel:_nav:close")

    built = builder.build()
    _sync_timer(chat_id or 0, msg_id or 0, panel_id, prompt, built)
    try:
        from backend.helper.panel_render import _style
        styled_prompt = _style(prompt)
    except Exception:
        styled_prompt = prompt
    await _present(event, styled_prompt, built, chat_id, msg_id)
    logger.info("[CALLBACK] _handle_input: prompt sent for panel='%s'", panel_id)
