"""Ghost Seen v2 Stage 1: a private Telegram chat browser.

This module deliberately contains no reply, selection, AI, or Ghost Room
behavior. It turns the current Telegram dialog stream into a small, bounded
view model for the first Ghost Seen screen.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

PAGE_SIZE = 5
_PREVIEW_LIMIT = 42
_NAME_LIMIT = 40


@dataclass(frozen=True)
class PrivateChat:
    """The display-safe fields needed by the Stage 1 browser."""

    chat_id: int
    first_name: str = ""
    last_name: str = ""
    username: str = ""
    preview: str = ""
    timestamp: datetime | None = None
    unread_count: int = 0

    @property
    def display_name(self) -> str:
        parts = [part for part in (self.first_name, self.last_name) if part]
        return " ".join(parts).strip() or self.username.strip() or "Unknown"


@dataclass(frozen=True)
class BrowserPage:
    chats: tuple[PrivateChat, ...]
    page: int
    total_pages: int
    query: str = ""


def _entity(dialog_or_entity: Any) -> Any:
    return getattr(dialog_or_entity, "entity", dialog_or_entity)


def _is_telegram_user(entity: Any, dialog: Any = None) -> bool:
    """Identify a User without trusting a display title or chat id shape."""
    try:
        from telethon.tl.types import User

        if isinstance(entity, User):
            return True
    except Exception:
        pass

    if entity.__class__.__name__ == "User":
        return True
    if entity.__class__.__name__ == "SimpleNamespace" and hasattr(entity, "first_name"):
        return True
    return bool(dialog is not None and getattr(dialog, "is_user", False))


def is_private_user_entity(entity: Any, owner_id: int | None = None, user_hint: bool = False) -> bool:
    """Return whether an entity is an eligible human private-chat user."""
    if entity is None or not (_is_telegram_user(entity) or user_hint):
        return False
    if any(bool(getattr(entity, attr, False)) for attr in ("bot", "is_bot", "is_self", "deleted")):
        return False
    entity_id = getattr(entity, "id", None)
    if entity_id is None:
        return False
    return owner_id is None or int(entity_id) != int(owner_id)


def filter_private_dialogs(dialogs: Iterable[Any], owner_id: int | None = None) -> list[Any]:
    return [dialog for dialog in dialogs if is_private_user_dialog(dialog, owner_id)]


def is_private_user_dialog(dialog: Any, owner_id: int | None = None) -> bool:
    """Filter a Telethon dialog to a real human-to-owner conversation."""
    entity = _entity(dialog)
    if bool(getattr(dialog, "is_group", False) or getattr(dialog, "is_channel", False)):
        return False
    if not _is_telegram_user(entity, dialog):
        return False
    return is_private_user_entity(entity, owner_id)


def _single_line(value: str) -> str:
    return re.sub(r"[\r\n\t\s]+", " ", str(value or "")).strip()


def _normalize(value: str) -> str:
    return _single_line(value).casefold()


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", _normalize(value))


def matches_search(chat: PrivateChat, query: str) -> bool:
    """Match only name components and username, tolerating human spacing."""
    normalized_query = _normalize(query)
    if not normalized_query:
        return True

    query_without_at = normalized_query.lstrip("@")
    username = _normalize(chat.username).lstrip("@")
    if username and query_without_at == username:
        return True

    first = _normalize(chat.first_name)
    last = _normalize(chat.last_name)
    full_name = _normalize(f"{chat.first_name} {chat.last_name}")
    if normalized_query in (first, last, full_name):
        return True
    if normalized_query in first or normalized_query in last:
        return True

    return bool(full_name and _compact(normalized_query) in _compact(full_name))


def _sort_key(chat: PrivateChat) -> float:
    if chat.timestamp is None:
        return float("-inf")
    try:
        value = chat.timestamp
        if value.tzinfo is None:
            value = value.replace(tzinfo=timezone.utc)
        return value.timestamp()
    except (AttributeError, TypeError, ValueError, OverflowError):
        return float("-inf")


def page_items(chats: Iterable[PrivateChat], page: int = 1, query: str = "") -> BrowserPage:
    filtered = sorted(
        (chat for chat in chats if matches_search(chat, query)),
        key=_sort_key,
        reverse=True,
    )
    total_pages = max(1, (len(filtered) + PAGE_SIZE - 1) // PAGE_SIZE)
    current_page = min(max(int(page), 1), total_pages)
    start = (current_page - 1) * PAGE_SIZE
    return BrowserPage(
        chats=tuple(filtered[start:start + PAGE_SIZE]),
        page=current_page,
        total_pages=total_pages,
        query=_single_line(query),
    )


def format_time(value: datetime | None, now: datetime | None = None) -> str:
    if value is None:
        return ""
    reference = now or datetime.now(value.tzinfo or timezone.utc)
    if value.date() == reference.date():
        return value.strftime("%H:%M")
    return value.strftime("%d %b")


def truncate_preview(value: str, limit: int = _PREVIEW_LIMIT) -> str:
    text = _single_line(value)
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)].rstrip() + "…"


def _truncate_name(value: str) -> str:
    text = _single_line(value)
    if len(text) <= _NAME_LIMIT:
        return text
    return text[:_NAME_LIMIT - 1].rstrip() + "…"


def _message_preview(message: Any) -> str:
    text = getattr(message, "text", None) or getattr(message, "message", None) or ""
    if text:
        return _single_line(text)
    if getattr(message, "media", None) is not None:
        return "Media"
    return ""


def private_chat_from_dialog(dialog: Any, owner_id: int | None = None) -> PrivateChat | None:
    if not is_private_user_dialog(dialog, owner_id):
        return None
    entity = _entity(dialog)
    chat_id = getattr(entity, "id", None)
    if chat_id is None:
        return None
    message = getattr(dialog, "message", None)
    return PrivateChat(
        chat_id=int(chat_id),
        first_name=_single_line(getattr(entity, "first_name", "") or ""),
        last_name=_single_line(getattr(entity, "last_name", "") or ""),
        username=_single_line(getattr(entity, "username", "") or ""),
        preview=_message_preview(message),
        timestamp=getattr(message, "date", None),
        unread_count=max(0, int(getattr(dialog, "unread_count", 0) or 0)),
    )


async def load_private_chats(client: Any, owner_id: int | None = None) -> list[PrivateChat]:
    """Read current dialogs and return only eligible human private chats."""
    if client is None:
        return []
    chats: list[PrivateChat] = []
    async for dialog in client.iter_dialogs():
        chat = private_chat_from_dialog(dialog, owner_id)
        if chat is not None:
            chats.append(chat)
    return chats


def render_chat_row(chat: PrivateChat, now: datetime | None = None) -> str:
    """Render one chat as exactly two source lines in a fixed-width block."""
    name = _truncate_name(chat.display_name)
    preview = truncate_preview(chat.preview)
    right = format_time(chat.timestamp, now)
    if chat.unread_count > 0:
        right = f"{right}  {chat.unread_count}".strip()

    # The list is placed in a Markdown code block by render_browser. Keeping
    # the width calculation here makes the timestamp/count columns stable
    # without letting a long preview add a third line.
    available_preview = max(8, 56 - len(right))
    preview = truncate_preview(preview, available_preview)
    second = f"   {preview:<{available_preview}}  {right}".rstrip()
    return f"💬 {name}\n{second}"


def render_browser(
    chats: Iterable[PrivateChat],
    page: int = 1,
    query: str = "",
    watcher_count: int = 0,
    now: datetime | None = None,
) -> tuple[str, BrowserPage]:
    """Render the Stage 1 browser body and its normalized page model."""
    view = page_items(chats, page, query)
    lines = [
        "Private chats",
        f"● {max(0, int(watcher_count))} watchers",
        "",
        "👻 Ghost is peeking through the walls...",
        "If someone whispers, I'll let you know.",
        "",
    ]
    if view.query:
        lines.append(f"Search: {truncate_preview(view.query, 32)}")
        lines.append("")

    if not view.chats:
        lines.extend([
            "No whispers yet...",
            "Ghost is quietly watching the walls.",
        ])
    else:
        lines.extend([
            "```",
            "\n".join(render_chat_row(chat, now) for chat in view.chats),
            "```",
        ])
    return "\n".join(lines), view
