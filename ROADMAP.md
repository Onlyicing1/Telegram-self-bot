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
- Requests are coalesced, never timer-batched: the first eligible message is
  evaluated immediately, messages arriving while one AI request is in flight
  wait in a per-owner queue and share the next request, each keeping its own
  id, context and send. A coalesced answer must identify every message it
  answers; an unknown, duplicate or missing result rejects the whole batch.
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

## Investigation complete (P0): Unified History & Event Intelligence

Status: **P0 complete — investigated, specified, no runtime change.** The
source-verified investigation lives in `INVESTIGATION.md`; this section is the
execution memory for the implementation phases. Base commit
`ca0399b2156999a065444f31a31a58bb38841663`.

### Problem and goal

Summarization already exists but is reachable only as an AI-chat tool call, is
all-or-nothing per request, and has no UI, no per-chat checkpoint, no automatic
per-chat processing and no shared candidate path to Task/Memory/Saved Items.
The goal is to evolve the existing bounded engine into a user-configurable
History & Event Intelligence feature: manual and link-based summarization from
the Glass UI, reliable incremental per-chat processing, then one text-first
candidate pipeline — without a second summarization engine, a second Telegram
client, a second scheduler or a parallel execution authority.

### Current verified implementation

- `backend/services/history_service.py` is the bounded retrieval layer over the
  `telegram_api` facade only (no Telethon import, asserted by tests).
  `MAX_HISTORY_MESSAGES = 1000` (:56) is the single count authority;
  `DEFAULT_PAGE_SIZE = 100` (:58), `MAX_PAGE_SIZE = 1000` (:60),
  `MAX_HISTORY_SCAN_MESSAGES = 5000` (:64), `HISTORY_RPC_TIMEOUT_S = 5.0`
  (:66). `fetch_recent_history` (:303), `fetch_history_page` (:358) and
  `iter_history_pages` (:387) already take exclusive `before_id` (→ Telethon
  `max_id`) and `after_id` (→ `min_id`), return chronological, de-duplicated
  messages, exclude AI-provenance messages centrally and raise `HistoryError`
  instead of returning empty on failure.
- `backend/services/history_ai_service.py` is the ONE map-reduce engine:
  `summarize_history` (:785) / `translate_history` (:695) with
  `(source, chat_id, *, count, instruction, provider_manager,
  current_message_id, request_id, timeout_s) → (success, text, data)`.
  `DEFAULT_ENVELOPE_S = 240.0` (:73), `MAP_CONCURRENCY = 2` (:83),
  `MAX_CALL_SPACING_S = 5.0` (:88), chunk budgets from
  `DEFAULT_MAX_CONTEXT_TOKENS` (:113-121), one deadline/capacity model
  (:127-175), `MAX_INSTRUCTION_CHARS = 500` (:188), stable error codes
  (:201-247). Oversized single messages are their own chunk and are never
  split; chunk/reduce/provider failures are never presented as a summary.
- `backend/ai/tools/history_ai.py` exposes both operations to the model as
  READ_ONLY, safe, long-running tools capped at 1,000; registered in
  `backend/ai/tools/registry.py:151,248-249`, progress labels in
  `executor.py:74-75`, executed without confirmation (`executor.py:370`), and
  delivered **verbatim** by `backend/ai/engine/dispatcher.py`
  (`_VERBATIM_READ_TOOLS`, :92-105) so no continuation round can paraphrase
  them. `_build_tool_context` (:1484) supplies `chat_id`,
  `request_message_id`, `request_timeout_s`, `request_id` and the live
  `provider_manager` (:1523).
- The manual request path is `ai_unified.py` → `AIRequest` (:757,
  `_AI_EXECUTE_TIMEOUT = 240.0`, :72) → dispatcher → ToolExecutor (:867) →
  history tool → history AI service → history service → `telegram_api
  .messages.iter_messages` (:144) → `ProviderManager.chat` via `_call` (:417).
  There is **no Glass UI entry** and **no checkpoint** anywhere in that chain.
- Links: `backend/services/save_service.py` is the one parser/resolver.
  `parse_link_target` (:1026) supports `https://t.me/<username>/<id>`,
  `https://t.me/c/<chat>/<id>` (→ `-100<id>`) and `tg://openmessage` with exactly
  one of `user_id`/`chat_id`/`channel_id` plus `message_id` (or `post`). A link
  **without a message id is refused**. `_fetch_linked_message` (:2181) resolves
  via `_resolve_link_peer` (:1172: username, session cache, entity, then a
  bounded 500-dialog scan) and validates that the message belongs to the
  requested peer; failures are classified `peer_unresolved` /
  `message_missing` / `lookup_failed`. No second client, no access bypass.
- UI mechanism: `backend/helper/panels.py::register_panel` (:279),
  `register_action` (:292), `register_input` (:301);
  `backend/helper/panel_registry.py` owns the panel tree;
  `backend/helper/input_state.py::set_pending` (:27) is the one-shot input path
  with its own expiry; `misc.py` shows the exact registration pattern (:668-669,
  :691, menu rows :218-234).
- Runtime/scheduling: `RuntimeSupervisor._start_task_scheduler`
  (`backend/runtime/supervisor.py:464`) builds ONE `TaskScheduler` with the
  shared `TaskRepository`, `TaskExecutionCoordinator`, `ToolExecutor`,
  `ToolRegistry` and outcome notifier, and stops it on shutdown (:528).
  `TaskScheduler` (`backend/ai/task_scheduler.py`) polls at
  `WAKE_INTERVAL_SECONDS = 60` (:22) with claim/CAS occurrences
  (`occurrence_key`, :42), bounded retries and `recover()` (:94).
  `backend/bot/handlers/task_events.py` already handles every new message on
  the existing update path and normalizes it with
  `task_event_dispatcher.extract_event_context` (:66), with deterministic
  per-message idempotency (`event_occurrence_key`, :40) and per-message caps
  (:33-35). The profile cron (`backend/profile/scheduler.py`) is the
  bio/username cadence, not a general job runner.
- Media/bandwidth: `backend/services/media_service.py` is the one bounded media
  boundary (`max_download_bytes` :490 reuses the Deep Save limit;
  `download_timeout` :503; resolution 30 s :143). Video/GIF/Animation are
  explicitly unprocessable (:162) — there is **no video analysis**. OCR and STT
  seams exist (`set_ocr_engine` :614, `set_stt_engine` :659) but only work when
  an engine is provisioned (`gemini_media_engine.py:1869-1870`,
  `stt_engine_factory.py:294`). `media_ai_service.answer_media_request` (:320)
  sends only a static instruction + the owner request + `MediaAnalysis
  .as_context_text()`. `outbound_bandwidth_service.reserve_outbound_bytes`
  (:171) has exactly one caller — Gemini media payload uploads — so Telegram
  downloads are inbound to Render and ungoverned, while provider uploads are
  reserved fail-closed. No measurement exists.

### Confirmed architectural constraints

- One summarization engine: `history_service` + `history_ai_service`. A new
  engine would duplicate the count bound, provenance filter and budget model
  that the tests pin.
- One Telegram client and one ordinary authenticated path. Parsing a link never
  grants access; `peer_unresolved` is the honest outcome.
- One runtime authority: `RuntimeSupervisor → Dispatcher → ProviderManager /
  ToolRegistry → ToolExecutor → existing service`. No new loop may be added for
  automatic processing.
- AI may classify/propose; deterministic code validates, authorizes and
  executes. No chat ids, message ids, sender identity, usernames or unrelated
  conversation may be sent to a provider.
- Glass UI additions use `register_panel`/`register_action`/`register_input`
  and the existing menu tree — no new panel API.
- Text-first: local deterministic filtering, batched candidates, no
  per-message LLM call, no media download or upload by default.

### Decisions and open questions

Proposed decisions:

1. P1 is one `history` panel (parent `menu`) with manual chat selection and a
   message-link field, calling `history_ai_service.summarize_history` with
   `get_engine().provider_manager` (same accessor as
   `backend/ai/tools/settings.py:113`). No persistence, no scheduler, no new
   tool.
2. Link → history is `before_id = linked_message_id + 1` (exclusive), i.e. the
   N messages ending at the linked message; the linked message is resolved by
   the existing `save_service` path.
3. P2 persists one row per `(owner_id, chat_id)` with `last_message_id`,
   `last_success_at`, `last_status`, bounded `last_error`,
   `last_summary_message_id` + a bounded summary reference, `enabled`,
   `cadence_minutes`, and a `version` for CAS. Advance only after the AI
   service returns success **and** the checkpoint/delivery write succeeds;
   retain on every failure; `running` + CAS blocks overlap.
4. P3 triggers automatic processing through the existing `TaskScheduler`
   (interval occurrences) and the existing `TaskExecutionCoordinator →
   ToolExecutor → history service` path, inheriting claim/CAS, retries,
   recovery, shutdown and outcome notification.
5. P4 attaches candidates at the existing `task_events` handler position, with
   a deterministic filter → one bounded classification request per batch →
   deterministic application, and payloads that contain no Telegram
   identifiers.

Unresolved (owner or P-phase verification):

- Whether chat-only links (no message id) should be supported; they are refused
  today and would need a deliberate parser extension.
- Whether the checkpoint lives in a dedicated table or a JSONB per-chat map in
  an existing config table. **No SQL, migration, Supabase or
  `DATABASE_ARCHITECTURE.md` change is made in P0**; the owner applies schema
  separately.
- Whether `task_contract.validate_prepared_action` accepts an internal action
  kind, and whether one scheduled row per chat or one owner-level sweep is
  preferable. Neither is verified yet.
- Exact chat-picker paging source/limits in the Glass UI.
- Deleted/edited messages cannot be detected or re-read without the archive
  subsystem; this is documented behavior, not coverage.

### Phase plan and dependencies

| Phase | Deliverable | Depends on | Status |
|---|---|---|---|
| P0 | Investigation + specification | — | **Complete** |
| P1 | Manual + link-based summarization panel | P0 | Planned |
| P2 | Incremental retrieval + per-chat checkpoint | P1 | Planned |
| P3 | Opt-in automatic per-chat digest | P2 | Planned |
| P4 | Text-first candidate pipeline | P2 (can ship independently) | Planned |
| P5 | Task / Memory / Saved Items integration | P4 | Planned |
| P6 | Selective bounded media inspection | P4 | Planned |
| P7 | Digest delivery + operational controls | P3 | Planned |

Per-phase detail (goal, user-visible behavior, reused files, likely changed
files, persistence, security, failure, acceptance, automated tests, live tests,
readiness) is in `INVESTIGATION.md` §11.

### Feature-level acceptance criteria

1. Manual run summarizes exactly the stated chat and range, ≤1,000 messages,
   and refuses more at the established bound.
2. A link run resolves through the existing parser/resolver and reports
   `peer_unresolved` / `message_missing` / `lookup_failed` specifically; it
   never claims access it does not have.
3. An incremental run fetches only messages after the checkpoint; a second run
   with nothing new makes zero provider calls and leaves the checkpoint alone.
4. No failure path (retrieval, provider, timeout, cancellation, persistence)
   advances the checkpoint, and no overlap can double-process a chat.
5. Automatic processing is opt-in per chat, runs through the existing scheduler
   authority, survives restart without duplicate execution, and surfaces status
   and last error.
6. Candidate processing sends only selected text to the provider, never ids or
   identity, and makes at most one bounded request per batch.
7. No media is downloaded or uploaded without a deterministic relevance signal;
   outbound provider transfers are reserved; video is refused honestly.

### Deferred scope

- Chat-only links; cross-chat or account-wide digests.
- Edit/delete detection or re-processing of edited history.
- Automatic acceptance of Task/Memory/Saved Items candidates (proposals only)
  and any new destination service.
- Media inspection beyond captions, all video processing, and any change to the
  Media Service contract.
- New schedulers, new Telegram clients, new summarization engines, new queues or
  background workers.
- Any migration, Supabase change or `DATABASE_ARCHITECTURE.md` edit.

### Phase status

- **P0 — complete.** `ROADMAP.md` and `INVESTIGATION.md` updated; no source,
  migration, settings or runtime change.
- **P1–P7 — not started.** Each begins only after the previous phase's
  acceptance tests and live checks pass, and after any schema required by P2 is
  applied by the owner.
