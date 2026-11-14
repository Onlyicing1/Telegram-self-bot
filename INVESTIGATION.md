# INVESTIGATION — Phase 3B: Durable History State and Oldest-First Retrieval

> Source-verified against the repository at the latest upstream `origin/main`
> `e2f16192907f3f874b689fd0fcc4ef2be1b7a860`, merged normally with the Phase 3B
> commit `417a6b84f86675993a45858fd3d73e1bcdaa9256` (whose preflight base was
> `bbe0ab76614eadbb6cb5ea0485a5368c1e6d3d98`). The working tree was clean before
> Phase 3B; no pre-existing modified or untracked project files were present. Code is authoritative; this document records what the code actually
> does, not what an earlier report claimed.
>
> Phase 3A approved this design. No SQL was executed against Supabase and no
> Telegram account was contacted while producing this investigation.

## 1. Preflight and delivery state

- Branch: `freebuff/99799eefb106784388e20bc8`.
- Preflight HEAD: `bbe0ab76614eadbb6cb5ea0485a5368c1e6d3d98`.
- Phase 3B commit: `417a6b84f86675993a45858fd3d73e1bcdaa9256`.
- Latest upstream `origin/main` fetched before delivery:
  `e2f16192907f3f874b689fd0fcc4ef2be1b7a860` (10 newer History/Appearance/
  reaction/bandwidth commits). It was merged normally, not rebased or forced.
- The merge resolves the older local Appearance implementation in favour of
  the newer upstream files; the upstream Appearance suites replace the stale
  local `tests/test_appearance.py`.
- `AGENTS.md` is the project's authority file and was read before source work.
- The pre-existing unrelated merge history was preserved. No rebase, reset,
  force-push or history rewrite was performed.

## 2. What Phase 3A approved

Phase 3B adds the durable foundation for scheduled History summarization only:

1. one `history_schedules` table and one additive migration;
2. durable-only repository primitives (an explicitly injected in-memory
   repository exists only as a test fake);
3. bounded oldest-first Telegram retrieval;
4. focused tests and canonical database documentation.

It deliberately does **not** add a scheduler, lifecycle hook, executor, UI,
natural-language schedule parser, notification, catch-up policy, second
Telegram client, second update loop or second source of truth.

## 3. Existing architecture that constrained the design

### 3.1 The task repository is the single task authority

`backend/ai/database/task_repository.py` defines the abstract
`TaskRepository`, the process-wide `InMemoryTaskRepository` fallback and the
durable `SupabaseTaskRepository`. `get_task_repository()` returns the one
authority used by the runtime. Phase 3B extends that existing hierarchy rather
than creating a second persistence API.

The target task schema already provides:

- `ai_tasks` as the owning task row, with `owner_id`, `status` and a CAS
  `version`;
- `ai_task_occurrences` as one execution attempt, with bounded
  `result_metadata` / `error_metadata` (8 KiB) and an occurrence-specific
  lifecycle;
- a physical task-delete path (`delete_task`) that existing child rows follow.

### 3.2 Occurrence metadata is not a cumulative summary store

`ai_task_occurrences.result_metadata` has an 8 KiB bound and belongs to one
occurrence. A cumulative History summary is long-lived, crosses occurrences and
is hard-bounded at 32 KiB. Storing it there would mix two lifecycles, make a
failed occurrence able to erase durable coverage, and exceed the existing
bound. Phase 3B therefore uses a dedicated table keyed by owner and peer.

### 3.3 Telegram retrieval was newest-first

`backend/telegram_api/messages.py::iter_messages` passed `limit`, `from_user`,
`min_id` and `max_id` to Telethon. Telethon's `min_id` / `max_id` are exclusive
bounds. Without `reverse=True`, Telethon returns newest messages first, which is
correct for the existing collector but wrong for an incremental schedule: a
bounded newest-first page silently skips older messages between runs.

`TelegramAPI.iter_messages` in `backend/telegram_api/api.py` is the facade used
by services. Both layers were inspected before adding the smallest supported
change: an optional `reverse: bool = False` parameter that is forwarded only
when true. Existing callers keep their exact behavior.

`backend/services/history_service.py` already had bounded newest-first paging,
normalization and provenance skipping. The new retrieval is additive.

### 3.4 History AI context

`backend/services/history_ai_service.py` builds the existing request and
`_visible_body`. The repository has no `with_identifiers=False` switch to reuse;
the scheduled payload therefore constructs the boundary directly. It accepts
only message body text and the owner-authored instruction, and does not pass
message/chat/peer/sender/task/occurrence/database identifiers, dates, media
metadata, peer titles or unrelated history. Identifiers typed by a human inside
a body are content and are not claimed to be semantically redacted.

## 4. Approved ownership and lifecycle model

Exactly one cumulative state exists per `(owner_id, peer_id)` in v1.

- `UNIQUE (owner_id, peer_id)` is the ownership invariant. It is not an
  active-only partial index.
- `active`, `paused`, `completed`, `failed` and `expired` all retain the state.
  A second schedule for the same owner and peer is refused while the row
  exists, regardless of the owning task's current status.
- Pausing keeps the cumulative summary and both cursors.
- Physically deleting the owning `ai_tasks` row cascades to the state through
  `task_id → ai_tasks(id) ON DELETE CASCADE`.
- A completed scheduled task is terminal; Phase 3B does not provide a reopen
  path.
- Any replacement/transfer is explicit and owner-authorized. A legacy
  `status = 'deleted'` row is the one repair edge: a create may reuse that same
  row after resetting its task, baseline, cursors and summary and advancing
  revision/version. `deleted` is never a normal write target and is not a
  permanent block.

There is no owner foreign key because the repository has no owner table. Owner
scoping is enforced by the repository and the unique key.

## 5. Cursor and summary invariants

The table stores three message-id cursors:

- `baseline_cursor`: immutable exclusive lower bound selected at creation;
- `scanned_cursor`: exclusive lower bound through which raw messages have been
  processed (summarized or explicitly safely skipped);
- `summarized_cursor`: exclusive lower bound actually covered by `summary`.

`baseline_cursor <= summarized_cursor <= scanned_cursor` is a database CHECK.
`summarized_cursor` may be lower than `scanned_cursor` for a safe skipped-only
batch. Cursors are monotonic in the repository: an attempt to move either one
backwards is rejected before any write.

Cursor advancement rules:

- only a successful atomic commit may advance a cursor;
- provider failure, timeout, invalid/empty result, persistence failure or a
  restart before commit must not advance either cursor;
- such a failure must not replace the previous cumulative summary;
- a safe skipped-only batch may advance `scanned_cursor` without advancing
  `summarized_cursor`, but only as a successful commit;
- an over-limit summary fails; it is never silently truncated;
- deleted Telegram content cannot be recovered, so the previous durable summary
  remains authoritative.

The summary is stored as `text`, limited by
`octet_length(summary) <= 32768` (32 KiB of UTF-8), not by character count.

## 6. Atomic commit model

`revision` counts successful content revisions: summary commits and explicit
resets. A recorded failure does not change it.

`version` is the compare-and-set token. It increments on **every** accepted
write, including a recorded failure. Therefore:

- a summary commit is one CAS update of summary, both cursors, revision,
  status, version and `last_committed_at`;
- a stale `expected_version` produces no write (`None`);
- a recorded failure is one CAS update of error/status/version only, and cannot
  touch the summary or cursors;
- a reset is an explicit owner-authorized write that sets `summary = ''` and
  moves `summarized_cursor` to `scanned_cursor`; it is never automatic.

The in-memory test fake mirrors the same checks and performs each accepted
mutation as one event-loop-atomic dictionary write. It removes the state when
the owning task is physically deleted, mirroring the SQL cascade. It is never a
production substitute: when the durable store is unavailable, every History
operation raises `HistoryStorageUnavailable` and nothing is written to memory.

An empty summary is allowed only when it is equal to the current summary (the
safe skipped-only case). It can never replace a non-empty durable summary.

## 7. RLS and access model

The migration follows the repository's strongest table posture:

- `ENABLE ROW LEVEL SECURITY`;
- **zero** policies;
- `REVOKE ALL` from `PUBLIC`, `anon` and `authenticated`;
- exactly `SELECT`, `INSERT`, `UPDATE`, `DELETE` granted to `service_role`.

The backend's one client uses `SUPABASE_SERVICE_ROLE_KEY`; no browser/client
role can read the summary, cursors or error metadata. The table has no
permissive policy to drop because it creates none.

## 8. Oldest-first retrieval design

`fetch_oldest_history` in `backend/services/history_service.py` accepts:

- `after_cursor` (exclusive lower cursor, normally `scanned_cursor`);
- `upper_cursor` (exclusive upper bound captured once by the caller);
- `limit` (target messages), `page_size`, `include_ai`.

It asks the facade for `reverse=True` inside that fixed window. It defensively
sorts each page ascending and drops duplicates, skips ids outside the window,
and advances `next_cursor` for every eligible raw message, including explicitly
safe AI-provenance skips. `has_more` is conservative when a page/target bound
stopped the batch early. `HistoryBatch` returns the messages, scanned count,
exclusive `next_cursor` and `has_more`.

Passing the returned cursor back covers each message at most once even when ids
are missing (deleted/inaccessible content) or provenance messages are skipped.
No gaps are introduced by a short page; no duplicates are read across pages.

## 9. Documentation and migration inventory

- Migration: `supabase/migrations/20261113000001_create_history_schedules.sql`.
- Inventory: 42 repository migration files; the setup block has 18 parts.
- `DATABASE_ARCHITECTURE.md`:
  - §16.1 documents the table, columns, constraints, index, FK, RLS and
    repository contract;
  - §31.1 classifies the migration as part 18 of 18;
  - §31.3 embeds the migration verbatim as part 18;
  - §22 records it as **NOT APPLIED — owner action required**;
  - §30.11 lists it among the parts of the one-paste action;
  - §31.4/§31.5/§31.6 describe its part-specific rollback and verification.
- The documentation tests were updated from the merged 41/17 baseline to
  42/18, and the canonical schema successor-table classifier now knows
  `history_schedules`.

## 10. Risks and limitations confirmed by inspection

- The live Supabase table does not exist until the owner applies the migration.
  Until then every durable History operation fails closed with
  `HistoryStorageUnavailable`; process memory is never substituted, so a
  checkpoint can never be reported successful and then lost on restart.
- Live database RLS, cascade and CAS behavior were not executed or verified;
  only the fake-client query shape and the in-memory fallback were tested.
- The migration provides no automatic summary compaction. Reset is explicit.
- Deleted or inaccessible Telegram messages cannot be reconstructed.
- No scheduler or execution path calls these primitives in Phase 3B; the
  `scheduled_summary_payload` boundary similarly has no production caller yet.
- The pre-existing `tests/test_task_scheduler.py` failure is unrelated to this
  phase and was not modified.

## 11. Audit corrections (post-delivery)

An independent source audit of the delivery commit found two defects and one
merge residue, corrected in the follow-up commit:

1. **Durable-only violation.** `SupabaseTaskRepository`'s six History methods
   caught every exception and returned `self._fallback` records, so a store
   outage, timeout or unapplied migration silently became volatile process
   memory while the caller received a successful-looking row. They now raise
   `HistoryStorageUnavailable` (logging the failure once, keeping the degraded
   diagnostic) and never touch the fallback. `create_history_schedule` also
   refuses to proceed when the owning task could only be read from memory.
2. **Missing regression coverage.** Six new tests prove reads, writes, CAS
   writes and creation fail closed, that the in-memory fake is untouched, and
   that no History method references `self._fallback`. The fake client's
   `maybe_single()` now returns `None` for zero rows, matching PostgREST.
3. **Merge residue.** Three files from the pre-upstream Appearance
   implementation (`backend/services/image_probe.py`,
   `backend/services/preview_storage.py`, `sql/panel_settings_appearance.sql`)
   were left in the tree by the merge although the upstream line never had
   them and no code or test references them. They were removed; the upstream
   Appearance implementation and its three replacement suites are unchanged.

The roadmap and this document now describe Phase 3B as implemented, audited and
not live-verified.

## 12. Explicit non-goals

Phase 3B does not implement TaskScheduler integration, lifecycle hooks,
scheduled History execution, schedule UI, natural-language parsing,
notifications, catch-up/missed-run policy, a second scheduler/executor/client/
loop, or unrelated AI/provider changes. Phase 4 and later phases are not
implemented.
