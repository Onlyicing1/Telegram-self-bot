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

---

## Follow-up — Outbound egress attribution (2026-11-23)

**Purpose:** measure outbound application payload bytes at the boundaries the
runtime actually controls, so the owner can compare them with Render's NAT
metric over the same UTC window. This is MEASUREMENT ONLY: no optimization, no
enforcement and no behavior change.

**Base commit:** `3e35f085378a3f70b063d6e6609942bb09756103`
**Enable with:** `RENDER_EGRESS_ATTRIBUTION=1`. Every other value — unset,
`0`, `true`, `yes`, `" 1"`, `"1 "` — keeps the layer fully off. Disabled means
no hook, no callback, no counter and no log line: the transfer paths are
byte-for-byte the ones they were before.

### 1. Exact measurements

| Category (label) | Seam | What the number is |
|---|---|---|
| `telegram_self_upload` (`deep_save`, `album_member`, `voice`, `rename_reupload`, `ai_attachment`) | Telethon `progress_callback(current, total)` on the existing single-file `send_file` / `upload_file` calls (pinned Telethon 1.43.0 reports cumulative bytes) | File payload bytes of that logical upload, from positive deltas only. A restarted upload is not counted twice, so this is a LOWER bound on wire bytes. |
| `ai_provider` (`openai`, `gemini`, `you`, and the other OpenAI-compatible providers) | `httpx` request hook reading the prepared request's `Content-Length` | Exact HTTP request-body bytes (the JSON prompt payload). |
| `ai_discovery` (`openai`, `gemini`, …) | same hook, plus a per-request category override where a provider reuses its chat client for `/models` | Exact request-body bytes (0 for a GET). |
| `stt_provider` (`groq`, `speechmatics`, `gemini_media`) | same hook | Exact body bytes. Multipart framing is included because `Content-Length` describes the body httpx actually sends. |
| `tts_provider` (`gemini`, `openai`, `grok`, `speechmatics`) | same hook | Exact JSON request-body bytes. |
| `supabase_rest` (`postgrest`) | same hook, installed on the ONE HTTP/1.1 PostgREST session (`backend/db/transport.py`) | Exact PostgREST request-body bytes for every table/RPC call of the shared client. |
| `supabase_storage` (`storage_upload`) | explicit payload length at the two Storage upload call sites | Exact object payload bytes; the Storage request's multipart framing is not included. |
| `render_api` (`render`) | `httpx` request hook | Exact request-body bytes (a GET, so 0). |

`Content-Length` is read from the already-prepared request, so the body is
never consumed, re-serialized or buffered: streaming, retries and connection
lifecycle are untouched. `exact_payload_bytes` excludes TCP/IP, TLS and HTTP
headers, and an error response (>= 400) marks the same operation failed.

### 2. Estimated measurements

**None.** Every instrumented path exposes an exact body length, so
`estimated_payload_bytes` stays 0 and is never summed with an exact total. The
estimate channel exists only so a future path that can bound but not measure
its payload cannot silently corrupt an exact figure; a body of unknown length
(a chunked stream) is reported as unmeasured instead.

### 3. Paths still unmeasured

| Path | Why |
|---|---|
| Telegram MTProto traffic (RPC frames, TL/encryption overhead, keepalives, panel edits, reactions, connection setup, updates) | No supported, documented byte seam at the transport layer; a private-internals or global-socket counter would change protocol/lifecycle/retry behavior. |
| Helper-bot traffic | The helper bot performs no file uploads (panels are inline results and text edits), and its MTProto bytes are unmeasurable for the same reason. `telegram_helper` therefore stays at zero. |
| Supabase Storage request framing and other Storage/GoTrue HTTP calls | `storage3` builds its own session internally; the two upload payloads are measured explicitly instead, and the rest is reported as unmeasured rather than estimated. |
| HTTP responses served by this service (`/health`, `/api/*`, the SPA, `/guide`, `/appearance/image/*`) | They are egress, but they are response bodies rather than request payloads; dashboard polling and Telegram fetching the appearance rendition stay outside these counters. |
| TLS/TCP overhead, DNS, retransmissions | Not visible at the application seam; Render's NAT metric includes them. |

### 4. Log format and collection window

One summary at most once per 300 s, emitted lazily by the next recorded event
(no timer, no task, no scheduler), with at most one line per (category, label)
plus one totals line:

```
[RENDER_EGRESS] window=2026-11-23T10:00:00Z..2026-11-23T10:05:00Z category=telegram_self_upload label=deep_save exact_payload_bytes=1834221 estimated_payload_bytes=0 operation_count=2 failed_operation_count=0 unmeasured_operations=0 scope=process
[RENDER_EGRESS] window=2026-11-23T10:00:00Z..2026-11-23T10:05:00Z totals exact_payload_bytes=... estimated_payload_bytes=0 operation_count=... failed_operation_count=... unmeasured_operations=... note=application_payload_only_not_render_bandwidth
```

Only fixed category names, bounded labels, byte counts, operation counts and
UTC timestamps are emitted — never a URL, query string, id, filename, prompt,
message content, credential or header. Collect for at least one full UTC day
(ideally a week) after a restart so the comparison covers complete,
hour-aligned Render metric points.

**Counters live in this process only**: they reset on every restart/redeploy,
and a window whose activity never resumes is emitted only when activity next
arrives (or on an explicit `flush()`). A restart is a hard boundary in the
data; nothing is persisted.

### 5. Comparing with Render NAT

1. Pick a UTC window that starts AFTER the process started (check the
   deploy/restart time in the Render log); no data exists before the first
   `[RENDER_EGRESS]` line.
2. Sum the `exact_payload_bytes` fields for that window (the totals line
   repeats the sum).
3. Read Render's outbound/NAT metric for the SAME UTC window (Bandwidth panel
   or the Render dashboard). The metric is hourly and published with a delay,
   so the newest hour may still be incomplete.
4. Compare and expect `NAT >= measured`:
   `unexplained = NAT - exact_payload_bytes`. That difference is expected to
   contain TLS/TCP/IP overhead, HTTP headers, MTProto framing and encryption,
   all Telegram non-file traffic, HTTP responses served by this service, and
   every path listed as unmeasured above.
5. A measured category that is far larger than expected is a finding; these
   counters do not by themselves identify a root cause, and their sum is NOT
   Render bandwidth.

### 6. Verification

```
.venv/bin/python -m pytest -q tests/test_egress_attribution.py --asyncio-mode=auto
→ 36 passed

.venv/bin/python -m pytest -q --asyncio-mode=auto
→ 8124 passed, 27 skipped, 4 failed

.venv/bin/python -m compileall -q backend   → exit 0
git diff --check                            → exit 0
```

The four failures are the pre-existing, unrelated documentation-inventory and
time-dependent scheduler tests already recorded in §5 above; they are
unchanged by this work.

Nothing was deployed, no Render environment variable was changed, no SQL ran,
no Supabase schema or row was touched, and no live Telegram/provider/Supabase
request was made from this workspace.

### 7. Unchanged by this change

Governor limits and accounting semantics, ProviderManager fallback/retry/
cooldown, Telegram RPC behavior, feature availability, OCR/STT/TTS selection
and retry policy, Supabase schema, `DATABASE_ARCHITECTURE.md`, the one-shot
Render bandwidth-source diagnostic, and every other Render account. No second
scheduler, executor, update loop, runtime, socket monkeypatch or HTTP client
was added: the existing clients gain exactly one observer hook when the flag
is `1`.
