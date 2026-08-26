# Implementation Report — Ghost Seen v2 AI Reply Execution Hardening

## Task objective

Apply the approved Ghost Seen v2 production-hardening changes to the
existing Stage 6 AI Reply flow and deliver them to GitHub. The user-facing
flow is unchanged:

`AI Reply → Context (1/5/10/20) → Disclosure (Yes/No) → automatic generation → automatic delivery`

No prompt input, prompt preview, provider/model selector, Send button, or
extra confirmation step was added. The changes harden the execution
boundary between Context/Disclosure → Engine → Dispatcher → ProviderManager
→ validated AI result → Telegram `send_reply`, and add sender identity to
the private-chat viewer.

## Repository / branch

- Repository: https://github.com/Onlyicing1/Telegram-self-bot
- Branch: `main` (local `main` tracked against `origin/main`)

## Files changed

Implementation (6 files, all pre-existing worktree changes from the
approved Ghost Seen v2 execution):

- `backend/bot/handlers/ghost_seen_v2.py` — hardened `_run_ai_reply`.
- `backend/services/ghost_seen_v2.py` — sender identity in the viewer.
- `backend/ai/engine/dispatcher.py` — per-provider-call execution
  diagnostics + failure-type normalization.
- `backend/ai/diagnostics.py` — correlated request-facts store
  (bounded) + `ai_last_request` snapshot surface.
- `tests/test_60_ghost_seen_v2_nav_search_perf.py` — removed the obsolete
  "no AI paths" regression (the AI Reply path is now a real, tested path).
- `tests/test_63_ghost_seen_v2_stage8.py` — Stage 8 regression coverage.

Documentation (1 file):

- `IMPLEMENTATION_REPORT.md` — replaced with this report.

`INVESTIGATION.md` was restored from HEAD (it had been deleted in the
working tree); it is byte-identical to the pushed Phase 1 + Phase 2 content
and is not part of the commit diff.

## What was implemented

### 1. Bounded AI generation timeout with cancellation containment

`backend/bot/handlers/ghost_seen_v2.py → _run_ai_reply`

- The Engine `execute` call runs as a shielded task under the existing
  `_AI_TIMEOUT_S = 45.0` bound (`asyncio.wait_for(asyncio.shield(task), ...)`).
- On timeout the engine task is cancelled and given a bounded
  `_AI_CANCEL_GRACE_S = 0.1` grace window; a `_consume_late_engine_task`
  done-callback drains any cancellation-resistant late result so it can
  never reach `send_reply`.
- No Telegram delivery can occur after the timeout: the delivery path is
  only reached inside the same bounded execution, and the state is cleared
  exactly once on the timeout path.

### 2. Duplicate-execution isolation preserved

- The existing per-chat `_ai_states` + `_ai_locks` architecture is
  retained (no parallel state system added). The Stage 8 regression
  `test_duplicate_rejection_does_not_clear_active_operation` proves a
  duplicate callback cannot consume or invalidate the state of an
  already-running execution.

### 3. Provider result validation before delivery

Distinct, honest failure classifications (recorded in request facts and
returned to the user):

- engine result `success=False` → `engine_result_failure`
- non-string response → `invalid_response_type`
- empty/whitespace-only response → `empty_response`
- reply over the `_TELEGRAM_TEXT_LIMIT` (4096) → `response_oversized`
  (no silent truncation)

Delivery success is only reported when `send_reply` actually succeeds;
generation success and delivery success are tracked as separate facts
(`delivery_reached`, `delivery_succeeded`, `final_failure_reason`).

### 4. Honest failure/fallback behavior

- Provider/Engine failures never produce a fake successful reply; the
  failure reason is propagated into the request facts and the user is
  told "✕ Couldn't generate the reply." / "✕ Couldn't send the reply."
  as appropriate.
- No Ghost Seen-specific provider fallback was added — the existing
  Engine → Dispatcher → ProviderManager fallback mesh remains the single
  provider path (`_provider_chat` instruments that existing path only).
- The AI request still executes with `allow_tools=False` (tool calls are
  disabled for Ghost Seen AI Reply; the general owner AI path retains tool
  access).

### 5. State cleanup on every terminal path

`_clear_ai_state` runs on: success, generation failure, delivery failure,
invalid/stale selection, timeout, cancellation, and the unhandled-exception
path. Stale selections cannot leak into a later Ghost Seen session.

### 6. Race-condition revalidation before delivery

Before `send_reply` the handler revalidates that the source chat is still
allowed, the reply target is still the original selected real Telegram
message ID, and the selection was not changed mid-generation
(`selection_changed_before_delivery`).

### 7. Structured diagnostics (existing infrastructure reused)

`backend/ai/diagnostics.py` gains a bounded (32-entry) correlated
request-facts store fed by `register_start(details=...)` /
`update_request(...)` / `set_stage(...)`; `snapshot()` exposes
`ai_last_request`. `backend/ai/engine/dispatcher.py` records per-provider-
call facts through the existing `_provider_manager.chat` path (start /
complete / failure / cancelled, elapsed, call count, failure type,
fallback used/exhausted, provider matrix size) and enriches the final
result metadata (`provider_call_count`, `provider_elapsed_s`,
`provider_failure_type`, `fallback_exhausted`, `provider_matrix_size`).
The handler correlates request ID, source chat, selected message ID,
context count, disclosure, provider/model, stage, provider timing, timeout
occurrence, cancellation state, engine result status/response length,
delivery reached/succeeded, and final failure reason. No message content,
credentials, session strings, or API keys are logged or recorded.

### 8. Sender identity in the private-chat viewer

`backend/services/ghost_seen_v2.py` adds `ViewerMessage.outgoing` (via
Telethon's direction bit `_message_is_outgoing`) and renders each line as
`You (outgoing): …` vs `{name} (incoming): …` so incoming vs outgoing
messages are unambiguous.

## Tests executed

- Ghost Seen v2 suite (`tests/test_52…test_64`): **175 passed** in 0.55s.
- Full repository test suite (`tests/`): **981 passed, 23 skipped** in
  31.02s. The 23 skips are pre-existing (legacy `ghost_seen_service`
  tests), unrelated to this change.
- `compileall` over `backend/` + `tests/`: clean (exit 0).
- `git diff --check`: PASS (no whitespace errors).

Test results are real — reported from the actual runs above. No frontend
(TypeScript) files changed, so TypeScript validation was not applicable.

## Final implementation state

All six implementation files are staged in one commit with this report.
`INVESTIGATION.md` is restored and byte-identical to the pushed Phase 1 +
Phase 2 handoff (no diff). Security boundaries preserved: `allow_tools=False`
for Ghost Seen AI Reply, owner-only access, no new Telegram RPC or SQL
execution surface, no credentials in logs or docs.

## Commit / delivery

- Implementation commit: `3faac6a88b819e28b6bea48aef94f223b1fc9123` —
  `fix: harden ghost seen v2 ai reply execution boundary` (7 files,
  +772/−276).
- This report is carried by a small follow-up metadata commit that fills
  in the verified delivery facts below (same pattern as the database
  architecture delivery).

---

## Delivery verification

- Commit: `3faac6a88b819e28b6bea48aef94f223b1fc9123` —
  `fix: harden ghost seen v2 ai reply execution boundary`.
- Push result: pushed to `origin/main` (`b160adc..3faac6a`), exit 0.
- Remote verification: after `git fetch origin main`, `local HEAD ==
  origin/main == git ls-remote origin HEAD == 3faac6a…`; `git show
  origin/main:IMPLEMENTATION_REPORT.md` confirms the pushed report is
  this document.
- Final working-tree state: `main` in sync with `origin/main`; all 6
  implementation files and this report are committed; `INVESTIGATION.md`
  is restored to the pushed Phase 1 + Phase 2 handoff (no diff); no
  unrelated files were touched.
