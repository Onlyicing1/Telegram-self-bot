# LIFEOS IDEAS — Product Inventory and Implementation Planning

> **Status:** planning only. This document is not an execution roadmap and does
> not authorize implementation. `ROADMAP.md` remains the authoritative record
> of active phases and verified delivery.
>
> **Source baseline:** `main` at
> `f61e12ea0bd87fda874ef072e5bf15f42c34e898` (`origin/main` verified equal).
> The working tree was clean before this document was created.
>
> **Investigated:** 2026-11-14, from the repository source, tests and existing
> architecture documents. No SQL was executed, no Supabase object was touched,
> no Telegram operation was performed, and no application deployment occurred.

## 1. Purpose and Scope

This document preserves the owner's product ideas for LifeOS, records what
already exists, and proposes dependency-aware ways to design and sequence the
remaining work. It is deliberately separate from `ROADMAP.md`:

- `ROADMAP.md` answers: what phase is active, what was actually delivered, what
  was verified, and what is blocked.
- `LIFEOS_IDEAS.md` answers: what could LifeOS become, which capabilities belong
  together, what depends on what, and which ideas need owner decisions or more
  investigation.

An idea being present here is **not** approval, a promise, or a schedule. A
feature is only "implemented" when the source and tests prove it. Status terms
used below:

| Status | Meaning |
|---|---|
| **Existing** | Implemented in the current source; reusable now. |
| **Partial** | A related capability exists, but the requested product behavior does not yet exist end to end. |
| **Proposed** | Owner-provided or assistant-discovered product idea, not implemented. |
| **Needs research** | A plausible direction whose feasibility, data model or Telegram/provider behavior is not established. |
| **Deferred** | Valuable but blocked by prerequisites or an unresolved decision. |

Origins are either **Owner-provided** (from the task brief) or
**Assistant-discovered** (a suggestion from this investigation). Assistant
suggestions are not owner decisions.

### Scope rules

- Keep one runtime authority: `RuntimeSupervisor`.
- Keep AI orchestration in the existing Dispatcher and provider selection in
  the existing `ProviderManager`.
- Keep AI capabilities registered through `ToolRegistry` and executed only by
  `ToolExecutor`; services own business logic.
- Keep Telegram access behind the existing self-client/`telegram_api` boundary.
- Do not add a second scheduler, executor, Telegram client, listener, update
  loop or summarization engine.
- AI proposes; deterministic application code validates, authorizes and
  executes. No unrestricted AI access to Telegram RPC, SQL, shell, filesystem
  or arbitrary HTTP is a valid implementation approach.
- Database changes remain manual owner decisions. This document proposes no SQL
  and executes none.

## 2. Product Vision

LifeOS can evolve from a set of Telegram utilities into a **personal,
evidence-grounded information workspace**:

1. **Capture** what matters from the owner's own Telegram account.
2. **Understand** selected sources through bounded summaries, extraction and
   follow-up questions.
3. **Ground** every useful conclusion in identifiable source messages and an
   honest statement of coverage.
4. **Organize** sources, saved material and recurring work into durable,
   owner-controlled Workspaces and Tasks.
5. **Compare** multiple selected sources without losing attribution.
6. **Keep the owner in control** of schedules, permissions, automation and
   destructive actions.

The current repository already provides meaningful foundations: a Glass UI,
Deep Save and Stash, bounded History summarization, durable Scheduled History
state, Taskloom task management, provider fallback, tool execution boundaries,
saved-item tags/search, peer classification and a durable Supabase layer with
an in-memory fallback. It does **not** yet provide a unified Workspace model,
durable evidence/citations, structured extraction, cross-source analysis,
activity analytics, semantic/vector search, a knowledge graph or a complete
Task Center.

The vision must not be described as current capability. The value of this
document is the gap between the two, made explicit.

## 3. Existing Capabilities and Reuse

### 3.1 Runtime and authority boundaries

| Existing implementation | Reuse | Still missing |
|---|---|---|
| `backend/runtime/supervisor.py` owns startup, reconnect, helper bot, web server, scheduler and shutdown. | Every future feature must attach through this lifecycle; no second supervisor or update loop. | Nothing for this document to change. |
| `backend/ai/engine/dispatcher.py` owns AI orchestration; `ProviderManager` owns provider selection/fallback/retry; `ToolRegistry`/`ToolExecutor` are the only tool execution authority. | Reuse for any future AI-assisted extraction or follow-up, with deterministic validation. | No generic evidence/extraction contract yet. |
| `backend/telegram_api/` is the typed Telegram boundary; `backend/services/` owns business logic. | Reuse the existing self client, peer classification and history retrieval. | No cross-source retrieval service. |
| Supabase-or-in-memory patterns in `backend/db/client.py` and `backend/ai/database/`. | Reuse repositories and honest fallback semantics. | Scheduled History deliberately uses durable-only storage; no Workspace/evidence schema. |

**Important source finding:** the History checkpoint repository is
durable-only. A missing/unavailable Supabase store raises
`HistoryStorageUnavailable`; memory is a test fake, never a production
substitute. Any future Workspace state must decide explicitly whether it is
durable-only or fallback-tolerant; that decision must not silently copy the
History behavior.

### 3.2 Glass UI and interaction patterns

| Existing implementation | Reuse | Still missing |
|---|---|---|
| `backend/helper/panels.py`, `panel_registry.py`, `inline_sender.py`, `input_state.py`, `panel_edit.py`, `callback_gate.py`, `styles.py`, `symbols.py`. | Registration, inline builders, navigation, confirmation panels, callback admission, one-writer paint, shared progress. | A coherent cross-panel layout and ordering standard; selection exists in only one feature. |
| Main menu in `backend/bot/handlers/misc.py::_build_menu_buttons`: Save, Delete, Profile, Database, General, Settings, Cipher, Todo, AI, History & Intelligence, Appearance. | The new UI work must start from this actual 11-entry menu, not an imagined tree. | The owner considers grouping/placement inconsistent; no shared UI specification exists. |
| **Stash multi-select** in `backend/bot/handlers/retrieve.py` + `backend/helper/selection_state.py`: exact save-code toggles, visible check mark, per-owner selection, 40-item cap, 900-second expiry, page state, snapshot-backed labels. | The proven interaction to extend: click row to select, click again to deselect, visible mark, bulk actions only when relevant. | Selection is not reusable for chats, sources or tasks; edit/delete controls are not standardized on the right of a row. |
| `backend/services/appearance_service.py` composes Appearance overrides only at the top-level page render boundary. | Future visual changes should preserve the top-level page contract and Appearance behavior. | No visual design token/component layer beyond fonts, styles and symbols. |
| `backend/helper/symbols.py` owns every functional glyph; `styles.py` reserves green/red for confirmation/cancellation. | Reuse instead of hard-coding emoji or glyphs. | The owner's requested anime-inspired visual identity is not represented by the current text-first panels. |

The Stash model is the only existing multi-select implementation. It is
**not** a generic selection service, and it does not select chats or task
sources. Extending it must be a deliberate generalization with tests, not a
parallel selection system.

### 3.3 Manual History and summarization

| Existing implementation | Reuse | Still missing |
|---|---|---|
| `backend/bot/handlers/history.py` (`panel:history`) — bounded chat picker, counts, focus, manual run, link run, paginated summary delivery. | The panel/action/input registration and bounded background-operation pattern. | No language selector, no workspace context, no follow-up questions, no structured extraction, no durable evidence references for manual summaries. |
| `backend/services/history_service.py` — bounded retrieval (`MAX_HISTORY_MESSAGES = 1000`, `MAX_HISTORY_SCAN_MESSAGES = 5000`), provenance filtering, exclusive `before_id`/`after_id`, peer classification. | The only retrieval layer for single-chat History. | No multi-source retrieval or activity aggregation. |
| `backend/services/history_ai_service.py` — one map-reduce summarization/translation engine, script-aware capacity, stable error codes, `MAX_INSTRUCTION_CHARS = 500`, `with_identifiers` mode. | The only summarization engine. | `summarize_history` has no explicit language parameter and defaults to English; no citation/evidence payload. `translate_history` accepts a language. |
| `backend/services/history_policy.py` and `backend/telegram_api/peers.py` — per-peer caps (bot/channel 300, user/group/supergroup 1000), count choices and reusable `PeerType`. | Reuse for any new History surface. | Policy is History-specific; Workspace analytics and extraction need their own explicit bounds. |
| `save_service.parse_link_target` + `_fetch_linked_message` — one Telegram link parser/resolver with distinct failures. | Reuse for source resolution; do not write another parser. | It refuses chat-only links and has no invite-link parser; Smart Source Search does not expose it. |

Manual summaries are not persisted as a first-class artifact. The AI
conversation/session and tool history may contain text, but the History service
does not own a durable "summary document" with citations.

### 3.4 Scheduled History (already beyond P1, but not fully live-verified)

The repository now contains durable Scheduled History work:

- `supabase/migrations/20261114180758_create_history_schedules.sql` creates
  `history_schedules` with one `(owner_id, peer_id)` row, baseline/scanned/
  summarized cursors, a 32 KiB cumulative summary, revision/version CAS,
  bounded error metadata and service-role-only access.
- `backend/services/history_schedule_service.py` performs bounded oldest-first
  batches, peer policy, stable upper boundary, cumulative summary and CAS commit.
- `backend/ai/tools/history_schedule.py` is a registered `scheduled_history`
  action requiring coordinator-injected task and occurrence provenance; the
  model cannot create or mutate the schedule.
- `backend/bot/handlers/history.py` has a deterministic owner-only schedule
  wizard: hourly/daily/weekly, new-only vs backlog baseline, optional focus,
  and creation through `TaskCreationService` with compensation on state failure.
- The migration is **applied** to the connected Supabase project (remote version
  `20261114180758`), and owner-performed live structure/RLS/grant checks passed.

**Do not treat this as fully validated.** `ROADMAP.md` records Phase 6 (shared
History admission) and Phase 7 (live validation: real schedule, restart,
deleted-message gap, provider failure, capped-peer refusal) as deferred.
Offline tests, including the new tracing suites, are not live verification.

### 3.5 Tasks, Taskloom and scheduling

| Existing implementation | Reuse | Still missing |
|---|---|---|
| `backend/ai/task_scheduler.py` — one scheduler, CAS occurrences, bounded retries, recovery, `waiting_answer`; `RuntimeSupervisor` owns it. | The only scheduling authority. | No generic user-facing run/cancel/retry console. |
| `backend/ai/task_execution.py` — coordinator executing action chains through `ToolExecutor`; provenance and deterministic validation. | Reuse for future scheduled extraction/analysis actions. | No extraction action type or cross-source action. |
| `backend/bot/handlers/taskloom.py` — Taskloom list and detail panels under AI; new/edit wizard, pause/resume/complete/delete, recent occurrences. | The base of a future Task Center; extend it rather than build a parallel task UI. | No unified running/scheduled/completed/failed view, no occurrence output inspection, no retry/cancel control for a running occurrence, no per-task language field separate from the instruction. |
| `backend/ai/task_management.py` — list/inspect/CAS transitions, todo steps, deletion, `fail`/`expire` service methods. | Single task-management boundary. | Not all service actions are exposed in Glass UI; no task artifact/history browser. |
| `backend/ai/task_wizard.py` — task language choices English/Persian/Arabic/Chinese, mapped into the deterministic preparation policy. | Reuse the language vocabulary and policy approach. | Language is a task-creation choice encoded in instruction/policy, not a first-class preference inherited from a Workspace. |

The `.task` text handler in `backend/bot/handlers/tasks.py` also exists in
source. Documentation drift was observed: `AGENTS.md`/`README.md` describe the
only text command as `Menu`/`.menu`, while the source also registers the
`.task` command family. This document is not the place to fix that drift; it is
recorded as a repository finding.

### 3.6 Language settings (partial and inconsistent)

- `backend/services/settings_service.py` stores `panel_settings.language`
  (default `"en"`, validator only "non-empty string"). The General/Settings UI
  displays and sets it through an input.
- No source caller was found that feeds `settings_service.language()` into
  `AIRequest`, the History panel, or History summarization. The settings value
  is therefore not a reliable output-language control today.
- `AIRequest.language` defaults to `"English"`; `backend/bot/handlers/ai_unified.py`
  does not pass a language when constructing `AIRequest`.
- `ai_preferences.language` is loaded by the dispatcher into the prompt's
  `[Preferences]` block, but no owner-facing preferences UI was found in the
  bot handlers.
- Taskloom supports per-task English/Persian/Arabic/Chinese choices, mapped
  through `task_wizard.py` and `preparation_policy.py`.
- `history_ai_service.translate_history` accepts a language;
  `history_ai_service.summarize_history` does not. Its prompt defaults to
  "use English" when no language is supplied, with the owner instruction as the
  only override.

Conclusion: the repository has several language fragments, not a unified
language system. Global, Workspace and Task scopes do not exist.

### 3.7 Source resolution and peer identity

- `save_service.parse_link_target` supports public username message links,
  private `/c/` message links and `tg://openmessage` message URIs. It refuses a
  link without a message id.
- `_fetch_linked_message` resolves through the authenticated account, a bounded
  500-dialog scan and peer-less lookup as a last resort; it validates the
  requested message id and peer and classifies `peer_unresolved`,
  `message_missing`, `lookup_failed`.
- `backend/telegram_api/peers.py` classifies resolved entities into
  `PeerType` values; it never guesses from names or ids.
- `history.py` uses a bounded `get_dialogs(limit=100)` picker, not a search
  service.

Missing: invite-link resolution, chat-only links, a deterministic "resolve this
username/id/link to a source" service exposed to the UI, and a source registry
that records owner-approved sources independently of a schedule.

### 3.8 Search, saved items and organization

| Existing implementation | Reuse | Still missing |
|---|---|---|
| `backend/services/discover_service.py` + `retrieve_service.py`: saved-item list/find, deterministic name/tag/code resolution, rename/tags, preview/send/delete. | Reuse for Saved Items organization and a future research notebook. | Search is over saved-item metadata, not chat history; no semantic/vector retrieval. |
| `backend/ai/tools/semantic.py`: bounded `list_recent_messages` and `delete_messages_by_ids`; semantic delete is deterministic ID validation, not embeddings. | Reuse bounded message listing and ID-based evidence references. | No semantic search or similar-discussion retrieval. |
| `backend/ai/tools/websearch.py` + the `you` provider: external web search capability. | Reuse when research needs current external facts. | No local corpus search or cross-source Telegram query. |
| Three-tier memory (`backend/ai/memory/`) with long/permanent repositories. | Reuse for owner facts/preferences, not as an evidence store. | No project/source knowledge base or citation graph. |

### 3.9 Persistence and analytics

- The database has `saved_items`, `ai_tasks`, `ai_task_occurrences`,
  `history_schedules`, `ai_memories`, `ai_preferences`, `todo_steps`, settings
  and other existing tables. There is no Workspace table, evidence table,
  entity graph, activity-metrics table or export table.
- `backend/services/database_service.py` reports save counts/size and provider
  statistics, not chat activity.
- `backend/web/app.py` exposes read-only dashboard APIs for saves, settings,
  logs, AI stats and health. There is no data-export endpoint and no chat
  analytics endpoint.
- No implementation was found for chat activity metrics, peak hours, topic
  evolution, contradiction detection, decision/promise tracking, entity
  extraction, knowledge graphs, semantic search or cross-source synthesis.

## 4. Complete Ideas Inventory

Each idea records the user problem, expected behavior, value, reuse, dependencies,
risks, origin and evidence-based status. IDs are stable; future documents should
reference them rather than renaming ideas.

### 4.1 Glass UI

#### UI-001 — Panel information architecture and visual consistency
- **Origin:** Owner-provided.
- **Status:** Partial foundation (Glass UI exists; organization is not standardized).
- **Problem:** Buttons and categories are inconsistently grouped, ordered and
  sized; the owner cannot predict where an action lives.
- **Expected behavior:** A documented ordering rule across panels (primary
  action, related controls, navigation), fewer competing buttons, consistent
  grouping, compact spacing and a clear hierarchy. Save/Delete and other proven
  patterns remain where they are useful.
- **Value:** Faster navigation, fewer mistakes, a coherent base for every later
  feature.
- **Reuse:** Existing `InlinePanelBuilder`, `panels.py`, `styles.py`,
  `symbols.py`, `appearance_service.py` and the actual 11-entry menu.
- **Depends on:** A UI inventory and owner-approved ordering rules.
- **Risks/unknowns:** "Minimal" can hide necessary actions; a broad rewrite would
  break callback data and Appearance contracts. No new panel API is justified.
- **Do not:** Replace the panel system or invent a second renderer.

#### UI-002 — Click-to-select rows with visible state
- **Origin:** Owner-provided.
- **Status:** Partial (Stash only).
- **Problem:** Multi-item operations require opening items or remembering codes;
  there is no common selection gesture across chats, sources and tasks.
- **Expected behavior:** Clicking the item/chat row selects it; clicking again
  deselects it; selected rows show a check indicator; edit/delete controls sit
  on the right where the platform permits. Selection survives pagination within
  a bounded session.
- **Value:** Makes bulk management and Workspace setup practical.
- **Reuse:** `helper/selection_state.py` semantics (bounded, expiring, exact
  identity, per-owner), `retrieve.py` row toggles and snapshot-backed labels.
- **Depends on:** UI-001 ordering rules; a safe identity per selectable object.
- **Risks/unknowns:** Telegram inline keyboard rows cannot host arbitrary
  nested controls; "right side" may need a separate control row. A generic
  selection store must not let stale callbacks act on different rows.
- **Do not:** Build a second selection system or persist selection as data.

#### UI-003 — Contextual bulk actions
- **Origin:** Owner-provided.
- **Status:** Partial (Stash has bulk send/delete).
- **Problem:** Enabling/disabling or acting on several sources requires repeated
  navigation; bulk actions are not available consistently.
- **Expected behavior:** Bulk actions appear only when at least one relevant
  item is selected; disabled actions are absent, not decorative; selected
  chats/sources can be enabled/disabled together. Each action reports honest
  per-item outcomes.
- **Value:** Reduces repetitive work for schedules, Workspaces and saved items.
- **Reuse:** Stash `do_bulk_send`/`do_bulk_delete`, `MAX_BULK_ITEMS`, existing
  confirmation panels and per-item result rendering.
- **Depends on:** UI-002 and a feature-specific service operation for each bulk
  action.
- **Risks/unknowns:** Partial success must not look like complete success;
  action scope must be re-verified server-side.
- **Do not:** Let a selected label be the authority; act on verified ids/codes.

#### UI-004 — Destructive-action confirmation
- **Origin:** Owner-provided.
- **Status:** Existing pattern (Stash delete confirmation; other panels vary).
- **Problem:** Destructive operations are not uniformly confirmed, and a
  mis-tap can affect many rows.
- **Expected behavior:** A dedicated confirmation view with the exact scope,
  item count and consequences; cancel is always available; destructive buttons
  use the reserved red style; the confirmed action re-validates every target.
- **Value:** Prevents irreversible mistakes and makes bulk actions trustworthy.
- **Reuse:** Existing confirmation panels, `styles.DANGER`, callback admission
  and Stash result reports.
- **Depends on:** UI-002/UI-003.
- **Risks/unknowns:** Confirmation fatigue; avoid confirming harmless reads.
- **Do not:** Use a generic "Are you sure?" without the exact affected scope.

#### UI-005 — Premium visual identity and accessibility
- **Origin:** Owner-provided.
- **Status:** Proposed (current UI is text-first with a font/symbol system).
- **Problem:** The current look does not express the requested polished,
  anime-inspired LifeOS identity, and touch targets/hierarchy are inconsistent.
- **Expected behavior:** Coordinated colors, clear typography, larger touch
  targets, consistent spacing and a recognizable visual language, while
  preserving Telegram's text/keyboard limits and the functional symbol registry.
- **Value:** A more legible, intentional product experience.
- **Reuse:** `styles.py`, `symbols.py`, `font_style.py`, Appearance header/page
  overrides and the existing photo-preview path.
- **Depends on:** UI-001; a bounded visual token specification.
- **Risks/unknowns:** Telegram inline panels are not a web canvas; rich styling
  is limited. A generated image per panel would add storage/delivery cost and
  must not become a second renderer.
- **Do not:** Hard-code emoji/glyphs or add per-panel images without a measured
  delivery design.

### 4.2 Language and output

#### LANG-001 — Global, Workspace and Task language scopes
- **Origin:** Owner-provided.
- **Status:** Partial (global setting, task wizard choice and AI preferences exist
  in fragments; no Workspace scope).
- **Problem:** There is no predictable place to choose the language of generated
  output, and UI language is mixed with content language.
- **Expected behavior:** Separate settings for the Glass UI language and the
  generated-content language; content language can be global, per-Workspace and
  per-Task, with a documented precedence. UI chrome should not change merely
  because output language changes.
- **Value:** Makes Persian/English output intentional and enables multilingual
  Workspaces.
- **Reuse:** `panel_settings.language` (storage pattern), Taskloom language
  choices, `ai_preferences.language`, `preparation_policy`.
- **Depends on:** LANG-002 and the Workspace model (WS-001) for the Workspace
  scope.
- **Risks/unknowns:** Existing settings are inconsistent and partly unused; a
  new scope must not silently override an explicit owner instruction.
- **Do not:** Treat the current `panel_settings.language` value as already
  effective for AI or History.

#### LANG-002 — Explicit output-language selector and precedence
- **Origin:** Owner-provided.
- **Status:** Proposed.
- **Problem:** A summary or extraction may come back in an unintended language;
  the owner cannot select Persian/English up front.
- **Expected behavior:** A first-class output-language selector (at minimum
  Persian and English) in History, extraction and task creation. Explicit
  per-request choice wins; then Task; then Workspace; then global; then a
  documented default. The selected language is part of the run metadata.
- **Value:** Predictable, usable output for the owner's actual languages.
- **Reuse:** History `translate_history` language argument, Taskloom language
  vocabulary, `AIRequest.language` and preference rendering.
- **Depends on:** LANG-001; summary/extraction surfaces.
- **Risks/unknowns:** Provider compliance is not guaranteed; the system must
  report an honest result rather than claim the requested language was used.
- **Do not:** Infer language solely from the owner's message text.

#### LANG-003 — Auto-detect as fallback, never a replacement
- **Origin:** Owner-provided.
- **Status:** Proposed.
- **Problem:** The owner may want the source language preserved or automatically
  matched without choosing a language every time.
- **Expected behavior:** An explicit "Auto" option that detects from source or
  request only when selected; explicit Persian/English always overrides it.
  The resolved language is shown with the result.
- **Value:** Convenience without losing deliberate control.
- **Reuse:** Existing prompt/context language plumbing; provider responses.
- **Depends on:** LANG-001/002.
- **Risks/unknowns:** Detection quality, mixed-language sources, and whether
  "auto" means source language or UI language. Needs a defined contract.
- **Do not:** Let auto-detection silently override a task's language.

#### LANG-004 — Extensible language catalog
- **Origin:** Owner-provided.
- **Status:** Proposed.
- **Problem:** Adding a language currently means editing scattered choices.
- **Expected behavior:** One validated catalog of language codes, labels,
  directionality and provider hints; Persian and English are the required
  first entries; adding another language is data/config, not a new code path.
- **Value:** Makes the language system maintainable and exportable.
- **Reuse:** The Taskloom language vocabulary, `preparation_policy` language
  tokens and the prompt-budget language heuristic.
- **Depends on:** LANG-001/002.
- **Risks/unknowns:** Provider language support differs; a catalog entry is not
  a capability guarantee.
- **Do not:** Advertise a language as verified without evidence.

#### LANG-005 — Extraction terminology glossary
- **Origin:** Assistant-discovered.
- **Status:** Proposed.
- **Problem:** Names, course titles, project terms and transliterations are
  inconsistently translated or extracted.
- **Expected behavior:** An owner-maintained, bounded glossary per global or
  Workspace scope, injected only into extraction/summarization instructions,
  with the exact terms visible to the owner.
- **Value:** Better entity consistency in Persian/English research.
- **Reuse:** Prompt preference/instruction composition and validation bounds.
- **Depends on:** LANG-001; extraction (INTEL-010).
- **Risks/unknowns:** Prompt growth, glossary conflicts and privacy. Bounded,
  explicit and opt-in.
- **Do not:** Turn the glossary into an unrestricted memory store.

### 4.3 Workspace and sources

#### WS-001 — Per-source Workspace
- **Origin:** Owner-provided.
- **Status:** Needs research (no Workspace model exists).
- **Problem:** Chats, groups, channels and private conversations have settings,
  summaries, schedules and tasks scattered across separate panels.
- **Expected behavior:** One Workspace per supported source (private chat,
  group, supergroup, channel), showing the source identity, type, access state,
  message/activity summary, summary history, schedules, language, extraction
  tasks and source settings. Navigation is explicit; selecting a source opens
  its Workspace.
- **Value:** The central organizing concept for the product vision.
- **Reuse:** `PeerType`, `history_schedules` per-peer state, `ai_tasks` scoped by
  owner, `saved_items.origin_chat_id`, History picker and peer classification.
- **Depends on:** SOURCE-001/002; an owner-approved persistence decision for
  Workspace settings.
- **Risks/unknowns:** No existing Workspace table; a row per source may duplicate
  `history_schedules`/task state. The exact data model and retention are
  unresolved. A Workspace must not become a second task or history authority.
- **Do not:** Invent schema or create a parallel scheduler/state store before
  the owner approves the model.

#### WS-002 — Workspace dashboard
- **Origin:** Owner-provided.
- **Status:** Proposed.
- **Problem:** The owner cannot see a source's state and history in one place.
- **Expected behavior:** A compact dashboard for one source: message count
  (when bounded and available), activity summary, last summary/checkpoint,
  next schedule, active tasks, selected language, recent failures and links to
  the relevant panels. Every number states its time window and source.
- **Value:** Makes LifeOS feel like a workspace rather than a menu of tools.
- **Reuse:** `history_schedules`, `ai_tasks`, task occurrences, History range
  metadata, database stats patterns.
- **Depends on:** WS-001; bounded analytics (ANALYTICS-001) for activity.
- **Risks/unknowns:** "Message count" is not cheap for large chats; do not scan
  an unbounded history just to fill a dashboard.
- **Do not:** Present estimated/partial metrics as exact totals.

#### WS-003 — Workspace settings and task scope
- **Origin:** Owner-provided.
- **Status:** Proposed.
- **Problem:** Per-source language, schedule, extraction and notification
  preferences have no single home.
- **Expected behavior:** Read and edit Workspace-scoped settings with validation;
  show inheritance from global settings; Tasks created in a Workspace carry that
  scope without allowing cross-source access.
- **Value:** Reduces per-task repetition and makes multi-source behavior
  predictable.
- **Reuse:** `settings_service` validators, `TaskCreationService`,
  notification destination and task action contract.
- **Depends on:** WS-001; LANG-001; TASK-001.
- **Risks/unknowns:** Scope escalation and accidental cross-Workspace queries;
  task provenance must remain deterministic.
- **Do not:** Let a task argument override its trusted Workspace/source scope.

#### WS-004 — Source access and health registry
- **Origin:** Assistant-discovered.
- **Status:** Proposed.
- **Problem:** A source can become inaccessible, migrated or renamed, and the
  owner learns only when a task fails.
- **Expected behavior:** A read-only source health view: last successful
  resolution, current `PeerType`, access state, last error category and affected
  schedules/tasks. No automatic repair, join or permission bypass.
- **Value:** Operational transparency and faster recovery.
- **Reuse:** `classify_chat`, `_fetch_linked_message`, task/checkpoint failure
  metadata, Health panel patterns.
- **Depends on:** WS-001.
- **Risks/unknowns:** Avoid caching Telegram entities as authority; re-resolve
  on demand. Do not leak peer identifiers into provider prompts.
- **Do not:** Auto-join chats or retry access outside the existing client.

#### SOURCE-001 — Smart source resolution
- **Origin:** Owner-provided.
- **Status:** Partial (link/peer resolution exists internally; no general UI).
- **Problem:** Adding a source requires browsing a bounded dialog list; a known
  username, id or link cannot be used directly.
- **Expected behavior:** Resolve a username, numeric Telegram id, message link,
  group/channel link or supported reference through the existing authenticated
  client; validate ownership/access and show the resolved source before adding
  it. Failures are specific and never guessed.
- **Value:** Faster, less error-prone Workspace creation.
- **Reuse:** `save_service` parser/resolver, `telegram_api.peers`, existing
  `get_dialogs` fallback and `PeerType`.
- **Depends on:** WS-001 and a deterministic source-registration service.
- **Risks/unknowns:** Numeric ids may not resolve from a StringSession; links
  may be inaccessible; username changes. Chat-only links need a separate parser
  decision.
- **Do not:** Assume a syntactically valid id/link proves access.

#### SOURCE-002 — Source search and selection UI
- **Origin:** Owner-provided.
- **Status:** Partial (bounded History picker only).
- **Problem:** Large accounts cannot find a source quickly, and the picker has
  no search or add flow.
- **Expected behavior:** Search by display name/username within a bounded
  dialog set; paste a link/id to resolve; show type and access; paginate; select
  by row; clear empty/failure states.
- **Value:** Makes Workspaces usable on real accounts.
- **Reuse:** Existing dialog enumeration, `PeerType`, selection pattern and
  callback admission.
- **Depends on:** SOURCE-001; UI-002.
- **Risks/unknowns:** Search must not trigger an unbounded account walk; display
  names are ambiguous.
- **Do not:** Treat a display-name match as proof of identity.

#### SOURCE-003 — Invite-link resolution (investigation)
- **Origin:** Owner-provided.
- **Status:** Needs research / deferred.
- **Problem:** The owner may have an invite link rather than a username or id.
- **Expected behavior:** If Telegram permits it through the authenticated
  account, resolve the invite to a source and validate access; otherwise refuse
  honestly without joining.
- **Value:** Covers a common source-sharing format.
- **Reuse:** Existing self client and link parsing conventions.
- **Depends on:** A verified Telegram API path and an explicit no-auto-join
  policy.
- **Risks/unknowns:** The current parser does not support invite links; joining
  or previewing can have side effects and permission constraints. Not assumed
  feasible.
- **Do not:** Add invite redemption as part of a UI refactor.

### 4.4 Activity analytics and schedule recommendations

#### ANALYTICS-001 — Source activity metrics
- **Origin:** Owner-provided.
- **Status:** Proposed.
- **Problem:** The owner cannot see when a source is active or how activity
  changes over time.
- **Expected behavior:** Bounded metrics for messages per hour/day, active
  periods, peak hours, weekday/weekend differences and trend direction, with
  the sampling window and coverage stated. No unbounded historical sweep.
- **Value:** Better schedule choices and source understanding.
- **Reuse:** `history_service` retrieval, `telegram_api.messages`, existing
  bounded scan patterns; no new AI engine.
- **Depends on:** WS-001 and a sampling/caching design.
- **Risks/unknowns:** Telegram RPC cost, timezone boundaries, edited/deleted
  messages and partial windows. A metric must never imply full-chat precision.
- **Do not:** Add a scheduler or scrape entire histories for a dashboard.

#### ANALYTICS-002 — Recommended summary/extraction times
- **Origin:** Owner-provided.
- **Status:** Proposed.
- **Problem:** Fixed cadences may run when a source is quiet or too active.
- **Expected behavior:** Recommend candidate times from observed activity,
  explain the evidence window, and let the owner choose. Recommendations do not
  create or modify tasks.
- **Value:** Better timing and less wasted AI work.
- **Reuse:** ANALYTICS-001, Scheduled History cadence model and Taskloom.
- **Depends on:** ANALYTICS-001; Scheduled History Phase 7 live evidence.
- **Risks/unknowns:** Recommendations may be unstable or misleading; the owner
  must remain in control.
- **Do not:** Auto-apply a recommendation or silently change a schedule.

#### ANALYTICS-003 — Owner-approved schedule application
- **Origin:** Owner-provided.
- **Status:** Proposed.
- **Problem:** Even a good recommendation requires manual re-entry.
- **Expected behavior:** An explicit "Use this time" action that opens the
  existing schedule flow with the proposed values pre-filled; the owner confirms
  and the existing creation boundary executes.
- **Value:** Turns analysis into a safe, low-friction action.
- **Reuse:** History schedule wizard, `TaskCreationService`, owner-only gate.
- **Depends on:** ANALYTICS-002; SOURCE-001/WS-001.
- **Risks/unknowns:** Must not bypass the owner-only `scheduled_history` action
  or create a task from the AI/model path.
- **Do not:** Auto-create or mutate a scheduled task.

#### ANALYTICS-004 — Source activity comparison
- **Origin:** Owner-provided.
- **Status:** Proposed.
- **Problem:** The owner cannot compare which sources are active, noisy or
  worth scheduling.
- **Expected behavior:** Compare selected Workspaces on bounded, normalized
  metrics (messages/hour, active days, peak windows) with source labels and
  coverage caveats.
- **Value:** Prioritizes attention and automation.
- **Reuse:** ANALYTICS-001; Workspace selection UI.
- **Depends on:** WS-001, ANALYTICS-001.
- **Risks/unknowns:** Different source types/sizes are not directly comparable;
  avoid false precision.
- **Do not:** Rank people or sources as "important" from message volume alone.

#### ANALYTICS-005 — Coverage and data-quality report
- **Origin:** Assistant-discovered.
- **Status:** Proposed.
- **Problem:** The owner cannot tell whether a summary/extraction saw the whole
  requested window or only part of it.
- **Expected behavior:** A per-run report: requested vs retrieved vs eligible
  text messages, skipped media/AI-provenance counts, cursor range, truncation,
  provider failures and known gaps (deleted/edited messages).
- **Value:** Trust and honest expectations for every intelligence result.
- **Reuse:** Existing `HistorySlice`/`HistoryPlan` metadata, scheduled-run result
  data, tracing categories (operational, not content).
- **Depends on:** G4 evidence/result contract.
- **Risks/unknowns:** Do not turn operational trace logs into user-facing
  evidence; the report needs its own bounded data contract.
- **Do not:** Claim completeness from a successful provider call.

### 4.5 Grounded summaries and extraction

#### INTEL-001 — Evidence-grounded follow-up questions
- **Origin:** Owner-provided.
- **Status:** Proposed.
- **Problem:** A summary is a dead end; the owner cannot ask why, who, what
  happened before, or whether sources contradicted each other.
- **Expected behavior:** After a summary, the owner can ask a follow-up. The
  system retrieves relevant source messages again (bounded), answers with
  citations and clearly says when the evidence is insufficient.
- **Value:** Turns summaries into an investigation workflow.
- **Reuse:** History retrieval, existing conversation UI, provider tools, and a
  future evidence contract.
- **Depends on:** INTEL-002/003; EVID-001; LANG-001.
- **Risks/unknowns:** Context isolation, prompt injection from source text,
  cost, and ensuring the answer is grounded rather than a paraphrase of the
  previous summary.
- **Do not:** Send the whole source or unrelated chat history to the provider.

#### INTEL-002 — Evidence references and source-message linking
- **Origin:** Owner-provided.
- **Status:** Proposed (range metadata exists; citations do not).
- **Problem:** A summary cannot be verified or opened at the supporting message.
- **Expected behavior:** Each conclusion/claim can carry bounded evidence
  references (source, message id/range, short excerpt), rendered as tappable
  links where Telegram permits. Evidence is attributed to the exact source.
- **Value:** Trust, auditability and a foundation for every advanced feature.
- **Reuse:** `HistoryMessage.message_id`, `HistorySlice` first/last ids,
  `saved_items.origin_chat_id/origin_msg_id`, message-link formats.
- **Depends on:** A provider output contract that requests structured claims and
  application-side validation of every id.
- **Risks/unknowns:** Message links can expose identifiers; evidence persistence
  and retention are undecided. The provider must never be trusted to invent ids.
- **Do not:** Present model-generated citations as verified without re-checking
  the source message.

#### INTEL-003 — Bounded evidence retrieval and context isolation
- **Origin:** Owner-provided.
- **Status:** Proposed.
- **Problem:** Follow-ups and extraction need more context without re-reading an
  unbounded chat or leaking unrelated context to the provider.
- **Expected behavior:** Retrieve only the smallest relevant bounded window(s)
  around known message ids; strip ids/sender identity from prompts unless a
  deterministic feature explicitly needs them; keep each source isolated.
- **Value:** Safer, cheaper, more relevant answers.
- **Reuse:** `history_service` exclusive cursors, `MAX_HISTORY_MESSAGES`,
  identifier-free summary mode, provenance filtering.
- **Depends on:** INTEL-002; existing history service.
- **Risks/unknowns:** Retrieval around a message may miss context; relevance
  selection must not become an unbounded semantic crawl.
- **Do not:** Add a second retrieval pipeline.

#### INTEL-010 — Natural-language targeted extraction
- **Origin:** Owner-provided.
- **Status:** Proposed.
- **Problem:** The owner can ask for a summary but cannot ask LifeOS to extract
  specific information (promises, dates, amounts, professors' opinions,
  recurring bot behavior) across a chat.
- **Expected behavior:** A deterministic extraction request surface (panel and/or
  AI tool) that accepts a target, source scope and optional schema; retrieves a
  bounded window; returns structured results with evidence and coverage.
- **Value:** Converts chat history into usable research data.
- **Reuse:** History retrieval/summarization, ToolRegistry/Executor, provider
  structured output, task scheduling for recurring extraction.
- **Depends on:** INTEL-002/003, LANG-001 and a validated output schema.
- **Risks/unknowns:** Extraction quality, schema validation, cost, and false
  positives. A generic "ask the model" call is not sufficient.
- **Do not:** Let the model choose an arbitrary source or bypass peer policy.

#### INTEL-011 — Structured, evidence-backed extraction output
- **Origin:** Owner-provided.
- **Status:** Proposed.
- **Problem:** Free-text extraction cannot be filtered, compared or exported.
- **Expected behavior:** Typed records (claim, entity, date, amount, category,
  confidence, evidence refs, source, coverage) validated locally; reject or
  flag malformed/unsupported output rather than silently storing it.
- **Value:** Enables export, trackers, comparison and review.
- **Reuse:** Existing tool result contracts, bounded JSON parsing patterns,
  deterministic validators.
- **Depends on:** INTEL-010; evidence contract.
- **Risks/unknowns:** Schema design and migration-free evolution; provider
  structured-output reliability.
- **Do not:** Store arbitrary model JSON as authoritative data.

#### INTEL-012 — Rigorous subjective assessments
- **Origin:** Owner-provided.
- **Status:** Proposed.
- **Problem:** A few messages can be turned into an unsupported definitive
  judgment about a person (for example, "this professor is bad").
- **Expected behavior:** Separate directly observed statements, repeated
  patterns, evidence count, conflicting opinions, missing perspectives and
  confidence. Use cautious language; refuse a definitive judgment from
  insufficient evidence.
- **Value:** Prevents harmful overclaiming and makes people-related analysis
  defensible.
- **Reuse:** Evidence counts/refs, structured output validation, existing
  conservative preparation policy.
- **Depends on:** INTEL-011 and a defined evidence threshold.
- **Risks/unknowns:** Subjectivity, privacy and bias; thresholds need owner
  approval and testing.
- **Do not:** Let the provider assign a person a final "good/bad" label.

#### INTEL-037 — Evidence Explorer
- **Origin:** Owner-provided.
- **Status:** Proposed.
- **Problem:** Even a cited answer is hard to inspect in context.
- **Expected behavior:** Open a result's evidence list, inspect the exact source
  message/range and surrounding bounded context, and see why it was included.
  Read-only; no model call required.
- **Value:** Verification and trust.
- **Reuse:** INTEL-002 references, Telegram message links, `list_recent_messages`
  bounded window.
- **Depends on:** INTEL-002; Workspace/source identity.
- **Risks/unknowns:** Access changes, deleted messages and identifier exposure in
  links.
- **Do not:** Make the explorer a new Telegram client or archive subsystem.

#### EVID-001 — Durable evidence index
- **Origin:** Assistant-discovered.
- **Status:** Needs research.
- **Problem:** Cross-session follow-ups, trackers and exports need stable
  references; current summaries carry only a range and scheduled summaries carry
  none.
- **Expected behavior:** A bounded, owner-scoped index of evidence references
  (source, message id/range, excerpt/hash, run, created time) that can be
  revalidated against Telegram. Deletion/expiry semantics are explicit.
- **Value:** Shared foundation for citations, follow-ups, extraction, trackers
  and export.
- **Reuse:** Existing run/result metadata and message links; a repository-style
  service if approved.
- **Depends on:** Owner decision on persistence/privacy; INTEL-002.
- **Risks/unknowns:** New schema, retention, sensitive content and stale
  references. An ephemeral-first design may be sufficient for MVP.
- **Do not:** Create a second message archive or copy full chats into the
  database.

### 4.6 Cross-source and cross-chat intelligence

#### INTEL-020 — Cross-source query over selected Workspaces
- **Origin:** Owner-provided.
- **Status:** Proposed.
- **Problem:** The owner cannot ask one question across several university
  groups and see what each source said.
- **Expected behavior:** Select multiple Workspaces, ask a bounded question,
  retrieve per-source evidence, answer with source attribution and explicit
  gaps. No source silently disappears from the result.
- **Value:** The core multi-source research workflow.
- **Reuse:** Workspace model, History retrieval per peer, evidence contract,
  provider map/reduce patterns.
- **Depends on:** WS-001, INTEL-002/003, ANALYTICS-001 for bounded scope.
- **Risks/unknowns:** Cost/complexity, per-source limits, ranking bias and
  attribution loss. A single provider call may not fit multiple sources.
- **Do not:** Concatenate unrelated chats into one unlabelled prompt.

#### INTEL-022 — Cross-chat pattern intelligence
- **Origin:** Owner-provided (listed as Cross-Chat Intelligence).
- **Status:** Proposed.
- **Problem:** The owner may want recurring themes, decisions or entities that
  appear across chats, not just one query.
- **Expected behavior:** A periodic or on-demand synthesis over explicitly
  selected Workspaces, preserving source boundaries and evidence, with a clear
  difference between "appeared in N sources" and "was asserted by N people".
- **Value:** Reveals patterns invisible in one chat.
- **Reuse:** INTEL-020, task scheduler for opt-in recurring runs, evidence index.
- **Depends on:** INTEL-020; owner-approved scope and cadence.
- **Risks/unknowns:** False pattern claims, duplicate identities, privacy and
  cost. This is not an autonomous global monitor.
- **Do not:** Scan every chat or create an always-on cross-account observer.

### 4.7 Task Center

#### TASK-001 — Unified Task Center
- **Origin:** Owner-provided.
- **Status:** Partial (Taskloom under AI; `.task` commands; no single unified view).
- **Problem:** Running, scheduled, completed and failed work is spread across
  Taskloom, notifications and commands.
- **Expected behavior:** One owner-only entry point listing running/scheduled,
  completed, failed and paused work with status, scope, next run and source.
  Read paths must use `TaskManagementService`, not direct database access.
- **Value:** A single operational home for all AI-generated and scheduled work.
- **Reuse:** `Taskloom`, `TaskManagementService`, `TaskRepository`,
  task-occurrence metadata, existing status filters.
- **Depends on:** UI-001/UI-002 for a coherent list; no new task repository.
- **Risks/unknowns:** Mixing todos, scheduled tasks and extraction jobs in one
  list can confuse scopes; filters must be explicit.
- **Do not:** Add a second task store or execution authority.

#### TASK-002 — Task lifecycle controls (cancel/retry included)
- **Origin:** Owner-provided.
- **Status:** Partial (pause/resume/complete/delete exist; cancel/retry of a
  running occurrence is not exposed as a complete console).
- **Problem:** The owner cannot reliably stop or retry work from one place.
- **Expected behavior:** Pause/resume/complete/delete plus explicit cancel and
  bounded retry where the existing scheduler/coordinator contract supports it;
  every mutation is CAS-guarded, owner-scoped and honestly reported.
- **Value:** Operational control without database surgery.
- **Reuse:** `TaskManagementService` CAS transitions, scheduler retry policy,
  `ai_task_occurrences` statuses.
- **Depends on:** TASK-001; verified semantics for cancelling a claimed/running
  occurrence without duplicate side effects.
- **Risks/unknowns:** Cancellation timing and side-effect uncertainty; retry may
  duplicate external actions if the boundary is wrong. Requires investigation.
- **Do not:** Kill asyncio tasks directly or invent a second cancellation path.

#### TASK-003 — Task output and occurrence inspection
- **Origin:** Owner-provided.
- **Status:** Partial (Taskloom shows occurrence status/attempt; output/source
  scope is not fully inspectable).
- **Problem:** The owner sees that a task ran but not what it produced or which
  source it covered.
- **Expected behavior:** A bounded result view per occurrence: status, attempt,
  timestamps, safe output reference/summary, source/scope, error category and
  links to the relevant evidence (when available). Never expose raw secrets or
  provider internals.
- **Value:** Makes automation auditable.
- **Reuse:** occurrence `result_metadata`/`error_metadata`, task tracing categories,
  History result metadata and delivery text.
- **Depends on:** TASK-001; a safe output contract.
- **Risks/unknowns:** Result metadata is bounded (8 KiB for occurrences) and is
  not a summary store; large outputs need a reference, not blind copying.
- **Do not:** Dump raw provider payloads or message bodies into the task table.

#### TASK-004 — Task-specific language and configuration
- **Origin:** Owner-provided.
- **Status:** Partial (Taskloom wizard supports a language choice; no Workspace
  inheritance or unified configuration view).
- **Problem:** Recreating similar tasks requires re-entering language, source,
  output and notification settings.
- **Expected behavior:** A task shows its effective language, source/scope and
  output configuration; edits are validated through the existing task contract;
  Workspace defaults can pre-fill but an explicit task choice wins.
- **Value:** Predictable multilingual scheduling and less repetition.
- **Reuse:** `task_wizard.py`, `preparation_policy.py`, `TaskCreationService`,
  notification destination, Workspace settings (future).
- **Depends on:** LANG-001/002; WS-003; TASK-001.
- **Risks/unknowns:** Existing tasks encode language in instruction/policy rather
  than a dedicated column; a schema change would need owner approval.
- **Do not:** Add a hidden language field or reinterpret existing task text.

#### TASK-005 — Run timeline and operational transparency
- **Origin:** Assistant-discovered.
- **Status:** Proposed.
- **Problem:** When a scheduled run fails, the owner cannot see the sequence of
  validation, retrieval, provider and commit stages.
- **Expected behavior:** A user-facing, bounded timeline derived from existing
  operational statuses: queued/claimed/running/retry/waiting/completed/failed,
  with safe reasons and timestamps. No content, prompts or secrets.
- **Value:** Faster diagnosis and more trust in automation.
- **Reuse:** `task_trace.py` categories, occurrence status transitions,
  scheduler/coordinator outcomes.
- **Depends on:** TASK-001/003.
- **Risks/unknowns:** Trace events are diagnostics, not a durable user data
  model; retention and exposure must be designed.
- **Do not:** Surface raw logs or exception text to the owner.

### 4.8 Search, research and export

#### SEARCH-001 — Semantic search over chat/saved content
- **Origin:** Owner-provided.
- **Status:** Needs research (no embeddings or vector store; saved-item search is
  deterministic name/tag matching).
- **Problem:** Keyword matching misses conceptually related messages.
- **Expected behavior:** Semantic retrieval over an explicitly bounded corpus
  with source/evidence attribution, permission checks and an honest empty result.
- **Value:** Finds relevant discussions that exact terms miss.
- **Reuse:** Existing retrieval boundaries, saved-item metadata and evidence
  references.
- **Depends on:** A vector/index/storage decision, an embedding provider, and
  owner approval for content retention. No such foundation exists today.
- **Risks/unknowns:** Cost, privacy, stale indexes, false similarity and a new
  data store. Do not assume a provider offers embeddings.
- **Do not:** Upload arbitrary chat history to a provider without an explicit
  owner-approved indexing policy.

#### SEARCH-002 — Similar discussion discovery
- **Origin:** Owner-provided.
- **Status:** Proposed / needs research.
- **Problem:** The owner may want other discussions of the same subject across
  one or more sources.
- **Expected behavior:** Given a selected message/topic, find bounded similar
  discussions with source/date/evidence and explain why they matched.
- **Value:** Research serendipity with provenance.
- **Reuse:** SEARCH-001 or deterministic lexical fallback; History retrieval.
- **Depends on:** SEARCH-001; INTEL-020 for cross-source scope.
- **Risks/unknowns:** Similarity is not relevance; result quality and cost need
  evaluation.
- **Do not:** Present similarity scores as factual equivalence.

#### SEARCH-003 — Saved search and watchlist
- **Origin:** Assistant-discovered.
- **Status:** Proposed.
- **Problem:** The owner may want to be told when new saved items/messages match
  a topic, without manually repeating a search.
- **Expected behavior:** An opt-in saved query scoped to explicit sources/items,
  evaluated on a bounded schedule through the existing TaskScheduler, with
  deduplication and an off switch.
- **Value:** Controlled monitoring without an always-on observer.
- **Reuse:** Existing task scheduler and saved-item/search services.
- **Depends on:** TASK-001 and a deterministic match contract; owner approval.
- **Risks/unknowns:** Notification spam, duplicate alerts, scope creep into
  monitoring all chats.
- **Do not:** Add a second polling loop or silently enable monitoring.

#### EXPORT-001 — Research export (JSON/CSV/Markdown)
- **Origin:** Owner-provided.
- **Status:** Proposed (no export endpoint or command found).
- **Problem:** Research results are trapped in Telegram panels.
- **Expected behavior:** Export selected structured results/evidence to bounded
  JSON, CSV and Markdown with source attribution, coverage and schema version.
  The owner chooses what is included; no secrets or unrelated data.
- **Value:** Portability into notes, spreadsheets and external analysis.
- **Reuse:** Structured extraction (INTEL-011), evidence refs, saved-item
  metadata and existing Telegram file delivery.
- **Depends on:** INTEL-011, LANG-001, EXPORT-002 for delivery/storage policy.
- **Risks/unknowns:** Sensitive content, formula injection in CSV, size bounds,
  and where exported files live.
- **Do not:** Add an unauthenticated web download endpoint.

#### EXPORT-002 — Portable Workspace bundle
- **Origin:** Assistant-discovered.
- **Status:** Proposed.
- **Problem:** A Workspace's settings, summaries, evidence links and task
  definitions are difficult to move or back up as a unit.
- **Expected behavior:** A versioned manifest plus selected artifacts (Markdown
  report, JSON metadata, evidence index) packaged as a bounded archive; import
  is a separate, validated future decision.
- **Value:** Data portability and long-term ownership.
- **Reuse:** EXPORT-001, Workspace model and existing task/state metadata.
- **Depends on:** WS-001, EXPORT-001.
- **Risks/unknowns:** Import semantics, secrets, schema drift and size.
- **Do not:** Export credentials, session data or unrestricted message dumps.

### 4.9 Advanced and exploratory intelligence

#### INTEL-030 — Knowledge Graph
- **Origin:** Owner-provided.
- **Status:** Needs research / deferred.
- **Problem:** Relationships among people, topics, events and messages are not
  represented as a queryable structure.
- **Expected behavior:** An evidence-backed graph with explicit entity
  resolution, source links and confidence; the owner can inspect why an edge
  exists.
- **Value:** Enables richer research and navigation.
- **Reuse:** INTEL-036 entity extraction, INTEL-002 evidence, Workspace model.
- **Depends on:** Evidence/entity foundations and a graph storage decision.
- **Risks/unknowns:** Entity ambiguity, false relations, schema and cost. High
  risk of overclaiming.
- **Do not:** Treat model-extracted edges as facts without evidence.

#### INTEL-031 — Topic Evolution
- **Origin:** Owner-provided.
- **Status:** Proposed / needs research.
- **Problem:** A subject's treatment changes over time and a single summary
  hides that.
- **Expected behavior:** A time-bucketed, evidence-backed view of how a topic,
  stance or terminology changes, with coverage limits and source attribution.
- **Value:** Longitudinal understanding.
- **Reuse:** Evidence index, activity windows, bounded History retrieval.
- **Depends on:** WS-001, INTEL-002, ANALYTICS-001.
- **Risks/unknowns:** Bucket boundaries, missing messages and narrative bias.
- **Do not:** Infer change from an unrepresentative sample.

#### INTEL-032 — Contradiction Detection
- **Origin:** Owner-provided.
- **Status:** Proposed / needs research.
- **Problem:** Conflicting statements across messages/sources are hard to spot.
- **Expected behavior:** Surface candidate contradictions with both sides,
  evidence, source/time and confidence; explicitly distinguish contradiction
  from changed circumstances or different speakers.
- **Value:** Improves research quality and decision review.
- **Reuse:** Evidence refs, cross-source retrieval, structured output.
- **Depends on:** INTEL-020 and EVID-001.
- **Risks/unknowns:** False positives, context loss and subjectivity.
- **Do not:** Declare a contradiction without showing the evidence.

#### INTEL-033 — Decision Tracker
- **Origin:** Owner-provided.
- **Status:** Proposed.
- **Problem:** Decisions are scattered across conversations and are easy to lose.
- **Expected behavior:** Extract candidate decisions with date, participants
  (only when evidence supports), rationale, status and source refs; owner
  confirms which are real.
- **Value:** A durable decision log.
- **Reuse:** INTEL-011 extraction, evidence index, Tasks for follow-up.
- **Depends on:** INTEL-011/012; Workspace.
- **Risks/unknowns:** A proposal is not a decision; confirmation is required.
- **Do not:** Auto-commit a model-extracted decision as authoritative.

#### INTEL-034 — Promise/Commitment Tracker
- **Origin:** Owner-provided.
- **Status:** Proposed.
- **Problem:** Promises and commitments are forgotten or misattributed.
- **Expected behavior:** Candidate commitments with speaker, recipient, due
  date (if stated), evidence, status and explicit uncertainty; owner can confirm
  or dismiss.
- **Value:** Accountability and follow-up.
- **Reuse:** INTEL-011, Tasks, evidence refs, language/date extraction.
- **Depends on:** INTEL-011/012; privacy review.
- **Risks/unknowns:** Sarcasm, hypotheticals, misattribution and surveillance
  concerns.
- **Do not:** Automatically notify or judge a person from an unconfirmed
  candidate.

#### INTEL-035 — Change Detection
- **Origin:** Owner-provided.
- **Status:** Proposed / needs research.
- **Problem:** The owner may want to know when recurring behavior or opinions
  change.
- **Expected behavior:** Compare bounded, evidence-backed snapshots over time and
  report candidates with before/after evidence and confidence. Edits/deletes
  are not detectable without an archive.
- **Value:** Early awareness of meaningful shifts.
- **Reuse:** Scheduled History snapshots, evidence index, activity windows.
- **Depends on:** Durable evidence/snapshots and a baseline policy.
- **Risks/unknowns:** Noise, seasonality, missing messages and false alarms.
- **Do not:** Claim detection of edited/deleted content that was never captured.

#### INTEL-036 — Entity Extraction
- **Origin:** Owner-provided.
- **Status:** Proposed.
- **Problem:** People, organizations, places, dates and recurring terms are not
  consistently indexed.
- **Expected behavior:** Extract typed candidates with evidence and confidence;
  resolve duplicates cautiously; keep source attribution.
- **Value:** Foundation for graph, search, trackers and comparison.
- **Reuse:** INTEL-011 schema, evidence refs, existing `PeerType` for sources.
- **Depends on:** INTEL-011/002.
- **Risks/unknowns:** Ambiguous names, privacy and entity-resolution quality.
- **Do not:** Merge entities solely because a model says they match.

#### INTEL-040 — Research notebook and collections
- **Origin:** Assistant-discovered.
- **Status:** Proposed.
- **Problem:** Summaries, saved items, evidence and notes for one investigation
  have no shared home.
- **Expected behavior:** Owner-created collections that reference existing saved
  items/Workspaces/results and hold bounded notes; no duplicate copies of source
  content; exportable.
- **Value:** Turns one-off answers into ongoing research.
- **Reuse:** Saved-item tags/display names, Workspace identity, EXPORT-001.
- **Depends on:** WS-001, EXPORT-001; a persistence decision if collections must
  survive restarts.
- **Risks/unknowns:** New storage model and retention; avoid becoming a second
  Saved Items system.
- **Do not:** Copy full chats or create an unowned document store.

## 5. Feature Relationships and Dependencies

These are hypotheses validated against the current source, not mandatory design
decisions.

### 5.1 Shared foundations

| Foundation | Why it is shared | Current evidence | Ideas enabled |
|---|---|---|---|
| **Language contract** (LANG-001/002/004) | Every generated artifact needs one predictable language decision and an extensible catalog. | Global setting exists but is not wired to AI/History; Taskloom has task choices; `AIRequest` defaults English. | Summaries, extraction, task output, exports, cross-source answers. |
| **Workspace/source identity** (WS-001, SOURCE-001/002) | Settings, analytics, schedules, tasks and cross-source queries need one owner-approved source identity and scope. | `PeerType`, History picker, `history_schedules` per peer, task rows; no Workspace model. | WS-002/003, ANALYTICS, TASK scope, INTEL-020. |
| **Evidence/provenance** (INTEL-002, EVID-001) | Claims, follow-ups, extraction, trackers and exports need stable, revalidated source references. | History range metadata and saved-item origin ids exist; no citation ledger or persisted evidence. | Follow-ups, extraction, contradiction, decisions, promises, explorer, cross-source. |
| **Bounded retrieval** (existing `history_service`) | All intelligence must read through one bounded Telegram layer with provenance filtering. | `MAX_HISTORY_MESSAGES`, scan/peer caps, exclusive cursors, `HistoryError`. | Everything that reads chat content. |
| **Selection state** (existing `selection_state`) | Bulk actions need exact, expiring, per-owner selection semantics. | Stash-only, 40-item/900-second bound, snapshot-backed labels. | UI-002/003, Workspace source enable/disable, Task bulk actions. |
| **Task authority** (existing scheduler/coordinator/executor) | Long-running and recurring work must reuse one lifecycle, not a second queue. | `TaskScheduler`, `TaskExecutionCoordinator`, `ToolExecutor`, CAS occurrences. | Task Center, recurring extraction, watchlists, scheduled cross-source analysis. |
| **Activity data** (ANALYTICS-001) | Schedule recommendations and Workspace dashboards need observable source activity. | No chat activity metrics; only save/provider stats. | ANALYTICS-002/003/004, WS-002, INTEL-031. |

### 5.2 Dependency observations

1. **Workspace is not a rename of `history_schedules`.** That table owns one
   cumulative scheduled summary per peer and is intentionally durable-only. A
   Workspace would cover settings, activity, tasks and non-scheduled sources; it
   must not become a second task/checkpoint authority.
2. **Evidence is the largest missing shared foundation.** Summaries currently
   return text/range metadata; they do not return validated claim-to-message
   references. Building follow-ups or extraction without that contract would
   produce plausible but unverifiable answers.
3. **Language is fragmented.** A setting exists, but global, Workspace and task
   scopes are not wired into one precedence. Any new feature that generates
   text needs the language decision resolved first.
4. **Source resolution is implemented but not productized.** The parser/resolver
   is reusable; invite links and chat-only links are separate feasibility
   questions. Smart Search must expose resolution, not duplicate it.
5. **Task management exists but is not a complete console.** The durable
   machinery, CAS transitions and occurrence records are reusable; retry/cancel
   semantics and output inspection need investigation before UI promises.
6. **Activity analytics needs a sampling contract.** There is no safe basis for
   an exact "all messages per hour" claim without a bounded strategy; any future
   dashboard must state its window and coverage.
7. **Scheduled History is implemented but not fully live-validated.** Any
   recommendation/automation idea that depends on it inherits Phase 6/7 open
   work; it must not be presented as production-proven.

## 6. Implementation Groups

Groups are chosen for shared foundation and end-to-end user value, not merely
similar priority. An idea can appear in only one primary group.

### G1 — Glass UI Interaction Foundation

- **Goal:** Make every panel predictable, compact and safely selectable before
  adding new data-heavy surfaces.
- **Included:** UI-001, UI-002, UI-003, UI-004, UI-005.
- **Shared foundation:** Existing Glass UI registries, `InlinePanelBuilder`,
  Stash selection semantics, confirmation panels, symbols/styles and Appearance.
- **Dependencies:** None for the interaction layer; Workspace/Task groups consume
  the resulting patterns.
- **Suggested sequence:** (1) inventory and ordering rules; (2) reusable selection
  state/row rendering; (3) contextual bulk-action contract; (4) confirmation
  standardization; (5) visual/accessibility polish.
- **Definition of Done:** A written UI ordering rule; every main panel follows
  it; selection works on at least Stash and one new list; selected rows show a
  check; re-click deselects; bulk actions are hidden without selection; every
  destructive bulk action has a scope-specific confirmation; no parallel panel
  or selection system; callback identifiers remain stable; tests cover toggle,
  stale callback, pagination and confirmation.
- **Risks:** Telegram keyboard limitations, callback-data compatibility,
  Appearance's top-level-only contract, and "minimal" hiding needed controls.
- **Why together:** The selection gesture, row layout, bulk affordance and
  confirmation are one interaction grammar; shipping them separately creates
  inconsistent rows and duplicate state.

### G2 — Language and Output Contract

- **Goal:** Give every generated artifact one explicit, extensible and testable
  language decision.
- **Included:** LANG-001, LANG-002, LANG-003, LANG-004, LANG-005.
- **Shared foundation:** `panel_settings.language`, Taskloom language policy,
  `AIRequest.language`, preference rendering, History translate parameter.
- **Dependencies:** Workspace scope (WS-001) for the Workspace level; owner
  decision on precedence and UI-vs-content separation.
- **Suggested sequence:** (1) define scopes/precedence; (2) implement catalog and
  validation; (3) wire explicit selector into History/task creation; (4) add
  auto only as a fallback; (5) add glossary to extraction.
- **Definition of Done:** Persian and English selectable at global/task scope;
  Workspace scope available when Workspace exists; explicit selection wins over
  lower scopes; the resolved language is displayed with results; unsupported/
  unknown language is rejected, not silently defaulted; existing translate
  behavior remains compatible; tests cover precedence and every scope.
- **Risks:** Provider non-compliance, inconsistent existing settings, and
  changing behavior for users who relied on implicit English.
- **Why together:** A selector without a scope model creates conflicting
  settings; a catalog without precedence creates silent overrides.

### G3 — Source and Workspace Foundation

- **Goal:** Turn a resolved Telegram source into a durable, owner-approved
  Workspace without creating a second task or checkpoint authority.
- **Included:** WS-001, WS-002, WS-003, WS-004, SOURCE-001, SOURCE-002,
  SOURCE-003 (investigation only).
- **Shared foundation:** `PeerType`, existing dialog enumeration, link resolver,
  `history_schedules`, `ai_tasks`, saved-item origin ids.
- **Dependencies:** Owner-approved persistence model; G1 selection patterns;
  G2 language scope for Workspace language.
- **Suggested sequence:** (1) source-resolution service and search UI; (2)
  Workspace read model from existing data; (3) durable Workspace settings with
  owner-approved schema; (4) dashboard; (5) source health.
- **Definition of Done:** Owner can resolve/search/add a source with access
  validation; one Workspace opens with source type, latest summary/checkpoint,
  active tasks, language and honest activity/coverage; settings show inheritance;
  no second task/checkpoint store; unknown/inaccessible sources fail honestly;
  tests cover resolution, scope isolation and empty states.
- **Risks:** Data-model duplication, schema approval, Telegram entity
  resolution limits, invite-link feasibility, and large-account RPC cost.
- **Why together:** A Workspace is the identity/scope that source search creates
  and settings/dashboard/tasks consume; building them separately would duplicate
  source state.

### G4 — Evidence-Grounded Intelligence

- **Goal:** Make summaries and extraction verifiable, interactive and honest
  about coverage.
- **Included:** EVID-001, INTEL-001, INTEL-002, INTEL-003, INTEL-010, INTEL-011,
  INTEL-012, INTEL-037.
- **Shared foundation:** History retrieval, `HistoryMessage` ids/range metadata,
  provider tool/structured output contracts, language contract.
- **Dependencies:** G2 language; existing P1 History; owner decision on evidence
  persistence/retention; G3 for Workspace/source display (not for a single-chat
  MVP).
- **Suggested sequence:** (1) define a bounded evidence envelope and validate
  ids locally; (2) expose citations on summaries; (3) add follow-up retrieval;
  (4) add structured extraction schema; (5) add subjective-confidence rules;
  (6) add Evidence Explorer.
- **Definition of Done:** Every claim shown as evidence-backed either has a
  revalidated source reference or is clearly marked unsupported; follow-ups
  re-retrieve bounded source text rather than only the summary; extraction
  output is schema-validated with evidence/coverage; subjective results separate
  observations from judgments and refuse definitive person judgments below a
  defined evidence threshold; no second retrieval engine; tests cover invented
  ids, empty evidence, conflicting evidence and context isolation.
- **Risks:** Citation hallucination, prompt injection, privacy, cost, evidence
  retention, and schema evolution with no migration in this planning task.
- **Why together:** Citations, follow-ups, extraction and subjective rigor all
  depend on the same evidence envelope; building extraction first would produce
  ungrounded output that later needs rewriting.

### G5 — Unified Task Center

- **Goal:** Make existing durable work visible and controllable without a second
  task system.
- **Included:** TASK-001, TASK-002, TASK-003, TASK-004, TASK-005.
- **Shared foundation:** `Taskloom`, `TaskManagementService`, `TaskRepository`,
  `TaskScheduler`, occurrence metadata and traces.
- **Dependencies:** G1 list/selection; G2 for task language; G3 for Workspace
  scope; scheduler cancellation/retry semantics must be verified.
- **Suggested sequence:** (1) unify read views; (2) expose output/occurrence
  detail; (3) add safe lifecycle controls; (4) add Workspace/language scope;
  (5) add operational timeline.
- **Definition of Done:** One owner-only Task Center lists running/scheduled/
  completed/failed/paused work; each task shows source/scope, effective language
  and next run; occurrence output is inspectable within bounds; supported
  cancel/retry operations are CAS-safe and honestly reported; unsupported
  operations are absent; no direct DB access in handlers; no second executor.
- **Risks:** Cancellation side effects, stale CAS versions, mixing todos with
  scheduled work, and exposing sensitive output.
- **Why together:** List, lifecycle, output and scope are one operational
  workflow; a list without controls or a control surface without output is
  incomplete and encourages duplicate solutions.

### G6 — Cross-Source Research and Portability

- **Goal:** Let the owner investigate multiple selected Workspaces and take the
  results out of Telegram with attribution preserved.
- **Included:** INTEL-020, INTEL-022, SEARCH-001, SEARCH-002, EXPORT-001,
  EXPORT-002, INTEL-040.
- **Shared foundation:** Workspace scope, evidence contract, bounded per-source
  retrieval, export serialization.
- **Dependencies:** G3 (Workspace), G4 (evidence), G2 (language); SEARCH-001
  additionally needs an approved embedding/index/store decision.
- **Suggested sequence:** (1) cross-source read-only query with explicit source
  labels; (2) citation-preserving aggregation; (3) export JSON/CSV/Markdown; (4)
  portable bundle; (5) research notebook; (6) semantic/similar search only
  after an approved indexing design.
- **Definition of Done:** A query selects explicit Workspaces and returns
  per-source evidence with gaps; no source is silently omitted; export carries
  source/coverage/evidence schema; search has an honest empty/unsupported state;
  owner can disable/delete stored indexes; tests cover attribution, per-source
  bounds and export escaping.
- **Risks:** Cost, ranking bias, cross-source privacy, vector-store operations,
  export leakage and format drift.
- **Why together:** Cross-source answers are only useful if they retain source
  boundaries and can be exported; semantic search without attribution would
  undermine the same trust contract.

### G7 — Activity Analytics and Schedule Recommendations

- **Goal:** Understand source rhythm and recommend owner-controlled timing.
- **Included:** ANALYTICS-001, ANALYTICS-002, ANALYTICS-003, ANALYTICS-004,
  ANALYTICS-005.
- **Shared foundation:** Workspace, bounded retrieval, Scheduled History,
  activity coverage metadata.
- **Dependencies:** G3 Workspace; G4 evidence/coverage; Scheduled History Phase 7
  live validation for recommendations that affect real schedules.
- **Suggested sequence:** (1) define sampling windows and coverage; (2) compute
  metrics; (3) dashboard; (4) recommendations; (5) owner-approved prefill into
  the existing schedule wizard; (6) comparison/coverage report.
- **Definition of Done:** Metrics state their window/coverage; peak/active-period
  logic is timezone-aware; recommendation never creates or changes a task;
  "use this time" opens the existing owner-only wizard; tests cover empty,
  partial, timezone and boundary data.
- **Risks:** RPC cost, sampling bias, misleading precision and schedule
  over-automation.
- **Why together:** Metrics, recommendations and schedule application form one
  user decision loop; separating them produces analytics that cannot be acted on
  or automation that bypasses the owner.

### G8 — Longitudinal and Advanced Intelligence

- **Goal:** Explore higher-order analysis only after evidence, Workspace and
  analytics foundations are trustworthy.
- **Included:** INTEL-030, INTEL-031, INTEL-032, INTEL-033, INTEL-034,
  INTEL-035, INTEL-036, SEARCH-003.
- **Shared foundation:** Evidence index, entity extraction, Workspace/activity
  history, task scheduler for opt-in runs.
- **Dependencies:** G3, G4, G7; owner approval for new persistence/retention and
  tolerance for false positives.
- **Suggested sequence:** entity extraction -> decisions/promises -> topic
  evolution/contradiction -> change detection -> graph -> watchlists, each behind
  an explicit evaluation.
- **Definition of Done:** Each capability has a precise evidence contract,
  confidence display, owner confirmation for consequential claims, and a clear
  unsupported/insufficient-evidence state; no automated judgment about people;
  no always-on global scan; tests cover false-positive/refusal paths.
- **Risks:** High false-positive risk, privacy, cost, entity ambiguity and
  schema/retention decisions.
- **Why together:** These are all evidence-derived longitudinal views; they
  should not ship before the evidence/Workspace/analytics contracts they consume.

## 7. Priority Model

Priority is not a single score. **Product priority** asks how much the capability
matters to the owner's vision. **Implementation priority** asks what should be
built first because other work depends on it. A high-value idea can have a later
implementation priority if its prerequisites are missing.

Scoring dimensions: user value, strategic importance, dependency impact,
implementation effort, technical risk, reuse potential and whether it unlocks
other features. The scores below are comparative judgments from source evidence,
not precise estimates.

| Group | Product priority | Implementation priority | Why |
|---|---|---|---|
| **G2 Language** | **P0** — every generated artifact needs it | **P0** — small shared prerequisite | Existing language behavior is fragmented and defaults to English; fixing the contract prevents inconsistent output in every later group. |
| **G4 Evidence** | **P0** — trust is the product's differentiator | **P1** — depends on G2 and a bounded evidence design | Without evidence, follow-ups/extraction would produce unverifiable claims. It is foundational but needs a design decision before coding. |
| **G1 UI** | **P1** — immediate owner pain, broad reach | **P0/P1** — selection grammar is reusable but polish can follow | Selection/confirmation blocks Workspace and Task Center; visual polish can ship incrementally. |
| **G3 Workspace** | **P1** — the organizing concept | **P1** — needs owner-approved persistence | It unlocks analytics, cross-source and per-source settings but can duplicate existing state if rushed. |
| **G5 Task Center** | **P1** — operational control | **P1** — existing machinery is reusable | High value with comparatively low new infrastructure, but cancellation/retry semantics need verification. |
| **G6 Cross-source/export** | **P2** — valuable research workflow | **P2** — requires G3 + G4 | Depends on source scope and evidence; semantic search adds a separate storage/provider decision. |
| **G7 Analytics/schedules** | **P2** — useful, not blocking | **P2** — needs Workspace and live Scheduled History evidence | Recommendations must not outrun the unfinished Phase 7 validation. |
| **G8 Advanced intelligence** | **P3** — exploratory | **P3** — high risk/cost | Valuable ideas but unproven quality and significant privacy/schema implications. |

### Why this ordering differs from product excitement

- The owner's UI pain is real, but a language contract and selectable rows are
  smaller shared enablers that prevent rework in every later panel.
- Workspace is strategically central, but its data model is unresolved; building
  analytics or cross-source queries before it would invent a second source model.
- Evidence is product-critical, yet implementation starts with a bounded
  envelope and validation, not a large graph or vector database.
- Advanced intelligence is intellectually attractive but has the weakest
  evidence base and highest risk of unsupported claims.

## 8. Proposed Implementation Sequence

This is a proposal for owner review, not permission to start. Each step must
finish its acceptance tests and owner-visible verification before the next
dependent step.

### Step 1 — G2 Language and Output Contract
- **Prerequisites:** Owner decision on precedence and UI/content separation.
- **Implement:** language catalog, scopes, explicit selector, precedence,
  validation, result metadata; auto only as fallback.
- **Out of scope:** Workspace persistence, automatic detection of every source,
  provider capability claims.
- **Deliverables:** one validated language contract; History/task UI selectors;
  tests for precedence and unsupported languages.
- **Validation:** focused tests; a real owner-visible summary in Persian and
  English; no language change to UI chrome.
- **Decision needed:** precedence and whether the existing `language` setting is
  migrated or replaced.

### Step 2 — G1 Glass UI Interaction Foundation
- **Prerequisites:** Approved ordering rules; G2 result labels available.
- **Implement:** shared row/selection grammar, check state, contextual bulk
  actions, destructive confirmations, visual/accessibility pass.
- **Out of scope:** data-heavy Workspace dashboards; new panel API.
- **Deliverables:** documented standards; Stash-compatible selection generalized
  to one new list; confirmation and stale-callback tests.
- **Validation:** offline UI tests plus live panel review by the owner.
- **Decision needed:** how far "compact" may reduce visible actions.

### Step 3 — G5 Unified Task Center
- **Prerequisites:** G1 list/selection; verified cancel/retry semantics.
- **Implement:** unified read views, occurrence output, safe lifecycle controls,
  Workspace/language scope, bounded timeline.
- **Out of scope:** new task repository/executor; automatic task repair.
- **Deliverables:** one owner-only Task Center over `TaskManagementService` and
  the existing scheduler.
- **Validation:** focused tests for CAS, stale versions, unsupported actions;
  real owner inspection of a paused/completed task.
- **Decision needed:** extend Taskloom in place versus a new navigation layer
  (recommended: extend Taskloom).

### Step 4 — G3 Source and Workspace Foundation
- **Prerequisites:** G1 selection; G2 language; owner-approved persistence model.
- **Implement:** source resolution/search, Workspace read model, settings,
  dashboard, source health; invite-link investigation separately.
- **Out of scope:** analytics/cross-source inference before the model is stable;
  auto-join; schema changes without approval.
- **Deliverables:** owner-approved source can be resolved and opened as a
  Workspace with honest state.
- **Validation:** source resolution/access tests, empty/failure states, scope
  isolation; live owner check on one private chat, one group/channel if available.
- **Decision needed:** where Workspace settings live and whether a new table is
  justified.

### Step 5 — G4 Evidence-Grounded Intelligence
- **Prerequisites:** G2; P1 History; approved evidence retention decision; G3 for
  Workspace display.
- **Implement:** evidence envelope/citations, bounded follow-up retrieval,
  structured extraction, subjective rigor, Evidence Explorer.
- **Out of scope:** knowledge graph, always-on extraction, unvalidated claim
  storage.
- **Deliverables:** verifiable summary/follow-up/extraction flows with coverage.
- **Validation:** invented-id/citation tests, conflicting evidence, empty/
  insufficient evidence, context isolation; live owner review of one cited run.
- **Decision needed:** ephemeral evidence versus durable index; retention.

### Step 6 — G7 Activity Analytics and Recommendations
- **Prerequisites:** G3; G4 coverage metadata; Scheduled History Phase 7
  live validation for any recommendation affecting schedules.
- **Implement:** bounded activity metrics, dashboard metrics, recommendations,
  owner-approved prefill, comparison/coverage report.
- **Out of scope:** auto-changing schedules; full-history exact counts.
- **Deliverables:** metrics with windows/coverage; recommendation that never
  auto-creates tasks.
- **Validation:** timezone/boundary/empty/partial tests; owner compares one
  recommendation with observed activity.
- **Decision needed:** sampling window, retention, and whether analytics are
  cached.

### Step 7 — G6 Cross-Source Research and Portability
- **Prerequisites:** G3, G4, G2; approved vector/index decision for semantic
  search.
- **Implement:** cross-source queries, similar discussion discovery, export,
  portable bundle, research notebook.
- **Out of scope:** global account-wide monitoring; unauthenticated download
  endpoints.
- **Deliverables:** attributed multi-source results and bounded exports.
- **Validation:** per-source attribution, gap reporting, CSV escaping, size/
  retention tests; owner runs one real multi-source question.
- **Decision needed:** export destination/retention; semantic index approval.

### Step 8 — G8 Longitudinal and Advanced Intelligence
- **Prerequisites:** G3/G4/G7; owner approval for persistence and false-positive
  tolerance.
- **Implement:** entity extraction, decision/promise trackers, topic evolution,
  contradiction/change detection, graph, watchlists — each behind its own
  evaluation gate.
- **Out of scope:** autonomous judgments about people, always-on monitoring,
  speculative graph edges presented as facts.
- **Deliverables:** evidence-backed candidate views with confidence and explicit
  refusal states.
- **Validation:** precision/recall evaluation on owner-approved samples, privacy
  review, false-positive/refusal tests.
- **Decision needed:** whether each advanced capability is worth its cost and
  risk; approval may be withheld.

## 9. Deferred and Exploratory Ideas

| Idea | Blocker / unanswered question |
|---|---|
| SOURCE-003 invite links | Current parser does not support them; Telegram invite preview/join semantics and side effects are unverified. |
| INTEL-030 Knowledge Graph | No entity/evidence foundation; high false-relation risk; graph storage decision missing. |
| INTEL-031 Topic Evolution | Requires reliable time-bucketed evidence and a baseline; missing-message bias unresolved. |
| INTEL-032 Contradiction Detection | Context and subjectivity can create false contradictions; needs an evidence/evaluation design. |
| INTEL-033 Decision Tracker | A proposal is not a decision; owner confirmation flow and schema are undefined. |
| INTEL-034 Promise Tracker | Sarcasm/hypotheticals, misattribution and privacy risks need policy. |
| INTEL-035 Change Detection | Edited/deleted messages are not re-readable without an archive; snapshots are incomplete. |
| INTEL-036 Entity Extraction | Entity resolution and duplicate merging need an evidence-backed policy. |
| SEARCH-001 Semantic Search | No embedding provider/index/store exists; content-retention and cost are owner decisions. |
| SEARCH-002 Similar Discussion | Depends on semantic retrieval; relevance evaluation is undefined. |
| INTEL-040 Research Notebook | Needs a persistence model and must not duplicate Saved Items. |
| EXPORT-002 Portable Bundle | Import semantics and sensitive-data boundaries are unresolved. |
| ANALYTICS-002/003 Recommendations | Depends on bounded analytics and Scheduled History Phase 7 live validation. |
| Chat-only links | Existing parser refuses them; extending it is a separate product decision. |
| Automated per-chat digest | P3 in the active History roadmap; Phase 6 admission and Phase 7 live checks remain deferred. |

## 10. Open Questions and Owner Decisions

Only decisions that genuinely need the owner are listed; source-inspectable
facts are not questions.

1. **Workspace persistence:** Should Workspace settings be a new owner-scoped
   model, an extension of existing task/history rows, or an initially derived
   read model with no new persistence? If new persistence is approved, the owner
   controls the schema and migration.
2. **Language precedence:** Confirm the intended order (explicit request > Task >
   Workspace > global > default) and whether Glass UI language is a separate
   setting from generated-content language.
3. **Evidence retention:** Is bounded evidence allowed to persist across sessions
   (for follow-ups/exports), or must it remain ephemeral and be re-fetched from
   Telegram? This determines whether a new index/table is justified.
4. **Cross-source scope:** May a query read only explicitly selected Workspaces,
   or does the owner want broader account-level discovery? The former is the
   safer proposed default.
5. **Activity data:** What sampling window/retention is acceptable for analytics,
   and should metrics be cached durably or recomputed on demand?
6. **Advanced intelligence:** Which of the graph/trackers/contradiction features
   are worth the false-positive and privacy risk? Approval can be withheld
   indefinitely.
7. **Task Center shape:** Extend Taskloom in place or introduce a new top-level
   navigation entry? The source already has Taskloom under AI.
8. **Export destination:** Telegram file delivery, dashboard download, or durable
   object storage? Each has different retention/security implications.
9. **UI scope:** Approve the ordering/selection rules and how much visual
   customization is worth pursuing within Telegram's panel constraints.

## 11. Change Log

| Date | Change |
|---|---|
| 2026-11-14 | Created this document at `main`/`origin/main` `f61e12e`. Recorded the owner-provided UI, language, interactive summary, extraction, Workspace, analytics, source search, cross-source, Task Center and advanced intelligence ideas; added clearly labeled assistant suggestions; mapped existing History/Scheduled History/Taskloom/language/search capabilities; proposed eight implementation groups, separate product/implementation priorities, a dependency-aware sequence, deferred items and owner decisions. No implementation, schema, Supabase, Telegram, deployment or test change was made. |
