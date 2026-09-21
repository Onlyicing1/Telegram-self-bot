"""
Retrieve — unified file-browser experience (Glass UI only, no dot commands).

Panel IDs (single deterministic workflow):
  retrieve       — Main menu: Saved Items + Retrieve by Code
  retrieve_saved — Paginated Saved Items browser
  retrieve_item  — Item preview panel (full metadata + action buttons)
  retrieve_code  — Manual code entry (secondary path)

Actions:
  retrieve_item     — Retrieve the file to current chat
  retrieve_rename   — Rename the item (input prompt)
  retrieve_move     — Move to a folder (input prompt)
  retrieve_delete   — Delete the item (with confirmation)

Inputs:
  retrieve:code     — Manual save code entry
  retrieve_item:rename — New display name
  retrieve_item:tags   — Add / replace / remove the item's tags
  retrieve_item:move   — Folder name

The item panel is the MANUAL half of the Save V2 Part 4 management contract:
it shows the stored display name and tags and edits them through the same
``retrieve_service`` operations the AI management tools call (do_rename /
do_edit_tags), so the two surfaces can never drift.
"""
import json
import logging
from datetime import datetime

from telethon import events

from backend.ai.persian import normalize_digits
from backend.bot.handlers.guard import is_owner
# The ONE "no tags" vocabulary, shared with the Save panel's metadata step —
# imported rather than re-typed so the two surfaces cannot drift.
from backend.bot.handlers.save import _NO_TAGS_TOKENS
from backend.services import retrieve_service
from backend.db import client as db_client
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
from backend.helper.input_state import set_pending

logger = logging.getLogger(__name__)

_SAVED_PER_PAGE = 8

# The bounded candidate list of the saved-item resolver (Save V2 Part 3).
_MAX_PICK_CANDIDATES = retrieve_service.MAX_RESOLUTION_CANDIDATES


# ── Utility ──

def _parse_extra_id(extra: str) -> str | None:
    """Extract item save_code from extra string like 'id:S0042'."""
    if extra.startswith("id:"):
        return extra[3:]
    return None


# ── Panel: retrieve (main menu) ──

def _retrieve_menu_rows(total: int) -> list:
    builder = InlinePanelBuilder()
    builder.add_row(f"📋 Saved Items ({total})", "panel:retrieve_saved")
    builder.add_row("🔍 Search by Name/Tag", "input:retrieve:search")
    builder.add_row("🔍 Retrieve by Code", "panel:retrieve_code")
    return builder.build()


async def _retrieve_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    total = await db_client.count_saves(_owner_id)
    return (
        "Retrieve",
        "Browse your saved files, search by name/tag, or retrieve by code.",
        _retrieve_menu_rows(total),
    )


async def _retrieve_inline_builder(event, extra: str) -> list:
    from backend.helper.inline_engine import _owner_id
    total = await db_client.count_saves(_owner_id)
    return [
        render(
            "Retrieve",
            "Browse your saved files, search by name/tag, or retrieve by code.",
            _retrieve_menu_rows(total),
        )
    ]


# ── Panel: retrieve_saved (paginated browser) ──

async def _retrieve_saved_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    page = 1
    if extra.startswith("page:"):
        page_str = extra[5:]
        if page_str.isdigit():
            page = max(1, int(page_str))
    per_page = _SAVED_PER_PAGE
    offset = (page - 1) * per_page
    items, total = await db_client.list_saves(_owner_id, limit=per_page, offset=offset)
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page > total_pages:
        page = total_pages
        offset = (page - 1) * per_page
        items, total = await db_client.list_saves(_owner_id, limit=per_page, offset=offset)

    if not items:
        return "Saved Items", "_No saved items found._\n\nSave something first from the LifeOS menu (📥 Save → Deep Save).", []

    body = f"_{total} items · page {page}/{total_pages}_"

    builder = InlinePanelBuilder()
    for item in items:
        code = item.get("save_code")
        if code:
            icon = retrieve_service._type_icon(item)
            name = retrieve_service._display_name(item)
            if len(name) > 20:
                name = name[:17] + "…"
            builder.add_row(f"{icon} {name} · {code}", f"panel:retrieve_item:id:{code}")

    nav_row = []
    if page > 1:
        nav_row.append(("◀ Prev", f"panel:retrieve_saved:page:{page - 1}"))
    nav_row.append((f"{page}/{total_pages}", "panel:_nav:noop"))
    if page < total_pages:
        nav_row.append(("Next ▶", f"panel:retrieve_saved:page:{page + 1}"))
    builder.add_buttons(*nav_row)

    return "Saved Items", body, builder.build()


async def _retrieve_saved_inline_builder(event, extra: str) -> list:
    result = await _retrieve_saved_panel_handler(event, extra)
    if result is None:
        return [render("Saved Items", "No saved items.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


# ── Panel: retrieve_item (preview + actions) ──

async def _retrieve_item_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _owner_id
    code = _parse_extra_id(extra)
    if not code:
        return "Item", "Item not found.", []
    # The persisted row, owner-verified by the service layer — the panel
    # renders stored metadata exactly like the AI preview does.
    row = await retrieve_service.load_saved_item(code, _owner_id)
    if not row:
        return "Item", f"❌ No item found for `{code}`", []

    body = retrieve_service.format_preview(row)
    builder = InlinePanelBuilder()
    builder.add_row("⬇ Retrieve", f"action:retrieve_item_exec:{code}")
    builder.add_row("✏ Rename", f"input:retrieve_item:rename:{code}")
    builder.add_row("🏷 Tags", f"input:retrieve_item:tags:{code}")
    builder.add_row("📂 Move", f"input:retrieve_item:move:{code}")
    builder.add_row("🗑 Delete", f"action:retrieve_item_delete:{code}")
    return "Item Preview", body, builder.build()


async def _retrieve_item_inline_builder(event, extra: str) -> list:
    result = await _retrieve_item_panel_handler(event, extra)
    if result is None:
        return [render("Item", "Not found.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


# ── Panel: retrieve_code (manual entry) ──

async def _retrieve_code_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    builder = InlinePanelBuilder()
    builder.add_row("Enter Code", "input:retrieve:code")
    return "Retrieve by Code", "Enter a save code to preview:", builder.build()


async def _retrieve_code_inline_builder(event, extra: str) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("Enter Code", "input:retrieve:code")
    return [render("Retrieve by Code", "Enter a save code to preview:", builder.build())]


# ── Search by name/tag (Save V2 Part 3 — the shared deterministic resolver) ──

async def _show_result(self_client, chat_id: int, msg_id: int,
                       inline_chat_id: int, inline_msg_id: int,
                       body: str, buttons: list | None = None) -> None:
    """Render a resolver/retrieval result and consume the owner's message.

    Preferred path is editing the inline panel through the helper (the
    existing zero-spam convention). When the helper is unavailable the
    result is delivered as ONE plain message instead of being silently
    dropped, and the owner's input message is always removed when possible.
    """
    helper = get_client()
    edited = False
    if helper and inline_chat_id and inline_msg_id:
        try:
            kwargs = {"buttons": to_edit_buttons(buttons)} if buttons is not None else {}
            await helper.edit_message(inline_chat_id, inline_msg_id, body, **kwargs)
            edited = True
        except Exception as exc:
            logger.warning("retrieve result inline edit failed: %s", exc)
    if not edited and self_client and chat_id:
        try:
            await self_client.send_message(chat_id, body)
        except Exception as exc:
            logger.warning("retrieve result fallback send failed: %s", exc)
    if self_client and chat_id and msg_id:
        try:
            await self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


def _pick_state(codes: list[str]) -> str:
    """Serialize the presented candidate codes into the pending state.

    The state carries the EXACT codes that were shown, in the shown order:
    selection never re-runs a search, so it can never map to a different row.
    """
    return json.dumps(list(codes), separators=(",", ":"))


def _parse_pick_state(extra) -> list[str]:
    """Read the pending selection state (defensive: malformed → no codes)."""
    try:
        codes = json.loads(extra or "[]")
    except (TypeError, ValueError):
        return []
    if not isinstance(codes, list):
        return []
    cleaned = [str(c).strip().upper() for c in codes if str(c).strip().isalnum()]
    return cleaned[:_MAX_PICK_CANDIDATES]


def _pick_index(text: str, count: int) -> int | None:
    """Map an explicit owner selection to a 1-based index (or None).

    Deterministic: a number (ASCII or Persian/Arabic digits) in range, or
    one of the exact presented save codes. Nothing else is accepted.
    """
    value = normalize_digits((text or "").strip()).replace(" ", "")
    if value.isdigit():
        index = int(value)
        return index if 1 <= index <= count else None
    return None


async def _retrieve_search_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id, extra=None):
    from backend.helper.inline_engine import _self_client, _owner_id
    await _resolve_saved_item_request(
        text, _self_client, _owner_id, chat_id, msg_id, inline_chat_id, inline_msg_id
    )


async def _resolve_saved_item_request(query: str, self_client, owner_id: int,
                                      chat_id: int, msg_id: int,
                                      inline_chat_id: int, inline_msg_id: int) -> None:
    """Resolve a name/tag request and act on 0/1/N — never on N.

    0 → honest not-found; 1 → retrieve immediately through the ONE
    do_retrieve authority; N → list the bounded candidates and wait for an
    explicit selection (buttons carry the exact codes, and a pending input
    holds the same codes for a numbered reply). No retrieval happens while
    the result is ambiguous.
    """
    try:
        resolution = await retrieve_service.resolve_saved_items(owner_id, query)
    except Exception as exc:
        await _show_result(
            self_client, chat_id, msg_id, inline_chat_id, inline_msg_id,
            f"❌ Search failed: {exc}",
        )
        return

    if resolution.status == retrieve_service.RESOLUTION_NOT_FOUND:
        builder = InlinePanelBuilder()
        builder.add_row("🔍 Search Again", "input:retrieve:search")
        builder.add_row("‹ Back", "panel:retrieve")
        await _show_result(
            self_client, chat_id, msg_id, inline_chat_id, inline_msg_id,
            retrieve_service.format_resolution(resolution), builder.build(),
        )
        return

    if resolution.status == retrieve_service.RESOLUTION_UNIQUE:
        code = resolution.candidates[0].save_code
        result = await retrieve_service.do_retrieve(self_client, owner_id, code, chat_id)
        await _show_result(
            self_client, chat_id, msg_id, inline_chat_id, inline_msg_id,
            result, _retrieve_item_buttons(code),
        )
        return

    # AMBIGUOUS — nothing is retrieved until the owner chooses.
    codes = [candidate.save_code for candidate in resolution.candidates]
    builder = InlinePanelBuilder()
    for index, candidate in enumerate(resolution.candidates, start=1):
        builder.add_row(
            f"{index}. {retrieve_service.candidate_label(candidate)}",
            f"action:resolve_pick:{candidate.save_code}",
        )
    builder.add_row("🔍 Search Again", "input:retrieve:search")
    builder.add_row("‹ Back", "panel:retrieve")

    set_pending(
        owner_id,
        "retrieve",
        _retrieve_pick_input_handler,
        chat_id or 0,
        "**Which saved item?**\n\nReply with its number:\n\n_Reply below._",
        inline_chat_id=inline_chat_id or 0,
        inline_msg_id=inline_msg_id or 0,
        extra=_pick_state(codes),
        timeout=60.0,
    )
    await _show_result(
        self_client, chat_id, msg_id, inline_chat_id, inline_msg_id,
        retrieve_service.format_resolution(resolution), builder.build(),
    )


async def _retrieve_pick_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id, extra=None):
    """Deterministic numbered selection for an ambiguous resolution.

    The codes come from the pending state that was set when the candidates
    were shown (never from a fresh search). The chosen code is re-verified
    against the owner's stored rows before retrieval — a deleted or foreign
    item fails cleanly with no fallback to any other candidate.
    """
    from backend.helper.inline_engine import _self_client, _owner_id
    owner_id = _owner_id
    codes = _parse_pick_state(extra)
    answer = normalize_digits((text or "").strip()).replace(" ", "")
    if answer.casefold() in ("cancel", "لغو"):
        await _show_result(
            _self_client, chat_id, msg_id, inline_chat_id, inline_msg_id,
            "Cancelled — nothing was retrieved. Search again from 🔍 Retrieve.",
        )
        return
    selection = answer.upper() if answer.upper() in codes else None
    index = _pick_index(text, len(codes))
    if not codes or (selection is None and index is None):
        await _show_result(
            _self_client, chat_id, msg_id, inline_chat_id, inline_msg_id,
            "⚠️ Please reply with the number of the saved item you want (e.g. 2), "
            "or open 🔍 Retrieve to search again. Nothing was retrieved.",
        )
        return
    code = selection or codes[index - 1]
    row = await retrieve_service.load_saved_item(code, owner_id)
    if not row:
        await _show_result(
            _self_client, chat_id, msg_id, inline_chat_id, inline_msg_id,
            f"❌ No item found for `{code}` — it may have been deleted. Nothing was retrieved.",
        )
        return
    result = await retrieve_service.do_retrieve(_self_client, owner_id, code, chat_id)
    await _show_result(
        _self_client, chat_id, msg_id, inline_chat_id, inline_msg_id,
        result, _retrieve_item_buttons(code),
    )


# ── Actions ──

async def _resolve_pick_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Retrieve the candidate the owner clicked — exactly that item.

    The button carries the save code that was presented, so the selection is
    deterministic. The code is re-verified (owner + existence) before the
    single Telegram retrieval; a vanished item fails cleanly with no
    fallback.
    """
    from backend.helper.inline_engine import _self_client, _owner_id
    code = (extra or "").strip().upper()
    builder = InlinePanelBuilder()
    builder.add_row("🔍 Search Again", "input:retrieve:search")
    builder.add_row("‹ Back to Saved Items", "panel:retrieve_saved")
    if not code:
        return "Retrieve", "❌ No item selected. Nothing was retrieved.", builder.build()
    row = await retrieve_service.load_saved_item(code, _owner_id)
    if not row:
        return (
            "Retrieve",
            f"❌ No item found for `{code}` — it may have been deleted. Nothing was retrieved.",
            builder.build(),
        )
    result = await retrieve_service.do_retrieve(_self_client, _owner_id, code, chat_id)
    return "Retrieve", result, _retrieve_item_buttons(code)


async def _retrieve_item_exec_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _self_client, _owner_id
    code = extra.strip()
    if not code:
        return "Retrieve", "❌ No code specified.", []
    if not chat_id:
        logger.error("[RETRIEVE] invalid target chat from callback: chat_id=%r", chat_id)
        return "Retrieve", "❌ Cannot determine target chat.", []
    result = await retrieve_service.do_retrieve(_self_client, _owner_id, code, chat_id)
    row = await db_client.query_save(code)
    if row:
        return "Retrieve", result, _retrieve_item_buttons(code)
    return "Retrieve", result, []


def _retrieve_item_buttons(code: str) -> list:
    builder = InlinePanelBuilder()
    builder.add_row("⬇ Retrieve Again", f"action:retrieve_item_exec:{code}")
    builder.add_row("‹ Back to Item", f"panel:retrieve_item:id:{code}")
    return builder.build()


async def _retrieve_item_delete_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    from backend.helper.inline_engine import _self_client, _owner_id
    code = extra.strip()
    if not code:
        return "Delete", "❌ No code specified.", []
    result = await retrieve_service.do_delete(_self_client, _owner_id, code)
    builder = InlinePanelBuilder()
    builder.add_row("‹ Back to Saved Items", "panel:retrieve_saved")
    return "Delete", result, builder.build()


# ── Input handlers ──

async def _retrieve_code_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.inline_engine import _self_client, _owner_id
    result = await retrieve_service.do_preview(_self_client, _owner_id, text)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("retrieve code inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _retrieve_rename_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id, extra=None):
    from backend.helper.inline_engine import _self_client, _owner_id
    # The input listener pops the pending state BEFORE calling the handler,
    # so the item code must arrive as the ``extra`` argument — reading it back
    # from ``get_pending`` returned an empty state (the documented defect).
    code = (extra or "").strip()
    text_stripped = text.strip()
    if not text_stripped:
        result = "⚠️ Nothing was renamed: send a name for the item."
    elif not code:
        result = "⚠️ No item selected."
    else:
        result = await retrieve_service.do_rename(_owner_id, code, text_stripped)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("rename inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


# ── Tag editing (the item panel's one-line grammar) ──

# One documented grammar, so the panel can never invent a tag operation:
#
#     university, semester-2   → REPLACE the item's tags with this list
#     +university, +semester-2 → ADD these tags
#     -university              → REMOVE these tags
#     - / none / بدون          → remove EVERY tag
#
# Mixing `+` and `-` in one line is refused rather than guessed. The tags
# themselves are normalized by the SHARED save rules inside the service, so
# the panel cannot store a tag a save would have refused.


def parse_tags_line(text: str) -> tuple[str, tuple[str, ...]]:
    """Parse the tag line into ``(operation, tags)`` for ``do_edit_tags``.

    Raises ``ValueError`` with an owner-readable reason; the operation is
    always explicit and never inferred from the tag list.
    """
    line = (text or "").strip()
    if not line:
        raise ValueError("send tags, or `-` to remove every tag")
    if line.casefold() in _NO_TAGS_TOKENS:
        return retrieve_service.TAG_OP_REPLACE, ()
    parts = [p.strip() for p in line.split(",")]
    parts = [p for p in parts if p]
    if not parts:
        raise ValueError("send tags, or `-` to remove every tag")
    added = [p for p in parts if p.startswith("+")]
    removed = [p for p in parts if p.startswith("-")]
    if added and removed:
        raise ValueError("use either `+tag` to add or `-tag` to remove — not both in one line")
    if added:
        return retrieve_service.TAG_OP_ADD, _require_tags(p[1:] for p in added)
    if removed:
        return retrieve_service.TAG_OP_REMOVE, _require_tags(p[1:] for p in removed)
    return retrieve_service.TAG_OP_REPLACE, _require_tags(parts)


def _require_tags(values) -> tuple[str, ...]:
    """Refuse a tag line whose every token is empty after its prefix."""
    tags = tuple(str(v) for v in values)
    if not any(t.strip() for t in tags):
        raise ValueError("send at least one tag")
    return tags


async def _retrieve_tags_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id, extra=None):
    """Apply one explicit tag edit to the item the panel carried in ``extra``.

    Same carry-through contract as rename/move: the input listener pops the
    pending state BEFORE calling this handler, so the item code arrives as
    ``extra`` and is re-verified (owner + existence) inside the service.
    """
    from backend.helper.inline_engine import _self_client, _owner_id
    code = (extra or "").strip()
    if not code:
        result = "⚠️ No item selected. Nothing was changed."
    else:
        try:
            op, tags = parse_tags_line(text)
        except ValueError as exc:
            result = f"⚠️ Nothing was changed: {exc}"
        else:
            result = await retrieve_service.do_edit_tags(_owner_id, code, op, tags)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("tags inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


async def _retrieve_move_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id, extra=None):
    from backend.helper.inline_engine import _self_client, _owner_id
    # Same carry-through contract as rename: the code arrives as ``extra``.
    code = (extra or "").strip()
    text_stripped = text.strip()
    if not code:
        result = "⚠️ No item selected."
    elif not text_stripped:
        result = "⚠️ Folder name cannot be empty. Enter 'unfiled' to remove folder."
    else:
        folder = None if text_stripped.lower() == "unfiled" else text_stripped
        result = await retrieve_service.do_move(_owner_id, code, folder)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, result)
        except Exception as exc:
            logger.warning("move inline edit failed: %s", exc)
    if _self_client:
        try:
            await _self_client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


# ── Registration ──

def register(client, owner_id: int):
    register_panel("retrieve", _retrieve_panel_handler, parent="save", title="🔍 Retrieve")
    register_panel("retrieve_saved", _retrieve_saved_panel_handler, parent="retrieve", title="Saved Items")
    register_panel("retrieve_item", _retrieve_item_panel_handler, parent="retrieve_saved", title="Item Detail")
    register_panel("retrieve_code", _retrieve_code_panel_handler, parent="retrieve", title="Retrieve by Code")
    register_inline_builder("retrieve", _retrieve_inline_builder)
    register_inline_builder("retrieve_saved", _retrieve_saved_inline_builder)
    register_inline_builder("retrieve_item", _retrieve_item_inline_builder)
    register_inline_builder("retrieve_code", _retrieve_code_inline_builder)
    register_action("retrieve_item_exec", _retrieve_item_exec_action)
    register_action("retrieve_item_delete", _retrieve_item_delete_action)
    register_action("resolve_pick", _resolve_pick_action)
    register_input("retrieve", "search", {
        "handler": _retrieve_search_input_handler,
        "prompt": "**Search Saved Items**\n\nEnter a saved name or tag (e.g. `university schedule`):\n\n_Reply below._",
    })
    register_input("retrieve", "code", {
        "handler": _retrieve_code_input_handler,
        "prompt": "**Retrieve by Code**\n\nEnter save code (e.g. S0042):\n\n_Reply with the code below._",
    })
    register_input("retrieve_item", "rename", {
        "handler": _retrieve_rename_input_handler,
        "prompt": "**Rename Item**\n\nEnter the new display name for this saved item:\n\n_Reply below._",
    })
    register_input("retrieve_item", "tags", {
        "handler": _retrieve_tags_input_handler,
        "prompt": (
            "**Edit Tags** — one line\n\n"
            "• `university, semester-2` → replace the tags\n"
            "• `+university` → add tags\n"
            "• `-university` → remove tags\n"
            "• `-` / `none` / `بدون` → remove every tag\n\n"
            "_Reply with the tags below._"
        ),
    })
    register_input("retrieve_item", "move", {
        "handler": _retrieve_move_input_handler,
        "prompt": "**Move Item**\n\nEnter the folder name (or 'unfiled' to remove):\n\n_Reply below._",
    })
