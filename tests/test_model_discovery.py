"""
Unit tests for model discovery: chat-capable filtering, centralized
fallback catalog, and TTL caching.
"""
from __future__ import annotations

import pytest
from unittest.mock import patch, AsyncMock, MagicMock

from backend.ai.model_discovery import (
    clear_cache,
    fetch_models,
    get_last_fetch_source,
    is_chat_capable,
)


def test_is_chat_capable():
    assert is_chat_capable("gpt-4o") is True
    assert is_chat_capable("gemini-2.5-flash") is True
    assert is_chat_capable("llama-3.3-70b-versatile") is True
    assert is_chat_capable("text-embedding-3-small") is False
    assert is_chat_capable("whisper-1") is False
    assert is_chat_capable("tts-1") is False
    assert is_chat_capable("dall-e-3") is False
    assert is_chat_capable("moderation-latest") is False
    assert is_chat_capable("") is False


def _mock_response(status_code: int, payload: dict):
    resp = MagicMock()
    resp.status_code = status_code
    resp.json.return_value = payload
    return resp


def _mock_async_client(resp):
    mock_client = AsyncMock()
    mock_client.get.return_value = resp
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False
    return mock_client


@pytest.mark.asyncio
async def test_fetch_openai_compat_filters_non_chat_and_caches():
    clear_cache()
    payload = {
        "data": [
            {"id": "gpt-4o", "context_length": 128000},
            {"id": "text-embedding-3-small"},
            {"id": "whisper-1"},
            {"id": "gpt-4o-mini"},
        ]
    }
    mock_client = _mock_async_client(_mock_response(200, payload))

    with patch("backend.ai.model_discovery.httpx.AsyncClient", return_value=mock_client):
        models = await fetch_models("openai", "fake_key", "https://api.openai.com/v1")
        # Second call within TTL is served from cache — no extra network hit.
        models2 = await fetch_models("openai", "fake_key", "https://api.openai.com/v1")

    ids = [m.id for m in models]
    assert "gpt-4o" in ids
    assert "gpt-4o-mini" in ids
    assert "text-embedding-3-small" not in ids
    assert "whisper-1" not in ids
    assert get_last_fetch_source("openai") == "api"
    assert len(models2) == len(models)
    assert mock_client.get.call_count == 1  # cache served the second call


@pytest.mark.asyncio
async def test_openai_compat_excludes_metadata_non_text_models():
    clear_cache()
    payload = {
        "data": [
            {"id": "gpt-4o", "context_length": 128000},
            {"id": "gpt-image-1", "capabilities": {"text": False, "image": True}},
            {"id": "video-model", "capabilities": {"text": False, "video": True}},
            {"id": "audio-model", "capabilities": {"text": False, "audio": True}},
            {"id": "chat-with-caps", "capabilities": {"text": True, "reasoning": True}},
        ]
    }
    mock_client = _mock_async_client(_mock_response(200, payload))

    with patch("backend.ai.model_discovery.httpx.AsyncClient", return_value=mock_client):
        models = await fetch_models("openrouter", "fake_key", "https://openrouter.ai/api/v1")

    ids = [m.id for m in models]
    assert "gpt-4o" in ids
    assert "chat-with-caps" in ids
    assert "gpt-image-1" not in ids
    assert "video-model" not in ids
    assert "audio-model" not in ids

    # Provider metadata survives into capabilities for chat-capable models.
    chat = next(m for m in models if m.id == "chat-with-caps")
    assert "reasoning" in chat.capabilities


@pytest.mark.asyncio
async def test_force_refresh_bypasses_cache():
    clear_cache()
    payload = {"data": [{"id": "gpt-4o"}]}
    mock_client = _mock_async_client(_mock_response(200, payload))

    with patch("backend.ai.model_discovery.httpx.AsyncClient", return_value=mock_client):
        await fetch_models("openai", "k", "https://api.openai.com/v1")
        await fetch_models("openai", "k", "https://api.openai.com/v1", force_refresh=True)
    assert mock_client.get.call_count == 2


def test_gemini_fallback_catalog_has_no_stale_20_models():
    from backend.ai.model_discovery import _FALLBACK_CATALOG
    gemini = _FALLBACK_CATALOG.get("gemini", [])
    assert "gemini-2.0-flash" not in gemini
    assert "gemini-2.0-flash-lite" not in gemini
    assert "gemini-2.5-flash" in gemini


@pytest.mark.asyncio
async def test_fallback_catalog_when_api_fails():
    clear_cache()
    mock_client = _mock_async_client(_mock_response(500, {}))

    with patch("backend.ai.model_discovery.httpx.AsyncClient", return_value=mock_client):
        models = await fetch_models("openai", "fake_key", "https://api.openai.com/v1")
    assert len(models) > 0
    assert any(m.id == "gpt-4o" for m in models)
    assert get_last_fetch_source("openai") == "fallback"


@pytest.mark.asyncio
async def test_fallback_catalog_when_api_returns_empty_data():
    clear_cache()
    mock_client = _mock_async_client(_mock_response(200, {"data": []}))

    with patch("backend.ai.model_discovery.httpx.AsyncClient", return_value=mock_client):
        models = await fetch_models("groq", "fake_key", "https://api.groq.com/openai/v1")
    assert any(m.id == "llama-3.3-70b-versatile" for m in models)
    assert get_last_fetch_source("groq") == "fallback"


@pytest.mark.asyncio
async def test_gemini_fetch_filters_non_generate_models():
    clear_cache()
    payload = {
        "models": [
            {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent", "embedContent"], "inputTokenLimit": 1000000},
            {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
            {"name": "models/gemini-2.0-flash", "supportedGenerationMethods": ["generateContent"]},
        ]
    }
    mock_client = _mock_async_client(_mock_response(200, payload))

    with patch("backend.ai.model_discovery.httpx.AsyncClient", return_value=mock_client):
        models = await fetch_models("gemini", "fake_key", "https://generativelanguage.googleapis.com/v1beta")
    ids = [m.id for m in models]
    assert "gemini-2.5-flash" in ids
    assert "gemini-2.0-flash" in ids
    assert "text-embedding-004" not in ids


@pytest.mark.asyncio
async def test_provider_without_listing_support_falls_back():
    clear_cache()
    # A provider whose API returns 403 (e.g. listing unsupported) still
    # yields the centralized fallback catalog.
    mock_client = _mock_async_client(_mock_response(403, {}))

    with patch("backend.ai.model_discovery.httpx.AsyncClient", return_value=mock_client):
        models = await fetch_models("mistral", "fake_key", "https://api.mistral.ai/v1")
    assert any(m.id == "mistral-large-latest" for m in models)


# ── OpenRouter: free models must survive discovery, metadata decides free ──


@pytest.mark.asyncio
async def test_openrouter_free_models_are_retained_and_free_is_metadata_driven():
    """An OpenRouter-shaped /models response keeps every chat-capable model
    (including ``:free`` ids) and marks a model free ONLY when authoritative
    pricing reports $0 prompt AND $0 completion."""
    clear_cache()
    payload = {
        "data": [
            {"id": "google/gemini-2.0-flash-exp:free", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "meta-llama/llama-3.3-70b-instruct:free", "pricing": {"prompt": "0", "completion": "0"}},
            {"id": "openai/gpt-4o", "pricing": {"prompt": "0.0000025", "completion": "0.00001"}},
            {"id": "vendor/paid-model:free", "pricing": {"prompt": "0.1", "completion": "0.2"}},
            {"id": "vendor/half-free", "pricing": {"prompt": "0", "completion": "0.5"}},
            {"id": "vendor/zero-numeric", "pricing": {"prompt": "0", "completion": 0.0}},
            {"id": "vendor/text-embedding-3", "pricing": {"prompt": "0", "completion": "0"}},
        ]
    }
    mock_client = _mock_async_client(_mock_response(200, payload))

    with patch("backend.ai.model_discovery.httpx.AsyncClient", return_value=mock_client):
        models = await fetch_models(
            "openrouter", "fake_key", "https://openrouter.ai/api/v1"
        )

    ids = [m.id for m in models]
    # Nothing chat-capable was dropped on the way in...
    assert "google/gemini-2.0-flash-exp:free" in ids
    assert "meta-llama/llama-3.3-70b-instruct:free" in ids
    assert "vendor/half-free" in ids
    # ...and the non-chat embedding family is still filtered out.
    assert "vendor/text-embedding-3" not in ids

    free = {m.id for m in models if m.is_free}
    assert free == {
        "google/gemini-2.0-flash-exp:free",
        "meta-llama/llama-3.3-70b-instruct:free",
        "vendor/zero-numeric",
    }
    # A name containing "free" is never proof of free status.
    assert "vendor/paid-model:free" not in free
    # Pricing that is free on only ONE side is not free.
    assert "vendor/half-free" not in free
    assert "openai/gpt-4o" not in free


def test_free_first_order_is_deterministic_lossless_and_metadata_only():
    from backend.ai.model_discovery import ModelInfo, order_models_for_selector

    models = [
        ModelInfo(id="z/paid", name="paid", provider="p"),
        ModelInfo(id="a/free", name="free-a", provider="p", is_free=True),
        ModelInfo(id="b/mid", name="mid", provider="p"),
        ModelInfo(id="c/free", name="free-c", provider="p", is_free=True),
    ]
    ordered = order_models_for_selector(models)

    assert [m.id for m in ordered] == ["a/free", "c/free", "b/mid", "z/paid"]
    assert len(ordered) == len(models)
    assert sorted(m.id for m in ordered) == sorted(m.id for m in models)
    # The input list is never mutated.
    assert [m.id for m in models] == ["z/paid", "a/free", "b/mid", "c/free"]


# ── Gemini: the /models contract paginates via nextPageToken ──


@pytest.mark.asyncio
async def test_gemini_discovery_follows_every_page():
    """Only reading the first page hides every model beyond it; the full
    ``nextPageToken`` chain is followed (and de-duplicated)."""
    clear_cache()
    pages = [
        {
            "models": [
                {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
            ],
            "nextPageToken": "tok-2",
        },
        {
            "models": [
                {"name": "models/gemini-2.5-pro", "supportedGenerationMethods": ["generateContent"]},
                {"name": "models/text-embedding-004", "supportedGenerationMethods": ["embedContent"]},
            ],
            "nextPageToken": "tok-3",
        },
        {
            "models": [
                {"name": "models/gemini-2.5-flash", "supportedGenerationMethods": ["generateContent"]},
            ],
        },
    ]

    mock_client = AsyncMock()
    mock_client.get.side_effect = [_mock_response(200, p) for p in pages]
    mock_client.__aenter__.return_value = mock_client
    mock_client.__aexit__.return_value = False

    with patch("backend.ai.model_discovery.httpx.AsyncClient", return_value=mock_client):
        models = await fetch_models(
            "gemini", "fake_key", "https://generativelanguage.googleapis.com/v1beta"
        )

    assert [m.id for m in models] == ["gemini-2.5-flash", "gemini-2.5-pro"]
    assert mock_client.get.call_count == 3
    urls = [call.args[0] for call in mock_client.get.call_args_list]
    assert "pageToken=tok-2" in urls[1]
    assert "pageToken=tok-3" in urls[2]


@pytest.mark.asyncio
async def test_single_page_openai_compatible_provider_is_not_paged():
    """A provider that returns its whole catalog in one response (the
    OpenRouter/OpenAI/Groq contract) is fetched exactly once."""
    clear_cache()
    payload = {"data": [{"id": "vendor/a"}, {"id": "vendor/b"}]}
    mock_client = _mock_async_client(_mock_response(200, payload))

    with patch("backend.ai.model_discovery.httpx.AsyncClient", return_value=mock_client):
        models = await fetch_models("openrouter", "fake_key", "https://openrouter.ai/api/v1")

    assert [m.id for m in models] == ["vendor/a", "vendor/b"]
    assert mock_client.get.call_count == 1
