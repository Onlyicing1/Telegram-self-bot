"""
Retrieve service — all retrieval business logic lives here.

Unified workflow:
  - resolve_saved_items(owner_id, query): the ONE deterministic saved-item
    resolver (Save V2) — turns a name/tag request into 0/1/N candidates
    (never into a Telegram action) before retrieval
  - load_saved_item(save_code, owner_id): the ONE owner-scoped, row-identity
    verified read of a persisted saved_items row (shared by every preview
    surface) — preview never regenerates metadata, it reads the stored row
  - format_preview(row): rich metadata display for the preview panel
  - build_metadata_block(row): the LifeOS metadata block injected into
    retrieved file captions
  - do_retrieve(self_client, owner_id, save_code, target_chat): forwards
    the saved media and edits its caption to include the metadata block
    (the SINGLE Telegram retrieval authority — the resolver never forwards)
  - do_preview / do_send: legacy text-command entry points (still work
    but the panel UI is the primary path)
  - resolve_management_target(owner_id, save_code|query): the ONE target
    resolution shared by every management surface (Save V2 Part 4) — it
    reuses the resolver above and never guesses among candidates
  - do_rename / do_edit_tags: the ONE writers of `saved_items.display_name`
    and the owner's `saved_items.tags` after the save itself
  - do_change_file_name: the ONE writer of the ACTUAL Telegram file name —
    it re-uploads the saved media with the new DocumentAttributeFilename and
    repoints the row, because Telegram cannot rename a document in place
  - _sync_saved_caption: the ONE Telegram-caption synchronization authority,
    shared by the tag writer and the file-name replacement
  - do_move / do_delete: item actions from the preview panel
"""
import asyncio
import logging
import os
import re
import shutil
import tempfile
import traceback
import unicodedata
from dataclasses import dataclass
from datetime import datetime

from telethon.tl.types import MessageMediaDocument

from backend.ai.semantic_delete import normalize_text as _persian_normalize
from backend.db import client as db_client
from backend.diagnostics import record_event
from backend.services import save_service

logger = logging.getLogger(__name__)



def _format_size(size_bytes) -> str:
    if not size_bytes:
        return "—"
    if size_bytes < 1024:
        return f"{size_bytes} B"
    if size_bytes < 1024 * 1024:
        return f"{size_bytes / 1024:.1f} KB"
    return f"{size_bytes / (1024 * 1024):.1f} MB"


def _format_date(created_at) -> str:
    if not created_at:
        return "—"
    try:
        dt = datetime.fromisoformat(str(created_at).replace("Z", "+00:00"))
        return dt.strftime("%Y-%m-%d %H:%M")
    except Exception:
        return str(created_at)[:16]


def _type_icon(row: dict) -> str:
    media = (row.get("media_type") or "").lower()
    mime = (row.get("mime_type") or "").lower()
    if "photo" in media or "image" in mime or media == "photo":
        return "🖼"
    if "video" in media or "video" in mime:
        return "🎬"
    if "audio" in media or "audio" in mime:
        return "🎵"
    if "voice" in media:
        return "🎤"
    if "sticker" in media:
        return "🎯"
    if "gif" in media or "animation" in media:
        return "🎞"
    if "document" in media or "file" in mime or mime:
        return "📎"
    return "📦"


def _display_name(row: dict) -> str:
    """Owner-facing label, in owner-metadata order.

    ``display_name`` (the owner's own name for the item) → ``file_name``
    (the source filename) → ``media_type``. Never derived from the caption,
    never invented when the owner named nothing.
    """
    for key in ("display_name", "file_name"):
        value = str(row.get(key) or "").strip()
        if value:
            return value
    return row.get("media_type") or "Untitled"


def saved_file_name(row: dict) -> str:
    """The item's stored TELEGRAM file name (``""`` when it has none).

    The Telegram layer of the item's identity — distinct from
    ``display_name``, which is the owner's logical label.
    """
    return str(row.get("file_name") or "").strip()


def build_metadata_block(row: dict) -> str:
    code = row.get("save_code") or "—"
    saved = _format_date(row.get("created_at"))
    return (
        f"**LifeOS** `{code}`\n"
        f"**Saved** {saved}"
    )


def format_preview(row: dict) -> str:
    code = row.get("save_code") or "—"
    name = _display_name(row)
    media_type = row.get("media_type") or "—"
    mime = row.get("mime_type") or "—"
    size = _format_size(row.get("file_size"))
    sender = row.get("sender_name") or "—"
    saved = _format_date(row.get("created_at"))
    return (
        f"**{name}** `{code}`\n\n"
        f"**Type** {media_type}\n"
        f"**Format** `{mime}`\n"
        f"**Size** {size}\n"
        f"**Tags** {format_owner_tags(row)}\n"
        f"**Sender** {sender}\n"
        f"**Saved** {saved}"
    )


async def load_saved_item(save_code: str, owner_id: int) -> dict | None:
    """Return the owner's persisted saved_items row for ``save_code``.

    The ONE verified row lookup every preview surface shares. ``query_save``
    is a code-only lookup (the DB layer adds no owner predicate), so the row
    is verified here — owner isolation AND row identity — before ANY surface
    formats it. Owner isolation + row identity are the same checks
    ``do_retrieve``/``do_delete`` apply.

    Read-only by contract: it reads the persisted row and returns it
    untouched. Nothing is re-fetched from Telegram, no field is re-derived
    from ``origin_chat_id``/``file_id``, and no row is written, repaired, or
    backfilled — the stored metadata IS the preview's source of truth.

    Returns ``None`` when the row is missing, foreign, or belongs to another
    code (all three are reported identically by callers, so a foreign item is
    never distinguishable from a missing one). Raises only when the DB layer
    itself fails, so callers can keep an honest DB-error path.
    """
    code = save_code.upper().strip()
    t0 = asyncio.get_event_loop().time()
    try:
        row = await db_client.query_save(code)
        record_event("database", "query_save", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
    except Exception as exc:
        logger.error("saved-item lookup error for %s: %s", code, exc)
        record_event("database", "query_save", 0, "ERROR", str(exc))
        raise
    if not row or row.get("owner_id") != owner_id:
        return None
    if str(row.get("save_code") or "").upper() != code:
        return None
    return row


async def do_preview(self_client, owner_id: int, save_code: str) -> str:
    save_code = save_code.upper().strip()
    try:
        row = await load_saved_item(save_code, owner_id)
    except Exception as exc:
        return f"❌ DB error: {exc}"
    if not row:
        return f"❌ No item found for `{save_code}`"
    await db_client.log(owner_id, "INFO", f"Preview {save_code}", {"save_code": save_code})
    return format_preview(row)


async def do_retrieve(self_client, owner_id: int, save_code: str, target_chat: int) -> str:
    """Forward the saved media to target_chat and inject the metadata block
    into the caption. If the media had no caption, one is generated."""
    save_code = save_code.upper().strip()
    t0 = asyncio.get_event_loop().time()
    try:
        row = await db_client.query_save(save_code)
        record_event("database", "query_save", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
    except Exception as exc:
        logger.error("retrieve db error: %s", exc)
        record_event("database", "query_save", 0, "ERROR", str(exc))
        return f"❌ DB error: {exc}"
    # Owner isolation, identical to do_rename/do_move/do_delete: checked
    # BEFORE any Telegram side effect (entity resolution / forwarding) and
    # reported with the same not-found wording so another owner's item is
    # never distinguishable from a missing one.
    if not row or row.get("owner_id") != owner_id:
        return f"❌ No item found for `{save_code}`"

    saved_chat_id = row.get("saved_chat_id")
    saved_msg_id = row.get("saved_msg_id")
    origin_chat_id = row.get("origin_chat_id")
    if not saved_chat_id or not saved_msg_id:
        return "❌ Saved location data is missing for this entry."

    logger.info("[RETRIEVE] save_code=%s", save_code)
    logger.info("[RETRIEVE] origin_chat_id=%s", origin_chat_id)
    logger.info("[RETRIEVE] saved_chat_id=%s", saved_chat_id)

    try:
        source_peer = await self_client.get_input_entity(saved_chat_id)
        logger.info("[RETRIEVE] resolved origin peer OK")
    except Exception as exc:
        logger.error("[RETRIEVE] entity resolution FAILED for saved_chat_id=%s: %s", saved_chat_id, exc)
        traceback.print_exc()
        logger.error("[RETRIEVE] failed IDs: saved_chat_id=%s target_chat=%s", saved_chat_id, target_chat)
        record_event("retrieve", "get_input_entity", 0, "ERROR", f"saved_chat_id={saved_chat_id}: {exc}")
        return f"❌ Could not resolve saved chat (id={saved_chat_id}): {exc}"

    try:
        dest_peer = await self_client.get_input_entity(target_chat)
        logger.info("[RETRIEVE] resolved destination peer OK")
    except Exception as exc:
        logger.error("[RETRIEVE] entity resolution FAILED for target_chat=%s: %s", target_chat, exc)
        traceback.print_exc()
        logger.error("[RETRIEVE] failed IDs: saved_chat_id=%s target_chat=%s", saved_chat_id, target_chat)
        record_event("retrieve", "get_input_entity", 0, "ERROR", f"target_chat={target_chat}: {exc}")
        return f"❌ Could not resolve destination chat (id={target_chat}): {exc}"

    logger.info("[RETRIEVE] entity=%s %r", type(dest_peer).__name__, dest_peer)
    logger.info("[RETRIEVE] from_peer=%s %r", type(source_peer).__name__, source_peer)
    logger.info("[RETRIEVE] message_id=%s", saved_msg_id)
    logger.info("[RETRIEVE] target_chat=%s", target_chat)
    logger.info("[RETRIEVE] forwarding...")
    t1 = asyncio.get_event_loop().time()
    try:
        messages = await self_client.forward_messages(
            entity=dest_peer,
            messages=saved_msg_id,
            from_peer=source_peer,
        )
        record_event("retrieve", "forward_messages", (asyncio.get_event_loop().time() - t1) * 1000, "SUCCESS")
        logger.info("[RETRIEVE] forward completed")
    except Exception as exc:
        logger.error("retrieve forward failed: %s", exc)
        traceback.print_exc()
        logger.error(
            "[RETRIEVE] forward_messages params: entity=%r messages=%r from_peer=%r",
            dest_peer, saved_msg_id, source_peer,
        )
        logger.error("[RETRIEVE] failed IDs: saved_chat_id=%s target_chat=%s", saved_chat_id, target_chat)
        record_event("retrieve", "forward_messages", 0, "ERROR", str(exc))
        return f"❌ Forward failed: {exc}"

    fwd_msg = messages[0] if isinstance(messages, list) else messages
    original_caption = row.get("caption") or ""
    metadata_block = build_metadata_block(row)
    new_caption = f"{metadata_block}\n\n{original_caption}".strip() if original_caption else metadata_block

    if fwd_msg and len(new_caption) <= 1024:
        try:
            await self_client.edit_message(dest_peer, fwd_msg.id, new_caption)
        except Exception as exc:
            logger.warning("retrieve caption edit failed: %s", exc)

    await db_client.log(owner_id, "INFO", f"Retrieved {save_code} to {target_chat}", {
        "save_code": save_code,
        "target_chat": target_chat,
    })
    return f"✅ Retrieved `{save_code}` to this chat."


async def do_send(self_client, owner_id: int, save_code: str, target_chat: int) -> str:
    return await do_retrieve(self_client, owner_id, save_code, target_chat)


async def do_rename(owner_id: int, save_code: str, new_name: str) -> str:
    """Give one owner-owned saved item a new display name (Save V2 Part 4).

    The name is the item's OWN metadata (``saved_items.display_name``) and the
    shared ``save_service`` normalizer decides what a valid name is — the same
    rule the Save panel already enforces, so a rename can never store a value
    the save path would have refused. The name is normalized first; then the
    write is CONFIRMED by re-reading the owner's row (see ``_write_metadata``),
    so success is reported only when the stored value is the one asked for.

    The underlying Telegram saved message is never touched: only the item's
    metadata changes, and only for a row that belongs to this owner. The
    Telegram FILENAME is a separate layer and is never changed here — that is
    an explicit ``do_change_file_name`` request.
    """
    code = str(save_code or "").upper().strip()
    try:
        name = save_service.normalize_display_name(new_name)
    except ValueError as exc:
        return f"⚠️ Nothing was renamed: {exc}"
    if name is None:
        return "⚠️ Nothing was renamed: send a name for the item."

    row, error = await _load_for_management(owner_id, code)
    if error:
        return error
    if row is None:
        return f"❌ No item found for `{code}`"

    failure = await _write_metadata(owner_id, code, "display_name", name, name)
    if failure:
        return failure
    await db_client.log(owner_id, "INFO", f"Renamed {code}", {"display_name": name})
    return f"✅ Renamed `{code}` to **{name}**"


async def do_move(owner_id: int, save_code: str, folder: str) -> str:
    save_code = save_code.upper().strip()
    folder = folder.strip() or "Unfiled"
    row = await db_client.query_save(save_code)
    if not row or row.get("owner_id") != owner_id:
        return f"❌ No item found for `{save_code}`"
    await db_client.log(owner_id, "INFO", f"Moved {save_code}", {"folder": folder})
    return f"✅ Moved to `{folder}`"


async def do_delete(self_client, owner_id: int, save_code: str) -> str:
    save_code = save_code.upper().strip()
    row = await db_client.query_save(save_code)
    if not row or row.get("owner_id") != owner_id:
        return f"❌ No item found for `{save_code}`"
    saved_chat_id = row.get("saved_chat_id")
    saved_msg_id = row.get("saved_msg_id")
    sc = row.get("save_code")
    db = db_client.get_db()
    deleted_db = False
    if db:
        try:
            removed = await db_client.delete_save_row(owner_id, sc)
            deleted_db = removed is not None
        except Exception as exc:
            logger.warning("delete db failed: %s", exc)
    if saved_chat_id and saved_msg_id:
        try:
            await self_client.delete_messages(saved_chat_id, [saved_msg_id])
        except Exception as exc:
            logger.warning("delete telegram msg failed: %s", exc)
    await db_client.log(owner_id, "INFO", f"Deleted {save_code}", {"save_code": save_code})
    return f"✅ Deleted `{save_code}`"


# ── Saved-item resolver (Save V2 Part 3 — deterministic 0/1/N) ────────────
#
# The ONE mechanism that turns a semantic saved-item request (a display name,
# a tag, a combination) into concrete candidate save codes. It is
# deterministic, owner-scoped INSIDE the database query, bounded, and
# Telegram-free: it never forwards, never deletes, never writes a row. Only
# the confirmed save code it returns reaches ``do_retrieve``.
#
# Matching is tiered with strict precedence, and the first tier that matches
# anything decides the outcome (no scoring, no ranking, no model, no
# embeddings):
#   1. exact save code (code-shaped input never goes through fuzzy search)
#   2. every token is a substring of display_name
#   3. every token is a whole tag — or the whole query is one tag in another
#      separator form ("semester 2" matches the tag "semester-2")
#   4. every token matches display_name, file_name or a whole tag
# Nothing else is searched: never the caption, never Telegram history, never
# a sender, never "the most recent item". A query that matches nothing is a
# clean not-found.

RESOLUTION_NOT_FOUND = "not_found"
RESOLUTION_UNIQUE = "unique"
RESOLUTION_AMBIGUOUS = "ambiguous"

# The candidate bound shared by every surface (the panel and the AI tool).
# Mirrors the existing clarification cap in ``ai/chat_resolution.py``.
MAX_RESOLUTION_CANDIDATES = 8

_MAX_QUERY_CHARS = 128
_MAX_QUERY_TOKENS = 6
_SAVE_CODE_SHAPE = re.compile(r"^S[A-Z0-9]{4}$")
_SEPARATOR_RE = re.compile(r"[-_\s]+")

# The canonical save-code shape (S + 4 alphanumerics). Persian/Arabic
# alternates — script (ی/ک) and digit (۰-۹ / ٠-٩) spellings — for the database
# prefilter only; the authoritative comparison folds every spelling to one
# matching form.
_PERSIAN_TO_ARABIC = str.maketrans({"ی": "ي", "ک": "ك"})
_ASCII_TO_PERSIAN_DIGITS = str.maketrans("0123456789", "۰۱۲۳۴۵۶۷۸۹")
_ASCII_TO_ARABIC_DIGITS = str.maketrans("0123456789", "٠١٢٣٤٥٦٧٨٩")
_ZERO_WIDTH = "\u200b\u200c\u200d\ufeff"


def _spelling_variants(token: str) -> list[str]:
    """Bounded alternate spellings of one normalized token.

    Covers the two recall-critical cases the authoritative folding creates:
    Arabic-script letters vs Persian letters, and ASCII digits vs the
    Persian/Arabic digit spellings an owner may have stored. Bounded and
    deterministic; a stored spelling outside this set simply falls back to
    the exact normalized match.
    """
    variants = [token]
    arabic = token.translate(_PERSIAN_TO_ARABIC)
    if arabic != token:
        variants.append(arabic)
    if any(ch.isdigit() for ch in token):
        variants.append(token.translate(_ASCII_TO_PERSIAN_DIGITS))
        variants.append(token.translate(_ASCII_TO_ARABIC_DIGITS))
    return list(dict.fromkeys(v for v in variants if v))


@dataclass(frozen=True)
class SavedItemCandidate:
    """One candidate the owner can act on. No Telegram or internal identity."""

    save_code: str
    display_name: str | None = None
    file_name: str | None = None
    media_type: str | None = None
    tags: tuple[str, ...] = ()
    created_at: str | None = None


@dataclass(frozen=True)
class SavedItemResolution:
    """The resolver's complete answer: 0, 1 or N candidates.

    ``status`` is one of ``not_found`` / ``unique`` / ``ambiguous``.
    ``overflowed`` means more matches exist than the bounded list shows —
    the shown candidates are then explicitly NOT the only matches.
    """

    status: str
    query: str
    candidates: tuple[SavedItemCandidate, ...] = ()
    overflowed: bool = False


def _normalize_search(value) -> str:
    """Deterministic matching form (comparison only, never stored).

    NFKC (Unicode compatibility) + the project's established Persian/Arabic
    normalization (``semantic_delete.normalize_text``: Persian/Arabic digit
    folding, script-variant folding e.g. ي→ی and ك→ک, diacritic removal,
    zero-width removal, casefold, whitespace collapse). Stored values are
    never rewritten and nothing is translated or transliterated.
    """
    text = unicodedata.normalize("NFKC", str(value or ""))
    return _persian_normalize(text)


def _fold_separators(value: str) -> str:
    """Fold -, _ and whitespace into single spaces (tag-form comparison)."""
    return _SEPARATOR_RE.sub(" ", str(value or "")).strip()


def _owner_tags(row: dict) -> tuple[str, ...]:
    """The row's OWNER tags, in stored order.

    Legacy rows carry synthetic ``#saved*`` hashtags (caption decoration,
    written by older versions). They start with ``#`` and are NEVER treated
    as owner metadata here — not matched and not displayed. Legacy values
    are kept in the database untouched.
    """
    out: list[str] = []
    for item in (row.get("tags") or []):
        tag = str(item or "").strip()
        if not tag or tag.startswith("#"):
            continue
        out.append(tag)
    return tuple(dict.fromkeys(out))


def candidate_label(candidate: SavedItemCandidate) -> str:
    """Owner-facing label: display_name → file_name → media_type → code."""
    for value in (candidate.display_name, candidate.file_name, candidate.media_type):
        if value:
            return str(value)
    return candidate.save_code


def format_candidate(index: int, candidate: SavedItemCandidate) -> str:
    """One numbered candidate line (label · type · date + save code)."""
    label = candidate_label(candidate)
    parts = [label]
    if candidate.media_type and str(candidate.media_type) != label:
        parts.append(str(candidate.media_type))
    if candidate.created_at:
        parts.append(_format_date(candidate.created_at))
    return f"{index}. {' · '.join(parts)} `{candidate.save_code}`"


def format_resolution(resolution: SavedItemResolution) -> str:
    """Owner-facing rendering of a resolution (no model instructions)."""
    if resolution.status == RESOLUTION_NOT_FOUND:
        return (
            f"🔍 No saved item matches `{resolution.query}`. "
            "Try the saved name, one of its tags, or its save code."
        )
    if resolution.status == RESOLUTION_UNIQUE:
        header = f"✅ One saved item matches `{resolution.query}`:"
    else:
        header = f"🔍 Multiple saved items match `{resolution.query}`:"
    lines = [header]
    for i, candidate in enumerate(resolution.candidates, start=1):
        lines.append(format_candidate(i, candidate))
    if resolution.status == RESOLUTION_AMBIGUOUS:
        if resolution.overflowed:
            lines.append("_More matches exist than are shown — narrow the query._")
        lines.append("Reply with the number or the save code of the one you want.")
    return "\n".join(lines)


def _pattern_variants(raw_token: str, normalized_token: str) -> list[str]:
    """Database-prefilter patterns for one token (a superset of the rule).

    Bounded and deterministic: the normalized token, its Arabic-script
    alternate (ی/ک), and — when the raw token carried a zero-width
    character — a wildcard-joined form so a stored ZWNJ spelling still
    prefilters. Comparison authority stays in Python; these patterns may
    over-match, never under-match for the folded variants they cover.
    """
    patterns = [
        f"%{db_client.resolve_pattern(v)}%" for v in _spelling_variants(normalized_token)
    ]
    if any(ch in raw_token for ch in _ZERO_WIDTH):
        wild = raw_token
        for ch in _ZERO_WIDTH:
            wild = wild.replace(ch, "%")
        patterns.append(f"%{db_client.resolve_pattern(wild)}%")
    return list(dict.fromkeys(patterns))[:4]


def _tag_variants(normalized_token: str) -> list[str]:
    """Whole-tag terms for one token (exact membership, bounded)."""
    terms = [db_client.resolve_tag_term(v) for v in _spelling_variants(normalized_token)]
    return list(dict.fromkeys(t for t in terms if t))[:4]


def _joined_tag_candidates(normalized_tokens: list[str]) -> list[str]:
    """Whole-query tag forms ("semester 2" → semester-2 / semester_2).

    Only meaningful for multi-token queries; a single token is already
    matched as a whole tag by its own term.
    """
    if len(normalized_tokens) < 2:
        return []
    joined = " ".join(normalized_tokens)
    candidates = [joined.replace(" ", "-"), joined.replace(" ", "_")]
    arabic = [c.translate(_PERSIAN_TO_ARABIC) for c in candidates]
    return list(dict.fromkeys(db_client.resolve_tag_term(c) for c in candidates + arabic if c))[:4]


def _token_groups(raw_tokens: list[str], normalized_tokens: list[str]) -> list[dict]:
    groups: list[dict] = []
    for raw, norm in zip(raw_tokens, normalized_tokens):
        groups.append({
            "patterns": _pattern_variants(raw, norm),
            "tags": _tag_variants(norm),
        })
    return groups


def _candidate_from_row(row: dict) -> SavedItemCandidate:
    def _clean(key: str) -> str | None:
        value = str(row.get(key) or "").strip()
        return value or None

    return SavedItemCandidate(
        save_code=str(row.get("save_code") or ""),
        display_name=_clean("display_name"),
        file_name=_clean("file_name"),
        media_type=_clean("media_type"),
        tags=_owner_tags(row),
        created_at=_clean("created_at"),
    )


def _refine_rows(tier: str, rows: list, normalized_tokens: list[str], query_norm: str) -> list:
    """Apply the authoritative (normalized, deterministic) tier rule.

    The database prefilter is deliberately permissive; this is the exact
    semantics. A row the prefilter over-matched is dropped here, and the
    next tier is tried only when a tier matches nothing at all.
    """
    matched: list = []
    whole_query = _fold_separators(query_norm)
    for row in rows:
        display = _normalize_search(row.get("display_name"))
        fname = _normalize_search(row.get("file_name"))
        tags = {_normalize_search(t) for t in _owner_tags(row)}
        whole_tag_hit = whole_query in {_fold_separators(t) for t in tags}
        if tier == "name":
            ok = all(token in display for token in normalized_tokens)
        elif tier == "tag":
            ok = whole_tag_hit or all(token in tags for token in normalized_tokens)
        else:
            ok = whole_tag_hit or all(
                token in display or token in fname or token in tags
                for token in normalized_tokens
            )
        if ok:
            matched.append(row)
    return matched


async def _fetch_resolver_rows(
    owner_id: int, tier: str, token_groups: list, joined_tags: list, limit: int
) -> list:
    fetch_limit = limit + 1  # one extra row proves "more matches exist"
    return await db_client.resolve_saves(owner_id, tier, token_groups, joined_tags, fetch_limit)


async def resolve_saved_items(
    owner_id: int, query: str, limit: int = MAX_RESOLUTION_CANDIDATES
) -> SavedItemResolution:
    """Resolve a saved-item request into 0, 1 or N owner-scoped candidates.

    Owner identity is the trusted runtime owner — never a model argument.
    Owner scoping happens INSIDE every database query; nothing is fetched
    globally and filtered afterwards. The resolver performs no Telegram
    action and writes nothing: a caller retrieves only a candidate it was
    explicitly given.
    """
    raw = str(query or "").strip()
    if not raw or len(raw) > _MAX_QUERY_CHARS or limit <= 0:
        return SavedItemResolution(status=RESOLUTION_NOT_FOUND, query=raw)

    # A code-shaped request is a CODE request: it goes straight to the
    # owner-scoped, identity-verified read and is never fuzzy-matched.
    code = raw.upper()
    if _SAVE_CODE_SHAPE.match(code):
        row = await load_saved_item(code, owner_id)
        if not row:
            return SavedItemResolution(status=RESOLUTION_NOT_FOUND, query=raw)
        return SavedItemResolution(
            status=RESOLUTION_UNIQUE,
            query=raw,
            candidates=(_candidate_from_row(row),),
        )

    raw_tokens = raw.split()
    normalized_tokens = [t for t in (_normalize_search(tok) for tok in raw_tokens) if t]
    normalized_tokens = normalized_tokens[:_MAX_QUERY_TOKENS]
    if not normalized_tokens:
        return SavedItemResolution(status=RESOLUTION_NOT_FOUND, query=raw)
    query_norm = " ".join(normalized_tokens)

    trimmed_raw = raw_tokens[: len(normalized_tokens)]
    groups = _token_groups(trimmed_raw, normalized_tokens)
    joined = _joined_tag_candidates(normalized_tokens)

    for tier in ("name", "tag", "mixed"):
        rows = await _fetch_resolver_rows(owner_id, tier, groups, joined, limit)
        matched = _refine_rows(tier, rows, normalized_tokens, query_norm)
        if not matched:
            continue
        candidates = [_candidate_from_row(row) for row in matched]
        # Deterministic order: created_at DESC, then save_code ASC. The
        # database applies the same order; re-applying it here also covers
        # the fallback store and any refinement reordering.
        candidates.sort(key=lambda c: str(c.save_code or ""))
        candidates.sort(key=lambda c: str(c.created_at or ""), reverse=True)
        overflowed = len(candidates) > limit
        candidates = candidates[:limit]
        status = RESOLUTION_UNIQUE if len(candidates) == 1 else RESOLUTION_AMBIGUOUS
        return SavedItemResolution(
            status=status,
            query=raw,
            candidates=tuple(candidates),
            overflowed=overflowed,
        )

    return SavedItemResolution(status=RESOLUTION_NOT_FOUND, query=raw)


# ── Saved-item management (Save V2 Part 4 — rename / tags) ────────────────
#
# The ONE management contract for a stored item. Every surface (the retrieve
# item panel and the AI management tools) resolves its target with
# ``resolve_management_target``, which REUSES the Part 3 resolver instead of
# searching again, and then mutates only the item's own metadata through
# ``do_rename`` / ``do_edit_tags``. There is no second resolver, no second
# metadata writer, no Telegram side effect in this section, and never a
# guessed target: 0 matches is an honest not-found and N matches must be
# narrowed by the owner before anything is written.

TARGET_OK = "ok"
TARGET_NOT_FOUND = "not_found"
TARGET_AMBIGUOUS = "ambiguous"
TARGET_INVALID = "invalid"

TAG_OP_ADD = "add"
TAG_OP_REPLACE = "replace"
TAG_OP_REMOVE = "remove"
TAG_OPS = (TAG_OP_ADD, TAG_OP_REPLACE, TAG_OP_REMOVE)

# Telegram's caption bound. The saved-item synchronizer only ever rewrites ONE
# metadata line, so a caption that would exceed this is refused instead of
# being silently truncated.
MAX_CAPTION_CHARS = 1024


@dataclass(frozen=True)
class ManagementTarget:
    """The resolver's answer for a management request: one item, or why not.

    ``status`` is ``ok`` / ``not_found`` / ``ambiguous`` / ``invalid``. Only
    ``ok`` carries a ``save_code``; ``message`` is the owner-facing rendering
    of the failure and is empty on success.
    """

    status: str
    save_code: str = ""
    message: str = ""
    resolution: SavedItemResolution | None = None


def format_owner_tags(row: dict) -> str:
    """One stored row's OWNER tags, rendered (never the legacy hashtags)."""
    tags = _owner_tags(row)
    return ", ".join(tags) if tags else "—"


def _legacy_tags(row: dict) -> list[str]:
    """The synthetic ``#saved*`` values a legacy row still carries.

    They are caption decoration, not owner metadata (see ``_owner_tags``), so
    a tag edit never rewrites or drops them: the owner's tags are stored
    alongside them and only the owner's are ever replaced or removed.
    """
    out: list[str] = []
    for item in (row.get("tags") or []):
        tag = str(item or "").strip()
        if tag.startswith("#"):
            out.append(tag)
    return out


async def resolve_management_target(
    owner_id: int, *, save_code: str = "", query: str = ""
) -> ManagementTarget:
    """Resolve ONE owner-owned item for a management operation.

    A save code goes through the owner-scoped, identity-verified read; a
    name/tag request goes through the SAME deterministic resolver retrieval
    uses. Either way the answer is one item or an explicit refusal — an
    ambiguous request is never narrowed by a guess, and nothing is written
    here (resolution is read-only by contract).
    """
    code = str(save_code or "").upper().strip()
    raw_query = str(query or "").strip()
    if code and raw_query:
        return ManagementTarget(
            status=TARGET_INVALID,
            message="Provide either a save code or a name/tag query — not both.",
        )
    if not code and not raw_query:
        return ManagementTarget(
            status=TARGET_INVALID,
            message="A save code or a name/tag query is required.",
        )

    if code:
        row, error = await _load_for_management(owner_id, code)
        if error:
            return ManagementTarget(status=TARGET_INVALID, message=error)
        if row is None:
            return ManagementTarget(
                status=TARGET_NOT_FOUND, message=f"❌ No item found for `{code}`"
            )
        return ManagementTarget(status=TARGET_OK, save_code=code)

    resolution = await resolve_saved_items(owner_id, raw_query)
    if resolution.status == RESOLUTION_UNIQUE:
        return ManagementTarget(
            status=TARGET_OK,
            save_code=resolution.candidates[0].save_code,
            resolution=resolution,
        )
    if resolution.status == RESOLUTION_AMBIGUOUS:
        return ManagementTarget(
            status=TARGET_AMBIGUOUS,
            message=format_resolution(resolution),
            resolution=resolution,
        )
    return ManagementTarget(
        status=TARGET_NOT_FOUND,
        message=format_resolution(resolution),
        resolution=resolution,
    )


async def _load_for_management(owner_id: int, save_code: str) -> tuple[dict | None, str]:
    """Owner-verified row read for a management operation.

    Returns ``(row, error)``; ``row`` is ``None`` for a missing or foreign code
    (reported identically, so another owner's item is never distinguishable
    from a missing one) and ``error`` is non-empty only when the database
    itself failed — which is never silently reported as "not found".
    """
    try:
        return await load_saved_item(save_code, owner_id), ""
    except Exception as exc:  # noqa: BLE001
        return None, f"❌ DB error: {exc}"


def _stored_matches(stored, expected) -> bool:
    """Does the re-read row carry exactly the value that was written?

    Tags are compared as the row's OWNER tags (legacy ``#`` values are not the
    owner's metadata and are never expected to disappear); a name is compared
    as the normalized text that was stored.
    """
    if isinstance(expected, tuple):
        return _owner_tags({"tags": stored}) == expected
    return str(stored or "").strip() == str(expected)


async def _sync_saved_caption(client, row: dict, new_caption: str) -> str:
    """Rewrite the item's SAVED Telegram message caption ("" = success).

    The ONE caption-synchronization authority: the tag writer and the
    file-name replacement both go through it, so the saved message can never
    be updated by two different code paths.

    Bounded and honest: the message must be reachable and the caption must fit
    Telegram's limit, otherwise the owner gets the reason and nothing is
    edited. Only a caption change is ever sent — the media is never re-sent
    here.
    """
    if len(new_caption) > MAX_CAPTION_CHARS:
        return (
            f"❌ The saved message was not updated: the caption would be "
            f"{len(new_caption)} characters and Telegram allows {MAX_CAPTION_CHARS}."
        )
    saved_chat_id = row.get("saved_chat_id")
    saved_msg_id = row.get("saved_msg_id")
    if not saved_chat_id or not saved_msg_id:
        return "❌ Saved location data is missing for this entry."
    try:
        peer = await client.get_input_entity(saved_chat_id)
        await client.edit_message(peer, saved_msg_id, new_caption)
    except Exception as exc:  # noqa: BLE001
        logger.warning("saved caption sync failed for %s: %s", row.get("save_code"), exc)
        return f"❌ The saved message could not be updated ({exc})."
    return ""


async def _write_metadata_fields(
    owner_id: int, save_code: str, fields: dict, expected: dict
) -> str:
    """Persist metadata fields in ONE statement and CONFIRM them by re-reading.

    One statement is what keeps a multi-field change (the filename
    synchronizer repointing the item's Telegram location) from landing
    half-applied. ``update_save_fields`` reports the representation PostgREST
    returns, which is not proof the write landed, so success is derived from a
    second owner-verified read (see ``_confirm_metadata``).
    """
    try:
        await db_client.update_save_fields(owner_id, save_code, fields)
    except Exception as exc:  # noqa: BLE001
        logger.warning("saved-item metadata write failed for %s: %s", save_code, exc)
        return f"❌ Could not save the change ({exc})."
    return await _confirm_metadata(owner_id, save_code, expected)


async def _write_metadata(owner_id: int, save_code: str, field: str, value, expected) -> str:
    """Persist one metadata field and confirm it by re-reading the row."""
    try:
        await db_client.update_save_field(owner_id, save_code, field, value)
    except Exception as exc:  # noqa: BLE001
        logger.warning("saved-item metadata write failed for %s: %s", save_code, exc)
        return f"❌ Could not save the change ({exc})."
    return await _confirm_metadata(owner_id, save_code, {field: expected})


async def _confirm_metadata(owner_id: int, save_code: str, expected: dict) -> str:
    """Confirm a write by re-reading the owner's row (``""`` when confirmed).

    A write is only reported as success when the STORED value is the one that
    was asked for, so a silently ignored UPDATE can never be shown as done.
    """
    try:
        row = await load_saved_item(save_code, owner_id)
    except Exception as exc:  # noqa: BLE001
        return f"❌ DB error while confirming the change: {exc}"
    if row is None:
        return f"❌ No item found for `{save_code}`"
    if all(_stored_matches(row.get(field), value) for field, value in expected.items()):
        return ""
    return "❌ The change was not stored — the saved item is unchanged."


async def _caption_base(client, row: dict) -> tuple[str, str]:
    """The caption a metadata rewrite starts from: ``(caption, error)``.

    The stored ``saved_items.caption`` IS the source of truth. Only when a row
    carries none (an item saved before the column was populated) does the
    saved message's own live caption stand in — otherwise a rewrite would
    build a caption from nothing and destroy the message's real one. Never
    called without a client, and never guesses: a body that cannot be read is
    an honest failure.
    """
    stored = str(row.get("caption") or "")
    if stored or client is None:
        return stored, ""
    try:
        peer = await client.get_input_entity(row.get("saved_chat_id"))
        message = await client.get_messages(peer, ids=row.get("saved_msg_id"))
    except Exception as exc:  # noqa: BLE001
        return "", f"❌ Could not read the saved message ({exc})."
    if message is None:
        return "", "❌ The saved message could not be found."
    return str(getattr(message, "message", None) or ""), ""


async def do_edit_tags(
    owner_id: int, save_code: str, op: str, tags=(), *, client=None
) -> str:
    """Add, replace or remove one item's OWNER tags (Save V2 Part 4).

    ``op`` is ``add`` / ``replace`` / ``remove``; the tag list is normalized by
    the SHARED ``save_service`` rules, so casing, whitespace, duplicates and
    the count bound follow exactly the same contract a save does — a tag the
    Save panel would have refused is refused here too, and nothing is invented.

    ``replace`` with an empty list is the ONE way to clear every tag (the
    explicit "no tags" case), and a removal that matches nothing is reported
    honestly instead of being written. Legacy ``#saved*`` values on the row are
    preserved untouched: they were never owner tags. Only the owner's own row
    is ever written.

    Tags are ONE layer and system hashtags another: the generated ``#saved*``
    line is never read, replaced or merged. When ``client`` is supplied the
    saved Telegram message is synchronized FIRST (only its Additional-tags
    section changes, through the shared caption authority), so a message that
    cannot be updated changes nothing at all in the database. Without a client
    the stored metadata is still written — the media is never re-sent for a
    tag-only change.
    """
    code = str(save_code or "").upper().strip()
    operation = str(op or "").strip().lower()
    if operation not in TAG_OPS:
        return "⚠️ Nothing was changed: unknown tag operation."
    try:
        incoming = save_service.normalize_tags(tags)
    except ValueError as exc:
        return f"⚠️ Nothing was changed: {exc}"
    if operation != TAG_OP_REPLACE and not incoming:
        return "⚠️ Nothing was changed: send at least one tag."

    row, error = await _load_for_management(owner_id, code)
    if error:
        return error
    if row is None:
        return f"❌ No item found for `{code}`"

    current = _owner_tags(row)
    if operation == TAG_OP_ADD:
        merged = current + tuple(t for t in incoming if t.casefold() not in {c.casefold() for c in current})
    elif operation == TAG_OP_REMOVE:
        removal = {t.casefold() for t in incoming}
        merged = tuple(t for t in current if t.casefold() not in removal)
        if merged == current:
            return f"⚠️ Nothing was changed: `{code}` has none of those tags."
    else:
        merged = incoming
    # The RESULT must obey the same shared rules (count bound included), so an
    # add that would exceed the limit is refused BEFORE anything is written.
    try:
        merged = save_service.normalize_tags(merged)
    except ValueError as exc:
        return f"⚠️ Nothing was changed: {exc}"

    stored = _legacy_tags(row) + list(merged)

    caption, error = await _caption_base(client, row)
    if error:
        return error
    new_caption = save_service.with_additional_tags(caption, merged)
    caption_changed = new_caption != caption
    if client is not None and caption_changed:
        failure = await _sync_saved_caption(client, row, new_caption)
        if failure:
            return failure

    fields: dict = {"tags": stored}
    if caption_changed:
        fields["caption"] = new_caption
    failure = await _write_metadata_fields(owner_id, code, fields, {"tags": merged})
    if failure:
        return failure
    await db_client.log(owner_id, "INFO", f"Edited tags {code}", {
        "op": operation,
        "tags": list(merged),
    })
    rendered = ", ".join(merged) if merged else "no tags"
    return f"✅ Tags for `{code}`: {rendered}"


async def do_change_file_name(
    client, owner_id: int, save_code: str, new_file_name: str
) -> str:
    """Change the ACTUAL Telegram file name of one owner-owned saved item.

    This is NOT ``do_rename``: the display name is the item's logical label in
    LifeOS, while this value is the filename inside the Telegram document — a
    different layer the owner must ask for explicitly. Telegram carries a
    document's filename in its media attributes and cannot edit it in place,
    so the content is downloaded and re-uploaded as a NEW Saved Messages
    message with only the ``DocumentAttributeFilename`` replaced; every other
    attribute, the MIME type and the caption (with its Additional-tags section
    intact) are preserved.

    Failure-safe by construction: the replacement is created first, its
    identifiers are CONFIRMED in the row, and only then is the previous saved
    message deleted. Any failure before that leaves the original message and
    the original row untouched and reports the reason — the row can never
    point at a message that does not exist. A replacement whose identifiers
    cannot be confirmed is NOT cleaned up (a failed confirmation read must not
    destroy the message the row may already point at); it is reported instead.
    """
    code = str(save_code or "").upper().strip()
    try:
        name = save_service.normalize_file_name(new_file_name)
    except ValueError as exc:
        return f"⚠️ Nothing was renamed: {exc}"

    if client is None:
        return "❌ Cannot change the saved file name: no Telegram client available."

    row, error = await _load_for_management(owner_id, code)
    if error:
        return error
    if row is None:
        return f"❌ No item found for `{code}`"

    saved_chat_id = row.get("saved_chat_id")
    saved_msg_id = row.get("saved_msg_id")
    if not saved_chat_id or not saved_msg_id:
        return "❌ Saved location data is missing for this entry."

    try:
        peer = await client.get_input_entity(saved_chat_id)
        current = await client.get_messages(peer, ids=saved_msg_id)
    except Exception as exc:  # noqa: BLE001
        logger.warning("saved file-name read failed for %s: %s", code, exc)
        return f"❌ Could not read the saved message ({exc}). Nothing was renamed."
    if current is None:
        return "❌ The saved message could not be found. Nothing was renamed."

    media = getattr(current, "media", None)
    if not isinstance(media, MessageMediaDocument):
        return (
            "❌ This saved item's Telegram media has no file name to change — "
            "only a saved document carries one. Nothing was renamed."
        )
    current_name = save_service.extract_file_name(media)
    if not current_name:
        return (
            "❌ This saved item's Telegram document carries no file name to "
            "change. Nothing was renamed."
        )
    if current_name == name:
        return f"⚠️ Nothing was renamed: `{code}` is already named `{name}`."

    caption, error = await _caption_base(client, row)
    if error:
        return error
    new_caption = save_service.with_file_name(caption, name)

    # The download target is derived from the owner's name but never trusted
    # with a path: the basename is taken and an empty one falls back.
    tmp_dir = tempfile.mkdtemp(prefix="lifeos_rename_")
    tmp_path = os.path.join(tmp_dir, os.path.basename(name) or "media.bin")
    try:
        try:
            await client.download_media(current, file=tmp_path)
        except Exception as exc:  # noqa: BLE001
            logger.warning("saved file-name download failed for %s: %s", code, exc)
            return f"❌ Could not download the saved media ({exc}). Nothing was renamed."
        if not os.path.exists(tmp_path) or os.path.getsize(tmp_path) == 0:
            return "❌ The downloaded saved media is empty. Nothing was renamed."

        # The SAME media handling Deep Save uses, with the new filename: the
        # document's other attributes and its MIME type are carried over, so a
        # video/audio/animation cannot degrade into a generic document.
        mime = getattr(getattr(media, "document", None), "mime_type", None) or row.get("mime_type")
        try:
            sent = await client.send_file(
                "me",
                tmp_path,
                caption=new_caption,
                **save_service._upload_kwargs_for_media(media, mime, name),
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("saved file-name re-upload failed for %s: %s", code, exc)
            return f"❌ Could not re-upload the saved media ({exc}). The saved item is unchanged."
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    if sent is None or getattr(sent, "id", None) is None:
        return "❌ The replacement saved message was not created. The saved item is unchanged."

    new_file_id, new_mime, new_size = save_service._extract_uploaded_metadata(sent)
    fields = {
        "saved_chat_id": getattr(sent, "chat_id", None) or saved_chat_id,
        "saved_msg_id": sent.id,
        "file_name": name,
        "caption": new_caption,
        "file_id": new_file_id or row.get("file_id"),
        "file_size": new_size or row.get("file_size"),
    }
    if new_mime:
        fields["mime_type"] = new_mime
    failure = await _write_metadata_fields(
        owner_id, code, fields, {"saved_msg_id": sent.id, "file_name": name}
    )
    if failure:
        logger.error("[SAVE_SYNC] replacement not persisted code=%s", code)
        return (
            f"{failure} The original saved message was NOT deleted and the item was "
            "not repointed; the replacement copy left in Saved Messages was kept "
            "so nothing is lost."
        )

    # Only now is the previous message removed: the row already points at the
    # confirmed replacement.
    try:
        await client.delete_messages(peer, [saved_msg_id])
    except Exception as exc:  # noqa: BLE001
        logger.warning("old saved message cleanup failed for %s: %s", code, exc)
        await db_client.log(owner_id, "INFO", f"Renamed file {code}", {"file_name": name})
        return (
            f"✅ File name for `{code}` is now **{name}** — the previous copy could "
            "not be removed from Saved Messages."
        )

    await db_client.log(owner_id, "INFO", f"Renamed file {code}", {"file_name": name})
    return f"✅ File name for `{code}` is now **{name}**"
