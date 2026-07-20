"""
.ping    — Edit trigger with PONG (zero-spam policy).
.id      — Chat ID + Message ID of the current context.
.help    — Interactive help menu (single message, edit-only navigation).
.health  — Full health dashboard.
.kill    — Diagnostic snapshot + stalled-task recovery.
.logs    — View recent diagnostic events (black box).

Help navigation:
  - .help sends ONE message (the main menu).
  - Replying with a number edits that SAME message into the category page.
  - Replying 0 returns to the main menu.
  - Navigation reply messages are auto-deleted (clean chat).
  - State is tracked per-owner via a module-level dict (single owner bot).
"""
import asyncio
import logging
import os
import resource
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telethon import events

from backend import diagnostics, health
from backend.bio import engine as bio_engine
from backend.bot.handlers.guard import is_owner
from backend.db import client as db_client


def _resolve_tz() -> str:
    try:
        tz_str = os.getenv("TZ", "Asia/Tehran")
        ZoneInfo(tz_str)
        return tz_str
    except (ZoneInfoNotFoundError, Exception):
        return "UTC"


logger = logging.getLogger(__name__)

# ── Help menu data ──────────────────────────────────────────────────────
# Each category is (menu_label, page_lines). Add a new category by appending
# to this list — no other changes needed.

_HELP_CATEGORIES: list[tuple[str, list[str]]] = [
    (
        "General",
        [
            "**General**\n",
            "`.ping` — PONG",
            "`.id` — Chat & Msg IDs",
            "`.help` — This menu",
            "`.health` — Health dashboard",
        ],
    ),
    (
        "Save Engine",
        [
            "**Save Engine**  _(reply to a message)_\n",
            "`.save f` · `.s f` — Forward save",
            "`.save d` · `.s d` — Deep save",
        ],
    ),
    (
        "Retrieve",
        [
            "**Retrieve**\n",
            "`.preview <code>` — Show metadata",
            "`.r <code>` · `.retrieve <code>` — Alias",
            "`.send <code>` — Forward asset here",
        ],
    ),
    (
        "Organizer",
        [
            "**Organizer**\n",
            "`.del <n>` — Delete last n messages",
            "`.del id <msgid>` — Delete from msgid",
            "`.del <code>` — Delete a saved item",
            "`.organize list` — Data overview",
            "`.organize clean` — Purge old logs",
        ],
    ),
    (
        "Bio Engine",
        [
            "**Bio Engine**\n",
            "`.bio help` — Token reference",
            "`.bio on` — Start cron",
            "`.bio off` — Stop cron",
            "`.bio show` — Inspect state",
            "`.bio template <tpl>` — Set template",
            "`.bio text <text>` — Set {text}",
            "`.bio mood <mood>` — Set {mood}",
        ],
    ),
    (
        "Database",
        [
            "**Database**\n",
            "`.db clean` — Remove orphan rows",
            "`.db stats` — Database statistics",
            "`.db vacuum` — Cleanup + optimize",
        ],
    ),
    (
        "Diagnostics",
        [
            "**Diagnostics**\n",
            "`.kill` — Snapshot + recovery",
            "`.logs` — Recent events (last 20)",
            "`.logs 50` — Last 50 events",
            "`.logs errors` — Errors only",
            "`.logs module <m>` — Filter by module",
        ],
    ),
]


def _build_main_menu() -> str:
    lines = ["**LifeOS Command Center**\n"]
    for i, (label, _) in enumerate(_HELP_CATEGORIES, start=1):
        lines.append(f"`{i}` • {label}")
    lines.append("\n_Reply with a number._")
    return "\n".join(lines)


def _build_category_page(index: int) -> str:
    _, lines = _HELP_CATEGORIES[index]
    page = list(lines)
    page.append("\n_Reply `0` to return._")
    return "\n".join(page)


# ── Help state (single owner) ───────────────────────────────────────────

_help_state: dict[int, dict] = {}


def _get_help_state(owner_id: int) -> dict | None:
    return _help_state.get(owner_id)


def _set_help_state(owner_id: int, msg_id: int) -> None:
    _help_state[owner_id] = {"msg_id": msg_id}


def _clear_help_state(owner_id: int) -> None:
    _help_state.pop(owner_id, None)


# ── Health dashboard ────────────────────────────────────────────────────


def _format_uptime(uptime_s):
    if uptime_s is None or uptime_s < 0:
        return "unknown"
    hours = int(uptime_s // 3600)
    minutes = int((uptime_s % 3600) // 60)
    if hours > 0:
        return f"{hours}h {minutes}m"
    return f"{minutes}m"


def _format_age(age_s):
    if age_s is None:
        return "—"
    if age_s < 60:
        return f"{int(age_s)}s ago"
    m = int(age_s // 60)
    if m < 60:
        return f"{m}m ago"
    h = m // 60
    return f"{h}h {m % 60}m ago"


def _indicator(ok):
    return "🟢" if ok else "🔴"


def _warn_indicator(ok):
    return "🟢" if ok else "🟡"


def _build_health_report(snap):
    process_ok = snap.get("process_alive", False)
    telegram_ok = snap.get("telethon_connected", False)
    supervisor_ok = snap.get("supervisor_ok", False)
    bio_cron_ok = snap.get("bio_cron_ok", False)
    watchdog_ok = snap.get("watchdog_ok", False)
    heartbeat_age = snap.get("heartbeat_age_s")
    uptime_s = snap.get("uptime_s")
    restart_count = snap.get("restart_count", 0)
    last_watchdog = snap.get("last_watchdog_check_s")
    last_tg_event = snap.get("last_telethon_event_s")
    last_bio = snap.get("last_bio_update_s")
    status = snap.get("status", "unknown")

    # Process section
    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        mem_mb = usage.ru_maxrss / 1024
        cpu_s = usage.ru_utime + usage.ru_stime
    except Exception:
        mem_mb = None
        cpu_s = None

    # Task counts
    try:
        all_tasks = asyncio.all_tasks()
        running = sum(1 for t in all_tasks if not t.done())
        pending = sum(1 for t in all_tasks if not t.done())
        locked = 0
    except Exception:
        running = None
        pending = None
        locked = None

    # DB status
    db_ok = db_client.is_available()

    # Heartbeat status
    if heartbeat_age is not None and heartbeat_age <= 15.0:
        hb_status = "OK"
    elif heartbeat_age is not None:
        hb_status = "WARNING"
    else:
        hb_status = "ERROR"

    lines = ["🩺 **LifeOS Health Dashboard**", ""]

    # Process
    lines.append(f"{_indicator(process_ok)} **Process**: {'Alive' if process_ok else 'Dead'}")
    if mem_mb is not None:
        lines.append(f"   • Memory: `{mem_mb:.1f} MB`")
    if cpu_s is not None:
        lines.append(f"   • CPU: `{cpu_s:.2f}s`")

    # Telegram
    lines.append(f"{_indicator(telegram_ok)} **Telegram**: {'Connected' if telegram_ok else 'Disconnected'}")
    lines.append(f"   • Last event: {_format_age(last_tg_event)}")

    # Supervisor
    lines.append(f"{_indicator(supervisor_ok)} **Supervisor**: {'Running' if supervisor_ok else 'Stopped'}")

    # Watchdog
    lines.append(f"{_indicator(watchdog_ok)} **Watchdog**: {'Running' if watchdog_ok else 'Stopped'}")
    lines.append(f"   • Last check: {_format_age(last_watchdog)}")

    # Bio Cron
    lines.append(f"{_indicator(bio_cron_ok)} **Bio Cron**: {'Running' if bio_cron_ok else 'Stopped'}")
    lines.append(f"   • Last update: {_format_age(last_bio)}")

    # Heartbeat
    hb_icon = "🟢" if hb_status == "OK" else ("🟡" if hb_status == "WARNING" else "🔴")
    lines.append(f"{hb_icon} **Heartbeat**: {hb_status}")
    if heartbeat_age is not None:
        lines.append(f"   • Age: `{int(heartbeat_age)}s`")

    # Restart counter
    lines.append(f"{'🟢' if restart_count == 0 else '🟡'} **Restarts**: `{restart_count}`")

    # Tasks
    if running is not None:
        lines.append(f"{'🟢' if running < 20 else '🟡'} **Running Tasks**: `{running}`")
    if pending is not None:
        lines.append(f"{'🟢' if pending < 20 else '🟡'} **Pending Async**: `{pending}`")
    if locked is not None:
        lines.append(f"{'🟢' if locked == 0 else '🟡'} **Locked Tasks**: `{locked}`")

    # Database
    lines.append(f"{_indicator(db_ok)} **Database**: {'Available' if db_ok else 'Fallback'}")

    # Uptime
    lines.append(f"{'🟢' if uptime_s and uptime_s > 0 else '🔴'} **Uptime**: `{_format_uptime(uptime_s)}`")

    lines.append("")
    if status == "ok":
        lines.append("_Everything looks healthy._")
    else:
        lines.append("_⚠️ Issues detected — needs attention._")

    return "\n".join(lines)


async def _safe_edit(event, text: str) -> None:
    """Edit a message, splitting if it exceeds Telegram's limit."""
    parts = diagnostics.split_message(text)
    for i, part in enumerate(parts):
        if i == 0:
            await event.edit(part)
        else:
            await event.reply(part)


def register(client, owner_id: int):

    # ── .ping ──────────────────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.ping$"))
    async def ping(event):
        if not is_owner(event, owner_id):
            return
        try:
            await event.edit("PONG")
        except Exception as exc:
            logger.warning("ping edit failed: %s", exc)

    # ── .id ────────────────────────────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.id$"))
    async def id_cmd(event):
        if not is_owner(event, owner_id):
            return
        try:
            chat_id = event.chat_id
            msg_id = event.message.id
            reply = await event.message.get_reply_message()
            lines = [f"**Chat ID:** `{chat_id}`", f"**Msg ID:** `{msg_id}`"]
            if reply:
                lines.append(f"**Reply Msg ID:** `{reply.id}`")
                lines.append(f"**Reply Sender ID:** `{reply.sender_id}`")
            await event.edit("\n".join(lines))
        except Exception as exc:
            logger.warning("id_cmd failed: %s", exc)

    # ── .help — interactive menu ──────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.help$"))
    async def help_cmd(event):
        if not is_owner(event, owner_id):
            return
        try:
            await event.edit(_build_main_menu())
            _set_help_state(owner_id, event.message.id)
        except Exception as exc:
            logger.warning("help edit failed: %s", exc)

    # ── Help navigation (reply with number) ────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^(\d+)$"))
    async def help_nav(event):
        if not is_owner(event, owner_id):
            return

        state = _get_help_state(owner_id)
        if not state:
            return

        reply_msg = await event.message.get_reply_message()
        if not reply_msg or reply_msg.id != state["msg_id"]:
            return

        num = int(event.pattern_match.group(1))

        # Auto-delete the navigation reply (clean chat)
        try:
            await event.delete()
        except Exception:
            pass

        if num == 0:
            # Return to main menu
            try:
                await reply_msg.edit(_build_main_menu())
            except Exception as exc:
                logger.warning("help nav edit failed: %s", exc)
            return

        if num < 1 or num > len(_HELP_CATEGORIES):
            return

        try:
            await reply_msg.edit(_build_category_page(num - 1))
        except Exception as exc:
            logger.warning("help nav edit failed: %s", exc)

    # ── .health — full dashboard ──────────────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.health$"))
    async def health_cmd(event):
        if not is_owner(event, owner_id):
            return
        try:
            snap = health.snapshot()
            report = _build_health_report(snap)
            await _safe_edit(event, report)
            diagnostics.record_event("health", "snapshot", 0, "SUCCESS")
        except Exception as exc:
            logger.warning("health_cmd failed: %s", exc)
            diagnostics.record_event("health", "snapshot", 0, "ERROR", str(exc))
            try:
                await event.edit(f"⚠️ Health check failed: {exc}")
            except Exception:
                pass

    # ── .kill — diagnostic snapshot + recovery ────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.kill$"))
    async def kill_cmd(event):
        if not is_owner(event, owner_id):
            return
        try:
            await event.edit("⏳ Collecting diagnostics...")
        except Exception:
            return

        try:
            snap = health.snapshot()
            report = diagnostics.build_diagnostic_report(
                client, bio_engine, db_client, snap
            )
            recovery = await diagnostics.recover_stalled(
                client, owner_id, _resolve_tz(), bio_engine, db_client
            )
            await _safe_edit(event, report + recovery)
            diagnostics.record_event("diagnostics", "kill", 0, "SUCCESS")
        except Exception as exc:
            logger.warning("kill_cmd failed: %s", exc)
            diagnostics.record_event("diagnostics", "kill", 0, "ERROR", str(exc))
            try:
                await event.edit(f"⚠️ Kill diagnostic failed: {exc}")
            except Exception:
                pass

    # ── .logs — diagnostic event viewer ───────────────────────────────
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.logs(?:\s+(.+))?$"))
    async def logs_cmd(event):
        if not is_owner(event, owner_id):
            return

        arg = (event.pattern_match.group(1) or "").strip()
        limit = 20
        module = None
        errors_only = False

        if arg:
            if arg.lower() == "errors":
                errors_only = True
            elif arg.lower().startswith("module "):
                module = arg[7:].strip()
            elif arg.isdigit():
                limit = int(arg)
                if limit < 1:
                    limit = 20
                if limit > 500:
                    limit = 500
            else:
                await event.edit(
                    "⚠️ Usage:\n"
                    "`.logs` — last 20\n"
                    "`.logs 50` — last 50\n"
                    "`.logs errors` — errors only\n"
                    "`.logs module <name>` — filter by module"
                )
                return

        try:
            events_list = diagnostics.filter_events(
                limit=limit, module=module, errors_only=errors_only
            )
            text = diagnostics.format_events(events_list)
            await _safe_edit(event, text)
        except Exception as exc:
            logger.warning("logs_cmd failed: %s", exc)
            try:
                await event.edit(f"⚠️ Logs failed: {exc}")
            except Exception:
                pass
