"""TEMPORARY read-only diagnostic: Render bandwidth split by traffic source.

This is the Tier 0 measurement described in ``INVESTIGATION.md`` section 11.5.
It is disabled unless ``RENDER_BANDWIDTH_SOURCES_DIAGNOSTIC`` is exactly
``1``, and then runs ONCE per process startup: one bounded GET to
``/v1/metrics/bandwidth-sources`` for the Render-injected
``RENDER_SERVICE_ID`` only. It writes nothing anywhere and never logs the API
key, the Authorization header, raw headers, or the raw response body.

Delete this module (and its single call site in ``backend/main.py``) once the
measurement has been taken.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, ROUND_HALF_UP
from typing import Any

import httpx

logger = logging.getLogger(__name__)

ENABLE_ENV = "RENDER_BANDWIDTH_SOURCES_DIAGNOSTIC"
_API_URL = "https://api.render.com/v1/metrics/bandwidth-sources"
_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
_MAX_RESPONSE_BYTES = 512 * 1024
_HOURLY_LOG_CHUNK = 24
_WINDOW_START = datetime(2026, 11, 7, 0, 0, tzinfo=timezone.utc)
_WINDOW_END = datetime(2026, 11, 13, 0, 0, tzinfo=timezone.utc)
_EXPECTED_HOURS = int((_WINDOW_END - _WINDOW_START).total_seconds() // 3600)
_TRAFFIC_SOURCES = ("total", "http", "websocket", "nat", "privatelink")
_PART_SOURCES = ("http", "websocket", "nat", "privatelink")
_UNIT_BYTES = {
    "b": Decimal(1),
    "byte": Decimal(1),
    "bytes": Decimal(1),
    "kb": Decimal(1_000),
    "mb": Decimal(1_000_000),
    "gb": Decimal(1_000_000_000),
    "tb": Decimal(1_000_000_000_000),
}


class _PayloadError(ValueError):
    """A sanitized, fixed-vocabulary parse failure."""

    def __init__(self, reason: str) -> None:
        self.reason = reason
        super().__init__(reason)


def is_enabled() -> bool:
    """The diagnostic runs only for the exact opt-in value ``1``."""
    return os.getenv(ENABLE_ENV) == "1"


def _iso(value: datetime) -> str:
    return value.astimezone(timezone.utc).isoformat(timespec="seconds").replace(
        "+00:00", "Z"
    )


def _failed(reason: str, *, service_id: str = "", http_status: int | None = None):
    return {
        "ran": True,
        "ok": False,
        "reason": reason,
        "service_id": service_id,
        "http_status": http_status,
    }


def _parse_timestamp(value: Any) -> datetime:
    if isinstance(value, bool) or value is None:
        raise ValueError("invalid timestamp")
    if isinstance(value, (int, float)):
        seconds = float(value)
        if not math.isfinite(seconds):
            raise ValueError("invalid timestamp")
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError) as exc:
            raise ValueError("invalid timestamp") from exc
    if isinstance(value, str):
        text = value.strip()
        if not text:
            raise ValueError("invalid timestamp")
        try:
            seconds = float(text)
        except ValueError:
            parsed = datetime.fromisoformat(text.replace("Z", "+00:00"))
            if parsed.tzinfo is None:
                raise ValueError("timestamp has no timezone")
            return parsed.astimezone(timezone.utc)
        if not math.isfinite(seconds):
            raise ValueError("invalid timestamp")
        try:
            return datetime.fromtimestamp(seconds, tz=timezone.utc)
        except (OverflowError, OSError) as exc:
            raise ValueError("invalid timestamp") from exc
    raise ValueError("invalid timestamp")


def _parse_value(value: Any) -> float:
    if isinstance(value, bool) or value is None:
        raise ValueError("invalid value")
    if isinstance(value, (int, float)):
        number = float(value)
    elif isinstance(value, str):
        number = float(value.strip())
    else:
        raise ValueError("invalid value")
    if not math.isfinite(number) or number < 0:
        raise ValueError("invalid value")
    return number


def _parse_series(entry: Any, *, service_id: str) -> tuple[str, dict[str, Any]]:
    if not isinstance(entry, dict):
        raise _PayloadError("malformed_series")
    labels = entry.get("labels")
    if not isinstance(labels, dict):
        raise _PayloadError("malformed_series")
    resource = labels.get("resource")
    if not isinstance(resource, str) or not resource.strip():
        raise _PayloadError("malformed_series")
    if resource != service_id:
        raise _PayloadError("resource_mismatch")
    source = labels.get("trafficSource")
    if not isinstance(source, str) or not source.strip():
        raise _PayloadError("malformed_series")
    source = source.strip()

    values = entry.get("values")
    if not isinstance(values, list):
        raise _PayloadError("malformed_series")

    raw_unit = entry.get("unit")
    if raw_unit is None or (isinstance(raw_unit, str) and not raw_unit.strip()):
        unit, multiplier, unit_assumed, unit_supported = (
            "unspecified",
            Decimal(1),
            True,
            True,
        )
    else:
        unit = str(raw_unit).strip().casefold()
        multiplier = _UNIT_BYTES.get(unit)
        unit_assumed = False
        unit_supported = multiplier is not None

    hourly: list[tuple[datetime, float]] = []
    buckets: set[int] = set()
    seen: set[datetime] = set()
    outside_window = 0
    raw_total = Decimal(0)
    for point in values:
        if not isinstance(point, dict):
            raise _PayloadError("malformed_datapoint")
        try:
            observed_at = _parse_timestamp(point.get("timestamp"))
        except (ValueError, TypeError):
            raise _PayloadError("invalid_timestamp")
        try:
            amount = _parse_value(point.get("value"))
        except (ValueError, TypeError):
            raise _PayloadError("invalid_value")
        if observed_at in seen:
            raise _PayloadError("duplicate_timestamp")
        seen.add(observed_at)
        if not (_WINDOW_START <= observed_at < _WINDOW_END):
            outside_window += 1
            continue
        buckets.add(int((observed_at - _WINDOW_START).total_seconds() // 3600))
        hourly.append((observed_at, amount))
        raw_total += Decimal(str(amount))

    bytes_total: int | None = None
    mb_total: float | None = None
    gb_total: float | None = None
    if unit_supported and multiplier is not None:
        bytes_total = int(
            (raw_total * multiplier).to_integral_value(rounding=ROUND_HALF_UP)
        )
        mb_total = round(bytes_total / 1_000_000, 6)
        gb_total = round(bytes_total / 1_000_000_000, 9)

    hourly.sort(key=lambda item: item[0])
    return source, {
        "unit": unit,
        "unit_assumed_bytes": unit_assumed,
        "unit_supported": unit_supported,
        "points": len(hourly),
        "has_data": bool(hourly),
        "outside_window_points": outside_window,
        "missing_hours": max(0, _EXPECTED_HOURS - len(buckets)),
        "raw_total": float(raw_total),
        "bytes_total": bytes_total,
        "mb_total": mb_total,
        "gb_total": gb_total,
        "hourly": [(item[0], item[1]) for item in hourly],
        "_buckets": buckets,
    }


def _parse_payload(payload: Any, *, service_id: str) -> dict[str, Any]:
    if not isinstance(payload, dict):
        return _failed("unexpected_payload", service_id=service_id)
    data = payload.get("data")
    if not isinstance(data, list):
        return _failed("unexpected_payload", service_id=service_id)
    if not data:
        return _failed("no_data", service_id=service_id)

    sources: dict[str, dict[str, Any]] = {}
    for entry in data:
        try:
            source, parsed = _parse_series(entry, service_id=service_id)
        except _PayloadError as exc:
            return _failed(exc.reason, service_id=service_id)
        if source in sources:
            return _failed("duplicate_traffic_source", service_id=service_id)
        sources[source] = parsed

    if sum(series["points"] for series in sources.values()) == 0:
        return _failed("no_data", service_id=service_id)

    bucket_union: set[int] = set()
    for series in sources.values():
        bucket_union.update(series.pop("_buckets"))

    parts_present = [name for name in _PART_SOURCES if name in sources]
    total_present = "total" in sources
    total_raw = sources["total"]["raw_total"] if total_present else None
    parts_sum_raw = (
        sum(sources[name]["raw_total"] for name in parts_present)
        if parts_present
        else None
    )
    mixed_units = len({series["unit"] for series in sources.values()}) > 1
    if total_raw is None or parts_sum_raw is None or mixed_units:
        parts_match: bool | None = None
    else:
        tolerance = max(1.0, abs(total_raw) * 0.005)
        parts_match = abs(parts_sum_raw - total_raw) <= tolerance

    hours_with_any_data = len(bucket_union)
    return {
        "ran": True,
        "ok": True,
        "reason": "",
        "service_id": service_id,
        "window_start": _iso(_WINDOW_START),
        "window_end": _iso(_WINDOW_END),
        "expected_hours": _EXPECTED_HOURS,
        "declared_series": len(data),
        "series_count": len(sources),
        "hours_with_any_data": hours_with_any_data,
        "missing_hours": max(0, _EXPECTED_HOURS - hours_with_any_data),
        "total_present": total_present,
        "parts_present": parts_present,
        "parts_sum_raw": parts_sum_raw,
        "total_raw": total_raw,
        "parts_match_total": parts_match,
        "mixed_units": mixed_units,
        "unexpected_sources": sorted(set(sources) - set(_TRAFFIC_SOURCES)),
        "sources": sources,
    }


async def _fetch(api_key: str, params: dict[str, str]) -> tuple[int | None, Any, str]:
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }
    status: int | None = None
    async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
        async with client.stream(
            "GET", _API_URL, params=params, headers=headers
        ) as response:
            status = response.status_code
            if status != 200:
                return status, None, "http_status"
            body = bytearray()
            async for chunk in response.aiter_bytes():
                body.extend(chunk)
                if len(body) > _MAX_RESPONSE_BYTES:
                    return status, None, "response_too_large"
    try:
        return status, json.loads(bytes(body)), ""
    except (ValueError, TypeError):
        return status, None, "invalid_json"


def _fmt(value: float | None, digits: int) -> str:
    return "unavailable" if value is None else f"{value:.{digits}f}"


def _log_result(result: dict[str, Any]) -> None:
    window = f"{_iso(_WINDOW_START)}..{_iso(_WINDOW_END)}"
    if not result.get("ok"):
        logger.warning(
            "[BANDWIDTH_SOURCES] failed service=%s window=%s reason=%s http_status=%s",
            result.get("service_id") or "missing",
            window,
            result.get("reason") or "unknown",
            result.get("http_status") if result.get("http_status") is not None else "none",
        )
        return

    logger.info(
        "[BANDWIDTH_SOURCES] response http_status=%s series=%d expected_hours=%d",
        result.get("http_status"),
        result["series_count"],
        result["expected_hours"],
    )
    for name, series in result["sources"].items():
        bytes_total = (
            "unavailable" if series["bytes_total"] is None else series["bytes_total"]
        )
        logger.info(
            "[BANDWIDTH_SOURCES] source=%s unit=%s unit_assumed_bytes=%s points=%d "
            "missing_hours=%d outside_window_points=%d raw_total=%.6f "
            "bytes_total=%s mb=%s gb=%s",
            name,
            series["unit"],
            series["unit_assumed_bytes"],
            series["points"],
            series["missing_hours"],
            series["outside_window_points"],
            series["raw_total"],
            bytes_total,
            _fmt(series["mb_total"], 6),
            _fmt(series["gb_total"], 9),
        )
        hourly = series["hourly"]
        for start in range(0, len(hourly), _HOURLY_LOG_CHUNK):
            chunk = hourly[start:start + _HOURLY_LOG_CHUNK]
            rendered = " ".join(
                f"{_iso(observed_at)}={amount:g}" for observed_at, amount in chunk
            )
            logger.info(
                "[BANDWIDTH_SOURCES] source=%s hourly %d-%d %s",
                name,
                start + 1,
                start + len(chunk),
                rendered,
            )

    logger.info(
        "[BANDWIDTH_SOURCES] completeness expected_hours=%d series=%d "
        "hours_with_any_data=%d missing_hours=%d total_present=%s "
        "parts_present=%s parts_sum_raw=%s total_raw=%s parts_match_total=%s "
        "mixed_units=%s unexpected_sources=%s",
        result["expected_hours"],
        result["series_count"],
        result["hours_with_any_data"],
        result["missing_hours"],
        result["total_present"],
        ",".join(result["parts_present"]) or "none",
        _fmt(result["parts_sum_raw"], 6),
        _fmt(result["total_raw"], 6),
        result["parts_match_total"],
        result["mixed_units"],
        ",".join(result["unexpected_sources"]) or "none",
    )


async def run_if_enabled() -> dict[str, Any]:
    """Run at most one bounded read-only request, only when explicitly enabled."""
    if not is_enabled():
        return {"ran": False, "ok": False, "reason": "disabled"}

    api_key = os.getenv("RENDER_API_KEY", "").strip()
    service_id = os.getenv("RENDER_SERVICE_ID", "").strip()
    if not api_key:
        result = _failed("missing_api_key", service_id=service_id)
        _log_result(result)
        return result
    if not service_id:
        result = _failed("missing_service_id")
        _log_result(result)
        return result

    params = {
        "resource": service_id,
        "startTime": _iso(_WINDOW_START),
        "endTime": _iso(_WINDOW_END),
    }
    logger.info(
        "[BANDWIDTH_SOURCES] start service=%s window=%s..%s endpoint=%s",
        service_id,
        _iso(_WINDOW_START),
        _iso(_WINDOW_END),
        _API_URL,
    )

    try:
        status, payload, reason = await _fetch(api_key, params)
    except asyncio.CancelledError:
        raise
    except httpx.TimeoutException:
        result = _failed("timeout", service_id=service_id)
    except httpx.RequestError as exc:
        result = _failed(
            f"request_failed:{type(exc).__name__}", service_id=service_id
        )
    except Exception as exc:  # noqa: BLE001 - external payloads are untrusted
        result = _failed(
            f"request_failed:{type(exc).__name__}", service_id=service_id
        )
    else:
        if reason:
            result = _failed(reason, service_id=service_id, http_status=status)
        else:
            result = _parse_payload(payload, service_id=service_id)
            result["http_status"] = status
    _log_result(result)
    return result
