# Telegram History Access — Architectural Investigation

> **Investigation only — nothing was implemented.** This document reports the
> source-backed findings of the reusable-Telegram-history investigation. It
> replaces the previous `INVESTIGATION.md` entirely; no earlier content is
> preserved, merged, or appended. No production code, tests,
> `IMPLEMENTATION_REPORT.md`, schema, migrations, configuration, presentation,
> delivery logic, or context-retrieval logic was modified. No `HistoryService`,
> cache, scheduler, client, or polling loop was created.

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Revision audited | `73bbda6` (`feat: add durable invisible AI provenance to AI answers`) |
| Question | Where should reusable, arbitrary-N Telegram history retrieval live, so that "translate/summarize the last N messages" is possible without dumping N messages into one prompt? |
| Verdict | **No reusable history layer exists today.** Two independent, non-overlapping, non-extensible readers exist; the correct boundary is a narrow services-layer history capability over the already-existing `backend/telegram_api` facade, with provenance eligibility owned there — **not** in individual AI tools, and **not** by enlarging the bounded conversational snapshot. |
| Changes made | only this document |

Every claim below is labelled as one of:

- **[CURRENT]** — implemented behavior verified in source at `73bbda6`.
- **[FINDING]** — a conclusion derived from that source, with the evidence cited.
- **[RECOMMENDED]** — a future implementation proposal. **Nothing in this
  section is implemented.**

---

## 1. Scope and method

The investigation answers one architectural question: given a request such as
*"translate the last 500 messages"* or *"summarize the last 1000 messages"*,
which existing layer should own Telegram history retrieval, and how do the
bounded conversational context and that layer relate?

Method: read-only source tracing from the AI trigger handler outward to
Telethon, plus the `telegram_api` facade, the services layer, the dispatcher,
the token-budget module, and the existing tests. No repository-wide search was
performed beyond targeted `grep` for the reader/history entry points named in
the task. No live Telegram, Supabase, or Render access was used.

---

## 2. Current Telegram context architecture

**[CURRENT]** The bounded surrounding-context snapshot is built exactly once per
AI request and threaded forward as data:

```
Telegram NewMessage (outgoing, owner)
  └─ backend/bot/handlers/ai_unified.py::_execute_ai
       request_chat_id    = event.chat_id
       request_message_id = event.message.id
       └─ _load_telegram_chat_context(...)            ai_unified.py:539
            └─ fetch_telegram_chat_context(...)       telegram_context.py:394   ← the ONLY I/O
                 ├─ _read_window(...)                 telegram_context.py:339
                 │    client.iter_messages(chat_id, limit=MAX_CONTEXT_MESSAGES,
                 │                          max_id=message_id)   :346
                 ├─ _resolve_sender_names(...)        (≤ MAX_SENDER_RESOLVES lookups)
                 └─ build_chat_context(...)           telegram_context.py:261   ← PURE
                      └─ _to_record(...)              telegram_context.py:223
       └─ AIRequest(telegram_context=snapshot)        ai_unified.py:668
            └─ engine.execute(request)                bounded by asyncio.wait_for(
                                                       _AI_TIMEOUT = 60.0)  ai_unified.py:62, :687
                 └─ ContextBuilder                        backend/ai/conversation/context_builder.py
                      telegram_chat=snapshot              context_builder.py:203, :238, :290
                      └─ PromptBuilder                    backend/ai/prompt/builder.py
                           if ctx.telegram_chat.is_empty: ...   builder.py:276
                           telegram_block = ctx.telegram_chat.render()  builder.py:289, :409
                           → `[Telegram Chat Context]`
```

Supporting facts, verified:

| Fact | Evidence |
|---|---|
| The snapshot is a **frozen value object**, never persisted, never merged into AI history | `TelegramChatContext` (`telegram_context.py`), module docstring rules |
| Exactly one Telegram read per request | `fetch_telegram_chat_context` → single `_read_window` call (`:339`–`:346`) |
| `build_chat_context` performs **no I/O** | signature + docstring (`telegram_context.py:261`); asserted by `tests/test_telegram_chat_context.py::test_prompt_builder_only_formats_the_already_built_snapshot` |
| `AIRequest.telegram_context` is typed as this snapshot | `backend/ai/session/request.py:36`, `:66` |
| The whole AI turn, including the prompt round and tool rounds, sits inside one `_AI_TIMEOUT = 60.0` | `ai_unified.py:62`, `:619`, `:687` |

**[FINDING]** The snapshot is request-scoped enrichment, not a history API. Its
selection logic is deliberately a single `iter_messages` window anchored on the
triggering message id (`max_id=message_id`), with "never invent future context"
and "newest N win" semantics.

---

## 3. Current Telegram history readers

**[CURRENT]** There are **two** end-user-facing conversation readers plus one
typed facade and one bounded scan helper. They do not share code.

| # | Reader | Location | Retrieval | Output | Provenance-aware? |
|---|---|---|---|---|---|
| 1 | Bounded surrounding window | `backend/ai/conversation/telegram_context.py::_read_window` (`:339`) → `build_chat_context` (`:261`) | `client.iter_messages(chat_id, limit=10, max_id=anchor)` | `TelegramChatContext` (frozen records) | **Yes** — `has_ai_provenance_marker` filter (`:305`) + `strip_ai_provenance_marker` (`:231`) |
| 2 | Recent-message listing tool | `backend/ai/tools/semantic.py::ListRecentMessagesTool.execute` (`:87`, `:142`) | `client.iter_messages(chat_id, limit=limit)` (`:165`), reversed to chronological | `ToolResult.data.messages` = list of dicts | **No** |
| 3 | Typed Telegram facade | `backend/telegram_api/messages.py::iter_messages` (`:144`), `search_messages` (`:172`), `get_messages` (`:95`); exposed on `telegram_api/api.py` (`:71`, `:82`, `:91`) | `client.iter_messages(...)` + `serialize_message` | serialized dicts (`limit` default 100; supports `from_user`, `min_id`) | **No** |
| 4 | Bounded scan helper (services) | `backend/services/delete_service.py::_iter_messages_bounded` (`:43`) | `client.iter_messages` with `rpc_await(..., timeout=5.0)` **per page/RPC** | raw Telethon messages (internal) | **No** (uses ownership, not provenance) |

**[FINDING] All four are independent implementations.** Reader 1 does not use
the facade (it calls the Telethon client directly); reader 2 does not use the
facade either; reader 3 is a thin serializer used by some tools
(`backend/ai/tools/delete.py`, `backend/ai/tools/context.py`) but **not** by
either conversation reader; reader 4 is the only one with per-RPC timeouts.

Additional raw history touch points exist for single-message fetches, not
conversation history: `backend/ai/tools/delete.py:345`, `:553`;
`backend/ai/tools/save.py:108`; `backend/bot/handlers/delete.py:158`, `:233`;
`backend/bot/handlers/save.py:104`; `backend/bot/handlers/misc.py:473`, `:512`,
`:561`; `backend/helper/target_context.py:41`;
`backend/services/database_service.py:54`; `backend/services/save_service.py:506`.

**[FINDING] No shared conversation-history layer exists.** There is no
`HistoryService`, no history repository, no message-normalization utility, and
no pagination abstraction anywhere in `backend/`. The only reusable primitives
that exist are the `telegram_api` serializer and the bounded-iteration pattern
in `delete_service`.

---

## 4. Current limits, and why the bounded context cannot serve N = 500/1000

**[CURRENT]** Hard constants, none user-configurable:

### 4.1 Bounded surrounding context — `backend/ai/conversation/telegram_context.py`

| Constant | Value | Meaning |
|---|---|---|
| `MAX_CONTEXT_MESSAGES` | `10` (`:58`) | surrounding messages per request |
| `MAX_MESSAGE_CHARS` | `200` (`:61`) | per-message truncation, `…` suffix |
| `MAX_TOTAL_CHARS` | `1500` (`:64`) | total text across the block; oldest dropped first |
| `MAX_SENDER_RESOLVES` | `4` (`:67`) | display-name lookups per request |
| `FETCH_TIMEOUT_S` | `3.0` (`:70`) | wall-clock bound for the whole read; any failure degrades to an **empty** context |

### 4.2 Recent-message tool — `backend/ai/tools/semantic.py`

| Limit | Value | Evidence |
|---|---|---|
| `_DEFAULT_CANDIDATES` | `50` | `:32` (default when `limit` is absent) |
| `_MAX_CANDIDATES` | `100` | `:31`; enforced `max(1, min(limit, _MAX_CANDIDATES))` in `execute` (`:142`) and declared as `"maximum": 100` in `parameters` |
| Per-message text | unbounded previews (`text` field) | `execute` builds `data.messages` dicts |
| No timeout wrapper | — | `execute` iterates directly (`:165`) |

### 4.3 Typed facade — `backend/telegram_api/messages.py`

| Limit | Value |
|---|---|
| `iter_messages` default `limit` | `100` (`:147`) |
| Range support | `from_user`, `min_id` only (`:148`–`:156`); **no** `max_id`, no offset/range pair |
| Timeout | none inside the function; `asyncio.TimeoutError` is only translated to `TelegramTimeoutError` if a caller already bounded it |

### 4.4 Bounded scan precedent — `backend/services/delete_service.py`

| Constant | Value | Evidence |
|---|---|---|
| `_MAX_DELETE_SCAN_MESSAGES` | `1000` (`:23`) | proves the codebase already scans 1000 messages — **bounded per RPC, not per request** |
| `_DELETE_RPC_TIMEOUT_SECONDS` | `5.0` (`:25`) | applied to **every** iteration step via `rpc_await` (`:51`–`:54`) |

### 4.5 Prompt/token limits — `backend/ai/prompt/budget.py`

| Cap | Value | Line |
|---|---|---|
| `DEFAULT_MAX_TOTAL_TOKENS` | `8500` | `:25` |
| `DEFAULT_MAX_OUTPUT_TOKENS` | `1000` | `:26` |
| `DEFAULT_MAX_SYSTEM_TOKENS` | `2000` | `:27` |
| `DEFAULT_MAX_CONTEXT_TOKENS` | `4000` | `:28` |
| `DEFAULT_MAX_MEMORY_TOKENS` | `1000` | `:29` |
| `DEFAULT_MAX_TOOL_RESULT_TOKENS` | `1500` | `:31` — **declared and re-exported, never applied anywhere** (only hits: `budget.py:31`, `prompt/__init__.py:85`, `:132`) |
| Estimation rule | 1 token ≈ 4 English chars, ≈ 2 non-English chars | `estimate_tokens` |

### 4.6 Execution-model limits

| Limit | Value | Evidence |
|---|---|---|
| `MAX_TOOL_ROUNDS` | `3` | `backend/ai/engine/dispatcher.py:58`, loop at `:704` |
| Whole AI turn | `_AI_TIMEOUT = 60.0` s | `ai_unified.py:62`, `:687` |
| Tool results injected verbatim into the continuation round | JSON `{tool, success, message, data, error}` | `Dispatcher._build_continuation_messages` (`:1933`–`:1973`) |
| Deterministic listing renderer | `_summarize_tool_results` (`:1794`) → `_render_message_list` (`:1821`) | uses `data.messages` (`:1813`–`:1815`) |

**[FINDING] Why the bounded context cannot be raised to 500/1000.** The snapshot
is not merely capped by a constant; it is *designed* as a proximate, anchored
window and every downstream layer assumes that:

1. **Selection semantics are anchor-bound** — `max_id=message_id`
   (`telegram_context.py:346`) with "never invent future context". A 1000-message
   history request is a *range* request, not "the messages just before the
   trigger", so it is a different query, not a bigger window.
2. **Truncation is lossy by design** — 200 chars/message + 1500 chars total
   (`:61`, `:64`). Translating a 1000-message range at 200 chars/message would
   silently drop most of the content; translation must not be lossy.
3. **The read is a single 3.0 s window** (`:70`) — a 1000-message fetch cannot
   honestly fit that budget, and its failure mode is *silent empty context*
   (`fetch_telegram_chat_context` returns `EMPTY_CHAT_CONTEXT` on any error or
   timeout). Silent emptiness is acceptable for optional enrichment and
   dangerous for an explicit user request.
4. **Everything lands in one prompt** — `PromptBuilder` renders the snapshot into
   one `[Telegram Chat Context]` block (`builder.py:289`) inside the same turn
   that is capped at ~8 500 input tokens / 4 000 context tokens and a 60 s wall
   clock. 1000 messages at ~50 chars each already exceed the total prompt budget;
   mixed Persian/English at 2–4 chars/token makes it far worse.
5. **The value object is frozen and request-scoped** — it deliberately carries no
   pagination cursor, no date range, no ordering contract beyond "chronological",
   and no chunk state, which are exactly the fields a large-range operation needs.

**Conclusion:** raising `MAX_CONTEXT_MESSAGES` would degrade ordinary requests
(more irrelevant context, more truncation loss, higher fetch-failure odds) while
still not producing a valid 500/1000-message operation. The two capabilities are
architecturally distinct.

**[FINDING] The separation already exists conceptually in the source**, even
though the large-history side is unimplemented:

- `telegram_context.py` documents itself as "the bounded surrounding-message
  snapshot … request-scoped … never persisted" and explicitly distinguishes
  itself from `HistoryManager` / `ConversationContext.history` (the AI session's
  own turns) and `ReplyContext` (the replied-to message). It is *not* a Telegram
  history API.
- `ListRecentMessagesTool` is documented as "a bounded window of the REAL recent
  Telegram messages … the first step of semantic delete" — a tool-scoped preview,
  and its `limit` schema is capped at 100.
- `delete_service._iter_messages_bounded` (`_MAX_DELETE_SCAN_MESSAGES = 1000`)
  demonstrates the existing pattern for large history work: iterate in the
  services layer, bound each RPC, keep raw message objects, and never put the
  result into a prompt.

---

## 5. Where the two paths diverge (root cause of the observed gap)

**[CURRENT]/[FINDING]** The surrounding-context path filters AI output; the
tool-rendering path does not:

```
path A (surrounding context)                     path B (list_recent_messages)
telegram_context._read_window                    semantic.ListRecentMessagesTool.execute
  → build_chat_context                                   → raw dicts incl. marked text
      drop if has_ai_provenance_marker (:305)             (no marker check)
      strip marker in _to_record (:231)                   → Dispatcher._summarize_tool_results
  → prompt: marker-free                                    → _render_message_list (:1821)
                                                             text reproduced verbatim (marker invisible)
                                                           → _build_continuation_messages JSON (tool role)
                                                             data.message JSON → provider
```

**[FINDING]** The divergence is therefore not "a missing marker check in a
tool". It is that the provenance *invariant* ("marker text never reaches the
model; AI output is not surrounding human conversation") is enforced only inside
`telegram_context`, while the tool-result → model chokepoint
(`Dispatcher._build_continuation_messages`, `dispatcher.py:1933`) and the
deterministic renderer (`_render_message_list`, `:1821`) inject tool `data`
verbatim.

**[CURRENT] Provenance today is one deterministic pair of helpers plus one
in-process registry:**

| Piece | Location | Role |
|---|---|---|
| `AI_PROVENANCE_MARKER = "\u2061\u2062\u2063\u2064"` | `backend/ai/context/provenance.py:40` | the single authoritative marker literal |
| `has_ai_provenance_marker` / `strip_ai_provenance_marker` / `apply_ai_provenance_marker` | `provenance.py:43`, `:53`, `:64` | substring detection / removal / idempotent append |
| Applied only to the final successful presentation | `backend/ai/tools/delivery.py::apply_presentation_provenance` (`:440`), called from `deliver_response` (`:686`, `:721`) | durable half |
| Read by the bounded collector | `telegram_context.py:305` (drop), `:231` (strip) | exclusion + sanitization |
| `ReplyResolver` | `backend/ai/context/reply_resolver.py` | in-process, RAM-only, LRU-capped mapping of Telegram msg id → AI content — still an in-process optimization, not durable storage |

`backend/ai/context/__init__.py` re-exports the marker helpers, so a shared layer
can consume them without importing `telegram_context`.

---

## 6. Correct architectural boundary

### 6.1 Answers to the required determinations

**A. Is there already a shared history/context layer that should own provenance
eligibility? — [FINDING] No.** §3 proves there is no history layer; §2 proves the
snapshot is the only context layer and that it is bounded and anchor-scoped.

**B. (n/a — no such layer exists.)**

**C. Where does the responsibility belong? [RECOMMENDED]** Two distinct answers,
because eligibility and retrieval are different concerns:

| Concern | Owner | Why |
|---|---|---|
| History **retrieval + normalization + pagination + range semantics + media metadata + message-ID preservation** | a new narrow module in the **services layer** (`backend/services/`), built over the existing `backend/telegram_api/messages.py` facade and the existing `rpc_await`-style per-RPC bounding (`backend/helper/rpc_timeout.py:20`; pattern already used by `delete_service.py:43`) | `AGENTS.md §13.3` (services own business logic, tools are thin wrappers); the facade and the bounded-RPC pattern already exist, so this **extends** an established pattern rather than inventing a new subsystem |
| History **provenance eligibility** (is this message AI output?) | the `backend/ai/context/provenance.py` helpers, consumed by whichever layer materializes history for the model | it is the single authoritative durable signal; re-implementing it per tool would create N definitions of the same predicate |

**D. Why a marker check directly inside `ListRecentMessagesTool` would violate the
current architecture — [FINDING] three source-grounded reasons:**

1. **The tool's own contract requires complete ID coverage.**
   `ListRecentMessagesTool` is documented (`semantic.py:88`–`:94`, `:106`–`:113`)
   as the first step of semantic delete: *"then call `delete_messages_by_ids` with
   only the IDs you saw here. Never invent IDs."* Dropping AI-marked messages
   would make them **undeletable**, because `DeleteMessagesByIdsTool` can only
   act on IDs the listing revealed. Filtering here breaks a documented contract.
2. **It is exactly the per-tool ad-hoc filtering the constraint forbids**, and it
   would have to be duplicated in every future reader (translate, summarize,
   search, export), each with its own definition of eligibility.
3. **The architecture already has a chokepoint.** The tool-result → model path is
   `Dispatcher._summarize_tool_results` / `_render_message_list` /
   `_build_continuation_messages` — the tool-path equivalent of
   `telegram_context._to_record`. Enforcing marker sanitization there matches the
   existing design instead of scattering it.

**E. Is a new abstraction justified? — [FINDING] Partially: retrieval, yes;
provenance, no.** The source proves *no* existing layer can perform
arbitrary-N, range-aware, lossless retrieval (readers 1–4 in §3 are each
unsuitable: 1 is bounded/anchor/lossy, 2 is capped at 100 and contractually
raw-for-delete, 3 has no `max_id`/range and no timeout, 4 is delete-specific).
Provenance, by contrast, already has a correct abstraction
(`backend/ai/context/provenance.py`) and must be **reused**, not reinvented.

### 6.2 Why the alternative locations are wrong

| Candidate | Verdict | Reason (source) |
|---|---|---|
| Enlarge `MAX_CONTEXT_MESSAGES` | **wrong** | §4 — anchor semantics, lossy truncation, 3 s single-shot read with silent-empty failure, single-prompt budget, frozen request-scoped object |
| Proliferate the marker check into every tool | **wrong** | breaks `ListRecentMessagesTool`'s delete-ID contract; duplicates a predicate that already has one authoritative home; contradicts `AGENTS.md §13.3` |
| Put history retrieval inside the AI tool itself | **wrong** | the tool would own Telegram retrieval policy (pagination, timeouts, ordering, media), which the task and `AGENTS.md` both place in the services layer |
| Add a second Telegram client / update loop / scheduler | **not needed** | `RuntimeSupervisor` is the single connection authority; the existing `client` is already reachable through `ToolContext` and the `telegram_api` facade |
| Persist history in Supabase | **not needed / forbidden** | the requirement is retrieval of Telegram history, which is already durable in Telegram; no schema change is justified |

---

## 7. [RECOMMENDED] Relationship between the bounded context and large-history operations

Bounded context stays exactly as it is: one request-scoped, anchored, 10-message,
3 s snapshot, provenance-filtered. Large-history retrieval is a **separate,
explicitly requested operation** that:

1. is never placed into `AIRequest.telegram_context` and never rendered into the
   `[Telegram Chat Context]` block;
2. returns a **normalized message collection** (id, sender, date, text/caption,
   media metadata, provenance flag) instead of a prompt string;
3. is consumed by an AI tool as *data*, then reduced (translated/summarized)
   **outside** the prompt-sized path — chunked, processed, and aggregated — so
   the model only ever sees bounded slices.

**[RECOMMENDED] Data flow — "translate the last N messages"**

```
user request ("translate the last 500 messages")
 → AI tool (thin) parses N / range from arguments
 → history retrieval (services layer) over telegram_api facade:
      chronological fetch, per-RPC bounded, ID-preserving, provenance-labelled
 → eligibility policy applied once, centrally (provenance helpers)
 → chunk into prompt-sized slices (respect MAX_TOTAL_TOKENS / context cap)
 → per-chunk translation through the existing provider path (ProviderManager)
 → reassemble in original message order, one translated block per source message
 → deliver (unchanged presentation path)
```

**[RECOMMENDED] Data flow — "summarize the last N messages"**

```
user request ("summarize the last 1000 messages")
 → same retrieval + eligibility + chunking
 → map step: per-chunk extraction/summary (bounded, provider round per chunk)
 → reduce step: aggregate the chunk summaries into the final answer
 → deliver
```

**[FINDING]** Neither flow exists today: there is **no translation tool/service**
and **no summarization tool/service** (§8). The dispatcher's bounds
(`MAX_TOOL_ROUNDS = 3`, `_AI_TIMEOUT = 60.0`) mean a 1000-message map/reduce
cannot honestly run inside one tool invocation as currently structured; the
source supports bounded per-slice work, not an unbounded in-turn loop.

---

## 8. Existing translation and summarization capabilities

**[CURRENT] Translation: none.** Every repository hit for `translate` /
`translation` is unrelated: a dispatcher comment about translating tool schemas
(`backend/ai/engine/dispatcher.py:350`), digit translation
(`backend/ai/persian.py:25`, `backend/ai/task_candidate.py:50`), and prompt text
instructing the model *not* to translate tool values / user instructions
(`backend/ai/prompt/template.py:88`, `backend/ai/task_interpreter.py:97`, `:452`).
There is no translation tool, service, provider configuration, or test. The model
can translate ad hoc because it is an LLM, but no architecture receives a batch
of Telegram messages for translation, and no message-boundary or order
preservation exists.

**[CURRENT] Summarization of Telegram content: none.** Hits are unrelated:
`UsageSummary` aggregation (`backend/ai/database/usage_reader.py`), the explicit
statement that `HistoryManager` has "no summaries" (`history.py:5`, `:45`), a
config comment about a history budget (`backend/ai/config/defaults.py:15`), and
the dispatcher's `_summarize_tool_results` — which is **deterministic string
assembly of tool results**, not model summarization (`dispatcher.py:1794`).

**[FINDING]** Therefore both target capabilities need the same missing
prerequisite first: a reusable history retrieval + normalization layer. Neither
needs a new provider, executor, or scheduler.

---

## 9. Chunking, pagination, token limits, hierarchical processing

**[CURRENT]** What the architecture already provides:

| Primitive | Location | Reusable for large history? |
|---|---|---|
| Per-RPC timeout wrapper | `backend/helper/rpc_timeout.py::rpc_await` (`:20`) | **Yes** — the right building block for long fetches |
| Generic bounded await with diagnostics | `backend/runtime/operation_watchdog.py::guarded_await` (`:81`) | **Yes** |
| Bounded iteration over history | `backend/services/delete_service.py::_iter_messages_bounded` (`:43`) | **Yes as a pattern** (delete-specific today) |
| Serialized message dicts | `backend/telegram_api/messages.py::serialize_message` (used by `iter_messages` `:144`) | **Yes** — already the facade's output shape |
| Token estimation + caps | `backend/ai/prompt/budget.py` (`estimate_tokens`, caps `:25`–`:31`) | **Yes** for slice sizing; note `DEFAULT_MAX_TOOL_RESULT_TOKENS` is currently unused |
| Provider round abstraction | `ProviderManager` via `Dispatcher` | **Yes** — map/reduce steps are ordinary provider rounds |
| Tool timeouts / long-running exemption | `ToolExecutor` (`long_running=True` skips the generic 10 s tool timeout) | available, but **not** a substitute for an honest multi-step design |

**[FINDING]** Missing pieces for correct chunking: no `max_id`/range pair in the
facade (`iter_messages` supports only `from_user`/`min_id`, `messages.py:148`),
no chunk state, no aggregation stage, no per-operation progress/failure contract
for partially completed ranges. `DEFAULT_MAX_TOOL_RESULT_TOKENS = 1500` is
declared and exported but never enforced anywhere — a large history result
injected into a prompt would not be capped by it today.

---

## 10. [RECOMMENDED] Minimum future change set (not implemented)

| File | Likely change |
|---|---|
| `backend/services/history_service.py` *(new)* | the narrow retrieval/normalization capability: chronological fetch over `telegram_api`, range/count semantics, per-RPC bounding via `rpc_await`, ID + media + provenance-labelled normalized records, explicit failure contract |
| `backend/ai/context/provenance.py` | **no change expected** — consumed as-is; it is already the single authoritative eligibility predicate |
| `backend/helpers`/`telegram_api/messages.py` | possible addition of a `max_id`/range parameter to `iter_messages` (facade currently lacks it) |
| `backend/ai/tools/registry.py` | register thin translate/summarize history tools (thin wrappers only) |
| `backend/ai/tools/<translate|summarize>.py` *(new)* | tool wrappers that parse N/range, call the service, chunk, and drive the provider rounds — no Telegram policy inside |
| `backend/ai/engine/dispatcher.py` | only if the aggregation path needs an explicit multi-step contract; `_build_continuation_messages` (`:1933`) / `_render_message_list` (`:1821`) are the chokepoint for marker sanitization of any tool result that can carry message text |
| `backend/ai/conversation/telegram_context.py` | **no change expected** — stays bounded |
| Tests | extend `tests/test_telegram_chat_context.py` (bounded behavior unchanged) and add focused history-service/tool tests |

**Must not be touched:** `RuntimeSupervisor` and the single Telegram client,
`Taskloom`/`backend/ai/task_scheduler.py`, `ToolExecutor` architecture,
provider architecture, Supabase (`backend/db/`, `supabase/migrations/`),
`DATABASE_ARCHITECTURE.md`, delivery/presentation (`backend/ai/tools/delivery.py`
visual paths), and `backend/ai/conversation/history.py`.

**[RECOMMENDED] Required tests for any future implementation:** count semantics
(exactly N, N > available, N = 0/negative); range semantics with `min_id`/`max_id`
and inclusive/exclusive boundaries; strict chronological ordering after
pagination; message-ID preservation; per-message text integrity (no lossy 200-char
truncation); provenance exclusion and marker stripping on the history path;
marker sanitization at the tool-result → model chokepoint; media metadata
labelling without downloads; per-RPC timeout/failure behavior (partial range →
explicit honest failure, never a silent empty result); no duplicate reads;
concurrency safety for two simultaneous history requests; bounded prompt size per
chunk; ordering preserved in translated/summarized output; and regression tests
proving the bounded surrounding context and the visible presentation are
unchanged.

---

## 11. Risks and safeguards

| Risk | Evidence / mitigation (source) |
|---|---|
| Token/context overflow | caps in `prompt/budget.py:25`–`:31`; `DEFAULT_MAX_TOOL_RESULT_TOKENS` is **not enforced today**, so chunking must size slices explicitly |
| Telegram API cost/latency | `_read_window` today is 10 messages / 3.0 s (`telegram_context.py:58`, `:70`); 1000 messages is ~100× the RPC count — per-RPC bounding (`rpc_await`, `delete_service.py:51`) is the existing answer |
| Memory on large N | `delete_service` already scans up to 1000 messages (`_MAX_DELETE_SCAN_MESSAGES = 1000`, `:23`) without accumulating normalized records; a history layer must stream/chunk rather than materialize 1000 fully-normalized records at once if N grows |
| Whole-turn timeout | `_AI_TIMEOUT = 60.0` (`ai_unified.py:62`) bounds the request; a large map/reduce cannot fit one turn as currently structured |
| Tool-round exhaustion | `MAX_TOOL_ROUNDS = 3` (`dispatcher.py:58`) — multi-step aggregation must not rely on the model chaining many rounds |
| Duplicate Telegram reads | currently one read per request (`telegram_context.py:339`); a history fetch plus the surrounding snapshot for the same turn would double-read — the design must not re-read the same window |
| Concurrent large requests | no queueing exists for history; `asyncio` semaphore guarding exists only for AI requests (`ai_unified.py:619`) |
| Ordering errors | `build_chat_context` sorts by id (`telegram_context.py:261` region); `ListRecentMessagesTool` manually reverses (`semantic.py:168`) — pagination must sort explicitly, not trust Telethon order |
| Message-ID loss | `_render_message_list` prints `[id]` (`dispatcher.py:1830`) and the delete contract depends on it; normalized history must keep ids |
| Media handling | context never downloads media (`TelegramContextMessage.media_type` label only); history must do the same |
| Provenance handling | one authoritative helper set (`provenance.py:40`–`:74`); never re-implement or regex |
| AI-generated messages accidentally included | the durable marker is the only trusted signal (`telegram_context.py:305`); `sender_id`/`out` are explicitly not provenance |
| Very large N requested by the user | requires an explicit cap + honest "requested N, retrieved M" reporting; no such contract exists today |
| Failure halfway through a multi-chunk operation | `fetch_telegram_chat_context` degrades to *silent empty* (`EMPTY_CHAT_CONTEXT`) — acceptable for enrichment, unacceptable for an explicit request; a history layer needs an explicit error contract |
| Visible presentation | unchanged by any of the above; delivery is untouched (`delivery.py:422`, `:440`, `:686`) |

---

## 12. Open questions the source cannot answer

1. Whether a future map/reduce summarization should run inside one AI turn
   (subject to `MAX_TOOL_ROUNDS = 3` / 60 s) or as a durable Taskloom task — no
   source states the intended pattern.
2. Whether `DEFAULT_MAX_TOOL_RESULT_TOKENS = 1500` was intended to be enforced and
   was simply never wired; it is declared and exported but referenced nowhere.
3. Whether the unspecified product policy should also exclude AI-provenance
   messages from *explicit* history listings (`list_recent_messages`), given that
   the same tool is the ID source for deletion — the source proves the trade-off
   but not the decision.
4. Whether per-message translation should preserve message boundaries (one
   translated line per source message) or translate a merged block; only the
   former preserves the ID/ordering contract that delete and review rely on.
5. Whether the facade should grow `max_id`/range support, or whether the history
   layer should page with `min_id` only.
6. What the intended upper bound on N is (the only existing number is
   `_MAX_DELETE_SCAN_MESSAGES = 1000`).

---

## 13. Validation status

| Item | Status |
|---|---|
| Scope honored | only `INVESTIGATION.md` modified |
| Production code / tests / `IMPLEMENTATION_REPORT.md` / schema / migrations / config / `DATABASE_ARCHITECTURE.md` | **untouched** |
| Implementation performed | **none** (investigation + documentation only) |
| New abstraction created | **none** — the `history_service.py` module is a recommendation, not a file |
| Evidence | every claim cites an exact path + symbol/line (§2–§9) |
| `git status` | only `INVESTIGATION.md` changed |
| Tests run | none — no code changed |
| Live Telegram / Supabase / Render verification | **not performed** (out of scope) |

**Proven from source:** the two-reader architecture and its exact call paths and
bounds (§2–§4); the absence of any shared history layer, translation service, or
summarization service (§3, §8); the provenance mechanism and its single
enforcement point in `telegram_context` (§5); the tool-result chokepoint that
bypasses marker sanitization (§5); the execution/token limits that make a
single-prompt 1000-message operation infeasible (§4.5–§4.6, §9).

**Not proven / not measured:** real Telegram latency, RPC cost, and memory for
N = 500/1000 (no live access); whether `_AI_TIMEOUT = 60.0` can be met by any
particular map/reduce design; and the product decisions listed in §12.

---

**No fix or feature was implemented. Only this document was modified.**
