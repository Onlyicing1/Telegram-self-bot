# Implementation Report — Manual “All my messages” Frozen (2026-11-09)

**Scope.** Only the manual Delete Glass UI path that offered to delete ALL of
the owner's messages in the current chat. Everything else is untouched.

**Status.** Implemented, tested, committed, pushed to `origin/main`.

## 1. What was frozen

The Delete home panel's **“All my messages”** button (`panel:del:all`, which led
to the preview/confirmation step `panel:del:allexec`) is no longer rendered,
and both legacy callbacks are now inert:

* `_home_buttons()` no longer adds the “All my messages” row. The other rows
  render exactly as before: Last 10 / 25 / 50 / 100 / 250 / 500, **Custom
  amount…**, **Delete from a message…**, **Delete by date range…**, plus the
  existing Back and confirmation navigation for the remaining options.
* `_del_view()` answers BOTH `all` and `allexec` with the same frozen view —
  title `Delete`, body “Deleting all of your messages is temporarily
  unavailable. Nothing was deleted.”, and a single **Back** button to
  `panel:del`. A stale panel or a manually replayed callback therefore cannot
  display a preview, cannot display a confirmation, cannot start the scan or
  the deletion, and performs no deletion of any kind.
* Nothing in the frozen path reads history, calls the deletion service, or
  invokes `preview_self_owned_count()`.

## 2. What remains, unchanged

* The **general Delete service** (`backend/services/delete_service.py`), its
  ownership-verification chokepoint (`delete_verified_self_messages`), and
  the All-capability (`do_del_self_filtered`, `select_self_owned_message_ids`,
  `preview_self_owned_count`) are byte-for-byte untouched.
* The **AI Delete tool** and its existing capabilities (`mode: "all"` included)
  are untouched. Its own timeout/partial-result behaviour never depended on
  the Glass panel and still passes its tests unchanged.
* Quick-count deletion (`panel:del:q:n` / `qexec`), custom-count input,
  delete-from-message (recent/preview/single/forward/reply), message-ID input,
  ID-forward, and date-range input behave exactly as before.
* Shared Telegram clients, the Helper Bot architecture, panel ownership
  (Helper Bot is still the only editor of the Delete panel), and all other UI
  infrastructure are untouched.

## 3. Freeze, not removal

The implementation is kept in place for possible future reactivation:

* `do_del_self_filtered` + its bounds/deadline/progress plumbing (service),
* the staged progress machinery — `_ProgressPainter`, `_progress_painter`,
  `_render_scan_progress` / `_render_delete_progress`, `_progress_message`,
  `_all_result_text` and the `_ALL_TITLE` / `_DELETING_TITLE` /
  `_COMPLETED_TITLE` / `_STOPPED_TITLE` titles (handler),
* `_ALL_PREVIEW_TIMEOUT` and `preview_self_owned_count()` (preview step).

Reactivation is limited to restoring the button in `_home_buttons()` and
routing `panel:del:all` / `panel:del:allexec` to implementing code again;
the whole backend + progress + rendering stack stays tested at the service
and painter level (see §4), so nothing else needs to be rebuilt.

## 4. Tests and validation

Frozen-callback coverage (new/updated):

| Test | Proves |
| --- | --- |
| `test_33…::test_frozen_all_callback_deletes_nothing_however_many_messages` | both callbacks delete nothing with 1,320 messages present; no `panel:del:allexec` button; Back offered |
| `test_33…::test_frozen_all_callbacks_never_invoke_the_deletion_service` | spies on `do_del_self_filtered`, `do_del_n_counts`, `preview_self_owned_count` record **zero** calls |
| `test_33…::test_frozen_all_callbacks_show_no_preview_or_confirmation` | no “Preview:”, no “Confirm”, no “Delete ALL” in the frozen body |
| `test_34…::test_frozen_all_renders_no_preview_and_no_confirmation` | same, plus the preview service is never even called |
| `test_34…::test_frozen_allexec_starts_no_operation` | replayed `allexec` deletes nothing, paints nothing |
| `test_delete_panel_ownership…::test_the_frozen_all_callbacks_edit_nothing` | neither callback edits the panel (Helper Bot or Self Client) |
| `test_33…::test_home_panel_offers_every_required_quick_action` (updated) | `panel:del:all` is absent from the home panel; every other button remains |
| `test_33…::test_quick_still_requires_confirmation_and_all_is_gone` (updated) | quick-count still requires confirmation; `all` shows no confirmation |
| `test_colored_buttons…::test_delete_submenu_is_primary` (updated) | frozen button absent; remaining buttons keep their styles |
| `test_33…::test_every_manual_path_reaches_the_ownership_chokepoint` (updated) | `allexec` removed from the path list; the other seven paths still reach the chokepoint exactly once |

Backend capability coverage kept (drives the implementation directly — the
frozen UI no longer reaches it):

* `tests/test_34_delete_all_execution.py` — the former handler tests now call
  the service through a `run_all()` helper that mirrors the old `allexec`
  handler (one progress painter → `do_del_self_filtered` →
  `_all_result_text`): total deadline, slow-history partial results, scan
  failure keeps verified candidates, candidate/scan bounds, mid-deletion
  deadline accounting, no duplicates, foreign messages untouched, staged
  progress (counters, bar, single-count rejection accounting), panel-edit
  throttling/single-flight/failure tolerance, chunk ceilings, event-loop
  yielding — plus the two new frozen tests above.
* `tests/test_delete_panel_ownership.py` — helper-owned panel editing tests
  now also drive the capability through the same helper (same
  `run_all()`), plus a frozen test asserting no panel edit at all.
* AI Delete tool / semantic-delete / silent-delete / durable-delete suites —
  untouched, still green.

Exact runs:

```
focused Delete suites (test_26 … test_34, ownership, durable delete) ... 159 passed
UI + ownership + coloured buttons .................................... 111 passed
full suite ........................................................... 7206 passed, 27 skipped, 1 failed, 3 warnings in 211.07s
py_compile / compileall (delete.py + 4 test files) ................... exit 0
git diff --check ..................................................... clean
```

The single full-suite failure is the pre-existing
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
(“interval advancement exceeded bound”), which reproduces on the untouched
baseline `c8e5e90`; neither the scheduler nor any of its files are touched by
this diff. (One transient additional failure during this work,
`test_colored_buttons …test_delete_submenu_is_primary`, asserted the frozen
button's presence; it was updated to assert the freeze and is green.)

## 5. Limitations

* No live Telegram behaviour was verified: the tests prove the panel no
  longer renders the option, the callbacks are inert, and no service or panel
  edit is reached — not the rendering of the frozen notice on a real client.
* The frozen implementation is intentionally kept (dead from the UI's point
  of view) for reactivation; it is covered by service/painter-level tests
  rather than callback tests.
* The AI delete tool in `mode: "all"` remains reachable through the assistant
  (untouched by design). If it too is ever to be gated, that belongs to the
  AI-tool governance, not to this UI freeze.
