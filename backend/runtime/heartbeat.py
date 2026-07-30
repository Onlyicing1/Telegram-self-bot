"""
Runtime heartbeat — comprehensive system snapshot every 30 seconds.

Logs a single structured line containing:
  - Self client connected status
  - Helper client connected status
  - Pending asyncio task count
  - Event loop latency (measured via sleep drift)
  - Memory (RSS)
  - Current runtime state
  - Client generation
  - Last RPC latency

The heartbeat is separate from the watchdog RPC check — it runs on its own
timer and never blocks or interferes with recovery.
"""
import asyncio
import logging
import resource
import time

from backend.runtime.tracer import trace

logger = logging.getLogger("backend.heartbeat")

_INTERVAL = 30.0
_task: asyncio.Task | None = None

_state_ref: dict = {
    "runtime_state": "STARTING",
    "self_connected": False,
    "helper_connected": False,
    "client_generation": 0,
    "rpc_latency_ms": 0.0,
}


def update_state(**kwargs) -> None:
    _state_ref.update(kwargs)


async def _heartbeat_loop() -> None:
    logger.info("Runtime heartbeat started (interval=%ds)", int(_INTERVAL))
    while True:
        t0 = time.monotonic()
        await asyncio.sleep(_INTERVAL)
        loop_latency = (time.monotonic() - t0 - _INTERVAL) * 1000

        try:
            tasks = asyncio.all_tasks()
            pending = sum(1 for t in tasks if not t.done())
        except Exception:
            pending = -1

        try:
            usage = resource.getrusage(resource.RUSAGE_SELF)
            mem_mb = usage.ru_maxrss / 1024
        except Exception:
            mem_mb = -1

        trace(
            "RUNTIME_HEARTBEAT",
            self_connected=_state_ref.get("self_connected", False),
            helper_connected=_state_ref.get("helper_connected", False),
            pending_tasks=pending,
            loop_latency_ms=f"{loop_latency:.1f}",
            memory_mb=f"{mem_mb:.1f}",
            runtime_state=_state_ref.get("runtime_state", "unknown"),
            client_gen=_state_ref.get("client_generation", 0),
            rpc_latency_ms=f"{_state_ref.get('rpc_latency_ms', 0):.1f}",
        )


def start_heartbeat() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = asyncio.create_task(_heartbeat_loop(), name="lifeos-heartbeat")


async def stop_heartbeat() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await asyncio.wait_for(_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    _task = None
