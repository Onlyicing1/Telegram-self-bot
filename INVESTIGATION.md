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
