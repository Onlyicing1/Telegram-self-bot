"""
PanelRenderer — reusable inline panel renderer.

Every panel must use this renderer. No panel may manually build layouts.

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

    The layout is:
      <title>           (bold)
      <body>            (optional, plain text)

    Buttons are passed as a list of rows, each row a list of
    (text, callback_data) tuples.
    """
    if title and body:
        message = f"**{title}**\n\n{body}"
    elif title:
        message = f"**{title}**"
    else:
        message = body or ""

    markup_rows = _build_button_rows(buttons) if buttons else None

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

    buttons arg/output format: list of rows, each row is list of (text, data) tuples.
    """
    if title and body:
        text = f"**{title}**\n\n{body}"
    elif title:
        text = f"**{title}**"
    else:
        text = body or ""

    built = _build_button_rows(buttons) if buttons else []
    return text, built


def _build_button_rows(buttons: list) -> list:
    """Convert list-of-rows of (text, data) tuples into KeyboardButtonRow list."""
    rows = []
    for row in buttons:
        row_buttons = []
        for item in row:
            if isinstance(item, Button):
                row_buttons.append(item)
            else:
                text, data = item
                row_buttons.append(
                    Button.inline(text, truncate_callback_data(data))
                )
        rows.append(types.KeyboardButtonRow(buttons=row_buttons))
    return rows
