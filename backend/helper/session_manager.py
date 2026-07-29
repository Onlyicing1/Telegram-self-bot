"""
Session manager — owns all panel session state.

This module is the single owner of panel session data (navigation stacks,
session IDs, and the session registry). Both ``panels.py`` and
``panel_timer.py`` import from here, so neither needs to import the other
for session-related operations.

Session lifecycle:
  Create → Render → Wait → Action/Input → Update → Back/Home/Close → Destroy
  Nothing survives after Destroy.
"""
import logging

logger = logging.getLogger(__name__)

_session_counter: int = 0
_sessions: dict[tuple[int, int], dict] = {}


def create_session(chat_id: int, msg_id: int, panel_type: str = "unknown") -> str:
    """Create a new panel session with a unique ID."""
    global _session_counter
    _session_counter += 1
    sid = f"PANEL-SESSION-{_session_counter:06d}"
    _sessions[(chat_id, msg_id)] = {
        "session_id": sid,
        "chat_id": chat_id,
        "msg_id": msg_id,
        "panel_type": panel_type,
        "nav_stack": [panel_type],
    }
    return sid


def get_session(chat_id: int | None, msg_id: int | None) -> dict | None:
    """Look up session metadata by (chat_id, msg_id)."""
    if chat_id is None or msg_id is None:
        return None
    return _sessions.get((chat_id, msg_id))


def push_nav(chat_id: int, msg_id: int, panel_id: str) -> None:
    """Push a panel onto the navigation stack."""
    session = get_session(chat_id, msg_id)
    if session is None:
        create_session(chat_id, msg_id, panel_id)
        return
    stack = session.get("nav_stack", [])
    if stack and stack[-1] == panel_id:
        return
    stack.append(panel_id)
    session["nav_stack"] = stack


def pop_nav(chat_id: int, msg_id: int) -> str | None:
    """Pop the current panel off the navigation stack and return the previous one."""
    session = get_session(chat_id, msg_id)
    if session is None:
        return None
    stack = session.get("nav_stack", [])
    if len(stack) <= 1:
        return None
    stack.pop()
    return stack[-1] if stack else None


def reset_nav(chat_id: int, msg_id: int, panel_id: str = "help") -> None:
    """Reset the navigation stack to a single root panel.

    Used by the Home button to guarantee a clean stack with no duplicates.
    """
    session = get_session(chat_id, msg_id)
    if session is None:
        create_session(chat_id, msg_id, panel_id)
        return
    session["nav_stack"] = [panel_id]


def current_nav(chat_id: int, msg_id: int) -> str | None:
    """Return the current panel at the top of the navigation stack."""
    session = get_session(chat_id, msg_id)
    if session is None:
        return None
    stack = session.get("nav_stack", [])
    return stack[-1] if stack else None


def clear_session(chat_id: int | None, msg_id: int | None) -> None:
    """Remove a single session by (chat_id, msg_id)."""
    if chat_id is None or msg_id is None:
        return
    _sessions.pop((chat_id, msg_id), None)


def clear_all_sessions() -> None:
    """Remove all sessions."""
    _sessions.clear()
