"""
FastAPI micro web server — keeps Render's HTTP health check satisfied
and exposes read-only API endpoints for the dashboard UI.

All routes are async and use the async db_client wrappers (which run Supabase
HTTP in a worker thread with a hard timeout) so a stalled Supabase request
never blocks the event loop and never freezes the health check.
"""
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.responses import FileResponse, JSONResponse
from fastapi.staticfiles import StaticFiles

from backend.db import client as db_client
from backend.supervisor import supervisor

logger = logging.getLogger(__name__)

app = FastAPI(title="LifeOS", docs_url=None, redoc_url=None)

_DIST = Path(__file__).parent.parent / "dist"


@app.get("/health")
async def health():
    return {
        "status": "ok",
        "supervisor": supervisor.status(),
    }


@app.get("/api/saves")
async def list_saves(limit: int = 50, offset: int = 0):
    try:
        items, total = await db_client.list_saves(0, limit=limit, offset=offset)
        return {"items": items, "total": total}
    except Exception as exc:
        logger.exception("api/saves error")
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
        logger.exception("api/saves/%s error", save_code)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/bio")
async def get_bio():
    try:
        state = await db_client.get_bio_state(0)
        return state or {}
    except Exception as exc:
        logger.exception("api/bio error")
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/logs")
async def get_logs(limit: int = 100):
    try:
        logs = await db_client.list_logs(0, limit=limit)
        return {"logs": logs}
    except Exception as exc:
        logger.exception("api/logs error")
        raise HTTPException(status_code=500, detail=str(exc))


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
