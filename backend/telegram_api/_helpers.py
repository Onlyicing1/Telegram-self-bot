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

logger = logging.getLogger(__name__)


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
    }
