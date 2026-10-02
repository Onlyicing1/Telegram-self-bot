# Implementation Report

> **This report was rebuilt from zero.** Every section below describes the
> repository as it stands after this change; no section, table, status list or
> wording was carried over from the previous report. Facts were re-verified
> against the current tree, the current test run, and `git log` before being
> written down.
>
> **Verification honesty rule:** nothing here claims the running bot was
> exercised against live Telegram, or that any SQL was executed against the
> live Supabase project. See §11 for exactly what was and was not verified.

---

**Stage completed: Stage 5**

**Next stage: Stage 6**

*Derivation (not guessed):* the previous report numbered its work units
`Part 1` … `Part 4` and then carried one additional unnumbered unit
("Audit — Intent-routing boundary"). The latest completed numbered unit was
**Part 4**, so this architectural correction is the next one: **Stage 5**.

---

## 1. What semantic routing existed, and where

The AI decision path contained a hand-written, deterministic semantic
interpreter that read the owner's raw natural-language message and decided,
**before any provider round**, which capability to run.

**Primary location — `backend/ai/actions.py`.** The module carried a
~1,450-line section after the structured-action contract:

| Element | Role |
|---|---|
| `parse_command_intent(text, has_reply, reply_text)` | The router. Returned a hard `ActionParseResult(kind="executable", action=…, tool_calls=[…])` built from the user's own words. |
| `_is_scheduling_intent(words)` | Decided "this message requests a schedule" from an **unanchored** token-membership test over the whole message. |
| `_FA_RECUR_WORDS` / `_EN_RECUR_WORDS` | Cadence vocabulary: `هفتگی`, `روزانه`, `ماهانه`, `weekly`, `daily`, … |
| `_FA_ACTION_VERBS` / `_EN_ACTION_VERBS` | Action-verb gate for the schedule decision. |
| `_INTERVAL_INTRO`, `_FA_PLAN_WORDS`, `_EN_PLAN_WORDS`, `_FUTURE_REF_WORDS` | Interval / plan / future vocabulary. |
| `_is_event_intent`, `_has_future_clock_request` | Event and clock-anchor intent detection. |
| `_DELETE_STEMS`, `_SAVE_STEMS`, `_SEND_STEMS`, `_EN_DELETE`, `_EN_SAVE`, `_EN_SEND`, `_EN_NEGATION`, `_EN_META_FRAME_WORDS`, `_IMPERATIVE_SUFFIXES` | Save / delete / send / negation vocabulary. |
| `_WRITE_TOKENS`, `_is_write_token`, `_extract_write_text` | "Write this now" detection. |
| `_THIS_TOKENS`, `_LAST_TOKENS`, `_MESSAGE_TOKENS`, `_DEEP_TOKENS`, `_ID_TOKENS`, `_ALL_DELETE_WORDS`, `_TODAY_WORDS`, `_SEMANTIC_DELETE_*`, `_SEMANTIC_SEARCH_WORDS`, `_ANALYSIS_STEMS`, `_PREVIEW_WORDS`, `_SAVE_LIST_WORDS`, `_DB_WORDS`, `_USERNAME_WORDS`, `_BIO_*`, `_STATUS_WORDS` | Per-capability intent vocabularies. |
| `_extract_count`, `_extract_until_time`, `_extract_after_time`, `_extract_message_id`, `_extract_save_code`, `_extract_single_save_code`, `_is_semantic_delete`, `_extract_semantic_query`, `_en_list_saved`, `_parse_status_intent`, `_is_history_analysis_intent`, `save_metadata_requested`, `explicit_no_tags_requested` | Target/predicate extraction and metadata-intent detection. |
| `_FA_CLOCK_WORDS`, `_EN_CLOCK_WORDS`, `_words_contain_clock_anchor`, `_text_has_clock_anchor` | Clock-anchor detection used both by the parser and the task gate. |

**Pre-provider fast path — `backend/ai/engine/dispatcher.py`.**

| Element | Role |
|---|---|
| `_try_local_fast_path()` + its call site in `dispatch()` | Ran **before** `_stage("PROMPT_BUILD")`; executed the parser's tool calls through the real `ToolExecutor` with no provider round. |
| `_build_deterministic_task_candidate()` + `extra["deterministic_task_candidate"]` | Synthesized an interval task candidate from the raw message, so `create_task` could persist without any model. |
| `_reply_text()` | Fed trusted reply text into the parser for target resolution. |
| `_apply_structured_action()` pre-pass | Ran `parse_command_intent` over the owner's message *before* parsing the model's own JSON. |

**Additional natural-language semantic gates.**

| Location | Role |
|---|---|
| `backend/ai/tools/task.py` (completeness gate) | Re-used `_is_scheduling_intent` / `_is_event_intent` / `_text_has_clock_anchor` to decide, from the raw request, that no schedule had been expressed → wizard, before any provider call. |
| `backend/ai/tools/task.py` (profile-fidelity gate) | Overrode the model's chosen action (`send_message` → `bio_set_text`) when the raw request mentioned a bio and a change verb. |
| `backend/ai/tools/save.py` | `explicit_no_tags_requested` forced an empty tag list from the raw message, over the model's proposal. |
| `backend/ai/semantic_delete.py` | `parse_structural_predicate(text)` read "دو کلمه انگلیسی" out of the raw message and built the delete predicate. |

### The live defect this produced

`اول یه سرچ بزن و پنج انیمه برتر در حال پخش جدید رو پیدا کن، بعد نتیجه رو سیو کن، و بعد تگ بزن انیمه های هفتگی`
— "…tag the **weekly** anime" — contains `هفتگی` as an **attributive adjective
describing the anime**, not as a cadence request. The unanchored membership
test read it as a schedule, `parse_command_intent` emitted a hard `create_task`
call, and the user saw `"🗓 Creating task..."` with **no provider round at
all**. This was diagnosed in `INVESTIGATION.md` at HEAD `62dadfe`.

---

## 2. What was removed

| Removed | File(s) | Why it was necessary |
|---|---|---|
| `parse_command_intent` and its entire semantic vocabulary section | `backend/ai/actions.py` | It was the router itself: a parallel semantic interpreter competing with the model. |
| `_try_local_fast_path()` + call site | `backend/ai/engine/dispatcher.py` | It let raw user text pick a tool without the model. |
| `_build_deterministic_task_candidate()` + `extra["deterministic_task_candidate"]` | same | It fabricated a durable task definition from the raw message. |
| `_reply_text()` | same | Its only consumer was the removed pre-pass. |
| The `parse_command_intent` pre-pass inside `_apply_structured_action()` | same | It overrode the model's own structured output with a locally-derived action. |
| The `deterministic_task_candidate` branch inside `CreateTaskTool` | `backend/ai/tools/task.py` | Dead once nothing set the key. |
| The natural-language completeness gate | same | It decided "is this a schedule?" from keywords. Replaced by the existing interpreter/candidate validation → wizard path (§4). |
| The profile-fidelity action override | same | Deterministic code overrode the model's chosen tool. |
| `explicit_no_tags_requested` + `_request_text` | `backend/ai/tools/save.py` | A keyword override of the model's tag decision. |
| `parse_structural_predicate` + its number-word / word-marker vocabulary | `backend/ai/semantic_delete.py` | It read the delete predicate out of the raw message. |
| `finish_state = "local_fast_path"` → `"local_boundary"` | `backend/ai/engine/dispatcher.py` + tests | The old name described a router that no longer exists. |

Net effect in `backend/`: **+193 / −2,013 lines**. `backend/ai/actions.py`
went from 3,188 → 1,821 lines; `dispatcher.py` from 2,450 → 2,193.

---

## 3. What remains as technical validation / parsing (Category B)

None of this chooses a capability. All of it validates, bounds, normalizes or
executes something the AI already selected.

| Kept | Why it is technical |
|---|---|
| `validate_action()` and every validator in `actions.py` | Validates the **AI's** structured action: known action name, known fields, count bounds, target scope. |
| `resolve_tool_calls()` | Maps a validated action to concrete tool-call arguments. |
| `parse_action_text()` / `extract_json_object()` | Extracts and validates the model's own JSON action object. |
| `_extract_telegram_link()` + `_TELEGRAM_LINK_RE` | Parses a fixed transport URL form. |
| `_SAVE_CODE_RE` (`^[A-Z0-9]{1,12}$`) | Validates a fixed artifact identifier shape. |
| `_TOKEN_RE`, `_tokenize` in `actions.py` | Provenance tokenization for **authorization** (see §6). |
| `_MAX_WORD_COUNT`, `spec_from_dict`, `build_matcher*`, `normalize_text`, `count_words` in `semantic_delete.py` | Validate the model's structured predicate and apply it to already-fetched message text. |
| `_JSON_BLOCK_RE` in `task_interpreter.py` / `task_execution.py` | Extracts a fenced JSON block from **model output**. |
| `preparation_policy.derive_policy` + its regexes | Parses explicit content constraints (language / length / source) and **enforces** them on generated content. |
| `_MAX_MESSAGE_ID_DIGITS`, `_WAIT_ISO_RE`, `coerce_int` in `task_contract.py` / `task_candidate.py` | Identifier/format bounds. |
| `_semantic_completeness_error`, `initial_next_run`, schedule validation in `task_creation.py` | Validate the AI's structured task candidate. |
| `ToolExecutor`, permission gate, `MAX_TOOLS_PER_TURN`, `MAX_TOOL_ROUNDS` | Execution authority and bounds. |
| The isolated media boundary (`_try_media_analysis`, `media.py`, `media_ai_service`) | Deterministic **target resolution** from runtime identifiers; never reads intent. Unchanged. |
| `Menu` exact-equality guard, trigger-word matching | Literal command surface, not semantic routing. |

---

## 4. How the AI becomes the semantic decision-maker

The flow is now exactly:

```
Telegram message (ai_unified)
  -> AIRequest
  -> Dispatcher.dispatch()
       confirmation round-trip (local, non-provider)
       isolated media boundary (local, non-provider, context-isolated)
       PromptBuilder  ->  ProviderManager  ->  Provider
       native tool call | _apply_structured_action(model JSON)
  -> validate (validate_action / resolve_tool_calls)
  -> ToolExecutor.execute_calls
  -> existing service
```

**Where the immediate-vs-durable distinction now lives.** The prompt contract
in `backend/ai/prompt/template.py` — the "Immediate workflows vs durable
tasks" rule. It was already there; before this change it was **bypassed** for
any message the deterministic parser claimed. Now it governs every request
that reaches the model.

**How `create_task` completeness is judged.** Previously a keyword gate ran
before the provider and opened the wizard when no schedule expression was
found. That gate is gone. Completeness is now judged on the **AI's own
structured proposal**: `TaskInterpreter` returns JSON `null` (the existing NULL
RULE — "never invent missing schedule… return null") and the candidate validator
raises `TaskSemanticCompletenessError`, which routes to the **same existing
Taskloom wizard** signal the delivery layer already consumes. Task validation
is therefore not weakened — it moved from keyword detection to validating the
model's structured candidate.

**Where a "safe, unambiguous schedule" refusal comes from.** The interpreter's
failure category, produced when the model declines — an honest, content-free
message plus the wizard signal.

---

## 5. Safety boundary — what did *not* change

- `ToolRegistry` still defines the entire capability surface. The model cannot
  reach Telegram RPC, SQL, a shell or the filesystem: none of those are tools,
  and `registry.get(unknown)` returns `None` → `not_found`.
- `ToolExecutor` is still the **sole** caller of `tool.execute()`. The
  dispatcher still never calls a tool directly (pinned by
  `test_tool_executor_remains_the_sole_execution_authority`).
- Permission levels, confirmation gating (`ADMIN_ONLY` /
  `CONFIRMATION_REQUIRED`), schema validation, `malformed_arguments` rejection
  and argument bounds are unchanged.
- `ProviderManager` selection / fallback / retry / cooldown unchanged.
- `RuntimeSupervisor` remains the single lifecycle and recovery authority.
- No provider contract changed.
- `create_task` is still registered and still offered to the model on every
  request; `TaskCreationService`, `TaskRepository`, the scheduler and the task
  executor are untouched.

---

## 6. Authorization vocabulary that intentionally remains

Two small vocabularies still read the owner's natural language. Both are
**authorization / provenance**, not capability routing, and both fail closed.
Removing them would weaken a guarantee §4 and §7 of the brief require
preserving.

| Location | Question it answers | Why it stays |
|---|---|---|
| `actions.py` → `_USERNAME_WORDS`, `_has_bio_mention`, `_has_bio_change_intent`, `_write_text_present`, `_is_write_token`, `_tokenize`; consumed by `task_contract.ground_ai_instruction` | "Is AI-generated content authorized for this durable task?" | By the time it runs, the model has already proposed `create_task` and a structured candidate. It never selects a capability. `ground_ai_instruction` fails closed: an instruction the owner did not ask for is **dropped**, never repaired from a model paraphrase. Removing it would let a provider invent generation instructions for a static task. |
| `proactive.py` → `has_proactive_authorization` | "Does the owner's message authorize bounded extra work?" | A permission gate, not a router. Fail-closed phrase vocabulary, per-request only, never persisted. Removing it would **weaken authorization**. |

Both are isolated and labelled in-place so the boundary stays auditable, and
`test_regex_routing_removal.py` asserts the *absence* of the retired routing
vocabulary around them.

---

## 7. Regex audit

**Goal audited:** *zero regex used to interpret natural-language intent or
select a tool/action.* (Not "zero regex anywhere".)

**Category 1 — semantic intent routing: ZERO remaining.**
Every regex that previously participated in command/intent detection is gone
(there were none after Part 4; the token-vocabulary form has now been removed
too). Verified by `test_no_command_parser_symbol_exists` and
`test_no_recurrence_or_action_vocabulary_remains`.

**Category 2 — technical parsing/validation (all remaining regexes).**

| File | Regex | Classification |
|---|---|---|
| `ai/actions.py:171` | `_TELEGRAM_LINK_RE` | Telegram URL form — protocol parsing |
| `ai/actions.py:780` | `_SAVE_CODE_RE` | save-code artifact shape `^[A-Z0-9]{1,12}$` — identifier validation |
| `ai/actions.py:1760` | `_TOKEN_RE` | provenance tokenization for authorization (§6) |
| `ai/semantic_delete.py` | `_DIACRITICS_RE`, `_TOKEN_RE`, `_EN_WORD_RE`, `_FA_LETTER_RE`, `_HAS_LETTER_RE`, `re.sub` (ZWNJ/zero-width) | text normalization + word counting to apply an **AI-chosen** predicate |
| `ai/task_interpreter.py:22`, `ai/task_execution.py:71` | `_JSON_BLOCK_RE` | fenced-JSON extraction from **model output** |
| `ai/task_contract.py:179`, `:1261` | `re.findall(r"\d+")`, `_WAIT_ISO_RE` | message-id digit bounds; ISO timestamp format |
| `ai/task_candidate.py:56` | `_COMPOUND_KEY_RE` | structured schedule-key shape from the **model's JSON** |
| `ai/preparation_policy.py` (6) | explicit length / language constraint patterns | content-policy **enforcement** on generated content |
| `ai/tools/delivery.py` | markdown/emoji normalization | presentation |
| `services/web_search_service.py`, `services/save_service.py`, `services/retrieve_service.py`, `services/ghost_seen_v2.py`, `services/history_ai_service.py` | URL / code / text normalization | service-layer parsing |
| `helper/font_style.py`, `bot/handlers/ai_stt_settings.py` | font/voice rendering | presentation |

**Category 3 — tests/docs:** regex literals in `tests/` pin the audit itself
(`test_regex_routing_removal.py`).

**Verified:** no remaining regex can take a natural-language user message and
directly choose `create_task`, `save`, `search`, tagging, or any other tool.

---

## 8. Exact files changed

### Backend (6 files, +193 / −2,013)

| File | Why |
|---|---|
| `backend/ai/actions.py` | Removed the entire semantic intent-routing section; kept validation/resolution. Docstring rewritten to state the boundary. |
| `backend/ai/engine/dispatcher.py` | Removed `_try_local_fast_path`, `_build_deterministic_task_candidate`, `_reply_text`, and the `_apply_structured_action` pre-pass. Docstring updated. `finish_state` renamed. |
| `backend/ai/tools/task.py` | Removed the natural-language completeness gate and the profile-fidelity action override; removed the dead deterministic-candidate branch. |
| `backend/ai/tools/save.py` | Removed the `explicit_no_tags_requested` keyword override and its now-dead `_request_text` helper. |
| `backend/ai/semantic_delete.py` | Removed `parse_structural_predicate` and its number-word / word-marker / language vocabulary. Docstring rewritten. |
| `backend/ai/task_contract.py` | Comment only: labels the retained authorization vocabulary as provenance, not intent routing. |

### Tests (30 modified, 1 renamed, 1 deleted, 1 added)

- **Added:** `tests/test_semantic_intent_boundary.py` (10 tests, 15 cases) —
  the architectural proof (§9).
- **Renamed:** `tests/test_25_fast_path.py` →
  `tests/test_provider_tool_boundary.py` (the fast path is gone; the remaining
  two tests are about the provider→executor boundary).
- **Deleted:** `tests/test_task_show_intent.py` (its entire subject was the
  removed parser).
- **Rewritten:** `tests/test_regex_routing_removal.py` (now pins the new
  boundary + the regex classification), `tests/test_intent_routing_boundary.py`
  (now the prompt/tool contract), `tests/test_task_wizard_nl_bridge.py`,
  `tests/test_task_semantic_triggers.py`,
  `tests/test_task_nl_interval_creation.py`, `tests/test_tool_health_audit.py`.
- **Tests removed because their subject no longer exists:** 206 test functions
  across 25 files asserted deterministic routing outcomes (`parse_command_intent`
  verdicts, "runs fast path without provider", schedule-word detection). They
  were deleted rather than re-pointed, because asserting the absence of a
  router is now covered by the new suite.
- **Dead code cleaned:** orphaned decorators, unused imports, unused string
  tables, unused `_make_dispatcher` helpers, and the now-stale docstrings that
  described the removed router.

---

## 9. Tests added / changed

`tests/test_semantic_intent_boundary.py` drives the **real** `Dispatcher`,
`ToolRegistry`, `ToolExecutor` and `CreateTaskTool`. Only the provider is
scripted. It covers the brief's cases A–G:

| Case | Test | Property |
|---|---|---|
| A | `test_immediate_workflow_reaches_the_model_and_selects_nothing_locally` | Immediate multi-action, the production string, the mixed weekly-topic variant and the English equivalent all reach the provider; `finish_state != local_boundary`; no local status label. |
| B | `test_production_request_never_produces_creating_task_locally` | The exact production string: provider called, no `Creating task` label, no tool results, no `ai_action == create_task`. |
 B | `test_cadence_words_carry_no_routing_power` | `هفتگی` / `ماهانه` / "weekly" all reach the model identically. |
| C | `test_durable_task_request_reaches_the_model_with_create_task_available` | The model chooses `create_task`; it is executed through the single `ToolExecutor` boundary. |
| C | `test_create_task_is_offered_to_the_model_in_the_tool_schemas` | `create_task`, `web_search`, `save`, `update_save_tags` are all in the registry. |
| D | `test_capability_mention_reaches_the_model_with_nothing_executed` | A capability/topic mention reaches the model and executes nothing. |
| E | `test_explicit_save_is_handled_by_the_model_not_the_parser` | "این رو سیو کن" is routed by the model's proposal through the executor. |
| G | `test_tool_executor_remains_the_sole_execution_authority` | The dispatcher never calls `tool.execute()` directly. |
| G | `test_provider_never_gets_arbitrary_execution_surface` | An invented action resolves to no tool call. |
| G | `test_media_context_isolation_is_untouched` | The isolated media boundary still runs before prompt construction. |

`tests/test_regex_routing_removal.py` (rewritten) pins the absence of every
retired router symbol and vocabulary across the decision-path modules, and
classifies the remaining regexes.

---

## 10. Test results

| Run | Result |
|---|---|
| Full suite **before** this change (HEAD `62dadfe`) | 5460 passed, 26 skipped |
| Full suite **after** this change | **5126 passed, 26 skipped** (117.71 s) |
| Delta | −334 net (206 deterministic-routing tests deleted, 1 test file deleted; 15 new boundary cases + prompt-contract and wizard tests retained/rewritten) |
| `python -m compileall backend/` | clean |
| `python -m compileall tests/` | clean |
| Semantic-routing grep over `backend/` | **0 hits** |
| Semantic-routing grep over `tests/` | **0 hits** (excluding the audit file, which names the retired symbols deliberately) |
| Outer worktree `/home/daytona/codebase` | untouched (same 6 pre-existing modified files) |

---

## 11. Verification status — what was and was not proven

**Verified in this change**

- Full test suite green (5126 passed, 26 skipped).
- The architectural property is pinned by tests, not by argument: no module in
  the decision path contains a retired router symbol or cadence/action
  vocabulary; every request reaches the provider; the executor remains the sole
  execution authority; `create_task` remains available.
- The local reproduction of the live bug (`"Creating task..."` with
  `provider calls: 0`) was driven before the change and is now impossible by
  construction.

**NOT live-verified**

- No live Telegram message was processed. The owner-visible behaviour of the
  AI on real requests (does it actually pick `save` + tag rather than
  `create_task` for the anime request?) is **model behaviour**, not a code
  invariant, and was not exercised against a real provider here.
- No SQL was executed; no Supabase project was touched.

**Database / schema:** **no change.** No migration, no schema edit, no SQL.
`DATABASE_ARCHITECTURE.md` is untouched and remains accurate.

**Provider contract:** **no change.** No provider module was modified; the
tool-schema shape and the prompt/tool architecture are unchanged.

---

## 12. Limitations and deferred work

1. **Intent quality is now the model's responsibility.** Removing the parser
   removes a source of false negatives *and* false positives, but it also means
   misclassification (in either direction) is a prompt/model-behaviour issue.
   The prompt contract is the mitigation; it is not a guarantee.
2. **Every request now costs a provider round.** The former fast path made
   some commands work with every provider down. That reliability guarantee is
   intentionally given up by this architectural correction; it was not
   preserved by reintroducing any local router.
3. **Provider-free durable task creation is gone.** `create_task` now always
   goes through `TaskInterpreter`, so creating a task requires a working
   provider. This is a direct, intended consequence of "the AI decides
   intent".
4. **`_generation_authorized` still reads the raw message** (§6). It is
   fail-closed authorization, not capability routing. Moving it to a purely
   structured signal would be a separate, non-trivial change to the
   preparation contract.
5. **`proactive.py` still reads the raw message** (§6) as an authorization
   gate. Same reasoning.
6. The 206 removed tests are **not** replaced one-for-one. That is deliberate:
   they asserted that a router existed. The new suite asserts that it does
   not.

---

## 13. Git status

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `m14-stt` (pushed to `main`) |
| Parent commit | `62dadfefec2c88a459d61c4b8bd1f731b74839e6` |
| Working tree at report time | only the files listed in §8 modified |
| Outer worktree `/home/daytona/codebase` | untouched; 6 pre-existing modified files + untracked `.m14/` (never staged) |
| Commit hash | see the delivery commit for this change |

---

## 14. Final current state

**Natural-language intent belongs to the AI.** Deterministic code validates,
authorizes, bounds and executes the AI's structured decision. There is no hidden
second semantic interpreter competing with the model: the decision boundary is
now the prompt/tool contract plus the model, and the enforcement boundary is
`ToolRegistry` → `ToolExecutor` → existing service — the same one that existed
before.