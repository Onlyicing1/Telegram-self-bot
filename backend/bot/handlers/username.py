"""
Username command handler — fully inline, no text commands.

Inline Username panels (mirrors Bio Engine exactly):
  panel:username             — Main menu (all buttons)
  panel:username:state       — Show State page
  panel:username:text        — Set Text page (input prompt)
  panel:username:mood        — Set Mood page (input prompt)

  panel:usernamehelp:vars       — Variable reference
  panel:usernamehelp:var:{tok}  — Single variable detail
  panel:usernamehelp:cmds       — Commands menu (all buttons, no text syntax)
  panel:usernamehelp:cmd:on     — Enable Sync page
  panel:usernamehelp:cmd:off    — Disable Sync page
  panel:usernamehelp:cmd:show   — Show State page
  panel:usernamehelp:cmd:text   — Set Text page (input prompt)
  panel:usernamehelp:cmd:mood   — Set Mood page (input prompt)
  panel:usernamehelp:builder    — Template Builder (sequential append)
  panel:usernamehelp:custom     — Custom Template mode (reply to apply)

Template Builder is strictly sequential — every action appends to the
END of the buffer. Nothing is ever reordered or overwritten. The buffer
is stored server-side (in-memory dict keyed by owner_id) so callback_data
stays short and never truncates.
"""
import logging

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.db import client as db_client
from backend.services import username_service
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


_USERNAME_VARS = [
    ("{time}", "Current time in HH:MM format", "14:30"),
    ("{mood}", "Current mood value", "😊"),
    ("{text}", "Custom freeform text", "Working"),
]

_DEFAULT_TEMPLATE = "{time} | {mood}"

_builder_buffers: dict[int, str] = {}


def _get_buf(owner_id: int) -> str:
    return _builder_buffers.get(owner_id, "")


def _set_buf(owner_id: int, buf: str) -> None:
    _builder_buffers[owner_id] = buf


def _clear_buf(owner_id: int) -> None:
    _builder_buffers.pop(owner_id, None)


def _render_preview(buf: str) -> str:
    if not buf:
        return "_(empty — tap variables to build)_"
    return f"`{buf}`"


# ── Username main panel ──

def _build_username_main_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("✅ Enable Sync", "panel:usernamehelp:cmd:on")
    builder.add_row("⏹ Disable Sync", "panel:usernamehelp:cmd:off")
    builder.add_row("👁 Show State", "panel:username:state")
    builder.add_row("🏗 Template Builder", "panel:usernamehelp:builder")
    builder.add_row("💬 Set Text", "panel:username:text")
    builder.add_row("💭 Set Mood", "panel:username:mood")
    builder.add_row("🔧 Variables", "panel:usernamehelp:vars")
    builder.add_row("📋 Commands", "panel:usernamehelp:cmds")
    return builder.build()


async def _username_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    if extra == "state":
        from backend.helper.inline_engine import _owner_id
        from backend.bot.handlers.misc import _resolve_tz
        result = await username_service.do_show(_owner_id, _resolve_tz())
        return "Username State", result, []

    if extra == "text":
        from backend.helper.inline_engine import _owner_id
        state = await db_client.get_or_create_username_state(_owner_id)
        current = state.get("custom_text") or "—"
        builder = InlinePanelBuilder()
        builder.add_row("💬 Enter New Text", "input:username:text")
        return "Set Text", f"**Current text:** `{current}`\n\nTap the button below, then reply with the new text value.", builder.build()

    if extra == "mood":
        from backend.helper.inline_engine import _owner_id
        state = await db_client.get_or_create_username_state(_owner_id)
        current = state.get("mood") or "—"
        builder = InlinePanelBuilder()
        builder.add_row("💭 Enter New Mood", "input:username:mood")
        return "Set Mood", f"**Current mood:** `{current}`\n\nTap the button below, then reply with the new mood value.", builder.build()

    return "Username Engine", "Choose an action:", _build_username_main_buttons()


async def _username_inline_builder(event, extra: str) -> list:
    title, body, buttons = await _username_panel_handler(event, extra)
    return [render(title, body, buttons)]


# ── Usernamehelp submenu ──

def _build_username_help_menu_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("🔧 Variables", "panel:usernamehelp:vars")
    builder.add_row("📋 Commands", "panel:usernamehelp:cmds")
    builder.add_row("🏗 Template Builder", "panel:usernamehelp:builder")
    return builder.build()


def _build_username_commands_menu_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("💬 Set Text", "panel:usernamehelp:cmd:text")
    builder.add_row("💭 Set Mood", "panel:usernamehelp:cmd:mood")
    builder.add_row("✅ Enable Sync", "panel:usernamehelp:cmd:on")
    builder.add_row("⏹ Disable Sync", "panel:usernamehelp:cmd:off")
    builder.add_row("👁 Show State", "panel:usernamehelp:cmd:show")
    return builder.build()


def _build_builder_buttons() -> list:
    builder = InlinePanelBuilder()
    for var_token, _, _ in _USERNAME_VARS:
        builder.add_row(f"+ {var_token}", f"action:username_builder_add:{var_token}")
    builder.add_row("␣ Space", "action:username_builder_space")
    builder.add_row("🗑 Clear", "action:username_builder_clear")
    builder.add_row("↩ Reset", "action:username_builder_reset")
    builder.add_row("✅ Apply Template", "action:username_builder_apply")
    builder.add_row("📝 Reply to Edit", "action:username_reply_mode")
    return builder.build()


def _build_applied_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("👁 Show State", "panel:username:state")
    builder.add_row("🏗 Back to Builder", "panel:usernamehelp:builder")
    return builder.build()


async def _usernamehelp_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    if not extra or extra == "menu":
        return "Username Engine", "Choose a section:", _build_username_help_menu_buttons()

    if extra == "vars":
        builder = InlinePanelBuilder()
        for var_token, _, _ in _USERNAME_VARS:
            builder.add_row(var_token, f"panel:usernamehelp:var:{var_token}")
        return "Username Variables", "Tap a variable to see details:", builder.build()

    if extra.startswith("var:"):
        token = extra[4:]
        var_info = next((v for v in _USERNAME_VARS if v[0] == token), None)
        if var_info is None:
            return "Username Variables", "Unknown variable.", _build_username_help_menu_buttons()
        _, desc, example = var_info
        body = f"**Variable:** `{token}`\n\n**Description:** {desc}\n\n**Example value:** `{example}`"
        return "Username Variable", body, []

    if extra == "cmds":
        return "Username Commands", "Tap a button to open that page:", _build_username_commands_menu_buttons()

    if extra.startswith("cmd:"):
        sub = extra[4:]
        if sub == "on":
            builder = InlinePanelBuilder()
            builder.add_row("✅ Enable Now", "action:username_on")
            return "Enable Sync", "Start the username cron — auto-updates your first name every minute.", builder.build()
        if sub == "off":
            builder = InlinePanelBuilder()
            builder.add_row("⏹ Disable Now", "action:username_off")
            return "Disable Sync", "Stop the username cron. Your first name will no longer auto-update.", builder.build()
        if sub == "show":
            from backend.helper.inline_engine import _owner_id
            from backend.bot.handlers.misc import _resolve_tz
            result = await username_service.do_show(_owner_id, _resolve_tz())
            return "Username State", result, []
        if sub == "text":
            from backend.helper.inline_engine import _owner_id
            state = await db_client.get_or_create_username_state(_owner_id)
            current = state.get("custom_text") or "—"
            builder = InlinePanelBuilder()
            builder.add_row("💬 Enter New Text", "input:usernamehelp:text")
            return "Set Text", f"**Current text:** `{current}`\n\nTap the button below, then reply with the new text value.", builder.build()
        if sub == "mood":
            from backend.helper.inline_engine import _owner_id
            state = await db_client.get_or_create_username_state(_owner_id)
            current = state.get("mood") or "—"
            builder = InlinePanelBuilder()
            builder.add_row("💭 Enter New Mood", "input:usernamehelp:mood")
            return "Set Mood", f"**Current mood:** `{current}`\n\nTap the button below, then reply with the new mood value.", builder.build()
        return "Username Commands", "Unknown command.", _build_username_commands_menu_buttons()

    if extra == "builder":
        from backend.helper.inline_engine import _owner_id
        buf = _get_buf(_owner_id)
        body = (
            f"**Template Builder**\n\n"
            f"**Preview:**\n{_render_preview(buf)}\n\n"
            f"Tap a variable to append it. Tap Space to add a space.\n"
            f"Everything is appended in order — nothing is reordered."
        )
        return "Template Builder", body, _build_builder_buttons()

    if extra == "custom":
        return await _render_custom_template_panel(event)

    return "Username Engine", "Choose a section:", _build_username_help_menu_buttons()


async def _usernamehelp_inline_builder(event, extra: str) -> list:
    title, body, buttons = await _usernamehelp_panel_handler(event, extra)
    return [render(title, body, buttons)]


# ── Custom Template mode (reply-based) ──

_CUSTOM_BODY = (
    "**Custom Template Mode**\n\n"
    "Reply to THIS message with your template.\n\n"
    "Available variables: `{time}`, `{mood}`, `{text}`\n\n"
    "Example reply:\n"
    "`{time} | {mood} | {text}`\n\n"
    "Your reply will instantly become the active username template."
)


def _build_custom_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("Cancel", "panel:usernamehelp:builder")
    return builder.build()


def _set_custom_pending(owner_id: int, chat_id: int, msg_id: int) -> None:
    from backend.helper.input_state import set_pending
    set_pending(
        owner_id, "usernamehelp", _username_custom_reply_handler,
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


async def _username_custom_reply_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _owner_id, _self_client

    result = await username_service.do_template(_owner_id, text)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("username custom template reply edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


# ── Builder actions (short callbacks, server-side buffer) ──

async def _username_builder_add_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    token = extra or ""
    buf = _get_buf(_owner_id)
    buf = buf + token
    _set_buf(_owner_id, buf)
    body = (
        f"**Template Builder**\n\n"
        f"**Preview:**\n{_render_preview(buf)}\n\n"
        f"Tap a variable to append it. Tap Space to add a space.\n"
        f"Everything is appended in order — nothing is reordered."
    )
    return "Template Builder", body, _build_builder_buttons()


async def _username_builder_space_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    buf = _get_buf(_owner_id)
    buf = buf + " "
    _set_buf(_owner_id, buf)
    body = (
        f"**Template Builder**\n\n"
        f"**Preview:**\n{_render_preview(buf)}\n\n"
        f"Tap a variable to append it. Tap Space to add a space.\n"
        f"Everything is appended in order — nothing is reordered."
    )
    return "Template Builder", body, _build_builder_buttons()


async def _username_builder_clear_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    _clear_buf(_owner_id)
    body = (
        f"**Template Builder**\n\n"
        f"**Preview:**\n{_render_preview('')}\n\n"
        f"Tap a variable to append it. Tap Space to add a space.\n"
        f"Everything is appended in order — nothing is reordered."
    )
    return "Template Builder", body, _build_builder_buttons()


async def _username_builder_reset_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    _set_buf(_owner_id, _DEFAULT_TEMPLATE)
    buf = _DEFAULT_TEMPLATE
    body = (
        f"**Template Builder**\n\n"
        f"**Preview:**\n{_render_preview(buf)}\n\n"
        f"Tap a variable to append it. Tap Space to add a space.\n"
        f"Everything is appended in order — nothing is reordered."
    )
    return "Template Builder", body, _build_builder_buttons()


async def _username_builder_apply_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    buf = _get_buf(_owner_id)
    if not buf:
        return "Template Builder", "⚠️ Nothing to apply — build a template first.", _build_builder_buttons()
    result = await username_service.do_template(_owner_id, buf)
    if not result.startswith("✅"):
        return "Template Builder", result, _build_builder_buttons()
    return "Template Applied", result, _build_applied_buttons()


# ── Other actions ──

async def _username_on_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.bot.handlers.misc import _resolve_tz
    result = await username_service.do_on(_self_client, _owner_id, _resolve_tz())
    builder = InlinePanelBuilder()
    builder.add_row("⏹ Disable Sync", "panel:usernamehelp:cmd:off")
    builder.add_row("👁 Show State", "panel:username:state")
    return "Username Engine", result, builder.build()


async def _username_off_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    result = await username_service.do_off(_owner_id)
    builder = InlinePanelBuilder()
    builder.add_row("✅ Enable Sync", "panel:usernamehelp:cmd:on")
    builder.add_row("👁 Show State", "panel:username:state")
    return "Username Engine", result, builder.build()


async def _username_reply_mode_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    return await _render_custom_template_panel(event)


# ── Input handlers ──

async def _username_text_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _owner_id, _self_client
    result = await username_service.do_text(_owner_id, text)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("username text inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _username_mood_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _owner_id, _self_client
    result = await username_service.do_mood(_owner_id, text)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("username mood inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


# ── Registration ──

def register(client, owner_id: int, tz_str: str):

    register_panel("username", _username_panel_handler)
    register_panel("usernamehelp", _usernamehelp_panel_handler)
    register_inline_builder("username", _username_inline_builder)
    register_inline_builder("usernamehelp", _usernamehelp_inline_builder)
    register_action("username_on", _username_on_action)
    register_action("username_off", _username_off_action)
    register_action("username_reply_mode", _username_reply_mode_action)
    register_action("username_builder_add", _username_builder_add_action)
    register_action("username_builder_space", _username_builder_space_action)
    register_action("username_builder_clear", _username_builder_clear_action)
    register_action("username_builder_reset", _username_builder_reset_action)
    register_action("username_builder_apply", _username_builder_apply_action)
    register_input("username", "text", {
        "handler": _username_text_input_handler,
        "prompt": "**Username Text**\n\nEnter the new {text} value:\n\n_Reply with the text below._",
    })
    register_input("username", "mood", {
        "handler": _username_mood_input_handler,
        "prompt": "**Username Mood**\n\nEnter the new {mood} value:\n\n_Reply with the mood below._",
    })
    register_input("usernamehelp", "text", {
        "handler": _username_text_input_handler,
        "prompt": "**Username Text**\n\nEnter the new {text} value:\n\n_Reply with the text below._",
    })
    register_input("usernamehelp", "mood", {
        "handler": _username_mood_input_handler,
        "prompt": "**Username Mood**\n\nEnter the new {mood} value:\n\n_Reply with the mood below._",
    })

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.username(?:\s+(.+))?$"))
    async def username_cmd(event):
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
                await send_inline_panel(client, event.chat_id, "username")
            except Exception as exc:
                logger.warning("username inline send failed: %s", exc)
            return

        parts = arg.split(None, 1)
        sub = parts[0].lower()
        rest = parts[1] if len(parts) > 1 else ""

        helper = get_client()
        if helper is None:
            if sub == "template" and rest:
                result = await username_service.do_template(owner_id, rest)
            elif sub == "text" and rest:
                result = await username_service.do_text(owner_id, rest)
            elif sub == "mood" and rest:
                result = await username_service.do_mood(owner_id, rest)
            elif sub == "on":
                result = await username_service.do_on(client, owner_id, tz_str)
            elif sub == "off":
                result = await username_service.do_off(owner_id)
            elif sub == "show":
                result = await username_service.do_show(owner_id, tz_str)
            else:
                result = "⚠️ Inline mode is required for the full Username Engine UI. Set BOT_TOKEN to enable it."
            await event.edit(result)
            return

        try:
            await event.delete()
            await send_inline_panel(client, event.chat_id, "username")
        except Exception as exc:
            logger.warning("username inline send failed: %s", exc)
