"""Render bandwidth API parsing and account/service isolation tests."""
from __future__ import annotations

from datetime import datetime, timezone

import pytest

from backend.services import render_bandwidth_service as render_bw

UTC = timezone.utc
NOW = datetime(2026, 11, 23, 18, 0, tzinfo=UTC)
SERVICE_ID = "srv-account-one"


def _payload(service_id: str = SERVICE_ID) -> list[dict]:
    return [{
        "labels": [
            {"field": "service", "value": service_id},
            {"field": "resource", "value": service_id},
        ],
        "unit": "mb",
        "values": [
            {"timestamp": "2026-11-01T01:00:00Z", "value": 2.5},
            {"timestamp": "2026-11-22T23:00:00Z", "value": 3.25},
            {"timestamp": "2026-11-23T10:00:00Z", "value": 17.75},
        ],
    }]


class _Response:
    def __init__(self, payload, status: int = 200):
        self._payload = payload
        self.status_code = status

    def raise_for_status(self):
        if self.status_code >= 400:
            import httpx
            request = httpx.Request("GET", render_bw._API_URL)
            response = httpx.Response(self.status_code, request=request)
            raise httpx.HTTPStatusError("failed", request=request, response=response)

    def json(self):
        return self._payload


class _Client:
    response = None
    calls = []

    def __init__(self, *, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def get(self, url, *, params, headers):
        self.calls.append((url, params, headers))
        return self.response


@pytest.fixture(autouse=True)
def _reset_client():
    _Client.calls = []
    _Client.response = None


@pytest.mark.asyncio
async def test_queries_only_the_render_injected_service_and_sums_utc_periods(monkeypatch):
    monkeypatch.setenv("RENDER_API_KEY", "workspace-scoped-secret")
    monkeypatch.setenv("RENDER_SERVICE_ID", SERVICE_ID)
    _Client.response = _Response(_payload())
    monkeypatch.setattr(render_bw.httpx, "AsyncClient", _Client)

    usage = await render_bw.get_render_bandwidth_usage(now=NOW)

    assert usage["available"] is True
    assert usage["daily_period"] == "2026-11-23"
    assert usage["monthly_period"] == "2026-11"
    assert usage["daily_used_bytes"] == 17_750_000
    assert usage["monthly_used_bytes"] == 23_500_000
    assert usage["latest_data_at"] == "2026-11-23T10:00:00Z"
    assert usage["data_points"] == 3

    url, params, headers = _Client.calls[0]
    assert url == render_bw._API_URL
    assert params == {
        "startTime": "2026-11-01T00:00:00Z",
        "endTime": "2026-11-23T18:00:00Z",
        "resource": SERVICE_ID,
    }
    assert headers["Authorization"] == "Bearer workspace-scoped-secret"


@pytest.mark.asyncio
async def test_missing_api_key_is_unavailable_not_zero(monkeypatch):
    monkeypatch.delenv("RENDER_API_KEY", raising=False)
    monkeypatch.setenv("RENDER_SERVICE_ID", SERVICE_ID)

    usage = await render_bw.get_render_bandwidth_usage(now=NOW)

    assert usage["available"] is False
    assert "not configured" in usage["detail"]
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_a_series_from_another_account_or_service_is_rejected(monkeypatch):
    monkeypatch.setenv("RENDER_API_KEY", "workspace-scoped-secret")
    monkeypatch.setenv("RENDER_SERVICE_ID", SERVICE_ID)
    _Client.response = _Response(_payload("srv-another-account"))
    monkeypatch.setattr(render_bw.httpx, "AsyncClient", _Client)

    usage = await render_bw.get_render_bandwidth_usage(now=NOW)

    assert usage["available"] is False
    assert "exactly one" in usage["detail"]
    assert usage.get("monthly_used_bytes") is None


@pytest.mark.asyncio
async def test_http_error_is_reported_without_leaking_credentials(monkeypatch):
    monkeypatch.setenv("RENDER_API_KEY", "workspace-scoped-secret")
    monkeypatch.setenv("RENDER_SERVICE_ID", SERVICE_ID)
    _Client.response = _Response({}, status=403)
    monkeypatch.setattr(render_bw.httpx, "AsyncClient", _Client)

    usage = await render_bw.get_render_bandwidth_usage(now=NOW)

    assert usage["available"] is False
    assert "HTTP 403" in usage["detail"]
    assert "workspace-scoped-secret" not in usage["detail"]


@pytest.mark.asyncio
async def test_31_day_month_uses_the_supported_30_day_window_and_marks_partial(monkeypatch):
    now = datetime(2026, 12, 31, 18, 0, tzinfo=UTC)
    monkeypatch.setenv("RENDER_API_KEY", "workspace-scoped-secret")
    monkeypatch.setenv("RENDER_SERVICE_ID", SERVICE_ID)
    _Client.response = _Response([{
        "labels": [{"field": "service", "value": SERVICE_ID}],
        "unit": "mb",
        "values": [{"timestamp": "2026-12-31T10:00:00Z", "value": 4.0}],
    }])
    monkeypatch.setattr(render_bw.httpx, "AsyncClient", _Client)

    usage = await render_bw.get_render_bandwidth_usage(now=now)

    assert usage["available"] is True
    assert usage["monthly_is_partial"] is True
    assert usage["monthly_coverage_start"] == "2026-12-02T18:00:00Z"
    assert _Client.calls[0][1]["startTime"] == "2026-12-02T18:00:00Z"
