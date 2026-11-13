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
`ca0399b2156999a065444f31a31a58bb38841663`. P1 is implemented on top of this
specification; its verified outcome is recorded below.

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
- Exact chat-picker paging source/limits in the Glass UI. Resolved in P1:
  `get_dialogs(limit=100)`, eight chats per page, with no unbounded walk.
- Deleted/edited messages cannot be detected or re-read without the archive
  subsystem; this is documented behavior, not coverage.

### Phase plan and dependencies

| Phase | Deliverable | Depends on | Status |
|---|---|---|---|
| P0 | Investigation + specification | — | **Complete** |
| P1 | Manual + link-based summarization panel | P0 | **Complete (offline verified)** |
| P2 | Incremental retrieval + per-chat checkpoint | P1 | Planned |
| P3 | Opt-in automatic per-chat digest | P2 | Planned |
| P4 | Text-first candidate pipeline | P2 (can ship independently) | Planned |
| P5 | Task / Memory / Saved Items integration | P4 | Planned |
| P6 | Selective bounded media inspection | P4 | Planned |
| P7 | Digest delivery + operational controls | P3 | Planned |

Per-phase detail (goal, user-visible behavior, reused files, likely changed
files, persistence, security, failure, acceptance, automated tests, live tests,
readiness) is in `INVESTIGATION.md` §11.

### P1 implementation outcome

Status: **Complete in code and offline tests; live Telegram verification not
performed in the delivery environment.** Implementation commit
`8434c26` (merged with P0 at `c83a557`). P1 is still manual only: no
checkpoints, incremental runs, persistence, scheduler, automatic cadence or
candidate pipeline exists.

- One `History & Intelligence` panel (`panel:history`, parent `menu`) with one
  main-menu row. It shows the selected chat, count (100/250/500/1000) and
  optional focus.
- `get_dialogs(limit=100)` supplies the picker, eight per page. Empty lists and
  RPC failures are shown honestly; a chosen peer is re-validated against the
  same bounded list before a run.
- Manual runs call `history_ai_service.summarize_history` directly with
  `current_message_id=None`, the selected chat/count/focus, the existing
  self-client and `get_engine().provider_manager`. The AI-chat tool path is not
  used.
- Link runs reuse `save_service.parse_link_target` and
  `_fetch_linked_message`, validate the resolved message and peer, and pass
  `current_message_id = linked_message_id + 1`. Because the history upper bound
  is exclusive, the linked message is included and newer messages are not.
- Results report chat, processed/requested counts, first/last IDs, and whether
  the window was complete, bounded/truncated or empty. Provider/retrieval
  failures and empty history are never rendered as a summary.
- The UI uses a backwards-compatible identifier-free summarization mode
  (`with_identifiers=False`): the provider receives only history text and the
  owner's focus, with no message or sender IDs. Existing AI-chat callers keep
  their attributed prompt by default.
- Runs use one finite `guarded_create_task`, a 300-second envelope, the
  existing panel-operation generation and `ExistingPanelEditor`; cancellation
  propagates and stale writers cannot overwrite a newer panel. Input handlers
  only validate and launch.
- Tests: `tests/test_history_panel.py` covers registration/navigation, dialog
  bounds and paging, selection validation, count choices (including invalid
  values), focus limits, manual argument passing, provider/empty/timeout
  outcomes, cancellation, stale writers, all supported link forms and failure
  kinds, peer/message validation, the `+1` boundary, and provider-prompt
  identifier isolation. Existing history-service and history-AI tests remain
  green.

#### P1 result-delivery hotfix (production evidence)

Status: **Fixed in code and offline tests; live Telegram verification still
outstanding.** Deployed logs showed the summary itself succeeding but the final
panel edit failing with `The caption is too long` and the fallback self-client
edit failing with `The action must be performed through an inline bot callback`,
leaving the panel on its progress state.

- Root cause: with Appearance preview active the panel is a PHOTO message, so
  its text is a caption bounded to 1024 characters, while the summary body may
  reach 2800 plus metadata. `_run_summary` ignored `_paint()`'s `False` return,
  so a refused edit still completed the operation as if delivered.
- Fix: the delivery state recorded by `appearance_service` selects the path. A
  media panel whose result exceeds the caption limit (or any refused final
  edit) is delivered by `PanelLifecycleManager.replace_panel` as ONE text-only
  panel: the non-empty replacement extra makes Appearance skip media
  composition, the result view is carried by a bounded one-shot token, and the
  existing lifecycle creates the new message before deleting the old one. No
  second client, listener, worker, scheduler or persistence is added.
- Safety: replacement is allowed only when the panel chat is the owner's OWN
  private chat (Saved Messages); elsewhere the panel receives a short honest
  failure notice and `HISTORY_RESULT_DELIVERY_FAILED` is logged. A failed
  Telegram edit is never reported as a delivered summary.
- Focus was not independently defective: its save/normalize/clear path and the
  `instruction=focus or None` pass-through are correct and now covered by
  regression tests; Focus summaries were affected only by the shared delivery
  defect.
- Live verification remains outstanding: it requires a real Telegram run with
  Appearance preview active and a summary above 1024 characters.

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
- **P1 — complete in code and offline tests.** Panel, bounded chat picker,
  manual and link summarization, range reporting and failure rendering are
  implemented with the existing engine. Live account runs were not possible in
  the delivery environment and remain the owner's acceptance step. No schema
  was required or changed.
- **P2–P7 — not started.** Each begins only after the previous phase's
  acceptance tests and live checks pass, and after any schema required by P2 is
  applied by the owner.
