"""
Ghost Seen service — discovery, validation, state, and message inspection.

Ghost Seen is a private management surface over private one-to-one human
conversations. AI reply generation is intentionally not part of this service.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any

from backend.telegram_api._helpers import serialize_message

logger = logging.getLogger(__name__)

_MAX_SELECTED = 10
_selections: dict[int, set[int]] = {}


def toggle_selection(chat_id: int, msg_id: int) -> bool:
    sel = _selections.setdefault(chat_id, set())
    if msg_id in sel:
        sel.discard(msg_id)
        return False
    if len(sel) >= _MAX_SELECTED:
        return False
    sel.add(msg_id)
    return True


def get_selection(chat_id: int) -> set[int]:
    return set(sorted(_selections.get(chat_id, set())))


def clear_selection(chat_id: int) -> None:
    _selections.pop(chat_id, None)


def count_selected(chat_id: int) -> int:
    return len(_selections.get(chat_id, set()))


_pages: dict[int, int] = {}
_CHUNK_SIZE = 5


def reset_chat_state(chat_id: int) -> None:
    _selections.pop(chat_id, None)
    _pages.pop(chat_id, None)


def get_page(chat_id: int) -> int:
    return _pages.get(chat_id, 0)


def set_page(chat_id: int, page: int) -> None:
    _pages[chat_id] = max(0, page)


def validate_private_source(chat_id: int | None, sender: Any, owner_id: int) -> str | None:
    try:
        cid = int(chat_id or 0)
    except (TypeError, ValueError):
        return None
    if cid <= 0 or sender is None:
        return None
    sender_id = getattr(sender, "id", 0) or 0
    if sender_id != cid or getattr(sender, "bot", False) or sender_id == owner_id:
        return None
    first = getattr(sender, "first_name", "") or ""
    last = getattr(sender, "last_name", "") or ""
    display_name = f"{first} {last}".strip() or getattr(sender, "username", "") or ""
    return display_name.strip() or str(sender_id)


async def read_registry_rows() -> list[dict[str, Any]]:
    try:
        from backend.db.client import get_db, _run_sync
        db = get_db()
        if db is None:
            return []
        def _query():
            return db.table("ghost_chats").select("*").order("last_message_at", desc=True).execute()
        result = await _run_sync(_query)
        return (result.data or []) if result else []
    except Exception as exc:
        logger.warning("Ghost Seen: read registry failed: %s", exc)
        return []


async def upsert_source_chat(chat_id: int, display_name: str, preview: str, timestamp: str) -> None:
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
            db.table("ghost_chats").update({"unread_count": db.raw("unread_count + 1")}).eq("chat_id", chat_id).execute()
        await _run_sync(_upsert)
    except Exception as exc:
        logger.warning("Ghost Seen: upsert failed for chat=%s: %s", chat_id, exc)


async def clear_unread(chat_id: int) -> None:
    try:
        from backend.db.client import get_db, _run_sync
        db = get_db()
        if db is None:
            return
        def _update():
            db.table("ghost_chats").update({"unread_count": 0}).eq("chat_id", chat_id).execute()
        await _run_sync(_update)
    except Exception as exc:
        logger.debug("Ghost Seen: clear unread failed for chat=%s: %s", chat_id, exc)


async def remove_chat(chat_id: int) -> bool:
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


def apply_retention(rows: list[dict[str, Any]], retention_seconds: int, now: datetime | None = None) -> tuple[list[dict[str, Any]], list[int]]:
    now = now or datetime.now(timezone.utc)
    if retention_seconds is None or int(retention_seconds) <= 0:
        return list(rows), []
    seconds = max(300, min(31_536_000, int(retention_seconds)))
    cutoff = now.timestamp() - seconds
    kept, expired = [], []
    for row in rows:
        dt = _parse_ts(row.get("last_message_at"))
        if dt is None or dt.timestamp() >= cutoff:
            kept.append(row)
        elif row.get("chat_id"):
            expired.append(int(row["chat_id"]))
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


_ENTITY_SWEEP_LIMIT = 500


async def ensure_entity(client: Any, chat_id: int) -> bool:
    try:
        await client.get_input_entity(chat_id)
        return True
    except Exception:
        pass
    for kwargs in ({"archived": False}, {"archived": True}):
        try:
            count = 0
            async for _dialog in client.iter_dialogs(limit=_ENTITY_SWEEP_LIMIT, **kwargs):
                count += 1
                if count >= _ENTITY_SWEEP_LIMIT:
                    break
        except Exception as exc:
            logger.debug("Ghost Seen: dialog sweep failed (%s): %s", kwargs, exc)
    try:
        await client.get_input_entity(chat_id)
        return True
    except Exception as exc:
        logger.warning("Ghost Seen: entity unresolvable after sweep chat=%s: %s", chat_id, exc)
        return False


def _sender_name_of(raw: Any) -> str:
    sender = getattr(raw, "sender", None)
    if sender is None:
        return ""
    first = getattr(sender, "first_name", "") or ""
    last = getattr(sender, "last_name", "") or ""
    return (f"{first} {last}".strip() or getattr(sender, "username", "") or "").strip()


def _serialize_with_sender(raw: Any) -> dict[str, Any]:
    data = serialize_message(raw)
    name = _sender_name_of(raw)
    if name:
        data["sender_name"] = name
    return data


async def fetch_chunk(client: Any, chat_id: int, page: int, chunk_size: int = _CHUNK_SIZE) -> tuple[list[dict[str, Any]], str]:
    if not await ensure_entity(client, chat_id):
        return [], "entity"
    limit = (page + 1) * chunk_size
    raw = []
    try:
        async for msg in client.iter_messages(chat_id, limit=limit):
            raw.append(msg)
            if len(raw) >= limit:
                break
    except Exception as exc:
        logger.warning("Ghost Seen: iter_messages failed for chat=%s page=%s: %s", chat_id, page, exc)
        return [], "fetch"
    raw.reverse()
    start = page * chunk_size
    return [_serialize_with_sender(msg) for msg in raw[start:start + chunk_size]], ""


def _format_sender(msg: dict[str, Any]) -> str:
    return str(msg.get("sender_name", "") or msg.get("sender_id", 0) or "?")


def _is_from_me(msg: dict[str, Any], owner_id: int) -> bool:
    return msg.get("out") is True or bool(owner_id) and msg.get("sender_id") == owner_id


def direction_label(msg: dict[str, Any], owner_id: int) -> str:
    return "FROM ME" if _is_from_me(msg, owner_id) else "FROM THEM"


def format_relative(value: Any) -> str:
    dt = _parse_ts(value)
    if dt is None:
        return ""
    seconds = int((datetime.now(timezone.utc) - dt).total_seconds())
    if seconds < 60:
        return "now"
    if seconds < 3600:
        return f"{seconds // 60}m"
    if seconds < 86400:
        return f"{seconds // 3600}h"
    return f"{seconds // 86400}d"


def format_chat_list_item(row: dict[str, Any]) -> str:
    name = row.get("display_name", "") or str(row.get("chat_id", "?"))
    preview = (row.get("last_preview", "") or "")[:80].replace("\n", " ")
    unread = row.get("unread_count", 0) or 0
    badge = f" ({unread})" if unread > 0 else ""
    ts = format_relative(row.get("last_message_at"))
    line = f"{name}{badge}{f' · {ts}' if ts else ''}"
    return f"{line}\n  _{preview}_" if preview else line


def format_chat_view_item(msg: dict[str, Any], is_selected: bool, seq: int, owner_id: int) -> str:
    text = (msg.get("text", "") or msg.get("caption", "") or "")[:200].replace("\n", " ")
    if len(msg.get("text", "") or msg.get("caption", "") or "") > 200:
        text += "…"
    header = f"[{seq}] {'✓' if is_selected else '○'} {direction_label(msg, owner_id)} — {_format_sender(msg)}"
    return f"{header}\n     {text}" if text else header


def format_reply_target(anchor: dict[str, Any], owner_id: int) -> str:
    mid = anchor.get("id", 0)
    text = (anchor.get("text", "") or anchor.get("caption", "") or "")[:120].replace("\n", " ")
    if len(anchor.get("text", "") or anchor.get("caption", "") or "") > 120:
        text += "…"
    lines = [f"↩ **Reply target:** #{mid} · {direction_label(anchor, owner_id)}", f"Sender: **{_format_sender(anchor)}**"]
    if text:
        lines.append(f'Content: "{text}"')
    lines.append("_The next manual reply targets this message._")
    return "\n".join(lines)
