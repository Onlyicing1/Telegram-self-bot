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
