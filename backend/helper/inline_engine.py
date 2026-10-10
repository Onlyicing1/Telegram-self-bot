"""
Inline Engine — the core of the Inline Mode architecture.

The helper bot answers InlineQuery events by generating panel results.
The self-bot triggers inline mode via client.inline_query(bot_username, query)
and auto-sends the first result.
"""
import logging
import time
from typing import Awaitable, Callable, Any

from telethon import events, types

from backend.bot.handlers.guard import is_owner
from backend.helper.context import truncate_callback_data
from backend.helper.panel_render import _to_inline_rows, _normalize_row

logger = logging.getLogger(__name__)

InlineResultBuilder = Callable[[events.InlineQuery.Event, str], Awaitable[list]]

_builders: dict[str, InlineResultBuilder] = {}
_self_client = None
_helper_client_ref: Any = None
_helper_username: str = ""
_helper_id: int = 0
_owner_id: int = 0

#: The honest "the bot answered, but with nothing" reason. Exposed so a caller
#: can tell an empty answer apart from a failed query WITHOUT matching prose.
INLINE_ZERO_RESULTS_REASON = "the helper returned zero results for this query"


def set_self_client(client) -> None:
    global _self_client
    _self_client = client


def get_self_client():
    """Return the self-bot client (set during startup)."""
    return _self_client


def set_helper_client_ref(client) -> None:
    global _helper_client_ref
    _helper_client_ref = client


def set_helper_username(username: str) -> None:
    global _helper_username
    username = username.lstrip("@") if username else ""
    _helper_username = username
    if username:
        logger.info("[HELPER] username set to @%s", username)
    else:
        logger.warning("[HELPER] username set to empty — inline mode will fail")


def set_helper_id(helper_id: int) -> None:
    global _helper_id
    _helper_id = helper_id or 0
    if helper_id:
        logger.info("[HELPER] id set to %s", helper_id)


def set_owner_id(owner_id: int) -> None:
    global _owner_id
    _owner_id = owner_id


def get_helper_username() -> str:
    return _helper_username


def get_helper_id() -> int:
    return _helper_id


def get_owner_id() -> int:
    return _owner_id


def _to_keyboard_button_rows(rows: list) -> list:
    """Convert any button rows (tuples OR TLObjects) into KeyboardButtonRow TLObjects."""
    return _to_inline_rows(rows) if rows else []


def _sanitize_results(results: list) -> list:
    """Ensure every result and its reply_markup contain only valid TLObjects."""
    for r in results:
        msg = getattr(r, "send_message", None)
        if msg is None:
            continue
        rm = getattr(msg, "reply_markup", None)
        if rm is not None and hasattr(rm, "rows") and rm.rows:
            rm.rows = _to_keyboard_button_rows(rm.rows)
    return results


def register_inline_builder(query_key: str, builder: InlineResultBuilder) -> None:
    _builders[query_key] = builder


def get_inline_builder(query_key: str) -> InlineResultBuilder | None:
    return _builders.get(query_key)


#: The helper bot's OWN record of the last answer it submitted, per query key.
#:
#: ``event.answer`` returns only a boolean, so without this the self-side flow
#: can see WHAT Telegram stored (``getInlineBotResults``) but never WHAT the bot
#: actually handed over (``setInlineBotResults``) — and those are two different
#: facts. Bounded by construction: a key is only ever recorded for a REGISTERED
#: builder, so this mapping cannot grow with query input, and an entry holds a
#: handful of scalars.
_last_answers: dict[str, dict[str, Any]] = {}


def _empty_answer_record() -> dict[str, Any]:
    """The "this bot submitted nothing we know of" record — every key present."""
    return {
        "recorded": False,
        "at": None,
        "ok": None,
        "error": "",
        "result_count": None,
        "custom_emoji_count": None,
        "document_ids": [],
    }


def _count_custom_emoji(results: Any) -> tuple[int, list[int]]:
    """The custom-emoji ENTITIES one answer carries — never a visible glyph."""
    count = 0
    document_ids: list[int] = []
    for result in results or []:
        message = getattr(result, "send_message", None)
        for entity in getattr(message, "entities", None) or []:
            document_id = getattr(entity, "document_id", None)
            if not isinstance(document_id, int) or isinstance(document_id, bool):
                continue
            count += 1
            if len(document_ids) < 32:
                document_ids.append(document_id)
    return count, document_ids


def record_inline_answer(
    query_key: str, results: Any, *, ok: bool, error: str = ""
) -> None:
    """Record what THIS bot's answer to one inline query carried.

    Called for a registered query key right around ``event.answer``: ``ok``
    states whether the submission completed without raising, and the counts
    describe the payload the bot built (not what Telegram kept).
    """
    if not isinstance(query_key, str) or not query_key:
        return
    count, document_ids = _count_custom_emoji(results)
    _last_answers[query_key] = {
        "at": time.monotonic(),
        "ok": bool(ok),
        "error": error or "",
        "result_count": len(results or []),
        "custom_emoji_count": count,
        "document_ids": document_ids,
    }


def last_inline_answer(query_key: str, *, since: float | None = None) -> dict[str, Any]:
    """The recorded answer for ONE query key — every key present.

    ``since`` is a monotonic timestamp taken BEFORE the query was issued, so a
    caller that needs evidence for ONE attempt does not read an answer that
    belongs to an earlier one: anything recorded before that moment is reported
    as not recorded.
    """
    record = _last_answers.get(query_key)
    if not isinstance(record, dict):
        return _empty_answer_record()
    if since is not None and record.get("at", 0) < since:
        return _empty_answer_record()
    return {"recorded": True, **record}


def inline_unavailable_reason() -> str:
    """Why the inline query cannot be issued at all — ``""`` means it can.

    The same three-way diagnosis :func:`trigger` has always logged, exposed
    as a string so a caller that drives the two halves itself reports the
    SAME honest reason instead of a generic failure.
    """
    if _helper_username:
        return ""
    from backend.helper import client as helper_client_mod
    if not helper_client_mod.is_available():
        return "the helper bot is not connected"
    if not _helper_id:
        return (
            "the helper username is empty and the helper id is 0 — GetMe likely "
            "failed during helper startup"
        )
    return (
        f"the helper account has no public username (id={_helper_id}) — inline "
        "mode requires a @username set via BotFather or Telegram settings"
    )


async def query_results(self_client, chat_id: int, query: str) -> tuple[Any, str]:
    """The ``getInlineBotResults`` half of :func:`trigger`.

    Returns ``(results, "")`` — the server's stored results for this query —
    or ``(None, honest_reason)``. Exposed so a caller that must inspect what
    Telegram actually kept in the returned result (before the user sends it)
    drives the SAME query the panels drive, instead of a second inline path.
    """
    reason = inline_unavailable_reason()
    if reason:
        if not _helper_username:
            logger.error("[PANEL] trigger: cannot start inline — %s", reason)
        return None, reason
    try:
        results = await self_client.inline_query(_helper_username, query, entity=chat_id)
    except Exception as exc:
        logger.error("trigger: exception for query '%s': %s", query, exc)
        return None, f"the inline query raised {type(exc).__name__}: {exc}"
    if not results:
        logger.warning("trigger: helper returned zero results for query '%s'", query)
        return None, INLINE_ZERO_RESULTS_REASON
    return results, ""


async def click_result(self_client, chat_id: int, result) -> tuple[Any, str]:
    """The ``sendInlineBotResult`` half of :func:`trigger`.

    Sends ONE already-fetched result (the user account is the sender and
    Telegram stamps ``via_bot_id``). Returns ``(message, "")`` or
    ``(None, honest_reason)``.
    """
    try:
        msg = await result.click(chat_id)
    except Exception as exc:
        logger.error("trigger: exception for click: %s", exc)
        return None, f"the inline send raised {type(exc).__name__}: {exc}"
    if msg is None:
        logger.warning("trigger: click() returned None")
        return None, "the inline send returned no message"
    return msg, ""


async def trigger(self_client, chat_id: int, query: str) -> tuple[bool, int, int, str]:
    """Trigger inline mode and auto-send the first result.

    Returns (success, chat_id, msg_id, inline_message_id).
    msg_id is 0 on failure. inline_message_id is "" when not applicable.
    """
    results, reason = await query_results(self_client, chat_id, query)
    if results is None:
        return False, chat_id, 0, ""
    msg, reason = await click_result(self_client, chat_id, results[0])
    if msg is None:
        return False, chat_id, 0, ""
    msg_id = getattr(msg, "id", 0) or 0
    msg_chat_id = getattr(msg, "chat_id", 0) or chat_id
    inline_msg_id = getattr(msg, "inline_message_id", None) or ""
    peer_id = None
    try:
        peer = getattr(msg, "peer_id", None)
        if peer is not None:
            peer_id = str(peer)
    except Exception:
        pass
    logger.info(
        "[PANEL] TRIGGER RESULT query='%s' click_chat_id=%s "
        "msg_chat_id=%s msg_id=%s entity_chat_id=%s "
        "inline_message_id=%s peer_id=%s",
        query, chat_id,
        msg_chat_id, msg_id, chat_id,
        inline_msg_id, peer_id,
    )
    if not msg_id:
        logger.warning("trigger: click() returned message with id=0")
    return True, msg_chat_id, msg_id, inline_msg_id


def register_inline_handler(helper_client, owner_id: int) -> None:
    """Wire the InlineQuery handler onto the helper bot client."""
    set_helper_client_ref(helper_client)

    @helper_client.on(events.InlineQuery())
    async def _inline_router(event):
        if not is_owner(event, owner_id):
            try:
                await event.answer([])
            except Exception:
                pass
            return

        raw_query = event.text.strip()
        if not raw_query:
            try:
                await event.answer([])
            except Exception:
                pass
            return

        parts = raw_query.split(":", 1)
        panel_id = parts[0]
        extra = parts[1] if len(parts) > 1 else ""

        builder = get_inline_builder(panel_id)
        if builder is None:
            try:
                await event.answer([])
            except Exception:
                pass
            return

        built: list = []
        try:
            built = await builder(event, extra)
            built = _sanitize_results(built)
            await event.answer(built)
            record_inline_answer(panel_id, built, ok=True)
        except Exception as exc:
            logger.exception("Inline router error for panel '%s'", panel_id)
            record_inline_answer(
                panel_id,
                built,
                ok=False,
                error=f"the answer raised {type(exc).__name__}: {exc}",
            )
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
    """Build a single InputBotInlineResult. Accepts tuples OR Button objects."""
    body_text = title
    if description:
        body_text = f"{title}\n\n{description}"

    if buttons is None:
        buttons = []

    msg = types.InputBotInlineMessageText(
        message=body_text,
        reply_markup=types.ReplyInlineMarkup(rows=_to_keyboard_button_rows(buttons)) if buttons else None,
    )

    return types.InputBotInlineResult(
        id="0",
        type="article",
        title=title.split("\n")[0][:255] if title else "LifeOS",
        send_message=msg,
    )


def make_button_rows(buttons_data: list) -> list:
    """Convert any button layout into KeyboardButtonRow TLObjects."""
    return _to_keyboard_button_rows(buttons_data)
