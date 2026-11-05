"""
Unit tests for AI Model Tester & POST /api/ai/test-models endpoint.
"""
from __future__ import annotations

import asyncio
import pytest
from contextlib import ExitStack
from unittest.mock import patch, AsyncMock, MagicMock
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
async def test_test_all_models_covers_full_discovery_and_filters():
    """No hidden per-provider truncation: EVERY chat-capable catalog model is
    tested."""
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
        data = await test_all_models(owner_id=0, per_model_timeout=0.5, overall_timeout=10)

    results = data["results"]
    # Complete catalog coverage: all 20 discovered chat models per provider
    # (whisper filtered by capability). The provider default is NOT a member
    # of this catalog and is therefore never invented as an extra target.
    assert len(results) == 40
    by_provider: dict[str, list[str]] = {}
    for r in results:
        by_provider.setdefault(r["provider"], []).append(r["model"])
    assert len(by_provider["openai"]) == 20
    assert len(by_provider["groq"]) == 20
    assert "gpt-4o" not in by_provider["openai"]
    all_models = by_provider["openai"] + by_provider["groq"]
    assert all("whisper" not in m and "embedding" not in m for m in all_models)
    assert data["summary"]["available"] == 40
    assert data["summary"]["discovered"] >= 1
    assert data["summary"]["total"] == len(results)


@pytest.mark.asyncio
async def test_the_global_budget_argument_no_longer_truncates_targets():
    """The legacy ``global_budget`` argument is accepted and IGNORED: every
    eligible model of every provider is still tested."""
    from backend.ai.discovery import ProviderStatus
    from backend.ai.model_discovery import ModelInfo

    fake_providers = [
        ProviderStatus(name="openai", display_name="OpenAI", env_var="AI_OPENAI_API_KEY", status="available", has_key=True, validated=True, default_model="gpt-4o", base_url="https://api.openai.com/v1", icon="🧠"),
        ProviderStatus(name="groq", display_name="Groq", env_var="AI_GROQ_API_KEY", status="available", has_key=True, validated=True, default_model="llama-3.3-70b-versatile", base_url="https://api.groq.com/openai/v1", icon="⚡"),
    ]

    async def fake_fetch(provider_name, api_key, base_url, force_refresh=False):
        prefix = provider_name
        return [
            ModelInfo(id=f"{prefix}-m{i:02d}", name=f"{prefix}-m{i:02d}", provider=provider_name)
            for i in range(20)
        ]

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
        data = await test_all_models(
            owner_id=0, per_model_timeout=0.5, overall_timeout=10, global_budget=7,
        )

    by_provider: dict[str, list[str]] = {}
    for r in data["results"]:
        by_provider.setdefault(r["provider"], []).append(r["model"])
    # ``global_budget=7`` was requested above and had no effect.
    assert len(data["results"]) == 40
    assert len(by_provider.get("openai", [])) == 20
    assert len(by_provider.get("groq", [])) == 20


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
    # A target cancelled before it started is UNATTEMPTED, never a provider
    # timeout: the provider was never asked, and claiming otherwise would
    # present an untested model as a real response.
    assert any(r["status"] == "NOT_ATTEMPTED" for r in data["results"])
    assert all(r["status"] != "TIMEOUT" for r in data["results"])
    assert data["summary"]["not_attempted"] >= 1
    assert data["summary"]["timeout"] == 0
    assert data["summary"]["tested"] + data["summary"]["not_configured"] + data["summary"]["not_attempted"] == data["summary"]["total"]


@pytest.mark.asyncio
async def test_streaming_twin_covers_full_discovery_and_feeds_candidate_pool():
    """The streaming tester (Test Modules path) covers the COMPLETE intended
    candidate set — no 6-per-provider truncation — and one discovery pass
    feeds the production router's model-level candidate pool."""
    from backend.ai.discovery import ProviderStatus
    from backend.ai.model_discovery import ModelInfo

    fake_providers = [
        ProviderStatus(name="openai", display_name="OpenAI", env_var="AI_OPENAI_API_KEY", status="available", has_key=True, validated=True, default_model="gpt-4o", base_url="https://api.openai.com/v1", icon="🧠"),
    ]

    async def fake_fetch(provider_name, api_key, base_url, force_refresh=False):
        return [
            ModelInfo(id=f"m{i:02d}", name=f"m{i:02d}", provider=provider_name)
            for i in range(11)
        ]

    async def fake_test(provider, display, icon, model, timeout=8.0):
        return {
            "provider": provider, "display_name": display, "icon": icon, "model": model,
            "status": "AVAILABLE", "error": None, "latency_s": 0.1, "http_status": 200,
            "retry_after": None, "error_type": None, "provider_code": None,
            "finish_reason": "stop", "capabilities": [],
        }

    class _FakePM:
        def __init__(self):
            self.candidates: dict[str, list[str]] = {}

        def set_model_candidates(self, name, models):
            self.candidates[name] = models

    class _FakeEngine:
        provider_manager = _FakePM()

    fake_pm = _FakeEngine.provider_manager

    progress_events: list[tuple[int, int]] = []
    result_events: list[dict] = []

    with patch("backend.ai.model_tester.discover_providers", new_callable=AsyncMock) as mock_disc, \
         patch("backend.ai.model_tester._get_env", return_value="fake_key"), \
         patch("backend.ai.model_tester.fetch_models", side_effect=fake_fetch), \
         patch("backend.ai.model_tester.test_single_model", side_effect=fake_test), \
         patch("backend.ai.engine.engine.get_engine", return_value=_FakeEngine):
        mock_disc.return_value = fake_providers
        from backend.ai.model_tester import test_all_models_streaming

        data = await test_all_models_streaming(
            owner_id=0,
            on_progress=lambda d, t: progress_events.append((d, t)),
            on_result=lambda item: result_events.append(item),
        )

    # All 11 catalog models tested — the old hidden 6-per-provider cap would
    # have produced exactly 6, and no provider default is invented.
    assert len(data["results"]) == 11
    assert data["summary"]["available"] == 11
    # Streaming callbacks fired per completion.
    assert progress_events[-1] == (11, 11)
    assert len(result_events) == 11
    # One discovery pass armed the production candidate pool (free-first,
    # configured model excluded).
    assert fake_pm.candidates.get("openai") == [f"m{i:02d}" for i in range(11)]


@pytest.mark.asyncio
async def test_production_feed_exceeds_display_cap_and_payload_stays_bounded():
    """A discovery result LARGER than ``_MODELS_IN_RESPONSE`` feeds the
    COMPLETE set into the production candidate pool — the display/response
    cap must never determine what production fallback is allowed to see."""
    from backend.ai.discovery import ProviderStatus
    from backend.ai.model_discovery import ModelInfo
    from backend.ai.model_tester import _MODELS_IN_RESPONSE

    fake_providers = [
        ProviderStatus(name="openai", display_name="OpenAI", env_var="AI_OPENAI_API_KEY", status="available", has_key=True, validated=True, default_model="gpt-4o", base_url="https://api.openai.com/v1", icon="🧠"),
    ]

    total_models = _MODELS_IN_RESPONSE + 20  # 50: 20 beyond the display cap

    async def fake_fetch(provider_name, api_key, base_url, force_refresh=False):
        return [
            ModelInfo(id=f"m{i:03d}", name=f"m{i:03d}", provider=provider_name)
            for i in range(total_models)
        ]

    async def fake_test(provider, display, icon, model, timeout=8.0):
        return {
            "provider": provider, "display_name": display, "icon": icon, "model": model,
            "status": "AVAILABLE", "error": None, "latency_s": 0.1, "http_status": 200,
            "retry_after": None, "error_type": None, "provider_code": None,
            "finish_reason": "stop", "capabilities": [],
        }

    class _FakePM:
        def __init__(self):
            self.candidates: dict[str, list[str]] = {}

        def set_model_candidates(self, name, models):
            self.candidates[name] = models

    class _FakeEngine:
        provider_manager = _FakePM()

    fake_pm = _FakeEngine.provider_manager

    with patch("backend.ai.model_tester.discover_providers", new_callable=AsyncMock) as mock_disc, \
         patch("backend.ai.model_tester._get_env", return_value="fake_key"), \
         patch("backend.ai.model_tester.fetch_models", side_effect=fake_fetch), \
         patch("backend.ai.model_tester.test_single_model", side_effect=fake_test), \
         patch("backend.ai.engine.engine.get_engine", return_value=_FakeEngine):
        mock_disc.return_value = fake_providers
        from backend.ai.model_tester import test_all_models_streaming

        data = await test_all_models_streaming(owner_id=0)

    # The COMPLETE eligible discovery result reached the production pool —
    # including models positioned after the display cap index.
    fed = fake_pm.candidates.get("openai", [])
    assert len(fed) == total_models
    assert fed[0] == "m000"
    assert fed[-1] == f"m{total_models - 1:03d}"
    # A model past the display cap (index >= _MODELS_IN_RESPONSE) is fed.
    assert f"m{_MODELS_IN_RESPONSE:03d}" in fed
    assert f"m{total_models - 1:03d}" in fed
    # The response/display payload stays bounded by the display cap.
    assert len(data["models"]) <= _MODELS_IN_RESPONSE
    # Diagnostic test execution ran the full set — every catalog model.
    assert len(data["results"]) == total_models


@pytest.mark.asyncio
async def test_the_budget_argument_does_not_bound_execution_or_the_feed():
    """``global_budget`` is legacy and ignored: the production pool receives
    exactly the AVAILABLE tested models — never the raw discovery set."""
    from backend.ai.discovery import ProviderStatus
    from backend.ai.model_discovery import ModelInfo
    from backend.ai.model_tester import _MODELS_IN_RESPONSE

    fake_providers = [
        ProviderStatus(name="openai", display_name="OpenAI", env_var="AI_OPENAI_API_KEY", status="available", has_key=True, validated=True, default_model="gpt-4o", base_url="https://api.openai.com/v1", icon="🧠"),
    ]

    total_models = _MODELS_IN_RESPONSE + 20

    async def fake_fetch(provider_name, api_key, base_url, force_refresh=False):
        return [
            ModelInfo(id=f"m{i:03d}", name=f"m{i:03d}", provider=provider_name)
            for i in range(total_models)
        ]

    async def fake_test(provider, display, icon, model, timeout=8.0):
        return {
            "provider": provider, "display_name": display, "icon": icon, "model": model,
            "status": "AVAILABLE", "error": None, "latency_s": 0.1, "http_status": 200,
            "retry_after": None, "error_type": None, "provider_code": None,
            "finish_reason": "stop", "capabilities": [],
        }

    class _FakePM:
        def __init__(self):
            self.candidates: dict[str, list[str]] = {}

        def set_model_candidates(self, name, models):
            self.candidates[name] = models

    class _FakeEngine:
        provider_manager = _FakePM()

    fake_pm = _FakeEngine.provider_manager

    with patch("backend.ai.model_tester.discover_providers", new_callable=AsyncMock) as mock_disc, \
         patch("backend.ai.model_tester._get_env", return_value="fake_key"), \
         patch("backend.ai.model_tester.fetch_models", side_effect=fake_fetch), \
         patch("backend.ai.model_tester.test_single_model", side_effect=fake_test), \
         patch("backend.ai.engine.engine.get_engine", return_value=_FakeEngine):
        mock_disc.return_value = fake_providers
        from backend.ai.model_tester import test_all_models_streaming

        data = await test_all_models_streaming(owner_id=0, global_budget=5)

    # ``global_budget=5`` was requested above and had no effect: the whole
    # catalog was executed and proven.
    assert len(data["results"]) == total_models
    proven = {r["model"] for r in data["results"] if r["status"] == "AVAILABLE"}
    assert proven == {f"m{i:03d}" for i in range(total_models)}
    # Production pool = the AVAILABLE test result minus the configured model
    # (the router already tries it first).
    fed = fake_pm.candidates.get("openai", [])
    assert set(fed) == proven - {"gpt-4o"}
    assert len(fed) == total_models
    # The display payload stays bounded by the display cap while the pool is
    # bounded only by what was proven usable.
    assert len(data["models"]) <= _MODELS_IN_RESPONSE


@pytest.mark.asyncio
async def test_multiple_providers_each_feed_complete_sets():
    """Every real provider contributes its COMPLETE proven-AVAILABLE set to
    the production pool — proven by an uncapped diagnostic run, so neither
    the display cap nor the budget binds any provider."""
    from backend.ai.discovery import ProviderStatus
    from backend.ai.model_discovery import ModelInfo
    from backend.ai.model_tester import _MODELS_IN_RESPONSE

    fake_providers = [
        ProviderStatus(name="openai", display_name="OpenAI", env_var="AI_OPENAI_API_KEY", status="available", has_key=True, validated=True, default_model="gpt-4o", base_url="https://api.openai.com/v1", icon="🧠"),
        ProviderStatus(name="groq", display_name="Groq", env_var="AI_GROQ_API_KEY", status="available", has_key=True, validated=True, default_model="llama", base_url="https://api.groq.com/openai/v1", icon="⚡"),
    ]

    per_provider = _MODELS_IN_RESPONSE + 10

    async def fake_fetch(provider_name, api_key, base_url, force_refresh=False):
        return [
            ModelInfo(id=f"{provider_name}-m{i:03d}", name=f"{provider_name}-m{i:03d}", provider=provider_name)
            for i in range(per_provider)
        ]

    async def fake_test(provider, display, icon, model, timeout=8.0):
        return {
            "provider": provider, "display_name": display, "icon": icon, "model": model,
            "status": "AVAILABLE", "error": None, "latency_s": 0.1, "http_status": 200,
            "retry_after": None, "error_type": None, "provider_code": None,
            "finish_reason": "stop", "capabilities": [],
        }

    class _FakePM:
        def __init__(self):
            self.candidates: dict[str, list[str]] = {}

        def set_model_candidates(self, name, models):
            self.candidates[name] = models

    class _FakeEngine:
        provider_manager = _FakePM()

    fake_pm = _FakeEngine.provider_manager

    with patch("backend.ai.model_tester.discover_providers", new_callable=AsyncMock) as mock_disc, \
         patch("backend.ai.model_tester._get_env", return_value="fake_key"), \
         patch("backend.ai.model_tester.fetch_models", side_effect=fake_fetch), \
         patch("backend.ai.model_tester.test_single_model", side_effect=fake_test), \
         patch("backend.ai.engine.engine.get_engine", return_value=_FakeEngine):
        mock_disc.return_value = fake_providers
        from backend.ai.model_tester import test_all_models_streaming

        # Uncapped run so every discovered candidate is actually tested;
        # the pool is then the proven AVAILABLE set.
        data = await test_all_models_streaming(owner_id=0, global_budget=0)

    assert sum(
        1 for r in data["results"]
        if r["provider"] == "openai" and r["status"] == "AVAILABLE"
    ) == per_provider
    assert len(fake_pm.candidates.get("openai", [])) == per_provider
    assert len(fake_pm.candidates.get("groq", [])) == per_provider
    assert fake_pm.candidates["openai"][-1] == f"openai-m{per_provider - 1:03d}"
    assert fake_pm.candidates["groq"][-1] == f"groq-m{per_provider - 1:03d}"
    # Both providers' display payloads stay bounded.
    assert len(data["models"]) <= _MODELS_IN_RESPONSE * 2


@pytest.mark.asyncio
async def test_streaming_tester_error_path_renders_terminal_failure():
    """A crashing tester still renders a terminal state and clears the
    running flag — a failed run can never strand the module in 'running'."""
    from backend.bot.handlers import ai as ai_module
    from backend.bot.handlers import ai_test_progress

    fake_event = MagicMock()
    fake_event.chat_id = 111
    fake_event.message_id = 222
    ai_module._test_running = True
    try:
        with patch.object(
            ai_test_progress, "test_all_models_streaming",
            AsyncMock(side_effect=RuntimeError("boom")),
        ), patch("backend.helper.panels._safe_edit", AsyncMock(return_value=True)) as mock_edit:
            await ai_test_progress.run_streaming_test(42, fake_event)
            for _ in range(30):
                await asyncio.sleep(0)
    finally:
        ai_module._test_running = False

    assert ai_module._test_running is False
    # Terminal failure view rendered: unicode × mark, no colorful emoji.
    assert mock_edit.await_count >= 1
    assert "× Test run failed" in mock_edit.await_args.args[1]
    assert "🧪" not in mock_edit.await_args.args[1]


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


# ── Production eligibility = a Test Modules AVAILABLE result ──


def _pm_engine():
    class _FakePM:
        def __init__(self):
            self.candidates: dict[str, list[str]] = {}

        def set_model_candidates(self, name, models):
            self.candidates[name] = models

    class _FakeEngine:
        provider_manager = _FakePM()

    return _FakeEngine.provider_manager, _FakeEngine


def _provider_status(name, default_model):
    from backend.ai.discovery import ProviderStatus

    return ProviderStatus(
        name=name, display_name=name.title(), env_var=f"AI_{name.upper()}_API_KEY",
        status="available", has_key=True, validated=True, default_model=default_model,
        base_url=f"https://{name}.example/v1", icon="◇",
    )


def _result(provider, model, status):
    return {
        "provider": provider, "display_name": provider.title(), "icon": "◇",
        "model": model, "status": status,
        "error": None if status == "AVAILABLE" else "boom",
        "latency_s": 0.1, "http_status": 200, "retry_after": None,
        "error_type": None, "provider_code": None, "finish_reason": "stop",
        "capabilities": [],
    }


@pytest.mark.asyncio
async def test_only_available_models_from_any_provider_enter_the_pool():
    """A failed test result never becomes a production candidate, while a
    usable model from ANY provider (and several from one provider) is
    eligible."""
    from backend.ai.model_discovery import ModelInfo

    providers = [_provider_status("groq", "llama"), _provider_status("openai", "gpt-4o")]

    async def fake_fetch(provider_name, api_key, base_url, force_refresh=False):
        return [
            ModelInfo(id=f"{provider_name}-ok1", name="ok1", provider=provider_name),
            ModelInfo(id=f"{provider_name}-ok2", name="ok2", provider=provider_name),
            ModelInfo(id=f"{provider_name}-bad", name="bad", provider=provider_name),
        ]

    async def fake_test(provider, display, icon, model, timeout=8.0):
        return _result(provider, model, "AVAILABLE" if "-ok" in model else "PROVIDER_ERROR")

    pm, engine = _pm_engine()
    with patch("backend.ai.model_tester.discover_providers", new_callable=AsyncMock) as disc, \
         patch("backend.ai.model_tester._get_env", return_value="fake_key"), \
         patch("backend.ai.model_tester.fetch_models", side_effect=fake_fetch), \
         patch("backend.ai.model_tester.test_single_model", side_effect=fake_test), \
         patch("backend.ai.engine.engine.get_engine", return_value=engine):
        disc.return_value = providers
        from backend.ai.model_tester import test_all_models_streaming

        data = await test_all_models_streaming(owner_id=0, global_budget=0)

    # Multiple AVAILABLE models per provider, configured model excluded.
    assert pm.candidates["groq"] == ["groq-ok1", "groq-ok2"]
    assert pm.candidates["openai"] == ["openai-ok1", "openai-ok2"]
    # Failed models are never candidates anywhere.
    failed = [r["model"] for r in data["results"] if r["status"] != "AVAILABLE"]
    assert "groq-bad" in failed and "openai-bad" in failed
    for models in pm.candidates.values():
        assert not any("-bad" in m for m in models)
    # Results carry free/paid metadata honestly (no metadata -> not free).
    assert all(r["is_free"] is False for r in data["results"])


@pytest.mark.asyncio
async def test_a_provider_with_no_available_model_gets_no_candidates():
    from backend.ai.model_discovery import ModelInfo

    providers = [_provider_status("groq", "llama")]

    async def fake_fetch(provider_name, api_key, base_url, force_refresh=False):
        return [ModelInfo(id="groq-bad1", name="bad1", provider=provider_name),
                ModelInfo(id="groq-bad2", name="bad2", provider=provider_name)]

    async def fake_test(provider, display, icon, model, timeout=8.0):
        return _result(provider, model, "RATE_LIMITED")

    pm, engine = _pm_engine()
    with patch("backend.ai.model_tester.discover_providers", new_callable=AsyncMock) as disc, \
         patch("backend.ai.model_tester._get_env", return_value="fake_key"), \
         patch("backend.ai.model_tester.fetch_models", side_effect=fake_fetch), \
         patch("backend.ai.model_tester.test_single_model", side_effect=fake_test), \
         patch("backend.ai.engine.engine.get_engine", return_value=engine):
        disc.return_value = providers
        from backend.ai.model_tester import test_all_models_streaming

        await test_all_models_streaming(owner_id=0, global_budget=0)

    # Discovery found two chat-capable models; neither was proven usable, so
    # the pool is empty rather than discovery-fed.
    assert pm.candidates.get("groq") == []


def test_dummy_is_never_a_production_candidate():
    """The local echo provider is never a runtime fallback candidate, even
    when a diagnostic run reports it AVAILABLE."""
    from backend.ai.model_tester import proven_usable_candidates

    results = [
        _result("dummy", "dummy-model", "AVAILABLE"),
        _result("groq", "groq-ok", "AVAILABLE"),
    ]
    providers = [_provider_status("dummy", "dummy-model"), _provider_status("groq", "llama")]

    feed = proven_usable_candidates(results, {}, providers)

    assert "dummy" not in feed
    assert feed["groq"] == ["groq-ok"]


@pytest.mark.asyncio
async def test_free_models_are_tested_before_paid_ones_under_the_budget():
    """The diagnostic budget is applied to a FREE-FIRST target list, so a
    genuinely-free catalog is reachable (and visible) instead of being cut
    off behind an alphabetically earlier paid catalog."""
    from backend.ai.model_discovery import ModelInfo

    providers = [_provider_status("openrouter", "openrouter/auto")]

    async def fake_fetch(provider_name, api_key, base_url, force_refresh=False):
        paid = [ModelInfo(id=f"paid-{i:02d}", name=f"paid-{i:02d}", provider=provider_name)
                for i in range(20)]
        free = [ModelInfo(id=f"free-{i:02d}", name=f"free-{i:02d}", provider=provider_name,
                          is_free=True)
                for i in range(3)]
        return paid + free

    async def fake_test(provider, display, icon, model, timeout=8.0):
        return _result(provider, model, "AVAILABLE")

    pm, engine = _pm_engine()
    with patch("backend.ai.model_tester.discover_providers", new_callable=AsyncMock) as disc, \
         patch("backend.ai.model_tester._get_env", return_value="fake_key"), \
         patch("backend.ai.model_tester.fetch_models", side_effect=fake_fetch), \
         patch("backend.ai.model_tester.test_single_model", side_effect=fake_test), \
         patch("backend.ai.engine.engine.get_engine", return_value=engine):
        disc.return_value = providers
        from backend.ai.model_tester import test_all_models_streaming

        data = await test_all_models_streaming(owner_id=0, global_budget=4)

    tested = [r["model"] for r in data["results"]]
    # Free-first ordering is about ORDER only now: every model is tested (the
    # ``global_budget=4`` above is legacy and ignored).
    assert tested[:3] == ["free-00", "free-01", "free-02"]
    assert len(tested) == 23
    assert "paid-00" in tested
    assert "openrouter/auto" not in tested  # default, not a catalog member
    # The display payload follows the same free-first order.
    assert [m["id"] for m in data["models"]][:3] == ["free-00", "free-01", "free-02"]
    # Free metadata is carried into the results for the UI.
    free_models = {r["model"] for r in data["results"] if r.get("is_free")}
    assert free_models == {"free-00", "free-01", "free-02"}


def test_is_free_annotation_uses_discovery_metadata_only():
    from backend.ai.model_discovery import ModelInfo
    from backend.ai.model_tester import annotate_is_free

    results = [{"provider": "openrouter", "model": "a"},
               {"provider": "openrouter", "model": "b"},
               {"provider": "openrouter", "model": "c"}]
    complete = {"openrouter": [
        ModelInfo(id="a", name="a", provider="openrouter", is_free=True),
        ModelInfo(id="b", name="b", provider="openrouter"),
    ]}
    annotate_is_free(results, complete, [_provider_status("openrouter", "auto")])

    assert results[0]["is_free"] is True
    assert results[1]["is_free"] is False
    # No discovery metadata -> not claimed free.
    assert results[2]["is_free"] is False


# ── Complete, selection-independent target coverage ──────────────────────


def _catalog(*model_ids, provider="openai", free=()):
    from backend.ai.model_discovery import ModelInfo

    return [
        ModelInfo(id=model_id, name=model_id, provider=provider, is_free=model_id in free)
        for model_id in model_ids
    ]


def _unconfigured(name, default_model):
    from backend.ai.discovery import ProviderStatus

    return ProviderStatus(
        name=name, display_name=name.title(), env_var=f"AI_{name.upper()}_API_KEY",
        status="not_configured", has_key=False, validated=False,
        default_model=default_model, base_url=f"https://{name}.example/v1", icon="◇",
    )


async def _run_diagnostic(providers, catalogs, impl, *, streaming=False, engine=None, **kwargs):
    """Run one diagnostic with discovery/network/chat fully stubbed."""
    from backend.ai import model_tester

    async def fake_fetch(provider_name, api_key, base_url, force_refresh=False):
        return catalogs.get(provider_name, [])

    with ExitStack() as stack:
        stack.enter_context(patch(
            "backend.ai.model_tester.discover_providers",
            AsyncMock(return_value=providers)))
        stack.enter_context(patch("backend.ai.model_tester._get_env", return_value="fake_key"))
        stack.enter_context(patch("backend.ai.model_tester.fetch_models", side_effect=fake_fetch))
        mock_test = stack.enter_context(patch(
            "backend.ai.model_tester.test_single_model", side_effect=impl))
        if engine is not None:
            stack.enter_context(patch("backend.ai.engine.engine.get_engine", return_value=engine))
        if streaming:
            data = await model_tester.test_all_models_streaming(owner_id=0, **kwargs)
        else:
            data = await model_tester.test_all_models(owner_id=0, **kwargs)
    return data, mock_test


async def _available(provider, display, icon, model, timeout=8.0):
    return _result(provider, model, "AVAILABLE")


@pytest.mark.asyncio
async def test_selected_provider_and_model_never_change_the_target_set():
    """The diagnostic is independent of the owner's selection: no active
    config is read, and switching it changes neither targets nor ordering."""
    from backend.ai import model_tester

    providers = [_provider_status("openai", "gpt-4o"), _provider_status("groq", "llama")]
    catalogs = {
        "openai": _catalog("gpt-4o", "o3"),
        "groq": _catalog("llama", "mixtral", provider="groq"),
    }

    assert not hasattr(model_tester, "get_config"), (
        "the tester must not read the owner's active configuration"
    )

    async def _targets(active):
        with patch("backend.ai.config_store.get_config", AsyncMock(return_value=active)):
            data, _ = await _run_diagnostic(providers, catalogs, _available)
        return [(r["provider"], r["model"]) for r in data["results"]]

    first = await _targets({"provider": "openai", "model": "gpt-4o"})
    second = await _targets({"provider": "groq", "model": "mixtral"})

    assert first == second
    assert first == [
        ("openai", "gpt-4o"),
        ("openai", "o3"),
        ("groq", "llama"),
        ("groq", "mixtral"),
    ]


@pytest.mark.asyncio
async def test_every_chat_capable_catalog_model_of_a_configured_provider_is_tested():
    providers = [_provider_status("openai", "gpt-4o")]
    catalogs = {"openai": _catalog(
        "gpt-4o", "gpt-4o-mini", "o3",
        "text-embedding-3-small", "gpt-4o-transcribe", "gpt-4o-mini-tts",
        "gpt-image-1", "sora-2", "gpt-4o-realtime-preview",
    )}

    data, _ = await _run_diagnostic(providers, catalogs, _available)

    assert {r["model"] for r in data["results"]} == {"gpt-4o", "gpt-4o-mini", "o3"}
    assert data["summary"]["available"] == 3


@pytest.mark.asyncio
async def test_an_unconfigured_provider_tests_its_whole_fallback_catalog_offline():
    """No key: every known model of that provider is reported NOT_CONFIGURED
    with no provider client, no discovery call and no network request."""
    from backend.ai.model_discovery import fallback_models

    providers = [_unconfigured("openai", "gpt-4o")]
    expected = {model.id for model in fallback_models("openai")}
    assert len(expected) > 1

    async def _no_network(*args, **kwargs):
        raise AssertionError("no discovery request may be made without a key")

    with ExitStack() as stack:
        stack.enter_context(patch(
            "backend.ai.model_tester.discover_providers",
            AsyncMock(return_value=providers)))
        stack.enter_context(patch("backend.ai.model_tester._get_env", return_value=""))
        stack.enter_context(patch("backend.ai.model_tester.fetch_models", side_effect=_no_network))
        stack.enter_context(patch(
            "backend.ai.model_tester.ProviderFactory.create_provider",
            side_effect=AssertionError("no provider client may be created")))
        data = await test_all_models(owner_id=0)

    assert {r["model"] for r in data["results"]} == expected
    assert all(r["status"] == "NOT_CONFIGURED" for r in data["results"])
    assert all(
        r["error"] == "API key not configured in environment" for r in data["results"]
    )
    assert all(r["latency_s"] is None and r["http_status"] is None for r in data["results"])
    assert all(r["provider"] == "openai" for r in data["results"])
    assert all(r["display_name"] == "Openai" and r["icon"] == "◇" for r in data["results"])
    assert data["summary"]["not_configured"] == len(expected)
    assert data["summary"]["tested"] == 0


@pytest.mark.asyncio
async def test_a_catalog_with_more_than_sixty_models_is_fully_tested():
    providers = [_provider_status("openai", "gpt-4o")]
    ids = [f"m{i:03d}" for i in range(75)]

    data, _ = await _run_diagnostic(providers, {"openai": _catalog(*ids)}, _available)

    assert len(data["results"]) == 75
    assert [r["model"] for r in data["results"]] == ids
    assert data["summary"]["available"] == 75


@pytest.mark.asyncio
async def test_a_combined_multi_provider_catalog_over_sixty_is_fully_tested():
    providers = [
        _provider_status("openai", "gpt-4o"),
        _provider_status("groq", "llama"),
        _provider_status("mistral", "mistral"),
    ]
    catalogs = {
        "openai": _catalog(*[f"o{i:03d}" for i in range(40)]),
        "groq": _catalog(*[f"g{i:03d}" for i in range(40)], provider="groq"),
        "mistral": _catalog(*[f"m{i:03d}" for i in range(40)], provider="mistral"),
    }

    batch, _ = await _run_diagnostic(providers, catalogs, _available)

    assert len(batch["results"]) == 120
    assert len({(r["provider"], r["model"]) for r in batch["results"]}) == 120
    assert {r["provider"] for r in batch["results"]} == {"openai", "groq", "mistral"}


@pytest.mark.asyncio
async def test_a_large_early_catalog_cannot_starve_later_providers():
    providers = [_provider_status("openai", "gpt-4o"), _provider_status("groq", "llama")]
    catalogs = {
        "openai": _catalog(*[f"o{i:03d}" for i in range(100)]),
        "groq": _catalog(*[f"g{i:03d}" for i in range(4)], provider="groq"),
    }

    data, _ = await _run_diagnostic(providers, catalogs, _available)

    groq = [r["model"] for r in data["results"] if r["provider"] == "groq"]
    assert groq == [f"g{i:03d}" for i in range(4)]
    assert len(data["results"]) == 104


@pytest.mark.asyncio
async def test_no_provider_model_pair_is_tested_twice():
    providers = [_provider_status("openai", "gpt-4o")]
    catalogs = {"openai": _catalog("gpt-4o", "gpt-4o", "o3", "o3", "gpt-4o-mini")}

    data, mock_test = await _run_diagnostic(providers, catalogs, _available)

    pairs = [(r["provider"], r["model"]) for r in data["results"]]
    assert sorted(set(pairs)) == sorted(pairs)
    assert len(pairs) == 3
    assert len(mock_test.call_args_list) == 3


@pytest.mark.asyncio
async def test_a_default_model_outside_the_catalog_is_never_a_phantom_target():
    providers = [_provider_status("openai", "ghost-default")]
    catalogs = {"openai": _catalog("real-1", "real-2")}

    data, mock_test = await _run_diagnostic(providers, catalogs, _available)

    assert {r["model"] for r in data["results"]} == {"real-1", "real-2"}
    assert "ghost-default" not in [c.args[3] for c in mock_test.call_args_list]


@pytest.mark.asyncio
async def test_the_exact_catalog_model_id_is_sent_to_test_single_model():
    providers = [_provider_status("openai", "gpt-4o")]
    ids = ["gpt-4o", "o3-mini", "gpt-4.1-nano"]

    _, mock_test = await _run_diagnostic(providers, {"openai": _catalog(*ids)}, _available)

    sent = sorted(call.args[3] for call in mock_test.call_args_list)
    assert sent == sorted(ids)
    assert all(call.args[0] == "openai" for call in mock_test.call_args_list)


@pytest.mark.asyncio
async def test_batch_and_streaming_runners_share_identical_coverage():
    providers = [_provider_status("openai", "gpt-4o"), _provider_status("groq", "llama")]
    catalogs = {
        "openai": _catalog(*[f"o{i:03d}" for i in range(35)]),
        "groq": _catalog(*[f"g{i:03d}" for i in range(35)], provider="groq"),
    }

    batch, _ = await _run_diagnostic(providers, catalogs, _available)
    streamed, _ = await _run_diagnostic(providers, catalogs, _available, streaming=True)

    assert [(r["provider"], r["model"]) for r in batch["results"]] == [
        (r["provider"], r["model"]) for r in streamed["results"]
    ]
    assert batch["summary"]["available"] == streamed["summary"]["available"] == 70
    assert batch["partial"] is False and streamed["partial"] is False


@pytest.mark.asyncio
async def test_cancellation_propagates_instead_of_claiming_completeness():
    providers = [_provider_status("openai", "gpt-4o")]
    catalogs = {"openai": _catalog(*[f"m{i:03d}" for i in range(20)])}

    async def _slow(provider, display, icon, model, timeout=8.0):
        await asyncio.sleep(5)
        return _result(provider, model, "AVAILABLE")

    async def fake_fetch(provider_name, api_key, base_url, force_refresh=False):
        return catalogs[provider_name]

    with patch("backend.ai.model_tester.discover_providers", AsyncMock(return_value=providers)), \
         patch("backend.ai.model_tester._get_env", return_value="fake_key"), \
         patch("backend.ai.model_tester.fetch_models", side_effect=fake_fetch), \
         patch("backend.ai.model_tester.test_single_model", side_effect=_slow):
        task = asyncio.create_task(test_all_models(owner_id=0))
        await asyncio.sleep(0.05)
        task.cancel()
        with pytest.raises(asyncio.CancelledError):
            await task


@pytest.mark.asyncio
async def test_unattempted_targets_never_enter_the_production_feed():
    pm, engine = _pm_engine()
    providers = [_provider_status("openai", "gpt-4o")]
    catalogs = {"openai": _catalog(*[f"m{i:02d}" for i in range(24)])}

    async def _slow(provider, display, icon, model, timeout=8.0):
        await asyncio.sleep(1)
        return _result(provider, model, "AVAILABLE")

    data, _ = await _run_diagnostic(
        providers, catalogs, _slow, streaming=True, engine=engine, overall_timeout=0.1
    )

    unattempted = {r["model"] for r in data["results"] if r["status"] == "NOT_ATTEMPTED"}
    assert len(data["results"]) == 24
    assert unattempted
    assert data["partial"] is True
    fed = set(pm.candidates.get("openai", []))
    assert fed.isdisjoint(unattempted)
    assert fed <= {r["model"] for r in data["results"] if r["status"] == "AVAILABLE"}


@pytest.mark.asyncio
async def test_one_provider_failure_does_not_stop_other_providers():
    providers = [_provider_status("openai", "gpt-4o"), _provider_status("groq", "llama")]
    catalogs = {
        "openai": _catalog("a", "b"),
        "groq": _catalog("c", "d", provider="groq"),
    }

    async def _mixed(provider, display, icon, model, timeout=8.0):
        if provider == "openai":
            return _result(provider, model, "PROVIDER_ERROR")
        return _result(provider, model, "AVAILABLE")

    data, _ = await _run_diagnostic(providers, catalogs, _mixed)

    by_provider: dict[str, set[str]] = {}
    for r in data["results"]:
        by_provider.setdefault(r["provider"], set()).add(r["status"])
    assert by_provider["openai"] == {"PROVIDER_ERROR"}
    assert by_provider["groq"] == {"AVAILABLE"}
    assert len(data["results"]) == 4
