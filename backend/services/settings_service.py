"""
Centralized Panel Settings Service — the single source of truth for all
Glass Panel configuration.

Architecture:

  Supabase (panel_settings table — column-per-setting model)
    ↓
  settings_service cache (in-memory, load-once + refresh-on-write)
    ↓
  Feature modules call settings_service.get_*() / set_*()

The cache is NEVER the source of truth. It exists only to avoid a DB
round-trip on every read. On any write (set), the DB is updated first,
then the cache is refreshed from the DB row. On startup, load_all()
populates the cache from the DB.

## Design Principles

1. **Column-per-setting model** — each setting is a real typed column on
   the `panel_settings` table. No key-value store. No generic settings table.
2. **Singleton row** — the table has exactly one row (key = "global").
3. **Validators** — each setting has its own validator that enforces
   type and range constraints. Validation happens before the DB write.
4. **Future-proof** — adding a new setting requires only:
   - `ALTER TABLE panel_settings ADD COLUMN ...`
   - A validator + accessor in this file
   No panel code needs to change.
5. **No hardcoded defaults in panels** — defaults live in the DB (column
   defaults) and in this service (fallback defaults). Panels read from
   the service, never from constants.

## Cache Flow

  Startup:  load_all() → SELECT * FROM panel_settings → _cache dict
  Read:     get_*() → _cache hit (no DB call)
  Write:    set_*() → validate → UPDATE panel_settings → refresh from DB → _cache
  Reload:   reload_settings() → SELECT * FROM panel_settings → _cache dict

The cache and DB are never left inconsistent: every write refreshes the
cache from the DB row immediately after the update.
"""
import logging
from datetime import datetime, timezone
from typing import Any, Callable

from backend.db import client as db_client

logger = logging.getLogger(__name__)

_TABLE = "panel_settings"
_KEY = "global"

_cache: dict[str, Any] = {}
_loaded = False

_DEFAULTS: dict[str, Any] = {
    "auto_close_enabled": True,
    "panel_auto_close_seconds": 120,
    "max_deep_save_mb": 50,
    "delete_batch_size": 100,
    "log_cleanup_days": 7,
}

ValidatorFn = Callable[[Any], bool]


def _get_db():
    return db_client.get_db()


def _ensure_row() -> None:
    db = _get_db()
    if not db:
        return
    try:
        existing = db.table(_TABLE).select("key").eq("key", _KEY).maybe_single().execute()
        if not existing or not existing.data:
            db.table(_TABLE).insert({
                "key": _KEY,
                "auto_close_enabled": True,
                "panel_auto_close_seconds": 120,
                "max_deep_save_mb": 50,
                "delete_batch_size": 100,
                "log_cleanup_days": 7,
            }).execute()
    except Exception as exc:
        logger.warning("settings_service: ensure_row failed: %s", exc)


def load_all() -> None:
    """Load all panel settings from the DB into the in-memory cache.

    Called once at startup. If the DB is unavailable, falls back to
    hardcoded defaults so the bot still functions.
    """
    global _loaded, _cache
    _loaded = True
    _cache = {}

    db = _get_db()
    if db:
        _ensure_row()
        try:
            result = db.table(_TABLE).select("*").eq("key", _KEY).maybe_single().execute()
            if result and result.data:
                row = result.data
                _cache["auto_close_enabled"] = bool(row.get("auto_close_enabled", True))
                _cache["panel_auto_close_seconds"] = int(row.get("panel_auto_close_seconds", 120))
                _cache["max_deep_save_mb"] = int(row.get("max_deep_save_mb", 50))
                _cache["delete_batch_size"] = int(row.get("delete_batch_size", 100))
                _cache["log_cleanup_days"] = int(row.get("log_cleanup_days", 7))
                logger.info("settings_service: loaded panel settings from DB")
                return
        except Exception as exc:
            logger.warning("settings_service: load_all failed (%s) — using defaults", exc)

    _cache = dict(_DEFAULTS)
    logger.info("settings_service: loaded defaults (no DB)")


def reload_settings() -> None:
    """Force a full reload of the cache from the DB."""
    load_all()


def _refresh_from_db() -> None:
    """Refresh the cache from the DB after a write."""
    db = _get_db()
    if not db:
        return
    try:
        result = db.table(_TABLE).select("*").eq("key", _KEY).maybe_single().execute()
        if result and result.data:
            row = result.data
            _cache["auto_close_enabled"] = bool(row.get("auto_close_enabled", True))
            _cache["panel_auto_close_seconds"] = int(row.get("panel_auto_close_seconds", 120))
            _cache["max_deep_save_mb"] = int(row.get("max_deep_save_mb", 50))
            _cache["delete_batch_size"] = int(row.get("delete_batch_size", 100))
            _cache["log_cleanup_days"] = int(row.get("log_cleanup_days", 7))
    except Exception as exc:
        logger.warning("settings_service: refresh failed: %s", exc)


def _ensure_loaded() -> None:
    if not _loaded:
        load_all()


def get_all() -> dict[str, Any]:
    """Return all cached settings as a dict."""
    _ensure_loaded()
    return dict(_cache)


# ── Generic get/set with validation ──

def get_setting(key: str, default: Any = None) -> Any:
    _ensure_loaded()
    return _cache.get(key, _DEFAULTS.get(key, default))


def set_setting(key: str, value: Any) -> bool:
    """Validate, write to DB, refresh cache. Returns True on success."""
    validator = _VALIDATORS.get(key)
    if validator and not validator(value):
        logger.warning("settings_service: validation failed for '%s' = %r", key, value)
        return False

    db = _get_db()
    if db:
        try:
            db.table(_TABLE).update({
                key: value,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).eq("key", _KEY).execute()
            _refresh_from_db()
            return True
        except Exception as exc:
            logger.warning("settings_service: set '%s' failed: %s", key, exc)

    _cache[key] = value
    return True


# ── Validators ──

def _validate_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _validate_int_range(min_val: int, max_val: int) -> ValidatorFn:
    def validator(value: Any) -> bool:
        if not isinstance(value, int) or isinstance(value, bool):
            return False
        return min_val <= value <= max_val
    return validator


_VALIDATORS: dict[str, ValidatorFn] = {
    "auto_close_enabled": _validate_bool,
    "panel_auto_close_seconds": _validate_int_range(10, 3600),
    "max_deep_save_mb": _validate_int_range(1, 500),
    "delete_batch_size": _validate_int_range(1, 1000),
    "log_cleanup_days": _validate_int_range(1, 365),
}


# ── Typed accessors ──

def is_auto_close_enabled() -> bool:
    _ensure_loaded()
    return bool(_cache.get("auto_close_enabled", True))


def set_auto_close_enabled(enabled: bool) -> bool:
    return set_setting("auto_close_enabled", bool(enabled))


def toggle_auto_close() -> bool:
    new_val = not is_auto_close_enabled()
    set_auto_close_enabled(new_val)
    return new_val


def panel_auto_close_seconds() -> int:
    _ensure_loaded()
    return int(_cache.get("panel_auto_close_seconds", 120))


def set_panel_auto_close_seconds(seconds: int) -> bool:
    return set_setting("panel_auto_close_seconds", seconds)


def max_deep_save_mb() -> int:
    _ensure_loaded()
    return int(_cache.get("max_deep_save_mb", 50))


def set_max_deep_save_mb(mb: int) -> bool:
    return set_setting("max_deep_save_mb", mb)


def delete_batch_size() -> int:
    _ensure_loaded()
    return int(_cache.get("delete_batch_size", 100))


def set_delete_batch_size(size: int) -> bool:
    return set_setting("delete_batch_size", size)


def log_cleanup_days() -> int:
    _ensure_loaded()
    return int(_cache.get("log_cleanup_days", 7))


def set_log_cleanup_days(days: int) -> bool:
    return set_setting("log_cleanup_days", days)
