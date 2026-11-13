# INVESTIGATION — Unified Telegram History & Event Intelligence (P0)

_Base commit: `ca0399b2156999a065444f31a31a58bb38841663` (`main`). This is a
source-driven investigation and specification. Nothing in this tree adds runtime
behavior: no listener, scheduler, worker, queue, executor, Telegram client or
database object was introduced. Every claim below was read out of this tree; the
label says how far it is proven._

Labels used throughout:

- **Verified in source** — read directly in the named file/function.
- **Verified by existing tests** — exercised by a deterministic test in `tests/`.
- **Architectural inference** — a conclusion drawn from the verified pieces, not
  itself implemented.
- **Proposed decision** — a design choice this investigation recommends.
- **Unresolved** — requires an owner decision or live verification.

## 1. The question

The owner wants Telegram history summarization to become a first-class,
incremental, user-configurable **History & Event Intelligence** feature: manual
and link-based summarization from the Glass UI, opt-in automatic per-chat
processing, and eventually one shared candidate pipeline feeding Task, Memory,
Saved Items, digest and selective-media destinations — without a second
summarization engine, a second Telegram client, a parallel execution authority,
or a bandwidth blow-up.

The existing feature already summarizes up to 1,000 messages. So the real
question is not "how do we build summarization" but "what exactly does the
existing pipeline already guarantee, what is genuinely missing around it, and in
what order can the missing pieces be added safely".

## 2. Verified current implementation

### 2.1 The one existing call chain

Today the only way to summarize is an AI-chat request. The chain is:

1. **Handler** — `backend/bot/handlers/ai_unified.py`. The owner's message is
   turned into an `AIRequest` (ai_unified.py:757) with
   `timeout_s=_AI_EXECUTE_TIMEOUT` (ai_unified.py:72, assigned at :767) and
   dispatched with that same bounded envelope (ai_unified.py:785). The live
   engine supplies the provider manager (`pm = engine.provider_manager`,
   ai_unified.py:739).
2. **Dispatcher** — `backend/ai/engine/dispatcher.py::Dispatcher.dispatch`.
   The model chooses a tool call; tool calls are executed through
   `ToolExecutor.execute_calls(...)` (dispatcher.py:867) with a per-request
   `ToolContext` built by `_build_tool_context` (dispatcher.py:1484). That
   context carries `chat_id`, `request_message_id`, `request_text`,
   `request_id`, provider/model labels, `request_timeout_s` (dispatcher.py:1503-1519)
   and the live `provider_manager` (dispatcher.py:1523).
3. **Tool schemas** — `backend/ai/tools/registry.py` registers
   `translate_history` and `summarize_history` (registry.py:151, 248-249);
   `ToolExecutor` treats them as long-running (executor.py:370) and gives them
   progress labels (executor.py:74-75).
4. **Tool** — `backend/ai/tools/history_ai.py::TranslateHistoryTool` /
   `SummarizeHistoryTool`. They are READ_ONLY, safe, long-running, cap the
   requested count at 1,000 and pass the owner's instruction through. They read
   `chat_id`, `request_message_id`, `request_timeout_s`, `request_id` and
   `provider_manager` from `ToolContext.extra` (`_provider_manager`,
   history_ai.py:49-51).
5. **History AI service** — `backend/services/history_ai_service.py::summarize_history`
   (:785) and `::translate_history` (:695). Both take
   `(source, chat_id, *, count, instruction, provider_manager,
   current_message_id, request_id, timeout_s)` and return
   `(success, text, data)`.
6. **History service** — `backend/services/history_service.py::fetch_recent_history`
   (:303) / `iter_history_pages` (:387).
7. **Telegram API facade** — `backend/telegram_api/messages.py::iter_messages`
   (:144), which maps `min_id`/`max_id` onto Telethon`s exclusive bounds
   (:158-164) and serializes each message.
8. **Provider** — `history_ai_service._call` (:417) calls
   `manager.chat(messages, tools=[])`; the manager is the runtime
   `ProviderManager` (`_resolve_manager`, :662), so provider selection is
   whatever the owner already configured — no second selection logic.
9. **Delivery** — the tool result is delivered **verbatim** by the dispatcher:
   `summarize_history` and `translate_history` are in `_VERBATIM_READ_TOOLS`
   (dispatcher.py:92-105) and are never re-paraphrased by a continuation round.
   `ai_unified` sends that text to the owner.

**Verified in source.** Everything in this chain exists today.

Two consequences matter for P1:

- **There is no Glass UI entry for summarization at all.** The only trigger is a
  model tool call in the AI chat. A deterministic panel handler can call
  `history_ai_service.summarize_history(...)` directly with
  `get_engine().provider_manager` (the same accessor used at
  `backend/ai/tools/settings.py:113`; `ai_unified.py:739` reads
  `engine.provider_manager`) — no new service and no new provider path.
- **`current_message_id`** exists so the triggering message is excluded from
  the retrieved window (history_ai_service.py:801, :719). A panel-triggered run
  has no triggering message in the chat, so it passes `None`.

### 2.2 How history is fetched

`history_service.py` is a Telegram-free layer over the `telegram_api` facade:
it never imports Telethon and never iterates the client itself
(**Verified by existing tests**: `tests/test_history_service.py:166`,
`:174`, `:185`). `_fetch_page` (:196) calls
`iter_messages(client, chat_id, limit=..., min_id=..., max_id=...)`;
`_collect` (:229) walks newest→oldest with a strictly decreasing exclusive
`max_id` cursor, accumulates, then reverses into chronological order, sorts by
`(message_id)` and de-duplicates.

Bounds are real and local:

| Constant | Value | Line |
|---|---|---|
| `MAX_HISTORY_MESSAGES` | 1000 | history_service.py:56 |
| `DEFAULT_PAGE_SIZE` | 100 | :58 |
| `MAX_PAGE_SIZE` | 1000 | :60 |
| `MAX_HISTORY_SCAN_MESSAGES` | 5000 | :64 |
| `HISTORY_RPC_TIMEOUT_S` | 5.0 s per page RPC | :66 |

`iter_history_pages` (:387) yields `HistoryPage` objects (:109) with an explicit
`next_before_id`/`has_more` cursor; `_coerce_page_size` (:438) clamps page size.
Each fetch is bounded by `guarded_await(..., timeout=HISTORY_RPC_TIMEOUT_S)`, and
a failure raises `HistoryError` rather than returning an empty slice
(**Verified by existing tests**: `test_every_page_fetch_is_bounded_by_rpc_await`,
`test_fetch_failure_raises_history_error_instead_of_returning_empty`,
`test_slow_rpc_is_bounded_and_surfaces_as_a_history_error`,
`test_cancellation_is_never_swallowed`).

### 2.3 Where the 1,000-message limit lives

The limit is enforced in **three cooperating layers**, not one:

1. **Tool layer (model-facing)** — `history_ai.py` caps the requested count at
   1,000 and states the cap in its result (**Verified by existing tests**:
   `test_translate_count_beyond_the_bound_is_capped_and_stated`,
   `test_summarize_1000_messages_uses_the_service_boundary`).
2. **Service layer (authoritative)** — `history_service._collect` clamps to
   `MAX_HISTORY_MESSAGES` (**Verified by existing tests**:
   `test_requested_count_is_clamped_to_the_hard_bound`,
   `test_bounded_context_bounds_are_unchanged`).
3. **AI-service default** — `DEFAULT_COUNT = 100` (history_ai_service.py:65) is
   only a default, never a ceiling: the caller's count is passed to
   `_prepare` (:259) and clamped by the history service.

So the single 1,000-message authority is `history_service.MAX_HISTORY_MESSAGES`;
the tool layer is a declaration, not a second engine. **Verified in source.**

### 2.4 ID boundaries and pagination

`fetch_recent_history`/`iter_history_pages` accept `before_id` (exclusive upper
bound → Telethon `max_id`) and `after_id` (exclusive lower bound → `min_id`)
(**Verified in source**: history_service.py:303-436;
**Verified by existing tests**: `test_before_id_and_after_id_bound_the_range_exclusively`,
`test_pagination_uses_a_strictly_decreasing_exclusive_cursor`,
`test_page_boundaries_are_deterministic_across_runs`,
`test_iter_history_pages_covers_every_message_exactly_once_in_order`).

This is the single most important existing capability for incremental work: a
checkpoint is just an `after_id`, and "the 1,000 messages ending at the linked
message" is just `before_id = linked_id + 1`.

### 2.5 Filtering

- **AI provenance** — messages produced by the bot are marked and excluded
  centrally (`include_ai=False` default); the marker is stripped from returned
  text and excluded AI messages do not consume the requested count
  (**Verified by existing tests**: `test_ai_marked_messages_are_excluded_centrally`,
  `test_excluded_ai_messages_do_not_consume_the_requested_count`,
  `test_include_ai_returns_marked_messages_classified_and_marker_free`,
  `test_marker_stripping_preserves_the_exact_visible_text`).
- **Scan cap** — a window made entirely of excluded output is bounded by
  `MAX_HISTORY_SCAN_MESSAGES` instead of scanning forever
  (`test_scan_cap_bounds_a_window_that_is_entirely_ai_output`).
- **Service messages / deleted / edited** — no explicit filter exists in the
  history layer. `HistoryMessage` carries text, ids, sender, media metadata and
  date; the AI service renders only text-bearing messages and keeps media-only
  messages as placeholders (see 2.6). Deleted messages disappear from Telegram
  history by definition; edited messages are returned in their current text.
  **Verified in source** (`_normalize`, history_service.py:178; `_visible_body`,
  history_ai_service.py:333): there is no edit/delete filter, and no attempt to
  detect "this message was edited".
- **Outgoing** — not filtered; the owner's own messages are eligible
  (`test_human_owner_messages_stay_eligible_despite_out_and_sender_id`).
  **Verified by existing tests.**
- **Text truncation** — returned text is never truncated
  (`test_returned_text_is_never_truncated`); chunking happens at the AI service,
  not in retrieval.

### 2.6 Ordering, de-duplication, and map-reduce

Ordering is chronological, deduplicated by message id, and stable across runs
(**Verified by existing tests**: `test_messages_are_returned_chronologically_even_though_telegram_is_newest_first`,
`test_page_boundaries_are_deterministic_across_runs`).

Map-reduce lives entirely in `history_ai_service.py`:

- `_chunk_history` (:369) packs rendered lines into chunks under
  `SUMMARIZE_CHUNK_TOKEN_BUDGET = DEFAULT_MAX_CONTEXT_TOKENS` (:121); a single
  oversized message becomes its own chunk and is never split
  (`test_no_chunk_exceeds_the_token_budget`,
  `test_oversized_single_message_is_its_own_chunk_and_never_split`).
- `_map` (:504) runs chunks concurrently with `MAP_CONCURRENCY = 2` (:83),
  paces calls by `MAX_CALL_SPACING_S = 5.0` (:88) and respects a single
  operation deadline.
- One coherent budget model: `llm_budget` (:127), `max_map_calls` (:138),
  `call_spacing` (:149), `llm_deadline` (:159), `remaining_s` (:168),
  `call_timeout` (:175); `DEFAULT_ENVELOPE_S = 240.0` (:73) matches the
  handler's envelope. The reduce step is reserved time so it is not starved.
- Failure is honest: a chunk failure is never presented as a summary, a final
  aggregation failure is reported, and a budget timeout is an honest failure
  (**Verified by existing tests**: `test_summarize_chunk_failure_is_not_presented_as_a_summary`,
  `test_summarize_final_aggregation_failure_is_honest`,
  `test_summarize_budget_timeout_is_an_honest_failure`).
- Error codes are stable: `history_unavailable`, `ai_provider_failed`,
  `history_too_large_for_one_request`, `ai_engine_unavailable`
  (history_ai_service.py:201-247 region; asserted by
  `test_history_too_large_for_one_request_is_refused_honestly`,
  `test_missing_ai_engine_is_reported`).
- Pacing and concurrency caps are tested
  (`test_map_calls_are_paced_instead_of_fired_as_one_burst`,
  `test_map_never_exceeds_the_concurrency_cap`).

**Retries:** the AI service does not implement its own retry loop; the provider
manager's own failover/retry behavior applies (`_call`, :417). **Verified in
source.** This matters for checkpoints: "success" must be the service's
`(True, ...)` return, never "the request was dispatched".

### 2.7 Provider selection

`_resolve_manager` (:662) uses the passed manager (the running engine's
`ProviderManager`) or fails with `ai_engine_unavailable`. There is exactly one
provider-selection authority. **Verified in source.**

### 2.8 What the tests already establish

`tests/test_history_service.py` (32 tests) and `tests/test_history_ai_tools.py`
(~45 tests) pin: facade-only retrieval, count clamps, exclusive ID bounds,
chronological order, deterministic page boundaries, central AI-provenance
exclusion, honest failure semantics, no-truncation, tool registration as
READ_ONLY/long-running, executor no-confirmation, verbatim dispatcher delivery
(`test_dispatcher_delivers_both_tools_verbatim`), trigger-message exclusion by
identity not text, and the stage traces that distinguish retrieval, provider and
tool-result failures. **Verified by existing tests.**

## 3. What exists vs what is actually missing

| Capability | State |
|---|---|
| Bounded retrieval (≤1000), paging, ID bounds | Exists — `history_service.py` |
| Map-reduce summarization, budgets, pacing | Exists — `history_ai_service.py` |
| Provider selection | Exists — runtime `ProviderManager` |
| Verbatim result delivery | Exists — dispatcher verbatim set |
| Trigger-message exclusion | Exists — `current_message_id` |
| Glass UI entry (manual panel, chat picker, count, focus) | **Missing** |
| Chat/message-link → bounded history glue | **Missing** (parsing + resolution exists in `save_service`) |
| Per-chat checkpoint / last-summary metadata | **Missing** |
| Automatic per-chat processing | **Missing** (no periodic trigger) |
| Candidate pipeline for Task/Memory/Saved Items/digest/media | **Missing** (only task event triggers exist) |
| Media pipeline (bounded download, OCR, STT, outbound governor) | Exists — see §8 |

**Architectural inference:** the feature is a thin, deterministic orchestration
layer around an already-bounded engine. Reusing the engine is not a
convenience; creating a second one would duplicate the limits, provenance
filter and budget model that the tests currently pin.

## 4. Telegram links

### 4.1 What is parsed today (verified)

`backend/services/save_service.py` is the ONE parser:

- `_LINK_RE` (:55) matches `https://t.me/<username>/<id>`,
  `https://t.me/c/<internal_chat_id>/<id>` (and the `telegram.me` forms).
- `_parse_http_link` (:996) maps a `/c/` link to peer `-100<internal_id>` and a
  username link to `LinkTarget(username=...)`.
- `_parse_tg_openmessage` (around :890) handles
  `tg://openmessage?user_id=<id>&message_id=<id>`,
  `?chat_id=<id>` (basic group → `-<id>`), `?channel_id=<id>` (→ `-100<id>`),
  plus the `post=<id>` alias; exactly one peer parameter is required.
- `parse_link_target` (:1026) is the single entry point; `parse_telegram_link`
  (:1073) is its tuple-shaped wrapper.

**A link without a message id is refused.** `parse_link_target` requires both a
peer and a `message_id` and otherwise returns a `LinkTarget(error=...)` that
names the three accepted forms. **Verified in source** (:1026-1071). This is
important: "link to a chat without a specific message" is *not* supported today
and must be designed, not assumed.

### 4.2 Resolution and access (verified)

`_fetch_linked_message` (:2181) resolves in this order, and nothing else:

1. `_resolve_link_peer` (:1172): username → `get_entity(username)`; otherwise
   `get_input_entity(peer_id)` (session cache) → `get_entity(peer_id)` →
   `_dialog_entity` (:1113), a bounded scan of the account's OWN dialogs
   (`LINK_DIALOG_SCAN_LIMIT = 500`, :1092).
2. Peer-less lookup of the message id, **accepted only if its peer matches the
   requested one** — so a different chat's message can never be used.
3. Distinct failure kinds: `peer_unresolved`, `message_missing`,
   `lookup_failed` (`LinkResolutionError`, :1098-1110).

Consequences, stated precisely:

- Parsing a link proves nothing about access. A private chat the account is not
  in fails `peer_unresolved`; a deleted message fails `message_missing`.
  **Verified in source.**
- There is **no** second client, no userbot, no join, no invite-link redemption
  and no `messages.getMessages` bypass: only ordinary authenticated calls on the
  self client. **Verified in source.**
- Private **user** chats can resolve only if the account already knows the peer
  (session/dialog). A `tg://openmessage?user_id=…` for a stranger is a
  `peer_unresolved` failure even though it parses. **Verified in source** — this
  is exactly why no claim of "private chat support" may be made from parsing.
- The full dialog scan is itself a real Telegram RPC (`iter_dialogs`,
  bounded 500). It is acceptable for one explicit link action, but must not be
  used inside an automatic per-message loop. **Architectural inference.**

### 4.3 Link → history (proposed)

**Proposed decision:** a link is resolved once by the existing
`parse_link_target` + `_fetch_linked_message` path; the resolved peer's numeric
id and the message id are then handed to the existing
`history_service.fetch_recent_history(..., before_id=message_id + 1)` for a
manual "the N messages ending at this message" request. No new parser, no new
resolver, no new client. `before_id` is exclusive, so `+1` includes the linked
message. **Verified in source** that `before_id` maps to `max_id` exclusively;
the `+1` is a proposed use of it.

**Unresolved:** what a link to a *chat* without a message id should mean
("summarize this chat's last N"). It fails parsing today; supporting it means
extending `parse_link_target` with a deliberate chat-only target. That is a
small, testable parser change and belongs to P1's link phase only if the owner
wants it — otherwise it stays deferred.

## 5. Glass UI integration

### 5.1 The existing mechanism (verified)

- `backend/helper/panels.py::register_panel(panel_id, handler, parent, title)`
  (:279) registers a panel and its parent/title; `register_action` (:292) and
  `register_input(panel_id, input_id, InputConfig)` (:301) register callbacks
  and text-input handlers. `get_panel`/`get_action`/`get_input` read them back.
- Rendering is `_render_panel` → `title`, `body`, `buttons`; root panels get a
  Close button, child panels get navigation (`_add_nav_buttons`). Editing is
  `_safe_edit`, which swallows Telegram edit errors and logs them.
- `backend/helper/panel_registry.py` holds the panel tree (register :41,
  `register_panel` :98, `get_panel_parent` :115) used for navigation.
- `backend/helper/input_state.py::set_pending(owner_id, panel_id, handler,
  chat_id, prompt, ..., extra, timeout, inline_id)` (:27) is the one-shot
  text-input mechanism; `get_pending` (:65), `clear_pending` (:78). Pending
  input expires after its own bounded window (documented at :38-40), so a long
  summarization must run as a bounded background action with its own timeout,
  not as a pending-input handler.
- `backend/bot/router.py` wires handlers; `misc.py` shows the exact pattern:
  `register_panel("general", _general_panel_handler, parent="menu",
  title="General")` (:668), `register_inline_builder("general", ...)` (:669),
  `register_input("general", "intelligent_reaction_probability", {...})` (:691),
  and a menu row `builder.add_row("General", "panel:general", PRIMARY)`
  (menu rows at :218-234).
- `backend/helper/inline_sender.py` delivers the panel through the Helper Bot
  when available (the same delivery every panel uses).

**Verified in source.** No panel API needs to be invented for P1.

### 5.2 Proposed UX

**Proposed decision:** one `history` panel (`parent="menu"`, title
"History & Intelligence") with:

- **Manual path.** A bounded list of accessible chats (from the account's own
  dialogs, the same source `_dialog_entity` uses), a count selector limited to
  the existing 1,000 bound (100 / 250 / 500 / 1000), an optional focus
  instruction (≤500 chars, already enforced by
  `MAX_INSTRUCTION_CHARS`, history_ai_service.py:188), and a Run action. The
  result states the exact chat and the exact retrieved range
  (`first_id`–`last_id`, count) so the owner can see what was summarized.
- **Link path.** A pending-input field ("paste a Telegram message link"). The
  handler runs the existing parser/resolver and summarizes the N messages
  ending at that message. Every failure kind is rendered specifically
  (`peer_unresolved` / `message_missing` / `lookup_failed` / not a message
  link), never as "failed".
- **Automatic path.** A per-chat list with enable/disable and cadence — P3, not
  P1. The panel must not show a dead toggle before it works.

### 5.3 Smallest useful first phase

**Proposed decision:** P1 = manual panel + link field, both calling
`history_ai_service.summarize_history` directly. That is one panel, two
actions, one input registration, one menu row, reusing all existing services.
No persistence, no scheduler, no new tool.

**Architectural inference:** chat enumeration for the picker must be bounded
(e.g. first N dialogs with paging) — an unbounded picker would be the same
RPC-walk problem `LINK_DIALOG_SCAN_LIMIT` already bounds.

## 6. Incremental summarization and persistence

### 6.1 What persistence exists (verified)

- `backend/db/client.py`: `get_db` (:90), `is_available` (:125),
  `run_sync_db(fn, ...)` (:223) — the synchronous Supabase client behind a
  thread executor with timeouts. This is the only DB access seam.
- **Repository pattern** (`backend/ai/database/*`): a class + an in-memory
  fallback, e.g. `MessageRepository` (`message_repository.py:48`) and
  `InMemoryMessageRepository` (:64). Retrieval/agents resolve a singleton.
- **Singleton JSONB settings**: `backend/services/panel_settings_repository.py`
  (`_TABLE = "panel_settings"`, `_KEY = "global"`, `load` :41,
  `update_field` :59, `update_fields` :78, `reload` :98) with
  `backend/services/settings_service.py` holding `_DEFAULTS` (:76-92), a cache,
  validation and strict setters (`set_setting_strict` :281).
- **Dedicated config row with legacy fallback**: `backend/services/anti_edit_delete_service.py`
  (`_read_config` :237, `_persist_state` :384, `set_enabled` :403, store-mode
  switch) — proof the repository already has a precedent for a dedicated
  config table plus a safe read-only legacy path.
- **CAS / idempotent occurrences**: `backend/ai/database/task_repository.py`
  (status transition tables, `SCHEDULE_TYPES`, `MAX_ATTEMPTS`, version/CAS) and
  `backend/ai/task_scheduler.py` (`occurrence_key` :42, claim/complete paths).
- Existing tables do **not** include any per-chat history checkpoint (verified
  table list: `saved_items`, `ai_tasks`, `ai_task_occurrences`,
  `panel_settings`, `aed_config`, `bio_state`, `username_state`, …).

### 6.2 What must be persisted

**Proposed decision:** one logical row per `(owner_id, chat_id)` — not per run —
holding only what reliability needs:

| Field | Why |
|---|---|
| `owner_id`, `chat_id` | identity + scope; unique together |
| `last_message_id` | the checkpoint (exclusive lower bound for the next run) |
| `last_success_at` | staleness/visibility |
| `last_status` | `idle` / `running` / `failed` / `disabled` for the panel |
| `last_error` (bounded, sanitized) | honest failure visibility |
| `last_summary_message_id` | the assistant message that carries the last summary (a reference, not a copy) |
| `last_summary_chars` (or a bounded hash) | dedupe/no-op detection without storing full text twice |
| `enabled`, `cadence_minutes` | owner configuration (or keep cadence in the singleton and only `enabled` here) |
| `version` / `updated_at` | CAS for concurrent runs |

**Rationale (verified precedent):** a dedicated table is the established
pattern for owner config with a legacy fallback (`aed_config`), while the
singleton `panel_settings` JSONB row is a single global document and is the
wrong shape for N chats. **Unresolved (owner):** whether to reuse an existing
config table with a JSONB `chats` map or create a dedicated table. No SQL,
migration or Supabase change is made in this task.

### 6.3 Checkpoint semantics (proposed, must be tested)

1. **Fetch** only messages with `after_id = last_message_id` (exclusive) up to
   the existing 1,000 bound; refuse (do not silently drop) when the backlog
   exceeds the bound.
2. **Never advance before success.** Advance only after the AI service returns
   `success=True` AND the delivery/persistence step recorded the new
   checkpoint. "Dispatched" and "provider answered" are not success.
3. **Failure retains the checkpoint.** Retrieval error, provider failure,
   timeout, cancellation, or a DB write failure leaves the previous value.
4. **Duplicate prevention.** The advance is a single CAS write
   (`WHERE last_message_id = <old>`) or an idempotent upsert keyed by
   `(owner_id, chat_id, message_id)`; a re-run of the same interval cannot
   re-summarize already-advanced messages.
5. **Empty interval.** Zero new eligible messages → no provider call, status
   `idle`, checkpoint untouched, an explicit "nothing new" result.
6. **Deleted/edited messages.** Deletions cannot be detected without the
   separate archive subsystem; an edit after the checkpoint is not re-read.
   This is documented behavior, not silently claimed coverage.
7. **Previous summary as context.** Supply at most one bounded previous summary
   (the stored `last_summary_message_id` text, truncated to a fixed budget) into
   the prompt; never re-fetch the full original history.
8. **Overlap prevention.** A `running` status row plus CAS/lease means a second
   trigger for the same chat while one runs is skipped (same discipline as an
   occurrence claim in `task_repository`), never queued unboundedly.

## 7. Scheduling and runtime lifecycle

### 7.1 Existing mechanisms (verified)

- `RuntimeSupervisor` (`backend/runtime/supervisor.py`) is the single runtime
  authority. It starts the scheduler once (`_start_task_scheduler`, :464),
  builds ONE `TaskScheduler` with the shared `TaskRepository`,
  `TaskExecutionCoordinator`, `ToolExecutor`, `ToolRegistry` and outcome
  notifier (:466-526), and stops it on shutdown (`_stop_task_scheduler`, :528).
  Forever loops use `immortal_create_task` (`_run_task`, :208) and the client is
  re-resolved through `client_provider` so a rebuilt client never leaves stale
  references (:480-486).
- `TaskScheduler` (`backend/ai/task_scheduler.py`): `WAKE_INTERVAL_SECONDS = 60`
  (:22), `MIN_WAKE_SECONDS = 1` (:24), claim keys `occurrence_key(task_id,
  scheduled_for)` (:42), `start` (:71)/`stop` (:78)/`recover` (:94), a bounded
  `run_once` → due retries + due sweeps (:330), `MAX_ATTEMPTS` retries, and a
  `run()` poll loop (:358). Occurrence claim/CAS prevents duplicate execution.
- Event triggers: `backend/bot/handlers/task_events.py` registers on the
  existing `NewMessage` update path (no second loop), applies the private-message
  policy first, then `TaskEventDispatcher.handle_event` and
  `TaskAnswerResolver`. `TaskEventDispatcher` caps matching
  (`MAX_EVENT_TASKS_PER_MESSAGE = 20`, `MAX_EVENT_EXECUTIONS_PER_MESSAGE = 5`)
  and creates occurrences with the deterministic
  `event_occurrence_key(task_id, chat_id, message_id)` — an idempotency
  primitive that already exists.
- Profile cron (`backend/profile/scheduler.py`): `register_updater` (:69),
  `_cron_loop` / `_supervised_cron` (:149/:235), `start_cron` (:254),
  `stop_cron` (:267), minute boundaries, engine-active gating. It is the
  profile-update cadence (bio/username), not a general job runner.

### 7.2 Recommended trigger

**Proposed decision:** P3 uses the existing **TaskScheduler**, not a new loop.
One internal scheduled job per auto-enabled chat (or one owner-level sweep,
**Unresolved**) with an `interval` schedule and a new deterministic internal
action kind executed through the existing
`TaskExecutionCoordinator → ToolExecutor → history_ai_service` path. That keeps
`RuntimeSupervisor → Dispatcher → ProviderManager/ToolRegistry → ToolExecutor
→ service` intact, inherits claim/CAS duplicate prevention, retries,
recovery-after-restart, shutdown, and outcome notification, and gives the owner
status/failure visibility.

**Unresolved and must be checked in P3 before coding:** whether
`backend/ai/task_contract.py::validate_prepared_action` accepts an internal
action kind without AI involvement, and whether per-chat rows are acceptable in
the Tasks surface (N rows vs one sweep). If those fail, the smallest
architecture change is a narrow, documented extension of the existing
occurrence model — *not* a second scheduler.

## 8. Media and bandwidth

### 8.1 What exists (verified)

- `backend/services/media_service.py` is the ONE media boundary:
  `resolve_media_message` (:1541) takes trusted `chat_id`/`message_id` (never
  model text), `analyze_media` (:1612) classifies → downloads bounded →
  validates → normalizes, and `MediaAnalysis.as_context_text()` is the only
  structured form that reaches the LLM.
- Bounds: `max_download_bytes()` (:490) reuses
  `settings_service.max_deep_save_mb()` (default ceiling 50 MB when settings
  fail); `download_timeout()` (:503) is bounded by
  `MEDIA_DOWNLOAD_TIMEOUT_S` (:138); resolution is bounded at 30 s
  (`MEDIA_RESOLVE_TIMEOUT_S`, :143).
- **Video/GIF/Animation are explicitly unprocessable**
  (`UNPROCESSABLE_MEDIA_TYPES`, :162), even though they appear in
  `DOWNLOADABLE_MEDIA_TYPES` (:149). So there is **no video analysis**.
- OCR: `OcrEngine` protocol (:596), `set_ocr_engine` (:614), `ocr_available`
  (:630). STT: `SttEngine` (:640), `set_stt_engine` (:659), `stt_available`
  (:675). Both seams are real; they only work when an engine is provisioned:
  `backend/services/gemini_media_engine.py` registers both (:1869-1870) and
  clears them on teardown (:1865-1866); `backend/services/stt_engine_factory.py`
  registers STT (:294, clears :281). Text-shaped documents and PDF/DOCX are
  extracted locally without a provider (`is_extractable_mime`, :475).
- `backend/services/media_ai_service.py::answer_media_request` (:320) sends the
  provider exactly two inputs: a static system instruction and the owner's
  request + `MediaAnalysis.as_context_text()` (module docstring; `_provider_call`
  :277). It uses `ProviderManager.chat` with plain string content — no image
  URLs, no base64, no multimodal provider dependency.
- **Bandwidth:** `backend/services/outbound_bandwidth_service.py` has
  `reserve_outbound_bytes` (:171), `reserve_outbound_bytes_sync` (:251),
  `get_outbound_usage` (:291), `release_reservation` (:363). Its **only**
  caller is `gemini_media_engine._reserve_gemini_bandwidth` (:750-780), i.e.
  outbound media payloads sent to Gemini are reserved fail-closed. Telegram
  media **downloads are inbound to Render and are not governed by this
  service** (verified: no other caller).

### 8.2 Policy for the future feature (proposed)

- Text-first: deterministic local filtering (chat match, text length, link/keyword
  shape, dedupe by message id) before any provider call; only selected text is
  batched into a request.
- Media: read text/caption first (the normalized message already carries it);
  consider filename only when available and relevant; **do not** download media
  by default. A bounded download is allowed only behind a deterministic
  relevance signal, through `media_service.analyze_media`, and its extracted
  text is treated like any other text candidate.
- Never upload media to external providers by default. If OCR/STT requires a
  provider, it consumes **outbound** bandwidth from Render and must pass the
  existing reservation; inbound Telegram transfers do not.
- **No quota or savings claim is made here.** No measurement was performed;
  the only verified fact is which side of the boundary each operation is on.

## 9. Unified event intelligence

### 9.1 What already exists as a candidate path (verified)

`backend/bot/handlers/task_events.py` already receives EVERY new message on the
existing update loop, normalizes it with
`task_event_dispatcher.extract_event_context` (:66) into a bounded dict
(`chat_id`, `sender_id`, `message_id`, text capped at 8,192 chars, `has_media`,
`media_type`, `is_reply`, `mentioned`, `out`, `date`), and hands it to
`TaskEventDispatcher.handle_event` (:143), which deterministically matches
event-scheduled tasks and creates one occurrence per
`(task_id, chat_id, message_id)` (`event_occurrence_key`, :40). No LLM is called
for non-matching messages; caps bound per-message work.

This is a real, working precedent for: a shared per-message entry point, a
bounded context contract, deterministic matching, and idempotent per-message
occurrences. It is **not** the requested candidate pipeline (it produces task
occurrences only, and it has no candidate object, no AI classification stage,
and no destination routing).

### 9.2 Proposed shape (P4+)

**Proposed decision:** one deterministic `EventCandidate` contract (local ids
only) produced by a text-first filter that rides the same handler, then:

1. **Deterministic stage** — local, no provider: eligibility (chat selected?),
   text presence/length, dedupe by `(chat_id, message_id)`, batching window,
   and a cheap relevance signal.
2. **AI stage** — one bounded request per batch (never per message), receiving
   only candidate texts plus destination-neutral instructions; **no chat ids,
   message ids, sender identity, usernames or unrelated conversation**.
   Classification returns destination + confidence + a short rationale; the
   provider maps results back by ordinal, not by Telegram identifiers.
3. **Deterministic application stage** — application code validates, authorizes
   and executes: Task via the existing task tool/coordinator, Memory via
   `MemoryRepository`, Saved Items via the existing save/retrieve services,
   digest via `history_ai_service`. Human approval policy differs per
   destination (Tasks/Saved Items propose; digest delivers).
4. **Idempotency and bounds** — deterministic candidate keys, a bounded queue,
   per-batch caps, and the same claim discipline the scheduler already uses.

**Architectural inference:** the existing `task_events` handler is the right
attachment point; it already owns the "every message, no second loop" position
and already applies the private-message policy first.

## 10. Answers to the required architectural questions

1. **Can the History Service support manual, link-based and incremental
   summarization with minimal changes?** Yes for retrieval. `after_id`/`before_id`,
   the 1,000 bound, ordering, provenance filtering and honest failures already
   exist. Missing: UI glue, link→history glue (one `+1` use of `before_id`), and
   checkpoint persistence. **Verified in source + architectural inference.**
2. **Smallest useful first phase?** P1: one `history` panel with manual chat
   selection and a link field, calling `history_ai_service.summarize_history`
   with the runtime provider manager. No persistence, no scheduler.
3. **What must be persisted?** `(owner_id, chat_id)` + `last_message_id` +
   status/time/error + a bounded reference to the last summary; see §6.2.
4. **What existing mechanism should trigger automatic processing?** The
   supervisor-owned `TaskScheduler` (interval occurrences) through the existing
   coordinator/executor path; not a new loop, not the profile cron.
5. **How to avoid duplicate summarization?** Exclusive `after_id` plus a
   CAS/lease checkpoint advance only on the defined success boundary, and
   `running` status to block overlap.
6. **How to avoid one LLM request per message?** Batch candidates under the
   existing chunk token budgets and pacing; deterministic filtering first;
   `MAP_CONCURRENCY = 2` and `MAX_CALL_SPACING_S` already bound provider load.
7. **How should selected chats be configured in Glass UI?** A per-chat list in
   the same panel, backed by the checkpoint/config row; toggles only appear in
   the phase that implements them.
8. **Inaccessible chats and links?** Fail with the existing distinct kinds
   (`peer_unresolved` / `message_missing` / `lookup_failed` / not-a-message-link),
   never silently empty and never by widening access.
9. **Boundary between summarization and Event Intelligence?** Summarization is a
   bounded, on-demand/digest operation with a checkpoint. Event Intelligence is a
   candidate pipeline that may *use* summarization as one destination; it must
   not become a per-message summarizer.
10. **Which services to reuse?** `history_service`, `history_ai_service`,
    `save_service` (links), `media_service`/`media_ai_service`,
    `outbound_bandwidth_service`, `TaskScheduler`/`TaskExecutionCoordinator`,
    `TaskRepository`, `TaskEventDispatcher`, `MemoryRepository`, the
    save/retrieve services, `panel_settings`/settings service and the existing
    panel/input registry.
11. **Which components are genuinely missing?** The History panel/actions, the
    link→history glue (and optionally chat-only links), the checkpoint/config
    store and repository methods, the automatic per-chat trigger/action kind,
    the candidate contract + deterministic filter + AI classification stage, and
    destination routing.
12. **What should be deferred?** Chat-only links, event/memory/saved-item
    auto-acceptance, media inspection of any kind, video, cross-chat digests,
    edit/delete re-processing, and any UI for phases that do not yet work.

## 11. Phase plan

Every phase is independently deployable and testable. "Files likely to change"
lists only paths established by this investigation; new file names are marked
proposed.

### P0 — Investigation and specification (this task)

- **Goal:** source-verified plan, no runtime change.
- **User-visible:** none.
- **Reuse:** n/a.
- **Files changed:** `ROADMAP.md`, `INVESTIGATION.md`.
- **Dependencies:** none.
- **Persistence/security/failure:** none.
- **Acceptance:** every conclusion labeled and traceable to a file/function;
  no source, migration, settings or runtime change.
- **Tests:** none added; `git diff --check` only.
- **Live tests:** none.
- **Readiness:** done when committed and pushed.

### P1 — Manual and link-based summarization in the Glass UI

- **Goal:** expose the existing engine deterministically.
- **User-visible:** `History & Intelligence` panel: pick an accessible chat,
  pick 100/250/500/1000, optional focus, Run; a link field that resolves via the
  existing parser and summarizes the window ending at that message; results
  state chat + range; every failure kind is specific.
- **Reuse:** `history_ai_service.summarize_history`, `history_service`,
  `save_service.parse_link_target`/`_fetch_linked_message`, `panels.register_*`,
  `input_state`, `inline_sender`, `get_engine().provider_manager`.
- **Likely files:** `backend/bot/handlers/misc.py` (menu row) or a new
  `backend/bot/handlers/history.py` registered from `router.py`;
  `backend/services/history_ui_service.py` (proposed, formatting + chat list).
- **Dependencies:** P0.
- **Persistence:** none.
- **Security/context:** only the owner's accessible chats; no new client; no ids
  sent to the provider beyond what the existing AI service already renders.
- **Failure:** retrieval/provider/timeout errors surface with the existing error
  codes; link failures use the existing kinds; no silent empty summary.
- **Acceptance:** manual run summarizes exactly the stated range; 1,001 is
  refused at the stated bound; link run excludes nothing extra; inaccessible
  link produces `peer_unresolved`.
- **Automated tests:** panel/action rendering + registration, chat-list bounds,
  manual run delegates to the service with the chosen count/instruction, link
  run maps to `before_id = message_id + 1`, all four link failure kinds,
  provider failure rendering.
- **Live tests:** open the panel on a real account, run one real chat and one
  public link; confirm the range line matches Telegram.
- **Readiness:** panel + tests pass; live run succeeds or fails honestly.

### P2 — Incremental retrieval and per-chat checkpoints

- **Goal:** never re-summarize an interval.
- **User-visible:** the panel shows "last summarized up to message X at time T"
  and a "Summarize new messages" action.
- **Reuse:** `history_service` (`after_id`), `db/client.run_sync_db`,
  repository + in-memory fallback pattern, `settings_service` validation
  helpers.
- **Likely files:** `backend/services/history_checkpoint_repository.py`
  (proposed), `history_ai_service`/UI glue for the previous-summary context.
- **Dependencies:** P1.
- **Persistence:** logical fields in §6.2; **no migration is written here** —
  the owner applies schema separately.
- **Security/context:** one bounded previous summary only; no full-history
  re-injection.
- **Failure:** provider/retrieval/timeout/DB failure retains the checkpoint;
  empty interval → no provider call, `idle`; overlap blocked by `running`.
- **Acceptance:** a second run with no new messages makes zero provider calls;
  a failed run leaves `last_message_id` unchanged; two concurrent triggers
  result in one execution; the backlog beyond 1,000 is refused, not truncated.
- **Automated tests:** checkpoint advance only on success, retention on each
  failure class, CAS duplicate advance, empty interval, backlog refusal,
  overlap lease, previous-summary budget.
- **Live tests:** run twice in a row on a busy chat; verify the second run has
  nothing to do and the checkpoint equals Telegram's newest id.
- **Readiness:** schema applied by owner; tests + live run pass.

### P3 — Opt-in automatic processing for selected chats

- **Goal:** cadence-driven digests for explicitly enabled chats.
- **User-visible:** per-chat enable/disable, cadence, status, last run, last
  error; no dead toggles.
- **Reuse:** `TaskScheduler` + `TaskExecutionCoordinator` + `ToolExecutor` +
  `TaskRepository` occurrences (claim/CAS/retries/recovery), supervisor
  lifecycle, outcome notifier.
- **Likely files:** `backend/ai/task_contract.py` (internal action kind,
  proposed), `backend/ai/task_execution.py` (dispatch branch, if needed),
  `backend/services/history_digest_service.py` (proposed), panel handlers.
- **Dependencies:** P2 (+ P1 UI).
- **Persistence:** per-chat `enabled`/cadence plus the checkpoint row.
- **Security/context:** only selected chats; AI sees only the bounded history
  text; no cross-chat mixing.
- **Failure:** a failed occurrence retries under the existing attempt policy,
  leaves the checkpoint unchanged, and surfaces the error in the panel.
- **Acceptance:** enabling a chat produces one occurrence per interval; a
  restart recovers without duplicate execution; disabling stops new work;
  failures are visible and do not advance checkpoints.
- **Automated tests:** scheduling/cadence, claim idempotency, restart recovery,
  disabled, failure retention, outcome notification.
- **Live tests:** enable one quiet and one busy chat; observe a real interval,
  one digest, correct checkpoint, then disable and confirm no further runs.
- **Readiness:** no new scheduler; single-authority architecture preserved.

### P4 — Text-first Event Intelligence candidate pipeline

- **Goal:** one deterministic candidate path, no per-message LLM.
- **User-visible:** none yet (candidates are observed, not applied).
- **Reuse:** `task_events` handler position, `extract_event_context`, existing
  chunk/token budgets, `TaskEventDispatcher` idempotency pattern.
- **Likely files:** `backend/services/event_candidates.py` (proposed),
  `backend/ai/task_event_dispatcher.py` (attach point, if chosen),
  `backend/bot/handlers/task_events.py` (minimal hook).
- **Dependencies:** P2 (checkpoint/config) conceptually; can ship independently.
- **Persistence:** candidate dedupe keys; bounded queue (in-memory first).
- **Security/context:** AI gets candidate text only, no Telegram identifiers.
- **Failure:** classification failure drops the batch honestly and never
  invents destinations.
- **Acceptance:** N messages → one bounded classification request; duplicate
  message → one candidate; caps enforced; no provider call for a quiet chat.
- **Automated tests:** filter determinism, batching/caps, ordinal mapping,
  dedupe, no-identifier payload assertion, failure semantics.
- **Live tests:** observe candidate counts and request counts on a real chat.
- **Readiness:** no illegal context leaves the process (payload tests).

### P5 — Integrate candidates with Task, Memory and Saved Items

- **Goal:** approved candidates reach existing destinations.
- **User-visible:** proposals with approve/reject for Task/Saved Items; Memory
  writes per owner policy.
- **Reuse:** `ToolExecutor`/task tools + coordinator, `MemoryRepository`,
  save/retrieve services, existing confirmation/consent rules.
- **Dependencies:** P4.
- **Security:** deterministic validation/authorization before any write; no AI
  execution authority.
- **Acceptance:** approval executes through the existing service exactly once;
  rejection mutates nothing; re-delivery is idempotent.
- **Tests:** routing per destination, authorization, idempotency, rejection.
- **Live tests:** one real task and one real saved item created from approved
  candidates.
- **Readiness:** all writes go through existing services.

### P6 — Selective bounded media inspection

- **Goal:** caption-first, deterministic-signal-only media handling.
- **Reuse:** `media_service.analyze_media`, `media_ai_service`, existing
  `max_download_bytes`/timeouts, `outbound_bandwidth_service` reservation.
- **Dependencies:** P4.
- **Precondition:** re-verify `ocr_available()`/`stt_available()` in the target
  deployment (engines are only registered when a provider is provisioned).
- **Security:** no unconditional download/upload; no media URL/base64 to a
  provider; no video (explicitly unprocessable today).
- **Acceptance:** no caption + no signal → zero download; signal → one bounded
  transfer; provider upload reserved and reported; video refused honestly.
- **Tests:** no-download default, bound enforcement, reservation call, refusal.
- **Live tests:** one image with a caption and one without; confirm inbound-only
  behavior and honest outbound accounting.
- **Readiness:** measured behavior documented; no quota claims.

### P7 — Digest delivery and operational controls

- **Goal:** scheduled digest delivery plus run/health controls.
- **Reuse:** existing panel rendering, outcome notifier, settings service.
- **Dependencies:** P3 (and P2).
- **Acceptance:** digest delivered to the configured destination with range,
  status visible, failures actionable.
- **Tests:** delivery, opt-out, error rendering.
- **Live tests:** one real delivery.
- **Readiness:** no new transport.

## 12. Not implemented / not verified

- No code, listener, scheduler, worker, queue, executor, client or database
  object was added. No SQL, migration, Supabase or `DATABASE_ARCHITECTURE.md`
  change.
- No Telegram session, owner chat or Render deployment is available here, so no
  live panel, link resolution against Telegram, provider call or digest was
  exercised. Every capability claim above is code- or test-backed, not live.
- Bandwidth effects are unmeasured; only the inbound/outbound boundary and the
  single outbound reservation caller are verified.
