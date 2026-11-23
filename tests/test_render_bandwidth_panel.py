"""Glass UI integration tests for the dual bandwidth sources."""
from __future__ import annotations

from unittest.mock import AsyncMock

import pytest

from backend.bot.handlers import misc
from backend.services import outbound_bandwidth_service as governor
from backend.services import render_bandwidth_service as render_bw


@pytest.mark.asyncio
async def test_bandwidth_panel_displays_governor_and_render_as_separate_sources(monkeypatch):
    monkeypatch.setattr(governor, "get_outbound_usage", AsyncMock(return_value={
        "available": True,
        "daily_period": "2026-11-23",
        "monthly_period": "2026-11",
        "daily_used_bytes": 12_000_000,
        "daily_limit_bytes": 150_000_000,
        "daily_remaining_bytes": 138_000_000,
        "monthly_used_bytes": 50_000_000,
        "monthly_limit_bytes": 4_000_000_000,
        "monthly_remaining_bytes": 3_950_000_000,
        "state": "normal",
    }))
    monkeypatch.setattr(render_bw, "get_render_bandwidth_usage", AsyncMock(return_value={
        "available": True,
        "daily_period": "2026-11-23",
        "monthly_period": "2026-11",
        "daily_used_bytes": 125_000_000,
        "monthly_used_bytes": 3_950_000_000,
        "latest_data_at": "2026-11-23T15:00:00Z",
        "data_points": 337,
    }))

    title, body, _ = await misc._bandwidth_panel_handler(None, "")

    assert title == "Bandwidth Usage"
    assert "LifeOS upload governor" in body
    assert "Render platform" in body
    assert "12.000 MB of 150.000 MB" in body
    assert "125.000 MB" in body
    assert "3.950 GB" in body
    assert "15:00:00Z" in body
    assert "different measurements" in body


@pytest.mark.asyncio
async def test_render_metrics_failure_does_not_hide_governor_usage(monkeypatch):
    monkeypatch.setattr(governor, "get_outbound_usage", AsyncMock(return_value={
        "available": True,
        "daily_period": "2026-11-23",
        "monthly_period": "2026-11",
        "daily_used_bytes": 0,
        "daily_limit_bytes": 150_000_000,
        "daily_remaining_bytes": 150_000_000,
        "monthly_used_bytes": 0,
        "monthly_limit_bytes": 4_000_000_000,
        "monthly_remaining_bytes": 4_000_000_000,
        "state": "normal",
    }))
    monkeypatch.setattr(render_bw, "get_render_bandwidth_usage", AsyncMock(return_value={
        "available": False,
        "detail": "Render API returned HTTP 403.",
    }))

    _, body, _ = await misc._bandwidth_panel_handler(None, "")

    assert "Used: 0 B of 150.000 MB" in body
    assert "Render platform outbound bandwidth" in body
    assert "Unavailable" in body
    assert "HTTP 403" in body
