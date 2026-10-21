"""Anti-Edit / Anti-Delete — Glass UI panels + the capture event handlers.

Registers, on the ONE shared self-client (through ``backend.bot.router``):

  * an ``events.NewMessage`` handler that records a bounded snapshot of an
    eligible private message WHILE its content is present — this snapshot is
    what makes a later deletion recoverable at all;
  * an ``events.MessageEdited`` handler that records each edit version;
  * an ``events.MessageDeleted`` handler that records the deletion event and
    marks it recoverable ONLY when the content was captured beforehand.

Telegram BOT accounts are excluded at every capture boundary: before a message,
an edit, or a deletion is recorded, the event's own chat entity is classified
through the REAL ``User.bot`` flag (``service.is_bot_entity``) — never a
username/name/id heuristic — and a confirmed bot peer is dropped BEFORE the
service scope gate, so no archive row can be created for bot chats. A peer whose
entity is missing or cannot be resolved is NOT classified as a human either:
behaviour falls back to the existing private-chat rules and that limitation is
recorded in the archive reports. Persisted rows that predate this exclusion are
hidden from the listing when their peer resolves to a bot at browse time
(``service.filter_bot_items``), using the live self client; rows that cannot be
resolved stay visible (the existing safe fallback).

Everything is registered LAST so a capture read never delays another handler's
first claim on an update; a capture that cannot finish is an honest miss, never
a block on unrelated behaviour. No second client, update loop, scheduler or
executor is introduced: the archive expires through the EXISTING memory-cleanup
worker, which calls ``backend.services.anti_edit_delete_service.cleanup_expired``.

Business logic and persistence live in the service; these handlers are thin.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.helper import (
    InlinePanelBuilder,
    register_action,
    register_inline_builder,
    register_input,
    register_panel,
    render,
)
from backend.helper import inline_engine
from backend.helper import symbols as sym
from backend.helper.styles import DANGER, PRIMARY
from backend.services import anti_edit_delete_service as service

logger = logging.getLogger(__name__)

PANEL_ID = "archive"
LIST_ID = "archive_list"
ITEM_ID = "archive_item"
CATS_ID = "archive_cats"
CONFIRM_ID = "archive_confirm"

_PAGE_SIZE = service.PAGE_SIZE
_BODY_PREVIEW = 160

_CATEGORY_LABELS = {
    service.CATEGORY_TEXT: "Text",
    service.CATEGORY_CAPTIONS: "Captions",
    service.CATEGORY_PHOTOS: "Photos",
    service.CATEGORY_VIDEOS: "Videos",
    service.CATEGORY_VOICE: "Voice",
    service.CATEGORY_DOCUMENTS: "Documents",
    service.CATEGORY_OTHER: "Other",
}


# ── Rendering helpers ────────────────────────────────────────────────────────

def _mark(value: bool) -> str:
    return sym.STATE_COMPLETED if value else sym.STATE_FAILED


def _category_glyph(category: str) -> str:
    return {
        service.CATEGORY_TEXT: sym.MEDIA_TEXT,
        service.CATEGORY_CAPTIONS: sym.MEDIA_TEXT,
        service.CATEGORY_PHOTOS: sym.MEDIA_PHOTO,
        service.CATEGORY_VIDEOS: sym.MEDIA_VIDEO,
        service.CATEGORY_VOICE: sym.MEDIA_VOICE,
        service.CATEGORY_DOCUMENTS: sym.MEDIA_DOCUMENT,
        service.CATEGORY_OTHER: sym.MEDIA_UNKNOWN,
    }.get(category, sym.MEDIA_UNKNOWN)


def _kind_glyph(kind: str) -> str:
    return {
        service.KIND_MESSAGE: sym.OP_SAVE,
        service.KIND_SNAPSHOT: sym.OP_SAVE,
        service.KIND_EDIT: sym.OP_EDIT,
        service.KIND_DELETE: sym.STATE_DELETED,
    }.get(kind, sym.STATE_DELETED)


async def _resolve_peer_identity(event, chat_id: Any) -> tuple[dict | None, bool]:
    """The OTHER participant's bounded identity, from the event's own chat entity.

    In a private chat the chat entity IS the peer, so this never confuses the
    message author with the conversation peer (an outgoing message's ``sender_id``
    is the OWNER, while the peer is the positive ``chat_id``). The entity is read
    from Telethon's own in-memory cache — ``get_chat()`` only performs an RPC when
    the entity is missing from the session cache, and any failure degrades to the
    numeric peer id rather than blocking capture. Never raises.

    Returns ``(identity, peer_is_bot)``. ``peer_is_bot`` is True ONLY when the
    resolved entity is a Telegram ``User`` with the real ``bot`` flag set
    (``service.is_bot_entity``); a missing or unresolved entity yields False =
    UNKNOWN — it is never guessed to be a human, and those captures keep the
    existing safe fallback (documented limitation).
    """
    if not isinstance(chat_id, int) or isinstance(chat_id, bool) or chat_id <= 0:
        return None, False
    identity: dict = {"peer_id": chat_id, "username": None, "name": None}
    try:
        entity = await event.get_chat()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.debug("[AED] peer entity unresolved: %s", type(exc).__name__)
        return identity, False
    if entity is None:
        return identity, False
    peer_is_bot = service.is_bot_entity(entity)
    # A private chat's entity is a User; never read a username off a non-user.
    if type(entity).__name__ == "User":
        identity["username"] = getattr(entity, "username", None)
        first = getattr(entity, "first_name", None) or ""
        last = getattr(entity, "last_name", None) or ""
        identity["name"] = f"{first} {last}".strip() or None
    return identity, peer_is_bot


def _truncate(value: str, limit: int = _BODY_PREVIEW) -> str:
    text = (value or "").strip().replace("\n", " ")
    return text if len(text) <= limit else text[: limit - 1] + "…"


def _parse_list_extra(extra: str) -> tuple[int, str, str]:
    """Decode ``p=<page>&c=<category>&q=<query>`` from panel navigation state."""
    from urllib.parse import unquote

    page, category, query = 1, "", ""
    for part in (extra or "").split("&"):
        key, separator, value = part.partition("=")
        if not separator:
            continue
        if key == "p" and value.isdigit():
            page = max(1, int(value))
        elif key == "c":
            category = unquote(value)
        elif key == "q":
            query = unquote(value)
    return page, category, query


def _encode_list_extra(page: int, category: str, query: str) -> str:
    from urllib.parse import quote

    return f"p={page}&c={quote(category, safe='')}&q={quote(query, safe='')}"


async def _render_main(owner_id: int) -> tuple[str, str, list]:
    await service.ensure_loaded_async()
    summary = service.summary()
    body = [
        f"{sym.PANEL_HEADING} Private conversations are archived when enabled.",
        "",
        f"{_mark(summary['enabled'])} Enabled",
        f"{sym.ICON_CATEGORY} Categories: {len(summary['categories'])}/{len(service.CATEGORIES)}",
        f"{sym.SCHEDULE_INTERVAL} Retention: {summary['retention_days']} day(s)",
        f"{_mark(summary['media_storage'])} Durable media storage"
        + ("" if summary["media_storage"] else " (metadata only)"),
        "",
        "A deletion is recoverable only if the message was captured while present.",
    ]
    builder = InlinePanelBuilder()
    builder.add_row(
        sym.label(sym.OP_PAUSE if summary["enabled"] else sym.OP_RESUME,
                  "Disable archiving" if summary["enabled"] else "Enable archiving"),
        "action:aed_toggle_enabled",
        PRIMARY,
    )
    builder.add_row(sym.label(sym.OP_MANAGE, "Browse archive"), f"panel:{LIST_ID}", PRIMARY)
    builder.add_row(sym.label(sym.ICON_CATEGORY, "Categories"), f"panel:{CATS_ID}", PRIMARY)
    builder.add_row(sym.label(sym.SCHEDULE_INTERVAL, "Set retention"), f"input:{PANEL_ID}:retention", PRIMARY)
    builder.add_row(sym.label(sym.OP_REFRESH, "Refresh"), "action:aed_refresh", PRIMARY)
    builder.add_row(sym.label(sym.OP_DELETE, "Clear archive"), f"panel:{CONFIRM_ID}", DANGER)
    return "Anti-Edit / Anti-Delete", "\n".join(body), builder.build()


async def _render_categories(owner_id: int) -> tuple[str, str, list]:
    await service.ensure_loaded_async()
    configured = set(service.categories())
    builder = InlinePanelBuilder()
    for category in service.CATEGORIES:
        builder.add_row(
            sym.label(sym.OP_SAVE if category in configured else sym.OP_CREATE,
                      _CATEGORY_LABELS[category]),
            f"action:aed_category:{category}",
            PRIMARY,
        )
    body = (
        f"{sym.ICON_CATEGORY} Choose the content categories to archive.\n\n"
        "At least one category must stay enabled."
    )
    return "Archive Categories", body, builder.build()


async def _render_list(owner_id: int, extra: str) -> tuple[str, str, list]:
    page, category, query = _parse_list_extra(extra)
    rows = await service.load_rows_async()
    if rows is None:
        return "Archive", f"{sym.STATE_WARNING} The archive store could not be read.", []
    items = service.build_items(rows)
    filtered = service.filter_items(items, category=category, query=query)
    filtered, bots_hidden = await service.filter_bot_items(
        inline_engine.get_self_client(), filtered
    )
    page_items, page, pages = service.page_of(filtered, page)
    builder = InlinePanelBuilder()
    if not page_items:
        builder.add_row(sym.label(sym.OP_CREATE, "Nothing archived yet"), "action:aed_noop", PRIMARY)
    for item in page_items:
        label = _item_label(item)
        builder.add_row(
            label,
            f"panel:{ITEM_ID}:{item.get('id')}",
            PRIMARY,
        )
    if pages > 1:
        builder.add_buttons(
            (sym.label(sym.OP_PREV, "Prev"), f"action:aed_page:{max(1, page - 1)}", PRIMARY),
            (f"{sym.PAGE_CURRENT} {page}/{pages}", "action:aed_noop", PRIMARY),
            (sym.label(sym.OP_NEXT, "Next"), f"action:aed_page:{min(pages, page + 1)}", PRIMARY),
        )
    active = category or "all"
    note = (
        f"\n{sym.STATE_WARNING} {bots_hidden} persisted item(s) hidden: "
        "the peer resolved as a Telegram bot."
        if bots_hidden else ""
    )
    body = (
        f"{sym.ICON_STATS} {len(filtered)} item(s) · filter: {active} · page {page}/{pages}\n\n"
        "Each message is ONE item; select it to view its versions or delete it."
    ) + note
    return "Archive", body, builder.build()


def _item_label(item: dict) -> str:
    """One bounded list button. Grouped messages show their version count so the
    owner can see history is preserved; a deletion event is marked as such."""
    kind = item.get("kind")
    preview = _truncate(item.get("body") or item.get("media_name") or "(no content)")
    if kind == service.KIND_DELETE:
        state = "recoverable" if item.get("recoverable") else "not recoverable"
        return f"{_kind_glyph(kind)} {_category_glyph(item.get('category'))} Deleted · {state} · {preview}"
    count = int(item.get("version_count") or 1)
    history = f" · {count} versions" if count > 1 else ""
    deleted = " · deleted" if item.get("has_deletion") else ""
    return f"{_kind_glyph(kind)} {_category_glyph(item.get('category'))}{history}{deleted} {preview}"


def _identity_line(identity: Any) -> str:
    identity = identity if isinstance(identity, dict) else {}
    parts = []
    if identity.get("username"):
        parts.append(f"@{identity['username']}")
    if identity.get("name"):
        parts.append(identity["name"])
    peer = identity.get("peer_id")
    parts.append(f"User ID: {peer if peer is not None else 'unknown'}")
    return f"{sym.META_SENDER} User: " + " · ".join(parts)


async def _render_item(owner_id: int, extra: str) -> tuple[str, str, list]:
    browse_id = (extra or "").strip()
    rows = await service.load_rows_async()
    if rows is None:
        return "Archive Item", f"{sym.STATE_WARNING} The archive store could not be read.", []
    items = service.build_items(rows)
    items, _bots_hidden = await service.filter_bot_items(
        inline_engine.get_self_client(), items
    )
    item = service.get_item(items, browse_id)
    if item is None:
        return "Archive Item", f"{sym.STATE_WARNING} That item is no longer in the archive.", []
    return _render_item_body(item)


def _render_item_body(item: dict) -> tuple[str, str, list]:
    kind = item.get("kind")
    is_delete = kind == service.KIND_DELETE
    body = [
        f"{_kind_glyph(kind)} Kind: {'deleted' if is_delete else 'message'}",
        f"{_category_glyph(item.get('category'))} Category: {item.get('category')}",
        f"{sym.OP_CHAT} Chat ID: {item.get('chat_id')} · Message ID: {item.get('message_id')}",
        _identity_line(item.get("identity")),
        f"{sym.META_TIME} Captured: {item.get('captured_at')}",
    ]
    versions = item.get("versions") or []
    if not is_delete and item.get("has_deletion"):
        body.append(f"{sym.STATE_DELETED} This message was also observed as deleted.")
    if is_delete:
        if item.get("recoverable"):
            body.append(f"{sym.STATE_COMPLETED} Recoverable from a pre-deletion snapshot.")
        else:
            body.append(f"{sym.STATE_WARNING} Content was never captured — not recoverable.")
    if item.get("has_media"):
        body.append(
            f"{sym.META_MEDIA} Media: {item.get('media_type') or 'unknown'}"
            + (f" · {item.get('media_name')}" if item.get("media_name") else "")
            + ("" if item.get("media_stored") else " (metadata only)")
        )
    if is_delete:
        if item.get("body"):
            body.append("")
            body.append(_truncate(item.get("body"), 600))
    else:
        body.append("")
        body.append(f"{sym.ICON_STATS} {len(versions)} observed version(s) of this message.")
        # Number chronologically so v1 is the original and later edits follow.
        for index, version in enumerate(reversed(versions), start=1):
            when = version.get("edited_at") or version.get("captured_at") or "unknown time"
            label = "edit" if version.get("kind") == service.KIND_EDIT else "original"
            content = _truncate(version.get("body") or version.get("media_name") or "(no content)", 200)
            body.append(f"{sym.OP_EDIT if version.get('kind') == service.KIND_EDIT else sym.OP_SAVE} "
                        f"v{index} ({label}) · {when}")
            body.append(f"   {content}")
    builder = InlinePanelBuilder()
    builder.add_row(sym.label(sym.OP_BACK, "Back to list"), f"panel:{LIST_ID}", PRIMARY)
    if not is_delete:
        # Group-level delete: removes this message's whole snapshot/edit history.
        builder.add_row(
            sym.label(sym.OP_DELETE, "Delete this message"),
            f"action:aed_delete_group:{item.get('chat_id')}:{item.get('message_id')}",
            DANGER,
        )
    else:
        builder.add_row(
            sym.label(sym.OP_DELETE, "Delete this event"),
            f"action:aed_delete:{item.get('id')}",
            DANGER,
        )
    return "Archive Item", "\n".join(body), builder.build()


async def _render_confirm(owner_id: int) -> tuple[str, str, list]:
    body = (
        f"{sym.STATE_WARNING} This permanently removes EVERY archived item of this "
        "account, including pre-deletion snapshots. It cannot be undone."
    )
    builder = InlinePanelBuilder()
    builder.add_row(sym.label(sym.OP_DELETE, "Confirm clear archive"), "action:aed_clear_confirm", DANGER)
    builder.add_row(sym.label(sym.OP_BACK, "Cancel"), f"panel:{PANEL_ID}", PRIMARY)
    return "Clear Archive", body, builder.build()


async def _panel_result(panel_id: str, extra: str, owner_id: int) -> tuple[str, str, list]:
    await service.ensure_loaded_async()
    if panel_id == PANEL_ID:
        return await _render_main(owner_id)
    if panel_id == LIST_ID:
        return await _render_list(owner_id, extra)
    if panel_id == ITEM_ID:
        return await _render_item(owner_id, extra)
    if panel_id == CATS_ID:
        return await _render_categories(owner_id)
    if panel_id == CONFIRM_ID:
        return await _render_confirm(owner_id)
    return await _render_main(owner_id)


def _inline_builder(panel_id: str, owner_id: int):
    async def _builder(event, extra: str) -> list:
        title, body, buttons = await _panel_result(panel_id, extra, owner_id)
        return [render(title, body, buttons)]

    return _builder


# ── Actions ──────────────────────────────────────────────────────────────────

async def _toggle_enabled_action(event, extra: str, chat_id: int):
    await service.ensure_loaded_async()
    if not await service.set_enabled(not service.is_enabled()):
        return "Anti-Edit / Anti-Delete", "Could not save the change. Please try again.", []
    return await _render_main(inline_engine.get_owner_id())


async def _refresh_action(event, extra: str, chat_id: int):
    await service.reload_async()
    return await _render_main(inline_engine.get_owner_id())


async def _category_action(event, extra: str, chat_id: int):
    await service.ensure_loaded_async()
    category = (extra or "").strip()
    if category not in service.CATEGORIES:
        return "Archive Categories", "That category is not valid — nothing changed.", []
    enabled = category not in set(service.categories())
    if not await service.set_category(category, enabled):
        return (
            "Archive Categories",
            f"{sym.STATE_WARNING} At least one category must stay enabled — nothing changed.",
            [],
        )
    return await _render_categories(inline_engine.get_owner_id())


async def _page_action(event, extra: str, chat_id: int):
    page = (extra or "").strip()
    return await _render_list(inline_engine.get_owner_id(), f"p={page}" if page.isdigit() else "")


async def _delete_action(event, extra: str, chat_id: int):
    archive_id = (extra or "").strip()
    if not await service.delete_row(archive_id):
        return "Archive", f"{sym.STATE_WARNING} That item could not be deleted.", []
    return await _render_list(inline_engine.get_owner_id(), "")


async def _delete_group_action(event, extra: str, chat_id: int):
    """Delete a logical message's whole snapshot/edit history (group semantics)."""
    parts = (extra or "").split(":", 1)
    if len(parts) != 2:
        return "Archive", f"{sym.STATE_WARNING} That item could not be deleted.", []
    chat_s, message_s = parts
    if not (chat_s.lstrip("-").isdigit() and message_s.lstrip("-").isdigit()):
        return "Archive", f"{sym.STATE_WARNING} That item could not be deleted.", []
    removed = await service.delete_group(int(chat_s), int(message_s))
    if removed < 0:
        return "Archive", f"{sym.STATE_WARNING} That item could not be deleted.", []
    return await _render_list(inline_engine.get_owner_id(), "")


async def _clear_confirm_action(event, extra: str, chat_id: int):
    removed = await service.clear_archive()
    if removed < 0:
        return "Archive", f"{sym.STATE_WARNING} The archive could not be cleared.", []
    return await _render_list(inline_engine.get_owner_id(), "")


async def _noop_action(event, extra: str, chat_id: int):
    return await _render_list(inline_engine.get_owner_id(), "")


# ── Input ────────────────────────────────────────────────────────────────────

async def _retention_input_handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    from backend.helper.client import get_client
    from backend.helper.input_state import clear_pending

    raw = (text or "").strip()
    days = int(raw) if raw.isdigit() else None
    if days is None or not await service.set_retention_days(days):
        notice = (
            f"{sym.STATE_WARNING} Enter a whole number of days between "
            f"{service.MIN_RETENTION_DAYS} and {service.MAX_RETENTION_DAYS}. Nothing changed."
        )
    else:
        notice = f"{sym.OP_SAVE} Retention set to {days} day(s)."
    clear_pending(0)
    helper = get_client()
    if helper and inline_chat_id and inline_msg_id:
        try:
            await helper.edit_message(inline_chat_id, inline_msg_id, notice)
        except Exception as exc:
            logger.warning("aed retention inline edit failed: %s", exc)
    client = inline_engine.get_self_client()
    if client is not None and msg_id:
        try:
            await client.delete_messages(chat_id, [msg_id])
        except Exception:
            pass


# ── Event capture ────────────────────────────────────────────────────────────

def register(client, owner_id: int, tz_str: str = "UTC") -> None:
    service.configure_owner(owner_id)

    # Preload the configuration in the background so the common case never waits
    # on a database read on the event path.
    try:
        asyncio.get_running_loop()
    except RuntimeError:
        pass
    else:
        try:
            from backend.runtime.task_guard import guarded_create_task

            guarded_create_task(service.ensure_loaded_async(), name="anti_edit_delete:preload_state")
        except Exception:
            pass

    @client.on(events.NewMessage())
    async def _aed_new_message(event):
        try:
            # Await the persisted configuration: the background preload may not
            # have finished yet, and an unloaded cache reads as "disabled", which
            # would silently drop every capture in the startup window.
            await service.ensure_loaded_async()
            if not service.is_enabled():
                return
            msg = getattr(event, "message", None)
            if msg is None or getattr(msg, "action", None) is not None:
                return
            identity, peer_is_bot = await _resolve_peer_identity(event, getattr(msg, "chat_id", None))
            if peer_is_bot:
                logger.info("[AED] snapshot skipped: peer is a Telegram bot chat_id=%s", msg.chat_id)
                return
            await service.record_message(
                msg=msg, kind=service.KIND_SNAPSHOT, owner=owner_id,
                identity=identity, peer_is_bot=peer_is_bot,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[AED] snapshot capture failed: %s", type(exc).__name__)

    @client.on(events.MessageEdited())
    async def _aed_message_edited(event):
        try:
            await service.ensure_loaded_async()
            if not service.is_enabled():
                return
            msg = getattr(event, "message", None)
            if msg is None:
                return
            identity, peer_is_bot = await _resolve_peer_identity(event, getattr(msg, "chat_id", None))
            if peer_is_bot:
                logger.info("[AED] edit skipped: peer is a Telegram bot chat_id=%s", msg.chat_id)
                return
            await service.record_message(
                msg=msg, kind=service.KIND_EDIT, owner=owner_id,
                identity=identity, peer_is_bot=peer_is_bot,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[AED] edit capture failed: %s", type(exc).__name__)

    @client.on(events.MessageDeleted())
    async def _aed_message_deleted(event):
        try:
            await service.ensure_loaded_async()
            if not service.is_enabled():
                return
            chat_id = getattr(event, "chat_id", None)
            _identity, peer_is_bot = (
                await _resolve_peer_identity(event, chat_id)
                if isinstance(chat_id, int) and not isinstance(chat_id, bool) and chat_id > 0
                else (None, False)
            )
            if peer_is_bot:
                logger.info("[AED] deletion skipped: peer is a Telegram bot chat_id=%s", chat_id)
                return
            await service.record_deletion(event=event, owner=owner_id, peer_is_bot=peer_is_bot)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("[AED] deletion capture failed: %s", type(exc).__name__)

    register_panel(PANEL_ID, lambda event, extra: _panel_result(PANEL_ID, extra, owner_id), parent="menu", title="Anti-Edit / Anti-Delete")
    register_panel(LIST_ID, lambda event, extra: _panel_result(LIST_ID, extra, owner_id), parent=PANEL_ID, title="Archive")
    register_panel(ITEM_ID, lambda event, extra: _panel_result(ITEM_ID, extra, owner_id), parent=LIST_ID, title="Archive Item")
    register_panel(CATS_ID, lambda event, extra: _panel_result(CATS_ID, extra, owner_id), parent=PANEL_ID, title="Categories")
    register_panel(CONFIRM_ID, lambda event, extra: _panel_result(CONFIRM_ID, extra, owner_id), parent=PANEL_ID, title="Clear Archive")

    register_inline_builder(PANEL_ID, _inline_builder(PANEL_ID, owner_id))
    register_inline_builder(LIST_ID, _inline_builder(LIST_ID, owner_id))
    register_inline_builder(ITEM_ID, _inline_builder(ITEM_ID, owner_id))
    register_inline_builder(CATS_ID, _inline_builder(CATS_ID, owner_id))
    register_inline_builder(CONFIRM_ID, _inline_builder(CONFIRM_ID, owner_id))

    register_action("aed_toggle_enabled", _toggle_enabled_action)
    register_action("aed_refresh", _refresh_action)
    register_action("aed_category", _category_action)
    register_action("aed_page", _page_action)
    register_action("aed_delete", _delete_action)
    register_action("aed_delete_group", _delete_group_action)
    register_action("aed_clear_confirm", _clear_confirm_action)
    register_action("aed_noop", _noop_action)

    register_input(PANEL_ID, "retention", {
        "handler": _retention_input_handler,
        "prompt": (
            "**Archive retention**\n\nEnter a whole number of days between "
            f"{service.MIN_RETENTION_DAYS} and {service.MAX_RETENTION_DAYS}:\n\n_Reply below._"
        ),
    })
