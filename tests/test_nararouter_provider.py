"""
Focused tests for the NaraRouter provider integration.

NaraRouter is an OpenAI-compatible gateway (base URL
``https://router.bynara.id/v1``). All HTTP is mocked — no live credentials
are required or used.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager


def _http_response(status_code: int, payload: dict | None = None) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload if payload is not None else {"error": {"message": "boom"}}
    resp.text = "boom"
    resp.headers = {}
    return resp


def _make_provider(config: ProviderConfig | None = None) -> Any:
    from backend.ai.providers.nararouter import NaraRouterProvider

    provider = NaraRouterProvider(config)
    provider._http_client = MagicMock()
    return provider


def _stub_backup() -> Any:
    class _Backup(BaseProvider):
        def __init__(self) -> None:
            super().__init__(ProviderConfig(provider_name="backup", enabled=True))

        @property
        def name(self) -> str:
            return "backup"

        async def chat(self, messages, **kwargs):
            return ProviderResponse(text="backup ok", provider_name="backup", success=True)

        def initialize(self) -> None:
            return None

        def shutdown(self) -> None:
            return None

        def count_tokens(self, text: str) -> int:
            return max(1, len(text) // 4)

        def health(self) -> dict[str, Any]:
            return {"healthy": True}

    return _Backup()


# ── Registration / configuration ──


def test_nararouter_registered_in_factory():
    from backend.ai.providers.factory import ProviderFactory

    assert "nararouter" in ProviderFactory.available_providers()


def test_nararouter_default_config():
    from backend.ai.providers.base.defaults import get_provider_default

    cfg = get_provider_default("nararouter")
    assert cfg.provider_name == "nararouter"
    assert cfg.base_url == "https://router.bynara.id/v1"
    assert cfg.default_model == "deepseek-v4-flash"
    assert cfg.api_key == ""
    assert cfg.enabled is False


def test_env_key_auto_registers_nararouter(monkeypatch):
    from backend.ai.providers.factory import ProviderFactory

    monkeypatch.setenv("NARAROUTER_API_KEY", "sk-nry-test-key")
    registry = ProviderFactory.create_registry()
    assert registry.has("nararouter")
    provider = registry.get("nararouter")
    assert provider.is_enabled
    assert provider.config.api_key == "sk-nry-test-key"
    assert provider.config.base_url == "https://router.bynara.id/v1"


def test_env_model_and_base_url_overrides(monkeypatch):
    from backend.ai.providers.factory import ProviderFactory

    monkeypatch.setenv("NARAROUTER_API_KEY", "sk-nry-test-key")
    monkeypatch.setenv("AI_NARAROUTER_MODEL", "combo/free-pack")
    monkeypatch.setenv("AI_NARAROUTER_BASE_URL", "https://router.bynara.id/custom")
    registry = ProviderFactory.create_registry()
    provider = registry.get("nararouter")
    assert provider.config.default_model == "combo/free-pack"
    assert provider.config.base_url == "https://router.bynara.id/custom"


def test_discovery_metadata_includes_nararouter():
    from backend.ai.discovery import get_provider_info, get_supported_provider_names

    assert "nararouter" in get_supported_provider_names()
    info = get_provider_info("nararouter")
    assert info is not None
    assert info["default_base_url"] == "https://router.bynara.id/v1"
    assert info["env_vars"] == ["AI_NARAROUTER_API_KEY", "NARAROUTER_API_KEY"]


def test_existing_providers_unaffected_by_nararouter_registration():
    from backend.ai.providers.factory import ProviderFactory

    available = ProviderFactory.available_providers()
    for name in ("gemini", "openai", "openrouter", "groq", "mistral", "dummy"):
        assert name in available


# ── Request construction ──


@pytest.mark.asyncio
async def test_chat_posts_openai_compat_payload_to_gateway():
    provider = _make_provider(ProviderConfig(
        api_key="sk-nry-secret", enabled=True,
        base_url="https://router.bynara.id/v1", default_model="deepseek-v4-flash",
    ))
    provider._http_client.post = AsyncMock(return_value=_http_response(200, {
        "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
    }))

    messages = [{"role": "user", "content": "hello"}]
    result = await provider.chat(messages)

    assert result.success is True
    assert result.text == "hi"
    url = provider._http_client.post.call_args.args[0]
    assert url == "https://router.bynara.id/v1/chat/completions"
    payload = provider._http_client.post.call_args.kwargs["json"]
    assert payload["model"] == "deepseek-v4-flash"
    assert payload["messages"] == messages


@pytest.mark.asyncio
async def test_tools_and_tool_choice_preserved():
    provider = _make_provider(ProviderConfig(
        api_key="sk-nry-secret", enabled=True,
        base_url="https://router.bynara.id/v1", default_model="deepseek-v4-flash",
    ))
    provider._http_client.post = AsyncMock(return_value=_http_response(200, {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {},
    }))

    tools = [{"type": "function", "function": {"name": "task_list", "parameters": {"type": "object"}}}]
    result = await provider.chat([{"role": "user", "content": "hi"}], tools=tools)

    assert result.success is True
    payload = provider._http_client.post.call_args.kwargs["json"]
    assert payload["tools"] == tools
    assert payload["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_combo_model_id_forwarded_opaque():
    # combo/<name> ids are valid NaraRouter aliases — never rejected or rewritten.
    provider = _make_provider(ProviderConfig(
        api_key="sk-nry-secret", enabled=True,
        base_url="https://router.bynara.id/v1", default_model="combo/free-pack",
    ))
    provider._http_client.post = AsyncMock(return_value=_http_response(200, {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {},
    }))

    result = await provider.chat([{"role": "user", "content": "hi"}])
    assert result.success is True
    payload = provider._http_client.post.call_args.kwargs["json"]
    assert payload["model"] == "combo/free-pack"


# ── Tool-call response normalization ──


@pytest.mark.asyncio
async def test_native_tool_call_normalized():
    provider = _make_provider(ProviderConfig(
        api_key="sk-nry-secret", enabled=True,
        base_url="https://router.bynara.id/v1", default_model="deepseek-v4-flash",
    ))
    provider._http_client.post = AsyncMock(return_value=_http_response(200, {
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "function": {"name": "task_list", "arguments": '{"status": "active"}'},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {},
    }))

    result = await provider.chat([{"role": "user", "content": "tasks"}])

    assert result.success is True
    assert result.tool_calls == [{"id": "call_1", "name": "task_list", "arguments": {"status": "active"}}]


@pytest.mark.asyncio
async def test_malformed_tool_arguments_flagged_not_executed():
    provider = _make_provider(ProviderConfig(
        api_key="sk-nry-secret", enabled=True,
        base_url="https://router.bynara.id/v1", default_model="deepseek-v4-flash",
    ))
    provider._http_client.post = AsyncMock(return_value=_http_response(200, {
        "choices": [{
            "message": {
                "content": None,
                "tool_calls": [{
                    "id": "call_1",
                    "function": {"name": "delete", "arguments": "{not-json"},
                }],
            },
            "finish_reason": "tool_calls",
        }],
        "usage": {},
    }))

    result = await provider.chat([{"role": "user", "content": "delete"}])

    call = result.tool_calls[0]
    assert call["malformed_arguments"] is True
    assert call["arguments"] == {}
    assert "arguments_error" in call


# ── Error mapping (NaraRouter error envelope) ──


@pytest.mark.asyncio
@pytest.mark.parametrize("status,expected_type,error_type", [
    (400, "request", "validation_error"),
    (401, "auth", "unauthorized"),
    (403, "auth", "forbidden"),
    (404, "model_not_found", "not_found"),
    (413, "request", "bad_request"),
    (415, "request", "unsupported_media_type"),
    (500, "server", "internal_error"),
    (503, "server", "service_unavailable"),
])
async def test_error_envelope_maps_to_existing_taxonomy(status, expected_type, error_type):
    provider = _make_provider(ProviderConfig(
        api_key="sk-nry-secret", enabled=True,
        base_url="https://router.bynara.id/v1", default_model="deepseek-v4-flash",
    ))
    payload = {"error": {"type": error_type, "message": "nope", "request_id": "req_abc123"}}
    provider._http_client.post = AsyncMock(return_value=_http_response(status, payload))

    result = await provider.chat([{"role": "user", "content": "hi"}])

    assert result.success is False
    meta = result.metadata
    assert meta["failure_type"] == expected_type
    assert meta["http_status"] == status
    assert meta["provider_error_type"] == error_type
    assert meta["provider_request_id"] == "req_abc123"


@pytest.mark.asyncio
async def test_429_maps_to_rate_limited_with_retry_after():
    provider = _make_provider(ProviderConfig(
        api_key="sk-nry-secret", enabled=True,
        base_url="https://router.bynara.id/v1", default_model="deepseek-v4-flash",
    ))
    resp = _http_response(429, {"error": {"type": "rate_limited", "message": "slow down", "request_id": "req_429"}})
    resp.headers = {"retry-after": "3"}
    provider._http_client.post = AsyncMock(return_value=resp)

    result = await provider.chat([{"role": "user", "content": "hi"}])

    assert result.success is False
    meta = result.metadata
    assert meta["failure_type"] == "rate_limited"
    assert meta["retry_after"] == 3.0
    assert meta["provider_request_id"] == "req_429"


@pytest.mark.asyncio
async def test_api_key_never_logged(caplog):
    provider = _make_provider(ProviderConfig(
        api_key="sk-nry-super-secret-value", enabled=True,
        base_url="https://router.bynara.id/v1", default_model="deepseek-v4-flash",
    ))
    provider._http_client.post = AsyncMock(return_value=_http_response(
        401, {"error": {"type": "unauthorized", "message": "bad key", "request_id": "req_401"}},
    ))

    with caplog.at_level(logging.WARNING):
        await provider.chat([{"role": "user", "content": "hi"}])

    assert "sk-nry-super-secret-value" not in caplog.text


# ── Manager integration: fallback, cooldown, recovery ──


@pytest.mark.asyncio
async def test_manager_falls_back_from_failing_nararouter():
    provider = _make_provider(ProviderConfig(
        api_key="sk-nry-secret", enabled=True,
        base_url="https://router.bynara.id/v1", default_model="deepseek-v4-flash",
    ))
    # 500 is retryable → exactly one bounded retry, then fallback.
    provider._http_client.post = AsyncMock(return_value=_http_response(
        500, {"error": {"type": "internal_error", "message": "boom", "request_id": "req_500"}},
    ))

    pm = ProviderManager()
    pm.register_provider(provider)
    backup = _stub_backup()
    pm.register_provider(backup)
    pm.switch_provider("nararouter")
    pm._fallback_chain = ["backup"]

    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert response.provider_name == "backup"
    assert response.metadata.get("fallback") is True
    assert response.metadata.get("fallback_from") == "nararouter"
    assert provider._http_client.post.await_count == 2, "5xx gets exactly one retry"
    assert pm.health_snapshot()["nararouter"]["state"] == "cooling_down"


@pytest.mark.asyncio
async def test_nararouter_cooldown_expires_and_provider_reenters_pool():
    provider = _make_provider(ProviderConfig(
        api_key="sk-nry-secret", enabled=True,
        base_url="https://router.bynara.id/v1", default_model="deepseek-v4-flash",
    ))
    # 429 with a SHORT retry-after: honored once, then the provider cools
    # down for exactly that window and re-enters the pool.
    resp = _http_response(429, {"error": {"type": "rate_limited", "message": "slow", "request_id": "req_429"}})
    resp.headers = {"retry-after": "0.05"}
    provider._http_client.post = AsyncMock(return_value=resp)

    pm = ProviderManager()
    pm.register_provider(provider)
    backup = _stub_backup()
    pm.register_provider(backup)
    pm.switch_provider("nararouter")
    pm._fallback_chain = ["backup"]

    response = await pm.chat([{"role": "user", "content": "hi"}])
    assert response.success is True
    assert response.provider_name == "backup"
    assert pm.health_snapshot()["nararouter"]["state"] == "cooling_down"
    assert pm.health_snapshot()["nararouter"]["cooldown_remaining_s"] > 0

    await asyncio.sleep(0.15)
    # After the cooldown the provider carries no penalty and is a routing
    # candidate again (absent from the snapshot = no active cooldown).
    assert "nararouter" not in pm.health_snapshot()
    assert pm._health.is_available("nararouter")


# ── Model discovery ──


@pytest.mark.asyncio
async def test_model_discovery_nararouter_fallback_catalog():
    from backend.ai.model_discovery import clear_cache, fetch_models, get_last_fetch_source, is_chat_capable

    assert is_chat_capable("deepseek-v4-flash") is True
    assert is_chat_capable("combo/free-pack") is True

    clear_cache()
    resp = MagicMock()
    resp.status_code = 500
    mock_client = AsyncMock()
    mock_client.get.return_value = resp
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False

    with patch("backend.ai.model_discovery.httpx.AsyncClient", return_value=mock_client):
        models = await fetch_models("nararouter", "sk-nry-secret", "https://router.bynara.id/v1")

    assert models, "nararouter must have an offline fallback catalog"
    ids = [m.id for m in models]
    assert "deepseek-v4-flash" in ids
    assert get_last_fetch_source("nararouter") == "fallback"