"""
Media module — download media from a message.

Downloads to a file path or returns raw bytes. Respects the size limits
enforced by the caller (service layer).
"""
from __future__ import annotations

import logging
import os
from typing import Any

from backend.telegram_api.exceptions import TelegramAPIError

logger = logging.getLogger(__name__)


async def download_media(
    client: Any,
    message: Any,
    file_path: str | None = None,
    progress_callback: Any = None,
) -> str | bytes | None:
    """Download media from a message.

    If ``file_path`` is provided, downloads to that path and returns it.
    Otherwise downloads to a BytesIO and returns the raw bytes.

    Returns None if the message has no media.
    """
    try:
        result = await client.download_media(
            message, file=file_path, progress_callback=progress_callback,
        )
        if result is None:
            return None
        if isinstance(result, str) and os.path.exists(result):
            return result
        return result
    except Exception as exc:
        if isinstance(exc, TelegramAPIError):
            raise
        raise TelegramAPIError(f"download_media failed: {exc}") from exc
