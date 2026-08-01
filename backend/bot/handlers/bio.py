"""
Bio command handler.

Business logic lives in backend.services.bio_service.
This handler is only the Telethon wiring + panel rendering.

Inline Bio Help submenu (panel:biohelp:*):
  vars       — Variable reference with Copy buttons
  cmds       — Command reference with detail pages
  builder    — Interactive Template Builder (live preview buffer)
  examples   — Ready-made template examples with Copy buttons
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
    to_edit_buttons,
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
    ("Set Template", "Set the bio template string", ".bio template <tpl>", ".bio template 🕒 {time} | 💭 {mood}"),
]

_BIO_EXAMPLES = [
    "🕒 {time}",
    "🕒 {time} | 💭 {mood}",
    "💭 {mood}\n📝 {text}",
    "🕒 {time}\n💭 {mood}\n📝 {text}",
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


def _build_bio_help_menu_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("🔧 Variables", "panel:biohelp:vars")
    builder.add_row("📋 Commands", "panel:biohelp:cmds")
    builder.add_row("🏗 Template Builder", "panel:biohelp:builder")
    builder.add_row("📝 Example", "panel:biohelp:examples")
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
        builder.add_row("‹ Back to Variables", "panel:biohelp:vars")
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
                builder = InlinePanelBuilder()
                builder.add_row("‹ Back to Commands", "panel:biohelp:cmds")
                return "Bio Command", body, builder.build()
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

        builder.add_row("🗑 Clear", "panel:biohelp:builder:")
        builder.add_row("↩ Reset", f"panel:biohelp:builder:{_encode_buffer(_DEFAULT_TEMPLATE)}")
        builder.add_row("📋 Copy Template", f"action:bio_copy:{_encode_buffer(buf)}" if buf else "panel:_nav:noop")
        return "Template Builder", body, builder.build()

    if extra == "examples":
        builder = InlinePanelBuilder()
        for i, ex in enumerate(_BIO_EXAMPLES):
            label = ex.replace("\n", " ")
            if len(label) > 30:
                label = label[:30] + "..."
            builder.add_row(label, f"panel:biohelp:ex:{i}")
        return "Bio Examples", "Tap an example to see details:", builder.build()

    if extra.startswith("ex:"):
        idx_str = extra[3:]
        if idx_str.isdigit():
            idx = int(idx_str)
            if 0 <= idx < len(_BIO_EXAMPLES):
                ex = _BIO_EXAMPLES[idx]
                body = f"**Example {idx + 1}:**\n\n`{ex}`"
                builder = InlinePanelBuilder()
                builder.add_row("📋 Copy", f"action:bio_copy:{_encode_buffer(ex)}")
                builder.add_row("‹ Back to Examples", "panel:biohelp:examples")
                return "Bio Example", body, builder.build()
        return "Bio Examples", "Unknown example.", _build_bio_help_menu_buttons()

    return "Bio Engine", "Choose a section:", _build_bio_help_menu_buttons()


async def _biohelp_inline_builder(event, extra: str) -> list:
    title, body, buttons = await _biohelp_panel_handler(event, extra)
    return [render(title, body, buttons)]


async def _bio_copy_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    if not extra:
        return "Bio Engine", "Nothing to copy.", _build_bio_help_menu_buttons()
    decoded = _decode_buffer(extra)
    if not decoded:
        return "Bio Engine", "Nothing to copy.", _build_bio_help_menu_buttons()

    try:
        from backend.helper.inline_engine import _self_client, _owner_id
        client = _self_client
        owner_id = _owner_id
        if client is not None:
            await client.send_message("me", f"📋 Copied:\n`{decoded}`")
            return "Bio Engine", f"✅ Copied to Saved Messages:\n`{decoded}`", _build_bio_help_menu_buttons()
    except Exception as exc:
        logger.warning("bio copy failed: %s", exc)
    return "Bio Engine", f"📋 Copy this:\n`{decoded}`", _build_bio_help_menu_buttons()


async def _bio_on_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.bot.handlers.misc import _resolve_tz
    result = await bio_service.do_on(_self_client, _owner_id, _resolve_tz())
    return "Bio Engine", result, _build_bio_help_menu_buttons()


async def _bio_off_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    result = await bio_service.do_off(_owner_id)
    return "Bio Engine", result, _build_bio_help_menu_buttons()


async def _bio_show_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    from backend.bot.handlers.misc import _resolve_tz
    result = await bio_service.do_show(_owner_id, _resolve_tz())
    return "Bio Engine", result, _build_bio_help_menu_buttons()


async def _bio_template_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _owner_id, _self_client
    result = await bio_service.do_template(_owner_id, text)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("bio template inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


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


async def _bio_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    builder = InlinePanelBuilder()
    builder.add_row("✅ Bio ON", "action:bio_on")
    builder.add_row("⏹ Bio OFF", "action:bio_off")
    builder.add_row("👁 Show State", "action:bio_show")
    builder.add_row("📝 Set Template", "input:bio:template")
    builder.add_row("💬 Set Text", "input:bio:text")
    builder.add_row("💭 Set Mood", "input:bio:mood")
    builder.add_row("❓ Help", "panel:biohelp:menu")
    return "Bio Engine", "Choose an action:", builder.build()


async def _bio_inline_builder(event, extra: str) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("✅ Bio ON", "action:bio_on")
    builder.add_row("⏹ Bio OFF", "action:bio_off")
    builder.add_row("👁 Show State", "action:bio_show")
    builder.add_row("📝 Set Template", "input:bio:template")
    builder.add_row("💬 Set Text", "input:bio:text")
    builder.add_row("💭 Set Mood", "input:bio:mood")
    builder.add_row("❓ Help", "panel:biohelp:menu")
    return [render("Bio Engine", "Choose an action:", builder.build())]


def register(client, owner_id: int, tz_str: str):

    register_panel("bio", _bio_panel_handler)
    register_panel("biohelp", _biohelp_panel_handler)
    register_inline_builder("bio", _bio_inline_builder)
    register_inline_builder("biohelp", _biohelp_inline_builder)
    register_action("bio_on", _bio_on_action)
    register_action("bio_off", _bio_off_action)
    register_action("bio_show", _bio_show_action)
    register_action("bio_copy", _bio_copy_action)
    register_input("bio", "template", {
        "handler": _bio_template_input_handler,
        "prompt": "**Bio Template**\n\nEnter the new template (use {time}, {mood}, {text}):\n\n_Reply with the template below._",
    })
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
