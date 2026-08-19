"""
ProviderMetrics — per-provider runtime statistics.

Tracks requests, failures, average latency, last error, and health
for a single provider. All data lives in RAM and is reset on process
restart. The ``ProviderMetricsRegistry`` holds one ``ProviderMetrics``
per registered provider name.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any


@dataclass
class ProviderMetrics:
    requests: int = 0
    failures: int = 0
    total_latency: float = 0.0
    last_error: str = ""
    last_error_time: str = ""
    healthy: bool = True
    #: Semantic quality — an HTTP 200 with no usable content when a structured
    #: action was required is NOT a successful AI execution.
    quality_requests: int = 0
    quality_failures: int = 0

    @property
    def success_rate(self) -> float:
        if self.requests == 0:
            return 0.0
        return (self.requests - self.failures) / self.requests

    @property
    def quality_success_rate(self) -> float:
        if self.quality_requests == 0:
            return 1.0
        return (self.quality_requests - self.quality_failures) / self.quality_requests

    @property
    def average_latency(self) -> float:
        if self.requests == 0:
            return 0.0
        return self.total_latency / self.requests

    def record(self, *, latency: float, error: str = "") -> None:
        self.requests += 1
        self.total_latency += latency
        if error:
            self.failures += 1
            self.last_error = error
            self.healthy = False
        else:
            self.healthy = True

    def record_quality(self, ok: bool) -> None:
        self.quality_requests += 1
        if not ok:
            self.quality_failures += 1

    def reset(self) -> None:
        self.requests = 0
        self.failures = 0
        self.total_latency = 0.0
        self.last_error = ""
        self.last_error_time = ""
        self.healthy = True
        self.quality_requests = 0
        self.quality_failures = 0

    def snapshot(self) -> dict[str, Any]:
        return {
            "requests": self.requests,
            "failures": self.failures,
            "success_rate": round(self.success_rate, 4),
            "average_latency": round(self.average_latency, 6),
            "last_error": self.last_error,
            "healthy": self.healthy,
            "quality_requests": self.quality_requests,
            "quality_failures": self.quality_failures,
        }


class ProviderMetricsRegistry:
    """Holds one ``ProviderMetrics`` per provider name."""

    __slots__ = ("_metrics",)

    def __init__(self) -> None:
        self._metrics: dict[str, ProviderMetrics] = {}

    def get(self, name: str) -> ProviderMetrics:
        if name not in self._metrics:
            self._metrics[name] = ProviderMetrics()
        return self._metrics[name]

    def record(self, name: str, *, latency: float, error: str = "") -> None:
        self.get(name).record(latency=latency, error=error)

    def record_quality(self, name: str, ok: bool) -> None:
        self.get(name).record_quality(ok)

    def snapshot(self) -> dict[str, dict[str, Any]]:
        return {name: m.snapshot() for name, m in self._metrics.items()}

    def reset(self) -> None:
        self._metrics.clear()
