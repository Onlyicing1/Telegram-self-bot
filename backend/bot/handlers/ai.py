"""
AI Menu — the Telegram-facing AI control panel.

The main panel shows the current provider, model, status, and last
initialization result. Settings is the single control center for all
configuration (provider, model, temperature, max tokens, memory,
conversation, context budget). Diagnostics remains available for
owner/developer usage.

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


def _get_provider_config_manager():
    from backend.ai.providers.manager.config_manager import get_provider_config_manager
    return get_provider_config_manager()


def _status_badge(ok: bool) -> str:
    return "READY" if ok else "FAIL"


def _warning_block(text: str) -> str:
    if "FAIL" not in text:
        return ""
    failures = [ln for ln in text.split("\n") if "FAIL" in ln]
    if not failures:
        return ""
    return "\n\n⚠ **Warning:**\n" + "\n".join(failures)


def _get_provider_info() -> dict:
    """Return current provider name, model, status, and init error."""
    info: dict = {
        "provider": "Unknown",
        "model": "—",
        "status": "UNKNOWN",
        "init_error": "",
        "is_dummy": True,
        "is_fallback": False,
    }
    engine = _get_engine()
    if engine is None:
        info["status"] = "FAIL: no engine"
        return info
    try:
        mgr = engine.provider_manager
        active = mgr.get_active()
        info["provider"] = active.name
        info["is_dummy"] = active.name == "dummy"
        info["is_fallback"] = active.name == mgr.registry.fallback_name and not info["is_dummy"]
        health = active.health()
        healthy = health.get("healthy", False)
        info["status"] = _status_badge(healthy)
        if not healthy:
            reason = health.get("reason", "")
            if reason:
                info["init_error"] = str(reason)
        try:
            pconfig = mgr.get_provider_config(active.name)
            info["model"] = pconfig.default_model or "—"
        except Exception:
            pass
    except Exception as exc:
        info["status"] = f"FAIL: {exc}"
        info["init_error"] = str(exc)
    return info


# ── Panel: AI Control Panel (main) ──


def _build_ai_main_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_buttons(
        ("⚙️ Settings", "panel:ai_settings"),
        ("🔧 Diagnostics", "panel:ai_diagnostics"),
    )
    return builder.build()


def _build_ai_main_body() -> str:
    info = _get_provider_info()
    lines = ["**🧠 AI Control Panel**\n"]
    lines.append(f"**Provider:** {info['provider']}")
    lines.append(f"**Model:** {info['model']}")
    lines.append(f"**Status:** {info['status']}")
    if info["init_error"]:
        lines.append(f"**Last Error:** {info['init_error']}")
    if info["is_dummy"]:
        lines.append("")
        lines.append("_⚠ Dummy provider active — no real provider configured._")
    lines.append("")
    lines.append("_Tap Settings to configure provider, model, and options._")
    return "\n".join(lines)


async def _ai_main_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    return "🧠 AI Control Panel", _build_ai_main_body(), _build_ai_main_buttons()


async def _ai_main_inline_builder(event, extra: str) -> list:
    return [render("🧠 AI Control Panel", _build_ai_main_body(), _build_ai_main_buttons())]


# ── Panel: Settings (unified control center) ──

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

_PROVIDER_TEXT_FIELDS = [
    "api_key",
    "base_url",
    "default_model",
]

_PROVIDER_TEXT_LABELS = {
    "api_key": "🔑 API Key",
    "base_url": "🌐 Base URL",
    "default_model": "🤖 Default Model",
}

_PROVIDER_TEXT_PROMPTS = {
    "api_key": "**API Key**\n\nEnter the API key for this provider:\n\n_Reply below._",
    "base_url": "**Base URL**\n\nEnter the base URL for this provider:\n\n_Reply below._",
    "default_model": "**Default Model**\n\nEnter the default model name:\n\n_Reply below._",
}


def _build_settings_buttons() -> list:
    mgr = _get_config_manager()
    snap = mgr.snapshot()
    pcm = _get_provider_config_manager()
    pconfig = pcm.get_active_config()
    builder = InlinePanelBuilder()

    builder.add_buttons(
        ("⬅ Back", "panel:ai"),
    )

    builder.add_row(
        f"{_TOGGLE_LABELS['enabled']}: {'ON' if snap.enabled else 'OFF'}",
        "action:ai_toggle_enabled",
    )

    builder.add_buttons(
        (f"🧠 Provider: {snap.provider}", "action:ai_provider_cycle"),
        (f"🤖 Model: {pconfig.default_model or '—'}", "input:ai_provider_config:default_model"),
    )

    builder.add_buttons(
        (f"{_PROVIDER_TEXT_LABELS['api_key']}: {'✅' if pconfig.api_key else '❌'}", "input:ai_provider_config:api_key"),
        (f"{_PROVIDER_TEXT_LABELS['base_url']}: {pconfig.base_url[:20] + '…' if len(pconfig.base_url) > 20 else pconfig.base_url or '—'}", "input:ai_provider_config:base_url"),
    )

    builder.add_buttons(
        (f"{_NUMERIC_LABELS['temperature']}: {pconfig.temperature}", "input:ai_provider_config:temperature"),
        (f"{_NUMERIC_LABELS['top_p']}: {pconfig.top_p}", "input:ai_provider_config:top_p"),
    )

    builder.add_buttons(
        (f"{_NUMERIC_LABELS['max_tokens']}: {pconfig.max_tokens}", "input:ai_provider_config:max_tokens"),
        (f"{_NUMERIC_LABELS['timeout']}: {pconfig.timeout}s", "input:ai_provider_config:timeout"),
    )

    builder.add_row(
        f"{_NUMERIC_LABELS['retry_count']}: {pconfig.retry_count}",
        "input:ai_provider_config:retry_count",
    )

    builder.add_buttons(
        (f"{_NUMERIC_LABELS['history_budget']}: {snap.history_budget}", "input:ai_settings:history_budget"),
        (f"{_NUMERIC_LABELS['tool_budget']}: {snap.tool_budget}", "input:ai_settings:tool_budget"),
    )

    for field in ("streaming_enabled", "vision_enabled", "reasoning_enabled", "developer_mode"):
        val = getattr(snap, field)
        builder.add_row(
            f"{_TOGGLE_LABELS[field]}: {'ON' if val else 'OFF'}",
            f"action:ai_toggle_{field}",
        )

    builder.add_row("Reset Provider Config", "action:ai_reset_provider_config")

    return builder.build()


def _build_settings_body() -> str:
    mgr = _get_config_manager()
    snap = mgr.snapshot()
    pcm = _get_provider_config_manager()
    active_name = pcm.active_name
    pconfig = pcm.get_active_config()
    info = _get_provider_info()

    lines = ["**⚙️ AI Settings**\n"]

    lines.append("**Provider Status**")
    lines.append(f"  • Provider: {info['provider']}")
    lines.append(f"  • Model: {info['model']}")
    lines.append(f"  • Status: {info['status']}")
    if info["init_error"]:
        lines.append(f"  • Error: {info['init_error']}")
    lines.append("")

    lines.append(f"**Active Config ({active_name})**")
    lines.append(f"  • {_PROVIDER_TEXT_LABELS['api_key']}: {'✅ set' if pconfig.api_key else '❌ missing'}")
    lines.append(f"  • {_PROVIDER_TEXT_LABELS['base_url']}: {pconfig.base_url or '—'}")
    lines.append(f"  • {_PROVIDER_TEXT_LABELS['default_model']}: {pconfig.default_model or '—'}")
    lines.append(f"  • {_NUMERIC_LABELS['temperature']}: {pconfig.temperature}")
    lines.append(f"  • {_NUMERIC_LABELS['top_p']}: {pconfig.top_p}")
    lines.append(f"  • {_NUMERIC_LABELS['max_tokens']}: {pconfig.max_tokens}")
    lines.append(f"  • {_NUMERIC_LABELS['timeout']}: {pconfig.timeout}s")
    lines.append(f"  • {_NUMERIC_LABELS['retry_count']}: {pconfig.retry_count}")
    lines.append("")

    lines.append("**Conversation & Memory**")
    lines.append(f"  • {_NUMERIC_LABELS['history_budget']}: {snap.history_budget} tokens")
    lines.append(f"  • {_NUMERIC_LABELS['tool_budget']}: {snap.tool_budget} tokens")
    lines.append(f"  • {_TOGGLE_LABELS['streaming_enabled']}: {'ON' if snap.streaming_enabled else 'OFF'}")
    lines.append(f"  • {_TOGGLE_LABELS['vision_enabled']}: {'ON' if snap.vision_enabled else 'OFF'}")
    lines.append(f"  • {_TOGGLE_LABELS['reasoning_enabled']}: {'ON' if snap.reasoning_enabled else 'OFF'}")
    lines.append(f"  • {_TOGGLE_LABELS['developer_mode']}: {'ON' if snap.developer_mode else 'OFF'}")
    lines.append("")

    lines.append("_Tap toggles to flip • Tap values to edit • Tap Provider to cycle._")
    return "\n".join(lines)


async def _ai_settings_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    return "AI · Settings", _build_settings_body(), _build_settings_buttons()


async def _ai_settings_inline_builder(event, extra: str) -> list:
    return [render("AI · Settings", _build_settings_body(), _build_settings_buttons())]


# ── Provider cycle action ──


async def _ai_provider_cycle_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Cycle to the next registered provider."""
    engine = _get_engine()
    if engine is None:
        return "AI · Settings", "**Error:** engine not available", _build_settings_buttons()
    mgr = engine.provider_manager
    providers = mgr.list_providers()
    if len(providers) <= 1:
        return "AI · Settings", _build_settings_body(), _build_settings_buttons()
    current = mgr.get_active_name()
    try:
        idx = providers.index(current)
    except ValueError:
        idx = -1
    next_idx = (idx + 1) % len(providers)
    next_name = providers[next_idx]
    ok = mgr.switch_provider(next_name)
    if ok:
        logger.info("AI provider cycled to '%s'", next_name)
    else:
        logger.warning("AI provider cycle to '%s' failed", next_name)
    return "AI · Settings", _build_settings_body(), _build_settings_buttons()


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


# ── Provider config input handlers (text fields via ProviderConfigManager) ──


def _make_provider_config_input_handler(field: str):
    async def _handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
        from backend.helper.inline_engine import _self_client

        text = text.strip()
        pcm = _get_provider_config_manager()
        active_name = pcm.active_name

        result = pcm.update(active_name, field, text)
        if result.valid:
            msg = f"✅ {_PROVIDER_TEXT_LABELS[field]} set"
            logger.info("AI provider config: set %s.%s", active_name, field)
        else:
            errors = "; ".join(e.message for e in result.errors)
            msg = f"❌ {errors}"

        helper = _self_client
        if helper and inline_chat_id and inline_msg_id:
            from backend.helper import render_edit, to_edit_buttons
            body = _build_settings_body()
            buttons = _build_settings_buttons()
            edit_text, edit_buttons = render_edit("AI · Settings", f"{msg}\n\n{body}", buttons)
            try:
                await helper.edit_message(inline_chat_id, inline_msg_id, edit_text)
            except Exception as exc:
                logger.warning("AI provider config input edit failed: %s", exc)

        if _self_client:
            try:
                await _self_client.delete_messages(chat_id, [msg_id])
            except Exception:
                pass

    return _handler


for _field in _PROVIDER_TEXT_FIELDS:
    globals()[f"_ai_provider_config_input_{_field}"] = _make_provider_config_input_handler(_field)


# ── Provider config numeric input handlers ──


def _make_provider_config_numeric_handler(field: str):
    async def _handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
        from backend.helper.inline_engine import _self_client

        text = text.strip()
        pcm = _get_provider_config_manager()
        active_name = pcm.active_name

        parser = _NUMERIC_PARSERS.get(field, str)
        try:
            value = parser(text)
        except (ValueError, TypeError):
            msg = f"❌ Invalid number: `{text}`"
        else:
            result = pcm.update(active_name, field, value)
            if result.valid:
                msg = f"✅ {_NUMERIC_LABELS[field]} set to {value}"
                logger.info("AI provider config: set %s.%s → %s", active_name, field, value)
            else:
                errors = "; ".join(e.message for e in result.errors)
                msg = f"❌ {errors}"

        helper = _self_client
        if helper and inline_chat_id and inline_msg_id:
            from backend.helper import render_edit, to_edit_buttons
            body = _build_settings_body()
            buttons = _build_settings_buttons()
            edit_text, edit_buttons = render_edit("AI · Settings", f"{msg}\n\n{body}", buttons)
            try:
                await helper.edit_message(inline_chat_id, inline_msg_id, edit_text)
            except Exception as exc:
                logger.warning("AI provider config numeric edit failed: %s", exc)

        if _self_client:
            try:
                await _self_client.delete_messages(chat_id, [msg_id])
            except Exception:
                pass

    return _handler


for _field in ("temperature", "top_p", "max_tokens", "timeout", "retry_count"):
    globals()[f"_ai_provider_config_input_{_field}"] = _make_provider_config_numeric_handler(_field)


# ── Reset provider config action ──


async def _ai_reset_provider_config_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    pcm = _get_provider_config_manager()
    active_name = pcm.active_name
    pcm.reset(active_name)
    logger.info("AI settings: reset provider config for '%s'", active_name)
    return "AI · Settings", _build_settings_body(), _build_settings_buttons()


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
    builder.add_buttons(
        ("⬅ Back", "panel:ai"),
    )
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

        for field in _PROVIDER_TEXT_FIELDS:
            register_input("ai_provider_config", field, {
                "handler": globals()[f"_ai_provider_config_input_{field}"],
                "prompt": _PROVIDER_TEXT_PROMPTS[field],
            })

        for field in ("temperature", "top_p", "max_tokens", "timeout", "retry_count"):
            register_input("ai_provider_config", field, {
                "handler": globals()[f"_ai_provider_config_input_{field}"],
                "prompt": _NUMERIC_PROMPTS[field],
            })

        register_action("ai_diagnostics_refresh", _ai_diagnostics_refresh_action)
        register_action("ai_provider_cycle", _ai_provider_cycle_action)
        register_action("ai_reset_provider_config", _ai_reset_provider_config_action)

        logger.info("AI panels registered OK (unified settings + diagnostics)")
    except Exception as exc:
        logger.error("AI panel registration FAILED: %s", exc)
