"""
Internal unit-test style checks for the AI Engine.

Run directly with::

    python -m backend.ai.engine.tests

Deterministic, offline, no network, no database. Exercises the full
execution flow through the DummyProvider and verifies failure handling,
metrics, hooks, and health.
"""
from __future__ import annotations

import sys
from typing import List, Tuple

from backend.ai.engine.engine import Engine
from backend.ai.engine.hooks import EngineHooks
from backend.ai.engine.result import EngineResult
from backend.ai.prompt.builder import PromptBuilder
from backend.ai.providers.factory import ProviderFactory
from backend.ai.runtime.manager import ConversationManager
from backend.ai.session.request import AIRequest


def _check(name: str, condition: bool, detail: str = "") -> bool:
    status = "ok" if condition else "FAIL"
    print(f"  [{status}] {name}{(' — ' + detail) if detail else ''}")
    return condition


def _make_request(owner_id: int = 1, message: str = "hello") -> AIRequest:
    return AIRequest(
        session_id=f"test-{owner_id}",
        user_message=message,
        owner_id=owner_id,
        chat_id=1,
        message_id=1,
    )


def test_engine_returns_result() -> bool:
    print("test_engine_returns_result")
    engine = Engine()
    result = engine.execute(_make_request())
    ok = True
    ok &= _check("returns EngineResult", isinstance(result, EngineResult))
    ok &= _check("provider is dummy", result.provider == "dummy")
    ok &= _check("latency > 0", result.latency >= 0.0)
    ok &= _check("total_tokens >= 0", result.total_tokens >= 0)
    ok &= _check("response is string", isinstance(result.response, str))
    return ok


def test_engine_health() -> bool:
    print("test_engine_health")
    engine = Engine()
    health = engine.engine_health()
    ok = True
    ok &= _check("health is READY", health == "READY", f"got {health!r}")
    return ok


def test_engine_health_module() -> bool:
    print("test_engine_health_module")
    from backend.ai.engine import engine_health
    health = engine_health()
    return _check("module health READY", health == "READY", f"got {health!r}")


def test_metrics_recorded() -> bool:
    print("test_metrics_recorded")
    engine = Engine()
    engine.execute(_make_request(owner_id=1))
    engine.execute(_make_request(owner_id=2))
    snap = engine.metrics_snapshot()
    ok = True
    ok &= _check("total_executions == 2", snap["total_executions"] == 2)
    ok &= _check("provider_usage has dummy", snap["provider_usage"].get("dummy", 0) == 2)
    ok &= _check("conversation_count == 2", snap["conversation_count"] == 2)
    ok &= _check("average_latency >= 0", snap["average_latency"] >= 0.0)
    return ok


def test_hooks_invoked() -> bool:
    print("test_hooks_invoked")

    calls: List[str] = []

    class TrackingHooks(EngineHooks):
        def before_execution(self, request):
            calls.append("before_execution")

        def after_prompt(self, prompt_package):
            calls.append("after_prompt")

        def after_provider(self, response):
            calls.append("after_provider")

        def after_response(self, result):
            calls.append("after_response")

        def on_error(self, error, stage):
            calls.append("on_error")

    engine = Engine(hooks=TrackingHooks())
    engine.execute(_make_request())
    ok = True
    ok &= _check("before_execution called", "before_execution" in calls)
    ok &= _check("after_prompt called", "after_prompt" in calls)
    ok &= _check("after_provider called", "after_provider" in calls)
    ok &= _check("after_response called", "after_response" in calls)
    ok &= _check("on_error NOT called", "on_error" not in calls)
    return ok


def test_failure_handling() -> bool:
    print("test_failure_handling")

    class _BrokenPromptBuilder:
        def build(self, context):
            raise RuntimeError("prompt boom")

    engine = Engine(prompt_builder=_BrokenPromptBuilder())  # type: ignore[arg-type]
    result = engine.execute(_make_request())
    ok = True
    ok &= _check("returns EngineResult", isinstance(result, EngineResult))
    ok &= _check("success is False", result.success is False)
    ok &= _check("errors non-empty", len(result.errors) > 0)
    ok &= _check("errors mention stage", any("prompt" in e for e in result.errors))
    ok &= _check("engine did not crash", True)
    return ok


def test_hooks_on_error() -> bool:
    print("test_hooks_on_error")

    errors: List[str] = []

    class ErrorHooks(EngineHooks):
        def on_error(self, error, stage):
            errors.append(f"{stage}: {error}")

    class _BrokenPromptBuilder:
        def build(self, context):
            raise RuntimeError("boom")

    engine = Engine(
        prompt_builder=_BrokenPromptBuilder(),  # type: ignore[arg-type]
        hooks=ErrorHooks(),
    )
    engine.execute(_make_request())
    return _check("on_error hook fired", len(errors) > 0, f"errors={errors}")


def test_immutable_result() -> bool:
    print("test_immutable_result")
    engine = Engine()
    result = engine.execute(_make_request())
    ok = True
    try:
        result.success = True  # type: ignore[misc]
        ok &= _check("result is frozen", False)
    except Exception:
        ok &= _check("result is frozen", True)
    return ok


def test_conversation_updated() -> bool:
    print("test_conversation_updated")
    engine = Engine()
    engine.execute(_make_request(owner_id=42, message="ping"))
    history = engine.conversation_manager.get_history(owner_id=42, n=10)
    ok = True
    ok &= _check("history has entries", len(history) >= 1)
    roles = [h.role for h in history]
    ok &= _check("user message recorded", "user" in roles)
    ok &= _check("assistant message recorded", "assistant" in roles)
    return ok


def run_all() -> int:
    tests: List[Tuple[str, callable]] = [
        ("engine_returns_result", test_engine_returns_result),
        ("engine_health", test_engine_health),
        ("engine_health_module", test_engine_health_module),
        ("metrics_recorded", test_metrics_recorded),
        ("hooks_invoked", test_hooks_invoked),
        ("failure_handling", test_failure_handling),
        ("hooks_on_error", test_hooks_on_error),
        ("immutable_result", test_immutable_result),
        ("conversation_updated", test_conversation_updated),
    ]
    failures = 0
    for name, fn in tests:
        print(f"== {name} ==")
        try:
            if not fn():
                failures += 1
                print(f"  !! {name} reported failures")
        except Exception as exc:  # noqa: BLE001
            failures += 1
            print(f"  !! {name} raised: {exc!r}")
    print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} TEST(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run_all())
