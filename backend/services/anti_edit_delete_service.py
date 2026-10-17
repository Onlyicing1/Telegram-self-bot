"""Anti-Edit / Anti-Delete archive service — the ONE owner of archive state.

The feature archives the owner's PRIVATE conversations: it records a bounded
snapshot of a private message while its content is still present, records each
edit version, and records a deletion event. It never fabricates content: a
deletion can only be marked recoverable when the original content was actually
observed and stored beforehand, and a message deleted while the bot was offline
(or before its content was captured) is recorded as an honest, unrecoverable
miss.

## What is stored, and where

Persistence reuses the EXISTING ``bot_settings`` durable key/value table (the
same store ``backend.services.ghost_seen_v2`` uses) — no new table and no
migration, because the canonical schema is documented in the protected
``DATABASE_ARCHITECTURE.md``. The configuration is one JSON document per owner;
the archive rows are split across a small, fixed number of shard documents so
neither a read nor a write grows without bound. When Supabase is unavailable the
db layer degrades to its in-memory fallback, exactly like every other feature.

## Media durability (honest limitation)

There is no durable media store in this repository: ``save_service`` downloads
to a temporary directory it then deletes, and no Supabase Storage bucket is used
anywhere. Media is therefore NOT archived by default. When the owner provisions
a Supabase Storage bucket and sets ``ARCHIVE_MEDIA_BUCKET``, a bounded snapshot
of the media is uploaded and its object path recorded (``media_stored=True``) so
cleanup can delete the object with the row; otherwise only the media metadata
(type, file name, size) is retained and ``media_stored`` stays ``False``. The
service never claims durable media archival it cannot deliver.

## Scope (applied identically to capture, browsing and cleanup)

Only genuine private (user-to-user) conversations with HUMAN accounts are
archived. Group/supergroup/channel chats (negative chat ids), Saved Messages
(``chat_id == owner_id``), messages sent via an inline bot, and Telegram BOT
accounts — decided by the RESOLVED peer entity's real ``User.bot`` flag, never
by a username/name heuristic — are excluded everywhere.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

logger = logging.getLogger(__name__)

# ── Content categories ───────────────────────────────────────────────────────

CATEGORY_TEXT = "text"
CATEGORY_CAPTIONS = "captions"
CATEGORY_PHOTOS = "photos"
CATEGORY_VIDEOS = "videos"
CATEGORY_VOICE = "voice"
CATEGORY_DOCUMENTS = "documents"
CATEGORY_OTHER = "other"

#: The category filter vocabulary the owner configures. ``captions`` is a filter
#: over media that carries a text caption (``has_caption``); the other values
#: match a row's single primary ``category``.
CATEGORIES: tuple[str, ...] = (
    CATEGORY_TEXT, CATEGORY_CAPTIONS, CATEGORY_PHOTOS,
    CATEGORY_VIDEOS, CATEGORY_VOICE, CATEGORY_DOCUMENTS, CATEGORY_OTHER,
)

#: The four typed media buckets a row's primary ``category`` may take (plus
#: ``text`` and ``other``). ``captions`` is never a primary category.
PRIMARY_CATEGORIES: tuple[str, ...] = (
    CATEGORY_TEXT, CATEGORY_PHOTOS, CATEGORY_VIDEOS,
    CATEGORY_VOICE, CATEGORY_DOCUMENTS, CATEGORY_OTHER,
)

KIND_SNAPSHOT = "snapshot"
KIND_EDIT = "edit"
KIND_DELETE = "delete"

#: Presentation-only kind: a LOGICAL message — the grouping of one snapshot plus
#: its observed edit versions. It is never persisted (every version keeps its own
#: ``snapshot``/``edit`` row); it exists only so browsing shows one list entry per
#: Telegram message instead of one per observed version.
KIND_MESSAGE = "message"

#: Prefix of a grouped browse id. Raw row ids are bare hex, so a leading ``g``
#: can never collide with one, and the two id spaces stay unambiguous.
GROUP_ID_PREFIX = "g"

# ── Bounds ───────────────────────────────────────────────────────────────────

MAX_BODY_CHARS = 4096           # Telegram's own text limit
MAX_CAPTION_CHARS = 1024
MAX_MEDIA_NAME_CHARS = 255
MAX_VERSIONS_PER_MESSAGE = 4    # one snapshot + up to 3 edit versions
MAX_ARCHIVE_ROWS = 500          # total retained rows across all shards
SHARD_COUNT = 4                 # fixed number of shard documents
ROWS_PER_SHARD = MAX_ARCHIVE_ROWS // SHARD_COUNT
PAGE_SIZE = 8                   # browse page size

DEFAULT_RETENTION_DAYS = 30
MIN_RETENTION_DAYS = 1
MAX_RETENTION_DAYS = 3650

#: Wall-clock bound for the idempotency registry (redelivered updates).
_HANDLED_TTL_S = 900.0
_HANDLED_MAX = 1024

#: Reject absurd media metadata rather than trusting it.
MAX_MEDIA_BYTES = 20 * 1024 * 1024

_CONFIG_KEY_PREFIX = "anti_edit_delete_config:"
_SHARD_KEYS = tuple(
    f"anti_edit_delete_archive_shard:{i}" for i in range(SHARD_COUNT)
)

_owner_id: int = 0

# ── In-memory cache ──────────────────────────────────────────────────────────

_state: dict[str, Any] = {}
_loaded = False
_load_task: asyncio.Task | None = None
_load_task_loop: Any = None
_rows: list[dict[str, Any]] = []
_rows_loaded = False
_lock: asyncio.Lock | None = None
_lock_loop: Any = None
_handled: dict[str, float] = {}


def configure_owner(owner_id: Any) -> None:
    global _owner_id
    if isinstance(owner_id, int) and not isinstance(owner_id, bool) and owner_id > 0:
        _owner_id = owner_id


def owner_id() -> int:
    return _owner_id


def _running_loop() -> Any:
    try:
        return asyncio.get_running_loop()
    except RuntimeError:
        return None


def _lock_for_loop() -> asyncio.Lock:
    """A serialization lock bound to the CURRENT loop (recreated if the loop
    changed, so a lock is never reused across loops)."""
    global _lock, _lock_loop
    loop = _running_loop()
    if _lock is None or _lock_loop is not loop:
        _lock = asyncio.Lock()
        _lock_loop = loop
    return _lock


def reset_in_memory() -> None:
    """Clear every in-memory cache (tests / process restart)."""
    global _state, _loaded, _load_task, _load_task_loop
    global _rows, _rows_loaded, _lock, _lock_loop, _owner_id
    _state = {}
    _loaded = False
    _load_task = None
    _load_task_loop = None
    _rows = []
    _rows_loaded = False
    _lock = None
    _lock_loop = None
    _owner_id = 0
    _handled.clear()
    _peer_bot_cache.clear()


# ── Config ───────────────────────────────────────────────────────────────────

def default_state() -> dict[str, Any]:
    return {
        "enabled": False,
        "categories": list(CATEGORIES),
        "retention_days": DEFAULT_RETENTION_DAYS,
    }


def sanitize_state(raw: Any) -> dict[str, Any]:
    """Coerce any stored document into the exact configuration contract.

    Unknown/malformed values fall back to the safe default; a category list is
    always a non-empty subset of the known vocabulary (an empty filter would
    archive nothing, which is never what the owner means by enabling).
    """
    state = default_state()
    if not isinstance(raw, dict):
        return state
    enabled = raw.get("enabled")
    if isinstance(enabled, bool):
        state["enabled"] = enabled
    cats = raw.get("categories")
    if isinstance(cats, list):
        valid = [c for c in cats if isinstance(c, str) and c in CATEGORIES]
        # de-duplicate while preserving the canonical order
        state["categories"] = [c for c in CATEGORIES if c in set(valid)] or list(CATEGORIES)
    days = raw.get("retention_days")
    if isinstance(days, int) and not isinstance(days, bool) and MIN_RETENTION_DAYS <= days <= MAX_RETENTION_DAYS:
        state["retention_days"] = days
    return state


def _config_key(owner: int) -> str:
    return f"{_CONFIG_KEY_PREFIX}{owner}"


async def _read_config(owner: int) -> dict[str, Any]:
    from backend.db import client as db_client

    raw = await db_client.aed_get_document(_config_key(owner))
    if raw is None:
        return default_state()
    try:
        return sanitize_state(json.loads(raw))
    except Exception:
        logger.warning("[AED] config document is not valid JSON — using defaults")
        return default_state()


async def ensure_loaded_async() -> None:
    """Single-flight load of this owner's configuration into memory."""
    global _loaded, _load_task, _load_task_loop
    owner = _owner_id
    loop = _running_loop()
    if _loaded and _state:
        return
    if _load_task is None or _load_task.done() or _load_task_loop is not loop:
        _load_task_loop = loop
        _load_task = asyncio.ensure_future(_read_config(owner))
    doc = await asyncio.shield(_load_task)
    _state.clear()
    _state.update(doc)
    _loaded = True


async def reload_async() -> None:
    global _loaded
    _loaded = False
    await ensure_loaded_async()


def state() -> dict[str, Any]:
    return dict(_state) if _state else default_state()


def is_enabled() -> bool:
    return bool(_state.get("enabled", False))


def categories() -> tuple[str, ...]:
    return tuple(_state.get("categories") or CATEGORIES)


def retention_days() -> int:
    return int(_state.get("retention_days", DEFAULT_RETENTION_DAYS))


async def _persist_state(candidate: dict[str, Any]) -> bool:
    from backend.db import client as db_client

    owner = _owner_id
    payload = json.dumps(candidate, separators=(",", ":"), default=str)
    ok = await db_client.aed_put_document(_config_key(owner), payload)
    if ok:
        _state.clear()
        _state.update(candidate)
    return ok


async def set_enabled(enabled: bool) -> bool:
    await ensure_loaded_async()
    candidate = state()
    candidate["enabled"] = bool(enabled)
    return await _persist_state(candidate)


async def set_retention_days(days: Any) -> bool:
    await ensure_loaded_async()
    if not isinstance(days, int) or isinstance(days, bool):
        return False
    if not (MIN_RETENTION_DAYS <= days <= MAX_RETENTION_DAYS):
        return False
    candidate = state()
    candidate["retention_days"] = days
    return await _persist_state(candidate)


async def set_category(category: str, enabled: bool) -> bool:
    """Toggle ONE content category. The filter may never become empty."""
    await ensure_loaded_async()
    if category not in CATEGORIES:
        return False
    candidate = state()
    current = list(candidate.get("categories") or [])
    if enabled and category not in current:
        current = [c for c in CATEGORIES if c in set(current) | {category}]
    elif not enabled and category in current:
        remaining = [c for c in CATEGORIES if c in set(current) - {category}]
        if not remaining:
            return False  # refuse to archive nothing
        current = remaining
    candidate["categories"] = current
    return await _persist_state(candidate)


# ── Scope ────────────────────────────────────────────────────────────────────

def eligible(
    *,
    owner: Any,
    chat_id: Any,
    is_private: Any,
    via_bot_id: Any = None,
    peer_is_bot: Any = None,
) -> tuple[bool, str]:
    """The ONE scope gate, shared by capture, browse and cleanup.

    A private chat id is the other user's POSITIVE id; groups/supergroups/
    channels are negative, and Saved Messages is the owner's own id.

    ``peer_is_bot`` is TRI-STATE: ``True`` only when the peer's RESOLVED
    Telegram ``User`` entity carries the real ``bot`` flag — a positive chat id
    alone is NOT proof of a human. ``None``/``False`` (entity missing or
    unresolved) never blocks here; callers keep their documented safe fallback
    rather than guessing an unclassified peer's humanity.
    """
    if not isinstance(owner, int) or isinstance(owner, bool) or owner <= 0:
        return False, "invalid_owner"
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id <= 0:
        return False, "not_private_chat"
    if is_private is not True:
        return False, "not_private_chat"
    if chat_id == owner:
        return False, "saved_messages"
    if via_bot_id:
        return False, "via_bot"
    if peer_is_bot is True:
        return False, "peer_is_bot"
    return True, "eligible"


# ── Peer bot classification (the RESOLVED entity, never a heuristic) ─────────

_PEER_BOT_CACHE_MAX = 512
_peer_bot_cache: dict[int, bool] = {}


def is_bot_entity(entity: Any) -> bool:
    """True ONLY for a resolved Telegram ``User`` entity whose real ``bot``
    flag is set. A missing, unresolved, or non-User entity returns False —
    absence of evidence is never evidence either way, so callers keep their
    existing safe fallback (documented in the capture handlers) instead of
    guessing a peer's humanity from usernames, names or id patterns.
    """
    if entity is None or type(entity).__name__ != "User":
        return False
    return getattr(entity, "bot", None) is True


def _cache_peer_bot(chat_id: int, value: bool) -> None:
    while len(_peer_bot_cache) >= _PEER_BOT_CACHE_MAX:
        _peer_bot_cache.pop(next(iter(_peer_bot_cache)))
    _peer_bot_cache[chat_id] = value


async def peer_bot_status(client: Any, chat_id: Any) -> bool:
    """The peer's real bot flag for one archive chat id, or ``False`` (UNKNOWN).

    ``True`` requires a RESOLVED bot ``User`` entity; anything else — no client,
    an unresolvable peer, an unresolved entity — stays False and is treated as
    unknown (rows are kept, never guessed human). Results are cached per chat id
    (bounded) so a browse page costs at most one resolution per unique peer over
    the process lifetime.
    """
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id <= 0 or client is None:
        return False
    if chat_id in _peer_bot_cache:
        return _peer_bot_cache[chat_id]
    try:
        entity = await client.get_entity(int(chat_id))
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("[AED] peer bot resolution failed: %s", type(exc).__name__)
        return False
    flagged = is_bot_entity(entity)
    _cache_peer_bot(chat_id, flagged)
    return flagged


async def filter_bot_items(client: Any, items: list[dict[str, Any]]) -> tuple[list[dict[str, Any]], int]:
    """Hide persisted archive items whose peer RESOLVES to a Telegram bot.

    Rows captured before the bot exclusion (or on a cold capture) carry no bot
    metadata, so the live entity check is the only reliable classification at
    browse time. Without a client, or when a peer cannot be resolved, the item
    is KEPT (the existing safe fallback) — the limitation is documented in the
    capture handlers. Returns ``(filtered_items, number_hidden)``.
    """
    if client is None or not items:
        return list(items), 0
    result: list[dict[str, Any]] = []
    hidden = 0
    for item in items:
        if await peer_bot_status(client, item.get("chat_id")):
            hidden += 1
            continue
        result.append(item)
    return result, hidden


# ── Content classification ───────────────────────────────────────────────────

def classify_category(msg: Any) -> str:
    """One primary category for a message, from its media (if any)."""
    media = getattr(msg, "media", None)
    if media is None:
        return CATEGORY_TEXT
    name = type(media).__name__
    if name == "MessageMediaPhoto":
        return CATEGORY_PHOTOS
    if name == "MessageMediaDocument":
        return _classify_document(media)
    if name == "MessageMediaWebPage":
        return CATEGORY_OTHER
    if name == "MessageMediaGeo" or name == "MessageMediaGeoLive":
        return CATEGORY_OTHER
    if name == "MessageMediaContact":
        return CATEGORY_OTHER
    return CATEGORY_OTHER


def _classify_document(media: Any) -> str:
    document = getattr(media, "document", None)
    for attr in getattr(document, "attributes", None) or []:
        attr_name = type(attr).__name__
        if attr_name == "DocumentAttributeAudio":
            return CATEGORY_VOICE if getattr(attr, "voice", False) else CATEGORY_OTHER
        if attr_name == "DocumentAttributeVideo":
            return CATEGORY_VIDEOS
        if attr_name == "DocumentAttributeSticker":
            return CATEGORY_OTHER
        if attr_name == "DocumentAttributeAnimated":
            return CATEGORY_OTHER
    return CATEGORY_DOCUMENTS


def media_metadata(msg: Any) -> dict[str, Any]:
    """Bounded, honest media metadata — never a fabricated file or size."""
    media = getattr(msg, "media", None)
    if media is None:
        return {"media_type": None, "media_name": None, "media_size": None}
    media_type = type(media).__name__.replace("MessageMedia", "") or "Unknown"
    name = None
    size = None
    document = getattr(media, "document", None)
    if document is not None:
        raw_size = getattr(document, "size", None)
        if isinstance(raw_size, int) and not isinstance(raw_size, bool) and 0 <= raw_size <= MAX_MEDIA_BYTES:
            size = raw_size
        for attr in getattr(document, "attributes", None) or []:
            if type(attr).__name__ == "DocumentAttributeFilename":
                fname = getattr(attr, "file_name", None)
                if isinstance(fname, str) and fname:
                    name = fname[:MAX_MEDIA_NAME_CHARS]
                break
    return {"media_type": media_type, "media_name": name, "media_size": size}


def body_of(msg: Any, category: str) -> str:
    text = getattr(msg, "message", None) or getattr(msg, "text", None) or ""
    if not isinstance(text, str):
        return ""
    limit = MAX_CAPTION_CHARS if category != CATEGORY_TEXT else MAX_BODY_CHARS
    return text[:limit]


def _iso(value: Any) -> str | None:
    if isinstance(value, datetime):
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.astimezone(timezone.utc).isoformat()
    return None


# ── Peer identity (bounded, optional, never inferred) ────────────────────────

MAX_USERNAME_CHARS = 64
MAX_PEER_NAME_CHARS = 128


def clean_username(value: Any) -> str | None:
    """A public ``@username`` without the sigil, or ``None``.

    Usernames are optional and changeable, so a malformed one is dropped rather
    than stored — the numeric id is always the identifier.
    """
    if not isinstance(value, str):
        return None
    cleaned = value.strip().lstrip("@").strip()
    if not cleaned or len(cleaned) > MAX_USERNAME_CHARS:
        return None
    if not all(ch.isalnum() or ch == "_" for ch in cleaned):
        return None
    return cleaned


def clean_peer_name(value: Any) -> str | None:
    """A display name, bounded. Never treated as an identifier."""
    if not isinstance(value, str):
        return None
    cleaned = " ".join(value.split())
    if not cleaned:
        return None
    return cleaned[:MAX_PEER_NAME_CHARS]


def peer_identity(row: Any) -> dict[str, Any]:
    """The OTHER participant's stored identity for one archive row.

    Backward compatible by construction: rows captured before these fields
    existed simply carry no username/name, and the caller falls back to the
    numeric peer id (which equals ``chat_id`` in a private chat). Nothing is
    ever invented.
    """
    if not isinstance(row, dict):
        return {"peer_id": None, "username": None, "name": None}
    peer = row.get("peer_id")
    if not isinstance(peer, int) or isinstance(peer, bool):
        chat_id = row.get("chat_id")
        peer = chat_id if isinstance(chat_id, int) and not isinstance(chat_id, bool) else None
    return {
        "peer_id": peer,
        "username": clean_username(row.get("peer_username")),
        "name": clean_peer_name(row.get("peer_name")),
    }


def describe_peer(row: Any) -> str:
    """One bounded display line: ``@username`` when known, the numeric peer id
    always, plus a display name when one was reliably observed."""
    identity = peer_identity(row)
    parts: list[str] = []
    if identity["username"]:
        parts.append(f"@{identity['username']}")
    if identity["name"]:
        parts.append(identity["name"])
    parts.append(f"User ID: {identity['peer_id'] if identity['peer_id'] is not None else 'unknown'}")
    return " · ".join(parts)


def _identity_fields(identity: dict[str, Any] | None) -> dict[str, Any]:
    identity = identity if isinstance(identity, dict) else {}
    return {
        "peer_id": _as_id(identity.get("peer_id")) or None,
        "peer_username": clean_username(identity.get("username")),
        "peer_name": clean_peer_name(identity.get("name")),
    }


def make_id(*, owner: int, chat_id: int, message_id: int, kind: str, version: str) -> str:
    """Deterministic archive id — the idempotency key for a redelivered update."""
    raw = f"{owner}|{chat_id}|{message_id}|{kind}|{version}".encode("utf-8")
    return hashlib.sha1(raw).hexdigest()[:16]


def already_handled(key: str, monotonic_now: float | None = None) -> bool:
    """True when this exact archive id was already recorded (bounded, timed)."""
    now = time.monotonic() if monotonic_now is None else monotonic_now
    for stale in [k for k, stamp in _handled.items() if now - stamp > _HANDLED_TTL_S]:
        _handled.pop(stale, None)
    if key in _handled:
        _handled[key] = now
        return True
    _handled[key] = now
    while len(_handled) > _HANDLED_MAX:
        _handled.pop(next(iter(_handled)))
    return False


# ── Archive rows ─────────────────────────────────────────────────────────────

def _split_shards(rows: list[dict[str, Any]]) -> list[list[dict[str, Any]]]:
    shards: list[list[dict[str, Any]]] = []
    for index in range(SHARD_COUNT):
        start = index * ROWS_PER_SHARD
        shards.append(rows[start:start + ROWS_PER_SHARD])
    return shards


async def load_rows_async(force: bool = False) -> list[dict[str, Any]] | None:
    """Load and bound the archive rows for THIS owner, newest first.

    ``None`` means the store could not be read — the caller must not present an
    unreadable store as an empty one.
    """
    global _rows, _rows_loaded
    if _rows_loaded and not force:
        return [dict(r) for r in _rows]
    from backend.db import client as db_client

    shards = await db_client.aed_load_archive(list(_SHARD_KEYS))
    if shards is None:
        return None
    owner = _owner_id
    rows: list[dict[str, Any]] = []
    for shard in shards:
        for row in shard:
            if isinstance(row, dict) and row.get("owner_id") == owner:
                rows.append(row)
    rows.sort(key=lambda r: str(r.get("captured_at") or ""), reverse=True)
    rows = rows[:MAX_ARCHIVE_ROWS]
    _rows = rows
    _rows_loaded = True
    return [dict(r) for r in rows]


async def _save_rows(rows: list[dict[str, Any]]) -> bool:
    global _rows, _rows_loaded
    from backend.db import client as db_client

    bounded = rows[:MAX_ARCHIVE_ROWS]
    ok = await db_client.aed_save_archive(list(_SHARD_KEYS), _split_shards(bounded))
    if ok:
        _rows = bounded
        _rows_loaded = True
    return ok


def _versions_for(rows: list[dict[str, Any]], chat_id: int, message_id: int) -> list[dict[str, Any]]:
    return [
        r for r in rows
        if r.get("chat_id") == chat_id
        and r.get("message_id") == message_id
        and r.get("kind") in (KIND_SNAPSHOT, KIND_EDIT)
    ]


def versions_for(rows: list[dict[str, Any]], chat_id: Any, message_id: Any) -> list[dict[str, Any]]:
    """Public accessor: the observed content versions of one message."""
    return _versions_for(rows, chat_id, message_id)


def _latest_content(rows: list[dict[str, Any]], chat_id: int, message_id: int) -> dict[str, Any] | None:
    """The most recent observed content for a message, if any (never fabricated)."""
    versions = _versions_for(rows, chat_id, message_id)
    if not versions:
        return None
    versions.sort(key=lambda r: str(r.get("captured_at") or ""), reverse=True)
    return versions[0]


async def record_message(
    *,
    msg: Any,
    kind: str,
    owner: int | None = None,
    identity: dict[str, Any] | None = None,
    peer_is_bot: Any = None,
) -> dict[str, Any] | None:
    """Record a snapshot (new message) or edit version. Returns the row, or None
    when out of scope / disabled / duplicate / not persisted.

    ``identity`` is the bounded peer metadata the handler resolved from the
    event's own chat entity (``{"peer_id", "username", "name"}``). It is
    optional: when absent the row is still captured, only without a username.

    ``peer_is_bot`` is the handler's classification from the SAME resolved
    entity (tri-state; only a confirmed ``True`` blocks inside ``eligible``).
    """
    owner = owner or _owner_id
    await ensure_loaded_async()
    if not is_enabled():
        return None
    chat_id = int(getattr(msg, "chat_id", 0) or 0)
    message_id = int(getattr(msg, "id", 0) or 0)
    if message_id <= 0:
        return None
    ok, _reason = eligible(
        owner=owner,
        chat_id=chat_id,
        is_private=_is_private_chat(msg),
        via_bot_id=getattr(msg, "via_bot_id", None),
        peer_is_bot=peer_is_bot,
    )
    if not ok:
        return None

    category = classify_category(msg)
    has_caption = category != CATEGORY_TEXT and bool(getattr(msg, "message", None))
    if not _category_enabled(category, has_caption):
        return None

    if kind == KIND_EDIT:
        version = _iso(getattr(msg, "edit_date", None)) or _iso(getattr(msg, "date", None)) or ""
    else:
        version = _iso(getattr(msg, "date", None)) or ""
    archive_id = make_id(owner=owner, chat_id=chat_id, message_id=message_id, kind=kind, version=version)
    if already_handled(archive_id):
        return None

    media = media_metadata(msg)
    row = {
        "id": archive_id,
        "owner_id": owner,
        "chat_id": chat_id,
        "message_id": message_id,
        "kind": kind,
        "category": category,
        "has_caption": has_caption,
        "captured_at": datetime.now(timezone.utc).isoformat(),
        "message_date": _iso(getattr(msg, "date", None)),
        "edited_at": _iso(getattr(msg, "edit_date", None)),
        "sender_id": _as_id(getattr(msg, "sender_id", None)),
        "out": bool(getattr(msg, "out", False)),
        "has_media": getattr(msg, "media", None) is not None,
        "body": body_of(msg, category),
        "media_type": media["media_type"],
        "media_name": media["media_name"],
        "media_size": media["media_size"],
        "media_stored": False,
        "media_object": None,
        "recoverable": kind != KIND_DELETE,
        "recovered_from": None,
        **_identity_fields(identity),
    }
    async with _lock_for_loop():
        rows = await load_rows_async(force=True)
        if rows is None:
            logger.warning("[AED] archive store unreadable — capture skipped (fail closed)")
            return None
        existing = [r for r in rows if r.get("id") == archive_id]
        if existing:
            return None
        # Bound the number of versions kept for one message.
        same = _versions_for(rows, chat_id, message_id)
        if len(same) >= MAX_VERSIONS_PER_MESSAGE:
            same.sort(key=lambda r: str(r.get("captured_at") or ""))
            oldest = same[0].get("id")
            rows = [r for r in rows if r.get("id") != oldest]
        rows.insert(0, row)
        if not await _save_rows(rows):
            logger.warning("[AED] archive write failed — capture not persisted")
            return None
    return row


async def record_deletion(
    *,
    event: Any,
    owner: int | None = None,
    peer_is_bot: Any = None,
) -> list[dict[str, Any]]:
    """Record deletion events honestly.

    A deletion row is ``recoverable`` only when the original content was observed
    and stored BEFORE the deletion; otherwise the row records the event with an
    empty body and ``recoverable=False`` (an honest miss — nothing is fabricated).

    ``peer_is_bot`` is the handler's classification from the event's own peer
    entity when Telegram provides one (tri-state; only ``True`` blocks).
    """
    owner = owner or _owner_id
    await ensure_loaded_async()
    if not is_enabled():
        return []
    chat_id = int(getattr(event, "chat_id", 0) or 0)
    ok, _reason = eligible(
        owner=owner,
        chat_id=chat_id,
        is_private=_is_private_event(event),
        via_bot_id=getattr(event, "via_bot_id", None),
        peer_is_bot=peer_is_bot,
    )
    if not ok:
        return []

    deleted_ids = list(getattr(event, "deleted_ids", None) or [])
    if not deleted_ids:
        single = getattr(event, "message_id", None)
        if single:
            deleted_ids = [single]

    recorded: list[dict[str, Any]] = []
    async with _lock_for_loop():
        rows = await load_rows_async(force=True)
        if rows is None:
            logger.warning("[AED] archive store unreadable — deletion not recorded (fail closed)")
            return []
        for message_id in deleted_ids:
            message_id = _as_id(message_id)
            if not message_id:
                continue
            archive_id = make_id(
                owner=owner, chat_id=chat_id, message_id=message_id, kind=KIND_DELETE, version="0",
            )
            if already_handled(archive_id) or any(r.get("id") == archive_id for r in rows):
                continue
            previous = _latest_content(rows, chat_id, message_id)
            category = previous.get("category") if previous else CATEGORY_TEXT
            has_caption = bool(previous.get("has_caption")) if previous else False
            if not _category_enabled(category, has_caption):
                continue
            row = {
                "id": archive_id,
                "owner_id": owner,
                "chat_id": chat_id,
                "message_id": message_id,
                "kind": KIND_DELETE,
                "category": category,
                "has_caption": has_caption,
                "captured_at": datetime.now(timezone.utc).isoformat(),
                "message_date": previous.get("message_date") if previous else None,
                "edited_at": None,
                "sender_id": previous.get("sender_id") if previous else None,
                "out": bool(previous.get("out")) if previous else False,
                "has_media": bool(previous.get("has_media")) if previous else False,
                "body": previous.get("body", "") if previous else "",
                "media_type": previous.get("media_type") if previous else None,
                "media_name": previous.get("media_name") if previous else None,
                "media_size": previous.get("media_size") if previous else None,
                "media_stored": bool(previous.get("media_stored")) if previous else False,
                "media_object": previous.get("media_object") if previous else None,
                "recoverable": previous is not None,
                "recovered_from": previous.get("id") if previous else None,
                "peer_id": previous.get("peer_id") if previous else chat_id,
                "peer_username": previous.get("peer_username") if previous else None,
                "peer_name": previous.get("peer_name") if previous else None,
            }
            rows.insert(0, row)
            recorded.append(row)
        if recorded and not await _save_rows(rows):
            logger.warning("[AED] archive write failed — deletion(s) not persisted")
            return []
    return recorded


# ── Browsing / mutation ──────────────────────────────────────────────────────

def filter_rows(
    rows: list[dict[str, Any]],
    *,
    category: str | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    result = list(rows)
    if category and category in CATEGORIES:
        if category == CATEGORY_CAPTIONS:
            result = [r for r in result if r.get("has_caption")]
        else:
            result = [r for r in result if r.get("category") == category]
    if query:
        needle = query.strip().lower()
        if needle:
            result = [
                r for r in result
                if needle in str(r.get("body") or "").lower()
                or needle in str(r.get("media_name") or "").lower()
            ]
    return result


# ── Logical-message grouping (presentation only) ─────────────────────────────

def group_id(chat_id: Any, message_id: Any) -> str:
    """The deterministic browse id of one logical message."""
    return f"{GROUP_ID_PREFIX}{chat_id}:{message_id}"


def _identity_from(versions: list[dict[str, Any]]) -> dict[str, Any]:
    """The best stored peer identity across a message's versions.

    A newer version that lost the username (Telegram does not resend entities on
    every edit) must not erase one already observed, so the first non-empty value
    wins. Nothing is ever inferred.
    """
    for row in versions:
        identity = peer_identity(row)
        if identity["peer_id"] is not None or identity["username"] or identity["name"]:
            return identity
    return {"peer_id": None, "username": None, "name": None}


def build_items(rows: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Collapse snapshot/edit rows into ONE item per logical message.

    Deletion rows are NEVER collapsed: a deletion is an independent archive event
    and stays its own list entry with its own recoverability display, even when
    the same message also has observed content versions.
    """
    groups: dict[tuple[Any, Any], dict[str, Any]] = {}
    order: list[Any] = []
    deletes: list[dict[str, Any]] = []
    for row in rows:
        if not isinstance(row, dict):
            continue
        if row.get("kind") == KIND_DELETE:
            deletes.append(row)
            continue
        if row.get("kind") not in (KIND_SNAPSHOT, KIND_EDIT):
            continue
        key = (row.get("chat_id"), row.get("message_id"))
        group = groups.get(key)
        if group is None:
            group = {"chat_id": key[0], "message_id": key[1], "versions": []}
            groups[key] = group
            order.append(key)
        group["versions"].append(row)

    items: list[dict[str, Any]] = []
    for key in order:
        group = groups[key]
        versions = group["versions"]
        latest = max(versions, key=lambda r: str(r.get("captured_at") or ""))
        body = latest.get("body") or ""
        media_name = latest.get("media_name")
        has_media = bool(latest.get("has_media"))
        if not body and not media_name and has_media:
            # Honest fallback: a media-only message has no text to show.
            body = f"({latest.get('media_type') or 'media'} — no text)"
        items.append({
            "id": group_id(key[0], key[1]),
            "kind": KIND_MESSAGE,
            "chat_id": key[0],
            "message_id": key[1],
            "category": latest.get("category"),
            "has_caption": bool(latest.get("has_caption")),
            "body": body,
            "media_name": media_name,
            "has_media": has_media,
            "media_type": latest.get("media_type"),
            "media_stored": bool(latest.get("media_stored")),
            "captured_at": latest.get("captured_at"),
            "version_count": len(versions),
            "has_deletion": any(
                d.get("chat_id") == key[0] and d.get("message_id") == key[1] for d in deletes
            ),
            "versions": sorted(versions, key=lambda r: str(r.get("captured_at") or ""), reverse=True),
        })
        items[-1]["identity"] = _identity_from(versions)

    for row in deletes:
        items.append({
            "id": row.get("id"),
            "kind": KIND_DELETE,
            "chat_id": row.get("chat_id"),
            "message_id": row.get("message_id"),
            "category": row.get("category"),
            "has_caption": bool(row.get("has_caption")),
            "body": row.get("body") or "",
            "media_name": row.get("media_name"),
            "has_media": bool(row.get("has_media")),
            "media_type": row.get("media_type"),
            "media_stored": bool(row.get("media_stored")),
            "captured_at": row.get("captured_at"),
            "version_count": 1,
            "has_deletion": True,
            "recoverable": bool(row.get("recoverable")),
            "identity": peer_identity(row),
            "row": row,
        })

    items.sort(key=lambda i: str(i.get("captured_at") or ""), reverse=True)
    return items


def filter_items(
    items: list[dict[str, Any]],
    *,
    category: str | None = None,
    query: str | None = None,
) -> list[dict[str, Any]]:
    """The same category/query filter, applied to grouped browse items."""
    result = list(items)
    if category and category in CATEGORIES:
        if category == CATEGORY_CAPTIONS:
            result = [i for i in result if i.get("has_caption")]
        else:
            result = [i for i in result if i.get("category") == category]
    if query:
        needle = query.strip().lower()
        if needle:
            result = [
                i for i in result
                if needle in str(i.get("body") or "").lower()
                or needle in str(i.get("media_name") or "").lower()
            ]
    return result


def get_item(items: list[dict[str, Any]], browse_id: str) -> dict[str, Any] | None:
    for item in items:
        if item.get("id") == browse_id:
            return item
    return None


def page_of(rows: list[dict[str, Any]], page: int) -> tuple[list[dict[str, Any]], int, int]:
    total = len(rows)
    pages = max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)
    page = max(1, min(page, pages))
    start = (page - 1) * PAGE_SIZE
    return rows[start:start + PAGE_SIZE], page, pages


def get_row(rows: list[dict[str, Any]], archive_id: str) -> dict[str, Any] | None:
    for row in rows:
        if row.get("id") == archive_id:
            return row
    return None


async def delete_row(archive_id: str) -> bool:
    """Delete ONE archive row of THIS owner (ownership enforced)."""
    async with _lock_for_loop():
        rows = await load_rows_async(force=True)
        if rows is None:
            return False
        target = get_row(rows, archive_id)
        if target is None:
            return False
        remaining = [r for r in rows if r.get("id") != archive_id]
        if not await _save_rows(remaining):
            return False
    _delete_media_object(target.get("media_object"))
    return True


async def delete_group(chat_id: Any, message_id: Any) -> int:
    """Delete the WHOLE logical message: its snapshot and every observed edit.

    Deletion-event rows for the same message are independent archive events and
    are deliberately left in place — deleting the content history must not erase
    the record that the message was deleted. Returns the number of content rows
    removed (0 when there was nothing to remove), or -1 when the store failed.
    """
    async with _lock_for_loop():
        rows = await load_rows_async(force=True)
        if rows is None:
            return -1
        victims = [
            r for r in rows
            if r.get("chat_id") == chat_id
            and r.get("message_id") == message_id
            and r.get("kind") in (KIND_SNAPSHOT, KIND_EDIT)
        ]
        if not victims:
            return 0
        victim_ids = {r.get("id") for r in victims}
        remaining = [r for r in rows if r.get("id") not in victim_ids]
        if not await _save_rows(remaining):
            return -1
    for row in victims:
        _delete_media_object(row.get("media_object"))
    return len(victims)


async def clear_archive() -> int:
    """Delete every archive row of THIS owner. Returns the number removed."""
    async with _lock_for_loop():
        rows = await load_rows_async(force=True)
        if rows is None:
            return -1
        mine = [r for r in rows if r.get("owner_id") == _owner_id]
        remaining = [r for r in rows if r.get("owner_id") != _owner_id]
        if not mine:
            return 0
        if not await _save_rows(remaining):
            return -1
    for row in mine:
        _delete_media_object(row.get("media_object"))
    return len(mine)


# ── Retention cleanup (rides the EXISTING memory-cleanup worker) ─────────────

def _cutoff(now: datetime, days: int) -> datetime:
    return now - timedelta(days=max(MIN_RETENTION_DAYS, int(days)))


def _parse_instant(value: Any) -> datetime | None:
    if not isinstance(value, str) or not value:
        return None
    try:
        parsed = datetime.fromisoformat(value)
    except Exception:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed


async def cleanup_expired(now: datetime | None = None) -> dict[str, int]:
    """Delete archive rows (and their stored media objects) past retention.

    Called by ``backend.runtime.memory_cleanup`` — the ONE existing periodic
    worker. Returns the exact statistics keys that worker reads. It never deletes
    an unexpired row: a row with an unparseable ``captured_at`` is treated as
    recent (kept), and retention is read per owner from the persisted config.
    """
    from backend.db import client as db_client

    stats = {"archive_deleted": 0, "snapshots_deleted": 0, "objects_deleted": 0}
    shards = await db_client.aed_load_archive(list(_SHARD_KEYS))
    if shards is None:
        return stats

    now = now or datetime.now(timezone.utc)
    owners = {
        row.get("owner_id")
        for shard in shards for row in shard
        if isinstance(row, dict) and isinstance(row.get("owner_id"), int)
    }
    retention: dict[int, int] = {}
    for owner in owners:
        doc = await db_client.aed_get_document(_config_key(owner))
        days = DEFAULT_RETENTION_DAYS
        if doc:
            try:
                days = sanitize_state(json.loads(doc)).get("retention_days", DEFAULT_RETENTION_DAYS)
            except Exception:
                days = DEFAULT_RETENTION_DAYS
        retention[owner] = days

    kept_shards: list[list[dict[str, Any]]] = []
    expired_objects: list[Any] = []
    for shard in shards:
        kept: list[dict[str, Any]] = []
        for row in shard:
            if not isinstance(row, dict):
                continue
            owner = row.get("owner_id")
            days = retention.get(owner, DEFAULT_RETENTION_DAYS)
            captured = _parse_instant(row.get("captured_at"))
            if captured is None or captured >= _cutoff(now, days):
                kept.append(row)
                continue
            if row.get("kind") == KIND_DELETE:
                stats["archive_deleted"] += 1
            else:
                stats["snapshots_deleted"] += 1
            if row.get("media_object"):
                expired_objects.append(row.get("media_object"))
        kept_shards.append(kept)

    if stats["archive_deleted"] or stats["snapshots_deleted"]:
        if not await db_client.aed_save_archive(list(_SHARD_KEYS), kept_shards):
            logger.warning("[AED] retention save failed — nothing was deleted durably")
            return {"archive_deleted": 0, "snapshots_deleted": 0, "objects_deleted": 0}

    for path in expired_objects:
        if _delete_media_object(path):
            stats["objects_deleted"] += 1
    return stats


# ── Media object storage (optional; honest no-op without a bucket) ───────────

def _bucket() -> str:
    import os

    return os.getenv("ARCHIVE_MEDIA_BUCKET", "") or ""


def media_storage_available() -> bool:
    """True only when a Supabase Storage bucket is configured AND Supabase is up."""
    from backend.db import client as db_client

    return bool(_bucket()) and db_client.get_db() is not None


def _delete_media_object(path: Any) -> bool:
    """Delete one stored media object. No-op (False) without a configured bucket."""
    if not isinstance(path, str) or not path:
        return False
    bucket = _bucket()
    if not bucket:
        return False
    try:
        from backend.db import client as db_client

        db = db_client.get_db()
        if db is None:
            return False
        db.storage.from_(bucket).remove([path])
        return True
    except Exception as exc:
        logger.warning("[AED] media object delete failed: %s", type(exc).__name__)
        return False


async def store_media_snapshot(client: Any, msg: Any, row: dict[str, Any]) -> bool:
    """Upload a bounded media snapshot to the configured bucket, if any.

    Without a configured bucket this is an honest no-op returning False — the
    service never claims durable media archival it cannot deliver.
    """
    if not media_storage_available() or getattr(msg, "media", None) is None:
        return False
    size = row.get("media_size")
    if isinstance(size, int) and size > MAX_MEDIA_BYTES:
        logger.info("[AED] media exceeds snapshot limit (%s bytes) — metadata only", size)
        return False
    try:
        import tempfile

        from backend.db import client as db_client

        bucket = _bucket()
        tmp_dir = tempfile.mkdtemp(prefix="lifeos_aed_")
        try:
            path = await client.download_media(msg, file=tmp_dir)
            if not path:
                return False
            object_name = f"{row['owner_id']}/{row['id']}"
            db = db_client.get_db()
            if db is None:
                return False
            with open(path, "rb") as handle:
                db.storage.from_(bucket).upload(object_name, handle.read())
            row["media_stored"] = True
            row["media_object"] = object_name
            return True
        finally:
            import shutil

            shutil.rmtree(tmp_dir, ignore_errors=True)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("[AED] media snapshot failed: %s", type(exc).__name__)
        return False


# ── Helpers ──────────────────────────────────────────────────────────────────

def _category_enabled(category: str, has_caption: bool) -> bool:
    configured = set(categories())
    if has_caption and CATEGORY_CAPTIONS in configured:
        return True
    if category in configured:
        return True
    return False


def _as_id(value: Any) -> int:
    if isinstance(value, int) and not isinstance(value, bool):
        return value
    return 0


def _is_private_chat(msg: Any) -> bool:
    # A message in a private chat has a positive chat_id equal to the peer's id.
    chat_id = getattr(msg, "chat_id", None)
    return isinstance(chat_id, int) and not isinstance(chat_id, bool) and chat_id > 0


def _is_private_event(event: Any) -> bool:
    is_private = getattr(event, "is_private", None)
    if isinstance(is_private, bool):
        return is_private
    chat_id = getattr(event, "chat_id", None)
    return isinstance(chat_id, int) and not isinstance(chat_id, bool) and chat_id > 0


def summary() -> dict[str, Any]:
    """A small, bounded, non-sensitive summary for the panel."""
    return {
        "enabled": is_enabled(),
        "categories": list(categories()),
        "retention_days": retention_days(),
        "media_storage": media_storage_available(),
    }
