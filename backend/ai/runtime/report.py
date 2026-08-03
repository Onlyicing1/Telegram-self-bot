"""
AI Runtime Report — a developer-only diagnostic snapshot of the entire
AI subsystem.

Produced by ``build_report(engine)``. Contains the status of every
layer: conversation, prompt, engine, provider, configuration, and
metrics. Each layer reports ``"OK"`` or ``"FAIL: <reason>"``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any

from backend.ai.engine.engine import Engine


@dataclass(frozen=True)
class RuntimeReport:
    """Immutable diagnostic snapshot of the AI subsystem."""

    conversation_status: str
    prompt_status: str
    engine_status: str
    provider_status: str
    configuration_status: str
    metrics_status: str
    overall_status: str
    details: dict[str, Any] = field(default_factory=dict)

    def is_healthy(self) -> bool:
        return self.overall_status == "OK"

    def summary(self) -> str:
        lines = [
            f"Conversation:  {self.conversation_status}",
            f"Prompt:         {self.prompt_status}",
            f"Engine:         {self.engine_status}",
            f"Provider:       {self.provider_status}",
            f"Configuration:  {self.configuration_status}",
            f"Metrics:        {self.metrics_status}",
            f"Overall:        {self.overall_status}",
        ]
        return "\n".join(lines)


def _check_conversation(engine: Engine) -> tuple[str, dict[str, Any]]:
    mgr = engine.conversation_manager
    if mgr is None:
        return "FAIL: no conversation manager", {}
    if mgr.active_count() < 0:
        return "FAIL: negative active count", {}
    return "OK", {"active_sessions": mgr.active_count()}


def _check_prompt(engine: Engine) -> tuple[str, dict[str, Any]]:
    try:
        from backend.ai.conversation.context_builder import ContextBuilder
        from backend.ai.conversation.state import ConversationState
        from backend.ai.prompt.builder import PromptBuilder

        class _MockSession:
            session_id = "report-check"
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

        ctx = ContextBuilder().build(
            session=_MockSession(),
            user_text="test",
            message_id=0,
        )
        pkg = PromptBuilder().build(ctx)
        if not pkg.system_prompt or not pkg.user_input:
            return "FAIL: empty prompt fields", {}
        return "OK", {"sections": len(pkg.sections)}
    except Exception as exc:
        return f"FAIL: {exc}", {}


def _check_engine(engine: Engine) -> tuple[str, dict[str, Any]]:
    health = engine.engine_health()
    if health == "READY":
        return "OK", {"health": health}
    return f"FAIL: {health}", {"health": health}


def _check_provider(engine: Engine) -> tuple[str, dict[str, Any]]:
    provider = engine.provider_registry.default_provider()
    h = provider.health()
    if h.get("healthy", False):
        return "OK", {"provider": provider.name, "version": provider.provider_version()}
    return f"FAIL: provider {provider.name} unhealthy", {"health": h}


def _check_configuration() -> tuple[str, dict[str, Any]]:
    try:
        from backend.ai.config import ConfigManager

        mgr = ConfigManager()
        errors = mgr.validate()
        if errors:
            return f"FAIL: {errors}", {}
        snap = mgr.snapshot()
        if not snap.provider:
            return "FAIL: no provider set", {}
        return "OK", {"provider": snap.provider, "model": snap.model}
    except Exception as exc:
        return f"FAIL: {exc}", {}


def _check_metrics(engine: Engine) -> tuple[str, dict[str, Any]]:
    snap = engine.metrics_snapshot()
    if snap.get("total_executions", 0) < 0:
        return "FAIL: negative execution count", snap
    return "OK", snap


def build_report(engine: Engine) -> RuntimeReport:
    """Build a complete RuntimeReport from an Engine instance."""
    conv_status, conv_details = _check_conversation(engine)
    prompt_status, prompt_details = _check_prompt(engine)
    engine_status, engine_details = _check_engine(engine)
    provider_status, provider_details = _check_provider(engine)
    config_status, config_details = _check_configuration()
    metrics_status, metrics_details = _check_metrics(engine)

    statuses = [
        conv_status, prompt_status, engine_status,
        provider_status, config_status, metrics_status,
    ]
    all_ok = all(s == "OK" for s in statuses)
    overall = "OK" if all_ok else "FAIL"

    return RuntimeReport(
        conversation_status=conv_status,
        prompt_status=prompt_status,
        engine_status=engine_status,
        provider_status=provider_status,
        configuration_status=config_status,
        metrics_status=metrics_status,
        overall_status=overall,
        details={
            "conversation": conv_details,
            "prompt": prompt_details,
            "engine": engine_details,
            "provider": provider_details,
            "configuration": config_details,
            "metrics": metrics_details,
        },
    )
