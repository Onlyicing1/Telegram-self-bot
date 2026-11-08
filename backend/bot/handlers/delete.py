"""
Delete Engine — Glass UI only (no dot commands).

Deletion scopes offered by the panel:

  - quick counts (last 10/25/50/100/250/500 of the owner's messages)
  - a custom count above those
  - all of the owner's messages in this chat
  - one selected message from a paginated preview of the owner's messages
  - the owner's messages from a selected message forward
  - one message by ID, or every owner message from an ID forward
  - a date/time range

The chat is resolved from the panel callback / live panel session — never from
a reply TargetContext, so the panel works from any chat without the owner
having to reply to something first. Ownership is NOT decided here: every chat
deletion goes through ``backend.services.delete_service`` and its single
``delete_verified_self_messages`` chokepoint.
"""
import asyncio
import logging
import os
import time

from backend.services import delete_service
from backend.services.delete_service import MAX_DELETE_COUNT, DeleteReport
from backend.helper import (
    InlinePanelBuilder,
    register_panel,
    register_inline_builder,
    register_input,
    register_action,
    render,
)
from backend.helper.client import get_client
from backend.helper.styles import PRIMARY, SUCCESS
from backend.helper import symbols as sym

logger = logging.getLogger(__name__)

_RECENT_PAGE_SIZE = 8
_QUICK_COUNTS = (10, 25, 50, 100, 250, 500)
#: Progress is advisory: a long deletion costs a few panel edits, never one per
#: message, and one slow edit can never hold up the operation.
_PROGRESS_PAINT_INTERVAL = 3.0
_PROGRESS_EDIT_TIMEOUT = 5.0
#: The confirmation panel must render even when the preview cannot be read.
_ALL_PREVIEW_TIMEOUT = 5.0


# ── shared plumbing ─────────────────────────────────────────────────────────


def _self_client():
    from backend.helper.inline_engine import _self_client

    return _self_client


def _configured_tz() -> str:
    """The deployment timezone (the same ``TZ`` source the runtime config reads)."""
    return os.getenv("TZ", "Asia/Tehran") or "UTC"


def _chat_for(event) -> int:
    """The chat this panel acts on — the panel's own context, never a guess.

    A normal panel callback carries its chat directly. An inline panel carries
    only its inline identifier, so the session that sent it is consulted for
    the real chat the router itself edits. ``0`` means "no usable context" and
    the caller says so instead of inventing a chat.
    """
    chat_id = getattr(event, "chat_id", None)
    if chat_id:
        return int(chat_id)
    msg_id = getattr(event, "message_id", None)
    inline_id = getattr(event, "inline_message_id", None)
    try:
        from backend.helper.lifecycle import get_lifecycle

        sessions = get_lifecycle().sessions
    except Exception:
        sessions = None
    if sessions is None:
        return 0
    session = sessions.get_by_inline_id(inline_id) if inline_id else None
    if session is None and msg_id:
        session = sessions.find_by_msg_id(int(msg_id))
    if session is not None and session.chat_id:
        return int(session.chat_id)
    return 0


async def _edit_panel(
    text: str,
    *,
    chat_id: int | None,
    msg_id: int | None,
    inline_chat_id: int | None,
    inline_msg_id: int | None,
    inline_id=None,
) -> bool:
    """Report on the panel the owner is looking at, through the real edit paths.

    The Helper Bot's inline edit is tried first (it is the only principal that
    can replace an inline panel's keyboard), and the owner's own account edits
    the same message when the inline path is unavailable — so a helper-less
    runtime still shows the result instead of nothing.
    """
    from backend.helper.panel_edit import ExistingPanelEditor

    editor = ExistingPanelEditor(
        chat_id=int(inline_chat_id or chat_id or 0),
        msg_id=int(inline_msg_id or msg_id or 0),
        inline_id=inline_id,
        helper=get_client(),
        self_client=_self_client(),
    )
    if not editor.available:
        logger.warning(
            "DELETE_UI no panel surface for the result (chat=%s msg=%s)",
            chat_id, msg_id,
        )
        return False
    return await editor.edit(text)


async def _delete_command_message(chat_id: int, msg_id: int) -> None:
    """Remove the owner's typed command message; a refusal is never fatal."""
    client = _self_client()
    if client is None or not chat_id or not msg_id:
        return
    try:
        await client.delete_messages(chat_id, [msg_id])
    except Exception as exc:
        logger.debug("delete: could not remove the command message: %s", exc)


def _report_text(report: DeleteReport, *, error: Exception | None = None) -> str:
    """The owner-facing account of one operation, from the real numbers."""
    scope = report.requested or "the requested messages"
    lines: list[str] = []
    if error is not None:
        lines.append(f"{sym.STATE_FAILED} Delete did not finish: {error}")
    elif report.deleted:
        lines.append(f"{sym.OP_DELETE} Deleted {report.deleted} of {scope}.")
    else:
        lines.append(f"{sym.STATE_WARNING} Nothing was deleted from {scope}.")
    lines.append(
        f"Considered {report.considered} · deleted {report.deleted} · "
        f"rejected {report.rejected}."
    )
    if report.rejected and not report.deleted:
        lines.append(
            f"{sym.STATE_WARNING} Rejected by the ownership check: only messages "
            "sent by your own account can be deleted."
        )
    if report.complete:
        lines.append("Completed.")
    else:
        lines.append(
            f"{sym.STATE_WARNING} Partial result: "
            f"{report.reason or 'the operation stopped early'}."
        )
    return "\n".join(lines)


def _progress_painter(event):
    """A throttled progress painter for the panel this click came from.

    The snapshot carries only measured figures (``scanned`` while the history is
    read, ``considered``/``deleted`` afterwards), so the panel never renders a
    number nobody counted. Painting is throttled and individually bounded:
    progress must not slow the deletion down, and must not fail it.
    """
    chat_id = _chat_for(event)
    msg_id = getattr(event, "message_id", None)
    inline_id = getattr(event, "inline_message_id", None)
    state = {"last": 0.0}

    async def paint(snapshot: dict) -> None:
        now = time.monotonic()
        if state["last"] and now - state["last"] < _PROGRESS_PAINT_INTERVAL:
            return
        state["last"] = now
        parts: list[str] = []
        if "scanned" in snapshot:
            parts.append(f"scanned {snapshot['scanned']} message(s)")
        if "considered" in snapshot:
            parts.append(f"{snapshot['considered']} of yours verified")
        if "deleted" in snapshot:
            parts.append(f"{snapshot['deleted']} deleted")
        detail = " · ".join(parts) or "starting the history scan"
        text = (
            f"{sym.OP_DELETE} Working — {detail}.\n\n"
            "Only messages sent by your account are ever deleted."
        )
        try:
            await asyncio.wait_for(
                _edit_panel(
                    text,
                    chat_id=chat_id, msg_id=msg_id,
                    inline_chat_id=chat_id, inline_msg_id=msg_id,
                    inline_id=inline_id,
                ),
                timeout=_PROGRESS_EDIT_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 — progress never aborts a deletion
            logger.debug("DELETE_UI progress paint failed: %s", exc)

    return paint


def _back_row(target: str = "panel:del") -> list:
    builder = InlinePanelBuilder()
    builder.add_row(sym.label(sym.OP_BACK, "Back"), target)
    return builder.build()


def _no_chat() -> tuple[str, str, list]:
    return (
        "Delete",
        f"{sym.STATE_WARNING} Could not determine this chat from the panel. "
        "Open the panel from the chat whose messages you want to delete.",
        _back_row(),
    )


def _unavailable() -> tuple[str, str, list]:
    return (
        "Delete",
        f"{sym.STATE_FAILED} The Telegram client is not available right now; "
        "nothing was deleted.",
        _back_row(),
    )


def _home_buttons() -> InlinePanelBuilder:
    builder = InlinePanelBuilder()
    row: list[tuple[str, str]] = []
    for n in _QUICK_COUNTS:
        row.append((sym.label(sym.OP_DELETE, f"Last {n}"), f"panel:del:q:{n}"))
        if len(row) == 3:
            builder.add_buttons(*row)
            row = []
    if row:
        builder.add_buttons(*row)
    builder.add_row(sym.label(sym.OP_DELETE, "Custom amount..."), "input:del:n")
    builder.add_row(sym.label(sym.OP_DELETE, "All my messages"), "panel:del:all")
    builder.add_row(sym.label(sym.OP_DELETE, "Delete from a message..."), "panel:delfrom")
    builder.add_row(sym.label(sym.OP_DELETE, "Delete by date range..."), "input:del:range")
    return builder


def _methods_buttons() -> InlinePanelBuilder:
    builder = InlinePanelBuilder()
    builder.add_row(sym.label(sym.OP_DELETE, "Recent messages"), "panel:delfrom:recent:0")
    builder.add_row(sym.label(sym.OP_DELETE, "Enter a message ID..."), "input:del:from")
    builder.add_row(sym.label(sym.OP_EDIT, "Reply to a message"), "action:delfrom_reply")
    return builder


# ── Delete home + quick counts + all ────────────────────────────────────────


async def _del_view(event, extra: str) -> tuple[str, str, list]:
    client = _self_client()
    chat_id = _chat_for(event)
    head, _, rest = (extra or "").partition(":")

    if head == "q" and rest.isdigit():
        n = int(rest)
        if not chat_id:
            return _no_chat()
        builder = InlinePanelBuilder()
        builder.add_row(
            sym.label(sym.OP_DELETE, f"Confirm: delete last {n}"),
            f"panel:del:qexec:{n}",
            SUCCESS,
        )
        builder.add_row(sym.label(sym.OP_BACK, "Back"), "panel:del")
        return (
            "Delete",
            f"Delete your {n} most recent messages in this chat?\n\n"
            "Only messages sent by your account are deleted.",
            builder.build(),
        )

    if head == "qexec" and rest.isdigit():
        n = int(rest)
        if not chat_id:
            return _no_chat()
        if client is None:
            return _unavailable()
        report = DeleteReport()
        _deleted, error = await delete_service.do_del_n_counts(
            client, chat_id, n, report=report,
        )
        return "Delete", _report_text(report, error=error), _back_row()

    if head == "all":
        if not chat_id:
            return _no_chat()
        if client is None:
            return _unavailable()
        try:
            owned, scanned = await asyncio.wait_for(
                delete_service.preview_self_owned_count(client, chat_id),
                timeout=_ALL_PREVIEW_TIMEOUT,
            )
        except Exception as exc:  # noqa: BLE001 — the preview is only advisory
            logger.warning("DELETE_UI all preview failed: %s", exc)
            owned, scanned = 0, 0
        builder = InlinePanelBuilder()
        builder.add_row(
            sym.label(sym.OP_DELETE, "Confirm: delete all mine"),
            "panel:del:allexec",
            SUCCESS,
        )
        builder.add_row(sym.label(sym.OP_BACK, "Back"), "panel:del")
        preview_line = (
            f"Preview: {owned} of the newest {scanned} message(s) here are yours; "
            "the scan continues to the start of the chat."
            if scanned
            else "Preview unavailable right now, so the preview was skipped."
        )
        return (
            "Delete",
            "Delete ALL of your messages in this chat.\n\n"
            f"{preview_line} Messages sent by other participants are never "
            "deleted.\n\n"
            f"The operation stops at {MAX_DELETE_COUNT} of your messages or its "
            "deadline, and reports exactly how far it got.",
            builder.build(),
        )

    if head == "allexec":
        if not chat_id:
            return _no_chat()
        if client is None:
            return _unavailable()
        report = DeleteReport()
        progress = _progress_painter(event)
        # Say that work started BEFORE the scan: a large chat must never look
        # frozen while the history is being read.
        await progress({})
        _considered, _deleted, error = await delete_service.do_del_self_filtered(
            client, chat_id, report=report, progress=progress,
        )
        return "Delete", _report_text(report, error=error), _back_row()

    return "Delete", "Choose a deletion scope:", _home_buttons().build()


async def _del_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    return await _del_view(event, extra)


async def _del_inline_builder(event, extra: str) -> list:
    title, body, buttons = await _del_view(event, extra)
    return [render(title, body, buttons)]


# ── Delete From... (recent preview, single vs forward) ──────────────────────


async def _delfrom_recent_page(event, page: int) -> tuple[str, str, list]:
    client = _self_client()
    chat_id = _chat_for(event)
    if not chat_id:
        return _no_chat()
    if client is None:
        return _unavailable()
    try:
        items, has_more = await delete_service.recent_self_message_previews(
            client,
            chat_id,
            offset=page * _RECENT_PAGE_SIZE,
            limit=_RECENT_PAGE_SIZE,
        )
    except Exception as exc:
        logger.warning("DELETE_UI recent page failed: %s", exc)
        return (
            "Delete From...",
            f"{sym.STATE_FAILED} Could not read this chat's history: {exc}",
            _back_row("panel:delfrom"),
        )
    if not items:
        return (
            "Delete From...",
            "No more of your messages in this chat.",
            _methods_buttons().build(),
        )
    builder = InlinePanelBuilder()
    for index, (message_id, preview) in enumerate(
        items, start=page * _RECENT_PAGE_SIZE + 1
    ):
        builder.add_row(
            f"{_circled_num(index)} {preview}",
            f"panel:delfrom:pick:{message_id}",
        )
    nav: list[tuple[str, str]] = []
    if page > 0:
        nav.append((sym.label(sym.OP_PREV, "Previous"), f"panel:delfrom:recent:{page - 1}"))
    if has_more:
        nav.append((f"Next {sym.OP_NEXT}", f"panel:delfrom:recent:{page + 1}"))
    if nav:
        builder.add_buttons(*nav)
    return (
        "Delete From...",
        f"Your recent messages (page {page + 1}) — choose one to act on:",
        builder.build(),
    )


async def _delfrom_pick(event, message_id: int) -> tuple[str, str, list]:
    client = _self_client()
    chat_id = _chat_for(event)
    if not chat_id:
        return _no_chat()
    if client is None:
        return _unavailable()
    preview = ""
    owned = False
    try:
        msg = await client.get_messages(chat_id, ids=message_id)
        text = (getattr(msg, "message", None) or getattr(msg, "text", None) or "")
        preview = " ".join(str(text).split())[:60] or "(no text)"
        owned = bool(getattr(msg, "out", False))
    except Exception as exc:
        logger.warning("DELETE_UI pick preview failed: %s", exc)
    builder = InlinePanelBuilder()
    builder.add_row(
        sym.label(sym.OP_DELETE, "Delete only this message"),
        f"panel:delfrom:one:{message_id}",
    )
    builder.add_row(
        sym.label(sym.OP_DELETE, "Delete from this message forward"),
        f"panel:delfrom:from:{message_id}",
    )
    builder.add_row(sym.label(sym.OP_BACK, "Back to list"), "panel:delfrom:recent:0")
    warning = (
        ""
        if owned
        else f"\n\n{sym.STATE_WARNING} This message was not sent by your account, "
        "so it cannot be deleted."
    )
    return (
        "Delete From...",
        f"Message {message_id}: “{preview}”\n\nChoose what to delete.{warning}",
        builder.build(),
    )


async def _delfrom_confirm(event, message_id: int, *, forward: bool) -> tuple[str, str, list]:
    builder = InlinePanelBuilder()
    verb = "from this message forward" if forward else "only this message"
    action = "fromexec" if forward else "oneexec"
    builder.add_row(
        sym.label(sym.OP_DELETE, "Confirm delete"),
        f"panel:delfrom:{action}:{message_id}",
        SUCCESS,
    )
    builder.add_row(sym.label(sym.OP_BACK, "Back"), f"panel:delfrom:pick:{message_id}")
    return (
        "Confirm Delete",
        f"Delete {verb}, starting at message {message_id}?\n\n"
        "Only messages sent by your account are deleted.",
        builder.build(),
    )


async def _delfrom_exec(event, message_id: int, *, forward: bool) -> tuple[str, str, list]:
    client = _self_client()
    chat_id = _chat_for(event)
    if not chat_id:
        return _no_chat()
    if client is None:
        return _unavailable()
    report = DeleteReport()
    if forward:
        _deleted, error = await delete_service.do_del_id_counts(
            client, chat_id, message_id, report=report,
        )
        return "Delete From Message", _report_text(report, error=error), _back_row("panel:delfrom")

    report.requested = f"message {message_id}"
    report.considered = 1
    error: Exception | None = None
    try:
        deleted, rejected = await delete_service.delete_verified_self_messages(
            client, chat_id, [message_id],
        )
        report.deleted = len(deleted)
        report.rejected = len(rejected)
    except Exception as exc:
        report.complete = False
        report.reason = str(exc)
        error = exc
    return "Delete From Message", _report_text(report, error=error), _back_row("panel:delfrom")


async def _delfrom_view(event, extra: str) -> tuple[str, str, list]:
    head, _, rest = (extra or "").partition(":")
    if head == "recent":
        page = int(rest) if rest.isdigit() else 0
        return await _delfrom_recent_page(event, page)
    if rest.isdigit():
        message_id = int(rest)
        if head == "pick":
            return await _delfrom_pick(event, message_id)
        if head == "one":
            return await _delfrom_confirm(event, message_id, forward=False)
        if head == "from":
            return await _delfrom_confirm(event, message_id, forward=True)
        if head == "oneexec":
            return await _delfrom_exec(event, message_id, forward=False)
        if head == "fromexec":
            return await _delfrom_exec(event, message_id, forward=True)
    return (
        "Delete From...",
        "Choose a method:",
        _methods_buttons().build(),
    )


async def _delfrom_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    return await _delfrom_view(event, extra)


async def _delfrom_inline_builder(event, extra: str) -> list:
    title, body, buttons = await _delfrom_view(event, extra)
    return [render(title, body, buttons)]


# ── Input handlers (count, single ID, ID forward, date range) ──────────────


async def _del_n_input_handler(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id, inline_id=None,
):
    client = _self_client()
    raw = (text or "").strip()
    if not raw.isdigit() or not (1 <= int(raw) <= MAX_DELETE_COUNT):
        await _edit_panel(
            f"{sym.STATE_WARNING} Enter a whole number between 1 and {MAX_DELETE_COUNT}.",
            chat_id=chat_id, msg_id=msg_id,
            inline_chat_id=inline_chat_id, inline_msg_id=inline_msg_id,
            inline_id=inline_id,
        )
        return
    n = int(raw)
    if client is None:
        await _edit_panel(
            f"{sym.STATE_FAILED} The Telegram client is not available right now.",
            chat_id=chat_id, msg_id=msg_id,
            inline_chat_id=inline_chat_id, inline_msg_id=inline_msg_id,
            inline_id=inline_id,
        )
        return
    report = DeleteReport()
    try:
        _deleted, error = await delete_service.do_del_n_counts(
            client, chat_id, n, exclude_message_id=msg_id, report=report,
        )
    finally:
        await _delete_command_message(chat_id, msg_id)
    await _edit_panel(
        _report_text(report, error=error),
        chat_id=chat_id, msg_id=msg_id,
        inline_chat_id=inline_chat_id, inline_msg_id=inline_msg_id,
        inline_id=inline_id,
    )


async def _del_id_input_handler(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id, inline_id=None,
):
    """Delete ONE specific message by ID — no reply context required."""
    client = _self_client()
    raw = (text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await _edit_panel(
            f"{sym.STATE_WARNING} Enter a valid message ID (a positive whole number).",
            chat_id=chat_id, msg_id=msg_id,
            inline_chat_id=inline_chat_id, inline_msg_id=inline_msg_id,
            inline_id=inline_id,
        )
        return
    if client is None:
        await _edit_panel(
            f"{sym.STATE_FAILED} The Telegram client is not available right now.",
            chat_id=chat_id, msg_id=msg_id,
            inline_chat_id=inline_chat_id, inline_msg_id=inline_msg_id,
            inline_id=inline_id,
        )
        return
    message_id = int(raw)
    report = DeleteReport(requested=f"message {message_id}", considered=1)
    error: Exception | None = None
    try:
        deleted, rejected = await delete_service.delete_verified_self_messages(
            client, chat_id, [message_id],
        )
        report.deleted = len(deleted)
        report.rejected = len(rejected)
    except Exception as exc:
        report.complete = False
        report.reason = str(exc)
        error = exc
    finally:
        await _delete_command_message(chat_id, msg_id)
    await _edit_panel(
        _report_text(report, error=error),
        chat_id=chat_id, msg_id=msg_id,
        inline_chat_id=inline_chat_id, inline_msg_id=inline_msg_id,
        inline_id=inline_id,
    )


async def _del_from_input_handler(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id, inline_id=None,
):
    """Delete every one of the owner's messages from an ID forward."""
    client = _self_client()
    raw = (text or "").strip()
    if not raw.isdigit() or int(raw) <= 0:
        await _edit_panel(
            f"{sym.STATE_WARNING} Enter a valid message ID (a positive whole number).",
            chat_id=chat_id, msg_id=msg_id,
            inline_chat_id=inline_chat_id, inline_msg_id=inline_msg_id,
            inline_id=inline_id,
        )
        return
    if client is None:
        await _edit_panel(
            f"{sym.STATE_FAILED} The Telegram client is not available right now.",
            chat_id=chat_id, msg_id=msg_id,
            inline_chat_id=inline_chat_id, inline_msg_id=inline_msg_id,
            inline_id=inline_id,
        )
        return
    report = DeleteReport()
    try:
        _deleted, error = await delete_service.do_del_id_counts(
            client, chat_id, int(raw), exclude_message_id=msg_id, report=report,
        )
    finally:
        await _delete_command_message(chat_id, msg_id)
    await _edit_panel(
        _report_text(report, error=error),
        chat_id=chat_id, msg_id=msg_id,
        inline_chat_id=inline_chat_id, inline_msg_id=inline_msg_id,
        inline_id=inline_id,
    )


_RANGE_SEPARATORS = ("..", ",", " to ", " تا ")


def split_range(text: str) -> tuple[str, str] | None:
    """Split the owner's range line into its two bounds.

    This is a SEPARATOR split only: every bound is parsed by the service's one
    timezone-aware parser (``_parse_cutoff``: ISO, ``today``/``yesterday``, or
    a local HH:MM), so the UI never grows a second time parser.
    """
    raw = (text or "").strip()
    for separator in _RANGE_SEPARATORS:
        if separator in raw:
            start, _, end = raw.partition(separator)
            start, end = start.strip(), end.strip()
            if start and end:
                return start, end
    return None


async def _del_range_input_handler(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id, inline_id=None,
):
    client = _self_client()
    bounds = split_range(text or "")
    if bounds is None:
        await _edit_panel(
            f"{sym.STATE_WARNING} Enter a range as `start .. end` — for example "
            "`2026-08-01 .. 2026-08-20`, `yesterday .. today`, or `09:00 .. 18:00`.",
            chat_id=chat_id, msg_id=msg_id,
            inline_chat_id=inline_chat_id, inline_msg_id=inline_msg_id,
            inline_id=inline_id,
        )
        return
    if client is None:
        await _edit_panel(
            f"{sym.STATE_FAILED} The Telegram client is not available right now.",
            chat_id=chat_id, msg_id=msg_id,
            inline_chat_id=inline_chat_id, inline_msg_id=inline_msg_id,
            inline_id=inline_id,
        )
        return
    start, end = bounds
    report = DeleteReport(requested=f"your messages from {start} to {end}")
    try:
        _considered, _deleted, error = await delete_service.do_del_self_filtered(
            client,
            chat_id,
            after_time=start,
            until_time=end,
            exclude_message_id=msg_id,
            tz_name=_configured_tz(),
            report=report,
        )
    finally:
        await _delete_command_message(chat_id, msg_id)
    await _edit_panel(
        _report_text(report, error=error),
        chat_id=chat_id, msg_id=msg_id,
        inline_chat_id=inline_chat_id, inline_msg_id=inline_msg_id,
        inline_id=inline_id,
    )


# ── Reply mode (the one flow that genuinely starts from a reply) ───────────


async def _delfrom_reply_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    from backend.helper.input_state import set_pending

    if not chat_id:
        return (
            "Delete From...",
            f"{sym.STATE_WARNING} Could not determine the current chat. Please try again.",
            _back_row("panel:delfrom"),
        )

    prompt = (
        "**Delete From — Reply Mode**\n\nReply to any message in this chat.\n"
        "All of your messages from that point forward will be deleted.\n\n"
        "_Reply to a message now._"
    )
    set_pending(
        _owner_id, "delfrom_reply", _delfrom_reply_wait_handler,
        chat_id, prompt,
        inline_chat_id=chat_id,
        inline_msg_id=getattr(event, "message_id", 0) or 0,
    )
    return (
        "Delete From...",
        "Waiting for your reply... Reply to any message to select the starting point.",
        [],
    )


async def _delfrom_reply_wait_handler(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id, inline_id=None,
):
    client = _self_client()
    if client is None:
        await _edit_panel(
            f"{sym.STATE_FAILED} The Telegram client is not available right now.",
            chat_id=chat_id, msg_id=msg_id,
            inline_chat_id=inline_chat_id, inline_msg_id=inline_msg_id,
            inline_id=inline_id,
        )
        return
    error: Exception | None = None
    report = DeleteReport()
    try:
        reply_msg = await client.get_messages(chat_id, ids=msg_id)
        if reply_msg and reply_msg.reply_to_msg_id:
            report.requested = f"your messages from message {reply_msg.reply_to_msg_id} forward"
            _deleted, error = await delete_service.do_del_id_counts(
                client, chat_id, reply_msg.reply_to_msg_id, report=report,
            )
        elif reply_msg and getattr(reply_msg, "out", False):
            report.requested = f"your messages from message {msg_id} forward"
            _deleted, error = await delete_service.do_del_id_counts(
                client, chat_id, msg_id, report=report,
            )
        elif reply_msg:
            await _edit_panel(
                f"{sym.STATE_WARNING} Your message was not a reply. Please reply to "
                "a message to select the starting point.",
                chat_id=chat_id, msg_id=msg_id,
                inline_chat_id=inline_chat_id, inline_msg_id=inline_msg_id,
                inline_id=inline_id,
            )
            return
        else:
            await _edit_panel(
                f"{sym.STATE_WARNING} Could not find your reply message. Please try again.",
                chat_id=chat_id, msg_id=msg_id,
                inline_chat_id=inline_chat_id, inline_msg_id=inline_msg_id,
                inline_id=inline_id,
            )
            return
    except Exception as exc:
        error = exc
        report.complete = False
        report.reason = str(exc)
    await _edit_panel(
        _report_text(report, error=error),
        chat_id=chat_id, msg_id=msg_id,
        inline_chat_id=inline_chat_id, inline_msg_id=inline_msg_id,
        inline_id=inline_id,
    )


# ── registration ───────────────────────────────────────────────────────────


_CIRCLED = "①②③④⑤⑥⑦⑧⑨⑩⑪⑫⑬⑭⑮⑯⑰⑱⑲⑳㉑㉒㉓㉔㉕㉖㉗㉘㉙㉚"


def _circled_num(n: int) -> str:
    if 1 <= n <= len(_CIRCLED):
        return _CIRCLED[n - 1]
    return f"[{n}]"


def register(client, owner_id: int):
    register_panel("del", _del_panel_handler, parent="menu", title="Delete")
    register_panel("delfrom", _delfrom_panel_handler, parent="del", title="Delete from Message")
    register_inline_builder("del", _del_inline_builder)
    register_inline_builder("delfrom", _delfrom_inline_builder)
    register_action("delfrom_reply", _delfrom_reply_action)
    register_input("del", "n", {
        "handler": _del_n_input_handler,
        "prompt": (
            "**Delete Messages**\n\n"
            f"Enter how many of your recent messages to delete (1-{MAX_DELETE_COUNT}):\n\n"
            "_Reply with the number below._"
        ),
    })
    register_input("del", "id", {
        "handler": _del_id_input_handler,
        "prompt": (
            "**Delete One Message**\n\nEnter the message ID to delete "
            "(your own message only):\n\n_Reply with the ID below._"
        ),
    })
    register_input("del", "from", {
        "handler": _del_from_input_handler,
        "prompt": (
            "**Delete From Message ID**\n\nEnter the message ID to start from; "
            "your messages from there forward are deleted:\n\n_Reply with the ID below._"
        ),
        "back": "panel:delfrom",
    })
    register_input("del", "range", {
        "handler": _del_range_input_handler,
        "prompt": (
            "**Delete by Date Range**\n\nEnter `start .. end` — ISO date/time, "
            "`today`, `yesterday`, or a local `HH:MM`:\n\n"
            "_Reply with the range below._"
        ),
    })
