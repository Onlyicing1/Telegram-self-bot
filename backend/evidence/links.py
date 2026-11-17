"""Locally constructed Telegram links for owner-facing Evidence surfaces.

Provider output never supplies a URL.  Every link is built here from the
locally validated ``peer_id`` and ``message_id`` already stored on the run or
archive source.  The link is only used by owner-facing rendering; it is never
part of a provider projection or a generic tool result.
"""
from __future__ import annotations


def telegram_message_link(peer_id: int, message_id: int) -> str:
    """Return a Telegram deep link for one stored source.

    Users use the app URI so a private user id is not placed in an HTTP path.
    Supergroups/channels that use Telegram's ``-100…`` peer id get the canonical
    ``https://t.me/c/<internal>/<message>`` form.  Other chats use the app URI.
    """
    peer = int(peer_id)
    mid = int(message_id)
    if mid <= 0:
        raise ValueError("message_id must be positive")
    if peer > 0:
        return f"tg://openmessage?user_id={peer}&message_id={mid}"
    text = str(peer)
    if text.startswith("-100"):
        internal = abs(peer) - 1_000_000_000_000
        if internal > 0:
            return f"https://t.me/c/{internal}/{mid}"
    return f"tg://openmessage?chat_id={peer}&message_id={mid}"


def source_link(peer_id: int, message_id: int) -> str:
    """Alias with the domain name used by the Evidence Explorer."""
    return telegram_message_link(peer_id, message_id)
