# Implementation Contract — AI Foundation & Ghost Room

> **Status:** Authoritative contract for the next implementation phase.
> **Source of truth:** This document was produced from direct source inspection of
> the repository at commit `8f8aeab` (clean tree). Every claim below was verified
> against the cited file and function. If code later contradicts this document,
> update this document in the same commit as the code change.
>
> The existing architecture is authoritative. No new architecture is introduced
> except where the existing one cannot express a requirement (only Ghost Room is
> greenfield; everything else reuses existing components).

---

## 1. Current architecture (verified)

### AI execution path

```
owner message (outgoing)
  → ai_unified.py  @client.on(events.NewMessage(outgoing=True))   (L698)
      trigger / reply-to-AI activation → _execute_ai()
      → engine.execute(AIRequest)                                 (engine/engine.py:115)
        → Dispatcher.dispatch()                                   (engine/dispatcher.py:132)
            1 conversation runtime   (runtime/manager.py: get/create session,
                                      add_user_message → persistence.add_message)
            2 deterministic fast path(_try_local_fast_path) — no provider round
            3 prompt build:
                Dispatcher._build_context (dispatcher.py:1162)
                  → MemoryManager.retrieve_for_prompt(owner_id)      (memory/manager.py:76)
                  → ContextBuilder.build(..., memory=...)           (conversation/context_builder.py)
                  → PromptBuilder.build → [Memory] section          (prompt/builder.py:191–219)
            4 ProviderManager.chat(messages, tools=…)               (providers/manager/manager.py)
            5 empty-response retry → structured-action parse →
              action-recovery retry → tool loop (MAX_TOOL_ROUNDS)
            6 usage accumulation + normalization
            7 metrics.record + telemetry.record_execution           (engine/metrics.py, telemetry.py)
      → deliver_response edit-in-place (+ optional compact stats line)
```

### Memory system (three tiers)

- `MemoryManager` (`backend/ai/memory/manager.py`) owns `ShortMemory` (per-turn RAM),
  `LongMemory` (90-day), `PermanentMemory` (never expires).
- `Engine.__init__` (`engine/engine.py:96`): `self._memory_manager = memory_manager or MemoryManager()`
  — constructed with **no repositories**.
- Without a repository: `LongMemory.retrieve()` returns `[]` (`long.py:82–84`),
  `PermanentMemory.retrieve_all()`/`as_text()` return empty (`permanent.py:74–78, 116–119`).
- Persistence interfaces exist: `MemoryRepository` + `InMemoryMemoryRepository`
  (`database/memory_repository.py`). `RepositoryManager.memory` exposes only the
  in-memory implementation (`database/manager.py:52`) — confirmed by
  DATABASE_ARCHITECTURE.md §19.11 ("no Supabase implementations wired").
- Supabase helpers already exist but are **dead code**: `persistence.save_memory`,
  `query_memories`, `delete_expired_memories` (`backend/ai/persistence.py:167–246`)
  write/read `ai_memories`; nothing calls them (repository-wide grep verified).

### Token accounting

| Stage | Location | Behavior |
|---|---|---|
| Local estimate | `prompt/budget.py::estimate_tokens` | `len/4` chars (English), `len/2` (non-English); drives `TokenBudget.estimated_input_tokens` |
| History item estimate | `runtime/tokens.py::estimate_tokens` | `len/4`, used by `HistoryItem` |
| Provider extraction | `providers/openai_compat.py:156,202–205` (`usage.*`), `providers/gemini.py:176,188` (`usageMetadata.*`) | normalized into `ProviderResponse.usage{prompt,completion,total}` |
| Accumulation | `dispatcher.py` (~L437 init; L494–496 continuation) | sums initial + every tool-loop continuation round |
| Estimate fallback | `dispatcher.py:588–594` | ONLY when success AND all three counts are 0 |
| Source labeling | `dispatcher.py:598+` | `token_source = actual / estimated / unavailable` |
| Reconciliation | `dispatcher.py:596–598` | `total = prompt+completion` when missing/smaller |
| Telemetry record | `engine/telemetry.py::AIExecutionRecord` + bounded deque (200) | single source of truth for all UI |

**Confirmed minor divergences (source-inspected):**
1. Empty-response retry (`dispatcher.py`, "AI_PROVIDER_EMPTY_RESPONSE_RETRY" block)
   replaces `response` with the retry response *before* the `usage` dict is
   initialized from it — the discarded attempt's usage never enters the total.
2. Action-recovery retry ("AI_ACTION_RECOVERY_RETRY" block): when the recovery
   response becomes the final response, the original response's usage is replaced,
   not summed.

Both are small, bounded, and fixable at the exact replacement points.

### Provider/model layer

- Adapters share `ProviderResponse` (`providers/base/contract.py`: `usage`,
  `metadata`, `success`, `tool_calls`, …) and stamp the serving model into
  `metadata["model"]`.
- Rate limiting: `ProviderManager` classifies failures
  (`rate_limited/auth/model_not_found/server/network/request/blocked`),
  honors provider `Retry-After` via `_retry_after_seconds()`
  (`manager/manager.py:603–606`) with ONE bounded in-place retry for short
  windows, and records per-category cooldowns in `manager/health.py`.
- **No adapter exposes quota or reset-window metadata.** Only per-request
  `retry_after`. There is no quota API to consume — none may be invented.

### Database layer

- Live Supabase writes: `backend/db/client.py` (singleton, service-role),
  `backend/ai/config_store.py` → `ai_config`, `backend/ai/persistence.py` →
  `ai_sessions`, `ai_messages`, `ai_tool_history`.
- Repository layer (`backend/ai/database/*`) is **in-memory only** for all seven
  repositories (§19.11). `UsageRepository` (→ `ai_usage`) and
  `ProviderStatsRepository` (→ `ai_provider_stats`) exist as interfaces +
  in-memory fallbacks with **no migrations** (§19.8) and are unused by production.
- Settings: `panel_settings` typed columns + write-through cache
  (`services/settings_service.py`, `services/panel_settings_repository.py`);
  exposed read-only to the dashboard via `GET /api/settings`.
- Schema documentation: **DATABASE_ARCHITECTURE.md** (root) — 21 sections;
  §12 `ai_provider_stats` and §13 `ai_usage` full schemas; §20 migration status;
  §21 migration generation rules (**doc-first rule #9**: update the document
  before writing the migration; migrations must be idempotent, RLS SELECT-only,
  one logical change each, logged in §20).

### Fonts

Exactly one font definition exists: `src/index.css:30`
`font-family: 'Inter', 'SF Pro Display', -apple-system, system-ui, sans-serif;`
(hardcoded). `tailwind.config.js` defines no `fontFamily`. Telegram messages
cannot carry custom fonts — fonts are a dashboard-only concern today.

### AI UI (Telegram)

Panels registered in `handlers/ai.py::register` (L1354+):
`ai` (Overview), `ai_provider`, `ai_model`, `ai_wizard`, `ai_settings`,
`ai_settings_adv`, `ai_usage`, `ai_health`, `ai_details`, `ai_status`,
`ai_diagnostics`. Callbacks route on `data.startswith("panel:"/"action:")`
(`helper/panels.py:370–374`); nav is `panel:_nav:back/home`; panels edit their
own message in place (`render_edit`). Callback payloads must stay ≤ 64 bytes
(model picker uses `page:idx:hash8` scheme for this reason).

**Duplication confirmed:** `ai_status` (ai.py:764–796) re-renders identity
(provider/model/connected) plus lifetime counters from
`config_store.last_request_at/last_latency_ms` (documented as never persisted,
§19.2) and `EngineMetrics.snapshot()` — three data sources answering questions
Overview/Health/Details/Usage already answer from telemetry.

Per-message usage: the optional chat line uses `compact_telemetry_line(telemetry.last())`
(`ai_unified.py:579–586`) — correct only because delivery is synchronous with
the request. `ReplyResolver.register(telegram_msg_id, session_id, role, content,
provider, model)` (`ai_unified.py:623–632`; `context/reply_resolver.py`) maps
Telegram msg IDs to AI content but carries **no token/latency fields**.

### Ghost Room (current state)

There is **no implementation**. Only `GHOST_ROOM_ID` env placeholder
(`backend/config.py:41`, default empty) and a dormant warning check in
`backend/runtime/startup_check.py:231–236`. Private chats have no discovery,
list, unread state, pagination, or reply infrastructure. Incoming events ARE
delivered to handlers — `router.register_runtime_hooks()` registers an unfiltered
`events.NewMessage()` hook (health timestamps only) proving the mechanism works.
Pagination precedent exists in the two-column model selector; list browsing
precedent in `retrieve.py`.

### Tests baseline

Full suite: **635 passed, 0 failed, 1 warning** (at `8f8aeab`). Relevant modules:
`test_02_ai_flow`, `test_03_database_consistency`, `test_09_reply_to_ai`,
`test_13_model_selection`, `test_17_providers`, `test_25_fast_path`,
`test_33_ai_telemetry`, `test_34_ai_model_ui`, `test_35_ai_retry_ux`,
`test_36_ai_settings_ux`, `test_model_discovery`.

---

## 2–3. Exact files/modules/functions involved

| Concern | File → symbol |
|---|---|
| Memory retrieval hook | `backend/ai/engine/dispatcher.py` → `Dispatcher._build_context` (calls `retrieve_for_prompt`) |
| Memory manager | `backend/ai/memory/manager.py` → `MemoryManager`, `retrieve_for_prompt`, `store_long`, `store_permanent` |
| Tier impls | `backend/ai/memory/{long,permanent}.py` → `LongMemory.retrieve/as_text`, `PermanentMemory.retrieve_all/as_text/token_footprint` |
| Repo interface | `backend/ai/database/memory_repository.py` → `MemoryRepository`, `InMemoryMemoryRepository` |
| Repo wiring point | `backend/ai/database/manager.py` → `RepositoryManager.__init__`, `.memory` |
| Dead Supabase memory helpers | `backend/ai/persistence.py` → `save_memory`, `query_memories`, `delete_expired_memories` |
| Prompt rendering/bounding | `backend/ai/prompt/builder.py` → `_render_memory`, `_trim_to_budget`; `budget.py` → `DEFAULT_MAX_MEMORY_TOKENS=1000` |
| Token accumulation fixes | `dispatcher.py` — empty-response retry block; action-recovery retry block |
| Usage persistence hook | `dispatcher.py` — beside both `telemetry.record_execution` call sites (main result + `_fail`) |
| Usage repos | `backend/ai/database/{usage_repository,provider_stats_repository}.py` |
| Reset detection | `backend/ai/providers/manager/health.py` (cooldown state), `manager/manager.py::_retry_after_seconds` |
| Settings/font | `backend/services/settings_service.py` (+`_DEFAULTS`, validators, accessors), `panel_settings_repository.py`, `src/index.css:30`, `src/App.tsx` boot fetch of `/api/settings` |
| Web exposure | `backend/web/app.py::get_settings` (already returns all settings) |
| AI UI dedup | `handlers/ai.py` → `_ai_status_panel_handler/_inline_builder/_ai_status_refresh_action`, registration lines, entry buttons at L1140 & L983 |
| Per-message detail | `context/reply_resolver.py` → `ReplyResolver.register/resolve`, `ResolvedAIContent`; call site `ai_unified.py:623–632`; consumer `_ai_details_panel_handler` |
| Ghost Room (new) | `backend/bot/handlers/ghost_room.py` (new), `backend/services/ghost_room_service.py` (new), router entry `backend/bot/router.py::register_all` |
| Incoming recorder | same new handler module — own `events.NewMessage(incoming=True)` private-chat listener |
| DB management | `backend/services/database_service.py` → `do_stats` |

---

## 4. Current data flow (relevant slices)

- **Context→provider:** `user_message` + history (≤20 items, trimmed to budget)
  + `[Memory]` section (always empty today) + preferences → `messages[]` where
  sections become `system` messages and `user_text` becomes the `user` message
  (`Dispatcher._build_messages`, dispatcher.py:1105–1118).
- **Result→UI:** `EngineResult.metadata` keys (`token_source`, `retry_count`,
  `fallback_used`, `tool_call_count`, `context_tokens`, `failure_type`,
  `model`) → `telemetry.record_execution` → panels/chat line render from the
  record only.
- **Config:** panel actions → `config_store.save_config` (Supabase, fallback
  dict) → `apply_runtime_selection(provider, model)` pushes into the live
  `ProviderManager` (`engine/engine.py:215`).

---

## 5. Current database structure relevant to this work

| Table | State | Relevant columns |
|---|---|---|
| `ai_memories` | migrated (20260804145402), **unused by code** | `owner_id`, `tier` CHECK(short/long/permanent), `category` CHECK(fact/preference/context/summary/instruction), `content` NOT NULL, `importance` real, `expires_at`, `metadata` jsonb; indexes owner/tier/owner_tier; RLS SELECT-only |
| `ai_usage` | **no migration** — schema fully specified in doc §13 | `owner_id`, `session_id`, `provider`, `model`, `prompt_tokens`, `completion_tokens`, `total_tokens`, `latency_ms`, `created_at` |
| `ai_provider_stats` | **no migration** — schema in doc §12; PK `(provider_name, owner_id)` | request/success/fail counters, `total_prompt_tokens`, `total_completion_tokens`, `avg_latency_ms`, `last_request_at` |
| `ai_config` | migrated (incomplete per §19.1) | provider/model selection singleton; `last_request_at`/`last_latency_ms` never persisted (§19.2) |
| `panel_settings` | migrated (missing columns per §19.3 — any new column migration MUST also close that gap or be additive-only) | 12 typed settings columns incl. `update_stale_seconds` |
| `saved_items`, `bio_state`, `username_state`, `bot_logs`, `ai_sessions`, `ai_messages`, `ai_tool_history` | migrated | not modified by this work |

**Extensible without new tables:** `panel_settings` (font setting).
**Specified-but-unmigrated tables to create exactly as documented:** `ai_usage`,
`ai_provider_stats`. **One genuinely new table required:** Ghost Room chat
registry (§8 below). Nothing else.

---

## 6. Problems CONFIRMED by source inspection

1. **P1 — Stored memories never reach the model.** Default `MemoryManager()` has
   no repositories; nothing writes memories either; `persistence.save_memory`
   is dead code. The `[Memory]` section is always empty.
2. **P2 — Discarded-attempt token loss.** Empty-response retry and
   action-recovery retry replace the response object before its usage is
   accumulated (two exact sites in `dispatcher.py`).
3. **P3 — Usage persistence does not exist.** `ai_usage`/`ai_provider_stats`
   unmigrated, repositories unwired, no recording call anywhere.
4. **P4 — No provider quota/reset metadata.** Only per-request `Retry-After`
   exists; cooldown windows in `health.py` are the only safe "reset" signal.
5. **P5 — Font hardcoded** at `src/index.css:30`; restart-safe persistence absent.
6. **P6 — `ai_status` duplicates Overview/Usage** from a third, partly-dead data
   source (`config_store.last_*`, §19.2).
7. **P7 — Per-message usage not addressable.** `ReplyResolver` entries lack
   tokens/latency; Details can render only `telemetry.last()`.
8. **P8 — Memory bounding not enforced.** `_trim_to_budget` trims history only;
   `PermanentMemory` cap is warning-only; `retrieve_for_prompt` caps count (10)
   but not tokens.
9. **P9 — Ghost Room has no implementation** despite env placeholder.

## 7. Problems NOT confirmed (do not act on them)

- "Double counting across continuation rounds" — NOT confirmed; accumulation is
  correct (each round's usage added once).
- "Estimates presented as actuals" — NOT confirmed; `token_source` labeling and
  `≈` rendering already enforce honesty (tests 33–35 pin it).
- "Providers expose quota/reset APIs" — NOT confirmed; do not build against
  imagined endpoints.
- "Multiple font paths across UI components" — NOT confirmed; there is exactly one.
- Any claim about Ghost Room behavior — nothing exists to audit beyond P9.

---

## 8. Required schema changes (doc-first, in this order)

Every change follows DATABASE_ARCHITECTURE.md §21: **update the document first**
(with executable SQL), then generate the idempotent migration from it, then add
the §20 row. One logical change per migration.

1. **Migration A — `CREATE TABLE IF NOT EXISTS ai_usage`** — exact DDL from doc
   §13 (columns/indexes/RLS SELECT policy for anon+authenticated). Update §20.
2. **Migration B — `CREATE TABLE IF NOT EXISTS ai_provider_stats`** — exact DDL
   from doc §12 incl. composite PK `(provider_name, owner_id)` + RLS policy. Update §20.
3. **Migration C — `ALTER TABLE panel_settings ADD COLUMN IF NOT EXISTS
   dashboard_font text NOT NULL DEFAULT '';`** — update doc §6 (columns),
   §17 (settings table), §20. Additive-only so it does not interact with §19.3.
4. **Migration D — `CREATE TABLE IF NOT EXISTS ghost_chats`** — smallest durable
   state for Ghost Room:

```sql
CREATE TABLE IF NOT EXISTS ghost_chats (
    chat_id         bigint       PRIMARY KEY,
    display_name    text         NOT NULL DEFAULT '',
    last_preview    text         NOT NULL DEFAULT '',
    last_message_at timestamptz,
    unread_count    integer      NOT NULL DEFAULT 0,
    created_at      timestamptz  DEFAULT now(),
    updated_at      timestamptz  DEFAULT now()
);
CREATE INDEX IF NOT EXISTS idx_ghost_chats_last_message
    ON ghost_chats (last_message_at DESC);
ALTER TABLE ghost_chats ENABLE ROW LEVEL SECURITY;
DROP POLICY IF EXISTS "anon_select_ghost_chats" ON ghost_chats;
CREATE POLICY "anon_select_ghost_chats" ON ghost_chats FOR SELECT
    TO anon, authenticated USING (true);
```

   Document as new section (e.g., §22 Ghost Chats) + §20 row. Previews truncated
   to 160 chars at write time (PII minimization).

No other schema changes are authorized by this contract.

---

## 9. Required code changes (WHERE / REUSE / NEW / DB / INTERACTION / TEST)

### 9.1 Wire memory retrieval (fixes P1)

- **WHERE:** `RepositoryManager.__init__` (`database/manager.py`): when
  `supabase_available`, instantiate a new `SupabaseMemoryRepository` instead of
  `InMemoryMemoryRepository`. Implement it inside
  `database/memory_repository.py`, reusing the call patterns of
  `persistence.query_memories/save_memory/delete_expired_memories`
  (`asyncio.to_thread` + bounded timeout via the existing `_run_sync` helper in
  `persistence.py`; expose async wrappers there rather than duplicating client
  access). Keep `InMemoryMemoryRepository` untouched as fallback.
- **REUSE:** `MemoryManager.retrieve_for_prompt`, `LongMemory`, `PermanentMemory`,
  `Dispatcher._build_context` — **no dispatcher changes needed**; injection at
  Engine construction is sufficient (`engine/engine.py:96`: pass
  `get_repository_manager().memory` when available, else `None`).
- **NEW:** `SupabaseMemoryRepository` implementing the four interface methods
  (`save/query/delete/delete_expired/count`) against `ai_memories`.
- **DB:** `ai_memories` already exists — none required.
- **INTERACTION:** memory text lands in the `[Memory]` prompt section exactly as
  designed (`PromptBuilder._render_memory`); failure degrades to empty strings
  (existing try/except in `_build_context`, dispatcher.py:1187–1190).
- **Writes (opt-in):** reuse `MemoryManager.store_long/store_permanent` proxies.
  Gate behind `PreferencesContext.auto_memory` (already loaded in
  `_load_preferences`); default remains OFF this phase — no automatic memory
  creation.
- **TESTS:** new `tests/test_37_ai_memory_db.py` — (a) fake-db
  `SupabaseMemoryRepository.save/query/delete_expired/count` round-trip;
  (b) `Engine(memory_manager=None)` picks up repository when supabase_available;
  (c) `_build_context` renders non-empty `[Memory]` when entries exist and empty
  section when repo fails; (d) degradation: repo raising → request still succeeds.

### 9.2 Bound memory context (fixes P8)

- **WHERE:** `MemoryManager.retrieve_for_prompt` — after assembling blocks, trim
  `long` and `permanent` text to `DEFAULT_MAX_MEMORY_TOKENS` (1000) using
  `prompt.budget.estimate_tokens` (drop lowest-importance entries first for
  long; drop oldest for permanent). Extend `PromptBuilder._trim_to_budget` to
  drop the entire MEMORY section when still over budget after history trimming
  (history keeps priority, matching current semantics).
- **REUSE:** `estimate_tokens`, `SOFT_TOKEN_CAP` constant (promote to shared use).
- **DB:** none. **TESTS:** (a) 25 long entries × 100 tokens → block ≤ 1000 est.
  tokens and highest-importance retained; (b) permanent >500-token footprint →
  truncated; (c) over-budget prompt loses `[Memory]` before user text.

### 9.3 Fix discarded-attempt token accounting (fixes P2)

- **WHERE:** `dispatcher.py` — (i) empty-response retry block: before replacing
  `response` with `retry_response`, add the old `response.usage` counts into a
  local accumulator and merge it when initializing `usage`; (ii)
  action-recovery retry block: same merge when `recovery_response` replaces
  `response`. Never exceed `actual` labeling rules — merged counts stay
  provider-reported.
- **REUSE:** existing `usage` dict and `provider_usage_reported` flag.
- **TESTS:** extend `tests/test_33_ai_telemetry.py` — stub manager returning an
  empty-success response (with usage) then a text response (with usage): total =
  sum of both; recovery path likewise; token_source stays `actual`.

### 9.4 Persist usage per provider+model (fixes P3)

- **WHERE:** new module `backend/ai/database/supabase_repositories.py` OR inside
  the two existing repository modules (choose: implement
  `SupabaseUsageRepository` / `SupabaseProviderStatsRepository` directly in
  `usage_repository.py` / `provider_stats_repository.py`, following the
  `config_store._run_sync` pattern). Wire into `RepositoryManager.__init__`
  when supabase_available (resolves §19.11 for these two repos only).
- **Recording hook:** in `dispatcher.py`, immediately after BOTH
  `telemetry.record_execution(result, ...)` calls (success path and `_fail`),
  call a new fire-and-forget `guarded_create_task(persist_usage(record))` in a
  tiny new helper `backend/ai/database/usage_recorder.py` — exactly-once per
  request, both fast path and provider path, failures logged never raised.
  Fast-path rows use `provider="local"`, `model="deterministic"` (matches the
  Details convention).
- **REUSE:** `UsageRecord.as_dict`, `ProviderStatsRecord`, existing
  `record_request` aggregate semantics.
- **DB:** Migrations A+B (§8.1–8.2).
- **TESTS:** new `tests/test_38_usage_persistence.py` — (a) one provider request
  → exactly one `ai_usage` insert + one `record_request` aggregate; (b) fast
  path → exactly one row with deterministic ids; (c) `_fail` path → one row
  with zero tokens and failure flag; (d) DB error → request unaffected.

### 9.5 Provider reset detection (fixes P4 — scoped honestly)

- **WHERE:** `manager/health.py` already tracks per-provider category cooldowns.
  Add a read-only accessor `reset_state(provider) -> {"available": bool,
  "cooldown_remaining_s": float}` consumed by the Health panel's existing
  `Backup · <state>` line (handlers/ai.py `_ai_health_panel_handler`).
- **EXPLICIT LIMIT:** providers expose NO account-quota or reset-window
  metadata. "Reset detection" means rate-limit cooldown expiry only. The UI may
  say "Rate-limit cooldown · ~Xs left"; it must NEVER claim quota/credit resets.
- **TESTS:** extend test_38 — cooldown set → accessor reports remaining window;
  expired → available; unknown provider → available.

### 9.6 Persistent dashboard font (fixes P5)

- **WHERE:** `services/settings_service.py` — add `"dashboard_font": ""` to
  `_DEFAULTS`, a non-empty-or-empty-string validator, and typed accessors
  `dashboard_font()/set_dashboard_font()` (exact pattern of `language`).
  Migration C adds the column. `/api/settings` needs no change (`get_all()`).
- **Frontend:** `src/App.tsx` (or index.css root) — on boot, read
  `settings.dashboard_font`; if non-empty set `document.documentElement.style
  .setProperty('--app-font', value)`; `src/index.css:30` becomes
  `font-family: var(--app-font, 'Inter', 'SF Pro Display', -apple-system,
  system-ui, sans-serif);`. Values are CSS font stacks chosen from a fixed
  allow-list rendered in the dashboard settings surface (no free-text injection).
- **Telegram:** explicitly out of scope (impossible).
- **REUSE:** whole settings pipeline (write-through cache ⇒ restart-safe).
- **TESTS:** settings roundtrip test (set → get_all contains value → invalid
  rejected); frontend covered by typecheck/build only.

### 9.7 AI Settings/UI cleanup — remove duplication (fixes P6)

- **WHERE:** delete `_ai_status_panel_handler`, `_ai_status_inline_builder`,
  `_ai_status_refresh_action`, their registrations, and repoint the two entry
  buttons (`ai.py:1140` Test-Models "📊 Status", post-pick redirect L983) to
  `panel:ai` (Overview). Identity lives in Overview; health in Health; counters
  in Usage — one surface per question.
- **GUARDRAIL:** residual search for `ai_status` across backend/tests/docs
  before committing; update pinned tests (test_11/test_34 families) to the new
  button targets WITHOUT weakening assertions.
- **Settings surfaces stay as-is** (Execution 12 completed the personal/
  advanced split). No further renaming.

### 9.8 Per-message AI usage/time details (fixes P7)

- **WHERE:** `ReplyResolver.register` gains optional kwargs
  `input_tokens/output_tokens/total_tokens/token_source/latency_s` (defaults keep
  every existing caller compiling); `ResolvedAIContent` gains the same fields.
  Populate at the existing call site `ai_unified.py:623–632` from
  `result.metadata` / `telemetry.last()`. Extend `_ai_details_panel_handler`
  with an optional `extra` = telegram_msg_id: when present and resolvable,
  render THAT message's facts; else current behavior. Details gains a
  "per-message" entry point: replying to an AI message and opening Details is
  out of scope for callbacks; instead the compact chat line stays the primary
  per-message surface and Details-from-reply arrives through the existing
  reply-aware activation (reply "details" to an AI message routes via
  ai_unified → open Details with extra=msg_id). No second details panel.
- **HONESTY:** unavailable fields render "Unavailable"/"—" (existing rules;
  estimated keeps `≈`).
- **TESTS:** extend test_33 — register with usage fields resolves; resolve miss
  falls back to last(); estimated marker preserved.

### 9.9 Database management extension

- **WHERE:** `services/database_service.py::do_stats` — append row counts for
  `ai_usage`, `ai_provider_stats`, `ghost_chats` (via db client, try/except per
  table). Panel needs no change.
- **TESTS:** extend `tests/test_03_database_consistency.py` style unit test with
  fake db returning counts.

### 9.10 Ghost Room (greenfield — the ONLY new subsystem)

New files: `backend/bot/handlers/ghost_room.py` (panels/actions/input),
`backend/services/ghost_room_service.py` (discovery/state/message fetch/send),
plus router registration line in `router.register_all`.

- **Discovery/recording:** the handler registers ONE
  `events.NewMessage(incoming=True)` listener filtered to private chats
  (`event.is_private`) excluding the owner's own ID. On message: upsert
  `ghost_chats` row (chat_id key, display_name, preview ≤160 chars, timestamp,
  unread_count += 1) via the repository pattern (`to_thread`, bounded timeout,
  in-memory fallback mirroring `db/client.py`). Listener body is lightweight and
  wrapped so a failure can never break event dispatch (mirror the
  `register_runtime_hooks` guard style).
- **Chat list panel `ghost`**: rows from ghost_chats ordered by
  last_message_at DESC, showing name · relative time · `(n)` unread badge;
  buttons `action:ghost_open:<chat_id>`; opening clears unread_count
  (write-through). Owner-gated via the standard panel guard (the UI is
  owner-only; incoming messages are from others by definition).
- **Chat view panel `ghost_chat`**: five-message chunk fetched with
  `client.iter_messages(chat_id, limit=(page+1)*5)`, sliced to the page's five,
  rendered oldest→newest with sender/preview and per-message toggle buttons
  `action:ghost_toggle:<msg_id>`; selected messages marked `✓`; explicit
  selection ONLY — the system never infers relatedness. Selection state is a
  process-RAM set keyed by (chat_id) capped at 10 entries.
- **Actions on the chunk:** `action:ghost_page:<dir>` (prev/next five),
  `input:ghost_reply` flow (quote reply → `client.send_message(chat_id, text,
  reply_to=<first_selected_msg_id>)`; "send without quote" button variant omits
  `reply_to`), `action:ghost_clear` (empty selection), AI actions:
  `action:ghost_ai_single` (exactly one selected message required) and
  `action:ghost_ai_multi` (≥1 selected). Both build the AIRequest in the
  handler: single-message uses the existing `ReplyContext` fields
  (`sender_name`, `text_preview`, `message_id`); multi-message joins the
  explicitly selected texts into a formatted prefix prepended to
  `user_message` ("Selected messages:\n[1] Name: text\n…\n\nRequest: …") —
  zero dispatcher/context-builder changes, no inferred context ever added.
  Execution goes through `get_engine().execute(AIRequest(...))` like
  ai_unified; delivery edits the panel message in place.
- **Navigation/state:** Back returns chat-view → list → menu via existing
  `panel:_nav:back/home` plus explicit `action:ghost_back`. Position (current
  chat/page/selection) is process-RAM only (documented as ephemeral); durable
  state is limited to the `ghost_chats` table.
- **Callback safety:** all callback payloads use short forms
  (`ghost_open:<chat_id>` fits 64 bytes; msg-id toggles use
  `<seq>:<msg_id>` pairs validated against the currently rendered chunk, same
  stale-guard philosophy as the model picker hash).
- **TESTS:** new `tests/test_39_ghost_room.py` — incoming upsert/unread math;
  five-slice pagination boundaries (1,5,6,11 messages); toggle/clear; quote vs
  no-quote send args; multi-message payload shape; unread cleared on open;
  callback staleness re-render; owner-exclusion filter.

---

## 10. Required UI changes (summary)

1. Overview/Details/Usage/Health remain THE execution-information surfaces; the
   only structural change is removing `ai_status` (§9.7) and adding the
   cooldown phrase to Health (§9.5).
2. Details optionally renders a resolved per-message record (§9.8).
3. Dashboard gains a font picker fed by `/api/settings` (§9.6).
4. Menu gains one `Ghost` panel entry (`register_panel("ghost", ..., parent="menu")`)
   — no new text commands anywhere.

## 11. Required Ghost Room behavior (normative)

List → chat (5-message chunks) → explicit per-message selection → actions
(reply / reply-without-quote / clear / AI single / AI multi) → back navigation.
Unread badges increment automatically on incoming private messages and clear on
open. All UI updates edit the existing panel message. The system MUST NOT
automatically infer which messages are related; context = exactly the messages
the user selected, nothing more.

## 12. Required tests (consolidated)

- `tests/test_37_ai_memory_db.py` (new): repo round-trip, injection,
  `[Memory]` rendering, degradation, bounding (§9.1–9.2).
- `tests/test_33_ai_telemetry.py` (extend): merged discarded-attempt usage;
  ReplyResolver usage fields (§9.3, §9.8).
- `tests/test_38_usage_persistence.py` (new): exactly-once rows on both paths,
  aggregates, failure isolation, cooldown/reset accessor (§9.4–9.5).
- `tests/test_39_ghost_room.py` (new): §9.10 suite.
- Settings font roundtrip (extend settings tests or test_36 family).
- `ai_status` removal: residual search + updated pins in test_11/test_34.
- Full gates unchanged: `python3 -m compileall -q backend`;
  `.venv/bin/python -m pytest tests/ -q --asyncio-mode=auto`;
  `npx tsc -b --noEmit`; `npm run build`; `git diff --check`.
  Baseline to beat without regressions: 635 passed / 0 failed / 1 warning.

## 13. Explicit non-goals

- No semantic/embedding memory, summarizers, or auto-memory writes (auto-memory
  stays OFF; manual store paths reused only).
- No cost/pricing estimates anywhere.
- No invented provider quota/reset APIs.
- No Telegram-side font control.
- No message relatedness inference in Ghost Room.
- No new text/dot commands; no second UI architecture; no parallel AI execution
  path; no changes to Supabase client behavior, RLS model, or existing tables'
  existing columns.
- No resolution of §19.1/§19.3 legacy gaps beyond the additive font column.

## 14. Risks

| Risk | Mitigation |
|---|---|
| Doc/migration drift | §21 doc-first rule restated above; every migration updates §20 |
| Memory grows prompts past budget | §9.2 hard bounds + budget-section drop; estimates include memory automatically |
| PII in `ghost_chats` previews | 160-char truncation; owner-only UI; RLS SELECT-only |
| Incoming listener overhead/failure | guarded, minimal work, never raises into dispatch loop |
| Exactly-once persistence under retries/fallback | single recording choke point beside `record_execution`; tests pin counts |
| Callback 64-byte ceiling | short payload schemes + stale re-render guards (proven pattern) |
| Regression in pinned AI contracts | update tests only where design intentionally changed; never weaken |

## 15. Implementation order

1. Schema docs + Migrations A/B (ai_usage, ai_provider_stats) + repositories + recorder hook + tests (§9.3–9.5 first: token merge fix precedes persistence).
2. Memory: SupabaseMemoryRepository + Engine injection + bounds + tests (§9.1–9.2).
3. Font column migration C + settings accessors + dashboard var (§9.6).
4. Remove `ai_status`, add Health cooldown line, per-message Details (§9.7–9.8).
5. DB-management stats extension (§9.9).
6. Migration D + Ghost Room MVP (§9.10).
7. Full validation chain, IMPLEMENTATION_REPORT.md execution entry, protected-doc verification, single coherent commits per step.

Each numbered step is independently shippable and must keep the full suite green.

## 16. Acceptance criteria

- Replying about a stored fact works without re-stating it: `[Memory]` section
  non-empty in built prompts when `ai_memories` has rows; empty (not error) when
  DB unavailable.
- Memory contribution to prompt estimate ≤ 1000 tokens, always.
- Every AI request produces exactly ONE `ai_usage` row and updates
  `ai_provider_stats` once — including fast-path and failed requests; totals
  include previously-discarded retry attempts.
- Health panel shows real cooldown windows; never claims quota resets.
- Changing the dashboard font persists across restart and applies on reload.
- No duplicate AI information surfaces: `grep -rn "ai_status" backend tests`
  returns only historical docs.
- Replying to a specific AI message can show THAT message's provider/model/
  tokens/latency with honest availability labels.
- Ghost Room: private chats appear automatically with unread counts; chunks are
  exactly five messages; only explicitly toggled messages enter AI context;
  quote/no-quote replies send correctly; Clear empties selection; Back always
  returns predictably; all updates are edits of one message.
- Full suite ≥ previous baseline with zero new failures; tsc + build pass;
  working tree clean after push; DATABASE_ARCHITECTURE.md contains executable
  SQL reproducing every new object and a §20 row per migration.

---

## Implementation checklist

```
[ ] AI memory retrieval
[ ] AI memory injection
[ ] bounded memory context
[ ] token accounting
[ ] provider-reported usage
[ ] fallback estimation
[ ] provider/model usage persistence
[ ] reset detection
[ ] font persistence
[ ] database management
[ ] database schema documentation
[ ] AI Settings cleanup
[ ] AI per-message detail
[ ] Ghost Room chat list
[ ] unread state
[ ] persistent Ghost Room position
[ ] five-message pagination
[ ] explicit message selection
[ ] reply
[ ] reply without quote
[ ] AI single-message reply
[ ] AI multi-message context
[ ] Ghost Room navigation
[ ] automatic incoming-message update
[ ] Clear action
[ ] edit-based UI updates
[ ] tests
```
