"""
Save engine + Settings panel + Glass Reply Mode regression tests.

Covers:
  - Settings glass panel renders (was: await on sync function → TypeError,
    so the Settings button on the main menu silently did nothing).
  - Deep Save is the ONLY save method and is a TRUE re-upload:
    download → temporary file → send_file. It never calls forward_messages,
    preserves the original media type (photo / video / voice / sticker /
    document) via Telegram attributes, cleans up its temporary file on
    success AND failure, and handles plain text (TEXT → send_message).
  - Deep Save metadata (save_code, mime, actual file_size, ids) refers to
    the NEWLY uploaded message, not the source.
  - The Glass UI Reply Mode flow: `.menu → Save → Deep Save → Reply Mode`
    sets a pending state, and the next outgoing reply resolves its
    ``reply_to_msg_id`` to the exact target message before saving it.
"""
from __future__ import annotations

import os
import tempfile

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

    def __init__(self, media=None):
        self.media = media


class MockClient:
    download_bytes = b"fake-media-bytes"

    def __init__(self, sent_media=None):
        self.calls = []
        self._sent_media = sent_media

    async def forward_messages(self, entity, messages):
        self.calls.append(("forward_messages", entity, messages))
        return FakeSent()

    async def download_media(self, message, file=None, **kwargs):
        self.calls.append(("download_media", message, file))
        if file is not None:
            if isinstance(file, (str, os.PathLike)):
                os.makedirs(os.path.dirname(file), exist_ok=True)
                with open(file, "wb") as fh:
                    fh.write(self.download_bytes)
            else:
                file.write(self.download_bytes)
        return "downloaded"

    async def send_file(self, entity, file, **kwargs):
        self.calls.append(("send_file", entity, file, kwargs))
        return FakeSent(media=self._sent_media)

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


def _assert_no_leftover_temp_dirs():
    leftovers = [
        d for d in os.listdir(tempfile.gettempdir())
        if d.startswith("lifeos_dl_")
    ]
    assert leftovers == [], f"leftover temp dirs: {leftovers}"


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
    # 10 legacy rows + the 🔤 Font row added by the Glass UI font setting.
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
    base = "📦 S0001 · DEEP"
    assert save_service._append_original_text(base, FakeMessage(text="source text")) == (
        f"{base}\n\nsource text"
    )
    assert save_service._append_original_text(base, FakeMessage(text="  ")) == base
    assert save_service._append_original_text(base, FakeMessage(text=None)) == base


def test_build_caption_is_compact():
    caption = save_service.build_caption(
        save_code="S0001",
        sender="Test User",
        chat_id=100,
        msg_id=200,
        dt=__import__("datetime").datetime(2026, 8, 18, 15, 30),
        media_type="Photo",
        mime="image/jpeg",
        file_size=12345,
        file_name="photo_S0001.jpg",
        tags=["#saved"],
    )
    assert "S0001 · DEEP" in caption
    assert "Test User" in caption
    assert "100/200" in caption
    assert "Photo" in caption
    assert "#saved" in caption
    # No verbose "Original Message Information:" style blocks.
    assert "Original Message Information" not in caption


def test_upload_kwargs_photo():
    media = MessageMediaPhoto(photo=FakePhoto(), ttl_seconds=None)
    kw = save_service._upload_kwargs_for_media(media, "image/jpeg", "photo_S0001.jpg")
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


# ── deep save (the only save method) ──


@pytest.mark.asyncio
async def test_deep_save_photo_preserves_type_and_actual_size():
    client = MockClient()
    media = MessageMediaPhoto(photo=FakePhoto(), ttl_seconds=None)
    msg = FakeMessage(media=media)
    result = await save_service.execute_save(client, 42, msg, "UTC")

    assert "Saved Successfully" in result
    _, entity, file, kwargs = _send_file_call(client)
    assert entity == "me"
    assert kwargs["force_document"] is False
    assert "DEEP" in kwargs["caption"]
    assert "hello world" in kwargs["caption"]
    assert isinstance(file, str)  # temporary file path, not an in-memory buffer
    assert not os.path.exists(file)  # temp file removed after upload
    _assert_no_leftover_temp_dirs()

    row = await db_client.query_save(_save_code(result))
    assert row["save_type"] == "deep"
    assert row["media_type"] == "Photo"
    assert row["file_size"] == len(client.download_bytes)  # actual downloaded size
    assert row["saved_msg_id"] == 600
    assert row["caption"]


@pytest.mark.asyncio
async def test_deep_save_never_forwards():
    client = MockClient()
    media = MessageMediaPhoto(photo=FakePhoto(), ttl_seconds=None)
    msg = FakeMessage(media=media)
    result = await save_service.execute_save(client, 42, msg, "UTC")

    assert "Saved Successfully" in result
    # Deep Save must be a genuine download → upload, never a forward.
    assert not [c for c in client.calls if c[0] == "forward_messages"]
    assert [c for c in client.calls if c[0] == "download_media"]
    assert [c for c in client.calls if c[0] == "send_file"]


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
    result = await save_service.execute_save(client, 42, msg, "UTC")

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
    result = await save_service.execute_save(client, 42, msg, "UTC")

    _, _, _, kwargs = _send_file_call(client)
    audios = [a for a in kwargs["attributes"] if isinstance(a, DocumentAttributeAudio)]
    assert audios and audios[0].voice is True

    row = await db_client.query_save(_save_code(result))
    assert row["media_type"] == "Voice"


@pytest.mark.asyncio
async def test_deep_save_text_message():
    client = MockClient()
    msg = FakeMessage(text="just some plain text", media=None)
    result = await save_service.execute_save(client, 42, msg, "UTC")

    assert "Saved Successfully" in result
    sm = [c for c in client.calls if c[0] == "send_message"][0]
    assert sm[1] == "me"
    assert "DEEP" in sm[2]
    assert "just some plain text" in sm[2]
    assert not [c for c in client.calls if c[0] == "forward_messages"]

    row = await db_client.query_save(_save_code(result))
    assert row["save_type"] == "deep"
    assert row["media_type"] == "Text"
    assert row["file_size"] == len("just some plain text".encode("utf-8"))
    assert "just some plain text" in row["caption"]


@pytest.mark.asyncio
async def test_deep_save_no_media_no_text_errors():
    client = MockClient()
    msg = FakeMessage(text="", media=None)
    result = await save_service.execute_save(client, 42, msg, "UTC")
    assert "no text or media" in result
    assert not [c for c in client.calls if c[0] in ("send_file", "send_message")]


@pytest.mark.asyncio
async def test_deep_save_cleans_temp_on_download_error():
    client = MockClient()

    async def boom(message, file=None, **kwargs):
        raise RuntimeError("network down")

    client.download_media = boom
    media = MessageMediaPhoto(photo=FakePhoto(), ttl_seconds=None)
    msg = FakeMessage(media=media)
    result = await save_service.execute_save(client, 42, msg, "UTC")
    assert "❌ Deep Save failed" in result
    # A download failure must remain a DEEP-save failure — it must never
    # react by falling back to forwarding (protected-chat invariant).
    assert not [c for c in client.calls if c[0] == "forward_messages"]
    assert not [c for c in client.calls if c[0] == "send_file"]
    assert db_client._fallback["saved_items"] == []  # no success DB record
    _assert_no_leftover_temp_dirs()


@pytest.mark.asyncio
async def test_deep_save_persists_new_upload_file_id():
    # The DB must reference the NEWLY uploaded message, not the source.
    new_doc = FakeDoc()
    new_doc.id = 424242
    new_doc.mime_type = "application/zip"
    client = MockClient(sent_media=MessageMediaDocument(document=new_doc, ttl_seconds=None))

    doc = FakeDoc()
    doc.mime_type = "application/zip"
    doc.size = 777
    doc.attributes = [DocumentAttributeFilename("archive.zip")]
    media = MessageMediaDocument(document=doc, ttl_seconds=None)
    msg = FakeMessage(media=media)
    result = await save_service.execute_save(client, 42, msg, "UTC")

    row = await db_client.query_save(_save_code(result))
    assert row["save_type"] == "deep"
    assert row["file_id"] == "424242"  # NEW upload id (source was 9999)
    assert row["mime_type"] == "application/zip"
    assert row["saved_msg_id"] == 600
    assert row["saved_chat_id"] == "me"


@pytest.mark.asyncio
async def test_deep_save_persists_full_metadata():
    client = MockClient()
    doc = FakeDoc()
    doc.mime_type = "application/zip"
    doc.size = 777
    doc.attributes = [DocumentAttributeFilename("archive.zip")]
    media = MessageMediaDocument(document=doc, ttl_seconds=None)
    msg = FakeMessage(media=media)
    result = await save_service.execute_save(client, 42, msg, "UTC")

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


@pytest.mark.asyncio
async def test_deep_save_forward_restriction_does_not_block_deep():
    # A protected chat blocks forwarding — but Deep Save must NOT depend on
    # forwarding. It downloads + re-uploads independently.
    client = MockClient()

    async def forward_blocked(entity, messages):
        raise RuntimeError("You can't forward messages from a protected chat")

    client.forward_messages = forward_blocked
    media = MessageMediaPhoto(photo=FakePhoto(), ttl_seconds=None)
    msg = FakeMessage(media=media)
    result = await save_service.execute_save(client, 42, msg, "UTC")

    assert "Saved Successfully" in result
    assert not [c for c in client.calls if c[0] == "forward_messages"]
    assert [c for c in client.calls if c[0] == "download_media"]
    assert [c for c in client.calls if c[0] == "send_file"]


@pytest.mark.asyncio
async def test_deep_save_upload_failure_no_db_record_and_cleanup():
    client = MockClient()

    async def upload_boom(entity, file, **kwargs):
        raise RuntimeError("upload rejected")

    client.send_file = upload_boom
    media = MessageMediaPhoto(photo=FakePhoto(), ttl_seconds=None)
    msg = FakeMessage(media=media)
    result = await save_service.execute_save(client, 42, msg, "UTC")

    assert "❌ Deep Save failed" in result
    assert db_client._fallback["saved_items"] == []  # no success DB record
    _assert_no_leftover_temp_dirs()


# ── Glass UI: Save → Deep Save → Reply Mode ──


def _panel_button_datas(buttons) -> list[str]:
    datas = []
    for row in buttons:
        if isinstance(row, list):
            for btn in row:
                data = getattr(btn, "data", None)
                if isinstance(data, bytes):
                    datas.append(data.decode("utf-8", errors="replace"))
                elif isinstance(data, str):
                    datas.append(data)
    return datas


@pytest.mark.asyncio
async def test_save_panel_has_no_forward_save():
    from backend.bot.handlers import save as save_handler

    title, body, buttons = await save_handler._save_panel_handler(None, "")
    labels = [b.text for row in buttons for b in row]
    datas = _panel_button_datas(buttons)
    assert "Forward Save" not in labels
    assert any("Deep Save" in l for l in labels)
    assert any(d.startswith("panel:save:type:d") for d in datas)
    assert any(d == "panel:retrieve" for d in datas)


@pytest.mark.asyncio
async def test_save_panel_deep_source_shows_reply_mode():
    from backend.bot.handlers import save as save_handler

    title, body, buttons = await save_handler._save_panel_handler(None, "type:d")
    labels = [b.text for row in buttons for b in row]
    datas = _panel_button_datas(buttons)
    assert any("Reply Mode" in l for l in labels)
    assert "action:save_reply" in datas
    assert "input:save:link" in datas


@pytest.mark.asyncio
async def test_save_reply_action_sets_pending_deep_state(monkeypatch):
    from backend.bot.handlers import save as save_handler
    from backend.helper import inline_engine, input_state

    monkeypatch.setattr(inline_engine, "_owner_id", 12345)

    class FakeEvent:
        message_id = 999
    ev = FakeEvent()

    title, body, buttons = await save_handler._save_reply_action(ev, "", 111)
    assert "Reply Mode" in body

    pending = input_state.get_pending(12345)
    assert pending is not None
    assert pending["panel_id"] == "save_reply"
    assert pending["chat_id"] == 111
    input_state.clear_pending(12345)


@pytest.mark.asyncio
async def test_save_reply_wait_resolves_reply_to_msg_id_and_deep_saves(monkeypatch):
    from backend.bot.handlers import save as save_handler
    from backend.helper import inline_engine, input_state

    calls = []

    class TargetMsg:
        chat_id = 111
        id = 555
        text = "target text"
        media = None
        sender_id = 777

        async def get_sender(self):
            return None

    class ReplyMsg:
        chat_id = 111
        id = 444
        reply_to_msg_id = 555

    class FakeClient:
        async def get_messages(self, chat_id, ids):
            calls.append(("get_messages", chat_id, ids))
            if ids == 444:
                return ReplyMsg()
            if ids == 555:
                return TargetMsg()
            return None

        async def delete_messages(self, chat_id, ids):
            calls.append(("delete_messages", chat_id, ids))

        async def send_message(self, entity, text):
            calls.append(("send_message", entity, text))
            return type("S", (), {"chat_id": "me", "id": 600, "media": None})()

        async def send_file(self, *a, **k):
            calls.append(("send_file", a, k))
            return type("S", (), {"chat_id": "me", "id": 600, "media": None})()

        async def download_media(self, *a, **k):
            calls.append(("download_media", a, k))
            return "ok"

        async def forward_messages(self, *a, **k):
            calls.append(("forward_messages", a, k))
            raise AssertionError("deep save must never forward")

    monkeypatch.setattr(inline_engine, "_owner_id", 12345)
    monkeypatch.setattr(inline_engine, "_self_client", FakeClient())
    monkeypatch.setattr(save_handler, "get_client", lambda: None)
    input_state.clear_pending(12345)

    await save_handler._save_reply_wait_handler("ignored", 111, 444, 0, 0)

    # Resolved the user reply, then the exact replied-to target message.
    assert ("get_messages", 111, 444) in calls
    assert ("get_messages", 111, 555) in calls
    # Text-only target → a NEW text message, never a forward.
    assert any(c[0] == "send_message" for c in calls)
    assert not any(c[0] == "forward_messages" for c in calls)
    # The trigger reply is cleaned up after saving.
    assert any(c[0] == "delete_messages" and c[1] == 111 for c in calls)
