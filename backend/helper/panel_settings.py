"""
Global panel settings — persistent across restarts.

Stores the auto-close preference in the database (panel_settings table)
with an in-memory fallback. The timer engine reads this on every panel
init and every tick so the preference takes effect immediately for all
future panels without any per-panel toggle.
"""
import logging
import os
from datetime import datetime, timezone

logger = logging.getLogger(__name__)

_auto_close_enabled: bool = True
_loaded: bool = False


def _get_db():
    from backend.db.client import get_db
    return get_db()


def load() -> None:
    """Load the auto-close preference from DB (or fallback). Called once at startup."""
    global _auto_close_enabled, _loaded
    if _loaded:
        return
    _loaded = True
    db = _get_db()
    if db:
        try:
            result = (
                db.table("panel_settings")
                .select("auto_close_enabled")
                .eq("key", "global")
                .maybe_single()
                .execute()
            )
            if result and result.data:
                _auto_close_enabled = bool(result.data.get("auto_close_enabled", True))
                return
        except Exception as exc:
            logger.warning("panel_settings load failed: %s", exc)
    _auto_close_enabled = True


def is_auto_close_enabled() -> bool:
    """Return True if auto-close is globally enabled."""
    if not _loaded:
        load()
    return _auto_close_enabled


def set_auto_close_enabled(enabled: bool) -> bool:
    """Persist the auto-close preference. Returns True on success."""
    global _auto_close_enabled
    _auto_close_enabled = enabled
    db = _get_db()
    if db:
        try:
            db.table("panel_settings").upsert({
                "key": "global",
                "auto_close_enabled": enabled,
                "updated_at": datetime.now(timezone.utc).isoformat(),
            }).execute()
            return True
        except Exception as exc:
            logger.warning("panel_settings persist failed: %s", exc)
    return True


def toggle_auto_close() -> bool:
    """Toggle and persist. Returns the new value."""
    new_val = not is_auto_close_enabled()
    set_auto_close_enabled(new_val)
    return new_val
