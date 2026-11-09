"""Appearance preview gallery — Storage listing, selection, deletion and upload.

Every test drives the REAL service functions against a fake Storage client that
mirrors the installed storage3 surface (``from_().list/upload/download/remove``)
and real image bytes the installed Pillow build can open.
"""
from __future__ import annotations

import asyncio
import io
import struct
import uuid
import zlib

import pytest
from PIL import Image

from backend.bot.handlers import appearance
from backend.db import client as db_client
from backend.helper import symbols as sym
from backend.services import (
    appearance_service,
    panel_settings_repository,
    settings_service,
)

CHAT = -1001234567890
PANEL_MSG = 501
BUCKET = "appearance-assets"
PREFIX = appearance_service.IMAGES_PREFIX


# ── image bytes ─────────────────────────────────────────────────────────────

def _chunk(tag: bytes, data: bytes) -> bytes:
    return (
        struct.pack(">I", len(data))
        + tag
        + data
        + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
    )


def png(width: int = 4, height: int = 3) -> bytes:
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    raw = b"".join(b"\x00" + b"\x11\x22\x33" * width for _ in range(height))
    return (
        b"\x89PNG\r\n\x1a\n"
        + _chunk(b"IHDR", ihdr)
        + _chunk(b"IDAT", zlib.compress(raw))
        + _chunk(b"IEND", b"")
    )


def png_header(width: int, height: int) -> bytes:
    """A structurally-claimed PNG with no pixel stream (bounds are read first)."""
    ihdr = struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + _chunk(b"IHDR", ihdr) + _chunk(b"IEND", b"")


def encoded(fmt: str, size: tuple[int, int] = (32, 24)) -> bytes:
    buffer = io.BytesIO()
    Image.new("RGB", size, (10, 20, 30)).save(buffer, fmt)
    return buffer.getvalue()


# ── fake Supabase Storage ───────────────────────────────────────────────────

class _Bucket:
    def __init__(self) -> None:
        self.objects: dict[str, bytes] = {}
        self.created: dict[str, str] = {}
        self.calls: list[tuple] = []
        self.faults: dict[str, bool] = {}
        self._seq = 0

    def put(self, key: str, data: bytes) -> str:
        self.objects[key] = data
        self._seq += 1
        self.created[key] = f"2026-01-01T00:00:{self._seq:02d}Z"
        return key

    def upload(self, path, file, options=None):
        self.calls.append(("upload", path, options))
        if self.faults.get("upload"):
            raise RuntimeError("upload refused")
        self.put(path, bytes(file))
        return type("Resp", (), {"status_code": 200})()

    def remove(self, paths):
        self.calls.append(("remove", tuple(paths)))
        if self.faults.get("remove"):
            raise RuntimeError("remove refused")
        for path in paths:
            self.objects.pop(path, None)
            self.created.pop(path, None)
        return []

    def download(self, path):
        return self.objects[path]

    def list(self, path=None, options=None):
        self.calls.append(("list", path, options))
        if self.faults.get("list"):
            raise RuntimeError("list refused")
        prefix = f"{path.rstrip('/')}/" if path else ""
        entries = []
        for key in self.objects:
            if not key.startswith(prefix):
                continue
            name = key[len(prefix):]
            if "/" in name:
                continue
            entries.append({
                "name": name,
                "id": f"id-{name}",
                "created_at": self.created.get(key, ""),
                "metadata": {"size": len(self.objects[key])},
            })
        return entries


class _Storage:
    def __init__(self, bucket: _Bucket) -> None:
        self.bucket = bucket

    def from_(self, name):
        return self.bucket


@pytest.fixture
def bucket(monkeypatch):
    store = _Bucket()
    db = type("DB", (), {"storage": _Storage(store)})()
    monkeypatch.setattr(db_client, "get_db", lambda: db)
    monkeypatch.setenv("APPEARANCE_MEDIA_BUCKET", BUCKET)
    for name in ("PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL", "ARCHIVE_MEDIA_BUCKET"):
        monkeypatch.delenv(name, raising=False)
    row: dict = {}

    def _update(field, value):
        row[field] = value
        return True

    monkeypatch.setattr(panel_settings_repository, "update_field", _update)
    monkeypatch.setattr(
        panel_settings_repository, "load", lambda: dict(row) if row else None
    )
    settings_service._cache = dict(settings_service._DEFAULTS)
    settings_service._cache["appearance_page_overrides"] = {}
    settings_service._loaded = True

    def seed(count: int, fmt: str = "png") -> list[str]:
        keys = []
        for _ in range(count):
            key = f"{PREFIX}/{uuid.uuid4().hex}.{fmt}"
            store.put(key, png() if fmt == "png" else encoded(fmt.upper()))
            keys.append(key)
        return keys

    store.seed = seed  # type: ignore[attr-defined]
    return store


def _data(rows) -> list[str]:
    out: list[str] = []
    for row in rows:
        for button in row:
            value = getattr(button, "data", None)
            out.append(value.decode() if isinstance(value, bytes) else str(value))
    return out


def _labels(rows) -> list[str]:
    return [getattr(button, "text", "") for row in rows for button in row]


# ── gallery inventory ───────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_empty_gallery_is_known_and_empty(bucket):
    state = await appearance_service.gallery()
    assert state["known"] is True
    assert state["items"] == [] and state["count"] == 0
    assert state["error"] == "" and state["missing_selected"] is False


@pytest.mark.asyncio
async def test_one_stored_image_is_listed_and_selected(bucket):
    key = bucket.seed(1)[0]
    settings_service.set_appearance_preview_image_key(key)

    state = await appearance_service.gallery()
    assert state["count"] == 1
    (item,) = state["items"]
    assert item["key"] == key and item["active"] is True
    assert item["token"] == appearance_service.token_from_key(key)
    assert item["size"] > 0


@pytest.mark.asyncio
async def test_multiple_images_are_listed_with_only_one_active(bucket):
    keys = bucket.seed(4)
    settings_service.set_appearance_preview_image_key(keys[1])

    state = await appearance_service.gallery()
    assert state["count"] == 4
    assert [item["active"] for item in state["items"]].count(True) == 1
    assert next(item for item in state["items"] if item["active"])["key"] == keys[1]
    assert state["missing_selected"] is False


@pytest.mark.asyncio
async def test_ten_images_fill_the_gallery_without_eviction(bucket):
    keys = bucket.seed(10)
    state = await appearance_service.gallery()
    assert state["count"] == 10
    assert set(item["key"] for item in state["items"]) == set(keys)


@pytest.mark.asyncio
async def test_eleventh_upload_is_refused_and_changes_nothing(bucket):
    bucket.seed(10)
    before = dict(bucket.objects)

    ok, notice = await appearance_service.install_image(png())
    assert ok is False
    assert "10/10" in notice and "Delete one" in notice
    assert bucket.objects == before
    assert not any(call[0] == "upload" for call in bucket.calls)


@pytest.mark.asyncio
async def test_deleting_one_from_a_full_gallery_permits_a_new_upload(bucket):
    bucket.seed(10)
    state = await appearance_service.gallery()
    target = state["items"][0]

    ok, _notice = await appearance_service.delete_image(target["token"])
    assert ok is True
    assert len(bucket.objects) == 9

    ok, notice = await appearance_service.install_image(png())
    assert ok is True and "uploaded" in notice
    assert len(bucket.objects) == 10


@pytest.mark.asyncio
async def test_listing_failure_is_not_presented_as_an_empty_gallery(bucket):
    bucket.seed(3)
    bucket.faults["list"] = True

    state = await appearance_service.gallery()
    assert state["known"] is False
    assert state["items"] == []
    assert "could not be read" in state["error"]

    _t, body, _b = appearance._preview_view(state)
    assert "could not be read" in body

    ok, notice = await appearance_service.install_image(png())
    assert ok is False and "could not be read" in notice
    assert not any(call[0] == "upload" for call in bucket.calls)


@pytest.mark.asyncio
async def test_out_of_band_objects_are_not_part_of_the_gallery(bucket):
    bucket.seed(1)
    bucket.put(f"{PREFIX}/not-a-generated-key.png", png())
    bucket.put(f"{PREFIX}/nested/{uuid.uuid4().hex}.png", png())

    state = await appearance_service.gallery()
    assert state["count"] == 1


# ── selection ───────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_selecting_an_inactive_image_changes_only_the_key(bucket):
    keys = bucket.seed(3)
    settings_service.set_appearance_preview_image_key(keys[0])
    before_objects = dict(bucket.objects)
    before_calls = len(bucket.calls)

    ok, notice = await appearance_service.select_image(
        appearance_service.token_from_key(keys[2])
    )
    assert ok is True and "selected" in notice.lower()
    assert settings_service.appearance_preview_image_key() == keys[2]
    assert bucket.objects == before_objects
    assert {call[0] for call in bucket.calls[before_calls:]} == {"list"}


@pytest.mark.asyncio
async def test_unknown_or_malformed_identifiers_fail_closed(bucket):
    keys = bucket.seed(2)
    settings_service.set_appearance_preview_image_key(keys[0])

    for token in ("", "   ", "../../etc/passwd", "A" * 32, "0" * 32, "z" * 40):
        ok, notice = await appearance_service.select_image(token)
        assert ok is False, token
        assert "not valid" in notice or "no longer in the gallery" in notice
        assert settings_service.appearance_preview_image_key() == keys[0]
        ok, _ = await appearance_service.delete_image(token)
        assert ok is False
    assert set(bucket.objects) == set(keys)


@pytest.mark.asyncio
async def test_deleting_an_inactive_image_preserves_the_selection(bucket):
    keys = bucket.seed(3)
    settings_service.set_appearance_preview_image_key(keys[1])

    ok, notice = await appearance_service.delete_image(
        appearance_service.token_from_key(keys[0])
    )
    assert ok is True and "not changed" in notice
    assert settings_service.appearance_preview_image_key() == keys[1]
    assert keys[0] not in bucket.objects
    assert {keys[1], keys[2]} <= set(bucket.objects)


@pytest.mark.asyncio
async def test_deleting_the_active_image_clears_its_reference_safely(bucket):
    keys = bucket.seed(2)
    settings_service.set_appearance_preview_image_key(keys[0])

    ok, notice = await appearance_service.delete_image(
        appearance_service.token_from_key(keys[0])
    )
    assert ok is True and "text-only" in notice
    assert settings_service.appearance_preview_image_key() == ""
    assert keys[0] not in bucket.objects and keys[1] in bucket.objects


@pytest.mark.asyncio
async def test_deletion_removes_exactly_one_object(bucket):
    keys = bucket.seed(4)
    ok, _notice = await appearance_service.delete_image(
        appearance_service.token_from_key(keys[2])
    )
    assert ok is True
    assert set(bucket.objects) == {keys[0], keys[1], keys[3]}
    assert [call for call in bucket.calls if call[0] == "remove"] == [
        ("remove", (keys[2],))
    ]


@pytest.mark.asyncio
async def test_deletion_failure_is_reported_and_keeps_the_selection(bucket):
    keys = bucket.seed(2)
    settings_service.set_appearance_preview_image_key(keys[0])
    bucket.faults["remove"] = True

    ok, notice = await appearance_service.delete_image(
        appearance_service.token_from_key(keys[1])
    )
    assert ok is False and "refused" in notice
    assert settings_service.appearance_preview_image_key() == keys[0]
    assert keys[1] in bucket.objects


@pytest.mark.asyncio
async def test_failed_deletion_of_the_active_image_reports_the_inconsistency(bucket):
    keys = bucket.seed(1)
    settings_service.set_appearance_preview_image_key(keys[0])
    bucket.faults["remove"] = True

    ok, notice = await appearance_service.delete_image(
        appearance_service.token_from_key(keys[0])
    )
    assert ok is False and "still stored" in notice
    assert settings_service.appearance_preview_image_key() == ""
    assert keys[0] in bucket.objects


@pytest.mark.asyncio
async def test_disabling_preview_keeps_every_image_and_the_selection(bucket, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://lifeos.example")
    keys = bucket.seed(3)
    settings_service.set_appearance_preview_image_key(keys[1])

    assert settings_service.set_appearance_preview_enabled(False) is True
    state = await appearance_service.gallery()
    assert state["count"] == 3 and state["missing_selected"] is False
    assert settings_service.appearance_preview_image_key() == keys[1]
    assert appearance_service.preview_active() is False


@pytest.mark.asyncio
async def test_reenabling_preview_reuses_the_selected_image(bucket, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://lifeos.example")
    keys = bucket.seed(2)
    settings_service.set_appearance_preview_image_key(keys[0])
    settings_service.set_appearance_preview_enabled(False)
    settings_service.set_appearance_preview_enabled(True)

    assert appearance_service.preview_active() is True
    assert appearance_service.preview_version() == appearance_service.token_from_key(keys[0])
    payload = await appearance_service.image_payload(
        appearance_service.preview_version()
    )
    assert payload is not None and payload[0] == bucket.objects[keys[0]]


@pytest.mark.asyncio
async def test_a_selected_key_that_no_longer_exists_is_reported(bucket):
    keys = bucket.seed(2)
    settings_service.set_appearance_preview_image_key(f"{PREFIX}/{uuid.uuid4().hex}.png")

    state = await appearance_service.gallery()
    assert state["missing_selected"] is True
    _t, body, _b = appearance._preview_view(state)
    assert "missing from Storage" in body
    assert state["count"] == 2

    # The owner can still select a retained image and recover.
    ok, _notice = await appearance_service.select_image(
        appearance_service.token_from_key(keys[0])
    )
    assert ok is True
    assert (await appearance_service.gallery())["missing_selected"] is False


# ── upload consistency ──────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_a_missing_selection_can_be_cleared_without_deleting_images(bucket):
    keys = bucket.seed(2)
    settings_service.set_appearance_preview_image_key(f"{PREFIX}/{uuid.uuid4().hex}.png")

    state = await appearance_service.gallery()
    _t, _b, buttons = appearance._preview_view(state)
    assert "action:appearance_clear_selection" in _data(buttons)

    ok, notice = await appearance_service.clear_selection()
    assert ok is True and "cleared" in notice
    assert settings_service.appearance_preview_image_key() == ""
    assert set(bucket.objects) == set(keys)
    assert (await appearance_service.gallery())["missing_selected"] is False


@pytest.mark.asyncio
async def test_upload_preserves_original_bytes_and_selects_the_new_image(bucket):
    original = encoded("JPEG")
    ok, notice = await appearance_service.install_image(original, "image/jpeg")
    assert ok is True and "uploaded" in notice

    key = settings_service.appearance_preview_image_key()
    assert key.endswith(".jpg") and key.startswith(f"{PREFIX}/")
    assert bucket.objects[key] == original
    options = next(call[2] for call in bucket.calls if call[0] == "upload")
    assert options == {"content-type": "image/jpeg", "upsert": "false"}


@pytest.mark.asyncio
async def test_upload_does_not_touch_existing_images(bucket):
    keys = bucket.seed(3)
    previous = dict(bucket.objects)
    ok, _notice = await appearance_service.install_image(png())
    assert ok is True

    assert all(previous[key] == bucket.objects[key] for key in keys)
    assert len(bucket.objects) == 4
    assert not any(call[0] == "remove" for call in bucket.calls)


@pytest.mark.asyncio
async def test_rejected_image_never_uploads_or_changes_the_selection(bucket):
    keys = bucket.seed(1)
    settings_service.set_appearance_preview_image_key(keys[0])
    before = dict(bucket.objects)

    for payload in (b"", b"not an image", png()[:40], encoded("GIF")):
        ok, notice = await appearance_service.install_image(payload)
        assert ok is False
        assert settings_service.appearance_preview_image_key() == keys[0]
        assert bucket.objects == before
    assert not any(call[0] == "upload" for call in bucket.calls)


@pytest.mark.asyncio
async def test_failed_persistence_removes_the_new_object(bucket, monkeypatch):
    keys = bucket.seed(1)
    settings_service.set_appearance_preview_image_key(keys[0])
    monkeypatch.setattr(panel_settings_repository, "update_field", lambda *a: False)

    ok, notice = await appearance_service.install_image(png())
    assert ok is False and "discarded" in notice
    assert set(bucket.objects) == set(keys)
    assert settings_service.appearance_preview_image_key() == keys[0]


@pytest.mark.asyncio
async def test_failed_upload_changes_nothing(bucket):
    keys = bucket.seed(1)
    settings_service.set_appearance_preview_image_key(keys[0])
    bucket.faults["upload"] = True

    ok, notice = await appearance_service.install_image(png())
    assert ok is False and "upload failed" in notice
    assert settings_service.appearance_preview_image_key() == keys[0]
    assert set(bucket.objects) == set(keys)


@pytest.mark.asyncio
async def test_concurrent_uploads_cannot_exceed_the_limit(bucket):
    bucket.seed(9)
    results = await asyncio.gather(
        appearance_service.install_image(png()),
        appearance_service.install_image(png()),
    )
    assert sum(1 for ok, _ in results if ok) == 1
    assert len(bucket.objects) == 10


@pytest.mark.asyncio
async def test_object_keys_are_unique_and_confined_to_the_prefix(bucket):
    for _ in range(3):
        assert (await appearance_service.install_image(png()))[0] is True
    keys = [key for key in bucket.objects]
    assert len(keys) == len(set(keys)) == 3
    assert all(key.startswith(f"{PREFIX}/") for key in keys)
    assert all(appearance_service.token_from_key(key) for key in keys)


# ── validation and quality ──────────────────────────────────────────────────

def test_each_format_reports_its_real_content_type():
    assert appearance_service.validate_image(png())[0] == "image/png"
    assert appearance_service.validate_image(encoded("JPEG"))[0] == "image/jpeg"
    assert appearance_service.validate_image(encoded("WEBP"))[0] == "image/webp"


def test_declared_mime_must_match_the_real_content():
    with pytest.raises(appearance_service.ImageRejected) as excinfo:
        appearance_service.validate_image(encoded("JPEG"), "image/png")
    assert "content is image/jpeg" in str(excinfo.value)
    # An unknown/absent declaration is decided by the content alone, and the
    # legacy "image/jpg" alias still has to match the real JPEG content.
    assert appearance_service.validate_image(encoded("JPEG"), "application/octet-stream")[0] == "image/jpeg"
    assert appearance_service.validate_image(encoded("JPEG"), "image/jpg")[0] == "image/jpeg"
    with pytest.raises(appearance_service.ImageRejected):
        appearance_service.validate_image(png(), "image/jpg")


def test_malformed_and_truncated_files_are_rejected():
    valid_png = png(16, 16)
    cases = {
        "truncated png": valid_png[: len(valid_png) // 2],
        "png without pixels": png_header(8, 8),
        "png with a short pixel stream": png_header(64, 64)[:-12]
        + _chunk(b"IDAT", zlib.compress(b"\x00" * 8))
        + _chunk(b"IEND", b""),
        "truncated jpeg": encoded("JPEG")[:20],
        "truncated webp": encoded("WEBP")[:20],
        "text": b"hello world, definitely not an image",
    }
    for name, payload in cases.items():
        with pytest.raises(appearance_service.ImageRejected):
            appearance_service.validate_image(payload)


def test_exactly_25_mib_is_accepted_and_one_byte_more_is_rejected():
    base = png(2, 2)
    exact = base + b"\x00" * (appearance_service.MAX_IMAGE_BYTES - len(base))
    assert len(exact) == appearance_service.MAX_IMAGE_BYTES
    assert appearance_service.validate_image(exact)[0] == "image/png"

    with pytest.raises(appearance_service.ImageRejected) as excinfo:
        appearance_service.validate_image(exact + b"\x00")
    assert "larger than 25 MB" in str(excinfo.value)


def test_side_boundary_is_8192_pixels():
    # A claimed 8192-wide image gets past the side bound (and then fails only
    # because it has no pixel data), while 8193 is refused for its width.
    with pytest.raises(appearance_service.ImageRejected) as accepted_side:
        appearance_service.validate_image(png_header(8192, 1))
    assert "8192px per side" not in str(accepted_side.value)

    with pytest.raises(appearance_service.ImageRejected) as excinfo:
        appearance_service.validate_image(png_header(8193, 1))
    assert "8192px per side" in str(excinfo.value)


def test_fifty_megapixel_boundary():
    allowed = png_header(8192, 6103)  # 49,995,776 pixels
    assert 8192 * 6103 <= appearance_service.MAX_IMAGE_PIXELS
    with pytest.raises(appearance_service.ImageRejected) as excinfo:
        appearance_service.validate_image(allowed)
    assert "megapixels" not in str(excinfo.value)

    over = png_header(8192, 6104)  # 50,003,968 pixels
    with pytest.raises(appearance_service.ImageRejected) as excinfo:
        appearance_service.validate_image(over)
    assert "megapixels" in str(excinfo.value)


def test_decompression_bomb_is_refused_before_decoding():
    import time

    started = time.monotonic()
    with pytest.raises(appearance_service.ImageRejected) as excinfo:
        appearance_service.validate_image(png_header(8000, 8000))  # 64 Mpx
    elapsed = time.monotonic() - started
    assert "megapixels" in str(excinfo.value)
    assert elapsed < 0.5


def test_validation_never_transforms_the_bytes():
    original = encoded("PNG")
    header = Image.open(io.BytesIO(original))
    size = header.size
    header.close()

    appearance_service.validate_image(original)
    after = Image.open(io.BytesIO(original))
    assert after.size == size and after.format == "PNG"
    after.close()
    assert original == encoded("PNG")


# ── bounded download ────────────────────────────────────────────────────────

class _Stream:
    def __init__(self, chunks):
        self.chunks = chunks
        self.consumed = 0
        self.closed = False

    async def __aiter__(self):
        for chunk in self.chunks:
            self.consumed += 1
            yield chunk

    async def aclose(self):
        self.closed = True


class _Client:
    def __init__(self, chunks):
        self.stream = _Stream(chunks)

    def iter_download(self, media, chunk_size=None):
        self.stream.requested_chunk = chunk_size
        return self.stream


class _Event:
    def __init__(self, client, media=True, size=None, mime="image/png"):
        self.media = object() if media else None
        self.file = type("F", (), {"size": size})()
        self.message = type("M", (), {
            "id": 1,
            "media": self.media,
            "photo": None,
            "document": type("D", (), {"mime_type": mime})() if media else None,
        })()
        self.client = client


@pytest.fixture
def painted(monkeypatch):
    seen: list = []

    async def _paint(inline_id, view):
        seen.append(view)
        return True

    monkeypatch.setattr(appearance, "_paint_input", _paint)
    return seen


@pytest.mark.asyncio
async def test_transfer_over_the_limit_is_stopped_mid_stream(bucket, painted):
    megabyte = b"\x00" * (1024 * 1024)
    client = _Client([megabyte] * 40)  # 40 MiB claimed, 25 MiB allowed

    await appearance._image_input("", CHAT, 1, CHAT, PANEL_MSG, event=_Event(client))

    assert client.stream.consumed < 40
    assert client.stream.closed is True
    assert painted and "larger than 25 MB" in painted[-1][1]
    assert bucket.objects == {}
    assert not any(call[0] == "upload" for call in bucket.calls)


@pytest.mark.asyncio
async def test_a_misleading_reported_size_cannot_bypass_the_limit(bucket, painted):
    megabyte = b"\x00" * (1024 * 1024)
    client = _Client([megabyte] * 30)

    await appearance._image_input(
        "", CHAT, 1, CHAT, PANEL_MSG, event=_Event(client, size=1)
    )

    assert "larger than 25 MB" in painted[-1][1]
    assert bucket.objects == {}


@pytest.mark.asyncio
async def test_a_missing_reported_size_is_irrelevant_to_the_cap(bucket, painted):
    client = _Client([encoded("PNG")])
    await appearance._image_input(
        "", CHAT, 1, CHAT, PANEL_MSG, event=_Event(client, size=None)
    )
    assert len(bucket.objects) == 1
    assert painted and "uploaded" in painted[-1][1]


@pytest.mark.asyncio
async def test_upload_uses_no_temporary_file(bucket, painted, monkeypatch):
    import tempfile

    monkeypatch.setattr(
        tempfile,
        "mkdtemp",
        lambda **kwargs: (_ for _ in ()).throw(AssertionError("no temp directory")),
    )
    client = _Client([png()])
    await appearance._image_input("", CHAT, 1, CHAT, PANEL_MSG, event=_Event(client))
    assert len(bucket.objects) == 1


@pytest.mark.asyncio
async def test_stream_is_closed_on_rejection(bucket, painted):
    client = _Client([b"x" * (appearance_service.MAX_IMAGE_BYTES + 1)])
    await appearance._image_input("", CHAT, 1, CHAT, PANEL_MSG, event=_Event(client))
    assert client.stream.closed is True
    assert "larger than 25 MB" in painted[-1][1]


@pytest.mark.asyncio
async def test_non_media_input_is_refused_without_touching_storage(bucket, painted):
    await appearance._image_input(
        "", CHAT, 1, CHAT, PANEL_MSG, event=_Event(_Client([]), media=False)
    )
    assert "Send a PNG, JPEG or WebP" in painted[-1][1]
    assert bucket.objects == {}


# ── preview routes ──────────────────────────────────────────────────────────

def _client():
    from fastapi.testclient import TestClient

    from backend.web.app import app

    return TestClient(app)


@pytest.mark.asyncio
async def test_selected_version_serves_the_original_bytes_and_type(bucket, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://lifeos.example")
    original = encoded("WEBP")
    ok, _notice = await appearance_service.install_image(original, "image/webp")
    assert ok is True

    version = appearance_service.preview_version()
    response = _client().get(f"/appearance/image/{version}")
    assert response.status_code == 200
    assert response.headers["content-type"].startswith("image/webp")
    assert response.content == original
    assert "immutable" in response.headers["cache-control"]

    page = _client().get(f"/appearance/preview/{version}")
    assert page.status_code == 200
    assert "/guide/" in page.text
    assert f"/appearance/image/{version}" in page.text


@pytest.mark.asyncio
async def test_old_version_stops_being_the_active_preview(bucket, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://lifeos.example")
    keys = bucket.seed(2)
    settings_service.set_appearance_preview_image_key(keys[0])
    settings_service.set_appearance_preview_enabled(True)
    old = appearance_service.preview_version()

    ok, _notice = await appearance_service.select_image(
        appearance_service.token_from_key(keys[1])
    )
    assert ok is True
    new = appearance_service.preview_version()
    assert new != old

    assert _client().get(f"/appearance/image/{old}").status_code == 404
    assert _client().get(f"/appearance/preview/{old}").status_code == 404
    assert _client().get(f"/appearance/image/{new}").status_code == 200


@pytest.mark.asyncio
async def test_disabled_preview_serves_nothing_and_stays_text_only(bucket, monkeypatch):
    monkeypatch.setenv("PUBLIC_BASE_URL", "https://lifeos.example")
    keys = bucket.seed(1)
    settings_service.set_appearance_preview_image_key(keys[0])
    settings_service.set_appearance_preview_enabled(False)

    assert appearance_service.preview_active() is False
    title, body = appearance_service.with_preview("menu", "", "LifeOS", "Choose:")
    assert (title, body) == ("LifeOS", "Choose:")
    assert "/appearance/preview/" not in body
    # The gallery itself survives: re-enabling reuses the same object.
    assert (await appearance_service.gallery())["count"] == 1


# ── Glass UI ────────────────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_gallery_panel_shows_count_selection_and_per_image_actions(bucket):
    keys = bucket.seed(3)
    settings_service.set_appearance_preview_image_key(keys[1])

    _title, body, buttons = appearance._preview_view(
        await appearance_service.gallery()
    )
    assert "**Images:** 3 / 10" in body
    assert "**Selected:** image" in body

    data = _data(buttons)
    tokens = [appearance_service.token_from_key(key) for key in keys]
    for token in tokens:
        assert f"action:appearance_select:{token}" in data
        assert f"action:appearance_delete:{token}" in data
    assert "action:appearance_toggle_preview" in data
    assert f"input:{appearance._PANEL}:image" in data


@pytest.mark.asyncio
async def test_gallery_panel_hides_upload_when_full(bucket):
    bucket.seed(10)
    _title, body, buttons = appearance._preview_view(
        await appearance_service.gallery()
    )
    assert f"input:{appearance._PANEL}:image" not in _data(buttons)
    assert "gallery is full (10)" in body


@pytest.mark.asyncio
async def test_gallery_panel_marks_only_the_selected_image(bucket):
    keys = bucket.seed(2)
    settings_service.set_appearance_preview_image_key(keys[0])
    state = await appearance_service.gallery()
    _t, _b, buttons = appearance._preview_view(state)

    def _raw(button) -> str:
        value = getattr(button, "data", "")
        return value.decode() if isinstance(value, bytes) else str(value)

    marked = [
        getattr(button, "text", "")
        for row in buttons
        for button in row
        if _raw(button).startswith("action:appearance_select:")
        and getattr(button, "text", "").startswith(sym.STATE_COMPLETED)
    ]
    assert len(marked) == 1
    active_index = next(
        index for index, item in enumerate(state["items"], 1) if item["active"]
    )
    assert marked == [f"{sym.STATE_COMPLETED} {active_index}"]


@pytest.mark.asyncio
async def test_gallery_labels_use_registry_symbols_only(bucket):
    bucket.seed(2)
    _t, _b, buttons = appearance._preview_view(await appearance_service.gallery())
    glyphs = {spec.glyph for spec in sym.ALL_SYMBOLS.values()}
    for label in _labels(buttons):
        assert label.split(" ")[0] in glyphs, label
        assert "\ufe0f" not in label
        assert not any(ord(char) >= 0x1F000 for char in label), label


@pytest.mark.asyncio
async def test_select_and_delete_actions_render_the_result(bucket):
    keys = bucket.seed(2)
    token = appearance_service.token_from_key(keys[0])

    _t, body, _b = await appearance._select_image(None, token, CHAT)
    assert settings_service.appearance_preview_image_key() == keys[0]
    assert "selected" in body.lower()

    _t, body, _b = await appearance._delete_image(None, token, CHAT)
    assert settings_service.appearance_preview_image_key() == ""
    assert "deleted" in body.lower()


@pytest.mark.asyncio
async def test_gallery_callbacks_stay_within_telegram_limits(bucket):
    bucket.seed(10)
    _t, _b, buttons = appearance._preview_view(await appearance_service.gallery())
    for value in _data(buttons):
        assert len(value.encode()) <= 64, value
