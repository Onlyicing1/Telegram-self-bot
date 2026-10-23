"""Admission, acknowledgement, ordering and timing for callback queries.

Telethon delivers every callback query as its own update task (the client is
built without ``sequential_updates``), so a slow handler does NOT delay the
delivery of the next click. That is the good half of the truth. The other half
is what this module exists for:

* the router answered the query **after** the handler had read the database and
  edited the message, so the client's progress indicator stayed up for the
  entire handler — the acknowledgement is the one thing a click must not wait
  for (Telegram expires it: ``BOT_RESPONSE_TIMEOUT``);
* every click on the SAME panel ran concurrently with the previous one, so two
  "next page" clicks both read page 1, and whichever edit finished last decided
  what the owner saw — a slow earlier click could overwrite a newer view;
* a query delivered twice would run its handler (and its side effects) twice.

This module adds exactly three things and owns no dispatcher, task, queue or
executor of its own — Telethon still decides what runs and when:

``ack``
    Answer the callback query as the FIRST thing a click does, before any
    session lookup, database read or edit round trip.

``acquire_turn`` / ``release_turn``
    One FIFO turn per panel, keyed by ``(chat_id, msg_id)``. Clicks on the same
    panel run one at a time in arrival order; clicks on different panels never
    wait for each other. The wait for a turn is bounded: a click that cannot get
    its turn is dropped with a logged reason instead of executed later against
    a panel it no longer matches.

``admit`` / ``finish``
    Duplicate suppression by Telegram's own ``query_id`` (the same query
    delivered twice is one click; two distinct clicks always carry distinct
    ids, so a genuine double tap is never suppressed) plus one bounded timing
    record per click (received → acked → dispatched → rendered → finished).

Every function is loop-local and synchronous except ``ack`` and
``acquire_turn``; nothing here blocks the event loop.
"""
from __future__ import annotations

import asyncio
import logging
import time
from collections import OrderedDict, deque

logger = logging.getLogger(__name__)

#: How long a click waits for its panel turn before it is dropped. Generous on
#: purpose: a panel handler is a view render (a bounded database read plus one
#: edit), so reaching this bound means an earlier handler is genuinely stuck.
DEFAULT_TURN_WAIT_S = 20.0

#: Duplicate-delivery memory. Telegram query ids are unique per query, so 128
#: entries are far more than a burst of clicks needs while staying bounded.
_RECENT_QUERY_LIMIT = 128

#: Retained timing records, for diagnostics and tests.
_RECENT_TIMING_LIMIT = 64


def _short(data: str) -> str:
    """Callback data for a log line — bounded, never the whole payload."""
    return (data or "")[:64]


class CallbackAdmission:
    """One accepted callback query and the stages it passed through."""

    __slots__ = (
        "data",
        "query_id",
        "received_at",
        "acked_at",
        "dispatch_at",
        "rendered_at",
        "finished_at",
        "outcome",
        "ack_error",
    )

    def __init__(self, data: str, query_id: int | None) -> None:
        self.data = data or ""
        self.query_id = query_id
        self.received_at = time.monotonic()
        self.acked_at: float | None = None
        self.dispatch_at: float | None = None
        self.rendered_at: float | None = None
        self.finished_at: float | None = None
        self.outcome = "pending"
        self.ack_error = ""

    # ── marks ──

    def mark_ack(self, error: str = "") -> None:
        self.acked_at = time.monotonic()
        if error:
            self.ack_error = error

    def mark_dispatch(self) -> None:
        self.dispatch_at = time.monotonic()

    def mark_rendered(self) -> None:
        self.rendered_at = time.monotonic()

    # ── measured stages (milliseconds) ──

    @staticmethod
    def _ms(start: float | None, end: float | None) -> float:
        if start is None or end is None:
            return 0.0
        return max(0.0, (end - start) * 1000.0)

    @property
    def ack_ms(self) -> float:
        """Receipt → acknowledgement: the latency the owner's client waits on."""
        return self._ms(self.received_at, self.acked_at)

    @property
    def wait_ms(self) -> float:
        """Acknowledgement → dispatch start: time spent queued for the panel."""
        return self._ms(self.acked_at, self.dispatch_at)

    @property
    def work_ms(self) -> float:
        """Dispatch start → edit attempted: handler work plus the UI response."""
        return self._ms(self.dispatch_at, self.rendered_at)

    @property
    def total_ms(self) -> float:
        return self._ms(self.received_at, self.finished_at)


_recent_queries: OrderedDict[int, float] = OrderedDict()
_recent_timings: deque[CallbackAdmission] = deque(maxlen=_RECENT_TIMING_LIMIT)


def query_id_of(event) -> int | None:
    """Telegram's identifier for ONE callback query, or ``None``.

    Read from the raw update, never derived from the callback data: two
    different clicks on the same button are two queries, and one query that
    arrives twice is one.
    """
    candidate = getattr(getattr(event, "query", None), "query_id", None)
    if isinstance(candidate, int) and not isinstance(candidate, bool):
        return candidate
    return None


def admit(data: str, query_id: int | None) -> CallbackAdmission | None:
    """Accept one callback query, or return ``None`` for a duplicate delivery."""
    if query_id is not None:
        if query_id in _recent_queries:
            return None
        _recent_queries[query_id] = time.monotonic()
        while len(_recent_queries) > _RECENT_QUERY_LIMIT:
            _recent_queries.popitem(last=False)
    return CallbackAdmission(data, query_id)


async def ack(event, admission: CallbackAdmission) -> None:
    """Answer the callback query — before any handler work.

    A refused answer (an expired query, a disconnected helper) is recorded on
    the admission and logged, and the click is still dispatched: the answer is
    a courtesy to the client, never a precondition for the operation.
    """
    error = ""
    try:
        await event.answer()
    except Exception as exc:  # noqa: BLE001 — an unanswered click still runs
        error = f"{type(exc).__name__}: {exc}"
        logger.info(
            "[CALLBACK] ack refused data='%s' query_id=%s: %s",
            _short(admission.data), admission.query_id, error,
        )
    admission.mark_ack(error)


class _Turn:
    """The one FIFO turn for a panel message."""

    __slots__ = ("busy", "waiters")

    def __init__(self) -> None:
        self.busy = False
        self.waiters: deque[asyncio.Future] = deque()


_turns: dict[tuple[int, int], _Turn] = {}


def _key(chat_id: int | None, msg_id: int | None) -> tuple[int, int]:
    return (int(chat_id or 0), int(msg_id or 0))


def _grant(entry: _Turn) -> None:
    """Hand the turn to the next waiter, or mark the panel idle."""
    while entry.waiters:
        waiter = entry.waiters.popleft()
        if waiter.done():
            continue
        waiter.set_result(True)
        return
    entry.busy = False


async def acquire_turn(
    chat_id: int | None,
    msg_id: int | None,
    wait: float | None = None,
) -> _Turn | None:
    """Take this panel's turn, or ``None`` if it could not be taken in time."""
    wait = DEFAULT_TURN_WAIT_S if wait is None else wait
    key = _key(chat_id, msg_id)
    entry = _turns.get(key)
    if entry is None:
        entry = _turns[key] = _Turn()
    if not entry.busy:
        entry.busy = True
        return entry

    waiter = asyncio.get_running_loop().create_future()
    entry.waiters.append(waiter)
    try:
        await asyncio.wait_for(waiter, wait)
        return entry
    except asyncio.TimeoutError:
        try:
            entry.waiters.remove(waiter)
        except ValueError:
            # The turn was granted exactly as the wait expired: pass it on so
            # the panel is not left looking busy to every later click.
            _grant(entry)
        return None
    except asyncio.CancelledError:
        try:
            entry.waiters.remove(waiter)
        except ValueError:
            _grant(entry)
        raise


def release_turn(chat_id: int | None, msg_id: int | None, entry: _Turn) -> None:
    """Release this panel's turn; always paired with ``acquire_turn``."""
    key = _key(chat_id, msg_id)
    _grant(entry)
    if not entry.busy and not entry.waiters and _turns.get(key) is entry:
        _turns.pop(key, None)


def finish(admission: CallbackAdmission, outcome: str) -> None:
    """Close one click's record: its outcome and its measured stages."""
    admission.outcome = outcome
    admission.finished_at = time.monotonic()
    _recent_timings.append(admission)
    logger.info(
        "[CALLBACK] timing data='%s' outcome=%s ack_ms=%.1f wait_ms=%.1f "
        "work_ms=%.1f total_ms=%.1f%s",
        _short(admission.data),
        outcome,
        admission.ack_ms,
        admission.wait_ms,
        admission.work_ms,
        admission.total_ms,
        f" ack_error={admission.ack_error}" if admission.ack_error else "",
    )


def recent_timings() -> list[CallbackAdmission]:
    """The retained timing records, oldest first."""
    return list(_recent_timings)


def reset() -> None:
    """Drop every gate, duplicate record and timing (tests, shutdown)."""
    _recent_queries.clear()
    _recent_timings.clear()
    _turns.clear()
