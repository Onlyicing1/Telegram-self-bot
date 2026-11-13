"""
Intelligent Reaction engine — AI-driven reactions on the owner's own outgoing
messages.

The AI PROPOSES, deterministic code DISPOSES:

    outgoing handler (existing self-client update path)
      → this service (eligibility → dedup → settings → AI evaluation)
        → per-message probability check (independent random draw)
          → reaction_service.react_to_message (existing typed target resolution)
            → telegram_api.reactions.send_reaction (existing typed wrapper)

Contracts:

* **Owner-authored outgoing messages only.** The handler is
  ``events.NewMessage(outgoing=True)`` and this service re-proves the serialized
  message's ``out`` flag and sender identity. The reaction TARGET is always the
  owner's own outgoing ``(chat_id, message_id)``; a replied-to message may only
  ever be context, never a target.
* **The model decides, locally validated.** The AI returns a probability and a
  reaction for the ONE message it was shown. Nothing here inspects the message
  for keywords, and no fixed ratio, quota, rolling window or compensation
  exists: every eligible message gets its own probability and its own
  independent random draw, so previous messages can never influence the current
  one.
* **Catalog-bound reactions.** The selectable set comes from Telegram's own
  available-reaction catalog (``reaction_source_service``). A reaction outside
  that catalog is refused, never substituted — no invented emoji, no fixed
  fallback, and no reaction when the catalog cannot be read.
* **Bounded context.** The model receives the outgoing message text and at most
  ONE replied-to message's text. It never receives chat ids, message ids,
  sender ids, usernames, chat history or unrelated messages.
* **Fail closed.** An unavailable provider, an invalid answer, an unsupported
  reaction, an unusable probability or a refused Telegram call produces NO
  reaction and one bounded log line. Message content is never logged.
* **Independent of the fixed Auto-Reaction mode.** The enabled flag is its own
  ``panel_settings`` column and the fixed emoji is never read. The two modes are
  mutually exclusive; if both are somehow enabled this service declines rather
  than risk a second reaction.
* **Dynamic coalescing, never a timer.** The first eligible message is
  evaluated immediately. While one AI request is in flight, further eligible
  messages wait in a per-owner pending queue; when the request finishes the
  queue is detached atomically and evaluated together in the NEXT request. The
  caller that owns the in-flight request drains the queue itself (leader /
  follower), so there is no background task, no debounce, no timer and no
  fixed window. Every queued message keeps its own Telegram id, its own text,
  its own replied-to context and its own probability draw; nothing is
  concatenated into a synthetic sentence.
* **No second infrastructure.** The handler and provider manager are passed in
  (the manager is the existing ``ProviderManager``), there is no client, loop,
  scheduler, executor or provider of this module's own, and exactly one
  ``SendReactionRequest`` is attempted per message.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import random
import time
from collections import OrderedDict
from collections.abc import Callable
from dataclasses import dataclass
from typing import Any

from backend.helper.input_state import get_pending
from backend.services import reaction_service
from backend.services import reaction_source_service
from backend.services import settings_service
from backend.telegram_api.reactions import normalize_emoji, normalize_reaction

logger = logging.getLogger(__name__)

#: Deduplication registry bound: a small in-memory map of
#: ``(chat_id, message_id) -> monotonic timestamp``. Re-entry is impossible
#: without unbounded state.
GUARD_MAX = 512
GUARD_TTL_S = 900.0

#: One AI evaluation is bounded so a slow provider can never hold the update
#: handler open indefinitely.
AI_TIMEOUT_S = 20.0

#: The model is shown at most this many catalog reactions, and the replied-to
#: context is bounded so one huge message cannot dominate the prompt.
MAX_PROMPT_REACTIONS = 100
MAX_CONTEXT_CHARS = 1000

#: Defensive bound on one model answer; a real answer is a tiny JSON object.
MAX_ANSWER_CHARS = 2000

#: A coalesced batch is bounded so one long burst cannot build an unbounded
#: prompt: at most this many messages (and this many characters of message +
#: replied-to text) are detached per AI request — the rest stay queued for the
#: following request, never dropped and never truncated. The answer budget
#: grows with the number of results requested.
MAX_BATCH_MESSAGES = 20
MAX_BATCH_PROMPT_CHARS = 12000
MAX_BATCH_TOKENS = 512
MAX_BATCH_ANSWER_CHARS = 4000

STATUS_REACTED = "reacted"
STATUS_FAILED = "failed"
STATUS_NOT_SELECTED = "skipped_probability"
STATUS_NO_REACTION = "skipped_no_reaction"
STATUS_DISABLED = "skipped_disabled"
STATUS_CONFLICT = "skipped_conflict_with_auto_reaction"
STATUS_INVALID = "skipped_invalid"
STATUS_NOT_OWNER = "skipped_not_owner_authored"
STATUS_INLINE_ORIGIN = "skipped_inline_origin"
STATUS_PENDING_INPUT = "skipped_pending_input"
STATUS_DUPLICATE = "skipped_duplicate"
STATUS_NO_TEXT = "skipped_no_text"
STATUS_CATALOG_UNAVAILABLE = "skipped_catalog_unavailable"

SYSTEM_PROMPT = (
    "You decide whether the owner's own outgoing Telegram message deserves a "
    "reaction, and which one.\n"
    "Answer with ONE JSON object and nothing else, in exactly this shape:\n"
    '{"probability": <number 0-100>, "reaction": <one emoji from the supported '
    "list, or null>}\n"
    "Rules:\n"
    "- probability is THIS message's own chance of being reacted to. It may "
    "differ for every message: routine or unremarkable messages get a low "
    "value, and a message that clearly does not benefit from a reaction gets "
    "0.\n"
    "- 0 means never react; 100 means always react.\n"
    "- reaction MUST be one of the supported reactions, or null when none fits.\n"
    "- Judge only the message text and the optional replied-to context.\n"
    "- Never explain, never add fields, never use markdown."
)

SYSTEM_PROMPT_BATCH = (
    "You decide, separately for EACH of the owner's own outgoing Telegram "
    "messages, whether it deserves a reaction and which one.\n"
    "Answer with ONE JSON object and nothing else, in exactly this shape:\n"
    '{"results": [{"id": <the integer shown for that message>, '
    '"probability": <number 0-100>, "reaction": <one emoji from the supported '
    'list, or null>}, ...]}\n'
    "Rules:\n"
    "- Return EXACTLY one result for every message, carrying that message's own "
    "id. Never merge messages, never skip one, never repeat an id, never invent "
    "an id.\n"
    "- probability is THAT message's own chance of being reacted to. It may "
    "differ for every message: routine or unremarkable messages get a low "
    "value, and a message that clearly does not benefit from a reaction gets "
    "0.\n"
    "- 0 means never react; 100 means always react.\n"
    "- reaction MUST be one of the supported reactions, or null when none "
    "fits.\n"
    "- Judge each message only on its own text and its own optional replied-to "
    "context. One message must never influence another.\n"
    "- Never explain, never add fields, never use markdown."
)

_handled: "OrderedDict[tuple[int, int], float]" = OrderedDict()


def _guard_prune(now: float) -> None:
    for key in [k for k, stamp in _handled.items() if now - stamp > GUARD_TTL_S]:
        _handled.pop(key, None)
    while len(_handled) > GUARD_MAX:
        _handled.popitem(last=False)


def _guard_has(key: tuple[int, int], now: float) -> bool:
    _guard_prune(now)
    return key in _handled


def _guard_add(key: tuple[int, int], now: float) -> None:
    _handled[key] = now
    _guard_prune(now)


def reset_loop_guard() -> None:
    """Clear the deduplication registry (bounded, in-memory only)."""
    _handled.clear()


def _valid_owner(owner_id: Any) -> bool:
    return isinstance(owner_id, int) and not isinstance(owner_id, bool) and owner_id > 0


def _valid_chat_id(chat_id: Any) -> bool:
    return isinstance(chat_id, int) and not isinstance(chat_id, bool) and chat_id != 0


def _valid_message_id(msg_id: Any) -> bool:
    return isinstance(msg_id, int) and not isinstance(msg_id, bool) and msg_id > 0


def _outcome(
    status: str,
    *,
    error: str | None = None,
    probability: float | None = None,
    reaction: dict[str, Any] | None = None,
) -> dict[str, Any]:
    return {
        "status": status,
        "error": error,
        "probability": probability,
        "reaction": reaction,
    }


def valid_probability(value: Any) -> float | None:
    """A percentage in 0..100 (never a bool, never NaN/inf), else ``None``."""
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    number = float(value)
    if not math.isfinite(number) or number < 0.0 or number > 100.0:
        return None
    return number


def passes_probability(probability: float, draw: float) -> bool:
    """The per-message independent decision for ONE message.

    ``draw`` is a uniform value in ``[0, 1)``. 0% never passes, 100% always
    passes, and nothing else is consulted — no counter, no window, no previous
    message.
    """
    if probability <= 0.0:
        return False
    if probability >= 100.0:
        return True
    return draw * 100.0 < probability


def _extract_json(
    text: str, *, limit: int = MAX_ANSWER_CHARS,
) -> dict[str, Any] | None:
    """The first JSON object in a bounded model answer, or ``None``."""
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if len(cleaned) > limit:
        return None
    if cleaned.startswith("```"):
        cleaned = cleaned.strip("`").strip()
        if cleaned.lower().startswith("json"):
            cleaned = cleaned[4:].strip()
    start = cleaned.find("{")
    end = cleaned.rfind("}")
    if start == -1 or end <= start:
        return None
    try:
        parsed = json.loads(cleaned[start:end + 1])
    except (ValueError, TypeError):
        return None
    return parsed if isinstance(parsed, dict) else None


def _decision_from_entry(
    entry: dict[str, Any], supported_emojis: list[str],
) -> dict[str, Any]:
    """Validate ONE decision object (single message or one batch result).

    An unsupported reaction is a hard failure for that decision — never a
    substitution and never a silent "reaction only" fallback.
    """
    if "probability" not in entry:
        return {"ok": False, "error": "answer has no probability"}
    probability = valid_probability(entry.get("probability"))
    if probability is None:
        return {"ok": False, "error": "probability is not a percentage in 0..100"}
    raw_reaction = entry.get("reaction")
    if raw_reaction is None or raw_reaction == "":
        return {"ok": True, "probability": probability, "reaction": None}
    if not isinstance(raw_reaction, str):
        return {"ok": False, "error": "reaction is not a string"}
    emoji = normalize_emoji(raw_reaction)
    if emoji is None:
        return {"ok": False, "error": "reaction is not a usable emoji"}
    if emoji not in supported_emojis:
        return {"ok": False, "error": "reaction is not in Telegram's catalog"}
    return {
        "ok": True,
        "probability": probability,
        "reaction": normalize_reaction({"kind": "emoji", "emoji": emoji}),
    }


def parse_evaluation(
    text: Any, supported_emojis: list[str],
) -> dict[str, Any]:
    """Validate one model answer against the catalog.

    Returns ``{"ok": True, "probability": float, "reaction": descriptor|None}``
    or ``{"ok": False, "error": <bounded machine reason>}``.
    """
    parsed = _extract_json(text)
    if parsed is None:
        return {"ok": False, "error": "answer is not a JSON object"}
    return _decision_from_entry(parsed, supported_emojis)


def _result_label(value: Any) -> int | None:
    """A batch result's synthetic message label (a positive int), else None."""
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value if value > 0 else None
    if isinstance(value, str) and value.strip().isdigit():
        number = int(value.strip())
        return number if number > 0 else None
    return None


def parse_batch_evaluation(
    text: Any, labels: list[int], supported_emojis: list[str],
) -> dict[str, Any]:
    """Validate a multi-message answer: one identifiable result per message.

    The labels are the synthetic per-request message numbers (never Telegram
    ids, which the model never sees). Structural violations reject the WHOLE
    answer — an unknown id, a duplicate id, a missing id, a non-list ``results``
    or a non-object entry — so no message can silently inherit another's
    decision. A per-message invalid probability/reaction fails only that
    message. Returns ``{"ok": True, "results": {label: decision}}`` or
    ``{"ok": False, "error": <bounded machine reason>}``.
    """
    parsed = _extract_json(text, limit=MAX_BATCH_ANSWER_CHARS)
    if parsed is None:
        return {"ok": False, "error": "answer is not a JSON object"}
    raw_results = parsed.get("results")
    if not isinstance(raw_results, list):
        return {"ok": False, "error": "answer has no results list"}
    wanted = set(labels)
    seen: dict[int, dict[str, Any]] = {}
    for entry in raw_results:
        if not isinstance(entry, dict):
            return {"ok": False, "error": "a result is not a JSON object"}
        label = _result_label(entry.get("id"))
        if label is None:
            return {"ok": False, "error": "a result has no usable id"}
        if label not in wanted:
            return {"ok": False, "error": f"result id {label} was not requested"}
        if label in seen:
            return {"ok": False, "error": f"result id {label} appears twice"}
        seen[label] = _decision_from_entry(entry, supported_emojis)
    missing = sorted(wanted - set(seen))
    if missing:
        return {"ok": False, "error": f"no result for message id {missing[0]}"}
    return {"ok": True, "results": seen}


async def _reply_context(
    client: Any, chat_id: int, reply_to_msg_id: Any,
) -> str:
    """The ONE replied-to message's text, or ``""``.

    Only the text is returned: no ids, no sender, no media. A missing, deleted
    or unreadable reply is simply no context.
    """
    if not _valid_message_id(reply_to_msg_id):
        return ""
    try:
        reply = await client.get_messages(chat_id, ids=reply_to_msg_id)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.info("[INTELLIGENT_REACTION] reply context unavailable: %s", type(exc).__name__)
        return ""
    text = getattr(reply, "text", None) or getattr(reply, "message", None) or ""
    if not isinstance(text, str):
        return ""
    text = text.strip()
    return text[:MAX_CONTEXT_CHARS]


def _prompt_messages(text: str, context_text: str, emojis: list[str]) -> list[dict[str, Any]]:
    """The bounded model input: the message, plus optional reply text.

    Deliberately free of ids, sender identity, usernames and history.
    """
    supported = " ".join(emojis[:MAX_PROMPT_REACTIONS])
    # The instructions live in the system turn only — never duplicated here.
    parts = [
        f"Supported reactions (choose only from these): {supported}",
        "",
        "Message:",
        text,
    ]
    if context_text:
        parts.extend(["", "Replied-to message (context only, never the target):", context_text])
    return [
        {"role": "system", "content": SYSTEM_PROMPT},
        {"role": "user", "content": "\n".join(parts)},
    ]


async def _ask_provider(
    provider_manager: Any, messages: list[dict[str, Any]], *, max_tokens: int,
) -> tuple[str, str | None]:
    """One bounded provider call through the existing ProviderManager.

    Returns ``(answer_text, error)``; ``error`` is ``None`` only for a usable
    answer. Never raises (cancellation propagates).
    """
    try:
        response = await asyncio.wait_for(
            provider_manager.chat(
                messages,
                temperature=0.3,
                max_tokens=max_tokens,
                response_format={"type": "json_object"},
            ),
            timeout=AI_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        return "", "the AI evaluation timed out"
    except Exception as exc:
        return "", f"the AI evaluation failed ({type(exc).__name__})"
    if not getattr(response, "success", False):
        return "", "the AI provider returned no usable answer"
    return getattr(response, "text", ""), None


async def evaluate_message(
    provider_manager: Any,
    *,
    text: str,
    context_text: str,
    emojis: list[str],
) -> dict[str, Any]:
    """Ask the existing provider mesh for ONE message's probability + reaction.

    Never raises; an unavailable manager, a failed response, a timeout or an
    invalid answer is an honest ``ok=False`` and nothing is sent.
    """
    if provider_manager is None:
        return {"ok": False, "error": "the AI provider manager is unavailable"}
    answer, error = await _ask_provider(
        provider_manager, _prompt_messages(text, context_text, emojis), max_tokens=96,
    )
    if error is not None:
        return {"ok": False, "error": error}
    return parse_evaluation(answer, emojis)


def _batch_prompt_messages(items: list["_Item"], emojis: list[str]) -> list[dict[str, Any]]:
    """One bounded prompt carrying several INDEPENDENT message records.

    Each message keeps its own text and its own replied-to context under a
    small synthetic label (``1..n``); Telegram ids, chats, senders and history
    are never included, and no text is concatenated into a synthetic sentence.
    """
    supported = " ".join(emojis[:MAX_PROMPT_REACTIONS])
    parts = [
        f"Supported reactions (choose only from these): {supported}",
        "",
        f"There are {len(items)} messages. Answer for every one of them.",
        "",
    ]
    for label, item in enumerate(items, start=1):
        parts.extend([f"Message {label}:", item.text, ""])
        if item.context_text:
            parts.extend([
                f"Replied-to context for message {label} (context only, never the target):",
                item.context_text,
                "",
            ])
    return [
        {"role": "system", "content": SYSTEM_PROMPT_BATCH},
        {"role": "user", "content": "\n".join(parts)},
    ]


async def evaluate_message_batch(
    provider_manager: Any, items: list["_Item"], emojis: list[str],
) -> dict[str, Any]:
    """Ask the provider mesh for a decision per queued message, in ONE call.

    Returns ``{"ok": True, "results": {label: decision}}`` or an honest
    ``ok=False`` for a structural violation; the caller fails every message of
    the batch rather than guessing which decision belonged to whom.
    """
    if provider_manager is None:
        return {"ok": False, "error": "the AI provider manager is unavailable"}
    answer, error = await _ask_provider(
        provider_manager,
        _batch_prompt_messages(items, emojis),
        max_tokens=min(MAX_BATCH_TOKENS, 96 * max(1, len(items))),
    )
    if error is not None:
        return {"ok": False, "error": error}
    return parse_batch_evaluation(answer, list(range(1, len(items) + 1)), emojis)


def _default_provider_manager() -> Any:
    try:
        from backend.ai.engine.engine import get_engine
        return get_engine().provider_manager
    except Exception as exc:
        logger.warning("[INTELLIGENT_REACTION] no AI engine available: %s", exc)
        return None


async def process_outgoing_message(
    *,
    owner_id: int,
    client: Any,
    message: dict[str, Any],
    via_bot_id: int | None = None,
    provider_manager: Any = None,
    random_fn: Callable[[], float] | None = None,
) -> dict[str, Any]:
    """Evaluate ONE owner-authored outgoing message and maybe react to it.

    ``message`` is the ``serialize_message`` representation of the owner's
    outgoing message. ``provider_manager`` is the existing
    ``ProviderManager`` (resolved from the runtime engine when omitted);
    ``random_fn`` is the independent per-message draw (``random.random`` when
    omitted, injectable for tests). Returns an honest outcome dict and never
    raises for a condition it can describe.
    """
    if not _valid_owner(owner_id) or not isinstance(message, dict):
        return _outcome(STATUS_INVALID, error="invalid owner or message")

    chat_id = message.get("chat_id")
    msg_id = message.get("id")
    if not _valid_chat_id(chat_id) or not _valid_message_id(msg_id):
        return _outcome(STATUS_INVALID, error="message has no usable chat/message id")

    if message.get("out") is not True or message.get("sender_id") != owner_id:
        return _outcome(STATUS_NOT_OWNER, error="message is not owner-authored")

    if via_bot_id is None:
        via_bot_id = message.get("via_bot_id")
    if _valid_message_id(via_bot_id):
        return _outcome(STATUS_INLINE_ORIGIN, error="message was sent through an inline bot")

    now = time.monotonic()
    key = (chat_id, msg_id)
    if _guard_has(key, now):
        return _outcome(STATUS_DUPLICATE, error="message was already handled")

    pending = get_pending(owner_id)
    if isinstance(pending, dict) and pending.get("chat_id") == chat_id:
        return _outcome(STATUS_PENDING_INPUT, error="message is input for a pending panel flow")

    if not settings_service.is_intelligent_reaction_enabled():
        return _outcome(STATUS_DISABLED, error="intelligent reaction is disabled")
    if settings_service.is_auto_reaction_enabled():
        logger.warning(
            "[INTELLIGENT_REACTION] both modes are enabled; declining chat=%s msg=%s",
            chat_id, msg_id,
        )
        return _outcome(
            STATUS_CONFLICT, error="the fixed Auto-Reaction mode is also enabled",
        )

    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return _outcome(STATUS_NO_TEXT, error="message has no text to evaluate")

    # Mark handled BEFORE the message is queued so a duplicate/edited update
    # for the same message never evaluates or reacts twice — and a message
    # waiting in the coalescing queue is already claimed.
    _guard_add(key, now)

    context_text = await _reply_context(client, chat_id, message.get("reply_to_msg_id"))
    manager = provider_manager if provider_manager is not None else _default_provider_manager()

    item = _Item(
        owner_id=owner_id,
        client=client,
        chat_id=chat_id,
        msg_id=msg_id,
        text=text,
        context_text=context_text,
        provider_manager=manager,
        random_fn=random_fn,
    )
    return await _submit(_scope_for(owner_id), item)


# ── dynamic coalescing ──────────────────────────────────────────────────────
#
# The scope is the owner: the handler is one listener on one self-client for
# one owner, so that is the natural processing scope — not per chat, not
# global. The FIRST eligible message becomes the leader and runs the AI
# request in its own handler coroutine (no background task, scheduler or
# timer). While that request is awaited, further messages append themselves to
# the leader's pending list and wait on their own future; when the request
# finishes, the leader detaches the whole pending list and evaluates it in the
# next request. Messages that arrive while that next request runs are simply
# added to the new pending list, so the loop keeps alternating until the queue
# drains.
#
# Every transition below is plain synchronous code on the running event loop:
# the queue is appended, the leader flag is checked and the batch is detached
# with no await in between, so the check/claim/detach sequence cannot
# interleave with another coroutine. That is what makes the protocol
# race-free without a second lock or worker.


@dataclass
class _Item:
    """One queued outgoing message, keeping its own identity and context."""

    owner_id: int
    client: Any
    chat_id: int
    msg_id: int
    text: str
    context_text: str
    provider_manager: Any
    random_fn: Callable[[], float] | None
    future: Any = None


class _Scope:
    """The coalescing state for one owner on one event loop."""

    __slots__ = ("leader", "pending")

    def __init__(self) -> None:
        self.leader = False
        self.pending: list[_Item] = []


_scopes: dict[int, _Scope] = {}
_scopes_loop: Any = None


def _scope_for(owner_id: int) -> _Scope:
    """The owner's coalescing scope, rebound per event loop.

    A new event loop (a restart, or a test's fresh loop) can never inherit
    futures or a leader flag from the old one; any work left over is reported
    rather than silently forgotten.
    """
    global _scopes_loop
    loop = asyncio.get_running_loop()
    if _scopes_loop is not loop:
        abandoned = sum(len(scope.pending) for scope in _scopes.values())
        if abandoned:
            logger.warning(
                "[INTELLIGENT_REACTION] %d queued message(s) dropped with their event loop",
                abandoned,
            )
        _scopes.clear()
        _scopes_loop = loop
    scope = _scopes.get(owner_id)
    if scope is None:
        scope = _scopes[owner_id] = _Scope()
    return scope


def reset_coalescer() -> None:
    """Drop all coalescing state (tests and a fresh runtime)."""
    global _scopes_loop
    _scopes.clear()
    _scopes_loop = None


def _settle(item: _Item, outcome: dict[str, Any]) -> None:
    future = item.future
    if future is not None and not future.done():
        future.set_result(outcome)


async def _submit(scope: _Scope, item: _Item) -> dict[str, Any]:
    """Queue the message; lead the drain when no request is in flight."""
    item.future = asyncio.get_running_loop().create_future()
    scope.pending.append(item)
    if scope.leader:
        return await item.future
    scope.leader = True
    return await _lead(scope, item)


def _detach_pending(scope: _Scope) -> list[_Item]:
    """Atomically take the next bounded batch off the front of the queue.

    Runs with no await inside, so no message can be queued between the size
    decision and the removal, and none can be taken twice.
    """
    budget = MAX_BATCH_PROMPT_CHARS
    count = 0
    total = 0
    for item in scope.pending[:MAX_BATCH_MESSAGES]:
        size = len(item.text) + len(item.context_text)
        if count and total + size > budget:
            break
        total += size
        count += 1
    batch = scope.pending[:count]
    del scope.pending[:count]
    return batch


def _leader_outcome(item: _Item) -> dict[str, Any]:
    future = item.future
    if future is not None and future.done() and not future.cancelled():
        return future.result()
    return _outcome(STATUS_FAILED, error="the reaction batch was interrupted")


async def _lead(scope: _Scope, leader_item: _Item) -> dict[str, Any]:
    """Drain the queue in THIS coroutine: one AI request in flight at a time."""
    batch: list[_Item] = []
    try:
        while True:
            batch = _detach_pending(scope)
            if not batch:
                scope.leader = False
                return _leader_outcome(leader_item)
            try:
                await _run_batch(batch)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # a batch bug must never wedge the queue
                logger.warning(
                    "[INTELLIGENT_REACTION] batch failed (%s) for %d message(s)",
                    type(exc).__name__, len(batch),
                )
                for item in batch:
                    _settle(item, _outcome(STATUS_FAILED,
                                           error="the evaluation batch failed"))
            batch = []
    finally:
        # Cancellation (disconnect, shutdown) or an escaped error: release
        # leadership and settle every message this coroutine still owns so no
        # caller waits forever and the next message starts cleanly.
        if scope.leader:
            scope.leader = False
            leftover = scope.pending
            scope.pending = []
            interrupted = [item for item in leftover]
            if interrupted:
                logger.warning(
                    "[INTELLIGENT_REACTION] %d queued message(s) interrupted",
                    len(interrupted),
                )
            for item in interrupted:
                _settle(item, _outcome(STATUS_FAILED,
                                       error="the reaction batch was interrupted"))
            for item in batch:
                _settle(item, _outcome(STATUS_FAILED,
                                       error="the reaction batch was interrupted"))


async def _run_batch(items: list[_Item]) -> None:
    """Evaluate a detached batch and settle every message's own future."""
    outcomes = await _evaluate_batch(items)
    for item, outcome in zip(items, outcomes):
        _settle(item, outcome)


async def _evaluate_batch(items: list[_Item]) -> list[dict[str, Any]]:
    """One catalog read + one AI request for the batch, then per-message execution."""
    client = items[0].client
    catalog = await reaction_source_service.load_reactions(client)
    options: list[dict[str, Any]] = []
    if isinstance(catalog, dict):
        options = list(catalog.get("options") or [])
    if not isinstance(catalog, dict) or not catalog.get("ok") or not options:
        detail = catalog.get("detail") if isinstance(catalog, dict) else None
        logger.warning(
            "[INTELLIGENT_REACTION] reaction catalog unavailable for %d message(s): %s",
            len(items), detail or "no usable reactions",
        )
        return [
            _outcome(STATUS_CATALOG_UNAVAILABLE,
                     error="Telegram's reaction catalog is unavailable")
            for _ in items
        ]
    emojis = [str(option.get("emoji")) for option in options if option.get("emoji")]
    if not emojis:
        return [
            _outcome(STATUS_CATALOG_UNAVAILABLE,
                     error="Telegram returned no usable reactions")
            for _ in items
        ]

    manager = items[0].provider_manager
    if manager is None:
        return [
            _outcome(STATUS_FAILED, error="the AI provider manager is unavailable")
            for _ in items
        ]

    decisions: dict[int, Any]
    if len(items) == 1:
        # A single message keeps the original one-message contract verbatim.
        item = items[0]
        decisions = {
            1: await evaluate_message(
                manager, text=item.text, context_text=item.context_text, emojis=emojis,
            ),
        }
    else:
        evaluated = await evaluate_message_batch(manager, items, emojis)
        if not evaluated.get("ok"):
            error = str(evaluated.get("error") or "invalid AI answer")
            logger.warning(
                "[INTELLIGENT_REACTION] no reaction for %d message(s): %s",
                len(items), error,
            )
            return [_outcome(STATUS_FAILED, error=error) for _ in items]
        decisions = evaluated["results"]

    return [
        await _apply_decision(item, decisions.get(label))
        for label, item in enumerate(items, start=1)
    ]


async def _apply_decision(item: _Item, decision: Any) -> dict[str, Any]:
    """Validate one message's OWN result, then draw and execute for that message."""
    chat_id, msg_id = item.chat_id, item.msg_id
    if not isinstance(decision, dict):
        return _outcome(STATUS_FAILED, error="no usable result for this message")
    if not decision.get("ok"):
        error = str(decision.get("error") or "invalid AI answer")
        logger.warning(
            "[INTELLIGENT_REACTION] no reaction chat=%s msg=%s: %s",
            chat_id, msg_id, error,
        )
        return _outcome(STATUS_FAILED, error=error)

    probability = decision["probability"]
    reaction = decision.get("reaction")
    if reaction is None:
        return _outcome(STATUS_NO_REACTION, probability=probability,
                        error="the AI chose no reaction")

    draw_fn = item.random_fn or random.random
    try:
        draw = float(draw_fn())
    except Exception as exc:
        logger.warning("[INTELLIGENT_REACTION] random draw failed: %s", type(exc).__name__)
        return _outcome(STATUS_FAILED, probability=probability,
                        error="the probability check failed")
    if not 0.0 <= draw < 1.0:
        return _outcome(STATUS_FAILED, probability=probability,
                        error="the probability draw was not in 0..1")
    if not passes_probability(probability, draw):
        return _outcome(STATUS_NOT_SELECTED, probability=probability, reaction=reaction,
                        error="this message did not pass its own probability check")

    client = item.client
    if client is None:
        return _outcome(STATUS_FAILED, probability=probability, reaction=reaction,
                        error="the self client is not connected")

    try:
        result = await reaction_service.react_to_message(
            client, item.owner_id, chat_id, msg_id, reaction,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning(
            "[INTELLIGENT_REACTION] reaction failed chat=%s msg=%s: %s",
            chat_id, msg_id, exc,
        )
        return _outcome(STATUS_FAILED, probability=probability, reaction=reaction,
                        error=f"reaction failed: {exc}")

    if not isinstance(result, dict) or not result.get("ok"):
        detail = result.get("detail") if isinstance(result, dict) else None
        logger.warning(
            "[INTELLIGENT_REACTION] telegram rejected chat=%s msg=%s: %s",
            chat_id, msg_id, detail or "unknown error",
        )
        return _outcome(STATUS_FAILED, probability=probability, reaction=reaction,
                        error=detail or "reaction rejected")

    logger.info(
        "[INTELLIGENT_REACTION] reacted chat=%s msg=%s probability=%s",
        chat_id, msg_id, probability,
    )
    return _outcome(STATUS_REACTED, probability=probability,
                    reaction=result.get("reaction", reaction))
