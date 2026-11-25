# IMPLEMENTATION REPORT

**Reporting date:** 2026-11-25
**Delivery:** Outbound egress attribution, Render metric reconciliation, and the
cumulative 50/60/70/80/90% staged policy
**Parent commit:** `a3bc59f426f2bfef2b1c9146fed2d2d2f3ba645a` (verified equal to
`origin/main` before editing)
**Branch:** `freebuff/f5137e20b1c5761eea04f020`
**Delivery commit:** the single commit carrying this report (`git log -1`)
**Status:** **IMPLEMENTED · TESTED OFFLINE · NOT DEPLOYED FROM THIS WORKSPACE ·
NOT LIVE VERIFIED.**

No SQL ran, no Supabase schema/row was modified, no Render action was taken, and
no live Telegram/AI/Render API call was made from this workspace.

---

## 1. Premise correction (verified first)

The task described an existing `backend/observability/egress_attribution.py`,
`backend/services/render_bandwidth_service.py` and
`backend/services/render_bandwidth_sources_diagnostic.py`, with staged
restrictions already deployed and attribution logs already configured.

**Those files do not exist in this repository, at this commit or anywhere in its
history.** Verified with:

```
git log --all --oneline --grep='egress|attribution|render bandwidth' -i
git log --all --pretty=format: --name-only --diff-filter=A | grep -iE 'egress|attribution|render_bandwidth'
grep -rn "RENDER_EGRESS_ATTRIBUTION|render_bandwidth|egress_attribution" .
```

The only bandwidth implementation present was the fail-closed governor delivered
in `a3bc59f`: a 150 MB/day and 4 GB/month reservation ceiling with per-upload
governance. It had **no** staged ladder, **no** Render metric query, **no**
attribution counters, and **no** lifecycle summary. The described instrumentation
could not have been produced by this codebase; whether a different fork/commit
is deployed on Render cannot be verified from here (no Render credential is
present in this workspace).

This is the first confirmed root cause of “attribution logs were never observed”:
in this repository there was no attribution code to emit them.

## 2. Other confirmed findings

| # | Finding | Evidence |
|---|---|---|
| A | No HTTP measurement existed | 12 ad-hoc `httpx.AsyncClient/Client` construction sites; Supabase creates its own internal clients, so call-site-only instrumentation could not cover REST/Storage |
| B | Telethon's callback is not a wire meter | `telethon==1.43.0`, `client/uploads.py`: `pos += len(part)` happens **before** `await self(request)`; the callback fires only after a successful part. An RPC retry inside `_call` does not advance `pos` again, so retransmitted parts are invisible. A new `upload_file` invocation resets `pos` to 0 |
| C | No staged policy existed | only `usage_state()` (`normal` / `near_limit` / `exhausted`) with a 90% informational boundary; reservations were denied only at 100% |
| D | No monthly policy floor existed | monthly bytes were durable, but a partial/absent external metric could not be remembered once the process restarted |
| E | No scheduled/interactive distinction existed | the Taskloom coordinator and an interactive Save called the same service with no execution-scope signal |
| F | Render's docs define two different shapes | `/metrics/bandwidth` → array of `{labels:[{field,value}], values:[{timestamp,value}], unit}`; `/metrics/bandwidth-sources` → `{data:[{labels:{resource,trafficSource}, values:[{timestamp,value}]}]}`, **no unit**, hourly, data only from 2025-03-09 |
| G | Render's lookback is 30 days | a 30/31-day calendar month cannot be fetched in one request; a 31-day month is necessarily partial near its end |
| H | Existing lifecycle boundaries were sufficient | the heartbeat (30s) and `RuntimeSupervisor.stop()` already exist; no new scheduler was needed |

## 3. Changes implemented

### 3.1 `backend/observability/egress_attribution.py` (new)

- A **closed** source vocabulary: `telegram_upload`, `telegram_download`,
  `telegram_transport`, `ai_provider`, `ai_search`, `stt_provider`,
  `tts_provider`, `supabase_rest`, `supabase_storage`, `render_api`,
  `other_http`. An unknown source is refused; a URL, filename, chat id, prompt,
  token or arbitrary string can never become a label.
- Bounded process-local counters, UTC-month rollover, exact and estimated bytes
  kept separate, measured and unmeasured operations kept separate.
- Explicit states: `disabled`, `enabled_idle`, `enabled_recording`,
  `measurement_failure`, plus `coverage=unknown|complete|partial`. Every public
  function swallows its own failure.
- **One** sanitized startup line, **one** aggregate line per bounded interval
  (default 300s; env `RENDER_EGRESS_FLUSH_SECONDS`, clamped 30–3600), **one**
  policy line, **one** reconciliation line. No per-request/per-message logging.
- Flush is driven by the existing 30s heartbeat and by
  `RuntimeSupervisor.stop()`; no scheduler, loop, executor or worker was added.
- `record_telegram_download()` measures a path or buffer exactly once; a missing
  path is an unmeasured operation, never zero.
- `reconciliation()` excludes inbound Telegram downloads, keeps estimated bytes
  separate, and returns a neutral `unattributed_difference` with an explicit
  `difference_is_not_attribution=true`.

### 3.2 HTTP measurement (public httpx API)

`install_httpx_instrumentation()` wraps `httpx.Client.send` and
`httpx.AsyncClient.send` once at supervisor startup, only when attribution is
enabled. It:

- counts a **materialized** request body exactly; a streaming request uses
  `Content-Length` when present, otherwise it is unmeasured;
- counts a non-streaming response body that httpx has already read;
- wraps a streaming response in a counting proxy (`httpx.AsyncByteStream` /
  `SyncByteStream`) that observes the caller's own iteration — it never consumes
  the stream itself; a closed-but-unconsumed stream is unmeasured, not zero;
- classifies by a bounded host/path table (provider, search, Supabase REST vs
  Storage, Render API, audio transcription/speech), never by logging the URL;
- counts every attempted request body even when the request fails; no response
  bytes are invented.
- is idempotent and uninstallable; disabled attribution installs nothing.

Measurement class: HTTP counts are **decoded payload bytes**. Content encoding can
make them differ from wire bytes; this is documented, not hidden.

### 3.3 Telegram measurement (payload-level ceiling)

- Uploads are recorded **once per logical transfer** at the same boundary that
  already reserves them: voice note, single Deep Save, each album member, rename
  re-upload, AI result attachment. The grouped album send records nothing
  because it attaches handles uploaded earlier.
- Downloads are recorded at the supported boundary: the `telegram_api.media`
  facade, Deep Save (single and per album member, including a partial file on
  failure), rename, anti-edit snapshot, and the appearance bounded
  `iter_download`. Repeated downloads each count; a failed download with no file
  is unmeasured.
- Telethon 1.43.0 exposes **logical payload progress**, not wire bytes. No
  high-water callback counter was introduced (it would be wrong across retries
  for the reason in §2B). MTProto framing, ACKs and automatic retransmissions
  are therefore **unmeasured** and reported as such.

### 3.4 `backend/services/render_bandwidth_service.py` (new)

- Queries **only** the injected `RENDER_SERVICE_ID` at
  `GET https://api.render.com/v1/metrics/bandwidth`, with `RENDER_API_KEY`.
- Accepts the documented array shape; validates series identity, numeric values,
  timestamps (ISO or epoch), units (`byte`/`bytes`/`b` only), duplicate series,
  duplicate timestamps, malformed points and HTTP status.
- Stitches non-overlapping 7-day windows inside the current UTC month, clamped to
  Render's 30-day lookback. Full-month coverage is required for `complete`; a
  31-day month near its end is reported `partial` with `missing_start_seconds`,
  never silently complete.
- Never treats missing data as zero: a failed/unparsed response returns
  `bytes=None`. A partial measurement still returns its **known lower bound**.
- Response size is capped at 512 KB, timeout 10s, refresh every 30 min
  (`RENDER_BANDWIDTH_REFRESH_SECONDS`, clamped 300s–6h), one bounded log line.
- Persists the observed lower bound through the existing JSONB state
  (`policy.floor_bytes` on the existing `panel_settings` row — **no new table or
  column**) so a restart cannot drop below a tier this month already reached.
- Publishes the validated lower bound to the single governor; it never creates a
  second accounting store.

### 3.5 `backend/services/render_bandwidth_sources_diagnostic.py` (new, retained)

- Validates the documented `{data:[...]}` shape, the closed traffic-source
  vocabulary (`total`, `http`, `websocket`, `nat`, `privatelink`), hourly
  coverage, duplicates, malformed points and unknown source labels (counted,
  never printed).
- Reconciles component sources against `total` only for hours where **every
  present component** and the total carry data. It reports
  `matches`/`differs`/`insufficient`, never assumes they must be equal.
- Reports `value_unit=unspecified` because this endpoint has **no unit field**;
  its values are never summed with byte measurements from another endpoint.
- Runs at most every 6h on the existing heartbeat, is a no-op when Render is
  unconfigured, and emits one sanitized line. It is retained rather than removed:
  it is the only view that separates HTTP/WebSocket/NAT/PrivateLink consumption,
  and it is not yet live-validated.

### 3.6 Cumulative staged policy (`outbound_bandwidth_service.py`)

Every reservation now carries a bounded action. The first tier that denies an
action is:

| Tier | Effective monthly usage | Denied actions |
|---|---|---|
| 1 | ≥ 50% / 2,000,000,000 B | `scheduled_media` |
| 2 | ≥ 60% / 2,400,000,000 B | + `album_media` |
| 3 | ≥ 70% / 2,800,000,000 B | + `ai_media` |
| 4 | ≥ 80% / 3,200,000,000 B | + `reupload`, `storage_upload`, `media_transfer` |
| 5 | ≥ 90% / 3,600,000,000 B | + `interactive_media`, `voice` (media/file payloads are text-only) |
| 6 | ≥ 100% / 4,000,000,000 B | the existing hard monthly-limit reservation refusal |

Because denial is `tier >= min_tier`, a higher tier never re-enables a lower
tier's restriction. The absolute daily/monthly limits are checked **before** the
ladder, so a payload crossing 100% always reports the hard-limit error.

- `effective_bytes = max(local monthly, durable floor, Render lower bound)`.
  Render bytes are included even when untrusted/stale—a lower bound may only
  raise usage. Trust/coverage are reported separately, so an incomplete metric
  can never be presented as complete or used to lower a tier.
- The effective usage before each successful reservation is persisted as the
  month's `policy.floor_bytes`; `persist_render_floor()` does the same at refresh
  time, even when no transfer occurred. Writes are confirmed by re-reading the
  row.
- Unknown categories default to `media_transfer` (tier 4), never interactive.
  Unknown action names are refused with `ValueError`.
- **Scheduled scope**: `TaskExecutionCoordinator` wraps both `execute_calls`
  sites in `scheduled_media_scope()` (a per-task contextvar). Any reservation
  made during a scheduled task is reclassified as `scheduled_media`, so it
  cannot pass as an interactive one.
- Text messages do not reserve; short owner-requested text remains available.
  There is no unaccounted media path: every existing Telegram upload and Storage
  upload site still reserves before transmission.

Call-site actions: voice → `voice`; single Save → `interactive_media`; each
album member → `album_media`; rename → `reupload`; AI result attachment →
`media_transfer`; Gemini OCR/STT → `ai_media`; appearance/anti-edit Storage →
`storage_upload`.

### 3.7 Diagnostics (sanitized, bounded)

Emitted on the existing lifecycle:

```
EGRESS_ATTRIBUTION_STARTUP status=enabled|disabled flag=... flush_s=... ...
EGRESS_ATTRIBUTION_SUMMARY status=... coverage=... month=... interval_s=...
  measured_bytes=... measured_outbound_bytes=... measured_inbound_bytes=...
  estimated_bytes=... operations=... unmeasured_operations=... failures=...
  sources=name:bytes/ops/uN,...
BANDWIDTH_POLICY_SNAPSHOT month=... tier=... effective_bytes=...
  local_month_bytes=... render_bytes=... floor_bytes=... render_trusted=...
  render_coverage=... render_latest_data_at=... snapshot_age_s=...
  denials=action:count,... available=...
EGRESS_RECONCILIATION render_outbound_bytes=... measured_outbound_bytes=...
  estimated_bytes=... unmeasured_operations=... unattributed_difference=...
  render_trusted=... render_coverage=... difference_is_not_attribution=true
RENDER_BANDWIDTH_METRIC status=... month=... bytes=... trusted=...
  coverage=... unit=... latest=... missing_hours=... duplicates=...
  windows=ok/total http_status=... detail=...
RENDER_BANDWIDTH_SOURCES status=... month=... coverage=... hours=...
  missing_hours=... duplicates=... unknown_source_series=... sources=...
  missing_series=... reconciliation=... difference_bytes=...
  value_unit=unspecified ...
OUTBOUND_BANDWIDTH_POLICY_DENIED action=... tier=... min_tier=...
  effective_bytes=... local_month_bytes=... render_bytes=... floor_bytes=...
```

No filename, URL, query string, prompt, chat id, user id, token or credential is
logged. Denials are logged only when they occur, with a bounded action category.

## 4. Attribution coverage

| Source | Measured? | Class |
|---|---|---|
| Telegram uploads | Yes, once per logical transfer | exact payload (not wire) |
| Telegram downloads | Yes (inbound) | exact payload / partial file |
| Telegram/helper MTProto transport | No | unmeasured |
| AI provider HTTP | Yes (request + response bodies) | decoded payload |
| AI search (You.com) | Yes | decoded payload |
| STT/TTS provider HTTP | Yes (host/path classification) | decoded payload |
| Supabase REST | Yes (internal httpx clients, global hook) | decoded payload |
| Supabase Storage | Yes | decoded payload |
| Render API | Yes | decoded payload |
| Other known HTTP | Yes, `other_http` | decoded payload |
| FastAPI server responses (`/health`, dashboard, guide, appearance renditions) | No | unmeasured |
| MTProto framing/ACKs/retransmissions | No | unmeasured |
| Transport-level HTTP retries inside httpcore | No | unmeasured |
| Content-encoding wire delta | No | decoded bytes only |

## 5. Tests and exact results

New suites:

- `tests/test_egress_attribution.py` — closed vocabulary, disabled state, idle,
  recording, measurement failure, partial coverage, exact/estimated separation,
  month rollover, startup/flush cadence, sanitized logs, reconciliation,
  Telegram download path/missing/repeated, HTTP request body, non-streaming
  response, consumed stream, unconsumed stream, unknown-length request, failed
  request, bounded classification, no sensitive values, idempotent install.
- `tests/test_render_bandwidth.py` — documented total array shape, 30-day month
  complete, 31-day month partial (not trusted), missing hour, duplicate series,
  invalid unit, unexpected HTTP 200 payload, HTTP 500, unconfigured no-request,
  documented sources object shape, missing source series, missing hours,
  duplicates, unknown traffic source, malformed payload, pre-2025-03-09
  unsupported, unit unspecified.
- `tests/test_bandwidth_policy.py` — 50/60/70/80/90/100 boundaries, per-tier
  actions, cumulative restrictions, unknown category/action, scheduled scope
  override, durable floor with no local usage, partial metric lower bound,
  previous-month ignore, stale snapshot lower bound with lost trust, month
  rollover, malformed policy block, hard limits still apply, denial counts,
  unavailable snapshot, effective usage in the read API, Render-floor persistence
  without a transfer.

Extended existing suites: `test_12_save_engine.py`,
`test_save_album_view_once_progress.py`, `test_save_v2_telegram_sync.py`,
`test_outbound_upload_governance.py` (reservation fakes accept the new bounded
`action` keyword; assertions unchanged).

Exact commands and results:

```
.venv/bin/python -m pytest -q tests/test_egress_attribution.py \
  tests/test_render_bandwidth.py tests/test_bandwidth_policy.py
→ 81 passed

.venv/bin/python -m pytest -q <new + governor + upload governance + panel +
  save/album/rename suites>
→ 281 passed

.venv/bin/python -m pytest -q <appearance, anti-edit, media, STT, TTS, task,
  save V2 and retrieve suites>
→ 1257 passed, 1 failed (the pre-existing time-dependent
  test_task_scheduler::test_recovered_interrupted_occurrence_is_picked_up_by_run_once)

.venv/bin/python -m pytest -q
→ 8118 passed, 27 skipped, 4 failed (175.25s)

.venv/bin/python -m compileall -q backend
→ exit 0

git diff --check
→ exit 0
```

The four full-suite failures are the same four documented in the parent
delivery and are unrelated to this change: three documentation-inventory tests
over `DATABASE_ARCHITECTURE.md` (untouched) and the time-dependent scheduler
test above. They were reproduced on a clean worktree at the parent commit in the
previous delivery; the base worktree was not re-run this time.

## 6. Delivery and verification

- Parent `origin/main`: `a3bc59f426f2bfef2b1c9146fed2d2d2f3ba645a`.
- One commit on `freebuff/f5137e20b1c5761eea04f020`, pushed to `origin/main`
  without force/rebase/reset.
- `origin/main` is verified equal to the delivery commit after the push
  (`git rev-parse HEAD`, `git rev-parse origin/main`, `git merge-base
  --is-ancestor`), and the working tree is clean apart from ignored dependency
  caches. The concrete SHA and remote state are stated in the delivery message.

## 7. Deployment / live verification status

**NOT DEPLOYED FROM THIS WORKSPACE; NOT LIVE VERIFIED.**

- No `RENDER_API_KEY` or `RENDER_SERVICE_ID` is present in this workspace, so the
  deployed commit and the live Render metric cannot be queried.
- No Render shell access exists on Free; no shell access was suggested or used.
- No live Telegram, AI, STT/TTS or Supabase call was made.

The owner can confirm, in the live deployment, without any code change:

1. The startup line says `EGRESS_ATTRIBUTION_STARTUP status=enabled` (or
   `disabled` if `RENDER_EGRESS_ATTRIBUTION` is not actually set on the service).
2. `RENDER_BANDWIDTH_METRIC` appears with `status=ok|partial|error|unconfigured`
   and `bytes=` a number or `unknown`—never a fabricated zero.
3. `RENDER_BANDWIDTH_SOURCES` appears at most every 6h with
   `value_unit=unspecified` and a `reconciliation=` result.
4. `EGRESS_ATTRIBUTION_SUMMARY` appears every ~5 min; a small upload increments
   `telegram_upload` bytes/ops. Silence is itself a finding: if the line is
   absent, the deployed code is not this commit or the heartbeat is not running.
5. `EGRESS_RECONCILIATION` shows Render vs measured bytes and a neutral
   difference.
6. `BANDWIDTH_POLICY_SNAPSHOT` shows `tier`, `render_trusted`,
   `render_coverage`, `floor_bytes` and `denials`.
7. The existing Settings → Bandwidth Usage panel still reads `available` (the
   panel was not changed).

What cannot be live-verified here: that Render's metric unit is actually `byte`
for this service, that the API returns the documented shapes for this account,
that the sources endpoint is available for the current month, and that a real
staged denial fires exactly at a production threshold. Tests simulate all of
these; only production can confirm them.

## 8. Remaining blind spots and risks

1. **The exact cause of the already-consumed ~3.8 GB is not identified.** The new
   measurements can identify future high-bandwidth sources; they do not recover
   or prove the past cause.
2. **The governor does not cap Render egress.** It caps governed payload
   reservations (150 MB/day, 4 GB/month). Server HTTP responses, MTProto
   overhead/retransmissions, helper-bot traffic, transport retries and decoded-
   vs-wire differences remain outside the cap. The staged ladder reduces
   avoidable media egress; it cannot guarantee the Render meter stays below 4 GB.
3. **31-day months are partial by API construction** near month end. The durable
   floor keeps the highest observed tier, but if Render cannot return the first
   day of a 31-day month, that day's bytes are not known—only conservatively
   bounded by local accounting and the floor.
4. **No live validation of Render response shape/unit.** Unsupported units and
   malformed/duplicate data are refused as untrusted rather than guessed.
5. **Cross-process atomicity is unchanged.** One process-wide `threading.Lock`
   serializes reservations; two concurrent processes could still lose an
   increment. Keep one Render instance.
6. **Telegram upload retries are not byte-accounted.** Payload bytes are exact at
   the logical boundary; re-sent MTProto parts are not visible through the pinned
   Telethon API.
7. **Attribution depends on `RENDER_EGRESS_ATTRIBUTION` being set in Render.**
   The repository cannot prove it is set; the startup line is the proof.
8. **No new table/column/SQL/migration was added.** The policy floor is an
   additional key inside the existing `outbound_bandwidth_state` JSONB value.
