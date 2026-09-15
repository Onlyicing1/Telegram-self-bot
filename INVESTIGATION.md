# Media Processing — Architectural Investigation

> **Investigation only — nothing was implemented.** This document reports the
> source-backed findings of the Media Processing investigation. It **replaces
> the previous `INVESTIGATION.md` entirely**; no earlier content (including the
> Telegram-history investigation) is preserved, merged, appended, or referenced
> as a historical section. No production code, tests,
> `IMPLEMENTATION_REPORT.md`, schema, migrations, configuration, dependency,
> presentation, delivery, context-retrieval, Save/Saved-Items, provider or
> runtime file was modified. No media processor, service, handler, tool,
> package, table, scheduler, client or loop was created.

## 1. Investigation Metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Audited HEAD | `893d3f439f5a5a266a302ef5a2b4721f0ab144d1` (short `893d3f4`, `docs: record the all-letter save-code grammar fix`) |
| Investigation date | 2026-09-15 |
| Status | **Investigation only. No implementation was performed during this investigation.** |
| Scope | Whether, and at which existing architectural boundary, a controlled Media Processing layer can be added upstream of the owner's currently selected LLM provider — without changing that provider, and without leaking Telegram/conversational context to the model. |
| Question | How can Telegram media (photo, voice, audio, document, video, sticker, GIF) be resolved, downloaded, validated and normalized into a controlled representation that the **currently selected** chat provider consumes as ordinary input? |
| Verdict | **GO WITH REQUIRED PREWORK.** No multimodal input path is wired today (`vision()` is declared-but-dead across every adapter), and both existing media-download paths are unbounded. A provider-independent, text-normalized media capability at the services layer is required first; true native image/audio understanding would require a provider-interface change and is therefore out of M1. |
| Files changed by this investigation | only `INVESTIGATION.md` |
| Classification labels | **[CURRENT]** implemented behavior verified in source at `893d3f4` · **[FINDING]** conclusion derived from that source (evidence cited) · **[RECOMMENDED]** future proposal — **nothing in those sections is implemented** · **[UNKNOWN]** requires an implementation-phase decision |

Evidence-strength tags used inline where a claim is not directly readable in
source: **VERIFIED FROM SOURCE**, **INFERENCE FROM SOURCE**, **UNKNOWN /
REQUIRES IMPLEMENTATION DECISION**.

---

## 2. Investigation Method

Read-only tracing from the Telegram activation handler outward to Telethon and
back through the provider mesh, plus the services, prompt, context, tool and
configuration layers. Areas inspected (all at `893d3f4`, all read-only):

| Area | Files inspected |
|---|---|
| Activation / request construction | `backend/bot/handlers/ai_unified.py` (1011 lines, full) |
| Request object | `backend/ai/session/request.py` |
| Execution | `backend/ai/engine/dispatcher.py` (2223 lines, targeted windows around `dispatch`, `_provider_chat`, `_build_tool_context`, `_try_local_fast_path`, `_read_results_authoritative`, `_build_messages`, `_build_continuation_messages`, `_build_context`), `backend/ai/engine/engine.py` |
| Providers | `backend/ai/providers/base/{contract,capabilities,config}.py`, `backend/ai/providers/{openai_compat,openai,gemini,dummy}.py`, `backend/ai/providers/manager/manager.py` |
| Media | `backend/ai/media.py`, `backend/telegram_api/{__init__,api,media,messages,_helpers,exceptions}.py` |
| Context / prompt | `backend/ai/conversation/telegram_context.py`, `backend/ai/conversation/context_builder.py`, `backend/ai/context/provenance.py`, `backend/ai/context/reply_resolver.py`, `backend/ai/prompt/{builder,budget,serializer,template}.py` |
| Tools / services | `backend/ai/tools/{base,context,registry,executor,save,history_ai}.py`, `backend/services/save_service.py`, `backend/services/history_service.py`, `backend/services/history_ai_service.py`, `backend/services/settings_service.py`, `backend/services/delete_service.py` |
| Limits / wiring | `backend/helper/rpc_timeout.py`, `backend/runtime/operation_watchdog.py`, `backend/ai/tools/delivery.py` |
| Runtime / deps | `requirements.txt`, `render.yaml`, `Procfile`, installed venv package list |
| Design docs | `AI_MASTER_DESIGN.md` §18 (Non Goals), §20 (Future Ideas), §28 (Resource Budget), §29 (Deterministic Runtime Rules) |

Method notes: no live Telegram, Supabase, Render or network access was used; no
package was installed; no test was executed (no code changed, so there was
nothing to test). Where a conclusion depends on a symbol's presence rather than
on executed behavior, that is stated as **[FINDING]** with the exact symbol and
file cited. Repository-wide greps were targeted (`vision`, `download_media`,
`supports_images`, `classify_message`, `ffmpeg`, OCR/STT/multimodal keywords),
not exhaustive.

---

## 3. Current AI Execution Pipeline

**[CURRENT]** The real flow, with exact symbols. Async/sync and context payload
are stated for every stage because they determine where media can enter.

```
Telegram outgoing message (owner's own account)
  └─ ai_unified.py::register.ai_unified_handler            ai_unified.py:920   @client.on(events.NewMessage(outgoing=True))
       ├─ is_owner(event, owner_id)                        → non-owner: return
       ├─ raw_text = event.raw_text or ""                  ai_unified.py:924
       ├─ if not raw_text: return                          ai_unified.py:925   ← blocks caption-less media
       ├─ if raw_text.startswith("."): return
       ├─ _load_triggers(owner_id) → match_trigger(...)     (TTL-cached ai_config row)
       ├─ reply-to-AI sniff: event.get_reply_message()
       │    + reply_resolver.get_resolver().resolve(id)    ai_unified.py:~955
       ├─ _extract_reply_context(...)                      ai_unified.py:425   async
       │    ├─ classify_message(reply_msg) → MediaInfo      ai_unified.py:459, media.py:70
       │    ├─ reply_msg.get_sender() / get_chat()          (parallel, best-effort)
       │    └─ ReplyContext(..., media_type, text_preview[:200], ai_content from resolver)
       └─ _execute_ai(event, owner_id, user_message, ...)   ai_unified.py:581   async
            ├─ semaphore gate: asyncio.wait_for(sem.acquire(), timeout=_AI_TIMEOUT=60.0)   ai_unified.py:62, :619
            ├─ event.edit("Thinking…")
            ├─ _restore_config(owner_id, config)            engine.apply_persisted_config
            ├─ _load_telegram_chat_context(...)             ai_unified.py:548
            │    └─ telegram_context.fetch_telegram_chat_context(...)   ← bounded 10-message window (3s)
            ├─ AIRequest(session_id, user_message, owner_id, chat_id, message_id,
            │            reply_context, telegram_context, timezone, request_id,
            │            timeout_s=_AI_EXECUTE_TIMEOUT=240.0)              ai_unified.py:71
            ├─ engine.execute(request, status_callback)     engine.py::Engine.execute
            │    └─ Dispatcher.dispatch(request, ...)       dispatcher.py:204
            │         1 conversation runtime: add_user_message
            │         2 _try_local_fast_path(...)           dispatcher.py:1479   deterministic, NO provider round
            │              └─ parse_command_intent(text, has_reply, reply_text)  actions.py:1722
            │         3 _build_context(request, session)    dispatcher.py:2009   ContextBuilder + MemoryManager
            │         4 PromptBuilder.build(context, tool_block)  prompt/builder.py
            │         5 _build_messages(package)            dispatcher.py:1952   plain {role, content:str} dicts
            │         6 _provider_chat(...)                 dispatcher.py:245
            │              └─ self._provider_manager.chat(messages, **kwargs)    dispatcher.py:274
            │                   └─ ProviderManager.chat(...)                     manager.py:103
            │                        └─ _attempt_with_retry → provider.chat(...)  openai_compat.py:65 / gemini.py
            │         7 tool loop (_build_continuation_messages)  dispatcher.py:1967, MAX_TOOL_ROUNDS=3 (dispatcher.py:58)
            │              └─ ToolExecutor.execute_calls → tool.execute()        executor.py
            └─ deliver_response(event, display_prompt, response_text, show_question)   ai_unified.py:798
            └─ reply_resolver.register(...)                 (associates the delivered message with the AI reply)
```

**[VERIFIED FROM SOURCE]** Stage-by-stage contracts:

| Stage | Symbol | In → Out | Async | Understands media | Model-visible Telegram context |
|---|---|---|---|---|---|
| Activation | `ai_unified_handler` :920 | event → gated call | async | no | no |
| Reply extraction | `_extract_reply_context` :425 | `Message` → `ReplyContext` | async | **label only** (`media_type`) | **yes** (see §7) |
| Window | `fetch_telegram_chat_context` (`telegram_context.py`) | chat+anchor → `TelegramChatContext` | async | label only (`_media_type`) | **yes** (10 messages) |
| Request | `AIRequest` (`session/request.py`) | frozen dataclass | — | no | fields exist for both |
| Fast path | `_try_local_fast_path` :1479 + `parse_command_intent` (`actions.py:1722`) | **text only** → tool calls | async | **no — pure text parser, zero media vocabulary** | reply text only |
| Prompt | `PromptBuilder.build` (`prompt/builder.py`) | context → `PromptPackage` | sync | no | renders reply + window |
| Assembly | `_build_messages` :1952 | package → `list[{"role","content":str}]` | sync | no | — |
| Provider call | `_provider_chat` :245 → `ProviderManager.chat` :103 | messages → `ProviderResponse` | async | **no** | — |
| Tool loop | `_build_continuation_messages` :1967 | tool_calls → results | async | no | tool JSON only |
| Delivery | `deliver_response` (`delivery.py`) | text → edited/split messages | async | no | — |

**[FINDING]** Two and only two seams exist where a media capability can enter:
(a) the deterministic fast path (`parse_command_intent` + `_try_local_fast_path`,
which runs before any provider round and is where the existing saved-item
retrieve/preview/delete routes live); and (b) a registered tool executed by the
provider tool loop (where `translate_history` / `summarize_history` live).
`parse_command_intent` reads **text only** — it cannot see media — so a
deterministic media route must receive the media signal from the request scope
(`AIRequest.reply_context.media_type`, already available to
`Dispatcher._build_tool_context` at `dispatcher.py:1233`) rather than from the
user's text.

**[FINDING]** Prompt assembly is text-only end to end. `_build_messages`
(`dispatcher.py:1952`) emits `{"role": "system"|"user", "content": <str>}` and
`OpenAICompatProvider.chat` (`openai_compat.py:65`) places that list directly into
`payload["messages"]`. No content-part, attachment, image-URL or base64 shape
exists anywhere in the pipeline.

---

## 4. Current Media Capabilities

**[CURRENT]** The only media machinery in the repository:

| Capability | Location | Class |
|---|---|---|
| Media **classifier** — pure attribute inspection, no I/O | `backend/ai/media.py::classify_message` → `MediaInfo` (`media.py:70`) | reusable as-is |
| Classifier consumers | `ai_unified.py:459` (`_extract_reply_context`), `telegram_context.py` `_media_type` | reusable as-is |
| Media **download** over the typed facade | `backend/telegram_api/media.py::download_media` (`media.py:18`) → raw `client.download_media` | incomplete/unsafe — **no timeout, no size check, no type validation** |
| Deep Save download → validate → re-upload | `backend/services/save_service.py::execute_save` (`save_service.py:392`–`465`) | reusable **pattern**, not the code |
| MIME→media-type map, extension map, byte formatter | `save_service.detect_media_type`, `_MIME_EXT`, `_format_bytes` | reusable as-is |
| Filename extraction / generated names | `save_service.extract_file_name`, `generate_filename` | reusable as-is |
| Serialized message media facts | `telegram_api/_helpers.py::serialize_message` | **`has_media: bool` only** — no mime/size/type reach facade dicts |
| Upload attribute preservation | `save_service._upload_kwargs_for_media` | unrelated to analysis |
| Content extraction (OCR, STT, PDF, frames) | — | **absent** |
| Media analysis | — | **absent** |
| Media → LLM delivery | — | **absent** |

Per media type, distinguishing detection / metadata / download / extraction /
analysis / current LLM delivery:

| Type | Detection | Metadata classification | Download | Content extraction | Analysis | Reaches the selected LLM |
|---|---|---|---|---|---|---|
| **Photo** | `MessageMediaPhoto` (`media.py`) → `"Photo"`, forced `image/jpeg`, size from `photo.sizes[-1].size` | `MediaInfo.media_type/mime_type/file_size` | only via Deep Save (`save_service.py:439`) | none | none | **no** — only the label `"Photo"` reaches `ReplyContext.media_type` (`ai_unified.py:459`) |
| **Voice** | `DocumentAttributeAudio.voice=True` → `"Voice"` | mime, size, filename | only via Deep Save | none (no STT) | none | **no** — label only |
| **Audio** | `DocumentAttributeAudio.voice=False` → `"Audio"` | mime, size, filename | only via Deep Save | none | none | **no** — label only |
| **Document** | `MessageMediaDocument` fallback → `"Document"` | mime, size, filename (`DocumentAttributeFilename`) | only via Deep Save | none (no PDF/DOCX/zip reader) | none | **no** — label only |
| **Video** | `DocumentAttributeVideo` → `"Video"` | mime, size, filename | only via Deep Save | none (no ffmpeg, no frame sampler) | none | **no** — label only |
| **Sticker** | `DocumentAttributeSticker` → `"Sticker"` (WEBP/TGS) | mime, size | only via Deep Save | none (no WEBP/TGS decoder) | none | **no** — label only |
| **Animation/GIF** | `DocumentAttributeAnimated` → `"Animation"`; `mime=="image/gif"` → `"GIF"` | mime, size, filename | only via Deep Save | none | none | **no** — label only |
| **WebPage** | `MessageMediaWebPage` → `"WebPage"`, `text/html` (`media.py`) | type + mime | never | never | none | label only |
| Contact / Poll / Location | `MessageMediaContact` / `Poll` / `Geo` | type only | never | never | none | label only |

**[FINDING]** Media is therefore *detected and labelled* everywhere, and
*downloaded* only by Deep Save — where the bytes exist solely to be re-uploaded.
No media byte ever reaches a provider, and no media capability exists in
`backend/ai/`.

---

## 5. Telegram Media Download Boundary

**[CURRENT]** Exactly two code paths can download media, and **both are
unbounded**:

1. **`backend/services/save_service.py:439`** — `await client.download_media(reply_msg, file=tmp_path)`
   (raw Telethon call; no `rpc_await`, no `guarded_await`, no timeout).
2. **`backend/telegram_api/media.py:32`** — the same call inside the typed facade
   wrapper; catches exceptions into `TelegramAPIError` but imposes **no timeout**.
   (Contrast: `telegram_api/messages.py` wraps every short call in
   `guarded_await(..., timeout=_SHORT_CALL_TIMEOUT = 30.0)`; the media module does
   not.)

Analysis of the existing boundary:

| Property | Current state | Evidence |
|---|---|---|
| Download function | `client.download_media(msg, file=path)` (save path) / `TelegramAPI.download_media(message, file_path, progress_callback)` (facade) | `save_service.py:439`; `telegram_api/media.py:18`; `telegram_api/api.py:102` |
| Timeout | **none on either path** | `telegram_api/media.py` has no bounded await; `save_service.py:439` calls the client directly |
| File-size limit | checked **before** download against Telegram's declared size: `settings_service.max_deep_save_mb()` — default **50 MB**, validated range 1..500 | `save_service.py:396`–`399`; `settings_service.py:69`, `:113`, `:281` |
| Post-download validation | `os.path.exists(tmp_path)` and `getsize > 0` only | `save_service.py:445`–`451` |
| MIME / type validation | **none** — the MIME type is whatever Telegram declares; the filename is Telegram-supplied and used only as the temp filename (`os.path.basename`) | `save_service.py` `_extract_source_media`, `generate_filename` |
| Temporary storage | `tempfile.mkdtemp(prefix="lifeos_dl_")` — OS temp dir, not configurable, per-operation | `save_service.py:435` |
| Cleanup | `shutil.rmtree(tmp_dir, ignore_errors=True)` in `finally` (every exit path) | `save_service.py:465` |
| Event-loop behavior | non-blocking: `download_media` is awaited on the async Telethon client and streams to a path. Handing it a `BytesIO` would materialize the whole file in RAM — so a bounded path must stay file-based | `save_service.py:439`; `telegram_api/media.py:24` |
| Error handling | save path catches broad `Exception` and returns an honest `"❌ Deep Save failed: …"` string (services never raise); facade raises `TelegramAPIError` / `TelegramTimeoutError` | `save_service.py:441`, `:455`; `telegram_api/exceptions.py` |
| FloodWait | **no** FloodWait handling in either media path (the facade docstring in `telegram_api/__init__.py` claims FloodWait handling, but no media code implements it) | `telegram_api/media.py` |

**[FINDING]** The only reusable safety primitives are the *pattern* (size
pre-check → download to a `mkdtemp` dir → validate → `finally: rmtree`) and the
existing bounded-await helpers `backend/helper/rpc_timeout.py::rpc_await` and
`backend/runtime/operation_watchdog.py::guarded_await`. What is **missing** is a
download that is itself bounded by one of them.

---

## 6. Current Provider Interface

**[CURRENT] The provider interface is TEXT-ONLY in practice.** The abstract
contract declares an image seam, but no adapter implements it and nothing calls
it.

| Layer | Symbol | Actual accepted input | Verdict |
|---|---|---|---|
| Abstract contract | `BaseProvider.chat(messages, **kwargs)` — `contract.py:102` | `list[dict[str, Any]]` (role/content) | **text** |
| Abstract contract | `BaseProvider.vision(messages, images: list[bytes])` — `contract.py:104` | declared `images: list[bytes]`; the **default body returns `NOT_IMPLEMENTED`** | declared, **not implemented** |
| Capabilities object | `ProviderCapabilities.supports_images` — `providers/base/capabilities.py:21` | a boolean flag | declared **and consumed nowhere** |
| OpenAI-compatible base | `OpenAICompatProvider.chat` — `openai_compat.py:65` | plain messages; handles `tools`, `response_format`; **no content parts, no attachments** | **text** |
| OpenAI-compatible base | `OpenAICompatProvider.vision` — `openai_compat.py:253` | returns `NOT_IMPLEMENTED` unconditionally, even though `capabilities.supports_images=True` (`openai_compat.py:40`) | declared, **not implemented** |
| Gemini | `GeminiProvider` — `gemini.py:36` declares `supports_images=True`; **defines no `vision`** | inherits the `NOT_IMPLEMENTED` default | declared, **not implemented** |
| OpenAI | `backend/ai/providers/openai.py:33` declares `supports_images=True` | inherits `OpenAICompatProvider.vision` → `NOT_IMPLEMENTED` | declared, **not implemented** |
| Dummy | `providers/dummy/provider.py:54` | `supports_images=False` | text |
| Manager | `ProviderManager.vision(messages, images, **kwargs)` — `manager.py:308` | `def` (**synchronous**) calls `provider.vision(...)`; for the OpenAI-compatible adapters that attribute is `async def`, so the call yields a coroutine, attribute access on it raises, the broad `except` swallows it and returns `_fallback_vision` (`manager.py:1108`) → `"no healthy vision provider"` | latent defect + **never called** |
| Manager routing | `ProviderManager.vision` uses `_get_healthy_provider()` (`manager.py:563`) — **it ignores the owner's active-provider ordering** that `chat` honors | text: image/audio/binary/file/URL input | — |
| Call sites | `grep -rn "\.vision("` across `backend/` finds **zero** production call sites (only the definition, the manager, and `_fallback_vision`) | — | **dead path** |

**[VERIFIED FROM SOURCE]** Input capabilities per category:
text **yes**; images/multimodal **no** (declared flag only); file input **no**;
audio **no**; binary/base64 **no**; URL/reference **no**; provider-specific
content blocks **no** — every adapter sends `{"model", "messages", "temperature",
"max_tokens"}` with string content (`openai_compat.py:75`–`100`).

**[FINDING] Answer to the provider-independence requirement.** Because the
`vision()` seam is dead, unimplemented, and (in the manager) not even wired to
the active-provider ordering, **media cannot be handed to a provider as media
today.** The only route that both (a) lets the owner keep *any* currently
selected provider and (b) keeps the selected provider unchanged is to
**normalize media into text upstream** and send it through the ordinary
`ProviderManager.chat(...)` path. That path already honors active-provider-first
ordering, model-level candidate failover, retry classification and cooldown
(`manager.py:103` onward). An existing precedent proves a service layer may do
exactly this: `backend/services/history_ai_service.py:453`
(`manager.chat(messages, tools=[])`) reaches the mesh through
`ToolContext.extra["provider_manager"]`, which `Dispatcher._build_tool_context`
(`dispatcher.py:1233`) sets from the live engine.

---

## 7. Context / Privacy Boundary

### 7.1 Every current injection path into the model

**[CURRENT]** These fields are attached to an AI request and rendered into the
prompt. Each is a candidate leak vector for a media request.

| # | What enters | Field / symbol | Added by | Rendered where |
|---|---|---|---|---|
| 1 | Owner's text after the trigger | `AIRequest.user_message` | `_execute_ai` (`ai_unified.py:581`) | `PromptBuilder._render_user_message` |
| 2 | Replied message: sender id + name, chat id + title, `media_type`, **200-char `text_preview`**, timestamp | `AIRequest.reply_context` (`ReplyContext`) | `_extract_reply_context` (`ai_unified.py:425`, `:459`) | `PromptBuilder._render_conversation_state` → `[Reply Context]` |
| 3 | For a replied message registered as an AI answer: the **full untruncated AI response** + provider/model/session | `ReplyContext.ai_content` etc. | `_extract_reply_context` via `reply_resolver.get_resolver().resolve(...)` | `[Reply to AI Message]` |
| 4 | Up to **10 real surrounding Telegram messages** (sender name, local `HH:MM`, per-message 200-char text, media label, ≤1500 chars total, ≤4 sender resolutions, 3 s bound) | `AIRequest.telegram_context` (`TelegramChatContext`) | `_load_telegram_chat_context` (`ai_unified.py:548`) → `fetch_telegram_chat_context` | `[Telegram Chat Context]` via `ctx.telegram_chat.render()` |
| 5 | AI session turns | `ConversationContext.history` | `Dispatcher._build_context` (`dispatcher.py:2009`) | `[History] (n entries)` |
| 6 | Retrieved memories | `ConversationContext.memory` | `MemoryManager.retrieve_for_prompt` | `[Memory]` |
| 7 | Tool schemas + last tool result | `ConversationContext.tool` | dispatcher | `[Tool Context]` / `[Tool Results]` |
| 8 | Request-scoped runtime keys: `chat_id`, `request_message_id`, `request_text`, `request_id`, `request_timeout_s`, `provider_manager`, `reply_msg{message_id, sender_id, sender_name, chat_id, chat_title, media_type, text_preview, timestamp}` | `ToolContext.extra` | `Dispatcher._build_tool_context` (`dispatcher.py:1233`) | **tool-visible only** (not a model message) |

**[FINDING] The precise leak vector for media.** Items 2 and 4 are attached
**unconditionally** inside `_execute_ai` for every reply-shaped request
(`ai_unified.py:581`–`640`). Because the current flow can only reach media
through a reply target — `_extract_reply_context` errors with `"No replied
message found. Reply to a message first."` when there is none
(`ai_unified.py:425` region) — a media request is *by construction* a reply, so
the replied message's 200-char preview **and** up to 10 unrelated surrounding
Telegram messages would travel in the same prompt as the media. Nothing in the
current code gates them.

**[CURRENT] Existing provenance discipline (must be preserved, not duplicated).**
`backend/ai/context/provenance.py::AI_PROVENANCE_MARKER` (`U+2061\u2062\u2063\u2064`)
is the only trusted authorship signal; `has_ai_provenance_marker` /
`strip_ai_provenance_marker` are the single authority. It is enforced in
`telegram_context.build_chat_context` and in `services/history_service.py`.
`sender_id == owner_id` and `out=True` are explicitly **not** provenance
(`telegram_context.py` module docstring). A media layer must not add a second
predicate or regex.

### 7.2 The required future boundary (hard architectural rule)

> **This section states a requirement. It is NOT implemented. Nothing in the
> current source satisfies it for media.**

The model must receive **only**:

1. the user's explicit request text (the text they actually typed), and
2. a controlled, normalized media representation produced by the application
   (e.g. extracted/OCR text, a speech transcript, a document extract, bounded
   visual analysis or frame descriptions, normalized metadata as *data trusted
   by the application* — not as instructions).

The model must **not** receive: raw Telegram messages; replied-message text;
previous chat messages or Telegram history; sender information; chat
information; message IDs as conversational context; arbitrary Telegram metadata;
`reply_context`; `telegram_context`; or any inferred conversational context.

Deterministic Telegram-side resolution (which message, which chat, which
attachment) happens **outside** the AI, in trusted runtime code, exactly the way
the saved-item retrieve/preview/delete routes already resolve a save code from
the request scope before the model is consulted.

**[FINDING]** Satisfying this rule for media means the media path must **not**
reuse `AIRequest.reply_context` / `AIRequest.telegram_context` for its model
call. Two source-supported ways exist: (a) gate those two fields off for
media-only requests, or (b) build the media call's own message list inside the
media service instead of reusing `Dispatcher._build_messages`
(`dispatcher.py:1952`). Option (b) requires no change to the ordinary AI request
shape at all and is the smaller change. **[INFERENCE FROM SOURCE]**

---

## 8. Tool / Service Architecture

**[FINDING]** Media Processing belongs at **a services-layer capability invoked
by a thin registered tool**, with deterministic resolution performed in the
runtime scope (not by the model). Evidence:

| Evidence | Source |
|---|---|
| "No feature may bypass the Tool Layer. The AI Core talks to the runtime exclusively through tools. No AI module may import Telethon, Supabase, or runtime internals directly." | `AI_MASTER_DESIGN.md` §29.4(27) |
| One execution authority: the executor is "the SOLE component that calls `tool.execute()`"; the deterministic fast path routes through it too | `ai/tools/executor.py` module docstring; `dispatcher.py:1479` `_try_local_fast_path` → `execute_calls` |
| Long-running exemption already exists for media-adjacent work | `ToolExecutor._execute_single` skips `TOOL_TIMEOUT_SECONDS = 10` when `tool.long_running`; precedent `SaveTool` / `SaveByLinkTool` (`ai/tools/save.py`) and `TranslateHistoryTool` / `SummarizeHistoryTool` |
| A **service** may legitimately call the provider mesh with a self-built message list | `services/history_ai_service.py:453` `manager.chat(messages, tools=[])`, reached through `ToolContext.extra["provider_manager"]` set at `dispatcher.py:1233` |
| Same pattern twice more | `ai/task_interpreter.py:570`, `ai/task_execution.py:167` |
| The established shape for a bounded new capability (facade-only Telegram access, `rpc_await` per call, dedicated error type, central provenance, no provider/prompt knowledge) | `services/history_service.py` (`HistoryError`, `MAX_HISTORY_MESSAGES = 1000`, `HISTORY_RPC_TIMEOUT_S = 5.0`, `_fetch_page` → `rpc_await`) |
| Media-relevant Telegram facts already reach a tool deterministically | `ToolContext.extra["reply_msg"]["media_type"]` and `["chat_id"]` / `["request_message_id"]` — `dispatcher.py:1233` |

**[FINDING]** Options assessed against that source:

- **Before the Dispatcher (pre-provider preprocessing):** not chosen. It would
  require new plumbing on the ordinary request object (a new `AIRequest` field
  or `metadata` key) and would put media work in the path of every request —
  contradicting the narrow-boundary precedent that explicit requests use a
  dedicated capability.
- **Inside an AI tool only:** insufficient by itself — a tool cannot download
  media without a bounded primitive, and cannot do LLM work without the service
  and provider-manager plumbing that `history_ai_service` already demonstrates.
- **Deterministic resolution + thin tool + service (chosen):** matches the
  existing fast-path/tool split, keeps the single execution authority, and
  satisfies the provider-independence and zero-context rules simultaneously.

**[RECOMMENDED]** The intended layering (nothing implemented):

```
Telegram media (reply target)
  → deterministic runtime resolution (request scope, no model)
  → NEW services-layer media capability   (bounded download → validate → resolve → normalize)
  → controlled normalized representation (text/structured data)
  → thin registered AI tool
  → existing ProviderManager.chat(...)  ← the owner's SELECTED provider, unchanged
  → ToolResult → existing delivery
```

The AI must never receive unrestricted Telegram RPC, filesystem, shell, HTTP or
SQL access; every media processor stays under explicit application control.

---

## 9. Timeout / Resource Limits

**[CURRENT]** Exact existing limits that a media path must respect:

| Boundary | Value | Source |
|---|---|---|
| AI slot acquisition | 60 s | `ai_unified.py:62` `_AI_TIMEOUT` |
| **AI execution envelope** | **240 s** | `ai_unified.py:71` `_AI_EXECUTE_TIMEOUT` → `AIRequest.timeout_s`, enforced by `asyncio.wait_for(engine.execute(...))` |
| AI request concurrency | 4 | `ai_unified.py:72` `_AI_MAX_CONCURRENCY` (semaphore) |
| Handler RPC helper | 30 s | `ai_unified.py:73` `_RPC_T` |
| Tool rounds per request | 3 | `dispatcher.py:58` `MAX_TOOL_ROUNDS` |
| Tools per turn | 5 | `ai/tools/executor.py` `MAX_TOOLS_PER_TURN` |
| Generic tool timeout | 10 s (exempt when `tool.long_running`) | `ai/tools/executor.py` `TOOL_TIMEOUT_SECONDS` |
| Provider HTTP | 30 s, 3 retries | `providers/base/config.py` `ProviderConfig.timeout`, `retry_count` |
| Facade short calls | 30 s | `telegram_api/messages.py` `_SHORT_CALL_TIMEOUT` |
| History page fetch | 5 s | `services/history_service.py` `HISTORY_RPC_TIMEOUT_S` |
| Bounded Telegram snapshot | 3 s | `conversation/telegram_context.py` `FETCH_TIMEOUT_S` |
| **Media download** | **none** | both paths (§5) |
| Media size | 50 MB default, 1..500 configurable | `services/settings_service.py:69`, `:113` |
| Bounded large scan precedent | 1000 messages | `services/delete_service.py` `_MAX_DELETE_SCAN_MESSAGES`; `services/history_service.py` `MAX_HISTORY_MESSAGES` |
| History AI operation budget (pacing/deadline math precedent) | `DEFAULT_ENVELOPE_S = 240`, `MAP_CONCURRENCY = 2`, `MAX_CALL_SPACING_S = 5.0`, `ASSUMED_CALL_LATENCY_S = 30`, `call_timeout = min(safety ceiling, time left)` | `services/history_ai_service.py` |
| Prompt budget | 8500 tokens total / 1000 output | `ai/prompt/budget.py` `DEFAULT_MAX_TOTAL_TOKENS`, `DEFAULT_MAX_OUTPUT_TOKENS`; `AI_MASTER_DESIGN.md` §28.6 |
| Process | 512 MB RAM, 0.1 shared vCPU | `AI_MASTER_DESIGN.md` §28.4 |
| AI Telegram reads | ≤ 60 calls/hour (budget, not enforced) | `AI_MASTER_DESIGN.md` §28.3 |

**[INFERENCE FROM SOURCE]** Per media type: **Photo / small document / short
voice** fit the existing 240 s envelope comfortably once the download itself is
bounded. **Voice/audio content analysis** is blocked by the absence of any
speech-to-text capability (§10), not by timeouts. **Video** is the worst case on
every axis — a 50 MB download with no extraction tool, no frame sampler and no
ffmpeg, inside a 240 s envelope shared with the LLM call. **Sticker/GIF**
bytes are obtainable but nothing can decode WEBP/TGS.

---

## 10. Dependencies / Runtime Constraints

**[CURRENT]**

- `requirements.txt`: `telethon==1.34.0`, `fastapi==0.111.0`,
  `uvicorn[standard]==0.29.0`, `supabase==2.4.2`, `aiofiles==23.2.1`,
  `httpx==0.27.0`, `tzdata==2026.3`. Nothing else.
- Installed venv confirms no image/audio/video/OCR/document library is present
  (no Pillow, numpy, opencv, pytesseract, whisper, pypdf, python-magic).
- A target grep (`ffmpeg|pytesseract|import PIL|whisper|opencv|pypdf`) over
  `backend/` returns **no** matches (only unrelated words such as "magic
  numbers" in a docstring and "whisper" inside an unrelated Ghost-Room string).
- Python **3.11.7** (`render.yaml` `PYTHON_VERSION`), deployed via
  `render.yaml` (`type: web`, `startCommand: python -m backend.main`,
  `healthCheckPath: /health`) and `Procfile` (`web: python -m backend.main`).
- **[INFERENCE FROM SOURCE]** The deploy target is a Python web service on
  Render, so Python dependencies can be added through `requirements.txt`;
  however `AI_MASTER_DESIGN.md` §28.4 caps the whole process at 512 MB / 0.1
  shared vCPU, which rules out heavy native stacks.
- **[FINDING]** `ffmpeg` and `tesseract` are **system binaries** —
  `requirements.txt` cannot provide them. Any capability requiring them is
  therefore not deliverable by dependency declaration alone.
- **[CURRENT] Documented non-goals that a media phase would contradict:**
  `AI_MASTER_DESIGN.md` §18(4) "Voice/audio input. The AI does not process voice
  messages as input." and §18(5) "Image understanding. The AI does not analyze
  images."; §20.1 sketches vision only as a future idea. **[FINDING]** A media
  phase must update these non-goals in the same commit that changes the behavior.

**Research context (not re-verified here, not permission to implement):** a
separate web-research phase established the provider-independent strategy
(local/open-source processing as the durable baseline, hosted permanently-free
services as optional specialist/fallback routes, media normalized *upstream* of
the selected LLM, no account/key farming or quota abuse, strictly *permanent*
free tiers only). **[UNKNOWN]** Nothing in the source confirms any specific
provider's free tier, and no such claim may be treated as verified by this
document.

---

## 11. Verified Blockers

Only blockers proven by source are listed.

1. **No working multimodal route exists.** `BaseProvider.vision` returns
   `NOT_IMPLEMENTED` (`contract.py:104`); `OpenAICompatProvider.vision` returns
   `NOT_IMPLEMENTED` despite `supports_images=True` (`openai_compat.py:253`,
   `:40`); `GeminiProvider` declares `supports_images=True` (`gemini.py:36`) and
   defines **no** `vision`; `grep -rn "\.vision("` finds **no** production call
   site. Therefore media bytes **cannot** reach any model today. *(True image/
   audio understanding requires implementing `vision()` per adapter — a
   provider-abstraction change the handoff explicitly limits.)*
2. **`ProviderManager.vision` is broken and off-policy anyway.** It is
   synchronous (`manager.py:308`) while the adapters' `vision` is `async def`
   (`openai_compat.py:253`), so it receives a coroutine and falls into
   `_fallback_vision` (`manager.py:1108`); and it selects via
   `_get_healthy_provider()` (`manager.py:563`), **ignoring the owner's active
   provider**, which would violate the "do not switch the user's provider"
   requirement. It must not be used.
3. **Every media download is unbounded.** `save_service.py:439` and
   `telegram_api/media.py:32` impose no timeout; a stalled transfer can hold the
   operation indefinitely.
4. **Media-only messages cannot activate the AI.** `ai_unified_handler` returns
   when `raw_text` is empty (`ai_unified.py:924`–`925`), so a caption-less photo
   starts no request; and the reply path requires an existing reply
   (`_extract_reply_context`, `ai_unified.py:425` region). *Requires a product
   decision, not a code discovery.*
5. **No extraction dependencies.** No OCR, STT, document, image or video
   library is declared or installed; no ffmpeg/tesseract binary can arrive via
   `requirements.txt`.
6. **The facade's message dicts carry `has_media` only** — no mime, size, or
   type. Media metadata for analysis must come from Telethon objects
   (`classify_message`) or a new facade primitive, never from
   `serialize_message` (`telegram_api/_helpers.py`).
7. **Telegram/conversational context is injected by default.** `reply_context`
   and the 10-message window are attached unconditionally for any reply-shaped
   request (`ai_unified.py:581` onward), which is exactly the shape a media
   request takes (§7.1).

---

## 12. Implementation Readiness

### Verdict: **GO WITH REQUIRED PREWORK**

Why:

- **GO** — the seams are real and singular: one activation handler, one request
  object, one dispatcher, one provider mesh, one tool-execution authority, one
  existing precedent (`history_service` + `history_ai_service` + thin tool +
  `extra["provider_manager"].chat`) for a bounded services-layer capability that
  performs its own LLM call through the selected provider. Media detection
  already exists (`ai/media.py::classify_message`) and media-relevant Telethon
  facts already reach the request scope (`ToolContext.extra["reply_msg"]`).
- **REQUIRED PREWORK** — three things must land *before* any media capability is
  useful: (1) a **bounded** download primitive (blocker 3); (2) a decision on
  the media-only activation path (blocker 4); (3) the normalized-representation
  contract and the explicit gating of `reply_context` / `telegram_context` for
  media calls (blocker 7), because the text-normalized route is the only one
  that satisfies provider-independence today.
- **NOT BLOCKED** — nothing requires a second client, scheduler, executor,
  provider abstraction, table or background loop.

**Explicitly out of reach for M1:** native image/audio/video understanding
(blocker 1), speech-to-text, OCR, document parsing, frame extraction (blockers
1, 5).

---

## 13. Exact Minimal Implementation Surface

**[RECOMMENDED]** — expected to require changes in the implementation phase.
Files listed here are those the analysis *proves* must change or be created;
everything inspected but not required is excluded and named in §14.

**Likely runtime files**

| File | Why it must change | Confidence |
|---|---|---|
| `backend/services/media_service.py` | **new** — the bounded capability: download via the facade under a timeout, size/type validation, media resolution, normalized representation, and the LLM call through the request-scoped provider manager | required |
| `backend/telegram_api/media.py` | **required for safety** — the download must be bounded (`rpc_await` / `guarded_await`) and size-guarded; today it is unbounded (blocker 3) | required |
| `backend/ai/tools/media.py` | **new** — thin tool over the service; no Telegram retrieval or provider logic inside it | required |
| `backend/ai/tools/registry.py` | register the new tool in `create_default_registry` | required |
| `backend/ai/media.py` | only if the normalized record needs more than `classify_message` yields (today: type, mime, size, filename, caption, text) | conditional |
| `backend/ai/actions.py` | only if an unambiguous media phrasing is defined; must not widen the existing command vocabulary | conditional |
| `backend/ai/engine/dispatcher.py` | only if a media-only route needs a new `ToolContext.extra` key (`chat_id`, `request_message_id`, `provider_manager`, `reply_msg` already exist) or a fast-path guard | conditional |
| `backend/bot/handlers/ai_unified.py` | only if caption-less media must activate the AI (blocker 4) — otherwise untouched | conditional |
| `backend/services/settings_service.py` | only if per-type size limits are introduced; the existing 50 MB setting may suffice | conditional |
| `AI_MASTER_DESIGN.md` | §18(4)/(5) media non-goals would need updating in the same commit as any behavior change | conditional |

**Likely test files**

| File | Content |
|---|---|
| `tests/test_<media>.py` (**new**) | bounded download (timeout enforced), size refusal, non-empty validation, temp cleanup, normalized-record shape, media-type resolution per type, tool registration, and **no Telegram context in the media call's message list** |

**Likely configuration / dependency files**

| File | Note |
|---|---|
| `requirements.txt` | only if a *pure-lightweight* dependency is genuinely required; `ffmpeg` / `tesseract` cannot be supplied this way (§10) |
| `render.yaml` / `Procfile` | **no change expected** — no new process, service or worker |

---

## 14. Files / Systems That Must Remain Untouched

Protect explicitly (these are outside the media boundary and were only inspected):

- **Save / Saved Items** — `backend/services/save_service.py`, `retrieve_service.py`, `backend/ai/tools/save.py`, `retrieve_save.py`, `backend/bot/handlers/{save,retrieve}.py`, `backend/db/client.py`. Save/Deep Save is completed and must not be altered; its download pattern is a *reference*, not a target.
- **RuntimeSupervisor** — `backend/runtime/*` (supervisor, heartbeat, keepalive, failsafe, task_guard, operation_watchdog's semantics). No second supervisor, client, loop, or recovery authority.
- **Provider selection / fallback architecture** — `backend/ai/providers/**` and `providers/manager/**`. **Unless** a minimal interface extension is proven necessary, the selection, scoring, retry, cooldown and fallback semantics stay byte-identical. This document's verdict is that no such extension is needed for M1 (§6, §12).
- **History AI** — `backend/services/history_service.py`, `history_ai_service.py`, `backend/ai/tools/history_ai.py`.
- **Task system / Taskloom** — `backend/ai/task_*.py`, `backend/bot/handlers/task*.py`, `task_scheduler.py`, `task_execution.py`.
- **Supabase / schema** — `supabase/**`, `sql/**`, `DATABASE_ARCHITECTURE.md`. No table, column, migration, RLS or SQL change is required by anything in this document.
- **Bounded conversational snapshot** — `backend/ai/conversation/telegram_context.py`. Its bounds (10 messages / 200 chars / 1500 total / 4 sender resolves / 3 s) must **not** be enlarged to serve media.
- **Unrelated handlers / features** — `backend/bot/handlers/*` (bio, username, delete, discover, database, misc, ghost, taskloom), `backend/helper/**` (panels/presentation), `backend/ai/tools/delivery.py`, `backend/profile/**`, `backend/bio/**`, `backend/username/**`.
- **Provenance authority** — `backend/ai/context/provenance.py`. Reuse the helpers; never add a second predicate, regex, or marker check.

---

## 15. Recommended M1 Scope

**[RECOMMENDED]** — the smallest sensible first phase, derived from §6/§11/§12.

**M1 delivers**

1. **One bounded media primitive** — a download through the existing
   `backend/telegram_api` facade, wrapped in the existing bounded-await helper
   (`rpc_await` or `guarded_await`), enforcing the existing
   `settings_service.max_deep_save_mb()` limit before download, writing to a
   `tempfile.mkdtemp` directory, validating existence + non-empty afterwards,
   and removing the directory in a `finally` block (the Deep Save pattern).
2. **One new services-layer capability** (`backend/services/media_service.py`),
   structured after `services/history_service.py`: facade-only Telegram access,
   bounded RPC, a dedicated error type, no Telethon objects escaping, and **no
   prompt/provider knowledge beyond one `ProviderManager.chat(...)` call**.
3. **One normalized representation** — media type, mime, size, filename,
   caption, source chat/message ids, and a text/structured payload that the
   application (not the model) treats as data. Never raw bytes to the model.
4. **One thin registered tool**, and the LLM step performed inside the service
   through `ToolContext.extra["provider_manager"].chat(...)` — the
   `history_ai_service` precedent. **Never** `ProviderManager.vision`.
5. **Explicit zero-context enforcement** for the media call: the message list is
   built by the media capability (or the request's `reply_context` /
   `telegram_context` are gated off for media requests) so no Telegram
   conversation reaches the model.
6. **Deterministic resolution first**: the reply target and its media are
   resolved from the request scope before any model is consulted; only the
   validated media reference and the user's own request text travel forward.

**M1 media coverage** — supported: Photo (metadata + caption/text normalization;
the LLM works on text), Document/Text, Audio/Voice **metadata only**,
Sticker/Animation **labels only**.

**M1 explicitly deferred** — native vision on image bytes; speech-to-text;
OCR; document parsing; video content analysis or frame sampling;
sticker/TGS decoding. Each is deferred because of a source-proven blocker
(§11), not because of preference.

**Not in M1 under any framing** — a second Telegram client, a second AI engine,
a second tool executor, a scheduler, a background worker, a new table, a
provider-interface change, or any Save/Saved-Items change.

---

## 16. Risks and Unknowns

**VERIFIED FROM SOURCE** (readable in the cited file)

- `vision()` is declared in `BaseProvider` and **implemented nowhere**;
  `grep -rn "\.vision("` finds no production caller (§6).
- `ProviderManager.vision` is `def` while the adapters' `vision` is `async def`,
  and it selects through `_get_healthy_provider()` rather than the active
  provider (`manager.py:308`, `:563`, `openai_compat.py:253`).
- Both media-download paths are unbounded (`save_service.py:439`,
  `telegram_api/media.py:32`).
- Prompt/message assembly is text-only (`dispatcher.py:1952`,
  `openai_compat.py:65`–`100`).
- `reply_context` and `telegram_context` are attached unconditionally for
  reply-shaped requests (`ai_unified.py:581` onward) and rendered into the prompt
  (`prompt/builder.py::_render_conversation_state`).
- `ai_unified_handler` returns on empty `raw_text` (`ai_unified.py:924`–`925`).
- Limits and their exact values (§9), and the absent dependencies (§10).
- `history_ai_service.py:453` is a real, working precedent for a service calling
  `manager.chat(...)` through the request-scoped provider manager.

**INFERENCE FROM SOURCE**

- That a text-normalized media route is the **only** provider-independent route
  available today (§6) — it follows from the dead `vision()` seam plus the
  active-provider-ordering requirement, but it was not executed.
- That gating `reply_context`/`telegram_context` off for media requests, or
  building the media call's own message list, is the minimal way to satisfy the
  zero-context rule (§7.2).
- Per-type envelope feasibility (Photo/document/voice likely; video likely not)
  and the exact M1 file surface (§9, §13).
- That a pure-Python/lightweight dependency would be practical on Render
  (§10), and that ffmpeg/tesseract cannot be delivered by `requirements.txt`.

**UNKNOWN / REQUIRES IMPLEMENTATION DECISION**

1. Whether a caption-less media message should activate the AI at all, and with
   what prompt (blocker 4) — a product decision with no source answer.
2. Whether the bounded 10-message Telegram window should be *included* or
   *excluded* for media requests. The handoff rule excludes it; the current
   source includes it for every reply-shaped request. The decision must be made
   explicitly and documented.
3. Where the owner-facing result is presented, and whether it reuses the
   existing status/verbatim-read presentation (`_STATUS_LABELS`,
   `_VERBATIM_READ_TOOLS`) or a new label.
4. Which media types M1 must *actually* answer (vs. label), and whether an
   LLM-only analysis of a text normalization is sufficient for the owner's
   intent for photos.
5. Whether a normalized representation is persisted anywhere. No storage path is
   proposed here; any persistence would touch Supabase and is therefore out of
   scope for M1.
6. Real Telegram download latency and memory for a 50 MB asset inside the 240 s
   envelope — **not measured** (no live access in this investigation).
7. Whether any hosted permanently-free processing service is genuinely required
   for M1. The research phase proposed them as optional specialist/fallback
   routes; no source evidence here establishes a need.

---

## 17. Investigation Conclusion

**[CURRENT]** The repository is architecturally **ready for a bounded,
provider-independent, text-normalized media layer** and **not** ready for native
multimodal input. There is exactly one AI entry handler, one request object, one
dispatcher, one provider mesh and one tool-execution authority; media *detection*
already exists (`backend/ai/media.py::classify_message`) and media-relevant
Telethon facts already reach the request scope
(`Dispatcher._build_tool_context` → `ToolContext.extra["reply_msg"]`). The one
existing large-capability precedent (`services/history_service.py` +
`history_ai_service.py` + a thin tool + `extra["provider_manager"].chat(...)`)
demonstrates precisely how a services-layer capability performs its own LLM call
through the owner's **selected** provider.

**[FINDING]** Three things stand between the current state and a usable media
capability: media downloads are unbounded on both existing paths; image/audio
`vision()` support is declared but implemented nowhere and uncalled (and the
manager's `vision` is both broken and off-policy); and Telegram
reply/window context is injected into the model by default for exactly the
request shape a media request takes.

**[RECOMMENDED]** The correct M1 boundary is therefore: **deterministic
Telegram-side resolution outside the AI → a new bounded services-layer media
capability over the existing `backend/telegram_api` facade → a controlled
normalized representation → a thin registered tool → the existing
`ProviderManager.chat` path**, so the owner's selected chat provider and its
routing, retry and fallback semantics are untouched, and the model receives only
the owner's explicit request plus application-prepared media data — never
Telegram conversation.

**Verdict: GO WITH REQUIRED PREWORK** (§12). No implementation was performed.

---

## Validation Status

| Item | Status |
|---|---|
| Scope honored | only `INVESTIGATION.md` modified |
| Previous (Telegram-history) investigation | **fully replaced** — not preserved, appended, merged, or referenced as current |
| Production code / tests / dependencies / configuration | **untouched** |
| Supabase / schema / migrations / `DATABASE_ARCHITECTURE.md` / Save | **untouched** |
| Provider architecture | **untouched** |
| Implementation performed | **none** — investigation + documentation only |
| New abstraction created | **none** — every `media_service.py` / tool / registry entry named in §13 is a recommendation, not a file |
| Evidence | every material claim cites an exact path + symbol/line (§3–§10) |
| Tests run | none — no code changed |
| Live Telegram / Supabase / Render verification | **not performed** (out of scope) |
| Fabricated commits / pushes | none claimed |

**Proven from source:** the text-only prompt and provider path and its exact
seams (§3, §6); the media detection/metadata/download inventory per type (§4);
the unbounded download boundary and the existing size/temp/cleanup controls
(§5); the dead `vision()` seam and the manager's inactive-provider selection
(§6); every context-injection path into the model and the unconditional
attachment for reply-shaped requests (§7.1); the services-layer precedent that
performs its own provider call through the *selected* provider (§6, §8); the
exact limit values (§9); and the absence of OCR/STT/document/video dependencies
and system binaries (§10).

**Not proven / not measured:** real Telegram latency, memory and RPC cost for a
50 MB asset inside the 240 s envelope; whether any particular normalization
quality suffices for the owner's intent; and the product decisions listed in
§16.

---

**No fix or feature was implemented. Only this document was modified.**
