"""Tests for the temporary, opt-in Render bandwidth-source diagnostic."""
from __future__ import annotations

import asyncio
import json
import logging

import httpx
import pytest

from backend.services import render_bandwidth_sources_diagnostic as diag

SERVICE_ID = "srv-self-bot-prod"
API_KEY = "render-api-key-never-logged"
START = int(diag._WINDOW_START.timestamp())


class _Stream:
    def __init__(self, status: int, chunks: list[bytes]):
        self.status_code = status
        self._chunks = chunks

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    async def aiter_bytes(self):
        for chunk in self._chunks:
            yield chunk


class _Client:
    status = 200
    chunks: list[bytes] = [b"{}"]
    calls: list[tuple] = []
    error: Exception | None = None

    def __init__(self, *, timeout):
        self.timeout = timeout

    async def __aenter__(self):
        return self

    async def __aexit__(self, *args):
        return False

    def stream(self, method, url, *, params, headers):
        type(self).calls.append((method, url, params, headers))
        if type(self).error is not None:
            raise type(self).error
        return _Stream(type(self).status, type(self).chunks)


@pytest.fixture(autouse=True)
def _reset_client():
    _Client.status = 200
    _Client.chunks = [b"{}"]
    _Client.calls = []
    _Client.error = None
    yield
    _Client.error = None


def _payload(points: dict[str, list[tuple[int, float]]], *, unit: str | None = "mb",
             service_id: str = SERVICE_ID) -> dict:
    data = []
    for source, values in points.items():
        entry = {
            "labels": {"resource": service_id, "trafficSource": source},
            "values": [
                {"timestamp": START + offset * 3600, "value": value}
                for offset, value in values
            ],
        }
        if unit is not None:
            entry["unit"] = unit
        data.append(entry)
    return {"data": data}


def _install(monkeypatch, payload, *, status: int = 200, chunks=None, error=None):
    monkeypatch.setenv(diag.ENABLE_ENV, "1")
    monkeypatch.setenv("RENDER_API_KEY", API_KEY)
    monkeypatch.setenv("RENDER_SERVICE_ID", SERVICE_ID)
    monkeypatch.setattr(diag.httpx, "AsyncClient", _Client)
    _Client.status = status
    _Client.chunks = chunks if chunks is not None else [json.dumps(payload).encode()]
    _Client.error = error


SPLIT = {
    "total": [(0, 100.0), (143, 200.0)],
    "http": [(0, 60.0), (143, 110.0)],
    "websocket": [(0, 10.0), (143, 20.0)],
    "nat": [(0, 25.0), (143, 65.0)],
    "privatelink": [(0, 5.0), (143, 5.0)],
}


@pytest.mark.asyncio
async def test_disabled_by_default_makes_no_request(monkeypatch):
    monkeypatch.delenv(diag.ENABLE_ENV, raising=False)

    result = await diag.run_if_enabled()

    assert result == {"ran": False, "ok": False, "reason": "disabled"}
    assert _Client.calls == []


@pytest.mark.asyncio
@pytest.mark.parametrize("value", ["", "0", "true", "yes", "on", " 1", "1 "])
async def test_only_the_exact_value_one_enables_the_run(monkeypatch, value):
    monkeypatch.setenv(diag.ENABLE_ENV, value)

    result = await diag.run_if_enabled()

    assert result["ran"] is False
    assert result["reason"] == "disabled"
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_queries_only_the_injected_service_for_the_requested_window(monkeypatch):
    _install(monkeypatch, _payload(SPLIT))

    await diag.run_if_enabled()

    assert len(_Client.calls) == 1
    method, url, params, headers = _Client.calls[0]
    assert method == "GET"
    assert url == "https://api.render.com/v1/metrics/bandwidth-sources"
    assert params == {
        "resource": SERVICE_ID,
        "startTime": "2026-11-07T00:00:00Z",
        "endTime": "2026-11-13T00:00:00Z",
    }
    assert headers["Authorization"] == f"Bearer {API_KEY}"


@pytest.mark.asyncio
async def test_aggregates_each_traffic_source_and_reports_completeness(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _install(monkeypatch, _payload(SPLIT))

    result = await diag.run_if_enabled()

    assert "response http_status=200" in caplog.text
    assert result["ok"] is True
    assert result["http_status"] == 200
    assert result["declared_series"] == 5
    assert result["series_count"] == 5
    assert result["expected_hours"] == 144
    assert result["hours_with_any_data"] == 2
    assert result["missing_hours"] == 142
    assert result["total_present"] is True
    assert result["parts_present"] == ["http", "websocket", "nat", "privatelink"]
    assert result["parts_match_total"] is True
    assert result["unexpected_sources"] == []
    assert result["mixed_units"] is False

    expected_mb = {
        "total": 300.0,
        "http": 170.0,
        "websocket": 30.0,
        "nat": 90.0,
        "privatelink": 10.0,
    }
    for source, megabytes in expected_mb.items():
        series = result["sources"][source]
        assert series["points"] == 2
        assert series["missing_hours"] == 142
        assert series["raw_total"] == pytest.approx(megabytes)
        assert series["bytes_total"] == int(megabytes * 1_000_000)
        assert series["mb_total"] == pytest.approx(megabytes)
        assert series["unit"] == "mb"
        assert series["unit_assumed_bytes"] is False

    assert result["sources"]["total"]["gb_total"] == pytest.approx(0.3)


@pytest.mark.asyncio
async def test_missing_datapoints_are_reported_not_zero_filled(monkeypatch):
    _install(monkeypatch, _payload({"http": [(0, 60.0), (5, 5.0)]}))

    result = await diag.run_if_enabled()

    assert result["ok"] is True
    assert result["total_present"] is False
    assert result["parts_match_total"] is None
    assert result["hours_with_any_data"] == 2
    assert result["missing_hours"] == 142
    http = result["sources"]["http"]
    assert http["raw_total"] == pytest.approx(65.0)
    assert http["bytes_total"] == 65_000_000
    assert http["points"] == 2
    assert http["missing_hours"] == 142


@pytest.mark.asyncio
async def test_absent_sources_are_not_treated_as_zero(monkeypatch):
    _install(monkeypatch, _payload({"http": [(0, 1.0)]}))

    result = await diag.run_if_enabled()

    assert set(result["sources"]) == {"http"}
    for absent in ("total", "websocket", "nat", "privatelink"):
        assert absent not in result["sources"]


@pytest.mark.asyncio
async def test_series_from_another_service_fails_closed_without_logging_its_id(
    monkeypatch, caplog
):
    caplog.set_level(logging.INFO)
    other = "srv-another-account"
    _install(monkeypatch, _payload({"http": [(0, 5.0)]}, service_id=other))

    result = await diag.run_if_enabled()

    assert result["ok"] is False
    assert result["reason"] == "resource_mismatch"
    assert "sources" not in result
    assert other not in caplog.text
    assert API_KEY not in caplog.text


@pytest.mark.asyncio
@pytest.mark.parametrize(
    "payload, reason",
    [
        ({}, "unexpected_payload"),
        ({"data": {}}, "unexpected_payload"),
        ({"data": []}, "no_data"),
        (
            {"data": [{"labels": {"resource": SERVICE_ID, "trafficSource": "http"}}]},
            "malformed_series",
        ),
        (
            {
                "data": [
                    {
                        "labels": {"resource": SERVICE_ID, "trafficSource": "http"},
                        "values": [],
                    }
                ]
            },
            "no_data",
        ),
        (
            {
                "data": [
                    {
                        "labels": {"resource": SERVICE_ID},
                        "values": [],
                    }
                ]
            },
            "malformed_series",
        ),
        (
            {
                "data": [
                    {
                        "labels": {"resource": SERVICE_ID, "trafficSource": "http"},
                        "values": [{"timestamp": START, "value": -1}],
                    }
                ]
            },
            "invalid_value",
        ),
        (
            {
                "data": [
                    {
                        "labels": {"resource": SERVICE_ID, "trafficSource": "http"},
                        "values": [{"timestamp": "not-a-time", "value": 1}],
                    }
                ]
            },
            "invalid_timestamp",
        ),
        (
            {
                "data": [
                    {
                        "labels": {"resource": SERVICE_ID, "trafficSource": "http"},
                        "values": [{"value": 1}],
                    }
                ]
            },
            "invalid_timestamp",
        ),
        (
            {
                "data": [
                    {
                        "labels": {"resource": SERVICE_ID, "trafficSource": "http"},
                        "values": [{"timestamp": START, "value": "NaN"}],
                    }
                ]
            },
            "invalid_value",
        ),
    ],
)
async def test_malformed_payloads_fail_closed(monkeypatch, payload, reason):
    _install(monkeypatch, payload)

    result = await diag.run_if_enabled()

    assert result["ok"] is False
    assert result["reason"] == reason


@pytest.mark.asyncio
async def test_duplicate_traffic_source_fails_closed(monkeypatch):
    payload = _payload({"http": [(0, 1.0)]})
    payload["data"].append(payload["data"][0])
    _install(monkeypatch, payload)

    result = await diag.run_if_enabled()

    assert result["ok"] is False
    assert result["reason"] == "duplicate_traffic_source"


@pytest.mark.asyncio
async def test_duplicate_timestamp_fails_closed(monkeypatch):
    payload = _payload({"http": [(0, 1.0), (0, 2.0)]})
    _install(monkeypatch, payload)

    result = await diag.run_if_enabled()

    assert result["ok"] is False
    assert result["reason"] == "duplicate_timestamp"


@pytest.mark.asyncio
async def test_timeout_is_reported_and_redacted(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _install(monkeypatch, None, error=httpx.TimeoutException("slow"))

    result = await diag.run_if_enabled()

    assert result["ok"] is False
    assert result["reason"] == "timeout"
    assert "reason=timeout" in caplog.text
    assert API_KEY not in caplog.text
    assert "Authorization" not in caplog.text


@pytest.mark.asyncio
async def test_request_failure_reports_only_the_exception_type(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _install(monkeypatch, None, error=httpx.ConnectError("cannot reach host"))

    result = await diag.run_if_enabled()

    assert result["ok"] is False
    assert result["reason"] == "request_failed:ConnectError"
    assert "cannot reach host" not in caplog.text


@pytest.mark.asyncio
async def test_http_error_reports_status_only(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _install(
        monkeypatch,
        None,
        status=403,
        chunks=[b'{"error": "denied", "secret": "super-secret-body"}'],
    )

    result = await diag.run_if_enabled()

    assert result["ok"] is False
    assert result["reason"] == "http_status"
    assert result["http_status"] == 403
    assert "http_status=403" in caplog.text
    assert "super-secret-body" not in caplog.text
    assert API_KEY not in caplog.text


@pytest.mark.asyncio
async def test_response_size_is_bounded(monkeypatch):
    _install(monkeypatch, None, chunks=[b"x" * 300_000, b"x" * 300_000])

    result = await diag.run_if_enabled()

    assert result["ok"] is False
    assert result["reason"] == "response_too_large"


@pytest.mark.asyncio
async def test_invalid_json_is_reported(monkeypatch):
    _install(monkeypatch, None, chunks=[b"not json"])

    result = await diag.run_if_enabled()

    assert result["ok"] is False
    assert result["reason"] == "invalid_json"


@pytest.mark.asyncio
async def test_missing_api_key_fails_without_a_request(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    monkeypatch.setenv(diag.ENABLE_ENV, "1")
    monkeypatch.delenv("RENDER_API_KEY", raising=False)
    monkeypatch.setenv("RENDER_SERVICE_ID", SERVICE_ID)
    monkeypatch.setattr(diag.httpx, "AsyncClient", _Client)

    result = await diag.run_if_enabled()

    assert result["ok"] is False
    assert result["reason"] == "missing_api_key"
    assert _Client.calls == []
    assert "missing_api_key" in caplog.text


@pytest.mark.asyncio
async def test_missing_service_id_fails_without_a_request(monkeypatch):
    monkeypatch.setenv(diag.ENABLE_ENV, "1")
    monkeypatch.setenv("RENDER_API_KEY", API_KEY)
    monkeypatch.delenv("RENDER_SERVICE_ID", raising=False)
    monkeypatch.setattr(diag.httpx, "AsyncClient", _Client)

    result = await diag.run_if_enabled()

    assert result["ok"] is False
    assert result["reason"] == "missing_service_id"
    assert _Client.calls == []


@pytest.mark.asyncio
async def test_unsupported_unit_keeps_raw_values_without_inventing_bytes(
    monkeypatch, caplog
):
    caplog.set_level(logging.INFO)
    _install(monkeypatch, _payload(SPLIT, unit="furlongs"))

    result = await diag.run_if_enabled()

    assert result["ok"] is True
    http = result["sources"]["http"]
    assert http["unit"] == "furlongs"
    assert http["unit_supported"] is False
    assert http["raw_total"] == pytest.approx(170.0)
    assert http["bytes_total"] is None
    assert http["mb_total"] is None
    assert http["gb_total"] is None
    assert "bytes_total=unavailable" in caplog.text


@pytest.mark.asyncio
async def test_absent_unit_is_reported_as_assumed_bytes(monkeypatch):
    _install(monkeypatch, _payload({"http": [(0, 300.0)]}, unit=None))

    result = await diag.run_if_enabled()

    http = result["sources"]["http"]
    assert http["unit"] == "unspecified"
    assert http["unit_assumed_bytes"] is True
    assert http["raw_total"] == pytest.approx(300.0)
    assert http["bytes_total"] == 300
    assert http["mb_total"] == pytest.approx(0.0003)


@pytest.mark.asyncio
async def test_points_outside_the_window_are_excluded_from_aggregates(monkeypatch):
    _install(
        monkeypatch,
        _payload({"http": [(0, 10.0), (-1, 999.0), (144, 999.0)]}),
    )

    result = await diag.run_if_enabled()

    http = result["sources"]["http"]
    assert http["points"] == 1
    assert http["outside_window_points"] == 2
    assert http["raw_total"] == pytest.approx(10.0)


@pytest.mark.asyncio
async def test_hourly_values_and_units_are_logged(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _install(monkeypatch, _payload(SPLIT))

    result = await diag.run_if_enabled()

    assert result["ok"] is True
    assert "source=http unit=mb" in caplog.text
    assert "2026-11-07T00:00:00Z=60" in caplog.text
    assert "2026-11-12T23:00:00Z=110" in caplog.text
    assert "completeness expected_hours=144" in caplog.text
    assert "parts_match_total=True" in caplog.text


@pytest.mark.asyncio
async def test_api_key_never_reaches_the_logs(monkeypatch, caplog):
    caplog.set_level(logging.INFO)
    _install(monkeypatch, _payload(SPLIT))

    result = await diag.run_if_enabled()

    assert result["ok"] is True
    assert API_KEY not in caplog.text
    assert "Authorization" not in caplog.text
    assert "Bearer" not in caplog.text


@pytest.mark.asyncio
async def test_entry_point_runs_the_diagnostic_once_before_the_supervisor(monkeypatch):
    import backend.main as entry

    events: list[str] = []

    async def _diagnostic():
        events.append("diagnostic")

    class _FakeSupervisor:
        def __init__(self, cfg):
            self.cfg = cfg
            self.shutdown_event = asyncio.Event()
            self.shutdown_event.set()

        async def start(self):
            events.append("supervisor-start")

        async def stop(self):
            events.append("supervisor-stop")

    monkeypatch.setattr(entry.cfg_module, "load", lambda: {})
    monkeypatch.setattr(entry, "RuntimeSupervisor", _FakeSupervisor)
    monkeypatch.setattr(entry, "install_crash_diagnostics", lambda: None)
    monkeypatch.setattr(entry.atexit, "register", lambda fn: None)
    monkeypatch.setattr(diag, "run_if_enabled", _diagnostic)

    await entry.main()

    assert events == ["diagnostic", "supervisor-start", "supervisor-stop"]
