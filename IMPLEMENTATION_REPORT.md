# IMPLEMENTATION REPORT

## Request

Audit the connected Supabase database against the current repository contract,
identify missing or redundant schema/security objects, apply safe corrections,
and record the result in the canonical database documentation.

## Scope and evidence

- Repository: `Onlyicing1/Telegram-self-bot`, branch `main`.
- Supabase project: `burfebhahuowywyfgcsc`.
- Live catalog inspection: public tables, columns, constraints, policies,
  grants, default privileges, sequences, functions and event triggers.
- Repository inspection: `DATABASE_ARCHITECTURE.md`,
  `supabase/canonical_bootstrap.sql`, `AGENTS.md`, current implementation
  report/roadmap and schema/security tests.
- Supabase security and performance advisors were rerun after the migration.

## Live schema findings

### Completeness

- 28 public application tables are present.
- All 179 expected core columns from the canonical bootstrap are present.
- The audited later feature columns are also present, including the
  Appearance fields and `panel_settings.outbound_bandwidth_state`.
- No missing expected core or audited feature column was found.
- The singleton `panel_settings('global')` row has
  `outbound_bandwidth_state = {}`; the column is NOT NULL JSONB with an
  object-type CHECK.
- The Vault extension, `api_credentials` table and credential-management
  functions exist. `vault.secrets` has zero rows, which is normal until a
  credential is saved. No secret was created.

### Non-canonical / legacy objects retained

- `ai_preferences` is non-canonical, empty, has no foreign-key dependents,
  no application repository and no current producer. It is retained for now;
  it was not dropped.
- `ai_messages.tool_calls` and `ai_messages.telegram_msg_id` are present
  but empty and have no current persistence consumer. They were retained.
- `saved_items.short_code` and `saved_items.file_name` are documented
  compatibility columns and were retained.
- No table, column, row or stored value was deleted or rewritten.

## Migration history

- Repository now contains 39 migration files.
- Supabase migration history contains 3 records, not 39. The live schema
  reflects a mixture of canonical/manual setup and tracked migrations.
  Missing history rows were not fabricated.
- The Appearance migration history version (`20261109164548`) does not
  match its repository filename prefix (`20261109000000`).
- The outbound bandwidth migration is recorded as `20261112000000`.
- The new security migration is recorded as `20261111143543` and is stored
  as `supabase/migrations/20261111143543_harden_public_schema_privileges.sql`.

## Applied correction

Migration:
`supabase/migrations/20261111143543_harden_public_schema_privileges.sql`

Applied to the connected Supabase project successfully.

It:

1. Revokes `INSERT`, `UPDATE`, `DELETE`, `TRUNCATE`,
   `REFERENCES` and `TRIGGER` privileges from `anon` and
   `authenticated` on all public tables.
2. Revokes their privileges on public sequences.
3. Removes those client write privileges from the `postgres` role's
   default ACLs for future public tables and sequences.
4. Removes fourteen redundant/non-canonical policies, retaining the 22
   canonical `anon_select_*` SELECT policies.
5. Pins `search_path = pg_catalog` on
   `panel_settings_updated_at()`, `set_updated_at()` and
   `update_ghost_chats_updated_at()`.
6. Revokes direct EXECUTE on `rls_auto_enable()` from `PUBLIC`,
   `anon` and `authenticated`. The registered DDL event trigger remains
   enabled.

An initial attempt to alter the Supabase-managed `supabase_admin` default
ACL was rejected with permission denied. Those statements were removed from
the migration; only the `postgres` role defaults were changed. The migration
was then applied successfully. Do not claim the Supabase-managed defaults
were changed.

## Post-migration verification

The live verification query returned:

| Check | Result |
|---|---:|
| Remaining client table write grants | 0 |
| Remaining client sequence grants | 0 |
| Remaining non-canonical policies | 0 |
| Canonical SELECT policies retained | 22 |
| Public tables with RLS enabled | 28 |
| Trigger functions with pinned search_path | 3 |
| anon can execute rls_auto_enable() | No |
| authenticated can execute rls_auto_enable() | No |
| PUBLIC can execute rls_auto_enable() | No |
| ai_preferences rows | 0 |

## Remaining security and performance findings

### Public SELECT exposure — not changed

The 22 canonical `anon_select_*` policies still use `USING (true)`.
A holder of the project's anon key can read all rows in those tables,
including sensitive data in `ai_messages`, `ai_sessions`,
`ai_memories`, `ai_tool_history`, `saved_items` and `bot_logs`.

The repository says the React dashboard does not use the anon key and reads
through the backend API; the backend database client uses
`SUPABASE_SERVICE_ROLE_KEY`. A service-role-only model is therefore
consistent with the current source architecture. SELECT access was not
revoked in this migration because that is a separate access-model change.
It remains the most important unresolved security decision.

### Supabase advisors after the migration

- Security: six informational `RLS Enabled No Policy` findings. Five are
  intentional service-role-only tables; the sixth is the non-canonical,
  empty `ai_preferences` table.
- Security: one warning for `pg_trgm` being installed in `public`. It
  remains there because the canonical bootstrap expects it; moving it needs
  a separate index/operator compatibility review.
- Performance: 22 unused-index findings. Usage counters alone are not enough
  evidence to delete indexes on a low/unknown-traffic project.
- Performance: four duplicate-index pairs on `ai_sessions`, `bio_state`,
  `bot_logs` and `saved_items`. They were not dropped without reviewing
  definitions and query plans.

## Validation limits

- The migration was applied and verified against the live Supabase catalog.
- Supabase security and performance advisors were rerun.
- No Python test suite, application runtime test, Telegram interaction or
  Render deployment test was executed as part of this database-only task.
- No migration-history backfill was attempted.

## Outcome

The missing core columns were already present. The concrete live drift was
excess client write privileges, redundant policies, mutable trigger-function
search paths and unnecessary direct execution on the RLS event-trigger
function. Those items were corrected and verified.

The public SELECT exposure, unused/duplicate indexes, empty legacy table,
legacy columns and migration-history drift remain explicitly documented
rather than being changed destructively or silently.
