# Implementation Report — LifeOS Telegram Self-Bot

## Task / Result

Fixed the remaining You.com runtime-capability defect and retained the
required Ghost Seen automatic-reply behavior.

- **You.com runtime capability:** the web-search tool was registered and
  visible, but it could resolve the process-global Engine instead of the live
  Engine/ProviderManager handling the request. The request-scoped live
  ProviderManager is now carried through `ToolContext` and reaches the
  existing `ToolRegistry` → `ToolExecutor` → `WebSearchTool` →
  `WebSearchService` → `ProviderManager.web_search()` → `YouSearchProvider`
  path. You.com remains a `web_search` capability and is never treated as a
  chat/LLM provider or allowed to execute Telegram actions.
- **Ghost Seen single-message AI reply:** after target selection, context
  count, and mandatory disclosure choice, execution now starts immediately.
  No owner-written prompt input is opened. The fixed task asks for the next
  natural reply from the owner to the recipient, with explicit `OWNER` and
  `RECIPIENT` context roles. Delivery still resolves `GHOST_ROOM_ID` and
  fails closed when it is missing or invalid.

The earlier You.com discovery/visibility fix is preserved in `4972693`; this
execution fixes the separate live tool-loop connection that the live check
exposed.

## Starting state

- `HEAD` was `4972693` (`fix: expose You.com web-search capability and Ghost
  Seen explicit AI reply flow`) with the runtime-capability and automatic
  Ghost Seen adjustments uncommitted.
- Discovery and UI already reported You.com as available when configured, but
  an actual AI tool call had not been proven to use the request's live
  ProviderManager.
- Ghost Seen still had tests and implementation remnants for the removed
  single-message prompt step.
- No existing user changes were discarded; no deployment was performed.

## Exact files changed in this execution

Backend:

- `backend/ai/engine/dispatcher.py` — keeps the live ProviderManager on the
  Dispatcher and injects it into each per-request ToolContext.
- `backend/ai/engine/engine.py` — synchronizes the attached ToolRegistry with
  the Dispatcher during runtime tool wiring.
- `backend/ai/tools/websearch.py` — passes the request-scoped manager to the
  web-search service and returns a safe generic failure message.
- `backend/services/web_search_service.py` — accepts an optional injected
  ProviderManager while retaining the existing global-engine compatibility
  path for direct callers.
- `backend/services/ghost_seen_service.py` — defines the fixed owner-to-
  recipient reply task and explicit OWNER/RECIPIENT context formatting.
- `backend/bot/handlers/ghost_seen.py` — removes the single-message prompt
  registration and makes the disclosure callback execute the automatic AI
  reply immediately.

Tests:

- `tests/test_11_runtime_wiring.py` — updates the scripted provider test
  double to declare native tool support.
- `tests/test_45_ghost_seen.py` — verifies the fixed role-aware Ghost Seen
  task text.
- `tests/test_49_ghost_seen_flows.py` — verifies automatic execution,
  disclosure handling, delivery, and fail-closed behavior without a prompt.
- `tests/test_51_execution27.py` — verifies context → disclosure → automatic
  delivery and destination invariants.
- `tests/test_52_you_search.py` — adds the full Engine native-tool regression
  from tool definitions through the injected ProviderManager and mocked
  YouSearchProvider, plus honest unavailable behavior.

## Exact architecture and behavior changes

### You.com runtime path

1. `Engine.attach_tools()` wires one ToolRegistry/ToolExecutor into the
   Dispatcher.
2. `Dispatcher.dispatch()` builds native definitions from that registry and
   adds the live `self._provider_manager` to the request ToolContext.
3. `ToolExecutor` remains the sole caller of `tool.execute()`.
4. `WebSearchTool` forwards the injected manager to `do_web_search()`.
5. `WebSearchService` invokes `ProviderManager.web_search()`.
6. `ProviderManager` selects only a healthy registered `web_search` provider;
   it never routes retrieval through chat fallbacks.
7. `YouSearchProvider.search()` performs the existing You.com Search API
   request and returns normalized results.

When no You.com provider is configured, the same tool returns an honest
unavailable result; it does not fabricate search results or claim that a
search occurred. The You.com API key remains runtime-environment-only via
`YDC_API_KEY`.

### Ghost Seen automatic reply path

`select one incoming message` → `Reply with AI` → `context count` →
`disclosure choice` → `_execute_single_ghost_ai_reply()` → fixed
`execute_ghost_seen_ai()` task → delivery to the resolved `GHOST_ROOM_ID`.

The removed `ai_reply_prompt` input is not registered. Informing the recipient
adds `AI_DISCLOSURE_SUFFIX`; opting out sends the generated text unchanged.
Missing/invalid `GHOST_ROOM_ID`, missing context, an owner-authored target,
AI failure, or delivery failure produces no fallback send.

## Intentionally untouched

- You.com provider identity, endpoint, authentication, response
  normalization, discovery registration, capability-only status UI, and
  chat-provider filtering from the preceding visibility fix.
- Provider fallback/retry/health implementation and the existing
  ProviderManager routing boundary.
- Save, Delete, Retrieve, retention, profile engines, supervisor, and
  Telegram authorization behavior.
- `ghost_chats` remains a source/private-chat registry and is never a
  destination selector.
- Database tables, migrations, schema, and persistence behavior.
- Render configuration and deployment; no Render deploy was performed.

## Database/schema impact

None. No database field, table, migration, or persisted credential was added.
`YDC_API_KEY` remains environment-backed and is never written to source,
tests, telemetry, exceptions, or reports.

## Validation

- Focused command:
  `.venv/bin/python -m pytest tests/test_11_runtime_wiring.py tests/test_52_you_search.py tests/test_49_ghost_seen_flows.py tests/test_45_ghost_seen.py -q --asyncio-mode=auto`
  — **122 passed**.
- Full command:
  `.venv/bin/python -m pytest tests/ -q --asyncio-mode=auto`
  — **909 passed, 1 warning**. The warning is the existing Starlette
  `multipart` PendingDeprecationWarning.
- `.venv/bin/python -m compileall -q backend` — **PASS**.
- `git diff --check` — **PASS**.
- `bun tsc -b --noEmit` — **PASS**.
- A repository-wide check confirms the single-message
  `ai_reply_prompt`/`Type your AI prompt` path is no longer registered; the
  remaining `ai_prompt` is the intentional legacy multi-select input.

## Live You.com verification

This workspace reported `YDC_API_KEY=absent` using a secret-safe check. No
real request was sent to `https://ydc-index.io/v1/search`, and no live API
success is claimed. The mocked full Engine tool-loop test proves the runtime
invocation path; the owner must restart the Render process after setting
`YDC_API_KEY` and perform the live verification there.

## Delivery

- Commit: `50012de0167aabcc400168891dea2d671633f4cc`.
- Push to `origin/main`: completed after the report-only follow-up commit.
- Remote HEAD verification: completed; `origin/main` matches local `HEAD`.
- Final working-tree cleanliness: verified clean.

## Stop

Implementation, validation, commit, push, and remote verification are complete.
