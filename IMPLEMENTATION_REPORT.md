# Implementation Report — Ghost Seen v2 Stage 4

## Scope

Added the explicit Action Menu after Stage 3 message selection. Stage 1 browser, Stage 2 Message Viewer, and Stage 3 selection were preserved. Reply and AI Reply are visible but inert next-stage placeholders; no execution, input, provider call, generation, or delivery was added.

## Files changed

- `backend/services/ghost_seen_v2.py` — source-keyed selection validation and inert action placeholder helpers.
- `backend/bot/handlers/ghost_seen_v2.py` — Action Menu panel, action callbacks, stale-selection checks, Back navigation, and Reply/AI Reply placeholders.
- `tests/test_55_ghost_seen_v2_stage4.py` — focused Action Menu state/placeholder tests.
- `INVESTIGATION.md` — Stage 4 callback/state tracing.
- `IMPLEMENTATION_REPORT.md` — this report.

## Behavior

The viewer shows an explicit `Actions (N)` control only when current source-chat selection exists. The Action Menu preserves the source chat and complete selected message IDs, handles one or multiple selections, and fails closed when selection is absent, stale, or from another source. Reply and AI Reply return `Coming in the next stage.` without creating input state or performing any Telegram/AI operation. Back returns to the viewer. No legacy `ai_prompt` callback or prompt text was restored, and no Refresh control was introduced.

## Validation

- Stage 1 + Stage 2 + Stage 3 + Stage 4 focused tests: **17 passed**.
- Full Python suite: **823 passed, 23 skipped, 1 pre-existing warning**.
- `.venv/bin/python -m compileall -q backend`: **PASS**.
- `git diff --check`: **PASS**.
- `bun tsc -b --noEmit`: **PASS**.
- Telegram live E2E was not performed.

## Delivery

Commit and remote verification are completed after this validation.
