"""
Focused tests for multi-provider infrastructure, cooldown/fallback state
machine, concurrency, diagnostics, and Persian digit normalization.

No real API keys or network access are required — provider HTTP is mocked.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager


# ── Stubs ──


class _StubProvider(BaseProvider):
    """Scripted provider that records call count and plays back responses."""

    def __init__(self, name: str, responses: list[ProviderResponse]) -> None:
        super().__init__(ProviderConfig(provider_name=name, enabled=True))
        self._name = name
        self._responses = list(responses)
        self.calls = 0

    @property
    def name(self) -> str:
        return self._name

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        self.calls += 1
        if self._responses:
            return self._responses.pop(0)
        return ProviderResponse(text="stub ok", provider_name=self._name, success=True)

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True}


def _failure(text: str, **meta: Any) -> ProviderResponse:
    return ProviderResponse(text=text, provider_name="", success=False, metadata=meta)


# ── Provider registration ──


def test_core_providers_registered_in_factory():
    from backend.ai.providers.factory import ProviderFactory

    available = ProviderFactory.available_providers()
    for name in (
        "gemini", "mistral", "zai", "groq", "sambanova",
        "nvidia", "cohere", "siliconflow", "cerebras", "fireworks",
        "nararouter",
    ):
        assert name in available, f"missing provider: {name}"


def test_provider_disabled_when_no_api_key(monkeypatch):
    from backend.ai.providers.factory import ProviderFactory

    for key in ("ZAI_API_KEY", "GEMINI_API_KEY", "GROQ_API_KEY", "MISTRAL_API_KEY"):
        monkeypatch.delenv(key, raising=False)

    registry = ProviderFactory.create_registry()
    assert registry.has("dummy")
    assert not registry.has("zai")
    assert not registry.has("gemini")


def test_env_key_auto_registers_provider(monkeypatch):
    from backend.ai.providers.factory import ProviderFactory

    monkeypatch.setenv("ZAI_API_KEY", "test-zai-key")
    registry = ProviderFactory.create_registry()
    assert registry.has("zai")
    assert registry.get("zai").is_enabled


def test_one_missing_provider_does_not_break_others(monkeypatch):
    from backend.ai.providers.factory import ProviderFactory

    monkeypatch.setenv("GROQ_API_KEY", "test-groq-key")
    registry = ProviderFactory.create_registry()
    assert registry.has("groq")
    # Gemini not configured, but registry still loads without raising.
    assert registry.has("dummy")


# ── OpenAI-compatible adapter for a new provider ──


@pytest.mark.asyncio
async def test_zai_provider_posts_openai_compat_payload():
    from backend.ai.providers.zai import ZaiProvider

    provider = ZaiProvider(ProviderConfig(
        api_key="k", enabled=True,
        base_url="https://api.z.ai/api/paas/v4", default_model="glm-4.5-flash",
    ))
    mock_resp = MagicMock()
    mock_resp.status_code = 200
    mock_resp.json.return_value = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {},
    }
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(return_value=mock_resp)

    result = await provider.chat([{"role": "user", "content": "hi"}])

    assert result.success is True
    url = provider._http_client.post.call_args.args[0]
    assert url == "https://api.z.ai/api/paas/v4/chat/completions"


# ── Structured output / unknown action rejection ──


@pytest.mark.asyncio
async def test_unknown_action_rejected_by_executor():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.executor import ToolExecutor
    from backend.ai.tools.registry import ToolRegistry

    ctx = ToolContext(telegram=None, owner_id=1, tz_str="UTC")
    executor = ToolExecutor(ToolRegistry(), ctx)
    results = await executor.execute_calls([{"name": "invented_action", "arguments": {}}], owner_id=1)

    assert results[0].success is False
    assert results[0].error == "not_found"


# ── Persian / Arabic digit normalization ──


def test_normalize_persian_digits():
    from backend.ai.persian import normalize_digits

    assert normalize_digits("۱۰ پیام") == "10 پیام"
    assert normalize_digits("٠١٢٣٤٥٦٧٨٩") == "0123456789"


def test_coerce_int_accepts_persian_and_english():
    from backend.ai.persian import coerce_int

    assert coerce_int(10) == 10
    assert coerce_int("۱۰") == 10
    assert coerce_int("10") == 10
    assert coerce_int("١٠") == 10
    assert coerce_int("not-a-number") is None
    assert coerce_int(True) is None


@pytest.mark.asyncio
async def test_delete_tool_accepts_persian_count():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.delete import DeleteTool
    from backend.ai.tools.executor import ToolExecutor
    from backend.ai.tools.registry import ToolRegistry
    from unittest.mock import patch

    class FakeTelegram:
        client = object()

    ctx = ToolContext(telegram=FakeTelegram(), owner_id=1, tz_str="UTC", extra={"chat_id": -100})
    registry = ToolRegistry()
    registry.register(DeleteTool(ctx))
    executor = ToolExecutor(registry, ctx)

    with patch("backend.services.delete_service.do_del_last_n_real", AsyncMock(return_value=(10, 10, None))):
        results = await executor.execute_calls([{"name": "delete", "arguments": {"count": "۱۰"}}], owner_id=1)

    assert results[0].success is True
    assert results[0].data["count"] == 10


# ── Cooldown / fallback state machine ──


@pytest.mark.asyncio
async def test_429_long_window_cooldown_then_fallback_without_retry():
    # A LONG Retry-After must never stall the request: no wait, no retry —
    # the provider cools down and the chain fails over. (A SHORT window ≤5s
    # is honored with exactly one bounded retry — see test_35.)
    rate = _StubProvider("rate", [_failure("Rate limited.", http_status=429, retry_after=60)])
    backup = _StubProvider("backup", [ProviderResponse(text="recovered", provider_name="backup", success=True)])

    pm = ProviderManager()
    pm.register_provider(rate)
    pm.register_provider(backup)
    pm.switch_provider("rate")
    pm._fallback_chain = ["backup"]

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert response.provider_name == "backup"
    assert response.metadata.get("fallback") is True
    assert response.metadata.get("fallback_from") == "rate"
    assert rate.calls == 1, "429 must NOT retry the limited provider"
    assert pm.health_snapshot()["rate"]["state"] == "cooling_down"


@pytest.mark.asyncio
async def test_5xx_one_retry_then_fallback():
    server = _StubProvider("server", [
        _failure("boom", http_status=500),
        _failure("boom again", http_status=500),
    ])
    backup = _StubProvider("backup", [ProviderResponse(text="ok", provider_name="backup", success=True)])

    pm = ProviderManager()
    pm.register_provider(server)
    pm.register_provider(backup)
    pm.switch_provider("server")
    pm._fallback_chain = ["backup"]

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert response.provider_name == "backup"
    assert server.calls == 2, "5xx gets exactly one immediate retry"


@pytest.mark.asyncio
async def test_auth_failure_disables_provider_without_retry():
    auth = _StubProvider("auth", [_failure("bad key", http_status=401)])

    pm = ProviderManager()
    pm.register_provider(auth)
    pm.switch_provider("auth")
    pm._fallback_chain = []

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is False
    assert auth.calls == 1, "auth failure must not retry"
    assert pm.health_snapshot()["auth"]["state"] == "disabled"


@pytest.mark.asyncio
async def test_network_error_one_retry_then_fallback():
    class NetworkFailProvider(_StubProvider):
        async def chat(self, messages, **kwargs):
            self.calls += 1
            raise ConnectionError("boom")

    net = NetworkFailProvider("net", [])
    backup = _StubProvider("backup", [ProviderResponse(text="ok", provider_name="backup", success=True)])

    pm = ProviderManager()
    pm.register_provider(net)
    pm.register_provider(backup)
    pm.switch_provider("net")
    pm._fallback_chain = ["backup"]

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert response.provider_name == "backup"
    assert net.calls == 2, "network error gets exactly one immediate retry"


def test_cooldown_state_machine_transitions():
    from backend.ai.providers.manager.health import ProviderHealthTracker

    tracker = ProviderHealthTracker()
    assert tracker.is_available("x") is True

    tracker.mark_cooling_down("x", 100)
    assert tracker.state("x") == "cooling_down"
    assert tracker.is_available("x") is False

    tracker.mark_healthy("x")
    assert tracker.is_available("x") is True

    tracker.mark_disabled("x")
    assert tracker.state("x") == "disabled"
    assert tracker.is_available("x") is False

    tracker.mark_healthy("x")
    assert tracker.is_available("x") is True


def test_cooldown_expires_and_provider_becomes_available():
    from backend.ai.providers.manager.health import ProviderHealthTracker

    tracker = ProviderHealthTracker()
    tracker.mark_cooling_down("x", 0.0)
    assert tracker.is_available("x") is True


def test_zai_concurrency_override():
    pm = ProviderManager()
    zai_sem = pm._health.acquire("zai")
    gemini_sem = pm._health.acquire("gemini")
    assert zai_sem._value == 2
    assert gemini_sem._value == 4


# ── Diagnostics / no secret leakage ──


@pytest.mark.asyncio
async def test_no_secret_leakage_in_failure_response():
    from backend.ai.providers.openai_compat import OpenAICompatProvider

    secret = "sk-super-secret-123"
    provider = OpenAICompatProvider(ProviderConfig(
        api_key=secret, enabled=True,
        base_url="https://api.example.com/v1", default_model="m",
    ))
    mock_resp = MagicMock()
    mock_resp.status_code = 401
    mock_resp.headers = {}
    mock_resp.json.return_value = {"error": {"message": "invalid api key"}}
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(return_value=mock_resp)

    result = await provider.chat([{"role": "user", "content": "hi"}])

    assert result.success is False
    assert secret not in result.text
    assert secret not in str(result.metadata)


@pytest.mark.asyncio
async def test_provider_timeout_is_classified_and_cools_down():
    from backend.ai.providers.manager.health import ProviderHealthTracker

    class TimeoutProvider(_StubProvider):
        async def chat(self, messages, **kwargs):
            self.calls += 1
            raise TimeoutError("timed out")

    provider = TimeoutProvider("slow", [])
    pm = ProviderManager()
    pm.register_provider(provider)
    pm.switch_provider("slow")
    pm._fallback_chain = []

    await pm.chat([{"role": "user", "content": "hi"}])

    assert provider.calls == 2
    assert pm.health_snapshot()["slow"]["state"] == "cooling_down"


# ── Executor receives normalized intent (service-layer convergence) ──


@pytest.mark.asyncio
async def test_delete_tool_routes_through_service_not_client():
    from backend.ai.tools.context import ToolContext
    from backend.ai.tools.delete import DeleteTool
    from backend.ai.tools.executor import ToolExecutor
    from backend.ai.tools.registry import ToolRegistry
    from unittest.mock import AsyncMock, patch

    class FakeTelegram:
        client = object()  # the tool must NOT call the raw client

    ctx = ToolContext(telegram=FakeTelegram(), owner_id=1, tz_str="UTC", extra={"chat_id": -100})
    registry = ToolRegistry()
    registry.register(DeleteTool(ctx))
    executor = ToolExecutor(registry, ctx)

    with patch("backend.services.delete_service.do_del_last_n_real", AsyncMock(return_value=(2, 2, None))) as svc:
        results = await executor.execute_calls([{"name": "delete", "arguments": {"count": 2}}], owner_id=1)

    svc.assert_awaited_once()
    assert results[0].success is True
