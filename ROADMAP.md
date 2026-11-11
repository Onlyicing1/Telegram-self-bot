# ROADMAP

## Completed task: Outbound Bandwidth Usage panel under Settings

Status: **Implemented, tested and pushed.** This was a Glass UI integration only;
the governor, its limits and its persistence were not redesigned or replaced.

### Objective

Expose the existing outbound bandwidth governor's current usage at
`Menu → Settings → Bandwidth Usage`, so the owner can inspect it without
querying Supabase or reading logs.

### Delivered

- A **Bandwidth Usage** row on the existing Settings panel, pointing at the
  registered `bandwidth` child panel (`parent="settings"`).
- A read-only page that renders, from `outbound_bandwidth_service.get_outbound_usage()`:
  - daily outbound usage, the daily limit (`150,000,000` bytes) and remaining capacity;
  - monthly outbound usage, the monthly limit (`4,000,000,000` bytes) and remaining capacity;
  - the current daily and monthly accounting periods, explicitly marked UTC;
  - the current state: **normal**, **near limit** (≥ 90% of either budget) or
    **exhausted**.
- A **Refresh** action that re-reads the service and repaints the **same**
  panel message (action results paint through the existing callback renderer;
  no second panel is created).
- The shared Back / Home / Close navigation row, driven by the panel registry
  and the existing navigation handlers — no separate navigation mechanism.
- An explicit **unavailable** state when durable accounting cannot be read. The
  getter now distinguishes “real zero” from “unreadable”, and the page never
  presents a failed read as `0` usage.
- Decimal byte formatting (KB/MB/GB, three decimals for MB/GB) so values remain
  useful at both limits.

### Governor facts preserved (unchanged)

- Daily limit **150,000,000 bytes**, monthly limit **4,000,000,000 bytes**, both
  UTC calendar periods.
- The governor accounts only explicit payload paths that call
  `reserve_outbound_bytes` before transmission: Gemini OCR/STT payloads,
  Telegram outbound media (including `send_voice`) and Deep Save single/album
  re-uploads.
- Explicitly **not counted**: Telegram downloads (inbound), cleanup deletes,
  text-only AI calls and other provider traffic.
- Reservations are persisted before transfer and are never refunded; a
  persistence failure fails closed.
- State lives in `panel_settings.outbound_bandwidth_state` (JSONB on the
  singleton `('global')` row) and is reached only through
  `panel_settings_repository`.
- **No category-level breakdown is available.** The persisted state contains
  only daily and monthly aggregates, so the panel shows no per-category totals
  and does not imply that they exist.

### Migration status

`20261112000000_add_panel_settings_outbound_bandwidth.sql` is **applied to the
connected Supabase project** (owner-confirmed). This task did not execute,
create or modify any migration or SQL. Earlier documentation claiming the
migration was still unapplied has been corrected.

### Validation

Focused panel/migration suites: **222 passed**. Full suite: **7,411 passed,
27 skipped, 1 failed** — the failure is the unrelated, pre-existing
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`.
`py_compile` and `git diff --check` pass; details are in
`IMPLEMENTATION_REPORT.md`.

### Not verified live

- No live Telegram/helper-bot rendering or click-through was performed in this
  environment; the panel tests are offline.
- The live Supabase accounting row was not read; the applied-migration status
  is the owner's confirmation, not a fresh remote check.
- Render deployment behaviour and live media transfers remain unverified.

### Out of scope

Governor limits, the reservation algorithm, outbound accounting integrations,
ProviderManager, media pipelines, database columns/migrations and unrelated
settings are untouched. No per-category usage breakdown is planned or shown.
