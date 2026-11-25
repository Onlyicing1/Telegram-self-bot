"""Egress attribution — bounded counters, lifecycle flushing, HTTP seams.

Covers the real defects this instrumentation must not repeat:

  * no unbounded label dimension (unknown sources are refused);
  * no per-request logging and no sensitive value in an aggregate line;
  * enabled/disabled/idle/partial/failure states are distinguishable;
  * exact and estimated bytes stay separate;
  * inbound Telegram downloads never enter outbound reconciliation;
  * request bodies, non-streaming responses and CONSUMED streaming responses
    are measured; an unconsumed stream is unmeasured, never a fake zero.
"""
from __future__ import annotations

import logging
import time
from datetime import datetime, timezone

import httpx
import pytest

from backend.observability import egress_attribution as egress

UTC = timezone.utc
NOW = datetime(2026, 11, 23, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _clean(monkeypatch):
    monkeypatch.setenv(egress.ENV_FLAG, "1")
    egress.reset_for_tests()
    egress.uninstall_httpx_instrumentation()
    yield
    egress.uninstall_httpx_instrumentation()
    egress.reset_for_tests()


# ── 1. closed vocabulary / disabled ──


def test_unknown_source_is_refused_and_never_becomes_a_label():
    assert egress.record("https://example.test/?token=SECRET", 10) is False
    assert egress.record_unmeasured("filename.pdf") is False
    assert egress.snapshot()["sources"] == {}


def test_disabled_attribution_records_nothing(monkeypatch):
    monkeypatch.delenv(egress.ENV_FLAG, raising=False)
    assert egress.enabled() is False
    assert egress.record("telegram_upload", 10) is False
    assert egress.install_httpx_instrumentation() is False
    snap = egress.snapshot(now=NOW)
    assert snap["status"] == "disabled"
    assert snap["enabled"] is False
    assert snap["measured_bytes"] == 0


def test_zero_length_operation_is_not_a_byte_measurement():
    assert egress.record("telegram_upload", 0) is False
    assert egress.record_operation("supabase_rest", 0) is True
    source = egress.snapshot()["sources"]["supabase_rest"]
    assert source == {"bytes": 0, "ops": 1, "unmeasured_ops": 0}


# ── 2. states / exactness / rollover ──


def test_enabled_with_no_activity_is_idle():
    assert egress.snapshot()["status"] == "enabled_idle"
    assert egress.snapshot()["coverage"] == "unknown"


def test_enabled_with_activity_is_recording_and_complete():
    egress.record("telegram_upload", 100)
    snap = egress.snapshot()
    assert snap["status"] == "enabled_recording"
    assert snap["coverage"] == "complete"
    assert snap["measured_outbound_bytes"] == 100


def test_estimated_bytes_are_kept_separate():
    egress.record("telegram_upload", 100)
    egress.record("telegram_upload", 50, estimated=True)
    snap = egress.snapshot()
    assert snap["measured_bytes"] == 150
    assert snap["estimated_bytes"] == 50


def test_measurement_failure_is_a_distinct_state(monkeypatch):
    def _boom(*args, **kwargs):
        raise RuntimeError("boom")

    monkeypatch.setattr(egress, "_positive_int", _boom)
    assert egress.record("telegram_upload", 10) is False
    assert egress.snapshot()["status"] == "measurement_failure"


def test_unmeasured_operation_makes_coverage_partial():
    egress.record("telegram_upload", 10)
    egress.record_unmeasured("telegram_transport")
    snap = egress.snapshot()
    assert snap["coverage"] == "partial"
    assert snap["unmeasured_operations"] == 1


def test_month_rollover_resets_bounded_counters():
    egress.record("telegram_upload", 100)
    december = datetime(2026, 12, 2, 0, 0, tzinfo=UTC)
    snap = egress.snapshot(now=december)
    assert snap["month"] == "2026-12"
    assert snap["measured_bytes"] == 0


# ── 3. flush / startup / shutdown ──


def test_flush_emits_one_bounded_summary_and_resets_the_interval(caplog):
    caplog.set_level(logging.INFO, logger="backend.observability.egress_attribution")
    egress.record("telegram_upload", 1000)
    egress.record("telegram_download", 2000)
    assert egress.flush(now=NOW) is True
    text = caplog.text
    assert "EGRESS_ATTRIBUTION_SUMMARY" in text
    assert "status=enabled_recording" in text
    assert "measured_outbound_bytes=1000" in text
    assert "measured_inbound_bytes=2000" in text
    assert "telegram_upload:1000/1/u0" in text
    assert "sources=none" not in text
    # Counters stay month-to-date; only the interval marker resets.
    assert egress.snapshot()["measured_bytes"] == 3000
    assert egress.flush_due() is False


def test_flush_due_is_time_based(monkeypatch):
    assert egress.flush_due() is False
    monkeypatch.setattr(egress, "_last_flush", time.monotonic() - 10_000)
    assert egress.flush_due() is True


def test_startup_confirmation_is_sanitized_and_once(caplog):
    caplog.set_level(logging.INFO, logger="backend.observability.egress_attribution")
    egress.log_startup()
    egress.log_startup()
    lines = [line for line in caplog.text.splitlines() if "EGRESS_ATTRIBUTION_STARTUP" in line]
    assert len(lines) == 1
    assert "status=enabled" in lines[0]
    assert "wire_overhead=false" in lines[0]


def test_startup_reports_disabled(monkeypatch, caplog):
    monkeypatch.delenv(egress.ENV_FLAG, raising=False)
    caplog.set_level(logging.INFO, logger="backend.observability.egress_attribution")
    egress.log_startup()
    assert "EGRESS_ATTRIBUTION_STARTUP status=disabled" in caplog.text


def test_reconciliation_line_compares_render_and_measured(caplog):
    caplog.set_level(logging.INFO, logger="backend.observability.egress_attribution")
    egress.record("telegram_upload", 400)
    egress.flush(
        policy={
            "render_bytes": 1000,
            "render_trusted": False,
            "render_coverage": "partial",
        }
    )
    assert "EGRESS_RECONCILIATION" in caplog.text
    assert "render_outbound_bytes=1000" in caplog.text
    assert "measured_outbound_bytes=400" in caplog.text
    assert "unattributed_difference=600" in caplog.text
    assert "difference_is_not_attribution=true" in caplog.text


def test_policy_line_carries_bounded_fields_only(caplog):
    caplog.set_level(logging.INFO, logger="backend.observability.egress_attribution")
    egress.flush(
        policy={
            "month": "2026-11",
            "tier": 3,
            "effective_bytes": 2_800_000_000,
            "available": True,
            "denials": "scheduled_media:2",
        }
    )
    assert "BANDWIDTH_POLICY_SNAPSHOT" in caplog.text
    assert "tier=3" in caplog.text
    assert "denials=scheduled_media:2" in caplog.text


# ── 4. reconciliation ──


def test_reconciliation_separates_inbound_and_labels_the_difference():
    egress.record("telegram_upload", 100)
    egress.record("telegram_download", 500)
    egress.record("ai_provider", 50, estimated=True)
    result = egress.reconciliation(1000, render_trusted=False, render_coverage="partial")
    assert result["measured_outbound_bytes"] == 150
    assert result["measured_inbound_bytes"] == 500
    assert result["estimated_bytes"] == 50
    assert result["unattributed_difference"] == 850
    assert result["difference_is_not_attribution"] is True


def test_reconciliation_without_a_render_metric_has_no_difference():
    egress.record("telegram_upload", 100)
    assert egress.reconciliation(None)["unattributed_difference"] is None


# ── 5. Telegram download boundary ──


def test_repeated_downloads_of_the_same_payload_are_each_counted(tmp_path):
    path = tmp_path / "media.bin"
    path.write_bytes(b"x" * 10)
    egress.record_telegram_download(str(path))
    egress.record_telegram_download(str(path))
    assert egress.snapshot()["sources"]["telegram_download"]["ops"] == 2
    assert egress.snapshot()["sources"]["telegram_download"]["bytes"] == 20


def test_download_path_is_measured_once(tmp_path):
    path = tmp_path / "media.bin"
    path.write_bytes(b"x" * 321)
    assert egress.record_telegram_download(str(path)) is True
    assert egress.snapshot()["sources"]["telegram_download"] == {
        "bytes": 321, "ops": 1, "unmeasured_ops": 0,
    }


def test_missing_download_path_is_unmeasured_not_zero():
    assert egress.record_telegram_download("/nonexistent/file.bin") is True
    snap = egress.snapshot()["sources"]["telegram_download"]
    assert snap["bytes"] == 0
    assert snap["unmeasured_ops"] == 1


# ── 6. HTTP: request/response/streaming ──


def _client(handler) -> httpx.AsyncClient:
    assert egress.install_httpx_instrumentation() is True
    return httpx.AsyncClient(transport=httpx.MockTransport(handler))


@pytest.mark.asyncio
async def test_non_streaming_response_body_is_measured():
    async def handler(request):
        return httpx.Response(200, content=b"r" * 100)

    async with _client(handler) as client:
        await client.get("https://unknown.example.test/data")
    source = egress.snapshot()["sources"]["other_http"]
    assert source == {"bytes": 100, "ops": 1, "unmeasured_ops": 0}


@pytest.mark.asyncio
async def test_request_body_and_response_body_are_both_measured():
    async def handler(request):
        return httpx.Response(200, content=b"r" * 40)

    async with _client(handler) as client:
        await client.post("https://unknown.example.test/upload", content=b"q" * 60)
    source = egress.snapshot()["sources"]["other_http"]
    assert source["bytes"] == 100
    assert source["ops"] == 1


@pytest.mark.asyncio
async def test_streaming_response_is_counted_only_as_the_caller_consumes_it():
    async def handler(request):
        async def body():
            yield b"a" * 10
            yield b"b" * 20

        return httpx.Response(200, content=body())

    async with _client(handler) as client:
        async with client.stream("GET", "https://unknown.example.test/stream") as response:
            assert egress.snapshot()["sources"].get("other_http", {}).get("bytes", 0) == 0
            chunks = [chunk async for chunk in response.aiter_bytes()]
    assert b"".join(chunks) == b"a" * 10 + b"b" * 20
    source = egress.snapshot()["sources"]["other_http"]
    assert source["bytes"] == 30
    assert source["ops"] == 1


@pytest.mark.asyncio
async def test_unconsumed_stream_is_unmeasured_never_a_fake_zero():
    async def handler(request):
        async def body():
            yield b"a" * 10

        return httpx.Response(200, content=body())

    async with _client(handler) as client:
        async with client.stream("GET", "https://unknown.example.test/stream"):
            pass
    source = egress.snapshot()["sources"]["other_http"]
    assert source["bytes"] == 0
    assert source["unmeasured_ops"] == 1


@pytest.mark.asyncio
async def test_failed_request_counts_the_sent_body_and_no_response():
    async def handler(request):
        raise httpx.ConnectError("nope", request=request)

    async with _client(handler) as client:
        with pytest.raises(httpx.ConnectError):
            await client.post("https://unknown.example.test/x", content=b"q" * 25)
    source = egress.snapshot()["sources"]["other_http"]
    assert source["bytes"] == 25
    assert source["unmeasured_ops"] == 1


@pytest.mark.asyncio
async def test_classification_is_by_bounded_source_never_the_url(monkeypatch):
    monkeypatch.setenv("SUPABASE_URL", "https://db.example.test")

    async def handler(request):
        return httpx.Response(200, content=b"x" * 5)

    async with _client(handler) as client:
        await client.get("https://db.example.test/rest/v1/saved_items?select=*")
        await client.get("https://db.example.test/storage/v1/object/bucket/key")
        await client.get("https://api.openai.com/v1/chat/completions")
        await client.get("https://api.openai.com/v1/audio/transcriptions")
        await client.get("https://ydc-index.io/v1/search")
        await client.get("https://api.render.com/v1/metrics/bandwidth")
    sources = egress.snapshot()["sources"]
    assert sources["supabase_rest"]["bytes"] == 5
    assert sources["supabase_storage"]["bytes"] == 5
    assert sources["ai_provider"]["bytes"] == 5
    assert sources["stt_provider"]["bytes"] == 5
    assert sources["ai_search"]["bytes"] == 5
    assert sources["render_api"]["bytes"] == 5


@pytest.mark.asyncio
async def test_unknown_length_request_body_is_unmeasured_never_zero():
    async def handler(request):
        return httpx.Response(200, content=b"ok")

    async def body():
        yield b"a" * 10

    async with _client(handler) as client:
        await client.post("https://unknown.example.test/x", content=body())
    source = egress.snapshot()["sources"]["other_http"]
    assert source["unmeasured_ops"] == 1
    assert source["bytes"] == 2


@pytest.mark.asyncio
async def test_repeated_operations_are_counted_separately():
    async def handler(request):
        return httpx.Response(200, content=b"r" * 10)

    async with _client(handler) as client:
        await client.get("https://unknown.example.test/a")
        await client.get("https://unknown.example.test/a")
    source = egress.snapshot()["sources"]["other_http"]
    assert source == {"bytes": 20, "ops": 2, "unmeasured_ops": 0}


@pytest.mark.asyncio
async def test_no_sensitive_value_reaches_a_log_line(caplog):
    caplog.set_level(logging.INFO, logger="backend.observability.egress_attribution")

    async def handler(request):
        return httpx.Response(200, content=b"SECRET-BODY")

    async with _client(handler) as client:
        await client.get("https://unknown.example.test/path?token=SECRET-TOKEN")
    egress.flush()
    assert "SECRET-TOKEN" not in caplog.text
    assert "SECRET-BODY" not in caplog.text
    assert "unknown.example.test" not in caplog.text


def test_instrumentation_is_idempotent_and_uninstallable():
    assert egress.install_httpx_instrumentation() is True
    assert egress.install_httpx_instrumentation() is True
    egress.uninstall_httpx_instrumentation()
    egress.uninstall_httpx_instrumentation()
