"""
Reactions module — typed message-reaction wrapper (Emoji & Reaction, §27).

A narrow typed wrapper around ``messages.SendReactionRequest`` for the
reaction subsystem, deliberately separate from the emoji-replacement
pipeline (ROADMAP §27: the two share no state, no code path and no panel).

Same conventions as the sibling modules (``messages.py`` /
``custom_emoji.py``): bounded timeouts through ``guarded_await``, exceptions
normalized to ``TelegramAPIError`` / ``TelegramTimeoutError``, plain-dict
results — callers never see Telethon objects and never build a request.

Scope boundary: this module exposes exactly the TWO send forms the feature
design needs — ONE Unicode emoji (``ReactionEmoji``) or ONE custom-emoji
document (``ReactionCustomEmoji``) — for ONE explicit ``(chat_id, msg_id)``
target, plus ONE read: the account's available-reaction catalog
(``get_available_reactions``). Arbitrary TL requests, multi-reaction
payloads and reaction removal are not reachable from here. A rejected
reaction is never retried with a different representation: Telegram's own
verdict is reported honestly (ROADMAP §28).
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon.tl import types as tl_types
from telethon.tl.functions.messages import (
    GetAvailableReactionsRequest,
    SendReactionRequest,
)
from telethon.tl.types import AvailableReaction, ReactionCustomEmoji, ReactionEmoji

from backend.runtime.operation_watchdog import guarded_await
from backend.telegram_api._helpers import utf16_length
from backend.telegram_api.exceptions import (
    TelegramAPIError,
    TelegramTimeoutError,
)

logger = logging.getLogger(__name__)

_SHORT_CALL_TIMEOUT = 30.0

#: Defensive bound on one account's reaction catalog. Telegram's set is far
#: smaller; this only stops a malformed/oversized response from being
#: propagated whole.
MAX_AVAILABLE_REACTIONS = 200

#: The two reaction kinds this module can send — the plain-dict reaction
#: shape shared by the wrapper, the reaction service and the Glass UI.
KIND_EMOJI = "emoji"
KIND_CUSTOM_EMOJI = "custom_emoji"

#: One reaction emoji is ONE grapheme; Telegram's own reaction set is short
#: ZWJ sequences. A longer value is not a reaction, so it is refused here
#: instead of being sent and rejected by Telegram.
MAX_EMOJI_UTF16_UNITS = 32


def _is_int(value: Any) -> bool:
    return isinstance(value, int) and not isinstance(value, bool)


def valid_chat_id(chat_id: Any) -> bool:
    """A chat id Telegram can address: a non-zero integer (never a bool)."""
    return _is_int(chat_id) and chat_id != 0


def valid_message_id(msg_id: Any) -> bool:
    """A message id: a strictly positive integer (never a bool)."""
    return _is_int(msg_id) and msg_id > 0


def valid_target(chat_id: Any, msg_id: Any) -> bool:
    """The explicit target pair must be usable BEFORE any RPC is attempted."""
    return valid_chat_id(chat_id) and valid_message_id(msg_id)


def normalize_emoji(emoji: Any) -> str | None:
    """The reaction emoji exactly as Telegram accepts it, or ``None``.

    A non-string, empty, whitespace-carrying, non-canonical (surrounding
    whitespace) or over-long value is refused — the value is validated, never
    rewritten into something the owner did not send.
    """
    if not isinstance(emoji, str):
        return None
    if not emoji or emoji != emoji.strip():
        return None
    if any(ch.isspace() for ch in emoji):
        return None
    if utf16_length(emoji) > MAX_EMOJI_UTF16_UNITS:
        return None
    return emoji


def normalize_document_id(document_id: Any) -> int | None:
    """A custom-emoji document id: a digit string or a positive int, else ``None``."""
    if isinstance(document_id, str) and document_id.isdigit():
        document_id = int(document_id)
    if not _is_int(document_id) or document_id <= 0:
        return None
    return document_id


def normalize_reaction(reaction: Any) -> dict[str, Any] | None:
    """The ONE plain-dict reaction shape, or ``None`` for an unusable value.

    ``{"kind": "emoji", "emoji": "👍"}`` or
    ``{"kind": "custom_emoji", "document_id": 123}`` — any other kind,
    a malformed payload, or an extra/missing field fails closed.
    """
    if not isinstance(reaction, dict):
        return None
    kind = reaction.get("kind")
    if kind == KIND_EMOJI:
        emoji = normalize_emoji(reaction.get("emoji"))
        return None if emoji is None else {"kind": KIND_EMOJI, "emoji": emoji}
    if kind == KIND_CUSTOM_EMOJI:
        document_id = normalize_document_id(reaction.get("document_id"))
        return None if document_id is None else {
            "kind": KIND_CUSTOM_EMOJI, "document_id": document_id,
        }
    return None


def _to_tl_reaction(reaction: dict[str, Any]) -> Any:
    """The Telethon reaction object for a NORMALIZED descriptor."""
    if reaction["kind"] == KIND_EMOJI:
        return ReactionEmoji(emoticon=reaction["emoji"])
    return ReactionCustomEmoji(document_id=reaction["document_id"])


async def send_reaction(
    client: Any,
    chat_id: int,
    msg_id: int,
    reaction: dict[str, Any],
    *,
    big: bool = False,
    add_to_recent: bool = True,
) -> dict[str, Any]:
    """Apply ONE reaction to ONE explicit message.

    Validates the target and the reaction value before any RPC, sends exactly
    ONE ``SendReactionRequest`` with a bounded timeout, and returns
    ``{chat_id, message_id, reaction, big}`` echoed from the validated input
    (Telegram's ``Updates`` response is not a reaction state, so nothing about
    the message's current reactions is inferred from it).

    Raises ``TelegramAPIError`` for an unusable input or a failed call and
    ``TelegramTimeoutError`` when the bounded call does not answer. There is
    no retry and no fallback representation.
    """
    if not valid_target(chat_id, msg_id):
        raise TelegramAPIError(
            "send_reaction requires an explicit non-zero chat id and a positive message id"
        )
    normalized = normalize_reaction(reaction)
    if normalized is None:
        raise TelegramAPIError("send_reaction received an unusable reaction value")
    request = SendReactionRequest(
        peer=chat_id,
        msg_id=msg_id,
        big=bool(big),
        add_to_recent=bool(add_to_recent),
        reaction=[_to_tl_reaction(normalized)],
    )
    try:
        await guarded_await(
            client(request),
            name="telegram:send_reaction",
            timeout=_SHORT_CALL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise TelegramTimeoutError(
            f"send_reaction timed out after {_SHORT_CALL_TIMEOUT:g}s"
        )
    except Exception as exc:
        if isinstance(exc, TelegramAPIError):
            raise
        raise TelegramAPIError(f"send_reaction failed: {exc}") from exc
    return {
        "chat_id": chat_id,
        "message_id": msg_id,
        "reaction": normalized,
        "big": bool(big),
    }


def _serialize_available_reaction(reaction: Any) -> dict[str, Any] | None:
    """One ``AvailableReaction`` as ``{reaction, title, premium, inactive}``.

    ``reaction`` is the exact emoji string Telegram exposes and expects back
    in ``ReactionEmoji`` — never normalized, never substituted. A record
    without a usable reaction string is refused (``None``) so it can never be
    offered as a selectable option.
    """
    if not isinstance(reaction, AvailableReaction):
        return None
    value = getattr(reaction, "reaction", None)
    if not isinstance(value, str) or not value:
        return None
    title = getattr(reaction, "title", None)
    return {
        "reaction": value,
        "title": title if isinstance(title, str) else "",
        "premium": bool(getattr(reaction, "premium", False)),
        "inactive": bool(getattr(reaction, "inactive", False)),
    }


async def get_available_reactions(
    client: Any,
    *,
    hash: int = 0,
) -> dict[str, Any]:
    """Read the account's available-reaction catalog from Telegram.

    Sends exactly ONE ``messages.GetAvailableReactionsRequest`` with a bounded
    timeout and returns a plain dict:

    ``{"ok": True, "modified": bool, "hash": int, "reactions": [...]}``

    where each reaction is ``{reaction, title, premium, inactive}``.
    ``modified=True`` means Telegram answered ``AvailableReactionsNotModified``
    — the caller's cached catalog (matching ``hash``) is still current, so
    ``reactions`` is empty and MUST NOT be read as "no reactions".

    Raises ``TelegramAPIError`` for an unusable client or a failed call and
    ``TelegramTimeoutError`` when the bounded call does not answer. An
    unexpected/unsupported response shape fails closed (``ok=False``) rather
    than fabricating a catalog.
    """
    if client is None:
        raise TelegramAPIError("get_available_reactions requires a connected client")
    try:
        response = await guarded_await(
            client(GetAvailableReactionsRequest(hash=int(hash))),
            name="telegram:get_available_reactions",
            timeout=_SHORT_CALL_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise TelegramTimeoutError(
            f"get_available_reactions timed out after {_SHORT_CALL_TIMEOUT:g}s"
        )
    except Exception as exc:
        if isinstance(exc, TelegramAPIError):
            raise
        raise TelegramAPIError(f"get_available_reactions failed: {exc}") from exc

    if isinstance(response, tl_types.messages.AvailableReactionsNotModified):
        return {"ok": True, "modified": True, "hash": int(hash), "reactions": []}
    if not isinstance(response, tl_types.messages.AvailableReactions):
        raise TelegramAPIError(
            "get_available_reactions received an unexpected response "
            f"({type(response).__name__})"
        )
    raw = list(getattr(response, "reactions", None) or [])[:MAX_AVAILABLE_REACTIONS]
    serialized: list[dict[str, Any]] = []
    for record in raw:
        item = _serialize_available_reaction(record)
        if item is not None:
            serialized.append(item)
    new_hash = getattr(response, "hash", hash)
    return {
        "ok": True,
        "modified": False,
        "hash": int(new_hash) if isinstance(new_hash, int) else int(hash),
        "reactions": serialized,
    }
