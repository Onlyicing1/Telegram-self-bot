"""
RPC Keepalive — lightweight health probe every 60 seconds.

Executes a tiny RPC (get_me) with a bounded timeout. Genuine transport
failures — a timeout, a connection error, a broken session — still trigger a
reconnect through the supervisor's recovery mechanism. A Telegram
``FloodWaitError`` is NOT a broken connection: it is a server-imposed rate
limit, so the keepalive records a bounded monotonic deadline and simply skips
probing until it elapses. No reconnect, no retry storm, and no sleep of the
event loop for the duration of the wait.

Loop instrumentation:
  - Reports progress via tick_loop("lifeos-keepalive")
  - Wrapped in immortal_create_task so it never dies from an exception
"""
import asyncio
import logging
import time

from telethon.errors import FloodWaitError

from backend.runtime.tracer import trace, trace_exception
from backend.runtime.task_guard import immortal_create_task, guarded_create_task
from backend.health import set_last_rpc, set_rpc_latency, tick_loop

logger = logging.getLogger(__name__)

_INTERVAL = 60.0
_RPC_TIMEOUT = 15.0
#: One FloodWait is remembered for at most an hour. A longer server wait is
#: re-probed hourly instead of silencing keepalive for days, and the deadline
#: is monotonic so a wall-clock change can neither shorten nor extend it.
_FLOODWAIT_MAX_S = 3600.0

_floodwait_until = 0.0
_supervisor_ref = None
_task: asyncio.Task | None = None


def configure(supervisor) -> None:
    global _supervisor_ref
    _supervisor_ref = supervisor


def floodwait_remaining(now: float | None = None) -> float:
    """Seconds left before the next probe is allowed (0 = allowed now)."""
    current = time.monotonic() if now is None else now
    return max(0.0, _floodwait_until - current)


def _bounded_floodwait_seconds(exc: FloodWaitError) -> int:
    try:
        seconds = int(getattr(exc, "seconds", 0) or 0)
    except (TypeError, ValueError):
        seconds = 0
    return max(1, min(seconds or 1, int(_FLOODWAIT_MAX_S)))


async def _probe(sup) -> str:
    """One keepalive probe. Returns its outcome; never raises, but cancellation.

    Outcomes: ``ok``, ``timeout``, ``reconnect``, ``floodwait``,
    ``floodwait_skip``, ``skipped``. The split matters: only the first four
    describe the connection, and only timeout/exception may reconnect.
    """
    global _floodwait_until

    remaining = floodwait_remaining()
    if remaining > 0:
        # The server already told us to wait: do not spend another get_me.
        trace("KEEPALIVE_FLOODWAIT_SKIP", remaining=f"{remaining:.0f}s", gen=sup.client_generation)
        logger.debug("KEEPALIVE_FLOODWAIT_SKIP — %.0fs remaining", remaining)
        return "floodwait_skip"

    client = sup.client
    if client is None or not sup._client_alive:
        return "skipped"
    if sup._recovery_lock.locked():
        return "skipped"

    t0 = time.monotonic()
    try:
        await asyncio.wait_for(client.get_me(), timeout=_RPC_TIMEOUT)
    except asyncio.CancelledError:
        raise
    except FloodWaitError as exc:
        wait_s = _bounded_floodwait_seconds(exc)
        _floodwait_until = max(_floodwait_until, time.monotonic() + wait_s)
        trace("KEEPALIVE_FLOODWAIT", wait_s=wait_s, gen=sup.client_generation)
        logger.warning(
            "KEEPALIVE_FLOODWAIT — Telegram asked to wait %ds; keeping the "
            "connection, NOT reconnecting (gen=%d)",
            wait_s, sup.client_generation,
        )
        return "floodwait"
    except asyncio.TimeoutError:
        trace("KEEPALIVE_TIMEOUT", gen=sup.client_generation)
        logger.warning("KEEPALIVE_TIMEOUT — RPC timed out after %ds (gen=%d)", _RPC_TIMEOUT, sup.client_generation)
        if not sup._recovery_lock.locked():
            guarded_create_task(sup._trigger_reconnect(), name="lifeos-keepalive-recovery")
        return "timeout"
    except Exception as exc:
        trace_exception("KEEPALIVE_FAILED", exc, gen=sup.client_generation)
        logger.warning("KEEPALIVE_FAILED — %s (gen=%d)", type(exc).__name__, sup.client_generation)
        if not sup._recovery_lock.locked():
            guarded_create_task(sup._trigger_reconnect(), name="lifeos-keepalive-recovery")
        return "reconnect"

    latency_ms = (time.monotonic() - t0) * 1000
    set_last_rpc()
    set_rpc_latency(latency_ms)
    tick_loop("lifeos-keepalive", state="RUNNING", success=True)
    trace("KEEPALIVE_OK", latency_ms=f"{latency_ms:.1f}", gen=sup.client_generation)
    return "ok"


async def _keepalive_loop() -> None:
    logger.info("RPC keepalive started (interval=%ds)", int(_INTERVAL))
    while True:
        await asyncio.sleep(_INTERVAL)

        sup = _supervisor_ref
        if sup is None or sup.shutdown_event.is_set():
            return

        tick_loop("lifeos-keepalive", state="RUNNING")
        await _probe(sup)


def start_keepalive() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = immortal_create_task(_keepalive_loop, name="lifeos-keepalive")


async def stop_keepalive() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await asyncio.wait_for(_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    _task = None
