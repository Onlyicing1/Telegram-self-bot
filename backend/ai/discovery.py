"""
Provider Discovery — automatic ENV scanning and key validation.

This module is the single source of truth for "which providers are
available right now." It scans all supported ENV variables, validates
API keys by making a lightweight API call, and reports the status of
every supported provider.

Status values:
  - "available"    — API key detected AND validated successfully
  - "detected"     — API key found in ENV but not yet validated
  - "invalid"      — API key found but validation failed (bad key)
  - "not_configured" — no API key found in ENV

The discovery layer NEVER silently falls back. If a key is invalid,
it reports "invalid" so the user can fix it.

Usage:
    from backend.ai.discovery import discover_providers, get_available_providers
    results = discover_providers()  # list of ProviderStatus
    available = get_available_providers()  # only validated ones
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

_CACHE_TTL = 300  # 5 minutes
_cache: dict[str, Any] = {"timestamp": 0.0, "results": []}
_validate_lock = asyncio.Lock()


@dataclass(frozen=True)
class ProviderStatus:
    """The discovery status of a single provider."""
    name: str
    display_name: str
    env_var: str
    status: str  # "available", "detected", "invalid", "not_configured"
    has_key: bool
    validated: bool
    default_model: str
    base_url: str
    icon: str


# ── Provider registry (the ONLY place provider metadata is defined) ──

_PROVIDERS: list[dict[str, str]] = [
    {
        "name": "openrouter",
        "display_name": "OpenRouter",
        "env_vars": ["AI_OPENROUTER_API_KEY", "OPENROUTER_API_KEY"],
        "model_env": ["AI_OPENROUTER_MODEL", "OPENROUTER_MODEL"],
        "base_url_env": ["AI_OPENROUTER_BASE_URL", "OPENROUTER_BASE_URL"],
        "default_base_url": "https://openrouter.ai/api/v1",
        "default_model": "openrouter/auto",
        "icon": "🟢",
    },
    {
        "name": "groq",
        "display_name": "Groq",
        "env_vars": ["AI_GROQ_API_KEY", "GROQ_API_KEY"],
        "model_env": ["AI_GROQ_MODEL", "GROQ_MODEL"],
        "base_url_env": [],
        "default_base_url": "https://api.groq.com/openai/v1",
        "default_model": "llama-3.3-70b-versatile",
        "icon": "⚡",
    },
    {
        "name": "gemini",
        "display_name": "Google Gemini",
        "env_vars": ["AI_GEMINI_API_KEY", "GEMINI_API_KEY"],
        "model_env": ["AI_GEMINI_MODEL", "GEMINI_MODEL"],
        "base_url_env": [],
        "default_base_url": "https://generativelanguage.googleapis.com/v1beta",
        "default_model": "gemini-2.0-flash",
        "icon": "💎",
    },
    {
        "name": "openai",
        "display_name": "OpenAI",
        "env_vars": ["AI_OPENAI_API_KEY", "OPENAI_API_KEY"],
        "model_env": ["AI_OPENAI_MODEL", "OPENAI_MODEL"],
        "base_url_env": ["AI_OPENAI_BASE_URL", "OPENAI_BASE_URL"],
        "default_base_url": "https://api.openai.com/v1",
        "default_model": "gpt-4o",
        "icon": "🧠",
    },
    {
        "name": "mistral",
        "display_name": "Mistral AI",
        "env_vars": ["AI_MISTRAL_API_KEY", "MISTRAL_API_KEY"],
        "model_env": ["AI_MISTRAL_MODEL", "MISTRAL_MODEL"],
        "base_url_env": [],
        "default_base_url": "https://api.mistral.ai/v1",
        "default_model": "mistral-large-latest",
        "icon": "🌬",
    },
    {
        "name": "cerebras",
        "display_name": "Cerebras",
        "env_vars": ["AI_CEREBRAS_API_KEY", "CEREBRAS_API_KEY"],
        "model_env": ["AI_CEREBRAS_MODEL", "CEREBRAS_MODEL"],
        "base_url_env": [],
        "default_base_url": "https://api.cerebras.ai/v1",
        "default_model": "llama-3.3-70b",
        "icon": "🔥",
    },
]


def _get_env(keys: list[str]) -> str:
    for key in keys:
        val = os.getenv(key, "").strip()
        if val:
            return val
    return ""


def _scan_provider(p: dict[str, Any]) -> ProviderStatus:
    """Scan ENV for a single provider and return its status."""
    api_key = _get_env(p["env_vars"])
    model = _get_env(p["model_env"]) or p["default_model"]
    base_url = _get_env(p["base_url_env"]) or p["default_base_url"]

    if not api_key:
        return ProviderStatus(
            name=p["name"],
            display_name=p["display_name"],
            env_var=p["env_vars"][0],
            status="not_configured",
            has_key=False,
            validated=False,
            default_model=model,
            base_url=base_url,
            icon=p["icon"],
        )

    return ProviderStatus(
        name=p["name"],
        display_name=p["display_name"],
        env_var=p["env_vars"][0],
        status="detected",
        has_key=True,
        validated=False,
        default_model=model,
        base_url=base_url,
        icon=p["icon"],
    )


async def _validate_provider(status: ProviderStatus) -> ProviderStatus:
    """Make a lightweight API call to validate the API key."""
    if not status.has_key:
        return status

    api_key = _get_env(
        next(p["env_vars"] for p in _PROVIDERS if p["name"] == status.name)
    )

    try:
        if status.name == "gemini":
            url = f"{status.base_url}/models?key={api_key}&pageSize=1"
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url)
                if resp.status_code == 200:
                    return _make_available(status)
                return _make_invalid(status, f"HTTP {resp.status_code}")
        else:
            url = f"{status.base_url}/models"
            headers = {"Authorization": f"Bearer {api_key}"}
            async with httpx.AsyncClient(timeout=10) as client:
                resp = await client.get(url, headers=headers)
                if resp.status_code == 200:
                    return _make_available(status)
                return _make_invalid(status, f"HTTP {resp.status_code}")
    except Exception as exc:
        logger.warning("Provider discovery: validation failed for '%s': %s", status.name, exc)
        return _make_invalid(status, str(exc)[:50])


def _make_available(status: ProviderStatus) -> ProviderStatus:
    return ProviderStatus(
        name=status.name,
        display_name=status.display_name,
        env_var=status.env_var,
        status="available",
        has_key=True,
        validated=True,
        default_model=status.default_model,
        base_url=status.base_url,
        icon=status.icon,
    )


def _make_invalid(status: ProviderStatus, reason: str) -> ProviderStatus:
    return ProviderStatus(
        name=status.name,
        display_name=status.display_name,
        env_var=status.env_var,
        status="invalid",
        has_key=True,
        validated=False,
        default_model=status.default_model,
        base_url=status.base_url,
        icon=status.icon,
    )


async def discover_providers(force_refresh: bool = False) -> list[ProviderStatus]:
    """Discover all providers, validate keys, and return their statuses.

    Results are cached for 5 minutes unless force_refresh is True.
    """
    now = time.time()
    if not force_refresh and _cache["results"] and (now - _cache["timestamp"]) < _CACHE_TTL:
        return _cache["results"]

    async with _validate_lock:
        scanned = [_scan_provider(p) for p in _PROVIDERS]
        to_validate = [s for s in scanned if s.has_key]
        validated = await asyncio.gather(
            *[_validate_provider(s) for s in to_validate],
            return_exceptions=True,
        )
        val_map: dict[str, ProviderStatus] = {}
        for v in validated:
            if isinstance(v, ProviderStatus):
                val_map[v.name] = v

        results: list[ProviderStatus] = []
        for s in scanned:
            if s.name in val_map:
                results.append(val_map[s.name])
            else:
                results.append(s)

        _cache["timestamp"] = now
        _cache["results"] = results
        return results


def get_available_providers(results: list[ProviderStatus] | None = None) -> list[ProviderStatus]:
    """Return only providers with status 'available'."""
    if results is None:
        results = _cache.get("results", [])
    return [r for r in results if r.status == "available"]


def get_detected_providers(results: list[ProviderStatus] | None = None) -> list[ProviderStatus]:
    """Return providers that have a key (available + detected + invalid)."""
    if results is None:
        results = _cache.get("results", [])
    return [r for r in results if r.has_key]


def get_not_configured_providers(results: list[ProviderStatus] | None = None) -> list[ProviderStatus]:
    """Return providers with no API key configured."""
    if results is None:
        results = _cache.get("results", [])
    return [r for r in results if not r.has_key]


def get_provider_info(name: str) -> dict[str, Any] | None:
    """Return the raw provider metadata dict for a provider name."""
    for p in _PROVIDERS:
        if p["name"] == name:
            return p
    return None


def get_supported_provider_names() -> list[str]:
    """Return all supported provider names."""
    return [p["name"] for p in _PROVIDERS]


def get_wizard_info() -> list[dict[str, str]]:
    """Return provider info for the configuration wizard.

    Each entry has: name, display_name, env_var, instructions.
    """
    return [
        {
            "name": p["name"],
            "display_name": p["display_name"],
            "env_var": p["env_vars"][0],
            "icon": p["icon"],
        }
        for p in _PROVIDERS
    ]
