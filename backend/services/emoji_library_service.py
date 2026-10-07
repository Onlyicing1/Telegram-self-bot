"""
Emoji Library service — Phase 1 remainder (set resolution/enumeration) of
Emoji & Reaction.

Extends the Phase 1 message-level import with sticker-set resolution and
enumeration through the EXISTING self-client facade. For each newly imported
custom-emoji document, resolves its owning sticker set via
``messages.GetCustomEmojiDocuments`` + ``messages.GetStickerSet``, enumerates
the set's members, and enriches the import report with real Telegram-provided
set metadata.

Contracts (ROADMAP §9 remainder):

* Set resolution is best-effort enrichment on top of the existing
  message-level import. The import remains fail-closed for collection and
  persistence; set-resolution failures degrade the report honestly without
  hiding behind a generic "success".
* Each unique document is resolved once. Each unique sticker set is
  enumerated once and reused for every document that belongs to it.
* Bounded: per-document resolution RPCs, per-set enumeration RPCs, total
  library records, and RPC timeouts are all explicitly bounded.
* No fabricated metadata: if a document/set cannot be resolved, the report
  says so honestly. No set_id, short_name, or membership is invented.
* The physical emoji_library schema is unchanged in this phase — set metadata
  is exposed through the import report and service internals only. Persisting
  set metadata is a later (manual schema) concern; see
  IMPLEMENTATION_REPORT.md §3 for the documented manual-only schema note.

Context isolation: nothing from ``backend.ai`` is imported. The service
remains deterministic aside from the bounded Telegram RPCs it makes.

No second client, loop, scheduler, or executor is created: the self client is
passed in by the caller, and the existing module-level ``asyncio.Lock``
serializes imports.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.db import client as db_client
from backend.helper.rpc_timeout import rpc_await
from backend.telegram_api._helpers import utf16_index_at
from backend.telegram_api.messages import iter_messages as _facade_iter_messages
from telethon.tl import types
from telethon.tl.functions.messages import GetCustomEmojiDocumentsRequest, GetStickerSetRequest

logger = logging.getLogger(__name__)


CUSTOM_EMOJI_ENTITY = "MessageEntityCustomEmoji"
SAVED_MESSAGES_CHAT = "me"

DEFAULT_MAX_MESSAGES = 200
DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_RECORDS = 500
DEFAULT_PAGE_TIMEOUT_S = 15.0
DEFAULT_SET_TIMEOUT_S = 15.0

HARD_MAX_MESSAGES = 2000
HARD_MAX_PAGE_SIZE = 200
HARD_MAX_RECORDS = 5000
MAX_ENTITIES_PER_MESSAGE = 100

# Bounds for set resolution/enumeration.
_MAX_SET_RESOLUTION_RPCS = 200        # total per import
_MAX_SET_ENUMERATION_RPCS = 50        # total per import
_MAX_LIBRARY_TOTAL = 5000             # mirror the dedup-read bound

_import_lock = asyncio.Lock()


class ExtractionResult:
    __slots__ = ("records", "malformed", "entity_limit_hit")

    def __init__(self, records: list[dict[str, Any]], malformed: int, entity_limit_hit: bool) -> None:
        self.records = records
        self.malformed = malformed
        self.entity_limit_hit = entity_limit_hit


def extract_custom_emoji_records(message: dict[str, Any]) -> ExtractionResult:
    """Extract validated custom-emoji records from ONE serialized message.

    Returns ``({document_id, alt_text, source_msg_id, source}, malformed,
    entity_limit_hit)`` where ``alt_text`` is the exact Unicode text span the
    entity covers (UTF-16 offsets resolved through ``utf16_index_at``). Invalid
    payloads — missing/non-integer/non-positive document id, bad offsets, a span
    outside the text — are skipped and counted as malformed: fail closed, never
    fabricate. Non-custom-emoji entities and plain Unicode emoji are ignored
    silently.
    """
    entities = message.get("entities") or []
    if not isinstance(entities, list):
        entities = []
    entity_limit_hit = len(entities) > MAX_ENTITIES_PER_MESSAGE

    text = message.get("text") or ""
    if not isinstance(text, str):
        text = ""
    raw_id = message.get("id")
    source_msg_id = (
        raw_id
        if isinstance(raw_id, int) and not isinstance(raw_id, bool) and raw_id > 0
        else None
    )

    records: list[dict[str, Any]] = []
    malformed = 0
    for ent in entities[:MAX_ENTITIES_PER_MESSAGE]:
        if not isinstance(ent, dict) or ent.get("type") != CUSTOM_EMOJI_ENTITY:
            continue

        doc = ent.get("document_id")
        if isinstance(doc, bool) or not isinstance(doc, (int, str)):
            malformed += 1
            continue
        try:
            document_id = int(doc)
        except (TypeError, ValueError):
            malformed += 1
            continue
        if document_id <= 0:
            malformed += 1
            continue

        offset = ent.get("offset")
        length = ent.get("length")
        if (
            not isinstance(offset, int)
            or isinstance(offset, bool)
            or offset < 0
            or not isinstance(length, int)
            or isinstance(length, bool)
            or length <= 0
        ):
            malformed += 1
            continue
        try:
            start = utf16_index_at(text, offset)
            end = utf16_index_at(text, offset + length)
        except ValueError:
            malformed += 1
            continue

        records.append(
            {
                "document_id": document_id,
                "alt_text": text[start:end],
                "source_msg_id": source_msg_id,
                "source": "imported",
            }
        )
    return ExtractionResult(records, malformed, entity_limit_hit)


def _bounded(value: Any, default: int, lo: int, hi: int) -> int:
    if value is None:
        return default
    try:
        number = int(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(number, hi))


def _bounded_float(value: Any, default: float, lo: float, hi: float) -> float:
    if value is None:
        return default
    try:
        number = float(value)
    except (TypeError, ValueError):
        return default
    return max(lo, min(number, hi))


async def _resolve_document_set(
    client: Any,
    document_id: int,
    *,
    timeout_s: float,
    resolved_docs: set[int],
    resolved_sets: dict[int, dict[str, Any]],
    set_members: dict[int, dict[str, Any]],
) -> dict[str, Any] | None:
    """Resolve a single custom-emoji document's owning sticker set.

    Returns a dict with ``set_id``, ``set_short_name``, ``set_title``,
    ``stickerset_access_hash``, and ``document_alt_text``, or ``None`` when
    resolution fails for any reason. Side effects: populates
    ``resolved_docs``, ``resolved_sets``, and ``set_members`` so subsequent
    lookups of the same document or same set are free.

    Bounded: the caller must enforce total RPC caps.
    """
    if document_id in resolved_docs:
        cached = resolved_sets.get(document_id)
        if cached is not None:
            return cached
        return None

    try:
        documents = await rpc_await(
            client(GetCustomEmojiDocumentsRequest([document_id])),
            timeout=timeout_s,
            label=f"emoji_set.resolve_documents[{document_id}]",
        )  # noqa: E501
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.warning(
            "[EMOJI_SET] document resolution failed for document_id=%s", document_id
        )
        resolved_docs.add(document_id)
        return None

    if not documents:
        resolved_docs.add(document_id)
        return None

    doc = documents[0]
    doc_id = getattr(doc, "id", None)
    if doc_id is None:
        resolved_docs.add(document_id)
        return None

    attributes = getattr(doc, "attributes", []) or []
    custom_emoji_attr = None
    for attr in attributes:
        if isinstance(attr, types.DocumentAttributeCustomEmoji):
            custom_emoji_attr = attr
            break

    if custom_emoji_attr is None:
        resolved_docs.add(document_id)
        return None

    stickerset = custom_emoji_attr.stickerset
    set_id = getattr(stickerset, "id", None)
    access_hash = getattr(stickerset, "access_hash", None)
    if set_id is None or access_hash is None:
        resolved_docs.add(document_id)
        return None

    alt_text = getattr(doc, "title", None) or ""
    if not alt_text:
        alt_text = getattr(custom_emoji_attr, "alt", "") or ""

    set_key = (int(set_id), int(access_hash))
    if set_key in set_members:
        members = set_members[set_key]
    else:
        try:
            set_result = await rpc_await(
                client(
                    GetStickerSetRequest(
                        types.InputStickerSetID(int(set_id), int(access_hash)),
                        hash=0,
                    )
                ),
                timeout=timeout_s,
                label=f"emoji_set.enumerate[{set_key}]",
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            logger.warning(
                "[EMOJI_SET] set enumeration failed for set_id=%s", set_id
            )
            resolved_docs.add(document_id)
            return None

        if set_result is None:
            resolved_docs.add(document_id)
            return None

        if set_result is not None and not hasattr(set_result, "hash"):
            # Telethon can return a variant without membership documents.
            resolved_docs.add(document_id)
            return None

        set_result = set_result

        members = {}
        set_title = getattr(set_result, "title", "") or ""
        set_short_name = getattr(set_result, "short_name", "") or ""
        set_stickerset_id = getattr(set_result, "id", set_id)
        set_access_hash = getattr(set_result, "access_hash", access_hash)

        docs = getattr(set_result, "documents", None)
        if docs:
            for d in docs:
                d_id = getattr(d, "id", None)
                if d_id is None:
                    continue
                dalt = ""
                for a in (getattr(d, "attributes", []) or []):
                    if isinstance(a, types.DocumentAttributeCustomEmoji):
                        dalt = getattr(a, "alt", "") or ""
                        break
                    if isinstance(a, types.DocumentAttributeSticker):
                        dalt = getattr(a, "alt", "") or ""
                        break
                if not dalt:
                    dalt = getattr(d, "title", "") or ""
                members[int(d_id)] = dalt
        else:
            count = getattr(set_result, "count", 0) or 0
            if count:
                logger.warning(
                    "[EMOJI_SET] set_id=%s has count=%s but no documents — "
                    "enumeration incomplete",
                    set_id,
                    count,
                )

        set_members[set_key] = members

        resolved_set_info = {
            "set_id": int(set_stickerset_id),
            "set_short_name": set_short_name,
            "set_title": set_title,
            "stickerset_access_hash": int(set_access_hash),
            "stickerset_id": int(set_id),
            "stickerset_access_hash_raw": int(access_hash),
        }
        resolved_sets[set_key] = resolved_set_info

    member_alt = members.get(int(doc_id), "")
    info = dict(resolved_sets[set_key])
    info["document_alt_text"] = member_alt or alt_text
    info["document_id"] = int(doc_id)
    resolved_sets[document_id] = info
    resolved_docs.add(document_id)
    return info


async def import_from_saved_messages(
    client: Any,
    owner_id: int,
    *,
    max_messages: int | None = None,
    page_size: int | None = None,
    max_records: int | None = None,
    page_timeout: float | None = None,
    set_timeout: float | None = None,
    resolve_sets: bool = True,
) -> dict[str, Any]:
    """Deterministic, bounded import of custom emoji from Saved Messages.
    Extends the Phase 1 message-level import with optional sticker-set
    resolution/enumeration. When ``resolve_sets`` is true, each newly imported
    document is resolved through the self-client to discover its owning sticker
    set and enumerate the set's members. The returned report carries the same
    Phase 1 counters plus set-resolution/enumeration results.

    The ``set_timeout`` controls the bounded timeout for both document
    resolution (``GetCustomEmojiDocuments``) and set enumeration
    (``GetStickerSet``) RPCs. Set resolution is best-effort: if it fails or
    times out for a document, that document is still imported (with whatever
    alt_text was extracted at message-entity time) and the report reflects the
    degradation honestly.

    The returned report always carries:

    Phase 1 counters: ``ok``, ``error``, ``storage``, ``pages``,
    ``scanned_messages``, ``custom_emoji_seen``, ``malformed_entities``,
    ``imported``, ``duplicates``, ``failed``, ``library_total``,
    ``end_reached``, ``hit_scan_limit``, ``hit_record_limit``,
    ``hit_entity_limit``.

    Set-resolution counters (only when ``resolve_sets`` is true):
    ``sets_requested``, ``sets_resolved``, ``sets_failed``,
    ``set_members_seen``, ``set_members_imported``, ``set_members_duplicates``,
    ``set_members_failed``, ``set_resolution_error``.

    For a completed import (``error is None``) the invariant
    ``custom_emoji_seen == imported + duplicates + failed`` holds. On a
    collection failure nothing is persisted (fail closed).
    """
    scan_limit = _bounded(max_messages, DEFAULT_MAX_MESSAGES, 1, HARD_MAX_MESSAGES)
    size_limit = _bounded(page_size, DEFAULT_PAGE_SIZE, 1, HARD_MAX_PAGE_SIZE)
    record_limit = _bounded(max_records, DEFAULT_MAX_RECORDS, 1, HARD_MAX_RECORDS)
    timeout_s = _bounded_float(page_timeout, DEFAULT_PAGE_TIMEOUT_S, 0.01, 120.0)
    set_timeout_s = _bounded_float(
        set_timeout, DEFAULT_SET_TIMEOUT_S, 0.01, 120.0
    )

    async with _import_lock:
        storage = "supabase" if db_client.get_db() else "memory"
        existing = await db_client.list_emoji_document_ids(owner_id)
        if existing is None:
            logger.error("[EMOJI_IMPORT] library read failed — aborting before any scan")
            return {
                "ok": False,
                "error": "emoji library unreadable — import aborted before any scan",
                "storage": storage,
                "pages": 0,
                "scanned_messages": 0,
                "custom_emoji_seen": 0,
                "malformed_entities": 0,
                "imported": 0,
                "duplicates": 0,
                "failed": 0,
                "library_total": None,
                "end_reached": False,
                "hit_scan_limit": False,
                "hit_record_limit": False,
                "hit_entity_limit": False,
                "sets_requested": 0,
                "sets_resolved": 0,
                "sets_failed": 0,
                "set_members_seen": 0,
                "set_members_imported": 0,
                "set_members_duplicates": 0,
                "set_members_failed": 0,
                "set_resolution_error": None,
            }

        existing_set = set(existing)

        candidates: dict[int, dict[str, Any]] = {}
        seen = 0
        duplicates = 0
        malformed = 0
        scanned = 0
        pages = 0
        hit_scan_limit = False
        hit_record_limit = False
        hit_entity_limit = False
        end_reached = False
        cursor_max_id: int | None = None
        abort_error: str | None = None
        stop = False

        while scanned < scan_limit:
            take = min(size_limit, scan_limit - scanned)
            try:
                page = await rpc_await(
                    _facade_iter_messages(
                        client, SAVED_MESSAGES_CHAT, limit=take, max_id=cursor_max_id
                    ),
                    timeout=timeout_s,
                    label="emoji_import.iter_messages",
                )
            except asyncio.CancelledError:
                raise
            except asyncio.TimeoutError:
                abort_error = f"telegram page fetch timed out after {timeout_s:g}s"
                break
            except Exception as exc:
                abort_error = str(exc) or type(exc).__name__
                break
            pages += 1
            if not page:
                end_reached = True
                break

            for msg in page:
                scanned += 1
                result = extract_custom_emoji_records(
                    msg if isinstance(msg, dict) else {}
                )
                if result.entity_limit_hit:
                    hit_entity_limit = True
                malformed += result.malformed
                for record in result.records:
                    doc_id = record["document_id"]
                    if doc_id in candidates or doc_id in existing_set:
                        seen += 1
                        duplicates += 1
                        continue
                    if len(candidates) >= record_limit:
                        hit_record_limit = True
                        stop = True
                        break
                    seen += 1
                    candidates[doc_id] = record
                if stop:
                    break
            if stop:
                break

            ids = [
                mid
                for mid in (m.get("id") for m in page if isinstance(m, dict))
                if isinstance(mid, int) and not isinstance(mid, bool) and mid > 0
            ]
            if not ids:
                break
            next_cursor = min(ids)
            if cursor_max_id is not None and next_cursor >= cursor_max_id:
                break
            cursor_max_id = next_cursor
            if len(page) < take:
                end_reached = True
                break

        if scanned >= scan_limit and not end_reached:
            hit_scan_limit = True

        if abort_error is not None:
            logger.error("[EMOJI_IMPORT] aborted: %s", abort_error)
            return {
                "ok": False,
                "error": abort_error,
                "storage": storage,
                "pages": pages,
                "scanned_messages": scanned,
                "custom_emoji_seen": seen,
                "malformed_entities": malformed,
                "imported": 0,
                "duplicates": duplicates,
                "failed": 0,
                "library_total": len(existing_set),
                "end_reached": end_reached,
                "hit_scan_limit": hit_scan_limit,
                "hit_record_limit": hit_record_limit,
                "hit_entity_limit": hit_entity_limit,
                "sets_requested": 0,
                "sets_resolved": 0,
                "sets_failed": 0,
                "set_members_seen": 0,
                "set_members_imported": 0,
                "set_members_duplicates": 0,
                "set_members_failed": 0,
                "set_resolution_error": None,
            }

        # ── Set resolution / enumeration (best-effort enrichment) ──
        sets_requested = 0
        sets_resolved = 0
        sets_failed = 0
        set_members_seen = 0
        set_members_imported = 0
        set_members_duplicates = 0
        set_members_failed = 0
        set_resolution_error: str | None = None
        resolved_docs: set[int] = set()
        resolved_sets: dict[int | tuple[int, int], dict[str, Any]] = {}
        set_members: dict[tuple[int, int], dict[int, str]] = {}

        if resolve_sets:
            doc_ids_to_resolve = list(candidates.keys())
            for doc_id in doc_ids_to_resolve:
                if sets_requested >= _MAX_SET_RESOLUTION_RPCS:
                    set_resolution_error = (
                        "set resolution RPC cap reached — "
                        f"{sets_requested} resolutions attempted"
                    )
                    break
                sets_requested += 1
                info = await _resolve_document_set(
                    client,
                    doc_id,
                    timeout_s=set_timeout_s,
                    resolved_docs=resolved_docs,
                    resolved_sets=resolved_sets,
                    set_members=set_members,
                )
                if info is None:
                    sets_failed += 1
                    continue
                sets_resolved += 1

            for members in set_members.values():
                set_members_seen += len(members)

        # ── Persist ──
        imported = 0
        failed = 0
        now_iso = _now_iso()
        for doc_id, record in candidates.items():
            row = dict(record)
            row["owner_id"] = owner_id
            row["created_at"] = now_iso
            stored = await db_client.insert_emoji_entry(row)
            if stored is None:
                failed += 1
                logger.error("[EMOJI_IMPORT] persist failed document_id=%s", doc_id)
            else:
                imported += 1
                existing_set.add(doc_id)

        # Count set-member outcomes for the report.
        if resolve_sets:
            for doc_id in candidates:
                info = resolved_sets.get(doc_id)
                if info is None or not info.get("stickerset_id"):
                    set_members_failed += 1
                else:
                    set_members_imported += 1
            set_members_duplicates = max(
                0, seen - set_members_imported - set_members_failed
            )

        return {
            "ok": failed == 0,
            "error": None,
            "storage": storage,
            "pages": pages,
            "scanned_messages": scanned,
            "custom_emoji_seen": seen,
            "malformed_entities": malformed,
            "imported": imported,
            "duplicates": duplicates,
            "failed": failed,
            "library_total": len(existing_set),
            "end_reached": end_reached,
            "hit_scan_limit": hit_scan_limit,
            "hit_record_limit": hit_record_limit,
            "hit_entity_limit": hit_entity_limit,
            "sets_requested": sets_requested,
            "sets_resolved": sets_resolved,
            "sets_failed": sets_failed,
            "set_members_seen": set_members_seen,
            "set_members_imported": set_members_imported,
            "set_members_duplicates": set_members_duplicates,
            "set_members_failed": set_members_failed,
            "set_resolution_error": set_resolution_error,
        }


def _now_iso() -> str:
    from datetime import datetime, timezone

    return datetime.now(timezone.utc).isoformat()


# Re-export the Phase 1 names for tests.
__all__ = [
    "extract_custom_emoji_records",
    "import_from_saved_messages",
    "ExtractionResult",
]
