"""
AI Menu — the Telegram-facing AI control panel.

This is the FIRST Telegram-facing AI feature. It is inspection-only:
every page is read-only, every button performs navigation only, and
no external AI provider is ever contacted. The panels read live data
from the already-built backend architecture (Engine, ConfigManager,
ConversationManager, ProviderRegistry, AIInspector, RuntimeReport).

No database. No persistence. No text commands. Everything is glass
buttons that edit the existing menu message in-place.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
)

logger = logging.getLogger(__name__)

# ── Future providers (shown disabled, not selectable) ──

_FUTURE_PROVIDERS = [
    "Gemini",
    "OpenAI",
    "GLM",
    "Claude",
]

# ── Engine access ──


def _get_engine():
    """Return the process-wide default Engine instance, or None."""
    try:
        from backend.ai.engine.engine import get_engine
        return get_engine()
    except Exception as exc:
        logger.warning("AI panel: could not get engine: %s", exc)
        return None


def _get_config_manager():
    """Return a fresh ConfigManager reflecting current defaults, or None."""
    try:
        from backend.ai.config.manager import ConfigManager
        return ConfigManager()
    except Exception as exc:
        logger.warning("AI panel: could not get config manager: %s", exc)
        return None


def _get_inspector():
    """Return an AIInspector for the live AISession, or None."""
    try:
        from backend.ai.session.session import AISession
        from backend.ai.inspector.inspector import AIInspector
        session = AISession()
        return AIInspector(session)
    except Exception as exc:
        logger.warning("AI panel: could not get inspector: %s", exc)
        return None


# ── Helpers ──


def _status_badge(ok: bool) -> str:
    return "READY" if ok else "FAIL"


def _warning_block(report_text: str) -> str:
    """Return a readable warning line if any FAIL is detected."""
    if "FAIL" not in report_text:
        return ""
    lines = report_text.split("\n")
    failures = [ln for ln in lines if "FAIL" in ln]
    if not failures:
        return ""
    return "\n\n⚠ **Warning:**\n" + "\n".join(failures)


# ── Panel: AI Control Panel (main) ──


def _build_ai_main_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_buttons(
        ("Provider", "panel:ai_provider"),
        ("Model", "panel:ai_model"),
    )
    builder.add_buttons(
        ("Conversation", "panel:ai_conversation"),
        ("Memory", "panel:ai_memory"),
    )
    builder.add_buttons(
        ("Settings", "panel:ai_settings"),
        ("Diagnostics", "panel:ai_diagnostics"),
    )
    return builder.build()


async def _ai_main_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    body = (
        "**Provider:** Dummy\n"
        "**Model:** dummy-1\n"
        "**Status:** READY\n"
        "\n"
        "_Inspection only — no provider calls._"
    )
    return "🧠 AI Control Panel", body, _build_ai_main_buttons()


async def _ai_main_inline_builder(event, extra: str) -> list:
    from backend.helper import render
    title, body, buttons = await _ai_main_panel_handler(event, extra)
    return [render(title, body, buttons)]


# ── Panel: Provider ──


def _build_ai_provider_buttons() -> list:
    builder = InlinePanelBuilder()
    return builder.build()


async def _ai_provider_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    engine = _get_engine()
    provider_name = "Unknown"
    provider_version = "..."
    provider_status = "UNKNOWN"
    provider_count = 0
    provider_enabled = False

    if engine is not None:
        try:
            registry = engine.provider_registry
            provider = registry.default_provider()
            provider_name = provider.name
            provider_version = provider.provider_version()
            health = provider.health()
            provider_status = _status_badge(health.get("healthy", False))
            provider_enabled = provider.is_enabled
            provider_count = len(registry.list())
        except Exception as exc:
            logger.warning("AI provider panel: %s", exc)
            provider_status = f"FAIL: {exc}"

    lines = [
        "**Provider Page**\n",
        f"**Current Provider:** {provider_name}",
        f"**Version:** {provider_version}",
        f"**Status:** {provider_status}",
        f"**Enabled:** {'YES' if provider_enabled else 'NO'}",
        f"**Provider Count:** {provider_count}",
        "",
        "**Future Providers:**",
    ]
    for fp in _FUTURE_PROVIDERS:
        lines.append(f"  • {fp} — _disabled_")

    warning = _warning_block(provider_status)
    if warning:
        lines.append(warning)

    return "AI · Provider", "\n".join(lines), _build_ai_provider_buttons()


async def _ai_provider_inline_builder(event, extra: str) -> list:
    from backend.helper import render
    title, body, buttons = await _ai_provider_panel_handler(event, extra)
    return [render(title, body, buttons)]


# ── Panel: Model ──


def _build_ai_model_buttons() -> list:
    builder = InlinePanelBuilder()
    return builder.build()


async def _ai_model_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    mgr = _get_config_manager()
    model_name = "Unknown"
    max_tokens = 0
    temperature = 0.0
    top_p = 0.0
    estimated_context = "..."

    if mgr is not None:
        try:
            snap = mgr.snapshot()
            model_name = snap.model
            max_tokens = snap.max_tokens
            temperature = snap.temperature
            top_p = snap.top_p
        except Exception as exc:
            logger.warning("AI model panel: %s", exc)

    lines = [
        "**Model Page**\n",
        f"**Current Model:** {model_name}",
        f"**Estimated Context:** {estimated_context}",
        f"**Max Tokens:** {max_tokens}",
        f"**Temperature:** {temperature}",
        f"**Top P:** {top_p}",
        "",
        "_Read-only._",
    ]

    return "AI · Model", "\n".join(lines), _build_ai_model_buttons()


async def _ai_model_inline_builder(event, extra: str) -> list:
    from backend.helper import render
    title, body, buttons = await _ai_model_panel_handler(event, extra)
    return [render(title, body, buttons)]


# ── Panel: Conversation ──


def _build_ai_conversation_buttons() -> list:
    builder = InlinePanelBuilder()
    return builder.build()


async def _ai_conversation_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    engine = _get_engine()
    session_id = "—"
    conv_state = "Idle"
    message_count = 0
    estimated_tokens = 0
    last_activity = "—"
    history_size = 0

    if engine is not None:
        try:
            conv_mgr = engine.conversation_manager
            sessions = conv_mgr.list_sessions()
            if sessions:
                session = sessions[0]
                session_id = session.session_id
                conv_state = "Active"
                history = session.conversation_history.all_items()
                message_count = len(history)
                history_size = session.conversation_history.size()
                estimated_tokens = session.token_estimate
                last_activity = session.last_activity.strftime("%Y-%m-%d %H:%M UTC")
            else:
                conv_state = "Idle (no active session)"
        except Exception as exc:
            logger.warning("AI conversation panel: %s", exc)
            conv_state = f"FAIL: {exc}"

    lines = [
        "**Conversation Page**\n",
        f"**Conversation State:** {conv_state}",
        f"**Session ID:** `{session_id}`",
        f"**Messages:** {message_count}",
        f"**Estimated Tokens:** {estimated_tokens}",
        f"**Last Activity:** {last_activity}",
        f"**History Size:** {history_size}",
        "",
        "_Read-only._",
    ]

    warning = _warning_block(conv_state)
    if warning:
        lines.append(warning)

    return "AI · Conversation", "\n".join(lines), _build_ai_conversation_buttons()


async def _ai_conversation_inline_builder(event, extra: str) -> list:
    from backend.helper import render
    title, body, buttons = await _ai_conversation_panel_handler(event, extra)
    return [render(title, body, buttons)]


# ── Panel: Memory ──


def _build_ai_memory_buttons() -> list:
    builder = InlinePanelBuilder()
    return builder.build()


async def _ai_memory_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    lines = [
        "**Memory Page**\n",
        "**Conversation Memory:** Active (RAM-only)",
        "**Session Memory:** Active (RAM-only)",
        "**Long Memory:** Not configured",
        "**Persistent Memory:** Disabled",
        "",
        "**Current Status:**",
        "  • Conversation Memory — _bounded, in-memory_",
        "  • Session Memory — _runtime state, in-memory_",
        "  • Long Memory — _no vector store_",
        "  • Persistent Memory — _no database_",
        "",
        "_Read-only._",
    ]

    return "AI · Memory", "\n".join(lines), _build_ai_memory_buttons()


async def _ai_memory_inline_builder(event, extra: str) -> list:
    from backend.helper import render
    title, body, buttons = await _ai_memory_panel_handler(event, extra)
    return [render(title, body, buttons)]


# ── Panel: Settings ──


def _build_ai_settings_buttons() -> list:
    builder = InlinePanelBuilder()
    return builder.build()


async def _ai_settings_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    mgr = _get_config_manager()
    lines = ["**AI Settings**\n"]

    if mgr is not None:
        try:
            snap = mgr.snapshot()
            lines.append(f"**Enabled:** {snap.enabled}")
            lines.append(f"**Provider:** {snap.provider}")
            lines.append(f"**Model:** {snap.model}")
            lines.append(f"**Temperature:** {snap.temperature}")
            lines.append(f"**Top P:** {snap.top_p}")
            lines.append(f"**Max Tokens:** {snap.max_tokens}")
            lines.append(f"**Timeout:** {snap.timeout}s")
            lines.append(f"**Retry Count:** {snap.retry_count}")
            lines.append(f"**History Budget:** {snap.history_budget} tokens")
            lines.append(f"**Tool Budget:** {snap.tool_budget} tokens")
            lines.append(f"**Streaming:** {snap.streaming_enabled}")
            lines.append(f"**Vision:** {snap.vision_enabled}")
            lines.append(f"**Reasoning:** {snap.reasoning_enabled}")
            lines.append(f"**Developer Mode:** {snap.developer_mode}")
            lines.append(f"**System Prompt:** _{len(snap.system_prompt)} chars_")
        except Exception as exc:
            logger.warning("AI settings panel: %s", exc)
            lines.append(f"**Error:** {exc}")
    else:
        lines.append("**Error:** Could not load configuration.")

    lines.append("")
    lines.append("_Read-only._")

    return "AI · Settings", "\n".join(lines), _build_ai_settings_buttons()


async def _ai_settings_inline_builder(event, extra: str) -> list:
    from backend.helper import render
    title, body, buttons = await _ai_settings_panel_handler(event, extra)
    return [render(title, body, buttons)]


# ── Panel: Diagnostics ──


def _build_ai_diagnostics_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("Refresh", "action:ai_diagnostics_refresh")
    return builder.build()


def _build_diagnostics_body() -> str:
    engine = _get_engine()

    engine_status = "FAIL: no engine"
    conv_status = "FAIL: no engine"
    prompt_status = "FAIL: no engine"
    provider_status = "FAIL: no engine"
    config_status = "FAIL: no engine"
    metrics_status = "FAIL: no engine"
    overall_status = "FAIL"

    if engine is not None:
        try:
            from backend.ai.runtime.report import build_report
            report = build_report(engine)
            engine_status = report.engine_status
            conv_status = report.conversation_status
            prompt_status = report.prompt_status
            provider_status = report.provider_status
            config_status = report.configuration_status
            metrics_status = report.metrics_status
            overall_status = report.overall_status
        except Exception as exc:
            logger.warning("AI diagnostics panel: %s", exc)
            overall_status = f"FAIL: {exc}"

    lines = [
        "**AI Diagnostics**\n",
        f"**Engine:** {engine_status}",
        f"**Conversation:** {conv_status}",
        f"**Prompt Builder:** {prompt_status}",
        f"**Context Builder:** {conv_status}",
        f"**Provider:** {provider_status}",
        f"**Configuration:** {config_status}",
        f"**Metrics:** {metrics_status}",
        f"**Overall:** {overall_status}",
    ]

    warning = _warning_block("\n".join([
        engine_status, conv_status, prompt_status,
        provider_status, config_status, metrics_status,
    ]))
    if warning:
        lines.append(warning)

    return "\n".join(lines)


async def _ai_diagnostics_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    return "AI · Diagnostics", _build_diagnostics_body(), _build_ai_diagnostics_buttons()


async def _ai_diagnostics_inline_builder(event, extra: str) -> list:
    from backend.helper import render
    title, body, buttons = await _ai_diagnostics_panel_handler(event, extra)
    return [render(title, body, buttons)]


async def _ai_diagnostics_refresh_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    return "AI · Diagnostics", _build_diagnostics_body(), _build_ai_diagnostics_buttons()


# ── Registration ──


def register(client, owner_id: int) -> None:
    """Register all AI panels and actions."""
    try:
        # Main AI panel — parent is "menu" so Back returns to the root menu
        register_panel("ai", _ai_main_panel_handler, parent="menu", title="🧠 AI")
        register_inline_builder("ai", _ai_main_inline_builder)

        # Sub-panels — parent is "ai" so Back returns to the AI control panel
        register_panel("ai_provider", _ai_provider_panel_handler, parent="ai", title="AI · Provider")
        register_inline_builder("ai_provider", _ai_provider_inline_builder)

        register_panel("ai_model", _ai_model_panel_handler, parent="ai", title="AI · Model")
        register_inline_builder("ai_model", _ai_model_inline_builder)

        register_panel("ai_conversation", _ai_conversation_panel_handler, parent="ai", title="AI · Conversation")
        register_inline_builder("ai_conversation", _ai_conversation_inline_builder)

        register_panel("ai_memory", _ai_memory_panel_handler, parent="ai", title="AI · Memory")
        register_inline_builder("ai_memory", _ai_memory_inline_builder)

        register_panel("ai_settings", _ai_settings_panel_handler, parent="ai", title="AI · Settings")
        register_inline_builder("ai_settings", _ai_settings_inline_builder)

        register_panel("ai_diagnostics", _ai_diagnostics_panel_handler, parent="ai", title="AI · Diagnostics")
        register_inline_builder("ai_diagnostics", _ai_diagnostics_inline_builder)

        # Actions
        from backend.helper import register_action
        register_action("ai_diagnostics_refresh", _ai_diagnostics_refresh_action)

        logger.info("AI panels registered OK")
    except Exception as exc:
        logger.error("AI panel registration FAILED: %s", exc)
