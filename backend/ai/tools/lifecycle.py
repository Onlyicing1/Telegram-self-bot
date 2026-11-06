"""Structured AI tool-lifecycle events.

One emitter for the whole tool lifecycle so the Dispatcher and the
``ToolExecutor`` produce searchable, correlatable lines with the same shape:

    <EVENT> id=<request id> round=<n> call=<tool call id> tool=<name> ...

This module is observability ONLY. It never decides anything, never touches a
tool call, and never carries user content:

  * arguments are described by FIELD NAME, COUNT and TYPE — never by value;
  * results are described by success, latency and bounded sizes — never by
    content;
  * exceptions are reduced to their type name, and any error string is reduced
    to its bounded class, so an unbounded payload (or a credential embedded in
    one) cannot reach the log.

Events are emitted at INFO for the normal lifecycle and WARNING for timeout /
unexpected failure / invalid execution state.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

MAX_FIELD_CHARS = 120
MAX_LISTED_FIELDS = 12

_SAFE_TYPE_NAMES = frozenset({"str", "int", "float", "bool", "list", "dict", "NoneType"})


def bounded(value: Any, limit: int = MAX_FIELD_CHARS) -> str:
    """A single-line, length-bounded rendering of ``value``."""
    text = str(value if value is not None else "")
    text = text.replace("\n", " ").replace("\r", " ").strip()
    if len(text) > limit:
        return text[: max(0, limit - 1)] + "…"
    return text


def error_class(value: Any) -> str:
    """The bounded CLASS of an error string, never its message.

    ``ValueError: token=abc`` becomes ``ValueError``; a prose message becomes
    ``error``. This is what makes an error field safe to log.
    """
    text = str(value or "").strip()
    if not text:
        return ""
    head = text.split(":", 1)[0].strip()
    if head and " " not in head:
        return bounded(head, 48)
    return "error"


def arg_shape(arguments: Any) -> str:
    """A value-free description of a tool call's arguments.

    Only the argument field NAMES, the field COUNT and the value TYPES are
    reported. No argument value is ever included.
    """
    if not isinstance(arguments, dict):
        return f"args_type={type(arguments).__name__}"
    names = sorted(str(name) for name in arguments)[:MAX_LISTED_FIELDS]
    types = sorted(
        {
            type(value).__name__ if type(value).__name__ in _SAFE_TYPE_NAMES else "other"
            for value in arguments.values()
        }
    )
    return (
        f"arg_count={len(arguments)} "
        f"arg_fields={','.join(names) or '-'} "
        f"arg_types={','.join(types) or '-'}"
    )


def call_id(call: dict[str, Any], round_no: int, index: int) -> str:
    """The provider's tool-call id, or a bounded synthetic correlation id.

    A provider that omits tool-call ids must not break correlation, and the
    provider-visible call contract must not be modified — so the synthetic id
    lives only in the log line.
    """
    raw = ""
    if isinstance(call, dict):
        raw = str(call.get("id") or call.get("tool_call_id") or "")
    if raw:
        return bounded(raw, 64)
    return f"auto-r{int(round_no)}-c{int(index) + 1}"


def emit(
    event: str,
    *,
    request_id: str = "",
    round_no: int = 0,
    tool_call_id: str = "",
    tool: str = "",
    provider: str = "",
    model: str = "",
    status: str = "",
    elapsed_ms: float | None = None,
    error: str = "",
    detail: str = "",
    level: int = logging.INFO,
) -> None:
    """Emit one bounded lifecycle event."""
    parts = [f"id={bounded(request_id, 72) or '-'}", f"round={int(round_no)}"]
    for key, value, limit in (
        ("call", tool_call_id, 64),
        ("tool", tool, 64),
        ("provider", provider, 48),
        ("model", model, 64),
        ("status", status, 48),
        ("error", error, 64),
        ("detail", detail, 160),
    ):
        text = bounded(value, limit)
        if text:
            parts.append(f"{key}={text}")
    if elapsed_ms is not None:
        parts.append(f"elapsed_ms={float(elapsed_ms):.1f}")
    logger.log(level, "%s %s", event, " ".join(parts))
