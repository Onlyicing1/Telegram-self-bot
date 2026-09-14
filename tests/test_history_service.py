"""
Telegram History Service — behavioral tests.

Source-proven contract this file pins:

  1. ``backend/services/history_service.py`` is the shared, reusable Telegram
     history capability. It reaches Telegram EXCLUSIVELY through the existing
     ``backend/telegram_api`` facade — no Telethon import, no direct
     ``client.iter_messages`` call.
  2. Retrieval is bounded and paginated: a strictly decreasing ``max_id``
     cursor, a page size, a per-request count, a raw scan cap, and a bounded
     ``rpc_await`` per page.
  3. Ordering is deterministic: every page is sorted by message id and the
     result is returned chronologically (oldest → newest) regardless of the
     order Telegram returned.
  4. Provenance eligibility is owned centrally, by reusing the durable marker
     helpers — an AI-answered/overwritten message is not human conversation
     history.
  5. Message text is returned losslessly; the marker is stripped but nothing is
     truncated.
  6. Failures are explicit (``HistoryError``), never a silent empty result.

No live Telegram is used: the fake client below mimics the exact Telethon
surface the facade consumes (``iter_messages`` with ``limit`` / ``min_id`` /
``max_id``, newest → oldest).
"""
from __future__ import annotations

import asyncio
import inspect
import re
from datetime import datetime, timezone
from typing import Any

import pytest

from backend.ai.context.provenance import AI_PROVENANCE_MARKER
from backend.ai.conversation.telegram_context import (
    MAX_CONTEXT_MESSAGES,
    MAX_MESSAGE_CHARS,
    MAX_TOTAL_CHARS,
    build_chat_context,
)
from backend.services import history_service
from backend.services.history_service import (
    DEFAULT_PAGE_SIZE,
    HISTORY_RPC_TIMEOUT_S,
    MAX_HISTORY_MESSAGES,
    MAX_HISTORY_SCAN_MESSAGES,
    MAX_PAGE_SIZE,
    HistoryError,
    fetch_history_page,
    fetch_recent_history,
    iter_history_pages,
)
from backend.telegram_api.api import TelegramAPI

CHAT = -100555000
NOW = datetime(2026, 9, 14, 14, 2, tzinfo=timezone.utc)


# ── Fake Telegram surface (mirrors Telethon) ──


class _FakeReplyTo:
    def __init__(self, reply_to_msg_id: int) -> None:
        self.reply_to_msg_id = reply_to_msg_id


class _FakeMessage:
    def __init__(
        self,
        mid: int,
        text: str = "",
        *,
        sender_id: int = 42,
        out: bool = True,
        media: bool = False,
        reply_to: int | None = None,
        chat_id: int = CHAT,
        date: datetime = NOW,
    ) -> None:
        self.id = mid
        self.chat_id = chat_id
        self.sender_id = sender_id
        self.text = text
        self.date = date
        self.media = object() if media else None
        self.reply_to = _FakeReplyTo(reply_to) if reply_to is not None else None
        self.out = out


class _FakeClient:
    """Telethon-shaped client: newest → oldest, ``max_id``/``min_id`` exclusive."""

    def __init__(self, messages: list[_FakeMessage]) -> None:
        self._messages = list(messages)
        self.calls: list[dict[str, Any]] = []

    def iter_messages(self, chat_id: Any, **kwargs: Any):
        self.calls.append({"chat_id": chat_id, **kwargs})
        limit = kwargs.get("limit")
        min_id = kwargs.get("min_id")
        max_id = kwargs.get("max_id")
        selected = [
            m
            for m in self._messages
            if (max_id is None or m.id < max_id) and (min_id is None or m.id > min_id)
        ]
        selected.sort(key=lambda m: m.id, reverse=True)
        if limit is not None:
            selected = selected[:limit]

        async def _aiter():
            for item in selected:
                yield item

        return _aiter()


class _FailingClient:
    def iter_messages(self, chat_id: Any, **kwargs: Any):
        async def _boom():
            raise ConnectionError("mtproto down")
            yield  # pragma: no cover

        return _boom()


class _SlowClient:
    def iter_messages(self, chat_id: Any, **kwargs: Any):
        async def _slow():
            await asyncio.sleep(5.0)
            yield _FakeMessage(1)  # pragma: no cover

        return _slow()


class _CancellingClient:
    def iter_messages(self, chat_id: Any, **kwargs: Any):
        async def _cancel():
            raise asyncio.CancelledError()
            yield  # pragma: no cover

        return _cancel()


def _human(mid: int, text: str = "hello") -> _FakeMessage:
    return _FakeMessage(mid, text)


def _ai(mid: int, text: str = "answer") -> _FakeMessage:
    return _FakeMessage(mid, f"{text}{AI_PROVENANCE_MARKER}")


def _conversation(count: int) -> list[_FakeMessage]:
    return [_human(mid, f"message {mid}") for mid in range(1, count + 1)]


# ── 1. Layering ──


def test_history_service_never_imports_telethon_or_iterates_the_client_itself():
    source = inspect.getsource(history_service)
    assert not re.search(r"^\s*(import|from)\s+telethon", source, re.MULTILINE)
    assert "client.iter_messages(" not in source
    assert "backend.telegram_api.messages import iter_messages" in source


@pytest.mark.asyncio
async def test_retrieval_goes_through_the_telegram_api_facade():
    client = _FakeClient(_conversation(5))
    slice_ = await fetch_recent_history(client, CHAT, count=5)
    assert [m.message_id for m in slice_.messages] == [1, 2, 3, 4, 5]
    # The facade is the only path: its bounded page shape is what Telegram saw.
    assert len(client.calls) == 1
    assert client.calls[0]["limit"] == DEFAULT_PAGE_SIZE
    assert client.calls[0]["chat_id"] == CHAT


@pytest.mark.asyncio
async def test_a_telegram_api_facade_and_a_raw_client_behave_identically():
    raw = _FakeClient(_conversation(30))
    via_facade = _FakeClient(_conversation(30))
    assert (
        await fetch_recent_history(raw, CHAT, count=30, page_size=10)
    ).messages == (
        await fetch_recent_history(TelegramAPI(via_facade), CHAT, count=30, page_size=10)
    ).messages
    assert raw.calls == via_facade.calls


# ── 2. Ordering and pagination ──


@pytest.mark.asyncio
async def test_messages_are_returned_chronologically_even_though_telegram_is_newest_first():
    slice_ = await fetch_recent_history(_FakeClient(_conversation(5)), CHAT, count=5)
    ids = [m.message_id for m in slice_.messages]
    assert ids == sorted(ids) == [1, 2, 3, 4, 5]


@pytest.mark.asyncio
async def test_pagination_uses_a_strictly_decreasing_exclusive_cursor():
    client = _FakeClient(_conversation(250))
    slice_ = await fetch_recent_history(client, CHAT, count=250, page_size=100)
    cursors = [call.get("max_id") for call in client.calls]
    assert cursors == [None, 151, 51]
    assert [m.message_id for m in slice_.messages] == list(range(1, 251))
    ids = [m.message_id for m in slice_.messages]
    assert len(ids) == len(set(ids))
    assert slice_.scanned == 250
    assert slice_.truncated is False


@pytest.mark.asyncio
async def test_page_size_is_honoured_and_clamped_to_the_hard_ceiling():
    client = _FakeClient(_conversation(20))
    await fetch_recent_history(client, CHAT, count=20, page_size=MAX_PAGE_SIZE * 10)
    assert client.calls[0]["limit"] == MAX_PAGE_SIZE

    small = _FakeClient(_conversation(20))
    await fetch_recent_history(small, CHAT, count=20, page_size=4)
    assert [call["limit"] for call in small.calls] == [4, 4, 4, 4, 4]


@pytest.mark.asyncio
async def test_page_boundaries_are_deterministic_across_runs():
    async def _pages():
        client = _FakeClient(_conversation(120))
        pages = [
            [m.message_id for m in page.messages]
            async for page in iter_history_pages(client, CHAT, count=120, page_size=50)
        ]
        return pages, [call.get("max_id") for call in client.calls]

    first, second = await _pages(), await _pages()
    assert first == second
    assert first[0] == [list(range(1, 51)), list(range(51, 101)), list(range(101, 121))]


@pytest.mark.asyncio
async def test_iter_history_pages_covers_every_message_exactly_once_in_order():
    client = _FakeClient(_conversation(250))
    seen: list[int] = []
    flags: list[bool] = []
    async for page in iter_history_pages(client, CHAT, count=250, page_size=100):
        assert [m.message_id for m in page.messages] == sorted(m.message_id for m in page.messages)
        seen.extend(m.message_id for m in page.messages)
        flags.append(page.has_more)
    assert seen == list(range(1, 251))
    assert flags == [True, True, False]


@pytest.mark.asyncio
async def test_iter_history_pages_yields_nothing_for_an_empty_chat():
    pages = [p async for p in iter_history_pages(_FakeClient([]), CHAT, count=10)]
    assert pages == []


# ── 3. Count / range semantics ──


@pytest.mark.asyncio
async def test_count_semantics_exact_and_more_than_available():
    exact = await fetch_recent_history(_FakeClient(_conversation(30)), CHAT, count=10)
    assert [m.message_id for m in exact.messages] == list(range(21, 31))
    assert exact.requested == 10
    assert exact.truncated is True

    plentiful = await fetch_recent_history(_FakeClient(_conversation(5)), CHAT, count=50)
    assert [m.message_id for m in plentiful.messages] == [1, 2, 3, 4, 5]
    assert plentiful.truncated is False


@pytest.mark.asyncio
async def test_zero_or_negative_count_never_touches_telegram():
    for value in (0, -5):
        client = _FakeClient(_conversation(10))
        slice_ = await fetch_recent_history(client, CHAT, count=value)
        assert slice_.messages == ()
        assert slice_.requested == 0
        assert client.calls == []


@pytest.mark.asyncio
async def test_requested_count_is_clamped_to_the_hard_bound():
    client = _FakeClient(_conversation(5))
    slice_ = await fetch_recent_history(client, CHAT, count=MAX_HISTORY_MESSAGES * 5)
    assert slice_.requested == MAX_HISTORY_MESSAGES


@pytest.mark.asyncio
async def test_before_id_and_after_id_bound_the_range_exclusively():
    client = _FakeClient(_conversation(50))
    slice_ = await fetch_recent_history(client, CHAT, count=50, before_id=40, after_id=30)
    assert [m.message_id for m in slice_.messages] == list(range(31, 40))
    assert client.calls[0]["max_id"] == 40
    assert client.calls[0]["min_id"] == 30


@pytest.mark.asyncio
async def test_fetch_history_page_walks_history_with_an_explicit_cursor():
    client = _FakeClient(_conversation(25))
    first = await fetch_history_page(client, CHAT, limit=10)
    assert [m.message_id for m in first.messages] == list(range(16, 26))
    assert first.has_more is True

    second = await fetch_history_page(client, CHAT, limit=10, before_id=first.messages[0].message_id)
    assert [m.message_id for m in second.messages] == list(range(6, 16))

    third = await fetch_history_page(client, CHAT, limit=10, before_id=second.messages[0].message_id)
    assert [m.message_id for m in third.messages] == list(range(1, 6))
    assert third.has_more is False


# ── 4. Provenance eligibility (central) ──


@pytest.mark.asyncio
async def test_ai_marked_messages_are_excluded_centrally():
    messages = [_human(1), _ai(2), _human(3), _ai(4)]
    slice_ = await fetch_recent_history(_FakeClient(messages), CHAT, count=10)
    assert [m.message_id for m in slice_.messages] == [1, 3]
    assert all(m.ai_provenance is False for m in slice_.messages)


@pytest.mark.asyncio
async def test_excluded_ai_messages_do_not_consume_the_requested_count():
    messages = [_ai(mid) for mid in range(1, 6)] + [_human(mid) for mid in range(6, 11)]
    slice_ = await fetch_recent_history(_FakeClient(messages), CHAT, count=3, page_size=5)
    assert [m.message_id for m in slice_.messages] == [8, 9, 10]


@pytest.mark.asyncio
async def test_include_ai_returns_marked_messages_classified_and_marker_free():
    messages = [_human(1, "hello"), _ai(2, "previous answer")]
    slice_ = await fetch_recent_history(
        _FakeClient(messages), CHAT, count=10, include_ai=True,
    )
    assert [m.message_id for m in slice_.messages] == [1, 2]
    marked = slice_.messages[1]
    assert marked.ai_provenance is True
    assert marked.text == "previous answer"
    assert AI_PROVENANCE_MARKER not in marked.text
    assert slice_.messages[0].ai_provenance is False


@pytest.mark.asyncio
async def test_scan_cap_bounds_a_window_that_is_entirely_ai_output(monkeypatch):
    monkeypatch.setattr(history_service, "MAX_HISTORY_SCAN_MESSAGES", 5)
    client = _FakeClient([_ai(mid) for mid in range(1, 21)])
    slice_ = await fetch_recent_history(client, CHAT, count=1, page_size=100)
    assert slice_.messages == ()
    assert slice_.scanned == 5
    assert slice_.truncated is True
    assert len(client.calls) == 1


# ── 5. Content integrity ──


@pytest.mark.asyncio
async def test_returned_text_is_never_truncated():
    long_text = "x" * 5000
    slice_ = await fetch_recent_history(_FakeClient([_human(1, long_text)]), CHAT, count=1)
    assert slice_.messages[0].text == long_text
    assert len(slice_.messages[0].text) > MAX_MESSAGE_CHARS


@pytest.mark.asyncio
async def test_marker_stripping_preserves_the_exact_visible_text():
    visible = "question\n┘─ answer\n    continuation"
    slice_ = await fetch_recent_history(
        _FakeClient([_ai(1, visible)]), CHAT, count=1, include_ai=True,
    )
    assert slice_.messages[0].text == visible


@pytest.mark.asyncio
async def test_message_identity_and_media_metadata_are_preserved():
    message = _FakeMessage(7, "caption", sender_id=99, out=False, media=True, reply_to=3)
    slice_ = await fetch_recent_history(_FakeClient([message]), CHAT, count=1)
    record = slice_.messages[0]
    assert record.message_id == 7
    assert record.chat_id == CHAT
    assert record.sender_id == 99
    assert record.out is False
    assert record.has_media is True
    assert record.reply_to_msg_id == 3
    assert record.date == NOW


# ── 6. Failure contract and robustness ──


@pytest.mark.asyncio
async def test_fetch_failure_raises_history_error_instead_of_returning_empty():
    with pytest.raises(HistoryError):
        await fetch_recent_history(_FailingClient(), CHAT, count=5)


@pytest.mark.asyncio
async def test_slow_rpc_is_bounded_and_surfaces_as_a_history_error(monkeypatch):
    monkeypatch.setattr(history_service, "HISTORY_RPC_TIMEOUT_S", 0.01)
    assert HISTORY_RPC_TIMEOUT_S > 0.01
    with pytest.raises(HistoryError):
        await fetch_recent_history(_SlowClient(), CHAT, count=5)


@pytest.mark.asyncio
async def test_cancellation_is_never_swallowed():
    with pytest.raises(asyncio.CancelledError):
        await fetch_recent_history(_CancellingClient(), CHAT, count=5)


@pytest.mark.asyncio
async def test_missing_client_and_unusable_chat_id_are_rejected():
    with pytest.raises(HistoryError):
        await fetch_recent_history(None, CHAT, count=5)
    with pytest.raises(HistoryError):
        await fetch_recent_history(_FakeClient([]), 0, count=5)


@pytest.mark.asyncio
async def test_concurrent_requests_do_not_share_state():
    client = _FakeClient(_conversation(60))
    first, second = await asyncio.gather(
        fetch_recent_history(client, CHAT, count=60, page_size=20),
        fetch_recent_history(client, CHAT, count=60, page_size=20),
    )
    assert first.messages == second.messages
    assert [m.message_id for m in first.messages] == list(range(1, 61))


# ── 7. The bounded conversational snapshot is unchanged ──


def test_bounded_context_bounds_are_unchanged():
    assert MAX_CONTEXT_MESSAGES == 10
    assert MAX_MESSAGE_CHARS == 200
    assert MAX_TOTAL_CHARS == 1500


def test_bounded_context_still_drops_marked_messages():
    marked = _FakeMessage(2, f"previous answer{AI_PROVENANCE_MARKER}")
    snapshot = build_chat_context(
        [_FakeMessage(1, "human"), marked, _FakeMessage(3, "after")],
        current_message_id=4,
        chat_id=CHAT,
    )
    assert [m.message_id for m in snapshot.messages] == [1, 3]
    assert AI_PROVENANCE_MARKER not in snapshot.render()
