"""
Bio service — all bio business logic lives here.

Both text commands and inline panels call these exact functions.
Bio Engine controls ONLY the bio. It delegates Telegram updates to the
shared Profile Scheduler.
"""
import logging
import time
from datetime import datetime, timedelta

from backend.bio import engine as bio_engine
from backend.db import client as db_client
from backend.diagnostics import record_event
from backend.profile import scheduler as profile_scheduler

logger = logging.getLogger(__name__)


async def do_on(client, owner_id: int, tz_str: str) -> str:
    try:
        await db_client.update_bio_state(owner_id, {"is_active": True})
    except Exception as exc:
        return f"❌ DB error: {exc}"
    bio_engine.start_cron(client, owner_id, tz_str)
    record_event("bio", "sync on", 0, "SUCCESS")
    state = await db_client.get_or_create_bio_state(owner_id)
    preview = bio_engine.render_bio(
        state.get("template", "🕒 {time} | 💭 {mood}"),
        state.get("mood", "😊"),
        state.get("custom_text", ""),
        tz_str,
    )
    return f"✅ Bio sync **ON**\nPreview: `{preview}`"


async def do_off(owner_id: int) -> str:
    try:
        await db_client.update_bio_state(owner_id, {"is_active": False})
    except Exception as exc:
        return f"❌ DB error: {exc}"
    await bio_engine.stop_cron()
    record_event("bio", "sync off", 0, "SUCCESS")
    return "⏹ Bio sync **OFF**"


async def do_toggle(client, owner_id: int, tz_str: str) -> str:
    state = await db_client.get_or_create_bio_state(owner_id)
    if state.get("is_active"):
        return await do_off(owner_id)
    return await do_on(client, owner_id, tz_str)


async def do_show(owner_id: int, tz_str: str) -> str:
    state = await db_client.get_or_create_bio_state(owner_id)
    now = bio_engine._get_tz(tz_str)
    now_dt = datetime.now(now)
    preview = bio_engine.render_bio(
        state.get("template", "🕒 {time} | 💭 {mood}"),
        state.get("mood", "😊"),
        state.get("custom_text", ""),
        tz_str,
    )
    status = "ON" if bio_engine.is_running() else "OFF"
    last_bio = state.get("last_bio") or "—"

    last_ts = profile_scheduler.get_last_update_ts()
    if last_ts:
        last_age = int(time.time() - last_ts)
        last_update_str = f"{last_age}s ago"
    else:
        last_update_str = "—"

    pending = profile_scheduler.get_pending_info()
    bio_pending = pending.get("bio")
    pending_str = "None"
    if bio_pending:
        pending_str = f"Field: `{bio_pending['field']}`, Last: `{bio_pending.get('last_value', '—')}`"

    next_minute = now_dt.replace(second=0, microsecond=0) + timedelta(minutes=1)
    next_str = next_minute.strftime("%H:%M:00")

    return (
        f"**Bio State**\n\n"
        f"Running: `{status}`\n"
        f"Template: `{state.get('template') or '🕒 {time} | 💭 {mood}'}`\n"
        f"Current rendered value: `{preview}`\n"
        f"Sync status: `{status}`\n"
        f"Next scheduled update: `{next_str}`\n"
        f"Last successful update: `{last_update_str}`\n"
        f"Pending update: `{pending_str}`\n"
        f"Last Bio: `{last_bio}`\n"
        f"Server Time ({tz_str}): `{now_dt.strftime('%H:%M:%S')}`"
    )


async def do_template(owner_id: int, template: str) -> str:
    if not template:
        return "⚠️ Template cannot be empty."
    try:
        await db_client.update_bio_state(owner_id, {"template": template})
    except Exception as exc:
        return f"❌ DB error: {exc}"
    return f"✅ Template updated:\n`{template}`"


async def do_text(owner_id: int, text: str) -> str:
    try:
        await db_client.update_bio_state(owner_id, {"custom_text": text})
    except Exception as exc:
        return f"❌ DB error: {exc}"
    return f"✅ Text set to: `{text}`"


async def do_mood(owner_id: int, mood: str) -> str:
    try:
        await db_client.update_bio_state(owner_id, {"mood": mood})
    except Exception as exc:
        return f"❌ DB error: {exc}"
    return f"✅ Mood set to: `{mood}`"
