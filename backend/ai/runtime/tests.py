"""
Internal unit-test style checks for the Conversation Runtime layer.

Run directly with::

    python -m backend.ai.runtime.tests

These are lightweight, deterministic, offline assertions — not a
pytest suite. They exercise the public API of the Conversation Runtime
layer and exit non-zero on any failure. No database, no network, no
AI provider.
"""
from __future__ import annotations

import sys
from typing import List, Tuple

from backend.ai.runtime.debug import (
    debug_add_messages,
    debug_create_session,
    debug_destroy,
    debug_trim,
)
from backend.ai.runtime.history import (
    ROLE_ASSISTANT,
    ROLE_SYSTEM,
    ROLE_TOOL,
    ROLE_USER,
    ConversationHistory,
)
from backend.ai.runtime.manager import ConversationManager
from backend.ai.runtime.registry import ConversationRegistry
from backend.ai.runtime.session import RuntimeSession
from backend.ai.runtime.tokens import estimate_tokens


def _check(name: str, condition: bool, detail: str = "") -> bool:
    status = "ok" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + detail) if detail else ''}")
    return condition


def test_tokens() -> bool:
    print("test_tokens")
    ok = True
    ok &= _check("empty -> 0", estimate_tokens("") == 0)
    ok &= _check("1 char -> 1", estimate_tokens("a") == 1)
    ok &= _check("4 chars -> 1", estimate_tokens("abcd") == 1)
    ok &= _check("5 chars -> 2", estimate_tokens("abcde") == 2)
    ok &= _check("deterministic", estimate_tokens("hello world!") == estimate_tokens("hello world!"))
    return ok


def test_history_roles_and_tokens() -> bool:
    print("test_history_roles_and_tokens")
    h = ConversationHistory()
    ok = True
    ok &= _check("starts empty", h.is_empty())
    h.add(ROLE_SYSTEM, "system prompt")
    h.add(ROLE_USER, "user message")
    h.add(ROLE_ASSISTANT, "assistant reply")
    h.add(ROLE_TOOL, "tool result")
    ok &= _check("size == 4", h.size() == 4)
    items = h.all_items()
    ok &= _check("roles recorded", [i.role for i in items] == [ROLE_SYSTEM, ROLE_USER, ROLE_ASSISTANT, ROLE_TOOL])
    ok &= _check("tokens stored", all(i.estimated_tokens > 0 for i in items))
    ok &= _check("total_tokens sums", h.total_tokens() == sum(i.estimated_tokens for i in items))
    try:
        h.add("bogus", "x")
        ok &= _check("unknown role raises", False)
    except ValueError:
        ok &= _check("unknown role raises", True)
    return ok


def test_trim_preserves_system_and_tool() -> bool:
    print("test_trim_preserves_system_and_tool")
    h = ConversationHistory()
    h.add(ROLE_SYSTEM, "system prompt that is somewhat long to spend tokens")
    for i in range(5):
        h.add(ROLE_USER, f"user message number {i} with some padding text")
        h.add(ROLE_ASSISTANT, f"assistant reply number {i} with some padding text")
    h.add(ROLE_TOOL, "latest tool result with some padding text")
    before = h.size()
    removed = h.trim_to_budget(40)
    after = h.size()
    items = h.all_items()
    roles = [i.role for i in items]
    ok = True
    ok &= _check("removed something", removed > 0, f"before={before} after={after}")
    ok &= _check("system preserved", roles[0] == ROLE_SYSTEM)
    ok &= _check("latest tool preserved", roles[-1] == ROLE_TOOL)
    # idempotent
    removed2 = h.trim_to_budget(40)
    ok &= _check("idempotent", removed2 == 0)
    ok &= _check("within budget", h.total_tokens() <= 40 or after == 1)
    return ok


def test_registry_single_session_per_owner() -> bool:
    print("test_registry_single_session_per_owner")
    reg = ConversationRegistry()
    ok = True
    s1 = reg.create_session(owner_id=42)
    s2 = reg.create_session(owner_id=42)
    ok &= _check("same instance returned", s1 is s2)
    ok &= _check("count == 1", reg.count() == 1)
    ok &= _check("get returns it", reg.get_session(42) is s1)
    ok &= _check("delete returns True", reg.delete_session(42) is True)
    ok &= _check("delete again False", reg.delete_session(42) is False)
    ok &= _check("count == 0", reg.count() == 0)
    return ok


def test_manager_lifecycle() -> bool:
    print("test_manager_lifecycle")
    mgr = ConversationManager(idle_timeout_seconds=60, token_budget=1000)
    ok = True
    s = mgr.create_session(owner_id=7)
    ok &= _check("session created", s is not None)
    ok &= _check("owner_id set", s.owner_id == 7)
    ok &= _check("session_id string", isinstance(s.session_id, str) and len(s.session_id) > 0)
    ok &= _check("active_count 1", mgr.active_count() == 1)
    got = mgr.get_session(owner_id=7)
    ok &= _check("get returns same", got is s)
    # single session per owner
    s_again = mgr.create_session(owner_id=7)
    ok &= _check("create again returns same", s_again is s)
    ok &= _check("still count 1", mgr.active_count() == 1)
    # reset
    mgr.add_user_message(owner_id=7, content="hello")
    ok &= _check("history grew", s.conversation_history.size() >= 1)
    mgr.reset_session(owner_id=7)
    ok &= _check("history cleared on reset", all(i.role == ROLE_SYSTEM for i in s.conversation_history.all_items()) or s.conversation_history.is_empty())
    # close
    ok &= _check("close True", mgr.close_session(owner_id=7) is True)
    ok &= _check("count 0 after close", mgr.active_count() == 0)
    ok &= _check("get None after close", mgr.get_session(owner_id=7) is None)
    return ok


def test_manager_system_prompt_and_messages() -> bool:
    print("test_manager_system_prompt_and_messages")
    mgr = ConversationManager(token_budget=10000)
    ok = True
    mgr.set_system_prompt(owner_id=9, prompt="be brief")
    s = mgr.get_session(owner_id=9)
    ok &= _check("system prompt set", s.system_prompt == "be brief")
    items = s.conversation_history.all_items()
    ok &= _check("one system in history", sum(1 for i in items if i.role == ROLE_SYSTEM) == 1)
    mgr.add_user_message(owner_id=9, content="hi")
    mgr.add_assistant_message(owner_id=9, content="hello")
    mgr.add_tool_result(owner_id=9, tool_name="save", result="saved")
    items = s.conversation_history.all_items()
    roles = [i.role for i in items]
    ok &= _check("roles order", roles == [ROLE_SYSTEM, ROLE_USER, ROLE_ASSISTANT, ROLE_TOOL])
    ok &= _check("tool_history recorded", len(s.tool_history) == 1)
    ok &= _check("pending_tool cleared", s.pending_tool is None)
    # replace system prompt keeps at most one system entry
    mgr.set_system_prompt(owner_id=9, prompt="be verbose")
    items = s.conversation_history.all_items()
    ok &= _check("still one system", sum(1 for i in items if i.role == ROLE_SYSTEM) == 1)
    ok &= _check("system is newest prompt", items[0].content == "be verbose")
    return ok


def test_manager_trim_on_budget() -> bool:
    print("test_manager_trim_on_budget")
    mgr = ConversationManager(token_budget=60)
    ok = True
    mgr.set_system_prompt(owner_id=11, prompt="system prompt padding text here")
    for i in range(6):
        mgr.add_user_message(owner_id=11, content=f"user message number {i} padded")
        mgr.add_assistant_message(owner_id=11, content=f"assistant reply number {i} padded")
    s = mgr.get_session(owner_id=11)
    ok &= _check("trimmed to budget", s.token_estimate <= 60 or s.conversation_history.size() == 1)
    items = s.conversation_history.all_items()
    ok &= _check("system kept", items[0].role == ROLE_SYSTEM)
    return ok


def test_idle_cleanup() -> bool:
    print("test_idle_cleanup")
    ok = True
    # timeout of 0 disables cleanup
    mgr = ConversationManager(idle_timeout_seconds=0)
    mgr.create_session(owner_id=1)
    removed = mgr.cleanup_idle()
    ok &= _check("disabled cleanup removes 0", removed == 0)
    ok &= _check("session still present", mgr.get_session(owner_id=1) is not None)
    # very short timeout + manual backdate
    mgr2 = ConversationManager(idle_timeout_seconds=1)
    s = mgr2.create_session(owner_id=2)
    from datetime import datetime, timedelta, timezone
    s.last_activity = datetime.now(timezone.utc) - timedelta(seconds=60)
    removed2 = mgr2.cleanup_idle()
    ok &= _check("idle session reaped", removed2 == 1)
    ok &= _check("gone after reap", mgr2.get_session(owner_id=2) is None)
    return ok


def test_debug_helpers() -> bool:
    print("test_debug_helpers")
    ok = True
    mgr, s = debug_create_session(owner_id=1, token_budget=1000)
    ok &= _check("debug create", s is not None)
    debug_add_messages(mgr, owner_id=1, turns=3)
    ok &= _check("messages added", s.conversation_history.size() >= 6)
    removed = debug_trim(mgr, owner_id=1, budget=20)
    ok &= _check("debug trim removed some", removed > 0)
    debug_destroy(mgr, owner_id=1)
    ok &= _check("debug destroy", mgr.active_count() == 0)
    return ok


def run_all() -> int:
    tests: List[Tuple[str, callable]] = [
        ("tokens", test_tokens),
        ("history_roles_and_tokens", test_history_roles_and_tokens),
        ("trim_preserves_system_and_tool", test_trim_preserves_system_and_tool),
        ("registry_single_session_per_owner", test_registry_single_session_per_owner),
        ("manager_lifecycle", test_manager_lifecycle),
        ("manager_system_prompt_and_messages", test_manager_system_prompt_and_messages),
        ("manager_trim_on_budget", test_manager_trim_on_budget),
        ("idle_cleanup", test_idle_cleanup),
        ("debug_helpers", test_debug_helpers),
    ]
    failures = 0
    for name, fn in tests:
        print(f"== {name} ==")
        try:
            if not fn():
                failures += 1
                print(f"  !! {name} reported failures")
        except Exception as exc:  # noqa: BLE001 — surface any error as a failure
            failures += 1
            print(f"  !! {name} raised: {exc!r}")
    print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} TEST(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run_all())
