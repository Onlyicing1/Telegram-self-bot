"""
Username service — all username business logic lives here.

Both text commands and inline panels call these exact functions.
Mirrors bio_service exactly, but for the Telegram first_name field.
"""
import logging
from datetime import datetime

from backend.username import engine as username_engine
from backend.db import client as db_client
from backend.diagnostics import record_event
from backend.helper import symbols as sym

logger = logging.getLogger(__name__)


async def do_on(client, owner_id: int, tz_str: str) -> str:
    try:
        await db_client.get_or_create_username_state(owner_id)
        await db_client.update_username_state(owner_id, {"is_active": True})
    except Exception as exc:
        return f"{sym.STATE_FAILED} DB error: {exc}"
    username_engine.start_cron(client, owner_id, tz_str)
    record_event("username", "cron on", 0, "SUCCESS")
    state = await db_client.get_or_create_username_state(owner_id)
    preview = username_engine.render_username(
        state.get("template", sym.USERNAME_DEFAULT_TEMPLATE),
        state.get("mood", sym.ICON_MOOD),
        state.get("custom_text", ""),
        tz_str,
    )
    return f"{sym.OP_SAVE} Username sync **ON**\nPreview: `{preview}`"


async def do_off(owner_id: int) -> str:
    try:
        await db_client.update_username_state(owner_id, {"is_active": False})
    except Exception as exc:
        return f"{sym.STATE_FAILED} DB error: {exc}"
    await username_engine.stop_cron()
    record_event("username", "cron off", 0, "SUCCESS")
    return f"{sym.OP_STOP} Username sync **OFF**"


async def do_show(owner_id: int, tz_str: str) -> str:
    state = await db_client.get_or_create_username_state(owner_id)
    now = username_engine._get_tz(tz_str)
    now_dt = datetime.now(now)
    preview = username_engine.render_username(
        state.get("template", sym.USERNAME_DEFAULT_TEMPLATE),
        state.get("mood", sym.ICON_MOOD),
        state.get("custom_text", ""),
        tz_str,
    )
    status = "ON" if username_engine.is_running() else "OFF"
    # Content values render as plain text so the selected Glass UI font
    # applies to them; the template stays in a code span because its
    # {variable} tokens must never be restyled.
    return (
        f"**Username State**\n\n"
        f"Status: `{status}`\n"
        f"Template: `{state.get('template') or sym.USERNAME_DEFAULT_TEMPLATE}`\n"
        f"Mood: {state.get('mood') or sym.ICON_MOOD}\n"
        f"Text: {state.get('custom_text') or '—'}\n"
        f"Last Name: {state.get('last_name') or '—'}\n"
        f"Preview: {preview}\n"
        f"Server Time ({tz_str}): `{now_dt.strftime('%H:%M:%S')}`"
    )


async def do_template(owner_id: int, template: str) -> str:
    if not template:
        return f"{sym.STATE_WARNING} Template cannot be empty."
    try:
        await db_client.update_username_state(owner_id, {"template": template})
    except Exception as exc:
        return f"{sym.STATE_FAILED} DB error: {exc}"
    return f"{sym.OP_SAVE} Template updated:\n`{template}`"


async def do_text(owner_id: int, text: str) -> str:
    try:
        await db_client.update_username_state(owner_id, {"custom_text": text})
    except Exception as exc:
        return f"{sym.STATE_FAILED} DB error: {exc}"
    return f"{sym.OP_SAVE} Text set to: `{text}`"


async def do_mood(owner_id: int, mood: str) -> str:
    try:
        await db_client.update_username_state(owner_id, {"mood": mood})
    except Exception as exc:
        return f"{sym.STATE_FAILED} DB error: {exc}"
    return f"{sym.OP_SAVE} Mood set to: `{mood}`"
