# INVESTIGATION REPORT — LifeOS

> **CANONICAL INVESTIGATION HANDOFF FILE**
>
> This file is the single source of truth for the **most recent** forensic
> investigation performed on this repository. Rules governing this file:
>
> 1. It always contains the COMPLETE result of the LATEST investigation.
> 2. Each new investigation **fully replaces** this content. Never append.
> 3. Obsolete findings from previous investigations are removed when the
>    report is replaced.
> 4. Findings are based on the actual repository source, logs, runtime
>    evidence and project instructions — never invented architecture.
> 5. The report distinguishes: **Confirmed facts** · **Direct source
>    evidence** · **Likely causes** · **Unknowns / missing evidence** ·
>    **Exact files/functions/execution paths** · **Recommended fix surface**.
> 6. If a later investigation proves a previous report wrong, the new
>    report replaces this file with corrected findings.
> 7. This file is the canonical handoff between investigation agents and
>    execution agents.

---

## Investigation Metadata

| Field | Value |
|---|---|
| **Repository** | `Onlyicing1/Telegram-self-bot` |
| **Branch** | `main` |
| **Investigated HEAD** | `84dcf3ba34d6d78ddeb6724b2a7c78f1eab5f544` (`fix: honest AI tool results, compact glass model message, deep save routing`) |
| **Remote HEAD (verified via `git ls-remote`)** | `84dcf3ba34d6d78ddeb6724b2a7c78f1eab5f544` — matches local |
| **Working tree** | Clean except untracked `FREEBUFF_PRE_PUSH_VERIFY.md` (prior report, unrelated) |
| **Type** | Forensic investigation ONLY — no code was modified, no commit, no push |
| **Date** | 2026-08-18 |

**Scope investigated:** (1) AI tool-call execution pipeline and the
"delete the last 10 messages" → "nothing happened" symptom; (2) Save Engine /
Deep Save routing, protected-chat behavior, caption/media/metadata pipeline,
DB persistence ordering; (3) runtime task-starvation / `ai_active` heartbeat
values.

---

## 1. Executive Summary

The symptom "AI says it deleted 10 messages but nothing happened" is fully
explained by the current code. It is **not** a single bug — it is a
**three-layer failure**:

1. **The tool call never becomes a structured tool call.** The runtime
   injects tool schemas as **plain text** into the system prompt and does
   **not** send a native `tools` parameter to the provider. The provider
   parsers only read **native API `tool_calls`** (OpenAI `message.tool_calls`
   / Gemini `functionCall` parts). Any tool call the model emits as JSON
   inside its text content is just text — it is displayed to the user and
   never parsed or executed. The exact JSON observed in the user's report
   (`{"tool": "delete_last_messages", ...}`) was **model text content**, not
   a structured call.
2. **The tool name does not exist.** The registered deletion tool is
   `delete` (plus `delete_by_id`). `delete_last_messages` appears **nowhere**
   in the codebase. Even if it were parsed natively, the executor would
   return `Tool 'delete_last_messages' is not registered`.
3. **Even a correctly-formed call to `delete` cannot execute.** `delete` is
   classified `DANGEROUS`; the executor's permission gate returns a
   `needs_confirmation=True` result **without executing**, and
   `needs_confirmation` has **zero consumers** in the entire codebase — there
   is no confirmation UI, no approval path, nothing. DANGEROUS tools
   (`delete`, `delete_by_id`, `organize_clean`) and `settings_set`
   (ADMIN_ONLY) are **permanently blocked in the current runtime**.

The claim that "Deep Save reached a protected-chat `ForwardMessagesRequest`
error" is **not supported by the current code**: `mode="d"` can never reach
`forward_messages` (the only call site is inside the `mode == "f"` branch).
The protected-chat error can only come from the forward branch — i.e., a
genuine forward-save attempt (panel default, or explicit Forward Save).
There is **no deep→forward fallback** anywhere.

The `TASK_STARVATION / ai_active=10 / ai_stage=TELEGRAM_REPLY` heartbeat
values are **most plausibly a diagnostics-accounting leak, not 10 concurrent
requests**: `register_start()` is called on every AI request but
`register_end()` is called **only in exception branches** — successful
requests never deregister, so `ai_active` grows monotonically and the oldest
stage string freezes. This is **confirmed in code**; whether it contributed
to actual starvation in production is **unknown** (no production logs were
available in this environment).

---

## 2. AI Tool Execution Architecture

Actual execution path (verified in source):

```
Owner message ("Nova delete the last 10 messages")
  │
  ▼
backend/bot/handlers/ai_unified.py  ·  register() → ai_trigger handler
  │   builds AIRequest (chat_id, message_id, owner_id, timezone)
  │   calls engine.execute(request)
  ▼
backend/ai/engine/engine.py  ·  Engine.execute()
  │
  ▼
backend/ai/engine/dispatcher.py  ·  Dispatcher.dispatch()
  │
  ├── Stage 1: Conversation Runtime
  │     backend/ai/runtime/manager.py · ConversationManager
  │
  ├── Stage 2: Prompt Builder
  │     backend/ai/prompt/builder.py · PromptBuilder.build(ctx)
  │     └── TOOL SCHEMAS INJECTED AS **TEXT**:
  │           dispatcher._render_tool_schemas() → "[Available Tools] ..." text
  │           dispatcher._inject_tool_schemas() → merged into tool_context
  │
  ├── Stage 3: Provider Manager
  │     backend/ai/providers/manager/manager.py · get_active_name()
  │
  ├── Stage 4: Provider call
  │     response = await self._provider_manager.chat(messages)
  │           ▲ NO native `tools` parameter is sent to the provider.
  │     provider parses response:
  │       openai_compat.py  → message.get("tool_calls")  (native field only)
  │       gemini.py         → functionCall parts          (native field only)
  │
  ├── Tool loop (only if response.tool_calls is NON-EMPTY):
  │     backend/ai/tools/executor.py · ToolExecutor.execute_calls()
  │       ├── _is_auto_executable(tool)  → READ_ONLY / READ_WRITE only
  │       ├── DANGEROUS/ADMIN_ONLY → needs_confirmation=True, NOT executed
  │       └── tool.execute(ctx, args) → real service call (when allowed)
  │
  └── Stage 5/6: conversation update + EngineResult
        backend/bot/handlers/ai_cmd.py / ai_unified.py → deliver_response()
        (edit-in-place / chunked Telegram reply)
```

**Critical facts (confirmed):**

- **FILE:** `backend/ai/engine/dispatcher.py` — `_render_tool_schemas()` /
  `_inject_tool_schemas()` / `dispatch()`
  **WHAT IT DOES:** Builds a plain-text `[Available Tools]` block from
  `registry.list_schemas()` and merges it into the prompt's `tool_context`
  system message. The provider is then called with only
  `self._provider_manager.chat(messages)` — **no** native `tools` array.
  **EVIDENCE:** dispatcher.py Stage 2 (`_inject_tool_schemas` → `replace(
  package, tool_context=merged)`) and Stage 4 (`chat(messages)`).
- **FILE:** `backend/ai/providers/openai_compat.py` — response parser
  **WHAT IT DOES:** Collects `tool_calls` only from
  `message.get("tool_calls", [])` — the native OpenAI chat-completions field.
  **EVIDENCE:** `raw_tool_calls = message.get("tool_calls", []) if choices
  else []` (line ~136).
- **FILE:** `backend/ai/providers/gemini.py` — response parser
  **WHAT IT DOES:** Collects `functionCall` parts from native Gemini
  `functionCall` part objects.
  **EVIDENCE:** `for fc in ...part.functionCall...` → `tool_calls.append(...)`
  (lines ~141-150).
- **Consequence:** A model that "calls a tool" by writing JSON into its text
  reply produces `tool_calls == []` → the tool loop never runs → the JSON is
  rendered to the user as part of the response. Nothing executes.

---

## 3. `delete_last_messages` Trace (hypothetical `count=10`)

The name `delete_last_messages` **does not exist anywhere** in the
repository (verified: `grep -rn "delete_last_messages" backend --include="*.py"`
→ zero matches).

Registered tools (from `backend/ai/tools/registry.py` →
`create_default_registry()`):

- `save`, `delete`, `delete_by_id`
- `bio_template`, `bio_text`, `bio_mood`, `bio_on`, `bio_off`, `bio_show`
- `username_template`, `username_text`, `username_mood`, `username_on`,
  `username_off`, `username_show`
- `search`, `list_saves`, `settings_get`, `settings_set`
- `organize_list`, `organize_clean`

**Hypothetical execution of a structured call to `delete` with
`{"count": 10}`:**

1. If the provider returned a native tool_call for `delete`, the executor
   looks it up → found.
2. `DeleteTool.permission_level` is `DANGEROUS`
   (FILE: `backend/ai/tools/delete.py` — `permission_level` property).
3. `ToolExecutor._is_auto_executable()` returns `False` for DANGEROUS
   (FILE: `backend/ai/tools/executor.py`).
4. The executor returns
   `ToolExecutionResult(success=False, needs_confirmation=True,
   error="confirmation_required", message="Tool 'delete' requires owner
   confirmation before it can be executed.")` — **the tool is never run**.
5. `needs_confirmation` is **never consumed** anywhere
   (verified: grep shows the only references are inside
   `backend/ai/tools/executor.py` itself — no UI, no handler, no engine
   code reads it).
6. The dispatcher records the failure into conversation history
   (`add_tool_result("❌ ...")`) and asks the provider for a continuation.
7. The final user-visible answer is whatever the model says — the actual
   deletion **cannot** have happened, because the executor never called
   `tool.execute()`.

**Even the real `delete` tool, if it could run, would need correct context:**
- FILE: `backend/ai/engine/dispatcher.py` — `_build_tool_context()`
  **WHAT IT DOES:** injects `chat_id` + `reply_msg` into the tool's
  per-request `extra` dict from the `AIRequest`.
  **EVIDENCE:** `extra["chat_id"] = request.chat_id`; `extra["reply_msg"] =
  {...}` when `request.reply_context.exists`.
- FILE: `backend/ai/tools/delete.py` — `DeleteTool.execute()`
  **WHAT IT DOES:** reads `chat_id` from `ctx.extra` and calls
  `delete_service.do_del_n(chat_id, count)` (string wrapper) or the
  count-returning variant.
  **EVIDENCE:** delete.py args `{"count": ...}` and service delegation.
- FILE: `backend/services/delete_service.py` — `do_del_n_counts()` /
  `do_del_id_counts()` (added by commit `84dcf3b`)
  **WHAT IT DOES:** returns `(real_deleted_count, error)` — 0 when nothing
  matched, partial when some batches succeeded, real exception on failure.
  The string wrappers (`do_del_n` / `do_del_id`) used by panels keep
  byte-identical output.
  **EVIDENCE:** delete_service.py function definitions and tests in
  `tests/test_14_tool_honesty_glass.py`.

---

## 4. Root Cause of "Tool Call But Nothing Happens"

**CONFIRMED — three independent blockers, any one of which alone produces
the symptom:**

| # | Blocker | Evidence |
|---|---|---|
| 1 | Tool schemas are prompt **text**, not a native provider `tools` parameter → models cannot emit parseable native `tool_calls`; JSON tool calls in text are never parsed. | dispatcher `_inject_tool_schemas` (text) + `chat(messages)` (no tools param); openai_compat/gemini parse native fields only |
| 2 | `delete_last_messages` (the name in the user's report) is not a registered tool. | zero grep matches; registry list above |
| 3 | `delete` / `delete_by_id` / `organize_clean` / `settings_set` are gated behind `needs_confirmation`, which has no consumer → permanently blocked. | executor `_is_auto_executable` (READ_ONLY/READ_WRITE only); grep: `needs_confirmation` only in executor.py |

**Consequence for the AI reply:** The final AI message is generated from the
model's own text continuation, which can confidently describe a deletion it
never performed. Tool-result honesty fixes from `84dcf3b` make tool *results*
truthful, but they cannot help when the tool is never invoked.

---

## 5. Save Engine Architecture

- **FILE:** `backend/services/save_service.py` — the single authoritative
  Save Engine.
  **Public entry points:**
  - `execute_save(client, owner_id, reply_msg, mode, tz_str)` — modes `"f"`
    (forward) / `"d"` (deep). Returns a confirmation string or an
    `"❌ …"` / `"⚠️ …"` failure string.
  - `execute_link_save(client, owner_id, link, tz_str)` — URL-based save.
  - `build_caption(...)` — deterministic LifeOS caption.
  - `_append_original_text(caption, reply_msg)` — preserves original text.
  - `_upload_kwargs_for_media(media, mime_type, file_name)` — media-type
    preserving `send_file` kwargs.
  - `_extract_uploaded_metadata(sent)` — metadata from the NEWLY uploaded
    message.
- **Callers (all route through the engine — no duplicate implementations):**
  - Glass Save panel → `backend/bot/handlers/save.py` → `execute_save`
  - AI tool → `backend/ai/tools/save.py` (`SaveTool`) → `execute_save`
    (with `"forward"`/`"deep"` mapped to `"f"`/`"d"`)
- **DB layer:** `backend/db/client.py` — `insert_save(payload)` with
  Supabase + in-memory fallback.

---

## 6. Forward Save Path (mode="f")

```
reply message + .save f (or panel Forward Save / SaveTool "forward")
  │
  ▼
save_service.execute_save(mode="f")
  ├── generate save_code
  ├── extract source metadata (origin_chat_id, origin_msg_id, sender,
  │     media_type, mime, file_id, file_name)
  ├── caption = build_caption(...); caption = _append_original_text(caption, reply)
  ├── sent = client.forward_messages("me", reply_msg)   ← THE ONLY forward call site
  ├── attach caption to the forwarded message via edit_message
  │     (caption preserved; original text kept below the LifeOS header;
  │      no second standalone message)
  ├── persist DB row (source file_id, source mime/size)
  └── return build_confirmation(...)
```

- **FILE:** `backend/services/save_service.py` — `execute_save`
  **EVIDENCE:** `if mode == "f":` branch; `raw = await
  client.forward_messages("me", reply_msg)` (line ~728).
- Forward Save **is** allowed to use Telegram's native forward — this is its
  defining semantic. It preserves the original media object exactly.
- **Protected chat:** forwarding from a content-protected chat raises
  `"You can't forward messages from a protected chat"` at this call site.
  The exception is caught, logged, and returned as a failure string — the
  save does **not** silently degrade into a deep save.

---

## 7. Deep Save Path (mode="d")

**CONFIRMED: Deep Save is a true download → re-upload. It never calls
`forward_messages`.**

```
reply message + .save d (or panel Deep Save / SaveTool "deep")
  │
  ▼
save_service.execute_save(mode="d")
  ├── generate save_code
  ├── validate: media present (else text path), size ≤ max_deep_save_mb
  ├── caption = build_caption(...); caption = _append_original_text(caption, reply)
  ├── TEXT path (no media):
  │     sent = client.send_message("me", caption)      ← text stays text
  ├── MEDIA path:
  │     tmp_dir = tempfile.mkdtemp(prefix="lifeos_dl_")   ← unique temp dir
  │     tmp_path = tmp_dir/<original filename>
  │     await client.download_media(reply_msg, file=tmp_path)
  │     if os.path.getsize(tmp_path) == 0 → abort ("❌ Download produced
  │          an empty file.")
  │     sent = client.send_file("me", tmp_path, caption=caption,
  │            **_upload_kwargs_for_media(media, mime_type, file_name))
  │     finally: shutil.rmtree(tmp_dir, ignore_errors=True)   ← cleanup ALWAYS
  ├── saved_chat_id / saved_msg_id ← from the NEW `sent` message
  ├── new_file_id, new_mime, new_size = _extract_uploaded_metadata(sent)
  │     ← metadata refers to the NEW upload (falls back to source only when
  │        Telegram returns nothing)
  ├── payload = { save_code, save_type="deep", origin_*, saved_*,
  │       sender_*, mime_type, file_id, file_size=actual_size, media_type,
  │       tags, caption, owner_id, created_at }
  ├── inserted = await db_client.insert_save(payload)    ← AFTER upload
  └── return build_confirmation(...)
```

- **FILE:** `backend/services/save_service.py` — `execute_save` deep branch
  **EVIDENCE:** `download_media(reply_msg, file=tmp_path)` (line ~842),
  `send_file("me", tmp_path, caption=caption, **_upload_kwargs_for_media(...))`
  (line ~851), `shutil.rmtree(tmp_dir, ignore_errors=True)` in `finally`
  (line ~878). Zero `forward_messages` in the deep path.
- **Media-type preservation:** `_upload_kwargs_for_media` passes the
  original Telegram document attributes through (photo→photo via filename,
  video duration/w/h/streaming, audio/voice, sticker/animated, mime, original
  filename), so IMAGE→image, VIDEO→video, VOICE→voice, AUDIO→audio,
  GIF→animation, STICKER→sticker, DOC→document.
- **No event-loop blocking:** download/upload are async Telethon calls;
  the temp file is unique per operation (concurrency-safe); no global lock
  is held during transfer.
- **Protected chat:** a protected chat blocks `download_media` too
  (`"You can't download messages from a protected chat"`). The exception is
  caught and returned honestly as `"❌ Download failed: ..."` — no success
  claim, no DB row (insert happens only after upload succeeds).

---

## 8. Protected Chat Failure Path

**Question:** Can Deep Save reach `ForwardMessagesRequest`?

**Answer: NO — not in the current code.** Verified facts:

1. `forward_messages` has exactly **one** call site in the entire Save
   subsystem: `backend/services/save_service.py` inside the
   `if mode == "f":` branch (line ~728).
2. `mode` comes from the caller: glass panel passes the panel's selected
   mode; `SaveTool` maps `"forward"→"f"`, `"deep"→"d"` (commit `84dcf3b`
   fixed a bug where AI "forward" saves silently became deep saves).
3. There is **no fallback from deep to forward** and no second
   `ForwardMessagesRequest` path anywhere.

**Therefore the production error `"You can't forward messages from a
protected chat"` caused by `ForwardMessagesRequest` can only originate from
a genuine forward-save attempt** — i.e., the user (or the panel default)
used Forward Save, or the Save panel's default mode is forward. The
correct response for a protected chat is: use Deep Save (download/re-upload),
which the engine already implements.

---

## 9. Caption / Media Pipeline

- **FILE:** `backend/services/save_service.py`
  - `build_caption(save_code, sender, chat_id, msg_id, dt, media_type, mime,
    file_size, file_name, tags)` — deterministic LifeOS caption with the
    save code, origin metadata, and tags.
  - `_append_original_text(caption, reply_msg)` — appends the source
    message's original text below the LifeOS header (single caption, not a
    separate message).
  - `_upload_kwargs_for_media(media, mime_type, file_name)` — media-type
    preserving `send_file` kwargs (attributes, mime, filename).
  - `_extract_uploaded_metadata(sent)` — pulls `file_id`, `mime_type`,
    `size` from the **newly uploaded** message's media.
- **Verified wiring:** forward save attaches the caption to the forwarded
  message via `edit_message` (no separate message, original text preserved);
  deep save passes `caption=caption` directly to `send_file`/`send_message`.
  No broken-emoji/box formatting: caption is plain text with Markdown-style
  markers consistent with the rest of the project.
- **Limitation:** live formatting was not exercised against a real Telegram
  session in this sandbox (no credentials); verified with mock clients and
  unit tests (`tests/test_12_save_engine.py`).

---

## 10. Database Persistence Pipeline

**Verified ordering (both save modes):**

```
1. validate source
2. (deep) download
3. (deep) upload / (forward) forward
4. verify resulting saved message (`sent`)
5. extract metadata from the NEW message (deep) / source (forward)
6. persist DB record (insert_save)      ← AFTER Telegram operation succeeds
7. cleanup temp resources (deep: finally)
8. return confirmation / honest failure string
```

- A failed download/upload/forward returns `"❌ …"` **before** any
  `insert_save` — a failed Telegram operation cannot create a
  successful-looking DB row.
- If `insert_save` itself fails after a successful upload, the error is
  logged (`[SAVE_DB] ... row NOT in database`) and the command still reports
  the save — the existing project architecture treats DB failure as a
  logged warning, not a fabricated success. **This asymmetry is intentional
  and documented; a future fix may want to surface it to the user.**
- Fields populated: `save_code`, `save_type` (f/d), `origin_chat_id`,
  `origin_msg_id`, `saved_chat_id`, `saved_msg_id`, `sender_name`,
  `sender_id`, `mime_type`, `file_id`, `file_size`, `media_type`, `tags`,
  `caption`, `owner_id`, `created_at`.

---

## 11. Runtime / Task-Starvation Analysis

**CONFIRMED (code): diagnostics accounting leak.**

- **FILE:** `backend/ai/diagnostics.py`
  - `register_start(request_id, owner_id)` — adds to `_active` (line ~50)
  - `register_end(request_id)` — removes from `_active` (line ~78)
  - `ai_active` = `len(_active)` (line ~129)
- **Callers of `register_start`:** every AI entry point, on every request
  (`ai_cmd.py`, `ai_unified.py`).
- **Callers of `register_end`:** ONLY exception branches —
  `ai_cmd.py` lines 286 (TimeoutError), 302 (CancelledError), 306 (generic
  Exception); `ai_unified.py` lines 445/458/462 (same pattern).
  **Successful requests never call `register_end`.**
- **Consequence:** `ai_active` grows monotonically with every successful
  request; the stage string freezes at the last `set_stage` value of the
  oldest registered request (e.g. `TELEGRAM_REPLY`). This fully explains
  `ai_active=10, ai_stage=TELEGRAM_REPLY, ai_last_db_s=224` style heartbeat
  values **without** any concurrency.

**LIKELY / UNKNOWN (no production logs in this environment):**
- Whether real starvation occurred concurrently is **unknown**. The
  heartbeat module (`backend/runtime/heartbeat.py`) reports these values;
  the long-running Telethon loops (`_recv_loop`, `_send_loop`,
  `_update_loop`, `_keepalive_loop`) are normal Telethon internals and must
  **not** be "fixed".
- No evidence in code of duplicate Telethon clients, deadlocks, or
  unbounded `asyncio.create_task` in the AI/Save paths. `SaveTool` and the
  save engine perform async-only I/O with bounded temp-file lifetimes.
- `ToolExecutor` runs tools sequentially with a 10s timeout
  (`TOOL_TIMEOUT_SECONDS = 10`) — bounded.

---

## 12. Confirmed Facts

1. Tool schemas are injected as **plain text** into the prompt
   (dispatcher `_render_tool_schemas` / `_inject_tool_schemas`); the
   provider call is `chat(messages)` with **no native `tools` parameter**.
2. Provider parsers (`openai_compat.py`, `gemini.py`) read **only native
   `tool_calls` / `functionCall` parts** — text-embedded JSON tool calls are
   never parsed.
3. `delete_last_messages` is **not registered** anywhere.
4. `delete`, `delete_by_id`, `organize_clean` are `DANGEROUS`;
   `settings_set` is `ADMIN_ONLY`. The executor returns
   `needs_confirmation=True` without executing; **`needs_confirmation` has
   zero consumers** → these tools are permanently blocked.
5. Deep Save (`mode="d"`) is a true download→temp-file→re-upload; the only
   `forward_messages` call is inside the `mode == "f"` branch. No
   deep→forward fallback exists.
6. Deep Save DB record is created **after** a successful upload and refers
   to the **newly uploaded message** (file_id/mime/size from
   `_extract_uploaded_metadata(sent)`).
7. Temp files use `tempfile.mkdtemp(prefix="lifeos_dl_")` and are removed in
   `finally` on success and failure (including the empty-download abort).
8. Text-only Deep Save sends a text message (never a fake document).
9. `register_start` is called on every AI request; `register_end` only in
   exception branches → `ai_active`/stage accounting leaks on success.
10. Repo HEAD `84dcf3b` == remote `main` HEAD; working tree clean except
    untracked `FREEBUFF_PRE_PUSH_VERIFY.md`.

## 13. Likely Causes

- The user-visible "AI said it deleted 10 messages" narrative most likely
  comes from the model's **text-only** reply describing a tool call the
  runtime never executed (blockers 1+3), with the invented tool name
  `delete_last_messages` (blocker 2) being the model's own naming.
- Protected-chat forward errors reported in production are almost certainly
  genuine **Forward Save** attempts (or the save panel's default mode being
  forward), not Deep Save routing bugs.

## 14. Unknowns / Missing Evidence

- **No production logs were available** in this sandbox: no actual
  `TASK_STARVATION` trace, no real provider chat transcripts, no real
  protected-chat session logs. All conclusions above are from source code
  and unit-test-verified behavior.
- Whether the model ever emits **native** `tool_calls` for providers that
  receive no `tools` parameter (some may hallucinate a `tools` call anyway)
  is untested live; the parse path would work only for providers returning
  native structures.
- Whether a confirmation UX is *planned* (a future `needs_confirmation`
  consumer) is unknown; none exists today.

## 15. Exact Files and Functions Involved

| File | Functions / classes |
|---|---|
| `backend/ai/engine/dispatcher.py` | `Dispatcher.dispatch`, `_render_tool_schemas`, `_inject_tool_schemas`, `_build_tool_context`, `_build_continuation_messages` |
| `backend/ai/providers/openai_compat.py` | response parser (`message.get("tool_calls")`) |
| `backend/ai/providers/gemini.py` | response parser (`functionCall` parts) |
| `backend/ai/providers/manager/manager.py` | `ProviderManager.chat`, `apply_selection` |
| `backend/ai/tools/registry.py` | `ToolRegistry`, `create_default_registry` |
| `backend/ai/tools/executor.py` | `ToolExecutor.execute_calls`, `_execute_single`, `_is_auto_executable` |
| `backend/ai/tools/base.py` | `PermissionLevel`, `Tool`, `ToolResult` |
| `backend/ai/tools/delete.py` | `DeleteTool`, `DeleteByIdTool` (DANGEROUS) |
| `backend/services/delete_service.py` | `do_del_n_counts`, `do_del_id_counts`, string wrappers |
| `backend/bot/handlers/ai_cmd.py` | `.ai` handler, `_restore_config`, `_humanize_error`, `register_start/register_end` call sites |
| `backend/bot/handlers/ai_unified.py` | trigger handler, `register_start/register_end` call sites |
| `backend/ai/diagnostics.py` | `register_start`, `register_end`, `set_stage`, `ai_active` |
| `backend/services/save_service.py` | `execute_save`, `execute_link_save`, `build_caption`, `_append_original_text`, `_upload_kwargs_for_media`, `_extract_uploaded_metadata` |
| `backend/ai/tools/save.py` | `SaveTool` (mode mapping forward→f / deep→d) |
| `backend/db/client.py` | `insert_save`, `log` |
| `tests/test_12_save_engine.py` | save engine regression tests |
| `tests/test_13_model_selection.py` | runtime-selection pipeline tests |
| `tests/test_14_tool_honesty_glass.py` | tool honesty + compact glass tests |

## 16. Minimal Fix Surface

Ordered by impact (implementation agent should confirm current state before
editing — the repository may have moved):

1. **Native tool-call wiring** (`dispatcher.py` + provider contract):
   - Pass a native `tools` list to `ProviderManager.chat()` (OpenAI `tools`
     / Gemini `tools.functionDeclarations`), or implement an explicit
     structured JSON-parse fallback for text-embedded tool calls with strict
     validation. The prompt-text approach may stay as documentation, but the
     runtime must receive structured tools.
   - Ensure providers accept the tools param and echo tool results in
     continuation rounds (continuation messages already build
     `assistant.tool_calls` + `role: tool` entries).
2. **Confirmation UX for DANGEROUS tools** (`executor.py` + a consumer):
   - Either implement a real `needs_confirmation` consumer (a Telegram
     inline confirm/cancel flow via the Glass panel system), or — for the
     self-bot single-owner model — allow DANGEROUS tools to execute when the
     request originates from the owner with an explicit risk-acknowledged
     instruction, with strict result honesty. Do NOT silently unblock.
3. **Tool-name enforcement / prompt guard** (`prompt/builder.py` +
   `ai_cmd.py`/`ai_unified.py`):
   - Restrict the model to the registered tool names in the prompt and
     verify against `registry.list_names()`; reject unknown names in the
     executor (already does) and surface that rejection to the user.
4. **Narration guard** (handlers):
   - If a turn produced tool calls that were not executed (blocked/
     unregistered), append an explicit warning to the user-visible reply —
   do not let the model claim success it cannot have.
5. **Diagnostics leak** (`backend/ai/diagnostics.py` + handlers):
   - Ensure `register_end(rid)` runs on the success path (wrap the whole
     request in `try/finally`), so `ai_active`/stage reflect reality.
6. **Save panel default mode** (`backend/bot/handlers/save.py`):
   - Confirm the panel default matches user intent (forward vs deep) so a
     protected chat never accidentally routes into forward; keep the
     engine's mode semantics untouched.
7. **Bounded save/delete operations** (optional hardening):
   - `asyncio.wait_for` around `download_media`/`send_file`/forward with a
     sane ceiling (the engine currently relies on Telethon's own timeouts).

## 17. Recommended Fix Order

1. **Native tools parameter + structured tool parsing** (fixes the primary
   symptom end-to-end).
2. **Confirmation path or owner-trusted DANGEROUS execution** (unblocks
   `delete` / `delete_by_id` / `organize_clean` / `settings_set` with honest
   results).
3. **Tool-name whitelist enforcement + narration guard** (stops false-success
   claims at the source).
4. **`register_end` on success** (truthful `ai_active`/heartbeat).
5. **Save panel mode default check + optional upload/download timeouts**
   (protected-chat UX and starvation-report hygiene).

---

## Repository State Verification (no changes made)

- Local HEAD: `84dcf3ba34d6d78ddeb6724b2a7c78f1eab5f544`
- Remote `refs/heads/main`: `84dcf3ba34d6d78ddeb6724b2a7c78f1eab5f544` ✅
- Working tree: clean except untracked `FREEBUFF_PRE_PUSH_VERIFY.md`
- This investigation made **no code changes, no commit, no push** — per the
  investigation-only mandate.
