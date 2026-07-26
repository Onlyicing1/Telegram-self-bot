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


def _normalize_button(btn) -> types.KeyboardButtonCallback:
    """Convert a single button (tuple OR TLObject) into KeyboardButtonCallback."""
    if isinstance(btn, tuple):
        text, data = btn[0], btn[1]
        return Button.inline(text, truncate_callback_data(str(data)))
    if isinstance(btn, types.KeyboardButtonCallback):
        return btn
    if isinstance(btn, type(Button.inline("x", "y"))):
        return btn
    if hasattr(btn, "text") and hasattr(btn, "data"):
        return btn
    if hasattr(btn, "text") and hasattr(btn, "url"):
        return Button.url(btn.text, btn.url)
    text = str(getattr(btn, "text", btn))
    return Button.inline(text, "panel:help:close")


def _normalize_row(row) -> list:
    """Normalize a single row (list of buttons OR a single button) into a list of Button objects."""
    if isinstance(row, types.KeyboardButtonRow):
        return row
    if isinstance(row, list):
        return [_normalize_button(b) for b in row]
    return [_normalize_button(row)]


def to_edit_buttons(buttons: list) -> list:
    """Convert any button layout to list[list[Button]] for event.edit()."""
    if not buttons:
        return []
    result = []
    for row in buttons:
        normalized = _normalize_row(row)
        if isinstance(normalized, types.KeyboardButtonRow):
            result.append(normalized.buttons)
        else:
            result.append(normalized)
    return result


def _to_inline_rows(buttons: list) -> list:
    """Convert any button layout to list[KeyboardButtonRow] for ReplyInlineMarkup."""
    if not buttons:
        return []
    rows = []
    for row in buttons:
        normalized = _normalize_row(row)
        if isinstance(normalized, types.KeyboardButtonRow):
            rows.append(normalized)
        else:
            rows.append(types.KeyboardButtonRow(buttons=normalized))
    return rows


def render(
    title: str,
    body: str = "",
    buttons: list | None = None,
) -> types.InputBotInlineResult:
    """Build a single inline result. Accepts tuples OR Button objects."""
    if title and body:
        message = f"**{title}**\n\n{body}"
    elif title:
        message = f"**{title}**"
    else:
        message = body or ""

    markup_rows = _to_inline_rows(buttons) if buttons else None

    msg = types.InputBotInlineMessageText(
        message=message,
        reply_markup=types.ReplyInlineMarkup(rows=markup_rows) if markup_rows else None,
    )

    return types.InputBotInlineResult(
        id="0",
        type="article",
        title=title[:255] if title else "LifeOS",
        send_message=msg,
    )


def render_edit(
    title: str,
    body: str = "",
    buttons: list | None = None,
) -> tuple[str, list]:
    """Return (text, buttons) for event.edit(). Accepts tuples OR Button objects."""
    if title and body:
        text = f"**{title}**\n\n{body}"
    elif title:
        text = f"**{title}**"
    else:
        text = body or ""

    built = to_edit_buttons(buttons) if buttons else []
    return text, built
