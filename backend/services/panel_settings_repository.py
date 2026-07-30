"""
PanelSettingsRepository — the ONLY module that touches the panel_settings
Supabase table directly.

Architecture:
  Supabase (panel_settings table)
    ↓
  PanelSettingsRepository  ← THIS FILE
    ↓
  PanelSettingsService (cache + validation)
    ↓
  Glass Panel

No other module may query panel_settings directly. All reads and writes
go through this repository.

Responsibilities:
  - load the global row
  - update a single field
  - update multiple fields at once
  - reload the row

No business logic, no validation, no caching — that lives in the service.
"""
import logging
from datetime import datetime, timezone
from typing import Any

from backend.db import client as db_client

logger = logging.getLogger(__name__)

_TABLE = "panel_settings"
_KEY = "global"


def _get_db():
    return db_client.get_db()


def load() -> dict[str, Any] | None:
    """Load the singleton global row from the database.

    Returns a dict of column→value, or None if the DB is unavailable
    or the row doesn't exist.
    """
    db = _get_db()
    if not db:
        return None
    try:
        result = db.table(_TABLE).select("*").eq("key", _KEY).maybe_single().execute()
        if result and result.data:
            return result.data
    except Exception as exc:
        logger.warning("panel_settings_repository: load failed: %s", exc)
    return None


def update_field(field: str, value: Any) -> bool:
    """Update a single column on the global row.

    Returns True on success, False on failure or no DB.
    """
    db = _get_db()
    if not db:
        return False
    try:
        db.table(_TABLE).update({
            field: value,
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }).eq("key", _KEY).execute()
        return True
    except Exception as exc:
        logger.warning("panel_settings_repository: update_field('%s') failed: %s", field, exc)
        return False


def update_fields(fields: dict[str, Any]) -> bool:
    """Update multiple columns at once on the global row.

    Returns True on success, False on failure or no DB.
    """
    if not fields:
        return True
    db = _get_db()
    if not db:
        return False
    payload = dict(fields)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        db.table(_TABLE).update(payload).eq("key", _KEY).execute()
        return True
    except Exception as exc:
        logger.warning("panel_settings_repository: update_fields(%s) failed: %s", list(fields.keys()), exc)
        return False


def reload() -> dict[str, Any] | None:
    """Force a fresh read of the global row. Alias for load()."""
    return load()
