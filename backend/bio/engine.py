"""
Bio Engine — renders the Telegram profile bio ("about") using a template.

Architecture change: the bio engine no longer runs its own cron loop.
Instead it registers an updater with the shared Profile Scheduler
(backend.profile.scheduler). The scheduler fires once per minute at
HH:MM:00 and calls all registered updaters in a single pass, sending
ONE UpdateProfileRequest to Telegram.

The bio engine controls ONLY the "about" field. It is completely
independent from the username engine.

Guarantees:
- Deduplicates: returns None when the rendered string has not changed.
- FloodWaitError handling is done by the shared scheduler.
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

_STOP_TIMEOUT = 10.0
_registered = False


def _get_tz(tz_str: str):
    try:
        return ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, Exception):
        logger.warning("Timezone '%s' not found — falling back to UTC.", tz_str)
        return timezone.utc


def render_bio(template: str, mood: str, text: str, tz_str: str) -> str:
    tz = _get_tz(tz_str)
    now = datetime.now(tz)
    return (
        (template or "🕒 {time} | 💭 {mood}")
        .replace("{time}", now.strftime("%H:%M"))
        .replace("{mood}", mood or "😊")
        .replace("{text}", text or "")
    )


async def _bio_updater(owner_id: int, tz_str: str) -> dict[str, str] | None:
    """Called by the shared profile scheduler each minute.

    Returns ``{"about": rendered_bio}`` if the bio changed, or ``None``
    if it hasn't (deduplication). Also persists the new bio to the DB.
    """
    state = await db_client.get_bio_state(owner_id)
    if not state or not state.get("is_active"):
        return None

    tmpl = state.get("template", "🕒 {time} | 💭 {mood}")
    mood = state.get("mood", "😊")
    ctxtxt = state.get("custom_text", "")

    new_bio = render_bio(tmpl, mood, ctxtxt, tz_str)
    last_bio = state.get("last_bio")

    if new_bio == (last_bio or ""):
        return None

    tz = _get_tz(tz_str)
    await db_client.update_bio_state(owner_id, {
        "last_bio": new_bio,
        "updated_at": datetime.now(tz).isoformat(),
    })
    return {"about": new_bio}


def _ensure_registered() -> None:
    global _registered
    if _registered:
        return
    profile_scheduler.register_updater("bio", _bio_updater)
    _registered = True


def start_cron(client, owner_id: int, tz_str: str) -> None:
    _ensure_registered()
    profile_scheduler.start_cron(client, owner_id, tz_str)
    trace("BIO_CRON_START_REQUESTED")
    record_event("bio", "start_cron", 0, "SUCCESS")


def update_client(client) -> None:
    """Swap the client after a rebuild without restarting the bio engine."""
    profile_scheduler.update_client(client)


async def stop_cron() -> None:
    """Stop the shared profile scheduler (which serves all engines)."""
    trace("BIO_CRON_STOP_REQUESTED")
    await profile_scheduler.stop_cron()
    record_event("bio", "stop_cron", 0, "SUCCESS")


def is_running() -> bool:
    return profile_scheduler.is_running()
