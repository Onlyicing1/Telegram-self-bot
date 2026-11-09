"""Appearance — Glass UI personalization panels.

Three sections, one navigation level apart from the main menu:

* **Header Customization** — the shared static header title/description.
* **Page Text Customization** — title/body overrides for the main pages that
  are reachable as top-level categories from the main menu.
* **Preview Image** — an independent enable/disable switch plus
  upload/replace/remove for the stored image.

Everything is persisted through the existing singleton settings service
(`settings_service` → `panel_settings_repository` → `panel_settings`) and
applied by `appearance_service` at the panel render boundary, so no page
handler knows about customization and no second settings framework exists.

Owner input reuses the EXISTING pending-input listener. The inline panel is
edited only by the Helper Bot through its own inline-message identifier; this
module never creates a client, a listener, an update loop, a second panel
message, or a self-client editing fallback.
"""
from __future__ import annotations

import logging
import shutil
import tempfile
from pathlib import Path

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
        f" {sym.SEPARATOR} image {'stored' if preview['stored'] else 'missing'}",
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


# ── Preview image ────────────────────────────────────────────────────────────

def _preview_view(notice: str = "") -> tuple[str, str, list]:
    state = appearance_service.preview_status()
    lines = [
        f"**Preview:** {_on_off(state['enabled'])}",
        f"**Stored image:** {'yes' if state['stored'] else 'no'}",
        f"**Mode:** {'image preview' if state['active'] else 'text only'}",
    ]
    warnings: list[str] = []
    if state["enabled"] and not state["stored"]:
        warnings.append(
            f"{sym.STATE_WARNING} No image is stored. Upload one, or leave the "
            "panel text-only."
        )
    if state["stored"] and not state["public_base"]:
        warnings.append(
            f"{sym.STATE_WARNING} PUBLIC_BASE_URL is not set, so Telegram cannot "
            "load a preview."
        )
    if not state["storage"]:
        warnings.append(f"{sym.STATE_WARNING} {appearance_service.storage_help()}")
    if warnings:
        lines.extend(["", *warnings])
    if notice:
        lines.extend(["", notice])
    builder = InlinePanelBuilder()
    builder.add_row(
        sym.label(sym.OP_SAVE if not state["enabled"] else sym.OP_STOP,
                  "Disable preview" if state["enabled"] else "Enable preview"),
        "action:appearance_toggle_preview",
        PRIMARY,
    )
    builder.add_row(
        sym.label(sym.OP_CREATE if not state["stored"] else sym.OP_EDIT,
                  "Upload image" if not state["stored"] else "Replace image"),
        f"input:{_PANEL}:image",
        PRIMARY,
    )
    builder.add_row(sym.label(sym.OP_DELETE, "Remove image"), "action:appearance_remove_image", PRIMARY)
    _nav(builder, f"panel:{_PANEL}")
    return "Preview Image", "\n".join(lines), builder.build()


async def _preview_panel(event, extra: str) -> tuple[str, str, list] | None:
    return _preview_view()


# ── Actions ──────────────────────────────────────────────────────────────────

async def _toggle_preview(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    enabled = settings_service.appearance_preview_enabled()
    if not settings_service.set_appearance_preview_enabled(not enabled):
        return _preview_view(notice=f"{sym.STATE_FAILED} Storage refused the change.")
    if settings_service.appearance_preview_enabled():
        return _preview_view(
            notice=f"{sym.OP_SAVE} Preview enabled. The stored image is reused when present."
        )
    return _preview_view(
        notice=f"{sym.OP_STOP} Preview disabled; the stored image is kept."
    )


async def _remove_image(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    _ok, notice = await appearance_service.clear_image()
    return _preview_view(notice=notice)


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


async def _image_input(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id,
    extra: str = "", inline_id=None, event=None,
):
    """Take the owner's uploaded image from the EXISTING input listener.

    Downloads to a bounded temporary directory (never the durable store),
    validates real content and cleans the directory up on every path.
    """
    import asyncio

    media = getattr(event, "media", None) if event is not None else None
    if media is None:
        await _paint_input(
            inline_id,
            _preview_view(
                notice=f"{sym.STATE_FAILED} Send a PNG, JPEG or WebP image as a "
                "photo or file."
            ),
        )
        return

    file_info = getattr(event, "file", None)
    reported_size = getattr(file_info, "size", None)
    if isinstance(reported_size, int) and reported_size > appearance_service.MAX_IMAGE_BYTES:
        await _paint_input(
            inline_id,
            _preview_view(
                notice=f"{sym.STATE_FAILED} That image is larger than "
                f"{appearance_service.MAX_IMAGE_MB} MB."
            ),
        )
        return

    notice = f"{sym.STATE_FAILED} The image could not be read."
    tmp_dir = tempfile.mkdtemp(prefix="lifeos_appearance_")
    try:
        path = await event.download_media(file=tmp_dir)
        if path:
            data = Path(path).read_bytes()
            _ok, notice = await appearance_service.install_image(data)
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — a bad upload is reported, never fatal
        logger.warning("[APPEARANCE] image input failed: %s", exc)
        notice = f"{sym.STATE_FAILED} That upload could not be processed."
    finally:
        shutil.rmtree(tmp_dir, ignore_errors=True)

    await _paint_input(inline_id, _preview_view(notice=notice))


# ── Registration (no listener, no client, no loop) ───────────────────────────

def register(client, owner_id: int) -> None:
    register_panel(_PANEL, _home, parent="menu", title="Appearance")
    register_panel(_HEADER_PANEL, _header_panel, parent=_PANEL, title="Header Customization")
    register_panel(_PAGES_PANEL, _pages_panel, parent=_PANEL, title="Page Text")
    register_panel(_PAGE_PANEL, _page_panel, parent=_PAGES_PANEL, title="Page Text")
    register_panel(_PREVIEW_PANEL, _preview_panel, parent=_PANEL, title="Preview Image")

    register_action("appearance_toggle_preview", _toggle_preview)
    register_action("appearance_remove_image", _remove_image)
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
            f"{appearance_service.MAX_IMAGE_SIDE}px per side).\n\n_Send it below._"
        ),
        "back": f"panel:{_PREVIEW_PANEL}",
    })
    logger.info("[APPEARANCE] panels registered for owner=%s", owner_id)
