"""Deterministic task triggers for event-driven automation.

A task may carry an optional ``trigger`` inside its ``schedule`` JSONB. Two
forms exist, validated by two functions:

- ``validate_trigger_spec`` — the MODEL-facing (unresolved) form. ``sender``
  and ``chat`` are semantic NAMES the AI may emit (e.g. "John", "this
  chat"); numeric Telegram ids are rejected outright because the model can
  never invent an identity.
- ``validate_resolved_trigger`` — the PERSISTED form after trusted runtime
  resolution (``resolve_trigger_references``) replaced names with real
  ``sender_id`` / ``chat_id`` integers from the authenticated Self Bot's
  dialogs.

``event_trigger_matches`` evaluates a resolved trigger against an incoming
Telegram event context dict — deterministic local logic, never an LLM call.
The runtime is the only consumer of this module; the AI only produces the
unresolved spec and never evaluates events.
"""
from __future__ import annotations

from typing import Any

TRIGGER_TYPES = frozenset({"telegram_message"})
_DIRECTIONS = frozenset({"incoming", "outgoing", "any"})

MAX_SENDER_NAME_CHARS = 128
MAX_CHAT_NAME_CHARS = 256
MAX_CONTAINS_TERMS = 10
MAX_CONTAINS_TERM_CHARS = 200
MAX_TEXT_EQUALS_CHARS = 4096
MAX_STARTS_WITH_CHARS = 256
MAX_TRIGGER_KEYS = 12

# Semantic aliases for "the chat this automation was created in". These
# resolve to the trusted request chat_id at creation time.
_THIS_CHAT_ALIASES = frozenset({
    "this chat", "current chat", "this conversation", "here",
    "همین چت", "این چت", "همین گفتگو", "این گفتگو", "اینجا",
})

_ALLOWED_UNRESOLVED_KEYS = frozenset({
    "type", "sender", "chat", "contains", "text_equals", "starts_with",
    "has_media", "is_reply", "direction",
})
_ALLOWED_RESOLVED_KEYS = _ALLOWED_UNRESOLVED_KEYS | frozenset({
    "sender_id", "sender_name", "chat_id", "chat_title",
})


class TaskTriggerError(ValueError):
    """A trigger spec is malformed, unbounded, ambiguous, or unsafe."""


def _require(value: Any, key: str, kind: type, max_chars: int = 0) -> Any:
    if not isinstance(value, kind):
        raise TaskTriggerError(f"trigger {key} must be {kind.__name__}")
    if kind is str:
        if not value.strip():
            raise TaskTriggerError(f"trigger {key} must not be blank")
        if len(value) > max_chars:
            raise TaskTriggerError(f"trigger {key} exceeds its bounded size")
        return value.strip()
    return value


def _direction(value: Any) -> str:
    if value is None:
        return "incoming"
    if not isinstance(value, str) or value.strip().lower() not in _DIRECTIONS:
        raise TaskTriggerError("trigger direction must be incoming, outgoing, or any")
    return value.strip().lower()


def _validate_common(spec: dict[str, Any], allowed: frozenset[str]) -> None:
    if set(spec) - allowed:
        raise TaskTriggerError(
            f"trigger has unsupported fields: {sorted(set(spec) - allowed)}"
        )
    if len(spec) > MAX_TRIGGER_KEYS:
        raise TaskTriggerError("trigger exceeds its bounded field count")
    trigger_type = spec.get("type")
    if trigger_type != "telegram_message":
        raise TaskTriggerError("trigger type must be telegram_message")


def _has_condition(spec: dict[str, Any]) -> bool:
    return any(
        spec.get(key) not in (None, False, [], "", 0)
        for key in (
            "sender", "chat", "sender_id", "chat_id", "contains",
            "text_equals", "starts_with", "has_media", "is_reply",
        )
    )


def validate_trigger_spec(value: Any) -> dict[str, Any]:
    """Validate the model-facing (unresolved) trigger spec.

    Accepts only the bounded structure below; numeric sender/chat ids are
    rejected because identities must come from trusted runtime resolution.
    Requires at least one matching condition so a trigger can never fire on
    every message by accident.
    """
    if not isinstance(value, dict):
        raise TaskTriggerError("trigger must be an object")
    _validate_common(value, _ALLOWED_UNRESOLVED_KEYS)
    normalized: dict[str, Any] = {"type": "telegram_message"}

    if "sender" in value:
        normalized["sender"] = _require(
            value["sender"], "sender", str, MAX_SENDER_NAME_CHARS
        )
    if "chat" in value:
        normalized["chat"] = _require(value["chat"], "chat", str, MAX_CHAT_NAME_CHARS)

    if "contains" in value:
        contains = value["contains"]
        if not isinstance(contains, list) or not 1 <= len(contains) <= MAX_CONTAINS_TERMS:
            raise TaskTriggerError("trigger contains must be a list of 1 to 10 terms")
        terms: list[str] = []
        for term in contains:
            if not isinstance(term, str) or not term.strip():
                raise TaskTriggerError("trigger contains terms must be nonblank strings")
            if len(term) > MAX_CONTAINS_TERM_CHARS:
                raise TaskTriggerError("trigger contains term exceeds its bounded size")
            terms.append(term.strip())
        normalized["contains"] = terms
    for key, max_chars in (("text_equals", MAX_TEXT_EQUALS_CHARS), ("starts_with", MAX_STARTS_WITH_CHARS)):
        if key in value:
            normalized[key] = _require(value[key], key, str, max_chars)
    for key in ("has_media", "is_reply"):
        if key in value:
            if not isinstance(value[key], bool):
                raise TaskTriggerError(f"trigger {key} must be a boolean")
            normalized[key] = value[key]

    normalized["direction"] = _direction(value.get("direction"))
    if not _has_condition(normalized):
        raise TaskTriggerError(
            "trigger needs at least one condition (sender, chat, or message content)"
        )
    return normalized


def validate_resolved_trigger(value: Any) -> dict[str, Any]:
    """Validate the persisted (resolved) trigger stored in a task's schedule.

    Same bounded structure as the unresolved form, plus optional integer
    ``sender_id`` / ``chat_id`` produced by trusted runtime resolution.
    """
    if not isinstance(value, dict):
        raise TaskTriggerError("trigger must be an object")
    _validate_common(value, _ALLOWED_RESOLVED_KEYS)
    normalized: dict[str, Any] = {"type": "telegram_message"}

    for key in ("sender_id", "chat_id"):
        if key in value:
            number = value[key]
            # Telegram ids: user/sender ids are positive, chat ids may be
            # negative for groups/supergroups — only 0 and non-integers are
            # invalid.
            if not isinstance(number, int) or isinstance(number, bool) or number == 0:
                raise TaskTriggerError(f"trigger {key} must be a nonzero integer")
            normalized[key] = number
    for key, max_chars in (
        ("sender_name", MAX_SENDER_NAME_CHARS),
        ("chat_title", MAX_CHAT_NAME_CHARS),
    ):
        if key in value:
            normalized[key] = _require(value[key], key, str, max_chars)
    unresolved = {key: val for key, val in value.items() if key in _ALLOWED_UNRESOLVED_KEYS}
    nested = validate_trigger_spec(unresolved)
    normalized.update(nested)
    return normalized


def is_this_chat_reference(value: str) -> bool:
    """True when a chat reference means the conversation the automation was
    created in (English/Persian aliases) — resolved to the trusted request
    chat_id at creation time."""
    return value.strip().casefold() in _THIS_CHAT_ALIASES


def resolve_trigger_references(
    spec: dict[str, Any],
    *,
    request_chat_id: int | None,
    chats: list[dict[str, Any]],
) -> tuple[dict[str, Any] | None, str | None]:
    """Resolve model-supplied sender/chat references to trusted ids.

    Returns ``(resolved_trigger, None)`` on success and ``(None, error)`` on
    failure — the caller fails closed with the human message. ``chats`` is
    the trusted dialog list (``id``, ``title``/``name``, ``username`` and,
    for users, ``first_name``/``last_name``). A resolved trigger never
    contains a model-invented id.
    """
    resolved = validate_trigger_spec(spec)
    chat_ref = resolved.pop("chat", None)
    if chat_ref is not None:
        if is_this_chat_reference(chat_ref):
            if not isinstance(request_chat_id, int) or request_chat_id == 0:
                return None, "The current chat could not be resolved; no task was created."
            resolved["chat_id"] = request_chat_id
        else:
            from backend.ai.chat_resolution import resolve_chat_name

            chat_result = resolve_chat_name(chat_ref, chats)
            if not chat_result.get("resolved"):
                from backend.ai.chat_resolution import format_clarification_options
                return None, (
                    "Could not resolve the message source chat for this trigger:\n"
                    + format_clarification_options(chat_result)
                )
            resolved["chat_id"] = chat_result["chat_id"]
            resolved["chat_title"] = chat_result.get("chat_title") or ""

    sender_ref = resolved.pop("sender", None)
    if sender_ref is not None:
        from backend.ai.chat_resolution import resolve_sender_name

        sender_result = resolve_sender_name(sender_ref, chats)
        if not sender_result.get("resolved"):
            from backend.ai.chat_resolution import format_clarification_options
            return None, (
                "Could not resolve the sender for this trigger:\n"
                + format_clarification_options(sender_result)
            )
        resolved["sender_id"] = sender_result["sender_id"]
        resolved["sender_name"] = sender_result.get("sender_name") or sender_ref

    return validate_resolved_trigger(resolved), None


def event_trigger_matches(trigger: dict[str, Any], event: dict[str, Any]) -> bool:
    """Deterministically evaluate a resolved trigger against a Telegram event.

    ``event`` keys: ``chat_id``, ``sender_id``, ``text``, ``has_media``,
    ``is_reply``, ``out``. Every condition is ANDed; a None/absent trigger
    field is not a constraint. No provider call, no content scoring.
    """
    direction = trigger.get("direction", "incoming")
    out = bool(event.get("out"))
    if direction == "incoming" and out:
        return False
    if direction == "outgoing" and not out:
        return False

    sender_id = trigger.get("sender_id")
    if sender_id is not None and event.get("sender_id") != sender_id:
        return False
    chat_id = trigger.get("chat_id")
    if chat_id is not None and event.get("chat_id") != chat_id:
        return False

    text = str(event.get("text") or "")
    contains = trigger.get("contains") or []
    if contains:
        lowered = text.casefold()
        if not all(str(term).casefold() in lowered for term in contains):
            return False
    text_equals = trigger.get("text_equals")
    if text_equals is not None and text != text_equals:
        return False
    starts_with = trigger.get("starts_with")
    if starts_with is not None and not text.startswith(starts_with):
        return False
    has_media = trigger.get("has_media")
    if has_media is not None and bool(event.get("has_media")) != has_media:
        return False
    is_reply = trigger.get("is_reply")
    if is_reply is not None and bool(event.get("is_reply")) != is_reply:
        return False
    return True


def trigger_summary(trigger: dict[str, Any]) -> str:
    """Compact bounded summary of a resolved trigger for user-facing display."""
    parts: list[str] = []
    sender = trigger.get("sender_name")
    if sender:
        parts.append(f"sender: {sender}")
    chat = trigger.get("chat_title")
    if chat:
        parts.append(f"chat: {chat}")
    for key, label in (
        ("contains", "contains"),
        ("text_equals", "text equals"),
        ("starts_with", "starts with"),
        ("has_media", "has media"),
        ("is_reply", "is reply"),
    ):
        value = trigger.get(key)
        if value is None or value is False:
            continue
        if key == "has_media":
            parts.append("has media")
            continue
        if key == "is_reply":
            parts.append("is a reply")
            continue
        if isinstance(value, list):
            parts.append(f"contains {', '.join(str(v)[:60] for v in value)}")
        else:
            parts.append(f"{label} '{str(value)[:60]}'")
    direction = trigger.get("direction", "incoming")
    if direction != "incoming":
        parts.append(f"direction: {direction}")
    return "Telegram message" + (f" ({', '.join(parts)})" if parts else "")