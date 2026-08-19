"""
Focused tests for the execution-reliability fixes:

- Groq/OpenAI-compatible requests never send a contradictory tool_choice.
- Stale Gemini model is resolved to a current one (data-driven deprecation).
- model_not_found is classified correctly (Gemini 404 → model_not_found).
- A dead model is marked unavailable and skipped on later requests.
- Account identity tool reads the authenticated self account via get_me.
- Deterministic Persian/English account-name intent resolves to account_show.
- Internal provider errors are humanized into clean, non-leaky messages.

No real API keys or network access required — provider HTTP and Telegram
facades are mocked.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.ai.actions import KIND_EXECUTABLE, parse_command_intent
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager


# ── tool_choice payload ──


@pytest.mark.asyncio
async def test_openai_compat_tools_set_tool_choice_to_auto():
    from backend.ai.providers.openai_compat import OpenAICompatProvider

    provider = OpenAICompatProvider(ProviderConfig(
        api_key="k", enabled=True,
        base_url="https://api.example.com/v1", default_model="m",
    ))
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {},
    }
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(return_value=mock_resp)

    await provider.chat(
        [{"role": "user", "content": "hi"}],
        tools=[{"type": "function", "function": {"name": "x", "parameters": {}}}],
    )

    payload = provider._http_client.post.call_args.kwargs["json"]
    assert payload["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_openai_compat_no_tools_omits_tool_choice():
    from backend.ai.providers.openai_compat import OpenAICompatProvider

    provider = OpenAICompatProvider(ProviderConfig(
        api_key="k", enabled=True,
        base_url="https://api.example.com/v1", default_model="m",
    ))
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {},
    }
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(return_value=mock_resp)

    await provider.chat([{"role": "user", "content": "hi"}])

    payload = provider._http_client.post.call_args.kwargs["json"]
    assert "tool_choice" not in payload
    assert "tools" not in payload


# ── Stale model resolution ──


def test_deprecated_gemini_model_is_substituted():
    from backend.ai.providers.base.defaults import resolve_model

    assert resolve_model("gemini", "gemini-2.0-flash") == "gemini-2.5-flash"
    assert resolve_model("gemini", "gemini-2.5-flash") == "gemini-2.5-flash"
    assert resolve_model("openai", "gpt-4o") == "gpt-4o"


def test_gemini_default_model_is_current():
    from backend.ai.providers.base.defaults import get_provider_default

    assert get_provider_default("gemini").default_model == "gemini-2.5-flash"


# ── Gemini 404 classification ──


@pytest.mark.asyncio
async def test_gemini_404_is_model_not_found():
    from backend.ai.providers.gemini import GeminiProvider

    provider = GeminiProvider(ProviderConfig(
        api_key="k", enabled=True, default_model="gemini-2.0-flash",
    ))
    mock_resp = MagicMock()
    mock_resp.status_code = 404
    mock_resp.json.return_value = {"error": {"message": "model not found"}}
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(return_value=mock_resp)

    result = await provider.chat([{"role": "user", "content": "hi"}])

    assert result.success is False
    assert result.metadata["failure_type"] == "model_not_found"
    assert result.metadata["model"] == "gemini-2.5-flash"  # deprecated model was resolved


# ── Dead model is skipped on later requests ──


class _ModelStub(BaseProvider):
    def __init__(self, name: str, model: str, responses: list[ProviderResponse]) -> None:
        super().__init__(ProviderConfig(provider_name=name, enabled=True, default_model=model))
        self._name = name
        self._responses = list(responses)
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True, supports_function_call=True)

    async def chat(self, messages, **kwargs):
        self.calls += 1
        if self._responses:
            return self._responses.pop(0)
        return ProviderResponse(text="ok", provider_name=self._name, success=True)

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True}


@pytest.mark.asyncio
async def test_model_not_found_marks_model_unavailable_and_skips():
    dead = _ModelStub("dead", "stale-model", [
        ProviderResponse(
            text="model not found", provider_name="dead", success=False,
            metadata={"http_status": 404, "failure_type": "model_not_found", "model": "stale-model"},
        ),
    ])
    backup = _ModelStub("backup", "ok-model", [
        ProviderResponse(text="ok", provider_name="backup", success=True),
    ])

    pm = ProviderManager()
    pm.register_provider(dead)
    pm.register_provider(backup)
    pm.switch_provider("dead")
    pm._fallback_chain = ["backup"]

    response = await pm.chat([{"role": "user", "content": "hi"}])
    assert response.success is True
    assert response.provider_name == "backup"
    assert ("dead", "stale-model") in pm._unavailable_models
    assert dead.calls == 1, "model_not_found must never be retried"


# ── Account identity tool ──


@pytest.mark.asyncio
async def test_account_show_reads_get_me():
    from backend.ai.tools.account import AccountShowTool
    from backend.ai.tools.context import ToolContext

    class FakeTelegram:
        async def get_me(self):
            return {
                "id": 123, "first_name": "Ali", "last_name": "Rezaei",
                "full_name": "Ali Rezaei", "username": "alirezaei", "phone": "989120000000",
            }

    ctx = ToolContext(telegram=FakeTelegram(), owner_id=1, tz_str="UTC")
    result = await AccountShowTool(ctx).execute(ctx, {})

    assert result.success is True
    assert "Ali Rezaei" in result.message
    assert "@alirezaei" in result.message
    assert result.data["first_name"] == "Ali"


@pytest.mark.asyncio
async def test_account_show_without_telegram_fails():
    from backend.ai.tools.account import AccountShowTool
    from backend.ai.tools.context import ToolContext

    ctx = ToolContext(telegram=None, owner_id=1, tz_str="UTC")
    result = await AccountShowTool(ctx).execute(ctx, {})
    assert result.success is False


def test_account_show_registered_in_default_registry():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.registry import create_default_registry

    registry = create_default_registry(ToolContext(telegram=None, owner_id=1, tz_str="UTC"))
    assert "account_show" in set(registry.list_names())


# ── Deterministic account-name intent ──


@pytest.mark.parametrize(
    "text",
    [
        "وضعیت اسم اکانتم رو بگو",
        "اسم اکانتم چیه؟",
        "نام اکانتم رو نشون بده",
        "what is my account name?",
        "what is my first name?",
    ],
)
def test_account_name_intent_resolves_to_account_show(text):
    r = parse_command_intent(text, has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{"name": "account_show", "arguments": {}}]


# ── Clean error humanization ──


def test_humanize_auth_failure_is_actionable_but_clean():
    from backend.bot.handlers.ai_unified import _humanize_error

    msg = _humanize_error("All AI providers failed. Last error: groq: 401 invalid api key")
    assert "authentication failed" in msg
    assert "groq" not in msg
    assert "sk-" not in msg
