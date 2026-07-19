"""
FastAPI micro web server — keeps Render's HTTP health check satisfied
and exposes read-only API endpoints for the dashboard UI.
"""
import logging
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse, JSONResponse

from backend.db import client as db_client
from backend.health import snapshot as health_snapshot

logger = logging.getLogger(__name__)

app = FastAPI(title="LifeOS", docs_url=None, redoc_url=None)

_DIST = Path(__file__).parent.parent / "dist"


@app.get("/health")
async def health():
    return health_snapshot()


@app.get("/api/saves")
async def list_saves(limit: int = 50, offset: int = 0):
    try:
        items, total = db_client.list_saves(0, limit=limit, offset=offset)
        return {"items": items, "total": total}
    except Exception as exc:
        logger.error("api/saves error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/saves/{save_code}")
async def get_save(save_code: str):
    try:
        row = db_client.query_save(save_code)
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
        state = db_client.get_bio_state(0)
        return state or {}
    except Exception as exc:
        logger.error("api/bio error: %s", exc)
        raise HTTPException(status_code=500, detail=str(exc))


@app.get("/api/logs")
async def get_logs(limit: int = 100):
    try:
        logs = db_client.list_logs(0, limit=limit)
        return {"logs": logs}
    except Exception as exc:
        logger.error("api/logs error: %s", exc)
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
