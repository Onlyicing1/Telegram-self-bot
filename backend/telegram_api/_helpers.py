"""
Internal helpers — entity resolution and result serialization.

These functions convert Telethon objects into plain dicts so the API
layer never leaks Telethon types to callers.
"""
from __future__ import annotations

import logging
from typing import Any

from telethon.tl.types import (
    User,
    Chat,
    Channel,
    PeerUser,
    PeerChat,
    PeerChannel,
)

from backend.telegram_api.exceptions import TelegramAPIError

logger = logging.getLogger(__name__)

#: Entity type names for entities that carry only offset/length. Types with
#: extra payload (TextUrl, Pre, CustomEmoji, MentionName) are handled
#: explicitly in :func:`dict_entities_to_tl`.
_SIMPLE_ENTITY_TYPES = (
    "MessageEntityMention", "MessageEntityHashtag", "MessageEntityBotCommand",
    "MessageEntityUrl", "MessageEntityEmail", "MessageEntityBold",
    "MessageEntityItalic", "MessageEntityUnderline", "MessageEntityStrike",
    "MessageEntityCode", "MessageEntityPhone", "MessageEntityCashtag",
    "MessageEntitySpoiler", "MessageEntityBlockquote",
)

#: Every entity type the dict representation can carry through this layer.
#: This is the authoritative support set: consumers that must fail closed on
#: an entity they cannot rebuild (the Emoji & Reaction transformer, ROADMAP
#: §22) check against it instead of guessing, and :func:`dict_entities_to_tl`
#: refuses anything outside it.
SUPPORTED_ENTITY_TYPES = frozenset(_SIMPLE_ENTITY_TYPES) | frozenset({
    "MessageEntityTextUrl", "MessageEntityPre",
    "MessageEntityCustomEmoji", "MessageEntityMentionName",
})


def utf16_length(text: str) -> int:
    """Length of ``text`` in UTF-16 code units — the unit Telegram uses for
    message length limits and entity offsets/lengths. Supplementary-plane
    characters (most emoji) occupy TWO units."""
    return len(text.encode("utf-16-le")) // 2


def utf16_offset(text: str, index: int) -> int:
    """UTF-16 code-unit offset of Python character index ``index``."""
    if index < 0 or index > len(text):
        raise ValueError(f"character index {index} out of range for text of {len(text)} chars")
    return utf16_length(text[:index])


def utf16_index_at(text: str, offset: int) -> int:
    """Python character index whose UTF-16 offset is ``offset``.

    Raises ``ValueError`` for out-of-range offsets and for offsets that fall
    inside a surrogate pair (mid-emoji) — a corrupt entity, never silently
    clamped."""
    total = utf16_length(text)
    if offset < 0 or offset > total:
        raise ValueError(f"UTF-16 offset {offset} out of range for text of {total} units")
    units = 0
    for i, ch in enumerate(text):
        if units == offset:
            return i
        width = 2 if ord(ch) > 0xFFFF else 1
        if units + width > offset:
            raise ValueError(f"UTF-16 offset {offset} falls inside a surrogate pair")
        units += width
    return len(text)


def _serialize_entity_list(entities: Any) -> list[dict[str, Any]]:
    """Convert Telethon MessageEntity objects to plain dicts.

    Offsets/lengths stay in UTF-16 code units exactly as Telegram sent them —
    they are the authoritative currency every downstream consumer (the
    Emoji & Reaction transformer, ROADMAP §22) must operate in.
    """
    result: list[dict[str, Any]] = []
    for ent in entities or []:
        data: dict[str, Any] = {
            "type": type(ent).__name__,
            "offset": getattr(ent, "offset", 0),
            "length": getattr(ent, "length", 0),
        }
        url = getattr(ent, "url", None)
        if url:
            data["url"] = url
        doc = getattr(ent, "document_id", None)
        if doc is not None:
            data["document_id"] = doc
        user_id = getattr(ent, "user_id", None)
        if user_id is not None:
            data["user_id"] = user_id
        language = getattr(ent, "language", None)
        if language:
            data["language"] = language
        result.append(data)
    return result


def _peer_to_id(peer: Any) -> int | None:
    if peer is None:
        return None
    if isinstance(peer, PeerUser):
        return peer.user_id
    if isinstance(peer, PeerChat):
        return -peer.chat_id
    if isinstance(peer, PeerChannel):
        return -1000000000000 - peer.channel_id
    return getattr(peer, "user_id", None) or getattr(peer, "chat_id", None) or getattr(peer, "channel_id", None)


def serialize_user(user: Any) -> dict[str, Any]:
    """Convert a Telethon User object to a plain dict."""
    if user is None:
        return {}
    first = getattr(user, "first_name", None) or ""
    last = getattr(user, "last_name", None) or ""
    return {
        "id": getattr(user, "id", 0),
        "first_name": first,
        "last_name": last,
        "full_name": f"{first} {last}".strip(),
        "username": getattr(user, "username", None),
        "phone": getattr(user, "phone", None),
        "about": getattr(user, "about", None),
        "is_bot": getattr(user, "bot", False),
        "is_deleted": getattr(user, "deleted", False),
    }


def serialize_chat(chat: Any) -> dict[str, Any]:
    """Convert a Telethon Chat/Channel/User to a plain dict."""
    if chat is None:
        return {}
    if isinstance(chat, User):
        result = serialize_user(chat)
        result["type"] = "user"
        return result
    title = getattr(chat, "title", None) or ""
    chat_id = getattr(chat, "id", 0)
    if isinstance(chat, Channel):
        chat_type = "channel" if getattr(chat, "broadcast", False) else "supergroup"
    elif isinstance(chat, Chat):
        chat_type = "group"
    else:
        chat_type = "unknown"
    return {
        "id": chat_id,
        "title": title,
        "type": chat_type,
        "username": getattr(chat, "username", None),
    }


def serialize_message(msg: Any) -> dict[str, Any]:
    """Convert a Telethon Message to a plain dict."""
    if msg is None:
        return {}
    return {
        "id": getattr(msg, "id", 0),
        "chat_id": getattr(msg, "chat_id", 0),
        "sender_id": getattr(msg, "sender_id", 0),
        "text": getattr(msg, "text", None) or getattr(msg, "message", None) or "",
        "date": getattr(msg, "date", None),
        "has_media": getattr(msg, "media", None) is not None,
        "reply_to_msg_id": getattr(getattr(msg, "reply_to", None), "reply_to_msg_id", None),
        "out": getattr(msg, "out", False),
        "entities": _serialize_entity_list(getattr(msg, "entities", None)),
    }


async def _resolve_input_user(client: Any, user_id: int) -> Any:
    """Resolve a user id to an InputUser through the client's entity cache.

    Used when rebuilding mention-name entities on a DIFFERENT client (the
    bridge bot resolves against its own session). Failure is an honest
    error — the send-first reconstruction ordering keeps the original
    message intact when delivery aborts (ROADMAP §15/§28).
    """
    from telethon import utils

    entity = await client.get_input_entity(user_id)
    input_user = utils.get_input_user(entity)
    if input_user is None:
        raise TelegramAPIError(f"cannot resolve user {user_id} to an InputUser")
    return input_user


async def dict_entities_to_tl(client: Any, entities: list[dict[str, Any]] | None) -> list[Any]:
    """Rebuild serialized plain-dict entities into Telethon TL objects.

    The inverse of :func:`_serialize_entity_list`: a transformed message's
    entity dicts (as produced by ``serialize_message``) become real
    ``MessageEntity*`` instances suitable for ``send_message(
    formatting_entities=...)``. Unknown types and missing payloads raise
    ``TelegramAPIError`` — an entity is never silently dropped (ROADMAP
    §22/§28). Mentions require the target client to resolve the user.
    """
    from telethon.tl import types as tl_types

    simple = {name: getattr(tl_types, name) for name in _SIMPLE_ENTITY_TYPES}
    result: list[Any] = []
    for ent in entities or []:
        etype = ent.get("type")
        offset = ent.get("offset", 0)
        length = ent.get("length", 0)
        if etype not in SUPPORTED_ENTITY_TYPES:
            raise TelegramAPIError(f"unknown entity type: {etype!r} ({ent})")
        if etype in simple:
            result.append(simple[etype](offset, length))
        elif etype == "MessageEntityTextUrl":
            url = ent.get("url")
            if not url:
                raise TelegramAPIError(f"TextUrl entity missing url: {ent}")
            result.append(tl_types.MessageEntityTextUrl(offset, length, url))
        elif etype == "MessageEntityPre":
            result.append(tl_types.MessageEntityPre(offset, length, ent.get("language", "")))
        elif etype == "MessageEntityCustomEmoji":
            doc = ent.get("document_id")
            if doc is None:
                raise TelegramAPIError(f"custom-emoji entity missing document_id: {ent}")
            try:
                doc_id = int(doc)
            except (TypeError, ValueError):
                raise TelegramAPIError(f"custom-emoji entity has invalid document_id: {ent}") from None
            result.append(tl_types.MessageEntityCustomEmoji(offset, length, doc_id))
        elif etype == "MessageEntityMentionName":
            user_id = ent.get("user_id")
            if user_id is None:
                raise TelegramAPIError(f"mention-name entity missing user_id: {ent}")
            result.append(tl_types.MessageEntityMentionName(
                offset, length, await _resolve_input_user(client, int(user_id))
            ))
        else:
            raise TelegramAPIError(f"unknown entity type: {etype!r} ({ent})")
    return result
