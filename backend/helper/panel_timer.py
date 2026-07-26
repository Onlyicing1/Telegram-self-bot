"""
PanelTimer — auto-delete inline panels after 120 seconds.

Every callback resets the timer. Close immediately deletes the panel.
Deleting the panel also clears the timer. No polling — uses asyncio tasks.

The self-bot client (not the helper bot) performs deletions because
the helper bot lacks delete permissions in the owner's chats.
"""
import asyncio
import logging

logger = logging.getLogger(__name__)

_AUTO_DELETE_SECONDS = 120

_timers: dict[str, asyncio.Task] = {}


def _key(chat_id: int, msg_id: int) -> str:
    return f"{chat_id}:{msg_id}"


async def _auto_delete(self_client, chat_id: int, msg_id: int) -> None:
    try:
        await asyncio.sleep(_AUTO_DELETE_SECONDS)
        await self_client.delete_messages(chat_id, [msg_id])
    except asyncio.CancelledError:
        raise
    except Exception:
        pass
    finally:
        _timers.pop(_key(chat_id, msg_id), None)


def start_timer(self_client, chat_id: int, msg_id: int) -> None:
    """Start or reset the auto-delete timer for a panel message."""
    k = _key(chat_id, msg_id)
    existing = _timers.get(k)
    if existing and not existing.done():
        existing.cancel()
    _timers[k] = asyncio.create_task(_auto_delete(self_client, chat_id, msg_id))


def reset_timer(self_client, chat_id: int, msg_id: int) -> None:
    """Reset the timer (same as start — every callback resets)."""
    start_timer(self_client, chat_id, msg_id)


def stop_timer(chat_id: int, msg_id: int) -> None:
    """Cancel the timer without deleting (used when panel is manually closed)."""
    k = _key(chat_id, msg_id)
    task = _timers.pop(k, None)
    if task and not task.done():
        task.cancel()


async def delete_panel(self_client, chat_id: int, msg_id: int) -> None:
    """Immediately delete the panel message and clear its timer."""
    stop_timer(chat_id, msg_id)
    try:
        await self_client.delete_messages(chat_id, [msg_id])
    except Exception:
        pass
