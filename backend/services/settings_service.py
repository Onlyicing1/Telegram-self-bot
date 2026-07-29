"""
Centralized settings service — the single source of truth for all
runtime configuration.

Architecture:
  Database (bot_settings table)
    ↓
  settings_service cache (in-memory, refresh-only)
    ↓
  Feature modules call settings_service.get_*()

The cache is NEVER the source of truth. It exists only to avoid a DB
round-trip on every read. On any write (set), the DB is updated first,
then the cache is refreshed. On startup, load_all() populates the
cache from the DB.

All configurable features must read through this service. No module
should read from bot_settings directly or use hardcoded constants.
"""
import logging
from datetime import datetime, timezone
from typing import Any

from backend.db import client as db_client

logger = logging.getLogger(__name__)

_TABLE = "bot_settings"

_cache: dict[str, dict] = {}
_loaded = False

_DEFAULTS: dict[str, dict] = {
    "auto_close_enabled": {
        "value": True,
        "value_type": "bool",
        "description": "Auto-close inline panels after a timeout",
    },
    "panel_auto_close_seconds": {
        "value": 120,
        "value_type": "int",
        "description": "Seconds before an inline panel auto-closes",
    },
    "max_deep_save_mb": {
        "value": 50,
        "value_type": "int",
        "description": "Maximum file size (MB) for deep saves",
    },
    "delete_batch_size": {
        "value": 100,
        "value_type": "int",
        "description": "Messages per delete_messages() call",
    },
    "log_cleanup_days": {
        "value": 7,
        "value_type": "int",
        "description": "Days of logs to retain before cleanup",
    },
}


def _coerce(value: str, value_type: str) -> Any:
    if value_type == "bool":
        return value.lower() in ("true", "1", "yes", "on")
    if value_type == "int":
        try:
            return int(value)
        except (ValueError, TypeError):
            return 0
    return value


def _get_db():
    return db_client.get_db()


def load_all() -> None:
    """Load all settings from the DB into the in-memory cache.

    Called once at startup. If the DB is unavailable, falls back to
    hardcoded defaults so the bot still functions.
    """
    global _loaded, _cache
    _loaded = True
    _cache = {}

    db = _get_db()
    if db:
        try:
            result = db.table(_TABLE).select("*").execute()
            if result and result.data:
                for row in result.data:
                    key = row["key"]
                    _cache[key] = {
                        "value": _coerce(row["value"], row.get("value_type", "str")),
                        "value_type": row.get("value_type", "str"),
                        "description": _DEFAULTS.get(key, {}).get("description", ""),
                    }
                logger.info("settings_service: loaded %d settings from DB", len(_cache))
                return
        except Exception as exc:
            logger.warning("settings_service: load_all failed (%s) — using defaults", exc)

    for key, spec in _DEFAULTS.items():
        _cache[key] = {
            "value": spec["value"],
            "value_type": spec["value_type"],
            "description": spec["description"],
        }
    logger.info("settings_service: loaded %d default settings (no DB)", len(_cache))


def _refresh(key: str) -> None:
    """Refresh a single key from the DB into the cache."""
    db = _get_db()
    if not db:
        return
    try:
        result = (
            db.table(_TABLE)
            .select("*")
            .eq("key", key)
            .maybe_single()
            .execute()
        )
        if result and result.data:
            _cache[key] = {
                "value": _coerce(result.data["value"], result.data.get("value_type", "str")),
                "value_type": result.data.get("value_type", "str"),
                "description": _DEFAULTS.get(key, {}).get("description", ""),
            }
    except Exception as exc:
        logger.warning("settings_service: refresh '%s' failed: %s", key, exc)


def _ensure_loaded() -> None:
    if not _loaded:
        load_all()


def get_bool(key: str, default: bool = False) -> bool:
    _ensure_loaded()
    entry = _cache.get(key)
    if entry is None:
        return default
    val = entry["value"]
    if isinstance(val, bool):
        return val
    return bool(val)


def get_int(key: str, default: int = 0) -> int:
    _ensure_loaded()
    entry = _cache.get(key)
    if entry is None:
        return default
    val = entry["value"]
    if isinstance(val, int):
        return val
    try:
        return int(val)
    except (ValueError, TypeError):
        return default


def get_str(key: str, default: str = "") -> str:
    _ensure_loaded()
    entry = _cache.get(key)
    if entry is None:
        return default
    val = entry["value"]
    return str(val) if val is not None else default


def get_all() -> dict[str, dict]:
    """Return all cached settings as {key: {value, value_type, description}}."""
    _ensure_loaded()
    return dict(_cache)


def set_bool(key: str, value: bool) -> bool:
    return _set(key, "true" if value else "false", "bool")


def set_int(key: str, value: int) -> bool:
    return _set(key, str(value), "int")


def set_str(key: str, value: str) -> bool:
    return _set(key, value, "str")


def _set(key: str, value: str, value_type: str) -> bool:
    """Write to DB first, then refresh cache. Returns True on success."""
    db = _get_db()
    if db:
        try:
            db.table(_TABLE).upsert({
                "key": key,
                "value": value,
                "value_type": value_type,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
            _refresh(key)
            return True
        except Exception as exc:
            logger.warning("settings_service: set '%s' failed: %s", key, exc)

    _cache[key] = {
        "value": _coerce(value, value_type),
        "value_type": value_type,
        "description": _DEFAULTS.get(key, {}).get("description", ""),
    }
    return True


def toggle_bool(key: str) -> bool:
    new_val = not get_bool(key, False)
    set_bool(key, new_val)
    return new_val


# ── Typed accessors for known settings ──

def is_auto_close_enabled() -> bool:
    return get_bool("auto_close_enabled", True)


def set_auto_close_enabled(enabled: bool) -> bool:
    return set_bool("auto_close_enabled", enabled)


def toggle_auto_close() -> bool:
    return toggle_bool("auto_close_enabled")


def panel_auto_close_seconds() -> int:
    return get_int("panel_auto_close_seconds", 120)


def set_panel_auto_close_seconds(seconds: int) -> bool:
    if seconds < 10 or seconds > 3600:
        return False
    return set_int("panel_auto_close_seconds", seconds)


def max_deep_save_mb() -> int:
    return get_int("max_deep_save_mb", 50)


def set_max_deep_save_mb(mb: int) -> bool:
    if mb < 1 or mb > 500:
        return False
    return set_int("max_deep_save_mb", mb)


def delete_batch_size() -> int:
    return get_int("delete_batch_size", 100)


def set_delete_batch_size(size: int) -> bool:
    if size < 1 or size > 1000:
        return False
    return set_int("delete_batch_size", size)


def log_cleanup_days() -> int:
    return get_int("log_cleanup_days", 7)


def set_log_cleanup_days(days: int) -> bool:
    if days < 1 or days > 365:
        return False
    return set_int("log_cleanup_days", days)
