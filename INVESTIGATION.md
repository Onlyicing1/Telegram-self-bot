# Investigation

## Status

**Investigation only — no implementation performed.**

- **Stage completed: Investigation only**
- **Next stage: Stage 6** — derived from the current `IMPLEMENTATION_REPORT.md`,
  which states at the top: *“Stage completed: Stage 5”* / *“Next stage:
  Stage 6”* (Stage 5 = commit `eb4d852`, the removal of the deterministic
  semantic router). The report defines the number; this investigation only
  recommends what Stage 6 should contain.

This document replaces the previous `INVESTIGATION.md` completely. It is a
read-only audit of the **current** `main`; it changes no production code, no
test, no migration, no prompt, and no tool definition. Nothing was executed
against live Telegram or live Supabase.

> Note on the investigation baseline: at the start of this audit the workspace
> was at `c69df41`, while `origin/main` had already advanced to `eb4d852`
> (Stage 5). The workspace was fast-forwarded to `eb4d852` (tree was clean)
> and **all findings below were re-verified against the post-Stage-5 code**;
> findings from the earlier revision were re-tested, not carried over.

---

## Repository State

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` (remote `origin`) |
| Branch | `main` |
| Local HEAD | `eb4d85220aa0f73265b3e18e14a50ba05f0065d0` — `refactor(ai): make the AI the sole interpreter of natural-language intent` |
| Remote HEAD (`origin/main`) | `eb4d85220aa0f73265b3e18e14a50ba05f0065d0` (equal; verified with `git fetch origin` + `git rev-parse origin/main` + `git merge-base --is-ancestor` → exit 0) |
| Working tree | clean except the pre-existing untracked nested repository `telegram-self-bot/` (never staged) |
| Commits since the previous audit baseline | `916536c` `fix(ai): separate immediate workflows from durable tasks at the routing boundary`; `62dadfe` `docs: forensic investigation of create_task misroute of immediate workflows`; `eb4d852` `refactor(ai): make the AI the sole interpreter of natural-language intent` (Stage 5) |
| Files this task writes | `INVESTIGATION.md` only |

---

## Investigation Scope

Primary question: **why does the AI currently fail to understand user requests
and, as a consequence, fail to select and use the right tools?**

The audit traces the complete decision path against the current source:

```
USER NATURAL LANGUAGE
  → ai_unified activation / AIRequest construction
  → ContextBuilder → PromptBuilder (system/developer instructions)
  → ProviderManager → provider/model input
  → model reasoning / native tool-call decision
  → tool selection + arguments
  → ToolRegistry → ToolExecutor → existing service/tool
  → tool result
  → AI continuation / final response
```

It answers questions A–J of the brief, separated as: (A) context/input
sufficiency, (B) hidden semantic routing, (C) system-prompt instruction audit,
(D) tool definitions, (E) provider tool contract, (F) execution loop, (G)
argument construction/validation, (H) diagnostic request matrix, (I) model
failure vs system failure, (J) ranked root causes.

Method: every claim was read from the current tree and, where measurement was
possible, executed in-process as read-only introspection (tool registry dump,
prompt-package build, parser probes, budget simulation). No source file was
modified. No live Telegram/Supabase verification was attempted.

---

## Current AI Decision Flow

Current HEAD is **post-Stage-5**: the deterministic semantic router
(`parse_command_intent`, the dispatcher fast path, the deterministic task
candidate, the create_task natural-language completeness gate, the save tag
override, the semantic-delete structural predicate) is **gone**. The decision
path is now:

1. **Activation — `backend/bot/handlers/ai_unified.py`** (`register()` at
   symbol `ai_unified_handler`). Fires on every `outgoing=True` message.
   - `.`-prefixed messages are skipped; `Menu` is a separate exact-equality
     handler in `misc.py`.
   - The configured trigger word (`match_trigger`, case-insensitive first
     word) or a reply to a known AI message activates the AI.
   - The trigger is stripped; the remaining text becomes `request.user_message`.
     A replied-to message is **context** (`ReplyContext`), never the user
     message. A one-shot Telegram surrounding window is fetched
     (`_load_telegram_chat_context`) and threaded via `AIRequest`.
   - `AIRequest` (`backend/ai/session/request.py`) carries `user_message`,
     `reply_context`, `telegram_context`, `request_media_type`,
     `timeout_s = 240.0` (`_AI_EXECUTE_TIMEOUT`).
2. **Dispatch — `backend/ai/engine/dispatcher.py::Dispatcher.dispatch`
   (line 213).** Before any prompt/provider work, two local boundaries run:
   - `_try_consume_confirmation` (owner approval for a previously blocked
     ADMIN_ONLY/CONFIRMATION_REQUIRED call);
   - `_try_media_analysis` (deterministic media target resolution from runtime
     identifiers; fully context-isolated).
3. **Prompt build** — `_build_context` + `_build_tool_definitions` +
   `_render_tool_schemas` + `PromptBuilder.build` produce one `PromptPackage`;
   `_build_messages` (line 1922) turns it into **four `system` messages + one
   `user` message**.
4. **Provider round** — `ProviderManager.chat` selects a (provider, model)
   candidate (active first, then score, then fallback/model pool) and calls the
   adapter; `tools` (OpenAI format) and `tool_choice="auto"` travel on every
   round.
5. **Structured decision** — a native `tool_calls` response goes straight to
   the executor; otherwise `_apply_structured_action` (line 1679) parses and
   validates the **model’s own JSON** (`parse_action_text` →
   `validate_action` → `resolve_tool_calls`). No local code re-reads the owner
   message to decide intent.
6. **Tool execution** — `ToolExecutor.execute_calls` is the sole caller of
   `tool.execute()`; registry lookup, permission gate, timeouts, malformed
   argument rejection (`backend/ai/tools/executor.py`).
7. **Feedback / continuation** — `_build_continuation_messages` (line 1937)
   replays the assistant tool call and JSON tool results, and the loop
   (max `MAX_TOOL_ROUNDS = 3`, line 67; max `MAX_TOOLS_PER_TURN = 5`) lets the
   model consume results and call more tools.
8. **Final result** — `EngineResult` → delivery; `ReplyResolver` registers the
   AI answer for future reply-to-AI activation.

The only remaining places where deterministic code reads natural language are
**authorization / provenance gates**, never capability selection (see
Semantic Routing Audit).

---

## Context/Input Audit

### What the model actually receives (measured on current HEAD)

`_build_messages` (dispatcher line 1922) sends, in order:

| # | Message | Content | Measured size |
|---|---|---|---|
| 1 | `system` | merged: `SYSTEM_RULES` + `PLATFORM_CONSTRAINTS` + `RUNTIME_RULES` + `[Memory]` + `[Preferences]` + `OUTPUT_INSTRUCTIONS` (builder `_merge_system`) | 21,730 chars of base templates |
| 2 | `system` | `[Runtime Context]` (menu/panel/timezone/language/current time/provider/model/counters) | ~230 chars |
| 3 | `system` | `[Conversation State]` (state/flow, Telegram surrounding block, reply context, **History**) | variable; **History is stripped — see below** |
| 4 | `system` | `[Tool Context]` + `[Available Tools]` schema text block | 15,166 chars (55 tools) |
| 5 | `user` | the owner’s request (trigger stripped; labeled `[Current Request]` only when a Telegram surrounding block exists) | the raw text, intact |

In addition, the **native tool definitions** travel out-of-band on every
provider call: OpenAI-format JSON generated by `Dispatcher._build_tool_definitions`
(line 1851) — measured **37,175 chars** for the 55 tools. The model therefore
sees the tool surface **twice** (text block + native schemas).

### Where information is added / transformed / lost

- **Added:** trigger-stripped user text; reply metadata and (for known AI
  replies) full AI content; bounded Telegram window (≤10 messages, ≤200 chars
  each, ≤1500 chars total, provenance-marked AI messages removed); memory
  blocks; preferences; runtime and tool context; tool schemas.
- **Transformed:** the request is only trigger-stripped — otherwise intact.
  Reply content is never promoted into the user message. Provider adapters
  translate the tool list (Gemini → `functionDeclarations`).
- **Lost — conversation history (systemic).** `PromptBuilder.build`
  (`backend/ai/prompt/builder.py`) computes `compute_budget`; when the estimated
  total exceeds `DEFAULT_MAX_TOTAL_TOKENS = 8500` (`prompt/budget.py` line 26)
  it calls `_trim_to_budget`, which pops history entries **oldest-first until
  the estimate fits**. With the production tool block, the static prompt alone
  already exceeds the ceiling, so **every history entry is removed on every
  request** and `[History]` is never rendered.
  - Measured: base prompt = **9,349 estimated tokens** (English heuristic) with
    **zero** history, vs the 8,500 ceiling → `within_budget=False`.
  - Measured: with 20 history entries of realistic Persian turns, the rendered
    `[Conversation State]` contains **0** history rows and no `[History]`
    label (same result for 0, 2, 5 and 20 entries).
  - The existing test `tests/test_context_architecture.py` pins trimming with a
    **synthetic ~7.2k-char** tool block (which stays within budget); it does not
    exercise the real 15.2k-char/55-tool block, so the production behavior is
    untested and unpinned.
- **Lost — Gemini system messages (provider-conditional).** `_build_messages`
  emits **four** `system` messages. The Gemini adapter
  (`backend/ai/providers/gemini.py`, lines 63–69) iterates messages and
  assigns `system_text = content` for each `role == "system"` — **the last one
  wins**. Gemini therefore receives only message #4 (`[Tool Context]` + tool
  schemas) as `systemInstruction`; the merged rules/platform/runtime/memory/
  output instructions, runtime context, conversation state, reply context and
  history are silently dropped. OpenAI-compatible providers pass all messages
  through unchanged.
- **Duplicated:** the full tool surface appears as prose (15,166 chars) and as
  native schemas (37,175 chars).

### Does the request reach the model intact?

Yes — for a single-turn request the owner’s text is delivered byte-for-byte
(only the trigger is removed). The losses are structural: **conversation
history never arrives** (all providers), and **the entire instruction layer is
dropped for Gemini-routed requests**.

---

## Semantic Routing Audit

**Finding: no hidden semantic router remains in the decision path. Stage 5 is
true in the current code, not only in the documentation.**

Removed and confirmed absent from current source (grep over `backend/` returns
no hits):

- `parse_command_intent`, `_is_scheduling_intent`, `_parse_status_intent`,
  `_is_event_intent`, `_has_future_clock_request`, `_extract_count`,
  `_is_semantic_delete`, `_extract_semantic_query`, `_parse_number`,
  `_is_history_analysis_intent` and the cadence/action vocabularies in
  `backend/ai/actions.py`;
- `_try_local_fast_path`, `_build_deterministic_task_candidate`, `_reply_text`
  and the `parse_command_intent` pre-pass in `backend/ai/engine/dispatcher.py`;
- the natural-language completeness gate and profile-fidelity override in
  `backend/ai/tools/task.py`;
- the `explicit_no_tags_requested` keyword override in `backend/ai/tools/save.py`;
- `parse_structural_predicate` and its number-word vocabulary in
  `backend/ai/semantic_delete.py`.

What remains deterministic in the decision path is **technical validation /
authorization**, and it cannot choose a capability:

| Location | What it reads | Why it is not routing |
|---|---|---|
| `backend/ai/actions.py` | `validate_action`, `resolve_tool_calls`, `parse_action_text`, `extract_json_object`, `_TELEGRAM_LINK_RE`, `_SAVE_CODE_RE` | validates/parses **the model’s own output** or fixed artifact shapes |
| `backend/ai/proactive.py::has_proactive_authorization` | the owner’s message | per-request **permission gate** for extra work; fail-closed; consumed after the model proposes an action |
| `backend/ai/task_contract.py::ground_ai_instruction` / `_generation_authorized` (uses `actions.py` helpers `_tokenize`, `_write_text_present`, `_has_bio_mention`, `_has_bio_change_intent`, `_USERNAME_WORDS`) | the owner’s message | **provenance**: decides whether generated content is authorized for an already-proposed `create_task`; drops ungrounded instructions |
| `backend/ai/confirmation.py::is_explicit_confirmation` | the message | exact full-message match against a tiny phrase set to consume a **previously created** pending approval |
| `backend/ai/config_store.py::match_trigger` | the first word | literal trigger-word activation |
| `backend/ai/semantic_delete.py` | text of already-fetched messages | applies the **AI-chosen** predicate (`spec_from_dict`, `build_matcher*`) |

Evidence of absence is additionally pinned by tests, all passing on this HEAD
(51 passed): `tests/test_semantic_intent_boundary.py` (no local selection;
every request reaches the provider; `create_task` still offered),
`tests/test_regex_routing_removal.py` (no retired router symbol/vocabulary),
`tests/test_intent_routing_boundary.py`, `tests/test_provider_tool_boundary.py`.

**Conclusion:** there is no equivalent semantic router elsewhere that can
override the AI. The AI is the sole interpreter of natural-language intent on
this HEAD. Consequently, every misunderstanding is now attributable to the
input context, the instruction layer, the tool/provider contract, the model’s
own decision, or the execution/feedback loop — not to deterministic
interception.

Handler surface (checked): outgoing-message handlers are `ai_unified`
(trigger/reply-to-AI), `misc` (literal `Menu`), and dot-command handlers
(`.task`, etc., skipped by `ai_unified`); `task_events` only resolves owner
**replies to parked task questions**. None reads free-form NL to choose a tool.

---

## System Prompt / Instruction Audit

The current `backend/ai/prompt/template.py` (all four templates are merged into
one system message; `OUTPUT_INSTRUCTIONS` is part of it) is substantially
better than the pre-Stage-5 prompt: it now contains an explicit
**immediate-vs-durable** policy, an ordered multi-action policy, and a
"talking about a capability is not using it" rule. Measured sizes: `SYSTEM_RULES`
10,436 / `PLATFORM` 707 / `RUNTIME` 2,088 / `OUTPUT` 8,499 chars.

What is explicitly instructed (verified in the current templates):

- understand the goal and call the matching tool: *“when the owner requests an
  action, you must call the matching tool and report its REAL result”*;
- immediate vs durable: *“An IMMEDIATE WORKFLOW is work the owner wants done NOW …
  A DURABLE TASK is work … done LATER, again, or at a time … NEVER turn an
  immediate workflow into create_task …”*; it names the production example
  (`search → save → tag`) and says to call `web_search`, `save`,
  `update_save_tags` in order in this turn;
- multi-action order: *“emit them as consecutive tool calls IN THIS TURN, in the
  owner’s order, and use each real tool result before deciding the next call”*;
- use results to continue / never fabricate success: *“After a tool call, report
  its REAL result”*, *“Tool results are AUTHORITATIVE data”*;
- prefer tools over prose for executable commands: *“output ONLY the tool call”*;
- clarification only when genuinely ambiguous;
- capability mention ≠ execution request;
- destructive-target resolution rules (reply / last N / explicit ID);
- saved-item name/tags/version rules.

Gaps and conflicts found (current text):

1. **Capability enumeration is incomplete and biased.** The “You may: …” list
   names save/delete/list/search/database/bio/username/account only. `web_search`,
   `translate_history`, `summarize_history`, `send_message`, `memory_*`,
   `organize_*`, `ask_owner` and `text_to_speech` are not in that list (some are
   covered elsewhere — web search only by one example sentence and the tool
   description; translation/summarization nowhere in the rules). A model that
   weighs the explicit capability summary over 55 tool descriptions can underuse
   these.
2. **The JSON fallback contract is narrower than the real tool set.** OUTPUT
   rule 8 enumerates 30 actions and ends with `…`; it omits `web_search`,
   `create_task`, `translate_history`/`summarize_history`, `send`, and the
   memory tools. `_ENFORCE_ACTION_NUDGE` (dispatcher line 120) repeats a similar
   narrow subset (“save, delete, list/search saved items, retrieve, database,
   bio/username, task list/inspect/transition, message review”). For providers
   without native tool calling, those capabilities cannot be expressed through
   the documented fallback at all.
3. **No explicit current-information policy.** The only guidance to use
   `web_search` is the tool description and the one immediate-workflow example.
   Rule 7 (*“If you don’t know something … say so — do not guess”*) and rule 4
   (*“If no tool is needed, respond with a natural language answer”*) can both
   read as permission to answer from training data or refuse, rather than
   search, for current-fact questions.
4. **Implicit tool need is still under-served.** RUNTIME_RULES says *“Execute an
   action only when the owner explicitly requests it in this turn”*. The new
   immediate-workflow bullet softens this for explicit multi-action requests,
   but nothing states the converse rule: when the answer depends on data a tool
   can fetch, use the tool even if the owner only asked a question.
5. **Correction handling is not instructed.** Nothing tells the model to
   reconsider its previous approach when the owner corrects it (e.g. *“اگر
   میخواستم خودم سرچ کنم بهت میگفتم”*). Combined with lost history (P0-2 below),
   a correction that is not a reply to the AI message has no anchor.
6. **“Tell me how” vs “do it for me”** is handled only for specific command
   families (save/delete) and the capability-tuning rule; a general distinction
   is absent.
7. Minor: rule 2’s “under 500 characters” competes with multi-step reporting;
   `[Available Tools]` (dispatcher `_render_tool_schemas`) renders parameter
   names and types but **not requiredness**, while the native schemas carry a
   (defective — see P0-1) `required` list.

---

## Tool Inventory and Contract Audit

Current registry: **55 tools** (`create_default_registry`, `backend/ai/tools/registry.py`
line 107; count pinned by `tests/test_capability_exposure_tools.py`). All tools
are thin wrappers returning `ToolResult(success, message, data)`; failures are
derived from service `❌/⚠️/🚫` prefixes (`tools/base.py`).

Inventory (name — permission — notable parameters / declarations; “required*”
= the `required` list the **provider schema** actually carries, see P0-1):

| Tool | Perm | Params / notes |
|---|---|---|
| `save` | read_write, long_running, requires reply | display_name, tags; description says both optional → provider `required* = [display_name, tags]` |
| `save_by_link` | read_write, long_running | link (declared required), display_name, tags → `required* = [link, display_name, tags]` |
| `delete` | dangerous | count(1–500), mode enum(last_n/all/until_time/until_message/filtered), until_time, after_time, boundary_id, query, semantic{query,word_count,english_word_count} → `required*` = all 7 |
| `delete_by_id` | dangerous | message_id (actual required) |
| `delete_replied` | dangerous, requires reply | no params |
| `delete_message_by_id` | dangerous | message_id |
| `delete_messages_by_ids` | dangerous | message_ids[] |
| `list_recent_messages` | read_only | limit(1–100, default 50) → `required* = [limit]` |
| `bio_set_template` / `bio_set_text` / `bio_set_mood` / `bio_on` / `bio_off` | read_write | template / text / mood |
| `bio_show` / `get_bio` | read_only | no params |
| `username_set_template` / `username_set_text` / `username_set_mood` / `username_on` / `username_off` | read_write | template / text / mood |
| `username_show` | read_only | no params |
| `search` | read_only | query (required) |
| `list_saves` | read_only | limit(1–50, default 10) |
| `database_stats` | read_only | no params |
| `account_show` | read_only | fields[] enum(first_name,last_name,full_name,username) → `required* = [fields]` |
| `settings_get` | read_only | key (required) |
| `settings_set` | **admin_only** (confirmation) | key, value |
| `organize_list` / `organize_clean` | read_only / dangerous | no params |
| `web_search` | read_only | query, count(1–100, default 10), freshness enum(day/week/month/year), include_domains[] → `required* = [query, freshness, include_domains]` |
| `create_task` | read_write | single `request` string; a **second provider round** interprets it (`TaskInterpreter.interpret`) |
| `task_list` | read_only | status enum(active/paused/completed), default null → no `required` key (pinned) |
| `task_inspect` | read_only | task_id (required) |
| `task_transition` | read_write | task_id, action enum(active/paused/completed), expected_version, query, complete_steps → `required* = [task_id, action, expected_version, query, complete_steps]` |
| `task_delete` | read_write | task_id, expected_version, query → `required* = [task_id, expected_version, query]` |
| `todo_add` | read_write | title, steps[] → `required* = [title, steps]` |
| `todo_find` | read_only | query |
| `todo_edit` | read_write | title, task_id, expected_version, query |
| `todo_step_add` | read_write | title or steps, task_id or query |
| `todo_step_list` | read_only | task_id or query |
| `todo_step_transition` | read_write | action enum(completed/active), step or step_query, task_id or query |
| `todo_step_edit` | read_write | title, step or step_query, task_id or query |
| `todo_step_delete` | read_write | step or step_query, task_id or query |
| `retrieve_save` | read_write | save_code or query (exactly one match; multi-match asks) |
| `preview_save` / `delete_save` | read_only / dangerous | save_code |
| `rename_save` | read_write | save_code or query; display_name and/or file_name (file_name re-uploads) |
| `update_save_tags` | read_write | save_code or query; tags[]; mode enum(add/replace/remove) |
| `translate_history` | read_only, long_running | count(1–1000, default 100), language, instruction → `required* = [language, instruction]` |
| `summarize_history` | read_only, long_running | count, instruction → `required* = [instruction]` |
| `send_message` | read_write | text(1–4096), font enum(23 fonts) → to Saved Messages only |
| `ask_owner` | read_write | question(1–512); durable-chain question |
| `text_to_speech` | read_write, long_running | text(1–1000) — subsystem is reported frozen/deferred |
| `memory_store` | read_write | content, tier enum(long/permanent), category enum, importance |
| `memory_list` | read_only | tier, query, limit |

**Overlap / ambiguity risks (tool-definition level):**

- **Delete family has five tools** (`delete`, `delete_by_id`, `delete_replied`,
  `delete_message_by_id`, `delete_messages_by_ids`) plus `delete_save`. The
  mega `delete` tool carries 7 parameters with terse semantics; a model choosing
  among “delete by id”, “delete one by id”, “delete replied”, “delete ids” and
  “delete with mode” has multiple plausible candidates for the same phrase.
- **Save family:** `save` vs `save_by_link` overlap on “save this” when the
  target is a link; the reply requirement of `save` is only in the description.
- **Retrieve/saved-item:** `search` (query over saved items), `list_saves`,
  `retrieve_save` (save_code **or** query), `preview_save`, `rename_save`,
  `update_save_tags` — four tools accept either code or query with multi-match
  clarification built in.
- **Task addressing:** `task_transition` / `task_delete` / `todo_edit` accept
  id+`expected_version` **or** `query`; `todo_step_*` accept `step` or
  `step_query` — the tool text explains this, but the provider schema marks all
  alternatives as required (P0-1), which directly contradicts the alternatives.
- **`create_task` has one opaque `request` argument.** Intent is resolved in a
  second provider call; the caller must already know a schedule exists. There is
  no schema-level help for what a durable vs immediate request looks like — that
  now lives only in the system prompt.
- **Hidden rules inferable only from implementation:** `save` needs a replied
  message; `web_search` requires the `you` retrieval provider to be configured
  (`YDC_API_KEY`) or returns `⚠️ Web search failed`; `update_save_tags` needs a
  saved item; `task_transition` needs the current version; `settings_set` is
  confirmation-gated; `text_to_speech` targets a frozen subsystem.

Error/result behavior is consistent (message text for the model; `success`
derived from service prefixes). Result formats are bounded by the services; the
dispatcher passes `{tool, success, message, data, error}` to the model.

---

## Provider Tool-Calling Contract

**Serialization.** `Dispatcher._build_tool_definitions` (line 1851) wraps each
registry schema as
`{"type":"function","function":{name, description, parameters:{type:object, properties}}}`.
The registry’s `parameters` is a flat `{param: descriptor}` map. The `required`
list is **inferred**: *every parameter whose descriptor lacks a `default` key is
declared required* (line 1861–1870).

**Defect (measured on current HEAD):** **40 of 55 tools** receive a nonempty
`required` list, and for many of them the list names genuinely optional
parameters. Examples:

| Tool | Provider `required` (current) | Actually |
|---|---|---|
| `save` | `[display_name, tags]` | both optional; the prompt *forbids inventing* them |
| `save_by_link` | `[link, display_name, tags]` | display_name/tags optional |
| `web_search` | `[query, freshness, include_domains]` | freshness/include_domains optional |
| `delete` | `[count, mode, until_time, after_time, boundary_id, query, semantic]` | alternative/optional scope params |
| `list_recent_messages` | `[limit]` | optional (default 50) |
| `account_show` | `[fields]` | optional (defaults first_name+username) |
| `task_transition` | `[task_id, action, expected_version, query, complete_steps]` | `query` is an ALTERNATIVE to id+version |
| `task_delete` | `[task_id, expected_version, query]` | `query` alternative |
| `todo_step_list` | `[task_id, query]` | either/or |
| `translate_history` | `[language, instruction]` | both optional; count defaulted |
| `memory_list` | `[tier, query, limit]` | all optional |

The text tool block (`[Available Tools]`) does **not** state requiredness, so the
model’s only requiredness signal is the wrong list. Contradiction example: the
system prompt says *“NEVER invent a name or a tag”* while the provider schema
marks `display_name` and `tags` required for `save` — the model must either
violate the prompt, pass empty placeholders, or avoid the tool.

**Provider coverage.** Every chat-capable provider is an `OpenAICompatProvider`
subclass (`openai`, `groq`, `cerebras`, `mistral`, `openrouter`, `nvidia`,
`sambanova`, `siliconflow`, `fireworks`, `zai`, `nararouter`, `cohere`): the
`tools` array and `tool_choice="auto"` are forwarded verbatim; tool calls are
parsed natively (malformed JSON arguments are flagged, never silently `{}`).
Gemini translates to `functionDeclarations` and preserves name/description/
parameters **including the same defective `required`**. `you` is a web-search
retrieval capability (`CAPABILITY_KIND="web_search"`), excluded from chat
routing; `dummy` never serves production. No tool is silently omitted: the
dispatcher logs `AI_TOOL_AVAILABILITY tools=55`.

**System-message collapse (Gemini).** As described in the Context/Input Audit:
`gemini.py` keeps only the **last** system message as `systemInstruction`. This
is a provider-contract/context defect independent of the required-fields defect;
for Gemini-routed requests the model receives the tool schemas but **none of the
rules or output contract**.

---

## Tool Execution Loop

Verified on current HEAD (`dispatcher.dispatch`, lines 213+, and the round loop):

1. **Receive** — provider adapters return `tool_calls` with `id`, `name`,
   `arguments` (dict; malformed JSON flagged). Gemini function calls are
   normalized to the same shape (id often empty).
2. **Validate** — `ToolExecutor._execute_single`: missing name, malformed
   arguments, non-object arguments, and unknown tools produce structured
   failures (`missing_name`, `malformed_arguments`, `not_found`). Permission
   levels: READ_ONLY/READ_WRITE/DANGEROUS execute; ADMIN_ONLY/
   CONFIRMATION_REQUIRED return `needs_confirmation` and the dispatcher raises
   a bounded pending approval.
3. **Resolve + execute** — registry lookup then `tool.execute()` (the executor is
   the sole caller); generic timeout 10 s, `long_running=True` exempt; tool
   history recorded.
4. **Feed back** — results are recorded in conversation history and, for native
   rounds, converted to `role: "tool"` messages carrying
   `{"tool","success","message","data","error"}` plus the assistant’s
   `tool_calls` replay. The model may then call more tools.
5. **Terminate** — loop bound `MAX_TOOL_ROUNDS = 3` (each round ≤
   `MAX_TOOLS_PER_TURN = 5`), with:
   - *verbatim read authority*: a round executing ONLY `get_bio`, `task_list`,
     `translate_history`, `summarize_history`, `preview_save` returns the tool
     text exactly and skips the continuation round (`_VERBATIM_READ_TOOLS`);
     `task_list`/`task_inspect` get one deferred continuation because they can
     feed a CAS mutation;
   - *structured-action short-circuit*: when the model emitted JSON (not a
     native call), the tool result becomes the final text via
     `_summarize_tool_results` — there is **no model continuation round**;
   - *salvage at limit*: tool calls from the final continuation are executed
     once without another provider round (never re-executed).
6. **Final result** — if tools ran and no text was produced, the real-result
   summary is delivered (never a fabricated success).

**Multi-step support:** genuinely supported for native tool calling within the
bounds — the model can chain tools across up to 3 execution rounds and consume
each result. The loop is NOT a one-tool-only design. Its limits: complex
immediate workflows needing >15 calls cannot complete; the JSON path cannot
phrase a final answer from results; read-only verbatim tools intentionally skip
the model on their round. Rounds are not persisted across requests.

---

## Tool Result Feedback Loop

- Native round → `_build_continuation_messages` builds:
  `assistant` (content + `tool_calls` with JSON-string arguments, ids preserved)
  followed by one `role: "tool"` message per execution carrying
  `tool_call_id`, `name`, and a JSON object with `success`, `message`, `data`,
  `error`. The next provider round receives the full accumulated message list
  plus the same `tools` array.
- The result payload is not truncated by the dispatcher; bound comes from the
  tools/services (e.g. web search formats at most 8 results; history tools
  return large text but are delivered verbatim).
- Results **are** recorded to conversation history (`add_tool_result`) and
  `all_tool_results` is attached to result metadata/telemetry.
- Results are **not** re-fed to the model in two cases by design: the
  structured-action path (deterministic summary is final) and verbatim read
  tools (anti-paraphrase guarantee). The salvage-at-limit round also executes
  without a synthesis round.
- Tool-result → plain-user-text conversion happens only in the deterministic
  summary path (`_summarize_tool_results`, and `_render_message_list` for
  message listings).
- Cross-request feedback: tool results enter conversation history, but that
  history is stripped from the prompt by the budget (P0-2), so the model does
  not actually see earlier turns on the next request.

---

## Argument Construction / Validation

- **Provider side:** OpenAI-compatible adapters `json.loads` the arguments
  string; on failure they set `malformed_arguments` + `arguments_error` instead
  of substituting `{}`. Gemini passes the arguments dict through.
- **Executor side:** `_execute_single` rejects missing names, malformed
  arguments, non-object arguments and unregistered tools with structured
  errors; it does **not** enforce the schema `required` list (tools validate
  themselves), so the wrong `required` list (P0-1) misleads without causing an
  executor rejection by itself.
- **Tool side (examples):** `web_search` coerces `count` and drops invalid
  `freshness`; `delete` enforces count bounds and mode semantics; `save`
  validates/trims/dedupes name and tags; task/todo tools require ids+versions or
  unique query matches and refuse ambiguity (returning candidates); saved-item
  management refuses multi-match and asks. These are deterministic **validation**
  failures, not model mistakes.
- **JSON path:** `validate_action` (actions.py) enforces known action names,
  field allowlists, count bounds and target scopes; `resolve_tool_calls` maps to
  concrete tool calls. Anything unknown fails closed (invalid/unsupported/
  clarify).
- Separation guidance: when a failure is a schema-shaped argument object that
  violates documented semantics, the cause is model-side (or schema-side for
  the required list); when the executor/tool returns a bounded validation
  message (invalid id, stale version, ambiguous match), the cause is
  execution/validation-level and the model is expected to recover — provided
  the result reaches it (see feedback-loop exceptions).

---

## Diagnostic Request Matrix

Measured against current HEAD: with Stage 5, **none of these classes is
intercepted locally** — every one reaches the model (subject to trigger
activation). The failure analysis below is therefore about what the model is
given and what the runtime will accept.

| # | Request class (example) | Ideal behavior | What the current system makes possible | Likely failure level |
|---|---|---|---|---|
| A | Direct factual/current: `قسمت بعدی وان پیس کی میاد؟` | `web_search` then answer with sources | Tool is registered and described; prompt mentions web search only via one example; rule 7 can justify “I don’t know”; no explicit current-info policy | Prompt/instruction (B) → model decision (E); tool is available |
| B | Explicit tool: `وب سرچ بزن و تاریخ قسمت بعدی وان پیس رو پیدا کن.` | call `web_search` with a good query, then synthesize | Reaches model; schema says freshness/include_domains **required** (P0-1); result flows back normally | Tool-definition/provider-schema (C/D), then execution loop works |
| C | Implicit tool need (same as A) | same as A | same as A | same as A |
| D | Multi-step immediate: `اول سرچ کن، پنج مورد برتر رو پیدا کن، بعد ذخیره‌شون کن و تگ بزن.` | web_search → save/send results → tag | Prompt now explicitly supports this pattern; loop bounds allow ≤3 rounds ×5 calls; “save” requires a replied message and tagging requires a stored item, so the literal chain is only partly achievable (web results are not Telegram messages) | Combination: prompt now good; service-capability + loop bound (G); required-args defect adds friction |
| E | Explicit durable: `هر هفته این کار رو انجام بده.` | reference the previous turn’s work, call `create_task` with the full context, interpreter produces a weekly candidate | Reaches model; create_task available; **history is stripped**, so “این کار” has no referent unless the request is a reply to the AI message; interpreter returns null/asks rather than inventing | Context construction (A) + model decision (E) |
| F | Capability question: `چه ابزارهایی برای ذخیره کردن داری؟` | explain capabilities, call no tool | Stage 5 fixed the old hijack; prompt now says talking about a capability → answer in words, no tool; tool list is in-context | Works as designed; only the incomplete “You may” list may narrow the answer |
| G | Explicit save: `این رو ذخیره کن.` | `save` (deep save) of the replied-to message | Model must choose among `save`/`save_by_link`; `save` schema marks display_name+tags required though prompt forbids inventing them; reply context provided | Provider-schema defect (D) + model decision (E) |
| H | Clarification: `اون رو انجام بده.` | ask one clarifying question | Reaches model; no local interception; history stripped means “اون” has no referent unless reply | Context construction (A) |
| I | Correction: `اگر میخواستم خودم سرچ کنم بهت میگفتم.` | recognize the correction and redo with `web_search` | Reaches model; no instruction about corrections; if it is not a reply to the AI message, history is stripped → the earlier answer is invisible | Context construction (A) + prompt (B) |
| J | Multi-turn continuation: AI asked a question, owner supplies the value | use the prior turn, call the tool | Reaches model; standalone continuation loses history (P0-2); reply-to-AI continuation keeps the prior AI message text via `ReplyContext.ai_content` | Context construction (A) |

Cross-cutting: request classes that depend on prior turns (E, H, I, J) fail
deterministically on the **stripped history**, regardless of model quality;
request classes that depend on the tool contract (B, G, and parts of D) are
distorted for **40/55 tools** by the required-fields defect; classes restricted
to Gemini lose the instruction layer entirely.

---

## Root Causes

### P0

**P0-1 — Provider tool-schema requiredness is inferred, not declared; 40/55
tools advertise wrong `required` lists.**

- Location: `backend/ai/engine/dispatcher.py::Dispatcher._build_tool_definitions`
  (line 1851; the inference at lines 1861–1870: *“Params without a `default`
  are treated as required”*). Registry metadata (`parameters`) has no
  per-parameter required flag; only `required_arguments` /
  `required_any_arguments` exist at the tool level and are not the source of the
  schema `required` list.
- Evidence (measured on current HEAD): 40 tools carry a nonempty `required`;
  `save → [display_name, tags]`, `web_search → [query, freshness,
  include_domains]`, `delete → [count, mode, until_time, after_time,
  boundary_id, query, semantic]`, `task_transition → [task_id, action,
  expected_version, query, complete_steps]`, `translate_history → [language,
  instruction]`, `memory_list → [tier, query, limit]`. The text block omits
  requiredness entirely.
- Affected classes: G (save), B (web search), D (save/tag chain), plus every
  tool where optional parameters are described as optional in the prompt but
  required in the schema — i.e. the majority of the 55-tool surface.
- Why it causes wrong behavior: the model is told it must supply parameters the
  prompt forbids inventing (names/tags), or must supply mutually exclusive
  alternatives (task id **or** query); it responds by fabricating placeholders,
  omitting the tool, or picking a different tool with a simpler-looking schema.
  Provider-side schema ≠ tool-side semantics.
- Side: contract-side (application → provider serialization). Fixing it is a
  single change that improves every tool that carries optional parameters.
- Multi-tool: yes — one fix, all 40 affected tools.

**P0-2 — Conversation history never reaches the model; the static prompt
already exceeds the token budget.**

- Location: `backend/ai/prompt/builder.py::PromptBuilder.build` +
  `_trim_to_budget` (line 371) against `backend/ai/prompt/budget.py`
  (`DEFAULT_MAX_TOTAL_TOKENS = 8500`, line 26); tool block added by the
  dispatcher before budgeting.
- Evidence (measured): base prompt with the real 55-tool block =
  **9,349 estimated tokens** > 8,500 with **zero** history; with 20 realistic
  Persian history entries the rendered `[Conversation State]` contains **0**
  history rows. `_trim_to_budget` pops history until it fits; since the base
  never fits, it removes everything. The pinned test
  (`tests/test_context_architecture.py`) uses a synthetic smaller block and so
  never exercises the production case.
- Affected classes: E, H, I, J (multi-turn references), plus any correction or
  follow-up that is not a Telegram reply to the AI message; also weakens
  multi-step request D’s ability to reference earlier results across turns.
- Why it causes wrong behavior: the model is asked to interpret pronouns and
  follow-ups (“این کار”, “اون”, “ادامه بده”) with no prior context, so it
  guesses, asks to restate, or answers without the earlier facts. This looks
  like “the AI doesn’t understand”, but the data was removed before the
  provider call.
- Side: context construction. One fix (budget/serialization policy) restores
  context for every request.
- Multi-tool: yes — conversation context underpins all tool decisions that
  reference earlier turns.

### P1

**P1-1 — Gemini drops every system message except the last.**

- Location: `backend/ai/providers/gemini.py` lines 63–69 (`system_text =
  content` per system role) and 109–110 (`systemInstruction` set once), versus
  `dispatcher._build_messages` which emits four system messages.
- Evidence: code path is unambiguous; OpenAI-compatible providers are
  unaffected. Consequence: for Gemini-routed requests the model sees only
  `[Tool Context]` + tool schemas as system instructions — no rules, no
  output contract, no reply context, no memory/preferences.
- Affected classes: all, when the active/fallback provider is Gemini.
- Side: provider contract. Fix = merge/forward system messages
  (provider-specific), not an architecture change.
- Multi-tool: yes — restores the entire instruction layer for that provider.

**P1-2 — Instruction coverage gaps: capability list, JSON fallback schema,
current-info policy, correction handling.**

- Location: `backend/ai/prompt/template.py` (`SYSTEM_RULES_TEMPLATE` “You may”
  list; `OUTPUT_INSTRUCTIONS_TEMPLATE` rule 8 schema + `_ENFORCE_ACTION_NUDGE`
  at `dispatcher.py` line 120).
- Evidence: the fallback JSON schema enumerates 30 actions and omits
  `web_search`, `create_task`, `translate_history`/`summarize_history`,
  `send_message`, and the memory tools; the nudge repeats the same subset; no
  rule says “when the answer depends on current facts, use `web_search`”; no
  rule covers honoring a user’s correction.
- Affected classes: A/B/C (search policy), I (corrections), and
  non-native-tool-calling providers for every omitted capability.
- Side: prompt-side. Multi-tool: yes for the capability/gap items.

**P1-3 — Multi-step execution is intentionally bounded, and two paths skip the
model’s synthesis round.**

- Location: `dispatcher.py` `MAX_TOOL_ROUNDS = 3` (line 67), structured-action
  short-circuit (`response = replace(response, tool_calls=[], text="")` after
  execution), `_VERBATIM_READ_TOOLS` (line 87), salvage-at-limit.
- Evidence: the loop executes at most 3 tool rounds × 5 calls; JSON-emitting
  models get a deterministic summary (`_summarize_tool_results`) instead of a
  final model answer; verbatim read tools deliberately bypass continuation;
  a continuation provider failure still yields the real-result summary.
- Affected classes: D (complex immediate workflows), any request needing >15
  calls or needing the model to articulate a multi-step outcome.
- Side: runtime (bounds by design; the JSON-path exception is the sharpest edge
  for providers without native tool calling). Multi-tool: partially.

### P2

**P2-1 — Tool overlap and ambiguous surfaces.**
Five delete tools + `delete_save`; `save` vs `save_by_link`; `search` vs
`list_saves` vs `retrieve_save`; task/todo tools with id+version **or** query;
`todo_step_*` with step **or** step_query; the 7-parameter `delete`
mega-tool. The prompt disambiguates some cases, but selection still depends on
the model inferring distinctions that are only clear from the descriptions
(and the descriptions are contradicted by the `required` lists, P0-1).

**P2-2 — Every request now requires a provider round (Stage 5 consequence).**
Removing the fast path intentionally removed the “works with every provider
down” guarantee for explicit commands; a provider outage now degrades every
request, including simple saves. This is a documented trade-off, not a
comprehension defect, but it widens the observable failure surface.

**P2-3 — Two authorization vocabularies still read the raw message.**
`has_proactive_authorization` and `ground_ai_instruction`/
`_generation_authorized` inspect owner text. They are fail-closed permission
gates that run only after the model proposes an action and cannot select a
capability; they are correctly outside the routing boundary but are worth
naming as the remaining NL-reading code in the decision path.

### P3

- **P3-1 — Prompt duplication/bloat:** the tool surface is sent twice
  (15,166-char text block + 37,175-char native JSON), and the base system
  templates alone are ~21.7k chars; this is what pushes the prompt over budget
  (P0-2) and leaves little room for context.
- **P3-2 — Text tool block lacks requiredness and parameter semantics**
  (`name(type)` only), while the native schemas carry the (wrong) required
  list; the two representations can disagree.
- **P3-3 — Presentation/guidance constraints:** the 500-character output
  guideline can conflict with reporting multi-step results; `[Available Tools]`
  badges (`safe` / `needs-confirm` / `destructive-on-explicit-request`) are
  coarse.
- **P3-4 — Trigger edge:** `match_trigger` matches the first word
  case-insensitively but does not strip punctuation (`Nova:` does not
  activate); `Menu` is exact-equality. Cosmetic, but a real activation miss.

---

## Evidence

All locations are in the current tree at HEAD `eb4d85220aa0f73265b3e18e14a50ba05f0065d0`.

| # | Claim | Evidence (current source + measurement) |
|---|---|---|
| E1 | Stage 5 removed the deterministic router | No hits for `parse_command_intent`, `_try_local_fast_path`, `_build_deterministic_task_candidate`, `deterministic_task_candidate`, `parse_structural_predicate` in `backend/`; `backend/ai/actions.py` now 1,821 lines (was 3,148); `dispatcher.py` now 2,193 (was 2,450). Boundary pinned by `tests/test_semantic_intent_boundary.py`, `tests/test_regex_routing_removal.py`, `tests/test_intent_routing_boundary.py`, `tests/test_provider_tool_boundary.py` — **51 passed** when run during this audit. |
| E2 | Every request reaches the provider (no local selection) | `Dispatcher.dispatch` (line 213): only confirmation consumption and the media boundary run before `PROMPT_BUILD`; no fast path. `_apply_structured_action` (line 1679) parses **only** the model’s JSON. |
| E3 | Remaining NL readers are authorization/provenance only | `backend/ai/proactive.py::has_proactive_authorization` (line 122); `backend/ai/task_contract.py::_generation_authorized` (line 48) + `ground_ai_instruction` (line 78); `backend/ai/confirmation.py::is_explicit_confirmation` (line 214); `backend/ai/config_store.py::match_trigger`. |
| E4 | Required-fields defect | `dispatcher.py::_build_tool_definitions` line 1851; inference at 1861–1870; measured 40/55 tools with a nonempty `required`; examples in P0-1. `tests/test_capability_exposure_tools.py` only pins `task_list` having no `required`. |
| E5 | History stripped every request | `prompt/builder.py::_trim_to_budget` line 371; `prompt/budget.py` line 26 (`8500`). Measured build: est. input **9,349 tokens** with zero history (`within_budget=False`); 20 Persian entries → **0** rendered history rows; tool block **15,166 chars**. |
| E6 | Gemini system collapse | `gemini.py` lines 63–69, 109–110 vs `dispatcher._build_messages` line 1922 (four system messages). |
| E7 | Prompt composition and duplication | Templates: SYSTEM_RULES 10,436 / PLATFORM 707 / RUNTIME 2,088 / OUTPUT 8,499 chars; native tool JSON 37,175 chars for 55 tools (measured via `_build_tool_definitions`). |
| E8 | JSON fallback/nudge coverage | `OUTPUT_INSTRUCTIONS_TEMPLATE` rule 8 action list (no `web_search`, `create_task`, `send`, translate/summarize, memory); `_ENFORCE_ACTION_NUDGE` at `dispatcher.py` line 120. |
| E9 | Execution loop and bounds | `MAX_TOOL_ROUNDS = 3` (`dispatcher.py` line 67); `MAX_TOOLS_PER_TURN = 5` (`tools/executor.py` line 50); verbatim set line 87; structured-action short-circuit and salvage in `dispatch`; `_build_continuation_messages` line 1937. |
| E10 | create_task path | `tools/task.py`: `TaskInterpreter.interpret` call, `TaskUnsupportedError` → wizard signal (`open_taskloom_wizard`), `TaskSemanticCompletenessError` → bounded failure; no NL completeness gate (removed). `task_interpreter.py::interpret` line 468 runs a second provider call with `tools=[]` and the candidate schema. |
| E11 | Repository state | `git fetch origin`; `git rev-parse HEAD` = `git rev-parse origin/main` = `eb4d852…`; `git merge-base --is-ancestor HEAD origin/main` exit 0; `git status --short` shows only `?? telegram-self-bot/`. |
| E12 | Tool inventory | Registry dump (55 tools, permissions, parameters, declared required/any/consumable) executed read-only against `create_default_registry`. |
| E13 | No live verification | No Telegram message or SQL statement was issued during this audit; measurements are in-process introspection. The full test suite was not re-run in this audit (the four new boundary suites were: 51 passed); `IMPLEMENTATION_REPORT.md` records 5,126 passed / 26 skipped at Stage 5. |

---

## Recommended Next Implementation Stage

**Next stage: Stage 6** (as stated by the current `IMPLEMENTATION_REPORT.md`;
Stage 5 is the completed “sole interpreter” refactor). This is a recommendation
only — nothing below is implemented by this investigation.

The smallest highest-leverage scope for Stage 6, in impact order, preserving
every architectural constraint (AI as sole intent interpreter; no second
router; ToolRegistry as the allowlist; ToolExecutor as the sole execution
authority; ProviderManager and RuntimeSupervisor unchanged):

1. **Fix the provider tool contract (P0-1).** Make the schema `required` list
   come from declared tool semantics instead of “absence of a `default`”, so
   optional/alternative parameters (`save.display_name/tags`,
   `web_search.freshness/include_domains`, `delete.*`, `task_transition.query`,
   `translate_history.*`, …) stop being advertised as mandatory. This is a
   contract/serialization fix; no tool behavior changes.
2. **Stop the conversation-history eviction (P0-2).** Re-derive the prompt
   budget policy so the static system prompt + tool block cannot consume the
   entire ceiling (e.g. budget the tool block and history separately, raise the
   ceiling, or trim the duplicated tool surface). Desired invariant: a bounded
   number of recent turns always reaches the model; the existing trim behavior
   may remain as a last resort.
3. **Forward all system messages on Gemini (P1-1).** Merge/forward the four
   system messages into Gemini’s `systemInstruction` (or one combined system
   message), so Gemini-routed requests receive the instruction layer the other
   providers receive.
4. **Close the instruction gaps (P1-2).** Add a current-information policy
   (“answer from `web_search` when the answer depends on current facts”), align
   the JSON fallback schema and the recovery nudge with the real capability
   surface (or state explicitly that they cover only the listed commands), and
   add a correction-handling rule. No keyword routing — this is prompt text
   only.

Verification for the stage should include: schema-level assertions for a
representative set of optional/alternative tools; a prompt-build test using the
**real** 55-tool block asserting history survives; a Gemini message-mapping
test asserting every system section reaches `systemInstruction`; and a live
behavior check on the diagnostic matrix classes (A/B/D/E/I/J) against a real
provider, which this audit could not perform.

**Stage completed: Investigation only**
**Next stage: Stage 6**
