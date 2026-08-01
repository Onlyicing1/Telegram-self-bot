"""
Shared Profile Scheduler — the single authority for Telegram profile updates.

Guarantees:
- Fires exactly at HH:MM:00 by sleeping to the next minute boundary.
- One tick per minute, maximum. Never updates twice in the same minute.
- If both Bio and Username changed, they update together in ONE batch.
- Only the scheduler talks to Telegram. Engines register renderers.
- Deduplicates: skips fields whose rendered value hasn't changed.
- FloodWaitError caught and slept precisely; all other errors logged.
- API calls have a 30s timeout.
- The loop is supervised: restarts with backoff if it crashes.
- stop() is deterministic: cancels and awaits the task.
"""
import asyncio
import logging
import random
import time
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telethon.errors import FloodWaitError
from telethon.tl.functions.account import UpdateProfileRequest

from backend.diagnostics import record_event
from backend.runtime.tracer import trace, trace_exception
from backend.runtime.task_guard import guarded_create_task

logger = logging.getLogger(__name__)

_task: asyncio.Task | None = None
_client = None
_tz_str: str = "UTC"

_API_TIMEOUT = 30
_BACKOFF_BASE = 2.0
_BACKOFF_MAX = 60.0
_BACKOFF_JITTER = 0.3
_STOP_TIMEOUT = 10.0

_engines: dict[str, dict] = {}
_last_values: dict[str, str] = {}
_last_update_ts: float = 0.0
_pending_hint: bool = False


def _backoff(attempt: int) -> float:
    base = min(_BACKOFF_MAX, _BACKOFF_BASE * (2 ** attempt))
    jitter = random.uniform(-_BACKOFF_JITTER, _BACKOFF_JITTER) * base
    return max(1.0, base + jitter)


def _get_tz(tz_str: str):
    try:
        return ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, Exception):
        return timezone.utc


def register_engine(name: str, field: str, render_fn) -> None:
    _engines[name] = {"field": field, "render_fn": render_fn}
    trace("PROFILE_SCHEDULER_REGISTER", engine=name, field=field)


def unregister_engine(name: str) -> None:
    _engines.pop(name, None)
    _last_values.pop(name, None)
    trace("PROFILE_SCHEDULER_UNREGISTER", engine=name)


def get_last_value(engine_name: str) -> str:
    return _last_values.get(engine_name, "")


def get_last_update_ts() -> float:
    return _last_update_ts


def get_pending_info() -> dict[str, dict]:
    result = {}
    for name, info in _engines.items():
        result[name] = {
            "field": info["field"],
            "last_value": _last_values.get(name, ""),
        }
    return result


def _seconds_to_next_minute(tz) -> float:
    now = datetime.now(tz)
    wait = 60.0 - now.second - now.microsecond / 1_000_000
    if wait <= 0:
        wait += 60.0
    return wait


async def _scheduler_loop() -> None:
    tz = _get_tz(_tz_str)
    trace("PROFILE_SCHEDULER_STARTED", tz=_tz_str)
    logger.info("Profile scheduler started (tz=%s)", _tz_str)

    while True:
        await asyncio.sleep(_seconds_to_next_minute(tz))

        if not _engines:
            continue

        batch_kwargs: dict = {}
        batch_updates: dict[str, str] = {}

        for name, info in _engines.items():
            try:
                value = await info["render_fn"]()
            except Exception as exc:
                trace_exception("PROFILE_SCHEDULER_RENDER_ERROR", exc, engine=name)
                logger.warning("Profile scheduler render error (%s): %s", name, exc)
                continue

            if value is None:
                continue

            last = _last_values.get(name, "")
            if value == last:
                continue

            batch_kwargs[info["field"]] = value
            batch_updates[name] = value

        if not batch_kwargs:
            continue

        t0 = time.monotonic()
        try:
            await asyncio.wait_for(
                _client(UpdateProfileRequest(**batch_kwargs)),
                timeout=_API_TIMEOUT,
            )
            latency = (time.monotonic() - t0) * 1000
            record_event("profile_scheduler", "UpdateProfileRequest", latency, "SUCCESS",
                         f"fields={list(batch_kwargs.keys())}")

            for name, value in batch_updates.items():
                _last_values[name] = value

            global _last_update_ts
            _last_update_ts = time.time()

            try:
                from backend.health import set_last_bio_update
                set_last_bio_update()
            except Exception:
                pass

        except asyncio.TimeoutError:
            logger.warning("Profile scheduler API call timed out (%ds)", _API_TIMEOUT)
            record_event("profile_scheduler", "UpdateProfileRequest", _API_TIMEOUT * 1000, "TIMEOUT")
        except FloodWaitError as fwe:
            logger.warning("Profile scheduler FloodWait %ds — sleeping.", fwe.seconds)
            record_event("profile_scheduler", "UpdateProfileRequest", 0, "FLOOD_WAIT", f"{fwe.seconds}s")
            await asyncio.sleep(fwe.seconds + 1)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            trace_exception("PROFILE_SCHEDULER_API_ERROR", exc)
            logger.exception("Profile scheduler API error: %s", exc)
            record_event("profile_scheduler", "UpdateProfileRequest", 0, "ERROR", str(exc))


async def _supervised_loop() -> None:
    attempt = 0
    while True:
        try:
            await _scheduler_loop()
            trace("PROFILE_SCHEDULER_EXIT", reason="loop_exited_normally")
            return
        except asyncio.CancelledError:
            trace("PROFILE_SCHEDULER_CANCELLED")
            raise
        except Exception as exc:
            attempt += 1
            delay = _backoff(attempt)
            trace_exception("PROFILE_SCHEDULER_CRASHED", exc, attempt=attempt, backoff_delay=delay)
            logger.exception("Profile scheduler crashed — restarting in %.1fs", delay)
            await asyncio.sleep(delay)


def start(client, tz_str: str) -> None:
    global _task, _client, _tz_str
    _client = client
    _tz_str = tz_str
    if _task and not _task.done():
        return
    _task = guarded_create_task(_supervised_loop(), name="lifeos-profile-scheduler")
    trace("PROFILE_SCHEDULER_START_REQUESTED")
    record_event("profile_scheduler", "start", 0, "SUCCESS")


async def stop() -> None:
    global _task
    if _task and not _task.done():
        trace("PROFILE_SCHEDULER_STOP_REQUESTED")
        _task.cancel()
        try:
            await asyncio.wait_for(_task, timeout=_STOP_TIMEOUT)
        except (asyncio.CancelledError, asyncio.TimeoutError, Exception):
            pass
    _task = None
    record_event("profile_scheduler", "stop", 0, "SUCCESS")


def is_running() -> bool:
    return bool(_task and not _task.done())
