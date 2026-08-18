"""
Model Discovery — queries provider APIs for available models.

When the user selects a provider, this module queries the provider's
API to download the list of available models. Models are cached in
memory with a TTL (configurable via ``MODEL_DISCOVERY_CACHE_TTL``,
default 600s). The user can manually refresh.

Behavior:
  - Chat-capable filtering: models that are obviously not usable for
    normal text chat (embeddings, whisper, tts, moderation, dall-e,
    image/audio generation, rerank, realtime, guard, search) are
    excluded from discovery results.
  - Fallback catalog: if a provider's model-list API fails or returns
    nothing, a centralized per-provider fallback catalog
    (``_FALLBACK_CATALOG`` — the ONE place to update offline model
    lists) is used so the UI still has models to show.
  - Per-provider fetch locks: a slow provider never blocks another.

Usage:
    from backend.ai.model_discovery import fetch_models, get_cached_models
    models = await fetch_models("openrouter", api_key, base_url)
    cached = get_cached_models("openrouter")
"""
from __future__ import annotations

import asyncio
import logging
import os
import time
from dataclasses import dataclass, field
from typing import Any

import httpx

logger = logging.getLogger(__name__)

_CACHE_TTL = int(os.getenv("MODEL_DISCOVERY_CACHE_TTL", "600"))  # seconds
_model_cache: dict[str, dict[str, Any]] = {}  # provider_name -> {"models": [...], "timestamp": float}
_locks: dict[str, asyncio.Lock] = {}
_last_source: dict[str, str] = {}  # provider_name -> "api" | "fallback"

# Substrings that identify models NOT usable for normal text chat.
_EXCLUDE_SUBSTRINGS = (
    "embedding",
    "whisper",
    "tts-",
    "tts_",
    "moderation",
    "dall-e",
    "dall_e",
    "image",
    "audio",
    "rerank",
    "realtime",
    "guard",
    "search",
    "stt",
)


def is_chat_capable(model_id: str) -> bool:
    """True when a model id looks usable for normal text chat.

    Conservative blocklist — chat-capable multimodal models (e.g. gpt-4o,
    gemini-2.5-flash) are kept; clearly non-chat model families
    (embeddings, whisper, tts, moderation, dall-e, image/audio gen,
    rerank, realtime, guardrails, search) are excluded.
    """
    if not model_id:
        return False
    low = model_id.lower()
    return not any(s in low for s in _EXCLUDE_SUBSTRINGS)


# ── Centralized fallback catalog (offline model lists) ──
# Used ONLY when a provider's model-list API is unreachable or empty.
# This is the single place to update curated model names.
_FALLBACK_CATALOG: dict[str, list[str]] = {
    "gemini": [
        "gemini-2.5-flash",
        "gemini-2.5-pro",
        "gemini-2.0-flash",
        "gemini-2.0-flash-lite",
    ],
    "openai": [
        "gpt-4o",
        "gpt-4o-mini",
        "gpt-4.1",
        "gpt-4.1-mini",
        "gpt-3.5-turbo",
    ],
    "openrouter": [
        "openrouter/auto",
        "openai/gpt-4o",
        "anthropic/claude-3.5-sonnet",
        "google/gemini-2.0-flash-001",
    ],
    "groq": [
        "llama-3.3-70b-versatile",
        "llama-3.1-8b-instant",
        "mixtral-8x7b-32768",
    ],
    "cerebras": [
        "llama-3.3-70b",
        "llama-3.1-8b",
    ],
    "mistral": [
        "mistral-large-latest",
        "mistral-small-latest",
        "open-mistral-nemo",
    ],
}


def _get_lock(provider_name: str) -> asyncio.Lock:
    return _locks.setdefault(provider_name, asyncio.Lock())


@dataclass(frozen=True)
class ModelInfo:
    """A single model from a provider's API."""
    id: str
    name: str
    provider: str = ""
    context_length: int = 0
    description: str = ""
    is_alias: bool = False
    capabilities: list[str] = field(default_factory=list)


def _build_fallback_models(provider_name: str) -> list[ModelInfo]:
    """Build ModelInfo list from the centralized fallback catalog."""
    ids = _FALLBACK_CATALOG.get(provider_name, [])
    return [
        ModelInfo(
            id=mid,
            name=mid.split("/")[-1] if "/" in mid else mid,
            provider=provider_name,
        )
        for mid in ids
    ]


def get_last_fetch_source(provider_name: str) -> str:
    """Return 'api' or 'fallback' for the most recent fetch of a provider."""
    return _last_source.get(provider_name, "")


async def fetch_models(
    provider_name: str,
    api_key: str,
    base_url: str,
    force_refresh: bool = False,
) -> list[ModelInfo]:
    """Query a provider's API for available models.

    Returns a list of chat-capable ModelInfo objects. Caches results
    for the configured TTL. Falls back to the centralized catalog when
    the API is unreachable or returns no chat-capable models.
    """
    now = time.time()
    cached = _model_cache.get(provider_name)
    if not force_refresh and cached and (now - cached["timestamp"]) < _CACHE_TTL:
        return cached["models"]

    async with _get_lock(provider_name):
        cached = _model_cache.get(provider_name)
        if not force_refresh and cached and (now - cached["timestamp"]) < _CACHE_TTL:
            return cached["models"]

        if provider_name == "gemini":
            models = await _fetch_gemini_models(api_key, base_url)
        else:
            models = await _fetch_openai_compat_models(provider_name, api_key, base_url)

        if not models:
            fallback = _build_fallback_models(provider_name)
            if fallback:
                logger.info("Model discovery: %s API empty/unreachable — using fallback catalog (%d models)",
                            provider_name, len(fallback))
                _last_source[provider_name] = "fallback"
            _model_cache[provider_name] = {"models": fallback, "timestamp": time.time()}
            return fallback

        _last_source[provider_name] = "api"
        _model_cache[provider_name] = {"models": models, "timestamp": time.time()}
        return models


async def _fetch_openai_compat_models(
    provider_name: str,
    api_key: str,
    base_url: str,
) -> list[ModelInfo]:
    """Fetch models from an OpenAI-compatible /models endpoint."""
    url = f"{base_url}/models"
    headers = {"Authorization": f"Bearer {api_key}"}

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url, headers=headers)
            if resp.status_code != 200:
                logger.warning("Model discovery: %s returned %d", provider_name, resp.status_code)
                return []

            data = resp.json()
            raw_models = data.get("data", [])

            models: list[ModelInfo] = []
            for m in raw_models:
                model_id = m.get("id", "")
                if not model_id:
                    continue
                if not is_chat_capable(model_id):
                    continue

                context_length = 0
                ctx = m.get("context_length") or m.get("max_context_length") or 0
                if isinstance(ctx, (int, float)):
                    context_length = int(ctx)

                display_name = model_id.split("/")[-1] if "/" in model_id else model_id

                caps: list[str] = []
                raw_caps = m.get("capabilities") if isinstance(m.get("capabilities"), dict) else {}
                for cap_name, enabled in raw_caps.items():
                    if enabled is True:
                        caps.append(str(cap_name))

                models.append(ModelInfo(
                    id=model_id,
                    name=display_name,
                    provider=provider_name,
                    context_length=context_length,
                    description=m.get("description", ""),
                    is_alias=bool(m.get("is_alias", False)),
                    capabilities=caps,
                ))

            models.sort(key=lambda m: m.name.lower())
            return models

    except Exception as exc:
        logger.warning("Model discovery: %s fetch failed: %s", provider_name, exc)
        return []


async def _fetch_gemini_models(api_key: str, base_url: str) -> list[ModelInfo]:
    """Fetch models from the Gemini API."""
    url = f"{base_url}/models?key={api_key}&pageSize=100"

    try:
        async with httpx.AsyncClient(timeout=15) as client:
            resp = await client.get(url)
            if resp.status_code != 200:
                logger.warning("Model discovery: gemini returned %d", resp.status_code)
                return []

            data = resp.json()
            raw_models = data.get("models", [])

            models: list[ModelInfo] = []
            for m in raw_models:
                model_name = m.get("name", "").replace("models/", "")
                if not model_name:
                    continue
                if not is_chat_capable(model_name):
                    continue

                supports_generation = "generateContent" in m.get("supportedGenerationMethods", [])
                if not supports_generation:
                    continue

                context_length = 0
                input_limit = m.get("inputTokenLimit", 0)
                if isinstance(input_limit, (int, float)):
                    context_length = int(input_limit)

                models.append(ModelInfo(
                    id=model_name,
                    name=model_name,
                    provider="gemini",
                    context_length=context_length,
                    description=m.get("description", ""),
                    capabilities=list(m.get("supportedGenerationMethods", [])),
                ))

            models.sort(key=lambda m: m.name.lower())
            return models

    except Exception as exc:
        logger.warning("Model discovery: gemini fetch failed: %s", exc)
        return []


def get_cached_models(provider_name: str) -> list[ModelInfo]:
    """Return cached models for a provider, or empty list."""
    cached = _model_cache.get(provider_name)
    if cached:
        return cached["models"]
    return []


def clear_cache(provider_name: str | None = None) -> None:
    """Clear model cache for a provider, or all if None."""
    if provider_name is None:
        _model_cache.clear()
        _last_source.clear()
    else:
        _model_cache.pop(provider_name, None)
        _last_source.pop(provider_name, None)


def get_api_key_for_provider(provider_name: str) -> str:
    """Get the API key for a provider from ENV."""
    from backend.ai.discovery import _get_env, _PROVIDERS
    for p in _PROVIDERS:
        if p["name"] == provider_name:
            return _get_env(p["env_vars"])
    return ""


def get_base_url_for_provider(provider_name: str) -> str:
    """Get the base URL for a provider from ENV or defaults."""
    from backend.ai.discovery import _get_env, _PROVIDERS
    for p in _PROVIDERS:
        if p["name"] == provider_name:
            base_url = _get_env(p["base_url_env"])
            return base_url or p["default_base_url"]
    return ""
