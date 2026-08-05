"""
Model Discovery — queries provider APIs for available models.

When the user selects a provider, this module queries the provider's
API to download the list of available models. Models are cached in
memory with a TTL. The user can manually refresh.

Never hardcodes model lists — always queries the live API.

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

_CACHE_TTL = 600  # 10 minutes
_model_cache: dict[str, dict[str, Any]] = {}  # provider_name -> {"models": [...], "timestamp": float}
_fetch_lock = asyncio.Lock()


@dataclass(frozen=True)
class ModelInfo:
    """A single model from a provider's API."""
    id: str
    name: str
    provider: str = ""
    context_length: int = 0
    description: str = ""
    is_alias: bool = False


async def fetch_models(
    provider_name: str,
    api_key: str,
    base_url: str,
    force_refresh: bool = False,
) -> list[ModelInfo]:
    """Query a provider's API for available models.

    Returns a list of ModelInfo objects. Caches results for 10 minutes.
    """
    now = time.time()
    cached = _model_cache.get(provider_name)
    if not force_refresh and cached and (now - cached["timestamp"]) < _CACHE_TTL:
        return cached["models"]

    async with _fetch_lock:
        cached = _model_cache.get(provider_name)
        if not force_refresh and cached and (now - cached["timestamp"]) < _CACHE_TTL:
            return cached["models"]

        if provider_name == "gemini":
            models = await _fetch_gemini_models(api_key, base_url)
        else:
            models = await _fetch_openai_compat_models(provider_name, api_key, base_url)

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

                context_length = 0
                ctx = m.get("context_length") or m.get("max_context_length") or 0
                if isinstance(ctx, (int, float)):
                    context_length = int(ctx)

                display_name = model_id.split("/")[-1] if "/" in model_id else model_id

                models.append(ModelInfo(
                    id=model_id,
                    name=display_name,
                    provider=provider_name,
                    context_length=context_length,
                    description=m.get("description", ""),
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

                display_name = model_name
                supports_generation = "generateContent" in m.get("supportedGenerationMethods", [])
                if not supports_generation:
                    continue

                context_length = 0
                input_limit = m.get("inputTokenLimit", 0)
                output_limit = m.get("outputTokenLimit", 0)
                if isinstance(input_limit, (int, float)):
                    context_length = int(input_limit)

                models.append(ModelInfo(
                    id=model_name,
                    name=display_name,
                    provider="gemini",
                    context_length=context_length,
                    description=m.get("description", ""),
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
    else:
        _model_cache.pop(provider_name, None)


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
