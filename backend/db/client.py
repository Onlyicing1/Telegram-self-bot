"""
Database layer — Supabase if available, in-memory fallback otherwise.

Every public function is async. Supabase's synchronous .execute() calls are
offloaded to a worker thread via asyncio.to_thread() with a hard timeout, so
a stalled HTTP request never blocks the event loop. If Supabase is missing or
fails, all operations silently degrade to in-memory storage.
"""
import asyncio
import logging
import os
import random
import string
import time
from datetime import datetime, timedelta, timezone

logger = logging.getLogger(__name__)

_client = None
_available = False
_fallback: dict = {"saved_items": [], "bio_state": {}, "bot_logs": []}
_save_code_lock = asyncio.Lock()
_initialised = False

_SHORT_CODE_PREFIX = "S"
_SHORT_CODE_NUM_LEN = 4
_SHORT_CODE_ALPHABET = string.ascii_uppercase + string.digits

_DB_TIMEOUT = 20.0


def _check_available() -> bool:
    return bool(os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY"))


def get_db():
    """Return the Supabase client, or None if unavailable."""
    global _client, _available, _initialised
    if _initialised:
        return _client if _available else None

    _initialised = True

    if not _check_available():
        logger.warning("Supabase env vars not set — using in-memory fallback.")
        _available = False
        return None

    try:
        from supabase import create_client
        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_ROLE_KEY"],
        )
        _available = True
        logger.info("Supabase client initialised.")
        return _client
    except Exception as exc:
        logger.warning("Supabase init failed (%s) — using in-memory fallback.", exc)
        _available = False
        return None


def is_available() -> bool:
    return _available


async def _run_sync(fn, *args, **kwargs):
    """Run a synchronous Supabase call in a worker thread with a hard timeout."""
    return await asyncio.wait_for(
        asyncio.to_thread(fn, *args, **kwargs),
        timeout=_DB_TIMEOUT,
    )


async def log(owner_id: int, level: str, message: str, context: dict | None = None) -> None:
    try:
        entry = {
            "owner_id": owner_id,
            "level": level,
            "message": message,
            "context": context or {},
            "created_at": datetime.now(timezone.utc).isoformat(),
        }
        db = get_db()
        if db:
            await _run_sync(lambda: db.table("bot_logs").insert(entry).execute())
        else:
            entry["id"] = len(_fallback["bot_logs"]) + 1
            _fallback["bot_logs"].append(entry)
    except Exception:
        logger.warning("db.log failed — using fallback", exc_info=True)
        entry["id"] = len(_fallback["bot_logs"]) + 1
        _fallback["bot_logs"].append(entry)


async def get_next_save_code() -> str:
    """Generate a compact save code (e.g. S0001). Sequential with collision fallback."""
    async with _save_code_lock:
        db = get_db()
        count = 0
        if db:
            try:
                result = await _run_sync(
                    lambda: db.table("saved_items").select("id", count="exact").execute()
                )
                count = result.count or 0
            except Exception:
                count = len(_fallback["saved_items"])
        else:
            count = len(_fallback["saved_items"])

        sequential = f"{_SHORT_CODE_PREFIX}{count + 1:0{_SHORT_CODE_NUM_LEN}d}"
        if await _is_code_free(sequential):
            return sequential

        for _ in range(50):
            rand_code = _SHORT_CODE_PREFIX + "".join(
                random.choices(_SHORT_CODE_ALPHABET, k=4)
            )
            if await _is_code_free(rand_code):
                return rand_code

        return sequential


async def _is_code_free(code: str) -> bool:
    db = get_db()
    if db:
        try:
            res = await _run_sync(
                lambda: db.table("saved_items")
                .select("id")
                .or_(f"save_code.eq.{code},short_code.eq.{code}")
                .limit(1)
                .execute()
            )
            return not (res.data or [])
        except Exception:
            pass
    for item in _fallback["saved_items"]:
        if item.get("save_code") == code or item.get("short_code") == code:
            return False
    return True


async def insert_save(data: dict) -> dict | None:
    db = get_db()
    if db:
        try:
            result = await _run_sync(lambda: db.table("saved_items").insert(data).execute())
            return result.data[0] if result.data else None
        except Exception as exc:
            logger.warning("Supabase insert_save failed (%s) — using fallback.", exc)
    data["id"] = len(_fallback["saved_items"]) + 1
    _fallback["saved_items"].append(data)
    return data


async def query_save(save_code: str) -> dict | None:
    """Look up by short_code OR legacy save_code."""
    code = save_code.upper()
    db = get_db()
    if db:
        try:
            result = await _run_sync(
                lambda: db.table("saved_items")
                .select("*")
                .or_(f"short_code.eq.{code},save_code.eq.{code}")
                .maybe_single()
                .execute()
            )
            return result.data
        except Exception as exc:
            logger.warning("Supabase query_save failed (%s) — using fallback.", exc)
    for item in _fallback["saved_items"]:
        sc = (item.get("short_code") or "").upper()
        lc = (item.get("save_code") or "").upper()
        if sc == code or lc == code:
            return item
    return None


async def list_saves(owner_id: int, limit: int = 50, offset: int = 0) -> tuple[list, int]:
    db = get_db()
    if db:
        try:
            result = await _run_sync(
                lambda: db.table("saved_items")
                .select("*")
                .eq("owner_id", owner_id)
                .order("created_at", desc=True)
                .range(offset, offset + limit - 1)
                .execute()
            )
            count_res = await _run_sync(
                lambda: db.table("saved_items")
                .select("id", count="exact")
                .eq("owner_id", owner_id)
                .execute()
            )
            return result.data or [], count_res.count or 0
        except Exception as exc:
            logger.warning("Supabase list_saves failed (%s) — using fallback.", exc)
    items = [s for s in _fallback["saved_items"] if s.get("owner_id") == owner_id]
    total = len(items)
    return items[offset:offset + limit], total


async def list_recent_saves(owner_id: int, limit: int = 10) -> list:
    db = get_db()
    if db:
        try:
            result = await _run_sync(
                lambda: db.table("saved_items")
                .select("short_code,save_code,save_type,media_type,file_name,mime_type,created_at")
                .eq("owner_id", owner_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return result.data or []
        except Exception as exc:
            logger.warning("Supabase list_recent_saves failed (%s) — using fallback.", exc)
    items = sorted(
        [s for s in _fallback["saved_items"] if s.get("owner_id") == owner_id],
        key=lambda x: x.get("created_at", ""),
        reverse=True,
    )
    return items[:limit]


async def search_saves(owner_id: int, query: str, limit: int = 20) -> list:
    pattern = f"%{query}%"
    db = get_db()
    if db:
        try:
            result = await _run_sync(
                lambda: db.table("saved_items")
                .select("short_code,save_code,save_type,media_type,file_name,mime_type,created_at")
                .eq("owner_id", owner_id)
                .or_(
                    f"caption.ilike.{pattern},"
                    f"file_name.ilike.{pattern},"
                    f"save_code.ilike.{pattern},"
                    f"short_code.ilike.{pattern},"
                    f"mime_type.ilike.{pattern}"
                )
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return result.data or []
        except Exception as exc:
            logger.warning("Supabase search_saves failed (%s) — using fallback.", exc)
    q_lower = query.lower()
    matches = []
    for item in _fallback["saved_items"]:
        if item.get("owner_id") != owner_id:
            continue
        haystack = " ".join(str(item.get(k) or "") for k in
                             ("caption", "file_name", "save_code", "short_code", "mime_type")).lower()
        if q_lower in haystack:
            matches.append(item)
    matches.sort(key=lambda x: x.get("created_at", ""), reverse=True)
    return matches[:limit]


async def delete_save(owner_id: int, code: str) -> dict | None:
    target = await query_save(code)
    if not target or target.get("owner_id") != owner_id:
        return None
    db = get_db()
    if db:
        try:
            sc = target.get("short_code") or target.get("save_code")
            col = "short_code" if target.get("short_code") else "save_code"
            res = await _run_sync(
                lambda: db.table("saved_items")
                .delete()
                .eq("owner_id", owner_id)
                .eq(col, sc)
                .execute()
            )
            return target if (res.data or []) else None
        except Exception as exc:
            logger.warning("Supabase delete_save failed (%s) — using fallback.", exc)
    _fallback["saved_items"] = [
        s for s in _fallback["saved_items"]
        if not (s.get("short_code") == target.get("short_code")
                or s.get("save_code") == target.get("save_code"))
    ]
    return target


async def count_saves(owner_id: int, save_type: str | None = None) -> int:
    db = get_db()
    if db:
        try:
            def _q():
                q = db.table("saved_items").select("id", count="exact").eq("owner_id", owner_id)
                if save_type:
                    q = q.eq("save_type", save_type)
                return q.execute()
            result = await _run_sync(_q)
            return result.count or 0
        except Exception as exc:
            logger.warning("Supabase count_saves failed (%s) — using fallback.", exc)
    items = [s for s in _fallback["saved_items"] if s.get("owner_id") == owner_id]
    if save_type:
        items = [s for s in items if s.get("save_type") == save_type]
    return len(items)


async def get_bio_state(owner_id: int) -> dict | None:
    db = get_db()
    if db:
        try:
            result = await _run_sync(
                lambda: db.table("bio_state")
                .select("*")
                .eq("owner_id", owner_id)
                .maybe_single()
                .execute()
            )
            return result.data
        except Exception as exc:
            logger.warning("Supabase get_bio_state failed (%s) — using fallback.", exc)
    return _fallback["bio_state"].get(owner_id)


async def get_or_create_bio_state(owner_id: int) -> dict:
    state = await get_bio_state(owner_id)
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
            await _run_sync(lambda: db.table("bio_state").insert(default).execute())
            result = await _run_sync(
                lambda: db.table("bio_state")
                .select("*")
                .eq("owner_id", owner_id)
                .maybe_single()
                .execute()
            )
            if result.data:
                return result.data
        except Exception as exc:
            logger.warning("Supabase get_or_create_bio_state failed (%s) — using fallback.", exc)
    _fallback["bio_state"][owner_id] = default
    return default


async def update_bio_state(owner_id: int, updates: dict) -> None:
    db = get_db()
    if db:
        try:
            await _run_sync(
                lambda: db.table("bio_state").update(updates).eq("owner_id", owner_id).execute()
            )
            return
        except Exception as exc:
            logger.warning("Supabase update_bio_state failed (%s) — using fallback.", exc)
    state = _fallback["bio_state"].get(owner_id, {})
    state.update(updates)
    _fallback["bio_state"][owner_id] = state


async def count_logs(owner_id: int) -> int:
    db = get_db()
    if db:
        try:
            result = await _run_sync(
                lambda: db.table("bot_logs")
                .select("id", count="exact")
                .eq("owner_id", owner_id)
                .execute()
            )
            return result.count or 0
        except Exception as exc:
            logger.warning("Supabase count_logs failed (%s) — using fallback.", exc)
    return len([l for l in _fallback["bot_logs"] if l.get("owner_id") == owner_id])


async def list_logs(owner_id: int, limit: int = 100) -> list:
    db = get_db()
    if db:
        try:
            result = await _run_sync(
                lambda: db.table("bot_logs")
                .select("*")
                .eq("owner_id", owner_id)
                .order("created_at", desc=True)
                .limit(limit)
                .execute()
            )
            return result.data or []
        except Exception as exc:
            logger.warning("Supabase list_logs failed (%s) — using fallback.", exc)
    logs = [l for l in _fallback["bot_logs"] if l.get("owner_id") == owner_id]
    return logs[-limit:] if limit > 0 else logs


async def clean_logs(owner_id: int, days: int = 7) -> int:
    db = get_db()
    cutoff = (datetime.now(timezone.utc) - timedelta(days=days)).isoformat()
    if db:
        try:
            result = await _run_sync(
                lambda: db.table("bot_logs")
                .delete()
                .eq("owner_id", owner_id)
                .lt("created_at", cutoff)
                .execute()
            )
            return len(result.data) if result.data else 0
        except Exception as exc:
            logger.warning("Supabase clean_logs failed (%s) — using fallback.", exc)
    before = len(_fallback["bot_logs"])
    _fallback["bot_logs"] = [
        l for l in _fallback["bot_logs"]
        if l.get("owner_id") != owner_id or l.get("created_at", "") >= cutoff
    ]
    return before - len(_fallback["bot_logs"])
