"""
Bio command handler.

Business logic lives in backend.services.bio_service.
This handler is only the Telethon wiring + panel rendering.

Inline Bio panels:
  panel:bio           — Main menu (all buttons)
  panel:bio:state     — Show State page
  panel:bio:text      — Set Text page (input prompt)
  panel:bio:mood      — Set Mood page (input prompt)

  panel:biohelp:vars       — Variable reference
  panel:biohelp:var:{tok}  — Single variable detail + copy
  panel:biohelp:cmds       — Command reference (no Set Template)
  panel:biohelp:cmd:{i}    — Single command detail
  panel:biohelp:builder    — Template Builder (insert variables)
  panel:biohelp:custom     — Custom Template mode (copy + reply)

Every page gets Back + Home + Close auto-added by _finalize_panel.
No page uses panel:_nav:noop in button data (that would suppress nav).
"""
import logging
import urllib.parse

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.db import client as db_client
from backend.services import bio_service
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    register_action,
    register_input,
    send_inline_panel,
    render,
)
from backend.helper.client import get_client

logger = logging.getLogger(__name__)


_BIO_VARS = [
    ("{time}", "Current time in HH:MM format", "14:30"),
    ("{mood}", "Current mood value", "😊"),
    ("{text}", "Custom freeform text", "Working"),
]

_BIO_CMDS = [
    ("Set Text", "Set the {text} variable value", ".bio text <text>", ".bio text Working hard"),
    ("Set Mood", "Set the {mood} variable value", ".bio mood <mood>", ".bio mood 😎"),
    ("Enable Sync", "Start the bio cron — auto-updates every minute", ".bio on", ".bio on"),
    ("Disable Sync", "Stop the bio cron", ".bio off", ".bio off"),
    ("Show State", "Inspect current bio state (template, mood, text, preview)", ".bio show", ".bio show"),
]

_DEFAULT_TEMPLATE = "🕒 {time} | 💭 {mood}"


def _encode_buffer(buf: str) -> str:
    return urllib.parse.quote(buf, safe="")


def _decode_buffer(encoded: str) -> str:
    if not encoded:
        return ""
    try:
        return urllib.parse.unquote(encoded)
    except Exception:
        return ""


def _render_preview(buf: str) -> str:
    if not buf:
        return "_(empty — tap variables to build)_"
    return f"`{buf}`"


# ── Bio main panel ──

def _build_bio_main_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("✅ Enable Sync", "action:bio_on")
    builder.add_row("⏹ Disable Sync", "action:bio_off")
    builder.add_row("👁 Show State", "panel:bio:state")
    builder.add_row("🏗 Template Builder", "panel:biohelp:builder")
    builder.add_row("💬 Set Text", "panel:bio:text")
    builder.add_row("💭 Set Mood", "panel:bio:mood")
    builder.add_row("🔧 Variables", "panel:biohelp:vars")
    builder.add_row("📋 Commands", "panel:biohelp:cmds")
    return builder.build()


async def _bio_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    if extra == "state":
        from backend.helper.inline_engine import _owner_id
        from backend.bot.handlers.misc import _resolve_tz
        result = await bio_service.do_show(_owner_id, _resolve_tz())
        return "Bio State", result, []

    if extra == "text":
        from backend.helper.inline_engine import _owner_id
        state = await db_client.get_or_create_bio_state(_owner_id)
        current = state.get("custom_text") or "—"
        builder = InlinePanelBuilder()
        builder.add_row("💬 Enter New Text", "input:bio:text")
        return "Set Text", f"**Current text:** `{current}`", builder.build()

    if extra == "mood":
        from backend.helper.inline_engine import _owner_id
        state = await db_client.get_or_create_bio_state(_owner_id)
        current = state.get("mood") or "—"
        builder = InlinePanelBuilder()
        builder.add_row("💭 Enter New Mood", "input:bio:mood")
        return "Set Mood", f"**Current mood:** `{current}`", builder.build()

    return "Bio Engine", "Choose an action:", _build_bio_main_buttons()


async def _bio_inline_builder(event, extra: str) -> list:
    title, body, buttons = await _bio_panel_handler(event, extra)
    return [render(title, body, buttons)]


# ── Biohelp submenu ──

def _build_bio_help_menu_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("🔧 Variables", "panel:biohelp:vars")
    builder.add_row("📋 Commands", "panel:biohelp:cmds")
    builder.add_row("🏗 Template Builder", "panel:biohelp:builder")
    return builder.build()


async def _biohelp_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    if not extra or extra == "menu":
        return "Bio Engine", "Choose a section:", _build_bio_help_menu_buttons()

    if extra == "vars":
        builder = InlinePanelBuilder()
        for var_token, _, _ in _BIO_VARS:
            builder.add_row(var_token, f"panel:biohelp:var:{var_token}")
        return "Bio Variables", "Tap a variable to see details:", builder.build()

    if extra.startswith("var:"):
        token = extra[4:]
        var_info = next((v for v in _BIO_VARS if v[0] == token), None)
        if var_info is None:
            return "Bio Variables", "Unknown variable.", _build_bio_help_menu_buttons()
        _, desc, example = var_info
        body = f"**Variable:** `{token}`\n\n**Description:** {desc}\n\n**Example value:** `{example}`"
        builder = InlinePanelBuilder()
        builder.add_row("📋 Copy Variable", f"action:bio_copy:{token}")
        return "Bio Variable", body, builder.build()

    if extra == "cmds":
        builder = InlinePanelBuilder()
        for i, (name, _, _, _) in enumerate(_BIO_CMDS):
            builder.add_row(name, f"panel:biohelp:cmd:{i}")
        return "Bio Commands", "Tap a command to see details:", builder.build()

    if extra.startswith("cmd:"):
        idx_str = extra[4:]
        if idx_str.isdigit():
            idx = int(idx_str)
            if 0 <= idx < len(_BIO_CMDS):
                name, purpose, syntax, example = _BIO_CMDS[idx]
                body = f"**{name}**\n\n**Purpose:** {purpose}\n\n**Syntax:**\n`{syntax}`\n\n**Example:**\n`{example}`"
                return "Bio Command", body, []
        return "Bio Commands", "Unknown command.", _build_bio_help_menu_buttons()

    if extra == "builder" or extra.startswith("builder:"):
        buf_encoded = ""
        if extra.startswith("builder:"):
            buf_encoded = extra[8:]
        buf = _decode_buffer(buf_encoded)

        body = f"**Template Builder**\n\n**Preview:**\n{_render_preview(buf)}\n\nTap a variable to insert it:"

        builder = InlinePanelBuilder()
        for var_token, _, _ in _BIO_VARS:
            new_buf = buf + var_token
            encoded = _encode_buffer(new_buf)
            builder.add_row(f"+ {var_token}", f"panel:biohelp:builder:{encoded}")

        builder.add_row("📝 Custom Template", "panel:biohelp:custom")
        builder.add_row("🗑 Clear", "panel:biohelp:builder:")
        builder.add_row("↩ Reset", f"panel:biohelp:builder:{_encode_buffer(_DEFAULT_TEMPLATE)}")
        if buf:
            builder.add_row("📋 Copy Template", f"action:bio_copy:{_encode_buffer(buf)}")
        return "Template Builder", body, builder.build()

    if extra == "custom":
        return await _render_custom_template_panel(event)

    return "Bio Engine", "Choose a section:", _build_bio_help_menu_buttons()


async def _biohelp_inline_builder(event, extra: str) -> list:
    title, body, buttons = await _biohelp_panel_handler(event, extra)
    return [render(title, body, buttons)]


# ── Custom Template mode ──

_CUSTOM_BODY = (
    "**Custom Template Mode**\n\n"
    "1. Tap a variable to copy it\n"
    "2. Write your template in any chat\n"
    "3. Reply to THIS message with your template\n\n"
    "Your reply will instantly become the active bio template."
)


def _build_custom_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("📋 {time}", "action:bio_copy_var:{time}")
    builder.add_row("📋 {text}", "action:bio_copy_var:{text}")
    builder.add_row("📋 {mood}", "action:bio_copy_var:{mood}")
    builder.add_row("Cancel", "panel:biohelp:builder")
    return builder.build()


def _set_custom_pending(owner_id: int, chat_id: int, msg_id: int) -> None:
    from backend.helper.input_state import set_pending
    set_pending(
        owner_id, "biohelp", _bio_custom_reply_handler,
        chat_id, _CUSTOM_BODY,
        inline_chat_id=chat_id,
        inline_msg_id=msg_id,
    )


async def _render_custom_template_panel(event) -> tuple[str, str, list]:
    from backend.helper.inline_engine import _owner_id

    owner_id = _owner_id
    chat_id = getattr(event, "chat_id", None) or 0
    msg_id = getattr(event, "message_id", None) or 0

    if chat_id and msg_id:
        _set_custom_pending(owner_id, chat_id, msg_id)

    return "Custom Template", _CUSTOM_BODY, _build_custom_buttons()


async def _bio_copy_var_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _self_client, _owner_id

    token = extra.strip()
    if not token:
        return "Custom Template", "Nothing to copy.", _build_custom_buttons()

    try:
        if _self_client is not None:
            await _self_client.send_message("me", f"📋 Copied: `{token}`")
    except Exception as exc:
        logger.warning("bio copy var failed: %s", exc)

    owner_id = _owner_id
    inline_chat_id = getattr(event, "chat_id", None) or chat_id or 0
    inline_msg_id = getattr(event, "message_id", None) or 0

    if inline_chat_id and inline_msg_id:
        _set_custom_pending(owner_id, inline_chat_id, inline_msg_id)

    body = f"✅ Copied `{token}` to Saved Messages\n\n{_CUSTOM_BODY}"
    return "Custom Template", body, _build_custom_buttons()


async def _bio_custom_reply_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _owner_id, _self_client

    result = await bio_service.do_template(_owner_id, text)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("bio custom template reply edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


# ── Actions ──

async def _bio_copy_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    if not extra:
        return "Bio Engine", "Nothing to copy.", []
    decoded = _decode_buffer(extra)
    if not decoded:
        return "Bio Engine", "Nothing to copy.", []

    try:
        from backend.helper.inline_engine import _self_client
        if _self_client is not None:
            await _self_client.send_message("me", f"📋 Copied:\n`{decoded}`")
            return "Bio Engine", f"✅ Copied to Saved Messages:\n`{decoded}`", []
    except Exception as exc:
        logger.warning("bio copy failed: %s", exc)
    return "Bio Engine", f"📋 Copy this:\n`{decoded}`", []


async def _bio_on_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.bot.handlers.misc import _resolve_tz
    result = await bio_service.do_on(_self_client, _owner_id, _resolve_tz())
    return "Bio Engine", result, _build_bio_main_buttons()


async def _bio_off_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    result = await bio_service.do_off(_owner_id)
    return "Bio Engine", result, _build_bio_main_buttons()


# ── Input handlers ──

async def _bio_text_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _owner_id, _self_client
    result = await bio_service.do_text(_owner_id, text)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("bio text inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _bio_mood_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _owner_id, _self_client
    result = await bio_service.do_mood(_owner_id, text)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("bio mood inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


# ── Registration ──

def register(client, owner_id: int, tz_str: str):

    register_panel("bio", _bio_panel_handler)
    register_panel("biohelp", _biohelp_panel_handler)
    register_inline_builder("bio", _bio_inline_builder)
    register_inline_builder("biohelp", _biohelp_inline_builder)
    register_action("bio_on", _bio_on_action)
    register_action("bio_off", _bio_off_action)
    register_action("bio_copy", _bio_copy_action)
    register_action("bio_copy_var", _bio_copy_var_action)
    register_input("bio", "text", {
        "handler": _bio_text_input_handler,
        "prompt": "**Bio Text**\n\nEnter the new {text} value:\n\n_Reply with the text below._",
    })
    register_input("bio", "mood", {
        "handler": _bio_mood_input_handler,
        "prompt": "**Bio Mood**\n\nEnter the new {mood} value:\n\n_Reply with the mood below._",
    })

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.bio(?:\s+(.+))?$"))
    async def bio_cmd(event):
        if not is_owner(event, owner_id):
            return
        arg = (event.pattern_match.group(1) or "").strip()

        if not arg:
            helper = get_client()
            if helper is None:
                await event.edit("⚠️ Inline mode requires the helper bot (BOT_TOKEN).")
                return
            try:
                await event.delete()
                await send_inline_panel(client, event.chat_id, "bio")
            except Exception as exc:
                logger.warning("bio inline send failed: %s", exc)
            return

        parts = arg.split(None, 1)
        sub = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        if sub == "help":
            helper = get_client()
            if helper is None:
                await event.edit(bio_service._HELP)
                return
            try:
                await event.delete()
                await send_inline_panel(client, event.chat_id, "biohelp")
            except Exception as exc:
                logger.warning("bio help inline send failed: %s", exc)
        elif sub == "template":
            if not rest:
                await event.edit("⚠️ Usage: `.bio template <template>`")
                return
            result = await bio_service.do_template(owner_id, rest)
            await event.edit(result)
        elif sub == "text":
            if not rest:
                await event.edit("⚠️ Usage: `.bio text <text>`")
                return
            result = await bio_service.do_text(owner_id, rest)
            await event.edit(result)
        elif sub == "mood":
            if not rest:
                await event.edit("⚠️ Usage: `.bio mood <mood>`")
                return
            result = await bio_service.do_mood(owner_id, rest)
            await event.edit(result)
        elif sub == "on":
            result = await bio_service.do_on(client, owner_id, tz_str)
            await event.edit(result)
        elif sub == "off":
            result = await bio_service.do_off(owner_id)
            await event.edit(result)
        elif sub == "show":
            result = await bio_service.do_show(owner_id, tz_str)
            await event.edit(result)
        else:
            await event.edit("⚠️ Unknown bio command. Try `.bio help`")
