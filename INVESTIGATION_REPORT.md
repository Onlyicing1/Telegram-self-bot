# INVESTIGATION REPORT — LifeOS (Deep Save rebuilt)

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
| **Scope** | Deep Save rebuild (independent download → re-upload pipeline) |
| **Type** | Investigation + rebuild (code restructured, tests added) |

---

## 1. Problem

Deep Save (`mode="d"`) must be an independent **download → re-upload**
pipeline that works when native Telegram forwarding is restricted (protected
chats, forwarding-restricted media). It must **never** call
`forward_messages`/`ForwardMessagesRequest` and must **never** fall back to
forwarding.

A production log showed `mode=f … forward save failed: You can't forward
messages from a protected chat`, so the mode routing also had to be verified.

## 2. Root Cause

**The old deep-save logic itself was already a true download → re-upload**
(the prior commits had fixed this), but it lived inside one monolithic
`execute_save()` as an `else` branch of the forward path. That structure
violated the required invariant that Forward Save and Deep Save be **two
explicitly separate pipelines**, and made the "Deep Save might forward" fear
hard to disprove by inspection.

The `mode=f` production log was **not** a deep→forward routing bug: it was a
genuine forward-save execution. Verified routing facts:

- The only `forward_messages` call in the Save Engine is inside the Forward
  pipeline (now `execute_forward_save`).
- The Glass Save panel routes Deep Save as `panel:save:type:d` →
  `action:save_reply:d` → `mode="d"` (deterministic).
- The AI `SaveTool` maps `"forward"`→`"f"` and `"deep"`→`"d"` (deterministic).
- The `.save f` / `.save d` dot commands documented in docstrings and
  AGENTS.md are **not registered** — the live command surface is `.menu`
  (Glass UI) and `.ai` (AI). A user typing `.save d` gets no handler, so the
  panel/AI paths are the real entry points.

## 3. Confirmed Facts

- **Deep Save was rebuilt from scratch** into `execute_deep_save()` — a
  standalone pipeline with no forward call and no forward fallback.
- **Deep Save = download source content → upload as a NEW Saved Messages
  message.** Media path: `download_media(reply_msg, file=tmp_path)` →
  `send_file("me", tmp_path, caption=…)`. Text path: `send_message("me",
  caption)` (TEXT → TEXT).
- **Deep Save NEVER forwards.** No `forward_messages` call site exists in
  `execute_deep_save` or any path reachable from it.
- Forward Save is now `execute_forward_save()` — the **only** place that
  calls `forward_messages`. It does not download.
- Mode normalization is owned by the engine dispatcher (`execute_save`):
  `"f"|"forward"|"fwd"` → Forward; everything else → Deep.
- Captions use the single pipeline `build_caption()` + `_append_original_text()`
  and are attached to the uploaded media via `send_file(caption=…)`.
- Destination metadata (`saved_chat_id`, `saved_msg_id`, `file_id`,
  `mime_type`, `file_size`) is derived from the **newly uploaded** message via
  `_extract_uploaded_metadata(sent)`; `origin_chat_id`/`origin_msg_id` stay the
  source.
- Database insert happens **after** a successful upload; download/upload
  failure returns an `"❌ …"` string **before** any `insert_save`.
- Temporary storage is unique per operation (`tempfile.mkdtemp`) and removed
  in `finally` on success, download failure, and upload failure.

## 4. Direct Source Evidence

- **FILE:** `backend/services/save_service.py` — `execute_deep_save()`
  **EVIDENCE:** STAGE 1 (text-only → `send_message`), STAGE 2 (media →
  `client.download_media(reply_msg, file=tmp_path)` then
  `client.send_file("me", tmp_path, caption=caption,
  **_upload_kwargs_for_media(...))`), STAGE 3 (metadata + `insert_save`).
  No `forward_messages` in the function.
- **FILE:** `backend/services/save_service.py` — `execute_forward_save()`
  **EVIDENCE:** `client.forward_messages("me", reply_msg)` — the only forward
  call site in the Save Engine.
- **FILE:** `backend/services/save_service.py` — `execute_save()`
  **EVIDENCE:** thin dispatcher; normalizes mode then calls one of the two
  pipelines. It never performs a transfer itself.
- **FILE:** `backend/services/save_service.py` — `_extract_uploaded_metadata()`
  **EVIDENCE:** reads `id`/`mime_type`/`size` from `sent.media` (the NEW
  message), not the source.
- **FILE:** `backend/services/save_service.py` — `_upload_kwargs_for_media()`
  **EVIDENCE:** rebuilds `DocumentAttribute*` attributes + MIME +
  `force_document=False` so photo/video/audio/voice/sticker/document
  semantics are preserved on re-upload.
- **FILE:** `backend/bot/handlers/save.py` — `_save_panel_handler()`
  **EVIDENCE:** `panel:save:type:d` → `action:save_reply:d`; `type:f` →
  `action:save_reply:f`.
- **FILE:** `backend/ai/tools/save.py` — `SaveTool.execute()`
  **EVIDENCE:** `mode = "f" if str(mode_arg).lower().startswith("f") else "d"`
  then delegates to `execute_save`.
- **FILE:** `backend/bot/router.py` — `register_all()`
  **EVIDENCE:** only `misc`/`ai`/`ai_cmd`/`ai_unified` register
  `events.NewMessage` handlers. `save.register()` registers only panels and
  actions, so `.save f`/`.save d` dot commands are **not** live.

## 5. Relevant Files

- `backend/services/save_service.py` — authoritative Save Engine (rebuilt).
- `backend/bot/handlers/save.py` — Glass Save panel (routing verified, unchanged).
- `backend/ai/tools/save.py` — AI `SaveTool` (mapping verified, unchanged).
- `tests/test_12_save_engine.py` — regression tests (extended).

## 6. Relevant Functions / Classes

| Function/Class | Role |
|---|---|
| `execute_save()` | Dispatcher (single authority for mode semantics). |
| `execute_forward_save()` | Forward pipeline (`forward_messages` only). |
| `execute_deep_save()` | Deep pipeline (download → re-upload only). |
| `execute_link_save()` | Link-based deep save (also download → re-upload). |
| `_resolve_sender()` | Shared sender resolution. |
| `_extract_source_media()` | Shared source metadata extraction. |
| `build_caption()` / `_append_original_text()` | Shared caption pipeline. |
| `_upload_kwargs_for_media()` | Media-type-preserving upload kwargs. |
| `_extract_uploaded_metadata()` | Destination metadata from the NEW message. |
| `SaveTool.execute()` | AI path → `execute_save`. |
| `_save_panel_handler()` | Glass path → `save_reply:f`/`save_reply:d`. |

## 7. Previous Implementation (rejected)

The previous `execute_save()` held both behaviors in one function: `if
mode == "f": …forward… else: …deep…`. The deep branch itself was a correct
download → re-upload, but:

- Forward and Deep were not separate functions, so the invariant
  "mode=d must never forward" was only true by reading one `else` branch.
- There was no independent Deep Save entry point to test or reason about
  directly.

## 8. New Architecture

```
Glass UI / AI SaveTool
        │  (mode = "f" | "d")
        ▼
execute_save()   ← dispatcher, normalizes mode, no transfer logic
   ┌────┴────┐
   │         │
 mode=f    mode=d
   │         │
   ▼         ▼
execute_forward_save()      execute_deep_save()
   │  forward_messages        │  STAGE 1: text-only → send_message
   │  + caption attach        │  STAGE 2: media → download_media → validate
   │                          │           → send_file (NEW message)
   │                          │  STAGE 3: extract NEW metadata → insert_save
   └────┬────┘                └──────────┬──────────┘
        └──────────────┬─────────────────┘
                       ▼
                   Database
```

## 9. Exact Execution Path (Deep Save)

1. `execute_save(client, owner_id, reply_msg, "d", tz_str)` normalizes `"d"`.
2. Dispatches to `execute_deep_save()`.
3. Resolve sender + source metadata (`_resolve_sender`,
   `_extract_source_media`).
4. If no media → build caption → `send_message("me", caption)`.
5. Else → `tempfile.mkdtemp` → `download_media(reply_msg, file=tmp_path)` →
   validate non-empty → `send_file("me", tmp_path, caption=caption,
   **_upload_kwargs_for_media(...))`.
6. `finally` removes the temp directory.
7. `_extract_uploaded_metadata(sent)` reads the NEW message's file metadata.
8. `insert_save(payload)` (destination ids/file metadata) → `db_client.log`
   → success confirmation.

## 10. Changes Made

- `backend/services/save_service.py`: split the monolithic `execute_save`
  into `execute_forward_save()`, `execute_deep_save()`, and a thin
  `execute_save()` dispatcher, plus `_resolve_sender()` and
  `_extract_source_media()` shared helpers. Behavior is preserved; only the
  pipeline boundaries are now explicit.
- `tests/test_12_save_engine.py`: added tests for the two independent
  entry points, forward-restriction independence, upload-failure honesty
  (no DB record + cleanup), strengthened download-failure assertions, and
  Glass panel mode routing.

## 11. Remaining Work

- **Bounded download/upload.** `execute_deep_save` uses the raw Telethon
  client (`download_media`, `send_file`) without the `guarded_await` watchdog
  used by `backend/telegram_api/*`. They are async and non-blocking, but a
  stuck connection could await indefinitely. A **size-proportional** timeout
  (not a flat 30s) is the recommended next hardening step.
- **DB failure after a successful upload** is logged but still returns the
  success confirmation (the media *is* in Saved Messages). Changing this to a
  degraded "⚠️ saved to Telegram but DB record failed" message is a policy
  decision, not yet applied.
- **`.save f` / `.save d` dot commands are not registered.** The save surface
  is Glass-UI + AI-tool. Registering them (routing to `execute_save`) is
  possible but was not part of this task, to avoid introducing a second
  command surface for a panel-driven architecture.
- **Animated GIF labeling:** animated GIFs (`video/mp4` +
  `DocumentAttributeAnimated`) are labeled `"Video"` by `detect_media_type`
  (cosmetic; the animated attribute is preserved on re-upload).

## 12. Database / Schema Impact

- **None.** No schema changes. `save_type` (`"forward"`/`"deep"`) is the
  existing mode column; all required fields are already populated.

## 13. Hard Constraints

- One authoritative Save Engine (`execute_save` + two pipelines). No
  duplicate pipeline was introduced.
- `mode="d"` = download → re-upload; it MUST NEVER forward and never falls
  back to forwarding.
- `mode="f"` = forward; it was not converted to deep.
- Single Telethon client; no new event loop; temp files cleaned up in
  `finally`.

## 14. Validation

- `python -m py_compile backend/services/save_service.py` → OK.
- `pytest tests/test_12_save_engine.py -q` → **25 passed**.
- `pytest -q` (full suite) → **205 passed**, 0 failures.
- Coverage now includes: Deep Save never forwards; deep works even when
  `forward_messages` raises (protected-chat simulation); download failure →
  no upload + no DB record; upload failure → no DB record + cleanup; text-only
  deep save; media-type attribute preservation; destination metadata from the
  NEW upload; temp cleanup on success/failure; independent
  `execute_forward_save`/`execute_deep_save` entry points; Glass panel
  `save_reply:d`/`save_reply:f` routing.

## 15. Live Telegram Verification Status

- **SOURCE VERIFIED** ✅ — Telethon 1.34 source confirms `get_attributes`
  honors explicit attributes and `_file_to_media` preserves photo/video/audio
  round-trips.
- **TEST VERIFIED** ✅ — mock-client regression tests above all pass.
- **LIVE TELEGRAM VERIFIED** ❌ — not performed (no credentials/session in
  this environment). Real re-upload behavior in a protected chat is untested
  live.
