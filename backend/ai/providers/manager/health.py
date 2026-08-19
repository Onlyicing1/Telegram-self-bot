"""
Provider health/cooldown state machine.

Per-provider availability state with a monotonic cooldown clock. The
``ProviderManager`` consults this before routing a request and updates it
after each failure, so a rate-limited or failing provider is temporarily
skipped instead of being retried forever.

States:
    healthy      → available
    cooling_down → skipped until the monotonic deadline passes
    disabled     → skipped until configuration changes (auth failures)

This is NOT a second supervisor — it is pure per-provider routing state
owned by the ``ProviderManager``.
"""
from __future__ import annotations

import asyncio
import logging
import time
from typing import Any

logger = logging.getLogger(__name__)

#: Failures eligible for ONE immediate retry before cooldown.
RETRYABLE_FAILURES = frozenset({"network", "timeout", "server"})
#: Auth failures are terminal for the provider until configuration changes.
AUTH_FAILURES = frozenset({"auth"})

DEFAULT_COOLDOWN_SECONDS = 60.0
MAX_COOLDOWN_SECONDS = 300.0


class ProviderHealthState:
    HEALTHY = "healthy"
    COOLING_DOWN = "cooling_down"
    DISABLED = "disabled"


class ProviderHealthTracker:
    """Tracks per-provider availability and concurrency limits.

    The cooldown clock uses ``time.monotonic()`` so it is immune to wall-clock
    jumps. Concurrency is enforced with a per-provider ``asyncio.Semaphore``;
    providers with a low concurrency ceiling (e.g. Z.ai) can declare a tighter
    override.
    """

    __slots__ = (
        "_cooldown_until",
        "_disabled",
        "_default_concurrency",
        "_concurrency_overrides",
        "_semaphores",
    )

    def __init__(
        self,
        default_concurrency: int = 4,
        concurrency_overrides: dict[str, int] | None = None,
    ) -> None:
        self._cooldown_until: dict[str, float] = {}
        self._disabled: set[str] = set()
        self._default_concurrency = max(1, default_concurrency)
        self._concurrency_overrides = dict(concurrency_overrides or {})
        self._semaphores: dict[str, asyncio.Semaphore] = {}

    # ── State ──

    def state(self, name: str) -> str:
        if name in self._disabled:
            return ProviderHealthState.DISABLED
        until = self._cooldown_until.get(name)
        if until is not None and time.monotonic() < until:
            return ProviderHealthState.COOLING_DOWN
        return ProviderHealthState.HEALTHY

    def is_available(self, name: str) -> bool:
        return self.state(name) == ProviderHealthState.HEALTHY

    def mark_cooling_down(self, name: str, seconds: float) -> None:
        seconds = min(max(0.0, float(seconds)), MAX_COOLDOWN_SECONDS)
        self._cooldown_until[name] = time.monotonic() + seconds
        logger.warning("provider-health: '%s' cooling_down for %.1fs", name, seconds)

    def mark_disabled(self, name: str) -> None:
        self._disabled.add(name)
        self._cooldown_until.pop(name, None)
        logger.warning("provider-health: '%s' disabled until configuration changes", name)

    def mark_healthy(self, name: str) -> None:
        self._cooldown_until.pop(name, None)
        self._disabled.discard(name)

    def cooldown_remaining(self, name: str) -> float:
        until = self._cooldown_until.get(name)
        if until is None:
            return 0.0
        return max(0.0, until - time.monotonic())

    # ── Concurrency ──

    def acquire(self, name: str) -> asyncio.Semaphore:
        if name not in self._semaphores:
            limit = self._concurrency_overrides.get(name, self._default_concurrency)
            self._semaphores[name] = asyncio.Semaphore(limit)
        return self._semaphores[name]

    # ── Introspection ──

    def snapshot(self) -> dict[str, dict[str, Any]]:
        now = time.monotonic()
        result: dict[str, dict[str, Any]] = {}
        for name, until in self._cooldown_until.items():
            result[name] = {
                "state": ProviderHealthState.COOLING_DOWN,
                "cooldown_remaining_s": round(max(0.0, until - now), 2),
            }
        for name in self._disabled:
            result[name] = {
                "state": ProviderHealthState.DISABLED,
                "cooldown_remaining_s": 0.0,
            }
        return result
