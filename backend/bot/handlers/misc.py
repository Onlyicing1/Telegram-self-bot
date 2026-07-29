"""
.ping    — Edit trigger with PONG (zero-spam policy).
.id      — Chat ID + Message ID of the current context.
.help    — Interactive inline help panel (via Inline Mode).
.panel   — Context panel for the replied message.
.health  — Full health dashboard (inline panel).
.kill    — Diagnostic snapshot + stalled-task recovery (inline panel).
.logs    — View recent diagnostic events (inline panel).

Falls back to plain-text edit-in-place when the helper bot is not available.
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
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    register_action,
    send_inline_panel,
    render,
    render_edit,
    to_edit_buttons,
    TargetContext,
    set_target,
    get_target,
    is_auto_close_enabled,
    toggle_auto_close,
)
from backend.helper.client import get_client


def _resolve_tz() -> str:
    try:
        tz_str = os.getenv("TZ", "Asia/Tehran")
        ZoneInfo(tz_str)
        return tz_str
    except (ZoneInfoNotFoundError, Exception):
        return "UTC"


logger = logging.getLogger(__name__)

_HELP_CATEGORIES: list[tuple[str, list[str]]] = [
    (
        "General",
        [
            "**General**\n",
            "`.ping` — PONG",
            "`.id` — Chat & Msg IDs",
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


def _build_main_menu_buttons() -> list:
    builder = InlinePanelBuilder()
    cats = _HELP_CATEGORIES
    for i in range(0, len(cats) - 1, 2):
        builder.add_buttons(
            (cats[i][0], f"panel:help:cat:{i}"),
            (cats[i + 1][0], f"panel:help:cat:{i + 1}"),
        )
    builder.add_row(cats[-1][0], f"panel:help:cat:{len(cats) - 1}")
    builder.add_row("⚙️ Settings", "panel:settings")
    return builder.build()


def _build_general_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("🏓 Ping", "action:general_ping")
    builder.add_row("🆔 Chat & Msg IDs", "action:general_id")
    builder.add_row("🩺 Health Dashboard", "action:general_health")
    return builder.build()


def _general_body() -> str:
    return (
        "**General**\n\n"
        "Tap a button to execute instantly."
    )


async def _help_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    if extra == "back":
        return "LifeOS Command Center", "", _build_main_menu_buttons()
    if extra.startswith("cat:"):
        idx_str = extra[4:]
        if idx_str.isdigit():
            idx = int(idx_str)
            if 0 <= idx < len(_HELP_CATEGORIES):
                if idx == 0:
                    return _HELP_CATEGORIES[0][0], _general_body(), _build_general_buttons()
                _, lines = _HELP_CATEGORIES[idx]
                body = "\n".join(lines)
                return _HELP_CATEGORIES[idx][0], body, []
    return "LifeOS Command Center", "", _build_main_menu_buttons()


async def _help_inline_builder(event, extra: str) -> list:
    if extra.startswith("cat:0"):
        return [render("General", _general_body(), _build_general_buttons())]
    return [render("LifeOS Command Center", "", _build_main_menu_buttons())]


async def _settings_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    enabled = is_auto_close_enabled()
    status = "ON" if enabled else "OFF"
    body = f"**Auto Close: {status}**\n\nPanels will {'auto-delete after 120s' if enabled else 'stay open until manually closed'}."
    builder = InlinePanelBuilder()
    builder.add_row(f"Auto Close: {status}", "action:settings_toggle_autoclose")
    return "Settings", body, builder.build()


async def _settings_inline_builder(event, extra: str) -> list:
    enabled = is_auto_close_enabled()
    status = "ON" if enabled else "OFF"
    body = f"**Auto Close: {status}**\n\nPanels will {'auto-delete after 120s' if enabled else 'stay open until manually closed'}."
    builder = InlinePanelBuilder()
    builder.add_row(f"Auto Close: {status}", "action:settings_toggle_autoclose")
    return [render("Settings", body, builder.build())]


async def _settings_toggle_autoclose_action(event, extra: str) -> tuple[str, str, list] | None:
    new_val = toggle_auto_close()
    status = "ON" if new_val else "OFF"
    body = f"**Auto Close: {status}**\n\nPanels will {'auto-delete after 120s' if new_val else 'stay open until manually closed'}."
    builder = InlinePanelBuilder()
    builder.add_row(f"Auto Close: {status}", "action:settings_toggle_autoclose")
    return "Settings", body, builder.build()


async def _general_ping_action(event, extra: str) -> tuple[str, str, list] | None:
    return "PONG", "", _build_general_buttons()


async def _general_id_action(event, extra: str) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _self_client, _owner_id

    owner_id = _owner_id
    client = _self_client

    your_id = "N/A"
    if client is not None:
        try:
            me = await client.get_me()
            your_id = str(me.id)
        except Exception:
            pass
    elif owner_id:
        your_id = str(owner_id)

    chat_id_val = "N/A"
    msg_id_val = "N/A"
    try:
        cid = getattr(event, "chat_id", None)
        if cid is not None:
            chat_id_val = str(cid)
    except Exception:
        pass
    try:
        mid = getattr(event, "message_id", None)
        if mid is not None:
            msg_id_val = str(mid)
    except Exception:
        pass

    body = (
        f"**Your ID:**\n`{your_id}`\n\n"
        f"**Current Chat ID:**\n`{chat_id_val}`\n\n"
        f"**Current Message ID:**\n`{msg_id_val}`"
    )
    return "Chat & Message IDs", body, _build_general_buttons()


async def _general_health_action(event, extra: str) -> tuple[str, str, list] | None:
    snap = health.snapshot()
    report = _build_health_report(snap)
    builder = InlinePanelBuilder()
    builder.add_row("Refresh", "action:health_refresh")
    return "Health Dashboard", report, builder.build()


def _register_help_panel() -> None:
    register_panel("help", _help_panel_handler)
    register_panel("settings", _settings_panel_handler)
    register_inline_builder("help", _help_inline_builder)
    register_inline_builder("settings", _settings_inline_builder)
    register_action("settings_toggle_autoclose", _settings_toggle_autoclose_action)
    register_action("general_ping", _general_ping_action)
    register_action("general_id", _general_id_action)
    register_action("general_health", _general_health_action)


async def _context_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id

    owner_id = _owner_id
    ctx = get_target(owner_id)

    if not ctx or ctx.kind != "reply":
        return "Context Panel", "Reply context expired.\n\nUse `.panel` while replying to a message.", []

    if extra.startswith("exec:"):
        action = extra[5:]
        from backend.helper.inline_engine import _self_client
        client = _self_client

        if action == "save_f":
            from backend.services import save_service
            reply_msg = await ctx.resolve(client)
            if reply_msg is None:
                return "Context Panel", "Reply message no longer exists.", []
            result = await save_service.execute_save(client, owner_id, reply_msg, "f", ctx.tz_str)
            return "Save Engine", result, []

        elif action == "save_d":
            from backend.services import save_service
            reply_msg = await ctx.resolve(client)
            if reply_msg is None:
                return "Context Panel", "Reply message no longer exists.", []
            result = await save_service.execute_save(client, owner_id, reply_msg, "d", ctx.tz_str)
            return "Save Engine", result, []

        elif action == "preview":
            from backend.helper.inline_engine import _self_client
            from backend.services import retrieve_service
            reply_msg = await ctx.resolve(_self_client)
            if reply_msg is None:
                return "Context Panel", "Reply message no longer exists.", []
            result = await retrieve_service.do_preview(_self_client, owner_id, str(reply_msg.id))
            return "Preview", result, []

    builder = InlinePanelBuilder()
    builder.add_row("📦 Forward Save", "panel:context:exec:save_f")
    builder.add_row("⬇️ Deep Save", "panel:context:exec:save_d")
    builder.add_row("👁 Preview", "panel:context:exec:preview")
    return "Context Panel", f"**Chat:** `{ctx.reply_chat_id}`\n**Message:** `{ctx.reply_msg_id}`\n\nChoose an action:", builder.build()


async def _context_inline_builder(event, extra: str) -> list:
    from backend.helper.inline_engine import _owner_id

    owner_id = _owner_id
    ctx = get_target(owner_id)

    if not ctx or ctx.kind != "reply":
        return [render("Context Panel", "Reply context expired.\n\nUse `.panel` while replying to a message.", [])]

    builder = InlinePanelBuilder()
    builder.add_row("📦 Forward Save", "panel:context:exec:save_f")
    builder.add_row("⬇️ Deep Save", "panel:context:exec:save_d")
    builder.add_row("👁 Preview", "panel:context:exec:preview")
    return [render(
        "Context Panel",
        f"**Chat:** `{ctx.reply_chat_id}`\n**Message:** `{ctx.reply_msg_id}`\n\nChoose an action:",
        builder.build(),
    )]


async def _context_error_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    return "Context Panel", "No replied message found.\n\nReply to any message and use `.panel` to open its context panel.", []


async def _context_error_inline_builder(event, extra: str) -> list:
    return [render(
        "Context Panel",
        "No replied message found.\n\nReply to any message and use `.panel` to open its context panel.",
        [],
    )]


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

    try:
        usage = resource.getrusage(resource.RUSAGE_SELF)
        mem_mb = usage.ru_maxrss / 1024
        cpu_s = usage.ru_utime + usage.ru_stime
    except Exception:
        mem_mb = None
        cpu_s = None

    try:
        all_tasks = asyncio.all_tasks()
        running = sum(1 for t in all_tasks if not t.done())
    except Exception:
        running = None

    db_ok = db_client.is_available()

    if heartbeat_age is not None and heartbeat_age <= 15.0:
        hb_status = "OK"
    elif heartbeat_age is not None:
        hb_status = "WARNING"
    else:
        hb_status = "ERROR"

    lines = ["🩺 **LifeOS Health Dashboard**", ""]

    lines.append(f"{_indicator(process_ok)} **Process**: {'Alive' if process_ok else 'Dead'}")
    if mem_mb is not None:
        lines.append(f"   • Memory: `{mem_mb:.1f} MB`")
    if cpu_s is not None:
        lines.append(f"   • CPU: `{cpu_s:.2f}s`")

    lines.append(f"{_indicator(telegram_ok)} **Telegram**: {'Connected' if telegram_ok else 'Disconnected'}")
    lines.append(f"   • Last event: {_format_age(last_tg_event)}")

    lines.append(f"{_indicator(supervisor_ok)} **Supervisor**: {'Running' if supervisor_ok else 'Stopped'}")

    lines.append(f"{_indicator(watchdog_ok)} **Watchdog**: {'Running' if watchdog_ok else 'Stopped'}")
    lines.append(f"   • Last check: {_format_age(last_watchdog)}")

    lines.append(f"{_indicator(bio_cron_ok)} **Bio Cron**: {'Running' if bio_cron_ok else 'Stopped'}")
    lines.append(f"   • Last update: {_format_age(last_bio)}")

    hb_icon = "🟢" if hb_status == "OK" else ("🟡" if hb_status == "WARNING" else "🔴")
    lines.append(f"{hb_icon} **Heartbeat**: {hb_status}")
    if heartbeat_age is not None:
        lines.append(f"   • Age: `{int(heartbeat_age)}s`")

    lines.append(f"{'🟢' if restart_count == 0 else '🟡'} **Restarts**: `{restart_count}`")

    if running is not None:
        lines.append(f"{'🟢' if running < 20 else '🟡'} **Running Tasks**: `{running}`")

    lines.append(f"{_indicator(db_ok)} **Database**: {'Available' if db_ok else 'Fallback'}")

    lines.append(f"{'🟢' if uptime_s and uptime_s > 0 else '🔴'} **Uptime**: `{_format_uptime(uptime_s)}`")

    lines.append("")
    if status == "ok":
        lines.append("_Everything looks healthy._")
    else:
        lines.append("_⚠️ Issues detected — needs attention._")

    return "\n".join(lines)


async def _health_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    snap = health.snapshot()
    report = _build_health_report(snap)
    builder = InlinePanelBuilder()
    builder.add_row("Refresh", "action:health_refresh")
    return "Health Dashboard", report, builder.build()


async def _health_inline_builder(event, extra: str) -> list:
    snap = health.snapshot()
    report = _build_health_report(snap)
    builder = InlinePanelBuilder()
    builder.add_row("Refresh", "action:health_refresh")
    return [render("Health Dashboard", report, builder.build())]


async def _health_refresh_action(event, extra: str) -> tuple[str, str, list] | None:
    snap = health.snapshot()
    report = _build_health_report(snap)
    builder = InlinePanelBuilder()
    builder.add_row("Refresh", "action:health_refresh")
    return "Health Dashboard", report, builder.build()


async def _kill_inline_builder(event, extra: str) -> list:
    snap = health.snapshot()
    self_client = _get_self_client()
    report = diagnostics.build_diagnostic_report(
        self_client, bio_engine, db_client, snap
    )
    recovery = await diagnostics.recover_stalled(
        self_client, 0, _resolve_tz(), bio_engine, db_client
    )
    full_text = report + recovery
    return [render("Diagnostics", full_text, [])]


async def _logs_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    limit = 20
    if extra and extra.isdigit():
        limit = min(int(extra), 500)
    elif extra == "errors":
        limit = 20
    events_list = diagnostics.filter_events(
        limit=limit,
        errors_only=(extra == "errors"),
    )
    text = diagnostics.format_events(events_list)
    builder = InlinePanelBuilder()
    builder.add_row("Errors Only", "action:logs_errors")
    builder.add_row("Last 50", "action:logs_50")
    return "Event Log", text, builder.build()


async def _logs_inline_builder(event, extra: str) -> list:
    limit = 20
    if extra and extra.isdigit():
        limit = min(int(extra), 500)
    elif extra == "errors":
        limit = 20
    events_list = diagnostics.filter_events(
        limit=limit,
        errors_only=(extra == "errors"),
    )
    text = diagnostics.format_events(events_list)
    builder = InlinePanelBuilder()
    builder.add_row("Errors Only", "action:logs_errors")
    builder.add_row("Last 50", "action:logs_50")
    return [render("Event Log", text, builder.build())]


async def _logs_errors_action(event, extra: str) -> tuple[str, str, list] | None:
    events_list = diagnostics.filter_events(limit=20, errors_only=True)
    text = diagnostics.format_events(events_list)
    builder = InlinePanelBuilder()
    builder.add_row("Last 50", "action:logs_50")
    return "Event Log", text, builder.build()


async def _logs_50_action(event, extra: str) -> tuple[str, str, list] | None:
    events_list = diagnostics.filter_events(limit=50, errors_only=False)
    text = diagnostics.format_events(events_list)
    builder = InlinePanelBuilder()
    builder.add_row("Errors Only", "action:logs_errors")
    return "Event Log", text, builder.build()


def _get_self_client():
    from backend.helper.inline_engine import _self_client
    return _self_client


async def _safe_edit(event, text: str) -> None:
    parts = diagnostics.split_message(text)
    for i, part in enumerate(parts):
        if i == 0:
            await event.edit(part)
        else:
            await event.reply(part)


def register(client, owner_id: int):
    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.ping$"))
    async def ping(event):
        if not is_owner(event, owner_id):
            return
        try:
            await event.edit("PONG")
        except Exception as exc:
            logger.warning("ping edit failed: %s", exc)

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

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.help$"))
    async def help_cmd(event):
        if not is_owner(event, owner_id):
            return

        helper = get_client()
        if helper is None:
            text, _ = render_edit("LifeOS Command Center", "", _build_main_menu_buttons())
            await event.edit(text)
            return

        try:
            await event.delete()
            await send_inline_panel(client, event.chat_id, "help")
        except Exception as exc:
            logger.warning("help inline send failed: %s", exc)
            try:
                text, _ = render_edit("LifeOS Command Center", "", _build_main_menu_buttons())
                await event.edit(text)
            except Exception:
                pass

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.panel$"))
    async def panel_cmd(event):
        if not is_owner(event, owner_id):
            return

        reply = await event.message.get_reply_message()
        if not reply:
            helper = get_client()
            if helper is None:
                await event.edit("⚠️ Reply to a message first, then use `.panel`")
                return
            try:
                await event.delete()
                await send_inline_panel(client, event.chat_id, "context_error")
            except Exception as exc:
                logger.warning("panel error inline send failed: %s", exc)
            return

        helper = get_client()
        if helper is None:
            await event.edit("⚠️ Inline mode requires the helper bot (BOT_TOKEN).")
            return

        set_target(owner_id, TargetContext(
            owner_id=owner_id,
            kind="reply",
            reply_chat_id=reply.chat_id,
            reply_msg_id=reply.id,
            tz_str=_resolve_tz(),
        ))

        try:
            success = await send_inline_panel(client, event.chat_id, "context")
            if success:
                await event.delete()
            else:
                await event.edit("⚠️ Panel failed to open. Check logs.")
        except Exception as exc:
            logger.warning("panel inline send failed: %s", exc)
            try:
                await event.edit(f"⚠️ Panel failed: {exc}")
            except Exception:
                pass

    try:
        _register_help_panel()
        register_panel("context", _context_panel_handler)
        register_panel("context_error", _context_error_panel_handler)
        register_panel("health", _health_panel_handler)
        register_panel("logs", _logs_panel_handler)
        register_inline_builder("health", _health_inline_builder)
        register_inline_builder("kill", _kill_inline_builder)
        register_inline_builder("logs", _logs_inline_builder)
        register_inline_builder("context", _context_inline_builder)
        register_inline_builder("context_error", _context_error_inline_builder)
        register_action("health_refresh", _health_refresh_action)
        register_action("logs_errors", _logs_errors_action)
        register_action("logs_50", _logs_50_action)
    except Exception as exc:
        logger.warning("Inline builder registration failed: %s", exc)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.health$"))
    async def health_cmd(event):
        if not is_owner(event, owner_id):
            return
        helper = get_client()
        if helper is None:
            try:
                snap = health.snapshot()
                report = _build_health_report(snap)
                await _safe_edit(event, report)
                diagnostics.record_event("health", "snapshot", 0, "SUCCESS")
            except Exception as exc:
                logger.warning("health_cmd failed: %s", exc)
            return
        try:
            await event.delete()
            await send_inline_panel(client, event.chat_id, "health")
            diagnostics.record_event("health", "snapshot", 0, "SUCCESS")
        except Exception as exc:
            logger.warning("health inline send failed: %s", exc)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.kill$"))
    async def kill_cmd(event):
        if not is_owner(event, owner_id):
            return
        helper = get_client()
        if helper is None:
            try:
                await event.edit("⏳ Collecting diagnostics...")
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
            return
        try:
            await event.delete()
            await send_inline_panel(client, event.chat_id, "kill")
            diagnostics.record_event("diagnostics", "kill", 0, "SUCCESS")
        except Exception as exc:
            logger.warning("kill inline send failed: %s", exc)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.logs(?:\s+(.+))?$"))
    async def logs_cmd(event):
        if not is_owner(event, owner_id):
            return

        arg = (event.pattern_match.group(1) or "").strip()
        query = "logs"
        if arg:
            if arg.lower() == "errors":
                query = "logs:errors"
            elif arg.lower().startswith("module "):
                query = "logs"
            elif arg.isdigit():
                query = f"logs:{arg}"

        helper = get_client()
        if helper is None:
            limit = 20
            errors_only = False
            if arg:
                if arg.lower() == "errors":
                    errors_only = True
                elif arg.lower().startswith("module "):
                    pass
                elif arg.isdigit():
                    limit = min(int(arg), 500)
            try:
                events_list = diagnostics.filter_events(
                    limit=limit, errors_only=errors_only
                )
                text = diagnostics.format_events(events_list)
                await _safe_edit(event, text)
            except Exception as exc:
                logger.warning("logs_cmd failed: %s", exc)
            return

        try:
            await event.delete()
            await send_inline_panel(client, event.chat_id, query)
        except Exception as exc:
            logger.warning("logs inline send failed: %s", exc)
