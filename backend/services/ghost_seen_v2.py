"""Ghost Seen v2 private-chat browser, bounded viewer, and selection model."""
from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any, Iterable

PAGE_SIZE = 5
MESSAGE_PAGE_SIZE = 8
_PREVIEW_LIMIT = 42
_NAME_LIMIT = 40
_MESSAGE_LIMIT = 180
_selections: dict[int, set[int]] = {}
_reply_states: dict[int, dict] = {}


def get_selected_ids(source_chat_id: int) -> tuple[int, ...]:
    return tuple(sorted(_selections.get(int(source_chat_id), set())))


def toggle_selection(source_chat_id: int, message_id: int) -> tuple[int, ...]:
    source_chat_id, message_id = int(source_chat_id), int(message_id)
    selected = _selections.setdefault(source_chat_id, set())
    if message_id in selected:
        selected.remove(message_id)
    else:
        selected.add(message_id)
    if not selected:
        _selections.pop(source_chat_id, None)
    return get_selected_ids(source_chat_id)


def clear_selection(source_chat_id: int) -> None:
    _selections.pop(int(source_chat_id), None)


def action_menu_state(source_chat_id: int) -> tuple[int, ...] | None:
    selected = get_selected_ids(source_chat_id)
    return selected or None


def reply_target(source_chat_id: int) -> int | None:
    """The single valid reply target message ID, or ``None`` unless exactly
    one message is currently selected in that source chat."""
    selected = get_selected_ids(source_chat_id)
    return selected[0] if len(selected) == 1 else None


def action_placeholder(action: str, source_chat_id: int, selected_ids: Iterable[int]) -> str:
    current = tuple(sorted(int(value) for value in selected_ids))
    if int(source_chat_id) <= 0 or not current or current != get_selected_ids(source_chat_id):
        return "That selection is no longer available."
    if action not in {"reply", "ai_reply"}:
        return "That action is not available."
    return "Coming in the next stage."


def begin_reply(panel_chat_id: int, source_chat_id: int, message_id: int, panel_msg_id: int) -> bool:
    """Arm a single-message Reply input for the panel chat.

    Stored keyed by the panel chat so a reply typed in another chat can
    never be consumed for this source. Only an exact one-message selection
    may begin a Reply.
    """
    source_chat_id, message_id = int(source_chat_id), int(message_id)
    if reply_target(source_chat_id) != message_id:
        return False
    _reply_states[int(panel_chat_id)] = {
        "source": source_chat_id,
        "message_id": message_id,
        "panel_msg_id": int(panel_msg_id),
    }
    return True


def get_reply(panel_chat_id: int) -> tuple[int, int, int] | None:
    entry = _reply_states.get(int(panel_chat_id))
    if entry is None:
        return None
    return int(entry["source"]), int(entry["message_id"]), int(entry["panel_msg_id"])


def consume_reply(panel_chat_id: int) -> tuple[int, int, int] | None:
    """Atomically take the pending Reply for the panel chat, if any."""
    entry = _reply_states.pop(int(panel_chat_id), None)
    if entry is None:
        return None
    return int(entry["source"]), int(entry["message_id"]), int(entry["panel_msg_id"])


def clear_reply(panel_chat_id: int) -> None:
    _reply_states.pop(int(panel_chat_id), None)


def clear_all_replies() -> None:
    _reply_states.clear()


async def send_reply(client: Any, source_chat_id: int, message_id: int, text: str) -> dict[str, Any]:
    """Send ``text`` to the source chat as a reply to the exact message.

    Uses the existing typed Telegram send utility; failures surface as
    exceptions so callers never report fabricated success.
    """
    from backend.telegram_api.messages import send_message
    return await send_message(client, int(source_chat_id), text, reply_to=int(message_id))


@dataclass(frozen=True)
class PrivateChat:
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


@dataclass(frozen=True)
class ViewerMessage:
    message_id: int
    source_chat_id: int
    text: str
    timestamp: datetime | None = None


@dataclass(frozen=True)
class MessageViewerPage:
    source_chat_id: int
    messages: tuple[ViewerMessage, ...]
    page: int
    total_pages: int
    selected_ids: tuple[int, ...] = ()


def _entity(value: Any) -> Any:
    return getattr(value, "entity", value)


def _is_telegram_user(entity: Any, dialog: Any = None) -> bool:
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
    if entity is None or not (_is_telegram_user(entity) or user_hint):
        return False
    if any(bool(getattr(entity, attr, False)) for attr in ("bot", "is_bot", "is_self", "deleted")):
        return False
    entity_id = getattr(entity, "id", None)
    return entity_id is not None and (owner_id is None or int(entity_id) != int(owner_id))


def is_private_user_dialog(dialog: Any, owner_id: int | None = None) -> bool:
    entity = _entity(dialog)
    if bool(getattr(dialog, "is_group", False) or getattr(dialog, "is_channel", False)):
        return False
    return _is_telegram_user(entity, dialog) and is_private_user_entity(entity, owner_id)


def filter_private_dialogs(dialogs: Iterable[Any], owner_id: int | None = None) -> list[Any]:
    return [dialog for dialog in dialogs if is_private_user_dialog(dialog, owner_id)]


def _single_line(value: str) -> str:
    return re.sub(r"[\r\n\t\s]+", " ", str(value or "")).strip()


def _normalize(value: str) -> str:
    return _single_line(value).casefold()


def _compact(value: str) -> str:
    return re.sub(r"\s+", "", _normalize(value))


def matches_search(chat: PrivateChat, query: str) -> bool:
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
    return normalized_query in (first, last, full_name) or normalized_query in first or normalized_query in last or bool(full_name and _compact(normalized_query) in _compact(full_name))


def _sort_key(chat: PrivateChat) -> float:
    if chat.timestamp is None:
        return float("-inf")
    value = chat.timestamp
    if value.tzinfo is None:
        value = value.replace(tzinfo=timezone.utc)
    return value.timestamp()


def page_items(chats: Iterable[PrivateChat], page: int = 1, query: str = "") -> BrowserPage:
    filtered = sorted((chat for chat in chats if matches_search(chat, query)), key=_sort_key, reverse=True)
    total_pages = max(1, (len(filtered) + PAGE_SIZE - 1) // PAGE_SIZE)
    current_page = min(max(int(page), 1), total_pages)
    start = (current_page - 1) * PAGE_SIZE
    return BrowserPage(tuple(filtered[start:start + PAGE_SIZE]), current_page, total_pages, _single_line(query))


def format_time(value: datetime | None, now: datetime | None = None) -> str:
    if value is None:
        return ""
    reference = now or datetime.now(value.tzinfo or timezone.utc)
    return value.strftime("%H:%M" if value.date() == reference.date() else "%d %b")


def truncate_preview(value: str, limit: int = _PREVIEW_LIMIT) -> str:
    text = _single_line(value)
    return text if len(text) <= limit else text[:max(1, limit - 1)].rstrip() + "…"


def _truncate_name(value: str) -> str:
    text = _single_line(value)
    return text if len(text) <= _NAME_LIMIT else text[:_NAME_LIMIT - 1].rstrip() + "…"


def _message_preview(message: Any) -> str:
    text = getattr(message, "text", None) or getattr(message, "message", None) or ""
    if text:
        return _single_line(text)
    return "Media" if getattr(message, "media", None) is not None else ""


def private_chat_from_dialog(dialog: Any, owner_id: int | None = None) -> PrivateChat | None:
    if not is_private_user_dialog(dialog, owner_id):
        return None
    entity = _entity(dialog)
    message = getattr(dialog, "message", None)
    return PrivateChat(int(entity.id), _single_line(getattr(entity, "first_name", "") or ""), _single_line(getattr(entity, "last_name", "") or ""), _single_line(getattr(entity, "username", "") or ""), _message_preview(message), getattr(message, "date", None), max(0, int(getattr(dialog, "unread_count", 0) or 0)))


async def load_private_chats(client: Any, owner_id: int | None = None) -> list[PrivateChat]:
    if client is None:
        return []
    chats = []
    async for dialog in client.iter_dialogs():
        chat = private_chat_from_dialog(dialog, owner_id)
        if chat is not None:
            chats.append(chat)
    return chats


async def load_viewer_messages(client: Any, source_chat_id: int, page: int = 1) -> MessageViewerPage:
    if client is None or int(source_chat_id) <= 0:
        return MessageViewerPage(int(source_chat_id), (), 1, 1, get_selected_ids(source_chat_id))
    messages = []
    async for message in client.iter_messages(int(source_chat_id), limit=MESSAGE_PAGE_SIZE * 20):
        message_id = int(getattr(message, "id", 0) or 0)
        if message_id <= 0:
            continue
        messages.append(ViewerMessage(message_id, int(source_chat_id), truncate_preview(_message_preview(message) or "Unsupported message", _MESSAGE_LIMIT), getattr(message, "date", None)))
    messages.sort(key=lambda item: item.message_id)
    total_pages = max(1, (len(messages) + MESSAGE_PAGE_SIZE - 1) // MESSAGE_PAGE_SIZE)
    current_page = min(max(int(page), 1), total_pages)
    start = (current_page - 1) * MESSAGE_PAGE_SIZE
    return MessageViewerPage(int(source_chat_id), tuple(messages[start:start + MESSAGE_PAGE_SIZE]), current_page, total_pages, get_selected_ids(source_chat_id))


def render_chat_row(chat: PrivateChat, now: datetime | None = None) -> str:
    right = format_time(chat.timestamp, now)
    if chat.unread_count > 0:
        right = f"{right}  {chat.unread_count}".strip()
    available = max(8, 56 - len(right))
    return f"💬 {_truncate_name(chat.display_name)}\n   {truncate_preview(chat.preview, available):<{available}}  {right}".rstrip()


def render_browser(chats: Iterable[PrivateChat], page: int = 1, query: str = "", watcher_count: int = 0, now: datetime | None = None) -> tuple[str, BrowserPage]:
    view = page_items(chats, page, query)
    lines = ["Private chats", f"● {max(0, int(watcher_count))} watchers", "", "👻 Ghost is peeking through the walls...", "If someone whispers, I'll let you know.", ""]
    if view.query:
        lines.extend([f"Search: {truncate_preview(view.query, 32)}", ""])
    if not view.chats:
        lines.extend(["No whispers yet...", "Ghost is quietly watching the walls."])
    else:
        lines.extend(["```", "\n".join(render_chat_row(chat, now) for chat in view.chats), "```"])
    return "\n".join(lines), view


def render_message_viewer(name: str, viewer: MessageViewerPage, now: datetime | None = None) -> str:
    suffix = f" · {len(viewer.selected_ids)} selected" if viewer.selected_ids else ""
    lines = ["👀 Ghost Seen", f"💬 {_truncate_name(name)}{suffix}", "", "Ghost is quietly watching the walls.", ""]
    if not viewer.messages:
        lines.append("👻 Ghost found nothing to see...")
    else:
        for item in viewer.messages:
            lines.append(f"«{item.text}»")
            stamp = format_time(item.timestamp, now)
            if stamp:
                lines.append(stamp)
    return "\n".join(lines)
