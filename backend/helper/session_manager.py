"""
SessionManager — owns ALL panel session state.

No module outside this file may create, modify, or destroy sessions
directly. All access goes through the PanelLifecycleManager which
holds a SessionManager instance.

Sessions are keyed by (chat_id, msg_id). For inline messages that
lack a msg_id, the inline_message_id string is used as a secondary
lookup key.

Thread safety: all operations are synchronous and rely on the
PanelLifecycleManager's asyncio.Lock for atomicity. The manager
calls these methods while holding the lock.
"""
import logging
from dataclasses import dataclass, field
from typing import Iterator

logger = logging.getLogger(__name__)


@dataclass
class Session:
    session_id: str
    chat_id: int
    msg_id: int
    panel_type: str
    nav_stack: list[tuple[str, str]] = field(default_factory=list)
    inline_message_id: str = ""
    owner_id: int = 0


class SessionManager:
    __slots__ = ("_sessions", "_inline_index", "_counter")

    def __init__(self) -> None:
        self._sessions: dict[tuple[int, int], Session] = {}
        self._inline_index: dict[str, tuple[int, int]] = {}
        self._counter: int = 0

    def create(
        self,
        chat_id: int,
        msg_id: int,
        panel_type: str = "unknown",
        extra: str = "",
        inline_message_id: str = "",
        owner_id: int = 0,
    ) -> str:
        self._counter += 1
        sid = f"PANEL-SESSION-{self._counter:06d}"
        session = Session(
            session_id=sid,
            chat_id=chat_id,
            msg_id=msg_id,
            panel_type=panel_type,
            nav_stack=[(panel_type, extra)],
            inline_message_id=inline_message_id,
            owner_id=owner_id,
        )
        self._sessions[(chat_id, msg_id)] = session
        if inline_message_id:
            self._inline_index[inline_message_id] = (chat_id, msg_id)
        return sid

    def get(self, chat_id: int | None, msg_id: int | None) -> Session | None:
        if chat_id is None or msg_id is None:
            return None
        return self._sessions.get((chat_id, msg_id))

    def get_by_inline_id(self, inline_message_id: str | None) -> Session | None:
        if not inline_message_id:
            return None
        key = self._inline_index.get(inline_message_id)
        if key is None:
            return None
        return self._sessions.get(key)

    def find_by_chat(self, chat_id: int) -> Session | None:
        for (cid, _mid), session in self._sessions.items():
            if cid == chat_id:
                return session
        return None

    def find_all_by_chat(self, chat_id: int) -> list[Session]:
        return [
            session for (cid, _mid), session in self._sessions.items()
            if cid == chat_id
        ]

    def find_by_msg_id(self, msg_id: int) -> Session | None:
        for (_cid, mid), session in self._sessions.items():
            if mid == msg_id:
                return session
        return None

    def destroy(self, chat_id: int | None, msg_id: int | None) -> Session | None:
        if chat_id is None or msg_id is None:
            return None
        session = self._sessions.pop((chat_id, msg_id), None)
        if session and session.inline_message_id:
            self._inline_index.pop(session.inline_message_id, None)
        return session

    def destroy_all_for_chat(self, chat_id: int) -> list[tuple[int, int]]:
        destroyed = []
        for (cid, mid) in list(self._sessions.keys()):
            if cid == chat_id:
                self.destroy(cid, mid)
                destroyed.append((cid, mid))
        return destroyed

    def destroy_all_for_owner(self, owner_id: int) -> list[tuple[int, int]]:
        destroyed = []
        for (cid, mid), session in list(self._sessions.items()):
            if session.owner_id == owner_id:
                self.destroy(cid, mid)
                destroyed.append((cid, mid))
        return destroyed

    def all_keys(self) -> list[tuple[int, int]]:
        return list(self._sessions.keys())

    def clear_all(self) -> None:
        self._sessions.clear()
        self._inline_index.clear()

    # ── Navigation ──

    def push_nav(self, chat_id: int, msg_id: int, panel_id: str, extra: str = "") -> None:
        session = self.get(chat_id, msg_id)
        if session is None:
            self.create(chat_id, msg_id, panel_id, extra)
            return
        if session.nav_stack and session.nav_stack[-1] == (panel_id, extra):
            return
        session.nav_stack.append((panel_id, extra))

    def pop_nav(self, chat_id: int, msg_id: int) -> tuple[str, str] | None:
        session = self.get(chat_id, msg_id)
        if session is None:
            return None
        if len(session.nav_stack) <= 1:
            return None
        session.nav_stack.pop()
        if not session.nav_stack:
            return None
        return session.nav_stack[-1]

    def reset_nav(self, chat_id: int, msg_id: int, panel_id: str = "menu", extra: str = "") -> None:
        session = self.get(chat_id, msg_id)
        if session is None:
            self.create(chat_id, msg_id, panel_id, extra)
            return
        session.nav_stack = [(panel_id, extra)]

    def current_nav(self, chat_id: int, msg_id: int) -> tuple[str, str] | None:
        session = self.get(chat_id, msg_id)
        if session is None:
            return None
        if not session.nav_stack:
            return None
        return session.nav_stack[-1]

    def is_root_view(self, chat_id: int, msg_id: int) -> bool:
        session = self.get(chat_id, msg_id)
        if session is None:
            return True
        return len(session.nav_stack) <= 1

    def nav_depth(self, chat_id: int, msg_id: int) -> int:
        session = self.get(chat_id, msg_id)
        if session is None:
            return 0
        return len(session.nav_stack)

    def set_current_extra(self, chat_id: int, msg_id: int, extra: str) -> None:
        session = self.get(chat_id, msg_id)
        if session is None or not session.nav_stack:
            return
        top = session.nav_stack[-1]
        session.nav_stack[-1] = (top[0], extra)


# ── Backward-compatible module-level API ──
# These delegate to a singleton SessionManager instance used by the lifecycle manager.
# New code should use the lifecycle manager's .sessions property instead.

_manager = SessionManager()


def create_session(
    chat_id: int, msg_id: int, panel_type: str = "unknown",
    extra: str = "", inline_message_id: str = "",
) -> str:
    return _manager.create(chat_id, msg_id, panel_type, extra, inline_message_id)


def get_session(chat_id: int | None, msg_id: int | None) -> Session | None:
    return _manager.get(chat_id, msg_id)


def get_session_by_inline_id(inline_message_id: str | None) -> Session | None:
    return _manager.get_by_inline_id(inline_message_id)


def find_session_by_chat(chat_id: int) -> Session | None:
    return _manager.find_by_chat(chat_id)


def find_all_sessions_by_chat(chat_id: int) -> list[Session]:
    return _manager.find_all_by_chat(chat_id)


def find_session_by_msg_id(msg_id: int) -> Session | None:
    return _manager.find_by_msg_id(msg_id)


def destroy_all_sessions_for_chat(chat_id: int) -> list[tuple[int, int]]:
    return _manager.destroy_all_for_chat(chat_id)


def push_nav(chat_id: int, msg_id: int, panel_id: str, extra: str = "") -> None:
    _manager.push_nav(chat_id, msg_id, panel_id, extra)


def pop_nav(chat_id: int, msg_id: int) -> tuple[str, str] | None:
    return _manager.pop_nav(chat_id, msg_id)


def reset_nav(chat_id: int, msg_id: int, panel_id: str = "menu", extra: str = "") -> None:
    _manager.reset_nav(chat_id, msg_id, panel_id, extra)


def current_nav(chat_id: int, msg_id: int) -> tuple[str, str] | None:
    return _manager.current_nav(chat_id, msg_id)


def is_root_view(chat_id: int, msg_id: int) -> bool:
    return _manager.is_root_view(chat_id, msg_id)


def nav_depth(chat_id: int, msg_id: int) -> int:
    return _manager.nav_depth(chat_id, msg_id)


def set_current_extra(chat_id: int, msg_id: int, extra: str) -> None:
    _manager.set_current_extra(chat_id, msg_id, extra)


def clear_session(chat_id: int | None, msg_id: int | None) -> None:
    _manager.destroy(chat_id, msg_id)


def clear_all_sessions() -> None:
    _manager.clear_all()
