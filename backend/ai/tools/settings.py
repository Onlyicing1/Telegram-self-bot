"""
Settings tools — wrap the authoritative setting stores.

Every key belongs to exactly ONE store:

- ``settings_service`` (``panel_settings`` table): Glass-panel settings
  (language, timeouts, fonts, ...). Synchronous get/set.
- ``config_store`` (``ai_config`` table): AI runtime configuration
  (provider, model, temperature, max_tokens, system_prompt,
  history_budget, triggers). The chat entry point
  (``ai_unified._restore_config``) re-applies this store to the runtime
  ``ProviderManager`` before EVERY request, so a ``settings_set`` on an
  AI key MUST write here — writing it to ``panel_settings`` persists
  nothing the runtime reads and the change silently never happens.

``provider`` / ``model`` additionally push into the live runtime through
``engine.apply_runtime_selection`` — the same single authoritative path
the web API and glass actions use — so the next request is served by the
requested (provider, model) pair immediately.

``SettingsGetTool`` is READ_ONLY. ``SettingsSetTool`` is ADMIN_ONLY —
the AI must ask the owner for confirmation before changing any setting.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext

logger = logging.getLogger(__name__)

#: AI runtime configuration keys owned by ``config_store`` (ai_config).
#: Everything NOT in this set is a Glass-panel key owned by
#: ``settings_service`` (panel_settings) and keeps the existing path.
_AI_CONFIG_KEYS: frozenset[str] = frozenset({
    "provider", "model", "temperature", "max_tokens",
    "system_prompt", "history_budget", "trigger_en", "trigger_fa",
})

_MAX_MODEL_LENGTH = 200


def _apply_runtime_selection(provider: str, model: str) -> None:
    """Push a (provider, model) selection into the live runtime engine.

    Same authoritative path as the web API and glass actions
    (``engine.apply_runtime_selection``). Failures are logged, never
    raised: the persisted ``config_store`` remains the source of truth
    and the chat entry point re-applies it on the next request.
    """
    try:
        from backend.ai.engine.engine import apply_runtime_selection
        apply_runtime_selection(provider, model)
    except Exception as exc:  # noqa: BLE001
        logger.warning("settings_set: runtime selection apply failed for %s/%s: %s", provider, model, exc)


async def _set_ai_config(context: ToolContext, key: str, value: Any) -> ToolResult:
    """Persist an AI runtime config key and apply it to the live runtime."""
    from backend.ai import config_store

    owner_id = context.owner_id

    if key == "provider":
        from backend.ai.discovery import get_provider_info
        provider = str(value).strip().lower()
        info = get_provider_info(provider)
        if info is None:
            from backend.ai.discovery import get_supported_provider_names
            supported = ", ".join(sorted(get_supported_provider_names()))
            return ToolResult(success=False, message=f"Unknown provider '{value}'. Supported providers: {supported}.")
        if info.get("capability_kind", "chat") != "chat":
            return ToolResult(success=False, message=f"Provider '{provider}' is not a chat provider.")
        # Discovery metadata describes EVERY supported provider by name, but
        # only providers whose API key exists in this process's ENV are
        # registered with the runtime ProviderManager. Persisting an
        # unregistered provider would create a phantom config that the AI
        # menu and settings_get display while the runtime keeps serving the
        # previous provider — reject it before persisting.
        try:
            from backend.ai.engine.engine import get_engine
            registered = get_engine().provider_manager.list_providers()
        except Exception as exc:  # noqa: BLE001
            logger.warning("settings_set: provider registration check failed: %s", exc)
            registered = []
        if provider not in registered:
            env_var = str((info.get("env_vars") or [""])[0] or "")
            return ToolResult(
                success=False,
                message=(
                    f"Provider '{provider}' is not available on this server "
                    f"(no API key configured — set {env_var} and restart)."
                ),
            )
        model = str(info.get("default_model", "") or "")
        ok = await config_store.update_provider(owner_id, provider, model)
        if not ok:
            return ToolResult(success=False, message="Failed to persist provider.")
        _apply_runtime_selection(provider, model)
        return ToolResult(success=True, message=f"Provider set to '{provider}' with model '{model}'.")

    if key == "model":
        model = str(value).strip()
        if not model:
            return ToolResult(success=False, message="Model must be a non-empty string.")
        if len(model) > _MAX_MODEL_LENGTH:
            return ToolResult(success=False, message=f"Model name is too long (max {_MAX_MODEL_LENGTH} chars).")
        config = await config_store.get_config(owner_id)
        provider = config.get("provider", "")
        if not provider:
            return ToolResult(success=False, message="No provider configured yet — set a provider first.")
        ok = await config_store.update_model(owner_id, model)
        if not ok:
            return ToolResult(success=False, message="Failed to persist model.")
        _apply_runtime_selection(provider, model)
        return ToolResult(success=True, message=f"Model set to '{model}'.")

    if key == "temperature":
        try:
            temperature = float(value)
        except (TypeError, ValueError):
            return ToolResult(success=False, message="Temperature must be a number between 0.0 and 2.0.")
        if not 0.0 <= temperature <= 2.0:
            return ToolResult(success=False, message="Temperature must be between 0.0 and 2.0.")
        ok = await config_store.update_setting(owner_id, "temperature", temperature)
        if not ok:
            return ToolResult(success=False, message="Failed to persist temperature.")
        return ToolResult(success=True, message=f"Temperature set to {temperature}.")

    if key in ("max_tokens", "history_budget"):
        try:
            limit = int(value)
        except (TypeError, ValueError):
            return ToolResult(success=False, message=f"{key} must be a positive integer.")
        if limit <= 0:
            return ToolResult(success=False, message=f"{key} must be a positive integer.")
        ok = await config_store.update_setting(owner_id, key, limit)
        if not ok:
            return ToolResult(success=False, message=f"Failed to persist {key}.")
        return ToolResult(success=True, message=f"{key} set to {limit}.")

    if key == "system_prompt":
        ok = await config_store.update_setting(owner_id, "system_prompt", str(value))
        if not ok:
            return ToolResult(success=False, message="Failed to persist system prompt.")
        return ToolResult(success=True, message="System prompt updated.")

    if key in ("trigger_en", "trigger_fa"):
        word = str(value).strip()
        if " " in word:
            return ToolResult(success=False, message="Trigger must be a single word.")
        config = await config_store.get_config(owner_id)
        other_key = "trigger_fa" if key == "trigger_en" else "trigger_en"
        other = str(config.get(other_key, "") or "")
        if word and other and word.lower() == other.lower():
            return ToolResult(success=False, message="English and Persian triggers must be different values.")
        ok = await config_store.update_setting(owner_id, key, word)
        if not ok:
            return ToolResult(success=False, message=f"Failed to persist {key}.")
        return ToolResult(success=True, message=f"{key} set to '{word}'.")

    return ToolResult(success=False, message=f"Unknown AI setting key '{key}'.")


class SettingsGetTool(Tool):
    """Read a bot setting value by key."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "settings_get"

    @property
    def description(self) -> str:
        return "Read a bot setting value by key."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "key": {
                "type": "string",
                "description": "The setting key to read.",
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_ONLY

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with setting value in message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        key = arguments.get("key")
        if not key:
            return ToolResult(success=False, message="Missing key argument.")

        if key in _AI_CONFIG_KEYS:
            from backend.ai import config_store
            try:
                config = await config_store.get_config(context.owner_id)
                value = config.get(key, "")
                return ToolResult(success=True, message=f"{key} = {value}", data={"key": key, "value": str(value)})
            except Exception as exc:  # noqa: BLE001
                return ToolResult(success=False, message=f"Settings get failed: {exc}")

        from backend.services import settings_service
        try:
            value = settings_service.get_setting(key)
            return ToolResult(success=True, message=f"{key} = {value}", data={"key": key, "value": str(value)})
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Settings get failed: {exc}")


class SettingsSetTool(Tool):
    """Write a bot setting value by key. Admin Only — requires confirmation."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "settings_set"

    @property
    def description(self) -> str:
        return "Set a bot setting value by key. Requires owner confirmation."

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "key": {
                "type": "string",
                "description": "The setting key to write.",
            },
            "value": {
                "type": "string",
                "description": "The value to set.",
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.ADMIN_ONLY

    @property
    def safe(self) -> bool:
        return False

    @property
    def return_type(self) -> str:
        return "ToolResult with confirmation message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        key = arguments.get("key")
        value = arguments.get("value")
        if not key or value is None:
            return ToolResult(success=False, message="Missing key or value argument.")

        if key in _AI_CONFIG_KEYS:
            return await _set_ai_config(context, key, value)

        from backend.services import settings_service
        try:
            success = settings_service.set_setting(key, value)
            if success:
                return ToolResult(success=True, message=f"Setting '{key}' updated to '{value}'.", data={"key": key, "value": str(value)})
            return ToolResult(success=False, message=f"Failed to set '{key}'. Value may be invalid.")
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Settings set failed: {exc}")