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
    register_input,
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
            "`.save` — Save replied message",
        ],
    ),
    (
        "Retrieve",
        [
            "**Retrieve**\n",
            "`.retrieve` · `.r` · `.files` — Browse saved items",
            "`.preview <code>` — Show metadata",
            "`.send <code>` — Forward asset here",
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
    if len(cats) % 2 == 1:
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


def _retrieve_body() -> str:
    return (
        "**Retrieve**\n\n"
        "Browse saved items in a file-manager view.\n"
        "Tap any item to preview, retrieve, rename, move, or delete."
    )


def _build_retrieve_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("📋 Saved Items", "panel:retrieve_saved")
    builder.add_row("🔍 Retrieve by Code", "panel:retrieve_code")
    return builder.build()


def _build_bio_help_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("🔧 Variables", "panel:biohelp:vars")
    builder.add_row("📋 Commands", "panel:biohelp:cmds")
    builder.add_row("🏗 Template Builder", "panel:biohelp:builder")
    return builder.build()


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
                if idx == 1:
                    return _HELP_CATEGORIES[1][0], _retrieve_body(), _build_retrieve_buttons()
                if idx == 2:
                    return "Bio Engine", "Choose a section:", _build_bio_help_buttons()
                _, lines = _HELP_CATEGORIES[idx]
                body = "\n".join(lines)
                return _HELP_CATEGORIES[idx][0], body, []
    return "LifeOS Command Center", "", _build_main_menu_buttons()


async def _help_inline_builder(event, extra: str) -> list:
    if extra.startswith("cat:0"):
        return [render("General", _general_body(), _build_general_buttons())]
    if extra.startswith("cat:1"):
        return [render("Retrieve", _retrieve_body(), _build_retrieve_buttons())]
    if extra.startswith("cat:2"):
        return [render("Bio Engine", "Choose a section:", _build_bio_help_buttons())]
    return [render("LifeOS Command Center", "", _build_main_menu_buttons())]


def _build_settings_body() -> str:
    from backend.services import settings_service
    ac = settings_service.is_auto_close_enabled()
    acd = settings_service.auto_close_delay()
    mds = settings_service.max_deep_save_mb()
    dbs = settings_service.delete_batch_size()
    lrd = settings_service.log_retention_days()
    pts = settings_service.panel_timeout_seconds()
    amp = settings_service.is_allow_multiple_panels()
    rep = settings_service.is_reuse_existing_panel()
    lang = settings_service.language()
    de = settings_service.is_diagnostics_enabled()
    dbg = settings_service.is_debug_callbacks()
    oo = settings_service.is_owner_only()
    uss = settings_service.update_stale_seconds()
    return (
        "**⚙️ LifeOS Settings**\n\n"
        f"Auto Close: `{'ON' if ac else 'OFF'}`\n"
        f"Auto-Close Delay: `{acd}s`\n"
        f"Max Deep Save: `{mds} MB`\n"
        f"Delete Batch Size: `{dbs}`\n"
        f"Log Retention: `{lrd} days`\n"
        f"Panel Timeout: `{pts}s`\n"
        f"Allow Multiple Panels: `{'ON' if amp else 'OFF'}`\n"
        f"Reuse Existing Panel: `{'ON' if rep else 'OFF'}`\n"
        f"Language: `{lang}`\n"
        f"Diagnostics: `{'ON' if de else 'OFF'}`\n"
        f"Debug Callbacks: `{'ON' if dbg else 'OFF'}`\n"
        f"Owner Only: `{'ON' if oo else 'OFF'}`\n"
        f"Update Stale Threshold: `{uss}s`"
    )


def _build_settings_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("Toggle Auto Close", "action:settings_toggle_autoclose")
    builder.add_row("Toggle Diagnostics", "action:settings_toggle_diagnostics")
    builder.add_row("Toggle Debug Callbacks", "action:settings_toggle_debug_callbacks")
    builder.add_row("Toggle Owner Only", "action:settings_toggle_owner_only")
    builder.add_row("Toggle Multiple Panels", "action:settings_toggle_multiple_panels")
    builder.add_row("Toggle Reuse Panel", "action:settings_toggle_reuse_panel")
    builder.add_row("Set Auto-Close Delay", "input:settings:auto_close_delay")
    builder.add_row("Set Max Deep Save (MB)", "input:settings:max_deep_save_mb")
    builder.add_row("Set Delete Batch Size", "input:settings:delete_batch_size")
    builder.add_row("Set Log Retention (days)", "input:settings:log_retention_days")
    builder.add_row("Set Panel Timeout (s)", "input:settings:panel_timeout_seconds")
    builder.add_row("Set Language", "input:settings:language")
    builder.add_row("Set Update Stale (s)", "input:settings:update_stale_seconds")
    return builder.build()


async def _settings_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    return "Settings", _build_settings_body(), _build_settings_buttons()


async def _settings_inline_builder(event, extra: str) -> list:
    return [render("Settings", _build_settings_body(), _build_settings_buttons())]


async def _settings_toggle_autoclose_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services import settings_service
    settings_service.toggle_auto_close()
    return "Settings", _build_settings_body(), _build_settings_buttons()


async def _settings_toggle_diagnostics_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services import settings_service
    settings_service.set_diagnostics_enabled(not settings_service.is_diagnostics_enabled())
    return "Settings", _build_settings_body(), _build_settings_buttons()


async def _settings_toggle_debug_callbacks_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services import settings_service
    settings_service.set_debug_callbacks(not settings_service.is_debug_callbacks())
    return "Settings", _build_settings_body(), _build_settings_buttons()


async def _settings_toggle_owner_only_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services import settings_service
    settings_service.set_owner_only(not settings_service.is_owner_only())
    return "Settings", _build_settings_body(), _build_settings_buttons()


async def _settings_toggle_multiple_panels_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services import settings_service
    settings_service.set_allow_multiple_panels(not settings_service.is_allow_multiple_panels())
    return "Settings", _build_settings_body(), _build_settings_buttons()


async def _settings_toggle_reuse_panel_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services import settings_service
    settings_service.set_reuse_existing_panel(not settings_service.is_reuse_existing_panel())
    return "Settings", _build_settings_body(), _build_settings_buttons()


async def _general_ping_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    return "PONG", "", _build_general_buttons()


async def _general_id_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.helper.target_context import get_target

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

    ctx = get_target(owner_id)
    if ctx and ctx.kind == "reply" and ctx.reply_chat_id and ctx.reply_msg_id:
        body += (
            f"\n\n**Replied Chat ID:**\n`{ctx.reply_chat_id}`\n\n"
            f"**Replied Message ID:**\n`{ctx.reply_msg_id}`"
        )

    return "Chat & Message IDs", body, _build_general_buttons()


async def _general_health_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    snap = health.snapshot()
    report = _build_health_report(snap)
    builder = InlinePanelBuilder()
    builder.add_row("Refresh", "action:health_refresh")
    return "Health Dashboard", report, builder.build()


async def _settings_auto_close_delay_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.services import settings_service
    from backend.helper.inline_engine import _self_client
    text = text.strip()
    if not text.isdigit():
        result = "⚠️ Please enter a number between 5 and 3600."
    else:
        ok = settings_service.set_auto_close_delay(int(text))
        result = f"✅ Auto-close delay set to `{text}s`" if ok else "⚠️ Value must be between 5 and 3600."
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("settings auto_close_delay inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _settings_max_deep_save_mb_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.services import settings_service
    from backend.helper.inline_engine import _self_client
    text = text.strip()
    if not text.isdigit():
        result = "⚠️ Please enter a number between 1 and 500."
    else:
        ok = settings_service.set_max_deep_save_mb(int(text))
        result = f"✅ Max deep save set to `{text} MB`" if ok else "⚠️ Value must be between 1 and 500."
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("settings max_deep_save_mb inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _settings_delete_batch_size_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.services import settings_service
    from backend.helper.inline_engine import _self_client
    text = text.strip()
    if not text.isdigit():
        result = "⚠️ Please enter a number between 1 and 1000."
    else:
        ok = settings_service.set_delete_batch_size(int(text))
        result = f"✅ Delete batch size set to `{text}`" if ok else "⚠️ Value must be between 1 and 1000."
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("settings delete_batch_size inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _settings_log_cleanup_days_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.services import settings_service
    from backend.helper.inline_engine import _self_client
    text = text.strip()
    if not text.isdigit():
        result = "⚠️ Please enter a number between 1 and 365."
    else:
        ok = settings_service.set_log_retention_days(int(text))
        result = f"✅ Log retention set to `{text} days`" if ok else "⚠️ Value must be between 1 and 365."
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("settings log_retention_days inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _settings_panel_timeout_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.services import settings_service
    from backend.helper.inline_engine import _self_client
    text = text.strip()
    if not text.isdigit():
        result = "⚠️ Please enter a number between 30 and 86400."
    else:
        ok = settings_service.set_panel_timeout_seconds(int(text))
        result = f"✅ Panel timeout set to `{text}s`" if ok else "⚠️ Value must be between 30 and 86400."
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("settings panel_timeout inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _settings_language_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.services import settings_service
    from backend.helper.inline_engine import _self_client
    text = text.strip()
    if not text:
        result = "⚠️ Language cannot be empty."
    else:
        ok = settings_service.set_language(text)
        result = f"✅ Language set to `{text}`" if ok else "⚠️ Invalid language."
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("settings language inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _settings_update_stale_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.services import settings_service
    from backend.helper.inline_engine import _self_client
    text = text.strip()
    if not text.isdigit():
        result = "⚠️ Please enter a number between 60 and 3600."
    else:
        ok = settings_service.set_update_stale_seconds(int(text))
        result = f"✅ Update stale threshold set to `{text}s`" if ok else "⚠️ Value must be between 60 and 3600."
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("settings update_stale inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


def _register_help_panel() -> None:
    register_panel("help", _help_panel_handler)
    register_panel("settings", _settings_panel_handler)
    register_inline_builder("help", _help_inline_builder)
    register_inline_builder("settings", _settings_inline_builder)
    register_action("settings_toggle_autoclose", _settings_toggle_autoclose_action)
    register_action("settings_toggle_diagnostics", _settings_toggle_diagnostics_action)
    register_action("settings_toggle_debug_callbacks", _settings_toggle_debug_callbacks_action)
    register_action("settings_toggle_owner_only", _settings_toggle_owner_only_action)
    register_action("settings_toggle_multiple_panels", _settings_toggle_multiple_panels_action)
    register_action("settings_toggle_reuse_panel", _settings_toggle_reuse_panel_action)
    register_action("general_ping", _general_ping_action)
    register_action("general_id", _general_id_action)
    register_action("general_health", _general_health_action)
    register_input("settings", "auto_close_delay", {
        "handler": _settings_auto_close_delay_handler,
        "prompt": "**Auto-Close Delay**\n\nEnter the delay in seconds (5-3600):\n\n_Reply with the number below._",
    })
    register_input("settings", "max_deep_save_mb", {
        "handler": _settings_max_deep_save_mb_handler,
        "prompt": "**Max Deep Save Size**\n\nEnter the maximum file size in MB (1-500):\n\n_Reply with the number below._",
    })
    register_input("settings", "delete_batch_size", {
        "handler": _settings_delete_batch_size_handler,
        "prompt": "**Delete Batch Size**\n\nEnter the batch size for message deletion (1-1000):\n\n_Reply with the number below._",
    })
    register_input("settings", "log_retention_days", {
        "handler": _settings_log_cleanup_days_handler,
        "prompt": "**Log Retention**\n\nEnter the number of days to retain logs (1-365):\n\n_Reply with the number below._",
    })
    register_input("settings", "panel_timeout_seconds", {
        "handler": _settings_panel_timeout_handler,
        "prompt": "**Panel Timeout**\n\nEnter the panel timeout in seconds (30-86400):\n\n_Reply with the number below._",
    })
    register_input("settings", "language", {
        "handler": _settings_language_handler,
        "prompt": "**Language**\n\nEnter the language code (e.g. en, fa):\n\n_Reply with the language code below._",
    })
    register_input("settings", "update_stale_seconds", {
        "handler": _settings_update_stale_handler,
        "prompt": "**Update Stale Threshold**\n\nEnter the threshold in seconds (60-3600):\n\n_Reply with the number below._",
    })


async def _context_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id

    owner_id = _owner_id
    ctx = get_target(owner_id)

    has_target = ctx is not None and ctx.kind == "reply" and ctx.reply_chat_id and ctx.reply_msg_id

    builder = InlinePanelBuilder()
    if has_target:
        builder.add_row("📦 Save", "panel:save")
    else:
        builder.add_row("📦 Save (reply required)", "panel:_nav:noop")
    builder.add_row("🗑 Delete", "panel:del")

    if has_target:
        body = f"**Chat:** `{ctx.reply_chat_id}`\n**Message:** `{ctx.reply_msg_id}`\n\nChoose an action:"
    else:
        body = "Choose an action:"

    return "Context Panel", body, builder.build()


async def _context_inline_builder(event, extra: str) -> list:
    from backend.helper.inline_engine import _owner_id

    owner_id = _owner_id
    ctx = get_target(owner_id)

    has_target = ctx is not None and ctx.kind == "reply" and ctx.reply_chat_id and ctx.reply_msg_id

    builder = InlinePanelBuilder()
    if has_target:
        builder.add_row("📦 Save", "panel:save")
    else:
        builder.add_row("📦 Save (reply required)", "panel:_nav:noop")
    builder.add_row("🗑 Delete", "panel:del")

    if has_target:
        body = f"**Chat:** `{ctx.reply_chat_id}`\n**Message:** `{ctx.reply_msg_id}`\n\nChoose an action:"
    else:
        body = "Choose an action:"

    return [render("Context Panel", body, builder.build())]


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


async def _health_refresh_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
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


async def _logs_errors_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    events_list = diagnostics.filter_events(limit=20, errors_only=True)
    text = diagnostics.format_events(events_list)
    builder = InlinePanelBuilder()
    builder.add_row("Last 50", "action:logs_50")
    return "Event Log", text, builder.build()


async def _logs_50_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
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
                lines.append(f"**Reply Chat ID:** `{reply.chat_id}`")
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

        helper = get_client()
        if helper is None:
            await event.edit("⚠️ Inline mode requires the helper bot (BOT_TOKEN).")
            return

        reply = await event.message.get_reply_message()
        if reply:
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
        register_panel("health", _health_panel_handler)
        register_panel("logs", _logs_panel_handler)
        register_inline_builder("health", _health_inline_builder)
        register_inline_builder("kill", _kill_inline_builder)
        register_inline_builder("logs", _logs_inline_builder)
        register_inline_builder("context", _context_inline_builder)
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
        from backend.services import settings_service
        if not settings_service.is_diagnostics_enabled():
            await event.edit("⚠️ Diagnostics are disabled. Enable them in Settings.")
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
        from backend.services import settings_service
        if not settings_service.is_diagnostics_enabled():
            await event.edit("⚠️ Diagnostics are disabled. Enable them in Settings.")
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
