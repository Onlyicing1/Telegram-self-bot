"""Glass UI Appearance — one central customization layer.

Three independent surfaces share the existing singleton settings architecture
(``settings_service`` -> ``panel_settings_repository`` -> ``panel_settings``):

* a global header title/description override applied to every main page,
* per-main-page title/description overrides,
* an independent preview-image switch plus the stored object key.

Rendering is composed HERE and applied at the three existing render boundaries
(callback edits in ``panels`` and initial inline results in ``panel_render``).
No handler rewrites its own strings, so a customization can never leak into
another page or into dynamic operational content: the page's static
description is replaced and the remaining (dynamic) body is kept verbatim.
"""
from __future__ import annotations

import hashlib
import html
import logging
import os
import re
from contextlib import contextmanager
from contextvars import ContextVar
from dataclasses import dataclass
from typing import Any, Iterator

from backend.services import settings_service

logger = logging.getLogger(__name__)

TITLE_MAX = 64
BODY_MAX = 500
PAGE_ID_RE = re.compile(r"^[a-z][a-z0-9_]{0,31}$")
IMAGE_KEY_RE = re.compile(r"^previews/[0-9a-f]{32}\.(?:png|jpg|webp)$")
ALLOWED_MIMES = ("image/png", "image/jpeg", "image/webp")


@dataclass(frozen=True)
class PageSpec:
    panel_id: str
    title: str
    description: str


#: The main-menu categories ONLY (sub-panels, pickers, wizard steps and
#: dynamic result screens are deliberately absent). Order mirrors the menu.
MAIN_PAGES: tuple[PageSpec, ...] = (
    PageSpec("save", "Save",
             "Deep Save downloads the message and re-uploads it as a new Saved Messages message."),
    PageSpec("del", "Delete", "Choose a deletion mode:"),
    PageSpec("profile", "Profile", "Choose a section:"),
    PageSpec("db", "Database", "Choose an action:"),
    PageSpec("general", "General", "Tap a button to execute instantly."),
    PageSpec("settings", "Settings", ""),
    PageSpec("cipher", "Cipher", "Private Communications\n\nChoose a section:"),
    PageSpec("todo", "Todo", ""),
    PageSpec("ai", "AI", ""),
    PageSpec("appearance", "Appearance",
             "Personalize the Glass UI header, page text, and preview image."),
)

MAIN_PAGE_IDS: tuple[str, ...] = tuple(page.panel_id for page in MAIN_PAGES)
_PAGE_BY_ID: dict[str, PageSpec] = {page.panel_id: page for page in MAIN_PAGES}

#: Panels the GLOBAL header applies to: the root menu (not a category, so it is
#: not per-page customizable) plus every main category.
_MENU_SPEC = PageSpec("menu", "LifeOS", "Choose a category:")
HEADER_PAGES: dict[str, PageSpec] = {
    _MENU_SPEC.panel_id: _MENU_SPEC,
    **{page.panel_id: page for page in MAIN_PAGES},
}

_current_panel: ContextVar[str] = ContextVar("appearance_panel", default="")


@contextmanager
def rendering(panel_id: str) -> Iterator[None]:
    """Bind the panel id while an inline builder renders its results."""
    token = _current_panel.set(str(panel_id or ""))
    try:
        yield
    finally:
        _current_panel.reset(token)


def current_panel() -> str:
    return _current_panel.get()


def is_main_page(panel_id: str) -> bool:
    return panel_id in _PAGE_BY_ID


def page_spec(panel_id: str) -> PageSpec | None:
    return _PAGE_BY_ID.get(panel_id)


# ── Stored values ────────────────────────────────────────────────────────────

def header_title() -> str:
    value = settings_service.get_setting("appearance_header_title", "")
    return value.strip() if isinstance(value, str) else ""


def header_body() -> str:
    value = settings_service.get_setting("appearance_header_body", "")
    return value.strip() if isinstance(value, str) else ""


def page_overrides() -> dict[str, dict[str, str]]:
    """The validated per-page map (unknown pages/fields are ignored)."""
    value = settings_service.get_setting("appearance_page_texts", {})
    if isinstance(value, str):
        import json

        try:
            value = json.loads(value)
        except (TypeError, ValueError):
            value = {}
    if not isinstance(value, dict):
        return {}
    result: dict[str, dict[str, str]] = {}
    for page_id, entry in value.items():
        if page_id not in _PAGE_BY_ID or not isinstance(entry, dict):
            continue
        clean: dict[str, str] = {}
        for field in ("title", "body"):
            raw = entry.get(field)
            if isinstance(raw, str) and raw.strip():
                clean[field] = raw.strip()
        if clean:
            result[page_id] = clean
    return result


def page_override(panel_id: str, field: str) -> str:
    return page_overrides().get(panel_id, {}).get(field, "")


def preview_enabled() -> bool:
    return bool(settings_service.get_setting("appearance_preview_enabled", False))


def preview_image_key() -> str:
    value = settings_service.get_setting("appearance_preview_image", "")
    return value if isinstance(value, str) and IMAGE_KEY_RE.match(value) else ""


def preview_image_meta() -> dict[str, Any]:
    value = settings_service.get_setting("appearance_preview_meta", {})
    if not isinstance(value, dict):
        return {}
    mime = value.get("mime")
    width = value.get("width")
    height = value.get("height")
    if mime not in ALLOWED_MIMES:
        return {}
    if not isinstance(width, int) or not isinstance(height, int):
        return {}
    if not (16 <= width <= 4096 and 16 <= height <= 4096):
        return {}
    return {"mime": mime, "width": width, "height": height}


# ── Validation + writes (strict: never claim a cache-only write) ─────────────

def _valid_text(value: Any, limit: int) -> bool:
    return isinstance(value, str) and len(value.strip()) <= limit


def _valid_page_map(value: Any) -> bool:
    if not isinstance(value, dict):
        return False
    for page_id, entry in value.items():
        if page_id not in _PAGE_BY_ID or not isinstance(entry, dict):
            return False
        if set(entry) - {"title", "body"}:
            return False
        for field, limit in (("title", TITLE_MAX), ("body", BODY_MAX)):
            if field in entry and not _valid_text(entry[field], limit):
                return False
    return True


def _valid_image_key(value: Any) -> bool:
    return isinstance(value, str) and (value == "" or bool(IMAGE_KEY_RE.match(value)))


def _valid_image_meta(value: Any) -> bool:
    if value in ({}, None):
        return True
    if not isinstance(value, dict):
        return False
    mime = value.get("mime")
    width = value.get("width")
    height = value.get("height")
    return (
        mime in ALLOWED_MIMES
        and isinstance(width, int) and not isinstance(width, bool)
        and isinstance(height, int) and not isinstance(height, bool)
        and 16 <= width <= 4096 and 16 <= height <= 4096
        and set(value) <= {"mime", "width", "height"}
    )


def validate_for_key(key: str, value: Any) -> bool:
    """The appearance-specific validators (used by settings_service)."""
    if key == "appearance_header_title":
        return _valid_text(value, TITLE_MAX)
    if key == "appearance_header_body":
        return _valid_text(value, BODY_MAX)
    if key == "appearance_page_texts":
        return _valid_page_map(value)
    if key == "appearance_preview_enabled":
        return isinstance(value, bool)
    if key == "appearance_preview_image":
        return _valid_image_key(value)
    if key == "appearance_preview_meta":
        return _valid_image_meta(value)
    return True


def _write(key: str, value: Any) -> bool:
    return settings_service.set_setting_strict(key, value)


def set_header_title(value: str) -> bool:
    if not _valid_text(value, TITLE_MAX):
        return False
    return _write("appearance_header_title", value.strip())


def set_header_body(value: str) -> bool:
    if not _valid_text(value, BODY_MAX):
        return False
    return _write("appearance_header_body", value.strip())


def reset_header() -> bool:
    return settings_service.set_many_strict({
        "appearance_header_title": "",
        "appearance_header_body": "",
    })


def set_page_text(panel_id: str, field: str, value: str) -> bool:
    if panel_id not in _PAGE_BY_ID or field not in ("title", "body"):
        return False
    if not isinstance(value, str):
        return False
    limit = TITLE_MAX if field == "title" else BODY_MAX
    if len(value.strip()) > limit:
        return False
    mapping = page_overrides()
    entry = dict(mapping.get(panel_id, {}))
    if value.strip():
        entry[field] = value.strip()
    else:
        entry.pop(field, None)
    if entry:
        mapping[panel_id] = entry
    else:
        mapping.pop(panel_id, None)
    return _write("appearance_page_texts", mapping)


def reset_page_text(panel_id: str, field: str | None = None) -> bool:
    if panel_id not in _PAGE_BY_ID:
        return False
    mapping = page_overrides()
    entry = dict(mapping.get(panel_id, {}))
    if entry:
        if field in ("title", "body"):
            entry.pop(field, None)
        else:
            entry = {}
        if entry:
            mapping[panel_id] = entry
        else:
            mapping.pop(panel_id, None)
    return _write("appearance_page_texts", mapping)


def set_preview_enabled(enabled: bool) -> bool:
    return _write("appearance_preview_enabled", bool(enabled))


def set_preview_image(key: str, meta: dict[str, Any]) -> bool:
    if not _valid_image_key(key) or not key or not _valid_image_meta(meta):
        return False
    return settings_service.set_many_strict({
        "appearance_preview_image": key,
        "appearance_preview_meta": dict(meta),
    })


def clear_preview_image() -> bool:
    return settings_service.set_many_strict({
        "appearance_preview_image": "",
        "appearance_preview_meta": {},
    })


# ── Composition ──────────────────────────────────────────────────────────────

def _strip_heading(body: str, title: str) -> str:
    """Drop a leading heading that repeats the title (e.g. ``**AI**``)."""
    text = str(body or "")
    if not title:
        return text
    first, sep, rest = text.partition("\n")
    probe = first.strip()
    while probe and not (probe[0].isalnum() or probe[0] in "*_"):
        probe = probe[1:].lstrip()
    probe = probe.strip("*_ ").strip()
    if probe and probe.casefold() == str(title).strip().casefold():
        return rest.lstrip("\n") if sep else ""
    return text


def _strip_description(body: str, description: str) -> str:
    text = str(body or "")
    if not description:
        return text
    lead = text.lstrip()
    if lead.startswith(description):
        return lead[len(description):].lstrip("\n ")
    return text


def compose(panel_id: str, title: str, body: str) -> tuple[str, str]:
    """Apply global + per-page text customization to one main panel.

    A panel with no override is returned untouched. Dynamic content after the
    page's static description is preserved verbatim; only the title and that
    description are replaceable.
    """
    spec = HEADER_PAGES.get(panel_id)
    if spec is None:
        return title, body
    try:
        page = page_overrides().get(panel_id, {}) if is_main_page(panel_id) else {}
        global_title = header_title()
        global_body = header_body()
        page_title = page.get("title", "")
        page_body = page.get("body", "")
        if not (global_title or global_body or page_title or page_body):
            return title, body

        effective_title = page_title or global_title or str(title or spec.title)
        rest = _strip_heading(body, title or spec.title)
        rest = _strip_description(rest, spec.description)
        effective_description = page_body or global_body or spec.description

        parts: list[str] = []
        if effective_description:
            parts.append(effective_description)
        if rest.strip():
            parts.append(rest.strip())
        return effective_title, "\n\n".join(parts)
    except Exception as exc:  # noqa: BLE001 — customization can never break a panel
        logger.warning("[APPEARANCE] compose failed for panel=%s: %s", panel_id, type(exc).__name__)
        return title, body


def effective_text(panel_id: str) -> tuple[str, str]:
    """The effective (title, description) for a panel, for previews/OG tags."""
    spec = HEADER_PAGES.get(panel_id) or _MENU_SPEC
    page = page_overrides().get(panel_id, {}) if is_main_page(panel_id) else {}
    title = page.get("title") or header_title() or spec.title
    body = page.get("body") or header_body() or spec.description
    return title, body


# ── Telegram link-preview identity ───────────────────────────────────────────

def public_base_url() -> str:
    base = os.getenv("PUBLIC_BASE_URL") or os.getenv("RENDER_EXTERNAL_URL") or ""
    return base.rstrip("/")


def preview_version() -> str:
    key = preview_image_key()
    if not key:
        return ""
    return hashlib.sha256(key.encode("utf-8")).hexdigest()[:16]


def preview_page_url(panel_id: str) -> str:
    """The public, per-version preview URL, or "" when it cannot be built."""
    base = public_base_url()
    key = preview_image_key()
    if not (preview_enabled() and base and key):
        return ""
    page = panel_id if PAGE_ID_RE.match(str(panel_id or "")) else "menu"
    return f"{base}/preview/{preview_version()}/{page}"


def preview_image_url(panel_id: str) -> str:
    page_url = preview_page_url(panel_id)
    return f"{page_url}/image" if page_url else ""


def og_html(panel_id: str) -> str:
    """The preview document Telegram reads; a browser is redirected to /guide."""
    title, description = effective_text(panel_id)
    image = preview_image_url(panel_id)
    base = public_base_url()
    meta = preview_image_meta()
    safe_title = html.escape(title or "LifeOS")
    safe_desc = html.escape(description or "LifeOS guide")
    tags = [
        '<!doctype html><html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>{safe_title}</title>",
        f'<meta property="og:title" content="{safe_title}">',
        f'<meta property="og:description" content="{safe_desc}">',
        f'<meta property="og:url" content="{html.escape(base + "/guide")}">',
        '<meta property="og:type" content="website">',
        f'<meta property="og:image" content="{html.escape(image)}">',
        f'<meta property="og:image:secure_url" content="{html.escape(image)}">',
    ]
    if meta:
        tags.extend([
            f'<meta property="og:image:type" content="{html.escape(meta["mime"])}">',
            f'<meta property="og:image:width" content="{int(meta["width"])}">',
            f'<meta property="og:image:height" content="{int(meta["height"])}">',
        ])
    tags.extend([
        '<meta name="twitter:card" content="summary_large_image">',
        f'<meta name="twitter:image" content="{html.escape(image)}">',
        '<meta http-equiv="refresh" content="0; url=/guide">',
        "</head><body>",
        f'<p><a href="/guide">{safe_title}</a></p>',
        "</body></html>",
    ])
    return "".join(tags)
