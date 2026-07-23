"""
Inline Engine — the core of the Inline Mode architecture.

The helper bot answers InlineQuery events by generating panel results.
The self-bot triggers inline mode via ``client.inline_query(bot_username, query)``
and auto-sends the first result.

Flow:
  1. Self-bot command (e.g. ``.help``) fires.
  2. Self-bot calls ``inline_engine.trigger(client, chat_id, query)``.
  3. Helper bot receives InlineQuery, generates results via registered
     inline result builders.
  4. Self-bot auto-sends the first result — message appears as
     ``OwnerName via @HelperBot`` with inline buttons.
  5. Callbacks work exactly as before — no changes to callback architecture.

Inline result builders are registered per "query key". The query string
passed to the helper bot's inline mode encodes which panel to show:
  ``<panel_id>`` or ``<panel_id>:<extra>``

The builder returns a list of InputBotInlineResult objects (usually one).
"""
import asyncio
import logging
import time
import traceback
from typing import Awaitable, Callable, Any

from telethon import events, types
from telethon.tl.custom import Button

from backend.bot.handlers.guard import is_owner
from backend.helper.context import truncate_callback_data

logger = logging.getLogger(__name__)

InlineResultBuilder = Callable[[events.InlineQuery.Event, str], Awaitable[list]]

_builders: dict[str, InlineResultBuilder] = {}
_self_client = None
_helper_client_ref: Any = None
_helper_username: str = ""
_owner_id: int = 0


def _now_ms() -> float:
    return time.monotonic() * 1000.0


def _loop_id() -> int:
    try:
        return id(asyncio.get_running_loop())
    except RuntimeError:
        return 0


def _task_name() -> str:
    try:
        t = asyncio.current_task()
        return t.get_name() if t else "(none)"
    except RuntimeError:
        return "(no-loop)"


def _task_count() -> int:
    try:
        return len(asyncio.all_tasks())
    except RuntimeError:
        return -1


def set_self_client(client) -> None:
    global _self_client
    _self_client = client


def set_helper_client_ref(client) -> None:
    global _helper_client_ref
    _helper_client_ref = client


def set_helper_username(username: str) -> None:
    global _helper_username
    username = username.lstrip("@") if username else ""
    _helper_username = username


def set_owner_id(owner_id: int) -> None:
    global _owner_id
    _owner_id = owner_id


def get_helper_username() -> str:
    return _helper_username


def register_inline_builder(query_key: str, builder: InlineResultBuilder) -> None:
    """Register a builder that returns inline results for a query key."""
    _builders[query_key] = builder
    logger.info("[INLINE] Builder registered: key='%s' (total=%d)", query_key, len(_builders))


def get_inline_builder(query_key: str) -> InlineResultBuilder | None:
    return _builders.get(query_key)


async def trigger(self_client, chat_id: int, query: str) -> bool:
    """Trigger inline mode and auto-send the first result.

    Returns True on success, False on failure.
    """
    t_enter = _now_ms()
    logger.info("[TRACE] trigger ENTER: t=%.1fms, chat_id=%s, query='%s', loop=%d, task='%s', tasks=%d",
                t_enter, chat_id, query, _loop_id(), _task_name(), _task_count())

    # ── Pre-flight: verify helper username ──
    logger.info("[TRACE] trigger pre-flight: helper_username='%s'", _helper_username)
    if not _helper_username:
        logger.error("[TRACE] trigger ABORT: helper username not set — set_helper_username() was never called or get_bot_username() returned empty")
        return False

    # ── Pre-flight: verify helper client is alive ──
    if _helper_client_ref is not None:
        hc_connected = _helper_client_ref.is_connected()
        logger.info("[TRACE] trigger pre-flight: helper_client connected=%s, loop=%d", hc_connected, _loop_id())
        try:
            update_handlers = _helper_client_ref.list_event_handlers()
            logger.info("[TRACE] trigger pre-flight: helper_client event_handlers=%d handlers registered", len(update_handlers))
            for i, (etype, _) in enumerate(update_handlers):
                logger.info("[TRACE] trigger pre-flight: handler[%d] event_type=%s", i, etype)
        except Exception as e:
            logger.warning("[TRACE] trigger pre-flight: failed to list helper event handlers: %s", e)
    else:
        logger.warning("[TRACE] trigger pre-flight: _helper_client_ref is None — set_helper_client_ref() was never called")

    # ── Pre-flight: verify self client is alive ──
    sc_connected = self_client.is_connected() if self_client else False
    logger.info("[TRACE] trigger pre-flight: self_client connected=%s, is_none=%s", sc_connected, self_client is None)

    t_before_iq = _now_ms()
    logger.info("[TRACE] trigger BEFORE inline_query: elapsed=%.1fms, bot='@%s', query='%s'",
                t_before_iq - t_enter, _helper_username, query)

    try:
        results = await self_client.inline_query(_helper_username, query)
        t_after_iq = _now_ms()
        logger.info("[TRACE] trigger AFTER inline_query: elapsed=%.1fms, type=%s, results_count=%d",
                    t_after_iq - t_enter,
                    type(results).__name__, len(results) if results else 0)

        if results:
            logger.info("[TRACE] trigger results: count=%d, result[0]_type=%s, result[0]_id=%s",
                        len(results), type(results[0]).__name__, getattr(results[0], 'result', None))

            t_before_click = _now_ms()
            logger.info("[TRACE] trigger BEFORE click: elapsed=%.1fms, chat_id=%s",
                        t_before_click - t_enter, chat_id)
            try:
                sent_msg = await results[0].click(chat_id)
                t_after_click = _now_ms()
                logger.info("[TRACE] trigger AFTER click: elapsed=%.1fms, sent_msg=%s",
                            t_after_click - t_enter, sent_msg)
                return True
            except Exception as click_exc:
                t_after_click = _now_ms()
                logger.error("[TRACE] trigger click FAILED: elapsed=%.1fms, exc=%s",
                             t_after_click - t_enter, click_exc)
                logger.exception("[TRACE] trigger click exception traceback:")
                return False
        else:
            t_zero = _now_ms()
            logger.error("[TRACE] trigger ZERO RESULTS: elapsed=%.1fms, query='%s', helper='@%s' — helper bot InlineQuery handler did not answer or returned empty list",
                         t_zero - t_enter, query, _helper_username)
            return False
    except Exception as exc:
        t_exc = _now_ms()
        logger.error("[TRACE] trigger inline_query EXCEPTION: elapsed=%.1fms, exc_type=%s, exc=%s",
                     t_exc - t_enter, type(exc).__name__, exc)
        logger.exception("[TRACE] trigger inline_query exception traceback:")
        return False


def register_inline_handler(helper_client, owner_id: int) -> None:
    """Wire the InlineQuery handler onto the helper bot client."""
    set_helper_client_ref(helper_client)

    # ── Canary: register a Raw handler to prove the helper bot's update loop is alive ──
    from telethon import events as _events
    from telethon.tl import types as _tl_types

    @helper_client.on(_events.Raw())
    async def _raw_canary(update):
        # Only log InlineQuery-related raw updates to avoid noise
        if isinstance(update, (_tl_types.UpdateBotInlineQuery, _tl_types.UpdateBotInlineSend)):
            logger.info("[TRACE] _raw_canary FIRED: type=%s, loop=%d, task='%s'",
                        type(update).__name__, _loop_id(), _task_name())
        # Log ALL raw updates for forensic purposes (comment out to reduce noise)
        logger.info("[TRACE] _raw_canary: raw update type=%s", type(update).__name__)

    logger.info("[TRACE] register_inline_handler: Raw canary registered on helper_client")

    @helper_client.on(events.InlineQuery())
    async def _inline_router(event):
        t_enter = _now_ms()
        logger.info("[TRACE] _inline_router ENTER: t=%.1fms, query='%s', user_id=%s, loop=%d, task='%s', tasks=%d",
                    t_enter, event.query, event.sender_id, _loop_id(), _task_name(), _task_count())

        # ── Owner check ──
        t_before_owner = _now_ms()
        owner_ok = is_owner(event, owner_id)
        t_after_owner = _now_ms()
        if not owner_ok:
            logger.warning("[TRACE] _inline_router owner check FAILED: elapsed=%.1fms, sender_id=%s, owner_id=%s",
                           t_after_owner - t_enter, event.sender_id, owner_id)
            try:
                await event.answer([])
            except Exception:
                logger.exception("[TRACE] _inline_router: failed to answer empty results on owner check fail")
            return

        logger.info("[TRACE] _inline_router owner check PASS: elapsed=%.1fms", t_after_owner - t_enter)

        # ── Parse query ──
        raw_query = event.query.strip()
        if not raw_query:
            logger.warning("[TRACE] _inline_router: empty query — answering with empty list")
            try:
                await event.answer([])
            except Exception:
                logger.exception("[TRACE] _inline_router: failed to answer empty results on empty query")
            return

        parts = raw_query.split(":", 1)
        panel_id = parts[0]
        extra = parts[1] if len(parts) > 1 else ""

        logger.info("[TRACE] _inline_router parsed: panel_id='%s', extra='%s'", panel_id, extra)

        # ── Builder lookup ──
        builder = get_inline_builder(panel_id)
        if builder is None:
            logger.error("[TRACE] _inline_router: NO BUILDER for panel_id='%s' (registered: %s) — register_inline_builder() was never called for this key",
                         panel_id, list(_builders.keys()))
            try:
                await event.answer([])
            except Exception:
                logger.exception("[TRACE] _inline_router: failed to answer empty results on missing builder")
            return

        logger.info("[TRACE] _inline_router: builder found for '%s' — invoking", panel_id)

        # ── Builder execution ──
        t_before_build = _now_ms()
        logger.info("[TRACE] _inline_router BEFORE builder: elapsed=%.1fms, panel_id='%s'",
                    t_before_build - t_enter, panel_id)
        try:
            results = await builder(event, extra)
            t_after_build = _now_ms()
            logger.info("[TRACE] _inline_router AFTER builder: elapsed=%.1fms, results_count=%d",
                        t_after_build - t_enter, len(results) if results else 0)

            if not results:
                logger.warning("[TRACE] _inline_router: builder returned empty list for panel_id='%s'", panel_id)

            # ── Answer inline query ──
            t_before_answer = _now_ms()
            logger.info("[TRACE] _inline_router BEFORE event.answer: elapsed=%.1fms, results_count=%d",
                        t_before_answer - t_enter, len(results) if results else 0)
            await event.answer(results)
            t_after_answer = _now_ms()
            logger.info("[TRACE] _inline_router AFTER event.answer: elapsed=%.1fms",
                        t_after_answer - t_enter)
        except Exception as exc:
            t_err = _now_ms()
            logger.error("[TRACE] _inline_router builder/answer EXCEPTION: elapsed=%.1fms, exc_type=%s, exc=%s",
                         t_err - t_enter, type(exc).__name__, exc)
            logger.exception("[TRACE] _inline_router builder/answer traceback:")
            try:
                await event.answer([])
            except Exception:
                logger.exception("[TRACE] _inline_router: failed to answer empty results on builder error")


def make_result(
    title: str,
    description: str = "",
    panel_id: str = "",
    extra: str = "",
    buttons: list | None = None,
    query_id: int = 0,
) -> types.InputBotInlineResult:
    """Build a single InputBotInlineResult with a text message and buttons.

    The result body is a InputBotInlineMessageTextAuto (auto-detected type).
    """
    body_text = title
    if description:
        body_text = f"{title}\n\n{description}"

    if buttons is None:
        buttons = []

    msg = types.InputBotInlineMessageTextAuto(
        message=body_text,
        reply_markup=types.ReplyInlineMarkup(rows=buttons) if buttons else None,
    )

    return types.InputBotInlineResult(
        id="0",
        type="article",
        title=title.split("\n")[0][:255] if title else "LifeOS",
        send_message=msg,
    )


def make_button_rows(buttons_data: list[list[tuple[str, str]]]) -> list:
    """Convert a list of button rows into ReplyInlineMarkup rows.

    Each row is a list of (text, callback_data) tuples.
    """
    rows = []
    for row_data in buttons_data:
        row_buttons = []
        for text, data in row_data:
            row_buttons.append(
                types.KeyboardButtonCallback(
                    text=text,
                    data=truncate_callback_data(data).encode("utf-8"),
                )
            )
        rows.append(row_buttons)
    return rows
