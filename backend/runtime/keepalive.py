"""
RPC Keepalive — lightweight health probe every 60 seconds.

Executes a tiny RPC (get_me) with a bounded timeout. If the RPC times out
or fails, the keepalive triggers a reconnect via the supervisor's
recovery mechanism. It never blocks the event loop indefinitely.

Loop instrumentation:
  - Reports progress via tick_loop("lifeos-keepalive")
  - Wrapped in immortal_create_task so it never dies from an exception
"""
import asyncio
import logging
import time

from backend.runtime.tracer import trace, trace_exception
from backend.runtime.task_guard import immortal_create_task
from backend.health import set_last_rpc, set_rpc_latency, tick_loop

logger = logging.getLogger(__name__)

_INTERVAL = 60.0
_RPC_TIMEOUT = 15.0
_task: asyncio.Task | None = None
_supervisor_ref = None


def configure(supervisor) -> None:
    global _supervisor_ref
    _supervisor_ref = supervisor


async def _keepalive_loop() -> None:
    logger.info("RPC keepalive started (interval=%ds)", int(_INTERVAL))
    while True:
        await asyncio.sleep(_INTERVAL)

        sup = _supervisor_ref
        if sup is None or sup.shutdown_event.is_set():
            return

        tick_loop("lifeos-keepalive", state="RUNNING")

        client = sup.client
        if client is None or not sup._client_alive:
            continue

        if sup._recovery_lock.locked():
            continue

        t0 = time.monotonic()
        try:
            await asyncio.wait_for(client.get_me(), timeout=_RPC_TIMEOUT)
            latency_ms = (time.monotonic() - t0) * 1000
            set_last_rpc()
            set_rpc_latency(latency_ms)
            tick_loop("lifeos-keepalive", state="RUNNING", success=True)
            trace("KEEPALIVE_OK", latency_ms=f"{latency_ms:.1f}", gen=sup.client_generation)
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            trace("KEEPALIVE_TIMEOUT", gen=sup.client_generation)
            logger.warning("KEEPALIVE_TIMEOUT — RPC timed out after %ds (gen=%d)", _RPC_TIMEOUT, sup.client_generation)
            sup._consecutive_failures += 1
            await sup._trigger_reconnect()
        except Exception as exc:
            trace_exception("KEEPALIVE_FAILED", exc, gen=sup.client_generation)
            logger.warning("KEEPALIVE_FAILED — %s (gen=%d)", type(exc).__name__, sup.client_generation)
            sup._consecutive_failures += 1
            await sup._trigger_reconnect()


def start_keepalive() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = immortal_create_task(_keepalive_loop(), name="lifeos-keepalive")


async def stop_keepalive() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await asyncio.wait_for(_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    _task = None
