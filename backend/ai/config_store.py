"""
AI Config Store — persists user AI configuration in Supabase.

Stores the user's selected provider, model, temperature, max_tokens,
and other settings in the `ai_config` table. Restored automatically
after restart.

All operations are async and use asyncio.to_thread with bounded
timeouts, matching the pattern in backend/db/client.py.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_DB_TIMEOUT = 10.0

_DEFAULTS: dict[str, Any] = {
    "provider": "",
    "model": "",
    "temperature": 1.0,
    "max_tokens": 4096,
    "system_prompt": "",
    "history_budget": 4000,
    "is_configured": False,
}


def _get_db():
    from backend.db.client import get_db
    return get_db()


async def _run_sync(fn, *args, **kwargs):
    return await asyncio.wait_for(
        asyncio.to_thread(fn, *args, **kwargs),
        timeout=_DB_TIMEOUT,
    )


def _get_config_sync(owner_id: int) -> dict[str, Any] | None:
    db = _get_db()
    if not db:
        return None
    try:
        result = db.table("ai_config").select("*").eq("owner_id", owner_id).maybe_single().execute()
        return result.data if result else None
    except Exception as exc:
        logger.warning("ai_config get failed: %s", exc)
        return None


async def get_config(owner_id: int) -> dict[str, Any]:
    """Get the AI config for an owner. Returns defaults if not found."""
    try:
        row = await _run_sync(_get_config_sync, owner_id)
        if row:
            return {k: row.get(k, v) for k, v in _DEFAULTS.items()}
    except Exception as exc:
        logger.warning("get_config failed: %s", exc)
    return dict(_DEFAULTS)


def _save_config_sync(owner_id: int, config: dict[str, Any]) -> bool:
    db = _get_db()
    if not db:
        return False
    try:
        existing = db.table("ai_config").select("id").eq("owner_id", owner_id).maybe_single().execute()
        payload = {
            "owner_id": owner_id,
            "provider": config.get("provider", ""),
            "model": config.get("model", ""),
            "temperature": config.get("temperature", 1.0),
            "max_tokens": config.get("max_tokens", 4096),
            "system_prompt": config.get("system_prompt", ""),
            "history_budget": config.get("history_budget", 4000),
            "is_configured": config.get("is_configured", False),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if existing and existing.data:
            db.table("ai_config").update(payload).eq("owner_id", owner_id).execute()
        else:
            payload["created_at"] = datetime.now(timezone.utc).isoformat()
            db.table("ai_config").insert(payload).execute()
        return True
    except Exception as exc:
        logger.warning("ai_config save failed: %s", exc)
        return False


async def save_config(owner_id: int, config: dict[str, Any]) -> bool:
    """Save AI config for an owner. Upserts the row."""
    try:
        return await _run_sync(_save_config_sync, owner_id, config)
    except Exception as exc:
        logger.warning("save_config failed: %s", exc)
        return False


async def update_provider(owner_id: int, provider: str, model: str = "") -> bool:
    """Update just the provider and model."""
    config = await get_config(owner_id)
    config["provider"] = provider
    if model:
        config["model"] = model
    config["is_configured"] = True
    return await save_config(owner_id, config)


async def update_model(owner_id: int, model: str) -> bool:
    """Update just the model."""
    config = await get_config(owner_id)
    config["model"] = model
    return await save_config(owner_id, config)


async def update_setting(owner_id: int, key: str, value: Any) -> bool:
    """Update a single setting."""
    config = await get_config(owner_id)
    config[key] = value
    return await save_config(owner_id, config)


async def record_request(owner_id: int, latency_ms: float) -> bool:
    """Record a successful request with its latency."""
    db = _get_db()
    if not db:
        return False
    try:
        def _update():
            db.table("ai_config").update({
                "last_request_at": datetime.now(timezone.utc).isoformat(),
                "last_latency_ms": latency_ms,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("owner_id", owner_id).execute()
        await _run_sync(_update)
        return True
    except Exception as exc:
        logger.warning("record_request failed: %s", exc)
        return False


async def is_configured(owner_id: int) -> bool:
    """Check if the user has completed the setup wizard."""
    config = await get_config(owner_id)
    return config.get("is_configured", False)
