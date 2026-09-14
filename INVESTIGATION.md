# INVESTIGATION — AI task-creation semantic completeness

> **Investigation only. No production code, tests, configuration, schema, or UI
> were modified.** This document is the full replacement of the previous
> `INVESTIGATION.md`; nothing from the earlier investigation is retained.

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Revision audited | `2551970` |
| Remote drift | `origin/main` advanced past the audited revision before this document was delivered (a `feat: enforce semantic completeness for task creation` commit is now present upstream). **All findings below describe `2551970`; they are not a statement about the newer revision.** |
| Scope | Does the current task-creation path reject *schema-valid but semantically incomplete* candidates before persistence, for every supported task action? |
| Verdict | **No — semantic completeness is enforced for exactly one action (`send_message`) at the persistence boundary.** |
| Changes made | none (this document only) |

---

## 1. Objective and method

The question is not "does the candidate parse" but "can a candidate be persisted
whose selected action cannot execute deterministically, or whose required user
intent the provider invented".

The audit traced, in source order:

```
user request
  → TaskInterpreter.interpret            (provider call, tools=[])
  → TaskCandidate.from_untrusted        (untrusted candidate boundary)
  → CreateTaskTool._execute             (NL-only repair gates, destination/trigger resolution)
  → TaskCreationService.create          (deterministic creation boundary)
  → TaskRepository.create_task          (ai_tasks)
  ... later ...
  → TaskScheduler.run_once              (occurrence creation)
  → TaskExecutionCoordinator.execute    (registry check, preparation, ToolExecutor)
  → Tool.execute                        (per-tool argument requirements)
```

Each registered tool's `execute` was read to derive the semantic inputs it
actually needs at runtime; those requirements were then compared against what
the pre-persistence layers validate.

---

## 2. Enforcement layers (confirmed)

| # | Layer | File / function | What is enforced |
|---|---|---|---|
| 1 | Untrusted candidate boundary | `backend/ai/task_candidate.py` — `TaskCandidate.from_untrusted`, `_canonicalize_action` | Envelope field set, label/timezone bounds, 1–5 actions, action-object shape plus aliases (`tool`→`name`, `parameters`/`args`→`arguments`), interval schedule canonicalization, `chat_name` bounds, `deliver_result`/`notify_on_outcome` bool types, bounded payload. **Action semantics: `send_message` aliases only** (`_SEND_ACTION_ALIASES = {"send", "send_message", "write_message", "send_text"}`), which must carry bounded nonblank `text`. Any other action name is returned unchanged as `{"name": name, "arguments": args}`. |
| 2 | Deterministic creation boundary | `backend/ai/task_creation.py` — `TaskCreationService.create` | Required/extra field sets, task↔schedule timezone agreement, `parse_schedule`, first-occurrence computation, `validate_ai_instruction`. **No per-action argument semantics.** |
| 3 | NL-path repair gates (create_task only) | `backend/ai/tools/task.py` — `CreateTaskTool._execute` | (a) **source-fidelity gate**: `derive_policy(request)` active → force `ai_instruction = request` verbatim; (b) **profile-fidelity gate**: request mentions bio **and** has change intent/write verb → rewrite `send_message` actions to `bio_set_text`. Also resolves `chat_name`→`chat_id` and event-trigger names→ids from trusted context. |
| 4 | Occurrence execution boundary | `backend/ai/task_execution.py` — `TaskExecutionCoordinator.execute` | `self.executor._registry.get(name) is None` → occurrence fails `unregistered_action`; prepared-call structure re-proof (`_validate_prepared_calls`); deterministic content policy re-proof (`_enforce_content_policy`); `chat_id` injected from `notification_destination`. |

**Confirmed structural consequence:** the candidate layer intentionally does not
consult the tool registry. Its own source comment states the unknown-action case
"remains the existing execution-time registry check's responsibility and fails
safely there", and
`tests/test_task_candidate_contract.py::test_registered_non_send_tool_survives_but_unknown_tool_fails_execution`
pins that behaviour. Therefore the *persistable* action vocabulary is unbounded
(any nonblank name + dict arguments), and per-action completeness exists only
where a layer names the action explicitly — which today means `send_message`
only (plus the NL-path bio/username repairs).

---

## 3. Action-by-action classification

Requirements below are derived from each tool's `execute` implementation.
"Pre-persistence check" = validated before `repository.create_task`.

| Action | Execution-required semantic input (source) | Pre-persistence check | Verdict |
|---|---|---|---|
| `send_message` | bounded nonblank `text` (`SendMessageTool.execute` rejects missing/blank/oversize) | `_canonicalize_action` enforces it at the candidate boundary; content policy on the AI path | **PASS** |
| `bio_set_text` | non-empty profile text **or** `ai_instruction` authorizing generation (`BioSetTextTool` calls `bio_service.do_text(context.owner_id, text)` with `text = arguments.get("text", "")`) | NL path only (gates 3a/3b); candidate layer and `TaskCreationService` enforce nothing | **GAP** |
| `username_set_text` | same as bio (`UsernameSetTextTool`, same `text` default) | interpreter prompt names it; **no username equivalent of gate 3b exists** (`CreateTaskTool` imports only `_has_bio_change_intent`, `_has_bio_mention`, `_write_text_present`, `_tokenize`) | **GAP** |
| `bio_set_template` / `username_set_template` | non-empty `template` (tool returns "Missing template argument.") | none | **GAP** |
| `bio_set_mood` / `username_set_mood` | `mood`; empty string is accepted by the tool (`do_mood(owner_id, "")`) | none | **GAP** |
| `bio_on`, `bio_off`, `bio_show`, `get_bio`, `username_on`, `username_off`, `username_show` | none | n/a | **PASS** |
| `delete` | explicit scope: `count` 1–500 **or** a filtered scope (`mode` ∈ all/until_time/until_message/filtered, `until_time`, `after_time`, `boundary_id`, `query`, `semantic`) | none (tool fails closed at execution) | **GAP** |
| `delete_replied` | `context.extra["reply_msg"]` | none; **the coordinator never injects `reply_msg` for a scheduled occurrence** | **GAP** |
| `save` | `context.extra["reply_msg"]` | none; never available to a scheduled occurrence | **GAP** |
| `save_by_link` | non-empty `link` starting with `http` | none | **GAP** |
| `delete_by_id` | integer `message_id` | none | **GAP** |
| `delete_message_by_id` | positive `message_id` | none | **GAP** |
| `delete_messages_by_ids` | non-empty list of positive ints | none | **GAP** |
| `search` | non-empty `query` | none | **GAP** |
| `list_saves` | optional `limit` | n/a | **PASS** |
| `list_recent_messages` | optional `limit`; needs trusted `chat_id` (injected) | n/a | **PASS** |
| `retrieve_save` | valid `save_code` + trusted `chat_id` | none for `save_code` | **GAP** |
| `memory_store` | non-empty `content` (bounded by `MAX_MEMORY_ENTRY_CHARS`) | none | **GAP** |
| `memory_list` | optional `tier`/`query`/`limit` | n/a | **PASS** |
| `settings_get` | `key` | none | **GAP** |
| `settings_set` | `key` + `value`; `PermissionLevel.ADMIN_ONLY` (confirmation expected) | none pre-persistence; the occurrence-time confirmation path was not traced in this pass | **UNCERTAIN** |
| `organize_clean` | none, but `PermissionLevel.DANGEROUS` (confirmation expected) | none pre-persistence | **UNCERTAIN** |
| `organize_list`, `database_stats` | none | n/a | **PASS** |
| `account_show` | optional `fields` allowlist; invalid values are rejected by the tool | none pre-persistence; invalid values fail closed at execution | **PASS** (fails closed) |
| `task_list`, `task_inspect` | none | n/a | **PASS** |
| `task_transition` | task id + `expected_version` (+ status) | none | **GAP** |
| `task_delete` | task id + `expected_version` | none | **GAP** |
| `create_task` | non-empty `request` | none | **GAP** |
| `web_search` | not read in this pass | — | **UNCERTAIN** |
| any unregistered action name | — | candidate accepts a nonblank name; resolved only at execution (`unregistered_action`) | **GAP** |

No action other than `send_message` has a pre-persistence semantic check.

---

## 4. Cross-cutting findings

1. **Empty static content is accepted with no generation authorization.**
   `{"name": "bio_set_text", "arguments": {"text": ""}}` (and the username
   equivalent) passes `TaskCandidate.from_untrusted` and
   `TaskCreationService.create`, then calls `do_text(owner_id, "")` at every
   occurrence. The rule "an empty static field must not be valid merely because
   `ai_instruction` exists" is not enforced in either direction at the boundary.

2. **The provider can attach generation semantics the user never requested.**
   `ai_instruction` is only *repaired* to the verbatim request when
   `derive_policy(request)` is active (gate 3a in `CreateTaskTool`). Otherwise a
   model-supplied `ai_instruction` passes `validate_ai_instruction` (nonblank +
   length bound) and is persisted for any action.

3. **Context-dependent actions are persistable but never executable.**
   `TaskExecutionCoordinator.execute` injects `chat_id` from
   `notification_destination`, but never `reply_msg`. `save` and
   `delete_replied` therefore cannot succeed for any scheduled occurrence.

4. **Destination trust is only partially enforced.** `notification_destination`
   is carried through `CreateTaskTool` as a plain dict. `chat_name` is popped
   and resolved, and `chat_id` is overwritten when a trusted request chat id
   exists or a name resolves — but the candidate layer does not strip a numeric
   `chat_id` emitted by the model, so a model-supplied value can survive when no
   trusted id is available. Recorded as a hypothesis (see §5).

5. **Schedule invention is not a gap.** `parse_schedule` rejection is
   unconditional, and interval shapes are canonicalized deterministically.

---

## 5. Confirmed facts vs hypotheses

**Confirmed from source (read directly)**

- `_canonicalize_action` special-cases only the `send_message` alias set and
  raises `TaskCandidateError("message action requires bounded nonblank text
  content")` otherwise it returns the action unchanged.
- `TaskCreationService.create` validates fields, timezone agreement, schedule,
  and `ai_instruction` only.
- `TaskExecutionCoordinator.execute` is where registry membership is first
  checked (`unregistered_action`).
- `CreateTaskTool._execute` contains exactly two semantic repair gates
  (source fidelity, bio profile fidelity) and resolves destinations/triggers
  from trusted runtime context.
- Every tool requirement listed in §3 is quoted from that tool's own `execute`.
- `tests/test_task_candidate_contract.py` explicitly encodes the intended
  behaviour that well-formed non-send actions persist and fail later at
  execution; `tests/test_task_nl_creation.py` encodes the same for a scheduled
  `delete {"mode": "all"}` action.

**Hypotheses (plausible, not proven in this pass)**

- H1: a model-supplied numeric `chat_id` inside `notification_destination` can
  reach persistence when the request lacks a usable chat id and no `chat_name`
  is resolved. Requires a focused replay to confirm.
- H2: `settings_set` (`ADMIN_ONLY`) and `organize_clean` (`DANGEROUS`) persisted
  as scheduled task actions may bypass the confirmation semantics the executor
  applies on the immediate path. The executor's confirmation branch was not
  traced here.
- H3: `web_search`'s required arguments were not inspected.
- H4: whether any consumer other than `CreateTaskTool` calls
  `TaskCreationService.create` directly (which would skip gates 3a/3b entirely)
  was not exhaustively enumerated.

---

## 6. Remaining work (not implemented)

1. Decide the pre-persistence contractual set of task actions (vs the
   immediate-only set), and enforce it before `create_task`.
2. Enforce per-action required arguments for that set, including the
   empty-static-vs-authorized-generation rule and a username counterpart to the
   bio profile-fidelity gate.
3. Decide the correct handling of context-dependent actions (`save`,
   `delete_replied`) for scheduled tasks.
4. Resolve H1 (destination trust) and H2 (confirmation-gated actions).
5. Add focused regression coverage for the incomplete-candidate cases once the
   boundary is defined.

---

## 7. Files / functions that would change if the gaps are fixed

| File | Symbol | Role in a fix |
|---|---|---|
| `backend/ai/task_candidate.py` | `TaskCandidate.from_untrusted`, `_canonicalize_action`, `_SEND_ACTION_ALIASES` | the only pre-persistence per-action gate today |
| `backend/ai/task_creation.py` | `TaskCreationService.create` | second pre-persistence checkpoint |
| `backend/ai/tools/task.py` | `CreateTaskTool._execute` | NL-path gates (source fidelity, bio profile fidelity) |
| `backend/ai/actions.py` | `_has_bio_mention`, `_has_bio_change_intent`, `_write_text_present`, `_tokenize` | deterministic intent vocabulary; no username counterpart exists |
| `backend/ai/task_interpreter.py` | interpreter prompt, `CANDIDATE_SCHEMA` | action contract currently names only `send_message`, `bio_set_text`, `username_set_text` |
| `backend/ai/tools/registry.py`, `backend/ai/tools/base.py` | `ToolRegistry`, `Tool` | natural single source for a registry-derived `name → required arguments` contract |
| `backend/ai/task_execution.py` | `TaskExecutionCoordinator.execute` | registry check stays as defence-in-depth |
| `backend/ai/preparation_policy.py` | `derive_policy`, `validate_prepared_arguments` | generated-content policy |
| `tests/test_task_candidate_contract.py`, `tests/test_task_nl_interval_creation.py` | — | existing contract tests to extend |

No production code, tests, configuration, or schema were modified by this
investigation.

---

## Stage A — Boundary coverage of 3763138d

> **Stage A only.** Sections 1–7 above describe revision `2551970` and are
> preserved unchanged. This section audits the **current** revision
> (`22a9dfb`) and adds no later stage. No stage beyond A was started.

### Scope executed

| Source | Region read |
|---|---|
| `backend/ai/task_creation.py` | `TaskSemanticCompletenessError` (L35), `_PROFILE_CONTENT_ACTIONS` (L39), `_semantic_completeness_error` (L42–59), the call site in `TaskCreationService.create` (L120–123), and the full ordering of `create()` (L99–177) |
| `backend/ai/tools/task.py` | the `TaskSemanticCompletenessError` import (L167), `_fail` (L241–287), and the `create()` catch site (L590–591) |
| `backend/ai/task_interpreter.py` | the NON-INVENTION CONTRACT block (L401–411 of the interpret prompt) |
| `tests/test_task_semantic_completeness.py` | all four tests and their assertions |
| Narrowly necessary neighbour | `backend/bot/handlers/taskloom.py` L811–820 — the wizard's `TaskCreationService.create` call and its `except Exception` handler |
| Narrowly necessary neighbour | `backend/ai/task_candidate.py` L218–244 — `_canonicalize_action`, to establish what argument shape reaches the check |

### PASS

**P1 — The check exists, is deterministic, and is keyed on an explicit action set.**
`task_creation.py:39` — `_PROFILE_CONTENT_ACTIONS = frozenset({"bio_set_text", "username_set_text"})`. `task_creation.py:42–59` — `_semantic_completeness_error(candidate)` returns a reason string or `None`.

**P2 — An empty profile action with no generation contract is rejected.**
`task_creation.py:59` — `if not (isinstance(instruction, str) and instruction.strip()) and not str(arguments.get("text") or "").strip():` → `"profile content requires explicit content or an AI instruction"`. The condition requires *both* a nonblank instruction *and* nonblank text to be absent before rejecting, so either one alone is sufficient to accept.

**P3 — `ai_instruction` is genuinely required to be nonblank when static profile content is empty.**
Same expression as P2: acceptance requires `isinstance(instruction, str) and instruction.strip()`. A supplied but blank/whitespace-only instruction does not satisfy it (and is separately rejected earlier — see P4).

**P4 — An empty or whitespace-only `ai_instruction` is rejected, and this rejection is broader than the profile set.**
`task_creation.py:52–53` runs **before** the action loop:
`if instruction is not None and (not isinstance(instruction, str) or not instruction.strip()): return "AI instruction is invalid"`.
Because it precedes `for action in actions`, this fires for *any* candidate carrying a non-`None`, non-nonblank-string instruction — including candidates whose actions are entirely outside `_PROFILE_CONTENT_ACTIONS`. The action set gates only the two *content* rejections, not the instruction-validity rejection.

**P5 — A malformed covered-action argument container is rejected.**
`task_creation.py:55–56` — `if not isinstance(arguments, dict): return "content action arguments are invalid"`.

**P6 — Rejection provably happens before schedule resolution.**
In `TaskCreationService.create` the order is: field-set checks (L116–119) → **`_semantic_completeness_error` (L120–123)** → task/schedule timezone agreement (L124–131) → schedule resolution via `parse_schedule` / `initial_next_run` (L132–146) → payload assembly + `validate_ai_instruction` (L147–155) → `repository.create_task` (L168). The requested logical order **candidate → semantic validation → schedule resolution → repository persistence is correct as written**; two steps sit between candidate parsing and the semantic check (`isinstance` guard + required/allowed field-set checks), and payload/instruction validation sits between schedule resolution and persistence.

**P7 — Rejection provably happens before repository persistence, for every caller.**
The check is **inside** the service, not in a caller. `repository.create_task` is reached only after it (L120→L168), and the raise occurs before the `payload` is even built. Confirmed for both `create()` call sites: `tools/task.py:586` (NL path) and `bot/handlers/taskloom.py:813` (Taskloom wizard path).

**P8 — Detection produces a defined error, a defined Taskloom signal, and no persistence.**
- Error: `TaskSemanticCompletenessError` (subclass of `TaskCreationError` → `ValueError`), message = the reason string (`task_creation.py:35`, `L120–123`).
- Trace: `_creation_trace("semantic_incomplete", reason=semantic_error)` (L121).
- NL path (`tools/task.py:590–591`): `isinstance(exc, TaskSemanticCompletenessError)` → `_fail("create_task_semantics", "candidate_semantically_incomplete", exc)`.
- `_fail` (L272–277) sets `data = {"open_taskloom_wizard": True, "wizard_reason": "candidate_semantically_incomplete"}` because the category is explicitly listed alongside the `candidate_invalid*` prefix, and logs one terminal record with `failed_stage=create_task_semantics`, `persisted=false`.
- Persistence skipped: the exception is raised before `payload`/`repository_call`, so `create_task_repository_create_result` and the repository call are never reached.
- Taskloom path (`taskloom.py:814–819`): caught by the generic `except Exception` and rendered as `× Save failed (TaskSemanticCompletenessError); nothing durable was written.` with the draft preserved — persistence likewise skipped.

**P9 — The happy paths are preserved.** A well-formed candidate with either nonblank static text or a nonblank instruction passes unchanged; the check returns `None` and creation proceeds exactly as before.

### GAP

**G1 — Coverage is exactly two action names.** `_PROFILE_CONTENT_ACTIONS` contains only `bio_set_text` and `username_set_text` (`task_creation.py:39`). Every other registered action is not evaluated by this boundary (the loop `continue`s at L54–55). This is the implementation's own scope, not an inferred defect: the added tests cover only `bio_set_text`.

**G2 — A candidate whose `actions` is not a list short-circuits the entire check.** `task_creation.py:50–51` — `if not isinstance(actions, list): return None`. The check then never runs, and `create()` does not itself validate list-ness of `actions` (the required-field check only tests key presence), so `payload` is built and `repository.create_task` is called. Unreachable through `TaskCandidate.from_untrusted` (which enforces a list), but reachable from a direct `create(validated, now)` caller such as the wizard path (`taskloom.py:813` accepts a raw `validated` dict).

**G3 — Action-name matching is exact and case-sensitive.** `action.get("name") not in _PROFILE_CONTENT_ACTIONS` (`L54`). `_canonicalize_action` (`task_candidate.py:224`) only `.strip()`s the name, never lower-cases it, so `BIO_SET_TEXT` bypasses this boundary. What happens downstream is outside Stage A’s scope and is not claimed here.

**G4 — A truthy non-string `text` satisfies the check.** `str(arguments.get("text") or "").strip()` (`L59`) coerces: `{"text": 123}` yields `"123"` and is accepted. The check validates *presence*, not *type*; whether that value is then usable by the profile service belongs to a later stage.

**G5 — The check does not verify that `ai_instruction` was authorized by the user.** It only requires a nonblank string. A provider-supplied invented instruction is therefore sufficient to pass the boundary. The deterministic verbatim-repair gate that ties the instruction to the original request lives in `CreateTaskTool`, *outside* this boundary, and is not exercised by `TaskCreationService` when called directly.

**G6 — No action-registration check.** An unregistered action name in a covered position is not rejected here; registration is resolved later at occurrence execution (established in the `2551970` sections; not re-derived in Stage A).

**G7 — Owner-facing message does not distinguish semantic incompleteness.** `_fail` returns the same generic “I could not turn that into a safe, unambiguous schedule …” text used for ambiguity, appending only `[failure category: candidate_semantically_incomplete]` (`L278–286`). The wizard data is accurate; the prose is not semantic-incompleteness-specific.

### UNCERTAIN

- **U1 — Downstream consumption of the `open_taskloom_wizard` signal.** Stage A read only the producer (`_fail`). What `backend/bot/handlers/ai_unified.py` does with the flag is not established here.
- **U2 — Whether any path can reach `repository.create_task` without `TaskCreationService.create`.** Only the two `create()` call sites were checked; the repository’s other callers are out of Stage A scope.
- **U3 — Execution-time consequences of G3/G4/G6.** Whether a case-variant name, a coerced non-string `text`, or an unregistered name fails closed at occurrence execution is a later-stage question.
- **U4 — Reachability of G2 from a live draft.** That the wizard passes a raw `validated` dict is confirmed by the call shape at `taskloom.py:813`; whether any live wizard draft can actually produce a non-list `actions` value was not traced in Stage A.

### Coverage Matrix

| Case | Covered? | Accepted/Rejected | Evidence |
|---|---|---|---|
| `bio_set_text`, nonblank string `text`, no instruction | yes | **Accepted** | `task_creation.py:59` (text branch truthy) |
| `bio_set_text`, blank text, nonblank `ai_instruction` | yes | **Accepted** | `task_creation.py:59` (instruction branch truthy) |
| `bio_set_text`, blank/missing text, no `ai_instruction` key | yes | **Rejected** — `profile content requires explicit content or an AI instruction` | `task_creation.py:59` |
| `bio_set_text`, blank text, `ai_instruction` = `""` / whitespace | yes | **Rejected** — `AI instruction is invalid` (raised before the action loop) | `task_creation.py:52–53` |
| `username_set_text`, the same four cases | yes | identical to `bio_set_text` | `task_creation.py:39`, `L54–59` |
| covered action, `arguments` not a dict | yes | **Rejected** — `content action arguments are invalid` | `task_creation.py:55–56` |
| covered action, truthy non-string `text` (e.g. `123`) | yes | **Accepted** (coerced by `str()`) | `task_creation.py:59` |
| any candidate, `ai_instruction` present but not a nonblank string | yes (broader than the profile set) | **Rejected** — `AI instruction is invalid` | `task_creation.py:52–53` |
| `send_message`, unregistered names, `bio_set_template`, `bio_set_mood`, `username_set_template`, `username_set_mood`, `delete*`, `save*`, `search`, `retrieve_save`, `memory_*`, `settings_*`, `task_*`, `create_task`, `web_search`, `account_show`, `list_*`, `organize_*`, `database_stats` | **no** | Not evaluated (`continue`) | `task_creation.py:54–55` |
| case-variant name (`BIO_SET_TEXT`) | **no** | Not evaluated (exact set membership) | `task_creation.py:39`, `L54`; `task_candidate.py:224` |
| candidate whose `actions` is not a list | **no** | Whole check returns `None` | `task_creation.py:50–51` |
| whether `ai_instruction` was user-authorized | **no** | Not evaluated (nonblank string suffices) | `task_creation.py:52`, `L59` |
| whether the action name is registered | **no** | Not evaluated | `task_creation.py:54–55` |
| destination / target / trusted-field semantics | **no** | Not evaluated | `task_creation.py:42–59` (no such reads) |
| permission / confirmation level of the action | **no** | Not evaluated | `task_creation.py:42–59` (no such reads) |

### Persistence Ordering

The requested sequence is **confirmed**, with the intermediate steps made explicit:

```
TaskCandidate.from_untrusted            (candidate: shape, schedule, alias/shape normalization)
  → CreateTaskTool._execute             (destination + event-trigger resolution; policy gates)
  → TaskCreationService.create
        1. isinstance guard                      (L109–111)
        2. reference tz-aware check              (L112–113)
        3. unsupported-field check               (L116–117)
        4. missing-required-field check          (L118–119)
        5. ► SEMANTIC COMPLETENESS ◄             (L120–123)  ← raise here
        6. task/schedule timezone agreement      (L124–131)
        7. ► SCHEDULE RESOLUTION ◄               (L132–146)
        8. payload assembly + validate_ai_instruction (L147–155)
        9. ► repository.create_task ◄            (L168)
```

No task-creating side effect precedes step 5. The only actions earlier in the NL path are the interpreter provider call and read-only `client.get_dialogs()` during destination/trigger resolution.

### Test Evidence

`tests/test_task_semantic_completeness.py` — four tests, assertions inspected:

| Test | What the assertions actually prove |
|---|---|
| `test_schema_valid_empty_profile_candidate_is_not_persisted` | Empty `bio_set_text` + no instruction ⇒ `result.success is False`; `result.data` equals **exactly** `{"open_taskloom_wizard": True, "wizard_reason": "candidate_semantically_incomplete"}`; `repository_manager.task.list_tasks(OWNER) == []`. Proves the rejection, the exact wizard payload, and that nothing was persisted. |
| `test_fully_specified_generated_profile_candidate_creates_directly` | Same candidate **with** `ai_instruction=REQUEST` ⇒ `success is True`, exactly one task, `tasks[0].ai_instruction == REQUEST`, `provider.calls == 1`. Proves the instruction-accepting half and that the interpreter ran once. |
| `test_direct_creation_service_rejects_empty_profile_candidate_before_repository` | Calls `TaskCreationService(repository, OWNER).create(TaskCandidate.from_untrusted(_candidate()), now)` directly and asserts `pytest.raises(TaskSemanticCompletenessError)`, then `repository.list_tasks(OWNER) == []`. Proves the check lives **in the service** (not only in the tool) and precedes persistence. |
| `test_interpreter_prompt_forbids_inventing_missing_requirements` | Asserts the substrings `"Never invent missing schedule"` and `"schedule expression alone does not authorize invented content"` appear in the system prompt, plus `CANDIDATE_SCHEMA["required"]` is truthy. Proves **prompt text presence only** — no behavioural assertion. |

**Directly proven:** the empty-profile-without-instruction rejection, the nonblank-instruction acceptance, the service-level placement before persistence, and the exact `open_taskloom_wizard` / `wizard_reason` payload.
**Implied by implementation but not asserted:** nonblank *static* profile text is accepted (no test); `username_set_text` is covered (every test uses `bio_set_text`); the `"AI instruction is invalid"` and `"content action arguments are invalid"` reasons; ordering relative to schedule resolution; G2/G3/G4.
**Not tested:** the `semantic_incomplete` trace record; the Taskloom-wizard caller’s failure notice; any downstream consumption of `open_taskloom_wizard`; any action outside the two-name set.

### Stage A Verdict

The `3763138d` boundary works as implemented and is correctly placed: it is a deterministic, in-service check that provably rejects an empty profile-content action lacking a generation contract **before schedule resolution and before every repository write**, and it degrades honestly into the existing Taskloom wizard signal instead of persisting a guessed task. Its deliberate scope is exactly two actions (`bio_set_text`, `username_set_text`) plus a broader `ai_instruction`-validity rejection. Outside its scope remain: every other registered action (G1), non-list `actions` (G2), case-variant names (G3), non-string content (G4), instruction authorization (G5), action registration (G6), destination/permission semantics, and the downstream wizard-signal consumption (U1). Stage B was not started; no production code, tests, schema, or configuration were modified.

---

## Stage B — Action vocabulary and requirement classes

> **Inventory/classification only.** This stage does **not** decide or implement
> any validator, and it does **not** check whether the task-creation layer
> enforces these requirements (Stages C–G). No file other than this document was
> modified. `IMPLEMENTATION_REPORT.md` was read, not changed. No repository-wide
> search was performed; the registry plus the concrete tool implementations were
> inspected directly.

### Scope executed

| Item | Value |
|---|---|
| Revision inspected | `960cb35` (`main`, Stage A recorded) |
| Authoritative vocabulary source | `backend/ai/tools/registry.py` → `create_default_registry()` |
| Execution contract sources | `backend/ai/tools/<domain>.py` tool classes; `backend/ai/tools/executor.py`; `backend/ai/task_execution.py`; `backend/runtime/supervisor.py` |
| Persistence-shape sources (context only) | `backend/ai/task_candidate.py` → `_canonicalize_action` |
| Evidence rule | every classification cites the tool class/method that reads the field or context key |

**How a scheduled action actually gets a context** (the baseline for every
“runtime context” column below):

- `backend/runtime/supervisor.py::RuntimeSupervisor._start_task_scheduler` builds
  the base `ToolContext(telegram=TelegramAPI(client), owner_id, tz_str, client)` —
  **`extra` is left unset/`None`** — and hands it to
  `TaskExecutionCoordinator` (and `ToolExecutor`).
- `backend/ai/task_execution.py::TaskExecutionCoordinator.execute` then resolves the
  trusted destination and, when `task.notification_destination["chat_id"]` is a
  non-zero int, installs a new context whose `extra == {"chat_id": <int>}`.
  Nothing else is ever added: **no `reply_msg`, no `request_message_id`, no
  `request_id`, no `provider_manager`, no `deterministic_task_candidate`.**
- `TaskExecutionCoordinator.execute` resolves each snapshotted name through
  `executor._registry.get(name)` and fails the occurrence with
  `unregistered_action` when absent; names are otherwise unconstrained.

### B1. Authoritative action vocabulary

39 tool names are registered by `create_default_registry()` — this is the
complete registry, enumerated from the `registry.register(...)` calls (not from
documentation):

```
01 save                      21 username_show
02 save_by_link              22 search
03 delete                    23 list_saves
04 delete_by_id              24 database_stats
05 delete_replied            25 account_show
06 delete_message_by_id      26 settings_get
07 list_recent_messages      27 settings_set
08 delete_messages_by_ids    28 organize_list
09 bio_set_template          29 organize_clean
10 bio_set_text              30 web_search
11 bio_set_mood              31 create_task
12 bio_on                    32 task_list
13 bio_off                   33 task_inspect
14 bio_show                  34 task_transition
15 get_bio                   35 task_delete
16 username_set_template     36 retrieve_save
17 username_set_text         37 send_message
18 username_set_mood         38 memory_store
19 username_on               39 memory_list
20 username_off
```

**Persistence-layer alias set (context only — not registry names).**
`backend/ai/task_candidate.py::_canonicalize_action` (L215–247) normalizes the
four send aliases `send`, `send_message`, `write_message`, `send_text` to the
single registered name `send_message`, taking content from the first present of
`text`/`content`/`message`/`body` and an optional allow-listed `font`. Every
other name is passed through verbatim (`{"name": name, "arguments": args}`), and
`TaskCandidate.from_untrusted` (L293–303) also tolerates `tool`→`name` and
`parameters`/`args`→`arguments`. So the *persistable* name set is not closed by
the candidate layer — the registry is the only closed vocabulary.

### B2. Action contract matrix

“Required args” = arguments the tool’s own `execute()` rejects when absent
(deterministic, in-tool). “Runtime context” = the `ToolContext`/`extra` keys the
tool actually reads. “Scheduled viability” is judged only against the trusted
context the coordinator builds (above) — never against an immediate request.

| Action | Required args | Optional args | Runtime context | Trusted target | Permission/confirmation | Scheduled-task viability | Source |
|---|---|---|---|---|---|---|---|
| `save` | None (schema `{}`) | None | **`extra["reply_msg"]` required** (chat_id + message_id); Telegram client | Destination is Saved Messages (owner's own); source = replied message | READ_WRITE (auto) | **NO — unexecutable**: scheduled `extra` never carries `reply_msg` | `tools/save.py::SaveTool.execute` |
| `save_by_link` | `link` (non-empty; `http` prefixed; parsed by `save_service.parse_telegram_link`) | None | Telegram client | Destination Saved Messages; source from the link | READ_WRITE (auto); long_running | Yes (static link) | `tools/save.py::SaveByLinkTool.execute` |
| `delete` | an explicit scope: `count` 1–500 **or** a filtered scope (`mode` ∈ last_n/all/until_time/until_message/filtered, or `until_time`/`after_time`/`boundary_id`/`query`/`semantic`) | `count`, `mode`, `until_time`, `after_time`, `boundary_id`, `query`, `semantic` | **`extra["chat_id"]` required**; `extra["request_message_id"]`, `extra["reply_msg"]` optional (boundary resolution) | Chat from trusted `chat_id`; outgoing-only enforced in service | DANGEROUS (auto) | Yes, **if** `chat_id` exists; `until_message` fails closed when no boundary resolves | `tools/delete.py::DeleteTool.execute` |
| `delete_by_id` | `message_id` int | None | **`extra["chat_id"]` required** | Chat from `chat_id`; outgoing-only in service | DANGEROUS (auto) | Yes, if `chat_id` exists | `tools/delete.py::DeleteByIdTool.execute` |
| `delete_replied` | None (schema `{}`) | None | **`extra["reply_msg"]` required** (chat_id + message_id) | Replied message in `reply_msg` chat; outgoing-only | DANGEROUS (auto) | **NO — unexecutable**: no `reply_msg` at scheduled time | `tools/delete.py::DeleteRepliedTool.execute` |
| `delete_message_by_id` | `message_id` positive int | None | **`extra["chat_id"]` required** | Chat from `chat_id`; outgoing-only | DANGEROUS (auto) | Yes, if `chat_id` exists | `tools/delete.py::DeleteMessageByIdTool.execute` |
| `list_recent_messages` | None | `limit` 1–100 (default 50) | **`extra["chat_id"]` required** | Chat from `chat_id` | READ_ONLY | Yes, if `chat_id` exists | `tools/semantic.py::ListRecentMessagesTool.execute` |
| `delete_messages_by_ids` | `message_ids` non-empty id list (max 100 after coercion) | None | **`extra["chat_id"]` required** | Chat from `chat_id`; every id re-fetched + outgoing-only | DANGEROUS (auto) | Conditional — static ids only; ids from a prior `list_recent_messages` are not durable | `tools/semantic.py::DeleteMessagesByIdsTool.execute` |
| `bio_set_template` | `template` truthy string | None | None (owner_id only) | n/a (own profile `about`) | READ_WRITE (auto) | Yes | `tools/bio.py::BioSetTemplateTool.execute` |
| `bio_set_text` | None in-tool — `arguments.get("text", "")` accepts empty | `text` | None | n/a (own profile) | READ_WRITE (auto) | Yes (content semantics = Stage A boundary) | `tools/bio.py::BioSetTextTool.execute` |
| `bio_set_mood` | None in-tool — accepts empty default `""` | `mood` | None | n/a (own profile) | READ_WRITE (auto) | Yes | `tools/bio.py::BioSetMoodTool.execute` |
| `bio_on` | None | None | `context.telegram.client` | n/a | READ_WRITE (auto) | Yes (engine start; process-local) | `tools/bio.py::BioOnTool.execute` |
| `bio_off` | None | None | None | n/a | READ_WRITE (auto) | Yes | `tools/bio.py::BioOffTool.execute` |
| `bio_show` | None | None | None | n/a | READ_ONLY | Yes (read) | `tools/bio.py::BioShowTool.execute` |
| `get_bio` | None | None | **`context.telegram` required** | n/a | READ_ONLY | Yes (read) | `tools/bio.py::BioGetTool.execute` |
| `username_set_template` | `template` truthy string | None | None | n/a (own profile `first_name`) | READ_WRITE (auto) | Yes | `tools/username.py::UsernameSetTemplateTool.execute` |
| `username_set_text` | None in-tool — accepts empty | `text` | None | n/a (own profile) | READ_WRITE (auto) | Yes (content semantics = Stage A boundary) | `tools/username.py::UsernameSetTextTool.execute` |
| `username_set_mood` | None in-tool — accepts empty default `""` | `mood` | None | n/a (own profile) | READ_WRITE (auto) | Yes | `tools/username.py::UsernameSetMoodTool.execute` |
| `username_on` | None | None | `context.telegram.client` | n/a | READ_WRITE (auto) | Yes (process-local) | `tools/username.py::UsernameOnTool.execute` |
| `username_off` | None | None | None | n/a | READ_WRITE (auto) | Yes | `tools/username.py::UsernameOffTool.execute` |
| `username_show` | None | None | None | n/a | READ_ONLY | Yes (read) | `tools/username.py::UsernameShowTool.execute` |
| `search` | `query` truthy | None | None | n/a (owner-scoped saved items) | READ_ONLY | Yes (read) | `tools/retrieve.py::SearchTool.execute` |
| `list_saves` | None | `limit` 1–50 (default 10) | None | n/a (owner-scoped) | READ_ONLY | Yes (read) | `tools/retrieve.py::ListSavesTool.execute` |
| `database_stats` | None | None | None | n/a | READ_ONLY | Yes (read) | `tools/database.py::DatabaseStatsTool.execute` |
| `account_show` | None | `fields` ⊆ {first_name,last_name,full_name,username} (defaults first_name+username) | **`context.telegram` required** | n/a (own identity; phone/id never returned) | READ_ONLY | Yes (read) | `tools/account.py::AccountShowTool.execute` |
| `settings_get` | `key` truthy | None | None (config_store / settings_service) | n/a | READ_ONLY | Yes (read) | `tools/settings.py::SettingsGetTool.execute` |
| `settings_set` | `key` truthy **and** `value` not None | `key`, `value` | None | n/a (global owner settings) | **ADMIN_ONLY → requires confirmation; never auto-executed** | **NO — cannot execute**: `executor.execute_calls` returns `needs_confirmation` | `tools/settings.py::SettingsSetTool`; `tools/executor.py::_is_auto_executable` |
| `organize_list` | None | None | None | n/a | READ_ONLY | Yes (read) | `tools/organize.py::OrganizeListTool.execute` |
| `organize_clean` | None | None | None | n/a (owner's bot logs) | DANGEROUS (auto) | Yes (purges logs > 7 days) | `tools/organize.py::OrganizeCleanTool.execute` |
| `web_search` | `query` non-empty | `count` 1–100, `freshness` ∈ day/week/month/year, `include_domains` list | `extra["provider_manager"]` **optional** — falls back to `get_engine().provider_manager`, else honest failure; Telegram not used | n/a (external retrieval) | READ_ONLY | Yes — engine fallback resolves the manager | `tools/websearch.py::WebSearchTool.execute`; `services/web_search_service.do_web_search` |
| `create_task` | `request` non-empty ≤ 2000 chars | None | `extra["request_id"]`, `extra["chat_id"]`, `extra["provider_manager"]` (engine fallback), `extra["deterministic_task_candidate"]` optional | owner_id from context; destination resolved during interpretation | READ_WRITE (auto) | UNCERTAIN — reachable via engine fallback, but a scheduled task that creates tasks is outside the audited semantics | `tools/task.py::CreateTaskTool.execute` |
| `task_list` | None | `status` ∈ active/paused/completed | None (repository manager) | owner-scoped in service | READ_ONLY | Yes (read) | `tools/task_management_tools.py::TaskListTool.execute` |
| `task_inspect` | `task_id` positive int | None | None | owner-scoped in service | READ_ONLY | Yes (read) | `tools/task_management_tools.py::TaskInspectTool.execute` |
| `task_transition` | `task_id` positive int, `action`/`action_status` ∈ paused/active/completed, `expected_version` positive int | None | None | owner-scoped + CAS version | READ_WRITE (auto) | Yes, but CAS version must match a value read at run time → practically requires AI preparation | `tools/task_management_tools.py::TaskTransitionTool.execute` |
| `task_delete` | `task_id` positive int, `expected_version` positive int | None | None | owner-scoped + CAS version | READ_WRITE (auto) | Same CAS caveat | `tools/task_management_tools.py::TaskDeleteTool.execute` |
| `retrieve_save` | `save_code` non-empty alphanumeric (upper-cased) | None | **`extra["chat_id"]` required** (non-zero int) | Chat from `chat_id`; code owner-scoped in service | READ_WRITE (auto) | Yes, if `chat_id` exists | `tools/retrieve_save.py::RetrieveSaveTool.execute` |
| `send_message` | `text` non-blank ≤ 4096 (also enforced at candidate layer) | `font` (must be in the font registry) | `extra["chat_id"]` optional — falls back to `owner_id`; Telegram client | **Never model-supplied**: owner's own chat / creation chat | READ_WRITE (auto) | Yes — the canonical scheduled action | `tools/message.py::SendMessageTool.execute` |
| `memory_store` | `content` non-empty ≤ `MAX_MEMORY_ENTRY_CHARS` | `tier` ∈ long/permanent, `category` ∈ fact/preference/context/summary/instruction, `importance` 0.0–1.0 | None (engine memory manager, with in-memory fallback) | n/a (owner-scoped memory) | READ_WRITE (auto) | Yes | `tools/memory.py::MemoryStoreTool.execute` |
| `memory_list` | None | `tier` ∈ long/permanent, `query`, `limit` 1–20 | None | n/a (owner-scoped) | READ_ONLY | Yes (read) | `tools/memory.py::MemoryListTool.execute` |

### B3. Requirement classifications

An action may appear in more than one group; groups are derived from the
contract matrix above, not from names.

**NO_ARGUMENT_REQUIREMENTS** (schema has no required field):
`save`, `delete_replied`, `list_recent_messages`, `bio_on`, `bio_off`, `bio_show`,
`get_bio`, `username_on`, `username_off`, `username_show`, `list_saves`,
`database_stats`, `account_show`, `organize_list`, `organize_clean`, `task_list`,
`memory_list`.

**STATIC_ARGUMENTS** (execution depends on at least one persisted argument):
`save_by_link` (`link`), `delete` (scope), `delete_by_id` (`message_id`),
`delete_message_by_id` (`message_id`), `delete_messages_by_ids` (`message_ids`),
`bio_set_template`/`username_set_template` (`template`),
`bio_set_text`/`username_set_text` (`text`), `bio_set_mood`/`username_set_mood`
(`mood`), `search` (`query`), `settings_get`/`settings_set` (`key`[+`value`]),
`web_search` (`query`), `create_task` (`request`), `task_inspect` (`task_id`),
`task_transition`/`task_delete` (`task_id` + `expected_version`),
`retrieve_save` (`save_code`), `send_message` (`text`), `memory_store` (`content`).

**CONTENT_BEARING** — free text that a human reads back:
`send_message.text`, `bio_set_text.text`, `username_set_text.text`,
`memory_store.content`, plus `bio_set_mood.mood` / `username_set_mood.mood`
(short single-token values) and `bio_set_template.template` /
`username_set_template.template` (token-bearing strings). Note the deterministic
ai-preparation content policy (`preparation_policy.CONTENT_FIELDS`) recognizes
exactly `{text, message, content, body}` — so of the above only
`send_message.text`, `bio_set_text.text`, `username_set_text.text`, and
`memory_store.content` are policy-validated content fields.

**RUNTIME_CONTEXT_REQUIRED** (`ToolContext`/`extra`, not arguments):
`save` and `delete_replied` → `extra["reply_msg"]` **required**;
`delete`, `delete_by_id`, `delete_message_by_id`, `delete_messages_by_ids`,
`list_recent_messages`, `retrieve_save` → `extra["chat_id"]` **required**;
`bio_on`/`username_on` → `context.telegram.client` required;
`get_bio`/`account_show` → `context.telegram` required;
`send_message` → `extra["chat_id"]` optional (fallback `owner_id`);
`web_search`/`create_task` → `extra["provider_manager"]` optional (engine fallback);
`create_task` → `extra["request_id"]`/`extra["chat_id"]` used for tracing.

**TRUSTED_DESTINATION_REQUIRED** (destination must come from runtime context,
never from the model): `send_message`, `retrieve_save`, `delete`,
`delete_by_id`, `delete_message_by_id`, `delete_messages_by_ids`,
`list_recent_messages`, and service-side source identity for `save`/`save_by_link`.

**CONFIRMATION_OR_PERMISSION_SENSITIVE**: `settings_set` (ADMIN_ONLY). All
DANGEROUS actions (`delete`, `delete_by_id`, `delete_replied`,
`delete_message_by_id`, `delete_messages_by_ids`, `organize_clean`) are
auto-executable by design in this single-owner self-bot and therefore are
*not* confirmation-gated.

**IMMEDIATE_ONLY_CONTEXT** (the required context exists only during a live reply
request): `save`, `delete_replied`.

**OTHER**: `create_task`, `task_list`, `task_inspect`, `task_transition`,
`task_delete`, `organize_list`, `organize_clean`, `database_stats` (meta /
maintenance rather than user-facing content actions).

### B4. Confirmed facts vs uncertainties

**CONFIRMED**

1. The registry is the authoritative vocabulary and contains exactly the 39 names
   in B1 (`registry.py::create_default_registry`).
2. A scheduled occurrence receives `extra == {"chat_id": <int>}` at most; the base
   context carries `telegram`, `owner_id`, `tz_str`, `client` and no `extra`
   (`supervisor.py::_start_task_scheduler`, `task_execution.py::execute`).
3. `save` and `delete_replied` require `extra["reply_msg"]`, which the scheduled
   path never sets — they are persistable names that can never execute as a
   scheduled occurrence (`tools/save.py`, `tools/delete.py`).
4. `settings_set` is ADMIN_ONLY and `executor.execute_calls` never passes the
   `confirmed` flag, so a snapshotted `settings_set` action always returns
   `needs_confirmation` instead of executing (`executor.py::_execute_single`,
   `executor.py::_is_auto_executable`).
5. Every action whose name is not in the registry fails the occurrence with
   `unregistered_action` before execution (`task_execution.py::execute`).
6. `send_message` is the only action with an explicit candidate-layer contract
   (bounded non-blank `text` + optional font) and a trusted-destination fallback
   to the owner's own chat (`task_candidate.py::_canonicalize_action`,
   `tools/message.py::SendMessageTool.execute`).
7. Several tools accept an empty/absent content argument without any in-tool
   rejection: `bio_set_text` and `username_set_text` (`arguments.get("text", "")`),
   `bio_set_mood` and `username_set_mood` (`arguments.get("mood", "")`).
   Their rejection, when it happens at all, is a creation-layer concern (Stage A)
   and is not enforced by the tool.
8. `web_search` and `create_task` tolerate a missing `extra["provider_manager"]`
   by resolving the process-global engine, so they are not hard-dependent on
   `extra` (`tools/websearch.py`, `tools/task.py::_execute`).

**UNCERTAIN**

- **U-B1:** Whether `notification_destination["chat_id"]` is populated for every
  created task. The mechanism is confirmed; per-task population is a creation-
  layer/destination question deferred to Stage E. Affects the entire
  `chat_id`-dependent family.
- **U-B2:** Whether a persisted `create_task` action is meaningful — it is
  reachable via the engine fallback but the recursion semantics are outside this
  stage's scope.
- **U-B3:** Whether persisting literal message IDs for `delete_messages_by_ids`
  can ever be a durable semantic contract (ids are chat-relative and time-relative).
- **U-B4:** Event-schedule tasks (`schedule_type == "event"`) use the same action
  vocabulary but a different trigger contract; trigger semantics were not audited
  here.
- **U-B5:** The ai-preparation path validates only `CONTENT_FIELDS`
  (`text`/`message`/`content`/`body`); whether that is sufficient for
  `mood`/`template`-bearing actions is a Stage C question, not decided here.

### B5. Stage B verdict

1. **Vocabulary established.** The complete currently registered task-action
   vocabulary was established from `registry.py` — 39 names, plus the
   candidate-layer send aliases that normalize into `send_message`.
2. **Every action classified.** All 39 received a requirement classification
   with source evidence; 37 are fully confirmed and 2 carry an explicit
   UNCERTAIN mark (`create_task` viability, `delete_messages_by_ids` id-durability).
3. **Actions requiring later investigation.**
   - **Stage C (content at the persistence boundary):** `send_message`,
     `bio_set_text`, `username_set_text`, `memory_store`, `bio_set_mood`,
     `username_set_mood`, `bio_set_template`, `username_set_template`.
   - **Stage D (context-dependent actions at scheduled execution):** `save`,
     `delete_replied` (unexecutable), and the `chat_id`-dependent family
     (`delete`, `delete_by_id`, `delete_message_by_id`,
     `delete_messages_by_ids`, `list_recent_messages`, `retrieve_save`),
     plus the CAS-dependent `task_transition` / `task_delete`.
   - **Stage E (trusted vs model-supplied fields):** destination population and
     per-action trusted-target enforcement.
   - **Stage F (permission/confirmation):** `settings_set`, and the DANGEROUS
     auto-execution set.
   - **Stage G (residual):** U-B1–U-B5.
4. **Still unknown.** Whether any of the required args/context above are checked
   *before persistence* — this stage deliberately did not examine the
   creation-layer enforcement (Stages C–G).

**Stopping condition met:** every currently registered task action enumerated;
 every action assigned an execution-contract classification with source evidence;
 unresolved cases explicitly marked UNCERTAIN. Stage C was not started; no
production code, tests, schema, configuration, UI, or `IMPLEMENTATION_REPORT.md`
was modified.

---

## Stage C — Content and semantic requirement enforcement

### C1. Scope

**Revision inspected:** `9ae1757` (`main`). This stage starts from the 39-action
registry and traces every registered action with meaningful persisted arguments
through `TaskCandidate.from_untrusted`, `CreateTaskTool._execute`, and
`TaskCreationService.create` to the `repository.create_task` call. Runtime
context is handled separately in Stage D.

The relevant path is:

```
provider JSON
→ TaskCandidate.from_untrusted()
→ CreateTaskTool normalization/repair gates (NL path)
→ TaskCreationService.create()
→ repository.create_task()
```

`TaskCandidate.from_untrusted` validates the action envelope and that
`arguments` is an object, except for the special send-action canonicalizer.
`TaskCreationService._semantic_completeness_error` is the only action-specific
creation check observed; its set is exactly `{"bio_set_text",
"username_set_text"}`. Tool-level checks occur after persistence and therefore
are not pre-persistence semantic enforcement.

The interpreter NON-INVENTION and AI-GENERATED CONTENT text is provider
instruction only. It is not a runtime assertion that the provider supplied the
value or that a nonblank `ai_instruction` represents an explicit user request.

### C2. Action enforcement matrix

| Action | Required semantic input | Pre-persistence enforcement | Generation exception | Verdict | Evidence |
|---|---|---|---|---|---|
| `send_message` | `text`: string, nonblank, max 4096; optional registered `font` | Yes. Send aliases are canonicalized, text is type/blank/length checked, and font is allow-listed | None for missing static text; the prompt requires user-supplied static text | **PASS** for required execution input; provenance is not independently proven | `backend/ai/task_candidate.py::_canonicalize_action`; `backend/ai/tools/message.py::SendMessageTool.execute` |
| `bio_set_text` | Nonblank static `text`, or a nonblank generation instruction | Partial. The service accepts either, but does not verify that the instruction authorizes generation or that static text is a string | Nonblank `ai_instruction` is accepted; NL source-fidelity repair applies only when `derive_policy(request).active` | **GAP** | `backend/ai/task_creation.py::_semantic_completeness_error`; `backend/ai/tools/task.py::_execute` |
| `username_set_text` | Nonblank static `text`, or a nonblank generation instruction | Same partial check as bio; non-string static values can satisfy `str(...).strip()` | Same presence-only instruction exception; no authorization proof | **GAP** | `backend/ai/task_creation.py::_semantic_completeness_error`; `backend/ai/tools/username.py::UsernameSetTextTool.execute` |
| `bio_set_template` | Truthy non-empty `template` | None; missing/empty template reaches persistence | None established; generic `ai_instruction` is not a source-proven template substitute | **GAP** | `backend/ai/tools/bio.py::BioSetTemplateTool.execute`; `backend/ai/task_creation.py::TaskCreationService.create` |
| `username_set_template` | Truthy non-empty `template` | None; missing/empty template reaches persistence | None established | **GAP** | `backend/ai/tools/username.py::UsernameSetTemplateTool.execute`; `backend/ai/task_creation.py::TaskCreationService.create` |
| `bio_set_mood` | None is required by the tool: missing/empty mood is explicitly passed through and accepted; a supplied value has no creation-time type check | Missing/empty is a valid execution case; malformed supplied values are not checked before persistence | None established | **GAP** only for malformed supplied values; **PASS** for the documented empty/missing case | `backend/ai/tools/bio.py::BioSetMoodTool.execute`; `backend/services/bio_service.py::do_mood` |
| `username_set_mood` | Same as `bio_set_mood` | Missing/empty is accepted; supplied value type is not checked before persistence | None established | **GAP** only for malformed supplied values; **PASS** for the documented empty/missing case | `backend/ai/tools/username.py::UsernameSetMoodTool.execute`; `backend/services/username_service.py::do_mood` |
| `memory_store` | Nonblank `content`, bounded by `MAX_MEMORY_ENTRY_CHARS`; tier/category/importance have bounded domains | None; empty content and invalid argument combinations reach persistence | No action-specific authorization that `ai_instruction` may generate memory content | **GAP** | `backend/ai/tools/memory.py::MemoryStoreTool.execute`; `backend/ai/task_creation.py::TaskCreationService.create` |
| `save_by_link` | Non-empty, parseable Telegram message `link` | None; link validation is execution-only | No instruction substitute for a source link | **GAP** | `backend/ai/tools/save.py::SaveByLinkTool.execute`; `backend/ai/task_candidate.py::TaskCandidate.from_untrusted` |
| `search` | Non-empty `query` | None; missing query reaches the tool | None established | **GAP** | `backend/ai/tools/retrieve.py::SearchTool.execute` |
| `delete` | Explicit deletion scope: bounded count or valid filtered scope; some modes need a boundary | None; an empty action object reaches execution and is rejected there | None; AI preparation does not establish a user-authorized deletion scope | **GAP** | `backend/ai/tools/delete.py::DeleteTool.execute`; `backend/ai/task_creation.py::_semantic_completeness_error` |
| `delete_by_id` | `message_id` | None; missing ID reaches the tool | None | **GAP** | `backend/ai/tools/delete.py::DeleteByIdTool.execute` |
| `delete_message_by_id` | Positive `message_id` | None; missing/nonpositive ID reaches the tool | None | **GAP** | `backend/ai/tools/delete.py::DeleteMessageByIdTool.execute` |
| `delete_messages_by_ids` | Non-empty list of positive IDs, bounded to 100 after coercion | None; empty/unusable list reaches the tool | None; IDs are not a generated-content contract | **GAP** | `backend/ai/tools/semantic.py::DeleteMessagesByIdsTool.execute` |
| `retrieve_save` | Non-empty alphanumeric `save_code` | None; missing/invalid code reaches the tool | None | **GAP** | `backend/ai/tools/retrieve_save.py::RetrieveSaveTool.execute` |
| `settings_get` | Truthy `key` | None; missing key reaches the tool | None | **GAP** | `backend/ai/tools/settings.py::SettingsGetTool.execute` |
| `settings_set` | Truthy `key` and a present `value` | None; missing key/value reaches the tool | None; confirmation is a separate runtime concern | **GAP** | `backend/ai/tools/settings.py::SettingsSetTool.execute` |
| `web_search` | Non-empty `query`; optional count/freshness/domains have bounded normalization in the tool | No query check before persistence; required query is rejected only at execution | None established | **GAP** | `backend/ai/tools/websearch.py::WebSearchTool.execute`; `backend/services/web_search_service.py::do_web_search` |
| `create_task` | Non-empty nested `request`, max 2000 chars | None; an outer task can persist a `create_task` action with no request | None; recursive scheduled-task semantics are not an accepted generation exception | **GAP** | `backend/ai/tools/task.py::CreateTaskTool._execute`; `backend/ai/task_creation.py::TaskCreationService.create` |
| `task_inspect` | Positive `task_id` | None; missing/nonpositive ID reaches the tool | None | **GAP** | `backend/ai/tools/task_management_tools.py::TaskInspectTool.execute` |
| `task_transition` | Positive `task_id`, valid status, positive `expected_version` | None; all are checked only by the tool | None; CAS version is not generated content | **GAP** | `backend/ai/tools/task_management_tools.py::TaskTransitionTool.execute` |
| `task_delete` | Positive `task_id` and positive `expected_version` | None; both are checked only by the tool | None | **GAP** | `backend/ai/tools/task_management_tools.py::TaskDeleteTool.execute` |
| `task_list` | No required argument; supplied `status` must be allow-listed | Missing status is valid, but an invalid supplied status reaches persistence and is rejected only by the tool | None | **GAP** for malformed optional input; missing status is valid | `backend/ai/tools/task_management_tools.py::TaskListTool.execute` |
| `list_saves` | No required argument; supplied `limit` is optional | Missing limit is defaulted by the tool; there is no creation-time check for a malformed supplied limit | None | **GAP** for malformed optional input; missing limit is valid | `backend/ai/tools/retrieve.py::ListSavesTool.execute`; `backend/services/discover_service.py::do_list` |
| `account_show` | No required argument; supplied `fields` must be a nonempty allow-list | Missing fields is valid, but invalid supplied fields reach persistence and `_normalize_fields` rejects them only at execution | None | **GAP** for malformed optional input; missing fields is valid | `backend/ai/tools/account.py::_normalize_fields`; `AccountShowTool.execute` |
| `memory_list` | No required argument; supplied tier/limit must be bounded | Missing options are valid; invalid tier or non-integer limit reaches persistence and is rejected only by the tool | None | **GAP** for malformed optional input; missing options are valid | `backend/ai/tools/memory.py::MemoryListTool.execute` |
| `list_recent_messages` | No required argument; `limit` is optional and coerced/defaulted/clamped to 1–100 | Tool normalizes missing and malformed limits to a bounded value; no required semantic input is absent | None | **PASS** for argument completeness; destination context is Stage D | `backend/ai/tools/semantic.py::ListRecentMessagesTool.execute` |

The no-argument actions (`bio_on`, `bio_off`, `bio_show`, `get_bio`,
`username_on`, `username_off`, `username_show`, `database_stats`,
`organize_list`, `organize_clean`, and `memory_list`'s missing-option case)
do not acquire a missing required argument merely by having `{}`. Their
context and permission behavior is not reclassified by this stage.

**Persistence paths for the confirmed required-argument gaps:**
`TaskCandidate.from_untrusted` accepts any registered-looking action name with
an object `arguments` value (and does not inspect nested fields), then
`TaskCreationService.create` checks only the envelope, schedule, and the two
profile text names. Therefore, for each GAP above, an otherwise valid
candidate containing `{"name": <action>, "arguments": {}}` (or an object
missing the named field) reaches `TaskCreationService.create`, passes
`_semantic_completeness_error`, resolves its schedule, and calls
`repository.create_task`. The tool's own missing-argument response is later
than persistence. The exception is the special send canonicalizer and the
profile blank-text/no-instruction check already documented in Stage A.

### C3. Confirmed PASS

- `send_message` has a deterministic candidate-layer contract: bounded,
  nonblank string text, optional canonical font, and no model-supplied
  destination field. This proves execution-input completeness, not that the
  provider's nonblank text was actually supplied by the user.
- `list_recent_messages` has no mandatory argument; its optional limit is
  coerced, defaulted, and clamped before the tool uses it.
- `bio_set_mood` and `username_set_mood` treat absent/empty mood as a valid
  state in their actual tool/service contract. Requiring nonblank mood would
  invent a requirement not established by source. Malformed supplied mood
  values remain the GAP subcase shown in C2.
- For both profile text actions, the specific case of blank text with no
  nonblank instruction is rejected before persistence by Stage A's service
  boundary. It is a confirmed PASS subcase, not a full action PASS.

### C4. Confirmed GAP

1. **Presence is not authorization for generated profile content.** A blank
   `bio_set_text` or `username_set_text` value plus any nonblank
   `ai_instruction` passes `_semantic_completeness_error`. The service receives
   no original user request and cannot prove that the provider did not invent
   the generation request. The NL source-fidelity gate repairs an instruction
   only when `derive_policy(request).active`; otherwise the model-supplied
   nonblank instruction remains sufficient.
2. **Nested required arguments are not checked at creation.** Empty/missing
   `template`, `content`, `link`, query, deletion scope/IDs, save code, setting
   key/value, nested task request, task IDs/status/version, and other fields in
   C2 all reach `repository.create_task` before their tool-level validators run.
3. **Malformed optional values can also be persisted.** The candidate boundary
   does not validate nested argument types, so invalid `status`, `fields`,
   memory options, and supplied mood values are stored in the action snapshot
   and fail or normalize only during execution. `send_message.font` is the
   explicit exception because the candidate canonicalizer validates it.
4. **Provider non-invention is not runtime-enforced.** The interpreter's
   NON-INVENTION CONTRACT says not to fabricate values, but it is prompt text.
   The deterministic creation boundary does not compare ordinary static action
   arguments or arbitrary `ai_instruction` against the original request.

### C5. UNCERTAIN

No action-level C2 result is UNCERTAIN: each listed required field or optional
bounded field was either read in its tool implementation or explicitly treated
as an accepted omission/default. The unresolved questions are not evidence
shortfalls for this stage: recursive `create_task` meaning and destination
trust are separated into Stage D and the later Stage E boundary.

### C6. Stage C verdict

Stage C classified **2 PASS**, **25 GAP**, and **0 UNCERTAIN** action-level
findings. The PASS count treats the documented empty/missing mood cases and
profile blank-text rejection as PASS subcases rather than full action PASS
rows; the two full PASS actions are `send_message` and
`list_recent_messages`. The dominant confirmed defect is that nested action
arguments are schema-shaped but not semantically validated before
`repository.create_task`; the existing profile boundary is only a narrow
exception and does not prove generation authorization. No fix was implemented.

---

## Stage D — Scheduled runtime-context enforcement

### D1. Scope

This stage compares every Stage B runtime-context requirement with the context
actually constructed for a scheduled occurrence. The source path is:

```
RuntimeSupervisor._start_task_scheduler
→ base ToolContext(telegram=TelegramAPI(client), owner_id, tz_str, client, extra=None)
→ TaskExecutionCoordinator.execute
→ optional extra = {"chat_id": task.notification_destination["chat_id"]}
→ ToolExecutor.execute_calls(context_override=execution_context)
→ tool.execute()
```

The base context is constructed in
`backend/runtime/supervisor.py::RuntimeSupervisor._start_task_scheduler`.
`backend/ai/task_execution.py::TaskExecutionCoordinator.execute` copies only a
nonzero persisted destination `chat_id` into `extra`. The event dispatcher
(`backend/ai/task_event_dispatcher.py::_execute_one`) calls the same coordinator
and does not add the triggering message as `reply_msg` or add a dynamic event
chat to the tool context.

The four concepts are kept separate below: the tool's requirement, what the
scheduled path injects, whether creation rejects a missing value, and what the
tool does when the value is absent.

### D2. Runtime-context matrix

| Action | Required context | Scheduled context actually injected | Creation-time guard | Runtime fail-closed behavior | Verdict | Evidence |
|---|---|---|---|---|---|---|
| `save` | `extra["reply_msg"]` with chat/message identity; Telegram client | Telegram/client supplied; no `reply_msg` | None; zero-argument action can persist | `SaveTool.execute` returns `No replied message to save.` before service call | **GAP** | `backend/ai/tools/save.py::SaveTool.execute`; `backend/runtime/supervisor.py::_start_task_scheduler`; `backend/ai/task_execution.py::TaskExecutionCoordinator.execute` |
| `delete_replied` | `extra["reply_msg"]` with chat/message identity; Telegram client | Telegram/client supplied; no `reply_msg`, including event-trigger executions | None; zero-argument action can persist | Returns `No replied message to delete. Reply to a message first.` | **GAP** | `backend/ai/tools/delete.py::DeleteRepliedTool.execute`; `backend/ai/task_event_dispatcher.py::_execute_one` |
| `delete` | `extra["chat_id"]`; `request_message_id`/`reply_msg` only help resolve an implicit message boundary | Only a nonzero persisted destination chat ID is copied; no request/reply metadata | None; empty destination and boundary-dependent modes can persist | Missing chat → `No chat context for deletion.`; missing `until_message` boundary → `No message boundary could be resolved` | **GAP** | `backend/ai/tools/delete.py::DeleteTool.execute`; `backend/ai/task_execution.py::TaskExecutionCoordinator.execute` |
| `delete_by_id` | `extra["chat_id"]` plus Telegram client | Conditional destination chat ID; base Telegram/client | None | `No chat context for deletion.` | **GAP** | `backend/ai/tools/delete.py::DeleteByIdTool.execute`; `TaskExecutionCoordinator.execute` |
| `delete_message_by_id` | `extra["chat_id"]` plus Telegram client | Conditional destination chat ID; base Telegram/client | None | `No chat context for deletion.` | **GAP** | `backend/ai/tools/delete.py::DeleteMessageByIdTool.execute`; `TaskExecutionCoordinator.execute` |
| `list_recent_messages` | `extra["chat_id"]` plus Telegram client | Conditional destination chat ID; base Telegram/client | None | `No chat context available.` | **GAP** | `backend/ai/tools/semantic.py::ListRecentMessagesTool.execute`; `TaskExecutionCoordinator.execute` |
| `delete_messages_by_ids` | `extra["chat_id"]` plus Telegram client | Conditional destination chat ID; base Telegram/client; IDs are persisted but not contextualized | None | `No chat context available.` before deletion; with chat, IDs are re-fetched and ownership-checked | **GAP** | `backend/ai/tools/semantic.py::DeleteMessagesByIdsTool.execute`; `TaskExecutionCoordinator.execute` |
| `retrieve_save` | Nonzero `extra["chat_id"]` plus Telegram client | Conditional destination chat ID; base Telegram/client | None | `No trusted destination chat is available; nothing was retrieved.` | **GAP** | `backend/ai/tools/retrieve_save.py::RetrieveSaveTool.execute`; `TaskExecutionCoordinator.execute` |
| `save_by_link` | Telegram client/raw client for link resolution and Deep Save | Supplied by the supervisor's base context | No special context guard needed in normal scheduler wiring | Returns an honest no-client failure if the base client is absent; normal path supplies it | **PASS** | `backend/ai/tools/save.py::SaveByLinkTool.execute`; `backend/runtime/supervisor.py::_start_task_scheduler` |
| `bio_on` | `context.telegram.client` | Supplied in the base context | No special guard needed in normal scheduler wiring | Service/tool catches failures and returns a failed result | **PASS** | `backend/ai/tools/bio.py::BioOnTool.execute`; `RuntimeSupervisor._start_task_scheduler` |
| `username_on` | `context.telegram.client` | Supplied in the base context | No special guard needed in normal scheduler wiring | Service/tool catches failures and returns a failed result | **PASS** | `backend/ai/tools/username.py::UsernameOnTool.execute`; `RuntimeSupervisor._start_task_scheduler` |
| `get_bio` | `context.telegram` | Supplied in the base context | No special guard needed in normal scheduler wiring | `Telegram is not available.` if absent; normal path supplies it | **PASS** | `backend/ai/tools/bio.py::BioGetTool.execute`; `RuntimeSupervisor._start_task_scheduler` |
| `account_show` | `context.telegram` | Supplied in the base context | No special guard needed in normal scheduler wiring | `Telegram is not available.` if absent | **PASS** | `backend/ai/tools/account.py::AccountShowTool.execute`; `RuntimeSupervisor._start_task_scheduler` |
| `web_search` | `extra["provider_manager"]` is optional; engine fallback is part of the tool contract | No manager in `extra`; `WebSearchTool`/service resolve `get_engine().provider_manager` | No guard needed for the optional key | Engine absence returns a controlled unavailable/failed result | **PASS** for context resolution | `backend/ai/tools/websearch.py::WebSearchTool.execute`; `backend/services/web_search_service.py::do_web_search` |
| `create_task` | `extra["provider_manager"]` is optional with engine fallback; `request_id` is tracing-only; `chat_id` supplies a nested default destination when present | No manager/request metadata in `extra`; manager falls back to engine; absent chat remains absent | No guard for nested destination or recursive semantics | Missing manager fails honestly; behavior of a nested scheduled `create_task` without chat context is not established | **UNCERTAIN** | `backend/ai/tools/task.py::_execute`; `backend/ai/task_execution.py::TaskExecutionCoordinator.execute` |
| `send_message` | Telegram client; `extra["chat_id"]` is optional because owner ID is fallback | Base Telegram/client supplied; destination chat copied when available, otherwise tool falls back to `owner_id` | No special context guard needed | If owner ID and transport are unavailable, returns trusted-destination/transport failure | **PASS** for context availability; destination trust remains Stage E | `backend/ai/tools/message.py::SendMessageTool.execute`; `RuntimeSupervisor._start_task_scheduler` |

**Concrete GAP paths:**

- `save` / `delete_replied`:

  ```
  task creation → persisted zero-argument action
  → scheduled base context with no reply_msg
  → tool.execute()
  → controlled no-reply failure; no source message can be resolved
  ```

- Chat-dependent family:

  ```
  task creation with notification_destination={}
  → repository.create_task
  → scheduled coordinator sees no persisted chat_id
  → extra has no chat_id
  → tool.execute()
  → controlled no-chat failure before the Telegram operation
  ```

  For `delete(mode="until_message")`, the scheduled context also has no
  `reply_msg` or `request_message_id`; only a durable `boundary_id` can make
  that mode self-contained. This is a second, source-proven context gap.

These are creation-time enforcement gaps even though the execution behavior is
fail-closed. They do not assert that a missing-context tool performs an unsafe
side effect.

### D3. Confirmed PASS

1. Normal supervisor wiring supplies a live `TelegramAPI` and raw client, so
   `save_by_link`, `bio_on`, `username_on`, `get_bio`, and `account_show` do not
   lack their base Telegram context at scheduled execution.
2. `web_search` does not require a manager in `extra`; the service explicitly
   resolves the engine manager and returns a controlled failure when the engine
   is unavailable.
3. `send_message` has a documented owner-ID destination fallback, so lack of a
   persisted chat ID alone does not make its required context unavailable.

### D4. Confirmed GAP

1. `save` and `delete_replied` are immediate-reply actions. Their required
   `reply_msg` metadata is never injected by either the time scheduler or the
   event dispatcher, while creation has no guard against persisting them.
2. `delete`, `delete_by_id`, `delete_message_by_id`,
   `list_recent_messages`, `delete_messages_by_ids`, and `retrieve_save` can
   persist with an empty destination. The coordinator adds `chat_id` only from
   a nonzero persisted destination, and each tool fails closed when it is
   absent. `delete` additionally lacks an implicit scheduled boundary unless a
   durable `boundary_id` was persisted.

### D5. UNCERTAIN

- `create_task` can resolve a missing scheduled `provider_manager` through the
  process engine, but source evidence here does not establish whether
  recursively creating tasks is a supported finite contract or whether a
  nested task with no scheduled chat destination is semantically valid.
- `delete_messages_by_ids` can receive a chat when one is persisted, but
  whether literal message IDs remain meaningful durable targets over time is an
  argument-lifetime question, not a missing context-key finding. It remains
  outside this stage's context verdict.

### D6. Stage D verdict

Stage D classified **7 PASS**, **8 GAP**, and **1 UNCERTAIN** context findings.
The supervisor supplies the base Telegram context and the documented manager
fallback, but only conditionally supplies a destination chat ID and never
supplies immediate reply metadata. Thus reply-dependent actions are
persistable but unexecutable on the scheduled path, and chat-dependent actions
are persistable without a guaranteed chat context. All observed runtime
protections fail closed at execution time; none replaces a creation-time guard.
No fix was implemented.

### Combined C+D Findings

**Confirmed creation-layer gaps (Stage C):**

- Required nested fields for profile templates, memory writes, link saves,
  searches, deletes, retrieval, settings, web search, nested task creation,
  and task-management actions are not validated before `repository.create_task`.
- The profile boundary rejects blank text without an instruction, but a
  nonblank instruction is accepted without deterministic proof of explicit
  user generation intent; non-string profile text can also satisfy the current
  coercive check.
- Malformed optional nested values are also persistable for moods, list/task
  filters, account fields, and memory-list options. The send font is the
  explicit candidate-layer exception.

**Confirmed scheduled-context gaps (Stage D):**

- `save` and `delete_replied` require immediate reply metadata that scheduled
  execution never provides.
- The chat-dependent deletion/list/retrieval family can persist without a
  destination `chat_id`, and `delete` can lack a scheduled message boundary.

**Execution-time-only protections:**

- Tool argument checks, no-context failures, registry lookup, permission
  handling, Telegram ownership re-fetches, and bounded service failures occur
  after persistence. They are useful defense-in-depth, not pre-persistence
  semantic completeness.
- The interpreter NON-INVENTION text is provider guidance, not a runtime
  validator.

**Unresolved for Stage E (not investigated):**

- Whether every task-creation path populates `notification_destination.chat_id`
  from trusted state, and whether model-supplied destination fields can survive
  or bypass that resolution.
- Whether persisted message IDs are durable semantic targets, and whether event
  trigger source chat should be the execution destination.
- Confirmation/permission semantics for persisted actions remain outside C+D
  and belong to the planned later stage.

Stages C and D are complete at their stated stopping conditions. Stage E was
not started; no production code, tests, configuration, database/schema, UI,
scheduler, provider, ToolExecutor, or Taskloom files were modified.

## Stage E — Trusted vs model-supplied fields

**VERDICT: CONFIRMED GAP.** A provider-supplied numeric `notification_destination.chat_id` survives when no trusted request `chat_id` and no resolvable `chat_name` exist. `TaskCandidate.from_untrusted()` only type-checks the destination as a dict; `CreateTaskTool._execute()` overwrites it only in trusted-id or successful-name branches; `TaskCreationService.create()` and the repository then persist it. `TaskExecutionCoordinator.execute()` reads the persisted id and injects it into `ToolContext.extra`, where `SendMessageTool.execute()` uses it as its destination. A bounded in-process reproduction retained `987654321` from candidate through the in-memory `TaskRecord`, confirming the previous hypothesis.

### Field classification

| Field/path | Classification | Actual flow |
|---|---|---|
| `ToolContext.extra["chat_id"]` at creation | **TRUSTED** | Read from runtime request context by `CreateTaskTool`; never provider output. |
| `notification_destination.chat_id` | **MODEL_SUPPLIED_VALUE_SURVIVES** | Not validated or stripped by `TaskCandidate`; survives the no-trusted-id/no-name branch into persistence and later scheduled context injection. |
| `notification_destination.chat_name` | **REJECTED** | Bounded, popped, and resolved against authenticated dialogs; unresolved/ambiguous resolution returns before persistence. |
| `notification_destination.chat_title` | **MODEL_SUPPLIED_VALUE_SURVIVES** | Not validated or stripped without `chat_name`; may persist as metadata. A resolved name replaces it with resolver output. |
| Destination keys in send aliases | **REJECTED** | `_canonicalize_action()` retains only bounded text and optional allow-listed font; `SendMessageTool` uses trusted context. |
| Event trigger names | **OVERWRITTEN_BY_TRUSTED_VALUE** | `resolve_trigger_references()` maps names to authenticated-dialog ids before persisting the resolved trigger. |
| Event trigger numeric ids in model-facing data | **REJECTED** | `validate_trigger_spec()` permits unresolved names, not `sender_id`/`chat_id`. |
| Event trigger display fields in model-facing data | **REJECTED** | Not allowed in the unresolved trigger vocabulary; resolved display values come from trusted dialogs. |

### Exact gap path

```text
provider candidate → TaskCandidate.from_untrusted
→ CreateTaskTool._execute (no trusted id/name overwrite)
→ TaskCreationService.create → TaskRepository.create_task
→ persisted notification_destination.chat_id
→ TaskExecutionCoordinator.execute destination injection
→ ToolExecutor → SendMessageTool.execute → persisted numeric destination
```

A trusted request chat id or successful chat-name resolution overwrites the model value. `_deliver_result()` also reads the task destination for opted-in result delivery. No live Telegram, Supabase, provider, or Render call was performed; no fix was implemented.

## Stage F — Permission- and confirmation-sensitive actions

**VERDICT: one confirmed GAP and three PASS classifications; no UNCERTAIN result.** `settings_set` is persistable but cannot execute on a scheduled occurrence because its ADMIN_ONLY confirmation cannot be supplied. `organize_clean` and the other registered DANGEROUS actions pass under the explicit single-owner contract. No built-in registered action declares `CONFIRMATION_REQUIRED`.

### Action classification

| Action | Declared level | Result | Source-backed reason |
|---|---|---|---|
| `settings_set` | `PermissionLevel.ADMIN_ONLY` | **GAP** | `SettingsSetTool.permission_level()` returns ADMIN_ONLY. `execute_calls()` uses `confirmed=False`; `_is_auto_executable()` excludes ADMIN_ONLY, returning `needs_confirmation=True` / `confirmation_required` without invoking the tool. |
| `organize_clean` | `PermissionLevel.DANGEROUS` | **PASS** | `_is_auto_executable()` includes DANGEROUS, so scheduled execution invokes `OrganizeCleanTool.execute()` directly under the owner-only authorization contract. |
| Other registered DANGEROUS actions | `PermissionLevel.DANGEROUS` | **PASS** | Same executor branch and their own bounded tool/service checks. |
| Built-in `CONFIRMATION_REQUIRED` action | None registered | **PASS / not applicable** | The enum exists, but the inspected registry has no built-in tool returning it; such a tool would be blocked by the same unconfirmed branch. |

### Complete `settings_set` path

```text
candidate settings_set action
→ TaskCandidate.from_untrusted (shape only; no permission check)
→ TaskCreationService.create → repository persistence
→ occurrence.action_snapshot
→ TaskExecutionCoordinator.execute → ToolExecutor.execute_calls
→ _execute_single(confirmed=False)
→ ADMIN_ONLY blocked: needs_confirmation=True / confirmation_required
→ existing failure handling marks the occurrence failed
```

The bounded reproduction persisted the action and observed `needs_confirmation=True`, `error="confirmation_required"`, and `success=False`; `SettingsSetTool.execute()` was not invoked. This is a confirmed contract gap, not an implemented fix.

### Stage F conclusion

The executor's interactive `execute_confirmed()` path remains separate and is not available to scheduled execution. No permission system, scheduler, executor, persistence path, or action behavior was modified. No live Telegram, Supabase, provider, or Render call was performed.

**Stage G and Stage H were NOT started.**

## Stage G — Residual unknowns

Stage G re-opened only the items left UNCERTAIN or unclassified after Stages A–F. The audited revision is `main` at `a3eb1c38a4854393d0e80e1c0d13e547d7`. No historical A–F finding was re-audited, rewritten, or deleted, and no production code, tests, configuration, schema, UI, scheduler, or provider file was modified.

### Scope executed

| Source read | Purpose |
|---|---|
| `backend/ai/tools/websearch.py` | resolve the Stage B `web_search` argument uncertainty |
| `backend/ai/tools/memory.py` | resolve the Stage B `memory_*` requirement uncertainty |
| `backend/ai/tools/task_management_tools.py` | resolve the Stage B `task_*` requirement uncertainty |
| `backend/ai/task_candidate.py`, `backend/ai/task_creation.py` | confirm whether nested action arguments are validated before persistence |
| `backend/ai/task_trigger.py`, `backend/ai/task_event_dispatcher.py`, `backend/ai/task_execution.py` | confirm event-source vs persisted-destination separation |

### G1. Residuals resolved

| Residual | Verdict | Decisive evidence |
|---|---|---|
| `web_search` argument requirements | **GAP** | `WebSearchTool.execute` rejects a missing or blank `query`, but `TaskCandidate.from_untrusted` and `TaskCreationService.create` never inspect nested action arguments, so an action with an empty `query` reaches `TaskRepository.create_task` and fails only at execution. |
| `memory_store` argument requirements | **GAP** | `MemoryStoreTool.execute` requires bounded nonblank `content`; that requirement is enforced only after the task is already durable. |
| `memory_list` argument requirements | **PASS** | `MemoryListTool` declares no required argument; omitted `tier`/`query`/`limit` are valid defaults. |
| `task_*` argument requirements | **GAP** | `TaskInspectTool`, `TaskTransitionTool`, and `TaskDeleteTool` require positive IDs, positive `expected_version`, and/or allow-listed status; creation validates none of these before `repository.create_task`. |
| Persistence path that bypasses the semantic-completeness boundary | **PASS (no bypass found)** | The inspected creation callers use `TaskCreationService.create`, which is itself the caller of `repository.create_task`; the boundary runs before payload assembly, so no inspected path persists around it. |
| Event-trigger source vs action destination | **PASS as a data-flow fact** | `resolve_trigger_references` resolves trigger names to trusted IDs. The event dispatcher consumes event data for matching, occurrence identity, and timestamps, while the coordinator delivers using the persisted `notification_destination`. These are distinct fields with distinct authority. |

### G2. Remaining uncertainties

| Residual | Verdict | Exact uncertainty |
|---|---|---|
| Recursive scheduled `create_task` | **UNCERTAIN** | A scheduled occurrence can reach the same creation service through the engine fallback, but the source defines no bounded or guaranteed-terminating contract for tasks that create further tasks. No recursive durable execution was attempted because it would write task state. |
| Literal Telegram message-ID durability | **UNCERTAIN** | Persisted IDs are re-fetched in the persisted chat with outgoing-only verification, but the source cannot prove that a delayed ID still denotes the intended semantic target. Live Telegram behavior was explicitly out of scope. |

### G3. Stage G result

**Stage G: PASS 2, GAP 3, UNCERTAIN 2.** H1 (Stage E) and H2 (Stage F) remain confirmed findings rather than residual uncertainties.

## Stage H — Final verdict and remaining gaps

Latest revision actually audited by Stages A–G: `main` at `a3eb1c38a4854393d0e80e1c0d13e547d7`.

1. **Is semantic completeness enforced for every supported task action?** No. It is enforced for `send_message` only, plus the narrower profile gates recorded in Stages A–F.
2. **Which actions are PASS?** `send_message`, `bio_on`, `bio_off`, `bio_show`, `get_bio`, `username_on`, `username_off`, `username_show`, `list_saves`, `list_recent_messages`, `memory_list`, `organize_list`, `database_stats`, `task_list`, and `account_show` (fails closed on invalid input).
3. **Which actions are confirmed GAP?** `web_search`, `memory_store`, the `task_*` management actions, `bio_set_template`/`username_set_template`, `bio_set_mood`/`username_set_mood`, `delete`, `delete_by_id`, `delete_message_by_id`, `delete_messages_by_ids`, `save_by_link`, `search`, `settings_get`, `retrieve_save`, `create_task`, `save`, `delete_replied`, and any unregistered action name.
4. **Which actions remain UNCERTAIN?** Only the two Stage G residuals: recursive scheduled `create_task` termination and delayed literal message-ID durability.
5. **Can model-supplied destination/identifier values bypass trusted resolution?** Yes. H1 is confirmed: a model-supplied numeric `chat_id` inside `notification_destination` survives when no trusted request chat ID exists and no `chat_name` resolves, and is later injected into the scheduled tool context.
6. **Can confirmation-sensitive actions become invalid scheduled tasks?** Yes. `settings_set` declares `PermissionLevel.ADMIN_ONLY` and is persistable, but the scheduled execution path calls `execute_calls(confirmed=False)`, so `_is_auto_executable()` excludes it and the occurrence terminates with `needs_confirmation` / `confirmation_required`.
7. **Which actions are immediate-only because required runtime context is unavailable to scheduled execution?** `save` and `delete_replied`, because the coordinator never injects `extra["reply_msg"]` for a scheduled occurrence.
8. **Can unregistered actions persist and only fail at execution?** Yes. The candidate and creation layers do not consult the tool registry; membership is first checked by `TaskExecutionCoordinator.execute`, which fails the occurrence with `unregistered_action`.
9. **Can schema-valid but semantically incomplete candidates still reach persistence?** Yes. Schema validity alone is not sufficient, and the current boundary does not check semantic completeness for every action.

### CONFIRMED FACTS

- The canonical creation path remains `TaskCandidate` → `CreateTaskTool`/wizard → `TaskCreationService.create` → `TaskRepository.create_task`; no separate inspected write path bypasses the service.
- H1 is confirmed: model-supplied `notification_destination.chat_id` can survive without trusted replacement and reaches scheduled tool context.
- H2 is split: `settings_set` is a confirmed scheduled confirmation gap, while `organize_clean` and the other DANGEROUS actions pass under the explicit owner-only contract.
- `save` and `delete_replied` are immediate-only; unregistered action names are rejected at occurrence execution rather than at creation.

### CONFIRMED GAPS

1. Required or malformed nested action arguments are not universally validated before persistence.
2. Model-supplied destination identifiers can survive without trusted replacement and reach scheduled destination/result-delivery context.
3. Reply-dependent actions can be persisted although scheduled execution has no reply metadata.
4. `settings_set` can be persisted although scheduled execution has no ADMIN_ONLY confirmation round-trip.
5. A nonblank model-supplied `ai_instruction` can satisfy the narrow profile check without universal proof of user authorization.
6. Unregistered action names are rejected too late, at occurrence execution rather than creation.

### REMAINING UNCERTAINTIES

- Whether recursive or chained scheduled `create_task` actions are supported, and what finite termination rule applies.
- Whether delayed literal Telegram message IDs remain durable semantic targets.
- Whether a complete required-field contract exists for every static action (Stages A–C record the missing general provenance enforcement without defining one).

### NOT INVESTIGATED / OUT OF SCOPE

No production fix, migration, SQL, schema change, provider change, scheduler/executor redesign, UI work, or live Telegram, Supabase, Render, or provider call was performed. Stages A–F were not re-audited or rewritten. Stages G and H are investigation and handoff only.

### Final implementation handoff targets

1. `backend/ai/task_creation.py::TaskCreationService.create` — required nested arguments are not validated before persistence. Expected invariant after the fix: schema-invalid or incomplete nested arguments never reach `repository.create_task`. Focused regression test: incomplete candidate is rejected with a repository non-invocation assertion. Affects immediate and scheduled creation.
2. `backend/ai/tools/task.py::CreateTaskTool._execute` plus the service boundary — a provider-supplied `ai_instruction` can act as ungrounded authorization for generated content. Expected invariant: generated content is used only when authorized by the user's request. Focused regression test: omitted, blank, invented, and verbatim authorization cases. Affects both immediate and scheduled creation.
3. `backend/ai/tools/task.py::CreateTaskTool._execute` / `backend/ai/task_candidate.py::TaskCandidate.from_untrusted` — model-supplied destinations are not stripped. Expected invariant: only trusted or explicitly resolved destinations reach persistence and scheduled context. Focused regression test: trusted, resolved, absent, and model-ID cases. Affects both paths.
4. The existing creation eligibility boundary for reply-dependent actions — `save` and `delete_replied` are persistable but unexecutable. Expected invariant: no action is persisted whose required runtime context scheduled execution cannot provide. Focused regression test: creation rejection for those actions while the immediate reply flow still works. Affects creation and scheduled execution.
5. The existing creation eligibility boundary plus `ToolExecutor` — `settings_set` is persistable but cannot satisfy confirmation on the scheduled path. Expected invariant: `settings_set` never becomes an invalid scheduled task, without changing interactive confirmation. Focused regression test: scheduled persistence rejection plus unchanged admin confirmation flow. Affects scheduled execution.
6. The candidate/service action-name boundary — unregistered action names persist and fail late. Expected invariant: unregistered action names are rejected before persistence while occurrence-time registry defense-in-depth is retained. Focused regression test: unknown-name rejection at creation with the coordinator check still present. Affects creation.

The two UNCERTAIN items are excluded from implementation targets until their contracts are defined. No fix was implemented in this stage.

## Stage I — Recursive/chained create_task semantics

**Scope and audited revision.** The audited checkout is `main` at
`86b00c69cbb23c184100be9cfdd0868edd895a45` (the revision carrying the
semantic-completeness boundary fix). Stage G recorded the recursive
`create_task` question as UNCERTAIN at `a3eb1c3`; this stage re-answers it at
the current revision. Only `INVESTIGATION.md` was modified. No fix was
implemented. No live Telegram, Supabase, Render, or provider call was made;
the two reproductions below are bounded, in-process, and use an in-memory task
repository with a stub provider and the REAL `CreateTaskTool`,
`TaskCreationService`, `ToolExecutor`, and `TaskExecutionCoordinator`.

### I0. Sources inspected

| Source | What it establishes |
|---|---|
| `backend/ai/tools/task.py::CreateTaskTool.execute` / `_execute` | the scheduled-reachable creation tool: provider resolution, destination resolution, `TaskCreationService` call |
| `backend/ai/task_interpreter.py` (`interpret`, prompt, `CANDIDATE_SCHEMA`) | what action names the provider contract asks for and whether `create_task` is excluded |
| `backend/ai/task_candidate.py::_canonicalize_action` / `from_untrusted` | which action names survive candidate validation |
| `backend/ai/task_creation.py::TaskCreationService.create` | the single persistence boundary and its eligibility checks |
| `backend/ai/task_execution.py::TaskExecutionCoordinator.execute` / `_fresh_context` | what runtime context a claimed occurrence supplies to tools |
| `backend/ai/task_scheduler.py` | per-wake work bounds (`MAX_TASKS_PER_WAKE=10`, `MAX_SWEEPS_PER_WAKE=20`, `MAX_CONCURRENT_EXECUTIONS=4`, `MAX_RETRIES_PER_WAKE=10`) |
| `backend/ai/database/task_repository.py::create_task` | the persisted task payload (any per-owner ceiling?) |
| `backend/runtime/supervisor.py::_wire_ai_tools`, `::_start_task_scheduler` | production wiring: the same `ProviderManager` on the default Engine, and the coordinator's base `ToolContext` |
| `backend/ai/tools/executor.py` (`execute_calls`, `session_id` handling) | whether the execution path carries any origin marker |

### I1. Can a durable task create another task? CONFIRMED YES

`TaskCandidate.from_untrusted` normalizes ONLY the message-writing aliases
(`_SEND_ACTION_ALIASES` → `send_message`); every other action name is passed
through unchanged as `{"name": <name>, "arguments": <object>}`. `create_task`
is a registered tool, so:

- a provider candidate whose action is
  `{"name": "create_task", "arguments": {"request": "..."}}` passes candidate
  validation;
- the creation boundary added in `86b00c6` only requires `create_task`'s
  declared `request` argument, so such a candidate reaches
  `repository.create_task`;
- the request text of the nested creation is model-supplied, so the nested
  candidate is produced by the SAME provider interpretation path.

The interpreter prompt (`task_interpreter.py`) enumerates `send_message`,
`bio_set_text`, `username_set_text`, and read/report actions, and says "Never
invent other action names: an action name that is not registered is rejected."
It does NOT name `create_task` as a permitted action, but neither the prompt
nor any validator forbids it, and `create_task` IS registered — so the contract
is permissive here, not prohibitive.

### I2. Can that creation actually run at occurrence time? CONFIRMED YES

`TaskExecutionCoordinator.execute` builds `execution_context` from
`self.context` (`_fresh_context`) and injects only the trusted `chat_id` taken
from the task's persisted `notification_destination` (task_execution.py lines
330–344). Production constructs that base context in
`RuntimeSupervisor._start_task_scheduler` with `extra` unset, so an occurrence
context carries NO `provider_manager`. `CreateTaskTool._execute` then falls
back to `from backend.ai.engine.engine import get_engine; provider_manager =
get_engine().provider_manager`, and the supervisor wires that same manager onto
the default Engine in `_wire_ai_tools`. The occurrence therefore runs the real
interpreter and the real creation service. The
`session_id=f"task:{task_id}:{occurrence_key}"` the coordinator passes is used
only for tool-history recording (`executor.py::_record_history`) and is never
consulted by task creation.

### I3. Is there a termination/depth/recursion bound? CONFIRMED NONE

- No lineage metadata exists: the persisted task payload keys are exactly
  `actions`, `ai_instruction`, `created_at`, `id`, `label`, `next_run_at`,
  `notification_destination`, `owner_id`, `schedule`, `schedule_type`, `status`,
  `terminal_at`, `timezone`, `updated_at`, `version` — no `parent_task_id`,
  depth, generation, or origin field.
- No context flag marks a creation as task-originated (only `chat_id` and,
  for the interactive path, `provider_manager` / `request_id` / a deterministic
  candidate are ever read).
- No per-owner task ceiling exists in `TaskCreationService.create` or in the
  repository `create_task` implementations.
- The scheduler constants bound WORK PER WAKE (how many due tasks one sweep
  claims) and concurrency; they do not bound how many tasks may exist or how
  many a task may create over time.
- `MAX_ACTIONS = 5` bounds actions per task (at most five `create_task`
  actions per occurrence), which is a constant multiplier, not a terminator.

### I4. Bounded in-process reproductions

Reproduction 1 — repeated creation from one recurring parent (stub provider
returns `create_task` for the parent request and `send_message` for the child
request; in-memory repository; real registry/executor/coordinator):

```
parent create success: True
parent action snapshot: [{'name': 'create_task', 'arguments': {'request': 'every 1 minute send hello'}}]
occurrence occ0: success=True status=succeeded tasks_now=2
occurrence occ1: success=True status=succeeded tasks_now=3
occurrence occ2: success=True status=succeeded tasks_now=4
final task count: 4   (all status=active, all scheduled)
child payload keys: [... no parent/depth field ...]
```

Reproduction 2 — self-replication (stub provider returns a `create_task`
action for EVERY interpretation):

```
gen0 -> child created: True succeeded
gen1 actions: [{'name': 'create_task', 'arguments': {'request': 'every 1 minute create a task that sends hello'}}]
gen1 -> grandchild created: True succeeded
gen2 actions: [{'name': 'create_task', 'arguments': {'request': 'every 1 minute create a task that sends hello'}}]
total durable tasks: 3
all tasks recurring: True
```

Both runs were bounded (3 and 2 occurrences) and wrote only in-memory state.

### I5. Normal tasks vs task-created-by-task

Both enter through the same `CreateTaskTool` → `TaskInterpreter` →
`TaskCreationService` → `TaskRepository` chain. Nothing distinguishes them:
not the owner (single owner), not the destination (the child inherits the
parent's trusted `chat_id` when present), not the payload, and not the
`session_id`. A later fix therefore cannot identify task-originated creations
from persisted state alone — it would need explicit lineage metadata or a
creation-time guard, neither of which exists today.

### I6. Stage I verdict

**CONFIRMED GAP.** A durable task can hold a `create_task` action, that action
runs through the real interpreter and creation service at occurrence time, and
there is no depth, lineage, or population bound anywhere in the source. A
recurring parent creates one new durable task per occurrence indefinitely
(linear growth), and a candidate that reproduces `create_task` in its children
compounds (each generation stays `active` and keeps firing). Whether a given
request produces such a candidate depends on model output, so the honest
statement is: the source guarantees neither a termination bound nor a
prohibition. No fix was implemented.


## Stage J — Delayed Telegram message-ID semantics

**Scope.** Same audited revision (`86b00c6`). This stage traces every supported
task action that can address a literal Telegram message ID after a delay, and
distinguishes (a) ID durability from (b) plain integer persistence,
(c) chat authority, and (d) ownership re-verification. No fix was implemented;
no live Telegram, Supabase, Render, or provider call was made.

### J0. Sources inspected

| Source | What it establishes |
|---|---|
| `backend/ai/tools/delete.py::DeleteByIdTool` | "delete from this ID onward", trusted chat source, fetch + `out` check |
| `backend/ai/tools/delete.py::DeleteMessageByIdTool` | single-ID deletion, trusted chat source, fetch + `out` check |
| `backend/ai/tools/semantic.py::DeleteMessagesByIdsTool` | ID-list deletion contract ("never invent IDs") |
| `backend/services/delete_service.py` (`delete_verified_self_messages`, `do_del_id_counts`, `_iter_messages_bounded`, `_is_self_owned`, `_resolve_me_id`) | the single deletion chokepoint and its fail-closed rules |
| `backend/ai/tools/save.py::SaveByLinkTool` + `save_service.parse_telegram_link` | link-encoded chat+message reference resolved at execution |
| `backend/ai/task_execution.py` (destination injection, `action_snapshot`) | what the occurrence supplies and what is frozen at creation |
| `backend/ai/database/task_repository.py` (payload/snapshot) | how the ID is persisted and copied into the occurrence |
| `backend/ai/task_creation.py::_action_eligibility_error` | what creation validates about ID arguments |

### J1. Which actions address a literal message ID after a delay

- `delete_message_by_id` — one explicit ID; never escalates to a range.
- `delete_messages_by_ids` — an explicit ID list (bounded by `_MAX_DELETE_IDS`).
- `delete_by_id` — "all outgoing messages from `message_id` forward", i.e. a
  bounded open-ended RANGE anchored by one literal ID
  (`client.iter_messages(chat, min_id=message_id - 1, from_user="me",
  limit=_MAX_DELETE_SCAN_MESSAGES)`).
- `save_by_link` — the chat + message ID are encoded inside a `t.me` /
  `t.me/c/<id>/<msg>` URL stored as text and resolved only at execution.
- Not literal-ID actions: `delete` (count/mode/time predicates),
  `save`/`delete_replied` (rejected at creation since `86b00c6`),
  `retrieve_save` (save code), `task_*` (task IDs, not Telegram message IDs).

### J2. What happens between creation and execution

The action name and its arguments — including the numeric ID(s) — are persisted
verbatim in the task `actions` payload and copied into the occurrence
`action_snapshot` when the occurrence is created. Nothing re-resolves,
re-validates, or rewrites the ID between creation and execution. At execution
the coordinator injects ONLY the trusted `chat_id` from the task's persisted
`notification_destination`; it never injects message identity, and the ID is
never resolved globally.

### J3. Chat authority — CONFIRMED SAFE

Every Telegram call is `(chat_id, id)`-scoped: `client.get_messages(chat_id,
ids=...)` (single-ID tool and ownership verification),
`client.delete_messages(chat_id, batch)`, and
`client.iter_messages(chat_id, min_id=...)` (range tool). `chat_id` must come
from `context.extra["chat_id"]`, which the coordinator sets from the persisted
trusted destination (itself only ever a trusted request chat ID or a
dialog-resolved `chat_name` after `86b00c6`). When no trusted chat context
exists, the tools fail closed — they never fall back to another chat. Because a
message ID is only ever read together with a chat, the same number in a
different chat cannot be targeted.

### J4. Ownership and stale targets — CONFIRMED SAFE

- `DeleteMessageByIdTool` fetches the ID first and requires `msg.out` before
  delegating; a missing message yields "Message N not found in this chat" and
  no deletion.
- `delete_verified_self_messages` (the single deletion chokepoint) re-fetches
  every candidate and applies `_is_self_owned`, which requires a present
  `out` flag, a present `sender_id`, and `sender_id == resolved account id`;
  an unresolved account identity rejects everything. Failures to fetch are
  rejected, not retried into a different target.
- `delete_messages_by_ids` reports `deleted` and `rejected` separately, so a
  stale or non-outgoing ID in a list is skipped rather than retargeted.
- For `delete_by_id` the range boundary is purely NUMERIC
  (`min_id = message_id - 1`), so a deleted/anchor-less ID does not shift the
  boundary; the owner filter is applied by the RPC (`from_user="me"`) and
  again by verification before each delete batch.

### J5. Bounded in-process reproduction

Real tools and real `delete_service` against a fake client (chat `-1001`;
ID 42 = owner's outgoing message, ID 43 = someone else's, ID 999 = absent):

```
A no chat_id       -> False | No chat context for deletion.
B stale id in chat -> False | Message 999 not found in this chat.
C not outgoing     -> False | Message 43 was not sent by the owner, so it cannot be deleted (outgoing-only).
D valid outgoing   -> True  | Deleted message 42. | deleted: [42]
E mixed ids        -> True  | data: {'deleted': [42], 'rejected': [999, 43]}
F ids, no chat     -> False | No chat context available.
G range from id    -> True  | iter kwargs: [{'min_id': 41, 'from_user': 'me', 'limit': 1000}]
```

ID provenance check at the creation boundary (same candidate shape used for
scheduled tasks):

```
H creation delete_message_by_id   -> PERSISTED args={'message_id': 12345}
H creation delete_messages_by_ids -> PERSISTED args={'message_ids': [1, 2]}
H creation delete_by_id           -> PERSISTED args={'message_id': 987654321}
```

### J6. Confirmed gap: literal-ID provenance

The creation boundary validates only presence, emptiness, and the tool's own
declared integer constraints. Nothing checks that a stored `message_id` /
`message_ids` value came from trusted runtime context (there is no trusted
message-ID source in a scheduled creation at all — unlike `chat_id`, which is
overwritten from trusted context). A model-supplied number therefore becomes
the delayed deletion target verbatim. The blast radius is bounded (the
persisted trusted chat, owner-authored messages only), but:

- an invented or misremembered ID can delete a DIFFERENT owner message in the
  persisted chat;
- `delete_by_id` deletes everything from that number forward — including
  messages the owner writes AFTER the task was authored, because the range is
  open-ended by construction.

This is newly observed in Stage J; the A–H handoff covered destination
identifiers (H1), not nested action ID arguments. No fix was implemented.

### J7. Stage J verdict (split by property)

| Property | Verdict | Decisive evidence |
|---|---|---|
| Chat targeting (can execution hit the wrong chat?) | **CONFIRMED SAFE** | every call is `(chat_id, id)`-scoped; `chat_id` comes from the persisted trusted destination; missing chat context fails closed (J3, reproductions A/F) |
| Ownership re-verification at execution | **CONFIRMED SAFE** | fetch-time `out` check plus `_is_self_owned` (`out` + `sender_id == me`) in the single deletion chokepoint (J4, reproductions C/E) |
| Stale/deleted target ID | **CONFIRMED SAFE** | stale IDs resolve to `None` and are rejected; range boundaries are numeric, not anchored on the message object (J4, reproductions B/E/G) |
| Literal-ID provenance at creation | **CONFIRMED GAP** | arbitrary model-supplied numbers persist (J6, reproduction H) |
| Absolute long-delay durability of a `(chat_id, message_id)` pair across Telegram-side state changes (basic-group → supergroup migration, ID remapping, chat-ID reuse) | **STILL UNCERTAIN** | the source proves only that the runtime re-verifies ownership and fails closed when resolution fails; it cannot prove what a server-side migrated/reused identifier denotes. Resolving this needs live Telegram observation, which is explicitly out of scope. |

The Stage G uncertainty ("persisted IDs are re-fetched with outgoing-only
verification, but the source cannot prove a delayed ID still denotes the
intended semantic target") is now split precisely: the source-provable parts
are SAFE, ID provenance is a GAP, and only the Telegram-side identifier
lifetime question remains UNCERTAIN. No fix was implemented for either Stage I
or Stage J; both are investigation results only.

---

## Stage K — Telegram surrounding-message provenance

> **Investigation only.** Same audited revision (`fdcc798`, `feat: add Telegram
> surrounding message context for AI`). No production code, tests, schema,
> migrations, presentation, provider, scheduler, or prompt formatting was
> modified. No live Telegram, Supabase, Render, or provider call was made. Only
> this document changed.

### K1. Scope

Given the live Telegram observation that the newly-added surrounding-message
context contains previous self-bot/AI output, determine **whether the current
architecture can reliably distinguish**, for every message in that window:

| Case | Meaning |
|---|---|
| **A** | a genuine, human-authored owner message |
| **B** | a human owner message later edited **in place** by the AI |
| **C** | a message **created** by the self-bot/AI as a new Telegram message |
| **D** | a temporary AI status/presentation message |
| **E** | another participant's message |
| **F** | a durable or request-scoped marker that separates the above |

The task explicitly forbids solving this by dropping every `sender_id ==
owner_id` message, because the owner legitimately produces both human messages
and AI-touched messages from the same account.

### K2. Current surrounding-context behavior (traced)

```
Telegram event
  → ai_unified._execute_ai                (ai_unified.py:572)
      request_chat_id      = event.chat_id                 (:594)
      request_message_id   = event.message.id              (:595)
      _load_telegram_chat_context(...)                     (:539)
  → fetch_telegram_chat_context           (telegram_context.py:364)
      _read_window: client.iter_messages(chat_id,
           limit=MAX_CONTEXT_MESSAGES, max_id=message_id)  (:309-320)
  → build_chat_context                    (telegram_context.py:241)
      drop current id + exclude_message_ids (reply target) (:262-270)
      drop any id > anchor ("never invent future")         (:271-274)
      sort ascending, cap 10, 200 chars/message, 1500 total
      _to_record -> TelegramContextMessage                 (:205-222)
  → AIRequest(telegram_context=snapshot)  (ai_unified.py:668)
  → ContextBuilder → PromptBuilder render (`[Telegram Chat Context]`)
```

**The window applies no provenance filter of any kind.** `_to_record` reads
`out` (`telegram_context.py:218`) and `sender_id` (:210), but `out` is used only
for the render attribution label `"You"` (:117-125) and to skip sender-name
resolution (:332). Every message the owner's account appears to have sent is
included verbatim — human-authored, AI-edited, or AI-created alike. The
per-message `body` is the message's **current** text (:184-190), which for an
AI-edited message is the AI answer.

Hard bounds (unchanged by this stage): 10 messages, 200 chars/message,
1500 chars total, <=4 sender resolutions, 3.0 s wall-clock
(`telegram_context.py:40-52`).

### K3. Provenance data actually available (per message)

Every field below was traced in source; nothing is assumed.

| Field | Source | Provenance value |
|---|---|---|
| `msg.id` | Telegram; read at `telegram_context.py:214` | identity only — carries no authorship |
| `msg.out` | read at `:218` | True for **any** owner-account message (A, B, C). Cannot separate human from AI |
| `msg.sender_id` | read at `:210` | equals the owner for A, B, C. Cannot separate |
| `msg.from_id` | same lineage as `sender_id` | same limitation |
| `msg.date` | read at `:220` | send time; an AI in-place edit does **not** change it |
| `msg.edit_date` | Telegram attribute, **never read by this module** | set on any edit — including the AI's in-place delivery, but also on human edits. Not authoritative (see K9) |
| `msg.message` / `msg.text` | read at `:184-190` | the **current** text; for B this is already the AI answer. Content is renderer-owned, not a marker |
| `msg.reply_to` | not read | reply linkage only |
| `msg.media` | via `classify_message` at `:192-203` | media presence only |

Request/event-level provenance:

| Signal | Source | Covers |
|---|---|---|
| `event.message.id`, `event.chat_id` | `ai_unified.py:594-595` | the current trigger message only |
| `ReplyContext.is_ai_message`, `ai_session_id`, `ai_role`, `ai_content` | `context_builder.py:69`; produced at `ai_unified.py:493-527`; consumed at `builder.py:294` | the **replied-to** message only |
| **`ReplyResolver`** (`get_resolver()`) | `backend/ai/context/reply_resolver.py` | **the only positive source-level marker that a given Telegram message ID holds AI output** |

`ReplyResolver` is an in-memory singleton: `register(telegram_msg_id, ...)`
(`reply_resolver.py:81-128`) and `resolve(id)` (`:130-141`), RAM-only, with an
LRU cap of `_MAX_ENTRIES = 500` (`:26`, eviction at `:120-127`) and **no
persistence**. It is written after a successful in-place delivery at
`ai_unified.py:803` (`telegram_msg_id=event.message.id`) and read for the
reply-to-AI check at `:495` and `:949`.

Task-side provenance (`task_contract.py`) exists but is **not applicable** here:
`SCHEDULED_OCCURRENCE_EXTRA` (`:21`), `trusted_message_ids` (`:139`), and
`message_reference_provenance_error` (`:190`) govern *task-creation argument*
authority, not the surrounding-window read.

### K4. Message lifecycle analysis

**Case B is the reported symptom, and it is structural.** The AI never sends a
separate answer for the primary path: `deliver_response` edits the triggering
message in place (`delivery.py:668` `await event.edit(messages[0])`), and the
status/thinking states are edits of that same message
(`ai_unified.py:676`, `:681`; `delivery.format_status` :435,
`format_thinking` :424). So:

```
T0  owner types  "هی"                    -> id 100, out=True, sender=owner
T1  AI eats it and edits id 100 in place -> id 100, out=True, sender=owner,
                                            text="└─ سلام...", edit_date bumped
T2  owner asks something else            -> id 101
    surrounding window for id 101 = [id 100]
    id 100 renders as  "You: └─ سلام..."
```

The Telegram message keeps the owner's id, the owner's `sender_id`, and
`out=True`. **`out=True` (or `sender_id == owner_id`) therefore does not prove
human authorship** — exactly the case the task flagged.

**Case C exists and is unmarked.** The self-bot also creates *new* Telegram
messages, none of which are registered anywhere:

| Producer | Source | Registered? |
|---|---|---|
| chunked/split AI answer (`messages[1:]`) | `delivery.py:679` `event.reply(message)` | **no** (returned `Message` discarded) |
| edit-failure fallback for the first chunk | `delivery.py:673` `event.reply(messages[0])` | **no** |
| error/failure fallback reply | `ai_unified.py:831` `event.reply(final_text)` | **no** |
| scheduled `send_message` action | `tools/message.py:133` `telegram.send_message(chat_id, text)` | **no** |
| task result delivery | `task_execution.py:727` `telegram.send_message(chat_id, text)` | **no** |
| task outcome notification | `supervisor.py:380` `TelegramAPI(self.client).send_message(owner, message)` | **no** |
| Deep Save re-upload to Saved Messages | `save_service.py:397`, `:428` `send_message("me", ...)` / `send_file` | **no** |

Only `event.message.id` (`ai_unified.py:803`) is ever registered — one id per AI
turn, for the edited trigger message only.

**Case D is not a separate Telegram message on the primary path.**
`format_thinking` :424 / `format_status` :435 / `format_failure` :449 are all
rendered into the *same* triggering message by `event.edit(...)`
(`ai_unified.py:610`, `:624`, `:676`, `:681`, `:851`) and are overwritten by the
final answer. A transient string such as `Reading messages...` is therefore a
**state of a case-B message**, not a distinct message, and needs no separate
filter. (When delivery falls back to `event.reply`, the transient/final text
becomes a **case-C** message instead.)

**Case E** is trivially distinct: `out=False` and `sender_id != owner` (the
window already renders such senders by display name at
`telegram_context.py:117-125`, `:323-361`).

### K5. Owner-authored vs AI-edited messages

The distinction is **provable in-process and only in-process**:

- `get_resolver().resolve(id) is not None` ⇒ that Telegram message's content was
  replaced by an AI answer (case B).
- `resolve(id) is None` ⇒ **not proof of human authorship** (case A). It also
  covers a case-B message whose marker was evicted (LRU cap 500) or lost to a
  process restart — the resolver is RAM-only (`reply_resolver.py:26`, and the
  module docstring's "process-wide in-memory singleton" contract).

This asymmetry matters: the resolver is usable as a **denylist** ("exclude what
we positively know is AI") but never as an allowlist ("keep only what we know is
human"). Using it as a denylist is conservative and cannot remove a genuine
human message.

The same marker is already used this way in the very same handler for the
reply target (`ai_unified.py:493-495`, `:945-949` →
`ReplyContext.is_ai_message`), so the mechanism is pre-existing architecture,
not a new one.

### K6. AI-generated message detection

| Sub-case | Detectable from current source? | Mechanism |
|---|---|---|
| B — in-place AI answer (the reported symptom) | **YES** | `ReplyResolver.resolve(id) is not None` |
| C — new self-bot message (chunks, fallbacks, sends, notifications, saves) | **NO** | no marker is recorded anywhere |
| D — status/thinking text | **N/A** | not a separate message; it is case B/C |
| A — human message | only as "not known to be AI" | absence in the resolver |
| E — other participant | **YES** | `out=False` |

### K7. Temporary/status message detection

Status/thinking/failure strings are in-place edits of the trigger message
(`ai_unified.py:676`, `:681`, `:610`, `:624`, `:851`). Because `iter_messages`
returns the message's **current** content, a transient status string is never
observed as a separate surrounding message; by the time a later request reads
the window, that id holds either the final answer (case B) or the failure text
(still the same id). No status-specific filter is required — filtering case B
by the resolver subsumes it.

### K8. Safe filtering possibilities (source-provable)

1. **Keep case E, drop nothing else by sender.** `out=False` messages render as
   the other participant (`telegram_context.py:117-125`) and must stay.
2. **Exclude messages positively identified as AI output** — the denylist:
   exclude any window message id for which `ReplyResolver.resolve(id)` returns a
   record. This is deterministic, provider-independent, needs no schema, and
   cannot remove a human message that the system does not *know* to be AI
   (K5). It reuses the exact mechanism already applied to the reply target.
3. **Preserve the existing exclusions** — the current trigger id and the reply
   target (`telegram_context.py:262-270`; `ai_unified.py:539`, `exclude`); and
   never read anything at or after the anchor (`:271-274`).
4. **Preserve attribution** — an AI-cleared window must still render genuine
   owner messages as `You` and other participants by name; nothing about the
   prompt contract, ordering, bounds, or authority statement changes.

### K9. Unsafe heuristics (must not be used)

| Heuristic | Why it is unsafe |
|---|---|
| Text matching on `│`, `─`, `┘`, `└─`, `┘─` | Presentation is renderer-owned and has already changed three times (`IMPLEMENTATION_REPORT.md` RTL phases). Not an authoritative provenance mechanism |
| Text matching on `Reading messages...`, the trigger word, the AI name, `🤖` | These are human-reproducible strings; a human message may contain them and a future status string may not |
| `sender_id == owner_id` / `out=True` | Removes cases A and B together — explicitly forbidden, and would delete legitimate human context (K4) |
| `edit_date is not None` | Humans edit messages too; not read by the module today, and not authoritative |
| Length, recency, Markdown/formatting shape, emoji presence | Correlation, not provenance |
| `is_ai_message` applied to the *whole* window | The field is derived from the resolver for the reply target only; treating it as ground truth for arbitrary messages would be an allowlist built on absence (K5) |
| Asking the provider/model to classify provenance | Non-deterministic and provider-dependent; the task forbids provider-dependent protection |

### K10. CONFIRMED SAFE

- Other participants are identifiable: `out=False` + `sender_id`
  (`telegram_context.py:210`, `:218`, `:117-125`).
- Current trigger message and reply target are already excluded
  (`telegram_context.py:262-270`; `ai_unified.py:539` exclude tuple).
- Messages at/after the anchor are never included (`telegram_context.py:271-274`).
- A positive, in-process marker for AI-in-place content exists: `ReplyResolver`
  (`reply_resolver.py:81-141`), written after a successful delivery
  (`ai_unified.py:803`) and already consumed by `ReplyContext` (`:493-527`;
  `context_builder.py:69`; `builder.py:294`).
- Status/thinking/presentation strings do not persist as separate Telegram
  messages on the primary path (`ai_unified.py:676`, `:681`, `:851`).

### K11. CONFIRMED GAP

- **G-K1 — The surrounding window has no provenance filter at all.** `_to_record`
  reads `out`/`sender_id` only for display (`telegram_context.py:205-222`,
  `:117-125`); `build_chat_context` never consults anything provenance-related
  (`:241-297`). This is the direct cause of the observed symptom.
- **G-K2 — New self-bot messages are unmarked.** Chunked answers, edit-failure
  fallbacks, scheduled sends, task results/notifications, and Deep Save uploads
  create Telegram messages (`delivery.py:673`, `:679`; `ai_unified.py:831`;
  `message.py:133`; `task_execution.py:727`; `supervisor.py:380`;
  `save_service.py:397`, `:428`) whose ids are discarded (`deliver_response`
  returns only counts — `delivery.py:635-683`) and never registered.
- **G-K3 — Provenance is not durable.** The only marker is RAM-only and
  LRU-evicted at 500 entries (`reply_resolver.py:26`, `:120-127`), so after a
  restart *no* previously AI-edited message can be recognized.

### K12. UNCERTAIN

- **U-K1** — Whether the live chat's residual AI text is case B only or also
  case C. Source cannot decide it without the actual chat; the two cases need
  different minimum fixes (K13).
- **U-K2** — Whether the owner ever triggers the AI in Saved Messages, where the
  Deep Save re-uploads (`save_service.py:397`, `:428`) and scheduled
  `send_message` output (`message.py:133`) accumulate as `out=True` messages.
- **U-K3** — Whether provenance must survive a process restart. The reported
  behavior is live-process; if a restart is involved, even case B becomes
  unrecognizable (K11, G-K3).
- **U-K4** — Whether Telegram's `edit_date` is populated for the in-place AI
  edit on every client (relevant only if someone proposes it as a fallback; not
  needed for the resolver path).

### K13. Minimum required fix surface (not implemented)

**Phase 1 — use the marker that already exists (fixes the observed symptom, no
schema, no new subsystem).** Thread provenance into the window build as a
predicate/exclusion set computed once in `_load_telegram_chat_context`, where
`get_resolver` is *already* imported and used (`ai_unified.py:493`, `:945`):

| File | Change |
|---|---|
| `backend/ai/conversation/telegram_context.py` | optional `is_ai_message` / `exclude_ai_ids` input to `build_chat_context` and `fetch_telegram_chat_context`; one filter in the existing exclusion loop (`:262-270`) |
| `backend/bot/handlers/ai_unified.py` | supply `get_resolver().resolve` at the single call site (`:539`) — the one Telegram read is preserved |

This removes case B (previous in-place AI answers) deterministically and leaves
keep-cases A and E untouched.

**Phase 2 — only if U-K1 shows case C in the live chat.** Extend the existing
registry rather than adding one: capture the `Message` objects that
`delivery.deliver_response` currently discards (`delivery.py:673`, `:679`) and
register their ids through the same `ReplyResolver`. Reuses the existing map;
no new store, no schema.

**Explicitly not required:** any database column/table or migration, a second
history system, a message database, new Telegram polling, prompt-format changes,
or changes to delivery, providers, scheduler, ToolExecutor, Taskloom, or the AI
runtime history.

### K14. Recommendation for the next stage

Proceed with **Phase 1 only** as Stage L: implement the bounded
`ReplyResolver`-based exclusion for the surrounding window, with focused tests
using a fake client and a fake resolver proving that (a) a previously AI-edited
message id is excluded from the window, (b) a genuine owner message is kept and
still renders as `You`, (c) another participant's message is kept, (d) an empty
resolver leaves current behavior byte-identical, and (e) the single-read and
all existing bounds (10 / 200 / 1500 / 4 / 3.0 s) are unchanged. Do **not**
introduce a new provenance store or text-based heuristic. Then observe the live
chat once more to resolve U-K1 and decide whether Phase 2 (registering new
self-bot message ids) is needed.

### K15. Stage K verdict

| Property | Verdict | Decisive evidence |
|---|---|---|
| Can the system tell other participants from the owner? | **CONFIRMED SAFE** | `out`/`sender_id` read and rendered (K10) |
| Can the system positively identify an in-place AI answer (case B)? | **CONFIRMED SAFE** (in-process) | `ReplyResolver` register/resolve, already used for the reply target (K10) |
| Can the system identify a NEW self-bot message (case C)? | **CONFIRMED GAP** | no marker recorded; `event.reply(...)` results discarded (K11, G-K2) |
| Does the surrounding window apply any provenance filter today? | **CONFIRMED GAP** | `_to_record` / `build_chat_context` read no provenance (K11, G-K1) |
| Does provenance survive a restart? | **CONFIRMED GAP** | RAM-only, LRU 500 (K11, G-K3) |
| Is text matching (`│`, `Reading messages...`, ...) acceptable? | **UNSAFE** | not authoritative; presentation is renderer-owned (K9) |
| Is dropping all `sender_id == owner_id` acceptable? | **UNSAFE** | removes cases A and B together; explicitly forbidden (K9) |
| Which case caused the live symptom? | **UNCERTAIN** | needs the live chat (U-K1) |

No fix was implemented. Only this document was modified.
