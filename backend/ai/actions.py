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
    "save_link",
    "delete_messages",
    "send",
    "clean_chat",
    "remember",
    "clarify",
})

EXECUTABLE_ACTION_NAMES = frozenset({"save", "deep_save", "save_link", "delete_messages"})

TARGET_SCOPES = frozenset({
    "replied_message",
    "current_message",
    "last_message",
    "recent_messages",
    "saved_item",
    "message_id",
})

# Fields the schema accepts. Anything else is rejected so an LLM can never
# smuggle an unknown field through to execution.
ALLOWED_FIELDS = frozenset({
    "action", "target", "count", "mode", "caption", "recipient", "query",
    "content", "reason", "link", "message_id",
})

_MIN_DELETE_COUNT = 1
_MAX_DELETE_COUNT = 500

# A Telegram message link, with or without the https:// scheme. The URL is
# preserved verbatim — only trailing punctuation is stripped for parsing.
_TELEGRAM_LINK_RE = re.compile(r"(?:https?://)?(?:t|telegram)\.me/\S+")


def _extract_telegram_link(text: str) -> str | None:
    """Extract the first Telegram message link from *text* (exact URL)."""
    if not isinstance(text, str):
        return None
    m = _TELEGRAM_LINK_RE.search(text)
    if not m:
        return None
    url = m.group(0).strip()
    return url.rstrip(".,;:)!?]}>\"'") or None

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
    link: str = ""
    message_id: int | None = None
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

    # Save-by-link: the link is the target. The URL is preserved verbatim and
    # validated only for the Telegram-link shape — the tool re-validates it
    # authoritatively before any Telegram call.
    if action == "save_link":
        link = raw.get("link", "")
        if not isinstance(link, str):
            return ActionParseResult(kind=KIND_INVALID, error="Invalid 'link' field.")
        url = _extract_telegram_link(link)
        if not url:
            return ActionParseResult(
                kind=KIND_INVALID,
                error="Invalid or missing Telegram link.",
            )
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action=action,
            target="telegram_link",
            link=url,
        )

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
        # An explicit single-message target must carry a valid message ID.
        if target == "message_id":
            message_id = coerce_int(raw.get("message_id"))
            if message_id is None or message_id <= 0:
                return ActionParseResult(
                    kind=KIND_INVALID,
                    error="Invalid 'message_id' field.",
                )
            return ActionParseResult(
                kind=KIND_EXECUTABLE,
                action=action,
                target=target,
                message_id=message_id,
            )
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

    if action == "save_link":
        # The existing execute_link_save() resolves the link and reuses the
        # SAME Deep Save pipeline. The URL is passed through verbatim.
        return [{"name": "save_by_link", "arguments": {"link": result.link}}]

    if action == "delete_messages":
        if target == "message_id":
            return [{"name": "delete_message_by_id", "arguments": {"message_id": result.message_id}}]
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
_LAST_TOKENS = frozenset({"آخر", "آخرین", "آخری", "آخریه", "اخیر", "last", "latest", "recent"})
_DEEP_TOKENS = frozenset({"عمیق", "deep", "کامل"})
_MESSAGE_TOKENS = frozenset({"پیام", "پیامها", "message", "messages", "msg", "msgs"})
_COUNT_CONTEXT = _MESSAGE_TOKENS | _LAST_TOKENS
_ID_TOKENS = frozenset({"id", "msgid", "message_id", "ایدی", "آیدی", "شناسه"})

_DEFAULT_LIST_LIMIT = 50

# Persian number words → int, so "ده پیام" / "بیست و پنج پیام" parse like "۱۰ پیام".
_FA_NUMBER_WORDS = {
    "یک": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5, "شش": 6, "هفت": 7, "هشت": 8, "نه": 9,
    "ده": 10, "یازده": 11, "دوازده": 12, "سیزده": 13, "چهارده": 14, "پانزده": 15,
    "شانزده": 16, "هفده": 17, "هجده": 18, "نوزده": 19,
    "بیست": 20, "سی": 30, "چهل": 40, "پنجاه": 50, "شصت": 60, "هفتاد": 70, "هشتاد": 80, "نود": 90,
    "صد": 100, "دویست": 200, "سیصد": 300, "چهارصد": 400, "پانصد": 500,
}


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


def _parse_number(words: list[str], i: int) -> int | None:
    """Parse an ASCII/Persian digit or a Persian number word at index *i*.

    Handles compounds like "بیست و پنج" (20 + 5 = 25). Returns None when
    the token is not a number.
    """
    if i >= len(words):
        return None
    tok = words[i]
    if tok.isdigit():
        try:
            return int(tok)
        except ValueError:
            return None
    if tok not in _FA_NUMBER_WORDS:
        return None
    total = _FA_NUMBER_WORDS[tok]
    j = i + 1
    while j + 1 < len(words) and words[j] == "و" and words[j + 1] in _FA_NUMBER_WORDS:
        total += _FA_NUMBER_WORDS[words[j + 1]]
        j += 2
    return total


def _extract_count(words: list[str]) -> int | None:
    """Extract a 1..500 count near a message/last word.

    Accepts Persian/Arabic-Indic digits (normalized), ASCII digits, and
    Persian number words ("ده", "بیست و پنج", ...).
    """
    for i in range(len(words)):
        n = _parse_number(words, i)
        if n is not None and 1 <= n <= _MAX_DELETE_COUNT:
            window = words[max(0, i - 2):i + 4]
            if any(w in _COUNT_CONTEXT for w in window):
                return n
    return None


def _extract_message_id(words: list[str]) -> int | None:
    """Extract an explicit message ID near an id/شناسه token (positive int)."""
    for i, tok in enumerate(words):
        if tok in _ID_TOKENS:
            for j in range(max(0, i - 2), min(len(words), i + 3)):
                if words[j].isdigit() and int(words[j]) > 0:
                    return int(words[j])
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

    delete_mentioned = delete_pos or delete_neg
    save_mentioned = save_pos or save_neg
    send_mentioned = send_pos

    link_url = _extract_telegram_link(text)

    # Send is recognized but deliberately has no executor wired.
    if send_pos and not do_delete and not do_save:
        return ActionParseResult(kind=KIND_UNSUPPORTED, action="send")

    if do_delete and do_save:
        return ActionParseResult(
            kind=KIND_CLARIFY,
            reason="Do you want me to save or delete?",
        )

    # Save-by-link takes priority over replied-message save when a Telegram
    # link is present — the URL is preserved exactly.
    if do_save and link_url:
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action="save_link",
            target="telegram_link",
            link=link_url,
            tool_calls=[{"name": "save_by_link", "arguments": {"link": link_url}}],
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
        # Explicit message-ID target: "پیام با ID 123 رو پاک کن".
        message_id = _extract_message_id(words)
        if message_id is not None:
            return ActionParseResult(
                kind=KIND_EXECUTABLE,
                action="delete_messages",
                target="message_id",
                message_id=message_id,
                tool_calls=[{"name": "delete_message_by_id", "arguments": {"message_id": message_id}}],
            )
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

    # "Review / show / tell me the last N messages" → list REAL Telegram
    # history from the current chat (all participants). This is the AI-session
    # vs Telegram-chat distinction: inspection always reads Telegram.
    if (
        has_message_word
        and (is_last or count is not None)
        and not delete_mentioned
        and not save_mentioned
        and not send_mentioned
    ):
        limit = count if count is not None else _DEFAULT_LIST_LIMIT
        args: dict[str, Any] = {"limit": limit} if count is not None else {}
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action="list_recent_messages",
            target="recent_messages",
            count=limit,
            tool_calls=[{"name": "list_recent_messages", "arguments": args}],
        )

    return ActionParseResult(kind=KIND_CONVERSATIONAL)
