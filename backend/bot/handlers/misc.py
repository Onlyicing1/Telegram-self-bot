"""
.menu    — Mother Panel — the central navigation root of LifeOS.
Falls back to plain-text edit-in-place when the helper bot is not available.
"""
import logging
import os
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from telethon import events

from backend import health
from backend import diagnostics as _diag_runtime
from backend.bot.handlers.guard import is_owner
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    register_action,
    register_input,
    send_inline_panel,
    render,
    render_edit,
    get_target,
    toggle_auto_close,
)
from backend.helper.client import get_client
from backend.helper.rpc_timeout import rpc_await

_RPC_T = 30.0


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
    builder.add_row("Ping", "action:general_ping")
    builder.add_row("Chat & Msg IDs", "action:general_id")
    builder.add_row("Health Dashboard", "action:general_health")
    return builder.build()


async def _general_body() -> str:
    return (
        "**General**\n\n"
        "Tap a button to execute instantly."
    )


def _build_menu_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_buttons(
        ("Save", "panel:save"),
        ("Delete", "panel:del"),
    )
    builder.add_buttons(
        ("Profile", "panel:profile"),
        ("Database", "panel:db"),
    )
    builder.add_buttons(
        ("General", "panel:general"),
        ("Settings", "panel:settings"),
    )
    builder.add_row("🧠 AI", "panel:ai")
    builder.add_row("👻 Ghost Seen", "panel:ghost_seen")
    return builder.build()


async def _menu_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    return "LifeOS", "Choose a category:", _build_menu_buttons()


async def _menu_inline_builder(event, extra: str) -> list:
    return [render("LifeOS", "Choose a category:", _build_menu_buttons())]


async def _general_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    return "General", await _general_body(), _build_general_buttons()


async def _general_inline_builder(event, extra: str) -> list:
    return [render("General", await _general_body(), _build_general_buttons())]


async def _profile_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    builder = InlinePanelBuilder()
    builder.add_row("Bio", "panel:bio")
    builder.add_row("Username", "panel:username")
    return "Profile", "Choose a section:", builder.build()


async def _profile_inline_builder(event, extra: str) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("Bio", "panel:bio")
    builder.add_row("Username", "panel:username")
    return [render("Profile", "Choose a section:", builder.build())]


async def _build_settings_body() -> str:
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
    dbg = settings_service.is_debug_callbacks()
    oo = settings_service.is_owner_only()

    def _state(on: bool) -> str:
        return "ON" if on else "OFF"

    lines = [
        f"**Auto-close:** {_state(ac)} ({acd}s)",
        f"**Max deep save:** {mds} MB",
        f"**Delete batch:** {dbs}",
        f"**Log retention:** {lrd} days",
        f"**Panel timeout:** {pts}s",
        f"**Multiple panels:** {_state(amp)}",
        f"**Reuse panel:** {_state(rep)}",
        f"**Language:** {lang}",
        f"**Debug callbacks:** {_state(dbg)}",
        f"**Owner only:** {_state(oo)}",
    ]
    return "\n".join(lines)


_FONT_PAGE_SIZE = 8
_font_page: int = 0


def _font_panel_page(current_key: str) -> tuple[str, str, list]:
    from backend.helper.font_style import FONT_KEYS, _FONT_BY_KEY

    pages = [FONT_KEYS[i:i + _FONT_PAGE_SIZE] for i in range(0, len(FONT_KEYS), _FONT_PAGE_SIZE)]
    page = min(max(_font_page, 0), len(pages) - 1) if pages else 0
    builder = InlinePanelBuilder()
    for key in pages[page] if pages else ():
        font = _FONT_BY_KEY[key]
        mark = "✓" if key == current_key else "·"
        builder.add_row(f"{mark} {font.label}", f"action:font_set:{key}")
    if len(pages) > 1:
        builder.add_buttons(
            ("◀ Prev", "action:font_page:prev"),
            (f"{page + 1}/{len(pages)}", "panel:_nav:noop"),
            ("Next ▶", "action:font_page:next"),
        )
    label = _FONT_BY_KEY[current_key].label
    body = (
        f"Current: **{label}**\n"
        "Applies to all Glass UI panels and buttons.\n\n"
        "English/Latin text is styled; Persian renders normally; "
        "IDs, code spans and URLs are never restyled."
    )
    return "Font", body, builder.build()


async def _font_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.helper.font_style import normalize_font_key
    from backend.services import settings_service
    return _font_panel_page(normalize_font_key(settings_service.dashboard_font()))


async def _font_set_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.font_style import is_valid_font, normalize_font_key
    from backend.services import settings_service

    key = extra.strip()
    if not is_valid_font(key):
        return "Font", "Invalid selection — keeping the previous font.", []
    settings_service.set_dashboard_font(key)
    return _font_panel_page(key)


async def _font_page_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    global _font_page
    from backend.helper.font_style import FONT_KEYS, normalize_font_key
    from backend.services import settings_service

    total = max(1, (len(FONT_KEYS) + _FONT_PAGE_SIZE - 1) // _FONT_PAGE_SIZE)
    if extra == "prev":
        _font_page = max(0, _font_page - 1)
    elif extra == "next":
        _font_page = min(total - 1, _font_page + 1)
    return _font_panel_page(normalize_font_key(settings_service.dashboard_font()))


async def _settings_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    builder = InlinePanelBuilder()
    builder.add_row("Toggle Auto-close", "action:settings_toggle_autoclose")
    builder.add_row("Toggle Debug Callbacks", "action:settings_toggle_debug_callbacks")
    builder.add_row("Toggle Owner Only", "action:settings_toggle_owner_only")
    builder.add_row("Toggle Multiple Panels", "action:settings_toggle_multiple_panels")
    builder.add_row("Toggle Reuse Panel", "action:settings_toggle_reuse_panel")
    builder.add_row("Set Auto-close Delay", "input:settings:auto_close_delay")
    builder.add_row("Set Max Deep Save", "input:settings:max_deep_save_mb")
    builder.add_row("Set Delete Batch Size", "input:settings:delete_batch_size")
    builder.add_row("Set Log Retention", "input:settings:log_retention_days")
    builder.add_row("Set Panel Timeout", "input:settings:panel_timeout_seconds")
    builder.add_row("🔤 Font", "panel:font")
    return "Settings", await _build_settings_body(), builder.build()


async def _settings_inline_builder(event, extra: str) -> list:
    return [render("Settings", await _build_settings_body(), [])]


async def _settings_toggle_autoclose_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    new_val = toggle_auto_close()
    return "Settings", f"Auto-close is now {'ON' if new_val else 'OFF'}", []


async def _settings_toggle_debug_callbacks_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services import settings_service
    new_val = settings_service.toggle_debug_callbacks()
    return "Settings", f"Debug callbacks is now {'ON' if new_val else 'OFF'}", []


async def _settings_toggle_owner_only_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services import settings_service
    new_val = settings_service.toggle_owner_only()
    return "Settings", f"Owner only is now {'ON' if new_val else 'OFF'}", []


async def _settings_toggle_multiple_panels_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services import settings_service
    new_val = settings_service.toggle_allow_multiple_panels()
    return "Settings", f"Multiple panels is now {'ON' if new_val else 'OFF'}", []


async def _settings_toggle_reuse_panel_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.services import settings_service
    new_val = settings_service.toggle_reuse_existing_panel()
    return "Settings", f"Reuse panel is now {'ON' if new_val else 'OFF'}", []


async def _settings_auto_close_delay_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.services import settings_service
    from backend.helper.inline_engine import _self_client
    text = text.strip()
    if not text.isdigit():
        result = "Please enter a number between 5 and 3600."
    else:
        ok = settings_service.set_auto_close_delay(int(text))
        result = f"Auto-close delay set to {text}s" if ok else "Value must be between 5 and 3600."
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
        result = "Please enter a number between 1 and 500."
    else:
        ok = settings_service.set_max_deep_save_mb(int(text))
        result = f"Max deep save set to {text} MB" if ok else "Value must be between 1 and 500."
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
        result = "Please enter a number between 1 and 100."
    else:
        ok = settings_service.set_delete_batch_size(int(text))
        result = f"Delete batch size set to {text}" if ok else "Value must be between 1 and 100."
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


async def _settings_log_retention_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.services import settings_service
    from backend.helper.inline_engine import _self_client
    text = text.strip()
    if not text.isdigit():
        result = "Please enter a number between 1 and 365."
    else:
        ok = settings_service.set_log_retention_days(int(text))
        result = f"Log retention set to {text} days" if ok else "Value must be between 1 and 365."
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("settings log_retention inline edit failed: %s", exc)
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
        result = "Please enter a number between 30 and 3600."
    else:
        ok = settings_service.set_panel_timeout_seconds(int(text))
        result = f"Panel timeout set to {text}s" if ok else "Value must be between 30 and 3600."
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
        result = "Language cannot be empty."
    else:
        ok = settings_service.set_language(text)
        result = f"Language set to {text}" if ok else "Invalid language."
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


def _register_panels() -> None:
    register_panel("settings", _settings_panel_handler, parent="menu", title="Settings")
    register_inline_builder("settings", _settings_inline_builder)
    register_panel("general", _general_panel_handler, parent="menu", title="General")
    register_inline_builder("general", _general_inline_builder)
    register_panel("font", _font_panel_handler, parent="settings", title="Font")
    register_action("font_set", _font_set_action)
    register_action("font_page", _font_page_action)
    register_action("settings_toggle_autoclose", _settings_toggle_autoclose_action)
    register_action("settings_toggle_debug_callbacks", _settings_toggle_debug_callbacks_action)
    register_action("settings_toggle_owner_only", _settings_toggle_owner_only_action)
    register_action("settings_toggle_multiple_panels", _settings_toggle_multiple_panels_action)
    register_action("settings_toggle_reuse_panel", _settings_toggle_reuse_panel_action)
    register_action("general_ping", _general_ping_action)
    register_action("general_id", _general_id_action)
    register_action("general_health", _general_health_action)
    register_input("settings", "auto_close_delay", {
        "handler": _settings_auto_close_delay_handler,
        "prompt": "**Auto-close Delay**\n\nEnter delay in seconds (5-3600):\n\n_Reply below._",
    })
    register_input("settings", "max_deep_save_mb", {
        "handler": _settings_max_deep_save_mb_handler,
        "prompt": "**Max Deep Save**\n\nEnter max size in MB (1-500):\n\n_Reply below._",
    })
    register_input("settings", "delete_batch_size", {
        "handler": _settings_delete_batch_size_handler,
        "prompt": "**Delete Batch Size**\n\nEnter batch size (1-100):\n\n_Reply below._",
    })
    register_input("settings", "log_retention_days", {
        "handler": _settings_log_retention_handler,
        "prompt": "**Log Retention**\n\nEnter retention in days (1-365):\n\n_Reply below._",
    })
    register_input("settings", "panel_timeout_seconds", {
        "handler": _settings_panel_timeout_handler,
        "prompt": "**Panel Timeout**\n\nEnter timeout in seconds (30-3600):\n\n_Reply below._",
    })


async def _general_ping_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    return "General", "PONG", _build_general_buttons()


async def _general_id_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _self_client
    client = _self_client
    body_lines = []
    try:
        event_chat_id = chat_id or getattr(event, "chat_id", 0) or 0
        event_msg_id = getattr(event, "message_id", 0) or 0

        body_lines.append("**Current Chat**")
        body_lines.append(f"Chat ID: `{event_chat_id}`")

        chat_type = "Unknown"
        chat_name = ""
        if client and event_chat_id:
            try:
                entity = await client.get_entity(event_chat_id)
                if hasattr(entity, "broadcast"):
                    chat_type = "Channel" if entity.broadcast else "Group"
                elif hasattr(entity, "megagroup") and entity.megagroup:
                    chat_type = "Supergroup"
                elif hasattr(entity, "first_name"):
                    chat_type = "Private"
                name_parts = []
                for attr in ("title", "first_name"):
                    val = getattr(entity, attr, "")
                    if val:
                        name_parts.append(val)
                last = getattr(entity, "last_name", "")
                if last:
                    name_parts.append(last)
                chat_name = " ".join(name_parts).strip()
            except Exception:
                pass
        body_lines.append(f"Type: {chat_type}")
        if chat_name:
            body_lines.append(f"Name: {chat_name}")
        body_lines.append("")

        body_lines.append("**Current Message**")
        if event_msg_id:
            body_lines.append(f"Message ID: `{event_msg_id}`")
        else:
            body_lines.append("Message ID: Unavailable")
        body_lines.append(f"Chat ID: `{event_chat_id}`")

        if client and event_chat_id and event_msg_id:
            try:
                msg = await client.get_messages(event_chat_id, ids=event_msg_id)
                if msg is not None and msg.fwd_from:
                    fwd = msg.fwd_from
                    fwd_chat_id = getattr(fwd, "from_id", None)
                    fwd_msg_id = getattr(fwd, "channel_post", None) or getattr(fwd, "msg_id", None)
                    fwd_name = getattr(fwd, "from_name", "") or ""
                    body_lines.append("")
                    body_lines.append("**Forward Source**")
                    has_fwd_info = False
                    if fwd_name:
                        body_lines.append(f"From: {fwd_name}")
                        has_fwd_info = True
                    if fwd_chat_id:
                        fwd_cid = getattr(fwd_chat_id, "channel_id", None) or getattr(fwd_chat_id, "user_id", None) or fwd_chat_id
                        body_lines.append(f"Source Chat ID: `{fwd_cid}`")
                        has_fwd_info = True
                    if fwd_msg_id:
                        body_lines.append(f"Source Message ID: `{fwd_msg_id}`")
                        has_fwd_info = True
                    if not has_fwd_info:
                        body_lines.append("Unavailable")
            except Exception:
                pass
        body_lines.append("")

        body_lines.append("**Reply Context**")
        reply_to_msg_id = getattr(event, "_reply_to_msg_id", None)
        if reply_to_msg_id is None:
            cq_msg = getattr(event, "message", None)
            if cq_msg is not None:
                reply_to_msg_id = getattr(cq_msg, "reply_to_msg_id", None)
        if not reply_to_msg_id:
            orig = getattr(event, "original_update", None)
            if orig is not None:
                reply_to_msg_id = getattr(orig, "msg_id", None)
        if reply_to_msg_id:
            body_lines.append(f"Reply To Msg ID: `{reply_to_msg_id}`")
            if client:
                try:
                    reply = await client.get_messages(event_chat_id, ids=reply_to_msg_id)
                    if reply:
                        body_lines.append(f"Reply Chat ID: `{reply.chat_id}`")
                        body_lines.append(f"Reply Sender ID: `{reply.sender_id}`")
                        if reply.text:
                            preview = reply.text[:100].replace("\n", " ")
                            body_lines.append(f"Preview: {preview}")
                        if reply.fwd_from:
                            body_lines.append("_(reply is a forwarded message)_")
                except Exception:
                    body_lines.append("_(reply message unavailable)_")
        else:
            body_lines.append("No reply context.")
    except Exception as exc:
        body_lines.append(f"Error: {exc}")
    return "Chat & Message IDs", "\n".join(body_lines), _build_general_buttons()


async def _general_health_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    snap = health.snapshot()
    report = _build_health_report(snap)
    builder = InlinePanelBuilder()
    builder.add_row("Refresh", "action:health_refresh")
    return "Health Dashboard", report, builder.build()


async def _health_refresh_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    snap = health.snapshot()
    report = _build_health_report(snap)
    builder = InlinePanelBuilder()
    builder.add_row("Refresh", "action:health_refresh")
    return "Health Dashboard", report, builder.build()


async def _context_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _self_client
    from backend.helper.target_context import get_target
    client = _self_client
    owner_id = 0
    try:
        from backend.helper.inline_engine import _owner_id
        owner_id = _owner_id
    except Exception:
        pass
    ctx = get_target(owner_id)
    if ctx is None or ctx.kind != "reply":
        return "Context panel", "No replied message context.", []
    body_lines = []
    try:
        reply = await client.get_messages(ctx.reply_chat_id, ids=ctx.reply_msg_id)
        if reply is None:
            return "Context Panel", "The replied message no longer exists.", []
        body_lines.append(f"**Chat ID:** `{ctx.reply_chat_id}`")
        body_lines.append(f"**Msg ID:** `{ctx.reply_msg_id}`")
        body_lines.append(f"**Sender ID:** `{reply.sender_id}`")
        sender = await reply.get_sender()
        if sender:
            name = " ".join(p for p in [getattr(sender, "first_name", ""), getattr(sender, "last_name", "")] if p).strip()
            if name:
                body_lines.append(f"**Sender:** {name}")
        body_lines.append(f"**Date:** {reply.date.strftime('%Y-%m-%d %H:%M') if reply.date else 'N/A'}")
        if reply.text:
            preview = reply.text[:200].replace("\n", " ")
            body_lines.append(f"**Text:** {preview}")
        has_media = reply.media is not None
        body_lines.append(f"**Has media:** {'Yes' if has_media else 'No'}")
        if has_media:
            from backend.services.save_service import detect_media_type, extract_file_name
            media_type = detect_media_type(getattr(reply.media, "document", None) and getattr(reply.media.document, "mime_type", "") or "")
            body_lines.append(f"**Media type:** {media_type}")
            file_name = extract_file_name(reply.media)
            if file_name:
                body_lines.append(f"**File:** `{file_name}`")
    except Exception as exc:
        body_lines.append(f"Error: {exc}")
    builder = InlinePanelBuilder()
    builder.add_row("⬇️ Deep Save", "action:save_reply")
    return "Context Panel", "\n".join(body_lines), builder.build()


async def _context_inline_builder(event, extra: str) -> list:
    result = await _context_panel_handler(event, extra)
    if result is None:
        return [render("Context Panel", "No context.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


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


def _indicator(ok: bool) -> str:
    return "OK" if ok else "FAIL"


def _format_age(ts: float) -> str:
    if not ts:
        return "never"
    import time as _time
    age = _time.time() - ts
    if age < 60:
        return f"{int(age)}s ago"
    if age < 3600:
        return f"{int(age / 60)}m ago"
    return f"{int(age / 3600)}h ago"


def _build_health_report(snap: dict) -> str:
    lines = ["**Health Dashboard**\n"]
    telegram_ok = snap.get("telegram_connected", False)
    lines.append(f"{_indicator(telegram_ok)} **Telegram**: {'Connected' if telegram_ok else 'Disconnected'}")
    lines.append(f"   Last event: {_format_age(snap.get('last_telethon_event', 0))}")
    lines.append(f"   Last update: {_format_age(snap.get('last_update', 0))}")
    lines.append(f"   Last dispatch: {_format_age(snap.get('last_event_dispatch', 0))}")
    lines.append(f"   Last RPC: {_format_age(snap.get('last_rpc', 0))}")
    lines.append(f"   Last callback: {_format_age(snap.get('last_callback', 0))}")
    lines.append(f"   Last heartbeat: {_format_age(snap.get('last_heartbeat', 0))}")
    lines.append("")
    supervisor_ok = snap.get("supervisor_running", False)
    lines.append(f"{_indicator(supervisor_ok)} **Supervisor**: {'Running' if supervisor_ok else 'Stopped'}")
    lines.append(f"   State: {snap.get('runtime_state', 'unknown')}")
    lines.append(f"   Recovery cooldown: {snap.get('recovery_cooldown', False)}")
    lines.append("")
    failsafe_ok = snap.get("failsafe_running", False)
    lines.append(f"{_indicator(failsafe_ok)} **Failsafe**: {'Running' if failsafe_ok else 'Stopped'}")
    lines.append("")
    keepalive_ok = snap.get("keepalive_running", False)
    lines.append(f"{_indicator(keepalive_ok)} **Keepalive**: {'Running' if keepalive_ok else 'Stopped'}")
    lines.append("")
    bio_ok = snap.get("bio_engine_running", False)
    lines.append(f"{_indicator(bio_ok)} **Bio Engine**: {'Running' if bio_ok else 'Stopped'}")
    lines.append("")
    username_ok = snap.get("username_engine_running", False)
    lines.append(f"{_indicator(username_ok)} **Username Engine**: {'Running' if username_ok else 'Stopped'}")
    lines.append("")
    panel_count = snap.get("active_panels", 0)
    session_count = snap.get("session_count", 0)
    timer_count = snap.get("timer_count", 0)
    lines.append(f"**Panels**: {panel_count} active, {session_count} sessions, {timer_count} timers")
    lines.append("")
    event_count = snap.get("event_count", 0)
    error_count = snap.get("error_count", 0)
    avg_ms = snap.get("avg_event_ms", 0)
    lines.append(f"**Events**: {event_count} total, {error_count} errors, avg {avg_ms:.1f}ms")
    lines.append("")
    mem_mb = snap.get("memory_mb", 0)
    lines.append(f"**Memory**: {mem_mb:.1f} MB")
    cpu_s = snap.get("cpu_seconds", None)
    if cpu_s is not None:
        lines.append(f"   CPU: {cpu_s:.2f}s")
    return "\n".join(lines)


async def _safe_edit(event, text: str) -> None:
    parts = _diag_runtime.split_message(text)
    for i, part in enumerate(parts):
        if i == 0:
            await event.edit(part)
        else:
            await event.reply(part)


def register(client, owner_id: int):
    try:
        _register_panels()
        register_panel("menu", _menu_panel_handler, parent="menu", title="LifeOS")
        register_inline_builder("menu", _menu_inline_builder)
        register_panel("profile", _profile_panel_handler, parent="menu", title="Profile")
        register_inline_builder("profile", _profile_inline_builder)
        register_panel("context", _context_panel_handler, parent="menu", title="Context Panel")
        register_panel("health", _health_panel_handler, parent="menu", title="Health Dashboard")
        register_inline_builder("health", _health_inline_builder)
        register_inline_builder("context", _context_inline_builder)
        register_action("health_refresh", _health_refresh_action)
    except Exception as exc:
        logger.warning("Inline builder registration failed: %s", exc)

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.menu$"))
    async def menu_cmd(event):
        if not is_owner(event, owner_id):
            return

        helper = get_client()
        if helper is None:
            text, _ = render_edit("LifeOS", "Choose a category:", _build_menu_buttons())
            await rpc_await(event.edit(text), timeout=_RPC_T, label="menu.no_helper_edit")
            return

        try:
            await rpc_await(event.delete(), timeout=_RPC_T, label="menu.delete")
            await rpc_await(send_inline_panel(client, event.chat_id, "menu"), timeout=_RPC_T, label="menu.send_inline")
        except Exception as exc:
            logger.warning("menu inline send failed: %s", exc)
            try:
                text, _ = render_edit("LifeOS", "Choose a category:", _build_menu_buttons())
                await rpc_await(event.edit(text), timeout=_RPC_T, label="menu.fallback_edit")
            except Exception:
                pass
