# IMPLEMENTATION REPORT

**Reporting date:** 2026-11-25
**Delivery:** Outbound-bandwidth attribution, Render coverage validation, and
staged monthly enforcement
**Branch:** `freebuff/f5137e20b1c5761eea04f020`
**Delivery commit:** the merge commit created from this worktree (`git log -1`)
**Remote parent:** `origin/main` at `b8f99af3bbbad3f43b3ba13e68a722402a9aeca9`
**Status:** **IMPLEMENTED · TESTED OFFLINE · NOT DEPLOYED · NOT LIVE VERIFIED.**

No SQL was executed, no Supabase row or schema was modified, no Render action
was taken, and no live Telegram/provider request was made from this workspace.

---

## 1. What happened

A parallel delivery landed on `origin/main` while this worktree was based on
`a3bc59f...`. That delivery already contained the staged 50/60/70/80/90% policy
and a first attribution pass. It was not overwritten. The two histories were
merged (`merge --no-ff`); the canonical remote implementation is the base and
this delivery adds the missing correctness work below. The merge preserves both
parents and does not rebase, reset, force-push, or rewrite either history.

The resulting delta against `origin/main` is 18 files: the attribution module,
the Render metric/source diagnostic, lifecycle wiring, download call sites, STT
reservations, focused tests, and this report.

## 2. Verified gaps fixed

| Gap in the canonical implementation | Fix |
|---|---|
| Aggregate summaries were emitted only by the **next recorded event**; an idle enabled process emitted nothing after its window | `heartbeat_tick()` runs on the existing 30s heartbeat: activity flushes at most every 5 minutes, and an idle enabled process emits one bounded `status=enabled_no_activity` line per hour |
| No startup confirmation and no shutdown flush | `RuntimeSupervisor.start()` calls `log_startup()`; `stop()` calls `shutdown()` before the DB executor is released |
| Only request `Content-Length` was measured; response bodies were intentionally ignored | Response hooks now measure bytes **consumed by the application as inbound**. Non-streaming bodies are counted once; streaming bodies are wrapped and counted only as consumed. Early close records consumed bytes plus `incomplete_inbound_streams` |
| Telegram downloads were not attributed | `record_telegram_download()` is wired at the Telegram API wrapper, Deep Save single/album downloads, rename, anti-edit snapshot, and appearance `iter_download` |
| Download payloads risked being reported as outbound | Downloads are recorded in `exact_inbound_bytes`, never `exact_payload_bytes`; MTProto request/ack egress remains unmeasured |
| No month-to-date application total to compare with Render | Process-local UTC month counters survive window flushes; the totals line reports `month_exact_payload_bytes`, `month_exact_inbound_bytes`, Render bytes, governor bytes, persisted floor, tier, trust, partial coverage, and a signed `unattributed_difference_bytes` |
| No explicit measurement state | `snapshot()["status"]` distinguishes `disabled`, `enabled_no_activity`, `recording`, `measurement_incomplete`, and `measurement_failure` |
| Render metric coverage/duplicates were not checked | Duplicate timestamps are rejected; `latest_age_seconds`, `distinct_hours`, `expected_hours`, `missing_hours`, `coverage_complete`, `monthly_days_covered`, and `monthly_month_days` are reported |
| Source diagnostic had a hardcoded past week and ran once | It retains the fixed forensic window for the original investigation but the heartbeat runs a **current UTC month-to-date** window at most once every six hours. Hourly output is capped to the latest 24 points |
| STT provider uploads checked the policy but did not reserve bytes | Both Groq and Speechmatics reserve `stt_upload` bytes after the provider-media policy check |
| Sample tests accepted an `action=` keyword that the canonical governor does not use | Unneeded fake changes were reverted; call sites use the existing category-based classification |

No new table, column, migration, scheduler, executor, update loop, hosted
telemetry, external account, or dependency was added. `httpx` was already a
project dependency and is used by the existing HTTP clients.

## 3. Attribution coverage

Closed category vocabulary, all process-local and content-free:

`telegram_self_upload`, `telegram_download`, `telegram_helper`,
`supabase_rest`, `supabase_storage`, `ai_provider`, `ai_discovery`,
`stt_provider`, `tts_provider`, `render_api`, `other_known_http`.

Measured outbound payload channels:

- Telegram uploads (`upload_file` / `send_file`) via Telethon's cumulative
  progress callback, counted once per logical payload. This is **payload-level,
  not MTProto wire bytes**.
- HTTP request bodies via `Content-Length` (JSON, raw bytes, form and multipart
  framing included) on the instrumented clients.
- Supabase Storage uploads at the two upload seams, recorded explicitly.
- AI provider, discovery, STT, TTS, Supabase REST, and Render API clients carry
  fixed category/label hooks.

Measured inbound channels (never added to outbound):

- Telegram download payloads, from a returned path or bytes.
- HTTP response bodies consumed by the application, including partial streams.

Unmeasured or only partially measured:

- MTProto framing, TL/encryption overhead, retransmissions, keepalives, update
  traffic, helper transport, and Telegram RPCs.
- HTTP headers, TLS/TCP, connection setup, and chunked/streamed request bodies
  whose length is unknown (counted as unmeasured operations, never guessed).
- Server-side FastAPI responses (`/health`, dashboard, guide, `/api/*`) and
  other traffic outside the instrumented service clients.
- Supabase Storage downloads/lists through the SDK's own internal client.
- A failed transfer may have sent bytes that never produced a completed
  payload; those retransmissions are not represented.
- The Render source diagnostic's own response is not attributed (inbound).

`exact_payload_bytes` and `estimated_payload_bytes` are separate channels and
are never summed. The estimate channel remains zero because no path currently
uses it. The reconciliation difference is explicitly **unattributed**: it can
contain transport overhead, retransmissions, instrumented gaps, or traffic from
another measured path. It is not claimed to be Telegram traffic or any provider.

## 4. Render monthly metric and source diagnostic

`backend/services/render_bandwidth_service.py` queries only the
Render-injected `RENDER_SERVICE_ID` with the workspace-scoped `RENDER_API_KEY`.
It reads the documented array of series (`labels`, `values`, `unit`), validates
the service label, units, finite non-negative values, timestamps with timezones,
duplicates, and the requested window. The supported lookback is 30 days; a
30/31-day calendar month can therefore be partial. `monthly_is_partial`,
coverage start/end, month days, and covered days are returned, and policy
reports `render_partial`.

A partial or stale reading cannot lower a previously persisted tier: the
governor evaluates `max(Render month, governor month, persisted floor)`, and a
floor write that cannot be confirmed makes media fail closed. A monthly metric
older than three hours or from another UTC month is not trusted. A partial
reading is reported as partial, not complete; the durable floor persists the
highest effective reading for the current UTC month. There is no second ledger
and no schema change; the floor is a `policy_floor` key inside the existing
`panel_settings.outbound_bandwidth_state` JSONB.

The temporary source diagnostic (`RENDER_BANDWIDTH_SOURCES_DIAGNOSTIC=1`) now
accepts both documented top-level shapes (`[...]` and `{"data": [...]}`), which
fixes the previous HTTP-200 `unexpected_payload` failure. It validates units,
duplicate timestamps, missing hours, unexpected sources, and part-vs-total
agreement without assuming the parts must equal the total. It is retained
rather than deleted because a hardcoded past week becomes ineffective after
Render's lookback; the heartbeat runs the current UTC month at most every six
hours, while the startup forensic call keeps the fixed investigation window.
No credentials, URLs, raw bodies, filenames, IDs, or user content are logged.

## 5. Staged policy verification

Thresholds remain exactly `50/60/70/80/90%` = `2.0/2.4/2.8/3.2/3.6 GB`, with
the hard monthly limit at `4,000,000,000` bytes and daily limit at
`150,000,000` bytes.

| Tier | Action classes denied |
|---|---|
| ≥ 50% | automatic media, scheduled media, unknown payloads |
| ≥ 60% | bulk/album/batch media |
| ≥ 70% | provider media (OCR/STT/AI media) |
| ≥ 80% | file uploads, reuploads, Storage uploads, media forwards |
| ≥ 90% | single/interactive media and files |
| ≥ 100% | hard monthly limit reservation refusal |

Restrictions are cumulative via `_ACTION_MIN_TIER`; a higher tier never
re-enables a lower-tier action. Interactive owner text remains available with a
32 KiB explicit text exemption; a larger text payload does not inherit it.
Unknown actions are never treated as text and are denied from 50%. A scheduled
occurrence is wrapped centrally in `automatic_media_scope()` around
`execute_calls`, so the 50% tier applies regardless of which tool runs. The
sync and async reservation paths use the same snapshot and lock. A stale or
untrusted snapshot denies media; text within the bound remains available. The
persisted floor prevents a restart or delayed/lower Render reading from lowering
the tier within the UTC month. Reservations are confirmed by re-reading the
stored state and fail closed on read/write/confirmation failure.

Verified upload seams: Deep Save single and album (per member), rename
re-upload, AI attachment, voice, Gemini OCR/STT, anti-edit Storage snapshot,
appearance Storage upload, and Groq/Speechmatics STT provider uploads. Telegram
forwards and metadata edits remain ungoverned because they do not create a new
local payload.

## 6. Tests and exact results

Focused suites (attribution, Render metric/source, policy, upload governance):

```
.venv/bin/python -m pytest -q tests/test_egress_attribution.py \
  tests/test_render_bandwidth_service.py \
  tests/test_render_bandwidth_sources_diagnostic.py \
  tests/test_outbound_bandwidth_policy.py \
  tests/test_outbound_upload_governance.py
→ 159 passed
```

Affected subsystem run (runtime wiring, save/retrieve/media/appearance/STT,
governor, panel, task execution):

```
.venv/bin/python -m pytest -q [20 affected suites]
→ 893 passed
```

Full suite:

```
.venv/bin/python -m pytest -q
→ 8189 passed, 27 skipped, 4 failed (154.08s)
```

The four failures are pre-existing and were reproduced on untouched
`origin/main` (`b8f99af...`):

- `tests/test_database_architecture_doc.py::test_every_migration_file_is_classified_in_the_audit_section`
- `tests/test_database_setup_order.py::test_the_audited_order_is_recorded_in_the_audit_table`
- `tests/test_private_message_db_security.py::test_the_documented_counts_match_the_repository`
- `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`

Compilation and diff checks:

```
.venv/bin/python -m compileall -q backend   → exit 0
git diff --check                            → exit 0
```

New coverage includes download path/bytes, repeated and missing downloads,
response bodies as inbound, full and early-closed streams, month totals after a
window flush, idle/activity heartbeat flushing, startup/shutdown sanitization,
measurement states, reconciliation fields, duplicate Render timestamps, coverage
reporting, partial-month floor preservation, and current-month/30-day source
windows.

## 7. Deployment and live verification

**NOT DEPLOYED. NOT LIVE VERIFIED.** This workspace has no Render credentials
or shell access:

- `RENDER_API_KEY` is not set here.
- `RENDER_SERVICE_ID` is not set here.
- `RENDER_EGRESS_ATTRIBUTION` is not set here.

Therefore the actual Render response shape, unit, timestamp density, monthly
lookback behavior, deployed commit, and live policy denials could not be
confirmed. No claim is made that the deployment is running this commit or that
the staged restrictions executed in production. The evidence above is offline
code, tests, and static review. Production confirmation requires the Render
deployment to pick up this commit and the owner to inspect the sanitized
`[RENDER_EGRESS]`, `OUTBOUND_POLICY_SNAPSHOT`, and `[BANDWIDTH_SOURCES]` lines;
those observations are not available in this workspace.

## 8. Remaining risks and limitations

1. Attribution is application-payload-level. Render's metric includes transport
   and traffic this instrumentation does not measure; the difference is not
   evidence of a specific source.
2. Telethon's progress callback is not a wire meter. Retransmissions and
   failed-attempt bytes may be undercounted; this is documented, not estimated.
3. HTTP response bodies are inbound and do not explain outbound usage. They are
   reported separately to avoid overstating egress.
4. Streamed request bodies without `Content-Length` are unmeasured, and
   server-side FastAPI responses are outside the instrumented clients.
5. A 30/31-day calendar month exceeds Render's 30-day lookback. `monthly_is_partial`
   and the durable floor prevent a partial reading from lowering a tier, but a
   fresh process with no prior floor cannot recover the missing first day(s).
6. Source part-vs-total reconciliation is reported, not assumed; unknown sources
   and missing hours remain visible.
7. The production flag and Render key configuration were not verifiable here.
   The implementation is disabled unless `RENDER_EGRESS_ATTRIBUTION` is exactly
   `1`, and media fails closed when the authoritative policy snapshot is
   unavailable.
8. The four full-suite failures are unrelated documentation inventory and
   time-dependent scheduler tests; they are not caused by this delivery.

## 9. Git delivery

The merge preserves the local attribution commit and the remote staged-policy
history. After this report is committed, delivery is verified with:

```
git rev-parse HEAD
git fetch origin main:refs/remotes/origin/main
git rev-parse origin/main
git merge-base --is-ancestor HEAD origin/main
git status --porcelain
```

Push is normal (no force, no rebase, no reset). If `origin/main` advanced again,
the new commits are inspected before integration; remote work is never
overwritten.
