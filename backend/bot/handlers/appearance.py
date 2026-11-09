"""Appearance — owner-only personalization of the Glass UI.

Reached from the ONE new main-menu entry (``panel:appearance``) and built on the
existing panel registry, callback router, pending-input listener and singleton
settings service. Three independent sections:

* Header Customization — global title/description overrides.
* Page Text Customization — per main-page title/description overrides.
* Preview Image — independent ON/OFF switch plus an owner-uploaded image.

Every write is strict: a rejected database write is reported as a failure and
never silently applied to the cache only. Panel repaints after a text/media
change go through the Helper Bot's inline message identifier; the owner's
self client is used ONLY to read the uploaded image and delete the upload
message it received — never to edit the panel and never as a fallback editor.
"""
from __future__ import annotations

import asyncio
import io
import logging

from telethon.tl import types
from telethon.tl.functions.messages import EditInlineBotMessageRequest

from backend.bot.handlers.guard import is_owner
from backend.helper import (
    InlinePanelBuilder,
    register_action,
    register_inline_builder,
    register_input,
    register_panel,
    render,
)
from backend.helper import symbols as sym
from backend.helper.client import get_client
from backend.helper.inline_engine import get_owner_id, get_self_client
from backend.helper.panel_edit import inline_message_id_from_event
from backend.helper.panel_render import render_edit
from backend.helper.panels import _finalize_panel
from backend.helper.styles import DANGER
from backend.services import appearance_service as appearance
from backend.services import preview_storage
from backend.services.image_probe import probe_image

logger = logging.getLogger(__name__)


def _owner_ok(event) -> bool:
    return is_owner(event, get_owner_id())


def _on_off(value: bool) -> str:
    return sym.label(sym.STATE_ACTIVE, "ON") if value else sym.label(sym.OP_CLOSE, "OFF")


def _media_for(panel_id: str):
    """The media that mirrors the CURRENT switch: preview or genuine text-only."""
    url = appearance.preview_page_url(panel_id)
    if url:
        return types.InputMediaWebPage(
            url=url, force_small_media=True, optional=False,
        )
    return types.InputMediaEmpty()


async def _paint_existing(
    client,
    inline_id,
    panel_id: str,
    chat_id: int,
    msg_id: int,
    title: str,
    body: str,
    buttons: list,
) -> bool:
    """Edit the SAME inline panel through the Helper Bot only.

    No self-client fallback and no replacement message: if the helper cannot
    address the inline message, the failure is logged and nothing is sent.
    """
    if client is None or inline_id is None:
        logger.warning(
            "[APPEARANCE] cannot repaint panel=%s — no Helper Bot inline identifier",
            panel_id,
        )
        return False
    try:
        final_title, final_body, final_buttons = _finalize_panel(
            title, body, list(buttons or []), panel_id, chat_id or None, msg_id or None,
        )
        text, built = render_edit(final_title, final_body, final_buttons)
        request = EditInlineBotMessageRequest(
            id=inline_id,
            message=text,
            media=_media_for(panel_id),
            reply_markup=types.ReplyInlineMarkup(rows=built) if built else None,
        )
        await client(request)
        return True
    except Exception as exc:  # noqa: BLE001 — reported, never replaced by a second message
        logger.warning(
            "[APPEARANCE] Helper Bot inline edit failed panel=%s: %s",
            panel_id, type(exc).__name__,
        )
        return False


# ── Views ────────────────────────────────────────────────────────────────────

def _home_view(notice: str = "") -> tuple[str, str, list]:
    builder = InlinePanelBuilder()
    builder.add_row(sym.label(sym.PANEL_HEADING, "Header Customization"), "panel:appearance_header")
    builder.add_row(sym.label(sym.OP_EDIT, "Page Text Customization"), "panel:appearance_pages")
    builder.add_row(sym.label(sym.MEDIA_PHOTO, "Preview Image"), "panel:appearance_preview")

    lines = [
        "Customize the Glass UI without changing any page's operation.",
        "",
        f"**Header:** {'customized' if appearance.header_title() or appearance.header_body() else 'defaults'}",
        f"**Page text:** {len(appearance.page_overrides())} page(s) customized",
        f"**Preview image:** {_on_off(appearance.preview_enabled())}",
    ]
    if notice:
        lines.extend(["", notice])
    return "Appearance", "\n".join(lines), builder.build()


def _header_view(notice: str = "") -> tuple[str, str, list]:
    title = appearance.header_title()
    body = appearance.header_body()
    lines = [
        f"**Global title:** {title if title else '— default per page —'}",
        f"**Global description:** {body if body else '— default per page —'}",
        "",
        "_An override applies to every main page. A page's own customization "
        "takes precedence; resetting clears the override._",
    ]
    if notice:
        lines.extend(["", notice])
    builder = InlinePanelBuilder()
    builder.add_row(sym.label(sym.OP_EDIT, "Edit title"), "input:appearance_header:title")
    builder.add_row(sym.label(sym.OP_EDIT, "Edit description"), "input:appearance_header:body")
    builder.add_row(sym.label(sym.OP_REFRESH, "Reset title"), "action:appearance_header_reset_title")
    builder.add_row(sym.label(sym.OP_REFRESH, "Reset description"), "action:appearance_header_reset_body")
    builder.add_row(sym.label(sym.OP_REFRESH, "Reset both"), "action:appearance_header_reset_all")
    return "Header Customization", "\n".join(lines), builder.build()


def _pages_view(notice: str = "") -> tuple[str, str, list]:
    overrides = appearance.page_overrides()
    builder = InlinePanelBuilder()
    for page in appearance.MAIN_PAGES:
        customized = bool(overrides.get(page.panel_id))
        mark = sym.OP_EDIT if customized else sym.SEPARATOR
        builder.add_row(f"{mark} {page.title}", f"panel:appearance_page:{page.panel_id}")
    lines = [
        "Choose a main page. Only top-level categories are eligible.",
        "",
        f"**Customized:** {sum(1 for k in overrides if appearance.is_main_page(k))} / {len(appearance.MAIN_PAGES)}",
    ]
    if notice:
        lines.extend(["", notice])
    return "Page Text Customization", "\n".join(lines), builder.build()


def _page_view(panel_id: str, notice: str = "") -> tuple[str, str, list]:
    spec = appearance.page_spec(panel_id)
    if spec is None:
        return _pages_view(f"{sym.STATE_WARNING} That page is not eligible for customization.")
    page = appearance.page_overrides().get(panel_id, {})
    effective_title, effective_body = appearance.effective_text(panel_id)
    lines = [
        f"**Title:** {effective_title}",
        f"**Description:** {effective_body if effective_body else '— dynamic content preserved —'}",
        "",
        f"**Title override:** {'yes' if page.get('title') else 'no'}",
        f"**Description override:** {'yes' if page.get('body') else 'no'}",
    ]
    if notice:
        lines.extend(["", notice])
    builder = InlinePanelBuilder()
    builder.add_row(sym.label(sym.OP_EDIT, "Edit title"), f"input:appearance_page:title:{panel_id}")
    builder.add_row(sym.label(sym.OP_EDIT, "Edit description"), f"input:appearance_page:body:{panel_id}")
    builder.add_row(sym.label(sym.OP_REFRESH, "Reset title"), f"action:appearance_page_reset_title:{panel_id}")
    builder.add_row(sym.label(sym.OP_REFRESH, "Reset description"), f"action:appearance_page_reset_body:{panel_id}")
    builder.add_row(sym.label(sym.OP_REFRESH, "Reset page"), f"action:appearance_page_reset_all:{panel_id}")
    return f"{spec.title} — Text", "\n".join(lines), builder.build()


def _preview_view(notice: str = "") -> tuple[str, str, list]:
    enabled = appearance.preview_enabled()
    key = appearance.preview_image_key()
    meta = appearance.preview_image_meta()
    lines = [
        f"**Preview:** {_on_off(enabled)}",
        f"**Stored image:** {'yes' if key else 'none'}",
    ]
    if meta:
        lines.append(f"**Image size:** {meta['width']} × {meta['height']}")
    if enabled and not key:
        lines.append("")
        lines.append(f"{sym.STATE_WARNING} No image stored — upload one, or leave the panel text-only.")
    if key and not preview_storage.available():
        lines.append("")
        lines.append(f"{sym.STATE_WARNING} Storage is not configured — the stored image cannot be served.")
    if enabled and key and not appearance.public_base_url():
        lines.append("")
        lines.append(f"{sym.STATE_WARNING} PUBLIC_BASE_URL is not set — Telegram cannot fetch the preview.")
    if notice:
        lines.extend(["", notice])

    builder = InlinePanelBuilder()
    builder.add_row(
        sym.label(sym.OP_REFRESH, f"Turn preview {'OFF' if enabled else 'ON'}"),
        "action:appearance_preview_toggle",
    )
    builder.add_row(
        sym.label(sym.OP_UPLOAD, "Replace image" if key else "Upload image"),
        "input:appearance_preview:upload",
    )
    if key:
        builder.add_row(
            sym.label(sym.OP_DELETE, "Remove image"),
            "action:appearance_preview_remove",
            DANGER,
        )
    return "Preview Image", "\n".join(lines), builder.build()


# ── Panel handlers ───────────────────────────────────────────────────────────

async def _appearance_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    if not _owner_ok(event):
        return None
    return _home_view()


async def _appearance_inline_builder(event, extra: str) -> list:
    title, body, buttons = _home_view()
    return [render(title, body, buttons)]


async def _appearance_header_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    if not _owner_ok(event):
        return None
    return _header_view()


async def _appearance_header_inline_builder(event, extra: str) -> list:
    title, body, buttons = _header_view()
    return [render(title, body, buttons)]


async def _appearance_pages_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    if not _owner_ok(event):
        return None
    return _pages_view()


async def _appearance_pages_inline_builder(event, extra: str) -> list:
    title, body, buttons = _pages_view()
    return [render(title, body, buttons)]


async def _appearance_page_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    if not _owner_ok(event):
        return None
    return _page_view(str(extra or "").strip())


async def _appearance_page_inline_builder(event, extra: str) -> list:
    title, body, buttons = _page_view(str(extra or "").strip())
    return [render(title, body, buttons)]


async def _appearance_preview_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    if not _owner_ok(event):
        return None
    return _preview_view()


async def _appearance_preview_inline_builder(event, extra: str) -> list:
    title, body, buttons = _preview_view()
    return [render(title, body, buttons)]


# ── Header resets ────────────────────────────────────────────────────────────

async def _header_reset_title_action(event, extra: str, chat_id: int):
    if not _owner_ok(event):
        return None
    ok = appearance.set_header_title("")
    return _header_view(sym.label(sym.OP_SAVE, "Title reset.") if ok else f"{sym.STATE_FAILED} Could not reset the title.")


async def _header_reset_body_action(event, extra: str, chat_id: int):
    if not _owner_ok(event):
        return None
    ok = appearance.set_header_body("")
    return _header_view(sym.label(sym.OP_SAVE, "Description reset.") if ok else f"{sym.STATE_FAILED} Could not reset the description.")


async def _header_reset_all_action(event, extra: str, chat_id: int):
    if not _owner_ok(event):
        return None
    ok = appearance.reset_header()
    return _header_view(sym.label(sym.OP_SAVE, "Header reset.") if ok else f"{sym.STATE_FAILED} Could not reset the header.")


# ── Per-page resets ──────────────────────────────────────────────────────────

async def _page_reset_title_action(event, extra: str, chat_id: int):
    if not _owner_ok(event):
        return None
    page_id = str(extra or "").strip()
    if appearance.page_spec(page_id) is None:
        return _pages_view(f"{sym.STATE_WARNING} Unknown page.")
    ok = appearance.reset_page_text(page_id, "title")
    return _page_view(page_id, sym.label(sym.OP_SAVE, "Title reset.") if ok else f"{sym.STATE_FAILED} Could not reset the title.")


async def _page_reset_body_action(event, extra: str, chat_id: int):
    if not _owner_ok(event):
        return None
    page_id = str(extra or "").strip()
    if appearance.page_spec(page_id) is None:
        return _pages_view(f"{sym.STATE_WARNING} Unknown page.")
    ok = appearance.reset_page_text(page_id, "body")
    return _page_view(page_id, sym.label(sym.OP_SAVE, "Description reset.") if ok else f"{sym.STATE_FAILED} Could not reset the description.")


async def _page_reset_all_action(event, extra: str, chat_id: int):
    if not _owner_ok(event):
        return None
    page_id = str(extra or "").strip()
    if appearance.page_spec(page_id) is None:
        return _pages_view(f"{sym.STATE_WARNING} Unknown page.")
    ok = appearance.reset_page_text(page_id)
    return _page_view(page_id, sym.label(sym.OP_SAVE, "Page reset.") if ok else f"{sym.STATE_FAILED} Could not reset the page.")


# ── Preview switch / removal (repaint carries the media change) ──────────────

async def _preview_toggle_action(event, extra: str, chat_id: int):
    if not _owner_ok(event):
        return None
    new_value = not appearance.preview_enabled()
    ok = appearance.set_preview_enabled(new_value)
    if ok:
        notice = sym.label(sym.OP_SAVE, f"Preview {'enabled' if new_value else 'disabled'}.")
        if new_value and not appearance.preview_image_key():
            notice += " Upload an image, or skip and keep it text-only."
    else:
        notice = f"{sym.STATE_FAILED} Could not change the preview switch."
    title, body, buttons = _preview_view(notice)
    await _paint_existing(
        event.client,
        inline_message_id_from_event(event),
        "appearance_preview",
        chat_id,
        getattr(event, "message_id", 0) or 0,
        title,
        body,
        buttons,
    )
    return None


async def _preview_remove_action(event, extra: str, chat_id: int):
    if not _owner_ok(event):
        return None
    old_key = appearance.preview_image_key()
    ok = appearance.clear_preview_image()
    if ok:
        if old_key:
            await asyncio.to_thread(preview_storage.remove, old_key)
        notice = sym.label(sym.OP_SAVE, "Image removed. The preview switch is unchanged.")
    else:
        notice = f"{sym.STATE_FAILED} Could not remove the image reference."
    title, body, buttons = _preview_view(notice)
    await _paint_existing(
        event.client,
        inline_message_id_from_event(event),
        "appearance_preview",
        chat_id,
        getattr(event, "message_id", 0) or 0,
        title,
        body,
        buttons,
    )
    return None


# ── Text inputs ──────────────────────────────────────────────────────────────

async def _header_title_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id, inline_id=None):
    value = (text or "").strip()
    if value == "-":
        value = ""
    ok = appearance.set_header_title(value)
    notice = (
        sym.label(sym.OP_SAVE, "Title saved.")
        if ok else f"{sym.STATE_FAILED} Title rejected (max {appearance.TITLE_MAX} characters)."
    )
    title, body, buttons = _header_view(notice)
    await _paint_existing(get_client(), inline_id, "appearance_header", inline_chat_id, inline_msg_id, title, body, buttons)


async def _header_body_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id, inline_id=None):
    value = (text or "").strip()
    if value == "-":
        value = ""
    ok = appearance.set_header_body(value)
    notice = (
        sym.label(sym.OP_SAVE, "Description saved.")
        if ok else f"{sym.STATE_FAILED} Description rejected (max {appearance.BODY_MAX} characters)."
    )
    title, body, buttons = _header_view(notice)
    await _paint_existing(get_client(), inline_id, "appearance_header", inline_chat_id, inline_msg_id, title, body, buttons)


async def _page_title_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id, extra="", inline_id=None):
    page_id = str(extra or "").strip()
    if appearance.page_spec(page_id) is None:
        title, body, buttons = _pages_view(f"{sym.STATE_WARNING} Unknown page.")
        await _paint_existing(get_client(), inline_id, "appearance_pages", inline_chat_id, inline_msg_id, title, body, buttons)
        return
    ok = appearance.set_page_text(page_id, "title", (text or "").strip())
    notice = (
        sym.label(sym.OP_SAVE, "Title saved.")
        if ok else f"{sym.STATE_FAILED} Title rejected (max {appearance.TITLE_MAX} characters)."
    )
    title, body, buttons = _page_view(page_id, notice)
    await _paint_existing(get_client(), inline_id, "appearance_page", inline_chat_id, inline_msg_id, title, body, buttons)


async def _page_body_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id, extra="", inline_id=None):
    page_id = str(extra or "").strip()
    if appearance.page_spec(page_id) is None:
        title, body, buttons = _pages_view(f"{sym.STATE_WARNING} Unknown page.")
        await _paint_existing(get_client(), inline_id, "appearance_pages", inline_chat_id, inline_msg_id, title, body, buttons)
        return
    ok = appearance.set_page_text(page_id, "body", (text or "").strip())
    notice = (
        sym.label(sym.OP_SAVE, "Description saved.")
        if ok else f"{sym.STATE_FAILED} Description rejected (max {appearance.BODY_MAX} characters)."
    )
    title, body, buttons = _page_view(page_id, notice)
    await _paint_existing(get_client(), inline_id, "appearance_page", inline_chat_id, inline_msg_id, title, body, buttons)


# ── Image upload ─────────────────────────────────────────────────────────────

async def _delete_upload(chat_id: int, msg_id: int) -> None:
    client = get_self_client()
    if client is None or not chat_id or not msg_id:
        return
    try:
        await client.delete_messages(chat_id, [msg_id])
    except Exception:
        pass


def _looks_like_image_upload(message) -> bool:
    """Bound the download before it happens (the bytes are validated after)."""
    from backend.services.image_probe import MAX_IMAGE_BYTES

    declared = getattr(getattr(message, "file", None), "size", None)
    if isinstance(declared, int) and declared > MAX_IMAGE_BYTES:
        return False
    if getattr(message, "photo", None) is not None:
        return True
    document = getattr(message, "document", None)
    mime = str(getattr(document, "mime_type", "") or "").lower()
    return mime in ("image/jpeg", "image/png", "image/webp")


async def _download_upload(message) -> bytes | None:
    try:
        buffer = io.BytesIO()
        await message.download_media(file=buffer)
        data = buffer.getvalue()
        return data or None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[APPEARANCE] upload download failed: %s", type(exc).__name__)
        return None


async def _preview_upload_input(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id,
    inline_id=None, message=None,
):
    notice = ""
    if message is None or getattr(message, "media", None) is None:
        notice = f"{sym.STATE_WARNING} Send a JPEG, PNG or WebP image."
    elif not _looks_like_image_upload(message):
        notice = f"{sym.STATE_FAILED} Send a photo or an image document (JPEG, PNG, WebP)."
    elif not preview_storage.available():
        notice = f"{sym.STATE_FAILED} Storage is not configured — nothing was saved."
    else:
        data = await _download_upload(message)
        probe = probe_image(data)
        if probe is None:
            notice = f"{sym.STATE_FAILED} Not a valid JPEG, PNG or WebP image (max 5 MB, up to 4096 px)."
        else:
            key = preview_storage.object_key(probe.ext)
            uploaded = await asyncio.to_thread(preview_storage.upload, key, data, probe.mime)
            if not uploaded:
                notice = f"{sym.STATE_FAILED} Upload failed — the previous image is unchanged."
            else:
                old_key = appearance.preview_image_key()
                persisted = appearance.set_preview_image(
                    key, {"mime": probe.mime, "width": probe.width, "height": probe.height},
                )
                if not persisted:
                    await asyncio.to_thread(preview_storage.remove, key)
                    notice = f"{sym.STATE_FAILED} Could not save the image reference."
                else:
                    if old_key and old_key != key:
                        await asyncio.to_thread(preview_storage.remove, old_key)
                    notice = sym.label(sym.OP_SAVE, "Preview image saved.")
    await _delete_upload(chat_id, msg_id)
    title, body, buttons = _preview_view(notice)
    await _paint_existing(
        get_client(), inline_id, "appearance_preview",
        inline_chat_id, inline_msg_id, title, body, buttons,
    )


def register(client, owner_id: int) -> None:
    register_panel("appearance", _appearance_panel_handler, parent="menu", title="Appearance")
    register_inline_builder("appearance", _appearance_inline_builder)
    register_panel("appearance_header", _appearance_header_panel_handler, parent="appearance", title="Header Customization")
    register_inline_builder("appearance_header", _appearance_header_inline_builder)
    register_panel("appearance_pages", _appearance_pages_panel_handler, parent="appearance", title="Page Text Customization")
    register_inline_builder("appearance_pages", _appearance_pages_inline_builder)
    register_panel("appearance_page", _appearance_page_panel_handler, parent="appearance_pages", title="Page Text")
    register_inline_builder("appearance_page", _appearance_page_inline_builder)
    register_panel("appearance_preview", _appearance_preview_panel_handler, parent="appearance", title="Preview Image")
    register_inline_builder("appearance_preview", _appearance_preview_inline_builder)

    register_action("appearance_header_reset_title", _header_reset_title_action)
    register_action("appearance_header_reset_body", _header_reset_body_action)
    register_action("appearance_header_reset_all", _header_reset_all_action)
    register_action("appearance_page_reset_title", _page_reset_title_action)
    register_action("appearance_page_reset_body", _page_reset_body_action)
    register_action("appearance_page_reset_all", _page_reset_all_action)
    register_action("appearance_preview_toggle", _preview_toggle_action)
    register_action("appearance_preview_remove", _preview_remove_action)

    register_input("appearance_header", "title", {
        "handler": _header_title_input,
        "prompt": (
            f"**Global header title**\n\nEnter up to {appearance.TITLE_MAX} characters, "
            "or send a single `-` to clear it.\n\n_Reply below._"
        ),
    })
    register_input("appearance_header", "body", {
        "handler": _header_body_input,
        "prompt": (
            f"**Global header description**\n\nEnter up to {appearance.BODY_MAX} characters, "
            "or send a single `-` to clear it.\n\n_Reply below._"
        ),
    })
    register_input("appearance_page", "title", {
        "handler": _page_title_input,
        "prompt": f"**Page title**\n\nEnter up to {appearance.TITLE_MAX} characters.\n\n_Reply below._",
        "back": "panel:appearance_pages",
    })
    register_input("appearance_page", "body", {
        "handler": _page_body_input,
        "prompt": f"**Page description**\n\nEnter up to {appearance.BODY_MAX} characters.\n\n_Reply below._",
        "back": "panel:appearance_pages",
    })
    register_input("appearance_preview", "upload", {
        "handler": _preview_upload_input,
        "prompt": (
            "**Preview image**\n\nSend ONE JPEG, PNG or WebP image (max 5 MB, "
            "up to 4096 px per side).\n\n_Reply with the image below._"
        ),
        "timeout": 120.0,
        "back": "panel:appearance_preview",
    })
