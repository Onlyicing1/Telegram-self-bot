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
import re
import time
import uuid
from collections import OrderedDict
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
from backend.services import (
    appearance_service,
    history_ai_service,
    history_service,
    save_service,
)

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

#: Telegram's own bound for a text message. A panel with Appearance preview is
#: a PHOTO message, so its text is a CAPTION (``appearance_service.MAX_CAPTION_CHARS``);
#: a plain text panel carries this larger bound. The panel's on-screen state
#: decides only how much text the SAME message can take — never its destination.
MAX_RESULT_TEXT_CHARS = 4096

#: A paginated result keeps the original inline panel and its Telegram limits.
#: Only the rendered TEXT changes per page; nothing is created anywhere.
#: A provider result is finite, but these hard caps keep retained page state
#: bounded even if a provider ignores its output budget. Exceeding them is
#: reported as an honest failure on the panel instead of silently dropping text.
MAX_RESULT_PAGES = 64
MAX_RETAINED_RESULT_CHARS = 40_000
_PAGE_STATE_TTL_S = 6 * 60 * 60.0
_PAGE_STATE_MAX = 16

#: Bounded, panel-keyed page state for the CURRENT process. One entry per
#: paginated result; replaced by the next run on the same panel, expired after
#: ``_PAGE_STATE_TTL_S``, and capped at ``_PAGE_STATE_MAX``. A restart (or an
#: expired entry) makes the old page buttons answer honestly instead of showing
#: a page that no longer matches the panel.
_PAGE_STATES: "OrderedDict[str, _PageState]" = OrderedDict()


@dataclass
class _PageState:
    token: str
    chat_id: int
    msg_id: int
    pages: tuple[str, ...]
    created: float
    touched: float
    index: int = 0


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
    chat_id: int, msg_id: int, inline_id: Any, generation: int = 0,
) -> ExistingPanelEditor:
    """The ONE editor for a History panel: the Helper Bot's inline edit.

    The panel is an inline message whose inline-bot identity belongs to the
    Helper Bot, so ``messages.editInlineBotMessage`` (Telethon's
    ``edit_message(InputBotInlineMessageID, ...)``) is the supported edit. The
    owner's self-bot client is deliberately NOT supplied: the account that sent
    an inline-bot result cannot edit it, and substituting that account would
    hide a real ownership failure instead of reporting it.
    """
    return ExistingPanelEditor(
        chat_id=chat_id,
        msg_id=msg_id,
        inline_id=inline_id,
        helper=get_client(),
        self_client=None,
        generation=generation,
    )


async def _paint(editor: ExistingPanelEditor, body: str, buttons: list | None = None) -> bool:
    return await editor.edit(f"**{TITLE}**\n\n{body}", buttons)


def _result_text(body: str) -> str:
    return f"**{TITLE}**\n\n{body}"


def _text_units(text: str) -> int:
    """Telegram counts a message in UTF-16 code units, not Python characters."""
    return len(str(text or "").encode("utf-16-le")) // 2


def _rendered_units(text: str) -> int:
    """UTF-16 units of the text EXACTLY as the panel renderer emits it.

    The Glass UI font can replace Latin letters with non-BMP mathematical
    glyphs (two UTF-16 units each), so a raw character count can understate the
    real Telegram length. Measuring through ``render_edit`` — the same call the
    editor makes before sending — keeps every page within the true limit.
    """
    try:
        from backend.helper.panel_render import render_edit

        rendered, _built = render_edit("", str(text or ""), [])
        return _text_units(rendered)
    except Exception:  # noqa: BLE001 — measurement must never block delivery
        return _text_units(text)


def _page_text(content: str, index: int, total: int) -> str:
    """One page's rendered text, including the title and page indicator.

    The page indicator is part of the SAME message, so its characters are part
    of the Telegram limit the caller must respect.
    """
    header = f"**{TITLE}**\n\n"
    if total <= 1:
        return f"{header}{content}"
    return f"{header}Page {index + 1}/{total}\n\n{content}"


def _pack_units(
    parts: list[str], joiner: str, limit: int, measure: Any,
) -> list[str]:
    """Greedily pack already-splittable pieces into limit-respecting chunks."""
    chunks: list[str] = []
    current = ""
    for part in parts:
        candidate = f"{current}{joiner}{part}" if current else part
        if measure(candidate) <= limit:
            current = candidate
        else:
            if current:
                chunks.append(current)
            current = part
    if current:
        chunks.append(current)
    return chunks


def _markdown_safe_cut(text: str, cut: int) -> int:
    """Back a hard cut out of an unclosed inline-Markdown span when possible.

    Only ``**`` and backticks are considered; a span larger than a whole page
    cannot be kept intact and is split at a word/character boundary instead.
    """
    for marker in ("**", "`"):
        if text[:cut].count(marker) % 2 == 1:
            opening = text.rfind(marker, 0, cut)
            if opening > 0:
                cut = opening
    return cut


def _hard_split(text: str, limit: int, measure: Any) -> list[str]:
    """The last resort: cut by measured units, avoiding a split word/marker."""
    out: list[str] = []
    rest = text
    while rest:
        if measure(rest) <= limit:
            out.append(rest)
            break
        low, high = 1, len(rest)
        while low < high:
            middle = (low + high + 1) // 2
            if measure(rest[:middle]) <= limit:
                low = middle
            else:
                high = middle - 1
        cut = _markdown_safe_cut(rest, low) or low
        out.append(rest[:cut])
        rest = rest[cut:]
    return out


def _split_sentence(text: str, limit: int, measure: Any) -> list[str]:
    if measure(text) <= limit:
        return [text]
    words = text.split(" ")
    if len(words) > 1:
        pieces: list[str] = []
        for word in words:
            pieces.extend(_hard_split(word, limit, measure))
        return _pack_units(pieces, " ", limit, measure)
    return _hard_split(text, limit, measure)


def _split_line(line: str, limit: int, measure: Any) -> list[str]:
    if measure(line) <= limit:
        return [line]
    # Sentence boundaries first (English and common RTL terminators).
    sentences = re.split(r"(?<=[.!?؟…۔])\s+", line)
    if len(sentences) > 1:
        pieces: list[str] = []
        for sentence in sentences:
            pieces.extend(_split_sentence(sentence, limit, measure))
        return _pack_units(pieces, " ", limit, measure)
    return _split_sentence(line, limit, measure)


def _split_block(block: str, limit: int, measure: Any) -> list[str]:
    """Split one paragraph: lines, then sentences, then words, then units."""
    if measure(block) <= limit:
        return [block]
    lines = block.split("\n")
    if len(lines) > 1:
        pieces: list[str] = []
        for line in lines:
            pieces.extend(_split_line(line, limit, measure))
        return _pack_units(pieces, "\n", limit, measure)
    return _split_line(block, limit, measure)


def _split_prefix(text: str, budget: int, measure: Any) -> tuple[str, str]:
    """Largest boundary-safe prefix that fits ``budget``, plus the remainder."""
    if measure(text) <= budget:
        return text, ""
    pieces = _split_block(text, max(32, budget), measure)
    first = pieces[0]
    return first, text[len(first):].lstrip("\n ")


def _split_blocks(body: str, limit: int, measure: Any) -> list[str]:
    """Pack paragraphs into pages, splitting only blocks that cannot fit.

    A block that overflows the page already in progress is split against the
    REMAINING space first, so a short metadata block does not waste page 1 and
    the summary begins on the page the owner receives.
    """
    blocks = [block for block in re.split(r"\n{2,}", body) if block.strip()]
    pages: list[str] = []
    current = ""
    for block in blocks:
        if current:
            remaining = limit - measure(current) - 2
            if measure(block) <= remaining:
                current = f"{current}\n\n{block}"
                continue
            first, remainder = _split_prefix(block, remaining, measure)
            if first.strip():
                current = f"{current}\n\n{first}"
                pages.append(current)
                current = ""
                block = remainder
            else:
                pages.append(current)
                current = ""
        if not block.strip():
            continue
        pieces = (
            [block] if measure(block) <= limit
            else _split_block(block, limit, measure)
        )
        for piece in pieces:
            candidate = f"{current}\n\n{piece}" if current else piece
            if measure(candidate) <= limit:
                current = candidate
            else:
                if current:
                    pages.append(current)
                current = piece
    if current:
        pages.append(current)
    return pages or [""]


def _paginate(body: str, limit: int, measure: Any = _rendered_units) -> list[str]:
    """The body as one or more pages that each fit ``limit`` INCLUDING chrome.

    A body that already fits is returned unchanged (a single page, no paging
    controls). Otherwise paragraphs are packed first and only a paragraph that
    cannot fit is split, so words, lines and most Markdown structure survive.
    """
    if measure(_page_text(body, 0, 1)) <= limit:
        return [body]
    # Reserve the title AND the widest realistic "Page N/N" indicator so the
    # content budget is measured against everything the message will contain.
    reserve = measure(f"**{TITLE}**\n\nPage 99/99\n\n")
    content_limit = max(64, limit - reserve)
    pages = _split_blocks(body, content_limit, measure)
    # The reserve covers "Page 99/99"; verify against the real indicators and
    # shrink deterministically if a pathologically small limit needs it.
    for _ in range(32):
        total = len(pages)
        if all(
            measure(_page_text(page, index, total)) <= limit
            for index, page in enumerate(pages)
        ):
            break
        content_limit = max(48, content_limit - 8)
        pages = _split_blocks(body, content_limit, measure)
    return pages


#: Telegram error → a stable, non-sensitive failure category for the log line.
_EDIT_ERROR_KINDS: dict[str, str] = {
    "MessageAuthorRequiredError": "not_author",
    "ChatAdminRequiredError": "permission",
    "ChatWriteForbiddenError": "permission",
    "MessageDeleteForbiddenError": "permission",
    "MessageIdInvalidError": "invalid_reference",
    "MessageNotModifiedError": "not_modified",
    "MessageTooLongError": "text_too_long",
    "MediaCaptionTooLongError": "caption_too_long",
    "NoEditablePanel": "no_inline_panel_handle",
}


def _edit_failure_kind(error: str) -> str:
    return _EDIT_ERROR_KINDS.get(str(error or ""), "telegram_error")


def _panel_limit(panel_chat_id: int, panel_msg_id: int) -> tuple[int, bool]:
    """The Telegram text limit of THIS panel and whether it is a media panel."""
    state = appearance_service.delivered_state(panel_chat_id, panel_msg_id)
    media_panel = (
        state[0] == "media" if state else appearance_service.preview_active()
    )
    limit = (
        appearance_service.MAX_CAPTION_CHARS if media_panel
        else MAX_RESULT_TEXT_CHARS
    )
    return limit, media_panel


def _forget_panel_pages(panel_chat_id: int, panel_msg_id: int) -> None:
    """Invalidate every retained page state for one panel message."""
    key = (int(panel_chat_id or 0), int(panel_msg_id or 0))
    for token, state in list(_PAGE_STATES.items()):
        if (state.chat_id, state.msg_id) == key:
            _PAGE_STATES.pop(token, None)


def _prune_pages(now: float | None = None) -> None:
    moment = time.monotonic() if now is None else now
    for token, state in list(_PAGE_STATES.items()):
        if moment - state.touched > _PAGE_STATE_TTL_S:
            _PAGE_STATES.pop(token, None)
    while len(_PAGE_STATES) > _PAGE_STATE_MAX:
        _PAGE_STATES.popitem(last=False)


def _remember_pages(
    chat_id: int, msg_id: int, pages: list[str], token: str,
) -> str:
    """Retain one bounded page state for this panel under its emitted token."""
    _prune_pages()
    _forget_panel_pages(chat_id, msg_id)
    moment = time.monotonic()
    _PAGE_STATES[token] = _PageState(
        token=token,
        chat_id=int(chat_id or 0),
        msg_id=int(msg_id or 0),
        pages=tuple(pages),
        created=moment,
        touched=moment,
    )
    while len(_PAGE_STATES) > _PAGE_STATE_MAX:
        _PAGE_STATES.popitem(last=False)
    return token


def _page_state(token: str) -> _PageState | None:
    _prune_pages()
    state = _PAGE_STATES.get(str(token or ""))
    if state is not None:
        _PAGE_STATES.move_to_end(state.token)
    return state


def _paged_buttons(index: int, total: int, token: str) -> list:
    """Page controls for a multi-page result; nothing extra for one page."""
    builder = InlinePanelBuilder()
    if total > 1:
        if index > 0:
            builder.add_row("Previous", f"action:history_page:{token}:{index - 1}")
        if index + 1 < total:
            builder.add_row("Next", f"action:history_page:{token}:{index + 1}")
    builder.add_row("New Summary", f"panel:{PANEL_ID}")
    builder.add_row(sym.label(sym.OP_HOME, "Home"), "panel:_nav:home")
    builder.add_row(sym.label(sym.OP_CLOSE, "Close"), "panel:_nav:close")
    return builder.build()


async def _deliver_result(
    editor: ExistingPanelEditor,
    body: str,
    *,
    panel_chat_id: int,
    panel_msg_id: int,
) -> tuple[bool, str]:
    """Edit the ORIGINAL panel with page 1; never replace or redirect it.

    The panel's on-screen delivery state decides only how much text Telegram
    can carry (photo caption vs text message) — it never changes the
    destination. The Helper Bot performs the edit (``_editor`` builds an
    inline-only editor); a refusal is returned as an honest failure and no
    other panel, chat or message is created.
    """
    limit, media_panel = _panel_limit(panel_chat_id, panel_msg_id)
    pages = _paginate(body, limit)
    total = len(pages)
    retained_chars = sum(_text_units(page) for page in pages)
    if total > MAX_RESULT_PAGES or retained_chars > MAX_RETAINED_RESULT_CHARS:
        logger.warning(
            "HISTORY_RESULT_PAGINATION_UNBOUNDED pages=%s chars=%s",
            total, retained_chars,
        )
        await editor.edit(
            _result_text(
                f"{sym.STATE_FAILED} This summary is too long to page safely "
                f"({retained_chars} characters). Nothing was sent anywhere else."
            ),
            _result_buttons(),
        )
        return False, "result_too_long"
    token = uuid.uuid4().hex[:12] if total > 1 else ""
    text = _page_text(pages[0], 0, total)
    buttons = _paged_buttons(0, total, token) if total > 1 else _result_buttons()
    if await editor.edit(text, buttons):
        if total > 1:
            _remember_pages(panel_chat_id, panel_msg_id, pages, token)
        logger.info(
            "HISTORY_RESULT_DELIVERED path=helper-inline-panel media=%s "
            "pages=%s chars=%s",
            media_panel, total, _text_units(text),
        )
        return True, ""
    error = editor.last_error or "NoEditablePanel"
    kind = _edit_failure_kind(error)
    logger.warning(
        "HISTORY_RESULT_DELIVERY_FAILED reason=helper_panel_edit_failed "
        "error=%s kind=%s", error, kind,
    )
    return False, kind


async def _paint_page_unavailable(
    event: Any, chat_id: int, msg_id: int, reason: str,
) -> None:
    """Say on the SAME panel that its paged result is no longer available."""
    editor = _editor(
        chat_id, msg_id, inline_message_id_from_event(event),
    )
    await editor.edit(
        _result_text(
            f"{sym.STATE_FAILED} These result pages are no longer available "
            f"({reason}). Run the summary again."
        ),
        _result_buttons(),
    )


async def _page_action(event, extra: str, chat_id: int):
    """Navigate one retained result page in the ORIGINAL panel.

    Registered as ``action:history_page:<token>:<index>``. The token binds the
    click to one panel message and one generation of the result; the callback's
    own inline-message handle is the only edit surface. Nothing is created.
    """
    token, _, raw_index = str(extra or "").partition(":")
    state = _page_state(token)
    msg_id = int(getattr(event, "message_id", 0) or 0)
    if state is None:
        logger.info("HISTORY_PAGE_EXPIRED token=%s", str(token)[:12])
        await _paint_page_unavailable(event, chat_id, msg_id, "expired")
        return None
    if state.chat_id != int(chat_id or 0) or (msg_id and state.msg_id != msg_id):
        logger.warning(
            "HISTORY_PAGE_STALE token=%s chat=%s msg=%s",
            str(token)[:12], chat_id, msg_id,
        )
        return None
    try:
        index = int(raw_index)
    except (TypeError, ValueError):
        logger.warning("HISTORY_PAGE_INVALID token=%s", str(token)[:12])
        return None
    total = len(state.pages)
    if not 0 <= index < total:
        logger.warning(
            "HISTORY_PAGE_INVALID token=%s index=%s total=%s",
            str(token)[:12], index, total,
        )
        return None
    editor = _editor(
        chat_id, msg_id or state.msg_id, inline_message_id_from_event(event),
    )
    text = _page_text(state.pages[index], index, total)
    if await editor.edit(text, _paged_buttons(index, total, token)):
        state.index = index
        state.touched = time.monotonic()
        _PAGE_STATES.move_to_end(token)
        logger.info(
            "HISTORY_PAGE_DELIVERED token=%s page=%s/%s",
            str(token)[:12], index + 1, total,
        )
    else:
        logger.warning(
            "HISTORY_PAGE_EDIT_FAILED token=%s error=%s kind=%s",
            str(token)[:12], editor.last_error or "NoEditablePanel",
            _edit_failure_kind(editor.last_error),
        )
    return None


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
        # The complete generated summary is retained; pagination, not a
        # display cap, is what makes it fit Telegram's per-message limit.
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
    editor = _editor(panel_chat_id, panel_msg_id, inline_id, generation)
    # A new run invalidates the previous run's page buttons IMMEDIATELY, so a
    # stale page click can never show a page that no longer matches this panel.
    _forget_panel_pages(panel_chat_id, panel_msg_id)
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
        delivered, kind = await _deliver_result(
            editor,
            body,
            panel_chat_id=panel_chat_id,
            panel_msg_id=panel_msg_id,
        )
        if not delivered:
            logger.warning(
                "HISTORY_RESULT_DELIVERY_FAILED reason=no_safe_panel_edit "
                "kind=%s", kind,
            )
            # The result exists but this panel could not be edited. Say so on
            # the SAME panel through the SAME Helper Bot edit; a failed edit is
            # never reported as a delivered summary, and no other chat or
            # message is ever created.
            await editor.edit(
                _result_text(
                    f"{sym.STATE_FAILED} The summary could not be delivered to "
                    f"this panel ({kind}). Nothing was sent anywhere else."
                ),
                _result_buttons(),
            )
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
    editor = _editor(inline_chat_id or chat_id, inline_msg_id or msg_id, inline_id)
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
    editor = _editor(panel_chat_id, panel_msg_id, inline_id, generation)
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
        editor = _editor(panel_chat_id, panel_msg_id, inline_id)
        await _paint(
            editor,
            f"{sym.STATE_FAILED} {target.error}.",
            _panel_buttons(),
        )
    else:
        manager = _manager()
        if client is None:
            editor = _editor(panel_chat_id, panel_msg_id, inline_id)
            await _paint(
                editor,
                f"{sym.STATE_FAILED} Telegram is not connected right now.",
                _panel_buttons(),
            )
        elif manager is None:
            editor = _editor(panel_chat_id, panel_msg_id, inline_id)
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
    register_action("history_page", _page_action)
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
