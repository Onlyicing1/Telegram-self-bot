# IMPLEMENTATION REPORT — P1 History & Intelligence result-delivery hotfix

## 1. Scope and production evidence

The P1 History & Intelligence panel could finish a summary successfully and still
leave the owner on the Loading state. Deployed Render logs for request
`glass-history` (2026-11-13T14:08–14:09Z) show:

- retrieval and every provider call completed with non-empty text;
- the final panel edit failed with `The caption is too long
  (caused by EditInlineBotMessageRequest)`;
- the fallback self-client edit failed with `The action must be performed
  through an inline bot callback (caused by EditMessageRequest)`.

Both failures are consistent with the panel being a photo message owned by the
Helper Bot's inline result: Telegram bounds its text to a caption, and only the
inline bot can edit it.

## 2. Root cause confirmed in source

- `backend/services/appearance_service.py`: with Appearance preview active the
  panel is delivered as a PHOTO inline result whose text is its caption;
  `MAX_CAPTION_CHARS = 1024` and `caption_for()` refuses longer text. Text-only
  delivery is not caption-bounded.
- `backend/bot/handlers/history.py`: `MAX_SUMMARY_CHARS = 2800`, plus metadata
  lines, so a valid summary can exceed 1024. `_run_summary()` called
  `_paint()` for the final result and ignored its Boolean return;
  `_run_link()` delegated to the same function. The `finally` block released
  the operation generation regardless, so a refused edit completed the
  operation as if the result had been delivered.
- `backend/helper/panel_edit.py`: `ExistingPanelEditor.edit()` returns `False`
  (never raises) when both edit paths are refused. Its self-client fallback
  cannot edit an inline-bot-owned message, which is exactly what production
  logged.

Focus was **not** independently defective. The registration, pending-input
consumption, `_focus_input_handler`, `_state` snapshot and
`instruction=focus or None` pass-through were verified and are now covered by
regression tests; Focus runs were affected only by the shared delivery defect.

## 3. Implemented fix (smallest supported delivery path)

Only `backend/bot/handlers/history.py` changed behaviourally:

- `_deliver_result()` chooses the path from the ACTUAL recorded delivery state
  (`appearance_service.delivered_state`). A media panel whose rendered result
  exceeds the caption bound goes straight to the text replacement; a short
  result still edits in place.
- `_replace_with_text_result()` reuses the existing
  `PanelLifecycleManager.replace_panel` (create the new inline panel first,
  delete the old message only after it exists) with a non-empty query extra
  (`result:<token>`). A non-empty extra makes
  `appearance_service.apply_to_inline_results` skip media composition, so the
  new panel is a genuine TEXT result with its keyboard/navigation intact.
- Result views are carried in a bounded (`8`) one-shot `OrderedDict` keyed by a
  random token; the inline builder drains the token, so no growing state, no
  global "current result", and no replay.
- Text is deterministically bounded to Telegram's text-message limit
  (`MAX_RESULT_TEXT_CHARS = 4096`) with a visible truncation marker; it is NOT
  truncated to the 1024 caption bound.
- A replacement message is sent only when the panel chat is the owner's OWN
  private chat (Saved Messages, `panel_chat_id == owner id`). In any other chat
  no separate message is sent; the panel receives a short honest failure notice.
- A refused final edit is no longer silent: `_run_summary()` checks the delivery
  result, falls back to the text replacement, and otherwise attempts a short
  failure notice and logs `HISTORY_RESULT_DELIVERY_FAILED` with a stable reason
  (`destination_not_owner_private`, `replacement_failed`,
  `replacement_raised`, `no_safe_text_path`). Operation-generation cleanup still
  runs in `finally`.
- No second Telegram client, listener, scheduler, worker, executor, provider
  manager, persistent state or database object was added. `history_ai_service`,
  `history_service`, the identifier-free prompt mode and all existing guards are
  unchanged.

## 4. Files changed

- `backend/bot/handlers/history.py` — delivery selection, text replacement,
  bounded one-shot result views, owner-private destination guard, honest failure
  handling.
- `tests/test_history_panel.py` — 10 new regression tests (39 total in the
  file): long media result replacement, short in-place render, text-bound
  determinism, refused edit is an explicit failure with generation cleanup,
  no replacement outside the owner's own chat, caption limit not applied to the
  text path, Focus save/pass-through/clear/over-limit rejection, focused long
  summary delivery, and link summary delivery through the same path.
- `ROADMAP.md` — P1 result-delivery hotfix recorded in the History & Event
  Intelligence entry.
- `IMPLEMENTATION_REPORT.md` — this report.

No other file changed. No database, Supabase, schema, migration or deployment
configuration was touched.

## 5. Automated verification

- New/affected focused suite: `.venv/bin/python -m pytest
  tests/test_history_panel.py -q` → **39 passed**.
- Related panel/lifecycle/appearance/history selection:
  `pytest tests/ -k "panel or lifecycle or appearance or history"`
  → **705 passed, 3 skipped**.
- Full suite (`pytest tests/ -q --ignore=tests/test_runtime_session_terminal.py`,
  the untracked startup-hardening test from unrelated work that cannot collect
  on this base) → **7571 passed, 6 failed, 27 skipped** in 144.34 s.
- Untouched `origin/main` baseline (`96b93ea`, clean worktree) → **7560 passed,
  6 failed, 27 skipped** in 163.47 s.
- Baseline comparison: the SAME six failures exist on the untouched baseline and
  none is in a file changed here:
  1. `tests/test_aed_db_security.py::test_the_documentation_describes_the_restrictive_model`
  2. `tests/test_canonical_schema_reconciliation.py::test_the_migration_is_forward_only_and_its_successors_stay_additive`
  3. `tests/test_database_setup_order.py::test_the_section_does_not_pretend_the_sql_was_executed`
  4. `tests/test_database_setup_order.py::test_the_waiting_answer_migration_is_absorbed_by_part_one`
  5. `tests/test_private_message_db_security.py::test_the_rls_model_names_the_two_tables_as_server_only_exceptions`
  6. `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
  These are documentation-drift assertions and one pre-existing scheduler
  recovery failure, outside this hotfix's authorized scope.
- `python -m compileall -q backend tests` → exit 0.
- `git diff --check` → exit 0.

## 6. Live verification status

**No live Telegram verification was performed.** The fix is verified only by
offline tests with scripted Telegram fakes and real composition/replacement
code paths. It must not be described as operationally fixed until a real run
with Appearance preview active and a summary longer than 1024 characters shows
the text replacement on screen. No Render deploy was triggered.

## 7. Remaining limitations

- The text replacement is allowed only in the owner's own private chat
  (Saved Messages). A long summary started from a group or another chat gets a
  short honest failure notice instead of a private-content message there; this
  is deliberate.
- Telegram's exact behaviour for the replacement inline result cannot be proven
  offline (the test fakes the lifecycle call); only the actual Telegram client
  confirms the message swap.
- The caption-too-long detection relies on the recorded Appearance delivery
  state; when that state is absent (e.g. after a restart) the first edit is
  attempted and its refusal still falls back to the text replacement.

## 8. Delivery

- Repository: `Onlyicing1/Telegram-self-bot`.
- Branch: `main`.
- Hotfix commit: **`678fab5bfdebf1a41084d6293a24acd65fdbd332`**
  (`fix(history): deliver long summaries outside the photo-caption limit`).
- This report is committed as the immediate child of that hotfix commit and
  pushed in the same fast-forward push; `origin/main` is verified to equal the
  report commit after the push (recorded in the delivery response).
- Final working tree: clean except the pre-existing untracked
  `tests/test_runtime_session_terminal.py` from unrelated in-progress work,
  which was not staged or modified.
