"""Pure, reusable Telegram peer classification.

The ONLY input is a resolved Telethon entity. The classifier answers one
question — what kind of Telegram peer is this? — and nothing else. Feature
policy (which peers a feature accepts, which limits apply, who may use it)
stays with the feature, so this module imports no History, AI, service, panel,
database or scheduling code and performs no RPC.

It exists because the same question used to be answered in several places with
different rules: ``type(entity).__name__ == "User"`` string comparisons, and
``Dialog.is_channel`` — which is true for BOTH broadcast channels and
supergroups — so the answer depended on check order and bot/gigagroup handling
was missing entirely. Telegram's flags are authoritative; they are read here
once.

Telegram's model is not a Python-class hierarchy: ``Channel`` covers broadcast
channels and (mega)gigagroups, while ``Chat`` is a basic group and ``User`` is a
human or a bot. Classification therefore reads ``User.bot``,
``Channel.broadcast`` and ``Channel.megagroup`` / ``Channel.gigagroup``, never a
title, username, id pattern, Dialog boolean or model output. A ``gigagroup`` is
a supergroup variant and shares the ``SUPERGROUP`` type — there is no separate
policy type for it.

Nothing here is persisted: a peer type is always derived from the currently
resolved entity, so a basic group that Telegram migrates to a supergroup is
classified correctly the next time it is resolved.
"""
from __future__ import annotations

from enum import Enum
from typing import Any

from telethon.tl.types import Channel, Chat, User


class PeerType(str, Enum):
    """The normalized peer vocabulary shared by every LifeOS feature."""

    PRIVATE_USER = "private_user"
    PRIVATE_BOT = "private_bot"
    BASIC_GROUP = "basic_group"
    SUPERGROUP = "supergroup"
    BROADCAST_CHANNEL = "broadcast_channel"
    UNKNOWN = "unknown"


def classify_entity(entity: Any) -> PeerType:
    """Classify ONE resolved Telegram entity. Never guesses, never raises.

    Args:
        entity: A resolved Telethon entity (``User``, ``Chat`` or ``Channel``).
            Anything else — ``None``, a Dialog, a serialized dict, a test double
            or a partially-populated object — is ``UNKNOWN``. Callers must treat
            ``UNKNOWN`` as "cannot decide": do not assume a channel, a group or
            a human, and never fall back to names or ids.

    Returns:
        The entity's :class:`PeerType`. Flag values are compared strictly
        against ``True`` so an absent flag is never read as a positive one.
    """
    if isinstance(entity, User):
        # ``bot`` is the only authority for a bot account; a missing flag means
        # "not established", which for a User is a human account.
        return PeerType.PRIVATE_BOT if getattr(entity, "bot", None) is True else PeerType.PRIVATE_USER
    if isinstance(entity, Chat):
        return PeerType.BASIC_GROUP
    if isinstance(entity, Channel):
        if getattr(entity, "broadcast", None) is True:
            # Checked first: a broadcast channel is never a supergroup, whatever
            # the other flags say.
            return PeerType.BROADCAST_CHANNEL
        if (
            getattr(entity, "megagroup", None) is True
            or getattr(entity, "gigagroup", None) is True
        ):
            # A gigagroup is a supergroup variant, deliberately not its own type.
            return PeerType.SUPERGROUP
        return PeerType.UNKNOWN
    return PeerType.UNKNOWN
