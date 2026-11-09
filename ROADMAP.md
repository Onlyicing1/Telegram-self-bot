# ROADMAP — Database Architecture Documentation Audit

## Current task (complete, pending owner-side delivery)

**Comprehensive, source-driven audit of `DATABASE_ARCHITECTURE.md`** against the
actual repository (`Onlyicing1/Telegram-self-bot`, branch `main`), with focused
regression tests. Documentation-only: no SQL was executed, no Supabase project
was contacted, no table, column, migration, policy, grant, service or runtime
behaviour was changed.

## Verified execution state

| Phase | State | Evidence |
|---|---|---|
| Read the whole architecture document and map its sections | Done | 8,305-line document re-read after editing; TOC and chapter numbering reconciled |
| Enumerate and read `supabase/migrations/` | Done | 37 SQL files; all 37 classified in §31, none missing |
| Verify table/schema claims against migrations | Done | 27 migrated tables; `ai_preferences` correctly documented as unmigrated |
| Verify `panel_settings` / Appearance | Done | Five columns, four CHECKs, singleton seed, 19 settings accessors in §6 |
| Verify Storage persistence model | Done | §35 documents `APPEARANCE_MEDIA_BUCKET`, `appearance/preview/`, 10-image gallery, 25 MiB / 8192 px / 50 MP bounds, key-only DB link |
| Reconcile `§1`, `§6`, `§22`, `§25`, `§26`, `§31.1.1` | Done | Stale counts and inventories corrected; no new audit-only appendix |
| Add regression tests | Done | `tests/test_database_architecture_doc.py` (12 tests) + two stale assertions updated |
| Run focused architecture/migration tests | Done | `97 passed` (doc contract + setup order + schema reconciliation + auto-reaction) |
| Run the full test suite | Done | See `IMPLEMENTATION_REPORT.md` for the recorded result |
| Python compilation / whitespace checks | Done | `py_compile` for the changed test module; `git diff --check` clean |
| Commit and fast-forward push | Done | See `IMPLEMENTATION_REPORT.md` |

## Owner action that remains (outside this repository task)

Applying the migrations is a **manual Supabase owner action**. The canonical
one-paste script is `DATABASE_ARCHITECTURE.md` §31.3 (parts 1–15); the Appearance
migration is part 15 (`20261109000000_add_panel_settings_appearance.sql`), and
the Private Message, Emoji, Anti-Edit/Delete and Vault objects are the other
manual-only parts. No coding agent executed any of it.

## Explicitly out of scope

- Creating/altering/dropping tables, columns, constraints, policies or grants.
- Editing the live Supabase project or running SQL.
- Changing application runtime behaviour, repositories or services.
- Fixing the implementation gaps that are documented as findings (for example
  `ai_messages.tool_calls` having no column, or `ai_preferences` having no
  migration/producer).
