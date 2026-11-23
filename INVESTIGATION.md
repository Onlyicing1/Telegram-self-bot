# INVESTIGATION — Forensic Outbound Bandwidth Audit

> Read-only forensic investigation of every outbound network boundary in the
> Telegram Self-Bot / LifeOS runtime. Source-verified against remote `main`
> `65632bf65297020f63f779e0f42f662a043a3586`. No production code, tests,
> dependencies, SQL, configuration or Render state was changed. The only
> repository change is this file. No live Telegram, Supabase, provider or
> Render API request was made to produce this report.

## 1. Executive summary

The application is a single Python `asyncio` process that talks to four
external families: **Telegram** (self client + optional helper bot),
**Supabase** (PostgREST + Storage), **AI/STT/TTS providers** over HTTPS, and
**Render's metrics API** on demand. Render bills *outbound* bytes only:
HTTP/WebSocket responses plus every byte the service itself sends to the
public internet (Telegram uploads, Supabase requests, provider prompts,
Storage uploads). Inbound traffic is free. Therefore the relevant question is
not "how much data does LifeOS move", but "how much does it *send*".

The existing governor (`backend/services/outbound_bandwidth_service.py`) is a
correct, fail-closed **reservation ledger** for selected payload paths. It is
explicitly not an egress meter (its own module docstring says so), and it
covers neither HTTP responses nor Telegram protocol traffic, helper-bot
traffic, Supabase request/response traffic, Groq/Speechmatics STT uploads,
provider prompts, or the appearance image endpoint.

The single most important quantitative fact in the supplied evidence is that
**the six days 2026-11-07..12 account for ~3,675.8 MB of the ~4,155.1 MB
month-to-date total (≈88.5 %)**. The governor's commit (`9d220b6`,
"feat(bandwidth): add persistent fail-closed outbound bandwidth governor")
is dated **2026-11-11**, so 2026-11-07..10 had no outbound cap at all. Even
the post-governor daily figures (380.8 MB and 410.4 MB on Nov 11 and 12) are
2.5–2.7× the governor's hard 150 MB/day ceiling, which means a material part
of the spike was outside the governor's coverage (or predates its
deployment — commit date is not deployment time).

No source-level evidence can attribute those bytes to one subsystem. Two
measurements settle it without a code change: Render's
`GET /v1/metrics/bandwidth-sources` endpoint (trafficSource = `http`,
`websocket`, `nat`, `privatelink`, `total`) and the dashboard's Network
Metrics graph, which already splits HTTP responses from service-initiated
traffic. The report ranks the candidate families and specifies exactly which
measurement separates them.

Highest-ranked candidates, in order:

1. **Telegram media uploads** (Deep Save, album upload, bulk re-send,
   file-name re-upload, AI result attachments). One 10-member album at the
   default 50 MB member limit is ~500 MB in a single operation; the same
   album at the maximum 500 MB setting is ~5 GB. These paths are governed
   today but were ungoverned before the Nov 11 governor.
2. **Appearance image HTTP egress.** Every Glass panel rendered with the
   preview active carries an `InputBotInlineResult` whose web document is
   `/appearance/image/<version>?fmt=jpeg` (`appearance_service.py:599-619`).
   Telegram fetches that URL from Render; the endpoint can return up to
   `TELEGRAM_PHOTO_MAX_BYTES = 5 MB` per fetch
   (`appearance_service.py:136`), plus a 320 px thumbnail. The response has
   a one-year immutable cache header, but each newly created panel message is
   a fresh web-document fetch by Telegram's servers. This is the largest
   plausible *HTTP-response* contributor and is ungoverned.
3. **Dashboard/API polling.** The React dashboard loads `/api/saves`,
   `/api/bio` and `/api/logs` every 30 s (`src/App.tsx:84`). `/api/saves`
   uses `select("*")` with a second `count=exact` query
   (`backend/db/client.py:456-475`); `/api/logs` also uses `select("*")`
   (`backend/db/client.py:1965-1975`). With no compression middleware and an
   unauthenticated API, a tab left open is a continuous response-byte
   stream. Arithmetic: at 30 s cadence there are 2,880 polls/day, so
   100 KB per poll is ~288 MB/day; 250 KB is ~720 MB/day. Per-row sizes were
   not measured, so this is an explicit conditional, not a measurement.
4. **Groq/Speechmatics STT uploads** (ungoverned). Audio is bounded at
   `MAX_STT_INPUT_BYTES = 20 MB` (`media_service.py:246`); Groq can send the
   same audio up to three sequential passes (`groq_stt_engine.py:609-631`,
   owner-configured 1–3), so one operation can be up to ~60 MB. Gemini is the
   default STT engine and *is* governed; Groq/Speechmatics are opt-in.
5. **AI provider prompt fan-out.** One user request can produce up to six
   `ProviderManager.chat()` calls (initial, empty-response retry,
   structured-action recovery, and up to three tool continuations,
   `dispatcher.py:72,696,781,1052`). Each call fans out over every eligible
   `(provider, model)` candidate, each with one same-model retry
   (`manager.py:66,919,1083`). The durable candidate pool is bounded at 256
   models/provider and 32 providers (`candidate_pool.py:57-59`), and a
   restored pool contains only models a complete Test Modules run proved
   usable. Prompts are on the order of 10–35 KB of text, so this is a
   *multiplier* risk rather than a primary byte source; it is bounded in
   wall-clock time by the 240 s request envelope
   (`ai_unified.py:72`).
6. **History summarization** (map/reduce over chunked Telegram history,
   `history_ai_service.py:93,118-126`) — many provider calls, each ~4k tokens
   of history, paced at 5 s; ungoverned but moderate.
7. **Supabase request traffic** — many small outbound requests, responses
   inbound. It is a request-count/CPU cost, not a credible GB-scale source.
8. **Telegram RPC, keepalive, panel edits, profile cron** — small control
   traffic.

Nothing in this report claims bandwidth was reduced. The deliverable is the
evidence-based diagnosis and the measurement/optimization plan.

## 2. Repository pre-flight and exact inspected commit

| Item | Value |
|---|---|
| Branch | `main` |
| Local HEAD | `c55efcdfd1905d72df35810cb5669e9d5c66866e` |
| Remote `main` (`git fetch` + `git ls-remote`) | `65632bf65297020f63f779e0f42f662a043a3586` |
| Relationship | local HEAD is an ancestor of remote main (18 commits behind; 0 ahead) |
| Inspected tree | remote `main` = `65632bf` (extracted read-only to a scratch directory and removed before delivery) |
| Expected reference commit | `65632bf65297020f63f779e0f42f662a043a3586` — matched exactly |

Pre-existing working-tree changes present **before** this task and preserved
untouched (not staged, not committed):

```
 M LIFEOS_IDEAS.md
 M ROADMAP.md
 M backend/bot/handlers/retrieve.py
 M backend/bot/handlers/taskloom.py
 M backend/helper/selection_state.py
 M tests/test_retrieve_multi_select.py
?? tests/test_taskloom_selection.py
```

`git diff --stat` for those files: 601 insertions / 99 deletions across six
tracked files plus a 487-line untracked test. They overlap files that also
changed upstream, which is why the local branch was not force-synchronised.

Documents read before the source audit: `AGENTS.md`, `INVESTIGATION.md`
(previous Phase 3B content), `IMPLEMENTATION_REPORT.md`, `ROADMAP.md`,
`AI_MASTER_DESIGN.md`, `DATABASE_ARCHITECTURE.md` (targeted sections only),
plus Render's published outbound-bandwidth, service-metrics, and API
documentation. Where documentation and code disagreed, code at `65632bf`
won.

Safety: no SQL executed, no Telegram/Supabase/provider/Render request made,
no Render shell, no environment variable read or printed, no secret in this
document. The only repository write is this file.

## 3. Confirmed network architecture

Single process: `backend/main.py` → `RuntimeSupervisor`
(`backend/runtime/supervisor.py`) which owns one Telethon self client built
from a `StringSession` (`backend/bot/client.py::build_client`), an optional
Telethon helper **bot** client (`backend/helper/client.py::build_helper`), a
FastAPI/uvicorn server started in-process
(`supervisor.py::_start_web_server`, `backend/web/app.py`), one task
scheduler (`backend/ai/task_scheduler.py`), the profile cron, heartbeat,
keepalive, failsafe, diagnostics and memory-cleanup loops.

Destinations and directions (confirmed from code):

| Destination | Protocol | Direction | Render-billed? |
|---|---|---|---|
| Telegram DCs (self client) | MTProto/TCP | send + receive | **send is outbound**; receive is inbound/free |
| Telegram DCs (helper bot) | MTProto/TCP | send + receive | **send is outbound**; receive inbound |
| Supabase PostgREST | HTTPS/1.1 | request + response | **request bytes outbound**; response inbound |
| Supabase Storage | HTTPS | upload / download | **upload outbound**; download inbound |
| AI providers (OpenAI-compatible, Gemini, You.com) | HTTPS | request + response | **request outbound**; response inbound |
| STT providers (Gemini, Groq, Speechmatics) | HTTPS | upload audio + poll + transcript | **upload/poll outbound**; transcript inbound |
| TTS providers (Gemini, OpenAI, Grok, Speechmatics) | HTTPS | request + audio response | **request outbound**; audio inbound |
| Render metrics API | HTTPS | GET | outbound (small, on demand) |
| Browsers/Telegram web-document fetcher → FastAPI | HTTP(S) | response | **HTTP response outbound** |

Render's own documentation confirms the accounting: outbound bandwidth is
"all network traffic sent by your services to destinations outside of
Render", including HTTP responses, WebSocket responses and service-initiated
traffic over any protocol; inbound bandwidth is free; the monthly included
amount is per workspace and shared across services. That is why a Telegram
upload and a Supabase `POST` count, while a Telegram download does not.

## 4. AI provider traffic findings

### 4.1 Request lifecycle

`backend/bot/handlers/ai_unified.py::ai_unified_handler` (line 1014) fires on
every outgoing text message. It calls `_load_triggers(owner_id)` (line ~1030)
**before** deciding whether the message is an AI request, so every outgoing
message performs one `ai_config` Supabase read. Once activated, the request
runs through `_execute_ai` (240 s envelope, `_AI_EXECUTE_TIMEOUT`,
`ai_unified.py:72`; a 60 s semaphore wait at line 715), which delegates to
`backend/ai/engine/engine.py::Engine.execute` → `Dispatcher.dispatch`.

The dispatcher builds the prompt once
(`dispatcher.py::_build_messages`, line 2280) and then performs:

1. one initial `ProviderManager.chat()` (line ~660);
2. one empty-response nudge retry when a "successful" response carries no
   text and no tool call (`AI_PROVIDER_EMPTY_RESPONSE_RETRY`, line 696);
3. one structured-action recovery retry when prose cannot be resolved into a
   tool call (`AI_ACTION_RECOVERY_RETRY`, line 781);
4. up to `MAX_TOOL_ROUNDS = 3` tool rounds (line 72), each followed by a
   continuation provider call that re-sends the original messages **plus**
   the assistant tool-call message and the tool results
   (`_build_continuation_messages`, line 2295; call site line 1052).

So one user AI request can produce **up to six `ProviderManager.chat()`
invocations**, and every one of them carries the full system prompt, tool
contract, conversation state and history.

### 4.2 Provider fan-out, retries and fallback

`ProviderManager.chat()` (`manager.py:134`) builds a **model-level** pool: for
each eligible provider, `_candidate_models` (line 919) returns the configured
model first, then every discovery-fed candidate model, with no per-provider
cap. `_attempt_with_retry` (line 1083) gives each `(provider, model)` pair at
most `_MAX_RETRIES = 1` retry (line 66) for `network`/`timeout`/`server`, or
for a 429 whose `Retry-After` is ≤ 5 s (`_RATE_LIMIT_MAX_WAIT_SECONDS`,
line 70). A rate limit with unknown scope may spend one sibling-model probe
per invocation. Any other failure advances to the next candidate.

The pool is supplied from two places:

* the live Test Modules run (`model_tester.py:659` → `set_model_candidates`,
  `manager.py:977`);
* a durable document restored once at boot (`candidate_pool.py::restore`,
  called by the supervisor), bounded by `MAX_PROVIDERS = 32`,
  `MAX_MODELS_PER_PROVIDER = 256`, `MAX_AGE_S = 24 h`.

Consequences:

* **Same prompt can be transmitted many times.** The preview/dead-model
  paths (`model_not_found` → mark unavailable → next model) make a provider
  outage walk the whole candidate list. Each attempt re-serialises the full
  messages array.
* **Fallback is cross-provider.** `_ordered_candidates(include_all=True)`
  appends every other registered provider, so a configured fallback chain is
  only the ordering hint, not the limit.
* **The worst case is bounded by time, not by an attempt counter.** Each
  provider HTTP call has a 30 s `_PROVIDER_RPC_TIMEOUT` (manager constant),
  and the whole request has a 240 s envelope, so at most ~8 genuine 30 s
  timeouts can fit; however, fast failures (429/404/400) return in
  milliseconds and can walk dozens of candidates inside that envelope.
* **No global request-level attempt cap exists.** This is the one place in
  the runtime where a single user action can fan a full prompt out to many
  destinations.

### 4.3 HTTP clients and serialisation

* `backend/ai/providers/openai_compat.py` creates one `httpx.AsyncClient` per
  provider instance (line 97) and performs a single `POST
  {base_url}/chat/completions` per attempt (line 138), JSON body containing
  `model`, `messages`, `temperature`, `max_tokens`, optional `tools` and
  `response_format`. `list_models()` (line ~319) is a separate GET.
* `backend/ai/providers/gemini.py` uses its own `httpx.AsyncClient` (line
  179) and posts the translated payload (line 307); model listing is a GET
  (line 426).
* Provider adapters do **not** retry internally — retry/cooldown/fallback is
  owned by `ProviderManager` (comment at `openai_compat.py:126`).
* Responses are small (text + tool-call JSON + usage counters). The large
  response path is Gemini media (`gemini_media_engine.py`), which downloads
  the generated file (line 1578) — inbound — after an upload that **is**
  governed (lines 968, 970, 1052, 1221).

### 4.4 Prompt size and history

`backend/ai/prompt/builder.py` assembles a fixed-order package. Relevant
bounds and facts:

* `DEFAULT_MAX_TOTAL_TOKENS = 8500`, `DEFAULT_MAX_HISTORY_TOKENS = 4000`,
  `DEFAULT_MAX_OUTPUT_TOKENS = 1000` (`prompt/budget.py:32-45`).
* The tool contract is rendered into the prompt before the budget is computed;
  the dispatcher states the full registry renders "~1.9k tokens"
  (`dispatcher.py:~575`).
* Only conversation **history** is trimmed (`_trim_history_to_budget`); system
  rules, tool contract, reply context, Telegram window and the current request
  are never evicted. The total estimate is reported, not enforced as a network
  bound.
* Token count is not byte count. `json.dumps` of the messages array, HTTP
  headers, provider-specific wrappers (e.g. Gemini `functionDeclarations`) and
  non-ASCII payloads all add bytes. The existing trace records
  `request_size = sum(len(str(content)))` in **characters**
  (`manager.py::_call_once`), not bytes.

### 4.5 Discovery, health checks and model testing

* `ProviderManager.health()` is **local** (API key present + enabled). It does
  not make an HTTP request.
* Model/provider discovery is **on demand only**, with a 600 s in-memory TTL
  (`model_discovery.py:52`, `_CACHE_TTL`), used by the AI panel and the web
  API (`/api/ai/providers`, `/api/ai/models`, `/api/ai/models/{provider}`).
* `backend/ai/model_tester.py::test_all_models` is triggered by the panel or
  `POST /api/ai/test-models`; it can send a real request to every model of
  every configured provider. This is a burst path, not a periodic one, and it
  is ungoverned.
* There is **no periodic provider polling** in the supervisor, heartbeat or
  failsafe.

### 4.6 Structured answer to the section 2.A questions

1. **HTTP requests per user AI request:** 1 initial + 1 empty retry + 1
   action-recovery retry + up to 3 continuations = up to 6 engine-level
   calls; each is multiplied by the model/provider candidate fan-out and the
   one same-model retry. There is no single fixed number.
2. **Failure conditions that retransmit the same prompt:** retryable
   network/timeout/5xx (one retry), 429 with short Retry-After (one retry),
   empty but successful response (nudge retry), unparseable prose when tools
   are on (recovery retry), dead model (next candidate), and every tool round
   (continuation re-sends the whole prompt).
3. **Can fallback send a large prompt to multiple providers?** Yes — that is
   exactly what `_ordered_candidates(include_all=True)` plus the model pool
   do. There is no byte or attempt cap, only the 240 s envelope.
4. **Are tool results/history unnecessarily repeated?** Yes, structurally:
   every continuation re-sends the original system prompt, tool contract,
   conversation state, history and the Telegram window, then appends the tool
   results. This is the normal protocol shape, but it is the dominant
   per-request multiplier.
5. **Periodic discovery/health requests?** No. Both are on demand.
6. **Which request bodies can become large?** Full prompts with a big tool
   contract and 4k tokens of history (largest routine bodies); Gemini media
   uploads and TTS requests (governed/limited); provider fan-out multiplies
   the prompt body.
7. **Which operations download large responses into Render?** Provider chat
   responses are small. Gemini media file download, Supabase query/Storage
   reads, Telegram media downloads and TTS audio are the large inbound paths
   — inbound is free and is **not** part of Render outbound, but it is
   immediately followed in several workflows by an outbound re-upload.

Confidence: request counts and retry mechanics **confirmed** from source;
byte sizes per prompt **estimated** (token/character evidence only); actual
provider fan-out width in production **unknown** (depends on the restored
candidate pool and provider health).

## 5. Supabase and database traffic findings

`backend/db/client.py` is the single Supabase singleton with an in-memory
fallback. Heavy calls run through `_run_sync` on a bounded worker pool; the
client is pinned to HTTP/1.1 (`backend/db/transport.py:48-96`) so concurrent
workers do not share one multiplexed HTTP/2 socket. Every query is a
`PostgREST` HTTPS request; **request bytes are Render egress**, response
bytes are inbound and free.

Operations by category:

| Subsystem | Operation (file/function) | Direction | Size character |
|---|---|---|---|
| AI config | `config_store.get_config` → `ai_config select("*")` per AI request; `_load_triggers` also reads it per outgoing message | Render → Supabase | small request, row-sized response (system_prompt can be KB) |
| Conversation | `dispatcher.restore_history`; `ConversationManager` message/session read/write | both | request small; responses history-sized (bounded) |
| Preferences | `_load_preference_record` / `_load_preferences` per AI request | request | small |
| Memory | `MemoryManager` long/permanent/short reads and expiry cleanup | request | row-sized |
| Persistence | `backend/ai/persistence.py` schedules message, usage (`usage_recorder.record_usage`), tool-history and provider-stats writes; dispatcher call site ~1510-1533 | request | one insert/upsert per record |
| Provider stats | `provider_stats_repository.py:188` upsert on `(provider_name, owner_id)` | request | small |
| Saved items | `db_client.list_saves` → `saved_items select("*")` + exact count; `query_save` | request | response row-sized, can be large |
| Logs | `db_client.list_logs` → `bot_logs select("*")` limit 100 | request | response up to 100 rows |
| Settings | `panel_settings_repository.load/update_field`; `settings_service` cache-first | both | small |
| Bandwidth ledger | one `panel_settings` JSONB read + write + confirm read per reservation | both | 3–4 round trips per governed transfer |
| Tasks | `task_repository` queries (section 6) | both | `select("*")` rows |
| Storage | `appearance_service._upload_sync`, `anti_edit_delete_service` upload/remove, `appearance_service.image_payload` download/list | upload = **outbound**, download = inbound | uploads governed; downloads inbound |

Confirmed issues:

1. **N+1 in `/api/db/stats`.** `backend/observability/db_stats.py:34-42`
   lists up to 10,000 sessions and then calls `mgr.message.count(session_id)`
   once per session. That is one HTTP request per session on every call to
   the endpoint. It is response-light (counts) but request-heavy, and it is
   reachable from an unauthenticated GET.
2. **`select("*")` on list endpoints.** `/api/saves` selects every column of
   50 rows and then issues a second `count=exact` query
   (`db/client.py:456-475`); `/api/logs` selects every column of 100 rows
   (`db/client.py:1965-1975`). Callers receive columns they do not render.
3. **Repeated config read per outgoing message.** `ai_unified_handler` reads
   `ai_config` before the trigger check; a non-AI message still costs one
   Supabase request.
4. **One extra durable read/write per governed transfer.** The governor
   reads the row, writes it, then re-reads to confirm
   (`outbound_bandwidth_service.py:233-300`), and the repository's `load`
   uses `select("*")` on a JSONB-bearing row
   (`panel_settings_repository.py:35-48`).
5. **Retry/degradation shape.** Each repository wraps its call and falls back
   to an in-memory store on failure; there is no retry storm, but a failing
   database silently moves work into process memory, which can change later
   traffic patterns (e.g. a governor read failure denies a transfer).

What is **not** a credible GB-scale outbound source: PostgREST request text
and headers are on the order of a few hundred bytes to a few KB per call.
Even thousands of calls per day sum to megabytes, not gigabytes. A
`select("*")` response is inbound (free). Storage **uploads** are governed;
Storage **downloads** are inbound.

Confidence: query shapes and directions **confirmed**; per-request byte
counts **not measured**.

## 6. Task scheduler and background traffic findings

The real implementation is `backend/ai/task_scheduler.py` (one
`TaskScheduler` created by `RuntimeSupervisor._start_task_scheduler`) backed
by `backend/ai/database/task_repository.py`; execution is handed to
`backend/ai/task_execution.py` through the existing `ToolExecutor`. There is
no second scheduler, worker pool or task lifecycle.

Confirmed loop behaviour:

* Wake interval is `WAKE_INTERVAL_SECONDS = 60.0` (upper bound); the sleep is
  shortened to the nearest known boundary (`_sleep_seconds`), floor 1 s.
* Each wake runs `_run_due_retries` (one `list_due_retry_occurrences` query,
  limit `MAX_RETRIES_PER_WAKE = 10`) and then `_run_due_sweeps`: up to
  `MAX_SWEEPS_PER_WAKE = 20` calls to `list_due_tasks`
  (`MAX_TASKS_PER_WAKE = 10`), stopping at the first sweep with no due task.
  `_sleep_seconds` then issues `next_run_hint`. **Idle cost is therefore
  ~2–3 Supabase queries per minute**, with no provider or Telegram traffic.
* `list_due_tasks` and `list_due_retry_occurrences` use `select("*")`
  (`task_repository.py:1256`, `:1409`), so each due row carries the task's
  `actions` JSON and `ai_instruction`; `next_run_hint` correctly selects only
  `next_run_at` (`:1260`).
* Execution adds round trips: `create_occurrence` first reads the task and
  the occurrence, then inserts (`task_repository.py:1343+`);
  `claim_occurrence` reads then CAS-updates (`:1431+`);
  `advance_next_run` is a CAS update; terminal transitions are CAS updates.
  A due task therefore costs several small requests, not one.
* AI-assisted tasks prepare ahead within
  `PREPARE_AHEAD_HORIZON_SECONDS = 120` (bounded by
  `PREPARE_AHEAD_TIMEOUT_SECONDS = 150`). Preparation is **one provider
  call** (content generation + deterministic validation, no side effects)
  per occurrence; the boundary re-proves and executes it. A recurring AI task
  therefore adds one provider prompt per period, plus the execution call.
* Retries are bounded at 3 attempts (`MAX_ATTEMPTS` in
  `backend/ai/retry.py:6`); recovery scans at most
  `MAX_RECOVERY_PER_START = 100` occurrences once at startup.
* Executions run with `MAX_CONCURRENT_EXECUTIONS = 4`, so a burst of due
  tasks can produce four simultaneous provider/Telegram operations.
* Event-triggered tasks are read by `list_event_tasks` from the existing
  message-update path (`task_events.py`), not by polling.

No periodic operation fetches full Telegram history, calls a provider, or
uploads media when there are no due tasks. The scheduler's contribution to
outbound bytes is small in idle operation; it matters when AI-assisted tasks
run (provider prompts) or when a task's tool performs a media/HTTP action.
## 7. HTTP server and dashboard egress findings

`backend/web/app.py` is the whole FastAPI application. Routes (confirmed):

| Route | Handler | Notes |
|---|---|---|
| `GET /health` | `health` (line 34) | 17-byte JSON; Render health checks |
| `GET /api/status` | `api_status` (39) | runtime status |
| `GET /api/ai/stats` | `api_ai_stats` (45) | RAM metrics |
| `GET /api/db/stats` | `api_db_stats` (51) | **N+1** count per session, up to 10k (section 5) |
| `GET /api/health/snapshot` | (57) | small |
| `GET /api/performance` | (63) | aggregates |
| `GET /api/diagnostics/events` | (69) | up to `limit` events (default 50) |
| `GET /api/maintenance` | (76) | **runs cleanup** (`run_all_maintenance`) on a GET |
| `GET /api/saves` | `list_saves` (82) | `select("*")` 50 rows + exact count |
| `GET /api/saves/{code}` | (92) | one full row |
| `GET /api/bio` | (106) | state row |
| `GET/PATCH /api/settings` | (116/125) | cache + single write |
| `GET /api/logs` | (139) | `select("*")` 100 rows |
| `GET /api/ai/providers` | (149) | discovery (metadata) |
| `GET /api/ai/models` | (156) | discovery + `fetch_models` for **every configured provider**, concurrently |
| `GET /api/ai/models/{provider}` | (188) | discovery for one provider |
| `GET/POST /api/ai/*` | (210-291) | config, runtime selection, `test-models` |
| `GET /appearance/preview/{version}` | (323) | Open Graph HTML |
| `GET /appearance/image/{version}` | (333) | up to 5 MB JPEG or thumbnail, `Cache-Control: public, max-age=31536000, immutable` (352, 362) |
| `GET /guide/*` | `mount_static` | static guide site |
| `GET /assets/*`, `GET /{path}` | `mount_static` | SPA build + fallback |

Confirmed egress characteristics:

1. **No authentication on any API route.** `_owner_id` is a module global set
   by the runtime; any client that reaches the service URL can read saved
   items, logs, settings and diagnostics, and can trigger maintenance,
   discovery and the model tester. This is a security finding and a
   bandwidth-amplification amplifier (an unauthenticated caller can trigger
   provider traffic).
2. **No compression middleware.** There is no `GZipMiddleware` (or any
   middleware) in `backend/web/app.py` or `backend/main.py`; JSON responses
   and the static SPA/guide assets are sent uncompressed.
3. **Dashboard polling.** `src/App.tsx:84` sets a 30 s interval that calls
   `api.saves()`, `api.bio()` and `api.logs()` in parallel. `src/lib/api.ts`
   issues `GET /api/saves?limit=50&offset=0`, `/api/bio`, `/api/logs?limit=100`.
   The dashboard is the only browser client; there is no WebSocket.
4. **`select("*")` responses.** `saved_items` rows carry captions, metadata
   JSON and file fields; `bot_logs` rows carry messages. The payload is
   proportional to stored content, not to what the page renders. Per-row
   bytes were not measured.
5. **Appearance image endpoint.** The stored original is up to
   `MAX_IMAGE_BYTES = 26,214,400` (25 MB, `appearance_service.py:108`); the
   Telegram photo rendition is capped at `TELEGRAM_PHOTO_MAX_BYTES = 5 MB`
   (`:136`); the thumbnail is 320 px / quality 80 (`:142-143`). Every fetch
   of the rendition performs `image_payload`/`telegram_photo`
   (`appearance_service.py:1519,1629`), which may download the object from
   Supabase Storage (inbound) and encode JPEG on the Render CPU, then sends
   the bytes out (outbound). The one-year immutable cache header helps only
   clients that honour the URL; Telegram's web-document fetcher is a
   server-side fetcher.
6. **Static surface.** `guide/` is ~88 KB total (`index.html` 48 KB,
   `styles.css` 16 KB, JS ~16 KB). The built SPA bundle is not committed
   (`dist/` is produced at deploy time). A normal page load is a few hundred
   KB; a reload loop would be material, an idle tab is not.
7. **`/health` is negligible** (17 bytes) even at Render's health-check
   cadence.
8. **`/api/ai/models` and `/api/ai/test-models` are external-traffic
   amplifiers**: a single GET fans out to every configured provider's model
   list; a single POST runs a real request against every model in the
   catalogue (`model_tester.test_all_models`).
9. **`/api/maintenance` performs writes/cleanup** (expired memory deletion,
   diagnostics trimming, repository validation) on an unauthenticated GET.
   It is response-light but side-effectful.

HTTP responses are billed by Render 1:1. The only current per-response byte
observability is the web server's access log (uvicorn default), which records
status and path, not bytes.

## 8. Telegram traffic findings

Two Telethon clients exist: the owner's self client and the optional helper
bot. Both use `auto_reconnect=True`, `connection_retries=5`,
`retry_delay=2`, `flood_sleep_threshold=60`
(`backend/bot/client.py:30-43`, `backend/helper/client.py:61-71`). Telethon's
default `catch_up=True` means a reconnect fetches the update difference
(inbound). Reconnect logic is centralised in `RuntimeSupervisor`
(`_trigger_reconnect`, `_trigger_full_recovery`, `_hard_reset_runtime`) with
a cooldown; keepalive uses `get_me` every 60 s
(`backend/runtime/keepalive.py:28,118-121`); heartbeat (30 s) and failsafe
(15 s) are local and make no network call.

Per-message handler fan-out (confirmed registrations):

| Handler | Scope | Network/database work |
|---|---|---|
| `backend/bot/router.py` | outgoing + all | dispatch only |
| `ai_unified.py::ai_unified_handler` (1014) | outgoing text | `ai_config` read per message; AI path only on trigger/reply-to-AI |
| `auto_reaction.py` (37) | outgoing | cache-first settings; one reaction RPC when eligible |
| `emoji_replacement.py` (38) | outgoing | cache-first settings; replacement may send/delete a message |
| `intelligent_reaction.py` (52) | outgoing | one AI request per coalesced batch when enabled (default OFF, probability 25) |
| `anti_edit_delete.py` (512) | **all messages** | resolves peer, downloads media (inbound), uploads a bounded snapshot to Supabase Storage (governed) |
| `task_events.py` → `private_message.enforce_incoming` (67) | incoming/private policy | per-message policy/state; AI reply when enabled (default `ai_response_enabled: False`), one CAS claim per reply |
| `task_events.py` (62) | all | event-task matching, bounded reads |
| `misc.py` (1136) | outgoing | `Menu` panel |
| `tasks.py` (95) | outgoing | task panels |

Direction facts:

* **Media downloads are inbound** (`save_service.py` `download_media` at
  1589/1889, `retrieve_service.py:1336`, `appearance.py:469`
  `iter_download`, `anti_edit_delete_service.py:1621`). They are free on
  Render but are typically followed by a governed outbound upload.
* **Media uploads are outbound**: `save_service` `upload_file` (1936) +
  `send_file` (1667/1980), `retrieve_service` `send_file` (1351),
  `telegram_api/media.py` `send_file` (152, voice) and `ai/tools/delivery.py`
  `send_file` (734). All five reserve before sending (section 10).
* **Inline panels** are sent as inline-bot results
  (`backend/helper/inline_sender.py::send_inline_panel`,
  `panel_render.py:155-189`). When the appearance preview is active the
  result is a `photo` web document pointing at the Render URL
  (`appearance_service.py:408-460,599-619`), so Telegram's servers fetch that
  URL from Render. This is the mechanism that turns panel usage into HTTP
  response egress.
* **Panel edits and timers**: `backend/helper/panel_timer.py` edits a panel
  every `_COUNTDOWN_INTERVAL = 30` s while one is open (line 21); lifecycle
  edits go through `_safe_edit`; the callback gate paints at most one edit
  per message at a time. These are small MTProto/HTTP payloads but a
  continuously open countdown panel is a steady low-rate stream.
* **Profile cron** wakes at every minute boundary
  (`backend/profile/scheduler.py:149-155`) and issues one `UpdateProfile`
  RPC when Bio/Username is active. Small, but persistent.
* **Helper bot**: optional; disabled when `BOT_TOKEN` is empty. It is a
  Telethon client (MTProto), so every panel send/edit and callback answer is a
  small outbound RPC — a higher request count than the self client, not a
  separate HTTP Bot API.

There is no evidence in the runtime of a media upload that bypasses
Telegram's own upload path, no CDN download-and-reupload loop, and no
periodic Telegram poll beyond keepalive and reconnect catch-up.

## 9. Other external network findings

* **You.com web search** (`backend/ai/providers/you_search.py:97-178`): one
  HTTPS POST per `web_search` tool call. Request is the query + bounded
  options; response is search results (inbound). Not governed; small.
* **TTS providers** (`gemini_tts_engine.py:552`, `openai_tts_engine.py:456`,
  `grok_tts_engine.py:465`, `speechmatics_tts_engine.py:421`): one HTTPS POST
  per synthesis; request body is the text (bounded), response is audio
  (inbound, `MAX_TTS_AUDIO_BYTES` enforced in `_extract_audio`). The audio is
  then uploaded to Telegram through the governed voice path.
* **Render metrics API** (`render_bandwidth_service.py:131-183`): one
  `GET https://api.render.com/v1/metrics/bandwidth` with an 8 s timeout,
  only when the Bandwidth panel is rendered/refreshed. Response is JSON
  (inbound); request is small.
* **Model discovery** (`backend/ai/discovery.py:402`,
  `backend/ai/model_discovery.py:382,459`): one GET per provider, on demand,
  600 s cache.
* **`backend/tools/stt_benchmark.py`**: a development benchmark tool, not
  wired into the runtime; excluded from production egress.
* **DNS/TLS/connection setup** for every HTTPS client is unmeasured but
  small relative to payloads.

No other network library is used: the scan for `httpx`, `requests`, `aiohttp`,
`urllib`, `websockets` and `socket` found only the files listed above (plus
`urllib.parse` for URL parsing, which performs no I/O).
## 10. Existing bandwidth governor coverage and gaps

`backend/services/outbound_bandwidth_service.py` is the single reservation
authority. Limits are exact and must not be changed silently:
`DAILY_LIMIT_BYTES = 150_000_000` (line 45) and
`MONTHLY_LIMIT_BYTES = 4_000_000_000` (line 46). State is one JSONB column,
`panel_settings.outbound_bandwidth_state` (`_STATE_KEY`, line 48), read and
written through `panel_settings_repository`; the module holds only a
process-wide `threading.Lock`, so it is a single-process guarantee, not a
distributed one. `reserve_outbound_bytes` (line 302) reads, checks both
windows, writes, and **re-reads to confirm**; any unreadable/malformed/
unwritable state denies the transfer (`_reserve_locked`, line 233).

Governed paths (verified call sites):

| Path | Call site | Category |
|---|---|---|
| Deep Save single upload | `save_service.py:1666` | `telegram_media` |
| Deep Save album member | `save_service.py:1935` | `telegram_media` |
| Link save | same `execute_link_save` pipeline | `telegram_media` |
| Retrieve re-upload (rename) | `retrieve_service.py:57,1350` | `telegram_media` |
| Telegram voice/media upload | `telegram_api/media.py:102,120,152` | `telegram_media` |
| AI result attachment | `ai/tools/delivery.py:716-734` | `telegram_media` |
| Gemini OCR/STT upload | `gemini_media_engine.py:749,968,970,1052,1221` | `gemini_ocr` / `gemini_stt` |
| Appearance Storage upload | `appearance_service.py:1079-1082` | `storage_upload` |
| Anti-edit/delete Storage upload | `anti_edit_delete_service.py:1632-1635` | `storage_upload` |

Every one of these is fail-closed: the reservation is awaited **before** the
transfer, and the governor is imported without a fallback.

Confirmed gaps (the governor's own docstring lists the first four):

1. **HTTP response egress** — `/health`, every `/api/*` response, the SPA and
   guide assets, and `/appearance/image/*`. Not counted.
2. **Telegram protocol traffic** — update dispatch, RPC calls, keepalive,
   panel edits, profile updates, reactions. Not counted.
3. **Helper-bot traffic** — every helper-bot RPC. Not counted.
4. **Supabase request/response traffic** — not counted (responses are
   inbound/free, but requests are outbound bytes).
5. **Groq STT uploads** (`groq_stt_engine.py:676`) and **Speechmatics STT**
   submit/poll (`speechmatics_stt_engine.py` `_submit`/`_wait_for_job`) —
   no reservation. These are the only media-sized outbound paths that are
   still ungoverned.
6. **Provider prompts** (all AI calls, including History map/reduce and task
   preparation) — no reservation.
7. **Model discovery and model testing** — no reservation.
8. **TTS provider requests** — no reservation (bodies are text-sized).
9. **Render metrics API** — no reservation (tiny).

The governor therefore caps *governed payload uploads* at 150 MB/day and
4 GB/month; it does **not** cap total Render egress. Both statements are
true simultaneously and the difference is what this investigation exists to
expose.

A second, subtle gap: the governor counts **reservations**, not actual
transmitted bytes. A failed upload still consumes its reservation (there is
no refund; `release_reservation` is a documented no-op). Over a month, the
ledger can therefore report more than the wire sent — it never reports less,
which is the safe direction for enforcement but makes it unsuitable as a
usage meter.

## 11. November 7–12 spike analysis

### 11.1 The supplied evidence

Metric: Render `bandwidth_usage` for the account-1 `Self_bot` service only;
period 2026-11-01 00:00 UTC → 2026-11-23 16:30 UTC; 3,600 s resolution; 338
hourly points; total ≈ 4,155.126 MB.

Daily values supplied: Nov 7 ≈ 560.5 MB, Nov 8 ≈ 793.7 MB, Nov 9 ≈ 771.4 MB,
Nov 10 ≈ 759.0 MB, Nov 11 ≈ 380.8 MB, Nov 12 ≈ 410.4 MB, Nov 23 ≈ 199.1 MB
(partial day). Maximum hourly point ≈ 54.9 MB.

### 11.2 Arithmetic (from the supplied numbers, no new estimates)

* Nov 7–12 sum = **3,675.8 MB**, i.e. **88.5 %** of the 4,155.1 MB
  month-to-date total.
* Nov 7–10 sum (before the governor commit) = **2,884.6 MB**.
* Day-by-day multiple of the governor's 150 MB/day cap:
  Nov 7 **3.7×**, Nov 8 **5.3×**, Nov 9 **5.1×**, Nov 10 **5.1×**,
  Nov 11 **2.5×**, Nov 12 **2.7×**.
* Six-day sum is **92 %** of the governor's 4 GB monthly cap, in six days.
* Excluding Nov 7–12 and the partial Nov 23, the remaining ~16 days account
  for ≈ 280 MB, i.e. ≈ 17.5 MB/day. That baseline is consistent with
  control traffic + occasional AI/media use, not with a runaway loop.
* 338 hourly points cover ~545 elapsed hours (22 days 16.5 h). The series is
  therefore incomplete (missing/zero-suppressed points); Render documents
  that points below 1 MB may display as 0. The month total is a sum of the
  returned points, so genuine sub-1 MB hours are absent and interior gaps are
  not detected by the current parser.

### 11.3 Timeline evidence (repository, not timestamps alone)

| Date | Commit | Relevance |
|---|---|---|
| 2026-11-06 | `0d07dc1` repair tool-call continuation, prompt schemas | more continuation rounds possible |
| 2026-11-06 | `fa2dfd0` probe one sibling model on ambiguous 429 | model-level fan-out |
| 2026-11-08 | `8ecccf9`, `d3aa5a1`, `c8e5e90`, `15bc401` Delete/Glass fixes | heavy panel testing; Delete itself does not upload |
| 2026-11-09 | `a301e64`, `c693014`, `9b66750`, `98c8f36`, `f687e60`, `1d0ed3a` Appearance | appearance image upload + **panel photo web document** introduced |
| 2026-11-09 | `15a7d8d` model tester caption/reachability | model testing exercised |
| 2026-11-11 | **`9d220b6` outbound bandwidth governor** | first outbound cap exists |
| 2026-11-11 | `1836dfd` bandwidth usage panel | operator visibility added |
| 2026-11-12 | `c43c5fa`, `5701aed`, `dcad23c` Intelligent Reaction | one AI call per outgoing message when enabled |
| 2026-11-13 | `3a2e8ab` coalesce reaction requests; History work | reaction request count reduced |

The governor commit is dated **2026-11-11**, after the four largest days.
Commit date is not deployment time, so the exact hour the cap became
effective is unknown; the two post-commit days still exceed the cap by
2.5–2.7×, which is the part that needs the measurement below.

### 11.4 What the spike is consistent with

Three families can produce hundreds of MB/day of **outbound** traffic from
this service:

* **Telegram media uploads** — Deep Save / album / bulk re-send / AI
  attachments. A single 10-member album at the default 50 MB setting is
  ~500 MB; at the maximum 500 MB setting it is ~5 GB. Before Nov 11 nothing
  stopped it. This is the only application feature that routinely moves
  hundreds of megabytes per operation.
* **Appearance image HTTP fetches** — up to 5 MB per panel web-document
  fetch. Appearance shipped Nov 9. Panel usage during feature testing was
  necessarily high. This is the only feature that can turn *opening panels*
  into hundreds of MB of HTTP egress.
* **Dashboard/API polling** — 2,880 polls/day per open tab; 100 KB/poll =
  288 MB/day. The API is unauthenticated and uncompressed, and `/api/saves`
  selects every column. If a browser tab was left open during the same
  window, this alone can reach the observed order of magnitude.

Groq/Speechmatics STT (20 MB × up to 3 passes) and provider fan-out are
plausible secondary contributors; Supabase and Telegram RPC are not
credible GB-scale sources.

### 11.5 The decisive measurement (no code change)

Render exposes `GET https://api.render.com/v1/metrics/bandwidth-sources`
with `startTime`, `endTime`, `resource` (max 30 resources) and hourly points,
returning `labels.trafficSource` in `{total, http, websocket, nat,
privatelink}`. Traffic-source data exists from 2025-03-09. Querying
2026-11-07..12 for `RENDER_SERVICE_ID` splits the spike into:

* **`http` high** → dashboard/API polling and/or the appearance image (and
  the `/guide`/SPA assets). Then the fix is response size/caching/auth, not
  media governance.
* **`nat` high** → service-initiated traffic: Telegram uploads, Gemini/STT
  uploads, Supabase requests, provider prompts. Then the fix is media
  preflight/governance and STT/provider accounting.
* **both** → quantify each; the report's ranked candidates already map to the
  two buckets.

The same split is visible without the API in the Render dashboard's Network
Metrics graph (HTTP Responses vs Service-Initiated). This is an operator
read-only action; no repository change and no new instrumentation is needed.

### 11.6 What cannot be concluded

The supplied evidence is for **one service in one account**. It does not
represent the owner's other three/four Render accounts or any preview
service. No causal claim is made from timestamps alone; the timeline above
establishes *what shipped*, not *what was used*. Byte attribution to a
specific feature remains unmeasured until the traffic-source split (and,
ideally, the in-app counters in section 15) exist.

## 12. Ranked root-cause candidates

Ranking is by plausible contribution to Render **outbound** bytes, with the
evidence and the missing measurement stated. "Frequency" and "byte size" are
marked confirmed/estimated/unknown per candidate.

| # | Candidate | Why it can consume bandwidth | Frequency | Byte size | Evidence | Confidence | Needed measurement |
|---|---|---|---|---|---|---|---|
| 1 | **Telegram media uploads** (Deep Save, albums, bulk re-send, renames, AI attachments) | Full media bytes are uploaded to Telegram DCs; albums multiply by member count up to ~5 GB; no cap before 2026-11-11 | Per save/send action; unknown production count | Confirmed formula: member size × members; default album ~500 MB | `save_service.py:1666,1935,1980`; `retrieve_service.py:1350`; `telegram_api/media.py:102`; `delivery.py:716`; governor commit `9d220b6` | **High** (family); per-day volume unknown | `nat` trafficSource on Nov 7–12; governor reservation logs (`OUTBOUND_BANDWIDTH_RESERVED`) and their `bytes` |
| 2 | **Appearance image HTTP egress** | Telegram fetches `/appearance/image/<version>?fmt=jpeg` for every photo panel; up to 5 MB + thumbnail per fetch | Every newly created panel message with preview active | Up to 5 MB confirmed per fetch | `appearance_service.py:136,408-460,599-619`; `web/app.py:333-362` | **High plausibility**, volume unknown | `http` trafficSource; count panel messages/day; add a per-response byte log |
| 3 | **Dashboard/API polling** | 3 GETs every 30 s; `/api/saves` select("*") 50 rows + count; `/api/logs` 100 rows; no gzip; unauthenticated | 2,880 polls/day per open tab | Row-size dependent; conditional formula: 100 KB/poll ≈ 288 MB/day | `src/App.tsx:84`; `src/lib/api.ts:146-158`; `db/client.py:456-475,1965-1975`; `web/app.py:82,139` | **Medium** (depends on an open tab) | `http` split; web access-log byte counters; per-row payload size |
| 4 | **Groq/Speechmatics STT uploads** | Audio is uploaded with no governor; Groq may send it up to 3 passes | Per transcription, only when those engines are selected (Gemini is default) | ≤20 MB per pass; ≤60 MB per Groq operation | `groq_stt_engine.py:609-631,676`; `speechmatics_stt_engine.py` submit/poll; `media_service.py:246`; `stt_control_plane.py:78` | **Medium/Low** | Config: selected STT candidate and passes; add `len(audio)` accounting |
| 5 | **AI provider prompt fan-out** | Up to 6 engine calls per request × model/provider candidates × full prompt | Per AI request, amplified on provider outage | Prompt order 10–35 KB (estimated from token budget, not measured); worst-case fan-out large | `dispatcher.py:72,696,781,1052`; `manager.py:66,134,919,1083`; `candidate_pool.py:57-59`; `openai_compat.py:138` | **Medium** (amplifier, not primary bytes) | Count provider attempts per request; sum serialized request bytes |
| 6 | **History summarization** | Chunked map + reduce over up to 1,000 messages, each chunk ~4k tokens, paced 5 s | Per History request/task occurrence | ~16 KB per chunk (estimated); chunk count bounded by capacity | `history_ai_service.py:93,118-126`; `history_service.py:58` | **Low/Medium** | Count chunks and provider bytes per History run |
| 7 | **Supabase request traffic** | Every query is an outbound HTTPS request; N+1 in `/api/db/stats`; config read per outgoing message | High request count | Per request hundreds of bytes to a few KB | `db/client.py`; `db_stats.py:34-42`; `ai_unified.py:1030` | **Low** | Request-count/byte counter at the transport |
| 8 | **Telegram RPC / panel edits / keepalive** | Persistent control traffic, panel countdown edits every 30 s, minute profile cron | Continuous | Small | `keepalive.py:28`; `panel_timer.py:21`; `profile/scheduler.py:149-155` | **Low** | Protocol-level byte counters (not currently available) |
| 9 | **Model discovery / model tester** | One GET per provider on panel open; a full real-model test on demand | On demand only | Request-count burst; responses inbound | `web/app.py:156-208,286`; `model_tester.py:684,861` | **Low** (spiky) | Count tests and provider calls |
| 10 | **Gemini media / TTS / governed uploads** | Real bytes, but already capped by the governor (Gemini) or bounded by audio limits | Per operation | Governed | `gemini_media_engine.py:749-762`; governor calls | **Low** (capped) | Existing governor ledger |

The ranking deliberately keeps **request count** and **byte volume** separate.
Supabase and Telegram RPC are high-count/low-byte; media uploads and the
appearance image are low-count/high-byte. A high AI token count does not
prove high egress, and a large database response is inbound/free.
## 13. Confirmed facts vs hypotheses

### Confirmed from code (source-verified at `65632bf`)

* Render bills outbound only: HTTP/WebSocket responses + all service-initiated
  traffic; inbound is free (Render documentation).
* The governor limits are 150,000,000 bytes/day and 4,000,000,000 bytes/month,
  are fail-closed, and are a reservation ledger rather than an egress meter
  (`outbound_bandwidth_service.py:45-46,233-300`).
* The governed call sites are exactly the nine groups in section 10.
* Groq and Speechmatics STT uploads have **no** governor call
  (`groq_stt_engine.py:676`; `speechmatics_stt_engine.py` submit/poll).
* HTTP responses, Telegram protocol traffic, helper-bot traffic, Supabase
  request/response traffic, provider prompts and discovery are ungoverned.
* `/appearance/image/{version}` can return up to 5 MB per fetch and sets a
  one-year immutable cache header (`appearance_service.py:136`;
  `web/app.py:333-362`).
* Every Glass panel with the appearance preview active is a `photo` inline
  result whose document URL is the Render appearance endpoint
  (`appearance_service.py:408-460,599-619`).
* The dashboard polls three endpoints every 30 s (`src/App.tsx:84`), and
  `/api/saves` / `/api/logs` use `select("*")` (`db/client.py:456-475`,
  `:1965-1975`). The API has no authentication and no compression middleware.
* One user AI request can produce up to six `ProviderManager.chat()` calls,
  and each can fan out across model/provider candidates with one retry per
  candidate (`dispatcher.py:72,696,781,1052`; `manager.py:66,919,1083`).
* The durable candidate pool is bounded at 32 providers × 256 models per
  provider and is restored only from a complete Test Modules run
  (`candidate_pool.py:57-59`).
* Provider health checks are local; discovery is on demand with a 600 s cache;
  there is no periodic provider poll.
* The task scheduler wakes at most every 60 s, performs ~2–3 queries when
  idle, and does no provider/Telegram work when nothing is due
  (`task_scheduler.py:15-23,470-480`).
* Telegram media downloads are inbound; uploads are outbound and governed on
  the five verified paths.
* Keepalive is one `get_me` per 60 s; heartbeat and failsafe are local.

### Hypotheses (supported by code, not proven by measurement)

* **H1.** Telecom media uploads (especially album Deep Save) dominate the
  Nov 7–12 outbound spike, because the governor did not exist until Nov 11
  and albums can reach hundreds of MB per operation.
* **H2.** The appearance panel photo causes one Telegram web-document fetch of
  up to 5 MB per created panel message, producing large `http` egress during
  the Appearance release window.
* **H3.** A dashboard tab left open during these days contributed materially
  through uncompressed `select("*")` polling.
* **H4.** Provider fan-out on an unhealthy provider multiplied prompt egress
  beyond the normal one-request-per-turn baseline.
* **H5.** Groq/Speechmatics multi-pass STT contributed for the periods when
  those engines were selected.

None of H1–H5 can be confirmed or ranked definitively from source alone. Each
has a named measurement in sections 11.5 and 15.

### Explicitly not concluded

* That total workspace usage is 4.155 GB — that figure is one service in one
  account. The owner's other accounts/services are out of scope of the
  supplied metric.
* That the 338 points are complete — they are not (551 elapsed hours).
* That any feature "caused" the spike from its commit date alone.
* That bandwidth was reduced by this task — nothing was changed.
* That the governor is ineffective — it is effective inside its declared
  scope; the point is that its scope is smaller than total Render egress.

## 14. Current observability gaps

| Gap | Consequence |
|---|---|
| Render metric is hourly, service-total, ~60 min delayed, and sub-1 MB hours may read as 0 | Cannot attribute a spike to a subsystem or see short bursts |
| The app never queries `/metrics/bandwidth-sources` | No HTTP vs service-initiated split inside LifeOS |
| Governor counts reservations, not transmitted bytes, and never refunds | Ledger ≠ wire; over-reports failed transfers |
| No HTTP response byte counter | `/api/*`, `/guide`, appearance image egress is invisible |
| No provider request/response **byte** counter | `task_trace` records `request_size` and `response_length` in characters only (`manager.py::_call_once`); not aggregated, not persisted |
| No STT/TTS upload byte counter | Groq/Speechmatics egress is invisible (Gemini logs sizes via the governor) |
| No Supabase request byte counter | Request-count and request-size attribution impossible |
| No Telegram protocol byte counter | RPC/panel/keepalive traffic is invisible (and likely small) |
| `monthly_is_partial` only checks the 29-day window clamp | Interior gaps and missing zero-hours are not detected (`render_bandwidth_service.py:181-182`) |
| `_parse_payload` rejects an empty `values` list as unavailable | A genuinely zero-usage service is reported as unmeasurable (`render_bandwidth_service.py:87-89`) |
| Render HTTP request-count metric returned no data for the same query | The request-count signal the operator tried to use is unavailable/renamed; do not infer request volume from it |
| No per-endpoint access log with bytes | `/health` vs `/api/saves` vs `/appearance/image` indistinguishable |

Existing partial instrumentation worth reusing (not duplicating):

* `task_trace` events `provider_request_start` (request_size chars),
  `provider_response` (response_length chars), `provider_selection`,
  `provider_fallback` (`backend/ai/task_trace.py`, emitted from
  `manager.py::_call_once`).
* Governor log line `OUTBOUND_BANDWIDTH_RESERVED category=... bytes=...
  daily_used=... monthly_used=...`.
* STT/TTS engines' bounded `_log_run(byte_count, chars, ...)` lines.
* The diagnostics ring (`backend/diagnostics.py`,
  `GET /api/diagnostics/events`).
* uvicorn access logs (path/status, no bytes).
* Render's own dashboard Network Metrics breakdown.

## 15. Minimal future instrumentation design

The design is deliberately compatible with the existing architecture: **no
second scheduler, no second HTTP client, no second Telegram client, no second
executor, no second task lifecycle, no new table or migration.**

### Tier 0 — zero code (available now)

1. `GET https://api.render.com/v1/metrics/bandwidth-sources` for
   `RENDER_SERVICE_ID`, `startTime=2026-11-07T00:00:00Z`,
   `endTime=2026-11-13T00:00:00Z`. Read `labels.trafficSource` and the
   `values[]`. This splits `http` from `nat` and settles H2/H3 vs H1/H5.
2. The Render dashboard's Network Metrics graph for the same range. The
   operator can read it without any credential handling by the agent.
3. Optional: query the same endpoint for the other accounts/services to
   establish the workspace total (the supplied 4.155 GB is one service).

### Tier 1 — in-process bounded counters (recommended next phase, RAM only)

One small module, e.g. `backend/observability/egress_counters.py`, with a
bounded dict of `{category: bytes}` plus `{category: count}`, an hourly
bucket, and a hard cap on categories (e.g. 12). It exposes
`add(category, n)` and `snapshot()`. It performs no I/O and never logs
payloads, headers or secrets. Call sites are the **existing boundaries**:

| Boundary | Where to count | What to count |
|---|---|---|
| Provider attempts | `ProviderManager._call_once` (already computes `request_size`) | `len(json.dumps(payload))` is not visible there; count `len(json.dumps(messages))` + `len(tools)` as an upper bound, and `len(response.text)` for responses. Category `provider:<name>` |
| Telegram uploads | the five governed helpers, plus `telegram_api/media.py` voice | payload bytes already passed to the governor |
| STT uploads | `groq_stt_engine._request`, `speechmatics_stt_engine._submit` | `len(audio)` |
| TTS requests | each engine's request builder | `len(text.encode())` |
| FastAPI responses | one `@app.middleware("http")` | `len(response.body)` (or `Content-Length`) + path class (`/api/saves`, `/appearance/image`, static, health) |
| Supabase | `backend/db/transport.py` `HTTP1Session.send` override | `len(request.content or b"")` + path class |
| Render metrics | `render_bandwidth_service` | request/response size |

Bounding rules: aggregate per category and per hour; keep a ring of 24
buckets; never store URLs with query strings, bodies, headers or tokens; one
log line per hour (reuse the existing heartbeat/diagnostics pattern rather
than a new loop). Expose the snapshot through the existing
`/api/status`/`runtime_status()` payload or the diagnostics ring — not a new
endpoint if avoidable.

This is enough to answer "which subsystem sent the bytes" at the same
resolution as the Render metric, without a schema change and without
touching enforcement.

### Tier 2 — optional persistence (requires explicit approval)

If counters must survive restarts, write the hourly snapshot into the
  existing `bot_settings` JSON document (the same `bot_settings` document
  mechanism `candidate_pool.publish`/`load` uses) or the existing
  `panel_settings` JSONB.
That is still no new table/migration. Persist at most once per hour, bounded
payload, no request bodies. Any schema change is explicitly out of scope for
this task.

### What the instrumentation must not do

* No second HTTP client manager or connection pool.
* No new scheduler/worker; piggyback on an existing tick.
* No new table, migration or SQL.
* No logging of prompts, messages, media, credentials or full URLs with
  tokens.
* No change to the governor's enforcement semantics; counters observe, they
  do not reserve.

## 16. Prioritized optimization opportunities

Ordered by expected egress reduction per unit of risk. None is implemented
here.

1. **Album/save preflight against remaining budget.** Today
   `_deep_save_album` reserves per member (`save_service.py:1935`); a
   10 × 50 MB album starts uploading and can fail mid-way after reserving
   hundreds of MB. Compute the total album size first and refuse the whole
   operation if it exceeds the remaining daily/monthly budget. This prevents
   the largest single-operation egress and avoids wasted uploads.
2. **Make the appearance photo cheap.** Serve a small dedicated JPEG (e.g.
   ≤100–200 KB, or the 320 px thumbnail) as the inline photo document instead
   of a rendition that can reach 5 MB; keep the full-resolution image behind
   the existing versioned URL only for explicit viewing. If Telegram's fetch
   behaviour is confirmed as the main `http` contributor, this is the single
   largest HTTP lever. Alternative: upload the preview once through the helper
   bot and reuse Telegram's `file_id`, eliminating Render-served image bytes
   entirely.
3. **Shrink and authenticate the dashboard API.** Select only rendered
   columns (drop `select("*")`), add `ETag`/`Last-Modified` + `304`, add
   gzip, reduce default limits, and require the owner session. A closed tab
   is free; an open tab should not stream full rows every 30 s.
4. **Govern Groq/Speechmatics STT uploads.** Reserve `len(audio)` (× passes)
   before the multipart POST, exactly as the Gemini path does. This closes the
   last media-sized ungoverned upload class.
5. **Bound provider fan-out.** Add a per-request attempt cap (e.g. 2–3 model
   candidates, or a byte/attempt budget) and avoid re-sending the full prompt
   to every model of a provider already proved unhealthy in this request.
   Keep the existing retry/cooldown semantics; only cap the breadth.
6. **Trim continuation payloads.** The engine already caps rounds at 3; the
   continuation messages still repeat the entire prompt. A bounded,
   explicitly-versioned continuation shape (or sending only the new tool
   results plus a compact instruction) would cut per-round bytes, but it
   changes the provider contract and needs its own correctness work.
7. **Avoid re-uploads on rename.** `do_change_file_name` downloads and
   re-uploads (`retrieve_service.py:1336-1351`). Telegram's server-side copy
   or an in-place caption edit (when only the name changes) avoids the bytes.
   This needs a product decision about message identity.
8. **Make the model tester and discovery deliberate.** Confirm before a full
   test (it can call every model), cache aggressively, and never run it from
   an unauthenticated request.
9. **Compress and cache static/guide assets.** Add gzip/brotli for text
   responses and long-lived `Cache-Control` for hashed SPA assets; the guide
   site can carry an ETag. Small but free.
10. **De-duplicate per-message config reads.** `ai_unified_handler` reads
    `ai_config` before knowing whether the message is a request. A short-TTL
    in-process cache (the settings service already uses this pattern) would
    remove one Supabase request per outgoing message.
11. **Keep the governor fail-closed and visible.** When a reservation is
    denied, tell the owner in the existing panel/notice path; do not add a
    second accounting system.
## 17. Explicitly deferred work

Not done, by design or by lack of authorization:

* Any code change, refactor, dependency change, test change or new module.
* Any database schema/migration/SQL, including adding counters or tables.
* Any Render configuration change: plan, instance count, health-check path,
  environment variables, preview services, suspend/resume/delete.
* Any live Telegram, Supabase, AI provider or Render API call; no Render shell.
* Any measurement on the owner's other Render accounts or preview services.
* Implementing the instrumentation in section 15 or any optimization in
  section 16.
* Changing the governor's limits (150 MB/day, 4 GB/month) or its fail-closed
  semantics.
* Deciding whether previews should be disabled, whether the plan should
  change, or whether the appearance feature should drop its photo.
* Deleting or rewriting any existing documentation.

The next phase needs an explicit instruction to implement, plus the Tier 0
traffic-source measurement so the first optimization targets the right
bucket.

## 18. Exact files/functions inspected

Repository root: `AGENTS.md`, `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md`,
`ROADMAP.md`, `AI_MASTER_DESIGN.md`, `DATABASE_ARCHITECTURE.md` (targeted),
`render.yaml`, `Procfile`, `Dockerfile`, `guide/*`, `src/*`.

### Bandwidth / governor
* `backend/services/outbound_bandwidth_service.py` — module docstring,
  `DAILY_LIMIT_BYTES`, `MONTHLY_LIMIT_BYTES`, `_STATE_KEY`, `_lock`,
  `_normalize_state`, `_read_row_state`, `_persist_state`, `_reserve_locked`,
  `reserve_outbound_bytes`, `reserve_outbound_bytes_sync`, `usage_state`,
  `_usage_snapshot`, `get_outbound_usage`, `release_reservation`.
* `backend/services/render_bandwidth_service.py` — `_parse_payload`,
  `_series_matches_service`, `get_render_bandwidth_usage`,
  `monthly_is_partial` handling.
* `backend/services/panel_settings_repository.py` — `load`, `update_field`,
  `update_fields`, `reload`.
* `backend/bot/handlers/misc.py` — `_format_bandwidth_bytes`,
  `_bandwidth_unavailable_body`, `_bandwidth_body`,
  `_render_platform_bandwidth_body`, `_bandwidth_render`,
  `_bandwidth_panel_handler`, `_bandwidth_refresh_action`, `_register_panels`.
* Thin alias modules: `outbound_bandwidth.py`, `bandwidth_service.py`,
  `bandwidth_governor.py`, `outbound_bandwidth_governor.py`.

### AI request path
* `backend/bot/handlers/ai_unified.py` — `ai_unified_handler` (1014),
  `_load_triggers` (~1030), `_execute_ai`, `_AI_TIMEOUT`, `_AI_EXECUTE_TIMEOUT`.
* `backend/ai/engine/engine.py` — `Engine.__init__`, `execute`,
  `engine_health`, `attach_tools`, `apply_persisted_config`,
  `_heal_phantom_config`.
* `backend/ai/engine/dispatcher.py` — `MAX_TOOL_ROUNDS`, `dispatch`, initial
  provider call, `AI_PROVIDER_EMPTY_RESPONSE_RETRY`,
  `AI_ACTION_RECOVERY_RETRY`, tool loop, `_build_continuation_messages`,
  `_build_messages`, usage accumulation, rate limiter.
* `backend/ai/providers/manager/manager.py` — `chat`, `_ordered_candidates`,
  `_skip_reason`, `_score`, `_candidate_models`, `set_model_candidates`,
  `_attempt_with_retry`, `_call_once`, `_apply_failure`,
  `_escalate_if_provider_wide`, `_fallback`, health snapshot.
* `backend/ai/providers/manager/health.py` — state machine, penalties,
  cooldowns, quarantine, model-scope classification.
* `backend/ai/providers/openai_compat.py` — client creation, `chat`, error
  fields, `list_models`.
* `backend/ai/providers/gemini.py` — client creation, `chat`, model list.
* `backend/ai/providers/you_search.py` — search request.
* `backend/ai/discovery.py` — `discover_providers`, provider GETs.
* `backend/ai/model_discovery.py` — `_CACHE_TTL`, `fetch_models`,
  `get_cached_models`, pagination.
* `backend/ai/model_tester.py` — `test_all_models`, `test_all_models_streaming`,
  candidate publication.
* `backend/ai/candidate_pool.py` — bounds, `build_document`, `publish`,
  `load`, `restore`.
* `backend/ai/retry.py` — `MAX_ATTEMPTS`, `retry_delay`, `can_retry`.
* `backend/ai/prompt/builder.py` — `build`, `_render_sections`,
  `_merge_system`, `_trim_history_to_budget`.
* `backend/ai/prompt/budget.py` — token defaults.
* `backend/ai/prompt/template.py` — fixed system/platform/runtime/output text.
* `backend/ai/conversation/context_builder.py`, `conversation/*`,
  `runtime/manager.py`, `runtime/history.py`, `session/request.py`.
* `backend/ai/memory/*`.
* `backend/ai/persistence.py` — session/message/memory/tool-history
  persistence, in-flight bound.
* `backend/ai/database/usage_recorder.py`, `usage_repository.py`,
  `usage_reader.py`, `provider_stats_repository.py`, `message_repository.py`,
  `session_repository.py`, `preferences_repository.py`,
  `tool_history_repository.py`, `memory_repository.py`.
* `backend/ai/tools/executor.py` — `MAX_TOOLS_PER_TURN`, execution semantics.
* `backend/ai/tools/delivery.py` — attachment reservation and send.
* `backend/services/media_service.py` — `MAX_STT_INPUT_BYTES`,
  `MAX_OCR_INPUT_BYTES`, `MAX_EXTRACTED_CHARS`, chunking.
* `backend/services/gemini_media_engine.py` — `_reserve_gemini_bandwidth`,
  upload/download/delete, `_client`.
* `backend/services/groq_stt_engine.py` — `_run_passes`, `_request`,
  `_client`.
* `backend/services/speechmatics_stt_engine.py` — `_submit`,
  `_wait_for_job`, `_job_status`, `_fetch_transcript`, `_send`, `_client`.
* `backend/ai/stt_control_plane.py` — candidates, default Gemini, passes.
* TTS engines: `gemini_tts_engine.py`, `openai_tts_engine.py`,
  `grok_tts_engine.py`, `speechmatics_tts_engine.py`.

### Save / retrieve / storage
* `backend/services/save_service.py` — `_reserve_upload_bytes`,
  `_deep_save_one`, `_deep_save_album`, `execute_save`, `execute_link_save`,
  `_fetch_linked_message`, album upload/send.
* `backend/services/retrieve_service.py` — `_reserve_reupload_bytes`,
  `do_retrieve` (`forward_messages`), `do_change_file_name`, `do_bulk_send`,
  `do_bulk_delete`, `_send_single`.
* `backend/services/anti_edit_delete_service.py` — `store_media_snapshot`,
  Storage upload/remove.
* `backend/services/appearance_service.py` — limits, `_upload_sync`,
  `image_payload`, `telegram_photo`, `telegram_thumb`, photo result builders,
  URL builders.
* `backend/bot/handlers/appearance.py` — `iter_download` path.
* `backend/telegram_api/media.py` — `_reserve_voice_bytes`, `send_voice`,
  `send_file`.
* `backend/telegram_api/api.py`, `backend/telegram_api/__init__.py`.

### Tasks / scheduling
* `backend/ai/task_scheduler.py` — constants, `recover`,
  `_execute_claimed`, `_run_due_retries`, `_process_due_task`,
  `_run_due_sweeps`, `run_once`, `_sleep_seconds`, `run`, `_prepare_next_ahead`.
* `backend/ai/task_execution.py` — coordinator/preparation paths.
* `backend/ai/task_trigger.py`, `task_events.py`, `task_answers.py`,
  `task_contract.py`, `task_candidate.py`, `task_creation.py`,
  `task_management.py`, `task_notifications.py`, `scheduling.py`.
* `backend/ai/database/task_repository.py` — `list_due_tasks`,
  `next_run_hint`, `list_due_retry_occurrences`, `list_recoverable_occurrences`,
  `create_occurrence`, `claim_occurrence`, `transition_occurrence`,
  `advance_next_run`, `delete_task`.
* `backend/runtime/supervisor.py` — `_start_task_scheduler`, background loop
  startup, recovery.
* `backend/services/history_ai_service.py`, `history_service.py` —
  `MAX_HISTORY_MESSAGES`, chunk budgets, pacing.

### HTTP / dashboard
* `backend/web/app.py` — every route, `mount_static`, appearance handlers.
* `backend/observability/*` — `runtime_status`, `ai_stats`, `db_stats`,
  `health_snapshot`, `performance`, `maintenance`, `crash_report`.
* `src/App.tsx`, `src/lib/api.ts`, `src/components/*`, `guide/*`.

### Runtime / Telegram
* `backend/main.py`, `backend/config.py`.
* `backend/bot/client.py` — `build_client` options.
* `backend/helper/client.py` — helper options.
* `backend/runtime/keepalive.py`, `heartbeat.py`, `failsafe.py`,
  `diagnostics.py`, `memory_cleanup.py`, `task_guard.py`,
  `operation_watchdog.py`, `tg_retry.py`.
* `backend/profile/scheduler.py` — `_cron_loop`.
* `backend/helper/panel_timer.py`, `inline_sender.py`, `inline_engine.py`,
  `panel_render.py`, `lifecycle.py`, `callback_gate.py`, `panel_edit.py`,
  `rpc_timeout.py`.
* `backend/bot/router.py`; handlers `auto_reaction.py`,
  `emoji_replacement.py`, `intelligent_reaction.py`, `private_message.py`,
  `task_events.py`, `tasks.py`, `anti_edit_delete.py`, `save.py`,
  `retrieve.py`, `misc.py`.
* `backend/services/intelligent_reaction_service.py`,
  `auto_reaction_service.py`, `emoji_replacement_service.py`,
  `private_message_service.py`, `settings_service.py`.
* `backend/db/client.py` — singleton, `_run_sync`, `list_saves`,
  `list_logs`, state/document helpers, Storage call sites.
* `backend/db/transport.py` — HTTP/1.1 pin.
* `backend/ai/database/manager.py` — repository manager.
* `tests/` — existing coverage names only; no test was executed.

## 19. Validation and Git delivery status

### 19.1 Repository scope validation (performed before commit)

Commands and expected results:

```
git status --short
git diff --name-only
git diff --check
```

Required outcome: `INVESTIGATION.md` is the only task-related path; the
seven pre-existing entries in section 2 remain exactly as they were; no
production code, test, dependency, SQL, configuration, `ROADMAP.md`,
`AGENTS.md`, `IMPLEMENTATION_REPORT.md` or `DATABASE_ARCHITECTURE.md` is
modified; `git diff --check` reports no whitespace errors.

### 19.2 Delivery procedure (performed)

1. The report was written from the exact tree at `65632bf` into
   `INVESTIGATION.md`.
2. Only that path was included: a temporary index was built from `65632bf`
   plus this file, so no other path could enter the commit.
3. The first commit was created with `65632bf` as its parent and the
   wording-correction commit as its child, so the delivered history remains a
   normal fast-forward of remote `main` (no rebase, no force, no history
   rewrite).
4. Both commits were pushed normally to `origin/main`.
5. Verification: `git fetch origin`; `git rev-parse origin/main`; then
   `git merge-base --is-ancestor <delivered> origin/main` (exit 0) and
   `git ls-remote origin refs/heads/main` equals the delivered SHA.

### 19.3 Known local-worktree limitation (reported honestly)

The chat worktree's local branch was 18 commits behind remote `main` and
carried seven pre-existing unrelated modifications (section 2). Six of those
files also changed upstream, so a fast-forward of the local branch would
require overwriting them. The report therefore preserves them in place and
does not move the local branch, reset, stash or rebase anything. The
delivered commit is built directly on `65632bf` and pushed to `origin/main`;
the delivered SHA and the remote verification are reported in the final
response. No unrelated file was staged or committed.

This limitation is a property of the pre-existing worktree, not of the
delivered change: `origin/main` receives two documentation commits (the report
and a follow-up wording correction), each of which touches only
`INVESTIGATION.md`; the cumulative diff against `65632bf` is still only
`INVESTIGATION.md`.

### 19.4 What was not validated

* No test suite, build, typecheck or linter was run: this is a documentation
  change and running project code was outside the read-only boundary.
* No live Render/Telegram/Supabase/provider call was made; the 4.155 GB figure
  is the operator-supplied measurement, not re-queried here.
* No byte attribution was measured; section 11.5 defines the first
  measurement to run in the next phase.
