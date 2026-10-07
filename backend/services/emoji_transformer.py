"""
Emoji transformer — Phase 4 (Emoji & Reaction, ROADMAP §15/§16/§20–§22).

The deterministic, Telegram-free core of the replacement pipeline: given a
message's text, its serialized Telegram entity dicts (the ``serialize_message``
representation — UTF-16 offsets/lengths, ROADMAP §22) and a mapping table
resolved from the effective category, it rewrites ONLY the mapped spans and
recomputes every entity offset in UTF-16 code units.

Contracts:

* Structural, never linguistic: a span is eligible because the owner's
  mapping table names it EXACTLY. There is no regex, no keyword routing, no
  natural-language intent, no chat history, no AI, and no Telegram call —
  this module imports none of them (it is pure).
* Emoji-only invariant (§16): text outside the replaced spans is copied
  unchanged; unmapped emoji and ordinary text are untouched (§21).
* Multi-emoji (§20): every mapped span in the message is replaced in one
  pass; the same key maps consistently. Spans are disjoint by construction
  (the longest key wins at each position, then the scan continues after it).
* Entity-safe (§22): entity spans are rebased to the new text. An entity
  that fully covers a replaced span keeps covering it (a premium emoji may
  stay inside bold/italic/...); an entity that only PARTIALLY overlaps a
  replaced span — or an entity whose type cannot be rebuilt / whose offsets
  do not resolve — is an unsafe reconstruction and fails the WHOLE
  transformation closed, so the caller leaves the original message untouched.
* Custom-emoji entities already present in the message are never transformed
  and never corrupted: a mapping match that intersects an existing
  custom-emoji span is skipped, so such an entity can only be rebased.
* Bounded: more matched spans than ``MAX_REPLACEMENTS`` fails closed instead
  of rewriting an unbounded message.

Fail-closed contract: this function never mutates the caller's text or
entity dicts and never fabricates a result. When ``error`` is set the caller
must ignore ``text``/``entities`` and leave the original message alone.

No second client, loop, scheduler, or executor: the function is pure.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.telegram_api._helpers import (
    SUPPORTED_ENTITY_TYPES,
    utf16_index_at,
    utf16_offset,
)

logger = logging.getLogger(__name__)

MAX_REPLACEMENTS = 200

E_INVALID_INPUT = "invalid_input"
E_UNSUPPORTED_ENTITY = "unsupported_entity"
E_MALFORMED_ENTITIES = "malformed_entities"
E_UNSAFE_ENTITY = "unsafe_entity_overlap"
E_TOO_MANY_REPLACEMENTS = "too_many_replacements"


def _valid_document_id(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool) and value > 0


def usable_mappings(mappings: Any) -> dict[str, tuple[int, str]]:
    """Filter a mapping table down to entries that can be applied.

    Returns ``{simple_text: (document_id, alt_text)}``. An entry is dropped
    when the key is not a non-empty string, the document id is not a
    positive int, or the library entry has no non-empty alt text: an
    unresolvable mapping leaves its emoji untouched instead of fabricating a
    replacement (fail closed). Deterministic — input order is preserved.
    """
    table: dict[str, tuple[int, str]] = {}
    if not isinstance(mappings, dict):
        return table
    for key, entry in mappings.items():
        if not isinstance(key, str) or not key:
            continue
        if not isinstance(entry, dict):
            continue
        alt = entry.get("alt_text")
        document_id = entry.get("document_id")
        if not isinstance(alt, str) or not alt:
            continue
        if not _valid_document_id(document_id):
            continue
        table[key] = (document_id, alt)
    return table


def _entity_char_range(entity: dict[str, Any], text: str) -> tuple[int, int] | None:
    """Text-character range of an entity, or None when its offsets do not
    resolve (out of range / mid-surrogate) — a corrupt entity, never clamped."""
    offset = entity.get("offset")
    length = entity.get("length")
    if not isinstance(offset, int) or isinstance(offset, bool) or offset < 0:
        return None
    if not isinstance(length, int) or isinstance(length, bool) or length < 0:
        return None
    try:
        return utf16_index_at(text, offset), utf16_index_at(text, offset + length)
    except ValueError:
        return None


def _find_spans(
    text: str,
    table: dict[str, tuple[int, str]],
    custom_spans: list[tuple[int, int]],
) -> list[tuple[int, int, str]]:
    """Non-overlapping mapped spans, left to right, longest key first.

    A match that intersects an existing custom-emoji span is skipped (that
    emoji is already premium — it must not be rewritten), and the scan
    advances one character so a shorter key can still match later.
    """
    by_first: dict[str, list[str]] = {}
    for key in table:
        by_first.setdefault(key[0], []).append(key)
    for keys in by_first.values():
        keys.sort(key=len, reverse=True)

    spans: list[tuple[int, int, str]] = []
    i = 0
    end_of_text = len(text)
    while i < end_of_text:
        match: tuple[int, int, str] | None = None
        for key in by_first.get(text[i], ()):
            if text.startswith(key, i):
                stop = i + len(key)
                if not any(start < stop and i < end for start, end in custom_spans):
                    match = (i, stop, key)
                break
        if match is None:
            i += 1
        else:
            spans.append(match)
            i = match[1]
    return spans


def transform_message(
    text: str,
    entities: list[dict[str, Any]] | None,
    mappings: Any,
) -> dict[str, Any]:
    """Transform every mapped span of one message.

    ``entities`` is the serialized dict representation (``serialize_message``,
    UTF-16 offsets). ``mappings`` is the resolved effective-category mapping
    table (``{simple_text: {"document_id": int, "alt_text": str}}``) — this
    function never decides the category itself.

    Returns ``{ok, changed, text, entities, error}``. ``ok`` is False and
    ``error`` is set whenever the message cannot be reconstructed safely;
    the caller must then leave the original message untouched.
    """
    result: dict[str, Any] = {
        "ok": False, "changed": 0, "text": text, "entities": [], "error": None,
    }
    if not isinstance(text, str) or not text:
        result["error"] = E_INVALID_INPUT
        return result

    raw_entities = entities if isinstance(entities, list) else []
    validated: list[tuple[dict[str, Any], int, int]] = []
    for entity in raw_entities:
        if not isinstance(entity, dict):
            result["error"] = E_MALFORMED_ENTITIES
            return result
        if entity.get("type") not in SUPPORTED_ENTITY_TYPES:
            result["error"] = E_UNSUPPORTED_ENTITY
            return result
        char_range = _entity_char_range(entity, text)
        if char_range is None:
            result["error"] = E_MALFORMED_ENTITIES
            return result
        validated.append((entity, char_range[0], char_range[1]))

    table = usable_mappings(mappings)
    if not table:
        result["ok"] = True
        result["entities"] = [dict(entity) for entity in raw_entities]
        return result

    custom_spans = [
        (start, end)
        for entity, start, end in validated
        if entity.get("type") == "MessageEntityCustomEmoji" and end > start
    ]
    spans = _find_spans(text, table, custom_spans)
    if len(spans) > MAX_REPLACEMENTS:
        result["error"] = E_TOO_MANY_REPLACEMENTS
        return result
    if not spans:
        result["ok"] = True
        result["entities"] = [dict(entity) for entity in raw_entities]
        return result

    placed: list[tuple[int, int, int, int, str, int]] = []
    pieces: list[str] = []
    cursor = 0
    delta = 0
    for start, stop, key in spans:
        document_id, alt = table[key]
        pieces.append(text[cursor:start])
        new_start = start + delta
        pieces.append(alt)
        new_stop = new_start + len(alt)
        placed.append((start, stop, new_start, new_stop, key, document_id))
        delta += len(alt) - (stop - start)
        cursor = stop
    pieces.append(text[cursor:])
    new_text = "".join(pieces)

    def _shift(index: int) -> int:
        """Character index in the new text for a character index in the old
        text (only called for positions outside every replaced span)."""
        shifted = index
        for start, stop, _new_start, _new_stop, key, _doc in placed:
            if stop <= index:
                shifted += len(table[key][1]) - (stop - start)
        return shifted

    new_entities: list[dict[str, Any]] = []
    for entity, start, stop in validated:
        overlapping = [p for p in placed if p[0] < stop and start < p[1]]
        if overlapping:
            covered = (
                start <= min(p[0] for p in overlapping)
                and stop >= max(p[1] for p in overlapping)
            )
            if not covered:
                result["error"] = E_UNSAFE_ENTITY
                return result
        rebuilt = dict(entity)
        try:
            rebuilt["offset"] = utf16_offset(new_text, _shift(start))
            rebuilt["length"] = utf16_offset(new_text, _shift(stop)) - rebuilt["offset"]
        except ValueError:
            result["error"] = E_MALFORMED_ENTITIES
            return result
        new_entities.append(rebuilt)

    for _start, _stop, new_start, new_stop, _key, document_id in placed:
        offset = utf16_offset(new_text, new_start)
        new_entities.append({
            "type": "MessageEntityCustomEmoji",
            "offset": offset,
            "length": utf16_offset(new_text, new_stop) - offset,
            "document_id": document_id,
        })

    result["ok"] = True
    result["changed"] = len(placed)
    result["text"] = new_text
    result["entities"] = new_entities
    return result
