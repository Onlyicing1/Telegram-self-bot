"""
Permanent runtime diagnostics reporter.

Every 60 seconds:
  1. Collects the latest 200 entries from a ring buffer that captures
     trace events, record_event calls, and Python log output.
  2. Builds a verbose TXT report with full runtime state, task dumps,
     memory/CPU, loop latency, and complete untruncated tracebacks.
  3. Uploads the report as a .txt file to Saved Messages ("me").
  4. Deletes the previous diagnostics file so exactly one exists at
     any time.

The ring buffer is fed by:
  - backend.runtime.tracer.trace / trace_exception (monkey-patched)
  - backend.diagnostics.record_event (monkey-patched)
  - A logging.Handler attached to the root logger

If Save Chat is unavailable the upload is retried next cycle.
Never blocks the bot — all I/O is wrapped with timeouts.
"""
import asyncio
import io
import logging
import os
import resource
import sys
import time
import traceback
from collections import deque
from datetime import datetime, timezone

from backend.runtime.task_guard import guarded_create_task

logger = logging.getLogger("backend.diagnostics_reporter")

_INTERVAL = 60.0
_RING_SIZE = 200
_UPLOAD_TIMEOUT = 30.0
_DELETE_TIMEOUT = 15.0
_FILENAME = "lifeos_diagnostics_report.txt"

_ring: deque = deque(maxlen=_RING_SIZE)
_task: asyncio.Task | None = None
_client = None
_started = False


# ── Ring buffer feed ──

def push(entry: dict) -> None:
    _ring.append(entry)


def _push_trace(event: str, **fields) -> None:
    ts = datetime.now(timezone.utc)
    parts = [event]
    for k, v in fields.items():
        if v is None or v == "":
            continue
        parts.append(f"{k}={v}")
    push({
        "ts": ts,
        "type": "TRACE",
        "tag": event,
        "text": " ".join(parts),
    })


def _push_trace_exception(event: str, exc: BaseException, **fields) -> None:
    ts = datetime.now(timezone.utc)
    parts = [event]
    for k, v in fields.items():
        if v is None or v == "":
            continue
        parts.append(f"{k}={v}")
    parts.append(f"exc_type={type(exc).__name__}")
    parts.append(f"exc_repr={exc!r}")
    tb_text = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
    push({
        "ts": ts,
        "type": "TRACE_EXC",
        "tag": event,
        "text": " ".join(parts),
        "traceback": tb_text,
    })


def _push_record_event(module: str, action: str, duration_ms: float, result: str, details: str | None = None) -> None:
    push({
        "ts": datetime.now(timezone.utc),
        "type": "EVENT",
        "tag": f"{module}.{action}",
        "text": f"module={module} action={action} duration={duration_ms}ms result={result}" + (f" details={details}" if details else ""),
    })


class _RingLogHandler(logging.Handler):
    def emit(self, record: logging.LogRecord) -> None:
        try:
            push({
                "ts": datetime.now(timezone.utc),
                "type": "LOG",
                "tag": record.name,
                "text": f"{record.levelname} {record.name}: {record.getMessage()}",
                "traceback": self.format(record) if record.exc_info else None,
            })
        except Exception:
            pass


_log_handler: _RingLogHandler | None = None

_original_trace = None
_original_trace_exception = None
_original_record_event = None


def _install_hooks() -> None:
    global _original_trace, _original_trace_exception, _original_record_event, _log_handler

    from backend.runtime import tracer
    from backend import diagnostics

    _original_trace = tracer.trace
    _original_trace_exception = tracer.trace_exception
    _original_record_event = diagnostics.record_event

    tracer.trace = _push_trace
    tracer.trace_exception = _push_trace_exception
    diagnostics.record_event = _push_record_event

    _log_handler = _RingLogHandler()
    _log_handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(_log_handler)


def _uninstall_hooks() -> None:
    global _original_trace, _original_trace_exception, _original_record_event, _log_handler

    from backend.runtime import tracer
    from backend import diagnostics

    if _original_trace is not None:
        tracer.trace = _original_trace
        _original_trace = None
    if _original_trace_exception is not None:
        tracer.trace_exception = _original_trace_exception
        _original_trace_exception = None
    if _original_record_event is not None:
        diagnostics.record_event = _original_record_event
        _original_record_event = None
    if _log_handler is not None:
        logging.getLogger().removeHandler(_log_handler)
        _log_handler = None


# ── Report builder ──

def _format_ts(ts: datetime) -> str:
    return ts.strftime("%Y-%m-%d %H:%M:%S.%f")[:-3] + " UTC"


def _format_entry(entry: dict) -> list[str]:
    lines = [f"[{_format_ts(entry['ts'])}] {entry['type']} {entry['tag']}"]
    lines.append(f"  {entry['text']}")
    tb = entry.get("traceback")
    if tb:
        for tb_line in tb.rstrip().splitlines():
            lines.append(f"  {tb_line}")
    return lines


def _collect_runtime_state() -> list[str]:
    from backend import health
    snap = health.snapshot()
    lines = [
        "=== RUNTIME STATE ===",
        f"Status: {snap.get('status', 'unknown')}",
        f"Runtime State: {snap.get('runtime_state', 'unknown')}",
        f"Process Alive: {snap.get('process_alive', False)}",
        f"Telethon Connected: {snap.get('telethon_connected', False)}",
        f"Helper Connected: {snap.get('helper_connected', False)}",
        f"Supervisor OK: {snap.get('supervisor_ok', False)}",
        f"Bio Cron OK: {snap.get('bio_cron_ok', False)}",
        f"Watchdog OK: {snap.get('watchdog_ok', False)}",
        f"Client Generation: {snap.get('client_generation', 0)}",
        f"Last Rebuild Reason: {snap.get('last_rebuild_reason', 'none')}",
        f"RPC Latency: {snap.get('rpc_latency_ms', 'N/A')}ms",
        f"Heartbeat Age: {snap.get('heartbeat_age_s', 'N/A')}s",
        f"Uptime: {snap.get('uptime_s', 'N/A')}s",
        f"Restart Count: {snap.get('restart_count', 0)}",
        f"Last RPC Age: {snap.get('last_rpc_s', 'N/A')}s",
        f"Last Command Age: {snap.get('last_command_s', 'N/A')}s",
        f"Last Update Age: {snap.get('last_update_s', 'N/A')}s",
        f"Last Handler Age: {snap.get('last_handler_dispatched_s', 'N/A')}s",
        f"Last Telethon Event Age: {snap.get('last_telethon_event_s', 'N/A')}s",
        f"Last Bio Update Age: {snap.get('last_bio_update_s', 'N/A')}s",
        f"Last Callback Age: {snap.get('last_callback_s', 'N/A')}s",
        f"Last Event Dispatch Age: {snap.get('last_event_dispatch_s', 'N/A')}s",
    ]
    task_states = snap.get("task_states", {})
    if task_states:
        lines.append("Task States:")
        for name, state in task_states.items():
            lines.append(f"  {name}: {state}")
    return lines


def _collect_process() -> list[str]:
    lines = ["=== PROCESS ==="]
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        mem_mb = usage.ru_maxrss / 1024
        cpu_s = usage.ru_utime + usage.ru_stime
        lines.append(f"PID: {os.getpid()}")
        lines.append(f"Memory: {mem_mb:.1f} MB (max RSS)")
        lines.append(f"CPU: {cpu_s:.2f}s user+sys")
        lines.append(f"Python: {sys.version.split()[0]}")
    except Exception:
        lines.append(f"PID: {os.getpid()}")
        lines.append("Memory/CPU: unavailable")
    return lines


def _collect_tasks() -> list[str]:
    lines = ["=== ASYNCIO TASKS ==="]
    try:
        tasks = asyncio.all_tasks()
        current = asyncio.current_task()
        pending = [t for t in tasks if t is not current and not t.done()]
        lines.append(f"Total tasks: {len(tasks)}")
        lines.append(f"Pending (excluding self): {len(pending)}")
        for t in pending:
            name = t.get_name()
            coro = t.get_coro()
            coro_name = getattr(coro, "__name__", getattr(coro, "__qualname__", "unknown"))
            state = "RUNNING"
            if t.done():
                state = "CANCELLED" if t.cancelled() else ("FAILED" if t.exception() else "DONE")
            loc = ""
            try:
                frame = coro.cr_frame if coro and hasattr(coro, "cr_frame") else None
                if frame is not None:
                    code = frame.f_code
                    loc = f" at {code.co_filename}:{frame.f_lineno}"
            except Exception:
                pass
            lines.append(f"  {name} — {state} — {coro_name}{loc}")
            if t.done() and not t.cancelled():
                exc = t.exception()
                if exc:
                    tb = "".join(traceback.format_exception(type(exc), exc, exc.__traceback__))
                    for tb_line in tb.rstrip().splitlines():
                        lines.append(f"    {tb_line}")
    except Exception as exc:
        lines.append(f"Task collection error: {exc}")
    return lines


def _collect_loop_latency() -> list[str]:
    lines = ["=== EVENT LOOP LATENCY ==="]
    try:
        t0 = time.monotonic()
        asyncio.get_running_loop().call_soon(asyncio.sleep, 0)
        latency_ms = (time.monotonic() - t0) * 1000
        lines.append(f"Current loop latency: {latency_ms:.2f}ms")
    except Exception as exc:
        lines.append(f"Loop latency error: {exc}")
    return lines


def _collect_db() -> list[str]:
    lines = ["=== DATABASE ==="]
    try:
        from backend.db import client as db_client
        lines.append(f"Available: {db_client.is_available()}")
    except Exception as exc:
        lines.append(f"Database check error: {exc}")
    return lines


def _collect_bio() -> list[str]:
    lines = ["=== BIO ENGINE ==="]
    try:
        from backend.bio import engine as bio_engine
        lines.append(f"Running: {bio_engine.is_running()}")
    except Exception as exc:
        lines.append(f"Bio check error: {exc}")
    return lines


def _collect_telethon() -> list[str]:
    lines = ["=== TELETHON ==="]
    client = _client
    if client is None:
        lines.append("Client: None")
        return lines
    try:
        connected = client.is_connected()
        lines.append(f"Connected: {connected}")
        if connected:
            lines.append(f"Authorized: {client.is_user_authorized()}")
    except Exception as exc:
        lines.append(f"Telethon check error: {exc}")
    try:
        if hasattr(client, "_updates"):
            upd = client._updates
            if hasattr(upd, "_pending"):
                lines.append(f"Update queue size: {len(upd._pending)}")
    except Exception:
        pass
    return lines


def build_report() -> str:
    now_str = _format_ts(datetime.now(timezone.utc))
    sections = [
        f"LifeOS Diagnostics Report",
        f"Generated: {now_str}",
        f"Interval: {_INTERVAL}s",
        f"Entries: {len(_ring)}/{_RING_SIZE}",
        "",
    ]
    sections.extend(_collect_process())
    sections.append("")
    sections.extend(_collect_runtime_state())
    sections.append("")
    sections.extend(_collect_telethon())
    sections.append("")
    sections.extend(_collect_db())
    sections.append("")
    sections.extend(_collect_bio())
    sections.append("")
    sections.extend(_collect_loop_latency())
    sections.append("")
    sections.extend(_collect_tasks())
    sections.append("")
    sections.append("=== RING BUFFER (latest 200, oldest first) ===")
    for entry in list(_ring):
        sections.extend(_format_entry(entry))
    return "\n".join(sections)


# ── File upload to Saved Messages ──

async def _upload_report(report_text: str) -> None:
    client = _client
    if client is None:
        logger.debug("Diagnostics upload skipped — no client")
        return
    if not client.is_connected():
        logger.debug("Diagnostics upload skipped — client not connected")
        return

    buf = io.BytesIO(report_text.encode("utf-8"))
    buf.seek(0)
    buf.name = _FILENAME

    try:
        old_msgs = await asyncio.wait_for(
            client.get_messages("me", limit=50),
            timeout=_DELETE_TIMEOUT,
        )
        for msg in old_msgs:
            if msg and msg.document and getattr(msg.document, "attributes", None):
                for attr in msg.document.attributes:
                    if hasattr(attr, "file_name") and attr.file_name == _FILENAME:
                        try:
                            await asyncio.wait_for(
                                client.delete_messages("me", [msg.id]),
                                timeout=_DELETE_TIMEOUT,
                            )
                        except Exception as exc:
                            logger.debug("Old diagnostics delete error: %s", exc)
                        break
    except asyncio.TimeoutError:
        logger.debug("Old diagnostics lookup timed out")
    except Exception as exc:
        logger.debug("Old diagnostics lookup error: %s", exc)

    try:
        await asyncio.wait_for(
            client.send_file(
                "me",
                buf,
                caption=f"LifeOS Diagnostics — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M:%S UTC')}",
                force_document=True,
            ),
            timeout=_UPLOAD_TIMEOUT,
        )
    except asyncio.TimeoutError:
        logger.warning("Diagnostics upload timed out")
    except Exception as exc:
        logger.warning("Diagnostics upload failed: %s", exc)
    finally:
        buf.close()


# ── Main loop ──

async def _reporter_loop() -> None:
    global _started
    _started = True
    logger.info("Diagnostics reporter started (interval=%ds)", int(_INTERVAL))
    while True:
        await asyncio.sleep(_INTERVAL)
        try:
            report = build_report()
            await _upload_report(report)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("Diagnostics reporter error: %s", exc)


def start(client) -> None:
    global _task, _client
    _client = client
    if not _started:
        _install_hooks()
    if _task and not _task.done():
        return
    _task = guarded_create_task(
        _reporter_loop(), name="lifeos-diagnostics-reporter"
    )


async def stop() -> None:
    global _task
    if _task and not _task.done():
        _task.cancel()
        try:
            await asyncio.wait_for(_task, timeout=5.0)
        except (asyncio.CancelledError, asyncio.TimeoutError):
            pass
    _task = None
    _uninstall_hooks()
