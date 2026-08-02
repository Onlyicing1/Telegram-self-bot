"""
Bio service — all bio business logic lives here.

Both text commands and inline panels call these exact functions.
"""
import logging
from datetime import datetime

from backend.bio import engine as bio_engine
from backend.db import client as db_client
from backend.diagnostics import record_event

logger = logging.getLogger(__name__)


async def do_on(client, owner_id: int, tz_str: str) -> str:
    try:
        await db_client.get_or_create_bio_state(owner_id)
        await db_client.update_bio_state(owner_id, {"is_active": True})
    except Exception as exc:
        return f"❌ DB error: {exc}"
    bio_engine.start_cron(client, owner_id, tz_str)
    record_event("bio", "cron on", 0, "SUCCESS")
    state = await db_client.get_or_create_bio_state(owner_id)
    preview = bio_engine.render_bio(
        state.get("template", "🕒 {time} | 💭 {mood}"),
        state.get("mood", "😊"),
        state.get("custom_text", ""),
        tz_str,
    )
    return f"✅ Bio cron **ON**\nPreview: `{preview}`"


async def do_off(owner_id: int) -> str:
    try:
        await db_client.update_bio_state(owner_id, {"is_active": False})
    except Exception as exc:
        return f"❌ DB error: {exc}"
    bio_engine.stop_cron()
    record_event("bio", "cron off", 0, "SUCCESS")
    return "⏹ Bio cron **OFF**"


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
    return (
        f"**Bio State**\n\n"
        f"Status: `{status}`\n"
        f"Template: `{state.get('template') or '🕒 {time} | 💭 {mood}'}`\n"
        f"Mood: `{state.get('mood') or '😊'}`\n"
        f"Text: `{state.get('custom_text') or '—'}`\n"
        f"Last Bio: `{state.get('last_bio') or '—'}`\n"
        f"Preview: `{preview}`\n"
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
