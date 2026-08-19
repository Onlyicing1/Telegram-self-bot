"""
Delete service — all deletion business logic lives here.

Both text commands and inline panels call these exact functions.
"""
from __future__ import annotations

import asyncio
import logging
from typing import Any

from backend.db import client as db_client
from backend.diagnostics import record_event
from backend.services import settings_service

logger = logging.getLogger(__name__)


async def _resolve_me_id(client) -> int | None:
    """Best-effort resolution of the authenticated account's numeric user ID.

    Uses Telethon's cached ``client.me`` when available and falls back to a
    ``get_me()`` RPC. Returns ``None`` when the identity cannot be resolved;
    ownership verification then relies on the server-authoritative ``out``
    flag plus a present ``sender_id`` (both are required — see
    :func:`_is_self_owned`).
    """
    try:
        me = getattr(client, "me", None)
        me_id = getattr(me, "id", None) if me is not None else None
        if me_id is None:
            me = await client.get_me()
            me_id = getattr(me, "id", None)
        return me_id
    except Exception:
        return None


def _is_self_owned(msg, me_id: int | None) -> bool:
    """True only when ``msg`` is verified as sent by the authenticated account.

    Fail-closed: a missing message, a false ``out`` flag, a missing
    ``sender_id``, or a sender that is not the connected account all reject.
    ``out`` is server-authoritative Telegram metadata (only the signed-in
    account's messages carry it), and ``sender_id`` is compared against the
    resolved account ID when available.
    """
    if msg is None:
        return False
    if not getattr(msg, "out", False):
        return False
    sender_id = getattr(msg, "sender_id", None)
    if sender_id is None:
        return False
    if me_id is not None and sender_id != me_id:
        return False
    return True


async def delete_verified_self_messages(
    client, chat_id, message_ids: list[int]
) -> tuple[list[int], list[int]]:
    """Delete ONLY messages verified as belonging to the authenticated account.

    Single authoritative chokepoint for every chat deletion in the Delete
    system. Each candidate ID is re-fetched from the actual chat immediately
    before deletion and ownership is verified against Telegram message
    metadata (server ``out`` flag + sender ID vs the connected account) via
    :func:`_is_self_owned`. Fail-closed: any candidate whose ownership cannot
    be verified is rejected and never reaches ``client.delete_messages``.

    Returns ``(deleted_ids, rejected_ids)``. Per-message ownership problems
    never raise; transport failures propagate to the caller so failures stay
    honest.
    """
    ids = [i for i in message_ids if isinstance(i, int) and i > 0]
    rejected: list[int] = [i for i in message_ids if not (isinstance(i, int) and i > 0)]
    if not ids:
        return [], rejected

    me_id = await _resolve_me_id(client)
    verified: list[int] = []
    chunk = 100
    for start in range(0, len(ids), chunk):
        part = ids[start:start + chunk]
        try:
            fetched = await client.get_messages(chat_id, ids=part)
        except Exception as exc:
            logger.warning(
                "DELETE_OWNERSHIP_CHECK chat_id=%s ids=%s result=rejected "
                "reason=fetch_failed error=%s",
                chat_id, part, exc,
            )
            rejected.extend(part)
            continue
        if fetched is None:
            logger.warning(
                "DELETE_OWNERSHIP_CHECK chat_id=%s ids=%s result=rejected reason=fetch_empty",
                chat_id, part,
            )
            rejected.extend(part)
            continue
        if not isinstance(fetched, list):
            fetched = [fetched]
        by_id: dict[int, Any] = {}
        for msg in fetched:
            if msg is None:
                continue
            mid = getattr(msg, "id", None)
            if mid is not None:
                by_id[mid] = msg
        chunk_verified = 0
        chunk_rejected = 0
        for mid in part:
            if _is_self_owned(by_id.get(mid), me_id):
                verified.append(mid)
                chunk_verified += 1
            else:
                rejected.append(mid)
                chunk_rejected += 1
        logger.info(
            "DELETE_OWNERSHIP_CHECK chat_id=%s ids=%s verified=%d rejected=%d "
            "me_id_resolved=%s",
            chat_id, part, chunk_verified, chunk_rejected,
            "yes" if me_id is not None else "no",
        )

    deleted: list[int] = []
    delete_errors: list[str] = []
    for start in range(0, len(verified), chunk):
        batch = verified[start:start + chunk]
        logger.info(
            "DELETE_EXECUTION_START chat_id=%s batch_size=%d ids=%s",
            chat_id, len(batch), batch,
        )
        try:
            await client.delete_messages(chat_id, batch)
            deleted.extend(batch)
        except Exception as exc:
            delete_errors.append(str(exc))
            logger.error(
                "DELETE_EXECUTION_ERROR chat_id=%s ids=%s error=%s",
                chat_id, batch, exc,
            )
            rejected.extend(batch)
    logger.info(
        "DELETE_EXECUTION_END chat_id=%s deleted=%d rejected=%d",
        chat_id, len(deleted), len(rejected),
    )
    if delete_errors:
        # Transport failures propagate so callers stay honest — but the loop
        # above already attempted every verified batch, so one bad batch can
        # never silently block the rest of the deletion.
        raise RuntimeError(
            "Telegram delete failed for some messages: " + "; ".join(delete_errors)
        )
    return deleted, rejected


async def do_del_n_counts(client, chat_id, n: int) -> tuple[int, Exception | None]:
    """Delete the last ``n`` outgoing messages.

    Returns ``(deleted_count, error)`` so callers (text commands, panels,
    AI tools) can report the REAL number of deleted messages and a real
    failure — never an inferred success. ``deleted_count`` is the actual
    number of messages Telegram deleted; ``error`` is ``None`` on success.
    """
    if n < 1 or n > 500:
        return 0, ValueError("n must be between 1 and 500")
    t0 = asyncio.get_event_loop().time()
    try:
        msg_ids = []
        async for msg in client.iter_messages(chat_id, limit=n + 5, from_user="me"):
            msg_ids.append(msg.id)
            if len(msg_ids) >= n:
                break
        deleted_ids, _rejected = await delete_verified_self_messages(client, chat_id, msg_ids[:n])
        record_event("delete", "del n", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
        return len(deleted_ids), None
    except Exception as exc:
        logger.error("del n failed: %s", exc)
        record_event("delete", "del n", 0, "ERROR", str(exc))
        return 0, exc


async def do_del_last_n_real(
    client, chat_id, n: int
) -> tuple[int, int, Exception | None]:
    """Delete outgoing messages within the last ``n`` REAL chat messages.

    Unlike :func:`do_del_n_counts` (which counts only the owner's outgoing
    messages), this counts EVERY recent message in the chat — the owner,
    other participants, and the Self-Bot's own generated/edited messages —
    and deletes only the outgoing subset the connected account is allowed to
    delete. Telegram's chronological history is the source of truth.

    Returns ``(considered, deleted, error)``: ``considered`` is the number
    of real messages inspected, ``deleted`` is the number Telegram deleted,
    and ``error`` is ``None`` on success.
    """
    if n < 1 or n > 500:
        return 0, 0, ValueError("n must be between 1 and 500")
    t0 = asyncio.get_event_loop().time()
    try:
        recent = []
        async for msg in client.iter_messages(chat_id, limit=n):
            recent.append(msg)
            if len(recent) >= n:
                break
        deleted_ids, _rejected = await delete_verified_self_messages(
            client, chat_id, [m.id for m in recent]
        )
        record_event(
            "delete", "del last n real",
            (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS",
            f"{len(deleted_ids)}/{len(recent)}",
        )
        return len(recent), len(deleted_ids), None
    except Exception as exc:
        logger.error("del last n real failed: %s", exc)
        record_event("delete", "del last n real", 0, "ERROR", str(exc))
        return 0, 0, exc


async def do_del_id_counts(client, chat_id, start_id: int) -> tuple[int, Exception | None]:
    """Delete all outgoing messages from ``start_id`` forward.

    Same contract as :func:`do_del_n_counts` — returns the real number of
    deleted messages plus any error, never a fabricated success.
    """
    t0 = asyncio.get_event_loop().time()
    total = 0
    try:
        batch = []
        async for msg in client.iter_messages(chat_id, min_id=start_id - 1, from_user="me"):
            batch.append(msg.id)
            if len(batch) >= settings_service.delete_batch_size():
                deleted_ids, _rejected = await delete_verified_self_messages(client, chat_id, batch)
                total += len(deleted_ids)
                batch = []
        if batch:
            deleted_ids, _rejected = await delete_verified_self_messages(client, chat_id, batch)
            total += len(deleted_ids)
        record_event("delete", "del id", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
        return total, None
    except Exception as exc:
        logger.error("del id failed: %s", exc)
        record_event("delete", "del id", 0, "ERROR", str(exc))
        return total, exc


async def do_del_n(client, chat_id, n: int) -> str:
    if n < 1 or n > 500:
        return "⚠️ n must be between 1 and 500."
    deleted, err = await do_del_n_counts(client, chat_id, n)
    if err is not None:
        return f"❌ Delete failed: {err}"
    return f"🗑 Deleted `{deleted}` messages."


async def do_del_id(client, chat_id, start_id: int) -> str:
    deleted, err = await do_del_id_counts(client, chat_id, start_id)
    if err is not None:
        return f"❌ Delete failed: {err}"
    return f"🗑 Deleted messages from ID `{start_id}` forward."


async def do_del_code(client, owner_id: int, code: str) -> str:
    code = code.upper().strip()
    t0 = asyncio.get_event_loop().time()
    try:
        row = await db_client.query_save(code)
        record_event("database", "query_save", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
    except Exception as exc:
        logger.error("del save_code DB query failed: %s", exc)
        record_event("database", "query_save", 0, "ERROR", str(exc))
        return f"❌ DB error: {exc}"
    if not row:
        return f"❌ No saved item found for `{code}`"

    saved_chat_id = row.get("saved_chat_id")
    saved_msg_id = row.get("saved_msg_id")
    display = row.get("save_code") or code

    tg_deleted = False
    tg_error = None
    if saved_chat_id and saved_msg_id:
        try:
            await client.delete_messages(saved_chat_id, [saved_msg_id])
            tg_deleted = True
        except Exception as exc:
            tg_error = exc
            logger.warning("del %s: Telegram deletion failed: %s", code, exc)
    else:
        tg_deleted = True

    db_deleted = False
    db_error = None
    try:
        removed = await db_client.delete_save_row(owner_id, code)
        db_deleted = removed is not None
    except Exception as exc:
        db_error = exc
        logger.error("del %s: DB deletion failed: %s", code, exc)

    await db_client.log(
        owner_id,
        "INFO" if (tg_deleted and db_deleted) else "ERROR",
        f"Delete {code}: tg={'ok' if tg_deleted else 'fail'}, db={'ok' if db_deleted else 'fail'}",
        {"save_code": code, "tg_error": str(tg_error) if tg_error else None},
    )

    if tg_deleted and db_deleted:
        return f"🗑 Deleted `{display}`"
    elif tg_deleted and not db_deleted:
        return f"⚠️ `{display}`: Telegram message deleted, but DB row removal failed: {db_error}"
    elif not tg_deleted and db_deleted:
        if tg_error:
            return f"⚠️ `{display}`: DB row deleted, but Telegram message deletion failed: {tg_error}"
        return f"🗑 Deleted `{display}` (Telegram message was already missing)"
    return f"❌ `{display}`: Both Telegram and DB deletion failed. TG: {tg_error}, DB: {db_error}"
