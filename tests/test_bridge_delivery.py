"""
Bot bridge + entity-aware serialization — Phase 0 slice (Emoji & Reaction).

Source-proven contracts this file pins (see ROADMAP.md §22/§17 and
IMPLEMENTATION_REPORT.md):

  1. ``serialize_message`` exposes an ``entities`` key: plain dicts whose
     offsets/lengths stay in UTF-16 code units (Telegram's entity currency),
     with payload fields (url / document_id / user_id / language) preserved.
  2. ``dict_entities_to_tl`` is the exact inverse: dicts become real Telethon
     ``MessageEntity`` objects; unknown types and missing payloads raise
     ``TelegramAPIError`` — an entity is never silently dropped.
  3. UTF-16 helpers are lossless and fail closed: a ``ValueError`` is raised
     for out-of-range offsets and offsets that fall inside a surrogate pair.
  4. ``send_reconstructed`` delivers the transformed content through the
     EXISTING helper bot client (send-only — no update loop), resolves the
     destination peer through the self-client (SAME destination), rebuilds
     entities for ``formatting_entities=``, propagates ``reply_to``, and
     fails honestly when the helper bot is unavailable (send-first
     reconstruction ordering keeps the original intact on failure).

No live Telegram is used: fake clients mimic the Telethon surface consumed
here (``send_message`` with ``formatting_entities``/``reply_to`` kwargs,
``get_input_entity``, ``get_input_entity`` on the bot for mention rebuild).
"""
from __future__ import annotations

import asyncio
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest
from telethon.tl import types as tl_types

import backend.helper.client as helper_client
from backend.telegram_api._helpers import (
    dict_entities_to_tl,
    serialize_message,
    utf16_index_at,
    utf16_length,
    utf16_offset,
)
from backend.telegram_api.bridge import (
    bridge_available,
    bridge_bot_id,
    send_reconstructed,
)
from backend.telegram_api.exceptions import TelegramAPIError, TelegramTimeoutError


# ── UTF-16 helpers ───────────────────────────────────────────────────────────


def test_utf16_length_counts_emoji_as_two_units():
    assert utf16_length("abc") == 3
    assert utf16_length("🫪") == 2
    assert utf16_length("a🫪b") == 4
    assert utf16_length("") == 0


def test_utf16_offset_and_index_roundtrip():
    text = "hi🫪there"
    # 't' starts at char index 3; emoji occupies units 2-3.
    assert utf16_offset(text, 3) == 4
    assert utf16_index_at(text, 4) == 3
    for i in range(len(text) + 1):
        assert utf16_index_at(text, utf16_offset(text, i)) == i


def test_utf16_index_at_rejects_out_of_range():
    with pytest.raises(ValueError):
        utf16_index_at("abc", 4)
    with pytest.raises(ValueError):
        utf16_index_at("abc", -1)


def test_utf16_index_at_rejects_mid_surrogate_offset():
    # Offset 1 of "🫪" falls inside the surrogate pair — corrupt entity.
    with pytest.raises(ValueError):
        utf16_index_at("🫪", 1)


def test_utf16_offset_rejects_out_of_range_index():
    with pytest.raises(ValueError):
        utf16_offset("ab", 3)


# ── serialize_message entities ───────────────────────────────────────────────


def _fake_message(**extra: Any) -> MagicMock:
    msg = MagicMock()
    msg.id = 10
    msg.chat_id = -100123
    msg.sender_id = 42
    msg.text = "a🫪b"
    msg.message = None
    msg.date = None
    msg.media = None
    msg.reply_to = None
    msg.out = True
    msg.entities = extra.pop("entities", [])
    for key, value in extra.items():
        setattr(msg, key, value)
    return msg


def test_serialize_message_includes_entities_key():
    data = serialize_message(_fake_message())
    assert data["text"] == "a🫪b"
    assert data["entities"] == []


def test_serialize_message_keeps_utf16_offsets_and_payload():
    msg = _fake_message(entities=[
        tl_types.MessageEntityCustomEmoji(offset=1, length=2, document_id=555),
        tl_types.MessageEntityTextUrl(offset=3, length=1, url="https://x.example"),
    ])
    data = serialize_message(msg)
    assert data["entities"][0] == {
        "type": "MessageEntityCustomEmoji", "offset": 1, "length": 2, "document_id": 555,
    }
    assert data["entities"][1]["type"] == "MessageEntityTextUrl"
    assert data["entities"][1]["url"] == "https://x.example"


def test_serialize_message_without_entities_attribute_yields_empty_list():
    msg = _fake_message()
    del msg.entities  # older/odd objects may not carry the attribute at all
    data = serialize_message(msg)
    assert data["entities"] == []


# ── dict_entities_to_tl ──────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_dict_entities_roundtrip_simple_and_custom_emoji():
    dicts = [
        {"type": "MessageEntityBold", "offset": 0, "length": 1},
        {"type": "MessageEntityCustomEmoji", "offset": 1, "length": 2, "document_id": "555"},
    ]
    built = await dict_entities_to_tl(None, dicts)
    assert type(built[0]) is tl_types.MessageEntityBold
    assert built[0].offset == 0 and built[0].length == 1
    assert type(built[1]) is tl_types.MessageEntityCustomEmoji
    assert built[1].document_id == 555


@pytest.mark.asyncio
async def test_dict_entities_rebuilds_text_url_and_pre():
    built = await dict_entities_to_tl(None, [
        {"type": "MessageEntityTextUrl", "offset": 0, "length": 4, "url": "https://x"},
        {"type": "MessageEntityPre", "offset": 5, "length": 2, "language": "py"},
    ])
    assert built[0].url == "https://x"
    assert built[1].language == "py"


@pytest.mark.asyncio
async def test_dict_entities_empty_and_none():
    assert await dict_entities_to_tl(None, None) == []
    assert await dict_entities_to_tl(None, []) == []


@pytest.mark.asyncio
async def test_dict_entities_unknown_type_raises_never_silently_drops():
    with pytest.raises(TelegramAPIError, match="unknown entity type"):
        await dict_entities_to_tl(None, [{"type": "MessageEntityNonsense", "offset": 0, "length": 1}])


@pytest.mark.asyncio
async def test_dict_entities_missing_payload_raises():
    with pytest.raises(TelegramAPIError, match="missing url"):
        await dict_entities_to_tl(None, [{"type": "MessageEntityTextUrl", "offset": 0, "length": 4}])
    with pytest.raises(TelegramAPIError, match="missing document_id"):
        await dict_entities_to_tl(None, [{"type": "MessageEntityCustomEmoji", "offset": 0, "length": 2}])
    with pytest.raises(TelegramAPIError, match="invalid document_id"):
        await dict_entities_to_tl(None, [{"type": "MessageEntityCustomEmoji", "offset": 0, "length": 2, "document_id": "abc"}])
    with pytest.raises(TelegramAPIError, match="missing user_id"):
        await dict_entities_to_tl(None, [{"type": "MessageEntityMentionName", "offset": 0, "length": 1}])


@pytest.mark.asyncio
async def test_dict_entities_mention_name_resolves_through_target_client():
    bot = MagicMock()
    input_user = tl_types.InputUser(user_id=99, access_hash=1)
    bot.get_input_entity = AsyncMock(return_value=tl_types.InputPeerUser(user_id=99, access_hash=1))
    built = await dict_entities_to_tl(bot, [
        {"type": "MessageEntityMentionName", "offset": 0, "length": 3, "user_id": 99},
    ])
    assert type(built[0]) is tl_types.MessageEntityMentionName
    assert built[0].user_id == input_user
    bot.get_input_entity.assert_awaited_once_with(99)


# ── bridge: send_reconstructed ───────────────────────────────────────────────


class _FakeBot:
    """Mimics the helper-bot Telethon surface the bridge consumes."""

    def __init__(self) -> None:
        self.calls: list[dict[str, Any]] = []

    def is_connected(self) -> bool:
        return True

    async def send_message(self, peer, text, *, formatting_entities=None, reply_to=None):
        self.calls.append({
            "peer": peer, "text": text,
            "formatting_entities": formatting_entities, "reply_to": reply_to,
        })
        return None


class _FakeSelfClient:
    def __init__(self) -> None:
        self.resolved: list[Any] = []

    async def get_input_entity(self, chat_id):
        self.resolved.append(chat_id)
        return ("resolved-peer", chat_id)


@pytest.fixture
def connected_bot(monkeypatch):
    bot = _FakeBot()
    monkeypatch.setattr(helper_client, "_client", bot)
    monkeypatch.setattr(helper_client, "_bot_id", 777)
    return bot


@pytest.mark.asyncio
async def test_bridge_unavailable_raises_honestly(monkeypatch):
    monkeypatch.setattr(helper_client, "_client", None)
    with pytest.raises(TelegramAPIError, match="helper bot is not connected"):
        await send_reconstructed(_FakeSelfClient(), -100123, "hello")


@pytest.mark.asyncio
async def test_bridge_sends_through_helper_bot_to_same_destination(connected_bot):
    self_client = _FakeSelfClient()
    await send_reconstructed(
        self_client, -100123, "hello",
        entities=[{"type": "MessageEntityBold", "offset": 0, "length": 5}],
        reply_to_msg_id=42,
    )
    assert self_client.resolved == [-100123]  # peer resolved from the SELF client
    call = connected_bot.calls[0]
    assert call["peer"] == ("resolved-peer", -100123)  # bot sends to the SAME chat
    assert call["text"] == "hello"
    assert call["reply_to"] == 42
    assert type(call["formatting_entities"][0]) is tl_types.MessageEntityBold


@pytest.mark.asyncio
async def test_bridge_send_without_entities_uses_none_formatting(connected_bot):
    await send_reconstructed(_FakeSelfClient(), 555, "plain text")
    call = connected_bot.calls[0]
    assert call["formatting_entities"] is None
    assert call["reply_to"] is None


@pytest.mark.asyncio
async def test_bridge_refuses_empty_message(connected_bot):
    with pytest.raises(TelegramAPIError, match="empty message"):
        await send_reconstructed(_FakeSelfClient(), 1, "")


@pytest.mark.asyncio
async def test_bridge_unknown_entity_fails_before_any_send(connected_bot):
    with pytest.raises(TelegramAPIError, match="unknown entity type"):
        await send_reconstructed(_FakeSelfClient(), 1, "x", entities=[
            {"type": "MessageEntityNonsense", "offset": 0, "length": 1},
        ])
    assert connected_bot.calls == []  # nothing was sent


@pytest.mark.asyncio
async def test_bridge_wraps_generic_send_failure(connected_bot, monkeypatch):
    class _BrokenBot(_FakeBot):
        async def send_message(self, *args, **kwargs):
            raise RuntimeError("connection reset")
    monkeypatch.setattr(helper_client, "_client", _BrokenBot())
    with pytest.raises(TelegramAPIError, match="connection reset"):
        await send_reconstructed(_FakeSelfClient(), 1, "x")


@pytest.mark.asyncio
async def test_bridge_wraps_timeout_as_telegram_timeout(connected_bot, monkeypatch):
    class _SlowBot(_FakeBot):
        async def send_message(self, *args, **kwargs):
            raise asyncio.TimeoutError()
    monkeypatch.setattr(helper_client, "_client", _SlowBot())
    with pytest.raises(TelegramTimeoutError):
        await send_reconstructed(_FakeSelfClient(), 1, "x")


def test_bridge_id_and_availability_accessors(connected_bot):
    assert bridge_bot_id() == 777  # loop-prevention sender check surface
    assert bridge_available() is True


@pytest.mark.asyncio
async def test_bridge_conversion_trace_covers_custom_emoji_only(connected_bot, caplog):
    """The ONE added diagnostic line, and its blast radius on other callers.

    ``BRIDGE_ENTITY_CONVERTED`` is emitted only when a custom-emoji entity was
    actually reconstructed. An unrelated formatting-only caller keeps the exact
    behaviour it had: same peer resolution, same rebuilt entity, same result —
    and no new output at all.
    """
    import logging as _logging

    caplog.set_level(_logging.INFO, logger="backend.telegram_api.bridge")
    await send_reconstructed(
        _FakeSelfClient(), -100123, "hi 🏂",
        entities=[{
            "type": "MessageEntityCustomEmoji",
            "offset": 3, "length": 2, "document_id": 42,
        }],
    )
    converted = [
        r.getMessage() for r in caplog.records
        if "BRIDGE_ENTITY_CONVERTED" in r.getMessage()
    ]
    assert len(converted) == 1
    assert "type=MessageEntityCustomEmoji" in converted[0]
    assert "count=1" in converted[0]
    assert "3:2:42" in converted[0]  # offset:length:document_id

    caplog.clear()
    result = await send_reconstructed(
        _FakeSelfClient(), -100123, "hello",
        entities=[{"type": "MessageEntityBold", "offset": 0, "length": 5}],
    )
    assert [
        r.getMessage() for r in caplog.records
        if "BRIDGE_ENTITY_CONVERTED" in r.getMessage()
    ] == []
    assert result == {}
    assert type(connected_bot.calls[-1]["formatting_entities"][0]) is tl_types.MessageEntityBold
