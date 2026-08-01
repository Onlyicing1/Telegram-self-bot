"""
Username Engine — first-name-specific logic.

Completely independent from the Bio Engine.
Controls ONLY the Telegram first_name field via UpdateProfileRequest(first_name=...).
Delegates Telegram updates to the shared Profile Scheduler.
Same architecture, same variable system, same scheduling guarantees.
"""
import logging
import os

from backend.db import client as db_client
from backend.profile import engine as profile_engine
from backend.profile import scheduler as profile_scheduler

logger = logging.getLogger(__name__)

_ENGINE_NAME = "username"
_FIELD = "first_name"


def render_username(template: str, mood: str, text: str, tz_str: str) -> str:
    return profile_engine.render_template(template, mood, text, tz_str)


def _get_tz(tz_str: str):
    return profile_engine.get_tz(tz_str)


def _resolve_tz() -> str:
    return os.getenv("TZ", "Asia/Tehran")


async def _render_current() -> str | None:
    """Render the current username value from DB state. Called by the scheduler."""
    from backend.helper.inline_engine import _owner_id

    owner_id = _owner_id
    state = await db_client.get_or_create_username_state(owner_id)
    if not state.get("is_active"):
        return None
    template = state.get("template") or "🕒 {time}"
    mood = state.get("mood") or "😊"
    text = state.get("custom_text") or ""
    tz_str = _resolve_tz()
    return render_username(template, mood, text, tz_str)


def start_cron(client, owner_id: int, tz_str: str) -> None:
    profile_scheduler.register_engine(_ENGINE_NAME, _FIELD, _render_current)
    if not profile_scheduler.is_running():
        profile_scheduler.start(client, tz_str)


async def stop_cron() -> None:
    profile_scheduler.unregister_engine(_ENGINE_NAME)


def is_running() -> bool:
    return _ENGINE_NAME in profile_scheduler._engines
