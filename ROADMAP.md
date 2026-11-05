# Roadmap — Persistent Test Modules Results + Explicit Full Refresh

This roadmap covers ONLY the current feature. No historical roadmap is appended.

## Objective

Opening Test Modules must not re-run a ~615-model live diagnostic. Results are saved
durably, displayed as cached, and refreshed only on an explicit action.

```
OPEN TEST MODULES
       |
       v
LOAD PERSISTENT SNAPSHOT
       |
       +---- valid snapshot ----> DISPLAY CACHED RESULTS
       |                                |
       |                                v
       |                         OWNER PRESSES REFRESH ALL MODELS
       |                                |
       |                                v
       |                       FULL EXISTING DIAGNOSTIC
       |                                |
       |                                v
       |                       SAVE NEW COMPLETE SNAPSHOT
       |                                |
       |                                v
       |                       DISPLAY FRESH RESULTS
       |
       +---- no snapshot -----> INITIAL FULL DIAGNOSTIC
                                        |
                                        v
                               SAVE INITIAL SNAPSHOT
                                        |
                                        v
                               DISPLAY FRESH RESULTS
```

## Current implementation status

| Item | Status |
|---|---|
| Versioned snapshot build + validation (`backend/ai/test_snapshot.py`) | IMPLEMENTED + unit-TESTED |
| Durable storage in the existing `bot_settings` document store | IMPLEMENTED + unit-TESTED (fake store; real Supabase unverified) |
| `ok` / `missing` / `malformed` / `unsupported_version` / `unavailable` load states | IMPLEMENTED + unit-TESTED |
| Cached display with `(CACHED)` labels and last full-test timestamp | IMPLEMENTED + unit-TESTED |
| Explicit Refresh All Models (new `action:ai_test_refresh`) | IMPLEMENTED + unit-TESTED |
| First visit with no snapshot runs the initial diagnostic | IMPLEMENTED + unit-TESTED |
| Exact `(provider, model_id)` reconciliation, `NOT_TESTED`, historical split | IMPLEMENTED + unit-TESTED |
| Partial / cancelled / failed-write safety | IMPLEMENTED + unit-TESTED |
| Cached AVAILABLE excluded from `proven_usable_candidates()` | IMPLEMENTED + unit-TESTED |
| Live provider verification | **NOT DONE** — no provider was called |
| Live Telegram verification | **NOT DONE** — handler output checked in tests only |
| Credential-revision invalidation | **NOT IMPLEMENTED** (no revision mechanism exists) |

## Storage design

- One durable JSON document in the EXISTING `bot_settings` table, key
  `ai_test_snapshot:v1`, written through the generic
  `db_client.settings_document_write` / read through
  `db_client.settings_document_state`.
- No new table, no migration, no SQL, no `DATABASE_ARCHITECTURE.md` change.
- One atomic upsert: a failed write leaves the previous complete snapshot intact.
- Bounded: `MAX_RESULTS=1000`, `MAX_HISTORICAL=500`, `MAX_MODELS=4000`,
  `MAX_ERROR_CHARS=200`, `MAX_DOCUMENT_BYTES=512 KiB`, with deterministic trimming and
  a `counts.truncated` marker.
- No module-level cache, no temporary file, no ephemeral filesystem.

## Snapshot format (schema v1)

```
schema_version        1
created_at            ISO-8601 UTC (snapshot write)
last_completed_at     ISO-8601 UTC (this diagnostic run)
complete / partial    booleans (partial runs are never persisted)
summary               tester summary buckets incl. not_tested
providers[]           name, display_name, icon, status, has_key, validated,
                      default_model, capability_kind
models[]              id, name, provider, context_length, is_free
results[]             provider, model, display_name, icon, status, error (bounded,
                      redacted), latency_s, http_status, retry_after, error_type,
                      provider_code, finish_reason, is_free, tested_at
historical[]          results whose pair is not in the latest catalog
counts                results, catalog, historical, dropped, truncated
```

## Cache reconciliation and invalidation rules

1. Match only on the exact `(provider, model_id)` pair.
2. Catalog = discovery metadata ∪ tested pairs ∪ provider default models.
3. Catalog model without a result → `NOT_TESTED` (never usable, never failed).
4. Result not in the catalog → `historical`, shown apart from the current catalog.
5. Duplicate pairs collapse to the first record.
6. A complete refresh replaces the snapshot; partial / cancelled runs and failed
   writes never replace it.
7. Read failure is `unavailable` — never treated as "no snapshot".
8. Credential-revision invalidation is not implemented; cached views always carry
   their timestamp and are never presented as live verification.

## UI behaviour

- AI panel → **Test Modules** opens the saved snapshot (`action:ai_test_models`).
- Cached header: `Cached snapshot · last full test <ts> · N results · catalog C ·
  complete|partial`, with per-result `·cached` markers and `(CACHED)` statuses.
- **↻ Refresh All Models** (`action:ai_test_refresh`) runs the complete diagnostic; a
  second tap during a run shows "Already running" and starts nothing.
- Unreadable / malformed / unsupported snapshots show an honest notice and offer
  Refresh; they never start a run implicitly.
- Existing provider grouping, summary chips, usable-model grid, All Results and
  pagination are preserved; All Results is bounded (60 current + 20 historical rows).

## Tests and exact results

- Focused group (6 files): **134 passed**.
- New `tests/test_model_tester_snapshot.py`: **36 passed**.
- Full suite: **6825 passed, 27 skipped, 1 failed** (136.86 s). The failure is the
  pre-existing `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`,
  reproduced on the unchanged baseline.
- `py_compile` on changed modules: exit 0. `git diff --check`: clean.

## Known limitations

- Cached results are a historical diagnostic record, never live proof.
- An extreme catalog is truncated at the document bound (`counts.truncated` is stored
  but not yet rendered in the panel).
- Credential changes are not detected; the old snapshot is shown until Refresh.
- With no Supabase configured, `bot_settings` falls back to process memory and does
  not survive a restart (pre-existing platform behaviour).

## Remaining work

- Surface `counts.truncated` in the cached header when it is set.
- If the architecture later exposes a credential revision, invalidate affected
  provider results on change.
- Live verification of one real full refresh on the deployed bot (owner action).

## Live verification status

Not live-verified. No provider call, no Telegram client, no Supabase deploy was made
while implementing or testing this feature.
