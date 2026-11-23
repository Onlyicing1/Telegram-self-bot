"""Outbound bandwidth governor — fail-closed accounting, limits and concurrency.

These tests replace the suite-wide persistence double (``tests/conftest.py``)
with explicit fakes so the REAL contract is exercised:

  * no database / unreadable row / refused or lost write  -> DENIED
  * malformed persisted state                             -> DENIED (never zero)
  * daily or monthly limit exceeded                       -> DENIED before write
  * exact remaining capacity                              -> accepted, confirmed
  * concurrent reservations                               -> serialized, no overspend
  * a usage read                                          -> unavailable != zero
"""
from __future__ import annotations

import asyncio
import copy
from datetime import datetime, timezone

import pytest

from backend.services import outbound_bandwidth_service as bw

UTC = timezone.utc
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
DAY = "2026-11-23"
MONTH = "2026-11"


def _state(daily=0, monthly=0, day=DAY, month=MONTH):
    return {
        "version": 1,
        "daily": {"period": day, "bytes": daily},
        "monthly": {"period": month, "bytes": monthly},
    }


class FakeRepo:
    """The persistence boundary: one JSONB column on the singleton row."""

    def __init__(self, state=None):
        self._state = copy.deepcopy(state if state is not None else {})
        self.load_result = "state"
        self.load_error = None
        self.write_refused = False
        self.write_error = None
        self.drop_writes = False
        self.loads = 0
        self.writes = []

    @property
    def state(self):
        return copy.deepcopy(self._state)

    def load(self):
        self.loads += 1
        if self.load_error is not None:
            raise self.load_error
        if self.load_result != "state":
            return self.load_result
        return {"outbound_bandwidth_state": copy.deepcopy(self._state)}

    def update_field(self, field, value):
        assert field == "outbound_bandwidth_state"
        self.writes.append(copy.deepcopy(value))
        if self.write_error is not None:
            raise self.write_error
        if self.write_refused:
            return False
        if not self.drop_writes:
            self._state = copy.deepcopy(value)
        return True


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    monkeypatch.setattr(bw, "repo", fake)
    monkeypatch.setattr(bw, "_db_configured", lambda: True)
    return fake


def test_the_operational_limits_are_unchanged():
    assert bw.DAILY_LIMIT_BYTES == 150_000_000
    assert bw.MONTHLY_LIMIT_BYTES == 4_000_000_000


# ── persistence failures deny the reservation ──────────────────────────────


@pytest.mark.asyncio
async def test_no_database_denies_the_reservation(monkeypatch, repo):
    monkeypatch.setattr(bw, "_db_configured", lambda: False)

    with pytest.raises(bw.OutboundBandwidthUnavailableError):
        await bw.reserve_outbound_bytes("test", 10, now=NOW)

    assert repo.writes == []


@pytest.mark.asyncio
async def test_a_missing_row_denies_the_reservation(repo):
    repo.load_result = None

    with pytest.raises(bw.OutboundBandwidthUnavailableError):
        await bw.reserve_outbound_bytes("test", 10, now=NOW)

    assert repo.writes == []


@pytest.mark.asyncio
async def test_a_repository_read_failure_denies_the_reservation(repo):
    repo.load_error = RuntimeError("supabase read failed")

    with pytest.raises(bw.OutboundBandwidthUnavailableError):
        await bw.reserve_outbound_bytes("test", 10, now=NOW)

    assert repo.writes == []


@pytest.mark.asyncio
async def test_a_refused_write_denies_the_reservation(repo):
    repo.write_refused = True

    with pytest.raises(bw.OutboundBandwidthUnavailableError):
        await bw.reserve_outbound_bytes("test", 10, now=NOW)

    assert len(repo.writes) == 1


@pytest.mark.asyncio
async def test_a_write_exception_denies_the_reservation(repo):
    repo.write_error = RuntimeError("supabase write failed")

    with pytest.raises(bw.OutboundBandwidthUnavailableError):
        await bw.reserve_outbound_bytes("test", 10, now=NOW)

    assert repo.state == {}


@pytest.mark.asyncio
async def test_a_write_that_never_lands_is_denied(repo):
    repo.drop_writes = True

    with pytest.raises(bw.OutboundBandwidthUnavailableError):
        await bw.reserve_outbound_bytes("test", 10, now=NOW)

    # Bounded retries, then denial — never a fabricated success.
    assert len(repo.writes) == bw._MAX_CONFIRM_ATTEMPTS


# ── limits ─────────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_the_daily_limit_is_rejected_before_any_write(repo):
    repo._state = _state(daily=bw.DAILY_LIMIT_BYTES - 9)

    with pytest.raises(bw.OutboundBandwidthExceededError) as excinfo:
        await bw.reserve_outbound_bytes("test", 10, now=NOW)

    assert excinfo.value.period == "daily"
    assert repo.writes == []


@pytest.mark.asyncio
async def test_the_monthly_limit_is_rejected_before_any_write(repo):
    repo._state = _state(daily=0, monthly=bw.MONTHLY_LIMIT_BYTES - 9)

    with pytest.raises(bw.OutboundBandwidthExceededError) as excinfo:
        await bw.reserve_outbound_bytes("test", 10, now=NOW)

    assert excinfo.value.period == "monthly"
    assert repo.writes == []


@pytest.mark.asyncio
async def test_exact_remaining_capacity_is_accepted(repo):
    repo._state = _state(daily=bw.DAILY_LIMIT_BYTES - 1000, monthly=1000)

    result = await bw.reserve_outbound_bytes("test", 1000, now=NOW)

    assert result["daily_used_bytes"] == bw.DAILY_LIMIT_BYTES
    assert repo.state["daily"]["bytes"] == bw.DAILY_LIMIT_BYTES
    assert repo.state["monthly"]["bytes"] == 2000


@pytest.mark.asyncio
async def test_a_previous_period_is_a_real_rollover_not_malformed_state(repo):
    repo._state = _state(daily=bw.DAILY_LIMIT_BYTES, day="2026-11-22", monthly=7)

    result = await bw.reserve_outbound_bytes("test", 10, now=NOW)

    assert result["daily_used_bytes"] == 10
    assert result["monthly_used_bytes"] == 17


# ── malformed state is never zero usage ────────────────────────────────────


@pytest.mark.parametrize(
    "raw",
    [
        "not-a-state",
        42,
        [],
        {"version": 1},
        {"version": 99, "daily": {"period": DAY, "bytes": 5},
         "monthly": {"period": MONTH, "bytes": 5}},
        {"version": 1, "daily": {"period": DAY, "bytes": 5}},
        {"version": 1, "daily": {"period": DAY, "bytes": "5"},
         "monthly": {"period": MONTH, "bytes": 0}},
        {"version": 1, "daily": {"period": DAY, "bytes": -1},
         "monthly": {"period": MONTH, "bytes": 0}},
        {"version": 1, "daily": {"period": "", "bytes": 5},
         "monthly": {"period": MONTH, "bytes": 0}},
        {"version": 1, "daily": None, "monthly": {"period": MONTH, "bytes": 0}},
        {"version": 1, "daily": {"period": DAY, "bytes": True},
         "monthly": {"period": MONTH, "bytes": 0}},
    ],
)
@pytest.mark.asyncio
async def test_malformed_state_is_denied_never_read_as_zero(repo, raw):
    repo._state = raw

    with pytest.raises(bw.OutboundBandwidthUnavailableError):
        await bw.reserve_outbound_bytes("test", 10, now=NOW)

    assert repo.writes == []


@pytest.mark.asyncio
async def test_a_fresh_empty_state_is_a_real_zero(repo):
    assert repo.state == {}

    result = await bw.reserve_outbound_bytes("test", 5, now=NOW)

    assert result["daily_used_bytes"] == 5
    assert repo.state["daily"] == {"period": DAY, "bytes": 5}


# ── unknown payload sizes are never zero ───────────────────────────────────


@pytest.mark.parametrize("value", [0, -1, None, "10", True, 1.5, 0.0])
@pytest.mark.asyncio
async def test_an_unknown_or_unmeasurable_size_is_rejected(repo, value):
    with pytest.raises(ValueError):
        await bw.reserve_outbound_bytes("test", value, now=NOW)

    assert repo.writes == []


# ── concurrency inside the supported single runtime ────────────────────────


@pytest.mark.asyncio
async def test_concurrent_reservations_cannot_overspend(monkeypatch, repo):
    monkeypatch.setattr(bw, "DAILY_LIMIT_BYTES", 1000)
    monkeypatch.setattr(bw, "MONTHLY_LIMIT_BYTES", 10_000)

    results = await asyncio.gather(
        *[bw.reserve_outbound_bytes("test", 100, now=NOW) for _ in range(12)],
        return_exceptions=True,
    )

    accepted = [item for item in results if isinstance(item, dict)]
    rejected = [
        item for item in results if isinstance(item, bw.OutboundBandwidthExceededError)
    ]
    assert len(accepted) == 10
    assert len(rejected) == 2
    assert repo.state["daily"]["bytes"] == 1000
    assert repo.state["monthly"]["bytes"] == 1000


# ── usage reads distinguish unavailable from zero ──────────────────────────


@pytest.mark.asyncio
async def test_usage_is_unavailable_without_database(monkeypatch, repo):
    monkeypatch.setattr(bw, "_db_configured", lambda: False)

    usage = await bw.get_outbound_usage(now=NOW)

    assert usage["available"] is False
    assert usage["state"] == "unavailable"
    assert "not configured" in usage["detail"]


@pytest.mark.asyncio
async def test_usage_reports_a_real_zero_when_the_state_is_valid(repo):
    repo._state = _state()

    usage = await bw.get_outbound_usage(now=NOW)

    assert usage["available"] is True
    assert usage["daily_used_bytes"] == 0
    assert usage["monthly_used_bytes"] == 0
    assert usage["state"] == "normal"


@pytest.mark.asyncio
async def test_usage_is_unavailable_when_the_state_is_malformed(repo):
    repo._state = {"version": 1}

    usage = await bw.get_outbound_usage(now=NOW)

    assert usage["available"] is False
    assert usage["state"] == "unavailable"
    assert usage["detail"]


@pytest.mark.asyncio
async def test_usage_is_unavailable_when_the_row_is_missing(repo):
    repo.load_result = None

    usage = await bw.get_outbound_usage(now=NOW)

    assert usage["available"] is False
    assert "unavailable" in usage["detail"].lower()
