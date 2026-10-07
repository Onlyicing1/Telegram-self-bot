"""
Custom-emoji module — document + sticker-set resolution (Emoji & Reaction).

Typed wrappers around ``messages.GetCustomEmojiDocumentsRequest`` and
``messages.GetStickerSetRequest`` used by the emoji library's set
enumeration (ROADMAP §9). Same conventions as ``messages.py``: short
bounded calls (``guarded_await``), exceptions normalized to
``TelegramAPIError``/``TelegramTimeoutError``, plain-dict results —
callers never touch Telethon objects and never see raw TL types.

Honesty contract: a document Telegram did not return, or one without a
custom-emoji attribute / usable sticker-set identity, is reported as
absent or ``set: None`` — nothing about set membership is ever inferred
or fabricated here.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon.tl.functions.messages import (
    GetCustomEmojiDocumentsRequest,
    GetStickerSetRequest,
)
from telethon.tl.types import (
    DocumentAttributeCustomEmoji,
    InputStickerSetID,
    InputStickerSetShortName,
)

from backend.runtime.operation_watchdog import guarded_await
from backend.telegram_api.exceptions import (
    TelegramAPIError,
    TelegramTimeoutError,
)

logger = logging.getLogger(__name__)

_SHORT_CALL_TIMEOUT = 30.0

#: Hard bound on one resolution batch (ids per RPC). The caller — the emoji
#: library importer — chunks its candidate list itself; this is the last
#: guard against an oversized request.
MAX_DOCUMENTS_PER_CALL = 100


def _sticker_set_identity(stickerset: Any) -> dict[str, Any] | None:
    """The usable identity of a custom-emoji attribute's stickerset.

    Only what Telegram actually provided: an id-based or short-name-based
    reference. Any other/empty input sticker set resolves to ``None`` —
    the caller treats that as "set unknown", never as data.
    """
    if isinstance(stickerset, InputStickerSetID):
        set_id = getattr(stickerset, "id", None)
        access_hash = getattr(stickerset, "access_hash", None)
        if isinstance(set_id, int) and isinstance(access_hash, int):
            return {"kind": "id", "id": set_id, "access_hash": access_hash}
        return None
    if isinstance(stickerset, InputStickerSetShortName):
        short_name = getattr(stickerset, "short_name", None)
        if isinstance(short_name, str) and short_name:
            return {"kind": "short_name", "short_name": short_name}
        return None
    return None


def _serialize_document(document: Any) -> dict[str, Any] | None:
    """One resolved document as ``{document_id, alt, set}``.

    ``alt`` is the custom-emoji attribute's Unicode fallback text (may be
    empty — Telegram provides what it provides). ``set`` is the owning
    set identity or ``None``. A document without a custom-emoji attribute
    is not a custom emoji: it yields ``None`` and the caller counts it
    unresolved rather than guessing.
    """
    document_id = getattr(document, "id", None)
    if not isinstance(document_id, int) or isinstance(document_id, bool) or document_id <= 0:
        return None
    for attribute in getattr(document, "attributes", None) or []:
        if not isinstance(attribute, DocumentAttributeCustomEmoji):
            continue
        alt = getattr(attribute, "alt", None)
        return {
            "document_id": document_id,
            "alt": alt if isinstance(alt, str) else "",
            "set": _sticker_set_identity(getattr(attribute, "stickerset", None)),
        }
    return None


async def get_custom_emoji_documents(
    client: Any, document_ids: list[int]
) -> list[dict[str, Any]]:
    """Resolve custom-emoji document ids into their documents.

    Returns one ``{document_id, alt, set}`` dict per document Telegram
    actually returned that carries a custom-emoji attribute, in Telegram's
    response order. Ids Telegram did not resolve are simply absent from the
    result — resolution is never guessed. Bounded: at most
    ``MAX_DOCUMENTS_PER_CALL`` ids are sent in one request.
    """
    ids: list[int] = []
    for raw in document_ids or []:
        if isinstance(raw, int) and not isinstance(raw, bool) and raw > 0:
            ids.append(raw)
        elif isinstance(raw, str) and raw.isdigit():
            ids.append(int(raw))
    ids = ids[:MAX_DOCUMENTS_PER_CALL]
    if not ids:
        return []
    try:
        documents = await guarded_await(
            client(GetCustomEmojiDocumentsRequest(document_id=ids)),
            name="telegram:get_custom_emoji_documents",
            timeout=_SHORT_CALL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise TelegramTimeoutError(
            f"get_custom_emoji_documents timed out after {_SHORT_CALL_TIMEOUT:g}s"
        )
    except Exception as exc:
        if isinstance(exc, TelegramAPIError):
            raise
        raise TelegramAPIError(f"get_custom_emoji_documents failed: {exc}") from exc
    serialized: list[dict[str, Any]] = []
    for document in documents or []:
        entry = _serialize_document(document)
        if entry is not None:
            serialized.append(entry)
    return serialized


async def get_sticker_set(client: Any, set_ref: dict[str, Any]) -> dict[str, Any]:
    """Fetch ONE sticker/custom-emoji set with its full member list.

    ``set_ref`` is a set identity as returned by
    :func:`get_custom_emoji_documents` (``kind`` ``id`` or
    ``short_name``). ``hash=0`` forces the full member list instead of a
    not-modified stub. Returns
    ``{set_id, access_hash, title, short_name, count, members}`` where each
    member is ``{document_id, alt}`` — only documents that carry a
    custom-emoji attribute appear as members, so a sticker set requested by
    mistake contributes no fake emoji records.
    """
    if not isinstance(set_ref, dict):
        raise TelegramAPIError("get_sticker_set requires a set identity dict")
    if set_ref.get("kind") == "id":
        stickerset: Any = InputStickerSetID(
            id=set_ref.get("id"), access_hash=set_ref.get("access_hash")
        )
    elif set_ref.get("kind") == "short_name":
        stickerset = InputStickerSetShortName(short_name=set_ref.get("short_name"))
    else:
        raise TelegramAPIError("get_sticker_set received an unusable set identity")
    try:
        result = await guarded_await(
            client(GetStickerSetRequest(stickerset=stickerset, hash=0)),
            name="telegram:get_sticker_set",
            timeout=_SHORT_CALL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise TelegramTimeoutError(
            f"get_sticker_set timed out after {_SHORT_CALL_TIMEOUT:g}s"
        )
    except Exception as exc:
        if isinstance(exc, TelegramAPIError):
            raise
        raise TelegramAPIError(f"get_sticker_set failed: {exc}") from exc

    info = getattr(result, "set", None)
    if info is None:
        raise TelegramAPIError("get_sticker_set returned no set information")
    members: list[dict[str, Any]] = []
    for document in getattr(result, "documents", None) or []:
        entry = _serialize_document(document)
        if entry is not None:
            members.append({"document_id": entry["document_id"], "alt": entry["alt"]})
    return {
        "set_id": getattr(info, "id", None),
        "access_hash": getattr(info, "access_hash", None),
        "title": getattr(info, "title", "") or "",
        "short_name": getattr(info, "short_name", "") or "",
        "count": getattr(info, "count", None),
        "members": members,
    }
