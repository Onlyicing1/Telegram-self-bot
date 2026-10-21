# INVESTIGATION.md — persistence of AED, Private Message Management and Auto-Reaction

Investigation of **Anti-Edit / Anti-Delete** (AED), **Private Message Management**
(“PV Manager”, PM) and the **Auto-Reaction** setting on `main`. It replaces every
previous investigation in full; earlier rounds (Supabase transport, AED bot
exclusion, AED/PM status audit) remain in Git history, and
`docs/investigations/anti-edit-delete-status.md` still carries the older
narrative. Nothing outside this file was changed by the investigation itself.

## 1. Scope

| Item | Value |
|---|---|
| Date | 2026-10-20 (UTC) |
| Repository | `Onlyicing1/Telegram-self-bot` (`origin`) |
| Branch / commit | **`main`** @ **`d33dbe27956aaf3819b27ae2ce416856f9de968f`**, verified `= origin/main` (`git fetch origin`, `git rev-parse origin/main`) |
| Working tree at start | only the pre-existing unrelated `INVESTIGATION.md` modification in the shared clone plus three untouched worktrees (`.aedwork`, `.m14`, `.taskrt`) |
| Runtime | CPython 3.10.12 venv; `supabase 2.4.2`, `telethon 1.43.0` |
| Live Telegram / Render / Supabase | **not accessed** — no credential, no connection, no SQL |

The task under investigation is the owner's report that **parts of these
features repeatedly reset after Self Bot updates/redeployments**. That report is
treated as a persistence defect: every durable field was traced from the panel
write → the repository write → the table, and from the table → the load →
the in-memory cache → the next restart.

## 2. Executive summary

Three **confirmed** defects explain resets, plus one schema gap and one
structural weakness. None of them is a “table does not exist” problem.

| # | Defect | Feature | Confirmed by |
|---|---|---|---|
| 1 | The legacy → dedicated transition has **no backfill**. When `aed_config` becomes readable for the first time, `_read_config` returns `default_state()` (enabled `false`) and `load_rows_async` returns `[]`, so the owner's legacy configuration and every legacy shard row disappear from view at the first restart after the migration. | AED | `anti_edit_delete_service.py::_read_config` / `load_rows_async`; §34.5 says the import was deferred |
| 2 | Every AED capture handler gates on the **synchronous** `service.is_enabled()` **before** the asynchronous load is awaited, so a message/edit/deletion that arrives during the startup preload window is silently dropped after every redeploy. | AED | `anti_edit_delete.py` handlers; `register()` starts the preload as a background task |
| 3 | An **unreadable** configuration row is indistinguishable from **no row**: `aed_load_config` / `get_private_message_config` return `None` for both, and both services then load defaults and mark themselves loaded. The next panel change persists those defaults over the real row. For PM the failed read also falls through to the **legacy document** and writes it over the dedicated row. | AED + PM | `client.py::_get_aed_config_sync` / `_get_private_message_config_sync`; `private_message_service._read_state_document` / `_load_task_body` |
| 4 | **No migration creates** `panel_settings.auto_reaction_enabled` / `auto_reaction_emoji`, although `settings_service` reads and writes both and `auto_reaction_service` consults them for every outgoing message. The write is rejected by the database and the value survives only in the process cache — reset on every redeploy. | Auto-Reaction | zero grep hits in `supabase/`; §6 documented them as “PROPOSED … no migration file creates it” |
| 5 | A dedicated-store write can fail **silently at the service boundary** when no owner is configured: PM's `_write_state_row` returns `True` without writing anywhere. | PM | `private_message_service._write_state_row` |

Defect 3 is the one that turns a transient outage into permanent data loss: the
defaults are not merely shown, they are written back. Defects 1 and 2 reproduce
the owner's “after an update” symptom exactly (a redeploy changes the store mode
and drops the startup window).

## 3. AED — complete persistence audit

### 3.1 Configuration

| Field / state | Authoritative storage | Read path | Write path |
|---|---|---|---|
| `enabled` | `aed_config.enabled` (dedicated) / `bot_settings['anti_edit_delete_config:<owner>']` (legacy JSON) | `aed_load_config` → `_read_config` → `sanitize_state` → `_state` | `_persist_state` → `aed_save_config` (upsert by `owner_id`) / `aed_put_document` |
| `categories` (`text[]`, never empty) | `aed_config.categories` | same | same |
| `retention_days` (1–3650) | `aed_config.retention_days` | same | same |

Store selection: `db_client.aed_store_mode()` probes `aed_config` **once per
process** and caches only a definitive answer (`42P01`/`PGRST205`/“does not
exist” ⇒ `legacy`; readable ⇒ `dedicated`; `get_db() is None` ⇒ `dedicated` over
the in-memory fallback). A transient probe failure is not cached and falls back
to `legacy` **for that call only**.

Startup: `register()` binds the owner and starts `ensure_loaded_async()` as one
background task. `_state` is the only cache; `state()`, `is_enabled()`,
`categories()`, `retention_days()` never perform I/O.

### 3.2 Archive

| Field / state | Storage |
|---|---|
| one row per observed version (`id` = deterministic SHA-1 of owner/chat/message/kind/version, `owner_id`, `chat_id` (>0), `message_id`, `kind`, `category`, `has_caption`, `captured_at`, `message_date`, `edited_at`, `sender_id`, `out`, `has_media`, `body`, `media_type/name/size/stored/object`, `recoverable`, `recovered_from`, `peer_id/username/name`) | `aed_archive_entries` (dedicated) or four `bot_settings['anti_edit_delete_archive_shard:0..3']` JSON lists (legacy) |
| Read | `load_rows_async` → `aed_load_entries(owner)` (dedicated) / `aed_load_archive(keys)` (legacy); owner-filtered, sorted by `captured_at DESC`, bounded to `MAX_ARCHIVE_ROWS = 500`; `None` = unreadable (fail closed) |
| Write | `_save_rows` → `aed_save_entries(owner, rows)` = upsert by `id` then prune the owner's rows no longer present / `aed_save_archive(keys, shards)` |
| Deletion | explicit single/group/clear, and `cleanup_expired` (the existing `memory_cleanup` worker; no second scheduler) |

### 3.3 What breaks (confirmed)

1. **No legacy → dedicated backfill.** `_read_config` (dedicated mode) returns
   `default_state()` when `aed_load_config` returns `None`; `load_rows_async`
   returns `stored` (empty list) as-is. The legacy document and shards are never
   read once the probe says `dedicated`, so the first restart after the tables
   appear loses the configuration and hides every previously archived row
   (§34.5 documents the deferral; the user experiences it as a reset).
2. **Startup capture race.** The three handlers check `service.is_enabled()`
   synchronously. Before the preload completes `_state` is `{}`, so `is_enabled()`
   is `False` and the event is dropped without ever calling
   `record_message` (which is the function that awaits the load). Every redeploy
   therefore opens a window in which captures are lost.
3. **Unreadable ≠ empty.** `_get_aed_config_sync` returns `None` both for “no
   row” and for “the read failed”. `ensure_loaded_async` then stores
   `default_state()` and sets `_loaded = True`; `_persist_state` happily writes
   that default document plus the next change, so one transient error silently
   rewrites the owner's configuration.

## 4. Private Message Management — complete persistence audit

| Field / state | Column | Read/write path |
|---|---|---|
| master switch | `private_message_config.enabled` | `sanitize_state` ⇄ `_pm_config_payload` (`_PM_CONFIG_COLUMNS`) via `get_private_message_config` / `save_private_message_config` |
| mode | `.mode` (`open`/`whitelist_only`/`closed`) | same |
| delete / fixed / AI action flags | `.delete_incoming`, `.fixed_response_enabled`, `.ai_response_enabled` | same |
| fixed response text | `.fixed_response_text` (≤512) | same |
| AI allowance | `.ai_max_replies_per_person`, `.ai_unlimited_replies` | same |
| AI interval | `.ai_interval_seconds` | same |
| automatic reset | `.ai_counter_reset_enabled`, `.ai_counter_reset_seconds` | same (added by `20261016000001…`) |
| allow-list / exceptions | `.allowlist`, `.exceptions` (`bigint[]`) | same |
| temporary override | `.temporary` (JSON, expiry) | same |
| weekly schedule | `.schedule` (JSON: enabled, timezone, rules) | same |
| per-person counters | `private_message_senders.ai_replies_used`, `.last_ai_reply_at`, `.window_started_at` | `claim_private_message_ai_reply` (one CAS) / `release_private_message_ai_reply` (compensating CAS) / `reset_private_message_counters` |
| legacy Phase-1 state | `bot_settings['private_message_management']` | `get_legacy_private_message_state`, imported once and written to the dedicated row |

Every column exists in the migrations (`20260929000001…` + `20261016000001…`)
and every one is in `_PM_CONFIG_COLUMNS` except the three sender columns, which
the CAS in `client.py` writes.

### 4.1 What breaks (confirmed)

1. **Unreadable ≠ missing, and the failed read can overwrite.** `_read_state_document`
   calls `get_private_message_config`, which returns `None` on a read error; the
   service then reads the **legacy** document and `_load_task_body` writes the
   imported (stale) state into the dedicated row. If the legacy document is
   absent, defaults are loaded, `_loaded` becomes `True`, and the next panel
   change persists defaults plus that change.
2. **A failed load is treated as a completed load.** `_load_task_body` sets
   `_loaded = True` before reading and never clears it on failure, so the
   process keeps serving/caching defaults for its whole lifetime.
3. **A change can be reported durable without a write.** `_write_state_row`
   returns `True` when `owner_id()` is `None` (“state kept in memory only”). The
   panel then reports success although nothing was persisted. (The no-database
   case is different and documented: `persistence_available()` is surfaced in the
   panel as “memory only”.)
4. Counters themselves are **not** reset by a restart: the claim, release and
   reset are dedicated-table writes; the in-memory fallback is used only when no
   database is configured at all.

## 5. Auto-Reaction

`settings_service` has 14 typed settings; `_DEFAULTS` includes
`auto_reaction_enabled` (False) and `auto_reaction_emoji` (""), the validators
reject non-bool/non-str, the repository writes a real column per setting
(`update_field`), and `auto_reaction_service.process_outgoing_message` reads both
through `is_auto_reaction_enabled()` / `auto_reaction_emoji()`.

`panel_settings` (canonical snapshot + migrations) has **no** such columns:
`grep -r auto_reaction supabase/` returns nothing, and
`tests/test_database_setup_order.py` previously pinned the absence. On a Supabase
database built from §31.3, PostgREST rejects the update (unknown column), the
repository logs the failure, `set_setting` still returns `True` after caching, and
the toggle/emoji are lost on every restart. The tree documented the pair as
“PROPOSED — no migration creates it” and even suggested a manual SQL snippet,
which was in practice a second, undocumented setup step.

## 6. Migration / canonical coverage

| Object | Migration | In §31.3 (before this round) |
|---|---|---|
| `aed_config`, `aed_archive_entries` (+ index, RLS, grants) | `20261017000001_create_anti_edit_delete_tables.sql` | part 12 of 13 |
| `private_message_config`, `private_message_senders` | `20260929000001_create_private_message_tables.sql` | part 10 of 13 |
| counter-reset columns + CHECK | `20261016000001_add_private_message_counter_reset.sql` | part 11 of 13 |
| PM access hardening (service-role only, zero policies) | `20261020144423_harden_private_message_table_access.sql` | part 13 of 13 |
| `panel_settings.auto_reaction_enabled` / `.auto_reaction_emoji` | **missing** | **missing** |

AED and PM security posture is already restrictive and was **not** weakened:
RLS enabled on all four tables, zero policies, every privilege revoked from
`PUBLIC`/`anon`/`authenticated` (table and column level), only
`SELECT/INSERT/UPDATE/DELETE` granted to `service_role`. The PM creation
migration's permissive policies live only in history; part 13 removes them.

## 7. Root causes — confirmed vs. hypothesis

| Claim | Status |
|---|---|
| AED configuration and archive appear reset because the dedicated tables are authoritative but empty (no legacy import) | **Confirmed** in code, §34.5 and the tests |
| AED misses captures right after a restart because the handlers check the cache before awaiting the load | **Confirmed** in code |
| A transient read error can replace a persisted AED/PM configuration with defaults (and, for PM, with the legacy document) | **Confirmed** in code |
| Auto-Reaction settings do not survive a redeploy because no column exists | **Confirmed** statically (no migration, no column) |
| “The migration was never applied on the live project” | **Hypothesis** — the live schema is not reachable from here; the code degrades honestly (writes report failure) in that case |
| “Supabase is deleting rows” | **No evidence**; nothing in the repository deletes configuration rows, and the archive is only pruned by explicit actions/retention |
| “A second scheduler/cache resets state” | **No evidence**; there is exactly one of each |

## 8. Tests executed before editing

`pytest tests/test_anti_edit_delete.py tests/test_private_message_management.py
tests/test_private_message_db_security.py tests/test_aed_db_security.py
tests/test_database_setup_order.py tests/test_canonical_schema_reconciliation.py
tests/test_private_message_scope.py -q` → see §12 for the recorded run.

They prove the offline contracts (routing, scope, owner isolation, honest failed
writes, retention, statement identity, security posture). They do **not** prove
any of the persistence transitions above — no existing test simulates a legacy →
dedicated restart, an unreadable read, or a capture during the preload window.

## 9. Fixes (implemented in this round)

1. New additive migration for the two Auto-Reaction columns, embedded as the
   next part of §31.3, with the migration inventory/counts and tests updated.
2. AED legacy → dedicated backfill (configuration **and** archive), idempotent,
   never overwriting dedicated data, gated so a cleared archive is not
   resurrected by a later restart.
3. AED handlers await the load before reading the cached switch.
4. AED + PM: an unreadable configuration is no longer treated as an empty one;
   default state is never persisted unless it was actually loaded; both services
   refuse a mutation while the state is not known.
5. PM: the legacy fallback only runs after a **successful** dedicated read, and
   a missing owner is reported as a failed durable write instead of a success.

## 10. Live verification limits

No Supabase connection, SQL execution or migration was performed here or by any
commit reachable from `d33dbe2`. No Telegram client was started; no Render
deployment or log was read. “The archive survives in production” and “PM policy
survives in production” remain **untested claims** until the owner applies the
migrations and observes a restart.

## 11. Evidence table

| Claim | Evidence | Status |
|---|---|---|
| AED capture is gated on a possibly-unloaded cache | `anti_edit_delete.py` handlers check `service.is_enabled()` before any await | Confirmed (source) |
| AED has no legacy import | `_read_config` / `load_rows_async` read only the active store; §34.5 | Confirmed (source) |
| Unreadable config ⇒ defaults ⇒ next write persists them | `_get_aed_config_sync` / `_get_private_message_config_sync` return `None`; `_read_config` / `_read_state_document` map `None` to defaults | Confirmed (source) |
| PM can overwrite a dedicated row with the legacy document | `_read_state_document` legacy branch + `_load_task_body` write | Confirmed (source) |
| PM can report a durable save without writing | `_write_state_row` returns `True` when `owner_id()` is `None` | Confirmed (source) |
| Auto-Reaction columns are absent from every migration | `grep -r auto_reaction supabase/` → no hits | Confirmed (static) |
| The four feature tables are service-role only | migrations 20261017000001 / 20260929000001 / 20261020144423 + `test_aed_db_security.py` / `test_private_message_db_security.py` | Confirmed (static + tests) |
| Live Supabase state | no connection | **Not verified** |
| Live Telegram behaviour | no session | **Not verified** |

## 12. Tests executed after the fixes

Recorded in `IMPLEMENTATION_REPORT.md` §13 (exact commands and results).

## 13. Changes made

Source: `backend/db/client.py`, `backend/services/anti_edit_delete_service.py`,
`backend/services/private_message_service.py`,
`backend/bot/handlers/anti_edit_delete.py`; schema:
`supabase/migrations/20261020160000_add_panel_settings_auto_reaction_settings.sql`;
documentation: `DATABASE_ARCHITECTURE.md`, `ROADMAP.md`,
`IMPLEMENTATION_REPORT.md`, this file; tests: `tests/test_auto_reaction_schema.py`
(new), `tests/test_anti_edit_delete.py`,
`tests/test_private_message_management.py`, `tests/test_database_setup_order.py`,
`tests/test_private_message_db_security.py`, `tests/test_private_message_scope.py`,
`tests/test_private_message_panel.py`. No secret, credential, dependency or
unrelated feature was touched, and no SQL was executed against any database.
