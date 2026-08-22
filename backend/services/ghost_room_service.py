"""
Ghost Room Service — discovery, state, message fetch, and AI dispatch.

Owns the in-memory selection registry and pagination state.
Persistence layer delegates to the existing ``db/client.py`` helpers.
The service never calls providers or dispatches AI directly —
it builds an ``AIRequest`` and hands it to the engine.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

from backend.telegram_api._helpers import serialize_message

logger = logging.getLogger(__name__)

# ── in-memory selection registry ──
# Keyed by chat_id; each value is a set of selected message IDs.
# Capped at 10 selected messages per chat.
_MAX_SELECTED = 10
_selections: dict[int, set[int]] = {}


def toggle_selection(chat_id: int, msg_id: int) -> bool:
    """Toggle a message in/out of the selection set. Returns True if selected."""
    sel = _selections.setdefault(chat_id, set())
    if msg_id in sel:
        sel.discard(msg_id)
        return False
    if len(sel) >= _MAX_SELECTED:
        return False
    sel.add(msg_id)
    return True


def get_selection(chat_id: int) -> set[int]:
    """Return the selected message IDs for a chat, sorted ascending."""
    sel = _selections.get(chat_id, set())
    return set(sorted(sel))


def clear_selection(chat_id: int) -> None:
    """Clear all selections for a chat."""
    _selections.pop(chat_id, None)


def count_selected(chat_id: int) -> int:
    """Return how many messages are currently selected."""
    return len(_selections.get(chat_id, set()))


# ── page state ──
# Simple in-memory current-page tracker: (chat_id) -> page_index (0-based).
_pages: dict[int, int] = {}
_CHUNK_SIZE = 5


def get_page(chat_id: int) -> int:
    return _pages.get(chat_id, 0)


def set_page(chat_id: int, page: int) -> None:
    """Set the current page. Clamped to >= 0."""
    _pages[chat_id] = max(0, page)


# ── message fetch ──


async def fetch_chunk(
    client: Any,
    chat_id: int,
    page: int,
    chunk_size: int = _CHUNK_SIZE,
) -> list[dict[str, Any]]:
    """Fetch a page of messages for a chat.

    Returns exactly *chunk_size* messages in oldest→newest order,
    or fewer if there are not enough messages.
    """
    limit = (page + 1) * chunk_size
    try:
        raw: list[dict[str, Any]] = []
        async for msg in client.iter_messages(chat_id, limit=limit):
            raw.append(serialize_message(msg))
            if len(raw) >= limit:
                break
    except Exception as exc:
        logger.warning("Ghost Room: iter_messages failed for chat=%s page=%s: %s", chat_id, page, exc)
        return []

    raw.reverse()
    start = page * chunk_size
    return raw[start:start + chunk_size]


# ── AI dispatch ──


async def execute_ghost_ai(
    owner_id: int,
    chat_id: int,
    prompt_text: str,
    selected_messages: list[dict[str, Any]],
    tz_str: str = "UTC",
) -> tuple[bool, str]:
    """Build an AIRequest from the selected Ghost Room messages and execute it.

    Context is assembled deterministically: each selected message
    contributes a [N] Name: text entry. Nothing is inferred.
    The request goes through the existing engine (same path as ai_unified).
    Returns (success, response_text).
    """
    try:
        from backend.ai.engine.engine import get_engine
        from backend.ai.session.request import AIRequest
        from backend.ai.conversation.context_builder import ReplyContext

        engine = get_engine()
        if engine is None:
            return False, "AI engine not available."

        if selected_messages:
            lines = ["Selected messages:"]
            for i, msg in enumerate(selected_messages, 1):
                sender = msg.get("sender_name", "") or msg.get("sender_id", "?")
                text = msg.get("text", "") or msg.get("caption", "") or ""
                text = text[:300]
                lines.append(f"[{i}] {sender}: {text}")
            context_block = "\n".join(lines)
            user_message = f"{context_block}\n\nRequest: {prompt_text}"
        else:
            user_message = prompt_text

        request = AIRequest(
            session_id=f"ghost:{chat_id}",
            user_message=user_message,
            owner_id=owner_id,
            chat_id=chat_id,
            message_id=0,
            reply_context=ReplyContext(),
            timezone=tz_str,
        )

        result = await asyncio.wait_for(
            engine.execute(request),
            timeout=120.0,
        )

        if result.success and result.response:
            return True, result.response
        elif result.errors:
            err = result.errors[-1] if result.errors else "Unknown error"
            return False, str(err)[:300]
        else:
            return False, "AI returned no response."
    except asyncio.TimeoutError:
        return False, "AI request timed out."
    except Exception as exc:
        logger.warning("Ghost Room: AI execution failed: %s", exc)
        return False, f"AI execution failed: {exc}"


# ── formatting helpers ──


def _format_sender(msg: dict[str, Any]) -> str:
    sender_name = msg.get("sender_name", "") or ""
    if not sender_name:
        sender_id = msg.get("sender_id", 0) or 0
        if sender_id:
            sender_name = str(sender_id)
    return sender_name or "?"


def _format_relative(dt_str: str | None) -> str:
    if not dt_str:
        return ""
    try:
        dt = datetime.fromisoformat(dt_str.replace("Z", "+00:00"))
        now = datetime.now(timezone.utc)
        diff = now - dt.replace(tzinfo=timezone.utc) if dt.tzinfo is None else now - dt
        seconds = int(diff.total_seconds())
        if seconds < 60:
            return "now"
        elif seconds < 3600:
            return f"{seconds // 60}m"
        elif seconds < 86400:
            return f"{seconds // 3600}h"
        else:
            return f"{seconds // 86400}d"
    except Exception:
        return ""


def format_chat_list_item(msg: dict[str, Any]) -> str:
    """Render one item for the Ghost Room chat list."""
    name = msg.get("display_name", "") or str(msg.get("chat_id", "?"))
    preview = msg.get("last_preview", "") or ""
    if preview:
        preview = preview[:80]
        preview = preview.replace("\n", " ")
    unread = msg.get("unread_count", 0) or 0
    badge = f" ({unread})" if unread > 0 else ""
    ts = _format_relative(msg.get("last_message_at"))
    ts_str = f" · {ts}" if ts else ""
    line = f"{name}{badge}{ts_str}"
    if preview:
        line += f"\n  _{preview}_"
    return line


def format_chat_view_item(
    msg: dict[str, Any],
    is_selected: bool,
    seq: int,
) -> str:
    """Render one message in the chat view (five-message page)."""
    sender = _format_sender(msg)
    text = msg.get("text", "") or msg.get("caption", "") or ""
    if len(text) > 200:
        text = text[:200] + "…"
    text = text.replace("\n", " ")
    ts = _format_relative(msg.get("date"))
    ts_str = f" · {ts}" if ts else ""
    mark = "✓" if is_selected else "○"
    return f"[{seq}] {mark} **{sender}**{ts_str}\n     {text}"