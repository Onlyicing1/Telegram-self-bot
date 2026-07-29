"""
Session manager — owns all panel session state.

This module is the single owner of panel session data (navigation stacks,
session IDs, and the session registry). Both ``panels.py`` and
``panel_timer.py`` import from here, so neither needs to import the other
for session-related operations.

The navigation stack stores (panel_id, extra) tuples so that different
views within the same panel (e.g. help root vs help category) are
distinguishable.  Root is always stack length 1.

Session lifecycle:
  Create → Render → Wait → Action/Input → Update → Back/Home/Close → Destroy
  Nothing survives after Destroy.
"""
import logging

logger = logging.getLogger(__name__)

_session_counter: int = 0
_sessions: dict[tuple[int, int], dict] = {}


def create_session(chat_id: int, msg_id: int, panel_type: str = "unknown", extra: str = "") -> str:
    """Create a new panel session with a unique ID."""
    global _session_counter
    _session_counter += 1
    sid = f"PANEL-SESSION-{_session_counter:06d}"
    _sessions[(chat_id, msg_id)] = {
        "session_id": sid,
        "chat_id": chat_id,
        "msg_id": msg_id,
        "panel_type": panel_type,
        "nav_stack": [(panel_type, extra)],
    }
    return sid


def get_session(chat_id: int | None, msg_id: int | None) -> dict | None:
    if chat_id is None or msg_id is None:
        return None
    return _sessions.get((chat_id, msg_id))


def find_session_by_chat(chat_id: int) -> dict | None:
    for (cid, _mid), session in _sessions.items():
        if cid == chat_id:
            return session
    return None


def push_nav(chat_id: int, msg_id: int, panel_id: str, extra: str = "") -> None:
    """Push a (panel_id, extra) view onto the navigation stack.

    Skips if the exact same view is already on top.
    """
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
    """Pop the current view and return the previous (panel_id, extra), or None."""
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
    """Reset the navigation stack to a single root view."""
    session = get_session(chat_id, msg_id)
    if session is None:
        create_session(chat_id, msg_id, panel_id, extra)
        return
    session["nav_stack"] = [(panel_id, extra)]


def current_nav(chat_id: int, msg_id: int) -> tuple[str, str] | None:
    """Return the (panel_id, extra) at the top of the stack, or None."""
    session = get_session(chat_id, msg_id)
    if session is None:
        return None
    stack = session.get("nav_stack", [])
    if not stack:
        return None
    top = stack[-1]
    return (top[0], top[1]) if isinstance(top, tuple) else (top, "")


def is_root_view(chat_id: int, msg_id: int) -> bool:
    """True when the current view is the root (stack length <= 1)."""
    session = get_session(chat_id, msg_id)
    if session is None:
        return True
    stack = session.get("nav_stack", [])
    return len(stack) <= 1


def nav_depth(chat_id: int, msg_id: int) -> int:
    """Return the current navigation stack depth."""
    session = get_session(chat_id, msg_id)
    if session is None:
        return 0
    return len(session.get("nav_stack", []))


def set_current_extra(chat_id: int, msg_id: int, extra: str) -> None:
    """Update extra on the topmost stack entry."""
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
    _sessions.pop((chat_id, msg_id), None)


def clear_all_sessions() -> None:
    _sessions.clear()
