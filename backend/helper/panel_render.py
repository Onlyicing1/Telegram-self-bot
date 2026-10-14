"""
PanelRenderer — reusable inline panel renderer.

Accepts BOTH button formats without crashing:
  - tuple: ("Text", "callback_data")
  - KeyboardButtonCallback / Button.inline objects

The renderer normalizes internally so no handler needs rewriting.
"""
import logging

from telethon.tl import types
from telethon.tl.custom import Button

from backend.helper.context import truncate_callback_data
from backend.helper.styles import PRIMARY

logger = logging.getLogger(__name__)

#: Named inline-button background styles defined by the MTProto
#: ``KeyboardButtonStyle`` constructor. Only these three Telegram-defined
#: colors exist — arbitrary RGB/HEX is not part of the schema.
SUPPORTED_BUTTON_STYLES = ("primary", "success", "danger")


def _style(text) -> str:
    """Apply the persisted Glass UI font to display text.

    Callback data, code spans, URLs, digits and identifiers are never
    transformed. Any failure falls back to the untouched text so a bad
    font state can never break rendering.
    """
    if not text:
        return text
    try:
        from backend.services import settings_service
        from backend.helper.font_style import apply_font
        return apply_font(str(text), settings_service.dashboard_font())
    except Exception:
        return str(text)


def _named_style(style) -> str | None:
    """Normalize a style value to a supported name, else ``None`` (fail safe)."""
    if isinstance(style, str):
        candidate = style.strip().lower()
        if candidate in SUPPORTED_BUTTON_STYLES:
            return candidate
    return None


def _style_name_from_tl(style_obj) -> str | None:
    """Recover the named style from a Telethon ``KeyboardButtonStyle``."""
    if style_obj is None:
        return None
    for name in SUPPORTED_BUTTON_STYLES:
        if getattr(style_obj, f"bg_{name}", None):
            return name
    return None


def _callback(text, data, style=None) -> types.KeyboardButtonCallback:
    """Build a callback button, applying a supported named style if present.

    An unsupported or malformed style degrades to an unstyled button rather
    than raising, so one bad button can never break a whole panel.
    """
    name = _named_style(style)
    if name is not None:
        try:
            return Button.inline(text, data, style=name)
        except (ValueError, TypeError):
            logger.warning("Button style %r rejected by Telethon; rendering unstyled", style)
    elif style is not None:
        logger.warning("Unsupported button style %r; rendering unstyled", style)
    return Button.inline(text, data)


def _url(text, url, style=None) -> types.KeyboardButtonUrl:
    """Build a URL button, applying a supported named style if present.

    Mirrors :func:`_callback`: an unsupported style degrades to an unstyled
    URL button (the destination is never changed).
    """
    name = _named_style(style)
    if name is not None:
        try:
            return Button.url(text, url, style=name)
        except (ValueError, TypeError):
            logger.warning("URL button style %r rejected by Telethon; rendering unstyled", style)
    elif style is not None:
        logger.warning("Unsupported URL button style %r; rendering unstyled", style)
    return Button.url(text, url)


def _normalize_button(btn) -> types.KeyboardButtonCallback:
    # Every button that reaches a rendered/edited panel passes through here, so
    # this is the single place that guarantees a default style: any button with
    # no explicit supported style becomes PRIMARY (blue). Explicit green/red
    # overrides are preserved. This keeps tuple buttons, raw TL callback
    # buttons, URL buttons and auto-injected nav buttons from silently
    # rendering unstyled.
    if isinstance(btn, tuple):
        text, data = _style(btn[0]), btn[1]
        return _callback(text, truncate_callback_data(str(data)), PRIMARY)
    if isinstance(btn, types.KeyboardButtonCallback):
        styled = _style(btn.text)
        existing = _style_name_from_tl(getattr(btn, "style", None))
        if styled != btn.text or existing is None:
            return _callback(styled, btn.data, existing or PRIMARY)
        return btn
    if isinstance(btn, type(Button.inline("x", "y"))):
        return btn
    if hasattr(btn, "text") and hasattr(btn, "data"):
        return btn
    if hasattr(btn, "text") and hasattr(btn, "url"):
        existing = _style_name_from_tl(getattr(btn, "style", None))
        return _url(_style(btn.text), btn.url, existing or PRIMARY)
    text = getattr(btn, "text", None)
    if not text:
        text = "Button"
    return _callback(_style(str(text)), "panel:_nav:close", PRIMARY)


def _normalize_row(row) -> list:
    if isinstance(row, types.KeyboardButtonRow):
        return [_normalize_button(b) for b in row.buttons]
    if isinstance(row, list):
        return [_normalize_button(b) for b in row]
    return [_normalize_button(row)]


def to_edit_buttons(buttons: list) -> list:
    if not buttons:
        return []
    result = []
    for row in buttons:
        result.append(_normalize_row(row))
    return result


def _to_inline_rows(buttons: list) -> list:
    if not buttons:
        return []
    rows = []
    for row in buttons:
        rows.append(types.KeyboardButtonRow(buttons=_normalize_row(row)))
    return rows


def render(
    title: str,
    body: str = "",
    buttons: list | None = None,
) -> types.InputBotInlineResult:
    """Build a single inline result. Accepts tuples OR Button objects.

    Initial render always adds only Close (root menu).  Subsequent callback
    edits add Back+Home+Close via _finalize_panel when the view is a submenu.
    """
    if buttons is None:
        buttons = []

    from backend.helper.panels import _has_nav_buttons, InlinePanelBuilder, _add_close_button
    if not _has_nav_buttons(buttons):
        builder = InlinePanelBuilder()
        for row in buttons:
            if isinstance(row, list):
                builder._rows.append(list(row))
            else:
                builder._rows.append([row])
        _add_close_button(builder)
        buttons = builder.build()

    if title and body:
        message = f"**{_style(title)}**\n\n{_style(body)}"
    elif title:
        message = f"**{_style(title)}**"
    else:
        message = _style(body) or ""

    markup_rows = _to_inline_rows(buttons) if buttons else None

    msg = types.InputBotInlineMessageText(
        message=message,
        reply_markup=types.ReplyInlineMarkup(rows=markup_rows) if markup_rows else None,
    )

    return types.InputBotInlineResult(
        id="0",
        type="article",
        title=_style(title)[:255] if title else "LifeOS",
        send_message=msg,
    )


def render_edit(
    title: str,
    body: str = "",
    buttons: list | None = None,
) -> tuple[str, list]:
    title, body = _style(title), _style(body)
    if title and body:
        text = f"**{title}**\n\n{body}"
    elif title:
        text = f"**{title}**"
    else:
        text = body or ""

    built = to_edit_buttons(buttons) if buttons else []
    return text, built
