"""
Global panel settings — delegates to settings_service.

All settings are stored in the panel_settings table (typed columns)
via the centralized settings_service. This module remains as a
compatibility shim so existing imports continue to work.
"""
from backend.services import settings_service


def load() -> None:
    settings_service.load_all()


def is_auto_close_enabled() -> bool:
    return settings_service.is_auto_close_enabled()


def set_auto_close_enabled(enabled: bool) -> bool:
    return settings_service.set_auto_close_enabled(enabled)


def toggle_auto_close() -> bool:
    return settings_service.toggle_auto_close()
