"""Tests for the reusable Telegram peer classifier and its call sites (Phase 1).

The classifier is exercised with REAL Telethon entity types, because the whole
point of the module is that ``User.bot`` / ``Chat`` / ``Channel.broadcast`` /
``Channel.megagroup`` / ``Channel.gigagroup`` — not Python class names, Dialog
booleans or names — decide the type.
"""
from __future__ import annotations

import inspect
from types import SimpleNamespace

import pytest
from telethon.tl.types import Channel, Chat, User

from backend.telegram_api import peers
from backend.telegram_api.peers import PeerType, classify_entity


# ── real Telethon entities ──────────────────────────────────────────────────


def _human(user_id: int = 101) -> User:
    return User(id=user_id, first_name="Alice", bot=False)


def _bot(user_id: int = 102) -> User:
    return User(id=user_id, first_name="Helper", bot=True)


def _basic_group(chat_id: int = 201) -> Chat:
    return Chat(
        id=chat_id, title="Basic group", photo=None,
        participants_count=3, date=None, version=1,
    )


def _broadcast(channel_id: int = 301) -> Channel:
    return Channel(
        id=channel_id, title="Broadcast", photo=None, date=None,
        broadcast=True, megagroup=False,
    )


def _supergroup(channel_id: int = 302) -> Channel:
    return Channel(
        id=channel_id, title="Supergroup", photo=None, date=None,
        broadcast=False, megagroup=True,
    )


def _gigagroup(channel_id: int = 303) -> Channel:
    return Channel(
        id=channel_id, title="Gigagroup", photo=None, date=None,
        broadcast=False, megagroup=True, gigagroup=True,
    )


# ── classifier: the normalized vocabulary ───────────────────────────────────


def test_peer_type_values_are_the_shared_vocabulary():
    assert [item.value for item in PeerType] == [
        "private_user",
        "private_bot",
        "basic_group",
        "supergroup",
        "broadcast_channel",
        "unknown",
    ]
    # str-compatible: a value round-trips through the enum.
    assert PeerType("private_bot") is PeerType.PRIVATE_BOT
    assert isinstance(PeerType.PRIVATE_USER, str)


def test_human_user_is_private_user():
    assert classify_entity(_human()) is PeerType.PRIVATE_USER


def test_user_with_absent_bot_flag_is_a_human_not_a_bot():
    plain = User(id=104)
    assert plain.bot is None
    assert classify_entity(plain) is PeerType.PRIVATE_USER


def test_bot_user_is_private_bot_and_not_a_human():
    assert classify_entity(_bot()) is PeerType.PRIVATE_BOT
    assert classify_entity(_human()) is not PeerType.PRIVATE_BOT


def test_basic_group_is_basic_group():
    assert classify_entity(_basic_group()) is PeerType.BASIC_GROUP


def test_broadcast_channel_is_not_a_supergroup():
    assert classify_entity(_broadcast()) is PeerType.BROADCAST_CHANNEL
    assert classify_entity(_broadcast()) is not PeerType.SUPERGROUP


def test_megagroup_channel_is_a_supergroup_and_not_a_channel():
    assert classify_entity(_supergroup()) is PeerType.SUPERGROUP
    assert classify_entity(_supergroup()) is not PeerType.BROADCAST_CHANNEL


def test_gigagroup_is_a_supergroup_variant_not_a_new_type():
    assert classify_entity(_gigagroup()) is PeerType.SUPERGROUP
    # A gigagroup whose megagroup flag is not set is still a supergroup: the
    # gigagroup flag on a Channel is Telegram's own established flag.
    giga_only = Channel(
        id=304, title="Giga only", photo=None, date=None,
        broadcast=False, megagroup=False, gigagroup=True,
    )
    assert classify_entity(giga_only) is PeerType.SUPERGROUP


def test_channel_with_neither_flag_is_unknown_not_guessed():
    neither = Channel(
        id=305, title="Neither", photo=None, date=None,
        broadcast=False, megagroup=False,
    )
    assert neither.gigagroup is None
    assert classify_entity(neither) is PeerType.UNKNOWN


def test_broadcast_wins_over_a_contradictory_megagroup_flag():
    contradictory = Channel(
        id=306, title="Both", photo=None, date=None,
        broadcast=True, megagroup=True,
    )
    assert classify_entity(contradictory) is PeerType.BROADCAST_CHANNEL


@pytest.mark.parametrize(
    "value",
    [
        None,
        0,
        "user",
        object(),
        {},
        [],
        SimpleNamespace(id=1, bot=False, first_name="Fake"),
        SimpleNamespace(is_user=True, is_channel=False, is_group=False),
        SimpleNamespace(broadcast=True),
        SimpleNamespace(megagroup=True),
    ],
)
def test_none_unexpected_partial_and_dialog_like_values_are_unknown(value):
    assert classify_entity(value) is PeerType.UNKNOWN


def test_a_partial_telethon_object_cannot_raise():
    """A real ``Channel`` subclass that lost its flags still fails closed."""

    class PartialChannel(Channel):
        pass

    partial = PartialChannel(id=307, title="Partial", photo=None, date=None)
    assert classify_entity(partial) is PeerType.UNKNOWN


def test_classifier_never_touches_unexpected_attributes():
    """No isinstance match means no attribute access at all (fail closed)."""

    class Exploding:
        def __getattr__(self, name):  # pragma: no cover - must never run
            raise AssertionError(f"classifier read unexpected attribute {name!r}")

    assert classify_entity(Exploding()) is PeerType.UNKNOWN


def test_classifier_is_pure_and_not_coroutine():
    assert inspect.iscoroutinefunction(classify_entity) is False
    signature = inspect.signature(classify_entity)
    assert list(signature.parameters) == ["entity"]
    # Same input, same answer; the entity is not mutated.
    group = _basic_group()
    before = group.to_dict()
    assert classify_entity(group) is classify_entity(group) is PeerType.BASIC_GROUP
    assert group.to_dict() == before


def test_classifier_module_has_no_runtime_service_dependencies():
    source = inspect.getsource(peers)
    assert "async def" not in source
    assert "await " not in source
    assert "backend.services" not in source
    assert "backend.ai" not in source
    assert "backend.bot" not in source
    assert "scheduler" not in source.lower()


# ── call site: History chat picker ──────────────────────────────────────────


def test_history_chat_kind_uses_the_shared_classifier():
    from backend.bot.handlers.history import _chat_kind

    assert _chat_kind(None, _human()) == "private"
    assert _chat_kind(None, _bot()) == "private"
    assert _chat_kind(None, _basic_group()) == "group"
    assert _chat_kind(None, _broadcast()) == "channel"
    assert _chat_kind(None, _supergroup()) == "supergroup"
    assert _chat_kind(None, _gigagroup()) == "supergroup"
    neither = Channel(
        id=308, title="Neither", photo=None, date=None,
        broadcast=False, megagroup=False,
    )
    # Legacy unknown bucket, unchanged; never labelled a channel by default.
    assert _chat_kind(None, neither) == "chat"


def test_history_chat_kind_keeps_the_double_compatibility():
    """The legacy dialog/attribute fallbacks are preserved for test doubles."""
    from backend.bot.handlers.history import _chat_kind

    double = SimpleNamespace(id=1, first_name="Ali")
    dialog = SimpleNamespace(is_user=True, is_channel=False, is_group=False)
    assert _chat_kind(dialog, double) == "private"

    channel_double = SimpleNamespace(broadcast=True)
    legacy_dialog = SimpleNamespace(is_user=False, is_channel=True, is_group=False)
    assert _chat_kind(legacy_dialog, channel_double) == "channel"


# ── call site: General → Chat & Message IDs ─────────────────────────────────


class _EntityClient:
    def __init__(self, entity):
        self._entity = entity
        self.calls: list[int] = []

    async def get_entity(self, chat_id):
        self.calls.append(chat_id)
        if isinstance(self._entity, Exception):
            raise self._entity
        return self._entity

    async def get_messages(self, *args, **kwargs):  # pragma: no cover - unused
        return None


def _general_event(chat_id: int):
    return SimpleNamespace(
        chat_id=chat_id,
        message_id=0,
        _reply_to_msg_id=None,
        message=None,
        original_update=None,
    )


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("entity", "label"),
    [
        (_broadcast(), "Channel"),
        (_supergroup(), "Supergroup"),
        (_gigagroup(), "Supergroup"),
        (_basic_group(), "Group"),
        (_human(), "Private"),
        (_bot(), "Bot"),
        (object(), "Unknown"),
        (RuntimeError("lookup failed"), "Unknown"),
    ],
)
async def test_general_id_displays_the_correct_peer_category(monkeypatch, entity, label):
    from backend.bot.handlers import misc

    client = _EntityClient(entity)
    monkeypatch.setattr("backend.helper.inline_engine._self_client", client)

    _, body, _ = await misc._general_id_action(_general_event(42), "", 42)

    assert f"Type: {label}" in body
    assert client.calls == [42]


# ── call site: Ghost Seen private-user eligibility ──────────────────────────


def test_ghost_seen_accepts_a_real_human_user():
    from backend.services.ghost_seen_v2 import is_private_user_entity

    assert is_private_user_entity(_human()) is True


def test_ghost_seen_rejects_a_real_bot_user():
    from backend.services.ghost_seen_v2 import is_private_user_entity

    assert is_private_user_entity(_bot()) is False


def test_ghost_seen_rejects_real_groups_and_channels():
    from backend.services.ghost_seen_v2 import is_private_user_entity

    assert is_private_user_entity(_basic_group()) is False
    assert is_private_user_entity(_broadcast()) is False
    assert is_private_user_entity(_supergroup()) is False


def test_ghost_seen_keeps_owner_and_double_exclusions():
    from backend.services.ghost_seen_v2 import is_private_user_entity, is_private_user_dialog

    owner = SimpleNamespace(id=7, first_name="Me", bot=False, is_self=True, deleted=False)
    assert is_private_user_entity(owner, owner_id=7) is False

    bot_double = SimpleNamespace(id=8, first_name="Bot", bot=True, is_self=False, deleted=False)
    assert is_private_user_entity(bot_double) is False

    human_double = SimpleNamespace(id=9, first_name="Ali", bot=False, is_self=False, deleted=False)
    assert is_private_user_entity(human_double) is True

    dialog = SimpleNamespace(
        entity=SimpleNamespace(id=10, title="Group"), is_user=False,
        is_group=True, is_channel=False,
    )
    assert is_private_user_dialog(dialog, owner_id=1) is False


# ── call site: Anti-Edit / Anti-Delete bot detection ────────────────────────


def test_aed_detects_a_real_bot_user():
    from backend.services.anti_edit_delete_service import is_bot_entity

    assert is_bot_entity(_bot()) is True


def test_aed_never_classifies_a_human_or_a_channel_as_a_bot():
    from backend.services.anti_edit_delete_service import is_bot_entity

    assert is_bot_entity(_human()) is False
    assert is_bot_entity(_basic_group()) is False
    assert is_bot_entity(_broadcast()) is False
    assert is_bot_entity(None) is False


def test_aed_keeps_the_legacy_user_double_fallback():
    """A non-Telethon ``User`` double still resolves exactly as before."""

    class User:  # noqa: N801 - deliberately the legacy class-name shape
        def __init__(self, bot):  # noqa: A002 - mirrors ``User.bot``
            self.bot = bot

    from backend.services.anti_edit_delete_service import is_bot_entity

    assert is_bot_entity(User(bot=True)) is True
    assert is_bot_entity(User(bot=False)) is False
    assert is_bot_entity(User(bot=None)) is False
