# Implementation Report — Dead Code + Dead Configuration + Stale Runtime Residue Cleanup

> Execution date: 2026-08-21
> Scope: verified-dead setting/symbol removal after the removed supervisor
> watchdog. No architecture changes, no schema changes, no behavior changes
> beyond removing dead surfaces.

## 1. Execution Summary

Completed a full lifecycle trace of `update_stale_seconds` (confirmed dead:
its only runtime consumer was the removed supervisor watchdog's update-staleness
check) and removed it from every layer that existed solely for it. A systematic
zero-reference sweep over `backend/health.py`, `backend/runtime/*`,
`backend/diagnostics.py`, and `backend/observability/*` identified nine further
verified-dead symbols/residue chains; all were traced (writers, readers,
imports, tests, docs, dynamic use) before removal. One test asserted the
removed settings-panel button count and was updated to match the intentional
removal. Net diff: **9 files, +7 / −258 lines.**

## 2. Files Deleted

None. No file was deleted in this execution.

## 3. Files Modified

| Path | Change |
|---|---|
| `backend/services/settings_service.py` | Removed `update_stale_seconds` default, validator, typed getter/setter, docstring row; header count corrected to 11 |
| `backend/bot/handlers/misc.py` | Removed "Update stale" display line, "Set Update Stale" button, `_settings_update_stale_handler`, input registration |
| `backend/health.py` | Removed `_last_handler_dispatched` state, `set_last_handler_dispatched`, `get_last_handler_dispatched`, snapshot key `last_handler_dispatched_s`, unused singular `get_loop_progress`, unused `get_last_update`, write-only `set_task_states`; docstring wording "managed task states" → "supervisor task states" |
| `backend/runtime/supervisor.py` | Removed unread `client_alive` property (`_client_alive` state kept — actively read by keepalive) |
| `backend/runtime/diagnostics.py` | Removed dead `_get_full_stack` helper and the never-firing `last_handler_dispatched_s` diagnostics line |
| `backend/runtime/tracer.py` | Removed unused `now_iso` helper and its now-orphaned `datetime`/`timezone` import |
| `backend/diagnostics.py` | Removed dead report-builder block: `build_diagnostic_report`, all ten `_collect_*` section helpers, and their exclusive helpers `_get_task_state` / `_get_coro_name` / `_get_await_location`; removed orphaned `asyncio`/`os`/`sys` imports; module docstring updated. Live event-ring API (`record_event`, `get_events`, `filter_events`, `format_events`, `split_message`, `_format_event`, `_format_duration`) untouched byte-for-byte |
| `tests/test_12_save_engine.py` | `test_settings_panel_renders`: expected button count 11 → 10 (the removed button) |
| `DATABASE_ARCHITECTURE.md` | Two factual updates caused by this task: removed the `update_stale_seconds` row from the *settings-service* table and corrected "all 12 settings" → "all 11" |

## 4. Dead Residue Removed (with proof)

| Symbol/config | Proof of death |
|---|---|
| `update_stale_seconds` (whole chain) | Defined/defaulted/validated in `settings_service.py`; displayed/edited only in `misc.py` Settings panel. Zero runtime readers (`rg` across `backend/`, `tests/`, `src/`): heartbeat/failsafe/supervisor never read it. Original consumer was the removed watchdog's update-staleness check. No test references. AST scan: zero refs beyond def sites. |
| `_last_handler_dispatched` chain (state + setter + getter + `last_handler_dispatched_s` snapshot key + diagnostics display line) | Writer `set_last_handler_dispatched` had zero callers → timestamp always 0 → snapshot value always `None` → display line could never fire. Residue of the removed watchdog's dispatch-stall detection; stall detection now lives in heartbeat via `last_telethon_event`. |
| `get_loop_progress(name)` (singular) | Zero references; the plural `get_all_loop_progress()` is the active API (crash_diagnostics, health_check, maintenance, runtime_status). |
| `get_last_update()` | Zero callers; the state `_last_update` and snapshot field stay (actively written by supervisor, displayed by runtime diagnostics). Heartbeat reads the separate `get_last_telethon_event()`. |
| `set_task_states(states)` (plural bulk writer) | Zero callers. Singular `set_task_state` IS active (supervisor writes `lifeos-recovery` states); `_task_states` dict and `task_states` snapshot field remain live. |
| `RuntimeSupervisor.client_alive` property | Zero readers anywhere; underlying `_client_alive` attribute is live (`keepalive.py` reads it directly). |
| `now_iso` (tracer.py) | Zero references; orphaned the module's only `datetime`/`timezone` use → import removed too. |
| `_get_full_stack` (runtime/diagnostics.py) | Zero references; sibling helpers remain used (lines 257–258). `traceback` import still needed elsewhere in the module. |
| `build_diagnostic_report` + 10 `_collect_*` helpers + `_get_task_state`/`_get_coro_name`/`_get_await_location` (backend/diagnostics.py) | `build_diagnostic_report` had zero references repo-wide (code, tests, docs); every `_collect_*` was referenced only by it; the three task helpers were referenced only by `_collect_event_loop_section`. Abandoned one-off superseded by the Context/Health Glass panels. |

## 5. Candidates Investigated but Preserved

| Candidate | Reason preserved |
|---|---|
| `get_stale_loops` | ACTIVE — called by `health_check.py:99`. (An earlier pass removed only heartbeat.py's unused *import*.) |
| `set_last_command` / `get_last_command`, `tick_loop`, `monotonic_seconds`, `set_rpc_latency`, `set_last_rpc` | All have live callers per reference scan. |
| `WATCHDOG_RECOVERY` trace tags + `supervisor.py` "self-healing watchdog" docstring phrase | Live trace-event names emitted by active recovery code; renaming would break Render log-search continuity. Not residue of the removed dormant loop. |
| `operation_watchdog.py` + all `guarded_await` imports | Active operation-level utility (db client, telegram_api, AI providers, helper). |
| `sql/saved_items.sql` "(forward + deep)" header | NOT misleading: the table's own `save_type` CHECK constraint still allows `'forward'` (historical rows), so the comment accurately describes the schema. Changing only the comment would create a comment/schema inconsistency; altering the constraint is a schema change (forbidden). Left unchanged. |
| `.sql.sql` migration filenames (`20260718143752_…`, `20260805075707_…`) | Applied migration history is authoritative; renames can break Supabase migration tracking. Cosmetic only → left as-is. |
| `DATABASE_ARCHITECTURE.md` lines 292/318/913/1093 mentioning `update_stale_seconds` | These document the DATABASE COLUMN and applied migration, which still exist — factually correct. Only the settings-service surface row became stale and was updated. |
| `tg_retry.py`, `startup_check.py` | Dormant but tested; removal requires a separate conscious decision (per INVESTIGATION.md). |
| Protected docs (`AGENTS.md`, `AI_MASTER_DESIGN.md`, `OBSERVABILITY.md`, `PRODUCTION_CHECKLIST.md`, `PRODUCTION_VERIFICATION.md`, `FREEBUFF_PRE_PUSH_VERIFY.md`, `INVESTIGATION.md`) | Untouched — verified via `git diff --name-only`. |

## 6. Documentation Changes

- `DATABASE_ARCHITECTURE.md`: two-line factual correction only (see §3).
- No other documentation referenced any removed symbol (verified by repo-wide
  search; `OBSERVABILITY.md` documents `task_states`, which remains live).
- README.md intentionally untouched; remains a concise entry point.
- Historical knowledge in `INVESTIGATION.md` untouched.

## 7. Tests and Validation

1. Baseline established first: clean tree at `3bf5356`, known baseline
   **571 passed / 1 failed** (`test_31_delete_rpc_failures.py::test_tehran_local_cutoff_is_converted_against_message_timezone`,
   previously reproduced at clean HEAD `e752dfc` in a temporary worktree).
2. Full lifecycle traces for every candidate (definition → persistence →
   UI → consumer → tests → docs) using `rg` over `backend/`, `tests/`,
   `src/`, `*.md`.
3. AST-based zero-reference sweep over `backend/runtime/`,
   `backend/observability/`, `backend/health.py`, `backend/diagnostics.py`;
   every flagged symbol manually re-verified with targeted greps (including
   string/dynamic-use patterns) before removal.
4. `py_compile` on all seven modified Python files → OK.
5. Post-removal residual-symbol search for every removed name → only
   legitimate hits remain (DB-column docs; runtime/diagnostics' own active
   `_get_coro_name`/`_get_await_location`).
6. Full test suite re-run.
7. Complete `git diff` inspection per file (this caught and reverted an
   over-broad rewrite of `backend/diagnostics.py` that had altered kept
   functions; the committed version removes ONLY the dead block).
8. Protected-document existence + modification check (all present, none
   modified except the two documented DATABASE_ARCHITECTURE.md lines).

## 8. Exact Test Results

Final full run: **571 passed, 1 failed, 1 warning** (~13.5 s).

- Focused run during fix: `test_12_save_engine.py` + `test_08_observability.py`
  → 44 passed.
- Intermediate failure observed and resolved:
  `test_12_save_engine.py::test_settings_panel_renders` (asserted the removed
  11th button). Updated 11 → 10 because the tested surface was intentionally
  removed — no assertion was weakened or deleted.

## 9. Baseline Comparison

Baseline: 571 passed / 1 failed (`test_31` Tehran cutoff, pre-existing,
proven at clean HEAD earlier this session). Final: identical failure set —
**same single pre-existing failure**, same pass count. No new failures; the
intermediate `test_12` failure was introduced by this task's intentional UI
removal and resolved by updating the count assertion.

## 10. Database / Schema Impact

None. No SQL, migration, or `backend/db/` file touched. The
`panel_settings.update_stale_seconds` column remains in the database
(harmless; `load_all()` iterates `_DEFAULTS`, so the stale column/value is
simply ignored). Documented schema rows referencing the column stay factual.

## 11. Runtime / Source-Code Impact

User-visible change limited to the Settings panel: the informational
"Update stale: Ns" line and the "Set Update Stale" button no longer render
(their sole purpose was the dead setting). All other behavior identical:
kept functions are byte-identical to HEAD; recovery, health, telemetry, and
diagnostics outputs are unchanged except the removal of a snapshot key that
was always `None`.

## 12. Remaining Known Cleanup Candidates

None new. Previously flagged items are resolved or consciously preserved:
`update_stale_seconds` → removed this pass; `sql/saved_items.sql` header →
preserved (matches schema, see §5); `.sql.sql` filenames → preserved (§5).

## 13. Commit Hash

Recorded below after commit (delivery summary).

## 14. Push Result / Remote Verification / Working Tree

Verified immediately after delivery and reported in the execution summary
below; the report file cannot contain its own final commit hash, so the
authoritative values are stated in the delivery summary accompanying this
push (local HEAD, origin/main, and `git status` output captured verbatim).
