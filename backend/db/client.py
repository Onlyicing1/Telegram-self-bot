"""
Database layer — Supabase if available, in-memory fallback otherwise.

The singleton client is initialised on first access. If Supabase env
vars are missing or the connection fails, all operations degrade to
in-memory storage so the bot never crashes.

CRITICAL: All public functions that touch Supabase are async and run
the synchronous HTTP calls on one bounded, reusable worker pool
(``run_sync_db``) with a bounded timeout. The supabase-py library uses
httpx synchronously — calling db.table(...).execute() directly in an
asyncio coroutine blocks the entire event loop until the HTTP response
arrives. If the Supabase REST API is slow or the TCP connection stalls,
the whole runtime freezes (no commands, no heartbeat, no bio updates).

By running each DB operation on that bounded pool with a timeout the
event loop stays responsive — and the process's Supabase concurrency,
and the sockets the one shared client must keep alive, stay bounded —
even when Supabase is slow or unreachable. The transport's own deadline
is pinned below the dispatch budget so a slow call can never pin a
worker thread longer than the application is willing to wait.
"""
import asyncio
import logging
import os
import random
import ssl
import string
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone

from backend.diagnostics import record_event
from backend.helper import symbols as sym

logger = logging.getLogger(__name__)

_client = None
_available = False
_fallback: dict = {
    "saved_items": [],
    "bio_state": {},
    "bot_logs": [],
    "username_state": {},
    "emoji_library": [],
    "emoji_categories": [],
    "emoji_mappings": [],
    "emoji_state": {},
    "emoji_chat_overrides": [],
    "bot_settings": {},
    "private_message_config": {},
    "private_message_senders": [],
    "aed_config": {},
    "aed_archive_entries": [],
}
_save_code_lock = asyncio.Lock()
_initialised = False

_SHORT_CODE_PREFIX = "S"
_SHORT_CODE_NUM_LEN = 4
_SHORT_CODE_ALPHABET = string.ascii_uppercase + string.digits

_DB_TIMEOUT = 10.0

# The Supabase HTTP transport's OWN deadline. It must be strictly shorter than
# ``_DB_TIMEOUT``: the watchdog abandons the awaiting coroutine at
# ``_DB_TIMEOUT``, but a thread blocked inside a synchronous HTTP call cannot be
# cancelled — only the transport deadline releases it. supabase-py defaults the
# postgrest client to a 120s timeout, so without this override a slow call kept
# a worker thread and its pooled connection pinned twelve times longer than the
# application was ever willing to wait, and every abandoned call pushed the next
# one onto a new thread and a new socket.
_DB_HTTP_TIMEOUT = 8.0

# The one bounded pool for synchronous Supabase work. Threads are reused across
# calls, so concurrent Supabase calls — and the number of sockets the single
# shared httpx client must keep alive — stay bounded no matter how many callers
# (DB layer, task repository, AI persistence) are active.
_DB_MAX_WORKERS = 4
_db_executor: ThreadPoolExecutor | None = None
_db_executor_lock = threading.Lock()


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
        from supabase import ClientOptions, create_client
        # The transport deadline is pinned to the application's own dispatch
        # budget so no synchronous call can outlive the operation that owns it.
        _client = create_client(
            os.environ["SUPABASE_URL"],
            os.environ["SUPABASE_SERVICE_ROLE_KEY"],
            ClientOptions(postgrest_client_timeout=_DB_HTTP_TIMEOUT),
        )
        _available = True
        logger.info(
            "[SAVE_DB] Supabase client initialised. runtime=python-%s ssl=%s",
            f"{sys.version_info.major}.{sys.version_info.minor}.{sys.version_info.micro}",
            ssl.OPENSSL_VERSION,
        )
        return _client
    except Exception as exc:
        logger.error("[SAVE_DB] Supabase init FAILED (%s) — using in-memory fallback.", exc)
        _available = False
        return None


def is_available() -> bool:
    return _available


def _get_db_executor() -> ThreadPoolExecutor:
    """Return the one bounded pool used for synchronous Supabase work."""
    global _db_executor
    executor = _db_executor
    if executor is None:
        with _db_executor_lock:
            if _db_executor is None:
                _db_executor = ThreadPoolExecutor(
                    max_workers=_DB_MAX_WORKERS,
                    thread_name_prefix="lifeos-supabase",
                )
            executor = _db_executor
    return executor


# In-flight duration of one synchronous transport call, tagged on the failure
# that comes out of the worker. A would-block that returns immediately and a
# transport deadline that expired after eight seconds produce the SAME errno
# chain, so the duration is the only in-process evidence that separates them.
# Diagnostic attribute only: nothing is retried, delayed or re-classified.
_DB_INFLIGHT_ATTR = "_lifeos_db_inflight_seconds"

# Production also emits a separate ``ConnectionTerminated error_code:1`` line,
# and whether it is causally related to the errno-11 read failures is still
# unmeasured. Recording the most recent PEER/HTTP-2 termination where every
# synchronous Supabase call already passes (this single dispatch) makes that
# relationship readable in the next episode instead of guessed at: the
# local-resource line reports the gap, or ``none`` when no teardown was ever
# seen in this process. Bounded bookkeeping only — no retry, delay, deadline,
# worker-count, lifecycle or classification change, and no payload captured.
_TRANSPORT_TEARDOWN_CLASSES = frozenset({"ConnectionTerminated", "RemoteProtocolError"})
_MAX_TEARDOWN_CHAIN_LINKS = 8
_last_transport_teardown: float | None = None


def _note_transport_teardown(exc) -> None:
    """Record the instant of a peer/HTTP-2 termination seen in a failure chain."""
    global _last_transport_teardown
    seen: set[int] = set()
    current = exc
    links = 0
    while current is not None and id(current) not in seen and links < _MAX_TEARDOWN_CHAIN_LINKS:
        seen.add(id(current))
        links += 1
        if type(current).__name__ in _TRANSPORT_TEARDOWN_CLASSES:
            _last_transport_teardown = time.monotonic()
            return
        current = current.__cause__ or current.__context__


def seconds_since_transport_teardown() -> float | None:
    """Seconds since the last observed transport teardown (``None`` if never)."""
    last = _last_transport_teardown
    if last is None:
        return None
    elapsed = time.monotonic() - last
    return elapsed if elapsed >= 0 else 0.0


def _timed_db_call(fn, args, kwargs):
    """Run one synchronous Supabase call and tag its in-flight duration.

    Measured INSIDE the worker so executor queueing is excluded: the value is
    the time the transport itself spent before failing.
    """
    started = time.monotonic()
    try:
        return fn(*args, **kwargs)
    except BaseException as exc:
        try:
            setattr(exc, _DB_INFLIGHT_ATTR, time.monotonic() - started)
        except Exception:
            pass
        try:
            _note_transport_teardown(exc)
        except Exception:
            pass
        raise


def db_inflight_seconds(exc) -> float | None:
    """How long the failing transport call was in flight, when it is known.

    ``None`` means the failure was raised OUTSIDE the worker (for example the
    dispatch deadline fired while the thread was still blocked in the
    transport), so no transport duration exists — reporting one would be a
    fabrication.
    """
    value = getattr(exc, _DB_INFLIGHT_ATTR, None)
    if isinstance(value, (int, float)) and value >= 0:
        return float(value)
    return None


async def run_sync_db(fn, *args, timeout=_DB_TIMEOUT, **kwargs):
    """The ONE bounded dispatch for synchronous Supabase HTTP work.

    ``asyncio.to_thread`` draws a fresh worker from the event loop's shared
    default executor, which every other subsystem also uses, and applies no
    bound of its own — a burst of DB, task and persistence calls could occupy an
    unbounded share of that pool and keep that many sockets alive on the one
    shared client. Supabase work therefore goes through this single bounded,
    reusable pool instead. Uses the centralized operation watchdog so a stuck
    operation emits structured OP_TIMEOUT diagnostics instead of dying silently.
    """
    from backend.runtime.operation_watchdog import guarded_await

    op_name = getattr(fn, "__name__", "db_unknown")
    loop = asyncio.get_running_loop()
    coro = loop.run_in_executor(
        _get_db_executor(), _timed_db_call, fn, args, kwargs
    )
    return await guarded_await(coro, name=f"db:{op_name}", timeout=timeout)


async def _run_sync(fn, *args, **kwargs):
    """Run a synchronous DB function on the bounded Supabase pool."""
    return await run_sync_db(fn, *args, **kwargs)


def shutdown_db_executor() -> None:
    """Release the bounded Supabase pool (deterministic shutdown only).

    Threads blocked inside the HTTP transport cannot be interrupted, but the
    transport deadline guarantees they return, so shutdown does not wait.
    """
    global _db_executor
    executor = _db_executor
    if executor is None:
        return
    _db_executor = None
    executor.shutdown(wait=False, cancel_futures=True)


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


# ── saved_items: deterministic saved-item resolver fetch (Save V2) ──
#
# One owner-scoped, bounded, explicit-ORDER-BY candidate fetch per resolver
# tier. The matching LOGIC lives in ``retrieve_service.resolve_saved_items``;
# this layer only expresses it as a PostgREST filter so owner scoping and
# bounding happen IN THE QUERY (never by filtering fetched rows in Python).
#
# The logic tree is a single ``or=`` param containing an explicit
# ``and(...)`` of per-token ``or(...)`` groups (tokens AND, variants OR).
# Using one param removes any dependency on how a server combines repeated
# ``or=`` params.

_RESOLVE_COLUMNS = (
    "save_code,display_name,file_name,media_type,mime_type,file_size,tags,created_at"
)

# Resolver tiers: display_name only; whole tags only; name/file/tag together.
_RESOLVE_TIERS = ("name", "tag", "mixed")

# PostgREST filter syntax is stripped from user text before it is placed in
# the logic tree, so a query can only ever match as literal content inside its
# own condition — it can never alter the tree. ``%`` survives a pattern on
# purpose (it is the ILIKE wildcard the resolver adds around a token); a
# user-typed ``%`` can only broaden the prefilter, and the authoritative
# comparison in ``retrieve_service`` still decides the match.
_PATTERN_STRIP = str.maketrans({c: None for c in ",()\\"})
_TAG_TERM_STRIP = str.maketrans({c: None for c in ',(){}"\\'})


def resolve_pattern(token: str) -> str:
    """Sanitize one raw token for use INSIDE an ILIKE pattern."""
    return str(token).translate(_PATTERN_STRIP)


def resolve_tag_term(token: str) -> str:
    """One sanitized whole-tag term for ``tags.cs.{term}``."""
    return str(token).translate(_TAG_TERM_STRIP)


def _resolve_condition_groups(tier: str, token_groups: list, joined_tags: list) -> list[list[str]]:
    """Per-token OR-groups of PostgREST conditions for one tier."""
    groups: list[list[str]] = []
    for group in token_groups or []:
        conditions: list[str] = []
        patterns = list(group.get("patterns") or []) if isinstance(group, dict) else []
        if tier in ("name", "mixed"):
            conditions.extend(f"display_name.ilike.{p}" for p in patterns)
        if tier == "mixed":
            conditions.extend(f"file_name.ilike.{p}" for p in patterns)
        if tier in ("tag", "mixed"):
            terms = list(group.get("tags") or []) if isinstance(group, dict) else []
            # A whole-query tag candidate ("semester 2" → "semester-2")
            # satisfies every token group, so it is offered to each of them.
            terms.extend(joined_tags or [])
            conditions.extend(f"tags.cs.{{{t}}}" for t in dict.fromkeys(t for t in terms if t))
        conditions = list(dict.fromkeys(c for c in conditions if c))
        if conditions:
            groups.append(conditions)
    return groups


def resolve_logic_expression(groups: list[list[str]]) -> str:
    """One PostgREST logic tree: tokens ANDed, each token's variants ORed."""
    if not groups:
        return ""
    if len(groups) == 1 and len(groups[0]) == 1:
        return groups[0][0]
    items = [f"or({','.join(g)})" if len(g) > 1 else g[0] for g in groups]
    return items[0] if len(items) == 1 else f"and({','.join(items)})"


def _resolve_saves_sync(owner_id: int, tier: str, token_groups: list, joined_tags: list, limit: int) -> list:
    groups = _resolve_condition_groups(tier, token_groups, joined_tags)
    if not groups or not limit or limit <= 0:
        return []
    logic = resolve_logic_expression(groups)
    db = get_db()
    if db:
        try:
            result = (
                db.table("saved_items")
                .select(_RESOLVE_COLUMNS)
                .eq("owner_id", owner_id)
                .or_(logic)
                .order("created_at", desc=True)
                .order("save_code")
                .limit(limit)
                .execute()
            )
            record_event("database", f"resolve saved_items ({tier})", 0, "SUCCESS")
            return result.data or []
        except Exception as exc:
            logger.error("[SAVE_DB] resolve_saves(%s) FAILED: %s", tier, exc)
            record_event("database", f"resolve saved_items ({tier})", 0, "ERROR", str(exc))

    # In-memory fallback (the same Supabase-or-fallback contract as every
    # other read): the SAME owner-scoped, tier-scoped prefilter, the same
    # bound, and the same created_at DESC / save_code ASC ordering.
    rows = [r for r in _fallback["saved_items"] if r.get("owner_id") == owner_id]
    rows = [r for r in rows if _fallback_resolve_match(tier, r, token_groups, joined_tags)]
    rows.sort(key=lambda r: str(r.get("save_code") or ""))
    rows.sort(key=lambda r: str(r.get("created_at") or ""), reverse=True)
    return rows[:limit]


def _fallback_resolve_match(tier: str, row: dict, token_groups: list, joined_tags: list) -> bool:
    """Permissive in-memory mirror of the PostgREST prefilter (superset only)."""
    def name_hit(patterns: list, value) -> bool:
        text = str(value or "").casefold()
        return any(str(p).strip("%").casefold() in text for p in patterns)

    tags = [str(t).casefold() for t in (row.get("tags") or []) if str(t)]

    def tag_hit(terms: list) -> bool:
        wanted = [str(t).casefold() for t in terms if str(t)]
        return any(w == tag for w in wanted for tag in tags)

    for group in token_groups or []:
        patterns = list(group.get("patterns") or []) if isinstance(group, dict) else []
        terms = list(group.get("tags") or []) if isinstance(group, dict) else []
        terms = terms + list(joined_tags or [])
        if tier == "name":
            ok = name_hit(patterns, row.get("display_name"))
        elif tier == "tag":
            ok = tag_hit(terms)
        else:
            ok = (
                name_hit(patterns, row.get("display_name"))
                or name_hit(patterns, row.get("file_name"))
                or tag_hit(terms)
            )
        if not ok:
            return False
    return True


async def resolve_saves(owner_id: int, tier: str, token_groups: list, joined_tags: list, limit: int) -> list:
    """Owner-scoped bounded candidate fetch for one resolver tier.

    ``tier``: ``name`` (display_name only), ``tag`` (whole tags only) or
    ``mixed`` (display_name / file_name / tags). ``token_groups`` is one
    entry per query token: ``{"patterns": [...], "tags": [...]}``. The
    result is bounded by ``limit`` and ordered deterministically; failures
    degrade to the in-memory store per the project's DB contract.
    """
    if tier not in _RESOLVE_TIERS:
        raise ValueError(f"unknown resolver tier: {tier!r}")
    try:
        return await _run_sync(
            _resolve_saves_sync, owner_id, tier, token_groups, joined_tags, limit
        )
    except Exception as exc:
        logger.error("[SAVE_DB] resolve_saves(%s) FAILED: %s", tier, exc)
        record_event("database", f"resolve saved_items ({tier})", 0, "ERROR", str(exc))
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

def _update_save_fields_sync(owner_id: int, code: str, fields: dict) -> dict | None:
    """Owner-scoped update of one or more saved_items columns in ONE statement.

    A single statement is what keeps a multi-field change (the saved-item
    synchronizer moving the item's Telegram location to its replacement
    message) from landing half-applied: the row either carries every new value
    or none of them.
    """
    target = _query_save_sync(code)
    if not target or target.get("owner_id") != owner_id:
        return None
    db = get_db()
    if db:
        try:
            sc = target.get("save_code")
            res = (
                db.table("saved_items")
                .update(dict(fields))
                .eq("owner_id", owner_id)
                .eq("save_code", sc)
                .execute()
            )
            return res.data[0] if (res.data or []) else None
        except Exception as exc:
            logger.error("[SAVE_DB] update_save_fields FAILED: %s", exc)
    target.update(fields)
    return target


async def update_save_fields(owner_id: int, code: str, fields: dict) -> dict | None:
    """Update several fields on a saved_items row by save_code."""
    try:
        return await _run_sync(_update_save_fields_sync, owner_id, code, fields)
    except Exception as exc:
        logger.error("[SAVE_DB] update_save_fields FAILED: %s", exc)
        return None


def _update_save_field_sync(owner_id: int, code: str, field: str, value) -> dict | None:
    return _update_save_fields_sync(owner_id, code, {field: value})


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
        "template": sym.BIO_DEFAULT_TEMPLATE,
        "mood": sym.ICON_MOOD,
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
            "template": sym.BIO_DEFAULT_TEMPLATE,
            "mood": sym.ICON_MOOD,
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
        "mood": sym.ICON_MOOD,
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
            "mood": sym.ICON_MOOD,
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


# ── emoji_library (Emoji & Reaction Phase 1) ──

#: Hard bound on the document-id read used for import deduplication. The
#: manual schema's UNIQUE (owner_id, document_id) index is the backstop for
#: libraries beyond this bound.
_EMOJI_MAX_ROWS = 5000


def _list_emoji_document_ids_sync(owner_id: int) -> list[int] | None:
    db = get_db()
    if db is None:
        return [
            int(row["document_id"])
            for row in _fallback["emoji_library"]
            if row.get("owner_id") == owner_id
            and isinstance(row.get("document_id"), int)
            and not isinstance(row.get("document_id"), bool)
        ]
    try:
        result = (
            db.table("emoji_library")
            .select("document_id")
            .eq("owner_id", owner_id)
            .limit(_EMOJI_MAX_ROWS)
            .execute()
        )
        return [
            int(row["document_id"])
            for row in (result.data or [])
            if row.get("document_id") is not None
        ]
    except Exception as exc:
        logger.error("[EMOJI_DB] list_emoji_document_ids FAILED: %s", exc)
        record_event("database", "select emoji_library", 0, "ERROR", str(exc))
        return None


async def list_emoji_document_ids(owner_id: int) -> list[int] | None:
    """Document ids in the owner's emoji library, for import deduplication.

    Returns None ONLY when Supabase is configured but the durable read
    fails: callers must fail closed instead of deduplicating against an
    empty in-memory list (config_store DEGRADED_READ contract). When no
    Supabase is configured the fallback list is the authoritative store.
    """
    try:
        return await _run_sync(_list_emoji_document_ids_sync, owner_id)
    except Exception as exc:
        logger.error("[EMOJI_DB] list_emoji_document_ids FAILED: %s", exc)
        return None


def _insert_emoji_entry_sync(data: dict) -> dict | None:
    row = dict(data)
    if row.get("created_at") is None:
        row["created_at"] = datetime.now(timezone.utc).isoformat()
    db = get_db()
    if db is None:
        store = _fallback["emoji_library"]
        for existing in store:
            if (
                existing.get("owner_id") == row.get("owner_id")
                and existing.get("document_id") == row.get("document_id")
            ):
                logger.warning(
                    "[EMOJI_DB] insert_emoji_entry: duplicate document_id=%s — refused.",
                    row.get("document_id"),
                )
                return None
        row["id"] = len(store) + 1
        store.append(row)
        return row
    try:
        result = db.table("emoji_library").insert(row).execute()
        inserted = result.data[0] if result.data else None
        if inserted is None:
            logger.error(
                "[EMOJI_DB] insert_emoji_entry ERROR: insert() returned no data."
            )
            record_event(
                "database", "insert emoji_library", 0, "ERROR",
                "insert returned no data",
            )
            return None
        record_event("database", "insert emoji_library", 0, "SUCCESS")
        return inserted
    except Exception as exc:
        logger.error("[EMOJI_DB] insert_emoji_entry ERROR: %s", exc, exc_info=True)
        record_event("database", "insert emoji_library", 0, "ERROR", str(exc))
        return None


async def insert_emoji_entry(data: dict) -> dict | None:
    """Insert one emoji_library row. Returns the stored row, or None on
    failure (and None when the fallback already holds the same
    owner/document entry). Never raises. The Supabase path relies on the
    manual UNIQUE (owner_id, document_id) index as its duplicate backstop;
    the service-level import lock keeps classification deterministic.
    """
    try:
        return await _run_sync(_insert_emoji_entry_sync, data)
    except Exception as exc:
        logger.error("[EMOJI_DB] insert_emoji_entry FAILED: %s", exc)
        record_event("database", "insert emoji_library", 0, "ERROR", str(exc))
        return None


def _list_emoji_entries_sync(owner_id: int, limit: int, offset: int) -> tuple[list, int]:
    db = get_db()
    if db:
        try:
            result = (
                db.table("emoji_library")
                .select("*")
                .eq("owner_id", owner_id)
                .order("created_at", desc=True)
                .range(offset, offset + limit - 1)
                .execute()
            )
            count_res = (
                db.table("emoji_library")
                .select("id", count="exact")
                .eq("owner_id", owner_id)
                .execute()
            )
            return result.data or [], count_res.count or 0
        except Exception as exc:
            logger.error("[EMOJI_DB] list_emoji_entries FAILED: %s", exc)
    items = [e for e in _fallback["emoji_library"] if e.get("owner_id") == owner_id]
    total = len(items)
    items = sorted(
        items,
        key=lambda r: (r.get("created_at") or "", r.get("id") or 0),
        reverse=True,
    )
    return items[offset:offset + limit], total


async def list_emoji_entries(owner_id: int, limit: int = 50, offset: int = 0) -> tuple[list, int]:
    """List the owner's emoji-library entries newest-first as (rows, total)."""
    try:
        return await _run_sync(_list_emoji_entries_sync, owner_id, limit, offset)
    except Exception as exc:
        logger.error("[EMOJI_DB] list_emoji_entries FAILED: %s", exc)
        items = [e for e in _fallback["emoji_library"] if e.get("owner_id") == owner_id]
        return items[offset:offset + limit], len(items)


# ── emoji_categories (Emoji & Reaction Phase 2) ──

def _insert_emoji_category_sync(data: dict) -> dict | None:
    row = dict(data)
    now = datetime.now(timezone.utc).isoformat()
    if row.get("created_at") is None:
        row["created_at"] = now
    if row.get("updated_at") is None:
        row["updated_at"] = now
    db = get_db()
    if db is None:
        store = _fallback["emoji_categories"]
        for existing in store:
            if (
                existing.get("owner_id") == row.get("owner_id")
                and existing.get("name") == row.get("name")
            ):
                logger.warning(
                    "[EMOJI_DB] insert_emoji_category: duplicate name=%r — refused.",
                    row.get("name"),
                )
                return None
        row["id"] = len(store) + 1
        store.append(row)
        return row
    try:
        result = db.table("emoji_categories").insert(row).execute()
        inserted = result.data[0] if result.data else None
        if inserted is None:
            logger.error(
                "[EMOJI_DB] insert_emoji_category ERROR: insert() returned no data."
            )
            record_event(
                "database", "insert emoji_categories", 0, "ERROR",
                "insert returned no data",
            )
            return None
        record_event("database", "insert emoji_categories", 0, "SUCCESS")
        return inserted
    except Exception as exc:
        logger.error("[EMOJI_DB] insert_emoji_category ERROR: %s", exc, exc_info=True)
        record_event("database", "insert emoji_categories", 0, "ERROR", str(exc))
        return None


async def insert_emoji_category(data: dict) -> dict | None:
    """Insert one emoji_categories row. Returns the stored row, or None on
    failure (and None when the owner already has a category with the same
    name — the manual UNIQUE (owner_id, name) index is the durable backstop).
    Never raises.
    """
    try:
        return await _run_sync(_insert_emoji_category_sync, data)
    except Exception as exc:
        logger.error("[EMOJI_DB] insert_emoji_category FAILED: %s", exc)
        record_event("database", "insert emoji_categories", 0, "ERROR", str(exc))
        return None


def _get_emoji_category_sync(owner_id: int, category_id: int) -> dict | None:
    db = get_db()
    if db is None:
        for row in _fallback["emoji_categories"]:
            if row.get("owner_id") == owner_id and row.get("id") == category_id:
                return row
        return None
    try:
        result = (
            db.table("emoji_categories")
            .select("*")
            .eq("owner_id", owner_id)
            .eq("id", category_id)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as exc:
        logger.error("[EMOJI_DB] get_emoji_category FAILED: %s", exc)
        return None


async def get_emoji_category(owner_id: int, category_id: int) -> dict | None:
    """One owner-scoped category row, or None when missing/unreadable."""
    try:
        return await _run_sync(_get_emoji_category_sync, owner_id, category_id)
    except Exception as exc:
        logger.error("[EMOJI_DB] get_emoji_category FAILED: %s", exc)
        return None


def _get_emoji_category_by_name_sync(owner_id: int, name: str) -> dict | None:
    db = get_db()
    if db is None:
        for row in _fallback["emoji_categories"]:
            if row.get("owner_id") == owner_id and row.get("name") == name:
                return row
        return None
    try:
        result = (
            db.table("emoji_categories")
            .select("*")
            .eq("owner_id", owner_id)
            .eq("name", name)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as exc:
        logger.error("[EMOJI_DB] get_emoji_category_by_name FAILED: %s", exc)
        return None


async def get_emoji_category_by_name(owner_id: int, name: str) -> dict | None:
    """One owner-scoped category by exact name, or None when missing."""
    try:
        return await _run_sync(_get_emoji_category_by_name_sync, owner_id, name)
    except Exception as exc:
        logger.error("[EMOJI_DB] get_emoji_category_by_name FAILED: %s", exc)
        return None


def _list_emoji_categories_sync(
    owner_id: int, limit: int, offset: int,
) -> tuple[list, int]:
    db = get_db()
    if db:
        try:
            result = (
                db.table("emoji_categories")
                .select("*")
                .eq("owner_id", owner_id)
                .order("created_at", desc=True)
                .range(offset, offset + limit - 1)
                .execute()
            )
            count_res = (
                db.table("emoji_categories")
                .select("id", count="exact")
                .eq("owner_id", owner_id)
                .execute()
            )
            return result.data or [], count_res.count or 0
        except Exception as exc:
            logger.error("[EMOJI_DB] list_emoji_categories FAILED: %s", exc)
    items = [c for c in _fallback["emoji_categories"] if c.get("owner_id") == owner_id]
    total = len(items)
    items = sorted(
        items,
        key=lambda r: (r.get("created_at") or "", r.get("id") or 0),
        reverse=True,
    )
    return items[offset:offset + limit], total


async def list_emoji_categories(
    owner_id: int, limit: int = 50, offset: int = 0,
) -> tuple[list, int]:
    """List the owner's categories newest-first as (rows, total)."""
    try:
        return await _run_sync(_list_emoji_categories_sync, owner_id, limit, offset)
    except Exception as exc:
        logger.error("[EMOJI_DB] list_emoji_categories FAILED: %s", exc)
        items = [c for c in _fallback["emoji_categories"] if c.get("owner_id") == owner_id]
        return items[offset:offset + limit], len(items)


def _update_emoji_category_sync(
    owner_id: int, category_id: int, name: str,
) -> dict | None:
    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    if db is None:
        for existing in _fallback["emoji_categories"]:
            if (
                existing.get("owner_id") == owner_id
                and existing.get("id") != category_id
                and existing.get("name") == name
            ):
                logger.warning(
                    "[EMOJI_DB] update_emoji_category: name=%r already used — refused.",
                    name,
                )
                return None
        for row in _fallback["emoji_categories"]:
            if row.get("owner_id") == owner_id and row.get("id") == category_id:
                row["name"] = name
                row["updated_at"] = now
                return row
        return None
    try:
        result = (
            db.table("emoji_categories")
            .update({"name": name, "updated_at": now})
            .eq("owner_id", owner_id)
            .eq("id", category_id)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as exc:
        logger.error("[EMOJI_DB] update_emoji_category ERROR: %s", exc, exc_info=True)
        record_event("database", "update emoji_categories", 0, "ERROR", str(exc))
        return None


async def update_emoji_category(
    owner_id: int, category_id: int, name: str,
) -> dict | None:
    """Rename one owner-scoped category. None on failure, a missing row, or
    when another category of the same owner already holds the new name."""
    try:
        return await _run_sync(_update_emoji_category_sync, owner_id, category_id, name)
    except Exception as exc:
        logger.error("[EMOJI_DB] update_emoji_category FAILED: %s", exc)
        return None


def _delete_emoji_category_sync(owner_id: int, category_id: int) -> bool:
    db = get_db()
    if db is None:
        store = _fallback["emoji_categories"]
        before = len(store)
        _fallback["emoji_categories"] = [
            c for c in store
            if not (c.get("owner_id") == owner_id and c.get("id") == category_id)
        ]
        return len(_fallback["emoji_categories"]) < before
    try:
        result = (
            db.table("emoji_categories")
            .delete()
            .eq("owner_id", owner_id)
            .eq("id", category_id)
            .execute()
        )
        return bool(result.data)
    except Exception as exc:
        logger.error("[EMOJI_DB] delete_emoji_category ERROR: %s", exc, exc_info=True)
        record_event("database", "delete emoji_categories", 0, "ERROR", str(exc))
        return False


async def delete_emoji_category(owner_id: int, category_id: int) -> bool:
    """Delete one owner-scoped category row. False on failure/missing.
    Callers remove the category's mappings FIRST — this helper never
    touches them (the service owns the deletion order)."""
    try:
        return await _run_sync(_delete_emoji_category_sync, owner_id, category_id)
    except Exception as exc:
        logger.error("[EMOJI_DB] delete_emoji_category FAILED: %s", exc)
        return False


# ── emoji_mappings (Emoji & Reaction Phase 2) ──

#: Hard bound on the read used for per-category mapping counts (UI display).
#: The manual UNIQUE (owner_id, category_id, simple_emoji) index is the
#: durability backstop beyond this bound.
_EMOJI_MAPPINGS_MAX_ROWS = 5000


def _get_emoji_entry_sync(owner_id: int, document_id: int) -> dict | None:
    db = get_db()
    if db is None:
        for row in _fallback["emoji_library"]:
            if (
                row.get("owner_id") == owner_id
                and row.get("document_id") == document_id
            ):
                return row
        return None
    try:
        result = (
            db.table("emoji_library")
            .select("*")
            .eq("owner_id", owner_id)
            .eq("document_id", document_id)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as exc:
        logger.error("[EMOJI_DB] get_emoji_entry FAILED: %s", exc)
        return None


async def get_emoji_entry(owner_id: int, document_id: int) -> dict | None:
    """One owner-scoped emoji_library row by document id, or None when
    missing/unreadable. Mappings must resolve their premium emoji through
    this lookup — the library stays the single source of definitions."""
    try:
        return await _run_sync(_get_emoji_entry_sync, owner_id, document_id)
    except Exception as exc:
        logger.error("[EMOJI_DB] get_emoji_entry FAILED: %s", exc)
        return None


def _insert_emoji_mapping_sync(data: dict) -> dict | None:
    row = dict(data)
    now = datetime.now(timezone.utc).isoformat()
    if row.get("created_at") is None:
        row["created_at"] = now
    if row.get("updated_at") is None:
        row["updated_at"] = now
    db = get_db()
    if db is None:
        store = _fallback["emoji_mappings"]
        for existing in store:
            if (
                existing.get("owner_id") == row.get("owner_id")
                and existing.get("category_id") == row.get("category_id")
                and existing.get("simple_emoji") == row.get("simple_emoji")
            ):
                logger.warning(
                    "[EMOJI_DB] insert_emoji_mapping: duplicate (category=%s, emoji) — refused.",
                    row.get("category_id"),
                )
                return None
        row["id"] = len(store) + 1
        store.append(row)
        return row
    try:
        result = db.table("emoji_mappings").insert(row).execute()
        inserted = result.data[0] if result.data else None
        if inserted is None:
            logger.error(
                "[EMOJI_DB] insert_emoji_mapping ERROR: insert() returned no data."
            )
            record_event(
                "database", "insert emoji_mappings", 0, "ERROR",
                "insert returned no data",
            )
            return None
        record_event("database", "insert emoji_mappings", 0, "SUCCESS")
        return inserted
    except Exception as exc:
        logger.error("[EMOJI_DB] insert_emoji_mapping ERROR: %s", exc, exc_info=True)
        record_event("database", "insert emoji_mappings", 0, "ERROR", str(exc))
        return None


async def insert_emoji_mapping(data: dict) -> dict | None:
    """Insert one emoji_mappings row. Returns the stored row, or None on
    failure (and None when the category already defines the same simple
    emoji — the manual UNIQUE (owner_id, category_id, simple_emoji) index is
    the durable backstop). Never raises. Callers must check for an existing
    mapping FIRST and route through the conflict path — this helper never
    overwrites."""
    try:
        return await _run_sync(_insert_emoji_mapping_sync, data)
    except Exception as exc:
        logger.error("[EMOJI_DB] insert_emoji_mapping FAILED: %s", exc)
        record_event("database", "insert emoji_mappings", 0, "ERROR", str(exc))
        return None


def _get_emoji_mapping_sync(
    owner_id: int, category_id: int, simple_emoji: str,
) -> dict | None:
    db = get_db()
    if db is None:
        for row in _fallback["emoji_mappings"]:
            if (
                row.get("owner_id") == owner_id
                and row.get("category_id") == category_id
                and row.get("simple_emoji") == simple_emoji
            ):
                return row
        return None
    try:
        result = (
            db.table("emoji_mappings")
            .select("*")
            .eq("owner_id", owner_id)
            .eq("category_id", category_id)
            .eq("simple_emoji", simple_emoji)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as exc:
        logger.error("[EMOJI_DB] get_emoji_mapping FAILED: %s", exc)
        return None


async def get_emoji_mapping(
    owner_id: int, category_id: int, simple_emoji: str,
) -> dict | None:
    """One owner-scoped mapping by (category, simple emoji), or None."""
    try:
        return await _run_sync(
            _get_emoji_mapping_sync, owner_id, category_id, simple_emoji,
        )
    except Exception as exc:
        logger.error("[EMOJI_DB] get_emoji_mapping FAILED: %s", exc)
        return None


def _list_emoji_mappings_sync(
    owner_id: int, category_id: int, limit: int, offset: int,
) -> tuple[list, int]:
    db = get_db()
    if db:
        try:
            result = (
                db.table("emoji_mappings")
                .select("*")
                .eq("owner_id", owner_id)
                .eq("category_id", category_id)
                .order("created_at", desc=True)
                .range(offset, offset + limit - 1)
                .execute()
            )
            count_res = (
                db.table("emoji_mappings")
                .select("id", count="exact")
                .eq("owner_id", owner_id)
                .eq("category_id", category_id)
                .execute()
            )
            return result.data or [], count_res.count or 0
        except Exception as exc:
            logger.error("[EMOJI_DB] list_emoji_mappings FAILED: %s", exc)
    items = [
        m for m in _fallback["emoji_mappings"]
        if m.get("owner_id") == owner_id and m.get("category_id") == category_id
    ]
    total = len(items)
    items = sorted(
        items,
        key=lambda r: (r.get("created_at") or "", r.get("id") or 0),
        reverse=True,
    )
    return items[offset:offset + limit], total


async def list_emoji_mappings(
    owner_id: int, category_id: int, limit: int = 50, offset: int = 0,
) -> tuple[list, int]:
    """List one category's mappings newest-first as (rows, total)."""
    try:
        return await _run_sync(
            _list_emoji_mappings_sync, owner_id, category_id, limit, offset,
        )
    except Exception as exc:
        logger.error("[EMOJI_DB] list_emoji_mappings FAILED: %s", exc)
        items = [
            m for m in _fallback["emoji_mappings"]
            if m.get("owner_id") == owner_id and m.get("category_id") == category_id
        ]
        return items[offset:offset + limit], len(items)


def _update_emoji_mapping_sync(
    owner_id: int, category_id: int, simple_emoji: str, document_id: int,
) -> dict | None:
    now = datetime.now(timezone.utc).isoformat()
    db = get_db()
    if db is None:
        for row in _fallback["emoji_mappings"]:
            if (
                row.get("owner_id") == owner_id
                and row.get("category_id") == category_id
                and row.get("simple_emoji") == simple_emoji
            ):
                row["document_id"] = document_id
                row["updated_at"] = now
                return row
        return None
    try:
        result = (
            db.table("emoji_mappings")
            .update({"document_id": document_id, "updated_at": now})
            .eq("owner_id", owner_id)
            .eq("category_id", category_id)
            .eq("simple_emoji", simple_emoji)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as exc:
        logger.error("[EMOJI_DB] update_emoji_mapping ERROR: %s", exc, exc_info=True)
        record_event("database", "update emoji_mappings", 0, "ERROR", str(exc))
        return None


async def update_emoji_mapping(
    owner_id: int, category_id: int, simple_emoji: str, document_id: int,
) -> dict | None:
    """Point an EXISTING mapping at a new library document. None on failure
    or when the mapping no longer exists — never an upsert, so a stale
    conflict panel cannot resurrect a deleted mapping."""
    try:
        return await _run_sync(
            _update_emoji_mapping_sync, owner_id, category_id, simple_emoji, document_id,
        )
    except Exception as exc:
        logger.error("[EMOJI_DB] update_emoji_mapping FAILED: %s", exc)
        return None


def _delete_emoji_mapping_sync(
    owner_id: int, category_id: int, simple_emoji: str,
) -> bool:
    db = get_db()
    if db is None:
        store = _fallback["emoji_mappings"]
        before = len(store)
        _fallback["emoji_mappings"] = [
            m for m in store
            if not (
                m.get("owner_id") == owner_id
                and m.get("category_id") == category_id
                and m.get("simple_emoji") == simple_emoji
            )
        ]
        return len(_fallback["emoji_mappings"]) < before
    try:
        result = (
            db.table("emoji_mappings")
            .delete()
            .eq("owner_id", owner_id)
            .eq("category_id", category_id)
            .eq("simple_emoji", simple_emoji)
            .execute()
        )
        return bool(result.data)
    except Exception as exc:
        logger.error("[EMOJI_DB] delete_emoji_mapping ERROR: %s", exc, exc_info=True)
        record_event("database", "delete emoji_mappings", 0, "ERROR", str(exc))
        return False


async def delete_emoji_mapping(
    owner_id: int, category_id: int, simple_emoji: str,
) -> bool:
    """Delete one owner-scoped mapping. False on failure/missing."""
    try:
        return await _run_sync(
            _delete_emoji_mapping_sync, owner_id, category_id, simple_emoji,
        )
    except Exception as exc:
        logger.error("[EMOJI_DB] delete_emoji_mapping FAILED: %s", exc)
        return False


def _delete_emoji_mappings_for_category_sync(
    owner_id: int, category_id: int,
) -> int:
    """Remove every mapping of one category. -1 signals a failed durable
    write so the caller can abort the category deletion instead of leaving
    a silently half-deleted state."""
    db = get_db()
    if db is None:
        store = _fallback["emoji_mappings"]
        before = len(store)
        _fallback["emoji_mappings"] = [
            m for m in store
            if not (m.get("owner_id") == owner_id and m.get("category_id") == category_id)
        ]
        return before - len(_fallback["emoji_mappings"])
    try:
        result = (
            db.table("emoji_mappings")
            .delete()
            .eq("owner_id", owner_id)
            .eq("category_id", category_id)
            .execute()
        )
        return len(result.data) if result.data else 0
    except Exception as exc:
        logger.error(
            "[EMOJI_DB] delete_emoji_mappings_for_category ERROR: %s", exc,
            exc_info=True,
        )
        record_event("database", "delete emoji_mappings", 0, "ERROR", str(exc))
        return -1


async def delete_emoji_mappings_for_category(owner_id: int, category_id: int) -> int:
    try:
        return await _run_sync(_delete_emoji_mappings_for_category_sync, owner_id, category_id)
    except Exception as exc:
        logger.error("[EMOJI_DB] delete_emoji_mappings_for_category FAILED: %s", exc)
        return -1


def _count_emoji_mappings_by_category_sync(owner_id: int) -> dict[int, int] | None:
    """Mapping count per category id for one owner, or None when a durable
    read fails (the caller must show an unknown-count state, not 0)."""
    db = get_db()
    if db is None:
        counts: dict[int, int] = {}
        for row in _fallback["emoji_mappings"]:
            if row.get("owner_id") != owner_id:
                continue
            cid = row.get("category_id")
            if isinstance(cid, int) and not isinstance(cid, bool):
                counts[cid] = counts.get(cid, 0) + 1
        return counts
    try:
        result = (
            db.table("emoji_mappings")
            .select("category_id")
            .eq("owner_id", owner_id)
            .limit(_EMOJI_MAPPINGS_MAX_ROWS)
            .execute()
        )
        counts = {}
        for row in result.data or []:
            cid = row.get("category_id")
            if isinstance(cid, int) and not isinstance(cid, bool):
                counts[cid] = counts.get(cid, 0) + 1
        return counts
    except Exception as exc:
        logger.error("[EMOJI_DB] count_emoji_mappings_by_category FAILED: %s", exc)
        record_event("database", "select emoji_mappings", 0, "ERROR", str(exc))
        return None


async def count_emoji_mappings_by_category(owner_id: int) -> dict[int, int] | None:
    try:
        return await _run_sync(_count_emoji_mappings_by_category_sync, owner_id)
    except Exception as exc:
        logger.error("[EMOJI_DB] count_emoji_mappings_by_category FAILED: %s", exc)
        return None


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


# ── emoji_state (Emoji & Reaction Phase 3) ───────────────────────────────────
#
# Two stores:
#   * ``emoji_state`` — the owner's single global row: replacement_enabled
#     (bool, default False) and global_default_category_id (int, nullable).
#     Single-row-per-owner persistence/cache pattern, mirroring bio_state /
#     username_state.
#   * ``emoji_chat_overrides`` — per-chat category overrides keyed by
#     ``(owner_id, chat_id)``. The override is a single nullable column set
#     on the same row shape; clearing it restores the global default at
#     resolution time.
#
# Both degrade to the in-memory fallback exactly like every other store in
# this module: a durable write failure returns False / None and is logged +
# record_event'ed, never raised.


def _get_emoji_state_sync(owner_id: int) -> dict | None:
    db = get_db()
    if db is None:
        row = _fallback["emoji_state"].get(owner_id)
        return dict(row) if row else None
    try:
        result = (
            db.table("emoji_state")
            .select("*")
            .eq("owner_id", owner_id)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as exc:
        logger.error("[EMOJI_DB] get_emoji_state FAILED: %s", exc)
        return None


async def get_emoji_state(owner_id: int) -> dict | None:
    """The owner's global emoji-replacement state row, or None when no row
    exists yet (callers treat every absent field as its default)."""
    try:
        return await _run_sync(_get_emoji_state_sync, owner_id)
    except Exception as exc:
        logger.error("[EMOJI_DB] get_emoji_state FAILED: %s", exc)
        return None


def _upsert_emoji_state_sync(owner_id: int, updates: dict) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    payload = dict(updates)
    payload["updated_at"] = now
    db = get_db()
    if db is None:
        row = dict(_fallback["emoji_state"].get(owner_id) or {"owner_id": owner_id})
        row.update(payload)
        _fallback["emoji_state"][owner_id] = row
        return True
    try:
        existing = (
            db.table("emoji_state")
            .select("owner_id")
            .eq("owner_id", owner_id)
            .limit(1)
            .execute()
        )
        if existing.data:
            db.table("emoji_state").update(payload).eq("owner_id", owner_id).execute()
        else:
            insert_payload = {"owner_id": owner_id, **payload}
            db.table("emoji_state").insert(insert_payload).execute()
        return True
    except Exception as exc:
        logger.error("[EMOJI_DB] upsert_emoji_state FAILED: %s", exc)
        record_event("database", "upsert emoji_state", 0, "ERROR", str(exc))
        return False


async def upsert_emoji_state(owner_id: int, updates: dict) -> bool:
    """Merge updates into the owner's global state row. Returns True on
    success, False on a durable-write failure — callers report the
    degradation honestly instead of pretending the write landed."""
    try:
        return await _run_sync(_upsert_emoji_state_sync, owner_id, updates)
    except Exception as exc:
        logger.error("[EMOJI_DB] upsert_emoji_state FAILED: %s", exc)
        return False


def _get_emoji_chat_override_sync(owner_id: int, chat_id: int) -> dict | None:
    db = get_db()
    if db is None:
        for row in _fallback["emoji_chat_overrides"]:
            if row.get("owner_id") == owner_id and row.get("chat_id") == chat_id:
                return row
        return None
    try:
        result = (
            db.table("emoji_chat_overrides")
            .select("*")
            .eq("owner_id", owner_id)
            .eq("chat_id", chat_id)
            .limit(1)
            .execute()
        )
        return result.data[0] if result.data else None
    except Exception as exc:
        logger.error("[EMOJI_DB] get_emoji_chat_override FAILED: %s", exc)
        return None


async def get_emoji_chat_override(owner_id: int, chat_id: int) -> dict | None:
    """The owner's override row for one chat, or None when no override
    exists (resolution then falls back to the global default)."""
    try:
        return await _run_sync(_get_emoji_chat_override_sync, owner_id, chat_id)
    except Exception as exc:
        logger.error("[EMOJI_DB] get_emoji_chat_override FAILED: %s", exc)
        return None


def _upsert_emoji_chat_override_sync(
    owner_id: int, chat_id: int, updates: dict,
) -> bool:
    now = datetime.now(timezone.utc).isoformat()
    payload = dict(updates)
    payload["updated_at"] = now
    db = get_db()
    if db is None:
        for row in _fallback["emoji_chat_overrides"]:
            if row.get("owner_id") == owner_id and row.get("chat_id") == chat_id:
                row.update(payload)
                return True
        _fallback["emoji_chat_overrides"].append(
            {"owner_id": owner_id, "chat_id": chat_id, **payload}
        )
        return True
    try:
        existing = (
            db.table("emoji_chat_overrides")
            .select("owner_id")
            .eq("owner_id", owner_id)
            .eq("chat_id", chat_id)
            .limit(1)
            .execute()
        )
        if existing.data:
            db.table("emoji_chat_overrides").update(payload).eq(
                "owner_id", owner_id
            ).eq("chat_id", chat_id).execute()
        else:
            insert_payload = {"owner_id": owner_id, "chat_id": chat_id, **payload}
            db.table("emoji_chat_overrides").insert(insert_payload).execute()
        return True
    except Exception as exc:
        logger.error("[EMOJI_DB] upsert_emoji_chat_override FAILED: %s", exc)
        record_event("database", "upsert emoji_chat_overrides", 0, "ERROR", str(exc))
        return False


async def upsert_emoji_chat_override(
    owner_id: int, chat_id: int, updates: dict,
) -> bool:
    """Merge updates into the owner's override row for one chat (creating
    it when absent). Returns True on success, False on a durable-write
    failure."""
    try:
        return await _run_sync(
            _upsert_emoji_chat_override_sync, owner_id, chat_id, updates,
        )
    except Exception as exc:
        logger.error("[EMOJI_DB] upsert_emoji_chat_override FAILED: %s", exc)
        return False


# ── Private Message Management (private_message_config / private_message_senders) ──
#
# Private Message Management (DATABASE_ARCHITECTURE.md §33) persists THREE
# things: the owner's enforcement configuration, the access state the panel
# edits (allowlist, exceptions, temporary override, schedule) and ONE durable
# counter row per human sender. Both tables degrade to the in-memory fallback
# exactly like every other store in this module — a durable failure returns
# False / None / an explicit ``ok=False`` envelope and is logged + record_event'ed,
# never raised. The fallback keeps enforcing the SAME per-person quota in
# memory: only durability degrades, never the limit itself.
#
# The AI-reply allowance is enforced by ONE CAS write (claim → compensate):
# ``claim_private_message_ai_reply`` increments ONLY while the row still holds
# the values it just read — the used count AND the reset window — so a limit can
# never be exceeded by a racing update and two claims at a window boundary can
# never both roll the same expired window, and
# ``release_private_message_ai_reply`` puts the exact previous triple back after
# a generation/delivery failure, so a reply that was never delivered can never
# consume a person's allowance.
#
# The automatic per-person reset is a ROLLING WINDOW anchored to the person's
# first counted reply in the current window (``window_started_at``). It is
# evaluated and applied INSIDE the same claim: no scheduler, no ticker, no
# second write. An expired window is rolled by the claim itself, which is why
# its identity is part of the CAS filter.

#: Bounded CAS attempts for one quota claim (a lost CAS means a concurrent
#: writer moved the row; the claim re-reads instead of overwriting it).
_PM_CAS_ATTEMPTS = 3

#: The configuration columns, in the order the state document is written.
_PM_CONFIG_COLUMNS: tuple[str, ...] = (
    "enabled",
    "mode",
    "delete_incoming",
    "fixed_response_enabled",
    "fixed_response_text",
    "ai_response_enabled",
    "ai_max_replies_per_person",
    "ai_unlimited_replies",
    "ai_interval_seconds",
    "ai_counter_reset_enabled",
    "ai_counter_reset_seconds",
    "allowlist",
    "exceptions",
    "temporary",
    "schedule",
)


def _pm_epoch(value) -> float | None:
    """Epoch seconds of a stored/observed ISO-8601 instant, or None."""
    if isinstance(value, (int, float)) and not isinstance(value, bool):
        return float(value)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.timestamp()


def _pm_config_payload(document: dict) -> dict:
    """The writable half of a state document (unknown keys are never written)."""
    payload = {column: document[column] for column in _PM_CONFIG_COLUMNS if column in document}
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    return payload


def _pm_sender_row(owner_id: int, sender_id: int) -> dict | None:
    for row in _fallback["private_message_senders"]:
        if row.get("owner_id") == owner_id and row.get("sender_id") == sender_id:
            return row
    return None


def _pm_claim_result(
    ok: bool, *, claimed: bool = False, reason: str = "", used: int = 0,
    token: dict | None = None,
) -> dict:
    return {"ok": ok, "claimed": claimed, "reason": reason, "used": used, "token": token}


def _pm_claim_decision(
    row: dict | None, now_epoch: float, now_iso: str, max_replies: int,
    unlimited: bool, interval_seconds: int, reset_enabled: bool,
    reset_seconds: int,
) -> tuple[dict | None, dict | None]:
    """``(denial, token)`` for ONE allowance claim — exactly one is returned.

    The person's reset window is resolved here, so the caller and every store
    share ONE decision:

    * ``window_started_at`` is the instant of the first counted reply in the
      current window; it is written by the claim when the row has none yet.
    * With automatic reset ENABLED and a window at least ``reset_seconds`` old,
      the window has expired: the count resets to zero for this very claim and a
      NEW window starts now. The pre-reset triple is carried in the token, so a
      release puts the expired window back instead of silently restarting it.
    * With automatic reset DISABLED nothing ever expires; the window is still
      maintained (a restart cannot lose it) but is never rolled.

    The token carries three groups of values: ``read_*`` is the identity the
    claim conditionally replaces (the CAS filter), ``used``/``last``/``window``
    are what the claim writes (the release conditions on them), and
    ``restore_*`` is the pre-claim triple the release puts back.
    """
    used = int(row.get("ai_replies_used") or 0) if row else 0
    last = row.get("last_ai_reply_at") if row else None
    window = row.get("window_started_at") if row else None
    window_epoch = _pm_epoch(window)
    expired = (
        reset_enabled
        and reset_seconds > 0
        and window_epoch is not None
        and now_epoch - window_epoch >= reset_seconds
    )
    effective_used = 0 if expired else used
    if not unlimited and effective_used >= max_replies:
        return (
            _pm_claim_result(
                True, claimed=False, reason="limit_reached", used=effective_used
            ),
            None,
        )
    # The interval is a SEPARATE anti-spam floor, so it is read from the last
    # delivered reply regardless of the window: a reset rolls the COUNT, it never
    # shortens the minimum delay between two replies to the same person.
    last_epoch = _pm_epoch(last)
    if interval_seconds > 0 and last_epoch is not None and now_epoch - last_epoch < interval_seconds:
        return (
            _pm_claim_result(
                True, claimed=False, reason="interval_not_elapsed", used=effective_used
            ),
            None,
        )
    token = {
        "read_used": used,
        "read_window": window,
        "used": effective_used + 1,
        "last": now_iso,
        "window": now_iso if (expired or window is None) else window,
        "restore_used": used,
        "restore_last": last,
        "restore_window": window,
        "window_expired": expired,
    }
    return None, token


def _pm_cas_filters(token: dict) -> list[tuple[str, object]]:
    """The CAS identity of a claim: the values it read and is replacing.

    ``window_started_at`` is only part of the filter when the row had one — a
    row without a window cannot expire, so there is nothing to double-roll. A
    PostgreSQL ``timestamptz`` equality is a timestamp comparison, so the value
    read from the row round-trips exactly.
    """
    filters: list[tuple[str, object]] = [("ai_replies_used", token["read_used"])]
    if token["read_window"] is not None:
        filters.append(("window_started_at", token["read_window"]))
    return filters


def _get_private_message_config_state_sync(owner_id: int) -> tuple[dict | None, bool]:
    """``(row, readable)`` for the owner's dedicated configuration row."""
    db = get_db()
    if db is None:
        row = _fallback["private_message_config"].get(owner_id)
        return (dict(row) if row else None), True
    try:
        result = (
            db.table("private_message_config")
            .select("*")
            .eq("owner_id", owner_id)
            .limit(1)
            .execute()
        )
        rows = getattr(result, "data", None) or []
        return (rows[0] if rows else None), True
    except Exception as exc:
        logger.error("[PM_DB] get_private_message_config FAILED: %s", exc)
        return None, False


async def read_private_message_config(owner_id: int) -> tuple[dict | None, bool]:
    """``(row, readable)`` for the configuration.

    ``readable=False`` means the read FAILED (the table is missing, the network
    is down, the query timed out). The service must then neither treat the
    policy as absent nor persist a default document over the stored one.
    ``(None, True)`` is the honest "no row yet" case (a fresh install).
    """
    try:
        return await _run_sync(_get_private_message_config_state_sync, owner_id)
    except Exception as exc:
        logger.error("[PM_DB] get_private_message_config FAILED: %s", exc)
        return None, False


async def get_private_message_config(owner_id: int) -> dict | None:
    """The owner's dedicated configuration row, or None when no row exists yet
    OR the store could not be read. Both resolve to the fail-closed default in
    the service (a missing row means the feature is OFF).

    The state LOAD uses :func:`read_private_message_config`, which keeps the two
    cases apart.
    """
    row, _ok = await read_private_message_config(owner_id)
    return row


def _get_legacy_private_message_state_sync(owner_id: int, key: str) -> str | None:
    """READ ONLY: the Phase-1 ``bot_settings`` document, used once as the
    backfill source when the dedicated row does not exist yet."""
    db = get_db()
    if db is None:
        row = _fallback.get("bot_settings", {}).get(key)
        return row if isinstance(row, str) else None
    try:
        result = db.table("bot_settings").select("value").eq("key", key).maybe_single().execute()
        data = getattr(result, "data", None)
        if isinstance(data, dict):
            value = data.get("value")
            return value if isinstance(value, str) and value.strip() else None
        if isinstance(data, list) and data:
            value = data[0].get("value")
            return value if isinstance(value, str) and value.strip() else None
        return None
    except Exception as exc:
        logger.error("[PM_DB] legacy private_message state read FAILED: %s", exc)
        return None


async def get_legacy_private_message_state(
    owner_id: int, key: str = "private_message_management",
) -> str | None:
    """The legacy Phase-1 document, read at most once for the import."""
    try:
        return await _run_sync(_get_legacy_private_message_state_sync, owner_id, key)
    except Exception as exc:
        logger.error("[PM_DB] legacy private_message state read FAILED: %s", exc)
        return None


def _save_private_message_config_sync(owner_id: int, document: dict) -> bool:
    payload = _pm_config_payload(document)
    db = get_db()
    if db is None:
        row = dict(_fallback["private_message_config"].get(owner_id) or {"owner_id": owner_id})
        row.update(payload)
        _fallback["private_message_config"][owner_id] = row
        return True
    try:
        existing = (
            db.table("private_message_config")
            .select("owner_id")
            .eq("owner_id", owner_id)
            .limit(1)
            .execute()
        )
        if getattr(existing, "data", None):
            db.table("private_message_config").update(payload).eq(
                "owner_id", owner_id
            ).execute()
        else:
            db.table("private_message_config").insert(
                {"owner_id": owner_id, **payload}
            ).execute()
        return True
    except Exception as exc:
        logger.error("[PM_DB] save_private_message_config FAILED: %s", exc)
        record_event("database", "save private_message_config", 0, "ERROR", str(exc))
        return False


async def save_private_message_config(owner_id: int, document: dict) -> bool:
    """Write the whole configuration document into the dedicated row.

    Returns True only when the durable write landed, so the panel never reports
    a setting as saved when it was not.
    """
    try:
        return await _run_sync(_save_private_message_config_sync, owner_id, document)
    except Exception as exc:
        logger.error("[PM_DB] save_private_message_config FAILED: %s", exc)
        return False


def _list_private_message_senders_sync(owner_id: int, limit: int) -> list | None:
    db = get_db()
    if db is None:
        rows = [
            dict(row) for row in _fallback["private_message_senders"]
            if row.get("owner_id") == owner_id
        ]
        return rows[:limit]
    try:
        result = (
            db.table("private_message_senders")
            .select("*")
            .eq("owner_id", owner_id)
            .order("updated_at", desc=True)
            .limit(limit)
            .execute()
        )
        return list(getattr(result, "data", None) or [])
    except Exception as exc:
        logger.error("[PM_DB] list_private_message_senders FAILED: %s", exc)
        return None


async def list_private_message_senders(owner_id: int, limit: int = 200) -> list | None:
    """Bounded per-person counter rows (newest first), or None when the store
    could not be read — the caller must not present an unreadable store as an
    empty one."""
    try:
        return await _run_sync(_list_private_message_senders_sync, owner_id, max(1, int(limit)))
    except Exception as exc:
        logger.error("[PM_DB] list_private_message_senders FAILED: %s", exc)
        return None


def _pm_note_window_reset(owner_id: int, sender_id: int, token: dict) -> None:
    """One honest log line when a claim rolled an expired reset window."""
    if token.get("window_expired"):
        logger.info(
            "[PM_DB] counter window expired, count reset: owner=%s sender=%s was=%s",
            owner_id, sender_id, token["read_used"],
        )


def _pm_token_payload(token: dict, now_iso: str) -> dict:
    """What a claim writes (and what a release therefore conditions on)."""
    return {
        "ai_replies_used": token["used"],
        "last_ai_reply_at": token["last"],
        "window_started_at": token["window"],
        "updated_at": now_iso,
    }


def _claim_private_message_ai_reply_sync(
    owner_id: int, sender_id: int, max_replies: int, unlimited: bool,
    interval_seconds: int, reset_enabled: bool, reset_seconds: int, now_iso: str,
) -> dict:
    now_epoch = _pm_epoch(now_iso)
    if now_epoch is None:
        return _pm_claim_result(False, reason="invalid_timestamp")
    db = get_db()
    if db is None:
        row = _pm_sender_row(owner_id, sender_id)
        denial, token = _pm_claim_decision(
            row, now_epoch, now_iso, max_replies, unlimited, interval_seconds,
            reset_enabled, reset_seconds,
        )
        if denial is not None:
            return denial
        _pm_note_window_reset(owner_id, sender_id, token)
        written = _pm_token_payload(token, now_iso)
        if row is None:
            _fallback["private_message_senders"].append({
                "owner_id": owner_id,
                "sender_id": sender_id,
                **written,
            })
        else:
            row.update(written)
        return _pm_claim_result(True, claimed=True, used=token["used"], token=token)
    for _attempt in range(_PM_CAS_ATTEMPTS):
        try:
            result = (
                db.table("private_message_senders")
                .select("*")
                .eq("owner_id", owner_id)
                .eq("sender_id", sender_id)
                .limit(1)
                .execute()
            )
        except Exception as exc:
            logger.error("[PM_DB] claim read FAILED: %s", exc)
            record_event("database", "private_message claim read", 0, "ERROR", str(exc))
            return _pm_claim_result(False, reason="claim_read_failed")
        rows = getattr(result, "data", None) or []
        row = rows[0] if rows else None
        denial, token = _pm_claim_decision(
            row, now_epoch, now_iso, max_replies, unlimited, interval_seconds,
            reset_enabled, reset_seconds,
        )
        if denial is not None:
            return denial
        payload = _pm_token_payload(token, now_iso)
        try:
            if row is None:
                db.table("private_message_senders").insert(
                    {"owner_id": owner_id, "sender_id": sender_id, **payload}
                ).execute()
            else:
                builder = (
                    db.table("private_message_senders")
                    .update(payload)
                    .eq("owner_id", owner_id)
                    .eq("sender_id", sender_id)
                )
                for column, value in _pm_cas_filters(token):
                    builder = builder.eq(column, value)
                written = builder.execute()
                if not (getattr(written, "data", None) or []):
                    continue
        except Exception as exc:
            logger.warning("[PM_DB] claim write lost/failed, re-reading: %s", exc)
            continue
        _pm_note_window_reset(owner_id, sender_id, token)
        return _pm_claim_result(True, claimed=True, used=token["used"], token=token)
    logger.error("[PM_DB] claim contended for owner=%s sender=%s", owner_id, sender_id)
    record_event("database", "private_message claim", 0, "ERROR", "cas contended")
    return _pm_claim_result(False, reason="claim_contended")


async def claim_private_message_ai_reply(
    owner_id: int, sender_id: int, *, max_replies: int, unlimited: bool,
    interval_seconds: int, reset_enabled: bool, reset_seconds: int, now_iso: str,
) -> dict:
    """Atomically claim ONE AI reply from a person's durable allowance.

    Returns an envelope: ``{"ok": True, "claimed": True, "used": n,
    "token": {...}}`` when the reply may be generated, ``{"ok": True,
    "claimed": False, "reason": "limit_reached"|"interval_not_elapsed",
    "used": n}`` when the configured limit/interval vetoes it, and
    ``{"ok": False, "reason": ...}`` when the store could not be read or the
    CAS stayed contended — the caller fails closed and sends nothing.

    When automatic reset is enabled and the person's window has expired, the
    count is reset to zero and a NEW window starts as part of this very claim
    (one conditional write, no separate step), so the reset and the allowance
    can never disagree.
    """
    try:
        return await _run_sync(
            _claim_private_message_ai_reply_sync,
            owner_id, sender_id, int(max_replies), bool(unlimited),
            int(interval_seconds), bool(reset_enabled), int(reset_seconds), now_iso,
        )
    except Exception as exc:
        logger.error("[PM_DB] claim_private_message_ai_reply FAILED: %s", exc)
        return _pm_claim_result(False, reason="claim_failed")


def _release_private_message_ai_reply_sync(
    owner_id: int, sender_id: int, token: dict,
) -> bool:
    used = token.get("used")
    last = token.get("last")
    window = token.get("window")
    if not isinstance(used, int) or isinstance(used, bool):
        return False
    # The pre-claim triple — INCLUDING the window. Restoring the expired window a
    # rolled claim replaced is what keeps a failed generation from silently
    # restarting the person's window.
    payload = {
        "ai_replies_used": token.get("restore_used", 0),
        "last_ai_reply_at": token.get("restore_last"),
        "window_started_at": token.get("restore_window"),
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    db = get_db()
    if db is None:
        row = _pm_sender_row(owner_id, sender_id)
        if (
            row is None
            or row.get("ai_replies_used") != used
            or row.get("last_ai_reply_at") != last
            or row.get("window_started_at") != window
        ):
            return False
        row.update(payload)
        return True
    try:
        builder = (
            db.table("private_message_senders")
            .update(payload)
            .eq("owner_id", owner_id)
            .eq("sender_id", sender_id)
            .eq("ai_replies_used", used)
            .eq("last_ai_reply_at", last)
        )
        if window is not None:
            builder = builder.eq("window_started_at", window)
        written = builder.execute()
        return bool(getattr(written, "data", None))
    except Exception as exc:
        logger.error("[PM_DB] release_private_message_ai_reply FAILED: %s", exc)
        record_event("database", "release private_message claim", 0, "ERROR", str(exc))
        return False


async def release_private_message_ai_reply(
    owner_id: int, sender_id: int, token: dict,
) -> bool:
    """Give a claimed AI reply back when generation or delivery failed. The CAS
    restores exactly the pair the claim replaced; False means the compensation
    itself did not land (logged honestly by the caller)."""
    if not isinstance(token, dict):
        return False
    try:
        return await _run_sync(
            _release_private_message_ai_reply_sync, owner_id, sender_id, token,
        )
    except Exception as exc:
        logger.error("[PM_DB] release_private_message_ai_reply FAILED: %s", exc)
        return False


def _reset_private_message_counters_sync(owner_id: int) -> bool:
    db = get_db()
    if db is None:
        _fallback["private_message_senders"] = [
            row for row in _fallback["private_message_senders"]
            if row.get("owner_id") != owner_id
        ]
        return True
    try:
        db.table("private_message_senders").delete().eq("owner_id", owner_id).execute()
        return True
    except Exception as exc:
        logger.error("[PM_DB] reset_private_message_counters FAILED: %s", exc)
        record_event("database", "reset private_message counters", 0, "ERROR", str(exc))
        return False


async def reset_private_message_counters(owner_id: int) -> bool:
    """The owner's explicit reset: every tracked person of THIS owner loses both
    their used count and their reset window (the row carries both). Nothing else
    ever clears the counters, so a restart cannot hand a person a fresh
    allowance; an automatic reset only rolls one person's own window."""
    try:
        return await _run_sync(_reset_private_message_counters_sync, owner_id)
    except Exception as exc:
        logger.error("[PM_DB] reset_private_message_counters FAILED: %s", exc)
        return False


# ── Anti-Edit / Anti-Delete archive persistence ──────────────────────────────
#
# The archive is stored as JSON documents in the EXISTING ``bot_settings`` table
# (the same durable key/value store ``ghost_seen_v2`` uses). No new table and no
# migration are introduced: the canonical schema is documented in the protected
# ``DATABASE_ARCHITECTURE.md``, and a table created by a migration would not be
# classified there. The configuration lives in one bounded document; the archive
# rows are chunked across a small, fixed set of shard documents so neither a read
# nor a write grows without bound.
#
# Every function returns ``None``/``False`` on failure (never raises), so the
# service can fail closed and never mistake an unreadable store for an empty one.

_AED_CONFIG_KEY = "anti_edit_delete_config"
_AED_SHARD_PREFIX = "anti_edit_delete_archive_shard:"


def _bot_settings_get_sync(key: str) -> str | None:
    db = get_db()
    if db is None:
        row = _fallback.get("bot_settings", {}).get(key)
        return row if isinstance(row, str) else None
    try:
        result = db.table("bot_settings").select("value").eq("key", key).maybe_single().execute()
        data = getattr(result, "data", None)
        if isinstance(data, dict):
            value = data.get("value")
            return value if isinstance(value, str) and value.strip() else None
        if isinstance(data, list) and data:
            value = data[0].get("value")
            return value if isinstance(value, str) and value.strip() else None
        return None
    except Exception as exc:
        logger.error("[AED_DB] read %s FAILED: %s", key, exc)
        return None


async def aed_get_document(key: str) -> str | None:
    """Read one JSON document from ``bot_settings``. ``None`` means missing OR
    unreadable — the caller resolves both to its fail-closed default.

    Callers that must TELL the two apart (the one-time legacy import, which may
    never overwrite a stored configuration with imported defaults) use
    :func:`aed_get_document_state`.
    """
    document, _ok = await aed_get_document_state(key)
    return document


def _bot_settings_get_state_sync(key: str) -> tuple[str | None, bool]:
    db = get_db()
    if db is None:
        row = _fallback.get("bot_settings", {}).get(key)
        return (row if isinstance(row, str) else None), True
    try:
        result = db.table("bot_settings").select("value").eq("key", key).maybe_single().execute()
        data = getattr(result, "data", None)
        if isinstance(data, dict):
            value = data.get("value")
            return (value if isinstance(value, str) and value.strip() else None), True
        if isinstance(data, list) and data:
            value = data[0].get("value")
            return (value if isinstance(value, str) and value.strip() else None), True
        return None, True
    except Exception as exc:
        logger.error("[AED_DB] read %s FAILED: %s", key, exc)
        return None, False


async def aed_get_document_state(key: str) -> tuple[str | None, bool]:
    """``(document, readable)`` for one ``bot_settings`` document.

    ``readable=False`` means the store could not be read — never a missing
    document — so an importer can refuse to treat an outage as "nothing stored".
    """
    try:
        return await _run_sync(_bot_settings_get_state_sync, key)
    except Exception as exc:
        logger.error("[AED_DB] read %s FAILED: %s", key, exc)
        return None, False


def _bot_settings_put_sync(key: str, value: str) -> bool:
    db = get_db()
    if db is None:
        _fallback["bot_settings"][key] = value
        return True
    try:
        existing = db.table("bot_settings").select("key").eq("key", key).limit(1).execute()
        if getattr(existing, "data", None):
            db.table("bot_settings").update({"value": value}).eq("key", key).execute()
        else:
            db.table("bot_settings").insert(
                {"key": key, "value": value, "value_type": "str"}
            ).execute()
        return True
    except Exception as exc:
        logger.error("[AED_DB] write %s FAILED: %s", key, exc)
        record_event("database", "write anti_edit_delete document", 0, "ERROR", str(exc))
        return False


async def aed_put_document(key: str, value: str) -> bool:
    """Upsert one JSON document into ``bot_settings``. True only when the durable
    write landed, so the service never reports a setting as saved when it was not."""
    try:
        return await _run_sync(_bot_settings_put_sync, key, value)
    except Exception as exc:
        logger.error("[AED_DB] write %s FAILED: %s", key, exc)
        return False


def _aed_load_shards_sync(shard_keys: list[str]) -> list[list] | None:
    """Read every archive shard document. ``None`` means the store could not be
    read (an unreadable store is never shown as an empty one)."""
    import json

    db = get_db()
    rows: dict[str, str] = {}
    if db is None:
        for key in shard_keys:
            value = _fallback.get("bot_settings", {}).get(key)
            if isinstance(value, str):
                rows[key] = value
    else:
        try:
            result = (
                db.table("bot_settings")
                .select("key, value")
                .like("key", f"{_AED_SHARD_PREFIX}%")
                .execute()
            )
            for row in getattr(result, "data", None) or []:
                if isinstance(row, dict) and isinstance(row.get("key"), str):
                    rows[row["key"]] = row.get("value") or "[]"
        except Exception as exc:
            logger.error("[AED_DB] shard read FAILED: %s", exc)
            return None

    shards: list[list] = []
    for key in shard_keys:
        raw = rows.get(key)
        if not raw:
            shards.append([])
            continue
        try:
            parsed = json.loads(raw)
        except Exception:
            logger.warning("[AED_DB] shard %s is not valid JSON — treated as empty", key)
            parsed = []
        shards.append(parsed if isinstance(parsed, list) else [])
    return shards


async def aed_load_archive(shard_keys: list[str]) -> list[list] | None:
    """Load all archive shards (each a bounded list of rows), newest last."""
    try:
        return await _run_sync(_aed_load_shards_sync, list(shard_keys))
    except Exception as exc:
        logger.error("[AED_DB] shard load FAILED: %s", exc)
        return None


def _aed_save_shards_sync(shard_keys: list[str], shards: list[list]) -> bool:
    import json

    db = get_db()
    for index, key in enumerate(shard_keys):
        rows = shards[index] if index < len(shards) else []
        value = json.dumps(rows, separators=(",", ":"), default=str)
        if db is None:
            _fallback["bot_settings"][key] = value
            continue
        try:
            existing = db.table("bot_settings").select("key").eq("key", key).limit(1).execute()
            if getattr(existing, "data", None):
                db.table("bot_settings").update({"value": value}).eq("key", key).execute()
            else:
                db.table("bot_settings").insert(
                    {"key": key, "value": value, "value_type": "str"}
                ).execute()
        except Exception as exc:
            logger.error("[AED_DB] shard %s write FAILED: %s", key, exc)
            record_event("database", "write anti_edit_delete shard", 0, "ERROR", str(exc))
            return False
    return True


async def aed_save_archive(shard_keys: list[str], shards: list[list]) -> bool:
    """Persist every archive shard. True only when every write landed."""
    try:
        return await _run_sync(_aed_save_shards_sync, list(shard_keys), list(shards))
    except Exception as exc:
        logger.error("[AED_DB] shard save FAILED: %s", exc)
        return False


# ── Anti-Edit / Anti-Delete dedicated tables (aed_config / aed_archive_entries) ──
#
# AED originally persisted through the generic ``bot_settings`` key/value store:
# ONE configuration document (``anti_edit_delete_config:<owner>``) plus a fixed
# set of JSON shard documents (``anti_edit_delete_archive_shard:0..3``). The
# dedicated tables (migration ``20261017000001_create_anti_edit_delete_tables.sql``,
# DATABASE_ARCHITECTURE.md §34) replace that store.
#
# ONE source of truth, decided once per process: ``aed_store_mode`` probes whether
# the dedicated tables are readable and then routes EVERY AED read and write to
# that one store for the rest of the process. Before the migration is applied the
# probe finds no table and the feature keeps its current behaviour through the
# legacy shards; after it is applied the dedicated tables are the ONLY store (the
# legacy shards are neither read nor written), so the two stores can never
# disagree. Applying the migration therefore takes effect on the next service start.
#
# Every dedicated function returns ``None``/``False`` on failure (never raises), so
# the service can fail closed and never mistake an unreadable store for an empty
# one — exactly like the legacy helpers above.

#: The writable columns of ``aed_config``.
_AED_CONFIG_COLUMNS: tuple[str, ...] = ("enabled", "categories", "retention_days")

#: The writable columns of ``aed_archive_entries`` (``updated_at`` is added here).
_AED_ENTRY_COLUMNS: tuple[str, ...] = (
    "id", "owner_id", "chat_id", "message_id", "kind", "category", "has_caption",
    "captured_at", "message_date", "edited_at", "sender_id", "out", "has_media",
    "body", "media_type", "media_name", "media_size", "media_stored",
    "media_object", "recoverable", "recovered_from", "peer_id", "peer_username",
    "peer_name",
)

#: ``"dedicated"`` | ``"legacy"`` | ``None`` (not probed yet). Decided once.
_aed_store_mode: str | None = None


def reset_aed_store_mode() -> None:
    """Forget the cached store decision (tests / process restart)."""
    global _aed_store_mode
    _aed_store_mode = None


def _aed_probe_store_sync() -> tuple[str, bool]:
    """Decide the ONE AED store for this process: the dedicated tables when they
    are readable, the legacy ``bot_settings`` shards otherwise.

    Returns ``(mode, definitive)``. Only a DEFINITIVE result is cached: a missing
    table cannot appear mid-process without a restart, but a transient network or
    timeout error must not pin the feature to the legacy store forever, so it is
    used for this call only and the probe runs again next time.
    """
    db = get_db()
    if db is None:
        # The in-memory fallback implements the dedicated store directly.
        return "dedicated", True
    try:
        db.table("aed_config").select("owner_id").limit(1).execute()
        return "dedicated", True
    except Exception as exc:
        code = str(getattr(exc, "code", "") or "")
        message = str(exc).lower()
        definitive = (
            code in ("42P01", "PGRST205")
            or "does not exist" in message
            or "could not find the table" in message
        )
        logger.info(
            "[AED_DB] dedicated tables unavailable (%s, definitive=%s) — using the "
            "legacy bot_settings shards until the migration is applied",
            type(exc).__name__, definitive,
        )
        return "legacy", definitive


async def aed_store_mode() -> str:
    """The ONE AED store for this process (probed once, then cached)."""
    global _aed_store_mode
    if _aed_store_mode is not None:
        return _aed_store_mode
    try:
        mode, definitive = await _run_sync(_aed_probe_store_sync)
    except Exception as exc:
        logger.error("[AED_DB] store probe FAILED: %s", exc)
        return "legacy"
    if definitive:
        _aed_store_mode = mode
    return mode


def _aed_config_payload(document: dict) -> dict:
    """The writable half of a configuration document (unknown keys are never written)."""
    payload = {column: document[column] for column in _AED_CONFIG_COLUMNS if column in document}
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    return payload


def _aed_entry_payload(owner_id: int, row: dict) -> dict:
    """One archive row mapped onto its columns, with the durable identity pinned."""
    payload = {column: row.get(column) for column in _AED_ENTRY_COLUMNS}
    payload["id"] = str(row.get("id"))
    payload["owner_id"] = owner_id
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    return payload


def _get_aed_config_state_sync(owner_id: int) -> tuple[dict | None, bool]:
    """``(row, readable)`` for the owner's dedicated configuration row."""
    db = get_db()
    if db is None:
        row = _fallback["aed_config"].get(owner_id)
        return (dict(row) if row else None), True
    try:
        result = (
            db.table("aed_config")
            .select("*")
            .eq("owner_id", owner_id)
            .limit(1)
            .execute()
        )
        rows = getattr(result, "data", None) or []
        return (dict(rows[0]) if rows else None), True
    except Exception as exc:
        logger.error("[AED_DB] get aed_config FAILED: %s", exc)
        return None, False


async def aed_load_config_state(owner_id: int) -> tuple[dict | None, bool]:
    """``(row, readable)`` for the dedicated configuration.

    ``readable=False`` means the read FAILED; the service must then keep the
    feature OFF and must never persist a default document over the stored one.
    ``(None, True)`` is the honest "no row yet" case.
    """
    try:
        return await _run_sync(_get_aed_config_state_sync, owner_id)
    except Exception as exc:
        logger.error("[AED_DB] get aed_config FAILED: %s", exc)
        return None, False


async def aed_load_config(owner_id: int) -> dict | None:
    """The owner's dedicated configuration row, or None when no row exists yet
    OR the store could not be read. Both resolve to the fail-closed default in
    the service (a missing row means the feature is OFF).

    The configuration LOAD uses :func:`aed_load_config_state`, which keeps the two
    cases apart; this collapsed form stays for the retention sweep, where an
    unreadable row must simply not invent a longer retention.
    """
    row, _ok = await aed_load_config_state(owner_id)
    return row


def _save_aed_config_sync(owner_id: int, document: dict) -> bool:
    payload = _aed_config_payload(document)
    db = get_db()
    if db is None:
        row = dict(_fallback["aed_config"].get(owner_id) or {"owner_id": owner_id})
        row.update(payload)
        _fallback["aed_config"][owner_id] = row
        return True
    try:
        existing = (
            db.table("aed_config").select("owner_id").eq("owner_id", owner_id).limit(1).execute()
        )
        if getattr(existing, "data", None):
            db.table("aed_config").update(payload).eq("owner_id", owner_id).execute()
        else:
            db.table("aed_config").insert({"owner_id": owner_id, **payload}).execute()
        return True
    except Exception as exc:
        logger.error("[AED_DB] save aed_config FAILED: %s", exc)
        record_event("database", "save aed_config", 0, "ERROR", str(exc))
        return False


async def aed_save_config(owner_id: int, document: dict) -> bool:
    """Write the whole configuration document into the dedicated row. True only
    when the durable write landed."""
    try:
        return await _run_sync(_save_aed_config_sync, owner_id, document)
    except Exception as exc:
        logger.error("[AED_DB] save aed_config FAILED: %s", exc)
        return False


def _aed_entry_rows(owner_id: int | None) -> list[dict]:
    rows = _fallback["aed_archive_entries"]
    if owner_id is None:
        return [dict(r) for r in rows]
    return [dict(r) for r in rows if r.get("owner_id") == owner_id]


def _load_aed_entries_sync(owner_id: int | None) -> list[dict] | None:
    """Read this owner's archive rows (or every owner's when ``owner_id`` is
    None). ``None`` means the store could not be read."""
    db = get_db()
    if db is None:
        return _aed_entry_rows(owner_id)
    try:
        query = db.table("aed_archive_entries").select("*")
        if owner_id is not None:
            query = query.eq("owner_id", owner_id)
        result = query.order("captured_at", desc=True).execute()
        return [dict(row) for row in (getattr(result, "data", None) or [])]
    except Exception as exc:
        logger.error("[AED_DB] load aed_archive_entries FAILED: %s", exc)
        return None


async def aed_load_entries(owner_id: int) -> list[dict] | None:
    """Load THIS owner's archive rows, newest first. ``None`` means the store
    could not be read (never presented as an empty archive)."""
    try:
        return await _run_sync(_load_aed_entries_sync, owner_id)
    except Exception as exc:
        logger.error("[AED_DB] load aed_archive_entries FAILED: %s", exc)
        return None


async def aed_load_all_entries() -> list[dict] | None:
    """Load EVERY owner's archive rows (the retention worker sweeps all owners).
    ``None`` means the store could not be read."""
    try:
        return await _run_sync(_load_aed_entries_sync, None)
    except Exception as exc:
        logger.error("[AED_DB] load aed_archive_entries FAILED: %s", exc)
        return None


def _save_aed_entries_sync(owner_id: int, rows: list[dict]) -> bool:
    """Replace THIS owner's archive rows with ``rows`` (the caller always passes
    the complete bounded set). New rows are written first, then the owner's rows
    that are no longer present are pruned, so an interrupted write can never
    leave the store claiming rows the service no longer holds."""
    db = get_db()
    payloads = [_aed_entry_payload(owner_id, row) for row in rows]
    if db is None:
        kept = [dict(r) for r in _fallback["aed_archive_entries"] if r.get("owner_id") != owner_id]
        _fallback["aed_archive_entries"] = kept + payloads
        return True
    try:
        if payloads:
            db.table("aed_archive_entries").upsert(
                payloads, on_conflict="id"
            ).execute()
            db.table("aed_archive_entries").delete().eq("owner_id", owner_id).not_.in_(
                "id", [p["id"] for p in payloads]
            ).execute()
        else:
            db.table("aed_archive_entries").delete().eq("owner_id", owner_id).execute()
        return True
    except Exception as exc:
        logger.error("[AED_DB] save aed_archive_entries FAILED: %s", exc)
        record_event("database", "save aed_archive_entries", 0, "ERROR", str(exc))
        return False


async def aed_save_entries(owner_id: int, rows: list[dict]) -> bool:
    """Persist THIS owner's complete bounded archive row set. True only when the
    durable write landed."""
    try:
        return await _run_sync(_save_aed_entries_sync, owner_id, list(rows))
    except Exception as exc:
        logger.error("[AED_DB] save aed_archive_entries FAILED: %s", exc)
        return False


def _delete_aed_entries_sync(ids: list[str]) -> bool:
    db = get_db()
    wanted = [str(i) for i in ids if i]
    if db is None:
        drop = set(wanted)
        _fallback["aed_archive_entries"] = [
            r for r in _fallback["aed_archive_entries"] if str(r.get("id")) not in drop
        ]
        return True
    if not wanted:
        return True
    try:
        db.table("aed_archive_entries").delete().in_("id", wanted).execute()
        return True
    except Exception as exc:
        logger.error("[AED_DB] delete aed_archive_entries FAILED: %s", exc)
        record_event("database", "delete aed_archive_entries", 0, "ERROR", str(exc))
        return False


async def aed_delete_entries(ids: list[str]) -> bool:
    """Delete specific archive rows by id (the retention worker's sweep)."""
    try:
        return await _run_sync(_delete_aed_entries_sync, list(ids))
    except Exception as exc:
        logger.error("[AED_DB] delete aed_archive_entries FAILED: %s", exc)
        return False
