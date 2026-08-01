"""
Bio Engine — bio-specific logic.

Delegates Telegram profile updates to the shared Profile Scheduler.
The Bio Engine never calls UpdateProfileRequest directly.
It registers a render function with the scheduler and manages its
own is_active flag in the DB.
"""
import logging

from backend.db import client as db_client
from backend.profile import engine as profile_engine
from backend.profile import scheduler as profile_scheduler

logger = logging.getLogger(__name__)

_ENGINE_NAME = "bio"
_FIELD = "about"


def render_bio(template: str, mood: str, text: str, tz_str: str) -> str:
    return profile_engine.render_template(template, mood, text, tz_str)


def _get_tz(tz_str: str):
    return profile_engine.get_tz(tz_str)


async def _render_current() -> str | None:
    """Render the current bio value from DB state. Called by the scheduler."""
    from backend.helper.inline_engine import _owner_id

    owner_id = _owner_id
    state = await db_client.get_or_create_bio_state(owner_id)
    if not state.get("is_active"):
        return None
    template = state.get("template") or "🕒 {time} | 💭 {mood}"
    mood = state.get("mood") or "😊"
    text = state.get("custom_text") or ""
    tz_str = _resolve_tz()
    return render_bio(template, mood, text, tz_str)


def _resolve_tz() -> str:
    import os
    return os.getenv("TZ", "Asia/Tehran")


def start_cron(client, owner_id: int, tz_str: str) -> None:
    profile_scheduler.register_engine(_ENGINE_NAME, _FIELD, _render_current)
    if not profile_scheduler.is_running():
        profile_scheduler.start(client, tz_str)


async def stop_cron() -> None:
    profile_scheduler.unregister_engine(_ENGINE_NAME)


def is_running() -> bool:
    return _ENGINE_NAME in profile_scheduler._engines
