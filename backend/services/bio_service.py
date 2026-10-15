"""
Bio service — all bio business logic lives here.

Both text commands and inline panels call these exact functions.
"""
import logging
from datetime import datetime

import asyncio

from backend.bio import engine as bio_engine
from backend.db import client as db_client
from backend.diagnostics import record_event
from backend.helper import symbols as sym

logger = logging.getLogger(__name__)

_PROFILE_TIMEOUT = 30.0
_DEFAULT_TEMPLATE = sym.BIO_DEFAULT_TEMPLATE


async def _apply_profile(owner_id: int, tz_str: str | None = None) -> str:
    """Render the bio engine's current state and apply it to Telegram NOW.

    This is the shared real-mutation boundary for bio writes: it renders
    through the existing engine (including the owner's Glass UI font) and
    sends ONE ``UpdateProfileRequest`` with the ``about`` field through the
    shared profile scheduler's client. It NEVER starts or stops the minute
    cron loop — it only borrows its client reference, so a live bio write is
    possible while the cron is off.

    Returns the applied bio text, raises on failure. Callers must surface
    failures honestly — a persisted DB state alone is NOT a successful bio
    change.
    """
    from backend.profile import scheduler as profile_scheduler
    from backend.services.bio_guardian import guard_bio_mutation
    from telethon.errors import FloodWaitError
    from telethon.tl.functions.account import UpdateProfileRequest

    state = await db_client.get_or_create_bio_state(owner_id)
    if not state:
        raise RuntimeError("bio state unavailable")
    about = bio_engine.render_bio(
        state.get("template", _DEFAULT_TEMPLATE),
        state.get("mood", sym.ICON_MOOD),
        state.get("custom_text", ""),
        tz_str or "UTC",
    )

    async def _mutate() -> str:
        client = profile_scheduler._client
        if client is None:
            raise RuntimeError("no active Telegram client for profile update")
        try:
            await asyncio.wait_for(
                client(UpdateProfileRequest(about=about)),
                timeout=_PROFILE_TIMEOUT,
            )
        except FloodWaitError as exc:
            raise RuntimeError(f"telegram flood wait {exc.seconds}s") from exc
        except asyncio.TimeoutError as exc:
            raise RuntimeError("telegram profile update timed out") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise RuntimeError(f"telegram profile update failed: {exc}") from exc
        return about

    # Every bio mutation path passes this boundary. A rejected mutation
    # raises and is never reported as a successful bio change; a failed RPC
    # never starts the 60-second window.
    about = await guard_bio_mutation(_mutate)
    await db_client.update_bio_state(owner_id, {"last_bio": about})
    record_event("bio", "UpdateProfileRequest", 0, "SUCCESS", "direct apply")
    return about


async def do_on(client, owner_id: int, tz_str: str) -> str:
    try:
        await db_client.get_or_create_bio_state(owner_id)
        await db_client.update_bio_state(owner_id, {"is_active": True})
    except Exception as exc:
        return f"{sym.STATE_FAILED} DB error: {exc}"
    bio_engine.start_cron(client, owner_id, tz_str)
    record_event("bio", "cron on", 0, "SUCCESS")
    state = await db_client.get_or_create_bio_state(owner_id)
    preview = bio_engine.render_bio(
        state.get("template", _DEFAULT_TEMPLATE),
        state.get("mood", sym.ICON_MOOD),
        state.get("custom_text", ""),
        tz_str,
    )
    return f"{sym.OP_SAVE} Bio cron **ON**\nPreview: `{preview}`"


async def do_off(owner_id: int) -> str:
    try:
        await db_client.update_bio_state(owner_id, {"is_active": False})
    except Exception as exc:
        return f"{sym.STATE_FAILED} DB error: {exc}"
    await bio_engine.stop_cron()
    record_event("bio", "cron off", 0, "SUCCESS")
    return f"{sym.OP_STOP} Bio cron **OFF**"


async def do_show(owner_id: int, tz_str: str) -> str:
    state = await db_client.get_or_create_bio_state(owner_id)
    now = bio_engine._get_tz(tz_str)
    now_dt = datetime.now(now)
    preview = bio_engine.render_bio(
        state.get("template", _DEFAULT_TEMPLATE),
        state.get("mood", sym.ICON_MOOD),
        state.get("custom_text", ""),
        tz_str,
    )
    status = "ON" if bio_engine.is_running() else "OFF"
    # Content values render as plain text so the selected Glass UI font
    # applies to them; the template stays in a code span because its
    # {variable} tokens must never be restyled.
    return (
        f"**Bio State**\n\n"
        f"Status: `{status}`\n"
        f"Template: `{state.get('template') or _DEFAULT_TEMPLATE}`\n"
        f"Mood: {state.get('mood') or sym.ICON_MOOD}\n"
        f"Text: {state.get('custom_text') or '—'}\n"
        f"Last Bio: {state.get('last_bio') or '—'}\n"
        f"Preview: {preview}\n"
        f"Server Time ({tz_str}): `{now_dt.strftime('%H:%M:%S')}`"
    )


async def do_template(owner_id: int, template: str, *, client=None, tz_str: str | None = None) -> str:
    if not template:
        return f"{sym.STATE_WARNING} Template cannot be empty."
    try:
        await db_client.update_bio_state(owner_id, {"template": template})
    except Exception as exc:
        return f"{sym.STATE_FAILED} DB error: {exc}"
    try:
        about = await _apply_profile(owner_id, tz_str)
    except Exception as exc:
        return f"{sym.STATE_WARNING} Template saved, but the Telegram bio was NOT updated: {exc}"
    return f"{sym.OP_SAVE} Template updated and applied:\n`{about}`"


async def do_text(owner_id: int, text: str, *, client=None, tz_str: str | None = None) -> str:
    try:
        await db_client.update_bio_state(owner_id, {"custom_text": text})
    except Exception as exc:
        return f"{sym.STATE_FAILED} DB error: {exc}"
    try:
        about = await _apply_profile(owner_id, tz_str)
    except Exception as exc:
        return f"{sym.STATE_WARNING} Text saved, but the Telegram bio was NOT updated: {exc}"
    return f"{sym.OP_SAVE} Bio set to: `{about}`"


async def do_mood(owner_id: int, mood: str, *, client=None, tz_str: str | None = None) -> str:
    try:
        await db_client.update_bio_state(owner_id, {"mood": mood})
    except Exception as exc:
        return f"{sym.STATE_FAILED} DB error: {exc}"
    try:
        about = await _apply_profile(owner_id, tz_str)
    except Exception as exc:
        return f"{sym.STATE_WARNING} Mood saved, but the Telegram bio was NOT updated: {exc}"
    return f"{sym.OP_SAVE} Mood set, bio applied: `{about}`"
