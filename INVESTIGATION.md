# INVESTIGATION.md — Canonical Forensic Report (Save System)

> **Rule:** this file is the single canonical investigation report for the
> LifeOS repository. Every new investigation **fully replaces** this file —
> never append. Always base findings on actual repository source; distinguish
> confirmed facts, direct evidence, likely causes, and unknowns.

---

## Latest investigation: Save System Rebuild (Deep Save only)

- **Date:** 2026-08-18
- **Scope:** Save Engine — remove Forward Save, make Deep Save the only method,
  and verify the Glass UI Reply Mode flow end-to-end.

---

## 1. Problem

The Save Engine previously had two modes — Forward Save (`mode="f"`,
`client.forward_messages(...)`) and Deep Save (`mode="d"`,
`download_media → send_file`). Forward Save is the wrong tool for protected
chats: Telegram rejects `ForwardMessagesRequest` with
`"You can't forward messages from a protected chat"`.

The requirement for this rebuild: **Forward Save is removed entirely.** Deep
Save is the only save method, and the canonical user workflow is the Glass UI:

```
.menu → Save → Deep Save → Reply Mode → reply to a target message
```

Deep Save must physically download the source content and re-upload it as a
**new** Saved Messages message, never forwarding.

## 2. Root Cause

No single bug — a design cleanup. The old `save_service.py` shipped two
pipeline implementations side by side:

- `execute_forward_save()` — the only `forward_messages` call site in the
  Save Engine.
- `execute_deep_save()` — a separate, correct download → re-upload pipeline.
- `execute_save()` — a mode dispatcher that selected between them.
- `execute_link_save()` — a **third** copy of the download → re-upload
  pipeline (with progress rendering) that duplicated `execute_deep_save`.

The Glass UI also exposed both `panel:save:type:f` (Forward) and
`panel:save:type:d` (Deep), and the AI `SaveTool` exposed `"forward"`/`"deep"`
modes. These have all been collapsed to a single Deep Save pipeline.

## 3. Confirmed Facts

1. **Deep Save never forwards.** The rebuilt `execute_save` contains **zero**
   `forward_messages`/`ForwardMessagesRequest` call sites. `grep` over
   `backend/services/save_service.py` confirms the only remaining forward
   references in the repo are the **unrelated** retrieval subsystem
   (`retrieve_service.py` — the `.send <code>` command) and the generic
   `backend/telegram_api` facade wrappers, neither of which is part of Save.
2. **Deep Save is a real download → re-upload.** For media it runs
   `download_media(file=tmp_path)` then `send_file("me", tmp_path, ...)`.
   For text-only messages it runs `send_message("me", ...)`.
3. **No deep→forward fallback.** Download failure, upload failure, empty
   file, and missing file each return an honest `❌ Deep Save failed: ...`
   result and never invoke forwarding.
4. **Temp files are isolated and cleaned up.** Each media save uses
   `tempfile.mkdtemp(prefix="lifeos_dl_")` and `shutil.rmtree(...)` in a
   `finally` block; tests assert no `lifeos_dl_*` directories remain.
5. **DB is written only after Telegram upload succeeds.** The
   `db_client.insert_save(...)` call happens in Stage 3, after the new message
   exists. Failed download/upload produce no DB row.
6. **DB metadata references the NEW upload.** `saved_chat_id`/`saved_msg_id`
   come from the `send_file`/`send_message` result; `file_id`/`mime_type`/
   `file_size` are extracted from the new message's media and only fall back
   to source values when Telegram exposes nothing.
7. **Reply Mode resolves the exact target.** `_save_reply_wait_handler`
   fetches the user's outgoing reply, reads `reply_to_msg_id`, then fetches
   that exact message and passes it into `execute_save`. The user's reply
   message itself is never saved.

## 4. Direct Source Evidence

- `backend/services/save_service.py` — `execute_save` (single pipeline),
  `execute_link_save` (resolves link → delegates), `_upload_kwargs_for_media`
  (media-type preservation), `_extract_uploaded_metadata` (new-message
  metadata), `build_caption` (compact caption), `_append_original_text`.
- `backend/bot/handlers/save.py` — `_save_panel_handler` (Deep-only panel),
  `_save_reply_action` (enters Reply Mode), `_save_reply_wait_handler`
  (reply-to-target resolution), `_save_link_input_handler`.
- `backend/helper/inline_sender.py` — `register_input_listener` feeds the
  next outgoing message into the pending handler.
- `backend/helper/input_state.py` — `set_pending`/`get_pending`/`clear_pending`
  (Reply Mode pending state).
- `backend/ai/tools/save.py` — `SaveTool` (Deep-only, delegates to
  `execute_save`).
- Tests: `tests/test_12_save_engine.py`, `tests/test_14_tool_honesty_glass.py`.

## 5. Relevant Files

| File | Role |
|---|---|
| `backend/services/save_service.py` | Authoritative Save Engine (Deep only) |
| `backend/bot/handlers/save.py` | Glass UI panel + Reply Mode + link input |
| `backend/ai/tools/save.py` | AI SaveTool (delegates to the engine) |
| `tests/test_12_save_engine.py` | Save engine + Glass Reply Mode tests |
| `tests/test_14_tool_honesty_glass.py` | Tool honesty + protected-chat tests |

## 6. Relevant Functions / Classes

- `save_service.execute_save(client, owner_id, reply_msg, tz_str)` — the one
  authoritative pipeline.
- `save_service.execute_link_save(client, owner_id, link, tz_str)` — link
  resolution then delegation.
- `save_service._upload_kwargs_for_media(media, mime_type, file_name)` — keeps
  photo/video/audio/voice/sticker/document semantics.
- `save_service._extract_uploaded_metadata(sent)` — new-message file metadata.
- `save_service._append_original_text(caption, message)` — preserves source text.
- `save_handler._save_reply_wait_handler(...)` — reply-to-target resolution.
- `SaveTool.execute(...)` — AI path (always Deep).

## 7. Current Behavior (after rebuild)

- The Save panel shows **Deep Save** and **Retrieve** only — no Forward Save.
- `panel:save:type:d` shows **💬 Reply Mode** and **🔗 Save using a link**.
- Reply Mode shows `Waiting for your reply...` and captures the next outgoing
  reply; its `reply_to_msg_id` resolves the target message.
- The target is downloaded → validated (exists + size > 0) → uploaded as a
  new Saved Messages message (or a new text message when text-only).
- Caption = compact LifeOS header + original source text, attached to the
  uploaded media (no separate message).
- DB record is inserted only after the new message exists, referencing the
  new message.

## 8. Desired Behavior (acceptance criteria)

```
.menu → Save → Deep Save → Reply Mode
→ reply to a real Telegram message
→ source message downloaded
→ downloaded content uploaded to Saved Messages
→ NEW Saved Messages message exists
```

## 9. Changes Made

1. **Deleted `execute_forward_save`** and the only `forward_messages` call in
   the Save Engine. Forward Save no longer exists.
2. **Collapsed to one pipeline** — `execute_save` is now the single Deep Save
   implementation (download → re-upload / text → `send_message`). Removed the
   duplicate `execute_deep_save` and `execute_forward_save` functions and the
   link-save progress machinery.
3. **`execute_link_save` now delegates** to `execute_save` after resolving the
   t.me link, removing the third copy of the pipeline.
4. **Compact caption** (`build_caption`) — `📦 S0001 · DEEP` style with sender,
   timestamp, `chat_id/msg_id`, media type/size/mime, filename, tags, and the
   original text appended.
5. **Glass UI** — removed the Forward Save button; Reply Mode no longer takes a
   mode parameter.
6. **AI SaveTool** — removed the `"forward"`/`"deep"` mode argument; always
   Deep Save.
7. **Honest stage-level errors** — download/upload failures include the actual
   Telegram error text.

## 10. Remaining Work

- **Live Telegram validation** — the download → re-upload and Reply Mode flows
  are source- and test-verified only (no credentials/session in the sandbox).
- The deep-save `download_media`/`send_file` calls are plain `await`s; they are
  async and non-blocking, but a stuck connection could still await long. A
  size-proportional bounded timeout (via the existing
  `runtime.operation_watchdog.guarded_await`) is a reasonable future
  hardening, deliberately left out to avoid breaking large-file saves.
- `backend/telegram_api` still exposes a `forward_messages` wrapper used by the
  **retrieve** subsystem (`.send <code>`). It is unrelated to Save and was
  intentionally not changed.

## 11. Database / Schema Impact

**None.** No schema change is needed. All records now use `save_type="deep"`.
The `saved_items` columns (`save_code`, `save_type`, `origin_chat_id`,
`origin_msg_id`, `saved_chat_id`, `saved_msg_id`, `file_id`, `mime_type`,
`file_size`, `media_type`, `tags`, `caption`, `owner_id`, `created_at`) are
unchanged.

## 12. Hard Constraints

- **`mode="d"` = download → re-upload, ALWAYS.**
- **Deep Save MUST NEVER fall back to `forward_messages` / `ForwardMessagesRequest`.**
- One authoritative Save Engine (`save_service`).
- UI / command / AI → Save Engine → Telegram → Database (no business logic in
  handlers).
- No unrelated subsystem changes (AI, deletion, heartbeat, Telethon internals).

## 13. Validation

- `py_compile` on `save_service.py`, `bot/handlers/save.py`,
  `ai/tools/save.py`, and the two test files — **OK**.
- `pytest tests/test_12_save_engine.py tests/test_14_tool_honesty_glass.py` —
  **43 passed**.
- Full suite `pytest -q` — **204 passed**, 0 failures.
- `grep` for `forward_messages`/`ForwardMessagesRequest`: none in the Save
  Engine; remaining occurrences are retrieve/`telegram_api` only.

**Verification levels:**
- `SOURCE VERIFIED` ✅
- `TEST VERIFIED` ✅
- `LIVE TELEGRAM VERIFIED` ❌ (not possible in this environment)

## 14. Unknowns / Missing Evidence

- Whether Telegram permits `download_media` for every protected-chat message
  (the pipeline correctly attempts it; if Telegram also blocks download, it
  reports the real error).
- Exact live behavior of timed/self-destructing media (no live session).
