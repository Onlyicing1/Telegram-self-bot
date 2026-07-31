"""
PanelTimerManager — thin timer that delegates to the lifecycle manager.

The timer NEVER calls destroy/cleanup logic directly. When it expires,
it calls lifecycle.timeout_panel() which runs the single cleanup path.

When auto-close is OFF globally, no timer task is created at all.

When ON, a single asyncio task per panel edits the SAME inline message
every 30 seconds with the updated countdown, then delegates to the
lifecycle manager at expiry.
"""
import asyncio
import logging

from backend.services import settings_service
from backend.runtime.task_guard import guarded_create_task

logger = logging.getLogger(__name__)

_COUNTDOWN_INTERVAL = 30


class _PanelEntry:
    __slots__ = ("expire_at", "task", "title", "body", "buttons", "owner_id")

    def __init__(self) -> None:
        self.expire_at: float = 0.0
        self.task: asyncio.Task | None = None
        self.title: str = "LifeOS"
        self.body: str = ""
        self.buttons: list = []
        self.owner_id: int = 0


class PanelTimerManager:
    __slots__ = ("_lifecycle", "_panels")

    def __init__(self, lifecycle) -> None:
        self._lifecycle = lifecycle
        self._panels: dict[str, _PanelEntry] = {}

    @staticmethod
    def _key(chat_id: int, msg_id: int) -> str:
        return f"{chat_id}:{msg_id}"

    @staticmethod
    def _now() -> float:
        try:
            return asyncio.get_running_loop().time()
        except RuntimeError:
            import time
            return time.monotonic()

    def start(
        self,
        self_client,
        chat_id: int,
        msg_id: int,
        owner_id: int = 0,
    ) -> None:
        k = self._key(chat_id, msg_id)
        self._cancel_entry(k)
        entry = _PanelEntry()
        entry.owner_id = owner_id
        self._panels[k] = entry
        duration = settings_service.auto_close_delay()
        entry.expire_at = self._now() + duration
        entry.task = guarded_create_task(
            self._timer_loop(self_client, chat_id, msg_id, duration),
            name=f"lifeos-panel-timer-{msg_id}",
        )

    def set_content(
        self,
        chat_id: int,
        msg_id: int,
        title: str,
        body: str,
        buttons: list,
    ) -> None:
        k = self._key(chat_id, msg_id)
        entry = self._panels.get(k)
        if entry is None:
            entry = _PanelEntry()
            self._panels[k] = entry
        entry.title = title
        entry.body = body
        entry.buttons = buttons or []

    def stop(self, chat_id: int, msg_id: int) -> None:
        k = self._key(chat_id, msg_id)
        entry = self._panels.pop(k, None)
        if entry:
            self._cancel_entry_task(entry)

    def has_timer(self, chat_id: int, msg_id: int) -> bool:
        k = self._key(chat_id, msg_id)
        entry = self._panels.get(k)
        return entry is not None and entry.task is not None and not entry.task.done()

    def active_count(self) -> int:
        return sum(1 for e in self._panels.values() if e.task is not None and not e.task.done())

    def stop_all(self) -> None:
        for k in list(self._panels.keys()):
            entry = self._panels.pop(k, None)
            if entry:
                self._cancel_entry_task(entry)

    def _cancel_entry_task(self, entry: _PanelEntry) -> None:
        if entry.task and not entry.task.done():
            entry.task.cancel()
        entry.task = None

    def _cancel_entry(self, k: str) -> None:
        entry = self._panels.get(k)
        if entry:
            self._cancel_entry_task(entry)

    async def _timer_loop(
        self,
        self_client,
        chat_id: int,
        msg_id: int,
        duration: int,
    ) -> None:
        try:
            steps = max(1, duration // _COUNTDOWN_INTERVAL)
            for i in range(steps):
                await asyncio.sleep(_COUNTDOWN_INTERVAL)
                k = self._key(chat_id, msg_id)
                entry = self._panels.get(k)
                if entry is None or entry.task is None or entry.task.done():
                    return
                if not settings_service.is_auto_close_enabled():
                    return
                remaining = duration - (i + 1) * _COUNTDOWN_INTERVAL
                if remaining > 0:
                    await self._edit_countdown(self_client, chat_id, msg_id, remaining)
            k = self._key(chat_id, msg_id)
            entry = self._panels.get(k)
            if entry is None or not settings_service.is_auto_close_enabled():
                return
            await self._lifecycle.timeout_panel(chat_id, msg_id)
        except asyncio.CancelledError:
            raise
        except Exception:
            pass

    async def _edit_countdown(
        self,
        self_client,
        chat_id: int,
        msg_id: int,
        seconds: int,
    ) -> None:
        k = self._key(chat_id, msg_id)
        entry = self._panels.get(k)
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


# ── Backward-compatible module-level API ──
# Delegates to a singleton. New code should use the lifecycle manager's
# .timers property instead.

_manager: PanelTimerManager | None = None


def _get_manager() -> PanelTimerManager:
    global _manager
    if _manager is None:
        from backend.helper.lifecycle import get_lifecycle
        _manager = get_lifecycle().timers
    return _manager


def init_panel(self_client, chat_id: int, msg_id: int, title: str = "LifeOS", body: str = "", buttons: list = None, owner_id: int = 0) -> None:
    mgr = _get_manager()
    mgr.set_content(chat_id, msg_id, title, body, buttons or [])
    if settings_service.is_auto_close_enabled():
        mgr.start(self_client, chat_id, msg_id, owner_id=owner_id)


def set_content(chat_id: int, msg_id: int, title: str, body: str, buttons: list) -> None:
    _get_manager().set_content(chat_id, msg_id, title, body, buttons)


def stop_timer(chat_id: int, msg_id: int) -> None:
    _get_manager().stop(chat_id, msg_id)


def has_timer(chat_id: int, msg_id: int) -> bool:
    return _get_manager().has_timer(chat_id, msg_id)


def active_count() -> int:
    return _get_manager().active_count()


def stop_all() -> None:
    _get_manager().stop_all()


async def destroy(self_client, chat_id: int, msg_id: int, owner_id: int = 0) -> None:
    from backend.helper.lifecycle import get_lifecycle
    await get_lifecycle().timeout_panel(chat_id, msg_id)
