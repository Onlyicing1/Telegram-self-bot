# Database Architecture — LifeOS Telegram Self-Bot

> **Canonical specification:** This document defines the final PostgreSQL/Supabase contract for the current application. Historical notes below are retained only where needed to explain compatibility; the canonical SQL block and final table contracts are authoritative.


> **Canonical database specification.**
> This document is the single source of truth for every table, column,
> index, constraint, and RLS policy in the Supabase database. Future
> migrations MUST be generated from this document — no schema change is
> valid unless it is reflected here first.
>
> Every column listed here is read or written by a Python module in the
> repository. Columns that exist in migrations but are never touched by
> code are listed in a dedicated "Dead Columns" subsection and marked for
> removal in a future migration.

---

## Table of Contents

1. [Overview](#1-overview)
2. [saved_items](#2-saved_items)
3. [bio_state](#3-bio_state)
4. [username_state](#4-username_state)
5. [bot_logs](#5-bot_logs)
6. [panel_settings](#6-panel_settings)
7. [ai_config](#7-ai_config)
8. [ai_sessions](#8-ai_sessions)
9. [ai_messages](#9-ai_messages)
10. [ai_memories](#10-ai_memories)
11. [ai_tool_history](#11-ai_tool_history)
12. [ai_provider_stats](#12-ai_provider_stats)
13. [ai_usage](#13-ai_usage)
14. [ai_preferences](#14-ai_preferences)
15. [ai_tasks](#15-ai_tasks)
16. [ai_task_occurrences](#16-ai_task_occurrences)
17. [Relationships](#17-relationships)
18. [RLS Policy Model](#18-rls-policy-model)
19. [Panel Database](#19-panel-database)
20. [In-Memory Fallback](#20-in-memory-fallback)
21. [Known Inconsistencies](#21-known-inconsistencies)
22. [Migration Status](#22-migration-status)
23. [Migration Generation Rules](#23-migration-generation-rules)
24. [Ghost Seen / Ghost PV](#24-ghost-seen--ghost-pv)
25. [Self Bot Persistent State Inventory](#25-self-bot-persistent-state-inventory)
26. [Hermes Integration Boundary & Corrected Architecture](#26-hermes-integration-boundary--corrected-architecture)
27. [Font System Persistence](#27-font-system-persistence)
28. [Current vs Proposed Status Matrix](#28-current-vs-proposed-status-matrix)
29. [API Credential Vault (PART 1)](#29-api-credential-vault-part-1)
30. [Canonical Schema Reconciliation & Drift Repair](#30-canonical-schema-reconciliation--drift-repair)

---

## 1. Overview

The database contains **13 tables** in the `public` schema — 5 core
LifeOS tables and 8 AI subsystem tables. Two additional tables exist in
migrations (`bot_settings`, `ghost_chats`). `ai_usage` and
`ai_provider_stats` have migration files (`20260827000003` /
`20260827000004`) pending manual application; `ai_preferences` is
specified here and implemented in code but has **no applied migration**.
The exact
status of every table is in [§19](#19-known-inconsistencies),
[§20](#20-migration-status), and [§23](#23-self-bot-persistent-state-inventory).

### Core Tables

| Table | Purpose | PK | Used by |
|---|---|---|---|
| `saved_items` | Media save records (forward + deep) | `id` (bigserial) | `db/client.py`, `save_service`, `retrieve_service`, `delete_service`, `discover_service`, `database_service`, `organize_service`, `web/app` |
| `bio_state` | Bio cron engine state per owner | `id` (bigserial) | `db/client.py`, `bio_service`, `bio/engine`, `organize_service`, `web/app` |
| `username_state` | Username cron engine state per owner | `id` (bigserial) | `db/client.py`, `username_service`, `username/engine` |
| `bot_logs` | Structured activity log | `id` (bigserial) | `db/client.py` (via `log()`), nearly every service, `web/app` |
| `panel_settings` | Glass Panel configuration (column-per-setting) | `key` (text) | `panel_settings_repository`, `settings_service`, `web/app` |

### AI Tables

| Table | Purpose | PK | Used by |
|---|---|---|---|
| `ai_config` | Per-owner AI configuration (provider, model, triggers, settings) | `id` (bigserial) | `ai/config_store.py`, `bot/handlers/ai_trigger.py`, `bot/handlers/ai.py`, `web/app` |
| `ai_sessions` | AI conversation session metadata | `session_id` (text) | `ai/persistence.py`, `ai/database/session_repository.py` |
| `ai_messages` | Individual AI messages within a session | `id` (bigserial) | `ai/persistence.py`, `ai/database/message_repository.py` |
| `ai_memories` | Three-tier memory (short, long, permanent) | `id` (bigserial) | `ai/persistence.py`, `ai/database/memory_repository.py` |
| `ai_tool_history` | Log of every tool call the AI made | `id` (bigserial) | `ai/persistence.py`, `ai/database/tool_history_repository.py` |
| `ai_provider_stats` | Per-provider aggregate statistics | `(provider_name, owner_id)` | `ai/database/provider_stats_repository.py` |
| `ai_usage` | Per-request token usage log | `id` (bigserial) | `ai/database/usage_repository.py` |
| `ai_preferences` | Per-owner AI personality and behavior preferences | `owner_id` (bigint) | `ai/database/preferences_repository.py` |

### Additional Tables in Migrations / Specified But Unmigrated

| Table | Migration | Code usage | Status |
|---|---|---|---|
| `bot_settings` | `20260729213959` | `ghost_seen_v2` (key `ghost_seen_allowed_chats`) | **Live** — NOT orphaned. See §19.4. |
| `ghost_chats` | `20260822090000` | none | **Orphaned** — no code reads or writes it. See §19.12. |
| `ai_usage` | `20260827000003` | `SupabaseUsageRepository` + `usage_recorder` | Migration created, **pending manual application** (§13). |
| `ai_provider_stats` | `20260827000004` | `SupabaseProviderStatsRepository` + `usage_recorder` | Migration created, **pending manual application** (§12). |
| `ai_preferences` | none | interface + in-memory only | Specified (§14), **no producer, no migration**. |

### Access Model

All access goes through the Supabase PostgREST API via the `supabase-py`
client. The backend uses the **service-role key**, which bypasses RLS.
The frontend reads via the backend API — it never touches Supabase
directly.

---

## 2. saved_items

Stores metadata for every media save operation. Deep Save is the ONLY
save method in the current code (`save_service.py::execute_save` always
writes `save_type='deep'`); the `'forward'` value exists only for the
column CHECK constraint and legacy rows. `forward_messages` exists
exclusively inside retrieval (`retrieve_service.do_retrieve`), never in
the Save path (see AGENTS.md §6).

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `save_code` | `text` | NO | — | Compact code, format `S` + 4 chars (e.g. `S0001`; random `SXXXX` on collision), generated by `db/client.py::get_next_save_code`. Unique. The legacy `SV-NNNNNN` format was retired by the short-code redesign; historical rows keep their original codes. |
| `save_type` | `text` | NO | — | `'forward'` or `'deep'`. CHECK constraint enforced. |
| `origin_chat_id` | `bigint` | YES | — | Telegram chat ID where the message originated |
| `origin_msg_id` | `bigint` | YES | — | Telegram message ID of the original message |
| `saved_chat_id` | `bigint` | YES | — | Telegram chat ID where the message was saved |
| `saved_msg_id` | `bigint` | YES | — | Telegram message ID of the saved message |
| `sender_name` | `text` | YES | — | Display name of the original sender |
| `sender_id` | `bigint` | YES | — | Telegram user ID of the original sender |
| `mime_type` | `text` | YES | — | MIME type of the media (e.g. `image/jpeg`) |
| `file_id` | `text` | YES | — | Telegram file ID |
| `file_size` | `bigint` | YES | — | File size in bytes |
| `media_type` | `text` | YES | — | Human-readable media type label (Photo, Video, etc.) |
| `display_name` | `text` | YES | — | **Owner-supplied name** for the item (e.g. `University Weekly Schedule — Semester Two`). Added by `20260921000001_add_saved_items_display_name.sql` — pending manual application. `NULL` = the owner gave no name: display falls back to the source filename and then to `media_type`. Never auto-filled and never LLM-generated; independent of `save_code`, `caption` and `file_name` (see *User-facing metadata* below). Searched by the saved-item resolver (`retrieve_service.resolve_saved_items`). |
| `tags` | `text[]` | YES | `'{}'` | **Owner-supplied semantic tags** (e.g. `{university, semester-2}`). Since Save V2 the column holds only what the owner provided — a save with no tags stores `'{}'` — while the machine-generated hashtags (`#saved`, `#saved_photo`, …) are rendered into `caption` instead. Historical rows keep the hashtags they were saved with; nothing is backfilled or rewritten, and a `#`-prefixed entry is not an owner tag (see *User-facing metadata* below). Searched by the saved-item resolver as whole-tag equality. |
| `caption` | `text` | YES | — | Caption attached to the saved message |

### User-facing metadata — Save V2 part 1 (owner-supplied name and tags)

Save V2 separates four concepts that were previously conflated:

| Concept | Where it lives | Mutability |
|---|---|---|
| Internal identity | `save_code` (`S` + 4 chars, UNIQUE) | **immutable** — no code path updates it, and naming/renaming never touches it |
| The SOURCE file's own name | `file_name` (Telegram's filename, or the generated `photo_<code>.jpg` / `<code>.bin`) | technical metadata; still not written by `execute_save` |
| The owner's name for the item | `display_name` | owner-editable; never invented |
| The owner's semantic tags | `tags` | owner-editable; never invented |

- **One representation, one writer.** `backend/services/save_service.py::SaveMetadata` (`display_name`, `tags`) is the single metadata object, and `execute_save(..., metadata=…)` is the only writer of both columns. The Glass Save panel and the AI `SaveTool` are thin adapters over that same pipeline — no second metadata type, no second store, no second writer.
- **`NULL`/`'{}'` are meanings, not gaps.** No name is stored as `NULL` (never `''`), no tags as `'{}'`. Nothing is inferred from the caption, the filename or the media type, and nothing is generated by a model.
- **Normalization is deterministic and shared** (`normalize_display_name`, `normalize_tags`): trim plus collapse of internal whitespace; empty/whitespace-only input means “no value”; tags are deduped case-insensitively (the owner's spelling is preserved) with a bounded count (`10`) and per-tag length (`40`); a name is bounded at `120` characters. Anything over a bound is **refused, never truncated** — invalid metadata cancels the save before any download or upload. Matching-time folding of Arabic/Persian letter variants is deliberately NOT applied to storage; it belongs to the retrieval phase.
- **Deploy order.** `execute_save` omits the `display_name` key entirely when the owner gave no name, so every metadata-less save keeps working against a database that has not applied the migration yet (PostgREST rejects an insert naming an unknown column). A save *with* a name requires the column: without it the insert fails honestly and the name is never silently lost. Apply `20260921000001_add_saved_items_display_name.sql` before any surface starts offering names.
- **Indexing is deliberately deferred.** No query reads `display_name` or `tags` yet, so this phase adds no index. When the retrieval phase starts matching on them the only indexes worth adding are `(owner_id, display_name)` btree and a GIN index on `tags`.
- **Retrieval, search, ambiguity handling and the management UI are NOT part of this phase.** `save_code` remains the only handle any current query uses.
| `owner_id` | `bigint` | NO | `0` | Telegram user ID of the bot owner |
| `created_at` | `timestamptz` | NO | `now()` | When the save was created |

### Dead Columns (exist in migrations, never used by code)

| Column | SQL Type | Added by | Notes |
|---|---|---|---|
| `short_code` | `text` | `20260718143752` | Never read or written by any Python module. Orphan trigram indexes depend on it. Must be dropped in a future cleanup migration along with its dependent indexes. |
| `file_name` | `text` | `20260718143752` | Never included in the insert payload built by `save_service.py`. Orphan trigram indexes depend on it. Must be dropped in a future cleanup migration. |

### Indexes

| Index | Columns | Type | Notes |
|---|---|---|---|
| `saved_items_pkey` | `id` | btree (PK) | |
| `saved_items_save_code_key` | `save_code` | btree (UNIQUE) | |
| `idx_saved_items_owner` | `owner_id` | btree | |
| `idx_saved_items_created_at` | `created_at` | btree | |
| `idx_saved_items_save_type` | `save_type` | btree | |
| `idx_saved_items_owner_created` | `(owner_id, created_at)` | btree | Added by migration `20260718143752`. Composite index for `list_saves` + `list_recent_saves` queries, and the owner filter + `created_at DESC` ordering of the Save V2 resolver. |
| `idx_saved_items_display_name_trgm` | `display_name` | GIN (trigram) | Save V2 — supports the resolver's owner-scoped `display_name ILIKE '%token%'` prefilter. Added by `20260922000001_add_saved_items_search_indexes.sql` — pending manual application. Requires `pg_trgm` (already enabled by the canonical script). |
| `idx_saved_items_tags` | `tags` | GIN | Save V2 — supports the resolver's whole-tag containment (`tags.cs.{term}`). Added by the same pending successor migration. |

### Dead Indexes (depend on dead columns, should be dropped)

| Index | Columns | Type | Notes |
|---|---|---|---|
| `idx_saved_items_caption_trgm` | `caption` | GIN (trigram) | Depends on `pg_trgm` extension. Only useful if full-text search on `caption` is implemented — it is not. |
| `idx_saved_items_file_name_trgm` | `file_name` | GIN (trigram) | Depends on dead `file_name` column. |
| `idx_saved_items_save_code_trgm` | `save_code` | GIN (trigram) | Redundant — `save_code` already has a unique btree index. |
| `idx_saved_items_short_code_trgm` | `short_code` | GIN (trigram) | Depends on dead `short_code` column. |
| `idx_saved_items_mime_trgm` | `mime_type` | GIN (trigram) | No search query uses trigram on `mime_type`. (Documented as `idx_saved_items_mime_type_trgm` before the drift repair; the SQL name is authoritative.) |

### CHECK Constraints

- `saved_items_save_type_check`: `save_type IN ('forward', 'deep')`

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.
All writes go through the backend service-role key.

### Repository

**`backend/db/client.py`** — all CRUD via `insert_save`, `query_save`,
`list_saves`, `list_recent_saves`, `search_saves`, `delete_save_row`,
`update_save_field`, `count_saves`, `list_all_saves`, `cleanup_orphans`,
`get_stats`, `get_next_save_code`.

**`backend/services/save_service.py`** — builds the insert payload with
all live columns (including `display_name` and the owner's `tags`, both carried
by `SaveMetadata`), calls `insert_save`. It is the only writer of those two
columns.

---

## 3. bio_state

Singleton bio engine state per owner. One row per owner (enforced by
UNIQUE constraint on `owner_id`).

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | `0` | Telegram user ID of the bot owner. UNIQUE. |
| `template` | `text` | NO | `'🕒 {time} \| 💭 {mood}'` | Bio template with `{time}`, `{mood}`, `{text}` tokens |
| `mood` | `text` | NO | `'😊'` | Current mood value |
| `custom_text` | `text` | NO | `''` | Custom text for `{text}` token |
| `is_active` | `boolean` | NO | `false` | Whether the bio cron is running |
| `last_bio` | `text` | YES | — | Last successfully rendered bio string |
| `updated_at` | `timestamptz` | NO | `now()` | Last update timestamp |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `bio_state_pkey` | `id` | btree (PK) |
| `bio_state_owner_id_key` | `owner_id` | btree (UNIQUE) |

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.

### Repository

**`backend/db/client.py`** — `get_bio_state`, `get_or_create_bio_state`,
`update_bio_state`.

**Defaults from code** (`get_or_create_bio_state`):
template `'🕒 {time} | 💭 {mood}'`, mood `'😊'`, custom_text `''`,
is_active `false`, last_bio `''`.

---

## 4. username_state

Singleton username engine state per owner. Mirrors `bio_state` in
structure but controls the Telegram `first_name` field instead of the
`about` field. Completely independent from the Bio Engine.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | `0` | Telegram user ID of the bot owner. UNIQUE. |
| `template` | `text` | NO | `'{time} \| {mood}'` | Username template with `{time}`, `{mood}`, `{text}` tokens |
| `mood` | `text` | NO | `'😊'` | Current mood value |
| `custom_text` | `text` | NO | `''` | Custom text for `{text}` token |
| `is_active` | `boolean` | NO | `false` | Whether the username cron is running |
| `last_name` | `text` | NO | `''` | Last successfully rendered username string |
| `updated_at` | `timestamptz` | NO | `now()` | Last update timestamp |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `username_state_pkey` | `id` | btree (PK) |
| `username_state_owner_id_key` | `owner_id` | btree (UNIQUE) |
| `idx_username_state_owner` | `owner_id` | btree |

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.

### Repository

**`backend/db/client.py`** — `get_username_state`,
`get_or_create_username_state`, `update_username_state`.

**Defaults from code** (`get_or_create_username_state`):
template `'{time} | {mood}'`, mood `'😊'`, custom_text `''`,
is_active `false`, last_name `''`.

---

## 5. bot_logs

Structured activity log.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | `0` | Telegram user ID of the bot owner |
| `level` | `text` | NO | `'INFO'` | Log level: `INFO`, `WARN`, `ERROR` |
| `message` | `text` | NO | — | Log message |
| `context` | `jsonb` | YES | — | Structured context (JSON) |
| `created_at` | `timestamptz` | NO | `now()` | When the log entry was created |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `bot_logs_pkey` | `id` | btree (PK) |
| `idx_bot_logs_owner` | `owner_id` | btree |
| `idx_bot_logs_created_at` | `created_at` | btree |

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.

### Repository

**`backend/db/client.py`** — `log`, `count_logs`, `list_logs`,
`clean_logs`.

---

## 6. panel_settings

Glass Panel configuration. Singleton row (key = `"global"`). Uses a
**column-per-setting** model — each setting is a real typed column, not
a key-value store.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `key` | `text` | NO | — | Primary key, always `"global"` |
| `auto_close_enabled` | `boolean` | NO | `true` | Whether panels auto-close |
| `auto_close_delay` | `integer` | NO | `120` | Auto-close delay in seconds |
| `max_deep_save_mb` | `integer` | NO | `50` | Max file size for deep save (MB) |
| `delete_batch_size` | `integer` | NO | `100` | Batch size for message deletion |
| `log_retention_days` | `integer` | NO | `7` | Days to retain logs |
| `panel_timeout_seconds` | `integer` | NO | `300` | Panel timeout in seconds |
| `allow_multiple_panels` | `boolean` | NO | `false` | Allow multiple simultaneous panels |
| `reuse_existing_panel` | `boolean` | NO | `true` | Reuse an existing panel instead of creating new |
| `language` | `text` | NO | `'en'` | Language code |
| `debug_callbacks` | `boolean` | NO | `false` | Debug callback tracing |
| `owner_only` | `boolean` | NO | `true` | Restrict commands to owner only |
| `dashboard_font` | `text` | NO | `'default'` | Glass UI / dashboard font key — one of `FONT_KEYS` in `backend/helper/font_style.py` (`DASHBOARD_FONTS` == `FONT_KEYS`); never free-text CSS |
| `ghost_seen_retention_seconds` | `bigint` | NO | `2592000` | Ghost Seen registry retention window in seconds (30 min–365 days); presets include sub-day durations |
| `update_stale_seconds` | `integer` | NO | `300` | Update staleness threshold (seconds) |
| `updated_at` | `timestamptz` | YES | `now()` | Last update timestamp |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `panel_settings_pkey` | `key` | btree (PK) |

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.

### Repository

**`backend/services/panel_settings_repository.py`** — `load`,
`update_field`, `update_fields`, `reload`.

**`backend/services/settings_service.py`** — cache-first reads,
write-through cache, **12 typed getters + 12 typed setters**
(`auto_close_enabled`, `auto_close_delay`, `max_deep_save_mb`,
`delete_batch_size`, `log_retention_days`, `panel_timeout_seconds`,
`allow_multiple_panels`, `reuse_existing_panel`, `language`,
`debug_callbacks`, `owner_only`, `dashboard_font`), per-setting
validators. See [§17 Panel Database](#17-panel-database) for details.

> **Note:** `ghost_seen_retention_seconds` and `update_stale_seconds`
> are migrated columns with **no accessor in `settings_service` and no
> production consumer** — configured-but-unconsumed until a consumer is
> implemented (see §19.13, §19.14).

### Migration Status

The initial migration (`20260726143924`) created only 3 columns
(`key`, `auto_close_enabled`, `updated_at`). A later migration
(`20260730210551`) added `update_stale_seconds`. The remaining 10
columns were supposed to be added by migrations
`20260730220000_panel_settings_column_model.sql` and
`20260730230000_panel_settings_full_13_columns.sql`, but **these
migration files do not exist** in the repository. Migration
`20260827000001_add_missing_panel_settings_columns.sql` now adds all
10 missing columns (idempotent, with CHECK constraints mirroring the
`settings_service` validators) — pending manual application. Migration
`20260823120000_add_dashboard_font_and_ghost_seen_settings.sql`
(idempotent) covers `dashboard_font` and (originally)
`ghost_seen_retention_days`; it was applied to the live database and
verified by the project owner. The follow-up migration
`20260823130000_ghost_seen_retention_duration.sql` (idempotent)
replaces the days column with `ghost_seen_retention_seconds`
(backfilling `days × 86400`) to support sub-day retention windows; it
is pending manual application. See [§20](#20-migration-status) and
[§19](#19-known-inconsistencies).

### Removed / phantom columns

The following columns were referenced in older documentation or the
`bot_settings` transition table but are **not** in the canonical schema:

- ~~`panel_auto_close_seconds`~~ — superseded by `auto_close_delay`
- ~~`log_cleanup_days`~~ — superseded by `log_retention_days`
- ~~`diagnostics_enabled`~~ — never implemented in `settings_service.py`
- ~~`bot_settings` table~~ — this earlier removal proposal is superseded.
  The current Ghost Seen implementation actively requires `bot_settings`
  for the `ghost_seen_allowed_chats` durable allow-list; do not drop it
  until a replacement persistence design and data migration are implemented.

---

## 7. ai_config

Per-owner AI configuration. **One row per owner** — enforced by a
UNIQUE constraint on `owner_id`. This is the canonical storage for
provider selection, model selection, trigger words, and tuning
parameters.

> **Current status:** The `ai_config` table is referenced by
> `backend/ai/config_store.py` but may not exist in the live database.
> The initial migration (`20260805075707`) creates the base columns but
> does NOT include `trigger_en`, `trigger_fa`, `show_question` or the
> three Gemini STT settings (`stt_model`, `stt_language`,
> `stt_passes`). Migrations
> `20260827000002_add_ai_config_trigger_columns.sql`,
> `20260913000000_add_ai_config_show_question.sql` and
> `20260917000001_add_ai_config_stt_settings.sql` (all idempotent) add
> them — pending manual application. Until they are applied, the
> runtime silently falls back to in-memory storage when the table or
> columns are missing. See
> [§19 Known Inconsistencies](#19-known-inconsistencies).

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | `0` | Telegram user ID of the bot owner. UNIQUE. One config row per owner. |
| `provider` | `text` | NO | `''` | Active provider name (e.g. `gemini`, `openai`, `openrouter`, `groq`, `mistral`, `cerebras`, `dummy`) |
| `model` | `text` | NO | `''` | Active model name |
| `temperature` | `double precision` | NO | `1.0` | Sampling temperature [0.0, 2.0] |
| `max_tokens` | `integer` | NO | `4096` | Max output tokens |
| `system_prompt` | `text` | NO | `''` | Custom system prompt (empty = default) |
| `history_budget` | `integer` | NO | `4000` | Context budget in tokens |
| `is_configured` | `boolean` | NO | `false` | Whether the user completed setup |
| `trigger_en` | `text` | YES | `NULL` | English trigger word (case-insensitive matching). NULL = not set. |
| `trigger_fa` | `text` | YES | `NULL` | Persian trigger word (exact matching). NULL = not set. |
| `show_question` | `boolean` | NO | `false` | Presentation-only "Show my message in AI replies" preference. Read/written by `config_store` (`_DEFAULTS` merge + upsert payload); toggled from AI → Settings. Consumed only by the Telegram reply renderer. Added by `20260913000000_add_ai_config_show_question.sql` — pending manual application. |
| `stt_model` | `text` | YES | `NULL` | Dedicated Gemini transcription model. Read/written by `config_store` (`_DEFAULTS` merge + upsert payload), edited from AI → Settings → Advanced, consumed at runtime by `services/gemini_media_engine.py` (`stt_settings_from` / `apply_stt_settings`). NULL = the general media model answers the STT instruction. Added by `20260917000001_add_ai_config_stt_settings.sql` — pending manual application. |
| `stt_language` | `text` | YES | `NULL` | BCP-47 language hint for transcription (e.g. `fa-IR`). Same read/write/consumer path as `stt_model`. NULL = automatic language detection. Added by `20260917000001_add_ai_config_stt_settings.sql` — pending manual application. |
| `stt_passes` | `integer` | NO | `1` | Bounded number of recognition passes (CHECK `BETWEEN 1 AND 3`; 1 = the single-pass route). Same read/write/consumer path as `stt_model`. Added by `20260917000001_add_ai_config_stt_settings.sql` — pending manual application. |
| `last_request_at` | `timestamptz` | YES | `NULL` | Timestamp of the last AI request. **Currently never persisted** — see [§19](#19-known-inconsistencies). |
| `last_latency_ms` | `real` | YES | `NULL` | Latency of the last AI request in ms. **Currently never persisted** — see [§19](#19-known-inconsistencies). |
| `created_at` | `timestamptz` | YES | `now()` | When the config row was created |
| `updated_at` | `timestamptz` | YES | `now()` | Last update timestamp |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `ai_config_pkey` | `id` | btree (PK) |
| `ai_config_owner_id_key` | `owner_id` | btree (UNIQUE) |

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.
All writes go through the backend service-role key.

### Trigger Validation Rules (enforced in application code)

- Both `trigger_en` and `trigger_fa` are optional individually (NULL or
  empty string).
- At least one must be non-empty before AI can be activated.
- The two values must not be identical (case-insensitive comparison).
- Triggers must be single words (no spaces).
- `trigger_en` matching is case-insensitive.
- `trigger_fa` matching is exact (no case folding).
- When a trigger matches, the trigger word is stripped from the message
  before being sent to the provider.

### Repository

**`backend/ai/config_store.py`** — `get_config`, `save_config`,
`update_provider`, `update_model`, `update_setting`, `record_request`,
`is_configured`, `validate_triggers`, `update_triggers`,
`get_triggers`, `match_trigger`.

**In-memory fallback:** `_fallback_config` dict keyed by `owner_id`.
Used when the DB is unavailable. All reads return fallback data; all
writes update the fallback dict. The fallback is also updated on
successful DB writes so the in-memory state stays consistent.

### Defaults from code (`_DEFAULT_CONFIG`)

```
provider: "", model: "", temperature: 1.0, max_tokens: 4096,
system_prompt: "", history_budget: 4000, is_configured: False,
trigger_en: "", trigger_fa: ""
```

---

## 8. ai_sessions

AI conversation session metadata. One row per active AI conversation.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `session_id` | `text` | NO | — | Primary key. Format: `owner-{owner_id}` |
| `owner_id` | `bigint` | NO | — | Telegram user ID of the bot owner |
| `provider` | `text` | YES | — | Active provider name |
| `model` | `text` | YES | — | Active model name |
| `status` | `text` | NO | `'active'` | Session status: `active`, `closed`, `error` |
| `total_tokens` | `integer` | YES | `0` | Cumulative token count |
| `message_count` | `integer` | YES | `0` | Number of messages in session |
| `created_at` | `timestamptz` | YES | `now()` | When the session was created |
| `updated_at` | `timestamptz` | YES | `now()` | Last update timestamp |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `ai_sessions_pkey` | `session_id` | btree (PK) |
| `idx_ai_sessions_owner` | `owner_id` | btree |

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.
All writes go through the backend service-role key.

### Repository

**`backend/ai/persistence.py`** — `create_session`, `update_session`,
`get_session`. Only inserts `session_id` and `owner_id` on creation;
other fields are updated via `update_session`.

**`backend/ai/database/session_repository.py`** — interface +
in-memory fallback (`SessionRecord`, `SessionRepository`,
`InMemorySessionRepository`). `SessionRecord` fields: `session_id`,
`owner_id`, `provider`, `model`, `status`, `total_tokens`,
`message_count`. No Supabase-backed implementation is wired here yet.

---

## 9. ai_messages

Individual AI messages within a conversation session.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `session_id` | `text` | NO | — | FK → `ai_sessions.session_id` (logical, not enforced) |
| `owner_id` | `bigint` | NO | — | Telegram user ID of the bot owner |
| `role` | `text` | NO | — | `user`, `assistant`, or `system` |
| `content` | `text` | NO | — | Message content (truncated to 8000 chars on write) |
| `token_count` | `integer` | YES | `0` | Estimated token count |
| `tool_calls` | `jsonb` | YES | `'[]'` | Tool calls made in this message (JSON array). **Defined in `MessageRecord` but not written by `persistence.py` and not in the applied migration.** See [§19](#19-known-inconsistencies). |
| `provider` | `text` | YES | — | Provider that generated this message |
| `model` | `text` | YES | — | Model that generated this message |
| `created_at` | `timestamptz` | YES | `now()` | When the message was created |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `ai_messages_pkey` | `id` | btree (PK) |
| `idx_ai_messages_session` | `session_id` | btree |
| `idx_ai_messages_owner` | `owner_id` | btree |

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.
All writes go through the backend service-role key.

### Repository

**`backend/ai/persistence.py`** — `add_message`, `get_messages`.
Inserts: `session_id`, `owner_id`, `role`, `content` (truncated),
`token_count`, `provider`, `model`. Does NOT insert `tool_calls`.

**`backend/ai/database/message_repository.py`** — interface +
in-memory fallback (`MessageRecord`, `MessageRepository`,
`InMemoryMessageRepository`). `MessageRecord` fields include
`tool_calls` and `metadata` — neither is written by `persistence.py`
nor created by the applied migration. See [§19](#19-known-inconsistencies).

---

## 10. ai_memories

Three-tier memory system: short, long, and permanent memories.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | — | Telegram user ID of the bot owner |
| `tier` | `text` | NO | — | `short`, `long`, or `permanent` |
| `category` | `text` | NO | — | `fact`, `preference`, `context`, `summary`, `instruction` |
| `content` | `text` | NO | — | Memory text (truncated to 8000 chars on write) |
| `importance` | `real` | YES | `0.5` | Score 0.0–1.0 (higher = more relevant) |
| `expires_at` | `timestamptz` | YES | — | When the memory expires (NULL = never) |
| `metadata` | `jsonb` | YES | `'{}'` | Arbitrary extra metadata |
| `created_at` | `timestamptz` | YES | `now()` | When the memory was created |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `ai_memories_pkey` | `id` | btree (PK) |
| `idx_ai_memories_owner_tier` | `(owner_id, tier)` | btree |
| `idx_ai_memories_importance` | `importance` | btree |

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.
All writes go through the backend service-role key.

### Repository

**`backend/ai/persistence.py`** — `save_memory`, `query_memories`,
`delete_expired_memories`.

**`backend/ai/database/memory_repository.py`** — interface +
in-memory fallback (`MemoryRepository`, `InMemoryMemoryRepository`).
Filters by: `owner_id`, `tier`, `category`, `importance` (gte),
query text (substring), `expires_at`.

**`backend/ai/memory/`** — `MemoryManager`, `ShortMemory`,
`LongMemory`, `PermanentMemory`.

---

## 11. ai_tool_history

Log of every tool call the AI has made. Used for auditing and debugging.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | — | Telegram user ID of the bot owner |
| `session_id` | `text` | YES | — | AI session ID |
| `tool_name` | `text` | NO | — | Name of the tool called |
| `arguments` | `jsonb` | YES | — | Arguments passed to the tool |
| `result_success` | `boolean` | YES | `false` | Whether the tool succeeded |
| `result_message` | `text` | YES | — | Result message (truncated to 2000 chars) |
| `result_data` | `jsonb` | YES | `'{}'` | Result data payload. **Defined in `ToolHistoryRecord` and migration but never written by `persistence.py`.** See [§19](#19-known-inconsistencies). |
| `latency_ms` | `real` | YES | `0` | Execution latency in milliseconds |
| `created_at` | `timestamptz` | YES | `now()` | When the tool was called |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `ai_tool_history_pkey` | `id` | btree (PK) |
| `idx_ai_tool_history_owner` | `owner_id` | btree |
| `idx_ai_tool_history_session` | `session_id` | btree |

### RLS

RLS is enabled. Only SELECT is granted to `anon` + `authenticated`.
All writes go through the backend service-role key.

### Repository

**`backend/ai/persistence.py`** — `record_tool_call`. Inserts:
`owner_id`, `session_id`, `tool_name`, `arguments`, `result_success`,
`result_message` (truncated), `latency_ms`. Does NOT insert
`result_data`.

**`backend/ai/database/tool_history_repository.py`** — interface +
in-memory fallback (`ToolHistoryRecord`, `ToolHistoryRepository`,
`InMemoryToolHistoryRepository`). `ToolHistoryRecord` fields include
`result_data` — not written by `persistence.py`.

---

## 12. ai_provider_stats

Per-provider aggregate statistics. One row per (provider, owner) pair.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `provider_name` | `text` | NO | — | Provider name (part of composite PK) |
| `owner_id` | `bigint` | NO | `0` | Owner ID (part of composite PK) |
| `total_requests` | `integer` | NO | `0` | Total requests made |
| `successful_requests` | `integer` | NO | `0` | Successful requests |
| `failed_requests` | `integer` | NO | `0` | Failed requests |
| `total_prompt_tokens` | `integer` | NO | `0` | Cumulative prompt tokens |
| `total_completion_tokens` | `integer` | NO | `0` | Cumulative completion tokens |
| `avg_latency_ms` | `real` | NO | `0` | Average latency |
| `last_request_at` | `timestamptz` | YES | — | Last request timestamp |
| `updated_at` | `timestamptz` | NO | `now()` | Last update |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `ai_provider_stats_pkey` | `(provider_name, owner_id)` | btree (composite PK) |

### RLS

RLS should be enabled. Only SELECT is granted to `anon` +
`authenticated`. All writes go through the backend service-role key.

### Repository

**`backend/ai/database/provider_stats_repository.py`** — interface +
in-memory fallback (`ProviderStatsRecord`, `ProviderStatsRepository`,
`InMemoryProviderStatsRepository`) plus a Supabase-backed implementation
(`SupabaseProviderStatsRepository`) wired into `RepositoryManager` when
Supabase is available. Updates come from
`backend/ai/database/usage_recorder.py` (read-modify-write upsert per
(provider, owner)). Methods: `get_or_create`, `record_request`, `get`,
`list_all`. Migration `20260827000004_create_ai_provider_stats_table.sql`
has been generated from this section (the composite PK matches the
writer's upsert conflict target) — pending manual application.

---

## 13. ai_usage

Per-request token usage log. One row per AI API call.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `id` | `bigserial` | NO | `nextval(...)` | Primary key |
| `owner_id` | `bigint` | NO | — | Telegram user ID of the bot owner |
| `session_id` | `text` | YES | — | AI session ID |
| `provider` | `text` | YES | — | Provider name |
| `model` | `text` | YES | — | Model name |
| `prompt_tokens` | `integer` | YES | `0` | Prompt token count |
| `completion_tokens` | `integer` | YES | `0` | Completion token count |
| `total_tokens` | `integer` | YES | `0` | Total token count |
| `latency_ms` | `real` | YES | `0` | Request latency in milliseconds |
| `token_source` | `text` | YES | `NULL` | `actual` / `estimated` / `unavailable` — the honesty label for the token counts (never fabricated) |
| `created_at` | `timestamptz` | YES | `now()` | When the usage was recorded |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `ai_usage_pkey` | `id` | btree (PK) |
| `idx_ai_usage_owner` | `owner_id` | btree |
| `idx_ai_usage_created_at` | `created_at` | btree |

### RLS

RLS should be enabled. Only SELECT is granted to `anon` +
`authenticated`. All writes go through the backend service-role key.

### Repository

**`backend/ai/database/usage_repository.py`** — interface +
in-memory fallback (`UsageRecord`, `UsageRepository`,
`InMemoryUsageRepository`) plus a Supabase-backed implementation
(`SupabaseUsageRepository`) wired into `RepositoryManager` when Supabase
is available. Writes come from `backend/ai/database/usage_recorder.py`,
which persists the normalized `AIExecutionRecord` exactly once per
request. Methods: `create`, `total_tokens`, `daily_tokens`, `recent`.
Migration `20260827000003_create_ai_usage_table.sql` has been generated
from this section (bigserial `id` — the Supabase writer never sends
`id`) — pending manual application.

---

## 14. ai_preferences

Per-owner AI personality and behavior preferences. One row per owner.

### Columns

| Column | SQL Type | Nullable | Default | Notes |
|---|---|---|---|---|
| `owner_id` | `bigint` | NO | — | Primary key. Telegram user ID of the bot owner. |
| `language` | `text` | NO | `'en'` | Preferred language |
| `personality` | `text` | NO | `'helpful'` | Personality mode |
| `response_style` | `text` | NO | `'concise'` | Response style |
| `custom_instructions` | `text` | NO | `''` | Custom system instructions |
| `auto_memory` | `boolean` | NO | `true` | Whether auto-memory is enabled |
| `auto_tools` | `boolean` | NO | `true` | Whether auto-tools are enabled |
| `metadata` | `jsonb` | YES | `'{}'` | Arbitrary extra metadata |
| `created_at` | `timestamptz` | YES | `now()` | When the preference row was created |
| `updated_at` | `timestamptz` | YES | `now()` | Last update timestamp |

### Indexes

| Index | Columns | Type |
|---|---|---|
| `ai_preferences_pkey` | `owner_id` | btree (PK) |

### RLS

RLS should be enabled. Only SELECT is granted to `anon` +
`authenticated`. All writes go through the backend service-role key.

### Repository

**`backend/ai/database/preferences_repository.py`** — interface +
in-memory fallback (`PreferencesRecord`, `PreferencesRepository`,
`InMemoryPreferencesRepository`). Methods: `get_or_create`, `update`,
`get`. No Supabase-backed implementation is wired here yet. No
migration has been applied for this table.

---

## 15. ai_tasks

Durable owner-scoped AI task definitions for the future scheduler. This table exists in the repository migration `supabase/migrations/20260829000001_create_ai_tasks.sql`; live Supabase application is a separate manual deployment step and has not been verified.

| Column | Type | Nullable | Default / constraint |
|---|---|---:|---|
| `id` | `bigserial` | NO | PRIMARY KEY |
| `owner_id` | `bigint` | NO | Authenticated Telegram owner; no owner FK |
| `label` | `text` | NO | Nonblank; max 256 enforced by repository |
| `status` | `text` | NO | `'active'`; CHECK `active`, `paused`, `completed`, `failed`, `expired`, `deleted` |
| `version` | `integer` | NO | `1`; CHECK `> 0` |
| `schedule_type` | `text` | NO | CHECK `once`, `interval`, `daily`, `weekly`, `event` (extended by migration `20260904000001_add_event_schedule_type.sql`) |
| `schedule` | `jsonb` | NO | Max 16,384 bytes by migration. Time schedules: `{seconds}`, `{at,timezone}`, `{hour,minute,timezone}`, `{weekday,hour,timezone}`. Event schedule: `{"trigger": {...}}` — the resolved trigger spec below |
| `timezone` | `text` | NO | Explicit IANA identifier validated by application |
| `next_run_at` | `timestamptz` | YES | UTC due instant |
| `actions` | `jsonb` | NO | JSON array, 1–5 actions, max 32,768 bytes |
| `notification_destination` | `jsonb` | NO | Max 4,096 bytes; explicit owner-scoped destination |
| `created_at` | `timestamptz` | NO | `now()` |
| `updated_at` | `timestamptz` | NO | `now()` |
| `terminal_at` | `timestamptz` | YES | Terminal lifecycle timestamp |
| `ai_instruction` | `text` | YES | Explicit bounded natural-language instruction for future per-occurrence AI preparation; never an executable action; max 4,096 UTF-8 characters in application contract |

### Event trigger spec (schedule_type `event`)

An event-triggered task has no wall-clock time: `next_run_at` stays NULL and
`schedule` carries a single `trigger` object. The AI produces the
model-facing (unresolved) form — `sender`/`chat` are semantic NAMES, never
numeric ids — and the Self Bot resolves names against its own trusted
dialogs at creation time. The persisted (resolved) form is:

```json
{
  "trigger": {
    "type": "telegram_message",
    "sender_id": 123456,            // resolved; optional
    "sender_name": "John",           // display only
    "chat_id": -100123456,          // resolved; optional
    "chat_title": "Chat name",       // display only
    "contains": ["urgent"],          // all terms must appear (case-insensitive)
    "text_equals": "...",            // exact match
    "starts_with": "...",
    "has_media": true,
    "is_reply": true,
    "direction": "incoming"          // incoming | outgoing | any (default incoming)
  }
}
```

Validation is application-side (`backend/ai/task_trigger.py`): bounded
fields only, at least one matching condition, unknown keys rejected,
resolved ids must be positive integers. The runtime evaluates triggers
deterministically against incoming Telegram events — no LLM per message.
Matching occurrences use key `"<task_id>:ev:<chat_id>:<message_id>"` (unique
with `task_id`), so duplicate delivery of the same event cannot create a
second occurrence.

Indexes are `idx_ai_tasks_status_next_run (status, next_run_at)` and `idx_ai_tasks_owner_updated (owner_id, updated_at DESC)`. There is no trigger or SQL schedule logic. Task version edits and lifecycle validation are repository/application responsibilities. Actions are bounded JSON; no action or step table exists.

RLS is enabled. The migration grants SELECT to `anon` and `authenticated` and adds no public write policy. Backend access uses the service-role client and repository methods must still filter by `owner_id`. Retain task definitions until explicit owner deletion/terminal cleanup policy is approved.

## 16. ai_task_occurrences

Durable occurrence/attempt history for `ai_tasks`. The base definition lives in `20260829000001_create_ai_tasks.sql`; `preparation_metadata` was added to that file later, and because the table is created with `CREATE TABLE IF NOT EXISTS`, a database whose table predates the column never received it. `20260912000001_add_ai_task_occurrences_preparation_metadata.sql` is the idempotent repair (`ADD COLUMN IF NOT EXISTS` + the object/size CHECK constraints + a PostgREST schema-cache reload). Live Supabase deployment is manual and must be verified by the operator; the application additionally keeps the durable STATE transition working when only this optional column is missing, and reports the dropped diagnostics field explicitly (never as a durable store outage).

| Column | Type | Nullable | Default / constraint |
|---|---|---:|---|
| `id` | `bigserial` | NO | PRIMARY KEY |
| `task_id` | `bigint` | NO | REFERENCES `ai_tasks(id)` ON DELETE RESTRICT |
| `owner_id` | `bigint` | NO | Denormalized owner; no owner FK |
| `occurrence_key` | `text` | NO | Nonblank; unique with `task_id` |
| `definition_version` | `integer` | NO | CHECK `> 0` |
| `action_snapshot` | `jsonb` | NO | JSON array, 1–5 actions, max 32,768 bytes |
| `scheduled_for` | `timestamptz` | NO | UTC scheduled instant |
| `attempt` | `smallint` | NO | `1`; CHECK 1–3 |
| `status` | `text` | NO | `'claimed'`; CHECK `claimed`, `running`, `succeeded`, `failed`, `retry_pending`, `cancelled`, `expired`, `interrupted` |
| `claimed_at` | `timestamptz` | YES | Claim timestamp |
| `started_at` | `timestamptz` | YES | Start timestamp |
| `finished_at` | `timestamptz` | YES | Finish timestamp |
| `retry_at` | `timestamptz` | YES | Required by migration for `retry_pending` |
| `error_metadata` | `jsonb` | NO | `'{}'`; object, max 8,192 bytes |
| `result_metadata` | `jsonb` | NO | `'{}'`; object, max 8,192 bytes |
| `created_at` | `timestamptz` | NO | `now()` |
| `updated_at` | `timestamptz` | NO | `now()` |
| `preparation_metadata` | `jsonb` | NO | `'{}'`; empty or explicit `prepared_action` envelope containing only bounded validated action data, matching `definition_version` and `prepared_at`; max 8,192 bytes |

Indexes are unique `uq_ai_task_occurrences_task_key (task_id, occurrence_key)`, `idx_ai_task_occurrences_owner_scheduled (owner_id, scheduled_for DESC)`, and `idx_ai_task_occurrences_task_scheduled (task_id, scheduled_for DESC)`. No `(status, retry_at)` index is present because the current repository does not issue that query. The unique index prevents duplicate durable occurrences, not duplicate Telegram side effects.

RLS is enabled. SELECT is granted to `anon` and `authenticated`; no public write policy is added. The task FK uses `ON DELETE RESTRICT`, preserving history. Occurrences snapshot `definition_version` and `action_snapshot`; later task edits do not rewrite history. Recommended retention is bounded terminal history (initially 90 days, subject to operations approval).

## 17. Relationships

The task foundation is the one intentional enforced relationship. The
following logical relationships exist without foreign keys:

- `ai_messages.session_id` → `ai_sessions.session_id` (logical)
- `ai_tool_history.session_id` → `ai_sessions.session_id` (logical)
- `ai_usage.session_id` → `ai_sessions.session_id` (logical)
- `ai_task_occurrences.owner_id` must match `ai_tasks.owner_id` by repository validation; there is no owner FK.
- `owner_id` on existing tables links rows to the bot owner but is not a foreign key.

`ai_task_occurrences.task_id` → `ai_tasks.id` is enforced by the task
migration with `ON DELETE RESTRICT`. Other relationships remain logical;
future changes require separate review.

---

## 18. RLS Policy Model

All tables have RLS enabled. Only SELECT policies are granted to
`anon` + `authenticated` (read-only dashboard access). All writes
(INSERT/UPDATE/DELETE) go through the backend's service-role key,
which bypasses RLS entirely. There are no anon/authenticated write
policies.

### Required Policies per Table

Every table must have exactly one SELECT policy:

```sql
CREATE POLICY "<table>_select" ON <table> FOR SELECT
  TO anon, authenticated USING (true);
```

The `USING (true)` is acceptable because:
1. All data belongs to a single owner (single-tenant self-bot).
2. The dashboard is read-only and has no sign-in screen.
3. All writes go through the backend service-role key (bypasses RLS).

Tables that do not yet have RLS enabled (`ai_preferences`) MUST have
RLS enabled and a SELECT policy added in their creation migration.
`ai_usage` and `ai_provider_stats` receive RLS + SELECT policies in
their creation migrations (`20260827000003` / `20260827000004`).

---

## 19. Panel Database

The Glass Panel system uses a **column-per-setting** model on the
`panel_settings` table. Each setting is a real typed column — no
key-value store, no JSONB blobs.

### Architecture

```
Supabase (panel_settings table)
  ↓
PanelSettingsRepository  (raw DB access — backend/services/panel_settings_repository.py)
  ↓
PanelSettingsService     (cache + validation — backend/services/settings_service.py)
  ↓
Glass Panel (reads via get_*(), writes via set_*())
```

### Cache-First Reads

Every getter reads from an in-memory cache. The database is NEVER
queried on a button click. The cache is loaded once at startup from
the DB (or from hardcoded defaults if the DB is unavailable).

### Write-Through Cache

On any `set_*()` call, the service:
1. Validates the value against a type/range validator.
2. Writes to the DB via the repository.
3. Reloads the cache from the DB.

Cache and DB are never left inconsistent.

### Settings (12 wired columns + `ghost_seen_retention_seconds` migrated-but-unwired)

| Column | Type | Default | Range/Constraint |
|---|---|---|---|
| `auto_close_enabled` | bool | `true` | must be boolean |
| `auto_close_delay` | int | `120` | 5..3600 (seconds) |
| `max_deep_save_mb` | int | `50` | 1..500 (MB) |
| `delete_batch_size` | int | `100` | 1..1000 |
| `log_retention_days` | int | `7` | 1..365 (days) |
| `panel_timeout_seconds` | int | `300` | 30..86400 (seconds) |
| `allow_multiple_panels` | bool | `false` | must be boolean |
| `reuse_existing_panel` | bool | `true` | must be boolean |
| `language` | str | `"en"` | non-empty string |
| `debug_callbacks` | bool | `false` | must be boolean |
| `owner_only` | bool | `true` | must be boolean |
| `dashboard_font` | str | `"default"` | one of `FONT_KEYS` |
| `ghost_seen_retention_seconds` | int | `2592000` | `0` (Never) or 300..31536000 (5 min..365 days) |

`ghost_seen_retention_seconds` has **no accessor in `settings_service`
and no production consumer** (see §19.13); it is listed here because
the column exists and the Glass UI presets are tested, not because it
is wired. If the DB is unavailable, the service uses hardcoded
`_DEFAULTS` for all 12 wired settings. The bot continues to function
normally — all panel operations work with default values. Every
Supabase call that fails logs a warning and falls back silently.

---

## 20. In-Memory Fallback

The bot is designed to run **with or without Supabase**. When the DB
is unavailable, all operations use in-memory fallbacks:

| Table | Fallback mechanism |
|---|---|
| `saved_items` | in-memory list in `db/client.py` |
| `bio_state` | in-memory dict in `db/client.py` |
| `username_state` | in-memory dict in `db/client.py` |
| `bot_logs` | in-memory list in `db/client.py` |
| `panel_settings` | hardcoded `_DEFAULTS` dict in `settings_service.py` |
| `ai_config` | `_fallback_config` dict in `config_store.py` |
| `ai_sessions` | `InMemorySessionRepository` in `database/manager.py` |
| `ai_messages` | `InMemoryMessageRepository` in `database/manager.py` |
| `ai_memories` | `InMemoryMemoryRepository` in `database/manager.py` |
| `ai_tool_history` | `InMemoryToolHistoryRepository` in `database/manager.py` |
| `ai_provider_stats` | `InMemoryProviderStatsRepository` in `database/manager.py` |
| `ai_usage` | `InMemoryUsageRepository` in `database/manager.py` |
| `ai_preferences` | `InMemoryPreferencesRepository` in `database/manager.py` |

The bot never crashes due to a database error. Every Supabase call that
fails logs a warning and falls back silently.

> **Design concern:** The silent fallback means schema problems are
> invisible at runtime. A table can be missing or a column can be absent
> and the bot will appear to function normally — but data is not
> persisted. The `ai_config` table is the most affected: trigger words
> and provider/model selections set by the user are lost on restart if
> the table or columns are missing. See [§19](#19-known-inconsistencies).

---

## 21. Known Inconsistencies

This section documents every discrepancy between the repository code,
the applied migrations, and this specification. Future migrations
MUST resolve all items marked **[MIGRATION REQUIRED]**.

### 19.1 `ai_config` — trigger columns not in any migration file

**Severity:** High

**Problem:** `config_store.py` reads and writes `trigger_en` and
`trigger_fa` columns on `ai_config`. The base migration
(`20260805075707`) creates `ai_config` but does NOT include these
columns. A migration file named `20260805130000_add_ai_trigger_columns`
was referenced in prior documentation but **does not exist** in the
`supabase/migrations/` directory.

**Impact:** When the bot tries to save trigger words to the database,
the Supabase API will either error (column does not exist) or silently
ignore the fields. The in-memory fallback catches the error, so the
bot continues running, but trigger words are lost on restart.

**Resolution [MIGRATION CREATED]:** Migration
`20260827000002_add_ai_config_trigger_columns.sql` adds
`trigger_en TEXT DEFAULT NULL` and `trigger_fa TEXT DEFAULT NULL` to
the `ai_config` table — pending manual application.

### 19.2 `ai_config` — `last_request_at` / `last_latency_ms` (RESOLVED in code)

**Severity:** Resolved

**Status:** `config_store.py::_save_config_sync` now includes
`last_request_at` and `last_latency_ms` in the DB payload, and
`record_request()` performs a targeted update of only those two columns.
The columns exist in the migration (`20260805075707`). No action
remaining for this item.

### 19.2a `ai_config` — `show_question` presentation preference

**Severity:** Medium

**Problem:** `config_store.py` includes `show_question` (boolean, the
durable "Show my message in AI replies" presentation preference) in every
`ai_config` upsert payload and merges it in `get_config` with a `False`
default. The base migration does not create this column. As with the
trigger columns (§19.1), an un-migrated database makes the whole upsert
degrade to the in-memory fallback, so the preference would be lost on
restart.

**Resolution [MIGRATION CREATED]:** Migration
`20260913000000_add_ai_config_show_question.sql` adds
`show_question BOOLEAN NOT NULL DEFAULT false` (idempotent, with
rollback SQL) — pending manual application. Until it is applied, the
application still serves the default (`false`) and the toggle degrades
to the in-memory fallback exactly like every other `ai_config` key.
No second preference store exists; the RAM-only `ExecutionTelemetry`
store is NOT a source of truth for this preference.

### 19.2b `ai_config` — the three Gemini STT settings

**Severity:** Medium

**Problem:** `config_store.py` includes `stt_model`, `stt_language` and
`stt_passes` (the owner-editable speech-to-text behavior settings,
edited from AI → Settings → Advanced) in every `ai_config` upsert
payload and merges them in `get_config` with the defaults (empty model,
empty language, one pass). The base migration does not create these
columns. As with the trigger columns (§19.1) and `show_question`
(§19.2a), an un-migrated database makes the whole upsert degrade to the
in-memory fallback, so a Telegram change would be lost on restart.

**Resolution [MIGRATION CREATED]:** Migration
`20260917000001_add_ai_config_stt_settings.sql` adds
`stt_model TEXT`, `stt_language TEXT` and
`stt_passes INTEGER NOT NULL DEFAULT 1` (plus the
`ai_config_stt_passes_range` CHECK `BETWEEN 1 AND 3`) — idempotent, with
rollback SQL, pending manual application. Until it is applied, the
application still serves the defaults (the single-pass route) and the
Settings controls degrade to the in-memory fallback exactly like every
other `ai_config` key. These values are behavior, not deployment
configuration: no ENV variable is required for them and no second
settings store exists.

### 19.3 `panel_settings` — 10 columns missing from migrations

**Severity:** High

**Problem:** `settings_service.py` reads and writes 12 typed settings
columns on `panel_settings` (11 base settings + `dashboard_font`).
The base applied migrations only create 4 columns:
`key`, `auto_close_enabled`, `updated_at`, `update_stale_seconds`.
The remaining 10 columns (`auto_close_delay`, `max_deep_save_mb`,
`delete_batch_size`, `log_retention_days`, `panel_timeout_seconds`,
`allow_multiple_panels`, `reuse_existing_panel`, `language`,
`debug_callbacks`, `owner_only`) were supposed to be added by
migrations `20260730220000_panel_settings_column_model.sql` and
`20260730230000_panel_settings_full_13_columns.sql`, but **neither
file exists** in the repository.

**Impact:** When `panel_settings_repository.load()` tries to `SELECT *`
from the table, it will only get 4 columns. The missing columns will
be absent from the response dict. The `settings_service` cache-first
approach will fall back to hardcoded defaults for all missing columns.
Settings changes by the user are not persisted.

**Resolution [MIGRATION CREATED]:** Migration
`20260827000001_add_missing_panel_settings_columns.sql` adds all
10 missing columns to `panel_settings` with the types and defaults
listed in [§6](#6-panel_settings) (plus CHECK constraints mirroring the
`settings_service` validators) — pending manual application.

Migration `20260823120000_add_dashboard_font_and_ghost_seen_settings.sql`
now exists and covers `dashboard_font` and (originally)
`ghost_seen_retention_days`; it was applied to the live database and
verified by the owner. `20260823130000_ghost_seen_retention_duration.sql`
then migrates that setting to `ghost_seen_retention_seconds`
(idempotent; backfills and drops the days column) — pending manual
application. The remaining 10 columns are delivered by
`20260827000001_add_missing_panel_settings_columns.sql`; see §30 for the
existing-database reconciliation that makes them land on a live table.

### 19.4 `bot_settings` table — NOT orphaned; Ghost Seen allow-list lives here

**Severity:** Corrected (previous doc claim was wrong)

**Problem:** This document previously claimed `bot_settings` had no
code references and should be dropped. That is false: Ghost Seen v2
(`backend/services/ghost_seen_v2.py`) actively reads and writes the
key `ghost_seen_allowed_chats` (JSON array of chat IDs) — loaded once
per process by `_ensure_allowed_loaded_async()` and persisted on every
Manage toggle by `_persist_allowed_to_db()`. It is the **only durable
store for the Ghost Seen per-chat privacy allow-list**.

**Verified restart lifecycle (persistence audit):** toggles run
`Manage → _toggle_permission_action → await _ensure_allowed_loaded_async()
→ allow_chat()/disallow_chat() → _persist_allowed_to_db()` (UPDATE, or
INSERT on first write, of the JSON array at `key =
'ghost_seen_allowed_chats'`). After a restart the handler `register()`
fires a background preload, and Browser/Manage open plus every toggle
await the SAME single in-flight load task; the loaded JSON list is
unioned into the runtime set and enforced by `is_chat_allowed()`.
Persistence races are closed: concurrent callers await the in-flight
load (a toggle can never persist a partial list over the persisted one)
and persist writes are serialized with an in-lock snapshot (the DB can
never end on an out-of-order stale value). Regression coverage:
`tests/test_65_ghost_seen_v2_restart_persistence.py`.

The table still has no `owner_id` (single-tenant assumption) and stores
a JSON blob where a relational representation is appropriate, but it
cannot be dropped until the allow-list is migrated to `ghost_chats`
(§22, §24, §20 item 8/9).

### 19.5 `ai_messages.tool_calls` — three-way mismatch

**Severity:** High

**Problem:** Three sources disagree about the `tool_calls` column:
- `MessageRecord` in `message_repository.py` defines it as a field.
- This document lists it as a column.
- The applied migration (`20260804145402`) does NOT create it.
- `persistence.py` does NOT write it.

**Impact:** If a Supabase-backed `MessageRepository` is ever wired up,
inserts will fail because the column does not exist in the database.

**Resolution [MIGRATION REQUIRED]:** A future migration must add
`tool_calls JSONB DEFAULT '[]'` to `ai_messages`. Code in
`persistence.py` should also be updated to populate it — but that is a
code fix, not a migration.

### 19.6 `ai_tool_history.result_data` — never written

**Severity:** Low

**Problem:** The `result_data` column exists in the migration and in
`ToolHistoryRecord`, but `persistence.py` never includes it in the
insert payload. It defaults to `'{}'` and is never populated.

**Resolution [CODE REQUIRED]:** `persistence.py` should include
`result_data` in its insert payload if available. This is a code fix,
not a migration.

### 19.7 `saved_items.short_code` / `saved_items.file_name` — dead columns

**Severity:** Low

**Problem:** Migration `20260718143752` added `short_code` and
`file_name` columns to `saved_items` and created 5 trigram GIN indexes
on them. No Python code ever reads or writes `short_code`. No Python
code ever writes `file_name` (the insert payload from `save_service.py`
does not include it). These columns and their dependent indexes are
dead weight.

**Resolution [MIGRATION REQUIRED]:** A future migration should drop
the trigram indexes and the dead columns. This must be done carefully —
dropping columns is a destructive operation that should only be
performed after confirming no data of value exists in them.

### 19.8 `ai_provider_stats` / `ai_usage` / `ai_preferences` — no migrations

**Severity:** Medium

**Problem:** Three AI tables have repository interfaces and in-memory
implementations but no migration has been applied for any of them.
The runtime operates entirely in-memory for these tables.

**Resolution [MIGRATION REQUIRED]:** The code now wires Supabase-backed
implementations for `ai_provider_stats` and `ai_usage` (via
`RepositoryManager` + `usage_recorder.py`); `ai_preferences` remains
interface + in-memory only. The runtime degrades to in-memory behavior
when Supabase is unavailable or the tables are missing. Migrations for
`ai_provider_stats` (`20260827000004`) and `ai_usage` (`20260827000003`)
have been generated and are pending manual application;
`ai_preferences` ([§14](#14-ai_preferences)) still has no migration
(see §20, item 6).

### 19.9 AI configuration persistence is non-deterministic

**Severity:** High

**Problem:** The AI configuration flow is:
1. User selects a provider → `config_store.save_config()` writes to DB.
2. If the DB write fails (table missing, column missing, network error),
   the error is caught, a warning is logged, and the in-memory fallback
   is updated.
3. On the next read, `config_store.get_config()` reads from the DB.
4. If the DB read fails, it falls back to the in-memory dict.
5. On restart, the in-memory dict is lost. The DB has no data. The
   user's configuration is gone.

This means configuration persistence depends on whether the database
is available and has the correct schema. If the schema is wrong, the
bot appears to work but does not persist anything.

**Resolution:** Resolve issues 19.1 and 19.3 (add missing columns via
migrations). After that, the configuration flow will be deterministic:
writes either succeed (data persists) or fail (error is visible).

### 19.10 `ai_messages.telegram_msg_id` — optional column for persistent reply resolution

**Severity:** Low (enhancement, not a bug)

**Problem:** The `ReplyResolver` (`backend/ai/context/reply_resolver.py`)
maps Telegram message IDs to full AI response content so that when the
owner replies to a previous AI message, the full untruncated AI text is
injected as high-priority context. Currently this mapping is stored
in-memory only (bounded LRU, 500 entries). On restart, all mappings are
lost — the owner can only get reply-aware context for AI messages
produced during the current process lifetime.

**Current behavior:** In-memory resolution works for active sessions.
This is the primary use case (the owner replies to a recent AI message
within the same session). The 200-character `text_preview` is used as
a fallback when the in-memory mapping is not found.

**Optional future schema change:** Add a `telegram_msg_id` column to
`ai_messages` so the resolver can fall back to a DB lookup when the
in-memory map misses. This would enable reply-aware context across
restarts.

**Required schema change (if implemented):**

| Table | Column | Type | Nullable | Default | Index | Purpose |
|---|---|---|---|---|---|---|
| `ai_messages` | `telegram_msg_id` | `bigint` | YES | `NULL` | `idx_ai_messages_tg_msg` (btree on `telegram_msg_id`) | Stores the Telegram message ID that contains the AI response. Enables the ReplyResolver to look up full AI content by Telegram message ID across restarts. |

**Why it is required:** Without this column, the in-memory resolver
cannot be backed by persistent storage. The column is nullable because
not all AI messages have a Telegram message ID (e.g. internal/system
messages). The index enables fast lookup by `telegram_msg_id` when
resolving a reply.

**Important:** This schema change is NOT required for the current
reply-aware context feature to work. The in-memory resolver is the
primary mechanism. This column would only add cross-restart persistence
as a fallback. Application code must NOT depend on this column existing —
the resolver must always handle the case where the column is absent.

### 19.11 `ai_database/manager.py` — partial Supabase wiring

**Severity:** Medium

**Problem:** `backend/ai/database/manager.py` wires Supabase-backed
implementations for **three** repositories when Supabase env vars are
present: `memory` (`SupabaseMemoryRepository` → `ai_memories`),
`provider_stats` (`SupabaseProviderStatsRepository` →
`ai_provider_stats`), and `usage` (`SupabaseUsageRepository` →
`ai_usage`). The remaining four (`session`, `message`, `preferences`,
`tool_history`) are still in-memory only. Separately, `persistence.py`
handles `ai_sessions`, `ai_messages`, `ai_tool_history` directly —
bypassing the repository pattern.

**Impact:** `ai_usage` and `ai_provider_stats` have **no applied
migration**, so their Supabase writes currently fail and degrade to
logged warnings (see §19.8). `ai_memories` has a live repository path
via the Engine, but nothing writes memories automatically (auto-memory
is off by default).

**Resolution [CODE REQUIRED]:** Either wire the remaining Supabase
implementations into the repository manager or remove the repository
abstraction.

### 19.12 `ghost_chats` — migrated table with no code consumer

**Severity:** High (schema hygiene + Ghost Seen correctness)

**Problem:** Migration `20260822090000` created `ghost_chats`
(`chat_id`, `display_name`, `last_preview`, `last_message_at`,
`unread_count`, timestamps) for the former "Ghost Room" design.
**No Python module reads or writes this table** — the only references
are skipped legacy tests in `tests/test_51_execution27.py`. The legacy
`backend/services/ghost_seen_service.py` that targeted it was removed
in the Ghost Seen v2 rebuild. The live Ghost Seen v2 feature persists
its per-chat privacy allow-list in `bot_settings` instead (§19.4).

**Impact:** Dead table today, and the allow-list lives in a legacy
key-value table without owner scoping.

**Resolution [MIGRATION + CODE REQUIRED]:** Repurpose `ghost_chats` as
the authoritative per-chat Ghost Seen state table (additive `owner_id`
+ `allowed` columns), migrate the `bot_settings` blob into it, then
drop `bot_settings`. See §22 and §24.

### 19.13 `ghost_seen_retention_seconds` — configured but unconsumed

**Severity:** Medium

**Problem:** The `panel_settings.ghost_seen_retention_seconds` column
(migration `20260823130000`, pending application) has **no accessor in
`settings_service.py`, no Glass UI panel, no retention job, and no code
consumer anywhere in `backend/`**. Only tests and this document
reference it. The legacy `ghost_seen_service.apply_retention` /
`delete_expired_rows` that would have consumed it was removed.

**Impact:** The setting is persisted but cannot be changed or read by
production code; no registry retention is enforced.

**Resolution:** Either wire the setting (accessor + Glass UI presets
already exist in tests) and implement a bounded retention job targeting
`ghost_chats` only (never Telegram RPC), or remove the column. The
schema is additive and correct as designed.

### 19.14 `panel_settings.update_stale_seconds` — migrated but never read

**Severity:** Low

**Problem:** Migration `20260730210551` added `update_stale_seconds`
(default 300). The heartbeat uses a hardcoded `_STALL_THRESHOLD = 90.0`
(`backend/runtime/heartbeat.py`) and never reads the setting; no other
module does either. The migration comment claiming "The watchdog reads
this value via settings_service on every tick" is false.

**Resolution [CODE REQUIRED]:** Either consume the setting in the
heartbeat invariant check or drop the column. Note the setting default
(300s) differs from the hardcoded threshold (90s) — wiring it changes
recovery behavior and must be tested.

### 19.15 `GHOST_SEEN_DESTINATION_CHAT_ID` / `_NAME` — defined, unused

**Severity:** Low

**Problem:** `backend/config.py` loads
`GHOST_SEEN_DESTINATION_CHAT_ID` / `GHOST_SEEN_DESTINATION_CHAT_NAME`
and `ghost_seen_v2.py` exposes `get_destination_chat_id()` /
`get_destination_chat_name()`. **No production code calls these
getters** — only tests. INVESTIGATION.md documents them as "for future
notification flows".

**Impact:** Dead environment configuration. Manual Reply and AI Reply
always deliver to the source private chat. If the destination flow is
never built, remove the env vars and getters; if it is built, it is
configuration (not database state) and must never override the source
chat as the AI Reply destination.

### 19.16 `dashboard_font` — backend 23-key list vs frontend 4-key list

**Severity:** Medium (UX, not schema)

**Problem:** The authoritative allow-list is `FONT_KEYS` (23 keys) in
`backend/helper/font_style.py`, enforced by `settings_service` and the
`panel_settings_dashboard_font_check` constraint. The web dashboard
(`src/App.tsx`) hardcodes 4 options (`default`, `system`, `mono`,
`serif`), of which `system` and `serif` are **not** valid backend keys
— the API rejects them, and any Glass-UI-selected key outside the 4
falls back to `default` in the dashboard.

**Impact:** The two surfaces can disagree about the active font after a
restart (dashboard shows its own fallback; Telegram panels keep the
persisted key).

**Resolution [CODE REQUIRED]:** Derive dashboard options from
`/api/settings` plus a shared key→CSS-stack map, or accept only backend
keys. Schema is correct; this is a frontend/backend contract fix.

### 19.17 `ai_preferences` — specified, in-memory only, no producer

**Severity:** Medium

**Problem:** `ai_preferences` has a full schema (§14) and an interface
+ `InMemoryPreferencesRepository`, but **no migration and no code path
that writes it**. `Dispatcher._load_preferences` reads
`preferences_repository.get_or_create(owner_id)` (in-memory only) and
falls back to `PreferencesContext()` defaults. No Glass UI or tool
writes preferences.

**Impact:** Personality/behavior preferences cannot persist across
restarts; every process start uses defaults.

**Resolution:** Either (a) add the migration + a Supabase-backed
repository + a Glass UI write path, or (b) mark the table speculative
and remove the interface until a producer exists.

### 19.18 `saved_items.save_code` — documentation drift (SV-NNNNNN → S####)

**Severity:** Low (documentation only)

**Problem:** §2 and the frontend type claimed codes like `SV-000001`.
The live generator `db/client.py::get_next_save_code` returns `S` + 4
characters (`S0001`, or a random `SXXXX` on collision) and writes only
`save_code` — the legacy `short_code` column (migration
`20260718143752`) is never written. Save type is always `'deep'`; the
`'forward'` value exists only for legacy rows and the CHECK constraint.

**Resolution:** §2 corrected in this revision. No schema change
required; `short_code`/`file_name` cleanup stays §19.7.

### 19.19 `ai_sessions` — primary-key drift between migration and spec

**Severity:** Low

**Problem:** The applied migration uses `id bigserial PRIMARY KEY` +
`session_id text UNIQUE`; this document (§8) and `persistence.py`
address rows by `session_id`. `ai_messages` / `ai_tool_history` /
`ai_usage` reference `session_id` logically (no FK).

**Resolution:** Acceptable as-is (unique `session_id` is the effective
key); optionally promote `session_id` to PK in a future cleanup
migration. No code change needed.

---

## 20. Migration Status

### Applied Migration Files (in `supabase/migrations/`)

| # | File | Creates / Alters | Status |
|---|---|---|---|
| 1 | `20260712234229_lifeos_schema.sql` | `saved_items`, `bio_state`, `bot_logs` (initial) | Superseded by #2 |
| 2 | `20260714111706_create_lifeos_tables.sql` | `saved_items`, `bio_state`, `bot_logs` (authoritative) | Applied |
| 3 | `20260718143752_...save_ux_redesign.sql` | Added `short_code`, `file_name` to `saved_items`; trigram indexes | Applied (dead columns — see §19.7) |
| 4 | `20260726143924_create_panel_settings_table.sql` | `panel_settings` (3 columns: `key`, `auto_close_enabled`, `updated_at`) | Applied (incomplete — see §19.3) |
| 5 | `20260729213959_...create_bot_settings_table.sql` | `bot_settings` key-value table | Applied (live consumer — see §19.4) |
| 6 | `20260730210551_...add_update_stale_seconds.sql` | Added `update_stale_seconds` to `panel_settings` | Applied |
| 7 | `20260801215007_create_username_state_table.sql` | `username_state` | Applied |
| 8 | `20260804145402_create_ai_tables.sql` | `ai_sessions`, `ai_messages`, `ai_memories`, `ai_tool_history` | Applied |
| 9 | `20260805075707_...create_ai_config_table.sql` | `ai_config` (base columns, no triggers) | Applied (incomplete — see §19.1) |
| 9a | `20260913000000_add_ai_config_show_question.sql` | Added `show_question boolean NOT NULL DEFAULT false` to `ai_config` (idempotent, rollback documented) | Pending manual application (see §19.2a) |
| 10 | `20260822090000_create_ghost_chats_table.sql` | `ghost_chats` table for Ghost Seen | Applied (verified by owner) |
| 11 | `20260823120000_add_dashboard_font_and_ghost_seen_settings.sql` | Added `dashboard_font`, `ghost_seen_retention_days` to `panel_settings` | Applied (verified by owner) |
| 12 | `20260823130000_ghost_seen_retention_duration.sql` | Replaces `ghost_seen_retention_days` with `ghost_seen_retention_seconds` (idempotent backfill + drop) | Pending manual application |
| 13 | `20260919000001_create_api_credential_vault.sql` | `api_credentials` metadata table (no secret column) + `api_credential_pool` SECURITY DEFINER resolution RPC over Supabase Vault + `stt_credential_pool` compatibility alias | **NOT APPLIED — owner action required** (the application tolerates its absence; see §29) |
| 14 | `20260919000002_credential_vault_management.sql` | Five owner-scoped SECURITY DEFINER management RPCs over the PART 1 table and Supabase Vault (`api_credential_list`, `api_credential_create`, `api_credential_replace_secret`, `api_credential_update`, `api_credential_delete`). Adds no table, no column and no secret store | **NOT APPLIED — owner action required** (management reports itself as not configured; the runtime and the PART 1 resolution path are unaffected; see §29.13) |
| 15 | `20260920000001_reconcile_canonical_schema.sql` | The canonical reconciliation script (§30): re-asserts every canonical column of all 16 tables with `ADD COLUMN IF NOT EXISTS` + deterministic backfill + final default/NOT NULL binding, data-guards every constraint/index addition, adds the four missing `ai_config` columns and the two task tables, and ends with a drift report. Adds no table, no secret store and no data change beyond deterministic backfills | **NOT APPLIED — owner action required** (§30.11). Byte-identical to `supabase/canonical_bootstrap.sql` and to the §30 SQL block; the reconciliation **snapshot**, extended only by additive successors |
| 16 | `20260921000001_add_saved_items_display_name.sql` | Adds the nullable, default-less `display_name text` to `saved_items` (`ADD COLUMN IF NOT EXISTS`) — the owner-facing saved-item name — and reloads the PostgREST schema cache. Additive successor to #15: it touches no other table, creates no competing definition of `saved_items`, rewrites no row and changes no existing value (`tags` needs no DDL; only its producer changed) | **NOT APPLIED — owner action required.** Apply it BEFORE any Save surface starts writing a name (§2, *User-facing metadata*). On a database that has not run #15 it is safe only if `saved_items` already exists — #15 creates the table |
| 17 | `20260922000001_add_saved_items_search_indexes.sql` | The Save V2 resolver's two search indexes on `saved_items`: `idx_saved_items_display_name_trgm` (GIN trigram for the `display_name ILIKE '%token%'` prefilter; enables `pg_trgm` itself) and `idx_saved_items_tags` (GIN for whole-tag containment `tags.cs.{…}`). Additive successor to #15/#16: indexes only — no column, no row, no `save_code` change; idempotent; reloads the PostgREST schema cache | **NOT APPLIED — owner action required** (§2 index table). Safe on any database that already has `saved_items` (created by #15 or earlier migrations) |

> This table is not exhaustive: the `20260827…`–`20260917…` migration files
> (`ai_config` trigger / `show_question` / STT columns, `ai_usage`,
> `ai_provider_stats`, `ai_tasks`, `ai_task_occurrences`) are not enumerated
> here. The API Credential Vault objects created by row 13 are documented in
> full in §29.

### Missing Migration Files (referenced in prior docs but never created)

| File | Purpose | Blocks |
|---|---|---|
| `20260730220000_panel_settings_column_model.sql` | Obsolete plan to migrate `bot_settings` into `panel_settings` — superseded by the column-per-setting model; `bot_settings` now hosts `ghost_seen_allowed_chats` | — |
| `20260730230000_panel_settings_full_13_columns.sql` | Add remaining 10 `panel_settings` columns | §19.3 |
| `20260805130000_add_ai_trigger_columns.sql` | Add `trigger_en` / `trigger_fa` to `ai_config` | §19.1 |
| *(never created)* `create_ai_usage_table.sql` | Create `ai_usage` from §13 | §19.8 |
| *(never created)* `create_ai_provider_stats_table.sql` | Create `ai_provider_stats` from §12 | §19.8 |
| *(never created)* `create_ai_preferences_table.sql` | Create `ai_preferences` from §14 | §19.8, §19.17 |

### Migrations That Must Be Generated From This Document

Status of each item below is verified against `supabase/migrations/`.
Items 1, 2, 4 and 5 were delivered **under different file names** by the
`2026082700000*` batch, and item 1-3 additionally converge through the §30
reconciliation script. Items 3 and 6 remain genuinely open.

1. **[DELIVERED — `20260827000001_add_missing_panel_settings_columns.sql`]** **Add missing `panel_settings` columns** — add all 10 missing
   columns with types and defaults from [§6](#6-panel_settings). Do NOT
   drop `bot_settings` — it is a live consumer of Ghost Seen allow-list
   state (see §19.4).

2. **[DELIVERED — `20260827000002_add_ai_config_trigger_columns.sql`]** **Add `trigger_en` / `trigger_fa` to `ai_config`** — add
   `trigger_en TEXT DEFAULT NULL` and `trigger_fa TEXT DEFAULT NULL`.

3. **Add `tool_calls` to `ai_messages`** — add
   `tool_calls JSONB DEFAULT '[]'`.

4. **[DELIVERED — `20260827000004_create_ai_provider_stats_table.sql`]** **Create `ai_provider_stats`** — full schema from [§12](#12-ai_provider_stats).

5. **[DELIVERED — `20260827000003_create_ai_usage_table.sql`]** **Create `ai_usage`** — full schema from [§13](#13-ai_usage).

6. **Create `ai_preferences`** — full schema from [§14](#14-ai_preferences).

7. **Drop dead `saved_items` columns and indexes** — drop `short_code`,
   `file_name`, and all 5 trigram GIN indexes. (Low priority — only
   after confirming no data of value exists.)

8. **Correct `ghost_chats`** — the table already exists (applied,
   migration #10). The required migration adds `owner_id` and `allowed`
   and backfills from `bot_settings.ghost_seen_allowed_chats`. See
   [§22](#22-ghost-seen--ghost-pv) and §24.

9. **Drop `bot_settings`** — only after the §20 item 8 backfill is
   applied and Ghost Seen v2 reads/writes `ghost_chats` (code change
   paired with the migration). No code may reference `bot_settings`
   afterwards.

10. **Retention wiring (no schema change)** — implement a consumer for
    `ghost_seen_retention_seconds` (see §19.13) or remove the column.

---

## 22. Ghost Seen / Ghost PV (ghost_chats + bot_settings)

### 22.1 Current runtime state (source-verified)

Ghost Seen v2 (`backend/services/ghost_seen_v2.py` +
`backend/bot/handlers/ghost_seen_v2.py`) keeps the following state:

| State | Storage today | Survives restart? | Should it persist? |
|---|---|---|---|
| Per-chat privacy allow-list (`_allowed_chats`) | `bot_settings` key `ghost_seen_allowed_chats` (JSON array of chat IDs) | Yes (Supabase) / No (in-memory fallback) | **Yes** — authoritative per-chat permission |
| Message selection (`_selections`) | in-memory `dict[int, set[int]]` | No | No — transient UI state; never persisted (privacy) |
| Pending reply input (`_reply_states`) | in-memory `dict` | No | No — transient |
| AI Reply candidate state (`_ai_states` / `_ai_locks`, handler) | in-memory `dict` | No | No — transient |
| Manage directory cache (`_manage_directory`) | in-memory list + 60s TTL | No | No — cache |
| Browser/viewer page & query | lifecycle session nav stack | No | No — transient |
| `ghost_chats` registry rows | **unused table** | — | Designed for the corrected per-chat model (§24) |

The only durable Ghost Seen state is the allow-list. The `ghost_chats`
table exists in the live database but nothing writes it (§19.12). The
former `GHOST_ROOM_ID` output-routing env no longer exists anywhere in
the code; `GHOST_SEEN_DESTINATION_CHAT_ID/_NAME` are defined but unused
(§19.15).

### 22.2 Corrected table: `ghost_chats` — authoritative per-chat Ghost PV state

`ghost_chats` becomes the single authoritative table for per-chat Ghost
Seen state. The change is **additive-only** (no column is dropped):

| Column | Type | Nullable | Default | Purpose | Source requirement |
|---|---|---|---|---|---|
| `chat_id` | `bigint` | NO | — | Telegram chat ID (PK) | existing column |
| `owner_id` | `bigint` | NO | `0` | Owner scoping (single-tenant) | new — every other table carries `owner_id`; required for ownership consistency (§24) |
| `allowed` | `boolean` | NO | `false` | Ghost Seen opt-in permission for this chat | new — replaces the `bot_settings.ghost_seen_allowed_chats` JSON blob |
| `display_name` | `text` | NO | `''` | Last known display name | existing column |
| `last_preview` | `text` | NO | `''` | Last message preview (≤160 chars, PII-minimized) | existing column (write path optional — v2 currently renders live previews) |
| `last_message_at` | `timestamptz` | YES | — | Last activity | existing column |
| `unread_count` | `integer` | NO | `0` | Unread badge | existing column |
| `created_at` / `updated_at` | `timestamptz` | YES | `now()` | Timestamps | existing columns |

**Indexes:** keep `idx_ghost_chats_last_message`; add
`idx_ghost_chats_owner_allowed ON ghost_chats (owner_id) WHERE allowed`.

**RLS:** unchanged — single SELECT policy for `anon`, `authenticated`
USING `(true)`; all writes go through the service-role key.

**Migration path (one additive migration, see §20 items 8–9):**
`ADD COLUMN owner_id bigint NOT NULL DEFAULT 0`;
`ADD COLUMN allowed boolean NOT NULL DEFAULT false`; backfill from
`bot_settings.ghost_seen_allowed_chats`; index; afterwards drop
`bot_settings`. Paired code change (future, not part of this spec):
`ghost_seen_v2._ensure_allowed_loaded_async()` and
`_persist_allowed_to_db()` read/write this table instead of
`bot_settings`.

> **Proportionality note:** No per-message Ghost PV table is proposed.
> The feature processes messages but persists none of their content;
> selection/reply/AI-candidate state is explicitly transient. Message
> content never enters the database.

---

## 21. Migration Generation Rules

When generating new Supabase migrations from this document:

1. **This document is authoritative.** Every table, column, type,
   default, index, and constraint listed here must be created exactly
   as specified. If the code and this document disagree, this document
   wins — the code must be fixed to match.

2. **One migration per logical change.** Do not combine unrelated
   schema changes in a single migration. Each migration should address
   one item from the "Migrations That Must Be Generated" list in
   [§20](#20-migration-status).

3. **Always use `IF NOT EXISTS` / `IF EXISTS`.** Migrations must be
   idempotent — safe to re-run. Use `ADD COLUMN IF NOT EXISTS` (inside
   a `DO $$ ... END $$` block for older Postgres versions) and
   `DROP COLUMN IF EXISTS` / `DROP INDEX IF EXISTS`.

4. **Enable RLS on every new table.** Add one SELECT policy for
   `anon, authenticated` using `USING (true)` — this is a single-tenant
   self-bot, so all data is intentionally readable by the dashboard.

5. **Never use `DROP TABLE` or `DELETE` data** without explicit
   confirmation. The `bot_settings` drop and the dead-column drops in
   §19.7 are exceptions, documented and justified here.

6. **Never use `FOR ALL` in RLS policies.** Write separate policies per
   CRUD verb. For this project, only SELECT policies are needed (all
   writes go through the service-role key).

7. **Use `auth.uid()` for ownership checks** only in multi-user apps.
   This is a single-tenant self-bot — `USING (true)` is correct for
   SELECT policies.

8. **Test migrations against this document.** After generating a
   migration, verify every column, type, default, and index matches
   the specification in this document.

9. **Update this document before writing the migration.** If a schema
   change is needed, first update this document, then generate the
   migration from the updated spec. Never generate a migration that
   contradicts this document.

10. **Document the migration in §20.** After generating a migration,
    add it to the "Applied Migration Files" table with its file name,
    date, and a one-line description.

---

## 23. Self Bot Persistent State Inventory

Source-verified restart-survival matrix for every feature. "Storage"
is where the state lives today; "Must survive restart?" answers what
the actual source requires (not what would be nice).

| Feature | Persistent state | Storage today | Must survive restart? | Gap |
|---|---|---|---|---|
| Save / Deep Save | saved item metadata (code, origin, saved location, sender, media, tags, caption) | `saved_items` | **Yes** — retrieval/delete/discover/stats read it | none (schema drift only, §19.18) |
| Save codes | sequential counter + collision check | computed from `saved_items` count + unique check | **Yes** — codes must be stable | none |
| Bio automation | template, mood, custom_text, `is_active`, `last_bio` | `bio_state` | **Yes** — `ProfileEngine.updater` reads `is_active`/tokens from DB each minute | none |
| Username automation | template, mood, custom_text, `is_active`, `last_name` | `username_state` | **Yes** — same engine path | none |
| Profile scheduler runtime | active-engine flags, running task | in-memory (`profile/scheduler.py`) | No — restarted on boot from `BIO_UPDATE_ENABLED` / `USERNAME_UPDATE_ENABLED` + DB `is_active` rows | none |
| Panel / global settings | 12 typed settings | `panel_settings` (4 applied columns + `dashboard_font`; 10 columns missing §19.3) | **Yes** — settings must survive restart | §19.3 |
| Glass UI / dashboard font | selected font key | `panel_settings.dashboard_font` (CHECK on 23 keys) | **Yes** — persisted; key-surface mismatch §19.16 | §19.16 |
| Ghost Seen retention window | `ghost_seen_retention_seconds` | `panel_settings` column (migration pending) | **Yes** (intended) — but no consumer exists | §19.13 |
| Heartbeat stall threshold | `update_stale_seconds` | `panel_settings` column (applied) | No consumer | §19.14 |
| Ghost Seen allow-list | per-chat opt-in | `bot_settings.ghost_seen_allowed_chats` (JSON) | **Yes** — privacy permissions must survive restart | §19.4, §19.12 |
| Ghost Seen transient state | selections, reply input, AI candidate, page/query | in-memory | **No** — transient by design; never persisted | none |
| Ghost Seen registry rows | per-chat last-preview/unread metadata | `ghost_chats` (unused) | Only if the registry feature is built (retention setting implies intent) | §19.12, §24 |
| AI configuration | provider, model, temperature, max_tokens, system_prompt, history_budget, is_configured, triggers, last-request stats | `ai_config` | **Yes** — restored on boot | §19.1 (trigger columns missing) |
| AI sessions / messages / tool history | conversation history + audit | `ai_sessions`, `ai_messages`, `ai_tool_history` | **Yes** (per design) — written by `ai/persistence.py` | §19.5 (tool_calls col), §19.6 (result_data) |
| AI memories | short/long/permanent tiers | `ai_memories` (Supabase repo wired; no automatic writer) | **Yes** (per design) — opt-in writes only | none |
| AI usage + provider stats | per-request usage, per-provider aggregates | `ai_usage`, `ai_provider_stats` (repos wired) | **Yes** (per design) — tables not migrated | §19.8 |
| AI preferences | personality / behavior | in-memory only | **Yes** (per design) — no producer, no migration | §19.17 |
| AI reply context map | Telegram msg-id → full AI content | in-memory LRU (`reply_resolver.py`) | Optional enhancement (cross-restart reply resolution) | §19.10 |
| Structured logs | activity log | `bot_logs` | **Yes** (bounded by `log_retention_days`) | none |
| Runtime health telemetry | heartbeat/stats/diagnostics | in-memory (`health.py`, `diagnostics.py`) | No — by design | none |

Every "Yes" row is backed by a concrete source requirement; every
"No" row is transient UI/runtime state that must NOT be persisted.

---

## 24. Hermes Integration Boundary & Corrected Architecture

> **Scope note:** The supplied Hermes Runtime architecture document is
> NOT present in this repository (zero references to "hermes" exist in
> the tree). Hermes source was NOT inspected — nothing below claims it
> was. This section defines the boundary using the actual Telegram Self
> Bot source as the sole authority, and treats Hermes-owned persistence
> as a contract to be agreed with the Hermes side, not implemented here.

### A. Existing database architecture

See §1–§21. Summary: 13 live/specified tables (`saved_items`,
`bio_state`, `username_state`, `bot_logs`, `panel_settings`,
`ai_config`, `ai_sessions`, `ai_messages`, `ai_memories`,
`ai_tool_history`, `ai_provider_stats`, `ai_usage`, `ai_preferences`)
plus `bot_settings` (live, §19.4) and `ghost_chats` (orphaned,
§19.12). All access via the Supabase service-role key; SELECT-only RLS;
in-memory fallback everywhere.

### B. Existing Self Bot persistent state NOT correctly in the database

1. **Ghost Seen allow-list** — persisted, but in a legacy key-value
   table (`bot_settings`) with no owner scoping and a JSON blob
   (§19.4). Corrected representation: `ghost_chats` rows with
   `owner_id` + `allowed` (§22.2).
2. **Ghost Seen registry metadata** (`display_name`, `last_preview`,
   `last_message_at`, `unread_count`) — table exists, never written.
   Either write it (registry/retention feature) or keep it dormant;
   the retention setting (§19.13) implies the feature is intended.
3. **AI usage / provider stats / preferences** — implemented in code,
   no applied migrations (§19.8, §19.17).
4. **Settings columns without consumers** — `ghost_seen_retention_seconds`
   (§19.13), `update_stale_seconds` (§19.14).

### C. Proposed new persistent state (all additive; each justified by source)

| Proposal | Source requirement | Table / column |
|---|---|---|
| `ghost_chats.owner_id` | owner scoping — every other table carries `owner_id` | new column |
| `ghost_chats.allowed` | replaces `bot_settings.ghost_seen_allowed_chats` JSON blob; the allow-list is per-chat relational state | new column |
| `ai_messages.tool_calls` | `MessageRecord` defines it; migration missing | new column (§19.5) |
| `ai_config.trigger_en/fa` | `config_store` reads/writes them; migration missing | new columns (§19.1) |
| `ai_usage`, `ai_provider_stats`, `ai_preferences` tables | Supabase repositories are wired; tables missing | create (§19.8) |
| `panel_settings` 10 missing columns | `settings_service` reads/writes them | add columns (§19.3) |

Nothing else is proposed. Specifically NOT proposed: per-message Ghost
PV storage, Telegram message content storage, execution/action logs
beyond `ai_tool_history`, provider credentials, session strings.

### D. Hermes-owned state (external contract — not implemented here)

Per the supplied architecture reference, Hermes may own: proposals,
durable task state, reasoning history, learning artifacts. None of
this exists in this repository and none is implemented here. Hermes
must NEVER own Telegram session/credentials or become the executor of
Telegram operations.

### E. Self-Bot-owned state

Everything in §A–§C: Telegram feature state, per-chat permissions,
save/profile/settings/AI configuration, execution history, audit logs.
The Self Bot is the sole authority on what actually happened on
Telegram (send/deliver results, message IDs, timestamps).

### F. Cross-system references / synchronization

- Join keys are **Telegram-native identifiers**: `owner_id` (bigint,
  Telegram user ID), `chat_id` (bigint), `message_id` (bigint).
- Hermes may reference Self Bot rows by these IDs; the Self Bot is
  authoritative for them. Hermes proposal IDs may reference back into
  Hermes-owned tables only.
- No bidirectional FK constraints; logical references only (existing
  convention).
- No synchronization daemon is proposed. If Hermes needs proposal
  state shared with the Self Bot, the Self Bot consumes it as read-only
  input and validates before any Telegram execution.

### G. Security boundaries

1. The Self Bot remains the **sole Telegram Execution Authority**. The
   database stores durable state and history only — it never executes
   Telegram actions, calls providers, or contains business logic.
2. Hermes/AI may reason, propose, classify, and return candidate
   actions, but cannot directly execute arbitrary Telegram operations.
3. **No credentials in the database**: no `SESSION_STRING`, `API_HASH`,
   provider API keys, `BOT_TOKEN`. AI/Telegram credentials stay in
   environment variables only.
   *Amended by §29 (API Credential Vault, PART 1):* provider API keys may
   additionally live in **Supabase Vault**, and the database then holds only
   non-secret credential METADATA (`api_credentials`). No table anywhere in
   this database stores a raw API key, and the column-level rule is
   unchanged.
4. **No AI SQL execution**: no shell executor, no arbitrary SQL
   executor, no Telegram method executor are added anywhere.
5. `is_owner` remains the single permission gate for every handler;
   the DB is never a trust boundary for Telegram actions.

### H. RLS / ownership

- Single-tenant self-bot: every table keeps exactly one SELECT policy
  for `anon, authenticated` USING `(true)`; all writes go through the
  service-role key (bypasses RLS).
- `owner_id` must exist on every table that holds owner-scoped data.
  `ghost_chats` gains it (§22.2); `bot_settings` is migrated away from;
  `panel_settings` is a global singleton (no `owner_id`, by design).
- No anon/authenticated write policies anywhere.

### I. Migration strategy

Follow §21 rules: doc-first, one logical change per migration,
idempotent (`IF NOT EXISTS`), RLS SELECT-only, log in §20. Priority
order: (1) §19.1 trigger columns + §19.3 panel_settings columns;
(2) `ai_usage` + `ai_provider_stats` (+ decide `ai_preferences`);
(3) `ghost_chats` correction + `bot_settings` drop (§20 items 8–9);
(4) §19.5/§19.6 column additions; (5) §19.7 dead-column cleanup.
Every migration is additive or explicitly documented destructive
(§19.7, §20 item 9) — never a surprise drop.

### J. Open decisions / unknowns

1. **`ai_preferences`**: wire fully (migration + producer + UI) or
   remove until a producer exists (§19.17).
2. **Ghost Seen registry**: is the dormant `ghost_chats` preview/unread
   metadata feature (and its retention setting) going to be built? If
   yes, the retention consumer (§19.13) is required; if no, drop the
   setting and keep the table as permission-only.
3. **`GHOST_SEEN_DESTINATION_CHAT_ID/_NAME`**: build the notification
   flow or remove (§19.15).
4. **`update_stale_seconds`**: wire into the heartbeat or drop (§19.14).
5. **Dashboard font surface**: align frontend options with the backend
   23-key allow-list (§19.16).
6. **`ai_sessions` PK** promotion to `session_id` (§19.19).
7. **`ai_messages.telegram_msg_id`** cross-restart reply resolution
   (§19.10) — optional enhancement, not required.
8. **Hermes-side schema** — cannot be verified from this repository;
   must be agreed with the Hermes repository owner.

### Per-table rationale for the corrected/proposed objects

Every column in §22.2 and §C answers "what actual source-code
requirement caused this field to exist?"; anything without a source
requirement is marked **REQUIRES SOURCE VERIFICATION** rather than
invented. The only schema object in this revision that is not yet
backed by a live writer is `ghost_chats.last_preview` / `unread_count`
— they are preserved (existing columns, no reason to drop) and flagged
under decision J.2.

---

## 25. Font System Persistence (current, verified)

### 25.1 What the font system actually is

The Glass UI font system is a **code-defined enumerated transform
registry**, not a database of font definitions:

- **Definitions:** `backend/helper/font_style.py` — `FontDef(key, label,
  convert, has_digit_glyphs)`; one authoritative allow-list `FONT_KEYS`
  (23 keys: `default`, `serif_bold`, `serif_italic`, `serif_bold_italic`,
  `sans`, `sans_bold`, `sans_italic`, `sans_bold_italic`, `script`,
  `script_bold`, `fraktur`, `fraktur_bold`, `double_struck`, `mono`,
  `small_caps`, `circled`, `circled_dark`, `fullwidth`, `parenthesized`,
  `underline`, `strikethrough`, `overline`, `wavy_underline`).
- **Identifier/slug:** the `key` string (e.g. `sans_bold`) IS the stable
  slug; there is no separate slug field.
- **Display name:** `FontDef.label` (e.g. "Sans Bold"), rendered
  self-demonstrating via `font_option_label()`.
- **Font definition:** a deterministic per-character Unicode transform;
  no external font resources, no CSS, no free-text input.
- **Enabled state:** every registry entry is always available; there is
  no per-font enable/disable state in the source.
- **Default selection:** the `default` key; `normalize_font_key()`
  falls back to it for any invalid key.
- **Ordering:** registry order (default first) — `FONT_KEYS` tuple order.
- **Metadata / versioning:** none. No version field, no custom fonts, no
  per-owner groupings.

### 25.2 What persists

Only ONE value is user-selected durable state: the active font key.

| Storage | Column / type | Constraint | Consumer |
|---|---|---|---|
| `panel_settings.dashboard_font` | `text NOT NULL DEFAULT 'default'` | CHECK against the 23-key allow-list (migration `20260823120000`) | Glass UI panel rendering (`helper/panel_render.py`), Bio/Username profile rendering (`profile/engine.py`), web dashboard (`src/App.tsx`) |

Read/write path: `settings_service.dashboard_font()` /
`set_dashboard_font()` → `panel_settings_repository` (write-through
cache). The dashboard maps the key to a fixed CSS stack; the Glass UI
maps it to the letter transform.

**Verified restart lifecycle (persistence audit):** selection
(`font_set` panel action or `PATCH /api/settings`) → validator
(`value in FONT_KEYS`) → `panel_settings_repository.update_field(
'dashboard_font', key)` (UPDATE … WHERE key = 'global') → on restart
`RuntimeSupervisor.start()` calls `settings_service.load_all()`
(`backend/runtime/supervisor.py`), hydrating the cache from
`panel_settings` before panels render → `dashboard_font()` serves the
restored key to `panel_render`, `profile/engine`, and
`GET /api/settings`. `load_all()` never writes defaults back to the DB,
so a restart cannot overwrite the persisted value. Caveat (pre-existing,
§19.3): if the DB write fails (e.g. migration not yet applied live),
`set_setting` degrades to the in-memory cache and still reports success
— the value sticks for the session only. Regression:
`tests/test_42_dashboard_font.py` (write-reaches-DB + restart restore +
startup-hydration guard).

### 25.3 Gaps / decisions

- **Frontend key-surface mismatch** — §19.16 (dashboard hardcodes 4
  keys; `system`/`serif` are rejected by backend validation).
- **PROPOSED:** none. No font table is needed; font definitions are
  code, and the single selected key is already a typed column. Adding a
  `fonts` table (slug/definition/order/version) would duplicate
  `font_style.py` with no source requirement.

---

## 26. Current vs Proposed Status Matrix

Legend: **CURRENT (verified)** = read/written by live code today.
**PROPOSED** = specified here; no migration applied and/or no live
producer yet.

| Domain / table | Status | Live writer/reader | Migration applied? |
|---|---|---|---|
| `saved_items` | CURRENT | `db/client.py`, `save_service`, retrieve/delete/discover/organize, `web/app` | Yes |
| `bio_state` | CURRENT | `db/client.py`, `profile/engine.py` (Bio) | Yes |
| `username_state` | CURRENT | `db/client.py`, `profile/engine.py` (Username) | Yes |
| `bot_logs` | CURRENT | `db/client.py::log` | Yes |
| `panel_settings` | CURRENT (12 accessors; 10 columns missing) | `settings_service` + `panel_settings_repository` | Partial — §19.3 |
| `ai_config` | CURRENT (trigger columns missing) | `config_store` | Partial — §19.1 |
| `ai_sessions` | CURRENT | `ai/persistence.py` | Yes |
| `ai_messages` | CURRENT (tool_calls col missing) | `ai/persistence.py` | Partial — §19.5 |
| `ai_memories` | CURRENT (repo wired; no auto writer) | `SupabaseMemoryRepository` via Engine | Yes |
| `ai_tool_history` | CURRENT | `ai/persistence.py`, `ai/tools/executor.py` | Yes |
| `bot_settings` | CURRENT (allow-list) | `ghost_seen_v2` | Yes |
| `ghost_chats` | **PROPOSED (corrected)** | no live writer today | Table exists; §22.2 correction not applied |
| `ai_usage` | **PROPOSED** | `SupabaseUsageRepository` + `usage_recorder` (wired; fails silently without table) | No |
| `ai_provider_stats` | **PROPOSED** | `SupabaseProviderStatsRepository` (wired) | No |
| `ai_preferences` | **PROPOSED** | interface + in-memory only; no producer | No |
| Ghost Seen allow-list | CURRENT (via `bot_settings`) → PROPOSED migration to `ghost_chats.allowed` | `ghost_seen_v2` | Backfill = §20 item 8 |
| Ghost Seen transient state | Intentionally NOT persisted (CURRENT design decision) | in-memory | n/a |
| Ghost Seen watermark / exclusions / delays | Do NOT exist in source; deliberately NOT added (would be REQUIRES SOURCE VERIFICATION) | — | n/a |
| Font definitions | CURRENT, code-only (`font_style.py`) | — | n/a |
| Font selection | CURRENT | `panel_settings.dashboard_font` | Yes |
| `ai_config.last_request_at` / `last_latency_ms` | CURRENT (resolved §19.2) | `config_store` | Yes (columns) |
| Scheduler active flags | CURRENT (runtime) + durable per-engine `is_active` in `bio_state` / `username_state` | `profile/scheduler.py` + engines | Yes |

---

---

## 29. API Credential Vault (PART 1)

> **Status: implemented in the repository; NOTHING was executed against
> Supabase.** The migration file exists, the application-side resolution
> boundary exists and is covered by tests, and the complete manual SQL is in
> §29.10–§29.11 below. The coding agent created **no table, no function, no
> extension, no schema change and no Vault secret**, and did not connect to
> Supabase. Every object in this section is pending an owner action.

**Migration file:** `supabase/migrations/20260919000001_create_api_credential_vault.sql`
— the executable statements in §29.10 are a byte-identical copy of that file's
statements.

### 29.1 Purpose — one generic secret architecture

Before this section the application had exactly one credential per provider, held
in a deployment environment variable. PART 1 adds the second source without
creating a second architecture:

```
ENV credentials            (unchanged — still the first credential)
        +
Supabase Vault credentials (new — additional, owner-managed)
        ↓
generic credential source         backend/ai/credential_source.py
        ↓
bounded credential pool           backend/services/stt_credential_pool.py (STT consumer)
        ↓
provider execution                the existing adapter for the current attempt
```

The design is deliberately **provider-agnostic**: the table and the RPC are keyed
by a free-form `provider` token, so today's Speech-to-Text providers (`gemini`,
`groq`, `speechmatics`) and a future Text-to-Speech provider (`openai`) reuse the
same table, the same RPC and the same application boundary. No `tts_*` secret
architecture is introduced anywhere.

### 29.2 `api_credentials` — credential metadata (contains NO secret)

| Column | PostgreSQL type | Nullability | Default | Notes |
|---|---|---|---|---|
| `credential_id` | `text` | NOT NULL | — | **PRIMARY KEY.** Stable, non-secret identifier, `^[A-Za-z0-9._-]{1,64}$` — the same alphabet and length the application refuses anything outside of. This is the only credential name that may appear in a log line. |
| `provider` | `text` | NOT NULL | — | Provider token shared with the runtime registry (`gemini`, `groq`, `speechmatics`, `openai`, …), `^[a-z0-9][a-z0-9._-]{0,31}$`. |
| `label` | `text` | NOT NULL | — | Non-secret display label for the owner; non-blank, ≤ 80 characters. |
| `owner_id` | `bigint` | NOT NULL | — | Owner scoping, consistent with every other owner-scoped table. `> 0`. |
| `enabled` | `boolean` | NOT NULL | `true` | A disabled credential is never returned by the RPC. |
| `priority` | `integer` | NOT NULL | `0` | Deterministic ordering; a **lower** value is tried first. `BETWEEN 0 AND 1000000`. |
| `vault_secret_id` | `uuid` | NOT NULL | — | **Foreign key** → `vault.secrets(id)` `ON DELETE CASCADE`. The only secret-bearing reference in the row. |
| `created_at` | `timestamptz` | NOT NULL | `now()` | Also the ordering tie-breaker after `priority`. |
| `updated_at` | `timestamptz` | NOT NULL | `now()` | Maintained by the writer, exactly like `ai_tasks` / `ai_task_occurrences` (this project has no `updated_at` trigger anywhere). |

**Does it contain secrets? NO.** There is no column of any name that can hold an
API key, token, password or session string — the schema has no `secret`,
`api_key`, `token`, `credential_secret` or equivalent column, and the test suite
asserts that the declared column list contains none. The raw key exists **only**
inside Supabase Vault; this table stores a *reference* to it.

Constraints

| Constraint | Kind | Definition |
|---|---|---|
| `api_credentials_pkey` | PRIMARY KEY | `(credential_id)` |
| `api_credentials_vault_secret_id_fkey` | FOREIGN KEY | `(vault_secret_id) REFERENCES vault.secrets(id) ON DELETE CASCADE` |
| `api_credentials_id_format` | CHECK | `credential_id ~ '^[A-Za-z0-9._-]{1,64}$'` |
| `api_credentials_provider_format` | CHECK | `provider ~ '^[a-z0-9][a-z0-9._-]{0,31}$'` |
| `api_credentials_label_not_blank` | CHECK | `length(btrim(label)) > 0 AND length(label) <= 80` |
| `api_credentials_owner_positive` | CHECK | `owner_id > 0` |
| `api_credentials_priority_range` | CHECK | `priority BETWEEN 0 AND 1000000` |

Indexes

| Index | Kind | Definition | Purpose |
|---|---|---|---|
| `api_credentials_pkey` | UNIQUE | `(credential_id)` | identity |
| `uq_api_credentials_vault_secret` | UNIQUE | `(vault_secret_id)` | **one Vault secret maps to exactly one credential.** A duplicate mapping would silently make rotation a no-op (the pool would "rotate" between two names for the same key), so it is refused at the schema level. |
| `idx_api_credentials_provider_order` | btree | `(provider, priority, created_at, credential_id)` | backs the single-provider resolution read |
| `idx_api_credentials_owner` | btree | `(owner_id, provider)` | backs the owner-scoped read |

RLS and grants

* `ALTER TABLE public.api_credentials ENABLE ROW LEVEL SECURITY;`
* **Policies: NONE.** Not one policy is created for `anon` or `authenticated`,
  and `REVOKE ALL … FROM PUBLIC, anon, authenticated` removes the default
  privileges. This is *deliberately different* from every other table in this
  document (which grant `anon` SELECT for the read-only dashboard): credential
  bookkeeping has no dashboard consumer, so it is exposed to nobody.
* `GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.api_credentials TO service_role;`
  — the backend's service-role client is the only writer/reader. (The service
  role also bypasses RLS, as for every other table.)

### 29.3 `api_credential_pool(p_provider text, p_owner_id bigint DEFAULT NULL)` — the ONE resolution boundary

| Property | Value |
|---|---|
| Args | `p_provider text` (required) · `p_owner_id bigint` (optional, default `NULL`) |
| Returns | `TABLE(credential_id text, secret text, priority integer, enabled boolean)` |
| Language / volatility | `sql`, `STABLE` |
| Security | `SECURITY DEFINER`, `SET search_path = ''` (hardened; the body is fully qualified) |
| Ownership | `OWNER TO postgres` — the role that owns `vault.decrypted_secrets` |
| Execute grants | `REVOKE ALL … FROM PUBLIC, anon, authenticated` then `GRANT EXECUTE … TO service_role` |
| Row ceiling | `LIMIT 8` (a fixed database-side bound; the application applies its own ceiling of 4 — `credential_source.MAX_CREDENTIALS_PER_PROVIDER`) |
| Ordering | `ORDER BY priority ASC, created_at ASC, credential_id ASC` — fully deterministic, no ties |
| Filters | `provider = p_provider AND enabled AND (p_owner_id IS NULL OR owner_id = p_owner_id)` |

It joins `api_credentials` to `vault.decrypted_secrets` on
`decrypted_secrets.id = api_credentials.vault_secret_id` and returns
`decrypted_secret AS secret`. **The application never reads `vault.*` directly**;
the SECURITY DEFINER function reads it on the backend's behalf, which keeps the
Vault schema, naming and access policy entirely the owner's.

The parameter shape is **the M2.4 contract unchanged**: the runtime calls
`db.rpc("api_credential_pool", {"p_provider": provider})`, and because
`p_owner_id` carries a default it is simply omitted. Owner scoping is available
at the database boundary for the phase that needs it (the credential-management
surface), without a second function.

What it deliberately is **not**: not a generic SQL endpoint, not a
`SECURITY INVOKER` function, not variadic, not a table-returning view of every
provider, and not callable by `anon`/`authenticated`.

### 29.4 `stt_credential_pool(p_provider text, p_owner_id bigint DEFAULT NULL)` — deprecated compatibility alias

The M2.4 documentation named this function, so the migration keeps it as a thin
alias (`SELECT * FROM public.api_credential_pool(p_provider, p_owner_id)`) with
the same signature, the same security posture, the same grants and the same
return shape. The application calls the generic name; the alias exists so an
installation that implemented the earlier name keeps working. It is marked
`@deprecated` in a `COMMENT ON FUNCTION` and is scheduled for removal in a later
phase once no deployment relies on it.

### 29.5 Supabase Vault architecture and the secret mapping

```
Supabase Vault                                  public schema
┌──────────────────────────┐                    ┌───────────────────────────────┐
│ vault.secrets (encrypted)│◄── FK (CASCADE) ───│ api_credentials.vault_secret_id│
└──────────┬───────────────┘                    │ provider / label / enabled /   │
           │ decrypted on read                  │ priority / owner_id            │
           ▼                                    └───────────────────────────────┘
   vault.decrypted_secrets  ──► api_credential_pool() ──► service role ──► credential_source
```

* The extension is `supabase_vault`, installed into the `vault` schema by
  `CREATE EXTENSION IF NOT EXISTS supabase_vault WITH SCHEMA vault;`. On a
  Supabase project where Vault has not been enabled yet, the owner may need to
  enable it from the dashboard (Database → Extensions) if the SQL role lacks the
  privilege; the statement is otherwise idempotent.
* **Mapping is deterministic and one-to-one:** one `vault.secrets` row → one
  `api_credentials` row (enforced by `uq_api_credentials_vault_secret`). The
  application never guesses a secret's name: it reads the row's
  `vault_secret_id`.
* The application knows nothing about Vault internals beyond the RPC's return
  shape — no table name, no key id, no nonce, no decryption key is referenced in
  Python.

### 29.6 Security model

| Boundary | Rule |
|---|---|
| Raw secret at rest | **only** inside `vault.secrets` (encrypted by Vault) |
| Raw secret in `public` | **never** — `api_credentials` has no secret column |
| Raw secret in application memory | only inside `credential_source.CredentialRecord.secret` for the attempt that needs it |
| Raw secret in logs | **never** — the only credential identifier logged is `credential_id` (a non-secret label the owner chose) |
| Raw secret to the AI layer / Telegram | **never** — the credential modules import nothing from `backend.bot` or Telethon, and no key is ever formatted into a message |
| Table access | service role only; RLS enabled with zero policies; `anon`/`authenticated` revoked |
| Function access | `EXECUTE` granted to `service_role` only; `PUBLIC`/`anon`/`authenticated` revoked |
| `search_path` hijack | impossible — `SECURITY DEFINER` + `SET search_path = ''` + fully qualified body |
| Owner scoping | `owner_id NOT NULL` plus the optional `p_owner_id` filter on the resolver |
| Arbitrary SQL | no dynamic SQL, no `EXECUTE`, no user-supplied identifier |

### 29.7 ENV compatibility (unchanged)

The deployment environment remains the **first** credential for every provider,
resolved through each provider adapter's *own declared* variable names
(`AI_GEMINI_API_KEY` → `GEMINI_API_KEY`, `AI_GROQ_API_KEY` → `GROQ_API_KEY`,
`AI_SPEECHMATICS_API_KEY`). There are no numbered `*_KEY_1`/`*_KEY_2` variables,
no environment scanning, and no automatic migration of an ENV secret into Vault.
Ordering is `(priority, source order)`, and the ENV credential is
`priority=0, order_index=0`, so an installation that configures no pool behaves
exactly as it did before this phase.

### 29.8 Failure behaviour and orphan handling

| Situation | Behaviour |
|---|---|
| Migration not applied / RPC missing | PostgREST answers with an error; the runtime logs one bounded warning (`STT_CREDENTIAL_VAULT_READ_FAILED provider=… error=…`) and keeps the ENV credential. Media processing is unaffected. |
| RPC refuses (permission / role) | same as above |
| Read exceeds the 5 s ceiling | `run_sync_db` times out, same bounded warning, ENV credential retained |
| Response is not a list, or a row is not an object | that row/response contributes nothing |
| Row has no `credential_id`, an id outside `[A-Za-z0-9._-]{1,64}`, or an empty `secret` | row contributes nothing (refused, never sanitized) |
| Row has `enabled: false` | row contributes nothing |
| Vault secret deleted | `ON DELETE CASCADE` removes the metadata row, so the credential disappears from the pool atomically and the table can never advertise an unresolvable credential |
| Metadata row deleted / disabled | the credential disappears from the pool; the Vault secret may remain as an **inert orphan** — the RPC only ever returns secrets anchored by a metadata row, so an orphan is never returned and never logged. Removing the orphan is an owner action in the Vault UI/SQL. |
| Credential is revoked but still configured | the runtime's credential health (process-local, M2.4) cools it down; `credential_source.mark_stale()` refreshes the snapshot at the next settings apply |

### 29.9 Application consumers

| Consumer | Role |
|---|---|
| `backend/ai/credential_source.py` | the **only** module that calls the RPC (`VAULT_RPC = "api_credential_pool"`; `LEGACY_VAULT_RPC` names the alias for reference). Resolves the ENV credential, merges the Vault rows, validates them, orders them and caches the bounded snapshot. |
| `backend/services/stt_credential_pool.py` | the STT consumer: rotation order, credential health/cooldown, credential-vs-provider failure classification. **Compatibility is unchanged** — it calls the same `credential_source.load()`, so switching the RPC name changed no STT behaviour. |
| a future TTS consumer | the same `credential_source.load(provider, env_var_names)` call with `provider="openai"` — no new module, table or RPC |

### 29.10 Manual Supabase SQL — NOT EXECUTED BY AI

Apply this in the Supabase SQL Editor **as `postgres`**. It is idempotent
(`IF NOT EXISTS` / `CREATE OR REPLACE`) but has not been run by the coding agent,
and no Vault secret is created by it.

```sql
-- ============================================================================
-- 1. Supabase Vault — the ONLY place a raw secret may live
-- ============================================================================

CREATE EXTENSION IF NOT EXISTS supabase_vault WITH SCHEMA vault;

-- ============================================================================
-- 2. api_credentials — credential METADATA. No secret column exists here.
-- ============================================================================

CREATE TABLE IF NOT EXISTS public.api_credentials (
    credential_id    text        PRIMARY KEY,
    provider         text        NOT NULL,
    label            text        NOT NULL,
    owner_id         bigint      NOT NULL,
    enabled          boolean     NOT NULL DEFAULT true,
    priority         integer     NOT NULL DEFAULT 0,
    vault_secret_id  uuid        NOT NULL
                                 REFERENCES vault.secrets(id) ON DELETE CASCADE,
    created_at       timestamptz NOT NULL DEFAULT now(),
    updated_at       timestamptz NOT NULL DEFAULT now(),
    CONSTRAINT api_credentials_id_format
        CHECK (credential_id ~ '^[A-Za-z0-9._-]{1,64}$'),
    CONSTRAINT api_credentials_provider_format
        CHECK (provider ~ '^[a-z0-9][a-z0-9._-]{0,31}$'),
    CONSTRAINT api_credentials_label_not_blank
        CHECK (length(btrim(label)) > 0 AND length(label) <= 80),
    CONSTRAINT api_credentials_owner_positive
        CHECK (owner_id > 0),
    CONSTRAINT api_credentials_priority_range
        CHECK (priority BETWEEN 0 AND 1000000)
);

CREATE INDEX IF NOT EXISTS idx_api_credentials_provider_order
    ON public.api_credentials (provider, priority, created_at, credential_id);
CREATE INDEX IF NOT EXISTS idx_api_credentials_owner
    ON public.api_credentials (owner_id, provider);
CREATE UNIQUE INDEX IF NOT EXISTS uq_api_credentials_vault_secret
    ON public.api_credentials (vault_secret_id);

ALTER TABLE public.api_credentials ENABLE ROW LEVEL SECURITY;

REVOKE ALL ON TABLE public.api_credentials FROM PUBLIC, anon, authenticated;
GRANT SELECT, INSERT, UPDATE, DELETE ON TABLE public.api_credentials TO service_role;

COMMENT ON TABLE public.api_credentials IS
    'API credential METADATA for every provider. Stores NO secret: the key lives in Supabase Vault and is referenced by vault_secret_id. Service-role only.';
COMMENT ON COLUMN public.api_credentials.credential_id IS
    'Stable, non-secret identifier (1-64 chars, [A-Za-z0-9._-]). This is the only credential name that may appear in a log line.';
COMMENT ON COLUMN public.api_credentials.provider IS
    'Provider token shared with the runtime registry (e.g. gemini, groq, speechmatics, openai).';
COMMENT ON COLUMN public.api_credentials.label IS
    'Non-secret human label for the owner.';
COMMENT ON COLUMN public.api_credentials.priority IS
    'Deterministic ordering; a LOWER value is tried first. Ties fall back to created_at then credential_id.';
COMMENT ON COLUMN public.api_credentials.vault_secret_id IS
    'Reference to vault.secrets(id). The raw key exists only in Vault; deleting the secret cascades this metadata row.';

-- ============================================================================
-- 3. api_credential_pool — the ONE resolution boundary (provider-generic)
-- ============================================================================

CREATE OR REPLACE FUNCTION public.api_credential_pool(
    p_provider text,
    p_owner_id bigint DEFAULT NULL
)
RETURNS TABLE (
    credential_id text,
    secret        text,
    priority      integer,
    enabled       boolean
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $$
    SELECT c.credential_id,
           d.decrypted_secret AS secret,
           c.priority,
           c.enabled
      FROM public.api_credentials AS c
      JOIN vault.decrypted_secrets AS d
        ON d.id = c.vault_secret_id
     WHERE c.provider = p_provider
       AND c.enabled
       AND (p_owner_id IS NULL OR c.owner_id = p_owner_id)
     ORDER BY c.priority ASC, c.created_at ASC, c.credential_id ASC
     LIMIT 8;
$$;

ALTER FUNCTION public.api_credential_pool(text, bigint) OWNER TO postgres;

REVOKE ALL ON FUNCTION public.api_credential_pool(text, bigint)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.api_credential_pool(text, bigint) TO service_role;

COMMENT ON FUNCTION public.api_credential_pool(text, bigint) IS
    'Returns the ordered, ENABLED credentials of ONE provider with each secret decrypted from Supabase Vault. At most 8 rows. Callable by service_role only.';

-- ============================================================================
-- 4. stt_credential_pool — deprecated alias of the same contract
-- ============================================================================

CREATE OR REPLACE FUNCTION public.stt_credential_pool(
    p_provider text,
    p_owner_id bigint DEFAULT NULL
)
RETURNS TABLE (
    credential_id text,
    secret        text,
    priority      integer,
    enabled       boolean
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $$
    SELECT * FROM public.api_credential_pool(p_provider, p_owner_id);
$$;

ALTER FUNCTION public.stt_credential_pool(text, bigint) OWNER TO postgres;

REVOKE ALL ON FUNCTION public.stt_credential_pool(text, bigint)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.stt_credential_pool(text, bigint) TO service_role;

COMMENT ON FUNCTION public.stt_credential_pool(text, bigint) IS
    'Deprecated compatibility alias of api_credential_pool. Use api_credential_pool.';

-- ============================================================================
-- 5. PostgREST schema cache
-- ============================================================================

NOTIFY pgrst, 'reload schema';
```

**Populating the pool (owner action, run once per credential).** The secret goes
into Vault; the table receives only its reference. Replace the placeholders — no
real key belongs in any file in this repository.

```sql
-- 1. Store the key in Vault (Vault encrypts it; this is the ONLY secret write).
SELECT vault.create_secret('<THE_API_KEY>', 'speechmatics-a', 'Speechmatics credential A');

-- 2. Map it into the pool. The metadata row holds no secret.
INSERT INTO public.api_credentials
    (credential_id, provider, label, owner_id, enabled, priority, vault_secret_id)
SELECT 'sm-a', 'speechmatics', 'Speechmatics A', <BOT_OWNER_ID>, true, 10, id
  FROM vault.secrets
 WHERE name = 'speechmatics-a'
ON CONFLICT (credential_id) DO NOTHING;

-- 3. Confirm what the runtime will resolve (never prints a secret here).
SELECT credential_id, provider, enabled, priority
  FROM public.api_credentials
 ORDER BY provider, priority, created_at;
```

### 29.11 Manual rollback SQL — NOT EXECUTED BY AI

Destructive: it removes the credential metadata and both functions. It does
**not** drop the shared `supabase_vault` extension and does **not** delete any
Vault secret — delete secrets explicitly in the Vault UI/SQL only when you are
certain nothing else uses them, because Vault holds no reference back to this
schema.

```sql
-- 1. Resolvers first (they depend on the table).
DROP FUNCTION IF EXISTS public.stt_credential_pool(text, bigint);
DROP FUNCTION IF EXISTS public.api_credential_pool(text, bigint);

-- 2. Then the metadata table (and, with it, every credential definition).
DROP TABLE IF EXISTS public.api_credentials;

-- 3. NOT dropped by this rollback, on purpose:
--    * EXTENSION supabase_vault — shared by the project, may serve other secrets;
--    * rows in vault.secrets     — owned by Vault; remove them explicitly:
--         SELECT id, name FROM vault.secrets ORDER BY name;
--         SELECT vault.delete_secret('<secret-id>');
--    * the ENV credentials on Render — untouched by design.
NOTIFY pgrst, 'reload schema';
```

### 29.13 API Credential Vault (PART 2) — owner-scoped credential management

> **Status: implemented in the repository; NOTHING was executed against
> Supabase.** Five SECURITY DEFINER functions are added by
> `supabase/migrations/20260919000002_credential_vault_management.sql`; the complete
> manual SQL is §29.14 and the reversal SQL is §29.15. The coding agent created **no
> function, no table, no column, no index, no policy, no schema change and no Vault
> secret**, deleted no key, and did not connect to Supabase.

**Migration file:** `supabase/migrations/20260919000002_credential_vault_management.sql`
— the executable statements in §29.14 are a byte-identical copy of that file.
**Depends on:** PART 1 (`20260919000001_create_api_credential_vault.sql`, §29.1–§29.12),
which must be applied first.

PART 1 made a credential *resolvable*; PART 2 makes it *manageable* from Telegram.
Neither half adds a second secret store:

```
Telegram owner
    ↓
AI → Media Analysis → API Credentials        backend/bot/handlers/ai_credentials.py
    ↓
credential management service                backend/services/credential_service.py
    ↓
the five functions of this section
    ├── public.api_credentials   (metadata only — still NO secret column)
    └── vault.secrets            (the raw key, written only through vault.create_secret)
    ↓
public.api_credential_pool  (PART 1, UNCHANGED)
    ↓
STT providers (gemini, groq, speechmatics)   ·   future TTS providers (openai)
```

#### 29.13.1 The five functions

| Function | Purpose | Touches a secret? | Return shape |
|---|---|---|---|
| `api_credential_list(p_owner_id bigint, p_provider text DEFAULT NULL)` | the owner's credential METADATA, optionally for one provider | **NO** — it never reads a secret column or view | 7-column metadata table, ≤ 64 rows |
| `api_credential_create(p_owner_id bigint, p_provider text, p_label text, p_secret text, p_priority integer DEFAULT 0, p_enabled boolean DEFAULT true)` | creates the Vault secret AND the metadata row referencing it | **WRITES** one (the argument is handed straight to `vault.create_secret`) | the metadata row |
| `api_credential_replace_secret(p_owner_id bigint, p_credential_id text, p_secret text)` | swaps in a NEW secret and removes the old one | **WRITES** one | the metadata row |
| `api_credential_update(p_owner_id bigint, p_credential_id text, p_label text DEFAULT NULL, p_enabled boolean DEFAULT NULL, p_priority integer DEFAULT NULL)` | metadata only: label / enabled / priority | **NO** — it cannot read or rotate a key | the metadata row |
| `api_credential_delete(p_owner_id bigint, p_credential_id text)` | removes the Vault secret and the metadata row | **DELETES** one (never returns it) | `boolean` |

All five live in `public`, are `CREATE OR REPLACE` (idempotent), return **metadata
only** — `credential_id, provider, label, enabled, priority, created_at,
updated_at` — and **no function can return a secret**: there is no return shape in
this migration that can hold one.

#### 29.13.2 Parameter and return contract

| Item | Value |
|---|---|
| `p_owner_id` | `bigint`, **required by every call**. `<= 0` raises `invalid_owner`. Every statement filters on it, so one owner can never read or modify another owner's credential. |
| `p_provider` | `text`, the free-form provider token shared with the runtime registry (`^[a-z0-9][a-z0-9._-](0, 31)$`, else `invalid_provider`). |
| `p_label` | `text`, non-blank and ≤ 80 characters (`invalid_label`). |
| `p_priority` | `integer`, `0 … 1000000` (`invalid_priority`); lower is tried first. |
| `p_enabled` | `boolean`; a disabled credential is never returned by `api_credential_pool`. |
| `p_credential_id` | `text`, `1 … 64` characters from `[A-Za-z0-9._-]` (`invalid_credential`); the same identity PART 1 declared. |
| `p_secret` | `text`, accepted **only** by create and replace: empty raises `empty_secret`, longer than 8192 characters raises `secret_too_long`. |
| Unknown credential | raises `credential_not_found` (except delete, which returns `false`). |
| Ordering (list) | `provider, priority, created_at, credential_id` — the same order `api_credential_pool` resolves in. |
| Row ceiling (list) | `LIMIT 64`. |

#### 29.13.3 Security model

| Concern | Value |
|---|---|
| Language / volatility | `api_credential_list` is `sql STABLE`; the four writers are `plpgsql VOLATILE`. |
| Security | **`SECURITY DEFINER`** on all five, so they read and write `vault.*` on the caller's behalf and the application never needs a privilege on the Vault schema. |
| `search_path` | **`SET search_path = ''`** on all five, with a fully qualified body — a caller cannot hijack resolution through the search path. |
| Ownership | `ALTER FUNCTION … OWNER TO postgres;` on all five. |
| Grants | `REVOKE ALL … FROM PUBLIC, anon, authenticated;` then `GRANT EXECUTE … TO service_role;` — `service_role` only, exactly like the PART 1 resolution function. |
| RLS | The PART 1 table already has RLS enabled with **no policy** and no grant to `anon`/`authenticated`; PART 2 does not alter it. Owner scoping is enforced inside each function by `p_owner_id`. |
| Dynamic SQL | **None.** No `EXECUTE format`, no string-built statement, no `SET LOCAL ROLE`: these are five fixed functions, not a query endpoint. |
| `usage` tokens | No function in this section calls `supabase_vault`, `usage` or any administrative helper. |

#### 29.13.4 Secret exposure boundary

| Where a raw secret may exist | Where it must NEVER appear |
|---|---|
| In `vault.secrets` (encrypted), written only by `vault.create_secret` from create/replace | `public.api_credentials` — it has no column that can hold one |
| In the argument and local variable of `api_credential_create` / `api_credential_replace_secret`, for the duration of that one call | any return shape, `COMMENT`, or `RAISE` message in this migration |
| In one in-flight request body inside the application (`credential_service.create_credential` / `replace_secret`) and one provider attempt | a log line, a Telegram message, callback data, `ai_config`, an error string returned to Telegram, or a test fixture |

None of the five functions reads a decrypted secret back out. `api_credential_list`
selects the metadata columns only, `api_credential_update` touches no secret at all,
and `api_credential_delete` removes the secret by id without ever selecting it.
There is deliberately **no "show key" operation** anywhere in the architecture.

#### 29.13.5 Deletion, replacement and orphan handling

| Operation | Order of operations | Guarantee |
|---|---|---|
| create | `vault.create_secret` → `INSERT` the metadata row (inside a nested `BEGIN … EXCEPTION` block) | if the metadata insert fails, the just-created secret is deleted and the error is re-raised: **a failed create leaves no unreferenced secret** |
| replace | read the mapping → `vault.create_secret` (new) → `UPDATE` the row to the new secret → `DELETE` the old secret | the old secret is removed **only after** the row points at the new one, because `vault_secret_id` is `ON DELETE CASCADE` and deleting it first would have cascaded the metadata row away; a failed `UPDATE` deletes the NEW secret instead |
| delete | read the mapping → `DELETE FROM vault.secrets` (the FK cascades the metadata row) → `DELETE` the metadata row explicitly | a success means **neither the secret nor the metadata remains**; both deletes happen in one transaction, so a refused secret removal aborts and is reported as a failure rather than a success |
| update | `UPDATE … SET label/enabled/priority, updated_at` | metadata only — it cannot orphan, rotate or disturb a secret |

Two honest exceptions, both documented rather than hidden:

* **A leftover old secret after a replace.** The final `DELETE` of the old secret is
  best-effort: if it fails, the swap still stands (the row points at the new secret
  and the runtime uses it) and an unreferenced row remains in `vault.secrets`. It is
  inert — nothing references it, nothing resolves it — and can be removed with
  `SELECT vault.delete_secret('<id>')`. Failing the whole swap instead would have
  left the caller unable to rotate a leaked key.
* **Uniqueness of the Vault name.** `vault.secrets.name` is UNIQUE, so create names
  its secret `api_credential:<credential_id>` and replace adds a random suffix to
  distinguish the new one from the one it is about to delete.

#### 29.13.6 Application consumers

| Layer | File | Responsibility |
|---|---|---|
| Telegram surface | `backend/bot/handlers/ai_credentials.py` | owner-only panels, actions and inputs; renders metadata and a bounded result; **no SQL, no database client, no secret-store call** |
| Management boundary | `backend/services/credential_service.py` | validation, the bounded reason vocabulary, the non-secret credential handle, the credential test, and the ONLY `db.rpc` calls to the five functions |
| Discovery | `stt_control_plane` + `tts_service` (through `credential_service.registered_providers()`) | decides which providers the panel may offer, so no imaginary provider can be created |
| Pool refresh | `backend/services/stt_credential_pool.prepare()` | after every successful change, the affected provider's pool is reloaded, so a new key is in effect on the very next request |

#### 29.13.7 Failure behaviour and ENV compatibility

* A management failure never raises into Telegram: `credential_service` maps every
  outcome to a bounded class (`store_not_configured`, `store_unavailable`,
  `invalid_*`, `empty_secret`, `secret_too_long`, `secret_shape`,
  `credential_not_found`, `rejected`, `failed`) and logs **the class and at most a
  code — never the database's message**, which is what keeps a refused statement
  from echoing anything sensitive.
* **Not configured is its own honest state:** an absent function (PostgREST
  `PGRST202` / PostgreSQL `42883`) or an unconfigured client reports
  `store_not_configured`, and the panel says so. The runtime is unaffected.
* **No new environment variable and no ENV reading here.** The managed store is an
  *additional* source: `backend/ai/credential_source.py` still resolves the
  provider's own declared variable first (PART 1, unchanged), and PART 2 neither
  reads nor writes any environment variable.
* **No automatic migration of ENV keys into Vault.** Adding a managed credential is
  an explicit owner action.

#### 29.13.8 STT compatibility and future TTS compatibility

The store is generic by the `provider` token, which is why PART 2 adds no
`tts_credential_pool.py`, no `stt_credentials.py` and no second secret architecture:

* **STT** keeps resolving through `api_credential_pool` → the M2.4 credential pool →
  the M2.3 provider fallback. A credential added, enabled, disabled or deleted from
  the panel is picked up by that existing path on the next request; no provider
  selection, model choice or fallback rule changes.
* **TTS** (`openai`) is listed by the same panel and managed through the same
  functions. It has no bounded credential test in this phase, so its TEST action is
  reported as not supported rather than approximated.

### 29.14 Manual Supabase SQL — NOT EXECUTED BY AI

The block below is `supabase/migrations/20260919000002_credential_vault_management.sql`
verbatim (header comment included). Apply it **as `postgres`** in the Supabase SQL
Editor, after PART 1. It creates functions only: no table, no column, no index, no
policy, no extension and no secret.

```sql
/*
# API Credential Vault — PART 2 (owner-scoped management)

PART 1 created the credential METADATA table and the ONE resolution boundary.
This migration adds the MANAGEMENT boundary the owner-facing Telegram surface
calls: create, replace the secret, update metadata, delete, and list.

    Telegram owner
        ↓
    AI → Media Analysis → API Credentials        backend/bot/handlers/ai_credentials.py
        ↓
    credential management service                backend/services/credential_service.py
        ↓
    the SECURITY DEFINER functions below
        ├── public.api_credentials   (metadata only — no secret column exists)
        └── vault.secrets            (the raw key, via the vault.* API)

WHAT THIS MIGRATION IS NOT

* It is NOT a plaintext key store and it does NOT change the PART 1 table: no
  column is added, no column can hold a secret, and `api_credentials` is left
  exactly as PART 1 created it.
* It is NOT a generic SQL execution endpoint: five functions, a fixed parameter
  list, a fixed return shape (metadata only — never a secret) and a fixed row
  ceiling on the one function that lists.
* It is NOT STT- or TTS-specific: every function takes the free-form `provider`
  token, so the same five functions manage the credentials of a
  Speech-to-Text provider and of a Text-to-Speech provider.
* It does NOT read a secret back out. There is no "show key" function: the only
  statements that touch a secret are the ones that WRITE one (create/replace).

Objects created (all in `public`, all idempotent):

| Object | Type | Purpose |
|---|---|---|
| `api_credential_list(bigint, text)` | function | owner-scoped METADATA listing (no secret), deterministic order, ≤ 64 rows |
| `api_credential_create(bigint, text, text, text, integer, boolean)` | function | creates the Vault secret AND the metadata row pointing at it |
| `api_credential_replace_secret(bigint, text, text)` | function | swaps in a NEW Vault secret and removes the old one |
| `api_credential_update(bigint, text, text, boolean, integer)` | function | metadata only (label / enabled / priority) — never the secret |
| `api_credential_delete(bigint, text)` | function | removes the Vault secret first, then the metadata row |

SECURITY MODEL

* Every function is SECURITY DEFINER with `SET search_path = ''` and a fully
  qualified body, owned by `postgres`, and REVOKEd from PUBLIC/anon/authenticated
  and granted to `service_role` only — the same posture PART 1 established, and
  deliberately different from the dashboard-readable tables.
* `owner_id` is a REQUIRED parameter of every call and every statement filters on
  it, so one owner can never read or modify another owner's credential, whatever
  button a client sends.
* A raw secret is accepted ONLY by `api_credential_create` and
  `api_credential_replace_secret`, only as an argument, and is handed straight to
  `vault.create_secret` — it is never inserted into `public.api_credentials`,
  never returned, never placed in a comment and never in a log.
* No function returns a secret: every RETURNS TABLE shape is the metadata
  projection of `api_credentials` (`credential_id, provider, label, enabled,
  priority, created_at, updated_at`).

FAILURE AND ORPHAN HANDLING

The Vault API and this table cannot be joined into one atomic unit by anything
other than the surrounding transaction, and this migration does not claim
otherwise. Each write is therefore written to be recoverable:

* CREATE — the metadata insert runs inside a nested BEGIN/EXCEPTION block that
  deletes the Vault secret it just made if the insert fails, then re-raises, so a
  failed create can never leave an unreferenced secret.
* REPLACE — the new secret is created first and the row is switched to it; the
  OLD secret is deleted only AFTER the switch, because `vault_secret_id` carries
  `ON DELETE CASCADE` and deleting it first would have removed the very row being
  updated. A failed switch deletes the new secret instead.
* DELETE — the secret is removed FIRST (which cascades the metadata row) and the
  metadata row is then deleted explicitly as a fallback, so no metadata can
  outlive its secret. A failed secret delete raises, and the caller reports the
  failure instead of a success.
* UPDATE — touches metadata only, so it can never orphan a secret.

## MANUAL SUPABASE ACTION REQUIRED

This migration has NOT been executed and Supabase has NOT been modified.
`vault.secrets` is owned by the Vault extension, so applying this file is an
owner action in the Supabase SQL Editor (it must run as `postgres`). It depends
on PART 1 (`20260919000001_create_api_credential_vault.sql`) having been applied
first. The identical SQL, with rollback, is reproduced in
DATABASE_ARCHITECTURE.md §29.13–§29.17.

Rollback (destructive only to the objects created here):

    DROP FUNCTION IF EXISTS public.api_credential_delete(bigint, text);
    DROP FUNCTION IF EXISTS public.api_credential_update(bigint, text, text, boolean, integer);
    DROP FUNCTION IF EXISTS public.api_credential_replace_secret(bigint, text, text);
    DROP FUNCTION IF EXISTS public.api_credential_create(bigint, text, text, text, integer, boolean);
    DROP FUNCTION IF EXISTS public.api_credential_list(bigint, text);

    -- The PART 1 table, its resolution RPC, the supabase_vault extension and any
    -- Vault secret are NOT touched by this rollback. Dropping the functions stops
    -- all owner-facing management; credentials already configured keep resolving
    -- through api_credential_pool and the owner's Telegram panel reports the
    -- management boundary as unavailable.
*/

-- ============================================================================
-- 1. api_credential_list — owner-scoped METADATA read (never a secret)
-- ============================================================================

CREATE OR REPLACE FUNCTION public.api_credential_list(
    p_owner_id bigint,
    p_provider text DEFAULT NULL
)
RETURNS TABLE (
    credential_id text,
    provider      text,
    label         text,
    enabled       boolean,
    priority      integer,
    created_at    timestamptz,
    updated_at    timestamptz
)
LANGUAGE sql
STABLE
SECURITY DEFINER
SET search_path = ''
AS $$
    SELECT c.credential_id,
           c.provider,
           c.label,
           c.enabled,
           c.priority,
           c.created_at,
           c.updated_at
      FROM public.api_credentials AS c
     WHERE c.owner_id = p_owner_id
       AND (p_provider IS NULL OR c.provider = p_provider)
     ORDER BY c.provider ASC, c.priority ASC, c.created_at ASC, c.credential_id ASC
     LIMIT 64;
$$;

ALTER FUNCTION public.api_credential_list(bigint, text) OWNER TO postgres;

REVOKE ALL ON FUNCTION public.api_credential_list(bigint, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.api_credential_list(bigint, text) TO service_role;

COMMENT ON FUNCTION public.api_credential_list(bigint, text) IS
    'Owner-scoped credential METADATA listing (no secret column is read or returned). Deterministic order: provider, priority, created_at, credential_id. At most 64 rows. Callable by service_role only.';

-- ============================================================================
-- 2. api_credential_create — Vault secret + metadata row, orphan-safe
-- ============================================================================

CREATE OR REPLACE FUNCTION public.api_credential_create(
    p_owner_id bigint,
    p_provider text,
    p_label    text,
    p_secret   text,
    p_priority integer DEFAULT 0,
    p_enabled  boolean DEFAULT true
)
RETURNS TABLE (
    credential_id text,
    provider      text,
    label         text,
    enabled       boolean,
    priority      integer,
    created_at    timestamptz,
    updated_at    timestamptz
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_provider   text;
    v_label      text;
    v_secret     text;
    v_priority   integer;
    v_enabled    boolean;
    v_id         text;
    v_attempt    integer := 0;
    v_secret_id  uuid;
    v_row        public.api_credentials%ROWTYPE;
BEGIN
    IF p_owner_id IS NULL OR p_owner_id <= 0 THEN
        RAISE EXCEPTION 'invalid_owner' USING ERRCODE = '22023';
    END IF;

    v_provider := btrim(coalesce(p_provider, ''));
    IF v_provider !~ '^[a-z0-9][a-z0-9._-]{0,31}$' THEN
        RAISE EXCEPTION 'invalid_provider' USING ERRCODE = '22023';
    END IF;

    v_label := btrim(coalesce(p_label, ''));
    IF length(v_label) = 0 OR length(v_label) > 80 THEN
        RAISE EXCEPTION 'invalid_label' USING ERRCODE = '22023';
    END IF;

    v_secret := coalesce(p_secret, '');
    IF length(v_secret) = 0 THEN
        RAISE EXCEPTION 'empty_secret' USING ERRCODE = '22023';
    END IF;
    IF length(v_secret) > 8192 THEN
        RAISE EXCEPTION 'secret_too_long' USING ERRCODE = '22023';
    END IF;

    v_priority := greatest(0, least(coalesce(p_priority, 0), 1000000));
    v_enabled  := coalesce(p_enabled, true);

    -- A short, non-secret identifier. Bounded retry: a 48-bit collision is
    -- astronomically unlikely, and the loop can never spin unbounded.
    LOOP
        v_attempt := v_attempt + 1;
        v_id := 'c' || substr(replace(gen_random_uuid()::text, '-', ''), 1, 12);
        EXIT WHEN NOT EXISTS (
            SELECT 1 FROM public.api_credentials AS c
             WHERE c.credential_id = v_id
        );
        IF v_attempt >= 3 THEN
            RAISE EXCEPTION 'credential_id_collision' USING ERRCODE = '23505';
        END IF;
    END LOOP;

    -- The ONLY statement in this repository that writes a raw secret, and it
    -- writes it into Vault. `vault.secrets.name` is UNIQUE, so the name is
    -- derived from the (already unique) credential id.
    v_secret_id := vault.create_secret(
        v_secret,
        'api_credential:' || v_id,
        'LifeOS API credential for provider ' || v_provider
    );

    BEGIN
        INSERT INTO public.api_credentials AS c (
            credential_id, provider, label, owner_id, enabled, priority, vault_secret_id
        ) VALUES (
            v_id, v_provider, v_label, p_owner_id, v_enabled, v_priority, v_secret_id
        )
        RETURNING * INTO v_row;
    EXCEPTION WHEN OTHERS THEN
        -- Never leave an unreferenced secret behind. This block runs in its own
        -- subtransaction, so it survives the failure it is cleaning up after.
        BEGIN
            DELETE FROM vault.secrets WHERE id = v_secret_id;
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
        RAISE;
    END;

    credential_id := v_row.credential_id;
    provider      := v_row.provider;
    label         := v_row.label;
    enabled       := v_row.enabled;
    priority      := v_row.priority;
    created_at    := v_row.created_at;
    updated_at    := v_row.updated_at;
    RETURN NEXT;
    RETURN;
END;
$$;

ALTER FUNCTION public.api_credential_create(bigint, text, text, text, integer, boolean)
    OWNER TO postgres;

REVOKE ALL ON FUNCTION public.api_credential_create(bigint, text, text, text, integer, boolean)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.api_credential_create(bigint, text, text, text, integer, boolean)
    TO service_role;

COMMENT ON FUNCTION public.api_credential_create(bigint, text, text, text, integer, boolean) IS
    'Creates one Vault secret and the owner-scoped metadata row referencing it. Accepts a raw secret ONLY as an argument, stores it ONLY in Supabase Vault, and returns metadata only. Removes the just-created secret if the metadata insert fails. Callable by service_role only.';

-- ============================================================================
-- 3. api_credential_replace_secret — swap the key, never orphan the old one
-- ============================================================================

CREATE OR REPLACE FUNCTION public.api_credential_replace_secret(
    p_owner_id bigint,
    p_credential_id text,
    p_secret text
)
RETURNS TABLE (
    credential_id text,
    provider      text,
    label         text,
    enabled       boolean,
    priority      integer,
    created_at    timestamptz,
    updated_at    timestamptz
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_id        text;
    v_secret    text;
    v_old_id    uuid;
    v_new_id    uuid;
    v_row       public.api_credentials%ROWTYPE;
BEGIN
    IF p_owner_id IS NULL OR p_owner_id <= 0 THEN
        RAISE EXCEPTION 'invalid_owner' USING ERRCODE = '22023';
    END IF;

    v_id := btrim(coalesce(p_credential_id, ''));
    IF length(v_id) = 0 OR length(v_id) > 64 THEN
        RAISE EXCEPTION 'invalid_credential' USING ERRCODE = '22023';
    END IF;

    v_secret := coalesce(p_secret, '');
    IF length(v_secret) = 0 THEN
        RAISE EXCEPTION 'empty_secret' USING ERRCODE = '22023';
    END IF;
    IF length(v_secret) > 8192 THEN
        RAISE EXCEPTION 'secret_too_long' USING ERRCODE = '22023';
    END IF;

    SELECT c.vault_secret_id INTO v_old_id
      FROM public.api_credentials AS c
     WHERE c.credential_id = v_id
       AND c.owner_id = p_owner_id
       FOR UPDATE;

    IF v_old_id IS NULL THEN
        RAISE EXCEPTION 'credential_not_found' USING ERRCODE = 'P0002';
    END IF;

    v_new_id := vault.create_secret(
        v_secret,
        'api_credential:' || v_id || ':' || substr(md5(gen_random_uuid()::text), 1, 8),
        'LifeOS API credential for provider (replaced)'
    );

    BEGIN
        UPDATE public.api_credentials AS c
           SET vault_secret_id = v_new_id,
               updated_at = now()
         WHERE c.credential_id = v_id
           AND c.owner_id = p_owner_id
        RETURNING * INTO v_row;
        IF NOT FOUND THEN
            RAISE EXCEPTION 'credential_not_found' USING ERRCODE = 'P0002';
        END IF;
    EXCEPTION WHEN OTHERS THEN
        BEGIN
            DELETE FROM vault.secrets WHERE id = v_new_id;
        EXCEPTION WHEN OTHERS THEN
            NULL;
        END;
        RAISE;
    END;

    -- Only AFTER the row points at the new secret: deleting the old one first
    -- would have cascaded the row away (vault_secret_id is ON DELETE CASCADE).
    BEGIN
        DELETE FROM vault.secrets WHERE id = v_old_id;
    EXCEPTION WHEN OTHERS THEN
        -- The new secret is live and the row is correct; a leftover old secret
        -- is inert because nothing references it any more. Reported honestly in
        -- DATABASE_ARCHITECTURE.md §29.16 rather than failing the swap.
        NULL;
    END;

    credential_id := v_row.credential_id;
    provider      := v_row.provider;
    label         := v_row.label;
    enabled       := v_row.enabled;
    priority      := v_row.priority;
    created_at    := v_row.created_at;
    updated_at    := v_row.updated_at;
    RETURN NEXT;
    RETURN;
END;
$$;

ALTER FUNCTION public.api_credential_replace_secret(bigint, text, text)
    OWNER TO postgres;

REVOKE ALL ON FUNCTION public.api_credential_replace_secret(bigint, text, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.api_credential_replace_secret(bigint, text, text)
    TO service_role;

COMMENT ON FUNCTION public.api_credential_replace_secret(bigint, text, text) IS
    'Replaces the Vault secret of ONE owner-scoped credential. Never returns the old or the new secret and never changes label/enabled/priority. Callable by service_role only.';

-- ============================================================================
-- 4. api_credential_update — metadata only, never the secret
-- ============================================================================

CREATE OR REPLACE FUNCTION public.api_credential_update(
    p_owner_id bigint,
    p_credential_id text,
    p_label text DEFAULT NULL,
    p_enabled boolean DEFAULT NULL,
    p_priority integer DEFAULT NULL
)
RETURNS TABLE (
    credential_id text,
    provider      text,
    label         text,
    enabled       boolean,
    priority      integer,
    created_at    timestamptz,
    updated_at    timestamptz
)
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_id      text;
    v_label   text;
    v_row     public.api_credentials%ROWTYPE;
BEGIN
    IF p_owner_id IS NULL OR p_owner_id <= 0 THEN
        RAISE EXCEPTION 'invalid_owner' USING ERRCODE = '22023';
    END IF;

    v_id := btrim(coalesce(p_credential_id, ''));
    IF length(v_id) = 0 OR length(v_id) > 64 THEN
        RAISE EXCEPTION 'invalid_credential' USING ERRCODE = '22023';
    END IF;

    IF p_label IS NOT NULL THEN
        v_label := btrim(p_label);
        IF length(v_label) = 0 OR length(v_label) > 80 THEN
            RAISE EXCEPTION 'invalid_label' USING ERRCODE = '22023';
        END IF;
    END IF;

    IF p_priority IS NOT NULL AND (p_priority < 0 OR p_priority > 1000000) THEN
        RAISE EXCEPTION 'invalid_priority' USING ERRCODE = '22023';
    END IF;

    UPDATE public.api_credentials AS c
       SET label      = coalesce(v_label, c.label),
           enabled    = coalesce(p_enabled, c.enabled),
           priority   = coalesce(p_priority, c.priority),
           updated_at = now()
     WHERE c.credential_id = v_id
       AND c.owner_id = p_owner_id
    RETURNING * INTO v_row;

    IF NOT FOUND THEN
        RAISE EXCEPTION 'credential_not_found' USING ERRCODE = 'P0002';
    END IF;

    credential_id := v_row.credential_id;
    provider      := v_row.provider;
    label         := v_row.label;
    enabled       := v_row.enabled;
    priority      := v_row.priority;
    created_at    := v_row.created_at;
    updated_at    := v_row.updated_at;
    RETURN NEXT;
    RETURN;
END;
$$;

ALTER FUNCTION public.api_credential_update(bigint, text, text, boolean, integer)
    OWNER TO postgres;

REVOKE ALL ON FUNCTION public.api_credential_update(bigint, text, text, boolean, integer)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.api_credential_update(bigint, text, text, boolean, integer)
    TO service_role;

COMMENT ON FUNCTION public.api_credential_update(bigint, text, text, boolean, integer) IS
    'Updates the owner-scoped METADATA of ONE credential (label / enabled / priority). A NULL argument leaves that field unchanged. Reads and writes no secret. Callable by service_role only.';

-- ============================================================================
-- 5. api_credential_delete — secret first (it cascades), row as a fallback
-- ============================================================================

CREATE OR REPLACE FUNCTION public.api_credential_delete(
    p_owner_id bigint,
    p_credential_id text
)
RETURNS boolean
LANGUAGE plpgsql
VOLATILE
SECURITY DEFINER
SET search_path = ''
AS $$
DECLARE
    v_id        text;
    v_secret_id uuid;
BEGIN
    IF p_owner_id IS NULL OR p_owner_id <= 0 THEN
        RAISE EXCEPTION 'invalid_owner' USING ERRCODE = '22023';
    END IF;

    v_id := btrim(coalesce(p_credential_id, ''));
    IF length(v_id) = 0 OR length(v_id) > 64 THEN
        RAISE EXCEPTION 'invalid_credential' USING ERRCODE = '22023';
    END IF;

    SELECT c.vault_secret_id INTO v_secret_id
      FROM public.api_credentials AS c
     WHERE c.credential_id = v_id
       AND c.owner_id = p_owner_id
       FOR UPDATE;

    IF v_secret_id IS NULL THEN
        -- Nothing of the owner's matches: an honest "not found", not an error.
        RETURN false;
    END IF;

    -- Removing the secret is what makes this a real deletion. If it fails the
    -- function raises and the caller must NOT report success.
    DELETE FROM vault.secrets WHERE id = v_secret_id;

    -- The foreign key cascades the metadata row; this explicit delete only does
    -- anything on a deployment whose constraint is missing, and it guarantees no
    -- metadata outlives its secret either way.
    DELETE FROM public.api_credentials AS c
     WHERE c.credential_id = v_id
       AND c.owner_id = p_owner_id;

    RETURN true;
END;
$$;

ALTER FUNCTION public.api_credential_delete(bigint, text) OWNER TO postgres;

REVOKE ALL ON FUNCTION public.api_credential_delete(bigint, text)
    FROM PUBLIC, anon, authenticated;
GRANT EXECUTE ON FUNCTION public.api_credential_delete(bigint, text) TO service_role;

COMMENT ON FUNCTION public.api_credential_delete(bigint, text) IS
    'Deletes ONE owner-scoped credential: the Vault secret first (which cascades the metadata row), then the metadata row. Returns false when the owner has no such credential. Callable by service_role only.';

-- ============================================================================
-- 6. PostgREST schema cache
-- ============================================================================

NOTIFY pgrst, 'reload schema';
```

### 29.15 Manual reversal SQL — NOT EXECUTED BY AI

Destructive only to the five functions created by §29.14. It does **not** drop the
PART 1 table or its resolution function, does **not** drop the shared
`supabase_vault` extension, and does **not** delete any stored key — removing the
panel must not destroy credentials the runtime still resolves.

```sql
-- 1. The five PART 2 management functions (they depend on the PART 1 table).
DROP FUNCTION IF EXISTS public.api_credential_delete(bigint, text);
DROP FUNCTION IF EXISTS public.api_credential_update(bigint, text, text, boolean, integer);
DROP FUNCTION IF EXISTS public.api_credential_replace_secret(bigint, text, text);
DROP FUNCTION IF EXISTS public.api_credential_create(bigint, text, text, text, integer, boolean);
DROP FUNCTION IF EXISTS public.api_credential_list(bigint, text);

-- 2. Deliberately NOT dropped by this reversal:
--    * public.api_credentials, its three indexes, public.api_credential_pool and
--      the stt_credential_pool alias — they belong to PART 1 and are reversed by
--      §29.11, not here;
--    * EXTENSION supabase_vault — a shared Supabase extension;
--    * rows in vault.secrets — a management rollback must NOT destroy the owner's
--      keys, because removing the panel does not remove the credentials the runtime
--      still resolves through public.api_credential_pool. Remove them explicitly,
--      in the Vault UI/SQL, only when you are certain nothing uses them:
--          SELECT id, name FROM vault.secrets ORDER BY name;
--    * the ENV credentials on the deployment;
--    * the credential METADATA rows (they are inert without the panel).
NOTIFY pgrst, 'reload schema';
```

### 29.16 Orphan-secret handling (summary)

| Situation | Outcome | Recovery |
|---|---|---|
| create fails after the secret was made | the secret is deleted before the error leaves the function | none needed |
| replace fails while switching the row | the NEW secret is deleted; the old key and the row are untouched | none needed |
| replace succeeds but the old secret cannot be removed | the row points at the new key; one unreferenced secret remains | `SELECT vault.delete_secret('<id>')` at your discretion |
| delete cannot remove the secret | the transaction aborts; the row and the key both remain | retry, or fix the privilege and retry |
| metadata row deleted in the Vault UI instead | the FK would already have cascaded the row; nothing to do | none |
| a key deleted directly in the Vault UI | the metadata row cascades away | the panel simply no longer lists it |

### 29.17 Owner-facing surface added by PART 2

| Panel / action | What it does | Secret handling |
|---|---|---|
| `AI → Media Analysis → API Credentials` | lists the registered providers and how many managed keys each has | counts only |
| one provider | lists that provider's credentials (label, enabled, priority, last test), and whether a deployment key exists | metadata only; the deployment key is reported as present/not present, never named or shown |
| one credential | enable/disable, rename, move earlier/later, set priority, replace key, test, delete | only "replace key" collects a value |
| add / replace input | asks for ONE message containing the key, stores it, then **deletes that message** and reports honestly if it could not | the key is never rendered, logged, echoed back or put in callback data |
| delete | asks for confirmation first, then removes the key and the metadata together | a failure is reported as a failure |

Credential addresses in callback data are short non-secret handles derived from the
credential id (SHA-256 prefix), so a payload never carries a value and a button is
never longer than Telegram's 64-byte callback limit.

### 29.18 PART 2 manual configuration still required

1. Apply §29.14 (identical to the migration) as `postgres`, after §29.10.
2. Nothing else: no table to create, no row to seed, no environment variable to add,
   no Render setting to change. Keys are added from the panel.
3. Optional verification, read-only:

```sql
SELECT credential_id, provider, label, enabled, priority, created_at
  FROM public.api_credentials
 ORDER BY provider, priority, created_at, credential_id;
```

4. Reversal, if ever needed, is §29.15 (management functions only) or §29.11
   (PART 1 objects).

### 29.19 What PART 2 deliberately does not do

* No second secret store, no per-capability credential table, no `tts_credentials.py`.
* No SQL execution, no Supabase connection, no secret creation or deletion by the
  coding agent — every object above is pending an owner action.
* No display of a raw key: there is no show, reveal, copy or export operation.
* No credential-management UI beyond the three panel levels described in §29.17, and
  no credential health claim: credential existence is never presented as provider
  health.
* No TTS control-plane change: the TTS provider's credentials are manageable, its
  model/voice selection and provider fallback remain deferred.
* No environment-variable scan and no numbered `*_KEY_1/2/3` variable scheme.

### 29.20 Migration status and next steps

| Item | Value |
|---|---|
| Applied by the coding agent | **NO** — no SQL was executed, Supabase was not modified, no Vault secret was created |
| Objects pending owner action | `api_credentials`, its 3 indexes, RLS+grants, `api_credential_pool`, `stt_credential_pool` |
| Application behaviour without it | unchanged from M2.4/M3.0 — the ENV credential keeps every provider working and the RPC failure is a bounded warning |
| Live Supabase verification | **NOT PERFORMED** — the RPC path was exercised only against a fake secret backend in `tests/test_credential_vault.py` |
| Applied by the coding agent (PART 2) | **NO** — no SQL was executed, Supabase was not modified, no Vault secret was created and no existing key was deleted |
| Objects pending owner action (PART 2) | the five management functions of §29.13 (grants and comments included) |
| PART 2 panel behaviour without it | the panel reports the credential store as not configured and changes nothing; every provider keeps resolving its credentials exactly as before |
| Live verification (PART 2) | **NOT PERFORMED** — the management path was exercised only against a fake store in `tests/test_credential_management.py`; no real provider request, no Telegram session and no Supabase project was used |
| Next after PART 2 | live verification of the panel against the owner's own Supabase project, then the deferred TTS provider fallback and its credential pool |

## Final Canonical Contract

The canonical database consists of **16** public-schema tables: `saved_items`, `bio_state`, `username_state`, `bot_logs`, `panel_settings`, `bot_settings`, `ai_config`, `ai_sessions`, `ai_messages`, `ai_memories`, `ai_tool_history`, `ai_usage`, `ai_provider_stats`, `ai_tasks`, `ai_task_occurrences`, and the compatibility-preserved legacy table `ghost_chats`. The complete definitions, defaults, constraints, indexes, RLS policies, seeds, existing-database reconciliation and drift report are in the single SQL block in the next section — and the same text is the repository migration `supabase/migrations/20260920000001_reconcile_canonical_schema.sql` (see §30).

All tables use RLS. `anon` and `authenticated` have SELECT-only policies with `USING (true)`; they have no INSERT, UPDATE, or DELETE policies. Backend writes use the service-role client. No foreign keys are required: identifier relationships are intentionally application-level.

### Persistent state contract

| State | Storage | Durable behavior |
|---|---|---|
| Saved item metadata | `saved_items` | Inserted after Deep Save and read by retrieval, search, delete, and dashboard paths |
| Bio automation | `bio_state` keyed by `owner_id` | Loaded/updated by the ProfileEngine |
| Username automation | `username_state` keyed by `owner_id` | Loaded/updated by the ProfileEngine |
| Global settings | `panel_settings` row `key='global'` | Required singleton is seeded; dashboard font is `dashboard_font text NOT NULL DEFAULT 'default'` with the 23-key CHECK |
| Ghost Seen enabled chats | `bot_settings` row `key='ghost_seen_allowed_chats'`, `value text` | JSON array of integer Telegram chat IDs; loaded at startup and consumed by `is_chat_allowed()` |
| AI configuration | `ai_config` keyed by `owner_id` | Provider/configuration and trigger fields persist across startup |
| AI sessions/messages/memories/tool history | respective AI tables | Persisted where current repositories write/read them |
| AI usage/provider aggregates | `ai_usage`, `ai_provider_stats` | Per-request telemetry and `(provider_name, owner_id)` aggregate upsert |

Ghost Seen does not persist messages, selections, reply state, pagination, locks, or temporary viewer state. `ghost_chats` is legacy compatibility storage and is not the Ghost Seen allow-list source.

### Required seeds

`panel_settings(key='global')` is required. The canonical SQL inserts it with `ON CONFLICT DO NOTHING`. Five legacy `bot_settings` compatibility rows are also inserted. The Ghost Seen allow-list row is intentionally not seeded: the first toggle creates it, and its initial logical value is an empty list. No historical application data or credentials are fabricated.

## Canonical Supabase Bootstrap SQL (Full Database Contract Audit)

**Audited revision:** `30bb3a426c2ec419be9d8f43373d85ce27d77099` (`origin/main`).

**Method:** repository-wide enumeration of every Supabase/PostgREST access
(`git grep` across `backend/`, `tests/`, `src/`: **60 `.table()` call sites,
zero `.rpc()` calls**), then payload-level tracing of every writer/reader into
the 16 chronological migrations under `supabase/migrations/`. The current
source code — not this document's earlier prose, not migration comments, and
not prior reports — is the authority this script was derived from.

> **Audit-scope note (§29):** the enumeration above was taken at the audited
> revision `30bb3a4` and is preserved as the basis of the bootstrap script.
> Since then exactly ONE `.rpc()` call site exists —
> `backend/ai/credential_source.py::_vault_rows_sync` calling
> `api_credential_pool` — and it is a **separate, optional** object documented
> in §29, deliberately NOT folded into the canonical public-schema bootstrap
> below (that script establishes the tables the core app requires; the vault
> RPC is an owner-configured secret path).

### Application ↔ database contract matrix

| Table | Live code path (evidence) | Operations | Status |
|---|---|---|---|
| `saved_items` | `backend/db/client.py` (insert/query/list/search/delete/count/stats); payload built in `backend/services/save_service.py` | INSERT; SELECT `*`/projected; DELETE; COUNT; filters `owner_id`, `save_code`, `id IN (…)`; ORDER `created_at DESC`; range pagination | CURRENT |
| `bio_state` | `backend/db/client.py` (`get_or_create_bio_state`/`update_bio_state`/`get_bio_state`) | SELECT/INSERT/UPDATE keyed by `owner_id` | CURRENT |
| `username_state` | `backend/db/client.py` (`get_or_create_username_state`/…); migration `20260801215007` | SELECT/INSERT/UPDATE keyed by `owner_id` | CURRENT |
| `bot_logs` | `backend/db/client.py` `log`/`list_logs`/`count_logs`/`clean_logs`; `backend/runtime/startup_check.py` (dormant) | INSERT; SELECT/COUNT/DELETE by `owner_id` + `created_at` cutoff | CURRENT |
| `panel_settings` | `backend/services/panel_settings_repository.py` (sole accessor) | SELECT/UPDATE where `key='global'` | CURRENT |
| `bot_settings` | `backend/services/ghost_seen_v2.py` (`ghost_seen_allowed_chats` KV) | SELECT/INSERT/UPDATE by `key` | CURRENT |
| `ai_config` | `backend/ai/config_store.py` | SELECT/INSERT/UPDATE keyed by `owner_id` (manual upsert flow) | CURRENT |
| `ai_sessions` | `backend/ai/persistence.py` (create/update/get session) | INSERT/UPDATE/SELECT keyed by `session_id` | CURRENT |
| `ai_messages` | `backend/ai/persistence.py` (`add_message`/`get_messages`) | INSERT; SELECT by `session_id` ORDER `created_at` ASC | CURRENT |
| `ai_memories` | `backend/ai/persistence.py` (save/query/delete/delete_expired/count) | INSERT/SELECT/DELETE/COUNT; filters `owner_id`/`tier`/`category`/`importance` | CURRENT |
| `ai_tool_history` | `backend/ai/persistence.py::record_tool_call` | INSERT only | CURRENT |
| `ai_usage` | `backend/ai/database/usage_repository.py::SupabaseUsageRepository` | INSERT/SELECT/COUNT; filters `owner_id`, `created_at` range | CURRENT |
| `ai_provider_stats` | `backend/ai/database/provider_stats_repository.py::SupabaseProviderStatsRepository` | UPSERT `on_conflict="provider_name,owner_id"`; SELECT; COUNT | CURRENT |
| `ghost_chats` | **none** — zero `.table("ghost_chats")` references in `backend/`, `tests/`, `src/` | — | LEGACY (owner-gated drop) |

### Migration-history reconciliation applied by the canonical script

1. **Duplicate first migration superseded.** `20260712234229_lifeos_schema.sql`
   and `20260714111706_create_lifeos_tables.sql` both create
   `saved_items`/`bio_state`/`bot_logs`. The later file is authoritative: it
   removes the `save_type IN ('forward','deep')` CHECK, the
   `bot_logs.level IN ('INFO','WARN','ERROR')` CHECK, and the broad anon
   INSERT/UPDATE/DELETE policies. A raw chronological replay leaves
   `anon_update_bot_logs` alive (the later file never drops it). The canonical
   script follows the later intent and explicitly drops all anon write
   policies on every table.
2. **`ghost_seen_retention_days` is transient.** Created by `20260823120000`,
   then dropped by `20260823130000` (a destructive `DROP COLUMN` inside a
   conditional DO block). The canonical script establishes only the final
   state (`ghost_seen_retention_seconds`) and never replays the drop.
3. **Legacy columns preserved additively (no live writer in current code):**
   `saved_items.file_name` and `saved_items.short_code` (the current Deep-Save
   payload in `save_service.py` writes neither),
   `panel_settings.update_stale_seconds` (its migration comment claims a
   watchdog consumer; none exists in code),
   `panel_settings.ghost_seen_retention_seconds` (zero Python consumers), and
   `ai_tool_history.result_data` (never inserted). All are kept so historical
   rows and rollback paths stay valid; removal remains owner-gated.
4. **`username_state` is fully migrated** (`20260801215007`). An earlier audit
   note claiming it was migration-less was an indexing artifact; corrected here.
5. **Seeds.** The only row current code *requires* is the `panel_settings`
   singleton (`key='global'` — repository updates target it and silently
   no-op without it). The five `bot_settings` default rows from
   `20260729213959` are consumed by no current reader; they are retained
   verbatim (`ON CONFLICT DO NOTHING`) for migration fidelity and classified
   legacy. `ghost_seen_allowed_chats` is created at runtime by
   `ghost_seen_v2` and is deliberately NOT seeded.
6. **No functions, triggers, or RPC endpoints are required** — as of the
   audited revision the codebase contained zero `.rpc(` calls. The optional
   `api_credential_pool` Vault RPC in §29 is outside this bootstrap and is not
   required for the application to run.
7. **Extension.** `pg_trgm` is required by the `saved_items` trigram indexes
   backing the `search_saves` ILIKE paths.
8. **`CREATE TABLE IF NOT EXISTS` is never the only reconciliation
   mechanism.** Every canonical column is re-asserted explicitly after every
   CREATE; this is the fix for the 42703 failure class documented in §30.
9. **Constraint and index additions are data-guarded.** `panel_settings`'s
   eight CHECKs, the `ai_config`/`ai_memories`/`ai_sessions`/`ai_tasks`/
   `ai_task_occurrences` CHECKs, the `ai_task_occurrences` foreign key, the
   two data-dependent unique indexes and the consolidated identity block
   (primary keys + UNIQUEs) are each applied only when the existing rows can
   satisfy them; otherwise the script raises a WARNING that names the table,
   the constraint and the offending row count, and continues.
10. **`ai_tasks` / `ai_task_occurrences` were missing from the canonical
    contract** even though live code depends on them; they are now part of
    it. `ai_preferences` (no migration, in-memory only) and
    `ai_messages.tool_calls` (no reader/writer) are deliberately still NOT
    canonical — see §19.5, §19.17 and §30.2.

The same text exists in exactly three places and they are kept
**byte-identical** — enforced by
`tests/test_canonical_schema_reconciliation.py`:

1. the fenced block above (the canonical reference, and the SQL the owner
   pastes into the Supabase SQL Editor),
2. `supabase/canonical_bootstrap.sql` (convenience copy),
3. `supabase/migrations/20260920000001_reconcile_canonical_schema.sql`
   (the repository migration for the §30 repair).

There is therefore exactly ONE reconciled definition of the schema; the
historical migration files stay untouched as history.

### The script (single copy-pasteable block — Supabase SQL Editor)

```sql
-- ============================================================================
-- LifeOS / Telegram Self-Bot — Canonical Supabase Bootstrap & Reconciliation
--
-- Generated from a repository-wide database contract audit of commit
-- 30bb3a426c2ec419be9d8f43373d85ce27d77099 (origin/main). See
-- DATABASE_ARCHITECTURE.md §30 for the schema-drift audit this revision
-- resolves.
--
-- Properties
--   * Establishes the COMPLETE database state required by CURRENT code:
--     16 canonical tables (the 14 previously documented public-schema tables
--     plus the live task-system tables ai_tasks / ai_task_occurrences that the
--     20260829…–20260912… migrations create) + 1 compatibility-preserved
--     legacy table (ghost_chats, kept additively).
--   * EXISTING-DATABASE RECONCILIATION. `CREATE TABLE IF NOT EXISTS` is a
--     silent NO-OP when the table already exists, so it is NEVER the only
--     mechanism that establishes a column. Immediately after every CREATE,
--     every canonical column is re-asserted with
--     `ADD COLUMN IF NOT EXISTS`, existing rows are backfilled
--     deterministically, and the column is bound to its final default /
--     NOT NULL contract. This is the fix for the 42703 class of failure
--     (`column "..." does not exist`) that a legacy table shape used to cause
--     on `bot_settings.value_type`, on the `panel_settings` constraint blocks
--     and on the `ai_config` upsert columns.
--   * Safe on a FRESH database, safe on any older/partially-migrated one, and
--     safe to run twice (every statement is idempotent or catalog-guarded).
--   * Additive only: no DROP TABLE, no DROP COLUMN, no DELETE, no TRUNCATE and
--     no data destruction. The only intentional drops are stale anon WRITE
--     policies that contradict the documented SELECT-only dashboard boundary.
--     Constraint/index additions that existing data could reject are
--     data-guarded: they are skipped with an explicit WARNING naming the
--     offending row count instead of aborting the script.
--   * Security model: ALL writes use the service-role key (bypasses RLS);
--     anon + authenticated get SELECT-only (read-only dashboard). No anon
--     INSERT/UPDATE/DELETE policy exists on any canonical table.
--   * The optional Supabase Vault credential RPCs are NOT part of this script;
--     they are documented and applied separately in DATABASE_ARCHITECTURE.md
--     §29 (api_credentials, api_credential_pool, the management functions).
--   * Historical application data (saved items, AI sessions/messages/
--     memories/usage/stats, logs, allow-list values) is intentionally NOT
--     fabricated. Only deterministic, project-defined seed rows are inserted.
--   * The script ends with a drift report: after COMMIT it lists any canonical
--     (table, column) that is still absent. Zero rows == the database now
--     matches the canonical contract.
--
-- Execution: paste the whole file into the Supabase SQL Editor as `postgres`.
--
-- This text exists in exactly three places and they are kept byte-identical
-- (tests/test_canonical_schema_reconciliation.py enforces it):
--   1. supabase/migrations/20260920000001_reconcile_canonical_schema.sql
--      (the repository migration for this repair)
--   2. supabase/canonical_bootstrap.sql (this convenience copy)
--   3. the fenced SQL block in DATABASE_ARCHITECTURE.md §30
-- ============================================================================

BEGIN;

-- ─── 1. Extensions ──────────────────────────────────────────────────────────
-- pg_trgm powers the saved_items trigram indexes used by the
-- db_client.search_saves caption/save_code/mime_type ILIKE paths.
CREATE EXTENSION IF NOT EXISTS pg_trgm;

-- ─── 2. saved_items ─────────────────────────────────────────────────────────
-- Writer: backend/db/client.py (insert/query/list/search/delete/count/stats)
-- Payload: backend/services/save_service.py::execute_save
-- save_type has NO CHECK: 20260714111706 supersedes the ('forward','deep')
-- CHECK from 20260712234229.
-- file_name / short_code are legacy columns (no live writer) kept additively.
CREATE TABLE IF NOT EXISTS saved_items (
    id              bigserial    PRIMARY KEY,
    save_code       text         NOT NULL UNIQUE,
    save_type       text         NOT NULL DEFAULT 'forward',
    origin_chat_id  bigint,
    origin_msg_id   bigint,
    saved_chat_id   bigint,
    saved_msg_id    bigint,
    sender_name     text,
    sender_id       bigint,
    mime_type       text,
    file_id         text,
    file_size       bigint,
    media_type      text,
    tags            text[]       DEFAULT '{}',
    caption         text,
    file_name       text,
    short_code      text,
    owner_id        bigint       NOT NULL,
    created_at      timestamptz  DEFAULT now()
);

-- Existing-database column reconciliation (see the header).
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS id              bigserial;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS save_code       text;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS save_type       text        NOT NULL DEFAULT 'forward';
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS origin_chat_id  bigint;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS origin_msg_id   bigint;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS saved_chat_id   bigint;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS saved_msg_id    bigint;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS sender_name     text;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS sender_id       bigint;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS mime_type       text;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS file_id         text;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS file_size       bigint;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS media_type      text;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS tags            text[]      DEFAULT '{}';
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS caption         text;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS file_name       text;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS short_code      text;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS owner_id        bigint;
ALTER TABLE saved_items ADD COLUMN IF NOT EXISTS created_at      timestamptz DEFAULT now();

-- Deterministic backfill so the NOT NULL contract below can never fail.
UPDATE saved_items SET save_code  = 'S' || lpad(id::text, 4, '0') WHERE save_code  IS NULL;
UPDATE saved_items SET save_type  = 'forward'  WHERE save_type  IS NULL;
UPDATE saved_items SET tags       = '{}'       WHERE tags       IS NULL;
UPDATE saved_items SET created_at = now()      WHERE created_at IS NULL;
UPDATE saved_items SET owner_id   = 0          WHERE owner_id   IS NULL;

ALTER TABLE saved_items ALTER COLUMN save_type  SET DEFAULT 'forward';
ALTER TABLE saved_items ALTER COLUMN tags       SET DEFAULT '{}';
ALTER TABLE saved_items ALTER COLUMN created_at SET DEFAULT now();

ALTER TABLE saved_items ALTER COLUMN save_code SET NOT NULL;
ALTER TABLE saved_items ALTER COLUMN save_type SET NOT NULL;
ALTER TABLE saved_items ALTER COLUMN owner_id  SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_saved_items_owner          ON saved_items (owner_id);
CREATE INDEX IF NOT EXISTS idx_saved_items_save_code      ON saved_items (save_code);
CREATE INDEX IF NOT EXISTS idx_saved_items_created_at     ON saved_items (created_at DESC);
CREATE INDEX IF NOT EXISTS idx_saved_items_owner_created  ON saved_items (owner_id, created_at DESC);
CREATE INDEX IF NOT EXISTS idx_saved_items_caption_trgm   ON saved_items USING gin (caption gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_file_name_trgm ON saved_items USING gin (file_name gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_save_code_trgm ON saved_items USING gin (save_code gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_short_code_trgm ON saved_items USING gin (short_code gin_trgm_ops);
CREATE INDEX IF NOT EXISTS idx_saved_items_mime_trgm      ON saved_items USING gin (mime_type gin_trgm_ops);

-- Data-guarded partial unique index: legacy rows may share a short_code.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'idx_saved_items_short_code') THEN
        IF NOT EXISTS (
            SELECT 1 FROM saved_items
            WHERE short_code IS NOT NULL
            GROUP BY short_code HAVING count(*) > 1
        ) THEN
            CREATE UNIQUE INDEX idx_saved_items_short_code
                ON saved_items (short_code) WHERE short_code IS NOT NULL;
        ELSE
            RAISE WARNING 'saved_items: idx_saved_items_short_code NOT created - duplicate non-NULL short_code values exist. Resolve them and re-run this script.';
        END IF;
    END IF;
END $$;

ALTER TABLE saved_items ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_saved_items" ON saved_items;
DROP POLICY IF EXISTS "anon_update_saved_items" ON saved_items;
DROP POLICY IF EXISTS "anon_delete_saved_items" ON saved_items;
DROP POLICY IF EXISTS "anon_select_saved_items" ON saved_items;
CREATE POLICY "anon_select_saved_items" ON saved_items FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 3. bio_state ───────────────────────────────────────────────────────────
-- Writer/reader: backend/db/client.py (bio state + Bio ProfileEngine)
CREATE TABLE IF NOT EXISTS bio_state (
    id           bigserial    PRIMARY KEY,
    owner_id     bigint       NOT NULL UNIQUE,
    template     text         NOT NULL DEFAULT '🕒 {time} | 💭 {mood}',
    mood         text         NOT NULL DEFAULT '😊',
    custom_text  text         NOT NULL DEFAULT '',
    is_active    boolean      NOT NULL DEFAULT false,
    last_bio     text         NOT NULL DEFAULT '',
    updated_at   timestamptz  DEFAULT now()
);

ALTER TABLE bio_state ADD COLUMN IF NOT EXISTS id          bigserial;
ALTER TABLE bio_state ADD COLUMN IF NOT EXISTS owner_id    bigint      NOT NULL DEFAULT 0;
ALTER TABLE bio_state ADD COLUMN IF NOT EXISTS template    text        NOT NULL DEFAULT '🕒 {time} | 💭 {mood}';
ALTER TABLE bio_state ADD COLUMN IF NOT EXISTS mood        text        NOT NULL DEFAULT '😊';
ALTER TABLE bio_state ADD COLUMN IF NOT EXISTS custom_text text        NOT NULL DEFAULT '';
ALTER TABLE bio_state ADD COLUMN IF NOT EXISTS is_active   boolean     NOT NULL DEFAULT false;
ALTER TABLE bio_state ADD COLUMN IF NOT EXISTS last_bio    text        NOT NULL DEFAULT '';
ALTER TABLE bio_state ADD COLUMN IF NOT EXISTS updated_at  timestamptz DEFAULT now();

UPDATE bio_state SET owner_id    = 0                       WHERE owner_id    IS NULL;
UPDATE bio_state SET template    = '🕒 {time} | 💭 {mood}' WHERE template    IS NULL;
UPDATE bio_state SET mood        = '😊'                    WHERE mood        IS NULL;
UPDATE bio_state SET custom_text = ''                      WHERE custom_text IS NULL;
UPDATE bio_state SET is_active   = false                   WHERE is_active   IS NULL;
UPDATE bio_state SET last_bio    = ''                      WHERE last_bio    IS NULL;
UPDATE bio_state SET updated_at  = now()                   WHERE updated_at  IS NULL;

ALTER TABLE bio_state ALTER COLUMN template    SET DEFAULT '🕒 {time} | 💭 {mood}';
ALTER TABLE bio_state ALTER COLUMN mood        SET DEFAULT '😊';
ALTER TABLE bio_state ALTER COLUMN custom_text SET DEFAULT '';
ALTER TABLE bio_state ALTER COLUMN is_active   SET DEFAULT false;
ALTER TABLE bio_state ALTER COLUMN last_bio    SET DEFAULT '';
ALTER TABLE bio_state ALTER COLUMN updated_at  SET DEFAULT now();

ALTER TABLE bio_state ALTER COLUMN owner_id    SET NOT NULL;
ALTER TABLE bio_state ALTER COLUMN template    SET NOT NULL;
ALTER TABLE bio_state ALTER COLUMN mood        SET NOT NULL;
ALTER TABLE bio_state ALTER COLUMN custom_text SET NOT NULL;
ALTER TABLE bio_state ALTER COLUMN is_active   SET NOT NULL;
ALTER TABLE bio_state ALTER COLUMN last_bio    SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_bio_state_owner ON bio_state (owner_id);

ALTER TABLE bio_state ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_bio_state" ON bio_state;
DROP POLICY IF EXISTS "anon_update_bio_state" ON bio_state;
DROP POLICY IF EXISTS "anon_delete_bio_state" ON bio_state;
DROP POLICY IF EXISTS "anon_select_bio_state" ON bio_state;
CREATE POLICY "anon_select_bio_state" ON bio_state FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 4. username_state ──────────────────────────────────────────────────────
-- Writer/reader: backend/db/client.py (username state + Username ProfileEngine)
CREATE TABLE IF NOT EXISTS username_state (
    id           bigserial    PRIMARY KEY,
    owner_id     bigint       NOT NULL UNIQUE,
    template     text         NOT NULL DEFAULT '{time} | {mood}',
    mood         text         NOT NULL DEFAULT '😊',
    custom_text  text         NOT NULL DEFAULT '',
    is_active    boolean      NOT NULL DEFAULT false,
    last_name    text         NOT NULL DEFAULT '',
    updated_at   timestamptz  DEFAULT now()
);

ALTER TABLE username_state ADD COLUMN IF NOT EXISTS id          bigserial;
ALTER TABLE username_state ADD COLUMN IF NOT EXISTS owner_id    bigint      NOT NULL DEFAULT 0;
ALTER TABLE username_state ADD COLUMN IF NOT EXISTS template    text        NOT NULL DEFAULT '{time} | {mood}';
ALTER TABLE username_state ADD COLUMN IF NOT EXISTS mood        text        NOT NULL DEFAULT '😊';
ALTER TABLE username_state ADD COLUMN IF NOT EXISTS custom_text text        NOT NULL DEFAULT '';
ALTER TABLE username_state ADD COLUMN IF NOT EXISTS is_active   boolean     NOT NULL DEFAULT false;
ALTER TABLE username_state ADD COLUMN IF NOT EXISTS last_name   text        NOT NULL DEFAULT '';
ALTER TABLE username_state ADD COLUMN IF NOT EXISTS updated_at  timestamptz DEFAULT now();

UPDATE username_state SET owner_id    = 0                 WHERE owner_id    IS NULL;
UPDATE username_state SET template    = '{time} | {mood}' WHERE template    IS NULL;
UPDATE username_state SET mood        = '😊'              WHERE mood        IS NULL;
UPDATE username_state SET custom_text = ''                WHERE custom_text IS NULL;
UPDATE username_state SET is_active   = false             WHERE is_active   IS NULL;
UPDATE username_state SET last_name   = ''                WHERE last_name   IS NULL;
UPDATE username_state SET updated_at  = now()             WHERE updated_at  IS NULL;

ALTER TABLE username_state ALTER COLUMN template    SET DEFAULT '{time} | {mood}';
ALTER TABLE username_state ALTER COLUMN mood        SET DEFAULT '😊';
ALTER TABLE username_state ALTER COLUMN custom_text SET DEFAULT '';
ALTER TABLE username_state ALTER COLUMN is_active   SET DEFAULT false;
ALTER TABLE username_state ALTER COLUMN last_name   SET DEFAULT '';
ALTER TABLE username_state ALTER COLUMN updated_at  SET DEFAULT now();

ALTER TABLE username_state ALTER COLUMN owner_id    SET NOT NULL;
ALTER TABLE username_state ALTER COLUMN template    SET NOT NULL;
ALTER TABLE username_state ALTER COLUMN mood        SET NOT NULL;
ALTER TABLE username_state ALTER COLUMN custom_text SET NOT NULL;
ALTER TABLE username_state ALTER COLUMN is_active   SET NOT NULL;
ALTER TABLE username_state ALTER COLUMN last_name   SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_username_state_owner ON username_state (owner_id);

ALTER TABLE username_state ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_username_state" ON username_state;
DROP POLICY IF EXISTS "anon_update_username_state" ON username_state;
DROP POLICY IF EXISTS "anon_delete_username_state" ON username_state;
DROP POLICY IF EXISTS "anon_select_username_state" ON username_state;
CREATE POLICY "anon_select_username_state" ON username_state FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 5. bot_logs ────────────────────────────────────────────────────────────
-- Writer/reader: backend/db/client.py log/list_logs/count_logs/clean_logs
-- (also backend/runtime/startup_check.py, dormant).
-- level has NO CHECK: 20260714111706 supersedes the ('INFO','WARN','ERROR')
-- CHECK from 20260712234229.
CREATE TABLE IF NOT EXISTS bot_logs (
    id         bigserial    PRIMARY KEY,
    owner_id   bigint       NOT NULL,
    level      text         NOT NULL DEFAULT 'INFO',
    message    text         NOT NULL,
    context    jsonb        DEFAULT '{}',
    created_at timestamptz  DEFAULT now()
);

ALTER TABLE bot_logs ADD COLUMN IF NOT EXISTS id         bigserial;
ALTER TABLE bot_logs ADD COLUMN IF NOT EXISTS owner_id   bigint      NOT NULL DEFAULT 0;
ALTER TABLE bot_logs ADD COLUMN IF NOT EXISTS level      text        NOT NULL DEFAULT 'INFO';
ALTER TABLE bot_logs ADD COLUMN IF NOT EXISTS message    text;
ALTER TABLE bot_logs ADD COLUMN IF NOT EXISTS context    jsonb       DEFAULT '{}';
ALTER TABLE bot_logs ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();

UPDATE bot_logs SET owner_id   = 0      WHERE owner_id   IS NULL;
UPDATE bot_logs SET level      = 'INFO' WHERE level      IS NULL;
UPDATE bot_logs SET message    = ''     WHERE message    IS NULL;
UPDATE bot_logs SET context    = '{}'   WHERE context    IS NULL;
UPDATE bot_logs SET created_at = now()  WHERE created_at IS NULL;

ALTER TABLE bot_logs ALTER COLUMN level      SET DEFAULT 'INFO';
ALTER TABLE bot_logs ALTER COLUMN context    SET DEFAULT '{}';
ALTER TABLE bot_logs ALTER COLUMN created_at SET DEFAULT now();

ALTER TABLE bot_logs ALTER COLUMN owner_id SET NOT NULL;
ALTER TABLE bot_logs ALTER COLUMN level    SET NOT NULL;
ALTER TABLE bot_logs ALTER COLUMN message  SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_bot_logs_owner      ON bot_logs (owner_id);
CREATE INDEX IF NOT EXISTS idx_bot_logs_created_at ON bot_logs (created_at DESC);

ALTER TABLE bot_logs ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_bot_logs" ON bot_logs;
DROP POLICY IF EXISTS "anon_update_bot_logs" ON bot_logs;
DROP POLICY IF EXISTS "anon_delete_bot_logs" ON bot_logs;
DROP POLICY IF EXISTS "anon_select_bot_logs" ON bot_logs;
CREATE POLICY "anon_select_bot_logs" ON bot_logs FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 6. panel_settings ──────────────────────────────────────────────────────
-- Sole accessor: backend/services/panel_settings_repository.py (key='global').
-- Column set = the 12 settings_service._DEFAULTS settings + the two
-- legacy-orphan columns (update_stale_seconds, ghost_seen_retention_seconds)
-- preserved additively. CHECK ranges mirror settings_service._VALIDATORS.
--
-- This is the SECOND instance of the drift class: 20260726143924 created only
-- (key, auto_close_enabled, updated_at), so the CHECK blocks further down
-- referenced columns a silent no-op CREATE never added.
CREATE TABLE IF NOT EXISTS panel_settings (
    key                          text        PRIMARY KEY,
    auto_close_enabled           boolean     NOT NULL DEFAULT true,
    auto_close_delay             integer     NOT NULL DEFAULT 120,
    max_deep_save_mb             integer     NOT NULL DEFAULT 50,
    delete_batch_size            integer     NOT NULL DEFAULT 100,
    log_retention_days           integer     NOT NULL DEFAULT 7,
    panel_timeout_seconds        integer     NOT NULL DEFAULT 300,
    allow_multiple_panels        boolean     NOT NULL DEFAULT false,
    reuse_existing_panel         boolean     NOT NULL DEFAULT true,
    language                     text        NOT NULL DEFAULT 'en',
    debug_callbacks              boolean     NOT NULL DEFAULT false,
    owner_only                   boolean     NOT NULL DEFAULT true,
    dashboard_font               text        NOT NULL DEFAULT 'default',
    update_stale_seconds         integer     NOT NULL DEFAULT 300,
    ghost_seen_retention_seconds bigint      NOT NULL DEFAULT 2592000,
    updated_at                   timestamptz DEFAULT now()
);

ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS key                          text;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS auto_close_enabled           boolean     NOT NULL DEFAULT true;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS auto_close_delay             integer     NOT NULL DEFAULT 120;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS max_deep_save_mb             integer     NOT NULL DEFAULT 50;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS delete_batch_size            integer     NOT NULL DEFAULT 100;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS log_retention_days           integer     NOT NULL DEFAULT 7;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS panel_timeout_seconds        integer     NOT NULL DEFAULT 300;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS allow_multiple_panels        boolean     NOT NULL DEFAULT false;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS reuse_existing_panel         boolean     NOT NULL DEFAULT true;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS language                     text        NOT NULL DEFAULT 'en';
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS debug_callbacks              boolean     NOT NULL DEFAULT false;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS owner_only                   boolean     NOT NULL DEFAULT true;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS dashboard_font               text        NOT NULL DEFAULT 'default';
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS update_stale_seconds         integer     NOT NULL DEFAULT 300;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS ghost_seen_retention_seconds bigint      NOT NULL DEFAULT 2592000;
ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS updated_at                   timestamptz DEFAULT now();

UPDATE panel_settings SET auto_close_enabled           = true      WHERE auto_close_enabled           IS NULL;
UPDATE panel_settings SET auto_close_delay             = 120       WHERE auto_close_delay             IS NULL;
UPDATE panel_settings SET max_deep_save_mb             = 50        WHERE max_deep_save_mb             IS NULL;
UPDATE panel_settings SET delete_batch_size            = 100       WHERE delete_batch_size            IS NULL;
UPDATE panel_settings SET log_retention_days           = 7         WHERE log_retention_days           IS NULL;
UPDATE panel_settings SET panel_timeout_seconds        = 300       WHERE panel_timeout_seconds        IS NULL;
UPDATE panel_settings SET allow_multiple_panels        = false     WHERE allow_multiple_panels        IS NULL;
UPDATE panel_settings SET reuse_existing_panel         = true      WHERE reuse_existing_panel         IS NULL;
UPDATE panel_settings SET language                     = 'en'      WHERE language                     IS NULL;
UPDATE panel_settings SET debug_callbacks              = false     WHERE debug_callbacks              IS NULL;
UPDATE panel_settings SET owner_only                   = true      WHERE owner_only                   IS NULL;
UPDATE panel_settings SET dashboard_font               = 'default' WHERE dashboard_font               IS NULL;
UPDATE panel_settings SET update_stale_seconds         = 300       WHERE update_stale_seconds         IS NULL;
UPDATE panel_settings SET ghost_seen_retention_seconds = 2592000   WHERE ghost_seen_retention_seconds IS NULL;
UPDATE panel_settings SET updated_at                   = now()     WHERE updated_at                   IS NULL;

ALTER TABLE panel_settings ALTER COLUMN auto_close_enabled           SET DEFAULT true;
ALTER TABLE panel_settings ALTER COLUMN auto_close_delay             SET DEFAULT 120;
ALTER TABLE panel_settings ALTER COLUMN max_deep_save_mb             SET DEFAULT 50;
ALTER TABLE panel_settings ALTER COLUMN delete_batch_size            SET DEFAULT 100;
ALTER TABLE panel_settings ALTER COLUMN log_retention_days           SET DEFAULT 7;
ALTER TABLE panel_settings ALTER COLUMN panel_timeout_seconds        SET DEFAULT 300;
ALTER TABLE panel_settings ALTER COLUMN allow_multiple_panels        SET DEFAULT false;
ALTER TABLE panel_settings ALTER COLUMN reuse_existing_panel         SET DEFAULT true;
ALTER TABLE panel_settings ALTER COLUMN language                     SET DEFAULT 'en';
ALTER TABLE panel_settings ALTER COLUMN debug_callbacks              SET DEFAULT false;
ALTER TABLE panel_settings ALTER COLUMN owner_only                   SET DEFAULT true;
ALTER TABLE panel_settings ALTER COLUMN dashboard_font               SET DEFAULT 'default';
ALTER TABLE panel_settings ALTER COLUMN update_stale_seconds         SET DEFAULT 300;
ALTER TABLE panel_settings ALTER COLUMN ghost_seen_retention_seconds SET DEFAULT 2592000;
ALTER TABLE panel_settings ALTER COLUMN updated_at                   SET DEFAULT now();

ALTER TABLE panel_settings ALTER COLUMN auto_close_enabled           SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN auto_close_delay             SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN max_deep_save_mb             SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN delete_batch_size            SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN log_retention_days           SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN panel_timeout_seconds        SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN allow_multiple_panels        SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN reuse_existing_panel         SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN language                     SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN debug_callbacks              SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN owner_only                   SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN dashboard_font               SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN update_stale_seconds         SET NOT NULL;
ALTER TABLE panel_settings ALTER COLUMN ghost_seen_retention_seconds SET NOT NULL;

-- panel_settings CHECK constraints. Every one is data-guarded: a legacy row
-- outside the documented range yields a WARNING naming the count instead of
-- aborting the whole script (mirrors settings_service._VALIDATORS).
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_auto_close_delay_check') THEN
        IF NOT EXISTS (SELECT 1 FROM panel_settings WHERE auto_close_delay IS NULL OR auto_close_delay NOT BETWEEN 5 AND 3600) THEN
            ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_auto_close_delay_check
                CHECK (auto_close_delay BETWEEN 5 AND 3600);
        ELSE
            RAISE WARNING 'panel_settings_auto_close_delay_check NOT added - % row(s) have auto_close_delay outside 5..3600.',
                (SELECT count(*) FROM panel_settings WHERE auto_close_delay IS NULL OR auto_close_delay NOT BETWEEN 5 AND 3600);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_max_deep_save_mb_check') THEN
        IF NOT EXISTS (SELECT 1 FROM panel_settings WHERE max_deep_save_mb IS NULL OR max_deep_save_mb NOT BETWEEN 1 AND 500) THEN
            ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_max_deep_save_mb_check
                CHECK (max_deep_save_mb BETWEEN 1 AND 500);
        ELSE
            RAISE WARNING 'panel_settings_max_deep_save_mb_check NOT added - % row(s) have max_deep_save_mb outside 1..500.',
                (SELECT count(*) FROM panel_settings WHERE max_deep_save_mb IS NULL OR max_deep_save_mb NOT BETWEEN 1 AND 500);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_delete_batch_size_check') THEN
        IF NOT EXISTS (SELECT 1 FROM panel_settings WHERE delete_batch_size IS NULL OR delete_batch_size NOT BETWEEN 1 AND 1000) THEN
            ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_delete_batch_size_check
                CHECK (delete_batch_size BETWEEN 1 AND 1000);
        ELSE
            RAISE WARNING 'panel_settings_delete_batch_size_check NOT added - % row(s) have delete_batch_size outside 1..1000.',
                (SELECT count(*) FROM panel_settings WHERE delete_batch_size IS NULL OR delete_batch_size NOT BETWEEN 1 AND 1000);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_log_retention_days_check') THEN
        IF NOT EXISTS (SELECT 1 FROM panel_settings WHERE log_retention_days IS NULL OR log_retention_days NOT BETWEEN 1 AND 365) THEN
            ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_log_retention_days_check
                CHECK (log_retention_days BETWEEN 1 AND 365);
        ELSE
            RAISE WARNING 'panel_settings_log_retention_days_check NOT added - % row(s) have log_retention_days outside 1..365.',
                (SELECT count(*) FROM panel_settings WHERE log_retention_days IS NULL OR log_retention_days NOT BETWEEN 1 AND 365);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_panel_timeout_seconds_check') THEN
        IF NOT EXISTS (SELECT 1 FROM panel_settings WHERE panel_timeout_seconds IS NULL OR panel_timeout_seconds NOT BETWEEN 30 AND 86400) THEN
            ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_panel_timeout_seconds_check
                CHECK (panel_timeout_seconds BETWEEN 30 AND 86400);
        ELSE
            RAISE WARNING 'panel_settings_panel_timeout_seconds_check NOT added - % row(s) have panel_timeout_seconds outside 30..86400.',
                (SELECT count(*) FROM panel_settings WHERE panel_timeout_seconds IS NULL OR panel_timeout_seconds NOT BETWEEN 30 AND 86400);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_language_check') THEN
        IF NOT EXISTS (SELECT 1 FROM panel_settings WHERE language IS NULL OR length(btrim(language)) = 0) THEN
            ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_language_check
                CHECK (length(btrim(language)) > 0);
        ELSE
            RAISE WARNING 'panel_settings_language_check NOT added - % row(s) have a blank language.',
                (SELECT count(*) FROM panel_settings WHERE language IS NULL OR length(btrim(language)) = 0);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_dashboard_font_check') THEN
        IF NOT EXISTS (SELECT 1 FROM panel_settings WHERE dashboard_font IS NULL OR dashboard_font NOT IN (
                'default', 'serif_bold', 'serif_italic', 'serif_bold_italic',
                'sans', 'sans_bold', 'sans_italic', 'sans_bold_italic',
                'script', 'script_bold', 'fraktur', 'fraktur_bold',
                'double_struck', 'mono', 'small_caps', 'circled',
                'circled_dark', 'fullwidth', 'parenthesized', 'underline',
                'strikethrough', 'overline', 'wavy_underline'
            )) THEN
            ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_dashboard_font_check
                CHECK (dashboard_font IN (
                    'default', 'serif_bold', 'serif_italic', 'serif_bold_italic',
                    'sans', 'sans_bold', 'sans_italic', 'sans_bold_italic',
                    'script', 'script_bold', 'fraktur', 'fraktur_bold',
                    'double_struck', 'mono', 'small_caps', 'circled',
                    'circled_dark', 'fullwidth', 'parenthesized', 'underline',
                    'strikethrough', 'overline', 'wavy_underline'
                ));
        ELSE
            RAISE WARNING 'panel_settings_dashboard_font_check NOT added - % row(s) have a dashboard_font outside the 23-key list.',
                (SELECT count(*) FROM panel_settings WHERE dashboard_font IS NULL OR dashboard_font NOT IN (
                    'default', 'serif_bold', 'serif_italic', 'serif_bold_italic',
                    'sans', 'sans_bold', 'sans_italic', 'sans_bold_italic',
                    'script', 'script_bold', 'fraktur', 'fraktur_bold',
                    'double_struck', 'mono', 'small_caps', 'circled',
                    'circled_dark', 'fullwidth', 'parenthesized', 'underline',
                    'strikethrough', 'overline', 'wavy_underline'
                ));
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'panel_settings_ghost_seen_retention_seconds_check') THEN
        IF NOT EXISTS (SELECT 1 FROM panel_settings WHERE ghost_seen_retention_seconds IS NULL
                       OR NOT (ghost_seen_retention_seconds = 0
                               OR ghost_seen_retention_seconds BETWEEN 300 AND 31536000)) THEN
            ALTER TABLE panel_settings ADD CONSTRAINT panel_settings_ghost_seen_retention_seconds_check
                CHECK (ghost_seen_retention_seconds = 0
                       OR ghost_seen_retention_seconds BETWEEN 300 AND 31536000);
        ELSE
            RAISE WARNING 'panel_settings_ghost_seen_retention_seconds_check NOT added - % row(s) have an out-of-range retention window.',
                (SELECT count(*) FROM panel_settings WHERE ghost_seen_retention_seconds IS NULL
                   OR NOT (ghost_seen_retention_seconds = 0
                           OR ghost_seen_retention_seconds BETWEEN 300 AND 31536000));
        END IF;
    END IF;
END $$;

-- Required singleton row: repository updates target key='global' and
-- silently no-op without it. `ON CONFLICT DO NOTHING` is deliberately
-- targetless so it cannot fail on a table whose unique index on `key` was not
-- (yet) reconcilable.
INSERT INTO panel_settings (key) VALUES ('global')
ON CONFLICT DO NOTHING;

ALTER TABLE panel_settings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_panel_settings" ON panel_settings;
DROP POLICY IF EXISTS "anon_update_panel_settings" ON panel_settings;
DROP POLICY IF EXISTS "anon_delete_panel_settings" ON panel_settings;
DROP POLICY IF EXISTS "anon_select_panel_settings" ON panel_settings;
CREATE POLICY "anon_select_panel_settings" ON panel_settings FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 7. bot_settings ────────────────────────────────────────────────────────
-- Live consumer: backend/services/ghost_seen_v2.py (ghost_seen_allowed_chats
-- KV row, created at runtime — deliberately NOT seeded here).
-- The five seed rows below are legacy defaults from migration 20260729213959,
-- consumed by no current reader; retained verbatim for migration fidelity.
--
-- THE REPORTED PRODUCTION FAILURE. A live database whose bot_settings already
-- existed without `value_type` made the CREATE a silent no-op, so the INSERT
-- below raised ERROR 42703 (`column "value_type" of relation "bot_settings"
-- does not exist`). The reconciliation block fixes the class, not the symptom.
CREATE TABLE IF NOT EXISTS bot_settings (
    key         text        PRIMARY KEY,
    value       text        NOT NULL,
    value_type  text        NOT NULL DEFAULT 'str',
    updated_at  timestamptz DEFAULT now()
);

ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS key        text;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS value      text;
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS value_type text        NOT NULL DEFAULT 'str';
ALTER TABLE bot_settings ADD COLUMN IF NOT EXISTS updated_at timestamptz DEFAULT now();

UPDATE bot_settings SET value      = ''    WHERE value      IS NULL;
UPDATE bot_settings SET value_type = 'str' WHERE value_type IS NULL;
UPDATE bot_settings SET updated_at = now() WHERE updated_at IS NULL;

ALTER TABLE bot_settings ALTER COLUMN value_type SET DEFAULT 'str';
ALTER TABLE bot_settings ALTER COLUMN updated_at SET DEFAULT now();

ALTER TABLE bot_settings ALTER COLUMN value      SET NOT NULL;
ALTER TABLE bot_settings ALTER COLUMN value_type SET NOT NULL;

INSERT INTO bot_settings (key, value, value_type) VALUES
    ('auto_close_enabled', 'true', 'bool'),
    ('panel_auto_close_seconds', '120', 'int'),
    ('max_deep_save_mb', '50', 'int'),
    ('delete_batch_size', '100', 'int'),
    ('log_cleanup_days', '7', 'int')
ON CONFLICT DO NOTHING;

ALTER TABLE bot_settings ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_bot_settings" ON bot_settings;
DROP POLICY IF EXISTS "anon_update_bot_settings" ON bot_settings;
DROP POLICY IF EXISTS "anon_delete_bot_settings" ON bot_settings;
DROP POLICY IF EXISTS "anon_select_bot_settings" ON bot_settings;
CREATE POLICY "anon_select_bot_settings" ON bot_settings FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 8. ai_config ───────────────────────────────────────────────────────────
-- Writer/reader: backend/ai/config_store.py (get/save/record_request).
-- trigger_en / trigger_fa are nullable text; the writer normalizes empty
-- strings to NULL. UNIQUE(owner_id) backs the writer's select-then-
-- insert/update flow.
--
-- show_question / stt_model / stt_language / stt_passes are written by EVERY
-- config_store upsert payload (migrations 20260913000000 and 20260917000001).
-- They are therefore part of the canonical contract: omitting them made a
-- database built only from this script reject the whole upsert and silently
-- lose AI settings on restart.
CREATE TABLE IF NOT EXISTS ai_config (
    id              bigserial    PRIMARY KEY,
    owner_id        bigint       NOT NULL UNIQUE,
    provider        text         DEFAULT '',
    model           text         DEFAULT '',
    temperature     real         DEFAULT 1.0,
    max_tokens      integer      DEFAULT 4096,
    system_prompt   text         DEFAULT '',
    history_budget  integer      DEFAULT 4000,
    is_configured   boolean      DEFAULT false,
    trigger_en      text         DEFAULT NULL,
    trigger_fa      text         DEFAULT NULL,
    show_question   boolean      NOT NULL DEFAULT false,
    stt_model       text         DEFAULT NULL,
    stt_language    text         DEFAULT NULL,
    stt_passes      integer      NOT NULL DEFAULT 1,
    last_request_at timestamptz,
    last_latency_ms real         DEFAULT 0,
    created_at      timestamptz  DEFAULT now(),
    updated_at      timestamptz  DEFAULT now()
);

ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS id              bigserial;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS owner_id        bigint      NOT NULL DEFAULT 0;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS provider        text        DEFAULT '';
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS model           text        DEFAULT '';
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS temperature     real        DEFAULT 1.0;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS max_tokens      integer     DEFAULT 4096;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS system_prompt   text        DEFAULT '';
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS history_budget  integer     DEFAULT 4000;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS is_configured   boolean     DEFAULT false;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS trigger_en      text        DEFAULT NULL;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS trigger_fa      text        DEFAULT NULL;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS show_question   boolean     NOT NULL DEFAULT false;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS stt_model       text        DEFAULT NULL;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS stt_language    text        DEFAULT NULL;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS stt_passes      integer     NOT NULL DEFAULT 1;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS last_request_at timestamptz;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS last_latency_ms real        DEFAULT 0;
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS created_at      timestamptz DEFAULT now();
ALTER TABLE ai_config ADD COLUMN IF NOT EXISTS updated_at      timestamptz DEFAULT now();

UPDATE ai_config SET owner_id        = 0     WHERE owner_id        IS NULL;
UPDATE ai_config SET provider        = ''    WHERE provider        IS NULL;
UPDATE ai_config SET model           = ''    WHERE model           IS NULL;
UPDATE ai_config SET temperature     = 1.0   WHERE temperature     IS NULL;
UPDATE ai_config SET max_tokens      = 4096  WHERE max_tokens      IS NULL;
UPDATE ai_config SET system_prompt   = ''    WHERE system_prompt   IS NULL;
UPDATE ai_config SET history_budget  = 4000  WHERE history_budget  IS NULL;
UPDATE ai_config SET is_configured   = false WHERE is_configured   IS NULL;
UPDATE ai_config SET show_question   = false WHERE show_question   IS NULL;
UPDATE ai_config SET stt_passes      = 1     WHERE stt_passes      IS NULL;
UPDATE ai_config SET last_latency_ms = 0     WHERE last_latency_ms IS NULL;
UPDATE ai_config SET created_at      = now() WHERE created_at      IS NULL;
UPDATE ai_config SET updated_at      = now() WHERE updated_at      IS NULL;

ALTER TABLE ai_config ALTER COLUMN provider        SET DEFAULT '';
ALTER TABLE ai_config ALTER COLUMN model           SET DEFAULT '';
ALTER TABLE ai_config ALTER COLUMN temperature     SET DEFAULT 1.0;
ALTER TABLE ai_config ALTER COLUMN max_tokens      SET DEFAULT 4096;
ALTER TABLE ai_config ALTER COLUMN system_prompt   SET DEFAULT '';
ALTER TABLE ai_config ALTER COLUMN history_budget  SET DEFAULT 4000;
ALTER TABLE ai_config ALTER COLUMN is_configured   SET DEFAULT false;
ALTER TABLE ai_config ALTER COLUMN show_question   SET DEFAULT false;
ALTER TABLE ai_config ALTER COLUMN stt_passes      SET DEFAULT 1;
ALTER TABLE ai_config ALTER COLUMN last_latency_ms SET DEFAULT 0;
ALTER TABLE ai_config ALTER COLUMN created_at      SET DEFAULT now();
ALTER TABLE ai_config ALTER COLUMN updated_at      SET DEFAULT now();

ALTER TABLE ai_config ALTER COLUMN owner_id      SET NOT NULL;
ALTER TABLE ai_config ALTER COLUMN show_question SET NOT NULL;
ALTER TABLE ai_config ALTER COLUMN stt_passes    SET NOT NULL;

-- stt_passes is the only ranged ai_config column (1..3, mirroring the AI
-- Settings control plane). Data-guarded like the panel_settings CHECKs.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_config_stt_passes_range') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_config WHERE stt_passes IS NULL OR stt_passes NOT BETWEEN 1 AND 3) THEN
            ALTER TABLE ai_config ADD CONSTRAINT ai_config_stt_passes_range
                CHECK (stt_passes BETWEEN 1 AND 3);
        ELSE
            RAISE WARNING 'ai_config_stt_passes_range NOT added - % row(s) have stt_passes outside 1..3.',
                (SELECT count(*) FROM ai_config WHERE stt_passes IS NULL OR stt_passes NOT BETWEEN 1 AND 3);
        END IF;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ai_config_owner ON ai_config (owner_id);

ALTER TABLE ai_config ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_config" ON ai_config;
DROP POLICY IF EXISTS "anon_update_ai_config" ON ai_config;
DROP POLICY IF EXISTS "anon_delete_ai_config" ON ai_config;
DROP POLICY IF EXISTS "anon_select_ai_config" ON ai_config;
CREATE POLICY "anon_select_ai_config" ON ai_config FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 9. ai_sessions ─────────────────────────────────────────────────────────
-- Writer/reader: backend/ai/persistence.py (create/update/get_session).
CREATE TABLE IF NOT EXISTS ai_sessions (
    id            bigserial    PRIMARY KEY,
    session_id    text         NOT NULL UNIQUE,
    owner_id      bigint       NOT NULL,
    provider      text         DEFAULT '',
    model         text         DEFAULT '',
    status        text         DEFAULT 'active',
    total_tokens  integer      DEFAULT 0,
    message_count integer      DEFAULT 0,
    created_at    timestamptz  DEFAULT now(),
    updated_at    timestamptz  DEFAULT now()
);

ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS id            bigserial;
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS session_id    text;
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS owner_id      bigint      NOT NULL DEFAULT 0;
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS provider      text        DEFAULT '';
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS model         text        DEFAULT '';
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS status        text        DEFAULT 'active';
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS total_tokens  integer     DEFAULT 0;
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS message_count integer     DEFAULT 0;
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS created_at    timestamptz DEFAULT now();
ALTER TABLE ai_sessions ADD COLUMN IF NOT EXISTS updated_at    timestamptz DEFAULT now();

UPDATE ai_sessions SET session_id    = 'recovered-' || id WHERE session_id    IS NULL;
UPDATE ai_sessions SET owner_id      = 0         WHERE owner_id      IS NULL;
UPDATE ai_sessions SET provider      = ''        WHERE provider      IS NULL;
UPDATE ai_sessions SET model         = ''        WHERE model         IS NULL;
UPDATE ai_sessions SET status        = 'active'  WHERE status        IS NULL;
UPDATE ai_sessions SET total_tokens  = 0         WHERE total_tokens  IS NULL;
UPDATE ai_sessions SET message_count = 0         WHERE message_count IS NULL;
UPDATE ai_sessions SET created_at    = now()     WHERE created_at    IS NULL;
UPDATE ai_sessions SET updated_at    = now()     WHERE updated_at    IS NULL;

ALTER TABLE ai_sessions ALTER COLUMN provider      SET DEFAULT '';
ALTER TABLE ai_sessions ALTER COLUMN model         SET DEFAULT '';
ALTER TABLE ai_sessions ALTER COLUMN status        SET DEFAULT 'active';
ALTER TABLE ai_sessions ALTER COLUMN total_tokens  SET DEFAULT 0;
ALTER TABLE ai_sessions ALTER COLUMN message_count SET DEFAULT 0;
ALTER TABLE ai_sessions ALTER COLUMN created_at    SET DEFAULT now();
ALTER TABLE ai_sessions ALTER COLUMN updated_at    SET DEFAULT now();

ALTER TABLE ai_sessions ALTER COLUMN session_id SET NOT NULL;
ALTER TABLE ai_sessions ALTER COLUMN owner_id   SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_sessions_status_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_sessions WHERE status IS NULL OR status NOT IN ('active', 'completed', 'error', 'closed')) THEN
            ALTER TABLE ai_sessions ADD CONSTRAINT ai_sessions_status_check
                CHECK (status IN ('active', 'completed', 'error', 'closed'));
        ELSE
            RAISE WARNING 'ai_sessions_status_check NOT added - % row(s) have an unrecognized status.',
                (SELECT count(*) FROM ai_sessions WHERE status IS NULL OR status NOT IN ('active', 'completed', 'error', 'closed'));
        END IF;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ai_sessions_owner      ON ai_sessions (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_sessions_session_id ON ai_sessions (session_id);

ALTER TABLE ai_sessions ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_sessions" ON ai_sessions;
DROP POLICY IF EXISTS "anon_update_ai_sessions" ON ai_sessions;
DROP POLICY IF EXISTS "anon_delete_ai_sessions" ON ai_sessions;
DROP POLICY IF EXISTS "anon_select_ai_sessions" ON ai_sessions;
CREATE POLICY "anon_select_ai_sessions" ON ai_sessions FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 10. ai_messages ────────────────────────────────────────────────────────
-- Writer/reader: backend/ai/persistence.py (add_message/get_messages).
CREATE TABLE IF NOT EXISTS ai_messages (
    id          bigserial    PRIMARY KEY,
    session_id  text         NOT NULL,
    owner_id    bigint       NOT NULL,
    role        text         NOT NULL DEFAULT 'user',
    content     text         NOT NULL DEFAULT '',
    token_count integer      DEFAULT 0,
    provider    text         DEFAULT '',
    model       text         DEFAULT '',
    created_at  timestamptz  DEFAULT now()
);

ALTER TABLE ai_messages ADD COLUMN IF NOT EXISTS id          bigserial;
ALTER TABLE ai_messages ADD COLUMN IF NOT EXISTS session_id  text;
ALTER TABLE ai_messages ADD COLUMN IF NOT EXISTS owner_id    bigint      NOT NULL DEFAULT 0;
ALTER TABLE ai_messages ADD COLUMN IF NOT EXISTS role        text        NOT NULL DEFAULT 'user';
ALTER TABLE ai_messages ADD COLUMN IF NOT EXISTS content     text        NOT NULL DEFAULT '';
ALTER TABLE ai_messages ADD COLUMN IF NOT EXISTS token_count integer     DEFAULT 0;
ALTER TABLE ai_messages ADD COLUMN IF NOT EXISTS provider    text        DEFAULT '';
ALTER TABLE ai_messages ADD COLUMN IF NOT EXISTS model       text        DEFAULT '';
ALTER TABLE ai_messages ADD COLUMN IF NOT EXISTS created_at  timestamptz DEFAULT now();

UPDATE ai_messages SET session_id  = ''     WHERE session_id  IS NULL;
UPDATE ai_messages SET owner_id    = 0      WHERE owner_id    IS NULL;
UPDATE ai_messages SET role        = 'user' WHERE role        IS NULL;
UPDATE ai_messages SET content     = ''     WHERE content     IS NULL;
UPDATE ai_messages SET token_count = 0      WHERE token_count IS NULL;
UPDATE ai_messages SET provider    = ''     WHERE provider    IS NULL;
UPDATE ai_messages SET model       = ''     WHERE model       IS NULL;
UPDATE ai_messages SET created_at  = now()  WHERE created_at  IS NULL;

ALTER TABLE ai_messages ALTER COLUMN role        SET DEFAULT 'user';
ALTER TABLE ai_messages ALTER COLUMN content     SET DEFAULT '';
ALTER TABLE ai_messages ALTER COLUMN token_count SET DEFAULT 0;
ALTER TABLE ai_messages ALTER COLUMN provider    SET DEFAULT '';
ALTER TABLE ai_messages ALTER COLUMN model       SET DEFAULT '';
ALTER TABLE ai_messages ALTER COLUMN created_at  SET DEFAULT now();

ALTER TABLE ai_messages ALTER COLUMN session_id SET NOT NULL;
ALTER TABLE ai_messages ALTER COLUMN owner_id   SET NOT NULL;
ALTER TABLE ai_messages ALTER COLUMN role       SET NOT NULL;
ALTER TABLE ai_messages ALTER COLUMN content    SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_messages_role_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_messages WHERE role IS NULL OR role NOT IN ('system', 'user', 'assistant', 'tool')) THEN
            ALTER TABLE ai_messages ADD CONSTRAINT ai_messages_role_check
                CHECK (role IN ('system', 'user', 'assistant', 'tool'));
        ELSE
            RAISE WARNING 'ai_messages_role_check NOT added - % row(s) have an unrecognized role.',
                (SELECT count(*) FROM ai_messages WHERE role IS NULL OR role NOT IN ('system', 'user', 'assistant', 'tool'));
        END IF;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ai_messages_session ON ai_messages (session_id);
CREATE INDEX IF NOT EXISTS idx_ai_messages_owner   ON ai_messages (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_messages_created ON ai_messages (created_at DESC);

ALTER TABLE ai_messages ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_messages" ON ai_messages;
DROP POLICY IF EXISTS "anon_update_ai_messages" ON ai_messages;
DROP POLICY IF EXISTS "anon_delete_ai_messages" ON ai_messages;
DROP POLICY IF EXISTS "anon_select_ai_messages" ON ai_messages;
CREATE POLICY "anon_select_ai_messages" ON ai_messages FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 11. ai_memories ────────────────────────────────────────────────────────
-- Writer/reader: backend/ai/persistence.py
-- (save/query/delete_expired/delete/count_memories).
CREATE TABLE IF NOT EXISTS ai_memories (
    id          bigserial    PRIMARY KEY,
    owner_id    bigint       NOT NULL,
    tier        text         NOT NULL DEFAULT 'long',
    category    text         NOT NULL DEFAULT 'context',
    content     text         NOT NULL,
    importance  real         DEFAULT 0.5,
    expires_at  timestamptz,
    metadata    jsonb        DEFAULT '{}',
    created_at  timestamptz  DEFAULT now()
);

ALTER TABLE ai_memories ADD COLUMN IF NOT EXISTS id         bigserial;
ALTER TABLE ai_memories ADD COLUMN IF NOT EXISTS owner_id   bigint      NOT NULL DEFAULT 0;
ALTER TABLE ai_memories ADD COLUMN IF NOT EXISTS tier       text        NOT NULL DEFAULT 'long';
ALTER TABLE ai_memories ADD COLUMN IF NOT EXISTS category   text        NOT NULL DEFAULT 'context';
ALTER TABLE ai_memories ADD COLUMN IF NOT EXISTS content    text;
ALTER TABLE ai_memories ADD COLUMN IF NOT EXISTS importance real        DEFAULT 0.5;
ALTER TABLE ai_memories ADD COLUMN IF NOT EXISTS expires_at timestamptz;
ALTER TABLE ai_memories ADD COLUMN IF NOT EXISTS metadata   jsonb       DEFAULT '{}';
ALTER TABLE ai_memories ADD COLUMN IF NOT EXISTS created_at timestamptz DEFAULT now();

UPDATE ai_memories SET owner_id   = 0         WHERE owner_id   IS NULL;
UPDATE ai_memories SET tier       = 'long'    WHERE tier       IS NULL;
UPDATE ai_memories SET category   = 'context' WHERE category   IS NULL;
UPDATE ai_memories SET content    = ''        WHERE content    IS NULL;
UPDATE ai_memories SET importance = 0.5       WHERE importance IS NULL;
UPDATE ai_memories SET metadata   = '{}'      WHERE metadata   IS NULL;
UPDATE ai_memories SET created_at = now()     WHERE created_at IS NULL;

ALTER TABLE ai_memories ALTER COLUMN tier       SET DEFAULT 'long';
ALTER TABLE ai_memories ALTER COLUMN category   SET DEFAULT 'context';
ALTER TABLE ai_memories ALTER COLUMN importance SET DEFAULT 0.5;
ALTER TABLE ai_memories ALTER COLUMN metadata   SET DEFAULT '{}';
ALTER TABLE ai_memories ALTER COLUMN created_at SET DEFAULT now();

ALTER TABLE ai_memories ALTER COLUMN owner_id SET NOT NULL;
ALTER TABLE ai_memories ALTER COLUMN tier     SET NOT NULL;
ALTER TABLE ai_memories ALTER COLUMN category SET NOT NULL;
ALTER TABLE ai_memories ALTER COLUMN content  SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_memories_tier_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_memories WHERE tier IS NULL OR tier NOT IN ('short', 'long', 'permanent')) THEN
            ALTER TABLE ai_memories ADD CONSTRAINT ai_memories_tier_check
                CHECK (tier IN ('short', 'long', 'permanent'));
        ELSE
            RAISE WARNING 'ai_memories_tier_check NOT added - % row(s) have an unrecognized tier.',
                (SELECT count(*) FROM ai_memories WHERE tier IS NULL OR tier NOT IN ('short', 'long', 'permanent'));
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_memories_category_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_memories WHERE category IS NULL OR category NOT IN ('fact', 'preference', 'context', 'summary', 'instruction')) THEN
            ALTER TABLE ai_memories ADD CONSTRAINT ai_memories_category_check
                CHECK (category IN ('fact', 'preference', 'context', 'summary', 'instruction'));
        ELSE
            RAISE WARNING 'ai_memories_category_check NOT added - % row(s) have an unrecognized category.',
                (SELECT count(*) FROM ai_memories WHERE category IS NULL OR category NOT IN ('fact', 'preference', 'context', 'summary', 'instruction'));
        END IF;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ai_memories_owner      ON ai_memories (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_memories_tier       ON ai_memories (tier);
CREATE INDEX IF NOT EXISTS idx_ai_memories_owner_tier ON ai_memories (owner_id, tier);

ALTER TABLE ai_memories ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_memories" ON ai_memories;
DROP POLICY IF EXISTS "anon_update_ai_memories" ON ai_memories;
DROP POLICY IF EXISTS "anon_delete_ai_memories" ON ai_memories;
DROP POLICY IF EXISTS "anon_select_ai_memories" ON ai_memories;
CREATE POLICY "anon_select_ai_memories" ON ai_memories FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 12. ai_tool_history ────────────────────────────────────────────────────
-- Writer: backend/ai/persistence.py::record_tool_call (INSERT only).
-- result_data is migration-defined but never inserted by current code
-- (legacy-preserved column).
CREATE TABLE IF NOT EXISTS ai_tool_history (
    id             bigserial    PRIMARY KEY,
    owner_id       bigint       NOT NULL,
    session_id     text         DEFAULT '',
    tool_name      text         NOT NULL,
    arguments      jsonb        DEFAULT '{}',
    result_success boolean      DEFAULT false,
    result_message text         DEFAULT '',
    result_data    jsonb        DEFAULT '{}',
    latency_ms     real         DEFAULT 0,
    created_at     timestamptz  DEFAULT now()
);

ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS id             bigserial;
ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS owner_id       bigint      NOT NULL DEFAULT 0;
ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS session_id     text        DEFAULT '';
ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS tool_name      text;
ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS arguments      jsonb       DEFAULT '{}';
ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS result_success boolean     DEFAULT false;
ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS result_message text        DEFAULT '';
ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS result_data    jsonb       DEFAULT '{}';
ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS latency_ms     real        DEFAULT 0;
ALTER TABLE ai_tool_history ADD COLUMN IF NOT EXISTS created_at     timestamptz DEFAULT now();

UPDATE ai_tool_history SET owner_id       = 0         WHERE owner_id       IS NULL;
UPDATE ai_tool_history SET session_id     = ''        WHERE session_id     IS NULL;
UPDATE ai_tool_history SET tool_name      = 'unknown' WHERE tool_name      IS NULL;
UPDATE ai_tool_history SET arguments      = '{}'      WHERE arguments      IS NULL;
UPDATE ai_tool_history SET result_success = false     WHERE result_success IS NULL;
UPDATE ai_tool_history SET result_message = ''        WHERE result_message IS NULL;
UPDATE ai_tool_history SET result_data    = '{}'      WHERE result_data    IS NULL;
UPDATE ai_tool_history SET latency_ms     = 0         WHERE latency_ms     IS NULL;
UPDATE ai_tool_history SET created_at     = now()     WHERE created_at     IS NULL;

ALTER TABLE ai_tool_history ALTER COLUMN session_id     SET DEFAULT '';
ALTER TABLE ai_tool_history ALTER COLUMN arguments      SET DEFAULT '{}';
ALTER TABLE ai_tool_history ALTER COLUMN result_success SET DEFAULT false;
ALTER TABLE ai_tool_history ALTER COLUMN result_message SET DEFAULT '';
ALTER TABLE ai_tool_history ALTER COLUMN result_data    SET DEFAULT '{}';
ALTER TABLE ai_tool_history ALTER COLUMN latency_ms     SET DEFAULT 0;
ALTER TABLE ai_tool_history ALTER COLUMN created_at     SET DEFAULT now();

ALTER TABLE ai_tool_history ALTER COLUMN owner_id  SET NOT NULL;
ALTER TABLE ai_tool_history ALTER COLUMN tool_name SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ai_tool_history_owner   ON ai_tool_history (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_tool_history_created ON ai_tool_history (created_at DESC);

ALTER TABLE ai_tool_history ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_tool_history" ON ai_tool_history;
DROP POLICY IF EXISTS "anon_update_ai_tool_history" ON ai_tool_history;
DROP POLICY IF EXISTS "anon_delete_ai_tool_history" ON ai_tool_history;
DROP POLICY IF EXISTS "anon_select_ai_tool_history" ON ai_tool_history;
CREATE POLICY "anon_select_ai_tool_history" ON ai_tool_history FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 13. ai_usage ───────────────────────────────────────────────────────────
-- Writer/reader: SupabaseUsageRepository
-- (backend/ai/database/usage_repository.py).
-- id is NOT sent by the writer (bigserial); token_source carries the honesty
-- label verbatim (actual / estimated / unavailable).
CREATE TABLE IF NOT EXISTS ai_usage (
    id                bigserial    PRIMARY KEY,
    owner_id          bigint       NOT NULL,
    session_id        text,
    provider          text,
    model             text,
    prompt_tokens     integer      DEFAULT 0,
    completion_tokens integer      DEFAULT 0,
    total_tokens      integer      DEFAULT 0,
    latency_ms        real         DEFAULT 0,
    token_source      text         DEFAULT NULL,
    created_at        timestamptz  DEFAULT now()
);

ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS id                bigserial;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS owner_id          bigint      NOT NULL DEFAULT 0;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS session_id        text;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS provider          text;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS model             text;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS prompt_tokens     integer     DEFAULT 0;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS completion_tokens integer     DEFAULT 0;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS total_tokens      integer     DEFAULT 0;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS latency_ms        real        DEFAULT 0;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS token_source      text        DEFAULT NULL;
ALTER TABLE ai_usage ADD COLUMN IF NOT EXISTS created_at        timestamptz DEFAULT now();

UPDATE ai_usage SET owner_id          = 0     WHERE owner_id          IS NULL;
UPDATE ai_usage SET prompt_tokens     = 0     WHERE prompt_tokens     IS NULL;
UPDATE ai_usage SET completion_tokens = 0     WHERE completion_tokens IS NULL;
UPDATE ai_usage SET total_tokens      = 0     WHERE total_tokens      IS NULL;
UPDATE ai_usage SET latency_ms        = 0     WHERE latency_ms        IS NULL;
UPDATE ai_usage SET created_at        = now() WHERE created_at        IS NULL;

ALTER TABLE ai_usage ALTER COLUMN prompt_tokens     SET DEFAULT 0;
ALTER TABLE ai_usage ALTER COLUMN completion_tokens SET DEFAULT 0;
ALTER TABLE ai_usage ALTER COLUMN total_tokens      SET DEFAULT 0;
ALTER TABLE ai_usage ALTER COLUMN latency_ms        SET DEFAULT 0;
ALTER TABLE ai_usage ALTER COLUMN created_at        SET DEFAULT now();

ALTER TABLE ai_usage ALTER COLUMN owner_id SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ai_usage_owner      ON ai_usage (owner_id);
CREATE INDEX IF NOT EXISTS idx_ai_usage_created_at ON ai_usage (created_at);

ALTER TABLE ai_usage ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_usage" ON ai_usage;
DROP POLICY IF EXISTS "anon_update_ai_usage" ON ai_usage;
DROP POLICY IF EXISTS "anon_delete_ai_usage" ON ai_usage;
DROP POLICY IF EXISTS "anon_select_ai_usage" ON ai_usage;
CREATE POLICY "anon_select_ai_usage" ON ai_usage FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 14. ai_provider_stats ──────────────────────────────────────────────────
-- Writer/reader: SupabaseProviderStatsRepository
-- (backend/ai/database/provider_stats_repository.py) which upserts with
-- on_conflict="provider_name,owner_id" — the composite PRIMARY KEY below IS
-- that conflict target.
CREATE TABLE IF NOT EXISTS ai_provider_stats (
    provider_name            text        NOT NULL,
    owner_id                 bigint      NOT NULL DEFAULT 0,
    total_requests           integer     NOT NULL DEFAULT 0,
    successful_requests      integer     NOT NULL DEFAULT 0,
    failed_requests          integer     NOT NULL DEFAULT 0,
    total_prompt_tokens      integer     NOT NULL DEFAULT 0,
    total_completion_tokens  integer     NOT NULL DEFAULT 0,
    avg_latency_ms           real        NOT NULL DEFAULT 0,
    last_request_at          timestamptz,
    updated_at               timestamptz NOT NULL DEFAULT now(),
    PRIMARY KEY (provider_name, owner_id)
);

ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS provider_name           text        NOT NULL DEFAULT '';
ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS owner_id                bigint      NOT NULL DEFAULT 0;
ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS total_requests          integer     NOT NULL DEFAULT 0;
ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS successful_requests     integer     NOT NULL DEFAULT 0;
ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS failed_requests         integer     NOT NULL DEFAULT 0;
ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS total_prompt_tokens     integer     NOT NULL DEFAULT 0;
ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS total_completion_tokens integer     NOT NULL DEFAULT 0;
ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS avg_latency_ms          real        NOT NULL DEFAULT 0;
ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS last_request_at         timestamptz;
ALTER TABLE ai_provider_stats ADD COLUMN IF NOT EXISTS updated_at              timestamptz NOT NULL DEFAULT now();

UPDATE ai_provider_stats SET provider_name           = ''    WHERE provider_name           IS NULL;
UPDATE ai_provider_stats SET owner_id                = 0     WHERE owner_id                IS NULL;
UPDATE ai_provider_stats SET total_requests          = 0     WHERE total_requests          IS NULL;
UPDATE ai_provider_stats SET successful_requests     = 0     WHERE successful_requests     IS NULL;
UPDATE ai_provider_stats SET failed_requests         = 0     WHERE failed_requests         IS NULL;
UPDATE ai_provider_stats SET total_prompt_tokens     = 0     WHERE total_prompt_tokens     IS NULL;
UPDATE ai_provider_stats SET total_completion_tokens = 0     WHERE total_completion_tokens IS NULL;
UPDATE ai_provider_stats SET avg_latency_ms          = 0     WHERE avg_latency_ms          IS NULL;
UPDATE ai_provider_stats SET updated_at              = now() WHERE updated_at              IS NULL;

ALTER TABLE ai_provider_stats ALTER COLUMN provider_name           SET DEFAULT '';
ALTER TABLE ai_provider_stats ALTER COLUMN owner_id                SET DEFAULT 0;
ALTER TABLE ai_provider_stats ALTER COLUMN total_requests          SET DEFAULT 0;
ALTER TABLE ai_provider_stats ALTER COLUMN successful_requests     SET DEFAULT 0;
ALTER TABLE ai_provider_stats ALTER COLUMN failed_requests         SET DEFAULT 0;
ALTER TABLE ai_provider_stats ALTER COLUMN total_prompt_tokens     SET DEFAULT 0;
ALTER TABLE ai_provider_stats ALTER COLUMN total_completion_tokens SET DEFAULT 0;
ALTER TABLE ai_provider_stats ALTER COLUMN avg_latency_ms          SET DEFAULT 0;
ALTER TABLE ai_provider_stats ALTER COLUMN updated_at              SET DEFAULT now();

ALTER TABLE ai_provider_stats ALTER COLUMN provider_name           SET NOT NULL;
ALTER TABLE ai_provider_stats ALTER COLUMN owner_id                SET NOT NULL;
ALTER TABLE ai_provider_stats ALTER COLUMN total_requests          SET NOT NULL;
ALTER TABLE ai_provider_stats ALTER COLUMN successful_requests     SET NOT NULL;
ALTER TABLE ai_provider_stats ALTER COLUMN failed_requests         SET NOT NULL;
ALTER TABLE ai_provider_stats ALTER COLUMN total_prompt_tokens     SET NOT NULL;
ALTER TABLE ai_provider_stats ALTER COLUMN total_completion_tokens SET NOT NULL;
ALTER TABLE ai_provider_stats ALTER COLUMN avg_latency_ms          SET NOT NULL;
ALTER TABLE ai_provider_stats ALTER COLUMN updated_at              SET NOT NULL;

ALTER TABLE ai_provider_stats ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ai_provider_stats" ON ai_provider_stats;
DROP POLICY IF EXISTS "anon_update_ai_provider_stats" ON ai_provider_stats;
DROP POLICY IF EXISTS "anon_delete_ai_provider_stats" ON ai_provider_stats;
DROP POLICY IF EXISTS "anon_select_ai_provider_stats" ON ai_provider_stats;
CREATE POLICY "anon_select_ai_provider_stats" ON ai_provider_stats FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 15. ghost_chats (LEGACY — preserved, owner-gated removal) ──────────────
-- Zero .table("ghost_chats") references in current backend/tests/src code;
-- the live Ghost Seen allow-list is the bot_settings KV row. The table is
-- kept additively per the no-destruction rule; dropping it remains an owner
-- decision gated on a live-data check.
CREATE TABLE IF NOT EXISTS ghost_chats (
    chat_id         bigint       PRIMARY KEY,
    display_name    text         NOT NULL DEFAULT '',
    last_preview    text         NOT NULL DEFAULT '',
    last_message_at timestamptz,
    unread_count    integer      NOT NULL DEFAULT 0,
    created_at      timestamptz  DEFAULT now(),
    updated_at      timestamptz  DEFAULT now()
);

ALTER TABLE ghost_chats ADD COLUMN IF NOT EXISTS chat_id         bigint;
ALTER TABLE ghost_chats ADD COLUMN IF NOT EXISTS display_name    text        NOT NULL DEFAULT '';
ALTER TABLE ghost_chats ADD COLUMN IF NOT EXISTS last_preview    text        NOT NULL DEFAULT '';
ALTER TABLE ghost_chats ADD COLUMN IF NOT EXISTS last_message_at timestamptz;
ALTER TABLE ghost_chats ADD COLUMN IF NOT EXISTS unread_count    integer     NOT NULL DEFAULT 0;
ALTER TABLE ghost_chats ADD COLUMN IF NOT EXISTS created_at      timestamptz DEFAULT now();
ALTER TABLE ghost_chats ADD COLUMN IF NOT EXISTS updated_at      timestamptz DEFAULT now();

UPDATE ghost_chats SET display_name = ''    WHERE display_name IS NULL;
UPDATE ghost_chats SET last_preview = ''    WHERE last_preview IS NULL;
UPDATE ghost_chats SET unread_count = 0     WHERE unread_count IS NULL;
UPDATE ghost_chats SET created_at   = now() WHERE created_at   IS NULL;
UPDATE ghost_chats SET updated_at   = now() WHERE updated_at   IS NULL;

ALTER TABLE ghost_chats ALTER COLUMN display_name SET DEFAULT '';
ALTER TABLE ghost_chats ALTER COLUMN last_preview SET DEFAULT '';
ALTER TABLE ghost_chats ALTER COLUMN unread_count SET DEFAULT 0;
ALTER TABLE ghost_chats ALTER COLUMN created_at   SET DEFAULT now();
ALTER TABLE ghost_chats ALTER COLUMN updated_at   SET DEFAULT now();

ALTER TABLE ghost_chats ALTER COLUMN display_name SET NOT NULL;
ALTER TABLE ghost_chats ALTER COLUMN last_preview SET NOT NULL;
ALTER TABLE ghost_chats ALTER COLUMN unread_count SET NOT NULL;

CREATE INDEX IF NOT EXISTS idx_ghost_chats_last_message
    ON ghost_chats (last_message_at DESC);

ALTER TABLE ghost_chats ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_insert_ghost_chats" ON ghost_chats;
DROP POLICY IF EXISTS "anon_update_ghost_chats" ON ghost_chats;
DROP POLICY IF EXISTS "anon_delete_ghost_chats" ON ghost_chats;
DROP POLICY IF EXISTS "anon_select_ghost_chats" ON ghost_chats;
CREATE POLICY "anon_select_ghost_chats" ON ghost_chats FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 16. ai_tasks ───────────────────────────────────────────────────────────
-- Created by supabase/migrations/20260829000001_create_ai_tasks.sql; the
-- canonical script previously omitted it even though the live task system
-- (backend/ai/task_scheduler.py, task_execution.py) depends on it.
-- schedule_type's CHECK includes 'event' — the final state produced by
-- 20260904000001_add_event_schedule_type.sql.
CREATE TABLE IF NOT EXISTS ai_tasks (
    id                         bigserial    PRIMARY KEY,
    owner_id                   bigint       NOT NULL,
    label                      text         NOT NULL,
    status                     text         NOT NULL DEFAULT 'active',
    version                    integer      NOT NULL DEFAULT 1,
    schedule_type              text         NOT NULL,
    schedule                   jsonb        NOT NULL,
    timezone                   text         NOT NULL,
    next_run_at                timestamptz,
    actions                    jsonb        NOT NULL,
    notification_destination   jsonb        NOT NULL,
    ai_instruction             text,
    created_at                 timestamptz  NOT NULL DEFAULT now(),
    updated_at                 timestamptz  NOT NULL DEFAULT now(),
    terminal_at                timestamptz,
    CONSTRAINT ai_tasks_label_not_blank CHECK (length(btrim(label)) > 0),
    CONSTRAINT ai_tasks_actions_count CHECK (jsonb_array_length(actions) BETWEEN 1 AND 5),
    CONSTRAINT ai_tasks_payload_size CHECK (octet_length(actions::text) <= 32768),
    CONSTRAINT ai_tasks_schedule_size CHECK (octet_length(schedule::text) <= 16384),
    CONSTRAINT ai_tasks_destination_size CHECK (octet_length(notification_destination::text) <= 4096),
    CONSTRAINT ai_tasks_ai_instruction_size
        CHECK (ai_instruction IS NULL OR (length(btrim(ai_instruction)) > 0 AND octet_length(ai_instruction) <= 16384))
);

ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS id                       bigserial;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS owner_id                 bigint      NOT NULL DEFAULT 0;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS label                    text;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS status                   text        NOT NULL DEFAULT 'active';
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS version                  integer     NOT NULL DEFAULT 1;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS schedule_type            text;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS schedule                 jsonb;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS timezone                 text;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS next_run_at              timestamptz;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS actions                  jsonb;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS notification_destination jsonb;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS ai_instruction           text;
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS created_at               timestamptz NOT NULL DEFAULT now();
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS updated_at               timestamptz NOT NULL DEFAULT now();
ALTER TABLE ai_tasks ADD COLUMN IF NOT EXISTS terminal_at              timestamptz;

-- Recovered rows cannot invent a real action list, so the empty array is the
-- deterministic sentinel; the array-count CHECK is then skipped with a
-- warning (see the guarded CHECK block below) rather than aborting the script.
UPDATE ai_tasks SET owner_id                 = 0           WHERE owner_id                 IS NULL;
UPDATE ai_tasks SET label                    = 'recovered' WHERE label                    IS NULL;
UPDATE ai_tasks SET status                   = 'active'    WHERE status                   IS NULL;
UPDATE ai_tasks SET version                  = 1           WHERE version                  IS NULL;
UPDATE ai_tasks SET schedule_type            = 'once'      WHERE schedule_type            IS NULL;
UPDATE ai_tasks SET schedule                 = '{}'        WHERE schedule                 IS NULL;
UPDATE ai_tasks SET timezone                 = 'UTC'       WHERE timezone                 IS NULL;
UPDATE ai_tasks SET actions                  = '[]'        WHERE actions                  IS NULL;
UPDATE ai_tasks SET notification_destination = '{}'        WHERE notification_destination IS NULL;
UPDATE ai_tasks SET created_at               = now()       WHERE created_at               IS NULL;
UPDATE ai_tasks SET updated_at               = now()       WHERE updated_at               IS NULL;

ALTER TABLE ai_tasks ALTER COLUMN status     SET DEFAULT 'active';
ALTER TABLE ai_tasks ALTER COLUMN version    SET DEFAULT 1;
ALTER TABLE ai_tasks ALTER COLUMN created_at SET DEFAULT now();
ALTER TABLE ai_tasks ALTER COLUMN updated_at SET DEFAULT now();

ALTER TABLE ai_tasks ALTER COLUMN owner_id                 SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN label                    SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN status                   SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN version                  SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN schedule_type            SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN schedule                 SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN timezone                 SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN actions                  SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN notification_destination SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN created_at               SET NOT NULL;
ALTER TABLE ai_tasks ALTER COLUMN updated_at               SET NOT NULL;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_tasks_label_not_blank') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_tasks WHERE length(btrim(label)) = 0) THEN
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_label_not_blank CHECK (length(btrim(label)) > 0);
        ELSE
            RAISE WARNING 'ai_tasks_label_not_blank NOT added - % row(s) have a blank label.',
                (SELECT count(*) FROM ai_tasks WHERE length(btrim(label)) = 0);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_tasks_status_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_tasks WHERE status NOT IN ('active', 'paused', 'completed', 'failed', 'expired', 'deleted')) THEN
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_status_check
                CHECK (status IN ('active', 'paused', 'completed', 'failed', 'expired', 'deleted'));
        ELSE
            RAISE WARNING 'ai_tasks_status_check NOT added - % row(s) have an unrecognized status.',
                (SELECT count(*) FROM ai_tasks WHERE status NOT IN ('active', 'paused', 'completed', 'failed', 'expired', 'deleted'));
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_tasks_version_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_tasks WHERE version <= 0) THEN
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_version_check CHECK (version > 0);
        ELSE
            RAISE WARNING 'ai_tasks_version_check NOT added - % row(s) have version <= 0.',
                (SELECT count(*) FROM ai_tasks WHERE version <= 0);
        END IF;
    END IF;
    -- schedule_type is DROP+ADD on purpose: a database whose constraint
    -- predates 20260904000001 still rejects the 'event' schedule type.
    IF NOT EXISTS (
        SELECT 1 FROM pg_constraint
        WHERE conname = 'ai_tasks_schedule_type_check'
          AND pg_get_constraintdef(oid) LIKE '%event%'
    ) THEN
        IF NOT EXISTS (SELECT 1 FROM ai_tasks WHERE schedule_type NOT IN ('once', 'interval', 'daily', 'weekly', 'event')) THEN
            ALTER TABLE ai_tasks DROP CONSTRAINT IF EXISTS ai_tasks_schedule_type_check;
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_schedule_type_check
                CHECK (schedule_type IN ('once', 'interval', 'daily', 'weekly', 'event'));
        ELSE
            RAISE WARNING 'ai_tasks_schedule_type_check NOT replaced - % row(s) have an unrecognized schedule_type.',
                (SELECT count(*) FROM ai_tasks WHERE schedule_type NOT IN ('once', 'interval', 'daily', 'weekly', 'event'));
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_tasks_actions_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_tasks WHERE jsonb_typeof(actions) IS DISTINCT FROM 'array') THEN
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_actions_check CHECK (jsonb_typeof(actions) = 'array');
        ELSE
            RAISE WARNING 'ai_tasks_actions_check NOT added - % row(s) have a non-array actions value.',
                (SELECT count(*) FROM ai_tasks WHERE jsonb_typeof(actions) IS DISTINCT FROM 'array');
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_tasks_actions_count') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_tasks WHERE jsonb_array_length(actions) NOT BETWEEN 1 AND 5) THEN
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_actions_count CHECK (jsonb_array_length(actions) BETWEEN 1 AND 5);
        ELSE
            RAISE WARNING 'ai_tasks_actions_count NOT added - % row(s) have an actions array outside 1..5 elements.',
                (SELECT count(*) FROM ai_tasks WHERE jsonb_array_length(actions) NOT BETWEEN 1 AND 5);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_tasks_payload_size') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_tasks WHERE octet_length(actions::text) > 32768) THEN
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_payload_size CHECK (octet_length(actions::text) <= 32768);
        ELSE
            RAISE WARNING 'ai_tasks_payload_size NOT added - % row(s) exceed 32768 bytes.',
                (SELECT count(*) FROM ai_tasks WHERE octet_length(actions::text) > 32768);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_tasks_schedule_size') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_tasks WHERE octet_length(schedule::text) > 16384) THEN
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_schedule_size CHECK (octet_length(schedule::text) <= 16384);
        ELSE
            RAISE WARNING 'ai_tasks_schedule_size NOT added - % row(s) exceed 16384 bytes.',
                (SELECT count(*) FROM ai_tasks WHERE octet_length(schedule::text) > 16384);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_tasks_destination_size') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_tasks WHERE octet_length(notification_destination::text) > 4096) THEN
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_destination_size CHECK (octet_length(notification_destination::text) <= 4096);
        ELSE
            RAISE WARNING 'ai_tasks_destination_size NOT added - % row(s) exceed 4096 bytes.',
                (SELECT count(*) FROM ai_tasks WHERE octet_length(notification_destination::text) > 4096);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_tasks_ai_instruction_size') THEN
        IF NOT EXISTS (
            SELECT 1 FROM ai_tasks
            WHERE ai_instruction IS NOT NULL
              AND NOT (length(btrim(ai_instruction)) > 0 AND octet_length(ai_instruction) <= 16384)
        ) THEN
            ALTER TABLE ai_tasks ADD CONSTRAINT ai_tasks_ai_instruction_size
                CHECK (ai_instruction IS NULL OR (length(btrim(ai_instruction)) > 0 AND octet_length(ai_instruction) <= 16384));
        ELSE
            RAISE WARNING 'ai_tasks_ai_instruction_size NOT added - % row(s) have an empty or oversized ai_instruction.',
                (SELECT count(*) FROM ai_tasks
                   WHERE ai_instruction IS NOT NULL
                     AND NOT (length(btrim(ai_instruction)) > 0 AND octet_length(ai_instruction) <= 16384));
        END IF;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ai_tasks_status_next_run
    ON ai_tasks (status, next_run_at);
CREATE INDEX IF NOT EXISTS idx_ai_tasks_owner_updated
    ON ai_tasks (owner_id, updated_at DESC);

ALTER TABLE ai_tasks ENABLE ROW LEVEL SECURITY;

-- SELECT grant is restated because the task migration grants it explicitly
-- (20260829000001) rather than relying on schema default privileges.
GRANT SELECT ON ai_tasks TO anon, authenticated;

DROP POLICY IF EXISTS "anon_insert_ai_tasks" ON ai_tasks;
DROP POLICY IF EXISTS "anon_update_ai_tasks" ON ai_tasks;
DROP POLICY IF EXISTS "anon_delete_ai_tasks" ON ai_tasks;
DROP POLICY IF EXISTS "anon_select_ai_tasks" ON ai_tasks;
CREATE POLICY "anon_select_ai_tasks" ON ai_tasks FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 17. ai_task_occurrences ────────────────────────────────────────────────
-- Created by 20260829000001; `preparation_metadata` is the SECOND confirmed
-- instance of the drift class — the table's CREATE was a silent no-op on a
-- database whose table predated the column, so 20260912000001 exists as a
-- single-column repair. The reconciliation block below generalizes it.
CREATE TABLE IF NOT EXISTS ai_task_occurrences (
    id                    bigserial    PRIMARY KEY,
    task_id               bigint       NOT NULL REFERENCES ai_tasks(id) ON DELETE RESTRICT,
    owner_id              bigint       NOT NULL,
    occurrence_key        text         NOT NULL,
    definition_version    integer      NOT NULL,
    action_snapshot       jsonb        NOT NULL,
    scheduled_for         timestamptz  NOT NULL,
    attempt               smallint     NOT NULL DEFAULT 1,
    status                text         NOT NULL DEFAULT 'claimed',
    claimed_at            timestamptz,
    started_at            timestamptz,
    finished_at           timestamptz,
    retry_at              timestamptz,
    error_metadata        jsonb        NOT NULL DEFAULT '{}',
    result_metadata       jsonb        NOT NULL DEFAULT '{}',
    preparation_metadata  jsonb        NOT NULL DEFAULT '{}',
    created_at            timestamptz  NOT NULL DEFAULT now(),
    updated_at            timestamptz  NOT NULL DEFAULT now(),
    CONSTRAINT ai_task_occurrences_key_not_blank CHECK (length(btrim(occurrence_key)) > 0),
    CONSTRAINT ai_task_occurrences_action_count CHECK (jsonb_array_length(action_snapshot) BETWEEN 1 AND 5),
    CONSTRAINT ai_task_occurrences_payload_size CHECK (octet_length(action_snapshot::text) <= 32768),
    CONSTRAINT ai_task_occurrences_error_metadata_check CHECK (jsonb_typeof(error_metadata) = 'object'),
    CONSTRAINT ai_task_occurrences_preparation_metadata_object CHECK (jsonb_typeof(preparation_metadata) = 'object'),
    CONSTRAINT ai_task_occurrences_error_size CHECK (octet_length(error_metadata::text) <= 8192),
    CONSTRAINT ai_task_occurrences_result_size CHECK (octet_length(result_metadata::text) <= 8192),
    CONSTRAINT ai_task_occurrences_preparation_size CHECK (octet_length(preparation_metadata::text) <= 8192),
    CONSTRAINT ai_task_occurrences_retry_state CHECK (
        (status = 'retry_pending' AND retry_at IS NOT NULL)
        OR (status <> 'retry_pending')
    )
);

ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS id                   bigserial;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS task_id              bigint;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS owner_id             bigint      NOT NULL DEFAULT 0;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS occurrence_key       text;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS definition_version   integer;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS action_snapshot      jsonb;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS scheduled_for        timestamptz;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS attempt              smallint    NOT NULL DEFAULT 1;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS status               text        NOT NULL DEFAULT 'claimed';
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS claimed_at           timestamptz;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS started_at           timestamptz;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS finished_at          timestamptz;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS retry_at             timestamptz;
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS error_metadata       jsonb       NOT NULL DEFAULT '{}';
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS result_metadata      jsonb       NOT NULL DEFAULT '{}';
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS preparation_metadata jsonb       NOT NULL DEFAULT '{}';
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS created_at           timestamptz NOT NULL DEFAULT now();
ALTER TABLE ai_task_occurrences ADD COLUMN IF NOT EXISTS updated_at           timestamptz NOT NULL DEFAULT now();

-- task_id points at an existing task when one exists, otherwise at the
-- deterministic sentinel 0; the foreign key is then added only when the data
-- actually resolves (see the guarded block below).
UPDATE ai_task_occurrences SET task_id              = COALESCE((SELECT min(id) FROM ai_tasks), 0) WHERE task_id              IS NULL;
UPDATE ai_task_occurrences SET owner_id             = 0                            WHERE owner_id             IS NULL;
UPDATE ai_task_occurrences SET occurrence_key       = 'recovered-' || id            WHERE occurrence_key       IS NULL;
UPDATE ai_task_occurrences SET definition_version   = 1                            WHERE definition_version   IS NULL;
UPDATE ai_task_occurrences SET action_snapshot      = '[]'                          WHERE action_snapshot      IS NULL;
UPDATE ai_task_occurrences SET scheduled_for        = now()                         WHERE scheduled_for        IS NULL;
UPDATE ai_task_occurrences SET attempt              = 1                             WHERE attempt              IS NULL;
UPDATE ai_task_occurrences SET status               = 'claimed'                     WHERE status               IS NULL;
UPDATE ai_task_occurrences SET error_metadata       = '{}'                          WHERE error_metadata       IS NULL;
UPDATE ai_task_occurrences SET result_metadata      = '{}'                          WHERE result_metadata      IS NULL;
UPDATE ai_task_occurrences SET preparation_metadata = '{}'                          WHERE preparation_metadata IS NULL;
UPDATE ai_task_occurrences SET created_at           = now()                         WHERE created_at           IS NULL;
UPDATE ai_task_occurrences SET updated_at           = now()                         WHERE updated_at           IS NULL;

ALTER TABLE ai_task_occurrences ALTER COLUMN attempt              SET DEFAULT 1;
ALTER TABLE ai_task_occurrences ALTER COLUMN status               SET DEFAULT 'claimed';
ALTER TABLE ai_task_occurrences ALTER COLUMN error_metadata       SET DEFAULT '{}';
ALTER TABLE ai_task_occurrences ALTER COLUMN result_metadata      SET DEFAULT '{}';
ALTER TABLE ai_task_occurrences ALTER COLUMN preparation_metadata SET DEFAULT '{}';
ALTER TABLE ai_task_occurrences ALTER COLUMN created_at           SET DEFAULT now();
ALTER TABLE ai_task_occurrences ALTER COLUMN updated_at           SET DEFAULT now();

ALTER TABLE ai_task_occurrences ALTER COLUMN task_id              SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN owner_id             SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN occurrence_key       SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN definition_version   SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN action_snapshot      SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN scheduled_for        SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN attempt              SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN status               SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN error_metadata       SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN result_metadata      SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN preparation_metadata SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN created_at           SET NOT NULL;
ALTER TABLE ai_task_occurrences ALTER COLUMN updated_at           SET NOT NULL;

-- The task foreign key is data-guarded: it is added only when every row
-- resolves to an existing ai_tasks row.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_task_id_fkey') THEN
        IF NOT EXISTS (
            SELECT 1 FROM ai_task_occurrences o
            LEFT JOIN ai_tasks t ON t.id = o.task_id
            WHERE t.id IS NULL
        ) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_task_id_fkey
                FOREIGN KEY (task_id) REFERENCES ai_tasks(id) ON DELETE RESTRICT;
        ELSE
            RAISE WARNING 'ai_task_occurrences_task_id_fkey NOT added - % row(s) reference a task_id with no ai_tasks row. Resolve them and re-run this script.',
                (SELECT count(*) FROM ai_task_occurrences o
                   LEFT JOIN ai_tasks t ON t.id = o.task_id
                  WHERE t.id IS NULL);
        END IF;
    END IF;
END $$;

DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_key_not_blank') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE length(btrim(occurrence_key)) = 0) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_key_not_blank
                CHECK (length(btrim(occurrence_key)) > 0);
        ELSE
            RAISE WARNING 'ai_task_occurrences_key_not_blank NOT added - % row(s) have a blank occurrence_key.',
                (SELECT count(*) FROM ai_task_occurrences WHERE length(btrim(occurrence_key)) = 0);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_definition_version_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE definition_version <= 0) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_definition_version_check
                CHECK (definition_version > 0);
        ELSE
            RAISE WARNING 'ai_task_occurrences_definition_version_check NOT added - % row(s) have definition_version <= 0.',
                (SELECT count(*) FROM ai_task_occurrences WHERE definition_version <= 0);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_action_count') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE jsonb_array_length(action_snapshot) NOT BETWEEN 1 AND 5) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_action_count
                CHECK (jsonb_array_length(action_snapshot) BETWEEN 1 AND 5);
        ELSE
            RAISE WARNING 'ai_task_occurrences_action_count NOT added - % row(s) have an action_snapshot outside 1..5 elements.',
                (SELECT count(*) FROM ai_task_occurrences WHERE jsonb_array_length(action_snapshot) NOT BETWEEN 1 AND 5);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_payload_size') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE octet_length(action_snapshot::text) > 32768) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_payload_size
                CHECK (octet_length(action_snapshot::text) <= 32768);
        ELSE
            RAISE WARNING 'ai_task_occurrences_payload_size NOT added - % row(s) exceed 32768 bytes.',
                (SELECT count(*) FROM ai_task_occurrences WHERE octet_length(action_snapshot::text) > 32768);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_attempt_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE attempt NOT BETWEEN 1 AND 3) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_attempt_check
                CHECK (attempt BETWEEN 1 AND 3);
        ELSE
            RAISE WARNING 'ai_task_occurrences_attempt_check NOT added - % row(s) have attempt outside 1..3.',
                (SELECT count(*) FROM ai_task_occurrences WHERE attempt NOT BETWEEN 1 AND 3);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_status_check') THEN
        IF NOT EXISTS (
            SELECT 1 FROM ai_task_occurrences
            WHERE status NOT IN ('claimed', 'running', 'succeeded', 'failed', 'retry_pending', 'cancelled', 'expired', 'interrupted')
        ) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_status_check
                CHECK (status IN ('claimed', 'running', 'succeeded', 'failed', 'retry_pending', 'cancelled', 'expired', 'interrupted'));
        ELSE
            RAISE WARNING 'ai_task_occurrences_status_check NOT added - % row(s) have an unrecognized status.',
                (SELECT count(*) FROM ai_task_occurrences
                  WHERE status NOT IN ('claimed', 'running', 'succeeded', 'failed', 'retry_pending', 'cancelled', 'expired', 'interrupted'));
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_error_metadata_check') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE jsonb_typeof(error_metadata) IS DISTINCT FROM 'object') THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_error_metadata_check
                CHECK (jsonb_typeof(error_metadata) = 'object');
        ELSE
            RAISE WARNING 'ai_task_occurrences_error_metadata_check NOT added - % row(s) have a non-object error_metadata.',
                (SELECT count(*) FROM ai_task_occurrences WHERE jsonb_typeof(error_metadata) IS DISTINCT FROM 'object');
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_retry_state') THEN
        IF NOT EXISTS (
            SELECT 1 FROM ai_task_occurrences
            WHERE NOT ((status = 'retry_pending' AND retry_at IS NOT NULL) OR (status <> 'retry_pending'))
        ) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_retry_state CHECK (
                (status = 'retry_pending' AND retry_at IS NOT NULL)
                OR (status <> 'retry_pending')
            );
        ELSE
            RAISE WARNING 'ai_task_occurrences_retry_state NOT added - % row(s) have status=retry_pending without retry_at.',
                (SELECT count(*) FROM ai_task_occurrences
                  WHERE NOT ((status = 'retry_pending' AND retry_at IS NOT NULL) OR (status <> 'retry_pending')));
        END IF;
    END IF;
    -- The preparation_metadata pair is exactly what 20260912000001 adds.
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_preparation_metadata_object') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE jsonb_typeof(preparation_metadata) IS DISTINCT FROM 'object') THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_preparation_metadata_object
                CHECK (jsonb_typeof(preparation_metadata) = 'object');
        ELSE
            RAISE WARNING 'ai_task_occurrences_preparation_metadata_object NOT added - % row(s) have a non-object preparation_metadata.',
                (SELECT count(*) FROM ai_task_occurrences WHERE jsonb_typeof(preparation_metadata) IS DISTINCT FROM 'object');
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_error_size') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE octet_length(error_metadata::text) > 8192) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_error_size
                CHECK (octet_length(error_metadata::text) <= 8192);
        ELSE
            RAISE WARNING 'ai_task_occurrences_error_size NOT added - % row(s) exceed 8192 bytes.',
                (SELECT count(*) FROM ai_task_occurrences WHERE octet_length(error_metadata::text) > 8192);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_result_size') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE octet_length(result_metadata::text) > 8192) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_result_size
                CHECK (octet_length(result_metadata::text) <= 8192);
        ELSE
            RAISE WARNING 'ai_task_occurrences_result_size NOT added - % row(s) exceed 8192 bytes.',
                (SELECT count(*) FROM ai_task_occurrences WHERE octet_length(result_metadata::text) > 8192);
        END IF;
    END IF;
    IF NOT EXISTS (SELECT 1 FROM pg_constraint WHERE conname = 'ai_task_occurrences_preparation_size') THEN
        IF NOT EXISTS (SELECT 1 FROM ai_task_occurrences WHERE octet_length(preparation_metadata::text) > 8192) THEN
            ALTER TABLE ai_task_occurrences ADD CONSTRAINT ai_task_occurrences_preparation_size
                CHECK (octet_length(preparation_metadata::text) <= 8192);
        ELSE
            RAISE WARNING 'ai_task_occurrences_preparation_size NOT added - % row(s) exceed 8192 bytes.',
                (SELECT count(*) FROM ai_task_occurrences WHERE octet_length(preparation_metadata::text) > 8192);
        END IF;
    END IF;
END $$;

-- Unique index: guarded because legacy occurrences may already collide on
-- (task_id, occurrence_key) — the occurrence ids are application-generated.
DO $$
BEGIN
    IF NOT EXISTS (SELECT 1 FROM pg_class WHERE relname = 'uq_ai_task_occurrences_task_key') THEN
        IF NOT EXISTS (
            SELECT 1 FROM ai_task_occurrences
            GROUP BY task_id, occurrence_key HAVING count(*) > 1
        ) THEN
            CREATE UNIQUE INDEX uq_ai_task_occurrences_task_key
                ON ai_task_occurrences (task_id, occurrence_key);
        ELSE
            RAISE WARNING 'ai_task_occurrences: uq_ai_task_occurrences_task_key NOT created - duplicate (task_id, occurrence_key) pairs exist. Resolve them and re-run this script.';
        END IF;
    END IF;
END $$;

CREATE INDEX IF NOT EXISTS idx_ai_task_occurrences_owner_scheduled
    ON ai_task_occurrences (owner_id, scheduled_for DESC);
CREATE INDEX IF NOT EXISTS idx_ai_task_occurrences_task_scheduled
    ON ai_task_occurrences (task_id, scheduled_for DESC);

ALTER TABLE ai_task_occurrences ENABLE ROW LEVEL SECURITY;

GRANT SELECT ON ai_task_occurrences TO anon, authenticated;

DROP POLICY IF EXISTS "anon_insert_ai_task_occurrences" ON ai_task_occurrences;
DROP POLICY IF EXISTS "anon_update_ai_task_occurrences" ON ai_task_occurrences;
DROP POLICY IF EXISTS "anon_delete_ai_task_occurrences" ON ai_task_occurrences;
DROP POLICY IF EXISTS "anon_select_ai_task_occurrences" ON ai_task_occurrences;
CREATE POLICY "anon_select_ai_task_occurrences" ON ai_task_occurrences FOR SELECT
    TO anon, authenticated USING (true);

-- ─── 18. Identity-constraint reconciliation ─────────────────────────────────
-- PRIMARY KEY / UNIQUE constraints are asserted here, after every column
-- exists, because PostgreSQL allows only ONE primary key per table and both
-- kinds require data that satisfies them. Each constraint is applied only when
-- the current rows can satisfy it; otherwise a WARNING names the table and
-- column so the owner can resolve it and re-run the script — the script itself
-- never aborts on pre-existing data.
--
-- kinds:  p = PRIMARY KEY (existence checked as "any PK on this table"),
--         u = UNIQUE      (existence checked by constraint name)
-- nullexpr / distinctexpr: the generic "no NULLs and no duplicates" predicate.
DO $$
DECLARE
    r record;
    ok boolean;
BEGIN
    FOR r IN
        SELECT * FROM (VALUES
            ('saved_items',         'saved_items_pkey',            'p', 'id',                      'id',            'id'),
            ('saved_items',         'saved_items_save_code_key',   'u', 'save_code',               'save_code',     'save_code'),
            ('bio_state',           'bio_state_pkey',              'p', 'id',                      'id',            'id'),
            ('bio_state',           'bio_state_owner_id_key',      'u', 'owner_id',                'owner_id',      'owner_id'),
            ('username_state',      'username_state_pkey',         'p', 'id',                      'id',            'id'),
            ('username_state',      'username_state_owner_id_key', 'u', 'owner_id',                'owner_id',      'owner_id'),
            ('bot_logs',            'bot_logs_pkey',               'p', 'id',                      'id',            'id'),
            ('panel_settings',      'panel_settings_pkey',         'p', 'key',                     'key',           'key'),
            ('bot_settings',        'bot_settings_pkey',           'p', 'key',                     'key',           'key'),
            ('ai_config',           'ai_config_pkey',              'p', 'id',                      'id',            'id'),
            ('ai_config',           'ai_config_owner_id_key',      'u', 'owner_id',                'owner_id',      'owner_id'),
            ('ai_sessions',         'ai_sessions_pkey',            'p', 'id',                      'id',            'id'),
            ('ai_sessions',         'ai_sessions_session_id_key',  'u', 'session_id',              'session_id',    'session_id'),
            ('ai_messages',         'ai_messages_pkey',            'p', 'id',                      'id',            'id'),
            ('ai_memories',         'ai_memories_pkey',            'p', 'id',                      'id',            'id'),
            ('ai_tool_history',     'ai_tool_history_pkey',        'p', 'id',                      'id',            'id'),
            ('ai_usage',            'ai_usage_pkey',               'p', 'id',                      'id',            'id'),
            ('ai_provider_stats',   'ai_provider_stats_pkey',      'p', 'provider_name, owner_id', 'provider_name', 'provider_name, owner_id'),
            ('ghost_chats',         'ghost_chats_pkey',            'p', 'chat_id',                 'chat_id',       'chat_id'),
            ('ai_tasks',            'ai_tasks_pkey',               'p', 'id',                      'id',            'id'),
            ('ai_task_occurrences', 'ai_task_occurrences_pkey',    'p', 'id',                      'id',            'id')
        ) AS v(tbl, cname, kind, cols, nullexpr, distinctexpr)
    LOOP
        IF NOT EXISTS (
            SELECT 1
            FROM pg_constraint c
            JOIN pg_class t ON t.oid = c.conrelid
            JOIN pg_namespace n ON n.oid = t.relnamespace
            WHERE n.nspname = 'public'
              AND t.relname = r.tbl
              AND (CASE WHEN r.kind = 'p' THEN c.contype = 'p' ELSE c.conname = r.cname END)
        ) THEN
            EXECUTE format('SELECT (count(*) = count(%s)) AND (count(DISTINCT %s) = count(*)) FROM %I',
                           r.nullexpr, r.distinctexpr, r.tbl) INTO ok;
            IF ok THEN
                EXECUTE format('ALTER TABLE %I ADD CONSTRAINT %I ' ||
                               CASE WHEN r.kind = 'p' THEN 'PRIMARY KEY (' ELSE 'UNIQUE (' END ||
                               r.cols || ')', r.tbl, r.cname);
            ELSE
                RAISE WARNING '%: constraint % NOT added - existing rows contain NULLs or duplicates in (%). Resolve them and re-run this script.',
                    r.tbl, r.cname, r.cols;
            END IF;
        END IF;
    END LOOP;
END $$;

-- ─── 19. PostgREST schema-cache reload ──────────────────────────────────────
-- Without this the API keeps serving the pre-reconciliation column list.
NOTIFY pgrst, 'reload schema';

COMMIT;

-- ============================================================================
-- Drift report — runs AFTER the commit, so it always reports the real state.
-- Zero rows means every canonical (table, column) now exists. Any row returned
-- names a column this script could not establish (create it manually).
-- ============================================================================
SELECT v.tbl || '.' || v.col AS missing_canonical_column
FROM (VALUES
    ('saved_items','id'), ('saved_items','save_code'), ('saved_items','save_type'),
    ('saved_items','origin_chat_id'), ('saved_items','origin_msg_id'),
    ('saved_items','saved_chat_id'), ('saved_items','saved_msg_id'),
    ('saved_items','sender_name'), ('saved_items','sender_id'), ('saved_items','mime_type'),
    ('saved_items','file_id'), ('saved_items','file_size'), ('saved_items','media_type'),
    ('saved_items','tags'), ('saved_items','caption'), ('saved_items','file_name'),
    ('saved_items','short_code'), ('saved_items','owner_id'), ('saved_items','created_at'),
    ('bio_state','id'), ('bio_state','owner_id'), ('bio_state','template'), ('bio_state','mood'),
    ('bio_state','custom_text'), ('bio_state','is_active'), ('bio_state','last_bio'), ('bio_state','updated_at'),
    ('username_state','id'), ('username_state','owner_id'), ('username_state','template'),
    ('username_state','mood'), ('username_state','custom_text'), ('username_state','is_active'),
    ('username_state','last_name'), ('username_state','updated_at'),
    ('bot_logs','id'), ('bot_logs','owner_id'), ('bot_logs','level'), ('bot_logs','message'),
    ('bot_logs','context'), ('bot_logs','created_at'),
    ('panel_settings','key'), ('panel_settings','auto_close_enabled'), ('panel_settings','auto_close_delay'),
    ('panel_settings','max_deep_save_mb'), ('panel_settings','delete_batch_size'),
    ('panel_settings','log_retention_days'), ('panel_settings','panel_timeout_seconds'),
    ('panel_settings','allow_multiple_panels'), ('panel_settings','reuse_existing_panel'),
    ('panel_settings','language'), ('panel_settings','debug_callbacks'), ('panel_settings','owner_only'),
    ('panel_settings','dashboard_font'), ('panel_settings','update_stale_seconds'),
    ('panel_settings','ghost_seen_retention_seconds'), ('panel_settings','updated_at'),
    ('bot_settings','key'), ('bot_settings','value'), ('bot_settings','value_type'), ('bot_settings','updated_at'),
    ('ai_config','id'), ('ai_config','owner_id'), ('ai_config','provider'), ('ai_config','model'),
    ('ai_config','temperature'), ('ai_config','max_tokens'), ('ai_config','system_prompt'),
    ('ai_config','history_budget'), ('ai_config','is_configured'), ('ai_config','trigger_en'),
    ('ai_config','trigger_fa'), ('ai_config','show_question'), ('ai_config','stt_model'),
    ('ai_config','stt_language'), ('ai_config','stt_passes'), ('ai_config','last_request_at'),
    ('ai_config','last_latency_ms'), ('ai_config','created_at'), ('ai_config','updated_at'),
    ('ai_sessions','id'), ('ai_sessions','session_id'), ('ai_sessions','owner_id'),
    ('ai_sessions','provider'), ('ai_sessions','model'), ('ai_sessions','status'),
    ('ai_sessions','total_tokens'), ('ai_sessions','message_count'), ('ai_sessions','created_at'),
    ('ai_sessions','updated_at'),
    ('ai_messages','id'), ('ai_messages','session_id'), ('ai_messages','owner_id'),
    ('ai_messages','role'), ('ai_messages','content'), ('ai_messages','token_count'),
    ('ai_messages','provider'), ('ai_messages','model'), ('ai_messages','created_at'),
    ('ai_memories','id'), ('ai_memories','owner_id'), ('ai_memories','tier'), ('ai_memories','category'),
    ('ai_memories','content'), ('ai_memories','importance'), ('ai_memories','expires_at'),
    ('ai_memories','metadata'), ('ai_memories','created_at'),
    ('ai_tool_history','id'), ('ai_tool_history','owner_id'), ('ai_tool_history','session_id'),
    ('ai_tool_history','tool_name'), ('ai_tool_history','arguments'), ('ai_tool_history','result_success'),
    ('ai_tool_history','result_message'), ('ai_tool_history','result_data'),
    ('ai_tool_history','latency_ms'), ('ai_tool_history','created_at'),
    ('ai_usage','id'), ('ai_usage','owner_id'), ('ai_usage','session_id'), ('ai_usage','provider'),
    ('ai_usage','model'), ('ai_usage','prompt_tokens'), ('ai_usage','completion_tokens'),
    ('ai_usage','total_tokens'), ('ai_usage','latency_ms'), ('ai_usage','token_source'),
    ('ai_usage','created_at'),
    ('ai_provider_stats','provider_name'), ('ai_provider_stats','owner_id'),
    ('ai_provider_stats','total_requests'), ('ai_provider_stats','successful_requests'),
    ('ai_provider_stats','failed_requests'), ('ai_provider_stats','total_prompt_tokens'),
    ('ai_provider_stats','total_completion_tokens'), ('ai_provider_stats','avg_latency_ms'),
    ('ai_provider_stats','last_request_at'), ('ai_provider_stats','updated_at'),
    ('ghost_chats','chat_id'), ('ghost_chats','display_name'), ('ghost_chats','last_preview'),
    ('ghost_chats','last_message_at'), ('ghost_chats','unread_count'),
    ('ghost_chats','created_at'), ('ghost_chats','updated_at'),
    ('ai_tasks','id'), ('ai_tasks','owner_id'), ('ai_tasks','label'), ('ai_tasks','status'),
    ('ai_tasks','version'), ('ai_tasks','schedule_type'), ('ai_tasks','schedule'),
    ('ai_tasks','timezone'), ('ai_tasks','next_run_at'), ('ai_tasks','actions'),
    ('ai_tasks','notification_destination'), ('ai_tasks','ai_instruction'),
    ('ai_tasks','created_at'), ('ai_tasks','updated_at'), ('ai_tasks','terminal_at'),
    ('ai_task_occurrences','id'), ('ai_task_occurrences','task_id'), ('ai_task_occurrences','owner_id'),
    ('ai_task_occurrences','occurrence_key'), ('ai_task_occurrences','definition_version'),
    ('ai_task_occurrences','action_snapshot'), ('ai_task_occurrences','scheduled_for'),
    ('ai_task_occurrences','attempt'), ('ai_task_occurrences','status'),
    ('ai_task_occurrences','claimed_at'), ('ai_task_occurrences','started_at'),
    ('ai_task_occurrences','finished_at'), ('ai_task_occurrences','retry_at'),
    ('ai_task_occurrences','error_metadata'), ('ai_task_occurrences','result_metadata'),
    ('ai_task_occurrences','preparation_metadata'), ('ai_task_occurrences','created_at'),
    ('ai_task_occurrences','updated_at')
) AS v(tbl, col)
LEFT JOIN information_schema.columns c
    ON c.table_schema = 'public' AND c.table_name = v.tbl AND c.column_name = v.col
WHERE c.column_name IS NULL
ORDER BY 1;
```

### Script boundaries & uncertainty

- **Derived from current code**, not from this document's earlier prose
  (which the script supersedes where the two disagree).
- **Safe on a fresh database, on an older/partially-migrated one, and on
  itself (re-running it is a no-op).** Every canonical column is re-asserted
  with `ADD COLUMN IF NOT EXISTS`, so a legacy table shape — including one
  whose `bot_settings` predates `value_type` — can no longer produce
  42703. The script ends with a drift report: zero rows returned means the
  database now matches the canonical contract.
- **Live Supabase state was NOT inspected** (no authenticated live access in
  the audit environment) and **the script has NOT been executed against any
  database** — not against a local PostgreSQL either (none is available in
  the build environment). Validation is: a contract cross-check against every
  `.table()` call site; a statement simulator that parses the shipped SQL,
  applies it to synthetic empty/legacy/idempotency schemas and fails on any
  column reference it cannot resolve (`tests/test_canonical_schema_reconciliation.py`);
  the three-way byte-identity test; the internal-consistency test that every
  table's CREATE column set equals its `ADD COLUMN` set equals its drift-report
  set; plus the full repository test suite. See §30.8 for the exact commands
  and their results — and §30.12 for what this validation does NOT prove.
- **No historical/user data is fabricated.** Only the deterministic
  `panel_settings('global')` singleton and the five legacy `bot_settings`
  default rows are seeded.
- **Owner-gated legacy decisions remain unchanged:** dropping `ghost_chats`,
  dropping `saved_items.short_code`/`file_name`, dropping the orphan
  `panel_settings` columns, implementing `ai_preferences`, AI-table retention
  policy, and live RLS-posture verification.
- The five legacy `bot_settings` seed rows are consumed by no current reader
  (retained for migration fidelity, not invention).

---

## 30. Canonical Schema Reconciliation & Drift Repair

**Status:** repository-side repair COMPLETE · the canonical SQL is **NOT
APPLIED** and was **NOT EXECUTED by the coding agent** · owner action required
(§30.11).

### 30.1 The defect

`CREATE TABLE IF NOT EXISTS` is a **silent NO-OP when the table already
exists**. Every earlier revision of the canonical script — and the historical
migration files it was derived from — used it as the *only* mechanism that
established a table's shape, then immediately ran statements that referenced
columns. On a database whose table predates one of those columns the CREATE
does nothing and the next statement fails.

The observed production error was:

```
ERROR:  42703
column "value_type" of relation "bot_settings" does not exist
INSERT INTO bot_settings (key, value, value_type) VALUES ...
```

**Root cause:** schema *reconciliation* was delegated to `IF NOT EXISTS`
instead of being performed explicitly. The failing statement is only the
symptom; the cause is that the script had no reconciliation phase at all.

This is a **class**, not an incident. Every canonical column that a legacy table
happens to lack produces the same 42703 — or, for a statement nested inside a
`DO $$ … $$` body, an error raised while planning the block. The audit below
lists every instance found in this repository.

### 30.2 Drift audit — every instance of the class

| # | Object | What a legacy shape can lack | How the old script failed | Status |
|---|---|---|---|---|
| 1 | `bot_settings` | `value_type` | `INSERT … (key, value, value_type)` → **the reported 42703** | **FIXED** — reconciled before the INSERT |
| 2 | `panel_settings` | 13 later columns (`auto_close_delay` … `ghost_seen_retention_seconds`); `20260726143924` created only `(key, auto_close_enabled, updated_at)` | the eight `CHECK` blocks reference those columns, so the whole `DO $$` body failed to plan | **FIXED** — reconciled before the CHECKs |
| 3 | `saved_items` | `short_code`, `file_name` (added by `20260718143752`) | `CREATE UNIQUE INDEX … (short_code)` and two trigram GIN indexes | **FIXED** — reconciled before the indexes |
| 4 | `ai_config` | `trigger_en`, `trigger_fa`, `show_question`, `stt_model`, `stt_language`, `stt_passes` | the columns were **absent from the canonical script entirely**, so a database built from it rejected *every* `config_store` upsert and silently lost AI settings on restart (§19.1, §19.2a, §19.2b) | **FIXED** — added to the canonical contract |
| 5 | `ai_task_occurrences` | `preparation_metadata` | already repaired once by `20260912000001_add_ai_task_occurrences_preparation_metadata.sql`, whose own comment names this exact class | **FIXED** — generalized |
| 6 | `ai_tasks` | the whole table | absent from the canonical script although `backend/ai/task_scheduler.py` and `task_execution.py` depend on it | **FIXED** — added to the canonical contract |
| 7 | `ai_messages` | `tool_calls` | no current reader or writer exists (§19.5) | **DOCUMENTED** — deliberately NOT canonical |
| 8 | `ai_preferences` | the whole table | no migration exists and every implementation is in-memory (§14, §19.17) | **DOCUMENTED** — deliberately NOT canonical |
| 9 | identity constraints | a missing `PRIMARY KEY` / `UNIQUE` | a targetless-less `ON CONFLICT (key)` cannot resolve without a unique index on `key` | **FIXED** — targetless `ON CONFLICT DO NOTHING` + guarded identity reconciliation |

### 30.3 The reconciliation contract

Every canonical table is established by the same fixed sequence:

```
CREATE TABLE IF NOT EXISTS <t> ( … canonical definition … )

ALTER TABLE <t> ADD COLUMN IF NOT EXISTS <col> <type> [DEFAULT <d>]      per column
UPDATE <t> SET <col> = <deterministic value> WHERE <col> IS NULL         per NOT NULL column
ALTER TABLE <t> ALTER COLUMN <col> SET DEFAULT <d>                       per defaulted column
ALTER TABLE <t> ALTER COLUMN <col> SET NOT NULL                          per NOT NULL column

CREATE INDEX IF NOT EXISTS … / DO $$ … data-guarded constraint scripts … $$
```

**New-column safety (the rule that must never be broken again).** When a
required column is missing, the script does not write
`ADD COLUMN … NOT NULL` blindly. It:

1. adds the column (carrying `DEFAULT` only when the canonical definition has
   one, which is safe on an existing table because PostgreSQL seeds existing
   rows from the default);
2. **backfills every remaining NULL deterministically** before any NOT NULL is
   enforced — text → `''` (or a named sentinel such as `'recovered'`,
   `'unknown'`, `'S' || lpad(id::text,4,'0')` where a non-blank CHECK requires
   it), numbers → `0`/canonical value, booleans → `false`, timestamps →
   `now()`, jsonb → `'{}'`, arrays → `'{}'`/`'[]'`;
3. enforces the final `DEFAULT` and `NOT NULL`.

Because step 2 runs for *every* NOT NULL column — not only the newly added ones
— a legacy table that was created with a nullable column which canonical says is
NOT NULL is also converged, and `SET NOT NULL` cannot fail. Verified by
`tests/test_canonical_schema_reconciliation.py`
(`test_legacy_*`, `test_the_worst_case_legacy_database_converges_*`).

**Guarded additions.** Anything that existing data could reject is applied only
when the data permits it, and is otherwise skipped with a `RAISE WARNING` that
names the table, the constraint and the offending row count — the script never
aborts because of pre-existing data:

* the 8 `panel_settings` CHECKs, the `ai_config` `stt_passes` range, the
  `ai_sessions` status CHECK, the `ai_memories` tier/category CHECKs, the
  `ai_tasks` CHECKs, the `ai_task_occurrences` CHECKs and its task foreign key;
* `idx_saved_items_short_code` and `uq_ai_task_occurrences_task_key` (both
  unique, both data-dependent);
* the consolidated **identity block** (§30.3.1).

`CREATE TABLE`, `ENABLE ROW LEVEL SECURITY`, `DROP POLICY IF EXISTS` /
`CREATE POLICY`, the `GRANT SELECT` restatements and the rest of the indexes are
unconditionally idempotent and need no guard.

#### 30.3.1 Identity-constraint reconciliation

PostgreSQL allows only one primary key per table, and both PRIMARY KEY and
UNIQUE require rows that satisfy them. So the script asserts them **after every
column exists**, from one static list, applying each only when the current rows
have no NULL and no duplicate in the constrained columns:

* `p` = PRIMARY KEY — existence is tested as "any PK on this table" (a legacy
  table may carry the same PK under a different name);
* `u` = UNIQUE — existence is tested by constraint name;
* the predicate is the generic `count(*) = count(cols) AND count(DISTINCT cols) = count(*)`.

21 constraints cover all 16 tables (each table has a PK; five carry a UNIQUE).
A redundant UNIQUE would be harmless, a duplicate PK is impossible, and a
constraint that the data cannot satisfy is reported instead of attempted.

### 30.4 Canonical column inventory

**181 `ADD COLUMN IF NOT EXISTS` statements** re-assert the **181 canonical
columns**; `tests/test_canonical_schema_reconciliation.py` proves that the
CREATE column set, the `ADD COLUMN` set and the drift-report set are identical
for every table, so no canonical column can be established by a silent no-op
alone.

| Table | Columns | Column list |
|---|---|---|
| `saved_items` | 20 | id, save_code, save_type, origin_chat_id, origin_msg_id, saved_chat_id, saved_msg_id, sender_name, sender_id, mime_type, file_id, file_size, media_type, tags, caption, file_name, display_name, short_code, owner_id, created_at |
| `bio_state` | 8 | id, owner_id, template, mood, custom_text, is_active, last_bio, updated_at |
| `username_state` | 8 | id, owner_id, template, mood, custom_text, is_active, last_name, updated_at |
| `bot_logs` | 6 | id, owner_id, level, message, context, created_at |
| `panel_settings` | 16 | key, auto_close_enabled, auto_close_delay, max_deep_save_mb, delete_batch_size, log_retention_days, panel_timeout_seconds, allow_multiple_panels, reuse_existing_panel, language, debug_callbacks, owner_only, dashboard_font, update_stale_seconds, ghost_seen_retention_seconds, updated_at |
| `bot_settings` | 4 | key, value, value_type, updated_at |
| `ai_config` | 19 | id, owner_id, provider, model, temperature, max_tokens, system_prompt, history_budget, is_configured, trigger_en, trigger_fa, show_question, stt_model, stt_language, stt_passes, last_request_at, last_latency_ms, created_at, updated_at |
| `ai_sessions` | 10 | id, session_id, owner_id, provider, model, status, total_tokens, message_count, created_at, updated_at |
| `ai_messages` | 9 | id, session_id, owner_id, role, content, token_count, provider, model, created_at |
| `ai_memories` | 9 | id, owner_id, tier, category, content, importance, expires_at, metadata, created_at |
| `ai_tool_history` | 10 | id, owner_id, session_id, tool_name, arguments, result_success, result_message, result_data, latency_ms, created_at |
| `ai_usage` | 11 | id, owner_id, session_id, provider, model, prompt_tokens, completion_tokens, total_tokens, latency_ms, token_source, created_at |
| `ai_provider_stats` | 10 | provider_name, owner_id, total_requests, successful_requests, failed_requests, total_prompt_tokens, total_completion_tokens, avg_latency_ms, last_request_at, updated_at |
| `ghost_chats` | 7 | chat_id, display_name, last_preview, last_message_at, unread_count, created_at, updated_at |
| `ai_tasks` | 15 | id, owner_id, label, status, version, schedule_type, schedule, timezone, next_run_at, actions, notification_destination, ai_instruction, created_at, updated_at, terminal_at |
| `ai_task_occurrences` | 18 | id, task_id, owner_id, occurrence_key, definition_version, action_snapshot, scheduled_for, attempt, status, claimed_at, started_at, finished_at, retry_at, error_metadata, result_metadata, preparation_metadata, created_at, updated_at |

`saved_items.display_name` is deliberately **not** in this inventory: the
column did not exist when the reconciliation snapshot was written and it is
added afterwards by the additive migration
`20260921000001_add_saved_items_display_name.sql` (§2, §20 row 16), together
with the resolver's two search indexes
(`20260922000001_add_saved_items_search_indexes.sql`, §20 row 17). The
current contract for `saved_items` is therefore this snapshot **plus** those
additive successors — the snapshot itself is never regenerated to contain
them.

`ai_preferences` is **not** in this inventory: it has no migration and no
producer (see §14, §19.17). `ai_messages.tool_calls` is **not** in it either:
nothing reads or writes it (§19.5). Adding either would invent a contract the
code does not have.

### 30.5 The reconciliation migration

`supabase/migrations/20260920000001_reconcile_canonical_schema.sql` is the
forward-only repair. It is **not** a rewrite of any historical migration — every
`202607…`–`20260919…` file is byte-untouched, and the repair is followed in
`supabase/migrations/` only by additive successors (the newest is
`20260921000001_add_saved_items_display_name.sql`, §2).

It contains the **complete** canonical script rather than a narrow
`ALTER TABLE bot_settings ADD COLUMN value_type`, because a database may be
missing any of the objects the earlier migrations introduce (several of them are
explicitly pending, §20). One idempotent script converges all of them. Its
statements are the same text that appears in:

* the fenced block in the previous section (this document is the canonical
  reference),
* `supabase/canonical_bootstrap.sql`,
* the migration file itself.

All three are kept **byte-identical**; a test enforces it.

The script is the reconciliation **snapshot**, not a living file. A later
schema change arrives as its own additive migration — the newest is
`20260921000001_add_saved_items_display_name.sql` (§2) — and the snapshot,
the convenience copy and this §30 block are never edited to absorb it. The
tests enforce both halves: the three copies stay byte-identical, and every
migration newer than the snapshot is additive-only (no `DROP TABLE`,
`TRUNCATE`, `DELETE FROM` or `DROP COLUMN` statement).

### 30.6 Verification: the drift report

The script ends with a query that runs **after `COMMIT`** (so it always reports
the real state) and lists every canonical `(table, column)` that does not exist:

```sql
SELECT v.tbl || '.' || v.col AS missing_canonical_column
FROM (VALUES …) AS v(tbl, col)
LEFT JOIN information_schema.columns c
    ON c.table_schema = 'public' AND c.table_name = v.tbl AND c.column_name = v.col
WHERE c.column_name IS NULL
ORDER BY 1;
```

**Zero rows returned means the database now matches the canonical contract.** A
non-empty result names exactly what still needs manual attention. Any
`RAISE WARNING` printed earlier in the run names the constraints that were
deliberately skipped and the row counts that blocked them.

### 30.7 Safety rules the script obeys

1. **Non-destructive by default.** The canonical script contains no `DROP
   TABLE`, no `DROP COLUMN`, no `TRUNCATE` and no `DELETE FROM`. The only
   `DROP`s are `DROP POLICY IF EXISTS` (stale anon **write** policies the
   documented model forbids) and `DROP CONSTRAINT IF EXISTS` inside a data
   guard, where the constraint is immediately recreated with the canonical
   definition.
2. **Existing rows are never rewritten except by a deterministic backfill of a
   column that was just added or was NULL** — the exact transition the NOT NULL
   contract requires.
3. **Constraint/index additions are validated first** (§30.3, §30.3.1). A
   constraint that existing data rejects is reported, not forced.
4. **The security model is restated, not relaxed:** RLS is enabled on all 16
   tables, `anon`/`authenticated` keep SELECT-only, and no anon INSERT/UPDATE/
   DELETE policy exists. No `FOR ALL`, no `TO PUBLIC`, no
   `DISABLE ROW LEVEL SECURITY`.
5. **Nothing outside the public canonical schema is touched.** The Supabase
   Vault credential objects of §29 (`api_credentials`, `api_credential_pool`,
   the five management functions) are intentionally *not* in this script; the
   reconciliation must never be able to read, move or delete a secret. No
   `vault.*` reference appears anywhere in it.
6. **No behavioural setting is invented.** The script writes no application
   configuration; the only rows it inserts are the pre-existing deterministic
   seeds (`panel_settings('global')` and the five legacy `bot_settings` rows).

### 30.8 Validation performed

Everything below was executed locally against the repository. **No database was
touched — no Supabase connection, no SQL execution, no Vault secret.**

| Command | Result |
|---|---|
| `python -m pytest tests/test_canonical_schema_reconciliation.py -q` | **32 passed** |
| `python -m pytest tests/test_credential_vault.py tests/test_stt_credential_pool.py -q` | see §30.8 note — run in the same batch as the regression suites |
| `python -m pytest tests/ -q` (full suite) | see IMPLEMENTATION_REPORT.md for the exact totals |
| `python -m py_compile` on every changed Python file | clean |
| `git diff --check` | clean |

The three kinds of evidence, and what each one does and does not prove:

1. **Static identity** — `test_the_three_copies_of_the_canonical_script_are_byte_identical`
   proves the migration, the convenience copy and the §30 block are the same
   text, so there is exactly one reconciled definition.
2. **Internal consistency** — `test_every_table_declares_adds_and_verifies_exactly_the_same_columns`
   and `test_no_canonical_column_is_established_by_create_table_alone` prove
   that all 181 canonical columns are re-asserted explicitly. This is the
   property whose absence caused the production failure.
3. **Simulated execution** — `apply_script()` parses the real statements of the
   shipped script and applies them with PostgreSQL's semantics to (a) an empty
   schema, (b) the worst-case legacy schema (all nine drift items of §30.2 at
   once) and (c) an already-canonical schema:
   * `test_the_reported_production_failure_is_reproducible_and_fixed` first
     reproduces the 42703 by applying the *old* pattern (CREATE IF NOT EXISTS +
     `INSERT … (key, value, value_type)`) to a `bot_settings` that has no
     `value_type`, asserting the failure, and then proves the shipped script
     converges instead, backfills `value_type = 'str'`, preserves the three
     pre-existing rows and adds the five seeds;
   * `test_a_fresh_database_reaches_the_full_canonical_contract` and
     `test_the_worst_case_legacy_database_converges_and_reports_no_drift`
     assert the final column set equals the canonical set for all 16 tables and
     that the drift report comes back **empty**;
   * `test_applying_the_script_twice_changes_nothing`,
     `test_applying_twice_to_a_legacy_database_changes_nothing_the_second_time`
     and `test_applying_to_an_already_canonical_database_is_a_no_op` prove
     repeat-safety;
   * the simulator **refuses to skip a statement form it does not understand**
     and fails on any unresolved table/column reference, so coverage cannot
     silently regress.

### 30.9 Rollback

**There is no safe rollback of the data-level part of this repair, and none is
needed — it is irreversible in that sense, by design.** The script is additive:
it creates missing columns, tables, indexes and constraints, and backfills only
values that were previously absent. A database that has already run it is a
superset of the pre-repair state, so no application-visible behaviour has to be
reversed. The deterministic backfills (for example a legacy `owner_id` becoming
`0`) cannot afterwards be told apart from real values, which is exactly why they
must be reviewed on a backup if reversibility matters.

Three distinct reversals exist; do not confuse them:

**(a) Reversing the constraints/indexes it added — no row is modified.**
Every constraint in the list below was *absent* before the repair (the identity
block only adds what the catalog said was missing), so dropping it restores the
pre-repair schema exactly. Row data is untouched.

```sql
-- Manual Supabase SQL — NOT EXECUTED BY AI. Reverses ONLY objects this repair
-- may have added. No row is modified. Owner decision only.
DROP INDEX IF EXISTS idx_saved_items_short_code;
DROP INDEX IF EXISTS uq_ai_task_occurrences_task_key;

ALTER TABLE saved_items         DROP CONSTRAINT IF EXISTS saved_items_save_code_key;
ALTER TABLE bio_state           DROP CONSTRAINT IF EXISTS bio_state_owner_id_key;
ALTER TABLE username_state      DROP CONSTRAINT IF EXISTS username_state_owner_id_key;
ALTER TABLE panel_settings      DROP CONSTRAINT IF EXISTS panel_settings_pkey;
ALTER TABLE bot_settings        DROP CONSTRAINT IF EXISTS bot_settings_pkey;
ALTER TABLE ai_config           DROP CONSTRAINT IF EXISTS ai_config_owner_id_key;
ALTER TABLE ai_sessions         DROP CONSTRAINT IF EXISTS ai_sessions_session_id_key;
ALTER TABLE ai_provider_stats   DROP CONSTRAINT IF EXISTS ai_provider_stats_pkey;
ALTER TABLE ghost_chats         DROP CONSTRAINT IF EXISTS ghost_chats_pkey;
ALTER TABLE ai_tasks            DROP CONSTRAINT IF EXISTS ai_tasks_pkey;
ALTER TABLE ai_task_occurrences DROP CONSTRAINT IF EXISTS ai_task_occurrences_task_id_fkey;
ALTER TABLE ai_task_occurrences DROP CONSTRAINT IF EXISTS ai_task_occurrences_pkey;
```

(Only drop a PRIMARY KEY if the pre-repair table had none — that is the only
state in which this repair would have created it. Dropping a PK that the
application relies on for `ON CONFLICT` will break its upserts.)

**(b) Reversing the columns this repair added to the canonical contract —
DATA-LOSING, one column at a time, owner-gated.** All six of the following are
columns live code writes; dropping them re-breaks the `ai_config` upsert and the
occurrence prepare-ahead path (§30.2 items 4–5). Only appropriate if the value
is unwanted.

```sql
-- Manual Supabase SQL — NOT EXECUTED BY AI. DESTRUCTIVE: each DROP COLUMN
-- discards the stored values for that column. Take a backup first.
ALTER TABLE ai_config           DROP COLUMN IF EXISTS show_question;
ALTER TABLE ai_config           DROP COLUMN IF EXISTS stt_model;
ALTER TABLE ai_config           DROP COLUMN IF EXISTS stt_language;
ALTER TABLE ai_config           DROP COLUMN IF EXISTS stt_passes;
ALTER TABLE ai_task_occurrences DROP COLUMN IF EXISTS preparation_metadata;
```

The same statement shape reverses any column this script created on a legacy
table (`ALTER TABLE <t> DROP COLUMN IF EXISTS <col>`) — but a column that
already existed on the live table before the repair cannot be distinguished
afterwards from one the repair added, and doing this loses data. **If you need
reversibility, take a Supabase backup before applying the script**; that is the
only contract that can be honoured for a schema that must converge.

**(c) Reversing the seeds — DATA-LOSING.**

```sql
-- Manual Supabase SQL — NOT EXECUTED BY AI. DESTRUCTIVE and NOT RECOMMENDED:
-- panel_settings('global') is REQUIRED (repository updates silently no-op
-- without it) and the five bot_settings rows are migration fidelity.
DELETE FROM bot_settings
 WHERE key IN ('auto_close_enabled', 'panel_auto_close_seconds',
               'max_deep_save_mb', 'delete_batch_size', 'log_cleanup_days');
-- DELETE FROM panel_settings WHERE key = 'global';   -- breaks the settings repository
```

Dropping the two tables this repair added to the canonical contract
(`ai_tasks`, `ai_task_occurrences`) is also possible, but they are created by
`20260829000001`, they hold the durable task system's state, and the
occurrence table has an `ON DELETE RESTRICT` foreign key — a plain
`DROP TABLE` will therefore fail while occurrences exist, and
`DROP TABLE … CASCADE` would discard them. Not provided.

### 30.10 Optional cleanup — separate from the reconciliation path

The following are the §20 removal proposals. They are **NOT part of the
canonical script**, they are **destructive**, and they remain owner-gated on a
live-data check. They are listed here so that "the canonical script" and
"proposed cleanup" cannot be confused with each other.

```sql
-- Manual Supabase SQL — NOT EXECUTED BY AI. OPTIONAL and DESTRUCTIVE.
-- Run only after confirming no row of value exists. Back up first.

-- Pre-check (read-only): is anything actually stored here?
SELECT count(*) AS legacy_ghost_chats_rows FROM ghost_chats;
SELECT count(*) AS rows_with_short_code FROM saved_items WHERE short_code IS NOT NULL;
SELECT count(*) AS rows_with_file_name  FROM saved_items WHERE file_name  IS NOT NULL;

-- §20 item 7 — dead saved_items columns and their indexes (no live writer).
DROP INDEX IF EXISTS idx_saved_items_short_code;
DROP INDEX IF EXISTS idx_saved_items_short_code_trgm;
DROP INDEX IF EXISTS idx_saved_items_file_name_trgm;
ALTER TABLE saved_items DROP COLUMN IF EXISTS short_code;
ALTER TABLE saved_items DROP COLUMN IF EXISTS file_name;

-- §19.14 / §19.13 — orphan panel_settings columns with no consumer.
ALTER TABLE panel_settings DROP COLUMN IF EXISTS update_stale_seconds;
ALTER TABLE panel_settings DROP COLUMN IF EXISTS ghost_seen_retention_seconds;

-- §19.12 — legacy table with zero code consumers.
DROP TABLE IF EXISTS ghost_chats;
```

`ai_messages.tool_calls` is intentionally **not** here: it does not exist in
this database (§19.5), and creating then dropping it would be pointless churn.

### 30.11 Manual Supabase action still required

Nothing in this document has been executed against Supabase by the coding
agent.

1. Open the Supabase **SQL Editor** as `postgres` and run the **complete**
   canonical script from the previous section — either paste the fenced block or
   paste `supabase/canonical_bootstrap.sql` / the migration file; the three are
   identical. It is one transaction-safe, re-runnable statement batch.
2. Read the output:
   * `WARNING` lines name any constraint that a pre-existing row blocked. Fix
     the named rows and re-run — the guard will then apply the constraint.
   * the final `missing_canonical_column` result set must be **empty**. Any row
     in it is a column the script could not create (see §30.12).
   * `NOTIFY pgrst, 'reload schema'` has already asked PostgREST to drop its
     stale schema cache, so the API sees the new columns immediately.
3. Then apply the **additive migrations newer than the snapshot** — currently
   `20260921000001_add_saved_items_display_name.sql` (idempotent: it adds the
   nullable `saved_items.display_name` and reloads the PostgREST cache) and
   `20260922000001_add_saved_items_search_indexes.sql` (the Save V2 resolver's
   two search indexes on `saved_items`; also idempotent). Each additive
   migration is self-contained and states its own copy-pasteable SQL; the
   snapshot is never regenerated to include them, so the trio above stays
   byte-identical.
4. Nothing else is required: no new table to create by hand, no env var, no
   Render setting, no Supabase Vault change.
5. §29 (the API Credential Vault) is still a **separate** owner action — with
   neither applied, the credential panel reports the store as not configured and
   every provider keeps its deployment key.

### 30.12 Limitations — what this validation does NOT prove

* **No PostgreSQL server was available** in the build environment and **no
  database was contacted**. The execution evidence of §30.8 is a faithful
  *simulation* of the statement semantics relevant to this defect (silent no-op
  CREATE, unresolved column reference, NOT NULL enforcement, ON CONFLICT
  skipping), not the real planner. Statements whose effects the simulator
  models statically — the `DO $$ … $$` guards, `CREATE POLICY`, `GRANT`,
  `NOTIFY` — are validated for identifiers and text, not executed.
* **Column types, defaults and nullability are not compared against the live
  database.** The drift report checks column *existence*. If a legacy column
  exists with an incompatible type (e.g. `bot_settings.value` as `integer`),
  the script cannot reconcile it safely and will neither alter nor report it.
  Types are only ever set by the CREATE, so a type conflict means the table is
  not this table; that case needs manual review.
* **RLS posture and the PostgREST schema cache are restated, not verified**
  against the live project.
* **Live Supabase state was NOT inspected** — the audit was performed on the
  repository's migration history, the canonical specification and the
  application's `.table()` call sites.
* **No application behaviour changed.** This repair touches schema
  reconciliation and documentation only: no Python module, no provider, no
  handler, no service, no dependency, no environment variable and no Supabase
  Vault object was modified.
