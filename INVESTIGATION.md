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

- Migration: `supabase/migrations/20261114180758_create_history_schedules.sql`
  (applied; the filename matches the recorded remote version).
- Inventory: 42 repository migration files; the setup block has 18 parts.
- `DATABASE_ARCHITECTURE.md`:
  - §16.1 documents the table, columns, constraints, index, FK, RLS and
    repository contract;
  - §31.1 classifies the migration as part 18 of 18;
  - §31.3 embeds the migration verbatim as part 18;
  - §22 records it as **APPLIED** (remote version `20261114180758`, 2026-11-14);
  - §30.11 lists it among the parts of the one-paste action;
  - §31.4/§31.5/§31.6 describe its part-specific rollback and verification.
- The documentation tests were updated from the merged 41/17 baseline to
  42/18, and the canonical schema successor-table classifier now knows
  `history_schedules`.

## 10. Risks and limitations confirmed by inspection

- The live migration is applied and its history record is `20261114180758`;
  the repository filename now matches it. When the store is unavailable (or the
  table absent in another environment) every durable History operation fails
  closed with `HistoryStorageUnavailable`; process memory is never substituted,
  so a checkpoint can never be reported successful and then lost on restart.
- Live schema/access checks and one transactional CAS probe were performed by
  the owner and rolled back with no test row left behind. Full production
  History execution and live Telegram retrieval were **not** executed; the
  fake-client query shape and the in-memory fake remain the unit-test basis.
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

Phase 3B is implemented and audited. The migration is applied to the connected
Supabase project; live schema/access checks and the transactional CAS probe
were performed. The CAS probe was rolled back and left no test row behind.
Full production Scheduled History execution and live Telegram retrieval remain
unimplemented and unverified.

### Migration version reconciliation (2026-11-14)

The migration was applied by the owner while the repository still named it
`20261113000001_create_history_schedules.sql`; Supabase recorded version
`20261114180758` for the same migration name. The repository file was renamed to
`20261114180758_create_history_schedules.sql` with its SQL byte-identical
(verified by blob hash), and every active reference — `DATABASE_ARCHITECTURE.md`
(inventory, §16.1, §22, part 18 banner and setup list), `ROADMAP.md`,
`IMPLEMENTATION_REPORT.md`, this document, `tests/test_database_setup_order.py`
and `tests/test_private_message_db_security.py` — was updated. No SQL was
executed, no migration was re-applied, and neither the live table nor
`supabase_migrations.schema_migrations` was modified.

## 12. Explicit non-goals

Phase 3B does not implement TaskScheduler integration, lifecycle hooks,
scheduled History execution, schedule UI, natural-language parsing,
notifications, catch-up/missed-run policy, a second scheduler/executor/client/
loop, or unrelated AI/provider changes. Phase 4 and later phases are not
implemented.

> Temporal note: the Phase 3B investigation above is preserved as recorded.
> Phases 4 and the Phase 5 prerequisite were subsequently implemented as
> documented in `ROADMAP.md` and `IMPLEMENTATION_REPORT.md`. Phase 6 remains
> deferred and Phase 7 remains the live-validation phase.

## 13. Diagnostic tracing for the first live Scheduled History run

An observability-only occurrence trace was added after the Phase 4/5 work. It
does not change the diagnosis or invariants above; it makes the deferred live
validation diagnosable without a new scheduler or persistence path.

- `SCHEDULED_OCCURRENCE_TRACE` uses the exact durable occurrence key as its
  stable `trace_id` and carries a separate `attempt`. Scheduler, coordinator,
  tool, History service, AI/provider and repository-history events share it.
- `HISTORY_SCHEDULE_TRACE` covers owner creation, the separate task/state
  writes and compensation with a short-lived creation id.
- The trace remains silent for ordinary Telegram/AI requests because separate
  context variables are unbound outside their scopes. The existing
  `AI_TASK_TRACE` create-task binding is untouched.
- The expected first-run order and the method for identifying the first missing
  stage are recorded in `IMPLEMENTATION_REPORT.md` §3. In particular, the last
  emitted occurrence stage before the trace stops localizes the boundary;
  peer refusal, retrieval failure, provider failure, invalid output, CAS
  conflict and durable error have distinct stable categories.
- No live occurrence or live Supabase operation was exercised by the tracing
  tests. The earlier conclusion remains: only the owner-performed live run can
  validate the real Telegram/provider/deployment behavior.

## 14. Verified root cause — Scheduled History posted its summary to Saved Messages

Source inspection of the delivery chain established a second, independent defect
from the provider/failure-metadata findings above.

**Exact path that delivered:**

1. The owner wizard built the Scheduled History candidate with a delivery
   opt-in and the owner's own chat id (`backend/bot/handlers/history.py`,
   `notification_destination: {"chat_id": int(_OWNER_ID), "deliver_result": True}`).
2. `TaskExecutionCoordinator` persisted that destination on the `ai_tasks` row
   and passed it unchanged into execution.
3. After a successful occurrence, `TaskExecutionCoordinator._deliver_result`
   (`backend/ai/task_execution.py`) saw `deliver_result is True`, joined the
   action result messages and called `telegram.send_message(chat_id, text)`.
4. `scheduled_history` returns the cumulative summary as its result message
   (`backend/services/history_schedule_service.py`), and the configured chat is
   the owner's own user id — Telegram Saved Messages. The summary was therefore
   posted there after every committed occurrence.

The persistence itself was correct: the same run commits summary + cursors +
revision through one CAS write. Only the post-commit delivery step was wrong.

**Corrected behavior (implemented):**

- The wizard now writes `notification_destination = {}`. No `deliver_result`,
  and no `chat_id` — the action already reads its peer from the durable
  `history_schedules` row, so the destination field was unnecessary.
- `_deliver_result` additionally refuses any task whose actions include
  `scheduled_history`, which covers definitions already persisted with the old
  opt-in. Other tasks keep their existing `deliver_result` behavior, and the
  separate `notify_on_outcome` opt-in is untouched.
- The cumulative summary is now read through the new **Summary Archive** under
  History & Intelligence (`history_archive` / `history_archive_item`), backed by
  a bounded, owner-scoped `TaskRepository.list_history_schedules` page. The
  archive is read-only: it does not regenerate the summary, call a provider, or
  send any Telegram message.

**Limitation:** this is verified from source and offline tests. No live
scheduled occurrence was executed, so the real account's Telegram behavior is
still owner-verified only.

---

## Outbound bandwidth containment (2026-11-23)

Source-verified against `77e451be69c222bb3a43f7bd3c53a5d44ae775cc`. Render's
bandwidth meter showed ~3.8 GB; the application limits are 150,000,000 bytes/day
and 4,000,000,000 bytes/month and were NOT changed.

### 1. Verified root causes

**A. A reservation could succeed without any durable accounting.**
`backend/services/outbound_bandwidth_service.py::_read_row_state()` returned `{}`
when `_db_configured()` was false, and `reserve_outbound_bytes()` then took the
`if not _db_configured(): ok = True` branch. A runtime without a readable
`panel_settings` row therefore received a SUCCESSFUL reservation that was never
persisted. The limits existed only on paper for that deployment. The read API
already disagreed with the write API about the same condition:
`get_outbound_usage()` reported `available: False` while the write path reported
success.

**B. Malformed persisted state was read as zero usage.**
`_normalize_state()` returned `(0, 0)` for any non-dict value and tolerated
`int(...)` conversion failures per window. A corrupted, partial or future-version
JSONB value therefore RESET both counters instead of denying.

**C. Three Telegram upload paths treated the governor as optional.**
`backend/telegram_api/media.py::send_voice`, and both
`backend/services/save_service.py` upload sites, wrapped the reservation in
`if _outbound_bandwidth_service is not None:` plus
`getattr(..., "reserve_outbound_bytes", None)`/`callable(...)`. An import failure
or a renamed function silently skipped accounting while the upload proceeded.

**D. Two Telegram upload paths had no accounting at all.**
`backend/ai/tools/delivery.py::_send_text_attachment` called
`sender.send_file(...)` directly, and
`backend/services/retrieve_service.py::do_change_file_name` re-uploaded the
downloaded media through `client.send_file("me", tmp_path, ...)`.

**E. The album path reserved the wrong event and risked double counting.**
`_deep_save_album` reserved the album TOTAL immediately before the grouped
`send_file("me", media_items, ...)`. The real byte transfer happens earlier, per
member, in `client.upload_file(tmp_path)`. So the album's bytes were transmitted
BEFORE any reservation, and a naive per-member reservation would have counted
the same bytes twice (once at each member upload, again at the grouped send that
only attaches already-uploaded handles).

**F. The Gemini reservation helper swallowed a missing governor.**
`_reserve_gemini_bandwidth()` returned silently when the import failed or the
callable was absent. It also preferred the ASYNC entry point and ran it with
`asyncio.run()` from the media boundary's worker thread, which cannot share the
module's `asyncio.Lock` with the main loop.

**G. The process lock did not cover worker threads or a second loop.**
`_lock` was an `asyncio.Lock`. `reserve_outbound_bytes_sync()` created a fresh
event loop (directly or in a `ThreadPoolExecutor`), so a reservation from a
worker thread either bypassed the lock's loop or failed to bind to it.

### 2. Complete outbound upload audit

Every actual byte-transfer mechanism in `backend/` was classified.

| # | Site | Mechanism | Classification | Before | Now |
|---|---|---|---|---|---|
| 1 | `telegram_api/media.py::send_voice` | `client.send_file(BytesIO, voice_note=True)` | new transfer | optional governor | mandatory, denies on failure |
| 2 | `services/save_service.py::_deep_save_one` | `client.send_file("me", tmp_path)` (Telethon uploads inside) | new transfer | optional governor | mandatory, once, with the downloaded `actual_size` |
| 3 | `services/save_service.py::_deep_save_album` | `client.upload_file(tmp_path)` per member | new transfer | NOT governed | mandatory per member, with that member's own size |
| 4 | `services/save_service.py::_deep_save_album` | `client.send_file("me", [InputMediaUploaded*])` | reuse of already-uploaded handles | reserved the album total at the wrong point | deliberately NOT reserved (no new bytes) |
| 5 | `ai/tools/delivery.py::_send_text_attachment` | `sender.send_file(BytesIO, force_document=True)` | new transfer | NOT governed | mandatory, with `len(payload)` |
| 6 | `services/retrieve_service.py::do_change_file_name` | `client.send_file("me", tmp_path, ...)` | new transfer (Telegram cannot rename in place) | NOT governed | mandatory, with the downloaded file size |
| 7 | `services/retrieve_service.py::do_retrieve` / `do_bulk_send` | `client.forward_messages(...)` | Telegram-side forward, no bytes uploaded from this host | n/a | unchanged |
| 8 | `services/retrieve_service.py` caption sync | `client.edit_message(...)` | metadata edit only | n/a | unchanged |
| 9 | `services/appearance_service.py::_upload_sync` | Supabase Storage `.upload(key, data)` | external-provider upload | NOT governed | mandatory (sync entry point; runs via `to_thread`) |
| 10 | `services/anti_edit_delete_service.py::store_media_snapshot` | Supabase Storage `.upload(object_name, bytes)` | external-provider upload | NOT governed | mandatory; denial means "not archived", never a silent transfer |
| 11 | `services/gemini_media_engine.py` (`_run`, `_run_consensus`, `_run_dedicated_transcription`) | Gemini inline base64 / Files API upload | external-provider upload | optional import, async-in-thread | mandatory sync reservation, conservative bound (base64 + 4096) |
| 12 | `services/save_service.py` text branch | `client.send_message("me", caption)` | text only | n/a | unchanged |
| 13 | `ai/tools/delivery.py` text paths | `event.edit` / `event.reply` / `send_message` | text only | n/a | unchanged |
| 14 | every `download_media(...)` (save, rename, anti-edit snapshot, media_service) | Telegram download | download only (ingress) | n/a | unchanged |
| 15 | `telegram_api/media.py::download_media` | Telegram download | download only | n/a | unchanged |

`InputFile` / `InputMediaUploaded*` appear only as the handles produced by
(3) and attached by (4); no other module constructs them.

### 3. What the governor now guarantees

- `DAILY_LIMIT_BYTES = 150_000_000` and `MONTHLY_LIMIT_BYTES = 4_000_000_000`
  are unchanged, and both are checked on every reservation.
- No database, an unreadable/missing `panel_settings` row, a malformed or
  unknown-version state, a refused write, a raised write and a write that cannot
  be confirmed in stored state all raise
  `OutboundBandwidthUnavailableError` — the caller never receives a successful
  reservation, and no upload follows.
- `{}` is the documented fresh-row value and is a REAL zero. Anything else that
  is not exactly the shape this module writes is a denial.
- A previous day/month is a legitimate rollover (zero for the new window), not
  malformed state.
- A reservation is returned only after re-reading the stored state and matching
  the expected new totals (bounded to `_MAX_CONFIRM_ATTEMPTS` = 3).
- `byte_count <= 0`, non-integers and booleans are rejected, so an unmeasurable
  payload can never be booked as zero bytes.
- Reservations are conservative and never refunded: a failed or timed-out
  upload keeps its bytes accounted (`release_reservation` remains a no-op).

### 4. Concurrency: what is proven, and what is not

`_lock` is now a `threading.Lock` held across read -> limit check -> write ->
confirm. It serializes every reservation in the process across all event loops
and worker threads, which is the supported deployment model (ONE
`RuntimeSupervisor`, one self client, one process). The queue test
(`12 x 100 bytes` against a 1000-byte day) accepts exactly 10 and rejects 2, and
the stored total is exactly 1000.

It is **not** a distributed lock. Two independent processes could still
interleave between our read and our write; the confirmation read detects a
dropped/lost write but cannot detect an overwritten increment whose value
happens to equal our expected total. Truly atomic cross-process accounting
needs a database-side compare-and-set (a SQL function or a migration). No SQL
was executed, no migration was added, `DATABASE_ARCHITECTURE.md` was not
modified, and no parallel accounting store or in-memory production fallback was
introduced.

### 5. Egress this governor still does not measure

The governor accounts for explicitly governed PAYLOAD transfers only. It does
not measure, and must not be described as measuring, Render's total egress:

- HTTP responses: `/health`, the dashboard SPA and `/api/*`, the static guide
  site, and the appearance image renditions Telegram fetches.
- Telegram protocol traffic itself (updates, RPC frames, keepalive pings) and
  helper-bot traffic.
- Supabase request/response bytes for every read and write, including the
  accounting write itself.
- Any second process/deploy overlap (see §4).
