# IMPLEMENTATION_REPORT.md — Anti-Edit & Anti-Delete (dedicated database foundation)

> This file describes ONLY the current state of the repository with respect to
> the Anti-Edit / Anti-Delete (AED) archive feature, including the **dedicated
> database foundation** delivered in this round, and **replaces every previous
> report in full** — it is not an appended report. The earlier reports — the
> peer-resolution reliability round (`6bec694`/round 4), the bot-exclusion round
> (`ad89419`), the archive-UX round (`33e1fd3`), the implementation round
> (`d862520`) and the inspection record (`cbeb6c1`) — remain readable in git
> history. Evidence-level records live in
> `docs/investigations/anti-edit-delete-status.md`.

| Item | Value |
|---|---|
| Stage | **Implementation — AED dedicated database foundation delivered (round 5)** |
| Date | 2026-10-17 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`) |
| Branch | `main` |
| Base commit | `89a3007baeac1e8c3d0446d9981d2bde8bbe60a3` — verified `= origin/main` at task start |
| Working tree at start | clean except untracked `.openhands/` (agent memory) |
| Live Telegram / Render / Supabase | **NOT verified.** No `.env`, no credentials, no deployment access. No Telegram request and no SQL statement was executed in this workspace |

---

## 1. What this round delivers

AED previously persisted **everything** through the generic `bot_settings`
key/value store: ONE configuration document
(`anti_edit_delete_config:<owner>`) plus a fixed set of JSON shard documents
(`anti_edit_delete_archive_shard:0..3`). This round gives the feature its own
**two dedicated tables** and moves the runtime onto them, following the exact
precedent of Private Message Management (PM, §33).

The change is **additive and behaviour-preserving**: before the migration is
applied the feature keeps its current behaviour through the legacy shards; after
it is applied the dedicated tables are the ONLY store. The two stores can never
disagree because ONE process-local decision (`aed_store_mode()`) selects exactly
one of them for the life of the process.

### Exact files changed

| File | Change | Reason |
|---|---|---|
| `supabase/migrations/20261017000001_create_anti_edit_delete_tables.sql` | **NEW.** Two tables — `aed_config` (ONE row per owner: `enabled`, `categories text[]`, `retention_days` 1..3650, `updated_at`) and `aed_archive_entries` (one row per observed archive record: deterministic `id` PK, `owner_id`, positive private `chat_id` (`CHECK > 0`), `message_id`, `kind` `snapshot`/`edit`/`delete`, `category`, `has_caption`, `captured_at`, `message_date`, `edited_at`, `sender_id`, `out`, `has_media`, bounded `body` (`CHECK length <= 4096`), media metadata, `recoverable`/`recovered_from`, bounded peer identity, `updated_at`); one read index; RLS + SELECT-only `anon`/`authenticated` policies on both; comments; `NOTIFY pgrst`; table/column verification queries; a read-only legacy probe | Dedicated, indexable, row-addressable persistence instead of JSON shards in a shared document |
| `backend/db/client.py` | **+282 lines.** AED dedicated repository: fallback keys `aed_config` / `aed_archive_entries`; `aed_store_mode()` (ONE store decision per process, only a DEFINITIVE "table missing" result is cached), `reset_aed_store_mode()`, `aed_load_config` / `aed_save_config`, `aed_load_entries` / `aed_load_all_entries` / `aed_save_entries`, `aed_delete_entries`. Every function returns `None`/`False` on failure (never raises). The legacy `aed_get_document` / `aed_put_document` / `aed_load_archive` / `aed_save_archive` helpers are **kept unchanged** | Repository layer for the two tables + the one-store routing, mirroring the PM repository functions |
| `backend/services/anti_edit_delete_service.py` | Config and rows now route through the store mode; `cleanup_expired` gains a dedicated branch that reads all owners' rows, reads each owner's retention, and deletes the expired ids with one bounded `delete ... in_`. No public signature changed; fail-closed semantics preserved | Move the runtime onto the dedicated tables without changing behaviour or the legacy path |
| `tests/test_anti_edit_delete.py` | **+13 tests (82 → 95).** The fake DB now models `bot_settings` + both dedicated tables and can simulate a database where the migration has NOT been applied (`has_aed_tables=False`). New section 16 pins: the store decision (dedicated / legacy), capture/config/row persistence into the dedicated tables, reload after restart, distinct row kinds, owner isolation, duplicate-id idempotency, malformed-row tolerance, honest failed write, retention deletion from the dedicated store, the migration's scope CHECK, and the legacy shards still working before the migration | Pin the dedicated-store contract end to end |
| `tests/test_canonical_schema_reconciliation.py` | Classify the two new tables in `SUCCESSOR_TABLES`; part banners `PART 1/2 of 10` → `of 11` | The reconciliation test requires every migration-created table to be classified |
| `tests/test_database_setup_order.py` | Add `ANTI_EDIT_DELETE_TABLES` to `DOCUMENTED_ORDER` (last); split the old PM "is last" assertion into an AED "is last" test + a PM "carries the two tables" test; update the migration count to 33 and the deviations list to name the AED migration and `aed_archive_entries` | The AED part is now the newest additive change, closing the ONE setup block |
| `tests/test_private_message_scope.py` | `part 10 of 10` → `part 10 of 11` | The PM part number moved when the AED part was appended |
| `DATABASE_ARCHITECTURE.md` | §34 (new): the AED persistence section. §31: migration count 32 → 33, `eight` → `eleven` later additive migrations, all part banners `of 10` → `of 11`, the AED audit row 11, and PART 11 embedded verbatim (statement-identical to the migration). §20: the two new fallback rows. Migration Status: the AED migration row 23. §31.4/§31.5/§31.6 updated for parts 10/11 | Keep the single source of truth authoritative, per the AGENTS.md rule that code and docs move together |

**No** scheduler, worker, update loop, second recovery owner, env var, or
Supabase write was added. The AI has no tool that reaches either table.

---

## 2. Design decisions (and why)

* **ONE store per process.** `aed_store_mode()` probes once whether the
  dedicated tables are readable and then routes EVERY AED read/write to that one
  store. This makes "before the migration" and "after the migration" mutually
  exclusive, so the two stores can never disagree. Only a DEFINITIVE
  "table missing" result is cached; a transient network/timeout error is used
  for that call only and re-probed next time (a transient blip must not pin the
  feature to the legacy store forever).
* **No data migration in this round.** The migration moves no existing record.
  The safe migration of the pre-existing archive is a SEPARATE, later task; this
  is documented in the migration header and in §34.5.
* **Scope stays code, with one structural backstop.** The human-vs-bot decision
  is code (unchanged); the migration adds only `CHECK (chat_id > 0)`, which makes
  storing a group/supergroup/channel row (negative ids) impossible. It is
  deliberately the only scope-shaped constraint.
* **Fail closed.** Every dedicated repository function returns `None`/`False` on
  failure, so the service never mistakes an unreadable store for an empty one; a
  refused write leaves the in-memory state unchanged and the panel reports the
  failure honestly.
* **Idempotency preserved.** The `id` primary key is the deterministic archive
  id; the repository upserts on `id`, so a redelivered update can never store the
  same record twice.
* **Retention unchanged.** Still read per owner from the config and applied by
  the EXISTING periodic memory-cleanup worker (`cleanup_expired`) — no scheduler
  is added.

---

## 3. Behaviour contract after the change

* **Before the migration** — the store probe finds no dedicated table; the
  feature reads and writes the legacy `bot_settings` shard documents exactly as
  before. Nothing about the running service changes.
* **After the migration** — the dedicated tables are the ONLY store; the legacy
  shards are neither read nor written. Records written before the migration are
  not shown until the separate data-migration task runs.
* **Config** — `enabled` / `categories` / `retention_days` live in the single
  `aed_config` row per owner; a missing row resolves to the fail-closed default
  (feature OFF), exactly like the legacy missing-document path.
* **Rows** — `load_rows_async` bounds to `MAX_ARCHIVE_ROWS`, filters by owner,
  sorts newest-first; `_save_rows` writes the complete bounded set (upsert on
  `id`, then prune the owner's rows no longer present).
* **Cleanup** — expired rows are deleted by id in one bounded `delete ... in_`;
  media objects are deleted only after the durable row delete lands.
* **Idempotency / owner isolation / fail-closed** — unchanged in spirit and now
  pinned by tests (see §4).

---

## 4. Verification actually executed (local only)

| Check | Result |
|---|---|
| `python -m pytest tests/test_anti_edit_delete.py -q` | **95 passed** |
| `python -m pytest tests/test_canonical_schema_reconciliation.py tests/test_database_setup_order.py tests/test_private_message_scope.py -q` | **all passed** |
| `python -m pytest tests/ -q` (full suite) | **6396 passed, 27 skipped, 0 failed, exit 0** (the 27 skips are the pre-existing opt-in/live tests) |
| `python -m compileall -q backend/` | **exit 0** |
| `git diff --check` | **clean** |
| Part 11 statement identity | The PART 11 embed in §31.3 is **statement-identical** to the migration file (35/35 normalized statements match) |
| Remote state | `git fetch origin` → `origin/main` = `89a3007…` = local `HEAD` (base commit; the delivery commit is reported in §8) |

The dedicated-store tests use a fake database that models the real PostgREST
shapes (`select`/`insert`/`update`/`upsert`/`delete`, `eq`/`in_`/`not_`/`order`/
`limit`) and can simulate the migration not being applied; they exercise the REAL
service code paths, not mocks of the service.

---

## 5. What was NOT tested or verified

* **No live Telegram, no Render, no Supabase.** Nothing was deployed; no SQL was
  run against any project; the running service's logs were not inspected. The
  migration exists as SQL in this repository only; §30.12's limitation applies.
* **No data migration** of the pre-existing `bot_settings` archive was performed
  or tested — it is explicitly out of scope for this round (§34.5).
* The dedicated-store tests run against the in-memory fallback and the fake DB
  (the `supabase`/`postgrest` client is not importable in this environment);
  real PostgREST round-trips were not observed.
* The `aed_store_mode()` "definitive vs transient" distinction is unit-tested via
  the fake DB's missing-table error; a real transient network error was not
  reproduced.

---

## 6. Previous findings that remain accurate (carried forward)

* **Round 4 — peer-resolution reliability (verified, still in force):**
  `peer_bot_status` uses ONE shared in-flight resolution per peer, a bounded
  temporary cooldown map, and honours `FloodError` (FloodWait/FloodPremiumWait)
  for the server duration capped at 3600 s. The unrelated production-log errors
  (private-message FloodWait, keepalive `GetUsersRequest`, `QueryIdInvalidError`,
  Supabase config reads) were NOT fixed and are not claimed fixed here.
* **Bot exclusion (round 3):** the capture path classifies from the event's own
  entity; the browse path classifies through the shared `filter_bot_items`;
  unresolved peers stay unknown and are never deleted or permanently suppressed.
* **Inspection record:** `docs/investigations/anti-edit-delete-status.md` is left
  untouched; its round-3 record of the exclusion boundary remains accurate.

---

## 7. Remaining limitations / risks

* Until the owner applies the migration, the archive still lives in the shared
  `bot_settings` shards (one document per shard, whole-shard rewrites).
* After the owner applies the migration, records written before it are not shown
  until the separate data-migration task runs (documented, deliberate).
* A genuinely unreachable peer still retries once per cooldown expiry
  (round-4 behaviour, unchanged).
* The migration is not idempotency-safe against a *concurrent* manual run and an
  application boot racing the schema cache reload, but every statement is
  `IF NOT EXISTS` / idempotent, so re-running is safe.

---

## 8. Delivery state

* **Commit:** one commit on `main`, task-scoped files only; pushed as
  `HEAD:main`.
* **Remote verification:** `git fetch origin` → `origin/main` equals the commit
  SHA; `git merge-base --is-ancestor` exit 0; 0 ahead / 0 behind. The exact SHA
  is reported in the task summary.
* **Working tree:** clean except the untracked `.openhands/` (agent memory,
  never committed).
* **Explicit confirmation:** no SQL was executed and no Supabase object was
  changed; the migration is a repository file only.

---

## 9. Next stage

`Implementation` continues with the **next isolated task** in the sequential
plan: the safe migration of the pre-existing `bot_settings` archive into the
dedicated tables (a read-once backfill, following PM's `_import_legacy_actions`
precedent), which is the only prerequisite before the legacy shards can be
retired. The owner must first apply
`20261017000001_create_anti_edit_delete_tables.sql` (part 11 of the ONE setup
block in §31.3) for the dedicated store to take effect.
