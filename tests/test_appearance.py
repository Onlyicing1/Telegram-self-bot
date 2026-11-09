"""Glass UI Appearance — central text composition, preview media and storage.

Every test drives REAL production code: the settings service and its
repository contract, the panel renderer, the web preview routes, the image
prober and the Appearance handler/registration. Telegram, Supabase and the
network are replaced by small fakes; nothing here claims live Telegram
behaviour.
"""
from __future__ import annotations

import asyncio
import json
import struct
import zlib
from types import SimpleNamespace

import pytest
from telethon import types
from telethon.tl.functions.messages import EditInlineBotMessageRequest

from backend.helper import symbols as sym
from backend.helper.panel_registry import registry as get_registry
from backend.helper.panel_render import render
from backend.services import appearance_service as appearance
from backend.services import settings_service
from backend.services.image_probe import MAX_IMAGE_BYTES, probe_image


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


@pytest.fixture()
def stored(monkeypatch):
    """A repository contract that really persists, plus a clean cache."""
    from backend.services import panel_settings_repository as repo

    store: dict = {}

    def update_field(key, value):
        if not settings_service.is_valid_key(key):
            return False
        store[key] = value
        return True

    def update_fields(fields):
        for key, value in fields.items():
            if not settings_service.is_valid_key(key):
                return False
            store[key] = value
        return True

    monkeypatch.setattr(repo, "update_field", update_field)
    monkeypatch.setattr(repo, "update_fields", update_fields)
    monkeypatch.setattr(repo, "load", lambda: {**settings_service._DEFAULTS, **store})

    monkeypatch.setattr(settings_service, "_cache", dict(settings_service._DEFAULTS))
    monkeypatch.setattr(settings_service, "_loaded", True)
    return store


def _png(width: int, height: int) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    chunk = b"IHDR" + ihdr
    return (
        b"\x89PNG\r\n\x1a\n"
        + struct.pack(">I", len(ihdr))
        + chunk
        + struct.pack(">I", zlib.crc32(chunk) & 0xFFFFFFFF)
    )


def _jpeg(width: int, height: int) -> bytes:
    return (
        b"\xff\xd8\xff\xc0"
        + struct.pack(">H", 8)
        + b"\x08"
        + struct.pack(">HH", height, width)
        + b"\x03"
    )


def _webp(width: int, height: int) -> bytes:
    payload = b"\x00\x00\x00\x00" + (width - 1).to_bytes(3, "little") + (height - 1).to_bytes(3, "little")
    return b"RIFF" + struct.pack("<I", 4 + 8 + len(payload)) + b"WEBP" + b"VP8X" + struct.pack("<I", len(payload)) + payload


# ── Navigation ───────────────────────────────────────────────────────────────

def test_menu_gains_exactly_one_appearance_entry():
    from backend.bot.handlers.misc import _build_menu_buttons

    datas = [
        str(getattr(button, "data", b"").decode() if isinstance(getattr(button, "data", None), bytes) else "")
        for row in _build_menu_buttons() for button in row
    ]
    assert datas.count("panel:appearance") == 1
    assert "panel:save" in datas and "panel:ai" in datas and "panel:settings" in datas


def test_appearance_panels_are_registered_with_the_existing_registry():
    from backend.bot.handlers import appearance as handler
    from backend.helper.panels import get_panel

    handler.register(client=None, owner_id=777)
    for panel_id in ("appearance", "appearance_header", "appearance_pages", "appearance_page", "appearance_preview"):
        assert get_registry().has(panel_id)
        assert get_panel(panel_id) is not None


def test_non_owner_cannot_open_or_change_settings(monkeypatch):
    from backend.bot.handlers import appearance as handler

    monkeypatch.setattr(handler, "get_owner_id", lambda: 777)
    event = SimpleNamespace(sender_id=999)

    assert _run(handler._appearance_panel_handler(event, "")) is None
    assert _run(handler._header_reset_all_action(event, "", 1)) is None
    assert _run(handler._preview_toggle_action(event, "", 1)) is None


# ── Global header + per-page text ───────────────────────────────────────────

def test_defaults_are_untouched_before_any_customization(stored):
    assert appearance.compose("menu", "LifeOS", "Choose a category:") == ("LifeOS", "Choose a category:")
    assert appearance.compose("general", "General", "**General**\n\nTap a button.") == (
        "General", "**General**\n\nTap a button."
    )


def test_global_header_applies_to_pages_and_menu(stored):
    assert appearance.set_header_title("Studio")
    assert appearance.set_header_body("Pick a workspace:")
    assert appearance.compose("menu", "LifeOS", "Choose a category:") == ("Studio", "Pick a workspace:")
    title, body = appearance.compose("db", "Database", "Choose an action:")
    assert title == "Studio"
    assert body == "Pick a workspace:"


def test_page_override_wins_over_global_and_keeps_dynamic_content(stored):
    assert appearance.set_header_title("Studio")
    assert appearance.set_page_text("general", "title", "Control")
    assert appearance.set_page_text("general", "body", "Tap an action. This paragraph is static.")

    title, body = appearance.compose("general", "General", "**General**\n\nTap a button.\n\nStatus: 3 items")
    assert title == "Control"
    assert body.startswith("Tap an action. This paragraph is static.")
    assert "Status: 3 items" in body
    assert "**General**" not in body  # duplicate title removed


def test_pages_are_independent_and_title_only_keeps_default_body(stored):
    assert appearance.set_page_text("save", "title", "Archive")
    assert appearance.set_page_text("ai", "body", "Ask anything.")

    save_title, save_body = appearance.compose(
        "save", "Save", "Deep Save downloads the message and re-uploads it as a new Saved Messages message."
    )
    ai_title, ai_body = appearance.compose("ai", "AI", "**AI**\n\nProvider: X")
    db_title, db_body = appearance.compose("db", "Database", "Choose an action:")

    assert save_title == "Archive"
    assert "Deep Save downloads" in save_body
    assert ai_title == "AI"
    assert ai_body == "Ask anything.\n\nProvider: X"
    assert (db_title, db_body) == ("Database", "Choose an action:")


def test_reset_restores_the_real_defaults(stored):
    assert appearance.set_page_text("save", "title", "Archive")
    assert appearance.set_page_text("save", "body", "Short.")
    assert appearance.reset_page_text("save", "title")
    real_save_body = "Deep Save downloads the message and re-uploads it as a new Saved Messages message."
    title, body = appearance.compose("save", "Save", real_save_body)
    assert title == "Save"
    assert body == "Short."
    assert appearance.reset_page_text("save")
    assert appearance.compose("save", "Save", real_save_body) == ("Save", real_save_body)

    assert appearance.set_header_title("Studio")
    assert appearance.reset_header()
    assert appearance.compose("menu", "LifeOS", "Choose a category:") == ("LifeOS", "Choose a category:")


def test_invalid_or_oversized_text_is_rejected_without_corruption(stored):
    assert appearance.set_header_title("ok")
    assert not appearance.set_header_title("x" * (appearance.TITLE_MAX + 1))
    assert appearance.header_title() == "ok"

    assert not appearance.set_page_text("save", "body", "x" * (appearance.BODY_MAX + 1))
    assert appearance.page_override("save", "body") == ""


def test_unknown_page_ids_are_rejected(stored):
    assert not appearance.set_page_text("not_a_panel", "title", "X")
    assert not appearance.set_page_text("save", "something", "X")
    assert not appearance.reset_page_text("list")


def test_repository_rejection_is_reported_and_never_cached(stored, monkeypatch):
    from backend.services import panel_settings_repository as repo

    monkeypatch.setattr(repo, "update_field", lambda key, value: False)
    assert appearance.set_header_title("Studio") is False
    assert appearance.header_title() == ""
    assert "appearance_header_title" not in stored


def test_settings_survive_a_cache_reload(stored):
    assert appearance.set_page_text("settings", "title", "Controls")
    assert appearance.set_preview_enabled(True)

    settings_service._cache = {}
    settings_service._loaded = False
    settings_service.load_all()

    assert appearance.page_override("settings", "title") == "Controls"
    assert appearance.preview_enabled() is True


# ── Preview identity + rendering ────────────────────────────────────────────

def _key(char: str = "a") -> str:
    return f"previews/{char * 32}.png"


def test_preview_switch_is_independent_of_the_stored_image(stored):
    assert appearance.set_preview_image(_key(), {"mime": "image/png", "width": 640, "height": 360})
    assert appearance.set_preview_enabled(False)
    assert appearance.preview_enabled() is False
    assert appearance.preview_image_key() == _key()

    assert appearance.set_preview_enabled(True)
    assert appearance.preview_image_key() == _key()


def test_preview_version_changes_when_the_image_is_replaced(stored):
    assert appearance.set_preview_image(_key("a"), {"mime": "image/png", "width": 640, "height": 360})
    first = appearance.preview_version()
    assert appearance.set_preview_image(_key("b"), {"mime": "image/png", "width": 640, "height": 360})
    assert appearance.preview_version() != first


def test_render_uses_web_page_media_only_when_preview_is_ready(stored, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    assert appearance.set_preview_image(_key(), {"mime": "image/png", "width": 640, "height": 360})

    text_only = render("LifeOS", "Choose a category:", [], panel_id="menu")
    assert isinstance(text_only.send_message, types.InputBotInlineMessageText)

    assert appearance.set_preview_enabled(True)
    media_result = render("LifeOS", "Choose a category:", [], panel_id="menu")
    sent = media_result.send_message
    assert isinstance(sent, types.InputBotInlineMessageMediaWebPage)
    assert sent.url.startswith("https://example.test/preview/")
    assert sent.url.endswith("/menu")
    assert sent.force_small_media is True


def test_link_preview_disabled_is_genuine_text_only(stored, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    assert appearance.set_preview_image(_key(), {"mime": "image/png", "width": 640, "height": 360})
    assert appearance.preview_page_url("menu") == ""  # switch still OFF


def test_og_document_carries_the_required_metadata(stored, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    assert appearance.set_preview_image(_key(), {"mime": "image/png", "width": 640, "height": 360})
    assert appearance.set_preview_enabled(True)
    document = appearance.og_html("menu")
    assert 'property="og:title"' in document
    assert 'property="og:description"' in document
    assert 'property="og:image"' in document
    assert f'content="https://example.test/preview/{appearance.preview_version()}/menu/image"' in document
    assert 'property="og:image:width" content="640"' in document
    assert 'property="og:url" content="https://example.test/guide"' in document


# ── Image validation + storage ──────────────────────────────────────────────

@pytest.mark.parametrize("payload,mime", [
    (_png(640, 360), "image/png"),
    (_jpeg(800, 600), "image/jpeg"),
    (_webp(320, 240), "image/webp"),
])
def test_real_image_headers_are_accepted(payload, mime):
    result = probe_image(payload)
    assert result is not None and result.mime == mime


def test_malformed_and_oversized_images_are_rejected():
    assert probe_image(b"") is None
    assert probe_image(b"<svg xmlns='http://www.w3.org/2000/svg'></svg>") is None
    assert probe_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * 8) is None
    assert probe_image(_png(5000, 5000)) is None
    assert probe_image(b"\x89PNG\r\n\x1a\n" + b"\x00" * (MAX_IMAGE_BYTES + 1)) is None


def test_storage_uploads_and_removes_by_key(monkeypatch):
    from backend.services import preview_storage

    calls = []

    class _Storage:
        def from_(self, bucket):
            calls.append(("bucket", bucket))
            return self

        def upload(self, key, data, options):
            calls.append(("upload", key, data, options))
            return {}

        def remove(self, keys):
            calls.append(("remove", list(keys)))
            return {}

    monkeypatch.setenv("PANEL_PREVIEW_BUCKET", "panel-previews")
    monkeypatch.setattr(preview_storage.db_client, "get_db", lambda: SimpleNamespace(storage=_Storage()))

    key = preview_storage.object_key("png")
    assert key.startswith("previews/") and key.endswith(".png")
    assert preview_storage.available() is True
    assert preview_storage.upload(key, _png(64, 64), "image/png") is True
    assert preview_storage.remove(key) is True
    assert ("upload", key, _png(64, 64), {"content-type": "image/png", "upsert": "false"}) in calls
    assert ("remove", [key]) in calls


def test_cleanup_after_a_rejected_reference(stored, monkeypatch):
    from backend.bot.handlers import appearance as handler
    from backend.services import preview_storage

    removed = []
    monkeypatch.setattr(preview_storage, "available", lambda: True)
    monkeypatch.setattr(preview_storage, "upload", lambda *a: True)
    monkeypatch.setattr(preview_storage, "remove", lambda key: removed.append(key) or True)
    monkeypatch.setattr(handler, "get_client", lambda: None)
    monkeypatch.setattr(handler, "get_self_client", lambda: None)
    monkeypatch.setattr(handler, "_delete_upload", lambda *a: asyncio.sleep(0))
    monkeypatch.setattr(appearance, "set_preview_image", lambda key, meta: False)

    message = SimpleNamespace(
        media=object(),
        photo=object(),
        document=None,
        file=SimpleNamespace(size=1024),
        download_media=None,
    )

    async def _download(_message):
        return _png(64, 64)

    monkeypatch.setattr(handler, "_download_upload", _download)
    _run(handler._preview_upload_input("", 1, 2, 3, 4, inline_id=None, message=message))
    assert len(removed) == 1  # the orphaned upload was removed


# ── Helper-Bot-only editing ─────────────────────────────────────────────────

def test_panel_repaint_uses_only_the_helper_inline_request(stored, monkeypatch):
    from backend.bot.handlers import appearance as handler

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    assert appearance.set_preview_image(_key(), {"mime": "image/png", "width": 640, "height": 360})
    assert appearance.set_preview_enabled(True)

    sent = []

    class _Helper:
        async def __call__(self, request):
            sent.append(request)
            return None

    inline_id = object()
    ok = _run(handler._paint_existing(
        _Helper(), inline_id, "appearance_preview", 10, 20,
        "Preview Image", "Body", [],
    ))
    assert ok is True
    assert len(sent) == 1
    request = sent[0]
    assert isinstance(request, EditInlineBotMessageRequest)
    assert request.id is inline_id
    assert isinstance(request.media, types.InputMediaWebPage)
    assert request.reply_markup is not None


def test_repaint_without_an_inline_identifier_reports_failure(stored):
    from backend.bot.handlers import appearance as handler

    class _Helper:
        async def __call__(self, request):  # pragma: no cover — must not be called
            raise AssertionError("no edit may happen without an inline identifier")

    assert _run(handler._paint_existing(
        _Helper(), None, "appearance_preview", 10, 20, "T", "B", [],
    )) is False


# ── Web preview routes ──────────────────────────────────────────────────────

def _web_client(stored, monkeypatch, payload: bytes = b"\x89PNGdata"):
    from fastapi.testclient import TestClient

    from backend.services import preview_storage
    from backend.web.app import app

    monkeypatch.setenv("PUBLIC_BASE_URL", "https://example.test")
    assert appearance.set_preview_image(_key(), {"mime": "image/png", "width": 640, "height": 360})
    assert appearance.set_preview_enabled(True)
    monkeypatch.setattr(preview_storage, "download", lambda key: payload)
    return TestClient(app), appearance.preview_version()


def test_preview_page_serves_og_metadata_to_telegram(stored, monkeypatch):
    client, version = _web_client(stored, monkeypatch)
    response = client.get(
        f"/preview/{version}/menu", headers={"user-agent": "TelegramBot (like TwitterBot)"},
    )
    assert response.status_code == 200
    assert "og:image" in response.text
    assert f"/preview/{version}/menu/image" in response.text


def test_preview_page_redirects_a_human_to_the_guide(stored, monkeypatch):
    client, version = _web_client(stored, monkeypatch)
    response = client.get(
        f"/preview/{version}/menu", headers={"user-agent": "Mozilla/5.0"},
        follow_redirects=False,
    )
    assert response.status_code == 302
    assert response.headers["location"] == "/guide"


def test_preview_image_route_serves_bytes_and_rejects_stale_versions(stored, monkeypatch):
    client, version = _web_client(stored, monkeypatch, b"\x89PNG-real-bytes")
    response = client.get(f"/preview/{version}/menu/image")
    assert response.status_code == 200
    assert response.content == b"\x89PNG-real-bytes"
    assert response.headers["content-type"].startswith("image/png")
    assert client.get("/preview/deadbeefdeadbeef/menu/image").status_code == 404


def test_preview_routes_are_404_when_the_switch_is_off(stored, monkeypatch):
    client, version = _web_client(stored, monkeypatch)
    assert appearance.set_preview_enabled(False)
    assert client.get(f"/preview/{version}/menu/image").status_code == 404


# ── Icons ───────────────────────────────────────────────────────────────────

def test_new_symbols_are_unicode_and_not_emoji():
    assert sym.ICON_APPEARANCE == "◑"
    assert sym.OP_UPLOAD == "⇧"
    assert "\ufe0f" not in sym.ICON_APPEARANCE + sym.OP_UPLOAD
    assert sym.label(sym.ICON_APPEARANCE, "Appearance") == "◑ Appearance"
