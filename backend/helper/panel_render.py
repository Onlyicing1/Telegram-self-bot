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

logger = logging.getLogger(__name__)


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


def _normalize_button(btn) -> types.KeyboardButtonCallback:
    if isinstance(btn, tuple):
        text, data = _style(btn[0]), btn[1]
        return Button.inline(text, truncate_callback_data(str(data)))
    if isinstance(btn, types.KeyboardButtonCallback):
        styled = _style(btn.text)
        if styled != btn.text:
            return Button.inline(styled, btn.data)
        return btn
    if isinstance(btn, type(Button.inline("x", "y"))):
        return btn
    if hasattr(btn, "text") and hasattr(btn, "data"):
        return btn
    if hasattr(btn, "text") and hasattr(btn, "url"):
        return Button.url(_style(btn.text), btn.url)
    text = getattr(btn, "text", None)
    if not text:
        text = "Button"
    return Button.inline(_style(str(text)), "panel:_nav:close")


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
