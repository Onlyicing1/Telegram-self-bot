"""Bounded validation of Render's bandwidth-by-traffic-source breakdown.

Documented endpoint (verified against Render's public API reference):
``GET /v1/metrics/bandwidth-sources`` returns::

    {"data": [
      {"labels": {"resource": "srv-...", "trafficSource": "http"},
       "values": [{"timestamp": 1709856000, "value": 100}]}
    ]}

The response carries NO unit field, so its values are reported as
``value_unit=unspecified`` and are never summed with byte measurements from a
different endpoint.  Only the closed traffic-source vocabulary is accepted in a
log line; an unknown value is counted, never printed.

This diagnostic exists because the aggregate metric alone cannot show WHICH
traffic source consumed the bytes.  It is retained (not temporary), runs on the
same bounded heartbeat cadence as the metric refresh, and is a no-op when the
Render API is not configured.
"""
from __future__ import annotations

import asyncio
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

from backend.services import render_bandwidth_service as metric

logger = logging.getLogger(__name__)

DIAGNOSE_INTERVAL_S = 6 * 3600.0
MAX_HOURS = 31 * 24 + 24
_SOURCE_VOCABULARY = frozenset({"total", "http", "websocket", "nat", "privatelink"})
_DOCUMENTED_COMPONENTS = ("http", "websocket", "nat", "privatelink")
_EARLIEST_SOURCES_DATE = datetime(2025, 3, 9, tzinfo=timezone.utc)

_last_summary: dict[str, Any] | None = None
_last_monotonic = 0.0


def _empty(status: str, month: str, detail: str) -> dict[str, Any]:
    return {
        "status": status,
        "month": month,
        "coverage": "unknown",
        "hours": 0,
        "expected_hours": 0,
        "missing_hours": None,
        "duplicates": 0,
        "unknown_source_series": 0,
        "missing_series": ["total", *_DOCUMENTED_COMPONENTS],
        "component_series": 0,
        "sources": {},
        "total_bytes": None,
        "source_sum_bytes": None,
        "difference_bytes": None,
        "reconciliation": "insufficient",
        "value_unit": "unspecified",
        "http_status": None,
        "detail": detail,
    }


def _parse_sources_payload(payload: Any) -> dict[str, Any]:
    if not isinstance(payload, dict) or not isinstance(payload.get("data"), list):
        return {"ok": False, "detail": "not_a_data_object"}
    sources: dict[str, dict[int, float]] = {}
    duplicates = 0
    malformed = 0
    unknown_series = 0
    for entry in payload["data"]:
        if not isinstance(entry, dict):
            return {"ok": False, "detail": "series_not_object"}
        labels = entry.get("labels")
        if not isinstance(labels, dict):
            return {"ok": False, "detail": "labels_malformed"}
        raw_source = labels.get("trafficSource")
        if not isinstance(raw_source, str):
            return {"ok": False, "detail": "traffic_source_missing"}
        source = raw_source.strip().lower()
        if source not in _SOURCE_VOCABULARY:
            unknown_series += 1
            continue
        values = entry.get("values")
        if not isinstance(values, list):
            return {"ok": False, "detail": "values_not_list"}
        points = sources.setdefault(source, {})
        for point in values:
            if not isinstance(point, dict):
                malformed += 1
                continue
            seconds = metric._timestamp_seconds(point.get("timestamp"))
            number = metric._number(point.get("value"))
            if seconds is None or number is None:
                malformed += 1
                continue
            bucket = metric._hour_bucket(seconds)
            if bucket in points:
                duplicates += 1
                continue
            points[bucket] = number
    return {
        "ok": True,
        "detail": "",
        "sources": sources,
        "duplicates": duplicates,
        "malformed": malformed,
        "unknown_source_series": unknown_series,
    }


def _merge(
    target: dict[str, dict[int, float]],
    source: dict[str, dict[int, float]],
) -> int:
    duplicates = 0
    for name, points in source.items():
        destination = target.setdefault(name, {})
        for bucket, value in points.items():
            if bucket in destination:
                duplicates += 1
                continue
            destination[bucket] = value
    return duplicates


def summarize(
    payloads: list[Any],
    *,
    month: str,
    start: datetime,
    now: datetime,
    http_status: int | None = None,
    failures: int = 0,
) -> dict[str, Any]:
    """Validate all fetched pages and reconcile sources against ``total``."""
    if not payloads:
        return _empty("unavailable", month, "no_source_payload")
    sources: dict[str, dict[int, float]] = {}
    duplicates = 0
    malformed = 0
    unknown_series = 0
    parsed_pages = 0
    for payload in payloads:
        parsed = _parse_sources_payload(payload)
        if not parsed.get("ok"):
            return _empty("unexpected_payload", month, str(parsed.get("detail"))[:40])
        parsed_pages += 1
        duplicates += int(parsed["duplicates"])
        malformed += int(parsed["malformed"])
        unknown_series += int(parsed["unknown_source_series"])
        duplicates += _merge(sources, parsed["sources"])

    start_epoch = int(start.timestamp())
    end_epoch = int(now.timestamp())
    sources = {
        name: {
            bucket: value
            for bucket, value in points.items()
            if start_epoch <= bucket <= end_epoch
        }
        for name, points in sources.items()
    }
    all_buckets = {bucket for points in sources.values() for bucket in points}
    coverage = metric._coverage(all_buckets, start, now, max(all_buckets) if all_buckets else None)
    if len(all_buckets) > MAX_HOURS:
        return _empty("too_many_hours", month, "hour_bound_exceeded")

    per_source = {
        name: int(round(sum(points.values()))) for name, points in sorted(sources.items())
    }
    total_points = sources.get("total", {})
    component_names = sorted(name for name in sources if name != "total")
    component_sum_by_hour = {
        bucket: sum(sources[name][bucket] for name in component_names)
        for bucket in all_buckets
        if component_names and all(bucket in sources[name] for name in component_names)
    }
    # An hour is comparable only when EVERY component series and the total
    # series carry it.  A missing series is never treated as zero.
    overlapping = [
        bucket
        for bucket in total_points
        if bucket in component_sum_by_hour
    ]
    difference: int | None = None
    reconciliation = "insufficient"
    if overlapping and component_names:
        total_bytes = sum(total_points[bucket] for bucket in overlapping)
        component_bytes = sum(component_sum_by_hour[bucket] for bucket in overlapping)
        difference = int(round(component_bytes - total_bytes))
        denominator = max(1.0, abs(total_bytes))
        reconciliation = "matches" if abs(difference) / denominator <= 0.01 else "differs"

    missing_series = ["total", *_DOCUMENTED_COMPONENTS]
    missing_series = [name for name in missing_series if name not in sources]
    status = "ok"
    if failures and parsed_pages == 0:
        status = "error"
    elif (
        failures
        or duplicates
        or malformed
        or unknown_series
        or coverage["missing_hours"]
        or not coverage["complete"]
        or "total" not in sources
    ):
        status = "partial"

    return {
        "status": status,
        "month": month,
        "coverage": "complete" if coverage["complete"] else ("partial" if all_buckets else "missing"),
        "hours": len(all_buckets),
        "expected_hours": coverage["expected_hours"],
        "missing_hours": coverage["missing_hours"],
        "duplicates": duplicates,
        "unknown_source_series": unknown_series,
        "missing_series": missing_series,
        "component_series": len(component_names),
        "sources": per_source,
        "total_bytes": per_source.get("total"),
        "source_sum_bytes": int(round(sum(component_sum_by_hour.values()))),
        "difference_bytes": difference,
        "reconciliation": reconciliation,
        "value_unit": "unspecified",
        "http_status": http_status,
        "detail": "" if status == "ok" else ("partial_data" if parsed_pages else "no_data"),
    }


async def diagnose(
    now: datetime | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Fetch and validate the current month's traffic-source breakdown."""
    moment = metric._now(now)
    start, _ = metric.month_bounds(moment)
    month = moment.strftime("%Y-%m")
    if not metric.configured():
        return _empty("unconfigured", month, "render_metric_not_configured")
    if start < _EARLIEST_SOURCES_DATE:
        return _empty("unsupported", month, "source_data_starts_2025_03_09")

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(metric.REQUEST_TIMEOUT_S))
    payloads: list[Any] = []
    failures = 0
    status: int | None = None
    try:
        query_start = max(start, moment - timedelta(days=metric.MAX_LOOKBACK_DAYS))
        for window_start, window_end in metric._windows(query_start, moment):
            fetched = await metric._get_json_bounded(
                client,
                "metrics/bandwidth-sources",
                params={
                    "resource": metric._service_id(),
                    "startTime": window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "endTime": window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            )
            status = fetched.get("status")
            if not fetched.get("ok"):
                failures += 1
                continue
            payloads.append(fetched.get("payload"))
    finally:
        if own_client:
            await client.aclose()

    if not payloads:
        result = _empty("error", month, "no_source_payload")
        result["http_status"] = status
        result["window_failures"] = failures
        return result
    return summarize(
        payloads,
        month=month,
        start=start,
        now=moment,
        http_status=status,
        failures=failures,
    )


def _log(summary: dict[str, Any]) -> None:
    sources = ",".join(
        f"{name}:{value}" for name, value in (summary.get("sources") or {}).items()
    ) or "none"
    logger.info(
        "RENDER_BANDWIDTH_SOURCES status=%s month=%s coverage=%s hours=%s "
        "expected_hours=%s missing_hours=%s duplicates=%d unknown_source_series=%d "
        "sources=%s missing_series=%s reconciliation=%s difference_bytes=%s "
        "value_unit=%s http_status=%s detail=%s",
        summary.get("status"),
        summary.get("month"),
        summary.get("coverage"),
        summary.get("hours"),
        summary.get("expected_hours"),
        summary.get("missing_hours") if summary.get("missing_hours") is not None else "unknown",
        int(summary.get("duplicates") or 0),
        int(summary.get("unknown_source_series") or 0),
        sources,
        ",".join(summary.get("missing_series") or []) or "none",
        summary.get("reconciliation"),
        summary.get("difference_bytes") if summary.get("difference_bytes") is not None else "unknown",
        summary.get("value_unit"),
        summary.get("http_status") if summary.get("http_status") is not None else "none",
        summary.get("detail") or "none",
    )


async def maybe_diagnose(now: datetime | None = None) -> dict[str, Any] | None:
    """Run at most once per bounded interval; never raises into the heartbeat."""
    global _last_summary, _last_monotonic
    if not metric.configured():
        return None
    if _last_summary is not None and (time.monotonic() - _last_monotonic) < DIAGNOSE_INTERVAL_S:
        return None
    try:
        summary = await asyncio.wait_for(
            diagnose(now), timeout=metric.REQUEST_TIMEOUT_S * 2
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        summary = _empty("error", metric._now(now).strftime("%Y-%m"), type(exc).__name__[:40])
    _last_summary = dict(summary)
    _last_monotonic = time.monotonic()
    try:
        _log(summary)
    except Exception:  # noqa: BLE001
        pass
    return summary


def reset_for_tests() -> None:
    global _last_summary, _last_monotonic
    _last_summary = None
    _last_monotonic = 0.0
