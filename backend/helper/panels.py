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
from typing import Awaitable, Callable, Any

from telethon import events
from telethon.tl.custom import Button

from backend.bot.handlers.guard import is_owner
from backend.helper.context import truncate_callback_data
from backend.helper.input_state import set_pending, clear_pending, get_pending
from backend.helper.lifecycle import get_lifecycle
from backend.helper.session_manager import Session

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

_panels: dict[str, PanelHandler] = {}
_actions: dict[str, ActionHandler] = {}
_inputs: dict[str, dict[str, InputConfig]] = {}


class InlinePanelBuilder:
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


def _add_close_button(builder: InlinePanelBuilder) -> None:
    builder.add_row("✕ Close", "panel:_nav:close")


def _add_nav_buttons(builder: InlinePanelBuilder) -> None:
    builder.add_buttons(
        ("‹ Back", "panel:_nav:back"),
        ("🏠 Home", "panel:_nav:home"),
    )
    builder.add_row("✕ Close", "panel:_nav:close")


def _has_nav_buttons(buttons: list) -> bool:
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

    is_root = True
    if chat_id is not None and msg_id is not None:
        is_root = get_lifecycle().sessions.is_root_view(chat_id, msg_id)

    if is_root:
        _add_close_button(builder)
    else:
        _add_nav_buttons(builder)

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
            return (result[0] if result else ""), (result[1] if len(result) > 1 else ""), (result[2] if len(result) > 2 else "")
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
    await _safe_edit(event, text, built_buttons, chat_id, msg_id)


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


def register_callback_handlers(client, owner_id: int) -> None:
    logger.info("[PANEL] callback handler registered: owner_id=%s client_id=%s", owner_id, id(client))

    @client.on(events.CallbackQuery())
    async def _callback_router(event):
        from backend.services import settings_service

        chat_id, msg_id, inline_msg_id = resolve_callback_message(event)
        data_raw = event.data
        data = data_raw.decode("utf-8") if data_raw else ""

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
            await _safe_answer(event)
            return

        if settings_service.is_owner_only() and not is_owner(event, owner_id):
            if debug:
                logger.info("[CALLBACK] REJECT: not owner (sender_id=%s owner_id=%s)", sender_id, owner_id)
            await _safe_answer(event)
            return

        if not data:
            if debug:
                logger.warning("[CALLBACK] REJECT: empty callback data")
            await _safe_answer(event)
            return

        session, real_chat_id, real_msg_id = _resolve_session(chat_id, msg_id, inline_msg_id)
        if session is None:
            if debug:
                logger.warning(
                    "[CALLBACK] REJECT: no session for chat_id=%s msg_id=%s inline_msg_id='%s'",
                    chat_id, msg_id, inline_msg_id or "",
                )
            await _safe_answer(event)
            return

        chat_id = real_chat_id
        msg_id = real_msg_id

        if debug:
            logger.info("[CALLBACK] DISPATCH: data='%s' session_id=%s chat_id=%s msg_id=%s", data, session.session_id, chat_id, msg_id)

        try:
            if data.startswith("panel:"):
                if debug:
                    logger.info("[CALLBACK] → panel handler: remainder='%s'", data[6:])
                await _handle_panel(event, data[6:], chat_id, msg_id, owner_id, inline_msg_id)
            elif data.startswith("action:"):
                if debug:
                    logger.info("[CALLBACK] → action handler: remainder='%s'", data[7:])
                await _handle_action(event, data[7:], chat_id, msg_id, owner_id)
            elif data.startswith("input:"):
                if debug:
                    logger.info("[CALLBACK] → input handler: remainder='%s'", data[6:])
                await _handle_input(event, data[6:], owner_id, chat_id, msg_id)
            else:
                if debug:
                    logger.warning("[CALLBACK] unknown callback prefix: '%s'", data)
        except Exception as exc:
            logger.exception("[CALLBACK] callback router error (data='%s'): %s", data, exc)
        finally:
            await _safe_answer(event)


async def _handle_panel(event, remainder: str, chat_id: int, msg_id: int, owner_id: int, inline_msg_id: str | None = None) -> None:
    parts = remainder.split(":", 1)
    panel_id = parts[0]
    extra = parts[1] if len(parts) > 1 else ""

    logger.info(
        "[CALLBACK] _handle_panel: panel_id='%s' extra='%s' chat_id=%s msg_id=%s",
        panel_id, extra, chat_id, msg_id,
    )

    if panel_id == "_nav":
        logger.info("[CALLBACK] → navigation: action='%s'", extra)
        await _handle_navigation(event, extra, chat_id, msg_id, owner_id)
        return

    handler = get_panel(panel_id)
    if handler is None:
        logger.warning(
            "[CALLBACK] _handle_panel: NO HANDLER for panel_id='%s' — "
            "registered panels: %s",
            panel_id, list(_panels.keys()),
        )
        return

    logger.info("[CALLBACK] _handle_panel: handler found for '%s', calling...", panel_id)

    lifecycle = get_lifecycle()
    lifecycle.sessions.push_nav(chat_id, msg_id, panel_id, extra)
    clear_pending(owner_id)

    try:
        result = await handler(event, extra)
        logger.info("[CALLBACK] _handle_panel: handler returned, rendering... (panel='%s')", panel_id)
        await _render_and_edit(event, result, panel_id, chat_id, msg_id)
        logger.info("[CALLBACK] _handle_panel: COMPLETE for panel='%s'", panel_id)
    except Exception as exc:
        logger.exception("[CALLBACK] _handle_panel: handler '%s' FAILED: %s", panel_id, exc)


async def close_panel(event, chat_id: int, msg_id: int, owner_id: int) -> None:
    """Delegate to the lifecycle manager for cleanup."""
    logger.info("[CLOSE] close_panel: chat_id=%s msg_id=%s", chat_id, msg_id)
    await get_lifecycle().close_panel(chat_id, msg_id, event=event)


async def _handle_navigation(event, action: str, chat_id: int, msg_id: int, owner_id: int) -> None:
    logger.info("[CALLBACK] _handle_navigation: action='%s' chat_id=%s msg_id=%s", action, chat_id, msg_id)

    if action == "close":
        await close_panel(event, chat_id, msg_id, owner_id)
        return

    if action == "home":
        lifecycle = get_lifecycle()
        lifecycle.sessions.reset_nav(chat_id, msg_id, "help", "")
        clear_pending(owner_id)
        handler = get_panel("help")
        if handler is None:
            logger.warning("[CALLBACK] _handle_navigation: no 'help' panel registered")
            return
        try:
            result = await handler(event, "")
            await _render_and_edit(event, result, "help", chat_id, msg_id)
            logger.info("[CALLBACK] _handle_navigation: home COMPLETE")
        except Exception as exc:
            logger.exception("[CALLBACK] _handle_navigation: home FAILED: %s", exc)
        return

    if action == "back":
        lifecycle = get_lifecycle()
        prev = lifecycle.sessions.pop_nav(chat_id, msg_id)
        clear_pending(owner_id)
        if prev is None:
            prev = ("help", "")
        prev_panel, prev_extra = prev
        logger.info("[CALLBACK] _handle_navigation: back → panel='%s' extra='%s'", prev_panel, prev_extra)
        handler = get_panel(prev_panel)
        if handler is None:
            prev_panel = "help"
            prev_extra = ""
            handler = get_panel("help")
        if handler is None:
            logger.warning("[CALLBACK] _handle_navigation: back — no handler for '%s'", prev_panel)
            return
        try:
            result = await handler(event, prev_extra)
            await _render_and_edit(event, result, prev_panel, chat_id, msg_id)
            logger.info("[CALLBACK] _handle_navigation: back COMPLETE")
        except Exception as exc:
            logger.exception("[CALLBACK] _handle_navigation: back FAILED: %s", exc)
        return

    if action == "noop":
        return


async def _handle_action(event, remainder: str, chat_id: int, msg_id: int, owner_id: int) -> None:
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
        return

    clear_pending(owner_id)

    try:
        result = await handler(event, extra, chat_id)
        nav = get_lifecycle().sessions.current_nav(chat_id, msg_id)
        current_panel = nav[0] if nav else action_id
        await _render_and_edit(event, result, current_panel, chat_id, msg_id)
        logger.info("[CALLBACK] _handle_action: COMPLETE for action='%s'", action_id)
    except Exception as exc:
        logger.exception("[CALLBACK] _handle_action: '%s' FAILED: %s", action_id, exc)


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

    clear_pending(owner_id)
    set_pending(
        owner_id, panel_id, handler, chat_id or 0, prompt,
        inline_chat_id=chat_id or 0, inline_msg_id=msg_id or 0,
        extra=extra,
    )

    builder = InlinePanelBuilder()
    builder.add_row("Cancel", f"panel:{panel_id}")
    _add_nav_buttons(builder)

    built = builder.build()
    _sync_timer(chat_id or 0, msg_id or 0, panel_id, prompt, built)
    await _safe_edit(event, prompt, built, chat_id, msg_id)
    logger.info("[CALLBACK] _handle_input: prompt sent for panel='%s'", panel_id)
