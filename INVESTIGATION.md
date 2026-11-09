# INVESTIGATION — Model Tester results overflowed Telegram's media caption

_Base commit: `1d0ed3afe8e2cac93f65073de62211713cf932a6` (`main`). Every claim
below comes from this tree's source and from deterministic tests that drive the
real renderer, paging action, snapshot opening and streaming completion paths._

## 1. Symptom

After tapping **Test Modules**, the panel appeared unchanged and the runtime
reported:

```
The caption is too long (caused by EditInlineBotMessageRequest)
```

Telegram rejects media captions longer than **1024 characters**. The results
view put every usable model name into the message body, so a sufficiently large
run exceeded that limit and the final edit was refused.

## 2. Why a normal panel edit is a CAPTION edit

The Appearance feature can deliver a panel message as a **photo** whose caption
is the panel's composed text (`backend/services/appearance_service.py`:
`MAX_CAPTION_CHARS = 1024`, `caption_for`, `_compose_rendered`,
`_as_photo_result`). For such a message Telegram interprets an `edit` text as a
**caption** edit, so the composed text must fit 1024 characters.

The composed text is `render_edit(title, body)` — for this panel:

```
**Test Modules**

<body>
```

`fits_as_caption(title, body)` is the repository's existing capability check for
exactly this ("can a photo message carry this view at all?"), built on the same
`caption_for`/`MAX_CAPTION_CHARS` boundary. Nothing in the Model Tester path
consulted it.

## 3. Why the body grew without bound

`backend/bot/handlers/ai.py::_render_test_results()` is the ONE canonical results
view shared by the batch action result, the paging action, opening a saved
snapshot and the streaming completion terminal render. It built the body as:

- counts line,
- `Current:` runtime pair,
- snapshot provenance line,
- **`✓ Usable Models` followed by one line per AVAILABLE model**, provider-grouped,
- **`Not usable: N`** followed by up to 8 failed-model lines.

The usable listing is proportional to the discovered catalog (hundreds of
models). With long model ids and multiple providers the body alone passed 1024
characters, so the composed caption did too.

## 4. Why the owner saw no change

`backend/helper/panels.py::_safe_edit` catches the Telegram error, logs
`[CALLBACK] edit FAILED: The caption is too long …` and returns `False`.
`_PanelEditGuardian` retries a **terminal** render a bounded number of times and
then records `AI_TEST_PROGRESS terminal render failed after N retries`. The
message therefore stayed on its previous (progress/launch) content: the run had
completed, but its results could never be painted into a photo panel.

## 5. What was ruled out

- **Not** the Test Modules workflow: opening a saved snapshot still issues zero
  provider calls; only an explicit refresh starts the diagnostic.
- **Not** a second runner or scheduler: the streaming tester and the batch tester
  both feed the same renderer.
- **Not** panel replacement: action results are painted with `home_view=False`,
  so they edit the existing message and never replace it.
- **Not** the pager: `_ai_test_page_action` is presentation-only over the cached
  payload and never tests or mutates anything.
- **Not** snapshot persistence: the snapshot write path and cached payload are
  unchanged; the failing edit was purely the size of the rendered body.
- **Not** a missing caption-limit implementation: the capability helper already
  existed; the renderer simply ignored it.

## 6. Corrective shape

1. **Bounded body.** The canonical renderer now emits only: the heading, the
   result counts (available, failed, rate-limited, not configured, invalid, no
   credits), the current runtime pair, the snapshot provenance line, the usable
   total (`✓ Usable Models: N`) and one line stating that every usable model is
   on the buttons below. No content grows with the number of discovered models.
2. **Validation through the existing helper.** The composed view is checked with
   `appearance_service.fits_as_caption("Test Modules", body)`. If it ever failed
   (not reachable with realistic identifiers), the renderer falls back to the
   same summary without the optional hint — the summary is designed, never
   truncated.
3. **Access preserved through pagination.** The adaptive 2/3-column grid,
   page size, Prev/Next navigation, page counter, active-model marker,
   `·free`/`·cached` indicators, latency labels and exact
   `action:ai_pick_model:<provider>:<model>` callback data are unchanged. Every
   usable `(provider, model)` pair remains reachable exactly once across pages.

## 7. Evidence

- `tests/test_model_tester_results_caption.py` — 11 regression tests: a 61-model,
  3-provider snapshot composes under the caption limit with a 100-character
  margin (every page), the summary keeps counts/current pair/usable total/
  snapshot state, paging reaches all 61 pairs exactly once with exact callbacks,
  active/cached/free indicators stay on the buttons, paging starts no test and
  mutates nothing, opening a saved snapshot makes zero provider requests, the
  streaming completion uses the same renderer and fits, the initial/refresh
  workflow is unchanged, and All Results diagnostics stay complete and paginated.
- Updated UI tests that had pinned the old unbounded listing
  (`test_test_modules_grid.py`, `test_13_model_selection.py`,
  `test_14_tool_honesty_glass.py`, `test_gemini_model_discovery.py`,
  `test_symbols.py`, `test_model_tester_snapshot.py`).

## 8. Not observed live

No Telegram session, owner chat or Render deployment is available in this
workspace, so no real photo panel was edited and the `EditInlineBotMessageRequest`
error was not reproduced against Telegram. The correction is proven by composing
the real view through the real caption capability helper in tests, not by a live
client.
