"""
Ghost Seen service — discovery, validation, state, message inspection,
and AI dispatch for the Ghost Seen management surface.

Ghost Seen is a private management surface over private one-to-one human
conversations. This module owns:

- the in-memory selection registry and pagination state,
- private-user-only source validation (Part C),
- read-safe message inspection (iterative reads never mark anything read),
- deterministic context windows for AI replies,
- the pending AI-reply flow state (anchor / context count / disclosure),
- registry retention and manual removal (registry rows ONLY — Telegram
  messages are never touched),
- honest formatting helpers that distinguish outgoing vs incoming.

AI dispatch reuses the single existing engine/dispatcher path via
``AIRequest`` — no second dispatcher, telemetry, or session system.
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


def reset_chat_state(chat_id: int) -> None:
    """Drop all in-memory state for a chat (selection, page, pending reply)."""
    _selections.pop(chat_id, None)
    _pages.pop(chat_id, None)
    _pending_replies.pop(chat_id, None)


# ── page state ──
_pages: dict[int, int] = {}
_CHUNK_SIZE = 5


def get_page(chat_id: int) -> int:
    return _pages.get(chat_id, 0)


def set_page(chat_id: int, page: int) -> None:
    """Set the current page. Clamped to >= 0."""
    _pages[chat_id] = max(0, page)


# ── pending AI reply flow state ──
# Keyed by chat_id: {"anchor": msg_id, "context_n": int | None,
#                    "informed": bool | None}
# Each step of the Glass UI flow writes exactly one field; execution
# consumes the record once and clears it.
_pending_replies: dict[int, dict[str, Any]] = {}

ALLOWED_CONTEXT_COUNTS = (1, 5, 10, 20)


def start_reply_flow(chat_id: int, anchor_msg_id: int) -> None:
    if anchor_msg_id > 0:
        _pending_replies[chat_id] = {
            "anchor": int(anchor_msg_id),
            "context_n": None,
            "informed": None,
        }


def get_reply_flow(chat_id: int) -> dict[str, Any] | None:
    flow = _pending_replies.get(chat_id)
    if not flow:
        return None
    return dict(flow)


def set_reply_context_count(chat_id: int, n: int) -> bool:
    flow = _pending_replies.get(chat_id)
    if not flow or n not in ALLOWED_CONTEXT_COUNTS:
        return False
    flow["context_n"] = int(n)
    return True


def set_reply_disclosure(chat_id: int, informed: bool) -> bool:
    flow = _pending_replies.get(chat_id)
    if not flow or flow.get("context_n") is None:
        return False
    flow["informed"] = bool(informed)
    return True


def consume_reply_flow(chat_id: int) -> dict[str, Any] | None:
    """Return the complete flow record and clear it (single use)."""
    flow = _pending_replies.pop(chat_id, None)
    if not flow:
        return None
    if flow.get("anchor") and flow.get("context_n") and flow.get("informed") is not None:
        return flow
    return None


def cancel_reply_flow(chat_id: int) -> None:
    _pending_replies.pop(chat_id, None)


# ── private-user-only source validation (authoritative rule) ──

def validate_private_source(
    chat_id: int | None,
    sender: Any,
    owner_id: int,
) -> str | None:
    """Return a display name when *sender* is a valid Ghost Seen source.

    A valid source is a real private conversation with another human
    Telegram user. Rejected: groups/channels/supergroups (non-positive
    or mismatched chat ids), bots, Saved Messages / self-chat, service
    chats without a usable user sender.
    """
    try:
        cid = int(chat_id or 0)
    except (TypeError, ValueError):
        return None
    if cid <= 0:
        # Groups/channels carry negative or -100-prefixed ids; Saved
        # Messages resolves to the owner's own id.
        return None

    if sender is None:
        return None
    sender_id = getattr(sender, "id", 0) or 0
    if not sender_id or sender_id != cid:
        return None
    if getattr(sender, "bot", False):
        return None
    if sender_id == owner_id:
        return None

    display_name = ""
    first = getattr(sender, "first_name", "") or ""
    last = getattr(sender, "last_name", "") or ""
    display_name = f"{first} {last}".strip()
    if not display_name:
        display_name = getattr(sender, "username", "") or ""
    if not isinstance(display_name, str):
        display_name = ""
    return display_name.strip() or str(sender_id)


# ── registry persistence (rows only — never Telegram data) ──


async def read_registry_rows() -> list[dict[str, Any]]:
    """Read ghost_chats rows (newest first); [] when unavailable."""
    try:
        from backend.db.client import get_db, _run_sync

        db = get_db()
        if db is None:
            return []

        def _query():
            return (
                db.table("ghost_chats")
                .select("*")
                .order("last_message_at", desc=True)
                .execute()
            )

        result = await _run_sync(_query)
        return (result.data or []) if result else []
    except Exception as exc:
        logger.warning("Ghost Seen: read registry failed: %s", exc)
        return []


async def upsert_source_chat(
    chat_id: int,
    display_name: str,
    preview: str,
    timestamp: str,
) -> None:
    """Upsert a registry row. Failures are logged, never raised."""
    try:
        from backend.db.client import get_db, _run_sync

        db = get_db()
        if db is None:
            return

        def _upsert():
            db.table("ghost_chats").upsert({
                "chat_id": chat_id,
                "display_name": display_name,
                "last_preview": preview[:160],
                "last_message_at": timestamp,
                "updated_at": timestamp,
            }, on_conflict="chat_id").execute()
            db.table("ghost_chats").update({
                "unread_count": db.raw("unread_count + 1"),
            }).eq("chat_id", chat_id).execute()

        await _run_sync(_upsert)
    except Exception as exc:
        logger.warning(
            "Ghost Seen: upsert failed for chat=%s: %s", chat_id, exc,
        )


async def clear_unread(chat_id: int) -> None:
    """Reset the registry unread counter for a chat (registry bookkeeping)."""
    try:
        from backend.db.client import get_db, _run_sync

        db = get_db()
        if db is None:
            return

        def _update():
            db.table("ghost_chats").update({
                "unread_count": 0,
            }).eq("chat_id", chat_id).execute()

        await _run_sync(_update)
    except Exception as exc:
        logger.debug("Ghost Seen: clear unread failed for chat=%s: %s", chat_id, exc)


async def remove_chat(chat_id: int) -> bool:
    """Remove one conversation from the Ghost Seen registry.

    Affects ONLY the registry row and local UI state. Telegram messages,
    chats, and read state are never touched.
    """
    reset_chat_state(chat_id)
    try:
        from backend.db.client import get_db, _run_sync

        db = get_db()
        if db is None:
            return False

        def _delete():
            db.table("ghost_chats").delete().eq("chat_id", chat_id).execute()

        await _run_sync(_delete)
        return True
    except Exception as exc:
        logger.warning("Ghost Seen: remove failed for chat=%s: %s", chat_id, exc)
        return False


async def delete_expired_rows(expired_ids: list[int]) -> None:
    """Best-effort deletion of expired registry rows. Never raises."""
    if not expired_ids:
        return
    try:
        from backend.db.client import get_db, _run_sync

        db = get_db()
        if db is None:
            return

        def _delete():
            db.table("ghost_chats").delete().in_("chat_id", expired_ids).execute()

        await _run_sync(_delete)
    except Exception as exc:
        logger.warning("Ghost Seen: expired-row cleanup failed: %s", exc)


def apply_retention(
    rows: list[dict[str, Any]],
    retention_days: int,
    now: datetime | None = None,
) -> tuple[list[dict[str, Any]], list[int]]:
    """Split registry rows into (kept, expired_ids).

    Deterministic lazy expiry: a row expires when its ``last_message_at``
    is older than ``retention_days``. Rows without a timestamp are kept.
    """
    now = now or datetime.now(timezone.utc)
    days = max(1, min(365, int(retention_days)))
    cutoff = now.timestamp() - days * 86400
    kept: list[dict[str, Any]] = []
    expired: list[int] = []
    for row in rows:
        ts = row.get("last_message_at")
        dt = _parse_ts(ts)
        if dt is None:
            kept.append(row)
            continue
        if dt.timestamp() < cutoff:
            cid = row.get("chat_id")
            if cid:
                expired.append(int(cid))
        else:
            kept.append(row)
    return kept, expired


def _parse_ts(value: Any) -> datetime | None:
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if isinstance(value, str) and value:
        try:
            dt = datetime.fromisoformat(value.replace("Z", "+00:00"))
            return dt if dt.tzinfo else dt.replace(tzinfo=timezone.utc)
        except ValueError:
            return None
    return None


# ── read-safe message inspection ──
# Only passive reads are used (iter_messages / get_messages). No
# ReadHistoryRequest / send_read_acknowledge / MarkDialogUnread call is
# ever made from Ghost Seen paths.


async def fetch_chunk(
    client: Any,
    chat_id: int,
    page: int,
    chunk_size: int = _CHUNK_SIZE,
) -> list[dict[str, Any]]:
    """Fetch a page of messages for a chat (oldest→newest, no read marking)."""
    limit = (page + 1) * chunk_size
    raw: list[Any] = []
    try:
        async for msg in client.iter_messages(chat_id, limit=limit):
            raw.append(msg)
            if len(raw) >= limit:
                break
    except Exception as exc:
        logger.warning(
            "Ghost Seen: iter_messages failed for chat=%s page=%s: %s",
            chat_id, page, exc,
        )
        return []

    raw.reverse()
    start = page * chunk_size
    return [serialize_message(m) for m in raw[start:start + chunk_size]]


async def fetch_context_window(
    client: Any,
    chat_id: int,
    anchor_msg_id: int,
    n: int,
) -> list[dict[str, Any]]:
    """Fetch up to *n* messages ending at the anchor (anchor inclusive).

    Passive reads only. Returns oldest→newest serialized dicts; [] when
    the anchor cannot be resolved.
    """
    n = max(1, int(n))
    anchor_raw = None
    try:
        anchor_raw = await client.get_messages(chat_id, ids=anchor_msg_id)
    except Exception as exc:
        logger.warning(
            "Ghost Seen: anchor fetch failed chat=%s id=%s: %s",
            chat_id, anchor_msg_id, exc,
        )
        return []
    if anchor_raw is None:
        return []

    older: list[Any] = []
    if n > 1:
        try:
            async for msg in client.iter_messages(
                chat_id, offset_id=anchor_msg_id, limit=n - 1,
            ):
                older.append(msg)
                if len(older) >= n - 1:
                    break
        except Exception as exc:
            logger.warning(
                "Ghost Seen: context fetch failed chat=%s: %s", chat_id, exc,
            )

    window = sorted(list(older) + [anchor_raw], key=lambda m: getattr(m, "id", 0))
    return [serialize_message(m) for m in window]


# ── AI dispatch (existing engine path only) ──


async def execute_ghost_seen_ai(
    owner_id: int,
    chat_id: int,
    prompt_text: str,
    context_messages: list[dict[str, Any]],
    tz_str: str = "UTC",
) -> tuple[bool, str]:
    """Build an AIRequest from the Ghost Seen context and execute it.

    Context is assembled deterministically: each message contributes a
    ``[i] ME/THEM — Name: text`` entry. Nothing is inferred. The request
    goes through the existing engine (same path as ai_unified).
    Returns (success, response_text).
    """
    try:
        from backend.ai.engine.engine import get_engine
        from backend.ai.session.request import AIRequest
        from backend.ai.conversation.context_builder import ReplyContext

        engine = get_engine()
        if engine is None:
            return False, "AI engine not available."

        lines = ["Conversation context:"]
        for i, msg in enumerate(context_messages, 1):
            who = "ME" if msg.get("out") else "THEM"
            name = msg.get("sender_name", "") or msg.get("sender_id", "?")
            text = msg.get("text", "") or msg.get("caption", "") or ""
            text = text[:300].replace("\n", " ")
            lines.append(f"[{i}] {who} — {name}: {text}")
        context_block = "\n".join(lines)
        user_message = (
            f"{context_block}\n\n"
            f"Task: {prompt_text}"
        )

        request = AIRequest(
            session_id=f"ghost_seen:{chat_id}",
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
        logger.warning("Ghost Seen: AI execution failed: %s", exc)
        return False, "AI execution failed."


# ── formatting helpers ──

AI_DISCLOSURE_SUFFIX = "\n\n🤖 _This reply was drafted with an AI assistant._"


def _format_sender(msg: dict[str, Any]) -> str:
    sender_name = msg.get("sender_name", "") or ""
    if not sender_name:
        sender_id = msg.get("sender_id", 0) or 0
        if sender_id:
            sender_name = str(sender_id)
    return sender_name or "?"


def _is_from_me(msg: dict[str, Any], owner_id: int) -> bool:
    if msg.get("out") is True:
        return True
    return bool(owner_id) and msg.get("sender_id") == owner_id


def direction_label(msg: dict[str, Any], owner_id: int) -> str:
    """Honest sender-direction label: 'FROM ME' or 'FROM THEM'."""
    return "FROM ME" if _is_from_me(msg, owner_id) else "FROM THEM"


def format_relative(value: Any) -> str:
    """Relative time for an ISO string or datetime; '' when unavailable."""
    dt = _parse_ts(value)
    if dt is None:
        return ""
    diff = datetime.now(timezone.utc) - dt
    seconds = int(diff.total_seconds())
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def format_chat_list_item(row: dict[str, Any]) -> str:
    """Render one item for the Ghost Seen list panel."""
    name = row.get("display_name", "") or str(row.get("chat_id", "?"))
    preview = row.get("last_preview", "") or ""
    if preview:
        preview = preview[:80].replace("\n", " ")
    unread = row.get("unread_count", 0) or 0
    badge = f" ({unread})" if unread > 0 else ""
    ts = format_relative(row.get("last_message_at"))
    ts_str = f" · {ts}" if ts else ""
    line = f"{name}{badge}{ts_str}"
    if preview:
        line += f"\n  _{preview}_"
    return line


def format_chat_view_item(
    msg: dict[str, Any],
    is_selected: bool,
    seq: int,
    owner_id: int,
) -> str:
    """Render one message with explicit sender identity (Part F)."""
    sender = _format_sender(msg)
    direction = direction_label(msg, owner_id)
    text = msg.get("text", "") or msg.get("caption", "") or ""
    if len(text) > 200:
        text = text[:200] + "…"
    text = text.replace("\n", " ")
    ts = format_relative(msg.get("date"))
    ts_str = f" · {ts}" if ts else ""
    mark = "✓" if is_selected else "○"
    header = f"[{seq}] {mark} {direction} — {sender}{ts_str}"
    body_line = f"     {text}" if text else ""
    return header if not body_line else f"{header}\n{body_line}"


def format_reply_target(anchor: dict[str, Any], owner_id: int) -> str:
    """Unambiguous reply-target banner for the selected message (Part G)."""
    mid = anchor.get("id", 0)
    direction = direction_label(anchor, owner_id)
    sender = _format_sender(anchor)
    text = anchor.get("text", "") or anchor.get("caption", "") or ""
    if len(text) > 120:
        text = text[:120] + "…"
    text = text.replace("\n", " ")
    lines = [
        f"↩ **Reply target:** #{mid} · {direction}",
        f"Sender: **{sender}**",
    ]
    if text:
        lines.append(f'Content: "{text}"')
    lines.append("_The next manual or AI reply targets this message._")
    return "\n".join(lines)
