"""
PanelRenderer — reusable inline panel renderer.

Every panel must use this renderer. No panel may manually build layouts.

Single button API (Option A):
  Builders always return tuples: ("Text", "callback_data")
  render() and render_edit() create Button objects from tuples.
  No other format is ever used.

Rules:
  - NO ASCII
  - NO separators
  - NO hamburger menu
  - NO command cheat sheet
  - Clean minimal layout
"""
from telethon.tl import types
from telethon.tl.custom import Button

from backend.helper.context import truncate_callback_data


def render(
    title: str,
    body: str = "",
    buttons: list | None = None,
) -> types.InputBotInlineResult:
    """Build a single inline result with a clean minimal layout.

    buttons: list of rows, each row is a list of (text, callback_data) tuples.
    """
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
    """Return (text, buttons) for event.edit() calls.

    buttons arg: list of rows, each row is a list of (text, callback_data) tuples.
    buttons output: list of lists of Button objects (for Telethon event.edit).
    """
    if title and body:
        text = f"**{title}**\n\n{body}"
    elif title:
        text = f"**{title}**"
    else:
        text = body or ""

    built = to_edit_buttons(buttons) if buttons else []
    return text, built


def to_edit_buttons(buttons: list) -> list:
    """Convert list[list[(text, data)]] to list[list[Button]] for event.edit()."""
    return [
        [Button.inline(text, truncate_callback_data(data)) for text, data in row]
        for row in buttons
    ]


def _to_inline_rows(buttons: list) -> list:
    """Convert list[list[(text, data)]] to list[KeyboardButtonRow] for ReplyInlineMarkup."""
    rows = []
    for row in buttons:
        row_buttons = [Button.inline(text, truncate_callback_data(data)) for text, data in row]
        rows.append(types.KeyboardButtonRow(buttons=row_buttons))
    return rows
