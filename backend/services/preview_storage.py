"""Durable storage for owner-uploaded panel preview images.

Reuses the project's existing Supabase Storage client (the same one the
anti-edit/anti-delete archive uses) instead of inventing a second storage
stack. A *bucket* is required — Render's local filesystem is ephemeral and is
never treated as storage.

The bucket name comes from ``PANEL_PREVIEW_BUCKET`` (default
``panel-previews``). The bucket itself is created by the owner in Supabase
(see the Appearance migration/setup notes); this module never creates it and
reports an honest failure when it is missing.

Only an object KEY is persisted in ``panel_settings`` — never image bytes and
never an expiring signed URL.
"""
from __future__ import annotations

import logging
import os

from backend.db import client as db_client

logger = logging.getLogger(__name__)

DEFAULT_BUCKET = "panel-previews"


def bucket() -> str:
    return (os.getenv("PANEL_PREVIEW_BUCKET", DEFAULT_BUCKET) or "").strip()


def available() -> bool:
    """True only when a bucket name is configured AND Supabase is reachable."""
    return bool(bucket()) and db_client.get_db() is not None


def object_key(ext: str) -> str:
    """A fresh, non-guessable object key (the version identity of the image)."""
    import uuid

    safe_ext = str(ext or "").lower().lstrip(".")
    if safe_ext not in ("png", "jpg", "webp"):
        safe_ext = "bin"
    return f"previews/{uuid.uuid4().hex}.{safe_ext}"


def upload(key: str, data: bytes, content_type: str) -> bool:
    """Upload one object. Errors are logged, never raised."""
    if not key or not isinstance(data, (bytes, bytearray)):
        return False
    db = db_client.get_db()
    if db is None:
        return False
    try:
        db.storage.from_(bucket()).upload(
            key,
            bytes(data),
            {"content-type": content_type, "upsert": "false"},
        )
        return True
    except Exception as exc:  # noqa: BLE001 — an upload failure is reported, not fatal
        logger.warning("[APPEARANCE] preview upload failed (%s): %s", key, type(exc).__name__)
        return False


def remove(key: str) -> bool:
    """Delete one object. A failed delete leaves the reference untouched."""
    if not isinstance(key, str) or not key:
        return False
    db = db_client.get_db()
    if db is None:
        return False
    try:
        db.storage.from_(bucket()).remove([key])
        return True
    except Exception as exc:  # noqa: BLE001
        logger.warning("[APPEARANCE] preview delete failed (%s): %s", key, type(exc).__name__)
        return False


def download(key: str) -> bytes | None:
    """Read one object's bytes (used only by the public image endpoint)."""
    if not isinstance(key, str) or not key:
        return None
    db = db_client.get_db()
    if db is None:
        return None
    try:
        payload = db.storage.from_(bucket()).download(key)
        return payload if isinstance(payload, (bytes, bytearray)) else None
    except Exception as exc:  # noqa: BLE001
        logger.warning("[APPEARANCE] preview read failed (%s): %s", key, type(exc).__name__)
        return None
