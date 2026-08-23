# Implementation Report — LifeOS Telegram Self-Bot

## Task / Result

Two real production problems reported after Execution-29 were fixed:

1. **You.com provider was not visible/available** — the provider itself was
   registered correctly at process start from `YDC_API_KEY`, but the
   discovery/status surfaces classified and displayed every provider as a
   chat/LLM provider. The capability was therefore hidden from the user
   (and the `you` entry could even appear in generic provider/model lists).
   Discovery, the Telegram AI panel, the dashboard, model testing, and the
   selection API are now capability-aware: `you` is exposed as a
   **web_search capability** everywhere, is never selectable as a chat
   provider, and shows an explicit Available / Not configured status.
2. **Ghost Seen single-message AI reply flow was wrong** — it still opened
   the legacy generic multi-select prompt. The flow now follows the required
   explicit sequence: select one message → action menu with an unambiguous
   REPLY TARGET banner → Reply myself (quote / no quote) or AI Reply →
   context-size menu → disclosure choice (inform recipient yes/no) →
   dedicated single-message AI prompt input armed only after those choices.
   Every output path still resolves `GHOST_ROOM_ID` and fails closed when it
   is missing/invalid; `ghost_chats` is never a destination.

## Starting state

- Working tree contained uncommitted Execution-28/29 continuation work
  (capability-aware discovery/registry edits and the new Ghost Seen flow),
  with the old auto-execute/no-disclosure tests still failing (7 failures
  in `tests/test_49_ghost_seen_flows.py`).
- All prior commits preserved; nothing was discarded.

## Exact files changed

Backend:
- `backend/ai/discovery.py` — `ProviderStatus` gains `capability_kind` +
  `capabilities`; the `you` provider metadata declares
  `capability_kind="web_search"`; capability providers with a key are
  reported `available` without an LLM `/models` validation call; all status
  constructors and `get_wizard_info()` propagate the fields.
- `backend/ai/model_tester.py` — model-test targets skip non-chat providers.
- `backend/ai/providers/registry/registry.py` — `list_metadata()` exposes
  `display_name`, `configured`, and `capability_kind` per provider.
- `backend/bot/handlers/ai.py` — provider panel splits chat vs capability
  providers; new `🔎 Web Search` panel (`panel:ai_web_search`) shows
  You.com status and its env var; wizard list filters to chat providers.
- `backend/bot/handlers/ghost_seen.py` — `ghost_ctx` renders the context
  menu then the disclosure menu (never auto-executes); new
  `ghost_inform:<yes|no>` action records the choice and arms the dedicated
  `ai_reply_prompt` input; new `_ghost_ai_reply_prompt_input` executes the
  AI reply with fail-closed `GHOST_ROOM_ID` resolution and the optional
  disclosure suffix; disclosure prompt is bound to the resolved panel chat.
- `backend/services/ghost_seen_service.py` — reply-flow state gains
  `informed`; `set_reply_disclosure()` validates context-then-disclosure
  ordering; `consume_reply_flow()` requires all three fields; removed
  `AUTO_REPLY_INSTRUCTION`, added `AI_DISCLOSURE_SUFFIX`; the engine prompt
  now uses a fixed reply task + `Owner instruction: <prompt>`.
- `backend/web/app.py` — `/api/ai/models`, `/api/ai/models/{provider}` and
  `/api/ai/provider` reject/ignore non-chat providers.

Frontend:
- `src/lib/api.ts` — `ProviderStatus` gains `capability_kind`/`capabilities`.
- `src/components/AIConfigPanel.tsx` — providers split into chat vs
  capability sections; new **Capabilities** section shows You.com Search
  status (and env var when not available) with no select buttons; setup
  guide/wizard wording is chat-scoped.

Tests:
- `tests/test_52_you_search.py` — added `TestDiscoveryStatus`
  (key present → `you` `available` as web_search; missing/blank →
  `not_configured`, no key leak in discovery output) and registry-metadata
  capability assertions.
- `tests/test_49_ghost_seen_flows.py`, `tests/test_51_execution27.py`,
  `tests/test_45_ghost_seen.py` — stale auto-execute/no-disclosure
  assertions replaced with regressions for the explicit
  target → context → disclosure → prompt contract, verbatim delivery,
  disclosure-suffix opt-in, invalid-choice fail-closed, missing
  `GHOST_ROOM_ID` fail-closed, and context-fetch failure blocking sends.

## Exact behavior changed

- `you` is a web-search capability: visible in status surfaces, never a
  chat provider, never selectable via the chat-provider APIs, never queried
  for chat models.
- Ghost Seen single-message AI reply requires the full explicit sequence;
  the legacy generic prompt no longer opens for a single selection. The
  dedicated prompt is armed only after context + disclosure, bound to the
  panel chat, and delivery honors the disclosure choice.
- When `GHOST_ROOM_ID` is missing/invalid, no Ghost Seen send occurs on any
  path (reply, no-quote reply, single-message AI reply, legacy multi-select
  AI) — fail closed, never a fallback chat.

## Intentionally untouched

- Provider architecture (BaseProvider / ProviderManager / fallback / health
  tracker), AI engine/dispatcher/ToolExecutor, and web-search tool paths.
- Save, Delete, Retrieve, retention, fonts, profile engines, supervisor.
- Database/schema, migrations, and Render deployment configuration.
- `GHOST_ROOM_ID` remains ENV-backed; no second configuration system.

## Database/schema impact

None. No migration, table, or field added; `YDC_API_KEY` remains
environment-backed.

## Validation

- Focused suites (Ghost Seen flows, Execution-27, You.com, Ghost Seen entry):
  **146 passed**.
- Full suite: **907 passed, 1 warning** (pre-existing Starlette
  `multipart` deprecation).
- `python -m compileall -q backend`: **PASS**.
- `git diff --check`: **PASS**.
- `bun tsc -b --noEmit`: **PASS**.

## Operational requirement

The provider registry is built once at process start. A newly added
Render environment variable (`YDC_API_KEY`) requires a **process restart**
before `YouSearchProvider` is registered; the AI panel's Refresh action
only re-scans discovery, it cannot reload env vars into the running
process. The dashboard and Telegram `🔎 Web Search` panel show
`YDC_API_KEY` as the required env var and the panel text notes the restart
requirement. No live You.com call was made; `YDC_API_KEY` was not present
in this environment.

## Remaining limitations

- Live You.com verification remains an owner/Render action (set
  `YDC_API_KEY`, restart, then issue a web-search request through the AI).
- The legacy multi-select `ai_prompt` input still exists for 2+ selected
  messages and is unchanged.

## Stop

Both reported production problems are fixed, tested, and validated. No
unrelated system was changed.
