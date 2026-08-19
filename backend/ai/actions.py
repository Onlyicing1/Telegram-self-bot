"""
Structured AI action contract — parse, validate, and resolve executable intent.

The AI model is an INTENT interpreter, never an executor. This module turns
the model's structured output (a native tool call, or a JSON action object
embedded in the text response) into concrete ``tool_calls`` for the EXISTING
``ToolExecutor``. The model's output is never trusted as executable code:

  parse → validate (action/fields/count/target) → resolve target
        → existing tool call → existing service → real result

Unknown actions, unknown fields, invalid counts, and unsupported targets are
rejected locally. Only a narrow allowlist of actions reaches the executor, and
each mapped action delegates to an existing LifeOS tool/service — no new
executor, no direct Telegram access.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from backend.ai.persian import coerce_int, normalize_digits

# ── Action vocabulary ──

# Recognized action names. EXECUTABLE_ACTION_NAMES map to an existing tool;
# the others are recognized but deliberately have no executor wired.
ACTION_NAMES = frozenset({
    "save",
    "deep_save",
    "delete_messages",
    "send",
    "clean_chat",
    "remember",
    "clarify",
})

EXECUTABLE_ACTION_NAMES = frozenset({"save", "deep_save", "delete_messages"})

TARGET_SCOPES = frozenset({
    "replied_message",
    "current_message",
    "last_message",
    "recent_messages",
    "saved_item",
})

# Fields the schema accepts. Anything else is rejected so an LLM can never
# smuggle an unknown field through to execution.
ALLOWED_FIELDS = frozenset({
    "action", "target", "count", "mode", "caption", "recipient", "query",
    "content", "reason",
})

_MIN_DELETE_COUNT = 1
_MAX_DELETE_COUNT = 500

# ── Parse outcome kinds ──

KIND_CONVERSATIONAL = "conversational"   # prose, no action
KIND_EXECUTABLE = "executable"           # validated + resolved to tool calls
KIND_CLARIFY = "clarify"                 # model asked for clarification
KIND_INVALID = "invalid"                 # rejected locally (unknown/field/count)
KIND_UNSUPPORTED = "unsupported"         # recognized action, no executor


@dataclass(frozen=True)
class ActionParseResult:
    """Result of parsing and validating one model output.

    ``tool_calls`` is populated only for ``executable`` results and always
    contains the concrete tool name + arguments understood by the existing
    ``ToolExecutor`` (e.g. ``{"name": "save", "arguments": {}}``).
    """

    kind: str
    action: str = ""
    target: str = ""
    count: int | None = None
    caption: bool = False
    reason: str = ""
    error: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


# ── Parsing ──


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from model text, tolerating fences/prose.

    Returns ``None`` when no JSON object is present (conversational prose).
    """
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None

    # Strip a markdown code fence if present.
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start:end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


# ── Validation ──


def validate_action(raw: dict[str, Any]) -> ActionParseResult:
    """Validate a raw action object. Never raises; never executes.

    Rejects unknown fields, unknown actions, invalid targets, and invalid
    counts. Returns a structured ``ActionParseResult`` with the normalized
    action, target, count, and (for executable actions) nothing yet — the
    target is resolved to tool calls by :func:`resolve_tool_calls`.
    """
    if not isinstance(raw, dict):
        return ActionParseResult(kind=KIND_INVALID, error="Action must be a JSON object.")

    unknown = sorted(set(raw) - ALLOWED_FIELDS)
    if unknown:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=f"Unknown field(s): {', '.join(unknown)}",
        )

    action = raw.get("action")
    if not isinstance(action, str) or not action.strip():
        return ActionParseResult(kind=KIND_INVALID, error="Missing 'action' field.")
    action = action.strip()

    if action not in ACTION_NAMES:
        return ActionParseResult(kind=KIND_INVALID, error=f"Unknown action: {action}")

    if action == "clarify":
        return ActionParseResult(
            kind=KIND_CLARIFY,
            action=action,
            reason=str(raw.get("reason", "") or ""),
        )

    if action not in EXECUTABLE_ACTION_NAMES:
        return ActionParseResult(kind=KIND_UNSUPPORTED, action=action)

    target = raw.get("target", "")
    if target:
        if not isinstance(target, str):
            return ActionParseResult(kind=KIND_INVALID, error="Invalid 'target' field.")
        target = target.strip()
        if target not in TARGET_SCOPES:
            return ActionParseResult(kind=KIND_INVALID, error=f"Unknown target: {target}")

    count: int | None = None
    if "count" in raw:
        count = coerce_int(raw.get("count"))
        if count is None or count < _MIN_DELETE_COUNT or count > _MAX_DELETE_COUNT:
            return ActionParseResult(
                kind=KIND_INVALID,
                error=f"Invalid count: {raw.get('count')!r} (must be 1-{_MAX_DELETE_COUNT}).",
            )

    if action == "delete_messages":
        # A recent_messages deletion (explicit or implied by a bare count)
        # must carry a deterministic count. A bare "delete" with no count and
        # no target is genuinely ambiguous → ask.
        effective_target = target or ("recent_messages" if count else "")
        if effective_target == "recent_messages" and count is None:
            return ActionParseResult(
                kind=KIND_CLARIFY,
                action=action,
                reason="How many messages should I delete?",
            )
        if not target and not count:
            return ActionParseResult(
                kind=KIND_CLARIFY,
                action=action,
                reason="Which message(s) should I delete?",
            )

    return ActionParseResult(
        kind=KIND_EXECUTABLE,
        action=action,
        target=target,
        count=count,
        caption=bool(raw.get("caption", False)),
    )


# ── Target resolution ──


def _default_target(action: str) -> str:
    if action in ("save", "deep_save"):
        return "replied_message"
    return "recent_messages"


def resolve_tool_calls(result: ActionParseResult) -> list[dict[str, Any]]:
    """Resolve a validated action into concrete tool calls for the ToolExecutor.

    Each returned call maps to an EXISTING tool (save / delete / delete_replied)
    which in turn delegates to the existing service layer. Telegram identity is
    resolved by those tools from the runtime context — never fabricated here.
    """
    if result.kind != KIND_EXECUTABLE:
        return []

    action = result.action
    target = result.target or _default_target(action)

    if action in ("save", "deep_save"):
        # Save is Deep Save only; the SaveTool resolves the replied-to message
        # from runtime context and calls execute_save(). Captions are always
        # preserved by the existing deep-save pipeline.
        return [{"name": "save", "arguments": {}}]

    if action == "delete_messages":
        if target in ("replied_message", "current_message"):
            return [{"name": "delete_replied", "arguments": {}}]
        if target == "last_message":
            return [{"name": "delete", "arguments": {"count": 1}}]
        if target == "recent_messages":
            return [{"name": "delete", "arguments": {"count": result.count or 1}}]

    return []


def parse_action_text(text: str) -> ActionParseResult:
    """Parse, validate, and resolve one model text output.

    Prose with no JSON → conversational. JSON action → validated and, when
    executable, resolved into tool calls. Unknown/unsupported/ambiguous
    outcomes are returned without ever reaching the executor.
    """
    raw = extract_json_object(text)
    if raw is None:
        return ActionParseResult(kind=KIND_CONVERSATIONAL)

    result = validate_action(raw)
    if result.kind == KIND_EXECUTABLE:
        tool_calls = resolve_tool_calls(result)
        if not tool_calls:
            return ActionParseResult(
                kind=KIND_UNSUPPORTED,
                action=result.action,
                error=f"Unsupported action: {result.action}",
            )
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action=result.action,
            target=result.target or _default_target(result.action),
            count=result.count,
            caption=result.caption,
            tool_calls=tool_calls,
        )
    return result


# ── Deterministic command intent (Persian/English) ──
#
# Safety net for when a provider returns prose instead of a structured action.
# It recognizes the narrow, high-confidence command vocabulary (save / deep
# save / delete N) directly from the ORIGINAL user message and resolves targets
# from the reply context — never from the model's prose. Only deterministic,
# high-confidence matches are produced; everything else stays conversational.

_DELETE_STEMS = ("پاک", "حذف")
_SAVE_STEMS = ("سیو", "ذخیره", "ذخیر")
_SEND_STEMS = ("بفرست", "ارسال", "فوروارد")

_IMPERATIVE_SUFFIXES = frozenset({"کن", "کنی", "کنید", "کنین"})

_EN_DELETE = frozenset({"delete", "remove", "deleting", "removing", "deleted", "removed"})
_EN_SAVE = frozenset({"save", "saving", "saved", "store", "storing"})
_EN_SEND = frozenset({"send", "sending", "forward", "forwarding"})
_EN_NEGATION = frozenset({"not", "never", "dont", "didnt"})

_THIS_TOKENS = frozenset({"این", "اینو", "اینم", "همین", "همینو", "this", "that", "it"})
_LAST_TOKENS = frozenset({"آخر", "آخرین", "آخری", "آخریه", "last", "latest", "recent"})
_DEEP_TOKENS = frozenset({"عمیق", "deep", "کامل"})
_MESSAGE_TOKENS = frozenset({"پیام", "پیامها", "message", "messages", "msg", "msgs"})
_COUNT_CONTEXT = _MESSAGE_TOKENS | _LAST_TOKENS


def _tokenize(text: str) -> list[str]:
    """Lowercase, normalize digits, and split Persian/English into word tokens."""
    s = normalize_digits(text)
    s = s.replace("\u200c", " ").replace("\u200b", " ")
    s = s.replace("'", "").replace("’", "")
    s = s.lower()
    return [t for t in re.findall(r"[a-z0-9\u0600-\u06ff]+", s) if t]


def _is_stem_token(tok: str, stems: tuple[str, ...]) -> bool:
    """True when *tok* is a verb stem, optionally with an attached -sh clitic."""
    for stem in stems:
        if tok == stem:
            return True
        if tok.startswith(stem + "ش"):
            return True
    return False


def _imperative_present(words: list[str], stems: tuple[str, ...]) -> bool:
    """True when a Persian imperative (stem + کن/کنی/کنید) is present."""
    for i, tok in enumerate(words):
        if _is_stem_token(tok, stems):
            if i + 1 < len(words) and words[i + 1] in _IMPERATIVE_SUFFIXES:
                return True
    return False


def _negated_present(words: list[str], stems: tuple[str, ...]) -> bool:
    """True when a Persian imperative is negated (stem + نکن...)."""
    for i, tok in enumerate(words):
        if _is_stem_token(tok, stems):
            if i + 1 < len(words) and words[i + 1].startswith("نکن"):
                return True
    return False


def _english_action(words: list[str], verbs: frozenset[str]) -> tuple[bool, bool]:
    """Return (present, negated) for an English verb token."""
    for i, tok in enumerate(words):
        if tok in verbs:
            negated = any(words[j] in _EN_NEGATION for j in range(max(0, i - 2), i))
            return True, negated
    return False, False


def _extract_count(words: list[str]) -> int | None:
    """Extract a 1..500 count near a message/last word (Persian digits normalized)."""
    for i, tok in enumerate(words):
        if tok.isdigit():
            n = int(tok)
            if 1 <= n <= _MAX_DELETE_COUNT:
                window = words[max(0, i - 2):i + 3]
                if any(w in _COUNT_CONTEXT for w in window):
                    return n
    return None


def parse_command_intent(text: str, *, has_reply: bool = True) -> ActionParseResult:
    """Deterministically parse a Persian/English executable command.

    Called when the model returned prose (no structured action). It never
    trusts the model's prose: it reads the original user message and resolves
    the target from the reply context. Only the narrow command vocabulary is
    recognized — everything else is conversational.
    """
    if not isinstance(text, str) or not text.strip():
        return ActionParseResult(kind=KIND_CONVERSATIONAL)

    words = _tokenize(text)
    if not words:
        return ActionParseResult(kind=KIND_CONVERSATIONAL)

    is_deep = any(w in _DEEP_TOKENS for w in words)
    is_this = any(w in _THIS_TOKENS for w in words)
    is_last = any(w in _LAST_TOKENS for w in words)
    has_message_word = any(w in _MESSAGE_TOKENS for w in words)
    count = _extract_count(words)

    delete_pos = _imperative_present(words, _DELETE_STEMS)
    delete_neg = _negated_present(words, _DELETE_STEMS)
    save_pos = _imperative_present(words, _SAVE_STEMS)
    save_neg = _negated_present(words, _SAVE_STEMS)
    send_pos = _imperative_present(words, _SEND_STEMS) or any(
        _is_stem_token(w, _SEND_STEMS) for w in words
    )

    en_delete, en_delete_neg = _english_action(words, _EN_DELETE)
    en_save, en_save_neg = _english_action(words, _EN_SAVE)
    en_send, _ = _english_action(words, _EN_SEND)

    # A bare English verb with no target/count/reply is likely a question
    # ("what does save mean?") rather than a command.
    en_has_target = has_reply or is_this or is_last or count is not None or has_message_word
    if not en_has_target:
        en_delete = en_save = en_send = False

    if en_delete:
        delete_pos, delete_neg = True, en_delete_neg
    if en_save:
        save_pos, save_neg = True, en_save_neg
    if en_send:
        send_pos = True

    do_delete = delete_pos and not delete_neg
    do_save = save_pos and not save_neg

    # Send is recognized but deliberately has no executor wired.
    if send_pos and not do_delete and not do_save:
        return ActionParseResult(kind=KIND_UNSUPPORTED, action="send")

    if do_delete and do_save:
        return ActionParseResult(
            kind=KIND_CLARIFY,
            reason="Do you want me to save or delete?",
        )

    if do_save:
        action = "deep_save" if is_deep else "save"
        if has_reply:
            return ActionParseResult(
                kind=KIND_EXECUTABLE,
                action=action,
                target="replied_message",
                tool_calls=[{"name": "save", "arguments": {}}],
            )
        return ActionParseResult(
            kind=KIND_CLARIFY,
            action=action,
            reason="Reply to the message you want me to save, then I can save it.",
        )

    if do_delete:
        if is_last or count is not None:
            n = count or 1
            target = "last_message" if (is_last and n == 1) else "recent_messages"
            return ActionParseResult(
                kind=KIND_EXECUTABLE,
                action="delete_messages",
                target=target,
                count=n,
                tool_calls=[{"name": "delete", "arguments": {"count": n}}],
            )
        if is_this:
            if has_reply:
                return ActionParseResult(
                    kind=KIND_EXECUTABLE,
                    action="delete_messages",
                    target="replied_message",
                    count=1,
                    tool_calls=[{"name": "delete_replied", "arguments": {}}],
                )
            return ActionParseResult(
                kind=KIND_CLARIFY,
                action="delete_messages",
                reason=(
                    "Reply to the message you want me to delete, or tell me "
                    "how many of your last messages to delete."
                ),
            )
        return ActionParseResult(
            kind=KIND_CLARIFY,
            action="delete_messages",
            reason="Which message(s) should I delete?",
        )

    return ActionParseResult(kind=KIND_CONVERSATIONAL)
