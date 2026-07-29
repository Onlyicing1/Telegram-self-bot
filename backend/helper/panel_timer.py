"""
PanelTimer — single-message auto-close timer for inline panels.

Respects the global auto-close preference (panel_settings).
When auto-close is OFF globally, no timer task is created at all.

When ON, a single asyncio task per panel edits the SAME inline message
every 30 seconds with the updated countdown:
  120s → 90s → 60s → 30s → delete

No second message. No new messages. Only edits.
"""
import asyncio
import logging

from backend.helper.panel_settings import is_auto_close_enabled

logger = logging.getLogger(__name__)

_DURATION = 120
_COUNTDOWN_INTERVAL = 30
_COUNTDOWN_VALUES = [90, 60, 30]


class _PanelEntry:
    __slots__ = ("expire_at", "task", "title", "body", "buttons")

    def __init__(self):
        self.expire_at: float = 0.0
        self.task: asyncio.Task | None = None
        self.title: str = "LifeOS"
        self.body: str = ""
        self.buttons: list = []


_panels: dict[str, _PanelEntry] = {}


def _key(chat_id: int, msg_id: int) -> str:
    return f"{chat_id}:{msg_id}"


def _now() -> float:
    try:
        return asyncio.get_running_loop().time()
    except RuntimeError:
        import time
        return time.monotonic()


def init_panel(self_client, chat_id: int, msg_id: int, title: str = "LifeOS", body: str = "", buttons: list = None) -> None:
    """Register a panel and start its timer (only if auto-close is globally ON)."""
    k = _key(chat_id, msg_id)
    _cancel_task(k)
    entry = _PanelEntry()
    entry.title = title
    entry.body = body
    entry.buttons = buttons or []
    _panels[k] = entry
    if is_auto_close_enabled():
        _start_timer(self_client, chat_id, msg_id, entry)


def set_content(chat_id: int, msg_id: int, title: str, body: str, buttons: list) -> None:
    """Update stored panel content for countdown re-renders."""
    k = _key(chat_id, msg_id)
    entry = _panels.get(k)
    if entry is None:
        entry = _PanelEntry()
        _panels[k] = entry
    entry.title = title
    entry.body = body
    entry.buttons = buttons or []


def _start_timer(self_client, chat_id: int, msg_id: int, entry: _PanelEntry) -> None:
    entry.expire_at = _now() + _DURATION
    entry.task = asyncio.create_task(_timer_loop(self_client, chat_id, msg_id))


def _cancel_entry_task(entry: _PanelEntry) -> None:
    if entry.task and not entry.task.done():
        entry.task.cancel()
    entry.task = None


def _cancel_task(k: str) -> None:
    entry = _panels.get(k)
    if entry:
        _cancel_entry_task(entry)


async def _timer_loop(self_client, chat_id: int, msg_id: int) -> None:
    """Single task: countdown every 30s via edits, then delete at 120s."""
    try:
        for value in _COUNTDOWN_VALUES:
            await asyncio.sleep(_COUNTDOWN_INTERVAL)
            entry = _panels.get(_key(chat_id, msg_id))
            if entry is None or entry.task is None or entry.task.done():
                return
            if not is_auto_close_enabled():
                return
            await _edit_countdown(self_client, chat_id, msg_id, value)
        await asyncio.sleep(_COUNTDOWN_INTERVAL)
        entry = _panels.get(_key(chat_id, msg_id))
        if entry is None or not is_auto_close_enabled():
            return
        await destroy(self_client, chat_id, msg_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


async def _edit_countdown(self_client, chat_id: int, msg_id: int, seconds: int) -> None:
    """Edit the inline message with the updated countdown value."""
    k = _key(chat_id, msg_id)
    entry = _panels.get(k)
    if entry is None:
        return
    header = f"**{entry.title}**\n\n⏱ {seconds}s"
    if entry.body:
        full_text = f"{header}\n\n{entry.body}"
    else:
        full_text = header
    try:
        await self_client.edit_message(chat_id, msg_id, message=full_text)
    except Exception:
        pass


async def destroy(self_client, chat_id: int, msg_id: int) -> None:
    """Auto-close entry point — calls the shared close_panel function.

    This is the EXACT same code path as pressing the Close button.
    No duplicate implementation.
    """
    from backend.helper.panels import close_panel
    from backend.helper.input_state import clear_all as clear_all_pending

    k = _key(chat_id, msg_id)
    entry = _panels.pop(k, None)
    if entry:
        _cancel_entry_task(entry)

    clear_all_pending()

    await close_panel(None, chat_id, msg_id, 0)


def stop_timer(chat_id: int, msg_id: int) -> None:
    """Cancel timer without clearing session or deleting the message."""
    k = _key(chat_id, msg_id)
    entry = _panels.pop(k, None)
    if entry:
        _cancel_entry_task(entry)


def has_timer(chat_id: int, msg_id: int) -> bool:
    k = _key(chat_id, msg_id)
    entry = _panels.get(k)
    return entry is not None and entry.task is not None and not entry.task.done()


def active_count() -> int:
    return sum(1 for e in _panels.values() if e.task is not None and not e.task.done())


def stop_all() -> None:
    """Cancel all active panel timers without deleting messages."""
    for k in list(_panels.keys()):
        entry = _panels.pop(k, None)
        if entry:
            _cancel_entry_task(entry)
