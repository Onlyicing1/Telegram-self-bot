# INVESTIGATION

## INVESTIGATION METADATA

- Repository: LifeOS / Telegram Self-Bot
- Branch: `main`
- Current HEAD: `5ed901aa1e50d57d63e4173f72e5954ac2d81706`
- Investigation date: 2026-08-21
- Scope: Forensic control-flow and branch reachability audit
- Status: Investigation-only; no production source, tests, configuration, dependencies, SQL, migrations, or protected documents were modified.

## 1. EXECUTIVE SUMMARY

The control-flow and branch reachability audit was completed across runtime supervision, configuration-derived flags, fallback/error paths, state transitions, handlers, AI dispatch, service validation, and dormant utilities.

No behavior-neutral dead control-flow surface was proven. No category-A candidate was identified, so no source deletion or runtime change is justified.

## 2. SCOPE AUDITED

The audit covered:

- Constant and permanently inactive conditions
- Environment/configuration-derived feature flags
- Runtime fallback and error branches
- Returns, raises, breaks, continues, and post-validation paths
- Runtime FSM states and state transitions
- Helper enablement and optional-service behavior
- Profile auto-start flags and persisted active-state paths
- AI provider/tool enablement and fallback behavior
- Handler/router input contracts
- Delete/save validation and ownership paths
- Dormant tested startup/retry utilities and their configuration inputs

## 3. CONFIRMED FINDINGS

- No branch or state transition met the category-A standard of being proven unreachable across runtime inputs, tests, fixtures, deployment configuration, and documented contracts.
- No source files or symbols were removed.
- No production Python, TypeScript, tests, configuration, dependencies, SQL, migrations, or protected documentation were changed.
- `HELPER_BOT_ENABLED` remains a runtime-derived value from `BOT_TOKEN`; both enabled and disabled paths are meaningful because deployment may provide or omit the token.
- `BIO_UPDATE_ENABLED` and `USERNAME_UPDATE_ENABLED` remain independently configurable boot defaults, while persisted active state can also resume each engine.
- Supabase and AI fallback branches remain meaningful because availability, network, permissions, table presence, provider keys, and provider responses are runtime-dependent.
- Runtime recovery and failsafe branches remain meaningful because connection, helper, event, RPC, lock, and task states vary at runtime.
- The known Delete-service timezone failure remains pre-existing and outside this audit:
  `tests/test_31_delete_rpc_failures.py::test_tehran_local_cutoff_is_converted_against_message_timezone`.

## 4. CANDIDATES CLASSIFIED

### PROVEN DEAD

None. No category-A candidate was found.

### INTENTIONALLY DORMANT

- `backend/runtime/tg_retry.py` and `backend/runtime/startup_check.py`: previously documented and intentionally tested dormant utilities; their branches remain valid for tests and operational reconstruction.
- `GHOST_ROOM_ID`: remains an input to `startup_check.py`, so its warning path is not dead merely because startup checks are dormant in production.
- Crash-report and diagnostic fallback paths: retained as last-resort observability behavior and protected operational surfaces.

### POSSIBLY OBSOLETE

- FSM enum values such as `AUTHORIZING`, `DEGRADED`, `RECOVERING`, and `REBUILDING` are not all emitted by the current supervisor transition calls, but they are part of the documented runtime state vocabulary and recovery model. The audit did not prove that external diagnostics, tests, or reconstruction tooling cannot depend on them; they were preserved.
- Some compatibility/fallback branches in provider and Telegram wrappers are uncommon in tests, but their inputs are supplied by external APIs and runtime failures. They were not removable under the evidence standard.

### ACTIVE

- Runtime supervisor recovery, heartbeat invariants, keepalive probes, failsafe freeze detection, helper optionality, profile startup/resume branches, AI provider/tool validation, API route error handling, and outgoing-message ownership validation were confirmed as meaningful control-flow surfaces.

## 5. PRESERVED SURFACES

Protected architecture and operational documents, SQL/migrations, deployment configuration, operational API endpoints, handler/tool/panel registration, dormant tested utilities, crash diagnostics, provider fallback logic, database fallback logic, and delete ownership checks were preserved.

The absence of a simple static caller, a rare runtime condition, or a currently unset environment variable was not treated as proof of deadness.

## 6. UNKNOWN / NOT PROVEN

This audit did not prove that every externally supplied failure mode occurs in every deployment, nor that every documented FSM state is currently emitted during normal operation. It also did not prove that uncommon provider/API exception branches are unnecessary.

No suspicious branch met the stronger threshold of zero possible runtime inputs, zero test or fixture value, zero deployment/operational role, and zero reconstruction value.

## 7. RECOMMENDED NEXT STEP

No cleanup implementation is justified from this control-flow audit. Any future candidate should be investigated separately with direct evidence covering its controlling-value producers, normalization, callers, tests, fixtures, deployment semantics, and protected operational contracts.

## 8. VALIDATION

Performed during this execution:

- Baseline `git status`, local HEAD, `origin/main`, and recent-history check: clean and synchronized at `5ed901aa1e50d57d63e4173f72e5954ac2d81706`.
- Repository control-flow searches over backend conditions, flags, state transitions, fallback paths, and terminal statements.
- AST scan for literal constant conditions and top-level statements after terminal control flow; no category-A result.
- Runtime/configuration searches for flag producers and consumers, including Render and tests.
- `python3 -m compileall -q backend`: passed (`py_compile_ok`).
- No full test suite was run during this execution; the previously reported baseline remains **571 passed, 1 failed, 1 warning**, with the known Delete-service Tehran timezone failure above.
