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
from backend.helper import trace_collector

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
    trace_collector.trace("TRIGGER ENTER (inline_engine)")

    if not _helper_username:
        trace_collector.trace("TRIGGER ABORT: helper_username not set")
        return False

    if _helper_client_ref is not None:
        try:
            update_handlers = _helper_client_ref.list_event_handlers()
            trace_collector.trace(f"HELPER HANDLERS: {len(update_handlers)}")
        except Exception:
            pass
    else:
        trace_collector.trace("HELPER CLIENT REF is None")

    sc_connected = self_client.is_connected() if self_client else False
    trace_collector.trace(f"SELF CLIENT connected={sc_connected}")

    trace_collector.trace("BEFORE INLINE_QUERY")
    try:
        results = await self_client.inline_query(_helper_username, query)
        trace_collector.trace(f"AFTER INLINE_QUERY: results={len(results) if results else 0}")

        if results:
            trace_collector.trace("BEFORE CLICK")
            try:
                sent_msg = await results[0].click(chat_id)
                trace_collector.trace("AFTER CLICK: success")
                return True
            except Exception as click_exc:
                trace_collector.trace(f"CLICK EXCEPTION: {type(click_exc).__name__}: {click_exc}")
                return False
        else:
            trace_collector.trace("ZERO RESULTS from inline_query")
            return False
    except Exception as exc:
        trace_collector.trace(f"INLINE_QUERY EXCEPTION: {type(exc).__name__}: {exc}")
        return False


def register_inline_handler(helper_client, owner_id: int) -> None:
    """Wire the InlineQuery handler onto the helper bot client."""
    set_helper_client_ref(helper_client)

    @helper_client.on(events.InlineQuery())
    async def _inline_router(event):
        t_enter = _now_ms()
        trace_collector.trace("INLINE_ROUTER ENTER")

        # ── Owner check ──
        owner_ok = is_owner(event, owner_id)
        if not owner_ok:
            trace_collector.trace("INLINE_ROUTER: owner check FAILED")
            try:
                await event.answer([])
            except Exception:
                pass
            return

        trace_collector.trace("INLINE_ROUTER: owner check PASS")

        # ── Parse query ──
        trace_collector.trace("BEFORE PARSE_QUERY")
        raw_query = event.query.strip()
        if not raw_query:
            trace_collector.trace("AFTER PARSE_QUERY: empty query")
            try:
                await event.answer([])
            except Exception:
                pass
            return

        parts = raw_query.split(":", 1)
        panel_id = parts[0]
        extra = parts[1] if len(parts) > 1 else ""
        trace_collector.trace(f"AFTER PARSE_QUERY: panel_id='{panel_id}', extra='{extra}'")

        # ── Builder lookup ──
        trace_collector.trace("BEFORE BUILDER LOOKUP")
        builder = get_inline_builder(panel_id)
        builder_name = getattr(builder, '__name__', str(builder)) if builder else None
        if builder is None:
            trace_collector.trace(f"AFTER BUILDER LOOKUP: NO BUILDER for '{panel_id}' (registered: {list(_builders.keys())})")
            try:
                await event.answer([])
            except Exception:
                pass
            return
        trace_collector.trace(f"AFTER BUILDER LOOKUP: found builder='{builder_name}' for '{panel_id}'")

        # ── Builder execution ──
        trace_collector.trace(f"BEFORE BUILDER(...) call: builder='{builder_name}'")
        try:
            results = await builder(event, extra)
            result_count = len(results) if results else 0
            result_type = type(results).__name__ if results else "None"
            trace_collector.trace(f"AFTER BUILDER(...): result_type={result_type}, result_count={result_count}")

            if not results:
                trace_collector.trace("BUILDER returned empty list")

            # ── Answer inline query ──
            trace_collector.trace(f"BEFORE EVENT.ANSWER: results_count={result_count}")
            await event.answer(results)
            trace_collector.trace("AFTER EVENT.ANSWER: success")

        except Exception as exc:
            tb = traceback.extract_tb(exc.__traceback__)
            tb_location = f"{tb[-1].filename}:{tb[-1].lineno} in {tb[-1].name}" if tb else "unknown"
            trace_collector.trace(f"INLINE_ROUTER EXCEPTION: type={type(exc).__name__}")
            trace_collector.trace(f"INLINE_ROUTER EXCEPTION: message={exc}")
            trace_collector.trace(f"INLINE_ROUTER EXCEPTION: traceback_location={tb_location}")
            if _self_client is not None:
                await trace_collector.flush_to_saved_messages(_self_client)
            try:
                await event.answer([])
            except Exception:
                pass


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
