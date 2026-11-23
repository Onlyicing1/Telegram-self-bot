"""Read Render's own outbound-bandwidth metric for this service only.

The API key must be workspace-scoped to the workspace that owns this service.
RENDER_SERVICE_ID is injected by Render at runtime, so a deployment in another
Render account automatically queries its own service and cannot reuse this
service's resource identifier.
"""
from __future__ import annotations

import os
from datetime import datetime, timedelta, timezone
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from typing import Any

import httpx

_API_URL = "https://api.render.com/v1/metrics/bandwidth"
_TIMEOUT = httpx.Timeout(8.0, connect=3.0)
_UNIT_BYTES = {
    "b": Decimal(1),
    "byte": Decimal(1),
    "bytes": Decimal(1),
    "kb": Decimal(1_000),
    "mb": Decimal(1_000_000),
    "gb": Decimal(1_000_000_000),
    "tb": Decimal(1_000_000_000_000),
}


def _utc(value: datetime | None = None) -> datetime:
    current = value or datetime.now(timezone.utc)
    if current.tzinfo is None:
        return current.replace(tzinfo=timezone.utc)
    return current.astimezone(timezone.utc)


def _iso(value: datetime) -> str:
    return value.isoformat(timespec="seconds").replace("+00:00", "Z")


def _unavailable(detail: str) -> dict[str, Any]:
    return {"available": False, "detail": detail, "state": "unavailable"}


def _series_matches_service(series: Any, service_id: str) -> bool:
    if not isinstance(series, dict):
        return False
    labels = series.get("labels")
    if not isinstance(labels, list):
        return False
    return any(
        isinstance(label, dict)
        and label.get("field") in {"service", "resource"}
        and label.get("value") == service_id
        for label in labels
    )


def _parse_payload(
    payload: Any,
    *,
    service_id: str,
    now: datetime,
    month_start: datetime,
) -> dict[str, Any]:
    if not isinstance(payload, list):
        return _unavailable("Render returned an unexpected metrics response.")
    matching = [item for item in payload if _series_matches_service(item, service_id)]
    if len(matching) != 1:
        return _unavailable(
            "Render did not return exactly one bandwidth series for this service."
        )

    series = matching[0]
    unit = str(series.get("unit") or "").strip().casefold()
    multiplier = _UNIT_BYTES.get(unit)
    if multiplier is None:
        return _unavailable("Render returned an unsupported bandwidth unit.")

    values = series.get("values")
    if not isinstance(values, list) or not values:
        return _unavailable("Render returned no bandwidth datapoints for this period.")

    daily_total = Decimal(0)
    monthly_total = Decimal(0)
    latest: datetime | None = None
    count = 0
    for point in values:
        if not isinstance(point, dict):
            return _unavailable("Render returned a malformed bandwidth datapoint.")
        timestamp = point.get("timestamp")
        if not isinstance(timestamp, str):
            return _unavailable("Render returned a datapoint without a timestamp.")
        try:
            observed_at = datetime.fromisoformat(timestamp.replace("Z", "+00:00"))
            if observed_at.tzinfo is None:
                return _unavailable("Render returned a timestamp without a timezone.")
            observed_at = observed_at.astimezone(timezone.utc)
            amount = Decimal(str(point.get("value")))
        except (ValueError, InvalidOperation, TypeError):
            return _unavailable("Render returned an invalid bandwidth datapoint.")
        if not amount.is_finite() or amount < 0:
            return _unavailable("Render returned an invalid bandwidth value.")
        if observed_at < month_start or observed_at > now:
            continue

        amount_bytes = amount * multiplier
        monthly_total += amount_bytes
        if observed_at.date() == now.date():
            daily_total += amount_bytes
        latest = observed_at if latest is None or observed_at > latest else latest
        count += 1

    if count == 0 or latest is None:
        return _unavailable("Render returned no bandwidth datapoints in the requested month.")

    return {
        "available": True,
        "state": "available",
        "daily_period": now.strftime("%Y-%m-%d"),
        "monthly_period": now.strftime("%Y-%m"),
        "daily_used_bytes": int(daily_total.to_integral_value(rounding=ROUND_HALF_UP)),
        "monthly_used_bytes": int(monthly_total.to_integral_value(rounding=ROUND_HALF_UP)),
        "latest_data_at": _iso(latest),
        "data_points": count,
        "unit": unit,
        "detail": "",
    }


async def get_render_bandwidth_usage(
    now: datetime | None = None,
) -> dict[str, Any]:
    """Return Render-reported egress for this service, grouped by UTC day/month.

    No account or service ID is accepted from callers. The workspace-scoped
    key and Render-injected service ID define the only permitted metric scope.
    """
    api_key = os.getenv("RENDER_API_KEY", "").strip()
    service_id = os.getenv("RENDER_SERVICE_ID", "").strip()
    if not api_key:
        return _unavailable("RENDER_API_KEY is not configured for this service.")
    if not service_id:
        return _unavailable("Render did not inject RENDER_SERVICE_ID for this service.")

    current = _utc(now)
    month_start = current.replace(day=1, hour=0, minute=0, second=0, microsecond=0)
    query_start = max(month_start, current - timedelta(days=29))
    params = {
        "startTime": _iso(query_start),
        "endTime": _iso(current),
        "resource": service_id,
    }
    headers = {
        "Authorization": f"Bearer {api_key}",
        "Accept": "application/json",
    }

    try:
        async with httpx.AsyncClient(timeout=_TIMEOUT) as client:
            response = await client.get(_API_URL, params=params, headers=headers)
            response.raise_for_status()
            payload = response.json()
    except httpx.HTTPStatusError as exc:
        return _unavailable(f"Render API returned HTTP {exc.response.status_code}.")
    except httpx.TimeoutException:
        return _unavailable("Render API request timed out.")
    except httpx.RequestError as exc:
        return _unavailable(f"Render API request failed ({type(exc).__name__}).")
    except (ValueError, TypeError):
        return _unavailable("Render API returned invalid JSON.")

    try:
        result = _parse_payload(
            payload,
            service_id=service_id,
            now=current,
            month_start=query_start,
        )
        if result.get("available") is True:
            result["monthly_is_partial"] = query_start > month_start
            result["monthly_coverage_start"] = _iso(query_start)
        return result
    except Exception as exc:  # noqa: BLE001 — external payloads are untrusted
        return _unavailable(f"Render metrics could not be parsed ({type(exc).__name__}).")
