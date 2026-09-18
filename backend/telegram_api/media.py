"""
Media module — bounded transfer of one message's media, in both directions.

Downloads to a file path or returns raw bytes; uploads ONE bounded audio buffer
as a voice message. The caller (service layer) enforces the SIZE limit; this
module owns the TIME bound: every transfer runs under ``guarded_await`` — the
same bounded-operation primitive ``messages`` uses — so a stalled transfer can
never hang the event loop. A large transfer legitimately outlives the short-call
bound, so the ceiling is its own finite constant rather than the 30s used by the
short RPC helpers.
"""
from __future__ import annotations

import asyncio
import io
import logging
import os
from typing import Any

from backend.runtime.operation_watchdog import guarded_await
from backend.telegram_api._helpers import serialize_message
from backend.telegram_api.exceptions import (
    TelegramAPIError,
    TelegramTimeoutError,
)

logger = logging.getLogger(__name__)

#: Finite ceiling for ONE media download. A media transfer (up to the
#: service layer's size limit) can legitimately take longer than the 30s
#: short-call bound, but it is never unbounded: a stalled download fails
#: honestly instead of holding the request forever. This is the single
#: authority for the bound — the media service derives its budget from it.
MEDIA_DOWNLOAD_TIMEOUT_S = 120.0

#: Finite ceiling for ONE media upload. Uploading an already-produced, bounded
#: buffer is a single short RPC by nature, but it is never unbounded: the send
#: fails honestly instead of holding the request forever.
MEDIA_UPLOAD_TIMEOUT_S = 120.0

#: Telegram's own voice-note representation: an OGG container with Opus audio.
#: Declared here (not at the call site) so one caller cannot name a different
#: container while asking for a voice message.
VOICE_NOTE_MIME = "audio/ogg"


async def download_media(
    client: Any,
    message: Any,
    file_path: str | None = None,
    progress_callback: Any = None,
    timeout: float | None = None,
) -> str | bytes | None:
    """Download media from a message under a bounded timeout.

    If ``file_path`` is provided, downloads to that path and returns it.
    Otherwise downloads to a BytesIO and returns the raw bytes.

    ``timeout`` lets a caller impose a tighter bound than the module ceiling;
    ``None`` (or a non-positive value) means "use the ceiling".

    Returns None if the message has no media.

    Raises:
        TelegramTimeoutError: the transfer exceeded its bound.
        TelegramAPIError:     the transfer failed for any other reason.
    """
    effective = _effective_timeout(timeout)
    try:
        result = await guarded_await(
            client.download_media(
                message, file=file_path, progress_callback=progress_callback,
            ),
            name="telegram:download_media",
            timeout=effective,
        )
    except asyncio.TimeoutError:
        logger.error(
            "TELEGRAM_MEDIA_TIMEOUT timeout=%.1fs — download did not finish in time",
            effective,
        )
        raise TelegramTimeoutError(f"download_media timed out after {effective}s")
    except Exception as exc:
        if isinstance(exc, TelegramAPIError):
            raise
        raise TelegramAPIError(f"download_media failed: {exc}") from exc

    if result is None:
        return None
    if isinstance(result, str) and os.path.exists(result):
        return result
    return result


def _effective_timeout(timeout: float | None) -> float:
    """Clamp a caller-supplied bound to the module ceiling (fail-closed)."""
    try:
        value = float(timeout) if timeout is not None else MEDIA_DOWNLOAD_TIMEOUT_S
    except (TypeError, ValueError):
        return MEDIA_DOWNLOAD_TIMEOUT_S
    if value <= 0:
        return MEDIA_DOWNLOAD_TIMEOUT_S
    return min(value, MEDIA_DOWNLOAD_TIMEOUT_S)


async def send_voice(
    client: Any,
    chat_id: int | str,
    audio: bytes | bytearray,
    mime_type: str = VOICE_NOTE_MIME,
    timeout: float | None = None,
) -> dict[str, Any]:
    """Send ONE bounded audio buffer as a Telegram VOICE message.

    The buffer is already-produced audio (the TTS boundary's normalized clip), so
    this helper owns only the transfer: it never parses, transcodes, names or
    stores it. ``chat_id`` is the caller's TRUSTED destination, resolved from
    runtime context — this module never chooses a recipient.

    ``timeout`` lets a caller impose a tighter bound than the module ceiling;
    ``None`` (or a non-positive value) means "use the ceiling".

    Raises:
        TelegramAPIError:     nothing usable to send, or the upload failed.
        TelegramTimeoutError: the upload exceeded its bound.
    """
    if not isinstance(audio, (bytes, bytearray)) or not audio:
        raise TelegramAPIError("send_voice needs audio bytes; nothing was sent.")
    effective = _effective_upload_timeout(timeout)
    try:
        message = await guarded_await(
            client.send_file(
                chat_id,
                io.BytesIO(bytes(audio)),
                voice_note=True,
                force_document=False,
                mime_type=mime_type,
            ),
            name="telegram:send_voice",
            timeout=effective,
        )
    except asyncio.TimeoutError:
        logger.error(
            "TELEGRAM_VOICE_TIMEOUT timeout=%.1fs — voice note was not sent in time",
            effective,
        )
        raise TelegramTimeoutError(f"send_voice timed out after {effective}s")
    except Exception as exc:
        if isinstance(exc, TelegramAPIError):
            raise
        raise TelegramAPIError(f"send_voice failed: {exc}") from exc
    return serialize_message(message)


def _effective_upload_timeout(timeout: float | None) -> float:
    """Clamp a caller-supplied upload bound to the module ceiling (fail-closed)."""
    try:
        value = float(timeout) if timeout is not None else MEDIA_UPLOAD_TIMEOUT_S
    except (TypeError, ValueError):
        return MEDIA_UPLOAD_TIMEOUT_S
    if value <= 0:
        return MEDIA_UPLOAD_TIMEOUT_S
    return min(value, MEDIA_UPLOAD_TIMEOUT_S)
