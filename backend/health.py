"""
Runtime health telemetry — comprehensive state tracking for the
RuntimeSupervisor FSM.

Tracks:
  - runtime_state (FSM state string)
  - heartbeat (timestamp + age)
  - last_rpc (last successful RPC call)
  - last_command (last owner command processed)
  - last_update (last Telethon update received)
  - restart_count (Telethon reconnects)
  - task_states (managed task states)
  - supervisor_ok
  - helper_connected
  - bio_cron_ok
  - client_generation
  - last_rebuild_reason
  - rpc_latency_ms

All access is synchronous and lock-free — simple primitives written by
the supervisor and read by the FastAPI request handler.
"""
import logging
import time

logger = logging.getLogger(__name__)

_HEARTBEAT_INTERVAL = 5.0
_STALE_THRESHOLD = 15.0

_started_at: float = 0.0
_last_heartbeat: float = 0.0
_last_stale_warn: float = 0.0

_runtime_state: str = "STARTING"
_telethon_connected: bool = False
_supervisor_ok: bool = False
_bio_cron_ok: bool = False
_username_cron_ok: bool = False
_helper_connected: bool = False

_restart_count: int = 0
_last_rpc: float = 0.0
_last_command: float = 0.0
_last_update: float = 0.0
_last_handler_dispatched: float = 0.0
_client_generation: int = 0
_last_rebuild_reason: str = ""
_rpc_latency_ms: float = 0.0

_last_telethon_event: float = 0.0
_last_bio_update: float = 0.0
_last_username_update: float = 0.0
_last_callback: float = 0.0
_last_event_dispatch: float = 0.0

_task_states: dict[str, str] = {}

# ── Loop progress registry ──
# Each forever-loop reports its last iteration timestamp + state.
# The supervisor reads this to detect loops that are alive (task not done)
# but not progressing (timestamp is stale).
_loop_progress: dict[str, dict] = {}  # name -> {"last_tick": float, "state": str, "last_success": float}


def tick_loop(name: str, state: str = "", success: bool = False) -> None:
    """Called by every forever-loop on each iteration."""
    now = time.time()
    entry = _loop_progress.get(name, {})
    entry["last_tick"] = now
    if state:
        entry["state"] = state
    if success:
        entry["last_success"] = now
    _loop_progress[name] = entry


def get_loop_progress(name: str) -> dict:
    return _loop_progress.get(name, {})


def get_all_loop_progress() -> dict[str, dict]:
    return dict(_loop_progress)


def get_stale_loops(threshold: float = 90.0) -> list[str]:
    """Return names of loops whose last_tick is older than threshold seconds."""
    now = time.time()
    stale = []
    for name, entry in _loop_progress.items():
        last_tick = entry.get("last_tick", 0)
        if last_tick > 0 and (now - last_tick) > threshold:
            stale.append(name)
    return stale


def mark_started() -> None:
    global _started_at, _last_heartbeat
    now = time.time()
    _started_at = now
    _last_heartbeat = now
    logger.info("health: process started at %.0f", now)


def update_heartbeat() -> None:
    global _last_heartbeat
    _last_heartbeat = time.time()


def set_heartbeat() -> None:
    global _last_heartbeat
    _last_heartbeat = time.time()


def set_runtime_state(state: str) -> None:
    global _runtime_state
    _runtime_state = state


def set_telethon_connected(connected: bool) -> None:
    global _telethon_connected
    if _telethon_connected and not connected:
        logger.warning("health: Telethon disconnected")
    _telethon_connected = bool(connected)


def set_supervisor_ok(ok: bool) -> None:
    global _supervisor_ok
    _supervisor_ok = bool(ok)


def set_bio_cron_ok(ok: bool) -> None:
    global _bio_cron_ok
    _bio_cron_ok = bool(ok)


def set_username_cron_ok(ok: bool) -> None:
    global _username_cron_ok
    _username_cron_ok = bool(ok)


def set_helper_connected(connected: bool) -> None:
    global _helper_connected
    _helper_connected = bool(connected)


def set_last_rpc() -> None:
    global _last_rpc
    _last_rpc = time.time()


def set_last_command() -> None:
    global _last_command
    _last_command = time.time()


def set_last_update() -> None:
    global _last_update
    _last_update = time.time()


def set_last_handler_dispatched() -> None:
    global _last_handler_dispatched
    _last_handler_dispatched = time.time()


def set_restart_count(count: int) -> None:
    global _restart_count
    _restart_count = count


def increment_restart() -> None:
    global _restart_count
    _restart_count += 1


def set_client_generation(gen: int) -> None:
    global _client_generation
    _client_generation = gen


def set_last_rebuild_reason(reason: str) -> None:
    global _last_rebuild_reason
    _last_rebuild_reason = reason


def set_rpc_latency(ms: float) -> None:
    global _rpc_latency_ms
    _rpc_latency_ms = round(ms, 1)


def set_last_bio_update() -> None:
    global _last_bio_update
    _last_bio_update = time.time()


def set_last_username_update() -> None:
    global _last_username_update
    _last_username_update = time.time()


def set_last_telethon_event() -> None:
    global _last_telethon_event
    _last_telethon_event = time.time()


def set_last_callback() -> None:
    global _last_callback
    _last_callback = time.time()


def set_last_event_dispatch() -> None:
    global _last_event_dispatch
    _last_event_dispatch = time.time()


def set_task_state(name: str, state: str) -> None:
    _task_states[name] = state


def set_task_states(states: dict[str, str]) -> None:
    global _task_states
    _task_states = dict(states)


def get_last_command() -> float:
    return _last_command


def get_last_update() -> float:
    return _last_update


def get_last_telethon_event() -> float:
    return _last_telethon_event


def get_last_handler_dispatched() -> float:
    return _last_handler_dispatched


def get_last_callback() -> float:
    return _last_callback


def get_last_event_dispatch() -> float:
    return _last_event_dispatch


def get_last_rpc() -> float:
    return _last_rpc


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
    global _last_stale_warn
    age = _heartbeat_age()
    if age > _STALE_THRESHOLD:
        now = time.time()
        if now - _last_stale_warn > _STALE_THRESHOLD:
            logger.warning("health: heartbeat stale (%.1fs old)", age)
            _last_stale_warn = now


def snapshot() -> dict:
    age = _heartbeat_age()
    alive = age >= 0 and age < _STALE_THRESHOLD
    check_stale()
    status = "ok"
    if not alive:
        status = "degraded"
    if _runtime_state in ("FAILED", "STOPPING"):
        status = "down"

    return {
        "status": status,
        "runtime_state": _runtime_state,
        "process_alive": alive,
        "telethon_connected": _telethon_connected,
        "supervisor_ok": _supervisor_ok,
        "helper_connected": _helper_connected,
        "bio_cron_ok": _bio_cron_ok,
        "username_cron_ok": _username_cron_ok,
        "heartbeat_age_s": round(age, 2) if age >= 0 else None,
        "uptime_s": round(_uptime(), 1) if _uptime() >= 0 else None,
        "restart_count": _restart_count,
        "client_generation": _client_generation,
        "last_rebuild_reason": _last_rebuild_reason or None,
        "last_rpc_s": _age_or_none(_last_rpc),
        "last_command_s": _age_or_none(_last_command),
        "last_update_s": _age_or_none(_last_update),
        "last_handler_dispatched_s": _age_or_none(_last_handler_dispatched),
        "rpc_latency_ms": _rpc_latency_ms if _rpc_latency_ms else None,
        "task_states": dict(_task_states),
        "last_telethon_event_s": _age_or_none(_last_telethon_event),
        "last_bio_update_s": _age_or_none(_last_bio_update),
        "last_username_update_s": _age_or_none(_last_username_update),
        "last_callback_s": _age_or_none(_last_callback),
        "last_event_dispatch_s": _age_or_none(_last_event_dispatch),
    }
