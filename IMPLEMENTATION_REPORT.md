# IMPLEMENTATION REPORT — Database Architecture Audit (2026-11-09)

This report replaces the previous one. It records the source-driven audit of
`DATABASE_ARCHITECTURE.md` against the repository at branch `main`. The task was
documentation accuracy only: no SQL was executed, no Supabase project was
contacted, and no schema, migration, repository, service or runtime behaviour was
changed.

## 1. Audit scope

- Reconcile every table, column claim, migration status, migration inventory and
  canonical setup statement in `DATABASE_ARCHITECTURE.md` with the actual
  migrations and code.
- Build a feature-to-persistence inventory using explicit categories (database,
  Storage, external, local filesystem, in-memory, cache, transient, missing
  persistence, planned, legacy).
- Audit Appearance in depth: five `panel_settings` columns, the gallery's Storage
  model, bucket configuration, bounds, reset/delete behaviour, restart-survival
  boundary and RLS impact.
- Add focused, non-brittle regression tests and correct the stale test pins.
- Rewrite `ROADMAP.md` and this report for the current task only.

Out of scope and not done: SQL execution, live Supabase changes, new/changed
migrations, schema edits, RLS/grant edits, runtime refactors, unrelated code.

## 2. Source files and directories inspected

- `DATABASE_ARCHITECTURE.md` (all sections; the modified sections were re-read in
  full and the whole document was re-scanned for stale counts and claims).
- `ROADMAP.md`, the previous `IMPLEMENTATION_REPORT.md`.
- `supabase/migrations/` — all **37** SQL files, oldest
  `20260712234229_lifeos_schema.sql` to newest
  `20261109000000_add_panel_settings_appearance.sql`, including the canonical
  reconciliation snapshot, Vault, Emoji/Reaction, Private Message, AED and
  Appearance migrations.
- Database access: `backend/db/client.py` (Supabase singleton, in-memory
  fallback, `settings_document_*`, AED store mode), `backend/services/panel_settings_repository.py`,
  `backend/services/settings_service.py`, `backend/ai/config_store.py`,
  `backend/ai/persistence.py`, `backend/ai/database/*` repositories,
  `backend/ai/test_snapshot.py`.
- Feature services: `backend/services/appearance_service.py`,
  `backend/bot/handlers/appearance.py`, `ghost_seen_v2.py`,
  `private_message_service.py`, `anti_edit_delete_service.py`,
  `backend/ai/task_scheduler.py`.
- Tests: `test_database_setup_order.py`, `test_canonical_schema_reconciliation.py`,
  `test_auto_reaction_schema.py`, `test_03_database_consistency.py`,
  `test_04_restart_persistence.py`, Appearance tests
  (`test_appearance_gallery.py`, `test_appearance_delivery.py`,
  `test_appearance_image_cache.py`, `test_appearance_ui.py`).

## 3. Tables and persistence categories verified

**27 tables are created by the migrations** (verified by parsing every
`CREATE TABLE IF NOT EXISTS` statement):

- Core/config (6): `saved_items`, `bio_state`, `username_state`, `bot_logs`,
  `panel_settings`, `bot_settings`.
- AI/automation (10): `ai_config`, `ai_sessions`, `ai_messages`, `ai_memories`,
  `ai_tool_history`, `ai_usage`, `ai_provider_stats`, `ai_tasks`,
  `ai_task_occurrences`, `todo_steps`.
- Feature (11): `ghost_chats`, `emoji_library`, `emoji_categories`,
  `emoji_mappings`, `emoji_state`, `emoji_chat_overrides`,
  `private_message_config`, `private_message_senders`, `aed_config`,
  `aed_archive_entries`, `api_credentials`.

`ai_preferences` is **not** among them: it exists only as an interface
(`backend/ai/database/preferences_repository.py`), with no migration and no
producer.

Persistence categories recorded in §25: persistent in the Supabase database
(the tables above); persistent in Supabase Storage (Appearance images);
persistent in Supabase Vault (credential secrets, metadata in
`api_credentials`); persistent in durable `bot_settings` JSON documents (Ghost
Seen allow-list, Test Modules snapshot, legacy AED/Private Message documents);
in-memory only (`ai_preferences`, reply-context LRU, health/diagnostics);
cache/delivery only (Appearance `_DELIVERED`, `_BUILT`, image cache, readiness
probe); transient UI state (Ghost Seen selections/inputs); manual-only SQL
(Emoji, Private Message, AED, Vault, Todo steps, Appearance columns);
legacy/unused (`ghost_chats`, the `bot_settings` shards); unimplemented
(`ai_preferences`).

## 4. Documentation inconsistencies discovered

1. **Overview table count was wrong.** §1 claimed 13 tables (5 core + 8 AI) plus
   `bot_settings`/`ghost_chats`; the migrations create 27. `ai_tasks`,
   `ai_task_occurrences`, `todo_steps`, the Emoji/Private Message/AED tables and
   `api_credentials` were absent, and the live `bot_settings` table was not
   listed as a core table.
2. **§6 accessor count stale.** It claimed “14 typed getters + 14 typed setters”
   and omitted the five Appearance accessors, while
   `settings_service._DEFAULTS` holds **19** keys. Only
   `update_stale_seconds` and `ghost_seen_retention_seconds` lack accessors.
3. **§6 Migration Status lacked Appearance.** No paragraph described
   `20261109000000_add_panel_settings_appearance.sql`, its five columns or its
   CHECKs.
4. **§22 Migration Status stale.** It said the authoritative classification
   covered **36** files (now 37) and had no Appearance row.
5. **§25 inventory stale.** It claimed “12 typed settings”, “4 applied columns”,
   “10 columns missing”, “tables not migrated”, and omitted Appearance, Emoji &
   Reaction, Private Message, Anti-Edit/Delete, the AI task scheduler, Todo
   steps, the Test Modules snapshot and the credential vault.
6. **§26 Hermes boundary stale.** It claimed “13 live/specified tables”, said
   usage/stats had “no applied migrations”, and still proposed ten
   `panel_settings` columns and the trigger columns, all of which now have
   migrations.
7. **§31.1.1 counts stale.** It stated “36 migration files; 14 embedded parts”
   and did not mention part 15.
8. **Chapter numbering duplicated and anchors broken.** Two chapters were
   numbered 20 and two 21; the TOC promised 22–28 but the body delivered
   22/21/23…; Ghost Seen preceded Migration Generation Rules while the TOC
   listed them in the opposite order. Renumbered to 22–28 and the TOC/anchors now
   match the body.
9. **`ai_sessions` PK misdocumented.** §1 and §8 called `session_id` the primary
   key; the migration creates `id bigserial PRIMARY KEY` with `session_id text
   NOT NULL UNIQUE` (already noted in §19.19). The status enum also omitted
   `completed`.
10. **Historical sections were not rewritten.** §19 keeps its labelled history
    and its `[MIGRATION CREATED]` resolutions; it is not presented as current
    state.

## 5. Corrections made

- §1 rewritten to 27 tables with correct groups, a `bot_settings` row, the task
  tables, and the explicit `ai_preferences` no-migration status.
- §6: 19 typed accessor pairs (Appearance included), a new Appearance migration
  paragraph (additive/idempotent, four CHECKs, part 15, Storage-key-only), and a
  corrected note that exactly two setting columns lack accessors.
- §22: 37-file count, a row 27 for the Appearance migration with its owner action
  and part-15 reference.
- §25: full restart-survival inventory of the current feature set, including
  Appearance (columns, gallery, selected key, delivery cache), Auto-Reaction,
  Emoji, Private Message, AED, test snapshot, task scheduler/Todo steps and the
  credential vault, with manual-only migrations clearly labelled.
- §26: 27-table summary; usage/stats migrations acknowledged; proposals reduced
  to what is genuinely undelivered (`ai_preferences`, schema verification).
- §31.1.1: 37 files / 15 parts / part 15 described.
- §8 and §1: `ai_sessions` PK/index/status corrected.
- New §35 “Appearance Persistence” chapter (see §6 below) and TOC entry.
- Chapter renumbering 20→22, 21→24, 22→23 and downstream 25–28, with every
  cross-reference and TOC anchor updated.

## 6. Appearance audit findings

- **Five columns on the existing singleton row** (`panel_settings.key='global'`):
  `appearance_header_title text NOT NULL DEFAULT ''`,
  `appearance_header_body text NOT NULL DEFAULT ''`,
  `appearance_page_overrides jsonb NOT NULL DEFAULT '{}'`,
  `appearance_preview_enabled boolean NOT NULL DEFAULT false`,
  `appearance_preview_image_key text NOT NULL DEFAULT ''`.
- **Constraints:** length ≤1024 and no NUL on both text columns;
  `jsonb_typeof(...) = 'object'` on the overrides; empty or a bounded Storage
  path on the image key. The service mirrors these in
  `_validate_appearance_text`, `_validate_page_overrides` and
  `_validate_storage_key`.
- **Ownership:** handler → `appearance_service` → `settings_service` (typed
  accessors, cache, validators) → `panel_settings_repository` (only module that
  touches the table). `''`/`'{}'` mean “built-in default”.
- **Gallery:** image objects in the dedicated private bucket named by
  `APPEARANCE_MEDIA_BUCKET` (empty ⇒ storage unavailable; also requires
  `SUPABASE_URL` + `SUPABASE_SERVICE_ROLE_KEY`), prefix
  `appearance/preview/`, key `appearance/preview/<32 hex>.<png|jpg|webp>`. The
  gallery is the Storage **listing** (`list()`); **no gallery table exists**.
- **Database stores only the selected object key** — never bytes, signed URLs,
  Telegram file IDs or filenames.
- **Bounds:** 10 images max, 25 MiB (`26_214_400` bytes), 8192 px per side,
  50 MP, PNG/JPEG/WebP by decoded format; upload validates, writes a new object,
  re-lists to confirm it, then persists the key, deleting the object if the key
  write fails. Delete of the selected image clears the key first, then deletes
  that object. Clear-selection deletes nothing; reset writes `''`/drops the
  override.
- **Restart boundary:** columns + Storage objects are durable; `_DELIVERED`,
  `_BUILT`, the bounded image cache and the ≤30 s readiness probe are
  process-local and rebuilt.
- **RLS unchanged:** `panel_settings` keeps RLS with SELECT-only grants for
  `anon`/`authenticated`; the migration adds no policy or grant; writes use the
  service-role client; the bucket is private.

## 7. Automated tests added or updated

- **Added `tests/test_database_architecture_doc.py`** (12 tests):
  every migration classified in §31 and the count matches; §22 states 37 files;
  Appearance is part 15 with all five `ADD COLUMN` statements; the §1 table count
  (27) equals the migrations and names every table; §26 uses 27 and keeps its
  “source not inspected” scope; §6 documents all five Appearance columns with
  their real type/nullability/default; §6 accessor count equals
  `settings_service._DEFAULTS` (19); the migration's CHECKs match the documented
  ones; §35 matches the source constants (bucket env, prefix, 10 images, 25 MiB,
  8192 px, 50 MP, key-only, no gallery table, SELECT-only RLS, process-local
  cache, rollback); §25 names the current features; current-state sections do not
  revive the stale “missing columns/tables” claims.
- **Updated `tests/test_database_setup_order.py`**: the Migration Status section
  anchor moved to §22 and the pinned migration count changed 36 → 37.
- **Updated `tests/test_canonical_schema_reconciliation.py`**: the §30.10 cleanup
  reference now points at §22.
- Existing statement-identity and schema-equivalence tests were left in place;
  they already pin parts 1–15 against the migration files.

## 8. Test and validation results

- Focused: `tests/test_database_architecture_doc.py`,
  `test_database_setup_order.py`, `test_canonical_schema_reconciliation.py`,
  `test_auto_reaction_schema.py` → **97 passed**; after the final `ai_sessions`
  documentation correction the three-document suite → **90 passed**.
- Full suite: **7,373 passed, 27 skipped, 1 failed** in 144.51 s. The failure is
  the pre-existing, unrelated
  `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
  (`assert 0 == 1`, “interval advancement exceeded bound”), unchanged by this
  documentation task.
- `py_compile` on the added test module: OK. `git diff --check`: clean.
- Source-to-document checks: all 37 migration filenames’ timestamps appear in
  §31; the 27 table names appear in §1; appearance columns/constraints match the
  migration SQL; the documented accessor count matches `_DEFAULTS`; no stale
  “14 typed”, “13 tables”, “36 files” or “tables not migrated” claims remain.

## 9. Known gaps that remain

- Manual-only migrations: Emoji (part 9), Private Message (10/11/13), AED (12),
  Vault (2/3), Todo steps (8) and Appearance (15) are **not applied** to any live
  project by this task.
- `ai_messages.tool_calls` has no column and `_add_message_sync` does not write
  it, so tool-call metadata on messages is never persisted (§19.5).
- `ai_preferences` has no migration and no producer (§19.17).
- `ghost_chats` has no reader/writer (§19.12).
- `update_stale_seconds` and `ghost_seen_retention_seconds` have no accessor or
  consumer (§19.13/§19.14).
- `ai_sessions` PK remains `id`, with `session_id` UNIQUE (§19.19).
- Emoji per-chat overrides are implemented but not owner-approved (§32.6).
- Legacy `bot_settings` documents remain live for Ghost Seen, AED fallback,
  Private Message import and the Test Modules snapshot.

## 10. Items deliberately not changed

- No SQL was executed; no Supabase project was touched; no migration was created
  or edited.
- No schema, RLS, grant or Storage configuration change was made.
- No repository, service or handler was refactored.
- The implementation gaps above were documented as findings, not “fixed”.
- §19’s labelled historical record and `[MIGRATION CREATED]` resolutions were
  kept; the canonical reconciliation snapshot was not regenerated (later
  migrations remain additive parts 2–15).
- `ROADMAP.md` and this report were replaced, not appended to.

## 11. Could not be verified from this repository

- Live Supabase state: whether any migration is applied, the actual constraints
  in the hosted project, the Storage bucket’s existence/privacy, and the live RLS
  posture.
- Hermes source/schema (not present in this repository), so §26 remains a
  boundary description, not an inspection of Hermes.
- Behaviour of a real PostgreSQL planner/Storage backend; the project’s tests are
  offline unit/simulation tests.
- A literal line-by-line read of every one of the 8,305 document lines was not
  performed: the changed sections were re-read in full and the whole document was
  re-scanned systematically (headings, TOC anchors, table names, migration
  timestamps and stale-claim searches).
- A full column-by-column re-verification of all 27 tables was not exhaustive;
  verification concentrated on the corrected tables, the Appearance columns,
  `ai_sessions`, and every claim the audit changed.
