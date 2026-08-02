"""
Bounded timeout wrapper for all Telegram RPC calls.

When Telegram's RPC layer stops responding (while the MTProto connection
stays alive), unbounded awaits hang forever and block the entire event loop.
This wrapper ensures every RPC call completes within a bounded time.
"""
import asyncio
import functools
import logging
from typing import Any, Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

_T = TypeVar("_T")

_DEFAULT_TIMEOUT = 30.0


async def rpc_await(coro: Awaitable[_T], timeout: float = _DEFAULT_TIMEOUT, label: str = "") -> _T:
    try:
        return await asyncio.wait_for(coro, timeout=timeout)
    except asyncio.TimeoutError:
        logger.error("RPC_TIMEOUT label='%s' timeout=%.1fs — Telegram RPC did not respond in time", label, timeout)
        raise
    except asyncio.CancelledError:
        raise


def rpc_timeout(timeout: float = _DEFAULT_TIMEOUT, label: str = ""):
    def decorator(fn: Callable[..., Awaitable[_T]]) -> Callable[..., Awaitable[_T]]:
        @functools.wraps(fn)
        async def wrapper(*args: Any, **kwargs: Any) -> _T:
            return await rpc_await(fn(*args, **kwargs), timeout=timeout, label=label or fn.__name__)
        return wrapper
    return decorator
