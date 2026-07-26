"""
Callback trace logger — instruments every callback with a unique trace ID.

Every step of the callback pipeline is logged and forwarded to
Saved Messages of the OWNER, exactly like the old trace_collector.

Only callback traces. Nothing else.
"""
import asyncio
import logging
import traceback as tb_mod

logger = logging.getLogger(__name__)

_counter = 0
_traces: dict[str, list[str]] = {}
_self_client = None
_owner_id: int = 0
_flush_lock = asyncio.Lock()


def configure(self_client, owner_id: int) -> None:
    global _self_client, _owner_id
    _self_client = self_client
    _owner_id = owner_id


def next_trace_id() -> str:
    global _counter
    _counter += 1
    return f"CALLBACK-{_counter:06d}"


def start_trace(trace_id: str) -> None:
    _traces[trace_id] = [
        "============================",
        f"{trace_id}",
    ]


def step(trace_id: str, step_num: int, message: str, ok: bool = True) -> None:
    if trace_id not in _traces:
        start_trace(trace_id)
    status = "OK" if ok else "FAIL"
    _traces[trace_id].append(f"Step {step_num} [{status}] {message}")


def fail(trace_id: str, reason: str) -> None:
    if trace_id not in _traces:
        start_trace(trace_id)
    _traces[trace_id].append("FAILED")
    _traces[trace_id].append("Reason:")
    _traces[trace_id].append(reason)


def log_exception(trace_id: str, exc: Exception) -> None:
    tb_lines = tb_mod.format_exception(type(exc), exc, exc.__traceback__)
    fail(trace_id, "".join(tb_lines))


def finish_trace(trace_id: str) -> None:
    if trace_id not in _traces:
        return
    _traces[trace_id].append("============================")
    text = "\n".join(_traces[trace_id])
    del _traces[trace_id]
    logger.info("CALLBACK TRACE:\n%s", text)
    if _self_client is not None:
        asyncio.create_task(_send_to_saved(text))


async def _send_to_saved(text: str) -> None:
    try:
        await _self_client.send_message("me", text)
    except Exception as exc:
        logger.warning("callback_trace: failed to send to Saved Messages: %s", exc)
