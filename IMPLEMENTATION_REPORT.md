# IMPLEMENTATION_REPORT.md — Save: album as ONE item, `tg://` links, real progress

> This file describes ONLY the current state of the repository for this task and
> **replaces every previous report in full**. The previous Save round
> (`577a65e`, "save full albums, refuse ephemeral media, report live progress")
> is superseded — its album model (one upload, one code and one row PER member)
> and its progress wiring were both wrong. Older reports remain readable in git
> history and in `docs/investigations/`.

| Item | Value |
|---|---|
| Stage | Corrective implementation + regression tests |
| Date | 2026-10-22 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`) |
| Base commit | `577a65e35cf295c127c3304b4bd79ae7be91e7c7` (the superseded Save round) |
| Delivery | One commit; pushed fast-forward to `origin/main` (SHA + remote equality reported in the delivery message, verified with `git fetch` → `git rev-parse origin/main` → `git merge-base --is-ancestor`) |
| Live Telegram / Supabase / Render | **NOT touched.** No session, no RPC, no SQL, no migration, no schema change, no `.env`, no credentials, no deployment |
| Document NOT modified | `DATABASE_ARCHITECTURE.md` (read only) |
| Interpreter used for verification | CPython 3.12 venv with `backend/requirements.txt` installed, i.e. **pinned Telethon 1.43.0** |

---

## 1. What was wrong (source-verified, not assumed)

* **Album ≠ one item.** `execute_save` resolved the album but looped
  `_deep_save_one` per member: N save codes, N Saved Messages messages, N
  `saved_items` rows, N metadata captions. The owner's requirement is the
  opposite: an album is ONE saved item.
* **`tg://openmessage` was unsupported.** `parse_telegram_link` only ran an
  HTTP(S) `t.me` regex, so `tg://openmessage?user_id=…&message_id=…` parsed to
  `(None, 0, 0)` and always failed.
* **Progress could never appear.** `_progress_reporter()` edited the panel
  message through `get_client()` — the **helper bot**. The panel message is sent
  by the OWNER'S account (an inline result the self-bot clicked), so a bot
  cannot edit it; every edit raised, was logged as a warning and swallowed, and
  the terminal result edit failed the same way. The passing unit tests only
  exercised the reporter with an in-memory edit callable, never the real
  authority.

## 2. Pinned Telethon 1.43.0 facts the fix relies on (source read, then exercised)

* `send_file(entity, [..])` routes to `_send_album`, which converts each
  `InputMediaUploadedPhoto`/`InputMediaUploadedDocument` through
  `UploadMediaRequest` and sends ONE `SendMultiMediaRequest`; the caption list
  places the single caption on the FIRST media item.
* `send_file` with a list returns a LIST of `Message` objects (built from the
  request's `random_id`s) — possibly containing `None` for missing mappings.
* `_send_album`'s own `progress_callback` reports `(index + fraction, len(files))`
  — an aggregate across files, not per-file bytes. This is why the album path
  uploads each member with `client.upload_file` (real per-file byte callbacks)
  and sends already-uploaded handles, instead of passing a progress callback to
  the album send.
* `download_media` calls `progress_callback(bytes_written, file_size)` per chunk
  and awaits an awaitable result; `upload_file` does the same per part. Part
  size is 128 KiB up to 100 MB, so real intermediate percentages exist.
* `utils.get_input_media(InputMedia)` returns the instance unchanged, so a
  prebuilt `InputMediaUploadedPhoto`/`Document` passes through `_send_album`
  without re-uploading bytes and without losing its media type.
* `Message.grouped_id` is the album identity; Telegram caps an album at 10
  items, so a bounded id window around a member always contains the album.
* `forward_messages(entity, [ids], from_peer=…)` sends ONE
  `ForwardMessagesRequest`, so an album's members are forwarded together.

## 3. Implementation

### `backend/services/save_service.py`

* **Album = one item** (`_deep_save_album`, replacing the per-member loop and
  `_album_outcome`):
  * pre-flight, before ANY transfer/code/row: any view-once or expiring member
    refuses the WHOLE album; a member without media refuses; a member whose
    declared size exceeds the deep-save limit refuses;
  * ONE `get_next_save_code()`, one caption, one temp root directory;
  * per member, in album order: `download_media` (per-member progress) →
    validate (exists, non-empty, limit) → `client.upload_file` (per-member
    progress) → build `InputMediaUploadedPhoto` (photo preserved) or
    `InputMediaUploadedDocument` (original attributes/mime/filename, video
    `nosound_video`) → delete that member's temp file;
  * ONE `client.send_file("me", [media…], caption=…)` — a real grouped album;
  * the response must contain exactly N messages: fewer means the partial album
    is deleted again and the save reports failure (no row);
  * ONE `saved_items` row: `media_type = "Album"`, `file_size` = album total,
    `origin_*` = the album's first source message, `saved_msg_id` = the album's
    FIRST destination message, `saved_chat_id` = Saved Messages, no single
    `file_id`/`mime_type` (an album has neither);
  * `SaveOutcome` for an album: `saved` = whole album persisted, `save_code` =
    the album's ONE code, `media_type = "Album"`.
* **Caption policy** (`build_album_caption` / `album_source_text`): the LifeOS
  metadata block is written ONCE (it rides the first album message); the album's
  source text is its member captions in album order, each DISTINCT caption once,
  blank-line separated, bounded (600 chars) with an explicit `…`, and the whole
  caption is kept under Telegram's 1024-char limit. `build_caption` gained an
  optional `album_count` so the media line reads `Album · 3 items · size`.
* **`tg://openmessage`** (`parse_link_target` → `LinkTarget`, with
  `parse_telegram_link` kept as the tuple wrapper): `tg://` is parsed as a URI
  with `parse_qs`. Supported: `user_id` (private chat), `chat_id` (basic group →
  `-id`), `channel_id` (→ `-100id`), each with `message_id` or `post`. Exactly
  one peer parameter is required; a missing/duplicated/non-numeric/zero id is
  refused with a reason. `execute_link_save` fetches through the ordinary
  Telethon client (`get_messages(peer, ids=…)`, with ONE explicit
  `get_entity` fallback when a bare id is not in the session cache), then
  validates `message.id == requested id` and `message.chat_id == requested
  peer`; a mismatch is an honest failure. HTTP links are unchanged.
* **Destination album resolution** (`is_album_row`,
  `resolve_saved_album_messages`): an album row's anchor is re-read and its
  `grouped_id` used to resolve members inside the bounded window; a non-grouped
  anchor resolves to itself (single-item behavior untouched); a grouped anchor
  whose siblings are gone is an ERROR, never a silent single-file result.
* `resolve_album_messages` additionally logs and enforces Telegram's 10-item cap.

### `backend/services/retrieve_service.py`

* `do_retrieve`: for an album row, resolve every member and forward the complete
  id list in ONE request; caption metadata is edited on the first forwarded
  message only. Resolution failure → honest failure, nothing forwarded. A
  single-item row keeps the exact previous scalar call.
* `do_delete`: resolves album members BEFORE anything is deleted; failure deletes
  nothing and keeps the row; success deletes every member in one call.
* `do_change_file_name`: an album row is refused with a reason before any
  Telegram read (a file name belongs to one document; an album has none).
* `_type_icon`: albums use the new `sym.MEDIA_ALBUM` glyph.

### `backend/services/delete_service.py`

* `do_del_code`: the same album resolution before deletion; every member is
  deleted in one call, and an unresolvable album deletes nothing and keeps the
  row.

### `backend/bot/handlers/save.py`

* `_progress_reporter(self_client, chat_id, msg_id, title=…)` now edits the
  panel message with the **SELF** client through the standard panel render path
  (`render_edit` + `_progress_buttons`), matching every other Glass panel.
  `None` only when there is no self client or no panel coordinates.
* Reply Mode resolves the panel message with `resolve_callback_message(event)`
  instead of hoping for `event.message_id`; both handlers pass
  `reporter.handle` to the service and close with the terminal result; a
  fallback `_edit_status` uses the self client if no reporter exists.
* Save-by-Link title is `Save by Link`; the input prompt now documents
  `tg://openmessage?user_id=…&message_id=…`.

### `backend/helper/save_progress.py`

* A known size now emits its truthful phase-start line (0%) the moment the
  phase begins, then each milestone crossed; dedupe keys make that a single edit
  per phase. Unknown size keeps the indeterminate line. Terminal protection and
  edit-failure containment are unchanged.

### `backend/helper/symbols.py`, `backend/services/database_service.py`

* New `MEDIA_ALBUM` (`◰`) in the media category/spec map, mapped by the save,
  retrieve and database icon tables — no glyph literal in a handler.

### `backend/ai/tools/save.py`

* `SaveByLinkTool` only prefixes `https://` when the link has NO scheme, so a
  `tg://openmessage…` URI reaches the parser verbatim; descriptions document the
  supported link forms.

### `AGENTS.md`

* §6 documents the one-album contract, the destination-album resolution, the
  caption policy, the self-client progress authority, and Save by Link formats.

**Unchanged on purpose:** no SQL/schema/table/column, no new supervisor or
client, no forwarding in Deep Save, single-item result strings, AI tool
semantics (the tools still pass no progress observer).

## 4. Tests

Rewritten `tests/test_save_album_view_once_progress.py` — **55 tests**, with a
fake client that mirrors pinned Telethon's call surface (including
`upload_file` + one list `send_file` for a genuine album) and a REAL-Telethon
request-level test.

| Area | Representative assertions |
|---|---|
| Album contract | three-member album → exactly ONE `send_file` with a 3-item list, 3 `upload_file`s, ONE row, ONE code, `media_type=Album`, `saved_msg_id` = first destination message, total `file_size`, metadata block once on the first message, hashtags once; album order preserved on download and upload; repeated source caption collapsed, distinct captions kept in order; neighbours from another album/chat excluded; documents/videos keep attributes + filenames; non-album saves keep one message/one row |
| Failure/cleanup | download failure, upload failure, album-send failure → no row, no messages, no temp dirs; ephemeral or over-limit member → nothing transferred at all; Telegram creating fewer messages than files → partial album deleted again and reported failed |
| Real Telethon boundary | `client.send_file("me", [InputMediaUploadedPhoto×3], caption=…)` on real Telethon 1.43 produces exactly ONE `SendMultiMediaRequest` of three `InputSingleMedia` items whose media are `InputMediaPhoto`, caption on the first, ZERO `SendMediaRequest`, and returns three messages — i.e. an actual grouped album, not adjacent messages (offline: request objects faked, no network) |
| Retrieval / deletion | album code forwards `[601,602,603]` in one request and edits the first forwarded caption; single item forwards the scalar; unresolvable album → nothing forwarded; delete removes all members and the row; unresolvable album deletes nothing and keeps the row; album file-name change refused with zero Telegram calls; `resolve_saved_album_messages` uses `grouped_id` (not adjacency), handles single anchors and missing anchors |
| Links | the owner's exact URI parses to `(None, 5406707941, 209974)`; channel/`post` and group forms; seven invalid forms refused; public and `/c/` regressions; the exact URI resolves and saves; a linked album saves as ONE album/one row; missing message, lookup exception, id mismatch, chat mismatch, malformed URI all honest failures; view-once through a `tg://` link refused by the MEDIA policy (not the link format); `SaveByLinkTool` preserves the scheme |
| Progress | cadence thresholds at exactly 10 MiB; percentage from bytes only; reporter phase start/milestones/dedupe/unknown size/terminal protection; REAL handler wiring for Reply Mode, Save by Link, and an album (`File 1/3…3/3` with that member's own percentage); all edits use the panel's `(chat, msg)` and none use the reply's id; download/upload callbacks are non-None; edit failures still save; unknown size shows no invented percentage; observer exceptions do not abort the transfer; no panel message → no progress; 12 MiB transfer reports 20/40/60/80/100 in BOTH phases |

**Measured results (this workspace, pinned Telethon 1.43.0)**

| Run | Result |
|---|---|
| `tests/test_save_album_view_once_progress.py` | **55 passed** |
| Save/handler/retrieval/delete/symbol/AI-action subset (14 files incl. the focused file) | **425 passed** |
| Full suite `python -m pytest tests -q --deselect tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once` | **6607 passed, 27 skipped, 1 deselected** in 129 s |
| `python -m py_compile` on every changed module + test file | exit 0 |
| `git diff --check` | clean |

The single deselected test fails identically on unmodified `577a65e`
(verified by stashing this work and re-running): it is a pre-existing,
date-dependent task-scheduler bound failure unrelated to Save.

## 5. Not live-verified / limitations

* **No live Telegram verification.** No session or credentials exist in this
  environment. Nothing was downloaded, uploaded, sent, forwarded, deleted or
  edited against a real account; the album's grouped rendering, the panel edits
  and the `tg://` lookup are proven at the request/library level only.
* **No Render deployment** was triggered or claimed; deployment state must be
  read from Render itself.
* **No schema change was made or needed.** Because there is no column for an
  album's member count, the album's identity is its anchor message plus
  Telegram's `grouped_id`; a saved album whose siblings Telegram no longer
  returns is reported as unresolvable (retrieval/deletion refuse) rather than
  silently treated as one file.
* `tg://openmessage` with a `user_id` can only be resolved when that chat is
  already resolvable by the authenticated account (Telegram does not expose an
  arbitrary user id); the failure is reported honestly with Telethon's reason.
* A partially created album is deleted again; an album Telegram rejects wholesale
  leaves the uploaded (unsent) bytes in Telegram's cache, which expires by
  itself — no message and no row exist.
* The AI `SaveTool` / `SaveByLinkTool` still receive no progress observer (no
  status message exists for them) — unchanged by design.
* Album documents (PDFs etc.) are sent inside the grouped request; Telegram may
  render non-media documents outside the album grid. Media-type preservation is
  per member.
