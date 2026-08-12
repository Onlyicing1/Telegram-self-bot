"""
Entities module — resolve and fetch chats, users, dialogs.

All methods return plain dicts. Callers never receive Telethon objects.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.runtime.operation_watchdog import guarded_await
from backend.telegram_api._helpers import serialize_chat, serialize_user
from backend.telegram_api.exceptions import (
    TelegramAPIError,
    TelegramNotFoundError,
    TelegramTimeoutError,
)

logger = logging.getLogger(__name__)

_RPC_TIMEOUT = 30.0


async def get_entity(client: Any, entity: int | str) -> Any:
    """Resolve any entity reference (ID, username) to a Telethon entity object.

    This is the one method that returns a raw Telethon object — it is
    used internally by other methods that need the resolved entity for
    further API calls.
    """
    try:
        return await guarded_await(
            client.get_entity(entity),
            name="telegram:get_entity",
            timeout=_RPC_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise TelegramTimeoutError(f"get_entity timed out after {_RPC_TIMEOUT}s")
    except Exception as exc:
        if isinstance(exc, TelegramAPIError):
            raise
        raise TelegramAPIError(f"get_entity failed: {exc}") from exc


async def get_input_entity(client: Any, entity: int | str) -> Any:
    """Resolve an entity reference to an InputPeer."""
    try:
        return await guarded_await(
            client.get_input_entity(entity),
            name="telegram:get_input_entity",
            timeout=_RPC_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise TelegramTimeoutError(f"get_input_entity timed out after {_RPC_TIMEOUT}s")
    except Exception as exc:
        if isinstance(exc, TelegramAPIError):
            raise
        raise TelegramAPIError(f"get_input_entity failed: {exc}") from exc


async def get_chat(client: Any, chat_id: int | str) -> dict[str, Any]:
    """Get chat metadata by ID or username. Returns serialized dict."""
    entity = await get_entity(client, chat_id)
    if entity is None:
        raise TelegramNotFoundError(f"Chat not found: {chat_id}")
    return serialize_chat(entity)


async def get_user(client: Any, user_id: int | str) -> dict[str, Any]:
    """Get user metadata by ID or username. Returns serialized dict."""
    entity = await get_entity(client, user_id)
    if entity is None:
        raise TelegramNotFoundError(f"User not found: {user_id}")
    return serialize_user(entity)


async def get_me(client: Any) -> dict[str, Any]:
    """Get the current account's user info. Returns serialized dict."""
    try:
        me = await guarded_await(
            client.get_me(),
            name="telegram:get_me",
            timeout=_RPC_TIMEOUT,
        )
        return serialize_user(me)
    except asyncio.TimeoutError:
        raise TelegramTimeoutError(f"get_me timed out after {_RPC_TIMEOUT}s")
    except Exception as exc:
        if isinstance(exc, TelegramAPIError):
            raise
        raise TelegramAPIError(f"get_me failed: {exc}") from exc


async def get_dialogs(client: Any, limit: int = 100) -> list[dict[str, Any]]:
    """List dialogs (chat list). Returns list of serialized dicts."""
    async def collect_dialogs() -> list[dict[str, Any]]:
        results: list[dict[str, Any]] = []
        async for dialog in client.iter_dialogs(limit=limit):
            results.append({
                "id": dialog.id,
                "name": dialog.name,
                "is_channel": getattr(dialog, "is_channel", False),
                "is_group": getattr(dialog, "is_group", False),
                "is_user": getattr(dialog, "is_user", False),
                "unread_count": getattr(dialog, "unread_count", 0),
            })
            if len(results) >= limit:
                break
        return results

    try:
        return await guarded_await(
            collect_dialogs(),
            name="telegram:get_dialogs",
            timeout=_RPC_TIMEOUT,
        )
    except asyncio.TimeoutError:
        raise TelegramTimeoutError(f"get_dialogs timed out after {_RPC_TIMEOUT}s")
    except Exception as exc:
        if isinstance(exc, TelegramAPIError):
            raise
        raise TelegramAPIError(f"get_dialogs failed: {exc}") from exc
