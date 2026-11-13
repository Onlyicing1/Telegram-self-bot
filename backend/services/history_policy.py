"""History's product policy — per-peer message caps for ONE manual request.

This is FEATURE POLICY, deliberately separate from the reusable Telegram peer
taxonomy in ``backend/telegram_api/peers``. It owns only the numbers: how many
messages one manual History request may cover for a given ``PeerType``, which
counts the panel may offer, and the honest refusal text when a request is not
allowed. It performs no Telegram RPC, database access, AI work or rendering, and
it never selects a bot, limits an AI request's capacity, or changes retrieval.

The 300-message caps for bot chats and broadcast channels are HARD product
limits: a larger request is REFUSED, never clamped with ``min()``. ``UNKNOWN`` is
refused outright — an unidentifiable peer never falls back to the 1,000-message
policy.

AI feasibility is a separate gate in ``history_ai_service``: passing the product
cap does not promise the workload fits one AI request.
"""
from __future__ import annotations

from types import MappingProxyType
from typing import Any

from backend.telegram_api.peers import PeerType

PRIVATE_USER = PeerType.PRIVATE_USER
PRIVATE_BOT = PeerType.PRIVATE_BOT
BASIC_GROUP = PeerType.BASIC_GROUP
SUPERGROUP = PeerType.SUPERGROUP
BROADCAST_CHANNEL = PeerType.BROADCAST_CHANNEL
UNKNOWN = PeerType.UNKNOWN

#: Hard maximum messages one manual History request may cover per peer type.
MAX_MESSAGES: MappingProxyType[PeerType, int] = MappingProxyType({
    PRIVATE_USER: 1000,
    PRIVATE_BOT: 300,
    BASIC_GROUP: 1000,
    SUPERGROUP: 1000,
    BROADCAST_CHANNEL: 300,
    UNKNOWN: 0,
})

#: The exact count buttons the History panel offers for each peer type. A
#: capped peer never advertises 500/1,000 — those choices are hidden, not
#: rendered disabled.
COUNT_CHOICES: MappingProxyType[PeerType, tuple[int, ...]] = MappingProxyType({
    PRIVATE_USER: (100, 250, 500, 1000),
    PRIVATE_BOT: (100, 250, 300),
    BASIC_GROUP: (100, 250, 500, 1000),
    SUPERGROUP: (100, 250, 500, 1000),
    BROADCAST_CHANNEL: (100, 250, 300),
    UNKNOWN: (),
})

_PEER_LABELS = {
    PRIVATE_USER: "A private user chat",
    PRIVATE_BOT: "A bot chat",
    BASIC_GROUP: "A basic group",
    SUPERGROUP: "A supergroup",
    BROADCAST_CHANNEL: "A broadcast channel",
}

UNKNOWN_REFUSAL = (
    "This chat's type could not be determined, so History cannot run on it. "
    "Select the chat again."
)


def maximum_for(peer_type: Any) -> int:
    """Hard maximum for ``peer_type``; 0 when the type is unsupported."""
    return int(MAX_MESSAGES.get(peer_type, 0)) if isinstance(peer_type, PeerType) else 0


def choices_for(peer_type: Any) -> tuple[int, ...]:
    """The offered count buttons for ``peer_type``; empty when unsupported."""
    if not isinstance(peer_type, PeerType):
        return ()
    return COUNT_CHOICES.get(peer_type, ())


def _choices_text(peer_type: PeerType) -> str:
    choices = choices_for(peer_type)
    if not choices:
        return "none"
    if len(choices) == 1:
        return str(choices[0])
    return ", ".join(str(value) for value in choices[:-1]) + f" or {choices[-1]}"


def _unsupported(peer_type: Any) -> bool:
    return (
        not isinstance(peer_type, PeerType)
        or peer_type is UNKNOWN
        or maximum_for(peer_type) <= 0
        or not choices_for(peer_type)
    )


def validate_count(peer_type: Any, requested: Any) -> str | None:
    """Validate a requested count against the peer's HARD maximum.

    Returns ``None`` when the request is allowed, otherwise a deterministic
    refusal explaining the actual limit. Any positive integer up to the maximum
    is valid here (the service boundary accepts counts a caller computed); the
    panel's narrower choice set is enforced by :func:`validate_choice`.

    Booleans, strings and other non-integers are rejected rather than coerced,
    and a count is never clamped to the maximum.
    """
    if _unsupported(peer_type):
        return UNKNOWN_REFUSAL
    maximum = maximum_for(peer_type)
    choices_text = _choices_text(peer_type)
    if isinstance(requested, bool) or not isinstance(requested, int):
        return f"Choose one of the offered message counts: {choices_text}."
    if requested <= 0:
        return f"Choose one of the offered message counts: {choices_text}."
    if requested > maximum:
        label = _PEER_LABELS.get(peer_type, "This chat")
        return (
            f"{label} is limited to {maximum} messages per request; "
            f"choose {choices_text}."
        )
    return None


def validate_choice(peer_type: Any, requested: Any) -> str | None:
    """Validate a count against the EXACT choices the panel offers.

    Used for panel callbacks, where a stale button may carry a count that is
    within the hard cap but is not offered for the current peer (for example
    ``999`` for a private user, or ``1000`` after the selected peer changed to a
    broadcast channel). Returns ``None`` only for an offered choice.
    """
    if _unsupported(peer_type):
        return UNKNOWN_REFUSAL
    if isinstance(requested, bool) or not isinstance(requested, int):
        return f"Choose one of the offered message counts: {_choices_text(peer_type)}."
    if requested not in choices_for(peer_type):
        return f"Choose one of the offered message counts: {_choices_text(peer_type)}."
    return None
