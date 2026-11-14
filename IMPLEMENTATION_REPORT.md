# IMPLEMENTATION REPORT — Phase 3B: Durable History State and Oldest-First Retrieval

## 1. Implemented

Phase 3B adds the durable persistence and retrieval foundation for scheduled
History summarization. It does **not** add execution.

- One additive migration:
  `supabase/migrations/20261113000001_create_history_schedules.sql`.
- One `history_schedules` state per `(owner_id, peer_id)`, owned by an
  `ai_tasks` row with `ON DELETE CASCADE`.
- Repository primitives on the existing `TaskRepository` hierarchy:
  `create_history_schedule`, `get_history_schedule`,
  `get_history_schedule_by_peer`, `commit_history_success`,
  `record_history_failure`, `reset_history_summary`.
- `InMemoryTaskRepository` as an explicitly injected test fake (task-delete
  cascade included). Production History persistence is durable-only: an
  unavailable store raises `HistoryStorageUnavailable` and never substitutes
  process memory.
- A `SupabaseTaskRepository` implementation using one CAS update per accepted
  write.
- Bounded oldest-first Telegram retrieval through an optional
  `reverse: bool = False` parameter on `messages.iter_messages` and the
  `TelegramAPI.iter_messages` facade, plus `fetch_oldest_history` and
  `HistoryBatch` in the History service.
- An identifier-free `scheduled_summary_payload` boundary in the History AI
  service (no production caller in this phase).
- Focused tests and the canonical documentation updates, including §16.1 and
  §31.3 part 18 of 18.
- No scheduler, executor, lifecycle hook, UI, parser, notification, catch-up
  policy, second client or second update loop.

Files changed:

| File | Change |
|---|---|
| `supabase/migrations/20261113000001_create_history_schedules.sql` | New table, index, RLS/grants, comments, verification and rollback note |
| `backend/ai/database/task_repository.py` | Record model, validation, in-memory fallback, Supabase CAS methods, task cascade |
| `backend/services/history_service.py` | `HistoryBatch` and bounded oldest-first retrieval |
| `backend/services/history_ai_service.py` | Identifier-free scheduled payload |
| `backend/telegram_api/messages.py` | Optional `reverse` forwarding to Telethon |
| `backend/telegram_api/api.py` | Facade forwarding of `reverse` |
| `tests/test_history_schedules.py` | 31 focused Phase 3B tests |
| `DATABASE_ARCHITECTURE.md` | §16.1, inventory 42/18, part 18, §22, §30.11, rollback/verification |
| `tests/test_database_setup_order.py` | 15-part / 37-file order and new-part assertions |
| `tests/test_private_message_db_security.py` | Part totals 15/37 and final-banner assertions |
| `tests/test_private_message_scope.py` | Part references 10/11/13 of 15 |
| `tests/test_canonical_schema_reconciliation.py` | `history_schedules` classified as a successor table |

## 2. Database schema and migration created

### 2.1 Shape

`public.history_schedules`:

- `id bigserial PRIMARY KEY`
- `task_id bigint NOT NULL REFERENCES ai_tasks(id) ON DELETE CASCADE`
- `owner_id bigint NOT NULL`
- `peer_id bigint NOT NULL` with `CHECK (peer_id <> 0)`
- `status text NOT NULL DEFAULT 'active'`
- `baseline_cursor bigint NOT NULL DEFAULT 0`
- `scanned_cursor bigint NOT NULL DEFAULT 0`
- `summarized_cursor bigint NOT NULL DEFAULT 0`
- `summary text NOT NULL DEFAULT ''`
- `revision bigint NOT NULL DEFAULT 0`
- `version integer NOT NULL DEFAULT 1`
- `last_error text NOT NULL DEFAULT ''`
- `last_error_at timestamptz`
- `last_committed_at timestamptz`
- `created_at timestamptz NOT NULL DEFAULT now()`
- `updated_at timestamptz NOT NULL DEFAULT now()`

Constraints:

- `history_schedules_owner_peer_key UNIQUE (owner_id, peer_id)` — the one-state
  ownership invariant.
- `history_schedules_peer_nonzero CHECK (peer_id <> 0)`.
- `history_schedules_status_check` — `active`, `paused`, `completed`, `failed`,
  `expired`, `deleted` (the last is legacy repair only).
- `history_schedules_baseline_nonnegative`,
  `history_schedules_scanned_nonnegative`,
  `history_schedules_summarized_nonnegative`.
- `history_schedules_cursor_order CHECK (baseline_cursor <= summarized_cursor
  AND summarized_cursor <= scanned_cursor)`.
- `history_schedules_summary_size CHECK (octet_length(summary) <= 32768)` —
  32 KiB of UTF-8, never truncated.
- `history_schedules_revision_nonnegative`, `history_schedules_version_positive`.
- `history_schedules_error_size CHECK (octet_length(last_error) <= 2048)`.
- `history_schedules_error_pair CHECK ((last_error = '') = (last_error_at IS NULL))`.

Index:

- `idx_history_schedules_task_id (task_id)` for the owning-task lookup and the
  cascade. The unique constraint serves owner+peer reads. There is no scheduler
  query, so there is no status/next-run index.

RLS/access:

- RLS enabled, zero policies.
- All privileges revoked from `PUBLIC`, `anon`, `authenticated`.
- Exactly `SELECT, INSERT, UPDATE, DELETE` granted to `service_role`.

There is no owner foreign key because the repository has no owner table.
`task_id` is the only FK, and it cascades on physical task deletion.

### 2.2 Exact manual application SQL

Apply the entire file
`supabase/migrations/20261113000001_create_history_schedules.sql` as
`postgres` in the Supabase SQL editor (or as §31.3 part 18 of the one-paste
setup). Its executable SQL is:

```sql
CREATE TABLE IF NOT EXISTS history_schedules (
    id                  bigserial    PRIMARY KEY,
    task_id             bigint       NOT NULL REFERENCES ai_tasks(id) ON DELETE CASCADE,
    owner_id            bigint       NOT NULL,
    peer_id             bigint       NOT NULL,
    status              text         NOT NULL DEFAULT 'active',
    baseline_cursor     bigint       NOT NULL DEFAULT 0,
    scanned_cursor      bigint       NOT NULL DEFAULT 0,
    summarized_cursor   bigint       NOT NULL DEFAULT 0,
    summary             text         NOT NULL DEFAULT '',
    revision            bigint       NOT NULL DEFAULT 0,
    version             integer      NOT NULL DEFAULT 1,
    last_error          text         NOT NULL DEFAULT '',
    last_error_at       timestamptz,
    last_committed_at   timestamptz,
    created_at          timestamptz  NOT NULL DEFAULT now(),
    updated_at          timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT history_schedules_owner_peer_key UNIQUE (owner_id, peer_id),
    CONSTRAINT history_schedules_peer_nonzero CHECK (peer_id <> 0),
    CONSTRAINT history_schedules_status_check CHECK (
        status IN ('active', 'paused', 'completed', 'failed', 'expired', 'deleted')
    ),
    CONSTRAINT history_schedules_baseline_nonnegative CHECK (baseline_cursor >= 0),
    CONSTRAINT history_schedules_scanned_nonnegative CHECK (scanned_cursor >= 0),
    CONSTRAINT history_schedules_summarized_nonnegative CHECK (summarized_cursor >= 0),
    CONSTRAINT history_schedules_cursor_order CHECK (
        baseline_cursor <= summarized_cursor
        AND summarized_cursor <= scanned_cursor
    ),
    CONSTRAINT history_schedules_summary_size CHECK (octet_length(summary) <= 32768),
    CONSTRAINT history_schedules_revision_nonnegative CHECK (revision >= 0),
    CONSTRAINT history_schedules_version_positive CHECK (version > 0),
    CONSTRAINT history_schedules_error_size CHECK (octet_length(last_error) <= 2048),
    CONSTRAINT history_schedules_error_pair CHECK (
        (last_error = '') = (last_error_at IS NULL)
    )
);

CREATE INDEX IF NOT EXISTS idx_history_schedules_task_id
    ON history_schedules (task_id);

ALTER TABLE history_schedules ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE history_schedules FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE history_schedules TO service_role;

COMMENT ON TABLE history_schedules IS
    'ONE cumulative scheduled-History state per (owner_id, peer_id), owned by an ai_tasks row and deleted with it. Service-role only: RLS is enabled with zero policies and every privilege is revoked from PUBLIC/anon/authenticated. Cursors advance only through a successful compare-and-set commit; failures never replace the previous summary or advance either cursor.';
COMMENT ON COLUMN history_schedules.baseline_cursor IS
    'Immutable exclusive lower bound chosen at creation. Messages at or below it are outside this schedule.';
COMMENT ON COLUMN history_schedules.scanned_cursor IS
    'Exclusive lower cursor through which content has been successfully processed or explicitly safely skipped.';
COMMENT ON COLUMN history_schedules.summarized_cursor IS
    'Exclusive lower cursor through which content is actually covered by the cumulative summary; never greater than scanned_cursor.';
COMMENT ON COLUMN history_schedules.summary IS
    'Cumulative summary, at most 32 KiB of UTF-8. Never truncated on write; an over-limit result fails.';
COMMENT ON COLUMN history_schedules.revision IS
    'Successful content revisions (summary/reset commits); a recorded failure does not change it.';
COMMENT ON COLUMN history_schedules.version IS
    'Compare-and-set token incremented by every accepted write, including a recorded failure.';
COMMENT ON COLUMN history_schedules.last_error IS
    'Bounded diagnostic from the last failed processing attempt; empty when no error is recorded.';
COMMENT ON COLUMN history_schedules.last_committed_at IS
    'Timestamp of the last successful summary commit; NULL until the first commit.';

NOTIFY pgrst, 'reload schema';

SELECT 'history_schedules table' AS check, count(*) AS present
FROM information_schema.tables
WHERE table_schema = 'public'
  AND table_name   = 'history_schedules';

SELECT 'history_schedules columns' AS check, count(*) AS present
FROM information_schema.columns
WHERE table_schema = 'public'
  AND table_name   = 'history_schedules';

SELECT 'history_schedules policies' AS check, count(*) AS present
FROM pg_policies
WHERE schemaname = 'public'
  AND tablename  = 'history_schedules';
```

The migration file is the source of truth and includes the full explanatory
header. Its SHA-256 at delivery is
`1f58c28b41649312ecb3904b9089628039e17ff9e86d7d124d544a2c7a7b898a`.

### 2.3 Dependency-safe rollback

The table is a leaf: nothing else references `history_schedules`, while it
references `ai_tasks`. Dropping it therefore has no dependent object to drop
first and does not touch the owning tasks:

```sql
DROP TABLE IF EXISTS history_schedules;
```

This destroys the cumulative History state and is irreversible for data. It
does not delete `ai_tasks` rows, any other table, column, index, policy, grant
or row. No agent has executed it.

## 3. Repository behavior

`create_history_schedule(owner_id, task_id, peer_id, baseline_cursor)`:

- verifies the task exists for the same owner;
- refuses a live existing `(owner_id, peer_id)` row (`None`);
- reuses a legacy `deleted` row in place, resetting task/baseline/cursors/
  summary and advancing revision/version;
- supports the `UNIQUE (owner_id, peer_id)` constraint as the durable backstop;
- in the in-memory fallback, physically deleting the owning task removes the
  state, mirroring the FK cascade.

`commit_history_success`:

- validates the summary's UTF-8 byte bound before any write;
- rejects a stale `expected_version` without writing;
- rejects backwards cursors and any violation of
  `baseline <= summarized <= scanned`;
- rejects replacing a non-empty durable summary with an empty/blank summary;
- writes summary, both cursors, status, `revision + 1`, `version + 1`,
  `last_committed_at` and cleared error metadata in ONE CAS update filtered by
  `owner_id`, `peer_id` and `version`.

`record_history_failure`:

- rejects a stale version without writing;
- validates a nonblank, ≤2048-byte error;
- writes only error, status, `version + 1` and timestamps;
- never changes summary, cursors or revision.

`reset_history_summary`:

- rejects a stale version without writing;
- sets `summary = ''`, `summarized_cursor = scanned_cursor`, advances revision
  and version, clears error metadata;
- is explicit and owner-authorized, never automatic.

On Supabase exception the durable methods FAIL CLOSED: they record the
degradation and raise `HistoryStorageUnavailable`. They never return a
memory-only row and never report a durable write as successful. This is
enforced by tests and by a structural assertion that no History method
references `self._fallback`. Live database behavior is not claimed, because no
live database was contacted.

## 4. Oldest-first retrieval

`backend/telegram_api/messages.py` now accepts `reverse: bool = False` and adds
`reverse=True` to Telethon's kwargs only when requested. The facade forwards
the parameter without changing any other caller's default.

`fetch_oldest_history`:

- requires an exclusive lower cursor and a positive, stable upper bound;
- returns an empty `HistoryBatch` when the range is empty, without a Telegram
  call;
- asks for oldest-first pages inside that fixed window;
- sorts and deduplicates each page defensively;
- ignores ids outside the window;
- advances `next_cursor` for every processed raw message, including safe
  AI-provenance skips;
- stops on the target, the scan bound, a short page or an exhausted range;
- reports `has_more` conservatively;
- never re-reads a processed id when the returned cursor is passed back.

Deleted/inaccessible ids create no gap because the next page starts strictly
after the last processed cursor. A skipped-only batch can advance the scanned
cursor without moving content coverage.

## 5. AI context isolation

`scheduled_summary_payload(messages, instruction="")` returns only:

- `Owner instruction: <bounded instruction>`, when nonblank; and
- the nonblank text bodies of the supplied messages, joined in order.

It excludes chat/peer/message/sender/task/occurrence/database identifiers,
sender attribution, dates, media metadata, peer title/username, unrelated chat
history, memory and session history. Empty and media-only messages contribute
nothing. Identifiers typed by a human inside message text are content, not
semantically redacted metadata. There is no production caller in Phase 3B.

## 6. Tests and validation actually performed

### 6.1 Focused tests

- `tests/test_history_schedules.py`: **31 passed**. Covers:
  - create/get owner scoping and owned-task requirement;
  - unique owner+peer refusal, including paused/completed/failed/expired
    ownership;
  - legacy `deleted` repair;
  - in-memory task-delete cascade;
  - one CAS write, stale-version rejection, owner-scoped writes;
  - failure preserving summary/cursors/revision and bounded error;
  - 32 KiB UTF-8 boundary and no truncation;
  - empty-summary protection;
  - safe skipped-only scanned-cursor advancement;
  - monotonic cursor/order rejection;
  - explicit reset semantics;
  - fake-Supabase CAS payload shape and no write on a stale version;
  - Supabase create refusing another owner's task;
  - oldest-first ordering, `reverse=True` forwarding, stable upper bound,
    no gaps/duplicates, deleted gaps, AI skips, empty ranges and facade
    defaults;
  - identifier-free scheduled payload;
  - durable-only failure: unavailable reads, writes and creation raise
    `HistoryStorageUnavailable` and never touch the injected fake;
  - a failed CAS write is not replayed against memory;
  - a structural guard that no History method references `self._fallback`.
- Merged documentation/setup/security/scope/canonical-schema suites:
  **269 passed** in one run (`test_history_schedules.py`,
  `test_database_setup_order.py`, `test_database_architecture_doc.py`,
  `test_private_message_db_security.py`, `test_private_message_scope.py`,
  `test_canonical_schema_reconciliation.py`). The full Appearance suites also
  pass after the merge (`279 passed`).

These are unit/repository tests against fake clients and an explicitly injected
in-memory fake. They do not contact live Supabase or live Telegram.

### 6.2 Full suite

Command: `.venv/bin/python -m pytest -q`

Result: **7788 passed, 27 skipped, 1 failed** in 163.08 s (after the audit
corrections).

The single failure is
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
(“interval advancement exceeded bound”). It was reproduced on clean worktrees at
the delivery commit `a12c207`, its Phase 3B parent `417a6b8` and the upstream
parent `e2f1619`; `git diff e2f1619 a12c207 -- tests/test_task_scheduler.py
backend/ai/task_scheduler.py` is empty. It is therefore demonstrably
pre-existing, not caused by Phase 3B or by the merge. No Phase 3B test failed.

### 6.3 Static checks

- `.venv/bin/python -m py_compile` on every changed Python module and test:
  exit 0.
- `git diff --check`: exit 0.

## 7. Live Supabase status

**No SQL was executed. No Supabase connection was made. The migration has NOT
been applied or live-verified.** The owner must review and apply it manually.
Consequently:

- live RLS behavior was not tested;
- live FK cascade behavior was not tested;
- live CAS/unique-constraint concurrency was not tested;
- no claim is made that the durable repository is working against a live
  database.

Until the owner applies the migration, every durable History read and write
raises `HistoryStorageUnavailable`; the repository never reports the state as
durably persisted and never substitutes process memory.

## 7.1 Merge integration with the latest upstream

While Phase 3B was being delivered, `origin/main` advanced from the merged
base `5f8437d` to `e2f1619` (10 newer History/Appearance/reaction/bandwidth
commits). The upstream changes were merged normally; the delivery commit
`a12c207` has parents `417a6b8` and `e2f1619`.

Audit of the merge (blob comparison against both parents):

- The merged Appearance implementation and suites are byte-identical to
  upstream (`backend/bot/handlers/appearance.py`,
  `backend/services/appearance_service.py`, `test_appearance_delivery.py`,
  `test_appearance_gallery.py`, `test_appearance_image_cache.py`).
- `tests/test_appearance.py` was deleted by the upstream line itself (absent at
  `e2f1619`); the merge did not remove local coverage. Its replacement suites
  are present with upstream blobs, so no Appearance coverage was lost.
- The merge did leave three files from the pre-upstream Appearance
  implementation in the tree (`backend/services/image_probe.py`,
  `backend/services/preview_storage.py`,
  `sql/panel_settings_appearance.sql`). None existed upstream, none is imported
  or referenced by code or tests, and the manual SQL script duplicates the
  Appearance columns already carried by migration part 15. The audit commit
  removes all three; no live behavior changes.
- The merge took upstream's History-focused ROADMAP wholesale and dropped the
  local Appearance roadmap section. The audit commit integrates both (Phase 3A
  complete, Phase 3B implemented, Appearance Stage 2/validation and Phases 4–7
  preserved).
- No Phase 3B code was dropped.

## 8. Commit SHA and remote verification

The Phase 3B implementation is commit
`417a6b84f86675993a45858fd3d73e1bcdaa9256`; the delivered merge commit audited
here is `a12c20779bc634cb4511fb0e79443b882e8a1a85` (parents `417a6b8`,
`e2f1619`). The audit corrections and this report are the commit that contains
it. No rebase, force or history rewrite was used. The exact HEAD and the
remote/ancestry/clean-tree verification results are reported in the delivery
message. They can be re-checked with:

```bash
git rev-parse HEAD
git ls-remote origin refs/heads/main
git merge-base --is-ancestor <commit> HEAD
git status --porcelain
```

If a safe normal push cannot be completed, the delivery stops and reports the
blocker instead of claiming success.

## 9. Remaining limitations

- Migration is unapplied; owner action is required.
- No live Supabase integration test exists in this phase.
- No scheduler/executor calls the repository or payload yet.
- Summary compaction is an explicit reset, not automatic.
- Deleted Telegram content cannot be recovered; the prior summary is retained.
- `history_schedules` has no owner FK because there is no owner table; owner
  scoping relies on the repository plus `UNIQUE (owner_id, peer_id)`.
- A completed state is terminal; no reopen path is implemented.
- The pre-existing scheduler test failure remains.

## 10. Explicit Phase 4+ status

Phase 4 and all later phases were **not implemented**. There is no scheduled
History execution, no scheduler/lifecycle integration, no schedule UI, no
natural-language schedule parsing, no notification, no catch-up/missed-run
policy, no second scheduler/worker/executor, no second Telegram client and no
second update loop in this change.
