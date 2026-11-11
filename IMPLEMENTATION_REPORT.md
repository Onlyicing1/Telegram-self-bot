# IMPLEMENTATION REPORT — Appearance Composition Deduplication (2026-11-11)

## 1. Request

Every piece of text intended to appear on a LifeOS Glass UI main page must
appear exactly once in the final rendered output. The owner reported identical
text repeated consecutively inside one panel. The fix had to cover every
eligible main page, keep all existing customization working without the owner
clearing settings, and leave sub-pages, pickers, confirmations, action results
and progress messages untouched.

## 2. Root cause

`backend/services/appearance_service.py::compose_text()` assembled one home
view from three independent sources:

1. the global header (`appearance_header_title` / `appearance_header_body`),
2. the per-page override (`appearance_page_overrides`),
3. the page handler's own title/body.

Both composition branches concatenated those sources unconditionally:

- With a header title, the body was `page title` + `header_body` + `page_body`
  joined with blank lines.
- Without a header title, the header body was prepended to `page_body`.

No branch compared the fragments. Identical text configured in two places was
therefore emitted twice. Concrete paths that produced the reported panel:

- global header body `X` and page override body `X`;
- global header body `X`, page override body `X`, and live handler body `X`;
- page override body equal to the handler's non-default live body.

The page override path had a related duplication: when the override body was
identical to a non-default handler body, the code took the `else` branch and
produced `override\n\nhandler_body`, i.e. the same text twice.

## 3. What was ruled out

The duplication was **not** composition being applied twice.

- **Initial inline rendering**: the panel builder returns raw results.
  `inline_engine` calls `appearance_service.apply_to_inline_results()` once, and
  that function calls `with_preview()` → `compose_text()` once per result.
- **Callback navigation**: `panels._handle_panel()` calls `_apply_appearance()`
  once; `_render_and_edit()`, `_finalize_panel()` and the delivery-replacement
  path either paint the already-composed tuple or ask Telegram for a fresh
  inline result (which starts again from the raw builder output).
- **Lifecycle reuse**: `PanelLifecycleManager._try_reuse` renders the handler
  result directly and does not compose a second time.
- **Repeated updates**: each render starts from the handler's raw body, so the
  layer is not accumulated across repaints.

No double-composition path was found, and none was changed.

## 4. Implementation change

`backend/services/appearance_service.py`:

- `compose_text()` now builds a `page_fragments` list from the page override and
  the handler body:
  - override body is appended;
  - the handler body is appended only when it is non-empty and is not the
    page's canonical default body (the override still replaces a default body,
    as before);
  - with no override, the handler body is the single page fragment.
- Both header branches now join `[header_body, *page_fragments]` through
  `_distinct_fragments()`, which drops empty fragments and repeats of the SAME
  whole stripped fragment.
- The page title is still emitted as `**title**` only when it differs from a
  customized header title, so no title is repeated either.
- Order is unchanged: page title, header description, page override, live
  handler body.

`_distinct_fragments()` compares whole stripped fragments only. It never splits
a handler body into lines, so legitimate repeated content inside dynamic output
is preserved. A header line that merely occurs inside a longer live body is not
treated as a duplicate.

No setting storage, write path, reset behaviour, gallery/preview delivery,
callback registration, button set or database code was changed.

## 5. Behaviour matrix

| Configuration | Composed body |
|---|---|
| No customization | handler title/body unchanged |
| Header body only | header body, then handler body (once each) |
| Header body == page override body | the shared text once, then live handler body |
| Header body == live handler body | once |
| Override body == live handler body | once |
| Different header and page text | both once, header first |
| Same override on two pages | each page shows it once; no cross-page leak |
| `extra` non-empty (sub-view/result) | untouched |
| Ineligible panel id | untouched |

## 6. Regression tests

Added to `tests/test_appearance_ui.py` (10 tests):

1. `test_identical_header_and_page_body_are_rendered_once`
2. `test_identical_header_and_page_body_with_header_title_are_rendered_once`
3. `test_identical_page_override_and_live_body_are_rendered_once`
4. `test_header_body_matching_page_override_keeps_live_content_once`
5. `test_different_header_and_page_text_both_appear_once_in_order`
6. `test_identical_text_on_two_pages_stays_isolated`
7. `test_defaults_are_unchanged_for_every_eligible_page`
8. `test_repeated_rendering_does_not_accumulate_text`
9. `test_inline_and_callback_composition_agree`
10. `test_operational_results_and_subviews_are_untouched_by_composition`

They cover both empty-default pages (`settings`, `ai`) and non-empty-default
pages (`menu`, `del`), a live dynamic body, repeated rendering, the initial
inline result vs the callback render, cross-page isolation, defaults, and
sub-view/confirmation non-interference.

## 7. Test and validation results

- `pytest tests/test_appearance_ui.py tests/test_appearance_delivery.py
  tests/test_appearance_gallery.py tests/test_appearance_image_cache.py
  tests/test_panel_update_delivery.py tests/test_model_tester_results_caption.py
  -q` → **165 passed**, exit 0.
- `pytest tests -q` → **7383 passed, 27 skipped, 1 failed** in 144.01 s.
- `py_compile` on the changed service and test module → exit 0.
- `git diff --check` → exit 0.

Known pre-existing failure (unrelated to Appearance, reproduces independently of
this change): `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`.

## 8. Verified versus assumed

Verified by execution:

- The new and existing Appearance/panel tests pass together.
- The full suite's only red test is the known scheduler failure.
- Both render paths invoke `compose_text` exactly once (source trace plus the
  inline-vs-callback equality test).

Assumed / not verified:

- No live Telegram client was driven, so the owner's real panel was not
  re-rendered against production Supabase settings. The same pure functions and
  settings accessors the runtime uses were exercised with mocked persistence.
- The screenshot was not available in the workspace; the fix targets the
  composition paths established from source and the described symptom.

## 9. Delivery

One commit on `main` containing the service change, the regression tests and
these documents:

`fix(appearance): render composition-layer text once`

The commit was pushed normally to `origin/main`; `git fetch origin` +
`git rev-parse origin/main` + `git merge-base --is-ancestor` verified that remote
`main` contains it, and the working tree was clean afterwards. No rebase, force
push or history rewrite was performed.
