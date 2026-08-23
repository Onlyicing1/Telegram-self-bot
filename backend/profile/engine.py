"""
Shared Profile Engine — a single parameterized implementation for profile
fields that render a template every minute and hand the change to the shared
Profile Scheduler.

Bio and Username previously mirrored this logic in two near-identical modules.
This class centralizes the renderer, updater, and scheduler lifecycle so the
two engines cannot drift apart. Each concrete engine (``backend.bio.engine``
and ``backend.username.engine``) instantiates one ``ProfileEngine`` with its
own field name, state key, default template, and DB accessors.

The engine controls exactly ONE ``UpdateProfileRequest`` field:

- Bio       → ``about``       (bio_state.last_bio)
- Username  → ``first_name``  (username_state.last_name)

It never owns its own cron loop. ``start_cron``/``stop_cron`` delegate to the
shared ``backend.profile.scheduler``. Per-engine active state is tracked in the
scheduler so turning one engine off never stops the other while it is active.
"""

from __future__ import annotations

import logging
from datetime import datetime, timezone
from typing import Any, Awaitable, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.diagnostics import record_event
from backend.profile import scheduler as profile_scheduler
from backend.runtime.tracer import trace

logger = logging.getLogger(__name__)

GetStateFn = Callable[[int], Awaitable[dict[str, Any] | None]]
UpdateStateFn = Callable[[int, dict[str, Any]], Awaitable[None]]


class ProfileEngine:
    """Parameterized profile-field renderer + updater + scheduler lifecycle."""

    def __init__(
        self,
        *,
        name: str,
        field: str,
        state_key: str,
        default_template: str,
        get_state: GetStateFn,
        update_state: UpdateStateFn,
    ) -> None:
        self.name = name
        self.field = field
        self.state_key = state_key
        self.default_template = default_template
        self._get_state = get_state
        self._update_state = update_state
        self._registered = False

    def get_tz(self, tz_str: str):
        try:
            return ZoneInfo(tz_str)
        except (ZoneInfoNotFoundError, Exception):
            logger.warning("Timezone '%s' not found — falling back to UTC.", tz_str)
            return timezone.utc

    def render(self, template: str, mood: str, text: str, tz_str: str) -> str:
        tz = self.get_tz(tz_str)
        now = datetime.now(tz)
        value = (
            (template or self.default_template)
            .replace("{time}", now.strftime("%H:%M"))
            .replace("{mood}", mood or "😊")
            .replace("{text}", text or "")
        )
        # The owner-selected Glass UI font applies to the visible profile
        # field (letters and supported digits). Defensive: a bad font state
        # must never break the profile update.
        try:
            from backend.helper.font_style import apply_font
            from backend.services import settings_service
            return apply_font(value, settings_service.dashboard_font())
        except Exception:
            return value

    async def updater(self, owner_id: int, tz_str: str) -> dict[str, str] | None:
        """Called by the shared profile scheduler each minute.

        Returns ``{<field>: rendered}`` when the value changed, or ``None``
        when inactive or unchanged (deduplication). Persists the new value
        before returning it.
        """
        state = await self._get_state(owner_id)
        if not state or not state.get("is_active"):
            return None

        template = state.get("template", self.default_template)
        mood = state.get("mood", "😊")
        text = state.get("custom_text", "")

        new_value = self.render(template, mood, text, tz_str)
        last_value = state.get(self.state_key)

        if new_value == (last_value or ""):
            return None

        await self._update_state(owner_id, {
            self.state_key: new_value,
            "updated_at": datetime.now(self.get_tz(tz_str)).isoformat(),
        })
        return {self.field: new_value}

    def _ensure_registered(self) -> None:
        if self._registered:
            return
        profile_scheduler.register_updater(self.name, self.updater)
        self._registered = True

    def start_cron(self, client, owner_id: int, tz_str: str) -> None:
        self._ensure_registered()
        profile_scheduler.set_engine_active(self.name, True)
        profile_scheduler.start_cron(client, owner_id, tz_str)
        trace(f"{self.name.upper()}_CRON_START_REQUESTED")
        record_event(self.name, "start_cron", 0, "SUCCESS")

    def update_client(self, client) -> None:
        """Swap the client after a rebuild without restarting the engine."""
        profile_scheduler.update_client(client)

    async def stop_cron(self) -> None:
        """Deactivate this engine; stop the shared scheduler only if no other
        engine remains active."""
        trace(f"{self.name.upper()}_CRON_STOP_REQUESTED")
        profile_scheduler.set_engine_active(self.name, False)
        await profile_scheduler.stop_if_idle()
        record_event(self.name, "stop_cron", 0, "SUCCESS")

    def is_running(self) -> bool:
        return profile_scheduler.is_running()
