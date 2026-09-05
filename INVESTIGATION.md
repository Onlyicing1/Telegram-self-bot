# INVESTIGATION

## INVESTIGATION METADATA

| Field | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Current HEAD | `347ac1dae268b3ad3390a8e1ded8137c9fda0566` (`docs: record verified delivery commit sha in report`) |
| Investigation date | 2026-09-04 (connectivity audit); updated 2026-09-05 (confirmation round-trip, runtime switching, state consistency, follow-up findings, git delivery verification) |
| Scope | AI → existing Self Bot capabilities/tools → actual execution path (AI Tool Connectivity) |
| Status | **INVESTIGATION ONLY** — no production code, tests, schema, or configuration modified |

Source priority used: current production source → current tests → current repository/schema usage → current INVESTIGATION.md (previous revision) → IMPLEMENTATION_REPORT.md (historical reference). Where documentation and code disagree, **code wins**; documentation drift is recorded as a finding.

---

## 1. EXECUTIVE SUMMARY

The question investigated: *which existing Self Bot capabilities can the AI actually use today, through the real production execution path?*

**Answer (current source): all 36 registered tools are genuinely reachable and executable by the AI end-to-end** through the intended architecture (AI entry → provider/Dispatcher → ToolRegistry → ToolExecutor → service → TelegramAPI/Supabase → ToolResult → AI response). Every tool is exposed to every chat provider as an OpenAI-format tool definition, all chat providers advertise native tool-call support, and `ToolExecutor` auto-executes READ_ONLY / READ_WRITE / DANGEROUS tools. A 19-tool subset is additionally reachable deterministically (fast path, `parse_command_intent`) and through validated JSON structured actions (`parse_action_text` → `resolve_tool_calls`).

`settings_set` (ADMIN_ONLY) was the original exception: the Dispatcher had no confirmation round-trip, so the executor correctly returned `needs_confirmation` and nothing completed it. That gap is now **FIXED** — a bounded owner-confirmation round-trip (`backend/ai/confirmation.py` + Dispatcher `_gate_confirmation_results` / `_try_consume_confirmation` + `ToolExecutor.execute_confirmed`, commit `c5d29f7`) makes `settings_set` executable through AI, but ONLY after the owner explicitly confirms the frozen action (120 s TTL, single-use, owner+chat scoped). This was a feature gap, not a security gap; the boundary is now implemented.

The live failure `❌ Unsupported action: send` is **source-proven**: `"send"` is not a registered tool name (the canonical registered capability is `send_message`); the deterministic command parser (`backend/ai/actions.py`) emitted `ActionParseResult(kind=KIND_UNSUPPORTED, action="send")` for any send-vocabulary match that was not a plain text-write, and the Dispatcher rendered it as `❌ Unsupported action: send` (`dispatcher.py:1200`, `:1438`). Commit `1285cdf` (already merged) routes event-intent and future-clock requests past the send/save vocabulary so the provider selects `create_task` semantically; the residual `KIND_UNSUPPORTED action="send"` branch is intentional (recipient/reference/forward sends must never let the model choose a destination). In the **task-creation path**, `"send"` IS a tolerated model-facing alias — `task_candidate._SEND_ACTION_ALIASES` canonicalizes it to the registered `send_message` with bounded text only.

### Status counts (all 36 registered tools classified)

- **REAL_CONNECTED: 36** (35 unconditional + `web_search`, which is path-complete but honestly fails without `YDC_API_KEY`; `settings_set` included — ADMIN_ONLY, executes only through the owner-confirmation round-trip)
- **REGISTERED_BUT_NOT_REACHABLE: 0**
- **PROVIDER_REACHABLE_BUT_NOT_EXECUTABLE: 0** (`settings_set` gap fixed in `c5d29f7`)
- **IMPLEMENTED_NOT_REGISTERED: 0** (among tools; see §13 for implemented-but-unregistered capabilities)
- **IMPLEMENTED_BUT_MISSING_AI_ENTRY: 2 groups** (AI memory write path — zero production callers; `TelegramAPI` facade methods `edit_message`, `search_messages`, `download_media` — no tool adapters)
- **BLOCKED_BY_SECURITY: 0** — the ToolRegistry → ToolExecutor boundary is intact; no arbitrary RPC/SQL/shell/HTTP path exists
- **CONFIRMATION_GATED (by design): 1** (`settings_set` — ADMIN_ONLY; executes only via `execute_confirmed` after explicit owner approval)
- **PANEL_OR_COMMAND_ONLY (by design): 4** (Ghost Seen v2, model tester/discovery, Taskloom panel, web dashboard API; AI provider/model switching and trigger-word config are now ALSO AI-reachable through confirmation-gated `settings_set`)
- **BROKEN_IMPLEMENTATION: 0**
- **UNKNOWN: 3** (per-provider live tool-call behavior; per-provider key configuration — secrets unreadable by design; live Telegram/Supabase behavior of the event-trigger path)

**Live Telegram verification: NOT PERFORMED** in this workspace (no credentials/runtime). All connectivity claims below are source-verified and in-process-test-verified; nothing here claims live verification.

---

## 2. CURRENT AI EXECUTION ARCHITECTURE

Source-verified path (actual class/function/module names):

```
Natural-language outgoing message
  → backend/bot/handlers/ai_unified.py   (canonical AI activation handler, is_owner gate)
  → backend/ai/engine/engine.py          (Engine.process_request, bounded wait_for)
  → backend/ai/engine/dispatcher.py      (Dispatcher._dispatch / _execute_ai)
       ├─ request.allow_tools (backend/ai/session/request.py, default True)
       │    → dispatcher._build_tool_definitions()  (OpenAI-format definitions, dispatcher.py:1548)
       │    → provider.chat(..., tools=definitions)  (all chat providers support native tool calls)
       ├─ deterministic fast path: actions.parse_command_intent(request.user_message, has_reply)
       │    (dispatcher.py:1085/1187/1396) → ActionParseResult(KIND_EXECUTABLE) with tool_calls
       ├─ JSON action path: actions.parse_action_text(text) (dispatcher.py:1400)
       │    → validate_action() → resolve_tool_calls() → registered tool names
       └─ provider native tool calls: parse_tool_calls() → validated against registry
  → backend/ai/tools/registry.py         (ToolRegistry.get(); built once by create_default_registry(ctx))
  → backend/ai/tools/executor.py         (ToolExecutor — SOLE caller of tool.execute(); never raises)
  → registered Tool instance             (backend/ai/tools/*.py, 36 tools)
  → service layer                        (backend/services/*.py)
  → backend/telegram_api/TelegramAPI     (typed RPC facade over the self client — only Telegram boundary)
  → backend/db/client.py                 (Supabase singleton + in-memory fallback)
  → ToolResult                           (success/error/needs_confirmation → dispatcher → AI response)
```

Key invariants (verified in source):

- **ToolRegistry** (`backend/ai/tools/registry.py`): constructed once at startup by `RuntimeSupervisor._wire_ai_tools()` (`supervisor.py:245-260`) and injected via `engine.attach_tools(...)`; duplicate names raise `ValueError`. A second wiring for the task path builds the same registry (`supervisor.py:325-365` region, coordinator receives the same instance).
- **ToolExecutor** (`backend/ai/tools/executor.py`): `MAX_TOOLS_PER_TURN=5`, `TOOL_TIMEOUT_SECONDS=10` with per-tool overrides (`save` 45s long-running, `delete` 30s, `create_task` 45s, `send_message` 30s). `_is_auto_executable()` auto-executes `READ_ONLY`, `READ_WRITE`, `DANGEROUS` (owner message = authorization in this single-owner self-bot); `ADMIN_ONLY` / `CONFIRMATION_REQUIRED` return `needs_confirmation` and never execute. Unknown tool name → `not_found` error result. Permission levels live in `backend/ai/tools/base.py` (`PermissionLevel`).
- **Security boundary**: AI proposes/selects registered capabilities; ToolRegistry validates; ToolExecutor executes; the Self Bot owns all Telegram RPC. The model can never emit a numeric chat/sender id into task triggers (`validate_trigger_spec` rejects ids), never inject a destination into `send_message` (`_canonicalize_action` strips everything but bounded text), and never reach arbitrary Telethon/SQL/shell/HTTP.

---

## 3. COMPLETE AI TOOL CONNECTIVITY MATRIX

### Invariant fields (identical for every tool below — verified once)

- **Registry**: `create_default_registry(context)` in `backend/ai/tools/registry.py` — exactly 36 `registry.register(...)` calls; duplicate names raise. Wired at startup (engine + task paths).
- **Dispatcher exposure**: `Dispatcher._build_tool_definitions()` builds OpenAI-format `{"type":"function","function":{...}}` definitions from each tool's `name/description/parameters`; gated by `request.allow_tools`.
- **Provider exposure**: all chat providers receive the definitions; Gemini translates to `functionDeclarations`; every chat provider advertises native tool-call support.
- **ToolExecutor**: sole caller of `tool.execute()`; auto-executes per permission level (see §2); per-tool timeout overrides noted per tool.
- **Result propagation**: `ToolResult.success/error/needs_confirmation` returned to the dispatcher, rendered into the AI response; execution history recorded via `ToolHistoryRepository` when available.
- **Tests**: registry-exposure tests (`tests/test_capability_exposure_tools.py`), tool-call chain tests (`tests/test_10_tool_calls.py`), action tests (`tests/test_19_ai_actions.py`), tool-health audit (`tests/test_tool_health_audit.py`), plus per-domain suites (save/delete/bio/username/retrieve/organize/database/task).
- **Live verification**: **NOT PERFORMED** for all rows (no live Telegram/Supabase runtime in this workspace).

### Tool-by-tool matrix

#### save — REAL_CONNECTED
- Purpose: Deep Save (download → re-upload to Saved Messages + DB record).
- Category: READ-WRITE / Telegram side effect (Saved Messages).
- Implementation: `SaveTool` (`backend/ai/tools/save.py`); underlying `save_service.execute_save` (`backend/services/save_service.py`).
- Permission: READ_WRITE; long-running (`long_running=True` — exempt from the 10 s tool timeout; bounded by the 60 s request-level `wait_for` in `ai_unified`, see A-4).
- Arguments: `{}` — target resolved from trusted runtime reply context (`extra.reply_msg` → real Telethon message via the injected client); model cannot invent chat ids or pass arguments.
- Failure point: none. Evidence: tool-call chain + action tests pass; registry + schema exposure proven.

#### save_by_link — REAL_CONNECTED
- Purpose: Deep Save a Telegram message link (preserves URL exactly).
- Implementation: `SaveByLinkTool` (`backend/ai/tools/save.py`); `save_service`.
- Permission: READ_WRITE. Arguments: `{link}`.
- Deterministic path: `parse_command_intent` (save + link detection, `actions.py`).
- Failure point: none.

#### delete — REAL_CONNECTED
- Purpose: Delete N recent messages (reply-from / recent / count modes).
- Implementation: `DeleteTool` (`backend/ai/tools/delete.py`); `delete_service`.
- Permission: DANGEROUS (auto-executed — owner message is authorization; deterministic argument validation); executor timeout 30s.
- Arguments: `{count, mode, ...}` — bounded, validated.
- Deterministic path: Persian/English delete vocabulary in `parse_command_intent` (incl. "تا ساعت ۶ … پاک کن" until-time and "ساعت ۹ دیروز" yesterday semantics pinned by tests).
- Failure point: none.

#### delete_replied — REAL_CONNECTED
- Purpose: Delete the replied-to message.
- Implementation: `DeleteRepliedTool` (`backend/ai/tools/delete.py`); `delete_service`.
- Permission: DANGEROUS; 30s timeout. Arguments: `{}` (reply binding only).
- Deterministic path: delete + reply detection.

#### delete_by_id — REAL_CONNECTED
- Purpose: Delete all outgoing messages from a given message ID onward in the current chat.
- Implementation: `DeleteByIdTool` (`backend/ai/tools/delete.py`); `delete_service.do_del_id_counts`.
- Permission: DANGEROUS; 30s timeout. Arguments: `{message_id}` (positive int; required).
- Deterministic path: delete + numeric message-id extraction.

#### delete_message_by_id — REAL_CONNECTED
- Purpose: Delete exactly one outgoing message by its message id in the current chat (single target, never a range).
- Implementation: `DeleteMessageByIdTool` (`backend/ai/tools/delete.py`); `delete_service.delete_verified_self_messages` (fetch via `rpc_await` 5 s, outgoing-only enforced).
- Permission: DANGEROUS. Arguments: `{message_id}` — chat always resolved from trusted context.
- Deterministic path: delete + numeric message-id extraction.

#### list_recent_messages — REAL_CONNECTED
- Purpose: List a bounded window (≤100) of REAL recent Telegram messages in the current chat (all participants, oldest→newest) with ids/preview — context for semantic deletes; the AI never invents ids.
- Implementation: `ListRecentMessagesTool` (`backend/ai/tools/semantic.py`); real Telethon `iter_messages` (10 s executor bound).
- Permission: READ_ONLY. Arguments: `{limit}` (1..100, default 50).
- Deterministic path: `_STATUS_ACTIONS` includes `list_recent_messages`.

#### delete_messages_by_ids — REAL_CONNECTED
- Purpose: Bulk-delete a bounded list (≤100) of outgoing message ids; every id is re-fetched and re-validated (outgoing + current chat) before deletion.
- Implementation: `DeleteMessagesByIdsTool` (`backend/ai/tools/semantic.py`); `delete_service.delete_verified_self_messages`.
- Permission: DANGEROUS. Arguments: `{message_ids: [...]}` (positive ints, deduped, capped).
- Note: the description tells the model ids “MUST have been returned by list_recent_messages in this turn” — that turn-scoped provenance is prompt guidance only; the enforced safety property is re-fetch + outgoing-only (see A-1).

#### bio_set_template / bio_set_text / bio_set_mood / bio_on / bio_off — REAL_CONNECTED (×5)
- Purpose: Profile Bio control (template/mood/text enable/disable).
- Implementation: `BioSetTemplateTool` etc. (`backend/ai/tools/bio.py`); `bio_service` → `ProfileEngine`.
- Permission: READ_WRITE. Arguments: per-tool bounded (`{template}`, `{text}`, `{mood}`, `{}`).
- Deterministic path: Persian/English bio verbs in `parse_command_intent` (set/on/off/show/get) + JSON actions.
- Telegram boundary: `update_profile` via `TelegramAPI`.

#### bio_show / bio_get — REAL_CONNECTED (×2)
- Purpose: Read current bio state / profile bio.
- Implementation: `BioShowTool` / `BioGetTool` (`backend/ai/tools/bio.py`).
- Permission: READ_ONLY. Deterministic path: `_STATUS_ACTIONS` (`bio_status`, `get_bio`).

#### username_set_template / username_set_text / username_set_mood / username_on / username_off / username_show — REAL_CONNECTED (×6)
- Purpose: First-name (Username) profile control; mirrors Bio set.
- Implementation: `Username*Tool` (`backend/ai/tools/username.py`); `username_service` → shared `ProfileEngine`.
- Permission: READ_WRITE (show: READ_ONLY). Deterministic path: username verbs + `_STATUS_ACTIONS` (`username_status`).

#### search — REAL_CONNECTED
- Purpose: Search saved items (Discover/find).
- Implementation: `SearchTool` (`backend/ai/tools/retrieve.py`); `discover_service.do_find`.
- Permission: READ_ONLY. Arguments: `{query}` (required).
- Deterministic path: `_STATUS_ACTIONS` (`search_saved_items`).

#### list_saves — REAL_CONNECTED
- Purpose: List recent saved items.
- Implementation: `ListSavesTool` (`backend/ai/tools/retrieve.py`).
- Permission: READ_ONLY. Deterministic path: `_STATUS_ACTIONS` (`list_saved_items`).

#### database_stats — REAL_CONNECTED
- Purpose: Database maintenance/status read.
- Implementation: `DatabaseStatsTool` (`backend/ai/tools/database.py`); `database_service`.
- Permission: READ_ONLY. Deterministic path: `_STATUS_ACTIONS` + DB-word detection (`_DB_WORDS`/`_DB_STATUS_WORDS`).

#### account_show — REAL_CONNECTED
- Purpose: Show owner account/profile info.
- Implementation: `AccountShowTool` (`backend/ai/tools/account.py`).
- Permission: READ_ONLY. Deterministic path: account-word detection (`_ACCOUNT_WORDS` + `_STATUS_WORDS`).

#### settings_get — REAL_CONNECTED
- Purpose: Read AI/runtime settings.
- Implementation: `SettingsGetTool` (`backend/ai/tools/settings.py`); `settings_service` / `config_store`.
- Permission: READ_ONLY. Arguments: `{key}` (required; AI keys read `config_store`, other keys read `settings_service`).

#### settings_set — REAL_CONNECTED (confirmation-gated)
- Purpose: Change settings (AI keys: provider, model, temperature, max_tokens, system_prompt, history_budget, triggers → `config_store`/`ai_config`; other keys → Glass panel settings → `settings_service`/`panel_settings`).
- Implementation: `SettingsSetTool` (`backend/ai/tools/settings.py`); `config_store` for AI keys, `settings_service` for panel keys.
- Permission: **ADMIN_ONLY** (`permission_level`, `safe=False`) — executes ONLY through the owner-confirmation round-trip.
- Confirmation round-trip (fixed in `c5d29f7`): executor returns `needs_confirmation` → Dispatcher `_gate_confirmation_results` stores a `PendingConfirmation` (frozen tool name + arguments, 120 s TTL, one per owner+chat) → confirmation prompt delivered → owner replies «تأیید»/«بله»/“yes” → Dispatcher `_try_consume_confirmation` takes the entry (single-use) and re-issues the ORIGINAL stored call through `ToolExecutor.execute_confirmed` — the only gate bypass, never model-invented.
- Provider/model keys additionally push into the live runtime via `engine.apply_runtime_selection` → `ProviderManager.apply_selection` (same authoritative path as web/glass writers); unregistered providers are rejected before persisting.
- Known defect (see RC-5): unknown/non-column keys are reported as success without persistence.
- Evidence: `backend/ai/confirmation.py` (store + exact-phrase recognition), `dispatcher.py` (`_gate_confirmation_results`/`_try_consume_confirmation`), `executor.py` (`execute_confirmed`); AI-path tests in `tests/test_confirmation_roundtrip.py` (14 dispatcher-level tests).

#### organize_list / organize_clean — REAL_CONNECTED (×2)
- Purpose: Organization suggestions / cleanup execution.
- Implementation: `OrganizeListTool` / `OrganizeCleanTool` (`backend/ai/tools/organize.py`); `organize_service`.
- Permission: READ_ONLY / DANGEROUS.

#### web_search — REAL_CONNECTED (key-dependent)
- Purpose: Web search via the `you` provider (You.com Search API) — retrieval capability, never a chat engine.
- Implementation: `WebSearchTool` (`backend/ai/tools/websearch.py`); `web_search_service.do_web_search` → `ProviderManager.web_search`.
- Permission: READ_ONLY. Arguments: `{query, count? (1..100, default 10), freshness? (day/week/month/year), include_domains?}`.
- **Key dependency**: without `YDC_API_KEY` the tool returns an honest failure (tested: `test_web_search_failure_is_honest_without_configuration`); the tool is always registered and exposed regardless. Env key unreadable in this workspace → live behavior UNKNOWN.
- Minor: the concrete failure reason is swallowed into a generic `❌ Web search failed.` (see A-3).

#### create_task — REAL_CONNECTED
- Purpose: Durable automation/task creation (time or event trigger + 1–5 actions).
- Implementation: `CreateTaskTool` (`backend/ai/tools/task.py`); `TaskInterpreter` (`task_interpreter.py`) → `TaskCandidate` validation (`task_candidate.py`) → `TaskCreationService` (`task_creation.py`) → repository (`ai_tasks`).
- Permission: READ_WRITE; long-running (45s timeout — interpreter + name resolution).
- Arguments: `{request}` (natural language; the interpreter fabricates nothing when ambiguous — returns clarification).
- Deterministic path: `_is_scheduling_intent(words)` → `ActionParseResult(action="create_task", tool_calls=[create_task])`; event-intent and future-clock requests now fall through as conversational so the provider selects `create_task` semantically (fix `1285cdf`).
- Task-action canonicalization: `_SEND_ACTION_ALIASES = {"send","send_message","write_message","send_text"}` → registered `send_message` with bounded text only; every other action name persists as-is and is **re-validated against the registry at execution time** (unregistered → fail closed).

#### task_list / task_inspect — REAL_CONNECTED (×2)
- Purpose: List / inspect durable tasks.
- Implementation: `TaskListTool` / `TaskInspectTool` (`backend/ai/tools/task_management_tools.py`); `TaskManagementService` (`task_management.py`).
- Permission: READ_ONLY. Deterministic path: `_STATUS_ACTIONS` (`task_list`); task-id parsing for inspect.

#### task_transition — REAL_CONNECTED
- Purpose: Pause/activate/complete/delete a task (CAS-versioned lifecycle).
- Implementation: `TaskTransitionTool` (`backend/ai/tools/task_management_tools.py`); `TaskManagementService.set_status` (owner-scoped, `expected_version` CAS).
- Permission: READ_WRITE. Arguments: `{task_id, action (active/paused/completed/deleted), expected_version}` — all required; stale version fails honestly, nothing changes.

#### retrieve_save — REAL_CONNECTED
- Purpose: Re-send a saved item (by save code) to a chat — the only re-send path.
- Implementation: `RetrieveSaveTool` (`backend/ai/tools/retrieve_save.py`); `retrieve_service.do_retrieve`.
- Permission: READ_WRITE (Telegram side effect — re-sends to the owner's own chat resolved from trusted context).
- Arguments: `{save_code}`.
- Note: previously documented as IMPLEMENTED_NOT_REGISTERED; current source registers it — the old INVESTIGATION.md was stale (see §8, docs drift).

#### send_message — REAL_CONNECTED
- Purpose: Send a text message from the owner's account (the ONLY model-reachable Telegram write capability).
- Implementation: `SendMessageTool` (`backend/ai/tools/message.py`); executor timeout 30s.
- Permission: READ_WRITE.
- Arguments: **`{text}` only** (bounded ≤4096 chars) — the destination is ALWAYS resolved from trusted runtime context (`extra.chat_id`, or owner's own Saved Messages chat), never from arguments; the model can never supply a chat id or destination. Write-text deterministic path (`_write_text_present`, "بنویس سلام") maps to this tool with `action="send"`.
- Canonical registered name: **`send_message`** — see §7 for the `"send"` mismatch.

### Classifications recap

| Status | Count | Tools |
|---|---|---|
| REAL_CONNECTED | 36 | all above (incl. `web_search` key-dependent and `settings_set` confirmation-gated) |
| PROVIDER_REACHABLE_BUT_NOT_EXECUTABLE | 0 | — |
| REGISTERED_BUT_NOT_REACHABLE | 0 | — |
| IMPLEMENTED_NOT_REGISTERED | 0 | (registered set is complete) |
| IMPLEMENTED_BUT_MISSING_AI_ENTRY | 2 groups | AI memory write path; `edit_message` / `search_messages` / `download_media` facade methods |
| BLOCKED_BY_SECURITY | 0 | — |
| CONFIRMATION_GATED | 1 | `settings_set` (executes only via `execute_confirmed`) |
| PANEL_OR_COMMAND_ONLY | 4 | Ghost Seen v2, model tester/discovery, Taskloom panel, web dashboard API |
| BROKEN_IMPLEMENTATION | 0 | — |
| UNKNOWN | 3 | per-provider live tool-call behavior; per-provider env keys; live event-trigger/Supabase behavior |

---

## 4. TELEGRAM SIDE-EFFECT TOOL AUDIT

| Capability | Tool name | Model-reachable? | Boundary |
|---|---|---|---|
| Send text | `send_message` | YES (only send path) | `TelegramAPI.send_message`; text-only args; destination from trusted context |
| Edit message | — (facade `edit_message`) | NO — no tool adapter | intentionally absent; panel-only today |
| Delete messages | `delete`, `delete_replied`, `delete_by_id`, `delete_message_by_id`, `delete_messages_by_ids` | YES (DANGEROUS, auto-executed) | `delete_service` → `TelegramAPI.delete_messages`; bounded/validated args; owner-scoped |
| Save (Deep) | `save`, `save_by_link` | YES | `save_service.execute_save` — download → NEW Saved Messages message (never forward) |
| Retrieve/re-send | `retrieve_save` | YES | `retrieve_service.do_retrieve` re-sends a saved asset to owner's chat |
| Search/recent | `search`, `list_saves`, `list_recent_messages` | YES | read-only over DB / chats |
| Reactions | — | NO | no reaction tool exists (not implemented as a tool) |
| Profile bio | `bio_*` (7) | YES | `ProfileEngine` → `TelegramAPI` profile update |
| Username (first_name) | `username_*` (6) | YES | shared `ProfileEngine` (about/first_name distinction preserved) |
| Other mutations | — | NO | Ghost Seen v2 (read-receipt spoof) is panel-only by design |

**No model-reachable path can pick an arbitrary Telegram destination or method.** All destinations resolve from trusted runtime context (`resolve_chat_name`, reply binding, owner chat) or are rejected.

---

## 5. TASK / EVENT AUTOMATION TOOL AUDIT

Separated claims (all source-verified; live execution NOT verified):

### A. AI can create a task — YES (proven)
`create_task` is registered, schema-exposed, deterministic (scheduling intent) and semantic (provider). `TaskInterpreter` returns JSON null / clarification when ambiguous; ordinary conversation never auto-creates a task (creation only via the explicit tool boundary).

### B. AI can create a task with a particular action — PARTIAL (proven)
- **`send_message`-family actions**: YES — `_SEND_ACTION_ALIASES` canonicalize `send`/`send_message`/`write_message`/`send_text` to the registered tool with bounded text; destination is never accepted from the model.
- **Any other registered tool name** (`save`, `delete*`, `bio_*`, `username_*`, `retrieve_save`, `search`, `task_*`, …): YES as far as the candidate validator is concerned — `_canonicalize_action` passes non-send names through unchanged; the stored name must match a registered tool **exactly**, or execution fails closed.
- Model near-miss names (e.g. `send` with a `chat` argument, or `delete_message` instead of `delete_message_by_id`) fail at validation/execution — fail-closed, never a security hole.

### C. Scheduler/event dispatcher can execute that action — YES (proven)
- Time schedules (`once`/`interval`/`daily`/`weekly`): `TaskScheduler` (`task_scheduler.py`) due-poll → `TaskExecutionCoordinator.execute` (`task_execution.py`).
- Event schedules (`event`): `TaskEventDispatcher.handle_event` (`task_event_dispatcher.py`) — wired to the Telethon update path via `backend/bot/handlers/task_events.py` (registered in `router.py`, configured in `supervisor.py:374-376`). Deterministic `event_trigger_matches` (`task_trigger.py`); occurrence key `"<task_id>:ev:<chat_id>:<message_id>"` dedups redelivery; bounded (20 tasks listed, ≤5 executions per message); never raises into the Telegram event path.
- Both paths share the same repository singleton, coordinator, and notifier — no parallel authorities.

### D. ToolExecutor can execute that action — YES (proven)
Stored action names are re-validated against `ToolRegistry` at execution time; `TaskExecutionCoordinator` routes through the same `ToolExecutor` as live AI turns (auto-execute rules identical).

### E. Real Telegram/Supabase side effect occurs — SOURCE-VERIFIED, LIVE NOT VERIFIED
Side effects go through the same service layer / `TelegramAPI` / `db` boundary as interactive execution. **No live Telegram run has been performed** in this workspace (IMPLEMENTATION_REPORT.md agrees: "live Telegram/Supabase verification NOT performed").

### Result delivery
`notify_on_outcome` / `deliver_result` default **false** — scheduled/event execution is silent by default; outcomes live in `ai_task_occurrences` + structured `TASK_EVENT_TRACE` logs. Opt-in result delivery uses the same notifier boundary.

---

## 6. NATURAL-LANGUAGE TOOL SELECTION

The architecture uses **all four** mechanisms — and is NOT regex/keyword-routing by design:

1. **Provider schemas + native tool calls** (primary): `Dispatcher._build_tool_definitions()` exposes all 36 tools to every chat provider; providers emit native `tool_calls` that are parsed (`parse_tool_calls`) and validated against the registry.
2. **Validated JSON actions**: `parse_action_text` (`actions.py`) extracts a JSON action from a model response, `validate_action` bounds it, `resolve_tool_calls` maps it to registered tool names — unsupported actions never reach the executor.
3. **Deterministic local resolution** (fast path): `parse_command_intent` recognizes explicit imperative vocabularies (save/delete/send/bio/username/status/scheduling) only when a target/reply/count anchor is present; bare English verbs without anchors are treated as questions; "تا ساعت ۶ … پاک کن" (until-time) and "ساعت ۹ دیروز" keep delete semantics. This is a bounded command vocabulary, not a growing NL pattern list, and it precedes the provider only for explicit commands.
4. **Conversational fallback**: anything the deterministic layer cannot resolve falls through to the provider with the full tool set; the provider decides semantically. Event-intent ("وقتی X پیام داد…") and future-clock ("فردا ساعت 15:35…") requests fall through deliberately (fix `1285cdf`).

**Accidental execution**: conversation cannot trigger tools — tool calls require provider-selected tool calls, validated JSON actions, or explicit deterministic imperative verbs with anchors; `request.allow_tools` can disable exposure entirely. Task creation additionally requires the explicit `create_task` boundary (interpreter returns clarification on ambiguity).

---

## 7. CONFIRMED ROOT CAUSES

### RC-1 — `❌ Unsupported action: send` (source-proven, fixed in `1285cdf`)
- `"send"` is **not** a registered tool name. The registered capability is `send_message` (`SendMessageTool`, `backend/ai/tools/message.py`).
- Before `1285cdf`, `parse_command_intent` matched the send vocabulary (`_SEND_STEMS` Persian + `_EN_SEND` English) and returned `ActionParseResult(kind=KIND_UNSUPPORTED, action="send")` for every send-like request that was not a plain text-write (`actions.py` send branch) — the live requests "…نتیجه‌ش رو برام بفرست" (no writable text extracted) hit exactly this branch.
- Dispatcher rendered it as `❌ Unsupported action: {result.action}` (`dispatcher.py:1200`, `:1438`).
- The **task path never had this bug**: `task_candidate._SEND_ACTION_ALIASES` accepts `"send"` as a model alias and canonicalizes it to `send_message` with bounded text.
- Fix (merged, current HEAD): event-intent and future-clock requests fall through as conversational before the send/save vocabulary runs → the provider selects `create_task` semantically (no regex, no pattern-list task fabrication). Residual `KIND_UNSUPPORTED action="send"` remains only for recipient/reference/forward sends ("اینو برای علی بفرست") — intentional: the model must never choose a destination.

### RC-2 — `settings_set` cannot execute through AI (source-proven, FIXED in `c5d29f7`)
- `SettingsSetTool.permission_level == ADMIN_ONLY`; `ToolExecutor._is_auto_executable()` excludes it → `needs_confirmation` ToolResult; the Dispatcher originally had **no confirmation round-trip** (no re-issue path in `dispatcher.py` / `ai_unified.py`). First failure layer was: **J (ToolExecutor permission gate) → O (result propagation)**.
- Fix (merged, current HEAD): `backend/ai/confirmation.py` (`PendingConfirmationStore`, exact-phrase `is_explicit_confirmation`, 120 s TTL, owner+chat scoped, single-use `take()`); `Dispatcher._gate_confirmation_results` (stores the FIRST blocked call, never overwrites a live pending entry) and `_try_consume_confirmation` (runs before any provider round, re-issues the FROZEN stored call through `ToolExecutor.execute_confirmed`); `executor.execute_confirmed` is the only gate bypass and is reachable only via a consumed `PendingConfirmation` — never a model-invented call. Registry lookup, malformed-argument rejection, timeouts, history, and error containment remain identical on the confirmed path.
- Proof: `tests/test_confirmation_roundtrip.py` — 63 tests incl. 14 full dispatcher AI-path tests (cross-chat/cross-owner rejection, expiry fail-closed, ambiguous acknowledgement never executes, frozen arguments cannot be changed by the confirmation reply).

### RC-3 — Event request miscreated as interval task (source-proven, fixed in `1285cdf`)
- Live: "وقتی Bs Abolfazl بهم پیام داد…" became "هر ۵ دقیقه یک بار". Root cause: the deterministic parser diverted the request before the interpreter could see it (sender-resolution clarification reply was re-parsed and matched the scheduling/save vocabulary). Current guard routes `_is_event_intent` requests to the conversational path so the provider (with `create_task` + event guidance in `TaskInterpreter`) preserves event semantics; the interpreter still returns clarification when the sender is ambiguous.

### RC-4 — Documentation drift (source-proven, NOT fixed — this investigation documents it)
Previous INVESTIGATION.md classified `task_list`/`task_inspect`/`task_transition` and `retrieve_save` as IMPLEMENTED_NOT_REGISTERED and event automation as design-only; the current registry registers all of them and `task_events` is live-wired in `router.py`/`supervisor.py`. Tool count drift 32 → 36 (incl. `nararouter` provider per AGENTS.md §9, matching source).

### RC-5 — `settings_set` phantom success on unknown keys (source-proven + reproduced in-process, NOT fixed)
- `SettingsSetTool.execute` routes every non-AI key to `settings_service.set_setting(key, value)`, which has **no key allowlist**: an unknown key skips validation (`_VALIDATORS.get(key)` → None), `repo.update_field` fails (no such column in Supabase; no DB in the fallback), and the function then does `_cache[key] = value; return True` — a phantom key + reported success.
- Reproduced in-process (2026-09-05): `settings_set {key: "no_such_setting", value: "xyz"}` → `success=True, "Setting 'no_such_setting' updated to 'xyz'."`, while the subsequent `settings_get` returns `no_such_setting = None` (cache rebuilt from defaults) and nothing is persisted.
- Impact: the owner EXPLICITLY confirms (ADMIN_ONLY round-trip) a change that silently never happens; the AI is told it succeeded. Same defect class as the provider/model phantom-pair issue. Fix belongs at the service/tool boundary: reject unknown keys before write.

### RC-6 — DANGEROUS permission docstring drift (source-proven, NOT fixed — documentation only)
- `ToolExecutor._is_auto_executable()` auto-executes READ_ONLY / READ_WRITE / DANGEROUS (owner message = authorization; deterministic argument validation in the tools themselves) — this is the documented, tested, intended behavior (IMPLEMENTATION_REPORT.md §6).
- But `backend/ai/tools/base.py` PermissionLevel docstring (“DANGEROUS → AI must ask the owner first”) and the module docstrings of `delete.py` / `organize.py` (“the AI must ask the owner for confirmation before calling them”) still claim otherwise. No behavior change is proposed — only the docstrings are stale.

---

## 8. LIKELY RISKS

- **`settings_set` unknown-key phantom success (proven, RC-5)**: an owner-confirmed settings write can report success without persisting anything. Fix: reject unknown keys at the service/tool boundary + one regression test.
- **DANGEROUS docstring drift (proven, RC-6)**: base.py / delete.py / organize.py docstrings contradict the executor's auto-execute behavior. Documentation-only fix.
- **Task-action name exactness (strongly indicated)**: non-send task actions must match registered names exactly; near-miss names fail closed at execution (safe, but user-visible as failed tasks). No alias layer exists beyond `_SEND_ACTION_ALIASES`.
- **Key-dependent tool honesty (proven)**: `web_search` is always exposed; without `YDC_API_KEY` it fails honestly — a model may repeatedly select it against a misconfigured environment.
- **Deterministic guard conservatism (indicated)**: `_is_event_intent` / `_has_future_clock_request` are conservative; unusual phrasing falls through as conversational (safe — provider decides), never hijacks a command.
- **Silent execution (proven design)**: task outcomes are silent by default; owners may not notice failed scheduled actions except via logs/occurrences.

---

## 9. UNKNOWN / UNVERIFIED

- **Live Telegram verification: NOT PERFORMED** — no credentials/runtime in this workspace; all end-to-end claims are source-verified and in-process-tested.
- **Live confirmation round-trip: UNVERIFIED** — the «تأیید»/«بله»/“yes” flow is dispatcher-AI-path tested in-process only; no live Telegram run of the full confirm → execute cycle occurred.
- **Live provider/model switching: UNVERIFIED** — runtime switching (menu = serving = AI-reported = runtime context) is source-verified + in-process-tested (`test_ai_state_consistency`, `test_ai_menu_state_consistency`, `test_settings_runtime_switch`, `test_13_model_selection`, `test_34_ai_model_ui`); no live Telegram session exists in this workspace to observe the menu.
- **Live Supabase verification: NOT PERFORMED** — repository code paths tested with in-memory fallback; Supabase-backed behavior (RLS, CAS, JSONB constraints, `event` CHECK constraint migration `20260904000001_add_event_schedule_type.sql`) unverified live.
- **Per-provider native tool-call behavior** (`gemini` functionDeclarations translation, `openrouter`/`groq`/`cerebras`/`mistral`/`nararouter`/… tool-call JSON shapes) — only dummy-provider in-process coverage.
- **Per-provider env keys** — secrets unreadable by design; provider availability is environment-dependent.
- **AI memory write path** — `store_long`/`store_permanent` have zero production callers; intent and correctness beyond unit tests unverified.
- **Ghost Seen v2 / dashboard API live behavior** — out of AI scope, unverified.

---

## 10. EXACT FILES INSPECTED

- `backend/bot/handlers/ai_unified.py`, `backend/bot/handlers/task_events.py`, `backend/bot/router.py`, `backend/bot/handlers/ai.py`, `backend/bot/handlers/ghost_seen_v2.py`, `backend/bot/handlers/misc.py`
- `backend/runtime/supervisor.py`
- `backend/ai/engine/engine.py`, `backend/ai/engine/dispatcher.py`
- `backend/ai/session/request.py`
- `backend/ai/actions.py`, `backend/ai/persian.py`
- `backend/ai/tools/registry.py`, `base.py`, `executor.py`, `context.py`, and all tool modules: `save.py`, `delete.py`, `semantic.py`, `bio.py`, `username.py`, `account.py`, `retrieve.py`, `retrieve_save.py`, `database.py`, `settings.py`, `organize.py`, `websearch.py`, `task.py`, `task_management_tools.py`, `message.py`
- `backend/ai/task_*` family: `task_candidate.py`, `task_interpreter.py`, `task_creation.py`, `task_management.py`, `task_management_interface.py`, `task_execution.py`, `task_scheduler.py`, `task_event_dispatcher.py`, `task_trigger.py`, `chat_resolution.py`, `scheduling.py`
- `backend/ai/database/task_repository.py`, `backend/ai/persistence.py`, `backend/ai/config_store.py`, `backend/ai/confirmation.py`
- `backend/services/settings_service.py`, `backend/services/panel_settings_repository.py`
- `backend/ai/providers/` (factory, registry, `openai_compat.py`, `dummy.py`, `you.py`, `nararouter.py`), `backend/ai/memory/`
- `backend/services/*.py`, `backend/telegram_api/`, `backend/db/client.py`
- `supabase/migrations/20260904000001_add_event_schedule_type.sql`
- `tests/` (see §16 for suites executed), `IMPLEMENTATION_REPORT.md`, `DATABASE_ARCHITECTURE.md`, previous `INVESTIGATION.md`

## 11. EXACT FUNCTIONS / CLASSES

| Symbol | Location | Role |
|---|---|---|
| `create_default_registry` | `backend/ai/tools/registry.py` | Builds the 36-tool registry |
| `ToolRegistry.get/has/list_schemas` | `backend/ai/tools/registry.py` | Lookup/exposure |
| `ToolExecutor.execute` / `_is_auto_executable` | `backend/ai/tools/executor.py` | Sole execution authority; permission gating |
| `PermissionLevel` | `backend/ai/tools/base.py` | READ_ONLY/READ_WRITE/DANGEROUS/ADMIN_ONLY/CONFIRMATION_REQUIRED |
| `Dispatcher._dispatch/_execute_ai/_build_tool_definitions` | `backend/ai/engine/dispatcher.py` | Provider + deterministic + JSON action routing |
| `parse_command_intent` / `parse_action_text` / `validate_action` / `resolve_tool_calls` / `_is_scheduling_intent` / `_is_event_intent` / `_has_future_clock_request` | `backend/ai/actions.py` | Deterministic fast path + JSON action validation |
| `Engine.process_request` | `backend/ai/engine/engine.py` | Bounded AI request entry |
| `CreateTaskTool` / `TaskInterpreter` / `TaskCandidate.from_untrusted` / `_canonicalize_action` / `TaskCreationService` | `backend/ai/tools/task.py`, `task_interpreter.py`, `task_candidate.py`, `task_creation.py` | Task creation boundary (names-only triggers, send aliases) |
| `TaskScheduler` / `TaskExecutionCoordinator` / `TaskEventDispatcher` / `event_trigger_matches` / `event_occurrence_key` | `backend/ai/task_scheduler.py`, `task_execution.py`, `task_event_dispatcher.py`, `task_trigger.py` | Time + event execution (single shared machinery) |
| `task_events.register/configure` | `backend/bot/handlers/task_events.py` | Telethon update-path hook |
| `SendMessageTool` | `backend/ai/tools/message.py` | Only model-reachable Telegram write |
| `TelegramAPI` | `backend/telegram_api/` | Typed RPC facade — only Telegram boundary |

---

## 12. EXECUTION DIAGRAMS

### Normal AI tool execution (proven path)
```
user message (outgoing)
  → ai_unified.py (is_owner)
  → Engine.process_request
  → Dispatcher: allow_tools? → _build_tool_definitions() → provider.chat(definitions)
       ├─ provider tool_calls → parse_tool_calls → ToolRegistry.get(name) → ToolExecutor.execute
       ├─ JSON action in response → parse_action_text → validate_action → resolve_tool_calls → ToolExecutor
       └─ deterministic command → parse_command_intent → ActionParseResult(tool_calls) → ToolExecutor
  → Tool.execute → service → TelegramAPI / db (Supabase|memory) → side effect
  → ToolResult → dispatcher → AI response text
```

### Task execution (time-based)
```
TaskScheduler due poll (once/interval/daily/weekly, Asia/Tehran)
  → occurrence create/claim (CAS)
  → TaskExecutionCoordinator.execute
  → action name re-validated against ToolRegistry (fail closed if unknown)
  → ToolExecutor → registered tool → service → Telegram/Supabase
  → occurrence finalize → opt-in notifier (silent by default)
```

### Task execution (event-triggered)
```
Telegram message update → task_events handler (both directions)
  → extract_event_context
  → TaskEventDispatcher.handle_event (≤20 active event tasks, ≤5 executions)
  → event_trigger_matches (deterministic; sender/chat ids pre-resolved at creation)
  → occurrence key "<task_id>:ev:<chat_id>:<message_id>" (dedup)
  → TaskExecutionCoordinator → ToolExecutor → service → side effect
```

### Failure path (documented examples)
```
a) provider emits tool name not in registry  → ToolExecutor → not_found ToolResult → AI response
b) deterministic send-vocabulary, non-write  → ActionParseResult(KIND_UNSUPPORTED, "send")
                                              → "❌ Unsupported action: send" (fixed by routing guard for event/clock requests)
c) ADMIN_ONLY tool selected                 → needs_confirmation ToolResult → confirmation prompt stored (120s TTL) → owner confirms → frozen call re-issued via execute_confirmed; no confirmation (or expired / ambiguous reply) → nothing executes (settings_set)
d) task action name unregistered at exec    → coordinator fail-closed → occurrence failure record
e) event trigger ambiguous sender at creation → clarification ToolResult, task NOT created
```

---

## 13. REMAINING AI-CONNECTABLE CAPABILITIES

Ranked (implemented + safe + utility + integration effort):

1. **AI memory write tools** (`store_short`-family; `backend/ai/memory/`) — implemented persistence, zero production callers.
   - Blocker: no tool adapter, not registered, not schema-exposed.
   - Smallest fix: 1–2 `Tool` classes (e.g. `memory_store` with bounded key/text), register in `create_default_registry`, re-run exposure tests.
   - Security: owner-scoped writes to AI memory tables; no Telegram side effect — safe.
   - Tests: unit (tool), registry exposure, tool-call chain, dummy-provider AI-path.

2. **Confirmation round-trip for ADMIN_ONLY/CONFIRMATION_REQUIRED tools** — **IMPLEMENTED** (`c5d29f7`; see RC-2 fix + §17). `settings_set` is now AI-executable through the owner-confirmation boundary. No remaining work in this item.

3. **`TelegramAPI` facade methods without adapters** (`edit_message`, `search_messages`, `download_media`).
   - Blocker: no tool adapters; `edit_message` additionally needs a strict identity/destination policy (edit only messages the bot itself sent in the current chat).
   - Smallest fix: tool adapters + registration + schema exposure; edit requires an explicit security decision first.
   - Security: medium — destination/identity validation mandatory; do not connect without a policy.

4. **Panel/command-only capabilities (Ghost Seen v2, provider/model switching, trigger config, model tester, Taskloom, dashboard)** — should remain PANEL_OR_COMMAND_ONLY; connecting them would either duplicate the panel UX or require new security boundaries. No action.

---

## 14. SINGLE BEST NEXT IMPLEMENTATION CHUNK

**Fix `settings_set` unknown-key handling (RC-5)** — reject unknown/non-column keys before any write so a confirmed settings change can never report success without persisting.

Rationale:
- It is the **only remaining proven correctness defect** in the AI tool surface (reproduced in-process; owner-confirmed writes that silently do nothing).
- Smallest correct fix at the right layer: `settings_service.set_setting` (or `SettingsSetTool` before calling it) must fail closed for keys outside `_VALIDATORS`/`_AI_CONFIG_KEYS` — the same fail-closed discipline every other tool already has.
- Minimal surface: one service/tool guard + one focused regression test through the real executor path; no schema, no migration, no new authority.

Runner-up (explicitly NOT chosen for this chunk): memory write tool adapters (§13.1) — valuable but adds new capabilities rather than completing an existing one.

---

## 15. REQUIRED TESTS FOR NEXT CHUNK (RC-5 fix)

### UNIT TESTS
- `settings_service.set_setting` with an unknown key → returns False; cache is NOT polluted.
- `SettingsSetTool.execute` with an unknown key → `success=False` with an explicit “unknown setting key” message (through the real `ToolExecutor` chain).

### INTEGRATION TESTS
- Confirmed `settings_set` (via `execute_confirmed`) with an unknown key → fails closed even after owner approval.
- AI keys (`provider`/`model`/`temperature`/… valid values) still succeed through `config_store` — no regression on the runtime-switch path.
- Panel keys (`language` etc., valid values) still succeed through `settings_service`.

### LIVE TELEGRAM TESTS (post-fix)
- "مدل رو عوض کن به X" → confirmation → applied (menu + settings_get + next request agree).
- "فلان تنظیم ناموجود رو ست کن" → honest failure, no phantom success.

### ALREADY IMPLEMENTED (confirmation chunk, `c5d29f7`)
- `tests/test_confirmation_roundtrip.py` (63 tests): store single-use/frozen/expiry, exact-phrase recognition, executor `execute_confirmed` fail-closed, 14 full dispatcher AI-path tests (cross-chat/cross-owner, expired, ambiguous, frozen args).

---

## 16. VALIDATION

- **Source inspection completed**: full AI/tool/task/runtime/service/facade/provider layers read; exact names verified from source (see §10–§11).
- **INVESTIGATION.md replaced**: yes — this file is the complete replacement; no obsolete sections retained.
- **Production code unchanged**: yes — only `INVESTIGATION.md` was modified; `git status` shows no other tracked changes (pre-existing untracked `telegram-self-bot/` nested clone untouched).
- **Tests run (connectivity audit, 2026-09-04)**: focused connectivity suites passing (211 passed, 0 failed) — `tests/test_capability_exposure_tools.py`, `tests/test_tool_health_audit.py`, `tests/test_10_tool_calls.py`, `tests/test_19_ai_actions.py`, `tests/test_task_nl_creation.py`, `tests/test_task_send_execution.py`, `tests/test_task_trigger_events.py`.
- **Tests run (follow-up investigation, 2026-09-05, commands executed)**: `./.venv/bin/python -m pytest tests/test_tool_health_audit.py tests/test_confirmation_roundtrip.py tests/test_10_tool_calls.py tests/test_capability_exposure_tools.py tests/test_settings_runtime_switch.py -q -p no:cacheprovider` → **178 passed**; full suite `./.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` → **1695 passed, 23 skipped**.
- **git diff --check**: clean.
- **Final git diff reviewed**: only `INVESTIGATION.md`.
- **Final git status reviewed**: clean except pre-existing untracked `telegram-self-bot/`.
- **Git delivery verified (2026-09-05)**: local HEAD == local `origin/main` == remote `refs/heads/main` (`git ls-remote origin refs/heads/main`) at `347ac1d`; `git push --dry-run origin main` exit 0 (remote write authorization works); see §17.4.

---

## 17. FOLLOW-UP INVESTIGATION (2026-09-05)

Follow-up sessions re-verified the AI tool surface, the confirmation round-trip, AI provider/model runtime switching, runtime state consistency, and the Git delivery path against the CURRENT source. Everything below is source-verified and/or in-process-tested; nothing is claimed as live-verified.

### 17.1 Confirmation round-trip + runtime switching + state consistency (IMPLEMENTED, commits on origin/main)

| Commit | Purpose |
|---|---|
| `c5d29f7` | owner-confirmation round-trip for ADMIN_ONLY/CONFIRMATION_REQUIRED tools (`settings_set`) |
| `8ac3779` | apply AI model/provider changes at runtime (`apply_runtime_selection` → `ProviderManager.apply_selection`) |
| `8fc0981` | end-to-end provider/model state consistency (single authority: ProviderManager; persisted `ai_config` healed to it) |
| `1905c7c` | AI menu never displays an unappliable provider/model pair (`_effective_pair` from ProviderManager) |
| `b16fcec` | web `/api/ai/provider` writer persists provider + default model atomically |

State flow verified in source (all surfaces read ONE authority):

```
persisted ai_config (config_store) → apply_persisted_config (boot via supervisor + before every chat request)
  → apply_runtime_selection → ProviderManager.apply_selection (active provider + effective model)
  → dispatcher runtime context / engine_info / menu + health panels / settings_get / actual request
```

- Menu/health panels render `_effective_pair` from `ProviderManager` (persisted config only as fallback) — a phantom pair can never be displayed.
- `settings_set`/web/glass writers persist provider + default model atomically and reject unregistered providers before persisting.
- Writers that fail to apply are healed: `apply_persisted_config` rewrites the persisted pair to the ACTIVE runtime pair.

### 17.2 New findings (proven this session)

- **F-1 / RC-5 — `settings_set` phantom success on unknown keys** (reproduced in-process; see RC-5). Source: `settings_service.set_setting` has no key allowlist and falls back to a cache write + `True`.
- **F-2 / RC-6 — DANGEROUS permission docstring drift** (see RC-6). Behavior intentional; docstrings stale.
- **A-1 — `delete_messages_by_ids` description overclaims turn-scoped ID provenance** ("MUST have been returned by list_recent_messages in this turn" is prompt guidance; the enforced boundary is re-fetch + outgoing-only + same chat). P3.
- **A-2 — `settings_get` on an unset AI key returns success with an empty value** (`config.get(key, "")` → `key = `). P3.
- **A-3 — `web_search` swallows the concrete failure reason** into a generic `❌ Web search failed.` (exception type logged only). P3.
- **A-4 — `save`/`save_by_link` long_running exemption** is bounded only by the 60 s request-level `wait_for` (plus 120 s pending-input expiry), not by a tool timeout. P3 design constraint.
- **No security regressions found**: ToolExecutor fail-closed paths (unknown tool, malformed args, non-object args), `MAX_TOOLS_PER_TURN=5`, `MAX_TOOL_ROUNDS=3`, per-tool timeouts, outgoing-only deletes, data-minimized `account_show`/`get_bio`, trusted-destination `send_message`/`retrieve_save` — all verified in source. The confirmation store is owner+chat scoped, single-use, 120 s TTL, exact-phrase, frozen-argument; `execute_confirmed` is the only gate bypass and only the Dispatcher calls it with a consumed `PendingConfirmation`.

### 17.3 Verification status of the 36-tool matrix (2026-09-05)

- 36/36 registered (counted by executing `create_default_registry`), all schema-exposed, all reachable through the real ToolExecutor path.
- Executor-chain tests: 31 tools parametrized in `test_tool_health_audit.py` (service boundary faked) + dedicated real-path suites (bio/username through real services with in-memory DB, `create_task` through the real repository, task tools, retrieve_save, confirmation AI-path).
- Classification: **35 REAL_CONNECTED** + **1 CONFIRMATION_GATED** (`settings_set`) = 36 AI-executable; **1 PARTIAL** (`settings_set` unknown-key defect, RC-5); 0 BROKEN/STALE/UNREACHABLE.
- Live side effects (Telegram uploads/deletes, provider network calls, Supabase-backed writes) remain **NOT LIVE VERIFIED**.

### 17.4 Git delivery verification (2026-09-05)

- `git push --dry-run origin main` → "Everything up-to-date", EXIT=0 (credential + ref exchange OK — remote WRITE authorized).
- `git ls-remote origin refs/heads/main` == `git rev-parse HEAD` == `git rev-parse origin/main` == `347ac1d…` before this document's commit; re-verified after push (see §18).
- **Nested-clone hazard (documented, not deleted):** `telegram-self-bot/` is a dormant stale clone (own `.git`, HEAD + tracking ref pinned 17 commits behind the remote, uncommitted older drafts). An agent operating inside it would commit into the wrong repository and verify against a stale tracking ref — the concrete mechanism by which "committed but not pushed" claims could arise. Authoritative repository root: `/home/daytona/codebase`. Full forensic record: IMPLEMENTATION_REPORT.md §15.

### 17.5 Validation classification (2026-09-05)

| Claim | Status |
|---|---|
| 36 tools registered + schema-exposed + executor-reachable | SOURCE-VERIFIED + TESTED (in-process) |
| `settings_set` confirmation round-trip (frozen args, TTL, single-use, owner/chat scope) | SOURCE-VERIFIED + TESTED (dispatcher AI-path) |
| provider/model runtime switching + menu/runtime/settings_get consistency | SOURCE-VERIFIED + TESTED (in-process) |
| F-1 phantom success on unknown settings keys | SOURCE-VERIFIED + REPRODUCED IN-PROCESS |
| RC-6 DANGEROUS docstring drift | SOURCE-VERIFIED |
| Full suite | 1695 passed, 23 skipped (2026-09-05) |
| Git remote state | REMOTE-VERIFIED (`ls-remote` == HEAD, pre- and post-push) |
| Live Telegram «تأیید» round-trip, live provider/model switch, live menu rendering, live Supabase | **UNVERIFIED** (no credentials/runtime in this workspace) |

---

## 18. PUSH VERIFICATION (this document)

Commands executed after committing this update (recorded exactly):

```
git push origin main
git fetch origin
git rev-parse HEAD
git rev-parse origin/main
git ls-remote origin refs/heads/main
git status --short
```

Result (all three SHAs identical):

| Check | SHA |
|---|---|
| Local HEAD | `a6ed8a2c991780c799089e7a545e32b8aafcc49c` |
| origin/main (tracking) | `a6ed8a2c991780c799089e7a545e32b8aafcc49c` |
| remote `refs/heads/main` (`ls-remote`) | `a6ed8a2c991780c799089e7a545e32b8aafcc49c` |

Working tree after push: clean except pre-existing untracked `telegram-self-bot/`.

**local HEAD == origin/main == remote main: YES** (verified, not assumed).