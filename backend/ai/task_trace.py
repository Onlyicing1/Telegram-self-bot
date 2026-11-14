"""Correlated trace helpers for task creation and scheduled occurrences.

Three independent context variables keep unrelated traffic silent:

* ``_binding`` is the existing create_task request binding (``AI_TASK_TRACE``).
  It is emitted only while ``bind_request`` is active, so provider/repository
  layers shared with ordinary chat never emit create-task stages there.
* ``_occurrence_binding`` is the durable scheduled-occurrence binding
  (``SCHEDULED_OCCURRENCE_TRACE``). Its trace id is the scheduler occurrence key
  and it carries the task id and current attempt, so scheduler, coordinator,
  tool, service, provider and persistence stages share one correlation id.
  It is emitted only inside ``occurrence_scope``; ordinary Telegram/AI
  requests never emit these stages.
* ``_history_creation_binding`` is a short-lived owner-creation scope
  (``HISTORY_SCHEDULE_TRACE``). Creation has no durable occurrence yet, so it
  gets its own bounded trace id and is restored before the callback returns.

Sanitization: callers pass bounded operational values (ids, statuses, names,
counts, exception classes, stable categories) — never message text, summaries,
instructions, prompts, provider payloads or credentials. ``bound_text``
flattens control characters and truncates with an explicit marker so every
line is single-line and bounded.
"""
from __future__ import annotations

import contextlib
import contextvars
import logging
import uuid
from typing import Any, Iterator

logger = logging.getLogger(__name__)

TRACE_PREFIX = "AI_TASK_TRACE"
OCCURRENCE_TRACE_PREFIX = "SCHEDULED_OCCURRENCE_TRACE"
HISTORY_CREATION_TRACE_PREFIX = "HISTORY_SCHEDULE_TRACE"
_DEFAULT_BOUND_CHARS = 240
_OCCURRENCE_FIELD_CHARS = 160

_binding: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "create_task_trace_binding", default=None
)
_occurrence_binding: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "scheduled_occurrence_trace_binding", default=None
)
_history_creation_binding: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "history_creation_trace_binding", default=None
)


def bind_request(request_id: str, **context: Any) -> contextvars.Token:
    """Bind the correlation id (plus small scalars like owner/chat ids).

    ``_owns_exit=True`` marks the caller as the terminal-exit owner (the
    create_task tool); nested layers then never emit the ``exit`` line.
    """
    owns_exit = bool(context.pop("_owns_exit", False))
    return _binding.set(
        {
            "request_id": str(request_id or "-"),
            "context": dict(context),
            "last_stage": "",
            "last_failure": {},
            "owns_exit": owns_exit,
        }
    )


def unbind(token: contextvars.Token) -> None:
    _binding.reset(token)


def bound_id() -> str:
    binding = _binding.get()
    return str(binding["request_id"]) if binding else ""


def last_stage() -> str:
    binding = _binding.get()
    return str(binding["last_stage"]) if binding else ""


def last_failure_category() -> str:
    binding = _binding.get()
    return str((binding["last_failure"] or {}).get("category", "-")) if binding else "-"


def _exit_owner_present() -> bool:
    binding = _binding.get()
    return bool(binding and binding.get("owns_exit"))


def bound_text(value: Any, limit: int = _DEFAULT_BOUND_CHARS) -> str:
    """Single-line, bounded representation with an explicit truncation marker."""
    text = " ".join(str(value).split())
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…(+{len(text) - limit} chars)"


def task_trace(stage: str, **fields: Any) -> None:
    binding = _binding.get()
    if binding is None:
        # Silent outside a bound create_task request — provider/repository
        # layers are shared with normal chat traffic and must stay quiet.
        return
    if binding is not None:
        # tool_result is the wrapper's own terminal line, not a pipeline
        # stage — never let it shadow the last real stage for exit reporting.
        if stage not in {"tool_result", "scheduler_handoff"}:
            binding["last_stage"] = stage
        if stage in {"failed", "rejected"}:
            binding["last_failure"] = fields
    rid = str(binding["request_id"]) if binding else "-"
    parts = [f"{TRACE_PREFIX} request_id={rid} stage={stage}"]
    if binding:
        parts.extend(f"{key}={value}" for key, value in binding["context"].items())
    parts.extend(f"{key}={value}" for key, value in fields.items())
    logger.info(" ".join(parts))


def _trace_value(value: Any, limit: int = _OCCURRENCE_FIELD_CHARS) -> str:
    """Single-line, bounded trace field; never raises on odd caller values."""
    try:
        text = " ".join(str(value).split())
    except Exception:  # noqa: BLE001 - tracing must never break an operation
        return "invalid"
    if len(text) <= limit:
        return text
    return f"{text[:limit]}…(+{len(text) - limit} chars)"


def new_history_trace_id() -> str:
    """A bounded, non-secret correlation id for the owner creation flow."""
    return f"hist-{uuid.uuid4().hex[:12]}"


def current_occurrence() -> dict[str, Any] | None:
    """A copy of the active scheduled-occurrence binding, if any."""
    binding = _occurrence_binding.get()
    return dict(binding) if binding else None


@contextlib.contextmanager
def occurrence_scope(
    trace_id: str,
    *,
    task_id: int,
    attempt: int,
    **context: Any,
) -> Iterator[dict[str, Any]]:
    """Bind one durable occurrence for the duration of an async call tree.

    ``trace_id`` is the scheduler-generated occurrence key, which is stable
    across retries. ``attempt`` is the durable attempt carried by the row at
    this stage, so a retry remains correlated but is distinguishable. The
    token is always reset in ``finally``; concurrent asyncio tasks each carry
    their own copied context, so bindings cannot mix.
    """
    bounded_context = {
        str(key): _trace_value(value) for key, value in context.items()
    }
    token = _occurrence_binding.set(
        {
            "trace_id": _trace_value(trace_id, 256),
            "task_id": int(task_id) if isinstance(task_id, int) and not isinstance(task_id, bool) else 0,
            "attempt": int(attempt) if isinstance(attempt, int) and not isinstance(attempt, bool) else 0,
            "context": bounded_context,
        }
    )
    try:
        binding = _occurrence_binding.get() or {}
        yield binding
    finally:
        _occurrence_binding.reset(token)


def occurrence_trace(stage: str, **fields: Any) -> None:
    """Emit one occurrence-correlated record; silent outside the scope."""
    binding = _occurrence_binding.get()
    if binding is None:
        return
    parts = [
        f"{OCCURRENCE_TRACE_PREFIX} trace_id={binding['trace_id']} "
        f"task_id={binding['task_id']} attempt={binding['attempt']} stage={stage}",
    ]
    parts.extend(f"{key}={value}" for key, value in binding["context"].items())
    for key, value in fields.items():
        if key in {"trace_id", "task_id", "attempt", "stage"}:
            continue
        parts.append(f"{key}={_trace_value(value)}")
    logger.info(" ".join(parts))


@contextlib.contextmanager
def history_creation_scope(trace_id: str = "") -> Iterator[str]:
    """Bind one owner creation flow; reuse an existing outer creation scope."""
    existing = _history_creation_binding.get()
    if existing is not None:
        yield str(existing["trace_id"])
        return
    chosen = _trace_value(trace_id, 128) if trace_id else new_history_trace_id()
    token = _history_creation_binding.set({"trace_id": chosen})
    try:
        yield chosen
    finally:
        _history_creation_binding.reset(token)


def history_creation_trace(stage: str, **fields: Any) -> None:
    """Emit one owner-creation record; silent outside the creation scope."""
    binding = _history_creation_binding.get()
    if binding is None:
        return
    parts = [
        f"{HISTORY_CREATION_TRACE_PREFIX} trace_id={binding['trace_id']} stage={stage}",
    ]
    for key, value in fields.items():
        if key in {"trace_id", "stage"}:
            continue
        parts.append(f"{key}={_trace_value(value)}")
    logger.info(" ".join(parts))
