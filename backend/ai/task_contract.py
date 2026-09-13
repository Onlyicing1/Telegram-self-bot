"""Bounded data contracts for future AI-backed Taskloom preparation."""
from __future__ import annotations

import json
from dataclasses import dataclass
from typing import Any

MAX_ACTIONS = 5
MAX_PAYLOAD_BYTES = 32768

MAX_AI_INSTRUCTION_CHARS = 4096
MAX_PREPARATION_METADATA_BYTES = 8192
MAX_TOOL_NAME_CHARS = 128


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
