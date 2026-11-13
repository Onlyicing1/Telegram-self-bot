"""The selected Appearance image is downloaded and encoded ONCE per version.

Delivery used to re-read the stored object on every request: ``image_payload``,
``telegram_photo`` and ``telegram_thumb`` each downloaded it, and the photo and
thumbnail paths downloaded the same original independently. These tests count
the real Storage calls through the service/route paths and prove the bounded
in-memory cache: one download, one encode per rendition, single-flight under
concurrency, and no stale bytes after any selection change.
"""
from __future__ import annotations

import asyncio
import io
import struct
import zlib

import pytest

from backend.helper.panel_render import render
from backend.services import (
    appearance_service,
    panel_settings_repository,
    settings_service,
)

CHAT = -1001234567890
MSG = 700


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
    monkeypatch.setattr(
        panel_settings_repository, "load", lambda: dict(row) if row else None
    )
    yield
    appearance_service.reset_delivery_state()


class _Bucket:
    """A counting Storage bucket: downloads/uploads are the evidence."""

    def __init__(self):
        self.objects: dict = {}
        self.downloads = 0
        self.uploads = 0
        self.download_error: Exception | None = None
        self.delay = 0.0

    def upload(self, path, file, options=None):
        self.uploads += 1
        self.objects[path] = bytes(file)

    def remove(self, paths):
        for path in paths:
            self.objects.pop(path, None)

    def download(self, path):
        self.downloads += 1
        if self.delay:
            import time

            time.sleep(self.delay)
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

    bucket = _Bucket()
    monkeypatch.setattr(db_client, "get_db", lambda: _DB(bucket))
    monkeypatch.setenv("APPEARANCE_MEDIA_BUCKET", "appearance-assets")
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://lifeos.example")
    return bucket


def png_bytes(width: int = 8, height: int = 6, colour=(10, 120, 200)) -> bytes:
    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    raw = b"".join(b"\x00" + bytes(colour) * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
        + chunk(b"IDAT", zlib.compress(raw))
        + chunk(b"IEND", b"")
    )


async def _install(store, data: bytes | None = None) -> str:
    ok, notice = await appearance_service.install_image(data or png_bytes())
    assert ok is True, notice
    assert settings_service.set_appearance_preview_enabled(True) is True
    return appearance_service.preview_version()


def _stored(store, version: str) -> bytes:
    return store.objects[f"appearance/preview/{version}.png"]


# ── one download, reused ───────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_first_request_downloads_and_repeats_reuse_the_bytes(store):
    version = await _install(store)
    assert store.downloads == 0  # selecting an upload reads no object

    first = await appearance_service.image_payload(version)
    assert first is not None
    assert store.downloads == 1
    assert first[0] == _stored(store, version)

    for _ in range(3):
        assert await appearance_service.image_payload(version) == first
    assert store.downloads == 1


@pytest.mark.asyncio
async def test_photo_and_thumbnail_share_one_original_download(store):
    version = await _install(store)
    photo = await appearance_service.telegram_photo(version)
    assert photo is not None
    assert store.downloads == 1

    thumb = await appearance_service.telegram_thumb(version)
    assert thumb is not None
    assert store.downloads == 1  # encoded from the SAME cached original

    payload = await appearance_service.image_payload(version)
    assert payload is not None and payload[0] == _stored(store, version)
    assert store.downloads == 1

    # Repeated renditions are cache hits too (no re-encode, no download).
    assert await appearance_service.telegram_photo(version) == photo
    assert await appearance_service.telegram_thumb(version) == thumb
    assert store.downloads == 1


@pytest.mark.asyncio
async def test_concurrent_requests_share_one_download_and_one_encode(store, monkeypatch):
    version = await _install(store)
    store.delay = 0.02  # keep the first download in flight while others arrive
    encodes: list = []
    real = appearance_service._encode_telegram_photo

    def _counting(data):
        encodes.append(len(data))
        return real(data)

    monkeypatch.setattr(appearance_service, "_encode_telegram_photo", _counting)

    results = await asyncio.gather(
        *[appearance_service.telegram_photo(version) for _ in range(6)]
    )

    assert all(result for result in results)
    assert store.downloads == 1
    assert len(encodes) == 1


@pytest.mark.asyncio
async def test_concurrent_payload_photo_and_thumb_share_one_download(store, monkeypatch):
    version = await _install(store)
    store.delay = 0.02
    thumbs: list = []
    real_thumb = appearance_service._encode_telegram_thumb

    def _counting_thumb(data):
        thumbs.append(len(data))
        return real_thumb(data)

    monkeypatch.setattr(appearance_service, "_encode_telegram_thumb", _counting_thumb)

    payload, photo, thumb = await asyncio.gather(
        appearance_service.image_payload(version),
        appearance_service.telegram_photo(version),
        appearance_service.telegram_thumb(version),
    )

    assert payload is not None and photo is not None and thumb is not None
    assert store.downloads == 1
    assert len(thumbs) == 1


@pytest.mark.asyncio
async def test_the_requested_image_is_a_valid_jpeg_rendition(store):
    from PIL import Image

    version = await _install(store, png_bytes(40, 30))
    photo = await appearance_service.telegram_photo(version)
    thumb = await appearance_service.telegram_thumb(version)
    with Image.open(io.BytesIO(photo)) as decoded:
        assert decoded.format == "JPEG"
        assert decoded.size == (40, 30)
    with Image.open(io.BytesIO(thumb)) as decoded:
        assert decoded.format == "JPEG"
        assert max(decoded.size) <= appearance_service.TELEGRAM_THUMB_MAX_SIDE
    # Delivery never changed the stored original.
    assert _stored(store, version) == png_bytes(40, 30)


# ── selection changes can never serve stale bytes ──────────────────────────

@pytest.mark.asyncio
async def test_selecting_another_image_drops_the_previous_bytes(store):
    first = await _install(store)
    first_bytes = _stored(store, first)
    assert await appearance_service.telegram_photo(first) is not None
    assert appearance_service.cached_image_bytes() > 0

    second = await _install(store, png_bytes(9, 7, (200, 40, 40)))
    assert second != first
    # Uploading + selecting a new image invalidates the old selection's cache.
    assert appearance_service.cached_image_bytes() == 0
    assert await appearance_service.telegram_photo(first) is None
    assert await appearance_service.image_payload(first) is None

    payload = await appearance_service.image_payload(second)
    assert payload is not None
    assert payload[0] == _stored(store, second)
    assert payload[0] != first_bytes


@pytest.mark.asyncio
async def test_selecting_an_existing_image_back_serves_that_image(store):
    first = await _install(store)
    first_bytes = _stored(store, first)
    second = await _install(store, png_bytes(9, 7, (200, 40, 40)))
    assert await appearance_service.image_payload(second) is not None

    ok, notice = await appearance_service.select_image(first)
    assert ok is True, notice
    assert appearance_service.preview_version() == first
    assert appearance_service.cached_image_bytes() == 0  # no second's bytes kept

    payload = await appearance_service.image_payload(first)
    assert payload is not None and payload[0] == first_bytes


@pytest.mark.asyncio
async def test_deleting_the_active_image_prevents_stale_delivery(store):
    version = await _install(store)
    assert await appearance_service.telegram_photo(version) is not None

    ok, notice = await appearance_service.delete_image(version)
    assert ok is True, notice
    assert appearance_service.cached_image_bytes() == 0
    assert await appearance_service.telegram_photo(version) is None
    assert await appearance_service.image_payload(version) is None


@pytest.mark.asyncio
async def test_clearing_the_selection_prevents_stale_delivery(store):
    version = await _install(store)
    assert await appearance_service.telegram_thumb(version) is not None

    ok, notice = await appearance_service.clear_selection()
    assert ok is True, notice
    assert appearance_service.cached_image_bytes() == 0
    assert await appearance_service.telegram_thumb(version) is None
    assert await appearance_service.image_payload(version) is None


# ── bounds, safety and the text fallback ───────────────────────────────────

@pytest.mark.asyncio
async def test_cache_memory_stays_within_the_budget(store):
    version = await _install(store)
    photo = await appearance_service.telegram_photo(version)
    thumb = await appearance_service.telegram_thumb(version)
    payload = await appearance_service.image_payload(version)

    assert photo is not None and thumb is not None and payload is not None
    assert appearance_service.cached_image_bytes() == (
        len(payload[0]) + len(photo) + len(thumb)
    )
    assert appearance_service.cached_image_bytes() <= (
        appearance_service.IMAGE_CACHE_BUDGET_BYTES
    )
    # One selected image only: the budget covers exactly original + 5 MiB photo
    # + a bounded thumbnail.
    assert appearance_service.IMAGE_CACHE_BUDGET_BYTES >= (
        appearance_service.MAX_IMAGE_BYTES
        + appearance_service.TELEGRAM_PHOTO_MAX_BYTES
    )


@pytest.mark.asyncio
async def test_an_image_that_cannot_fit_the_budget_is_still_delivered(store, monkeypatch):
    version = await _install(store)
    monkeypatch.setattr(appearance_service, "IMAGE_CACHE_BUDGET_BYTES", 8)

    photo = await appearance_service.telegram_photo(version)
    thumb = await appearance_service.telegram_thumb(version)
    payload = await appearance_service.image_payload(version)

    assert photo is not None and thumb is not None and payload is not None
    assert appearance_service.cached_image_bytes() <= 8


@pytest.mark.asyncio
async def test_a_cache_failure_keeps_the_safe_text_fallback(store):
    version = await _install(store)
    store.download_error = RuntimeError("storage down")

    assert await appearance_service.telegram_photo(version) is None
    assert await appearance_service.image_payload(version) is None
    assert appearance_service.cached_image_bytes() == 0

    built = await appearance_service.apply_to_inline_results(
        "menu", "", [render("LifeOS", "Choose a category:", [])]
    )
    assert built and built[0].send_message is not None
    store.download_error = None
    assert await appearance_service.telegram_photo(version) is not None


@pytest.mark.asyncio
async def test_ordinary_delivery_never_uploads_anything(store):
    version = await _install(store)
    assert store.uploads == 1  # the owner's own upload

    await appearance_service.telegram_photo(version)
    await appearance_service.telegram_thumb(version)
    await appearance_service.image_payload(version)
    await appearance_service.apply_to_inline_results(
        "menu", "", [render("LifeOS", "Choose a category:", [])]
    )
    await appearance_service.telegram_photo(version)

    assert store.uploads == 1


@pytest.mark.asyncio
async def test_the_gallery_is_not_prefetched(store):
    first = await _install(store)
    second = await _install(store, png_bytes(9, 7, (200, 40, 40)))
    assert store.downloads == 0  # two stored images, nothing read yet

    assert await appearance_service.telegram_photo(first) is None  # not selected
    assert store.downloads == 0

    assert await appearance_service.telegram_photo(second) is not None
    assert store.downloads == 1  # only the SELECTED image was read
