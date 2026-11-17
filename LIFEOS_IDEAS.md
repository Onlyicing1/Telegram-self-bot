# LIFEOS IDEAS — Product Inventory and Implementation Planning

> **Status:** planning only. This document is not an execution roadmap and does
> not authorize implementation. `ROADMAP.md` remains the authoritative record
> of active phases and verified delivery.
>
> **Source baseline:** `main` at
> `416918e93584456044a2fa0ac4d4bc18a3c65dba` (`origin/main`, re-verified
> 2026-11-15). The working tree was clean before this revision.
>
> **Investigated:** 2026-11-14, from the repository source, tests and existing
> architecture documents; **revised 2026-11-15** for the owner-approved
> evidence-persistence direction (§4.5.1, G4 stages, §10). No SQL was executed,
> no Supabase object was touched, no Telegram operation was performed, and no
> application deployment occurred.

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

### Cross-cutting product principle — User Agency First (owner-approved)

LifeOS should give the owner meaningful control and freedom to decide what they
want, how they want to use a capability, and how much assistance or automation
they want. It should not make decisions on the owner's behalf when meaningful
choice is possible, and it should not force one rigid interaction model. The
goal is to maximize meaningful agency **while preserving security, privacy,
reliability and predictable system behavior**.

1. **User choice over unnecessary assumptions** — where multiple legitimate
   modes exist, let the owner choose; do not silently infer a preference that
   materially changes the result; an assistant's preferred workflow is not a
   mandatory product rule.
2. **Configurable behavior** — where appropriate, offer meaningful choices for
   feature behavior, interaction style, evidence visibility, automation,
   retention and other user-facing policies. Choices stay changeable, and each
   setting respects its own scope and lifetime.
3. **Safe and predictable defaults** — a feature stays usable without
   configuring every detail; defaults are conservative, transparent and
   predictable; the absence of a preference is never permission for a more
   invasive or less safe behavior.
4. **Progressive disclosure** — present essential choices when they matter,
   keep ordinary interaction free of a large configuration surface, and make
   advanced controls discoverable instead of forcing them into every
   interaction while preserving access for owners who want more control.
5. **Honest boundaries** — freedom is not unrestricted execution: security,
   privacy, authorization, deterministic validation and architectural
   boundaries remain mandatory, and unavailable or restricted capabilities are
   explained honestly.
6. **No unnecessary paternalism** — do not restrict a legitimate choice merely
   because the system prefers another workflow; explain meaningful
   consequences and let the owner decide where policy and safety permit,
   without confusing protection with taking control away.

This is a product-direction principle, not an implementation decision: it does
not authorize new capabilities, settings, schema or automation, and it does not
settle any question in §10. The scope rules in §1 and the existing authority
boundaries continue to govern every design.

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
- **Status:** Partial — the ordering rule is documented in `AGENTS.md` and applied
  to Stash/Taskloom; broader panel normalization remains.
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
- **Status:** Partial — Stash and Taskloom use the same owner/feature-scoped
  bounded selection store.
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
- **Status:** Partial — Stash has bulk send/delete; Taskloom has contextual
  Pause/Resume/Complete/Delete over CAS-revalidated task ids.
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
- **Status:** Partial — Stash and Taskloom destructive bulk operations have
  dedicated confirmation views with red confirm buttons; other panels vary.
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
- **Status:** Partial — global, request and task scopes are implemented
  (`G2-A`: `backend/ai/language.py`, global `ai_preferences.language`, Taskloom
  task language). The Workspace scope is deferred to `G2-B`; the Glass UI
  language remains separate and unchanged.
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
- **Status:** Implemented for History, AI requests and Taskloom (`G2-A`):
  request > task > global > English, with the effective language in result
  metadata and the History panel/result surface.
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
- **Status:** Implemented as an explicit mode for source-based History summaries
  (`G2-A`). Detection is a conservative script heuristic; an unresolved source
  uses the documented English fallback and says so. Auto is never inferred and
  never overrides an explicit task language.
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
- **Status:** Implemented (`G2-A`) in `backend/ai/language.py`: one validated
  catalog (code, label, direction, provider hint) used by the resolver, Taskloom
  and the History tools. Adding a language is a data change.
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

#### LANG-005 — Extraction terminology glossary (persistent Workspace Glossary)
- **Origin:** Assistant-discovered; persistence and scope approved by the owner
  (decision 6, 2026-11-15).
- **Status:** Approved direction, **design only** (G4-C). No glossary table, UI
  or injection exists, and none may be added before G3-B.
- **Problem:** Names, course titles, project terms and transliterations are
  inconsistently translated or extracted.
- **Expected behavior:** A **persistent, Workspace-scoped, owner-editable and
  extensible** glossary, bounded in entries and characters, injected only into
  the explicitly supported G4-C evidence operations. Conflict handling is
  explicit (refuse, show the existing entry, require an owner-confirmed
  replace); there is no automatic learning from conversations.
- **Workspace dependency:** the glossary must use the real Workspace identity,
  ownership and authorization established by **G3-B**. No owner-level key,
  temporary Workspace table, environment/header scope or other substitute is
  permitted. Implementation is blocked until G3-B is complete and approved.
- **Value:** Better entity consistency in Persian/English research.
- **Reuse:** Prompt preference/instruction composition and validation bounds;
  G2-A language precedence stays authoritative.
- **Depends on:** LANG-001; extraction (INTEL-010); **G3-B** for any persistence
  or injection.
- **Risks/unknowns:** Prompt growth, conflicts, entry/character bounds, and
  privacy. Bounded, explicit and opt-in.
- **Do not:** Turn the glossary into an unrestricted memory store, inject it into
  Scheduled History, let it silently override evidence, or invent a Workspace.

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
- **Status:** G4-A/F1 implements bounded evidence refs, locally constructed
  links and opt-in Cited Summary offline (migration artifact unapplied).
  Restart-safe follow-up citations remain G4-A/F2.
- **Problem:** A summary cannot be verified or opened at the supporting message.
- **Expected behavior:** Each conclusion/claim can carry bounded evidence
  references (source, message id/range, short excerpt), rendered as tappable
  links in the owner-facing Explorer. Links are **constructed locally** from a
  validated record; provider-supplied URLs/ids are never trusted. Cited Summary
  is an explicit owner-selected mode; ordinary summaries stay plain.
- **Value:** Trust, auditability and a foundation for every advanced feature.
- **Reuse:** `HistoryMessage.message_id`, `HistorySlice` first/last ids,
  `saved_items.origin_chat_id/origin_msg_id`, message-link formats.
- **Depends on:** A provider output contract that requests structured claims and
  application-side validation of every key/id; EVID-002 storage.
- **Risks/unknowns:** Links expose identifiers to the owner's own client only;
  edited/deleted sources need captured snapshots; an expired run must fail
  honestly instead of re-reading unrelated history.
- **Do not:** Present model-generated citations as verified without re-checking
  the source record, or put an evidence envelope into generic tool/audit/
  conversation history.

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
- **Status:** Proposed (G4-A / F1).
- **Problem:** Even a cited answer is hard to inspect in context.
- **Expected behavior:** Open a run's evidence list, inspect the exact source
  message/range and bounded surrounding context, and see why it was included.
  Read-only; no model call required. Direct Telegram deep links are permitted
  here (decision 7), built locally from the validated record; a link is never
  copied from provider output.
- **Value:** Verification and trust.
- **Reuse:** INTEL-002 references, the existing Telegram message facade,
  `list_recent_messages` bounded window.
- **Depends on:** INTEL-002; the EVID-002 durable run; a validated source
  identity for the link (the owner's own chat, re-checked on read).
- **Risks/unknowns:** Access changes, deleted messages, expired runs and
  identifier exposure in links.
- **Do not:** Make the explorer a new Telegram client or archive subsystem, or
  resolve a link from an unvalidated id.

#### EVID-001 — Durable evidence index
- **Origin:** Assistant-discovered.
- **Status:** **G4-A/F1 implemented offline** (2026-11-16). Service,
  repository, tests and an additive migration artifact exist; the migration has
  **not** been applied and no live table is claimed.
- **Problem:** Cross-session follow-ups, trackers and exports need stable
  references; current summaries carry only a range and scheduled summaries carry
  none.
- **Expected behavior:** One bounded evidence contract over **two distinct
  retention classes** — a temporary Active Evidence Run with an explicit TTL,
  and an owner-approved indefinite Evidence Archive built from **immutable
  revisions** — revalidated against Telegram and never substituted from
  conversation history.
- **Value:** Shared foundation for citations, follow-ups, extraction, trackers
  and export.
- **Reuse:** Existing run/result metadata and message links; the
  `history_schedules` migration is the durable service-role-only precedent.
- **Depends on:** EVID-002/003; the TTL, limits and archive quotas approved on
  2026-11-16 (§4.5.1). Implementation still needs an owner-authorized
  migration.
- **Risks/unknowns:** New schema, sensitive content, stale references and quota
  growth. The owner has approved indefinite retention but not unlimited
  consumption.
- **Do not:** Create a second message archive, copy full chats, or attach
  evidence to Scheduled History (which stays body-only and citation-free).

#### EVID-002 — Durable Active Evidence Run
- **Origin:** Owner-provided (decisions 1, 3, 9, 11).
- **Status:** **G4-A/F1 implemented offline.** Durable runs, refs, TTL and
  opportunistic expiry cleanup are covered by focused tests; the migration
  artifact remains unapplied. Restart-safe follow-up interaction is F2.
- **Problem:** A follow-up must still work after the process restarts, while the
  working evidence must not become a permanent archive.
- **Expected behavior:** One server-generated opaque run per Cited Summary or
  follow-up, owned by the owner and scoped to one source. It stores a bounded
  message range, bounded excerpts, hashes, an optional archived
  `revision_id` for refs that came from the archive, validation state, an
  optional bounded summary, and an `expires_at`. Each ref is a bounded snapshot
  copy, not a pointer that keeps the archived revision alive: the archive row
  can be manually deleted while this excerpt remains until the run's TTL.
  The TTL is chosen at creation (default 7 days; owner range 1–30 days) and
  persisted for that run. Expiry is checked lazily against authoritative
  database time (no new scheduler). Once `expires_at` passes, the run is
  unusable for retrieval, Explorer, Cited Summary and follow-ups; it fails
  honestly and is never reconstructed from Telegram, archive, summary or
  conversation context. Logical expiry does not itself delete rows. Approved
  Option 1 permits physical deletion of the run and its refs only
  opportunistically inside an existing authorized operation, with no
  tombstone, scheduler or promised deadline; never-touched expired data may
  remain physically stored indefinitely. See §4.5.1.
- **Value:** Makes follow-ups restart-safe without keeping everything forever.
- **Reuse:** `HistoryMessage`/`HistorySlice`, the existing Telegram re-read
  boundary, the `history_schedules` service-role-only migration pattern.
- **Depends on:** G2-A (language) and the existing History service.
- **Risks/unknowns:** source edits/deletes must be labelled, not silently
  refreshed; archive deletion leaves bounded excerpts in active runs until TTL,
  and no purge is approved or specified. A never-read expired row may remain
  physically stored indefinitely because no cleanup scheduler is approved.
- **Do not:** Store media binaries, full chats, provider URLs, or reuse a run
  across owners/sources. An opaque run id alone is never authorization. Never
  treat an active-run excerpt as the archive, and never reconstruct manually
  deleted archive text from it.

#### EVID-003 — Owner-approved Evidence Archive (immutable revisions)
- **Origin:** Owner-provided (decisions 1, 11).
- **Status:** **G4-A/F1 implemented offline.** Owner-confirmed archive
  suggestions, immutable revisions, hash idempotency, quota rejection and
  dependent-aware deletion are covered by focused tests; the migration artifact
  remains unapplied.
- **Problem:** Some evidence must stay inspectable months later, even after the
  original Telegram message is edited or deleted.
- **Expected behavior:** A stable archived **source** per
  `(owner, source, message id)` plus one or more **immutable revisions**. The
  system may suggest candidates and explain why each is a candidate, but each
  archived revision requires explicit owner approval. Identical content is
  deduped by hash (no write); edited
  content produces a new revision only after approval — never an overwrite.
- **Revision identity:** `revision_id`, equivalently
  `(source_id, revision_no, content_hash)`. Citations, runs and later analyses
  reference that exact revision; nothing means "latest".
- **Deletion (approved 2026-11-16):** owner-confirmed per revision or per
  source. At both levels, dependent derived records block the ordinary
  confirmation; if any exist, a separate explicit cascade confirmation with a
  clear impact explanation is required. Active-run refs and their bounded
  excerpts are not dependent records and are not deleted by archive deletion;
  they remain until the run's TTL. No purge is approved or specified. No
  automatic expiry and no eviction to satisfy a quota. Full rules: §4.5.1.
- **Value:** Durable, trustworthy provenance without an unrestricted chat
  archive.
- **Reuse:** EVID-002 capture path, the same evidence contract.
- **Depends on:** EVID-002; archive quotas and the deletion policy approved on
  2026-11-16 (§4.5.1). Implementation still needs an owner-authorized
  migration.
- **Risks/unknowns:** Storage growth (revisions multiply on edits) and quota
  exhaustion; archive quotas are 5,000 records or 50 MiB per owner, whichever
  is reached first, with rejection rather than eviction. Earlier analyses must
  remain traceable to the revision they used.
- **Do not:** Overwrite a snapshot, merge revisions, archive a whole chat or a
  media binary, or discard archived evidence to make room.

#### EVID-004 — Derived Workspace Knowledge record
- **Origin:** Assistant-discovered.
- **Status:** Proposed for a later stage (design only).
- **Problem:** Repeated analysis should cite a stable archived source instead of
  copying text again.
- **Expected behavior:** A persistent derived record (claim/summary/entity),
  scoped to the real Workspace and linked to its source evidence, references an
  archived **`revision_id`** plus its analysis provenance and its own revision.
  It never stores a second copy of the source text and never widens or narrows
  the intended Workspace Knowledge scope.
- **Value:** Keeps archive storage normalized and deletion coherent.
- **Reuse:** EVID-003 revision ids, structured extraction (INTEL-011), the real
  Workspace identity from G3-B.
- **Depends on:** EVID-003, G3-B and G4-C extraction.
- **Risks/unknowns:** Evidence-to-claim linkage, revision semantics and
  Workspace scope.
- **Do not:** Treat a derived claim as the source, let it outlive a deleted
  revision silently, or place it in a scope that is not the real Workspace.

### 4.5.1 Evidence persistence model (owner-approved design — NOT IMPLEMENTED)

No schema, migration or Supabase object exists. `ROADMAP.md` carries the
implementation status. The owner approved the data classes, TTL, limits,
archive quotas, deletion rules and immutable-revision rules recorded here on
2026-11-16. They are binding requirements for a future implementation, not
evidence that code or schema exists; implementation still requires explicit
owner authorization of the migration, and Supabase schema changes remain
manual.

**Retention classes (never conflated):**

| Class | Lifetime | Logical expiry | Physical deletion | Purpose |
|---|---|---|---|---|
| Active Evidence Run + refs | 7-day default; owner-selected 1–30 days, stored per run | Yes — authoritative database time; immediate denial; no scheduler | Option 1: opportunistic physical deletion inside an existing authorized operation; no tombstone; no guaranteed deadline | Cited Summary, Explorer, restart-safe follow-ups; each ref holds a bounded excerpt snapshot, not an archive pointer. |
| Archived Evidence Source + revisions | Until manual deletion | No | Manual owner deletion only | Owner-approved long-term source; immutable revisions. |
| Derived Workspace Knowledge | Derived | No | Explicit, owner-confirmed archive-deletion cascade only | References a revision id; no second text copy. |
| Workspace Glossary | Persistent (after G3-B) | No | Owner CRUD only | Owner terminology, not evidence and not AI memory. |

**Immutable archive revisions (corrected):**

- `ai_evidence_sources` is the stable identity (owner + source + message id)
  and stores no text.
- `ai_evidence_revisions` is append-only: `revision_id`, `source_id`,
  `revision_no`, `content_hash`, bounded text snapshot, captured-at, approval
  reference and last observed source state. A revision's text/hash/number is
  never updated, merged or replaced.
- Capturing content whose hash already exists writes nothing (idempotent, no
  duplicate). A different hash is a **candidate** revision; only an explicit
  owner approval creates it, and the previous revision is untouched. There is
  no automatic refresh or replacement.
- References identify `revision_id`; an earlier analysis can always be traced
  to the exact text it used. A provider or callback can never choose "latest".
- Telegram edits/deletes never mutate stored evidence: a revalidation records
  `live`/`edited`/`deleted`/`inaccessible` and a check time only.

**Active-run expiry: logical vs physical (APPROVED 2026-11-16):**

The TTL is part of the approved policy. It is selected when the run is created,
stored on that run, and never silently extended.

- **TTL:** default **7 days**; the owner may select **1–30 days** at creation.
  The selected value is persisted for that run.
- **Logical expiry:** `expires_at` is compared with authoritative database time,
  never a client clock. Once it has passed, the run is immediately inaccessible
  for retrieval, Explorer, Cited Summary and follow-ups, and is not valid
  evidence. Follow-ups fail honestly. The run and its evidence are never
  reconstructed from Telegram history, archive revisions, a stored summary,
  inferred conversation or another source. Active-run expiry does not affect
  immutable Archive revisions or derived Workspace Knowledge.
- **Physical deletion (Option 1, approved):** expired run data and its refs may
  be physically deleted only opportunistically during an existing authorized
  operation. No scheduler, worker, cron job, background cleanup loop or second
  lifecycle authority is introduced. No tombstone is created. There is no
  promised physical-deletion deadline: if no later authorized operation occurs,
  expired data may remain physically stored indefinitely. Expiry is still
  enforced logically in the meantime.
- **Capacity:** at the active-run quota, an authorized operation may first
  remove expired runs; if capacity is still unavailable, the new run is rejected
  with an explicit reason. A valid, unexpired run is never evicted.

**Deletion semantics (APPROVED 2026-11-16):**

Manual deletion is owner-triggered. The dependent-record and active-run rules
below apply equally to revision-level and source-level deletion; there is no
hidden or silent cascade.

| Data class | Holds | Revision-level delete | Source-level delete |
|---|---|---|---|
| Archived source identity | `source_id` + Telegram provenance; no text | Unchanged; removing a now-empty source is a separate explicit action | Removed with every revision |
| Archived revision text | Immutable bounded text, hash, approval and observed state | Selected revision permanently removed; other revisions untouched | Every revision permanently removed |
| Active run refs | Bounded excerpt copy, hash, `message_id`, optional `revision_id`; short TTL | Unchanged by default; excerpt remains until TTL and `revision_id` becomes a historical unresolved reference | Same for every revision of the source |
| Derived Workspace Knowledge | Claim/summary/entity referencing `revision_id`; no second text copy | Dependent: blocks deletion until explicit cascade | Same for all revisions of the source |

- **Dependents block by default.** The preflight covers every registered
  dependent derived record (currently `ai_workspace_knowledge`; any future
  dependent table must join the preflight) and each record's `revision_id`
  reference. If any derived record references a selected revision or any
  revision of a selected source, the first confirmation refuses and reports the
  count. A separate, explicit "delete dependents too" confirmation must list
  every dependent and remove it together with the archive selection. Derived
  records are never silently orphaned.
- **No dependents means one explicit confirmation.** It reports all target
  revisions and matching active-run refs/excerpts. Source-level confirmation
  never hides a multi-revision delete.
- **Revision vs source:** a revision delete leaves source identity and sibling
  revisions intact. If the last revision is selected, removing the now-empty
  source identity requires a separate explicit source-level confirmation; it is
  never an automatic cascade.
- **Active-run excerpts are retained by default.** Archive deletion does not
  cascade to `ai_evidence_run_refs`; their excerpts, hashes and historical
  `revision_id` remain until normal TTL expiry. An unresolved revision is
  labelled as deleted from the archive, never treated as live archive evidence
  and never used to reconstruct deleted text. This is approved behavior.
- **Permanent deletion is scoped.** It permanently removes the selected archive
  rows and, with explicit cascade, dependent derived records. It is not a
  promise that every copy disappears while active-run excerpts remain; the
  confirmation must disclose the TTL retention. No purge is available under the
  approved policy.
- **No purge is approved or specified.** A separate owner-requested purge may
  be considered later, but this design does not define one and implementation
  must not invent it. Until a purge policy is approved, archive deletion leaves
  matching active-run refs and excerpts untouched until their own TTL; the UI
  must disclose that retention and must not claim that every copy disappeared.
- **No automatic expiry or eviction** applies to archived revisions. No
  cleanup scheduler, silent eviction or TTL-based archive deletion is
  introduced; no whole-chat or media archival exists.

**Workspace dependency (no substitute):** the Glossary is genuinely persistent
and extensible, but the repository has no Workspace model. Its storage and
authorization must use the real Workspace identity/ownership established by
**G3-B**. G4-A and G4-C must not create an owner-level workspace key, a
temporary Workspace table, or any parallel scope. **Glossary persistence, UI
and injection stay blocked until G3-B is complete and its authorization model
is approved.** Workspace Knowledge keeps its intended scope: derived,
structured, persistent knowledge linked to source evidence.

**Conceptual objects (names only; no SQL is proposed here):**
`ai_evidence_runs` (temporary parent), `ai_evidence_run_refs` (temporary child,
`ON DELETE CASCADE` from the run only; optional `revision_id`),
`ai_evidence_sources` (stable identity), `ai_evidence_revisions` (immutable,
deduped by `(source_id, content_hash)`), `ai_workspace_glossary` (persistent,
only after G3-B), and a later `ai_workspace_knowledge` (derived, referencing a
revision). Runs store only the bounded excerpts they need; archive revisions
are the one long-term copy; derived rows reference a revision instead of
copying text. Logical expiry blocks all use; approved Option 1 permits
opportunistic physical deletion of runs and their refs inside an existing
authorized operation, with no scheduler or tombstone and no guaranteed
deadline. Revisions are never time-deleted, and archive deletion never cascades
to run refs.

**Storage: measured vs estimated vs assumed.**

- **Measured facts (code/schema only, not storage measurements):** no evidence
  or archive table exists; `history_schedules.summary` is bounded at 32 KiB;
  `ai_messages` truncates at 8,000 characters.
- **Assumptions:** average excerpt length, edit/re-archive rate, bytes per
  character by script (≈1 Latin, ≈2 Persian/Arabic, ≈3 CJK), one or two
  secondary indexes, and no unusual TOAST compression.
- **PostgreSQL overhead that the estimate must include:** tuple header and null
  bitmap, alignment padding, 8 KiB pages and fillfactor/free space, TOAST for
  large text, B-tree index pages, unique/foreign-key indexes where defined,
  revision multiplier after edits, and bloat before vacuum. WAL is not part of
  the table's reported size.
- **Transparent planning estimate (not measured):** 400 characters × 2 bytes ≈
  800 B of text; with roughly 0.3–0.8 KB of tuple/index overhead ⇒ **≈1.1–1.6
  KB per revision**. At 1,000 characters ⇒ **≈2.5–4 KB per revision**. These are
  ranges, not PostgreSQL measurements.
- **Illustrative scenarios (estimates):** 20 archived messages/month ≈0.3–0.7 MB
  per year; 200/month ≈3–7 MB per year; 2,000/month ≈30–72 MB per year,
  depending on edits and revision count. No Supabase billing figure or plan
  limit is claimed.

**Approved limits (2026-11-16):**

| Limit | Approved value |
|---|---|
| Maximum excerpt length | 1,000 characters |
| Maximum references per run | 50 |
| Maximum references per follow-up | 20 |
| Maximum normalized text per run | 32 KiB |
| Maximum active runs per owner | 20 |
| Active-run TTL | Default 7 days; owner-selectable 1–30 days, persisted per run |
| Archived records per owner | 5,000 |
| Archived data per owner | 50 MiB |

The archive limits are whichever is reached first. They are application
planning limits, not exact PostgreSQL billing or physical-storage
measurements. Limits must come from the approved policy/configuration; no value
may be hard-coded. **The configuration surface itself is still an open
decision**, as are glossary quotas, which are deferred until G3-B establishes
the real Workspace; no glossary number is approved.

**At an approved limit** the system applies the approved Option 1 policy: at
the active-run cap, an authorized admission path may opportunistically remove
expired runs first; if capacity is still unavailable, the new run is rejected
with an explicit reason. A valid, unexpired run is never evicted. A new archive
operation is rejected when either archive quota is reached; archived evidence
is never automatically deleted, evicted or overwritten to make room. The owner
may only delete explicitly or raise a limit through an explicit approval
workflow.

**Isolation and provider projection:** every row is owner-scoped; a Workspace
reference must be a real G3-B identity validated for that owner, and an opaque
id is never authorization. Runs, sources, revisions and the glossary are
service-role-only (RLS enabled, zero policies,
`SELECT/INSERT/UPDATE/DELETE` granted to `service_role`) following
`history_schedules`. A provider sees only the minimum bounded excerpt plus
opaque local keys; it never receives Telegram message ids, sender ids, owner
ids, Workspace ids, URLs or content hashes, and never decides ids, revision,
counts, links, existence, archival, deletion or retention. No Telegram
conversation context is passed into isolated media/evidence analysis. The
envelope (ids, excerpts, hashes, links) must not enter
`ToolResult.data`/`message`, tool arguments, `ai_tool_history`, `ai_messages`,
the prompt or the provider continuation. Generic results carry only an opaque
run id and bounded non-sensitive counts/coverage.

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

Revised 2026-11-14 after a second source inspection. The previous graph treated
each group's idea list as if every idea in it had the same prerequisite. That
was wrong in three places: Workspace language, Workspace-aware Task Center
scope, and Workspace-aware evidence browsing were all scheduled before the
Workspace foundation existed.

### 5.1 Dependency classes

These are deliberately separate. They are not interchangeable.

| Class | Meaning | Consequence |
|---|---|---|
| **Technical dependency** | The capability cannot be implemented correctly without the foundation. | The dependent stage must not start. |
| **Product dependency** | The capability is technically possible, but the user workflow would be incomplete or misleading without another feature. | A limited stage may ship, but the dependent deliverable must be labelled incomplete or deferred. |
| **Validation gate** | Code may exist, but real-world validation must pass before dependent automation or recommendations are enabled. | Build only behind a disabled/default-off path until the gate passes. |
| **Owner approval** | A product, persistence, privacy or provider choice cannot be made from source inspection. | Stop at the decision boundary; do not invent schema, storage or policy. |

Readiness is reported separately in Section 7. A high product priority does not
mean a stage is ready.

### 5.2 Corrected dependency graph

Corrected 2026-11-15: G4-B keeps its original Workspace-aware meaning, the
Glossary depends on the real G3-B Workspace, and restart-safe follow-ups are a
G4-A delivery phase rather than a stage.

```text
G1-A UI interaction ────────────────────────────┬──> G5-A Task Center
                                                │
G2-A language (global/task/manual) ─────────────┘
                                                │
G4-A / F1 single-source evidence storage ───────┘
      + Cited Summary + Explorer (no Workspace)
        │
        ├──> G4-A / F2 restart-safe follow-ups
        │
        └──> G4-B Workspace-aware durable evidence <── G3-A + G3-B

G3-A source/Workspace identity ──> G3-B Workspace settings/dashboard/health
        │                               ├──> G2-B Workspace language
        │                               ├──> G5-B Workspace task scope
        │                               └──> G4-C extraction + persistent Glossary
        │
        ├──> G6-A cross-source query + export <── G4-A
        └──> G7-A activity metrics + coverage <── G4-A
                    └──> G7-B schedule recommendations ──[V: Scheduled History Phase 7]

G4-C ──> G4-D threshold-gated assessments
G6-A ──[O: index/store]──> G6-B semantic search/similar discussion

G3-A + G4-A + G4-C + G5-A + G6-A + G7-A ──[O: advanced approval]──> G8 longitudinal intelligence

G1-A ──> G1-B visual identity/accessibility
```

### 5.3 Shared foundations

| Foundation | Correct scope | Source-verified evidence | First consuming stage |
|---|---|---|---|
| **Language base** | Global default, explicit request, task language, manual History output. | `panel_settings.language` exists but no AI/History caller was found; `AIRequest.language` defaults to `"English"` and `ai_unified.py` does not pass it; `summarize_history` has no language argument and defaults to English; `translate_history` accepts one; Taskloom has `en/fa/ar/zh` policy words. | `G2-A` |
| **Workspace language** | Per-Workspace default and effective-language resolution. | No Workspace model or per-source language storage exists. | `G2-B`, after `G3-B` |
| **Workspace identity** | Owner-approved source identity, type, access state and source list. | `PeerType`, bounded dialog enumeration, link resolver and `history_schedules(owner_id, peer_id)` exist; there is no Workspace table/model. | `G3-A` |
| **Workspace settings/dashboard** | Durable per-source settings, dashboard and health. | No Workspace settings, activity dashboard or source-health surface exists. | `G3-B` |
| **Evidence envelope** | Claim/message references, local validation, bounded follow-up retrieval, coverage. | `HistorySlice`/`HistoryMessage` expose message ids and first/last range; no claim-to-message citation contract or evidence ledger exists. | `G4-A` |
| **Active evidence run** | Restart-safe Cited Summary, Explorer and follow-ups under an explicit TTL. | `HistorySlice`/`HistoryMessage` expose ids and dates; **G4-A/F1 adds `backend/evidence/` and an unapplied migration**, while no `workspace` model exists. | `G4-A` / F1 |
| **Evidence archive** | Owner-approved indefinite retention of selected sources as immutable revisions. | G4-A/F1 provides the archive service and unapplied tables. | `G4-A` foundation, owner-gated |
| **Workspace glossary** | Persistent, bounded, owner-editable terminology in the **real** Workspace scope. | No Workspace and no glossary table/model exist; G2-A owns the language contract. | `G4-C`, **blocked on G3-B** |
| **Durable evidence index** | Workspace-aware, cross-session/cross-run evidence browsing. | Occurrence `result_metadata`/`error_metadata` are bounded diagnostics, not evidence; no Workspace model exists. | `G4-B` (original meaning), on the G4-A contract |
| **Selection** | Exact, expiring, per-owner/per-feature selection for bulk actions. | `selection_state.py` now has owner + feature scopes (Stash default, Taskloom second), capped at 40 per scope, expiring after 900s; `retrieve.py` supplies the row/check/confirmation pattern and Taskloom adds CAS-revalidated bulk actions. | `G1-A` |
| **Task authority** | One scheduler, one coordinator, one tool executor, CAS lifecycle. | `TaskScheduler`, `TaskExecutionCoordinator` and `ToolExecutor` exist; there is no separate `TaskExecutor` class. `TaskManagementService` exposes list/pause/resume/complete/fail/expire/delete, not cancel/retry. | `G5-A` |
| **Activity data** | Bounded message/activity metrics and coverage. | No chat-activity metrics exist; `database_service` reports saves/provider stats only. | `G7-A` |
| **Cross-source/export** | Explicit multi-source scope, attribution and portability. | No cross-source retrieval or export endpoint exists. | `G6-A` |
| **Semantic index** | Embeddings/vector retrieval and similar-discussion search. | No embedding provider, index, vector store or semantic search exists. | `G6-B`, owner-gated |

### 5.4 Source-verified dependency findings

1. **Workspace is absent, not merely undocumented.** No `Workspace` model,
   table, service or handler was found. `history_schedules` is a per-peer
   cumulative checkpoint, not a Workspace: it has one row per `(owner_id,
   peer_id)`, no general settings, no activity data and no task scope. A
   Workspace may consume it, but must not duplicate its cursor/CAS authority.
2. **Language cannot be delivered at Workspace scope yet.** `G2-A` can correctly
   fix global/task/manual-History behavior using existing settings, Taskloom and
   History services. `G2-B` needs a Workspace settings home. Implementing
   `G2-B` inside `G2-A` would either invent a Workspace schema or create a
   second per-source language store.
3. **Task Center can be split at a real boundary.** `G5-A` can use
   `TaskManagementService`, `TaskRepository.list_occurrences`, existing
   Taskloom panels and existing CAS transitions. `G5-B` needs `WS-001/WS-003`
   and `G2-B` for source scope and inherited language. `G5-A` must not claim
   Workspace scope.
4. **Task cancellation/retry is not implementation-ready.** The scheduler has
   bounded retry and a `cancelled` occurrence status, but no owner-facing
   service path to cancel a running occurrence or retry a selected failed
   occurrence. `TASK-002` can deliver the existing pause/resume/complete/delete
   controls now; cancel/retry remains a documented deferred part until the
   coordinator contract is proven.
5. **Task output inspection is partially available.** Occurrence records carry
   `status`, `attempt`, `error_metadata`, `result_metadata` and timestamps, and
   `Taskloom` already lists recent occurrences. It does not display output or
   error detail. `G5-A` can add bounded inspection without a new store.
6. **Evidence must start single-source, but it is no longer ephemeral-only.**
   History returns a bounded `HistorySlice` with message ids, and summaries
   return first/last id metadata, but no claim-to-message citations. The owner
   approved durable Active Runs (TTL), an owner-approved indefinite Archive
   built from immutable revisions, and a persistent Glossary. `G4-A` therefore
   establishes storage and the envelope on one source without a Workspace;
   restart-safe follow-ups are its second phase (G4-A/F2). `G4-B` keeps its
   original Workspace-aware meaning and must not create a second evidence
   format.
7. **UI selection does not depend on language.** Stash already proves row
   toggle, visible check, pagination survival, cap/expiry and confirmation.
   `G1-A` can establish the reusable interaction contract without `G2-A`.
   `G1-B` visual polish also has no language prerequisite.
8. **Scheduled History remains a validation gate.** The durable repository,
   wizard, registered action and durable-only semantics are implemented, but
   `ROADMAP.md` still records Phase 6 (shared admission) and Phase 7 (live
   validation) as deferred. Offline tests are not live validation. Any
   recommendation that changes schedules must wait for the Phase 7 path it
   depends on.
9. **G6 and G7 are siblings, not a chain.** Both need `G3-A` and `G4-A`.
   `G7-A` metrics do not need cross-source synthesis; `G6-A` cross-source
   analysis does not need schedule recommendations. `G7-B` has the stronger
   external gate (Phase 7); `G6-B` has a separate owner gate (index/store).
10. **LANG-005 was misplaced.** The extraction glossary cannot be delivered by
    the base language contract because it only has meaning alongside structured
    extraction. It is assigned to `G4-C`, after `INTEL-010/011` and after the
    approved persistent-glossary store, not to `G2-A`.
11. **Evidence persistence is owner-decided; other persistence is not.**
    Temporary runs, an indefinite owner-approved archive of immutable revisions
    and a persistent glossary are approved **in principle**, with TTL/quotas
    and the deletion policy still open (§4.5.1, §10). Workspace settings,
    semantic indexes and broader analytics caching remain unapproved. This
    document still proposes no SQL, migration or provider object.
12. **No second runtime path is justified.** Every stage reuses
    `RuntimeSupervisor`, Dispatcher/ProviderManager, `ToolRegistry`/
    `ToolExecutor`, `TaskScheduler`/`TaskExecutionCoordinator`, existing
    services and the Telegram API boundary.
13. **The generic tool path cannot carry evidence.** `ToolExecutor` persists
    `arguments` and `result_data` (`backend/ai/tools/executor.py:380`, `:501`,
    `:504`), the dispatcher puts the full `data` into provider continuation
    (`dispatcher.py:2341`), tool messages enter `ai_messages`
    (`dispatcher.py:975` → `persistence.py:167-171`), and `ai_tool_history`
    stores arguments plus the result message (`persistence.py:359-366`). An
    Evidence Envelope therefore needs a dedicated store and must never travel
    as `ToolResult.data`/`message`.
14. **A Workspace still does not exist, and no substitute is permitted.**
    No `workspace` table, model, service or handler is present in `backend/`.
    The Glossary is approved as persistent, but it must use the real Workspace
    identity/ownership from **G3-B**; an owner-level key, temporary table or
    parallel scope is explicitly prohibited. Glossary persistence, UI and
    injection stay blocked until G3-B is complete and its authorization model
    approved. G4-A evidence itself remains scoped to the owner plus the
    Telegram source.
15. **Strict JSON has no existing precedent in the History path.**
    `history_ai_service._call` uses `manager.chat(messages, tools=[])` with no
    `response_format` (`history_ai_service.py:646-686`), so the JSON contract is
    new work with its own validation and failure states — never an assumed
    provider behavior.
16. **Archive retention and storage cost are separate decisions.** Indefinite
    retention is approved; unlimited consumption is not. Quotas must refuse new
    archival honestly and must never evict previously archived evidence.
17. **Archive edits are history, not corrections.** A Telegram edit must append
    a new owner-approved revision. Overwriting, merging or silently refreshing a
    stored snapshot would invalidate every earlier analysis that used it, so
    references identify an immutable revision rather than "latest".
18. **G4-B is an existing identity.** Its original meaning (Workspace-aware
    durable evidence) is preserved; follow-ups are G4-A/F2, and the new G4-C/
    G4-D IDs are explicitly documented rather than reusing G4-B.
19. **Archive deletion is not global text deletion.** Archived source identity,
    immutable revision text, active-run excerpt copies and derived Workspace
    Knowledge are distinct classes. The approved default deletes only the
    selected archive rows, blocks on derived dependents until a separate
    explicit cascade confirmation, and leaves active-run excerpts until TTL. No
    purge is approved or specified; it may be considered separately.
20. **Expiry is logical before physical.** Every Active Run becomes unusable
    when authoritative database time passes `expires_at`, and is never
    reconstructed. Approved Option 1 permits physical deletion only
    opportunistically inside an existing authorized operation; no scheduler or
    tombstone is approved, and never-read expired rows may persist
    indefinitely. See §4.5.1.

### 5.5 Stage prerequisites, gates and independent delivery

| Stage | Technical prerequisites | Product prerequisites | Acceptance validation | Validation gate / owner decision | Independently deliverable | Deferred within the stage |
|---|---|---|---|---|---|---|
| **G1-A** | None; existing Glass UI and Stash | None | Toggle/deselect/check, stale callback, pagination, confirmation | Owner direction on ordering/compactness | Yes | Visual identity, non-Stash polish |
| **G1-B** | G1-A | None | Readability, touch targets, preserved callbacks/Appearance | Owner visual direction (optional) | After G1-A | None |
| **G2-A** | Existing settings, Taskloom, History | G1-A optional for selector rendering | Precedence tests; Persian/English real output | Owner approval: precedence and global-setting semantics | Yes | Workspace language |
| **G2-B** | G2-A + G3-B | G3-A | Effective-language tests across scopes | Same precedence decision | No | None |
| **G3-A** | Existing resolver/dialogs + G1-A selection | None | Resolution/access, empty/failure, scope isolation | Owner approval for durable source registry if added | Yes, with derived read model | SOURCE-003 invite links |
| **G3-B** | G3-A | G1-A | Settings inheritance, dashboard honesty, health failure states | Owner approval: Workspace persistence/schema | No | Durable features not approved |
| **G4-A / F1** | G2-A + existing History + approved evidence storage | None (single source) | Invented ids/keys, insufficient evidence, expired-run honesty, owner isolation, immutable revisions | **Implemented offline; owner must apply the migration manually** | Yes | Follow-ups, extraction, glossary, assessments |
| **G4-A / F2** | G4-A / F1 | None | Restart-safe follow-up, stale/expired run, source isolation | Approved TTL proven usable in practice | After F1 | A second retrieval engine |
| **G4-B** *(original)* | G4-A + G3-A + G3-B | G3-A | Workspace-scoped evidence, attribution, cross-run isolation, deletion honesty | Owner approval: Workspace persistence + evidence retention scope | No | Whole stage until G3-B and its approval exist |
| **G4-C** *(new)* | G4-A + G3-B + approved extraction schema | G3-B | Malformed JSON rejection, Glossary conflict/bounds, prompt growth | G3-B complete; extraction schema approved; glossary quotas defined after G3-B | No | Glossary until G3-B; tagged-text fallback unless separately approved |
| **G4-D** *(new)* | G4-C + approved thresholds/policies | Owner policy | Insufficient-evidence refusal, no definitive person label | Thresholds and person-level policy approved (never invented) | No | Whole stage until policy exists |
| **G5-A** | G1-A + existing Taskloom/management | G2-A optional for language display | CAS, stale version, unsupported actions, bounded output | Owner approval: extend Taskloom vs new entry point | Yes | Cancel/retry, Workspace scope, inherited language |
| **G5-B** | G5-A + G3-B + G2-B | G3-A | Workspace scope isolation, inherited effective language | Owner approval: Workspace persistence | No | None |
| **G6-A** | G3-A + G4-A + G2-A | G3-B for the portable bundle | Attribution, per-source bounds, gaps, CSV escaping | Owner approval: export destination and notebook persistence | After prerequisites | EXPORT-002 until G3-B; semantic search |
| **G6-B** | G6-A + approved index/store | None | Relevance, empty/unsupported state, index deletion | Owner approval: embedding provider, retention, cost | No | Whole stage if not approved |
| **G7-A** | G3-A + G4-A | G3-B optional | Timezone, empty/partial data, coverage wording | Owner approval: sampling window/retention | Yes | Recommendations |
| **G7-B** | G7-A | G3-A (G3-B optional for Workspace context) | Recommendation explanation, owner-confirmed prefill | **Validation gate: Scheduled History Phase 7** | No | Whole stage until gate passes |
| **G8** | G3-A + G4-A + G4-C + G5-A + G6-A + G7-A | G3-B helpful, not required | Precision/refusal, privacy, no person judgments | Owner approval per capability | No | Any unapproved capability |

## 6. Implementation Groups

The eight parent groups are preserved for continuity. Where a parent contains
capabilities with different prerequisites, it is split into stable sub-stages.
An idea ID has one primary owner; shared IDs are explicitly marked, never
silently duplicated.

### G1 — Glass UI Interaction Foundation

#### G1-A — Reusable interaction grammar
- **Included:** UI-001, UI-002, UI-003, UI-004.
- **Status:** Implemented for Stash and Taskloom, with focused offline tests.
  Broader panel normalization, live Telegram validation and G1-B remain
  deferred.
- **Goal:** Make panels predictable and safely selectable using the proven Stash
  pattern, without waiting for language or Workspace work.
- **Shared foundation:** `panels.py`, `panel_registry.py`,
  `InlinePanelBuilder`, `selection_state.py`, `retrieve.py` confirmation and
  bulk-result patterns, `styles.py`/`symbols.py`.
- **Dependencies:** None technical. Owner direction on ordering/compactness.
- **Deliverables:** documented ordering rule; generalized row toggle with visible
  check and re-click deselect; contextual bulk actions; scope-specific
  confirmation; stale-callback and pagination tests.
- **Definition of Done:** Works on Stash and at least one new list; no second
  selection system; actions use verified ids/codes; no destructive bulk action
  without confirmation.
- **Risks:** Telegram keyboard limits and callback-data compatibility.
- **Why this stage:** Selection/confirmation is consumed by source search and
  Task Center and has no language dependency.

#### G1-B — Visual identity and accessibility
- **Included:** UI-005.
- **Goal:** Apply the requested compact, polished identity without redesigning
  the panel engine.
- **Dependencies:** G1-A. Owner visual direction helpful, not blocking a
  conservative first pass.
- **Deliverables:** coordinated colors/typography, larger touch targets,
  consistent spacing, preserved symbol registry and Appearance contract.
- **Definition of Done:** Readable on Telegram clients; callbacks unchanged;
  no per-panel renderer or image pipeline.
- **Why separate:** Visual polish has no dependency on language/Workspace and
  should not block functional stages.

### G2 — Language and Output Contract

#### G2-A — Global, request and task language
- **Status:** Implemented offline (2026-11-15). Global default, request-level
  selection, Taskloom task language, shared catalog/resolver, History summary
  language and effective-language reporting. Auto is summary-only. Unsupported
  explicit values are rejected before any provider call, and an invalid stored
  preference fails explicitly instead of becoming English; the prompt labels
  the global default separately from the effective request language. G2-B
  (Workspace) remains deferred; no schema change was made.
- **Included:** LANG-001, LANG-002, LANG-003, LANG-004.
- **Shared with later stage:** LANG-001/002 are extended by G2-B; their primary
  ownership stays here.
- **Goal:** Make global/task/manual-History output language explicit and
  testable using existing storage and task contracts.
- **Source boundary:** `panel_settings.language` (`en` default, currently not
  consumed by AI/History), `AIRequest.language` (defaults English; caller omits
  it), `summarize_history` (no language argument), `translate_history` (accepts
  one), Taskloom `en/fa/ar/zh` and `preparation_policy`.
- **Dependencies:** None technical. G1-A optional for selector rendering.
  Owner decision on precedence and whether the current global setting is
  migrated or replaced.
- **Deliverables:** validated catalog; explicit selector; global/task/request
  precedence; effective language in result metadata; summary language argument;
  auto-detect only as fallback.
- **Definition of Done:** Persian and English selectable; explicit choice wins;
  unsupported values rejected; UI language remains separate; no Workspace claim.
- **Deferred:** Workspace scope, durable per-source defaults, glossary.
- **Why split:** It is implementable now and prevents every later text feature
  from inventing another language path.

#### G2-B — Workspace language inheritance
- **Included:** No new IDs; extends LANG-001/002.
- **Goal:** Add the Workspace scope and one effective-language resolver after a
  Workspace settings home exists.
- **Dependencies:** G2-A + G3-B; G3-A for source identity. Same owner precedence
  decision.
- **Deliverables:** per-Workspace default language, inheritance display, and
  effective-language resolution for Workspace tasks.
- **Definition of Done:** Explicit request > Task > Workspace > global > default;
  the effective value is shown; no duplicate language store.
- **Why split:** A Workspace language cannot exist before a Workspace settings
  model; implementing it earlier would create a parallel store.

### G3 — Source and Workspace Foundation

#### G3-A — Source resolution and Workspace identity
- **Included:** WS-001, SOURCE-001, SOURCE-002; SOURCE-003 is investigation-only.
- **Goal:** Resolve an owner-accessible source and open a Workspace identity/read
  model without assuming a new schema.
- **Reuse:** `PeerType`, bounded dialog enumeration, `save_service` parser/
  resolver, `history_schedules`, `ai_tasks`, `saved_items.origin_chat_id`, and
  G1-A selection rows.
- **Dependencies:** G1-A. A derived read model can start without persistence;
  a durable source registry needs owner approval.
- **Deliverables:** search/resolve/add flow with access validation; one Workspace
  view showing source type, schedules, tasks, saved-item links and current
  evidence/coverage; honest unknown/inaccessible states.
- **Definition of Done:** No guessed identity, no auto-join, no second task or
  checkpoint store; invite links explicitly unsupported unless investigated.
- **Deferred:** SOURCE-003 invite links; durable settings (G3-B).

#### G3-B — Workspace settings, dashboard and health
- **Included:** WS-002, WS-003, WS-004; shares WS-001.
- **Goal:** Give the Workspace a durable settings/dashboard/health home once the
  owner approves the persistence model.
- **Dependencies:** G3-A; owner-approved persistence. It deliberately does NOT
  require G2-B: it creates the settings framework, and G2-B adds the language
  field through that same contract.
- **Deliverables:** validated settings with inheritance display, dashboard with
  windows/coverage, source health and affected-task links.
- **Definition of Done:** Settings are owner-scoped; dashboard numbers state
  their source/window; health is read-only and re-resolves access; G2-B can add
  language without a second settings system.
- **Why split:** Identity/read model is useful before persistence; durable
  settings and dashboards are a separate owner-gated change.

### G4 — Evidence-Grounded Intelligence

Corrected 2026-11-15. G4-B keeps its original identity (**Workspace-aware
durable Evidence**) and is not renamed. Restart-safe follow-ups are a G4-A
delivery phase, not a stage. G4-C and G4-D are new, explicitly documented IDs
for work that the original broad G4-A bundled once durable storage and a real
Workspace dependency split it. G4-A/F1 is implemented offline; its additive
migration artifact is not applied.

#### G4-A — Single-source evidence and interactive intelligence
- **Included:** EVID-001, EVID-002, EVID-003 (foundation), INTEL-001,
  INTEL-002, INTEL-003, INTEL-037.
- **Goal:** Establish one bounded, durable, single-source evidence contract and
  use it for opt-in Cited Summary, a basic read-only Explorer, and restart-safe
  follow-ups — without a Workspace.
- **Delivery phases (not separate stages):**
  - **G4-A / F1 — evidence foundation:** durable Active Evidence Runs with an
    explicit TTL; owner-approved archive with immutable revisions; opt-in Cited
    Summary; Explorer with locally constructed Telegram links. No follow-up
    tool, extraction, glossary or assessment.
  - **G4-A / F2 — restart-safe follow-ups:** answer from the same durable run
    after a restart, with citations and an honest insufficient-evidence state.
    Each follow-up is bounded to the approved 20 references.
- **Approved decisions recorded (2026-11-16):** working evidence is temporary
  with a default 7-day TTL (owner-selectable 1–30 days, fixed at creation);
  nothing is archived automatically; archive revisions are immutable and
  retained until manual owner deletion; an edited message creates a new
  owner-approved revision instead of overwriting; dependent archive records
  block deletion unless a separate explicit cascade is confirmed; archive
  deletion leaves active-run excerpts to their own TTL; approved limits are
  1,000-character excerpts, 50 refs per run, 20 refs per follow-up, 32 KiB
  normalized text per run, 20 active runs per owner, and 5,000 records or
  50 MiB archived per owner (whichever is first, with rejection rather than
  eviction); expired runs use approved Option 1 opportunistic physical deletion
  with no scheduler, tombstone or fixed deadline; ordinary summaries stay plain;
  Scheduled History stays body-only and citation-free; the Evidence Envelope
  never enters generic tool/audit/conversation history; provider output is
  never authoritative for ids, counts, links or existence. Full policy:
  §4.5.1.
- **User Agency First (§2):** the owner chooses the run TTL at creation, whether
  to cite, whether to archive a suggested candidate/revision, and which
  evidence to inspect.
- **Reuse:** `history_service`, `HistoryMessage` ids, `HistorySlice` range,
  `ToolRegistry`/`ToolExecutor`, the Telegram message facade, G2-A language.
- **Dependencies:** G2-A + existing History. The storage model, TTL, limits,
  archive quotas, deletion rules and expired-run physical policy were approved
  on 2026-11-16 (§4.5.1). **G4-A/F1 is implemented offline; the additive
  migration artifact is not applied and Supabase schema changes remain manual.**
  No Workspace is required.
- **Deliverables:** conceptual `ai_evidence_runs`/`_run_refs`,
  `ai_evidence_sources` and immutable `ai_evidence_revisions`; logical TTL
  expiry against database time and approved Option 1 opportunistic physical
  deletion (no scheduler, tombstone or deadline); owner isolation; bounded
  excerpts with hashes; owner-confirmed archival and revision append; explicit
  deletion with dependent-record blocking, separate cascade confirmation and
  active-run excerpt disclosure (no purge approved); Cited Summary; Explorer
  with local links; restart-safe follow-ups in F2.
- **Definition of Done:** invented ids/keys rejected; no cross-owner or
  cross-run read; expired runs fail honestly and are never reconstructed;
  logical expiry is not presented as physical deletion; no media binaries or
  full chats; no evidence in tool/audit/conversation history; archived text
  never overwritten or auto-deleted; manual archive deletion reports dependent
  and active-run excerpt consequences honestly and requires a separate explicit
  cascade where dependents exist; no purge is implied; no extraction, glossary
  or assessment.
- **Deferred:** Workspace-aware browsing (G4-B), extraction/glossary (G4-C),
  assessments (G4-D).
- **Why it stays G4-A:** it is the same single-source trust foundation; only
  its delivery is split so follow-ups do not consume the G4-B identity.

#### G4-B — Workspace-aware durable evidence (original meaning preserved)
- **Included:** No new IDs; consumes EVID-001/002/003 and INTEL-037.
- **Goal:** Make evidence durable and browsable **by Workspace** once the real
  Workspace model exists, using the SAME evidence contract.
- **Dependencies:** G4-A + G3-A + G3-B; owner approval of Workspace persistence
  and evidence-retention scope.
- **Deliverables:** Workspace-scoped evidence navigation, cross-run
  inspectability inside the Workspace, and Workspace attribution — using
  immutable revision references and no second evidence format.
- **Definition of Done:** no second format; deletion/expiry honest; source and
  Workspace isolation hold.
- **Deferred:** Whole stage until G3-B and its approval exist; it is not
  restart-safe follow-ups, which belong to G4-A/F2.

#### G4-C — Strict-JSON extraction and persistent Workspace Glossary (new ID)
- **Included:** INTEL-010, INTEL-011, LANG-005.
- **Why a new ID:** the original G4-A bundled extraction and the glossary. The
  glossary now depends on the real Workspace (G3-B), so it cannot be delivered
  with the single-source foundation; a separate stage keeps that dependency
  explicit. This does not rename or replace G4-B.
- **Goal:** Extract structured, evidence-backed records under a locally
  validated strict-JSON contract, and let the owner maintain the bounded,
  persistent Workspace Glossary.
- **Dependencies:** G4-A; **G3-B is mandatory for the Glossary** (identity,
  ownership and authorization); owner-approved extraction schema. Glossary
  quotas and implementation are deferred until G3-B; no glossary limit is
  approved.
- **Deliverables:** JSON schema + validator with explicit rejection; typed
  records referencing immutable revision ids; glossary add/edit/delete with
  conflict handling and quotas defined after G3-B; no automatic learning.
- **Definition of Done:** malformed/unsupported JSON rejected; the Glossary
  never uses a substitute Workspace or enters general memory/Scheduled History;
  G2-A language precedence unchanged; prompt size bounded.
- **Deferred:** tagged-text fallback (separate future contract); cross-source
  extraction.

#### G4-D — Descriptive and threshold-gated subjective assessments (new ID)
- **Included:** INTEL-012.
- **Why a new ID:** the original G4-A bundled subjective rigor; it must not be
  implemented before extraction exists and thresholds are approved.
- **Goal:** Separate observed statements, repeated patterns, evidence counts,
  conflicting opinions, missing perspectives and confidence, and refuse a
  definitive judgment from insufficient evidence.
- **Dependencies:** G4-C; **explicitly approved** thresholds, assessment policy
  and person-level policy.
- **Deliverables:** descriptive assessment mode; refusal on insufficient
  evidence; no numeric threshold or default without approval.
- **Definition of Done:** no provider-assigned "good/bad" person label;
  conflicting evidence shown; confidence tied to the approved policy; refusal
  is a valid, reported outcome.
- **Deferred:** whole stage until the policy exists.

### G5 — Unified Task Center

#### G5-A — Existing-scope Task Center
- **Included:** TASK-001, TASK-002, TASK-003, TASK-004, TASK-005.
- **Shared with later stage:** TASK-004 is extended by G5-B; primary ownership
  stays here.
- **Goal:** Extend Taskloom into a unified console over existing task authority.
- **Reuse:** `Taskloom`, `TaskManagementService.list_tasks/inspect/counts`,
  `TaskRepository.list_occurrences`, occurrence status/attempt/timestamps,
  `task_trace.py`, CAS pause/resume/complete/delete.
- **Dependencies:** G1-A; G2-A optional for consistent language display. No G3.
- **Deliverables:** unified list/status views; bounded output/error inspection;
  pause/resume/complete/delete; effective task-language display; bounded
  operational timeline.
- **Definition of Done:** Owner-only; CAS-safe; unsupported actions absent; no
  direct DB access in handlers; no second executor; Workspace scope not claimed.
- **Deferred:** cancel/retry of a running occurrence (no service path yet);
  Workspace scope and inherited language (G5-B).
- **Why split:** The existing task system can deliver a useful console now; only
  source scope and inherited language need Workspace.

#### G5-B — Workspace task scope and inherited language
- **Included:** No new IDs; extends TASK-001/TASK-004.
- **Goal:** Scope tasks to a Workspace and show/apply effective language.
- **Dependencies:** G5-A + G3-B + G2-B.
- **Deliverables:** Workspace task list/filter, Workspace-scoped creation and
  display of inherited/effective language.
- **Definition of Done:** No cross-Workspace access; explicit task choice wins;
  source scope cannot be overridden by a model argument.
- **Why split:** It is impossible before the Workspace settings and language
  resolver exist.

### G6 — Cross-Source Research and Portability

#### G6-A — Attributed cross-source query and export
- **Included:** INTEL-020, INTEL-022, EXPORT-001, EXPORT-002, INTEL-040.
- **Goal:** Query explicitly selected Workspaces with per-source evidence and
  export the result.
- **Dependencies:** G3-A + G4-A + G2-A; G3-B required for the portable Workspace
  bundle and notebook persistence. Owner decision on export destination.
- **Deliverables:** cross-source query with source labels/gaps; citation-preserving
  aggregation; JSON/CSV/Markdown export; portable bundle; research notebook.
- **Definition of Done:** No source silently omitted; attribution retained;
  export schema includes source/coverage; CSV escaped; no unauthenticated
  download.
- **Deferred:** semantic/similar search (G6-B).
- **Why split:** Cross-source/export does not need an embedding index. Semantic
  search does, so it must not hold the whole group hostage.

#### G6-B — Semantic search and similar discussions
- **Included:** SEARCH-001, SEARCH-002.
- **Goal:** Add semantic retrieval only after an approved index/store decision.
- **Dependencies:** G6-A + owner-approved embedding provider/index/store.
- **Deliverables:** bounded semantic index, semantic search and similar-discussion
  discovery with source/evidence and index deletion.
- **Definition of Done:** Honest empty/unsupported state; owner can remove the
  index; no upload of unrestricted history.
- **Deferred:** Whole stage if the owner does not approve content retention/cost.
- **Why split:** It introduces a new data store and provider capability that no
  other G6 deliverable requires.

### G7 — Activity Analytics and Schedule Recommendations

#### G7-A — Metrics, comparison and coverage
- **Included:** ANALYTICS-001, ANALYTICS-004, ANALYTICS-005.
- **Goal:** Read-only activity metrics and coverage on top of Workspace identity.
- **Dependencies:** G3-A + G4-A; G3-B optional for dashboard placement. Owner
  decision on sampling window/retention.
- **Deliverables:** timezone-aware messages/hour/day, peak/active periods,
  weekday/weekend comparison, source comparison with coverage caveats, and a
  per-run coverage report that consumes G4-A's coverage metadata.
- **Definition of Done:** Every metric states window/coverage; no unbounded
  history sweep; no schedule mutation.
- **Deferred:** Recommendations (G7-B).
- **Why split:** Metrics are read-only and do not need Scheduled History Phase 7.

#### G7-B — Owner-controlled recommendations
- **Included:** ANALYTICS-002, ANALYTICS-003.
- **Goal:** Recommend timing and let the owner prefill the existing wizard.
- **Dependencies:** G7-A; G3-A for the target source. G3-B is a product
  dependency for Workspace context, not a technical prerequisite.
  **Validation gate: Scheduled History Phase 7 live validation must pass before
  recommendations are enabled.**
- **Deliverables:** explained recommendation; explicit "use this time" that opens
  the existing owner-only wizard; no auto-create/change.
- **Definition of Done:** Owner confirms; existing creation boundary executes;
  no recommendation is presented as validated before Phase 7.
- **Why split:** The recommendation is only as trustworthy as the unfinished
  Scheduled History validation it depends on.

### G8 — Longitudinal and Advanced Intelligence

- **Included:** INTEL-030, INTEL-031, INTEL-032, INTEL-033, INTEL-034, INTEL-035,
  INTEL-036, SEARCH-003.
- **Goal:** Explore evidence-derived longitudinal capabilities only after the
  evidence, Workspace and activity foundations are trustworthy.
- **Dependencies:** G3-A, G4-A, G4-C, G5-A, G6-A, G7-A; G3-B is helpful but not
  mandatory for a first per-source evaluation. Each capability needs owner
  approval.
- **Suggested order:** entity extraction → decisions/promises → topic
  evolution/contradiction → change detection → graph → watchlists.
- **Definition of Done:** Precise evidence contract, confidence display, owner
  confirmation, explicit insufficient-evidence state, no automated judgment
  about people, no always-on global scan.
- **Risks:** False positives, privacy, entity ambiguity, cost and retention.
- **Why not split now:** All members are exploratory and individually gated;
  splitting them would imply a precision the source does not support.

## 7. Priority Model

Product priority, implementation priority and readiness are separate. Readiness
names the strongest thing standing between the stage and implementation.

| Stage | Product priority | Implementation priority | Readiness | Blocking decision / validation gate | Rationale |
|---|---|---|---|---|---|
| **G1-A** | P1 | **P0** | Ready with owner direction | Owner ordering/compactness decision | Selection/confirmation is reused by source search and Task Center; no language/Workspace dependency. |
| **G1-B** | P1 | P2 | Ready after G1-A | Optional visual direction | Polish matters but does not block data features. |
| **G2-A** | **P0** | **P0** | Owner-gated | Precedence and global-setting semantics | Small, shared, and currently inconsistent; fixes output for every later text feature. |
| **G2-B** | **P0** | P1 | Blocked by G3-B | Workspace persistence + precedence | Cannot exist before a Workspace settings home. |
| **G3-A** | P1 | P1 | Ready with G1-A | Durable registry persistence if added | Identity/read model unlocks analytics and cross-source work; can start derived. |
| **G3-B** | P1 | P1 | Owner-gated | Workspace persistence/schema | Dashboard/settings are valuable but require the persistence decision. |
| **G4-A / F1** | **P0** | P1 | **Implemented offline; migration unapplied** | Owner manually applies the additive migration | Trust foundation; single-source scope avoids waiting for Workspace. |
| **G4-A / F2** | **P0** | P1 | Blocked by F1 | Approved TTL must prove follow-up usefulness | Restart-safe follow-ups on the same contract; not a new stage. |
| **G4-B** *(original)* | **P0** | P2 | **Blocked by G3-B** | Workspace persistence + retention scope | Workspace-aware browsing, not a second evidence contract. |
| **G4-C** *(new)* | P1 | P2 | **Blocked by G3-B** (Glossary) | Extraction schema; glossary quotas defined after G3-B | Extraction can be designed first, but glossary storage may not start. |
| **G4-D** *(new)* | P1 | P3 | **Policy-gated** | Explicit thresholds + person-level policy | No number may be invented; refusal is a valid outcome. |
| **G5-A** | P1 | **P0/P1** | Owner-gated after G1-A | Task Center shape; cancel/retry investigation | High value, mostly existing machinery, no Workspace needed. |
| **G5-B** | P1 | P2 | Blocked by G5-A/G3-B/G2-B | Workspace persistence | Only the Workspace-scoped part waits. |
| **G6-A** | P2 | P2 | Blocked by G3-A/G4-A | Export destination; notebook persistence | Depends on explicit source scope and evidence; no vector store needed. |
| **G6-B** | P2 | P3 | Owner-gated | Embedding/index/store/cost approval | New store and provider capability; can be withheld indefinitely. |
| **G7-A** | P2 | P2 | Blocked by G3-A/G4-A | Sampling/retention decision | Read-only metrics; no Phase 7 gate. |
| **G7-B** | P2 | P3 | **Validation-gated** | **Scheduled History Phase 7** | Recommendations must not outrun real schedule validation; G3-A supplies the source, while G3-B only adds Workspace context. |
| **G8** | P3 | P3 | Owner-gated | Advanced capability approval | High false-positive/privacy risk; depends on evidence, cross-source context and activity. |

### Why the priority table changed

- **G2 is no longer one priority.** `G2-A` is a P0 implementation prerequisite;
  `G2-B` is P1 because Workspace must exist first.
- **G5 is no longer uniformly P1.** `G5-A` is an early P0/P1 console;
  `G5-B` is P2 because it waits for Workspace and language inheritance.
- **G4 keeps one contract, one preserved identity and two new IDs.** `G4-A`
  (storage + Cited Summary + Explorer, then follow-ups) is the single-source
  foundation; `G4-B` remains Workspace-aware durable evidence and waits for
  G3-B; `G4-C` extraction/glossary is **blocked on G3-B for the glossary**;
  `G4-D` is policy-gated. Durability is no longer a later stage.
- **G6/G7 are no longer a single ordering.** `G7-A` can precede `G6-A`;
  `G7-B` and `G6-B` are separately gated. The previous plan put all of G7 before
  all of G6 while ignoring that only the recommendation half needs Phase 7.
- **G3 is no longer one prerequisite gate.** Identity/read model and durable
  settings are separate; treating them as one made Workspace look both required
  and impossible to start.
- **G1 visual work is separated from interaction work.** The original sequence
  blocked all UI on language result labels; source shows selection is
  independent.

## 8. Proposed Implementation Sequence

This is a proposal for owner review, not permission to start. Stages marked
"parallelizable" may be developed concurrently only if their prerequisites are
complete; the numbering is the recommended order, not a claim that later work
cannot start.

### Step 1 — G1-A Reusable interaction grammar
- **Prerequisites:** Owner direction on ordering/compactness.
- **Implement:** ordering rule, generalized row toggle/check, contextual bulk
  actions, scope-specific confirmation.
- **Out of scope:** language selectors, Workspace dashboards, visual overhaul.
- **Deliverables:** documented standard; Stash plus one new list; tests for
  toggle, stale callback, pagination, confirmation.
- **Validation:** offline UI tests and owner panel review.
- **Gate:** owner approves ordering/compactness.

### Step 2 — G2-A Global, request and task language
- **Prerequisites:** owner precedence decision; G1-A optional.
- **Implement:** catalog, selector, global/task/request precedence, summary
  language argument, effective-language metadata, auto fallback.
- **Out of scope:** Workspace scope, glossary, durable per-source defaults.
- **Deliverables:** one validated language contract; manual History and task
  creation use it; tests for precedence and unsupported values.
- **Validation:** real Persian and English summary output; UI chrome unchanged.
- **Gate:** owner confirms precedence and migration of the existing setting.

### Step 3 — G1-B Visual identity and accessibility
- **Prerequisites:** G1-A. **Parallelizable with G2-A.**
- **Implement:** colors/typography/spacing/touch targets within Telegram limits.
- **Out of scope:** new renderer, generated images, panel-engine rewrite.
- **Deliverables:** visual/accessibility pass over the G1-A surfaces.
- **Validation:** owner review; callback/Appearance regression checks.
- **Gate:** owner visual direction.

### Step 4 — G5-A Existing-scope Task Center
- **Prerequisites:** G1-A; G2-A optional for language display.
- **Implement:** unified list/status, bounded output/error inspection,
  pause/resume/complete/delete, effective task language, timeline.
- **Out of scope:** Workspace scope, inherited language, unverified cancel/retry.
- **Deliverables:** Taskloom extension over `TaskManagementService` and existing
  occurrences; no new store/executor.
- **Validation:** CAS/stale/unsupported-action tests; owner inspects a completed
  and a paused task.
- **Gate:** owner chooses extend Taskloom vs a new entry point (recommended:
  extend Taskloom); cancellation/retry requires a separate scheduler
  investigation before it is promised.

### Step 5 — G4-A / F1 evidence foundation (single source)
- **Prerequisites:** G2-A; existing P1 History; the evidence policy approved on
  2026-11-16 (§4.5.1); explicit owner authorization of the schema/migration.
- **Implement:** temporary Active Evidence Runs with the approved 7-day default
  TTL (owner-selected 1–30 days) and logical expiry against database time, plus
  approved Option 1 opportunistic deletion; archived sources with **immutable,
  owner-approved revisions** and approved 5,000-record/50 MiB quotas; opt-in
  Cited Summary; basic read-only Explorer with locally constructed Telegram
  links; owner-confirmed archival and deletion with dependent blocking and
  active-run retention disclosure.
- **Out of scope:** follow-up tools, extraction, glossary UI, assessments,
  Workspace browsing, scheduled citations.
- **Deliverables:** one bounded evidence contract with honest expiry, immutable
  revisions and owner isolation.
- **Validation:** invented-id/key rejection, expired-run honesty, owner/run
  isolation, excerpt/quota bounds, same-hash dedupe, edited-message revision
  append (previous revision unchanged), deletion/dependent behavior, and
  evidence absence from tool/audit/conversation history.
- **Gate:** the evidence policy is approved, but implementation may not start
  until the owner explicitly authorizes the schema/migration. No SQL, Supabase
  change or migration is authorized by the policy approval, and no purge is
  approved.

### Step 5b — G4-A / F2 restart-safe follow-ups
- **Prerequisites:** G4-A/F1; the approved TTL proven usable.
- **Implement:** a follow-up over the SAME durable run, bounded context and
  local citation keys; it survives a restart and fails honestly when the run
  expired or its source is unavailable.
- **Out of scope:** a new stage identity, a second retrieval engine, cross-source
  questions.
- **Gate:** F1 accepted; no new storage decision. The approved TTL must be
  usable in practice.

### Step 6 — G3-A Source resolution and Workspace identity
- **Prerequisites:** G1-A; existing resolver/dialogs.
- **Implement:** source search/resolve/add with access validation; Workspace
  identity/read model from existing state.
- **Out of scope:** durable settings, dashboard, invite links.
- **Deliverables:** owner-approved source opens a Workspace with honest state and
  links to existing schedules/tasks/saves.
- **Validation:** resolution/access, empty/failure and scope-isolation tests;
  owner checks one private chat and one group/channel if available.
- **Gate:** owner approves any durable source-registry persistence.

### Step 7 — G3-B Workspace settings, dashboard and health
- **Prerequisites:** G3-A; owner-approved persistence model.
- **Implement:** settings framework, dashboard, source health; no language field
  yet.
- **Out of scope:** Workspace language, analytics recommendations, task scope.
- **Deliverables:** validated owner-scoped settings, honest dashboard, read-only
  health.
- **Validation:** settings inheritance/scope tests; dashboard window/coverage
  checks; health failure states.
- **Gate:** owner approves schema/persistence. No SQL is executed by this plan.

### Step 8 — G2-B Workspace language inheritance
- **Prerequisites:** G2-A + G3-B.
- **Implement:** Workspace default language, inheritance display, effective
  resolver.
- **Out of scope:** new language store; UI-language change.
- **Deliverables:** explicit request > Task > Workspace > global > default, shown
  with results.
- **Validation:** precedence tests across all scopes.
- **Gate:** same precedence decision as G2-A.

### Step 9 — G5-B Workspace task scope and inherited language
- **Prerequisites:** G5-A + G3-B + G2-B.
- **Implement:** Workspace task filter/creation scope and effective-language
  display.
- **Out of scope:** cross-Workspace tasks; model-selected scope.
- **Deliverables:** Workspace-scoped tasks with deterministic provenance.
- **Validation:** scope isolation and inheritance tests.
- **Gate:** Workspace persistence already approved.

### Step 10 — G4-B Workspace-aware durable evidence (original meaning)
- **Prerequisites:** G4-A + G3-A + G3-B; owner approval of Workspace persistence
  and evidence-retention scope.
- **Implement:** Workspace-scoped evidence browsing and cross-run inspectability
  over the SAME contract and immutable revisions.
- **Out of scope:** a second evidence format, editor/overwrite of archived text,
  whole-chat archival, restart-safe follow-ups (G4-A/F2).
- **Deliverables:** Workspace evidence navigation with attribution and honest
  deletion/expiry behavior.
- **Validation:** Workspace and source isolation, revision traceability,
  deletion/dependent reporting.
- **Gate:** G3-B complete and approved; otherwise the stage stays closed.

### Step 10b — G4-C extraction and persistent Workspace Glossary
- **Prerequisites:** G4-A; **G3-B complete for the Glossary**; approved
  extraction schema and glossary limits.
- **Implement:** strict-JSON extraction with local validation; the persistent,
  owner-editable Workspace Glossary with explicit conflicts and caps.
- **Out of scope:** a substitute Workspace, tagged-text fallback, automatic
  glossary learning, cross-source extraction.
- **Gate:** G3-B authorization approved and schema/limits approved; Scheduled
  History untouched.

### Step 10c — G4-D threshold-gated assessments
- **Prerequisites:** G4-C; explicitly approved evidence thresholds and
  person-level policy.
- **Implement:** descriptive assessment with confidence, conflicts and refusal.
- **Out of scope:** any invented numeric threshold or definitive person label.
- **Gate:** policy approval only; the stage may stay closed indefinitely.

### Step 11 — G7-A Activity metrics, comparison and coverage
- **Prerequisites:** G3-A + G4-A; owner sampling/retention decision.
- **Implement:** bounded timezone-aware metrics, source comparison, coverage
  report.
- **Out of scope:** recommendations, schedule mutation, full-history totals.
- **Deliverables:** read-only metrics that state window/coverage.
- **Validation:** empty/partial/timezone/boundary tests.
- **Gate:** owner approves sampling/retention.

### Step 12 — G6-A Cross-source query and export
- **Prerequisites:** G3-A + G4-A + G2-A; G3-B for the portable bundle.
- **Implement:** explicit multi-source query, attributed aggregation, JSON/CSV/
  Markdown export, portable bundle, research notebook.
- **Out of scope:** semantic search, account-wide monitoring, unauthenticated
  downloads.
- **Deliverables:** attributed results and bounded exports.
- **Validation:** per-source attribution, gaps, CSV escaping, size/retention;
  owner runs one real multi-source question.
- **Gate:** owner approves export destination and notebook persistence.

### Step 13 — G7-B Owner-controlled recommendations
- **Prerequisites:** G7-A; G3-A; G3-B optional for Workspace context.
- **Implement:** explained timing recommendation and owner-confirmed prefill into
  the existing wizard.
- **Out of scope:** auto-create/change, recommendation-based task mutation.
- **Deliverables:** recommendation plus explicit owner action.
- **Validation:** owner compares one recommendation with observed activity;
  existing wizard path remains owner-only.
- **Gate:** **Scheduled History Phase 7 live validation must pass first.** Until
  then this stage remains disabled/deferred.

### Step 14 — G6-B Semantic search and similar discussions
- **Prerequisites:** G6-A; approved embedding provider/index/store.
- **Implement:** bounded index, semantic search, similar-discussion discovery,
  index deletion.
- **Out of scope:** unrestricted history upload; an unapproved vector store.
- **Deliverables:** attributed semantic results with an honest empty state.
- **Validation:** relevance/empty/index-deletion tests; owner-approved sample.
- **Gate:** owner approves provider, retention and cost. May be withheld.

### Step 15 — G8 Longitudinal and advanced intelligence
- **Prerequisites:** G3-A + G4-A + G4-C + G5-A + G6-A + G7-A; owner approval per capability.
- **Implement:** entity extraction, decisions/promises, topic evolution,
  contradiction/change detection, graph, watchlists, each behind evaluation.
- **Out of scope:** autonomous judgments about people, always-on global
  monitoring, speculative graph facts.
- **Deliverables:** evidence-backed candidate views with confidence/refusal.
- **Validation:** precision/refusal/privacy evaluation on owner-approved samples.
- **Gate:** owner approval; may be withheld indefinitely.

### Sequence notes

- **Why G7-A precedes G6-A:** `G7-A` is a smaller read-only consumer of
  Workspace identity and coverage; it validates activity sampling without
  cross-source synthesis. `G6-A` then builds attribution/export on the already
  tested evidence envelope. Neither requires the other.
- **Why G5-A precedes G3:** The existing Task Center can be improved without
  Workspace. Only `G5-B` waits.
- **Why G4-A precedes G3:** Single-source evidence is the shared trust contract;
  delaying it for Workspace would block the highest-value intelligence work.
- **Why G1-B is early but not blocking:** It is scheduled after `G1-A` and may
  run alongside `G2-A`; it does not gate data features.
- **No stage may enable `G7-B` before Phase 7,** and no stage may start `G6-B`
  without the owner's index decision.

## 9. Deferred and Exploratory Ideas

| Idea / capability | Blocker or unanswered question |
|---|---|
| **TASK-002 cancel/retry** | The service exposes pause/resume/complete/fail/expire/delete, not cancellation of a running occurrence or retry of a selected failed occurrence. The scheduler has a `cancelled` status and bounded retries, but no owner-facing path. Cancel/retry is deferred until the coordinator/claim boundary is proven; the rest of TASK-002 belongs to G5-A. |
| **G3-B durable Workspace settings** | No Workspace table/model exists. A derived identity/read model can start in G3-A, but durable settings/dashboard/health need owner-approved persistence. |
| **G4-B Workspace-aware durable evidence** | G4-A/F1 is implemented offline; Workspace-aware browsing requires G3-A/G3-B and the owner-applied migration. Archive quotas decide how long it remains useful. |
| **SOURCE-003 invite links** | Current parser does not support them; Telegram invite preview/join semantics and side effects are unverified. |
| **SEARCH-001 Semantic Search** | No embedding provider/index/store exists; content-retention and cost are owner decisions. Delivered only in G6-B. |
| **SEARCH-002 Similar Discussion** | Depends on semantic retrieval; relevance evaluation and index ownership are undefined. Delivered only in G6-B. |
| **ANALYTICS-002/003 Recommendations** | Depends on G7-A and on Scheduled History Phase 7 live validation. The recommendation/prefill stage remains disabled until that gate passes. |
| **INTEL-030 Knowledge Graph** | No entity/evidence foundation; high false-relation risk; graph storage decision missing. |
| **INTEL-031 Topic Evolution** | Requires reliable time-bucketed evidence and a baseline; missing-message bias unresolved. |
| **INTEL-032 Contradiction Detection** | Context and subjectivity can create false contradictions; needs an evidence/evaluation design. |
| **INTEL-033 Decision Tracker** | A proposal is not a decision; owner confirmation flow and schema are undefined. |
| **INTEL-034 Promise Tracker** | Sarcasm/hypotheticals, misattribution and privacy risks need policy. |
| **INTEL-035 Change Detection** | Edited/deleted messages are not re-readable without an archive; snapshots are incomplete. |
| **INTEL-036 Entity Extraction** | Entity resolution and duplicate merging need an evidence-backed policy. |
| **INTEL-040 Research Notebook** | Needs a persistence model and must not duplicate Saved Items. |
| **EXPORT-002 Portable Bundle** | Import semantics and sensitive-data boundaries are unresolved; the complete bundle also needs G3-B Workspace settings. |
| **Chat-only links** | Existing parser refuses them; extending it is a separate product decision. |
| **Automated per-chat digest** | P3 in the active History roadmap; Phase 6 admission and Phase 7 live checks remain deferred. |

LANG-005 is **not** deferred: it moves from the base language stage to G4-C,
where structured extraction exists and the persistent glossary can be injected.
Its persistence is **blocked on G3-B**, because no real Workspace model exists
yet and no substitute is permitted.
Workspace language is not deferred either; it is a distinct stage (G2-B) blocked
by G3-B.

## 10. Open Questions and Owner Decisions

Only decisions that genuinely need the owner are listed; source-inspectable
facts are not questions. Resolved evidence items keep their numbers for
traceability and are marked closed rather than deleted.

1. **Workspace persistence:** Should G3-A start with a derived read model and no
   new table, or should a durable source/Workspace registry be approved? If
   durable, should G3-B use a new owner-scoped model or extend existing rows?
   The owner controls any schema/migration.
2. **Language precedence:** Confirm the intended order (explicit request > Task >
   Workspace > global > default), whether the existing `panel_settings.language`
   value is migrated or replaced, and whether Glass UI language is a separate
   setting from generated-content language.
3. **Evidence retention (RESOLVED 2026-11-16):** temporary Active Runs with a
   7-day default TTL (owner range 1–30 days, fixed at creation) and Option 1
   opportunistic physical deletion; approved limits of 1,000-character
   excerpts, 50 refs per run, 20 refs per follow-up, 32 KiB normalized text per
   run and 20 active runs per owner; archive quotas of 5,000 records or 50 MiB
   per owner, with rejection rather than eviction; immutable archive revisions
   and manual owner deletion. The full policy is §4.5.1. Only the limit
   configuration surface (#18) remains open. The expired-run physical policy is
   resolved as #19.
4. **Cross-source scope:** May a query read only explicitly selected Workspaces,
   or does the owner want broader account-level discovery? The former is the
   safer proposed default.
5. **Activity data:** What sampling window/retention is acceptable for G7-A, and
   should metrics be cached durably or recomputed on demand?
6. **Task Center shape:** Extend Taskloom in place or introduce a new top-level
   navigation entry? The source already has Taskloom under AI.
7. **Cancel/retry:** Is owner-triggered cancellation/retry required for the first
   Task Center release, or may G5-A ship the existing pause/resume/complete/
   delete controls while cancel/retry remains deferred pending a coordinator
   contract?
8. **Export destination:** Telegram file delivery, dashboard download, or durable
   object storage? Each has different retention/security implications.
9. **Semantic index:** Approve or reject an embedding provider/index/store,
   including content retention, cost and deletion. G6-B cannot start without it.
10. **Advanced intelligence:** Which graph/tracker/contradiction features are
    worth the false-positive and privacy risk? Approval can be withheld
    indefinitely.
11. **UI scope:** Approve the ordering/selection rules and how much visual
    customization is worth pursuing within Telegram's panel constraints.
12. **Cited Summary surface (RESOLVED 2026-11-16):** both the History panel and
    an explicit AI-triggered request; chosen per request, never remembered per
    source and never the ordinary summary default.
13. **Archive suggestions (RESOLVED 2026-11-16):** only evidence explicitly
    selected/reviewed in the current Evidence workflow; maximum 10 per surface,
    each with a reason, and every archival needs owner confirmation.
14. **Glossary Workspace dependency:** the Glossary is **blocked until G3-B**
    establishes the real Workspace identity, ownership and authorization. No
    owner-level key or temporary Workspace is acceptable. Entry and character
    limits also need approval once that model exists.
15. **Person-level policy:** which thresholds, evidence counts and refusal rules
    are acceptable for G4-D, and whether any definitive person-level
    judgment is permitted at all.
16. **Tagged-text fallback:** whether to specify it at all, and if so, which
    failure classes may use it without weakening strict-JSON validation.
17. **Archive deletion and active-run retention (RESOLVED 2026-11-16):** at
    both revision and source level, dependent derived records block deletion by
    default; a separate explicit cascade confirmation with a clear impact
    explanation is required. Archive deletion does not delete active-run
    excerpts; they remain until their own TTL. No purge is approved or
    specified; a separate owner-requested purge may be considered later, but
    implementation must not invent it.
18. **Limit configuration surface (RESOLVED for F1 2026-11-16):** one
    centralized owner-level policy; no per-Workspace configuration or settings
    UI before G3-B. Workspace-specific overrides may be considered after G3-B,
    inheriting global defaults. TTL remains owner-selectable at creation.
19. **Expired-run physical policy (RESOLVED 2026-11-16):** approved Option 1,
    opportunistic physical deletion inside an existing authorized operation,
    with no tombstone, scheduler, worker or fixed deadline. Expired data may
    remain physically stored indefinitely if no later authorized operation
    touches it; logical expiry still denies all access. See §4.5.1.

## 11. Change Log

| Date | Change |
|---|---|
| 2026-11-14 | Created this document at `main`/`origin/main` `f61e12e`. Recorded the owner-provided UI, language, interactive summary, extraction, Workspace, analytics, source search, cross-source, Task Center and advanced intelligence ideas; added clearly labeled assistant suggestions; mapped existing History/Scheduled History/Taskloom/language/search capabilities; proposed eight implementation groups, separate product/implementation priorities, a dependency-aware sequence, deferred items and owner decisions. No implementation, schema, Supabase, Telegram, deployment or test change was made. |
| 2026-11-14 | Corrected dependency graph and sequence after source re-verification of the committed document (`63fdd5c`). Split G1, G2, G3, G4, G5, G6 and G7 into A/B sub-stages; moved G2-B after G3-B; moved G5-B after G3-B/G2-B; made G4-A single-source and G4-B Workspace-aware; separated G7-A metrics from Phase-7-gated G7-B recommendations; moved semantic search to owner-gated G6-B; reassigned LANG-005 to G4-A; recorded that task cancel/retry has no service path yet. No idea was removed, no runtime/test/schema/Supabase/Telegram/Render change was made. |
| 2026-11-15 | Recorded G1-A as implemented for Stash and Taskloom: documented panel ordering, owner/feature-scoped selection, visible toggles, Taskloom bulk CAS actions and destructive confirmation. G1-B, G2-A, G5-A beyond this narrow UI work, Workspace and Scheduled History Phase 6/7 remain deferred. No schema, Supabase, Telegram, Render or live-operation change was made. |
| 2026-11-15 | Added **User Agency First** as an owner-approved cross-cutting product principle in §2: meaningful owner choice over unnecessary assumptions, configurable behavior, safe predictable defaults, progressive disclosure, honest boundaries and no unnecessary paternalism. Added a concise G4-A reference naming the capabilities it should influence. G4-A owner decisions (citation defaults, evidence retention, follow-up lifetime, subjective-assessment thresholds/person judgments, glossary persistence/scope, deep-link privacy and structured-output failure behavior) remain unresolved. Documentation only; no code, test, config, schema, Supabase, Telegram, Render or deployment change. |
| 2026-11-15 | Recorded the owner-approved evidence-persistence direction: temporary Active Evidence Runs with an explicit TTL, restart-safe follow-ups, an owner-approved indefinite Evidence Archive with manual deletion, a persistent bounded Workspace Glossary, opt-in Cited Summary, locally built Telegram links, strict-JSON-only initial extraction and unchanged citation-free Scheduled History. Added EVID-002/003/004 and §4.5.1 (retention classes, conceptual objects, proposed TTL/quotas, growth estimates, provider isolation), revalidated the leak paths against `416918e`, and split G4 into four stages (G4-A storage/Explorer, G4-B follow-ups, G4-C extraction/glossary, G4-D assessments). No schema, SQL, Supabase, Telegram, deployment or code change; no quota or TTL is approved yet. |
| 2026-11-15 | Corrected the G4-A evidence design (documentation only). Archive snapshots are now immutable revisions: identical content is deduped by hash, an edit creates a new owner-approved revision instead of overwriting, and references identify a revision so earlier analyses stay traceable. Removed the temporary owner-scoped Workspace key; the persistent Glossary now explicitly depends on G3-B's real Workspace identity and authorization. Restored G4-B's original meaning (Workspace-aware durable evidence); restart-safe follow-ups are G4-A/F2, and G4-C/G4-D remain new, documented stages. Storage numbers are now separated into measured facts, assumptions, estimates and proposals, with every limit PENDING OWNER APPROVAL. No code, SQL, schema, Supabase, Telegram or deployment change. |
| 2026-11-15 | Reconciled archive deletion and active-run retention (documentation only). Revision- and source-level deletion now use the same proposed rule: dependent derived records block by default and require a separate explicit cascade confirmation; active-run refs/excerpts are TTL snapshots, not cascade dependents, and remain until expiry unless the owner separately chooses a purge. Clarified that permanent archive deletion is scoped, that source-level deletion may offer a separate purge, and that every deletion/purge rule remains PENDING OWNER APPROVAL. No code, SQL, schema, Supabase, Telegram, runtime or deployment change. |
| 2026-11-16 | Separated Active Evidence Run logical expiry from physical deletion (documentation only). Expiry is authoritative-database-time and immediately blocks retrieval, Explorer, Cited Summary and follow-ups; expired runs fail honestly and are never reconstructed from Telegram, archive, summaries or conversation. Physical deletion is not automatic, no cleanup scheduler or worker is approved, and never-read expired rows may remain indefinitely. Documented physical deletion, minimal non-content tombstone, and deferred deletion as pending owner Options 1–3, with outright deletion recommended but NOT approved. No code, SQL, schema, Supabase, Telegram, runtime or deployment change. |
| 2026-11-16 | Recorded the owner-approved G4-A evidence policies (documentation only). Approved Active Evidence Runs with a default 7-day TTL (owner range 1–30 days, selected at creation, database-time expiry, no silent extension), Option 1 opportunistic physical deletion with no scheduler/tombstone/fixed deadline, approved limits (1,000-character excerpt, 50 refs per run, 20 refs per follow-up, 32 KiB normalized text per run, 20 active runs per owner), archive quotas (5,000 records or 50 MiB per owner, rejection rather than eviction), immutable revisions, dependency-blocked archive deletion with a separate explicit cascade, retention of active-run snapshots until TTL with no purge specified, and Glossary quotas/implementation deferred until the real G3-B Workspace. Closed §10 decisions 3, 17 and 19; implementation and schema remain unauthorized. No code, test, SQL, Supabase, migration, Telegram, Render or deployment change. |
| 2026-11-16 | Implemented G4-A/F1 offline and closed the remaining F1 product decisions: one centralized owner-level policy, opt-in Cited Summary from both the History panel and an explicit AI tool (per request), archive suggestions limited to evidence reviewed in the current run (max 10, reason + confirmation), and no Workspace Glossary or assessment in F1. Added the owner-only Explorer, local links, strict-JSON failure behavior, focused tests and an additive migration artifact that has NOT been applied. `DATABASE_ARCHITECTURE.md` was not modified; no SQL, Supabase, Telegram, Render, live provider or deployment operation was performed. |
