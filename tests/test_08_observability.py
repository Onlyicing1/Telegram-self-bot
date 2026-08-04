"""
Observability layer tests — verifies all observability modules
aggregate correctly from existing infrastructure without duplicating state.
"""
from __future__ import annotations

import pytest


@pytest.mark.asyncio
async def test_runtime_status_returns_fields():
    from backend.observability.runtime_status import runtime_status
    status = runtime_status()
    assert "telegram_connected" in status
    assert "runtime_state" in status
    assert "memory_mb" in status
    assert "cpu_time_s" in status
    assert "pending_tasks" in status
    assert "uptime_s" in status
    assert "restart_count" in status
    assert "background_loops" in status


@pytest.mark.asyncio
async def test_ai_statistics_returns_fields():
    from backend.observability.ai_stats import ai_statistics
    stats = ai_statistics()
    assert "available" in stats
    assert stats["available"] is True
    assert "total_requests" in stats
    assert "success_rate" in stats
    assert "failure_rate" in stats
    assert "average_latency_s" in stats
    assert "total_tokens" in stats
    assert "provider_usage" in stats
    assert "active_provider" in stats


@pytest.mark.asyncio
async def test_database_statistics_returns_fields():
    from backend.observability.db_stats import database_statistics
    stats = database_statistics()
    assert "available" in stats
    assert stats["available"] is True
    assert "total_sessions" in stats
    assert "total_messages" in stats
    assert "long_term_memories" in stats
    assert "permanent_memories" in stats
    assert "tool_history_size" in stats


@pytest.mark.asyncio
async def test_health_snapshot_aggregates_all():
    from backend.observability.health_snapshot import health_snapshot
    snap = health_snapshot()
    assert "status" in snap
    assert "overall_healthy" in snap
    assert "checks" in snap
    assert "runtime" in snap
    assert "ai" in snap
    assert "database" in snap


@pytest.mark.asyncio
async def test_crash_report_generates_structured_output():
    from backend.observability.crash_report import generate_crash_report, format_crash_report
    exc = ValueError("Test crash")
    report = generate_crash_report(
        component="test_component",
        exc=exc,
        active_provider="dummy",
        active_session="sess-test",
    )
    assert report["trace_id"] != ""
    assert report["component"] == "test_component"
    assert report["exception_type"] == "ValueError"
    assert report["exception_message"] == "Test crash"
    assert "stack_trace" in report
    assert report["active_provider"] == "dummy"
    assert report["active_session"] == "sess-test"
    assert "runtime_state" in report

    formatted = format_crash_report(report)
    assert "CRASH REPORT" in formatted
    assert "test_component" in formatted


@pytest.mark.asyncio
async def test_performance_report_returns_fields():
    from backend.observability.performance import performance_report
    report = performance_report()
    assert "average_response_time_s" in report
    assert "slowest_operations" in report
    assert "most_expensive_providers" in report
    assert "memory_mb" in report
    assert "background_loops" in report
    assert "watchdog_ok" in report
    assert isinstance(report["slowest_operations"], list)
    assert isinstance(report["most_expensive_providers"], list)


@pytest.mark.asyncio
async def test_maintenance_run_all():
    from backend.observability.maintenance import run_all_maintenance
    result = run_all_maintenance()
    assert "cleanup_expired_memory" in result
    assert "cleanup_old_diagnostics" in result
    assert "validate_repositories" in result
    assert "validate_runtime_state" in result
    assert result["validate_repositories"]["ok"] is True


@pytest.mark.asyncio
async def test_maintenance_validate_repositories():
    from backend.observability.maintenance import validate_repositories
    result = validate_repositories()
    assert result["ok"] is True
    assert "results" in result
    assert "session" in result["results"]
    assert "message" in result["results"]


@pytest.mark.asyncio
async def test_maintenance_validate_runtime_state():
    from backend.observability.maintenance import validate_runtime_state
    result = validate_runtime_state()
    assert "runtime_state" in result
    assert "supervisor_ok" in result
    assert "active_loops" in result
    assert "issues" in result
    assert isinstance(result["issues"], list)


@pytest.mark.asyncio
async def test_maintenance_cleanup_expired_memory():
    from backend.observability.maintenance import cleanup_expired_memory
    result = cleanup_expired_memory()
    assert result["action"] == "cleanup_expired_memory"
    assert "deleted" in result
    assert result["ok"] is True


@pytest.mark.asyncio
async def test_no_duplicated_metrics():
    """Verify observability modules don't duplicate EngineMetrics state."""
    from backend.observability.ai_stats import ai_statistics
    from backend.ai.engine.engine import get_engine

    engine = get_engine()
    before = engine.metrics_snapshot()
    stats = ai_statistics()
    after = engine.metrics_snapshot()

    # Observability should not modify engine metrics
    assert before["total_executions"] == after["total_executions"]
    assert stats["total_requests"] == before["total_executions"]


@pytest.mark.asyncio
async def test_tool_usage_frequency():
    from backend.observability.ai_stats import tool_usage_frequency
    freq = tool_usage_frequency(owner_id=1)
    assert isinstance(freq, dict)
