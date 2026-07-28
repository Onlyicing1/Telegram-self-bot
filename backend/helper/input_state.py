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
from typing import Awaitable, Callable, Any

logger = logging.getLogger(__name__)

InputHandler = Callable[[str, int, int, int, int], Awaitable[None]]

_pending: dict[int, dict] = {}


def set_pending(
    owner_id: int,
    panel_id: str,
    handler: InputHandler,
    chat_id: int,
    prompt: str,
    inline_chat_id: int = 0,
    inline_msg_id: int = 0,
) -> None:
    """Set a pending input request for the owner."""
    _pending[owner_id] = {
        "panel_id": panel_id,
        "handler": handler,
        "chat_id": chat_id,
        "prompt": prompt,
        "inline_chat_id": inline_chat_id,
        "inline_msg_id": inline_msg_id,
    }
    logger.debug("Input pending for owner %s: panel=%s", owner_id, panel_id)


def get_pending(owner_id: int) -> dict | None:
    """Get the pending input request, or None."""
    return _pending.get(owner_id)


def clear_pending(owner_id: int) -> dict | None:
    """Clear and return the pending input request."""
    return _pending.pop(owner_id, None)


def has_pending(owner_id: int) -> bool:
    """Check if there's a pending input for the owner."""
    return owner_id in _pending


def clear_all() -> None:
    """Clear all pending input states."""
    _pending.clear()
