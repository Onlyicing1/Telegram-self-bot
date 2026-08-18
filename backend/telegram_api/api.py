"""
TelegramAPI — the single public facade for all Telegram operations.

AI Tools receive a ``TelegramAPI`` instance via ``ToolContext.telegram``.
They never import Telethon, never call ``client.send_message()``, and
never handle raw Telethon objects.

The facade wraps a Telethon client and delegates to the sub-modules
(messages, media, entities). Every method is async, has a bounded
timeout, and returns plain dicts or simple types.

The facade is stateless beyond the client reference. No caching, no
background tasks, no polling. It is a pure pass-through.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.telegram_api import entities, media, messages

logger = logging.getLogger(__name__)


class TelegramAPI:
    """Clean async facade over the Telethon MTProto client.

    Constructed once with the active Telethon client and injected into
    ``ToolContext``. All methods are async and return plain types.

    Usage::

        api = TelegramAPI(client)
        msg = await api.send_message(chat_id, "hello")
        await api.delete_message(chat_id, msg["id"])
    """

    __slots__ = ("_client",)

    def __init__(self, client: Any) -> None:
        self._client = client

    @property
    def client(self) -> Any:
        """Direct client access — for service layer use only.

        AI Tools must NOT use this property. It exists so the existing
        service layer can receive the raw client when needed (e.g.
        ``save_service.execute_save`` expects a Telethon client for
        complex media operations that don't map to simple primitives).
        """
        return self._client

    # ── Messages ──

    async def send_message(self, chat_id: int | str, text: str, **kwargs: Any) -> dict[str, Any]:
        return await messages.send_message(self._client, chat_id, text, **kwargs)

    async def edit_message(self, chat_id: int | str, msg_id: int, text: str, **kwargs: Any) -> dict[str, Any]:
        return await messages.edit_message(self._client, chat_id, msg_id, text, **kwargs)

    async def delete_messages(self, chat_id: int | str, msg_ids: list[int]) -> int:
        return await messages.delete_messages(self._client, chat_id, msg_ids)

    async def delete_message(self, chat_id: int | str, msg_id: int) -> int:
        return await messages.delete_messages(self._client, chat_id, [msg_id])

    async def get_message(self, chat_id: int | str, msg_id: int) -> dict[str, Any]:
        return await messages.get_message(self._client, chat_id, msg_id)

    async def get_messages(self, chat_id: int | str, ids: list[int]) -> list[dict[str, Any]]:
        return await messages.get_messages(self._client, chat_id, ids)

    async def forward_messages(
        self,
        dest_chat_id: int | str,
        msg_ids: list[int] | int,
        from_chat_id: int | str,
    ) -> list[dict[str, Any]]:
        return await messages.forward_messages(self._client, dest_chat_id, msg_ids, from_chat_id)

    async def iter_messages(
        self,
        chat_id: int | str,
        limit: int = 100,
        from_user: str | None = None,
        min_id: int | None = None,
    ) -> list[dict[str, Any]]:
        return await messages.iter_messages(self._client, chat_id, limit, from_user, min_id)

    async def search_messages(
        self,
        chat_id: int | str,
        query: str,
        limit: int = 20,
    ) -> list[dict[str, Any]]:
        return await messages.search_messages(self._client, chat_id, query, limit)

    # ── Media ──

    async def download_media(
        self,
        message: Any,
        file_path: str | None = None,
        progress_callback: Any = None,
    ) -> str | bytes | None:
        return await media.download_media(self._client, message, file_path, progress_callback)

    # ── Entities ──

    async def get_chat(self, chat_id: int | str) -> dict[str, Any]:
        return await entities.get_chat(self._client, chat_id)

    async def get_user(self, user_id: int | str) -> dict[str, Any]:
        return await entities.get_user(self._client, user_id)

    async def get_me(self) -> dict[str, Any]:
        return await entities.get_me(self._client)

    async def get_dialogs(self, limit: int = 100) -> list[dict[str, Any]]:
        return await entities.get_dialogs(self._client, limit)

    async def get_input_entity(self, entity: int | str) -> Any:
        return await entities.get_input_entity(self._client, entity)

