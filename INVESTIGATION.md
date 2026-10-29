# INVESTIGATION — whole-runtime responsiveness of the Telegram self-bot

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Worktree branch | `freebuff/99799eefb106784388e20bc8` |
| **HEAD at investigation start** | **`a4b6e4f1479c172a824b3084233b9d108c08d90d`** (`perf(callbacks): cut redundant round trips and coalesce burst renders`), equal to the remote tip this clone fetched (`git rev-parse FETCH_HEAD`) |
| Working tree at start | clean |
| Date | 2026-10-23 (UTC) |
| Symptom under investigation | the freeze/backlog after rapid inline-button clicks persisted after the phase-1/2 callback work |
| Scope | the complete runtime, with measurements concentrated on the click path (update → ack → turn → handler → database/Telegram → paint) |
| Explicitly out of scope | Save pipeline semantics, the AI runtime's own behaviour, Taskloom scheduling, Supabase schema, Render, any live account |
| **Status** | **Two application defects CONFIRMED for the remaining burst cost, one secondary lock defect CONFIRMED and fixed, one residual cost CONFIRMED and left in place (documented).** One amplifier hypothesis remains UNCONFIRMED |

### Method and its limits

* Every source claim was re-read from the working tree at `a4b6e4f` (and, for
  "before" numbers, from the same tree with this change stashed).
* **No production evidence was accessible in this workspace**: Render logs could
  not be read and no Telegram session exists. The phase-1 live numbers
  (`ack_ms` 160–296, `wait_ms` ≤ 352, `work_ms` 368–668, `total_ms` 528–1095, low
  CPU) are used only as the latency budget for the fakes.
* Timing evidence comes from `logs/bench_panel_burst.py` and
  `tests/test_callback_latency_burst.py`, which install the **real** callback
  router, the **real** panel handlers and the **real** gate/lifecycle, and fake
  only the two network ends at fixed latencies.
* Consequence: the numbers prove what a click waits on and how many round trips
  it makes. They do not prove what Telegram or Supabase answer.

---

## 1. The runtime latency map (Phase 2)

```
Telegram update ──► helper-bot client (Telethon, one task per update)
                    │
                    ├─ guard: is_owner (integer compare)
                    ├─ callback_gate.admit  (query_id dedupe, bounded 128)
                    ├─ callback_gate.ack    ── 1 Telegram RPC (the spinner)
                    ├─ resolve session      (in-memory registry)
                    ├─ supersede background writer (in-memory)
                    ├─ PER-PANEL TURN (FIFO future queue)
                    │   ├─ handler: services + db_client.run_sync_db (4 threads)
                    │   └─ returns the view          ◄── turn released here
                    └─ PAINTER (one per message): newest view → event.edit
                                                      ── 0..1 Telegram RPC
```

| Stage | Where | Cost observed |
|---|---|---|
| Update receipt / dispatch | `telethon/client/updates.py:289` | concurrent, per-update task |
| Acknowledgement | `callback_gate.ack` | one RPC (`ack_rpc_ms`), never waits for work |
| Turn wait | `callback_gate.acquire_turn` | 0 for an idle panel; grew with the queue (was the visible freeze) |
| Handler | panel/action handlers → services | 0 ms (pure views) to ~120 ms (one Supabase read) |
| Database | `db_client.run_sync_db` (4 workers, 10 s watchdog, 8 s transport) | ~120 ms per call on Render; **inside** the turn |
| Telegram edit | `panels._safe_edit` | ~250 ms |
| Paint | `callback_gate.PanelPainter` | outside the turn since this change |

Global/shared primitives audited: `lifecycle._lock` (panel create/close — **was**
held across an RPC), `_painters` (one per message), `_turns` (one per message),
`_save_code_lock` (save-code allocation only), `db._db_executor_lock` (pool
construction), provider semaphores (AI only), Taskloom semaphores (task
occurrences only), `callback_trace._flush_lock` (unused module).

---

## 2. Confirmed findings

| # | Finding | Evidence | Status |
|---|---|---|---|
| 1 | The FIFO turn spanned the Telegram edit, so N clicks on one panel meant N serialised edits and every click waited for an edit it would overwrite. 15 clicks with a trivial handler = 3.9 s, 15 edits | source (`_callback_router`) + benchmark | **CONFIRMED, FIXED** |
| 2 | The phase-2 coalescing was opt-in for ONE handler (`retrieve`'s tick); every other panel kept (1) | source (`SKIP_RENDER` only returned by `_toggle_selected_item`) + benchmark on `menu`/`health` | **CONFIRMED, FIXED** (replaced by a general writer) |
| 3 | `PanelLifecycleManager.close_panel`/`timeout_panel` performed the closing edit **inside** `_lock`, and `create_panel`/`try_reuse_panel` hold it across the inline trigger + edit, so a Close (or auto-close timer) serialised unrelated panel operations process-wide | source (`lifecycle.py`) + measurement: opening a panel during a slow close waited **230.6 ms** | **CONFIRMED, FIXED** |
| 4 | A panel that reads the database per click pays one read per click **inside** the turn, so a burst still costs ≈ N × read (30 clicks ≈ 3.6 s) | benchmark | **CONFIRMED, NOT FIXED** — ordered views require it; documented in §5 |
| 5 | Handlers that raised were logged but reported `completed` (the dispatch helpers swallowed the exception) | source | **CONFIRMED, FIXED** — a raising handler is now `failed` |

## 3. Explicitly ruled out (this phase, current source)

* **Telethon serialising updates** — `updates.py:289` creates a task per update;
  neither client sets `sequential_updates`. A slow handler cannot delay the next
  click's delivery, which the burst benchmarks show directly.
* **Event-loop blocking** — the only `time.sleep` calls in the runtime
  (`speechmatics_stt_engine._sleep_briefly`, `gemini_media_engine._await_file_ready`)
  live in synchronous engine methods that are invoked through `asyncio.to_thread`
  (`ai/stt_provider_probe.py:329` and the media-engine boundaries). No
  `requests`, `subprocess`, `run_until_complete` or `.result()` on a request path.
* **Database-pool saturation as the cause of the freeze** — the pool is bounded
  (4 workers) with a 10 s watchdog and an 8 s transport deadline. A saturated
  pool now has a test proving it degrades to a bounded `OperationTimeoutError`
  and that an unrelated panel still answers and paints meanwhile. Saturation
  would show as `db_ms`, which the timing line reports.
* **Heartbeat/keepalive/failsafe** — no database work, no panel turn.
* **`callback_trace`** — the module is not imported anywhere in the runtime.
* **AI handler / providers / tool executor** — `ai_unified` listens on the
  **self** client for outgoing messages; provider and tool calls are `httpx`
  awaits inside their own task, bounded by AI-only semaphores. They cannot sit in
  the click path.
* **Duplicate delivery** — suppressed by `query_id` since phase 1; the benchmark
  harness re-confirmed it (reused query ids had 15 clicks suppressed, which is
  why the harness resets the gate per cell).
* **Unbounded queues / task leaks** — one future per waiting click (bounded by
  delivered updates), one painter per active message (forgotten on close), bounded
  timing (64) and duplicate (128) stores.

## 4. Open hypothesis (not a finding)

Telegram rapid-edit throttling: frequent edits of one message are widely reported
to be rate-limited, and both clients set `flood_sleep_threshold=60`, so Telethon
sleeps inside the call on a flood answer. Thirty edits of one message in a few
seconds is exactly that pattern. This change cuts edits per burst by 87–93 % on
I/O-free panels, which removes most of the exposure — but no `FLOOD_WAIT` was
observed here, so it is not claimed as a cause.

## 5. Why the fix takes this shape

* **Paint is a projection, not state.** The turn must order *state* (handlers
  read the panel's session/nav and may write it). A paint is a pure function of
  the view the handler just built, so it does not need the turn and must not hold
  it.
* **One writer per message, newest view wins.** Edits to one message must not run
  concurrently (two in flight can land out of order); a queued view is worthless
  the moment a newer one exists. A one-slot painter gives both: serialised
  writes, newest wins, and N clicks cost at most two edits.
* **No task per click.** The first click's task drives the drain loop and picks
  up whatever the queue published; a second click publishes into the slot and
  returns. Nothing is spawned, nothing is queued beyond one slot, and a click
  whose view is superseded is still applied exactly once.
* **The turn remains FIFO and per panel.** Different panels never wait for each
  other, and a handler always sees the state left by the previous click.
* **The lifecycle lock guards state only.** Resource release is synchronous; the
  closing edit is a courtesy to the client and moved outside the lock, so one
  panel's close cannot serialise another panel's create.
* **Observability before and after.** `ack_rpc_ms` separates the answer RPC from
  application latency, `work_ms` is handler-only, `paint_ms` is the paint, and
  `db`/`edits`/`coalesced` state what the click actually cost — measured at the
  one dispatcher (`run_sync_db`) and the one edit path (`_safe_edit`), never
  estimated.

---

## 6. Historical investigations (kept, superseded, not relied on)

* **2026-10-23 — phase 2 (round-trip reduction + Stash snapshot).** Its confirmed
  findings (4 DB round trips per Stash tick; one edit per tick) and its Stash page
  snapshot stand. Its *cost* conclusion — that a serialised burst is inherently
  `N × (read + edit)` — is superseded: the edit was never part of ordering, and a
  burst on an I/O-free panel is now constant-time. Its `SKIP_RENDER` opt-in is
  replaced by the panel writer.
* **2026-10-23 — phase 1 (acknowledgement + ordering).** Ack-first, one FIFO turn
  per panel, `query_id` duplicate suppression: all still in force and still
  verified (ack is one RPC; handlers on one panel never overlap). Its replay of
  Telethon's dispatch (`updates.py:289`) was re-checked against the pinned wheel.
* **2026-10-22 — "can the Helper Bot edit an existing inline panel
  asynchronously?"** Verdict was UNRESOLVED (no runtime evidence that Telegram
  hands the Helper Bot an inline-message identifier for these panels). Its library
  facts stand and are what `backend/helper/panel_edit.py::ExistingPanelEditor`
  relies on. This task neither depends on nor changes it.
* Earlier Save-pipeline reports (view-once media, SAVE_TRACE, deep-save/album
  contracts, the Stash rename and multi-select) describe behaviour this change
  does not touch; their current contracts are in `AGENTS.md`.

---

## 7. Residual risk

* **A skipped paint is only as good as the next paint.** If the click queued
  behind a view fails before publishing, the panel keeps the previous view until
  the next click. State stays authoritative and every panel reads state, so no
  operation is affected — the visible marks can lag.
* **A paint racing a close is narrowed, not eliminated** (the close edit is not
  routed through the painter; window = one edit RPC; outcome = a stale,
  unclickable panel).
* **The residual read-per-click** keeps DB-backed bursts linear in N. Reducing it
  needs a read-only/mutating handler taxonomy or concurrent reads for ordered
  views — deliberately not introduced here.
* **Live proof is missing.** That a real client sees a 30-click burst answered
  immediately and the panel ending on the newest view must be observed in
  production; the report does not claim it.

---

## Appendix A — Cloudflare Workers AI provider evidence (separate implementation task, 2026-10-25)

> This appendix documents a DIFFERENT task from the responsiveness investigation
> above, which is left intact. Every API claim is taken from current official
> Cloudflare documentation, cited inline. **No live Cloudflare request was
> made** — the repository side is source-verified, the API side is
> documentation-verified.

### A.1 Which endpoint, and why it is account-scoped

Workers AI's OpenAI-compatible surface is

```
POST https://api.cloudflare.com/client/v4/accounts/{account_id}/ai/v1/chat/completions
```

with `Authorization: Bearer <api_token>` and a standard OpenAI body (`model`,
`messages`, `temperature`, `max_tokens`, `tools`/`tool_choice`,
`response_format`), answering the standard OpenAI `choices[].message` / `usage`
shape. The same page documents `/v1/embeddings`, and `/v1/responses` for the
GPT-OSS models only. — [OpenAI compatible API endpoints](https://developers.cloudflare.com/workers-ai/configuration/open-ai-compatibility/)

Consequence for this repository: there is **no host root** to put in a static
`base_url`, which is why `cloudflare` is the only provider whose endpoint is
composed in the factory rather than in `defaults.py`. A token alone is not a
usable configuration.

The native (non-OpenAI) REST endpoint `/ai/run/{model}` answers
`{"result": {"response": …}, "success": true, "errors": [], "messages": []}` —
[REST API quickstart](https://developers.cloudflare.com/workers-ai/get-started/rest-api/).
It is **not** used: the provider layer is an OpenAI-compatible client and the
compatible endpoint is the documented one for SDK use.

### A.2 Authentication

A Cloudflare API token in the standard Bearer header; for a manually created
token the guide grants **Workers AI – Read** and **Workers AI – Edit**. The
legacy *global API key* is a different credential and is not a Bearer token,
which is why only `*_API_TOKEN` names are accepted
([REST API quickstart](https://developers.cloudflare.com/workers-ai/get-started/rest-api/)).

### A.3 Errors are a different envelope, with meaningful codes

`{"success": false, "errors": [{"code": …, "message": …}], "result": null}` — not
the OpenAI `{"error": {…}}` object. Documented codes that change classification
([Workers AI errors](https://developers.cloudflare.com/workers-ai/platform/errors/)):

| Code | HTTP | Meaning | Classification |
|---|---|---|---|
| `5007` | 400 | `No such model … or task` | `model_not_found` (never retried; model marked unavailable) |
| `3042` | 404 | invalid model id | `model_not_found` (already the status rule) |
| `5035` | 403 | model requires a Workers Paid plan | `request` — **not** `auth`, so one paid-only model cannot disable the provider |
| `3007`/`3008` | 408 | request timeout / aborted | `timeout` (retryable) |
| `3036` | 429 | daily free neuron allocation exhausted | `rate_limited` |
| `3040` | 429 | capacity temporarily exceeded | `rate_limited` |

The `retry-after` header (when present) is honored by the existing 429 path.
Nothing in `ProviderManager` needed to change: its retry / cooldown / quarantine
/ fallback ownership is untouched, and only the adapter's two classification
hooks are overridden.

### A.4 There is no OpenAI-style `/models`

The compatible surface documents chat completions, embeddings and (GPT-OSS only)
responses — no model-list endpoint. `GET …/ai/v1/models` therefore cannot verify
anything (a 404), so:

* the adapter's `list_models()` returns `[]` without a request;
* discovery's probe keeps its existing conservative policy (a non-auth status
  leaves the provider configured and selectable, `validated=False`);
* the model selector is fed from the curated `_FALLBACK_CATALOG` entry, the
  repository's documented place for offline model lists.

### A.5 Model ids and the free tier

Ids are `@cf/<vendor>/<model>` and are forwarded verbatim in the JSON body (the
OpenAI-compatible endpoint takes the model in the body, not in the path, so the
`@` and `/` need no encoding). Documented as function-calling capable:
`@cf/meta/llama-3.3-70b-instruct-fp8-fast`,
`@cf/meta/llama-4-scout-17b-16e-instruct`, `@cf/openai/gpt-oss-120b`,
`@cf/openai/gpt-oss-20b`, `@cf/qwen/qwen3-30b-a3b-fp8`,
`@cf/zai-org/glm-4.7-flash` — per their model pages under
[Workers AI models](https://developers.cloudflare.com/workers-ai/models/). The
chosen default is `@cf/meta/llama-3.3-70b-instruct-fp8-fast`
([model page](https://developers.cloudflare.com/workers-ai/models/llama-3.3-70b-instruct-fp8-fast/)).

The Free plan includes **10,000 Neurons/day**, reset at 00:00 UTC, shared across
models; some models require the Workers Paid plan
([pricing](https://developers.cloudflare.com/workers-ai/platform/pricing/)).

### A.6 What is still unproven

No Cloudflare account, token or account id was used, so nothing here proves that
the token's permissions are sufficient, that the account id matches the
deployment, that the default model is enabled for that account, or that the free
allocation has headroom. Those are live checks for the owner after the
deployment variables are set.

---

# Appendix B — Custom emoji, thinking/progress indicators, and table rendering (investigation, 2026-10-29)

> Scope of this appendix: an **investigation only**. No application code, provider,
> schema, dependency or delivery behaviour was changed. The only file edited is
> this report. Every claim below is labelled
> **CONFIRMED FROM SOURCE**, **CONFIRMED BY TEST**, **CONFIRMED BY OFFICIAL
> DOCUMENTATION**, **INFERENCE**, or **UNRESOLVED / REQUIRES LIVE VERIFICATION**.
> No Telegram account, session string or network call was used.

## B.1 Executive summary

1. **Custom emoji is not a missing feature in this repository — it is a large,
   already-implemented subsystem** (library import, replacement transformer,
   three distinct send routes, a diagnostics panel, ~1,900 lines of tests). The
   earlier premise "investigate whether any implementation exists" is answered
   by: it exists, it is entity-based (never HTML), and its **delivery** outcome is
   the only unproven part. What is missing is a *production* custom-emoji send
   path that is known to preserve the entity.
2. **`<tg-emoji emoji-id="…">` IS supported by the installed Telethon 1.43.0 HTML
   parser** (verified). This contradicts the common assumption that the tag is
   Bot-API-only. The project does not use it anywhere; it builds
   `MessageEntityCustomEmoji` objects directly instead.
3. **`<tg-thinking>` is NOT a Telegram MTProto/Telethon construct.** It is a
   **Bot API 10.2+ Rich Message** HTML tag (`sendRichMessageDraft` only, bots
   only). Telethon 1.43.0 silently drops it (verified: text survives, tags
   vanish, zero entities). LifeOS therefore cannot use it at all.
4. The current "thinking" indicator is **one `event.edit("Thinking…")`** plus
   opportunistic status edits per tool call. There is **no timer, no streaming,
   no progress loop** — which matches the project's single-scheduler constraint.
5. **Table rendering already exists** (`backend/ai/tools/delivery.py`) and is
   wired into the AI output pipeline. It works end-to-end because the Telethon
   client's default `parse_mode` is **markdown**, and the formatter emits a
   fenced code block, which Telethon converts to `MessageEntityPre` (verified).
6. **Two real table defects are confirmed by measurement**: (a) URLs / inline
   code inside a cell are measured as `\u0000N\u0000` placeholders, so those
   columns overflow after restoration; (b) the width model counts ZWJ/flag emoji
   sequences as 4–6 columns while they render as 2, so emoji columns drift.
7. `process_output()` computes `RenderedOutput.entities` and **no delivery path
   ever uses it** — formatting is delivered entirely by Telethon's implicit
   markdown parse, not by the entities the pipeline builds. That is dead code
   with a misleading contract.

## B.2 Baseline (verified at investigation start)

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Worktree branch | `freebuff/6fcab3542194eccf8041805e` |
| HEAD | `9240f562c2a145f22a5ec84c7a3aba5638718814` (`feat: add Cloudflare Workers AI provider`) |
| Working tree | **clean** (`git status --porcelain` empty) before this report |
| `INVESTIGATION.md` before this appendix | 278 lines (Appendix A = Cloudflare provider) — preserved verbatim |
| Dependency manifest | `backend/requirements.txt` |
| Telethon pin | **`telethon==1.43.0`** (`telethon.__version__` → `1.43.0` from a project-local `.venv`) |
| Local interpreter | Python 3.12.3 (project documents/targets **3.11**; no 3.11-only syntax was needed for these checks) |
| Other pins | `fastapi==0.111.0`, `uvicorn[standard]==0.29.0`, `supabase==2.4.2`, `aiofiles==23.2.1`, `httpx==0.27.0`, `tzdata==2026.3`, `pypdf==6.18.1` |
| Test runner | `.venv/bin/python -m pytest` (pytest + pytest-asyncio installed into `.venv`, git-ignored) |
| Baseline full suite | **6838 passed, 1 failed, 27 skipped** in 150.76s |
| The 1 failure | `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once` → `assert 0 == 1`, log `Task 1 was not scheduled: interval advancement exceeded bound`. Reproduced standalone (0.73s) and within its file (10 passed, 1 failed). **Unrelated to this appendix** (Taskloom scheduling; no emoji/AI-output/delivery code involved). Pre-existing, reported not fixed. |

The AGENTS.md "verified delivery state" table still names `6bec694…` as HEAD;
the actual checkout is `9240f56`. Recorded here so the next session does not
trust the stale snapshot.

## B.3 Telegram client, authentication, and the actual parse mode

| Question | Answer | Evidence |
|---|---|---|
| Framework | **Telethon / MTProto** (not Pyrogram, not Bot API) | `backend/bot/client.py::build_client`, `from telethon import TelegramClient` |
| Session type | `StringSession` (never file, never interactive) | `build_client(api_id, api_hash, session_string)` → `TelegramClient(StringSession(session_string), …)` |
| Self client flags | `auto_reconnect=True`, `connection_retries=5`, `retry_delay=2`, **`flood_sleep_threshold=60`** | `backend/bot/client.py` |
| Second client | optional **helper bot** (`StringSession()` + `start(bot_token=…)`), same flood threshold | `backend/helper/client.py::build_helper` |
| Central send/edit | self client: `backend/telegram_api/messages.py::send_message` / `edit_message` (30s `guarded_await`, normalized `TelegramAPIError`/`TelegramTimeoutError`); helper/bot send: `backend/telegram_api/bridge.py::send_reconstructed` | source |
| AI delivery | `backend/ai/tools/delivery.py::deliver_response` and `deliver_single_message` — `event.edit(...)`, `event.reply(...)` fallback, `sender.send_file(...)` for the one-attachment media path | source |
| **Parse mode actually in effect** | **Telethon's default `parse_mode = markdown`.** `TelegramBaseClient.__init__` sets `self._parse_mode = markdown` (`…/telethon/client/telegrambaseclient.py:418`); `_parse_message_text` uses it whenever the call passes no `parse_mode` (`…/telethon/client/messageparse.py:74-87`). **No call site in `backend/` ever passes `parse_mode` or `formatting_entities` for AI delivery** (`grep -rn parse_mode backend --include=*.py` → no application hits). | source + grep |
| Consequence | every AI answer/edit is parsed by Telethon's **markdown** parser on the way out; formatting is implicit. A literal fenced block therefore becomes `MessageEntityPre`, `**x**` becomes bold, etc. | CONFIRMED BY TEST (§B.7 probe 4) |

Entity/UTF-16 handling that already exists and is reusable:
`backend/telegram_api/_helpers.py::utf16_length` (line 47),
`utf16_offset`, `utf16_index_at` (raises on mid-surrogate instead of clamping),
`_serialize_entity_list`, and `dict_entities_to_tl` (line 206) which already
rebuilds `MessageEntityCustomEmoji(offset, length, document_id)` (line 243).

## B.4 Call graphs (entry point → Telegram boundary)

### B.4.1 AI request (thinking → answer)

```
owner sends "Nova …" (or replies to an AI message)
  backend/bot/handlers/ai_unified.py::ai_unified_handler   (events.NewMessage(outgoing=True))
    → is_owner (backend/bot/handlers/guard.py)
    → _load_triggers → ai/config_store.get_config         (30s TTL cache, durable row)
    → _extract_reply_context / _media_type_of
    → _execute_ai  (line 638)
        → _restore_config → engine.apply_persisted_config
        → _load_telegram_chat_context (ONE bounded read)
        → event.edit(format_thinking(display_prompt, show_question))    line 749  ← THE progress indicator
        → engine.execute(request, status_callback=_status_callback)     line 754
              backend/ai/engine/engine.py::execute → dispatcher.dispatch
                → media path: status_callback("… Reading media...")    dispatcher.py:1529-1531
                → ToolExecutor.execute_calls / execute_confirmed
                      status_callback(_STATUS_LABELS[tool] or "⏳ Running <tool>...")  executor.py:185-188, 218-221
        → deliver_response(event, display_prompt, response_text, show_question)   (line 844)
        / deliver_single_message(...)                                        (line 730, media answers)
              process_output → format_presentation → _format_chunks → event.edit / event.reply / send_file
```

* `_status_callback` itself (ai_unified.py:741) simply re-renders
  `format_status(display_prompt, status, show_question)` into the same message.
  It is **event-driven only** (one edit per tool call, one for media). No timer,
  no repeating task, no second scheduler — consistent with AGENTS.md §4.
* Outer timeout: `asyncio.wait_for(engine.execute(...), timeout=request.timeout_s or 240)`
  (`_AI_EXECUTE_TIMEOUT = 240.0`); concurrency `asyncio.Semaphore` =
  `AI_MAX_CONCURRENCY` (default 4) acquired with a 60s wait; `CancelledError` is
  re-raised; `finally` always releases the semaphore and ends diagnostics.

### B.4.2 Custom emoji (three mutually exclusive routes + one importer)

```
(A) helper bot sends the entity itself
    panel/action → services/emoji_replacement_service.py
      → emoji_transformer.transform_message(text, entities, mappings)   (pure, UTF-16 rebase)
      → telegram_api/bridge.py::send_reconstructed(..., entities=[…])
          → _helpers.dict_entities_to_tl(bot, entities) → MessageEntityCustomEmoji
          → bot.send_message(peer, text, formatting_entities=…)        bridge.py:111

(B) owner's account sends a HELPER-BOT inline result  (production route)
    bot/handlers/emoji.py → premium_emoji_inline_service.send_premium_emoji_via_inline (line 1007)
      → inspect_source_message (265)  → real MessageEntityCustomEmoji only
      → build_inline_payload (346)    → 1 entity over the document's own `alt`
      → inline_engine query → BotInlineResult with TextWithEntities
      → messages.sendInlineBotResult (self client) → read back by id → classify

(C) owner's account sends DIRECTLY  (diagnostic only, no production caller)
    premium_emoji_direct_service.send_premium_emoji_direct (295)
      → dict_entities_to_tl → telegram_api/messages.send_message(..., formatting_entities=…)
      → read back by id → classify

(D) import existing emoji from Saved Messages
    bot/handlers/emoji.py:239 → emoji_library_service.import_from_saved_messages (394)
      → extract_custom_emoji_records (104)   (document_id + exact alt span, UTF-16)
      → telegram_api/custom_emoji.get_custom_emoji_documents (108) / get_sticker_set
```

### B.4.3 Tables

```
AI text → delivery.process_output (247)
   → _normalize_plain (86)  → _render_markdown (104)  → _render_tables (181)
       → _split_table_row (141) / _is_table_separator (156) / _build_table_block (163)
           → _cell_display_width (133) → _display_width (118)
   → RenderedOutput(text, entities=_render_entities(text))
→ format_presentation / _format_chunks (pagination in UTF-16 units, SAFE_LIMIT=4000)
→ event.edit(text)  → Telethon markdown.parse  → MessageEntityPre  → messages.editMessage
```

## B.5 Current implementation status per requested feature

| Feature | Status | Where |
|---|---|---|
| Custom-emoji **entity extraction** | **IMPLEMENTED + TESTED (offline)** | `emoji_library_service.extract_custom_emoji_records`; `_helpers._serialize_entity_list` |
| Custom-emoji **library/set enumeration** | **IMPLEMENTED + TESTED (offline)** | `telegram_api/custom_emoji.py`; `emoji_library_service._enumerate_sets` |
| Custom-emoji **text replacement** | **IMPLEMENTED + TESTED (offline)** | `emoji_transformer.transform_message` |
| Custom-emoji **delivery via helper bot** | Implemented; **entity preservation CONTRADICTED by the project's own recorded live result** | `telegram_api/bridge.py`; ROADMAP §17 |
| Custom-emoji **delivery via inline bot** | Implemented; **fail-closed gate has never passed live** (`INLINE_RESULT_ENTITY_MISSING`) | `premium_emoji_inline_service` |
| Custom-emoji **direct self-send** | Implemented as a **diagnostic only**; preservation **never established** | `premium_emoji_direct_service`; live marker `LIFEOS_LIVE_PREMIUM_EMOJI_SELF_SEND` |
| Custom-emoji **in normal AI answers** | **NOT IMPLEMENTED** — no AI/delivery path emits `MessageEntityCustomEmoji` | `delivery.py` has no custom-emoji reference |
| `<tg-emoji>` **HTML usage** | **NOT USED ANYWHERE** (parser supports it anyway) | `grep -rn "tg-emoji" backend` → 0 |
| Thinking indicator | **IMPLEMENTED (minimal)**: single edit, event-driven status edits | `ai_unified.py:741-754`; `delivery.format_thinking` (481) / `format_status` (492) |
| `<tg-thinking>` | **NOT AVAILABLE** to a user account (Bot API Rich Message only) | §B.6.2 |
| Genuine token streaming | **NOT IMPLEMENTED**; providers return complete responses; dispatcher is request/response | `ai/providers/*`, `ai/engine/dispatcher.py` |
| Table rendering | **IMPLEMENTED + WIRED** (fenced pre block via implicit markdown parse); two width defects | `delivery.py:118-221`, `process_output` |
| Table **entity** (`MessageEntityPre` built by us) | **NOT USED** — `RenderedOutput.entities` is discarded | §B.6.3 defect T-0 |

## B.6 Findings and defects

### B.6.1 Custom emoji — what the source and documentation actually establish

1. **Entity shape** — `messageEntityCustomEmoji#c8cf05f8 offset:int length:int
   document_id:long = MessageEntity;` and
   `documentAttributeCustomEmoji#fd149899 flags:# free:flags.0?true text_color:flags.1?true alt:string stickerset:InputStickerSet`
   (**CONFIRMED BY OFFICIAL DOCUMENTATION**,
   <https://core.telegram.org/api/custom-emoji>, accessed 2026-10-29).
2. **Offsets are UTF-16 code units**, not code points (**CONFIRMED BY OFFICIAL
   DOCUMENTATION**, <https://core.telegram.org/api/entities>, accessed
   2026-10-29: "the number of UTF-16 code units … Code points in all other
   planes count as 2"). The repository already implements exactly this
   (`_helpers.utf16_length`), so offset correctness is a solved problem here.
3. **The entity must wrap exactly one regular emoji, the document's `alt`**,
   otherwise "the server will ignore it" (**CONFIRMED BY OFFICIAL
   DOCUMENTATION**, same custom-emoji page). Both `premium_emoji_*` services
   deliberately rebuild the payload around `document_alt` for this reason —
   correct by construction.
4. **Extraction with Telethon 1.43.0** — the numeric id is
   `entity.document_id` on a real `MessageEntityCustomEmoji`; resolution is
   `messages.GetCustomEmojiDocumentsRequest(document_id=[…])` →
   `DocumentAttributeCustomEmoji.alt` / `.free` / `.stickerset`
   (**CONFIRMED FROM SOURCE**: `telegram_api/custom_emoji.py:108-160`,
   `_serialize_document`).
5. **`<tg-emoji emoji-id="…">` is supported by the installed parser**
   (**CONFIRMED BY TEST**, §B.7 probe 6): `telethon/extensions/html.py` lines
   83-90 parse it into `MessageEntityCustomEmoji`, and `unparse` emits it back
   (line 162). So a future implementation *may* use `parse_mode='html'`; today
   nothing does.
6. **Premium / entitlement** — the API exposes `documentAttributeCustomEmoji.free`
   ("whether this emoji can be used by non-premium users") and the official blog
   states everyone may use **all** custom emoji **in Saved Messages**
   (**CONFIRMED BY OFFICIAL DOCUMENTATION**:
   <https://telegram.org/blog/custom-emoji>, accessed 2026-10-29). The
   custom-emoji API page itself does **not** state that a *user account* must be
   Premium to attach an entity, and it does not describe what the server does to
   a non-entitled account's send. The precise failure mode for a non-Premium
   self-account in an arbitrary chat is therefore
   **UNRESOLVED / REQUIRES LIVE VERIFICATION**.
7. **Bots** — "Custom emoji entities can only be used by bots that purchased
   additional usernames on Fragment" (**CONFIRMED BY OFFICIAL DOCUMENTATION**,
   <https://core.telegram.org/bots/api#html-style>, accessed 2026-10-29). This is
   the constraint behind route (A): the helper bot is an ordinary bot.
8. **The project's own recorded live outcomes** (source comments, not re-run
   here): route (A) → *"Telegram dropped the entity server-side"*
   (`premium_emoji_direct_service` docstring, point 1); route (B) → *"three live
   attempts ended at `INLINE_RESULT_ENTITY_MISSING`"* (same docstring, point 2);
   route (C) → *"the only one whose entity preservation has never been
   established"*. These are **CONFIRMED FROM SOURCE** as recorded history, and
   **UNRESOLVED** as current behaviour (they are not re-verified by this
   investigation, which made no Telegram call).
9. **No production consumer** — `premium_emoji_direct_service` has exactly one
   caller (a panel action); nothing in the AI answer path, Save path, or panel
   rendering emits custom-emoji entities. Custom emoji is therefore **not** part
   of LifeOS's everyday output today.

### B.6.2 Thinking / processing indicators

1. **`<tg-thinking>` is not a Telegram entity tag.** It corresponds to
   `RichBlockThinking`, documented as *"A block with a 'Thinking…' placeholder,
   corresponding to the custom HTML tag `<tg-thinking>`. The block may be used
   only in sendRichMessageDraft"* — a **Bot API** Rich Message facility added in
   Bot API 10.2 (June 2026) (**CONFIRMED BY OFFICIAL DOCUMENTATION** via
   <https://core.telegram.org/bots/api> §RichBlockThinking, accessed 2026-10-29;
   corroborated by the Bot API 10.2 changelog on the same page).
2. **Telethon 1.43.0 silently strips it** (**CONFIRMED BY TEST**, §B.7 probe 7):
   `html.parse("<tg-thinking>Thinking…</tg-thinking>")` → `("Thinking…", [])`.
   Same for `markdown.parse`. There is no entity, no error, no rendering effect.
3. **Repository search** — `grep -rn "tg-thinking" .` → **0 matches**
   (**CONFIRMED FROM SOURCE**). The project never attempted it.
4. **What exists instead** — one initial `event.edit("Thinking…")` (or the
   question-quoted variant) at `ai_unified.py:749`, plus
   `delivery.format_status` edits driven by `ToolExecutor` (per tool call) and
   the dispatcher's single media line. Both are *event-driven*; during one long
   provider call the message is static (**CONFIRMED FROM SOURCE**).
5. **No streaming** — providers are request/response (`ai/providers/*` return a
   complete `ProviderResponse`); the dispatcher has no token callback surface;
   only `status_callback(str)` exists. Genuine token streaming would require a
   provider-contract change (**CONFIRMED FROM SOURCE**).
6. **Concrete gaps/defects (source-verified)**
   - **P-1**: `format_thinking` hard-codes the English ellipsis form
     `"Thinking…"` regardless of the owner's language or the presentation
     preference (`delivery.py:481-489`). Persian requests show an English status.
   - **P-2**: `_render_markdown` is not applied to the *status* strings, so a
     tool label containing markdown metacharacters is parsed by the implicit
     markdown parse instead (they are plain today, so this is latent).
   - **P-3**: no heartbeat/elapsed timer, so a 240s provider timeout is
     indistinguishable from a 3s answer until it resolves. This is a UX gap, not
     a correctness bug, and adding a timer would introduce a second periodic
     writer — which the project explicitly forbids (§7 of the task brief,
     AGENTS.md §4).
   - **P-4 (minor inconsistency)**: `ToolExecutor._STATUS_LABELS`
     (`executor.py:61`) contains emoji (`⏳ Running …`), while AGENTS.md §16
     reserves symbols for the registry and forbids emoji glyphs. It is message
     text, not a panel glyph, so it does not violate the letter of the rule —
     but it is an inconsistency a future implementation should not copy.

### B.6.3 Table formatting

1. **Telegram has no table entity.** The entity list contains bold, italic,
   underline, strike, spoiler, blockquote, code, pre, text-url, mention, … and
   **no table** (**CONFIRMED BY OFFICIAL DOCUMENTATION**,
   <https://core.telegram.org/api/entities>, accessed 2026-10-29). "Monospace
   alignment" is therefore the only in-message table mechanism available to a
   user account. (Bot API 10.1+ *Rich Messages* add `InputRichBlockTable`, but
   that is bots-only and not reachable from MTProto — recorded, not usable here.)
2. **Telethon 1.43.0 has no table support in markdown**: `markdown.parse` of a
   pipe table returns the text unchanged with **0 entities** (**CONFIRMED BY
   TEST**, §B.7 probe 5). It does support fenced code → `MessageEntityPre`.
3. **The project's formatter**: `_render_tables` detects header + dash separator
   + equal-width body rows, computes per-column display widths, pads cells, wraps
   the block in a fenced code block, and fails closed (leaves text untouched)
   on ragged/ambiguous input (`delivery.py:141-221`).
4. **It is actually wired**: `process_output` calls
   `_render_tables(_render_markdown(_normalize_plain(text)))`, and
   `deliver_response` delivers `process_output(...).text` (**CONFIRMED FROM
   SOURCE**). End-to-end, Telethon's implicit markdown parse turns the fence into
   one `MessageEntityPre` (**CONFIRMED BY TEST**, §B.7 probe 4: `MessageEntityPre`
   at offset 0, length 55).
5. **Defects confirmed by measurement** (§B.7 probes 10-11):
   - **T-0 — the pipeline's own entities are discarded.** `process_output` builds
     `RenderedOutput.entities` from the *original* text via `_render_entities`
     (line 237) and no delivery path reads them (`grep` shows no consumer).
     Formatting therefore depends on Telethon's implicit markdown parse, not on
     this code. Two consequences: (i) the entity offsets it computes are
     unverified in delivery, (ii) any future `parse_mode=None` change would
     silently drop all formatting.
   - **T-1 — protected spans overflow their column.** `_render_tables` masks
     URLs, inline code and commands with `\u0000N\u0000` placeholders *before*
     computing widths, then restores them *after* padding. Measured: a
     `https://example.com/very/long/path` cell produced a 35-column row inside a
     6-column column, and a `` `save_code` `` cell produced an 11-column row
     inside a 5-column column. The separator and every other row are therefore
     misaligned.
   - **T-2 — the width model over-counts emoji sequences.** `_display_width`
     sums per code point: ZWJ family `👨‍👩‍👧` = **6** columns, flag `🇮🇷` = **4**,
     while both render as a single 2-column glyph. Measured: the ZWJ row is
     padded to 6 while rendering 2 wide → the column drifts. East-Asian wide
     (`漢` = 2) and combining marks (=0 plus a 1-column base) are handled
     correctly by this model.
   - **T-3 — RTL inside a pre block cannot be column-stable (INFERENCE,
     strong).** The presentation layer injects BiDi isolates inside each
     rendered line, including every table line
     (`format_presentation` → `_bidi_isolate`). Inside a `MessageEntityPre`
     Telegram lays the run out with the same Unicode BiDi algorithm, so a
     Persian/Arabic cell's *visual* column order is reversed/reshuffled relative
     to the padded character order. No padding scheme that assumes left-to-right
     columns can survive this; a per-line RTL table would need a different
     layout entirely. **UNRESOLVED without a device check** (see §B.8).
6. **Fixed-width padding across clients** — Telegram renders `pre` in a
   monospace font, so *character* alignment is reliable for BMP non-emoji text;
   it is **not** reliable for (a) emoji/ZWJ/flags, (b) RTL runs, (c) cells wider
   than the viewport (Telegram soft-wraps inside `pre`, destroying alignment),
   or (d) any proportional fallback for glyphs missing from the mono font.
   These limits are structural; the report does **not** claim perfect alignment
   on any client.

## B.7 Tests and probes actually executed

All commands were run from the repository root with a project-local venv
(`python -m venv .venv && .venv/bin/pip install -r backend/requirements.txt
pytest pytest-asyncio`). **No Telegram network call was made.**

| # | Command / probe | Actual result |
|---|---|---|
| 1 | `.venv/bin/python -m pytest tests/test_premium_emoji_inline.py tests/test_premium_emoji_direct.py tests/test_premium_emoji_probe.py tests/test_premium_emoji_self_send_route.py tests/test_emoji_library_import.py tests/test_emoji_set_enumeration.py tests/test_emoji_replacement_phase4.py tests/test_bridge_delivery.py -q` | **412 passed, 1 skipped** (3.56s); the skip is the `live_telegram` test |
| 2 | `.venv/bin/python -m pytest tests/test_67_ai_output_pipeline.py tests/test_ai_presentation_redesign.py tests/test_ai_provenance.py tests/test_emoji_ui.py tests/test_emoji_diagnostics_ui.py -q` | **231 passed** (1.87s) |
| 3 | `.venv/bin/python -m pytest tests -q -p no:randomly` | **6838 passed, 1 failed, 27 skipped** (150.76s); failure = `test_task_scheduler…run_once`, unrelated (§B.2) |
| 4 | probe: `process_output("| Name | Value |…")` then `markdown.parse(rendered)` | rendered = fenced block; **`MessageEntityPre`** `(0, 55)`; `text != rendered` (delimiters consumed, entity carries the style) |
| 5 | probe: `markdown.parse("| a | b |\n|---|---|\n| 1 | 2 |")` | text unchanged, **0 entities** — no table support |
| 6 | probe: `html.parse('<tg-emoji emoji-id="5368324170671202286">👍</tg-emoji>')` | `('👍', [MessageEntityCustomEmoji(0, 2, 5368324170671202286)])` — **supported** |
| 7 | probe: `html.parse('<tg-thinking>Thinking…</tg-thinking>')` | `('Thinking…', [])` — **silently stripped** |
| 8 | probe: `html.parse('<pre>a   b\nc</pre>')`, `'<pre><code class="language-python">x=1</code></pre>'`, `'<blockquote>q</blockquote>'` | `MessageEntityPre(0,7)`, `MessageEntityPre(0,3)` with language `''` (a language hint is **not** reliably recovered from that nesting), `MessageEntityBlockquote(0,1)` |
| 9 | probe: `html.unparse("👍", [MessageEntityCustomEmoji(0,2,…)])` | `<tg-emoji emoji-id="5368324170671202286">👍</tg-emoji>` |
| 10 | probe: `_cell_display_width` over `مَ`, `👍`, `👨‍👩‍👧`, `🇮🇷`, `漢`, `س` | `1, 2, 6, 4, 2, 1` — ZWJ/flag over-counted (**T-2**) |
| 11 | probe: table with a URL cell and with an inline-code cell | URL row 35 wide in a 6-wide column; code row 11 wide in a 5-wide column (**T-1**) |
| 12 | probe: `format_presentation` of a table with `show_question=True` | one `MessageEntityPre` `(17, 96)`; the four-space continuation indent lands **inside** the pre block (uniform, so rows still align with each other) |

Existing coverage worth knowing (all passing above):
`tests/test_67_ai_output_pipeline.py` asserts per-column padding equality for a
mixed Persian/CJK/emoji table (it calls the production `_cell_display_width`, so
it is not a test-only re-implementation), determinism of a second pass, and that
150-row tables survive pagination; `tests/test_premium_emoji_self_send_route.py`
contains the opt-in live test (marker `live_telegram`,
`LIFEOS_LIVE_PREMIUM_EMOJI_SELF_SEND=1` + `LIFEOS_LIVE_PREMIUM_EMOJI_DOC_ID`),
skipped here by design.

## B.8 Live-verification requirements (cannot be answered offline)

| # | Question | Why offline cannot answer |
|---|---|---|
| L-1 | Does a **non-Premium self account** keep a `MessageEntityCustomEmoji` when sending to (a) Saved Messages, (b) another chat? | server-side entitlement decision; the API page documents `free` but not the failure mode |
| L-2 | Does a **Premium self account** keep it everywhere? | same |
| L-3 | Is route (B)'s `INLINE_RESULT_ENTITY_MISSING` still the outcome, or was it a helper-bot state? | needs a real inline query round trip |
| L-4 | Does route (A) still drop the entity (Fragment constraint)? | server-side bot entitlement |
| L-5 | How do Persian/Arabic cells and emoji columns **render** in Telegram Desktop/Android/iOS inside `pre`? | client rendering + BiDi layout |
| L-6 | Does a rapid sequence of thinking→status→answer edits hit `FLOOD_WAIT`? | server rate limiting (`flood_sleep_threshold=60` would sleep inside the call) |

No live test was run and none is claimed. The opt-in harness already exists
(`tests/test_premium_emoji_self_send_route.py -m live_telegram`) and must be run
by the owner with their own session; it writes only to the owner's Saved
Messages and reports an honest diagnosis.

## B.9 Minimal examples (verified vs illustrative)

**Verified — custom-emoji entity with correct UTF-16 offsets** (run in this
investigation, no network):

```python
from telethon.tl.types import MessageEntityCustomEmoji
from backend.telegram_api._helpers import utf16_length

text = "🙂 ok"                      # 🙂 is a surrogate pair
entity = MessageEntityCustomEmoji(0, utf16_length("🙂"), 5368324170671202286)
assert (entity.offset, entity.length) == (0, 2)
assert utf16_length("س") == 1       # Persian BMP text stays 1 unit/char
```

**Verified — the installed parser accepts the tag** (equivalent to building the
entity above):

```python
from telethon.extensions import html
text, entities = html.parse('<tg-emoji emoji-id="5368324170671202286">👍</tg-emoji>')
assert text == "👍" and entities[0].document_id == 5368324170671202286
```

**Verified — a fenced block becomes preformatted text:**

```python
from telethon.extensions import markdown
_, entities = markdown.parse("```\na | b\n```")
assert type(entities[0]).__name__ == "MessageEntityPre"
```

**Illustrative / UNVERIFIED** (never sent — requires a live account and an
entitlement decision):

```python
# Route (C), exactly as premium_emoji_direct_service already does it.
sent = await client.send_message(
    "me", "Direct self-chat test: 🙂",
    formatting_entities=[MessageEntityCustomEmoji(20, 2, DOCUMENT_ID)],
)
# Whether the stored message still carries the entity is a READ-BACK fact:
stored = await client.get_messages("me", ids=sent.id)
assert stored.entities  # NOT proven offline
```

## B.10 Recommended implementation plan (ordered by dependency and risk)

1. **Decide the custom-emoji product scope first (no code).** Choose exactly one
   of: (a) custom emoji only in the owner's Saved Messages, (b) premium-only
   decorative prefixes for AI answers, (c) no production use (keep the
   diagnostic panels). This decision gates everything else; §B.8 L-1/L-2 must be
   answered by the owner's live test before promising any behaviour.
2. **Make AI delivery's formatting explicit (low risk, high value).** Pass the
   entities the pipeline already computes: `event.edit(text,
   formatting_entities=process_output(...).entities)` with `parse_mode=None`, or
   — simpler and closer to today — keep the implicit markdown but delete the
   dead `RenderedOutput.entities` contract so no future caller trusts it. Either
   way: one central change in `delivery.py` only. (Fixes **T-0**.)
3. **Fix table measurement (low risk).** Measure cells on the **unmasked** text
   (protect only for detection, or record the placeholder→original width), and
   measure emoji as grapheme clusters with width 2 rather than per code point.
   (Fixes **T-1**, **T-2**.) Keep the fail-closed behaviour for ragged input.
4. **Table default for narrow screens (product).** Given **T-3**, do not promise
   Persian tables. Recommended default: for RTL-dominant or >4-column content,
   render a **key–value list** (one `label: value` per line, no padding) instead
   of a padded grid; keep the monospace grid only for LTR, ≤4 columns, no
   emoji/URL cells. This lives entirely in `_render_tables`.
5. **Thinking indicator (no new scheduler).** Keep the existing single
   `event.edit` and the event-driven status callback. If more feedback is
   wanted, enrich the *content* of those same edits (e.g. a tool name from
   `_STATUS_LABELS`, a localized word replacing "Thinking…"), not the cadence.
   Explicitly reject `<tg-thinking>` (§B.6.2) and reject a timer/loop.
6. **Only then consider a production custom-emoji send path**, and only if step 1
   chose (a) or (b): reuse `dict_entities_to_tl` + the existing guarded send,
   reuse the existing read-back classifier for honest reporting, and keep the
   helper-bot/inline routes exactly as they are (they are diagnostic-grade).
7. **Leave the live tests opt-in.** No new test may send to a real account
   without `LIFEOS_LIVE_*` gating.

Explicitly **not** recommended: a second delivery module, a periodic progress
task, token streaming for this release, HTML `<tg-emoji>` rewrites of existing
text, or an alt-text fallback for entitlement failures (owner-rejected in
ROADMAP §34-D).

## B.11 Exact files a future implementation phase should change

| File | Change |
|---|---|
| `backend/ai/tools/delivery.py` | T-0 (explicit entities or remove the dead contract); T-1/T-2 width fixes in `_render_tables` / `_display_width` / `_pad_cell`; optional RTL key–value fallback; localization of `format_thinking` (P-1) |
| `backend/bot/handlers/ai_unified.py` | only if the status text/localization changes (`_format_thinking`, `_status_callback`) — cadence must stay event-driven |
| `backend/telegram_api/_helpers.py` | only if grapheme-aware width or a shared entity helper is reused; `utf16_*` and `dict_entities_to_tl` are already correct |
| `backend/services/premium_emoji_direct_service.py` | only if step 1 promotes route (C) to production (it is currently diagnostic-only, by contract) |
| `tests/test_67_ai_output_pipeline.py` | add T-1/T-2 cases: URL cell, inline-code cell, ZWJ/flag cell, RTL table |
| `tests/test_ai_presentation_redesign.py`, `tests/test_ai_provenance.py` | only if the thinking/status strings change |
| `INVESTIGATION.md` | record the live results (L-1…L-6) when they exist |

## B.12 Explicitly out of scope (must remain untouched)

* `backend/services/save_service.py` and the Deep Save / album / view-once pipeline.
* `backend/helper/` panel machinery (`panels.py`, `callback_gate.py`, `lifecycle.py`,
  `panel_edit.py`, `progress.py`, `save_progress.py`) — the Glass UI delivery path
  and its single-writer/one-turn contracts.
* `backend/runtime/` — supervisor, heartbeat, keepalive, failsafe, task guard.
* `backend/ai/engine/dispatcher.py` provider/retry/fallback ownership and
  `backend/ai/providers/*` contracts (no streaming work in this scope).
* Taskloom (`ai/task_scheduler.py`, `task_execution.py`, `task_contract.py`) and
  the pre-existing `test_task_scheduler` failure (reported, not fixed).
* `supabase/`, `DATABASE_ARCHITECTURE.md`, `ROADMAP.md`, `AGENTS.md`,
  `backend/requirements.txt` (no version changes), the React dashboard, `guide/`.
* Any second client, scheduler, executor, update loop or delivery system.

## B.13 Sources (all accessed 2026-10-29)

* Custom emoji (entity + attribute + `getCustomEmojiDocuments`): <https://core.telegram.org/api/custom-emoji>
* Styled text / entity offsets in UTF-16 / allowed entities (no table entity): <https://core.telegram.org/api/entities>
* Telegram Premium and the `premium` user flag: <https://core.telegram.org/api/premium>
* Bot API HTML style (`tg-emoji`, Fragment constraint) and `RichBlockThinking` / `<tg-thinking>` / `sendRichMessageDraft`: <https://core.telegram.org/bots/api#html-style>
* "Everyone can use all custom emoji for free in Saved Messages": <https://telegram.org/blog/custom-emoji>
* Installed library source actually read (not documentation): Telethon 1.43.0 —
  `telethon/extensions/html.py` (tags, `tg-emoji` at lines 83-90, `unparse` at 148-162),
  `telethon/extensions/markdown.py` (no custom emoji, no tables),
  `telethon/client/telegrambaseclient.py:418` (default `parse_mode = markdown`),
  `telethon/client/messageparse.py:74-97`, `telethon/client/messages.py:883-906`
  (`parse_mode=None` plus `formatting_entities`),
  `telethon/tl/custom/message.py:397-409`.
