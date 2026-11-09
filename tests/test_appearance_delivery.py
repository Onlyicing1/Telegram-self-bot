"""Appearance preview DELIVERY — the panel message is MEDIA, never a URL.

The reported defect: the main panel carried the versioned
``/appearance/preview/<version>`` URL as its text, so Telegram rendered its
web-page preview card ("Onrender" + the Guide site) instead of the owner's image
and exposed the Render link.

These tests drive the REAL delivery path — the inline result builder, the panel
repaint boundary and the lifecycle replacement — and assert on the Telegram
objects that path produces (a photo inline result whose ``send_message`` is a
media message), never on a formatter's string alone.
"""
from __future__ import annotations

import io
import logging
import struct
import zlib

import pytest

from backend.bot.handlers import appearance
from backend.helper import inline_engine
from backend.helper import panels
from backend.helper import lifecycle as lifecycle_mod
from backend.helper import symbols as sym
from backend.services import (
    appearance_service,
    panel_settings_repository,
    settings_service,
)

CHAT = -1001234567890
MSG = 700
NEW_MSG = 701


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _fresh(monkeypatch):
    settings_service._cache = dict(settings_service._DEFAULTS)
    settings_service._cache["appearance_page_overrides"] = {}
    settings_service._loaded = True
    appearance_service.reset_delivery_state()
    for name in (
        "PUBLIC_BASE_URL",
        "RENDER_EXTERNAL_URL",
        "APPEARANCE_MEDIA_BUCKET",
        "ARCHIVE_MEDIA_BUCKET",
    ):
        monkeypatch.delenv(name, raising=False)
    row: dict = {}

    def _update(field, value):
        row[field] = value
        return True

    monkeypatch.setattr(panel_settings_repository, "update_field", _update)
    # A configured DB RELOADS after every write, so the row must survive it.
    monkeypatch.setattr(
        panel_settings_repository, "load", lambda: dict(row) if row else None
    )
    yield
    appearance_service.reset_delivery_state()


class _Bucket:
    def __init__(self, objects):
        self.objects = objects
        self.download_error: Exception | None = None

    def upload(self, path, file, options=None):
        self.objects[path] = bytes(file)

    def remove(self, paths):
        for path in paths:
            self.objects.pop(path, None)

    def download(self, path):
        if self.download_error is not None:
            raise self.download_error
        return self.objects[path]

    def list(self, path=None, options=None):
        prefix = f"{path.rstrip('/')}/" if path else ""
        entries = []
        for key in sorted(self.objects):
            if not key.startswith(prefix):
                continue
            name = key[len(prefix):]
            if "/" in name:
                continue
            entries.append({
                "name": name,
                "created_at": "2026-01-01T00:00:00Z",
                "metadata": {"size": len(self.objects[key])},
            })
        return entries


class _Storage:
    def __init__(self, bucket):
        self.bucket = bucket

    def from_(self, _bucket):
        return self.bucket


class _DB:
    def __init__(self, bucket):
        self.storage = _Storage(bucket)


@pytest.fixture
def store(monkeypatch):
    from backend.db import client as db_client

    bucket = _Bucket({})
    monkeypatch.setattr(db_client, "get_db", lambda: _DB(bucket))
    monkeypatch.setenv("APPEARANCE_MEDIA_BUCKET", "appearance-assets")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://lifeos.example")
    return bucket


def png_bytes(width: int, height: int, colour: tuple[int, int, int] = (10, 120, 200)) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(
        b"\x00" + bytes(colour) * width for _ in range(height)
    )
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


async def _installed_with_preview(store, width: int = 4, height: int = 3) -> str:
    ok, _notice = await appearance_service.install_image(png_bytes(width, height))
    assert ok is True
    assert settings_service.set_appearance_preview_enabled(True) is True
    appearance_service.reset_delivery_state()
    return appearance_service.preview_version()


def _data(rows) -> list[str]:
    out: list[str] = []
    for row in rows:
        for button in getattr(row, "buttons", row):
            value = getattr(button, "data", None)
            out.append(value.decode() if isinstance(value, bytes) else str(value))
    return out


# ── the defect itself ───────────────────────────────────────────────────────

def test_the_old_url_composer_is_gone():
    """No code path may put the preview URL back into panel text."""
    title, body = appearance_service.with_preview("menu", "", "LifeOS", "Choose a category:")
    assert (title, body) == ("LifeOS", "Choose a category:")
    # preview_url() still exists for the Open Graph page, but is not composed.
    import inspect

    source = inspect.getsource(appearance_service.with_preview)
    assert "preview_url()" not in source


def test_main_pages_never_request_a_web_page_preview():
    assert panels._appearance_link_preview("menu") is False
    assert panels._appearance_link_preview("save") is False
    assert panels._appearance_link_preview("not_a_main_page") is None


# ── the real builder produces photo media ──────────────────────────────────

@pytest.mark.asyncio
async def test_enabled_preview_builds_a_photo_result_with_the_text_as_caption(store):
    from telethon.tl import types

    from backend.helper.panel_render import render

    version = await _installed_with_preview(store)
    result = render("LifeOS", "Choose a category:", [])
    built = await appearance_service.apply_to_inline_results("menu", "", [result])

    photo = built[0]
    assert photo.type == "photo"
    assert isinstance(photo.content, types.InputWebDocument)
    assert photo.content.url == (
        f"https://lifeos.example/appearance/image/{version}?fmt=jpeg"
    )
    assert photo.content.mime_type == "image/jpeg"
    assert isinstance(photo.send_message, types.InputBotInlineMessageMediaAuto)
    caption = photo.send_message.message
    assert "Choose a category:" in caption
    assert "LifeOS" in caption
    assert "https://" not in caption and "Onrender" not in caption
    # No web-page flag exists on a media message: a card cannot be requested.
    assert not hasattr(photo.send_message, "no_webpage")


@pytest.mark.asyncio
async def test_the_selected_image_is_the_one_handed_to_telegram(store):
    keys_versions: list[str] = []
    first = await _installed_with_preview(store)
    keys_versions.append(first)

    ok, _notice = await appearance_service.install_image(png_bytes(5, 5))
    assert ok is True
    appearance_service.reset_delivery_state()
    second = appearance_service.preview_version()
    keys_versions.append(second)
    assert first != second

    from backend.helper.panel_render import render

    built = await appearance_service.apply_to_inline_results(
        "menu", "", [render("LifeOS", "Choose a category:", [])]
    )
    url = built[0].content.url
    assert second in url and first not in url


@pytest.mark.asyncio
async def test_caption_too_long_falls_back_to_text_without_a_url(store):
    from telethon.tl import types

    from backend.helper.panel_render import render

    await _installed_with_preview(store)
    body = "x" * (appearance_service.MAX_CAPTION_CHARS + 10)
    result = render("LifeOS", body, [])
    built = await appearance_service.apply_to_inline_results("menu", "", [result])

    text = built[0].send_message
    assert isinstance(text, types.InputBotInlineMessageText)
    assert body in text.message  # the full text is preserved
    assert "https://" not in text.message
    assert text.no_webpage is True


@pytest.mark.asyncio
async def test_disabled_preview_builds_text_only_and_hides_any_card(store):
    from telethon.tl import types

    from backend.helper.panel_render import render

    await _installed_with_preview(store)
    assert settings_service.set_appearance_preview_enabled(False) is True

    built = await appearance_service.apply_to_inline_results(
        "menu", "", [render("LifeOS", "Choose a category:", [])]
    )
    message = built[0].send_message
    assert isinstance(message, types.InputBotInlineMessageText)
    assert message.no_webpage is True
    assert "/appearance/" not in message.message


@pytest.mark.asyncio
async def test_a_missing_selected_object_never_becomes_a_broken_photo(store):
    from telethon.tl import types

    from backend.helper.panel_render import render

    settings_service.set_appearance_preview_image_key(
        f"appearance/preview/{'f' * 32}.png"
    )
    assert settings_service.set_appearance_preview_enabled(True) is True

    built = await appearance_service.apply_to_inline_results(
        "menu", "", [render("LifeOS", "Choose a category:", [])]
    )
    message = built[0].send_message
    assert isinstance(message, types.InputBotInlineMessageText)
    assert message.no_webpage is True
    assert "https://" not in message.message


# ── the JPEG rendition the photo points at ─────────────────────────────────

@pytest.mark.asyncio
async def test_telegram_photo_rendition_is_jpeg_and_leaves_the_original_alone(store):
    from PIL import Image

    from fastapi.testclient import TestClient

    from backend.web.app import app

    version = await _installed_with_preview(store)
    original = store.objects[f"appearance/preview/{version}.png"]

    client = TestClient(app)
    photo = client.get(f"/appearance/image/{version}?fmt=jpeg")
    assert photo.status_code == 200
    assert photo.headers["content-type"].startswith("image/jpeg")
    assert "immutable" in photo.headers["cache-control"]
    with Image.open(io.BytesIO(photo.content)) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.size == (4, 3)

    stored = client.get(f"/appearance/image/{version}")
    assert stored.status_code == 200
    assert stored.content == original  # untouched original bytes


@pytest.mark.asyncio
async def test_telegram_photo_refuses_a_stale_version(store):
    from fastapi.testclient import TestClient

    from backend.web.app import app

    await _installed_with_preview(store)
    client = TestClient(app)
    assert client.get(f"/appearance/image/{'0' * 32}?fmt=jpeg").status_code == 404


@pytest.mark.asyncio
async def test_an_unreadable_object_yields_no_rendition(store):
    version = await _installed_with_preview(store)
    store.download_error = RuntimeError("storage down")
    assert await appearance_service.telegram_photo(version) is None


# ── switching / disabling requires a NEW message ───────────────────────────

@pytest.mark.asyncio
async def test_switching_the_image_marks_the_panel_message_stale(store):
    first = await _installed_with_preview(store)
    assert await appearance_service.delivery_ready() is True
    appearance_service.note_delivered(CHAT, MSG, ("media", first))

    assert appearance_service.needs_new_message(CHAT, MSG, "LifeOS", "Choose a category:") is False

    ok, _notice = await appearance_service.install_image(png_bytes(6, 6))
    assert ok is True
    second = appearance_service.preview_version()
    assert second != first

    assert appearance_service.needs_new_message(CHAT, MSG, "LifeOS", "Choose a category:") is True


@pytest.mark.asyncio
async def test_disabling_preview_returns_the_panel_to_text(store):
    version = await _installed_with_preview(store)
    assert await appearance_service.delivery_ready() is True
    appearance_service.note_delivered(CHAT, MSG, ("media", version))

    assert settings_service.set_appearance_preview_enabled(False) is True
    assert appearance_service.needs_new_message(
        CHAT, MSG, "LifeOS", "Choose a category:"
    ) is True
    appearance_service.note_delivered(CHAT, MSG, ("text", ""))
    assert appearance_service.needs_new_message(
        CHAT, MSG, "LifeOS", "Choose a category:"
    ) is False


@pytest.mark.asyncio
async def test_an_unknown_old_panel_is_replaced_once_for_media(store):
    await _installed_with_preview(store)
    assert await appearance_service.delivery_ready() is True
    # No recorded state: a text panel left by an older build must become media.
    assert appearance_service.needs_new_message(
        CHAT, MSG, "LifeOS", "Choose a category:"
    ) is True


@pytest.mark.asyncio
async def test_sub_views_and_other_panels_are_never_replaced(store):
    await _installed_with_preview(store)
    assert await appearance_service.delivery_ready() is True

    class _Lifecycle:
        def __init__(self):
            self.calls: list = []
            self.sessions = type("S", (), {"current_nav": lambda _self, c, m: ("menu", "sub")})()

        async def replace_panel(self, *args):
            self.calls.append(args)
            return True

    fake = _Lifecycle()
    original = panels.get_lifecycle
    panels.get_lifecycle = lambda: fake
    try:
        assert await panels._replace_for_delivery("menu", "LifeOS", "Choose", CHAT, MSG) is False
        assert await panels._replace_for_delivery("not_a_page", "X", "Y", CHAT, MSG) is False
        assert fake.calls == []
    finally:
        panels.get_lifecycle = original


@pytest.mark.asyncio
async def test_the_paint_path_replaces_instead_of_editing_the_old_message(store, monkeypatch):
    await _installed_with_preview(store)
    assert await appearance_service.delivery_ready() is True

    class _Lifecycle:
        def __init__(self):
            self.calls: list = []
            self.sessions = type("S", (), {"current_nav": lambda _self, c, m: ("menu", "")})()

        async def replace_panel(self, *args):
            self.calls.append(args)
            return True

    fake = _Lifecycle()
    monkeypatch.setattr(panels, "get_lifecycle", lambda: fake)

    edited: list = []

    class _Event:
        async def edit(self, *args, **kwargs):  # pragma: no cover - must not run
            edited.append((args, kwargs))

    await panels._render_and_edit(
        _Event(), ("LifeOS", "Choose a category:", []), "menu", CHAT, MSG
    )

    assert fake.calls == [(CHAT, MSG, "menu", "")]
    assert edited == []  # the old message is never painted


# ── lifecycle: replace, and no duplicate panels ────────────────────────────

class _SelfClient:
    def __init__(self):
        self.deleted: list = []
        self.edits: list = []

    async def delete_messages(self, chat_id, ids):
        self.deleted.append((chat_id, list(ids)))

    async def edit_message(self, *args, **kwargs):
        self.edits.append((args, kwargs))


def _trigger(monkeypatch, outcomes):
    from backend.helper import inline_engine

    async def _fake(self_client, chat_id, query):
        return outcomes.pop(0)

    monkeypatch.setattr(inline_engine, "trigger", _fake)


@pytest.mark.asyncio
async def test_create_panel_records_what_the_builder_actually_produced(monkeypatch):
    manager = lifecycle_mod.PanelLifecycleManager()
    client = _SelfClient()
    manager.configure(client, 1)
    _trigger(monkeypatch, [(True, CHAT, NEW_MSG, "")])
    appearance_service.note_built("menu", ("media", "a" * 32))

    ok, chat, msg = await manager.create_panel(CHAT, "menu", "")

    assert (ok, chat, msg) == (True, CHAT, NEW_MSG)
    assert appearance_service.delivered_state(chat, msg) == ("media", "a" * 32)


@pytest.mark.asyncio
async def test_replacement_creates_first_and_only_then_deletes_the_old(monkeypatch):
    manager = lifecycle_mod.PanelLifecycleManager()
    client = _SelfClient()
    manager.configure(client, 1)
    manager.sessions.create(CHAT, MSG, panel_type="menu", extra="")
    _trigger(monkeypatch, [(True, CHAT, NEW_MSG, "")])
    appearance_service.note_built("menu", ("media", "b" * 32))

    replaced = await manager.replace_panel(CHAT, MSG, "menu", "")

    assert replaced is True
    assert client.deleted == [(CHAT, [MSG])]
    assert manager.sessions.get(CHAT, NEW_MSG) is not None
    assert manager.sessions.get(CHAT, MSG) is None
    assert appearance_service.delivered_state(CHAT, NEW_MSG) == ("media", "b" * 32)


@pytest.mark.asyncio
async def test_a_failed_replacement_keeps_the_existing_panel(monkeypatch):
    manager = lifecycle_mod.PanelLifecycleManager()
    client = _SelfClient()
    manager.configure(client, 1)
    manager.sessions.create(CHAT, MSG, panel_type="menu", extra="")
    _trigger(monkeypatch, [(False, CHAT, 0, "")])

    replaced = await manager.replace_panel(CHAT, MSG, "menu", "")

    assert replaced is False
    assert client.deleted == []
    assert manager.sessions.get(CHAT, MSG) is not None


@pytest.mark.asyncio
async def test_closing_a_panel_forgets_its_delivery_state(monkeypatch):
    manager = lifecycle_mod.PanelLifecycleManager()
    manager.configure(_SelfClient(), 1)
    manager.sessions.create(CHAT, MSG, panel_type="menu", extra="")
    appearance_service.note_delivered(CHAT, MSG, ("text", ""))

    await manager.destroy_panel(CHAT, MSG)

    assert appearance_service.delivered_state(CHAT, MSG) is None


# ── the Appearance panel's own notice still tells the truth ────────────────

@pytest.mark.asyncio
async def test_the_gallery_panel_never_shows_the_preview_url(store):
    await _installed_with_preview(store)
    _title, body, buttons = appearance._preview_view(await appearance_service.gallery())
    assert "https://" not in body
    assert "/appearance/preview/" not in body
    assert f"input:{appearance._PANEL}:image" in _data(buttons)


# ── the required thumbnail document (the production regression) ────────────
#
# Production evidence: after the media-delivery change, every root `menu`
# query failed with `PhotoThumbUrlEmptyError` from `SetInlineBotResultsRequest`
# and the Helper Bot answered nothing. The raw TL constructor carries the photo
# document in `content` and the thumbnail in a SEPARATE `thumb` field; the old
# `_as_photo_result()` supplied only `content`.

@pytest.mark.asyncio
async def test_photo_result_carries_the_required_thumbnail_document(store):
    from telethon.tl import types

    from backend.helper.panel_render import render

    version = await _installed_with_preview(store)
    built = await appearance_service.apply_to_inline_results(
        "menu", "", [render("LifeOS", "Choose a category:", [])]
    )

    photo = built[0]
    assert photo.type == "photo"
    assert isinstance(photo.content, types.InputWebDocument)
    assert isinstance(photo.thumb, types.InputWebDocument)
    assert photo.content.url == (
        f"https://lifeos.example/appearance/image/{version}?fmt=jpeg"
    )
    assert photo.thumb.url == (
        f"https://lifeos.example/appearance/image/{version}?fmt=jpeg&thumb=1"
    )
    assert photo.thumb.url != photo.content.url
    assert photo.thumb.mime_type == "image/jpeg"
    assert photo.content.mime_type == "image/jpeg"
    assert photo.thumb.attributes == []
    assert appearance_service.contains_photo_result(built) is True


@pytest.mark.asyncio
async def test_both_media_urls_resolve_to_valid_jpeg_images(store):
    from PIL import Image

    from fastapi.testclient import TestClient

    from backend.web.app import app

    version = await _installed_with_preview(store, 800, 600)
    client = TestClient(app)

    photo = client.get(f"/appearance/image/{version}?fmt=jpeg")
    thumb = client.get(f"/appearance/image/{version}?fmt=jpeg&thumb=1")
    for response in (photo, thumb):
        assert response.status_code == 200
        assert response.headers["content-type"].startswith("image/jpeg")
        with Image.open(io.BytesIO(response.content)) as decoded:
            assert decoded.format == "JPEG"

    with Image.open(io.BytesIO(thumb.content)) as decoded:
        assert max(decoded.size) <= appearance_service.TELEGRAM_THUMB_MAX_SIDE
        assert decoded.size == (320, 240)  # bounded, aspect ratio kept

    # Delivery renditions never modify the stored original for either URL.
    original = store.objects[f"appearance/preview/{version}.png"]
    assert client.get(f"/appearance/image/{version}").content == original


@pytest.mark.asyncio
async def test_the_thumbnail_url_is_never_in_the_caption(store):
    from backend.helper.panel_render import render

    await _installed_with_preview(store)
    built = await appearance_service.apply_to_inline_results(
        "menu", "", [render("LifeOS", "Choose a category:", [])]
    )
    caption = built[0].send_message.message
    assert "https://" not in caption
    assert "/appearance/" not in caption
    assert "thumb" not in caption


# ── the root menu must survive a rejected photo result ─────────────────────

OWNER = 4242


def _panel_result():
    from backend.helper.panel_render import render

    builder = panels.InlinePanelBuilder()
    builder.add_row("Save", "panel:save")
    return render("LifeOS", "Choose a category:", builder.build())


class _InlineEvent:
    def __init__(self, text: str = "menu"):
        self.text = text
        self.sender_id = OWNER
        self.answers: list = []

    async def answer(self, results=None, **kwargs):
        self.answers.append(results)


def _inline_handler(monkeypatch, builder):
    """Register the REAL router on a fake helper client and return its handler."""

    class _Helper:
        def __init__(self):
            self.handlers: list = []

        def on(self, _event):
            def _decorator(fn):
                self.handlers.append(fn)
                return fn

            return _decorator

    helper = _Helper()
    monkeypatch.setattr(inline_engine, "is_owner", lambda event, owner: True)
    monkeypatch.setitem(inline_engine._builders, "menu", builder)
    monkeypatch.setattr(inline_engine, "_last_answers", {})
    inline_engine.register_inline_handler(helper, OWNER)
    assert len(helper.handlers) == 1
    return helper.handlers[0]


@pytest.mark.asyncio
async def test_a_rejected_photo_result_opens_the_text_panel(store, monkeypatch, caplog):
    """The production regression: PhotoThumbUrlEmptyError must not empty the menu."""
    from telethon.errors.rpcerrorlist import PhotoThumbUrlEmptyError
    from telethon.tl import types

    await _installed_with_preview(store)

    async def builder(event, extra):
        return [_panel_result()]

    handler = _inline_handler(monkeypatch, builder)

    class _RejectingEvent(_InlineEvent):
        async def answer(self, results=None, **kwargs):
            self.answers.append(results)
            if len(self.answers) == 1:
                raise PhotoThumbUrlEmptyError(request=None)

    event = _RejectingEvent()
    with caplog.at_level(logging.WARNING, logger="backend.helper.inline_engine"):
        await handler(event)

    # The photo was attempted, Telegram refused it, and the SAME query was then
    # answered with the preserved text panel.
    assert len(event.answers) == 2
    assert event.answers[0][0].type == "photo"
    fallback = event.answers[1]
    assert isinstance(fallback, list) and len(fallback) == 1
    message = fallback[0].send_message
    assert isinstance(message, types.InputBotInlineMessageText)
    assert message.no_webpage is True
    assert "LifeOS" in message.message
    assert "Choose a category:" in message.message
    assert "https://" not in message.message
    assert "/appearance/" not in message.message
    # The fallback keeps exactly the keyboard the photo result carried.
    photo_rows = event.answers[0][0].send_message.reply_markup.rows
    assert _data(message.reply_markup.rows) == _data(photo_rows)
    assert "panel:save" in _data(message.reply_markup.rows)

    record = inline_engine.last_inline_answer("menu")
    assert record["ok"] is False
    assert "PhotoThumbUrlEmptyError" in record["error"]
    assert "text" in record["error"]

    logs = "\n".join(record_.getMessage() for record_ in caplog.records)
    assert "media result rejected" in logs
    assert "PhotoThumbUrlEmptyError" in logs
    assert "Choose a category:" not in logs  # the caption itself is never logged


@pytest.mark.asyncio
async def test_an_accepted_photo_result_never_uses_the_fallback(store, monkeypatch):
    from telethon.tl import types

    await _installed_with_preview(store)

    async def builder(event, extra):
        return [_panel_result()]

    handler = _inline_handler(monkeypatch, builder)
    event = _InlineEvent()
    await handler(event)

    assert len(event.answers) == 1
    photo = event.answers[0][0]
    assert photo.type == "photo"
    assert isinstance(photo.send_message, types.InputBotInlineMessageMediaAuto)
    assert isinstance(photo.thumb, types.InputWebDocument)
    assert inline_engine.last_inline_answer("menu")["ok"] is True


@pytest.mark.asyncio
async def test_unrelated_answer_failures_are_not_a_media_fallback(store, monkeypatch):
    await _installed_with_preview(store)

    async def builder(event, extra):
        return [_panel_result()]

    handler = _inline_handler(monkeypatch, builder)

    class _FloodingEvent(_InlineEvent):
        async def answer(self, results=None, **kwargs):
            self.answers.append(results)
            raise TimeoutError("transport died")

    event = _FloodingEvent()
    await handler(event)

    # The failure stays a failure: the last answer is the existing empty answer,
    # and no text fallback is claimed.
    assert event.answers[-1] == []
    assert len(event.answers) == 2
    record = inline_engine.last_inline_answer("menu")
    assert record["ok"] is False
    assert "text" not in record["error"]
    assert inline_engine.is_media_result_rejection(TimeoutError("x")) is False


@pytest.mark.asyncio
async def test_preview_disabled_still_answers_one_text_panel(store, monkeypatch):
    from telethon.tl import types

    await _installed_with_preview(store)
    assert settings_service.set_appearance_preview_enabled(False) is True
    appearance_service.reset_delivery_state()

    async def builder(event, extra):
        return [_panel_result()]

    handler = _inline_handler(monkeypatch, builder)
    event = _InlineEvent()
    await handler(event)

    assert len(event.answers) == 1
    message = event.answers[0][0].send_message
    assert isinstance(message, types.InputBotInlineMessageText)
    assert message.no_webpage is True
    assert "panel:save" in _data(message.reply_markup.rows)
    assert inline_engine.last_inline_answer("menu")["ok"] is True
