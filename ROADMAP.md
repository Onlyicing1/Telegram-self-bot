# ROADMAP

## Current delivery: G2-A — Global, request and task language

Status: **Implemented offline. G2-B (Workspace language), G4-A (extraction and
glossary), G5-A and Scheduled History Phase 4+ remain deferred.** No schema,
migration, Supabase, Telegram, Render or live-operation change was made by
G2-A. A G4-A evidence-persistence **design record** follows below; nothing in
it is implemented.

### What G2-A adds

- **One catalog and resolver** (`backend/ai/language.py`): stable codes with
  labels and directionality for `en`, `fa`, `ar`, `zh` (Persian and English
  required; Taskloom's existing four preserved). External values are validated;
  unsupported values are refused, never coerced.
- **Deterministic precedence:** explicit request > explicit task > global
  `ai_preferences.language` > English. The Glass UI language
  (`panel_settings.language`) is untouched and remains UI-only.
- **Explicit Auto:** available for source-based summaries, never inferred, and
  never able to override an explicit task language. A source's dominant script
  is used when it can be resolved; otherwise the documented English fallback is
  reported honestly. Translation exposes concrete languages only (Auto there
  would be a no-op).
- **One preference snapshot per request:** the dispatcher loads preferences
  once, resolves the language, and passes the same decision to the prompt and to
  every tool. `AIRequest.language` now means "explicit only" (empty otherwise).
- **History:** `summarize_history` accepts a validated language; the same
  decision drives every map call and the reduce call; translation routes its
  language through the same catalog. Result metadata carries the effective
  language, its source and Auto detection. The panel gains a request-level
  language selector and renders the effective language honestly.
- **Tasks:** Taskloom's language choices come from the catalog; the wizard's
  generated-content instruction freezes the owner's global default at creation
  (an explicit task language always wins), so a later preference change cannot
  rewrite an existing task. `preparation_policy` is unchanged and still
  fail-closed.
- **UI:** AI → Settings exposes the global content language (with an honest
  in-memory note while `ai_preferences` has no deployed table). Taskloom keeps
  its task-level selector and shows the effective language in review.

### Limitations reported for G2-A

- The global content language lives in the existing in-memory
  `ai_preferences` repository: there is no deployed `ai_preferences` table, so
  the setting resets on restart. The UI says so instead of claiming a durable
  write.
- Natural-language task instructions are preserved verbatim (source-fidelity
  contract), so the global default is frozen only for wizard-generated content.
  Freezing it for NL tasks needs a durable task-language field (schema change),
  which G2-A does not add.
- No extraction surface exists yet; G4-A must consume the same resolver.
- No live Telegram or provider compliance test was performed.

### G2-A corrective pass (2026-11-15)

- **Invalid language values are rejected.** The dispatcher refuses an
  unsupported explicit request language (or an invalid stored global
  preference it would actually use) through the existing `EngineResult` failure
  convention, before any provider call. No English fallback is substituted and
  no fallback decision is recorded.
- **Prompt language is unambiguous.** The preferences block renders the global
  default as `Global default language`; the runtime context renders the
  effective request language as `Language: … (effective for this response)`.
  Auto keeps its honest detected/fallback wording.
- Catalog, aliases, precedence, the single preference snapshot and the
  one-resolution-per-request design are unchanged. No schema or live operation.

## Design record (NOT IMPLEMENTED): G4-A evidence persistence and bounded storage

Status: **Design and documentation only, 2026-11-15.** No production code,
migration, SQL, Supabase object, Telegram operation or deployment was made.
G4-A remains unimplemented, no stage below is complete, and **no evidence table
exists**. This section records the owner-approved product direction and the
storage model that must be approved before Stage 1 starts.

### Approved owner decisions (2026-11-15)

| # | Decision | Effect on G4-A |
|---|---|---|
| 1 | Working evidence is temporary. Nothing is archived automatically; selected evidence may be archived only through an owner-confirmed workflow. | Two retention classes, never one unlimited table. |
| 2 | Ordinary summaries stay plain. Cited Summary is an explicit, owner-selected mode. | The existing summary default does not change. |
| 3 | Follow-ups must remain usable after an application restart. | The active run and the context it needs must be durable. |
| 4 | Conditional subjective assessments are allowed only after explicit evidence thresholds and assessment policies are approved. | No threshold, default or confidence rule may be invented. |
| 5 | Person-level judgments require explicitly approved policies and thresholds. | Gated; no provider may assign a definitive person label. |
| 6 | The Workspace Glossary is persistent, owner-editable and extensible, and distinct from AI memory and source evidence. | Its own bounded store; no automatic learning from conversations. |
| 7 | Direct Telegram links are allowed in the owner-facing Evidence Explorer, but identifiers and URLs are resolved and constructed locally. | Provider output is never authoritative for ids, counts, links or existence. |
| 8 | Strict JSON is the initial structured-output contract. A tagged-text fallback may be specified later. | No fallback now, and no silent downgrade of a JSON operation. |
| 9 | Evidence Envelopes never enter ToolResult history, tool arguments, generic audit records, conversation history or provider continuation. Tool results may carry only opaque run ids and bounded counts/coverage. A dedicated access-controlled evidence store is permitted. | A new storage path, never the generic tool path. |
| 10 | Scheduled History is unchanged: body-only and citation-free. | `scheduled_summary_payload` and its callers are untouched. |
| 11 | Archive is hybrid: the system may suggest candidates, the owner approves each archival, and archived evidence remains until manually deleted. | No automatic archival and no automatic archive expiry. |

### Retention classes (must remain distinct)

| Class | Lifetime | Automatic expiry | Authority |
|---|---|---|---|
| **Active Evidence Run** | Short, explicit TTL | Yes (lazy/database-time, no scheduler) | Supports Cited Summary, the Explorer and restart-safe follow-ups. |
| **Archived Evidence Source** | Indefinite until manual deletion | No | Owner-approved long-term source; the only indefinite-retention class. |
| **Derived Workspace Knowledge** | Derived record, later stage | Follows its source's deletion | References an archived source id; never a second copy of the source text. |
| **Workspace Glossary** | Persistent | No (owner CRUD only) | Owner definitions; not evidence and not AI memory. |

An Active Run is **not** an archive. An archive row is **not** a chat export. A
derived record is **not** a source. The glossary is **not** evidence.

### Proposed storage model (conceptual only — no schema exists)

No table, migration file, SQL statement or Supabase object was created. The
concepts below follow the existing `history_schedules` conventions
(`supabase/migrations/20261114180758_create_history_schedules.sql`): additive,
service-role-only, RLS enabled with zero policies, and no owner foreign key
(there is no owner table).

| Concept | Lifetime | Essential contents | Deletion |
|---|---|---|---|
| `ai_evidence_runs` | TTL | server-generated opaque `run_id`, `owner_id`, `peer_id`, mode, status, `created_at`, `expires_at`, bounded message range, bounded summary text, reference/text counters, `version` | Expired rows are deleted with their refs; an expired lookup fails honestly. |
| `ai_evidence_run_refs` | TTL (child of the run) | `run_id`, ordinal, Telegram `message_id`, sender/user attribution flag, source date, bounded excerpt, content hash, captured-at, last-validation state | `ON DELETE CASCADE` from the run. |
| `ai_evidence_archive` | Indefinite until manual deletion | opaque `archive_id`, `owner_id`, `peer_id`, `message_id`, bounded source text/excerpt, hash, sender attribution flag, source date, `archived_at`, `origin_run_id` (provenance only, not a blocking FK), revision | Explicit owner-confirmed delete; dependent derived records cascade; no time-based delete. |
| `ai_workspace_glossary` | Persistent | `owner_id`, workspace key, normalized term, term, definition, `version`, timestamps | Owner-confirmed delete; later operations only. |
| `ai_workspace_knowledge` (later) | Derived | archived-source id, claim/summary, run/analysis provenance, revision | Cascades from its source; never outlives a deleted archive row silently. |

Storage rules:

- **Owner and Workspace isolation first.** Every read/write is filtered by
  `owner_id`; the Workspace key is validated against the owner's own scope. An
  opaque id alone is never authorization.
- **Runs are bounded and temporary.** Expiry is evaluated on read and on write
  (lazy), so no background scheduler or second worker is required. An expired
  run is reported as expired; its evidence is never reconstructed from
  conversation history or a later chat read.
- **Archive is deduped.** One row per `(owner_id, peer_id, message_id)`. A
  re-archival of the same message proposes a refresh of the snapshot instead of
  a second copy. An excerpt/content hash detects an edited source.
- **Text is stored once per class.** A run stores only the bounded excerpts it
  needs; the archive stores the one long-term copy; derived records reference
  the archive id instead of copying text.
- **No media binaries, no full chats, no sender names.** Only bounded text,
  media presence flags and local attribution.
- **Revalidation is local.** Before presenting evidence as current, the source
  message is re-read through the existing Telegram boundary; deleted/edited or
  inaccessible messages are labelled from the captured snapshot + hash instead
  of being silently refreshed.

### Bounds and quotas — AWAITING OWNER APPROVAL

Every value below is a **proposal**. None is approved, and implementation must
not hard-code one before the owner accepts it.

| Parameter | Proposed value | Rationale |
|---|---|---|
| Maximum stored excerpt per source | 1,000 characters (no full-message copy) | Enough to verify a claim; bounds every row. |
| Maximum refs per run | 50 (follow-up reads at most 20 of them) | Matches the bounded History request shape. |
| Maximum stored text per run | 32 KiB UTF-8 | Same order as the existing 32 KiB summary bound. |
| Maximum active runs per owner | 20 | Bounds steady-state temporary storage. |
| Active-run TTL | **7 days** recommended (options: 24 h, 72 h, 7 d, 30 d) | Keeps a follow-up usable for a week without becoming an archive. |
| Archived records per owner | 5,000 | Bounded indefinite growth. |
| Archived bytes per owner | 50 MiB of text | Same bound expressed in bytes; whichever is reached first refuses new archival. |
| Glossary entries per workspace | 200 | Bounds prompt injection size. |
| Glossary characters per workspace | 50,000 | Bounds the injected block. |
| Duplicate strategy | `(owner_id, peer_id, message_id)` unique + content hash | Prevents repeats; detects edits. |
| At-limit behavior | Refuse honestly after sweeping expired runs; never delete archived evidence | The owner's approved retention is not silently violated. |

### Supabase growth scenarios (estimates, not measurements)

Assumptions: an average excerpt of 400 characters at 2 bytes/character
(Persian/Arabic UTF-8) ≈ 800 B of text, ≈ 250 B of row metadata/index entry, so
≈ 1.1–1.4 KB per archived row; a maximum-length excerpt ≈ 2.9 KB per row. A run
with 25 refs and a short summary ≈ 36 KB. These are planning assumptions, not
measured database facts. No Supabase billing figure or plan limit is asserted.

| Scenario | Archived rows/month | Archive growth/month | Archive growth/year | Live active runs (7-day TTL) | Active-run footprint |
|---|---|---|---|---|---|
| Small personal | 20 | ≈ 25 KB | ≈ 0.3 MB | 3/day → ≈ 21 | ≈ 0.8 MB |
| Moderate regular | 200 | ≈ 250 KB | ≈ 3 MB | 10/day, capped at 20 | ≈ 0.8 MB |
| Heavy collection | 2,000 | ≈ 2.5 MB | ≈ 30 MB | 50/day, capped at 20 | ≈ 0.8 MB |

At the proposed 50 MiB archive quota, the heavy scenario reaches the bound in
roughly 18–20 months (≈ 37,000 average rows); the owner then archives only what
still matters or raises the quota explicitly. For comparison, the existing
conversation tables already store up to 8,000 characters per message
(`backend/ai/persistence.py`) with no cleanup, so evidence storage is a small
addition, not the dominant growth source.

### Privacy, authorization and provider isolation (revalidated against `416918e`)

The earlier leak-path findings still hold in the current source:

- `backend/ai/tools/executor.py:380` records every execution, and
  `:501`/`:504` persist `arguments` and `result_data=result.data` into
  `ToolHistoryRecord`.
- `backend/ai/engine/dispatcher.py:2341` places the full `ToolResult.data` into
  the tool message of the provider continuation.
- `backend/ai/engine/dispatcher.py:975`, `:1238` and `:1740` send the tool
  result message into conversation history, which
  `backend/ai/runtime/manager.py` persists via `persistence.add_message` into
  `ai_messages` (`backend/ai/persistence.py:167-171`, 8,000-character cap).
- `backend/ai/persistence.py:359-366` writes `arguments` and
  `result_message[:2000]` into `ai_tool_history`; `dispatcher.py:989`/`:1266`/
  `:1303`/`:1736` expose `tool_results` through `EngineResult.metadata`, and
  `backend/ai/prompt/builder.py:355` renders tool history into the prompt.
- `backend/services/history_ai_service.py:646-686` calls the provider with
  `tools=[]` and no `response_format`; strict JSON therefore needs an explicit
  new contract, not an assumption about the current call.

Consequences for the design: the evidence envelope (source ids, excerpts,
hashes, links) must never be placed in `ToolResult.data`, `message`,
`arguments`, metadata or conversation history. An evidence tool returns only an
opaque `run_id` plus bounded counts/coverage; the owner-facing rendered summary
is produced by dedicated code from the durable run. Links are constructed
locally from a validated record (`https://t.me/c/<internal_id>/<message_id>` or
the app URI) and are never taken from provider output.

### Revised G4 stage plan

| Stage | Scope | Gate |
|---|---|---|
| **Stage 1 = G4-A** | Evidence storage foundation; durable Active Runs with TTL; owner-approved Archive foundation; Cited Summary (opt-in); basic Explorer with locally built links. No extraction, glossary UI, follow-up tools or assessments. | Owner approves the storage model, TTL, quotas and excerpt cap. |
| **Stage 2 = G4-B** | Restart-safe follow-ups over the same run/contract; no second retrieval engine, no second evidence format. | Stage 1 accepted and the TTL proves usable in practice. |
| **Stage 3 = G4-C** | Strict-JSON extraction and the persistent Workspace Glossary (owner CRUD, bounded). A tagged-text fallback may be specified separately, never as a silent downgrade. | Extraction schema approved; glossary limits approved. |
| **Stage 4 = G4-D** | Descriptive, then threshold-gated subjective assessments. | Explicit thresholds and person-level policy approved; no invented numbers. |

Scheduled History stays outside all four stages: it remains body-only and
citation-free, and no evidence run, citation or archive path may be attached to
it.

### Remaining owner decisions (Stage 1 is blocked on these)

1. Active-run TTL duration (7 days recommended; alternatives 24 h, 72 h, 30 d).
2. Maximum excerpt size, refs per run, text per run and active runs per owner.
3. Archive quota (records and bytes) and the honest at-limit behavior.
4. Whether the archive stores one row per message (proposed) or per run.
5. Glossary scope until a Workspace model exists, plus entry/character limits.
6. Whether Cited Summary is panel-only, AI-trigger-only, or both.
7. Whether the system may suggest candidates from any run, or only from
   owner-flagged evidence.
8. Subjective-assessment thresholds and the person-level judgment policy.
9. When (or whether) a tagged-text fallback is specified.
10. Whether an expired run leaves any tombstone or is deleted outright
    (outright deletion proposed).

## Previous delivery: G1-A — Reusable interaction grammar

Status: **Implemented offline for Stash and Taskloom. This is a UI interaction
delivery only; G1-B, G2-A, G5-A beyond this narrow Taskloom work, Workspace and
Scheduled History Phase 6/7 remain deferred.** No schema, migration, Supabase,
Telegram, Render or live-operation change was made.

### What G1-A adds

- **Panel ordering rule:** documented in `AGENTS.md` — primary action, grouped
  controls, item-specific controls, contextual bulk actions, then navigation
  and pagination. The existing `InlinePanelBuilder`, registry, callback
  namespaces, symbols/styles and Appearance boundary are unchanged.
- **Reusable selection:** `backend/helper/selection_state.py` now keys sessions
  by owner + feature scope, preserving the Stash default API, 40-item cap,
  900-second expiry, exact identity and visible mark behavior. Stash remains on
  its existing scope; Taskloom gets its own scope.
- **Taskloom multi-select:** a select mode over the existing task list, exact
  `task_id:version` row callbacks, pagination survival, explicit clear, and
  contextual Pause / Resume / Complete / Delete actions only for applicable
  selected tasks.
- **CAS-revalidated bulk work:** every selected task is re-read through
  `TaskManagementService`; missing, foreign-owner, changed-version and
  status-inapplicable targets are refused individually and reported per item.
- **Destructive confirmation:** Taskloom bulk Delete has a dedicated review
  showing the exact count/targets/consequence, a cancel action, DANGER styling
  on the final confirm, and revalidation at confirmation time. Stash's bulk
  delete confirm uses the same DANGER style.
- **No new scheduler, executor, task repository, selection store, renderer,
  Telegram client, listener or persistence path.**

### Validation and limits

- Focused offline tests cover Stash compatibility, Taskloom selection, scope
  isolation, cap/expiry, pagination, stale/missing/foreign targets, partial
  bulk results, confirmation cancel and destructive confirm styling.
- No live Telegram callback/render, Supabase, Render or live task execution was
  tested.
- G1-B visual identity/accessibility, G2-A language, G5-A Task Center beyond
  selection/bulk, Workspace features and Scheduled History Phase 6/7 are not
  part of this delivery.

## Previous delivery: Scheduled History diagnostics tracing — Phase 7 prerequisite

Status: **An observability-only, occurrence-scoped diagnostic trace layer is
implemented for the existing Scheduled History lifecycle. It changes no
execution, scheduler, cursor, CAS, retrieval, provider, creation or
compensation semantics. Phase 4's owner wizard/task+state creation and the
prerequisite registered Phase 5 action remain implemented and focused-tested.
The Phase 3B migration remains APPLIED to the connected Supabase project
(remote version `20261114180758`). Live Telegram execution, restart/catch-up
validation and provider-failure validation remain unverified; Phase 6/7 stay
deferred.**
The owner chose to move the registered action and trusted task-id plumbing ahead
of Phase 4 as a prerequisite; no phase was renumbered. Phase 3B audited commit:
`a12c20779bc634cb4511fb0e79443b882e8a1a85` (implementation `417a6b8`, upstream
parent `e2f1619`). Phase 3A base: `62a18de`.

### Phase map

| Phase | Scope | Status |
|---|---|---|
| **3A** | Durable History state investigation and design | **Complete** |
| **3B** | Durable schema/repository, oldest-first retrieval, identifier-free payload boundary | **Implemented and audited** — migration applied (remote `20261114180758`) |
| **4** | Deterministic schedule UI | **Implemented** — owner wizard, trusted creation path and compensating deletion |
| **5** | Scheduled History action | **Prerequisite implemented** — registered action, trusted task id, bounded catch-up, CAS commit; live validation deferred |
| **6** | Shared History admission | Deferred; numbering unchanged |
| **7** | Live validation | Deferred; numbering unchanged — occurrence tracing prerequisite delivered |

Phase 3 remains the umbrella durable-state phase. The owner changed only the
delivery order: the Phase 5 action/trusted task-id plumbing was required before
Phase 4 creation, then Phase 4 completed. No phase was renumbered.

### What this delivery adds

- **Registered action:** `scheduled_history` is one `ToolExecutor` action in the
  existing registry. It uses the coordinator-injected trusted task id from
  `ToolContext.extra`; peer, baseline, cursors and prior summary come only from
  the durable `history_schedules` row. It accepts no peer/task/message id from
  arguments and never creates or mutates a schedule.
- **Owner-only creation gate:** `TaskCreationService.create()` rejects
  `scheduled_history` unless the trusted owner-wizard action set is explicitly
  passed. The model/natural-language candidate path cannot create the action.
- **Phase 4 creation:** the History panel opens a deterministic wizard for the
  selected, re-classified peer. The owner chooses hourly/daily/weekly cadence,
  new-only vs explicit backlog baseline, and optional focus. Creation uses the
  existing `TaskCandidate → TaskCreationService → TaskRepository` path, writes
  exactly one task and one peer state row, refuses a live duplicate, refuses
  before any Telegram work when the manager reports durable storage unavailable,
  and compensates by deleting the exact created task when state creation fails.
- **Bounded execution:** one stable upper boundary per run, oldest-first batches
  of at most the peer policy cap, cumulative summary through the existing
  identifier-free History AI boundary, and one CAS commit for summary plus both
  cursors. Media/AI-provenance-only batches may advance the scanned cursor but
  never replace the summary; failures record bounded metadata without moving the
  summary or cursors.
- **No new scheduler, executor, repository, Telegram client, listener, update
  loop or persistence path.** The existing scheduler/coordinator/executor and
  durable-only repository remain the only execution and state paths.

### Scheduled History diagnostics tracing (Phase 7 prerequisite)

- `AI_TASK_TRACE` remains the existing `create_task` request trace and is not
  rebound for scheduled work. `backend/ai/task_trace.py` adds independent
  occurrence and History-creation context variables, scopes and bounded
  structured emitters.
- `SCHEDULED_OCCURRENCE_TRACE` carries one stable `trace_id` per durable
  occurrence (the Scheduler occurrence key, `<task_id>:<scheduled_for UTC ISO>`),
  the task id and a separate `attempt`; `HISTORY_SCHEDULE_TRACE` covers owner
  creation and compensation with a short-lived random creation id.
- Covered stages: owner creation, duplicate/durability checks, separate task
  and state writes, compensation; scheduler recovery, due discovery,
  occurrence creation/idempotent existence, claim, handoff, retry/terminal
  result and notification; coordinator validation, provenance injection,
  action-chain planning, each action start/result, timeout/cancellation and
  terminal outcome; tool provenance/argument validation and service invocation;
  durable state lookup/validation, peer policy, stable upper bound, retrieval,
  safe skip, AI/provider, output validation, CAS commit and failure metadata;
  narrow repository read/CAS outcomes and occurrence-visible memory-fallback
  outcomes.
- Trace output carries only bounded operational metadata. It never logs message
  bodies, summaries, owner instructions, prompts, provider bodies, credentials
  or peer identifiers; raw exception text is not used. Provider metadata values
  are allowlisted, with unknown origins/reasons normalized to `unknown`.
  Ordinary Telegram/AI requests emit no scheduled trace because the occurrence
  scope is absent.
- Twenty focused test functions (22 parameterized cases) cover ordered
  success, cross-stage correlation, retry attempt separation, concurrent
  isolation, reset after success/exception/cancellation, ordinary-request
  silence, provenance rejection, storage/state, peer-policy, retrieval,
  AI/provider, safe-skip/summary, output, CAS/durable-error, compensation,
  repository memory-fallback success/failure, provider metadata allowlisting
  and content-privacy distinctions.
- No schema, migration, scheduler, worker, execution path, cursor/CAS rule,
  provider policy or retrieval limit was changed.

### What Phase 3B actually delivers (source-verified)

- One additive migration, `supabase/migrations/20261114180758_create_history_schedules.sql`
  (part 18 of 18; inventory 42 files), applied to the connected project with
  remote version `20261114180758`:
  `task_id → ai_tasks(id) ON DELETE CASCADE`, `UNIQUE (owner_id, peer_id)`,
  immutable `baseline_cursor`, monotonic `scanned_cursor`/`summarized_cursor`
  with `baseline <= summarized <= scanned`, a 32 KiB UTF-8-bounded `summary`,
  `revision` + CAS `version`, bounded error metadata, status CHECK, one task-id
  index, RLS enabled with zero policies and `SELECT/INSERT/UPDATE/DELETE` granted
  only to `service_role`.
- Six repository primitives on the existing `TaskRepository` hierarchy:
  `create_history_schedule`, `get_history_schedule`,
  `get_history_schedule_by_peer`, `commit_history_success`,
  `record_history_failure`, `reset_history_summary`.
- **Durable-only production semantics.** Every History read/write is a single
  owner-scoped (and, for writes, CAS-versioned) durable operation. When the store
  is unavailable, times out, or the table is absent, the operation raises
  `HistoryStorageUnavailable` and writes nothing. Process memory is never
  substituted; `InMemoryTaskRepository` is an explicitly injected test fake only.
- Bounded oldest-first retrieval: optional `reverse` on
  `messages.iter_messages`/`TelegramAPI.iter_messages` (default unchanged), plus
  `fetch_oldest_history`/`HistoryBatch` with a stable upper bound, exclusive
  lower cursor, defensive sort/dedup and conservative `has_more`.
- `scheduled_summary_payload` — the identifier-free AI boundary (message bodies
  plus the owner's instruction only). No production caller in Phase 3B.
- No scheduler, executor, lifecycle hook, UI, parser, notification, catch-up
  policy, second Telegram client or second update loop.

### Independent audit corrections (this delivery)

1. **Durable-only violation fixed.** The six Supabase History methods caught
   every exception and returned memory-fallback records, so an outage or an
   unapplied migration could report a successful checkpoint that no restart
   could see. They now fail closed (`HistoryStorageUnavailable`); creation also
   refuses when the owning task could only be read from memory. Six regression
   tests cover reads, writes, CAS writes, creation and the structural guard that
   no History method references the fallback.
2. **Merge residue removed.** The merge left three files from the pre-upstream
   Appearance implementation (`backend/services/image_probe.py`,
   `backend/services/preview_storage.py`, `sql/panel_settings_appearance.sql`);
   upstream never had them and nothing references them. They were deleted.
3. **Documentation reconciled.** `ROADMAP.md` (this file), `DATABASE_ARCHITECTURE.md`
   (§16.1, §22, §30 inventory row) and `IMPLEMENTATION_REPORT.md` no longer claim
   an in-memory History fallback. `INVESTIGATION.md` records the audit findings.
4. **Scheduler failure classified.** `test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
   fails identically at `a12c207`, `417a6b8` and `e2f1619`; the scheduler code
   and test are untouched by Phase 3B — demonstrably pre-existing.

### Completed

- Phase 3A source-verified design and approved ownership/lifecycle contract.
- Phase 3B migration, repository, retrieval, payload boundary and focused tests.
- Phase 5 prerequisite: trusted task-id injection, registered
  `scheduled_history` action, bounded incremental checkpoint service, stable
  upper boundary and CAS commit.
- Phase 4: owner-only schedule wizard, deterministic cadences/baseline, task
  and state creation through the existing service, duplicate refusal and
  compensating deletion.
- Focused tests cover owner gating, creation/compensation, cursor/CAS behavior,
  safe-skip, provider failure metadata, trusted task-id injection and the UI
  creation path.
- Independent audit of Phase 3B: repository/CAS/cursor review, durable-only
  enforcement, merge-integrity comparison against both parents,
  migration/§31.3 reconciliation.
- Phase 7 observability prerequisite: occurrence-scoped end-to-end tracing for
  creation/compensation, scheduler/occurrence lifecycle, coordinator actions,
  the History tool/service, AI/provider boundary and repository CAS outcomes;
  22 focused trace cases pass, including correlation, retry, concurrency,
  cleanup, failure distinctions and content privacy.
- `IMPLEMENTATION_REPORT.md` and `INVESTIGATION.md` record the audited state.

### Applied migration and verification (2026-11-14)

- The migration is **applied** to the connected Supabase project. Its live
  migration-history record is version `20261114180758`, name
  `create_history_schedules`; the repository filename was reconciled from
  `20261113000001` to match that recorded version. The SQL is byte-identical.
- Owner-performed live checks passed for the table structure, constraints, RLS
  configuration, grants and sequence privileges.
- A transactional CAS probe ran against the live table and was **rolled back**;
  it left no test row behind.
- No coding agent executed SQL, applied a migration, repaired the migration
  history or modified any Supabase object.

### Not verified / blockers

- **Live scheduled execution is not verified**: no real scheduled occurrence has
  run against a live Telegram chat; the action and UI are covered by focused
  tests with injected retrieval/provider doubles.
- **No live restart/catch-up run, deleted-message gap run, provider-failure run
  or capped-peer refusal was exercised.** These remain Phase 7 work. The new
  tracing was verified only with offline tests and test doubles; its first
  production sequence is still to be observed.
- **Task and state are two durable writes**: state failure is compensated by
  deleting the exact created task, but this is not a single cross-table
  transaction. A crash between the two writes can leave an owner-created task
  without its state; the action then fails closed and reports the missing state.
- The pre-existing scheduler recovery test failure recorded below remains
  unrelated and untouched.

### Deferred evidence-backed constraints (unchanged)

- 1,000-message single-request summaries remain infeasible at 240–300 s with
  concurrency 2 and 4,000-token chunks (~352 Latin / ~176 non-Latin messages).
- No verified chain-wide context floor, so the chunk budget must not grow.
- `ASSUMED_CALL_LATENCY_S = 30` is a planning value; measure
  `ProviderManager.metrics_snapshot` before changing pacing or concurrency.
- `ai_task_occurrences.result_metadata` stays out of summary storage (8 KiB
  bound, occurrence lifecycle ownership).

---

## Appearance (merged from the upstream line, preserved)

The upstream Appearance delivery in the merge is preserved byte-for-byte:
`backend/bot/handlers/appearance.py`, `backend/services/appearance_service.py`
and its three suites (`test_appearance_delivery.py`,
`test_appearance_gallery.py`, `test_appearance_image_cache.py`). Its
`tests/test_appearance.py` was removed by the upstream line itself and replaced
by those suites; the audit confirmed no Appearance coverage was lost.

Implemented: one Appearance category, shared header overrides, per-main-page
title/body overrides, and a preview image with an independent switch, delivered
as the panel's own photo with a versioned `/appearance/image/<version>` rendition,
backed by a durable Storage object key and a ten-image bounded gallery.

Still required (Stage 2): live Telegram verification of small-preview rendering,
tap-through and keyboard preservation on current clients; optional strict-write
hardening for the remaining panel settings.

Explicitly out of scope: a generated default image, web-dashboard upload,
multiple selected images, per-page preview images, scheduled rotation, or any
second renderer/settings table/listener/client/update loop.

---

## Later phases (deferred, in order)

1. **Phase 6 — shared History admission**: one process-wide in-flight/start gate
   inside `history_ai_service`, because per-operation pacing does not coordinate
   bounded concurrent occurrences.
2. **Phase 7 — live validation** of a real schedule, a restart mid-run, a deletion
   of a previously processed source message, a provider failure, and a capped-peer
   refusal.

Completed sequencing note: Phase 5's action and trusted task-id plumbing were
moved ahead as prerequisites, then Phase 4's owner wizard and task/state creation
were completed. No phase was renumbered.

---

## Previous delivery (complete, unchanged): History Peer Policy Phase 2

Base `ffe72fd`: deterministic `history_policy` over the Phase 1 `PeerType`,
per-peer caps (bot/channel 300, user/group/supergroup 1000), dynamic panel count
choices, stale-callback rejection, service-boundary and linked-message enforcement.
78 new policy tests. See the commit history and the report history.

---

## Previous delivery (complete, unchanged): Peer Classification Phase 1

Base `ecc3779`: `PeerType` + pure `classify_entity` in
`backend/telegram_api/peers.py`, migrated into the History picker label,
General → Chat & Message IDs, Ghost Seen eligibility and Anti-Edit bot detection.
Strict entity-flag precedence, fail-closed `unknown`, no persistence or cache.
41 tests.

---

## Previous delivery (complete, unchanged): History AI-pass capacity

Base `7894f31`: script-aware token estimation, concurrency-aware map capacity,
map + reduce call accounting, and an honest refusal naming the AI time budget and
the affordable message count. Chunk budget, concurrency, pacing, deadlines and
ProviderManager ownership were unchanged.

---

## Open items carried over (untouched by this delivery)

- `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
  (“interval advancement exceeded bound”) — the only full-suite failure at
  `a12c207`, reproduced identically at both parents; not caused by Phase 3B.
- Live scheduled History execution and live Telegram behavior remain unverified
  (the migration itself is applied).
