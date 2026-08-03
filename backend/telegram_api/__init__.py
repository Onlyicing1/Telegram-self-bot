"""
Telegram Internal API — the ONLY interface between AI Tools and Telethon.

AI Tools must NEVER import Telethon directly. They call the clean async
methods exposed by ``TelegramAPI``:

    api.send_message(chat_id, text)
    api.edit_message(chat_id, msg_id, text)
    api.delete_message(chat_id, msg_id)
    api.forward_message(chat_id, from_chat_id, msg_id)
    api.download_media(message, file_path)
    api.get_dialogs()
    api.get_chat(chat_id)
    api.get_user(user_id)
    api.search_messages(chat_id, query, limit)

The layer is a thin facade — it wraps Telethon calls with:
  - bounded timeouts (no unbounded awaits)
  - FloodWait handling (sleep the exact seconds)
  - structured exceptions (TelegramAPIError hierarchy)
  - consistent return types (plain dicts, not Telethon objects)
  - proper logging

No business logic lives here. No database access. No state.
It is a pure pass-through abstraction over the MTProto client.
"""
from backend.telegram_api.api import TelegramAPI
from backend.telegram_api.exceptions import (
    TelegramAPIError,
    TelegramTimeoutError,
    TelegramFloodWaitError,
    TelegramNotFoundError,
)

__all__ = [
    "TelegramAPI",
    "TelegramAPIError",
    "TelegramTimeoutError",
    "TelegramFloodWaitError",
    "TelegramNotFoundError",
]
