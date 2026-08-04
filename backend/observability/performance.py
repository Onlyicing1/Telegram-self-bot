"""
Performance report generator — periodic performance summaries.

Reuses EngineMetrics, ProviderManager.metrics_snapshot(), and
diagnostics events. No duplicated counters.
"""
from __future__ import annotations

from typing import Any


def performance_report(owner_id: int = 0) -> dict[str, Any]:
    """Generate a performance summary.

    Reads from:
      - Engine.metrics_snapshot()  (latency, tokens)
      - ProviderManager.metrics_snapshot()  (per-provider latency)
      - diagnostics.filter_events()  (slow operations, errors)
      - runtime_status()  (memory, worker health)
    """
    from backend.ai.engine.engine import get_engine
    from backend.observability.runtime_status import runtime_status
    from backend.diagnostics import filter_events

    try:
        engine = get_engine()
        metrics = engine.metrics_snapshot()
        provider_metrics = engine.provider_manager.metrics_snapshot()
    except Exception:
        metrics = {}
        provider_metrics = {}

    runtime = runtime_status()

    slow_ops = _extract_slow_operations(filter_events(limit=100))
    error_events = filter_events(errors_only=True, limit=20)

    avg_latency = metrics.get("average_latency", 0.0)
    max_latency = metrics.get("max_latency", 0.0)
    min_latency = metrics.get("min_latency", 0.0)

    most_expensive_providers = _rank_providers_by_latency(provider_metrics)

    return {
        "average_response_time_s": avg_latency,
        "min_response_time_s": min_latency,
        "max_response_time_s": max_latency,
        "slowest_operations": slow_ops,
        "most_expensive_providers": most_expensive_providers,
        "memory_mb": runtime.get("memory_mb", 0),
        "cpu_time_s": runtime.get("cpu_time_s", 0),
        "pending_tasks": runtime.get("pending_tasks", 0),
        "background_loops": runtime.get("background_loops", {}),
        "watchdog_ok": runtime.get("watchdog_ok", False),
        "recent_errors": len(error_events),
        "total_ai_requests": metrics.get("total_executions", 0),
        "total_tokens": metrics.get("total_prompt_tokens", 0) + metrics.get("total_completion_tokens", 0),
    }


def _extract_slow_operations(events: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Extract the 5 slowest operations from diagnostics events."""
    timed = [e for e in events if isinstance(e.get("duration_ms"), (int, float)) and e["duration_ms"] > 0]
    timed.sort(key=lambda e: e.get("duration_ms", 0), reverse=True)
    result = []
    for e in timed[:5]:
        result.append({
            "module": e.get("module", ""),
            "action": e.get("action", ""),
            "duration_ms": e.get("duration_ms", 0),
            "result": e.get("result", ""),
        })
    return result


def _rank_providers_by_latency(provider_metrics: dict[str, dict[str, Any]]) -> list[dict[str, Any]]:
    """Rank providers by average latency (descending)."""
    ranked = []
    for name, stats in provider_metrics.items():
        ranked.append({
            "provider": name,
            "requests": stats.get("requests", 0),
            "average_latency": stats.get("average_latency", 0),
            "success_rate": stats.get("success_rate", 0),
            "healthy": stats.get("healthy", False),
        })
    ranked.sort(key=lambda p: p.get("average_latency", 0), reverse=True)
    return ranked
