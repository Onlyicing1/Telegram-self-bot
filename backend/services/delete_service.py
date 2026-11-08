"""
Delete service — all deletion business logic lives here.

Both text commands and inline panels call these exact functions.
"""
from __future__ import annotations

import asyncio
import inspect
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import dataclass
from datetime import datetime, time as datetime_time, timedelta, timezone, tzinfo
from typing import Any
from zoneinfo import ZoneInfo

from backend.ai.semantic_delete import normalize_text
from backend.db import client as db_client
from backend.diagnostics import record_event
from backend.helper import symbols as sym
from backend.helper.rpc_timeout import rpc_await
from backend.services import save_service, settings_service

logger = logging.getLogger(__name__)

#: Hard bounds for ONE Delete request. They are operational, not cosmetic:
#: every selected candidate costs one ownership fetch, every 100 verified ids
#: cost one Telegram delete, and ``_DELETE_OPERATION_DEADLINE_SECONDS`` bounds
#: the wall clock those round trips may take. Whichever bound is reached first
#: ends the operation, and the result is reported as PARTIAL — never as a
#: completed deletion of the larger scope the owner asked for.
#:
#: ``MAX_DELETE_COUNT`` is therefore not a message-count ceiling on what the
#: owner may request; it is the most candidates one operation will carry. A
#: count above it is refused with this exact bound, and a scan that meets the
#: scan/deadline bound at a lower number returns what it actually deleted.
MAX_DELETE_COUNT = 20000
_MAX_DELETE_SCAN_MESSAGES = 50000
_DELETE_VERIFY_CHUNK = 100
_DELETE_RPC_TIMEOUT_SECONDS = 5.0
_DELETE_OPERATION_DEADLINE_SECONDS = 240.0
_DELETE_PREVIEW_SAMPLE = 200
#: A confirmation panel must never wait on a history scan, so the preview gets
#: its own short wall clock instead of the operation's deadline.
_DELETE_PREVIEW_SECONDS = 3.0
#: How many scanned messages may pass between two progress reports.
_DELETE_PROGRESS_EVERY = 200


@dataclass
class DeleteReport:
    """The honest account of ONE delete operation.

    ``requested`` is the scope the owner asked for, ``considered`` the
    candidates the scan examined, ``deleted``/``rejected`` what Telegram and
    the ownership chokepoint actually did, and ``complete``/``reason`` whether
    the operation reached the end of its scope or stopped at a documented
    bound. Callers render this instead of inferring success from the request.
    """

    requested: str = ""
    considered: int = 0
    deleted: int = 0
    rejected: int = 0
    complete: bool = True
    reason: str = ""


@dataclass(frozen=True)
class Selection:
    """Candidate ids selected from history, plus how far the scan got."""

    ids: list[int]
    considered: int
    complete: bool = True
    reason: str = ""
    error: Exception | None = None


def _describe_scope(
    *,
    count: int | None = None,
    boundary_id: int | None = None,
    until_time: Any = None,
    after_time: Any = None,
    query: str = "",
) -> str:
    """One owner-readable name for the requested scope."""
    if query:
        return f"messages matching “{query}”"
    if boundary_id is not None:
        return f"your messages from message {boundary_id} forward"
    if until_time is not None or after_time is not None:
        return "your messages in the requested time range"
    if count is not None:
        return f"your last {count} messages"
    return "all of your messages in this chat"


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


async def _iter_messages_bounded(client, chat_id, *, deadline: float | None = None,
                                 stats: dict[str, str] | None = None, **kwargs):
    """Iterate Telegram history with a timeout on every page/RPC.

    The iterator is STREAMED: callers keep only what they select, never the
    messages themselves. ``limit`` bounds the number of items this scan may
    yield, and ``deadline`` (a ``time.monotonic()`` instant) bounds its wall
    clock. When either is the reason the scan stopped, ``stats["stopped"]``
    carries it so the caller can report a partial result honestly.
    """
    limit = kwargs.get("limit")
    if limit is None:
        limit = _MAX_DELETE_SCAN_MESSAGES
    kwargs["limit"] = limit
    iterator = client.iter_messages(chat_id, **kwargs)
    for _ in range(limit):
        timeout = _DELETE_RPC_TIMEOUT_SECONDS
        if deadline is not None:
            remaining = deadline - time.monotonic()
            if remaining <= 0:
                if stats is not None:
                    stats["stopped"] = "operation deadline"
                return
            # One page fetch must not outlive the operation's remaining budget.
            timeout = min(timeout, remaining)
        try:
            yield await rpc_await(
                iterator.__anext__(),
                timeout=timeout,
                label="delete.iter_messages",
            )
        except StopAsyncIteration:
            return
    if stats is not None:
        stats["stopped"] = "history scan limit"


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


def _note_stopped(stopped: dict[str, str] | None, reason: str) -> None:
    """Record, once, why an operation stopped before it finished."""
    if stopped is not None:
        stopped.setdefault("reason", reason)


async def _emit_progress(
    progress: Callable[[dict[str, int]], Awaitable[None]] | None,
    **fields: int,
) -> None:
    """Report progress through a caller-supplied painter.

    ``fields`` carries only the figures that are actually known at that point
    (``scanned`` while the history is read, ``considered``/``deleted``
    afterwards), so a painter can never render a number nobody measured. A
    painter failure is logged and never aborts the deletion.
    """
    if progress is None:
        return
    try:
        await progress(dict(fields))
    except Exception as exc:  # noqa: BLE001 — progress is never load-bearing
        logger.debug("DELETE_PROGRESS painter failed: %s", exc)


def _batch_progress(report: "DeleteReport | None"):
    """A per-batch observer for :func:`delete_verified_self_messages`.

    Progress is recorded AS EACH BATCH COMPLETES, so a cancellation or an
    outer deadline keeps the batches that really happened instead of resetting
    the operation to zero.
    """

    def _observe(done: list[int], failed: list[int]) -> None:
        if report is not None:
            report.deleted += len(done)
            report.rejected += len(failed)

    return _observe


async def delete_verified_self_messages(
    client,
    chat_id,
    message_ids: list[int],
    *,
    exclude_message_id: int | None = None,
    request_id: str = "",
    on_batch: Callable[[list[int], list[int]], object] | None = None,
    deadline: float | None = None,
    stopped: dict[str, str] | None = None,
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
        if deadline is not None and time.monotonic() >= deadline:
            # The remaining candidates were neither verified nor deleted, so
            # they are NOT reported as ownership rejections.
            _note_stopped(stopped, "operation deadline reached")
            break
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
        if deadline is not None and time.monotonic() >= deadline:
            _note_stopped(stopped, "operation deadline reached")
            break
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
            if on_batch is not None:
                outcome = on_batch(list(batch), [])
                if inspect.isawaitable(outcome):
                    await outcome
        except Exception as exc:
            delete_errors.append(str(exc))
            logger.error(
                "DELETE_EXECUTION_ERROR chat_id=%s ids=%s error=%s",
                chat_id, batch, exc,
            )
            rejected.extend(batch)
            if on_batch is not None:
                outcome = on_batch([], list(batch))
                if inspect.isawaitable(outcome):
                    await outcome
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
        # never silently block the rest of the deletion. The ids that DID reach
        # Telegram ride the exception so a caller can report the partial result
        # instead of claiming nothing happened.
        error = RuntimeError(
            "Telegram delete failed for some messages: " + "; ".join(delete_errors)
        )
        error.deleted_ids = list(deleted)  # type: ignore[attr-defined]
        error.rejected_ids = list(rejected)  # type: ignore[attr-defined]
        raise error
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
    deadline: float | None = None,
    progress: Callable[[dict[str, int]], Awaitable[None]] | None = None,
) -> Selection:
    """Select recent self-owned IDs before the final ownership chokepoint.

    Telegram history is scanned newest-first in ONE streaming pass and only
    the selected integer ids are kept — messages are never accumulated, so a
    large scope costs a bounded amount of memory. The scan ends when the
    requested ``count`` is satisfied, when the history ends, when
    ``_MAX_DELETE_SCAN_MESSAGES`` is reached, or when the operation
    ``deadline`` passes. The last two are returned as an INCOMPLETE selection
    so the caller reports exactly how far it got.

    Ownership is checked for selection here, and the final fetch in
    ``delete_verified_self_messages`` remains authoritative.
    """
    if count is not None and (count < 1 or count > MAX_DELETE_COUNT):
        error = ValueError(f"count must be between 1 and {MAX_DELETE_COUNT}")
        return Selection([], 0, False, str(error), error)
    cutoff, cutoff_daily = await _parse_cutoff(until_time, tz_name)
    floor, floor_daily = await _parse_cutoff(after_time, tz_name)
    if (until_time is not None and cutoff is None) or (after_time is not None and floor is None):
        error = ValueError("invalid time range")
        return Selection([], 0, False, str(error), error)
    if boundary_id is not None and boundary_id <= 0:
        error = ValueError("invalid boundary message ID")
        return Selection([], 0, False, str(error), error)
    me_id = await _resolve_me_id(client)
    if me_id is None:
        error = RuntimeError("authenticated account identity could not be verified")
        return Selection([], 0, False, str(error), error)

    selected: list[int] = []
    considered = 0
    scanned = 0
    stats: dict[str, str] = {}
    # Topic queries are matched on the deterministic normalized form so
    # Persian/Arabic script variants, digits, and zero-width characters do
    # not defeat an otherwise exact content match. ``match`` (when present)
    # is a pure semantic predicate applied AFTER self ownership — it can
    # never turn a foreign message into a candidate.
    needle = normalize_text(query.strip()) if query else ""
    boundary_seen = boundary_id is None
    try:
        # Never ask Telethon for an unbounded history: the scan bound and the
        # operation deadline keep a pathological ``all`` request inside a
        # deterministic execution window, and reaching either is reported.
        iterator = _iter_messages_bounded(
            client, chat_id, limit=_MAX_DELETE_SCAN_MESSAGES,
            deadline=deadline, stats=stats,
        )
        async for msg in iterator:
            scanned += 1
            if progress is not None and scanned % _DELETE_PROGRESS_EVERY == 0:
                await _emit_progress(
                    progress, scanned=scanned, considered=considered,
                )
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
                return Selection(selected, considered, True, "")
            if len(selected) >= MAX_DELETE_COUNT:
                # All mode is bounded by the same per-operation candidate limit
                # as an explicit count; reaching it is a PARTIAL result.
                return Selection(
                    selected, considered, False,
                    f"candidate limit ({MAX_DELETE_COUNT}) reached",
                )
    except Exception as exc:
        return Selection(selected, considered, False, str(exc), exc)

    if boundary_id is not None and not boundary_seen:
        # A missing/foreign boundary is a safe empty result, not permission
        # to expand the range to the whole chat.
        return Selection([], considered, False, "boundary message not found")
    stopped = stats.get("stopped", "")
    if stopped:
        return Selection(selected, considered, False, f"{stopped} reached")
    return Selection(selected, considered, True, "")


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
    report: DeleteReport | None = None,
    deadline: float | None = None,
    progress: Callable[[dict[str, int]], Awaitable[None]] | None = None,
) -> tuple[int, int, Exception | None]:
    """Delete a self-owned range after deterministic candidate selection.

    ``report`` (optional) is filled with the real numbers, including a partial
    result when the scan or the Telegram batches did not complete.
    """
    if deadline is None:
        # Every caller — the panel included — gets a real total deadline that
        # covers BOTH the history scan and the verification/deletion batches.
        # A shorter caller-supplied deadline (the AI tool's) still wins.
        deadline = time.monotonic() + _DELETE_OPERATION_DEADLINE_SECONDS
    if report is not None and not report.requested:
        report.requested = _describe_scope(
            count=count, boundary_id=boundary_id, until_time=until_time,
            after_time=after_time, query=query,
        )
    selection = await select_self_owned_message_ids(
        client, chat_id, count=count, until_time=until_time, after_time=after_time,
        boundary_id=boundary_id, query=query, match=match,
        exclude_message_id=exclude_message_id,
        tz_name=tz_name, request_id=request_id, deadline=deadline,
        progress=progress,
    )
    ids, considered = selection.ids, selection.considered
    logger.info(
        "DELETE_CANDIDATES_FOUND request_id=%s chat_id=%s mode=%s "
        "candidate_count=%d considered=%d complete=%s reason=%s",
        request_id or "-", chat_id,
        "semantic" if query else ("boundary" if boundary_id is not None else "range"),
        len(ids), considered, selection.complete, selection.reason or "-",
    )
    if report is not None:
        report.considered = considered
        report.complete = selection.complete
        report.reason = selection.reason
    scan_error = selection.error
    if not ids:
        # Nothing was safely verified: identity unresolved, a missing boundary,
        # a scan that failed before its first candidate, or a chat with none of
        # the owner's messages. None of those may attempt a deletion.
        return considered, 0, scan_error

    async def _on_batch(done: list[int], failed: list[int]) -> None:
        if report is not None:
            report.deleted += len(done)
            report.rejected += len(failed)
        await _emit_progress(
            progress,
            considered=considered,
            deleted=report.deleted if report is not None else 0,
        )

    stopped: dict[str, str] = {}
    try:
        # Even a failed scan keeps its verified candidates: they still pass
        # through the same ownership chokepoint inside the remaining budget.
        deleted, _rejected = await delete_verified_self_messages(
            client, chat_id, ids, exclude_message_id=exclude_message_id,
            request_id=request_id, on_batch=_on_batch,
            deadline=deadline, stopped=stopped,
        )
    except Exception as exc:
        partial = getattr(exc, "deleted_ids", []) or []
        if report is not None:
            report.complete = False
            report.reason = str(exc)
        return considered, len(partial), exc
    if report is not None and stopped:
        report.complete = False
        report.reason = stopped.get("reason", "operation stopped early")
    return considered, len(deleted), scan_error


async def do_del_self_last_n(
    client, chat_id, n: int, *, exclude_message_id: int | None = None,
    tz_name: str = "UTC", report: DeleteReport | None = None,
) -> tuple[int, int, Exception | None]:
    """Delete the last N verified self-owned messages before the request."""
    return await do_del_self_filtered(
        client, chat_id, count=n, exclude_message_id=exclude_message_id,
        tz_name=tz_name, report=report,
    )


async def do_del_n_counts(
    client, chat_id, n: int, *, exclude_message_id: int | None = None,
    report: DeleteReport | None = None,
) -> tuple[int, Exception | None]:
    """Delete the last ``n`` outgoing messages.

    Returns ``(deleted_count, error)`` so callers (panels, AI tools) can report
    the REAL number of deleted messages and a real failure — never an inferred
    success. ``deleted_count`` is the number of messages Telegram deleted;
    ``error`` is ``None`` on success. ``report`` additionally records whether
    the requested count was reachable at all (a chat with fewer of the owner's
    messages returns a complete, smaller result rather than a failure).
    """
    if n < 1 or n > MAX_DELETE_COUNT:
        error = ValueError(f"n must be between 1 and {MAX_DELETE_COUNT}")
        if report is not None:
            report.requested = f"your last {n} messages"
            report.complete = False
            report.reason = str(error)
        return 0, error
    if report is not None and not report.requested:
        report.requested = f"your last {n} messages"
    t0 = asyncio.get_event_loop().time()
    try:
        msg_ids: list[int] = []
        stats: dict[str, str] = {}
        exhausted = True
        async for msg in _iter_messages_bounded(
            client, chat_id, limit=n + 1, from_user="me", stats=stats,
        ):
            if exclude_message_id is not None and getattr(msg, "id", None) == exclude_message_id:
                continue
            msg_ids.append(msg.id)
            if len(msg_ids) >= n:
                exhausted = False
                break
        deleted_ids, _rejected = await delete_verified_self_messages(
            client, chat_id, msg_ids[:n], exclude_message_id=exclude_message_id,
            on_batch=_batch_progress(report),
        )
        elapsed = (asyncio.get_event_loop().time() - t0) * 1000
        record_event("delete", "del n", elapsed, "SUCCESS", f"{len(deleted_ids)}/{n}")
        if report is not None:
            report.considered = len(msg_ids)
            if exhausted and stats.get("stopped"):
                report.complete = False
                report.reason = f"{stats['stopped']} reached"
        return len(deleted_ids), None
    except Exception as exc:
        logger.error("del n failed: %s", exc)
        record_event("delete", "del n", 0, "ERROR", str(exc))
        if report is not None:
            report.deleted = len(getattr(exc, "deleted_ids", []) or [])
            report.complete = False
            report.reason = str(exc)
        return len(getattr(exc, "deleted_ids", []) or []), exc


async def do_del_last_n_real(
    client, chat_id, n: int, *, report: DeleteReport | None = None,
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
    if n < 1 or n > MAX_DELETE_COUNT:
        error = ValueError(f"n must be between 1 and {MAX_DELETE_COUNT}")
        if report is not None:
            report.requested = f"your last {n} real messages"
            report.complete = False
            report.reason = str(error)
        return 0, 0, error
    if report is not None and not report.requested:
        report.requested = f"your last {n} real messages"
    t0 = asyncio.get_event_loop().time()
    try:
        # Only the ids of the inspected window are kept: a large ``n`` must not
        # hold a large slice of the chat in memory just to delete it.
        recent_ids: list[int] = []
        stats: dict[str, str] = {}
        async for msg in _iter_messages_bounded(client, chat_id, limit=n, stats=stats):
            recent_ids.append(msg.id)
            if len(recent_ids) >= n:
                break
        deleted_ids, _rejected = await delete_verified_self_messages(
            client, chat_id, recent_ids, on_batch=_batch_progress(report),
        )
        elapsed = (asyncio.get_event_loop().time() - t0) * 1000
        record_event(
            "delete", "del last n real", elapsed, "SUCCESS",
            f"{len(deleted_ids)}/{len(recent_ids)}",
        )
        if report is not None:
            report.considered = len(recent_ids)
            if len(recent_ids) < n and stats.get("stopped"):
                report.complete = False
                report.reason = f"{stats['stopped']} reached"
        return len(recent_ids), len(deleted_ids), None
    except Exception as exc:
        logger.error("del last n real failed: %s", exc)
        record_event("delete", "del last n real", 0, "ERROR", str(exc))
        if report is not None:
            report.deleted = len(getattr(exc, "deleted_ids", []) or [])
            report.complete = False
            report.reason = str(exc)
        return 0, len(getattr(exc, "deleted_ids", []) or []), exc


async def do_del_id_counts(
    client, chat_id, start_id: int, *, exclude_message_id: int | None = None,
    report: DeleteReport | None = None,
) -> tuple[int, Exception | None]:
    """Delete all outgoing messages from ``start_id`` forward.

    Same contract as :func:`do_del_n_counts` — returns the real number of
    deleted messages plus any error, never a fabricated success. Candidates
    are verified and deleted in bounded batches as the history is streamed, so
    the operation's memory and its batch size stay bounded regardless of how
    old ``start_id`` is.
    """
    if report is not None and not report.requested:
        report.requested = f"your messages from message {start_id} forward"
    t0 = asyncio.get_event_loop().time()
    total = 0
    considered = 0
    rejected_total = 0
    deadline = time.monotonic() + _DELETE_OPERATION_DEADLINE_SECONDS
    stats: dict[str, str] = {}
    stopped: dict[str, str] = {}
    try:
        batch: list[int] = []
        async for msg in _iter_messages_bounded(
            client,
            chat_id,
            min_id=start_id - 1,
            from_user="me",
            limit=_MAX_DELETE_SCAN_MESSAGES,
            deadline=deadline,
            stats=stats,
        ):
            considered += 1
            if exclude_message_id is not None and getattr(msg, "id", None) == exclude_message_id:
                continue
            batch.append(msg.id)
            if len(batch) >= settings_service.delete_batch_size():
                deleted_ids, _rejected = await delete_verified_self_messages(
                    client, chat_id, batch, exclude_message_id=exclude_message_id,
                    on_batch=_batch_progress(report), deadline=deadline,
                    stopped=stopped,
                )
                total += len(deleted_ids)
                batch = []
        if batch:
            deleted_ids, _rejected = await delete_verified_self_messages(
                client, chat_id, batch, exclude_message_id=exclude_message_id,
                on_batch=_batch_progress(report), deadline=deadline,
                stopped=stopped,
            )
            total += len(deleted_ids)
        record_event("delete", "del id", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
        if report is not None:
            report.considered = considered
            if stats.get("stopped"):
                report.complete = False
                report.reason = f"{stats['stopped']} reached"
            if stopped.get("reason"):
                report.complete = False
                report.reason = stopped["reason"]
        return total, None
    except Exception as exc:
        logger.error("del id failed: %s", exc)
        record_event("delete", "del id", 0, "ERROR", str(exc))
        partial = getattr(exc, "deleted_ids", []) or []
        total += len(partial)
        if report is not None:
            report.considered = considered
            report.deleted = total
            report.complete = False
            report.reason = str(exc)
        return total, exc


async def do_del_n(client, chat_id, n: int) -> str:
    if n < 1 or n > MAX_DELETE_COUNT:
        return f"{sym.STATE_WARNING} n must be between 1 and {MAX_DELETE_COUNT}."
    deleted, err = await do_del_n_counts(client, chat_id, n)
    if err is not None:
        return f"{sym.STATE_FAILED} Delete failed: {err}"
    if deleted < n:
        return f"{sym.OP_DELETE} Deleted `{deleted}` of the `{n}` requested messages."
    return f"{sym.OP_DELETE} Deleted `{deleted}` messages."


async def do_del_id(client, chat_id, start_id: int) -> str:
    deleted, err = await do_del_id_counts(client, chat_id, start_id)
    if err is not None:
        return f"{sym.STATE_FAILED} Delete failed: {err}"
    return f"{sym.OP_DELETE} Deleted `{deleted}` messages from ID `{start_id}` forward."


async def preview_self_owned_count(
    client, chat_id, *, sample: int = _DELETE_PREVIEW_SAMPLE,
) -> tuple[int, int]:
    """How many of the newest ``sample`` messages are the owner's.

    A cheap, bounded preview for a confirmation panel: it never walks the
    whole chat and never deletes anything. Returns ``(self_owned, scanned)``.
    """
    me_id = await _resolve_me_id(client)
    if me_id is None:
        return 0, 0
    owned = scanned = 0
    deadline = time.monotonic() + _DELETE_PREVIEW_SECONDS
    try:
        async for msg in _iter_messages_bounded(
            client, chat_id, limit=sample, deadline=deadline,
        ):
            scanned += 1
            if _is_self_owned(msg, me_id):
                owned += 1
    except Exception as exc:
        logger.warning("DELETE_PREVIEW failed chat_id=%s error=%s", chat_id, exc)
    return owned, scanned


async def recent_self_message_previews(
    client, chat_id, *, offset: int = 0, limit: int = 8,
) -> tuple[list[tuple[int, str]], bool]:
    """One page of the owner's recent messages as ``[(id, preview)]``.

    Reads the real history newest-first and keeps only a bounded text preview
    per row, so the panel never materialises a chat history. ``has_more``
    reports whether another page exists. Raises ``RuntimeError`` when the
    authenticated identity cannot be verified (ownership stays fail-closed).
    """
    me_id = await _resolve_me_id(client)
    if me_id is None:
        raise RuntimeError("authenticated account identity could not be verified")
    items: list[tuple[int, str]] = []
    skipped = 0
    has_more = False
    async for msg in _iter_messages_bounded(
        client, chat_id, limit=offset + limit + 1, from_user="me",
    ):
        if not _is_self_owned(msg, me_id):
            continue
        if skipped < offset:
            skipped += 1
            continue
        if len(items) >= limit:
            has_more = True
            break
        text = " ".join(_message_text(msg).split())
        preview = text[:40] + ("…" if len(text) > 40 else "") if text else "(no text)"
        items.append((int(msg.id), preview))
    return items, has_more


async def do_del_code(client, owner_id: int, code: str) -> str:
    code = code.upper().strip()
    t0 = asyncio.get_event_loop().time()
    try:
        row = await db_client.query_save(code)
        record_event("database", "query_save", (asyncio.get_event_loop().time() - t0) * 1000, "SUCCESS")
    except Exception as exc:
        logger.error("del save_code DB query failed: %s", exc)
        record_event("database", "query_save", 0, "ERROR", str(exc))
        return f"{sym.STATE_FAILED} DB error: {exc}"
    if not row:
        return f"{sym.STATE_FAILED} No saved item found for `{code}`"

    saved_chat_id = row.get("saved_chat_id")
    saved_msg_id = row.get("saved_msg_id")
    display = row.get("save_code") or code

    # One album code covers ONE Telegram album: resolve every member first, so
    # deleting the item can never leave orphaned album members behind. A
    # resolution failure deletes NOTHING and keeps the row.
    message_ids = [saved_msg_id] if saved_msg_id else []
    if save_service.is_album_row(row):
        members, album_error = await save_service.resolve_saved_album_messages(
            client, saved_chat_id, saved_msg_id
        )
        if album_error:
            logger.warning("del %s: album resolution failed: %s", code, album_error)
            return (
                f"{sym.STATE_FAILED} Could not delete `{display}`: {album_error}. "
                "Nothing was deleted."
            )
        message_ids = [member.id for member in members]

    tg_deleted = False
    tg_error = None
    if saved_chat_id and message_ids:
        try:
            await client.delete_messages(saved_chat_id, message_ids)
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
        return f"{sym.OP_DELETE} Deleted `{display}`"
    elif tg_deleted and not db_deleted:
        return f"{sym.STATE_WARNING} `{display}`: Telegram message deleted, but DB row removal failed: {db_error}"
    elif not tg_deleted and db_deleted:
        if tg_error:
            return f"{sym.STATE_WARNING} `{display}`: DB row deleted, but Telegram message deletion failed: {tg_error}"
        return f"{sym.OP_DELETE} Deleted `{display}` (Telegram message was already missing)"
    return f"{sym.STATE_FAILED} `{display}`: Both Telegram and DB deletion failed. TG: {tg_error}, DB: {db_error}"
