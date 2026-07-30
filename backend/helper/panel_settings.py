"""
Global panel settings — delegates to settings_service.

All panel configuration is stored in the panel_settings table via the
centralized settings_service. This module remains as a compatibility
shim so existing imports (backend.helper.panel_settings) continue
to work without changes.
"""
from backend.services import settings_service


def load() -> None:
    settings_service.load_all()


def reload() -> None:
    settings_service.reload_panel_settings()


def reload() -> None:
    settings_service.reload_panel_settings()


def is_auto_close_enabled() -> bool:
    return settings_service.is_auto_close_enabled()


def set_auto_close_enabled(enabled: bool) -> bool:
    return settings_service.set_auto_close_enabled(enabled)


def auto_close_delay() -> int:
    return settings_service.auto_close_delay()


def toggle_auto_close() -> bool:
    return settings_service.toggle_auto_close()


def auto_close_delay() -> int:
    return settings_service.auto_close_delay()
