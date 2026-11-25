"""Render bandwidth metric + source diagnostic — validated, never invented.

The documented shapes exercised here are Render's own:

  * ``GET /v1/metrics/bandwidth`` -> array of
    ``{labels: [{field,value}], values: [{timestamp, value}], unit}``;
  * ``GET /v1/metrics/bandwidth-sources`` -> ``{data: [{labels:
    {resource, trafficSource}, values: [{timestamp, value}]}]}``.

No live Render call is made: every test uses an ``httpx.MockTransport`` whose
responses are built from that documentation.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone

import httpx
import pytest

from backend.services import render_bandwidth_service as rbw
from backend.services import render_bandwidth_sources_diagnostic as sources

UTC = timezone.utc


@pytest.fixture(autouse=True)
def _configured(monkeypatch):
    monkeypatch.setenv("RENDER_API_KEY", "test-key")
    monkeypatch.setenv("RENDER_SERVICE_ID", "srv-test")
    rbw.reset_for_tests()
    sources.reset_for_tests()
    yield
    rbw.reset_for_tests()
    sources.reset_for_tests()


def _series(unit="byte", value=1.0, skip_hour=None, duplicate=False):
    def handler(request: httpx.Request) -> httpx.Response:
        start = datetime.fromisoformat(
            request.url.params["startTime"].replace("Z", "+00:00")
        )
        end = datetime.fromisoformat(
            request.url.params["endTime"].replace("Z", "+00:00")
        )
        points = []
        cursor = start
        while cursor < end:
            if skip_hour is None or cursor != skip_hour:
                points.append(
                    {"timestamp": cursor.strftime("%Y-%m-%dT%H:%M:%SZ"), "value": value}
                )
            cursor += timedelta(hours=1)
        entry = {
            "labels": [{"field": "instance", "value": "srv-test-instance"}],
            "values": points,
            "unit": unit,
        }
        return httpx.Response(200, json=[entry, entry] if duplicate else [entry])

    return handler


async def _refresh(handler, now):
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        return await rbw.refresh_month_metric(now=now, client=client)


# ── 1. coverage over calendar months ──


@pytest.mark.asyncio
async def test_30_day_month_is_complete_and_trusted():
    now = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)
    result = await _refresh(_series(), now)
    assert result["status"] == "ok"
    assert result["trusted"] is True
    assert result["coverage"] == "complete"
    assert result["unit_valid"] is True
    assert result["bytes"] == 29 * 24 + 12
    assert result["missing_hours"] == 0
    assert result["full_month_queryable"] is True
    assert result["latest_data_at"] == "2026-11-30T11:00:00Z"


@pytest.mark.asyncio
async def test_31_day_month_is_partial_and_never_trusted_as_complete():
    now = datetime(2026, 12, 31, 12, 0, tzinfo=UTC)
    result = await _refresh(_series(), now)
    assert result["trusted"] is False
    assert result["coverage"] == "partial"
    assert result["full_month_queryable"] is False
    assert result["missing_start_seconds"] == 12 * 3600
    # The queryable portion is still a real LOWER BOUND, never zero.
    assert result["bytes"] == 30 * 24
    assert result["missing_hours"] == 12


@pytest.mark.asyncio
async def test_missing_hour_is_detected_and_not_zero_filled():
    now = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)
    skipped = datetime(2026, 11, 15, 0, 0, tzinfo=UTC)
    result = await _refresh(_series(skip_hour=skipped), now)
    assert result["missing_hours"] == 1
    assert result["bytes"] == 29 * 24 + 12 - 1
    assert result["trusted"] is False
    assert result["coverage"] == "partial"


@pytest.mark.asyncio
async def test_duplicate_series_is_detected_and_not_double_counted():
    now = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)
    result = await _refresh(_series(duplicate=True), now)
    assert result["duplicates"] > 0
    assert result["trusted"] is False
    assert result["bytes"] == 29 * 24 + 12


@pytest.mark.asyncio
async def test_unknown_unit_is_never_treated_as_bytes():
    now = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)
    result = await _refresh(_series(unit="byte/s"), now)
    assert result["unit_valid"] is False
    assert result["trusted"] is False
    assert result["status"] == "partial"


@pytest.mark.asyncio
async def test_unexpected_http_200_payload_is_reported_not_parsed_as_zero():
    async def handler(request):
        return httpx.Response(200, json={"unexpected": True})

    now = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)
    result = await _refresh(handler, now)
    assert result["status"] == "error"
    assert result["bytes"] is None
    assert result["trusted"] is False


@pytest.mark.asyncio
async def test_http_error_is_reported_without_inventing_bytes():
    async def handler(request):
        return httpx.Response(500, text="upstream error")

    now = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)
    result = await _refresh(handler, now)
    assert result["status"] == "error"
    assert result["http_status"] == 500
    assert result["bytes"] is None


@pytest.mark.asyncio
async def test_unconfigured_render_api_makes_no_request(monkeypatch):
    monkeypatch.delenv("RENDER_API_KEY", raising=False)
    called = False

    async def handler(request):
        nonlocal called
        called = True
        return httpx.Response(200, json=[])

    result = await _refresh(handler, datetime(2026, 11, 30, 12, 0, tzinfo=UTC))
    assert called is False
    assert result["status"] == "unconfigured"
    assert result["bytes"] is None


def test_source_missing_data_is_not_zero():
    now = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)
    start = now - timedelta(hours=3)
    summary = sources.summarize([], month="2026-11", start=start, now=now)
    assert summary["status"] == "unavailable"
    assert summary["total_bytes"] is None
    assert summary["hours"] == 0


# ── 2. source breakdown ──


def _source_payload(start, hours=3, omit=None, duplicate=False, unknown=False):
    data = []
    for name, value in (("total", 10.0), ("http", 6.0), ("websocket", 4.0)):
        if name == omit:
            continue
        points = [
            {
                "timestamp": int((start + timedelta(hours=index)).timestamp()),
                "value": value,
            }
            for index in range(hours)
        ]
        data.append(
            {"labels": {"resource": "srv-test", "trafficSource": name}, "values": points}
        )
        if duplicate:
            data.append(
                {"labels": {"resource": "srv-test", "trafficSource": name}, "values": points}
            )
    if unknown:
        data.append(
            {
                "labels": {"resource": "srv-test", "trafficSource": "proprietary"},
                "values": [{"timestamp": int(start.timestamp()), "value": 99}],
            }
        )
    return {"data": data}


def test_source_breakdown_reconciles_without_assuming_equality():
    now = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)
    start = now - timedelta(hours=3)
    summary = sources.summarize(
        [_source_payload(start)], month="2026-11", start=start, now=now
    )
    assert summary["sources"]["total"] == 30
    assert summary["sources"]["http"] == 18
    assert summary["sources"]["websocket"] == 12
    assert summary["reconciliation"] == "matches"
    assert summary["difference_bytes"] == 0
    assert summary["value_unit"] == "unspecified"


def test_missing_component_series_is_reported_and_not_reconciled_as_zero():
    now = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)
    start = now - timedelta(hours=3)
    summary = sources.summarize(
        [_source_payload(start, omit="http")], month="2026-11", start=start, now=now
    )
    assert "http" in summary["missing_series"]
    assert summary["reconciliation"] == "differs"
    assert summary["difference_bytes"] == -18
    assert "http" not in summary["sources"]


def test_duplicate_source_series_is_detected():
    now = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)
    start = now - timedelta(hours=3)
    summary = sources.summarize(
        [_source_payload(start, duplicate=True)], month="2026-11", start=start, now=now
    )
    assert summary["duplicates"] > 0
    assert summary["status"] == "partial"


def test_unknown_traffic_source_is_counted_not_printed():
    now = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)
    start = now - timedelta(hours=3)
    summary = sources.summarize(
        [_source_payload(start, unknown=True)], month="2026-11", start=start, now=now
    )
    assert summary["unknown_source_series"] == 1
    assert "proprietary" not in summary["sources"]
    assert summary["status"] == "partial"


def test_missing_hours_in_sources_are_not_zero_filled():
    now = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)
    start = now - timedelta(hours=5)
    summary = sources.summarize(
        [_source_payload(start, hours=3)], month="2026-11", start=start, now=now
    )
    assert summary["expected_hours"] == 5
    assert summary["missing_hours"] == 2
    assert summary["status"] == "partial"


def test_malformed_sources_payload_is_reported():
    now = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)
    start = now - timedelta(hours=3)
    summary = sources.summarize(
        [{"nope": []}], month="2026-11", start=start, now=now
    )
    assert summary["status"] == "unexpected_payload"
    assert summary["total_bytes"] is None


@pytest.mark.asyncio
async def test_source_diagnostic_reads_the_documented_object_shape():
    now = datetime(2026, 11, 30, 12, 0, tzinfo=UTC)
    start = now - timedelta(hours=3)

    async def handler(request):
        return httpx.Response(200, json=_source_payload(start))

    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        summary = await sources.diagnose(now=now, client=client)
    assert summary["status"] in {"partial", "ok"}
    assert summary["sources"]["total"] == 30
    assert summary["value_unit"] == "unspecified"


@pytest.mark.asyncio
async def test_source_diagnostic_before_its_data_window_is_unsupported():
    async def handler(request):
        raise AssertionError("must not call the API before 2025-03-09")

    now = datetime(2025, 2, 15, 12, 0, tzinfo=UTC)
    async with httpx.AsyncClient(transport=httpx.MockTransport(handler)) as client:
        summary = await sources.diagnose(now=now, client=client)
    assert summary["status"] == "unsupported"
