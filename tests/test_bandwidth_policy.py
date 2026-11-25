"""Cumulative staged bandwidth policy + durable monthly floor.

The intended production ladder is 50/60/70/80/90/100% of the 4 GB monthly
budget.  These tests prove it is cumulative (a higher tier never re-enables a
lower tier's restriction), that a partial/stale Render metric can only RAISE
usage, that the durable floor survives a restart, and that scheduled work cannot
be classified as interactive.
"""
from __future__ import annotations

import copy
from datetime import datetime, timezone

import pytest

from backend.services import outbound_bandwidth_service as bw

UTC = timezone.utc
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)
DAY = "2026-11-23"
MONTH = "2026-11"


def _state(monthly=0, daily=0, month=MONTH, day=DAY, floor=None):
    state = {
        "version": 1,
        "daily": {"period": day, "bytes": daily},
        "monthly": {"period": month, "bytes": monthly},
    }
    if floor is not None:
        state["policy"] = {"month": month, "floor_bytes": floor}
    return state


class FakeRepo:
    def __init__(self, state=None):
        self._state = copy.deepcopy(state if state is not None else {})
        self.load_error = None

    @property
    def state(self):
        return copy.deepcopy(self._state)

    def load(self):
        if self.load_error is not None:
            raise self.load_error
        return {"outbound_bandwidth_state": copy.deepcopy(self._state)}

    def update_field(self, field, value):
        assert field == "outbound_bandwidth_state"
        self._state = copy.deepcopy(value)
        return True


@pytest.fixture
def repo(monkeypatch):
    fake = FakeRepo()
    monkeypatch.setattr(bw, "repo", fake)
    monkeypatch.setattr(bw, "_db_configured", lambda: True)
    monkeypatch.setattr(bw, "_render_metric", {
        "month": "", "bytes": None, "trusted": False,
        "coverage": "unknown", "latest_data_at": None, "updated_monotonic": 0.0,
    })
    monkeypatch.setattr(bw, "_denials", {action: 0 for action in bw.ACTION_MIN_TIER})
    monkeypatch.setattr(bw, "_denials_month", "")
    return fake


# ── 1. tier boundaries ──


@pytest.mark.parametrize(
    ("used", "tier"),
    [
        (0, 0),
        (1_999_999_999, 0),
        (2_000_000_000, 1),
        (2_399_999_999, 1),
        (2_400_000_000, 2),
        (2_800_000_000, 3),
        (3_200_000_000, 4),
        (3_600_000_000, 5),
        (4_000_000_000, 6),
    ],
)
def test_policy_tier_thresholds(used, tier):
    assert bw.policy_tier(used) == tier


# ── 2. cumulative action restrictions ──


@pytest.mark.parametrize(
    ("used", "denied_action", "allowed_action"),
    [
        (2_000_000_000, "scheduled_media", "interactive_media"),
        (2_400_000_000, "album_media", "interactive_media"),
        (2_800_000_000, "ai_media", "interactive_media"),
        (3_200_000_000, "reupload", "interactive_media"),
        (3_200_000_000, "storage_upload", "interactive_media"),
        (3_200_000_000, "media_transfer", "interactive_media"),
        (3_600_000_000, "interactive_media", None),
        (3_600_000_000, "voice", None),
    ],
)
@pytest.mark.asyncio
async def test_each_tier_denies_its_actions(repo, used, denied_action, allowed_action):
    repo._state = _state(monthly=used)
    with pytest.raises(bw.OutboundBandwidthPolicyError):
        await bw.reserve_outbound_bytes("telegram_media", 1, action=denied_action, now=NOW)
    if allowed_action is not None:
        result = await bw.reserve_outbound_bytes(
            "telegram_media", 1, action=allowed_action, now=NOW
        )
        assert result["reserved_bytes"] == 1


@pytest.mark.asyncio
async def test_higher_tier_keeps_every_lower_restriction(repo):
    repo._state = _state(monthly=3_600_000_000)  # 90%
    for action in (
        "scheduled_media",
        "album_media",
        "ai_media",
        "reupload",
        "storage_upload",
        "media_transfer",
        "interactive_media",
        "voice",
    ):
        with pytest.raises(bw.OutboundBandwidthPolicyError):
            await bw.reserve_outbound_bytes(
                "telegram_media", 1, action=action, now=NOW
            )


@pytest.mark.asyncio
async def test_below_the_first_tier_media_is_allowed(repo):
    repo._state = _state(monthly=1_999_999_999)
    result = await bw.reserve_outbound_bytes(
        "telegram_media", 1, action="scheduled_media", now=NOW
    )
    assert result["policy_tier"] == 0


@pytest.mark.asyncio
async def test_unknown_category_is_classified_conservatively(repo):
    repo._state = _state(monthly=3_200_000_000)  # 80%
    with pytest.raises(bw.OutboundBandwidthPolicyError):
        await bw.reserve_outbound_bytes("mystery_payload", 1, now=NOW)
    repo._state = _state(monthly=2_800_000_000)  # 70%
    result = await bw.reserve_outbound_bytes("mystery_payload", 1, now=NOW)
    assert result["action"] == "media_transfer"


@pytest.mark.asyncio
async def test_unknown_action_is_rejected_before_any_reservation(repo):
    repo._state = _state(monthly=0)
    with pytest.raises(ValueError):
        await bw.reserve_outbound_bytes(
            "telegram_media", 1, action="arbitrary_string", now=NOW
        )
    assert repo.state["monthly"]["bytes"] == 0


@pytest.mark.asyncio
async def test_scheduled_scope_cannot_be_classified_as_interactive(repo):
    repo._state = _state(monthly=2_000_000_000)  # 50%
    with bw.scheduled_media_scope():
        assert bw.in_scheduled_media_scope() is True
        with pytest.raises(bw.OutboundBandwidthPolicyError):
            await bw.reserve_outbound_bytes("telegram_media", 1, now=NOW)
    assert bw.in_scheduled_media_scope() is False


# ── 3. durable floor / Render metric trust ──


@pytest.mark.asyncio
async def test_durable_floor_applies_without_local_monthly_usage(repo):
    repo._state = _state(monthly=0, floor=3_300_000_000)
    with pytest.raises(bw.OutboundBandwidthPolicyError):
        await bw.reserve_outbound_bytes(
            "telegram_media", 1, action="reupload", now=NOW
        )
    result = await bw.reserve_outbound_bytes(
        "telegram_media", 1, action="interactive_media", now=NOW
    )
    assert result["policy_tier"] == 4
    assert repo.state["policy"]["floor_bytes"] >= 3_300_000_000


@pytest.mark.asyncio
async def test_partial_render_metric_is_a_lower_bound_and_never_lowers_a_tier(repo):
    repo._state = _state(monthly=0, floor=3_300_000_000)
    bw.set_render_month_metric(
        MONTH, 3_400_000_000, trusted=False, coverage="partial",
        latest_data_at="2026-11-23T10:00:00Z",
    )
    with pytest.raises(bw.OutboundBandwidthPolicyError):
        await bw.reserve_outbound_bytes(
            "telegram_media", 1, action="reupload", now=NOW
        )
    snapshot = bw.get_policy_snapshot_sync(now=NOW)
    assert snapshot["available"] is True
    assert snapshot["tier"] == 4
    assert snapshot["effective_bytes"] == 3_400_000_000
    assert snapshot["render_trusted"] is False
    assert snapshot["render_coverage"] == "partial"


@pytest.mark.asyncio
async def test_previous_month_render_metric_is_ignored(repo):
    repo._state = _state(monthly=0)
    bw.set_render_month_metric(
        "2026-10", 3_500_000_000, trusted=True, coverage="complete",
        latest_data_at="2026-10-31T23:00:00Z",
    )
    result = await bw.reserve_outbound_bytes(
        "telegram_media", 1, action="scheduled_media", now=NOW
    )
    assert result["policy_tier"] == 0


@pytest.mark.asyncio
async def test_stale_render_bytes_stay_a_lower_bound_but_lose_trust(repo, monkeypatch):
    repo._state = _state(monthly=0)
    bw.set_render_month_metric(
        MONTH, 3_300_000_000, trusted=True, coverage="complete",
        latest_data_at="2026-11-23T10:00:00Z",
    )
    real_monotonic = bw.time.monotonic
    monkeypatch.setattr(
        bw.time, "monotonic",
        lambda: real_monotonic() + bw.RENDER_SNAPSHOT_MAX_AGE_S + 1,
    )
    snapshot = bw.get_policy_snapshot_sync(now=NOW)
    assert snapshot["tier"] == 4
    assert snapshot["effective_bytes"] == 3_300_000_000
    assert snapshot["render_trusted"] is False
    assert snapshot["render_coverage"] == "stale"


def test_render_floor_is_persisted_even_without_a_successful_transfer(repo):
    repo._state = _state(monthly=0)
    assert bw.persist_render_floor(MONTH, 3_300_000_000, now=NOW) is True
    assert repo.state["policy"]["floor_bytes"] == 3_300_000_000
    assert repo.state["monthly"]["bytes"] == 0
    # A restart with no Render snapshot still starts at the persisted tier.
    bw.set_render_month_metric("", None, trusted=False, coverage="unknown", latest_data_at=None)
    snapshot = bw.get_policy_snapshot_sync(now=NOW)
    assert snapshot["tier"] == 4
    assert snapshot["effective_bytes"] == 3_300_000_000


@pytest.mark.asyncio
async def test_month_rollover_clears_the_previous_floor(repo):
    repo._state = _state(monthly=0, floor=3_500_000_000, month="2026-10")
    december = datetime(2026, 12, 1, 0, 0, tzinfo=UTC)
    result = await bw.reserve_outbound_bytes(
        "telegram_media", 1, action="scheduled_media", now=december
    )
    assert result["policy_tier"] == 0


@pytest.mark.asyncio
async def test_malformed_policy_block_fails_closed(repo):
    state = _state(monthly=0)
    state["policy"] = "not-an-object"
    repo._state = state
    with pytest.raises(bw.OutboundBandwidthUnavailableError):
        await bw.reserve_outbound_bytes("telegram_media", 1, now=NOW)


# ── 4. existing hard limits still apply ──


@pytest.mark.asyncio
async def test_monthly_hard_limit_still_denies(repo):
    repo._state = _state(monthly=bw.MONTHLY_LIMIT_BYTES)
    with pytest.raises(bw.OutboundBandwidthError):
        await bw.reserve_outbound_bytes(
            "telegram_media", 1, action="interactive_media", now=NOW
        )


@pytest.mark.asyncio
async def test_daily_limit_still_denies(repo):
    repo._state = _state(monthly=0, daily=bw.DAILY_LIMIT_BYTES - 1)
    with pytest.raises(bw.OutboundBandwidthExceededError) as exc:
        await bw.reserve_outbound_bytes("telegram_media", 2, now=NOW)
    assert exc.value.period == "daily"


# ── 5. diagnostics ──


@pytest.mark.asyncio
async def test_denials_are_counted_by_bounded_action(repo):
    repo._state = _state(monthly=2_000_000_000)
    with pytest.raises(bw.OutboundBandwidthPolicyError):
        await bw.reserve_outbound_bytes(
            "telegram_media", 1, action="scheduled_media", now=NOW
        )
    snapshot = bw.get_policy_snapshot_sync(now=NOW)
    assert "scheduled_media:1" in snapshot["denials"]
    assert snapshot["next_threshold_bytes"] == bw.POLICY_TIER_BYTES[1]


def test_policy_snapshot_reports_unavailable_without_inventing_zero(repo):
    repo.load_error = RuntimeError("db down")
    snapshot = bw.get_policy_snapshot_sync(now=NOW)
    assert snapshot["available"] is False
    assert snapshot["detail"]


@pytest.mark.asyncio
async def test_usage_snapshot_exposes_effective_usage(repo):
    repo._state = _state(monthly=100, floor=3_300_000_000)
    usage = await bw.get_outbound_usage(now=NOW)
    assert usage["available"] is True
    assert usage["monthly_used_bytes"] == 100
    assert usage["policy_floor_bytes"] == 3_300_000_000
    assert usage["effective_month_bytes"] == 3_300_000_000
    assert usage["policy_tier"] == 4
