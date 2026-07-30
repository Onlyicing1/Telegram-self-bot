"""
Helper Watchdog — monitors the helper bot and recovers it automatically.

The watchdog is now a thin layer that delegates to the RuntimeSupervisor.
It detects permanent failures (e.g. invalid token) and avoids infinite
reconnect loops by giving up after _MAX_REBUILD_ATTEMPTS.

  - If disconnected: reconnect automatically.
  - If reconnect repeatedly fails: rebuild helper automatically.
  - If rebuild repeatedly fails: mark permanently failed, stop retrying.
  - Never crashes backend.main.
"""
import asyncio
import logging

from backend.runtime.task_guard import guarded_create_task

logger = logging.getLogger(__name__)

_CHECK_INTERVAL = 30
_MAX_REBUILD_ATTEMPTS = 3
_REBUILD_DELAY = 15

_task: asyncio.Task | None = None
_consecutive_failures: int = 0
_permanent_failure: bool = False


def start() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = guarded_create_task(_watchdog_loop(), name="lifeos-helper-watchdog")
    logger.info("Helper watchdog started")


def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
    _task = None


def is_permanent_failure() -> bool:
    return _permanent_failure


async def _watchdog_loop() -> None:
    global _consecutive_failures, _permanent_failure

    while True:
        try:
            await asyncio.sleep(_CHECK_INTERVAL)

            if _permanent_failure:
                return

            from backend.helper.client import get_client
            helper = get_client()

            if helper is None:
                logger.warning("Helper watchdog: helper client is None")
                _consecutive_failures += 1
            elif not helper.is_connected():
                logger.warning("Helper watchdog: helper disconnected")
                _consecutive_failures += 1
            else:
                _consecutive_failures = 0
                continue

            if _consecutive_failures >= _MAX_REBUILD_ATTEMPTS:
                logger.error(
                    "Helper watchdog: %d consecutive failures — marking permanently failed",
                    _consecutive_failures,
                )
                _permanent_failure = True
                from backend.health import set_helper_connected
                set_helper_connected(False)
                return

        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Helper watchdog loop error: %s", exc)
