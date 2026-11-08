"""Manual "All my messages": total deadline, partial scan, caps and progress.

These tests drive the real panel handler and the real ``delete_service``
against a deterministic fake Telethon client. They lock in the live failure
that motivated this fix: All mode scanned an unbounded history with no total
deadline (so the panel simply hung), discarded candidates it had already
verified when the scan raised, and never enforced its documented candidate
bound.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from telethon.tl.types import InputBotInlineMessageID

from backend.bot.handlers import delete as delete_ui
from backend.helper import callback_gate, inline_engine, input_state, target_context
from backend.services import delete_service

OWNER = 111
CHAT = -100
PANEL_MSG = 500
INLINE_ID = InputBotInlineMessageID(dc_id=2, id=1, access_hash=2)


class Message:
    def __init__(self, mid, *, out=True, sender_id=OWNER, text=""):
        self.id = mid
        self.out = out
        self.sender_id = sender_id
        self.message = text
        self.text = text
        self.date = None
        self.media = None
        self.reply_to_msg_id = None


class FakeClient:
    def __init__(
        self,
        messages,
        *,
        iter_delay: float = 0.0,
        iter_error_at: int | None = None,
        batch_delay: float = 0.0,
        fail_on_batch: int | None = None,
    ):
        self.messages = {message.id: message for message in messages}
        self.me = SimpleNamespace(id=OWNER)
        self.deleted: list[int] = []
        self.batches: list[list[int]] = []
        self.edits: list[tuple[int, int, str]] = []
        self.iter_delay = iter_delay
        self.iter_error_at = iter_error_at
        self.batch_delay = batch_delay
        self.fail_on_batch = fail_on_batch
        self.edit_delay = 0.0
        self.fail_edits = False
        self.edits_in_flight = 0
        self.max_concurrent_edits = 0
        self.fetch_sizes: list[int] = []
        self.missing_on_fetch: set[int] = set()
        #: Any panel edit attempted with the owner's OWN account. The Glass
        #: panel belongs to the Helper Bot, so tests assert this stays empty.
        self.self_edits: list[tuple] = []

    async def iter_messages(self, chat_id, **kwargs):
        limit = kwargs.get("limit")
        from_user = kwargs.get("from_user")
        min_id = kwargs.get("min_id")
        emitted = 0
        for mid in sorted(self.messages, reverse=True):
            message = self.messages[mid]
            if min_id is not None and mid <= min_id:
                continue
            if from_user == "me" and not message.out:
                continue
            if self.iter_error_at is not None and emitted >= self.iter_error_at:
                raise ConnectionError("history read failed")
            if self.iter_delay:
                await asyncio.sleep(self.iter_delay)
            yield message
            emitted += 1
            if limit is not None and emitted >= limit:
                return

    async def get_messages(self, chat_id, ids):
        if isinstance(ids, (list, tuple)):
            self.fetch_sizes.append(len(ids))
            return [
                None if mid in self.missing_on_fetch else self.messages.get(mid)
                for mid in ids
            ]
        return self.messages.get(ids)

    async def delete_messages(self, chat_id, ids):
        batch = list(ids) if isinstance(ids, (list, tuple)) else [ids]
        if self.batch_delay:
            await asyncio.sleep(self.batch_delay)
        self.batches.append(batch)
        if self.fail_on_batch is not None and len(self.batches) == self.fail_on_batch:
            raise RuntimeError("Telegram RPC failed")
        for mid in batch:
            self.messages.pop(mid, None)
        self.deleted.extend(batch)

    async def edit_message(self, chat_id, message_id, text, **kwargs):
        self.self_edits.append((chat_id, message_id, text))


class FakeHelper:
    """The Helper Bot: the ONLY principal allowed to edit the Glass panel."""

    def __init__(self, client: FakeClient) -> None:
        self._client = client
        self.inline_ids: list = []
        self.edits: list[tuple] = []

    async def edit_message(self, inline_id, text=None, buttons=None, **kwargs):
        client = self._client
        client.edits_in_flight += 1
        client.max_concurrent_edits = max(client.max_concurrent_edits, client.edits_in_flight)
        self.inline_ids.append(inline_id)
        try:
            if client.edit_delay:
                await asyncio.sleep(client.edit_delay)
            if client.fail_edits:
                raise RuntimeError("panel edit refused")
            self.edits.append((inline_id, text))
            client.edits.append((None, None, text))
        finally:
            client.edits_in_flight -= 1


class FakeEvent:
    def __init__(
        self, *, chat_id=CHAT, message_id=PANEL_MSG, inline_identifier=INLINE_ID,
    ):
        self.chat_id = chat_id
        self.message_id = message_id
        self.inline_message_id = None
        self.id = message_id
        self.out = True
        # A callback on an inline panel carries Telegram's identifier in
        # ``query.msg_id`` — that is what ``inline_message_id_from_event`` reads.
        self.query = SimpleNamespace(msg_id=inline_identifier)


def install(monkeypatch, messages, **kwargs) -> FakeClient:
    client = FakeClient(messages, **kwargs)
    client.helper = FakeHelper(client)
    monkeypatch.setattr(inline_engine, "_self_client", client)
    monkeypatch.setattr(delete_ui, "get_client", lambda: client.helper)
    return client


def button_data(buttons) -> list[str]:
    out: list[str] = []
    for row in buttons:
        for button in row:
            data = getattr(button, "data", b"")
            out.append(data.decode() if isinstance(data, bytes) else str(data))
    return out


@pytest.fixture(autouse=True)
def _isolated():
    input_state.clear_all()
    target_context.clear_all()
    for key in list(callback_gate._painters):
        callback_gate.forget_painter(*key)
    yield
    input_state.clear_all()
    target_context.clear_all()


# ── 1/2. the panel's All path carries its own total deadline ────────────────


@pytest.mark.asyncio
async def test_manual_all_applies_its_own_total_deadline(monkeypatch):
    monkeypatch.setattr(delete_service, "_DELETE_OPERATION_DEADLINE_SECONDS", 0.15)
    client = install(
        monkeypatch, [Message(i) for i in range(1, 4001)], iter_delay=0.001,
    )

    started = time.monotonic()
    _title, body, _buttons = await delete_ui._del_panel_handler(FakeEvent(), "allexec")
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert "Partial result" in body
    assert "deadline" in body
    assert len(client.deleted) < 4000


@pytest.mark.asyncio
async def test_slow_history_returns_a_partial_result_instead_of_hanging(monkeypatch):
    monkeypatch.setattr(delete_service, "_DELETE_OPERATION_DEADLINE_SECONDS", 0.1)
    client = install(
        monkeypatch, [Message(i) for i in range(1, 100001)], iter_delay=0.002,
    )

    started = time.monotonic()
    _title, body, _buttons = await delete_ui._del_panel_handler(FakeEvent(), "allexec")
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert "Partial result" in body
    assert client.batches == [] or all(len(b) <= 100 for b in client.batches)


# ── 3/4. a failed scan keeps its verified candidates, a failed start does not ─


@pytest.mark.asyncio
async def test_history_failure_after_valid_ids_still_deletes_them(monkeypatch):
    client = install(
        monkeypatch, [Message(i) for i in range(1, 41)], iter_error_at=25,
    )

    _title, body, _buttons = await delete_ui._del_panel_handler(FakeEvent(), "allexec")

    # The scan raised after 25 verified candidates; those must still be
    # processed through the ownership chokepoint.
    assert sorted(client.deleted, reverse=True) == list(range(40, 15, -1))
    assert len(client.deleted) == 25
    assert "did not finish" in body
    assert "Deleted: 25" in body
    assert "Rejected: 0" in body
    assert "Partial result" in body


@pytest.mark.asyncio
async def test_failure_before_any_valid_id_deletes_nothing(monkeypatch):
    client = install(
        monkeypatch, [Message(i) for i in range(1, 21)], iter_error_at=0,
    )

    _title, body, _buttons = await delete_ui._del_panel_handler(FakeEvent(), "allexec")

    assert client.batches == []
    assert client.deleted == []
    assert "did not finish" in body
    assert "Deleted: 0" in body


# ── 5/6. the documented bounds are enforced and reported ────────────────────


@pytest.mark.asyncio
async def test_all_mode_never_selects_more_than_the_candidate_bound(monkeypatch):
    monkeypatch.setattr(delete_service, "MAX_DELETE_COUNT", 7)
    client = install(monkeypatch, [Message(i) for i in range(1, 51)])

    selection = await delete_service.select_self_owned_message_ids(client, CHAT)

    assert len(selection.ids) == 7
    assert selection.complete is False
    assert "7" in selection.reason
    assert "limit" in selection.reason


@pytest.mark.asyncio
async def test_reaching_the_history_scan_bound_is_partial(monkeypatch):
    monkeypatch.setattr(delete_service, "_MAX_DELETE_SCAN_MESSAGES", 5)
    client = install(monkeypatch, [Message(i) for i in range(1, 21)])

    selection = await delete_service.select_self_owned_message_ids(client, CHAT)

    assert len(selection.ids) == 5
    assert selection.complete is False
    assert "history scan limit" in selection.reason


# ── 7/8. deadline mid-deletion: real counts, no duplicates ─────────────────


@pytest.mark.asyncio
async def test_deadline_mid_deletion_keeps_completed_batch_counts(monkeypatch):
    monkeypatch.setattr(delete_service, "_DELETE_OPERATION_DEADLINE_SECONDS", 0.4)
    client = install(
        monkeypatch, [Message(i) for i in range(1, 901)], batch_delay=0.05,
    )

    _title, body, _buttons = await delete_ui._del_panel_handler(FakeEvent(), "allexec")

    assert 0 < len(client.deleted) < 900
    assert len(client.batches) < 9
    assert "Partial result" in body
    assert f"Deleted: {len(client.deleted):,}" in body


@pytest.mark.asyncio
async def test_no_id_is_deleted_twice_after_a_deadline_stop(monkeypatch):
    monkeypatch.setattr(delete_service, "_DELETE_OPERATION_DEADLINE_SECONDS", 0.3)
    client = install(
        monkeypatch, [Message(i) for i in range(1, 1201)], batch_delay=0.04,
    )

    await delete_ui._del_panel_handler(FakeEvent(), "allexec")

    assert len(client.deleted) == len(set(client.deleted))
    attempted = [mid for batch in client.batches for mid in batch]
    assert sorted(attempted) == sorted(client.deleted)


# ── 9. foreign messages stay untouched on the error path ───────────────────


@pytest.mark.asyncio
async def test_foreign_messages_are_never_deleted_when_the_scan_fails(monkeypatch):
    messages = [
        Message(i, out=(i % 2 == 0), sender_id=(OWNER if i % 2 == 0 else 222))
        for i in range(1, 61)
    ]
    client = install(monkeypatch, messages, iter_error_at=40)

    await delete_ui._del_panel_handler(FakeEvent(), "allexec")

    assert client.deleted, "the verified candidates should still be deleted"
    assert all(mid % 2 == 0 for mid in client.deleted)
    assert all(mid not in client.deleted for mid in range(1, 61, 2))


# ── 10/11. preview responsiveness and live progress ────────────────────────


@pytest.mark.asyncio
async def test_all_preview_is_bounded_and_still_renders_confirmation(monkeypatch):
    monkeypatch.setattr(delete_service, "_DELETE_PREVIEW_SECONDS", 0.05)
    install(monkeypatch, [Message(i) for i in range(1, 501)], iter_delay=0.01)

    started = time.monotonic()
    _title, body, buttons = await delete_ui._del_panel_handler(FakeEvent(), "all")
    elapsed = time.monotonic() - started

    assert elapsed < 5.0
    assert "Preview" in body
    assert "panel:del:allexec" in button_data(buttons)


@pytest.mark.asyncio
async def test_long_operation_paints_progress_and_an_accurate_result(monkeypatch):
    monkeypatch.setattr(delete_ui, "_PROGRESS_PAINT_INTERVAL", 0.0)
    monkeypatch.setattr(delete_service, "_DELETE_PROGRESS_EVERY", 5)
    client = install(
        monkeypatch, [Message(i) for i in range(1, 121)], batch_delay=0.01,
    )

    title, body, _buttons = await delete_ui._del_panel_handler(FakeEvent(), "allexec")

    progress_texts = [text for _chat, _msg, text in client.edits]
    assert any("Scanning chat" in text and "Scanned: 5 messages" in text
               for text in progress_texts)
    assert any("Processing 0%" in text and "Deleted: 0 / 120" in text
               for text in progress_texts)
    assert "Deleted: 120" in body
    assert title == "Delete — Completed"


# ── 12+. large chat: capped counters, a real bar, and no edit spam ─────────


def _scan_counts(texts: list[str]) -> list[int]:
    return [
        int(text.split("Scanned: ")[1].split(" messages")[0].replace(",", ""))
        for text in texts
        if "Scanned: " in text
    ]


@pytest.mark.asyncio
async def test_ten_thousand_message_chat_paints_real_scan_counters(monkeypatch):
    monkeypatch.setattr(delete_ui, "_PROGRESS_PAINT_INTERVAL", 0.0)
    monkeypatch.setattr(delete_service, "_DELETE_PROGRESS_EVERY", 1000)
    client = install(monkeypatch, [Message(i) for i in range(1, 10001)])

    title, body, _buttons = await delete_ui._del_panel_handler(FakeEvent(), "allexec")

    scanning = [text for _c, _m, text in client.edits if "Scanning chat" in text]
    counts = _scan_counts(scanning)
    assert len(counts) >= 5          # painted WHILE scanning, not only at the end
    assert counts[0] == 0
    assert counts == sorted(counts)
    assert counts[-1] == 10000
    assert "%" not in "".join(scanning)  # never a fabricated scan percentage
    assert "Delete — All My Messages" in scanning[0]
    assert "Scanned: 10,000" in body
    assert "Found: 10,000" in body
    assert "Deleted: 10,000" in body
    assert "Rejected: 0" in body
    assert title == "Delete — Completed"
    assert "All selected messages were processed." in body


@pytest.mark.asyncio
async def test_progress_bar_starts_empty_and_fills_only_on_resolved_work(monkeypatch):
    monkeypatch.setattr(delete_ui, "_PROGRESS_PAINT_INTERVAL", 0.0)
    client = install(monkeypatch, [Message(i) for i in range(1, 1001)])

    _title, body, _buttons = await delete_ui._del_panel_handler(FakeEvent(), "allexec")

    deleting = [text for _c, _m, text in client.edits if "Processing " in text]
    percentages = [int(t.split("Processing ")[1].split("%")[0]) for t in deleting]
    assert percentages[0] == 0
    assert percentages == sorted(percentages)
    assert percentages[-1] == 100
    assert "▱▱▱▱▱" in deleting[0]
    assert "Deleted: 0 / 1,000" in deleting[0]
    assert "▰▰▰▰▰" in body


@pytest.mark.asyncio
async def test_ownership_rejections_are_counted_exactly_once(monkeypatch):
    monkeypatch.setattr(delete_ui, "_PROGRESS_PAINT_INTERVAL", 60.0)
    client = install(monkeypatch, [Message(i) for i in range(1, 301)])
    # Messages that vanish between selection and the ownership fetch are
    # rejections the chokepoint discovers — counted once, never twice.
    client.missing_on_fetch = set(range(251, 301))

    title, body, _buttons = await delete_ui._del_panel_handler(FakeEvent(), "allexec")

    assert "Found: 300" in body
    assert "Deleted: 250" in body
    assert "Rejected: 50" in body
    assert "Rejected: 100" not in body
    assert title == "Delete — Completed"
    assert "All selected messages were processed." in body


@pytest.mark.asyncio
async def test_telegram_delete_failure_is_counted_once(monkeypatch):
    monkeypatch.setattr(delete_ui, "_PROGRESS_PAINT_INTERVAL", 60.0)
    client = install(monkeypatch, [Message(i) for i in range(1, 301)])
    client.fail_on_batch = 2

    title, body, _buttons = await delete_ui._del_panel_handler(FakeEvent(), "allexec")

    assert "Deleted: 200" in body
    assert "Rejected: 100" in body
    assert "Rejected: 200" not in body
    assert title == "Delete — Stopped"
    assert "Delete did not finish" in body
    assert "All selected messages were processed." not in body


@pytest.mark.asyncio
async def test_unprocessed_candidates_are_not_reported_as_rejected(monkeypatch):
    monkeypatch.setattr(delete_service, "_DELETE_OPERATION_DEADLINE_SECONDS", 0.25)
    monkeypatch.setattr(delete_ui, "_PROGRESS_PAINT_INTERVAL", 60.0)
    client = install(
        monkeypatch, [Message(i) for i in range(1, 3001)], batch_delay=0.03,
    )

    title, body, _buttons = await delete_ui._del_panel_handler(FakeEvent(), "allexec")

    assert 0 < len(client.deleted) < 3000
    assert f"Deleted: {len(client.deleted):,}" in body
    assert "Rejected: 0" in body
    assert "Remaining: " in body
    assert "Partial result" in body
    assert title == "Delete — Stopped"


@pytest.mark.asyncio
async def test_repeated_paints_inside_the_interval_edit_once(monkeypatch):
    monkeypatch.setattr(delete_ui, "_PROGRESS_PAINT_INTERVAL", 60.0)
    client = install(monkeypatch, [Message(1)])
    paint = delete_ui._progress_painter(FakeEvent())

    await paint({"scanned": 1, "considered": 1})
    for n in range(2, 20):
        await paint({"scanned": n, "considered": n})

    assert len(client.edits) == 1
    assert "Scanned: 1 messages" in client.edits[0][2]


@pytest.mark.asyncio
async def test_unchanged_counters_never_repaint(monkeypatch):
    monkeypatch.setattr(delete_ui, "_PROGRESS_PAINT_INTERVAL", 0.0)
    client = install(monkeypatch, [Message(1)])
    paint = delete_ui._progress_painter(FakeEvent())
    snapshot = {"total": 100, "deleted": 10, "rejected": 0, "resolved": 10,
                "remaining": 90}

    await paint(snapshot)
    await paint(dict(snapshot))
    await paint(dict(snapshot))

    assert len(client.edits) == 1


@pytest.mark.asyncio
async def test_a_slow_panel_edit_never_stacks_pending_edits(monkeypatch):
    monkeypatch.setattr(delete_ui, "_PROGRESS_PAINT_INTERVAL", 0.0)
    client = install(monkeypatch, [Message(1)])
    client.edit_delay = 0.05
    paint = delete_ui._progress_painter(FakeEvent())

    await asyncio.gather(*(
        paint({"total": 100, "deleted": n, "rejected": 0, "resolved": n,
               "remaining": 100 - n})
        for n in range(1, 21)
    ))

    assert client.max_concurrent_edits == 1
    assert len(client.edits) == 1


@pytest.mark.asyncio
async def test_a_failed_panel_edit_never_aborts_the_deletion(monkeypatch):
    monkeypatch.setattr(delete_ui, "_PROGRESS_PAINT_INTERVAL", 0.0)
    client = install(monkeypatch, [Message(i) for i in range(1, 401)])
    client.fail_edits = True

    title, body, _buttons = await delete_ui._del_panel_handler(FakeEvent(), "allexec")

    assert len(client.deleted) == 400
    assert client.edits == []
    assert "Deleted: 400" in body
    assert title == "Delete — Completed"


@pytest.mark.asyncio
async def test_large_deletion_never_exceeds_the_chunk_size(monkeypatch):
    monkeypatch.setattr(delete_ui, "_PROGRESS_PAINT_INTERVAL", 60.0)
    client = install(monkeypatch, [Message(i) for i in range(1, 1001)])

    await delete_ui._del_panel_handler(FakeEvent(), "allexec")

    assert len(client.deleted) == 1000
    assert len(client.deleted) == len(set(client.deleted))
    assert all(
        len(batch) <= delete_service._DELETE_VERIFY_CHUNK for batch in client.batches
    )
    assert all(
        size <= delete_service._DELETE_VERIFY_CHUNK for size in client.fetch_sizes
    )


@pytest.mark.asyncio
async def test_a_large_deletion_yields_control_to_the_event_loop(monkeypatch):
    monkeypatch.setattr(delete_ui, "_PROGRESS_PAINT_INTERVAL", 60.0)
    client = install(monkeypatch, [Message(i) for i in range(1, 2001)])
    client.iter_delay = 0.0005
    client.batch_delay = 0.0005
    ticks = 0
    stop = False

    async def ticker():
        nonlocal ticks
        while not stop:
            ticks += 1
            await asyncio.sleep(0.001)

    task = asyncio.create_task(ticker())
    try:
        await delete_ui._del_panel_handler(FakeEvent(), "allexec")
    finally:
        stop = True
        await task

    assert len(client.deleted) == 2000
    assert ticks > 5


@pytest.mark.asyncio
async def test_foreign_messages_stay_untouched_in_a_large_chat(monkeypatch):
    monkeypatch.setattr(delete_ui, "_PROGRESS_PAINT_INTERVAL", 60.0)
    messages = [
        Message(i, out=(i % 10 != 0), sender_id=(OWNER if i % 10 != 0 else 222))
        for i in range(1, 2001)
    ]
    client = install(monkeypatch, messages)

    _title, body, _buttons = await delete_ui._del_panel_handler(FakeEvent(), "allexec")

    expected = {m.id for m in messages if m.out}
    assert set(client.deleted) == expected
    assert len(expected) == 1800
    assert "Found: 1,800" in body
    assert "Deleted: 1,800" in body
