# IMPLEMENTATION REPORT

## Request

Add a read-only outbound bandwidth usage page under the existing Settings panel:
`Menu → Settings → Bandwidth Usage`. The task was a UI integration for the
already-implemented governor; the governor itself was not redesigned or
replaced.

## What changed

### `backend/services/outbound_bandwidth_service.py`

The read API now distinguishes a real zero from unreadable durable state,
without changing limits, reservation logic or persistence:

- `get_outbound_usage()` gained `available` (bool), `detail` (why it is
  unavailable) and `state` (`normal` / `near_limit` / `exhausted` /
  `unavailable`). The byte fields and all previous aliases are retained.
- `available` is **False** when no database is configured, when the
  `panel_settings` row is missing/malformed, or when the read raises. The zero
  values remain in the payload for compatibility but must not be presented as
  usage when `available` is False.
- `available` is **True with zeros** only when the current-period state is
  genuinely readable (fresh/empty state, or a rolled-over period).
- `usage_state()` classifies with `NEAR_LIMIT_RATIO = 0.90`; the limits
  (`DAILY_LIMIT_BYTES = 150_000_000`, `MONTHLY_LIMIT_BYTES = 4_000_000_000`)
  are untouched.
- No change to `reserve_outbound_bytes`, `_read_row_state`, the JSONB shape or
  the repository.

### `backend/bot/handlers/misc.py`

- Settings panel: new **Bandwidth Usage** row → `panel:bandwidth`.
- Registered child panel `bandwidth` (`parent="settings"`, title
  “Bandwidth Usage”) plus the `bandwidth_refresh` action, through the existing
  registry.
- The page reads usage **only** through `get_outbound_usage()`; the handler
  never queries Supabase.
- Renders daily/monthly used, limits, remaining, the UTC periods and the state
  label; byte formatting is decimal (`0 B`, `1.5 KB`, `150.000 MB`, `4.000 GB`).
- On an unavailable read it renders “Usage unavailable” and the reason, shows no
  byte counts, and explicitly says an unreadable state is not a zero reading.
- **Refresh** calls the service again and returns the panel view for the same
  panel; the callback router paints it as an action result (`home_view=False`),
  i.e. an edit of the existing message, never a new panel.
- Navigation is the shared row: Back/Home/Close are injected by
  `_finalize_panel` for a submenu, and Back resolves to `settings` through the
  registry parent.

### Tests and documentation

- New `tests/test_bandwidth_panel.py` (26 tests).
- `tests/test_12_save_engine.py`: Settings row count 11 → 12 (the new row).
- `DATABASE_ARCHITECTURE.md`: migration inventory updated to **38 files / 16
  embedded parts**; the bandwidth migration is part 16 with the column and
  CHECK; §6 documents `outbound_bandwidth_state`; §22 records it as applied.
- Documentation count guards updated accordingly
  (`test_database_architecture_doc.py`, `test_database_setup_order.py`,
  `test_private_message_db_security.py`, `test_private_message_scope.py`).
- `ROADMAP.md` and this report replaced.

## Governor limits, coverage and limitations (preserved)

- **Limits:** daily 150,000,000 bytes; monthly 4,000,000,000 bytes; UTC
  calendar day/month.
- **Covered:** Gemini OCR and STT payloads, Telegram outbound media (including
  `send_voice`), Deep Save single-item and album re-uploads.
- **Not counted:** Telegram downloads (inbound), cleanup delete requests,
  text-only AI calls and other provider traffic.
- **Failure posture:** a reservation is persisted before the transfer; a
  persistence failure rejects instead of transmitting unaccounted bytes;
  reservations are never refunded.
- **State:** one JSONB column (`outbound_bandwidth_state`) on the singleton
  `panel_settings` row, accessed only through `panel_settings_repository`.
- **Category-level usage breakdown is not available.** The persisted state has
  only daily and monthly aggregates, so the UI shows none and does not imply
  per-category accounting.

## Migration status

`supabase/migrations/20261112000000_add_panel_settings_outbound_bandwidth.sql`
is **already applied to the connected Supabase project** (owner-confirmed).
This task did not execute SQL, create a migration or change the JSONB schema.
The previously stale “migration unapplied / manual SQL required” statements have
been corrected in the rewritten documents.

## Tests executed and results

- Focused suites (`test_bandwidth_panel`, `test_12_save_engine`,
  `test_database_architecture_doc`, `test_database_setup_order`,
  `test_private_message_db_security`, `test_colored_buttons`,
  `test_settings_model_key_contract`, `test_panel_update_delivery`,
  `test_callback_backlog`): **222 passed**.
- New panel suite: **26 passed** (Settings entry, registration, rendering,
  formatting, normal/near/exhausted, refresh same-panel behaviour, unavailable
  vs real zero, shared navigation).
- Full suite: **7,411 passed, 27 skipped, 1 failed**. The failure is
  `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
  (`assert 0 == 1`, “interval advancement exceeded bound”), which is unrelated
  to this change and pre-existing.
- `py_compile` on the changed modules/tests: passed.
- `git diff --check`: passed.

## Not live-verified or deferred

- No live Telegram/helper-bot rendering or interaction was performed; the
  tests are offline and source-level.
- The live `panel_settings.outbound_bandwidth_state` row was not read; the
  applied-migration status is the owner's confirmation.
- Render deployment behaviour and real media transfers were not exercised.
- No category-level breakdown was added, because the persisted state cannot
  support one honestly.
