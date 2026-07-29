import logging
import time
from typing import Awaitable, Callable, Any

from backend.services import settings_service

logger = logging.getLogger(__name__)

InputHandler = Callable[[str, int, int, int, int], Awaitable[None]]

_pending: dict[int, dict] = {}


def set_input(
    owner_id: int,
    panel: str,
    field: str,
    handler: InputHandler,
    prompt: str,
    inline_chat_id: int = 0,
    inline_msg_id: int = 0,
) -> None:
    _pending[owner_id] = {
        "panel": panel,
        "field": field,
        "handler": handler,
        "prompt": prompt,
        "inline_chat_id": inline_chat_id,
        "inline_msg_id": inline_msg_id,
        "created_at": time.monotonic(),
    }
    logger.info("Input set for owner %s: panel=%s field=%s", owner_id, panel, field)


def get_input(owner_id: int) -> dict | None:
    entry = _pending.get(owner_id)
    if entry is None:
        return None
    created_at = entry.get("created_at", 0)
    timeout = settings_service.input_timeout_seconds()
    if time.monotonic() - created_at > timeout:
        _pending.pop(owner_id, None)
        logger.info("Input for owner %s expired (timeout=%ds)", owner_id, timeout)
        return None
    return entry


def clear_input(owner_id: int) -> None:
    _pending.pop(owner_id, None)
    logger.info("Input cleared for owner %s", owner_id)


def clear_all() -> None:
    _pending.clear()
    logger.info("All pending inputs cleared")


def has_pending(owner_id: int) -> bool:
    return get_input(owner_id) is not None


def get_prompt(owner_id: int) -> str | None:
    entry = get_input(owner_id)
    if entry is None:
        return None
    return entry.get("prompt")
