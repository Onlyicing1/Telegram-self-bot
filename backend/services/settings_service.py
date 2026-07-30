"""
PanelSettingsService — the single source of truth for all Glass Panel
configuration. Panels communicate ONLY with this service.

Architecture:
  Supabase (panel_settings table)
    ↓
  PanelSettingsRepository  (raw DB access)
    ↓
  PanelSettingsService     (cache + validation)  ← THIS FILE
    ↓
  Glass Panel (reads via get_*(), writes via set_*())

## Design Principles

1. **Column-per-setting model** — each setting is a real typed column on
   the `panel_settings` table. No key-value store.
2. **Singleton row** — the table has exactly one row (key = "global").
3. **Repository pattern** — this service never touches Supabase directly.
   All DB access goes through PanelSettingsRepository.
4. **Validators** — each setting has its own validator enforcing type and
   range constraints. Validation happens BEFORE the DB write.
5. **Cache-first reads** — every getter reads from the in-memory cache.
   The database is NEVER queried on a button click.
6. **Write-through cache** — on any set(), the service validates, writes
   to the DB via the repository, then reloads the cache from the DB.
   Cache and DB are never left inconsistent.
7. **Future-proof** — adding a new setting requires only:
   - A column on the panel_settings table (already exists)
   - A default + validator + typed accessor here
   No panel code needs to change.

## Settings (12 columns on panel_settings)

| Column                  | Type    | Default | Range/Constraint        |
|-------------------------|---------|---------|-------------------------|
| auto_close_enabled      | bool    | true    | must be boolean         |
| auto_close_delay        | int     | 120     | 5..3600 (seconds)       |
| max_deep_save_mb        | int     | 50      | 1..500 (MB)             |
| delete_batch_size       | int     | 100     | 1..1000                 |
| log_retention_days      | int     | 7       | 1..365 (days)           |
| panel_timeout_seconds   | int     | 300     | 30..86400 (seconds)     |
| allow_multiple_panels   | bool    | false   | must be boolean         |
| reuse_existing_panel    | bool    | true    | must be boolean         |
| language                | str     | "en"    | non-empty string        |
| diagnostics_enabled     | bool    | true    | must be boolean         |
| debug_callbacks        | bool    | false   | must be boolean         |
| owner_only              | bool    | true    | must be boolean         |
"""
import logging
from typing import Any, Callable

from backend.services import panel_settings_repository as repo

logger = logging.getLogger(__name__)

_DEFAULTS: dict[str, Any] = {
    "auto_close_enabled": True,
    "auto_close_delay": 120,
    "max_deep_save_mb": 50,
    "delete_batch_size": 100,
    "log_retention_days": 7,
    "panel_timeout_seconds": 300,
    "allow_multiple_panels": False,
    "reuse_existing_panel": True,
    "language": "en",
    "diagnostics_enabled": True,
    "debug_callbacks": False,
    "owner_only": True,
}

_cache: dict[str, Any] = {}
_loaded: bool = False

ValidatorFn = Callable[[Any], bool]


# ── Validators ──

def _validate_bool(value: Any) -> bool:
    return isinstance(value, bool)


def _validate_int_range(min_val: int, max_val: int) -> ValidatorFn:
    def validator(value: Any) -> bool:
        if not isinstance(value, int) or isinstance(value, bool):
            return False
        return min_val <= value <= max_val
    return validator


def _validate_nonempty_str(value: Any) -> bool:
    return isinstance(value, str) and len(value.strip()) > 0


_VALIDATORS: dict[str, ValidatorFn] = {
    "auto_close_enabled": _validate_bool,
    "auto_close_delay": _validate_int_range(5, 3600),
    "max_deep_save_mb": _validate_int_range(1, 500),
    "delete_batch_size": _validate_int_range(1, 1000),
    "log_retention_days": _validate_int_range(1, 365),
    "panel_timeout_seconds": _validate_int_range(30, 86400),
    "allow_multiple_panels": _validate_bool,
    "reuse_existing_panel": _validate_bool,
    "language": _validate_nonempty_str,
    "diagnostics_enabled": _validate_bool,
    "debug_callbacks": _validate_bool,
    "owner_only": _validate_bool,
}


# ── Cache lifecycle ──

def load_all() -> None:
    """Load all panel settings from the DB into the in-memory cache.

    Called once at startup. If the DB is unavailable, falls back to
    hardcoded defaults so the bot still functions.
    """
    global _loaded, _cache
    _loaded = True

    row = repo.load()
    if row is not None:
        _cache = {}
        for key, default in _DEFAULTS.items():
            _cache[key] = row.get(key, default)
        logger.info("Panel settings loaded.")
        return

    _cache = dict(_DEFAULTS)
    logger.info("Panel settings loaded. (defaults — no DB)")


def reload_panel_settings() -> None:
    """Force a full reload of the cache from the DB without restart."""
    load_all()


def _refresh_from_db() -> None:
    """Refresh the cache from the DB after a write."""
    row = repo.load()
    if row is not None:
        for key in _DEFAULTS:
            _cache[key] = row.get(key, _DEFAULTS[key])


def _ensure_loaded() -> None:
    if not _loaded:
        load_all()



# ── Generic get/set ──

def get_setting(key: str, default: Any = None) -> Any:
    _ensure_loaded()
    return _cache.get(key, _DEFAULTS.get(key, default))


def get_all() -> dict[str, Any]:
    _ensure_loaded()
    return dict(_cache)


def set_setting(key: str, value: Any) -> bool:
    """Validate, write to DB via repository, refresh cache. Returns True on success."""
    validator = _VALIDATORS.get(key)
    if validator and not validator(value):
        logger.warning("settings_service: validation failed for '%s' = %r", key, value)
        return False

    if repo.update_field(key, value):
        _refresh_from_db()
        return True

    _cache[key] = value
    return True


# ── Typed accessors: booleans ──

def is_auto_close_enabled() -> bool:
    _ensure_loaded()
    return bool(_cache.get("auto_close_enabled", True))


def set_auto_close_enabled(enabled: bool) -> bool:
    return set_setting("auto_close_enabled", bool(enabled))


def toggle_auto_close() -> bool:
    new_val = not is_auto_close_enabled()
    set_auto_close_enabled(new_val)
    return new_val


def is_allow_multiple_panels() -> bool:
    _ensure_loaded()
    return bool(_cache.get("allow_multiple_panels", False))


def set_allow_multiple_panels(value: bool) -> bool:
    return set_setting("allow_multiple_panels", bool(value))


def is_reuse_existing_panel() -> bool:
    _ensure_loaded()
    return bool(_cache.get("reuse_existing_panel", True))


def set_reuse_existing_panel(value: bool) -> bool:
    return set_setting("reuse_existing_panel", bool(value))


def is_diagnostics_enabled() -> bool:
    _ensure_loaded()
    return bool(_cache.get("diagnostics_enabled", True))


def set_diagnostics_enabled(value: bool) -> bool:
    return set_setting("diagnostics_enabled", bool(value))


def is_debug_callbacks() -> bool:
    _ensure_loaded()
    return bool(_cache.get("debug_callbacks", False))


def set_debug_callbacks(value: bool) -> bool:
    return set_setting("debug_callbacks", bool(value))


def is_owner_only() -> bool:
    _ensure_loaded()
    return bool(_cache.get("owner_only", True))


def set_owner_only(value: bool) -> bool:
    return set_setting("owner_only", bool(value))


# ── Typed accessors: integers ──

def auto_close_delay() -> int:
    _ensure_loaded()
    return int(_cache.get("auto_close_delay", 120))


def set_auto_close_delay(seconds: int) -> bool:
    return set_setting("auto_close_delay", seconds)


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


def log_retention_days() -> int:
    _ensure_loaded()
    return int(_cache.get("log_retention_days", 7))


def set_log_retention_days(days: int) -> bool:
    return set_setting("log_retention_days", days)


def panel_timeout_seconds() -> int:
    _ensure_loaded()
    return int(_cache.get("panel_timeout_seconds", 300))


def set_panel_timeout_seconds(seconds: int) -> bool:
    return set_setting("panel_timeout_seconds", seconds)


# ── Typed accessors: strings ──

def language() -> str:
    _ensure_loaded()
    return str(_cache.get("language", "en"))


def set_language(value: str) -> bool:
    return set_setting("language", value)
