"""
Username Engine — renders the Telegram first_name using a template.

Mirrors the Bio Engine exactly, but controls ONLY the "first_name"
field of UpdateProfileRequest. Completely independent from the Bio
Engine — both register separate updaters with the shared Profile
Scheduler.

Guarantees:
- Deduplicates: returns None when the rendered string has not changed.
- start_cron / stop_cron delegate to the shared scheduler.
- Only one scheduler task can exist at a time (idempotent start).
- Timezone resolved via zoneinfo with UTC fallback — never crashes.
"""
import asyncio
import logging
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.db import client as db_client
from backend.diagnostics import record_event
from backend.profile import scheduler as profile_scheduler
from backend.runtime.tracer import trace

logger = logging.getLogger(__name__)

_registered = False


def _get_tz(tz_str: str):
    try:
        return ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, Exception):
        logger.warning("Timezone '%s' not found — falling back to UTC.", tz_str)
        return timezone.utc


def render_username(template: str, mood: str, text: str, tz_str: str) -> str:
    tz = _get_tz(tz_str)
    now = datetime.now(tz)
    return (
        (template or "{time} | {mood}")
        .replace("{time}", now.strftime("%H:%M"))
        .replace("{mood}", mood or "😊")
        .replace("{text}", text or "")
    )


async def _username_updater(owner_id: int, tz_str: str) -> dict[str, str] | None:
    """Called by the shared profile scheduler each minute.

    Returns ``{"first_name": rendered_name}`` if it changed, or ``None``
    if it hasn't (deduplication). Also persists the new name to the DB.
    """
    state = await db_client.get_username_state(owner_id)
    if not state or not state.get("is_active"):
        return None

    tmpl = state.get("template", "{time} | {mood}")
    mood = state.get("mood", "😊")
    ctxtxt = state.get("custom_text", "")

    new_name = render_username(tmpl, mood, ctxtxt, tz_str)
    last_name = state.get("last_name")

    if new_name == (last_name or ""):
        return None

    tz = _get_tz(tz_str)
    await db_client.update_username_state(owner_id, {
        "last_name": new_name,
        "updated_at": datetime.now(tz).isoformat(),
    })
    return {"first_name": new_name}


def _ensure_registered() -> None:
    global _registered
    if _registered:
        return
    profile_scheduler.register_updater("username", _username_updater)
    _registered = True


def start_cron(client, owner_id: int, tz_str: str) -> None:
    _ensure_registered()
    profile_scheduler.start_cron(client, owner_id, tz_str)
    trace("USERNAME_CRON_START_REQUESTED")
    record_event("username", "start_cron", 0, "SUCCESS")


async def stop_cron() -> None:
    """Stop the shared profile scheduler (which serves all engines)."""
    trace("USERNAME_CRON_STOP_REQUESTED")
    await profile_scheduler.stop_cron()
    record_event("username", "stop_cron", 0, "SUCCESS")


def is_running() -> bool:
    return profile_scheduler.is_running()
