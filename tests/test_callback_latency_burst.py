"""Callback latency and burst behaviour on the REAL Stash selection path.

The backlog fix made acknowledgement immediate and ordered clicks per panel,
but a tick still cost four database round trips (an item lookup, then the page
plus its exact count) and its own Telegram edit. A burst of ticks therefore
became a burst of edits, each waiting behind the previous one's network round
trip — the interface looked frozen and then caught up all at once.

These tests drive the production router, the production retrieve panel/action
handlers and the production selection state. Only the two ends are fakes: the
Telegram RPCs (a fixed, test-chosen latency) and the database calls (a fixed,
test-chosen latency wrapped around the in-memory fallback). That makes the
numbers below the click path's own cost plus deliberate delays — never a guess
about a real network.

Measured per test (printed with ``-s``):
  * burst completion time for 10 / 15 / 30 distinct clicks,
  * per-click state-application latency,
  * database reads and Telegram edits per burst,
  * the click's own stage record (``ack_ms``, ``db``, ``edits``, ``coalesced``).
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from telethon import events

from backend.bot.handlers import retrieve as handler
from backend.db import client as db_client
from backend.helper import callback_gate, inline_engine, panels, selection_state
from backend.helper import symbols as sym
from backend.helper.lifecycle import get_lifecycle

OWNER = 7780001
OTHER_OWNER = 7780002
CHAT = -1001234567890
MSG = 4242

#: Simulated network cost of one Telegram edit, and of one database call.
EDIT_DELAY = 0.020
DB_DELAY = 0.010


# ── fakes ──────────────────────────────────────────────────────────────────

class _Click:
    """One CallbackQuery event: answers and edits at a chosen latency."""

    def __init__(
        self,
        data: str,
        query_id: int,
        *,
        chat_id: int = CHAT,
        msg_id: int = MSG,
        edit_delay: float = EDIT_DELAY,
        answer_delay: float = 0.0,
    ) -> None:
        self.data = data.encode("utf-8")
        self.sender_id = OWNER
        self.chat_id = chat_id
        self.message_id = msg_id
        self.inline_message_id = None
        self.query = SimpleNamespace(query_id=query_id, msg_id=msg_id)
        self.edit_delay = edit_delay
        self.answer_delay = answer_delay
        self.created_at = time.monotonic()
        self.answer_count = 0
        self.edits: list[tuple[str, list]] = []
        self.edit_times: list[float] = []

    @property
    def texts(self) -> list[str]:
        return [text for text, _buttons in self.edits]

    def _buttons(self, index: int = -1) -> list:
        if not self.edits:
            return []
        return [button for row in self.edits[index][1] or [] for button in row]

    def datas(self, index: int = -1) -> list[str]:
        out = []
        for button in self._buttons(index):
            data = getattr(button, "data", None)
            out.append(data.decode("utf-8") if isinstance(data, bytes) else str(data))
        return out

    def labels(self, index: int = -1) -> list[str]:
        return [str(getattr(button, "text", "")) for button in self._buttons(index)]

    async def answer(self, *args, **kwargs) -> None:
        self.answer_count += 1
        if self.answer_delay:
            await asyncio.sleep(self.answer_delay)

    async def edit(self, text, buttons=None) -> None:
        if self.edit_delay:
            await asyncio.sleep(self.edit_delay)
        self.edits.append((text, buttons))
        self.edit_times.append(time.monotonic())


class _Db:
    """Counted, delayed stand-in for the two reads the Stash path performs."""

    def __init__(self, monkeypatch) -> None:
        self.list_calls = 0
        self.query_calls = 0
        self.delay = DB_DELAY

        real_list = db_client.list_saves
        real_query = db_client.query_save

        async def counted_list(owner_id, limit=50, offset=0):
            self.list_calls += 1
            if self.delay:
                await asyncio.sleep(self.delay)
            return await real_list(owner_id, limit=limit, offset=offset)

        async def counted_query(*args, **kwargs):
            self.query_calls += 1
            if self.delay:
                await asyncio.sleep(self.delay)
            return await real_query(*args, **kwargs)

        monkeypatch.setattr(db_client, "list_saves", counted_list)
        monkeypatch.setattr(db_client, "query_save", counted_query)

    @property
    def reads(self) -> int:
        return self.list_calls + self.query_calls


def _capture_router():
    captured: dict = {}

    def _on(*args, **_kwargs):
        builder = args[0] if args else None

        def _decorator(func):
            if isinstance(builder, events.CallbackQuery):
                captured.setdefault("router", func)
            return func

        return _decorator

    panels.register_callback_handlers(SimpleNamespace(on=_on), OWNER)
    return captured["router"]


def _seed(code: str, *, owner: int = OWNER, name: str | None = None) -> dict:
    row = {
        "id": len(db_client._fallback["saved_items"]) + 1,
        "save_code": code,
        "owner_id": owner,
        "display_name": name or f"Item {code}",
        "file_name": f"{code}.jpg",
        "media_type": "Photo",
        "mime_type": "image/jpeg",
        "file_size": 2048,
        "caption": "",
        "saved_chat_id": -1000,
        "saved_msg_id": 500 + len(db_client._fallback["saved_items"]),
    }
    db_client._fallback["saved_items"].append(row)
    return row


@pytest.fixture
def rig(monkeypatch):
    """The production router + retrieve handlers over fake network ends."""
    callback_gate.reset()
    selection_state.clear_all()
    handler._snapshot_invalidate()
    get_lifecycle().sessions.clear_all()
    get_lifecycle().timers.stop_all()
    db_client._fallback["saved_items"] = []
    db_client._fallback["bot_logs"] = []
    inline_engine.set_owner_id(OWNER)
    inline_engine.set_self_client(None)
    handler.register(None, OWNER)

    applied: list[tuple[str, bool, float]] = []
    real_toggle = selection_state.toggle

    def spy_toggle(owner_id, code, label=""):
        result = real_toggle(owner_id, code, label)
        applied.append((code, result[0], time.monotonic()))
        return result

    monkeypatch.setattr(selection_state, "toggle", spy_toggle)

    state = SimpleNamespace(
        router=_capture_router(),
        db=_Db(monkeypatch),
        applied=applied,
        query_ids=iter(range(90000, 99999)),
    )
    state.click = lambda data, **kw: _Click(data, next(state.query_ids), **kw)

    get_lifecycle().sessions.create(CHAT, MSG, "retrieve")
    yield state

    callback_gate.reset()
    selection_state.clear_all()
    handler._snapshot_invalidate()
    get_lifecycle().sessions.clear_all()
    get_lifecycle().timers.stop_all()
    db_client._fallback["saved_items"] = []
    db_client._fallback["bot_logs"] = []
    inline_engine.set_self_client(None)


async def _open_selection(rig, count: int = 8) -> list[str]:
    """Seed ``count`` items, open the selection view, return the tapped codes."""
    for index in range(1, count + 1):
        _seed(f"S{index:04d}")
    click = rig.click("panel:retrieve_saved:select")
    await rig.router(click)
    codes = [
        data[len("action:retrieve_toggle:"):]
        for data in click.datas()
        if data.startswith("action:retrieve_toggle:")
    ]
    assert len(codes) == count
    return codes


def _last_paint(clicks: list[_Click]) -> _Click:
    """The click that performed the NEWEST edit (the panel's final view).

    The newest view may be painted by an earlier click whose paint was already
    in flight — that is the whole point of the writer — so the panel's final
    content is "whichever edit happened last", not "the last click's edit".
    """
    latest: tuple[float, _Click] | None = None
    for click in clicks:
        if click.edit_times:
            stamp = max(click.edit_times)
            if latest is None or stamp > latest[0]:
                latest = (stamp, click)
    assert latest is not None, "nothing was painted"
    return latest[1]


def _marks(click: _Click) -> tuple[int, int]:
    """Selected / unselected ROW marks in the panel's buttons."""
    labels = click.labels()
    return (
        sum(1 for label in labels if label.startswith(sym.SELECT_MARKED)),
        sum(1 for label in labels if label.startswith(sym.SELECT_EMPTY)),
    )


# ── 1. the burst ───────────────────────────────────────────────────────────

@pytest.mark.asyncio
@pytest.mark.parametrize("burst", [10, 15, 30])
async def test_burst_of_ticks_costs_one_view_and_no_rereads(rig, burst):
    """N distinct ticks: every one applied once, one or two edits, no re-reads."""
    codes = await _open_selection(rig)
    reads_after_open = rig.db.reads

    clicks = [
        rig.click(f"action:retrieve_toggle:{codes[index % len(codes)]}")
        for index in range(burst)
    ]
    started = time.monotonic()
    await asyncio.gather(*(rig.router(click) for click in clicks))
    elapsed = time.monotonic() - started

    # Every click answered exactly once and accounted for exactly once.
    assert [click.answer_count for click in clicks] == [1] * burst
    records = [
        record for record in callback_gate.recent_timings()
        if record.data.startswith("action:retrieve_toggle:")
    ]
    assert len(records) == burst
    assert {record.outcome for record in records} == {"completed"}

    # Every click's state transition was applied, in arrival order.
    expected = [codes[index % len(codes)] for index in range(burst)]
    assert [code for code, _selected, _at in rig.applied] == expected

    # The final authoritative selection is the parity of the burst.
    final = set()
    for code in expected:
        final.symmetric_difference_update({code})
    assert set(selection_state.codes(OWNER)) == final

    # Re-rendering the same page after a tick costs NO database read.
    assert rig.db.reads == reads_after_open

    # And the panel was edited a bounded number of times, not once per click.
    edits = sum(len(click.edits) for click in clicks)
    assert 1 <= edits <= 3, f"{edits} edits for {burst} ticks"

    # The last click leaves the newest state on screen: every presented row
    # carries the mark of its final state and the counter agrees.
    painted = _last_paint(clicks)
    last_text = painted.texts[-1]
    marked, empty = _marks(painted)
    assert marked == len(final)
    assert marked + empty == len(codes)
    assert f"{len(final)} selected" in last_text

    # Per-click cost is tiny and the burst does not serialize on the network.
    latencies = [
        (applied_at - click.created_at) * 1000.0
        for (code, _selected, applied_at), click in zip(rig.applied, clicks)
    ]
    latencies.sort()
    p95 = latencies[min(len(latencies) - 1, int(0.95 * len(latencies)))]
    print(
        f"\n[burst-{burst}] total_ms={elapsed * 1000:.1f} "
        f"edits={edits} db_reads={rig.db.reads - reads_after_open} "
        f"state_p50_ms={latencies[len(latencies) // 2]:.2f} p95_ms={p95:.2f} "
        f"ack_max_ms={max(r.ack_ms for r in records):.2f}"
    )
    # A pre-fix burst of 15 cost ~4 round trips per click (3 reads + 1 edit);
    # this bound fails long before that behaviour could return unnoticed.
    assert elapsed < 0.40, f"{burst}-click burst took {elapsed * 1000:.0f} ms"
    assert p95 < 200.0


@pytest.mark.asyncio
async def test_burst_edits_are_fewer_than_clicks_and_coalesced(rig):
    """Views superseded while a paint is in flight are recorded as such."""
    codes = await _open_selection(rig, count=4)
    clicks = [rig.click(f"action:retrieve_toggle:{codes[i % 4]}") for i in range(15)]
    await asyncio.gather(*(rig.router(click) for click in clicks))

    records = [
        record for record in callback_gate.recent_timings()
        if record.data.startswith("action:retrieve_toggle:")
    ]
    assert sum(record.coalesced for record in records) >= 10
    assert sum(record.edit_calls for record in records) <= 3
    # The final state reached the panel, whoever happened to paint it.
    assert _last_paint(clicks).texts[-1]


@pytest.mark.asyncio
async def test_a_single_click_always_renders_its_own_view(rig):
    """With nothing queued behind it, a tick must render (no lost feedback)."""
    codes = await _open_selection(rig, count=3)
    click = rig.click(f"action:retrieve_toggle:{codes[1]}")
    await rig.router(click)

    assert len(click.edits) == 1
    text = click.texts[0]
    assert "1 selected" in text
    record = callback_gate.recent_timings()[-1]
    assert record.edit_calls == 1
    assert record.coalesced == 0
    assert selection_state.codes(OWNER) == (codes[1],)


# ── 2. correctness under repetition and staleness ──────────────────────────

@pytest.mark.asyncio
async def test_duplicate_delivery_applies_the_tick_once(rig):
    codes = await _open_selection(rig, count=3)
    first = _Click(f"action:retrieve_toggle:{codes[0]}", 50001)
    twin = _Click(f"action:retrieve_toggle:{codes[0]}", 50001)

    await rig.router(first)
    await rig.router(twin)

    assert len(rig.applied) == 1
    assert selection_state.codes(OWNER) == (codes[0],)
    assert twin.answer_count == 1  # acknowledged, just not applied twice


@pytest.mark.asyncio
async def test_two_genuine_taps_on_one_row_both_apply(rig):
    codes = await _open_selection(rig, count=3)

    await rig.router(rig.click(f"action:retrieve_toggle:{codes[0]}"))
    await rig.router(rig.click(f"action:retrieve_toggle:{codes[0]}"))

    assert len(rig.applied) == 2
    assert selection_state.codes(OWNER) == ()


@pytest.mark.asyncio
async def test_a_code_outside_the_snapshot_falls_back_to_the_verified_lookup(rig):
    """No snapshot (a hand-built callback): the item is re-verified, not assumed."""
    codes = await _open_selection(rig, count=3)
    db_client._fallback["saved_items"] = [
        row for row in db_client._fallback["saved_items"]
        if row["save_code"] != codes[1]
    ]
    handler._snapshot_invalidate()  # as if the page had never been read
    reads_before = rig.db.reads

    click = rig.click(f"action:retrieve_toggle:{codes[1]}")
    await rig.router(click)

    assert selection_state.codes(OWNER) == ()
    # One verified item lookup, then one fresh page read (the stale snapshot is
    # discarded so the view cannot keep offering a row that is gone).
    assert rig.db.reads == reads_before + 2, "the fallback is the verified lookup"
    assert "no longer in your saved items" in click.texts[-1]
    assert f"action:retrieve_toggle:{codes[1]}" not in click.datas()


@pytest.mark.asyncio
async def test_a_stale_snapshot_is_bounded_by_bulk_time_reverification(rig):
    """A tick may trust the presented page — the bulk action still re-verifies."""
    from backend.services import retrieve_service

    codes = await _open_selection(rig, count=3)
    db_client._fallback["saved_items"] = [
        row for row in db_client._fallback["saved_items"]
        if row["save_code"] != codes[1]
    ]
    reads_before = rig.db.reads

    click = rig.click(f"action:retrieve_toggle:{codes[1]}")
    await rig.router(click)

    assert rig.db.reads == reads_before, "a presented row costs no round trip"
    assert selection_state.codes(OWNER) == (codes[1],)
    # ... and the operation that would touch it refuses, because the service
    # re-reads the row before any side effect.
    assert await retrieve_service.load_saved_item(codes[1], OWNER) is None


@pytest.mark.asyncio
async def test_another_owners_snapshot_can_never_be_toggled(rig):
    """The page snapshot is owner-scoped: a foreign tick is verified, and fails."""
    codes = await _open_selection(rig, count=3)
    inline_engine.set_owner_id(OTHER_OWNER)

    click = rig.click(f"action:retrieve_toggle:{codes[0]}")
    await rig.router(click)

    assert rig.applied == []
    assert selection_state.codes(OTHER_OWNER) == ()
    assert selection_state.codes(OWNER) == ()
    assert "no longer in your saved items" in click.texts[-1]


@pytest.mark.asyncio
async def test_a_tick_never_opens_or_re_resolves_an_item_panel(rig):
    codes = await _open_selection(rig, count=3)
    click = rig.click(f"action:retrieve_toggle:{codes[2]}")
    await rig.router(click)

    assert f"panel:retrieve_item:id:{codes[2]}" not in click.datas()
    assert get_lifecycle().sessions.current_nav(CHAT, MSG)[0] != "retrieve_item"


# ── 3. ordering that must survive the optimisation ─────────────────────────

@pytest.mark.asyncio
async def test_selection_and_review_agree_after_a_burst(rig):
    """The review panel lists exactly what the burst left selected."""
    codes = await _open_selection(rig, count=4)
    burst = [codes[0], codes[1], codes[2], codes[1]]  # S0002 toggled twice
    clicks = [rig.click(f"action:retrieve_toggle:{code}") for code in burst]
    await asyncio.gather(*(rig.router(click) for click in clicks))

    review = rig.click("panel:retrieve_saved:bulk:send")
    await rig.router(review)

    text = review.texts[-1]
    assert "2 selected items" in text
    assert codes[0] in text and codes[2] in text
    assert codes[1] not in text
    assert set(selection_state.codes(OWNER)) == {codes[0], codes[2]}


@pytest.mark.asyncio
async def test_a_failed_paint_does_not_wedge_the_panel(rig):
    """A raising edit is recorded and the next click still paints."""
    codes = await _open_selection(rig, count=3)
    calls = {"n": 0}
    real_edit = _Click.edit

    async def flaky_edit(self, text, buttons=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise RuntimeError("EDIT_FAILED")
        await real_edit(self, text, buttons)

    _Click.edit = flaky_edit
    try:
        first = rig.click(f"action:retrieve_toggle:{codes[0]}")
        await rig.router(first)
        assert not first.edits
        assert callback_gate.painter(CHAT, MSG).in_flight is False

        second = rig.click(f"action:retrieve_toggle:{codes[1]}")
        await asyncio.wait_for(rig.router(second), 2.0)
        assert second.edits
    finally:
        _Click.edit = real_edit
    assert set(selection_state.codes(OWNER)) == {codes[0], codes[1]}


@pytest.mark.asyncio
async def test_a_slow_paint_does_not_hold_the_next_click(rig):
    """The turn ends with the handler, so queued handlers run during an edit."""
    codes = await _open_selection(rig, count=3)
    first = rig.click(f"action:retrieve_toggle:{codes[0]}", edit_delay=0.30)
    second = rig.click(f"action:retrieve_toggle:{codes[1]}")

    started = time.monotonic()
    await asyncio.gather(rig.router(first), rig.router(second))
    elapsed = time.monotonic() - started

    # Both state changes are applied without waiting for the first edit...
    assert set(selection_state.codes(OWNER)) == {codes[0], codes[1]}
    second_record = [
        r for r in callback_gate.recent_timings()
        if r.data == f"action:retrieve_toggle:{codes[1]}"
    ][0]
    assert second_record.work_ms < 100.0, "the handler waited for a paint"
    # ...and the exchange costs two 300 ms paints (the first view and the
    # newest one), not one paint per click.
    assert elapsed < 0.75
    assert sum(len(click.edits) for click in (first, second)) == 2


@pytest.mark.asyncio
async def test_a_pending_view_is_dropped_when_the_panel_closes(rig):
    """Closing a panel must not leave a paint that edits the closed message."""
    painter = callback_gate.painter(CHAT, MSG)
    assert painter.publish(("old view", [])) is True
    painter.in_flight = True  # pretend another click is painting it

    callback_gate.forget_painter(CHAT, MSG)
    assert painter.closed is True
    assert painter.pending is None
    # The same click publishing again lands on a FRESH writer, not the dead one.
    assert callback_gate.painter(CHAT, MSG) is not painter


@pytest.mark.asyncio
async def test_a_saturated_database_pool_stays_bounded_and_spares_the_panel(rig):
    """Four slow Supabase workers must not freeze the process or a paint."""
    import threading

    from backend.db.client import run_sync_db
    from backend.runtime.operation_watchdog import OperationTimeoutError

    release = threading.Event()

    def _blocking() -> str:
        release.wait(2.0)
        return "late"

    # Fill the whole bounded pool with calls that cannot finish yet.
    saturating = [
        asyncio.create_task(run_sync_db(_blocking, timeout=2.0)) for _ in range(4)
    ]
    await asyncio.sleep(0.02)  # let them reach the workers

    # One more call is bounded by the watchdog instead of hanging forever.
    with pytest.raises(OperationTimeoutError):
        await run_sync_db(_blocking, timeout=0.05)

    # And a click whose handler touches no database still answers and paints.
    panels.register_panel("_free_panel", lambda event, extra: _free_view(extra))
    get_lifecycle().sessions.create(CHAT, MSG, "_free_panel")
    callback_gate.reset()
    click = rig.click("panel:_free_panel:live")
    await asyncio.wait_for(rig.router(click), 1.0)
    assert click.answer_count == 1
    assert any("free:live" in text for text in click.texts)

    release.set()
    await asyncio.wait_for(asyncio.gather(*saturating, return_exceptions=True), 3.0)


async def _free_view(extra: str):
    return "Free Panel", f"free:{extra}", []


@pytest.mark.asyncio
async def test_a_slow_close_does_not_block_another_panel(rig):
    """The lifecycle lock must not be held across the closing edit."""
    slow_edit_started = asyncio.Event()
    release = asyncio.Event()

    class _SlowEvent:
        async def edit(self, text, buttons=None):
            slow_edit_started.set()
            await release.wait()

    close_task = asyncio.create_task(
        get_lifecycle().close_panel(CHAT, 9999, event=_SlowEvent())
    )
    await asyncio.wait_for(slow_edit_started.wait(), 2.0)

    # While the closing edit is in flight, another panel can still be created.
    async def _open() -> bool:
        ok, chat, msg = await get_lifecycle().create_panel(CHAT, "retrieve")
        return ok

    from backend.helper import inline_engine

    async def _fake_trigger(self_client, chat_id, query):
        return True, chat_id, 9001, ""

    original = inline_engine.trigger
    inline_engine.trigger = _fake_trigger
    try:
        opened = await asyncio.wait_for(_open(), 0.5)
    finally:
        inline_engine.trigger = original
        release.set()
        await asyncio.wait_for(close_task, 2.0)
    assert opened is True


@pytest.mark.asyncio
async def test_a_failing_tick_does_not_block_the_next_click(rig):
    codes = await _open_selection(rig, count=3)
    real_query = db_client.query_save
    rig.db.delay = 0.0

    async def failing_query(*args, **kwargs):
        raise RuntimeError("database exploded")

    db_client.query_save = failing_query
    try:
        bad = rig.click("action:retrieve_toggle:S0ZZZ")  # not presented → lookup
        await rig.router(bad)
    finally:
        db_client.query_save = real_query

    assert "Could not verify" in bad.texts[-1]
    after = rig.click(f"action:retrieve_toggle:{codes[0]}")
    await asyncio.wait_for(rig.router(after), timeout=2.0)
    assert selection_state.codes(OWNER) == (codes[0],)


# ── 4. gating helpers ─────────────────────────────────────────────────────

@pytest.mark.asyncio
async def test_the_painter_keeps_only_the_newest_view_and_never_overlaps(rig):
    """Three views published during one paint cost two edits, newest last."""
    painter = callback_gate.painter(CHAT, MSG)
    painted: list[str] = []
    started = asyncio.Event()

    async def _paint(view) -> None:
        painted.append(view)
        started.set()
        await asyncio.sleep(0.05)

    assert painter.publish("v1") is True
    task = asyncio.create_task(painter.drain(_paint))
    await asyncio.wait_for(started.wait(), 1.0)

    assert painter.publish("v2") is False   # superseded, no second painter
    assert painter.publish("v3") is False   # v2 is dropped, never painted
    await asyncio.wait_for(task, 2.0)

    assert painted == ["v1", "v3"]
    assert painter.in_flight is False
    assert painter.pending is None


def test_turn_deadline_scales_with_backlog_and_stays_bounded():
    assert callback_gate.turn_deadline(20.0, 0) == 20.0
    assert callback_gate.turn_deadline(20.0, 5) > 20.0
    assert callback_gate.turn_deadline(20.0, 10_000) == callback_gate.MAX_TURN_WAIT_S


# ── 5. the production instrumentation reports the real cost ────────────────

@pytest.mark.asyncio
async def test_timing_record_reports_edits_and_database_round_trips(rig, monkeypatch):
    """With the real DB dispatcher, a click's record states what it actually cost."""
    monkeypatch.undo()  # a real list_saves: in-memory fallback, bounded dispatcher
    codes = await _open_selection(rig, count=3)
    click = rig.click(f"action:retrieve_toggle:{codes[0]}")
    await rig.router(click)

    record = callback_gate.recent_timings()[-1]
    assert record.data == f"action:retrieve_toggle:{codes[0]}"
    assert record.edit_calls == 1
    assert record.edit_ms >= EDIT_DELAY * 1000.0 * 0.5
    assert record.db_calls == 0, "a tick on a presented item must not read the database"
    assert record.state_ms >= 0.0
