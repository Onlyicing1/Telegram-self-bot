"""
Centralized Telegram retry/backoff/timeout utility.

Every Telegram RPC call that may encounter FloodWait, network errors,
or timeouts should go through ``tg_rpc``. This eliminates duplicated
retry/backoff/timeout code scattered across handlers and services.

Usage:
    from backend.runtime.tg_retry import tg_rpc
    result = await tg_rpc(client.send_message(chat_id, "hello"), label="send_message")
    result = await tg_rpc(client.get_me(), label="get_me")

Behavior:
  - Bounded timeout (default 30s, configurable)
  - FloodWaitError: sleep the exact seconds + 1, then retry (up to max_retries)
  - Other transient errors: exponential backoff with jitter
  - CancelledError: always re-raised (cooperative cancellation)
  - Never silently swallows errors — raises on final failure
"""
from __future__ import annotations

import asyncio
import logging
import random
from typing import Any, Awaitable, TypeVar

from telethon.errors import FloodWaitError

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_DEFAULT_TIMEOUT = 30.0
_DEFAULT_MAX_RETRIES = 3
_BACKOFF_BASE = 2.0
_BACKOFF_MAX = 60.0
_BACKOFF_JITTER = 0.3


def _backoff_delay(attempt: int) -> float:
    base = min(_BACKOFF_MAX, _BACKOFF_BASE * (2 ** attempt))
    jitter = random.uniform(-_BACKOFF_JITTER, _BACKOFF_JITTER) * base
    return max(1.0, base + jitter)


async def tg_rpc(
    coro: Awaitable[_T],
    *,
    timeout: float = _DEFAULT_TIMEOUT,
    max_retries: int = _DEFAULT_MAX_RETRIES,
    label: str = "",
) -> _T:
    """Execute a Telegram RPC call with retry, backoff, and timeout.

    Args:
        coro:        The coroutine to execute.
        timeout:      Per-attempt timeout in seconds.
        max_retries:  Maximum number of retry attempts (total = max_retries + 1).
        label:        Human-readable label for logging.

    Returns:
        The result of the coroutine.

    Raises:
        The last exception if all retries are exhausted.
        asyncio.CancelledError is always re-raised immediately.
    """
    last_exc: BaseException | None = None
    for attempt in range(max_retries + 1):
        try:
            return await asyncio.wait_for(coro, timeout=timeout)
        except asyncio.CancelledError:
            raise
        except FloodWaitError as fwe:
            last_exc = fwe
            if attempt < max_retries:
                wait = fwe.seconds + 1
                logger.warning("TG_RPC FloodWait %ds (label=%s, attempt %d/%d) — sleeping",
                               wait, label, attempt + 1, max_retries + 1)
                await asyncio.sleep(wait)
                continue
            logger.error("TG_RPC FloodWait exhausted (label=%s): %ds", label, fwe.seconds)
            raise
        except (asyncio.TimeoutError, ConnectionError, OSError) as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = _backoff_delay(attempt)
                logger.warning("TG_RPC transient error (label=%s, attempt %d/%d): %s — retrying in %.1fs",
                               label, attempt + 1, max_retries + 1, type(exc).__name__, delay)
                await asyncio.sleep(delay)
                continue
            logger.error("TG_RPC exhausted after %d attempts (label=%s): %s",
                         max_retries + 1, label, exc)
            raise
        except Exception as exc:
            last_exc = exc
            if attempt < max_retries:
                delay = _backoff_delay(attempt)
                logger.warning("TG_RPC error (label=%s, attempt %d/%d): %s — retrying in %.1fs",
                               label, attempt + 1, max_retries + 1, type(exc).__name__, delay)
                await asyncio.sleep(delay)
                continue
            logger.error("TG_RPC exhausted after %d attempts (label=%s): %s",
                         max_retries + 1, label, exc)
            raise

    assert last_exc is not None
    raise last_exc
