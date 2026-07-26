"""
PanelTimer — deterministic auto-close state machine for inline panels.

One source of truth per panel (chat_id, msg_id). States:

  ACTIVE  — timer running, panel will auto-delete after _DURATION seconds.
  PAUSED  — timer cancelled, panel stays alive until manually closed.

The toggle button switches between ACTIVE and PAUSED.
Close and auto-delete both fully clear all state.

Countdown is displayed in 30-second steps: 120s → 90s → 60s → 30s → delete.
The countdown is rendered by a single asyncio task that sleeps 30s between
edits and re-renders the full panel message with the updated countdown.
"""
import asyncio
import logging
from enum import Enum

logger = logging.getLogger(__name__)

_DURATION = 120
_COUNTDOWN_INTERVAL = 30
_COUNTDOWN_VALUES = [90, 60, 30]


class TimerState(Enum):
    ACTIVE = "active"
    PAUSED = "paused"


class _PanelEntry:
    __slots__ = ("state", "expire_at", "task", "title", "body", "buttons")

    def __init__(self):
        self.state = TimerState.PAUSED
        self.expire_at: float = 0.0
        self.task: asyncio.Task | None = None
        self.title: str = "LifeOS"
        self.body: str = ""
        self.buttons: list = []


_panels: dict[str, _PanelEntry] = {}


def _key(chat_id: int, msg_id: int) -> str:
    return f"{chat_id}:{msg_id}"


def _now() -> float:
    return asyncio.get_event_loop().time()


def init_panel(self_client, chat_id: int, msg_id: int, title: str = "LifeOS", body: str = "", buttons: list = None) -> None:
    """Register a new panel and start its auto-close timer."""
    k = _key(chat_id, msg_id)
    _cancel_task(k)
    entry = _PanelEntry()
    entry.title = title
    entry.body = body
    entry.buttons = buttons or []
    _panels[k] = entry
    _activate(self_client, chat_id, msg_id, entry)


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


def _activate(self_client, chat_id: int, msg_id: int, entry: _PanelEntry) -> None:
    entry.state = TimerState.ACTIVE
    entry.expire_at = _now() + _DURATION
    entry.task = asyncio.create_task(_timer_loop(self_client, chat_id, msg_id))


def _pause(entry: _PanelEntry) -> None:
    entry.state = TimerState.PAUSED
    entry.expire_at = 0.0
    _cancel_entry_task(entry)


def _cancel_entry_task(entry: _PanelEntry) -> None:
    if entry.task and not entry.task.done():
        entry.task.cancel()
    entry.task = None


def _cancel_task(k: str) -> None:
    entry = _panels.get(k)
    if entry:
        _cancel_entry_task(entry)


async def _timer_loop(self_client, chat_id: int, msg_id: int) -> None:
    """Single task: countdown every 30s, then delete at 120s."""
    try:
        for value in _COUNTDOWN_VALUES:
            await asyncio.sleep(_COUNTDOWN_INTERVAL)
            entry = _panels.get(_key(chat_id, msg_id))
            if entry is None or entry.state != TimerState.ACTIVE:
                return
            await _edit_countdown(self_client, chat_id, msg_id, value)
        await asyncio.sleep(_COUNTDOWN_INTERVAL)
        destroy(self_client, chat_id, msg_id)
    except asyncio.CancelledError:
        raise
    except Exception:
        pass


async def _edit_countdown(self_client, chat_id: int, msg_id: int, seconds: int) -> None:
    """Re-render the inline message with the updated countdown value."""
    k = _key(chat_id, msg_id)
    entry = _panels.get(k)
    if entry is None:
        return
    header = f"**{entry.title}**\n\nAuto Close\n{seconds}s"
    if entry.body:
        full_text = f"{header}\n\n{entry.body}"
    else:
        full_text = header
    try:
        await self_client.edit_message(chat_id, msg_id, message=full_text)
    except Exception:
        pass


def toggle(self_client, chat_id: int, msg_id: int) -> TimerState:
    """Toggle between ACTIVE and PAUSED. Returns the new state."""
    k = _key(chat_id, msg_id)
    entry = _panels.get(k)
    if entry is None:
        entry = _PanelEntry()
        _panels[k] = entry
    if entry.state == TimerState.ACTIVE:
        _pause(entry)
        return TimerState.PAUSED
    _activate(self_client, chat_id, msg_id, entry)
    return TimerState.ACTIVE


def get_state(chat_id: int, msg_id: int) -> TimerState:
    """Return the current timer state for a panel."""
    k = _key(chat_id, msg_id)
    entry = _panels.get(k)
    return entry.state if entry else TimerState.PAUSED


def get_countdown_text(chat_id: int, msg_id: int) -> str:
    """Return the countdown header text for the current state."""
    k = _key(chat_id, msg_id)
    entry = _panels.get(k)
    if entry is None or entry.state != TimerState.ACTIVE:
        return "Auto Close\nDisabled"
    remaining = int(entry.expire_at - _now())
    if remaining <= 0:
        return "Auto Close\n0s"
    for step in [120] + _COUNTDOWN_VALUES:
        if remaining >= step:
            return f"Auto Close\n{step}s"
    return f"Auto Close\n{remaining}s"


def get_toggle_button_text(chat_id: int, msg_id: int) -> str:
    """Return the toggle button label for the current state."""
    state = get_state(chat_id, msg_id)
    return "Disable Auto Close" if state == TimerState.ACTIVE else "Enable Auto Close"


def is_paused(chat_id: int, msg_id: int) -> bool:
    return get_state(chat_id, msg_id) == TimerState.PAUSED


def destroy(self_client, chat_id: int, msg_id: int) -> None:
    """Fully clear timer state and delete the panel message."""
    k = _key(chat_id, msg_id)
    entry = _panels.pop(k, None)
    if entry:
        _cancel_entry_task(entry)
    try:
        asyncio.create_task(self_client.delete_messages(chat_id, [msg_id]))
    except Exception:
        pass


def stop_timer(chat_id: int, msg_id: int) -> None:
    """Cancel timer without deleting."""
    k = _key(chat_id, msg_id)
    entry = _panels.pop(k, None)
    if entry:
        _cancel_entry_task(entry)


def has_timer(chat_id: int, msg_id: int) -> bool:
    """Check if a panel has an active timer entry."""
    k = _key(chat_id, msg_id)
    entry = _panels.get(k)
    return entry is not None and entry.state == TimerState.ACTIVE


def active_count() -> int:
    """Return number of panels with active timers."""
    return sum(1 for e in _panels.values() if e.state == TimerState.ACTIVE)
