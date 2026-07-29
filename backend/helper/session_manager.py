"""
Session manager — owns all panel session state.

The navigation stack stores (panel_id, extra) tuples so that different
views within the same panel (e.g. help root vs help category) are
distinguishable.  Root is always stack length 1.

Sessions are keyed by (chat_id, msg_id).  For inline messages that lack
a msg_id, the inline_message_id string is used as a secondary lookup key.
"""
import logging

logger = logging.getLogger(__name__)

_session_counter: int = 0
_sessions: dict[tuple[int, int], dict] = {}
_inline_sessions: dict[str, tuple[int, int]] = {}


def create_session(
    chat_id: int, msg_id: int, panel_type: str = "unknown",
    extra: str = "", inline_message_id: str = "",
) -> str:
    global _session_counter
    _session_counter += 1
    sid = f"PANEL-SESSION-{_session_counter:06d}"
    _sessions[(chat_id, msg_id)] = {
        "session_id": sid,
        "chat_id": chat_id,
        "msg_id": msg_id,
        "panel_type": panel_type,
        "nav_stack": [(panel_type, extra)],
        "inline_message_id": inline_message_id,
    }
    if inline_message_id:
        _inline_sessions[inline_message_id] = (chat_id, msg_id)
    return sid


def get_session(chat_id: int | None, msg_id: int | None) -> dict | None:
    if chat_id is None or msg_id is None:
        return None
    return _sessions.get((chat_id, msg_id))


def get_session_by_inline_id(inline_message_id: str | None) -> dict | None:
    if not inline_message_id:
        return None
    key = _inline_sessions.get(inline_message_id)
    if key is None:
        return None
    return _sessions.get(key)


def find_session_by_chat(chat_id: int) -> dict | None:
    for (cid, _mid), session in _sessions.items():
        if cid == chat_id:
            return session
    return None


def find_session_by_msg_id(msg_id: int) -> dict | None:
    for (_cid, mid), session in _sessions.items():
        if mid == msg_id:
            return session
    return None


def push_nav(chat_id: int, msg_id: int, panel_id: str, extra: str = "") -> None:
    session = get_session(chat_id, msg_id)
    if session is None:
        create_session(chat_id, msg_id, panel_id, extra)
        return
    stack = session.get("nav_stack", [])
    if stack and stack[-1] == (panel_id, extra):
        return
    stack.append((panel_id, extra))
    session["nav_stack"] = stack


def pop_nav(chat_id: int, msg_id: int) -> tuple[str, str] | None:
    session = get_session(chat_id, msg_id)
    if session is None:
        return None
    stack = session.get("nav_stack", [])
    if len(stack) <= 1:
        return None
    stack.pop()
    if not stack:
        return None
    top = stack[-1]
    return (top[0], top[1]) if isinstance(top, tuple) else (top, "")


def reset_nav(chat_id: int, msg_id: int, panel_id: str = "help", extra: str = "") -> None:
    session = get_session(chat_id, msg_id)
    if session is None:
        create_session(chat_id, msg_id, panel_id, extra)
        return
    session["nav_stack"] = [(panel_id, extra)]


def current_nav(chat_id: int, msg_id: int) -> tuple[str, str] | None:
    session = get_session(chat_id, msg_id)
    if session is None:
        return None
    stack = session.get("nav_stack", [])
    if not stack:
        return None
    top = stack[-1]
    return (top[0], top[1]) if isinstance(top, tuple) else (top, "")


def is_root_view(chat_id: int, msg_id: int) -> bool:
    session = get_session(chat_id, msg_id)
    if session is None:
        return True
    stack = session.get("nav_stack", [])
    return len(stack) <= 1


def nav_depth(chat_id: int, msg_id: int) -> int:
    session = get_session(chat_id, msg_id)
    if session is None:
        return 0
    return len(session.get("nav_stack", []))


def set_current_extra(chat_id: int, msg_id: int, extra: str) -> None:
    session = get_session(chat_id, msg_id)
    if session is None:
        return
    stack = session.get("nav_stack", [])
    if not stack:
        return
    top = stack[-1]
    if isinstance(top, tuple):
        stack[-1] = (top[0], extra)
    else:
        stack[-1] = (top, extra)
    session["nav_stack"] = stack


def clear_session(chat_id: int | None, msg_id: int | None) -> None:
    if chat_id is None or msg_id is None:
        return
    session = _sessions.pop((chat_id, msg_id), None)
    if session:
        imid = session.get("inline_message_id", "")
        if imid:
            _inline_sessions.pop(imid, None)


def clear_all_sessions() -> None:
    _sessions.clear()
    _inline_sessions.clear()
