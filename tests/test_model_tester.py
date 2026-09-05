"""
Unit tests for AI Model Tester & POST /api/ai/test-models endpoint.
"""
from __future__ import annotations

import asyncio
import pytest
from unittest.mock import patch, AsyncMock
from fastapi.testclient import TestClient

from backend.ai.model_tester import sanitize_error_message, test_single_model, test_all_models
from backend.ai.providers.base.contract import ProviderResponse
from backend.web.app import app


def test_sanitize_error_message():
    raw_error = "Error calling API with key sk-1234567890abcdef and Bearer secrettoken123456"
    sanitized = sanitize_error_message(raw_error)
    assert "sk-1234567890abcdef" not in sanitized
    assert "secrettoken123456" not in sanitized
    assert "***REDACTED***" in sanitized


@pytest.mark.asyncio
async def test_single_model_not_configured():
    # Without ENV set for openrouter
    with patch("os.getenv", return_value=""):
        res = await test_single_model("openrouter", "OpenRouter", "🟢", "openrouter/auto")
        assert res["provider"] == "openrouter"
        assert res["status"] == "NOT_CONFIGURED"
        assert res["latency_s"] is None


@pytest.mark.asyncio
async def test_single_model_available():
    mock_response = ProviderResponse(
        text="pong",
        provider_name="openai",
        success=True,
        metadata={"latency": 0.25, "model": "gpt-4o"},
    )
    with patch("backend.ai.model_tester._get_env", return_value="fake_key"):
        with patch("backend.ai.providers.openai.OpenAIProvider.chat", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = mock_response
            res = await test_single_model("openai", "OpenAI", "🧠", "gpt-4o")
            assert res["status"] == "AVAILABLE"
            assert res["latency_s"] is not None
            assert res["http_status"] == 200
            assert res["error"] is None


@pytest.mark.asyncio
async def test_single_model_timeout():
    with patch("backend.ai.model_tester._get_env", return_value="fake_key"):
        with patch("backend.ai.providers.openai.OpenAIProvider.chat", side_effect=asyncio.TimeoutError()):
            res = await test_single_model("openai", "OpenAI", "🧠", "gpt-4o", timeout=0.1)
            assert res["status"] == "TIMEOUT"
            assert "timed out" in res["error"]


@pytest.mark.asyncio
async def test_single_model_unavailable_http_404():
    mock_response = ProviderResponse(
        text="Model not found",
        provider_name="groq",
        success=False,
        metadata={"http_status": 404},
    )
    with patch("backend.ai.model_tester._get_env", return_value="fake_key"):
        with patch("backend.ai.providers.groq.GroqProvider.chat", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = mock_response
            res = await test_single_model("groq", "Groq", "⚡", "unknown-model")
            assert res["status"] == "INVALID_MODEL"
            assert res["http_status"] == 404
            assert "Model not found" in res["error"]


@pytest.mark.asyncio
async def test_test_all_models_runs_all_targets():
    results_data = await test_all_models(owner_id=0, per_model_timeout=1.0)
    assert "results" in results_data
    assert "summary" in results_data
    assert isinstance(results_data["results"], list)
    assert len(results_data["results"]) >= 1

    summary = results_data["summary"]
    assert "total" in summary
    assert "available" in summary
    assert "unavailable" in summary
    assert "not_configured" in summary
    assert summary["total"] == len(results_data["results"])


@pytest.mark.asyncio
async def test_single_model_auth_error_401():
    mock_response = ProviderResponse(
        text="Invalid API key", provider_name="openai", success=False,
        metadata={"http_status": 401, "provider_error_type": "invalid_request_error"},
    )
    with patch("backend.ai.model_tester._get_env", return_value="fake_key"):
        with patch("backend.ai.providers.openai.OpenAIProvider.chat", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = mock_response
            res = await test_single_model("openai", "OpenAI", "🧠", "gpt-4o")
    assert res["status"] == "AUTH_ERROR"
    assert res["http_status"] == 401
    assert "Invalid API key" in res["error"]


@pytest.mark.asyncio
async def test_single_model_rate_limited_429():
    mock_response = ProviderResponse(
        text="Rate limited", provider_name="groq", success=False,
        metadata={"http_status": 429, "retry_after": 7},
    )
    with patch("backend.ai.model_tester._get_env", return_value="fake_key"):
        with patch("backend.ai.providers.groq.GroqProvider.chat", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = mock_response
            res = await test_single_model("groq", "Groq", "⚡", "llama-3.3-70b-versatile")
    assert res["status"] == "RATE_LIMITED"
    assert res["retry_after"] == 7
    assert "retry-after" in res["error"]


@pytest.mark.asyncio
async def test_single_model_blocked_400():
    mock_response = ProviderResponse(
        text="The response was blocked by the content filter", provider_name="gemini",
        success=False, metadata={"http_status": 400},
    )
    with patch("backend.ai.model_tester._get_env", return_value="fake_key"):
        with patch("backend.ai.providers.gemini.GeminiProvider.chat", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = mock_response
            res = await test_single_model("gemini", "Gemini", "💎", "gemini-2.0-flash")
    assert res["status"] == "BLOCKED"


@pytest.mark.asyncio
async def test_single_model_provider_error_500():
    mock_response = ProviderResponse(
        text="Internal server error", provider_name="mistral", success=False,
        metadata={"http_status": 500},
    )
    with patch("backend.ai.model_tester._get_env", return_value="fake_key"):
        with patch("backend.ai.providers.mistral.MistralProvider.chat", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = mock_response
            res = await test_single_model("mistral", "Mistral", "🌬", "mistral-large-latest")
    assert res["status"] == "PROVIDER_ERROR"
    assert res["http_status"] == 500


@pytest.mark.asyncio
async def test_single_model_unknown_error_no_http():
    mock_response = ProviderResponse(
        text="Unexpected failure", provider_name="cerebras", success=False,
        metadata={},
    )
    with patch("backend.ai.model_tester._get_env", return_value="fake_key"):
        with patch("backend.ai.providers.cerebras.CerebrasProvider.chat", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = mock_response
            res = await test_single_model("cerebras", "Cerebras", "🔥", "llama-3.3-70b")
    assert res["status"] == "UNKNOWN_ERROR"


def test_api_ai_test_models_endpoint():
    client = TestClient(app)
    response = client.post("/api/ai/test-models")
    assert response.status_code == 200
    data = response.json()
    assert "results" in data
    assert "summary" in data
    assert isinstance(data["results"], list)


@pytest.mark.asyncio
async def test_single_model_insufficient_credits_402():
    mock_response = ProviderResponse(
        text="Insufficient credits", provider_name="openrouter", success=False,
        metadata={"http_status": 402, "provider_error_type": "INSUFFICIENT_CREDITS"},
    )
    with patch("backend.ai.model_tester._get_env", return_value="fake_key"):
        with patch("backend.ai.providers.openrouter.OpenRouterProvider.chat", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = mock_response
            res = await test_single_model("openrouter", "OpenRouter", "🟢", "openrouter/auto")
    assert res["status"] == "INSUFFICIENT_CREDITS"
    assert res["http_status"] == 402


@pytest.mark.asyncio
async def test_single_model_insufficient_credits_textual():
    # OpenRouter-style failure without an HTTP 402: error type/text must still
    # classify as INSUFFICIENT_CREDITS, not a generic PROVIDER_ERROR.
    mock_response = ProviderResponse(
        text="You do not have enough credits to run this request",
        provider_name="openrouter", success=False,
        metadata={"provider_error_type": "INSUFFICIENT_CREDITS"},
    )
    with patch("backend.ai.model_tester._get_env", return_value="fake_key"):
        with patch("backend.ai.providers.openrouter.OpenRouterProvider.chat", new_callable=AsyncMock) as mock_chat:
            mock_chat.return_value = mock_response
            res = await test_single_model("openrouter", "OpenRouter", "🟢", "openrouter/auto")
    assert res["status"] == "INSUFFICIENT_CREDITS"


@pytest.mark.asyncio
async def test_single_model_not_configured_makes_no_request():
    # A provider without a key must NEVER construct a provider instance
    # or make a network request.
    with patch("backend.ai.model_tester._get_env", return_value=""):
        with patch("backend.ai.model_tester.ProviderFactory.create_provider", side_effect=AssertionError("must not create provider")):
            res = await test_single_model("mistral", "Mistral", "🌬", "mistral-large-latest")
    assert res["status"] == "NOT_CONFIGURED"
    assert res["latency_s"] is None


@pytest.mark.asyncio
async def test_test_all_models_caps_per_provider_and_filters():
    from backend.ai.discovery import ProviderStatus
    from backend.ai.model_discovery import ModelInfo

    fake_providers = [
        ProviderStatus(name="openai", display_name="OpenAI", env_var="AI_OPENAI_API_KEY", status="available", has_key=True, validated=True, default_model="gpt-4o", base_url="https://api.openai.com/v1", icon="🧠"),
        ProviderStatus(name="groq", display_name="Groq", env_var="AI_GROQ_API_KEY", status="available", has_key=True, validated=True, default_model="llama-3.3-70b-versatile", base_url="https://api.groq.com/openai/v1", icon="⚡"),
    ]

    async def fake_fetch(provider_name, api_key, base_url, force_refresh=False):
        prefix = provider_name
        ms = [ModelInfo(id=f"{prefix}-m{i:02d}", name=f"{prefix}-m{i:02d}", provider=provider_name) for i in range(20)]
        ms.append(ModelInfo(id="whisper-1", name="whisper-1", provider=provider_name))
        return ms

    async def fake_test(provider, display, icon, model, timeout=8.0):
        return {
            "provider": provider, "display_name": display, "icon": icon, "model": model,
            "status": "AVAILABLE", "error": None, "latency_s": 0.1, "http_status": 200,
            "retry_after": None, "error_type": None, "provider_code": None,
            "finish_reason": "stop", "capabilities": [],
        }

    with patch("backend.ai.model_tester.discover_providers", new_callable=AsyncMock) as mock_disc, \
         patch("backend.ai.model_tester._get_env", return_value="fake_key"), \
         patch("backend.ai.model_tester.fetch_models", side_effect=fake_fetch), \
         patch("backend.ai.model_tester.test_single_model", new_callable=AsyncMock) as mock_test:
        mock_disc.return_value = fake_providers
        mock_test.side_effect = fake_test
        data = await test_all_models(owner_id=0, per_model_timeout=0.5, overall_timeout=10, max_per_provider=3)

    results = data["results"]
    assert len(results) == 6  # 2 providers × max 3
    by_provider: dict[str, list[str]] = {}
    for r in results:
        by_provider.setdefault(r["provider"], []).append(r["model"])
    assert len(by_provider["openai"]) <= 3
    assert len(by_provider["groq"]) <= 3
    all_models = by_provider["openai"] + by_provider["groq"]
    assert all("whisper" not in m and "embedding" not in m for m in all_models)
    assert data["summary"]["available"] == 6
    assert data["summary"]["discovered"] >= 1
    assert data["summary"]["total"] == len(results)


@pytest.mark.asyncio
async def test_test_all_models_partial_on_overall_timeout():
    from backend.ai.discovery import ProviderStatus
    from backend.ai.model_discovery import ModelInfo

    fake_providers = [
        ProviderStatus(name="openai", display_name="OpenAI", env_var="AI_OPENAI_API_KEY", status="available", has_key=True, validated=True, default_model="gpt-4o", base_url="https://api.openai.com/v1", icon="🧠"),
    ]

    async def fake_fetch(provider_name, api_key, base_url, force_refresh=False):
        return [ModelInfo(id="gpt-4o", name="gpt-4o", provider="openai")]

    async def slow(*args, **kwargs):
        await asyncio.sleep(1)
        return {}

    with patch("backend.ai.model_tester.discover_providers", new_callable=AsyncMock) as mock_disc, \
         patch("backend.ai.model_tester._get_env", return_value="fake_key"), \
         patch("backend.ai.model_tester.fetch_models", side_effect=fake_fetch), \
         patch("backend.ai.model_tester.test_single_model", new_callable=AsyncMock) as mock_test:
        mock_disc.return_value = fake_providers
        mock_test.side_effect = slow
        data = await test_all_models(owner_id=0, per_model_timeout=5, overall_timeout=0.2)

    assert data["partial"] is True
    assert any(r["status"] == "TIMEOUT" for r in data["results"])
    assert data["summary"]["timeout"] >= 1


def test_api_ai_set_model_endpoint():
    client = TestClient(app)
    with patch("backend.ai.config_store.update_model", new_callable=AsyncMock) as mock_upd:
        mock_upd.return_value = True
        resp = client.post("/api/ai/model", json={"model": "gpt-4o"})
        assert resp.status_code == 200
        assert resp.json()["success"] is True
        assert resp.json()["model"] == "gpt-4o"
    resp2 = client.post("/api/ai/model", json={"model": ""})
    assert resp2.status_code == 400


def test_api_ai_set_provider_endpoint():
    from unittest.mock import MagicMock

    client = TestClient(app)
    engine = MagicMock()
    engine.provider_manager.list_providers.return_value = ["dummy", "openai"]
    with patch("backend.ai.config_store.update_provider", new_callable=AsyncMock) as mock_upd, \
         patch("backend.ai.engine.engine.get_engine", return_value=engine):
        mock_upd.return_value = True
        resp = client.post("/api/ai/provider", json={"provider": "openai"})
        assert resp.status_code == 200
        assert resp.json()["success"] is True
    resp2 = client.post("/api/ai/provider", json={"provider": "nonexistent"})
    assert resp2.status_code == 400


def test_api_ai_models_all_endpoint():
    from backend.ai.discovery import ProviderStatus
    from backend.ai.model_discovery import ModelInfo

    client = TestClient(app)
    fake = [
        ProviderStatus(name="openai", display_name="OpenAI", env_var="AI_OPENAI_API_KEY", status="available", has_key=True, validated=True, default_model="gpt-4o", base_url="https://api.openai.com/v1", icon="🧠"),
    ]

    async def fake_fetch(provider_name, api_key, base_url, force_refresh=False):
        return [ModelInfo(id="gpt-4o", name="gpt-4o", provider="openai")]

    with patch("backend.ai.discovery.discover_providers", new_callable=AsyncMock) as mock_disc, \
         patch("backend.ai.model_discovery.fetch_models", side_effect=fake_fetch):
        mock_disc.return_value = fake
        resp = client.get("/api/ai/models")
    assert resp.status_code == 200
    data = resp.json()
    assert "providers" in data
    assert data["providers"][0]["provider"] == "openai"
    assert data["providers"][0]["models"][0]["id"] == "gpt-4o"
