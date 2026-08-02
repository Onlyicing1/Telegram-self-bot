"""
.ping    — Edit trigger with PONG (zero-spam policy).
.id      — Chat ID + Message ID of the current context.
.panel   — Context panel for the replied message.
.health  — Full health dashboard (inline panel).
.menu    — Mother Panel — the central navigation root of LifeOS.
Falls back to plain-text edit-in-place when the helper bot is not available.
"""
import asyncio
import logging
import os
import resource
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telethon import events

from backend import health
from backend import diagnostics as _diag_runtime
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


def _build_general_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("🏓 Ping", "action:general_ping")
    builder.add_row("🆔 Chat & Msg IDs", "action:general_id")
    builder.add_row("🩺 Health Dashboard", "action:general_health")
    return builder.build()


async def _general_body() -> str:
    return (
        "**General**\n\n"
        "Tap a button to execute instantly."
    )


def _build_menu_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_buttons(
        ("📥 Save", "panel:save"),
        ("🗑 Delete", "panel:del"),
    )
    builder.add_buttons(
        ("👤 Profile", "panel:profile"),
        ("🗄 Database", "panel:db"),
    )
    builder.add_buttons(
        ("🔧 General", "panel:general"),
        ("⚙️ Settings", "panel:settings"),
    )
    return builder.build()