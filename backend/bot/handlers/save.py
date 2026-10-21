"""
Save Engine — Deep Save only.

Business logic lives in backend.services.save_service.execute_save, the
single authoritative Deep Save pipeline (download → re-upload as a NEW
Saved Messages message). This handler is only the Glass UI wiring:

    Menu → Save → Deep Save → Reply Mode → reply to a message

The reply's ``reply_to_msg_id`` is resolved to the exact target message,
which is passed into the shared Save Engine.

Owner metadata is OPTIONAL and never prompted for: ``Name & tags`` is its own
row, and Reply Mode without it stays exactly as prompt-free as before. The
metadata always travels as the shared ``save_service.SaveMetadata`` contract —
this module parses the one-line UI format and nothing else.

Live transfer progress is wired the same way: both user-facing sources (Reply
Mode and Save by Link) pass a ``save_service`` progress observer bound to the
SAME status message, which the helper edits in place. Without a helper bot there
is no inline panel message to edit, so the pipeline simply runs without
progress instead of inventing a new UI.
"""
import logging
import os

from backend.services import save_service
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    register_input,
    register_action,
    render,
)
from backend.helper.client import get_client
from backend.helper.save_progress import SaveProgressReporter
from backend.helper.styles import PRIMARY
from backend.helper import symbols as sym

logger = logging.getLogger(__name__)


async def _save_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    if extra.startswith("type:"):
        builder = InlinePanelBuilder()
        builder.add_row(sym.label(sym.OP_CHAT, "Reply Mode"), "action:save_reply", PRIMARY)
        builder.add_row(sym.label(sym.OP_EDIT, "Name & tags"), "input:save:meta", PRIMARY)
        builder.add_row(sym.label(sym.OP_SAVE, "Save using a link"), "input:save:link", PRIMARY)
        return "Deep Save", "Choose a source. Name & tags are optional.", builder.build()

    builder = InlinePanelBuilder()
    builder.add_row(sym.label(sym.OP_SAVE, "Deep Save"), "panel:save:type:d", PRIMARY)
    builder.add_row(sym.label(sym.OP_SEARCH, "Retrieve"), "panel:retrieve", PRIMARY)
    return (
        "Save",
        "Deep Save downloads the message and re-uploads it as a new Saved Messages message.",
        builder.build(),
    )


async def _save_inline_builder(event, extra: str) -> list:
    if extra.startswith("type:"):
        builder = InlinePanelBuilder()
        builder.add_row(sym.label(sym.OP_CHAT, "Reply Mode"), "action:save_reply", PRIMARY)
        builder.add_row(sym.label(sym.OP_EDIT, "Name & tags"), "input:save:meta", PRIMARY)
        builder.add_row(sym.label(sym.OP_SAVE, "Save using a link"), "input:save:link", PRIMARY)
        return [render("Deep Save", "Choose a source. Name & tags are optional.", builder.build())]

    builder = InlinePanelBuilder()
    builder.add_row(sym.label(sym.OP_SAVE, "Deep Save"), "panel:save:type:d", PRIMARY)
    builder.add_row(sym.label(sym.OP_SEARCH, "Retrieve"), "panel:retrieve", PRIMARY)
    return [
        render(
            "Save",
            "Deep Save downloads the message and re-uploads it as a new Saved Messages message.",
            builder.build(),
        )
    ]


async def _save_reply_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    from backend.helper.input_state import set_pending

    owner_id = _owner_id

    if not chat_id:
        return "Deep Save", f"{sym.STATE_WARNING} Could not determine the current chat. Please try again.", []

    wait_text = (
        "**Deep Save — Reply Mode**\n\n"
        "Waiting for your reply...\n"
        "Reply to any message to save it (download → re-upload)."
    )

    set_pending(
        owner_id, "save_reply", _save_reply_wait_handler,
        chat_id, wait_text,
        inline_chat_id=chat_id,
        inline_msg_id=getattr(event, "message_id", 0) or 0,
        extra="",
        timeout=None,
    )

    return "Deep Save", wait_text, []


# ── optional owner metadata (the "Name & tags" step) ──
#
# ONE deterministic line format, documented in the input prompt itself:
#
#     <name> | <tag>, <tag>
#
# - everything is optional, and the FIRST `|` splits the name from the tags
# - no `|`                 → the whole line is the name (no tags)
# - empty left side        → no name
# - empty right side, or `-` / `none` / `no` → explicitly NO tags
# - the right side is split on commas and normalized by the SHARED tag rules,
#   so the panel never invents a tag and a sentence is never read as tags
# - a second `|` is refused: one documented delimiter, not five syntaxes
_METADATA_DELIMITER = "|"
_NO_TAGS_TOKENS = frozenset({"-", "none", "no", "بدون", "هیچ"})


def parse_metadata_line(text: str) -> "save_service.SaveMetadata":
    """Parse the panel's one-line metadata format into the shared contract.

    Raises ``ValueError`` with an owner-readable reason; the shared
    ``SaveMetadata`` normalizer stays the one authority on lengths, whitespace,
    duplicates and the tag count.
    """
    line = (text or "").strip()
    if not line:
        raise ValueError("send a name, tags, or both")
    if line.count(_METADATA_DELIMITER) > 1:
        raise ValueError(
            f"use one {_METADATA_DELIMITER} between the name and the tags"
        )
    if _METADATA_DELIMITER in line:
        name_part, tags_part = line.split(_METADATA_DELIMITER, 1)
        parts = [p.strip() for p in tags_part.split(",")]
        raw_tags = (
            []
            if len(parts) == 1 and parts[0].casefold() in _NO_TAGS_TOKENS
            else parts
        )
    else:
        name_part, raw_tags = line, []

    metadata = save_service.SaveMetadata.from_raw(name_part, raw_tags)
    if metadata.display_name is None and not metadata.tags:
        raise ValueError("nothing to set — send a name, tags, or both")
    return metadata


def describe_metadata(metadata) -> str:
    """The owner-facing echo of the metadata armed for the next save."""
    name = metadata.display_name or "—"
    tags = ", ".join(metadata.tags) if metadata.tags else "no tags"
    return f"**Name:** {name} · **Tags:** {tags}"


def _reply_handler_with_metadata(metadata):
    """Reply Mode armed with metadata, carried INSIDE the pending entry.

    ``set_pending`` stores the handler callable in the per-owner pending state,
    so a closure is the one vehicle this architecture already provides for
    carrying an object from the arming step to the owner's reply — and it
    expires with that state exactly like every other pending input. Reading the
    popped state back inside the handler would not work: the input listener
    clears the entry before it calls the handler.
    """
    async def _handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
        await _save_reply_wait_handler(
            text, chat_id, msg_id, inline_chat_id, inline_msg_id, metadata=metadata
        )

    return _handler


async def _deep_save_panel_with_notice(notice: str) -> tuple[str, list]:
    """The Deep Save source panel with an owner-facing notice on top.

    One edit carries both the reason and the panel the owner retries from —
    the same finish-an-input idiom the other input flows use, never a bare
    text that strands the owner.
    """
    from backend.helper.panel_render import render_edit

    _title, body, raw_buttons = await _save_panel_handler(None, "type:")
    return render_edit("", f"{notice}\n\n{body}", raw_buttons)


async def _save_metadata_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    """Collect the optional name/tags line, then arm Reply Mode with it."""
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.helper.input_state import clear_pending, set_pending

    owner_id = _owner_id
    client = _self_client
    clear_pending(owner_id)

    try:
        metadata = parse_metadata_line(text)
    except ValueError as exc:
        result, buttons = await _deep_save_panel_with_notice(f"{sym.STATE_WARNING} {exc}")
    else:
        wait_text = (
            "**Deep Save — Reply Mode**\n\n"
            f"{describe_metadata(metadata)}\n\n"
            "Reply to any message to save it (download → re-upload)."
        )
        set_pending(
            owner_id, "save_reply", _reply_handler_with_metadata(metadata),
            chat_id, wait_text,
            inline_chat_id=inline_chat_id or chat_id,
            inline_msg_id=inline_msg_id or msg_id,
            timeout=None,
        )
        result, buttons = wait_text, []

    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=buttons)
        except Exception as exc:
            logger.warning("save metadata result edit failed: %s", exc)

    if client:
        try:
            await client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


def _progress_reporter(helper, inline_chat_id, inline_msg_id):
    """Bind save progress to the panel message that launched the operation.

    ``None`` when there is no helper-backed status message: a non-inline save
    has no message to edit, and a progress UI must not be invented for it.
    """
    if not helper or not inline_chat_id or not inline_msg_id:
        return None

    async def _edit(text: str) -> bool:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, text, buttons=[])
            return True
        except Exception as exc:
            logger.warning("save progress edit failed: %s", exc)
            return False

    return SaveProgressReporter(_edit)


async def _save_reply_wait_handler(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id, *, metadata=None
):
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.helper.input_state import clear_pending

    owner_id = _owner_id
    client = _self_client
    clear_pending(owner_id)

    helper = get_client()
    reporter = _progress_reporter(helper, inline_chat_id, inline_msg_id)

    try:
        # Resolve the user's outgoing reply, then the exact message it
        # replied to. That target — never the reply itself — is Deep Saved.
        reply_msg = await client.get_messages(chat_id, ids=msg_id)
        if reply_msg and reply_msg.reply_to_msg_id:
            target_id = reply_msg.reply_to_msg_id
            target_msg = await client.get_messages(chat_id, ids=target_id)
            if target_msg is None:
                result = f"{sym.STATE_WARNING} The replied message no longer exists."
            else:
                result = await save_service.execute_save(
                    client, owner_id, target_msg, os.getenv("TZ", "Asia/Tehran"),
                    metadata=metadata,
                    progress=reporter.handle if reporter is not None else None,
                )
        elif reply_msg:
            result = f"{sym.STATE_WARNING} Your message was not a reply. Please reply to a message to select what to save."
        else:
            result = f"{sym.STATE_WARNING} Could not find your reply message. Please try again."
    except Exception as exc:
        result = f"{sym.STATE_FAILED} Deep Save failed: {exc}"

    if reporter is not None:
        await reporter.close(result)
    elif helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=[])
        except Exception as exc:
            logger.warning("save reply result edit failed: %s", exc)

    if client:
        try:
            await client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _save_link_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.helper.input_state import clear_pending

    owner_id = _owner_id
    client = _self_client
    clear_pending(owner_id)

    helper = get_client()
    reporter = _progress_reporter(helper, inline_chat_id, inline_msg_id)

    link = text.strip()
    if not link:
        result = f"{sym.STATE_WARNING} Link cannot be empty."
    else:
        result = await save_service.execute_link_save(
            client, owner_id, link, os.getenv("TZ", "Asia/Tehran"),
            progress=reporter.handle if reporter is not None else None,
        )

    logger.info("[LINK_SAVE] handler result: %s", result)

    if reporter is not None:
        await reporter.close(result)
    elif helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result, buttons=[])
        except Exception as exc:
            logger.warning("[LINK_SAVE] result edit failed: %s", exc)

    if client:
        try:
            await client.delete_messages(chat_id, [msg_id])
        except Exception as exc:
            logger.warning("[LINK_SAVE] delete trigger msg failed: %s", exc)


def register(client, owner_id: int, tz_str: str) -> None:
    register_panel("save", _save_panel_handler, parent="menu", title="Save")
    register_inline_builder("save", _save_inline_builder)
    register_action("save_reply", _save_reply_action)
    register_input("save", "meta", {
        "handler": _save_metadata_input_handler,
        "prompt": (
            "**Name & tags** — optional\n\n"
            "One line: `Name | tag, tag`\n\n"
            "• no `|` → the line is the name\n"
            "• nothing after `|`, or `-` → no tags\n"
            "• tags are split on commas; nothing is invented\n\n"
            "_Reply with the line below._"
        ),
    })
    register_input("save", "link", {
        "handler": _save_link_input_handler,
        "prompt": "**Save by Link**\n\nSend a Telegram message link:\n`https://t.me/channel/123`\n`https://t.me/c/123/456`\n\n_Reply with the link below._",
    })
