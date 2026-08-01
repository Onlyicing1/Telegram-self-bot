"""
Username service — all username business logic lives here.

Mirrors bio_service.py in structure. Both text commands and inline panels call these exact functions.
The Username Engine is completely independent from the Bio Engine.
"""
import logging
import time
from datetime import datetime

from backend.db import client as db_client
from backend.diagnostics import record_event
from backend.username import engine as username_engine
from backend.profile import scheduler as profile_scheduler

logger = logging.getLogger(__name__)


async def do_on(client, owner_id: int, tz_str: str) -> str:
    try:
        await db_client.update_username_state(owner_id, {"is_active": True})
    except Exception as exc:
        return f"❌ DB error: {exc}"
    username_engine.start_cron(client, owner_id, tz_str)
    record_event("username", "sync on", 0, "SUCCESS")
    state = await db_client.get_or_create_username_state(owner_id)
    preview = username_engine.render_username(
        state.get("template", "🕒 {time}"),
        state.get("mood", "😊"),
        state.get("custom_text", ""),
        tz_str,
    )
    return f"✅ Username sync **ON**\nPreview: `{preview}`"


async def do_off(owner_id: int) -> str:
    try:
        await db_client.update_username_state(owner_id, {"is_active": False})
    except Exception as exc:
        return f"❌ DB error: {exc}"
    await username_engine.stop_cron()
    record_event("username", "sync off", 0, "SUCCESS")
    return "⏹ Username sync **OFF**"


async def do_toggle(client, owner_id: int, tz_str: str) -> str:
    state = await db_client.get_or_create_username_state(owner_id)
    if state.get("is_active"):
        return await do_off(owner_id)
    return await do_on(client, owner_id, tz_str)


async def do_show(owner_id: int, tz_str: str) -> str:
    state = await db_client.get_or_create_username_state(owner_id)
    now = username_engine._get_tz(tz_str)
    now_dt = datetime.now(now)
    preview = username_engine.render_username(
        state.get("template", "🕒 {time}"),
        state.get("mood", "😊"),
        state.get("custom_text", ""),
        tz_str,
    )
    status = "ON" if username_engine.is_running() else "OFF"
    last_username = state.get("last_username") or "—"

    last_ts = profile_scheduler.get_last_update_ts()
    if last_ts:
        last_age = int(time.time() - last_ts)
        last_update_str = f"{last_age}s ago"
    else:
        last_update_str = "—"

    pending = profile_scheduler.get_pending_info()
    username_pending = pending.get("username")
    pending_str = "None"
    if username_pending:
        pending_str = f"Field: `{username_pending['field']}`, Last: `{username_pending.get('last_value', '—')}`"

    next_minute = now_dt.replace(second=0, microsecond=0)
    from datetime import timedelta
    next_minute = next_minute + timedelta(minutes=1)
    next_str = next_minute.strftime("%H:%M:00")

    return (
        f"**Username State**\n\n"
        f"Running: `{status}`\n"
        f"Template: `{state.get('template') or '🕒 {time}'}`\n"
        f"Current rendered value: `{preview}`\n"
        f"Sync status: `{status}`\n"
        f"Next scheduled update: `{next_str}`\n"
        f"Last successful update: `{last_update_str}`\n"
        f"Pending update: `{pending_str}`\n"
        f"Last Username: `{last_username}`\n"
        f"Server Time ({tz_str}): `{now_dt.strftime('%H:%M:%S')}`"
    )


async def do_template(owner_id: int, template: str) -> str:
    if not template:
        return "⚠️ Template cannot be empty."
    try:
        await db_client.update_username_state(owner_id, {"template": template})
    except Exception as exc:
        return f"❌ DB error: {exc}"
    return f"✅ Template updated:\n`{template}`"


async def do_text(owner_id: int, text: str) -> str:
    try:
        await db_client.update_username_state(owner_id, {"custom_text": text})
    except Exception as exc:
        return f"❌ DB error: {exc}"
    return f"✅ Text set to: `{text}`"


async def do_mood(owner_id: int, mood: str) -> str:
    try:
        await db_client.update_username_state(owner_id, {"mood": mood})
    except Exception as exc:
        return f"❌ DB error: {exc}"
    return f"✅ Mood set to: `{mood}`"
