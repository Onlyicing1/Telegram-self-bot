"""Bounded data contracts for future AI-backed Taskloom preparation."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from typing import Any

MAX_ACTIONS = 5
MAX_PAYLOAD_BYTES = 32768

MAX_AI_INSTRUCTION_CHARS = 4096
MAX_PREPARATION_METADATA_BYTES = 8192
MAX_TOOL_NAME_CHARS = 128

#: ``ToolContext.extra`` key marking a context that belongs to a CLAIMED
#: SCHEDULED occurrence. It is written ONLY by ``TaskExecutionCoordinator``
#: from trusted runtime state — never by a model, a candidate field, or a task
#: argument — so it is the one trustworthy signal that a tool call originates
#: from a scheduled occurrence rather than from an interactive owner request.
SCHEDULED_OCCURRENCE_EXTRA = "scheduled_occurrence"

#: Registered actions whose arguments address a literal Telegram message.
#: Telegram message IDs are only meaningful together with their chat, so the
#: value must be grounded in trusted context — a numeric shape alone proves
#: nothing (that is why this is a provenance rule, not integer validation).
MESSAGE_ID_ACTION_ARGUMENTS: dict[str, tuple[str, ...]] = {
    "delete_message_by_id": ("message_id",),
    "delete_by_id": ("message_id",),
    "delete_messages_by_ids": ("message_ids",),
}

#: Registered action whose single argument is a Telegram message LINK. The
#: link encodes its own chat + message id, so it too must come from the owner.
MESSAGE_LINK_ACTION = "save_by_link"
MESSAGE_LINK_ARGUMENT = "link"

# Telegram message IDs are far below this many digits; longer digit runs in a
# request are not message references and must not widen the trusted set.
_MAX_MESSAGE_ID_DIGITS = 18


class TaskContractError(ValueError):
    """A task AI contract is malformed or exceeds its safety bounds."""


def _generation_authorized(request: str) -> tuple[bool, bool]:
    """``(authorized, policy_active)`` for one human request.

    Generation is authorized when the request derives a deterministic content
    policy (a named source/person/character, a length bound, a language) or
    asks to CHANGE the owner's profile content — the established
    per-occurrence profile-generation contract. Anything else leaves the task
    static. Reuses the existing intent vocabulary; no new phrase list.
    """
    from backend.ai.actions import (
        _USERNAME_WORDS,
        _has_bio_change_intent,
        _has_bio_mention,
        _tokenize,
        _write_text_present,
    )
    from backend.ai.preparation_policy import derive_policy

    if derive_policy(request).active:
        return True, True
    words = _tokenize(request)
    if not words or not (_has_bio_change_intent(words) or _write_text_present(words)):
        return False, False
    return (_has_bio_mention(words) or any(word in _USERNAME_WORDS for word in words)), False


def ground_ai_instruction(candidate: dict[str, Any], request: str) -> str:
    """Ground a candidate's ``ai_instruction`` in the ORIGINAL user request.

    Generated content is authorized ONLY by the request. When it is
    authorized the instruction becomes the request VERBATIM — a provider can
    neither weaken, paraphrase, translate, nor drop it; when it is not
    authorized, a provider-supplied instruction is not authorization at all
    and is dropped, so the task stays static instead of turning a static
    request into generated content.

    Idempotent: the same rule is applied at the provider-output boundary and
    again at the creation boundary, so applying it twice changes nothing.
    Returns the applied reason for the caller's trace ("" when nothing
    changed).
    """
    if not isinstance(candidate, dict) or not isinstance(request, str) or not request.strip():
        return ""
    supplied = candidate.get("ai_instruction")
    authorized, policy_active = _generation_authorized(request)
    if policy_active:
        if supplied == request:
            return ""
        candidate["ai_instruction"] = request
        return "model_repaired" if supplied else "omitted"
    if not supplied:
        return ""
    if authorized:
        if supplied == request:
            return ""
        candidate["ai_instruction"] = request
        return "grounded_to_request"
    del candidate["ai_instruction"]
    return "ungrounded_dropped"


def scheduled_creation_error(is_scheduled: bool) -> str | None:
    """Refuse durable task creation that originates from a scheduled task.

    A claimed occurrence already carries a trusted context marker; durable
    task creation from that context is exactly the recursive path (the created
    child is itself scheduled and can create again). Refusing it is
    deterministic, needs no provider round, and leaves every owner-initiated
    creation path untouched.
    """
    if not is_scheduled:
        return None
    return (
        "a scheduled task cannot create other tasks; "
        "consult the owner directly instead"
    )


def _message_reference(value: Any) -> int | None:
    """The message ID a value would mean at execution, or ``None``.

    Uses the SAME coercion the executing tools use, so a digit string (ASCII,
    Persian, or Arabic-Indic) cannot slip past a numeric-shape check.
    """
    from backend.ai.persian import coerce_int

    number = coerce_int(value)
    if number is None or number <= 0:
        return None
    return number


def trusted_message_ids(extra: Any) -> set[int]:
    """Message identities the CREATING context can vouch for.

    Only trusted runtime context counts: the owner's own triggering message
    (``request_message_id``) and the message the owner replied to
    (``reply_msg``). A scheduled occurrence carries neither, so a scheduled
    creation can never ground a message reference at all.
    """
    trusted: set[int] = set()
    if not isinstance(extra, dict):
        return trusted
    number = _message_reference(extra.get("request_message_id"))
    if number is not None:
        trusted.add(number)
    reply = extra.get("reply_msg")
    if isinstance(reply, dict):
        number = _message_reference(reply.get("message_id"))
        if number is not None:
            trusted.add(number)
    return trusted


def request_declared_message_ids(text: Any) -> set[int]:
    """Message numbers the OWNER wrote in the trusted request text.

    The owner's own message is trusted input: an explicitly typed number is
    user authorization, exactly as the deterministic command parser treats an
    explicit ID typed by the owner. Digit runs are normalized (Persian/Arabic
    included) and absurdly long runs are ignored.
    """
    if not isinstance(text, str) or not text.strip():
        return set()
    from backend.ai.persian import normalize_digits

    declared: set[int] = set()
    for token in re.findall(r"\d+", normalize_digits(text)):
        if len(token) > _MAX_MESSAGE_ID_DIGITS:
            continue
        number = _message_reference(token)
        if number is not None:
            declared.add(number)
    return declared


def _normalized_reference(text: Any) -> str:
    """Whitespace-collapsed, lower-cased text for link grounding."""
    if not isinstance(text, str):
        return ""
    return " ".join(text.split()).lower()


def message_reference_provenance_error(
    candidate: Any, *, extra: Any, request_text: Any
) -> str | None:
    """Fail closed when an action references an ungrounded Telegram message.

    Provenance comes from trusted context only: the owner's triggering message
    (``request_message_id``), the message they replied to (``reply_msg``), or a
    number the owner explicitly wrote in the request text. A provider-shaped
    number with no such grounding is refused before persistence, so an
    invented message ID can never become a durable executable target.
    """
    actions = candidate.get("actions") if isinstance(candidate, dict) else None
    if not isinstance(actions, list):
        return None
    trusted = trusted_message_ids(extra) | request_declared_message_ids(request_text)
    normalized_text = _normalized_reference(request_text)
    for action in actions:
        if not isinstance(action, dict):
            continue
        name = action.get("name")
        if not isinstance(name, str):
            continue
        arguments = action.get("arguments")
        if not isinstance(arguments, dict):
            arguments = {}
        for argument in MESSAGE_ID_ACTION_ARGUMENTS.get(name, ()):
            value = arguments.get(argument)
            for item in (value if isinstance(value, list) else [value]):
                message_id = _message_reference(item)
                if message_id is not None and message_id not in trusted:
                    return (
                        f"action '{name}' references Telegram message {message_id}, "
                        "which the request does not authorize"
                    )
        if name == MESSAGE_LINK_ACTION:
            link = arguments.get(MESSAGE_LINK_ARGUMENT)
            if isinstance(link, str) and link.strip():
                if not normalized_text or _normalized_reference(link) not in normalized_text:
                    return (
                        f"action '{name}' references a Telegram link "
                        "the request does not contain"
                    )
    return None


def validate_ai_instruction(value: Any) -> str:
    if not isinstance(value, str) or not value.strip():
        raise TaskContractError("AI instruction must be a nonblank string")
    instruction = value.strip()
    if len(instruction) > MAX_AI_INSTRUCTION_CHARS:
        raise TaskContractError("AI instruction exceeds its bounded size")
    return instruction


def validate_prepared_action(value: Any) -> dict[str, Any]:
    if not isinstance(value, dict) or set(value) != {"name", "arguments"}:
        raise TaskContractError("prepared action must contain only name and arguments")
    name = value["name"]
    arguments = value["arguments"]
    if not isinstance(name, str) or not name.strip() or len(name) > MAX_TOOL_NAME_CHARS:
        raise TaskContractError("prepared action tool name is invalid")
    if not isinstance(arguments, dict):
        raise TaskContractError("prepared action arguments must be an object")
    normalized = {"name": name.strip(), "arguments": dict(arguments)}
    if len(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_PAYLOAD_BYTES:
        raise TaskContractError("prepared action exceeds its bounded size")
    return normalized


@dataclass(frozen=True)
class AIInstruction:
    text: str
    version: int = 1

    def __post_init__(self) -> None:
        object.__setattr__(self, "text", validate_ai_instruction(self.text))
        if not isinstance(self.version, int) or self.version < 1:
            raise TaskContractError("AI instruction version must be positive")

    def as_dict(self) -> dict[str, Any]:
        return {"kind": "ai_instruction", "version": self.version, "text": self.text}


# ── Durable action chains ───────────────────────────────────────────────────
# A durable task's ordered actions may pass a bounded result forward: one
# argument value may be a REFERENCE to a declared output field of an EARLIER
# action in the same occurrence —
#
#     {"$ref": {"action": 1, "field": "save_code"}}
#
# The reference is DATA in the task definition, never model-resolved: it is
# validated at creation against the referenced tool's declared consumable
# output fields, re-proved before any execution, and resolved deterministically
# by the TaskExecutionCoordinator from the bounded per-action runs the
# occurrence already recorded. Everything unknown fails closed: a reference may
# only name an earlier position of THIS occurrence, can never reach another
# task's or another occurrence's result, and is never guessed or substituted.
REFERENCE_KEY = "$ref"
#: Occurrence-metadata key holding the bounded per-action run records.
ACTION_RUNS_KEY = "actions"
#: ``skipped`` is the ONE non-execution status the conditional-branch contract
#: produces: the action belongs to the branch the chain's condition did NOT
#: select, so it never ran and never will in this occurrence.
BRANCH_SKIPPED_STATUS = "skipped"
ACTION_RUN_STATUSES = frozenset(
    {"pending", "running", "succeeded", "failed", BRANCH_SKIPPED_STATUS}
)
MAX_REFERENCE_FIELD_CHARS = 64
MAX_ACTION_OUTPUT_FIELDS = 3
MAX_ACTION_OUTPUT_TEXT_CHARS = 128
MAX_ACTION_ERROR_CHARS = 256
#: The whole runs list must fit the occurrence's bounded metadata convention
#: with room for its counters; the per-run bounds below make that structural.
MAX_ACTION_RUNS_BYTES = 6144


def is_action_reference(value: Any) -> bool:
    """True when a value claims the reserved reference namespace."""
    return isinstance(value, dict) and REFERENCE_KEY in value


def _validated_target(target: Any, *, what: str) -> dict[str, Any]:
    """The ONE accepted ``{action, field}`` shape, shared by result references
    and condition sources; every deviation fails closed."""
    if not isinstance(target, dict) or set(target) != {"action", "field"}:
        raise TaskContractError(f"{what} must name exactly an action and a field")
    position = target["action"]
    if isinstance(position, bool) or not isinstance(position, int) or position < 1:
        raise TaskContractError(f"{what} action must be a positive action number")
    field = target["field"]
    if not isinstance(field, str) or not field.strip() or len(field) > MAX_REFERENCE_FIELD_CHARS:
        raise TaskContractError(f"{what} field must be a bounded nonblank name")
    return {"action": position, "field": field.strip()}


def validate_action_reference(value: Any) -> dict[str, Any]:
    """The ONE accepted reference shape, normalized. Fails closed."""
    if not isinstance(value, dict) or set(value) != {REFERENCE_KEY}:
        raise TaskContractError("a reference must contain only '$ref'")
    return {
        REFERENCE_KEY: _validated_target(value[REFERENCE_KEY], what="a reference")
    }


def _reserved_key_error(value: Any, argument: str) -> str | None:
    """Refuse any ``$``-prefixed key nested below an argument value.

    Only a WHOLE argument value may be a reference; a reserved key buried in
    a list/object is refused here, so no object traversal or expression
    language can ever grow out of the reference shape.
    """
    if isinstance(value, dict):
        for key, item in value.items():
            if str(key).startswith("$"):
                return (
                    f"argument '{argument}' nests a reserved '{key}' key; only a "
                    "whole argument value may be a reference"
                )
            error = _reserved_key_error(item, argument)
            if error:
                return error
    elif isinstance(value, list):
        for item in value:
            error = _reserved_key_error(item, argument)
            if error:
                return error
    return None


def _action_references(action: Any) -> tuple[dict[str, dict[str, Any]], str]:
    """``(references by argument, error)`` for one action."""
    if not isinstance(action, dict):
        return {}, "each action must be an object"
    arguments = action.get("arguments", action.get("parameters", {}))
    if not isinstance(arguments, dict):
        return {}, "action arguments must be objects"
    references: dict[str, dict[str, Any]] = {}
    for argument, value in arguments.items():
        if is_action_reference(value):
            try:
                references[str(argument)] = validate_action_reference(value)
            except TaskContractError as exc:
                return {}, f"argument '{argument}': {exc}"
            continue
        error = _reserved_key_error(value, str(argument))
        if error:
            return {}, error
    return references, ""


def _declared_fields(tool: Any) -> tuple[str, ...]:
    """The tool's declared consumable output fields (one shared definition)."""
    from backend.ai.tools.base import declared_consumable_output_fields

    return declared_consumable_output_fields(tool)


def action_reference_error(
    actions: Any, registry: Any | None, *, generation_authorized: bool = False
) -> str | None:
    """Reject an action list whose references could never resolve.

    Enforced at task creation AND re-proved before any execution: a reference
    to the action's own position or a later one, to a position that does not
    exist, to an unregistered tool, or to a field that tool does not declare
    as chainable is refused — as is a reference on a task whose arguments are
    generated per occurrence (the model would be free to drop it). A missing
    registry skips only the tool/field checks (the execution boundary still
    fails the occurrence closed); the position rule is always enforced.
    """
    if not isinstance(actions, list) or not actions:
        return None
    has_registry = registry is not None and hasattr(registry, "get")
    # The branch structure (if any) decides which results an action may EVER
    # consume: the two branches are mutually exclusive at execution, so a
    # cross-branch reference could never resolve in a selected occurrence.
    condition_position, branch_of, _structure_error = _branch_structure(actions)
    for position, action in enumerate(actions, start=1):
        references, error = _action_references(action)
        if error:
            return error
        if not references:
            continue
        if generation_authorized:
            return (
                f"action {position} carries a result reference, which cannot be "
                "combined with per-occurrence generated arguments"
            )
        for argument, reference in references.items():
            target = reference[REFERENCE_KEY]
            target_position = target["action"]
            if target_position >= position:
                return (
                    f"action {position} argument '{argument}' references action "
                    f"{target_position}, which is not an earlier action of the same task"
                )
            if condition_position is not None:
                if target_position == condition_position:
                    return (
                        f"action {position} argument '{argument}' references the "
                        "condition action, which declares no output"
                    )
                source_branch = branch_of.get(position)
                target_branch = branch_of.get(target_position)
                if source_branch and target_branch and source_branch != target_branch:
                    return (
                        f"action {position} argument '{argument}' references action "
                        f"{target_position} of the '{target_branch}' branch, whose "
                        "result the selected occurrence never produces"
                    )
            if not has_registry:
                continue
            target_action = actions[target_position - 1]
            target_name = target_action.get("name") if isinstance(target_action, dict) else None
            tool = registry.get(target_name) if isinstance(target_name, str) else None
            if tool is None:
                return (
                    f"action {position} argument '{argument}' references an "
                    "action whose tool is not registered"
                )
            if target["field"] not in _declared_fields(tool):
                return (
                    f"action {position} argument '{argument}' references field "
                    f"'{target['field']}', which action {target_position}'s tool "
                    "does not declare as chainable"
                )
    return None


def actions_need_resolution(calls: Any) -> bool:
    """True when at least one argument value is a reference to resolve."""
    if not isinstance(calls, list):
        return False
    for call in calls:
        if not isinstance(call, dict):
            continue
        arguments = call.get("arguments")
        if isinstance(arguments, dict) and any(is_action_reference(v) for v in arguments.values()):
            return True
    return False


def bounded_action_output(data: Any, fields: Any) -> dict[str, Any]:
    """The declared, chainable, bounded subset of one tool's ``ToolResult.data``.

    Exactly the declared fields, each must be a bounded scalar (a nonblank
    string within ``MAX_ACTION_OUTPUT_TEXT_CHARS``, a number, or a boolean);
    anything else — a missing key, a blank/oversized string, a nested object,
    a null — is omitted rather than coerced, so a reference to it fails the
    referencing action closed instead of executing with an invented value.
    The field/​size bounds make the whole record structurally fit its budget.
    """
    if not isinstance(data, dict) or not isinstance(fields, (list, tuple)):
        return {}
    output: dict[str, Any] = {}
    for field in list(fields)[:MAX_ACTION_OUTPUT_FIELDS]:
        name = str(field)
        value = data.get(name)
        if isinstance(value, bool):
            output[name] = value
        elif isinstance(value, int):
            output[name] = value
        elif isinstance(value, float):
            if value == value and value not in (float("inf"), float("-inf")):
                output[name] = value
        elif isinstance(value, str):
            if value.strip() and len(value) <= MAX_ACTION_OUTPUT_TEXT_CHARS:
                output[name] = value
    return output


def build_action_run(
    position: int,
    tool: str,
    status: str,
    *,
    output: dict[str, Any] | None = None,
    error: str = "",
) -> dict[str, Any]:
    """One bounded per-action run record, validated as it is built."""
    value: dict[str, Any] = {"position": position, "tool": tool, "status": status}
    if status == "succeeded":
        value["output"] = dict(output or {})
    elif status == "failed":
        value["error"] = " ".join(str(error or "").split())[:MAX_ACTION_ERROR_CHARS]
    return validate_action_run(value)


def validate_action_run(value: Any) -> dict[str, Any]:
    """Normalize ONE action run record; every deviation fails closed."""
    if not isinstance(value, dict):
        raise TaskContractError("an action run must be an object")
    allowed = {"position", "tool", "status", "output", "error"}
    if set(value) - allowed:
        raise TaskContractError("an action run carries unsupported fields")
    position = value.get("position")
    if isinstance(position, bool) or not isinstance(position, int) or not 1 <= position <= MAX_ACTIONS:
        raise TaskContractError("an action run needs a bounded 1-based position")
    tool = value.get("tool")
    if not isinstance(tool, str) or not tool.strip() or len(tool) > MAX_TOOL_NAME_CHARS:
        raise TaskContractError("an action run needs a bounded tool name")
    status = value.get("status")
    if status not in ACTION_RUN_STATUSES:
        raise TaskContractError("invalid action run status")
    output = value.get("output")
    error = value.get("error")
    if output is not None and status != "succeeded":
        raise TaskContractError("only a succeeded action carries an output")
    if error is not None and status != "failed":
        raise TaskContractError("only a failed action carries an error")
    normalized = {"position": position, "tool": tool.strip(), "status": status}
    if status == "succeeded":
        if not isinstance(output, dict) or len(output) > MAX_ACTION_OUTPUT_FIELDS:
            raise TaskContractError("an action output must be a bounded object")
        normalized["output"] = bounded_action_output(output, tuple(output))
    elif status == "failed":
        if not isinstance(error, str):
            raise TaskContractError("an action error must be text")
        normalized["error"] = " ".join(error.split())[:MAX_ACTION_ERROR_CHARS]
    if len(json.dumps(normalized, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_ACTION_RUNS_BYTES:
        raise TaskContractError("an action run exceeds its bounded size")
    return normalized


def validate_action_runs(value: Any) -> list[dict[str, Any]]:
    """Normalize the bounded per-action run records of ONE occurrence."""
    if value is None:
        return []
    if not isinstance(value, list) or len(value) > MAX_ACTIONS:
        raise TaskContractError("action runs must be a bounded list")
    runs = [validate_action_run(item) for item in value]
    positions = [run["position"] for run in runs]
    if len(set(positions)) != len(positions):
        raise TaskContractError("action run positions must be unique")
    if len(json.dumps(runs, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_ACTION_RUNS_BYTES:
        raise TaskContractError("action runs exceed their bounded size")
    return sorted(runs, key=lambda run: run["position"])


def action_runs_from_metadata(metadata: Any) -> tuple[list[dict[str, Any]], bool]:
    """``(runs, recorded)`` for an occurrence's metadata.

    ``recorded`` is False only when no run records were ever written (a fresh
    occurrence, or a row created before action chains existed). A malformed
    record raises, so the caller can fail closed instead of replaying side
    effects it cannot account for.
    """
    if not isinstance(metadata, dict):
        return [], False
    if ACTION_RUNS_KEY not in metadata:
        return [], False
    return validate_action_runs(metadata.get(ACTION_RUNS_KEY)), True


def resolve_action_arguments(
    arguments: Any, runs: Any
) -> tuple[dict[str, Any], str]:
    """Resolve a call's references from THIS occurrence's recorded runs.

    Returns ``(resolved_arguments, "")``, or ``({}, reason)`` when anything is
    unknown. Only a run of the same occurrence that is recorded ``succeeded``
    and whose bounded output carries the named field resolves; every other
    case fails closed with a deterministic reason — never a guess, never null.
    """
    if not isinstance(arguments, dict):
        return {}, "action_arguments_invalid"
    try:
        recorded = validate_action_runs(runs)
    except (TaskContractError, TypeError, ValueError):
        return {}, "action_record_invalid"
    by_position = {run["position"]: run for run in recorded}
    resolved: dict[str, Any] = {}
    for argument, value in arguments.items():
        if not is_action_reference(value):
            nested = _reserved_key_error(value, str(argument))
            if nested:
                return {}, f"invalid_reference ({nested})"
            resolved[argument] = value
            continue
        try:
            reference = validate_action_reference(value)
        except TaskContractError:
            return {}, f"invalid_reference (argument '{argument}')"
        target = reference[REFERENCE_KEY]
        run = by_position.get(target["action"])
        if run is None or run["status"] != "succeeded":
            return {}, (
                f"reference_target_not_succeeded (argument '{argument}', "
                f"action {target['action']})"
            )
        output = run.get("output")
        if not isinstance(output, dict) or target["field"] not in output:
            return {}, (
                f"reference_field_unavailable (argument '{argument}', "
                f"field '{target['field']}')"
            )
        resolved[argument] = output[target["field"]]
    return resolved, ""


# ── Bounded conditional branches ────────────────────────────────────────────
# ONE action of a chain may be a CONDITION. It declares no tool call: it
# compares ONE declared, bounded output field of an EARLIER action of the SAME
# occurrence against a bounded literal, and exactly ONE of two labelled runs of
# actions executes —
#
#     [search, {"condition": {…}}, save(branch true), tag(branch true), save(branch false)]
#
# The condition is DATA in the task definition, never model-resolved: it is
# validated at creation against the referenced tool's declared consumable
# output fields, re-proved verbatim before any execution, and evaluated
# deterministically by the TaskExecutionCoordinator from the bounded per-action
# runs the occurrence already recorded. Its RESULT is the condition's own run
# record (``{"matched": …, "selected_branch": "true"|"false"}``) — the same
# structured result a tool action uses, so there is no second result type — and
# that record is persisted BEFORE either branch runs, so a restart resumes the
# SAME branch: the condition is never re-evaluated and the other branch never
# starts. The language stays deliberately tiny: one comparison, two operators,
# bounded scalar values, at most ONE condition per chain (no nesting), no loops,
# no expressions, no traversal, no graph.
CONDITION_KEY = "condition"
BRANCH_KEY = "branch"
BRANCH_TRUE = "true"
BRANCH_FALSE = "false"
BRANCH_VALUES = (BRANCH_TRUE, BRANCH_FALSE)
#: The pseudo tool name a condition's run record carries. It is never a
#: registered tool and is never handed to the ToolExecutor.
CONDITION_TOOL = "condition"
#: The two bounded fields of a condition's durable result.
MATCHED_KEY = "matched"
BRANCH_SELECTION_KEY = "selected_branch"
#: Equality is what the current product needs (a flag, a report, or a query
#: outcome); nothing in the codebase requires ordering, substring or presence
#: operators today, so they are deliberately NOT implemented.
CONDITION_OPERATORS = frozenset({"equals", "not_equals"})
#: A condition value is a bounded scalar: text within the SAME bound a recorded
#: output field carries, a boolean, null, or a finite number within this
#: magnitude. No objects, no lists, no nesting, no expressions.
MAX_CONDITION_NUMBER = 10**15
_CONDITION_KEYS = frozenset({"source", "operator", "value"})


def branch_name(matched: bool) -> str:
    """The branch label a boolean condition outcome selects."""
    return BRANCH_TRUE if matched else BRANCH_FALSE


def validate_condition_value(value: Any) -> Any:
    """The ONE accepted condition value: a bounded scalar. Fails closed."""
    if value is None or isinstance(value, bool):
        return value
    if isinstance(value, int):
        if abs(value) > MAX_CONDITION_NUMBER:
            raise TaskContractError("a condition value number is out of range")
        return value
    if isinstance(value, float):
        if value != value or value in (float("inf"), float("-inf")) or abs(value) > MAX_CONDITION_NUMBER:
            raise TaskContractError("a condition value must be a finite bounded number")
        return value
    if isinstance(value, str):
        if not value.strip() or len(value) > MAX_ACTION_OUTPUT_TEXT_CHARS:
            raise TaskContractError(
                "a condition value text must be nonblank and within "
                f"{MAX_ACTION_OUTPUT_TEXT_CHARS} characters"
            )
        return value
    raise TaskContractError(
        "a condition value must be a bounded scalar: text, a boolean, a finite "
        "number, or null"
    )


def validate_condition(value: Any) -> dict[str, Any]:
    """The ONE accepted condition shape, normalized. Fails closed."""
    if not isinstance(value, dict) or set(value) != _CONDITION_KEYS:
        raise TaskContractError(
            "a condition must contain exactly a source, an operator and a value"
        )
    operator = value["operator"]
    if operator not in CONDITION_OPERATORS:
        raise TaskContractError(
            "a condition operator must be one of: "
            + ", ".join(sorted(CONDITION_OPERATORS))
        )
    return {
        "source": _validated_target(value["source"], what="a condition source"),
        "operator": operator,
        "value": validate_condition_value(value["value"]),
    }


def _branch_structure(actions: Any) -> tuple[int | None, dict[int, str], str]:
    """``(condition position, branch by position, error)`` — structure only.

    A chain is either conditional or not, never something in between: at most
    ONE action may be a condition, every action AFTER it must declare exactly one
    branch, no action before it may declare one, each branch must be ONE
    contiguous run, and the true run must come first. Anything else is refused
    here (fail closed) instead of being interpreted.
    """
    if not isinstance(actions, list) or not actions:
        return None, {}, ""
    positions = [
        position
        for position, action in enumerate(actions, start=1)
        if isinstance(action, dict) and CONDITION_KEY in action
    ]
    if not positions:
        for position, action in enumerate(actions, start=1):
            if isinstance(action, dict) and BRANCH_KEY in action:
                return None, {}, (
                    f"action {position} declares a branch, but the chain has no condition"
                )
        return None, {}, ""
    if len(positions) > 1:
        return None, {}, (
            "only one condition per chain is supported; nested conditions are "
            "not implemented"
        )
    condition_position = positions[0]
    branches: dict[int, str] = {}
    for position, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            return None, {}, "each action must be an object"
        if position == condition_position:
            if set(action) != {CONDITION_KEY}:
                return None, {}, (
                    f"action {position}: a condition action must contain only "
                    f"'{CONDITION_KEY}'"
                )
            continue
        label = action.get(BRANCH_KEY)
        if position < condition_position:
            if label is not None:
                return None, {}, (
                    f"action {position}: only actions after the condition may "
                    "declare a branch"
                )
            continue
        if label not in BRANCH_VALUES:
            return None, {}, (
                f"action {position}: every action after the condition must declare "
                f"'{BRANCH_KEY}' as '{BRANCH_TRUE}' or '{BRANCH_FALSE}'"
            )
        branches[position] = label
    trues = tuple(sorted(p for p, b in branches.items() if b == BRANCH_TRUE))
    falses = tuple(sorted(p for p, b in branches.items() if b == BRANCH_FALSE))
    if not trues or not falses:
        return None, {}, "a condition needs at least one action in each branch"
    if trues != tuple(range(trues[0], trues[-1] + 1)):
        return None, {}, "the true branch must be one contiguous run of actions"
    if falses != tuple(range(falses[0], falses[-1] + 1)):
        return None, {}, "the false branch must be one contiguous run of actions"
    if trues[-1] >= falses[0]:
        return None, {}, "the true branch must come before the false branch"
    return condition_position, branches, ""


@dataclass(frozen=True)
class BranchLayout:
    """The bounded conditional structure of ONE action list.

    ``branches`` maps every position AFTER the condition to the branch it
    belongs to, and ``condition`` is the normalized condition payload. The whole
    structure is proven over the list before anything runs, so no later stage
    ever has to interpret a partial or ambiguous chain.
    """
    condition_position: int
    condition: dict[str, Any]
    branches: dict[int, str]

    @property
    def true_positions(self) -> tuple[int, ...]:
        return tuple(sorted(p for p, b in self.branches.items() if b == BRANCH_TRUE))

    @property
    def false_positions(self) -> tuple[int, ...]:
        return tuple(sorted(p for p, b in self.branches.items() if b == BRANCH_FALSE))


def resolve_conditions(
    actions: Any, registry: Any | None = None, *, generation_authorized: bool = False
) -> tuple[BranchLayout | None, str]:
    """``(layout, error)`` — the ONE conditional contract of an action list.

    Enforced at task creation AND re-proved verbatim before any execution. A
    condition may only read an EARLIER action of the same occurrence — which, by
    construction, precedes both branches — and only a field that action's tool
    DECLARES as chainable; a missing registry skips only those tool/field checks
    (the execution boundary still fails the occurrence closed). A condition is
    also refused together with per-occurrence generated arguments, exactly like a
    result reference: the model would be free to reshape the chain around it.
    """
    condition_position, branches, error = _branch_structure(actions)
    if error:
        return None, error
    if condition_position is None:
        return None, ""
    if generation_authorized:
        return None, (
            "the chain carries a condition, which cannot be combined with "
            "per-occurrence generated arguments"
        )
    action = actions[condition_position - 1]
    try:
        condition = validate_condition(action.get(CONDITION_KEY))
    except TaskContractError as exc:
        return None, f"action {condition_position}: {exc}"
    target = condition["source"]
    if target["action"] >= condition_position:
        return None, (
            f"action {condition_position}: the condition reads action "
            f"{target['action']}, which is not an earlier action of the same task"
        )
    if registry is not None and hasattr(registry, "get"):
        source_action = actions[target["action"] - 1]
        source_name = source_action.get("name") if isinstance(source_action, dict) else None
        tool = registry.get(source_name) if isinstance(source_name, str) else None
        if tool is None:
            return None, (
                f"action {condition_position}: the condition reads an action whose "
                "tool is not registered"
            )
        if target["field"] not in _declared_fields(tool):
            return None, (
                f"action {condition_position}: the condition reads field "
                f"'{target['field']}', which action {target['action']}'s tool does "
                "not declare as chainable"
            )
    return (
        BranchLayout(
            condition_position=condition_position,
            condition=condition,
            branches=dict(branches),
        ),
        "",
    )


def _condition_values_match(recorded: Any, expected: Any) -> bool:
    """Strict, type-preserving comparison — never a coercion.

    Python's ``True == 1`` shortcut is exactly the guessing this contract
    refuses: a boolean only ever matches a boolean, a number only ever a number,
    and text only ever text. No digit normalization, no case folding, no
    substring matching, no traversal. A ``null`` value only ever matches a
    recorded ``null`` (and a recorded output never carries one — see
    ``bounded_action_output``), so a null comparison is decided, never guessed.
    """
    if isinstance(expected, bool) or expected is None:
        return recorded is expected
    if isinstance(expected, str):
        return isinstance(recorded, str) and recorded == expected
    if isinstance(expected, (int, float)):
        return (
            not isinstance(recorded, bool)
            and isinstance(recorded, (int, float))
            and recorded == expected
        )
    return False


def evaluate_condition(condition: Any, runs: Any) -> tuple[bool | None, str]:
    """``(matched, reason)`` for ONE condition over THIS occurrence's runs.

    Resolution mirrors result references exactly: only a run of the same
    occurrence that is recorded ``succeeded`` and whose bounded output carries
    the named field resolves. Everything else — a malformed condition, an
    unrecorded source action, a source that did not succeed, a field the source
    never produced — fails closed with a deterministic reason, never a guess and
    never a silent ``false``: an invalid condition is an execution failure and
    neither branch may run.
    """
    try:
        normalized = validate_condition(condition)
    except (TaskContractError, TypeError, ValueError):
        return None, "invalid_condition"
    try:
        recorded = validate_action_runs(runs)
    except (TaskContractError, TypeError, ValueError):
        return None, "action_record_invalid"
    by_position = {run["position"]: run for run in recorded}
    target = normalized["source"]
    run = by_position.get(target["action"])
    if run is None or run["status"] != "succeeded":
        return None, f"condition_source_not_succeeded (action {target['action']})"
    output = run.get("output")
    if not isinstance(output, dict) or target["field"] not in output:
        return None, (
            "condition_field_unavailable (action "
            f"{target['action']}, field '{target['field']}')"
        )
    matched = _condition_values_match(output[target["field"]], normalized["value"])
    return (matched if normalized["operator"] == "equals" else not matched), ""


def condition_run_output(matched: bool) -> dict[str, Any]:
    """The bounded durable result of ONE condition evaluation.

    Exactly the structured shape a chain action's result uses — the occurrence's
    existing per-action record — so no second result type exists: ``matched``
    plus the ``selected_branch`` it selects.
    """
    selected = bool(matched)
    return {MATCHED_KEY: selected, BRANCH_SELECTION_KEY: branch_name(selected)}


def selected_branch_from_runs(runs: Any, position: int) -> tuple[str | None, str]:
    """``(branch, reason)`` — the branch the occurrence's condition SELECTED.

    Read back from the occurrence's own bounded runs, never re-derived by
    evaluating the condition again: the recorded selection is durable state, so
    a restart during either branch resumes the same one and can never switch.
    """
    try:
        recorded = validate_action_runs(runs)
    except (TaskContractError, TypeError, ValueError):
        return None, "action_record_invalid"
    run = {item["position"]: item for item in recorded}.get(position)
    if run is None or run["status"] != "succeeded":
        return None, "branch_selection_unavailable (the condition was not evaluated)"
    output = run.get("output")
    branch = output.get(BRANCH_SELECTION_KEY) if isinstance(output, dict) else None
    if branch not in BRANCH_VALUES:
        return None, (
            "branch_selection_unavailable (the recorded condition result carries "
            "no selected branch)"
        )
    return branch, ""


# ── Durable question / answer continuation ──────────────────────────────────
# ONE action of a chain may be a QUESTION: a REGISTERED tool call (the same
# ToolRegistry → ToolExecutor → TelegramAPI boundary every scheduled action
# uses) whose result is produced LATER, by the owner's correlated reply. When
# the chain reaches it, the question is sent through the tool, the occurrence
# parks durably on the ``waiting_answer`` status (the ONE non-terminal,
# non-retry state the scheduler, the wake loop, recovery and the claim CAS
# already keep their hands off), and the SAME occurrence resumes exactly where
# it stopped when the answer arrives. The question is DATA in the task
# definition — bounded text, never an executable instruction — and the ANSWER
# enters the chain through the EXISTING Phase 3A reference mechanism: the
# question action's run record carries the single bounded field ``answer``, so
# a later action consumes it with {"$ref": {"action": N, "field": "answer"}}
# and a condition reads it with its existing source/operator/value contract.
# No second reference language, no second result type, no conversation.
QUESTION_TOOL = "ask_owner"
#: The single field of the question action's durable result.
ANSWER_FIELD = "answer"
#: Occurrence-metadata key holding the bounded pending-question record.
PENDING_QUESTION_KEY = "pending_question"
#: Identity of the exact Telegram message that carries the question.
QUESTION_MESSAGE_ID_KEY = "question_message_id"
QUESTION_CHAT_ID_KEY = "question_chat_id"
QUESTION_TEXT_KEY = "question_text"
QUESTION_ASKED_AT_KEY = "asked_at"
QUESTION_ANSWERED_KEY = "answered"
QUESTION_ANSWER_AT_KEY = "answered_at"
#: Statuses an occurrence may park on while a question is open (the smallest
#: state extension compatible with 3A/3B: the question action's own run record
#: stays ``pending`` — QUESTION WAITING is never QUESTION SUCCESS).
WAITING_ANSWER_STATUS = "waiting_answer"
MAX_QUESTION_CHARS = 512
MAX_ANSWER_CHARS = 128
MAX_PENDING_QUESTION_BYTES = 1024
_QUESTION_META_KEYS = frozenset({
    "action", QUESTION_CHAT_ID_KEY, QUESTION_MESSAGE_ID_KEY, QUESTION_TEXT_KEY,
    QUESTION_ASKED_AT_KEY, QUESTION_ANSWERED_KEY, QUESTION_ANSWER_AT_KEY,
})


def validate_question_text(value: Any) -> str:
    """The ONE accepted question payload: bounded, nonblank, ordinary text.

    A question is displayable content for the owner, never an instruction
    carrier: no object, no list, no HTML/Telegram parse mode beyond plain
    text, and within the bounded size. Everything else fails closed.
    """
    if not isinstance(value, str):
        raise TaskContractError("a question must be plain text")
    text = " ".join(value.split())
    if not text:
        raise TaskContractError("a question must be nonblank")
    if len(text) > MAX_QUESTION_CHARS:
        raise TaskContractError(
            f"a question must be at most {MAX_QUESTION_CHARS} characters"
        )
    return text


def normalize_answer(value: Any) -> tuple[str | None, str]:
    """``(answer, error)`` — the ONE accepted correlated answer.

    Ordinary text only, whitespace-collapsed and bounded; media, captions and
    everything Telegram does not deliver as plain text are refused with an
    honest reason (never silently consumed). A bounded blank answer is a
    refusal, not an empty resume.
    """
    if not isinstance(value, str):
        return None, "answer_not_text"
    text = " ".join(value.split())
    if not text:
        return None, "answer_blank"
    if len(text) > MAX_ANSWER_CHARS:
        return None, "answer_too_long"
    return text, ""


def build_pending_question(
    *,
    action: int,
    chat_id: Any,
    message_id: Any,
    question_text: Any,
    asked_at: Any,
) -> dict[str, Any]:
    """The bounded durable record of ONE open question, validated as built.

    Everything the resume needs and nothing else: which action of THIS
    occurrence asked, the exact Telegram identity of the question message
    (chat + message id — the explicit correlation handle), the bounded text
    the owner saw, and when it was asked. ``answered`` is False until a
    correlated answer is durably consumed.
    """
    if isinstance(action, bool) or not isinstance(action, int) or not 1 <= action <= MAX_ACTIONS:
        raise TaskContractError("a pending question needs the asking action's position")
    chat = chat_id if isinstance(chat_id, int) and chat_id != 0 else None
    message = message_id if isinstance(message_id, int) and message_id > 0 else None
    if chat is None or message is None:
        raise TaskContractError(
            "a pending question needs the question message's chat and message id"
        )
    text = validate_question_text(question_text)
    stamp = " ".join(str(asked_at or "").split())
    if not stamp:
        raise TaskContractError("a pending question needs its asked-at instant")
    value = {
        "action": action,
        QUESTION_CHAT_ID_KEY: chat,
        QUESTION_MESSAGE_ID_KEY: message,
        QUESTION_TEXT_KEY: text,
        QUESTION_ASKED_AT_KEY: stamp,
        QUESTION_ANSWERED_KEY: False,
    }
    if len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_PENDING_QUESTION_BYTES:
        raise TaskContractError("a pending question record exceeds its bounded size")
    return value


def validate_pending_question(value: Any) -> dict[str, Any]:
    """Normalize ONE stored pending-question record; every deviation fails closed.

    The consumed state rides the same record: once the resume CAS has durably
    consumed a reply, the stored record carries ``answered`` plus its
    ``answered_at`` instant. Both are preserved verbatim — never re-derived,
    never guessed — and an answered record without its stamp fails closed.
    """
    if not isinstance(value, dict):
        raise TaskContractError("a pending question must be an object")
    extra = set(value) - _QUESTION_META_KEYS
    if extra:
        raise TaskContractError("a pending question carries unsupported fields")
    if not value:
        raise TaskContractError("a pending question must not be empty")
    try:
        built = build_pending_question(
            action=value.get("action"),
            chat_id=value.get(QUESTION_CHAT_ID_KEY),
            message_id=value.get(QUESTION_MESSAGE_ID_KEY),
            question_text=value.get(QUESTION_TEXT_KEY),
            asked_at=value.get(QUESTION_ASKED_AT_KEY),
        )
    except TaskContractError as exc:
        raise TaskContractError(f"invalid pending question: {exc}") from exc
    answered = value.get(QUESTION_ANSWERED_KEY, False)
    if not isinstance(answered, bool):
        raise TaskContractError("invalid pending question: answered must be a boolean")
    if answered:
        stamp = value.get(QUESTION_ANSWER_AT_KEY)
        if not isinstance(stamp, str) or not stamp.strip():
            raise TaskContractError(
                "invalid pending question: an answered question carries its "
                "answered-at instant"
            )
        built[QUESTION_ANSWERED_KEY] = True
        built[QUESTION_ANSWER_AT_KEY] = " ".join(stamp.split())
    return built


def pending_question_from_metadata(metadata: Any) -> dict[str, Any] | None:
    """``(record | None)`` — the ONE open question of ONE occurrence, or None.

    Read from the occurrence's own bounded metadata channels only. A
    malformed record raises so the caller fails closed instead of resuming a
    workflow it cannot account for.
    """
    if not isinstance(metadata, dict):
        return None
    for channel in (PENDING_QUESTION_KEY,):
        record = metadata.get(channel)
        if record is None:
            continue
        if isinstance(record, dict) and not record:
            continue
        return validate_pending_question(record)
    return None


def question_chain_error(actions: Any) -> str | None:
    """``(error | None)`` — the ONE bounded question contract of an action list.

    Enforced at task creation AND re-proved verbatim before any execution: at
    most ONE question per chain, the question's registered tool call carries
    ONLY its bounded ``question`` argument (never a destination, a reference
    is still allowed only through the shared 3A reference contract), no wait
    boundary may sit BEFORE the question (a question parks on its own answer,
    not on a clock), and — after the 3C branch gate has accepted the chain —
    no question may live inside either branch (a question needs the OWNER's
    answer, and the chain must not depend on an answer for a branch the
    condition could then re-decide around). Anything else fails closed.
    """
    if not isinstance(actions, list) or not actions:
        return None
    positions = [
        position
        for position, action in enumerate(actions, start=1)
        if isinstance(action, dict) and action.get("name") == QUESTION_TOOL
    ]
    if not positions:
        return None
    if len(positions) > 1:
        return "only one question per chain is supported"
    question_position = positions[0]
    condition_position, branches, structure_error = _branch_structure(actions)
    if structure_error:
        return None  # the condition contract reports its own failure verbatim
    if branches.get(question_position) is not None:
        return (
            f"action {question_position}: a question cannot live inside a "
            "conditional branch"
        )
    if condition_position is not None:
        condition = actions[condition_position - 1]
        source = condition.get(CONDITION_KEY)
        target = source.get("source") if isinstance(source, dict) else None
        condition_reads_answer = (
            isinstance(target, dict) and target.get("action") == question_position
        )
        if not condition_reads_answer:
            # The condition does NOT consume the answer, so a conditional
            # structure around the question is the one thing that could
            # re-decide around an answer the owner may never give.
            for position, branch in branches.items():
                if position < question_position:
                    continue
                if branch == BRANCH_TRUE:
                    return (
                        f"action {position}: a branch after a question must wait for "
                        "the answer outside the conditional structure"
                    )
    for position in range(1, question_position):
        earlier = actions[position - 1]
        if isinstance(earlier, dict) and earlier.get(WAIT_KEY) is not None:
            return (
                f"action {position}: a wait boundary may not sit before the "
                f"question at action {question_position} — a question parks on "
                "its own answer, not on a clock"
            )
    action = actions[question_position - 1]
    arguments = action.get("arguments")
    if not isinstance(arguments, dict) or set(arguments) != {"question"}:
        return (
            f"action {question_position}: '{QUESTION_TOOL}' takes exactly one "
            "bounded 'question' argument"
        )
    try:
        validate_question_text(arguments.get("question"))
    except TaskContractError as exc:
        return f"action {question_position}: {exc}"
    return None


def answer_run_output(answer: str) -> dict[str, Any]:
    """The bounded durable result of ONE answered question.

    Exactly the structured shape a chain action's result uses — the single
    declared field ``answer`` — so no second result type exists and the
    existing reference/condition mechanisms read it unchanged.
    """
    return {ANSWER_FIELD: answer}


def pending_question_correlation_error(
    record: Any, *, owner_id: Any, chat_id: Any, reply_to_message_id: Any
) -> str | None:
    """``(error | None)`` — the EXPLICIT correlation proof for one reply.

    A reply may resume a question only when EVERY identity agrees: the same
    owner (the answerer is the task owner), the same chat the question was
    sent to, and the reply's ``reply_to_msg_id`` naming EXACTLY the stored
    question message. Anything else — no reply target, a reply to another
    message, another chat — is not an answer and fails closed without
    touching the pending workflow.
    """
    try:
        question = validate_pending_question(record)
    except (TaskContractError, TypeError, ValueError):
        return "pending_question_invalid"
    if not isinstance(owner_id, int) or owner_id <= 0:
        return "answer_owner_unverified"
    if not isinstance(chat_id, int) or chat_id != question[QUESTION_CHAT_ID_KEY]:
        return "answer_chat_mismatch"
    if (
        isinstance(reply_to_message_id, bool)
        or not isinstance(reply_to_message_id, int)
        or reply_to_message_id != question[QUESTION_MESSAGE_ID_KEY]
    ):
        return "answer_not_correlated"
    return None


# ── Durable wait boundaries ─────────────────────────────────────────────────
# One action may carry ONE optional reserved field, ``not_before``: an ISO-8601
# timestamp naming the earliest instant at which THAT action may run. It is
# data in the task definition, never model-resolved at execution: validated at
# creation against the task's own timezone (and normalized to an absolute UTC
# instant there, so the stored definition no longer depends on the timezone),
# then re-proved before any execution. A wait is ELIGIBILITY, not a new action
# state: the waiting action stays ``pending`` and is never marked succeeded by
# the wait itself. The chain parks on the occurrence's existing durable
# eligibility instant (``retry_pending`` + ``retry_at`` — the one non-terminal
# state the schema constraint, the claim CAS, the recovery exemption and the
# wake loop already agree on) and resumes through the Phase 3A skip rule.
WAIT_KEY = "not_before"
#: An ISO-8601 date+time with an optional offset fits far inside this.
MAX_WAIT_CHARS = 64
#: No boundary may name an instant further ahead than this (the interval
#: schedule's 10-year cap, same order of magnitude). A PAST boundary is valid
#: and immediately eligible — the requested instant is never shifted.
MAX_WAIT_AHEAD_SECONDS = 366 * 24 * 3600 * 10
_WAIT_ISO_RE = re.compile(
    r"^\d{4}-\d{2}-\d{2}[T ]\d{2}:\d{2}(?::\d{2}(?:\.\d{1,6})?)?(?:Z|[+-]\d{2}:\d{2})?$"
)


def _wait_zone(name: Any):
    """The task's IANA timezone, or a contract error (never a guess)."""
    from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

    if not isinstance(name, str) or not name.strip():
        raise TaskContractError(
            "a wait boundary needs the task timezone to resolve a local instant"
        )
    text = name.strip()
    if text == "UTC":
        return timezone.utc
    try:
        return ZoneInfo(text)
    except (ZoneInfoNotFoundError, ValueError, TypeError) as exc:
        raise TaskContractError(
            "a wait boundary cannot resolve an invalid task timezone"
        ) from exc


def resolve_wait_boundary(value: Any, *, timezone_name: Any) -> datetime:
    """The ONE accepted wait shape, resolved to an aware UTC instant.

    A full ISO-8601 date+time is required. A naive value is the task's LOCAL
    wall-clock time — exactly the convention the ``once``/``daily``/``weekly``
    schedules already use — and an offset/``Z`` value is absolute. Everything
    else fails closed here: a date without a time, a clock without a date, a
    non-string, an unparseable or oversized value, or an unresolvable task
    timezone, so no surface ever guesses an instant.
    """
    if not isinstance(value, str) or not value.strip() or len(value) > MAX_WAIT_CHARS:
        raise TaskContractError("a wait boundary must be a bounded ISO-8601 timestamp string")
    text = value.strip()
    if not _WAIT_ISO_RE.match(text):
        raise TaskContractError(
            "a wait boundary must be an ISO-8601 date and time "
            "(e.g. 2026-09-26T18:00:00)"
        )
    try:
        parsed = datetime.fromisoformat(text.replace(" ", "T").replace("Z", "+00:00"))
    except ValueError as exc:
        raise TaskContractError("a wait boundary is not a valid timestamp") from exc
    if parsed.tzinfo is None or parsed.utcoffset() is None:
        return parsed.replace(tzinfo=_wait_zone(timezone_name)).astimezone(timezone.utc)
    return parsed.astimezone(timezone.utc)


def resolve_action_waits(
    actions: Any, *, timezone_name: Any, reference: Any
) -> tuple[list[datetime | None], list[dict[str, Any]] | None, str]:
    """``(boundaries, normalized actions, error)`` for one action chain.

    Enforced at task creation — where the returned actions carry each
    boundary as an absolute UTC ISO string, so persistence never stores an
    instant that depends on the task's timezone — and re-proved verbatim
    before any execution. Ordering is part of the contract: a boundary may
    not be EARLIER than a previous action's boundary (the chain would
    contradict its own order), and none may be more than
    ``MAX_WAIT_AHEAD_SECONDS`` ahead of the reference. A past boundary stays
    valid and immediately eligible. Anything else fails closed.
    """
    if not isinstance(actions, list) or not actions:
        return [], None, ""
    if not isinstance(reference, datetime) or reference.tzinfo is None:
        return [], None, "a wait boundary requires a timezone-aware reference instant"
    boundaries: list[datetime | None] = []
    normalized: list[dict[str, Any]] = []
    previous: datetime | None = None
    for position, action in enumerate(actions, start=1):
        if not isinstance(action, dict):
            return [], None, "each action must be an object"
        value = action.get(WAIT_KEY)
        if value is None:
            boundaries.append(None)
            normalized.append(dict(action))
            continue
        try:
            boundary = resolve_wait_boundary(value, timezone_name=timezone_name)
        except TaskContractError as exc:
            return [], None, f"action {position}: {exc}"
        if (boundary - reference).total_seconds() > MAX_WAIT_AHEAD_SECONDS:
            return [], None, f"action {position}: the wait boundary is unreasonably far ahead"
        if previous is not None and boundary < previous:
            return [], None, (
                f"action {position}: the wait boundary is earlier than a previous "
                "action's boundary"
            )
        previous = boundary
        normalized.append({**action, WAIT_KEY: boundary.isoformat()})
        boundaries.append(boundary)
    return boundaries, normalized, ""


@dataclass(frozen=True)
class PreparedAction:
    definition_version: int
    action: dict[str, Any]
    prepared_at: str

    def as_dict(self) -> dict[str, Any]:
        if not isinstance(self.prepared_at, str) or not self.prepared_at.strip():
            raise TaskContractError("prepared_at must be a nonblank string")
        if not isinstance(self.definition_version, int) or self.definition_version < 1:
            raise TaskContractError("definition version must be positive")
        action = validate_prepared_action(self.action)
        arguments = action["arguments"]
        value = {
            "kind": "prepared_action",
            "definition_version": self.definition_version,
            "prepared_at": self.prepared_at,
            "action": action,
        }
        if len(json.dumps(value, ensure_ascii=False, separators=(",", ":")).encode()) > MAX_PREPARATION_METADATA_BYTES:
            raise TaskContractError("prepared action metadata exceeds its bounded size")
        return value
