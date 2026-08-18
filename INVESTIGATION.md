# INVESTIGATION — Comprehensive LifeOS Forensic Report

## INVESTIGATION METADATA

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Current HEAD (at investigation) | `beb018163036b200cb38a257b922c266140e57b4` |
| Investigation date | 2026-08-18 |
| Scope | Runtime stability · Save system · AI system · Glass UI / handlers · Cross-subsystem interactions |
| Status | INVESTIGATION ONLY — no production code modified |
| Verification status | SOURCE VERIFIED (read-only). No live Telegram, no test run, no remote execution performed for this report. |

> This report is the canonical, current investigation and completely replaces the
> previous investigation. GitHub `main` is the source of truth for the code; this
> file only reports what was verified in the actual source.

---

## 1. EXECUTIVE SUMMARY

The repository has evolved far beyond the dot-command self-bot described in
`AGENTS.md`. The current architecture is a **Glass UI (inline panel) first** system:

- The self-bot (Telethon `StringSession`) is the runtime host; an optional
  helper bot (`BOT_TOKEN`) renders inline panels and receives callback queries.
- A `RuntimeSupervisor` owns the client, a heartbeat, a keepalive, a failsafe,
  an asyncio diagnostics loop, a memory-cleanup worker, the web server, and a
  shared per-minute profile scheduler.
- Save is **Deep Save only** (download → re-upload). Forward Save is removed.
- AI is triggered by trigger words / reply-to-AI / `.ai`, dispatched through an
  Engine → ProviderManager → Provider → ToolExecutor pipeline.
- Bio and Username share one profile scheduler with per-engine active state.

**Highest-confidence runtime-stability defect (CONFIRMED):** the failsafe
monitor — the "last line" that is supposed to hard-reset a frozen runtime —
references `guarded_create_task` without importing it, so when its freeze
condition actually triggers it raises `NameError` and the hard reset never runs.

**Most likely cause of the reported intermittent freeze/sleep (LIKELY):**
self-inflicted reconnect churn. The heartbeat treats three benign states as
"stalled" — `READY_BUT_DISCONNECTED` (fires when the helper bot is legitimately
disabled), `UPDATE_PIPELINE_STALLED` and `CALLBACK_DISPATCH_STALLED` (fire on a
naturally idle account) — and calls `_trigger_reconnect()`, which performs a
full `disconnect()` → `connect()` with no post-success cooldown, while the run
loop (`run_until_disconnected`) is simultaneously driving the same client.

**Highest-confidence Save/AI cross-defect (CONFIRMED):** Deep Save is correctly
unbounded at the service layer, but two of its entry points impose blanket
timeouts that can kill a legitimate large transfer: the Glass UI input listener
(60 s) and the AI `ToolExecutor` (10 s per tool).

**Documentation drift (CONFIRMED):** `AGENTS.md` (and several module docstrings)
still document `.ping`, `.id`, `.help`, `.save f/d`, `.preview`, `.send`,
`.del`, `.organize`, `.bio`, `.username`, and the `SV-NNNNNN` save-code format —
none of which exist in the current code. Only `.menu` and `.ai` are registered
dot commands, plus trigger/reply AI activation.

---

## 2. RUNTIME STABILITY

### Architecture

Entry point `backend/main.py::main()` → `config.load()` → install crash
diagnostics → `RuntimeSupervisor(cfg).start()` (up to 5 startup attempts) →
`supervisor.shutdown_event.wait()` → `supervisor.stop()`.

`backend/runtime/supervisor.py::RuntimeSupervisor` owns:

- `_build_and_register()` — builds the Telethon client, increments generation,
  registers handlers, wires AI tools.
- `_start_helper()` — optional helper bot (only when `helper_enabled`).
- `_resume_bio_cron()` / `_resume_username_cron()` — resume shared profile scheduler.
- `_run_loop()` — `client.run_until_disconnected()` in an immortal task.
- `_trigger_reconnect()` — lightweight disconnect/connect.
- `_trigger_full_recovery()` / `_do_recovery()` / `_retry_full_recovery()` —
  rebuild path with backoff and a 180 s cooldown.
- `_hard_reset_runtime()` — failsafe path (destroys all Telethon tasks, rebuilds).
- `_watchdog_loop()` — **defined but never started** (see Confirmed Facts).
- `_start_web_server()` — uvicorn `Server.serve()` in an immortal task.

Three independent monitor tasks run as `immortal_create_task` (from
`backend/runtime/task_guard.py`): heartbeat (30 s), keepalive (60 s), failsafe
(15 s check / 120 s freeze threshold), plus an asyncio diagnostics loop (60 s)
and a memory-cleanup worker (6 h).

### Execution Flow

```
python -m backend.main
  → config.load()
  → install_crash_diagnostics()   (signal handlers, excepthooks, ring buffers)
  → RuntimeSupervisor.start()
      → settings_service.load_all()
      → _build_and_register()     (build_client → register_all → _wire_ai_tools)
      → _start_helper()           (if helper_enabled)
      → _resume_bio_cron() / _resume_username_cron()
      → _start_web_server()
      → start_heartbeat() / start_keepalive() / start_failsafe() / start_diagnostics()
      → _run_task = immortal_create_task(self._run_loop)
      → start_memory_cleanup()
  → await supervisor.shutdown_event.wait()
  → supervisor.stop()
```

Monitoring sources that can trigger recovery:

1. `heartbeat._heartbeat_loop` — state-invariant + stall detection → `_trigger_reconnect`.
2. `keepalive._keepalive_loop` — `get_me` RPC probe (15 s) → `_trigger_reconnect`.
3. `failsafe._failsafe_loop` — all-signals-frozen → `_hard_reset_runtime`.
4. `supervisor._watchdog_loop` — **dormant** (never started).

### Timeout / Watchdog Audit

| Component | Operation protected | Bounded? | Cancellation safe? | Notes |
|---|---|---|---|---|
| `build_client` | connect / authorize / get_me | 30/15/15 s via `wait_for` | yes | raises on invalid session |
| `_trigger_reconnect` | disconnect / connect / authorize | 10/30/15 s | yes | **no post-success cooldown** |
| `_do_recovery` | helper stop, run task cancel, old client disconnect | 5–10 s each | yes | backoff + 180 s cooldown on success |
| `_hard_reset_runtime` | build/register/verify | 60/30/15 s | yes | guarded by recovery lock |
| `keepalive` | `get_me` | 15 s | yes | → `_trigger_reconnect` |
| `heartbeat` | none (pure state read) | n/a | n/a | → `_trigger_reconnect` on false positives |
| `failsafe` | all-signal freeze detection | 15 s loop / 120 s threshold | yes | **`NameError` on trigger** (see bugs) |
| `operation_watchdog.guarded_await` | DB / short RPC | caller-specified (10–30 s) | yes | the only timeout actually used |
| `operation_watchdog.bounded_operation` | (unused) | **no internal timer** | n/a | dead code |
| `tg_retry.tg_rpc` | (unused) | 30 s + retries | yes | dead code |
| `save_service.execute_save` | download / upload | **intentionally unbounded** | yes | correct for large files |
| `inline_sender._input_listener` | pending-input handler | 60 s | yes (cancels handler) | can kill a long Deep Save |
| `ai ToolExecutor` | every tool incl. `save` | 10 s | yes (cancels tool) | can kill a long Deep Save |
| `ai handler` | whole `engine.execute()` | 60 s | yes | bounds AI+tool total |

### Lock / Cancellation Audit

- `RuntimeSupervisor._recovery_lock` (`asyncio.Lock`) serializes reconnect /
  full recovery / hard reset. Acquired via `async with` in reconnect/full
  recovery (auto-release), but `_hard_reset_runtime` acquires it with
  `wait_for(..., 10)` and releases it **manually** in several early-return
  branches and in `finally`. This is fragile but each early return does call
  `self._recovery_lock.release()` before returning; the `finally` guard is a
  backstop.
- `ProfileEngine`/`profile.scheduler` use no lock; the shared scheduler is a
  single task and updaters are awaited sequentially.
- `save_service` save-code generation is guarded by `db_client._save_code_lock`
  (`asyncio.Lock`) — safe.
- `Panels`/`LifecycleManager` use a single `asyncio.Lock` around all panel
  lifecycle transitions.
- `CancelledError` is re-raised in the deep-save `finally` block and in the
  profile scheduler, keepalive, and failsafe loops. `immortal_create_task`
  re-raises `CancelledError` so shutdown can cancel immortal tasks.

### Recovery Audit

- `RuntimeSupervisor` **is** the single recovery authority for the self client.
- `backend/diagnostics.py::recover_stalled` is a dormant, second,
  supervisor-like recovery function that cancels *every* non-protected task —
  **it has zero callers** (dead code, not an active second supervisor).
- `supervisor._watchdog_loop` is a dormant recovery path (never started).
- The failsafe's hard-reset trigger is broken by a `NameError` (see bugs), so
  the one "bypass everything and reset" recovery path never actually fires.

### Confirmed Facts

1. **`supervisor._watchdog_loop` is never started.** `start()` calls
   `start_heartbeat/start_keepalive/start_failsafe/start_diagnostics` and
   creates `_run_task`, but never `self._watchdog_loop`. The method exists
   (`supervisor.py` ~line 802) and contains the RPC check, helper restart,
   memory-pressure GC, stale-loop restart, and `_trigger_reconnect` logic, but
   is dead. Evidence: `backend/runtime/supervisor.py::start` has no
   `_watchdog_loop` reference; `grep _watchdog_loop` returns only the
   definition and `backend/helper/watchdog.py` (a different helper watchdog).
2. **Failsafe hard reset raises `NameError`.** `backend/runtime/failsafe.py`
   imports only `immortal_create_task` (line 28) but calls
   `guarded_create_task(sup._hard_reset_runtime(), ...)` (line 156) inside the
   freeze trigger. `guarded_create_task` is undefined in that module, so the
   call raises `NameError`, is swallowed by the surrounding `except Exception`,
   and the hard reset never runs. CONFIRMED by source.
3. **`READY_BUT_DISCONNECTED` fires when the helper bot is disabled.**
   `backend/runtime/heartbeat.py::_heartbeat_loop`:
   `if current_state == "READY" and (not self_connected or not helper_connected):`
   → `_trigger_reconnect()`. When `BOT_TOKEN` is unset,
   `config.load()` sets `HELPER_BOT_ENABLED=False` and
   `supervisor.helper_connected` never becomes `True`, so every 30 s heartbeat
   re-triggers a reconnect. CONFIRMED.
4. **Idle accounts are treated as stalled.**
   `heartbeat._heartbeat_loop` emits `UPDATE_PIPELINE_STALLED` when
   `last_update` age > 90 s while `rpc_healthy` (keepalive refreshes
   `last_rpc` every 60 s via `get_me`), and `CALLBACK_DISPATCH_STALLED` when no
   callback for > 90 s while `rpc_healthy`; both call `_trigger_reconnect()`.
   A quiet account (no messages, no button presses) therefore triggers
   reconnect every ~90 s. CONFIRMED.
5. **`_trigger_reconnect` has no post-success cooldown.** Only
   `_do_recovery` (full rebuild) sets `_recovery_cooldown_until`. A successful
   lightweight reconnect does not, so the heartbeat/keepalive can re-trigger it
   repeatedly. CONFIRMED.
6. **Dual connection ownership.** `_run_loop` runs
   `client.run_until_disconnected()` while `_trigger_reconnect` independently
   calls `client.disconnect()`/`client.connect()`. A reconnect's `disconnect()`
   makes `run_until_disconnected()` return, causing `_run_loop` to mark
   `_client_alive=False` / `telethon_connected=False`, sleep 2 s, and re-enter
   `run_until_disconnected()` while the reconnect is still in progress. Two
   drivers on one client. CONFIRMED (code path); the live interleaving is the
   LIKELY freeze mechanism.
7. **`run_startup_checks` is dead code.** `backend/runtime/startup_check.py`
   defines a full startup validation module, but neither `backend/main.py` nor
   `backend/runtime/supervisor.py` imports or calls it. `grep run_startup_checks`
   returns only the module's own docstring/definition. CONFIRMED.
8. **`recover_stalled` is dead code.** `backend/diagnostics.py::recover_stalled`
   has zero callers. CONFIRMED.
9. **`tg_rpc` is dead code.** `backend/runtime/tg_retry.py::tg_rpc` has zero
   callers; the `telegram_api/*` layer uses `guarded_await` instead. CONFIRMED.
10. **`bounded_operation` / `attach_task` are dead code and do not enforce a
    timeout.** `backend/runtime/operation_watchdog.py::bounded_operation` has no
    internal timer; `__aexit__` only emits a diagnostic when an *external*
    `CancelledError` arrives and a task was attached. Only `guarded_await`
    (which uses `asyncio.wait_for`) is used, and only for DB + short RPC.
    CONFIRMED.
11. **Supabase cannot block the event loop.** `backend/db/client.py::_run_sync`
    and `backend/ai/config_store.py::_run_sync` and
    `backend/ai/persistence.py::_run_sync` all use `asyncio.to_thread` + a
    10 s timeout. CONFIRMED.
12. **No sync `requests`/`time.sleep`/`urllib` in backend.** `grep` returns no
    matches; AI providers use `httpx.AsyncClient`. CONFIRMED.

### Likely Causes / Risks

- **LIKELY — reconnect churn is the freeze/sleep driver.** False-positive
  heartbeat triggers (items 3, 4) + no reconnect cooldown (item 5) + dual
  ownership (item 6) combine into repeated `disconnect()/connect()` cycles that
  drop the connection, invalidate in-flight RPCs (AI calls, saves, edits), and
  make the bot appear "asleep" while the process is actually alive and busy
  reconnecting.
- **LIKELY — the failsafe cannot rescue a real freeze.** Because of the
  `NameError`, even a genuine all-signals-frozen state cannot trigger the hard
  reset, so the intended last-resort recovery is unavailable.
- **SUSPECTED — `bounded_operation` gives a false sense of protection.** The
  context manager form documents cancellation guarantees but is never used and
  would not enforce a timeout if it were.

### Ruled Out

- **RULED OUT — silent process death with no diagnostic.** `main.py` +
  `crash_diagnostics.py` install `sys.excepthook`, `threading.excepthook`,
  `asyncio` exception handler, signal handlers, an `atexit` handler, and a
  `PROCESS_EXIT_REASON` trace; every explicit exit records a reason and dumps a
  crash snapshot. A *dead* process would leave an exit reason in Render logs.
- **RULED OUT — Supabase blocking the event loop.** `asyncio.to_thread` + 10 s
  timeouts on all DB access.
- **RULED OUT — sync AI HTTP blocking the loop.** providers use
  `httpx.AsyncClient`; `ProviderManager.chat` is `async` and bounded.
- **RULED OUT — blanket Deep Save timeout at the service layer.** The service
  correctly leaves `download_media`/`send_file` unbounded. (The blanket
  timeouts live at the *entry points*, not the service.)
- **RULED OUT — `recover_stalled` acting as a second active supervisor.** No
  callers.

### Unknowns

- Actual production env values (`BOT_TOKEN`, Supabase, AI keys) and whether the
  helper bot is enabled in production.
- Live Render logs at freeze time — the report infers the freeze mechanism from
  code; it cannot prove which of the false-positive triggers fired first
  without logs.
- Whether a genuine blocking call (a stuck Telethon RPC outside `wait_for`)
  ever occurs — no thread dump at freeze time was available.
- Live Telegram behavior for protected/self-destructing media.

---

## 3. SAVE SYSTEM

### Current Save Modes

**Deep Save is the only Save mode.** There is no Forward Save, no mode
selection, and no fallback to forwarding.

Evidence: `backend/services/save_service.py::execute_save` contains no
`forward_messages` and no `ForwardMessagesRequest`; the module docstring states
Deep Save is the only method. `grep forward_messages backend/` returns only
`retrieve_service.py` (retrieval) and the `telegram_api` facade — none in the
Save path.

### Forward Save

- **Removed from Save.** The old `.save f` path no longer exists.
- The *only* remaining forward is in `backend/services/retrieve_service.py`
  (`.send` / "retrieve by code" forwards the saved asset back to a chat), which
  is a different feature. CONFIRMED.
- `TelegramAPI.forward_messages` (facade) and
  `telegram_api/messages.py::forward_messages` still exist as a general
  Telegram primitive but are not part of Save.

### Deep Save

`backend/services/save_service.py::execute_save(client, owner_id, reply_msg, tz_str)`

1. Generate `save_code` via `db_client.get_next_save_code()` (short format, e.g. `S391`).
2. Resolve sender (`_resolve_sender` — calls `reply_msg.get_sender()`).
3. Extract source metadata (`_extract_source_media`).
4. Enforce `max_deep_save_mb` (default 50 MB) *before* any download.
5. Build caption (`build_caption` + `_append_original_text`).
6. Text-only source → `client.send_message("me", caption)` (a NEW message).
7. Media source → `tempfile.mkdtemp` → `client.download_media(reply_msg, file=tmp_path)`
   → validate file exists and is non-empty → `client.send_file("me", tmp_path, caption, ...)`
   with original media attributes preserved.
8. `finally: shutil.rmtree(tmp_dir)` — temp cleanup on every exit path;
   `asyncio.CancelledError` is re-raised.
9. Extract metadata from the **newly uploaded** message
   (`_extract_uploaded_metadata`), build the DB payload, and
   `db_client.insert_save(payload)` **after** the Telegram upload succeeds.

CONFIRMED: the pipeline is genuinely download → upload; the DB write happens
after a successful Telegram operation; `forward_messages` is never called.

### Glass UI Flow

```
.menu (misc.py pattern r"^\.menu$")
  → send_inline_panel(client, chat_id, "menu")
  → helper InlineQuery → menu panel
  → "📥 Save" (panel:save)
      → "⬇️ Deep Save" (panel:save:type:d)
          → "💬 Reply Mode" (action:save_reply)  → set_pending("save_reply")
          → "🔗 Save using a link" (input:save:link) → execute_link_save
  → owner replies to target message
      → _input_listener (inline_sender) matches pending input
      → _save_reply_wait_handler:
           client.get_messages(chat_id, ids=reply_msg_id)
           → target_id = reply.reply_to_msg_id
           → client.get_messages(chat_id, ids=target_id)
           → execute_save(client, owner_id, target_msg, tz)
      → edit inline message with result, delete the owner's reply
```

CONFIRMED: target resolution is `reply_to_msg_id` → exact message → Deep Save;
the user's reply message is never saved.

### Download / Upload Flow

- Download to a per-operation temp directory; upload from that temp path.
- `_upload_kwargs_for_media` preserves photos (no `force_document`) and copies
  the original document attributes (video/audio/voice/sticker/filename) so the
  re-upload renders as the same media type.
- No `BytesIO`; file-based temp storage with `shutil.rmtree` cleanup.

### File Handling

- Size limit enforced pre-download (`file_size > max_bytes` → honest error).
- Empty-download and missing-file guards exist.
- Large transfers are **unbounded** at the service layer (correct), but the
  entry points impose timeouts (see §2 Timeout Audit and §4/§5).

### Metadata

DB payload fields: `save_code`, `save_type="deep"`, `origin_chat_id`,
`origin_msg_id`, `saved_chat_id`, `saved_msg_id`, `sender_name`, `sender_id`,
`mime_type` (new-message mime or source mime), `file_id` (new or source),
`file_size` (`actual_size` or `new_size`), `media_type`, `tags`, `caption`,
`owner_id`, `created_at`.

- Metadata refers to the **new** message where available
  (`_extract_uploaded_metadata`), so retrieval points at the real saved asset.
- Partially-empty metadata is possible for edge cases (e.g. photo `size` is
  `None` in the new-message extraction → `file_size` falls back to `actual_size`,
  `saved_*` ids come from `sent`), but `insert_save` is only reached after a
  successful upload, so `saved_chat_id`/`saved_msg_id` should be populated.

### Caption

`build_caption` produces:

```
{icon} {save_code} · DEEP
👤 {sender}
🕒 {YYYY-MM-DD HH:MM}
🆔 {chat_id}/{msg_id}
🗂 {media_type} · {size} · {mime}
📄 {file_name}        (if present)
#saved #saved_<type> #saved_<year> #saved_<year>_<month> #saved_<year>_<month>_<day>
```

`_append_original_text` appends the original message text below. Format is
consistent between text and media sources (text uses the same builder).

### Database Interaction

- `db_client.insert_save` runs through `_run_sync` (thread + 10 s timeout).
- On insert returning `None`, the save reports "uploaded but DB record failed"
  and writes a `bot_logs` ERROR entry — honest partial-failure handling.
- Save codes are short (`S####`) and collision-checked; the legacy
  `SV-NNNNNN` format documented in `AGENTS.md` no longer exists.

### Confirmed Facts

1. Deep Save only; no forward fallback anywhere in Save. CONFIRMED.
2. DB write occurs after Telegram upload. CONFIRMED.
3. Temp directory cleanup in `finally`; `CancelledError` re-raised. CONFIRMED.
4. Forwarding remains only in the retrieval (`.send`) path and the generic
   `telegram_api` facade. CONFIRMED.
5. Save code format changed to short codes; `AGENTS.md` is stale. CONFIRMED.

### Likely Causes / Risks

- **LIKELY — AI/Glass entry-point timeouts abort legitimate Deep Saves.** The
  service is correctly unbounded, but the Glass UI input listener (60 s) and the
  AI `ToolExecutor` (10 s) bound the *entire* operation, so a large download or
  upload can be cancelled mid-transfer.

### Ruled Out

- **RULED OUT — Deep Save secretly forwards.** `execute_save` has no
  `forward_messages` and no exception path to forwarding.
- **RULED OUT — `BytesIO` leak.** The implementation uses file-based temp
  storage, not `BytesIO`.
- **RULED OUT — metadata persisted before upload.** Insert happens last.

### Unknowns

- Live behavior for timed/self-destructing media (depends on Telegram's
  download permission; code attempts `download_media` and reports honest
  failure if Telegram blocks it).
- Whether `reply_msg.get_sender()` (unbounded in `_resolve_sender`) can hang on
  a protected sender — no live evidence.

---

## 4. AI SYSTEM

### Architecture

```
outgoing message (trigger word / reply-to-AI / .ai)
  → ai_unified._execute_ai  OR  ai_cmd
  → AIRequest
  → Engine.execute → Dispatcher.dispatch
      → ConversationManager (session + history)
      → PromptBuilder
      → ProviderManager.chat (guarded_await 30 s)
      → Provider (httpx.AsyncClient)
      → Tool loop (MAX_TOOL_ROUNDS=3, MAX_TOOLS_PER_TURN=5)
      → ConversationUpdate
      → EngineResult
  → deliver_response (edit-in-place / chunked)
  → config_store.record_request (last_request_at / last_latency_ms)
```

### Execution Flow

- `ai_unified.py` registers `events.NewMessage(outgoing=True)` (no pattern) and
  matches the first word against `trigger_en`/`trigger_fa`, or detects
  reply-to-AI. `ai_cmd.py` registers `.ai <text>`. Both wrap
  `engine.execute()` in `asyncio.wait_for(..., timeout=60)`.
- `Dispatcher.dispatch` catches every stage exception and returns
  `EngineResult(success=False)`; it never raises.
- `ProviderManager.chat` tries the healthy active provider, then the configured
  fallback chain, then an emergency dummy fallback that **never reports fake
  success** (`success=False` with preserved errors).

### Provider / Fallback

- Providers: `gemini.py` and `openai_compat.py` (covers OpenAI/OpenRouter/Groq/
  Cerebras/Mistral style APIs) plus `dummy/provider.py`. All `chat` methods are
  `async` and use `httpx.AsyncClient`.
- `ProviderManager` has `_ensure_dummy_fallback`, `_load_env_fallback_chain`
  (`AI_PROVIDER_FALLBACK`), and `_try_fallback_chain`.

### Tool System

- `ToolRegistry` + `create_default_registry` register 22 tools: `save`, `delete`,
  `delete_by_id`, 6 bio tools, 6 username tools, `search`, `list_saves`,
  `settings_get`, `settings_set`, `organize_list`, `organize_clean`.
- `ToolExecutor.execute_calls` is the sole caller of `tool.execute()`.
- `SaveTool` delegates to `save_service.execute_save` (no duplicated logic).
- Permission levels: `READ_ONLY`/`READ_WRITE` auto-execute; `DANGEROUS`/
  `ADMIN_ONLY`/`CONFIRMATION_REQUIRED` would require confirmation (none of the
  22 default tools use those levels — `SaveTool.safe=True`,
  `DeleteTool` also safe per registry).

### Memory / History

- Conversation history lives in `ConversationManager`/`RuntimeSession` (in-memory)
  with Supabase persistence via `backend/ai/persistence.py` (session/message/
  memory/tool-history tables), all through `asyncio.to_thread` + 10 s timeout.
- `ReplyResolver` maps Telegram msg-id → AI response (bounded LRU, 500 entries).

### Database / Persistence

- `config_store.get_config`/`save_config`/`record_request` upsert the `ai_config`
  row. `_is_missing_config_response` explicitly handles PostgREST `204` +
  "Missing response" (`maybe_single` on an empty table) and returns `None`
  (defaults). CONFIRMED present.
- `record_request` sets `last_request_at`/`last_latency_ms`.
- Tool history: `ToolExecutor._record_history` is a no-op in production
  (`history_repo=None`); the real persistence is the fire-and-forget
  `guarded_create_task(persistence.record_tool_call(...))`.

### Timeout / Cancellation

- `ProviderManager.chat` → `guarded_await(..., timeout=30)`.
- `ToolExecutor._execute_single` → `asyncio.wait_for(tool.execute(...), timeout=10)`
  for **every** tool, including `save`.
- Handler level → `asyncio.wait_for(engine.execute(...), timeout=60)`.

### Runtime Relationship

- AI runs on the same event loop and the same self client. A reconnect
  (`_trigger_reconnect` → `client.disconnect()`) drops any in-flight AI provider
  call's Telegram reply/edit and any in-flight Save tool's download/upload.

### Confirmed Facts

1. **`ToolExecutor.TOOL_TIMEOUT_SECONDS = 10` applies to the Save tool.**
   `backend/ai/tools/executor.py` wraps every `tool.execute()` in
   `asyncio.wait_for(..., timeout=10)`. A Deep Save invoked by the AI that takes
   > 10 s (any non-trivial download/upload) is cancelled and reported as
   "timed out". CONFIRMED — a blanket timeout on a large Save operation.
2. **Three AI handler modules, only two registered.**
   `backend/bot/router.py` registers `ai`, `ai_cmd`, `ai_unified` — **not**
   `ai_trigger`. `backend/bot/handlers/ai_trigger.py` (a full trigger handler)
   is dead code, yet `ai_cmd.py`'s docstring claims "The primary AI activation
   method is now the trigger-based system in `backend.bot.handlers.ai_trigger`".
   CONFIRMED inconsistency.
3. **`ai_cmd` and `ai_unified` are two separate AI entry points** (`.ai`
   command vs trigger/reply), each with its own duplicated ~200-line
   `_restore_config`/`_format_*`/`_humanize_error`/timeout logic. CONFIRMED.
4. **AI DB access is non-blocking** (`to_thread` + 10 s). CONFIRMED.
5. **`record_request` / `get_config` handle PostgREST 204 "Missing response".**
   CONFIRMED.
6. **Tool history repository is never wired** (`history_repo=None`), so the
   in-executor `_record_history` is inert; DB tool history is written via
   fire-and-forget `persistence.record_tool_call`. CONFIRMED.

### Likely Causes / Risks

- **LIKELY — AI Save is effectively unusable for media.** 10 s tool timeout +
  60 s total handler timeout make any meaningful Deep Save fail or abort.
- **LIKELY — duplicated AI handler code drifts.** `ai_cmd`/`ai_unified` (and the
  dead `ai_trigger`) each re-implement config restore, error formatting, and
  timeout handling.

### Ruled Out

- **RULED OUT — AI blocks the event loop with sync HTTP.** `httpx.AsyncClient`.
- **RULED OUT — Dummy provider reports fake success.** `success=False` always.
- **RULED OUT — AI DB write blocks the loop.** `to_thread` + timeout.

### Unknowns

- Whether `ProviderManager.vision()`/`stream()` are ever called (they are sync
  and inconsistent with the async providers — `openai_compat.vision` is
  `async def` — but no dispatcher path calls them).
- Live provider latency/behavior (no credentials exercised).

---

## 5. GLASS UI / HANDLER ARCHITECTURE

### Menu / Navigation

- Root panel `menu` registered in `backend/bot/handlers/misc.py`; `.menu`
  triggers inline mode (`send_inline_panel`).
- Navigation: `panel:_nav:back|home|close`; root has Close only, submenus have
  Back/Home/Close (`_finalize_panel`).

### Panels

Registered panels (from `register_panel`):

- `menu`, `profile`, `context`, `health`, `settings`, `general` (misc.py)
- `save` (save.py), `retrieve`, `retrieve_saved`, `retrieve_item`,
  `retrieve_code` (retrieve.py), `del`, `delfrom` (delete.py)
- `bio`, `biohelp` (bio.py), `username`, `usernamehelp` (username.py)
- `list`, `find` (discover.py), `db` (database.py)
- `ai`, `ai_provider`, `ai_model`, `ai_wizard`, `ai_settings`, `ai_status`,
  `ai_diagnostics` (ai.py)

### Actions / Callbacks

- Callback routing in `backend/helper/panels.py::_callback_router`:
  `panel:` → `_handle_panel`, `action:` → `_handle_action`, `input:` →
  `_handle_input`. All wrapped in try/except; `event.answer()` in `finally`.
- Owner gating: `settings_service.is_owner_only()` + `is_owner`.
- Session resolution via `_resolve_session` (chat/msg → inline_message_id).

### Inputs / Reply Modes

- `backend/helper/input_state.py` — single pending input per owner, 120 s
  expiry, replaced on new request.
- `backend/helper/inline_sender.py::_input_listener` — matches owner's next
  outgoing message in the same chat, skips `.`-prefixed messages, and runs the
  handler under `asyncio.wait_for(..., timeout=60)`.
- `target_context.py` — reply-target resolution used by `delete.py`
  (`delfrom` reply) and `misc.py`.

### Dot Commands

Current registered dot commands (CONFIRMED by grep across `backend/bot`):

| Command | Handler | Registered? |
|---|---|---|
| `.menu` | `misc.py` `r"^\.menu$"` | yes |
| `.ai <text>` | `ai_cmd.py` `r"^\.ai(?:\s+(.+))?$"` | yes |
| trigger word | `ai_unified.py` (all outgoing) | yes |
| reply-to-AI | `ai_unified.py` (all outgoing) | yes |
| trigger word | `ai_trigger.py` (all outgoing) | **no — dead code** |

No `.ping`, `.id`, `.help`, `.kill`, `.health`, `.logs`, `.save`, `.del`,
`.organize`, `.bio`, `.username`, `.retrieve`, `.preview`, `.send`, `.list`,
`.find` dot commands exist. These features are exposed only through Glass UI
panels/actions/inputs and AI tools.

### Handler Registration

`backend/bot/router.py::register_all` registers, in order:
`misc, save, retrieve, delete, organize, bio, discover, database, username,
ai, ai_cmd, ai_unified` — each in its own try/except. `register_runtime_hooks`
registers health-timestamp hooks first.

Note: `organize` is imported in the router but `backend/bot/handlers/organize.py`
registers no dot command (its functionality moved to the `db`/`discover`
panels + AI `organize_*` tools).

### Service / Engine Flow

- Save → `save_service.execute_save`.
- Bio/Username → `bio_service`/`username_service` → shared `ProfileEngine` →
  shared `profile.scheduler`.
- Delete/Retrieve/Discover/Organize/Database → their `services/*` modules.
- AI → `Engine` → tools → services.

### Stability Risks

- **CONFIRMED — 60 s input-listener timeout bounds Deep Save.** Any pending-input
  handler (including `_save_reply_wait_handler` → `execute_save`) is cancelled
  after 60 s by `inline_sender._input_listener`.
- **CONFIRMED — two SessionManager singletons exist.** `session_manager.py`
  defines both the `SessionManager` class and a module-level `_manager` with
  "backward-compatible" functions (`create_session`, `get_session`, `push_nav`,
  `pop_nav`, ...). `grep` shows none of the module-level functions are used —
  all panel code uses `get_lifecycle().sessions` (the lifecycle's own instance).
  The module-level `_manager` is a dormant second session store.
- **LIKELY — stale documentation invites misuse.** `AGENTS.md` documents many
  removed dot commands and the removed Forward Save.
- **SUSPECTED — callback handlers can hang the helper.** `_handle_panel`/
  `_handle_action`/`_handle_input` run without a per-callback timeout; a slow
  handler (e.g. AI `fetch_models`, discovery) can delay subsequent callbacks.

### Confirmed Facts

1. Only `.menu` and `.ai` are registered dot commands; `ai_trigger.py` is dead.
   CONFIRMED.
2. `AGENTS.md` documents `.ping/.id/.help/.save f/.save d/.preview/.send/.del/
   .organize/.bio/.username` — none exist. CONFIRMED stale.
3. `retrieve.py` line 99 says "Save something first with `.save`" — stale. CONFIRMED.
4. Glass UI Save flow is deterministic reply → `reply_to_msg_id` → target →
   `execute_save`. CONFIRMED.
5. `target_context` is used by delete and misc (not dead). CONFIRMED.
6. Module-level `session_manager` API is unused (second singleton). CONFIRMED.

### Likely Causes / Risks

- **LIKELY — `AGENTS.md` is no longer the source of truth** and will mislead
  future agents/operators about commands and save semantics.
- **LIKELY — no per-callback timeout** can make the helper appear unresponsive
  if a panel/action/input handler performs a slow RPC (model discovery, large
  DB read) without its own bound.

### Ruled Out

- **RULED OUT — `target_context` is orphaned.** It is used (delete + misc).
- **RULED OUT — Forward Save still reachable via a hidden dot command.** No
  `.save` command is registered; the Save panel offers only Deep Save + link save.

### Unknowns

- Whether any callback data exceeds Telegram's 64-byte limit (the system
  truncates via `truncate_callback_data`), and whether truncation ever causes
  collisions in practice.
- Live helper-bot inline latency.

---

## 6. CROSS-SUBSYSTEM INTERACTIONS

| Interaction | Evidence / status |
|---|---|
| Runtime ↔ AI | Same event loop + same self client. `_trigger_reconnect`/`disconnect` can drop in-flight AI provider Telegram reply + in-flight Save tool. CONFIRMED coupling. |
| Runtime ↔ Save | Same client. Reconnect drops download/upload. Entry-point timeouts (60 s input / 10 s tool) abort saves. CONFIRMED. |
| Runtime ↔ DB | `to_thread` + 10 s — no loop blocking. CONFIRMED safe. |
| Runtime ↔ Telegram | `_run_loop` vs `_trigger_reconnect` dual ownership. CONFIRMED race. |
| Glass UI ↔ Save | `.menu` → save panel → reply mode → `execute_save` under 60 s input-listener timeout. CONFIRMED. |
| Glass UI ↔ AI | AI panels configure provider/model/triggers; AI activation is separate (outgoing-message handler). |
| Glass UI ↔ Profile | Bio/Username panels call `bio_service`/`username_service` → shared scheduler. CONFIRMED. |
| AI ↔ Save | `SaveTool` → `execute_save` with 10 s tool timeout. CONFIRMED. |
| AI ↔ DB | `config_store`/`persistence` via `to_thread` + 10 s. CONFIRMED. |
| Save ↔ DB | `insert_save` after upload, `to_thread` + 10 s. CONFIRMED. |
| Save ↔ Telegram | `download_media`/`send_file` unbounded at service. CONFIRMED. |
| Profile ↔ Runtime | One shared `lifeos-profile-scheduler` task; per-engine active state prevents one engine's stop from killing the other. CONFIRMED (fix present). |

**Shared resources:** single asyncio event loop, one self client, one helper
client, one recovery lock, one panel lifecycle lock, one pending-input slot per
owner, one shared profile scheduler.

**Duplicate supervisors:** `RuntimeSupervisor` (active) vs dormant
`diagnostics.recover_stalled` and dormant `supervisor._watchdog_loop`.
**Duplicate AI activation:** `ai_cmd` + `ai_unified` (active) + `ai_trigger`
(dormant).

---

## 7. ARCHITECTURAL COMPLIANCE

| Requirement | Verdict | Evidence |
|---|---|---|
| Single-client Self architecture | ✅ | one self client + optional helper bot |
| Deterministic behavior | ✅ mostly | Save target resolution is exact (`reply_to_msg_id`); scheduler fires at minute boundary |
| Layer separation (parsing / permission / execution / persistence) | ✅ | handlers → services → db; tools → services |
| Single handler per feature | ⚠️ partial | AI has 3 handler modules (2 active + 1 dead); bio/username services are near-verbatim mirrors |
| Zero-spam philosophy | ✅ | edit-in-place for AI; panels edit inline; reply handlers delete the trigger |
| Resource discipline | ⚠️ | Deep Save temp dir cleaned in `finally`; but input/tool timeouts cancel long work |
| Scalability | ⚠️ | single pending-input slot; single process; acceptable for single owner |
| Lifecycle ownership | ⚠️ | supervisor is authoritative, but `_watchdog_loop` is dormant and failsafe hard-reset is broken |
| ENV authority | ✅ | `config.load()` is the single loader; `HELPER_BOT_ENABLED` derived from `BOT_TOKEN` |
| Persistence boundaries | ✅ | DB access isolated in `db/client.py`, `ai/config_store.py`, `ai/persistence.py` |

---

## 8. CONFIRMED BUGS / DEFECTS

Source-proven defects:

1. **Failsafe hard-reset is a `NameError`.** `backend/runtime/failsafe.py` uses
   `guarded_create_task` (line 156) but never imports it (only
   `immortal_create_task`, line 28). The last-resort recovery can never fire.
2. **`supervisor._watchdog_loop` is never started.** The supervisor's own
   watchdog (RPC check, helper restart, memory GC, stale-loop restart) is dead.
3. **Heartbeat `READY_BUT_DISCONNECTED` false positive.** Fires
   `_trigger_reconnect` whenever the helper bot is disabled
   (`helper_connected=False` while `helper_enabled=False`).
4. **Heartbeat idle = stalled false positives.** `UPDATE_PIPELINE_STALLED` and
   `CALLBACK_DISPATCH_STALLED` treat a naturally idle account as stalled and
   reconnect.
5. **No reconnect cooldown on lightweight success.** `_trigger_reconnect`
   can be re-triggered every 30–60 s.
6. **Dual connection ownership** between `_run_loop` and `_trigger_reconnect`
   on the same client.
7. **`ToolExecutor` 10 s timeout applies to the Save tool.** A blanket timeout
   on large Deep Save via AI.
8. **Glass UI input listener 60 s timeout bounds Deep Save.** A blanket timeout
   on large Deep Save via Reply Mode.
9. **`ai_trigger.py` dead code** + `ai_cmd.py` docstring pointing to it as
   "primary", while the actual trigger handler is `ai_unified.py`.
10. **`AGENTS.md` documents removed commands/save modes** (`.ping/.id/.help/
    .save f/.save d/.preview/.send/.del/.organize/.bio/.username`,
    `SV-NNNNNN`), none of which exist.
11. **`run_startup_checks` dead code** (never called by `main.py` or supervisor).
12. **`diagnostics.recover_stalled` dead code** (dormant second recovery path).
13. **`tg_retry.tg_rpc` dead code** (zero callers).
14. **`operation_watchdog.bounded_operation` / `attach_task` dead code and
    non-functional as a timeout** (no internal timer; only `guarded_await` is
    used).
15. **Second SessionManager singleton** in `session_manager.py` module-level
    API is unused (dormant divergent session store).
16. **Bio/Username service duplication** remains (only the engine/scheduler
    layer was consolidated; `bio_service` and `username_service` are
    near-verbatim mirrors).

---

## 9. LIKELY RISKS

Strongly supported by source but not proven as an active failure in production:

1. **Reconnect churn is the intermittent freeze/sleep driver** (defects 3–6
   interact). Not live-proven without Render logs.
2. **AI Save cannot complete for media** (defect 7 + 60 s handler timeout).
3. **AI handler drift** between `ai_cmd`/`ai_unified` duplicated logic.
4. **Per-callback hang** in the helper bot (no per-callback timeout) when a
   panel/action performs a slow RPC.

---

## 10. RULED OUT

Explicitly investigated and not supported by source:

- Supabase / synchronous HTTP blocking the event loop (`to_thread` + timeout).
- Sync AI HTTP (`httpx.AsyncClient`).
- Silent process death with no diagnostics (crash diagnostics + exit reasons).
- `recover_stalled` acting as an active second supervisor (no callers).
- Deep Save falling back to forwarding (no forward in the Save path).
- `BytesIO` leak in Deep Save (file-based temp storage).
- Metadata persisted before upload (insert is last).
- Blanket Deep Save timeout at the service layer (the service is unbounded;
  timeouts are at the entry points).
- `target_context` orphaned (used by delete + misc).
- `.bio`/`.username`/`.save` dot commands existing (they don't).

---

## 11. UNKNOWN / MISSING EVIDENCE

- Actual production env (`BOT_TOKEN` set or not) — determines whether the
  `READY_BUT_DISCONNECTED` false positive fires every 30 s in production.
- Live Render logs at freeze time — needed to prove which reconnect trigger
  fired first and to distinguish "process dead" from "loop blocked" from
  "reconnect churn".
- Whether any Telethon RPC outside `wait_for`/`guarded_await` can block
  indefinitely (e.g. `reply_msg.get_sender()` in `_resolve_sender`, `get_chat`
  in AI reply extraction) — no live thread dump available.
- Live Telegram behavior for protected/self-destructing media.
- Whether `ProviderManager.vision`/`stream` are reachable (they appear dormant
  but are inconsistent with async providers).

---

## 12. EXACT FILES

Runtime / lifecycle:
- `backend/main.py`
- `backend/config.py`
- `backend/runtime/supervisor.py`
- `backend/runtime/heartbeat.py`
- `backend/runtime/keepalive.py`
- `backend/runtime/failsafe.py`
- `backend/runtime/task_guard.py`
- `backend/runtime/operation_watchdog.py`
- `backend/runtime/tg_retry.py`
- `backend/runtime/managed_task.py`
- `backend/runtime/diagnostics.py`
- `backend/runtime/crash_diagnostics.py`
- `backend/runtime/memory_cleanup.py`
- `backend/runtime/health_check.py`
- `backend/runtime/startup_check.py`
- `backend/runtime/states.py`
- `backend/runtime/tracer.py`
- `backend/health.py`
- `backend/diagnostics.py`
- `backend/bot/client.py`
- `backend/helper/client.py`
- `backend/helper/watchdog.py`
- `backend/helper/rpc_timeout.py`
- `backend/helper/lifecycle.py`

Save:
- `backend/services/save_service.py`
- `backend/bot/handlers/save.py`
- `backend/ai/tools/save.py`
- `backend/telegram_api/api.py`
- `backend/telegram_api/messages.py`
- `backend/telegram_api/media.py`
- `backend/telegram_api/entities.py`
- `backend/telegram_api/_helpers.py`
- `backend/db/client.py`

AI:
- `backend/ai/engine/engine.py`
- `backend/ai/engine/dispatcher.py`
- `backend/ai/providers/manager/manager.py`
- `backend/ai/providers/gemini.py`
- `backend/ai/providers/openai_compat.py`
- `backend/ai/providers/dummy/provider.py`
- `backend/ai/tools/registry.py`
- `backend/ai/tools/executor.py`
- `backend/ai/tools/context.py`
- `backend/ai/tools/base.py`
- `backend/ai/config_store.py`
- `backend/ai/persistence.py`
- `backend/ai/diagnostics.py`
- `backend/ai/context/reply_resolver.py`
- `backend/ai/session/request.py`
- `backend/bot/handlers/ai.py`
- `backend/bot/handlers/ai_cmd.py`
- `backend/bot/handlers/ai_trigger.py`
- `backend/bot/handlers/ai_unified.py`

Glass UI / handlers:
- `backend/bot/router.py`
- `backend/bot/handlers/misc.py`
- `backend/bot/handlers/bio.py`
- `backend/bot/handlers/username.py`
- `backend/bot/handlers/delete.py`
- `backend/bot/handlers/retrieve.py`
- `backend/bot/handlers/discover.py`
- `backend/bot/handlers/database.py`
- `backend/bot/handlers/organize.py`
- `backend/helper/panels.py`
- `backend/helper/inline_engine.py`
- `backend/helper/inline_sender.py`
- `backend/helper/input_state.py`
- `backend/helper/target_context.py`
- `backend/helper/session_manager.py`
- `backend/helper/panel_registry.py`
- `backend/helper/panel_render.py`
- `backend/helper/pagination.py`

Profile:
- `backend/profile/engine.py`
- `backend/profile/scheduler.py`
- `backend/bio/engine.py`
- `backend/username/engine.py`
- `backend/services/bio_service.py`
- `backend/services/username_service.py`

Web:
- `backend/web/app.py`

Docs (stale vs source):
- `AGENTS.md` (documents removed commands/save modes)

---

## 13. EXACT FUNCTIONS / CLASSES

Runtime:
- `RuntimeSupervisor` — `start`, `_build_and_register`, `_run_loop`,
  `_trigger_reconnect`, `_trigger_full_recovery`, `_do_recovery`,
  `_retry_full_recovery`, `_hard_reset_runtime`, `_verify_heartbeat`,
  `_cancel_orphan_tasks`, `_watchdog_loop` (dormant), `stop`.
- `heartbeat._heartbeat_loop` (false-positive reconnect triggers).
- `keepalive._keepalive_loop`.
- `failsafe._failsafe_loop` / `_all_frozen` (NameError bug).
- `task_guard.guarded_create_task` / `immortal_create_task`.
- `operation_watchdog.guarded_await` / `bounded_operation` / `attach_task`.
- `tg_retry.tg_rpc` (dead).
- `startup_check.run_startup_checks` (dead).
- `diagnostics.recover_stalled` (dead).

Save:
- `save_service.execute_save`, `execute_link_save`, `build_caption`,
  `_append_original_text`, `_extract_source_media`, `_extract_uploaded_metadata`,
  `_upload_kwargs_for_media`, `parse_telegram_link`, `build_tags`,
  `detect_media_type`, `extract_file_name`, `generate_filename`.
- `save._save_reply_wait_handler`, `_save_link_input_handler`.
- `ai.tools.save.SaveTool.execute`.

AI:
- `Engine`, `Dispatcher.dispatch`, `ProviderManager.chat/_try_fallback_chain/
  _fallback`, `ToolExecutor.execute_calls/_execute_single`,
  `create_default_registry`, `ToolContext`, `SaveTool.execute`.
- `config_store.get_config/save_config/record_request/_is_missing_config_response`.
- `ai_unified._execute_ai/_extract_reply_context/register`,
  `ai_cmd.register`, `ai_trigger.register` (dead).

Glass UI:
- `panels._callback_router/_handle_panel/_handle_action/_handle_input/
  _finalize_panel/register_panel/register_action/register_input`.
- `inline_sender.send_inline_panel/register_input_listener/_input_listener`.
- `lifecycle.PanelLifecycleManager.create_panel/try_reuse_panel/_cleanup_locked/
  shutdown_all`.
- `session_manager.SessionManager` + unused module-level `_manager`.
- `input_state.set_pending/get_pending/clear_pending`.
- `inline_engine.trigger/register_inline_handler`.

Profile:
- `ProfileEngine.render/updater/start_cron/stop_cron/is_running`.
- `profile.scheduler._cron_loop/_collect_updates/register_updater/
  set_engine_active/any_engine_active/stop_if_idle/stop_cron`.
- `bio_service.*`, `username_service.*` (mirrors).

---

## 14. COMPLETE EXECUTION PATHS

### Runtime startup

```
main.main()
  → cfg_module.load()
  → install_crash_diagnostics()
  → RuntimeSupervisor(cfg).start()
      → settings_service.load_all()
      → _build_and_register()          # build_client → register_all → _wire_ai_tools
      → _start_helper()                # if BOT_TOKEN
      → _resume_bio_cron() / _resume_username_cron()
      → _start_web_server()            # uvicorn Server.serve()
      → start_heartbeat(); start_keepalive(); start_failsafe(); start_diagnostics()
      → _run_task = immortal_create_task(_run_loop)
      → start_memory_cleanup()
  → shutdown_event.wait() → stop()
```

### Reconnect false positive (the suspected freeze loop)

```
heartbeat._heartbeat_loop (30s)
  → READY && (not self_connected or not helper_connected)   # helper disabled → true
     → guarded_create_task(supervisor._trigger_reconnect)
  → or UPDATE_PIPELINE_STALLED / CALLBACK_DISPATCH_STALLED  # idle account → true
     → guarded_create_task(supervisor._trigger_reconnect)

_trigger_reconnect()
  → client.disconnect()               # makes run_until_disconnected() return
  → client.connect()
  → is_user_authorized()
  → success → NO cooldown → next heartbeat can repeat it
```

### Deep Save (Glass UI)

```
.menu → inline menu panel → Save → Deep Save → Reply Mode
  → set_pending(owner, "save_reply", _save_reply_wait_handler, chat, ...)
  → owner replies
  → inline_sender._input_listener (60s wait_for)
      → _save_reply_wait_handler
          → reply_msg = get_messages(chat, reply_id)
          → target = get_messages(chat, reply_msg.reply_to_msg_id)
          → save_service.execute_save(client, owner, target, tz)
              → get_next_save_code()
              → text? send_message("me", caption)
              → media? mkdtemp → download_media → validate → send_file
              → finally rmtree
              → insert_save(payload)   # AFTER upload
```

### AI trigger

```
outgoing message (first word == trigger)
  → ai_unified._execute_ai
      → _restore_config (get_config → apply_runtime_selection)
      → engine.execute(request) [wait_for 60s]
          → Dispatcher.dispatch
              → ConversationManager session/history
              → PromptBuilder
              → ProviderManager.chat [guarded_await 30s]
              → tool loop (≤3 rounds, ≤5 tools, each tool wait_for 10s)
          → EngineResult
      → record_request(last_request_at/last_latency_ms)
      → deliver_response (edit-in-place / chunked)
      → ReplyResolver.register
```

### Profile (Bio + Username shared scheduler)

```
bio_service.do_on / username_service.do_on
  → db update is_active=True
  → ProfileEngine.start_cron
      → profile.scheduler.set_engine_active(name, True)
      → profile.scheduler.start_cron  (single "lifeos-profile-scheduler" task)
  → each minute _cron_loop
      → _collect_updates (bio.updater + username.updater)
      → client(UpdateProfileRequest(**merged))  [wait_for 30s]
      → _record_update_telemetry (about→bio, first_name→username)

bio_service.do_off / username_service.do_off
  → db update is_active=False
  → ProfileEngine.stop_cron
      → set_engine_active(name, False)
      → stop_if_idle()  # cancels scheduler only when NEITHER engine active
```

---

## 15. RECOMMENDED FIX SURFACE

Recommendations only — not implemented in this investigation.

**High priority (runtime stability):**
1. Fix `failsafe.py` — import `guarded_create_task` (or use
   `immortal_create_task`) so the hard reset can actually run.
2. Gate `READY_BUT_DISCONNECTED` on `supervisor.helper_enabled` — do not require
   `helper_connected=True` when the helper bot is disabled.
3. Stop treating idle as stalled — only emit `UPDATE_PIPELINE_STALLED` /
   `CALLBACK_DISPATCH_STALLED` when there is actual evidence the pipeline is
   stuck (e.g. update queue growing while RPC is healthy), not merely "no
   updates for 90 s".
4. Add a post-success cooldown (or a minimum interval) to
   `_trigger_reconnect` so a lightweight reconnect cannot be re-triggered every
   30–60 s.
5. Unify connection ownership — make either `_run_loop` or the reconnect path
   the single driver of `client.connect()/disconnect()`, and serialize them
   (the recovery lock already exists; ensure the run loop respects it).
6. Either wire or delete `supervisor._watchdog_loop`, `startup_check`, and
   `diagnostics.recover_stalled` to remove dormant recovery/validation paths.

**High priority (Save/AI timeouts):**
7. Remove the blanket 10 s `ToolExecutor` timeout for the Save tool (use a
   media budget / progress-based guard, or no timeout for `save`). Never a
   blanket timeout on large transfers.
8. Remove or raise the 60 s `_input_listener` timeout for the Save reply mode
   (or make it configurable per input), so large downloads/uploads are not
   cancelled.
9. Consider a bounded-but-generous media transfer budget only if live logs prove
   a stuck transfer; never a blanket save timeout.

**Medium priority (consolidation / drift):**
10. Consolidate the duplicated AI handler logic (`ai_cmd` vs `ai_unified`) and
    remove/register `ai_trigger.py` explicitly.
11. Update `AGENTS.md` (and `retrieve.py` docstring) to reflect the Glass-UI-only
    command model and Deep-Save-only Save model + short save codes.
12. Consider consolidating the near-verbatim `bio_service`/`username_service`
    duplication (mirrors the already-consolidated `ProfileEngine`).

**Low priority:**
13. Remove/repurpose the dormant second `SessionManager` module-level API and
    the non-functional `bounded_operation` context manager / `tg_rpc` dead code.

---

## 16. REMAINING WORK

**High priority**
- Failsafe hard-reset NameError (blocks last-resort recovery).
- Heartbeat false-positive reconnect triggers (helper-disabled + idle = stalled).
- Reconnect cooldown + connection-ownership race.
- AI/Glass entry-point blanket timeouts on Deep Save.

**Medium priority**
- AI handler consolidation / `ai_trigger` dead-code cleanup.
- `AGENTS.md` documentation refresh.
- Bio/Username service consolidation.

**Low priority**
- Dead-code cleanup: `supervisor._watchdog_loop`, `startup_check`,
  `recover_stalled`, `tg_rpc`, `bounded_operation`, module-level SessionManager.

**Validation-only**
- Confirm no regression in the 19 Bio/Username tests after any scheduler change.
- Confirm Save/AI timeout changes do not regress the save-engine and tool tests.

**Unknown / blocking**
- Production env values and live Render logs needed to prove the freeze
  mechanism end-to-end.

---

## 17. VALIDATION PLAN

After any future fix, perform (in order):

1. **Compile check** — `python -m py_compile` on every modified file.
2. **Unit/regression tests** —
   - `tests/test_15_bio_username.py` (shared scheduler both directions:
     Bio OFF while Username active; Username OFF while Bio active).
   - `tests/test_12_save_engine.py` (Deep Save download → upload, no forward).
   - `tests/test_10_tool_calls.py`, `tests/test_14_tool_honesty_glass.py`
     (tool timeouts / honesty).
   - `tests/test_11_runtime_wiring.py`, `tests/test_07_diagnostics.py`,
     `tests/test_08_observability.py` (runtime wiring / observability).
3. **Full suite** — `python -m pytest -q`.
4. **Source verification** — grep for:
   - `forward_messages`/`ForwardMessagesRequest` in the Save path (must be absent).
   - `guarded_create_task` in `failsafe.py` (must be imported after fix).
   - heartbeat gating on `helper_enabled`.
   - no `TOOL_TIMEOUT_SECONDS` blanket on `save`.
5. **Remote verification** — commit + push; confirm `origin/main` contains the fix.
6. **Live Telegram verification** — only when a real session/credentials exist:
   - Helper-disabled idle soak (>5 min) with no reconnect logs.
   - Bio OFF while Username active (and inverse) with per-minute first_name
     updates continuing.
   - A >60 s large Deep Save through Reply Mode completing without cancellation.

---

## 18. VERIFICATION LEVELS

| Level | Meaning | Used here |
|---|---|---|
| SOURCE VERIFIED | Read actual code; direct evidence cited | ✅ this entire report |
| TEST VERIFIED | Test suite actually executed | ❌ not run for this investigation |
| REMOTE VERIFIED | `origin/main` confirmed to contain the change | ❌ (no code change made) |
| LIVE TELEGRAM VERIFIED | Real session/credentials exercised | ❌ not available |

Do not claim a higher verification level than actually performed.
