"""History & Intelligence — P1 manual and link-based history summaries.

This is the Glass UI surface for the EXISTING history stack:

    Telegram -> history_service -> history_ai_service.summarize_history
             -> the existing ProviderManager

No second summarizer, client, listener, scheduler or provider path exists here.
The panel only chooses a chat, a bounded count and an optional focus, then calls
``summarize_history`` through deterministic application code.

P1 is deliberately manual: there are no checkpoints, no incremental runs, no
automatic/cadence processing and no persistence. The selected chat, count and
focus live in this process's UI state only.
"""
from __future__ import annotations

import asyncio
import logging
from dataclasses import dataclass
from typing import Any

from telethon import utils

from backend.ai.engine.engine import get_engine
from backend.helper import (
    InlinePanelBuilder,
    register_action,
    register_inline_builder,
    register_input,
    register_panel,
    render,
)
from backend.helper import symbols as sym
from backend.helper.client import get_client
from backend.helper.inline_engine import get_self_client
from backend.helper.panel_edit import (
    ExistingPanelEditor,
    begin_panel_operation,
    finish_panel_operation,
    inline_message_id_from_event,
)
from backend.helper.panels import _progress_buttons
from backend.helper.rpc_timeout import rpc_await
from backend.runtime.task_guard import guarded_create_task
from backend.services import history_ai_service, history_service, save_service

logger = logging.getLogger(__name__)

PANEL_ID = "history"
TITLE = "History & Intelligence"

#: The existing per-request bound is authoritative; the UI offers only these.
COUNTS: tuple[int, ...] = tuple(
    value for value in (100, 250, 500, 1000)
    if value <= history_service.MAX_HISTORY_MESSAGES
)
DEFAULT_COUNT = 100 if 100 in COUNTS else (COUNTS[0] if COUNTS else 0)

#: One bounded Telegram dialog page; never an unbounded account walk.
DIALOG_SCAN_LIMIT = 100
DIALOG_PAGE_SIZE = 8
DIALOGS_TIMEOUT_S = 15.0

#: The operation's own wall-clock envelope. The summarizer derives its paced
#: provider budget from the value passed here, and the outer wait_for guarantees
#: the panel operation cannot run forever even if a provider ignores its bound.
OPERATION_TIMEOUT_S = 300.0
LINK_RESOLVE_TIMEOUT_S = 60.0

#: Panel text is bounded so one Telegram edit cannot exceed its message limit.
MAX_SUMMARY_CHARS = 2800


@dataclass
class _State:
    chat_id: int = 0
    chat_name: str = ""
    count: int = DEFAULT_COUNT
    focus: str = ""


@dataclass(frozen=True)
class _Chat:
    peer_id: int
    name: str
    kind: str


_STATE = _State()


def _short(value: Any, limit: int = 32) -> str:
    text = " ".join(str(value or "").split())
    if len(text) <= limit:
        return text
    return text[: max(1, limit - 1)] + "…"


def _chat_kind(dialog: Any, entity: Any) -> str:
    if getattr(dialog, "is_user", False) or type(entity).__name__ == "User":
        return "private"
    if getattr(entity, "megagroup", False):
        return "supergroup"
    if getattr(dialog, "is_channel", False) or getattr(entity, "broadcast", False):
        return "channel"
    if getattr(dialog, "is_group", False):
        return "group"
    return "chat"


def _entity_name(entity: Any) -> str:
    title = getattr(entity, "title", None)
    if isinstance(title, str) and title.strip():
        return _short(title)
    first = getattr(entity, "first_name", None)
    last = getattr(entity, "last_name", None)
    name = " ".join(part for part in (first, last) if isinstance(part, str) and part.strip())
    if name.strip():
        return _short(name)
    username = getattr(entity, "username", None)
    if isinstance(username, str) and username.strip():
        return _short("@" + username.lstrip("@"))
    return "Unnamed chat"


async def _list_chats(client: Any) -> tuple[list[_Chat], str]:
    """The account's own dialogs, bounded, or an honest reason.

    ``get_dialogs(limit=...)`` is the ordinary authenticated listing the account
    already has access to. Nothing is fabricated when Telegram fails, and no
    peer is resolved from an unchecked id.
    """
    if client is None:
        return [], "Telegram is not connected right now."
    try:
        dialogs = await rpc_await(
            client.get_dialogs(limit=DIALOG_SCAN_LIMIT),
            timeout=DIALOGS_TIMEOUT_S,
            label="history.dialogs",
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001 — the panel reports the failure
        logger.warning("HISTORY_DIALOGS_FAILED error=%s", type(exc).__name__)
        return [], f"Telegram's chat list could not be read ({type(exc).__name__})."

    chats: list[_Chat] = []
    seen: set[int] = set()
    for dialog in list(dialogs or [])[:DIALOG_SCAN_LIMIT]:
        entity = getattr(dialog, "entity", None)
        if entity is None:
            continue
        try:
            peer_id = int(utils.get_peer_id(entity))
        except Exception:
            continue
        if not peer_id or peer_id in seen:
            continue
        seen.add(peer_id)
        name = getattr(dialog, "name", None)
        chats.append(
            _Chat(
                peer_id=peer_id,
                name=_short(name) if isinstance(name, str) and name.strip() else _entity_name(entity),
                kind=_chat_kind(dialog, entity),
            )
        )
    return chats, ""


def _panel_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("Select Chat", f"panel:{PANEL_ID}:chats:0")
    builder.add_buttons(
        *[(str(value), f"action:history_count:{value}") for value in COUNTS[:2]]
    )
    if len(COUNTS) > 2:
        builder.add_buttons(
            *[(str(value), f"action:history_count:{value}") for value in COUNTS[2:]]
        )
    builder.add_row("Set Focus Instruction", f"input:{PANEL_ID}:focus")
    builder.add_row("Run Summary", "action:history_run")
    builder.add_row("Summarize Message Link", f"input:{PANEL_ID}:link")
    return builder.build()


def _panel_body(notice: str = "") -> str:
    lines = [
        "Summarize a chat with the existing AI engine. Each run is one-time; nothing is stored.",
        "",
        f"Chat: {_STATE.chat_name or 'not selected'}",
        f"Messages: {_STATE.count}",
        f"Focus: {_STATE.focus or 'none'}",
    ]
    if notice:
        lines.extend(["", notice])
    return "\n".join(lines)


def _panel_view(notice: str = "") -> tuple[str, str, list]:
    return TITLE, _panel_body(notice), _panel_buttons()


async def _chats_view(page_text: str) -> tuple[str, str, list]:
    try:
        page = max(0, int(str(page_text).lstrip(":")))
    except (TypeError, ValueError):
        page = 0
    return await _chats_view_impl(page)


async def _chats_view_impl(page: int) -> tuple[str, str, list]:
    chats, error = await _list_chats(get_self_client())
    builder = InlinePanelBuilder()
    if error:
        body = f"{sym.STATE_FAILED} {error}"
    elif not chats:
        body = "No accessible chats were found for this account."
    else:
        pages = max(1, (len(chats) + DIALOG_PAGE_SIZE - 1) // DIALOG_PAGE_SIZE)
        page = min(page, pages - 1)
        start = page * DIALOG_PAGE_SIZE
        visible = chats[start:start + DIALOG_PAGE_SIZE]
        body = f"Choose a chat (page {page + 1} of {pages}, first {DIALOG_SCAN_LIMIT} dialogs scanned)."
        for chat in visible:
            builder.add_row(
                f"{_short(chat.name, 34)} · {chat.kind}",
                f"panel:{PANEL_ID}:pick:{chat.peer_id}",
            )
        if page > 0:
            builder.add_row("Previous", f"panel:{PANEL_ID}:chats:{page - 1}")
        if page + 1 < pages:
            builder.add_row("Next", f"panel:{PANEL_ID}:chats:{page + 1}")
    builder.add_row("Back to History", f"panel:{PANEL_ID}")
    return TITLE, body, builder.build()


async def _panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    if extra.startswith("chats:"):
        return await _chats_view(extra.split(":", 1)[1])
    if extra.startswith("pick:"):
        return await _pick_chat(extra.split(":", 1)[1])
    return _panel_view()


async def _pick_chat(raw: str) -> tuple[str, str, list]:
    try:
        peer_id = int(raw)
    except (TypeError, ValueError):
        return _panel_view(f"{sym.STATE_FAILED} That chat selection is not valid.")
    chats, error = await _list_chats(get_self_client())
    if error:
        return _panel_view(f"{sym.STATE_FAILED} {error}")
    chat = next((item for item in chats if item.peer_id == peer_id), None)
    if chat is None:
        return _panel_view(
            f"{sym.STATE_FAILED} That chat is no longer in the accessible list. Select it again."
        )
    _STATE.chat_id = chat.peer_id
    _STATE.chat_name = chat.name
    return _panel_view(f"Selected: {chat.name}")


async def _count_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    try:
        value = int(extra)
    except (TypeError, ValueError):
        value = -1
    if value not in COUNTS:
        return _panel_view(f"{sym.STATE_FAILED} Choose one of the offered message counts.")
    _STATE.count = value
    return _panel_view(f"Message count set to {value}.")


def _manager() -> Any:
    """The existing runtime provider manager, or None. Never constructs a new one."""
    try:
        return get_engine().provider_manager
    except Exception as exc:  # noqa: BLE001 — reported by the panel
        logger.warning("HISTORY_PROVIDER_MANAGER_FAILED error=%s", type(exc).__name__)
        return None


def _editor(
    chat_id: int, msg_id: int, inline_id: Any, client: Any, generation: int = 0,
) -> ExistingPanelEditor:
    return ExistingPanelEditor(
        chat_id=chat_id,
        msg_id=msg_id,
        inline_id=inline_id,
        helper=get_client(),
        self_client=client,
        generation=generation,
    )


async def _paint(editor: ExistingPanelEditor, body: str, buttons: list | None = None) -> bool:
    return await editor.edit(f"**{TITLE}**\n\n{body}", buttons)


def _range_line(data: dict[str, Any]) -> str:
    first = data.get("first_message_id")
    last = data.get("last_message_id")
    if first is None or last is None:
        return "Range: no eligible messages"
    return f"Range: #{first} – #{last}"


def _history_line(data: dict[str, Any]) -> str:
    processed = data.get("processed") or 0
    if not processed:
        return "History: empty."
    if data.get("capped"):
        return (
            f"History: bounded to {history_service.MAX_HISTORY_MESSAGES} messages "
            "(the per-request maximum)."
        )
    if data.get("truncated"):
        return f"History: bounded at {processed} messages; older eligible messages remain."
    return "History: complete for this requested window."


def _summary_body(
    text: str, data: dict[str, Any], *, chat_name: str, ending_at: int | None = None,
) -> str:
    processed = data.get("processed")
    requested = data.get("requested")
    lines = [
        f"Chat: {chat_name}",
        f"Processed: {processed if processed is not None else 'unknown'}"
        + (f" of {requested} requested" if requested is not None else ""),
        _range_line(data),
    ]
    if ending_at is not None:
        lines.append(f"Ending at: #{ending_at}")
    lines.append(_history_line(data))
    body = "\n".join(lines)
    summary = str(text or "").strip()
    if summary:
        if len(summary) > MAX_SUMMARY_CHARS:
            summary = summary[:MAX_SUMMARY_CHARS].rstrip() + "\n\n… (summary truncated for display)"
        body = f"{body}\n\n{summary}"
    return body


def _result_buttons() -> list:
    builder = InlinePanelBuilder()
    builder.add_row("New Summary", f"panel:{PANEL_ID}")
    builder.add_row(sym.label(sym.OP_HOME, "Home"), "panel:_nav:home")
    builder.add_row(sym.label(sym.OP_CLOSE, "Close"), "panel:_nav:close")
    return builder.build()


async def _run_summary(
    client: Any,
    manager: Any,
    *,
    chat_id: int,
    chat_name: str,
    count: int,
    focus: str,
    panel_chat_id: int,
    panel_msg_id: int,
    inline_id: Any,
    generation: int,
    current_message_id: int | None = None,
    ending_at: int | None = None,
) -> None:
    editor = _editor(panel_chat_id, panel_msg_id, inline_id, client, generation)
    try:
        await _paint(
            editor,
            f"Summarizing {chat_name} ({count} messages)…",
            _progress_buttons(),
        )
        try:
            ok, text, data = await asyncio.wait_for(
                history_ai_service.summarize_history(
                    client,
                    chat_id,
                    count=count,
                    instruction=focus or None,
                    provider_manager=manager,
                    current_message_id=current_message_id,
                    request_id="glass-history",
                    timeout_s=OPERATION_TIMEOUT_S,
                    with_identifiers=False,
                ),
                timeout=OPERATION_TIMEOUT_S,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            ok, text, data = (
                False,
                f"{sym.STATE_FAILED} Summarization timed out after {OPERATION_TIMEOUT_S:.0f}s.",
                {"error": "operation_timeout"},
            )
        except Exception as exc:  # noqa: BLE001 — never fabricate a result
            logger.warning("HISTORY_SUMMARY_FAILED error=%s", type(exc).__name__)
            ok, text, data = (
                False,
                f"{sym.STATE_FAILED} Summarization failed ({type(exc).__name__}).",
                {"error": "execution_failed"},
            )

        if ok:
            body = _summary_body(
                text, data, chat_name=chat_name, ending_at=ending_at,
            )
        else:
            body = (
                f"Chat: {chat_name}\n{text or (sym.STATE_FAILED + ' Summarization failed.')}"
            )
        await _paint(editor, body, _result_buttons())
    finally:
        finish_panel_operation(panel_chat_id, panel_msg_id, generation)


async def _validated_chat(client: Any) -> str:
    if not _STATE.chat_id:
        return "Select a chat first."
    chats, error = await _list_chats(client)
    if error:
        return error
    if not any(chat.peer_id == _STATE.chat_id for chat in chats):
        return "The selected chat is no longer accessible. Select it again."
    return ""


async def _run_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    client = get_self_client()
    problem = await _validated_chat(client)
    if problem:
        return _panel_view(f"{sym.STATE_FAILED} {problem}")
    manager = _manager()
    if manager is None:
        return _panel_view(f"{sym.STATE_FAILED} The AI engine is not running.")
    panel_msg_id = int(getattr(event, "message_id", 0) or 0)
    generation = begin_panel_operation(chat_id, panel_msg_id)
    snapshot = _State(
        chat_id=_STATE.chat_id,
        chat_name=_STATE.chat_name,
        count=_STATE.count,
        focus=_STATE.focus,
    )
    try:
        guarded_create_task(
            _run_summary(
                client,
                manager,
                chat_id=snapshot.chat_id,
                chat_name=snapshot.chat_name,
                count=snapshot.count,
                focus=snapshot.focus,
                panel_chat_id=chat_id,
                panel_msg_id=panel_msg_id,
                inline_id=inline_message_id_from_event(event),
                generation=generation,
            ),
            name="history:summarize",
        )
    except Exception as exc:  # noqa: BLE001 — launching is the only thing left
        finish_panel_operation(chat_id, panel_msg_id, generation)
        logger.warning("HISTORY_LAUNCH_FAILED error=%s", type(exc).__name__)
        return _panel_view(f"{sym.STATE_FAILED} Could not start the summary.")
    return None


def _normalize_focus(value: Any) -> str | None:
    text = " ".join(str(value or "").split())
    if len(text) > history_ai_service.MAX_INSTRUCTION_CHARS:
        return None
    return text


async def _focus_input_handler(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id, inline_id=None,
):
    value = _normalize_focus(text)
    if value is None:
        notice = (
            f"{sym.STATE_FAILED} Focus instructions must be "
            f"{history_ai_service.MAX_INSTRUCTION_CHARS} characters or fewer. "
            f"Still: {_STATE.focus or 'none'}."
        )
    else:
        _STATE.focus = value
        notice = "Focus: " + (value or "cleared")
    client = get_self_client()
    editor = _editor(inline_chat_id or chat_id, inline_msg_id or msg_id, inline_id, client)
    await _paint(editor, _panel_body(notice), _panel_buttons())
    if client is not None and msg_id:
        try:
            await client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


def _link_error_text(exc: save_service.LinkResolutionError) -> str:
    return save_service._link_failure_text(exc)


async def _resolve_link(
    client: Any, target: save_service.LinkTarget,
) -> tuple[Any | None, str]:
    try:
        message = await asyncio.wait_for(
            save_service._fetch_linked_message(client, target),
            timeout=LINK_RESOLVE_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except save_service.LinkResolutionError as exc:
        return None, _link_error_text(exc)
    except asyncio.TimeoutError:
        return None, f"{sym.STATE_FAILED} Resolving the link timed out."
    except Exception as exc:  # noqa: BLE001 — an honest resolution failure
        logger.warning("HISTORY_LINK_RESOLVE_FAILED error=%s", type(exc).__name__)
        return None, f"{sym.STATE_FAILED} Could not resolve that link ({type(exc).__name__})."
    return message, ""


async def _run_link(
    client: Any,
    manager: Any,
    target: save_service.LinkTarget,
    *,
    count: int,
    focus: str,
    panel_chat_id: int,
    panel_msg_id: int,
    inline_id: Any,
    generation: int,
) -> None:
    editor = _editor(panel_chat_id, panel_msg_id, inline_id, client, generation)
    try:
        await _paint(editor, "Resolving the message link…", _progress_buttons())
        message, error = await _resolve_link(client, target)
        if message is None:
            await _paint(editor, error, _result_buttons())
            return

        actual_id = int(getattr(message, "id", 0) or 0)
        actual_chat = int(getattr(message, "chat_id", 0) or 0)
        if actual_id != target.message_id:
            await _paint(
                editor,
                f"{sym.STATE_FAILED} The link did not resolve to the requested message.",
                _result_buttons(),
            )
            return
        if target.chat_id and actual_chat != target.chat_id:
            await _paint(
                editor,
                f"{sym.STATE_FAILED} The link did not resolve to the requested chat.",
                _result_buttons(),
            )
            return
        if not actual_chat:
            await _paint(
                editor,
                f"{sym.STATE_FAILED} The linked message has no readable chat.",
                _result_buttons(),
            )
            return

        chat_name = _entity_name(getattr(message, "chat", None))
        if chat_name == "Unnamed chat" and target.username:
            chat_name = _short("@" + target.username.lstrip("@"))
        # The history API's upper bound is EXCLUSIVE: linked_id + 1 includes the
        # linked message itself and excludes everything newer than it.
        await _run_summary(
            client,
            manager,
            chat_id=actual_chat,
            chat_name=chat_name,
            count=count,
            focus=focus,
            panel_chat_id=panel_chat_id,
            panel_msg_id=panel_msg_id,
            inline_id=inline_id,
            generation=generation,
            current_message_id=target.message_id + 1,
            ending_at=target.message_id,
        )
    finally:
        finish_panel_operation(panel_chat_id, panel_msg_id, generation)


async def _link_input_handler(
    text, chat_id, msg_id, inline_chat_id, inline_msg_id, inline_id=None,
):
    target = save_service.parse_link_target(text)
    client = get_self_client()
    panel_chat_id = inline_chat_id or chat_id
    panel_msg_id = inline_msg_id or msg_id
    if target.error:
        editor = _editor(panel_chat_id, panel_msg_id, inline_id, client)
        await _paint(
            editor,
            f"{sym.STATE_FAILED} {target.error}.",
            _panel_buttons(),
        )
    else:
        manager = _manager()
        if client is None:
            editor = _editor(panel_chat_id, panel_msg_id, inline_id, client)
            await _paint(
                editor,
                f"{sym.STATE_FAILED} Telegram is not connected right now.",
                _panel_buttons(),
            )
        elif manager is None:
            editor = _editor(panel_chat_id, panel_msg_id, inline_id, client)
            await _paint(
                editor,
                f"{sym.STATE_FAILED} The AI engine is not running.",
                _panel_buttons(),
            )
        else:
            generation = begin_panel_operation(panel_chat_id, panel_msg_id)
            try:
                guarded_create_task(
                    _run_link(
                        client,
                        manager,
                        target,
                        count=_STATE.count,
                        focus=_STATE.focus,
                        panel_chat_id=panel_chat_id,
                        panel_msg_id=panel_msg_id,
                        inline_id=inline_id,
                        generation=generation,
                    ),
                    name="history:link-summarize",
                )
            except Exception as exc:  # noqa: BLE001
                finish_panel_operation(panel_chat_id, panel_msg_id, generation)
                logger.warning("HISTORY_LINK_LAUNCH_FAILED error=%s", type(exc).__name__)
    if client is not None and msg_id:
        try:
            await client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _inline_builder(event, extra: str) -> list:
    result = await _panel_handler(event, extra)
    title, body, buttons = result if result is not None else (TITLE, "", [])
    return [render(title, body, buttons)]


def register(client=None, owner_id: int = 0) -> None:
    """Register the History & Intelligence panel. No listener is added."""
    register_panel(PANEL_ID, _panel_handler, parent="menu", title=TITLE)
    register_inline_builder(PANEL_ID, _inline_builder)
    register_action("history_count", _count_action)
    register_action("history_run", _run_action)
    register_input(
        PANEL_ID,
        "focus",
        {
            "handler": _focus_input_handler,
            "prompt": (
                "**Focus Instruction**\n\n"
                f"Optional. Up to {history_ai_service.MAX_INSTRUCTION_CHARS} characters. "
                "Send an empty message to clear.\n\n_Reply below._"
            ),
            "back": f"panel:{PANEL_ID}",
        },
    )
    register_input(
        PANEL_ID,
        "link",
        {
            "handler": _link_input_handler,
            "prompt": (
                "**Summarize Message Link**\n\n"
                "Send a Telegram message link:\n"
                "https://t.me/<username>/<id>\n"
                "https://t.me/c/<chat>/<id>\n"
                "tg://openmessage?user_id=<id>&message_id=<id>\n\n_Reply below._"
            ),
            "back": f"panel:{PANEL_ID}",
        },
    )
