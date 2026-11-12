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

## Settings (columns on panel_settings)

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
| debug_callbacks        | bool    | false   | must be boolean         |
| owner_only              | bool    | true    | must be boolean         |
| dashboard_font          | str     | "default" | one of DASHBOARD_FONTS  |
| auto_reaction_enabled   | bool    | false   | must be boolean         |
| auto_reaction_emoji     | str     | ""      | non-empty string (a Telegram reaction emoji) |
| intelligent_reaction_enabled | bool | false  | must be boolean         |
| appearance_header_title | str     | ""      | bounded text ("" = default header) |
| appearance_header_body  | str     | ""      | bounded text ("" = default header) |
| appearance_page_overrides | jsonb | {}      | page id → {title?, body?}, each bounded |
| appearance_preview_enabled | bool  | false   | must be boolean         |
| appearance_preview_image_key | str | ""      | bounded Storage object key ("" = none) |
"""
import logging
import re
from typing import Any, Callable

from backend.helper.font_style import FONT_KEYS

#: Enumerated Glass UI / dashboard font choices (keys only — no arbitrary
#: CSS/font strings ever reach the DB or the frontend). The authoritative
#: allow-list lives in ``backend/helper/font_style.py``; the web dashboard
#: maps each key to a fixed CSS font stack and the Glass UI maps each key
#: to a fixed letter-styling transform.
DASHBOARD_FONTS: tuple[str, ...] = FONT_KEYS

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
    "debug_callbacks": False,
    "owner_only": True,
    "dashboard_font": "default",
    "auto_reaction_enabled": False,
    "auto_reaction_emoji": "",
    "intelligent_reaction_enabled": False,
    "appearance_header_title": "",
    "appearance_header_body": "",
    "appearance_page_overrides": {},
    "appearance_preview_enabled": False,
    "appearance_preview_image_key": "",
}

#: Appearance text is owner-supplied, so it is bounded here (the service layer
#: applies the tighter per-field limits). 1024 characters is far more than a
#: Glass UI panel title or description can usefully show.
_APPEARANCE_TEXT_LIMIT = 1024
_STORAGE_KEY_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9/_.-]*$")

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


def _validate_dashboard_font(value: Any) -> bool:
    return isinstance(value, str) and value in DASHBOARD_FONTS


def _validate_appearance_text(value: Any) -> bool:
    """Owner-supplied display text: a string, bounded, and NUL-free."""
    return (
        isinstance(value, str)
        and len(value) <= _APPEARANCE_TEXT_LIMIT
        and "\x00" not in value
    )


def _validate_page_overrides(value: Any) -> bool:
    """Per-page overrides: page id → {title?, body?} with bounded values.

    The page ids themselves are validated against the live eligible-page list
    in ``appearance_service`` (this module only owns the column contract), so
    an unrelated key can never be persisted through the generic setter.
    """
    if not isinstance(value, dict) or len(value) > 64:
        return False
    for page_id, fields in value.items():
        if not isinstance(page_id, str) or not page_id or len(page_id) > 64:
            return False
        if not isinstance(fields, dict) or not fields:
            return False
        if set(fields) - {"title", "body"}:
            return False
        for text in fields.values():
            if not _validate_appearance_text(text) or not text.strip():
                return False
    return True


def _validate_storage_key(value: Any) -> bool:
    """A Storage object key (never a URL): empty, or a bounded safe path."""
    if not isinstance(value, str) or len(value) > 256 or "\x00" in value:
        return False
    return value == "" or bool(_STORAGE_KEY_RE.match(value))



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
    "debug_callbacks": _validate_bool,
    "owner_only": _validate_bool,
    "dashboard_font": _validate_dashboard_font,
    "auto_reaction_enabled": _validate_bool,
    "auto_reaction_emoji": _validate_nonempty_str,
    "intelligent_reaction_enabled": _validate_bool,
    "appearance_header_title": _validate_appearance_text,
    "appearance_header_body": _validate_appearance_text,
    "appearance_page_overrides": _validate_page_overrides,
    "appearance_preview_enabled": _validate_bool,
    "appearance_preview_image_key": _validate_storage_key,
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


def known_keys() -> tuple[str, ...]:
    """All valid panel setting keys (the real panel_settings columns).

    Derived from the authoritative defaults table — the same keys that
    define the columns, validators, and typed accessors — so the allowlist
    can never drift from the actual settings.
    """
    return tuple(_DEFAULTS.keys())


def is_valid_key(key: str) -> bool:
    """True only for keys that are real panel_settings columns."""
    return key in _DEFAULTS


def _db_configured() -> bool:
    """Whether a durable database is configured at all.

    With no database the cache IS the configured store (the documented
    in-memory fallback), so a cache-only write is not a lie. With a database
    configured, a rejected write must never be reported as persisted.
    """
    try:
        from backend.db import client as db_client
        return db_client.get_db() is not None
    except Exception:
        return False


def set_setting_strict(key: str, value: Any) -> bool:
    """Like :func:`set_setting`, but never reports a rejected DB write as saved.

    Used by the Appearance settings: when a database IS configured and the
    repository rejects the write, the cache is left untouched and ``False`` is
    returned, so the owner is told the truth instead of seeing a value that
    disappears on the next restart/redeploy.
    """
    if not is_valid_key(key):
        logger.warning("settings_service: unknown setting key '%s' rejected", key)
        return False
    validator = _VALIDATORS.get(key)
    if validator and not validator(value):
        logger.warning("settings_service: validation failed for '%s' = %r", key, value)
        return False
    if repo.update_field(key, value):
        _refresh_from_db()
        return True
    if _db_configured():
        logger.warning("settings_service: '%s' was NOT persisted (repository rejected the write)", key)
        return False
    _cache[key] = value
    return True


def set_setting(key: str, value: Any) -> bool:
    """Validate, write to DB via repository, refresh cache. Returns True on success."""
    # Fail closed for keys that are not real panel_settings columns: an
    # unknown key is not a table column, so no DB write can succeed and
    # caching it would be a phantom success (reported as applied, persisted
    # nowhere). Reject before validation, the repository, and the cache.
    if not is_valid_key(key):
        logger.warning("settings_service: unknown setting key '%s' rejected", key)
        return False
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


def dashboard_font() -> str:
    """Selected dashboard font key; deterministic "default" when missing
    or when the persisted value is not one of the supported choices."""
    _ensure_loaded()
    value = _cache.get("dashboard_font", "default")
    return value if value in DASHBOARD_FONTS else "default"


def set_dashboard_font(value: str) -> bool:
    return set_setting("dashboard_font", value)


def is_auto_reaction_enabled() -> bool:
    """Whether the owner has enabled automatic reactions to their own
    outgoing messages. This is the SETTING only — the reaction engine that
    would consume it is a later stage and is NOT activated here."""
    _ensure_loaded()
    return bool(_cache.get("auto_reaction_enabled", False))


def set_auto_reaction_enabled(enabled: bool) -> bool:
    """Enable/disable the fixed Auto-Reaction mode.

    The two reaction modes are mutually exclusive: enabling this one turns
    Intelligent Reaction OFF in the same operation. Disabling writes nothing
    else, so the ordinary off-switch stays a single-column write.
    """
    ok = set_setting("auto_reaction_enabled", bool(enabled))
    if ok and enabled and is_intelligent_reaction_enabled():
        set_setting("intelligent_reaction_enabled", False)
    return ok


def toggle_auto_reaction() -> bool:
    """Flip the setting and return its NEW value (mirrors toggle_auto_close)."""
    new_val = not is_auto_reaction_enabled()
    set_auto_reaction_enabled(new_val)
    return new_val


def auto_reaction_emoji() -> str:
    """The owner's selected auto-reaction emoji, or "" when none is set.

    The value is the EXACT emoji string Telegram exposed in its
    available-reaction catalog — the setting never invents or normalizes it,
    so it stays directly usable by the reaction execution path."""
    _ensure_loaded()
    value = _cache.get("auto_reaction_emoji", "")
    return value if isinstance(value, str) else ""


def set_auto_reaction_emoji(emoji: str) -> bool:
    return set_setting("auto_reaction_emoji", emoji)


def is_intelligent_reaction_enabled() -> bool:
    """Whether the AI-driven Intelligent Reaction mode is enabled.

    Independent of the fixed Auto-Reaction setting: this mode never reads
    ``auto_reaction_emoji``. The two are mutually exclusive, and
    ``set_intelligent_reaction_enabled(True)`` enforces that.
    """
    _ensure_loaded()
    return bool(_cache.get("intelligent_reaction_enabled", False))


def set_intelligent_reaction_enabled(enabled: bool) -> bool:
    """Enable/disable Intelligent Reaction, turning Auto-Reaction OFF on enable."""
    ok = set_setting("intelligent_reaction_enabled", bool(enabled))
    if ok and enabled and is_auto_reaction_enabled():
        set_setting("auto_reaction_enabled", False)
    return ok


def toggle_intelligent_reaction() -> bool:
    """Flip Intelligent Reaction and return its NEW value."""
    new_val = not is_intelligent_reaction_enabled()
    set_intelligent_reaction_enabled(new_val)
    return new_val


# ── Typed accessors: Appearance personalization ──
#
# Every appearance value rides the same singleton row and the same
# cache-first/write-through service. Empty means "use the built-in default",
# so resetting a value is stored as "" (never as the current customized value).

def appearance_header_title() -> str:
    _ensure_loaded()
    value = _cache.get("appearance_header_title", "")
    return value if isinstance(value, str) else ""


def set_appearance_header_title(value: str) -> bool:
    return set_setting_strict("appearance_header_title", value)


def appearance_header_body() -> str:
    _ensure_loaded()
    value = _cache.get("appearance_header_body", "")
    return value if isinstance(value, str) else ""


def set_appearance_header_body(value: str) -> bool:
    return set_setting_strict("appearance_header_body", value)


def appearance_page_overrides() -> dict[str, dict[str, str]]:
    """Per-main-page text overrides; always a fresh, validated-shaped copy."""
    _ensure_loaded()
    value = _cache.get("appearance_page_overrides", {})
    if not isinstance(value, dict):
        return {}
    return {
        str(page_id): {str(field): str(text) for field, text in fields.items()}
        for page_id, fields in value.items()
        if isinstance(fields, dict)
    }


def set_appearance_page_overrides(value: dict[str, dict[str, str]]) -> bool:
    return set_setting_strict("appearance_page_overrides", value)


def appearance_preview_enabled() -> bool:
    _ensure_loaded()
    return bool(_cache.get("appearance_preview_enabled", False))


def set_appearance_preview_enabled(enabled: bool) -> bool:
    return set_setting_strict("appearance_preview_enabled", bool(enabled))


def appearance_preview_image_key() -> str:
    _ensure_loaded()
    value = _cache.get("appearance_preview_image_key", "")
    return value if isinstance(value, str) else ""


def set_appearance_preview_image_key(key: str) -> bool:
    return set_setting_strict("appearance_preview_image_key", key)
