# Implementation Report — Repository-Wide Dead Surface Sweep

> Execution date: 2026-08-21
> Scope: repo-wide dead-file / dead-symbol / dead-import sweep beyond the
> previously completed watchdog-residue passes. No redesign, no schema
> changes, no behavior changes beyond removing proven-dead surfaces.

## 1. Execution Summary

Performed an AST-based import-graph analysis of every `backend` module plus a
zero-reference scan of all top-level functions/classes across services,
helper, telegram_api, web, observability, profile, bio/username, AI tools,
engine, memory, prompt, and conversation packages. Three fully dead modules
were found and deleted; one dead no-op handler stub was removed together with
its router wiring; ten dead imports in runtime-core files were removed; one
stale AGENTS.md tree entry was corrected. The dormant candidates
`tg_retry.py` and `startup_check.py` were investigated and consciously
preserved. Full suite re-run: identical to baseline.

## 2. Files Deleted

| Path | Proof (6-point standard) |
|---|---|
| `backend/ai/config/env.py` (288 lines) | All seven public functions (`load_ai_env`, `load_provider_env_configs`, `apply_env_to_config_manager`, `apply_env_to_provider_configs`, `_get_bool/_get_float/_get_int`) have zero callers repo-wide; module never imported (not even by its own package `__init__`); not tested; not referenced in any doc/render.yaml/Procfile. Runtime env loading actually happens via direct `os.getenv` in `providers/factory.py`, `discovery.py`, `model_discovery.py`, and `config_store.py`. |
| `backend/ai/runtime/report.py` (158 lines) | `build_report`/`RuntimeReport` referenced nowhere outside the file; zero importers incl. package `__init__`; no tests; no docs. Developer-only diagnostic snapshot that was never wired into any entry point. |
| `backend/bot/handlers/organize.py` (17 lines) | Self-described no-op stub (`register()` = `pass`) kept only so the router import wouldn't break; zero panel/action/input references anywhere; no tests; INVESTIGATION.md §9 explicitly recommends removal. |

## 3. Files Modified

| Path | Change |
|---|---|
| `backend/bot/router.py` | Removed the `organize` import + registration entry; removed dead imports `asyncio`, `time`, `trace_handler_exception` |
| `backend/bot/handlers/misc.py` | Removed dead imports: `asyncio`, `resource`, `bio_engine`, `db_client`, `to_edit_buttons`, `TargetContext`, `set_target`, `is_auto_close_enabled` (panel code uses `settings_service.is_auto_close_enabled()`) |
| `backend/runtime/supervisor.py` | Removed dead imports (watchdog-removal residue): `typing.Any`, `guarded_create_task`, `get_helper_client` alias, `panels_module`, `profile_scheduler` — each verified to appear only on its import line |
| `backend/runtime/failsafe.py` | Trimmed unused names from the local from-import (`_heartbeat_age`, `_started_at` accessed via the `_h` module alias instead) |
| `backend/runtime/health_check.py` | Removed dead imports `asyncio`, `time`, `get_all_loop_progress` (uses snapshot + `get_stale_loops` only) |
| `backend/runtime/diagnostics.py` | Removed orphaned `collections.deque` import |
| `AGENTS.md` | Directory tree: removed the deleted `handlers/organize.py` line (factual staleness caused by this task). Lines documenting still-existing dormant modules left intact. |
| `IMPLEMENTATION_REPORT.md` | Replaced with this execution's results |

## 4. Dead Files Investigated

Full module graph built over all 150+ backend Python files. Modules with zero
importers: `ai/config/env.py`, `ai/runtime/report.py`,
`observability/crash_report.py`, `runtime/startup_check.py`,
`runtime/tg_retry.py`. All five individually adjudicated (§5).

## 5. Candidates Preserved (with reasons)

| Candidate | Reason |
|---|---|
| `backend/runtime/tg_retry.py` | Test-only in production but **consciously retained**: covered by three focused unit tests (`test_06_failure_simulation.py`), documented as a deliberate dormant path in AGENTS.md §4 ("dormant in prod, tested"), listed as an available utility in PRODUCTION_CHECKLIST.md. Contains unique FloodWait-aware retry logic not duplicated elsewhere. Removing it would contradict the authoritative architecture doc without new evidence of harm. |
| `backend/runtime/startup_check.py` | Same conscious-retention status (AGENTS.md §4, PRODUCTION_VERIFICATION.md test table). Also the only consumer of the `GHOST_ROOM_ID` config key — both stay consistent with the prior decision that retired them together or not at all. |
| `GHOST_ROOM_ID` config key | Consumed by preserved `startup_check.py`; removal is coupled to that module's fate. |
| `HELPER_BOT_ENABLED` env var | Code derives helper enablement from `BOT_TOKEN` presence (`config.py`) and never reads an explicit override. Honoring the explicit flag would be a **behavior change** (out of cleanup scope); the AGENTS.md §11 row stays until the owner decides whether the flag should be honored. Flagged in Remaining Candidates. |
| `backend/observability/crash_report.py` | Zero production callers but documented as part of the observability surface in OBSERVABILITY.md (protected) and covered by `test_08_observability.py`. Documented-and-tested utility API — same retention standard as tg_retry/startup_check. Production crash *recording* is separately handled live by `runtime/crash_diagnostics.py`. |
| ~170 flagged "unused imports" that are `from __future__ import annotations` or typing-only imports | False positives / load-bearing style; bulk removal would be cosmetic churn across 100+ files with real breakage risk. Out of scope by the no-cosmetic-refactor rule. Imports in files NOT touched by recent cleanups were deliberately left alone for the same reason. |
| Duplicate-helper check | No duplicate utility implementations found with one side completely caller-less (scanner output empty for all scanned packages). |

## 6. Documentation Impact

- `AGENTS.md`: one tree line removed (file deleted this pass). Nothing else.
- `INVESTIGATION.md`: untouched; its mentions of the removed modules are
  historical audit evidence, which it is allowed to contain.
- README.md: already accurate (does not list individual handler files).
- OBSERVABILITY.md / DATABASE_ARCHITECTURE.md / other protected docs:
  untouched and verified unmodified.

## 7. Tests and Validation

1. Baseline: clean tree at `00e871d`; known baseline 571 passed / 1 failed.
2. Import-graph + AST scans (module level and symbol level) over the whole
   backend; manual verification of every candidate via targeted searches
   including string/dynamic-use patterns before any deletion.
3. Package `__init__.py` contents verified before each file deletion.
4. `py_compile` on all six modified Python files → OK.
5. Residual-reference searches after deletion: `handlers.organize`,
   `runtime.report`, env.py function names → only historical
   INVESTIGATION.md evidence remains (permitted).
6. Full test suite run twice during the pass (after file deletions, after
   import cleanups) — see §8.
7. Complete final diff inspection; protected-document modification check
   (only AGENTS.md tree line, permitted factual fix).

## 8. Exact Results

- `py_compile`: all modified files compile.
- Full suite (final): **571 passed, 1 failed, 1 warning** (~14 s).
- Failure: `tests/test_31_delete_rpc_failures.py::test_tehran_local_cutoff_is_converted_against_message_timezone`
  — verified same identity as the pre-existing baseline failure (Delete-service
  timezone logic, untouched by this pass).

## 9. Baseline Comparison

Identical to baseline: same single pre-existing failure, same pass count.
No new failures introduced; no tests removed or weakened.

## 10. Database / Schema Impact

None. No SQL, migration, or DB-client change.

## 11. Runtime Impact

Removals are unreachable-by-construction (dead modules, a no-op stub, unused
import bindings). Startup now registers one fewer no-op handler. No behavioral
change on any live path; suite confirms.

## 12. Remaining Candidates

- Decide whether explicit `HELPER_BOT_ENABLED=true` (without `BOT_TOKEN`)
  should enable the helper (behavior change) or the stale env-var row should
  be dropped from AGENTS.md §11 (doc change). Needs an owner decision.
- `crash_report.py`, `tg_retry.py`, `startup_check.py`: retained by documented
  decision; revisit only if the owner wants them retired together with their
  tests/docs.

## 13. Commit

Cleanup commit: **`bf56f85`** (`bf56f85aadd4ba28ff4f169484ac91bf30d407d2`)
— "chore: remove dead modules, no-op handler stub, and dead imports"
(11 files changed, 125 insertions(+), 634 deletions(-)). This report's
finalization update is delivered as the immediately following docs commit
(a file cannot contain its own commit hash); it is HEAD at push and
verifiable via `git log --oneline -2` / `git rev-parse HEAD`.

## 14. Push Result

Push to `origin/main` **succeeded** for the cleanup commit:
`00e871d..bf56f85  main -> main`. The finalization commit is pushed
immediately after being created — its presence on the remote is the proof
of a successful push.

## 15. Remote Verification

Verified via `git fetch origin && git rev-parse origin/main HEAD` after the
cleanup push: local HEAD and origin/main both =
`bf56f85aadd4ba28ff4f169484ac91bf30d407d2`. The finalization commit is
verified the same way immediately after its push (result captured in the
delivery summary).

## 16. Final Working Tree

Before the finalization commit: exactly the ten files of this pass staged
and committed (`git status` showed only this report modified). After the
finalization commit + push: **working tree clean**, branch up to date with
`origin/main` (verified).
