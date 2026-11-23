"""Every governed upload reserves BEFORE transmission.

A reservation failure (or an unimportable governor) must mean the Telegram RPC
is NEVER called. The fakes record the real RPCs, so "no upload happened" is
asserted against the transfer call itself, not against a constant.
"""
from __future__ import annotations

import sys
from types import SimpleNamespace

import pytest

from backend.services import outbound_bandwidth_service as bw
from backend.services import gemini_media_engine as gemini
from backend.telegram_api import media as media_api


class RecordingClient:
    def __init__(self):
        self.calls = []

    async def send_file(self, entity, file, **kwargs):
        self.calls.append(("send_file", entity, kwargs))
        return SimpleNamespace(id=1, chat_id=entity, message="", date=None)

    async def upload_file(self, file, **kwargs):
        self.calls.append(("upload_file", file, kwargs))
        return SimpleNamespace(id=1, parts=1, name="x", md5_checksum="")


@pytest.fixture
def reservations(monkeypatch):
    recorded = []

    async def _record(category, byte_count, **kwargs):
        recorded.append((category, int(byte_count)))
        return {"category": category, "reserved_bytes": int(byte_count)}

    monkeypatch.setattr(bw, "reserve_outbound_bytes", _record)
    return recorded


@pytest.fixture
def denied(monkeypatch):
    async def _deny(category, byte_count, **kwargs):
        raise bw.OutboundBandwidthUnavailableError("accounting unavailable")

    monkeypatch.setattr(bw, "reserve_outbound_bytes", _deny)


@pytest.fixture
def broken_governor_import(monkeypatch):
    """Make ``from backend.services import outbound_bandwidth_service`` fail."""
    import backend.services as services_pkg

    monkeypatch.setitem(
        sys.modules, "backend.services.outbound_bandwidth_service", None
    )
    monkeypatch.delattr(services_pkg, "outbound_bandwidth_service")


# ── voice notes (backend/telegram_api/media.py) ────────────────────────────


@pytest.mark.asyncio
async def test_a_voice_note_is_reserved_before_the_upload(reservations):
    client = RecordingClient()

    await media_api.send_voice(client, 777, b"a" * 4096)

    assert reservations == [("telegram_media", 4096)]
    assert [call[0] for call in client.calls] == ["send_file"]


@pytest.mark.asyncio
async def test_a_voice_note_is_not_sent_when_the_reservation_is_denied(denied):
    client = RecordingClient()

    with pytest.raises(media_api.TelegramAPIError):
        await media_api.send_voice(client, 777, b"a" * 4096)

    assert client.calls == []


@pytest.mark.asyncio
async def test_a_voice_note_is_not_sent_when_the_governor_cannot_be_imported(
    broken_governor_import,
):
    client = RecordingClient()

    with pytest.raises(media_api.TelegramAPIError):
        await media_api.send_voice(client, 777, b"a" * 4096)

    assert client.calls == []


# ── AI result attachments (backend/ai/tools/delivery.py) ───────────────────


@pytest.mark.asyncio
async def test_an_ai_attachment_is_reserved_before_the_upload(reservations):
    from backend.ai.tools import delivery

    sender = RecordingClient()

    await delivery._send_text_attachment(sender, 42, b"x" * 5000)

    assert reservations == [("telegram_media", 5000)]
    assert [call[0] for call in sender.calls] == ["send_file"]


@pytest.mark.asyncio
async def test_an_ai_attachment_is_not_uploaded_when_the_reservation_is_denied(denied):
    from backend.ai.tools import delivery

    sender = RecordingClient()

    with pytest.raises(bw.OutboundBandwidthUnavailableError):
        await delivery._send_text_attachment(sender, 42, b"x" * 5000)

    assert sender.calls == []


@pytest.mark.asyncio
async def test_an_ai_attachment_is_not_uploaded_when_the_governor_cannot_be_imported(
    broken_governor_import,
):
    from backend.ai.tools import delivery

    sender = RecordingClient()

    with pytest.raises(ImportError):
        await delivery._send_text_attachment(sender, 42, b"x" * 5000)

    assert sender.calls == []


class FakeEvent:
    chat_id = 42
    input_chat = 42

    def __init__(self, client):
        self.client = client
        self.edits = []
        self.replies = []

    async def edit(self, text, **kwargs):
        self.edits.append(text)

    async def reply(self, text, **kwargs):
        self.replies.append(text)


@pytest.mark.asyncio
async def test_a_denied_attachment_still_delivers_the_text_without_any_upload(denied):
    from backend.ai.tools import delivery

    client = RecordingClient()
    event = FakeEvent(client)

    result = await delivery.deliver_single_message(
        event, "question", "word " * (delivery.SAFE_LIMIT * 2),
    )

    assert result.success is True
    assert client.calls == []
    assert event.edits or event.replies


@pytest.mark.asyncio
async def test_ordinary_text_delivery_never_touches_accounting_or_upload(
    reservations,
):
    from backend.ai.tools import delivery

    client = RecordingClient()
    event = FakeEvent(client)

    result = await delivery.deliver_response(event, "question", "a short answer")

    assert result.success is True
    assert reservations == []
    assert client.calls == []
    assert len(event.edits) == 1
    assert event.edits[0].startswith("a short answer")


# ── Gemini media payloads (backend/services/gemini_media_engine.py) ────────


def test_a_missing_governor_denies_the_gemini_payload(broken_governor_import):
    with pytest.raises(ImportError):
        gemini._reserve_gemini_bandwidth("gemini_ocr", 100)


def test_a_governor_without_the_sync_entry_point_denies(monkeypatch):
    monkeypatch.delattr(bw, "reserve_outbound_bytes_sync", raising=False)

    with pytest.raises(RuntimeError):
        gemini._reserve_gemini_bandwidth("gemini_ocr", 100)


def test_the_gemini_reservation_is_a_conservative_upper_bound(monkeypatch):
    recorded = []
    monkeypatch.setattr(
        bw,
        "reserve_outbound_bytes_sync",
        lambda category, byte_count: recorded.append((category, byte_count)) or {},
    )

    gemini._reserve_gemini_bandwidth("gemini_ocr", 1000)

    assert recorded[0][0] == "gemini_ocr"
    assert recorded[0][1] >= 1000


def test_ocr_never_reaches_the_provider_when_the_reservation_is_denied(monkeypatch):
    engine = gemini.GeminiMediaEngine("test-key")
    provider_calls = []
    monkeypatch.setattr(
        type(engine), "_generate",
        lambda self, *args, **kwargs: provider_calls.append(args) or "text",
    )

    def _deny(category, byte_count):
        raise bw.OutboundBandwidthUnavailableError("accounting unavailable")

    monkeypatch.setattr(bw, "reserve_outbound_bytes_sync", _deny)

    with pytest.raises(bw.OutboundBandwidthUnavailableError):
        engine._run(gemini._OCR_KIND, b"payload", "read this", 5.0)

    assert provider_calls == []


def test_ocr_behavior_is_unchanged_when_the_reservation_succeeds(monkeypatch):
    engine = gemini.GeminiMediaEngine("test-key")
    recorded = []
    monkeypatch.setattr(
        bw,
        "reserve_outbound_bytes_sync",
        lambda category, byte_count: recorded.append((category, byte_count)) or {},
    )
    monkeypatch.setattr(gemini, "gemini_mime_type", lambda payload: "image/png")
    monkeypatch.setattr(type(engine), "_generate", lambda self, *a, **k: "extracted")

    assert engine._run(gemini._OCR_KIND, b"payload", "read this", 5.0) == "extracted"
    assert [entry[0] for entry in recorded] == ["gemini_ocr"]
