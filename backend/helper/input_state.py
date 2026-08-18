"""
Input state management for Type B inline panels.

When a panel needs user input (e.g. save code, bio text), the panel
transitions to an "input" state. The self-bot listens for the owner's
next message in the same chat and feeds it to the pending input handler.

State is stored per-owner (single owner bot). Only one input can be
pending at a time — requesting a new input cancels the previous one.

The input handler receives: (text, chat_id, msg_id, inline_chat_id, inline_msg_id)
so it can edit the inline panel message after processing the input.
"""
import logging
import time
from typing import Awaitable, Callable, Any

logger = logging.getLogger(__name__)

InputHandler = Callable[[str, int, int, int, int], Awaitable[None]]

_pending: dict[int, dict] = {}

_INPUT_TIMEOUT_S = 120


def set_pending(
    owner_id: int,
    panel_id: str,
    handler: InputHandler,
    chat_id: int,
    prompt: str,
    inline_chat_id: int = 0,
    inline_msg_id: int = 0,
    extra: str = "",
    timeout: float | None = 60.0,
) -> None:
    """Set a pending input request for the owner. Replaces any previous pending input.

    ``timeout`` bounds the handler execution once the owner's reply arrives.
    Pass ``None`` for operations that may legitimately run long (e.g. Deep Save
    media transfer). The separate 120 s pending-state expiry is unaffected.
    """
    _pending[owner_id] = {
        "panel_id": panel_id,
        "handler": handler,
        "chat_id": chat_id,
        "prompt": prompt,
        "inline_chat_id": inline_chat_id,
        "inline_msg_id": inline_msg_id,
        "extra": extra,
        "timeout": timeout,
        "created_at": time.monotonic(),
    }
    logger.debug("Input pending for owner %s: panel=%s", owner_id, panel_id)


def get_pending(owner_id: int) -> dict | None:
    """Get the pending input request, or None. Auto-expires stale inputs."""
    entry = _pending.get(owner_id)
    if entry is None:
        return None
    created_at = entry.get("created_at", 0)
    if time.monotonic() - created_at > _INPUT_TIMEOUT_S:
        _pending.pop(owner_id, None)
        logger.info("Input for owner %s expired (timeout=%ds)", owner_id, _INPUT_TIMEOUT_S)
        return None
    return entry


def clear_pending(owner_id: int) -> dict | None:
    """Clear and return the pending input request."""
    return _pending.pop(owner_id, None)


def has_pending(owner_id: int) -> bool:
    """Check if there's a pending input for the owner."""
    return get_pending(owner_id) is not None


def clear_all() -> None:
    """Clear all pending input states."""
    _pending.clear()
