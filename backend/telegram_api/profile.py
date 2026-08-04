"""
Profile module — update the owner's Telegram profile (bio, name).

Thin wrapper over Telethon's ``UpdateProfileRequest``. The actual
cron scheduling and template rendering stays in the Bio/Username
engines — this module only exposes the raw profile update primitive.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.telegram_api.exceptions import TelegramAPIError, TelegramTimeoutError

logger = logging.getLogger(__name__)

_RPC_TIMEOUT = 30.0


async def update_profile(
    client: Any,
    about: str | None = None,
    first_name: str | None = None,
    last_name: str | None = None,
) -> dict[str, Any]:
    """Update the current user's profile. Only provided fields are changed."""
    from telethon.tl.functions.account import UpdateProfileRequest

    kwargs: dict[str, Any] = {}
    if about is not None:
        kwargs["about"] = about
    if first_name is not None:
        kwargs["first_name"] = first_name
    if last_name is not None:
        kwargs["last_name"] = last_name

    if not kwargs:
        return {"updated": False}

    try:
        await asyncio.wait_for(
            client(UpdateProfileRequest(**kwargs)),
            timeout=_RPC_TIMEOUT,
        )
        return {"updated": True, "fields": list(kwargs.keys())}
    except asyncio.TimeoutError:
        raise TelegramTimeoutError(f"update_profile timed out after {_RPC_TIMEOUT}s")
    except Exception as exc:
        if isinstance(exc, TelegramAPIError):
            raise
        raise TelegramAPIError(f"update_profile failed: {exc}") from exc
