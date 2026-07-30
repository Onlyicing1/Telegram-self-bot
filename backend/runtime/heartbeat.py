"""
Runtime heartbeat — comprehensive system snapshot every 30 seconds.

Logs a single structured line containing:
  - Runtime State
  - Self client connected status
  - Helper client connected status
  - Pending asyncio task count
  - Event loop latency (measured via sleep drift)
  - Memory (RSS)
  - Client generation
  - Last RPC latency
  - Active Telethon update queue size (if available)
  - Number of registered handlers
  - last_update_age — age of last incoming Telegram update
  - last_event_age — age of last dispatched event
  - last_rpc_age — age of last successful RPC
  - last_command_age — age of last handled owner command
  - last_callback_age — age of last callback query

Stall detection:
  - If updates stop arriving while RPC is still healthy → UPDATE_PIPELINE_STALLED
  - If dispatcher stops processing updates → EVENT_DISPATCH_STALLED

The heartbeat is separate from the watchdog RPC check — it runs on its own
timer and never blocks or interferes with recovery.
"""
import asyncio
import logging
import resource
import time

from backend.runtime.tracer import trace
from backend.runtime.task_guard import guarded_create_task

logger = logging.getLogger("backend.heartbeat")

_INTERVAL = 30.0
_STALL_THRESHOLD = 120.0
_task: asyncio.Task | None = None

_state_ref: dict = {
    "runtime_state": "STARTING",
    "self_connected": False,
    "helper_connected": False,
    "client_generation": 0,
    "rpc_latency_ms": 0.0,
    "update_queue_size": -1,
    "registered_handlers": -1,
}


def update_state(**kwargs) -> None:
    _state_ref.update(kwargs)


def _count_handlers(client) -> int:
    """Count registered event handlers on a Telethon client."""
    try:
        if client is None:
            return 0
        handlers = client.list_event_handlers()
        return len(handlers)
    except Exception:
        return -1


def _get_update_queue_size(client) -> int:
    """Get the Telethon update queue size if available."""
    try:
        if client is None:
            return -1
        if hasattr(client, "_updates") and hasattr(client._updates, "_pending"):
            return len(client._updates._pending)
        if hasattr(client, "updates") and hasattr(client.updates, "_pending"):
            return len(client.updates._pending)
        return -1
    except Exception:
        return -1


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

        client = _state_ref.get("_client_ref")
        handler_count = _count_handlers(client) if client else _state_ref.get("registered_handlers", -1)
        queue_size = _get_update_queue_size(client) if client else _state_ref.get("update_queue_size", -1)

        # ── Age tracking ──
        from backend.health import (
            get_last_telethon_event,
            get_last_event_dispatch,
            get_last_rpc,
            get_last_command,
            get_last_callback,
        )

        now = time.time()
        last_update = get_last_telethon_event()
        last_event = get_last_event_dispatch()
        last_rpc = get_last_rpc()
        last_command = get_last_command()
        last_callback = get_last_callback()

        last_update_age = f"{now - last_update:.0f}s" if last_update > 0 else "never"
        last_event_age = f"{now - last_event:.0f}s" if last_event > 0 else "never"
        last_rpc_age = f"{now - last_rpc:.0f}s" if last_rpc > 0 else "never"
        last_command_age = f"{now - last_command:.0f}s" if last_command > 0 else "never"
        last_callback_age = f"{now - last_callback:.0f}s" if last_callback > 0 else "never"

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
            update_queue_size=queue_size,
            registered_handlers=handler_count,
            last_update_age=last_update_age,
            last_event_age=last_event_age,
            last_rpc_age=last_rpc_age,
            last_command_age=last_command_age,
            last_callback_age=last_callback_age,
        )

        # ── Stall detection (trace only, never reconnect) ──
        rpc_healthy = last_rpc > 0 and (now - last_rpc) < _INTERVAL * 2

        if last_update > 0 and (now - last_update) > _STALL_THRESHOLD:
            if rpc_healthy:
                trace(
                    "UPDATE_PIPELINE_STALLED",
                    last_update_age=last_update_age,
                    last_rpc_age=last_rpc_age,
                    threshold=f"{_STALL_THRESHOLD:.0f}s",
                    gen=_state_ref.get("client_generation", 0),
                )
                logger.warning(
                    "UPDATE_PIPELINE_STALLED — no updates for %s but RPC is healthy "
                    "(last_rpc_age=%s, gen=%d)",
                    last_update_age, last_rpc_age,
                    _state_ref.get("client_generation", 0),
                )

        if last_update > 0 and last_event > 0 and (now - last_event) > _STALL_THRESHOLD:
            if (now - last_update) < _STALL_THRESHOLD:
                trace(
                    "EVENT_DISPATCH_STALLED",
                    last_update_age=last_update_age,
                    last_event_age=last_event_age,
                    threshold=f"{_STALL_THRESHOLD:.0f}s",
                    gen=_state_ref.get("client_generation", 0),
                )
                logger.warning(
                    "EVENT_DISPATCH_STALLED — updates arriving (%s) but no event "
                    "dispatched for %s (gen=%d)",
                    last_update_age, last_event_age,
                    _state_ref.get("client_generation", 0),
                )


def start_heartbeat() -> None:
    global _task
    if _task and not _task.done():
        return
    _task = guarded_create_task(_heartbeat_loop(), name="lifeos-heartbeat")


async def stop_heartbeat() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await asyncio.wait_for(_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    _task = None
