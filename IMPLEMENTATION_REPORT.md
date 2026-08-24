# Implementation Report — LifeOS Telegram Self-Bot

## Task / Result

Fixed the two runtime failures found during owner live verification:

- A successful You.com search no longer causes redundant follow-up search calls
  to exhaust the tool-round limit. When a `web_search` execution succeeds and
  returns non-empty normalized sources, the dispatcher terminates the tool loop
  with an evidence-based result from the actual tool output.
- Ghost Seen's legacy `ai_prompt` input is now explicitly blocked for a single
  selected message. The single-message route remains the Glass flow:
  selection → `ghost_actions` → context count → disclosure → automatic AI
  generation. The legacy typed prompt remains available only for 2+ selected
  messages.

## Root causes

1. The dispatcher always requested another provider continuation after every
   successful tool call, including a completed web search. A model that kept
   requesting searches could therefore reach `MAX_TOOL_ROUNDS` even though the
   first search had usable evidence.
2. The Ghost Seen registration still contains the intentional legacy
   `ghost_chat:ai_prompt` input. A stale/legacy single-selection callback could
   therefore open that prompt directly. The handler now fails closed whenever
   that input is invoked with fewer than two selected messages.

## Exact files changed

- `backend/ai/engine/dispatcher.py` — terminates after successful, non-empty
  web-search evidence instead of issuing a redundant continuation.
- `backend/bot/handlers/ghost_seen.py` — documents and enforces that
  `ai_prompt` is multi-select-only.
- `tests/test_45_ghost_seen.py` — updates the destination regression to use the
  authoritative automatic disclosure flow.
- `tests/test_52_you_search.py` — adds a full orchestration regression proving
  one successful search produces a final result without a second model call.

## Exact behavior

The web-search path remains:

`Engine/Dispatcher → ToolRegistry/ToolExecutor → WebSearchTool →
WebSearchService → ProviderManager.web_search() → YouSearchProvider`.

After a successful web search with non-empty normalized `results`, the
Dispatcher returns the formatted actual search output and does not call the
chat provider again for a redundant search. Empty or failed searches do not
claim success and retain the existing continuation/error behavior.

Ghost Seen exactly-one selection cannot enter `ai_prompt`, even if an old
callback or stale UI state invokes it. It is cleared and logged instead. The
normal one-message action menu and automatic AI reply flow are unchanged:
context selection and mandatory disclosure are followed immediately by fixed
AI generation, with `OWNER`/`RECIPIENT` role-aware context and
`GHOST_ROOM_ID` fail-closed delivery.

## Intentionally untouched

- You.com endpoint, authentication, environment configuration, provider
  registration, capability-only classification, and health routing.
- Provider fallback/retry architecture other than the bounded successful-search
  termination condition.
- Multi-select Ghost Seen typed prompt behavior for 2+ messages.
- Save, Delete, Retrieve, profiles, supervisor, Telegram authorization, and
  database/schema behavior.
- Render deployment/configuration. No deployment was performed.

## Database/schema impact

None. No schema, migration, database field, or persisted credential changed.
`YDC_API_KEY` remains runtime-environment-only.

## Validation

- Focused You.com + Ghost Seen tests: **104 passed**.
- Full suite: **910 passed, 1 existing Starlette multipart warning**.
- `bun tsc -b --noEmit`: **PASS**.
- `.venv/bin/python -m compileall -q backend`: **PASS**.
- `git diff --check`: **PASS**.

The new orchestration regression verifies the actual dispatcher boundary: one
chat-provider tool call, one `web_search` execution, actual result propagation,
and no redundant second provider call. The Ghost Seen regression verifies that
single-message automatic execution uses the disclosure route rather than the
legacy typed prompt.

## Live verification limitations

No live You.com request was made in this workspace because `YDC_API_KEY` was
not available here. The owner’s earlier live verification established that the
You.com request itself succeeds and exposed the redundant-loop behavior fixed
in this change. A Render process restart remains required after environment
changes; live confirmation of the final behavior must be performed there.

The owner should also verify the Ghost Seen callback from a fresh Render
process/session so no stale inline message remains; any stale single-selection
`ai_prompt` callback now fails closed rather than opening an owner prompt.

## Delivery

- Commit: to be recorded after commit.
- Push: to `origin/main` after commit.
- Remote HEAD: to be verified after push.
- Final working-tree state: to be verified after delivery.

## Stop

Implementation and validation are complete pending commit, push, and remote
verification.
