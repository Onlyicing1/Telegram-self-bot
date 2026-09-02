"""Request-correlated trace helper for the create_task pipeline.

``[CREATE_TASK_TRACE]`` lines are emitted only while a create_task request
is bound (see ``bind_request``), so the provider and repository layers
shared with normal chat traffic stay completely silent outside task
creation. Every line carries the correlation id plus the bound owner/chat
context; the helper also remembers the most recent stage and failure
category so the terminal ``exit`` line can report where a request ended
without extra plumbing through the layers.

Sanitization: this helper never logs credentials — it only formats what
callers pass, and callers pass bounded, task-scoped values (request text,
candidate fields, exception class names). ``bound_text`` flattens control
characters and truncates with an explicit marker so log lines stay
single-line and bounded.
"""
from __future__ import annotations

import contextvars
import logging
from typing import Any

logger = logging.getLogger(__name__)

TRACE_PREFIX = "AI_TASK_TRACE"
_DEFAULT_BOUND_CHARS = 240

_binding: contextvars.ContextVar[dict[str, Any] | None] = contextvars.ContextVar(
    "create_task_trace_binding", default=None
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
