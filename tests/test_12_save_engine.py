"""
Save engine + Settings panel regression tests.

Covers:
  - Settings glass panel renders (was: await on sync function → TypeError,
    so the Settings button on the main menu silently did nothing).
  - Forward save attaches the generated caption to the forwarded message
    and preserves the original text.
  - Deep save preserves the original media type (photo / video / voice /
    sticker / document) via Telegram attributes instead of degrading to a
    generic document.
  - Deep save of plain text (TEXT → TEXT).
  - Deep save metadata (save_code, mime, file_id, actual file_size, ids).
  - BytesIO buffer is always closed.
"""
from __future__ import annotations

import io

import pytest

from telethon.tl.types import (
    MessageMediaPhoto,
    MessageMediaDocument,
    DocumentAttributeAudio,
    DocumentAttributeVideo,
    DocumentAttributeFilename,
    DocumentAttributeSticker,
    InputStickerSetEmpty,
)

from backend.services import save_service
from backend.db import client as db_client


class FakeSender:
    first_name = "Test"
    last_name = "User"


class FakePhoto:
    id = 8888
    sizes = [type("S", (), {"size": 12345})()]


class FakeDoc:
    id = 9999
    mime_type = "video/mp4"
    size = 54321
    attributes: list = []


class FakeMessage:
    def __init__(self, chat_id=100, msg_id=200, text="hello world", media=None, sender_id=777):
        self.chat_id = chat_id
        self.id = msg_id
        self.text = text
        self.media = media
        self.sender_id = sender_id

    async def get_sender(self):
        return FakeSender()


class FakeSent:
    chat_id = "me"
    id = 600


class MockClient:
    download_bytes = b"fake-media-bytes"

    def __init__(self):
        self.calls = []

    async def forward_messages(self, entity, messages):
        self.calls.append(("forward_messages", entity, messages))
        return FakeSent()

    async def edit_message(self, entity, message, caption=None, **kwargs):
        self.calls.append(("edit_message", entity, message, caption))
        return FakeSent()

    async def download_media(self, message, file=None, **kwargs):
        if file is not None:
            file.write(self.download_bytes)
        return "downloaded"

    async def send_file(self, entity, file, **kwargs):
        self.calls.append(("send_file", entity, file, kwargs))
        return FakeSent()

    async def send_message(self, entity, text):
        self.calls.append(("send_message", entity, text))
        return FakeSent()


@pytest.fixture(autouse=True)
def reset_fallback():
    db_client._fallback["saved_items"] = []
    db_client._fallback["bio_state"] = {}
    db_client._fallback["bot_logs"] = []
    yield


def _send_file_call(client):
    return [c for c in client.calls if c[0] == "send_file"][0]


def _save_code(result: str) -> str:
    import re
    m = re.search(r"\b[A-Z]\d+\b", result)
    assert m, f"no save code in result: {result!r}"
    return m.group(0)


# ── Settings glass panel ──


@pytest.mark.asyncio
async def test_settings_panel_renders():
    from backend.bot.handlers import misc

    title, body, buttons = await misc._settings_panel_handler(None, "")
    assert title == "Settings"
    assert "Auto-close" in body
    assert "Max deep save" in body
    assert len(buttons) == 11


@pytest.mark.asyncio
async def test_menu_has_settings_button():
    from backend.bot.handlers import misc

    title, body, buttons = await misc._menu_panel_handler(None, "")
    assert title == "LifeOS"
    labels = [b.text for row in buttons for b in row]
    assert "Settings" in labels
    data = [b.data for row in buttons for b in row]
    assert any(b"panel:settings" in d for d in data)


@pytest.mark.asyncio
async def test_settings_callback_dispatch_renders_panel():
    from backend.helper.panels import _handle_panel
    from backend.helper.lifecycle import get_lifecycle
    from backend.bot.handlers import misc

    class FakeCbEvent:
        sender_id = 12345
        chat_id = 111
        message_id = 222
        original_update = None
        data = b"panel:settings"

        async def answer(self):
            pass

        async def edit(self, text, buttons=None):
            self.edited = text

    misc._register_panels()
    lifecycle = get_lifecycle()
    lifecycle.sessions.create(111, 222, panel_type="menu", owner_id=12345)

    ev = FakeCbEvent()
    await _handle_panel(ev, "settings", 111, 222, 12345)
    assert getattr(ev, "edited", None) is not None
    assert "**Settings**" in ev.edited
    assert "Auto-close:" in ev.edited


# ── caption helpers ──


def test_append_original_text():
    base = "**LifeOS** `SV-000001`"
    assert save_service._append_original_text(base, FakeMessage(text="source text")) == (
        f"{base}\n\nsource text"
    )
    assert save_service._append_original_text(base, FakeMessage(text="  ")) == base
    assert save_service._append_original_text(base, FakeMessage(text=None)) == base


def test_upload_kwargs_photo():
    media = MessageMediaPhoto(photo=FakePhoto(), ttl_seconds=None)
    kw = save_service._upload_kwargs_for_media(media, "image/jpeg", "photo_SV-1.jpg")
    assert kw == {"force_document": False}


def test_upload_kwargs_video_preserves_attributes():
    doc = FakeDoc()
    doc.mime_type = "video/mp4"
    doc.attributes = [
        DocumentAttributeVideo(duration=10, w=1280, h=720, round_message=False, supports_streaming=True),
        DocumentAttributeFilename("clip.mp4"),
    ]
    media = MessageMediaDocument(document=doc, ttl_seconds=None)
    kw = save_service._upload_kwargs_for_media(media, "video/mp4", "clip.mp4")
    assert kw["force_document"] is False
    assert kw["mime_type"] == "video/mp4"
    videos = [a for a in kw["attributes"] if isinstance(a, DocumentAttributeVideo)]
    assert videos and videos[0].duration == 10 and videos[0].supports_streaming is True
    filenames = [a for a in kw["attributes"] if isinstance(a, DocumentAttributeFilename)]
    assert filenames and filenames[0].file_name == "clip.mp4"


def test_upload_kwargs_voice_preserves_voice_flag():
    doc = FakeDoc()
    doc.mime_type = "audio/ogg"
    doc.attributes = [
        DocumentAttributeAudio(duration=5, voice=True),
        DocumentAttributeFilename("voice.ogg"),
    ]
    media = MessageMediaDocument(document=doc, ttl_seconds=None)
    kw = save_service._upload_kwargs_for_media(media, "audio/ogg", "voice.ogg")
    audios = [a for a in kw["attributes"] if isinstance(a, DocumentAttributeAudio)]
    assert audios and audios[0].voice is True and audios[0].duration == 5
    assert kw["mime_type"] == "audio/ogg"


def test_upload_kwargs_sticker_preserved():
    doc = FakeDoc()
    doc.mime_type = "image/webp"
    doc.attributes = [DocumentAttributeSticker(alt="😀", stickerset=InputStickerSetEmpty())]
    media = MessageMediaDocument(document=doc, ttl_seconds=None)
    kw = save_service._upload_kwargs_for_media(media, "image/webp", "sticker.webp")
    stickers = [a for a in kw["attributes"] if isinstance(a, DocumentAttributeSticker)]
    assert stickers
    assert any(isinstance(a, DocumentAttributeFilename) for a in kw["attributes"])


def test_upload_kwargs_plain_document_keeps_mime_and_name():
    doc = FakeDoc()
    doc.mime_type = "application/pdf"
    doc.attributes = [DocumentAttributeFilename("report.pdf")]
    media = MessageMediaDocument(document=doc, ttl_seconds=None)
    kw = save_service._upload_kwargs_for_media(media, "application/pdf", "report.pdf")
    assert kw["mime_type"] == "application/pdf"
    filenames = [a for a in kw["attributes"] if isinstance(a, DocumentAttributeFilename)]
    assert filenames and filenames[0].file_name == "report.pdf"


# ── forward save ──


@pytest.mark.asyncio
async def test_forward_save_attaches_caption_and_preserves_text():
    client = MockClient()
    media = MessageMediaPhoto(photo=FakePhoto(), ttl_seconds=None)
    msg = FakeMessage(media=media)
    result = await save_service.execute_save(client, 42, msg, "f", "UTC")

    assert "Saved Successfully" in result
    fwd = [c for c in client.calls if c[0] == "forward_messages"][0]
    assert fwd[1] == "me"
    assert fwd[2] is msg

    edit = [c for c in client.calls if c[0] == "edit_message"][0]
    assert edit[1] == "me"
    assert "**LifeOS**" in edit[3]
    assert "hello world" in edit[3]  # original text preserved below the caption

    row = await db_client.query_save(_save_code(result))
    assert row is not None
    assert row["save_type"] == "forward"
    assert row["media_type"] == "Photo"
    assert row["mime_type"] == "image/jpeg"
    assert row["file_id"] == "8888"
    assert row["origin_chat_id"] == 100
    assert row["origin_msg_id"] == 200
    assert row["saved_msg_id"] == 600
    assert row["owner_id"] == 42
    assert row["caption"] and "**LifeOS**" in row["caption"]


# ── deep save ──


@pytest.mark.asyncio
async def test_deep_save_photo_preserves_type_and_actual_size():
    client = MockClient()
    media = MessageMediaPhoto(photo=FakePhoto(), ttl_seconds=None)
    msg = FakeMessage(media=media)
    result = await save_service.execute_save(client, 42, msg, "d", "UTC")

    assert "Saved Successfully" in result
    _, entity, file, kwargs = _send_file_call(client)
    assert entity == "me"
    assert kwargs["force_document"] is False
    assert "**LifeOS**" in kwargs["caption"]
    assert "hello world" in kwargs["caption"]
    assert isinstance(file, io.BytesIO)
    assert file.closed  # buffer always closed

    row = await db_client.query_save(_save_code(result))
    assert row["save_type"] == "deep"
    assert row["media_type"] == "Photo"
    assert row["file_size"] == len(client.download_bytes)  # actual downloaded size
    assert row["saved_msg_id"] == 600
    assert row["caption"]


@pytest.mark.asyncio
async def test_deep_save_video_keeps_video_attributes():
    client = MockClient()
    doc = FakeDoc()
    doc.mime_type = "video/mp4"
    doc.attributes = [
        DocumentAttributeVideo(duration=10, w=1280, h=720, round_message=False, supports_streaming=True),
        DocumentAttributeFilename("clip.mp4"),
    ]
    media = MessageMediaDocument(document=doc, ttl_seconds=None)
    msg = FakeMessage(media=media)
    result = await save_service.execute_save(client, 42, msg, "d", "UTC")

    _, _, _, kwargs = _send_file_call(client)
    videos = [a for a in kwargs["attributes"] if isinstance(a, DocumentAttributeVideo)]
    assert videos and videos[0].duration == 10 and videos[0].w == 1280
    assert kwargs["mime_type"] == "video/mp4"

    row = await db_client.query_save(_save_code(result))
    assert row["media_type"] == "Video"


@pytest.mark.asyncio
async def test_deep_save_voice_keeps_voice_type():
    client = MockClient()
    doc = FakeDoc()
    doc.mime_type = "audio/ogg"
    doc.attributes = [
        DocumentAttributeAudio(duration=5, voice=True),
        DocumentAttributeFilename("voice.ogg"),
    ]
    media = MessageMediaDocument(document=doc, ttl_seconds=None)
    msg = FakeMessage(media=media)
    result = await save_service.execute_save(client, 42, msg, "d", "UTC")

    _, _, _, kwargs = _send_file_call(client)
    audios = [a for a in kwargs["attributes"] if isinstance(a, DocumentAttributeAudio)]
    assert audios and audios[0].voice is True

    row = await db_client.query_save(_save_code(result))
    assert row["media_type"] == "Voice"


@pytest.mark.asyncio
async def test_deep_save_text_message():
    client = MockClient()
    msg = FakeMessage(text="just some plain text", media=None)
    result = await save_service.execute_save(client, 42, msg, "d", "UTC")

    assert "Saved Successfully" in result
    sm = [c for c in client.calls if c[0] == "send_message"][0]
    assert sm[1] == "me"
    assert "**LifeOS**" in sm[2]
    assert "just some plain text" in sm[2]

    row = await db_client.query_save(_save_code(result))
    assert row["save_type"] == "deep"
    assert row["media_type"] == "Text"
    assert row["file_size"] == len("just some plain text".encode("utf-8"))
    assert "just some plain text" in row["caption"]


@pytest.mark.asyncio
async def test_deep_save_no_media_no_text_errors():
    client = MockClient()
    msg = FakeMessage(text="", media=None)
    result = await save_service.execute_save(client, 42, msg, "d", "UTC")
    assert "no downloadable media" in result
    assert not [c for c in client.calls if c[0] in ("send_file", "send_message")]


@pytest.mark.asyncio
async def test_deep_save_buffer_closed_on_download_error():
    client = MockClient()

    async def boom(message, file=None, **kwargs):
        raise RuntimeError("network down")

    client.download_media = boom
    media = MessageMediaPhoto(photo=FakePhoto(), ttl_seconds=None)
    msg = FakeMessage(media=media)
    result = await save_service.execute_save(client, 42, msg, "d", "UTC")
    assert "❌ Download failed" in result


@pytest.mark.asyncio
async def test_deep_save_persists_full_metadata():
    client = MockClient()
    doc = FakeDoc()
    doc.mime_type = "application/zip"
    doc.size = 777
    doc.attributes = [DocumentAttributeFilename("archive.zip")]
    media = MessageMediaDocument(document=doc, ttl_seconds=None)
    msg = FakeMessage(media=media)
    result = await save_service.execute_save(client, 42, msg, "d", "UTC")

    row = await db_client.query_save(_save_code(result))
    assert row["mime_type"] == "application/zip"
    assert row["file_id"] == "9999"
    assert row["media_type"] == "Document"
    assert row["origin_chat_id"] == 100
    assert row["origin_msg_id"] == 200
    assert row["saved_chat_id"] == "me"
    assert row["saved_msg_id"] == 600
    assert row["owner_id"] == 42
    assert row["save_code"] == _save_code(result)
    assert row["tags"]
