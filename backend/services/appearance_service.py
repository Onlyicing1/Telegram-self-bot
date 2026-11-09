"""Appearance personalization for the Glass UI.

Three independent areas, all stored on the EXISTING singleton settings row
(`settings_service` → `panel_settings_repository` → `panel_settings`):

* **global header** — a shared static title/description prepended to the
  customizable main pages;
* **per-main-page text** — title/body overrides for the pages reachable as
  top-level categories from the main menu;
* **preview image gallery** — an independent enable flag plus at most
  ``MAX_GALLERY_IMAGES`` original images in one private Supabase Storage
  bucket, and the durable key of the ONE selected image.

Composition happens at the panel render boundary: ``panels._handle_panel``
for callback navigation and ``inline_engine._inline_router`` for the initial
inline result. Dynamic operational content (action results, confirmations,
paginated pickers) is never rewritten — only the home view of an eligible
main page is customizable.

Gallery inventory is the Storage bucket itself, under ONE prefix
(``appearance/preview/``): the objects ARE the gallery, so no table, no extra
column and no second settings system exists. The settings row only ever holds
the selected object's key — never image bytes, a signed/expiring URL, a
Telegram file/message id or a filename. The rendered preview is Telegram's
web-page preview of a public HTTPS page (``/appearance/preview/<version>``)
served by ``backend/web/app.py``, whose Open Graph tags point at
``/appearance/image/<version>`` and whose tap target is the existing Guide
site.

Uploads preserve the owner's original bytes exactly: no resize, no
recompression, no format conversion, no metadata stripping. Validation reads
the file's real content (Pillow) inside a worker thread under hard size, side
and pixel bounds, so a mislabelled, truncated or decompression-bomb file is
refused before it can be decoded without limit.
"""
from __future__ import annotations

import asyncio
import html
import io
import logging
import os
import re
import time
import uuid
import zlib
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

# ── Image gallery bounds ─────────────────────────────────────────────────────
#
# These are the OWNER's quality contract as much as safety bounds: the original
# bytes are stored and served untouched up to 25 MiB (the bucket's own object
# ceiling), 8192 pixels per side and 50 decoded megapixels.
IMAGES_PREFIX = "appearance/preview"
MAX_GALLERY_IMAGES = 10
MAX_IMAGE_MB = 25
MAX_IMAGE_BYTES = 26_214_400
MAX_IMAGE_SIDE = 8192
MAX_IMAGE_PIXELS = 50_000_000

#: A full pixel decode proves an image is genuinely decodable, but its memory
#: cost grows with area. Above this budget the structural proof (container
#: trailer + Pillow's own header/CRC verification) is used instead, so a 50 MP
#: PNG is validated without materialising a 150 MB bitmap.
_DECODE_PIXEL_BUDGET = 12_000_000

#: One upload at a time inside this single-instance runtime: the count check and
#: the object write must not interleave, or two concurrent uploads could both
#: observe 9/10 and end at 11.
_UPLOAD_LOCK = asyncio.Lock()

_FORMAT_MIME = {"PNG": "image/png", "JPEG": "image/jpeg", "WEBP": "image/webp"}
_MIME_EXT = {"image/png": "png", "image/jpeg": "jpg", "image/webp": "webp"}
_EXT_MIME = {"png": "image/png", "jpg": "image/jpeg", "webp": "image/webp"}
_KEY_RE = re.compile(r"^appearance/preview/([0-9a-f]{32})\.(png|jpg|webp)$")
_TOKEN_RE = re.compile(r"^[0-9a-f]{32}$")
_VERSION_RE = re.compile(r"^[A-Za-z0-9_-]{8,80}$")

#: Telegram's media-caption ceiling. A composed panel above it is delivered as
#: TEXT instead, so the owner's full text is never truncated.
MAX_CAPTION_CHARS = 1024

#: The inline-photo fetch limit Telegram applies to a web document. The image
#: is re-encoded for delivery only; the stored object is never touched.
TELEGRAM_PHOTO_MAX_BYTES = 5 * 1024 * 1024

#: How long a positive/negative readiness answer is reused before the gallery is
#: listed again. Panel renders are frequent; the gallery changes rarely.
_DELIVERY_READY_TTL = 30.0


class ImageRejected(Exception):
    """Uploaded media is not a supported, bounded, decodable image."""


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
    """Compose the view's TEXT only; the image travels as the message's media.

    This function used to append the versioned ``/appearance/preview/<version>``
    URL to the body. Telegram then rendered its own web-page preview card of
    that page (the Render hostname and the Guide site's title/description)
    instead of the owner's image — the reported defect. No URL is composed
    here any more: the selected image is sent as the inline result's PHOTO and
    this text becomes its caption, so a panel is either genuine media with a
    caption or genuine text, never a link.
    """
    return compose_text(panel_id, extra, title, body)


async def apply_to_inline_results(panel_id: str, extra: str, results: list) -> list:
    """Apply the appearance layer to freshly built inline results.

    The initial panel is an inline result, so it never passes through
    ``panels._handle_panel``. The title is recovered from the panel registry (or
    the canonical default) and the body is the remaining text, so the same
    composition is used for the first render and every later callback edit.

    When a preview is genuinely deliverable, the result is REBUILT as a photo
    result whose ``send_message`` is a media message: Telegram fetches the
    selected image itself and shows it as the message's photo with this text as
    its caption. The image URL is an API parameter, never message text, so no
    link and no web-page card can appear.

    Anything that makes media impossible (preview off, no selected image, the
    selected object unreadable, or a caption above Telegram's limit) falls back
    to the plain TEXT result with web-page previews suppressed — never to a URL
    in the message.
    """
    if not is_eligible(panel_id) or extra or not results:
        return results

    ready = await delivery_ready()
    built_state: tuple[str, str] = ("text", "")
    composed_any = False
    output: list = []
    for result in results:
        message = getattr(result, "send_message", None)
        text = getattr(message, "message", None)
        if not isinstance(text, str):
            output.append(result)
            continue
        split = _split_rendered_text(text, panel_id)
        if split is None:
            output.append(result)
            continue
        title, body = with_preview(panel_id, extra, *split)
        composed = _compose_rendered(title, body)
        composed_any = True
        caption = caption_for(composed)
        if preview_active() and ready and caption:
            output.append(_as_photo_result(result, caption, telegram_photo_url()))
            built_state = ("media", preview_version())
            continue
        message.message = composed
        if hasattr(message, "no_webpage"):
            message.no_webpage = True
        output.append(result)

    # Only claim a state when a result was genuinely composed; a result whose
    # text could not be recognised is left alone rather than mislabelled.
    if composed_any:
        note_built(panel_id, built_state)
    return output


def _as_photo_result(result, caption: str, image_url: str):
    """Rebuild one inline result as a PHOTO result carrying ``caption``.

    ``InputBotInlineResult`` with ``type="photo"`` and an
    ``InputWebDocument`` content is Telegram's own shape for an inline photo
    (the Bot API's ``InlineQueryResultPhoto``): the server downloads the image
    from the document URL and sends it as the message media. The existing
    inline keyboard is carried over untouched, so navigation is unchanged.
    """
    from telethon.tl import types

    message = getattr(result, "send_message", None)
    markup = getattr(message, "reply_markup", None)
    return types.InputBotInlineResult(
        id=str(getattr(result, "id", "0") or "0"),
        type="photo",
        title=str(getattr(result, "title", "") or "LifeOS")[:255],
        content=types.InputWebDocument(
            url=image_url,
            size=0,
            mime_type="image/jpeg",
            attributes=[],
        ),
        send_message=types.InputBotInlineMessageMediaAuto(
            message=caption,
            reply_markup=markup,
        ),
    )


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


def token_from_key(key: str) -> str:
    """The short identifier/version of one gallery object key ("" if invalid)."""
    match = _KEY_RE.match(str(key or ""))
    return match.group(1) if match else ""


def preview_version() -> str:
    return token_from_key(settings_service.appearance_preview_image_key())


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
    """The selected-image view; the gallery itself is read by :func:`gallery`."""
    enabled = settings_service.appearance_preview_enabled()
    key = settings_service.appearance_preview_image_key()
    return {
        "enabled": enabled,
        "stored": bool(key),
        "selected": token_from_key(key),
        "active": preview_active(),
        "public_base": bool(public_base_url()),
        "storage": storage_available(),
        "mode": "image" if preview_active() else "text",
        "max_images": MAX_GALLERY_IMAGES,
    }


# ── Telegram media delivery ──────────────────────────────────────────────────
#
# The preview reaches Telegram as the panel message's PHOTO, with the panel's
# own text as its caption. There are exactly two delivery states, and a panel
# message can only be ONE of them:
#
#   ("media", <version>) — a photo message built from the selected object
#   ("text", "")        — a text message with web-page previews suppressed
#
# Telegram cannot turn a photo message back into a text one, and cannot swap a
# photo without an upload, so any change of state is delivered by REPLACING the
# panel message through the existing lifecycle (create first, delete the old
# one after) rather than by editing it in place.

#: ``(mode, version)`` last known to be on screen, per panel message.
_DELIVERED: dict[tuple[int, int], tuple[str, str]] = {}

#: State the inline builder actually produced, per panel id. The builder runs in
#: the Helper Bot's inline query; ``lifecycle.create_panel`` claims it once the
#: message exists, so a fresh panel is recorded as what it really is.
_BUILT: dict[str, tuple[str, str]] = {}

_READY_AT: float = 0.0
_READY_VALUE: bool = False


def caption_for(text: str) -> str:
    """The media caption for a composed panel text ("" when it cannot be one)."""
    text = text or ""
    if not text.strip() or len(text) > MAX_CAPTION_CHARS:
        return ""
    return text


def telegram_photo_url() -> str:
    """The public JPEG rendition of the selected image ("" when unavailable)."""
    base = public_base_url()
    version = preview_version()
    if not base or not version:
        return ""
    return f"{base}/appearance/image/{version}?fmt=jpeg"


async def delivery_ready(force: bool = False) -> bool:
    """Whether the SELECTED object genuinely exists and can be delivered.

    Listing the prefix is what tells a real image apart from a stale key, so a
    missing object degrades to a text panel instead of Telegram failing to
    fetch a 404 as the message's photo. The answer is cached briefly: panel
    renders are frequent and the gallery changes rarely.
    """
    global _READY_AT, _READY_VALUE
    now = time.monotonic()
    if not force and _READY_AT and (now - _READY_AT) < _DELIVERY_READY_TTL:
        return _READY_VALUE

    ready = False
    if preview_active() and storage_available():
        key = settings_service.appearance_preview_image_key()
        try:
            entries = await asyncio.to_thread(_list_sync)
            ready = any(entry["key"] == key for entry in entries)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — an unreadable gallery is not media
            logger.warning("appearance: delivery readiness check failed: %s", exc)
            ready = False
    _READY_AT = now
    _READY_VALUE = ready
    return ready


def note_built(panel_id: str, state: tuple[str, str]) -> None:
    """Record what the inline builder produced for this panel query."""
    _BUILT[str(panel_id)] = state


def claim_built(panel_id: str) -> tuple[str, str] | None:
    """Take (and clear) the state the inline builder produced for ``panel_id``."""
    return _BUILT.pop(str(panel_id), None)


def note_delivered(chat_id: int, msg_id: int, state: tuple[str, str]) -> None:
    """Record the state that is actually on screen for one panel message."""
    key = (int(chat_id or 0), int(msg_id or 0))
    if not key[0] or not key[1]:
        return
    _DELIVERED[key] = state
    if len(_DELIVERED) > 256:  # bounded: the oldest panel is dropped
        _DELIVERED.pop(next(iter(_DELIVERED)))


def forget_delivered(chat_id: int, msg_id: int) -> None:
    _DELIVERED.pop((int(chat_id or 0), int(msg_id or 0)), None)


def delivered_state(chat_id: int, msg_id: int) -> tuple[str, str] | None:
    return _DELIVERED.get((int(chat_id or 0), int(msg_id or 0)))


def desired_state(title: str = "", body: str = "") -> tuple[str, str]:
    """The state the CURRENT settings should put on screen.

    ``title``/``body`` are the composed view about to be painted; a caption
    above Telegram's limit means text, so the full text is preserved.
    """
    caption = caption_for(_compose_rendered(title, body))
    if preview_active() and _READY_VALUE and caption:
        return ("media", preview_version())
    return ("text", "")


def needs_new_message(chat_id: int, msg_id: int, title: str, body: str) -> bool:
    """Whether this panel message must be REPLACED to match the desired state.

    Telegram cannot remove media from a message or exchange it without an
    upload, so a text↔media switch, or a different selected image, needs a new
    message. An unknown state (fresh process, panel created by an older build)
    is replaced once when media is what should be shown.
    """
    desired = desired_state(title, body)
    delivered = delivered_state(chat_id, msg_id)
    if delivered is None:
        return desired[0] == "media"
    return delivered != desired


def reset_delivery_state() -> None:
    """Forget every recorded state (process start, and isolated tests)."""
    global _READY_AT, _READY_VALUE
    _DELIVERED.clear()
    _BUILT.clear()
    _READY_AT = 0.0
    _READY_VALUE = False


def preview_meta(version: str) -> dict[str, str] | None:
    """Open Graph metadata for the SELECTED preview version, or ``None``."""
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
    """The Appearance bucket: the dedicated one only, never the archive bucket."""
    return (os.getenv("APPEARANCE_MEDIA_BUCKET") or "").strip()


def storage_available() -> bool:
    from backend.db import client as db_client

    return bool(bucket()) and db_client.get_db() is not None


def storage_help() -> str:
    return (
        "Durable image storage is not configured. Set APPEARANCE_MEDIA_BUCKET to "
        "the private Appearance Storage bucket and keep SUPABASE_URL / "
        "SUPABASE_SERVICE_ROLE_KEY set."
    )


# ── Content validation (real bytes, bounded, off the event loop) ─────────────

def sniff_image(data: bytes) -> tuple[str, int, int] | None:
    """Return ``(mime, width, height)`` from the bytes themselves.

    A cheap header probe used for the image response's content type; it never
    decides whether an upload is accepted (``validate_image`` does, by
    decoding).
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


def validate_image(data: bytes, declared_mime: str = "") -> tuple[str, int, int]:
    """Validate real content, bounds and decodability; raise :class:`ImageRejected`.

    Runs synchronously (callers offload it with ``asyncio.to_thread``) so no
    decoding ever happens on the event loop. The bytes are never modified: this
    function only reads them.
    """
    if not data:
        raise ImageRejected("the file is empty")
    if len(data) > MAX_IMAGE_BYTES:
        raise ImageRejected(
            f"the image is larger than {MAX_IMAGE_MB} MB "
            f"({len(data)} bytes received, limit {MAX_IMAGE_BYTES})"
        )
    try:
        from PIL import Image, UnidentifiedImageError
    except ImportError as exc:  # pragma: no cover - pinned dependency
        raise ImageRejected("image validation is unavailable on this server") from exc

    declared = (declared_mime or "").strip().lower()
    if declared.split(";")[0] == "image/jpg":
        declared = "image/jpeg"

    try:
        with Image.open(io.BytesIO(data)) as image:
            fmt = (image.format or "").upper()
            mime = _FORMAT_MIME.get(fmt)
            if mime is None:
                raise ImageRejected("that file is not a PNG, JPEG or WebP image")
            width, height = image.size
            if width < 1 or height < 1:
                raise ImageRejected("the image has no usable dimensions")
            if width > MAX_IMAGE_SIDE or height > MAX_IMAGE_SIDE:
                raise ImageRejected(
                    f"the image is {width}x{height}; the limit is "
                    f"{MAX_IMAGE_SIDE}px per side"
                )
            if width * height > MAX_IMAGE_PIXELS:
                raise ImageRejected(
                    f"the image is {width}x{height} "
                    f"({width * height / 1_000_000:.1f} megapixels); the limit is "
                    f"{MAX_IMAGE_PIXELS // 1_000_000} megapixels"
                )
            image.verify()
    except ImageRejected:
        raise
    except UnidentifiedImageError as exc:
        raise ImageRejected("that file is not a readable PNG, JPEG or WebP image") from exc
    except Image.DecompressionBombError as exc:
        raise ImageRejected("the image is too large to be decoded safely") from exc
    except (OSError, ValueError, SyntaxError, IndexError) as exc:
        raise ImageRejected("that image file is damaged or truncated") from exc

    if declared in _MIME_EXT and declared != mime:
        raise ImageRejected(
            f"the file's content is {mime} but it was sent as {declared}"
        )
    _prove_decodable(data, fmt, width, height)
    return mime, width, height


def _prove_decodable(data: bytes, fmt: str, width: int, height: int) -> None:
    """Confirm the container is complete and the pixel data really decodes.

    PNG is proved by walking its own chunks and streaming the IDAT through
    zlib in small bounded pieces: a truncated, corrupt or empty pixel stream is
    detected without ever materialising the bitmap. JPEG is decoded at a
    reduced draft size, which detects a truncated entropy stream cheaply.
    WebP has no draft mode, so it is fully decoded only under
    :data:`_DECODE_PIXEL_BUDGET`; above that its RIFF container is checked
    instead of allocating a very large bitmap.
    """
    from PIL import Image

    if fmt == "PNG":
        _prove_png_stream(data)
        return
    _require_complete_container(data, fmt)
    if fmt == "WEBP" and width * height > _DECODE_PIXEL_BUDGET:
        return
    try:
        with Image.open(io.BytesIO(data)) as image:
            if fmt == "JPEG":
                image.draft("RGB", (1024, 1024))
            image.load()
    except Image.DecompressionBombError as exc:
        raise ImageRejected("the image is too large to be decoded safely") from exc
    except (OSError, ValueError, SyntaxError, IndexError) as exc:
        raise ImageRejected("that image file is damaged or truncated") from exc


def _prove_png_stream(data: bytes) -> None:
    """Verify every PNG chunk CRC and that the IDAT stream is complete.

    The decompressed length is checked against the size the header promises
    (non-interlaced images), and decompression is fed in fixed-size pieces with
    a bounded output so a compressed bomb can neither exhaust memory nor run
    unbounded. A PNG with no IDAT, a short one, or one that ends mid-stream is
    rejected here.
    """
    width = int.from_bytes(data[16:20], "big")
    height = int.from_bytes(data[20:24], "big")
    bit_depth = data[24]
    color_type = data[25]
    interlace = data[28]
    channels = {0: 1, 2: 3, 3: 1, 4: 2, 6: 4}.get(color_type)
    if channels is None:
        raise ImageRejected("that image file is damaged or truncated")
    expected = 0
    if not interlace:
        row = (width * channels * bit_depth + 7) // 8
        expected = height * (row + 1)

    index = 8
    seen_idat = False
    produced = 0
    inflater = zlib.decompressobj()
    while index + 8 <= len(data):
        length = int.from_bytes(data[index:index + 4], "big")
        kind = data[index + 4:index + 8]
        start = index + 8
        end = start + length
        if end + 4 > len(data):
            raise ImageRejected("that image file is damaged or truncated")
        payload = data[start:end]
        crc = int.from_bytes(data[end:end + 4], "big")
        if (zlib.crc32(kind + payload) & 0xFFFFFFFF) != crc:
            raise ImageRejected("that image file is damaged or truncated")
        if kind == b"IDAT":
            seen_idat = True
            produced += _inflate_bounded(inflater, payload)
            if expected and produced > expected:
                raise ImageRejected("that image file is damaged or truncated")
        elif kind == b"IEND":
            break
        index = end + 4

    if not seen_idat:
        raise ImageRejected("that image file is damaged or truncated")
    try:
        produced += len(inflater.flush())
    except zlib.error as exc:
        raise ImageRejected("that image file is damaged or truncated") from exc
    if not inflater.eof or produced <= 0:
        raise ImageRejected("that image file is damaged or truncated")
    if expected and produced != expected:
        raise ImageRejected("that image file is damaged or truncated")


def _inflate_bounded(inflater, payload: bytes, step: int = 65_536, cap: int = 1 << 20) -> int:
    """Decompress ``payload`` in bounded slices, returning the output length."""
    produced = 0
    try:
        for offset in range(0, len(payload), step):
            piece = payload[offset:offset + step]
            while piece:
                produced += len(inflater.decompress(piece, cap))
                piece = inflater.unconsumed_tail
    except zlib.error as exc:
        raise ImageRejected("that image file is damaged or truncated") from exc
    return produced


def _require_complete_container(data: bytes, fmt: str) -> None:
    """Reject a JPEG/WebP whose own container does not end where it should."""
    if fmt == "JPEG":
        if not data.rstrip(b"\x00").endswith(b"\xff\xd9"):
            raise ImageRejected("that image file is damaged or truncated")
        return
    if fmt == "WEBP":
        if len(data) < 12 or int.from_bytes(data[4:8], "little") + 8 > len(data):
            raise ImageRejected("that image file is damaged or truncated")


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


# ── Storage primitives ──────────────────────────────────────────────────────

def _db():
    from backend.db import client as db_client

    db = db_client.get_db()
    if db is None:
        raise RuntimeError("database unavailable")
    return db


def _upload_sync(key: str, data: bytes, mime: str) -> None:
    response = _db().storage.from_(bucket()).upload(
        key, data, {"content-type": mime, "upsert": "false"}
    )
    status = getattr(response, "status_code", None)
    if isinstance(status, int) and status >= 400:
        raise RuntimeError(f"storage upload refused ({status})")


def _delete_sync(key: str) -> bool:
    try:
        _db().storage.from_(bucket()).remove([key])
        return True
    except Exception as exc:  # noqa: BLE001 — a failed deletion is reported, never fatal
        logger.warning("appearance: could not delete stored object: %s", exc)
        return False


def _download_sync(key: str) -> bytes | None:
    data = _db().storage.from_(bucket()).download(key)
    if isinstance(data, (bytes, bytearray)) and data:
        return bytes(data)
    return None


def _entry_key(entry: Any) -> str | None:
    """The validated object key of one listing entry, or ``None``.

    Only a direct child of :data:`IMAGES_PREFIX` whose name is exactly a
    generated key is part of the gallery — folder placeholders, nested paths
    and anything a human put in the bucket are ignored.
    """
    if not isinstance(entry, dict):
        return None
    name = str(entry.get("name") or "").strip()
    if not name or "/" in name:
        return None
    key = f"{IMAGES_PREFIX}/{name}"
    return key if _KEY_RE.match(key) else None


def _list_sync() -> list[dict[str, Any]]:
    """List the gallery objects (newest first) from the Storage API."""
    raw = _db().storage.from_(bucket()).list(
        IMAGES_PREFIX,
        {"limit": 100, "sortBy": {"column": "created_at", "order": "desc"}},
    )
    items: list[dict[str, Any]] = []
    for entry in raw or []:
        key = _entry_key(entry)
        if key is None:
            continue
        metadata = entry.get("metadata")
        metadata = metadata if isinstance(metadata, dict) else {}
        try:
            size = int(metadata.get("size") or 0)
        except (TypeError, ValueError):
            size = 0
        extension = key.rsplit(".", 1)[-1]
        items.append({
            "key": key,
            "token": token_from_key(key),
            "mime": _EXT_MIME.get(extension, "application/octet-stream"),
            "size": size,
            "created_at": str(entry.get("created_at") or ""),
        })
    items.sort(key=lambda item: (item["created_at"], item["token"]), reverse=True)
    return items


async def gallery() -> dict[str, Any]:
    """The stored gallery plus the selected image, read from Storage itself.

    A listing failure is returned as ``error`` with an EMPTY ``items`` list and
    ``known=False``: the caller must never present that as an empty gallery.
    """
    selected_key = settings_service.appearance_preview_image_key()
    state: dict[str, Any] = {
        "items": [],
        "error": "",
        "known": False,
        "max": MAX_GALLERY_IMAGES,
        "selected_key": selected_key,
        "selected": token_from_key(selected_key),
        "missing_selected": False,
        "available": storage_available(),
    }
    if not storage_available():
        state["error"] = storage_help()
        return state
    try:
        entries = await asyncio.to_thread(_list_sync)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — an unreadable gallery is reported honestly
        logger.warning("appearance: gallery listing failed: %s", exc)
        state["error"] = (
            f"{sym.STATE_FAILED} The image gallery could not be read from Storage."
        )
        return state

    for entry in entries:
        entry["active"] = bool(selected_key) and entry["key"] == selected_key
    state["items"] = entries
    state["known"] = True
    state["count"] = len(entries)
    state["missing_selected"] = bool(selected_key) and not any(
        entry["active"] for entry in entries
    )
    return state


def _find(state: dict[str, Any], token: str) -> dict[str, Any] | None:
    for entry in state.get("items") or []:
        if entry.get("token") == token:
            return entry
    return None


def _normalize_token(token: str) -> str:
    value = str(token or "").strip()
    return value if _TOKEN_RE.match(value) else ""


def _gallery_unreadable(state: dict[str, Any]) -> tuple[bool, str]:
    return False, (
        f"{sym.STATE_FAILED} The image gallery could not be read, so nothing was "
        "changed. Try again in a moment."
    )


async def select_image(token: str) -> tuple[bool, str]:
    """Make one STORED image the preview; never uploads, copies or deletes."""
    if not storage_available():
        return False, f"{sym.STATE_FAILED} {storage_help()}"
    wanted = _normalize_token(token)
    if not wanted:
        return False, (
            f"{sym.STATE_FAILED} That image reference is not valid, so nothing "
            "was changed."
        )
    state = await gallery()
    if not state["known"]:
        return _gallery_unreadable(state)
    entry = _find(state, wanted)
    if entry is None:
        return False, (
            f"{sym.STATE_FAILED} That image is no longer in the gallery. The "
            "selection was not changed."
        )
    if entry["key"] == state["selected_key"]:
        return True, f"{sym.OP_SAVE} That image is already the preview."
    if not settings_service.set_appearance_preview_image_key(entry["key"]):
        return False, (
            f"{sym.STATE_FAILED} The selection could not be saved, so nothing "
            "was changed."
        )
    return True, f"{sym.OP_SAVE} Preview image selected. Existing images were kept."


async def delete_image(token: str) -> tuple[bool, str]:
    """Delete exactly one gallery object; the active key is cleared first."""
    if not storage_available():
        return False, f"{sym.STATE_FAILED} {storage_help()}"
    wanted = _normalize_token(token)
    if not wanted:
        return False, (
            f"{sym.STATE_FAILED} That image reference is not valid, so nothing "
            "was deleted."
        )
    state = await gallery()
    if not state["known"]:
        return _gallery_unreadable(state)
    entry = _find(state, wanted)
    if entry is None:
        return False, (
            f"{sym.STATE_FAILED} That image is no longer in the gallery, so "
            "nothing was deleted."
        )

    was_active = entry["key"] == state["selected_key"]
    if was_active and not settings_service.set_appearance_preview_image_key(""):
        return False, (
            f"{sym.STATE_FAILED} The selected image could not be cleared, so the "
            "image was not deleted."
        )

    removed = await asyncio.to_thread(_delete_sync, entry["key"])
    if not removed:
        if was_active:
            return False, (
                f"{sym.STATE_FAILED} Storage refused the deletion. The image is "
                "no longer selected, but it is still stored; try deleting it again."
            )
        return False, (
            f"{sym.STATE_FAILED} Storage refused the deletion, so the image is "
            "still there. Nothing else was changed."
        )
    if was_active:
        return True, (
            f"{sym.OP_DELETE} Preview image deleted. The preview is text-only "
            "until another image is selected."
        )
    return True, (
        f"{sym.OP_DELETE} Image deleted. The selected preview was not changed."
    )


async def clear_selection() -> tuple[bool, str]:
    """Drop the selected reference without touching any stored image."""
    if not settings_service.appearance_preview_image_key():
        return True, f"{sym.SEPARATOR} No image is selected."
    if not settings_service.set_appearance_preview_image_key(""):
        return False, (
            f"{sym.STATE_FAILED} The selection could not be cleared, so nothing "
            "was changed."
        )
    return True, (
        f"{sym.OP_DELETE} Selection cleared. Every stored image is still in the "
        "gallery; the panels stay text-only until another is selected."
    )


async def install_image(data: bytes, declared_mime: str = "") -> tuple[bool, str]:
    """Validate, upload as a NEW object, then select it. Never evicts images."""
    if not storage_available():
        return False, f"{sym.STATE_FAILED} {storage_help()}"

    async with _UPLOAD_LOCK:
        state = await gallery()
        if not state["known"]:
            return False, (
                f"{sym.STATE_FAILED} The image gallery could not be read, so the "
                "upload was refused and nothing was changed."
            )
        if len(state["items"]) >= MAX_GALLERY_IMAGES:
            return False, (
                f"{sym.STATE_FAILED} The gallery already holds "
                f"{len(state['items'])}/{MAX_GALLERY_IMAGES} images. Delete one "
                "before uploading another."
            )

        try:
            mime, width, height = await asyncio.to_thread(
                validate_image, data, declared_mime
            )
        except ImageRejected as exc:
            return False, f"{sym.STATE_FAILED} {exc}."

        key = f"{IMAGES_PREFIX}/{uuid.uuid4().hex}.{_MIME_EXT[mime]}"
        try:
            await asyncio.to_thread(_upload_sync, key, data, mime)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001 — an upload failure is reported
            logger.warning("appearance: preview upload failed: %s", exc)
            return False, (
                f"{sym.STATE_FAILED} The upload failed, so nothing was changed."
            )

        try:
            stored = await asyncio.to_thread(_list_sync)
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001 — verify what we just wrote, best effort
            stored = []
        if not any(entry["key"] == key for entry in stored):
            await asyncio.to_thread(_delete_sync, key)
            return False, (
                f"{sym.STATE_FAILED} Storage did not confirm the upload, so the "
                "copy was discarded."
            )

        if not settings_service.set_appearance_preview_image_key(key):
            # The object must not outlive a reference we failed to persist.
            await asyncio.to_thread(_delete_sync, key)
            return False, (
                f"{sym.STATE_FAILED} The image could not be selected in settings, "
                "so the uploaded copy was discarded."
            )

    return True, (
        f"{sym.OP_SAVE} Image uploaded and selected ({width}x{height}, "
        f"{len(stored)}/{MAX_GALLERY_IMAGES} stored). Original bytes kept as sent."
    )


async def image_payload(version: str) -> tuple[bytes, str] | None:
    """The stored image for the SELECTED version, with its content type."""
    if not version or version != preview_version() or not storage_available():
        return None
    try:
        data = await asyncio.to_thread(
            _download_sync, settings_service.appearance_preview_image_key()
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a missing object is a 404, not a crash
        logger.warning("appearance: preview download failed: %s", exc)
        return None
    if not data:
        return None
    sniffed = sniff_image(data)
    return data, (sniffed[0] if sniffed else "application/octet-stream")


def _encode_telegram_photo(data: bytes) -> bytes | None:
    """A JPEG rendition Telegram will accept as an inline photo.

    Delivery only: the stored object keeps the owner's original bytes exactly.
    Telegram re-encodes photos for its own preview anyway, and an inline photo
    fetch is limited to a JPEG document of a few megabytes. The image is
    encoded at quality 90 and, only if that still exceeds the fetch limit,
    progressively scaled down — never enlarged, never cropped.
    """
    try:
        from PIL import Image
    except ImportError:  # pragma: no cover - pinned dependency
        return None

    with Image.open(io.BytesIO(data)) as image:
        image.load()
        if image.mode not in ("RGB", "L"):
            rgba = image.convert("RGBA")
            canvas = Image.new("RGB", rgba.size, (255, 255, 255))
            canvas.paste(rgba, mask=rgba.split()[-1])
            frame = canvas
        else:
            frame = image if image.mode == "RGB" else image.convert("RGB")

        scale = 1.0
        for _attempt in range(4):
            candidate = frame
            if scale < 1.0:
                size = (
                    max(1, int(frame.width * scale)),
                    max(1, int(frame.height * scale)),
                )
                candidate = frame.resize(size, Image.LANCZOS)
            buffer = io.BytesIO()
            candidate.save(buffer, "JPEG", quality=90, optimize=True)
            encoded = buffer.getvalue()
            if len(encoded) <= TELEGRAM_PHOTO_MAX_BYTES:
                return encoded
            scale *= 0.75
        logger.warning(
            "appearance: cannot fit the preview into Telegram's inline photo "
            "limit (%d bytes)",
            TELEGRAM_PHOTO_MAX_BYTES,
        )
        return None


async def telegram_photo(version: str) -> bytes | None:
    """The selected image encoded for Telegram's inline photo fetch.

    ``None`` on a stale version, a missing object or an undecodable file: the
    caller answers 404 rather than serving a wrong image, and the panel falls
    back to text instead of showing a broken photo.
    """
    if not version or version != preview_version() or not storage_available():
        return None
    try:
        data = await asyncio.to_thread(
            _download_sync, settings_service.appearance_preview_image_key()
        )
        if not data:
            return None
        return await asyncio.to_thread(_encode_telegram_photo, data)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a bad image is a 404, not a crash
        logger.warning("appearance: telegram photo encoding failed: %s", exc)
        return None
