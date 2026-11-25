"""Staged monthly outbound-bandwidth policy.

These tests exercise the central policy directly. The suite-wide accounting
double supplies only persistence and a fresh low Render observation; the
observations here are explicit so every threshold, failure mode and rollover is
deterministic.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import pytest

from backend.services import outbound_bandwidth_service as bw

UTC = timezone.utc
NOW = datetime.now(UTC)
DAY = NOW.strftime("%Y-%m-%d")
MONTH = NOW.strftime("%Y-%m")


@pytest.fixture
def policy_store(_outbound_bandwidth_test_accounting):
    """The suite-wide persistence double's JSONB state, by name."""
    return _outbound_bandwidth_test_accounting


def _snapshot(effective: int, *, trusted: bool = True, detail: str = ""):
    tier = bw._policy_tier(effective)
    current = NOW.isoformat()
    return {
        "trusted": trusted,
        "state": "available" if trusted else "unavailable",
        "detail": detail,
        "tier": tier,
        "effective_monthly_bytes": int(effective),
        "render_monthly_bytes": int(effective),
        "governor_monthly_bytes": 0,
        "policy_floor_bytes": 0,
        "monthly_limit_bytes": bw.MONTHLY_TARGET_BYTES,
        "monthly_period": MONTH,
        "latest_data_at": current,
        "checked_at": current,
        "render_available": trusted,
        "render_partial": False,
    }


def _install(monkeypatch, effective: int, *, trusted: bool = True, detail: str = ""):
    snapshot = _snapshot(effective, trusted=trusted, detail=detail)
    monkeypatch.setattr(bw, "_policy_snapshot", snapshot)
    return snapshot


def _render(monthly: int, *, latest: datetime | None = None, available: bool = True):
    return {
        "available": available,
        "state": "available" if available else "unavailable",
        "monthly_period": MONTH,
        "monthly_used_bytes": monthly,
        "latest_data_at": (latest or NOW).isoformat(),
        "monthly_is_partial": False,
        "detail": "" if available else "Render API unavailable.",
    }


def _governor(monthly: int, *, floor: int | None = None, available: bool = True):
    return {
        "available": available,
        "monthly_used_bytes": int(monthly),
        "daily_used_bytes": 0,
        "policy_floor_bytes": floor,
    }


def _allow_floor_write(monkeypatch):
    monkeypatch.setattr(bw, "_record_policy_floor", lambda *a, **k: True)


# ── exact boundaries and cumulative tiers ─────────────────────────────────


@pytest.mark.parametrize(
    "effective,expected",
    [
        (0, 0),
        (1_999_999_999, 0),
        (2_000_000_000, 50),
        (2_399_999_999, 50),
        (2_400_000_000, 60),
        (2_799_999_999, 60),
        (2_800_000_000, 70),
        (3_199_999_999, 70),
        (3_200_000_000, 80),
        (3_599_999_999, 80),
        (3_600_000_000, 90),
        (3_999_999_999, 90),
        (4_000_000_000, 90),
    ],
)
def test_exact_policy_thresholds(effective, expected):
    assert bw._policy_tier(effective) == expected


@pytest.mark.asyncio
async def test_50_percent_denies_only_automatic_media(monkeypatch):
    _install(monkeypatch, 2_000_000_000)

    with pytest.raises(bw.OutboundBandwidthPolicyError):
        await bw.ensure_action_allowed(bw.ACTION_AUTOMATIC_MEDIA, 1)
    assert await bw.ensure_action_allowed(bw.ACTION_SINGLE_MEDIA, 1)
    assert await bw.ensure_action_allowed(bw.ACTION_TEXT, 1)


@pytest.mark.asyncio
async def test_60_percent_keeps_the_50_percent_restriction_and_adds_bulk(
    monkeypatch,
):
    _install(monkeypatch, 2_400_000_000)

    with pytest.raises(bw.OutboundBandwidthPolicyError):
        await bw.ensure_action_allowed(bw.ACTION_AUTOMATIC_MEDIA, 1)
    with pytest.raises(bw.OutboundBandwidthPolicyError):
        await bw.ensure_action_allowed(bw.ACTION_BULK_MEDIA, 1)
    assert await bw.ensure_action_allowed(bw.ACTION_PROVIDER_MEDIA, 1)
    assert await bw.ensure_action_allowed(bw.ACTION_SINGLE_MEDIA, 1)


@pytest.mark.asyncio
async def test_70_percent_adds_provider_media(monkeypatch):
    _install(monkeypatch, 2_800_000_000)

    with pytest.raises(bw.OutboundBandwidthPolicyError):
        await bw.ensure_action_allowed(bw.ACTION_PROVIDER_MEDIA, 1)
    assert await bw.ensure_action_allowed(bw.ACTION_FILE_UPLOAD, 1)
    assert await bw.ensure_action_allowed(bw.ACTION_SINGLE_MEDIA, 1)


@pytest.mark.asyncio
async def test_80_percent_adds_file_reupload_and_storage_uploads(monkeypatch):
    _install(monkeypatch, 3_200_000_000)

    for action in (
        bw.ACTION_FILE_UPLOAD,
        bw.ACTION_REUPLOAD,
        bw.ACTION_STORAGE_UPLOAD,
        bw.ACTION_MEDIA_FORWARD,
    ):
        with pytest.raises(bw.OutboundBandwidthPolicyError):
            await bw.ensure_action_allowed(action, 1)
    assert await bw.ensure_action_allowed(bw.ACTION_SINGLE_MEDIA, 1)


@pytest.mark.asyncio
async def test_90_percent_is_strict_text_only(monkeypatch):
    _install(monkeypatch, 3_600_000_000)

    for action in (
        bw.ACTION_AUTOMATIC_MEDIA,
        bw.ACTION_SINGLE_MEDIA,
        bw.ACTION_BULK_MEDIA,
        bw.ACTION_PROVIDER_MEDIA,
        bw.ACTION_FILE_UPLOAD,
        bw.ACTION_REUPLOAD,
        bw.ACTION_STORAGE_UPLOAD,
        bw.ACTION_MEDIA_FORWARD,
        bw.ACTION_UNKNOWN,
    ):
        with pytest.raises(bw.OutboundBandwidthPolicyError):
            await bw.ensure_action_allowed(action, 1)
    assert await bw.ensure_action_allowed(bw.ACTION_TEXT, 1)


# ── text exemption, disguised payloads, unknown actions ───────────────────


@pytest.mark.asyncio
@pytest.mark.parametrize("effective", [0, 2_000_000_000, 2_400_000_000, 2_800_000_000, 3_200_000_000, 3_600_000_000])
async def test_text_is_exempt_at_every_tier_but_bounded(monkeypatch, effective):
    _install(monkeypatch, effective)

    assert await bw.ensure_action_allowed(bw.ACTION_TEXT, 1)
    assert await bw.ensure_action_allowed(bw.ACTION_TEXT, bw.TEXT_EXEMPT_MAX_BYTES)
    with pytest.raises(bw.OutboundBandwidthPolicyError):
        await bw.ensure_action_allowed(
            bw.ACTION_TEXT, bw.TEXT_EXEMPT_MAX_BYTES + 1,
        )


@pytest.mark.asyncio
async def test_an_oversized_text_payload_cannot_bypass_at_tier_zero(monkeypatch):
    _install(monkeypatch, 0)

    with pytest.raises(bw.OutboundBandwidthPolicyError):
        await bw.ensure_action_allowed(
            bw.ACTION_TEXT, bw.TEXT_EXEMPT_MAX_BYTES + 1,
        )


def test_unknown_actions_are_not_classified_as_text():
    assert bw.classify_outbound_action("some-new-media-path") == bw.ACTION_UNKNOWN
    assert bw.classify_outbound_action("text") == bw.ACTION_TEXT
    assert bw.classify_outbound_action("telegram_media") == bw.ACTION_SINGLE_MEDIA


@pytest.mark.asyncio
async def test_unknown_actions_default_to_the_conservative_media_policy(
    monkeypatch,
):
    _install(monkeypatch, 2_000_000_000)

    with pytest.raises(bw.OutboundBandwidthPolicyError):
        await bw.ensure_action_allowed(bw.ACTION_UNKNOWN, 1)


@pytest.mark.asyncio
async def test_scheduled_context_marks_every_media_class_automatic(monkeypatch):
    _install(monkeypatch, 2_000_000_000)

    with pytest.raises(bw.OutboundBandwidthPolicyError):
        with bw.automatic_media_scope():
            await bw.ensure_action_allowed(bw.ACTION_PROVIDER_MEDIA, 1)

    assert await bw.ensure_action_allowed(bw.ACTION_PROVIDER_MEDIA, 1)


# ── Render + governor: max, never a sum ───────────────────────────────────


@pytest.mark.asyncio
async def test_render_usage_is_used_when_it_is_higher(monkeypatch):
    _allow_floor_write(monkeypatch)
    monkeypatch.setattr(bw, "_render_usage_reader", lambda **k: _async_return(_render(3_000_000_000)))
    monkeypatch.setattr(bw, "_current_governor_monthly", lambda now: _governor(1_000_000_000))

    snapshot = await bw.refresh_policy_snapshot(now=NOW, force=True)

    assert snapshot["trusted"] is True
    assert snapshot["effective_monthly_bytes"] == 3_000_000_000
    assert snapshot["render_monthly_bytes"] == 3_000_000_000
    assert snapshot["governor_monthly_bytes"] == 1_000_000_000


@pytest.mark.asyncio
async def test_governor_usage_is_used_when_it_is_higher(monkeypatch):
    _allow_floor_write(monkeypatch)
    monkeypatch.setattr(bw, "_render_usage_reader", lambda **k: _async_return(_render(1_000_000_000)))
    monkeypatch.setattr(bw, "_current_governor_monthly", lambda now: _governor(2_500_000_000))

    snapshot = await bw.refresh_policy_snapshot(now=NOW, force=True)

    assert snapshot["effective_monthly_bytes"] == 2_500_000_000
    assert snapshot["effective_monthly_bytes"] != 3_500_000_000
    assert snapshot["tier"] == 60


# ── failure modes fail conservatively for media, never text ───────────────


async def _async_return(value):
    return value


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "observation,expected_detail",
    [
        ({"available": False, "detail": "Render API unavailable"}, "unavailable"),
        (_render(3_000_000_000, latest=NOW - timedelta(hours=4)), "older"),
        (
            {
                "available": True,
                "monthly_period": MONTH,
                "monthly_used_bytes": "not-a-number",
                "latest_data_at": NOW.isoformat(),
            },
            "malformed",
        ),
    ],
)
async def test_untrusted_render_observations_deny_media_but_not_text(
    monkeypatch, observation, expected_detail,
):
    _allow_floor_write(monkeypatch)
    monkeypatch.setattr(bw, "_render_usage_reader", lambda **k: _async_return(observation))
    monkeypatch.setattr(bw, "_current_governor_monthly", lambda now: _governor(0))

    snapshot = await bw.refresh_policy_snapshot(now=NOW, force=True)
    assert snapshot["trusted"] is False
    assert expected_detail in snapshot["detail"].lower()

    with pytest.raises(bw.OutboundBandwidthPolicyError):
        await bw.ensure_action_allowed(bw.ACTION_SINGLE_MEDIA, 1)
    assert await bw.ensure_action_allowed(bw.ACTION_TEXT, 1)


@pytest.mark.asyncio
async def test_unavailable_governor_also_denies_media(monkeypatch):
    _allow_floor_write(monkeypatch)
    monkeypatch.setattr(bw, "_render_usage_reader", lambda **k: _async_return(_render(0)))
    monkeypatch.setattr(bw, "_current_governor_monthly", lambda now: _governor(0, available=False))

    snapshot = await bw.refresh_policy_snapshot(now=NOW, force=True)

    assert snapshot["trusted"] is False
    with pytest.raises(bw.OutboundBandwidthPolicyError):
        await bw.ensure_action_allowed(bw.ACTION_FILE_UPLOAD, 1)
    assert await bw.ensure_action_allowed(bw.ACTION_TEXT, 1)


# ── cache, restart and UTC rollover ───────────────────────────────────────


@pytest.mark.asyncio
async def test_policy_cache_avoids_one_render_request_per_upload(monkeypatch):
    calls = []

    async def _reader(**kwargs):
        calls.append(kwargs.get("now"))
        return _render(0)

    _allow_floor_write(monkeypatch)
    monkeypatch.setattr(bw, "_render_usage_reader", _reader)
    monkeypatch.setattr(bw, "_current_governor_monthly", lambda now: _governor(0))

    first = await bw.refresh_policy_snapshot(now=NOW, force=True)
    second = await bw.refresh_policy_snapshot(now=NOW + timedelta(seconds=60))
    third = await bw.refresh_policy_snapshot(now=NOW + timedelta(seconds=301))

    assert first["checked_at"] == NOW.isoformat()
    assert second["checked_at"] == NOW.isoformat()
    assert third["checked_at"] == (NOW + timedelta(seconds=301)).isoformat()
    assert len(calls) == 2


def test_floor_write_preserves_the_current_reservation_counters(policy_store):
    policy_store["state"] = bw._build_state(DAY, 1234, MONTH, 5678)

    assert bw._record_policy_floor(MONTH, 3_600_000_000, DAY) is True

    persisted = policy_store["state"]
    assert persisted["daily"]["bytes"] == 1234
    assert persisted["monthly"]["bytes"] == 5678
    assert persisted["policy_floor"] == {
        "period": MONTH, "bytes": 3_600_000_000,
    }


@pytest.mark.asyncio
async def test_restart_does_not_reset_the_persisted_monthly_floor(
    monkeypatch, policy_store,
):
    policy_store["state"] = bw._build_state(
        DAY, 0, MONTH, 0,
        policy_floor={"period": MONTH, "bytes": 3_600_000_000},
    )
    monkeypatch.setattr(bw, "_policy_snapshot", None)
    monkeypatch.setattr(bw, "_render_usage_reader", lambda **k: _async_return(_render(0)))

    snapshot = await bw.refresh_policy_snapshot(now=NOW, force=True)

    assert snapshot["trusted"] is True
    assert snapshot["effective_monthly_bytes"] == 3_600_000_000
    assert snapshot["tier"] == 90


def test_utc_month_rollover_ignores_the_previous_month_floor(policy_store):
    policy_store["state"] = bw._build_state(
        DAY, 100, MONTH, 100,
        policy_floor={"period": MONTH, "bytes": 3_600_000_000},
    )

    rolled = bw._current_governor_monthly(
        datetime(2026, 12, 1, 0, 0, tzinfo=UTC)
    )

    assert rolled["monthly_used_bytes"] == 0
    assert rolled["policy_floor_bytes"] is None


# ── hard limits and sync/async parity ─────────────────────────────────────


@pytest.mark.asyncio
async def test_daily_hard_limit_is_unchanged_and_precedes_policy(monkeypatch):
    _install(monkeypatch, 3_600_000_000)

    with pytest.raises(bw.OutboundBandwidthExceededError) as error:
        await bw.reserve_outbound_bytes("telegram_media", 150_000_001)

    assert error.value.period == "daily"
    assert bw.DAILY_LIMIT_BYTES == 150_000_000
    assert bw.MONTHLY_TARGET_BYTES == 4_000_000_000


@pytest.mark.asyncio
async def test_async_and_sync_reservations_enforce_the_same_policy(
    monkeypatch, policy_store,
):
    _install(monkeypatch, 3_600_000_000)

    with pytest.raises(bw.OutboundBandwidthPolicyError):
        await bw.reserve_outbound_bytes("telegram_media", 1)
    with pytest.raises(bw.OutboundBandwidthPolicyError):
        bw.reserve_outbound_bytes_sync("telegram_media", 1)
    assert policy_store["state"] == {}


@pytest.mark.asyncio
async def test_bulk_context_cannot_bypass_the_sixty_percent_policy(
    monkeypatch, policy_store,
):
    _install(monkeypatch, 2_400_000_000)

    with pytest.raises(bw.OutboundBandwidthPolicyError):
        await bw.ensure_action_allowed(bw.ACTION_BULK_MEDIA, 1)
    assert policy_store["state"] == {}


@pytest.mark.asyncio
async def test_a_partial_month_cannot_lower_a_persisted_floor(
    monkeypatch, policy_store,
):
    policy_store["state"] = bw._build_state(
        DAY, 0, MONTH, 0,
        policy_floor={"period": MONTH, "bytes": 3_600_000_000},
    )
    monkeypatch.setattr(bw, "_policy_snapshot", None)
    partial = _render(1_000_000_000)
    partial["monthly_is_partial"] = True
    monkeypatch.setattr(
        bw, "_render_usage_reader", lambda **k: _async_return(partial)
    )
    monkeypatch.setattr(
        bw, "_current_governor_monthly",
        lambda now: _governor(0, floor=3_600_000_000),
    )

    snapshot = await bw.refresh_policy_snapshot(now=NOW, force=True)

    assert snapshot["trusted"] is True
    assert snapshot["effective_monthly_bytes"] == 3_600_000_000
    assert snapshot["tier"] == 90
    assert snapshot["render_partial"] is True
    assert snapshot["policy_floor_bytes"] == 3_600_000_000
