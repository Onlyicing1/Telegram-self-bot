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
