# ROADMAP

## Completed: Supabase live schema and privilege audit

Status: **Audited, selectively hardened, live-verified, and documented.**

### Verified schema state

- 28 public application tables exist.
- All 179 expected core columns and the audited feature columns are present.
- The outbound-bandwidth JSONB state column and global row are present and valid.
- Vault infrastructure exists; zero saved secrets is currently normal.
- No table, column, row or stored value was deleted.

### Applied migration

`20261111143543_harden_public_schema_privileges.sql`

- Removed client INSERT/UPDATE/DELETE/TRUNCATE/REFERENCES/TRIGGER privileges
  on public tables.
- Removed client sequence privileges.
- Removed client write privileges from the `postgres` role's default ACLs.
- Removed fourteen redundant/non-canonical policies; retained 22 canonical
  SELECT policies.
- Pinned three trigger-function search paths to `pg_catalog`.
- Revoked direct PUBLIC/anon/authenticated EXECUTE on `rls_auto_enable()`
  without disabling its registered DDL event trigger.

Post-migration checks: zero client table write grants, zero client sequence
grants, zero non-canonical policies, all 28 tables still have RLS enabled,
three trigger search paths pinned, and no client/PUBLIC EXECUTE on
`rls_auto_enable()`.

## Decisions and cleanup still open

### 1. Public SELECT access — highest priority

The remaining 22 `anon_select_*` policies use `USING (true)`. They expose
all rows to anyone holding the project's anon key, including AI conversations,
memories, tool history, saved items and logs.

The repository says the React dashboard does not use the anon key and the
backend accesses Supabase with the service-role key. The recommended target
is therefore service-role-only access for application tables. Before applying
that separate access-model migration, confirm there is no external anon-key
consumer.

### 2. Non-canonical objects

- `ai_preferences`: empty, unused, non-canonical table. Retain until the
  repository contract and any external consumers are checked one final time.
- `ai_messages.tool_calls` and `telegram_msg_id`: empty legacy columns.
  Do not drop without a dependency/data-retention review.
- `saved_items.short_code` and `file_name`: compatibility columns; keep.

### 3. Index review

Supabase reports 22 unused indexes and four duplicate-index pairs:
`ai_sessions`, `bio_state`, `bot_logs`, `saved_items`. Review actual
definitions, query plans and workload before removing anything.

### 4. Migration-history reconciliation

There are 39 repository migration files but only three remote migration
history records. The live schema is present, but the migration ledger is
incomplete. Do not fabricate history rows. Reconcile each historical file
against its effective schema effect before any future ledger repair.

The Appearance migration's history version (`20261109164548`) differs from
its filename prefix (`20261109000000`).

### 5. Extension placement

`pg_trgm` remains in `public` as required by the current canonical setup.
Moving it is a separate compatibility task.

The Supabase health advisor currently reports no findings.

## Implemented: Intelligent Reaction (backend)

AI-driven reactions on the owner's own outgoing messages, independent of the
fixed Auto-Reaction setting:

- The probability is OWNER configuration, not an AI decision:
  `panel_settings.intelligent_reaction_probability` (integer 0–100, default 25)
  is set in the General panel and read for every eligible message, so a change
  applies without a restart. The AI never returns or overrides a probability.
- The AI answers only `{"reaction": <catalog emoji|null>}` — a legacy
  `probability` field invalidates the whole answer. Deterministic application
  code owns the probability: 0% never reacts, 100% always does, and every other
  value performs exactly one independent draw per message (no quota, rolling
  window, streak or compensation).
- Execution reuses the existing path: `reaction_service.react_to_message` →
  `telegram_api.reactions.send_reaction`. The target is always the owner's
  own outgoing message; a replied-to message is context only.
- Context is bounded: the message text plus at most ONE replied-to text. No
  ids, sender identity, usernames or chat history reach the model.
- Fail closed: an unavailable catalog, a provider failure, invalid or legacy
  output, an unsupported reaction or a refused Telegram call sends nothing and
  never falls back to a fixed emoji. One stable outcome status per evaluation.
- The two reaction modes are mutually exclusive in `settings_service`:
  enabling one disables the other.

Tests: `tests/test_intelligent_reaction.py` (offline, scripted provider and
Telegram fakes). Migrations:
`supabase/migrations/20261112140000_add_panel_settings_intelligent_reaction.sql`
adds the single `panel_settings.intelligent_reaction_enabled` column (part 17
of the one-paste setup), and
`supabase/migrations/20261112160000_add_panel_settings_intelligent_reaction_probability.sql`
adds the probability-only column (0–100 CHECK, default 25) without touching the
enabled column. Neither is **applied** — that remains a manual owner action.
