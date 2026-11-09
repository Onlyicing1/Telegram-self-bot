"""Appearance personalization for the Glass UI.

Three independent areas, all stored on the EXISTING singleton settings row
(`settings_service` → `panel_settings_repository` → `panel_settings`):

* **global header** — a shared static title/description prepended to the
  customizable main pages;
* **per-main-page text** — title/body overrides for the pages reachable as
  top-level categories from the main menu;
* **preview image** — an independent enable flag plus a durable Storage object
  key (never image bytes, never a signed/expiring URL).

Composition happens at the panel render boundary: ``panels._handle_panel``
for callback navigation and ``inline_engine._inline_router`` for the initial
inline result. Dynamic operational content (action results, confirmations,
paginated pickers) is never rewritten — only the home view of an eligible
main page is customizable.

The preview image lives in a Supabase Storage bucket
(``APPEARANCE_MEDIA_BUCKET``, falling back to the existing
``ARCHIVE_MEDIA_BUCKET``); Render's local filesystem is never used. The
rendered preview is Telegram's web-page preview of a public HTTPS page
(``/appearance/preview/<version>``) served by ``backend/web/app.py``, whose
Open Graph tags point at ``/appearance/image/<version>`` and whose tap target
is the existing Guide site.
"""
from __future__ import annotations

import asyncio
import html
import logging
import os
import re
import uuid
from typing import Any

from backend.helper import symbols as sym
from backend.services import settings_service

logger = logging.getLogger(__name__)

#: The main pages reachable as top-level categories from the main menu
#: (``misc._build_menu_buttons``). Sub-panels, inputs, pagination, pickers,
#: confirmation and result screens are deliberately absent: only these pages
#: are customizable. ``tests/test_appearance_ui.py`` asserts this tuple matches
#: the menu's own ``panel:*`` destinations, so the two can never drift.
MAIN_PAGE_IDS: tuple[str, ...] = (
    "menu",
    "save",
    "del",
    "profile",
    "db",
    "general",
    "settings",
    "cipher",
    "todo",
    "ai",
    "appearance",
)

#: Canonical defaults, used for the Appearance UI and for deciding whether a
#: handler body is still its untouched default. An empty default body means the
#: page renders live content (counters, lists, provider/model state): a body
#: override is then shown ABOVE that live content instead of replacing it.
PAGE_DEFAULTS: dict[str, tuple[str, str]] = {
    "menu": ("LifeOS", "Choose a category:"),
    "save": (
        "Save",
        "Deep Save downloads the message and re-uploads it as a new Saved "
        "Messages message.",
    ),
    "del": ("Delete", "Choose a deletion scope:"),
    "profile": ("Profile", "Choose a section:"),
    "db": ("Database", "Choose an action:"),
    "general": ("General", ""),
    "settings": ("Settings", ""),
    "cipher": ("Cipher", "Private Communications\n\nChoose a section:"),
    "todo": ("Todo", ""),
    "ai": ("AI", ""),
    "appearance": ("Appearance", ""),
}

TITLE_LIMIT = 120
BODY_LIMIT = 800

MAX_IMAGE_MB = 5
MAX_IMAGE_BYTES = MAX_IMAGE_MB * 1024 * 1024
MAX_IMAGE_SIDE = 4096

_VERSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")
_MIME_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}


class ImageRejected(Exception):
    """Uploaded media is not a supported, bounded image."""


# ── Page identity ────────────────────────────────────────────────────────────

def is_eligible(page_id: str) -> bool:
    return page_id in PAGE_DEFAULTS


def default_title(page_id: str) -> str:
    return PAGE_DEFAULTS.get(page_id, ("", ""))[0]


def default_body(page_id: str) -> str:
    return PAGE_DEFAULTS.get(page_id, ("", ""))[1]


def page_override(page_id: str) -> dict[str, str]:
    override = settings_service.appearance_page_overrides().get(page_id)
    return dict(override) if isinstance(override, dict) else {}


def page_state(page_id: str) -> dict[str, Any]:
    """Effective title/body plus what is customized, for the Appearance UI."""
    override = page_override(page_id)
    return {
        "page_id": page_id,
        "title": override.get("title") or default_title(page_id),
        "body": override.get("body") or default_body(page_id),
        "default_title": default_title(page_id),
        "default_body": default_body(page_id),
        "custom_title": bool(override.get("title")),
        "custom_body": bool(override.get("body")),
    }


def customized_page_count() -> int:
    overrides = settings_service.appearance_page_overrides()
    return sum(1 for page_id in MAIN_PAGE_IDS if overrides.get(page_id))


# ── Writes (validated; never store an invalid value) ─────────────────────────

def set_header(field: str, value: str) -> tuple[bool, str]:
    """Persist one global header field. ``field`` is ``title`` or ``body``."""
    text = _clean_text(value, single_line=(field == "title"))
    limit = TITLE_LIMIT if field == "title" else BODY_LIMIT
    if not text:
        return False, f"{sym.STATE_FAILED} That value is empty, so nothing was changed."
    if len(text) > limit:
        return False, (
            f"{sym.STATE_FAILED} Too long: {len(text)} characters (limit {limit}). "
            "Nothing was changed."
        )
    if field == "title":
        ok = settings_service.set_appearance_header_title(text)
    elif field == "body":
        ok = settings_service.set_appearance_header_body(text)
    else:
        return False, f"{sym.STATE_FAILED} Unknown header field."
    if not ok:
        return False, f"{sym.STATE_FAILED} Storage refused the change; nothing was saved."
    return True, f"{sym.OP_SAVE} Header {field} saved."


def reset_header(field: str) -> tuple[bool, str]:
    """Clear one header field (``title``/``body``) or ``both``."""
    if field == "title":
        ok = settings_service.set_appearance_header_title("")
        label = "title"
    elif field == "body":
        ok = settings_service.set_appearance_header_body("")
        label = "description"
    elif field == "both":
        ok = (
            settings_service.set_appearance_header_title("")
            and settings_service.set_appearance_header_body("")
        )
        label = "title and description"
    else:
        return False, f"{sym.STATE_FAILED} Unknown header field."
    if not ok:
        return False, f"{sym.STATE_FAILED} Storage refused the reset; nothing was changed."
    return True, f"{sym.OP_REFRESH} Header {label} reset to the default."


def set_page_text(page_id: str, field: str, value: str) -> tuple[bool, str]:
    """Persist one field of one eligible main page's override."""
    if not is_eligible(page_id):
        return False, f"{sym.STATE_FAILED} {page_id!r} is not a customizable main page."
    if field not in ("title", "body"):
        return False, f"{sym.STATE_FAILED} Unknown page field."
    text = _clean_text(value, single_line=(field == "title"))
    limit = TITLE_LIMIT if field == "title" else BODY_LIMIT
    if not text:
        return False, f"{sym.STATE_FAILED} That value is empty, so nothing was changed."
    if len(text) > limit:
        return False, (
            f"{sym.STATE_FAILED} Too long: {len(text)} characters (limit {limit}). "
            "Nothing was changed."
        )
    overrides = settings_service.appearance_page_overrides()
    overrides.setdefault(page_id, {})[field] = text
    if not settings_service.set_appearance_page_overrides(overrides):
        return False, f"{sym.STATE_FAILED} Storage refused the change; nothing was saved."
    return True, f"{sym.OP_SAVE} {default_title(page_id)} {field} saved."


def reset_page_text(page_id: str, field: str) -> tuple[bool, str]:
    """Clear one field (``title``/``body``) or ``all`` of one page's override."""
    if not is_eligible(page_id):
        return False, f"{sym.STATE_FAILED} {page_id!r} is not a customizable main page."
    overrides = settings_service.appearance_page_overrides()
    fields = overrides.get(page_id) or {}
    if field == "all":
        overrides.pop(page_id, None)
        label = "customization"
    elif field in ("title", "body"):
        fields.pop(field, None)
        if fields:
            overrides[page_id] = fields
        else:
            overrides.pop(page_id, None)
        label = field
    else:
        return False, f"{sym.STATE_FAILED} Unknown page field."
    if not settings_service.set_appearance_page_overrides(overrides):
        return False, f"{sym.STATE_FAILED} Storage refused the reset; nothing was changed."
    return True, f"{sym.OP_REFRESH} {default_title(page_id)} {label} reset to the default."


def _clean_text(value: str, *, single_line: bool) -> str:
    text = (value or "").replace("\x00", "")
    if single_line:
        return " ".join(text.split())
    return text.replace("\r\n", "\n").strip()


# ── Composition ──────────────────────────────────────────────────────────────

def compose_text(panel_id: str, extra: str, title: str, body: str) -> tuple[str, str]:
    """Apply the global header and the page override to one rendered view.

    Only the home view (``extra == ""``) of an eligible main page is touched,
    so a confirmation, picker, paginated list or dynamic result rendered under
    the same panel keeps its own text.
    """
    if not is_eligible(panel_id) or extra:
        return title, body

    override = page_override(panel_id)
    page_title = override.get("title") or title
    page_body = body
    custom_body = override.get("body")
    if custom_body:
        canonical = default_body(panel_id)
        if not page_body or (canonical and page_body.strip() == canonical.strip()):
            page_body = custom_body
        else:
            page_body = f"{custom_body}\n\n{page_body}"

    header_title = settings_service.appearance_header_title().strip()
    header_body = settings_service.appearance_header_body().strip()

    if header_title:
        parts: list[str] = []
        if page_title and page_title.strip() != header_title:
            parts.append(f"**{page_title}**")
        if header_body:
            parts.append(header_body)
        if page_body:
            parts.append(page_body)
        return header_title, "\n\n".join(parts)

    if header_body:
        page_body = f"{header_body}\n\n{page_body}" if page_body else header_body
    return page_title, page_body


def with_preview(panel_id: str, extra: str, title: str, body: str) -> tuple[str, str]:
    """Compose the view and, when active, the versioned preview URL.

    The URL is what makes Telegram render its web-page preview above the text
    and buttons. It is appended (not substituted) so the page's own text is
    kept, and it carries the object's unique version so a replaced image is
    refetched instead of served from Telegram's preview cache.
    """
    title, body = compose_text(panel_id, extra, title, body)
    # Disabled preview must produce a GENUINELY text-only panel: no URL at all,
    # even when an image is still stored (turning preview OFF keeps the image).
    url = preview_url() if preview_active() else ""
    if url:
        body = f"{body}\n\n{url}" if body else url
    return title, body


def apply_to_inline_results(panel_id: str, extra: str, results: list) -> list:
    """Apply the appearance layer to freshly built inline results.

    The initial panel is an inline result, so it never passes through
    ``panels._handle_panel``. The title is recovered from the panel registry (or
    the canonical default) and the body is the remaining text, so the same
    composition is used for the first render and every later callback edit.
    """
    if not is_eligible(panel_id) or extra or not results:
        return results
    for result in results:
        message = getattr(result, "send_message", None)
        text = getattr(message, "message", None)
        if not isinstance(text, str):
            continue
        split = _split_rendered_text(text, panel_id)
        if split is None:
            continue
        title, body = with_preview(panel_id, extra, *split)
        message.message = _compose_rendered(title, body)
        if hasattr(message, "no_webpage"):
            message.no_webpage = not preview_active()
    return results


def _split_rendered_text(text: str, panel_id: str) -> tuple[str, str] | None:
    from backend.helper.panel_registry import get_panel_title
    from backend.helper.panel_render import _style

    for candidate in (get_panel_title(panel_id), default_title(panel_id)):
        if not candidate:
            continue
        prefix = f"**{_style(candidate)}**"
        if text.startswith(prefix):
            return candidate, text[len(prefix):].lstrip("\n")
    return None


def _compose_rendered(title: str, body: str) -> str:
    from backend.helper.panel_render import _style

    if title and body:
        return f"**{_style(title)}**\n\n{_style(body)}"
    if title:
        return f"**{_style(title)}**"
    return _style(body) or ""


# ── Preview identity ─────────────────────────────────────────────────────────

def public_base_url() -> str:
    """The public HTTPS origin used in preview URLs ("" when unconfigured)."""
    for name in ("PUBLIC_BASE_URL", "RENDER_EXTERNAL_URL"):
        value = (os.getenv(name) or "").strip().rstrip("/")
        if value:
            if value.startswith("https://"):
                return value
            logger.warning("appearance: %s is not an https:// URL — preview disabled", name)
            return ""
    return ""


def preview_version() -> str:
    key = settings_service.appearance_preview_image_key()
    if not key:
        return ""
    stem = key.rsplit("/", 1)[-1].split(".")[0]
    return stem if _VERSION_RE.match(stem) else ""


def preview_url() -> str:
    base = public_base_url()
    version = preview_version()
    if not base or not version:
        return ""
    return f"{base}/appearance/preview/{version}"


def preview_image_url() -> str:
    base = public_base_url()
    version = preview_version()
    if not base or not version:
        return ""
    return f"{base}/appearance/image/{version}"


def preview_active() -> bool:
    """True only when a genuine preview can be rendered right now."""
    return (
        settings_service.appearance_preview_enabled()
        and bool(settings_service.appearance_preview_image_key())
        and bool(public_base_url())
    )


def preview_status() -> dict[str, Any]:
    enabled = settings_service.appearance_preview_enabled()
    stored = bool(settings_service.appearance_preview_image_key())
    return {
        "enabled": enabled,
        "stored": stored,
        "active": preview_active(),
        "public_base": bool(public_base_url()),
        "storage": storage_available(),
        "mode": "image" if preview_active() else "text",
    }


def preview_meta(version: str) -> dict[str, str] | None:
    """Open Graph metadata for one stored preview version, or ``None``."""
    current = preview_version()
    base = public_base_url()
    if not version or not current or version != current or not base:
        return None
    title = settings_service.appearance_header_title().strip() or "LifeOS"
    description = (
        settings_service.appearance_header_body().strip()
        or "LifeOS — Glass UI panel for your own Telegram account."
    )
    return {
        "version": version,
        "title": title,
        "description": description,
        "image_url": f"{base}/appearance/image/{version}",
        "url": f"{base}/guide/",
    }


def preview_html(meta: dict[str, str]) -> str:
    """The public page Telegram reads for its preview (and opens on tap).

    It carries the Open Graph tags and immediately continues to the existing
    Guide site; it is not a second application UI.
    """
    title = html.escape(meta["title"])
    description = html.escape(meta["description"])
    image = html.escape(meta["image_url"])
    url = html.escape(meta["url"])
    return (
        "<!doctype html>\n"
        '<html lang="en" dir="ltr">\n<head>\n<meta charset="utf-8" />\n'
        '<meta name="viewport" content="width=device-width, initial-scale=1.0" />\n'
        f"<title>{title}</title>\n"
        f'<meta name="description" content="{description}" />\n'
        '<meta property="og:type" content="website" />\n'
        f'<meta property="og:title" content="{title}" />\n'
        f'<meta property="og:description" content="{description}" />\n'
        f'<meta property="og:image" content="{image}" />\n'
        '<meta property="og:image:type" content="image/*" />\n'
        f'<meta property="og:url" content="{url}" />\n'
        '<meta name="twitter:card" content="summary_large_image" />\n'
        f'<meta name="twitter:image" content="{image}" />\n'
        f'<link rel="canonical" href="{url}" />\n'
        f'<meta http-equiv="refresh" content="0; url={url}" />\n'
        "</head>\n<body>\n"
        f'<p><a href="{url}">{title}</a></p>\n'
        f'<img src="{image}" alt="" />\n'
        f"<script>location.replace({meta['url']!r});</script>\n"
        "</body>\n</html>\n"
    )


# ── Durable image storage (Supabase Storage; never the local filesystem) ─────

def bucket() -> str:
    """The Storage bucket: a dedicated one, or the existing archive bucket."""
    return (
        (os.getenv("APPEARANCE_MEDIA_BUCKET") or "").strip()
        or (os.getenv("ARCHIVE_MEDIA_BUCKET") or "").strip()
    )


def storage_available() -> bool:
    from backend.db import client as db_client

    return bool(bucket()) and db_client.get_db() is not None


def sniff_image(data: bytes) -> tuple[str, int, int] | None:
    """Return ``(mime, width, height)`` from the bytes themselves.

    The declared MIME type is never trusted: only real PNG/JPEG/WebP headers
    with a readable size are accepted.
    """
    if not data:
        return None
    if data.startswith(b"\x89PNG\r\n\x1a\n"):
        if len(data) < 33 or data[12:16] != b"IHDR":
            return None
        width = int.from_bytes(data[16:20], "big")
        height = int.from_bytes(data[20:24], "big")
        return ("image/png", width, height) if width and height else None
    if data.startswith(b"\xff\xd8"):
        size = _jpeg_size(data)
        return ("image/jpeg", *size) if size else None
    if data[:4] == b"RIFF" and data[8:12] == b"WEBP":
        size = _webp_size(data)
        return ("image/webp", *size) if size else None
    return None


def validate_image(data: bytes) -> tuple[str, int, int]:
    """Validate size, real content and dimensions; raise :class:`ImageRejected`."""
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageRejected(f"the image is larger than {MAX_IMAGE_MB} MB")
    if not data:
        raise ImageRejected("the file is empty")
    sniffed = sniff_image(data)
    if sniffed is None:
        raise ImageRejected("that file is not a readable PNG, JPEG or WebP image")
    mime, width, height = sniffed
    if width < 1 or height < 1:
        raise ImageRejected("the image has no usable dimensions")
    if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
        raise ImageRejected(
            f"the image is {width}x{height}; the limit is {MAX_IMAGE_SIDE}px per side"
        )
    return mime, width, height


def _jpeg_size(data: bytes) -> tuple[int, int] | None:
    index = 2
    length = len(data)
    while index + 9 < length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            index += 2
            continue
        segment = int.from_bytes(data[index + 2:index + 4], "big")
        if segment < 2:
            return None
        # SOF0..SOF3, SOF5..SOF7, SOF9..SOF11, SOF13..SOF15 (not DHT/JPG/DAC).
        if 0xC0 <= marker <= 0xCF and marker not in (0xC4, 0xC8, 0xCC):
            height = int.from_bytes(data[index + 5:index + 7], "big")
            width = int.from_bytes(data[index + 7:index + 9], "big")
            return (width, height) if width and height else None
        index += 2 + segment
    return None


def _webp_size(data: bytes) -> tuple[int, int] | None:
    chunk = data[12:16]
    if chunk == b"VP8X" and len(data) >= 30:
        width = 1 + int.from_bytes(data[24:27], "little")
        height = 1 + int.from_bytes(data[27:30], "little")
        return (width, height)
    if chunk == b"VP8 ":
        start = data.find(b"\x9d\x01\x2a")
        if start < 0 or len(data) < start + 7:
            return None
        width = int.from_bytes(data[start + 3:start + 5], "little") & 0x3FFF
        height = int.from_bytes(data[start + 5:start + 7], "little") & 0x3FFF
        return (width, height) if width and height else None
    if chunk == b"VP8L" and len(data) >= 25:
        bits = int.from_bytes(data[21:25], "little")
        width = 1 + (bits & 0x3FFF)
        height = 1 + ((bits >> 14) & 0x3FFF)
        return (width, height)
    return None


def _upload_sync(key: str, data: bytes, mime: str) -> None:
    from backend.db import client as db_client

    db = db_client.get_db()
    if db is None:
        raise RuntimeError("database unavailable")
    db.storage.from_(bucket()).upload(
        key, data, {"content-type": mime, "upsert": "true"}
    )


def _delete_sync(key: str) -> bool:
    from backend.db import client as db_client

    db = db_client.get_db()
    if db is None:
        return False
    try:
        db.storage.from_(bucket()).remove([key])
        return True
    except Exception as exc:  # noqa: BLE001 — a stale object is logged, never fatal
        logger.warning("appearance: could not delete stored object: %s", exc)
        return False


def _download_sync(key: str) -> bytes | None:
    from backend.db import client as db_client

    db = db_client.get_db()
    if db is None:
        return None
    data = db.storage.from_(bucket()).download(key)
    if isinstance(data, (bytes, bytearray)) and data:
        return bytes(data)
    return None


# ── Owner-facing image operations ────────────────────────────────────────────

def storage_help() -> str:
    return (
        "Durable image storage is not configured. Set APPEARANCE_MEDIA_BUCKET "
        "(or ARCHIVE_MEDIA_BUCKET) to a Supabase Storage bucket and keep "
        "SUPABASE_URL / SUPABASE_SERVICE_ROLE_KEY set."
    )


async def install_image(data: bytes) -> tuple[bool, str]:
    """Validate, upload, persist the new key, then remove the replaced object."""
    if not storage_available():
        return False, f"{sym.STATE_FAILED} {storage_help()}"
    try:
        mime, width, height = validate_image(data)
    except ImageRejected as exc:
        return False, f"{sym.STATE_FAILED} {exc}."

    key = f"appearance/preview/{uuid.uuid4().hex}.{_MIME_EXT[mime]}"
    try:
        await asyncio.to_thread(_upload_sync, key, data, mime)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — upload failure is reported, not raised
        logger.warning("appearance: preview upload failed: %s", exc)
        return False, f"{sym.STATE_FAILED} The upload failed, so nothing was changed."

    previous = settings_service.appearance_preview_image_key()
    if not settings_service.set_appearance_preview_image_key(key):
        # The object must not outlive a reference we failed to persist.
        await asyncio.to_thread(_delete_sync, key)
        return False, (
            f"{sym.STATE_FAILED} The image could not be saved to settings, so the "
            "upload was discarded."
        )
    if previous and previous != key:
        await asyncio.to_thread(_delete_sync, previous)
    return True, f"{sym.OP_SAVE} Preview image stored ({width}x{height})."


async def clear_image() -> tuple[bool, str]:
    """Remove the stored object and clear the reference (settings first)."""
    key = settings_service.appearance_preview_image_key()
    if not key:
        return True, f"{sym.OP_DELETE} No stored preview image."
    if not settings_service.set_appearance_preview_image_key(""):
        return False, f"{sym.STATE_FAILED} The reference could not be cleared; nothing was changed."
    removed = False
    if storage_available():
        removed = await asyncio.to_thread(_delete_sync, key)
    if removed:
        return True, f"{sym.OP_DELETE} Preview image removed."
    return True, (
        f"{sym.OP_DELETE} Image reference removed. The stored object could not be "
        "deleted and no longer appears anywhere."
    )


async def image_payload(version: str) -> tuple[bytes, str] | None:
    """The stored image for one live version, with its content type."""
    if not version or version != preview_version() or not storage_available():
        return None
    try:
        data = await asyncio.to_thread(_download_sync, settings_service.appearance_preview_image_key())
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a missing object is a 404, not a crash
        logger.warning("appearance: preview download failed: %s", exc)
        return None
    if not data:
        return None
    sniffed = sniff_image(data)
    return data, (sniffed[0] if sniffed else "application/octet-stream")
