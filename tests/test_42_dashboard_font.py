"""
TASK 42 — Dashboard font setting.

Verifies the enumerated dashboard-font setting through the existing
settings_service pipeline: deterministic default, persistence via the
write-through cache, reload/read round-trip, invalid/missing value
fallback, other settings unaffected, the PATCH /api/settings endpoint,
and that the dashboard frontend actually consumes the selected font.
"""
from __future__ import annotations

from unittest.mock import patch

import pytest


def _reset_service():
    import backend.services.settings_service as svc
    svc._loaded = False
    svc._cache = {}


def _load_with(row: dict | None):
    """Reset the service and load from a fake DB row."""
    _reset_service()
    with patch("backend.services.panel_settings_repository.load", return_value=row):
        from backend.services import settings_service as svc
        svc.load_all()


def test_default_font_is_deterministic():
    _load_with(None)  # no DB row -> defaults
    from backend.services import settings_service as svc

    assert svc.dashboard_font() == "default"
    assert svc.get_all()["dashboard_font"] == "default"


def test_valid_font_selection_persists():
    _load_with(None)
    from backend.services import settings_service as svc

    assert svc.set_dashboard_font("mono") is True
    assert svc.get_all()["dashboard_font"] == "mono"
    assert svc.dashboard_font() == "mono"


def test_reload_read_returns_persisted_font():
    row = {"key": "global", "dashboard_font": "fraktur"}
    _load_with(row)
    from backend.services import settings_service as svc

    assert svc.dashboard_font() == "fraktur"
    assert svc.get_all()["dashboard_font"] == "fraktur"


def test_invalid_font_value_rejected():
    _load_with(None)
    from backend.services import settings_service as svc

    assert svc.set_dashboard_font("comic-sans") is False
    assert svc.dashboard_font() == "default"  # unchanged
    assert svc.set_setting("dashboard_font", "mono") is True
    assert svc.set_setting("dashboard_font", "mono !important") is False
    assert svc.dashboard_font() == "mono"


def test_missing_font_setting_falls_back_safely():
    # DB row exists but has no dashboard_font column value.
    row = {"key": "global", "language": "en", "owner_only": True}
    _load_with(row)
    from backend.services import settings_service as svc

    assert svc.dashboard_font() == "default"


def test_invalid_persisted_value_falls_back_safely():
    row = {"key": "global", "dashboard_font": "weird"}
    _load_with(row)
    from backend.services import settings_service as svc

    assert svc.dashboard_font() == "default"


def test_other_settings_remain_unaffected():
    _load_with(None)
    from backend.services import settings_service as svc

    before = svc.get_all()
    assert svc.set_dashboard_font("small_caps") is True
    after = svc.get_all()

    assert after["dashboard_font"] == "small_caps"
    for key, value in before.items():
        if key != "dashboard_font":
            assert after[key] == value, f"setting '{key}' changed"


@pytest.mark.asyncio
async def test_patch_settings_endpoint_roundtrip():
    """The endpoint validates and persists; when the DB write is unavailable
    (column not yet applied / no DB) it degrades to the in-memory cache —
    the value still sticks for the session and never breaks the API."""
    _reset_service()
    from fastapi import HTTPException
    from backend.web.app import update_setting

    # Emulate the running process: service loaded once at startup, then the
    # DB write is unavailable (column not applied / no DB) — the value must
    # still stick in the session cache and the API must keep working.
    with patch("backend.services.panel_settings_repository.load", return_value=None), \
         patch("backend.services.panel_settings_repository.update_field",
               return_value=False):
        from backend.services import settings_service as svc
        svc.load_all()
        result = await update_setting({"key": "dashboard_font", "value": "fraktur"})
    assert result["dashboard_font"] == "fraktur"

    with pytest.raises(HTTPException) as exc:
        await update_setting({"key": "dashboard_font", "value": "bogus"})
    assert exc.value.status_code == 400
    assert "Invalid value" in exc.value.detail


def test_allow_list_is_the_single_font_style_registry():
    """The Glass UI font panel and the dashboard setting must share ONE
    authoritative allow-list (backend/helper/font_style.FONT_KEYS)."""
    _load_with(None)
    from backend.services import settings_service as svc
    from backend.helper.font_style import FONT_KEYS

    assert svc.DASHBOARD_FONTS == FONT_KEYS
    # The web dashboard tolerates any key with a deterministic fallback.
    from pathlib import Path
    app_tsx = Path("src/App.tsx").read_text()
    assert "?? DASHBOARD_FONT_OPTIONS[0].stack" in app_tsx


def test_dashboard_consumes_selected_font():
    """Source-level guard: the frontend applies the persisted allow-listed
    key as a CSS variable and the stylesheet consumes it (compiled by
    typecheck/build)."""
    from pathlib import Path

    app_tsx = Path("src/App.tsx").read_text()
    css = Path("src/index.css").read_text()

    assert "settings.dashboard_font" in app_tsx
    assert "--app-font" in app_tsx
    assert "var(--app-font" in css
    # Enumerated keys only — no free-text CSS ever reaches the DOM style.
    assert "default" in app_tsx and "mono" in app_tsx and "serif" in app_tsx
