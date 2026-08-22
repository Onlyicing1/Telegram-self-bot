"""
Provider health/cooldown state machine with a circuit breaker.

Per-provider availability state with a monotonic cooldown clock. The
``ProviderManager`` consults this before routing a request and updates it
after each failure/success, so a rate-limited or failing provider is
temporarily skipped instead of being retried forever.

States:
    healthy      → available
    cooling_down → skipped until the monotonic deadline passes
    quarantined  → skipped after a run of consecutive failures
    disabled     → skipped until configuration changes (auth failures)

A provider that fails ``QUARANTINE_AFTER_FAILURES`` times in a row is
quarantined for ``QUARANTINE_SECONDS``. Failures carry different penalties
(see ``FAILURE_PENALTIES``): a 429 honors the server's retry-after, a timeout
is short, a malformed/quality failure is longer. After quarantine expires the
provider automatically re-enters the pool; ``record_success`` clears the
failure streak so a recovered provider is fully trusted again.

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

#: Consecutive failures that push a provider into quarantine (circuit open).
QUARANTINE_AFTER_FAILURES = 5
#: How long a quarantined provider stays out of the pool.
QUARANTINE_SECONDS = 600.0

#: Per-failure-category cooldown penalties. ``rate_limited`` is resolved from
#: the provider's retry-after by the caller; ``request``/``model_not_found``
#: are permanent config errors and are never cooled down (see the manager);
#: ``empty_response``/``structured_output`` are request-level quality signals
#: handled by the router's failover path (no cooldown on a single occurrence).
FAILURE_PENALTIES: dict[str, float] = {
    "timeout": 30.0,
    "server": 60.0,
    "network": 60.0,
    "malformed": 120.0,
    "quality": 120.0,
    "empty_response": 30.0,
    "structured_output": 120.0,
    "tool_call": 60.0,
    "unknown": 60.0,
}

#: Canonical normalized failure categories. Provider-specific error strings
#: (HTTP status, provider error codes, vendor messages) never travel past
#: this layer — the rest of LifeOS reasons about these categories only.
FAILURE_CATEGORIES: frozenset[str] = frozenset({
    "auth",              # 401/403 — disabled until configuration changes
    "rate_limited",      # 429 / quota — cooldown, honor Retry-After
    "model_not_found",   # 404 dead/retired model — (provider, model) unavailable
    "timeout",           # request timeout — retry once, then fail over
    "network",           # connection/reset — retry once, then fail over
    "server",            # 5xx — bounded retry, then fail over
    "request",           # 4xx invalid request — deterministic repair or fail over
    "tool_call",         # provider emitted an unusable tool call
    "structured_output", # malformed structured output / all-malformed tool calls
    "empty_response",    # success with no text and no tool call
    "malformed",         # unparsable provider response body
    "unknown",           # anything unclassified
})


class ProviderHealthState:
    HEALTHY = "healthy"
    COOLING_DOWN = "cooling_down"
    DISABLED = "disabled"
    QUARANTINED = "quarantined"


class ProviderHealthTracker:
    """Tracks per-provider availability, concurrency, and failure streaks.

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
        "_consecutive_failures",
        "_consecutive_successes",
        "_quarantine_until",
        "_last_failure_category",
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
        self._consecutive_failures: dict[str, int] = {}
        self._consecutive_successes: dict[str, int] = {}
        self._quarantine_until: dict[str, float] = {}
        self._last_failure_category: dict[str, str] = {}

    # ── State ──

    def state(self, name: str) -> str:
        if name in self._disabled:
            return ProviderHealthState.DISABLED
        q_until = self._quarantine_until.get(name)
        if q_until is not None and time.monotonic() < q_until:
            return ProviderHealthState.QUARANTINED
        until = self._cooldown_until.get(name)
        if until is not None and time.monotonic() < until:
            return ProviderHealthState.COOLING_DOWN
        return ProviderHealthState.HEALTHY

    def is_available(self, name: str) -> bool:
        return self.state(name) == ProviderHealthState.HEALTHY

    def mark_cooling_down(self, name: str, seconds: float) -> None:
        seconds = min(max(0.0, float(seconds)), MAX_COOLDOWN_SECONDS)
        self._cooldown_until[name] = time.monotonic() + seconds
        logger.info("PROVIDER_COOLDOWN provider=%s seconds=%.1f", name, seconds)

    def mark_disabled(self, name: str) -> None:
        self._disabled.add(name)
        self._cooldown_until.pop(name, None)
        self._quarantine_until.pop(name, None)
        self._consecutive_failures.pop(name, None)
        logger.warning("provider-health: '%s' disabled until configuration changes", name)

    def mark_healthy(self, name: str) -> None:
        """Fully reset a provider's health (cooldown, quarantine, streaks)."""
        self._cooldown_until.pop(name, None)
        self._disabled.discard(name)
        self._quarantine_until.pop(name, None)
        self._consecutive_failures.pop(name, None)
        self._consecutive_successes.pop(name, None)
        self._last_failure_category.pop(name, None)

    # ── Circuit breaker / failure recording ──

    def record_failure(
        self,
        name: str,
        category: str = "unknown",
        retry_after: float | None = None,
    ) -> str:
        """Record a failure and apply a category-specific penalty.

        Returns the new state. Repeated consecutive failures quarantine the
        provider (circuit opens); a single failure only cools it down with the
        penalty appropriate for the failure category. The triggering category
        is remembered (``last_failure_category``) so observability can say
        WHY a provider is cooling down without guessing.
        """
        self._consecutive_successes.pop(name, None)
        n = self._consecutive_failures.get(name, 0) + 1
        self._consecutive_failures[name] = n
        self._last_failure_category[name] = category

        if n >= QUARANTINE_AFTER_FAILURES:
            self._quarantine_until[name] = time.monotonic() + QUARANTINE_SECONDS
            self._cooldown_until.pop(name, None)
            logger.warning(
                "PROVIDER_QUARANTINED provider=%s failures=%d seconds=%.0f",
                name, n, QUARANTINE_SECONDS,
            )
            return ProviderHealthState.QUARANTINED

        if category == "rate_limited" and retry_after is not None:
            seconds = float(retry_after)
        else:
            seconds = FAILURE_PENALTIES.get(category, DEFAULT_COOLDOWN_SECONDS)
        self.mark_cooling_down(name, seconds)
        return ProviderHealthState.COOLING_DOWN

    def record_success(self, name: str) -> None:
        """Record a successful request — clears failure streaks and quarantine."""
        was_quarantined = self._is_quarantined_now(name)
        self._consecutive_failures.pop(name, None)
        self._consecutive_successes[name] = self._consecutive_successes.get(name, 0) + 1
        self._cooldown_until.pop(name, None)
        self._quarantine_until.pop(name, None)
        self._disabled.discard(name)
        self._last_failure_category.pop(name, None)
        if was_quarantined:
            logger.info("PROVIDER_RECOVERED provider=%s", name)

    def _is_quarantined_now(self, name: str) -> bool:
        until = self._quarantine_until.get(name)
        return until is not None and time.monotonic() < until

    def consecutive_failures(self, name: str) -> int:
        return self._consecutive_failures.get(name, 0)

    def consecutive_successes(self, name: str) -> int:
        return self._consecutive_successes.get(name, 0)

    def cooldown_remaining(self, name: str) -> float:
        until = self._cooldown_until.get(name)
        if until is None:
            return 0.0
        return max(0.0, until - time.monotonic())

    def quarantine_remaining(self, name: str) -> float:
        until = self._quarantine_until.get(name)
        if until is None:
            return 0.0
        return max(0.0, until - time.monotonic())

    def last_failure_category(self, name: str) -> str:
        """The normalized category that triggered the current recovery state.

        Empty string when unknown (e.g. the provider was never failed or
        already recovered) — callers must not guess a reason.
        """
        return self._last_failure_category.get(name, "")

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
            if now < until:
                entry: dict[str, Any] = {
                    "state": ProviderHealthState.COOLING_DOWN,
                    "cooldown_remaining_s": round(max(0.0, until - now), 2),
                }
                if self._consecutive_failures.get(name):
                    entry["consecutive_failures"] = self._consecutive_failures[name]
                result[name] = entry
        for name, until in self._quarantine_until.items():
            if now < until:
                result[name] = {
                    "state": ProviderHealthState.QUARANTINED,
                    "cooldown_remaining_s": round(max(0.0, until - now), 2),
                    "consecutive_failures": self._consecutive_failures.get(name, 0),
                }
        for name in self._disabled:
            result[name] = {
                "state": ProviderHealthState.DISABLED,
                "cooldown_remaining_s": 0.0,
            }
        return result
