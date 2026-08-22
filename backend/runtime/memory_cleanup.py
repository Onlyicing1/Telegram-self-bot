"""
Memory cleanup worker — periodic expired-memory purging.

Runs as a supervised background task that wakes every 6 hours and:
  1. Calls LongMemory.expire_old() to remove expired long-tier memories.
  2. Calls PermanentMemory.token_footprint() to warn if permanent
     memory exceeds the soft token cap.

The worker is safe to run with or without Supabase — when the repository
is in-memory (no DB), expire_old() is a no-op.

This ensures no stale memory remains forever, matching the architecture:
    Short → Long → Permanent → Expiration → Cleanup
"""
from __future__ import annotations

import asyncio
import logging

from backend.runtime.task_guard import immortal_create_task

logger = logging.getLogger(__name__)

_CLEANUP_INTERVAL = 6 * 3600  # 6 hours
_task: asyncio.Task | None = None


async def _cleanup_loop() -> None:
    logger.info("Memory cleanup worker started (interval=%ds)", _CLEANUP_INTERVAL)
    while True:
        await asyncio.sleep(_CLEANUP_INTERVAL)
        try:
            from backend.ai.memory.long import LongMemory
            from backend.ai.memory.permanent import PermanentMemory
            from backend.ai.database.manager import get_repository_manager

            repo_mgr = get_repository_manager()
            long_repo = repo_mgr.memory if repo_mgr.supabase_available else None
            perm_repo = repo_mgr.memory if repo_mgr.supabase_available else None

            # Repository calls are synchronous (Supabase HTTP) — run them off
            # the event loop so maintenance can never stall AI dispatch.
            long_mem = LongMemory(repository=long_repo)
            removed = await asyncio.to_thread(long_mem.expire_old)
            if removed > 0:
                logger.info("Memory cleanup: expired %d long-tier memories", removed)

            perm_mem = PermanentMemory(repository=perm_repo)
            footprint = await asyncio.to_thread(perm_mem.token_footprint, 0)
            if footprint > 500:
                logger.warning("Memory cleanup: permanent memory token footprint=%d exceeds soft cap 500", footprint)

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Memory cleanup worker error: %s", exc)


def start_memory_cleanup() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = immortal_create_task(_cleanup_loop, name="lifeos-memory-cleanup")


async def stop_memory_cleanup() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await asyncio.wait_for(_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    _task = None
