"""
Centralized settings service — the single source of truth for all
helper panel configuration.

Architecture:
  Database (panel_settings table, typed columns)
    ↓
  settings_service cache (in-memory, refresh-only)
    ↓
  Feature modules call settings_service.get_*()

The cache is NEVER the source of truth. It exists only to avoid a DB
round-trip on every read. On any write (set), the DB is updated first,
then the cache is refreshed. On startup, load_all() populates the
cache from the DB.

All configurable features must read through this service. No module
should use hardcoded constants for values that belong to the helper
panel configuration.
"""
import logging
from datetime import datetime, timezone

from backend.db import client as db_client

logger = logging.getLogger(__name__)

_TABLE = "panel_settings"
_KEY = "global"

_cache: dict | None = None
_loaded = False

_DEFAULTS: dict = {
    "auto_close_enabled": True,
    "auto_close_delay_seconds": 120,
    "max_deep_save_mb": 50,
    "delete_batch_size": 100,
    "log_retention_days": 7,
    "panel_countdown_interval": 30,
    "input_timeout_seconds": 120,
}


def _get_db():
    return db_client.get_db()


def load_all() -> None:
    """Load settings from the DB into the in-memory cache.

    Called once at startup. If the DB is unavailable, falls back to
    hardcoded defaults so the bot still functions.
    """
    global _loaded, _cache
    _loaded = True
    _cache = None

    db = _get_db()
    if db:
        try:
            result = (
                db.table(_TABLE)
                .select("*")
                .eq("key", _KEY)
                .maybe_single()
                .execute()
            )
            if result and result.data:
                row = result.data
                _cache = {
                    "auto_close_enabled": row.get("auto_close_enabled", True),
                    "auto_close_delay_seconds": row.get("auto_close_delay_seconds", 120),
                    "max_deep_save_mb": row.get("max_deep_save_mb", 50),
                    "delete_batch_size": row.get("delete_batch_size", 100),
                    "log_retention_days": row.get("log_retention_days", 7),
                    "panel_countdown_interval": row.get("panel_countdown_interval", 30),
                    "input_timeout_seconds": row.get("input_timeout_seconds", 120),
                }
                logger.info("settings_service: loaded from panel_settings")
                return
        except Exception as exc:
            logger.warning("settings_service: load_all failed (%s) — using defaults", exc)

    _cache = dict(_DEFAULTS)
    logger.info("settings_service: using defaults (no DB)")


def _refresh() -> None:
    """Refresh the cache from the DB."""
    db = _get_db()
    if not db:
        return
    try:
        result = (
            db.table(_TABLE)
            .select("*")
            .eq("key", _KEY)
            .maybe_single()
            .execute()
        )
        if result and result.data:
            row = result.data
            _cache = {
                "auto_close_enabled": row.get("auto_close_enabled", True),
                "auto_close_delay_seconds": row.get("auto_close_delay_seconds", 120),
                "max_deep_save_mb": row.get("max_deep_save_mb", 50),
                "delete_batch_size": row.get("delete_batch_size", 100),
                "log_retention_days": row.get("log_retention_days", 7),
                "panel_countdown_interval": row.get("panel_countdown_interval", 30),
                "input_timeout_seconds": row.get("input_timeout_seconds", 120),
            }
    except Exception as exc:
        logger.warning("settings_service: refresh failed: %s", exc)


def _ensure_loaded() -> None:
    if not _loaded:
        load_all()


def _update(updates: dict) -> bool:
    """Write to DB, then refresh cache. Returns True on success."""
    db = _get_db()
    payload = {**updates, "updated_at": datetime.now(timezone.utc).isoformat()}
    if db:
        try:
            db.table(_TABLE).upsert({**payload, "key": _KEY}).execute()
            _refresh()
            return True
        except Exception as exc:
            logger.warning("settings_service: update failed: %s", exc)

    if _cache is not None:
        _cache.update(updates)
    else:
        _cache = {**_DEFAULTS, **updates}
    return True


def get_all() -> dict:
    """Return all cached settings as a dict."""
    _ensure_loaded()
    return dict(_cache) if _cache else dict(_DEFAULTS)


# ── Typed accessors ──

def is_auto_close_enabled() -> bool:
    _ensure_loaded()
    return bool(_cache.get("auto_close_enabled", True)) if _cache else True


def set_auto_close_enabled(enabled: bool) -> bool:
    return _update({"auto_close_enabled": enabled})


def toggle_auto_close() -> bool:
    new_val = not is_auto_close_enabled()
    set_auto_close_enabled(new_val)
    return new_val


def auto_close_delay_seconds() -> int:
    _ensure_loaded()
    return int(_cache.get("auto_close_delay_seconds", 120)) if _cache else 120


def set_auto_close_delay_seconds(seconds: int) -> bool:
    if seconds < 10 or seconds > 3600:
        return False
    return _update({"auto_close_delay_seconds": seconds})


def max_deep_save_mb() -> int:
    _ensure_loaded()
    return int(_cache.get("max_deep_save_mb", 50)) if _cache else 50


def set_max_deep_save_mb(mb: int) -> bool:
    if mb < 1 or mb > 500:
        return False
    return _update({"max_deep_save_mb": mb})


def delete_batch_size() -> int:
    _ensure_loaded()
    return int(_cache.get("delete_batch_size", 100)) if _cache else 100


def set_delete_batch_size(size: int) -> bool:
    if size < 1 or size > 1000:
        return False
    return _update({"delete_batch_size": size})


def log_retention_days() -> int:
    _ensure_loaded()
    return int(_cache.get("log_retention_days", 7)) if _cache else 7


def set_log_retention_days(days: int) -> bool:
    if days < 1 or days > 365:
        return False
    return _update({"log_retention_days": days})


def panel_countdown_interval() -> int:
    _ensure_loaded()
    return int(_cache.get("panel_countdown_interval", 30)) if _cache else 30


def set_panel_countdown_interval(seconds: int) -> bool:
    if seconds < 5 or seconds > 120:
        return False
    return _update({"panel_countdown_interval": seconds})


def input_timeout_seconds() -> int:
    _ensure_loaded()
    return int(_cache.get("input_timeout_seconds", 120)) if _cache else 120


def set_input_timeout_seconds(seconds: int) -> bool:
    if seconds < 10 or seconds > 600:
        return False
    return _update({"input_timeout_seconds": seconds})
