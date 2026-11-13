# ROADMAP

## Feature: Unified History & Event Intelligence

### P0 — Investigation and specification

Status: **Complete** (P0 contract supplied to P1). The P1 brief's source
requirements were treated as authoritative for this phase.

### P1 — Manual and link-based history summarization

Status: **Complete**. Delivered through the existing Glass UI and the existing
history stack; no new summarizer, provider manager, Telegram client, listener,
scheduler or persistence exists.

- One `History & Intelligence` panel (`panel:history`, parent `menu`) with a
  main-menu entry. It shows the selected chat, the bounded message count
  (100/250/500/1000) and the optional focus instruction.
- Chat selection lists the account's own dialogs with `get_dialogs(limit=100)`,
  paginates eight per view, and handles empty lists and RPC failures without
  fabricating entries. A selection is re-validated against that same bounded
  list before a run.
- Manual runs call `history_ai_service.summarize_history` directly with
  `current_message_id=None`, the selected chat/count/focus, the existing
  self-client and `get_engine().provider_manager`. No AI-chat tool call is
  involved.
- Link runs reuse `save_service.parse_link_target` and
  `save_service._fetch_linked_message`, validate the resolved message id and
  peer, then summarize with `current_message_id = linked_message_id + 1`. The
  history service's upper bound is exclusive, so the linked message is the
  last included message and nothing newer is read.
- The result reports the selected chat, processed vs requested messages, the
  exact first/last eligible message ids when available, and whether the window
  was complete, bounded/truncated or empty. A provider/retrieval failure or an
  empty history is never rendered as an invented summary.
- The UI uses an optional, backwards-compatible anonymized summarization mode:
  the provider receives history text and the owner's focus only, with no
  message ids or sender ids. Existing AI-chat callers keep their original
  attributed prompt by default.
- Long runs execute in one finite `guarded_create_task` with an explicit
  300-second envelope, the existing panel-operation generation (newest writer
  wins) and `ExistingPanelEditor`; input handlers only validate and launch.
  Cancellation propagates; no permanent loop or worker was added.
- Tests: `tests/test_history_panel.py` covers registration/navigation, bounded
  dialogs, selection validation, counts, focus limits, manual argument
  passing, provider failure, empty history, timeout, cancellation, stale
  writers, all supported link forms, distinct link failure kinds, peer/message
  mismatch, the `linked_id + 1` boundary, and the UI prompt's identifier
  isolation. `tests/test_history_ai_tools.py` remains green.
- Acceptance: offline focused tests pass; no live Telegram or provider run was
  performed in this environment.

### P2 — Incremental retrieval and per-chat checkpoints

Status: **Next**. Not implemented in P1. Per-chat checkpoints, incremental
summarization, previous-summary persistence, automatic per-chat mode, cadence
configuration, scheduler integration, Event Intelligence candidates, and
automated digest delivery remain deliberately out of scope.

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

- The AI returns a per-message `probability` (0–100) and a reaction from
  Telegram's available-reaction catalog, or no reaction. There is no fixed
  ratio, global quota, rolling window or compensation for missed messages.
- Each message then passes its own independent random draw
  (`draw * 100 < probability`); 0% never reacts and 100% always does.
- Execution reuses the existing path: `reaction_service.react_to_message` →
  `telegram_api.reactions.send_reaction`. The target is always the owner's
  own outgoing message; a replied-to message is context only.
- Context is bounded: the message text plus at most ONE replied-to text. No
  ids, sender identity, usernames or chat history reach the model.
- Fail closed: a provider failure, invalid output, an unsupported reaction or
  a refused Telegram call sends nothing and never falls back to a fixed emoji.
- The two reaction modes are mutually exclusive in `settings_service`:
  enabling one disables the other.

Tests: `tests/test_intelligent_reaction.py` (offline, scripted provider and
Telegram fakes). Migration
`supabase/migrations/20261112140000_add_panel_settings_intelligent_reaction.sql`
adds the single `panel_settings.intelligent_reaction_enabled` column (part 17
of the one-paste setup) and is **not yet applied** — that remains a manual
owner action.
