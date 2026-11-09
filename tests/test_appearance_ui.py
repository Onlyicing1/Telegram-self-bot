"""Appearance personalization — navigation, text composition and the preview.

Source-backed: the eligible pages are exactly the main menu's own ``panel:*``
destinations plus the menu itself, and the canonical defaults below are the
handlers' real home-view text (asserted against them here).
"""
from __future__ import annotations

import asyncio
import io
import os
import struct
import tempfile
import zlib

import pytest

from backend.bot.handlers import appearance, misc
from backend.helper import panels, symbols as sym
from backend.helper.panel_registry import get_panel_def
from backend.services import (
    appearance_service,
    panel_settings_repository,
    settings_service,
)

OWNER = 7770001
CHAT = -1001234567890
PANEL_MSG = 500


# ── fixtures ────────────────────────────────────────────────────────────────

@pytest.fixture(autouse=True)
def _fresh_settings(monkeypatch):
    settings_service._cache = dict(settings_service._DEFAULTS)
    settings_service._cache["appearance_page_overrides"] = {}
    settings_service._loaded = True
    for name in (
        "PUBLIC_BASE_URL",
        "RENDER_EXTERNAL_URL",
        "APPEARANCE_MEDIA_BUCKET",
        "ARCHIVE_MEDIA_BUCKET",
    ):
        monkeypatch.delenv(name, raising=False)
    yield


@pytest.fixture
def persisted(monkeypatch):
    """A repository whose write succeeds and whose row survives a reload."""
    row: dict = {}

    def _update(field, value):
        row[field] = value
        return True

    monkeypatch.setattr(panel_settings_repository, "update_field", _update)
    monkeypatch.setattr(
        panel_settings_repository, "load", lambda: dict(row) if row else None
    )
    return row


class _Bucket:
    def __init__(self, objects, calls):
        self.objects = objects
        self.calls = calls
        self.list_error: Exception | None = None

    def upload(self, path, file, options=None):
        self.objects[path] = bytes(file)
        self.calls.append(("upload", path, options))

    def remove(self, paths):
        for path in paths:
            self.objects.pop(path, None)
        self.calls.append(("remove", tuple(paths)))

    def download(self, path):
        return self.objects[path]

    def list(self, path=None, options=None):
        self.calls.append(("list", path, options))
        if self.list_error is not None:
            raise self.list_error
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
                "id": f"id-{name}",
                "created_at": "2026-01-01T00:00:00Z",
                "metadata": {"size": len(self.objects[key])},
            })
        return entries


class _Storage:
    def __init__(self, objects, calls):
        self.objects = objects
        self.calls = calls

    def from_(self, bucket):
        self.calls.append(("bucket", bucket))
        return _Bucket(self.objects, self.calls)


class _DB:
    def __init__(self, objects, calls):
        self.storage = _Storage(objects, calls)


@pytest.fixture
def storage(monkeypatch):
    from backend.db import client as db_client

    objects: dict = {}
    calls: list = []
    monkeypatch.setattr(db_client, "get_db", lambda: _DB(objects, calls))
    monkeypatch.setenv("APPEARANCE_MEDIA_BUCKET", "appearance-assets")
    return objects, calls


# ── image builders ──────────────────────────────────────────────────────────

def png_bytes(width: int, height: int) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x00\x00\x00" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", ihdr)
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


def jpeg_bytes(width: int, height: int) -> bytes:
    """A JPEG the installed decoder can actually open (upload is verified)."""
    from PIL import Image

    buffer = io.BytesIO()
    Image.new("RGB", (width, height), (5, 6, 7)).save(buffer, "JPEG")
    return buffer.getvalue()


def _buttons_of(row):
    return getattr(row, "buttons", row)


def _data(rows) -> list[str]:
    out: list[str] = []
    for row in rows:
        for button in _buttons_of(row):
            value = getattr(button, "data", None)
            out.append(value.decode() if isinstance(value, bytes) else str(value))
    return out


def _labels(rows) -> list[str]:
    out: list[str] = []
    for row in rows:
        for button in _buttons_of(row):
            out.append(getattr(button, "text", ""))
    return out


# ── navigation ──────────────────────────────────────────────────────────────

def test_main_menu_gains_exactly_one_category():
    data = _data(misc._build_menu_buttons())
    assert "panel:appearance" in data
    # The pre-existing nine categories plus the one new Appearance entry.
    assert len(data) == 10
    for expected in (
        "panel:save",
        "panel:del",
        "panel:profile",
        "panel:db",
        "panel:general",
        "panel:settings",
        "panel:cipher",
        "panel:todo",
        "panel:ai",
    ):
        assert expected in data


def test_eligible_pages_are_the_actual_menu_destinations():
    menu_pages = {
        value[6:]
        for value in _data(misc._build_menu_buttons())
        if value.startswith("panel:")
    }
    assert menu_pages | {"menu"} == set(appearance_service.MAIN_PAGE_IDS)
    assert "appearance" in menu_pages


def test_appearance_sections_are_registered_sub_panels():
    class _NoListeners:
        def on(self, *args, **kwargs):  # pragma: no cover - asserted below
            raise AssertionError("Appearance must not register a listener")

    client = _NoListeners()
    appearance.register(client, OWNER)

    assert get_panel_def("appearance").parent_panel == "menu"
    assert get_panel_def("appearance").title == "Appearance"
    for panel_id in ("appearance_header", "appearance_pages", "appearance_preview"):
        assert get_panel_def(panel_id).parent_panel == "appearance"
    assert get_panel_def("appearance_page").parent_panel == "appearance_pages"


def test_appearance_home_offers_three_sections():
    title, body, buttons = asyncio.run(appearance._home(None, ""))
    data = _data(buttons)
    assert title == "Appearance"
    assert "panel:appearance_header" in data
    assert "panel:appearance_pages" in data
    assert "panel:appearance_preview" in data
    assert "Header" in body and "Pages" in body and "Preview" in body


def test_appearance_labels_use_registry_symbols_only():
    glyphs = {spec.glyph for spec in sym.ALL_SYMBOLS.values()}
    views = [
        asyncio.run(appearance._home(None, "")),
        appearance._header_view(),
        appearance._pages_view(),
        appearance._preview_view(),
    ]
    for _title, _body, buttons in views:
        for label in _labels(buttons):
            assert label.split(" ")[0] in glyphs, label
            assert "\ufe0f" not in label
            assert not any(ord(ch) >= 0x1F000 for ch in label), label


# ── global header ───────────────────────────────────────────────────────────

def test_header_defaults_leave_panels_untouched():
    assert settings_service.appearance_header_title() == ""
    assert settings_service.appearance_header_body() == ""
    assert appearance_service.compose_text(
        "menu", "", "LifeOS", "Choose a category:"
    ) == ("LifeOS", "Choose a category:")


def test_header_title_and_body_are_independent(persisted):
    ok, _notice = appearance_service.set_header("title", "My LifeOS")
    assert ok is True
    # The header becomes the panel title; the page's own title is preserved
    # beneath it so the page stays identifiable (never duplicated).
    title, body = appearance_service.compose_text(
        "menu", "", "LifeOS", "Choose a category:"
    )
    assert title == "My LifeOS"
    assert body == "**LifeOS**\n\nChoose a category:"

    ok, _notice = appearance_service.set_header("body", "Shared header line.")
    assert ok is True
    title, body = appearance_service.compose_text(
        "menu", "", "LifeOS", "Choose a category:"
    )
    assert title == "My LifeOS"
    assert body.index("**LifeOS**") < body.index("Shared header line.")
    assert body.index("Shared header line.") < body.index("Choose a category:")


def test_header_values_reset_to_real_defaults(persisted):
    appearance_service.set_header("title", "Custom")
    appearance_service.set_header("body", "Custom description")

    ok, _ = appearance_service.reset_header("title")
    assert ok is True and settings_service.appearance_header_title() == ""
    assert settings_service.appearance_header_body() == "Custom description"

    ok, _ = appearance_service.reset_header("both")
    assert ok is True
    assert settings_service.appearance_header_body() == ""
    assert appearance_service.compose_text(
        "menu", "", "LifeOS", "Choose a category:"
    ) == ("LifeOS", "Choose a category:")


def test_header_rejects_empty_and_oversized_without_corruption(persisted):
    appearance_service.set_header("title", "Kept")

    ok, notice = appearance_service.set_header("title", "   ")
    assert ok is False and "empty" in notice
    assert settings_service.appearance_header_title() == "Kept"

    ok, notice = appearance_service.set_header(
        "title", "x" * (appearance_service.TITLE_LIMIT + 1)
    )
    assert ok is False and "Too long" in notice
    assert settings_service.appearance_header_title() == "Kept"

    ok, _ = appearance_service.set_header(
        "body", "y" * (appearance_service.BODY_LIMIT + 1)
    )
    assert ok is False
    assert settings_service.appearance_header_body() == ""


def test_header_title_is_single_line(persisted):
    appearance_service.set_header("title", "Line one\nLine   two")
    assert settings_service.appearance_header_title() == "Line one Line two"


# ── per-page text ───────────────────────────────────────────────────────────

def test_page_title_and_body_overrides_are_independent(persisted):
    default_title, default_body = appearance_service.PAGE_DEFAULTS["del"]

    ok, _ = appearance_service.set_page_text("del", "title", "Erase")
    assert ok is True
    composed = appearance_service.compose_text("del", "", default_title, default_body)
    assert composed == ("Erase", default_body)

    ok, _ = appearance_service.set_page_text("del", "body", "Pick how much to erase.")
    assert ok is True
    composed = appearance_service.compose_text("del", "", default_title, default_body)
    assert composed == ("Erase", "Pick how much to erase.")


def test_page_override_never_leaks_to_another_page(persisted):
    appearance_service.set_page_text("del", "title", "Erase")
    assert appearance_service.compose_text(
        "menu", "", "LifeOS", "Choose a category:"
    ) == ("LifeOS", "Choose a category:")
    assert appearance_service.compose_text(
        "save", "", "Save", appearance_service.default_body("save")
    ) == ("Save", appearance_service.default_body("save"))


def test_page_reset_restores_the_handler_default(persisted):
    appearance_service.set_page_text("profile", "title", "People")
    appearance_service.set_page_text("profile", "body", "Choose.")

    ok, _ = appearance_service.reset_page_text("profile", "title")
    assert ok is True
    state = appearance_service.page_state("profile")
    assert state["title"] == "Profile"
    assert state["body"] == "Choose."  # body override untouched

    ok, _ = appearance_service.reset_page_text("profile", "all")
    assert ok is True
    assert appearance_service.page_state("profile")["custom_body"] is False
    assert appearance_service.page_state("profile")["body"] == "Choose a section:"


def test_dynamic_body_keeps_live_content(persisted):
    appearance_service.set_page_text("settings", "body", "My own introduction.")
    live = "**Auto-close:** ON (120s)\n**Delete batch:** 100"
    title, body = appearance_service.compose_text("settings", "", "Settings", live)
    assert title == "Settings"
    assert body.startswith("My own introduction.")
    assert "Auto-close" in body and "Delete batch" in body


def test_subviews_and_results_are_never_customized(persisted):
    appearance_service.set_page_text("del", "title", "Erase")
    appearance_service.set_page_text("del", "body", "Pick one.")
    assert appearance_service.compose_text(
        "del", "q:25", "Delete", "Delete the last 25 of your messages?"
    ) == ("Delete", "Delete the last 25 of your messages?")
    assert appearance_service.compose_text(
        "delfrom", "", "Delete From", "Pick a message"
    ) == ("Delete From", "Pick a message")


def test_unknown_page_ids_and_fields_are_rejected(persisted):
    for page_id in ("delfrom", "font", "appearance_header", "", "MENU"):
        ok, notice = appearance_service.set_page_text(page_id, "title", "x")
        assert ok is False and "not a customizable main page" in notice
    ok, _ = appearance_service.set_page_text("del", "footer", "x")
    assert ok is False
    assert settings_service.appearance_page_overrides() == {}


def test_page_override_survives_a_reload(persisted):
    appearance_service.set_page_text("ai", "title", "Assistant")
    appearance_service.set_header("body", "Header text")

    settings_service._loaded = False
    settings_service.load_all()

    assert appearance_service.page_state("ai")["title"] == "Assistant"
    assert settings_service.appearance_header_body() == "Header text"


def test_buttons_and_callbacks_are_unchanged_by_customization(persisted):
    appearance_service.set_page_text("menu", "title", "Custom")
    _t, _b, buttons = asyncio.run(misc._menu_panel_handler(None, ""))
    assert _data(buttons) == _data(misc._build_menu_buttons())
    assert panels._apply_appearance("menu", "", ("LifeOS", "Choose a category:", buttons)) == (
        "Custom",
        "Choose a category:",
        buttons,
    )


# ── persistence contract ────────────────────────────────────────────────────

def test_rejected_database_write_is_reported_honestly(monkeypatch):
    monkeypatch.setattr(panel_settings_repository, "update_field", lambda *a: False)
    monkeypatch.setattr(settings_service, "_db_configured", lambda: True)

    ok, notice = appearance_service.set_header("title", "Not saved")
    assert ok is False and "refused" in notice
    assert settings_service.appearance_header_title() == ""


def test_no_database_uses_the_documented_memory_fallback():
    assert settings_service.set_appearance_header_title("Memory") is True
    assert settings_service.appearance_header_title() == "Memory"


# ── image validation and storage ────────────────────────────────────────────

def test_sniff_image_reads_real_headers():
    assert appearance_service.sniff_image(png_bytes(4, 3))[1:] == (4, 3)
    assert appearance_service.sniff_image(jpeg_bytes(7, 9))[1:] == (7, 9)
    assert appearance_service.sniff_image(b"plain text, not an image") is None
    assert appearance_service.sniff_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 40) is None


def test_validate_image_enforces_size_and_dimensions():
    appearance_service.validate_image(png_bytes(10, 10))
    with pytest.raises(appearance_service.ImageRejected):
        appearance_service.validate_image(
            b"x" * (appearance_service.MAX_IMAGE_BYTES + 1)
        )
    with pytest.raises(appearance_service.ImageRejected):
        appearance_service.validate_image(png_bytes(appearance_service.MAX_IMAGE_SIDE + 1, 2))
    with pytest.raises(appearance_service.ImageRejected):
        appearance_service.validate_image(b"not an image")


@pytest.mark.asyncio
async def test_storage_unavailable_is_honest(persisted):
    ok, notice = await appearance_service.install_image(png_bytes(2, 2))
    assert ok is False and "not configured" in notice
    assert settings_service.appearance_preview_image_key() == ""


@pytest.mark.asyncio
async def test_upload_stores_object_and_only_a_key(persisted, storage):
    objects, calls = storage
    ok, notice = await appearance_service.install_image(png_bytes(3, 2))
    assert ok is True and "stored" in notice

    key = settings_service.appearance_preview_image_key()
    assert key.startswith("appearance/preview/") and key.endswith(".png")
    assert not key.startswith("http")
    assert key in objects
    assert ("upload", key, {"content-type": "image/png", "upsert": "false"}) in calls


@pytest.mark.asyncio
async def test_upload_retains_every_previous_image(persisted, storage):
    objects, _calls = storage
    await appearance_service.install_image(png_bytes(2, 2))
    first = settings_service.appearance_preview_image_key()
    await appearance_service.install_image(jpeg_bytes(5, 5))
    second = settings_service.appearance_preview_image_key()

    assert second != first
    assert first in objects and second in objects
    assert second.endswith(".jpg")
    assert len(objects) == 2


@pytest.mark.asyncio
async def test_failed_upload_keeps_the_previous_image(persisted, storage):
    objects, _calls = storage
    await appearance_service.install_image(png_bytes(2, 2))
    first = settings_service.appearance_preview_image_key()

    ok, notice = await appearance_service.install_image(b"not an image")
    assert ok is False and "not a readable" in notice
    assert settings_service.appearance_preview_image_key() == first
    assert first in objects


@pytest.mark.asyncio
async def test_deleting_the_selected_image_clears_reference_and_object(persisted, storage):
    objects, _calls = storage
    await appearance_service.install_image(png_bytes(2, 2))
    key = settings_service.appearance_preview_image_key()

    ok, _notice = await appearance_service.delete_image(
        appearance_service.token_from_key(key)
    )
    assert ok is True
    assert settings_service.appearance_preview_image_key() == ""
    assert key not in objects


@pytest.mark.asyncio
async def test_disabling_preview_retains_the_stored_image(persisted, storage):
    objects, _calls = storage
    await appearance_service.install_image(png_bytes(2, 2))
    key = settings_service.appearance_preview_image_key()
    assert settings_service.set_appearance_preview_enabled(False) is True

    assert settings_service.appearance_preview_image_key() == key
    assert key in objects

    assert settings_service.set_appearance_preview_enabled(True) is True
    assert settings_service.appearance_preview_image_key() == key


@pytest.mark.asyncio
async def test_enabling_without_an_image_offers_upload(persisted):
    assert settings_service.set_appearance_preview_enabled(True) is True
    status = appearance_service.preview_status()
    assert status["enabled"] is True and status["stored"] is False
    assert status["active"] is False
    _t, body, _b = appearance._preview_view()
    assert "without a selected image" in body


# ── preview URL and metadata ────────────────────────────────────────────────

def _key(monkey: str = "a" * 32, ext: str = "png") -> str:
    return f"appearance/preview/{monkey}.{ext}"


def test_preview_url_is_versioned_and_https(persisted, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://lifeos.example")
    settings_service.set_appearance_preview_image_key(_key())
    settings_service.set_appearance_preview_enabled(True)

    url = appearance_service.preview_url()
    assert url == f"https://lifeos.example/appearance/preview/{'a' * 32}"
    assert appearance_service.preview_active() is True

    settings_service.set_appearance_preview_image_key(_key("b" * 32))
    assert appearance_service.preview_url().endswith("b" * 32)


def test_preview_disabled_produces_text_only(persisted, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://lifeos.example")
    settings_service.set_appearance_preview_image_key(_key())
    assert settings_service.appearance_preview_enabled() is False

    title, body = appearance_service.with_preview("menu", "", "LifeOS", "Choose a category:")
    assert title == "LifeOS" and body == "Choose a category:"
    assert panels._appearance_link_preview("menu") is False


def test_non_https_public_base_is_refused(persisted, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "http://insecure.example")
    settings_service.set_appearance_preview_image_key(_key())
    settings_service.set_appearance_preview_enabled(True)
    assert appearance_service.public_base_url() == ""
    assert appearance_service.preview_active() is False


def test_preview_metadata_and_page(persisted, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://lifeos.example")
    settings_service.set_appearance_preview_image_key(_key())
    appearance_service.set_header("title", "Owner header")
    appearance_service.set_header("body", "Owner description")

    meta = appearance_service.preview_meta("a" * 32)
    assert meta is not None
    assert meta["title"] == "Owner header"
    assert meta["description"] == "Owner description"
    assert meta["image_url"] == f"https://lifeos.example/appearance/image/{'a' * 32}"
    assert meta["url"] == "https://lifeos.example/guide/"

    page = appearance_service.preview_html(meta)
    for tag in ('property="og:title"', 'property="og:description"',
                'property="og:image"', 'property="og:url"'):
        assert tag in page
    assert "/guide/" in page
    assert appearance_service.preview_meta("b" * 32) is None


@pytest.mark.asyncio
async def test_preview_web_routes(persisted, storage, monkeypatch):
    from fastapi.testclient import TestClient

    from backend.web.app import app

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://lifeos.example")
    objects, _calls = storage
    await appearance_service.install_image(png_bytes(2, 2))
    version = appearance_service.preview_version()
    assert version

    client = TestClient(app)
    page = client.get(f"/appearance/preview/{version}")
    assert page.status_code == 200
    assert "og:image" in page.text and "/guide/" in page.text

    image = client.get(f"/appearance/image/{version}")
    assert image.status_code == 200
    assert image.headers["content-type"].startswith("image/png")
    assert image.content[:8] == b"\x89PNG\r\n\x1a\n"

    assert client.get("/appearance/preview/unknown").status_code == 404
    assert client.get("/appearance/image/unknown").status_code == 404


# ── inline rendering ────────────────────────────────────────────────────────

def test_inline_result_is_text_only_by_default():
    from backend.helper.panel_render import render

    result = render("LifeOS", "Choose a category:", [])
    appearance_service.apply_to_inline_results("menu", "", [result])
    assert result.send_message.no_webpage is True
    assert "appearance/preview" not in result.send_message.message


def test_inline_result_carries_preview_and_keeps_the_keyboard(persisted, monkeypatch):
    from backend.helper.panel_render import render

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://lifeos.example")
    settings_service.set_appearance_preview_image_key(_key())
    settings_service.set_appearance_preview_enabled(True)

    builder = panels.InlinePanelBuilder()
    builder.add_row("Save", "panel:save")
    result = render("LifeOS", "Choose a category:", builder.build())
    before = [len(_buttons_of(row)) for row in result.send_message.reply_markup.rows]
    before_data = _data(result.send_message.reply_markup.rows)

    appearance_service.apply_to_inline_results("menu", "", [result])

    assert result.send_message.no_webpage is False
    assert appearance_service.preview_url() in result.send_message.message
    assert [len(_buttons_of(row)) for row in result.send_message.reply_markup.rows] == before
    assert _data(result.send_message.reply_markup.rows) == before_data
    assert "panel:save" in before_data


def test_inline_composition_respects_page_override(persisted):
    from backend.helper.panel_render import render

    appearance_service.set_page_text("menu", "title", "Owner home")
    result = render("LifeOS", "Choose a category:", [])
    appearance_service.apply_to_inline_results("menu", "", [result])
    assert result.send_message.message.startswith("**Owner home**")
    assert "Choose a category:" in result.send_message.message


# ── Helper-Bot-only editing ────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_panel_repaint_uses_the_helper_bot_only(monkeypatch, persisted):
    recorded: dict = {}

    class _Editor:
        def __init__(self, **kwargs):
            recorded.update(kwargs)
            self.available = True

        async def edit(self, text, buttons=None):
            recorded["text"] = text
            recorded["buttons"] = buttons
            return True

    monkeypatch.setattr("backend.helper.panel_edit.ExistingPanelEditor", _Editor)
    helper = object()
    monkeypatch.setattr("backend.helper.client.get_client", lambda: helper)

    ok = await appearance._paint_input("inline-handle", appearance._header_view())

    assert ok is True
    assert recorded["inline_id"] == "inline-handle"
    assert recorded["helper"] is helper
    assert recorded.get("self_client") is None
    assert "**Header Customization**" in recorded["text"]


@pytest.mark.asyncio
async def test_unavailable_helper_reports_and_never_falls_back(monkeypatch, persisted):
    calls: list = []

    class _Editor:
        def __init__(self, **kwargs):
            self.available = False
            calls.append(kwargs)

        async def edit(self, text, buttons=None):  # pragma: no cover
            raise AssertionError("no editor may be used when unavailable")

    monkeypatch.setattr("backend.helper.panel_edit.ExistingPanelEditor", _Editor)
    monkeypatch.setattr("backend.helper.client.get_client", lambda: None)

    ok = await appearance._paint_input(None, appearance._header_view())

    assert ok is False
    assert calls and calls[0].get("self_client") is None


# ── image input path ────────────────────────────────────────────────────────

class _File:
    def __init__(self, size):
        self.size = size


class _Downloader:
    def __init__(self, data, chunk=7):
        self._data = data
        self._chunk = chunk

    async def __aiter__(self):
        for start in range(0, len(self._data), self._chunk):
            yield self._data[start:start + self._chunk]

    async def aclose(self):
        return None


class _Event:
    def __init__(self, data=b"", media=True, size=None, mime="image/png"):
        self.media = object() if media else None
        self.file = _File(size if size is not None else len(data))
        self.message = type("M", (), {
            "id": 1,
            "media": self.media,
            "photo": None,
            "document": type("D", (), {"mime_type": mime})() if media else None,
        })()
        self.client = type("C", (), {
            "iter_download": lambda _self, media, chunk_size=None: _Downloader(data),
        })()
        self._data = data
        self._mime = mime


@pytest.mark.asyncio
async def test_image_input_stores_original_bytes_without_a_temp_file(
    persisted, storage, monkeypatch
):
    monkeypatch.setattr("backend.helper.client.get_client", lambda: object())
    objects, _calls = storage
    painted: list = []

    async def _paint(inline_id, view):
        painted.append(view)
        return True

    monkeypatch.setattr(appearance, "_paint_input", _paint)
    # An upload must never need a temporary file (the old download_media +
    # read_bytes path); creating one here would fail the test.
    monkeypatch.setattr(
        tempfile, "mkdtemp", lambda **kwargs: (_ for _ in ()).throw(AssertionError("no temp dir"))
    )

    original = png_bytes(2, 2)
    await appearance._image_input(
        "", CHAT, 1, CHAT, PANEL_MSG, event=_Event(original),
    )

    key = settings_service.appearance_preview_image_key()
    assert key in objects and objects[key] == original
    assert painted and "uploaded" in painted[-1][1]


@pytest.mark.asyncio
async def test_image_input_rejects_non_media_and_missing_helper(persisted, monkeypatch):
    painted: list = []

    async def _paint(inline_id, view):
        painted.append(view)
        return True

    monkeypatch.setattr(appearance, "_paint_input", _paint)
    await appearance._image_input("", CHAT, 1, CHAT, PANEL_MSG, event=_Event(media=False))
    assert painted and "Send a PNG, JPEG or WebP" in painted[-1][1]

    await appearance._image_input("", CHAT, 1, CHAT, PANEL_MSG, event=None)
    assert "Send a PNG" in painted[-1][1]


@pytest.mark.asyncio
async def test_input_listener_ignores_non_owner(monkeypatch, persisted):
    from backend.helper import inline_sender, input_state

    captured: dict = {}

    class _Client:
        def on(self, _event):
            def _register(fn):
                captured["handler"] = fn
                return fn
            return _register

    inline_sender.register_input_listener(_Client(), OWNER)

    async def _handler(text, *args, **kwargs):  # pragma: no cover - not called
        raise AssertionError("a non-owner input must never reach the handler")

    input_state.set_pending(OWNER, "appearance", _handler, CHAT, "prompt")

    class _Incoming:
        sender_id = OWNER + 1
        chat_id = CHAT
        raw_text = "hacked"
        message = type("M", (), {"id": 9})()

    await captured["handler"](_Incoming())

    assert settings_service.appearance_header_title() == ""
    assert input_state.has_pending(OWNER) is True
