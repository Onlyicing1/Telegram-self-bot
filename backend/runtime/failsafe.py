"""
Last-line failsafe monitor — independent of all normal runtime logic.

Every 15 seconds it checks four independent signals:
  1. Event loop is progressing (asyncio.sleep returns on time)
  2. Heartbeat timestamp changes
  3. Last Telegram update timestamp changes
  4. Last RPC timestamp changes

If ALL four stay frozen for longer than 120 seconds, the failsafe
triggers a HARD client reset via the supervisor's _hard_reset_runtime()
method. This bypasses the normal supervisor/heartbeat/keepalive chain
entirely — those may all be stuck.

Design guarantees:
  - Does NOT depend on supervisor, heartbeat, or keepalive tasks.
  - Uses its own asyncio task (immortal — never dies from exceptions).
  - Every check is bounded — no unbounded await.
  - Calls supervisor._hard_reset_runtime() which has its own timeouts.
  - Respects the recovery cooldown (180s) to prevent recovery loops.
  - Never calls sys.exit / os.exit / quit.
"""
import asyncio
import logging
import time

from backend.runtime.tracer import trace
from backend.runtime.task_guard import immortal_create_task, guarded_create_task
from backend.health import (
    get_last_telethon_event,
    get_last_rpc,
    get_last_event_dispatch,
)

logger = logging.getLogger("backend.failsafe")

_CHECK_INTERVAL = 15.0
_FREEZE_THRESHOLD = 120.0

_task: asyncio.Task | None = None
_supervisor_ref = None

_prev_heartbeat: float = 0.0
_prev_update: float = 0.0
_prev_rpc: float = 0.0
_prev_dispatch: float = 0.0
_frozen_since: float = 0.0


def configure(supervisor) -> None:
    global _supervisor_ref
    _supervisor_ref = supervisor


def _get_heartbeat_ts() -> float:
    try:
        import backend.health as _h
        # Compute the actual heartbeat timestamp from age
        age = _h._heartbeat_age()
        if age < 0:
            return 0.0
        return time.time() - age
    except Exception:
        return 0.0


def _all_frozen() -> bool:
    """Check if all four signals are frozen."""
    hb = _get_heartbeat_ts()
    upd = get_last_telethon_event()
    rpc = get_last_rpc()
    dsp = get_last_event_dispatch()

    global _prev_heartbeat, _prev_update, _prev_rpc, _prev_dispatch

    hb_changed = hb != _prev_heartbeat
    upd_changed = upd != _prev_update
    rpc_changed = rpc != _prev_rpc
    dsp_changed = dsp != _prev_dispatch

    _prev_heartbeat = hb
    _prev_update = upd
    _prev_rpc = rpc
    _prev_dispatch = dsp

    # All must be non-zero (initialized) AND none changed
    all_initialized = hb > 0 and upd > 0 and rpc > 0 and dsp > 0
    none_changed = not (hb_changed or upd_changed or rpc_changed or dsp_changed)

    return all_initialized and none_changed


async def _failsafe_loop() -> None:
    global _frozen_since

    logger.info("Failsafe monitor started (interval=%ds, threshold=%ds)",
                int(_CHECK_INTERVAL), int(_FREEZE_THRESHOLD))

    # Give the runtime time to boot before checking
    await asyncio.sleep(30.0)

    while True:
        t0 = time.monotonic()
        await asyncio.sleep(_CHECK_INTERVAL)
        loop_latency = (time.monotonic() - t0 - _CHECK_INTERVAL) * 1000

        # If the loop itself took way too long, the event loop was blocked
        if loop_latency > 5000:
            trace("FAILSAFE_LOOP_STARVATION", latency_ms=f"{loop_latency:.1f}")
            logger.error(
                "FAILSAFE_LOOP_STARVATION — loop latency %.1fms. Event loop blocked.",
                loop_latency,
            )

        sup = _supervisor_ref
        if sup is None:
            continue

        if sup.shutdown_event.is_set():
            return

        # Skip if recovery is already in progress or in cooldown
        try:
            if sup._recovery_lock.locked():
                _frozen_since = 0.0
                continue
            if sup._in_recovery_cooldown():
                _frozen_since = 0.0
                continue
        except Exception:
            pass

        frozen = _all_frozen()

        if frozen:
            if _frozen_since == 0.0:
                _frozen_since = time.time()
                trace("FAILSAFE_FREEZE_DETECTED")
                logger.warning(
                    "FAILSAFE_FREEZE_DETECTED — all signals frozen, "
                    "waiting %ds before hard reset",
                    int(_FREEZE_THRESHOLD),
                )
            else:
                frozen_duration = time.time() - _frozen_since
                if frozen_duration >= _FREEZE_THRESHOLD:
                    trace("FAILSAFE_HARD_RESET", frozen_duration=f"{frozen_duration:.0f}s")
                    logger.error(
                        "FAILSAFE_HARD_RESET — all signals frozen for %.0fs "
                        "(threshold=%ds). Triggering hard client reset.",
                        frozen_duration, int(_FREEZE_THRESHOLD),
                    )
                    _frozen_since = 0.0
                    try:
                        guarded_create_task(
                            sup._hard_reset_runtime(),
                            name="lifeos-failsafe-reset",
                        )
                    except Exception as exc:
                        logger.error("FAILSAFE: failed to trigger hard reset: %s", exc)
        else:
            if _frozen_since > 0:
                trace("FAILSAFE_FREEZE_RECOVERED")
                logger.info("FAILSAFE_FREEZE_RECOVERED — signals resumed")
            _frozen_since = 0.0


def start_failsafe() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = immortal_create_task(_failsafe_loop, name="lifeos-failsafe")


async def stop_failsafe() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await asyncio.wait_for(_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    _task = None
