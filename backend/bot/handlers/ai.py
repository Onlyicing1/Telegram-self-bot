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
  ai             — Overview (model · provider state · last request · context)
  ai_usage       — Compact usage summary (requests, tokens, failures, fallbacks)
  ai_health      — Is the AI working? (overall + one-line causes)
  ai_details     — Precise per-request facts from the execution record
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


def _apply_runtime_selection(provider: str, model: str) -> None:
    """Push a (provider, model) selection into the runtime engine.

    Same authoritative path as the web API and the chat entry points —
    never a parallel config store. Failures are logged, not raised; the
    persisted config_store remains the source of truth and chat re-applies
    it on the next request.
    """
    try:
        from backend.ai.engine.engine import apply_runtime_selection
        apply_runtime_selection(provider, model)
    except Exception as exc:
        logger.warning("AI panel: runtime selection apply failed: %s", exc)


async def _get_owner_id() -> int:
    from backend.helper.inline_engine import _owner_id
    return _owner_id


async def _get_saved_config(owner_id: int) -> dict:
    from backend.ai.config_store import get_config
    config = await get_config(owner_id)
    logger.info("[AI_TRACE] _get_saved_config owner_id=%s provider='%s' model='%s'", owner_id, config.get("provider", ""), config.get("model", ""))
    return config


async def _save_config(owner_id: int, config: dict) -> bool:
    from backend.ai.config_store import save_config
    logger.info("[AI_TRACE] _save_config owner_id=%s provider='%s' model='%s'", owner_id, config.get("provider", ""), config.get("model", ""))
    result = await save_config(owner_id, config)
    logger.info("[AI_TRACE] _save_config result=%s", result)
    return result


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


_PROVIDER_DISPLAY_FALLBACK = {
    "gemini": "Google",
    "openai": "OpenAI",
    "groq": "Groq",
    "mistral": "Mistral",
    "cerebras": "Cerebras",
    "cohere": "Cohere",
    "nvidia": "NVIDIA",
    "zai": "Z.ai",
    "openrouter": "OpenRouter",
    "sambanova": "SambaNova",
    "siliconflow": "SiliconFlow",
    "fireworks": "Fireworks",
    "dummy": "Built-in",
    "local": "Built-in",
}


def _provider_display(name: str) -> str:
    """User-facing provider name — never an internal id when avoidable."""
    clean = (name or "").strip()
    if not clean or clean == "—":
        return "—"
    try:
        from backend.ai.discovery import get_provider_info
        info = get_provider_info(clean)
        if info and info.get("display_name"):
            return str(info["display_name"])
    except Exception:
        pass
    return _PROVIDER_DISPLAY_FALLBACK.get(clean.lower(), clean.title())


async def _resolve_context_limit(provider: str, model: str) -> int:
    """Authoritative context limit for a model, from discovery metadata.

    Uses the discovery cache (refreshing it once if cold). Returns 0 when
    unknown — callers must show "limit unknown", never invent a number.
    """
    if not provider or not model or provider == "local":
        return 0
    from backend.ai.model_discovery import (
        fetch_models,
        get_api_key_for_provider,
        get_base_url_for_provider,
        get_model_context_length,
    )
    try:
        await fetch_models(
            provider,
            get_api_key_for_provider(provider),
            get_base_url_for_provider(provider),
        )
    except Exception:
        pass
    return get_model_context_length(provider, model)


def _context_line(context_tokens: int, max_context: int) -> str:
    """Honest context usage line — never invents a limit."""
    if context_tokens <= 0:
        return "Context unavailable"
    from backend.ai.engine.telemetry import format_tokens, remaining_context
    if max_context > 0:
        line = f"{format_tokens(context_tokens)} / {format_tokens(max_context)} context"
        left = remaining_context(context_tokens, max_context)
        if left is not None:
            line += f" · {format_tokens(left)} left"
        return line
    return f"{format_tokens(context_tokens)} context"


def _nav_buttons(builder: InlinePanelBuilder) -> None:
    builder.add_buttons(
        ("⬅ Back", "panel:_nav:back"),
        ("🏠 Home", "panel:_nav:home"),
    )


async def _ai_main_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    """LEVEL 1 — Overview. What the owner most commonly needs, nothing more.

    Renders from the normalized execution record (single source of truth);
    falls back to the persisted config only for identity when no request
    has run yet.
    """
    from backend.ai.engine.telemetry import compact_telemetry_line, telemetry

    owner_id = await _get_owner_id()
    config = await _get_saved_config(owner_id)
    engine_info = _get_engine_info()
    saved_provider = config.get("provider", "") or engine_info["provider"]
    saved_model = config.get("model", "") or engine_info["model"]

    if not saved_provider or saved_provider == "—":
        lines = ["**AI**\n", "⚠️ **No provider configured**", "",
                 "_Tap **Provider** to select one._"]
        builder = InlinePanelBuilder()
        builder.add_row("🔄 Select Provider", "panel:ai_provider")
        builder.add_row("🧪 Test Models", "action:ai_test_models")
        _nav_buttons(builder)
        return "AI", "\n".join(lines), builder.build()

    if not saved_model or saved_model == "—":
        lines = ["**AI**\n",
                 f"{saved_provider.title()} · ⚠️ **No model selected**", "",
                 "_Tap **Model** to select one._"]
        builder = InlinePanelBuilder()
        builder.add_row("🤖 Select Model", "panel:ai_model")
        builder.add_row("🔄 Change Provider", "panel:ai_provider")
        _nav_buttons(builder)
        return "AI", "\n".join(lines), builder.build()

    record = telemetry.last()
    connected = engine_info["connected"]
    state_text = "Ready" if connected else "Offline"

    lines = [f"**{saved_model}**"]
    lines.append(f"{_provider_display(saved_provider)} · {state_text}")
    lines.append("")
    if record is not None and record.status == "success":
        lines.append(compact_telemetry_line(record))
        limit = record.max_context
        if limit <= 0:
            limit = await _resolve_context_limit(
                record.provider or saved_provider, record.model or saved_model
            )
        lines.append(_context_line(record.context_tokens, limit))
    elif record is not None:
        lines.append(f"Last request failed — {record.error_reason}")
    else:
        lines.append("_No requests yet_")

    builder = InlinePanelBuilder()
    builder.add_row("💬 Start Chat", "action:ai_start_chat")
    builder.add_buttons(("📈 Usage", "panel:ai_usage"), ("🩺 Health", "panel:ai_health"))
    builder.add_buttons(("🔍 Details", "panel:ai_details"), ("🤖 Model", "panel:ai_model"))
    builder.add_buttons(("🔄 Provider", "panel:ai_provider"), ("⚙️ Settings", "panel:ai_settings"))
    builder.add_row("🧪 Test Models", "action:ai_test_models")
    _nav_buttons(builder)
    return "AI", "\n".join(lines), builder.build()


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


_MODEL_PAGE_SIZE = 16  # two-column grid: 8 rows × 2 buttons


def _model_callback_hash(model_id: str) -> str:
    """Stable 8-char id fingerprint so a stale index can never mis-select."""
    import hashlib
    return hashlib.sha1((model_id or "").encode("utf-8")).hexdigest()[:8]


def _model_button_label(m, current_model: str) -> str:
    """Compact two-column button label: current mark, name, free/ctx tag."""
    name = m.name or m.id.split("/")[-1]
    mark = "✓ " if m.id == current_model else ""
    label = f"{mark}{name}"
    if m.is_free:
        label += " ·free"
    elif m.context_length > 0:
        ctx_k = m.context_length // 1000
        if ctx_k > 0:
            label += f" ({ctx_k}K)"
    return label[:34]


async def _ai_model_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    owner_id = await _get_owner_id()
    config = await _get_saved_config(owner_id)
    provider_name = config.get("provider", "")
    logger.info("[AI_TRACE] model_panel_handler provider_name='%s' extra='%s'", provider_name, extra)
    if not provider_name:
        logger.warning("[AI_TRACE] model_panel_handler NO PROVIDER — config=%s", config)
        return "🤖 Model", "⚠️ Select a provider first.", [
            [InlinePanelBuilder().add_row("⬅ Back", "panel:ai").build()[0][0]],
        ]
    from backend.ai.model_discovery import (
        fetch_models,
        get_api_key_for_provider,
        get_base_url_for_provider,
        order_models_for_selector,
    )
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
    ordered = order_models_for_selector(models)
    total = len(ordered)
    total_pages = max(1, (total + _MODEL_PAGE_SIZE - 1) // _MODEL_PAGE_SIZE)
    page = max(0, min(page, total_pages - 1))
    start = page * _MODEL_PAGE_SIZE
    page_models = ordered[start:start + _MODEL_PAGE_SIZE]
    current_model = config.get("model", "")
    free_count = sum(1 for m in ordered if m.is_free)
    # One compact header line — vertical space belongs to the model grid,
    # not to repeated headings.
    header = f"_{total} models · page {page + 1}/{total_pages}"
    if free_count:
        header += f" · Free ({free_count}) first"
    lines = [header + "_"]
    builder = InlinePanelBuilder()
    # Two-column grid: pairs share one button row, so roughly twice as many
    # models fit per screen. Callbacks carry a stable list INDEX plus a short
    # id hash — never the raw id — so Telegram's 64-byte callback limit can't
    # truncate long ids into a wrong selection; a stale index/hash simply
    # re-renders instead of mis-selecting.
    for i in range(0, len(page_models), 2):
        pair = []
        for j in (i, i + 1):
            if j >= len(page_models):
                break
            m = page_models[j]
            data = (
                f"action:ai_model_pick_idx:{page}:{start + j}"
                f":{_model_callback_hash(m.id)}"
            )
            pair.append((_model_button_label(m, current_model), data))
        builder.add_buttons(*pair)
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
    from backend.ai.engine.telemetry import telemetry

    owner_id = await _get_owner_id()
    config = await _get_saved_config(owner_id)
    reply_stats = telemetry.get_telemetry_pref(owner_id)
    lines = [
        "**⚙️ AI Settings**\n",
        f"**Temperature:** {config.get('temperature', 1.0)}",
        f"**Max Tokens:** {config.get('max_tokens', 4096)}",
        f"**Context Budget:** {config.get('history_budget', 4000)} tokens",
        f"**Reply stats:** {'On' if reply_stats else 'Off'}",
    ]
    prompt = config.get("system_prompt", "")
    if prompt:
        lines.append(f"**System Prompt:** Custom ✅")
    else:
        lines.append(f"**System Prompt:** Default")

    trigger_en = config.get("trigger_en", "") or ""
    trigger_fa = config.get("trigger_fa", "") or ""
    lines.append("")
    lines.append("**Triggers:**")
    lines.append(f"  English: `{trigger_en or '—'}`")
    lines.append(f"  Persian: `{trigger_fa or '—'}`")
    if not trigger_en and not trigger_fa:
        lines.append("  ⚠️ Set at least one trigger to enable AI.")

    builder = InlinePanelBuilder()
    builder.add_row("🔤 Set English Trigger", "input:ai_settings:trigger_en")
    builder.add_row("🇮🇷 Set Persian Trigger", "input:ai_settings:trigger_fa")
    builder.add_row("🌡 Temperature", "input:ai_settings:temperature")
    builder.add_row("📦 Max Tokens", "input:ai_settings:max_tokens")
    builder.add_row("📝 Context Budget", "input:ai_settings:history_budget")
    builder.add_row("💬 System Prompt", "input:ai_settings:system_prompt")
    builder.add_row(
        f"📊 Reply stats: {'Off' if reply_stats else 'On'}",
        "action:ai_toggle_telemetry",
    )
    _nav_buttons(builder)
    return "⚙️ Settings", "\n".join(lines), builder.build()


async def _ai_settings_inline_builder(event, extra: str) -> list:
    result = await _ai_settings_panel_handler(event, extra)
    if result is None:
        return [render("Settings", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


_USAGE_RANGES = (("today", "Today"), ("7d", "7 days"), ("30d", "30 days"))


async def _ai_details_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    """LEVEL 2 — precise per-request facts from the execution record.

    Every value comes ONLY from the latest ``AIExecutionRecord`` — never
    from the persisted config — so a failed or engine-level execution can
    never display another request's identity or usage.
    """
    from backend.ai.engine.telemetry import (
        format_latency_exact,
        format_time_of,
        format_tokens_exact,
        remaining_context,
        telemetry,
    )

    record = telemetry.last()

    if record is None:
        lines = ["**AI · Details**\n", "_No AI requests yet._"]
    else:
        if record.token_source == "unavailable":
            tokens = "Unavailable"
        elif record.input_tokens == 0 and record.output_tokens == 0:
            tokens = "Unavailable"
        else:
            est = " ≈" if record.token_source == "estimated" else ""
            tokens = (
                f"{format_tokens_exact(record.input_tokens)} in · "
                f"{format_tokens_exact(record.output_tokens)} out{est}"
            )
        limit = record.max_context
        if limit <= 0:
            limit = await _resolve_context_limit(record.provider, record.model)
        if record.context_tokens > 0:
            context = format_tokens_exact(record.context_tokens)
            if limit > 0:
                left = remaining_context(record.context_tokens, limit)
                context += (
                    f" / {format_tokens_exact(limit)}"
                    + (f" · {format_tokens_exact(left)} left" if left is not None else "")
                )
            else:
                context += " · limit unknown"
        else:
            context = "Unavailable"
        status = "Ready" if record.status == "success" else f"Failed — {record.error_reason}"
        # The deterministic fast path runs no model at all — show that
        # honestly instead of leaking internal names.
        model_value = record.model if record.model and record.model != "deterministic" else "—"
        rows = [
            ("Model", model_value),
            ("Provider", _provider_display(record.provider)),
            ("Status", status),
            ("Context", context),
            ("Tokens", tokens),
            ("Latency", format_latency_exact(record.latency)),
            ("Retries", str(record.retry_count)),
            ("Backup", "Used" if record.fallback_used else "—"),
            ("Tools", str(record.tool_call_count)),
            ("When", format_time_of(record.timestamp)),
        ]
        width = max(len(label) for label, _ in rows)
        body = "\n".join(f"{label.ljust(width)}  {value}" for label, value in rows)
        lines = ["**AI · Details**\n", f"```\n{body}\n```"]

    builder = InlinePanelBuilder()
    builder.add_buttons(("📈 Usage", "panel:ai_usage"), ("❤️ Health", "panel:ai_health"))
    _nav_buttons(builder)
    return "AI · Details", "\n".join(lines), builder.build()


async def _ai_details_inline_builder(event, extra: str) -> list:
    result = await _ai_details_panel_handler(event, extra)
    if result is None:
        return [render("AI · Details", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


async def _ai_usage_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    """Compact usage summary over a selectable window. RAM-only telemetry."""
    from backend.ai.engine.telemetry import format_tokens, telemetry

    range_key = (extra or "").strip() or "today"
    if range_key not in dict(_USAGE_RANGES):
        range_key = "today"
    if range_key == "7d":
        summary = telemetry.summary(hours=24 * 7)
    elif range_key == "30d":
        summary = telemetry.summary(hours=24 * 30)
    else:
        summary = telemetry.summary(since_midnight_utc=True)
    label = dict(_USAGE_RANGES)[range_key]

    lines = ["**AI · Usage**\n", f"**{label}**", ""]
    if summary["requests"] == 0:
        lines.append("_No AI requests in this period._")
    else:
        lines.append(
            f"{summary['requests']} requests · {format_tokens(summary['total_tokens'])} tokens"
        )
        lines.append("")
        lines.append(
            f"{format_tokens(summary['input_tokens'])} in · "
            f"{format_tokens(summary['output_tokens'])} out"
        )
        lines.append("")
        issues = []
        if summary["failed"]:
            issues.append(f"{summary['failed']} failed")
        if summary["fallbacks"]:
            issues.append(f"{summary['fallbacks']} fallbacks")
        lines.append(" · ".join(issues) if issues else "No failures")

    builder = InlinePanelBuilder()
    builder.add_buttons(*[
        (f"{'✓ ' if key == range_key else ''}{lbl}", f"panel:ai_usage:{key}")
        for key, lbl in _USAGE_RANGES
    ])
    _nav_buttons(builder)
    return "AI · Usage", "\n".join(lines), builder.build()


async def _ai_usage_inline_builder(event, extra: str) -> list:
    result = await _ai_usage_panel_handler(event, extra)
    if result is None:
        return [render("AI · Usage", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


async def _ai_health_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    """Answer one question: is my AI working correctly right now?

    Three honest states with a one-line cause when something is wrong —
    diagnostics stay behind Details, never here.
    """
    from backend.ai.engine.telemetry import compact_telemetry_line, telemetry

    owner_id = await _get_owner_id()
    config = await _get_saved_config(owner_id)
    engine = _get_engine()
    engine_info = _get_engine_info()
    record = telemetry.last()
    configured = bool(config.get("provider"))

    if engine is None or not configured:
        overall = "OFFLINE"
    elif engine_info["connected"]:
        overall = "HEALTHY"
    else:
        overall = "DEGRADED"
    cause = ""
    if overall == "OFFLINE":
        cause = "No provider configured" if not configured else "AI engine unavailable"
    elif overall == "DEGRADED":
        if record is not None and record.status == "failed" and engine_info["connected"]:
            cause = f"Last request failed — {record.error_reason}"
        else:
            cause = "Provider unreachable"
    if overall == "HEALTHY" and record is not None and record.status == "failed":
        overall = "DEGRADED"
        cause = f"Last request failed — {record.error_reason}"

    headline = {
        "HEALTHY": "AI is healthy",
        "DEGRADED": "AI is degraded",
        "OFFLINE": "AI is offline",
    }[overall]
    lines = [f"**{headline}**"]
    if configured:
        model_name = config.get("model", "—") or engine_info["model"]
        lines.append(f"{model_name} · {_provider_display(config.get('provider', ''))}")
    if cause:
        lines.append(cause)
    lines.append("")

    try:
        available = [p for p in await _discover() if p.status == "available"]
        fallback_state = "Available" if len(available) > 1 else "Single provider"
    except Exception:
        fallback_state = "—"

    if record is not None and record.status == "success":
        lines.append(f"Last response · {compact_telemetry_line(record)}")
    elif record is not None:
        lines.append(f"Last request failed — {record.error_reason}")
    else:
        lines.append("No requests yet")
    lines.append(f"Backup · {fallback_state}")

    builder = InlinePanelBuilder()
    builder.add_row("🔄 Refresh", "action:ai_health_refresh")
    _nav_buttons(builder)
    return "AI · Health", "\n".join(lines), builder.build()


async def _ai_health_inline_builder(event, extra: str) -> list:
    result = await _ai_health_panel_handler(event, extra)
    if result is None:
        return [render("AI · Health", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


async def _ai_health_refresh_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    return await _ai_health_panel_handler(event, extra)


async def _ai_toggle_telemetry_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Flip the compact per-request chat telemetry preference and re-render."""
    from backend.ai.engine.telemetry import telemetry

    owner_id = await _get_owner_id()
    telemetry.set_telemetry_pref(owner_id, not telemetry.get_telemetry_pref(owner_id))
    return await _ai_settings_panel_handler(event, extra)


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
    logger.info("[AI_TRACE] select_provider START provider_name='%s' owner_id=%s", provider_name, owner_id)
    from backend.ai.discovery import get_provider_info
    info = get_provider_info(provider_name)
    if not info:
        logger.warning("[AI_TRACE] select_provider UNKNOWN provider '%s'", provider_name)
        return "Provider", "❌ Unknown provider.", []
    config = await _get_saved_config(owner_id)
    config["provider"] = provider_name
    config["model"] = info["default_model"]
    config["is_configured"] = True
    await _save_config(owner_id, config)
    logger.info("[AI_TRACE] select_provider SAVED provider='%s' model='%s'", provider_name, config["model"])
    _apply_runtime_selection(provider_name, config["model"])
    from backend.ai.model_discovery import fetch_models, get_api_key_for_provider, get_base_url_for_provider
    api_key = get_api_key_for_provider(provider_name)
    base_url = get_base_url_for_provider(provider_name)
    models = await fetch_models(provider_name, api_key, base_url)
    if models:
        first = models[0]
        config["model"] = first.id
        await _save_config(owner_id, config)
        _apply_runtime_selection(provider_name, first.id)
        logger.info("[AI_TRACE] select_provider MODEL_UPDATED model='%s'", first.id)
    logger.info("[AI_TRACE] select_provider → entering model panel")
    return await _ai_model_panel_handler(event, "")


async def _ai_select_model_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    model_id = extra.strip()
    owner_id = await _get_owner_id()
    logger.info("[AI_TRACE] select_model START model_id='%s' owner_id=%s", model_id, owner_id)
    config = await _get_saved_config(owner_id)
    config["model"] = model_id
    await _save_config(owner_id, config)
    _apply_runtime_selection(config.get("provider", ""), model_id)
    logger.info("[AI_TRACE] select_model SAVED provider='%s' model='%s'", config.get("provider", ""), model_id)
    return await _ai_main_panel_handler(event, "")


async def _ai_model_pick_idx_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Select a model from the two-column grid via a stable list position.

    Callback payload: ``<page>:<ordered-index>:<id-hash>``. The hash is
    verified against the freshly-resolved ordered list; on any mismatch
    (cache refreshed between render and tap) the panel re-renders instead
    of selecting the wrong model.
    """
    owner_id = await _get_owner_id()
    config = await _get_saved_config(owner_id)
    provider_name = config.get("provider", "")
    try:
        page_str, idx_str, received_hash = (extra.split(":") + [""])[:3]
        page = max(0, int(page_str))
        idx = int(idx_str)
    except ValueError:
        return await _ai_model_panel_handler(event, "")

    from backend.ai.model_discovery import (
        fetch_models,
        get_api_key_for_provider,
        get_base_url_for_provider,
        order_models_for_selector,
    )
    models = await fetch_models(
        provider_name,
        get_api_key_for_provider(provider_name),
        get_base_url_for_provider(provider_name),
    )
    ordered = order_models_for_selector(models)
    if not 0 <= idx < len(ordered):
        return await _ai_model_panel_handler(event, f"page:{page}")
    candidate = ordered[idx]
    if received_hash and _model_callback_hash(candidate.id) != received_hash:
        logger.info(
            "[AI_TRACE] pick_idx stale (idx=%d hash=%s) — re-rendering", idx, received_hash
        )
        return await _ai_model_panel_handler(event, f"page:{page}")

    config["model"] = candidate.id
    await _save_config(owner_id, config)
    _apply_runtime_selection(provider_name, candidate.id)
    logger.info(
        "[AI_TRACE] pick_idx SAVED provider='%s' model='%s'", provider_name, candidate.id
    )
    return await _ai_main_panel_handler(event, "")


async def _ai_pick_model_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Pick a tested-usable model — provider and model are selected TOGETHER.

    Callback data: ``ai_pick_model:<provider>:<model>``. Selecting a model
    this way also selects its provider automatically, persists both, and
    applies them to the runtime in one authoritative call — eliminating
    provider/model mismatches.
    """
    provider_name, _, model_id = extra.partition(":")
    provider_name = provider_name.strip()
    model_id = model_id.strip()
    owner_id = await _get_owner_id()
    logger.info("[AI_TRACE] pick_model START provider='%s' model='%s' owner_id=%s", provider_name, model_id, owner_id)
    if not provider_name or not model_id:
        return "🧠 AI", "❌ Invalid model selection.", []
    from backend.ai.discovery import get_provider_info
    if not get_provider_info(provider_name):
        return "🧠 AI", f"❌ Unknown provider '{provider_name}'.", []

    config = await _get_saved_config(owner_id)
    config["provider"] = provider_name
    config["model"] = model_id
    config["is_configured"] = True
    await _save_config(owner_id, config)
    _apply_runtime_selection(provider_name, model_id)
    logger.info("[AI_TRACE] pick_model SAVED + APPLIED provider='%s' model='%s'", provider_name, model_id)

    builder = InlinePanelBuilder()
    builder.add_row("💬 Start Chat", "action:ai_start_chat")
    builder.add_row("🔁 Re-run Tests", "action:ai_test_models")
    builder.add_row("📊 Status", "panel:ai_status")
    _nav_buttons(builder)
    return (
        "🧠 AI",
        f"**Model selected**\n\nProvider: **{provider_name}**\nModel: `{model_id}`\n\n"
        f"This pair is saved and applied to the runtime. Start chat to use it.",
        builder.build(),
    )


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
    trigger_en = config.get("trigger_en", "") or ""
    trigger_fa = config.get("trigger_fa", "") or ""
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
    if not trigger_en and not trigger_fa:
        return "🧠 AI", (
            "⚠️ No trigger words configured.\n\n"
            "Tap **Settings** to set your trigger words.\n"
            "You need at least one trigger to start chatting."
        ), [
            [InlinePanelBuilder().add_row("⚙️ Set Triggers", "panel:ai_settings").build()[0][0]],
            [InlinePanelBuilder().add_row("⬅ Back", "panel:ai").build()[0][0]],
        ]
    trigger_display = []
    if trigger_en:
        trigger_display.append(f"**English:** `{trigger_en}`")
    if trigger_fa:
        trigger_display.append(f"**Persian:** `{trigger_fa}`")
    triggers = "\n".join(trigger_display)
    return "🧠 AI", (
        f"✅ **Ready to chat!**\n\n"
        f"**Provider:** {provider.title()}\n"
        f"**Model:** {model}\n"
        f"**Triggers:**\n{triggers}\n\n"
        f"Send a message starting with your trigger word.\n"
        f"Example: `{trigger_en or trigger_fa} Hello, how are you?`\n\n"
        f"_The trigger word is removed before sending to the AI._"
    ), []


_TEST_STATUS_ICONS: dict[str, str] = {
    "AVAILABLE": "🟢",
    "NOT_CONFIGURED": "⚪",
    "AUTH_ERROR": "🔴",
    "RATE_LIMITED": "🟠",
    "TIMEOUT": "🟡",
    "PROVIDER_ERROR": "🔴",
    "INVALID_MODEL": "🔵",
    "BLOCKED": "🚫",
    "INSUFFICIENT_CREDITS": "💳",
    "UNKNOWN_ERROR": "❓",
    "ERROR": "❓",
}

# Most recent Test Models payload — powers the compact message and the
# one-tap "All Results" diagnostic view without re-running the tests.
_last_test_payload: dict = {}


def _short_model_name(model_id: str) -> str:
    """Compact model name for tight glass layouts."""
    if not model_id:
        return "?"
    return model_id.split("/")[-1]


async def _ai_test_models_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Run the model availability diagnostics inside the existing glass UI.

    Uses the same isolated tester as the web dashboard — it never touches
    conversation history, the active provider configuration, or the DB.
    """
    from backend.ai.model_tester import test_all_models

    owner_id = await _get_owner_id()
    try:
        results_payload = await test_all_models(owner_id=owner_id)
    except Exception as exc:
        logger.error("[AI_TRACE] test_models action failed: %s", exc)
        builder = InlinePanelBuilder()
        builder.add_row("🔄 Retry", "action:ai_test_models")
        builder.add_row("📊 Status", "panel:ai_status")
        _nav_buttons(builder)
        return "🧪 Test Models", f"**🧪 Test Models**\n\n❌ Diagnostic run failed: {exc}", builder.build()

    global _last_test_payload
    _last_test_payload = results_payload

    results = results_payload.get("results", [])
    summary = results_payload.get("summary", {})

    def s(key: str, default: int = 0) -> int:
        return summary.get(key, default)

    lines = ["**🧪 Model Tests**\n"]
    lines.append(
        f"_Available: {s('available')} · Failed: {s('failed')} · "
        f"Rate limited: {s('rate_limited')} · Not configured: {s('not_configured')} · "
        f"Invalid: {s('invalid')} · No credits: {s('insufficient_credits')}_"
    )
    lines.append("")

    # Compact layout: usable models are the focus (provider-grouped, model
    # name dominant, one line each). Failure details stay out of the main
    # message — one compact line per model, capped, full detail one tap away.
    usable = [r for r in results if r.get("status") == "AVAILABLE"]
    usable_sorted = sorted(
        usable,
        key=lambda x: (x.get("provider", ""), x.get("latency_s") if x.get("latency_s") is not None else 999),
    )

    if usable_sorted:
        lines.append("**✅ Usable Models**")
        current_provider = None
        for r in usable_sorted:
            provider = r.get("provider", "?")
            if provider != current_provider:
                current_provider = provider
                lines.append(f"🟢 **{r.get('display_name', provider)}**")
            lines.append(f"• `{_short_model_name(r.get('model', '?'))}`")
        lines.append("")
    else:
        lines.append("_No usable chat models right now._")
        lines.append("")

    failed = [r for r in results if r.get("status") != "AVAILABLE"]
    if failed:
        lines.append(f"**⚠️ Not usable: {len(failed)}**")
        for r in failed[:8]:
            status = r.get("status", "UNKNOWN_ERROR")
            icon = _TEST_STATUS_ICONS.get(status, "❓")
            lines.append(f"{icon} `{_short_model_name(r.get('model', '?'))}` — {status}")
        if len(failed) > 8:
            lines.append(f"_…and {len(failed) - 8} more_")
        lines.append("")

    # Buttons: usable models select provider+model together; everything else
    # is navigation/diagnostics. All existing actions preserved.
    builder = InlinePanelBuilder()
    if usable_sorted:
        for r in usable_sorted[:12]:
            label = f"🟢 {r.get('display_name', r.get('provider', '?'))} — {_short_model_name(r.get('model', '?'))}"
            latency = r.get("latency_s")
            if latency is not None:
                label += f" ({latency}s)"
            builder.add_row(label[:64], f"action:ai_pick_model:{r['provider']}:{r['model']}")
    builder.add_row("🔄 Re-run Tests", "action:ai_test_models")
    if results:
        builder.add_row("🔍 All Results", "action:ai_test_details")
    builder.add_row("🤖 Pick Model", "panel:ai_model")
    builder.add_row("📊 Status", "panel:ai_status")
    _nav_buttons(builder)
    return "🧪 Test Models", "\n".join(lines).rstrip(), builder.build()


async def _ai_test_details_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Show the full per-model diagnostics from the last Test Models run.

    Keeps the main model-selection message compact while preserving full
    failure details (HTTP status, retry-after, sanitized errors) one tap
    away. Uses the cached payload — never re-runs the tests.
    """
    results = (_last_test_payload or {}).get("results", [])
    if not results:
        builder = InlinePanelBuilder()
        builder.add_row("🧪 Run Tests", "action:ai_test_models")
        _nav_buttons(builder)
        return "🧪 Test Models", "**🧪 Model Tests**\n\n_No results yet. Tap **Run Tests** first._", builder.build()

    lines = ["**🧪 Model Tests — All Results**\n"]
    current_provider = None
    for r in sorted(results, key=lambda x: (x.get("provider", ""), x.get("model", ""))):
        provider = r.get("provider", "?")
        if provider != current_provider:
            current_provider = provider
            lines.append(f"**{r.get('display_name', provider)}**")
        status = r.get("status", "UNKNOWN_ERROR")
        icon = _TEST_STATUS_ICONS.get(status, "❓")
        model = r.get("model", "?")
        line = f"  {icon} `{model}` — {status}"
        latency = r.get("latency_s")
        if latency is not None:
            line += f" · {latency}s"
        lines.append(line)
        if r.get("http_status"):
            lines.append(f"      _HTTP {r['http_status']}_")
        if r.get("retry_after"):
            lines.append(f"      _retry-after: {r['retry_after']}s_")
        if r.get("error"):
            lines.append(f"      _{r['error'][:160]}_")
        lines.append("")

    builder = InlinePanelBuilder()
    builder.add_row("🔄 Re-run Tests", "action:ai_test_models")
    builder.add_row("🤖 Pick Model", "panel:ai_model")
    _nav_buttons(builder)
    return "🧪 Test Models", "\n".join(lines).rstrip(), builder.build()


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


async def _ai_trigger_en_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client
    owner_id = await _get_owner_id()
    text_stripped = text.strip()
    if text_stripped.lower() == "clear":
        from backend.ai.config_store import update_setting
        await update_setting(owner_id, "trigger_en", "")
        result = "✅ English trigger cleared."
    elif " " in text_stripped:
        result = "❌ Trigger must be a single word (no spaces)."
    else:
        config = await _get_saved_config(owner_id)
        existing_fa = config.get("trigger_fa", "") or ""
        if text_stripped and existing_fa and text_stripped.lower() == existing_fa.lower():
            result = "❌ English trigger must differ from Persian trigger."
        else:
            from backend.ai.config_store import update_setting
            await update_setting(owner_id, "trigger_en", text_stripped)
            result = f"✅ English trigger set to: `{text_stripped}`" if text_stripped else "✅ English trigger cleared."
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


async def _ai_trigger_fa_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client
    owner_id = await _get_owner_id()
    text_stripped = text.strip()
    if text_stripped.lower() == "clear":
        from backend.ai.config_store import update_setting
        await update_setting(owner_id, "trigger_fa", "")
        result = "✅ Persian trigger cleared."
    elif " " in text_stripped:
        result = "❌ Trigger must be a single word (no spaces)."
    else:
        config = await _get_saved_config(owner_id)
        existing_en = config.get("trigger_en", "") or ""
        if text_stripped and existing_en and text_stripped.lower() == existing_en.lower():
            result = "❌ Persian trigger must differ from English trigger."
        else:
            from backend.ai.config_store import update_setting
            await update_setting(owner_id, "trigger_fa", text_stripped)
            result = f"✅ Persian trigger set to: `{text_stripped}`" if text_stripped else "✅ Persian trigger cleared."
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
        register_panel("ai_usage", _ai_usage_panel_handler, parent="ai", title="📈 Usage")
        register_inline_builder("ai_usage", _ai_usage_inline_builder)
        register_panel("ai_health", _ai_health_panel_handler, parent="ai", title="🩺 Health")
        register_inline_builder("ai_health", _ai_health_inline_builder)
        register_panel("ai_details", _ai_details_panel_handler, parent="ai", title="🔍 Details")
        register_inline_builder("ai_details", _ai_details_inline_builder)
        register_panel("ai_status", _ai_status_panel_handler, parent="ai", title="📊 Status")
        register_inline_builder("ai_status", _ai_status_inline_builder)
        register_panel("ai_diagnostics", _ai_diagnostics_panel_handler, parent="ai", title="🔧 Diagnostics")
        register_inline_builder("ai_diagnostics", _ai_diagnostics_inline_builder)
        register_action("ai_select_provider", _ai_select_provider_action)
        register_action("ai_select_model", _ai_select_model_action)
        register_action("ai_model_pick_idx", _ai_model_pick_idx_action)
        register_action("ai_pick_model", _ai_pick_model_action)
        register_action("ai_refresh_providers", _ai_refresh_providers_action)
        register_action("ai_refresh_models", _ai_refresh_models_action)
        register_action("ai_start_chat", _ai_start_chat_action)
        register_action("ai_test_models", _ai_test_models_action)
        register_action("ai_test_details", _ai_test_details_action)
        register_action("ai_status_refresh", _ai_status_refresh_action)
        register_action("ai_diagnostics_refresh", _ai_diagnostics_refresh_action)
        register_action("ai_health_refresh", _ai_health_refresh_action)
        register_action("ai_toggle_telemetry", _ai_toggle_telemetry_action)
        register_input("ai_settings", "trigger_en", {
            "handler": _ai_trigger_en_input,
            "prompt": "**🔤 English Trigger**\n\nEnter a single trigger word (case-insensitive):\nOr send 'clear' to remove.\n\n_Reply below._",
        })
        register_input("ai_settings", "trigger_fa", {
            "handler": _ai_trigger_fa_input,
            "prompt": "**🇮🇷 Persian Trigger**\n\nEnter a single trigger word (exact match):\nOr send 'clear' to remove.\n\n_Reply below._",
        })
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
