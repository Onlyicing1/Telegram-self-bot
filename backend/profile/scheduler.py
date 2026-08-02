"""
Shared Profile Scheduler — fires once per minute at HH:MM:00.

All profile engines (Bio, Username, future engines) register an updater
callable via ``register_updater``. The scheduler calls every active
updater each tick, collects the profile fields they want to change,
and sends a SINGLE ``UpdateProfileRequest`` to Telegram.

This guarantees:
- Exactly one ``UpdateProfileRequest`` per minute — never more.
- Bio and Username (and any future engine) update together in ONE API call.
- Each engine is completely independent — it only knows about its own state.
- Adding a new engine is as simple as calling ``register_updater``.
"""
import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from typing import Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telethon.errors import FloodWaitError
from telethon.tl.functions.account import UpdateProfileRequest

from backend.diagnostics import record_event
from backend.runtime.tracer import trace, trace_exception
from backend.runtime.task_guard import guarded_create_task

logger = logging.getLogger(__name__)

_API_TIMEOUT = 30
_BACKOFF_BASE = 2.0
_BACKOFF_MAX = 60.0
_BACKOFF_JITTER = 0.3
_STOP_TIMEOUT = 10.0

UpdaterFn = Callable[[int, str], Awaitable[dict[str, str] | None]]

_updaters: list[tuple[str, UpdaterFn]] = []
_task: asyncio.Task | None = None
_client = None


def _backoff(attempt: int) -> float:
    base = min(_BACKOFF_MAX, _BACKOFF_BASE * (2 ** attempt))
    jitter = random.uniform(-_BACKOFF_JITTER, _BACKOFF_JITTER) * base
    return max(1.0, base + jitter)


def get_tz(tz_str: str):
    try:
        return ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, Exception):
        logger.warning("Timezone '%s' not found — falling back to UTC.", tz_str)
        return timezone.utc


def _seconds_to_next_minute(tz) -> float:
    now = datetime.now(tz)
    wait = 60.0 - now.second - now.microsecond / 1_000_000
    if wait <= 0:
        wait += 60.0
    return wait


def register_updater(name: str, fn: UpdaterFn) -> None:
    """Register a profile updater.

    ``fn(owner_id, tz_str) -> dict[str, str] | None``
    Returns a dict of UpdateProfileRequest fields (e.g. ``{"about": "..."}``,
    ``{"first_name": "..."}``) or ``None`` if nothing changed.
    """
    _updaters.append((name, fn))
    logger.info("Profile updater registered: %s (total=%d)", name, len(_updaters))


def unregister_updater(name: str) -> None:
    global _updaters
    _updaters = [(n, fn) for n, fn in _updaters if n != name]


def _set_client(client) -> None:
    global _client
    _client = client


def update_client(client) -> None:
    """Swap the client reference after a rebuild — no restart needed."""
    global _client
    _client = client


async def _collect_updates(owner_id: int, tz_str: str) -> dict[str, str]:
    """Call every registered updater and merge their results."""
    merged: dict[str, str] = {}
    for name, fn in list(_updaters):
        try:
            result = await fn(owner_id, tz_str)
            if result:
                merged.update(result)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            trace_exception("PROFILE_UPDATER_ERROR", exc, updater=name)
            logger.exception("Profile updater '%s' error: %s", name, exc)
    return merged


async def _cron_loop(client, owner_id: int, tz_str: str) -> None:
    tz = get_tz(tz_str)
    trace("PROFILE_CRON_STARTED", tz=tz_str)
    logger.info("Profile scheduler started (tz=%s)", tz_str)

    while True:
        await asyncio.sleep(_seconds_to_next_minute(tz))

        try:
            from backend.health import tick_loop
            tick_loop("lifeos-profile-scheduler", state="RUNNING")
        except Exception:
            pass

        try:
            updates = await _collect_updates(owner_id, tz_str)
            if not updates:
                continue

            t0 = time.monotonic()
            try:
                await asyncio.wait_for(
                    client(UpdateProfileRequest(**updates)),
                    timeout=_API_TIMEOUT,
                )
                record_event("profile", "UpdateProfileRequest", (time.monotonic() - t0) * 1000, "SUCCESS",
                              f"fields={list(updates.keys())}")
                try:
                    from backend.health import tick_loop
                    tick_loop("lifeos-profile-scheduler", state="RUNNING", success=True)
                except Exception:
                    pass
            except asyncio.TimeoutError:
                logger.warning("Profile API call timed out (%ds) — will retry next minute", _API_TIMEOUT)
                record_event("profile", "UpdateProfileRequest", _API_TIMEOUT * 1000, "TIMEOUT")
                continue
            except FloodWaitError as fwe:
                logger.warning("Profile FloodWait %ds — sleeping.", fwe.seconds)
                record_event("profile", "UpdateProfileRequest", 0, "FLOOD_WAIT", f"{fwe.seconds}s")
                await asyncio.sleep(fwe.seconds + 1)
                continue
            except asyncio.CancelledError:
                raise
            except Exception as api_exc:
                logger.exception("Profile API error (retrying next minute): type=%s repr=%r",
                                  type(api_exc).__name__, api_exc)
                record_event("profile", "UpdateProfileRequest", 0, "ERROR", str(api_exc))
                continue

            try:
                from backend.health import set_last_bio_update
                set_last_bio_update()
            except Exception:
                pass

        except asyncio.CancelledError:
            trace("PROFILE_CRON_CANCELLED")
            logger.info("Profile scheduler cancelled.")
            raise
        except Exception as exc:
            trace_exception("PROFILE_CRON_TICK_ERROR", exc)
            logger.exception("Profile scheduler tick error (will retry next minute)")


async def _supervised_cron(client, owner_id: int, tz_str: str) -> None:
    attempt = 0
    while True:
        try:
            await _cron_loop(client, owner_id, tz_str)
            trace("PROFILE_CRON_SUPERVISOR_EXIT", reason="loop_exited_normally")
            logger.info("Profile scheduler supervisor: loop exited normally.")
            return
        except asyncio.CancelledError:
            trace("PROFILE_CRON_SUPERVISOR_CANCELLED")
            raise
        except Exception as exc:
            attempt += 1
            delay = _backoff(attempt)
            trace_exception("PROFILE_CRON_CRASHED", exc, attempt=attempt, backoff_delay=delay)
            logger.exception("Profile scheduler crashed — restarting in %.1fs: %s", delay, exc)
            await asyncio.sleep(delay)


def start_cron(client, owner_id: int, tz_str: str) -> None:
    global _task
    if _task and not _task.done():
        return
    _set_client(client)
    _task = guarded_create_task(
        _supervised_cron(client, owner_id, tz_str),
        name="lifeos-profile-scheduler",
    )
    trace("PROFILE_CRON_START_REQUESTED")
    record_event("profile", "start_cron", 0, "SUCCESS")


def update_client(client) -> None:
    """Swap the client after a rebuild without restarting the scheduler."""
    _set_client(client)


async def stop_cron() -> None:
    global _task
    if _task and not _task.done():
        trace("PROFILE_CRON_STOP_REQUESTED")
        _task.cancel()
        try:
            await asyncio.wait_for(_task, timeout=_STOP_TIMEOUT)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
    _task = None
    record_event("profile", "stop_cron", 0, "SUCCESS")


def is_running() -> bool:
    return bool(_task and not _task.done())
