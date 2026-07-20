"""
Lightweight production health monitor.

A single module-level state object tracks:
  - process liveness (heartbeat updated every few seconds)
  - Telethon connection state + last event timestamp
  - supervisor loop status
  - watchdog status + last check timestamp
  - bio cron status + last successful update timestamp
  - restart counter (Telethon reconnects)
  - process uptime

The heartbeat is updated by a background coroutine started from main.py.
If the heartbeat becomes stale (no update within the threshold), a WARNING
is logged on each check so operators can detect a stalled event loop.

All access is synchronous and lock-free — the values are simple primitives
written by one task and read by the FastAPI request handler. This keeps the
architecture deterministic and avoids any I/O in the health path.
"""
import logging
import time

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 5.0
_STALE_THRESHOLD = 15.0

_started_at: float = 0.0
_last_heartbeat: float = 0.0
_telethon_connected: bool = False
_supervisor_ok: bool = False
_bio_cron_ok: bool = False
_last_stale_warn: float = 0.0

_restart_count: int = 0
_watchdog_ok: bool = False
_last_watchdog_check: float = 0.0
_last_telethon_event: float = 0.0
_last_bio_update: float = 0.0


def mark_started() -> None:
    global _started_at, _last_heartbeat
    now = time.time()
    _started_at = now
    _last_heartbeat = now
    logger.info("health: process started at %.0f", now)


def update_heartbeat() -> None:
    global _last_heartbeat
    _last_heartbeat = time.time()


def set_telethon_connected(connected: bool) -> None:
    global _telethon_connected, _last_telethon_event
    if _telethon_connected and not connected:
        logger.warning("health: Telethon disconnected unexpectedly")
    _telethon_connected = bool(connected)
    _last_telethon_event = time.time()


def set_supervisor_ok(ok: bool) -> None:
    global _supervisor_ok
    _supervisor_ok = bool(ok)


def set_bio_cron_ok(ok: bool) -> None:
    global _bio_cron_ok
    _bio_cron_ok = bool(ok)


def set_watchdog_ok(ok: bool) -> None:
    global _watchdog_ok, _last_watchdog_check
    _watchdog_ok = bool(ok)
    _last_watchdog_check = time.time()


def increment_restart() -> None:
    global _restart_count
    _restart_count += 1


def set_last_bio_update() -> None:
    global _last_bio_update
    _last_bio_update = time.time()


def _heartbeat_age() -> float:
    if not _last_heartbeat:
        return -1.0
    return max(0.0, time.time() - _last_heartbeat)


def _uptime() -> float:
    if not _started_at:
        return -1.0
    return max(0.0, time.time() - _started_at)


def _age_or_none(ts: float) -> float | None:
    if not ts:
        return None
    return round(max(0.0, time.time() - ts), 1)


def check_stale() -> None:
    """Log a WARNING once per stale episode if the heartbeat is stale."""
    global _last_stale_warn
    age = _heartbeat_age()
    if age > _STALE_THRESHOLD:
        now = time.time()
        if now - _last_stale_warn > _STALE_THRESHOLD:
            logger.warning("health: heartbeat stale (%.1fs old)", age)
            _last_stale_warn = now


def snapshot() -> dict:
    """Return a serializable health snapshot for /health."""
    age = _heartbeat_age()
    alive = age >= 0 and age < _STALE_THRESHOLD
    check_stale()
    return {
        "status": "ok" if alive else "degraded",
        "process_alive": alive,
        "telethon_connected": _telethon_connected,
        "heartbeat_age_s": round(age, 2) if age >= 0 else None,
        "supervisor_ok": _supervisor_ok,
        "bio_cron_ok": _bio_cron_ok,
        "uptime_s": round(_uptime(), 1) if _uptime() >= 0 else None,
        "restart_count": _restart_count,
        "watchdog_ok": _watchdog_ok,
        "last_watchdog_check_s": _age_or_none(_last_watchdog_check),
        "last_telethon_event_s": _age_or_none(_last_telethon_event),
        "last_bio_update_s": _age_or_none(_last_bio_update),
    }
