# Media Processing — Architectural Investigation

> **Investigation only — no code is changed by this document.** This is the
> canonical current-state investigation for the Media Processing layer **and for
> the source lineage of the STT text the owner reads in Telegram** (§18). It
> **replaces the previous `INVESTIGATION.md` entirely**; no earlier content
> (including the Telegram-history investigation) is preserved, merged, appended,
> or referenced as a historical section. Producing this document modified
> **only** `INVESTIGATION.md` — no production code, tests,
> `IMPLEMENTATION_REPORT.md`, schema, migrations, configuration, dependency,
> presentation, delivery, context-retrieval, Save/Saved-Items, provider or
> runtime file.
>
> The media capability this document originally specified as *required prework*
> has since **landed** in the current tree (§4, §5, §8, §13, §15): the bounded
> download, the `backend/services/media_service.py` boundary, the Gemini OCR/STT
> engines and the `backend/services/media_ai_service.py` model step. The newest
> verification recorded here is the **STT UI-text lineage** (§18): which stage
> actually produces the transcription text shown to the owner.

## 1. Investigation Metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Audited HEAD | `801f8dbe61a3185d1eb8d87aa35ede12ff75166b` (short `801f8db`, `docs: record the Gemini STT language-contract fix`) |
| Investigation date | 2026-09-16 |
| Status | **Investigation only. No code was changed while producing this document.** |
| Scope | (a) At which existing architectural boundary a controlled Media Processing layer sits upstream of the owner's **currently selected** LLM provider — without changing that provider and without leaking Telegram/conversational context to the model; and (b) **the verified source lineage of the STT text the owner reads in Telegram** for a replied Voice note (§18). |
| Question | How is Telegram media (photo, voice, audio, document, video, sticker, GIF) resolved, downloaded, validated and normalized into a controlled representation that the **currently selected** chat provider consumes as ordinary input — and which stage of that chain produces the text the owner actually sees? |
| Media-layer verdict | The verdict at `893d3f4` was **GO WITH REQUIRED PREWORK**; that prework has since **landed** (§11, §13, §15). No native multimodal provider path is wired — `vision()` is still declared-but-unreachable across every adapter (§6) — so media is normalized to **text upstream** instead, on a provider-neutral path. |
| STT-lineage verdict | **RESOLVED FOR THE WRAPPER; ONE RUNTIME GAP REMAINS** (§18.8): the owner-visible text is **neither** the engine transcript **nor** locally formatted — it is a **second model's output**. The exact value the STT engine returned on the Persian live run is still **not provable from source**. |
| Files changed by this investigation | only `INVESTIGATION.md` |
| Classification labels | **[CURRENT]** implemented behavior verified in source at the audited HEAD (`801f8db`) · **[FINDING]** conclusion derived from that source (evidence cited) · **[RECOMMENDED]** future proposal — **nothing in those sections is implemented** · **[UNKNOWN]** requires an implementation-phase decision |

Evidence-strength tags used inline where a claim is not directly readable in
source: **VERIFIED FROM SOURCE**, **INFERENCE FROM SOURCE**, **UNKNOWN /
REQUIRES IMPLEMENTATION DECISION**. §18 uses the equivalent canonical triple
**PROVEN FROM SOURCE** / **INFERRED FROM CONTROL FLOW** / **NOT PROVABLE WITHOUT
LIVE TRACE**.

---

## 2. Investigation Method

Read-only tracing from the Telegram activation handler outward to Telethon and
back through the provider mesh, plus the services, prompt, context, tool and
configuration layers. Areas inspected (all at the audited HEAD, all read-only):

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
| **STT UI-text lineage (this audit)** | `backend/services/gemini_media_engine.py`, `backend/services/media_service.py`, `backend/services/media_ai_service.py`, `backend/ai/engine/dispatcher.py` (`_media_target`, `_try_media_analysis`, `_build_fast_path_result`), `backend/ai/tools/delivery.py` (full), `backend/ai/context/provenance.py`, `backend/ai/media.py`, `backend/ai/session/request.py`, `backend/bot/handlers/ai_unified.py` (`_extract_reply_context`, `_execute_ai`, the delivery call), the media test files, and the provider layer (grep for any script rewriting) |

Method notes: no live Telegram, Supabase, Render or network access was used; no
package was installed by this work; no test was executed while producing this
document. Where a conclusion depends on a symbol's presence rather than on
executed behavior, that is stated as **[FINDING]** with the exact symbol and file
cited. Repository-wide greps were targeted (`vision`, `download_media`,
`supports_images`, `classify_message`, `ffmpeg`, OCR/STT/multimodal keywords) —
**except the two that decide §18, which were exhaustive over the tracked tree**:
the owner-visible wrapper phrase (`محتوای صوتی` / `ارسالی` → zero hits in any
code file; the only hit in the whole tracked tree is this document's own §2
method note) and the guillemet pair `«`/`»` (hits only in unrelated modules and
their tests: `ai/confirmation.py`, `ai/preparation_policy.py`,
`services/ghost_seen_v2.py`, `tests/test_63_ghost_seen_v2_stage8.py`,
`tests/test_preparation_policy_source.py`, `tests/test_confirmation_roundtrip.py`;
none of them executes on the media path). No runtime value could be observed: this environment has
no Telegram session, no provider credential and no live traffic, which is why
§18 ends with an explicit runtime gap rather than a conclusion.

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
| Media **download** over the typed facade | `backend/telegram_api/media.py::download_media` → `guarded_await(client.download_media(...))` | **bounded** — `MEDIA_DOWNLOAD_TIMEOUT_S = 120.0`, caller may only tighten (`_effective_timeout`), `TelegramTimeoutError` on expiry; size is the caller's responsibility |
| **Media boundary** — deterministic resolve → bounded transfer → validate → extract → normalize → cleanup | `backend/services/media_service.py` (`resolve_media_message`, `analyze_media`) | **the single media-processing boundary** |
| Content extraction (OCR / STT / text, PDF, DOCX) | `media_service._extract_image_content`, `_extract_audio_content`, `_extract_content`; engines `backend/services/gemini_media_engine.py::GeminiMediaEngine.recognize` / `.transcribe` | present — image→OCR and audio→STT **only when an engine is provisioned**; text / PDF (`pypdf`) / DOCX (stdlib XML) otherwise |
| Media analysis record — provider-independent, Telethon-free | `media_service.MediaAnalysis` + `MediaAnalysis.as_context_text()` | present — the model-facing surface |
| Media → LLM delivery | `backend/services/media_ai_service.py::build_media_messages` / `answer_media_request` → `ProviderManager.chat(messages, tools=[])` | present — provider-neutral, plain-string path |
| Engine provisioning, optional and fail-closed | `gemini_media_engine.provision_gemini_media_engines()` ← `backend/runtime/supervisor.py:278` | present — an unconfigured runtime leaves both seams empty and media keeps failing closed |
| Deep Save download → validate → re-upload | `backend/services/save_service.py::execute_save` | unchanged and protected (§14); its own `client.download_media` call is **still unbounded** (§5) |
| MIME→media-type map, extension map, byte formatter | `save_service.detect_media_type`, `_MIME_EXT`, `_format_bytes` | reusable as-is |
| Filename extraction / generated names | `save_service.extract_file_name`, `generate_filename` | reusable as-is |
| Serialized message media facts | `telegram_api/_helpers.py::serialize_message` (`has_media` at `:89`) | **`has_media: bool` only** — no mime/size/type reach facade dicts; the media boundary reads Telethon objects via `classify_message` instead |
| Upload attribute preservation | `save_service._upload_kwargs_for_media` | unrelated to analysis |

Per media type, distinguishing detection / metadata / download / extraction /
analysis / current LLM delivery:

| Type | Detection | Metadata classification | Download | Content extraction | Analysis | Reaches the selected LLM |
|---|---|---|---|---|---|---|
| **Photo** | `MessageMediaPhoto` (`media.py`) → `"Photo"`, forced `image/jpeg`, size from `photo.sizes[-1].size` | `MediaInfo.media_type/mime_type/file_size` | media boundary, bounded | OCR text when the OCR engine is provisioned; otherwise honest `UNSUPPORTED` | `MediaAnalysis` | **yes — as text** via `as_context_text()`; never as image bytes |
| **Voice** | `DocumentAttributeAudio.voice=True` → `"Voice"` | mime, size, filename | media boundary, bounded | STT transcript when the STT engine is provisioned; otherwise `UNSUPPORTED` | `MediaAnalysis` | **yes — as text** (§18) |
| **Audio** | `DocumentAttributeAudio.voice=False` → `"Audio"` | mime, size, filename | media boundary, bounded | same STT seam as Voice | `MediaAnalysis` | **yes — as text** |
| **Document** | `MessageMediaDocument` fallback → `"Document"` | mime, size, filename (`DocumentAttributeFilename`) | media boundary, bounded | text for `text/*` and text-shaped MIME; PDF via `pypdf`; DOCX via stdlib XML; other containers `UNSUPPORTED` | `MediaAnalysis` | **yes — as text** when extractable |
| **Video** | `DocumentAttributeVideo` → `"Video"` | mime, size, filename | media boundary, bounded | none — no ffmpeg, no frame sampler, no STT on the container | honest `UNSUPPORTED` | **no** — deterministic unsupported answer |
| **Sticker** | `DocumentAttributeSticker` → `"Sticker"` (WEBP/TGS) | mime, size | media boundary, bounded | none — no WEBP/TGS decoder | honest `UNSUPPORTED` | **no** — deterministic unsupported answer |
| **Animation/GIF** | `DocumentAttributeAnimated` → `"Animation"`; `mime=="image/gif"` → `"GIF"` | mime, size, filename | media boundary, bounded | none | honest `UNSUPPORTED` | **no** — deterministic unsupported answer |
| **WebPage** | `MessageMediaWebPage` → `"WebPage"`, `text/html` (`media.py`) | type + mime | **never** — not in `DOWNLOADABLE_MEDIA_TYPES` | never | none | label only; the media route does not resolve it |
| Contact / Poll / Location | `MessageMediaContact` / `Poll` / `Geo` | type only | **never** — not in `DOWNLOADABLE_MEDIA_TYPES` | never | none | label only; the media route does not resolve it |

**[FINDING]** Media is *detected and labelled* everywhere (`classify_message`),
and a deterministically resolved target is now *transferred and normalized* by
the `media_service` boundary — whose transferable taxonomy is exactly
`DOWNLOADABLE_MEDIA_TYPES = {Photo, Voice, Audio, Document, Video, Sticker,
Animation, GIF}` (`media_service.py:131`), with `WebPage`/`Contact`/`Poll`/
`Location`/`Unknown` never fetched. **No media byte ever reaches a provider**:
the only media-derived value that enters a model message is text produced by
`MediaAnalysis.as_context_text()` (§18.6, stage C→D). A type with no engine or
extractor is not transferred at all and yields an honest `UNSUPPORTED` result.

---

## 5. Telegram Media Download Boundary

**[CURRENT]** Two code paths can download media. **The media boundary's path is
bounded; Deep Save's own path is not** (and Deep Save is protected scope, §14):

1. **`backend/telegram_api/media.py::download_media`** — the client call is now
   wrapped in `guarded_await(..., timeout=_effective_timeout(timeout))`, whose
   module ceiling is `MEDIA_DOWNLOAD_TIMEOUT_S = 120.0`. A caller can only ask for
   **less** (`min(value, ceiling)`, fail-closed on a non-positive or unparsable
   value), and expiry logs `TELEGRAM_MEDIA_TIMEOUT` and raises
   `TelegramTimeoutError`. This is the one primitive the media boundary uses, and
   `media_service.MEDIA_DOWNLOAD_TIMEOUT_S` (`media_service.py:120`) **is that
   same constant** — not a second, contradictable bound.
2. **`backend/services/save_service.py:439`** — Deep Save still calls
   `await client.download_media(reply_msg, file=tmp_path)` **directly**: no
   `rpc_await`, no `guarded_await`, no timeout. The media phase did not change it.

Analysis of the existing boundary:

| Property | Current state | Evidence |
|---|---|---|
| Download function | media boundary: `TelegramAPI.download_media` → `guarded_await(client.download_media(...))`. Deep Save: raw `client.download_media(msg, file=path)` | `telegram_api/media.py`; `save_service.py:439`; `telegram_api/api.py` |
| Timeout | media boundary **120 s ceiling** (`MEDIA_DOWNLOAD_TIMEOUT_S`), caller-tightenable, `TelegramTimeoutError` on expiry. Deep Save: **none** | `telegram_api/media.py` (`_effective_timeout`); `media_service.py:120`, `:454`; `save_service.py:439` |
| File-size limit | media boundary: declared size checked against `media_service.max_download_bytes()` = **the existing** `settings_service.max_deep_save_mb()` (default **50 MB**, validated range 1..500) **before** transfer, and the actual transferred size re-checked against it afterwards. Deep Save: declared-size pre-check only | `media_service.max_download_bytes`; `settings_service.py:69`, `:113`, `:281`; `save_service.py:396`–`399` |
| Post-download validation | media boundary: the returned path must exist, be non-empty and be within the size bound; Deep Save: `os.path.exists(tmp_path)` and `getsize > 0` only | `save_service.py:445`–`451` |
| MIME / type validation | media boundary: an extractability gate before transfer (`is_extractable_mime` / image / STT MIME sets) and container-signature corroboration before an engine runs (`_validate_audio_payload`). Deep Save: **none** — the MIME is whatever Telegram declares, and the filename is used only as the temp filename | `media_service.py` (`is_extractable_mime`, `is_image_mime`, `is_stt_mime`, `_validate_audio_payload`); `save_service.py` `_extract_source_media` |
| Temporary storage | media boundary: `tempfile.mkdtemp(prefix="lifeos_media_")` (`media_service.py:1522`); Deep Save: `tempfile.mkdtemp(prefix="lifeos_dl_")` — OS temp dir, not configurable, per-operation | `media_service.py:1522`; `save_service.py:435` |
| Cleanup | **both** remove the directory on every exit path: `shutil.rmtree(tmp_dir, ignore_errors=True)` in `finally` (media boundary `media_service.py:1586`) | `media_service.py:1586`; `save_service.py:465` |
| Event-loop behavior | non-blocking: `download_media` is awaited on the async Telethon client and streams to a path; CPU-bound extraction runs through `asyncio.to_thread` under a finite timeout | `media_service.py` `_run_ocr` / `_run_stt`; `save_service.py:439` |
| Error handling | media boundary raises `MediaError` carrying its failing **stage** (`MEDIA_STAGE_*`), surfaced by the dispatcher as a media failure identity; facade raises `TelegramAPIError` / `TelegramTimeoutError`; Deep Save catches broad `Exception` and returns an honest `"❌ Deep Save failed: …"` string | `media_service.py`; `dispatcher.py::_try_media_analysis`; `save_service.py:441`, `:455` |
| FloodWait | **no** FloodWait handling in either media path (the facade docstring in `telegram_api/__init__.py` claims FloodWait handling, but no media code implements it) | `telegram_api/media.py` |

**[FINDING]** The bounded-download gap this document originally recorded is
**closed for the media boundary**: the facade download is itself bounded by
`backend/runtime/operation_watchdog.py::guarded_await` (`media_service` and
`telegram_api/messages.py` share that primitive; `helper/rpc_timeout.py::rpc_await`
remains available). What is **still missing** is a bound on Deep Save's own
direct call — deliberately left unchanged, because Deep Save is protected scope.

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

**[CURRENT] The media layer ships exactly that route** (§18.6, stage D→E):
`backend/services/media_ai_service.py::_provider_call` calls
`await manager.chat(messages, tools=[])` with a message list it builds itself, so
the owner's selected chat provider — with its candidate ordering, retry
classification and cooldown — is untouched. `ProviderManager.vision` is **still
never called** (`grep -rn "\.vision("` over `backend/` finds no production caller)
and no adapter gained a `vision` implementation: media still reaches a model **as
text only**.

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

**[FINDING] The precise leak vector, and how the media route avoids it.** Items 2
and 4 are attached **unconditionally** inside `_execute_ai` for every
reply-shaped request (`ai_unified.py:581` onward) — and a media request *is*
reply-shaped, because media is reachable through a replied-to message or the
triggering message itself. Those two fields would therefore travel in the same
prompt as the media.

**[CURRENT] The media route never uses that prompt path.**
`Dispatcher._try_media_analysis` runs immediately after the deterministic fast
path and **returns before** `_build_context` / `_build_messages`, so items 1–7
cannot enter a media request, and `media_ai_service.build_media_messages` builds
the whole message list itself (a static system instruction + one user message).
The §7.2 boundary is therefore satisfied **for media, by construction**. The
unconditional attachment described above still governs every ordinary
(non-media) request, unchanged.

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

**[CURRENT]** The layering as it now exists. The originally recommended "thin
registered AI tool" became a **dispatcher media route plus a services-layer
capability**: nothing was added to the tool registry, and `ToolExecutor` is not
in this path.

```
Telegram media (deterministically resolved target)
  → Dispatcher._media_target                replied-to message, else the triggering message — no model, no text matching
  → backend/services/media_service.py       bounded download → validate → extract → normalize → cleanup
  → MediaAnalysis                           provider-independent, Telethon-free
  → backend/services/media_ai_service.py    builds its own system + user message list
  → ProviderManager.chat(messages, tools=[])  ← the owner's SELECTED provider, unchanged
  → EngineResult.response → delivery.process_output → Telegram
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
| **Media download** | **120 s ceiling** for the media boundary; **none** for Deep Save's own path | `telegram_api/media.py` `MEDIA_DOWNLOAD_TIMEOUT_S`; `media_service.py:120`; `save_service.py:439` (§5) |
| Media target resolution | 30 s | `media_service.MEDIA_RESOLVE_TIMEOUT_S` |
| OCR engine call | 45 s boundary / 30 s engine | `media_service.OCR_TIMEOUT_S`; `gemini_media_engine.OCR_TIMEOUT_S` |
| STT engine call | 60 s boundary / 40 s engine | `media_service.STT_TIMEOUT_S`; `gemini_media_engine.STT_TIMEOUT_S` |
| STT input payload | 20 MiB | `media_service.MAX_STT_INPUT_BYTES` |
| Extracted-text ceiling (OCR / STT / document share it) | `MAX_EXTRACTED_CHARS = DEFAULT_MAX_CONTEXT_TOKENS × 4` = **16 000 chars** | `media_service.py:163`–`165`, `:194`, `:232`; `ai/prompt/budget.py:29` |
| Media answer provider call | `min(120 s, caller envelope)`; never started with less than 20 s left | `media_ai_service.PROVIDER_CALL_SAFETY_TIMEOUT_S`, `media_call_timeout`, `MIN_PROVIDER_CALL_TIMEOUT_S` |
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
  `httpx==0.27.0`, `tzdata==2026.3`, `pypdf==6.18.1`. Nothing else.
- `pypdf` is the only media-adjacent library and it belongs to document text
  extraction (PDF); DOCX and text-shaped MIME need the standard library only. No
  image/audio/video/OCR library is declared or installed (no Pillow, numpy,
  opencv, pytesseract, whisper, python-magic).
- **Nothing links an OCR or STT library.** Both seams are served by the
  *remote* `GeminiMediaEngine` over the `httpx` stack the provider adapters
  already use, provisioned optionally from the repository's existing Gemini
  credential variables (`backend/services/gemini_media_engine.py`,
  `provision_gemini_media_engines` ← `backend/runtime/supervisor.py:278`).
- The engine's accepted containers are deliberately narrow: image `png`/`jpeg`/
  `webp` and audio `ogg`/`opus`/`wav`/`flac` only — no MP3, M4A, webm, AAC, BMP
  or GIF (`_GEMINI_IMAGE_MIME_TYPES`, `_GEMINI_AUDIO_MIME_TYPES`,
  `gemini_mime_type`).
- A target grep (`ffmpeg|pytesseract|import PIL|whisper|opencv`) over `backend/`
  still returns no functional match (only unrelated prose).
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

Only blockers proven by source are listed, each with its status at the audited
HEAD.

**Still open**

1. **No working multimodal route exists — and none is used.** `BaseProvider.vision`
   returns `NOT_IMPLEMENTED` (`contract.py:104`); `OpenAICompatProvider.vision`
   returns `NOT_IMPLEMENTED` despite `supports_images=True` (`openai_compat.py:253`,
   `:40`); `GeminiProvider` declares `supports_images=True` (`gemini.py:36`) and
   defines **no** `vision`; `grep -rn "\.vision("` finds **no** production call
   site. Media bytes therefore **cannot** reach any model, and the media layer
   deliberately keeps it that way: media reaches a model **as text** only
   (§18.6, stage C→D). *(True image/audio understanding would require implementing
   `vision()` per adapter — a provider-abstraction change explicitly out of scope.)*
2. **`ProviderManager.vision` is broken and off-policy anyway.** It is
   synchronous (`manager.py:308`) while the adapters' `vision` is `async def`
   (`openai_compat.py:253`), so it receives a coroutine and falls into
   `_fallback_vision` (`manager.py:1108`); and it selects via
   `_get_healthy_provider()` (`manager.py:563`), **ignoring the owner's active
   provider**, which would violate the "do not switch the user's provider"
   requirement. Unchanged, and still never called.
3. **Media-only messages cannot activate the AI.** `ai_unified_handler` returns
   when `raw_text` is empty, so a caption-less photo or voice note starts no
   request; and a reply-shaped request still needs the trigger word or a message
   already registered as an AI answer. Unchanged by the media phase — still a
   product decision, not a code discovery. The handler probes the triggering
   message's media type only on the **trigger mode** branch, so this guard is
   load-bearing for the media route.
4. **No local extraction stack.** No OCR, STT, document, image or video library
   is declared or installed, and no ffmpeg/tesseract binary can arrive through
   `requirements.txt` (§10). OCR and STT are therefore served **remotely and
   optionally**; a runtime without a Gemini credential keeps failing closed with
   an honest `UNSUPPORTED`/stage-tagged failure.

**Closed by the landed media phase**

5. **Media downloads are bounded on the media path.** `telegram_api/media.py`
   now wraps the transfer in `guarded_await` with a 120 s ceiling and raises
   `TelegramTimeoutError` on expiry (§5). *(Deep Save's own direct
   `client.download_media` call remains unbounded and is out of scope, §14.)*
6. **The media boundary does not depend on facade message dicts.**
   `serialize_message` still carries `has_media` only (`_helpers.py:89`), but the
   media boundary reads Telethon objects through `ai/media.py::classify_message`
   and passes nothing but `MediaAnalysis` onward.
7. **Telegram/conversational context no longer reaches a media request.**
   `reply_context` and the 10-message window are still attached unconditionally
   for ordinary reply-shaped requests, but the media route returns before
   prompt/context construction, so neither can enter a media request (§7.1).

---

## 12. Implementation Readiness

### Verdict: **GO — the required prework has landed**

The verdict at `893d3f4` was **GO WITH REQUIRED PREWORK** for exactly three
items. All three are present in source at the audited HEAD:

| Required prework | Status | Evidence |
|---|---|---|
| (1) A **bounded** download primitive | **landed** | `telegram_api/media.py` `MEDIA_DOWNLOAD_TIMEOUT_S = 120.0` through `guarded_await`, caller-tightenable, `TelegramTimeoutError` on expiry (§5) |
| (2) A decision on the media activation path | **landed, conservatively** | caption-less media still does **not** activate; the triggering message's media type or a replied-to media message does (§11.3) |
| (3) A normalized representation + zero-context enforcement | **landed** | `media_service.MediaAnalysis` + `media_ai_service.build_media_messages`; the media route returns before context/prompt construction (§7.1, §8) |

- **GO** — the seams are real and singular: one activation handler, one request
  object, one dispatcher, one provider mesh, one media boundary, one download
  primitive. Provider independence is met by normalizing media to **text
  upstream** and sending it through `ProviderManager.chat(...)`, so the owner's
  selected chat provider is never switched (§6).
- **STILL NOT COVERED** — native image/audio/video input (blockers 1–2, out of
  scope by design); OCR/STT **quality**, which depends on a remotely provisioned
  model and is measured only by the owner's next live request (§18.9); document
  formats beyond text/PDF/DOCX; and video, sticker and animation/GIF content
  extraction (blocker 4).
- **NOT BLOCKED** — nothing requires a second client, scheduler, executor,
  provider abstraction, table or background loop.

---

## 13. Exact Minimal Implementation Surface

**[SUPERSEDED — historical plan.]** This section is the original surface estimate
and is retained as the record of what the prework *was* expected to touch; it is
**no longer a statement of what remains to be done**. The delivered shape differs
in one decisive way: the media route landed **inside the dispatcher**
(`dispatcher._try_media_analysis` → `services/media_ai_service` →
`services/media_service`), **not** as a registered AI tool —
`backend/ai/tools/media.py` does not exist at the audited HEAD and is not
reachable from `backend/ai/tools/registry.py`. The `backend/services/media_service.py`
and `backend/telegram_api/media.py` rows below are **landed** (§4, §5, §8, §12).

**[RECOMMENDED]** — expected to require changes in the implementation phase.
Files listed here are those the analysis *proves* must change or be created;
everything inspected but not required is excluded and named in §14.

**Likely runtime files**

| File | Why it must change | Confidence |
|---|---|---|
| `backend/services/media_service.py` | **new** — the bounded capability: download via the facade under a timeout, size/type validation, media resolution, normalized representation, and the LLM call through the request-scoped provider manager | required |
| `backend/telegram_api/media.py` | **required for safety** — the download must be bounded (`rpc_await` / `guarded_await`) and size-guarded; it was unbounded at `893d3f4` and is now bounded (§5, §11.5) | required |
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

**[SUPERSEDED — historical plan.]** This section describes the phase as it was
scoped *before* it landed; where it conflicts with §4/§5/§8/§12, those sections
are current. Two of its statements are contradicted by the audited HEAD:
(i) the LLM step **and** the route are not a registered tool — the media route
runs in the dispatcher (§13), and (ii) OCR **and** STT are **not deferred**: both
landed as optional remote Gemini engines behind the boundary's existing
`OcrEngine`/`SttEngine` seams and are invoked live (§6, §12, §18.2).

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

## 18. STT UI-Text Lineage — Which Stage Produces the Text the Owner Reads

**Scope.** This is the current-state record of one live incident: a **Persian
Voice note** was replied to with `این رو stt کن`, and the delivered Telegram
message was not the string the STT engine returned. This section traces that
complete path in the source at the audited HEAD and separates the three things
the original question conflated: the **engine's transcript**, the **chat
provider's answer**, and the **delivered Telegram text**. Evidence tags are the
canonical triple **PROVEN FROM SOURCE** / **INFERRED FROM CONTROL FLOW** /
**NOT PROVABLE WITHOUT LIVE TRACE**. The quoted log lines are the runtime
evidence from that request; they were not re-produced here (this environment has
no Telegram session, no provider credential and no live traffic, §2).

### 18.1 Incident

- The owner replied to a Persian **Voice** note with `این رو stt کن`.
- Runtime evidence for that request: media-target resolution, the bounded
  download (35 941 bytes), an STT engine invocation, and the engine result
  reported as **51 characters** (`stt_engine_returned … chars=51`,
  `media_analysis_completed … chars=51`), followed by a **second** model call
  (`provider_call_started timeout_s=120.0`, `ROUTER_SELECTED
  provider=nararouter model=agnes-2.5-flash`, `provider_call_completed chars=80`),
  then delivery (`AI_OUTPUT_NORMALIZED … changed=True length=75`).
- The delivered message contained a Persian-language wrapper plus a quoted,
  garbled-looking transcription — approximately `محتوای صوتی ارسالی شما:` on its
  own line, then `«دیزی هاتهوا و تون کهم. چیزه باڵێه. هۆڵهسید چات.»`.
- The engine-level result quoted for the same audio at an earlier point was
  different in **both script and wording** (`Dia de ventos e de cap. Xi, zabolié.
  Olha, sei chat.`).
- **[FINDING]** The raw STT output and the delivered UI text are therefore
  **separate values** unless source proves otherwise, and here it does not: every
  media log line carries a **length**, never a **string**. Nothing in the pipeline
  records the transcript anywhere — `analyze_media` is read-only and persists
  nothing (`media_service.py::analyze_media`, "media processing is read-only and
  persists nothing").

### 18.2 Verified execution lineage

Every row was read in source. A step is listed only where the audit established
it; the log line that corroborates a step is named where the log carries one.

| # | Step | Exact location | Value / effect | Status |
|---|---|---|---|---|
| 1 | Replied Voice resolved into the request | `bot/handlers/ai_unified.py` (`_extract_reply_context` → `ai/media.py::classify_message`) | `ReplyContext.media_type == "Voice"`, `exists == True` | PROVEN (log: `media_request target=replied replied_media=Voice request_media=-`) |
| 2 | Media-target resolution | `ai/engine/dispatcher.py::_media_target` (`:1661`) | Returns `(chat_id, message_id)` of the **replied** message (the replied target is tried first, the triggering message second, and only types in `media_service.is_downloadable` resolve) | PROVEN |
| 3 | Media request handling | `dispatcher.py::_try_media_analysis` (`:1688`) | Runs **after** the local command fast path and **before** any prompt/context construction; calls `media_ai_service.answer_media_request(source, owner_id, chat_id, message_id, request_text=request.user_message, provider_manager, request_id, timeout_s)` | PROVEN (log: `media_request`, then `media_resolution_started`) |
| 4 | Deterministic resolution | `services/media_ai_service.py::answer_media_request` (`:209`) → `services/media_service.py::resolve_media_message` (`:1339`) | Fetches the one message the trusted runtime identified, by the ids from step 2 — never by text, recency, sender or model output | PROVEN (log: `media_resolution_started` → `media_resolution_completed`) |
| 5 | STT availability + bounded transfer | `media_service.py::analyze_media` (`:1410`) → `_stage_trace("stt_availability", …)`, `telegram_api/media.py::download_media`, `_stage_trace("media_download_*", …)` | Availability is `is_stt_mime(mime)` **and** `stt_available()`; the transfer bound is `min(max_download_bytes(), MAX_STT_INPUT_BYTES)`, applied **before** the transfer | PROVEN (log: `stt_availability type=Voice mime=audio/ogg available=True candidate=True`, `media_download_started declared_bytes=35941 timeout_s=120`, `media_download_completed bytes=35941`) |
| 6 | STT engine invocation | `media_service.py::_extract_audio_content` (`:1243`) → `_run_stt` (`:1190`) | `raw_text = await asyncio.wait_for(asyncio.to_thread(engine.transcribe, data), timeout=STT_TIMEOUT_S=60)` — the engine receives **only the validated audio bytes**; no chat id, no message id, no caption, no owner request text | PROVEN (log: `stt_engine_invoked engine=GeminiMediaEngine bytes=35941`) |
| 7 | Engine request | `services/gemini_media_engine.py::GeminiMediaEngine.transcribe` (`:311`) → `_run` (`:315`) → `_generate` / `_generate_from_upload` (`:342`, `:356`) | ONE `POST {GEMINI_API_BASE}/models/{model}:generateContent` with `contents=[{role: user, parts:[{text: STT_INSTRUCTION}, audio-part]}]`, `temperature=SAMPLING_TEMPERATURE (0.0)`, `maxOutputTokens=MAX_OUTPUT_TOKENS (8192)`; default model `gemini-3.5-flash-lite` (`DEFAULT_MEDIA_MODEL`, `:90`); one attempt, no internal retry | PROVEN |
| 8 | Raw Gemini response extraction | `gemini_media_engine.py::_extract_text` (`:544`) | Joins `candidates[0].content.parts[*].text` with `"\n"`; returns `""` for genuinely empty output; raises `MediaError` for blocked/refused/malformed. **No trimming, no case folding, no script conversion, no normalization call** | PROVEN (the engine calls no normalizer on this path) |
| 9 | Boundary normalization | `media_service.py::_extract_audio_content` (`:1291`–`:1294`) → `_normalize_extracted_text` (`:921`) → `_cap_text` (`:949`) | Whitespace only: per-line `" ".join(line.split())`, blank-run collapse, edge-blank trim; then the `MAX_STT_CHARS` cap. This is the value the log's `chars=51` measures — i.e. the **normalized** length | PROVEN |
| 10 | Analysis record | `media_service.py::MediaAnalysis` (`:281`), `analyze_media` return | `MediaAnalysis(content=<step 9 text>, status="extracted")` | PROVEN (log: `media_analysis_completed media_type=Voice status=extracted chars=51 truncated=False`) |
| 11 | Provider input construction | `media_ai_service.py::build_media_messages` (`:104`) | Exactly two messages: the static `MEDIA_ANALYSIS_SYSTEM_PROMPT` (`:63`), and one user message `f"{request_text}\n\n{analysis.as_context_text()}"`. `as_context_text()` (`media_service.py:339`) renders `[Media Content]` / `Type: Voice` / `MIME:` / `Size:` / `Status:` / (`Reason:`) / `Content:\n<transcript>` — and deliberately **no caption, no sender, no chat or message id** | PROVEN |
| 12 | Provider call | `media_ai_service.py::_provider_call` (`:166`) → `provider_manager.chat(messages, tools=[])` under `asyncio.wait_for(…, media_call_timeout(timeout_s))` = `min(120, envelope)` | One plain-text completion through the owner's selected provider on the non-multimodal path | PROVEN (log: `provider_call_started timeout_s=120.0`, `ROUTER_SELECTED provider=nararouter model=agnes-2.5-flash`, `AI_PROVIDER_ATTEMPT … attempt=1`, `provider_call_completed chars=80`) |
| 13 | Media answer | `media_ai_service.py::answer_media_request` (`:311`) | `MediaAnswer(text = str(response.text).strip())` — the boundary does not recase, translate, re-script, summarize or re-render the provider's text | PROVEN |
| 14 | Dispatcher response handling | `dispatcher.py::_try_media_analysis` return → `_build_fast_path_result` (`:1849`) | `EngineResult(response = text if success else "")` — i.e. `answer.text` **verbatim**; there is no media-specific transformation after the provider call | PROVEN (log: `media_completed status=extracted chars=80`) |
| 15 | Handler → delivery | `ai_unified.py` (`:842`) | `deliver_response(event, display_prompt, response_text=result.response, show_question)` | PROVEN (log: `telegram_response success=True`) |
| 16 | Output normalization | `ai/tools/delivery.py::process_output` (`:241`) → `_render_tables(_render_markdown(_normalize_plain(text)))` | NFC, optional `ي→ی` / `ك→ک` (only when the text already contains one of `پچژگ`), whitespace collapse, punctuation spacing, markdown/table rendering — see §18.5 | PROVEN (log: `AI_OUTPUT_NORMALIZED scripts=LATIN direction=ltr mixed=False markdown=False changed=True length=75`) |
| 17 | Presentation | `delivery.py::_format_chunks` (`:651`) → `format_presentation` (`:422`) → `_answer_block`, then `apply_presentation_provenance` (`:440`) | Adds only the `│`-quoted **owner** question (when the `show_question` preference is on), the `└─`/`┘─` elbow + 4-space indent, BiDi isolates, and the **invisible** provenance marker. It never adds a media label | PROVEN |
| 18 | Delivery | `delivery.py::deliver_response` (`:686`) → `event.edit` / `event.reply` | The delivered chunks are the final Telegram text | PROVEN |

**[FINDING]** The media path is therefore **one engine call plus one chat
provider call**: the engine produces a transcript (steps 7–9) and the chat
provider produces the text that is delivered (steps 12–13). There is no second
transcription step, no cached transcript and no persisted analysis anywhere in
this chain.

### 18.3 Exact transformation point

- **[PROVEN FROM SOURCE]** The first place the value the owner reads **stops
  being the transcription** is the **chat provider call in step 12**. Everything
  from the engine's return (step 8) to the provider input (step 11) is
  *identity-preserving apart from whitespace*: `_extract_text` joins parts,
  `_normalize_extracted_text` collapses whitespace, `_cap_text` caps length, and
  `as_context_text()` embeds the result verbatim after a `Content:` line. No
  local function in that span rewrites wording, language, script or case.
- **[PROVEN FROM SOURCE]** The transformation itself is **generation by a second
  model**, not a local string operation. `MEDIA_ANALYSIS_SYSTEM_PROMPT` asks that
  model to "answer the owner's request using only the owner's request and that
  content" and imposes **no** verbatim, no-translation, no-paraphrase or
  quote-only constraint on the content it received. Nothing in the pipeline
  compares `response.text` against `analysis.content`, and `MediaAnswer.text`
  (`media_ai_service.py:311`) is taken unmodified.
- **[PROVEN FROM SOURCE]** `response.text` is what the dispatcher returns
  (`_build_fast_path_result`: `response=text`), what the handler passes to
  `deliver_response`, and what delivery renders. A rewrite performed by that
  model therefore travels to the owner unopposed, including a change of script.
- **[INFERRED FROM CONTROL FLOW]** Because the second model is prompted in
  English about content labelled `[Media Content]` / `Type: Voice` while the
  owner wrote Persian, a Persian restatement is likely — but the code neither
  requires nor forbids it.
- **No local transformation explains the delivered text**, and none was found
  that renames, translates, transliterates, summarizes or reformats media
  content (§18.5).

### 18.4 The UI wrapper `محتوای صوتی ارسالی شما:`

- **[PROVEN FROM SOURCE]** The wrapper is **not local**. An exhaustive `git grep`
  over the tracked tree for `محتوای صوتی` and for `ارسالی` returns **zero hits in
  any code, configuration, template or test file** — the single hit anywhere is
  this document's own §2 method note. The guillemet pair `«`/`»` appears only in
  modules that do not execute on the media path (`ai/confirmation.py`,
  `ai/preparation_policy.py`, `services/ghost_seen_v2.py` and their tests).
- The complete set of **local** media-facing strings is enumerable and contains
  none of it: `MEDIA_ANALYSIS_SYSTEM_PROMPT` (`media_ai_service.py:63`);
  `MediaAnalysis.as_context_text()` → `[Media Content]`, `Type:`, `MIME:`,
  `Size:`, `Status:`, `Reason:`, `Content:` (`media_service.py:339`);
  `unsupported_text()` → `⚠️ I can't process this <type> yet.`
  (`media_ai_service.py:122`); the dispatcher's failure text
  `❌ Media processing failed: <reason>` (`dispatcher.py`); and the delivery
  presentation glyphs `│`, `└─`, `┘─` plus the four-space indent (`delivery.py`).
- **[PROVEN FROM SOURCE]** Therefore the wrapper is **provider-generated**: it is
  part of stage E (the chat provider's answer), inside the `80` characters the log
  reports for `provider_call_completed`, and the presentation layer merely renders
  that text.
- **[INFERRED FROM CONTROL FLOW]** Its wording plausibly restates the provider
  **input**'s own label — `as_context_text()` opens with `[Media Content]` and
  `Type: Voice` (`media_service.py:339`), which corresponds closely to "the audio
  content you sent". The correspondence is suggestive but is **not** proof that
  the model read that label rather than simply inferring an audio note from the
  owner's request.
- The wrapper's presence does **not** prove the model rewrote the transcript: the
  system prompt neither demands nor prohibits a wrapper, so its presence is
  consistent with a faithful quote. Which of the two happened is the gap in
  §18.9.

### 18.5 Unicode / script findings

| Mechanism | Where | What it actually does | Can it explain `باڵێه` / `هۆڵهسید`? |
|---|---|---|---|
| `unicodedata.normalize("NFC", text)` | `ai/tools/delivery.py:81` | Canonical **composition** only: it composes/decomposes a character with its own combining marks. It performs no letter substitution | **No** — NFC has no canonical mapping that yields `ڵ` (U+06B5), `ێ` (U+06CE) or `ۆ` (U+06C6) |
| Persian/Arabic character conversion | `ai/tools/delivery.py:82`–`:84` | The **only** letter mapping in the whole output path: `ي`→`ی`, `ك`→`ک`, and it runs **only if** the text already contains one of `پچژگ` | **No** — it cannot synthesize letters, and it never runs on Latin-script text |
| `_normalize_extracted_text` | `services/media_service.py:921` | Whitespace only (per-line run collapse, blank-run collapse, edge trim). Documented to leave Persian/Arabic text, ZWNJ (U+200C) and directional marks **untouched**. Uses no `unicodedata` at all | **No** — spacing only |
| `gemini_media_engine._extract_text` | `services/gemini_media_engine.py:544` | Joins response parts with `"\n"`; no normalization, no strip | **No** |
| Whitespace / punctuation rules | `ai/tools/delivery.py:80`–`:96` | Collapse spaces/tabs, trim around newlines, at most one blank line, remove a space before `,.;:!?،؛؟`, insert a space after `,;!?،؛؟` before a Latin/Cyrillic/Arabic letter. Protected regions (URLs, `@names`, `/commands`, inline/fenced code) are excluded | **No** — spacing only |
| BiDi / directional marks | `ai/tools/delivery.py:329`–`:339` (`_bidi_isolate`) and `apply_presentation_provenance` (`:440`) | **Adds** `U+2066`/`U+2067`, `U+200E`/`U+200F`, `U+2069` isolates and an invisible provenance marker to the rendered message | **No** — control characters cannot create a letter; they are added after the text exists |
| Script detection | `ai/tools/delivery.py:42`–`:63` (`_script`, `_profile`, `_RTL_SCRIPTS`) | Read-only classification (`unicodedata.name`), used to choose the elbow direction and to emit the log's `scripts=` / `direction=` fields | **No** — classification only |
| Transliteration / romanization / script converter | — | **Absent.** A `transliterat` grep finds only the STT instruction's own prohibition and doc prose; there is no conversion table, no Arabic→Persian mapper, no romanizer anywhere in the tree | **No** — no such code exists |

- **[PROVEN FROM SOURCE]** No inspected local stage can produce `ڵ`, `ێ` or `ۆ`,
  and the only local letter mapping (`ي`→`ی`, `ك`→`ک`) cannot create them. Those
  characters therefore arrive from **upstream** — stage A (Gemini) and/or stage E
  (the chat provider).
- **[PROVEN FROM SOURCE]** The log's `changed=True length=75` proves only that
  *something* in `process_output` changed the 80-character provider text (a
  5-character reduction is consistent with the whitespace/punctuation rules above,
  and equally with the `ي`/`ك` mapping); it does **not** identify which rule fired,
  and it does not imply that any word was rewritten.
- **[PROVEN FROM SOURCE — a contradiction inside the runtime evidence]**
  `AI_OUTPUT_NORMALIZED … scripts=LATIN direction=ltr` is computed on the text
  **after** normalization, i.e. on what delivery is about to send. It describes
  the delivered message as Latin-script and left-to-right. That cannot describe
  the Persian-script text quoted in §18.1. Two observations presented as the same
  delivery therefore disagree: either they are different requests, or one of the
  two records is a transcription of the other. The audit cannot decide this
  without the runtime values (§18.9); the disagreement is recorded as-is rather
  than resolved in favour of either.
- **[NOT PROVABLE WITHOUT LIVE TRACE]** Whether `ڵ`/`ێ`/`ۆ` were produced by
  Gemini from the audio or by the chat provider's restatement cannot be settled
  from source, because the transcript value (stages A/B/C) and the provider value
  (stage E) are never recorded — only their lengths are.

### 18.6 Value lineage (A–G)

| Stage | Value | Where it is produced | Proven | Runtime-only |
|---|---|---|---|---|
| **A** raw Gemini response | the model text of `generateContent`, parts joined with `"\n"` | `gemini_media_engine._extract_text` (`:544`) | its existence, its construction rule, and that it is passed unchanged to the seam | **the string itself** — never logged; only the post-normalization length (`chars=51`) is |
| **B** normalized STT text | A with whitespace collapsed and capped at `MAX_STT_CHARS` | `media_service._normalize_extracted_text` (`:921`) → `_cap_text` (`:949`) | the transformation is whitespace/length only | **the string**; the log reports its length only |
| **C** `MediaAnalysis.content` | exactly B (one assignment path) | `media_service.analyze_media` (`:1410`) | `content` is B verbatim; status `extracted`; nothing is persisted | **the string**; `chars=51 truncated=False` in the log |
| **D** provider input | `"<owner request>\n\n" + analysis.as_context_text()`, with C embedded verbatim after `Content:` | `media_ai_service.build_media_messages` (`:104`) | the construction is fully determined by the owner's text and C, and it is the **only** construction point, so no Telegram context can enter | **the string** |
| **E** provider output | `response.text.strip()` from the chat provider | `media_ai_service._provider_call` (`:166`) → `MediaAnswer.text` (`:311`) | that E is taken unmodified by the boundary and by the dispatcher, and **that the wrapper in §18.4 can only be inside E** | **the string**; `chars=80` in the log. Whether E echoes C or restates it is the §18.9 gap |
| **F** presentation-layer text | E (after `process_output`) + optional `│`-quoted owner question + elbow/indent + invisible marker | `delivery.process_output` (`:241`) → `format_presentation` (`:422`) → `apply_presentation_provenance` (`:440`) | that F adds **no** media wrapper, and that F's only change to E is the §18.5 normalization | **the string**; `length=75` in the log |
| **G** final Telegram text | the chunk(s) `event.edit` / `event.reply` send | `delivery.deliver_response` (`:686`) | that G is F (plus the invisible marker), and that nothing between F and G alters text | **the string as delivered**; the owner's report is the only record of it |

**[FINDING]** Read together: **G is a rendering of E**, and **E is a second
model's answer over D**, which contains **C**, which is a whitespace-normalized
form of **A**. The one step in that chain that can change language, script or
wording is **D→E** (a generative model). Every earlier step is identity-preserving
apart from whitespace.

### 18.7 Confidence by claim

**PROVEN FROM SOURCE**

- The media target is resolved deterministically from the runtime's own ids
  (`_media_target`), never by a model (§18.2 steps 2–3).
- The STT engine receives only validated bytes, and the engine returns the model's
  text with no local rewriting (§18.2 steps 6–8).
- The only local transformations applied to the transcript are whitespace collapse
  and a character cap (§18.2 step 9, §18.5).
- The provider input is built in exactly one place and contains the transcript
  verbatim, with no caption, sender, chat id or message id (`build_media_messages`,
  `as_context_text`).
- `MediaAnswer.text` and `EngineResult.response` carry the provider's text
  unmodified to the handler (§18.2 steps 13–14).
- Delivery's local additions are the question block, the connectors, BiDi isolates
  and the invisible provenance marker — none of them a media wrapper (§18.2 step 17).
- The wrapper `محتوای صوتی ارسالی شما:` occurs nowhere in the tracked tree's code
  (§18.4), so it is provider-generated.
- No transliteration, romanization, script conversion or Unicode letter mapping
  exists in the output path beyond `ي→ی`/`ك→ک`, and `unicodedata.normalize("NFC")`
  cannot create `ڵ`/`ێ`/`ۆ` (§18.5).
- `MediaAnalysis` is never persisted, so no stored artifact can be consulted to
  recover the transcript.

**INFERRED FROM CONTROL FLOW**

- That the wrapper restates the provider input's own `[Media Content]` /
  `Type: Voice` label (plausible; not established by code).
- That the reported UI text and the logged `AI_OUTPUT_NORMALIZED` line describe the
  same delivered value — their script/direction content disagrees (§18.5), so this
  is an assumption about the evidence, not a source fact.
- That the `show_question` presentation preference was off for the reported message
  (the reported text shows no `│` question block) — the preference is owner
  configuration, not source.

**NOT PROVABLE WITHOUT LIVE TRACE**

- The engine's returned string (stages A/B), and therefore whether the engine's own
  transcript was Persian-script, Latin-script or gibberish on that request.
- The provider's returned string (stage E), and therefore whether E quotes C or
  restates it — the decisive question for "who garbled the text".
- Which `process_output` rule produced `changed=True`, and the exact delivered
  string (stage G).

### 18.8 Root-cause status

**[FINDING] MIXED RESPONSIBILITY — the wrapper is resolved; the transcript's
fidelity is not.** Only these states are supported by the audit:

1. **A local formatting/transformation root cause is excluded.** No local stage can
   produce the wrapper, and none can rewrite language or script beyond the single
   `ي→ی`/`ك→ک` mapping plus whitespace (§18.3–§18.5). The delivered text is
   therefore **not** evidence of what the STT engine returned.
2. **The owner-visible wrapper is a downstream (provider) responsibility** — a
   second model's generation, not local presentation (§18.4).
3. **The transcription quality itself is not attributable from source.** The
   garbled wording could be (a) the engine's transcript faithfully quoted by the
   provider, (b) the engine's transcript restated or transliterated by the
   provider, or (c) the provider's own rendering of a shorter or partially garbled
   transcript. The code permits all three: the second model's prompt allows
   paraphrase, and neither value is recorded anywhere.
4. **What is already known about the engine's language behavior** is recorded in
   the engine itself, not in this chain: the instruction now requires identifying
   the spoken language, transcribing verbatim in it, writing it in that language's
   own script, and forbids translation and transliteration/romanization — with a
   comment citing the earlier live run in which a Persian Voice note came back as
   Latin-script gibberish straight from the engine (`STT_INSTRUCTION`,
   `gemini_media_engine.py:104`–`:127`). That is a **recorded prior live
   observation**, not a value from the audited request; it explains why the
   engine's own output is the first suspect without proving it.

**Verdict: RESOLVED FOR THE WRAPPER; UNRESOLVED FOR THE TRANSCRIPT.** The claim
"the delivered text is the Gemini STT output" is **false on the evidence**: it is a
second model's output. The claim "the delivered text is a faithfully quoted Gemini
transcript" is **not proven**: it remains a runtime-only question.

### 18.9 Remaining gap

The value-level lineage cannot be closed from source. The following runtime
observations are the ones that would close it — named because the audit must make
the gap explicit, **not** because this document changes anything:

1. **The engine's returned string** (stages A/B) alongside its length, so the
   transcript is a value rather than a `chars=` count.
2. **The provider's returned string** (stage E) alongside its length, so it can be
   compared with (1) — that single comparison separates "faithful quote" from
   "provider restatement".
3. **The delivered string** (stage G) with its `show_question` state, to reconcile
   the script/direction contradiction recorded in §18.5.

The existing traces already identify *which leg* ran and *how large* each value was
— `stt_engine_invoked` / `stt_engine_returned` / `media_analysis_completed` /
`provider_call_started` / `provider_call_completed` / `media_completed` /
`AI_OUTPUT_NORMALIZED` — so the gap is specifically the **content** of those
values, not the shape of the path. Until such an observation exists, the quality of
the STT result is measured only by the owner's next live request (§12).

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
| New abstraction created | **none by this document** — the media boundary, the engines and the services it names are *pre-existing* at the audited HEAD (§4, §13); §13/§15 are marked superseded |
| Evidence | every material claim cites an exact path + symbol/line (§3–§18) |
| Tests run | none — no code changed |
| Media STT UI-text lineage (§18) | **traced from source** — the owner-visible wrapper is provider-generated (§18.4); the transcript's fidelity is a runtime-only gap (§18.9) |
| Media value lineage A–G (§18.6) | separated by stage; the stage **values** are not recorded by the runtime (lengths only) |
| Live Telegram / Supabase / Render verification | **not performed** — no session, credential or traffic here; §18.9 states what a live observation would have to capture |
| Fabricated commits / pushes | none claimed |

**Proven from source:** the text-only prompt and provider path and its exact
seams (§3, §6); the media detection/metadata/download inventory per type (§4);
the download bound on the media path plus the existing size/temp/cleanup
controls (§5); the dead `vision()` seam and the manager's inactive-provider selection
(§6); every context-injection path into the model and the unconditional
attachment for reply-shaped requests (§7.1); the services-layer precedent that
performs its own provider call through the *selected* provider (§6, §8); the
exact limit values (§9); the absence of **local** OCR/STT/document/video dependencies and system
binaries (§10); and, for §18, that the STT engine normalizes nothing, that the
provider input is built in exactly one place, that the wrapper phrase exists
nowhere in the tracked tree, and that no local stage can introduce
`ڵ`/`ێ`/`ۆ` (§18.3–§18.5).

**Not proven / not measured:** real Telegram latency, memory and RPC cost for a
50 MB asset inside the 240 s envelope; whether any particular normalization
quality suffices for the owner's intent; the product decisions listed in §16; and,
for §18, the runtime **values** of the engine transcript (A/B/C) and of the chat
provider's answer (E) — the two strings that would decide whether the delivered
transcription was quoted or restated (§18.9).

---

**No fix or feature was implemented. Only this document was modified.**
