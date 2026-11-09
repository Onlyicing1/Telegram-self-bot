"""Appearance — Glass UI personalization panels.

Three sections, one navigation level apart from the main menu:

* **Header Customization** — the shared static header title/description.
* **Page Text Customization** — title/body overrides for the main pages that
  are reachable as top-level categories from the main menu.
* **Preview Image** — an independent enable/disable switch plus a bounded
  gallery (at most ``MAX_GALLERY_IMAGES`` original images) whose objects are
  listed from Storage itself, with per-image selection and deletion.

Everything is persisted through the existing singleton settings service
(`settings_service` → `panel_settings_repository` → `panel_settings`) and
applied by `appearance_service` at the panel render boundary, so no page
handler knows about customization and no second settings framework exists.

Owner input reuses the EXISTING pending-input listener. The inline panel is
edited only by the Helper Bot through its own inline-message identifier; this
module never creates a client, a listener, an update loop, a second panel
message, or a self-client editing fallback. The uploaded media is streamed
through Telethon's own download iterator into a strictly bounded in-memory
buffer, so neither a reported file size nor an unbounded temporary file can
exceed the 25 MiB ceiling.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.bot.handlers.guard import is_owner
from backend.helper import (
    InlinePanelBuilder,
    register_action,
    register_input,
    register_panel,
)
from backend.helper import symbols as sym
from backend.helper.styles import PRIMARY
from backend.services import appearance_service, settings_service

logger = logging.getLogger(__name__)

_PANEL = "appearance"
_HEADER_PANEL = "appearance_header"
_PAGES_PANEL = "appearance_pages"
_PAGE_PANEL = "appearance_page"
_PREVIEW_PANEL = "appearance_preview"

#: Bytes requested per download round trip. The cap is enforced on the bytes
#: actually received, so this only bounds the overshoot of one chunk.
_DOWNLOAD_CHUNK = 256 * 1024


# ── Small shared pieces ──────────────────────────────────────────────────────

def _on_off(value: bool) -> str:
    return sym.label(sym.OP_SAVE, "ON") if value else sym.label(sym.OP_STOP, "OFF")


def _custom(value: bool) -> str:
    return sym.STATE_COMPLETED if value else sym.SEPARATOR


def _nav(builder: InlinePanelBuilder, back: str) -> None:
    builder.add_buttons(
        (sym.label(sym.OP_BACK, "Back"), back),
        (sym.label(sym.OP_HOME, "Home"), "panel:_nav:home"),
    )
    builder.add_row(sym.label(sym.OP_CLOSE, "Close"), "panel:_nav:close")


def _raw_text(title: str, body: str) -> str:
    if title and body:
        return f"**{title}**\n\n{body}"
    if title:
        return f"**{title}**"
    return body or ""


# ── Home ─────────────────────────────────────────────────────────────────────

async def _home(event, extra: str) -> tuple[str, str, list] | None:
    header_set = bool(
        settings_service.appearance_header_title()
        or settings_service.appearance_header_body()
    )
    preview = appearance_service.preview_status()
    customized = appearance_service.customized_page_count()
    body = "\n".join([
        f"{sym.OP_EDIT} **Header:** {_on_off(header_set)}",
        f"{sym.PANEL_HEADING} **Pages customized:** {customized} / "
        f"{len(appearance_service.MAIN_PAGE_IDS)}",
        f"{sym.MEDIA_PHOTO} **Preview:** {_on_off(preview['enabled'])}"
        f" {sym.SEPARATOR} {'image selected' if preview['stored'] else 'no image'}",
        "",
        "_Customization applies to the main pages only. Operational results, "
        "confirmations and pickers keep their own text._",
    ])
    builder = InlinePanelBuilder()
    builder.add_row(sym.label(sym.OP_EDIT, "Header Customization"), f"panel:{_HEADER_PANEL}", PRIMARY)
    builder.add_row(sym.label(sym.PANEL_HEADING, "Page Text"), f"panel:{_PAGES_PANEL}", PRIMARY)
    builder.add_row(sym.label(sym.MEDIA_PHOTO, "Preview Image"), f"panel:{_PREVIEW_PANEL}", PRIMARY)
    _nav(builder, "panel:_nav:home")
    return "Appearance", body, builder.build()


# ── Header ───────────────────────────────────────────────────────────────────

def _header_view(notice: str = "") -> tuple[str, str, list]:
    title = settings_service.appearance_header_title()
    body = settings_service.appearance_header_body()
    lines = [
        f"**Title:** {title or '_default_'}",
        f"**Description:** {body or '_default_'}",
        "",
        f"_Limits: title {appearance_service.TITLE_LIMIT} characters, "
        f"description {appearance_service.BODY_LIMIT}._",
    ]
    if notice:
        lines.extend(["", notice])
    builder = InlinePanelBuilder()
    builder.add_row(sym.label(sym.OP_EDIT, "Edit title"), f"input:{_PANEL}:header_title", PRIMARY)
    builder.add_row(sym.label(sym.OP_EDIT, "Edit description"), f"input:{_PANEL}:header_body", PRIMARY)
    builder.add_row(sym.label(sym.OP_REFRESH, "Reset title"), "action:appearance_reset_header:title", PRIMARY)
    builder.add_row(sym.label(sym.OP_REFRESH, "Reset description"), "action:appearance_reset_header:body", PRIMARY)
    builder.add_row(sym.label(sym.OP_REFRESH, "Reset both"), "action:appearance_reset_header:both", PRIMARY)
    _nav(builder, f"panel:{_PANEL}")
    return "Header Customization", "\n".join(lines), builder.build()


async def _header_panel(event, extra: str) -> tuple[str, str, list] | None:
    return _header_view()


# ── Page text ────────────────────────────────────────────────────────────────

def _pages_view(notice: str = "") -> tuple[str, str, list]:
    builder = InlinePanelBuilder()
    for page_id in appearance_service.MAIN_PAGE_IDS:
        state = appearance_service.page_state(page_id)
        mark = _custom(state["custom_title"] or state["custom_body"])
        builder.add_row(
            f"{mark} {state['default_title']}",
            f"panel:{_PAGE_PANEL}:{page_id}",
            PRIMARY,
        )
    body = (
        f"{sym.PANEL_HEADING} Main pages are customizable independently.\n"
        "Only their home view changes; results and confirmations do not."
    )
    if notice:
        body = f"{body}\n\n{notice}"
    _nav(builder, f"panel:{_PANEL}")
    return "Page Text", body, builder.build()


async def _pages_panel(event, extra: str) -> tuple[str, str, list] | None:
    return _pages_view()


def _page_view(page_id: str, notice: str = "") -> tuple[str, str, list] | None:
    if not appearance_service.is_eligible(page_id):
        return None
    state = appearance_service.page_state(page_id)
    body_text = state["body"] or "_This page renders live content; a custom body is shown above it._"
    lines = [
        f"**Page:** {state['default_title']} (`{page_id}`)",
        f"**Title:** {state['title']}",
        f"**Body:** {body_text}",
        "",
        f"{_custom(state['custom_title'])} title customized",
        f"{_custom(state['custom_body'])} body customized",
    ]
    if notice:
        lines.extend(["", notice])
    builder = InlinePanelBuilder()
    builder.add_row(
        sym.label(sym.OP_EDIT, "Edit title"),
        f"input:{_PANEL}:page_title:{page_id}",
        PRIMARY,
    )
    builder.add_row(
        sym.label(sym.OP_EDIT, "Edit body"),
        f"input:{_PANEL}:page_body:{page_id}",
        PRIMARY,
    )
    builder.add_row(
        sym.label(sym.OP_REFRESH, "Reset title"),
        f"action:appearance_reset_page:{page_id}:title",
        PRIMARY,
    )
    builder.add_row(
        sym.label(sym.OP_REFRESH, "Reset body"),
        f"action:appearance_reset_page:{page_id}:body",
        PRIMARY,
    )
    builder.add_row(
        sym.label(sym.OP_REFRESH, "Reset page"),
        f"action:appearance_reset_page:{page_id}:all",
        PRIMARY,
    )
    _nav(builder, f"panel:{_PAGES_PANEL}")
    return state["default_title"], "\n".join(lines), builder.build()


async def _page_panel(event, extra: str) -> tuple[str, str, list] | None:
    result = _page_view((extra or "").strip())
    if result is not None:
        return result
    return _pages_view(notice=f"{sym.STATE_FAILED} Unknown page.")


# ── Preview image gallery ────────────────────────────────────────────────────

def _preview_view(
    gallery: dict[str, Any] | None = None,
    notice: str = "",
) -> tuple[str, str, list]:
    """Render the gallery state. ``gallery=None`` means "not listed yet"."""
    state = appearance_service.preview_status()
    items = list((gallery or {}).get("items") or [])
    known = bool(gallery and gallery.get("known"))
    limit = appearance_service.MAX_GALLERY_IMAGES
    count = len(items) if known else (1 if state["stored"] else 0)

    lines = [
        f"**Preview:** {_on_off(state['enabled'])}",
        f"**Mode:** {'image preview' if state['active'] else 'text only'}",
        f"{sym.PANEL_HEADING} **Images:** {count} / {limit}"
        + ("" if known else f" {sym.SEPARATOR} not listed yet"),
    ]
    active_index = next(
        (index for index, item in enumerate(items, 1) if item.get("active")), 0
    )
    if active_index:
        lines.append(f"{sym.STATE_COMPLETED} **Selected:** image {active_index}")
    elif state["stored"]:
        lines.append(
            f"{sym.STATE_WARNING} **Selected:** the saved key is not in the gallery"
        )
    else:
        lines.append(f"{sym.SEPARATOR} **Selected:** none")

    warnings: list[str] = []
    if not state["storage"]:
        warnings.append(f"{sym.STATE_WARNING} {appearance_service.storage_help()}")
    if gallery is not None and gallery.get("error"):
        warnings.append(str(gallery["error"]))
    if gallery is not None and gallery.get("missing_selected"):
        warnings.append(
            f"{sym.STATE_WARNING} The selected image is missing from Storage. "
            "Select another image or delete that entry."
        )
    if state["enabled"] and not state["stored"]:
        warnings.append(
            f"{sym.STATE_WARNING} Preview is ON without a selected image, so the "
            "panels stay text-only."
        )
    if state["stored"] and not state["public_base"]:
        warnings.append(
            f"{sym.STATE_WARNING} PUBLIC_BASE_URL is not set, so Telegram cannot "
            "load a preview."
        )
    if warnings:
        lines.extend(["", *warnings])

    if known and items:
        lines.extend([
            "",
            f"`{sym.STATE_COMPLETED}` selects an image {sym.SEPARATOR} "
            f"`{sym.OP_DELETE}` deletes that image only.",
            "_Uploading keeps every image already stored._",
        ])
    elif known:
        lines.extend(["", "_No images stored yet. Upload one to start the gallery._"])
    if notice:
        lines.extend(["", notice])

    builder = InlinePanelBuilder()
    for index, item in enumerate(items, 1):
        mark = sym.STATE_COMPLETED if item.get("active") else sym.SELECT_EMPTY
        builder.add_buttons(
            (f"{mark} {index}", f"action:appearance_select:{item['token']}", PRIMARY),
            (sym.label(sym.OP_DELETE, str(index)),
             f"action:appearance_delete:{item['token']}", PRIMARY),
        )
    builder.add_row(
        sym.label(
            sym.OP_STOP if state["enabled"] else sym.OP_SAVE,
            "Disable preview" if state["enabled"] else "Enable preview",
        ),
        "action:appearance_toggle_preview",
        PRIMARY,
    )
    if state["stored"] and not active_index:
        builder.add_row(
            sym.label(sym.OP_DELETE, "Clear the selection"),
            "action:appearance_clear_selection",
            PRIMARY,
        )
    if not known or count < limit:
        builder.add_row(
            sym.label(
                sym.OP_CREATE if not count else sym.OP_SAVE,
                "Upload image" if not count else "Upload another image",
            ),
            f"input:{_PANEL}:image",
            PRIMARY,
        )
    else:
        lines.append(
            f"{sym.STATE_WARNING} The gallery is full ({limit}). Delete an image "
            "to upload another; nothing was removed automatically."
        )
    _nav(builder, f"panel:{_PANEL}")
    return "Preview Image", "\n".join(lines), builder.build()


async def _preview_after(notice: str = "") -> tuple[str, str, list]:
    return _preview_view(await appearance_service.gallery(), notice=notice)


async def _preview_panel(event, extra: str) -> tuple[str, str, list] | None:
    return await _preview_after()


# ── Actions ──────────────────────────────────────────────────────────────────

async def _toggle_preview(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    enabled = settings_service.appearance_preview_enabled()
    if not settings_service.set_appearance_preview_enabled(not enabled):
        return _preview_view(
            notice=f"{sym.STATE_FAILED} Storage refused the change."
        )
    if settings_service.appearance_preview_enabled():
        return await _preview_after(
            notice=f"{sym.OP_SAVE} Preview enabled. The selected image is reused "
            "when it is still stored."
        )
    return await _preview_after(
        notice=f"{sym.OP_STOP} Preview disabled; every stored image and the "
        "selection are kept."
    )


async def _select_image(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    _ok, notice = await appearance_service.select_image((extra or "").strip())
    return await _preview_after(notice=notice)


async def _delete_image(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    _ok, notice = await appearance_service.delete_image((extra or "").strip())
    return await _preview_after(notice=notice)


async def _clear_selection(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    _ok, notice = await appearance_service.clear_selection()
    return await _preview_after(notice=notice)


async def _reset_header(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    _ok, notice = appearance_service.reset_header((extra or "").strip())
    return _header_view(notice=notice)


async def _reset_page(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    parts = (extra or "").split(":", 1)
    page_id = parts[0].strip()
    field = parts[1].strip() if len(parts) > 1 else "all"
    _ok, notice = appearance_service.reset_page_text(page_id, field)
    view = _page_view(page_id, notice=notice)
    if view is None:
        return _pages_view(notice=f"{sym.STATE_FAILED} Unknown page.")
    return view


# ── Owner input (existing listener; Helper Bot is the only editor) ───────────

async def _paint_input(inline_id, view: tuple[str, str, list]) -> bool:
    """Render an input result onto the SAME panel, through the Helper Bot only."""
    from backend.helper.client import get_client
    from backend.helper.panel_edit import ExistingPanelEditor

    title, body, buttons = view
    editor = ExistingPanelEditor(inline_id=inline_id, helper=get_client())
    if not editor.available:
        logger.warning(
            "[APPEARANCE] panel edit unavailable — the setting was applied but the "
            "panel could not be repainted (no Helper Bot inline identifier)"
        )
        return False
    return await editor.edit(_raw_text(title, body), buttons=buttons)


async def _header_title_input(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id,
    extra: str = "", inline_id=None, event=None,
):
    _ok, notice = appearance_service.set_header("title", text)
    await _paint_input(inline_id, _header_view(notice=notice))


async def _header_body_input(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id,
    extra: str = "", inline_id=None, event=None,
):
    _ok, notice = appearance_service.set_header("body", text)
    await _paint_input(inline_id, _header_view(notice=notice))


async def _page_title_input(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id,
    extra: str = "", inline_id=None, event=None,
):
    page_id = (extra or "").strip()
    _ok, notice = appearance_service.set_page_text(page_id, "title", text)
    view = _page_view(page_id, notice=notice)
    if view is None:
        view = _pages_view(notice=f"{sym.STATE_FAILED} Unknown page.")
    await _paint_input(inline_id, view)


async def _page_body_input(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id,
    extra: str = "", inline_id=None, event=None,
):
    page_id = (extra or "").strip()
    _ok, notice = appearance_service.set_page_text(page_id, "body", text)
    view = _page_view(page_id, notice=notice)
    if view is None:
        view = _pages_view(notice=f"{sym.STATE_FAILED} Unknown page.")
    await _paint_input(inline_id, view)


def _message_media(event) -> Any:
    message = getattr(event, "message", None)
    return getattr(message, "media", None) or getattr(event, "media", None)


def _declared_mime(event) -> str:
    """The MIME Telegram DECLARED, used only to corroborate the real content."""
    message = getattr(event, "message", None)
    if getattr(message, "photo", None) is not None:
        return "image/jpeg"
    document = getattr(message, "document", None)
    value = getattr(document, "mime_type", "") if document is not None else ""
    return str(value or "").strip().lower()


async def _download_bounded(event) -> tuple[bytes | None, str]:
    """Stream the owner's media with a hard 25 MiB cap on RECEIVED bytes.

    A reported Telegram size is never trusted: the transfer is stopped the
    moment the bytes actually received would exceed the ceiling, and nothing
    larger than the ceiling is ever buffered. No temporary file is created.
    """
    media = _message_media(event)
    if media is None:
        return None, (
            f"{sym.STATE_FAILED} Send a PNG, JPEG or WebP image as a photo or file."
        )
    client = getattr(event, "client", None)
    if client is None or not hasattr(client, "iter_download"):
        return None, f"{sym.STATE_FAILED} The image could not be read."

    limit = appearance_service.MAX_IMAGE_BYTES
    buffer = bytearray()
    stream = client.iter_download(media, chunk_size=_DOWNLOAD_CHUNK)
    try:
        async for chunk in stream:
            if not chunk:
                continue
            received = len(buffer) + len(chunk)
            if received > limit:
                return None, (
                    f"{sym.STATE_FAILED} That file is larger than "
                    f"{appearance_service.MAX_IMAGE_MB} MB. The upload was stopped "
                    "at the limit and nothing was stored."
                )
            buffer.extend(chunk)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a failed transfer is reported
        logger.warning("[APPEARANCE] bounded media download failed: %s", exc)
        return None, f"{sym.STATE_FAILED} The image could not be downloaded."
    finally:
        close = getattr(stream, "aclose", None)
        if close is not None:
            try:
                await close()
            except Exception:  # noqa: BLE001 — closing a finished stream is harmless
                pass
    if not buffer:
        return None, f"{sym.STATE_FAILED} That file is empty."
    return bytes(buffer), ""


async def _image_input(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id,
    extra: str = "", inline_id=None, event=None,
):
    """Take the owner's image from the EXISTING input listener.

    The transfer is bounded in memory, then validated and uploaded by the
    service without any transformation.
    """
    data, notice = await _download_bounded(event)
    if data is None:
        await _paint_input(inline_id, _preview_view(notice=notice))
        return
    try:
        _ok, notice = await appearance_service.install_image(
            data, _declared_mime(event)
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a bad upload is reported, never fatal
        logger.warning("[APPEARANCE] image input failed: %s", exc)
        notice = f"{sym.STATE_FAILED} That upload could not be processed."
    await _paint_input(inline_id, await _preview_after(notice=notice))


# ── Registration (no listener, no client, no loop) ───────────────────────────

def register(client, owner_id: int) -> None:
    register_panel(_PANEL, _home, parent="menu", title="Appearance")
    register_panel(_HEADER_PANEL, _header_panel, parent=_PANEL, title="Header Customization")
    register_panel(_PAGES_PANEL, _pages_panel, parent=_PANEL, title="Page Text")
    register_panel(_PAGE_PANEL, _page_panel, parent=_PAGES_PANEL, title="Page Text")
    register_panel(_PREVIEW_PANEL, _preview_panel, parent=_PANEL, title="Preview Image")

    register_action("appearance_toggle_preview", _toggle_preview)
    register_action("appearance_select", _select_image)
    register_action("appearance_delete", _delete_image)
    register_action("appearance_clear_selection", _clear_selection)
    register_action("appearance_reset_header", _reset_header)
    register_action("appearance_reset_page", _reset_page)

    register_input(_PANEL, "header_title", {
        "handler": _header_title_input,
        "prompt": (
            f"{sym.OP_EDIT} **Header title**\n\nSend the new title "
            f"(max {appearance_service.TITLE_LIMIT} characters):\n\n_Send it below._"
        ),
        "back": f"panel:{_HEADER_PANEL}",
    })
    register_input(_PANEL, "header_body", {
        "handler": _header_body_input,
        "prompt": (
            f"{sym.OP_EDIT} **Header description**\n\nSend the new description "
            f"(max {appearance_service.BODY_LIMIT} characters):\n\n_Send it below._"
        ),
        "back": f"panel:{_HEADER_PANEL}",
    })
    register_input(_PANEL, "page_title", {
        "handler": _page_title_input,
        "prompt": (
            f"{sym.OP_EDIT} **Page title**\n\nSend the new title "
            f"(max {appearance_service.TITLE_LIMIT} characters):\n\n_Send it below._"
        ),
        "back": f"panel:{_PAGES_PANEL}",
    })
    register_input(_PANEL, "page_body", {
        "handler": _page_body_input,
        "prompt": (
            f"{sym.OP_EDIT} **Page body**\n\nSend the new body "
            f"(max {appearance_service.BODY_LIMIT} characters):\n\n_Send it below._"
        ),
        "back": f"panel:{_PAGES_PANEL}",
    })
    register_input(_PANEL, "image", {
        "handler": _image_input,
        "prompt": (
            f"{sym.MEDIA_PHOTO} **Preview image**\n\nSend a PNG, JPEG or WebP image "
            f"(max {appearance_service.MAX_IMAGE_MB} MB, "
            f"{appearance_service.MAX_IMAGE_SIDE}px per side, up to "
            f"{appearance_service.MAX_IMAGE_PIXELS // 1_000_000} megapixels).\n\n"
            "_The original bytes are stored exactly as sent; up to "
            f"{appearance_service.MAX_GALLERY_IMAGES} images are kept._\n\n"
            "_Send it below._"
        ),
        "back": f"panel:{_PREVIEW_PANEL}",
    })
    logger.info("[APPEARANCE] panels registered for owner=%s", owner_id)
