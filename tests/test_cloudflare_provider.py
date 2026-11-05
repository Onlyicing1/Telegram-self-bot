"""
Focused tests for the Cloudflare Workers AI provider integration.

Workers AI is an account-scoped, OpenAI-compatible provider
(``…/accounts/{account_id}/ai/v1``). All HTTP is mocked — no Cloudflare
credentials, account access or live API request is used anywhere here.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import httpx
import pytest

from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.cloudflare import (
    BASE_URL_TEMPLATE,
    CloudflareProvider,
    resolve_base_url,
)
from backend.ai.providers.manager.manager import ProviderManager

ACCOUNT_ID = "0123456789abcdef0123456789abcdef"
BASE_URL = f"https://api.cloudflare.com/client/v4/accounts/{ACCOUNT_ID}/ai/v1"
MODEL = "@cf/meta/llama-3.3-70b-instruct-fp8-fast"
TOKEN = "cf-token-super-secret-value"


def _http_response(
    status_code: int,
    payload: dict | None = None,
    headers: dict[str, str] | None = None,
) -> MagicMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload if payload is not None else {}
    resp.text = "upstream body"
    resp.headers = headers or {}
    return resp


def _cloudflare_error(code: int, message: str) -> dict[str, Any]:
    """The documented Workers AI error envelope."""
    return {"success": False, "errors": [{"code": code, "message": message}], "result": None}


def _make_provider(**overrides: Any) -> CloudflareProvider:
    config = ProviderConfig(
        api_key=TOKEN,
        enabled=True,
        base_url=BASE_URL,
        default_model=MODEL,
        timeout=60,
        **overrides,
    )
    provider = CloudflareProvider(config)
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


def _manager(provider: CloudflareProvider) -> ProviderManager:
    pm = ProviderManager()
    pm.register_provider(provider)
    pm.register_provider(_stub_backup())
    pm.switch_provider("cloudflare")
    pm._fallback_chain = ["backup"]
    return pm


# ── Registration / configuration ──


def test_cloudflare_registered_in_factory():
    from backend.ai.providers.factory import ProviderFactory

    assert "cloudflare" in ProviderFactory.available_providers()


def test_cloudflare_default_config():
    from backend.ai.providers.base.defaults import get_provider_default

    cfg = get_provider_default("cloudflare")
    assert cfg.provider_name == "cloudflare"
    # Account-scoped: the factory resolves the endpoint, never a static default.
    assert cfg.base_url == ""
    assert cfg.default_model == MODEL
    assert cfg.api_key == ""
    assert cfg.enabled is False


def test_base_url_is_account_scoped_and_requires_the_account_id():
    assert resolve_base_url(ACCOUNT_ID) == BASE_URL
    assert resolve_base_url("") == ""
    assert resolve_base_url("   ") == ""
    assert BASE_URL_TEMPLATE.endswith("/accounts/{account_id}/ai/v1")


def test_env_token_and_account_id_register_the_provider(monkeypatch):
    from backend.ai.providers.factory import ProviderFactory

    monkeypatch.setenv("AI_CLOUDFLARE_API_TOKEN", TOKEN)
    monkeypatch.setenv("AI_CLOUDFLARE_ACCOUNT_ID", ACCOUNT_ID)
    registry = ProviderFactory.create_registry()

    assert registry.has("cloudflare")
    provider = registry.get("cloudflare")
    assert provider.is_enabled
    assert provider.config.api_key == TOKEN
    assert provider.config.base_url == BASE_URL
    assert provider.config.default_model == MODEL


def test_unprefixed_token_alias_is_accepted(monkeypatch):
    from backend.ai.providers.factory import ProviderFactory

    monkeypatch.delenv("AI_CLOUDFLARE_API_TOKEN", raising=False)
    monkeypatch.setenv("CLOUDFLARE_API_TOKEN", TOKEN)
    monkeypatch.setenv("CLOUDFLARE_ACCOUNT_ID", ACCOUNT_ID)
    registry = ProviderFactory.create_registry()

    provider = registry.get("cloudflare")
    assert provider.config.api_key == TOKEN
    assert provider.config.base_url == BASE_URL


def test_missing_account_id_leaves_the_provider_unregistered(monkeypatch, caplog):
    """A token alone cannot address Workers AI — never register a broken URL."""
    from backend.ai.providers.factory import ProviderFactory

    monkeypatch.setenv("AI_CLOUDFLARE_API_TOKEN", TOKEN)
    monkeypatch.delenv("AI_CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("AI_CLOUDFLARE_BASE_URL", raising=False)
    monkeypatch.delenv("CLOUDFLARE_BASE_URL", raising=False)

    with caplog.at_level(logging.WARNING):
        registry = ProviderFactory.create_registry()

    assert not registry.has("cloudflare")
    assert "AI_CLOUDFLARE_ACCOUNT_ID" in caplog.text
    assert TOKEN not in caplog.text


def test_explicit_base_url_removes_the_account_id_requirement(monkeypatch):
    from backend.ai.providers.factory import ProviderFactory

    monkeypatch.setenv("AI_CLOUDFLARE_API_TOKEN", TOKEN)
    monkeypatch.delenv("AI_CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    monkeypatch.setenv("AI_CLOUDFLARE_BASE_URL", "https://proxy.internal/cf/v1")

    registry = ProviderFactory.create_registry()
    assert registry.get("cloudflare").config.base_url == "https://proxy.internal/cf/v1"


def test_env_model_override(monkeypatch):
    from backend.ai.providers.factory import ProviderFactory

    monkeypatch.setenv("AI_CLOUDFLARE_API_TOKEN", TOKEN)
    monkeypatch.setenv("AI_CLOUDFLARE_ACCOUNT_ID", ACCOUNT_ID)
    monkeypatch.setenv("AI_CLOUDFLARE_MODEL", "@cf/openai/gpt-oss-120b")
    registry = ProviderFactory.create_registry()

    assert registry.get("cloudflare").config.default_model == "@cf/openai/gpt-oss-120b"


def test_no_token_means_no_registration(monkeypatch):
    from backend.ai.providers.factory import ProviderFactory

    for var in (
        "AI_CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_TOKEN",
        "AI_CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID",
    ):
        monkeypatch.delenv(var, raising=False)
    assert "cloudflare" not in ProviderFactory.create_registry().list()


def test_existing_providers_unaffected_by_cloudflare_registration():
    from backend.ai.providers.factory import ProviderFactory

    available = ProviderFactory.available_providers()
    for name in (
        "gemini", "openai", "openrouter", "groq", "mistral", "dummy",
        "nararouter", "cohere", "fireworks",
    ):
        assert name in available


# ── Request construction ──


@pytest.mark.asyncio
async def test_chat_posts_to_the_account_scoped_endpoint_with_bearer_auth(monkeypatch):
    captured: dict[str, Any] = {}

    class _Client:
        def __init__(self, **kwargs: Any) -> None:
            captured["client_kwargs"] = kwargs

        async def post(self, url: str, json: dict | None = None, **_: Any) -> Any:
            captured["url"] = url
            captured["payload"] = json
            return _http_response(200, {
                "choices": [{"message": {"content": "hi"}, "finish_reason": "stop"}],
                "usage": {"prompt_tokens": 3, "completion_tokens": 1, "total_tokens": 4},
            })

    provider = _make_provider()
    provider._http_client = None
    monkeypatch.setattr(
        "backend.ai.providers.openai_compat.httpx.AsyncClient", _Client
    )

    messages = [{"role": "user", "content": "hello"}]
    result = await provider.chat(messages)

    assert result.success is True
    assert result.text == "hi"
    assert captured["url"] == f"{BASE_URL}/chat/completions"
    headers = captured["client_kwargs"]["headers"]
    assert headers["Authorization"] == f"Bearer {TOKEN}"
    assert headers["Content-Type"] == "application/json"
    assert captured["client_kwargs"]["timeout"] == 60

    payload = captured["payload"]
    assert payload["model"] == MODEL
    assert payload["messages"] == messages
    assert payload["temperature"] == 1.0
    assert payload["max_tokens"] == 4096


@pytest.mark.asyncio
async def test_per_call_model_override_is_forwarded_verbatim():
    provider = _make_provider()
    provider._http_client.post = AsyncMock(return_value=_http_response(200, {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {},
    }))

    await provider.chat(
        [{"role": "user", "content": "hi"}], model="@cf/openai/gpt-oss-120b"
    )

    payload = provider._http_client.post.call_args.kwargs["json"]
    # A Workers AI id keeps its '@' and '/' characters untouched.
    assert payload["model"] == "@cf/openai/gpt-oss-120b"


@pytest.mark.asyncio
async def test_tools_are_serialized_with_auto_tool_choice():
    provider = _make_provider()
    provider._http_client.post = AsyncMock(return_value=_http_response(200, {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {},
    }))

    tools = [{
        "type": "function",
        "function": {"name": "task_list", "parameters": {"type": "object"}},
    }]
    result = await provider.chat([{"role": "user", "content": "tasks"}], tools=tools)

    assert result.success is True
    payload = provider._http_client.post.call_args.kwargs["json"]
    assert payload["tools"] == tools
    assert payload["tool_choice"] == "auto"


@pytest.mark.asyncio
async def test_response_format_is_serialized_for_structured_output():
    provider = _make_provider()
    provider._http_client.post = AsyncMock(return_value=_http_response(200, {
        "choices": [{"message": {"content": "{}"}, "finish_reason": "stop"}],
        "usage": {},
    }))

    await provider.chat(
        [{"role": "user", "content": "json"}],
        response_format={"type": "json_object"},
    )

    payload = provider._http_client.post.call_args.kwargs["json"]
    assert payload["response_format"] == {"type": "json_object"}


# ── Response parsing ──


@pytest.mark.asyncio
async def test_successful_response_parsing_keeps_usage_and_finish_reason():
    provider = _make_provider()
    provider._http_client.post = AsyncMock(return_value=_http_response(200, {
        "choices": [{"message": {"content": "hello there"}, "finish_reason": "stop"}],
        "usage": {"prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16},
    }))

    result = await provider.chat([{"role": "user", "content": "hi"}])

    assert result.provider_name == "cloudflare"
    assert result.success is True
    assert result.text == "hello there"
    assert result.usage == {
        "prompt_tokens": 11, "completion_tokens": 5, "total_tokens": 16,
    }
    assert result.metadata["model"] == MODEL
    assert result.metadata["finish_reason"] == "stop"


@pytest.mark.asyncio
async def test_native_tool_call_is_normalized():
    provider = _make_provider()
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
    assert result.tool_calls == [
        {"id": "call_1", "name": "task_list", "arguments": {"status": "active"}}
    ]


@pytest.mark.asyncio
async def test_malformed_tool_arguments_are_flagged_not_executed():
    provider = _make_provider()
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


@pytest.mark.asyncio
async def test_empty_choices_is_an_empty_result_not_fabricated_text():
    provider = _make_provider()
    provider._http_client.post = AsyncMock(return_value=_http_response(
        200, {"choices": [], "usage": {}}
    ))

    result = await provider.chat([{"role": "user", "content": "hi"}])

    assert result.success is True
    assert result.text == ""
    assert result.tool_calls == []


# ── Error handling: Cloudflare's documented envelope and codes ──


@pytest.mark.asyncio
async def test_error_envelope_message_and_code_are_surfaced():
    provider = _make_provider()
    provider._http_client.post = AsyncMock(return_value=_http_response(
        400, _cloudflare_error(5007, "No such model @cf/nope/nope or task"),
    ))

    result = await provider.chat([{"role": "user", "content": "hi"}])

    assert result.success is False
    assert "No such model" in result.text
    assert result.metadata["http_status"] == 400
    assert result.metadata["provider_error_code"] == "5007"
    assert result.metadata["failure_type"] == "model_not_found"
    assert result.metadata["model"] == MODEL


@pytest.mark.asyncio
async def test_paid_plan_only_model_is_a_request_error_not_an_auth_failure():
    """5035 is a plan restriction: it must never disable the whole provider."""
    provider = _make_provider()
    provider._http_client.post = AsyncMock(return_value=_http_response(
        403, _cloudflare_error(5035, "This model requires a Workers Paid plan."),
    ))

    result = await provider.chat([{"role": "user", "content": "hi"}])

    assert result.success is False
    assert result.metadata["http_status"] == 403
    assert result.metadata["provider_error_code"] == "5035"
    assert result.metadata["failure_type"] == "request"


@pytest.mark.asyncio
async def test_rejected_token_is_an_auth_failure():
    provider = _make_provider()
    provider._http_client.post = AsyncMock(return_value=_http_response(
        401, _cloudflare_error(10000, "Authentication error"),
    ))

    result = await provider.chat([{"role": "user", "content": "hi"}])

    assert result.success is False
    assert result.metadata["failure_type"] == "auth"
    assert result.metadata["http_status"] == 401


@pytest.mark.asyncio
async def test_workers_ai_request_timeout_code_is_retryable():
    provider = _make_provider()
    provider._http_client.post = AsyncMock(return_value=_http_response(
        408, _cloudflare_error(3007, "Request timeout"),
    ))

    result = await provider.chat([{"role": "user", "content": "hi"}])

    assert result.success is False
    assert result.metadata["failure_type"] == "timeout"


@pytest.mark.asyncio
async def test_rate_limit_keeps_the_retry_after_window():
    provider = _make_provider()
    provider._http_client.post = AsyncMock(return_value=_http_response(
        429,
        _cloudflare_error(3036, "You have used up your daily free allocation"),
        headers={"retry-after": "7"},
    ))

    result = await provider.chat([{"role": "user", "content": "hi"}])

    assert result.success is False
    assert result.metadata["failure_type"] == "rate_limited"
    assert result.metadata["retry_after"] == 7.0
    assert result.metadata["http_status"] == 429


@pytest.mark.asyncio
async def test_transport_timeout_is_a_timeout_failure():
    provider = _make_provider()
    provider._http_client.post = AsyncMock(
        side_effect=httpx.TimeoutException("read timeout")
    )

    result = await provider.chat([{"role": "user", "content": "hi"}])

    assert result.success is False
    assert result.metadata["failure_type"] == "timeout"
    assert "timed out" in result.text


@pytest.mark.asyncio
async def test_non_json_error_body_degrades_to_the_first_bytes():
    provider = _make_provider()
    resp = _http_response(502)
    resp.json.side_effect = ValueError("not json")
    resp.text = "bad gateway"
    provider._http_client.post = AsyncMock(return_value=resp)

    result = await provider.chat([{"role": "user", "content": "hi"}])

    assert result.success is False
    assert "bad gateway" in result.text
    assert result.metadata["failure_type"] == "server"


@pytest.mark.asyncio
async def test_malformed_success_body_is_reported_not_invented():
    provider = _make_provider()
    resp = _http_response(200)
    resp.json.side_effect = ValueError("truncated body")
    provider._http_client.post = AsyncMock(return_value=resp)

    result = await provider.chat([{"role": "user", "content": "hi"}])

    assert result.success is False
    assert result.metadata["failure_type"] == "network"


@pytest.mark.asyncio
async def test_missing_configuration_returns_the_disabled_response():
    provider = CloudflareProvider(ProviderConfig(
        provider_name="cloudflare", api_key="", enabled=False, base_url=BASE_URL,
    ))
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock()

    result = await provider.chat([{"role": "user", "content": "hi"}])

    assert result.success is False
    assert result.text == "AI_DISABLED"
    provider._http_client.post.assert_not_awaited()


@pytest.mark.asyncio
async def test_list_models_makes_no_request():
    """Workers AI exposes no OpenAI-style /models endpoint."""
    provider = _make_provider()
    provider._http_client.get = AsyncMock()

    assert await provider.list_models() == []
    provider._http_client.get.assert_not_awaited()


def test_health_reports_missing_key_without_a_request():
    provider = CloudflareProvider(ProviderConfig(
        provider_name="cloudflare", api_key="", enabled=True, base_url=BASE_URL,
    ))
    assert provider.health()["healthy"] is False


@pytest.mark.asyncio
async def test_token_never_appears_in_logs_or_results(caplog):
    provider = _make_provider()
    provider._http_client.post = AsyncMock(return_value=_http_response(
        401, _cloudflare_error(10000, "Authentication error"),
    ))

    with caplog.at_level(logging.WARNING):
        result = await provider.chat([{"role": "user", "content": "hi"}])

    assert TOKEN not in caplog.text
    assert TOKEN not in result.text
    assert TOKEN not in str(result.metadata)


# ── Shared provider-manager behavior (selection / retry / fallback) ──


@pytest.mark.asyncio
async def test_manager_falls_back_from_a_failing_cloudflare_provider():
    provider = _make_provider()
    # 5xx is retryable → exactly one bounded retry, then fallback.
    provider._http_client.post = AsyncMock(return_value=_http_response(
        500, _cloudflare_error(3000, "internal error"),
    ))

    pm = _manager(provider)
    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert response.provider_name == "backup"
    assert response.metadata.get("fallback") is True
    assert response.metadata.get("fallback_from") == "cloudflare"
    assert provider._http_client.post.await_count == 2, "5xx gets exactly one retry"
    assert pm.health_snapshot()["cloudflare"]["state"] == "cooling_down"


@pytest.mark.asyncio
async def test_a_rejected_token_disables_cloudflare_and_the_backup_serves():
    provider = _make_provider()
    provider._http_client.post = AsyncMock(return_value=_http_response(
        401, _cloudflare_error(10000, "Authentication error"),
    ))

    pm = _manager(provider)
    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert response.provider_name == "backup"
    assert provider._http_client.post.await_count == 1, "auth is never retried"
    assert pm.health_snapshot()["cloudflare"]["state"] == "disabled"


@pytest.mark.asyncio
async def test_rate_limited_cloudflare_cools_down_then_reenters_the_pool():
    provider = _make_provider()
    provider._http_client.post = AsyncMock(return_value=_http_response(
        429,
        _cloudflare_error(3040, "Capacity temporarily exceeded"),
        headers={"retry-after": "0.05"},
    ))

    pm = _manager(provider)
    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.success is True
    assert response.provider_name == "backup"
    assert pm.health_snapshot()["cloudflare"]["state"] == "cooling_down"

    await asyncio.sleep(0.15)
    assert pm._health.is_available("cloudflare")


@pytest.mark.asyncio
async def test_unknown_model_is_not_retried_and_fails_over():
    provider = _make_provider()
    provider._http_client.post = AsyncMock(return_value=_http_response(
        400, _cloudflare_error(5007, "No such model @cf/nope/nope or task"),
    ))

    pm = _manager(provider)
    response = await pm.chat([{"role": "user", "content": "hi"}])

    assert response.provider_name == "backup"
    assert provider._http_client.post.await_count == 1, "model errors are not retried"


@pytest.mark.asyncio
async def test_a_paid_plan_error_does_not_disable_the_provider():
    provider = _make_provider()
    provider._http_client.post = AsyncMock(return_value=_http_response(
        403, _cloudflare_error(5035, "This model requires a Workers Paid plan."),
    ))

    pm = _manager(provider)
    await pm.chat([{"role": "user", "content": "hi"}])

    assert pm.health_snapshot().get("cloudflare", {}).get("state") != "disabled"
    assert pm._health.is_available("cloudflare")


# ── Discovery visibility ──


def _clear_discovery_cache() -> None:
    from backend.ai import discovery

    discovery._cache["timestamp"] = 0.0
    discovery._cache["results"] = []


def _probe_client(status_code: int) -> AsyncMock:
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = {"result": [], "success": True, "errors": []}
    resp.headers = {}
    resp.text = "probe"
    client = AsyncMock()
    client.get.return_value = resp
    client.__aenter__.return_value = client
    client.__aexit__.return_value = False
    return client


async def _cloudflare_status(monkeypatch, client, *, account_id: str = ACCOUNT_ID) -> Any:
    from backend.ai import discovery

    monkeypatch.setenv("AI_CLOUDFLARE_API_TOKEN", TOKEN)
    if account_id:
        monkeypatch.setenv("AI_CLOUDFLARE_ACCOUNT_ID", account_id)
    else:
        monkeypatch.delenv("AI_CLOUDFLARE_ACCOUNT_ID", raising=False)
        monkeypatch.delenv("CLOUDFLARE_ACCOUNT_ID", raising=False)
    _clear_discovery_cache()
    try:
        with patch("backend.ai.discovery.httpx.AsyncClient", return_value=client):
            results = await discovery.discover_providers(force_refresh=True)
    finally:
        _clear_discovery_cache()
    return next(r for r in results if r.name == "cloudflare"), results


@pytest.mark.asyncio
async def test_discovery_marks_cloudflare_available_and_verified(monkeypatch):
    client = _probe_client(200)
    status, results = await _cloudflare_status(monkeypatch, client)

    assert status.status == "available"
    assert status.has_key is True
    assert status.validated is True
    assert status.base_url == BASE_URL
    assert status.default_model == MODEL
    assert status.display_name == "Cloudflare Workers AI"
    assert client.get.call_args.args[0] == f"{BASE_URL}/models"
    assert client.get.call_args.kwargs["headers"] == {"Authorization": f"Bearer {TOKEN}"}
    # The provider is present in the full discovery list, not only in its own row.
    assert "cloudflare" in {r.name for r in results}


@pytest.mark.asyncio
async def test_discovery_reports_missing_account_id_without_a_probe(monkeypatch):
    client = _probe_client(200)
    status, _ = await _cloudflare_status(monkeypatch, client, account_id="")

    assert status.status == "not_configured"
    assert status.has_key is False
    assert status.base_url == ""
    client.get.assert_not_awaited()


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [401, 403])
async def test_discovery_reports_a_rejected_token_as_invalid(monkeypatch, code):
    status, _ = await _cloudflare_status(monkeypatch, _probe_client(code))
    assert status.status == "invalid"
    assert status.has_key is True


@pytest.mark.asyncio
@pytest.mark.parametrize("code", [404, 429, 500, 503])
async def test_discovery_keeps_cloudflare_selectable_on_an_inconclusive_probe(
    monkeypatch, code
):
    """A 404 (no /models endpoint) or a transient error is not a bad token."""
    status, _ = await _cloudflare_status(monkeypatch, _probe_client(code))
    assert status.status == "available"
    assert status.validated is False


def test_discovery_metadata_is_the_configured_endpoint_source():
    from backend.ai.discovery import get_provider_info, get_supported_provider_names

    assert "cloudflare" in get_supported_provider_names()
    info = get_provider_info("cloudflare")
    assert info["env_vars"] == ["AI_CLOUDFLARE_API_TOKEN", "CLOUDFLARE_API_TOKEN"]
    assert info["account_id_env"] == ["AI_CLOUDFLARE_ACCOUNT_ID", "CLOUDFLARE_ACCOUNT_ID"]
    assert info["default_base_url"] == BASE_URL_TEMPLATE
    assert info["default_model"] == MODEL


def test_wizard_lists_the_account_id_alongside_the_token():
    from backend.ai.discovery import get_wizard_info

    entry = next(p for p in get_wizard_info() if p["name"] == "cloudflare")
    assert entry["env_var"] == "AI_CLOUDFLARE_API_TOKEN"
    assert entry["extra_env"] == ["AI_CLOUDFLARE_ACCOUNT_ID"]


# ── Model catalog (no OpenAI-style /models endpoint) ──


@pytest.mark.asyncio
async def test_model_catalog_is_used_when_the_models_endpoint_is_absent():
    from backend.ai.model_discovery import (
        clear_cache,
        fetch_models,
        get_last_fetch_source,
        is_chat_capable,
    )

    resp = MagicMock()
    resp.status_code = 404
    mock_client = AsyncMock()
    mock_client.get.return_value = resp
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False

    clear_cache("cloudflare")
    try:
        with patch("backend.ai.model_discovery.httpx.AsyncClient", return_value=mock_client):
            models = await fetch_models("cloudflare", TOKEN, BASE_URL)
        assert get_last_fetch_source("cloudflare") == "fallback"
    finally:
        clear_cache("cloudflare")

    ids = [m.id for m in models]
    assert MODEL in ids
    assert all(model_id.startswith("@cf/") for model_id in ids)
    assert all(is_chat_capable(model_id) for model_id in ids)


def test_catalog_model_ids_survive_the_chat_capability_filter():
    from backend.ai.model_discovery import _FALLBACK_CATALOG, is_chat_capable

    for model_id in _FALLBACK_CATALOG["cloudflare"]:
        assert is_chat_capable(model_id), model_id


# ── Regression: the shared OpenAI-compatible error path is unchanged ──


@pytest.mark.asyncio
async def test_openai_error_envelope_still_parses_for_existing_providers():
    from backend.ai.providers.groq import GroqProvider

    provider = GroqProvider(ProviderConfig(
        provider_name="groq", api_key="gsk-secret", enabled=True,
        base_url="https://api.groq.com/openai/v1", default_model="llama-3.3-70b-versatile",
    ))
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(return_value=_http_response(
        400,
        {"error": {"message": "bad tool", "code": "tool_use_failed",
                   "type": "invalid_request_error", "request_id": "req_1"}},
    ))

    result = await provider.chat([{"role": "user", "content": "hi"}])

    assert result.success is False
    assert result.text == "API error (400): bad tool"
    assert result.metadata["provider_error_code"] == "tool_use_failed"
    assert result.metadata["provider_error_type"] == "invalid_request_error"
    assert result.metadata["provider_request_id"] == "req_1"
    assert result.metadata["failure_type"] == "request"


@pytest.mark.asyncio
async def test_existing_provider_auth_and_5xx_classification_is_unchanged():
    from backend.ai.providers.groq import GroqProvider

    def _provider() -> GroqProvider:
        p = GroqProvider(ProviderConfig(
            provider_name="groq", api_key="gsk-secret", enabled=True,
            base_url="https://api.groq.com/openai/v1",
        ))
        p._http_client = MagicMock()
        return p

    unauthorised = _provider()
    unauthorised._http_client.post = AsyncMock(return_value=_http_response(
        403, {"error": {"message": "forbidden"}},
    ))
    assert (await unauthorised.chat([{"role": "user", "content": "x"}])).metadata["failure_type"] == "auth"

    missing = _provider()
    missing._http_client.post = AsyncMock(return_value=_http_response(
        404, {"error": {"message": "no model"}},
    ))
    assert (await missing.chat([{"role": "user", "content": "x"}])).metadata["failure_type"] == "model_not_found"

    broken = _provider()
    broken._http_client.post = AsyncMock(return_value=_http_response(
        503, {"error": {"message": "down"}},
    ))
    assert (await broken.chat([{"role": "user", "content": "x"}])).metadata["failure_type"] == "server"
