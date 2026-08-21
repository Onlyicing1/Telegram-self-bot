"""
Database layer — Supabase if available, in-memory fallback otherwise.

The singleton client is initialised on first access. If Supabase env
vars are missing or the connection fails, all operations degrade to
in-memory storage so the bot never crashes.

CRITICAL: All public functions that touch Supabase are async and run
the synchronous HTTP calls via asyncio.to_thread() with a bounded
timeout. The supabase-py library uses httpx synchronously — calling
db.table(...).execute() directly in an asyncio coroutine blocks the
entire event loop until the HTTP response arrives. If the Supabase
REST API is slow or the TCP connection stalls, the whole runtime
freezes (no commands, no heartbeat, no bio updates).

By running each DB operation in a thread with a timeout, the event
loop stays responsive even when Supabase is slow or unreachable.
"""
import asyncio
import logging
import os
import random
import string
from datetime import datetime, timedelta, timezone

from backend.diagnostics import record_event

logger = logging.getLogger(__name__)

_client = None
_available = False
_fallback: dict = {"saved_items": [], "bio_state": {}, "bot_logs": [], "username_state": {}}
_save_code_lock = asyncio.Lock()
_initialised = False

_SHORT_CODE_PREFIX = "S"
_SHORT_CODE_NUM_LEN = 4
_SHORT_CODE_ALPHABET = string.ascii_uppercase + string.digits

_DB_TIMEOUT = 10.0


def _check_available() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"))


def get_db():
    """Return the Supabase client, or None if unavailable."""
    global _client, _available, _initialised
    if _initialised:
        return _client if _available else None

    _initialised = True

    if not _check_available():
        logger.warning("[SAVE_DB] Supabase env vars not set — using in-memory fallback.")
        _available = False
        return None

    try:
        from supabase import create_client
        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_ROLE_KEY"],
        )
        _available = True
        logger.info("[SAVE_DB] Supabase client initialised.")
        return _client
    except Exception as exc:
        logger.error("[SAVE_DB] Supabase init FAILED (%s) — using in-memory fallback.", exc)
        _available = False
        return None


def is_available() -> bool:
    return _available


async def _run_sync(fn, *args, **kwargs):
    """Run a synchronous DB function in a thread with a bounded timeout.

    Uses the centralized operation watchdog so that stuck DB operations
    emit structured OP_TIMEOUT diagnostics (operation name, elapsed time,
    runtime state, client generation) instead of dying silently.
    """
    from backend.runtime.operation_watchdog import guarded_await

    op_name = getattr(fn, "__name__", "db_unknown")
    return await guarded_await(
        asyncio.to_thread(fn, *args, **kwargs),
        name=f"db:{op_name}",
        timeout=_DB_TIMEOUT,
    )


# ── bot_logs ──

def _log_sync(entry: dict) -> None:
    db = get_db()
    if db:
        db.table("bot_logs").insert(entry).execute()
    else:
        entry["id"] = len(_fallback["bot_logs"]) + 1
        _fallback["bot_logs"].append(entry)


async def log(owner_id: int, level: str, message: str, context: dict | None = None) -> None:
    try:
        entry = {
            "owner_id": owner_id,
            "level": level,
            "message": message,
            "context": context or {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        await _run_sync(_log_sync, entry)
    except Exception as exc:
        logger.error("[SAVE_DB] bot_logs insert FAILED: %s", exc)


# ── save codes ──

async def get_next_save_code() -> str:
    """Generate a compact, human-readable save code (e.g. S391, A82).

    Tries a sequential numeric code first (S + zero-padded count) so codes
    are stable and sortable. If that collides with an existing row (e.g.
    legacy SV-NNNNNN rows were removed), falls back to a random alphanumeric
    code. Always verifies uniqueness against the DB before returning.
    """
    async with _save_code_lock:
        db = get_db()
        if db is None:
            logger.warning("[SAVE_DB] get_next_save_code: DB unavailable — using fallback counter.")
            count = len(_fallback["saved_items"])
            sequential = f"{_SHORT_CODE_PREFIX}{count + 1:0{_SHORT_CODE_NUM_LEN}d}"
            return sequential

        count = 0
        try:
            count = await _run_sync(_count_saves_sync)
        except Exception as exc:
            logger.error("[SAVE_DB] get_next_save_code count query FAILED: %s", exc)
            count = len(_fallback["saved_items"])

        sequential = f"{_SHORT_CODE_PREFIX}{count + 1:0{_SHORT_CODE_NUM_LEN}d}"
        if await _is_code_free(sequential):
            logger.info("[SAVE_DB] get_next_save_code → %s (sequential)", sequential)
            return sequential

        for _ in range(50):
            rand_code = _SHORT_CODE_PREFIX + "".join(
                random.choices(_SHORT_CODE_ALPHABET, k=4)
            )
            if await _is_code_free(rand_code):
                logger.info("[SAVE_DB] get_next_save_code → %s (random)", rand_code)
                return rand_code

        logger.warning("[SAVE_DB] get_next_save_code: collision fallback → %s", sequential)
        return sequential


def _count_saves_sync() -> int:
    db = get_db()
    if not db:
        return len(_fallback["saved_items"])
    result = db.table("saved_items").select("id", count="exact").execute()
    return result.count or 0


async def _is_code_free(code: str) -> bool:
    """Check that a code is not already used as save_code."""
    db = get_db()
    if db:
        try:
            res = await _run_sync(_is_code_free_sync, code)
            return res
        except Exception as exc:
            logger.error("[SAVE_DB] _is_code_free(%s) query FAILED: %s", code, exc)
            return True
    for item in _fallback["saved_items"]:
        if item.get("save_code") == code:
            return False
    return True


def _is_code_free_sync(code: str) -> bool:
    db = get_db()
    if not db:
        return True
    res = (
        db.table("saved_items")
        .select("id")
        .eq("save_code", code)
        .limit(1)
        .execute()
    )
    return not (res.data or [])


# ── saved_items: writes ──

def _insert_save_sync(data: dict) -> dict | None:
    db = get_db()
    logger.info("[SAVE_DB] insert_save payload=%s", _safe_payload(data))
    if db is None:
        logger.warning("[SAVE_DB] insert_save: DB unavailable — storing in fallback.")
        data["id"] = len(_fallback["saved_items"]) + 1
        _fallback["saved_items"].append(data)
        logger.info("[SAVE_DB] insert_save fallback_ok=True id=%s", data["id"])
        return data

    try:
        result = db.table("saved_items").insert(data).execute()
        inserted = result.data[0] if result.data else None
        if inserted is None:
            logger.error("[SAVE_DB] insert_save ERROR: insert() returned no data. response=%s", result)
            record_event("database", "insert saved_items", 0, "ERROR", "insert returned no data")
            return None
        logger.info("[SAVE_DB] insert_save response=%s", _safe_row(inserted))
        logger.info("[SAVE_DB] insert_ok=True id=%s", inserted.get("id"))
        record_event("database", "insert saved_items", 0, "SUCCESS")
        return inserted
    except Exception as exc:
        logger.error("[SAVE_DB] insert_save ERROR: %s", exc, exc_info=True)
        record_event("database", "insert saved_items", 0, "ERROR", str(exc))
        return None


async def insert_save(data: dict) -> dict | None:
    """Insert a saved_items row. Returns the inserted row, or None on failure. Never raises."""
    try:
        return await _run_sync(_insert_save_sync, data)
    except Exception as exc:
        logger.error("[SAVE_DB] insert_save FAILED: %s", exc)
        record_event("database", "insert saved_items", 0, "ERROR", str(exc))
        return None


# ── saved_items: reads ──

def _query_save_sync(save_code: str) -> dict | None:
    code = save_code.upper()
    db = get_db()
    if db:
        try:
            result = (
                db.table("saved_items")
                .select("*")
                .eq("save_code", code)
                .maybe_single()
                .execute()
            )
            record_event("database", "select saved_items", 0, "SUCCESS")
            return result.data
        except Exception as exc:
            logger.error("[SAVE_DB] query_save(%s) FAILED: %s", code, exc)
            record_event("database", "select saved_items", 0, "ERROR", str(exc))
    for item in _fallback["saved_items"]:
        lc = (item.get("save_code") or "").upper()
        if lc == code:
            return item
    return None


async def query_save(save_code: str) -> dict | None:
    """Look up a saved item by save_code."""
    try:
        return await _run_sync(_query_save_sync, save_code)
    except Exception as exc:
        logger.error("[SAVE_DB] query_save(%s) FAILED: %s", save_code, exc)
        return None


def _list_saves_sync(owner_id: int, limit: int, offset: int) -> tuple[list, int]:
    db = get_db()
    if db:
        try:
            result = (
                db.table("saved_items")
                .select("*")
                .eq("owner_id", owner_id)
                .order("created_at", desc=True)
                .range(offset, offset + limit - 1)
                .execute()
            )
            count_res = (
                db.table("saved_items")
                .select("id", count="exact")
                .eq("owner_id", owner_id)
                .execute()
            )
            return result.data or [], count_res.count or 0
        except Exception as exc:
            logger.error("[SAVE_DB] list_saves FAILED: %s", exc)
    items = [s for s in _fallback["saved_items"] if s.get("owner_id") == owner_id]
    total = len(items)
    return items[offset:offset + limit], total


async def list_saves(owner_id: int, limit: int = 50, offset: int = 0) -> tuple[list, int]:
    try:
        return await _run_sync(_list_saves_sync, owner_id, limit, offset)
    except Exception as exc:
        logger.error("[SAVE_DB] list_saves FAILED: %s", exc)
        items = [s for s in _fallback["saved_items"] if s.get("owner_id") == owner_id]
        return items[offset:offset + limit], len(items)


def _list_recent_saves_sync(owner_id: int, limit: int) -> list:
    db = get_db()
    if db:
        try:
            result = (
                db.table("saved_items")
                .select("save_code,save_type,media_type,mime_type,created_at")
                .eq("owner_id", owner_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return result.data or []
        except Exception as exc:
            logger.error("[SAVE_DB] list_recent_saves FAILED: %s", exc)
    items = sorted(
        [s for s in _fallback["saved_items"] if s.get("owner_id") == owner_id],
        key=lambda x: x.get("created_at", ""),
        reverse=True,
    )
    return items[:limit]


async def list_recent_saves(owner_id: int, limit: int = 10) -> list:
    """Return recent saves for .list — uses idx_saved_items_owner_created."""
    try:
        return await _run_sync(_list_recent_saves_sync, owner_id, limit)
    except Exception as exc:
        logger.error("[SAVE_DB] list_recent_saves FAILED: %s", exc)
        return []


def _search_saves_sync(owner_id: int, query: str, limit: int) -> list:
    pattern = f"%{query}%"
    db = get_db()
    if db:
        try:
            result = (
                db.table("saved_items")
                .select("save_code,save_type,media_type,mime_type,created_at")
                .eq("owner_id", owner_id)
                .or_(
                    f"caption.ilike.{pattern},"
                    f"save_code.ilike.{pattern},"
                    f"mime_type.ilike.{pattern}"
                )
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return result.data or []
        except Exception as exc:
            logger.error("[SAVE_DB] search_saves FAILED: %s", exc)
    q_lower = query.lower()
    matches = []
    for item in _fallback["saved_items"]:
        if item.get("owner_id") != owner_id:
            continue
        haystack = " ".join(str(item.get(k) or "") for k in
                             ("caption", "save_code", "mime_type")).lower()
        if q_lower in haystack:
            matches.append(item)
    matches.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return matches[:limit]


async def search_saves(owner_id: int, query: str, limit: int = 20) -> list:
    """Search saves by caption, save_code, mime_type."""
    try:
        return await _run_sync(_search_saves_sync, owner_id, query, limit)
    except Exception as exc:
        logger.error("[SAVE_DB] search_saves FAILED: %s", exc)
        return []


# ── saved_items: deletes ──

def _delete_save_sync(owner_id: int, code: str) -> dict | None:
    target = _query_save_sync(code)
    if not target or target.get("owner_id") != owner_id:
        return None
    db = get_db()
    if db:
        try:
            sc = target.get("save_code")
            res = (
                db.table("saved_items")
                .delete()
                .eq("owner_id", owner_id)
                .eq("save_code", sc)
                .execute()
            )
            return target if (res.data or []) else None
        except Exception as exc:
            logger.error("[SAVE_DB] delete_save FAILED: %s", exc)
    _fallback["saved_items"] = [
        s for s in _fallback["saved_items"]
        if s.get("save_code") != target.get("save_code")
    ]
    return target


async def delete_save(owner_id: int, code: str) -> dict | None:
    """Delete a saved_items row by save_code. Returns the row or None."""
    try:
        return await _run_sync(_delete_save_sync, owner_id, code)
    except Exception as exc:
        logger.error("[SAVE_DB] delete_save FAILED: %s", exc)
        return None


def _delete_save_row_sync(owner_id: int, code: str) -> dict | None:
    target = _query_save_sync(code)
    if not target or target.get("owner_id") != owner_id:
        return None
    db = get_db()
    if db:
        try:
            sc = target.get("save_code")
            res = (
                db.table("saved_items")
                .delete()
                .eq("owner_id", owner_id)
                .eq("save_code", sc)
                .execute()
            )
            return target if (res.data or []) else None
        except Exception as exc:
            logger.error("[SAVE_DB] delete_save_row FAILED: %s", exc)
    _fallback["saved_items"] = [
        s for s in _fallback["saved_items"]
        if s.get("save_code") != target.get("save_code")
    ]
    return target


async def delete_save_row(owner_id: int, code: str) -> dict | None:
    """Delete a saved_items row by save_code. Returns the deleted row or None."""
    try:
        return await _run_sync(_delete_save_row_sync, owner_id, code)
    except Exception as exc:
        logger.error("[SAVE_DB] delete_save_row FAILED: %s", exc)
        return None


# ── saved_items: bulk operations ──

def _list_all_saves_sync(owner_id: int) -> list:
    db = get_db()
    if db:
        try:
            result = (
                db.table("saved_items")
                .select("id,save_code,saved_chat_id,saved_msg_id,media_type,mime_type,file_size,save_type,created_at")
                .eq("owner_id", owner_id)
                .order("created_at", desc=True)
                .execute()
            )
            return result.data or []
        except Exception as exc:
            logger.error("[SAVE_DB] list_all_saves FAILED: %s", exc)
    items = [s for s in _fallback["saved_items"] if s.get("owner_id") == owner_id]
    items.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return items


async def list_all_saves(owner_id: int) -> list:
    """Return ALL saved items for an owner — used by cleanup and stats."""
    try:
        return await _run_sync(_list_all_saves_sync, owner_id)
    except Exception as exc:
        logger.error("[SAVE_DB] list_all_saves FAILED: %s", exc)
        return []


def _cleanup_orphans_sync(owner_id: int, orphan_ids: list[int]) -> int:
    if not orphan_ids:
        return 0
    db = get_db()
    if db:
        try:
            res = (
                db.table("saved_items")
                .delete()
                .eq("owner_id", owner_id)
                .in_("id", orphan_ids)
                .execute()
            )
            return len(res.data) if res.data else 0
        except Exception as exc:
            logger.error("[SAVE_DB] cleanup_orphans FAILED: %s", exc)
    before = len(_fallback["saved_items"])
    id_set = set(orphan_ids)
    _fallback["saved_items"] = [
        s for s in _fallback["saved_items"]
        if not (s.get("owner_id") == owner_id and s.get("id") in id_set)
    ]
    return before - len(_fallback["saved_items"])


async def cleanup_orphans(owner_id: int, orphan_ids: list[int]) -> int:
    """Delete saved_items rows by ID. Returns count of deleted rows."""
    if not orphan_ids:
        return 0
    try:
        return await _run_sync(_cleanup_orphans_sync, owner_id, orphan_ids)
    except Exception as exc:
        logger.error("[SAVE_DB] cleanup_orphans FAILED: %s", exc)
        return 0


# ── saved_items: stats ──

async def get_stats(owner_id: int) -> dict:
    """Return aggregate statistics for saved items."""
    items = await list_all_saves(owner_id)
    total = len(items)

    by_type: dict[str, int] = {}
    for item in items:
        mt = item.get("media_type") or "Unknown"
        by_type[mt] = by_type.get(mt, 0) + 1

    total_size = sum(item.get("file_size") or 0 for item in items)

    oldest = items[-1].get("created_at") if items else None
    newest = items[0].get("created_at") if items else None

    return {
        "total": total,
        "by_type": by_type,
        "size_estimate": total_size,
        "oldest": oldest,
        "newest": newest,
    }


# ── saved_items: updates ──

def _update_save_field_sync(owner_id: int, code: str, field: str, value) -> dict | None:
    target = _query_save_sync(code)
    if not target or target.get("owner_id") != owner_id:
        return None
    db = get_db()
    if db:
        try:
            sc = target.get("save_code")
            res = (
                db.table("saved_items")
                .update({field: value})
                .eq("owner_id", owner_id)
                .eq("save_code", sc)
                .execute()
            )
            return res.data[0] if (res.data or []) else None
        except Exception as exc:
            logger.error("[SAVE_DB] update_save_field FAILED: %s", exc)
    target[field] = value
    return target


async def update_save_field(owner_id: int, code: str, field: str, value) -> dict | None:
    """Update a single field on a saved_items row by save_code."""
    try:
        return await _run_sync(_update_save_field_sync, owner_id, code, field, value)
    except Exception as exc:
        logger.error("[SAVE_DB] update_save_field FAILED: %s", exc)
        return None


def _count_saves_with_filter_sync(owner_id: int, save_type: str | None) -> int:
    db = get_db()
    if db:
        try:
            q = db.table("saved_items").select("id", count="exact").eq("owner_id", owner_id)
            if save_type:
                q = q.eq("save_type", save_type)
            result = q.execute()
            return result.count or 0
        except Exception as exc:
            logger.error("[SAVE_DB] count_saves FAILED: %s", exc)
    items = [s for s in _fallback["saved_items"] if s.get("owner_id") == owner_id]
    if save_type:
        items = [s for s in items if s.get("save_type") == save_type]
    return len(items)


async def count_saves(owner_id: int, save_type: str | None = None) -> int:
    try:
        return await _run_sync(_count_saves_with_filter_sync, owner_id, save_type)
    except Exception as exc:
        logger.error("[SAVE_DB] count_saves FAILED: %s", exc)
        return 0


# ── bio_state ──

def _get_bio_state_sync(owner_id: int) -> dict | None:
    db = get_db()
    if db:
        try:
            result = (
                db.table("bio_state")
                .select("*")
                .eq("owner_id", owner_id)
                .maybe_single()
                .execute()
            )
            return result.data
        except Exception as exc:
            logger.error("[SAVE_DB] get_bio_state FAILED: %s", exc)
    return _fallback["bio_state"].get(owner_id)


async def get_bio_state(owner_id: int) -> dict | None:
    try:
        return await _run_sync(_get_bio_state_sync, owner_id)
    except Exception as exc:
        logger.error("[SAVE_DB] get_bio_state FAILED: %s", exc)
        return _fallback["bio_state"].get(owner_id)


def _get_or_create_bio_state_sync(owner_id: int) -> dict:
    state = _get_bio_state_sync(owner_id)
    if state:
        return state

    default = {
        "owner_id": owner_id,
        "template": "🕒 {time} | 💭 {mood}",
        "mood": "😊",
        "custom_text": "",
        "is_active": False,
        "last_bio": "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    db = get_db()
    if db:
        try:
            db.table("bio_state").insert(default).execute()
            result = (
                db.table("bio_state")
                .select("*")
                .eq("owner_id", owner_id)
                .maybe_single()
                .execute()
            )
            if result.data:
                return result.data
        except Exception as exc:
            logger.error("[SAVE_DB] get_or_create_bio_state FAILED: %s", exc)
    _fallback["bio_state"][owner_id] = default
    return default


async def get_or_create_bio_state(owner_id: int) -> dict:
    try:
        return await _run_sync(_get_or_create_bio_state_sync, owner_id)
    except Exception as exc:
        logger.error("[SAVE_DB] get_or_create_bio_state FAILED: %s", exc)
        return _fallback["bio_state"].get(owner_id) or {
            "owner_id": owner_id,
            "template": "🕒 {time} | 💭 {mood}",
            "mood": "😊",
            "custom_text": "",
            "is_active": False,
            "last_bio": "",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }


def _update_bio_state_sync(owner_id: int, updates: dict) -> None:
    db = get_db()
    if db:
        try:
            db.table("bio_state").update(updates).eq("owner_id", owner_id).execute()
            return
        except Exception as exc:
            logger.error("[SAVE_DB] update_bio_state FAILED: %s", exc)
    state = _fallback["bio_state"].get(owner_id, {})
    state.update(updates)
    _fallback["bio_state"][owner_id] = state


async def update_bio_state(owner_id: int, updates: dict) -> None:
    try:
        await _run_sync(_update_bio_state_sync, owner_id, updates)
    except Exception as exc:
        logger.error("[SAVE_DB] update_bio_state FAILED: %s", exc)


# ── username_state ──

def _get_username_state_sync(owner_id: int) -> dict | None:
    db = get_db()
    if db:
        try:
            result = (
                db.table("username_state")
                .select("*")
                .eq("owner_id", owner_id)
                .maybe_single()
                .execute()
            )
            if result.data:
                return result.data
            logger.info("USERNAME_DB_ROW_NOT_FOUND owner_id=%s", owner_id)
            return None
        except Exception as exc:
            logger.error("[SAVE_DB] get_username_state FAILED: %s", exc)
            return None
    return _fallback.get("username_state", {}).get(owner_id)


async def get_username_state(owner_id: int) -> dict | None:
    try:
        return await _run_sync(_get_username_state_sync, owner_id)
    except Exception as exc:
        logger.error("[SAVE_DB] get_username_state FAILED: %s", exc)
        return _fallback.get("username_state", {}).get(owner_id)


def _get_or_create_username_state_sync(owner_id: int) -> dict:
    state = _get_username_state_sync(owner_id)
    if state:
        return state

    default = {
        "owner_id": owner_id,
        "template": "{time} | {mood}",
        "mood": "😊",
        "custom_text": "",
        "is_active": False,
        "last_name": "",
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }

    db = get_db()
    if db:
        try:
            db.table("username_state").insert(default).execute()
            result = (
                db.table("username_state")
                .select("*")
                .eq("owner_id", owner_id)
                .maybe_single()
                .execute()
            )
            if result.data:
                return result.data
        except Exception as exc:
            logger.error("[SAVE_DB] get_or_create_username_state FAILED: %s", exc)
    if "username_state" not in _fallback:
        _fallback["username_state"] = {}
    _fallback["username_state"][owner_id] = default
    return default


async def get_or_create_username_state(owner_id: int) -> dict:
    try:
        return await _run_sync(_get_or_create_username_state_sync, owner_id)
    except Exception as exc:
        logger.error("[SAVE_DB] get_or_create_username_state FAILED: %s", exc)
        if "username_state" not in _fallback:
            _fallback["username_state"] = {}
        return _fallback["username_state"].get(owner_id) or {
            "owner_id": owner_id,
            "template": "{time} | {mood}",
            "mood": "😊",
            "custom_text": "",
            "is_active": False,
            "last_name": "",
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }


def _update_username_state_sync(owner_id: int, updates: dict) -> None:
    db = get_db()
    if db:
        try:
            db.table("username_state").update(updates).eq("owner_id", owner_id).execute()
            return
        except Exception as exc:
            logger.error("[SAVE_DB] update_username_state FAILED: %s", exc)
    if "username_state" not in _fallback:
        _fallback["username_state"] = {}
    state = _fallback["username_state"].get(owner_id, {})
    state.update(updates)
    _fallback["username_state"][owner_id] = state


async def update_username_state(owner_id: int, updates: dict) -> None:
    try:
        await _run_sync(_update_username_state_sync, owner_id, updates)
    except Exception as exc:
        logger.error("[SAVE_DB] update_username_state FAILED: %s", exc)


# ── bot_logs: reads/cleanup ──

def _count_logs_sync(owner_id: int) -> int:
    db = get_db()
    if db:
        try:
            result = (
                db.table("bot_logs")
                .select("id", count="exact")
                .eq("owner_id", owner_id)
                .execute()
            )
            return result.count or 0
        except Exception as exc:
            logger.error("[SAVE_DB] count_logs FAILED: %s", exc)
    return len([l for l in _fallback["bot_logs"] if l.get("owner_id") == owner_id])


async def count_logs(owner_id: int) -> int:
    try:
        return await _run_sync(_count_logs_sync, owner_id)
    except Exception as exc:
        logger.error("[SAVE_DB] count_logs FAILED: %s", exc)
        return 0


def _list_logs_sync(owner_id: int, limit: int) -> list:
    db = get_db()
    if db:
        try:
            result = (
                db.table("bot_logs")
                .select("*")
                .eq("owner_id", owner_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return result.data or []
        except Exception as exc:
            logger.error("[SAVE_DB] list_logs FAILED: %s", exc)
    logs = [l for l in _fallback["bot_logs"] if l.get("owner_id") == owner_id]
    return logs[-limit:] if limit > 0 else logs


async def list_logs(owner_id: int, limit: int = 100) -> list:
    try:
        return await _run_sync(_list_logs_sync, owner_id, limit)
    except Exception as exc:
        logger.error("[SAVE_DB] list_logs FAILED: %s", exc)
        return []


def _clean_logs_sync(owner_id: int, days: int) -> int:
    db = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    if db:
        try:
            result = (
                db.table("bot_logs")
                .delete()
                .eq("owner_id", owner_id)
                .lt("created_at", cutoff)
                .execute()
            )
            return len(result.data) if result.data else 0
        except Exception as exc:
            logger.error("[SAVE_DB] clean_logs FAILED: %s", exc)
    before = len(_fallback["bot_logs"])
    _fallback["bot_logs"] = [
        l for l in _fallback["bot_logs"]
        if l.get("owner_id") != owner_id or l.get("created_at", "") >= cutoff
    ]
    return before - len(_fallback["bot_logs"])


async def clean_logs(owner_id: int, days: int = 7) -> int:
    try:
        return await _run_sync(_clean_logs_sync, owner_id, days)
    except Exception as exc:
        logger.error("[SAVE_DB] clean_logs FAILED: %s", exc)
        return 0


# ── helpers ──

def _safe_payload(data: dict) -> str:
    """Render a payload dict for logging, truncating long fields."""
    try:
        redacted = {}
        for k, v in data.items():
            if k == "caption" and isinstance(v, str) and len(v) > 80:
                redacted[k] = v[:80] + "…"
            elif k == "tags" and isinstance(v, list):
                redacted[k] = v
            else:
                redacted[k] = v
        return repr(redacted)
    except Exception:
        return "<unreprable>"


def _safe_row(row: dict | None) -> str:
    """Render an inserted row for logging."""
    if row is None:
        return "None"
    try:
        return repr({k: row.get(k) for k in ("id", "save_code", "owner_id")})
    except Exception:
        return "<unreprable>"
