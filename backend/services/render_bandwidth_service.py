"""Render's authoritative service bandwidth metric — bounded, validated, honest.

Render's public Metrics API exposes ``GET /v1/metrics/bandwidth`` as a collection
of time series::

    [
      {
        "labels": [{"field": "...", "value": "..."}],
        "values": [{"timestamp": "2026-11-24T10:00:00Z", "value": 1234.0}],
        "unit": "byte"
      }
    ]

The API accepts at most a 30-day lookback, so a 30/31-day calendar month CANNOT
be fetched in one request.  This module stitches non-overlapping 7-day windows
inside the current UTC month and reports ``coverage=partial`` whenever the
month's first day is already outside the API's lookback or any completed hour is
missing.  A partial month is a LOWER BOUND: it may raise the policy tier but it
is never presented as complete and never resets or lowers a durable floor.

No URL, response body or credential is ever logged or returned.
"""
from __future__ import annotations

import asyncio
import json
import logging
import os
import time
from datetime import datetime, timedelta, timezone
from typing import Any

import httpx

logger = logging.getLogger(__name__)

API_BASE = "https://api.render.com/v1"
REQUEST_TIMEOUT_S = 10.0
MAX_RESPONSE_BYTES = 512_000
REFRESH_INTERVAL_S = 1800.0
WINDOW_DAYS = 7
MAX_LOOKBACK_DAYS = 30
MAX_DATA_AGE_S = 3 * 3600
_HOUR = 3600

_BYTE_UNITS = frozenset({"byte", "bytes", "b"})

_last_result: dict[str, Any] | None = None
_last_monotonic = 0.0


def _now(now: datetime | None) -> datetime:
    if now is None:
        return datetime.now(timezone.utc)
    if now.tzinfo is None:
        return now.replace(tzinfo=timezone.utc)
    return now.astimezone(timezone.utc)


def _service_id() -> str:
    """The ONE authorized service: Render's injected current service id."""
    try:
        return (os.getenv("RENDER_SERVICE_ID", "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def _api_key() -> str:
    try:
        return (os.getenv("RENDER_API_KEY", "") or "").strip()
    except Exception:  # noqa: BLE001
        return ""


def configured() -> bool:
    return bool(_service_id() and _api_key())


def refresh_interval_seconds() -> float:
    try:
        value = float(os.getenv("RENDER_BANDWIDTH_REFRESH_SECONDS", "") or REFRESH_INTERVAL_S)
    except (TypeError, ValueError):
        return REFRESH_INTERVAL_S
    return min(6 * 3600.0, max(300.0, value))


def month_bounds(now: datetime | None = None) -> tuple[datetime, datetime]:
    moment = _now(now)
    start = moment.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    return start, moment


def _windows(start: datetime, end: datetime) -> list[tuple[datetime, datetime]]:
    """Non-overlapping windows, each inside Render's 30-day lookback."""
    windows: list[tuple[datetime, datetime]] = []
    cursor = start
    while cursor < end:
        boundary = min(end, cursor + timedelta(days=WINDOW_DAYS))
        windows.append((cursor, boundary))
        cursor = boundary
    return windows


def _timestamp_seconds(raw: Any) -> int | None:
    if isinstance(raw, bool):
        return None
    if isinstance(raw, (int, float)):
        return int(raw)
    if isinstance(raw, str):
        text = raw.strip()
        if not text:
            return None
        if text.isdigit():
            return int(text)
        try:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
        except ValueError:
            return None
        if parsed.tzinfo is None:
            parsed = parsed.replace(tzinfo=timezone.utc)
        return int(parsed.timestamp())
    return None


def _hour_bucket(seconds: int) -> int:
    return int(seconds // _HOUR) * _HOUR


def _iso(seconds: int | None) -> str | None:
    if seconds is None:
        return None
    return datetime.fromtimestamp(seconds, tz=timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _series_identity(labels: Any) -> tuple[tuple[str, str], ...] | None:
    if labels is None:
        return ()
    if isinstance(labels, dict):
        items = labels.items()
    elif isinstance(labels, (list, tuple)):
        pairs = []
        for entry in labels:
            if not isinstance(entry, dict):
                return None
            field = entry.get("field")
            value = entry.get("value")
            if not isinstance(field, str) or not isinstance(value, str):
                return None
            pairs.append((field, value))
        items = pairs
    else:
        return None
    identity = []
    for key, value in items:
        if not isinstance(key, str) or not isinstance(value, str):
            return None
        identity.append((key, value))
    return tuple(sorted(identity))


def _number(value: Any) -> float | None:
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if number != number or number in (float("inf"), float("-inf")) or number < 0:
        return None
    return number


def parse_total_payload(payload: Any) -> dict[str, Any]:
    """Validate the documented bandwidth collection without trusting it."""
    if not isinstance(payload, list):
        return {"ok": False, "detail": "not_a_collection"}
    series_seen: dict[tuple[tuple[str, str], ...], dict[int, float]] = {}
    units: set[str] = set()
    duplicates = 0
    malformed_points = 0
    series_count = 0
    for entry in payload:
        if not isinstance(entry, dict):
            return {"ok": False, "detail": "series_not_object"}
        identity = _series_identity(entry.get("labels", []))
        if identity is None:
            return {"ok": False, "detail": "labels_malformed"}
        unit = entry.get("unit", "")
        units.add(str(unit).strip().casefold())
        values = entry.get("values")
        if not isinstance(values, list):
            return {"ok": False, "detail": "values_not_list"}
        series_count += 1
        points = series_seen.setdefault(identity, {})
        for point in values:
            if not isinstance(point, dict):
                malformed_points += 1
                continue
            seconds = _timestamp_seconds(point.get("timestamp"))
            number = _number(point.get("value"))
            if seconds is None or number is None:
                malformed_points += 1
                continue
            bucket = _hour_bucket(seconds)
            if bucket in points:
                duplicates += 1
                continue
            points[bucket] = number
    unit_valid = len(units) == 1 and next(iter(units)) in _BYTE_UNITS
    return {
        "ok": True,
        "detail": "",
        "series": series_seen,
        "series_count": series_count,
        "unit": next(iter(units)) if len(units) == 1 else "mixed",
        "unit_valid": unit_valid,
        "duplicates": duplicates,
        "malformed_points": malformed_points,
    }


def _merge_series(
    target: dict[tuple[tuple[str, str], ...], dict[int, float]],
    source: dict[tuple[tuple[str, str], ...], dict[int, float]],
) -> int:
    duplicates = 0
    for identity, points in source.items():
        destination = target.setdefault(identity, {})
        for bucket, value in points.items():
            if bucket in destination:
                duplicates += 1
                continue
            destination[bucket] = value
    return duplicates


def _coverage(
    buckets: set[int],
    start: datetime,
    now: datetime,
    latest: int | None,
) -> dict[str, Any]:
    end_floor = now.replace(minute=0, second=0, microsecond=0)
    start_epoch = int(start.timestamp())
    end_epoch = int(end_floor.timestamp())
    expected = max(0, (end_epoch - start_epoch) // _HOUR)
    present = {bucket for bucket in buckets if start_epoch <= bucket < end_epoch}
    missing = max(0, expected - len(present))
    queryable_start = now - timedelta(days=MAX_LOOKBACK_DAYS)
    full_queryable = start >= queryable_start
    missing_start_seconds = (
        0 if full_queryable else max(0, int((queryable_start - start).total_seconds()))
    )
    latest_age = None if latest is None else max(0, int(now.timestamp()) - latest)
    fresh = latest is not None and latest_age is not None and latest_age <= MAX_DATA_AGE_S
    complete = (
        full_queryable
        and missing == 0
        and expected > 0
        and fresh
    )
    return {
        "expected_hours": expected,
        "present_hours": len(present),
        "missing_hours": missing,
        "full_month_queryable": full_queryable,
        "missing_start_seconds": missing_start_seconds,
        "latest_age_seconds": latest_age,
        "fresh": fresh,
        "complete": complete,
    }


async def _get_json_bounded(
    client: httpx.AsyncClient,
    path: str,
    params: dict[str, str],
) -> dict[str, Any]:
    """Fetch one JSON document with bounded size/time; never expose the body."""
    try:
        async with client.stream(
            "GET",
            f"{API_BASE}/{path.lstrip('/')}",
            params=params,
            headers={
                "Authorization": f"Bearer {_api_key()}",
                "Accept": "application/json",
            },
            timeout=httpx.Timeout(REQUEST_TIMEOUT_S),
        ) as response:
            status = response.status_code
            if status != 200:
                return {"ok": False, "status": status, "detail": "http_error"}
            chunks: list[bytes] = []
            total = 0
            async for chunk in response.aiter_bytes():
                if not chunk:
                    continue
                total += len(chunk)
                if total > MAX_RESPONSE_BYTES:
                    return {"ok": False, "status": status, "detail": "response_too_large"}
                chunks.append(chunk)
        return {"ok": True, "status": status, "payload": json.loads(b"".join(chunks))}
    except asyncio.CancelledError:
        raise
    except (httpx.TimeoutException, httpx.HTTPError, ValueError) as exc:
        return {"ok": False, "status": None, "detail": type(exc).__name__}
    except Exception as exc:  # noqa: BLE001 — an unexpected parser failure is reported
        return {"ok": False, "status": None, "detail": type(exc).__name__}


async def refresh_month_metric(
    now: datetime | None = None,
    *,
    client: httpx.AsyncClient | None = None,
) -> dict[str, Any]:
    """Query the current UTC month and return a validated lower bound."""
    moment = _now(now)
    start, _ = month_bounds(moment)
    month = moment.strftime("%Y-%m")
    if not configured():
        result = _unconfigured(month, moment)
        _publish(result)
        return result

    own_client = client is None
    if own_client:
        client = httpx.AsyncClient(timeout=httpx.Timeout(REQUEST_TIMEOUT_S))
    query_start = max(start, moment - timedelta(days=MAX_LOOKBACK_DAYS))
    merged: dict[tuple[tuple[str, str], ...], dict[int, float]] = {}
    units: set[str] = set()
    duplicates = 0
    malformed = 0
    series_count = 0
    windows_ok = 0
    windows_failed = 0
    last_status: int | None = None
    detail = ""
    try:
        for window_start, window_end in _windows(query_start, moment):
            payload = await _get_json_bounded(
                client,
                "metrics/bandwidth",
                params={
                    "resource": _service_id(),
                    "startTime": window_start.strftime("%Y-%m-%dT%H:%M:%SZ"),
                    "endTime": window_end.strftime("%Y-%m-%dT%H:%M:%SZ"),
                },
            )
            last_status = payload.get("status")
            if not payload.get("ok"):
                windows_failed += 1
                detail = str(payload.get("detail") or "http_error")[:40]
                continue
            parsed = parse_total_payload(payload.get("payload"))
            if not parsed.get("ok"):
                windows_failed += 1
                detail = str(parsed.get("detail") or "unexpected_payload")[:40]
                continue
            windows_ok += 1
            duplicates += int(parsed["duplicates"])
            malformed += int(parsed["malformed_points"])
            series_count += int(parsed["series_count"])
            units.add(str(parsed["unit"]))
            duplicates += _merge_series(merged, parsed["series"])
    finally:
        if own_client:
            await client.aclose()

    unit = next(iter(units)) if len(units) == 1 else ("mixed" if units else "")
    unit_valid = len(units) == 1 and unit in _BYTE_UNITS
    start_epoch = int(start.timestamp())
    end_epoch = int(moment.timestamp())
    all_buckets = {
        bucket
        for points in merged.values()
        for bucket in points
        if start_epoch <= bucket <= end_epoch
    }
    latest = max(all_buckets) if all_buckets else None
    known_bytes = int(round(sum(
        value
        for points in merged.values()
        for bucket, value in points.items()
        if start_epoch <= bucket <= end_epoch
    )))
    coverage = _coverage(all_buckets, start, moment, latest)
    trusted = (
        windows_failed == 0
        and windows_ok > 0
        and unit_valid
        and duplicates == 0
        and malformed == 0
        and coverage["complete"]
    )

    if windows_ok == 0:
        status = "error" if windows_failed else "unavailable"
        trusted = False
    elif trusted:
        status = "ok"
    else:
        status = "partial"

    result = {
        "configured": True,
        "status": status,
        "month": month,
        "bytes": known_bytes if all_buckets else None,
        "trusted": trusted,
        "coverage": "complete" if coverage["complete"] else ("partial" if all_buckets else "missing"),
        "unit": unit or "unknown",
        "unit_valid": unit_valid,
        "series": series_count,
        "duplicates": duplicates,
        "malformed_points": malformed,
        "windows": windows_ok + windows_failed,
        "windows_ok": windows_ok,
        "windows_failed": windows_failed,
        "http_status": last_status,
        "latest_data_at": _iso(latest),
        "detail": detail,
        "fetched_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "missing_hours": coverage["missing_hours"],
        "expected_hours": coverage["expected_hours"],
        "full_month_queryable": coverage["full_month_queryable"],
        "missing_start_seconds": coverage["missing_start_seconds"],
        "latest_age_seconds": coverage["latest_age_seconds"],
    }
    _publish(result)
    if result.get("bytes") is not None:
        # Persist Render's lower bound even when no transfer succeeds, so a
        # restart cannot drop below a tier this month already reached.
        try:
            from backend.services import outbound_bandwidth_service as governor

            await asyncio.to_thread(
                governor.persist_render_floor, month, int(result["bytes"])
            )
        except asyncio.CancelledError:
            raise
        except Exception:  # noqa: BLE001
            pass
    return result


def _unconfigured(month: str, moment: datetime) -> dict[str, Any]:
    return {
        "configured": False,
        "status": "unconfigured",
        "month": month,
        "bytes": None,
        "trusted": False,
        "coverage": "unconfigured",
        "unit": "unknown",
        "unit_valid": False,
        "series": 0,
        "duplicates": 0,
        "malformed_points": 0,
        "windows": 0,
        "windows_ok": 0,
        "windows_failed": 0,
        "http_status": None,
        "latest_data_at": None,
        "detail": "render_metric_not_configured",
        "fetched_at": moment.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "missing_hours": None,
        "expected_hours": None,
        "full_month_queryable": False,
        "missing_start_seconds": None,
        "latest_age_seconds": None,
    }


def _publish(result: dict[str, Any]) -> None:
    """Hand the validated lower bound to the ONE governor (never a second store)."""
    try:
        from backend.services import outbound_bandwidth_service as governor

        governor.set_render_month_metric(
            str(result.get("month") or ""),
            result.get("bytes"),
            trusted=bool(result.get("trusted")),
            coverage=str(result.get("coverage") or "unknown"),
            latest_data_at=result.get("latest_data_at"),
        )
    except Exception:  # noqa: BLE001 — metric publication must never break refresh
        pass


def last_result() -> dict[str, Any] | None:
    return dict(_last_result) if _last_result is not None else None


async def maybe_refresh(now: datetime | None = None) -> dict[str, Any] | None:
    """Refresh at the bounded cadence from the existing heartbeat. Never raises."""
    global _last_result, _last_monotonic
    if not configured():
        return None
    moment = _now(now)
    if _last_result is not None and (time.monotonic() - _last_monotonic) < refresh_interval_seconds():
        return None
    try:
        result = await asyncio.wait_for(
            refresh_month_metric(moment), timeout=REQUEST_TIMEOUT_S * 2
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:  # noqa: BLE001
        result = {
            "configured": True,
            "status": "error",
            "month": moment.strftime("%Y-%m"),
            "bytes": None,
            "trusted": False,
            "coverage": "error",
            "detail": type(exc).__name__[:40],
            "latest_data_at": None,
            "missing_hours": None,
            "duplicates": 0,
            "unit": "unknown",
            "http_status": None,
            "windows_failed": 0,
        }
    _last_result = dict(result)
    _last_monotonic = time.monotonic()
    try:
        logger.info(
            "RENDER_BANDWIDTH_METRIC status=%s month=%s bytes=%s trusted=%s "
            "coverage=%s unit=%s latest=%s missing_hours=%s duplicates=%d "
            "windows=%s/%s http_status=%s detail=%s",
            result.get("status"),
            result.get("month"),
            result.get("bytes") if result.get("bytes") is not None else "unknown",
            "true" if result.get("trusted") else "false",
            result.get("coverage"),
            result.get("unit"),
            result.get("latest_data_at") or "unknown",
            result.get("missing_hours") if result.get("missing_hours") is not None else "unknown",
            int(result.get("duplicates") or 0),
            result.get("windows_ok", 0),
            result.get("windows", 0),
            result.get("http_status") if result.get("http_status") is not None else "none",
            result.get("detail") or "none",
        )
    except Exception:  # noqa: BLE001
        pass

    # The source breakdown is a separate, bounded diagnostic (retained, not
    # silently deployed): its own cadence and its own one-line summary.
    try:
        from backend.services import render_bandwidth_sources_diagnostic as sources

        await sources.maybe_diagnose(moment)
    except asyncio.CancelledError:
        raise
    except Exception:  # noqa: BLE001 — a diagnostic never breaks the metric
        pass
    return result


def reset_for_tests() -> None:
    global _last_result, _last_monotonic
    _last_result = None
    _last_monotonic = 0.0
