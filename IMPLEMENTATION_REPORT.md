# IMPLEMENTATION REPORT

**Reporting date:** 2026-11-23
**Delivery:** Emergency outbound bandwidth containment — fail-closed governor and
removal of every verified upload bypass
**Parent commit:** `77e451be69c222bb3a43f7bd3c53a5d44ae775cc` (verified equal to
`origin/main` by `git fetch origin main:refs/remotes/origin/main` +
`git rev-parse origin/main`; the local branch was fast-forwarded to it before
editing)
**Branch:** `freebuff/f5137e20b1c5761eea04f020`
**Delivery commit:** the single commit carrying this report (`git log -1`)
**Status:** **IMPLEMENTED · TESTED OFFLINE · NOT LIVE VERIFIED · NOT DEPLOYED.**
No SQL was executed, no Supabase schema or row was modified, no Render action
was taken and no live provider/Telegram call was made from this workspace.

---

## 1. Verified root causes

All six were read from the source at the parent commit; the previous report was
not trusted.

| # | Root cause | Evidence in source |
|---|---|---|
| A | A reservation could succeed with **no durable accounting** | `outbound_bandwidth_service._read_row_state()` returned `{}` when `_db_configured()` was false, and `reserve_outbound_bytes()` set `ok = True` on the same condition — a success that was never persisted |
| B | **Malformed persisted state was read as zero usage** | `_normalize_state()` returned `(0, 0)` for non-dict values and swallowed `int(...)` failures per window, resetting the counters |
| C | Three Telegram uploads treated the governor as **optional** | `telegram_api/media.py::send_voice` and both `save_service.py` sites used `if _outbound_bandwidth_service is not None` + `getattr(... callable ...)` |
| D | Two Telegram uploads had **no accounting at all** | `ai/tools/delivery.py::_send_text_attachment` (`sender.send_file`) and `retrieve_service.do_change_file_name` (`client.send_file("me", tmp_path)`) |
| E | The album reserved the **wrong event** and risked double counting | `_deep_save_album` reserved `total_size` before the grouped `send_file`, while the real transfer happens earlier in per-member `client.upload_file` |
| F | The Gemini helper **swallowed a missing governor** | `_reserve_gemini_bandwidth()` used `except Exception: return` / `if not callable(...): return`, and ran the async entry point with `asyncio.run()` from a worker thread |
| G | The **lock did not cover worker threads or a second loop** | `_lock` was an `asyncio.Lock`; `reserve_outbound_bytes_sync()` created fresh loops (directly or in a thread pool) |

`rate_limited`/provider behavior and every other subsystem were out of scope and
untouched.

## 2. Complete outbound upload audit (production code)

| Site | Mechanism | Classification | Disposition |
|---|---|---|---|
| `telegram_api/media.py::send_voice` | `client.send_file(BytesIO, voice_note=True)` | new byte transfer | reserved once, before the RPC; denial raises `TelegramAPIError` |
| `save_service.py::_deep_save_one` | `client.send_file("me", tmp_path)` | new byte transfer | reserved once with the downloaded `actual_size` |
| `save_service.py::_deep_save_album` | `client.upload_file(tmp_path)` per member | new byte transfer | reserved per member with that member's size |
| `save_service.py::_deep_save_album` | `client.send_file("me", [InputMediaUploaded*])` | reuse of uploaded handles | **no reservation** (no new bytes) |
| `ai/tools/delivery.py::_send_text_attachment` | `sender.send_file(BytesIO, force_document=True)` | new byte transfer | reserved once with `len(payload)` |
| `retrieve_service.py::do_change_file_name` | `client.send_file("me", tmp_path, ...)` | new byte transfer | reserved once with the downloaded size |
| `retrieve_service.py::do_retrieve` / `do_bulk_send` | `forward_messages` | Telegram-side forward, no local upload | unchanged |
| `retrieve_service.py` caption sync | `edit_message` | metadata only | unchanged |
| `appearance_service.py::_upload_sync` | Supabase Storage `.upload(key, data)` | external-provider upload | reserved via the sync entry point before transfer |
| `anti_edit_delete_service.py::store_media_snapshot` | Supabase Storage `.upload(...)` | external-provider upload | reserved before transfer; denial reports "not archived" |
| `gemini_media_engine.py` (`_run`, `_run_consensus`, `_run_dedicated_transcription`) | Gemini inline base64 / Files API upload | external-provider upload | mandatory sync reservation, conservative bound (base64 + 4096) |
| `save_service.py` text branch | `client.send_message` | text only | unchanged |
| `ai/tools/delivery.py` text paths | `edit` / `reply` | text only | unchanged |
| every `download_media(...)` | Telegram download | download only | unchanged |

`InputFile` / `InputMediaUploaded*` are constructed only where the album attaches
handles produced by its own `upload_file` calls.

## 3. Implemented protections

`backend/services/outbound_bandwidth_service.py`

- `_read_row_state()` now raises `OutboundBandwidthUnavailableError` when no
  database is configured — there is no reservation-without-accounting branch.
  Repository read exceptions, a missing row and a non-dict row are all normalized
  to the same typed denial.
- `_normalize_state()`/`_window_bytes()` are strict: `{}` is a real zero (the
  migrated column's fresh value); anything else that is not exactly
  `{"version":1,"daily":{period,bytes},"monthly":{period,bytes}}` is a denial.
  A previous day/month remains a legitimate rollover.
- `_reserve_locked()` performs read -> both limit checks -> write -> **confirm by
  re-reading the stored row**. A refused write, a raised write or an unconfirmed
  write denies after `_MAX_CONFIRM_ATTEMPTS` (3). Both limits are checked before
  any write.
- `_lock` is a `threading.Lock`, serializing the whole sequence across every
  event loop and worker thread; `reserve_outbound_bytes()` runs it via
  `asyncio.to_thread`, and `reserve_outbound_bytes_sync()` is now a genuine
  synchronous entry point (no `asyncio.run`, no new loop, no lock rebinding).
- `_validate_byte_count` rejects `0`, negatives, booleans and non-integers.
- `get_outbound_usage()` keeps `available: False` + `state: "unavailable"` +
  a `detail` for unconfigured, missing, malformed and unreadable state, so a
  panel never shows a fabricated zero.
- The dead `_memory_state` fallback was removed; the exact limits
  (`150_000_000` / `4_000_000_000`) are unchanged; `release_reservation` remains
  a no-op (reservations are conservative).

Upload boundaries

- `telegram_api/media.py`: the optional module import is gone. `send_voice`
  reserves before constructing the upload and converts any governor failure into
  `TelegramAPIError`, so the RPC is never attempted.
- `save_service.py`: optional import removed. Single uploads reserve
  `actual_size`; each album member reserves its own `member_size` before
  `upload_file`; the grouped send no longer reserves (it attaches existing
  handles).
- `ai/tools/delivery.py`: the attachment reserves `len(payload)` before
  `sender.send_file`. A denial therefore produces no upload; the existing honest
  fallback still delivers the content as text.
- `retrieve_service.py`: the rename re-upload reserves the downloaded file size
  before `send_file`; a denial returns the existing failure message and leaves
  the row and the old message untouched.
- `gemini_media_engine.py`: an unimportable governor or a missing sync entry
  point now raises instead of returning, so a remote payload can never start
  unaccounted. OCR/STT routing, retries, deadlines and representation choices
  are unchanged.
- `appearance_service.py` and `anti_edit_delete_service.py`: the two Supabase
  Storage uploads reserve their bytes before the object transfer.

No second client, runtime loop, scheduler, upload manager, table, column,
migration, accounting store or in-memory production fallback was added.
`RuntimeSupervisor`, Dispatcher, ToolExecutor, ProviderManager, TelegramAPI and
service-layer ownership are untouched.

## 4. Upload paths still not governed

These are genuinely outside the governor and are reported rather than claimed:

1. **HTTP response egress** — `/health`, the dashboard SPA and `/api/*`, the
   static guide site, and the appearance image renditions Telegram fetches.
2. **Telegram protocol traffic** — updates, RPC frames, keepalive pings, and the
   helper bot's own traffic.
3. **Supabase request/response bytes**, including the accounting write itself.
4. **Cross-process atomicity** — see §8.

No Telegram file-upload path and no Storage upload path remains ungoverned.

## 5. Tests and exact results

New suites

- `tests/test_outbound_bandwidth_governor.py` — no database, missing row,
  repository read failure, refused write, raised write, lost/unconfirmed write,
  daily limit, monthly limit, exact remaining capacity, period rollover, eleven
  malformed-state shapes, fresh `{}` as a real zero, invalid byte counts, and
  concurrent reservations against a patched budget.
- `tests/test_outbound_upload_governance.py` — voice reservation/denial/import
  failure, AI attachment reservation/denial/import failure, denied attachment
  falling back to text with zero uploads, ordinary text delivery untouched,
  Gemini import failure, missing sync entry point, conservative bound, OCR
  blocked before the provider, OCR unchanged on success.

Extended suites (real upload fakes record the RPC)

- `tests/test_12_save_engine.py` — single Deep Save reserves exactly the
  downloaded size; a denied reservation means no `send_file`.
- `tests/test_save_album_view_once_progress.py` — three members reserve exactly
  `[1000, 1000, 1000]`, three `upload_file` calls, one grouped `send_file`; a
  denied reservation means no upload and no send.
- `tests/test_save_v2_telegram_sync.py` — the rename re-upload reserves the
  downloaded size; a denial uploads nothing and deletes nothing.
- `tests/conftest.py` — an autouse fixture supplies a per-test persistence
  double for the governor (the suite has no Supabase, and production accounting
  is durable-only). It fakes ONLY `_db_configured` + the repository; the
  fail-closed contract is exercised against explicit fakes in the new governor
  suite.

Exact commands and results

```
.venv/bin/python -m pytest -q \
  tests/test_outbound_bandwidth_governor.py tests/test_outbound_upload_governance.py \
  tests/test_bandwidth_panel.py tests/test_12_save_engine.py \
  tests/test_save_album_view_once_progress.py tests/test_save_v2_telegram_sync.py \
  tests/test_save_v2_wiring.py tests/test_save_v2_management.py \
  tests/test_save_v2_metadata.py tests/test_save_v2_resolution.py \
  tests/test_save_reply_panel_edit.py tests/test_save_success_panel.py \
  tests/test_save_live_status_and_private_link.py tests/test_save_trace.py \
  tests/test_save_view_once_photo_semantics.py tests/test_saved_item_sender_and_retrieve.py \
  tests/test_tts_service.py tests/test_anti_edit_delete.py tests/test_appearance_*.py \
  tests/test_gemini_*.py tests/test_media_*.py tests/test_stt_*.py
→ 1793 passed, 2 skipped, 0 failed (55.28s)

.venv/bin/python -m pytest -q
→ 8038 passed, 27 skipped, 4 failed (173.84s)

.venv/bin/python -m compileall -q backend
→ exit 0

git diff --check
→ exit 0
```

The four full-suite failures are **pre-existing and unrelated**: they were
reproduced on a clean `git worktree add .basecheck HEAD` at the parent commit
(same four, exit 1) and touch documentation inventories and a time-dependent
scheduler test:

- `tests/test_database_architecture_doc.py::test_every_migration_file_is_classified_in_the_audit_section`
- `tests/test_database_setup_order.py::test_the_audited_order_is_recorded_in_the_audit_table`
- `tests/test_private_message_db_security.py::test_the_documented_counts_match_the_repository`
- `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`

No SQL ran, no live Supabase/Telegram/Gemini call was made, and the Render
service was not touched, so every statement here is offline evidence.

## 6. Commit and remote verification

- Parent `origin/main`: `77e451be69c222bb3a43f7bd3c53a5d44ae775cc`
  (fast-forwarded, no rebase/reset/force).
- Delivery: one commit on `freebuff/f5137e20b1c5761eea04f020`, pushed to
  `origin/main` without force. The SHA and the remote HEAD were verified after
  the push in the delivery message (`git rev-parse HEAD`, `git rev-parse
  origin/main`, `git merge-base --is-ancestor`).
- Working tree: no pre-existing work was discarded; the only untracked paths are
  the two new test files, which are part of this commit.

## 7. Deployment status

**NOT DEPLOYED / NOT LIVE VERIFIED.** Nothing was pushed to Render, no service
was restarted, no database row was written and no bandwidth panel was read. The
production guard only becomes effective once this commit is deployed to the
Render service and the owner confirms, in the live deployment:

1. the Bandwidth Usage panel reads `available` (not "unavailable"),
2. a small upload increments both counters,
3. with the `panel_settings` row temporarily unreadable, an upload is REFUSED.

## 8. Remaining risk against Render's 5 GB meter

The governor is not, and does not claim to be, a measurement of Render egress.

- **Already-consumed bandwidth is not recovered.** ~3.8 GB is spent; the daily
  and monthly budgets only bound the GOVERNED payload bytes from now on.
- **Unmetered egress remains** (§4): HTTP responses, Telegram protocol traffic,
  helper-bot traffic and Supabase traffic can still consume the Render meter.
  A 150 MB/day payload budget does not imply 150 MB/day of total egress, and the
  service can in principle approach suspension from traffic the governor does
  not see.
- **Conservative accounting** means failed/timed-out uploads keep their bytes,
  so real transfer can be lower than the counter but never higher for governed
  paths.
- **Cross-process race.** Within one process the serializer is complete. Two
  concurrent processes could interleave read-modify-write and lose an increment
  (worst case: two simultaneous uploads counted once). A database-side
  compare-and-set would be required to close it; no SQL/migration was created.
  Mitigation available to the owner without code: keep a single Render instance
  and avoid overlapping deploy windows during large transfers.
- **Governed paths are enforced** — if accounting is unavailable, the upload is
  denied. The service prefers refused uploads over unaccounted egress.


---

## Follow-up — Render platform bandwidth metrics (2026-11-23)

**Purpose:** distinguish the LifeOS upload governor from the actual outbound
bandwidth metric reported by Render.

- Added backend/services/render_bandwidth_service.py, which calls Render's
  GET /v1/metrics/bandwidth endpoint with the workspace-scoped RENDER_API_KEY
  and Render-injected RENDER_SERVICE_ID.
- The service ID is never accepted from a panel callback or user input. The
  API key is never logged or returned. A missing key, missing service ID,
  HTTP failure, malformed payload, unsupported unit, or mismatched resource
  label produces an explicit unavailable state rather than a fabricated zero.
- The Settings → Bandwidth Usage panel now displays two independent sections:
  LifeOS tracked payload reservations (150 MB/day, 4 GB/month) and Render's
  platform outbound-bandwidth metric. The text explicitly explains why the
  numbers differ.
- Render metric values are summed by UTC day and month from the hourly points
  returned for the current service. The latest datapoint and point count are
  shown; Render's hourly publication delay is disclosed.
- Account isolation: the key is scoped to one Render workspace and the
  resource ID comes from the currently running service's platform-injected
  RENDER_SERVICE_ID. A key scoped to another account cannot authorize this
  service's resource lookup. No account-1 service ID is hardcoded in source.
- No Supabase schema, migration, SQL, second scheduler, or second runtime was
  added. httpx was already a production dependency.

Tests added:
- tests/test_render_bandwidth_service.py — UTC aggregation, unit conversion,
  workspace/service scoping, missing key, mismatched resource, and HTTP error
  without credential leakage.
- tests/test_render_bandwidth_panel.py — dual-source rendering and
  independent failure handling.

**Verification status at report authoring:** source and tests in this follow-up
are not yet deployed or live-verified. Test execution is pending.
