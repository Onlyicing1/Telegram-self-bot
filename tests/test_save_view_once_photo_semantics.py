"""Deep Save's ephemeral-media gate, verified against Telegram's own wire schema.

The production report behind this file: a saved ``MessageMediaPhoto`` was
refused as view-once before any download, upload or row, and the owner
remembered that the same item used to save. So the contracts pinned here are
the PROTOCOL's, not the implementation's:

  * ``messageMediaPhoto#e216eb63`` carries ``photo:flags.0`` and
    ``ttl_seconds:flags.2``; the pinned Telethon parses those same flag bits,
    so a value the app classifies is a value Telegram put on the wire;
  * Telegram documents ``ttl_seconds`` as: ``0`` disables self-destruction,
    ``0x7FFFFFFF`` self-destructs immediately after playback (view-once), and
    every other positive value is expiring media (the timer starts on open);
  * therefore an ordinary photo never carries the sentinel, a photo that DOES
    carry it is genuinely view-once and is refused before any transfer, and
    the refusal is the documented LifeOS policy — not a misread of a normal
    photo, and never a value the app invented;
  * link and reply Save differ only in target resolution: both hand the
    resolved message to the ONE ``execute_save`` pipeline.

No network, no Telegram session, no Supabase: the wire fixtures are parsed by
the pinned Telethon itself, and the client fakes are the ones the other Save
suites use.
"""
from __future__ import annotations

import logging
import struct
import sys

import pytest
from telethon.extensions.binaryreader import BinaryReader
from telethon.tl.types import MessageMediaPhoto, PhotoEmpty

from backend.bot.handlers import save as save_handler
from backend.db import client as db_client
from backend.helper import inline_engine
from backend.runtime import save_trace
from backend.services import save_service
from backend.services.save_service import (
    EPHEMERAL_EXPIRING,
    EPHEMERAL_VIEW_ONCE,
    VIEW_ONCE_TTL_SECONDS,
    SaveOutcome,
)

from tests.test_save_album_view_once_progress import (
    FakeClient,
    FakeMessage,
    FakePhoto,
)
from tests.test_save_live_status_and_private_link import (
    MSG_ID,
    OWNER,
    PEER,
    PeerResolutionClient,
)

CHAT = -1001234567890
PANEL_CHAT = 777
PANEL_MSG = 5001
LINK = f"tg://openmessage?user_id={PEER}&message_id={MSG_ID}"

#: ``messageMediaPhoto#e216eb63`` (Telegram layer 224+, where Live Photos were
#: added) — the fields this gate reads, with their documented flag bits:
#:     spoiler:flags.3?true live_photo:flags.4?true photo:flags.0?Photo
#:     ttl_seconds:flags.2?int     video:flags.4?Document
PHOTO_CONSTRUCTOR_ID = 0xE216EB63
FLAG_PHOTO = 1 << 0
FLAG_TTL_SECONDS = 1 << 2
FLAG_SPOILER = 1 << 3

_UNSET = object()


# ── wire fixtures (parsed by the pinned Telethon, never hand-built objects) ──


def _photo_wire(*, ttl=_UNSET, spoiler: bool = False) -> bytes:
    """Real ``messageMediaPhoto`` bytes with exactly the documented flags set."""
    assert MessageMediaPhoto.CONSTRUCTOR_ID == PHOTO_CONSTRUCTOR_ID
    flags = FLAG_PHOTO
    if ttl is not _UNSET:
        flags |= FLAG_TTL_SECONDS
    if spoiler:
        flags |= FLAG_SPOILER
    body = struct.pack("<I", flags) + PhotoEmpty(id=987654321)._bytes()
    if ttl is not _UNSET:
        body += struct.pack("<i", int(ttl))
    return struct.pack("<I", PHOTO_CONSTRUCTOR_ID) + body


def _parsed_photo(*, ttl=_UNSET, spoiler: bool = False) -> MessageMediaPhoto:
    parsed = BinaryReader(_photo_wire(ttl=ttl, spoiler=spoiler)).tgread_object()
    assert isinstance(parsed, MessageMediaPhoto)
    return parsed


def _photo_media(ttl, *, size: int = 2048, photo_id: int = 4321) -> MessageMediaPhoto:
    """A photo media object shaped exactly like the ones ``get_messages`` yields."""
    return MessageMediaPhoto(photo=FakePhoto(photo_id, size), ttl_seconds=ttl)


def _message(msg_id: int, media, *, text: str = "", chat_id: int = CHAT) -> FakeMessage:
    return FakeMessage(
        msg_id, chat_id=chat_id, media=media, text=text, download_size=2048
    )


@pytest.fixture(autouse=True)
def clean_state():
    db_client._fallback["saved_items"] = []
    db_client._fallback["bot_logs"] = []
    save_trace.reset_state()
    inline_engine.set_self_client(None)
    inline_engine.set_owner_id(0)
    yield
    save_trace.reset_state()
    inline_engine.set_self_client(None)
    inline_engine.set_owner_id(0)


def _rows() -> list[dict]:
    return db_client._fallback["saved_items"]


def _records(caplog) -> list[str]:
    return [
        record.getMessage()
        for record in caplog.records
        if record.getMessage().startswith(save_trace.TRACE_PREFIX)
    ]


def _fields(caplog) -> list[dict[str, str]]:
    parsed = []
    for record in _records(caplog):
        fields: dict[str, str] = {}
        for token in record[len(save_trace.TRACE_PREFIX):].strip().split(" "):
            key, _, value = token.partition("=")
            if key and value:
                fields[key] = value
        parsed.append(fields)
    return parsed


def _named(caplog, name: str) -> list[dict[str, str]]:
    return [event for event in _fields(caplog) if event.get("name") == name]


# ── 1. what the pinned Telethon actually parses out of the wire ─────────────


def test_pinned_telethon_parses_the_documented_photo_ttl_flags():
    """The sentinel must come from Telegram's schema, not from our assumptions."""
    assert MessageMediaPhoto.CONSTRUCTOR_ID == PHOTO_CONSTRUCTOR_ID
    assert _parsed_photo().ttl_seconds is None
    assert _parsed_photo(ttl=0).ttl_seconds == 0
    assert _parsed_photo(ttl=30).ttl_seconds == 30
    assert _parsed_photo(ttl=VIEW_ONCE_TTL_SECONDS).ttl_seconds == VIEW_ONCE_TTL_SECONDS
    # A spoiler is an independent flag: it never manufactures a TTL.
    assert _parsed_photo(spoiler=True).ttl_seconds is None
    assert (
        _parsed_photo(ttl=VIEW_ONCE_TTL_SECONDS, spoiler=True).ttl_seconds
        == VIEW_ONCE_TTL_SECONDS
    )


def test_wire_parsed_ttl_values_map_to_the_documented_categories():
    """Telegram's documented split: 0 = off, 0x7FFFFFFF = view-once, else expiring."""
    assert save_service.media_ttl_seconds(_parsed_photo()) is None
    assert save_service.media_ttl_seconds(_parsed_photo(ttl=0)) is None

    assert save_service.ephemeral_media_kind(_parsed_photo()) is None
    assert save_service.ephemeral_media_kind(_parsed_photo(ttl=0)) is None

    assert save_service.ephemeral_media_kind(_parsed_photo(ttl=30)) == EPHEMERAL_EXPIRING
    assert (
        save_service.ephemeral_media_kind(_parsed_photo(ttl=VIEW_ONCE_TTL_SECONDS - 1))
        == EPHEMERAL_EXPIRING
    )
    assert (
        save_service.ephemeral_media_kind(_parsed_photo(ttl=VIEW_ONCE_TTL_SECONDS))
        == EPHEMERAL_VIEW_ONCE
    )


# ── 2. the production case: a view-once PHOTO is refused before any transfer ──


@pytest.mark.asyncio
async def test_view_once_photo_is_refused_before_download_upload_and_persist(
    monkeypatch, caplog
):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    caplog.set_level(logging.INFO)

    secret = _message(
        MSG_ID, _parsed_photo(ttl=VIEW_ONCE_TTL_SECONDS), text="one time only"
    )
    client = FakeClient([secret])

    result = await save_service.execute_save(client, OWNER, secret, "UTC")

    assert getattr(result, "saved", False) is False
    assert "view-once" in result
    # Nothing moved and nothing was recorded: the guard is pre-transfer.
    assert client.calls == []
    assert client.forward_calls == []
    assert _rows() == []

    media = _named(caplog, "media_present")[0]
    assert media["media_class"] == "MessageMediaPhoto"
    guard = _named(caplog, "ephemeral_guard")[0]
    assert guard["matched"] == "view_once"
    assert guard["ttl"] == "view_once_sentinel"

    not_reached = {
        event["stage"]
        for event in _fields(caplog)
        if event.get("kind") == save_trace.NOT_REACHED
    }
    assert not_reached == {"download", "upload", "persist"}
    assert all(
        event["reason"] == "ephemeral_refusal"
        for event in _fields(caplog)
        if event.get("kind") == save_trace.NOT_REACHED
    )
    assert sys.gettrace() is None


@pytest.mark.asyncio
async def test_the_view_once_refusal_never_leaks_content_or_ids(monkeypatch, caplog):
    monkeypatch.setenv(save_trace.ENV_FLAG, "1")
    caplog.set_level(logging.INFO)

    secret = _message(
        MSG_ID, _parsed_photo(ttl=VIEW_ONCE_TTL_SECONDS), text="private caption text"
    )
    client = FakeClient([secret])

    await save_service.execute_save(client, OWNER, secret, "UTC")

    records = _records(caplog)
    assert records
    assert not any("private caption text" in record for record in records)
    assert not any(str(MSG_ID) in record for record in records)
    assert not any(str(CHAT) in record for record in records)
    assert not any(str(PEER) in record for record in records)


# ── 3. ordinary photos keep saving (missing TTL and Telegram's TTL 0) ────────


@pytest.mark.asyncio
async def test_ordinary_photo_without_a_ttl_is_saved():
    target = _message(MSG_ID, _photo_media(None), text="hello")
    client = FakeClient([target])

    result = await save_service.execute_save(client, OWNER, target, "UTC")

    assert getattr(result, "saved", False) is True, result
    assert len(client.download_calls) == 1
    assert len(client.send_calls) == 1
    assert client.forward_calls == []
    assert len(_rows()) == 1
    assert _rows()[0]["media_type"] == "Photo"


@pytest.mark.asyncio
async def test_ordinary_photo_with_zero_ttl_is_saved():
    """Telegram documents ``ttl_seconds = 0`` as "disable self-destruction"."""
    target = _message(MSG_ID, _parsed_photo(ttl=0), text="hello")
    assert save_service.ephemeral_media_kind(target.media) is None
    client = FakeClient([target])

    result = await save_service.execute_save(client, OWNER, target, "UTC")

    assert getattr(result, "saved", False) is True, result
    assert len(client.download_calls) == 1
    assert len(client.send_calls) == 1
    assert len(_rows()) == 1


@pytest.mark.asyncio
async def test_photo_with_a_finite_ttl_follows_the_expiring_policy():
    target = _message(MSG_ID, _parsed_photo(ttl=30), text="timer")
    assert save_service.ephemeral_media_kind(target.media) == EPHEMERAL_EXPIRING
    client = FakeClient([target])

    result = await save_service.execute_save(client, OWNER, target, "UTC")

    assert getattr(result, "saved", False) is False
    assert "30s" in result
    assert client.calls == []
    assert _rows() == []


# ── 4. the classification reads an exact TTL, never "a big number" ──────────


def test_only_the_exact_sentinel_is_view_once():
    class NoTtlField:
        pass

    class _Media:
        def __init__(self, ttl):
            self.ttl_seconds = ttl

    assert save_service.ephemeral_media_kind(NoTtlField()) is None
    assert save_service.ephemeral_media_kind(None) is None
    assert save_service.ephemeral_media_kind(_Media(None)) is None
    assert save_service.ephemeral_media_kind(_Media(0)) is None
    assert save_service.ephemeral_media_kind(_Media(-1)) is None
    assert save_service.ephemeral_media_kind(_Media(True)) is None
    assert save_service.ephemeral_media_kind(_Media("2147483647")) is None
    assert save_service.ephemeral_media_kind(_Media(2147483648)) == EPHEMERAL_EXPIRING
    assert save_service.ephemeral_media_kind(_Media(VIEW_ONCE_TTL_SECONDS)) == EPHEMERAL_VIEW_ONCE


# ── 5. link and reply are target resolution only; ONE pipeline executes ─────


@pytest.mark.asyncio
async def test_link_and_reply_paths_both_use_the_one_execute_save_pipeline(monkeypatch):
    # Reply Mode resolves the zone from the runtime env; pin it for the assertion.
    monkeypatch.setenv("TZ", "UTC")
    calls: list[dict] = []

    async def spy(client, owner_id, message, tz_str, **kwargs):
        calls.append(
            {
                "owner": owner_id,
                "chat": getattr(message, "chat_id", None),
                "msg": getattr(message, "id", None),
                "tz": tz_str,
            }
        )
        return SaveOutcome("saved", saved=True)

    monkeypatch.setattr(save_service, "execute_save", spy)

    # Link path: the resolved link target is the message handed to the pipeline.
    link_target = _message(MSG_ID, _photo_media(None), chat_id=PEER)
    link_client = PeerResolutionClient([link_target], global_by_id={MSG_ID: link_target})
    await save_service.execute_link_save(link_client, OWNER, LINK, "UTC")

    # Reply path: the replied-to message (never the trigger) is handed over.
    target = _message(MSG_ID, _parsed_photo(), text="hello", chat_id=PANEL_CHAT)
    trigger = FakeMessage(
        MSG_ID + 1, chat_id=PANEL_CHAT, text="save", reply_to_msg_id=MSG_ID
    )
    reply_client = FakeClient([target, trigger])
    inline_engine.set_self_client(reply_client)
    inline_engine.set_owner_id(OWNER)
    await save_handler._save_reply_wait_handler(
        "save", PANEL_CHAT, MSG_ID + 1, PANEL_CHAT, PANEL_MSG
    )

    assert len(calls) == 2
    assert calls[0]["msg"] == MSG_ID and calls[0]["chat"] == PEER
    assert calls[1]["msg"] == MSG_ID and calls[1]["chat"] == PANEL_CHAT
    assert all(call["owner"] == OWNER and call["tz"] == "UTC" for call in calls)
    # Neither path ever substitutes a forward for the pipeline.
    assert link_client.forward_calls == []
    assert reply_client.forward_calls == []
