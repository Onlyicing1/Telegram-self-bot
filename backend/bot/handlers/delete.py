"""
.del <n>         — Delete the last n outgoing messages in this chat.
.del id <msgid>  — Delete all messages from <msgid> forward in this chat.
.del <code>      — Delete a saved item: Telegram message + DB row.
.del             — Inline panel: choose deletion mode.

Delete-From-Message methods (accessed via .panel context panel):
  - Reply Mode: wait for reply, delete from replied message forward
  - Recent Messages: show recent outgoing messages as buttons
  - Manual Message ID: enter message ID manually

Business logic lives in backend.services.delete_service.
"""
import logging
from telethon import events
from backend.bot.handlers.guard import is_owner
from backend.services import delete_service
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    register_input,
    register_action,
    send_inline_panel,
    render,
    to_edit_buttons,
)
from backend.helper.client import get_client

logger = logging.getLogger(__name__)

_RECENT_PAGE_SIZE = 8


async def _del_n_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client
    text = text.strip()
    if not text.isdigit():
        result = "⚠️ Please enter a number between 1 and 500."
    else:
        result = await delete_service.do_del_n(_self_client, chat_id, int(text))
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("del n inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _del_id_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client
    text = text.strip()
    if not text.isdigit():
        result = "⚠️ Please enter a valid message ID (number)."
    else:
        result = await delete_service.do_del_id(_self_client, chat_id, int(text))
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("del id inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _del_code_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client, _owner_id
    result = await delete_service.do_del_code(_self_client, _owner_id, text)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("del code inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _del_reply_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client
    from backend.helper.input_state import clear_pending
    clear_pending(0)
    result = "⚠️ Reply mode cancelled."
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("del reply cancel edit failed: %s", exc)


async def _del_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    builder = InlinePanelBuilder()
    builder.add_row("🗑 Delete last N messages", "input:del:n")
    builder.add_row("🗑 Delete from Msg ID", "panel:delfrom")
    builder.add_row("🗑 Delete saved item", "input:del:code")
    return "Delete", "Choose a deletion mode:", builder.build()


async def _del_inline_builder(event, extra: str) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("🗑 Delete last N messages", "input:del:n")
    builder.add_row("🗑 Delete from Msg ID", "panel:delfrom")
    builder.add_row("🗑 Delete saved item", "input:del:code")
    return [render("Delete", "Choose a deletion mode:", builder.build())]


async def _delfrom_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    if extra.startswith("recent:"):
        page_str = extra[7:]
        page = int(page_str) if page_str.isdigit() else 0
        return await _delfrom_recent_page(event, page)
    if extra.startswith("confirm:"):
        msg_id_str = extra[9:]
        if msg_id_str.isdigit():
            return await _delfrom_confirm(int(msg_id_str))
    if extra.startswith("exec:"):
        msg_id_str = extra[5:]
        if msg_id_str.isdigit():
            from backend.helper.inline_engine import _self_client
            from backend.helper.target_context import get_target
            from backend.helper.inline_engine import _owner_id
            ctx = get_target(_owner_id)
            chat_id = ctx.reply_chat_id if ctx and ctx.kind == "reply" else 0
            if chat_id:
                result = await delete_service.do_del_id(_self_client, chat_id, int(msg_id_str))
                return "Delete From Message", result, []

    builder = InlinePanelBuilder()
    builder.add_row("Reply to Message", "action:delfrom_reply")
    builder.add_row("Recent Messages", "panel:delfrom:recent:0")
    builder.add_row("Enter Message ID", "input:del:id")
    return "Delete From...", "Choose a method:", builder.build()


async def _delfrom_inline_builder(event, extra: str) -> list:
    if extra.startswith("recent:"):
        page_str = extra[7:]
        page = int(page_str) if page_str.isdigit() else 0
        title, body, buttons = await _delfrom_recent_page(event, page)
        return [render(title, body, buttons)]
    if extra.startswith("confirm:"):
        msg_id_str = extra[9:]
        if msg_id_str.isdigit():
            title, body, buttons = await _delfrom_confirm(int(msg_id_str))
            return [render(title, body, buttons)]
    builder = InlinePanelBuilder()
    builder.add_row("Reply to Message", "action:delfrom_reply")
    builder.add_row("Recent Messages", "panel:delfrom:recent:0")
    builder.add_row("Enter Message ID", "input:del:id")
    return [render("Delete From...", "Choose a method:", builder.build())]


async def _delfrom_recent_page(event, page: int) -> tuple[str, str, list]:
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.helper.target_context import get_target

    client = _self_client
    owner_id = _owner_id
    ctx = get_target(owner_id)
    chat_id = ctx.reply_chat_id if ctx and ctx.kind == "reply" else 0
    if not chat_id:
        return "Delete From...", "⚠️ No chat context. Use `.panel` while replying to a message first.", []

    try:
        msgs = []
        async for msg in client.iter_messages(chat_id, limit=_RECENT_PAGE_SIZE * (page + 1) + 5, from_user="me"):
            msgs.append(msg)
            if len(msgs) >= _RECENT_PAGE_SIZE * (page + 1):
                break
        page_msgs = msgs[_RECENT_PAGE_SIZE * page: _RECENT_PAGE_SIZE * (page + 1)]
    except Exception as exc:
        logger.warning("delfrom recent: fetch failed: %s", exc)
        return "Delete From...", f"⚠️ Failed to fetch messages: {exc}", []

    if not page_msgs:
        return "Delete From...", "No more messages.", []

    builder = InlinePanelBuilder()
    for i in range(0, len(page_msgs), 2):
        pair = []
        for j in range(2):
            idx = i + j
            if idx < len(page_msgs):
                msg = page_msgs[idx]
                num = _RECENT_PAGE_SIZE * page + idx + 1
                raw_text = msg.text or msg.message or ""
                text_preview = raw_text[:20].replace("\n", " ")
                if len(raw_text) > 20:
                    text_preview += "..."
                btn_text = f"{_circled_num(num)} {text_preview}"
                pair.append((btn_text, f"panel:delfrom:confirm:{msg.id}"))
        if len(pair) == 2:
            builder.add_buttons(*pair)
        else:
            builder.add_row(pair[0][0], pair[0][1])

    has_prev = page > 0
    has_next = len(page_msgs) == _RECENT_PAGE_SIZE
    nav_buttons = []
    if has_prev:
        nav_buttons.append(("‹ Previous", f"panel:delfrom:recent:{page - 1}"))
    if has_next:
        nav_buttons.append(("Next ›", f"panel:delfrom:recent:{page + 1}"))
    if nav_buttons:
        builder.add_buttons(*nav_buttons)

    return "Delete From...", f"Recent outgoing messages (page {page + 1}):", builder.build()


async def _delfrom_confirm(msg_id: int) -> tuple[str, str, list]:
    builder = InlinePanelBuilder()
    builder.add_row("🗑 Confirm Delete", f"panel:delfrom:exec:{msg_id}")
    builder.add_row("‹ Back to methods", "panel:delfrom")
    return "Confirm Delete", f"Delete all outgoing messages from ID `{msg_id}` forward?", builder.build()


async def _delfrom_reply_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.helper.target_context import get_target
    from backend.helper.input_state import set_pending

    owner_id = _owner_id
    ctx = get_target(owner_id)
    if not ctx or ctx.kind != "reply":
        return "Delete From...", "⚠️ No reply context. Use `.panel` while replying to a message first.", []

    prompt = "**Delete From — Reply Mode**\n\nReply to any message in this chat.\nAll outgoing messages from that point will be deleted.\n\n_Reply to a message now._"

    set_pending(
        owner_id, "delfrom_reply", _delfrom_reply_wait_handler,
        ctx.reply_chat_id, prompt,
        inline_chat_id=chat_id,
        inline_msg_id=getattr(event, "message_id", 0) or 0,
    )
    return "Delete From...", "Waiting for your reply... Reply to any message to select the starting point.", []


async def _delfrom_reply_wait_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client, _owner_id

    client = _self_client
    try:
        reply_msg = await client.get_messages(chat_id, ids=msg_id)
        if reply_msg and reply_msg.reply_to_msg_id:
            target_id = reply_msg.reply_to_msg_id
            target_msg = await client.get_messages(chat_id, ids=target_id)
            if target_msg and not getattr(target_msg, "out", True):
                result = "⚠️ The replied message is not your outgoing message. Only your own messages can be deleted."
            else:
                result = await delete_service.do_del_id(client, chat_id, target_id)
        elif reply_msg:
            if not getattr(reply_msg, "out", True):
                result = "⚠️ Your message was not a reply. Please reply to a message to select the starting point."
            else:
                result = await delete_service.do_del_id(client, chat_id, msg_id)
        else:
            result = "⚠️ Could not find your reply message. Please try again."
    except Exception as exc:
        result = f"❌ Delete failed: {exc}"

    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("delfrom reply result edit failed: %s", exc)


_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚"

def _circled_num(n: int) -> str:
    if 1 <= n <= len(_CIRCLED):
        return _CIRCLED[n - 1]
    return f"[{n}]"


def register(client, owner_id: int):
    register_panel("del", _del_panel_handler)
    register_panel("delfrom", _delfrom_panel_handler)
    register_inline_builder("del", _del_inline_builder)
    register_inline_builder("delfrom", _delfrom_inline_builder)
    register_action("delfrom_reply", _delfrom_reply_action)
    register_input("del", "n", {
        "handler": _del_n_input_handler,
        "prompt": "**Delete Messages**\n\nEnter the number of messages to delete (1-500):\n\n_Reply with the number below._",
    })
    register_input("del", "id", {
        "handler": _del_id_input_handler,
        "prompt": "**Delete from Message ID**\n\nEnter the starting message ID:\n\n_Reply with the ID below._",
    })
    register_input("del", "code", {
        "handler": _del_code_input_handler,
        "prompt": "**Delete Saved Item**\n\nEnter the save code (e.g. S0001):\n\n_Reply with the code below._",
    })

    @client.on(events.NewMessage(outgoing=True, pattern=r"^\.del(?:\s+(.+))?$"))
    async def del_cmd(event):
        if not is_owner(event, owner_id):
            return

        arg = (event.pattern_match.group(1) or "").strip()

        if not arg:
            helper = get_client()
            if helper is None:
                await event.edit("⚠️ Usage: `.del <n>` or `.del id <msgid>` or `.del <code>`")
                return
            try:
                await event.delete()
                await send_inline_panel(client, event.chat_id, "del")
            except Exception as exc:
                logger.warning("del inline send failed: %s", exc)
            return

        if arg.lower().startswith("id "):
            rest = arg[3:].strip()
            if not rest.isdigit():
                await event.edit("⚠️ Usage: `.del id <msgid>`")
                return
            await event.delete()
            result = await delete_service.do_del_id(client, event.chat_id, int(rest))

        elif arg.isdigit():
            n = int(arg)
            if n < 1 or n > 500:
                await event.edit("⚠️ n must be between 1 and 500.")
                return
            await event.delete()
            result = await delete_service.do_del_n(client, event.chat_id, n)

        else:
            result = await delete_service.do_del_code(client, owner_id, arg)
            await event.edit(result)
