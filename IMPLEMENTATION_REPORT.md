# IMPLEMENTATION_REPORT.md — Save: albums, view-once refusal, live transfer progress

> This file describes ONLY the current state of the repository for this task and
> **replaces every previous report in full** — it is not an appended report.
> Earlier reports (the AED reaction-only fix at `f5b6862`, the AED scope audit
> at `ed22a6d`, the lifecycle-task stall round, the Supabase transport-evidence
> round) remain readable in git history and in `docs/investigations/`.

| Item | Value |
|---|---|
| Stage | Scoped feature + defect hardening + regression tests |
| Date | 2026-10-21 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`) |
| Branch | `feat/private-message-phase2` (local HEAD == `origin/main` @ `f5b6862` at task start) |
| Base commit | `f5b68620188c9fa243e0ae2317d472b548974e40` |
| Delivery | One commit carrying this report, pushed fast-forward to `origin/main` (SHA and remote equality reported in the task delivery message and verified with `git fetch` → `git rev-parse origin/main` → `git merge-base --is-ancestor`) |
| Live Telegram / Render / Supabase | **NOT touched.** No session, no RPC, no deployment, no `.env`, no credentials, no SQL, no migration, no schema change |
| Workspace interpreter | CPython **3.10.12**, workspace **Telethon 1.34.0**, pinned **Telethon 1.43.0** (`backend/requirements.txt`); the pinned wheel was downloaded from PyPI and its callback/album code inspected directly |
| Documents NOT modified | `DATABASE_ARCHITECTURE.md`, `INVESTIGATION.md`, `ROADMAP.md`, `supabase/**` |

---

## 1. Objective

Three narrowly scoped Save improvements, all through the existing authoritative
Deep Save pipeline (`backend/services/save_service.py::execute_save`):

* **A — Albums**: saving a selected album member must save the intended album,
  not one item; no duplicates, no unrelated neighbours, no forward, honest
  partial failure, per-item metadata and cleanup.
* **B — View-once**: one-time / expiring media must be refused deterministically
  BEFORE any download or upload, with no success record and no bypass.
* **C — Progress**: every user-facing Save flow that transfers media must show
  live download/upload progress on its existing status message, using the
  model tester's five-segment bar and a size-driven milestone cadence.

Out of scope, unchanged: AI reasoning/tool semantics/provider behavior, private
`t.me/c/...` link investigation, SQL/Supabase/schema, Render.

---

## 2. Source audit (read before any edit)

**Repository** — `git status`, branch, HEAD, `origin/main` and the log were
inspected first. HEAD equalled `origin/main` at `f5b6862`; the tree was clean.

**Application code** — `AGENTS.md`, `IMPLEMENTATION_REPORT.md`,
`backend/services/save_service.py`, `backend/bot/handlers/save.py`,
`tests/test_12_save_engine.py`, `backend/bot/handlers/ai_test_progress.py`,
`backend/helper/panels.py` (`_safe_edit`, `_edit_panel_message`,
`_progress_buttons`), `backend/helper/panel_render.py`,
`backend/helper/inline_sender.py` (input listener),
`backend/helper/input_state.py`, `backend/helper/inline_engine.py`,
`backend/helper/symbols.py`, `backend/ai/tools/save.py`,
`backend/ai/tools/base.py`, `backend/services/retrieve_service.py`, plus every
caller of `execute_save` / `execute_link_save` (grep: save handler, AI
`SaveTool`, `SaveByLinkTool`, tests).

**Pinned Telethon 1.43.0** (wheel unpacked, not assumed):

* `client/downloads.py` — the download loop calls
  `progress_callback(f.tell(), file_size)` and awaits the result when it is
  awaitable.
* `client/uploads.py` — upload parts call
  `await helpers._maybe_await(progress_callback(pos, file_size))`.
* `tl/custom/message.py` — `Message.grouped_id` is the album identity.
* Telethon caps an album at 10 media items, so a bounded id window around the
  selected message always contains the whole album.

**Verified UI facts**

* Both user-facing Save flows (`Reply Mode`, `Save by Link`) are input handlers
  inside the Glass UI. Their status message is the panel message passed to the
  handler as `inline_chat_id` / `inline_msg_id`; the existing path edits it via
  the helper bot (`get_client().edit_message(...)`).
* Without a helper bot there is no inline panel message at all, so those
  handlers already had no status surface. Progress is therefore reported only
  where a status message actually exists; no new UI is invented.
* The model-tester bar is `render_progress_bar` in
  `backend/bot/handlers/ai_test_progress.py` — five segments, 20 points each,
  `▰`/`▱` from `backend/helper/symbols.py`, fill `int(fraction * 5)`.

**Applicable transfer flows found:** Reply Mode and Save by Link. The AI
`SaveTool` / `SaveByLinkTool` share the pipeline but have no status message
(their contract is deliberately unchanged), and retrieval re-sends a stored
asset rather than performing a Deep Save, so neither is a progress surface.

---

## 3. Implementation changes (exact)

### `backend/services/save_service.py` — the one pipeline

* **Album resolution** — `album_group_id` / `resolve_album_messages`. A
  non-album message resolves to itself with no extra RPC. A grouped message is
  resolved by reading the bounded id window (`selected ± (ALBUM_MAX_ITEMS - 1)`,
  at most 19 ids), keeping only messages whose `grouped_id` **and** chat match
  the target, deduping by message id, ensuring the selected message is present,
  and ordering by id. Neighbouring messages from other albums/chats are never
  included. A resolution failure returns an honest error — it never falls back
  to silently saving one item.
* **Per-item pipeline** — the previous single-item body became
  `_deep_save_one`, preserving exactly: download → validate (exists, non-empty)
  → upload as a NEW Saved Messages message → extract the NEW message metadata →
  insert one `saved_items` row → log. Each album member gets its own save code,
  caption (its own source id/caption appended), new message id, row, and
  isolated temp directory removed in `finally`.
* **Honest aggregation** — `_album_outcome`: `✓ Album saved — N of N items`
  only when every item persisted; `◬ Album saved — N of M items` with the
  failing item numbers and bounded reasons otherwise; `× Album save failed —
  0 of M items` when nothing persisted. `SaveOutcome.saved` means the whole
  album; `save_code` is empty unless exactly one item saved.
* **Ephemeral gate** — `media_ttl_seconds`, `ephemeral_media_kind`,
  `ephemeral_refusal`. `ttl_seconds == 2147483647` is the view-once sentinel;
  any other positive TTL is an expiry timer. Both are refused at the very top of
  `_deep_save_one`, before a save code, caption, download, upload or DB write.
  No Telegram protection is bypassed and no workaround exists.
* **Progress contract** — `SaveProgressEvent` (`phase`, real
  `completed_bytes`/`total_bytes`, `item_index`, `item_count`, `finished`) and
  an optional `progress` observer on `execute_save` / `execute_link_save`.
  `_emit_progress` swallows reporting failures and re-raises cancellation. The
  download/upload phase boundaries emit a start (0 bytes) and a finished event,
  so an unknown-size or callback-less transfer still yields a deterministic
  sequence. The service never imports the Glass UI.

### `backend/helper/progress.py` (new)

The shared five-segment bar, moved out of the model-tester handler so both
surfaces use ONE renderer. `ai_test_progress` now imports it (the public
`render_progress_bar` name and behavior are unchanged).

### `backend/helper/save_progress.py` (new)

`SaveProgressReporter` binds ONE status message to ONE save operation:

* cadence from the transfer's actual size — `50/100` at or below 10 MiB,
  `20/40/60/80/100` above it, independently per phase;
* unknown size → one phase line without a percentage, then a truthful 100% at
  the phase boundary (never treated as a small file);
* milestones/repeated callback values/identical renders are deduped; at most
  five progress edits per phase per item;
* Telegram edit failures are logged and swallowed;
* `close(text)` marks the reporter terminal BEFORE the final edit, so a late
  progress event can never overwrite the result.

### `backend/bot/handlers/save.py`

Both `_save_reply_wait_handler` and `_save_link_input_handler` build the
reporter from their existing panel message and pass `progress=` into the
service; the final success/failure text goes through the same reporter
(`close`). With no helper/status message the previous contract is preserved
exactly (no progress, same result edit fallback).

### `backend/bot/handlers/ai_test_progress.py`

Only the renderer import changed — the tester's rendering, coalescing and
terminal semantics are untouched.

### `AGENTS.md`

§2 helper layout and §6 Save System now document albums, the ephemeral gate,
progress and the shared bar.

**Unchanged on purpose:** no second scheduler/executor/client/update loop, no
new table or column, no SQL/Supabase access, no AI prompt/tool/provider change,
no forwarding anywhere, and the single-item result strings remain byte-identical.

---

## 4. Tests (real Telethon TL media types, real service, real handlers)

New file `tests/test_save_album_view_once_progress.py` — **27 tests**.

| Area | Tests | Asserts |
|---|---|---|
| Album | 7 | all members saved in id order; per-item source id, destination id (`601/602/603`) and code; unrelated album/chat neighbours excluded; duplicate window entries deduped; partial download and partial upload report `◬ N of M` with failing item and only successful rows; total failure reports 0 of M; resolution failure performs no transfer; a non-album message never fetches neighbours; no forwarding; temp dirs cleaned on success and both failure paths |
| View-once | 5 | sentinel detected as `view_once`, a 30s TTL as `expiring`, ordinary media as neither; view-once refused with **zero client calls** and zero rows; expiring media refused with its own reason; ordinary photo and document still save; a view-once album member fails without blocking siblings and is never downloaded |
| Progress | 12 | shared bar equals `ai_test_progress.render_progress_bar` for eight fractions, always five segments of `▰`/`▱`; size cadence boundaries at exactly 10 MiB; percentage from callback bytes; small file emits `Downloading/Uploading 50%` and `100%` in phase order; a 12 MiB declared download emits 20/40/60/80/100 while the small local upload keeps 50/100 (independent phases); album edits name `File 1/3…3/3` and never combine files; repeated values never repeat an edit; an edit failure still saves and records; a closed reporter ignores late events; unknown size emits one line without `%` then a truthful 100%; callback-less transfer still ends with the correct terminal result |
| Handler wiring | 4 | Reply Mode and Save by Link edit exactly one `(chat, msg)` pair, show both phases and end with the success result; Save by Link on an album saves all members with per-file progress; no-helper path keeps the old contract and records the save |

**Measured results**

| Run | Result |
|---|---|
| New focused file (`tests/test_save_album_view_once_progress.py`) | **27 passed** |
| Save + handler + panel + tool selection (19 files, incl. the new file) | **637 passed** |
| Full suite (`python3 -m pytest -q`) | **6540 passed, 27 skipped, 40 failed** |
| `python3 -m py_compile` on all changed modules and the new test | exit 0 |
| `git diff --check` | clean |

**The 40 full-suite failures are pre-existing and unrelated:** every one is in
`tests/test_colored_buttons.py`, which needs `Button.inline(style=...)` — the
workspace has Telethon **1.34.0** while the project pins **1.43.0**. The
baseline on this tree was 6513 passed / 40 failed; the diff adds exactly the 27
new passing tests and no new failure.

---

## 5. Not live-verified / limitations

* **No live Telegram verification.** No session or credentials were available;
  no `download_media`, `send_file`, album fetch or panel edit was performed
  against a real account. The owner should confirm on a live session: an album
  saves every item with per-file progress, a view-once photo is refused with no
  download, and link/reply progress edits the panel in place.
* **No Render deployment** was performed or claimed.
* Album resolution depends on Telegram's 10-item album cap. A hypothetical
  larger album would be saved as the bounded window; the API does not allow one.
* A partial album run does not roll back already-saved items (honest partial,
  each with its own row/code); re-saving duplicates would be a new owner action.
* The AI `SaveTool` / `SaveByLinkTool` intentionally receive no progress
  observer: they have no status message, and inventing one would change their
  contract — which this task explicitly keeps out of scope.
* Without a helper bot there is no inline panel message, so those flows run
  without progress (their pre-existing fallback behavior).
* Private `t.me/c/...` link resolution was not investigated beyond running the
  existing parser unchanged (explicitly out of scope).
