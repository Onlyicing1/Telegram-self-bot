# INVESTIGATION

## INVESTIGATION METADATA

- Repository: `https://github.com/Onlyicing1/Telegram-self-bot`
- Branch: `main`
- Date: 2026-08-29
- Starting commit: `e594a497a8f5b552f4a8244dc69aa4b0d89a1988`
- Phase: Phase 1 database contract review; investigation/design only

## 1. PROBLEM

The durable AI Task/Scheduler feature needs restart-safe task definitions and execution occurrence history. This phase freezes the database contract only. It does not create migrations, execute SQL, add repositories, or implement scheduling.

## 2. SOURCE-BACKED CURRENT CONVENTIONS

### Identity and keys

- Telegram owner identity is a numeric `BIGINT`; existing tables store it as a plain `owner_id BIGINT NOT NULL`.
- Existing application tables generally use `bigserial` primary keys. AI sessions use a text `session_id`, and provider statistics use a composite primary key. No existing owner table is referenced by foreign key: current migrations explicitly use plain `owner_id` columns and describe the schema as having no owner foreign keys.
- The proposed task records therefore use `BIGINT owner_id` and stable `UUID` task/occurrence IDs only if the implementation dependency accepts UUID generation. Because the repository has no existing UUID primary-key convention, the safer repository-compatible choice is `BIGSERIAL` identifiers for both new tables; the immutable occurrence key remains a separate text value with a uniqueness constraint.

### Timestamps and JSON

Existing migrations use `TIMESTAMPTZ DEFAULT now()`. Application writes use UTC-aware Python timestamps and ISO-8601 strings. Existing structured payload columns use `JSONB DEFAULT '{}'` (AI tool arguments/results, memory metadata, bot-log context). Schedule and action payloads should use JSONB, with deterministic application validation and database size/count checks where feasible.

### Constraints and indexes

Existing migrations use inline `CHECK` constraints for finite statuses/roles/types, `UNIQUE` constraints for per-owner state, primary/composite keys, and btree indexes such as `(owner_id)`, `(owner_id, created_at DESC)`. There is no existing PostgreSQL enum type convention. New values should therefore use text columns with explicit CHECK constraints rather than database enum types.

### RLS and service role

Existing tables enable RLS and generally grant SELECT to `anon, authenticated`; backend writes use the Supabase service-role client, which bypasses RLS. Existing policies are permissive/select-oriented rather than request-user owner policies because the dashboard reads through the backend and the service role is authoritative. New task tables must still enable RLS and default to no public write grants; backend repositories use the service role and always add explicit owner filters. Any dashboard exposure requires a separately reviewed owner-scoped API.

### Database functions/triggers

No current scheduler or business execution is implemented in SQL. No trigger is justified for task scheduling, claiming, retries, or execution. Timestamps may use existing defaults; monotonic versioning and occurrence claiming belong in explicit repository compare-and-set operations so behavior is visible and testable.

## 3. CURRENT ARCHITECTURE RELEVANT TO THE CONTRACT

`RuntimeSupervisor` is the single lifecycle/recovery authority. `profile.scheduler` is a separate in-memory Bio/Username minute-boundary scheduler and is not reusable. The AI path remains `ai_unified` → `Engine` → `Dispatcher` → `ProviderManager` → `EngineResult`; registered actions are executed only through `ToolExecutor`, with Telegram controlled by the self-client/TelegramAPI. Existing persistence/repository layers are thin Supabase wrappers with in-memory fallback and bounded async database calls. No task table, execution table, task repository, or scheduler exists.

## 4. EXACTLY TWO-TABLE CONTRACT

The next schema implementation may introduce exactly two tables/concepts. No action or step table is permitted initially.

### A. `ai_tasks` — authoritative task definitions

| Column | Proposed type | Null/default | Contract |
|---|---|---|---|
| `id` | `bigserial` | NOT NULL, PK | Stable task identifier using the repository's established ID convention. |
| `owner_id` | `bigint` | NOT NULL | Authenticated Telegram owner; never model-supplied. |
| `label` | `text` | NOT NULL | Bounded display name. |
| `status` | `text` | NOT NULL, default `'active'` | CHECK: `active`, `paused`, `completed`, `failed`, `expired`, `deleted`. Only active tasks are due. |
| `version` | `integer` | NOT NULL, default `1` | Positive monotonic definition version. |
| `schedule_type` | `text` | NOT NULL | CHECK: `once`, `interval`, `daily`, `weekly`. |
| `schedule` | `jsonb` | NOT NULL | Versioned normalized schedule payload; deterministic validator owns semantics. |
| `timezone` | `text` | NOT NULL | Explicit valid IANA timezone identifier. Machine timezone is never implied. |
| `next_run_at` | `timestamptz` | NULL for terminal tasks | UTC due instant, indexed with status. |
| `actions` | `jsonb` | NOT NULL | Versioned bounded ordered safe-tool action list; no conditionals, loops, replanning, or arbitrary commands. |
| `notification_destination` | `jsonb` | NOT NULL | Explicit owner-scoped validated destination, not inferred at execution. |
| `created_at` | `timestamptz` | NOT NULL, default `now()` | Creation time. |
| `updated_at` | `timestamptz` | NOT NULL, default `now()` | Last definition/lifecycle update. |
| `terminal_at` | `timestamptz` | NULL | Set for completed/failed/expired/deleted tasks. |

Required constraints: positive version; nonblank bounded label; valid status and schedule type; non-null schedule/actions/destination; JSON action array with a bounded maximum count and bounded serialized size; schedule-type payload validation in application code; `next_run_at` required for active recurring/once tasks and null for terminal tasks where enforced safely. A database CHECK can enforce basic JSON shape/array length, but full tool/action validation remains application responsibility.

Required indexes: `ai_tasks(status, next_run_at)` for due active tasks; `ai_tasks(owner_id, updated_at DESC)` for owner management; optionally `ai_tasks(owner_id, status)` if repository query patterns require it. Do not add speculative indexes.

### B. `ai_task_occurrences` — durable occurrence/attempt history

| Column | Proposed type | Null/default | Contract |
|---|---|---|---|
| `id` | `bigserial` | NOT NULL, PK | Stable history row identifier. |
| `task_id` | `bigint` | NOT NULL | Foreign key to `ai_tasks.id` if the migration dependency/order permits it. |
| `owner_id` | `bigint` | NOT NULL | Denormalized owner for fast owner filtering and RLS; must match task owner. |
| `occurrence_key` | `text` | NOT NULL | Immutable deterministic key derived from task and normalized scheduled instant; unique with task ID. |
| `definition_version` | `integer` | NOT NULL | Version used for this occurrence. |
| `action_snapshot` | `jsonb` | NOT NULL | Immutable validated action snapshot, bounded and independent of later task edits. |
| `scheduled_for` | `timestamptz` | NOT NULL | UTC scheduled instant. |
| `attempt` | `smallint` | NOT NULL, default `1` | Total attempt number, CHECK 1–3. |
| `status` | `text` | NOT NULL, default `'claimed'` | CHECK: `claimed`, `running`, `succeeded`, `failed`, `retry_pending`, `cancelled`, `expired`, `interrupted`. |
| `claimed_at` | `timestamptz` | NULL | Durable claim time. |
| `started_at` | `timestamptz` | NULL | Execution start time. |
| `finished_at` | `timestamptz` | NULL | Terminal time. |
| `retry_at` | `timestamptz` | NULL | Next retry UTC instant, only for retry-pending state. |
| `error_metadata` | `jsonb` | NOT NULL, default `'{}'` | Bounded safe error classification/details; no secrets. |
| `result_metadata` | `jsonb` | NOT NULL, default `'{}'` | Bounded safe result summary; no sensitive provider output. |
| `created_at` | `timestamptz` | NOT NULL, default `now()` | History insertion time. |
| `updated_at` | `timestamptz` | NOT NULL, default `now()` | Last state transition. |

Required constraints: foreign key `task_id → ai_tasks.id` with deliberate delete behavior; positive owner ID policy consistent with existing schema (no new owner FK invented); owner/task consistency enforced by repository transaction or a database constraint/trigger only if later proven necessary; positive attempt and maximum 3; valid statuses; retry timestamp only for retry-pending; immutable occurrence identity; bounded JSON metadata/action snapshot.

Required indexes: unique `(task_id, occurrence_key)`; `(owner_id, scheduled_for DESC)` for history; `(task_id, scheduled_for DESC)` for task history; `(status, retry_at)` for due retries only if the repository needs that query. Keep the unique index as the duplicate-claim protection.

## 5. VERSIONING AND OCCURRENCE MODEL

A task row's `version` starts at 1 and increments only through compare-and-set update. An occurrence stores both the version and immutable action snapshot at claim/creation time. Editing a task changes future unclaimed work only. The scheduler creates or claims one occurrence using a deterministic key based on task ID plus the normalized UTC scheduled instant (including an explicit recurrence identity where needed). The unique `(task_id, occurrence_key)` constraint makes duplicate occurrence creation fail atomically. This is idempotency protection for occurrence records, not exactly-once Telegram execution.

## 6. RLS AND SERVICE-ROLE DESIGN

Both tables must enable RLS. Because the current backend uses a service-role Supabase client and existing policies do not establish authenticated-user identity mapping, the initial migration should grant no public INSERT/UPDATE/DELETE and expose only the minimum read policy required by existing conventions, preferably no direct client access. Repository methods must always filter by the authoritative owner ID even when service role bypasses RLS. If future dashboard reads are added, they require a dedicated backend owner-scoped API and reviewed policies; do not rely on RLS alone.

The task foreign key is the one relationship directly required by the model. Existing schema has no owner identity table, so no owner foreign key can be safely added from current source evidence. `owner_id` consistency must be checked in repository operations and validated on load.

## 7. RETENTION

Task definitions are durable until owner deletion/terminal cleanup according to an explicit future policy. Occurrence history should be retained for a bounded period sufficient for debugging, retry/idempotency audit, and restart reconciliation; a conservative initial recommendation is 90 days, with terminal history cleanup performed by a bounded maintenance operation rather than a database trigger. Retention is a product/operations setting and must be confirmed before migration if deletion semantics matter. No automatic cleanup logic is part of this phase.

## 8. MIGRATION ORDER AND ROLLBACK

1. Review this contract and exact names/types with the repository owner.
2. Create `ai_tasks` first, including owner/status/schedule/action checks and due indexes.
3. Create `ai_task_occurrences` second with the task foreign key, unique occurrence index, history indexes, and RLS.
4. Add repository/fallback code in a later implementation phase.
5. Add scheduler only after repositories and deterministic schedule validation exist.

Migrations must be additive and idempotent according to current migration style. Rollback must be an explicitly reviewed reverse migration that drops occurrence table before task table only when no production data must be preserved; never perform destructive rollback automatically. No migration or SQL was created or executed in this phase.

## 9. SECURITY REVIEW

Owner identity is taken from authenticated self-bot context. Persisted task JSON is untrusted and validated on every load. Only allowlisted safe ToolRegistry actions are eligible; confirmation-required, admin-only, and destructive actions are excluded initially. No scheduler/database operation may call AI, providers, Telegram, arbitrary RPC, SQL, shell, or timers. Explicit destinations are validated and owner-scoped. Error/result metadata are bounded and secret-free. At-least-once execution is the only honest side-effect guarantee.

## 10. REMAINING BLOCKERS

- The repository proves `BIGINT owner_id`, `bigserial`, `TIMESTAMPTZ`, JSONB, text CHECK constraints, btree indexes, and service-role access, but it does not prove a UUID convention or a shared owner table; therefore UUID owner/task foreign keys must not be invented.
- Exact live Supabase RLS state cannot be established without live access, which was not performed.
- Retention duration and terminal task deletion semantics need owner approval before migration.
- Exact JSON byte-size/count limits and schedule-payload shapes require implementation-level validation constants; their table columns are settled but not numeric thresholds in this phase.
- Whether a task destination should default to a configured owner chat remains unresolved because no existing task notification setting is present.

## 11. CONFIRMED FINDINGS

- Exactly two durable concepts are required by the restart, claim, version, retry, and history contract: task definitions and occurrences/attempts.
- Existing source uses numeric Telegram `BIGINT owner_id`, `bigserial` IDs, UTC `TIMESTAMPTZ`, JSONB payloads, text CHECK constraints, btree indexes, RLS, and service-role backend writes.
- No existing table can safely represent task definitions or occurrence claims.
- No SQL, migration, database state, or production code was changed.

## 12. PROPOSED / LIKELY FINDINGS

- `ai_tasks` and `ai_task_occurrences` are the smallest compatible names and schema concepts, subject to final migration review.
- A task foreign key is appropriate; an owner foreign key is not supported by current source because no owner table exists.
- Two tables remain sufficient while actions are bounded JSON and execution history is aggregate; a step table is deferred.
- No trigger or database business logic is justified.

## 13. TEST / VALIDATION PLAN FOR NEXT PHASES

Schema tests must verify migration creation, constraints, indexes, RLS posture, owner filtering, task/occurrence FK consistency, unique occurrence claims, bounded JSON/action validation, timestamp serialization, and service-role repository behavior. Repository tests must cover Supabase failure fallback, compare-and-set version updates, duplicate claims, and safe status transitions. Scheduler tests come later and must cover DST, missed runs, retries, restart reconciliation, and at-least-once semantics.

## 14. PHASE BOUNDARY

This was database contract investigation/design only. Production code changed: **NO**. Tests changed: **NO**. Migrations changed: **NO**. SQL executed: **NO**. Supabase changed: **NO**. Telegram behavior changed: **NO**. Commit made: **NO**. Push made: **NO**.
