"""
AI Panel — simplified, user-friendly AI configuration.

The user never needs to know:
  - Provider names
  - ENV variable names
  - API key formats
  - Model names

Everything is discovered automatically. The panel flow is:

  AI → Provider (auto-detected) → Model (auto-listed) → Ready → Start Chat

Panels:
  ai             — Main panel (status + next action + Start Chat)
  ai_provider    — Provider selection (only available ones shown)
  ai_model       — Model selection (auto-fetched from provider API)
  ai_wizard      — Setup wizard (shown when no provider is configured)
  ai_settings    — Simple settings (temperature, max_tokens, system prompt)
  ai_status      — Status screen (provider, model, connected, latency)
  ai_diagnostics — Diagnostics (owner/developer only)
"""
from __future__ import annotations

import asyncio
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


def _get_engine():
    try:
        from backend.ai.engine.engine import get_engine
        return get_engine()
    except Exception:
        return None


async def _get_owner_id() -> int:
    from backend.helper.inline_engine import _owner_id
    return _owner_id


async def _get_saved_config(owner_id: int) -> dict:
    from backend.ai.config_store import get_config
    return await get_config(owner_id)


async def _save_config(owner_id: int, config: dict) -> bool:
    from backend.ai.config_store import save_config
    return await save_config(owner_id, config)


async def _discover() -> list:
    from backend.ai.discovery import discover_providers
    return await discover_providers()


def _get_engine_info() -> dict:
    info: dict = {"provider": "—", "model": "—", "status": "UNKNOWN", "connected": False}
    engine = _get_engine()
    if engine is None:
        info["status"] = "No engine"
        return info
    try:
        mgr = engine.provider_manager
        active = mgr.get_active()
        info["provider"] = active.name
        info["connected"] = active.health().get("healthy", False)
        info["status"] = "Connected" if info["connected"] else "Disconnected"
        try:
            pconfig = mgr.get_provider_config(active.name)
            info["model"] = pconfig.default_model or "—"
        except Exception:
            pass
    except Exception:
        pass
    return info


def _status_icon(connected: bool) -> str:
    return "🟢" if connected else "🔴"


def _nav_buttons(builder: InlinePanelBuilder) -> None:
    builder.add_buttons(
        ("⬅ Back", "panel:_nav:back"),
        ("🏠 Home", "panel:_nav:home"),
    )


async def _ai_main_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    owner_id = await _get_owner_id()
    config = await _get_saved_config(owner_id)
    engine_info = _get_engine_info()
    available = [p for p in await _discover() if p.status == "available"]
    saved_provider = config.get("provider", "") or engine_info["provider"]
    saved_model = config.get("model", "") or engine_info["model"]
    connected = engine_info["connected"]

    lines = ["**🧠 AI Assistant**\n"]

    if not saved_provider or saved_provider == "—":
        lines.append("⚠️ **No provider configured**")
        lines.append("")
        lines.append("_Tap **Provider** to select one._")
        builder = InlinePanelBuilder()
        builder.add_row("🔄 Select Provider", "panel:ai_provider")
        _nav_buttons(builder)
        return "🧠 AI", "\n".join(lines), builder.build()

    if not saved_model or saved_model == "—":
        lines.append(f"**Provider:** {saved_provider.title()}")
        lines.append("⚠️ **No model selected**")
        lines.append("")
        lines.append("_Tap **Model** to select one._")
        builder = InlinePanelBuilder()
        builder.add_row("🤖 Select Model", "panel:ai_model")
        builder.add_row("🔄 Change Provider", "panel:ai_provider")
        _nav_buttons(builder)
        return "🧠 AI", "\n".join(lines), builder.build()

    lines.append(f"**Provider:** {saved_provider.title()}")
    lines.append(f"**Model:** {saved_model}")
    lines.append(f"{_status_icon(connected)} **Status:** Ready")
    if config.get("last_request_at"):
        lines.append(f"**Last request:** {config['last_request_at'][:19]}")
    lines.append("")
    lines.append("_Tap **Start Chat** to begin._")
    builder = InlinePanelBuilder()
    builder.add_row("💬 Start Chat", "action:ai_start_chat")
    builder.add_buttons(("🔄 Provider", "panel:ai_provider"), ("🤖 Model", "panel:ai_model"))
    builder.add_buttons(("📊 Status", "panel:ai_status"), ("⚙️ Settings", "panel:ai_settings"))
    _nav_buttons(builder)
    return "🧠 AI", "\n".join(lines), builder.build()


async def _ai_main_inline_builder(event, extra: str) -> list:
    result = await _ai_main_panel_handler(event, extra)
    if result is None:
        return [render("🧠 AI", "Error loading panel.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


async def _ai_provider_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    owner_id = await _get_owner_id()
    config = await _get_saved_config(owner_id)
    results = await _discover()
    available = [p for p in results if p.status == "available"]
    invalid = [p for p in results if p.status == "invalid"]
    current = config.get("provider", "")

    lines = ["**🔄 Provider**\n"]

    if available:
        lines.append("**Available:**")
        for p in available:
            mark = "✅" if p.name == current else "  "
            lines.append(f"  {mark} {p.icon} {p.display_name}")
        lines.append("")
    else:
        lines.append("_No providers available._")
        lines.append("_Tap **Setup Guide** for instructions._")
        lines.append("")

    if invalid:
        lines.append("**Invalid Key:**")
        for p in invalid:
            lines.append(f"  ⚠️ {p.icon} {p.display_name}")
        lines.append("")

    builder = InlinePanelBuilder()
    if available:
        for p in available:
            label = f"{'✅' if p.name == current else '  '} {p.icon} {p.display_name}"
            builder.add_row(label, f"action:ai_select_provider:{p.name}")
    else:
        builder.add_row("📖 Setup Guide", "panel:ai_wizard")

    builder.add_row("🔄 Refresh", "action:ai_refresh_providers")
    _nav_buttons(builder)
    return "🔄 Provider", "\n".join(lines), builder.build()


async def _ai_provider_inline_builder(event, extra: str) -> list:
    result = await _ai_provider_panel_handler(event, extra)
    if result is None:
        return [render("Provider", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


async def _ai_model_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    owner_id = await _get_owner_id()
    config = await _get_saved_config(owner_id)
    provider_name = config.get("provider", "")
    if not provider_name:
        return "🤖 Model", "⚠️ Select a provider first.", [
            [InlinePanelBuilder().add_row("⬅ Back", "panel:ai").build()[0][0]],
        ]
    from backend.ai.model_discovery import fetch_models, get_api_key_for_provider, get_base_url_for_provider
    api_key = get_api_key_for_provider(provider_name)
    base_url = get_base_url_for_provider(provider_name)
    if not api_key:
        return "🤖 Model", "⚠️ No API key for this provider.", [
            [InlinePanelBuilder().add_row("⬅ Back", "panel:ai").build()[0][0]],
        ]
    page = 0
    if extra.startswith("page:"):
        try:
            page = int(extra[5:])
        except ValueError:
            page = 0
    models = await fetch_models(provider_name, api_key, base_url)
    if not models:
        return "🤖 Model", "⚠️ Could not fetch models.\n\nTap **Refresh** to try again.", [
            [InlinePanelBuilder().add_row("🔄 Refresh", "action:ai_refresh_models").build()[0][0]],
            [InlinePanelBuilder().add_row("⬅ Back", "panel:ai").build()[0][0]],
        ]
    per_page = 8
    total = len(models)
    total_pages = max(1, (total + per_page - 1) // per_page)
    start = page * per_page
    end = start + per_page
    page_models = models[start:end]
    current_model = config.get("model", "")
    lines = [
        f"**🤖 Model Selection**\n",
        f"_{total} models available · Page {page + 1}/{total_pages}_\n",
    ]
    builder = InlinePanelBuilder()
    for m in page_models:
        mark = "✅" if m.id == current_model else "  "
        label = f"{mark} {m.name}"
        if m.context_length > 0:
            ctx_k = m.context_length // 1000
            label += f" ({ctx_k}K)"
        builder.add_row(label[:60], f"action:ai_select_model:{m.id}")
    nav = []
    if page > 0:
        nav.append(("‹ Prev", f"panel:ai_model:page:{page - 1}"))
    nav.append((f"{page + 1}/{total_pages}", "panel:ai_model:noop"))
    if page < total_pages - 1:
        nav.append(("Next ›", f"panel:ai_model:page:{page + 1}"))
    if nav:
        builder.add_buttons(*nav)
    builder.add_row("🔄 Refresh Models", "action:ai_refresh_models")
    _nav_buttons(builder)
    return "🤖 Model", "\n".join(lines), builder.build()


async def _ai_model_inline_builder(event, extra: str) -> list:
    result = await _ai_model_panel_handler(event, extra)
    if result is None:
        return [render("Model", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


async def _ai_wizard_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.ai.discovery import get_wizard_info
    wizard_info = get_wizard_info()
    results = await _discover()
    invalid = [p for p in results if p.status == "invalid"]
    lines = [
        "**🧠 AI Setup**\n",
        "No provider detected.\n",
        "To use AI, set one API key as an environment variable:\n",
    ]
    for p in wizard_info:
        lines.append(f"  {p['icon']} **{p['display_name']}**")
        lines.append(f"     Set: `{p['env_var']}`")
        lines.append("")
    if invalid:
        lines.append("⚠️ **Invalid keys detected:**")
        for p in invalid:
            lines.append(f"  {p.icon} {p.display_name} — key may be expired or wrong")
        lines.append("")
    lines.append("_Set a key and tap **Refresh**._")
    builder = InlinePanelBuilder()
    builder.add_row("🔄 Refresh", "action:ai_refresh_providers")
    _nav_buttons(builder)
    return "🧠 AI Setup", "\n".join(lines), builder.build()


async def _ai_wizard_inline_builder(event, extra: str) -> list:
    result = await _ai_wizard_panel_handler(event, extra)
    if result is None:
        return [render("Setup", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


async def _ai_settings_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    owner_id = await _get_owner_id()
    config = await _get_saved_config(owner_id)
    lines = [
        "**⚙️ AI Settings**\n",
        f"**Temperature:** {config.get('temperature', 1.0)}",
        f"**Max Tokens:** {config.get('max_tokens', 4096)}",
        f"**Context Budget:** {config.get('history_budget', 4000)} tokens",
    ]
    prompt = config.get("system_prompt", "")
    if prompt:
        lines.append(f"**System Prompt:** Custom ✅")
    else:
        lines.append(f"**System Prompt:** Default")
    builder = InlinePanelBuilder()
    builder.add_row("🌡 Temperature", "input:ai_settings:temperature")
    builder.add_row("📦 Max Tokens", "input:ai_settings:max_tokens")
    builder.add_row("📝 Context Budget", "input:ai_settings:history_budget")
    builder.add_row("💬 System Prompt", "input:ai_settings:system_prompt")
    _nav_buttons(builder)
    return "⚙️ Settings", "\n".join(lines), builder.build()


async def _ai_settings_inline_builder(event, extra: str) -> list:
    result = await _ai_settings_panel_handler(event, extra)
    if result is None:
        return [render("Settings", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


async def _ai_status_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    owner_id = await _get_owner_id()
    config = await _get_saved_config(owner_id)
    engine_info = _get_engine_info()
    lines = [
        "**📊 AI Status**\n",
        f"**Provider:** {config.get('provider', '—').title() or '—'}",
        f"**Model:** {config.get('model', '—')}",
        f"**Connected:** {'✅ Yes' if engine_info['connected'] else '❌ No'}",
    ]
    if config.get("last_request_at"):
        lines.append(f"**Last Request:** {config['last_request_at'][:19]}")
    else:
        lines.append("**Last Request:** Never")
    latency = config.get("last_latency_ms", 0)
    if latency > 0:
        lines.append(f"**Latency:** {latency:.0f}ms")
    else:
        lines.append("**Latency:** —")
    engine = _get_engine()
    if engine:
        try:
            snap = engine.metrics_snapshot()
            lines.append(f"**Total Requests:** {snap.get('total_executions', 0)}")
            lines.append(f"**Successful:** {snap.get('successful_executions', 0)}")
        except Exception:
            pass
    builder = InlinePanelBuilder()
    builder.add_row("🔄 Refresh", "action:ai_status_refresh")
    _nav_buttons(builder)
    return "📊 AI Status", "\n".join(lines), builder.build()


async def _ai_status_inline_builder(event, extra: str) -> list:
    result = await _ai_status_panel_handler(event, extra)
    if result is None:
        return [render("Status", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


async def _ai_diagnostics_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    engine = _get_engine()
    lines = ["**🔧 AI Diagnostics**\n"]
    if engine is None:
        lines.append("❌ Engine not available")
    else:
        try:
            health = engine.engine_health()
            lines.append(f"**Engine:** {health}")
            snap = engine.metrics_snapshot()
            lines.append(f"**Executions:** {snap.get('total_executions', 0)}")
            lines.append(f"**Avg Latency:** {snap.get('average_latency', 0):.3f}s")
            lines.append(f"**Providers:** {', '.join(engine.provider_manager.list_providers())}")
        except Exception as exc:
            lines.append(f"Error: {exc}")
    builder = InlinePanelBuilder()
    builder.add_row("🔄 Refresh", "action:ai_diagnostics_refresh")
    _nav_buttons(builder)
    return "🔧 Diagnostics", "\n".join(lines), builder.build()


async def _ai_diagnostics_inline_builder(event, extra: str) -> list:
    result = await _ai_diagnostics_panel_handler(event, extra)
    if result is None:
        return [render("Diagnostics", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


async def _ai_select_provider_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    provider_name = extra.strip()
    owner_id = await _get_owner_id()
    from backend.ai.discovery import get_provider_info
    info = get_provider_info(provider_name)
    if not info:
        return "Provider", "❌ Unknown provider.", []
    config = await _get_saved_config(owner_id)
    config["provider"] = provider_name
    config["model"] = info["default_model"]
    config["is_configured"] = True
    await _save_config(owner_id, config)
    from backend.ai.model_discovery import fetch_models, get_api_key_for_provider, get_base_url_for_provider
    api_key = get_api_key_for_provider(provider_name)
    base_url = get_base_url_for_provider(provider_name)
    models = await fetch_models(provider_name, api_key, base_url)
    if models:
        first = models[0]
        config["model"] = first.id
        await _save_config(owner_id, config)
    return await _ai_model_panel_handler(event, "")


async def _ai_select_model_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    model_id = extra.strip()
    owner_id = await _get_owner_id()
    config = await _get_saved_config(owner_id)
    config["model"] = model_id
    await _save_config(owner_id, config)
    return await _ai_main_panel_handler(event, "")


async def _ai_refresh_providers_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.ai.discovery import discover_providers
    await discover_providers(force_refresh=True)
    return await _ai_provider_panel_handler(event, "")


async def _ai_refresh_models_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    owner_id = await _get_owner_id()
    config = await _get_saved_config(owner_id)
    provider_name = config.get("provider", "")
    if provider_name:
        from backend.ai.model_discovery import fetch_models, get_api_key_for_provider, get_base_url_for_provider
        api_key = get_api_key_for_provider(provider_name)
        base_url = get_base_url_for_provider(provider_name)
        await fetch_models(provider_name, api_key, base_url, force_refresh=True)
    return await _ai_model_panel_handler(event, "")


async def _ai_status_refresh_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    return await _ai_status_panel_handler(event, "")


async def _ai_diagnostics_refresh_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    return await _ai_diagnostics_panel_handler(event, "")


async def _ai_start_chat_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    owner_id = await _get_owner_id()
    config = await _get_saved_config(owner_id)
    provider = config.get("provider", "")
    model = config.get("model", "")
    if not provider:
        return "🧠 AI", "⚠️ No provider configured.\n\nTap **Provider** to select one.", [
            [InlinePanelBuilder().add_row("🔄 Select Provider", "panel:ai_provider").build()[0][0]],
            [InlinePanelBuilder().add_row("⬅ Back", "panel:ai").build()[0][0]],
        ]
    if not model:
        return "🧠 AI", "⚠️ No model selected.\n\nTap **Model** to select one.", [
            [InlinePanelBuilder().add_row("🤖 Select Model", "panel:ai_model").build()[0][0]],
            [InlinePanelBuilder().add_row("⬅ Back", "panel:ai").build()[0][0]],
        ]
    return "🧠 AI", (
        f"✅ **Ready to chat!**\n\n"
        f"**Provider:** {provider.title()}\n"
        f"**Model:** {model}\n\n"
        f"Send `.ai <message>` to start talking.\n"
        f"Example: `.ai Hello, how are you?`"
    ), []


async def _ai_temperature_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client
    owner_id = await _get_owner_id()
    try:
        val = float(text.strip())
    except ValueError:
        result = "❌ Invalid number."
    else:
        if 0.0 <= val <= 2.0:
            from backend.ai.config_store import update_setting
            await update_setting(owner_id, "temperature", val)
            result = f"✅ Temperature set to {val}"
        else:
            result = "❌ Temperature must be 0.0–2.0"
    from backend.helper.client import get_client
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception:
            pass
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _ai_max_tokens_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client
    owner_id = await _get_owner_id()
    try:
        val = int(text.strip())
    except ValueError:
        result = "❌ Invalid number."
    else:
        if val > 0:
            from backend.ai.config_store import update_setting
            await update_setting(owner_id, "max_tokens", val)
            result = f"✅ Max tokens set to {val}"
        else:
            result = "❌ Must be positive."
    from backend.helper.client import get_client
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception:
            pass
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _ai_history_budget_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client
    owner_id = await _get_owner_id()
    try:
        val = int(text.strip())
    except ValueError:
        result = "❌ Invalid number."
    else:
        if val > 0:
            from backend.ai.config_store import update_setting
            await update_setting(owner_id, "history_budget", val)
            result = f"✅ Context budget set to {val}"
        else:
            result = "❌ Must be positive."
    from backend.helper.client import get_client
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception:
            pass
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _ai_system_prompt_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client
    owner_id = await _get_owner_id()
    from backend.ai.config_store import update_setting
    await update_setting(owner_id, "system_prompt", text.strip())
    result = "✅ System prompt updated."
    from backend.helper.client import get_client
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception:
            pass
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


def register(client, owner_id: int) -> None:
    try:
        register_panel("ai", _ai_main_panel_handler, parent="menu", title="🧠 AI")
        register_inline_builder("ai", _ai_main_inline_builder)
        register_panel("ai_provider", _ai_provider_panel_handler, parent="ai", title="🔄 Provider")
        register_inline_builder("ai_provider", _ai_provider_inline_builder)
        register_panel("ai_model", _ai_model_panel_handler, parent="ai", title="🤖 Model")
        register_inline_builder("ai_model", _ai_model_inline_builder)
        register_panel("ai_wizard", _ai_wizard_panel_handler, parent="ai", title="Setup")
        register_inline_builder("ai_wizard", _ai_wizard_inline_builder)
        register_panel("ai_settings", _ai_settings_panel_handler, parent="ai", title="⚙️ Settings")
        register_inline_builder("ai_settings", _ai_settings_inline_builder)
        register_panel("ai_status", _ai_status_panel_handler, parent="ai", title="📊 Status")
        register_inline_builder("ai_status", _ai_status_inline_builder)
        register_panel("ai_diagnostics", _ai_diagnostics_panel_handler, parent="ai", title="🔧 Diagnostics")
        register_inline_builder("ai_diagnostics", _ai_diagnostics_inline_builder)
        register_action("ai_select_provider", _ai_select_provider_action)
        register_action("ai_select_model", _ai_select_model_action)
        register_action("ai_refresh_providers", _ai_refresh_providers_action)
        register_action("ai_refresh_models", _ai_refresh_models_action)
        register_action("ai_start_chat", _ai_start_chat_action)
        register_action("ai_status_refresh", _ai_status_refresh_action)
        register_action("ai_diagnostics_refresh", _ai_diagnostics_refresh_action)
        register_input("ai_settings", "temperature", {
            "handler": _ai_temperature_input,
            "prompt": "**🌡 Temperature**\n\nEnter value (0.0 – 2.0):\n\n_Reply below._",
        })
        register_input("ai_settings", "max_tokens", {
            "handler": _ai_max_tokens_input,
            "prompt": "**📦 Max Tokens**\n\nEnter a positive integer:\n\n_Reply below._",
        })
        register_input("ai_settings", "history_budget", {
            "handler": _ai_history_budget_input,
            "prompt": "**📝 Context Budget**\n\nEnter budget in tokens (positive integer):\n\n_Reply below._",
        })
        register_input("ai_settings", "system_prompt", {
            "handler": _ai_system_prompt_input,
            "prompt": "**💬 System Prompt**\n\nEnter your custom system prompt:\n(or 'reset' to use default)\n\n_Reply below._",
        })
        logger.info("AI panels registered OK (simplified UX)")
    except Exception as exc:
        logger.error("AI panel registration FAILED: %s", exc)
