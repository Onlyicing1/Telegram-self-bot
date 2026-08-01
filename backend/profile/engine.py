"""
Shared profile engine logic — variable rendering used by both Bio and Username engines.

Keeps the variable system generic so future engines get variables for free.
"""
import logging
from datetime import datetime
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)


def get_tz(tz_str: str):
    try:
        return ZoneInfo(tz_str)
    except (ZoneInfoNotFoundError, Exception):
        from datetime import timezone
        return timezone.utc


def render_template(template: str, mood: str, text: str, tz_str: str) -> str:
    tz = get_tz(tz_str)
    now = datetime.now(tz)
    return (
        (template or "")
        .replace("{time}", now.strftime("%H:%M"))
        .replace("{mood}", mood or "")
        .replace("{text}", text or "")
    )
