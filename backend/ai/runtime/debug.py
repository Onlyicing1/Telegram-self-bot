"""
Debug helpers for the Conversation Runtime layer.

These are DEVELOPER-ONLY utilities for exercising the runtime in a REPL
or a throwaway script. They are NOT wired into the bot menu, the web
UI, or any production code path. They make no network calls and touch
no database.

Usage (from a Python shell)::

    from backend.ai.runtime.debug import (
        debug_create_session,
        debug_add_messages,
        debug_trim,
        debug_destroy,
    )

    mgr, session = debug_create_session(owner_id=1)
    debug_add_messages(mgr, owner_id=1, turns=3)
    debug_trim(mgr, owner_id=1, budget=50)
    debug_destroy(mgr)

Each helper prints a short, human-readable report so a developer can
 eyeball the runtime state without writing a full test.
"""
from __future__ import annotations

from typing import Any, Dict, List, Tuple

from backend.ai.runtime.manager import (
    DEFAULT_IDLE_TIMEOUT_SECONDS,
    DEFAULT_TOKEN_BUDGET,
    ConversationManager,
)
from backend.ai.runtime.session import RuntimeSession


def debug_create_session(
    owner_id: int = 1,
    idle_timeout_seconds: int = DEFAULT_IDLE_TIMEOUT_SECONDS,
    token_budget: int = DEFAULT_TOKEN_BUDGET,
    system_prompt: str = "You are a helpful assistant.",
) -> Tuple[ConversationManager, RuntimeSession]:
    """Construct a fresh manager and create one session for ``owner_id``.

    Returns ``(manager, session)``. Prints a summary.
    """
    mgr = ConversationManager(
        idle_timeout_seconds=idle_timeout_seconds,
        token_budget=token_budget,
    )
    session = mgr.create_session(owner_id=owner_id)
    if system_prompt:
        mgr.set_system_prompt(owner_id=owner_id, prompt=system_prompt)
    print(
        f"[debug] created session '{session.session_id}' for owner {owner_id} "
        f"(idle_timeout={idle_timeout_seconds}s, budget={token_budget})"
    )
    return mgr, session


def debug_add_messages(
    mgr: ConversationManager,
    owner_id: int = 1,
    turns: int = 3,
    user_prefix: str = "Hello turn",
    assistant_prefix: str = "Hi! I heard turn",
) -> List[str]:
    """Append ``turns`` user/assistant message pairs to the owner's session.

    Returns the list of created message contents. Prints a summary.
    """
    contents: List[str] = []
    for i in range(turns):
        u = f"{user_prefix} {i}"
        a = f"{assistant_prefix} {i}"
        mgr.add_user_message(owner_id=owner_id, content=u)
        mgr.add_assistant_message(owner_id=owner_id, content=a)
        contents.extend([u, a])
    session = mgr.get_session(owner_id=owner_id)
    count = session.conversation_history.size() if session else 0
    tokens = session.token_estimate if session else 0
    print(
        f"[debug] added {turns} turns -> history size={count}, tokens≈{tokens}"
    )
    return contents


def debug_trim(
    mgr: ConversationManager, owner_id: int = 1, budget: int = 50
) -> int:
    """Force the manager to trim the owner's history to ``budget`` tokens.

    Temporarily overrides the manager's token budget for this call only,
    so the configured budget is not permanently changed. Returns the
    number of items removed. Prints a before/after summary.
    """
    session = mgr.get_session(owner_id=owner_id)
    before = session.conversation_history.size() if session else 0
    before_tokens = session.token_estimate if session else 0
    original_budget = mgr.token_budget
    # Trim directly against the requested budget without mutating config.
    removed = 0
    if session is not None:
        removed = session.conversation_history.trim_to_budget(budget)
        session._refresh_token_estimate()  # noqa: SLF001 — debug-only coordination
    after = session.conversation_history.size() if session else 0
    after_tokens = session.token_estimate if session else 0
    print(
        f"[debug] trim(budget={budget}): {before}→{after} items, "
        f"{before_tokens}→{after_tokens} tokens, removed={removed} "
        f"(manager budget unchanged at {original_budget})"
    )
    return removed


def debug_destroy(mgr: ConversationManager, owner_id: int = 1) -> bool:
    """Close the owner's session and report the registry state."""
    removed = mgr.close_session(owner_id=owner_id)
    remaining = mgr.active_count()
    print(
        f"[debug] destroy owner={owner_id}: removed={removed}, "
        f"remaining sessions={remaining}"
    )
    return removed
