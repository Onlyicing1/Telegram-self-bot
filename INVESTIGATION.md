# INVESTIGATION — Phase 3: Durable History state, schema and repository design

**Status: investigation and design only. Nothing in this document is implemented,
applied or deployed.** No production Python, test, migration or Supabase object
was changed by this phase. The proposed SQL is unapproved design material, is not
a migration file, and must not be executed by an agent.

Base verified before investigation: `62a18de07f1bc5bf8b51c36438f66ad8c4f4d498`
(Phase 2), which descends from Phase 1 `ffe72fd0bce9286e3d3d6583614b3b243bfd4fdf`.
The branch and working tree were checked before any edit.

---

## 1. Current source architecture and data flow

The actual History path is:

```
Telegram entity resolution
  backend/telegram_api/entities.get_entity
        ↓
History retrieval
  backend/services/history_service.fetch_recent_history / _collect / _fetch_page
        ↓
Message normalization
  history_service._normalize → HistoryMessage (frozen dataclass)
        ↓
Provenance filtering
  has_ai_provenance_marker / strip_ai_provenance_marker
        ↓
AI summarization / translation
  backend/services/history_ai_service._prepare → _chunk_history → ProviderManager
        ↓
Result assembly
  history_ai_service._finish/_result_data
        ↓
Existing delivery
  backend/bot/handlers/history._run_summary → ExistingPanelEditor (Helper Bot)
```

### Source facts

- **Entity resolution.** `history_service.classify_chat` calls
  `telegram_api.entities.get_entity` (a bounded Telethon `get_entity`) and then the
  Phase 1 pure `classify_entity`. Phase 2's `history_policy` consumes that type.
  Classification is live; nothing is cached.
- **Retrieval.** `history_service` accepts a `TelegramAPI` facade or its raw
  client. Every page goes through `telegram_api.messages.iter_messages`, which
  passes `limit`, `min_id` and `max_id` to Telethon. Both bounds are exclusive.
  Each page is bounded by `rpc_await` with `HISTORY_RPC_TIMEOUT_S = 5.0`.
- **Normalization.** `HistoryMessage` carries `message_id`, `chat_id`,
  `sender_id`, `out`, `date`, `text`, `reply_to_msg_id`, `has_media` and
  `ai_provenance`. Text is full, never truncated. Media is never downloaded.
- **Provenance.** The durable AI marker is detected from message text; marked
  messages are excluded by default (`include_ai=False`) and their marker is
  stripped before any consumer sees text. This is the central eligibility rule,
  not a per-caller heuristic.
- **AI work.** `_prepare` retrieves eligible history, `_chunk_history` drops
  messages with no text from the model payload, and map/reduce calls go through
  `ProviderManager.chat`. The panel passes `with_identifiers=False`, so the model
  receives no message ids, sender ids or attribution. The default for the AI tools
  is `with_identifiers=True`.
- **Result assembly.** `_result_data` contains counts and, notably,
  `first_message_id` and `last_message_id`. The panel renders a range line. No
  message body is returned in this metadata.
- **Delivery.** `_run_summary` edits the original panel through the Helper Bot's
  inline edit; it never sends a new message, never uses Saved Messages, and never
  writes a database row.

### Current bounds (source constants)

| Constant | Value | Location |
|---|---:|---|
| `MAX_HISTORY_MESSAGES` | 1000 | `backend/services/history_service.py` |
| `DEFAULT_PAGE_SIZE` | 100 | same |
| `MAX_PAGE_SIZE` | 1000 | same |
| `MAX_HISTORY_SCAN_MESSAGES` | 5000 raw messages per request | same |
| `HISTORY_RPC_TIMEOUT_S` | 5.0 seconds per page | same |
| `MAP_CONCURRENCY` | 2 | `history_ai_service` |
| `MAX_CALL_SPACING_S` | 5.0 seconds | same |
| Product caps | bot/channel 300; user/group/supergroup 1000 | `backend/services/history_policy.py` |

### What is not persisted today

There is **no History state table, repository, cursor, summary record or
migration** anywhere in `backend/`, `supabase/migrations/` or `tests/` (verified by
search before writing this document). Every retrieval and summary is one-shot:

- The sum of the current turn exists only in the `HistoryPlan`, chunk lists and
  provider response in memory.
- The panel's selected chat, count, focus and `peer_type` live in the module-level
  `_STATE` object in `backend/bot/handlers/history.py`; result pages live in a
  bounded in-memory page-state cache. Both are lost on process restart.
- The delivered summary exists only as the text of the edited Telegram panel
  message. No application code can read it back as state.
- AI tool call results are not written by `history_ai_service`; that service has no
  persistence import or DB call.

Consequences:

- After a restart, a new History request retrieves old Telegram messages again.
- A summary that was generated but could not be delivered is lost.
- There is no cumulative summary and no checkpoint, so a future scheduled History
  run cannot continue where a previous one stopped.
- A deleted Telegram message cannot be recovered; today it simply disappears from
  a later fetch. That is honest but also means the old result is not reproducible
  from Telegram.

### Message IDs today

Message IDs are used as:

1. Telethon `min_id`/`max_id` retrieval bounds;
2. the chunk ordering key and translation line key (`[id] text`);
3. `first_message_id`/`last_message_id` in `_result_data`;
4. the panel's displayed range.

They are **not** durable content storage. A Telegram message ID is a reference to
a message that Telegram may delete, hide or make inaccessible. It must never be
treated as a promise that the content still exists.

---

## 2. Existing persistence mechanisms and limitations

### `ai_tasks`

Definition table: `id bigserial PK`, `owner_id`, `label`, `status`, `version`,
`schedule_type`, `schedule jsonb` (16 KiB), `timezone`, `next_run_at`, `actions
jsonb` (32 KiB, 1–5 actions except Todo), `notification_destination jsonb` (4 KiB),
`ai_instruction text` (16 KiB), timestamps. Version is the CAS counter.

It is a **task definition**, not execution state. Storing a cumulative summary or
cursors here would rewrite the definition, invalidate prepared actions, and mix
content with scheduling. It is unsuitable.

### `ai_task_occurrences`

Execution ledger: `task_id` (`ON DELETE RESTRICT`), `owner_id`,
`occurrence_key`, `definition_version`, `action_snapshot`, `scheduled_for`,
`attempt` (1–3), `status` (claimed/running/succeeded/failed/retry_pending/
cancelled/expired/interrupted/waiting_answer), timestamps,
`error_metadata`/`result_metadata`/`preparation_metadata` (each **8192 bytes**).

**Verified:** `ai_task_occurrences_result_size` is
`octet_length(result_metadata::text) <= 8192`; `MAX_METADATA_BYTES = 8192` in
`task_execution.py`. `result_metadata` is owned by the occurrence's action/park/
terminal lifecycle and is overwritten as the chain progresses. It is not a
canonical cumulative summary store, and its 8 KiB bound cannot hold a long-lived
summary. It is unsuitable.

### `notification_destination`

`TaskExecutionCoordinator.execute` reads the task's
`notification_destination.chat_id` and injects it into
`ToolContext.extra["chat_id"]`. The current History tools read that key as their
source chat. For scheduled History this would make the **delivery destination**
double as the **read source**, which is explicitly wrong and can send summaries
into the monitored chat. The destination is delivery-only; the state row must own
the source peer.

### Repository conventions

`TaskRepository` has a Supabase implementation and an `InMemoryTaskRepository`
fallback; `RepositoryManager` is the process-wide singleton. CAS is expressed as
`.eq("version", expected_version)` (task/step) or `.eq("status", current_status)`
(occurrence claim/transition); one row returned means the write won, zero means
stale. `delete_task` deletes occurrences first because of `ON DELETE RESTRICT`.

The task repository's in-memory fallback is acceptable for task definitions where
a degraded run is visible, but it is **not acceptable for a durable History
checkpoint**: silently continuing after a Supabase failure would advance a cursor
in RAM, lose it on restart, and permanently skip unprocessed content. The future
History state repository must be durable-only for writes and fail closed when the
durable store is unavailable.

### Migration and documentation constraints

- Repository has **41 migration files**: 40 schema migrations plus the separately
  applied `harden_public_schema_privileges` migration. Latest filename is
  `20261112160000_add_panel_settings_intelligent_reaction_probability.sql`.
- `DATABASE_ARCHITECTURE.md` is the repository's declared schema authority
  (§24: "This document is authoritative", and "Update this document before writing
  the migration"). It currently documents 27 tables in its overview, 28 live tables
  in its audit, and the 17-part canonical setup script in §31.3.
- The suite enforces that **every migration's table is classified in the document**
  (`test_no_migration_creates_a_table_that_the_documentation_does_not_classify`)
  and that §31.3 is statement-identical to its embedded migrations
  (`test_database_setup_order`). A new migration without the corresponding
  document update fails those tests.
- Therefore a future Phase 3 implementation cannot legally or test-wise add the
  migration first: the owner must approve an update to
  `DATABASE_ARCHITECTURE.md` (new table section, §22 status, §31.3 canonical part,
  and the table count) before the migration is written/applied. This phase does
  **not** modify that protected document.

---

## 3. Proposed durable state model

### Decision

Use **one dedicated table, `history_schedules`**, one row per durable History
task and one row per `(owner_id, peer_id)`.

Why this shape:

- It follows the existing durable-task architecture (`ai_tasks` +
  `ai_task_occurrences`) instead of inventing a second scheduler/store.
- A History state is meaningful only while a task exists to read it; `task_id`
  gives lifecycle and ownership a single anchor.
- The cumulative summary is per peer, not per occurrence. `UNIQUE
  (owner_id, peer_id)` prevents two tasks from silently producing two divergent
  summaries for the same chat.
- It does not overload `ai_tasks.actions`, `ai_task_occurrences.result_metadata`
  or `notification_destination`.

The state row stores **the summary and its checkpoints**, not Telegram message
bodies. It does not replace `classify_chat`: the peer type stored in the row is an
informational snapshot only.

### Proposed columns (summary)

| Column | PostgreSQL type | Null/default | Role |
|---|---|---|---|
| `task_id` | `bigint` | PK, NOT NULL, FK `ai_tasks(id) ON DELETE CASCADE` | One state per task |
| `owner_id` | `bigint` | NOT NULL | Owner scope; every query filters it |
| `peer_id` | `bigint` | NOT NULL, `<> 0` | The Telegram peer the task reads |
| `peer_type_snapshot` | `text` | NULL | Last observed `PeerType`; informational only |
| `baseline_message_id` | `bigint` | NOT NULL DEFAULT 0, `>= 0` | Start boundary for a new schedule |
| `last_scanned_message_id` | `bigint` | NULL, `>= baseline` | Durable disposition cursor |
| `last_summarized_message_id` | `bigint` | NULL, `<= scanned` | Content coverage cursor |
| `summary_text` | `text` | NOT NULL DEFAULT `''`, `<= 32768` bytes | The one cumulative summary |
| `summary_revision` | `integer` | NOT NULL DEFAULT 0, `>= 0` | Increments only when summary text changes |
| `status` | `text` | NOT NULL DEFAULT `'active'`, CHECK `active/degraded/blocked` | State health, not scheduling authority |
| `last_success_at` | `timestamptz` | NULL | Last successful durable commit |
| `last_error_class` | `text` | NULL, `<= 64` chars | Stable diagnostic code |
| `last_error_detail` | `text` | NULL, `<= 512` bytes | Bounded, content-free diagnostic |
| `version` | `integer` | NOT NULL DEFAULT 1, `> 0` | CAS version |
| `created_at` | `timestamptz` | NOT NULL DEFAULT `now()` | Durable timestamp |
| `updated_at` | `timestamptz` | NOT NULL DEFAULT `now()` | Durable timestamp |

### Field-by-field justification

| Field | Why it is needed | Source requirement | Authoritative? | Derived/stale/sensitive? | Index? | Retention? |
|---|---|---|---|---|---|---|
| `task_id` | Binds state to the one existing durable task and its lifecycle | `ai_tasks` is the scheduler's authority; occurrence FK pattern | Yes (identity) | Not derivable; not sensitive | PK (implicit unique index); no extra index | Deleted with the task via cascade; no separate retention |
| `owner_id` | Every repository operation filters owner; prevents cross-owner access | Existing repository pattern | Yes | Denormalized from task; repository validates; not sensitive beyond owner identity | Second column of `UNIQUE (owner_id, peer_id)`; no separate index | Same row lifetime |
| `peer_id` | The source chat to read; never taken from delivery or model | `notification_destination` is delivery-only; current panel passes a numeric chat to retrieval | Yes (target) | Can become stale if Telegram migrates a basic group to a supergroup; sensitive (chat identifier) | Second column of the unique key; no separate index | Same row lifetime |
| `peer_type_snapshot` | Display/diagnostic "last known type" while a peer is unreachable | Phase 1 `PeerType`; Phase 2 policy | **No** | Stale by design; never used for policy; not sensitive | No | Overwritten each run; no history |
| `baseline_message_id` | Distinguishes "start from this point" from "no checkpoint yet"; prevents a new task from silently summarizing all history | New feature; current manual retrieval has no start concept | Yes (initial boundary) | Captured at creation; can be 0 for empty chat/backlog choice | No | Same row lifetime; needed to interpret cursors |
| `last_scanned_message_id` | Retrieval cursor: everything at or below it has a durable disposition (content or safe skip) | Current collector has only in-memory bounds; retry after restart needs this | Yes | Monotonic; can be stale only if a stale writer wins (CAS prevents); not content | No | Same row lifetime |
| `last_summarized_message_id` | Content coverage/reporting: all text content through here is represented in the summary or explicitly excluded | Needed to prove no gaps and to explain ranges | Yes for coverage | May be lower than scanned when only skipped tail messages exist; monotonic | No | Same row lifetime |
| `summary_text` | The durable result that survives restart and deleted messages | The core Phase 3 requirement | Yes (content) | Sensitive private chat content; bounded; no identifier metadata is added (user-authored text can itself contain identifiers) | No | Latest only; replaced on commit; hard 32 KiB ceiling |
| `summary_revision` | Distinguishes content revisions from status/error writes | Audit and future UI | Yes | Current value only; no revision bodies retained | No | Same row lifetime |
| `status` | Health of the state (`active/degraded/blocked`) | Requirement H; separate from task scheduling status | Informational | Can lag a transient failure; never used to schedule | No index initially; a future UI listing degraded states may justify `(owner_id, status)` | Current value only |
| `last_success_at` | Proves the last committed processing time | Requirement I | Informational | Stale only until next success; not sensitive | No | Overwritten; current value only |
| `last_error_class`/`last_error_detail` | Bounded diagnostics, safe to show/operate | Requirement J | Informational | Must never contain message text/provider raw output/credentials; bounded | No | Overwritten each failure; no history |
| `version` | Optimistic concurrency guard for every write | Existing task/step/occurrence CAS pattern | Yes | Incremented every write; not sensitive | No | Current value only |
| `created_at`/`updated_at` | Retention and operational audit | Existing table convention | Informational | Not sensitive | No | Same row lifetime; no time-based purge initially |

### Fields deliberately rejected

- **A separate `summary_updated_at`:** `last_success_at` plus `updated_at` cover
  it; a third timestamp would be redundant.
- **A separate `last_attempt_at`/attempt counter:** `ai_task_occurrences` already
  owns attempts, retry scheduling and terminal outcomes. Duplicating them in state
  would create two authorities.
- **A `schedule`/`destination` copy:** `ai_tasks` owns these; copying them into the
  state permits divergence and puts delivery data in a content table.
- **Stored message bodies, sender ids, peer titles/usernames:** no source
  requirement; unbounded and privacy-invasive. Only the summary and the peer id are
  necessary.
- **A summary-revision history table:** not required; bounded latest summary is
  the product requirement. History can be added only with an explicit retention
  decision later.

### Proposed table constraints

- `PRIMARY KEY (task_id)` — one state per task.
- `FOREIGN KEY (task_id) REFERENCES ai_tasks(id) ON DELETE CASCADE`.
- `UNIQUE (owner_id, peer_id)` — one cumulative state per peer; prevents silent
  duplicate schedules. (Open product decision; see §17.)

**Owner consistency.** `owner_id` is denormalized for owner-scoped queries, as it
is on `ai_task_occurrences`. There is no `(task_id, owner_id)` composite foreign
key because `ai_tasks` has no unique pair to reference; a mismatch is prevented by
the repository validating the task's owner at creation and by every read/write
filtering both `owner_id` and `task_id`. This mirrors the documented logical
`ai_task_occurrences.owner_id` relationship, not a new ownership model. A future
database trigger could enforce it; that is not proposed here.
- `peer_id <> 0`.
- `baseline_message_id >= 0`.
- `last_scanned_message_id IS NULL OR last_scanned_message_id >= baseline_message_id`.
- `last_summarized_message_id IS NULL OR
  (last_scanned_message_id IS NOT NULL AND
   last_summarized_message_id <= last_scanned_message_id)`.
- `octet_length(summary_text) <= 32768`.
- `summary_revision >= 0`, `version > 0`.
- `status IN ('active','degraded','blocked')`.
- `last_error_detail IS NULL OR octet_length(last_error_detail) <= 512`.
- `peer_type_snapshot IS NULL OR peer_type_snapshot IN
  ('private_user','private_bot','basic_group','supergroup','broadcast_channel','unknown')`.

No index beyond the PK and `UNIQUE (owner_id, peer_id)` is needed: the unique
index serves both the owner-scoped peer lookup and the owner prefix; reads by task
use the PK. A `(owner_id, status)` index is not justified until a UI actually lists
degraded states at scale. No field has an independent retention window: the row is
small, diagnostics and the summary are overwritten in place, and only the latest
summary is kept.

### Ownership, RLS and grants

The summary is private conversation content. This table must **not** follow the
legacy `USING (true)` public-read policy. Proposed posture, consistent with the
service-role-only precedents in §18 and the AED/Private Message hardening:

- `ALTER TABLE history_schedules ENABLE ROW LEVEL SECURITY;`
- **Zero policies.**
- `REVOKE ALL ON history_schedules FROM PUBLIC, anon, authenticated;`
- `GRANT SELECT, INSERT, UPDATE, DELETE ON history_schedules TO service_role;`
- No sequence exists (`task_id` is the PK; no `bigserial`), so no sequence grant
  is needed.
- The backend uses only the service-role client; every repository method filters
  `owner_id` and `task_id`. A task id or peer id from the model is never trusted.
- The React dashboard/API does not read Supabase directly in this design.

This deliberately conflicts with §24's generic "grant SELECT USING(true)" rule and
must be recorded in the approved `DATABASE_ARCHITECTURE.md` update before the
migration. The stricter posture is required because a cumulative summary is not a
panel setting.

### PROPOSED DESIGN SQL — UNAPPROVED, NOT EXECUTED

> This block is design material for review only. It is **not** a migration file, is
> not applied, and no agent may execute it. The table is not part of the live
> schema. A future migration must first be authorized and documented in
> `DATABASE_ARCHITECTURE.md`.

```sql
-- PROPOSED ONLY — NOT APPLIED. Do not execute as part of Phase 3.
CREATE TABLE IF NOT EXISTS history_schedules (
    task_id                   bigint      PRIMARY KEY
                              REFERENCES ai_tasks(id) ON DELETE CASCADE,
    owner_id                  bigint      NOT NULL,
    peer_id                   bigint      NOT NULL,
    peer_type_snapshot        text,
    baseline_message_id       bigint      NOT NULL DEFAULT 0,
    last_scanned_message_id   bigint,
    last_summarized_message_id bigint,
    summary_text              text        NOT NULL DEFAULT '',
    summary_revision          integer     NOT NULL DEFAULT 0,
    status                    text        NOT NULL DEFAULT 'active',
    last_success_at           timestamptz,
    last_error_class          text,
    last_error_detail         text,
    version                   integer     NOT NULL DEFAULT 1,
    created_at                 timestamptz NOT NULL DEFAULT now(),
    updated_at                 timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT history_schedules_peer_not_zero
        CHECK (peer_id <> 0),
    CONSTRAINT history_schedules_baseline_nonnegative
        CHECK (baseline_message_id >= 0),
    CONSTRAINT history_schedules_scanned_nonnegative
        CHECK (last_scanned_message_id IS NULL OR last_scanned_message_id >= 0),
    CONSTRAINT history_schedules_summarized_nonnegative
        CHECK (last_summarized_message_id IS NULL OR last_summarized_message_id >= 0),
    CONSTRAINT history_schedules_scanned_after_baseline
        CHECK (last_scanned_message_id IS NULL
               OR last_scanned_message_id >= baseline_message_id),
    CONSTRAINT history_schedules_summarized_not_after_scanned
        CHECK (last_summarized_message_id IS NULL
               OR (last_scanned_message_id IS NOT NULL
                   AND last_summarized_message_id <= last_scanned_message_id)),
    CONSTRAINT history_schedules_summary_size
        CHECK (octet_length(summary_text) <= 32768),
    CONSTRAINT history_schedules_revision_nonnegative
        CHECK (summary_revision >= 0),
    CONSTRAINT history_schedules_status_check
        CHECK (status IN ('active', 'degraded', 'blocked')),
    CONSTRAINT history_schedules_error_detail_size
        CHECK (last_error_detail IS NULL OR octet_length(last_error_detail) <= 512),
    CONSTRAINT history_schedules_version_positive
        CHECK (version > 0),
    CONSTRAINT history_schedules_peer_type_check
        CHECK (peer_type_snapshot IS NULL
               OR peer_type_snapshot IN ('private_user', 'private_bot', 'basic_group',
                                         'supergroup', 'broadcast_channel', 'unknown')),
    CONSTRAINT history_schedules_owner_peer_key
        UNIQUE (owner_id, peer_id)
);

ALTER TABLE history_schedules ENABLE ROW LEVEL SECURITY;
REVOKE ALL ON history_schedules FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON history_schedules TO service_role;

COMMENT ON TABLE history_schedules IS
    'PROPOSED DESIGN ONLY: durable cumulative summary and checkpoints for ONE History task/peer. Never stores raw Telegram messages.';
```

---

## 4. Repository API contract (design)

New module (implementation phase): `backend/ai/database/history_state_repository.py`.

```python
@dataclass
class HistoryStateRecord:
    task_id: int
    owner_id: int
    peer_id: int
    peer_type_snapshot: str | None
    baseline_message_id: int
    last_scanned_message_id: int | None
    last_summarized_message_id: int | None
    summary_text: str
    summary_revision: int
    status: str
    last_success_at: datetime | None
    last_error_class: str | None
    last_error_detail: str | None
    version: int
    created_at: datetime
    updated_at: datetime


class HistoryStateRepository:
    async def create(
        self, owner_id: int, *, task_id: int, peer_id: int,
        peer_type_snapshot: str | None, baseline_message_id: int,
    ) -> HistoryStateRecord: ...

    async def get(self, owner_id: int, task_id: int) -> HistoryStateRecord | None: ...

    async def get_by_peer(self, owner_id: int, peer_id: int) -> HistoryStateRecord | None: ...

    async def commit_success(
        self, owner_id: int, task_id: int, expected_version: int, *,
        summary_text: str, summary_revision: int,
        last_scanned_message_id: int,
        last_summarized_message_id: int | None,
        peer_type_snapshot: str | None,
        committed_at: datetime,
    ) -> HistoryStateRecord | None: ...

    async def record_failure(
        self, owner_id: int, task_id: int, expected_version: int, *,
        status: str, error_class: str, error_detail: str,
    ) -> HistoryStateRecord | None: ...

    async def reset_summary(
        self, owner_id: int, task_id: int, expected_version: int,
    ) -> HistoryStateRecord | None: ...
```

### Method contracts

- **`create`**: inserts exactly one row. `task_id` must reference an existing
  `ai_tasks` row owned by `owner_id`. The application resolves the peer and
  captures `baseline_message_id` before calling; it never accepts a peer id from a
  model. A unique `(owner_id, peer_id)` violation is a product error ("this peer
  already has a History state"), not an overwrite. No in-memory fallback.
- **`get`**: owner- and task-scoped. A missing row is `None`, not an empty state.
- **`get_by_peer`**: owner/peer lookup for the creation UI and duplicate check.
- **`commit_success`**: exactly one SQL/PostgREST `UPDATE` with
  `WHERE task_id = ? AND owner_id = ? AND version = expected_version`. It writes
  `summary_text`, `last_scanned_message_id`, `last_summarized_message_id`,
  `summary_revision`, `peer_type_snapshot`, `status='active'`,
  `last_success_at=committed_at`, clears `last_error_class/detail`,
  `updated_at=now()`, and `version = expected_version + 1`. Returns the updated
  record or `None` on zero rows (stale/deleted). It never inserts or retries with a
  fresh version automatically.
- **`record_failure`**: one CAS `UPDATE` that writes only `status`,
  `last_error_class`, `last_error_detail`, `updated_at`, and
  `version = expected_version + 1`. It never changes summary or cursors. Zero rows
  means a newer writer won; the caller logs and does not overwrite.
- **`reset_summary`**: an explicit owner action, one CAS update that clears summary
  text/revision and sets both cursors to `baseline_message_id` while preserving
  peer/baseline and incrementing `version`. It exists so a summary-limit block has
  an explicit, auditable escape hatch rather than silent truncation. Not exposed to
  the model.

### No silent fallback

`SupabaseHistoryStateRepository` is the runtime implementation. If Supabase is
unavailable or the table is missing, every method raises a typed
`HistoryStateUnavailable`; scheduled History must fail the occurrence and retry
later. An `InMemoryHistoryStateRepository` may exist **for tests only** and must
not be selected by `RepositoryManager` at runtime. Advancing a cursor in RAM would
violate the durability requirement and permanently skip messages after restart.

### Creation coordination

`ai_tasks` and `history_schedules` are two writes. Following the existing
`create_todo` compensating-delete pattern (there is no generalized transaction
framework), the implementation sequence is:

1. create the `ai_tasks` row;
2. create the state row;
3. on failure, delete the task under its CAS version (and report the truthful
   failure), so a History task can never exist without state.

The scheduler must also fail closed if an `ai_tasks` History row has no state row.
A future database function could make this atomic; this phase proposes the existing
compensating pattern, not a new transaction framework.

---

## 5. Cursor and failure semantics

### Definitions

- **`baseline_message_id`** — the start boundary captured when the state is
  created (the newest accessible message id at that moment, or `0` for an
  explicitly requested backlog/empty chat). It is not a processed cursor.
- **`last_scanned_message_id`** — the retrieval cursor. Everything at or below it
  (above baseline) has a **durably committed disposition**: summarized content, or
  an explicit safe skip (AI provenance, non-text/media-only). It never advances
  merely because a fetch returned rows.
- **`last_summarized_message_id`** — content coverage. All text content through it
  is represented in `summary_text` (or explicitly excluded as non-content). It can
  be lower than `last_scanned_message_id` when the tail of a batch contains only
  skipped messages.

Retrieval uses `after_id = COALESCE(last_scanned_message_id, baseline_message_id)`
(exclusive) and a **run-stable upper bound** captured once at the start:

1. bounded latest-message lookup (`limit=1`) → `upper_bound_message_id`;
2. if no newest message, the run has nothing to read; no cursor changes;
3. retrieve oldest-first with `after_id` (exclusive) and
   `before_id = upper_bound_message_id + 1` (exclusive);
4. page upward; new messages arriving during the run are above the captured upper
   bound and are excluded until the next run.

A cursor is committed only together with the summary. Deletions cannot erase a
previously committed summary.

### Case table

| # | Case | Scanned cursor | Summarized cursor | Summary | Retry / state | Human? |
|---|---|---|---|---|---|---|
| 1 | Batch fetched; summarization succeeds | advances to the highest **dispositioned** message (not raw fetched past an unprocessed text message) | advances to highest summarized content | updated atomically | no; active | no |
| 2 | Provider fails | unchanged | unchanged | unchanged | degraded `provider_failed`; retry appropriate | no |
| 3 | Provider times out | unchanged | unchanged | unchanged | degraded `provider_timeout`; retry | no |
| 4 | Empty/invalid summary | unchanged | unchanged | unchanged | degraded `invalid_summary`; retry | no unless persistent |
| 5 | Summary persistence fails | unchanged | unchanged | unchanged | degraded `persistence_failed`; retry; no RAM fallback | no |
| 6 | Restart during summarization | unchanged | unchanged | unchanged | no commit; recovery contract decides occurrence; next run re-reads | no |
| 7 | Restart after summary, before persistence | unchanged | unchanged | unchanged | same as 6; no partial state | no |
| 8 | Restart after persistence | already advanced | already advanced | already stored | safe; next run starts after cursor; no duplicate | no |
| 9 | Batch contains only messages with no text (media-only without a caption, empty) | advances to high-water | unchanged | unchanged (no text) | active; no AI call | no |
| 10 | Batch only AI-provenance messages | advances to high-water | unchanged | unchanged | active; no AI call | no |
| 11 | Message deleted before fetch | available messages drive high-water; no tombstone for the deleted id | advances only for available text | available content summarized; prior summary untouched | no error; if the whole range is empty, no cursor advances | no |
| 12 | Previously processed message becomes inaccessible | unchanged | unchanged | **retained**; never erased | no re-fetch of old range | no |
| 13 | Peer becomes inaccessible | unchanged | unchanged | retained | blocked `peer_unavailable`; scheduler retry/backoff | yes if persistent |
| 14 | Peer type or access changes | unchanged until next successful commit | unchanged | retained; policy recomputed live | reprocess only if `classify_chat` still resolves; `UNKNOWN`/migration → blocked | yes for re-point |

### Additional rules

- **Empty successful range.** If retrieval returns no messages between the cursor
  and the captured upper bound, this means there is no currently accessible content
  in that range, not that a cursor may jump. No cursor advances; no error is
  fabricated. A later message above the cursor is fetched normally. This avoids
  guessing at deletions.
- **Deleted middle messages.** Telegram returns gaps; the high-water mark is based
  on the returned ids. No tombstone is invented, and no previously stored summary
  is rewritten.
- **Summary persistence failure is all-or-nothing.** The summary, both cursors,
  revision, status and version are one update. A failed/zero-row update leaves every
  field unchanged.
- **No summary revision on skipped-only batches.** Cursors can advance; summary and
  revision do not.
- **Policy is live.** The peer is classified with `classify_chat` every run. A
  stored `peer_type_snapshot` never authorizes a count. If the current type is
  `UNKNOWN` or a migration makes the stored peer id unresolvable, the state is
  `blocked` and the summary remains.

---

## 6. Atomic persistence and CAS

### The one atomic commit

`commit_success` is a single row update guarded by `(task_id, owner_id, version)`.
It persists together:

- `summary_text`
- `last_scanned_message_id`
- `last_summarized_message_id`
- `summary_revision`
- `peer_type_snapshot`
- `status='active'`, `last_success_at`, cleared errors
- `version = version + 1`
- `updated_at`

PostgREST applies one `UPDATE` atomically. There are no secondary writes that could
partially succeed. A transaction is not required for a single row.

### CAS rules

- `expected_version` is read at the start of the run (with the summary/cursors).
- A commit with a stale version returns zero rows and **must not** overwrite the
  newer row. The newly generated summary is discarded; the occurrence records a
  bounded `state_conflict` failure. The next run reads the current state.
- A failure/status write is also CAS-guarded; a stale failure cannot clobber a
  newer success.
- `summary_revision` is content-only. It increments only on a successful content
  commit; status/error writes increment only `version`.
- The repository distinguishes **zero rows (stale/deleted)** from **transport
  failure (raise)**. It never treats a database outage as a stale no-op.

### Concurrency matrix

| Scenario | Expected behavior |
|---|---|
| Two scheduled occurrences for the same task overlap | Both may read the same version; one commit wins, the other gets `None` and discards its result. No duplicate cursor/summary. |
| A manual summary overlaps scheduled History | Manual operations remain read-only and never commit state; they may duplicate provider work but cannot corrupt cursors. |
| Task paused mid-run | The in-flight run may still commit its successfully generated summary; pause only stops future scheduling. The summary is valuable, and the state remains internally consistent. |
| Task deleted/disabled mid-run | The FK cascade removes the state; the late CAS update returns zero rows and is discarded. No orphan state, no recreation. |
| A stale worker commits after a newer revision | CAS returns zero rows; never overwrites. |
| Duplicate execution after restart | If the prior commit succeeded, the cursor has advanced and the duplicate cannot include the same content. If it did not commit, no state changed and a retry re-reads the same range. The stored summary is never duplicated. |

---

## 7. Restart and failure recovery

- No state is written before the atomic commit. A process death at any point before
  it leaves the prior row exactly as it was.
- The existing task recovery contract remains authoritative: a `running` occurrence
  becomes `failed/restart_side_effect_uncertain` (never blindly retried); a
  `claimed` future occurrence is untouched; an interrupted occurrence follows the
  bounded retry policy. The History layer does not invent a second recovery path.
- If the process died **after** the commit but before the occurrence transition, the
  state is already advanced. The recovery-marked failure is honest about the
  occurrence, but no summary content is duplicated or skipped: the next scheduled
  run starts from the durable cursor.
- If the process died **before** the commit, the next run re-reads from the old
  cursor and regenerates the batch. That is repeated AI work, not data corruption.
- A persistence failure never advances any cursor and never leaves a partial
  summary.

---

## 8. Deleted or inaccessible Telegram messages

Telegram is treated as a **source that can lose content**, never as durable
storage.

- The state stores only a cumulative summary and checkpoints, not message bodies.
- A message deleted after successful summarization does not change the stored
  summary. The summary is the durable record.
- A message deleted before retrieval is simply absent. No tombstone is invented and
  no content is claimed. If other messages remain, the batch covers them; if the
  range is empty, no cursor advances and no error is fabricated.
- A message that becomes inaccessible after a previous run does not cause a re-read
  of old content; the summary remains.
- Deleted content is **not recoverable** by this system. The design distinguishes:
  1. **processed and durably summarized** — `summary_text`/cursor;
  2. **new and still fetchable** — above `last_scanned_message_id`;
  3. **unavailable before successful processing** — absent from Telegram; not in
     the summary and not recoverable.

- If the peer itself is inaccessible or its type becomes `UNKNOWN`, the state is
  `blocked`; the existing summary is retained and the scheduler retries with the
  existing bounded policy. A persistent block requires the owner to re-point/recreate
  the task.
- A basic group migrating to a supergroup can change the peer id. The current code
  has no migration-alias table; the design does **not** guess a new id from a title
  or flags. The state becomes `blocked` and the owner must re-point it. Preserve the
  old summary. This is an open evidence gap for Phase 4/5.

---

## 9. Retention and summary compaction

### Bounds

- `summary_text` hard limit: **32,768 UTF-8 bytes**, enforced by the table CHECK and
  by the repository before every write. The number is explicit, matches the largest
  existing bounded task payload (32 KiB), and is small enough to avoid unlimited
  TOAST growth.
- Only the latest cumulative summary is kept. No per-batch summaries, no revision
  history table, no raw messages.
- `last_error_detail` ≤ 512 bytes, `last_error_class` ≤ 64 characters; each failure
  overwrites the prior diagnostic rather than appending.
- `version`, revision, timestamps and IDs are fixed-size.

### Compaction

On each scheduled run, the new batch is summarized and merged into the prior
cumulative summary, then the result is bounded before commit. The intended
algorithm:

1. summarize the new batch within the existing AI capacity gate;
2. merge it with the prior summary in a bounded reduce prompt;
3. measure the UTF-8 result;
4. if ≤ 32 KiB, commit atomically;
5. if > 32 KiB, **do not truncate and do not commit**. Preserve the old summary
   and cursors, set status `degraded`, `last_error_class='summary_limit'`, and
   surface an honest owner-visible message. The owner can compact/reset explicitly
   through a future panel action (`reset_summary`), or the implementation may add a
   separately approved compaction step. Silent loss of old content is prohibited.

This is a hard ceiling, not a guarantee that a schedule can run forever without
compaction. A very long-lived, high-volume schedule may reach it; the design fails
honestly rather than silently dropping history. The merge prompt that combines the
prior summary with a new batch must pass through the same AI capacity gate as any
other History request: a large prior summary consumes context and reduces the
affordable batch; it must never be silently dropped or truncated to make room.

32 KiB is a **storage** bound, not a Telegram message bound. Delivery of a long
summary may need the existing panel/page or chunking behavior; changing delivery
is outside this phase and does not change the stored-summary ceiling.

### Deletion/retention

- Deleting the `ai_tasks` row is an explicit owner deletion; the state row cascades
  with it. Pausing retains the state and summary; the owner should pause, not
  delete, when they want to keep it.
- No time-based automatic deletion is proposed initially; the row is small. Any
  future retention policy must be explicit and owner-approved.
- Owner account deletion is outside this schema (no owner table); the task deletion
  path is the current lifecycle.

### Privacy

`summary_text` is private chat content. The service-role-only RLS/grants are part
of retention, not optional hardening. The state row contains no message bodies,
sender ids, chat titles, usernames or provider raw responses.

---

## 10. AI context isolation

The established manual panel path already uses `with_identifiers=False`: the model
gets only text lines and the owner's instruction, with no `[id]` prefixes and no
sender attribution. The future scheduled path must preserve that, and additionally:

- Never send: peer id, chat title/username, message ids, sender identity, task id,
  occurrence key, baseline/cursor/checkpoint values, database primary keys,
  repository metadata, schedule, destination, unrelated conversation, saved memory
  or prior AI session messages.
- The new scheduled History action must be deterministic and read the state row
  from a trusted coordinator-injected `task_id`; it must not read
  `extra["chat_id"]` as the source (that is the delivery destination). It must
  return only the generated summary and non-identifying counts to the owner; it
  must not return `first_message_id`/`last_message_id` (the current
  `history_ai_service._result_data` does include them, and the new path must omit
  them).
- The summarizer must call the service with `with_identifiers=False` explicitly;
  the service default is `True` for the existing tool path. Messages with no text
  (including media-only messages without a caption) are skipped by the chunker;
  a caption is text and remains eligible, exactly as in the current manual path.
  Media bytes are never downloaded.
- The owner's authored instruction is the only instruction input. The task label,
  schedule, destination, IDs and occurrence metadata are not prompt material.
- **Scope of the identifier rule.** It prohibits identifiers as *metadata*: no
  `[id]` prefixes, sender attribution, peer/task/checkpoint fields or database keys
  are added. The owner's message text is passed as content and may itself contain
  a phone number, link, numeric id or similar string; that is user-authored content,
  not an injected identifier, and this design does not attempt semantic redaction
  of it. Any stricter content-redaction requirement would need a separate,
  approved design.

### Open privacy decision

A cumulative summary update needs the prior stored summary as context for the
merge/compaction step. That summary is the target artifact, was generated under
`with_identifiers=False`, and contains no identifier metadata added by the
pipeline. It is not "unrelated conversation history" or a prior AI session.
However, the strict reading of the requirement lists only new message text and the
owner instruction as permitted input. This must be explicitly approved before
Phase 5; if the owner forbids it, cumulative AI compaction is not possible without
a different deterministic strategy. This is recorded in §17.

---

## 11. Oldest-first retrieval — explicit blocking prerequisite

### Verified defect (still unfixed)

`history_service._collect` scans **newest → oldest** and, after collecting up to a
large bound, keeps `collected[:target]` — the newest `target` eligible messages —
then reverses them. It does not stop at the oldest requested boundary. With 60
eligible messages after a cursor and `count=10`, it can return messages 51–60 and
silently omit 1–50. This is the known defect from the prior investigation; **Phase
3 does not fix it**.

The existing test suite actually pins this behavior:
`tests/test_history_ai_tools.py::test_translate_requests_exactly_the_requested_count_oldest_to_newest`
asserts that a `count=10` translation over 50 messages sends ids **41–50** to the
provider. The name describes chronological output order, not the selected window.
`tests/test_history_service.py` also pins the newest-first decreasing-`max_id`
pagination. Any Phase 5 oldest-first implementation must update these tests
deliberately, not merely add a parallel path; the five History test files
(191 test functions) are the regression baseline.

### Required retrieval contract for scheduled processing

- Capture a **stable upper-bound message id** once per run (newest at run start).
- Use an **exclusive lower cursor** (`after_id = last durably dispositioned id`).
- Retrieve **oldest-first** (Telegram `reverse=True`), never newest-first-then-trim.
- Page upward with an exclusive lower cursor; never re-read a committed page.
- Bounded batch and bounded raw scan; on the scan bound, stop and commit only the
  messages actually dispositioned, never jump the cursor.
- No gaps and no duplicates for the committed range.
- AI-provenance and non-text messages are explicit skips; they may advance the
  scanned cursor but never the summarized content cursor, and never reach the model.
- Deleted/absent messages are gaps in the source, not invented messages; no cursor
  jump beyond what was actually dispositioned.
- If the whole fetched range is provenance/non-text, scanned may advance; the
  summary and summarized cursor do not change.
- If no eligible text exists at all, no AI call and no summary revision; the run
  reports the honest no-content outcome.
- If the scan bound is reached before a stable batch is complete, do not advance
  past the last dispositioned message; the next run continues there.

### Smallest facade change (not implemented)

`backend/telegram_api/messages.py::iter_messages` currently accepts `limit`,
`from_user`, `min_id`, `max_id` and calls Telethon without `reverse`. The
`TelegramAPI` facade in `backend/telegram_api/api.py` also needs the optional
argument forwarded. The smallest compatible extension is an optional
`reverse: bool = False` on both layers, forwarded to `client.iter_messages`, plus
an oldest-first path in `history_service._collect` / `fetch_history_page` that
uses `min_id` as the upward cursor. `max_id` remains the exclusive stable upper
bound. No cursor/order change is made in Phase 3.

**This defect and contract are a prerequisite for Phase 5 scheduled catch-up. No
scheduled History implementation may ship before it is fixed and tested.**

---

## 12. Migration strategy and numbering

### Verified current state

- 41 migration files: 40 schema migrations + the separately applied privilege
  hardening migration.
- Latest schema migration: `20261112160000_add_panel_settings_intelligent_reaction_probability.sql`.
- No file matches `20261113*`, and no History table/migration exists.
- `DATABASE_ARCHITECTURE.md` is the authority and is **protected in this phase**.
- Its §24 requires a document update before a migration, and the suite requires
  every migration's table to be classified and the §31.3 setup block to match.

### Proposed migration (not written)

- Filename candidate: `supabase/migrations/20261113120000_create_history_schedules.sql`.
  It sorts after the current latest and collides with nothing.
- One additive table creation, idempotent (`CREATE TABLE IF NOT EXISTS`,
  `DROP POLICY IF EXISTS`, `NOTIFY pgrst`), no changes to existing tables/columns/
  rows. `task_id` is the PK; no sequence.
- It must be applied manually by the owner in the Supabase SQL editor. **No agent
  may execute SQL or modify Supabase.**
- Before the file is added, the owner must approve an update to
  `DATABASE_ARCHITECTURE.md`:
  - a new section for `history_schedules` (purpose, columns, constraints, RLS,
    grants, retention);
  - the table count/classification in §1/§17/§22/§25 as applicable;
  - the canonical §31.3 part 1 SQL (fresh-database setup must create the table) and
    the embedded-part/order audit;
  - the §24 exception for this table's service-role-only RLS posture.

Without that document update, `test_no_migration_creates_a_table_that_the_documentation_does_not_classify`
and `test_database_setup_order` will fail. This is a documentation/approval
prerequisite, not a reason to bypass the tests.

---

## 13. Exact implementation sequence for the next phase

1. **Owner approval + documentation update** (separate, protected change): update
   `DATABASE_ARCHITECTURE.md` with the table and canonical setup part.
2. **Migration**: add the idempotent `20261113120000_create_history_schedules.sql`;
   owner applies it manually; verify columns/constraints/RLS/grants.
3. **Repository**: add `HistoryStateRecord`,
   `HistoryStateRepository`, and the Supabase implementation; wire a durable-only
   accessor through `RepositoryManager` (no runtime in-memory fallback). Add a
   test-only fake.
4. **Retrieval prerequisite**: add the optional reverse/oldest-first facade path
   and a history-service collector that never trims to the newest subset; add
   tests for gaps/duplicates/bounds.
5. **Scheduled service**: read state by trusted task id, re-resolve/classify the
   peer, recompute `history_policy`, capture the stable upper bound, retrieve
   oldest-first, summarize the batch with `with_identifiers=False`, merge with the
   bounded cumulative summary, enforce the 32 KiB bound, and call
   `commit_success` with the run's expected version.
6. **Execution wiring**: register one deterministic action/tool with no
   model-supplied peer/chat identifiers. The coordinator injects the trusted
   `task_id` (and occurrence key only for logging); the tool never reads
   `notification_destination.chat_id` as its source. Use the existing
   `ToolExecutor`, scheduler and occurrence recovery; do not add a scheduler,
   worker, client or listener.
7. **Creation/UI**: the owner wizard resolves and classifies the peer from trusted
   context, captures the baseline (latest id or explicit backlog), creates the
   task, then the state row; on failure, compensatingly delete the task. Refuse a
   second state for the same `(owner_id, peer_id)`.
8. **Tests** (see §14).
9. **Manual live validation** (owner, after apply): create a History task on a test
   peer, run a boundary, restart the process, verify no re-summarization of the
   committed range, delete an old source message, verify the stored summary remains,
   simulate a provider failure and verify no cursor advance, and verify the
   oldest-first ordering with >batch messages.

No step above was performed in Phase 3.

---

## 14. Tests required for the implementation phase

1. **Policy/state unit**: every `PeerType` cap/choice still applies to a scheduled
   count; no stored snapshot can bypass `classify_chat`.
2. **Cursor semantics**: success advances both cursors correctly; a skipped-only
   batch advances only the scanned cursor; mixed batch advances content coverage to
   the last summarized id.
3. **Oldest-first**: 60 messages with batch 10 returns 1–10, 11–20, … not 51–60;
   no gaps/duplicates; stable upper bound excludes messages arriving mid-run.
4. **Atomic commit**: summary + cursors + revision + version + success status are
   written together; transport failure writes none; stale CAS returns zero and
   leaves the row unchanged.
5. **Failure cases**: provider failure, timeout, empty/invalid summary, persistence
   failure, restart before/after commit, duplicate recovery—assert no cursor/summary
   corruption.
6. **Deleted/inaccessible**: existing summary remains after source deletion;
   empty range does not advance; peer inaccessible blocks; peer migration/UNKNOWN
   blocks and preserves summary.
7. **Summary bound**: 32 KiB is enforced; an over-limit result is refused, not
   truncated, and the old summary remains.
8. **Repository**: create/get owner scoping; unique `(owner_id, peer_id)`; cascade
   deletion; no runtime in-memory fallback; PK/FK behavior.
9. **Task creation**: task+state compensates on state failure; a History task
   without state fails closed; task delete cascades state.
10. **Execution context**: scheduled action receives trusted task id, ignores
    model-supplied ids and `notification_destination.chat_id` as source; delivery
    still uses the destination only.
11. **Privacy**: prompt contains no message ids, sender ids, peer id/title,
    task/occurrence/db ids, checkpoint values, unrelated context or memory; result
    omits `first_message_id`/`last_message_id`.
12. **Retry/performance**: no duplicate summary after restart; paced/concurrent
    limits unchanged; capacity refusal remains honest.
13. **Schema audit**: migration idempotent; doc classification and §31.3 part are
    present; RLS enabled, zero policies, no client grants, service-role grants only.
14. **No regressions**: Phase 1/2 suites, history panel/service/AI/capacity tests,
    full suite baseline comparison.

---

## 15. What this phase proves and does not prove

### Proven from source

- There is no durable History state today; summaries and cursors are in memory or
  only in the Telegram panel message.
- No message content/summary is persisted by the History path.
- `result_metadata` is 8 KiB and occurrence-owned; `notification_destination` is
  delivery-only.
- Classification is live and policy is live; a stored type would be a stale
  diagnostic at best.
- The collector has the documented newest-first defect; existing tests pin it
  (`count=10` over 50 messages sends 41–50), so it is not suitable for scheduled
  catch-up without a deliberate test update.
- A durable table/repository is required; a small additive table linked to
  `ai_tasks` is the least invasive fit with the existing scheduler/repository.

### Not proven / not done

- No migration was created or applied; no SQL was executed; Supabase was not
  contacted.
- No production Python or test was modified.
- No live Telegram or provider behavior was tested.
- The table design is not deployed and has no runtime consumer.
- The live database's current 28-table inventory was not re-audited in this phase;
  the repository documents it as a manual audit.
- Whether the owner permits sending the prior cumulative summary to the AI for
  compaction is unresolved (see §17).

---

## 16. Open decisions and evidence gaps

1. **One state per peer vs multiple schedules per peer.** The design enforces
   `UNIQUE (owner_id, peer_id)` and one state per task. If multiple independent
   schedules for the same peer are required, the unique constraint and summary
   ownership must be redesigned before implementation. Recommendation: one for the
   first implementation, because two summaries for one peer are ambiguous and
   duplicate provider work.
2. **Start behavior.** `baseline_message_id` defaults to the newest message (new
   content only) or `0` for an explicit backlog. Phase 4 must expose this choice
   explicitly; silently summarizing years of backlog is not acceptable.
3. **Prior summary as AI input.** Cumulative compaction needs it; §10's strict
   "only" wording does not list it. Owner approval required. Recommendation: allow
   it as the bounded target artifact with `with_identifiers=False`, no metadata.
4. **Task deletion semantics.** Proposed `ON DELETE CASCADE`: deleting the task
   explicitly deletes the summary. Pausing retains it. Confirm this is the intended
   privacy/retention behavior.
5. **Peer migration / inaccessible peer.** Telegram basic-group→supergroup id
   migration has no alias map in the current code. The design blocks and asks the
   owner to re-point; do not infer a new id. Confirm this product behavior.
6. **Summary-limit escape hatch.** `reset_summary` (explicit owner action) vs a
   future AI compaction worker. Recommendation: explicit reset/compaction, never
   silent truncation; no new worker in Phase 5.
7. **RLS exception.** The table uses zero policies/service-role-only, deviating from
   §24's generic SELECT policy. Owner must approve the documentation exception.
8. **Migration numbering.** Proposed `20261113120000`; confirm after the protected
   document update and before writing the file.
9. **Manual/scheduled overlap.** Manual summaries stay read-only and never advance
   state. If duplicate provider work becomes a cost problem, a future admission
   lock can coordinate them; not part of Phase 3.
10. **Live schema.** The documented 28-table live inventory and the 3-record
    migration history are manual audit facts, not re-verified against Supabase in
    this phase.

---

## 17. Unresolved prerequisite for scheduled processing

The oldest-first retrieval defect remains **unfixed**. The current collector can
return the newest subset of a range and silently omit older messages after a
cursor. Until the contract in §11 is implemented and tested, Phase 5 scheduled
History must not ship, even with durable state: a durable cursor would faithfully
record a skipped range.

This investigation does not fix retrieval, does not change cursors, and does not
claim scheduled History is implemented.
