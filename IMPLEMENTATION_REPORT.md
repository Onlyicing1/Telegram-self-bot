# IMPLEMENTATION REPORT — Model Tester results caption bound + complete paginated access

_Base commit: `1d0ed3afe8e2cac93f65073de62211713cf932a6` (`main`). This document
replaces all earlier reports; only the current verified state is described._

## 1. Root cause

`backend/bot/handlers/ai.py::_render_test_results()` is the canonical Test
Modules results view, and it printed **every usable model** (provider group +
one line each) plus up to eight failed-model lines. The panel message can be a
**photo** when the Appearance preview is active, so its text is a Telegram
**media caption**, capped at 1024 characters. With a large discovered catalog the
composed `**Test Modules**\n\n<body>` text exceeded that cap, Telegram rejected
the edit with `The caption is too long (caused by EditInlineBotMessageRequest)`,
`_safe_edit` logged `[CALLBACK] edit FAILED`, and the owner kept seeing the
previous panel content.

## 2. Implemented

### 2.1 Bounded, deterministic summary (main body)

`_render_test_results()` now emits only:

- the `**Model Tests**` heading,
- the counts line — available, failed, rate-limited, not configured, invalid,
  no credits,
- `Current: <provider> · <model>` (authoritative runtime pair),
- the snapshot provenance line (cached/saved/partial/unsaved, with timestamp and
  complete/partial state where applicable),
- `**✓ Usable Models: <N>**`,
- one line: `_• Every usable model is on the buttons below._` (or
  `_No usable chat models right now._` when the set is empty).

The per-model listing and the failed-model listing were removed from the body;
no content grows with the number of discovered models or providers.

### 2.2 Validated with the existing caption capability helper

Added `_TEST_RESULTS_TITLE = "Test Modules"` and
`_results_body_fits_caption(body)` → `appearance_service.fits_as_caption(title,
body)` (which composes through `_compose_rendered` and checks the single
`MAX_CAPTION_CHARS = 1024` / `caption_for` boundary). No second limit was
introduced. If the composed view ever failed (not reachable with realistic
identifiers), the renderer falls back to the same summary without the optional
hint line — designed, never truncated.

### 2.3 Access preserved through the existing pagination

Unchanged: adaptive 2/3-column grid, 12/18-button page sizes, Prev/Next buttons,
page counter, active-model in-place mark, `·free` / `·cached` tags, latency
labels, and exact `action:ai_pick_model:<provider>:<model>` callback data. Every
usable pair remains reachable exactly once across pages.

### 2.4 Workflow and delivery unchanged

- Opening Test Modules shows a saved snapshot (zero provider calls); no snapshot
  starts the initial diagnostic; **Refresh All Models** is still the only
  explicit full re-run; paging never tests or mutates.
- Streaming completion and snapshot opening share the same renderer.
- No change to `home_view` restriction, panel lifecycle, single-writer painter,
  message identity, or Appearance delivery/cache.
- All Results (`_ai_test_details_action`) is untouched: same full diagnostics and
  pagination.

## 3. Files changed

| File | Change |
|---|---|
| `backend/bot/handlers/ai.py` | `_render_test_results` bounded body; `_TEST_RESULTS_TITLE`, `_results_body_fits_caption`; removed per-model body listing and failed-list block; buttons/pagination untouched |
| `tests/test_model_tester_results_caption.py` | **NEW** — 11 regression tests (caption bound on every page, summary content, complete paginated access, exact callbacks, indicators, page purity, saved-snapshot open, streaming completion, initial/refresh workflow, All Results pagination) |
| `tests/test_test_modules_grid.py` | Assertions moved from the old body listing to the bounded summary + button markers |
| `tests/test_13_model_selection.py` | Compact body assertions; provider/model listing no longer expected |
| `tests/test_14_tool_honesty_glass.py` | Compact body assertions; "Not usable" listing absent; buttons still carry models |
| `tests/test_gemini_model_discovery.py` | Summary counts + button access instead of model lines in the body |
| `tests/test_symbols.py` | Registry-glyph assertions updated (active mark asserted on the button) |
| `tests/test_model_tester_snapshot.py` | Cached-state test verifies the summary, `·cached` buttons and cached detail one tap away; pager test expects the bounded body |
| `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md`, `ROADMAP.md` | Replaced in full with this verified state |

Not touched: provider discovery/testing, snapshot persistence, provider
selection/fallback, Save/Appearance behaviour, panel lifecycle, Supabase,
`DATABASE_ARCHITECTURE.md`.

## 4. Tests and validation

| Check | Result |
|---|---|
| Focused Model Tester / grid / panel-delivery / Appearance suites (13 files, incl. the new one) | **302 passed** |
| Full suite (`pytest -q`) | **7362 passed, 27 skipped, 1 failed** (143.98 s) |
| `py_compile` on the changed module and test files | exit `0` |
| `git diff --check` | exit `0` |
| Complete diff review | no behaviour change outside the results body; buttons, pagination, workflow and delivery untouched |

The single full-suite failure is the pre-existing
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
(“interval advancement exceeded bound”), which also fails on the untouched base
tree and involves no file in this diff. It is reported, not fixed here.

Environment note: the workspace venv was missing `Pillow`, which
`backend/requirements.txt` pins at `pillow==11.3.0` and the Appearance suite
imports. Installing that pinned version was required to run the full suite; it is
a local setup step, not a source change.

## 5. Git

One commit on top of `1d0ed3a`, subject
`fix: bound the model tester results caption and keep every model reachable`.
Fast-forward delivery only: no rebase, force-push or history rewrite. The commit
cannot contain its own SHA; the exact SHA and the fresh
`git fetch` → `rev-parse` → `merge-base --is-ancestor` verification are recorded
in the task's delivery message.

## 6. Not live-verified

No Telegram session, owner chat or Render deployment is available here, so:

- no real photo panel was edited and the `caption is too long` error was not
  reproduced against Telegram;
- the real catalog's page count and client-side rendering were not observed.

The bound is proven by composing the real view through the repository's own
caption capability helper in tests, not by a live edit.

## 7. Known limitations

- Telegram's 64-byte callback-data budget still truncates very long
  `(provider, model)` pairs via the pre-existing `truncate_callback_data`
  helper; that encoding rule is unchanged by this task.
- The body shows counts rather than per-model failure lines; full per-model
  evidence (including `RATE_LIMITED (CACHED)`) remains one tap away in
  **All Results**.
