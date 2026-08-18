"""
FastAPI micro web server — keeps Render's HTTP health check satisfied
and exposes read-only API endpoints for the dashboard UI.
"""
import asyncio
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from backend.db import client as db_client
from backend.health import snapshot as health_snapshot
from backend.services import settings_service

logger = logging.getLogger(__name__)

app = FastAPI(title="LifeOS", docs_url=None, redoc_url=None)

_DIST = Path(__file__).parent.parent / "dist"

_owner_id: int = 0


def set_owner_id(owner_id: int) -> None:
    global _owner_id
    _owner_id = owner_id


@app.get("/health")
async def health():
    return {"status": "ok"}


@app.get("/api/status")
async def api_status():
    from backend.observability.runtime_status import runtime_status
    return runtime_status()


@app.get("/api/ai/stats")
async def api_ai_stats():
    from backend.observability.ai_stats import ai_statistics
    return ai_statistics()


@app.get("/api/db/stats")
async def api_db_stats():
    from backend.observability.db_stats import database_statistics
    return database_statistics(owner_id=_owner_id)


@app.get("/api/health/snapshot")
async def api_health_snapshot():
    from backend.observability.health_snapshot import health_snapshot
    return health_snapshot()


@app.get("/api/performance")
async def api_performance():
    from backend.observability.performance import performance_report
    return performance_report(owner_id=_owner_id)


@app.get("/api/diagnostics/events")
async def api_diagnostics_events(limit: int = 50, module: str | None = None, errors_only: bool = False):
    from backend.diagnostics import filter_events
    events = filter_events(limit=limit, module=module, errors_only=errors_only)
    return {"events": events, "count": len(events)}


@app.get("/api/maintenance")
async def api_maintenance_report():
    from backend.observability.maintenance import run_all_maintenance
    return run_all_maintenance()


@app.get("/api/saves")
async def list_saves(limit: int = 50, offset: int = 0):
    try:
        items, total = await db_client.list_saves(_owner_id, limit=limit, offset=offset)
        return {"items": items, "total": total}
    except Exception as exc:
        logger.error("api/saves error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/saves/{save_code}")
async def get_save(save_code: str):
    try:
        row = await db_client.query_save(save_code)
        if not row:
            raise HTTPException(status_code=404, detail="Not found")
        return row
    except HTTPException:
        raise
    except Exception as exc:
        logger.error("api/saves/%s error: %s", save_code, exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/bio")
async def get_bio():
    try:
        state = await db_client.get_bio_state(_owner_id)
        return state or {}
    except Exception as exc:
        logger.error("api/bio error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/settings")
async def get_settings():
    try:
        return settings_service.get_all()
    except Exception as exc:
        logger.error("api/settings error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/logs")
async def get_logs(limit: int = 100):
    try:
        logs = await db_client.list_logs(_owner_id, limit=limit)
        return {"logs": logs}
    except Exception as exc:
        logger.error("api/logs error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/ai/providers")
async def api_ai_providers():
    from backend.ai.discovery import discover_providers
    results = await discover_providers()
    return {"providers": [r.__dict__ for r in results]}


@app.get("/api/ai/models")
async def api_ai_models_all():
    """Discovered models for every configured (has-key) provider, concurrently."""
    from backend.ai.discovery import discover_providers
    from backend.ai.model_discovery import fetch_models, get_api_key_for_provider, get_base_url_for_provider

    results = await discover_providers()
    configured = [r for r in results if r.has_key]

    async def _load(p):
        api_key = get_api_key_for_provider(p.name)
        base_url = get_base_url_for_provider(p.name)
        models = await fetch_models(p.name, api_key, base_url)
        return {
            "provider": p.name,
            "display_name": p.display_name,
            "icon": p.icon,
            "status": p.status,
            "models": [m.__dict__ for m in models[:30]],
        }

    if not configured:
        return {"providers": []}

    loaded = await asyncio.gather(*[_load(p) for p in configured], return_exceptions=True)
    providers = [p for p in loaded if isinstance(p, dict)]
    return {"providers": providers}


@app.get("/api/ai/models/{provider_name}")
async def api_ai_models(provider_name: str):
    from backend.ai.model_discovery import fetch_models, get_api_key_for_provider, get_base_url_for_provider, get_last_fetch_source
    api_key = get_api_key_for_provider(provider_name)
    base_url = get_base_url_for_provider(provider_name)
    if not api_key:
        raise HTTPException(status_code=400, detail="No API key configured for this provider")
    models = await fetch_models(provider_name, api_key, base_url)
    return {
        "provider": provider_name,
        "source": get_last_fetch_source(provider_name) or "api",
        "models": [m.__dict__ for m in models],
    }


@app.get("/api/ai/config")
async def api_ai_config():
    from backend.ai.config_store import get_config
    config = await get_config(_owner_id)
    return config


@app.post("/api/ai/provider")
async def api_ai_set_provider(body: dict):
    from backend.ai.config_store import update_provider
    from backend.ai.discovery import get_provider_info

    provider = (body.get("provider") or "").strip()
    if not provider:
        raise HTTPException(status_code=400, detail="provider is required")
    info = get_provider_info(provider)
    if not info:
        raise HTTPException(status_code=400, detail=f"Unknown provider '{provider}'")
    ok = await update_provider(_owner_id, provider)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to save provider")
    # Persistence alone is not enough — the runtime provider instance must
    # switch immediately so chat uses the same (provider, model) pair.
    _apply_runtime_selection(provider, info["default_model"])
    return {"success": True, "provider": provider, "model": info["default_model"]}


@app.post("/api/ai/model")
async def api_ai_set_model(body: dict):
    from backend.ai.config_store import get_config, update_model

    model = (body.get("model") or "").strip()
    if not model:
        raise HTTPException(status_code=400, detail="model is required")
    ok = await update_model(_owner_id, model)
    if not ok:
        raise HTTPException(status_code=500, detail="Failed to save model")
    # Apply the model to the runtime provider instance (same authoritative path).
    config = await get_config(_owner_id)
    _apply_runtime_selection(config.get("provider", ""), model)
    return {"success": True, "model": model}


@app.post("/api/ai/triggers")
async def api_ai_update_triggers(body: dict):
    from backend.ai.config_store import update_triggers
    trigger_en = body.get("trigger_en", "")
    trigger_fa = body.get("trigger_fa", "")
    success, message = await update_triggers(_owner_id, trigger_en, trigger_fa)
    if not success:
        raise HTTPException(status_code=400, detail=message)
    return {"success": success, "message": message}


@app.post("/api/ai/test-models")
async def api_ai_test_models():
    from backend.ai.model_tester import test_all_models
    try:
        results = await test_all_models(owner_id=_owner_id)
        return results
    except Exception as exc:
        logger.error("api/ai/test-models error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


def _apply_runtime_selection(provider: str, model: str) -> None:
    """Push a (provider, model) selection into the runtime engine.

    Uses the same authoritative path as the glass actions and the chat
    entry points — never a parallel config store. Failures are logged,
    never raised (the persisted config_store remains the source of truth
    and the chat entry point re-applies it on the next request).
    """
    try:
        from backend.ai.engine.engine import apply_runtime_selection
        apply_runtime_selection(provider, model)
    except Exception as exc:
        logger.warning("api/ai: runtime selection apply failed: %s", exc)


def mount_static():
    if _DIST.exists():
        app.mount("/assets", StaticFiles(directory=str(_DIST / "assets")), name="assets")

        @app.get("/{full_path:path}")
        async def spa_fallback(full_path: str):
            index = _DIST / "index.html"
            if index.exists():
                return FileResponse(str(index))
            return JSONResponse({"status": "LifeOS API running"})
    else:
        @app.get("/")
        async def root():
            return {"status": "LifeOS API running — no UI build found"}


mount_static()
