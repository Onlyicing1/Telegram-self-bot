"""
Emoji Library service — Phase 1 (Library & Import) of Emoji & Reaction.

Collects custom-emoji records from the owner's Saved Messages through the
EXISTING self-client message boundary (``backend.telegram_api.messages``),
validates them against the Phase 0 entity representation (``serialize_message``
dicts), and persists them idempotently through the existing
``backend/db/client.py`` Supabase-or-in-memory-fallback pattern.

Contracts (ROADMAP §7/§8/§9):

* Entity-based recognition only: a record exists iff the message carries a
  ``MessageEntityCustomEmoji`` entity with a valid positive ``document_id``
  and a span that resolves inside the message text (Phase 0 ``utf16_index_at``
  — fail closed). Plain Unicode emoji carry no such entity and are never
  imported; unrelated entity types are ignored, not counted as malformed.
* One library entry per ``(owner_id, document_id)``: repeated imports and the
  same emoji appearing in several Saved Messages are counted as duplicates,
  never stored twice.
* Bounded and deterministic: explicit scan/page/record/entity bounds (caller
  values are clamped to hard caps), newest-first pagination through an
  exclusive ``max_id`` cursor that must strictly decrease, and one bounded
  fetch timeout per page (``rpc_await``).
* Fail closed: an unreadable durable library or a Telegram collection error
  aborts the import BEFORE anything is persisted, with an honest report; a
  completed scan persists every collected record and reports each insert
  outcome (``imported`` / ``failed``).

Context isolation: nothing from ``backend.ai`` is imported here — no chat
history, no quoted-message context, no conversation state, no AI
interpretation. The Saved Messages scan plus this service's explicit bounds
are the whole input.

No second client, loop, scheduler, or executor is created: the self client is
passed in by the caller, and one module-level ``asyncio.Lock`` serializes
concurrent imports so deduplication decisions stay deterministic.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any, NamedTuple

from backend.db import client as db_client
from backend.helper.rpc_timeout import rpc_await
from backend.telegram_api._helpers import utf16_index_at
from backend.telegram_api.messages import iter_messages as _facade_iter_messages

logger = logging.getLogger(__name__)

CUSTOM_EMOJI_ENTITY = "MessageEntityCustomEmoji"
SAVED_MESSAGES_CHAT = "me"

DEFAULT_MAX_MESSAGES = 200
DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_RECORDS = 500
DEFAULT_PAGE_TIMEOUT_S = 15.0

HARD_MAX_MESSAGES = 2000
HARD_MAX_PAGE_SIZE = 200
HARD_MAX_RECORDS = 5000
MAX_ENTITIES_PER_MESSAGE = 100

_import_lock = asyncio.Lock()


class ExtractionResult(NamedTuple):
    records: list[dict[str, Any]]
    malformed: int
    entity_limit_hit: bool


def extract_custom_emoji_records(message: dict[str, Any]) -> ExtractionResult:
    """Extract validated custom-emoji records from ONE serialized message.

    Returns ``({document_id, alt_text, source_msg_id, source}, malformed,
    entity_limit_hit)`` where ``alt_text`` is the exact Unicode text span the
    entity covers (UTF-16 offsets resolved through Phase 0
    ``utf16_index_at``). Invalid payloads — missing/non-integer/non-positive
    document id, bad offsets, a span outside the text — are skipped and
    counted as malformed: fail closed, never fabricate. Non-custom-emoji
    entities and plain Unicode emoji are ignored silently.
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


async def import_from_saved_messages(
    client: Any,
    owner_id: int,
    *,
    max_messages: int | None = None,
    page_size: int | None = None,
    max_records: int | None = None,
    page_timeout: float | None = None,
) -> dict[str, Any]:
    """Deterministic, bounded import of custom emoji from Saved Messages.

    Scans the owner's Saved Messages newest-first through the passed self
    client, extracts validated custom-emoji records (deduplicated within the
    scan and against the durable library), then persists the new ones. The
    returned report is always honest:

    ``ok``             True iff nothing failed (``error is None`` and
                       ``failed == 0``).
    ``error``          collection/abortion failure, or None.
    ``storage``        ``"supabase"`` or ``"memory"`` (configured backend).
    ``pages``          successful page fetches.
    ``scanned_messages`` messages actually examined.
    ``custom_emoji_seen`` valid custom-emoji entity occurrences encountered.
    ``malformed_entities`` invalid custom-emoji entities skipped.
    ``imported``       new library rows persisted.
    ``duplicates``     valid occurrences not imported because the document id
                       was already seen in this scan or already in the library.
    ``failed``         persistence attempts that failed.
    ``library_total``  library size for the owner after the run, or None when
                       the library could not be read.
    ``end_reached``    the scan confirmed the end of Saved Messages.
    ``hit_scan_limit`` stopped because the scan budget ran out (end not
                       confirmed).
    ``hit_record_limit`` stopped because the new-record budget ran out.
    ``hit_entity_limit`` at least one message carried more entities than the
                       per-message processing bound.

    For a completed import (``error is None``) the invariant
    ``custom_emoji_seen == imported + duplicates + failed`` holds. On a
    collection failure nothing is persisted (fail closed) and the observed
    counters describe the partial scan only.
    """
    scan_limit = _bounded(max_messages, DEFAULT_MAX_MESSAGES, 1, HARD_MAX_MESSAGES)
    size_limit = _bounded(page_size, DEFAULT_PAGE_SIZE, 1, HARD_MAX_PAGE_SIZE)
    record_limit = _bounded(max_records, DEFAULT_MAX_RECORDS, 1, HARD_MAX_RECORDS)
    timeout_s = _bounded_float(page_timeout, DEFAULT_PAGE_TIMEOUT_S, 0.01, 120.0)

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
            }

        imported = 0
        failed = 0
        now_iso = datetime.now(timezone.utc).isoformat()
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
        }
