"""
AI Menu — the Telegram-facing AI control panel.

Inspection-only for most pages; the Settings page is fully interactive
with toggle buttons for booleans and reply-mode input for numerics.
The Provider page now supports switching between registered providers.
All changes go through the process-wide ConfigManager singleton so
every subsequent snapshot reflects the new state immediately.

No database. No persistence. No text commands. Everything is glass
buttons that edit the existing menu message in-place.
"""
from __future__ import annotations

import logging

from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    register_action,
    register_input,
    render,
)

logger = logging.getLogger(__name__)

_FUTURE_PROVIDERS = ["Gemini", "OpenAI", "GLM", "Claude"]


# ── Engine / Config access ──


def _get_engine():
    try:
        from backend.ai.engine.engine import get_engine
        return get_engine()
    except Exception as exc:
        logger.warning("AI panel: could not get engine: %s", exc)
        return None


def _get_config_manager():
    from backend.ai.config.manager import get_config_manager
    return get_config_manager()


def _status_badge(ok: bool) -> str:
    return "READY" if ok else "FAIL"


def _warning_block(text: str) -> str:
    if "FAIL" not in text:
        return ""
    failures = [ln for ln in text.split("\n") if "FAIL" in ln]
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
        "_Settings page is now interactive._"
    )
    return "🧠 AI Control Panel", body, _build_ai_main_buttons()


async def _ai_main_inline_builder(event, extra: str) -> list:
    return [render("🧠 AI Control Panel", (
        "**Provider:** Dummy\n"
        "**Model:** dummy-1\n"
        "**Status:** READY\n"
        "\n"
        "_Settings page is now interactive._"
    ), _build_ai_main_buttons())]


# ── Panel: Provider ──


async def _ai_provider_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    engine = _get_engine()
    provider_name = "Unknown"
    provider_version = "..."
    provider_status = "UNKNOWN"
    provider_count = 0
    provider_enabled = False

    if engine is not None:
        try:
            mgr = engine.provider_manager
            active = mgr.get_active()
            provider_name = active.name
            provider_version = active.provider_version()
            health = active.health()
            provider_status = _status_badge(health.get("healthy", False))
            provider_enabled = active.is_enabled
            provider_count = len(mgr.list_providers())
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
        "_Tap a provider below to switch._",
    ]

    warning = _warning_block(provider_status)
    if warning:
        lines.append(warning)

    return "AI · Provider", "\n".join(lines), _build_provider_buttons()


async def _ai_provider_inline_builder(event, extra: str) -> list:
    title, body, buttons = await _ai_provider_panel_handler(event, extra)
    return [render(title, body, buttons)]


def _build_provider_buttons() -> list:
    """Build provider switch buttons — one row per registered provider."""
    engine = _get_engine()
    builder = InlinePanelBuilder()
    if engine is not None:
        try:
            mgr = engine.provider_manager
            active_name = mgr.get_active_name()
            for name in mgr.list_providers():
                label = f"{'→ ' if name == active_name else ''}{name}"
                builder.add_row(label, f"action:ai_switch_provider:{name}")
        except Exception:
            pass
    return builder.build()


async def _ai_switch_provider_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Switch the active provider. ``extra`` is the provider name."""
    engine = _get_engine()
    if engine is None:
        return "AI · Provider", "**Error:** engine not available", []
    target = extra.strip()
    if not target:
        return "AI · Provider", "**Error:** no provider specified", _build_provider_buttons()
    ok = engine.provider_manager.switch_provider(target)
    if ok:
        logger.info("AI provider switched to '%s'", target)
    else:
        logger.warning("AI provider switch to '%s' failed", target)
    return await _ai_provider_panel_handler(event, extra)


# ── Panel: Model ──


async def _ai_model_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    mgr = _get_config_manager()
    snap = mgr.snapshot()
    lines = [
        "**Model Page**\n",
        f"**Current Model:** {snap.model}",
        f"**Max Tokens:** {snap.max_tokens}",
        f"**Temperature:** {snap.temperature}",
        f"**Top P:** {snap.top_p}",
        "",
        "_Read-only — Dummy model until real providers exist._",
    ]
    return "AI · Model", "\n".join(lines), []


async def _ai_model_inline_builder(event, extra: str) -> list:
    title, body, buttons = await _ai_model_panel_handler(event, extra)
    return [render(title, body, buttons)]


# ── Panel: Conversation ──


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

    return "AI · Conversation", "\n".join(lines), []


async def _ai_conversation_inline_builder(event, extra: str) -> list:
    title, body, buttons = await _ai_conversation_panel_handler(event, extra)
    return [render(title, body, buttons)]


# ── Panel: Memory ──


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
    return "AI · Memory", "\n".join(lines), []


async def _ai_memory_inline_builder(event, extra: str) -> list:
    title, body, buttons = await _ai_memory_panel_handler(event, extra)
    return [render(title, body, buttons)]


# ── Panel: Settings (interactive) ──

_TOGGLE_FIELDS = [
    "enabled",
    "streaming_enabled",
    "vision_enabled",
    "reasoning_enabled",
    "developer_mode",
]

_TOGGLE_LABELS = {
    "enabled": "⚡ Enabled",
    "streaming_enabled": "📡 Streaming",
    "vision_enabled": "👁 Vision",
    "reasoning_enabled": "🧩 Reasoning",
    "developer_mode": "👨‍💻 Developer Mode",
}

_NUMERIC_FIELDS = [
    "temperature",
    "top_p",
    "max_tokens",
    "timeout",
    "retry_count",
    "history_budget",
    "tool_budget",
]

_NUMERIC_LABELS = {
    "temperature": "🌡 Temperature",
    "top_p": "🎯 Top P",
    "max_tokens": "📦 Max Tokens",
    "timeout": "⏱ Timeout",
    "retry_count": "🔁 Retry",
    "history_budget": "📝 History Budget",
    "tool_budget": "🛠 Tool Budget",
}

_NUMERIC_PROMPTS = {
    "temperature": "**Temperature**\n\nEnter value (0.0 – 2.0):\n\n_Reply below._",
    "top_p": "**Top P**\n\nEnter value (0.0 – 1.0):\n\n_Reply below._",
    "max_tokens": "**Max Tokens**\n\nEnter a positive integer:\n\n_Reply below._",
    "timeout": "**Timeout**\n\nEnter timeout in seconds (positive integer):\n\n_Reply below._",
    "retry_count": "**Retry Count**\n\nEnter retry count (0+):\n\n_Reply below._",
    "history_budget": "**History Budget**\n\nEnter budget in tokens (positive integer):\n\n_Reply below._",
    "tool_budget": "**Tool Budget**\n\nEnter budget in tokens (positive integer):\n\n_Reply below._",
}

_NUMERIC_PARSERS = {
    "temperature": float,
    "top_p": float,
    "max_tokens": int,
    "timeout": int,
    "retry_count": int,
    "history_budget": int,
    "tool_budget": int,
}


def _build_settings_buttons() -> list:
    mgr = _get_config_manager()
    snap = mgr.snapshot()
    builder = InlinePanelBuilder()

    builder.add_row(
        f"{_TOGGLE_LABELS['enabled']}: {'ON' if snap.enabled else 'OFF'}",
        "action:ai_toggle_enabled",
    )

    builder.add_buttons(
        ("🧠 Provider", "panel:ai_provider"),
        (f"🤖 Model: {snap.model}", "panel:ai_model"),
    )

    builder.add_buttons(
        (f"{_NUMERIC_LABELS['temperature']}: {snap.temperature}", "input:ai_settings:temperature"),
        (f"{_NUMERIC_LABELS['top_p']}: {snap.top_p}", "input:ai_settings:top_p"),
    )

    builder.add_buttons(
        (f"{_NUMERIC_LABELS['max_tokens']}: {snap.max_tokens}", "input:ai_settings:max_tokens"),
        (f"{_NUMERIC_LABELS['timeout']}: {snap.timeout}s", "input:ai_settings:timeout"),
    )

    builder.add_buttons(
        (f"{_NUMERIC_LABELS['retry_count']}: {snap.retry_count}", "input:ai_settings:retry_count"),
        (f"{_NUMERIC_LABELS['history_budget']}: {snap.history_budget}", "input:ai_settings:history_budget"),
    )

    builder.add_row(
        f"{_NUMERIC_LABELS['tool_budget']}: {snap.tool_budget}",
        "input:ai_settings:tool_budget",
    )

    for field in ("streaming_enabled", "vision_enabled", "reasoning_enabled", "developer_mode"):
        val = getattr(snap, field)
        builder.add_row(
            f"{_TOGGLE_LABELS[field]}: {'ON' if val else 'OFF'}",
            f"action:ai_toggle_{field}",
        )

    return builder.build()


def _build_settings_body() -> str:
    mgr = _get_config_manager()
    snap = mgr.snapshot()
    lines = ["**AI Settings**\n"]
    lines.append(f"**Provider:** {snap.provider}")
    lines.append(f"**Model:** {snap.model}")
    lines.append(f"**Enabled:** {snap.enabled}")
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
    lines.append("")
    lines.append("_Tap toggles to flip • Tap values to edit._")
    return "\n".join(lines)


async def _ai_settings_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    return "AI · Settings", _build_settings_body(), _build_settings_buttons()


async def _ai_settings_inline_builder(event, extra: str) -> list:
    return [render("AI · Settings", _build_settings_body(), _build_settings_buttons())]


# ── Toggle actions ──


def _make_toggle_action(field: str):
    async def _toggle(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
        mgr = _get_config_manager()
        current = mgr.get(field)
        try:
            mgr.set(field, not current)
            logger.info("AI settings: toggled %s → %s", field, not current)
        except Exception as exc:
            logger.warning("AI settings: toggle %s failed: %s", field, exc)
        return "AI · Settings", _build_settings_body(), _build_settings_buttons()
    return _toggle


for _field in _TOGGLE_FIELDS:
    globals()[f"_ai_toggle_{_field}_action"] = _make_toggle_action(_field)


# ── Numeric input handlers (reply-mode) ──


def _make_input_handler(field: str):
    async def _handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
        from backend.helper.inline_engine import _self_client
        from backend.ai.config.validation import ConfigValidationError

        text = text.strip()
        parser = _NUMERIC_PARSERS[field]
        mgr = _get_config_manager()

        try:
            value = parser(text)
        except (ValueError, TypeError):
            result = f"❌ Invalid number: `{text}`"
        else:
            try:
                mgr.set(field, value)
                result = f"✅ {_NUMERIC_LABELS[field]} set to {value}"
                logger.info("AI settings: set %s → %s", field, value)
            except ConfigValidationError as exc:
                result = f"❌ {exc}"

        helper = _self_client
        if helper and inline_chat_id and inline_msg_id:
            from backend.helper import render_edit, to_edit_buttons
            body = _build_settings_body()
            buttons = _build_settings_buttons()
            edit_text, edit_buttons = render_edit("AI · Settings", f"{result}\n\n{body}", buttons)
            try:
                await helper.edit_message(inline_chat_id, inline_msg_id, edit_text)
            except Exception as exc:
                logger.warning("AI settings input edit failed: %s", exc)

        if _self_client:
            try:
                await _self_client.delete_messages(chat_id, [msg_id])
            except Exception:
                pass

    return _handler


for _field in _NUMERIC_FIELDS:
    globals()[f"_ai_input_{_field}"] = _make_input_handler(_field)


# ── Panel: Diagnostics ──


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


def _build_ai_diagnostics_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("Refresh", "action:ai_diagnostics_refresh")
    return builder.build()


async def _ai_diagnostics_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    return "AI · Diagnostics", _build_diagnostics_body(), _build_ai_diagnostics_buttons()


async def _ai_diagnostics_inline_builder(event, extra: str) -> list:
    return [render("AI · Diagnostics", _build_diagnostics_body(), _build_ai_diagnostics_buttons())]


async def _ai_diagnostics_refresh_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    return "AI · Diagnostics", _build_diagnostics_body(), _build_ai_diagnostics_buttons()


# ── Registration ──


def register(client, owner_id: int) -> None:
    try:
        register_panel("ai", _ai_main_panel_handler, parent="menu", title="🧠 AI")
        register_inline_builder("ai", _ai_main_inline_builder)

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

        for field in _TOGGLE_FIELDS:
            register_action(f"ai_toggle_{field}", globals()[f"_ai_toggle_{field}_action"])

        for field in _NUMERIC_FIELDS:
            register_input("ai_settings", field, {
                "handler": globals()[f"_ai_input_{field}"],
                "prompt": _NUMERIC_PROMPTS[field],
            })

        register_action("ai_diagnostics_refresh", _ai_diagnostics_refresh_action)
        register_action("ai_switch_provider", _ai_switch_provider_action)

        logger.info("AI panels registered OK (interactive settings + provider switching)")
    except Exception as exc:
        logger.error("AI panel registration FAILED: %s", exc)
