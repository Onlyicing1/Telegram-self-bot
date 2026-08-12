"""
PanelLifecycleManager — the single owner of the entire panel lifecycle.

State machine:

    IDLE → CREATING → ACTIVE → CLOSING → IDLE
                       ↑   |
                       └───┘ (callback edits / input)

Every exit path (close button, auto-close timeout, edit failure,
replacement, error, shutdown) goes through exactly ONE cleanup
function: lifecycle_cleanup().

No other module may:
  - manipulate sessions directly
  - cancel timers directly
  - clear pending input directly
  - pop render caches directly

All resource management goes through this manager.
"""
import asyncio
import logging
from enum import Enum, auto
from typing import Any

from backend.helper.input_state import clear_pending, clear_all as clear_all_pending
from backend.helper.session_manager import (
    SessionManager,
    Session,
)
from backend.helper.panel_timer import PanelTimerManager
from backend.runtime.operation_watchdog import guarded_await
from backend.runtime.task_guard import guarded_create_task

logger = logging.getLogger(__name__)


async def _delayed_delete(client, chat_id: int, msg_id: int, delay: float = 2.5) -> None:
    try:
        await asyncio.sleep(delay)
        await guarded_await(
            client.delete_messages(chat_id, [msg_id]),
            name="panel:delayed-delete",
            timeout=15.0,
        )
    except Exception:
        pass


class PanelState(Enum):
    IDLE = auto()
    CREATING = auto()
    ACTIVE = auto()
    CLOSING = auto()


class PanelLifecycleManager:
    """Single owner of all panel resources.

    Thread-safe via a single asyncio.Lock. Every state transition is
    atomic. Every cleanup goes through the same path.
    """

    __slots__ = (
        "_lock",
        "_sessions",
        "_timers",
        "_renders",
        "_states",
        "_self_client",
        "_owner_id",
    )

    def __init__(self) -> None:
        self._lock = asyncio.Lock()
        self._sessions = SessionManager()
        self._timers = PanelTimerManager(self)
        self._renders: dict[tuple[int, int], tuple[str, tuple]] = {}
        self._states: dict[tuple[int, int], PanelState] = {}
        self._self_client: Any = None
        self._owner_id: int = 0

    def configure(self, self_client: Any, owner_id: int) -> None:
        self._self_client = self_client
        self._owner_id = owner_id

    @property
    def sessions(self) -> SessionManager:
        return self._sessions

    @property
    def timers(self) -> PanelTimerManager:
        return self._timers

    # ── Render cache ──

    def get_render(self, chat_id: int, msg_id: int) -> tuple[str, tuple] | None:
        return self._renders.get((chat_id, msg_id))

    def set_render(self, chat_id: int, msg_id: int, text: str, buttons_repr: tuple) -> None:
        self._renders[(chat_id, msg_id)] = (text, buttons_repr)

    def clear_render(self, chat_id: int, msg_id: int) -> None:
        self._renders.pop((chat_id, msg_id), None)

    def get_state(self, chat_id: int, msg_id: int) -> PanelState:
        return self._states.get((chat_id, msg_id), PanelState.IDLE)

    def _set_state(self, chat_id: int, msg_id: int, state: PanelState) -> None:
        if state == PanelState.IDLE:
            self._states.pop((chat_id, msg_id), None)
        else:
            self._states[(chat_id, msg_id)] = state

    # ── Create ──

    async def create_panel(
        self,
        chat_id: int,
        panel_id: str,
        extra: str = "",
        inline_message_id: str = "",
    ) -> tuple[bool, int, int]:
        """Create a new panel. Destroys all existing sessions for the chat first.

        Returns (success, msg_chat_id, msg_id).
        """
        async with self._lock:
            await self._destroy_all_for_chat_locked(chat_id)

            self._set_state(chat_id, 0, PanelState.CREATING)

            from backend.helper import inline_engine
            from backend.services import settings_service

            query = f"{panel_id}:{extra}" if extra else panel_id
            try:
                success, msg_chat_id, msg_id, inline_msg_id = await inline_engine.trigger(
                    self._self_client, chat_id, query
                )
            except Exception:
                logger.exception("[LIFECYCLE] create_panel: trigger failed")
                self._set_state(chat_id, 0, PanelState.IDLE)
                return False, chat_id, 0

            if not success or not msg_id:
                logger.warning(
                    "[LIFECYCLE] create_panel: trigger returned success=%s msg_id=%s",
                    success, msg_id,
                )
                self._set_state(chat_id, 0, PanelState.IDLE)
                return False, chat_id, 0

            self._sessions.create(
                msg_chat_id, msg_id,
                panel_type=panel_id,
                extra=extra,
                inline_message_id=inline_msg_id or inline_msg_id,
            )

            if settings_service.is_auto_close_enabled():
                self._timers.start(
                    self._self_client, msg_chat_id, msg_id,
                    owner_id=self._owner_id,
                )

            self._set_state(msg_chat_id, msg_id, PanelState.ACTIVE)
            logger.info(
                "[LIFECYCLE] CREATE panel='%s' chat=%s msg=%s",
                panel_id, msg_chat_id, msg_id,
            )
            return True, msg_chat_id, msg_id

    async def try_reuse_panel(
        self,
        chat_id: int,
        panel_id: str,
        extra: str = "",
    ) -> bool:
        """Try to reuse an existing panel for the same chat. Returns True if reused.

        Only reuses if:
          - reuse_existing_panel setting is True
          - exactly one session exists for the chat
          - the edit succeeds
        """
        from backend.services import settings_service

        if not settings_service.is_reuse_existing_panel():
            return False

        async with self._lock:
            sessions = self._sessions.find_all_by_chat(chat_id)
            if len(sessions) != 1:
                return False

            session = sessions[0]
            old_chat = session.chat_id
            old_msg = session.msg_id

            from backend.helper.panels import _finalize_panel, _extract_render_result, get_panel
            from backend.helper.panel_render import render_edit

            handler = get_panel(panel_id)
            if handler is None:
                return False

            try:
                result = await handler(None, extra)
                title, body, buttons = _extract_render_result(result)
                if not title and not body and not buttons:
                    return False

                title, body, buttons = _finalize_panel(
                    title, body, buttons, panel_id, old_chat, old_msg
                )
                text, built_buttons = render_edit(title, body, buttons)
                self._timers.set_content(old_chat, old_msg, title, body, buttons)

                try:
                    await self._self_client.edit_message(
                        old_chat, old_msg,
                        message=text,
                        buttons=built_buttons if built_buttons else [],
                    )
                except Exception as exc:
                    if "not modified" in str(exc).lower():
                        pass
                    else:
                        logger.warning("[LIFECYCLE] reuse edit failed: %s", exc)
                        return False

                self._sessions.push_nav(old_chat, old_msg, panel_id, extra)
                self._set_state(old_chat, old_msg, PanelState.ACTIVE)
                logger.info(
                    "[LIFECYCLE] REUSE panel='%s' chat=%s msg=%s",
                    panel_id, old_chat, old_msg,
                )
                return True
            except Exception:
                logger.warning("[LIFECYCLE] reuse failed — will create new")
                return False

    # ── Close / Destroy ──

    async def close_panel(
        self,
        chat_id: int,
        msg_id: int,
        event: Any = None,
    ) -> None:
        """Close a panel: cleanup all resources, edit message to closed state."""
        async with self._lock:
            await self._cleanup_locked(chat_id, msg_id, edit_message=True, event=event)

    async def timeout_panel(self, chat_id: int, msg_id: int) -> None:
        """Called by the timer when it expires. Delegates to cleanup."""
        async with self._lock:
            await self._cleanup_locked(chat_id, msg_id, edit_message=True, event=None)

    async def destroy_panel(self, chat_id: int, msg_id: int) -> None:
        """Destroy a panel without editing the message (e.g. replacement)."""
        async with self._lock:
            await self._cleanup_locked(chat_id, msg_id, edit_message=False, event=None)

    async def destroy_all_for_chat(self, chat_id: int) -> None:
        """Destroy ALL panels for a chat."""
        async with self._lock:
            await self._destroy_all_for_chat_locked(chat_id)

    async def destroy_all_for_owner(self, owner_id: int) -> None:
        """Destroy ALL panels for an owner."""
        async with self._lock:
            for (cid, mid) in list(self._sessions.all_keys()):
                session = self._sessions.get(cid, mid)
                if session and session.owner_id == owner_id:
                    await self._cleanup_locked(cid, mid, edit_message=False, event=None)

    async def _destroy_all_for_chat_locked(self, chat_id: int) -> None:
        sessions = self._sessions.find_all_by_chat(chat_id)
        for session in sessions:
            await self._cleanup_locked(
                session.chat_id, session.msg_id,
                edit_message=False, event=None,
            )

    async def _cleanup_locked(
        self,
        chat_id: int,
        msg_id: int,
        edit_message: bool,
        event: Any = None,
    ) -> None:
        """THE ONE cleanup function. Every exit path goes through here.

        Releases:
          - Session registry
          - Inline message registry
          - Pending inputs
          - Active timer
          - Cached render
          - Cached state
        """
        self._set_state(chat_id, msg_id, PanelState.CLOSING)

        self._timers.stop(chat_id, msg_id)
        self._sessions.destroy(chat_id, msg_id)
        self.clear_render(chat_id, msg_id)
        if self._owner_id:
            clear_pending(self._owner_id)

        self._set_state(chat_id, msg_id, PanelState.IDLE)

        if edit_message:
            closed_text = "✕ **Panel closed**"
            edited = False
            if event is not None:
                try:
                    await event.edit(closed_text, buttons=[])
                    edited = True
                except Exception as exc:
                    logger.debug("[LIFECYCLE] cleanup event.edit: %s", exc)
            elif self._self_client is not None and chat_id and msg_id:
                try:
                    await self._self_client.edit_message(
                        chat_id, msg_id, message=closed_text, buttons=[]
                    )
                    edited = True
                except Exception as exc:
                    logger.debug("[LIFECYCLE] cleanup edit_message: %s", exc)

            if edited and self._self_client is not None and chat_id and msg_id:
                guarded_create_task(
                    _delayed_delete(self._self_client, chat_id, msg_id),
                    name="panel:delayed-delete",
                )

        logger.info("[LIFECYCLE] CLEANUP chat=%s msg=%s", chat_id, msg_id)

    # ── Shutdown ──

    async def shutdown_all(self) -> None:
        """Destroy all panels. Called during process shutdown."""
        async with self._lock:
            for (cid, mid) in list(self._sessions.all_keys()):
                await self._cleanup_locked(cid, mid, edit_message=False, event=None)
            self._timers.stop_all()
            clear_all_pending()

    # ── Diagnostics ──

    def active_count(self) -> int:
        return sum(1 for s in self._states.values() if s == PanelState.ACTIVE)

    def session_count(self) -> int:
        return len(self._sessions.all_keys())

    def timer_count(self) -> int:
        return self._timers.active_count()


_lifecycle: PanelLifecycleManager | None = None


def get_lifecycle() -> PanelLifecycleManager:
    global _lifecycle
    if _lifecycle is None:
        _lifecycle = PanelLifecycleManager()
    return _lifecycle


def configure_lifecycle(self_client: Any, owner_id: int) -> None:
    get_lifecycle().configure(self_client, owner_id)
