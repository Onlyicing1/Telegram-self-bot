"""
Bio command handler — fully inline, no text commands.

Inline Bio panels:
  panel:bio             — Main menu (all buttons, single sync toggle)
  panel:bio:state       — Show State page
  panel:bio:text        — Set Text page (input prompt)
  panel:bio:mood        — Set Mood page (input prompt)

  panel:biohelp:vars       — Variable reference
  panel:biohelp:var:{tok}  — Single variable detail
  panel:biohelp:cmds       — Commands menu (all buttons, no text syntax)
  panel:biohelp:cmd:toggle — Sync toggle page
  panel:biohelp:cmd:show   — Show State page
  panel:biohelp:cmd:text   — Set Text page (input prompt)
  panel:biohelp:cmd:mood   — Set Mood page (input prompt)
  panel:biohelp:builder    — Template Builder (insert variables, preview updates in-place)
  panel:biohelp:custom     — Custom Template mode (reply to apply)

Every page gets Back + Home + Close auto-added by _finalize_panel.
No text command list. No .bio syntax anywhere in the UI.
No messages sent to Saved Messages.
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

def _build_bio_main_buttons(is_active: bool = False) -> list:
    builder = InlinePanelBuilder()
    if is_active:
        builder.add_row("Sync: ON (tap to disable)", "action:bio_toggle")
    else:
        builder.add_row("Sync: OFF (tap to enable)", "action:bio_toggle")
    builder.add_row("👁 Show State", "panel:bio:state")
    builder.add_row("🏗 Template Builder", "panel:biohelp:builder")
    builder.add_row("💬 Set Text", "panel:bio:text")
    builder.add_row("💭 Set Mood", "panel:bio:mood")
    builder.add_row("🔧 Variables", "panel:biohelp:vars")
    builder.add_row("📋 Commands", "panel:biohelp:cmds")
    return builder.build()


async def _bio_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id

    state = await db_client.get_or_create_bio_state(_owner_id)
    is_active = state.get("is_active", False)

    if extra == "state":
        from backend.bot.handlers.misc import _resolve_tz
        result = await bio_service.do_show(_owner_id, _resolve_tz())
        return "Bio State", result, []

    if extra == "text":
        current = state.get("custom_text") or "—"
        builder = InlinePanelBuilder()
        builder.add_row("💬 Enter New Text", "input:bio:text")
        return "Set Text", f"**Current text:** `{current}`\n\nTap the button below, then reply with the new text value.", builder.build()

    if extra == "mood":
        current = state.get("mood") or "—"
        builder = InlinePanelBuilder()
        builder.add_row("💭 Enter New Mood", "input:bio:mood")
        return "Set Mood", f"**Current mood:** `{current}`\n\nTap the button below, then reply with the new mood value.", builder.build()

    return "Bio Engine", "Choose an action:", _build_bio_main_buttons(is_active)


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


def _build_commands_menu_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("💬 Set Text", "panel:biohelp:cmd:text")
    builder.add_row("💭 Set Mood", "panel:biohelp:cmd:mood")
    builder.add_row("🔄 Toggle Sync", "panel:biohelp:cmd:toggle")
    builder.add_row("👁 Show State", "panel:biohelp:cmd:show")
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
        return "Bio Variable", body, []

    if extra == "cmds":
        return "Bio Commands", "Tap a button to open that page:", _build_commands_menu_buttons()

    if extra.startswith("cmd:"):
        sub = extra[4:]
        if sub == "toggle":
            from backend.helper.inline_engine import _owner_id
            state = await db_client.get_or_create_bio_state(_owner_id)
            is_active = state.get("is_active", False)
            label = "ON" if is_active else "OFF"
            body = f"**Sync is currently {label}.**\n\nTap the button below to toggle."
            builder = InlinePanelBuilder()
            if is_active:
                builder.add_row("Sync: ON (tap to disable)", "action:bio_toggle")
            else:
                builder.add_row("Sync: OFF (tap to enable)", "action:bio_toggle")
            return "Toggle Sync", body, builder.build()
        if sub == "show":
            from backend.helper.inline_engine import _owner_id
            from backend.bot.handlers.misc import _resolve_tz
            result = await bio_service.do_show(_owner_id, _resolve_tz())
            return "Bio State", result, []
        if sub == "text":
            from backend.helper.inline_engine import _owner_id
            state = await db_client.get_or_create_bio_state(_owner_id)
            current = state.get("custom_text") or "—"
            builder = InlinePanelBuilder()
            builder.add_row("💬 Enter New Text", "input:biohelp:text")
            return "Set Text", f"**Current text:** `{current}`\n\nTap the button below, then reply with the new text value.", builder.build()
        if sub == "mood":
            from backend.helper.inline_engine import _owner_id
            state = await db_client.get_or_create_bio_state(_owner_id)
            current = state.get("mood") or "—"
            builder = InlinePanelBuilder()
            builder.add_row("💭 Enter New Mood", "input:biohelp:mood")
            return "Set Mood", f"**Current mood:** `{current}`\n\nTap the button below, then reply with the new mood value.", builder.build()
        return "Bio Commands", "Unknown command.", _build_commands_menu_buttons()

    if extra == "builder" or extra.startswith("builder:"):
        buf_encoded = ""
        if extra.startswith("builder:"):
            buf_encoded = extra[8:]
        buf = _decode_buffer(buf_encoded)

        body = (
            f"**Template Builder**\n\n"
            f"**Preview:**\n{_render_preview(buf)}\n\n"
            f"Tap a variable to add it to the preview:"
        )

        builder = InlinePanelBuilder()
        for var_token, _, _ in _BIO_VARS:
            new_buf = buf + var_token
            encoded = _encode_buffer(new_buf)
            builder.add_row(f"+ {var_token}", f"panel:biohelp:builder:{encoded}")

        builder.add_row("🗑 Clear", "panel:biohelp:builder:")
        builder.add_row("↩ Reset", f"panel:biohelp:builder:{_encode_buffer(_DEFAULT_TEMPLATE)}")
        if buf:
            builder.add_row("✅ Apply Template", f"action:bio_apply:{_encode_buffer(buf)}")
        builder.add_row("📝 Reply to Edit", "action:bio_reply_mode")
        return "Template Builder", body, builder.build()

    if extra == "custom":
        return await _render_custom_template_panel(event)

    return "Bio Engine", "Choose a section:", _build_bio_help_menu_buttons()


async def _biohelp_inline_builder(event, extra: str) -> list:
    title, body, buttons = await _biohelp_panel_handler(event, extra)
    return [render(title, body, buttons)]


# ── Custom Template mode (reply-based) ──

_CUSTOM_BODY = (
    "**Custom Template Mode**\n\n"
    "Reply to THIS message with your template.\n\n"
    "Available variables: `{time}`, `{mood}`, `{text}`\n\n"
    "Example reply:\n"
    "`🕒 {time} | 💭 {mood} | 📝 {text}`\n\n"
    "Your reply will instantly become the active bio template."
)


def _build_custom_buttons() -> list:
    builder = InlinePanelBuilder()
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

async def _bio_toggle_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.bot.handlers.misc import _resolve_tz
    result = await bio_service.do_toggle(_self_client, _owner_id, _resolve_tz())

    state = await db_client.get_or_create_bio_state(_owner_id)
    is_active = state.get("is_active", False)

    builder = InlinePanelBuilder()
    if is_active:
        builder.add_row("Sync: ON (tap to disable)", "action:bio_toggle")
    else:
        builder.add_row("Sync: OFF (tap to enable)", "action:bio_toggle")
    builder.add_row("👁 Show State", "panel:bio:state")
    return "Bio Engine", result, builder.build()


async def _bio_apply_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    decoded = _decode_buffer(extra)
    if not decoded:
        return "Template Builder", "⚠️ Nothing to apply — build a template first.", _build_builder_buttons("")

    result = await bio_service.do_template(_owner_id, decoded)
    if not result.startswith("✅"):
        return "Template Builder", result, _build_builder_buttons(decoded)

    builder = InlinePanelBuilder()
    builder.add_row("👁 Show State", "panel:bio:state")
    builder.add_row("🏗 Back to Builder", "panel:biohelp:builder")
    return "Template Applied", result, builder.build()


async def _bio_reply_mode_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    return await _render_custom_template_panel(event)


def _build_builder_buttons(buf: str) -> list:
    builder = InlinePanelBuilder()
    for var_token, _, _ in _BIO_VARS:
        new_buf = buf + var_token
        encoded = _encode_buffer(new_buf)
        builder.add_row(f"+ {var_token}", f"panel:biohelp:builder:{encoded}")
    builder.add_row("🗑 Clear", "panel:biohelp:builder:")
    builder.add_row("↩ Reset", f"panel:biohelp:builder:{_encode_buffer(_DEFAULT_TEMPLATE)}")
    if buf:
        builder.add_row("✅ Apply Template", f"action:bio_apply:{_encode_buffer(buf)}")
    builder.add_row("📝 Reply to Edit", "action:bio_reply_mode")
    return builder.build()


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
    register_action("bio_toggle", _bio_toggle_action)
    register_action("bio_apply", _bio_apply_action)
    register_action("bio_reply_mode", _bio_reply_mode_action)
    register_input("bio", "text", {
        "handler": _bio_text_input_handler,
        "prompt": "**Bio Text**\n\nEnter the new {text} value:\n\n_Reply with the text below._",
    })
    register_input("bio", "mood", {
        "handler": _bio_mood_input_handler,
        "prompt": "**Bio Mood**\n\nEnter the new {mood} value:\n\n_Reply with the mood below._",
    })
    register_input("biohelp", "text", {
        "handler": _bio_text_input_handler,
        "prompt": "**Bio Text**\n\nEnter the new {text} value:\n\n_Reply with the text below._",
    })
    register_input("biohelp", "mood", {
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

        helper = get_client()
        if helper is None:
            if sub == "template" and rest:
                result = await bio_service.do_template(owner_id, rest)
            elif sub == "text" and rest:
                result = await bio_service.do_text(owner_id, rest)
            elif sub == "mood" and rest:
                result = await bio_service.do_mood(owner_id, rest)
            elif sub == "on":
                result = await bio_service.do_on(client, owner_id, tz_str)
            elif sub == "off":
                result = await bio_service.do_off(owner_id)
            elif sub == "toggle":
                result = await bio_service.do_toggle(client, owner_id, tz_str)
            elif sub == "show":
                result = await bio_service.do_show(owner_id, tz_str)
            else:
                result = "⚠️ Inline mode is required for the full Bio Engine UI. Set BOT_TOKEN to enable it."
            await event.edit(result)
            return

        try:
            await event.delete()
            await send_inline_panel(client, event.chat_id, "bio")
        except Exception as exc:
            logger.warning("bio inline send failed: %s", exc)
