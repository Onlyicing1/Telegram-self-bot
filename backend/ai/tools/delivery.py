"""
Centralized AI Delivery Core — formats and delivers AI responses to Telegram.

Guarantees:
  - Never silently truncates AI responses.
  - Uses a safe internal limit (4000 chars) to stay under Telegram's 4096-char
    message limit while leaving room for formatting wrappers.
  - If the response fits within the safe limit → edit the original message.
  - If the response exceeds the safe limit → split into chunks and deliver
    each chunk separately (first chunk edits the original, rest are sent
    as new messages).
  - Splitting prefers: paragraph → newline → word → character (last resort).
  - Every chunk is guaranteed to be within the safe limit.
  - Delivery failures are reported via the return value, never raised.

Public API:
    deliver_response(event, user_message, trigger_label, response_text) -> DeliveryResult
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any

logger = logging.getLogger(__name__)

SAFE_LIMIT = 4000
_MIN_SPLIT_CHUNK = 100


@dataclass(frozen=True)
class DeliveryResult:
    """Result of a delivery operation."""
    success: bool
    chunks_delivered: int
    total_chunks: int
    error: str = ""


def _format_message(user_message: str, trigger_label: str, response: str) -> str:
    return (
        f"{user_message}\n"
        f"────────────\n"
        f"🤖 {trigger_label}\n"
        f"{response}"
    )


def _format_continuation(response: str, part: int, total: int) -> str:
    return f"{response}\n\n_({part}/{total})_"


def _find_split_point(chunk: str) -> int | None:
    """Find the best split point within ``chunk``.

    Preference: paragraph boundary > newline > word boundary > None.
    Returns the index to split at (relative to start of chunk), or None.
    """
    idx = chunk.rfind("\n\n")
    if idx > _MIN_SPLIT_CHUNK:
        return idx + 2

    idx = chunk.rfind("\n")
    if idx > _MIN_SPLIT_CHUNK:
        return idx + 1

    idx = chunk.rfind(" ")
    if idx > _MIN_SPLIT_CHUNK:
        return idx + 1

    return None


def _split_text(text: str, limit: int) -> list[str]:
    """Split text into chunks of at most ``limit`` characters.

    Splitting preference order:
      1. Paragraph (double newline)
      2. Newline
      3. Word (space)
      4. Character (last resort)

    Every chunk is guaranteed to be <= ``limit`` characters.
    """
    if len(text) <= limit:
        return [text]

    chunks: list[str] = []
    remaining = text

    while remaining:
        if len(remaining) <= limit:
            chunks.append(remaining)
            break

        chunk = remaining[:limit]
        split_at = _find_split_point(chunk)
        if split_at is None or split_at < _MIN_SPLIT_CHUNK:
            split_at = limit

        chunks.append(remaining[:split_at])
        remaining = remaining[split_at:].lstrip("\n")

    return chunks


def _format_chunks(
    user_message: str,
    trigger_label: str,
    response_text: str,
) -> list[str]:
    """Format the full response into one or more deliverable message strings.

    - If the formatted message fits within SAFE_LIMIT, returns a single
      message (edit target).
    - Otherwise, splits the response body and wraps each chunk. The first
      chunk uses the full header (user_message + trigger_label). Subsequent
      chunks use a lightweight continuation header.
    """
    full = _format_message(user_message, trigger_label, response_text)
    if len(full) <= SAFE_LIMIT:
        return [full]

    header = (
        f"{user_message}\n"
        f"────────────\n"
        f"🤖 {trigger_label}\n"
    )
    first_capacity = SAFE_LIMIT - len(header)
    if first_capacity < _MIN_SPLIT_CHUNK:
        first_capacity = _MIN_SPLIT_CHUNK

    body_chunks = _split_text(response_text, first_capacity)

    if len(body_chunks) == 1:
        return _split_text(full, SAFE_LIMIT)

    total = len(body_chunks)
    messages: list[str] = []

    first_msg = f"{header}{body_chunks[0]}"
    messages.append(first_msg)

    for i in range(1, total):
        part_text = body_chunks[i]
        cont = _format_continuation(part_text, i + 1, total)
        if len(cont) > SAFE_LIMIT:
            sub = _split_text(part_text, SAFE_LIMIT - 20)
            for j, s in enumerate(sub):
                if j == 0:
                    messages.append(_format_continuation(s, i + 1, total))
                else:
                    messages.append(s)
        else:
            messages.append(cont)

    return messages


async def deliver_response(
    event: Any,
    user_message: str,
    trigger_label: str,
    response_text: str,
) -> DeliveryResult:
    """Deliver an AI response to Telegram via edit-in-place + follow-up.

    Args:
        event:          The Telethon event (must have ``edit`` and ``reply``).
        user_message:   The original user message text (for the header).
        trigger_label:  The trigger word or "AI" label.
        response_text:  The full AI response text.

    Returns:
        DeliveryResult with success status and chunk counts.
    """
    if not response_text:
        try:
            await event.edit(
                f"{user_message}\n────────────\n🤖 {trigger_label}\n"
                f"❌ Error\nAI returned no response."
            )
        except Exception as exc:
            logger.warning("delivery: empty-response edit failed: %s", exc)
            return DeliveryResult(success=False, chunks_delivered=0, total_chunks=0, error=str(exc))
        return DeliveryResult(success=True, chunks_delivered=1, total_chunks=1)

    messages = _format_chunks(user_message, trigger_label, response_text)
    total = len(messages)
    delivered = 0

    try:
        await event.edit(messages[0])
        delivered += 1
    except Exception as exc:
        logger.warning("delivery: first chunk edit failed: %s", exc)
        try:
            await event.reply(messages[0])
            delivered += 1
        except Exception as exc2:
            logger.error("delivery: first chunk edit AND reply failed: %s", exc2)
            return DeliveryResult(
                success=False,
                chunks_delivered=delivered,
                total_chunks=total,
                error=f"edit failed: {exc}; reply failed: {exc2}",
            )

    for i in range(1, total):
        try:
            await event.reply(messages[i])
            delivered += 1
        except Exception as exc:
            logger.error("delivery: chunk %d/%d reply failed: %s", i + 1, total, exc)
            return DeliveryResult(
                success=False,
                chunks_delivered=delivered,
                total_chunks=total,
                error=f"chunk {i + 1}/{total} reply failed: {exc}",
            )

    return DeliveryResult(success=True, chunks_delivered=delivered, total_chunks=total)
