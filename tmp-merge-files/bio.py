"""
Bio command handler.

  .bio help              — Token reference
  .bio template <tpl>    — Set template
  .bio text <text>       — Set {text} token
  .bio mood <mood>       — Set {mood} token
  .bio on                — Start timezone-synchronized cron
  .bio off               — Stop cron
  .bio show              — Inspect current state
  .bio                   — Inline panel: choose bio action

Inline Mode:
  - .bio (no args) → inline panel with buttons.
  - .bio on/off/show → Type A: execute immediately via callback.
  - .bio template/text/mood → Type B: input prompt for text.
  - All .bio <subcommand> with args still work as edit-in-place (backward compat).
"""
import logging
from datetime import datetime

from telethon import events

from backend.bio import engine as bio_engine
from backend.bot.handlers.guard import is_owner
from backend.db import client as db_client
from backend.diagnostics import record_event
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    register_action,
    register_input,
    send_inline_panel,
)
from backend.helper.client import get_client

logger = logging.getLogger(__name__)

_HELP = (
    "**Bio Engine — Token Reference**\n\n"
    "`{time}` — Current time (HH:MM)\n"
    "`{mood}` — Current mood value\n"
    "`{text}` — Custom freeform text\n\n"
    "**Commands**\n"
    "`.bio text <text>` — Set {text}\n"
    "`.bio mood <mood>` — Set {mood}\n"
    "`.bio on` — Start cron sync\n"
    "`.bio off` — Stop cron sync\n"
    "`.bio show` — Inspect state\n"
    "`.bio template <tpl>` — Set template\n\n"
    "**Example template**\n"
    "`🕒 {time} | 💭 {mood} | 📝 {text}`"
)


async def _do_on(client, owner_id: int, tz_str: str) -> str:
    try:
        db_client.update_bio_state(owner_id, {"is_active": True})
    except Exception as exc:
        return f"❌ DB error: {exc}"
    bio_engine.start_cron(client, owner_id, tz_str)
    record_event("bio", "cron on", 0, "SUCCESS")
    state = db_client.get_or_create_bio_state(owner_id)
    preview = bio_engine.render_bio(
        state.get("template", "🕒 {time} | 💭 {mood}"),
        state.get("mood", "😊"),
        state.get("custom_text", ""),
        tz_str,
    )
    return f"✅ Bio cron **ON**\nPreview: `{preview}`"


async def _do_off(owner_id: int) -> str:
    try:
        db_client.update_bio_state(owner_id, {"is_active": False})
    except Exception as exc:
        return f"❌ DB error: {exc}"
    bio_engine.stop_cron()
    record_event("bio", "cron off", 0, "SUCCESS")
    return "⏹ Bio cron **OFF**"


async def _do_show(owner_id: int, tz_str: str) -> str:
    state = db_client.get_or_create_bio_state(owner_id)
    now = bio_engine._get_tz(tz_str)
    now_dt = datetime.now(now)
    preview = bio_engine.render_bio(
        state.get("template", "🕒 {time} | 💭 {mood}"),
        state.get("mood", "😊"),
        state.get("custom_text", ""),
        tz_str,
    )
    status = "ON" if bio_engine.is_running() else "OFF"
    return (
        f"**Bio State**\n\n"
        f"Status: `{status}`\n"
        f"Template: `{state.get('template') or '🕒 {time} | 💭 {mood}'}`\n"
        f"Mood: `{state.get('mood') or '😊'}`\n"
        f"Text: `{state.get('custom_text') or '—'}`\n"
        f"Last Bio: `{state.get('last_bio') or '—'}`\n"
        f"Preview: `{preview}`\n"
        f"Server Time ({tz_str}): `{now_dt.strftime('%H:%M:%S')}`"
    )


async def _do_template(owner_id: int, template: str) -> str:
    if not template:
        return "⚠️ Template cannot be empty."
    try:
        db_client.update_bio_state(owner_id, {"template": template})
    except Exception as exc:
        return f"❌ DB error: {exc}"
    return f"✅ Template updated:\n`{template}`"


async def _do_text(owner_id: int, text: str) -> str:
    try:
        db_client.update_bio_state(owner_id, {"custom_text": text})
    except Exception as exc:
        return f"❌ DB error: {exc}"
    return f"✅ Text set to: `{text}`"


async def _do_mood(owner_id: int, mood: str) -> str:
    try:
        db_client.update_bio_state(owner_id, {"mood": mood})
    except Exception as exc:
        return f"❌ DB error: {exc}"
    return f"✅ Mood set to: `{mood}`"


async def _bio_on_action(event, extra: str) -> tuple:
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.bot.handlers.misc import _resolve_tz
    result = await _do_on(_self_client, _owner_id, _resolve_tz())
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:bio")
    builder.add_row("Close", "panel:help:close")
    return result, builder.build()


async def _bio_off_action(event, extra: str) -> tuple:
    from backend.helper.inline_engine import _owner_id
    result = await _do_off(_owner_id)
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:bio")
    builder.add_row("Close", "panel:help:close")
    return result, builder.build()


async def _bio_show_action(event, extra: str) -> tuple:
    from backend.helper.inline_engine import _owner_id
    from backend.bot.handlers.misc import _resolve_tz
    result = await _do_show(_owner_id, _resolve_tz())
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:bio")
    builder.add_row("Close", "panel:help:close")
    return result, builder.build()


async def _bio_help_action(event, extra: str) -> tuple:
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:bio")
    builder.add_row("Close", "panel:help:close")
    return _HELP, builder.build()


async def _bio_template_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _owner_id
    result = await _do_template(_owner_id, text)
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:bio")
    builder.add_row("Close", "panel:help:close")
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=builder.build())
            await helper.delete_messages(chat_id, [msg_id])
        except Exception as exc:
            logger.warning("bio template inline edit failed: %s", exc)


async def _bio_text_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _owner_id
    result = await _do_text(_owner_id, text)
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:bio")
    builder.add_row("Close", "panel:help:close")
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=builder.build())
            await helper.delete_messages(chat_id, [msg_id])
        except Exception as exc:
            logger.warning("bio text inline edit failed: %s", exc)


async def _bio_mood_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _owner_id
    result = await _do_mood(_owner_id, text)
    builder = InlinePanelBuilder()
    builder.add_row("Back", "panel:bio")
    builder.add_row("Close", "panel:help:close")
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=builder.build())
            await helper.delete_messages(chat_id, [msg_id])
        except Exception as exc:
            logger.warning("bio mood inline edit failed: %s", exc)


async def _bio_panel_handler(event, extra: str) -> None:
    text = "**Bio Engine**\n\nChoose an action:"
    builder = InlinePanelBuilder()
    builder.add_row("✅ Bio ON", "action:bio_on")
    builder.add_row("⏹ Bio OFF", "action:bio_off")
    builder.add_row("👁 Show State", "action:bio_show")
    builder.add_row("📝 Set Template", "input:bio:template")
    builder.add_row("💬 Set Text", "input:bio:text")
    builder.add_row("💭 Set Mood", "input:bio:mood")
    builder.add_row("❓ Help", "action:bio_help")
    builder.add_row("Close", "panel:help:close")
    await event.edit(text, buttons=builder.build())


async def _bio_inline_builder(event, extra: str) -> list:
    from telethon.tl import types
    text = "**Bio Engine**\n\nChoose an action:"
    builder = InlinePanelBuilder()
    builder.add_row("✅ Bio ON", "action:bio_on")
    builder.add_row("⏹ Bio OFF", "action:bio_off")
    builder.add_row("👁 Show State", "action:bio_show")
    builder.add_row("📝 Set Template", "input:bio:template")
    builder.add_row("💬 Set Text", "input:bio:text")
    builder.add_row("💭 Set Mood", "input:bio:mood")
    builder.add_row("❓ Help", "action:bio_help")
    builder.add_row("Close", "panel:help:close")
    buttons = builder.build()
    msg = types.InputBotInlineMessageTextAuto(
        message=text,
        reply_markup=types.ReplyInlineMarkup(rows=buttons) if buttons else None,
    )
    result = types.InputBotInlineResult(
        id="0",
        type="article",
        title="Bio Engine",
        send_message=msg,
    )
    return [result]


def register(client, owner_id: int, tz_str: str):

    register_panel("bio", _bio_panel_handler)
    register_inline_builder("bio", _bio_inline_builder)
    register_action("bio_on", _bio_on_action)
    register_action("bio_off", _bio_off_action)
    register_action("bio_show", _bio_show_action)
    register_action("bio_help", _bio_help_action)
    register_input("bio", "template", {
        "handler": _bio_template_input_handler,
        "prompt": "**Set Template**\n\nEnter the new bio template:\n\n_Tokens: {time} {mood} {text}_\n\n_Reply below._",
    })
    register_input("bio", "text", {
        "handler": _bio_text_input_handler,
        "prompt": "**Set Text**\n\nEnter the {text} token value:\n\n_Reply below._",
    })
    register_input("bio", "mood", {
        "handler": _bio_mood_input_handler,
        "prompt": "**Set Mood**\n\nEnter the {mood} token value:\n\n_Reply below._",
    })

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.bio(?:\s+(.+))?$"))
    async def bio_cmd(event):
        if not is_owner(event, owner_id):
            return

        arg = (event.pattern_match.group(1) or "").strip()

        if not arg:
            helper = get_client()
            if helper is None:
                await event.edit(_HELP)
                return
            try:
                await event.delete()
                await send_inline_panel(client, event.chat_id, "bio")
            except Exception as exc:
                logger.warning("bio inline send failed: %s", exc)
            return

        try:
            state = db_client.get_or_create_bio_state(owner_id)
        except Exception as exc:
            logger.error("bio db init failed: %s", exc)
            await event.edit(f"❌ DB error: {exc}")
            return

        if arg in ("help", "template") and " " not in arg:
            if arg == "template":
                await event.edit(
                    f"**Current template:**\n`{state.get('template') or '🕒 {time} | 💭 {mood}'}`\n\n"
                    "To change: `.bio template <new template>`"
                )
            else:
                await event.edit(_HELP)
            return

        if arg.startswith("template "):
            result = await _do_template(owner_id, arg[9:].strip())
            await event.edit(result)
        elif arg.startswith("text "):
            result = await _do_text(owner_id, arg[5:].strip())
            await event.edit(result)
        elif arg.startswith("mood "):
            result = await _do_mood(owner_id, arg[5:].strip())
            await event.edit(result)
        elif arg == "on":
            result = await _do_on(client, owner_id, tz_str)
            await event.edit(result)
        elif arg == "off":
            result = await _do_off(owner_id)
            await event.edit(result)
        elif arg == "show":
            result = await _do_show(owner_id, tz_str)
            await event.edit(result)
        else:
            await event.edit("⚠️ Unknown bio command. Try `.bio help`")
