"""
AI Model Availability Tester — lightweight diagnostic testing system.

Tests configured AI providers and models to determine real-time usability
without polluting conversation history or database state.
"""
from __future__ import annotations

import asyncio
import logging
import re
import time
from typing import Any

from backend.ai.config_store import get_config
from backend.ai.discovery import discover_providers, _get_env, _PROVIDERS
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.factory import ProviderFactory

logger = logging.getLogger(__name__)

# Sanitization regex patterns for secrets/credentials
_SECRET_PATTERNS = [
    re.compile(r"sk-[a-zA-Z0-9_\-]{8,}", re.IGNORECASE),
    re.compile(r"gsk_[a-zA-Z0-9_\-]{8,}", re.IGNORECASE),
    re.compile(r"ms-key-[a-zA-Z0-9_\-]{8,}", re.IGNORECASE),
    re.compile(r"Bearer\s+[a-zA-Z0-9_\.\-]{8,}", re.IGNORECASE),
    re.compile(r"key=[a-zA-Z0-9_\-]{8,}", re.IGNORECASE),
]


def sanitize_error_message(msg: str) -> str:
    """Sanitize error messages to ensure no API keys or credentials leak to UI."""
    if not msg:
        return ""
    sanitized = str(msg)
    for pattern in _SECRET_PATTERNS:
        sanitized = pattern.sub("***REDACTED***", sanitized)
    return sanitized


async def test_single_model(
    provider_name: str,
    display_name: str,
    icon: str,
    model_id: str,
    timeout: float = 8.0,
) -> dict[str, Any]:
    """Test a single model for a provider using isolated test request."""
    # Find provider info in _PROVIDERS
    p_info = next((p for p in _PROVIDERS if p["name"] == provider_name), None)
    if not p_info:
        return {
            "provider": provider_name,
            "display_name": display_name,
            "icon": icon,
            "model": model_id,
            "status": "NOT_CONFIGURED",
            "error": "Provider not recognized in registry",
            "latency_s": None,
            "http_status": None,
        }

    api_key = _get_env(p_info["env_vars"])
    if not api_key:
        return {
            "provider": provider_name,
            "display_name": display_name,
            "icon": icon,
            "model": model_id,
            "status": "NOT_CONFIGURED",
            "error": "API key not configured in environment",
            "latency_s": None,
            "http_status": None,
        }

    base_url = (
        _get_env(p_info["base_url_env"]) if p_info.get("base_url_env") else ""
    ) or p_info.get("default_base_url", "")

    config = ProviderConfig(
        provider_name=provider_name,
        base_url=base_url,
        api_key=api_key,
        default_model=model_id,
        temperature=0.0,
        max_tokens=5,
        timeout=int(timeout),
        retry_count=0,  # Fast fail for diagnostic test
        enabled=True,
    )

    provider_inst = None
    t0 = time.perf_counter()
    try:
        provider_inst = ProviderFactory.create_provider(provider_name, config)
        response = await asyncio.wait_for(
            provider_inst.chat(
                [{"role": "user", "content": "ping"}], model=model_id
            ),
            timeout=timeout,
        )
        latency_s = round(time.perf_counter() - t0, 2)

        if response.success:
            return {
                "provider": provider_name,
                "display_name": display_name,
                "icon": icon,
                "model": model_id,
                "status": "AVAILABLE",
                "error": None,
                "latency_s": latency_s,
                "http_status": 200,
            }

        # Failure handling
        metadata = response.metadata or {}
        http_status = metadata.get("http_status")
        err_type = metadata.get("error_type", "")
        err_text = response.text or "Request failed"

        if err_type == "timeout" or "timeout" in err_text.lower():
            return {
                "provider": provider_name,
                "display_name": display_name,
                "icon": icon,
                "model": model_id,
                "status": "TIMEOUT",
                "error": f"Request timed out after {timeout}s",
                "latency_s": latency_s,
                "http_status": http_status,
            }

        # Check status classification
        if http_status and http_status >= 400:
            status = "UNAVAILABLE"
        else:
            status = "UNAVAILABLE" if "not found" in err_text.lower() or "invalid" in err_text.lower() else "ERROR"

        clean_err = sanitize_error_message(err_text)
        return {
            "provider": provider_name,
            "display_name": display_name,
            "icon": icon,
            "model": model_id,
            "status": status,
            "error": clean_err,
            "latency_s": latency_s,
            "http_status": http_status,
        }

    except asyncio.TimeoutError:
        latency_s = round(time.perf_counter() - t0, 2)
        return {
            "provider": provider_name,
            "display_name": display_name,
            "icon": icon,
            "model": model_id,
            "status": "TIMEOUT",
            "error": f"Request timed out after {timeout}s",
            "latency_s": latency_s,
            "http_status": None,
        }
    except Exception as exc:
        latency_s = round(time.perf_counter() - t0, 2)
        clean_err = sanitize_error_message(str(exc))
        return {
            "provider": provider_name,
            "display_name": display_name,
            "icon": icon,
            "model": model_id,
            "status": "ERROR",
            "error": clean_err,
            "latency_s": latency_s,
            "http_status": None,
        }
    finally:
        if provider_inst:
            try:
                provider_inst.shutdown()
            except Exception:
                pass


test_single_model.__test__ = False  # Tell pytest not to collect this as a test function


async def test_all_models(
    owner_id: int = 0,
    per_model_timeout: float = 8.0,
    overall_timeout: float = 25.0,
) -> dict[str, Any]:
    """Discover all configured providers/models and run availability tests.

    Executes all model tests concurrently with timeout protection and resilience.
    One provider failure will never abort testing of other providers.
    """
    providers_status = await discover_providers(force_refresh=True)
    active_config = await get_config(owner_id)

    targets: list[dict[str, str]] = []
    seen = set()

    for p in providers_status:
        # Default model
        def_model = p.default_model
        key = (p.name, def_model)
        if def_model and key not in seen:
            seen.add(key)
            targets.append({
                "provider": p.name,
                "display_name": p.display_name,
                "icon": p.icon,
                "model": def_model,
            })

        # Selected active model if configured
        if active_config.get("provider") == p.name:
            cfg_model = active_config.get("model")
            key_cfg = (p.name, cfg_model)
            if cfg_model and key_cfg not in seen:
                seen.add(key_cfg)
                targets.append({
                    "provider": p.name,
                    "display_name": p.display_name,
                    "icon": p.icon,
                    "model": cfg_model,
                })

    tasks = [
        test_single_model(
            t["provider"],
            t["display_name"],
            t["icon"],
            t["model"],
            timeout=per_model_timeout,
        )
        for t in targets
    ]

    try:
        raw_results = await asyncio.wait_for(
            asyncio.gather(*tasks, return_exceptions=True),
            timeout=overall_timeout,
        )
    except asyncio.TimeoutError:
        logger.warning("test_all_models overall timeout reached (%ss)", overall_timeout)
        raw_results = []

    results: list[dict[str, Any]] = []
    summary = {
        "total": len(targets),
        "available": 0,
        "unavailable": 0,
        "error": 0,
        "timeout": 0,
        "not_configured": 0,
    }

    for idx, item in enumerate(raw_results):
        target_info = targets[idx] if idx < len(targets) else {"provider": "unknown", "display_name": "Unknown", "icon": "❓", "model": "unknown"}
        if isinstance(item, Exception):
            res = {
                "provider": target_info["provider"],
                "display_name": target_info["display_name"],
                "icon": target_info["icon"],
                "model": target_info["model"],
                "status": "ERROR",
                "error": sanitize_error_message(str(item)),
                "latency_s": None,
                "http_status": None,
            }
        else:
            res = item

        results.append(res)
        status_key = res["status"].lower()
        if status_key in summary:
            summary[status_key] += 1
        else:
            summary["error"] += 1

    return {"results": results, "summary": summary}


test_all_models.__test__ = False  # Tell pytest not to collect this as a test function
