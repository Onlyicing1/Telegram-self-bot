# IMPLEMENTATION_REPORT — the "All Results" button now opens the diagnostics

> Replaces every previous report in full. Scope: one dead Glass UI button in
> Test Modules, its regression tests, and the two documents. No other
> behaviour was changed.

| Item | Value |
|---|---|
| Stage | Fix: bound one message body by paging the All Results diagnostics; make a failing action visible instead of silent |
| Date | 2026-11-05 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`) |
| Base commit | `21f3df0d9e0a01ed31c10b3891eb280120726cf7` — `feat: expose provider-attributed failure diagnostics in Test Modules` |
| Root cause | The All Results body was unbounded; a real run's diagnostics exceed Telegram's 4096-character message limit, so the edit was rejected while the already-acknowledged click left nothing on screen (`INVESTIGATION.md` §2–3) |
| Files changed in production | `backend/bot/handlers/ai.py`, `backend/helper/panels.py` |
| Tests added | `tests/test_test_modules_details_callback.py` (13 tests) |
| Live Telegram / Render | **NOT used.** No session, no RPC, no deployment, no Render access |
| Not touched | model selection, availability classification, provider fallback, `_last_test_payload` contents, redaction/bounds of diagnostic fields, SQL/Supabase, `DATABASE_ARCHITECTURE.md` |
| Interpreter | CPython 3.12 venv, `backend/requirements.txt` (pinned Telethon 1.43.0) + `pytest 9.2.0` + `pytest-asyncio` |

## 1. Root cause (one sentence)

`_ai_test_details_action` returned the whole run's diagnostics as a single
Telegram message; Telegram refuses any text over 4096 characters, `_safe_edit`
swallowed the rejection, and the click had already been acknowledged — so the
tap completed with no visible response and no error.

## 2. Production changes

### 2.1 `backend/bot/handlers/ai.py` — page the diagnostics instead of overflowing

* `_TEST_DETAILS_BODY_BUDGET = 3500` — a body budget that leaves room for the
  title and the page footer inside Telegram's 4096-character cap.
* `_test_detail_blocks(results)` — the existing per-model rendering, extracted
  verbatim into self-contained blocks. A block carries its provider header
  whenever the provider changes, so paging can never split an entry or lose
  provider attribution. Every diagnostic field and every existing bound is
  unchanged: `_failure_diagnostic_lines` and `_diag_text` (160/120 characters)
  are called exactly as before; the model name is additionally clamped to 80
  characters so no single block can exceed the budget.
* `_detail_pages(blocks, budget)` — greedy packing into pages that fit one
  message.
* `_ai_test_details_action` — reads the page index from the **existing**
  `extra` argument (already parsed by the router from `action:<id>:<extra>`,
  defaulting to page 0), renders that page, appends
  `_Page N/M · full diagnostics, nothing omitted_` when there is more than one
  page, and adds a `Prev · page · Next` row through the same
  `InlinePanelBuilder`. Page buttons use the **already registered** action id
  (`action:ai_test_details:<page>`), so no new action, listener, update loop,
  scheduler or parallel implementation exists.
* The empty/expired-payload branch is unchanged and still answers with an
  explicit "No results yet. Tap **Run Tests** first." view.
* The handler stays pure presentation over `_last_test_payload`: it never
  re-runs the tests, never mutates the payload, and does no I/O.

Single-page runs (the common short payload, and every existing renderer test)
render exactly as before, with no extra buttons.

### 2.2 `backend/helper/panels.py` — a failing action is never a silent no-op

`_callback_router` already acknowledged the click before dispatch (`5b34a3a`),
so a handler exception previously ended as "acknowledged, nothing painted".
The exception branch now paints one bounded notice through the same panel
writer (`_present` → `_safe_edit`):

```
× This action failed (RuntimeError). Nothing was changed.
```

Only the exception **type** is shown — never its message, which can carry
provider internals or credentials. The notice cannot itself break the click:
its paint is wrapped, and the admission is finished either way.

## 3. Regression tests (`tests/test_test_modules_details_callback.py`)

They drive the **production router** (`register_callback_handlers`) with real
Telethon `CallbackQuery` events over fakes for the two Telegram RPCs; the fake
message raises above 4096 characters exactly like the API, so an unbounded body
produces zero edits and the test fails.

| Requirement | Test |
|---|---|
| 1. Rendered button carries the callback data | `test_the_results_view_renders_the_all_results_callback` |
| 2. Router recognises and dispatches `ai_test_details` | `test_a_real_callback_dispatches_to_the_details_handler` |
| 3. Registry resolves the correct handler | `test_the_action_registry_resolves_the_details_handler` |
| 4. Simulated callback invokes `_ai_test_details_action` | `test_a_real_callback_dispatches_to_the_details_handler` (spy delegates to the real handler) |
| 5. Acknowledged promptly | `test_the_click_is_acknowledged_before_the_panel_is_painted` (answer timestamp ≤ first edit timestamp) |
| 6. Diagnostics edited through the production path | `test_the_diagnostics_render_through_the_production_panel_path` (HTTP 404, INVALID_MODEL, NOT_FOUND, model_lookup, retry-after, provider message, buttons) |
| 7. Missing / expired payload stays visible | `test_a_missing_payload_explains_itself_through_the_router`, `test_an_expired_payload_without_results_explains_itself` |
| 8. Handler exceptions are not silent | `test_a_failing_action_shows_a_failure_instead_of_doing_nothing` (visible notice, type only, no exception text) |
| 9. Unauthorized clicks stay rejected | `test_unauthorized_callback_is_rejected_without_editing`, `test_a_tap_without_a_panel_session_is_dropped` (answered, never painted) |
| Root-cause guard | `test_a_full_run_pages_instead_of_failing_the_edit` (60 results: unpaged body proven > 4096, paged click produces exactly one edit ≤ 4096 with provider attribution) |
| Content completeness | `test_paging_covers_every_result_exactly_once` (every model on exactly one page, each page ≤ 4096) |
| Action id stays the same | `test_details_pages_stay_on_the_registered_action_id` |
| 10. Existing views unaffected | the existing Test Modules grid / pagination / model-selection / diagnostic suites, re-run unchanged |

## 4. Verification (exact commands and results)

```
/tmp/repoenv/bin/python -m pytest tests/test_test_modules_details_callback.py -q -p no:cacheprovider
13 passed

/tmp/repoenv/bin/python -m pytest tests/test_test_modules_grid.py tests/test_14_tool_honesty_glass.py \
  tests/test_13_model_selection.py tests/test_11_runtime_wiring.py tests/test_callback_backlog.py \
  tests/test_callback_latency_burst.py tests/test_save_reply_panel_edit.py -q -p no:cacheprovider
139 passed

/tmp/repoenv/bin/python -m pytest tests -q -p no:cacheprovider
1 failed, 6888 passed, 27 skipped in 136.79s

/tmp/repoenv/bin/python -m compileall -q backend
OK

git diff --check
OK (no whitespace errors)
```

The single failure is pre-existing and unrelated:
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`.
It was reproduced on a clean `git archive HEAD` (base `21f3df0`, no local
changes applied) with the same failure, so it is not caused by this change.

## 5. What is deliberately unchanged

* Every provider-attributed diagnostic field, its redaction and its bounds
  (HTTP status, provider code/status, failure origin, endpoint/operation,
  retry-after, reasons, bounded sanitized message).
* `_last_test_payload`: written only by `ai_test_progress`, read only here.
* Model selection, availability classification, provider fallback, the usable
  grid, its column/page geometry and its pager.
* No new action ids, callbacks, listeners, loops, schedulers, or a second All
  Results implementation. No credentials, keys, session data or private
  conversation content is logged or rendered.

## 6. Remaining limitations

* Not verified against live Telegram: no session was used. The 4096-character
  limit is simulated in the tests with the same rejection Telegram returns.
* The run that produces the payload was not executed live either; realistic
  payload shapes are constructed in the tests (60 results, every third
  failing, with the full diagnostic fields).
* On a very large run the diagnostics now span several taps (Prev/Next); this
  is the intended trade-off for keeping every field instead of truncating.
* The failure notice added in `_callback_router` is generic by design; it names
  the exception type only, so diagnosing a specific action failure still needs
  the server log.
