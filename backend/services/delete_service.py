"""
Delete service — all deletion business logic lives here.

Both text commands and inline panels call these exact functions.
"""
from __future__ import annotations

import asyncio
import logging
from collections.abc import Callable
from datetime import datetime, time as datetime_time, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from backend.ai.semantic_delete import normalize_text
from backend.db import client as db_client
from backend.diagnostics import record_event
from backend.helper.rpc_timeout import rpc_await
from backend.services import settings_service

logger = logging.getLogger(__name__)

_MAX_DELETE_SCAN_MESSAGES = 1000
_DELETE_VERIFY_CHUNK = 100
_DELETE_RPC_TIMEOUT_SECONDS = 5.0


async def _telegram_rpc(operation, *, label: str, retries: int = 0):
    """Run one Telegram operation with a small, explicit retry budget."""
    for attempt in range(retries + 1):
        try:
            return await rpc_await(
                operation(),
                timeout=_DELETE_RPC_TIMEOUT_SECONDS,
                label=label,
            )
        except (asyncio.TimeoutError, ConnectionError, OSError):
            if attempt >= retries:
                raise
            await asyncio.sleep(0.05 * (attempt + 1))


async def _iter_messages_bounded(client, chat_id, **kwargs):
    """Iterate Telegram history with a timeout on every page/RPC."""
    limit = kwargs.get("limit")
    if limit is None:
        limit = _MAX_DELETE_SCAN_MESSAGES
    iterator = client.iter_messages(chat_id, **kwargs)
    for _ in range(limit):
        try:
            yield await rpc_await(
                iterator.__anext__(),
                timeout=_DELETE_RPC_TIMEOUT_SECONDS,
                label="delete.iter_messages",
            )
        except StopAsyncIteration:
            return


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
            me = await _telegram_rpc(
                lambda: client.get_me(),
                label="delete.get_me",
            )
            me_id = getattr(me, "id", None)
        return me_id
    except Exception:
        return None


def _is_self_owned(msg, me_id: int | None) -> bool:
    """True only when ``msg`` is verified as sent by the authenticated account.

    Fail-closed: a missing message, unresolved account identity, a false
    ``out`` flag, a missing ``sender_id``, or a sender that is not the
    connected account all reject. ``out`` is server-authoritative Telegram
    metadata and ``sender_id`` is compared against the resolved account ID.
    """
    if msg is None or me_id is None:
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
    client,
    chat_id,
    message_ids: list[int],
    *,
    exclude_message_id: int | None = None,
    request_id: str = "",
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
    ids: list[int] = []
    rejected: list[int] = []
    for value in message_ids:
        if not isinstance(value, int) or value <= 0:
            rejected.append(value)
        elif exclude_message_id is not None and value == exclude_message_id:
            rejected.append(value)
        elif value not in ids:
            ids.append(value)
    if not ids:
        return [], rejected

    me_id = await _resolve_me_id(client)
    verified: list[int] = []
    chunk = _DELETE_VERIFY_CHUNK
    for start in range(0, len(ids), chunk):
        part = ids[start:start + chunk]
        try:
            fetched = await _telegram_rpc(
                lambda: client.get_messages(chat_id, ids=part),
                label="delete.ownership_fetch",
                retries=1,
            )
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
            await _telegram_rpc(
                lambda: client.delete_messages(chat_id, batch),
                label="delete.batch",
            )
            deleted.extend(batch)
        except Exception as exc:
            delete_errors.append(str(exc))
            logger.error(
                "DELETE_EXECUTION_ERROR chat_id=%s ids=%s error=%s",
                chat_id, batch, exc,
            )
            rejected.extend(batch)
    logger.info(
        "DELETE_EXECUTION_END request_id=%s chat_id=%s deleted=%d rejected=%d",
        request_id or "-", chat_id, len(deleted), len(rejected),
    )
    logger.info(
        "DELETE_OWNERSHIP_FILTERED request_id=%s chat_id=%s candidates=%d "
        "self_owned=%d rejected=%d",
        request_id or "-", chat_id, len(ids), len(verified), len(rejected),
    )
    if delete_errors:
        # Transport failures propagate so callers stay honest — but the loop
        # above already attempted every verified batch, so one bad batch can
        # never silently block the rest of the deletion.
        raise RuntimeError(
            "Telegram delete failed for some messages: " + "; ".join(delete_errors)
        )
    return deleted, rejected


def _load_tz(tz_name: str) -> tzinfo:
    """Resolve a configured timezone with a deterministic fallback."""
    try:
        return ZoneInfo(tz_name)
    except Exception:
        # Some minimal containers omit the system tzdata package. Tehran
        # has used a fixed UTC+03:30 offset since its 2022 DST change;
        # keep local cutoff semantics deterministic there instead of
        # silently interpreting HH:MM as UTC.
        if tz_name == "Asia/Tehran":
            return timezone(timedelta(hours=3, minutes=30))
        return timezone.utc


async def _parse_cutoff(value: Any, tz_name: str = "UTC") -> tuple[datetime | None, bool]:
    """Parse an ISO timestamp, a date keyword, or a local HH:MM cutoff.

    Returns ``(cutoff, is_daily)``. ``is_daily`` is True only for a bare
    HH:MM wall-clock cutoff, which must be anchored to each message's own
    local date instead of the current date.
    """
    if value is None:
        return None, False
    if isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text:
            return None, False
        tz = _load_tz(tz_name)
        if text.casefold() in {"today", "امروز"}:
            now = datetime.now(tz)
            dt = datetime.combine(now.date(), datetime_time.min, tzinfo=tz)
        elif text.casefold() in {"yesterday", "دیروز"}:
            now = datetime.now(tz)
            dt = datetime.combine(now.date() - timedelta(days=1), datetime_time.min, tzinfo=tz)
        else:
            # A bare HH:MM is local wall-clock time in the configured
            # timezone. Parse it before ISO timestamps because some Python
            # versions accept "09:00" as a date-time with UTC semantics.
            if ":" in text and len(text) <= 5:
                try:
                    parsed = datetime_time.fromisoformat(text)
                except ValueError:
                    return None, False
                now = datetime.now(tz)
                dt = datetime.combine(now.date(), parsed, tzinfo=tz)
                return dt, True
            try:
                dt = datetime.fromisoformat(text.replace("Z", "+00:00"))
            except ValueError:
                return None, False
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt, False


def _daily_cutoff_for(msg_date: datetime, cutoff: datetime, tz_name: str) -> datetime:
    """Anchor a daily wall-clock cutoff to the message's own local date."""
    local = msg_date.astimezone(_load_tz(tz_name))
    return local.replace(
        hour=cutoff.hour, minute=cutoff.minute, second=0, microsecond=0,
    )


def _message_text(msg) -> str:
    return str(getattr(msg, "message", None) or getattr(msg, "text", None) or "")


async def select_self_owned_message_ids(
    client,
    chat_id,
    *,
    count: int | None = None,
    until_time: Any = None,
    after_time: Any = None,
    boundary_id: int | None = None,
    query: str = "",
    match: Callable[[str], bool] | None = None,
    exclude_message_id: int | None = None,
    tz_name: str = "UTC",
    request_id: str = "",
) -> tuple[list[int], int, Exception | None]:
    """Select recent self-owned IDs before the final ownership chokepoint.

    Telegram history is scanned newest-first. The active Delete request can
    participate in the requested range, ownership is checked for selection, and the
    final fetch in ``delete_verified_self_messages`` remains authoritative.
    """
    if count is not None and (count < 1 or count > 500):
        return [], 0, ValueError("count must be between 1 and 500")
    cutoff, cutoff_daily = await _parse_cutoff(until_time, tz_name)
    floor, floor_daily = await _parse_cutoff(after_time, tz_name)
    if (until_time is not None and cutoff is None) or (after_time is not None and floor is None):
        return [], 0, ValueError("invalid time range")
    if boundary_id is not None and boundary_id <= 0:
        return [], 0, ValueError("invalid boundary message ID")
    me_id = await _resolve_me_id(client)
    if me_id is None:
        return [], 0, RuntimeError("authenticated account identity could not be verified")

    selected: list[int] = []
    considered = 0
    # Topic queries are matched on the deterministic normalized form so
    # Persian/Arabic script variants, digits, and zero-width characters do
    # not defeat an otherwise exact content match. ``match`` (when present)
    # is a pure semantic predicate applied AFTER self ownership — it can
    # never turn a foreign message into a candidate.
    needle = normalize_text(query.strip()) if query else ""
    boundary_seen = boundary_id is None
    try:
        # Never ask Telethon for an unbounded history. The cap is large
        # enough for normal requests while keeping a pathological ``all`` or
        # broad semantic request inside a deterministic execution window.
        iterator = _iter_messages_bounded(
            client, chat_id, limit=_MAX_DELETE_SCAN_MESSAGES,
        )
        async for msg in iterator:
            mid = getattr(msg, "id", None)
            if not isinstance(mid, int) or mid <= 0:
                continue
            # A boundary must still be observed when it is the active request
            # itself. It controls the range but is never selected below.
            is_boundary = boundary_id is not None and not boundary_seen and mid == boundary_id
            if is_boundary:
                boundary_seen = True
                if (
                    _is_self_owned(msg, me_id)
                    and mid != exclude_message_id
                    and (not needle or needle in normalize_text(_message_text(msg)))
                    and (match is None or match(_message_text(msg)))
                ):
                    selected.append(mid)
                    considered += 1
                continue
            if exclude_message_id is not None and mid == exclude_message_id:
                continue

            msg_date = getattr(msg, "date", None)
            if msg_date is not None:
                if not isinstance(msg_date, datetime):
                    msg_date = None
                elif msg_date.tzinfo is None:
                    msg_date = msg_date.replace(tzinfo=timezone.utc)
                if msg_date is not None and cutoff is not None:
                    if cutoff_daily:
                        if msg_date > _daily_cutoff_for(msg_date, cutoff, tz_name):
                            continue
                    elif msg_date > cutoff:
                        continue
                if msg_date is not None and floor is not None:
                    if floor_daily:
                        if msg_date < _daily_cutoff_for(msg_date, floor, tz_name):
                            # A daily floor is per-message: older messages on
                            # earlier dates can still be after the wall-clock
                            # time, so the newest-first scan must not stop.
                            continue
                    elif msg_date < floor:
                        # History is newest-first, so older messages cannot
                        # enter a bounded absolute time range after this point.
                        break
            elif cutoff is not None or floor is not None:
                continue

            if boundary_id is not None and not boundary_seen:
                continue

            # Apply the self-only candidate boundary before any semantic text
            # predicate. The model/query can never turn a foreign match into
            # a deletion candidate, even transiently.
            if not _is_self_owned(msg, me_id):
                continue
            text = _message_text(msg)
            if needle and needle not in normalize_text(text):
                continue
            if match is not None and not match(text):
                continue
            considered += 1
            selected.append(mid)
            if count is not None and len(selected) >= count:
                break
    except Exception as exc:
        return selected, considered, exc

    if boundary_id is not None and not boundary_seen:
        # A missing/foreign boundary is a safe empty result, not permission
        # to expand the range to the whole chat.
        return [], considered, None
    return selected, considered, None


async def do_del_self_filtered(
    client,
    chat_id,
    *,
    count: int | None = None,
    until_time: Any = None,
    after_time: Any = None,
    boundary_id: int | None = None,
    query: str = "",
    match: Callable[[str], bool] | None = None,
    exclude_message_id: int | None = None,
    tz_name: str = "UTC",
    request_id: str = "",
) -> tuple[int, int, Exception | None]:
    """Delete a self-owned range after deterministic candidate selection."""
    ids, considered, selection_error = await select_self_owned_message_ids(
        client, chat_id, count=count, until_time=until_time, after_time=after_time,
        boundary_id=boundary_id, query=query, match=match,
        exclude_message_id=exclude_message_id,
        tz_name=tz_name, request_id=request_id,
    )
    logger.info(
        "DELETE_CANDIDATES_FOUND request_id=%s chat_id=%s mode=%s "
        "candidate_count=%d considered=%d",
        request_id or "-", chat_id,
        "semantic" if query else ("boundary" if boundary_id is not None else "range"),
        len(ids), considered,
    )
    if selection_error is not None:
        return considered, 0, selection_error
    try:
        deleted, _rejected = await delete_verified_self_messages(
            client, chat_id, ids, exclude_message_id=exclude_message_id,
            request_id=request_id,
        )
    except Exception as exc:
        return considered, 0, exc
    return considered, len(deleted), None


async def do_del_self_last_n(
    client, chat_id, n: int, *, exclude_message_id: int | None = None, tz_name: str = "UTC"
) -> tuple[int, int, Exception | None]:
    """Delete the last N verified self-owned messages before the request."""
    return await do_del_self_filtered(
        client, chat_id, count=n, exclude_message_id=exclude_message_id, tz_name=tz_name,
    )


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
        async for msg in _iter_messages_bounded(
            client, chat_id, limit=n + 5, from_user="me"
        ):
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
        async for msg in _iter_messages_bounded(client, chat_id, limit=n):
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


async def do_del_id_counts(
    client, chat_id, start_id: int, *, exclude_message_id: int | None = None
) -> tuple[int, Exception | None]:
    """Delete all outgoing messages from ``start_id`` forward.

    Same contract as :func:`do_del_n_counts` — returns the real number of
    deleted messages plus any error, never a fabricated success.
    """
    t0 = asyncio.get_event_loop().time()
    total = 0
    try:
        batch = []
        async for msg in _iter_messages_bounded(
            client,
            chat_id,
            min_id=start_id - 1,
            from_user="me",
            limit=_MAX_DELETE_SCAN_MESSAGES,
        ):
            if exclude_message_id is not None and getattr(msg, "id", None) == exclude_message_id:
                continue
            batch.append(msg.id)
            if len(batch) >= settings_service.delete_batch_size():
                deleted_ids, _rejected = await delete_verified_self_messages(
                    client, chat_id, batch, exclude_message_id=exclude_message_id
                )
                total += len(deleted_ids)
                batch = []
        if batch:
            deleted_ids, _rejected = await delete_verified_self_messages(
                client, chat_id, batch, exclude_message_id=exclude_message_id
            )
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
