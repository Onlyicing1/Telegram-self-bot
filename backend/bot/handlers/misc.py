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
        "Tap a button to preview, send, or browse saved items."
    )


def _build_retrieve_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("👁 Preview Item", "panel:preview")
    builder.add_row("📤 Send Item", "panel:send")
    builder.add_row("📋 Saved Items", "panel:retrieve_saved")
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
                _, lines = _HELP_CATEGORIES[idx]
                body = "\n".join(lines)
                return _HELP_CATEGORIES[idx][0], body, []
    return "LifeOS Command Center", "", _build_main_menu_buttons()


async def _help_inline_builder(event, extra: str) -> list:
    if extra.startswith("cat:0"):
        return [render("General", _general_body(), _build_general_buttons())]
    if extra.startswith("cat:1"):
        return [render("Retrieve", _retrieve_body(), _build_retrieve_buttons())]
    return [render("LifeOS Command Center", "", _build_main_menu_buttons())]
