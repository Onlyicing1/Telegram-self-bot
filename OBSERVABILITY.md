# Observability — LifeOS Telegram Self-Bot

> The owner can understand system health without reading source code.

---

## 1. Runtime Monitoring

### `/api/status` — Runtime Status

Exposes process-level and worker metrics:

| Field | Description |
|-------|-------------|
| `telegram_connected` | Whether the Telethon client is connected |
| `runtime_state` | Current FSM state (READY, DEGRADED, RECOVERING, etc.) |
| `supervisor_ok` | Whether the runtime supervisor is healthy |
| `uptime_s` | Process uptime in seconds |
| `restart_count` | Number of process restarts |
| `client_generation` | Telethon client generation (increments on reconnect) |
| `memory_mb` | Peak RSS memory in MB |
| `cpu_time_s` | Total CPU time (user + system) in seconds |
| `pending_tasks` | Number of pending asyncio tasks |
| `task_states` | Per-task state dict |
| `background_loops` | Per-loop state and last success time |
| `heartbeat_age_s` | Age of last heartbeat in seconds |
| `rpc_latency_ms` | Last Telegram RPC latency in ms |
| `ai_status` | AI subsystem status (engine health, active provider/model, provider count, total requests) |
| `queue_sizes` | Telegram update queue sizes |

**Source:** `backend/observability/runtime_status.py` — reads from `backend.health.snapshot()` and `resource.getrusage()`.

---

## 2. Diagnostics

### `/api/diagnostics/events` — Event Ring

Query parameters: `limit` (default 50), `module` (optional filter), `errors_only` (bool).

Returns recent diagnostics events from the in-memory ring (bounded at 500 entries):

```json
{
  "events": [
    {
      "ts": "2026-08-04T12:00:00+00:00",
      "module": "runtime",
      "action": "build_client",
      "duration_ms": 12.3,
      "result": "SUCCESS",
      "details": "gen=1"
    }
  ],
  "count": 1
}
```

**Source:** `backend/diagnostics.py` — the single event ring used by all subsystems.

### Trace Flow

Every important runtime event is logged with a `[TRACE]` tag:

```
[TRACE] SELF_CONNECTED gen=1 user=Parham id=123456 t=12345678.123
[TRACE] WATCHDOG_RECOVERY reason=full attempt=1 backoff_delay=4.0s t=12345695.000
[TRACE] TASK_CRASHED task=lifeos-keepalive exc_type=ConnectionError exc_repr=... t=12345700.000
```

Trace events carry monotonic timestamps (`t=<seconds>`) for precise delta computation.

**Source:** `backend/runtime/tracer.py` — `trace()`, `trace_exception()`, `trace_task_crash()`.

---

## 3. Health System

### `/health` — Unified Health Check

Returns the existing `unified_snapshot()` from `backend.runtime.health_check`:

```json
{
  "status": "ok",
  "overall_healthy": true,
  "runtime_state": "READY",
  "uptime_s": 3600.5,
  "restart_count": 0,
  "checks": {
    "telegram": {"healthy": true, "connected": true, ...},
    "supabase": {"healthy": true, "available": true},
    "ai_providers": {"healthy": true, "active_provider": "dummy", ...},
    "memory_manager": {"healthy": true, "short_count": 0, ...},
    "background_workers": {"healthy": true, "workers": {...}, ...},
    "runtime_manager": {"healthy": true, "active_sessions": 1, ...},
    "diagnostics": {"healthy": true, "event_count": 42}
  }
}
```

### `/api/health/snapshot` — Extended Health Snapshot

Adds runtime, AI, and database statistics on top of the unified health check:

```json
{
  "status": "ok",
  "overall_healthy": true,
  "runtime_state": "READY",
  "uptime_s": 3600.5,
  "restart_count": 0,
  "checks": {...},
  "runtime": {...},
  "ai": {...},
  "database": {...}
}
```

**Source:** `backend/observability/health_snapshot.py` — aggregates from `health_check`, `runtime_status`, `ai_stats`, and `db_stats`.

---

## 4. Statistics

### `/api/ai/stats` — AI Statistics

| Field | Description |
|-------|-------------|
| `total_requests` | Total AI execution count |
| `successful_requests` | Successful execution count |
| `failed_requests` | Failed execution count |
| `success_rate` | Success ratio (0.0–1.0) |
| `failure_rate` | Failure ratio (0.0–1.0) |
| `average_latency_s` | Mean execution latency in seconds |
| `min_latency_s` / `max_latency_s` | Latency extremes |
| `total_prompt_tokens` | Cumulative prompt tokens |
| `total_completion_tokens` | Cumulative completion tokens |
| `total_tokens` | Sum of prompt + completion tokens |
| `conversation_count` | Distinct owner conversations |
| `provider_usage` | Per-provider call count |
| `model_usage` | Per-model call count |
| `failure_counts` | Per-error-message count |
| `active_provider` | Currently active provider name |
| `active_model` | Currently active model name |
| `provider_metrics` | Per-provider health, latency, success rate |
| `cost_estimation` | Estimated cost (USD) based on per-provider token rates |
| `tool_usage` | Tool call frequency from tool history repository |

**Source:** `backend/observability/ai_stats.py` — reads from `Engine.metrics_snapshot()` and `ProviderManager.metrics_snapshot()`.

### `/api/db/stats` — Database Statistics

| Field | Description |
|-------|-------------|
| `supabase_available` | Whether Supabase is connected |
| `total_sessions` | Total AI session count |
| `total_messages` | Total message count across sessions |
| `long_term_memories` | Long-term memory entry count |
| `permanent_memories` | Permanent memory entry count |
| `tool_history_size` | Tool history record count |
| `provider_stats` | Per-provider statistics records |
| `database_latency_ms` | Last measured DB operation latency (from diagnostics ring) |
| `slow_queries` | List of slow DB operations (>= 500ms, from diagnostics ring) |

**Source:** `backend/observability/db_stats.py` — reads from `RepositoryManager` repositories and `diagnostics` event ring.

---

## 5. Crash Reports

When a fatal exception occurs, `generate_crash_report()` produces a structured report:

```python
from backend.observability.crash_report import generate_crash_report

report = generate_crash_report(
    component="bio_engine",
    exc=exc,
    active_provider="gemini",
    active_session="sess-123",
)
```

The report includes:

| Field | Description |
|-------|-------------|
| `trace_id` | Unique crash identifier |
| `timestamp` | UTC ISO timestamp |
| `component` | Which subsystem crashed |
| `exception_type` | Exception class name |
| `exception_message` | Exception message |
| `stack_trace` | Full stack trace |
| `runtime_state` | Runtime FSM state at crash time |
| `active_provider` | AI provider active at crash time |
| `active_session` | Session ID active at crash time |
| `memory_summary` | Memory, CPU, and pending task summary at crash time |
| `memory_mb` | Memory usage at crash time |
| `pending_tasks` | Pending asyncio tasks at crash time |
| `uptime_s` | Process uptime at crash time |
| `restart_count` | Process restart count |
| `ai_status` | AI subsystem status snapshot at crash time |

The crash is also logged via `tracer.trace_exception()` with the `[TRACE] FATAL_EXCEPTION` tag.

**Source:** `backend/observability/crash_report.py`.

---

## 6. Performance Reports

### `/api/performance` — Performance Summary

| Field | Description |
|-------|-------------|
| `average_response_time_s` | Mean AI response time |
| `min_response_time_s` / `max_response_time_s` | Latency extremes |
| `slowest_operations` | Top 5 slowest operations from diagnostics |
| `most_expensive_providers` | Providers ranked by average latency |
| `memory_mb` | Current memory usage |
| `memory_growth_mb` | Memory growth since last performance report sample |
| `cpu_time_s` | Total CPU time |
| `pending_tasks` | Pending asyncio tasks |
| `background_loops` | Per-loop state and health |
| `background_loops_health` | Summary of loop health (total, running, all_healthy) |
| `recent_errors` | Count of recent error events |
| `total_ai_requests` | Total AI request count |
| `total_tokens` | Total tokens consumed |

**Source:** `backend/observability/performance.py` — reads from `EngineMetrics`, `ProviderManager`, and `diagnostics`.

---

## 7. Maintenance

### `/api/maintenance` — Maintenance Report

Runs all safe maintenance operations and returns a combined report:

| Operation | Description |
|-----------|-------------|
| `cleanup_expired_memory` | Deletes expired long-term memories |
| `cleanup_old_diagnostics` | Reports on diagnostics ring status |
| `cleanup_old_statistics` | Resets AI engine metrics when they exceed 10,000 executions |
| `validate_repositories` | Verifies all repositories are reachable |
| `validate_runtime_state` | Checks runtime state consistency |

All operations are non-destructive. No data is lost. The cleanup only removes
entries that are already expired or resets aggregate counters.

**Source:** `backend/observability/maintenance.py`.

---

## 8. API Endpoint Summary

| Method | Path | Description |
|--------|------|-------------|
| GET | `/health` | Unified health check (existing) |
| GET | `/api/status` | Runtime status (memory, CPU, workers, AI, queues) |
| GET | `/api/ai/stats` | AI execution statistics |
| GET | `/api/db/stats` | Database repository statistics |
| GET | `/api/health/snapshot` | Extended health snapshot |
| GET | `/api/performance` | Performance summary |
| GET | `/api/diagnostics/events` | Diagnostics event ring |
| GET | `/api/maintenance` | Maintenance report |

---

## 9. Architecture

```
backend/observability/
├── __init__.py
├── runtime_status.py    — process + worker + AI + queue metrics
├── ai_stats.py          — AI execution statistics + cost + model usage
├── db_stats.py          — database repository statistics + latency + slow queries
├── health_snapshot.py   — unified snapshot generator
├── crash_report.py      — crash report generator + memory summary
├── performance.py       — performance report generator + memory growth
└── maintenance.py       — safe maintenance utilities + statistics cleanup
```

All modules are pure aggregation layers. They read from existing infrastructure:

- `backend.health` — process health hub
- `backend.runtime.health_check` — unified health checker
- `backend.runtime.tracer` — structured event tracer
- `backend.diagnostics` — in-memory event ring
- `backend.ai.engine.metrics` — AI execution metrics
- `backend.ai.providers.manager` — provider metrics
- `backend.ai.database.manager` — repository manager

No duplicated metrics, health checks, or statistics. Everything reuses existing diagnostics infrastructure.
