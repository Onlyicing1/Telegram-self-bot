# INVESTIGATION REPORT — LifeOS (Deep Save)

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
| **Scope** | Deep Save (`mode="d"`) implementation correctness |
| **Type** | Investigation + fix (code changed, tests updated) |

---

## 1. Problem

The Deep Save feature was repeatedly reported as "broken" with a claim that a
Deep Save request in a content-protected chat reached Telegram's
`ForwardMessagesRequest` and failed with "You can't forward messages from a
protected chat". Deep Save exists specifically to work in situations where
native forwarding is blocked, so this was the symptom to prove or disprove.

## 2. Root Cause

**The reported protected-chat → forward failure is NOT reproduced by the
current code.** `mode="d"` cannot reach `forward_messages`. The claim appears
to be based on stale code or a misattributed log line.

Two real, code-confirmed defects **were** found in the authoritative Save
Engine while tracing the deep-save path:

1. **Forward-save caption attachment never worked.**
   `backend/services/save_service.py` called
   `client.edit_message("me", fwd, caption=caption)`. Telethon's
   `edit_message` has **no `caption` keyword** (its parameter is `text=`), so
   this raised `TypeError` every time, was swallowed by the surrounding
   `except` (logged only as a warning), and left the forwarded message's
   caption unattached with `caption=None` written to the database.

2. **Mode normalization lived only in one caller.** `execute_save` used
   `if mode == "f": forward else: deep`. The AI `SaveTool` mapped friendly
   names to `"f"`/`"d"`, but the engine itself treated any non-`"f"` string
   (including `"forward"` or `"fwd"`) as **deep**. The engine — the single
   authority — did not own mode semantics, so a future/alternate caller
   passing `"forward"` would silently perform a deep save.

## 3. Confirmed Facts

- **Deep Save is a true download → re-upload.** `execute_save(mode="d")`
  downloads the source media to a unique temporary directory and uploads it
  with `send_file` as a brand-new Telegram message. It never calls
  `forward_messages`.
- The **only** `forward_messages` call in the Save Engine is inside the
  `mode == "f"` branch (`backend/services/save_service.py`, ~line 728).
- **Text-only deep save** sends a new text message via `send_message`
  (TEXT → TEXT). No download is attempted for media-less messages.
- **Captions** for deep save are attached directly to the uploaded media via
  `send_file(caption=caption)`; text-only deep save sends the combined
  LifeOS header + original text as the message body.
- **Media type preservation** is implemented via `_upload_kwargs_for_media`,
  which passes the original Telegram document attributes (video/audio/voice/
  sticker/filename), the original MIME type, and `force_document=False`.
- **Metadata refers to the new upload.** `_extract_uploaded_metadata(sent)`
  reads `file_id`/`mime_type`/`size` from the message returned by
  `send_file`, and the DB payload falls back to source metadata only when
  Telegram returns nothing.
- **Database insert happens after** the Telegram upload succeeds. Failed
  download/upload returns an `"❌ …"` string **before** any `insert_save`.
- **Temporary files are always cleaned up.** The deep-save media path wraps
  download/upload in `try/finally: shutil.rmtree(tmp_dir, ignore_errors=True)`.
- **Protected-chat invariant holds.** A download failure returns an honest
  `"❌ Download failed: …"` and does **not** fall back to forwarding (tested).

## 4. Direct Source Evidence

- **FILE:** `backend/services/save_service.py` — `execute_save(client, owner_id,
  reply_msg, mode, tz_str)`
  **EVIDENCE:** forward branch `if mode == "f":` calls `client.forward_messages
  ("me", reply_msg)` (~line 728). The `else` branch (deep) calls
  `client.download_media(reply_msg, file=tmp_path)` (~line 842) then
  `client.send_file("me", tmp_path, caption=caption,
  **_upload_kwargs_for_media(...))` (~line 851). No `forward_messages` in the
  deep branch.
- **FILE:** `backend/services/save_service.py` — `_upload_kwargs_for_media`
  **EVIDENCE:** `MessageMediaPhoto → {"force_document": False}`; otherwise
  rebuilds `attributes` from the source document, re-adds
  `DocumentAttributeFilename`, resolves `mime_type`, returns
  `{"attributes": attrs, "force_document": False, "mime_type": ...}`.
- **FILE:** `backend/services/save_service.py` — `_append_original_text`
  **EVIDENCE:** appends the source `message.text` below the generated LifeOS
  caption (single caption pipeline shared by both modes).
- **FILE:** `backend/services/save_service.py` — `_extract_uploaded_metadata`
  **EVIDENCE:** reads `id`/`mime_type`/`size` from `sent.media.document` or
  `sent.media.photo` (the NEW message), not the source.
- **FILE:** `backend/services/save_service.py` — deep-save media path
  **EVIDENCE:** `tmp_dir = tempfile.mkdtemp(prefix="lifeos_dl_")`;
  `finally: shutil.rmtree(tmp_dir, ignore_errors=True)`.
- **FILE:** `/tmp/lifeos-venv/.../telethon/client/messages.py` — `edit_message`
  **EVIDENCE:** signature is `edit_message(entity, message=None, text=None,
  *, parse_mode=(), attributes=None, formatting_entities=None,
  link_preview=True, file=None, thumb=None, force_document=False,
  buttons=None, supports_streaming=False, schedule=None)`. **No `caption`
  keyword** — confirming the forward-save caption bug.
- **FILE:** `/tmp/lifeos-venv/.../telethon/utils.py` — `get_attributes`
  **EVIDENCE:** explicit user `attributes` override auto-detected ones by
  class (`attr_dict[type(a)] = a`), so passing the original
  `DocumentAttributeVideo`/`DocumentAttributeAudio`/`DocumentAttributeSticker`
  preserves the media type on re-upload.
- **FILE:** `/tmp/lifeos-venv/.../telethon/client/uploads.py` —
  `_file_to_media`
  **EVIDENCE:** `as_image = is_image and not force_document` (photos stay
  photos); documents are sent with `force_file=force_document and not
  is_image` and the supplied attributes, so video/voice/audio attributes
  round-trip correctly.

## 5. Relevant Files

- `backend/services/save_service.py` — authoritative Save Engine (changed).
- `backend/ai/tools/save.py` — `SaveTool` (caller; maps `"forward"/"deep"` →
  `"f"/"d"`). Not changed.
- `backend/bot/handlers/save.py` — Glass Save panel (caller; passes
  `"f"`/`"d"`). Not changed.
- `backend/telegram_api/api.py` / `messages.py` / `media.py` — bounded
  Telegram facade (not used by `save_service`, which uses the raw client).
- `tests/test_12_save_engine.py` — regression tests (changed).

## 6. Relevant Functions / Classes

| Function/Class | Role |
|---|---|
| `execute_save()` | One authoritative save entry point; branches `"f"` vs `"d"`. |
| `execute_link_save()` | Link-based deep save (also download → re-upload). |
| `build_caption()` | Deterministic LifeOS caption header. |
| `_append_original_text()` | Preserves source text below the header. |
| `_upload_kwargs_for_media()` | Preserves media type/attributes on re-upload. |
| `_extract_uploaded_metadata()` | Reads metadata from the NEW upload. |
| `SaveTool.execute()` | AI path; delegates to `execute_save`. |
| `_save_reply_wait_handler()` | Glass panel path; delegates to `execute_save`. |

## 7. Current Behavior (after this fix)

- `mode="d"` → download → temp file → `send_file`/`send_message` → DB insert
  → cleanup. Never forwards.
- `mode="f"` → `forward_messages` → caption attached via `edit_message
  (text=…)` → DB insert.
- Friendly mode names (`"forward"`, `"fwd"`) now normalize to forward inside
  the engine, so they can no longer silently become a deep save.

## 8. Desired Behavior (invariant)

```
mode="d"  =  download → re-upload  (MUST NEVER fall back to forwarding)
mode="f"  =  forward                (native Telegram forward)
```

Deep Save must remain a Deep Save failure on any download/upload error. It
must not degrade into Forward Save.

## 9. Changes Made

1. `backend/services/save_service.py`:
   - Added engine-level mode normalization at the top of `execute_save`:
     `"f"|"forward"|"fwd"` → `"f"`, everything else → `"d"`.
   - Fixed forward-save caption attachment: `edit_message(..., caption=caption)`
     → `edit_message(..., text=caption)`.
2. `tests/test_12_save_engine.py`:
   - `MockClient.edit_message` now mirrors Telethon's real signature
     (`text=None, **kwargs`) so the forward-caption test actually catches a
     `caption=` regression.
   - Added `test_execute_save_normalizes_friendly_mode_names` (forward vs
     deep stay separated for friendly names).
   - Strengthened `test_deep_save_cleans_temp_on_download_error` to also
     assert no `forward_messages` on a failed download.

## 10. Remaining Work

- **Bounded download/upload.** `save_service` uses the raw Telethon client
  directly (`download_media`, `send_file`) without the `guarded_await`
  watchdog used by `backend/telegram_api/*`. These are async and non-blocking
  (no event-loop stall), but a stuck connection could await indefinitely. A
  **size-proportional** timeout (not a flat 30s, which would break large
  files) is the recommended next hardening step. Not implemented in this
  task to avoid "blindly increasing timeouts".
- **DB failure after a successful upload** is logged but still returns the
  success confirmation (the media *is* in Saved Messages). This matches the
  documented "don't pretend the save never happened" policy; if the team wants
  a "⚠️ saved to Telegram but DB record failed" message, that is a policy
  change, not applied here.
- **Animated GIF labeling:** animated GIFs are `video/mp4` with
  `DocumentAttributeAnimated`; `detect_media_type` labels them `"Video"`
  (cosmetic only — the animated attribute is preserved on re-upload).
- **`.save f` / `.save d` dot commands** are documented in
  `backend/bot/handlers/save.py`'s docstring but not registered; saving is
  currently panel-driven (`panel:save`) plus the AI `save` tool. Out of scope
  for this task.

## 11. Database / Schema Impact

- **None.** No schema changes. `save_type` (`"forward"`/`"deep"`) is the
  existing "mode" column. The payload already populates `save_code`,
  `origin_chat_id`, `origin_msg_id`, `saved_chat_id`, `saved_msg_id`,
  `file_id`, `mime_type`, `file_size`, `owner_id`, `created_at`, `caption`,
  `tags`, `media_type`, `sender_name`, `sender_id`.

## 12. Hard Constraints

- One authoritative Save Engine (`execute_save` / `execute_link_save`). No
  duplicate save pipeline was introduced.
- Deep Save MUST NOT call `forward_messages`/`ForwardMessagesRequest`.
- Forward Save stays a forward; it was not converted to deep.
- Single Telethon client; no new client/event loop; temp files cleaned up.

## 13. Validation

- `python -m py_compile backend/services/save_service.py` → OK.
- `pytest tests/test_12_save_engine.py -q` → **20 passed**.
- `pytest -q` (full suite) → **200 passed**, 0 failures.
- Tests now cover: deep save never forwards; deep download failure never
  forwards; forward save still forwards and attaches the caption; text-only
  deep save; photo/video/voice/sticker/document attribute preservation;
  metadata from the NEW upload; temp-file cleanup on success and failure;
  friendly mode-name normalization.

## 14. Unknowns / Missing Evidence

- **No live Telegram validation** was possible (no credentials/session in this
  environment). Re-upload media-type behavior is verified against the
  installed Telethon 1.34 source and mock-client tests, **not** a live chat.
- The exact production log line that showed `ForwardMessagesRequest` could not
  be replayed; it does not correspond to the current `mode="d"` code path.
