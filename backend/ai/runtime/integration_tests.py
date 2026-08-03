"""
AI Integration Tests — verify every backend AI layer communicates
correctly through its public interface.

Run directly::

    python -m backend.ai.runtime.integration_tests

No Telegram. No database. No external APIs. No pytest.
"""
from __future__ import annotations

import sys
from typing import List, Tuple

from backend.ai.engine.engine import Engine
from backend.ai.engine.result import EngineResult
from backend.ai.runtime.report import RuntimeReport, build_report
from backend.ai.runtime.consistency import run_all_consistency_checks


def _ok(name: str, detail: str = "") -> bool:
    status = "PASS" if not detail else f"PASS ({detail})"
    print(f"  [{status}] {name}")
    return True


def _fail(name: str, reason: str) -> bool:
    print(f"  [FAIL] {name} — {reason}")
    return False


def integration_test_pipeline() -> bool:
    print("integration_test_pipeline")
    from backend.ai.session.request import AIRequest

    engine = Engine()
    req = AIRequest(
        session_id="it-pipeline",
        user_message="Save the replied message as forward.",
        owner_id=11111,
        chat_id=0,
        message_id=0,
    )
    result = engine.execute(req)

    ok = True
    ok &= _ok("returns EngineResult") if isinstance(result, EngineResult) else _fail("returns EngineResult", f"got {type(result)}")
    ok &= _ok("success=True") if result.success else _fail("success=True", f"success={result.success}, errors={result.errors}")
    ok &= _ok("provider=dummy") if result.provider == "dummy" else _fail("provider=dummy", f"provider={result.provider}")
    ok &= _ok("response non-empty") if result.response else _fail("response non-empty", "empty response")
    ok &= _ok("total_tokens > 0") if result.total_tokens > 0 else _fail("total_tokens > 0", f"total_tokens={result.total_tokens}")
    ok &= _ok("latency >= 0") if result.latency >= 0 else _fail("latency >= 0", f"latency={result.latency}")
    ok &= _ok("stages recorded") if len(result.metadata.get("stages", [])) >= 5 else _fail("stages recorded", f"stages={result.metadata.get('stages')}")

    history = engine.conversation_manager.get_history(owner_id=11111, n=10)
    ok &= _ok("history has user+assistant") if len(history) >= 2 else _fail("history has user+assistant", f"len={len(history)}")

    snap = engine.metrics_snapshot()
    ok &= _ok("metrics recorded execution") if snap["total_executions"] >= 1 else _fail("metrics recorded execution", f"total={snap['total_executions']}")
    return ok


def integration_test_provider() -> bool:
    print("integration_test_provider")
    from backend.ai.providers.factory import ProviderFactory
    from backend.ai.providers.base import ProviderResponse

    registry = ProviderFactory.create_registry()
    ok = True
    ok &= _ok("registry has dummy") if registry.has("dummy") else _fail("registry has dummy", "missing")
    provider = registry.default_provider()
    ok &= _ok("default is dummy") if provider.name == "dummy" else _fail("default is dummy", f"name={provider.name}")

    health = provider.health()
    ok &= _ok("health healthy=True") if health.get("healthy") else _fail("health healthy=True", f"health={health}")

    from backend.ai.conversation.context_builder import ContextBuilder
    from backend.ai.conversation.state import ConversationState
    from backend.ai.prompt.builder import PromptBuilder

    class _S:
        session_id = "p"
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

    ctx = ContextBuilder().build(session=_S(), user_text="test", message_id=0)
    pkg = PromptBuilder().build(ctx)
    resp = provider.generate(pkg)
    ok &= _ok("returns ProviderResponse") if isinstance(resp, ProviderResponse) else _fail("returns ProviderResponse", f"got {type(resp)}")
    ok &= _ok("response success=True") if resp.success else _fail("response success=True", f"success={resp.success}")
    ok &= _ok("usage has tokens") if resp.usage.get("prompt_tokens", 0) > 0 else _fail("usage has tokens", f"usage={resp.usage}")
    return ok


def integration_test_conversation() -> bool:
    print("integration_test_conversation")
    from backend.ai.runtime.manager import ConversationManager

    mgr = ConversationManager()
    ok = True
    session = mgr.create_session(owner_id=22222)
    ok &= _ok("session created") if session is not None else _fail("session created", "None")

    mgr.add_user_message(owner_id=22222, content="Hello")
    mgr.add_assistant_message(owner_id=22222, content="Hi there")
    history = mgr.get_history(owner_id=22222, n=10)
    ok &= _ok("history has 2 entries") if len(history) == 2 else _fail("history has 2 entries", f"len={len(history)}")
    ok &= _ok("first is user") if history[0].role == "user" else _fail("first is user", f"role={history[0].role}")
    ok &= _ok("second is assistant") if history[1].role == "assistant" else _fail("second is assistant", f"role={history[1].role}")

    closed = mgr.close_session(owner_id=22222)
    ok &= _ok("session closed") if closed else _fail("session closed", "not closed")
    ok &= _ok("history cleared") if len(mgr.get_history(owner_id=22222)) == 0 else _fail("history cleared", "still has entries")
    return ok


def integration_test_metrics() -> bool:
    print("integration_test_metrics")
    engine = Engine()
    from backend.ai.session.request import AIRequest

    req = AIRequest(
        session_id="it-metrics",
        user_message="test message",
        owner_id=33333,
        chat_id=0,
        message_id=0,
    )
    engine.execute(req)
    engine.execute(req)

    snap = engine.metrics_snapshot()
    ok = True
    ok &= _ok("total_executions == 2") if snap["total_executions"] == 2 else _fail("total_executions == 2", f"total={snap['total_executions']}")
    ok &= _ok("successful >= 2") if snap["successful_executions"] >= 2 else _fail("successful >= 2", f"success={snap['successful_executions']}")
    ok &= _ok("provider_usage has dummy") if snap["provider_usage"].get("dummy", 0) >= 2 else _fail("provider_usage has dummy", f"usage={snap['provider_usage']}")
    ok &= _ok("conversation_count >= 1") if snap["conversation_count"] >= 1 else _fail("conversation_count >= 1", f"count={snap['conversation_count']}")
    ok &= _ok("total_prompt_tokens > 0") if snap["total_prompt_tokens"] > 0 else _fail("total_prompt_tokens > 0", f"tokens={snap['total_prompt_tokens']}")
    return ok


def integration_test_configuration() -> bool:
    print("integration_test_configuration")
    from backend.ai.config import ConfigManager, ConfigSnapshot, ConfigValidationError

    mgr = ConfigManager()
    ok = True
    ok &= _ok("enabled defaults False") if mgr.get("enabled") is False else _fail("enabled defaults False", f"enabled={mgr.get('enabled')}")
    ok &= _ok("provider defaults dummy") if mgr.get("provider") == "dummy" else _fail("provider defaults dummy", f"provider={mgr.get('provider')}")

    mgr.set("temperature", 0.7)
    ok &= _ok("temperature set") if mgr.get("temperature") == 0.7 else _fail("temperature set", f"temp={mgr.get('temperature')}")

    try:
        mgr.set("temperature", 5.0)
        ok &= _fail("invalid temperature rejected", "accepted 5.0")
    except ConfigValidationError:
        ok &= _ok("invalid temperature rejected")

    snap = mgr.snapshot()
    ok &= _ok("snapshot is ConfigSnapshot") if isinstance(snap, ConfigSnapshot) else _fail("snapshot is ConfigSnapshot", f"got {type(snap)}")
    ok &= _ok("snapshot temperature=0.7") if snap.temperature == 0.7 else _fail("snapshot temperature=0.7", f"temp={snap.temperature}")

    try:
        snap.temperature = 1.0
        ok &= _fail("snapshot frozen", "mutation succeeded")
    except Exception:
        ok &= _ok("snapshot frozen")

    errors = mgr.validate()
    ok &= _ok("config valid") if len(errors) == 0 else _fail("config valid", str(errors))
    return ok


def integration_test_runtime_report() -> bool:
    print("integration_test_runtime_report")
    from backend.ai.session.request import AIRequest

    engine = Engine()
    req = AIRequest(
        session_id="it-report",
        user_message="report test",
        owner_id=44444,
        chat_id=0,
        message_id=0,
    )
    engine.execute(req)
    report = build_report(engine)

    ok = True
    ok &= _ok("is RuntimeReport") if isinstance(report, RuntimeReport) else _fail("is RuntimeReport", f"got {type(report)}")
    ok &= _ok("conversation OK") if report.conversation_status == "OK" else _fail("conversation OK", report.conversation_status)
    ok &= _ok("prompt OK") if report.prompt_status == "OK" else _fail("prompt OK", report.prompt_status)
    ok &= _ok("engine OK") if report.engine_status == "OK" else _fail("engine OK", report.engine_status)
    ok &= _ok("provider OK") if report.provider_status == "OK" else _fail("provider OK", report.provider_status)
    ok &= _ok("configuration OK") if report.configuration_status == "OK" else _fail("configuration OK", report.configuration_status)
    ok &= _ok("metrics OK") if report.metrics_status == "OK" else _fail("metrics OK", report.metrics_status)
    ok &= _ok("overall OK") if report.overall_status == "OK" else _fail("overall OK", report.overall_status)
    return ok


def integration_test_consistency_validation() -> bool:
    print("integration_test_consistency_validation")
    results = run_all_consistency_checks()
    ok = True
    for name, result in results:
        if result == "PASS":
            ok &= _ok(name)
        else:
            ok &= _fail(name, result)
    return ok


def run_all() -> int:
    tests: List[Tuple[str, callable]] = [
        ("pipeline", integration_test_pipeline),
        ("provider", integration_test_provider),
        ("conversation", integration_test_conversation),
        ("metrics", integration_test_metrics),
        ("configuration", integration_test_configuration),
        ("runtime_report", integration_test_runtime_report),
        ("consistency_validation", integration_test_consistency_validation),
    ]
    failures = 0
    for name, fn in tests:
        print(f"== {name} ==")
        try:
            if not fn():
                failures += 1
        except Exception as exc:
            failures += 1
            print(f"  [FAIL] {name} raised: {exc!r}")
    print(f"\n{'ALL PASSED' if failures == 0 else f'{failures} TEST(S) FAILED'}")
    return 1 if failures else 0


if __name__ == "__main__":
    sys.exit(run_all())
