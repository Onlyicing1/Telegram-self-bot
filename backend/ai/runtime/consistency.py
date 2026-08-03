"""
Internal Consistency Validator — verifies that every AI layer is
correctly wired to its dependencies.

Each check returns ``"PASS"`` or ``"FAIL: <reason>"``.
"""
from __future__ import annotations

from backend.ai.engine.result import EngineResult


def check_engine_without_provider() -> str:
    try:
        from backend.ai.providers.registry import ProviderRegistry
        reg = ProviderRegistry()
        if reg.default_provider() is None:
            return "FAIL: no default provider"
        return "PASS"
    except Exception as exc:
        return f"FAIL: {exc}"


def check_provider_without_config() -> str:
    try:
        from backend.ai.providers.dummy import DummyProvider
        p = DummyProvider()
        if p.config is None:
            return "FAIL: provider has no config"
        if not hasattr(p.config, "name"):
            return "FAIL: config has no name field"
        return "PASS"
    except Exception as exc:
        return f"FAIL: {exc}"


def check_conversation_without_engine() -> str:
    try:
        from backend.ai.runtime.manager import ConversationManager
        mgr = ConversationManager()
        session = mgr.create_session(owner_id=1)
        if session is None:
            return "FAIL: create_session returned None"
        mgr.add_user_message(owner_id=1, content="test")
        history = mgr.get_history(owner_id=1, n=5)
        if len(history) == 0:
            return "FAIL: history empty after add_user_message"
        return "PASS"
    except Exception as exc:
        return f"FAIL: {exc}"


def check_broken_prompt_builder() -> str:
    try:
        from backend.ai.conversation.context_builder import ContextBuilder
        from backend.ai.conversation.state import ConversationState
        from backend.ai.prompt.builder import PromptBuilder

        class _S:
            session_id = "v"
            owner_id = 0
            chat_id = 0
            state = ConversationState.IDLE
            current_panel = ""
            current_category = ""
            current_flow = ""
            pending_action = ""
            language = "English"
            timezone = "UTC"
            current_tool = ""
            last_tool = ""

        ctx = ContextBuilder().build(session=_S(), user_text="hi", message_id=0)
        pkg = PromptBuilder().build(ctx)
        if not pkg.system_prompt.strip():
            return "FAIL: system_prompt empty"
        if not pkg.user_input.strip():
            return "FAIL: user_input empty"
        return "PASS"
    except Exception as exc:
        return f"FAIL: {exc}"


def check_broken_context_builder() -> str:
    try:
        from backend.ai.conversation.context_builder import ContextBuilder
        from backend.ai.conversation.state import ConversationState

        class _S:
            session_id = "v"
            owner_id = 0
            chat_id = 0
            state = ConversationState.IDLE
            current_panel = ""
            current_category = ""
            current_flow = ""
            pending_action = ""
            language = "English"
            timezone = "UTC"
            current_tool = ""
            last_tool = ""

        ctx = ContextBuilder().build(session=_S(), user_text="x", message_id=0)
        if ctx.user_text != "x":
            return "FAIL: user_text mismatch"
        if ctx.session_id != "v":
            return "FAIL: session_id mismatch"
        return "PASS"
    except Exception as exc:
        return f"FAIL: {exc}"


def check_full_pipeline_integration() -> str:
    try:
        from backend.ai.engine.engine import Engine
        from backend.ai.session.request import AIRequest

        engine = Engine()
        req = AIRequest(
            session_id="consistency-test",
            user_message="Hello, AI!",
            owner_id=99999,
            chat_id=0,
            message_id=0,
        )
        result = engine.execute(req)
        if not isinstance(result, EngineResult):
            return "FAIL: execute did not return EngineResult"
        if not result.success:
            return f"FAIL: execution unsuccessful — {result.errors}"
        if not result.response:
            return "FAIL: empty response"
        if result.provider != "dummy":
            return f"FAIL: wrong provider '{result.provider}'"
        if result.total_tokens <= 0:
            return "FAIL: zero total_tokens"
        return "PASS"
    except Exception as exc:
        return f"FAIL: {exc}"


def run_all_consistency_checks() -> list[tuple[str, str]]:
    return [
        ("engine_without_provider", check_engine_without_provider()),
        ("provider_without_config", check_provider_without_config()),
        ("conversation_without_engine", check_conversation_without_engine()),
        ("broken_prompt_builder", check_broken_prompt_builder()),
        ("broken_context_builder", check_broken_context_builder()),
        ("full_pipeline_integration", check_full_pipeline_integration()),
    ]
