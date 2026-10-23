"""Callback backlog — acknowledgement, ordering and accounting regression tests.

The router under test is the REAL one, captured through the same stubbed
``client.on`` convention the other helper tests use. Telethon's dispatcher is
simulated the way the pinned client actually runs with ``sequential_updates``
left at its default: every click gets its own task, so a slow handler never
delays the delivery of the next click. What a click must not wait for is its own
acknowledgement, and what two clicks on one panel must never do is run at the
same time.

No live Telegram, no Supabase, no network: every RPC is a fake whose latency the
test chooses, so the numbers below are the click path's own overhead plus the
deliberate delays — never a guess about a real network.
"""
from __future__ import annotations

import asyncio
import time
from types import SimpleNamespace

import pytest
from telethon import events

from backend.helper import callback_gate, panels
from backend.helper.lifecycle import get_lifecycle
from backend.helper.panel_registry import registry as panel_registry

OWNER = 7770001
CHAT = -1001234567890
MSG = 4242
OTHER_MSG = 4343


class _Click:
    """One CallbackQuery event: records when Telegram is answered."""

    def __init__(
        self,
        data: str,
        query_id: int,
        *,
        chat_id: int = CHAT,
        msg_id: int = MSG,
        answer_delay: float = 0.0,
        answer_error: Exception | None = None,
    ) -> None:
        self.data = data.encode("utf-8")
        self.sender_id = OWNER
        self.chat_id = chat_id
        self.message_id = msg_id
        self.inline_message_id = None
        self.query = SimpleNamespace(query_id=query_id, msg_id=msg_id)
        self.answer_delay = answer_delay
        self.answer_error = answer_error
        self.created_at = time.monotonic()
        self.answered_at: float | None = None
        self.answer_count = 0
        self.edits: list[str] = []
        self.edit_times: list[float] = []

    @property
    def ack_latency(self) -> float:
        assert self.answered_at is not None
        return self.answered_at - self.created_at

    async def answer(self, *args, **kwargs) -> None:
        self.answer_count += 1
        if self.answer_error is not None:
            raise self.answer_error
        if self.answer_delay:
            await asyncio.sleep(self.answer_delay)
        self.answered_at = time.monotonic()

    async def edit(self, text, buttons=None) -> None:
        self.edits.append(text)
        self.edit_times.append(time.monotonic())


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


@pytest.fixture
def router():
    callback_gate.reset()
    get_lifecycle().sessions.clear_all()
    get_lifecycle().timers.stop_all()
    router = _capture_router()
    yield router
    callback_gate.reset()
    get_lifecycle().sessions.clear_all()
    get_lifecycle().timers.stop_all()


@pytest.fixture
def panel(router):
    """Register the panel the clicks drive; returns its recorded calls."""
    calls: list[str] = []
    hooks: dict = {"on_call": None}

    async def _handler(event, extra: str):
        calls.append(extra)
        hook = hooks["on_call"]
        if hook is not None:
            await hook(extra)
        return "Backlog Panel", f"view:{extra}", []

    panels.register_panel("_backlog_panel", _handler)
    session = get_lifecycle().sessions.create(CHAT, MSG, "_backlog_panel")
    assert session
    return SimpleNamespace(calls=calls, hooks=hooks, handler=_handler)


def _open_panel(chat_id: int = CHAT, msg_id: int = MSG) -> None:
    get_lifecycle().sessions.create(chat_id, msg_id, "_backlog_panel")


def _timing_for(data: str):
    matches = [t for t in callback_gate.recent_timings() if t.data == data]
    assert matches, f"no timing record for {data!r}"
    return matches[-1]


@pytest.mark.asyncio
async def test_burst_of_fifteen_clicks_is_acknowledged_and_accounted_for(router, panel):
    """15 clicks arriving together: every one answered at once, run once."""

    async def _slow(extra: str):
        await asyncio.sleep(0.05)

    panel.hooks["on_call"] = _slow
    clicks = [
        _Click(f"panel:_backlog_panel:{i}", query_id=1000 + i) for i in range(15)
    ]

    started = time.monotonic()
    await asyncio.gather(*(router(click) for click in clicks))
    elapsed = time.monotonic() - started

    assert [c.answer_count for c in clicks] == [1] * 15
    assert panel.calls == [str(i) for i in range(15)]
    # The acknowledgement never waits for the 50 ms handler, let alone for the
    # 14 clicks queued behind it.
    assert max(click.ack_latency for click in clicks) < 0.05
    assert all(click.edits for click in clicks)
    records = [t for t in callback_gate.recent_timings() if t.data.startswith("panel:_backlog_panel:")]
    assert len(records) == 15
    assert {t.outcome for t in records} == {"completed"}
    assert elapsed < 5.0
    print(
        f"\n[burst-15] ack_max_ms={max(c.ack_latency for c in clicks) * 1000:.2f} "
        f"total_ms={elapsed * 1000:.1f}"
    )


@pytest.mark.asyncio
async def test_ack_latency_excludes_slow_database_and_telegram_work(router, panel):
    """The headline measurement: ack is one answer RPC, not the click's work."""

    async def _slow(extra: str):
        await asyncio.sleep(0.20)

    panel.hooks["on_call"] = _slow
    click = _Click("panel:_backlog_panel:slow", query_id=2001)

    await router(click)

    assert click.answer_count == 1
    assert click.ack_latency < 0.05
    record = _timing_for("panel:_backlog_panel:slow")
    assert record.outcome == "completed"
    assert record.ack_ms < 50.0
    assert record.work_ms >= 200.0
    assert record.total_ms >= record.work_ms
    print(
        f"\n[slow-work] ack_ms={record.ack_ms:.2f} work_ms={record.work_ms:.1f} "
        f"total_ms={record.total_ms:.1f}"
    )


@pytest.mark.asyncio
async def test_same_panel_clicks_run_in_arrival_order(router, panel):
    """Two concurrent clicks must not both read the same pre-click state."""

    order: list[str] = []
    in_flight = 0
    peak = 0

    async def _tracked(extra: str):
        nonlocal in_flight, peak
        in_flight += 1
        peak = max(peak, in_flight)
        await asyncio.sleep(0.02 if extra == "1" else 0.001)
        order.append(extra)
        in_flight -= 1

    panel.hooks["on_call"] = _tracked
    clicks = [_Click(f"panel:_backlog_panel:{i}", query_id=3000 + i) for i in (1, 2, 3)]

    await asyncio.gather(*(router(click) for click in clicks))

    assert order == ["1", "2", "3"]
    assert peak == 1
    assert all(click.edits for click in clicks)
    # The LAST handler's view is the one left on the panel.
    assert "view:3" in clicks[-1].edits[-1]


@pytest.mark.asyncio
async def test_independent_panels_progress_while_one_is_blocked(router, panel):
    """A stuck panel must not stop a different panel's click."""
    _open_panel(msg_id=OTHER_MSG)
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _blocking(extra: str):
        if extra == "blocked":
            entered.set()
            await release.wait()

    panel.hooks["on_call"] = _blocking
    blocked = asyncio.create_task(router(_Click("panel:_backlog_panel:blocked", query_id=4001)))
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    other = _Click("panel:_backlog_panel:free", query_id=4002, msg_id=OTHER_MSG)
    await asyncio.wait_for(router(other), timeout=1.0)
    assert other.edits, "the unrelated panel never rendered while the other was blocked"

    release.set()
    await asyncio.wait_for(blocked, timeout=2.0)


@pytest.mark.asyncio
async def test_duplicate_delivery_of_one_query_runs_the_handler_once(router, panel):
    """The same Telegram query delivered twice is one click."""
    click = _Click("panel:_backlog_panel:dup", query_id=5001)
    same_query = _Click("panel:_backlog_panel:dup", query_id=5001)

    await router(click)
    await router(same_query)

    assert panel.calls == ["dup"]
    assert same_query.answer_count == 1  # still acknowledged, just not executed
    assert not same_query.edits


@pytest.mark.asyncio
async def test_two_distinct_clicks_on_the_same_button_both_run(router, panel):
    """A genuine double tap is two queries and must not be suppressed."""
    await router(_Click("panel:_backlog_panel:twice", query_id=6001))
    await router(_Click("panel:_backlog_panel:twice", query_id=6002))

    assert panel.calls == ["twice", "twice"]


@pytest.mark.asyncio
async def test_click_queued_for_a_panel_closed_meanwhile_is_dropped(router, panel):
    """A queued click must not edit a panel the earlier click closed."""
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _blocking(extra: str):
        entered.set()
        await release.wait()

    panel.hooks["on_call"] = _blocking
    first = asyncio.create_task(router(_Click("panel:_backlog_panel:first", query_id=7001)))
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    second = _Click("panel:_backlog_panel:second", query_id=7002)
    second_task = asyncio.create_task(router(second))
    await asyncio.sleep(0.01)  # let it queue on the panel turn

    get_lifecycle().sessions.destroy(CHAT, MSG)
    release.set()
    await asyncio.wait_for(first, timeout=2.0)
    await asyncio.wait_for(second_task, timeout=2.0)

    assert second.answer_count == 1
    assert not second.edits
    assert _timing_for("panel:_backlog_panel:second").outcome == "dropped:panel-closed"


@pytest.mark.asyncio
async def test_failing_handler_does_not_block_the_next_click(router, panel):
    async def _boom(extra: str):
        if extra == "boom":
            raise RuntimeError("handler exploded")

    panel.hooks["on_call"] = _boom

    await router(_Click("panel:_backlog_panel:boom", query_id=8001))
    after = _Click("panel:_backlog_panel:after", query_id=8002)
    await asyncio.wait_for(router(after), timeout=2.0)

    assert after.edits
    assert panel.calls == ["boom", "after"]


@pytest.mark.asyncio
async def test_cancelled_handler_releases_the_panel_turn(router, panel):
    entered = asyncio.Event()

    async def _hang(extra: str):
        entered.set()
        await asyncio.Event().wait()

    panel.hooks["on_call"] = _hang
    task = asyncio.create_task(router(_Click("panel:_backlog_panel:hang", query_id=9001)))
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    panel.hooks["on_call"] = None
    after = _Click("panel:_backlog_panel:after-cancel", query_id=9002)
    await asyncio.wait_for(router(after), timeout=2.0)
    assert after.edits


@pytest.mark.asyncio
async def test_refused_ack_is_recorded_and_the_click_still_runs(router, panel):
    click = _Click(
        "panel:_backlog_panel:noack",
        query_id=9101,
        answer_error=RuntimeError("QUERY_ID_INVALID"),
    )

    await router(click)

    assert panel.calls == ["noack"]
    assert click.edits
    record = _timing_for("panel:_backlog_panel:noack")
    assert "QUERY_ID_INVALID" in record.ack_error
    assert record.outcome == "completed"


@pytest.mark.asyncio
async def test_a_click_that_cannot_get_its_turn_is_dropped_with_a_reason(router, panel, monkeypatch):
    entered = asyncio.Event()
    release = asyncio.Event()

    async def _blocking(extra: str):
        entered.set()
        await release.wait()

    panel.hooks["on_call"] = _blocking
    monkeypatch.setattr(callback_gate, "DEFAULT_TURN_WAIT_S", 0.05)

    first = asyncio.create_task(router(_Click("panel:_backlog_panel:hold", query_id=9201)))
    await asyncio.wait_for(entered.wait(), timeout=2.0)

    late = _Click("panel:_backlog_panel:late", query_id=9202)
    await asyncio.wait_for(router(late), timeout=2.0)

    assert late.answer_count == 1  # the click is still acknowledged
    assert not late.edits
    assert _timing_for("panel:_backlog_panel:late").outcome == "dropped:panel-busy"

    release.set()
    await asyncio.wait_for(first, timeout=2.0)


@pytest.mark.asyncio
async def test_action_dispatch_still_renders_and_receives_its_extra(router, panel):
    seen: list[str] = []

    async def _action(event, extra: str, chat_id: int):
        seen.append(extra)
        return "Action Done", f"acted:{extra}", []

    panels.register_action("_backlog_action", _action)
    click = _Click("action:_backlog_action:payload", query_id=9301)

    await router(click)

    assert seen == ["payload"]
    assert click.edits and "acted:payload" in click.edits[-1]
    assert _timing_for("action:_backlog_action:payload").outcome == "completed"


@pytest.mark.asyncio
async def test_authored_navigation_noop_is_acked_and_renders_nothing(router, panel):
    click = _Click("panel:_nav:noop", query_id=9401)

    await router(click)

    assert click.answer_count == 1
    assert not click.edits
    assert _timing_for("panel:_nav:noop").outcome == "completed"


@pytest.mark.asyncio
async def test_home_navigation_still_renders_the_menu(router, panel):
    from backend.helper.panel_registry import get_panel_def

    previous = get_panel_def("menu")

    async def _menu(event, extra: str):
        return "LifeOS", "Choose a category:", []

    panels.register_panel("menu", _menu)
    try:
        click = _Click("panel:_nav:home", query_id=9501)
        await router(click)
        assert click.edits and "Choose a category" in click.edits[-1]
        assert get_lifecycle().sessions.nav_depth(CHAT, MSG) == 1
    finally:
        if previous is not None:
            panel_registry()._panels["menu"] = previous
        else:
            panel_registry()._panels.pop("menu", None)


@pytest.mark.asyncio
async def test_unauthorized_and_unroutable_clicks_are_acked_and_recorded(router, panel):
    stranger = _Click("panel:_backlog_panel:x", query_id=9601)
    stranger.sender_id = OWNER + 1
    await router(stranger)
    assert stranger.answer_count == 1
    assert not stranger.edits

    unknown = _Click("panel:_backlog_missing", query_id=9602)
    await router(unknown)
    assert unknown.answer_count == 1
    assert not unknown.edits


@pytest.mark.asyncio
async def test_timings_are_recorded_per_stage_in_order(router, panel):
    async def _slow(extra: str):
        await asyncio.sleep(0.03)

    panel.hooks["on_call"] = _slow
    await router(_Click("panel:_backlog_panel:t", query_id=9701))

    record = _timing_for("panel:_backlog_panel:t")
    assert record.received_at <= record.acked_at <= record.dispatch_at
    assert record.dispatch_at <= record.rendered_at <= record.finished_at
    assert record.ack_ms < 50.0
    assert record.work_ms >= 30.0
    assert record.total_ms >= record.work_ms
    assert len(callback_gate.recent_timings()) <= callback_gate._RECENT_TIMING_LIMIT
