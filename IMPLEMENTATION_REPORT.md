# Implementation Report — Ghost Seen v2 Stage 3

## Scope

Added message selection to the existing Stage 1 browser and Stage 2 Message Viewer. AI, Reply, Action Menu, prompts, disclosure, generation, delivery, providers, web search, and You.com remain untouched and unimplemented.

## Files changed

- `backend/services/ghost_seen_v2.py` — source-chat-keyed selection model and viewer rendering support.
- `backend/bot/handlers/ghost_seen_v2.py` — selection callbacks, selected-count/clear controls, and same-source pagination state.
- `tests/test_54_ghost_seen_v2_stage3.py` — Stage 3 selection regression coverage.
- `INVESTIGATION.md` — Stage 3 source/state tracing.
- `IMPLEMENTATION_REPORT.md` — this report.

## Behavior

Each visible message has a compact Select/Selected toggle control while message text remains content. Selection uses the real Telegram message ID and is keyed by source private chat ID, not panel chat ID, display name, username, page, or message text. Identical message text can therefore be selected independently. Selection survives pagination within one source chat, is cleared when opening a chat, and can be cleared without changing the current chat or page. The viewer header reports `N selected`.

Zero, one, and multiple selections are display-only in Stage 3. No Action Menu, AI, Reply, prompt, disclosure, generation, or delivery path is registered or executed. Back remains the Stage 2 navigation control; later v2 stages are not introduced.

## Validation

- Stage 1 + Stage 2 + Stage 3 focused tests: **14 passed**.
- Full Python suite: **820 passed, 23 skipped, 1 pre-existing warning**.
- `.venv/bin/python -m compileall -q backend`: **PASS**.
- `git diff --check`: **PASS**.
- `bun tsc -b --noEmit`: **PASS**.
- Legacy Ghost Seen AI callback/prompt identifiers remain absent from production code; historical skipped assertions remain only in `tests/test_51_execution27.py`.
- Exactly one `INVESTIGATION.md` exists.
- Telegram live E2E was not performed.

## Delivery

Commit and remote verification follow this final validation.
