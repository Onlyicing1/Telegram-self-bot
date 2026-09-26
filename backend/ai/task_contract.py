"""Bounded data contracts for future AI-backed Taskloom preparation."""
from __future__ import annotations

import json
import re
from dataclasses import dataclass
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
ACTION_RUN_STATUSES = frozenset({"pending", "running", "succeeded", "failed"})
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


def validate_action_reference(value: Any) -> dict[str, Any]:
    """The ONE accepted reference shape, normalized. Fails closed."""
    if not isinstance(value, dict) or set(value) != {REFERENCE_KEY}:
        raise TaskContractError("a reference must contain only '$ref'")
    target = value[REFERENCE_KEY]
    if not isinstance(target, dict) or set(target) != {"action", "field"}:
        raise TaskContractError("a reference must name exactly an action and a field")
    position = target["action"]
    if isinstance(position, bool) or not isinstance(position, int) or position < 1:
        raise TaskContractError("a reference action must be a positive action number")
    field = target["field"]
    if not isinstance(field, str) or not field.strip() or len(field) > MAX_REFERENCE_FIELD_CHARS:
        raise TaskContractError("a reference field must be a bounded nonblank name")
    return {REFERENCE_KEY: {"action": position, "field": field.strip()}}


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
