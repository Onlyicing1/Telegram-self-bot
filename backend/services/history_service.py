"""
Telegram History Service — the reusable, bounded, provenance-aware history layer.

This is the shared capability for retrieving REAL Telegram conversation history
on explicit request ("translate the last 500 messages", "summarize the last
1000 messages", "give me messages 100 through 300"). It is deliberately NOT the
bounded request-scoped snapshot in ``backend/ai/conversation/telegram_context.py``:
that snapshot stays anchored to the triggering message, 10 messages wide, 3s
long, and lossy by design, because it only ever enriches one ordinary AI turn.

Layering rules this module exists to enforce:

  * Telegram access goes exclusively through the existing
    ``backend/telegram_api`` facade. This module never imports Telethon and
    never calls ``client.iter_messages`` directly; both a ``TelegramAPI``
    instance and the client it wraps are accepted and normalized to the same
    facade call.
  * Every page fetch is bounded by ``rpc_await`` — the same primitive
    ``backend/services/delete_service.py`` uses for its bounded large scans —
    so a stalled RPC can never hang a caller.
  * Provenance eligibility is owned HERE, centrally, by reusing the durable
    marker helpers in ``backend/ai/context/provenance.py``: a message the AI
    answered or overwrote in place is NOT human conversation history. No AI
    tool may re-implement this predicate.
  * Message text is returned losslessly (no per-message truncation) because
    translation and summarization need the full content. Bounding happens in
    the caller's chunking step, never by silently dropping text.
  * No prompts, no providers, no translation or summarization logic. This layer
    retrieves and normalizes eligible history; it knows nothing about an LLM.

Failure contract: retrieval errors are raised as ``HistoryError``. Unlike the
optional surrounding-context snapshot — which degrades to an empty context —
an explicit history request must never be silently answered with nothing.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import AsyncIterator
from dataclasses import dataclass
from datetime import datetime
from typing import Any

from backend.ai.context.provenance import (
    has_ai_provenance_marker,
    strip_ai_provenance_marker,
)
from backend.helper.rpc_timeout import rpc_await
from backend.telegram_api.messages import iter_messages as _facade_iter_messages

logger = logging.getLogger(__name__)

#: Upper bound on eligible messages a single request may return. Mirrors the
#: only large-scan bound already established in the codebase
#: (``delete_service._MAX_DELETE_SCAN_MESSAGES``).
MAX_HISTORY_MESSAGES = 1000
#: One Telegram page. Mirrors the facade's own default page size.
DEFAULT_PAGE_SIZE = 100
#: Hard ceiling for a caller-supplied page size.
MAX_PAGE_SIZE = 1000
#: Raw Telegram messages examined for one request, regardless of how many of
#: them turn out to be eligible. Bounds the cost of a request whose window is
#: mostly AI-produced messages.
MAX_HISTORY_SCAN_MESSAGES = 5000
#: Bounded timeout for one page fetch (services-layer precedent).
HISTORY_RPC_TIMEOUT_S = 5.0


class HistoryError(Exception):
    """Raised when Telegram history cannot be retrieved honestly."""


@dataclass(frozen=True)
class HistoryMessage:
    """One normalized, provenance-classified Telegram message.

    ``text`` is the full message text with the invisible AI provenance marker
    removed — the marker is metadata, never content, and never reaches a
    consumer as text. ``ai_provenance`` records that the marker was present, so
    an opted-in consumer can still tell the message apart without re-parsing
    it.

    Attributes:
        message_id:       Real Telegram message ID (always > 0 for a normalized
                          record).
        chat_id:          Chat the message belongs to.
        sender_id:        Telegram sender id (0 when Telegram did not supply it).
        out:              True when the owner's own account sent it.
        date:             Telegram send time, or ``None``.
        text:             Full message text, marker-stripped, never truncated.
        reply_to_msg_id:  Replied-to message id, or ``None``.
        has_media:        True when the message carries media. Media is never
                          downloaded by this layer.
        ai_provenance:    True when the durable AI provenance marker was present.
    """

    message_id: int
    chat_id: int
    sender_id: int
    out: bool
    date: datetime | None
    text: str
    reply_to_msg_id: int | None
    has_media: bool
    ai_provenance: bool


@dataclass(frozen=True)
class HistoryPage:
    """One bounded chunk of eligible history, chronological (oldest → newest).

    Attributes:
        chat_id:   Chat the page was read from.
        messages:  The chunk's eligible messages, oldest first.
        has_more:  True when something follows this page — either another page
                   of the same request, or older eligible history beyond the
                   request's count bound.
    """

    chat_id: int
    messages: tuple[HistoryMessage, ...]
    has_more: bool


@dataclass(frozen=True)
class HistorySlice:
    """A complete bounded history request: up to N eligible messages.

    Attributes:
        chat_id:    Chat the history was read from.
        messages:   Eligible messages, chronological (oldest → newest).
        requested:  The count actually pursued (clamped to
                    ``MAX_HISTORY_MESSAGES``; 0 when nothing was requested).
        scanned:    Raw Telegram messages examined to satisfy the request.
        truncated:  True when the request stopped at a bound while older
                    eligible messages were still available. Callers must report
                    this honestly rather than presenting a partial result as the
                    whole history.
    """

    chat_id: int
    messages: tuple[HistoryMessage, ...]
    requested: int
    scanned: int
    truncated: bool


def _coerce_int(value: Any) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return 0


def _resolve_client(source: Any) -> Any:
    """Normalize a ``TelegramAPI`` facade or a raw client to the client.

    The facade's ``client`` property is documented as service-layer use only,
    which is exactly what this module is.
    """
    if source is None:
        raise HistoryError("No Telegram client available for history retrieval.")
    wrapped = getattr(source, "client", None)
    if wrapped is not None and hasattr(wrapped, "iter_messages"):
        return wrapped
    if hasattr(source, "iter_messages"):
        return source
    raise HistoryError("Unsupported Telegram source for history retrieval.")


def _require_chat_id(chat_id: Any) -> int:
    chat = _coerce_int(chat_id)
    if chat == 0:
        raise HistoryError("A concrete chat id is required for history retrieval.")
    return chat


def _normalize(raw: dict[str, Any], chat_id: int) -> HistoryMessage:
    text = raw.get("text")
    text = text if isinstance(text, str) else ""
    marked = has_ai_provenance_marker(text)
    reply_to = raw.get("reply_to_msg_id")
    return HistoryMessage(
        message_id=_coerce_int(raw.get("id")),
        chat_id=_coerce_int(raw.get("chat_id")) or chat_id,
        sender_id=_coerce_int(raw.get("sender_id")),
        out=bool(raw.get("out", False)),
        date=raw.get("date") if isinstance(raw.get("date"), datetime) else None,
        text=strip_ai_provenance_marker(text),
        reply_to_msg_id=_coerce_int(reply_to) if reply_to is not None else None,
        has_media=bool(raw.get("has_media", False)),
        ai_provenance=marked,
    )


async def _fetch_page(
    client: Any,
    chat_id: int,
    *,
    limit: int,
    before_id: int | None,
    after_id: int | None,
) -> list[dict[str, Any]]:
    """One bounded Telegram page through the facade. Never silently empty."""
    try:
        return await rpc_await(
            _facade_iter_messages(
                client,
                chat_id,
                limit=limit,
                min_id=after_id,
                max_id=before_id,
            ),
            timeout=HISTORY_RPC_TIMEOUT_S,
            label="history.iter_messages",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.error(
            "HISTORY_FETCH_FAILED chat_id=%s limit=%s before_id=%s after_id=%s error=%r",
            chat_id, limit, before_id, after_id, exc,
        )
        raise HistoryError(
            f"Telegram history fetch failed for chat {chat_id}: {exc}"
        ) from exc


async def _collect(
    source: Any,
    chat_id: int,
    *,
    target: int,
    page_size: int,
    before_id: int | None,
    after_id: int | None,
    include_ai: bool,
) -> tuple[list[HistoryMessage], int, bool]:
    """Scan backwards page by page until ``target`` eligible messages exist.

    Returns ``(messages oldest → newest, raw_scanned, truncated)``.

    Determinism: pages are fetched with a strictly decreasing ``max_id``
    cursor, every page is sorted by message id, duplicates are impossible
    because the cursor is exclusive, and the result is always returned in
    chronological order regardless of the order Telegram returned.
    """
    client = _resolve_client(source)
    # Newest → oldest while scanning; reversed once at the end.
    collected: list[HistoryMessage] = []
    scanned = 0
    cursor_max = before_id
    exhausted = False
    bound_hit = False

    while len(collected) < target:
        remaining = MAX_HISTORY_SCAN_MESSAGES - scanned
        if remaining <= 0:
            bound_hit = True
            break
        page_limit = min(page_size, remaining)
        raw = await _fetch_page(
            client, chat_id, limit=page_limit, before_id=cursor_max, after_id=after_id,
        )
        if not raw:
            exhausted = True
            break
        scanned += len(raw)
        raw_ids = [mid for mid in (_coerce_int(d.get("id")) for d in raw) if mid > 0]
        if not raw_ids:
            exhausted = True
            break
        for record in (_normalize(d, chat_id) for d in raw):
            if record.message_id <= 0:
                continue
            if record.ai_provenance and not include_ai:
                # Central provenance eligibility: AI answers/overwrites are not
                # human conversation history.
                continue
            collected.append(record)
        collected.sort(key=lambda m: m.message_id, reverse=True)
        if len(raw) < page_limit:
            # Telegram had nothing older to give: the window is exhausted.
            exhausted = True
            break
        next_cursor = min(raw_ids)
        if cursor_max is not None and next_cursor >= cursor_max:
            # Defensive: a cursor that does not move would loop forever.
            bound_hit = True
            break
        cursor_max = next_cursor

    # Newest-first list: the newest ``target`` eligible messages are kept. Any
    # eligible message dropped here is proof that older history exists.
    trimmed = len(collected) > target
    if trimmed:
        collected = collected[:target]

    truncated = bound_hit or trimmed or not exhausted
    return list(reversed(collected)), scanned, truncated


async def fetch_recent_history(
    source: Any,
    chat_id: Any,
    *,
    count: int | None,
    page_size: int = DEFAULT_PAGE_SIZE,
    before_id: int | None = None,
    after_id: int | None = None,
    include_ai: bool = False,
) -> HistorySlice:
    """Retrieve up to ``count`` eligible messages, oldest → newest.

    Args:
        source:     A ``TelegramAPI`` facade or the client it wraps.
        chat_id:    Chat to read.
        count:      Number of eligible messages wanted. ``None`` means "as many
                    as the bound allows" (``MAX_HISTORY_MESSAGES``). A value of
                    0 or less returns an empty slice without touching Telegram.
        page_size:  Telegram page size (clamped to ``MAX_PAGE_SIZE``).
        before_id:  Exclusive upper bound — read only strictly older messages.
        after_id:   Exclusive lower bound — read only strictly newer messages.
        include_ai: Opt in to AI-provenance-bearing messages. They keep
                    ``ai_provenance=True`` and their text stays marker-free, so
                    a caller can still distinguish them.

    Raises:
        HistoryError: retrieval failed, timed out, or the chat id was unusable.
    """
    chat = _require_chat_id(chat_id)
    target = MAX_HISTORY_MESSAGES if count is None else _coerce_int(count)
    if target <= 0:
        return HistorySlice(
            chat_id=chat, messages=(), requested=0, scanned=0, truncated=False,
        )
    target = min(target, MAX_HISTORY_MESSAGES)
    size = _coerce_page_size(page_size)

    messages, scanned, truncated = await _collect(
        source,
        chat,
        target=target,
        page_size=size,
        before_id=before_id,
        after_id=after_id,
        include_ai=include_ai,
    )
    return HistorySlice(
        chat_id=chat,
        messages=tuple(messages),
        requested=target,
        scanned=scanned,
        truncated=truncated,
    )


async def fetch_history_page(
    source: Any,
    chat_id: Any,
    *,
    limit: int = DEFAULT_PAGE_SIZE,
    before_id: int | None = None,
    after_id: int | None = None,
    include_ai: bool = False,
) -> HistoryPage:
    """Retrieve ONE bounded page of eligible history, oldest → newest.

    This is the low-level cursor API for callers that want to walk history
    themselves: pass the oldest message id of the previous page as
    ``before_id`` to continue, and stop when ``has_more`` is False.
    """
    chat = _require_chat_id(chat_id)
    size = _coerce_page_size(limit)
    messages, _scanned, truncated = await _collect(
        source,
        chat,
        target=size,
        page_size=size,
        before_id=before_id,
        after_id=after_id,
        include_ai=include_ai,
    )
    return HistoryPage(chat_id=chat, messages=tuple(messages), has_more=truncated)


async def iter_history_pages(
    source: Any,
    chat_id: Any,
    *,
    count: int | None,
    page_size: int = DEFAULT_PAGE_SIZE,
    before_id: int | None = None,
    after_id: int | None = None,
    include_ai: bool = False,
) -> AsyncIterator[HistoryPage]:
    """Yield eligible history as bounded pages, oldest page first.

    The requested count is satisfied first (honestly bounded by
    ``MAX_HISTORY_MESSAGES`` and ``MAX_HISTORY_SCAN_MESSAGES``), then split into
    chunks of at most ``page_size`` messages. Consecutive pages never overlap
    and together are exactly chronological: a consumer that processes each page
    in order processes the conversation in order.

    The remote scan is bounded, so at most ``MAX_HISTORY_MESSAGES`` normalized
    records are ever held at once. ``has_more`` is True on every page that is
    followed by another page, and on the final page it reports whether older
    eligible history exists beyond the request.
    """
    chat = _require_chat_id(chat_id)
    target = MAX_HISTORY_MESSAGES if count is None else _coerce_int(count)
    if target <= 0:
        return
    target = min(target, MAX_HISTORY_MESSAGES)
    size = _coerce_page_size(page_size)

    messages, _scanned, truncated = await _collect(
        source,
        chat,
        target=target,
        page_size=size,
        before_id=before_id,
        after_id=after_id,
        include_ai=include_ai,
    )
    if not messages:
        return
    for start in range(0, len(messages), size):
        chunk = messages[start:start + size]
        is_last = start + size >= len(messages)
        yield HistoryPage(
            chat_id=chat,
            messages=tuple(chunk),
            has_more=truncated if is_last else True,
        )


def _coerce_page_size(value: Any) -> int:
    size = _coerce_int(value)
    if size <= 0:
        return DEFAULT_PAGE_SIZE
    return min(size, MAX_PAGE_SIZE)
