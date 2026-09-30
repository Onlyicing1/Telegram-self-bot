# Regex / Command Parsing Investigation — `Onlyicing1/Telegram-self-bot`

- **Branch:** `main`
- **Commit audited:** `5c47f66bd0e4e8f5dd285ab4db6e8247f2c4b111` (== `origin/main` at time of audit)
- **Scope:** investigation only — no code changes, no fixes, no refactors.
- **Status:** COMPLETE

## 1. Executive Summary

The repository contains regex in **21 backend files (~60 call sites)**, but only **~15 locations across ~12 files** are command-flow-relevant. Exactly **one** is a Telegram text-command pattern (`^Menu$`); everything else is extraction (save codes, links, JSON blocks), validation, resilience classification, or security/formatting.

The AI tool flow is **structured-first by design**: native provider tool calls are preferred, and the deterministic token/vocabulary parser (`parse_command_intent`) is a *fallback* layer explicitly scoped to a narrow command vocabulary (save/delete/send/scheduling). Task/todo management is deliberately routed **semantically** through provider tool schemas.

**No regex bypasses `ToolRegistry` → `ToolExecutor`** — every execution path, native or parsed, converges on the single executor boundary. **No architectural violations found.**

The live "No active todos" + "Memory fallback" failure is **not regex-related**: it is a local-resource degradation in `task_repository` that truthfully degraded to in-memory storage.

## 2. Complete Regex Inventory

Command-relevant inventory (verified with surrounding code, not grep-only):

| Location | Command/Input | Regex Used | Purpose | Required? | Replacement Candidate |
|---|---|---|---|---|---|
| `bot/handlers/misc.py:703` | `Menu` (the only text command) | `pattern=r"^Menu$"` in `events.NewMessage(outgoing=True)` | Opens Glass UI mother panel | Required (Telethon-idiomatic) | Plain `text == "Menu"` guard inside a pattern-less handler (like trigger matching) |
| `ai/actions.py:165` | t.me link in prompt | `_TELEGRAM_LINK_RE` | Link extraction → save-by-link | Required | — (URL shapes need pattern matching) |
| `ai/actions.py:768` | save-code argument | `_SAVE_CODE_RE = ^[A-Z0-9]{1,12}$` | Validates save-code tool args | Partially redundant | Consolidate with `_SAVE_CODE_TOKEN_RE` (2220) |
| `ai/actions.py:2037` | any text | `_tokenize` → `re.findall(r"[a-z0-9\u0621-\u06ff]+")` | Substrate for the deterministic vocabulary (token sets, not regex command detection) | Required | — |
| `ai/actions.py:2220` | token `s0001` | `_SAVE_CODE_TOKEN_RE = ^s[0-9a-z]{1,11}$` | Classifies tokenized word as save code | Required | — |
| `ai/actions.py:2221` | random code `sxxxx` | `_SAVE_CODE_RANDOM_TOKEN_RE = ^s[0-9a-z]{4}$` | Collision-issued random codes | Required | — |
| `ai/actions.py:2228` | `S0001` in text | `_SAVE_CODE_CANONICAL_RE = (?<![A-Za-z0-9])S[A-Z0-9]{4}(?![A-Za-z0-9])` | Canonical code detection with boundary guards | Required | — |
| `ai/actions.py:2536-2538` | scheduling phrase | `\d{1,2}:\d{2}`, `ساعت\s*\d`, `\bat\s+\d{1,2}\b` in `_has_future_clock_request` | Detects clock-anchored scheduling intent | **Removable candidate** | Token-digit adjacency scan (`_tokenize` already preserves digits) |
| `ai/tools/task.py:208` | create_task request | `re.search(r"\d{1,2}:\d{2}", request)` | Completeness gate (clock anchor present?) before wizard | **Removable candidate** | Same token-digit scan via actions helpers |
| `ai/task_interpreter.py:21` | model output | `_JSON_BLOCK_RE` = fenced ```json block extractor (DOTALL) | Extracts model's fenced JSON candidate | Required (structured-output extraction, not command parsing) | — |
| `ai/task_execution.py:71` | occurrence prep output | Same fenced-JSON pattern | Occurrence-time preparation extraction | Required | — |
| `ai/task_candidate.py:56` | model-emitted candidate keys | `_COMPOUND_KEY_RE = ^(?:interval|every|each|repeat)_(minutes?|...)$` | Normalizes structured candidate keys | Required (schema normalization) | — |
| `ai/database/task_repository.py:131` | PostgREST error | `_UNKNOWN_COLUMN_RE = could not find the '([^']+)' column` | PGRST204 detection → optional-column retry | Required (resilience, not commands) | — |
| `services/retrieve_service.py:388-389` | user-typed code | `_SAVE_CODE_SHAPE = ^S[A-Z0-9]{4}$`, `_SEPARATOR_RE` | 0/1/N save-code resolution with spelling variants | Required | — |
| `services/save_service.py:43` | link input | `_LINK_RE` | Parses Telegram link for `save_by_link` | Required | — |
| `ai/preparation_policy.py:300-372` | task instruction | exact/max character patterns (e.g. Persian/English "at most N characters") | Derives deterministic content policy (validation of generated content, not commands) | Required (fail-closed policy) | Data-driven only if instruction grammar changed |
| `ai/semantic_delete.py` | deletion query | `^(\d+)(word|words)$`, `^(.+?)(کلمه)...$`, diacritic normalization | Structural deletion predicates | Required (deterministic, tested) | Structured-AI path only as architecture evolution |

**Non-command regex (out of scope but verified present):** secret redaction (`web_search_service.py` Bearer, `ai/discovery.py:71`, `model_tester.py:51-56`), output formatting (`tools/delivery.py`, `helper/font_style.py`), chat-title whitespace normalization (`chat_resolution.py:42-43`), language-tag validation (`ai_stt_settings.py`), provenance digits/links (`task_contract.py`), `providers/you_search.py`.

**Verified regex-free (grep-confirmed zero matches):** `task_wizard.py` (str.split/int/strptime), `bot/handlers/taskloom.py`, `bot/handlers/todo.py`, `bot/handlers/tasks.py` (`.task` is a string prefix, not regex), `ai/task_management.py`, `ai/tools/todo_tools.py`, `ai/tools/executor.py`, `ai/config_store.py` (`match_trigger` is case-insensitive `==`).

## 3. User Command Flow Analysis

```
Telegram outgoing message
  → is_owner gate (no regex)
  → handler selection:  Menu → misc.py pattern ^Menu$  (the ONLY regex command match)
                        AI text → ai_unified.py (NO pattern; fires on all outgoing)
                        .task (legacy dot-command) → tasks.py string prefix (NO regex)
  → normalization: raw text only; Glass UI decorative font never affects the Menu match
  → AI/router: first-word trigger via match_trigger (equality, no regex)
               → dispatcher
  → tool selection: native provider tool_calls OR _apply_structured_action fallback (§4)
  → ToolRegistry.get(tool_name) (executor.py:264)
  → ToolExecutor.execute_calls → tool.execute()
  → service layer (save/retrieve/delete/bio/username/todo services)
  → db/client.py (Supabase or in-memory fallback)
```

Regex participates at exactly **one** user-command point (`^Menu$`) and at **argument/artifact** points downstream (save-code shape checks, link parsing). Command *routing* for everything else is equality or semantics — not regex.

## 4. AI Tool Invocation Analysis

Three invocation paths exist — a **designed mixture**, not an accident:

- **(A) Native structured tool calls (primary/authoritative):** providers emit real `tool_calls`/`functionCall` (OpenAI format; Gemini translates to `functionDeclarations`) → `dispatcher` → `ToolExecutor.execute_calls`. Task/todo management is deliberately routed only this way (actions.py ~2660 comment: "task management is routed semantically rather than by per-phrase vocabulary").
- **(B) Deterministic token/vocabulary fallback:** when the provider returns prose only (`tools_allowed and response.success and not response.tool_calls`), `_apply_structured_action` (~1928) runs `parse_command_intent(request.user_message, ...)` **first** (authoritative for the narrow save/deep_save/delete/send vocabulary; built on token frozensets, not regex), then `parse_action_text(text)` for model-JSON contracts. Scheduling requests additionally pre-build a `deterministic_task_candidate` in `_build_tool_context` (1275).
- **(C) Model-JSON parsing:** `parse_action_text` → `validate_action` → `resolve_tool_calls` — structured output validated against the action contract.

**Authoritative:** native tool calls; the deterministic parser is authoritative only for the narrow command vocabulary when the model produces prose.

**Critical compliance fact:** paths A, B, and C all terminate at the **same** `ToolRegistry.get` → `ToolExecutor.execute_calls` boundary (`executor.py` is the sole caller of `tool.execute()`; unknown tool → not found; malformed args rejected). **No regex constructs or executes a tool call outside this boundary → no architectural violation.**

The only theoretical concern is ambiguity *within* the fallback parser (false-positive command vocabulary), which is bounded by its small token sets and the exclusion of task/todo vocabulary from it.

## 5. Todo Creation Flow Analysis

Three routes, none regex-parsing commands:

1. **Glass UI:** Menu → todo panel → structured `input:*` callbacks → `TodoService` (no regex anywhere in `todo.py`).
2. **AI native tool call:** provider emits `todo_add` → `validate_action` → `TodoAddTool` → `TodoService` (no regex).
3. **Scheduling create_task:** completeness gate (`tools/task.py` ~195-235; one clock regex at 208) → incomplete requests route to the Taskloom wizard (`open_taskloom_wizard`) → `TaskInterpreter` (`_JSON_BLOCK_RE` extracts *model* JSON) → `TaskCandidate` (`_COMPOUND_KEY_RE` normalizes keys) → `TaskCreationService` → `task_repository`.

### Live-test failure ("No active todos" + "Memory fallback — a local resource error prevented the durable store from being reached") — root cause, not assumed:

- `"No active todos."` is rendered by `bot/handlers/todo.py:124` when the `active` list is empty.
- The fallback string is `FALLBACK_RESOURCE_NOTE` (`ai/task_management_interface.py:27-31`), selected by `fallback_note(reason)` when `reason == FALLBACK_REASON_LOCAL_RESOURCE`.
- Root: `ai/database/task_repository.py` — `_is_local_resource_failure` (213, walks `__cause__`/`__context__` chains; httpx/httpcore wrap OSError) → `_classify_degradation` (198) → `_mark_fallback` (633) sets `_fallback_active` with a 5.0s `LocalResourceCooldown`. The durable read failed with a **local resource error** (socket/resource exhaustion class) → in-memory fallback served (empty) → panel truthfully shows "No active todos" **plus** the fallback note, because a degraded read is not an authoritative empty list.
- Creation succeeded through the fallback with a truthful `durable: False` marker (`tools/task.py:715-740`).

**Conclusion: regex parsing contributes NOTHING to this failure.** It is a DB-access/degradation-path event; the note machinery worked as designed and told the truth.

## 6. Regex That Can Be Removed

- **Clock anchor regexes** — `ai/actions.py:2536-2538` (3 patterns) and `ai/tools/task.py:208` (1). Digits survive `_tokenize`, so a token-adjacency digit scan (`12` `:` `30` / `ساعت` + digit) can replace them. Behavior-equivalent replacement is testable against `tests/test_19_ai_actions.py` and the NL-creation suites.
- **`_SAVE_CODE_RE` (`ai/actions.py:768`)** — redundant with `_SAVE_CODE_TOKEN_RE` (2220); consolidate to one shape definition. Cleanup, not a behavior fix.

No regex is *harmful*; removal is optional hygiene.

## 7. Regex That Must Stay

- Canonical save-code shapes (`actions.py:2220/2221/2228`, `retrieve_service.py:388`) — fixed-format artifact matching is exactly what regex is for; removal invites false-positive tool calls on arbitrary words.
- Link extraction (`actions.py:165`, `save_service.py:43`).
- Fenced-JSON extractors (`task_interpreter.py:21`, `task_execution.py:71`) — structured-output recovery from prose-wrapped model replies.
- `_COMPOUND_KEY_RE` (`task_candidate.py:56`) — schema normalization of structured keys.
- `_UNKNOWN_COLUMN_RE` (`task_repository.py:131`) — PGRST204 resilience.
- Provenance digit extraction (`task_contract.py`), preparation-policy patterns (fail-closed validation), semantic-delete predicates (deterministic, tested), and all security redaction.
- `^Menu$` — required today (see §8 for the optional migration).

## 8. Recommended Migration Strategy

**Nothing is mandatory** — the architecture already satisfies "AI reasons and proposes structured actions; runtime validates and executes." If zero command regex is a hard goal:

1. Replace `pattern=r"^Menu$"` with a pattern-less outgoing handler + exact-match guard (`text == "Menu"`), mirroring `match_trigger`'s equality style. Regression risk: near zero.
2. Replace the 4 clock regexes with token-based digit-adjacency checks reusing `_tokenize`; pin behavior with the existing action/NL test suites.
3. Consolidate `_SAVE_CODE_RE` into `_SAVE_CODE_TOKEN_RE`.
4. Leave `parse_command_intent`'s token vocabulary as-is (it is data, not regex); schema validation already lives in `validate_action`; no ToolRegistry/ToolExecutor change is needed or desirable — the boundary is already regex-free and single.

## 9. Files Examined

`AGENTS.md`, `IMPLEMENTATION_REPORT.md`, `INVESTIGATION.md`, `DATABASE_ARCHITECTURE.md`; `backend/bot/handlers/` (misc, ai_unified, tasks, task_events, taskloom, todo, ai_stt_settings, router); `backend/ai/` (actions, engine/dispatcher, tools/executor, tools/task, tools/todo_tools, task_interpreter, task_execution, task_candidate, task_contract, task_wizard, task_management, task_management_interface, database/task_repository, preparation_policy, semantic_delete, discovery, config_store, model_tester); `backend/services/` (save_service, retrieve_service, web_search_service); `backend/helper/font_style.py`.

Test suites consulted (behavior pins, not modified): `tests/test_19_ai_actions.py`, `test_task_nl_creation.py`, `test_task_nl_interval_creation.py`, `test_task_fallback_classification.py`, `test_task_fallback_cooldown.py`, `test_task_list_consistency.py`, `test_task_list_reliability.py`, `test_task_hardening.py`, `test_tool_health_audit.py`.

## 10. No-Code-Change Conclusion

This document records an audit only. At audit time `git status` showed a clean tree (HEAD `5c47f66` == `origin/main`); no handler, tool, executor, registry, service, scheduler, database, schema, prompt, or test file was modified, and no regex was added or removed. The Todo failure fix and any regex migration belong to a separate implementation task.
