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
            assert res["status"] == "UNAVAILABLE"
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


def test_api_ai_test_models_endpoint():
    client = TestClient(app)
    response = client.post("/api/ai/test-models")
    assert response.status_code == 200
    data = response.json()
    assert "results" in data
    assert "summary" in data
    assert isinstance(data["results"], list)
