# INVESTIGATION — whole-runtime responsiveness of the Telegram self-bot

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Worktree branch | `freebuff/99799eefb106784388e20bc8` |
| **HEAD at investigation start** | **`a4b6e4f1479c172a824b3084233b9d108c08d90d`** (`perf(callbacks): cut redundant round trips and coalesce burst renders`), equal to the remote tip this clone fetched (`git rev-parse FETCH_HEAD`) |
| Working tree at start | clean |
| Date | 2026-10-23 (UTC) |
| Symptom under investigation | the freeze/backlog after rapid inline-button clicks persisted after the phase-1/2 callback work |
| Scope | the complete runtime, with measurements concentrated on the click path (update → ack → turn → handler → database/Telegram → paint) |
| Explicitly out of scope | Save pipeline semantics, the AI runtime's own behaviour, Taskloom scheduling, Supabase schema, Render, any live account |
| **Status** | **Two application defects CONFIRMED for the remaining burst cost, one secondary lock defect CONFIRMED and fixed, one residual cost CONFIRMED and left in place (documented).** One amplifier hypothesis remains UNCONFIRMED |

### Method and its limits

* Every source claim was re-read from the working tree at `a4b6e4f` (and, for
  "before" numbers, from the same tree with this change stashed).
* **No production evidence was accessible in this workspace**: Render logs could
  not be read and no Telegram session exists. The phase-1 live numbers
  (`ack_ms` 160–296, `wait_ms` ≤ 352, `work_ms` 368–668, `total_ms` 528–1095, low
  CPU) are used only as the latency budget for the fakes.
* Timing evidence comes from `logs/bench_panel_burst.py` and
  `tests/test_callback_latency_burst.py`, which install the **real** callback
  router, the **real** panel handlers and the **real** gate/lifecycle, and fake
  only the two network ends at fixed latencies.
* Consequence: the numbers prove what a click waits on and how many round trips
  it makes. They do not prove what Telegram or Supabase answer.

---

## 1. The runtime latency map (Phase 2)

```
Telegram update ──► helper-bot client (Telethon, one task per update)
                    │
                    ├─ guard: is_owner (integer compare)
                    ├─ callback_gate.admit  (query_id dedupe, bounded 128)
                    ├─ callback_gate.ack    ── 1 Telegram RPC (the spinner)
                    ├─ resolve session      (in-memory registry)
                    ├─ supersede background writer (in-memory)
                    ├─ PER-PANEL TURN (FIFO future queue)
                    │   ├─ handler: services + db_client.run_sync_db (4 threads)
                    │   └─ returns the view          ◄── turn released here
                    └─ PAINTER (one per message): newest view → event.edit
                                                      ── 0..1 Telegram RPC
```

| Stage | Where | Cost observed |
|---|---|---|
| Update receipt / dispatch | `telethon/client/updates.py:289` | concurrent, per-update task |
| Acknowledgement | `callback_gate.ack` | one RPC (`ack_rpc_ms`), never waits for work |
| Turn wait | `callback_gate.acquire_turn` | 0 for an idle panel; grew with the queue (was the visible freeze) |
| Handler | panel/action handlers → services | 0 ms (pure views) to ~120 ms (one Supabase read) |
| Database | `db_client.run_sync_db` (4 workers, 10 s watchdog, 8 s transport) | ~120 ms per call on Render; **inside** the turn |
| Telegram edit | `panels._safe_edit` | ~250 ms |
| Paint | `callback_gate.PanelPainter` | outside the turn since this change |

Global/shared primitives audited: `lifecycle._lock` (panel create/close — **was**
held across an RPC), `_painters` (one per message), `_turns` (one per message),
`_save_code_lock` (save-code allocation only), `db._db_executor_lock` (pool
construction), provider semaphores (AI only), Taskloom semaphores (task
occurrences only), `callback_trace._flush_lock` (unused module).

---

## 2. Confirmed findings

| # | Finding | Evidence | Status |
|---|---|---|---|
| 1 | The FIFO turn spanned the Telegram edit, so N clicks on one panel meant N serialised edits and every click waited for an edit it would overwrite. 15 clicks with a trivial handler = 3.9 s, 15 edits | source (`_callback_router`) + benchmark | **CONFIRMED, FIXED** |
| 2 | The phase-2 coalescing was opt-in for ONE handler (`retrieve`'s tick); every other panel kept (1) | source (`SKIP_RENDER` only returned by `_toggle_selected_item`) + benchmark on `menu`/`health` | **CONFIRMED, FIXED** (replaced by a general writer) |
| 3 | `PanelLifecycleManager.close_panel`/`timeout_panel` performed the closing edit **inside** `_lock`, and `create_panel`/`try_reuse_panel` hold it across the inline trigger + edit, so a Close (or auto-close timer) serialised unrelated panel operations process-wide | source (`lifecycle.py`) + measurement: opening a panel during a slow close waited **230.6 ms** | **CONFIRMED, FIXED** |
| 4 | A panel that reads the database per click pays one read per click **inside** the turn, so a burst still costs ≈ N × read (30 clicks ≈ 3.6 s) | benchmark | **CONFIRMED, NOT FIXED** — ordered views require it; documented in §5 |
| 5 | Handlers that raised were logged but reported `completed` (the dispatch helpers swallowed the exception) | source | **CONFIRMED, FIXED** — a raising handler is now `failed` |

## 3. Explicitly ruled out (this phase, current source)

* **Telethon serialising updates** — `updates.py:289` creates a task per update;
  neither client sets `sequential_updates`. A slow handler cannot delay the next
  click's delivery, which the burst benchmarks show directly.
* **Event-loop blocking** — the only `time.sleep` calls in the runtime
  (`speechmatics_stt_engine._sleep_briefly`, `gemini_media_engine._await_file_ready`)
  live in synchronous engine methods that are invoked through `asyncio.to_thread`
  (`ai/stt_provider_probe.py:329` and the media-engine boundaries). No
  `requests`, `subprocess`, `run_until_complete` or `.result()` on a request path.
* **Database-pool saturation as the cause of the freeze** — the pool is bounded
  (4 workers) with a 10 s watchdog and an 8 s transport deadline. A saturated
  pool now has a test proving it degrades to a bounded `OperationTimeoutError`
  and that an unrelated panel still answers and paints meanwhile. Saturation
  would show as `db_ms`, which the timing line reports.
* **Heartbeat/keepalive/failsafe** — no database work, no panel turn.
* **`callback_trace`** — the module is not imported anywhere in the runtime.
* **AI handler / providers / tool executor** — `ai_unified` listens on the
  **self** client for outgoing messages; provider and tool calls are `httpx`
  awaits inside their own task, bounded by AI-only semaphores. They cannot sit in
  the click path.
* **Duplicate delivery** — suppressed by `query_id` since phase 1; the benchmark
  harness re-confirmed it (reused query ids had 15 clicks suppressed, which is
  why the harness resets the gate per cell).
* **Unbounded queues / task leaks** — one future per waiting click (bounded by
  delivered updates), one painter per active message (forgotten on close), bounded
  timing (64) and duplicate (128) stores.

## 4. Open hypothesis (not a finding)

Telegram rapid-edit throttling: frequent edits of one message are widely reported
to be rate-limited, and both clients set `flood_sleep_threshold=60`, so Telethon
sleeps inside the call on a flood answer. Thirty edits of one message in a few
seconds is exactly that pattern. This change cuts edits per burst by 87–93 % on
I/O-free panels, which removes most of the exposure — but no `FLOOD_WAIT` was
observed here, so it is not claimed as a cause.

## 5. Why the fix takes this shape

* **Paint is a projection, not state.** The turn must order *state* (handlers
  read the panel's session/nav and may write it). A paint is a pure function of
  the view the handler just built, so it does not need the turn and must not hold
  it.
* **One writer per message, newest view wins.** Edits to one message must not run
  concurrently (two in flight can land out of order); a queued view is worthless
  the moment a newer one exists. A one-slot painter gives both: serialised
  writes, newest wins, and N clicks cost at most two edits.
* **No task per click.** The first click's task drives the drain loop and picks
  up whatever the queue published; a second click publishes into the slot and
  returns. Nothing is spawned, nothing is queued beyond one slot, and a click
  whose view is superseded is still applied exactly once.
* **The turn remains FIFO and per panel.** Different panels never wait for each
  other, and a handler always sees the state left by the previous click.
* **The lifecycle lock guards state only.** Resource release is synchronous; the
  closing edit is a courtesy to the client and moved outside the lock, so one
  panel's close cannot serialise another panel's create.
* **Observability before and after.** `ack_rpc_ms` separates the answer RPC from
  application latency, `work_ms` is handler-only, `paint_ms` is the paint, and
  `db`/`edits`/`coalesced` state what the click actually cost — measured at the
  one dispatcher (`run_sync_db`) and the one edit path (`_safe_edit`), never
  estimated.

---

## 6. Historical investigations (kept, superseded, not relied on)

* **2026-10-23 — phase 2 (round-trip reduction + Stash snapshot).** Its confirmed
  findings (4 DB round trips per Stash tick; one edit per tick) and its Stash page
  snapshot stand. Its *cost* conclusion — that a serialised burst is inherently
  `N × (read + edit)` — is superseded: the edit was never part of ordering, and a
  burst on an I/O-free panel is now constant-time. Its `SKIP_RENDER` opt-in is
  replaced by the panel writer.
* **2026-10-23 — phase 1 (acknowledgement + ordering).** Ack-first, one FIFO turn
  per panel, `query_id` duplicate suppression: all still in force and still
  verified (ack is one RPC; handlers on one panel never overlap). Its replay of
  Telethon's dispatch (`updates.py:289`) was re-checked against the pinned wheel.
* **2026-10-22 — "can the Helper Bot edit an existing inline panel
  asynchronously?"** Verdict was UNRESOLVED (no runtime evidence that Telegram
  hands the Helper Bot an inline-message identifier for these panels). Its library
  facts stand and are what `backend/helper/panel_edit.py::ExistingPanelEditor`
  relies on. This task neither depends on nor changes it.
* Earlier Save-pipeline reports (view-once media, SAVE_TRACE, deep-save/album
  contracts, the Stash rename and multi-select) describe behaviour this change
  does not touch; their current contracts are in `AGENTS.md`.

---

## 7. Residual risk

* **A skipped paint is only as good as the next paint.** If the click queued
  behind a view fails before publishing, the panel keeps the previous view until
  the next click. State stays authoritative and every panel reads state, so no
  operation is affected — the visible marks can lag.
* **A paint racing a close is narrowed, not eliminated** (the close edit is not
  routed through the painter; window = one edit RPC; outcome = a stale,
  unclickable panel).
* **The residual read-per-click** keeps DB-backed bursts linear in N. Reducing it
  needs a read-only/mutating handler taxonomy or concurrent reads for ordered
  views — deliberately not introduced here.
* **Live proof is missing.** That a real client sees a 30-click burst answered
  immediately and the panel ending on the newest view must be observed in
  production; the report does not claim it.

---

## 8. Phase 3 — residual callback database latency (2026-10-23)

> **Investigation only.** This section changes no application code, no test and
> no schema. It records what was inspected, what was measured offline, and what
> is still unknown.

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Worktree branch | `freebuff/dcc722156bb0a1a080890e10` |
| **HEAD** | **`8a0c5b95cc335656c75f9be476487fdf9ebdad74`** (`perf(callbacks): paint outside the panel turn through one writer per message`) |
| Remote tip at start | `git fetch origin` → `FETCH_HEAD` = `8a0c5b95cc335656c75f9be476487fdf9ebdad74` — **identical to HEAD**, so this worktree is exactly the remote tip (`main`). The worktree has no local `origin/main` ref, so equality is established by SHA identity with the fetched tip, not by an ancestry walk |
| Working tree | At the start of the analysis: `git status --porcelain` → **empty**, no untracked files, nothing stashed. The **only** modification in the tree now is this section of `INVESTIGATION.md` (419 added lines, 0 deleted at final inspection); no application file, test or config is modified |
| Date | 2026-10-23 (UTC) |
| Live Telegram / Supabase / Render | **not touched** — no session, no RPC, no SQL, no `.env`, no deployment, no Render shell |
| Documents not modified | `AGENTS.md`, `IMPLEMENTATION_REPORT.md`, `DATABASE_ARCHITECTURE.md`, `README.md` |
| Interpreter / deps | CPython 3.12.3, `backend/requirements.txt` installed into a throwaway venv (`telethon==1.43.0`, `supabase==2.4.2`, `postgrest==0.16.11`); **no repository dependency changed** |

### 8.1 What the per-panel turn still contains

The painter change moved *painting* out of the turn. Everything a handler
`await`s is still inside it: `_callback_router` acquires the turn (`panels.py:491`),
`_handle_panel` awaits the handler (`panels.py:557`, dispatch at `:523`), and the
turn is released in the `finally` (`panels.py:541`) — i.e. **after** the handler's
database reads have completed. Painting is the only thing that left.

### 8.2 Remaining read paths, with call chains (source-confirmed)

| Click (callback data) | Handler chain | Supabase HTTP requests inside the turn |
|---|---|---|
| `panel:retrieve` (Stash menu) | `_retrieve_panel_handler` (`retrieve.py:94`) → `db_client.count_saves` → `_count_saves_sync` | **1** |
| `panel:retrieve_saved` / `:page:N` | `_retrieve_saved_panel_handler` (`:420`) → `_render_saved_browser` (`:263`) → `_saved_page(..., fresh=True)` (`:265`) → `db_client.list_saves` → `_list_saves_sync` (`db/client.py:454`) | **2** — the page query (`:458-464`, `.select("*")` at `:460`) **and** a second count query (`:466-471`, `.select("id", count="exact")` at `:468`) |
| `panel:retrieve_saved:select` / `:back` / `:cancel` | `_render_selection(..., fresh=True)` (`:429`, `:431`, `:437`) → `_saved_page` → `list_saves` | **2** |
| `panel:retrieve_item:id:CODE` | `_retrieve_item_panel_handler` (`:455`) → `retrieve_service.load_saved_item` → `db_client.query_save` → `_query_save_sync` | **1** |
| `action:retrieve_toggle:CODE` (snapshot hit) | `_toggle_selected_item` → `_snapshot_row` (in-memory) | **0** |
| `action:retrieve_toggle:CODE` (snapshot miss) | `_snapshot_row` miss → `load_saved_item` (1) → `_render_selection` (0, snapshot) | **1**; **3** when the row is gone (invalidate + fresh page, `retrieve.py:369`) |
| `panel:retrieve_saved:bulk:send` / `bulk:delete` (review) | `_bulk_review` (in-memory selection) | **0** |
| `action:retrieve_bulk_send/delete:confirm` | `retrieve_service.do_bulk_*` — re-verifies every code | mutating path; **must** stay serialized |
| Emoji library page ≥ 2 | `_library_page_handler` (`emoji.py:261`) reads page 0 for the total (`:265`) **then** the requested page (`:268`) → `list_emoji_entries` → `_list_emoji_entries_sync` (2 requests each) | **4** |
| Emoji categories / mappings list | `_list_emoji_categories_sync` (`db/client.py:1434`), `_list_emoji_mappings_sync` (`:1710`) — same page+count pattern (rows) | **2** each |
| Cipher → private-message policy panel | `_render_actions` → `_counter_line` → `service.counter_rows` → `list_private_message_senders` | **1** |
| `action:settings_toggle_*` | handler (`misc.py:385-413`) → `settings_service.toggle_*` → `set_setting` (`settings_service.py:201`) → `repo.update_field` (`panel_settings_repository.py:59`) **then** `_refresh_from_db` (`settings_service.py:160`) → `repo.load` (`panel_settings_repository.py:41`) — **both synchronous, on the event loop** | **2, blocking** |
| Bio / Username panels, Database stats, Discover list/find | `get_or_create_*_state`, `list_all_saves` (whole table), `list_recent_saves` / `search_saves` | 1 each (source-scanned, not measured per click) |
| Menu, Health, Context, Font, top-level Cipher, navigation | no database call | **0** |

Two facts matter more than the individual rows:

1. **`list_saves` is two round trips, not one.** The previous phase's "one DB
   read per click" counts *Python calls* to `db_client.list_saves`. The sync
   function behind it issues the page query and a separate exact-count query.
2. **A page overflow doubles it again.** `_saved_page` re-reads the page when the
   requested page exceeds `total_pages` (`retrieve.py:255-258`), so one click can
   cost 4 requests after deletions.

**Callback categories (question B).** The taxonomy below is derived from the call
chains above; it is deliberately not a single caching rule for every click.

| Category | Representative callbacks | DB work per click | Stance |
|---|---|---|---|
| Pure navigation / rendering | `panel:_nav:home` / `back` / `noop`, Menu, Health, Context, Font, Cipher category | 0 | leave untouched |
| Read-only views | Stash browser/page, Stash selection open/back, item preview, Emoji library/categories/mappings, List/Find | 1–4 requests | **candidate A** (remove a redundant query, never add a cache) |
| State-changing, reversible | selection tick, page/selection state, settings toggle, reaction refresh | 0–2 (settings: 2 **blocking**) | **candidate B** for settings; FIFO ordering must stay |
| Irreversible / externally visible side effects | bulk delete confirm, single-item delete, retrieve/send, Save | mutating | no optimization proposed; ownership re-verification stays |
| Requires fresh existence / authorization | item preview by code, bulk action per code, search resolution | 1+ per item | **must stay** (the read is the contract) |

### 8.3 Why the existing metrics hid this (source-confirmed + measured)

* `callback_gate.note_db` is called **once per `run_sync_db` dispatch**
  (`db/client.py:251`), and `run_sync_db` wraps the *whole* sync function. A page
  read therefore logs `db=1 db_ms≈241` while issuing **2** HTTP requests. This is
  measured, not inferred: see §8.4 table C (current page read `db=1`, `db_ms=240.7`;
  the one-request path reports the same `db=1` with `db_ms=120.7`).
* The burst suite's `_Db` (`tests/test_callback_latency_burst.py`) monkeypatches
  `db_client.list_saves`, so its `db_reads` counter is also Python-call scoped and
  cannot see the second query.
* The panel-settings write path never enters `run_sync_db` at all, so its two
  blocking requests are **invisible** in `[CALLBACK] timing` (`db=0`).

### 8.4 Measurements (current HEAD, offline, reproducible)

**Method.** A throwaway probe (kept outside the repository — `/tmp/probe/`,
because this task may only change `INVESTIGATION.md`) imports the production
router, panels and DB layer, registers the **real** callback router, seeds the
lifecycle session, and replaces exactly one thing: `backend.db.client.get_db()`
returns a fake Supabase client whose `.execute()` **counts one HTTP request** and
sleeps a fixed 120 ms. Telegram `answer`/`edit` are fixed fakes (0 ms / 250 ms).
Three runs per cell; the numbers below reproduced within ±2 ms, and were
re-run a second time on the same revision (tables A–D: 20 requests / 2751.8 ms
current vs 10 requests / 1628.3 ms single-request; loop gap 240.5 ms vs 5.9 ms).

```bash
# revision under analysis (repository root)
git rev-parse --abbrev-ref HEAD      # freebuff/dcc722156bb0a1a080890e10
git rev-parse HEAD                   # 8a0c5b95cc335656c75f9be476487fdf9ebdad74
git fetch origin && git rev-parse FETCH_HEAD   # identical sha → HEAD == remote tip

python3 -m venv /tmp/venv
/tmp/venv/bin/pip install -q -r backend/requirements.txt pytest pytest-asyncio

/tmp/venv/bin/python /tmp/probe/residual_probe.py   # tables A–D
/tmp/venv/bin/python /tmp/probe/wire_check.py       # table E

/tmp/venv/bin/python -m pytest tests/test_callback_backlog.py \
  tests/test_callback_latency_burst.py tests/test_retrieve_multi_select.py \
  tests/test_retrieve_owner_isolation.py tests/test_saved_item_preview_metadata.py \
  tests/test_saved_item_sender_and_retrieve.py tests/test_helper_callback_receipt.py \
  tests/test_save_success_panel.py tests/test_supabase_transport_http1.py \
  tests/test_save_v2_wiring.py -q          # 188 passed (re-verified)
```

The two probe scripts are **not** committed (this task permits changing only
`INVESTIGATION.md`); the Method paragraph above specifies exactly what they fake,
so they can be recreated from this description.

**Table A — HTTP requests per single click (0 ms fake).**

| Click | Requests | Edits |
|---|---|---|
| `panel:retrieve` | 1 | 1 |
| `panel:retrieve_saved` (browse) | **2** | 1 |
| `panel:retrieve_saved:page:2` | **2** | 1 |
| `panel:retrieve_saved:select` (open) | **2** | 1 |
| `panel:retrieve_item:id:S0003` | 1 | 1 |
| `action:retrieve_toggle:S0001` (snapshot hit) | 0 | 1 |
| `panel:_nav:home` (Menu) | 0 | 0 |
| `action:settings_toggle_*` (synchronous path) | **2** | — |

**Table B — 10 clicks on `panel:retrieve_saved:page:2`, 120 ms/request, 250 ms/edit.**

| Variant | p50 total | Requests | Edits |
|---|---|---|---|
| Current (page + count) | **2750.9 ms** (2750.9–2752.4, 3 runs) | **20** | 10 |
| Single request per page (emulated in the probe only) | **1626.0 ms** (1625.5–1627.0) | **10** | 6 |

Δ ≈ **−1125 ms (−40.9 %)** for the burst and **−10 HTTP requests**. Edit counts
differ between runs (10 vs 6) because coalescing depends on paint timing; the
request counts and the DB portion do not.

**Table C — single-click admission record (`work_ms`, `db`, `db_ms`).**

| Click | work_ms | db (dispatches) | db_ms |
|---|---|---|---|
| Page read, current | **241.0** | 1 | 240.7 |
| Item preview (1 request) | 120.9 | 1 | 120.6 |
| Page read, one request | **120.8** | 1 | 120.5 |

This is the cleanest statement of the residual: **the page read costs exactly one
extra HTTP round trip versus a one-request panel**, and the click's reported
`db` counter does not show it.

**Table D — the settings toggle blocks the loop (120 ms/request fake).**

| Variant | Call duration | Max event-loop gap | Requests |
|---|---|---|---|
| Same sync call moved to `asyncio.to_thread` (baseline) | 241.2 ms | **5.9 ms** | 2 |
| `settings_service.set_setting` on the loop (current) | 240.3 ms | **240.5 ms** | 2 |

While that call runs, **no** callback can be admitted, acknowledged or painted —
this is a different failure mode from a slow turn, and it is not visible in any
callback timing field.

**Table E — wire-level check of the proposed query (local HTTP server, no
Supabase).** Using the real `backend.db.transport.create_http1_client` against a
local server that answers like PostgREST (`Content-Range: 8-15/20`):

```
wire requests : 1
path          : /rest/v1/saved_items?select=%2A&owner_id=eq.7&order=created_at.desc&offset=8&limit=8
prefer header : count=exact
data rows     : 8 (expected 8)
count         : 20 (expected 20)     VERDICT: PASS
```

So `select("*", count="exact").range(a, b)` sends **one** request that carries the
owner filter, the page window **and** `Prefer: count=exact`, and postgrest-py
parses the total from that same response's `Content-Range`
(`postgrest/base_request_builder.py:164-193`). The database-side work is
identical to today's separate count; only the transport round trip disappears.

**Tests run on this HEAD** (so the baseline is measured, not remembered):
`pytest tests/test_callback_backlog.py tests/test_callback_latency_burst.py
tests/test_retrieve_multi_select.py tests/test_retrieve_owner_isolation.py
tests/test_saved_item_preview_metadata.py
tests/test_saved_item_sender_and_retrieve.py tests/test_helper_callback_receipt.py
tests/test_save_success_panel.py` → **134 passed**;
plus `test_supabase_transport_http1.py test_48_font_panel.py
test_private_message_panel.py test_save_v2_wiring.py` → **113 passed**; and a
second pass over ten of those files in one command → **188 passed** (exact
command in §8.4).
`test_callback_latency_burst.py -s` on this HEAD prints
`[burst-10] total_ms=42.5 edits=2 db_reads=0 state_p50_ms=1.93` (10/15/30 all
≈41–43 ms). The printed `state_p50_ms` differs from the block quoted in
`IMPLEMENTATION_REPORT.md` (21.39 ms) by ~10× — that figure is machine/scheduling
dependent and must not be compared across hosts; `total_ms`, `edits` and
`db_reads` are the portable structural numbers.

**Limitations of this evidence.**

* 120 ms/request and 250 ms/edit are the *previous phase's* log-implied fakes,
  not production latency. The percentage improvement equals the percentage of
  round trips removed; the absolute production gain is **unknown**.
* Nothing here was measured against Supabase, Telegram or Render. The 120 ms fake
  is a modelling assumption.
* The PostgREST **server-side** guarantee (a limited/offset query with
  `Prefer: count=exact` returns the *total* in `Content-Range`) is documented and
  is what the client parses; it was **not** verified against the project's
  Supabase. Only the client half is verified locally (table E).
* `logs/bench_panel_burst.py`, cited throughout `IMPLEMENTATION_REPORT.md` §5, is
  **not in the repository** (`git ls-files` has no `logs/` path). Those numbers
  cannot be reproduced from this tree; §8.4 comes from a new probe instead.
* The probe lives outside the repo and is not committed, so the measurement is
  described rather than shipped.

### 8.5 Correctness constraints per candidate

**A — one request for page + total (`_list_saves_sync` and the three emoji list
functions).**

* The count must keep the **same owner predicate** as the rows; the current count
  query is `.eq("owner_id", …)` and the merged one must be too. A count without
  the filter would leak the global row count across owners (not row data, but
  still wrong and not owner-scoped).
* `total` must be the **full filtered count**, not the page length: `total_pages`
  drives the page clamp, the `page N/M` label and the `Prev`/`Next` buttons.
* If the server ever returns `count=None`, the code must **fall back to the
  existing separate count query** — never treat `None` as 0 (that would collapse
  pagination to one page and hide every later page).
* Ordering (`created_at desc`), offset arithmetic, the page-overflow re-read and
  the snapshot write (`_snapshot_put`) are unchanged.
* Read-only: no state mutation, no change to which click applies which action,
  no change to the FIFO turn. The read stays **inside** the turn.
* Same reasoning applies verbatim to `list_emoji_entries`,
  `list_emoji_categories`, `list_emoji_mappings`.
* **Pool / timeout:** A removes a request and adds no concurrency, so it cannot
  increase pool pressure. The remaining call still runs through the one
  `run_sync_db` dispatch with the same 10 s watchdog and 8 s transport deadline;
  a transport failure is still swallowed by `_list_saves_sync`'s existing
  `except` and returns the in-memory fallback, exactly as today (today a failed
  page query already yields an empty page and a zero total).
* **Close / timeout interaction:** A touches no painter, no lifecycle state and
  no turn; the read stays inside the same FIFO turn, so a click that queued
  behind a Close is still dropped with `dropped:panel-closed` at the same point
  (`panels.py:505-509`) and a panel auto-close timer behaves unchanged.
* **Bulk interaction:** A leaves `_snapshot_put` and `_snapshot_invalidate`
  untouched, so a bulk delete still invalidates the page and the bulk
  send/delete path still re-verifies every code before acting.

**B — panel-settings writes off the event loop.**

* A toggle is a **state mutation** whose value decides the *next* click's
  admission (`is_owner_only`, `is_debug_callbacks`, `is_auto_close_enabled`), so
  it must stay applied-before-response and serialized; it may not be moved to a
  fire-and-forget task.
* The write + cache contract must survive: either keep the read-after-write, or
  update the cache from the validated value that was just written (the value is
  already validator-checked; there are no DB defaults/triggers for these
  columns).
* `repo.load()` / `update_field()` are synchronous by design; making them await
  `run_sync_db` changes `settings_service.set_setting` and every public
  `set_*`/`toggle_*` to `async`, which ripples to ~20 backend call sites
  (`misc.py`, `web/app.py`, `ai/tools/settings.py`, `helper/panel_settings.py`)
  plus their tests.
* `load_all()` is also called from `RuntimeSupervisor.start()` (`supervisor.py:178`)
  inside an `async def` — one blocking request at boot; same class of defect,
  lower impact.

**C — reuse the page snapshot for the browse view.** **REJECTED.**
`_snapshot_invalidate` has callers only inside `retrieve.py` (`:369`, `:428`,
`:434`, `:795`); `save_service` never invalidates it. Reusing the browse snapshot
would therefore keep a just-saved item invisible for up to the 60 s TTL — a
freshness regression on the primary "show my saved items" view, to save one
request that A already halves.

**D — emoji library page.** A handler restructure can drop the page-0 probe
because the page query itself would carry the total: **4 → 1** request for page
≥ 2. Correctness constraints: the page number must be clamped only after the
read, so the first render of a stale page can briefly request an empty page
(existing behaviour already re-reads/clamps); the current `_page_of` helper needs
total before computing the offset. Medium risk, bigger win than A for that panel.

**Rejected outright:** concurrent reads on one panel (two page handlers racing
was exactly the phase-1 corruption: both read page 1, the later edit wins), any
unbounded or write-unaware cache, and any scheme that drops or defers accepted
clicks.

### 8.6 Ranked recommendation

1. **A — merge page + exact count into one request** in `_list_saves_sync` and
the three emoji list functions. Strongest evidence-to-risk: measured **2 → 1**
HTTP requests per page click, **−120 ms per click (−49.9 %)** and **−1125 ms per
10-click burst (−40.9 %)** at the stated fakes; zero API change; no new caching,
concurrency, task, queue or ordering rule; the existing tests stub
`db_client.list_saves` at the Python boundary, so they are unaffected; a
fail-closed fallback keeps pagination correct if `count` is absent.
2. **B — move the panel-settings write off the loop (and ideally drop the
read-after-write).** Highest severity per click (a **240 ms** process-wide stall
measured, vs **5.9 ms** when the same call is threaded), but a real async API
change across ~20 call sites and their tests. Do it after A, as its own change.
3. **D — emoji library 4 → 1 request.** Worth doing with or after A, in the
handler rather than the DB layer.
4. **Not recommended now:** C (staleness), concurrent reads (corruption),
snapshot caching for the browse view (freshness), and optimising
`panel:retrieve_item` (its single verified read is the freshness/authorization
contract itself).

If the owner wants one change only, it is **A**.

### 8.7 Acceptance criteria and regression tests for phase A

Implementation scope: `backend/db/client.py` only (four list functions). No
change to `callback_gate`, `panels`, `lifecycle`, `retrieve.py` handlers or
tests. Acceptance:

1. **One request per listing.** With a fake Supabase client that counts
   `.execute()`, each of `_list_saves_sync`, `_list_emoji_entries_sync`,
   `_list_emoji_categories_sync`, `_list_emoji_mappings_sync` issues **exactly
   one** request and returns the same `(rows, total)` as the current code for:
   empty table, exactly one full page, a partial last page, `offset = 0`,
   `offset > 0`, and an offset past the end.
2. **Owner isolation.** The counting request carries `owner_id=eq.<owner>` (same
   predicate as the page), and another owner's rows never appear in `rows` or in
   `total` — extend `tests/test_retrieve_owner_isolation.py` with the fake-client
   assertion.
3. **Fail-closed fallback.** A fake response with `count=None` makes the code
   issue the separate count query (2 requests) and still report the true total;
   `total` must never become 0 in that case (`_saved_page` would then clamp every
   page to 1).
4. **Wire test.** A local `ThreadingHTTPServer` test (the pattern already in
   `tests/test_supabase_transport_http1.py`) asserting: one HTTP request,
   `Prefer: count=exact`, `offset`/`limit` in the query string, and `count` parsed
   from `Content-Range` (`8-15/20` → 20).
5. **Burst regression.** In `tests/test_callback_latency_burst.py`, drive 10
   `panel:retrieve_saved:page:2` clicks through the real router with a counting
   fake DB and assert **10** HTTP requests (was 20), every click applied exactly
   once, and the final painted view being the newest page; keep the existing
   ≤3-edits / coalescing assertions unchanged.
6. **No behavioural drift in pagination.** Existing label/clamp behaviour:
   page 1 of 3 shows `Next`, page 3 shows `Prev`, a deleted page clamps to the
   last non-empty page — covered by the existing multi-select and owner-isolation
   suites, which must pass unchanged.
7. **Unchanged guarantees.** FIFO per-panel ordering, `query_id` duplicate
   suppression, exactly-once application of each accepted click, the snapshot
   invalidation rules and the painter contract are not touched; the diff must
   not remove or reorder any `await` on the click path.

**Regression suites to run for that change:** `test_callback_latency_burst.py`,
`test_callback_backlog.py`, `test_retrieve_multi_select.py`,
`test_retrieve_owner_isolation.py`, `test_saved_item_preview_metadata.py`,
`test_saved_item_sender_and_retrieve.py`, `test_save_v2_wiring.py`,
`test_helper_callback_receipt.py`, `test_save_success_panel.py`,
`test_supabase_transport_http1.py` — all of these are **green on this HEAD**
(134 + 113 passed as recorded in §8.4), so any red after the change is caused by
the change.

### 8.8 Instrumentation: what exists and what is missing (question 7)

The `[CALLBACK] timing` line already separates the click's stages honestly:
`ack_ms` (receipt → answer), `ack_rpc_ms` (the answer RPC itself), `wait_ms`
(answer → dispatch, i.e. FIFO queueing), `work_ms` (dispatch → handler done),
`paint_ms`, `total_ms`, plus what the click cost (`db`/`db_ms`, `edits`/`edit_ms`,
`state_ms`, `coalesced`). That is enough to tell "slow handler" from "slow edit"
from "waiting behind an earlier click".

Three gaps remain, all source-confirmed:

1. **`db` counts dispatches, not HTTP requests.** A 2-request page read logs
   `db=1` (measured, §8.4 C). Instrumenting at `_timed_db_call` would not fix
   this either — it also wraps one *function* call, not one request.
2. **The synchronous settings path is invisible.** `repo.update_field` /
   `repo.load` never enter `run_sync_db`, so the click reports `db=0` while
   blocking the loop for 2 round trips (§8.4 D).
3. **No split between executor queueing and transport time.** `db_ms` starts at
   dispatch, so pool queueing and the request are summed.

Minimal proposal (not implemented): add request counting at the **one transport
boundary** the codebase already owns — `HTTP1Session` in
`backend/db/transport.py` can be built with `event_hooks={"request": [...]}`
(httpx runs the hook on the calling thread), so a thread-local counter read by
`run_sync_db` yields a real `db_requests` alongside `db_calls`, and per-request
elapsed time can be reported separately from queueing. A `blocking_db=1` marker
(or simply moving the settings write off the loop, candidate B) would close gap
2. This is instrumentation only: it must never classify or change a result.

### 8.9 What was not changed, and what remains unverified

* **Files changed by this investigation: `INVESTIGATION.md` only.** No
  application code, no test, no SQL, no migration, no `AGENTS.md`, no
  `DATABASE_ARCHITECTURE.md`. No dependency added to the repository (the venv and
  the probe live in `/tmp`).
* No `.env`, session string, Telegram RPC, Supabase, Render or deployment was
  accessed. No branch was rebased, reset, force-pushed or committed to.
* **Unverified:** production latency and production `db_ms`; that the project's
  Supabase returns a total in `Content-Range` for a limited query (documented +
  client-verified, server-side unverified); that the settings toggle ever
  coincides with a burst in production; the split of `db_ms` between pool
  queueing and transport; the previous phase's `logs/bench_panel_burst.py`
  figures (the script is absent from the repository); the full test suite was
  not run here (the two relevant groups recorded above were).
* **Hypothesis, not a finding:** on a real Supabase the second (count) query may
  be substantially cheaper server-side than the page query (index-only), so the
  *latency* saved could be less than one full RTT — the round trip itself, which
  is what the network path costs, is still removed.

### 8.10 Status of earlier findings after this pass

| Earlier finding | Status | Basis |
|---|---|---|
| Phase 1 — acknowledge first, one FIFO turn per panel, `query_id` duplicate suppression | **VALID**, unchanged | source (`callback_gate.py`); 188 focused tests green on this revision |
| Phase 1 — lifecycle lock never spans a Telegram round trip | **VALID**, unchanged | source (`lifecycle.py`: `_cleanup_state_locked` then `_close_message`) |
| Phase 2 — Stash page snapshot makes a tick cost no read | **VALID**, re-measured | this pass: `action:retrieve_toggle` = **0** HTTP requests on a snapshot hit |
| Phase 2 — "a burst is inherently N × (read + edit)" | **STALE** (already superseded by the painter in §3/§6) | I/O-free panels are constant-time; paints, not reads, were the multiplier |
| Phase 2/3 — "DB-backed panels re-read the database **once** per click" | **STALE as a request count** | the metric counted `list_saves` calls; each call is **2** HTTP requests (`db/client.py:454-478`) |
| Phase 3 — "the residual read keeps DB-backed bursts linear in N" | **VALID in shape, refined** | the per-click constant is 2 requests for paged reads, 1 for single-row reads, 0 for snapshot-backed ticks |
| Phase 3 — `logs/bench_panel_burst.py` numbers in `IMPLEMENTATION_REPORT.md` §5 | **NOT INDEPENDENTLY VERIFIED** | the script is absent from the repository; only the in-suite structural figures could be re-run (≈41–43 ms, 2 edits, 0 reads) |
| Phase 3 — in-suite `state_p50_ms` figures (1.93 vs 21.39 ms) | **NOT COMPARABLE across hosts** | scheduling-dependent; `total_ms`, `edits`, `db_reads` are the portable numbers |
| 2026-10-22 — can the Helper Bot edit an existing inline panel? | **UNCHANGED / not relied on** | out of scope for this pass; `ExistingPanelEditor` behaviour untouched |

### 8.11 Independent re-run on the same revision (2026-10-23)

The whole pass above was re-executed from a fresh shell against the same
checkout, without trusting the previous run's output.

**State re-verified, not assumed.** `git rev-parse HEAD` →
`8a0c5b95cc335656c75f9be476487fdf9ebdad74`; `git fetch origin` → `FETCH_HEAD`
same SHA (still the remote tip); working tree held **only** this file. Nothing
was reset, rebased or discarded, and no branch was changed.

**Numbers reproduced** (`/tmp/venv/bin/python /tmp/probe/residual_probe.py`,
`/tmp/probe/wire_check.py`; same fakes and workload as §8.4):

| Quantity | §8.4 recorded | This run |
|---|---|---|
| 10-click burst, current (page + count) | 2750.9 ms / 20 requests / 10 edits | **2751.1 ms / 20 / 10** |
| 10-click burst, one request (probe only) | 1626.0 ms / 10 requests / 6 edits | **1625.9 ms / 10 / 6** |
| Page read `work_ms` / `db` / `db_ms` | 241.0 / 1 / 240.7 | **241.0 / 1 / 240.7** |
| Item preview `work_ms` (1 request) | 120.9 | **120.8** |
| Page read, one request | 120.8 / 1 / 120.5 | **120.7 / 1 / 120.5** |
| Toggle on the loop: call / loop gap | 240.3 / 240.5 ms | **240.5 / 240.6 ms** |
| Same call via `to_thread`: call / loop gap | 241.2 / 5.9 ms | **241.1 / 5.8 ms** |
| Wire check (local PostgREST-shaped server) | 1 request, `count=20` | **PASS, identical** |

All cells are within ±0.5 ms of §8.4; the variance is the 120 ms fake's timer
resolution, not a code difference (same SHA). Edit counts again vary with paint
coalescing (10 vs 6) while request counts do not — as §8.4 already states.

**Focused suites:** `pytest` over the same ten files listed in §8.4 →
**188 passed** (7.04 s), matching the recorded baseline.

**Source anchors re-checked line by line** against this revision. Seven citations
in §8.2–§8.3 were imprecise or pointed at the wrong module and are now corrected
(no finding changed):

| Claim | Was | Now (verified) |
|---|---|---|
| `note_db` call site | `db/client.py:249` (that line is the local import) | **`:251`** |
| page query inside `_list_saves_sync` | `:457` (that line is `try:`) | **`:458-464`**, `.select("*")` at `:460` |
| count query | `:466` (the `count_res = (` assignment) | **`:466-471`**, `.select("id", count="exact")` at `:468` |
| `_list_emoji_mappings_sync` | `:1709` | **`:1710`** |
| `_refresh_from_db` | `panel_settings_repository.py:160` — **no such function in that module** | **`settings_service.py:160`** (→ `repo.load` at `panel_settings_repository.py:41`) |
| page-overflow re-read | `retrieve.py:252-259` | **`:255-258`** |
| settings toggle handlers | `misc.py:386-411` | **`:385-413`** |

The conclusions are unaffected: `list_saves` still issues **two** requests
(`:458-464` and `:466-471`), the settings toggle still performs two synchronous
requests on the loop (`update_field` at `panel_settings_repository.py:59` then
`_refresh_from_db` → `load` at `:41`), and candidate **A** (§8.6) remains the
recommendation — still **not implemented**.

---

# 9. Phase 4 — panel content-update latency (2026-10-23)

**Question asked of this pass.** The owner re-tested the callback work and reports
*no noticeable improvement*. The remaining symptom is stated as **panel content
update latency on every panel** — the interval from a valid click to the point
where the *new content* is on screen — not the spinner, not backlog, not Stash
specifically. This section answers that question and nothing else.

**Evidence classes used below.**

* **(A) source-confirmed** at this revision — quoted file/line, no runtime.
* **(B) reproduced locally** with the real router/service/DB layer and faked
  network ends; the harness is `/tmp/probe/panel_latency_probe.py` (outside the
  repository, not committed).
* **(C) production measurement** — **none was available for this pass.** No Render
  log, no live Telegram session, no Supabase. Every statement about production
  magnitude is therefore a hypothesis, and is labelled as one.
* **(D) unknown / needs measurement.**

## 9.1 Verified repository state (A)

| Item | Value |
|---|---|
| Branch | `freebuff/dcc722156bb0a1a080890e10` |
| HEAD | `8a0c5b95cc335656c75f9be476487fdf9ebdad74` |
| Requested baseline | `8a0c5b95cc335656c75f9be476487fdf9ebdad74` → **matches exactly** |
| `git fetch origin` → `FETCH_HEAD` | `8a0c5b95cc335656c75f9be476487fdf9ebdad74` (identical; still the remote tip) |
| `origin/main` ref | none locally; equality is by **SHA identity** with the fetched tip, not an ancestry walk |
| Working tree at start | `M INVESTIGATION.md` **only** — this report. No unrelated changes, nothing reset/rejected/stashed |
| This pass changed | `INVESTIGATION.md` only. No code, test, SQL, migration, `.env`, secret, RPC or deployment |

**On prior numbers in this file.** §8 and §8.11 were measured on **this same
SHA**, so they are *not* stale with respect to the baseline. The one prior claim
that still cannot be validated at any revision is
`IMPLEMENTATION_REPORT.md` §5's `logs/bench_panel_burst.py` figures: that script
is absent from the repository (re-checked), so those numbers remain **not
independently verified**.

## 9.2 The click → visible-content path, stage by stage (A)

Every panel goes through the same eleven stages below. A panel is an **inline message**
sent by the owner's own account through the Helper Bot
(`inline_engine.trigger` → `result.click(chat_id)`, `inline_engine.py:241,251`),
so its buttons deliver a callback query to the **Helper Bot**
(`register_callback_handlers(helper, self.owner_id)`, `supervisor.py:433`).

| # | Stage | Code (this revision) | Before/after turn release | Awaits I/O | Measured by |
|---|---|---|---|---|---|
| 1 | Update delivery: device → Telegram → Helper Bot → `_callback_router` | Telethon dispatch → `panels.py:394` | before everything | network (outside the app) | **nothing** — `received_at` starts inside `admit()`, i.e. *after* delivery |
| 2 | Coordinate resolve, owner gate, settings flags | `panels.py:399-424`, `guard.is_owner` | before | none (memory) | inside `ack_ms` |
| 3 | **Acknowledgement** | `callback_gate.ack` (`:365`) → `await event.answer()` (`:375`) → `SetBotCallbackAnswerRequest` | **before the turn**, before any handler | **Telegram RPC, awaited, no timeout** | `ack_ms` (receipt→answer), `ack_rpc_ms` (the RPC) |
| 4 | Session/panel resolution | `panels.py:310` `_resolve_session` | before | none (memory maps) | inside `ack_ms`/`wait_ms` |
| 5 | Per-panel FIFO turn | `panels.py:491`, `callback_gate.acquire_turn:420` | — | none for an idle panel | `wait_ms` |
| 6 | **Handler**: state change + service/DB work | `panels.py:523` (`_handle_panel`) / `:527` (`_handle_action`) | **inside** the turn | database, via `run_sync_db` (bounded 4-worker pool) | `work_ms`, `db`, `db_ms`, `state_ms` |
| 7 | Turn released, then view built | `panels.py:540-541` then `:545` → `_finalize_panel:223` → `render_edit` (font transform + buttons) | turn already released | CPU only | inside `paint_ms` (before the RPC) |
| 8 | Painter publishes; one writer per message | `panels.py:368` `_present` → `callback_gate.PanelPainter` (`:263`, `drain:299`) | after release | none | `coalesced` |
| 9 | **The edit RPC** | `panels.py:50` `_safe_edit` → `await event.edit(...)` (`:60`) → Helper Bot `edit_message(InputBotInlineMessageID)` → `EditInlineBotMessageRequest` | after release, still awaited by this click | **Telegram RPC, awaited** | `edit_ms` (inside `paint_ms`) |
| 10 | Record closed | `panels.py:553` `mark_rendered()`, `callback_gate.finish` | — | none | `total_ms` ends here |
| 11 | **Server → owner's device** (the update propagating to the phone/desktop that is showing the panel) | not in this process | — | network | **nothing. No code in this repository can observe it** |

Two consequences follow directly from the table and are worth stating plainly:

* `total_ms` **is** the click→"edit RPC returned" interval. It is the closest
  available proxy for content-update latency, and it stops one stage short:
  the propagation of the edit to the owner's device (stage 11) is outside the
  process and is **not instrumented anywhere**.
* Between stage 3 and stage 9 the click performs **two serialized Telegram round
  trips** (`SetBotCallbackAnswer`, then `EditInlineBotMessage`) plus 0–2 database
  round trips. Nothing else on the path waits on anything.

## 9.3 Representative panels across subsystems (B, measured)

`/tmp/venv/bin/python /tmp/probe/panel_latency_probe.py` drives the **production
router** (`panels._callback_router`) with the **production registrars** of eleven
subsystems, a fake Telegram event that answers/edits instantly, and the real
in-memory DB fallback (so `run_sync_db` is genuinely dispatched). Numbers are
therefore the click path's **own in-process cost** and its **call counts** — not
production latency. Workload: one click on each panel, 12 seeded saved items,
settings pre-loaded.

| Panel | Handler → delegated service | DB dispatches | Edits | `work_ms` | `total_ms` |
|---|---|---|---|---|---|
| `menu` | `_menu_panel_handler` (pure, no service) | 0 | 1 | 0.09 | 0.55 |
| `settings` / `general` / `font` | misc handlers, settings cache only | 0 | 1 | 0.04–0.13 | 0.17–0.32 |
| `list` | `discover_service.do_list` → `list_saves` (**2 HTTP requests**) | 1 | 1 | 0.71 | 0.82 |
| `find` | `_find_panel_handler` (no query typed) | 0 | 1 | 0.01 | 0.08 |
| `db` | `_db_panel_handler` | 0 | 1 | 0.02 | 0.10 |
| `profile` | `_profile_panel_handler` | 0 | 1 | 0.01 | 0.08 |
| `bio` | `_bio_panel_handler` (engine state in memory) | 0 | 1 | 0.04 | 0.16 |
| `ai` | `_ai_main_panel_handler` (config cache) | 0 | 1 | 2.68 | 2.82 |
| `retrieve` | `_retrieve_panel_handler` → `count_saves` (1 request) | 1 | 1 | 0.21 | 0.32 |
| `retrieve_saved` | `_retrieve_saved_panel_handler` → `_render_saved_browser` → `list_saves` (**2 requests**) | 1 | 1 | 0.23 | 0.37 |
| `retrieve_item:S001` | `_retrieve_item_panel_handler` (snapshot hit) | 0 | 1 | 0.01 | 0.08 |
| `taskloom` | `_taskloom_panel` (registry + schedule render) | 0 | 1 | 10.98 | 11.09 |
| `todo` | `_todo_list_panel` | 0 | 1 | 0.05 | 0.17 |
| `pm` | `_panel_result` → `private_message_service` state | 1 | 1 | 0.29 | 0.60 |
| `del` / `context` / `health` | delete / context / health handlers | 0 | 1 | ≤ 0.3 | 0.07–0.30 |

Run-to-run wobble on this host: `taskloom` 10.85–10.98 ms, `ai` 2.48–2.68 ms,
`del` 0.02–0.22 ms. The call counts (`db`, `edits`) and `outcome` were identical
across runs; only the CPU figures move.

**The shape of the result is the finding: 15 of 19 panels do not touch the
database at all, every panel performs exactly one Telegram edit, and the click
path's own work is sub-millisecond for all but `taskloom` (11 ms of CPU) and
`ai` (2.7 ms).** Database reads are real but confined to four read-only
panels; they cannot be the explanation for latency "across all panels".

Turn admission and painting add nothing measurable for a single click:
`wait_ms` was 0.01–0.25 ms on every panel and `coalesced` was 0 — the painter's
coalescing only engages behind an in-flight edit, which a lone click never has.

## 9.4 Is each stage measurable? (A/B/D)

| Stage | Instrument | Can it be attributed today? |
|---|---|---|
| device → Helper Bot delivery (stage 1) | `health.set_last_callback()` only sets a timestamp | **No** — no duration, no baseline |
| acknowledgement (3) | `ack_ms`, `ack_rpc_ms` in the `[CALLBACK] timing` line | **Yes** |
| turn wait (5) | `wait_ms` | **Yes** |
| handler + DB (6) | `work_ms`, `db`, `db_ms`, `state_ms` | **Yes**, with the §8.8 caveats (dispatches not requests; sync settings path invisible; queueing not split from transport) |
| painter scheduling (8) | `coalesced`; `paint_ms` starts at `handled_at` | **Yes** (fastest; it is pure in-process) |
| edit RPC (9) | `edit_ms` inside `paint_ms` | **Yes** |
| RPC returned → content **visible** (11) | *nothing* | **No — instrumentation gap, by construction** |

So the existing line can already separate ack / work / paint. What it cannot see
is (a) how long the update took to reach the bot, and (b) what happens after the
edit RPC returns. Neither can be added inside this process without a second
observing client.

## 9.5 Ranked cause assessment

**1. The acknowledgement is a serialized Telegram round trip on the content
path — and the recent callback change is what put it there.** *Confidence in existence:
high (A + B). Confidence in magnitude: unknown (C absent).*

* (A) `await callback_gate.ack(event, admission)` (`panels.py:426`) runs before
  the session lookup, the turn and the handler; `ack` awaits `event.answer()`
  (`callback_gate.py:375`) with **no timeout**, so a slow answer RPC delays all
  later stages for as long as it lasts.
* (A) Before `5b34a3a` the order was the opposite: the handler ran (and rendered)
  inside the dispatch block, and `_safe_answer(event)` ran in the `finally`
  *after* it — verified with `git show 5b34a3a^:backend/helper/panels.py`. That
  commit therefore **added one full `SetBotCallbackAnswer` round trip in front of
  every panel's content update** while making the spinner immediate.
* (B) It propagates 1:1: on `menu` (0 DB dispatches) a 120 ms answer delay moved
  `total_ms` 250.7 → 371.3 ms with `work_ms` unchanged (0.1 ms). Timeline of the
  instrumented click: `answer:start@0 → answer:end@100 → edit:start@101 →
  edit:end@351`.

**2. Telegram-side cost of the edit itself, including silent flood sleeps.
*Existence: high (A). Magnitude: unknown (C absent).***

* (A) The edit is one `EditInlineBotMessageRequest` on the Helper Bot
  (`telethon/client/messages.py:1228-1229`). Its duration is `paint_ms`. The
  server→device propagation after it returns is stage 11 and is unmeasured.
* (A) Both clients are built with `flood_sleep_threshold=60`
  (`bot/client.py:44`, `helper/client.py:70`). Telethon then **sleeps silently
  inside the RPC** for any flood wait ≤ 60 s (`telethon/client/users.py:121-125`,
  logged at INFO as `Sleeping for Ns (…) on <Request> flood wait`). A flood sleep
  is therefore charged to `paint_ms` (or `ack_ms`) with no error and no outcome
  change — the click still reports `completed`.
* (A) Flood state is keyed by **request type**
  (`self._flood_waited_requests[r.CONSTRUCTOR_ID]`, `users.py:47-57`), so one
  flooded `EditInlineBotMessageRequest` makes the **next edit on every panel**
  sleep until it expires. This is the only mechanism found that can delay all
  panels at once from a single event.
* (D) Whether Telegram actually rate-limits this bot's edits in the owner's usage
  is unknown here; it is a hypothesis consistent with the symptom, not a finding.

**3. Synchronous database work blocks the event loop, which delays every panel's
paint. *Existence: high (A+B). Frequency: unknown.***

* (A) `settings_service.set_setting` (`:201`) calls `repo.update_field` (`:215`,
  a synchronous HTTP request) and then `_refresh_from_db()` (`:216` → `repo.load`,
  `:41`, a second synchronous request) **on the event loop**. Panel toggles call
  it directly (`misc.py:392,398`).
* (B) With the real HTTP/1.1 transport against a local server: the call blocks
  the loop for its whole duration — `set_setting` 27.3–34.5 ms wall, **2
  requests**, **max loop gap 28.4–35.6 ms** across two runs (the spread is local
  HTTP scheduling, not a code quantity). During such a gap no panel can start a
  paint, and the ack/answer RPC of a click arriving in it is delayed too. In
  production the gap is two Supabase round trips. This is a *stall*, not a
  constant delay.

**4. Database reads on the four read-only panels.** *Existence: high (A+B).*
`list` and `retrieve_saved` pay `list_saves` (**2 HTTP requests** each, §8.2);
`retrieve` pays `count_saves`; `pm` reads its service state. Real, already
measured in §8, and **not a whole-surface cause**: 15/19 panels read nothing.

**5. Application CPU.** *Existence: measured (B).* ≤ 11 ms (`taskloom`), and
sub-millisecond on 17 panels. Not a candidate.

**6. Painter / per-panel turn.** *Ruled out for a lone click (B).* `wait_ms` ≈ 0,
one edit per click, `coalesced = 0`. The painter removes *burst* cost; it cannot
remove a single click's edit RTT, and it never delayed one in measurement.

**7. "No update at all" masquerading as latency.** *Source-confirmed, not
observed.* `_safe_edit` returns early when the rendered text+keyboard equal the
last render (`panels.py:55`), so a click whose view is unchanged performs **zero**
edits. A panel that looks unresponsive after a click may simply have produced an
identical view (e.g. a refresh button over unchanged state) — a different defect
from latency, and it would read as "nothing happened", not "slow".

## 9.6 Counter-evidence and alternative explanations

* **The ack cannot be blamed on magnitude alone.** If the owner's logs show
  `ack_ms` near 0 and `paint_ms` dominating, candidate 1 contributes nothing and
  the residual is stage 9–11 (Telegram), which no application change can remove.
  Nothing in this pass distinguishes those cases — that is exactly what §9.7's
  measurement plan is for.
* **A `paint_ms` of tens of seconds** would identify a flood sleep (candidate 2)
  rather than latency; that is distinguishable from the same log line and should
  be checked before any code change.
* **The fallback edit path is not the common case.** If the Helper Bot's inline
  edit were refused, `_safe_edit` would still edit through `event.edit` (the same
  RPC), and `ExistingPanelEditor`'s two-step fallback is used by background Save
  flows, not by clicks. It is not a hidden second round trip on the click path.
* **Delivery of the update to the bot (stage 1) is invisible to this repository.**
  If that leg is slow (connection quality, DC routing, update queueing after a
  reconnect), the whole surface looks slow while every in-app metric stays small.
  No evidence either way was available offline.
* **Client-side redraw** (the edit carries the full text plus the whole styled
  keyboard on every click) could add perceived time on the owner's device. This
  cannot be measured here and is listed as an unknown, not a cause.
* **A slow database on the owner's real Supabase** would not explain "all
  panels", because 15 of them never query it (measured). That is the strongest
  piece of counter-evidence against the previous phase's recommendation being the
  answer to *this* symptom.

## 9.7 The smallest justified next implementation step

**Recommended: take the acknowledgement off the content path — issue it first,
but stop making the handler wait for it to return.**

This is the only stage in the whole path that is (i) on the critical path for
*every* panel, (ii) a full Telegram round trip, and (iii) not required for the
content update to be correct. It is also the one the recent change introduced
(§9.5-1), which matches the reported "no noticeable improvement": the previous
work improved the spinner and bursts, while adding one RTT to what the owner is
looking at.

Scope and constraints (analysis only — **not implemented**):

* `callback_gate.ack` would send the answer and hand the awaiting to a supervised
  background task, so dispatch proceeds immediately. It is still *sent* before any
  session lookup, database read, render or edit; it is no longer *completed*
  before them. AGENTS.md §5's admission contract says "answered before any
  session lookup…", so that sentence must be restated to "issued before" in the
  same change — otherwise the documented contract and the code disagree.
* Same-panel FIFO, exactly-once side effects, owner/freshness validation and the
  one-writer-per-message rule are untouched: nothing about ordering of *work*
  changes, only the order in which the click waits for a courtesy RPC.
* `ack_rpc_ms` keeps its meaning; `ack_ms` would need either a bound
  (`wait_for`) or an explicit "0 = not awaited" convention, and the timing line
  should say which.
* Risk: a click whose answer fails is no longer reported on that click's record
  synchronously; the failure must be logged where the task runs. Risk: a burst now
  issues answers concurrently with handlers, so the ack stream must be
  fire-and-forget without unbounded growth (one task per click, dropped by the
  existing duplicate gate).
* Expected benefit: exactly **one round trip removed from every panel's
  content-update interval** (measured effect of that RTT: +120 ms on a 250 ms
  click, §9.5-1). It does **not** shorten the edit RTT, stage 11, or the DB read
  on the four read-only panels.

**If the contract is not to be touched**, the conservative variant is the same
change with the await kept but bounded (`wait_for(event.answer(), ~0.5 s)`): it
removes the *unbounded* stall but not the normal RTT.

**Runner-up (different symptom):** move the settings write path off the loop
(§9.5-3). It removes a real all-panel stall, but only while a toggle is in
flight; it is a bigger edit (≈20 synchronous call sites) and does not explain a
constant delay.

**Explicitly not recommended:** another database optimisation. §8.6's
page+count merge remains a correct, low-risk improvement for Stash pages, but
§9.3 shows 15 of 19 panels read nothing, so it cannot be the answer to "all
panels".

## 9.8 Minimal measurement plan that separates the stages (no production access)

1. **Zero code, first.** From the existing `[CALLBACK] timing` lines, take ~50
   clicks per panel and compare `ack_ms`, `work_ms`, `paint_ms` within `total_ms`.
   This alone decides whether candidate 1 (large `ack_ms`), candidate 4 (large
   `work_ms`, only on the read-only panels) or stage 9–11 (large `paint_ms`) is
   responsible. Predicted shapes: DB → `work_ms` high, `db ≥ 1`; ack → `ack_ms`
   ≈ one RTT on *every* panel including `menu`; flood → `paint_ms` in whole
   seconds; delivery → `total_ms` small while the owner still sees lag.
2. **Flood check, zero code.** Search the process log for
   `Sleeping for` / `flood wait` (logger `telethon.client.users`, INFO). Any hit
   on `EditInlineBotMessageRequest` explains blocky panel lag and is invisible in
   the app's own logs today.
3. **Offline A/B in the existing harness** (`/tmp/probe/panel_latency_probe.py`,
   §9.3 shape): same click with the answer awaited vs issued-and-not-awaited,
   both with one chosen edit delay. This quantifies exactly what step §9.7 buys
   under that model — and nothing more; it is not a production claim.
4. **The one measurement the repository cannot make.** Stage 11 (RPC returned →
   content visible) needs an observer *outside* the editing client: a second
   authorized account/client sitting in the same chat, timestamping the incoming
   `MessageEdited` update against the edit RPC's return. That requires a live
   session and is out of scope for this pass; until it exists, stage 11 stays an
   explicit unknown rather than an assumed cause.
5. **Acceptance criteria for §9.7 when it is implemented** (not now):
   * per-click `total_ms` on `menu` (0 DB) drops by ≈ one answer RTT in the
     offline A/B, with edits unchanged at 1 and `db` unchanged at 0;
   * `answer` is still called exactly once per accepted click and zero times for
     a duplicate `query_id` (extend `test_timing_record_reports_edits_and_database_round_trips`
     with an ordering assertion: answer *issued* before the first handler await);
   * per-panel FIFO, panel-closed drop, owner isolation and burst receipts stay
     green (`test_callback_latency_burst.py`, `test_callback_backlog.py`,
     `test_helper_callback_receipt.py`, `test_retrieve_owner_isolation.py` — 147
     passed on this revision in this pass);
   * a failing/slow answer cannot delay dispatch (new test with a blocked
     `event.answer`).

## 9.9 What remains unverified (D)

* Any production magnitude: `ack_ms`, `work_ms`, `paint_ms`, request latency,
  update-delivery delay, and stage 11. **No production evidence was available for
  this pass**, so the ranking in §9.5 is by *structural* position on the path, not
  by measured seconds.
* Whether Telegram floods this bot's panel edits at all.
* Whether the owner's perceived delay is measured from the click or from their
  own client's animation, and which client they used.
* The `logs/bench_panel_burst.py` figures in `IMPLEMENTATION_REPORT.md` §5 (script
  absent).
* Whether `SetBotCallbackAnswer` and `EditInlineBotMessage` have materially
  different RTTs in production (they share the Helper Bot's connection, but the
  server-side handlers differ).

**Commands run for this section**

* `git rev-parse HEAD|--abbrev-ref HEAD`, `git fetch origin`, `git rev-parse FETCH_HEAD`, `git status --porcelain`, `git diff --numstat` — state above.
* `git show 5b34a3a^:backend/helper/panels.py` — pre-change ordering (`_safe_answer` in the `finally` after dispatch).
* `/tmp/venv/bin/python /tmp/probe/panel_latency_probe.py` — §9.3/§9.4 tables, the ack-propagation check, the `list_saves` dispatch count and the loop-block measurement; run twice, with the call counts, outcomes and the ack-propagation result identical both times (CPU/wall figures differ by the amounts noted in §9.3 and §9.5-3).
* `/tmp/venv/bin/python -m pytest -q tests/test_callback_backlog.py tests/test_callback_latency_burst.py tests/test_helper_callback_receipt.py tests/test_retrieve_owner_isolation.py tests/test_retrieve_multi_select.py tests/test_48_font_panel.py tests/test_private_message_panel.py` → **147 passed** (4.33 s).
* Read-only inspection of the installed Telethon 1.43.0
  (`events/callbackquery.py`, `client/users.py`, `client/messages.py`) for the
  exact RPCs `event.answer()` / `event.edit()` issue and the flood-sleep path.

No application file, test, migration or architecture document was modified; no
commit, push, branch change, deployment or live RPC was performed.
