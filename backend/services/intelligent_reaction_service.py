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


def _extract_json(text: str) -> dict[str, Any] | None:
    """The first JSON object in a bounded model answer, or ``None``."""
    if not isinstance(text, str):
        return None
    cleaned = text.strip()
    if len(cleaned) > MAX_ANSWER_CHARS:
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


def parse_evaluation(
    text: Any, supported_emojis: list[str],
) -> dict[str, Any]:
    """Validate one model answer against the catalog.

    Returns ``{"ok": True, "probability": float, "reaction": descriptor|None}``
    or ``{"ok": False, "error": <bounded machine reason>}``. An unsupported
    reaction is a hard failure — never a substitution and never a silent
    "reaction only" fallback.
    """
    parsed = _extract_json(text)
    if parsed is None:
        return {"ok": False, "error": "answer is not a JSON object"}
    if "probability" not in parsed:
        return {"ok": False, "error": "answer has no probability"}
    probability = valid_probability(parsed.get("probability"))
    if probability is None:
        return {"ok": False, "error": "probability is not a percentage in 0..100"}
    raw_reaction = parsed.get("reaction")
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
    messages = _prompt_messages(text, context_text, emojis)
    try:
        response = await asyncio.wait_for(
            provider_manager.chat(
                messages,
                temperature=0.3,
                max_tokens=96,
                response_format={"type": "json_object"},
            ),
            timeout=AI_TIMEOUT_S,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        return {"ok": False, "error": "the AI evaluation timed out"}
    except Exception as exc:
        return {"ok": False, "error": f"the AI evaluation failed ({type(exc).__name__})"}

    if not getattr(response, "success", False):
        return {"ok": False, "error": "the AI provider returned no usable answer"}
    return parse_evaluation(getattr(response, "text", ""), emojis)


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

    catalog = await reaction_source_service.load_reactions(client)
    options: list[dict[str, Any]] = []
    if isinstance(catalog, dict):
        options = list(catalog.get("options") or [])
    if not isinstance(catalog, dict) or not catalog.get("ok") or not options:
        detail = catalog.get("detail") if isinstance(catalog, dict) else None
        logger.warning(
            "[INTELLIGENT_REACTION] reaction catalog unavailable chat=%s msg=%s: %s",
            chat_id, msg_id, detail or "no usable reactions",
        )
        return _outcome(STATUS_CATALOG_UNAVAILABLE, error="Telegram's reaction catalog is unavailable")

    emojis = [str(option.get("emoji")) for option in options if option.get("emoji")]
    if not emojis:
        return _outcome(STATUS_CATALOG_UNAVAILABLE, error="Telegram returned no usable reactions")

    # Mark handled BEFORE the AI round trip so a duplicate/edited update for the
    # same message never evaluates or reacts twice.
    _guard_add(key, now)

    context_text = await _reply_context(client, chat_id, message.get("reply_to_msg_id"))

    manager = provider_manager if provider_manager is not None else _default_provider_manager()
    evaluation = await evaluate_message(
        manager, text=text, context_text=context_text, emojis=emojis,
    )
    if not evaluation.get("ok"):
        logger.warning(
            "[INTELLIGENT_REACTION] no reaction chat=%s msg=%s: %s",
            chat_id, msg_id, evaluation.get("error") or "invalid AI answer",
        )
        return _outcome(STATUS_FAILED, error=str(evaluation.get("error") or "invalid AI answer"))

    probability = evaluation["probability"]
    reaction = evaluation.get("reaction")
    if reaction is None:
        return _outcome(STATUS_NO_REACTION, probability=probability,
                        error="the AI chose no reaction")

    draw_fn = random_fn or random.random
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

    if client is None:
        return _outcome(STATUS_FAILED, probability=probability, reaction=reaction,
                        error="the self client is not connected")

    try:
        result = await reaction_service.react_to_message(
            client, owner_id, chat_id, msg_id, reaction,
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
