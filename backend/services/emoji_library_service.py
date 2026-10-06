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
* Set enrichment (Phase 1 remainder, ROADMAP §9) is explicitly BEST-EFFORT
  and bounded: each newly collected ``document_id`` is resolved once through
  the existing self client (``messages.GetCustomEmojiDocumentsRequest``,
  chunked), the real Telegram-provided sticker-set identity from the
  document's ``DocumentAttributeCustomEmoji`` is used to enumerate each
  unique set exactly once (``messages.GetStickerSetRequest``), and the set's
  member documents (``document_id`` + the attribute's Unicode ``alt``) are
  added to the same library through the same deduplication — so importing
  one emoji from a collection imports the collection. Nothing is ever
  fabricated: an unresolvable document, a document without a set identity,
  or a failed/rejected set enumeration is counted and reported (``degraded``
  + ``set_error`` + the ``set_*`` counters) while the message-level records
  that DID resolve still persist; a set failure never aborts the import and
  never invents metadata. Set-enumerated members carry no message origin:
  their ``source_msg_id`` is ``None`` and their alt text is Telegram's own.

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
from backend.telegram_api.custom_emoji import (
    get_custom_emoji_documents as _facade_get_custom_emoji_documents,
    get_sticker_set as _facade_get_sticker_set,
)
from backend.telegram_api.messages import iter_messages as _facade_iter_messages

logger = logging.getLogger(__name__)

CUSTOM_EMOJI_ENTITY = "MessageEntityCustomEmoji"
SAVED_MESSAGES_CHAT = "me"

DEFAULT_MAX_MESSAGES = 200
DEFAULT_PAGE_SIZE = 50
DEFAULT_MAX_RECORDS = 500
DEFAULT_MAX_SET_RECORDS = 500
DEFAULT_PAGE_TIMEOUT_S = 15.0

HARD_MAX_MESSAGES = 2000
HARD_MAX_PAGE_SIZE = 200
HARD_MAX_RECORDS = 5000
HARD_MAX_SET_RECORDS = 2000
MAX_ENTITIES_PER_MESSAGE = 100

# Set-enrichment bounds (ROADMAP §9): every stage is explicitly capped and
# deterministic — unique documents are resolved once (chunked RPCs), each
# unique set is enumerated exactly once, and set members are accepted up to
# explicit budgets. Nothing here paginates Telegram without a bound.
MAX_DOC_RESOLVE_BATCH = 50    # document ids per resolution RPC
MAX_DOC_RESOLVE_CALLS = 10    # ⇒ at most 500 documents resolved per import
MAX_SETS_PER_IMPORT = 20      # unique sets enumerated per import
MAX_SET_MEMBERS_PER_SET = 200 # members processed per set

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


def _set_identity_key(set_ref: Any) -> tuple[Any, ...] | None:
    """A hashable identity for ONE Telegram-provided set reference.

    Two documents belong to the same enumerated set iff their identity keys
    are equal — so a set encountered through several imported emojis is
    resolved (and enumerated) exactly once. Anything Telegram did not
    provide as a real id/short-name identity yields ``None``.
    """
    if not isinstance(set_ref, dict):
        return None
    kind = set_ref.get("kind")
    if kind == "id":
        set_id = set_ref.get("id")
        access_hash = set_ref.get("access_hash")
        if (
            isinstance(set_id, int)
            and not isinstance(set_id, bool)
            and isinstance(access_hash, int)
            and not isinstance(access_hash, bool)
        ):
            return ("id", set_id, access_hash)
        return None
    if kind == "short_name":
        short_name = set_ref.get("short_name")
        if isinstance(short_name, str) and short_name.strip():
            return ("short_name", short_name.strip())
        return None
    return None


async def _enumerate_sets(
    client: Any,
    candidates: dict[int, dict[str, Any]],
    existing: set[int],
    timeout_s: float,
    set_record_limit: int,
) -> dict[str, Any]:
    """Resolve the collected documents and enumerate their real sets.

    BEST-EFFORT by contract: every failure here is counted and reported, and
    the caller still persists the message-level records that the scan
    collected. Nothing is fabricated — a document Telegram did not return,
    a document without a custom-emoji attribute, and a document without a
    usable set identity all count as "set unknown" rather than becoming
    invented metadata.

    Stages, each explicitly bounded and deterministic:

    1. Resolve candidate document ids in ``MAX_DOC_RESOLVE_BATCH``-sized
       chunks, at most ``MAX_DOC_RESOLVE_CALLS`` chunks per import, against
       the same bounded timeout as the scan.
    2. Group the resolved documents by their Telegram-provided set identity
       (first-appearance order of the candidates) and enumerate at most
       ``MAX_SETS_PER_IMPORT`` unique sets, one RPC each.
    3. Classify each enumerated member once: duplicates (already in the
       scan candidates, the durable library, or seen as another member)
       are counted, new members become library records up to
       ``set_record_limit`` and ``MAX_SET_MEMBERS_PER_SET`` per set.
    """
    result: dict[str, Any] = {
        "error": None,
        "documents_resolved": 0,
        "unresolved_documents": 0,
        "documents_without_set": 0,
        "sets_resolved": 0,
        "set_members_seen": 0,
        "set_members_malformed": 0,
        "set_duplicates": 0,
        "hit_set_limit": False,
        "hit_set_member_limit": False,
        "set_candidates": {},
    }
    if not candidates:
        return result

    # 1. Resolve each unique candidate document once (chunked, bounded).
    doc_ids = list(candidates)[: MAX_DOC_RESOLVE_BATCH * MAX_DOC_RESOLVE_CALLS]
    resolved: dict[int, dict[str, Any]] = {}
    for start in range(0, len(doc_ids), MAX_DOC_RESOLVE_BATCH):
        chunk = doc_ids[start:start + MAX_DOC_RESOLVE_BATCH]
        try:
            documents = await rpc_await(
                _facade_get_custom_emoji_documents(client, chunk),
                timeout=timeout_s,
                label="emoji_import.get_custom_emoji_documents",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result["error"] = (
                f"custom emoji document resolution failed: {str(exc) or type(exc).__name__}"
            )
            break
        for entry in documents or []:
            if isinstance(entry, dict) and isinstance(entry.get("document_id"), int):
                resolved[entry["document_id"]] = entry

    # 2. Group by the real Telegram set identity, first-appearance order.
    set_refs: list[tuple[tuple[Any, ...], dict[str, Any]]] = []
    seen_identities: set[tuple[Any, ...]] = set()
    for doc_id in doc_ids:
        entry = resolved.get(doc_id)
        if entry is None:
            result["unresolved_documents"] += 1
            continue
        result["documents_resolved"] += 1
        identity = _set_identity_key(entry.get("set"))
        if identity is None:
            result["documents_without_set"] += 1
            continue
        if identity in seen_identities:
            continue
        if len(set_refs) >= MAX_SETS_PER_IMPORT:
            result["hit_set_limit"] = True
            break
        seen_identities.add(identity)
        set_refs.append((identity, entry["set"]))

    # 3. Enumerate each unique set exactly once and classify its members.
    set_candidates: dict[int, dict[str, Any]] = result["set_candidates"]
    for _, set_ref in set_refs:
        try:
            set_info = await rpc_await(
                _facade_get_sticker_set(client, set_ref),
                timeout=timeout_s,
                label="emoji_import.get_sticker_set",
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            result["error"] = (
                f"sticker set enumeration failed: {str(exc) or type(exc).__name__}"
            )
            break
        if not isinstance(set_info, dict):
            result["error"] = "sticker set enumeration returned no set information"
            break
        result["sets_resolved"] += 1
        processed = 0
        for member in set_info.get("members") or []:
            if processed >= MAX_SET_MEMBERS_PER_SET:
                result["hit_set_member_limit"] = True
                break
            processed += 1
            if not isinstance(member, dict):
                result["set_members_malformed"] += 1
                continue
            member_id = member.get("document_id")
            if (
                not isinstance(member_id, int)
                or isinstance(member_id, bool)
                or member_id <= 0
            ):
                result["set_members_malformed"] += 1
                continue
            alt = member.get("alt")
            result["set_members_seen"] += 1
            if member_id in set_candidates or member_id in candidates or member_id in existing:
                result["set_duplicates"] += 1
                continue
            if len(set_candidates) >= set_record_limit:
                result["hit_set_member_limit"] = True
                break
            set_candidates[member_id] = {
                "document_id": member_id,
                "alt_text": alt if isinstance(alt, str) else "",
                "source_msg_id": None,
                "source": "imported",
            }
        if len(set_candidates) >= set_record_limit:
            result["hit_set_member_limit"] = True
            break

    return result


def _empty_set_report() -> dict[str, Any]:
    """The zero-value set-enrichment section of the import report."""
    return {
        "degraded": False,
        "set_error": None,
        "documents_resolved": 0,
        "unresolved_documents": 0,
        "documents_without_set": 0,
        "sets_resolved": 0,
        "set_members_seen": 0,
        "set_members_malformed": 0,
        "set_imported": 0,
        "set_duplicates": 0,
        "set_failed": 0,
        "hit_set_limit": False,
        "hit_set_member_limit": False,
    }


async def import_from_saved_messages(
    client: Any,
    owner_id: int,
    *,
    max_messages: int | None = None,
    page_size: int | None = None,
    max_records: int | None = None,
    max_set_records: int | None = None,
    page_timeout: float | None = None,
) -> dict[str, Any]:
    """Deterministic, bounded import of custom emoji from Saved Messages.

    Scans the owner's Saved Messages newest-first through the passed self
    client, extracts validated custom-emoji records (deduplicated within the
    scan and against the durable library), resolves the collected documents'
    real sticker sets (best-effort, bounded — see the module docstring), then
    persists the new records. The returned report is always honest:

    ``ok``             True iff the message-level import did not fail
                       (``error is None``, ``failed == 0``, ``set_failed
                       == 0``). Set-enumeration degradation is reported
                       separately through ``degraded``/``set_error`` and
                       never disguises itself as full success.
    ``error``          collection/abortion failure, or None.
    ``storage``        ``"supabase"`` or ``"memory"`` (configured backend).
    ``pages``          successful page fetches.
    ``scanned_messages`` messages actually examined.
    ``custom_emoji_seen`` valid custom-emoji entity occurrences encountered.
    ``malformed_entities`` invalid custom-emoji entities skipped.
    ``imported``       new library rows persisted from the message scan.
    ``duplicates``     valid occurrences not imported because the document id
                       was already seen in this scan or already in the library.
    ``failed``         persistence attempts for scan records that failed.
    ``library_total``  library size for the owner after the run, or None when
                       the library could not be read.
    ``end_reached``    the scan confirmed the end of Saved Messages.
    ``hit_scan_limit`` stopped because the scan budget ran out (end not
                       confirmed).
    ``hit_record_limit`` stopped because the new-record budget ran out.
    ``hit_entity_limit`` at least one message carried more entities than the
                       per-message processing bound.
    ``degraded``       the set enrichment could not complete fully while the
                       message-level import itself succeeded.
    ``set_error``      the first document/set resolution or enumeration
                       failure, or None.
    ``documents_resolved`` candidate documents Telegram resolved with a
                       custom-emoji attribute.
    ``unresolved_documents`` candidate documents Telegram did not resolve (or
                       that fell outside the resolution bound).
    ``documents_without_set`` resolved documents without a usable Telegram
                       set identity.
    ``sets_resolved``  unique sets successfully enumerated.
    ``set_members_seen`` valid set members classified.
    ``set_members_malformed`` enumerated members that were not usable custom
                       emoji documents.
    ``set_imported``   new library rows persisted from set enumeration.
    ``set_duplicates`` set members already covered by the scan candidates or
                       the durable library.
    ``set_failed``     persistence attempts for set members that failed.
    ``hit_set_limit``  more unique sets were detected than the enumeration
                       bound allows.
    ``hit_set_member_limit`` a set's member bound or the set record budget
                       stopped the enumeration stage.

    For a fully successful import (``error is None``, ``set_error is None``,
    no limit flags) the invariant ``custom_emoji_seen + set_members_seen ==
    imported + duplicates + failed + set_imported + set_duplicates +
    set_failed`` holds. On a collection failure nothing is persisted (fail
    closed) and the observed counters describe the partial scan only.
    """
    scan_limit = _bounded(max_messages, DEFAULT_MAX_MESSAGES, 1, HARD_MAX_MESSAGES)
    size_limit = _bounded(page_size, DEFAULT_PAGE_SIZE, 1, HARD_MAX_PAGE_SIZE)
    record_limit = _bounded(max_records, DEFAULT_MAX_RECORDS, 1, HARD_MAX_RECORDS)
    set_record_limit = _bounded(
        max_set_records, DEFAULT_MAX_SET_RECORDS, 0, HARD_MAX_SET_RECORDS
    )
    timeout_s = _bounded_float(page_timeout, DEFAULT_PAGE_TIMEOUT_S, 0.01, 120.0)

    async with _import_lock:
        storage = "supabase" if db_client.get_db() else "memory"
        set_report = _empty_set_report()
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
                **set_report,
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
                **set_report,
            }

        # Set enrichment (best-effort): runs after the scan, before any
        # persistence — the whole run stays collect-then-persist, and a set
        # failure is reported (``degraded``/``set_error``) without aborting
        # the message-level records the scan collected.
        enrichment = await _enumerate_sets(
            client, candidates, existing_set, timeout_s, set_record_limit
        )
        set_candidates: dict[int, dict[str, Any]] = enrichment["set_candidates"]

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

        set_imported = 0
        set_failed = 0
        for doc_id, record in set_candidates.items():
            row = dict(record)
            row["owner_id"] = owner_id
            row["created_at"] = now_iso
            stored = await db_client.insert_emoji_entry(row)
            if stored is None:
                set_failed += 1
                logger.error(
                    "[EMOJI_IMPORT] set member persist failed document_id=%s", doc_id
                )
            else:
                set_imported += 1
                existing_set.add(doc_id)

        set_report = {
            "degraded": bool(
                enrichment["error"]
                or enrichment["unresolved_documents"]
                or enrichment["documents_without_set"]
                or enrichment["hit_set_limit"]
                or enrichment["hit_set_member_limit"]
            ),
            "set_error": enrichment["error"],
            "documents_resolved": enrichment["documents_resolved"],
            "unresolved_documents": enrichment["unresolved_documents"],
            "documents_without_set": enrichment["documents_without_set"],
            "sets_resolved": enrichment["sets_resolved"],
            "set_members_seen": enrichment["set_members_seen"],
            "set_members_malformed": enrichment["set_members_malformed"],
            "set_imported": set_imported,
            "set_duplicates": enrichment["set_duplicates"],
            "set_failed": set_failed,
            "hit_set_limit": enrichment["hit_set_limit"],
            "hit_set_member_limit": enrichment["hit_set_member_limit"],
        }

        return {
            "ok": failed == 0 and set_failed == 0,
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
            **set_report,
        }
