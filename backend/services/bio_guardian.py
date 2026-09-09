"""
Global Bio mutation guardian.

Hard execution safety boundary for Telegram Bio mutations: at most ONE
successful ``UpdateProfileRequest(about=...)`` within any rolling 60-second
window, across EVERY Bio mutation path — recurring AI tasks, manual AI
commands, deterministic tools, the profile cron scheduler, prepare-ahead
execution, retries, duplicate wakes, and overlapping execution paths.

Every Bio mutation flows through :func:`guard_bio_mutation`. The guardian is
concurrency-safe (one asyncio lock serializes racing callers; the window is
re-checked inside the lock, so exactly one racing mutation can reach
Telegram) and honest: a rejected mutation raises and is never reported as a
success, and a failed mutation never starts the window.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Awaitable, Callable, TypeVar

logger = logging.getLogger(__name__)

WINDOW_SECONDS = 60.0

_T = TypeVar("_T")

_last_success_monotonic: float | None = None
_attempt_lock = asyncio.Lock()


class BioMutationGuarded(RuntimeError):
    """A Bio mutation was rejected because another succeeded within the
    rolling window. The Telegram profile was NOT touched."""


def seconds_until_bio_mutation_allowed() -> float:
    """Seconds remaining in the active window; 0.0 when a mutation may run."""
    if _last_success_monotonic is None:
        return 0.0
    remaining = WINDOW_SECONDS - (time.monotonic() - _last_success_monotonic)
    return remaining if remaining > 0 else 0.0


async def guard_bio_mutation(mutation: Callable[[], Awaitable[_T]]) -> _T:
    """Run exactly one async ``mutation()`` under the Bio mutation boundary.

    Concurrent callers serialize on one lock; after any success the window
    check inside the lock rejects every further mutation for
    ``WINDOW_SECONDS``. A mutation that raises never starts the window.
    """
    global _last_success_monotonic
    async with _attempt_lock:
        remaining = seconds_until_bio_mutation_allowed()
        if remaining > 0:
            raise BioMutationGuarded(
                f"bio mutation rejected: the last bio mutation succeeded "
                f"{WINDOW_SECONDS - remaining:.1f}s ago; the "
                f"{WINDOW_SECONDS:.0f}-second bio mutation window is active"
            )
        result = await mutation()
        _last_success_monotonic = time.monotonic()
        return result


def reset_window_for_tests() -> None:
    """Clear the window state (tests only)."""
    global _last_success_monotonic
    _last_success_monotonic = None
