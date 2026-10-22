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

Live transfer progress always edits a message the owner is ALREADY looking at,
and neither input mode adds one:

* Reply Mode owns the panel itself. The ``Waiting for your reply...`` panel is
  replaced — in that same message — by target resolution, every transfer
  milestone and the terminal result. The panel coordinates captured when Reply
  Mode was armed travel through the pending input and are used for every edit,
  so one save never adds a second message to the chat.
* Save by Link has no panel to keep, so the SELF client sends ONE dedicated
  Loading/status message right after the link arrives (before it is parsed or
  resolved), and every update edits that message.

Only real download/upload byte callbacks drive the milestones, and a UI edit
failure is logged and swallowed — a transfer is never cancelled by its own
progress surface (the AI tools have no chat UI and simply run without it).
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

    # The panel IS the progress surface: these exact coordinates travel through
    # the pending input and every later edit of this save addresses them, so
    # Reply Mode never adds a message to the chat.
    panel_chat, panel_msg_id = _panel_coords(event, chat_id)

    wait_text = _reply_wait_text()

    set_pending(
        owner_id, "save_reply", _save_reply_wait_handler,
        chat_id, wait_text,
        inline_chat_id=panel_chat or chat_id,
        inline_msg_id=panel_msg_id,
        extra="",
        timeout=None,
    )

    return "Deep Save", wait_text, []


REPLY_PANEL_TITLE = "Deep Save — Reply Mode"


def _reply_wait_text(metadata=None) -> str:
    """The ONE Reply Mode panel text, with the optional metadata echo."""
    lines: list[str] = [f"**{REPLY_PANEL_TITLE}**", ""]
    if metadata is not None:
        lines.extend([describe_metadata(metadata), ""])
    lines.append("Waiting for your reply...")
    lines.append("Reply to any message to save it (download → re-upload).")
    return "\n".join(lines)


def _panel_coords(event, chat_id: int) -> tuple[int, int]:
    """The EXACT (chat id, message id) of the panel the owner is looking at.

    ``resolve_callback_message`` answers directly for a normal panel; an inline
    panel carries only its ``inline_message_id``, so the session that sent it is
    consulted for the real pair the router itself edits. ``(chat_id, 0)`` when
    no message id can be established — the caller then runs without UI rather
    than inventing a second message.
    """
    from backend.helper.panels import resolve_callback_message

    panel_chat, panel_msg_id, inline_id = resolve_callback_message(event)
    panel_chat = panel_chat or chat_id or 0
    if not panel_msg_id and inline_id:
        try:
            from backend.helper.lifecycle import get_lifecycle
            session = get_lifecycle().sessions.get_by_inline_id(inline_id)
        except Exception:
            session = None
        if session is not None:
            panel_chat = panel_chat or session.chat_id
            panel_msg_id = session.msg_id
    return int(panel_chat or 0), int(panel_msg_id or 0)


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
        wait_text = _reply_wait_text(metadata)
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


def _status_editor(self_client, chat_id, msg_id):
    """An edit callable for ONE message, deduped and failure-contained.

    Used for the Save-by-Link status message and for the Reply Mode panel: both
    are sent by the OWNER'S account (an inline panel result is sent by the
    requesting client, so the helper bot never owns it), and both are edited
    through this one callable.

    ``edit_message`` is called with Telethon's documented signature
    (``entity, message_id, text``) — passing the text as ``message`` bound it to
    the message-id parameter and every edit raised ``TypeError``, which is why
    no progress could ever appear live even with the right client.

    No reply markup: a user account cannot attach a bot keyboard, so a rejected
    edit would hide the progress entirely.
    """
    from backend.helper.panel_render import render_edit

    applied: dict[str, str | None] = {"last": None}

    async def _edit(text: str) -> bool:
        rendered, _buttons = render_edit("", text, [])
        if rendered == applied["last"]:
            return True
        try:
            await self_client.edit_message(chat_id, msg_id, rendered)
            applied["last"] = rendered
            return True
        except Exception as exc:  # noqa: BLE001 — progress never aborts a save
            if "not modified" in str(exc).lower():
                # Telegram already shows exactly this text — not a failure.
                applied["last"] = rendered
                return True
            logger.warning(
                "save status edit failed chat=%s msg=%s: %s", chat_id, msg_id, exc
            )
            return False

    return _edit


def _panel_editor(self_client, chat_id, msg_id):
    """An edit callable for the panel Reply Mode already shows, or ``None``.

    The panel is the message the owner's own account already sent (an inline
    panel result is sent by the requesting client, and the fallback panel is
    sent the same way), so the self client is the one authority that may edit
    it. ``None`` when there is no client or no panel coordinate: the save then
    runs without UI — a missing panel is never replaced by a second message.
    """
    if self_client is None or not chat_id or not msg_id:
        return None
    return _status_editor(self_client, chat_id, msg_id)


async def _start_status(
    self_client, chat_id: int, *, title: str, body: str
) -> "SaveProgressReporter | None":
    """Send the ONE dedicated Loading/status message and bind progress to it.

    Used by Save by Link, which has no panel to keep. Created by the SELF client
    in the chat the owner just typed in, immediately after the input arrives and
    before any link resolution or media work, so a slow lookup never leaves the
    owner staring at the static prompt. The returned reporter edits that exact
    message for every update and for the terminal result.

    ``None`` when there is no client, no chat, or Telegram refused to send the
    message: the save still runs, and the caller renders its result on the
    panel as the previous behaviour did. Reply Mode must never come here — it
    edits its existing panel instead (``_panel_editor``).
    """
    if self_client is None or not chat_id:
        return None

    from backend.helper.panel_render import render_edit

    try:
        rendered, _buttons = render_edit(title, body, [])
        message = await self_client.send_message(chat_id, rendered)
    except Exception as exc:  # noqa: BLE001 — a save must not fail on its own UI
        logger.warning("save status message could not be created chat=%s: %s", chat_id, exc)
        return None

    msg_id = getattr(message, "id", None)
    if not msg_id:
        logger.warning("save status message has no id chat=%s: no progress shown", chat_id)
        return None

    # The message's OWN coordinates, not the requested ones: that is the message
    # every later edit must address.
    status_chat_id = getattr(message, "chat_id", None) or chat_id
    logger.info(
        "[SAVE_UI] status message created chat=%s msg=%s title=%s",
        status_chat_id, msg_id, title,
    )
    return SaveProgressReporter(
        _status_editor(self_client, status_chat_id, msg_id), title=title
    )


async def _edit_status(self_client, inline_chat_id, inline_msg_id, text: str, label: str) -> bool:
    """Fallback for Save by Link: render its result on the panel instead."""
    if self_client is None or not inline_chat_id or not inline_msg_id:
        return False
    try:
        from backend.helper.panel_render import render_edit
        rendered, buttons = render_edit("", text, [])
        await self_client.edit_message(
            inline_chat_id, inline_msg_id, rendered, buttons=buttons
        )
        return True
    except Exception as exc:  # noqa: BLE001 — the transfer result already exists
        logger.warning("%s result edit failed: %s", label, exc)
        return False


async def _save_reply_wait_handler(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id, *, metadata=None
):
    from backend.helper.inline_engine import _self_client, _owner_id
    from backend.helper.input_state import clear_pending

    owner_id = _owner_id
    client = _self_client
    clear_pending(owner_id)

    # Reply Mode keeps the panel it armed: "Waiting for your reply..." becomes
    # the resolution line, then the transfer milestones, then the result — one
    # message edited in place, never a second one, and it is touched BEFORE the
    # reply/target lookup so resolution time is visible instead of silent.
    editor = _panel_editor(client, inline_chat_id, inline_msg_id)
    reporter = None
    if editor is None:
        logger.warning(
            "[SAVE_UI] reply mode has no editable panel (chat=%s msg=%s) — "
            "the save runs without status updates",
            inline_chat_id, inline_msg_id,
        )
    else:
        await editor(f"**{REPLY_PANEL_TITLE}**\n\nResolving the target message…")
        reporter = SaveProgressReporter(editor, title=REPLY_PANEL_TITLE)

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

    # The status message exists BEFORE the link is parsed or resolved, so a
    # slow lookup is never mistaken for an ignored link.
    reporter = await _start_status(
        client, chat_id, title="Save by Link", body="Resolving the link…"
    )

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
    else:
        await _edit_status(client, inline_chat_id, inline_msg_id, result, "link save")

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
        "prompt": (
            "**Save by Link**\n\nSend a Telegram message link:\n"
            "`https://t.me/channel/123`\n"
            "`https://t.me/c/123/456`\n"
            "`tg://openmessage?user_id=123&message_id=456`\n\n"
            "_Reply with the link below._"
        ),
    })
