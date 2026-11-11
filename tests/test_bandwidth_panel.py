"""Bandwidth Usage panel — Settings child panel + governor read contract.

Covers the read-only Glass UI surface added under Settings:

  * The Settings panel carries exactly one ``Bandwidth Usage`` row pointing at
    the registered ``bandwidth`` child panel (parent ``settings``).
  * The page renders daily/monthly usage, the exact governor limits, remaining
    capacity, the UTC accounting periods and the normal/near/exhausted state.
  * Byte values use decimal MB/GB with useful precision.
  * Refresh re-reads the service and repaints the SAME panel view.
  * An unreadable accounting state is shown as unavailable, never as zero.
  * Back/Home/Close remain the shared navigation row.

The governor itself is not exercised here beyond its read API: these tests use
the real handlers and registry, and stub only ``get_outbound_usage`` / the
repository read boundary.
"""
from __future__ import annotations

from datetime import datetime, timezone
from unittest.mock import AsyncMock

import pytest

# Import the helper package before the handler: ``misc`` pulls diagnostics,
# whose first import of the helper package would otherwise re-enter it.
from backend.helper import panels as panels_mod
from backend.helper import symbols as sym

from backend.bot.handlers import misc
from backend.helper.panel_registry import (
    get_panel_parent,
    get_panel_title,
    has_panel,
)
from backend.helper.panels import get_action, get_panel
from backend.services import outbound_bandwidth_service as bw

UTC = timezone.utc
NOW = datetime(2026, 11, 11, 12, 0, tzinfo=UTC)


@pytest.fixture(autouse=True)
def _registered_panels():
    misc._register_panels()


def _flatten(rows) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for row in rows:
        cells = row if isinstance(row, list) else [row]
        for btn in cells:
            data = getattr(btn, "data", None) or b""
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            out.append((getattr(btn, "text", ""), str(data)))
    return out


def _buttons(rows) -> dict[str, object]:
    result: dict[str, object] = {}
    for row in rows:
        cells = row if isinstance(row, list) else [row]
        for btn in cells:
            result[getattr(btn, "text", "")] = btn
    return result


def _bg(button, name: str):
    return getattr(getattr(button, "style", None), f"bg_{name}", None)


def _usage(**overrides) -> dict:
    base = {
        "available": True,
        "detail": "",
        "daily_period": "2026-11-11",
        "monthly_period": "2026-11",
        "daily_used_bytes": 10_000_000,
        "daily_limit_bytes": 150_000_000,
        "daily_remaining_bytes": 140_000_000,
        "monthly_used_bytes": 100_000_000,
        "monthly_limit_bytes": 4_000_000_000,
        "monthly_remaining_bytes": 3_900_000_000,
        "state": "normal",
    }
    base.update(overrides)
    return base


# ── 1. Settings entry point + registration ──


@pytest.mark.asyncio
async def test_settings_panel_contains_the_bandwidth_usage_button():
    _, _, rows = await misc._settings_panel_handler(None, "")
    ones = _flatten(rows)
    data = dict(ones)
    assert data["Bandwidth Usage"] == "panel:bandwidth"
    buttons = _buttons(rows)
    assert _bg(buttons["Bandwidth Usage"], "primary") is True
    # Every pre-existing Settings action survives unchanged.
    for label, expected in (
        ("Toggle Auto-close", "action:settings_toggle_autoclose"),
        ("Toggle Debug Callbacks", "action:settings_toggle_debug_callbacks"),
        ("Font", "panel:font"),
    ):
        assert data[label] == expected
    assert len(ones) == len({text for text, _ in ones})


def test_bandwidth_panel_is_registered_under_settings():
    assert has_panel("bandwidth")
    assert get_panel("bandwidth") is misc._bandwidth_panel_handler
    assert get_panel_parent("bandwidth") == "settings"
    assert get_panel_title("bandwidth") == "Bandwidth Usage"


def test_refresh_action_is_registered():
    assert get_action("bandwidth_refresh") is misc._bandwidth_refresh_action


# ── 2. Rendering ──


@pytest.mark.asyncio
async def test_panel_renders_usage_limits_remaining_and_utc_periods(monkeypatch):
    monkeypatch.setattr(bw, "get_outbound_usage", AsyncMock(return_value=_usage()))
    title, body, rows = await misc._bandwidth_panel_handler(None, "")

    assert title == "Bandwidth Usage"
    assert "2026-11-11" in body
    assert "2026-11" in body
    assert "Used: 10.000 MB of 150.000 MB" in body
    assert "Remaining: 140.000 MB" in body
    assert "Used: 100.000 MB of 4.000 GB" in body
    assert "Remaining: 3.900 GB" in body
    assert "**State:** Normal" in body
    assert "UTC" in body
    assert "category" not in body.lower()
    assert sym.label(sym.OP_REFRESH, "Refresh") in dict(_flatten(rows))


@pytest.mark.parametrize(
    ("value", "expected"),
    [
        (0, "0 B"),
        (999, "999 B"),
        (1_500, "1.5 KB"),
        (1_500_000, "1.500 MB"),
        (150_000_000, "150.000 MB"),
        (4_000_000_000, "4.000 GB"),
        (2_766_000_000, "2.766 GB"),
        (None, "—"),
    ],
)
def test_byte_formatting(value, expected):
    assert misc._format_bandwidth_bytes(value) == expected


# ── 3. States ──


@pytest.mark.parametrize(
    ("daily", "monthly", "expected"),
    [
        (0, 0, "normal"),
        (134_999_999, 0, "normal"),
        (135_000_000, 0, "near_limit"),
        (0, 3_600_000_000, "near_limit"),
        (149_999_999, 0, "near_limit"),
        (150_000_000, 0, "exhausted"),
        (0, 4_000_000_000, "exhausted"),
    ],
)
def test_usage_state_classification(daily, monthly, expected):
    assert bw.usage_state(daily, monthly) == expected


@pytest.mark.asyncio
@pytest.mark.parametrize(
    ("state", "label"),
    [("near_limit", "Near limit"), ("exhausted", "Exhausted")],
)
async def test_panel_shows_near_limit_and_exhausted(monkeypatch, state, label):
    monkeypatch.setattr(
        bw, "get_outbound_usage", AsyncMock(return_value=_usage(state=state)),
    )
    _, body, _ = await misc._bandwidth_panel_handler(None, "")
    assert f"**State:** {label}" in body


# ── 4. Refresh repaints the same panel ──


@pytest.mark.asyncio
async def test_refresh_reads_fresh_data_and_keeps_the_same_panel(monkeypatch):
    readings = iter([
        _usage(),
        _usage(
            daily_used_bytes=20_000_000,
            daily_remaining_bytes=130_000_000,
            state="near_limit",
        ),
    ])

    async def _read(now=None):
        return next(readings)

    monkeypatch.setattr(bw, "get_outbound_usage", _read)

    _, first, _ = await misc._bandwidth_panel_handler(None, "")
    _, second, _ = await get_action("bandwidth_refresh")(None, "", 1)

    assert "10.000 MB" in first
    assert "20.000 MB" in second
    assert second != first

    class _Sessions:
        def current_nav(self, chat_id, msg_id):
            return ("bandwidth", "")

    class _Life:
        sessions = _Sessions()

    monkeypatch.setattr(panels_mod, "get_lifecycle", lambda: _Life())
    paint = await panels_mod._handle_action(None, "bandwidth_refresh", 1, 2, 1)
    assert paint is not None
    panel_id, result, home_view = paint
    assert panel_id == "bandwidth"
    assert home_view is False  # action result paints, never re-creates


# ── 5. Unavailable is not zero ──


@pytest.mark.asyncio
async def test_read_failure_becomes_an_explicit_unavailable_state(monkeypatch):
    monkeypatch.setattr(bw, "_db_configured", lambda: True)
    monkeypatch.setattr(bw.repo, "load", lambda: None)

    usage = await bw.get_outbound_usage(now=NOW)
    assert usage["available"] is False
    assert usage["state"] == "unavailable"
    assert "unavailable" in usage["detail"].lower()

    _, body, _ = await misc._bandwidth_panel_handler(None, "")
    assert "unavailable" in body.lower()
    assert "Used:" not in body
    assert "0 B" not in body


@pytest.mark.asyncio
async def test_missing_database_is_unavailable_not_zero(monkeypatch):
    monkeypatch.setattr(bw, "_db_configured", lambda: False)
    usage = await bw.get_outbound_usage(now=NOW)
    assert usage["available"] is False
    assert "not configured" in usage["detail"]


@pytest.mark.asyncio
async def test_a_real_zero_state_is_available_zero(monkeypatch):
    state = {
        "version": 1,
        "daily": {"period": "2026-11-11", "bytes": 0},
        "monthly": {"period": "2026-11", "bytes": 0},
    }
    monkeypatch.setattr(bw, "_db_configured", lambda: True)
    monkeypatch.setattr(bw.repo, "load", lambda: {"outbound_bandwidth_state": state})

    usage = await bw.get_outbound_usage(now=NOW)
    assert usage["available"] is True
    assert usage["daily_used_bytes"] == 0
    assert usage["monthly_used_bytes"] == 0
    assert usage["state"] == "normal"

    _, body, _ = await misc._bandwidth_panel_handler(None, "")
    assert "Used: 0 B of 150.000 MB" in body


# ── 6. Shared navigation ----------


@pytest.mark.asyncio
async def test_back_home_close_navigation_is_the_shared_row(monkeypatch):
    monkeypatch.setattr(bw, "get_outbound_usage", AsyncMock(return_value=_usage()))

    class _Sessions:
        def is_root_view(self, chat_id, msg_id):
            return False

    class _Life:
        sessions = _Sessions()

    monkeypatch.setattr(panels_mod, "get_lifecycle", lambda: _Life())
    _, _, rows = await misc._bandwidth_panel_handler(None, "")
    _, _, finalized = panels_mod._finalize_panel(
        "Bandwidth Usage", "body", rows, "bandwidth", chat_id=1, msg_id=2,
    )
    data = [callback for _, callback in _flatten(finalized)]
    assert "panel:_nav:back" in data
    assert "panel:_nav:home" in data
    assert "panel:_nav:close" in data
    assert get_panel_parent("bandwidth") == "settings"
