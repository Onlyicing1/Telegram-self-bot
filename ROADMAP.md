# Roadmap — Model Tester panel delivery

Status legend: **IMPLEMENTED + TESTED** = code in this tree with regression
coverage; **IMPLEMENTED, NOT LIVE-VERIFIED** = code exists, no real Telegram
client was exercised; **LIMITATION** = deliberately not done / not provable here.

| Capability | Status | Where |
|---|---|---|
| Test Modules results body is strictly bounded (no per-model or provider listing, no content proportional to the discovered catalog) | IMPLEMENTED + TESTED | `backend/bot/handlers/ai.py::_render_test_results` |
| Body keeps the essential summary: heading, available/failed counts, failure-category counts, current runtime pair, snapshot provenance, usable-model total, button-access hint | IMPLEMENTED + TESTED | `_render_test_results`, `_snapshot_line`, `_current_model_line` |
| Composed view (title + body) is validated against the ONE caption capability helper instead of a second limit | IMPLEMENTED + TESTED | `_results_body_fits_caption` → `appearance_service.fits_as_caption` |
| Deterministic bounded fallback if the composed view ever cannot be a caption (no truncation of arbitrary text) | IMPLEMENTED + TESTED | `_render_test_results` |
| Every usable `(provider, model)` pair reachable exactly once through paginated buttons | IMPLEMENTED + TESTED | adaptive grid + `action:ai_test_page` |
| Exact `action:ai_pick_model:<provider>:<model>` callback data, deterministic order | IMPLEMENTED + TESTED | `_render_test_results` button builder |
| Active-model marker, `·free`, `·cached`, latency indicators preserved on the buttons | IMPLEMENTED + TESTED | `_render_test_results`, `_usable_results` |
| Paging is presentation-only: no provider test, no payload mutation | IMPLEMENTED + TESTED | `_ai_test_page_action` |
| Opening Test Modules shows the saved snapshot with zero provider calls | IMPLEMENTED + TESTED | `_ai_test_models_action` |
| Streaming completion and saved-snapshot opening share the same canonical renderer | IMPLEMENTED + TESTED | `ai_test_progress.run_streaming_test` → `_render_test_results` |
| Initial visit without a snapshot runs the diagnostic; **Refresh All Models** is the only explicit full re-run | IMPLEMENTED + TESTED | `_launch_test_run`, `_ai_test_refresh_action` |
| All Results keeps full per-model diagnostics and its own pagination (nothing omitted) | IMPLEMENTED + TESTED | `_ai_test_details_action` |
| Panel delivery behaviour (message identity, single-writer painter, `home_view` restriction, Appearance cache/delivery) unchanged | IMPLEMENTED + TESTED | `backend/helper/panels.py`, `backend/services/appearance_service.py` |
| A real Telegram photo panel accepts the compact caption edit | IMPLEMENTED, NOT LIVE-VERIFIED | production observation required |

## Invariants this task must not break

- One renderer for the results view: no second rendering path, no second tester,
  no extra scheduler or execution route.
- The panel message is edited, never replaced, for operational results
  (`home_view=False` stays intact); the caption limit is solved by bounding the
  content, not by swapping the panel.
- Provider discovery, model testing, snapshot persistence, provider selection and
  fallback eligibility are untouched.
- Save and Appearance behaviour (upload, cache, delivery states) are untouched.
- Telegram's own 64-byte callback-data budget still applies to very long
  `(provider, model)` ids through the existing `truncate_callback_data` helper;
  this task does not change that encoding rule.

## Remaining work

1. **Live verification:** open Test Modules on a deployment where the Appearance
   preview is active (photo panel), run/refresh the full model set, and confirm
   the results page paints with no `caption is too long` error.
2. Observe a genuinely large real catalog (hundreds of usable models) through all
   pages to confirm the paginated access on a real Telegram client.
3. If a future change adds per-model lines back to the main body, it must go
   through `fits_as_caption` and the bounded-summary rule above.

## Known limitations

- Long provider/model pairs can exceed Telegram's 64-byte callback budget and are
  truncated by the pre-existing helper; this predates the display fix and is not
  changed here.
- The exact client rendering of a long caption (line wrapping, label elision) is
  Telegram-side behaviour and is not testable in this workspace.
