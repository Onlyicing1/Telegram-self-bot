# Render Outbound Bandwidth — Forensic Investigation

**Date:** 2026-11-26

**Repository:** LifeOS / Telegram Self-Bot

**Branch investigated:** `freebuff/f5137e20b1c5761eea04f020`

**Commit investigated:** `31406f7912da14dcb23e5d099a587f16413be6eb`

**Scope:** read-only source investigation of the current repository, checked against the production observations supplied in the task. No production access, live Telegram/Supabase/Render calls, or web research were performed.

**Evidence labels used below**

- **VERIFIED FACT** — directly established from the current source, test, or supplied production observation.
- **INFERENCE** — a conclusion derived from verified facts, with the reasoning stated.
- **UNKNOWN / NOT MEASURABLE** — not established by the available code or telemetry.

---

## 1. Executive summary

**VERIFIED FACT — the reported outbound split is overwhelmingly NAT, not the HTTP server.**

The supplied Render data reports approximately `5,559.03 MB` (`5,559,025,983` bytes) of NAT traffic and approximately `9–10 MB` of HTTP traffic. Within the reported points, NAT is approximately **99.8%** of the source split. The HTTP server and dashboard therefore cannot be the primary explanation for a 5.56 GB month.

**VERIFIED FACT — the PostgREST rate is consistent with per-message database work, not a timer or retry loop.**

Every Telegram `NewMessage` event reaches `TaskEventDispatcher.handle_event`, which unconditionally executes `list_event_tasks` before it knows whether any event-triggered task exists (`backend/bot/handlers/task_events.py:56-97`; `backend/ai/task_event_dispatcher.py:151-163`; `backend/ai/database/task_repository.py:1280-1282`). An owner-authored message additionally executes `list_waiting_answer_occurrences` even when it is not a reply (`backend/ai/task_answers.py:98-118`; `backend/ai/database/task_repository.py:1419-1422`).

At `2,300–2,600` operations per five minutes, that is **7.7–8.7 requests/second**. If every operation were the per-message path, the message rate would be 7.7–8.7 messages/second when no message is owner-authored, or 3.8–4.3 messages/second if every message is owner-authored with the extra query. The scheduler, dashboard, heartbeat, and governor cache together contribute only tens of operations per five minutes, not thousands.

**VERIFIED FACT — there is also a conditional, config-gated per-message writer.**

When Anti-Edit / Anti-Delete is enabled, its `NewMessage` handler calls `record_message`, which forces a full archive read and then writes the archive back for every eligible **private human** message (`backend/bot/handlers/anti_edit_delete.py:512-530`; `backend/services/anti_edit_delete_service.py:925-971`, `997-1105`). In the dedicated-table mode this is roughly one read plus an upsert and a delete; in the legacy shard mode it can be one read plus a read/write pair for each of four shards. That is potentially **3–9 PostgREST requests per eligible private message**, far more than the task-event query. It is disabled by configuration by default, and its enabled state in production was not supplied. It is therefore a conditional alternative source, not an established cause. The supplied 7–9 KB body measurement argues against a large active archive: that path would send the archive row set on every eligible message, which would be much larger than 7–9 KB.

**VERIFIED FACT — the attribution counter is body-only, so a high operation count with almost no measured bytes is expected.**

`egress_attribution` measures HTTP request bodies using `Content-Length` (`backend/observability/egress_attribution.py:748-765`). A bodyless PostgREST `GET` is recorded as an exact **zero-byte** operation (`record_exact`, lines 234-238). Headers, TLS records, TCP/IP, MTProto framing, retransmissions, and keepalives are outside the counter by design (module doc, lines 1-35).

Consequently:

- `unmeasured_operations=0` means only that no instrumented request body had an unknown length. It is **not** evidence that all outbound bytes were measured.
- `7–9 KB` of measured body per five-minute window is consistent with a stream of bodyless `GET`s. It does **not** mean the requests only cost 7–9 KB on the wire.
- The missing bytes are mainly the HTTP request line and headers. The Supabase client is deliberately pinned to **HTTP/1.1** (`backend/db/transport.py:1-27`, `50-93`), so there is no HTTP/2 HPACK header compression across requests. Every request carries its own headers, including the deployment's Supabase API-key and bearer-token headers. Header size is deployment-specific, but it is not near zero.

**INFERENCE — per-message PostgREST header overhead is the leading candidate for the NAT total, but it is not proven as the sole cause.**

The arithmetic is compatible with the supplied numbers. If 2,300–2,600 requests per five minutes were sustained for a month, that would be roughly 20–22 million requests; the observed 5.56 GB month would correspond to an average of only about **250–280 bytes per request**. That is at the low end for authenticated HTTP/1.1, so the observed window rate was probably not sustained 24/7 for the entire month, or the headers are smaller than expected. Conversely, at 500 bytes of request overhead, the same rate sustained 24/7 would produce about 10–11 GB/month, roughly twice the observed total. The exact duty cycle and header size are **not measured**.

The important conclusion is not a precise percentage. It is that the request count is high enough for per-request header/transport overhead to be **GB-scale**, while the attribution counter can still report almost zero bytes. The prior assumption that thousands of PostgREST calls must be “only megabytes” is not supported once the count is expressed per five minutes: 2,400 per five minutes is about **691,000 per day**, not “thousands per day.”

**VERIFIED FACT — the observed `tier=90` above 4 GB is expected by the current policy code.**

`_policy_tier` evaluates only `50, 60, 70, 80, 90` and caps at 90 (`backend/services/outbound_bandwidth_service.py:465-470`). `4,000,000,000` bytes explicitly maps to tier 90 in `tests/test_outbound_bandwidth_policy.py:84-105`. The 4 GB “hard limit” checks in `_reserve_locked` and `_precheck_limits` compare against the **local reservation ledger**, not `effective_monthly_bytes` from Render (`outbound_bandwidth_service.py:778-790`, `852-856`). There is no `effective >= MONTHLY_TARGET_BYTES` refusal anywhere in the governor.

**VERDICT — the 100% hard limit is not a total-egress cap.**

- The **local reservation ledger** does have a 4 GB monthly refusal. That part is implemented and tested.
- The **effective Render monthly volume** does not have an independent 100% refusal. At 5.61 GB the code is designed to remain at tier 90; tier 90 denies all governed non-text uploads, but text up to 32 KiB remains exempt and, more importantly, traffic that never calls the governor is unaffected.
- Therefore the observed `tier=90` is **not evidence that the governor is malfunctioning**. It is evidence that the governor is a **90%-style policy plus a local-ledger cap**, not a universal 100% egress stop.
- The implementation report’s table row “`>= 100%` — hard monthly limit reservation refusal” (`IMPLEMENTATION_REPORT.md:121-137`) is **not implemented for effective Render bytes**. At the effective level, 100% and 90% are the same tier.

The exact split of the 5.56 GB between PostgREST headers, Telegram/MTProto, AI/provider requests, TTS/STT, Storage, and other NAT traffic remains **UNKNOWN / NOT MEASURABLE** with the current instrumentation.

---

## 2. Supabase / PostgREST request-volume investigation

### 2.1 The per-message path is unconditional

**VERIFIED FACT — every new message, in both directions, enters the task-event handler.**

`backend/bot/handlers/task_events.py:56-97` registers `events.NewMessage()` and documents “both directions; each path applies its own owner gate.” The handler:

1. Calls `private_message.enforce_incoming` first.
2. Extracts an event context.
3. Calls `TaskAnswerResolver.handle_event` if configured.
4. Calls `TaskEventDispatcher.handle_event` if configured.

The supervisor wires both the dispatcher and the resolver at startup (`backend/runtime/supervisor.py:557-570`). The dispatcher is therefore not a no-op in normal production.

**VERIFIED FACT — the dispatcher queries `ai_tasks` before checking whether any event task exists.**

`TaskEventDispatcher.handle_event` validates only `chat_id` and `message_id`, then executes:

```python
tasks = await self.repository.list_event_tasks(
    self.owner_id, MAX_EVENT_TASKS_PER_MESSAGE
)
```

at `backend/ai/task_event_dispatcher.py:151-163`. There is no cache, no “does the owner have event tasks?” flag, and no short-circuit for messages that cannot match an event schedule. Even when `tasks` is empty, the HTTP request has already happened.

The repository query is:

```python
self._client.table("ai_tasks") \
    .select("*") \
    .eq("owner_id", owner_id) \
    .eq("status", "active") \
    .eq("schedule_type", "event") \
    .order("id") \
    .limit(limit) \
    .execute()
```

at `backend/ai/database/task_repository.py:1280-1282`. The result is normally empty, but the request is still one PostgREST `GET`.

**VERIFIED FACT — the test suite treats this as a deliberate one-query-per-event design.**

`tests/test_external_call_efficiency.py:298-315` is titled “the per-event task query is bounded (documented, unchanged)” and asserts:

```python
assert repo.event_queries == 2, "one authoritative query per event, never more"
```

after two `handle_event` calls. The behavior is therefore intentional and documented, not an accidental loop. The missing consideration is the **wire cost of the request**, which the test does not measure.

### 2.2 Owner messages add a second query

**VERIFIED FACT — the answer resolver queries even when there is no reply to correlate.**

`TaskAnswerResolver.handle_event` checks `chat_id` and `sender_id == owner_id`, then immediately calls:

```python
parked = await self.repository.list_waiting_answer_occurrences(
    self.owner_id, 20
)
```

at `backend/ai/task_answers.py:98-118`. It does **not** check `reply_to_message_id` before the query. A `None` reply is rejected later, after the database round trip. The query is:

```python
self._client.table("ai_task_occurrences") \
    .select("*") \
    .eq("owner_id", owner_id) \
    .eq("status", WAITING_ANSWER_STATUS) \
    .order("updated_at") \
    .limit(limit) \
    .execute()
```

at `backend/ai/database/task_repository.py:1419-1422`.

Thus, for a normal incoming message from someone else: **one** PostgREST call. For an owner-authored message: **two** (unless the private-message gate rejects it first).

### 2.3 Quantification

**VERIFIED FACT — the supplied rate is 7.7–8.7 operations/second.**

| Observation | Arithmetic |
|---|---|
| 2,300 operations / 5 min | 460/min = 7.67/s |
| 2,600 operations / 5 min | 520/min = 8.67/s |
| Sustained per day | 662,400–748,800 requests |
| Sustained per 30 days | 19.9–22.5 million requests |

**INFERENCE — the task-event path is the only unconditional steady source large enough to explain the count.**

Let `M` be messages per five minutes and `O` the fraction that are owner-authored (each adding the waiting-answer query). Then:

```text
Q = M × (1 + O)
```

For `Q = 2,300–2,600` (assuming the observed count is task-event traffic; Anti-Edit can add more):

| Owner-authored share `O` | Implied messages / 5 min | Implied messages / s |
|---:|---:|---:|
| 0% | 2,300–2,600 | 7.7–8.7 |
| 25% | 1,840–2,080 | 6.1–6.9 |
| 50% | 1,533–1,733 | 5.1–5.8 |
| 100% | 1,150–1,300 | 3.8–4.3 |

This is a derived range, not a measured message rate. The production message rate and owner share were not supplied.

**VERIFIED FACT — the periodic background baseline is far too small.**

| Source | Queries per five minutes | Evidence |
|---|---:|---|
| Task scheduler idle | ~15 | 3 queries per wake at a 60 s ceiling: `list_due_retry_occurrences`, `list_due_tasks`, `next_run_hint` (`backend/ai/task_scheduler.py:21-23`, `443-447`, `452-475`; repository queries at `task_repository.py:1258`, `1263`, `1411`) |
| Dashboard polling (one open tab) | 40 | 4 queries per 30 s: `api.saves` performs 2 (`backend/db/client.py:454-472`), `api.bio` 1, `api.logs` 1 (`src/App.tsx:62-85`) |
| Governor policy refresh | ~2 (one `panel_settings` read + one Render API request per 5 min cache) | `backend/services/outbound_bandwidth_service.py:428-436`, `550-616` |
| Heartbeat/attribution | ~1 | `backend/runtime/heartbeat.py:181-203` |
| AI trigger config | at most 1 per 30 s on an AI-triggered message | `backend/bot/handlers/ai_unified.py:61-62`, `263-268` |

Even adding all of these, the non-message baseline is on the order of **50–60 operations per five minutes**, roughly **2–3%** of the observed window. It cannot produce 2,300–2,600 by itself.

### 2.4 Conditional per-message writer: Anti-Edit / Anti-Delete

**VERIFIED FACT — the Anti-Edit / Anti-Delete handler can add multiple PostgREST calls per eligible private message.**

The handler is registered on `events.NewMessage()` (`backend/bot/handlers/anti_edit_delete.py:512-530`). It awaits `service.ensure_loaded_async()`, returns immediately if the feature is disabled, and otherwise resolves the peer identity and calls `service.record_message`. `record_message` then:

1. calls `load_rows_async(force=True)`, which reads the archive from the dedicated `aed_archive_entries` table or the legacy `bot_settings` shards (`backend/services/anti_edit_delete_service.py:925-956`);
2. builds the row and calls `_save_rows(rows)` (`:1050-1105`);
3. in dedicated mode, `_save_aed_entries_sync` performs an `upsert` of the complete bounded row set and a delete of rows no longer present (`backend/db/client.py:3221-3248`);
4. in legacy mode, `_aed_save_shards_sync` performs a `select` and an `update`/`insert` for each of four shards (`backend/db/client.py:2957-2980`).

That is approximately **3 requests** per eligible private message in dedicated mode (one load, one upsert, one delete), or **9 requests** in legacy mode (one load plus four select/write pairs). The archive is bounded, but the write payload is the whole bounded row set, not one row.

**UNKNOWN — whether the feature was enabled and populated in production.**

No configuration value was supplied. The 7–9 KB measured request-body rate is evidence against a large active archive, because an active archive would write the whole row set on every eligible message. A disabled feature, an empty archive, or a small archive could remain compatible with 7–9 KB. In the small-archive case, its 3–9 requests per eligible private message could dominate the observed count while still producing little body traffic. This path must be ruled in or out from configuration/feature state before attributing the entire PostgREST count to task events.

### 2.5 Retries, concurrency, and duplication

**VERIFIED FACT — the per-message read path has no request retry.**

`TaskRepository._run` dispatches one synchronous call through `run_sync_db` and returns or raises (`backend/ai/database/task_repository.py:1179-1188`; `backend/db/client.py:223-243`). There is no loop around `list_event_tasks` or `list_waiting_answer_occurrences`. The governor’s `_MAX_CONFIRM_ATTEMPTS = 3` applies only to a state write/confirm, not to these reads (`outbound_bandwidth_service.py:137`, `769-840`).

**INFERENCE — the count is not amplified by retries in normal operation.**

A transient Supabase failure would produce one failed request and a fallback, not an automatic retry storm. A reconnect replay of Telegram updates is a possible duplicate source, but no deduplication or replay measurement exists in this path; its size is **UNKNOWN**.

**VERIFIED FACT — each event is handled independently; there is no request coalescing.**

Two messages arriving close together produce two `list_event_tasks` calls. There is no per-chat debounce, no batch endpoint, and no shared future. The bounded DB executor controls concurrency but does not deduplicate work (`backend/db/client.py:223-243`).

### 2.6 Correction: `/api/db/stats` is not a PostgREST N+1

**VERIFIED FACT — the previously suspected `/api/db/stats` N+1 does not issue one PostgREST request per session.**

`backend/observability/db_stats.py:32-43` does loop over up to 10,000 sessions and calls `mgr.message.count(session_id)` once per session. However, `RepositoryManager` constructs:

```python
self._session = InMemorySessionRepository()
self._message = InMemoryMessageRepository()
```

unconditionally, even when Supabase is available (`backend/ai/database/manager.py:64-68`). Only the task repository, memory repository, usage repository, and provider-stats repository are Supabase-backed. The session/message loop is therefore an **in-memory loop**, not a PostgREST fan-out.

The endpoint still makes a small number of Supabase calls for memory/provider counts, but it is not a credible source of 2,300–2,600 PostgREST operations. This correction matters because the endpoint is unauthenticated and easy to misidentify as the cause.

### 2.7 Request count versus measured bytes

**VERIFIED FACT — measured body bytes are tiny compared with the Render total.**

The supplied attribution window reports `7–9 KB` of `exact_payload_bytes` per five minutes. If that body rate were sustained for a month, it would be only:

```text
7–9 KB / 5 min × 288 windows/day × 30 days ≈ 60–78 MB/month
```

That is roughly **1.1–1.4%** of 5.56 GB even under an unrealistic sustained assumption. Body bytes alone cannot explain the overage.

**VERIFIED FACT — the body-only counter cannot see the bytes that a `GET` actually sends.**

A PostgREST `GET` has no request body. `_body_size` returns `0` when neither `Content-Length` nor `Transfer-Encoding` is present (`egress_attribution.py:748-765`), and `record_exact` records that zero plus one operation (`egress_attribution.py:234-238`). The request line and every header are excluded. The module doc says so explicitly:

> “an `httpx` event hook reading the request's `Content-Length` … excludes TCP/IP, TLS and HTTP headers.”

The same doc says the sum is “not expected to equal” Render (`egress_attribution.py:1-35`).

**INFERENCE — a 2,400-per-five-minute stream of bodyless `GET`s can be GB-scale while measuring 7–9 KB of body.**

The request overhead per call is the only necessary explanation. At the observed rate, the monthly total corresponds to roughly **250–280 bytes per request** if the rate were sustained 24/7. If the real header block is 500 bytes, the same rate would produce about **10–11 GB/month**, so the actual rate must have been lower on average or limited to active hours. The exact per-request size is **UNKNOWN / NOT MEASURABLE** from the current counter.

The HTTP/1.1 pin makes this more consequential, not less: unlike HTTP/2, HTTP/1.1 has no HPACK compression of repeated headers. The Supabase client passes the deployment key into the client (`backend/db/transport.py:108-124`), so each request carries its own authentication headers. Header length is deployment-specific and was not inspected.

---

## 3. Telegram / Telethon traffic investigation

### 3.1 The per-message handler does not make a Telegram RPC

**VERIFIED FACT — the task-event handler itself does not make a Telegram RPC.**

`task_events.py:56-97` receives the update from Telethon and performs database work. It does not call `send_message`, `get_messages`, `get_entity`, or `get_me`. The private-message gate it calls first may resolve an entity for a private chat (`backend/bot/handlers/private_message.py:431-475`), but the task-event path itself is database work. The observed 2,300–2,600 PostgREST operations are therefore **not** Telegram RPC operations; they are database requests caused by Telegram events.

This distinction is central: the message rate can drive PostgREST egress without a corresponding per-message Telegram upload.

### 3.2 Telegram RPC categories and repeated patterns

**VERIFIED FACT — Telegram calls are scattered across handlers and services, with a central retry helper.**

Representative categories found:

| Category | Call sites / behavior |
|---|---|
| Send text/panels | `client.send_message` / edit paths in handlers; text is not governed by the bandwidth reservation ledger |
| Upload media | `client.send_file` / `upload_file` in Deep Save, album members, rename re-upload, voice notes, AI attachments |
| Download media | `client.download_media` in save/retrieve/appearance/anti-edit paths; recorded as inbound payload, not egress |
| Entity resolution | `get_entity` / `get_input_entity` for peers and private-message senders |
| Message reads | `get_messages` for replies, history, and verification |
| Keepalive | `client.get_me()` every 60 s (`backend/runtime/keepalive.py:23-24`, `83`) |
| Recovery | `get_me()` verification during supervisor recovery (`backend/runtime/supervisor.py:1012-1023`) |
| Retry | `tg_rpc` supports up to 4 total attempts (3 retries) for factories on `FloodWait`/transient errors (`backend/runtime/tg_retry.py:59-120`) |

**VERIFIED FACT — no periodic Telegram media transfer or history poll was found.**

The only fixed Telegram cadence found is the 60 s `get_me` keepalive. The task scheduler is a local wake loop with database queries, not a Telegram poll. Telethon itself receives updates over the existing connection; no application-level per-message `getUpdates` loop exists.

**INFERENCE — `tg_rpc` can amplify traffic during instability, but not in the normal path.**

A FloodWait or transport failure can cause up to four attempts of the same RPC. That is bounded and only occurs on failure. It cannot explain a steady 2,300–2,600 PostgREST reads, which do not go through `tg_rpc` at all.

### 3.3 Governed upload seams

**VERIFIED FACT — the main new-payload Telegram uploads reserve before transmitting.**

| Path | Reservation / evidence |
|---|---|
| Deep Save single | `backend/services/save_service.py:1678-1692` (`_reserve_upload_bytes("telegram_media", actual_size)` immediately before `send_file`) |
| Deep Save album member | `backend/services/save_service.py:1969-1980` (per-member reservation before `upload_file`; grouped send attaches existing handles at `2018-2022`) |
| Rename re-upload | `backend/services/retrieve_service.py:1374-1411` (policy then download then reservation/upload) |
| Voice note | `backend/telegram_api/media.py:117-166` (policy then reservation then `send_file`) |
| AI attachment | `backend/ai/tools/delivery.py:725-747` (reservation then policy then `send_file`) |

The album path is especially large by configuration: `max_deep_save_mb` defaults to 50 MB and is validated up to 500 MB (`backend/services/settings_service.py:39`, `79`, `176`). A multi-file album can therefore represent multiple GB in one operation, although the per-member reservations and the bulk policy gate are applied before each transfer.

**VERIFIED FACT — these reservations are payload-level, not wire-level.**

`egress_attribution` counts Telethon's cumulative `progress_callback(current, total)` deltas for one file and documents that RPC frames, TL/encryption overhead, keepalives, panel edits, and connection setup stay unmeasured (`backend/observability/egress_attribution.py:8-20`). A reserved byte count is the file size, not the MTProto wire size. TLS/TCP retransmissions are not represented.

**INFERENCE — a small number of large media operations could dominate a 5.56 GB month, but the supplied evidence does not show that they occurred.**

The governor snapshot’s `governor_monthly_bytes` was not supplied. The effective value alone (`5.61 B`) cannot tell whether the local ledger contained a large media total or only a small one. The exact media contribution is **UNKNOWN**. At the current tier 90, the governed media seams would deny new non-text transfers, so historical media is the only relevant question; it cannot be answered from the snapshot fields provided.

### 3.4 What Telegram traffic is measurable

**VERIFIED FACT — the current instrumentation can measure only completed payload boundaries.**

- Upload: completed file bytes via progress deltas, one logical transfer per file.
- Download: payload path/bytes returned by the supported download boundary.
- Unmeasured: MTProto frames, TL serialization, encryption, keepalives, ACKs, retransmissions, failed transfers without a completed payload, entity/RPC calls, and connection setup.

**UNKNOWN / NOT MEASURABLE — raw MTProto wire bytes.**

The repository does not instrument Telethon's transport or the MTProto connection. No statement in this report claims to measure raw Telegram wire bytes. Any estimate of Telegram's share of the 5.56 GB would be an estimate, not a measurement.

---

## 4. Outbound governor and 100% hard-limit audit

### 4.1 Enforcement flow

**VERIFIED FACT — the policy snapshot is a max of Render, the local ledger, and the persisted floor.**

`refresh_policy_snapshot`:

1. Reads Render's monthly metric (`render_bandwidth_service.get_render_bandwidth_usage`).
2. Reads the local governor state (`_current_governor_monthly`).
3. Reads the persisted monthly `policy_floor`.
4. Computes `effective = max(render_bytes or 0, governor_bytes or 0, floor)`.
5. Sets `trusted = render_trusted and governor_available`, with fail-closed reasons for untrusted Render or governor.
6. Computes `tier = _policy_tier(effective)`.
7. Persists a new floor when `effective > floor`.

Evidence: `backend/services/outbound_bandwidth_service.py:550-616`. The Render metric is trusted only when it is available, belongs to the current UTC month, has a trustworthy `latest_data_at` no more than 3 hours old, and is not in the future (`:452-462`). A partial Render month may still be trusted; the floor prevents a lower partial reading from lowering the tier (`tests/test_outbound_bandwidth_policy.py:413-444`).

**VERIFIED FACT — reservations fail closed, except for the explicit text exemption.**

`_read_row_state` raises `OutboundBandwidthUnavailableError` when durable accounting is unconfigured, unreadable, missing, or malformed (`outbound_bandwidth_service.py:340-362`). `_persist_state` raises when the write fails or is refused (`:364-385`), and `_reserve_locked` retries the read/check/write/confirm sequence at most `_MAX_CONFIRM_ATTEMPTS = 3` times before denying (`:137`, `769-840`). `_assert_policy_allows` denies every non-text action when the snapshot is untrusted (`:690-715`). The deliberate exception is text up to 32 KiB, which remains available even when the policy is untrusted. That is a fail-open by design for availability, not an accidental bypass.

### 4.2 Exact tier behavior

**VERIFIED FACT — the tiers are cumulative and stop at 90.**

`_policy_tier` iterates `(50, 60, 70, 80, 90)` and returns the highest reached (`outbound_bandwidth_service.py:465-470`). There is no 100 entry. `POLICY_THRESHOLD_BYTES` is:

```text
50% = 2,000,000,000
60% = 2,400,000,000
70% = 2,800,000,000
80% = 3,200,000,000
90% = 3,600,000,000
```

(`outbound_bandwidth_service.py:63-69`). `4,000,000,000` is explicitly tested to return 90 (`tests/test_outbound_bandwidth_policy.py:84-105`).

The action minimums (`_ACTION_MIN_TIER`, `outbound_bandwidth_service.py:100-125`) produce:

| Tier | Denied governed classes | Still allowed governed class |
|---:|---|---|
| 0–49% | none | all, including unknown at 0–49% |
| 50% | automatic/scheduled media, unknown | single media, bulk, provider, file, reupload, storage, forward, text |
| 60% | + bulk/album media | single media, provider, file, reupload, storage, forward, text |
| 70% | + provider media (OCR/STT/AI media) | single media, file, reupload, storage, forward, text |
| 80% | + file upload, reupload, storage upload, media forward | single media, text |
| 90% | + single/interactive media; every governed non-text class | text ≤ 32 KiB only |
| ≥100% | no distinct tier; same as 90 for governed classes | text ≤ 32 KiB; all ungoverned traffic unaffected |

The 90% strict-text behavior is directly tested in `tests/test_outbound_bandwidth_policy.py:151-171`: every listed non-text action raises `OutboundBandwidthPolicyError`, and `ACTION_TEXT` is allowed.

### 4.3 Is the 100% hard limit independent of the tier?

**VERIFIED FACT — no.**

`_assert_policy_allows` uses the snapshot’s `tier` and `trusted` fields; it never compares `effective_monthly_bytes` to `MONTHLY_TARGET_BYTES` (`outbound_bandwidth_service.py:690-715`). The only 4 GB monthly checks are in `_reserve_locked` and `_precheck_limits`:

```python
if monthly_used + byte_count > MONTHLY_LIMIT_BYTES:
    raise OutboundBandwidthExceededError(...)
```

(`outbound_bandwidth_service.py:778-790`, `852-856`). Here `monthly_used` comes from `_normalize_state(_read_row_state(), ...)`, i.e. the **local reservation ledger**. It is not `effective_monthly_bytes`, not Render's reading, and not the policy floor.

**VERIFIED FACT — `MONTHLY_TARGET_BYTES` is used only as a reported limit/error field, not as an effective-volume gate.**

A repository-wide search finds `MONTHLY_TARGET_BYTES` at the constant, the `OutboundBandwidthPolicyError.limit_bytes` field, the snapshot’s `monthly_limit_bytes`, and the error text. It is never used in a comparison against `effective`. The same is true of `MONTHLY_LIMIT_BYTES` outside the local-ledger checks and the local `usage_state`.

### 4.4 The observed `tier=90` at 5.61 GB

**VERIFIED FACT — it is expected, not a malfunction.**

At `effective=5.61 B`, the loop has passed `3.6 B` (90%) and stops at 90. Nothing in the code maps 100% to a higher tier. The snapshot’s `trusted=true` and `partial=false` are consistent with a fresh Render reading whose 30-day query covers the month; `partial=false` does not prove that every hourly point is present, and `trusted` does not change the tier calculation.

**INFERENCE — the reported “100% hard limit” expectation is a specification/documentation mismatch, not an observed code regression.**

`IMPLEMENTATION_REPORT.md:121-137` states:

> “Thresholds remain exactly 50/60/70/80/90% … with the hard monthly limit at 4,000,000,000 bytes …”
>
> “| ≥ 100% | hard monthly limit reservation refusal |”

The code does have a 4 GB **local-ledger** refusal, and it is tested (`tests/test_outbound_bandwidth_governor.py:155-173`). But the table’s “≥100%” row reads as if it applies to the effective/Render percentage. It does not. At 5.61 B effective with a low local ledger, no `OutboundBandwidthExceededError` is raised by the monthly check; the policy simply remains at 90.

### 4.5 Possible bypasses and gaps

**VERIFIED FACT — all governed non-text paths are blocked at tier 90; the bypass is traffic that never calls the governor.**

- **Unclassified actions:** `classify_outbound_action` maps unknown actions to `ACTION_UNKNOWN`, whose minimum tier is 50, so they are denied at 90 (`outbound_bandwidth_service.py:100-125`). This is conservative and is not a 90 bypass. At tier 0–49, unknown is allowed; that is by design.
- **Scheduled operations:** `automatic_media_scope()` maps scheduled media to `ACTION_AUTOMATIC_MEDIA`, denied from 50 (`outbound_bandwidth_service.py:631-658`; test at `tests/test_outbound_bandwidth_policy.py:195-205`). Not a 90 bypass.
- **Async/sync paths:** both `reserve_outbound_bytes` and `reserve_outbound_bytes_sync` call the same `_assert_policy_allows` and the same local lock (`:858-900`; parity test at `tests/test_outbound_bandwidth_policy.py:397-410`). Not a bypass.
- **Stale snapshots:** the async path refreshes at most every 5 minutes (`RENDER_CACHE_TTL_SECONDS = 300`, `:74`, `428-436`); the sync path deliberately uses the cached snapshot without refreshing (`:733-745`). A threshold crossing can therefore take up to 5 minutes to affect a later upload. That is a bounded lag, not an indefinite bypass.
- **Untrusted/partial metrics:** untrusted media is denied (`_assert_policy_allows` checks `trusted`); text within 32 KiB is still allowed. A fresh but partial Render month can be trusted; the floor prevents downward movement, but if the process has no floor yet and the partial reading omits traffic, the effective value can be understated. The supplied observation says `partial=false`, so this particular snapshot is not in that state.
- **Text:** `ACTION_TEXT` with `0 < byte_count <= 32 KiB` is allowed at every tier, even when untrusted (`outbound_bandwidth_service.py:690-700`). This is an intentional availability exception. It cannot explain GB-scale egress by itself, but it is a real hole in any “stop all egress” interpretation.
- **Direct service calls:** any code path that calls `client.send_file`/`upload_file` or an HTTP client without `reserve_outbound_bytes`/`ensure_action_allowed` bypasses the governor. The main upload seams are governed, but there is no global interception at the Telethon or httpx layer.
- **Media processing / STT:** Groq and Speechmatics reserve once with `len(audio)` before transcription (`backend/services/groq_stt_engine.py:558-562`; `speechmatics_stt_engine.py:550-554`). Groq's `_run_passes` then makes up to `self._passes` sequential uploads of the same audio (`groq_stt_engine.py:620-654`) without a new reservation per pass. At tier 90, provider media is denied before any pass, so this is not a 90 bypass; at lower tiers it is a ledger undercount.
- **Appearance Storage preview:** `backend/services/appearance_service.py:1351` calls `ensure_action_allowed(ACTION_STORAGE_UPLOAD, len(data))` but does not call `reserve_outbound_bytes` for that upload. At tier 80+ the policy denies it; below 80 the bytes are not added to the local ledger. This is a policy-gated but ledger-unaccounted path.
- **PostgREST reads and Telethon RPCs:** these never call the governor at all. The per-message `ai_tasks` GET, `get_me` keepalive, entity resolution, and other reads continue at tier 90. This is the largest conceptual bypass: the governor is an **upload-payload governor**, not a network-egress governor.

### 4.6 Test coverage for the hard-limit behavior

**VERIFIED FACT — the local-ledger hard limit is tested; the effective 100% cap is not.**

- `tests/test_outbound_bandwidth_governor.py:155-173` sets the local monthly state to `MONTHLY_LIMIT_BYTES - 9` and asserts a 10-byte reservation raises `OutboundBandwidthExceededError("monthly")` with no write.
- `tests/test_outbound_bandwidth_policy.py:84-105` asserts `4,000,000,000` maps to tier 90, not 100.
- `tests/test_outbound_bandwidth_policy.py:151-171` asserts tier 90 denies every governed non-text class and allows text.
- `tests/test_outbound_bandwidth_policy.py:397-410` asserts async/sync parity.
- No test asserts that a reservation is refused when `effective_monthly_bytes >= MONTHLY_TARGET_BYTES` while the local ledger is below the limit.

---

## 5. NAT / HTTP / application attribution reconciliation

### 5.1 What the supplied numbers support

**VERIFIED FACT — the reported split is dominated by NAT.**

| Reported source | Bytes | Share of reported split |
|---|---:|---:|
| NAT | 5,559,025,983 | ≈99.8% |
| HTTP | ≈9–10 MB | ≈0.2% |
| Total | ≈5.56 GB | 100% of the reported points |

The HTTP server, dashboard responses, and static assets cannot account for the 5.56 GB. They are bounded by the HTTP series, which is three orders of magnitude smaller. Any explanation that places the dashboard at the center is inconsistent with this split.

**VERIFIED FACT — estimation is not attribution.**

The source diagnostic reports `225` points and `368` missing hours for the current-month window. `_window_for` bounds the window to Render's 30-day lookback (`backend/services/render_bandwidth_sources_diagnostic.py:454-463`), and `missing_hours` is computed as `expected_hours - hours_with_any_data` without filling zeros (`:324-341`). The diagnostic therefore cannot distinguish:

- an hour with genuinely zero traffic, and
- an hour for which Render published no point.

It also reports `parts_match_total` only when total and parts are both present and units agree (`:315-322`). That field was not supplied. The exact source split is therefore **not fully reconciled**.

**VERIFIED FACT — the diagnostic's own HTTP request is outside attribution.**

`_fetch` constructs `httpx.AsyncClient(timeout=_TIMEOUT)` without `egress.async_client_kwargs` (`render_bandwidth_sources_diagnostic.py:354`). The diagnostic's request therefore does not appear in the `[RENDER_EGRESS]` counters. This is a small 6-hourly request, not a GB source, but it proves that “all HTTP clients are instrumented” is false.

### 5.2 What cannot be reconciled

**UNKNOWN / NOT MEASURABLE — the byte split inside NAT.**

NAT is measured at the network boundary, so it includes every outbound request from the service and may include transport-level bytes that the application counter does not. The known client categories are:

- PostgREST HTTP/1.1 requests and their headers;
- Telethon/MTProto RPCs, framing, encryption, and keepalives;
- AI provider chat/completion requests;
- TTS/STT provider uploads;
- Supabase Storage uploads;
- Render API requests;
- any other outbound client traffic.

The current application counters measure request bodies and selected Telegram payloads only. They cannot divide NAT into those categories, and they do not measure TCP/IP or TLS overhead.

**VERIFIED FACT — the `unattributed_difference_bytes` log field is not a true residual.**

`_emit_flush_locked` computes `difference = render_monthly_bytes - measured_month`, where `measured_month` is the sum of `exact_payload_bytes` (`egress_attribution.py:358-364`). That subtraction compares a service-level NAT total with a body-only, process-local application counter. It is useful as a warning that a large gap exists; it is not an attribution of the gap to a protocol.

**VERIFIED FACT — the attribution counters are process-local and restart-scoped.**

The counters are module globals with `scope=process` in their logs (`egress_attribution.py:136-143`, `364-395`). They do not persist to a database, do not aggregate across processes, and start when the process starts. The supplied logs were captured after attribution was enabled, not from the beginning of the month. They cannot be used as a November baseline.

**VERIFIED FACT — missing hours are not zero-filled by either Render reader.**

`render_bandwidth_service._parse_payload` counts distinct hours and reports `missing_hours`, but it sums only the points that exist (`render_bandwidth_service.py:100-148`). It marks a month partial when the 30-day query starts after the month start (`:166-213`). A fresh but partial reading can be trusted for policy, with the floor protecting against a lower value; that does not make the source split complete.

### 5.3 The defensible quantitative statement

The evidence supports only these quantitative statements:

1. NAT is approximately **99.8%** of the reported source split; HTTP is approximately **0.2%**.
2. The PostgREST operation rate is **7.7–8.7 requests/second**, or **2,300–2,600 per five minutes**.
3. The measured request-body rate is **7–9 KB per five minutes**, which is too small to explain the total by itself.
4. If the observed request rate were sustained for a month, the monthly total would imply roughly **250–280 bytes per request**; at 500 bytes of request overhead, the same rate would produce about **10–11 GB/month**, above 5.56 GB, so the actual duty cycle must be lower or bursty. This is an order-of-magnitude consistency check, not a measurement.
5. The exact split of the 5.56 GB among PostgREST headers, Telegram/MTProto, and other NAT clients is **not measurable** with the current code.

No precise “PostgREST caused X%” or “Telegram caused Y%” figure is justified.

---

## 6. Findings table

| Finding | Evidence | Severity | Confidence | Code location |
|---|---|---|---|---|
| Every Telegram `NewMessage` causes one PostgREST `ai_tasks` read, even with zero event tasks | Unconditional `list_event_tasks` before any task check; 2,300–2,600 ops/5 min is 7.7–8.7/s | **HIGH** | **HIGH** | `backend/bot/handlers/task_events.py:56-97`; `backend/ai/task_event_dispatcher.py:151-163`; `backend/ai/database/task_repository.py:1280-1282` |
| Every owner-authored message adds a waiting-answer read, even without a reply | `sender_id == owner` check followed immediately by the query; `reply_to` is not checked first | **MEDIUM** | **HIGH** | `backend/ai/task_answers.py:98-118`; `backend/ai/database/task_repository.py:1419-1422` |
| The per-message query is intentional and documented, not a runaway loop | Test asserts one query per event; comment says “documented, unchanged” | **INFORMATIONAL** | **HIGH** | `tests/test_external_call_efficiency.py:298-315` |
| Anti-Edit can add 3–9 PostgREST calls per eligible private message when enabled | Full archive read plus whole-set upsert/delete in dedicated mode; four select/write pairs in legacy mode | **HIGH** (if enabled) | **HIGH** for code; **UNKNOWN** for production state | `backend/bot/handlers/anti_edit_delete.py:512-530`; `backend/services/anti_edit_delete_service.py:925-971`, `997-1105`; `backend/db/client.py:2957-2980`, `3221-3248` |
| The periodic scheduler/dashboard/heartbeat baseline is far too small to explain the rate | ~15 scheduler + ~40 dashboard + ~1 heartbeat + ~2 governor per 5 min | **INFORMATIONAL** | **HIGH** | `backend/ai/task_scheduler.py:21-23`, `443-475`; `src/App.tsx:62-85`; `backend/db/client.py:454-472` |
| The per-message read path has no retry amplification | `_run` dispatches one `run_sync_db` call; no loop around the read | **INFORMATIONAL** | **HIGH** | `backend/ai/database/task_repository.py:1179-1188`; `backend/db/client.py:223-243` |
| `/api/db/stats` is not a PostgREST N+1 | Session/message repositories are in-memory even with Supabase; only memory/provider counts are remote | **INFORMATIONAL** | **HIGH** | `backend/ai/database/manager.py:64-68`; `backend/observability/db_stats.py:32-43` |
| Attribution measures request bodies only; headers, TLS, TCP, and MTProto are excluded | `_body_size` reads `Content-Length`; module doc states exclusions | **HIGH** | **HIGH** | `backend/observability/egress_attribution.py:1-35`, `748-765` |
| `unmeasured_operations=0` does not mean all bytes were measured | Bodyless `GET` is `record_exact(0)` plus one operation; no header field exists | **MEDIUM** | **HIGH** | `backend/observability/egress_attribution.py:234-238`, `748-765` |
| PostgREST is pinned to HTTP/1.1, so repeated auth headers are not HPACK-compressed | `http2=False`; transport doc says “same headers” and HTTP/1.1 | **MEDIUM** | **HIGH** (transport fact); **INFERENCE** for volume | `backend/db/transport.py:1-27`, `50-93`, `108-124` |
| The observed `tier=90` at 5.61 GB is expected by design | `_policy_tier` caps at 90; 4,000,000,000 explicitly maps to 90 | **INFORMATIONAL** | **HIGH** | `backend/services/outbound_bandwidth_service.py:465-470`; `tests/test_outbound_bandwidth_policy.py:84-105` |
| There is no independent 100%-of-effective hard cap | The only 4 GB checks use the local ledger; no `effective >= MONTHLY_TARGET` refusal exists | **HIGH** | **HIGH** | `backend/services/outbound_bandwidth_service.py:778-790`, `852-856` |
| The implementation report claims a ≥100% reservation refusal that the effective path does not implement | Table row “≥ 100% — hard monthly limit reservation refusal” | **HIGH** | **HIGH** | `IMPLEMENTATION_REPORT.md:121-137`; contrast `outbound_bandwidth_service.py:465-470` |
| The governor is an upload-payload governor, not a total-egress governor | Module doc: accounts only explicit payload paths; Render used for policy | **HIGH** | **HIGH** | `backend/services/outbound_bandwidth_service.py:1-30` |
| NAT is ~99.8% of the reported split; HTTP is ~0.2% | Supplied Render source diagnostic: 5,559.03 MB NAT vs ~9–10 MB HTTP | **INFORMATIONAL** | **HIGH** (within reported points) | Production observation supplied in task |
| The source split has 225 points and 368 missing hours and does not zero-fill | Diagnostic computes missing hours from union of buckets; no synthetic zeros | **MEDIUM** | **HIGH** | `backend/services/render_bandwidth_sources_diagnostic.py:324-341`, `454-463` |
| The diagnostic’s own HTTP request is outside attribution | `httpx.AsyncClient(timeout=_TIMEOUT)` without egress hooks | **LOW** | **HIGH** | `backend/services/render_bandwidth_sources_diagnostic.py:354` |
| The 5-minute policy cache can delay a tier crossing | `RENDER_CACHE_TTL_SECONDS = 300`; sync path uses cached snapshot | **LOW** | **HIGH** | `backend/services/outbound_bandwidth_service.py:74`, `428-436`, `733-745` |
| Telegram upload attribution is payload-level only | Progress callback counts file bytes; doc excludes RPC/TL/encryption/keepalives | **MEDIUM** | **HIGH** | `backend/observability/egress_attribution.py:8-20` |
| STT reserves once but can upload the same audio multiple times | Reservation before `_run_passes`; pass loop performs sequential requests | **MEDIUM** | **HIGH** | `backend/services/groq_stt_engine.py:558-562`, `620-654`; `speechmatics_stt_engine.py:550-554` |
| Appearance preview Storage upload is policy-gated but not ledger-reserved | `ensure_action_allowed` only; no `reserve_outbound_bytes` at that seam | **LOW** | **HIGH** | `backend/services/appearance_service.py:1351` |
| Telegram keepalive sends `get_me` every 60 s | `_INTERVAL = 60.0`; `client.get_me()` in `_probe` | **INFORMATIONAL** | **HIGH** | `backend/runtime/keepalive.py:23-24`, `83` |
| A meaningful Telegram media operation can be large but is currently blocked at tier 90 | `max_deep_save_mb` default 50, max 500; governed seams reserve per member | **INFORMATIONAL** | **HIGH** (limits); **UNKNOWN** whether used | `backend/services/settings_service.py:39`, `79`, `176`; `backend/services/save_service.py:1969-1980` |

---

## 7. Prioritized next investigations or fixes

These are recommendations only. None was implemented in this read-only investigation.

1. **Measure on-wire request bytes for one PostgREST read, then for the per-message path.** The decisive experiment is to count the full serialized request (request line + headers + body + TLS record overhead) at the `httpx` transport, not just `Content-Length`, for a short controlled window. Compare that count with Render NAT for the same window. This directly tests the leading inference and does not require guessing header sizes. The current counter cannot answer it.

2. **Correlate hourly Render NAT deltas with hourly Telegram `NewMessage` counts and PostgREST operation counts.** The existing logs already carry operation counts and message traffic. If NAT rises with message/query count and not with media events, the per-message header path is confirmed. If NAT spikes align with Deep Save/album operations, the media path is confirmed instead. This is the cheapest way to split the two hypotheses without new production instrumentation.

3. **Add a call-site label to the PostgREST attribution category.** Today all PostgREST requests are one `supabase_rest/postgrest` bucket. A bounded label for `task_event`, `task_answer`, `dashboard`, `governor`, and similar call sites would make the 2,300–2,600 count attributable without logging URLs or payloads. This is an observability change, not a behavior change.

4. **Correct the governor’s public semantics and documentation.** Either:
   - document that the 4 GB hard limit is a local reservation-ledger cap and that the effective Render policy caps at 90%, or
   - add an explicit `effective >= MONTHLY_TARGET_BYTES` refusal if a true 100% policy is required.

   An explicit 100% policy still would not stop ungoverned PostgREST/Telegram traffic; it would only stop paths that call the governor. The documentation must say that.

5. **Evaluate caching or precomputing the event-task lookup.** `list_event_tasks` is one `GET` per message even when the owner has no event tasks. A bounded cache with correct invalidation on task create/update/delete, or a cheap “has any event task” flag, would remove most of the request volume. This is a fix candidate, but it should be measured first because freshness semantics are deliberate.

6. **Resolve the source diagnostic’s coverage gap.** Determine whether the 368 missing hours are zero-traffic hours or unpublished points, and whether `parts_match_total` is true. Do not treat the source split as a complete month until that is known. This affects any future claim about NAT/HTTP percentages.

7. **Determine the Anti-Edit enabled/populated state and its per-message request count.** The feature can add 3–9 PostgREST calls per eligible private message, but its configuration was not supplied. A single bounded log of `enabled`, archive row count, and requests per captured message would rule this source in or out before optimizing the task-event query.

8. **If PostgREST does not reconcile, instrument Telegram at the connection boundary.** A transport-level byte count (without logging content) is the only way to separate MTProto wire bytes from application payload. Do not infer raw MTProto bytes from `progress_callback` values.

9. **Fix the STT reservation multiplier and the appearance preview reservation gap.** At lower tiers, Groq/Speechmatics can upload the same audio multiple times against one reservation, and the appearance preview upload is not in the ledger. These are accounting gaps, not the observed 90% behavior, but they weaken the ledger’s meaning.

---

## 8. Files inspected

**Documentation and repository state**

- `AGENTS.md`
- `IMPLEMENTATION_REPORT.md`
- `INVESTIGATION.md` (the previous, stale report; replaced by this file)

**Telegram event and task paths**

- `backend/bot/handlers/task_events.py`
- `backend/bot/handlers/anti_edit_delete.py`
- `backend/services/anti_edit_delete_service.py`
- `backend/ai/task_event_dispatcher.py`
- `backend/ai/task_answers.py`
- `backend/ai/database/task_repository.py`
- `backend/runtime/supervisor.py`
- `backend/ai/task_scheduler.py`
- `backend/runtime/heartbeat.py`
- `backend/runtime/keepalive.py`
- `backend/runtime/tg_retry.py`
- `backend/bot/handlers/ai_unified.py`
- `backend/bot/handlers/misc.py`

**Database and attribution**

- `backend/db/transport.py`
- `backend/db/client.py`
- `backend/observability/egress_attribution.py`
- `backend/observability/db_stats.py`
- `backend/ai/database/manager.py`
- `backend/ai/database/session_repository.py`
- `backend/ai/database/message_repository.py`

**Bandwidth, Render, and upload seams**

- `backend/services/outbound_bandwidth_service.py`
- `backend/services/render_bandwidth_service.py`
- `backend/services/render_bandwidth_sources_diagnostic.py`
- `backend/services/save_service.py`
- `backend/services/retrieve_service.py`
- `backend/services/appearance_service.py`
- `backend/services/groq_stt_engine.py`
- `backend/services/speechmatics_stt_engine.py`
- `backend/services/settings_service.py`
- `backend/telegram_api/media.py`
- `backend/ai/tools/delivery.py`

**Web and dashboard**

- `backend/web/app.py`
- `src/App.tsx`

**Tests**

- `tests/test_external_call_efficiency.py`
- `tests/test_outbound_bandwidth_policy.py`
- `tests/test_outbound_bandwidth_governor.py`
- `tests/test_outbound_upload_governance.py`

---

## 9. Files changed

**Only `INVESTIGATION.md`.**

No production code, test, configuration, dependency, database, or deployment file was modified. The investigation was read-only; no live Render, Telegram, Supabase, or provider request was made.

---

## 10. Unknowns and limitations

1. **Production deployment commit is unknown.** The code at repository HEAD `31406f7` predicts tier 90 at 4 GB, but the running Render commit was not verified from this workspace.
2. **No live Render logs or credentials.** The supplied observations are treated as evidence; the source diagnostic’s raw points and `parts_match_total` were not re-fetched.
3. **No per-request byte measurement.** Header sizes, TLS record overhead, retransmissions, and TCP/IP overhead are not measured.
4. **No Telegram wire-level measurement.** MTProto framing, encryption, ACKs, keepalives, and retransmissions are not measured.
5. **No production message rate or owner share.** The 7.7–8.7 messages/second figure is derived from the operation count, not observed directly. The Anti-Edit enabled/populated state is also unknown, so its conditional 3–9 calls per eligible private message cannot be ruled in or out from telemetry alone.
6. **No `governor_monthly_bytes` or `policy_floor_bytes` values.** The effective 5.61 B cannot be decomposed into Render, local ledger, and floor from the supplied snapshot.
7. **Attribution is process-local and post-enable.** It cannot serve as a month-to-date November baseline and does not aggregate across processes.
8. **The source split is incomplete.** 225 points and 368 missing hours; missing hours are not zero-filled and may be zero-traffic or unpublished.
9. **The code is authoritative.** The implementation report’s “≥100%” row conflicts with the current effective-policy code; this report follows the code.
