"""
Inline Engine — the core of the Inline Mode architecture.

The helper bot answers InlineQuery events by generating panel results.
The self-bot triggers inline mode via client.inline_query(bot_username, query)
and auto-sends the first result.
"""
import logging
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
_owner_id: int = 0


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


def set_owner_id(owner_id: int) -> None:
    global _owner_id
    _owner_id = owner_id


def get_helper_username() -> str:
    return _helper_username


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


async def trigger(self_client, chat_id: int, query: str) -> tuple[bool, int, int]:
    """Trigger inline mode and auto-send the first result.

    Returns (success, chat_id, msg_id). msg_id is 0 on failure.
    """
    if not _helper_username:
        logger.warning("trigger: no helper username set — cannot inline query")
        return False, chat_id, 0

    try:
        results = await self_client.inline_query(_helper_username, query, entity=chat_id)
        if not results:
            logger.warning("trigger: helper returned zero results for query '%s'", query)
            return False, chat_id, 0
        msg = await results[0].click(chat_id)
        if msg is not None:
            msg_id = getattr(msg, "id", 0) or 0
            msg_chat_id = getattr(msg, "chat_id", 0) or chat_id
            inline_msg_id = getattr(msg, "inline_message_id", None)
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
            return True, msg_chat_id, msg_id
        logger.warning("trigger: click() returned None for query '%s'", query)
        return False, chat_id, 0
    except Exception as exc:
        logger.error("trigger: exception for query '%s': %s", query, exc)
        return False, chat_id, 0


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

        try:
            results = await builder(event, extra)
            results = _sanitize_results(results)
            await event.answer(results)
        except Exception:
            logger.exception("Inline router error for panel '%s'", panel_id)
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
