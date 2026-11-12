"""
Intelligent Reaction engine — fixed owner probability + AI semantic choice.

The owner's persisted percentage decides WHETHER to react; the AI decides ONLY
WHICH Telegram reaction fits the message (or none). The two decisions can never
be confused: the model's answer contract is ``{"reaction": <emoji|null>}``, and
a legacy ``probability`` field makes the whole answer invalid — the configured
percentage is never overridable by the model.

    outgoing handler (existing self-client update path)
      → this service (eligibility → probability → catalog → AI → validate)
        → reaction_service.react_to_message  (existing typed target check)
          → telegram_api.reactions.send_reaction (existing typed wrapper)

Contracts:

* **The percentage is configuration.** It is read through
  ``settings_service.intelligent_reaction_probability()`` for EVERY eligible
  message, so a change applies to the next message with no restart. Exactly one
  independent draw per message: no counter, quota, rolling window, streak,
  carryover or compensation exists here.
* **Telegram is the only reaction source.** The catalog comes from
  ``reaction_source_service`` (``messages.GetAvailableReactionsRequest``). There
  is no static emoji list, no curated fallback and no substitution. The emoji the
  AI chose is validated against the SAME snapshot offered to it; an unavailable
  catalog fails closed (nothing is sent).
* **Bounded context, no identity.** The AI receives the owner's message text, at
  most one replied-to text, a coarse chat-type classification and an explicit
  ``is_reply`` boolean. It never receives chat/message/sender ids, usernames,
  chat titles, media metadata, history or memory.
* **Strict validation, honest outcomes.** Every completed evaluation produces
  exactly one stable outcome code and one bounded log record carrying the status
  only — never message text, identities or full prompts. A Telegram failure is
  ``telegram_send_failed`` and is never reported as ``reacted``.
* **At most one attempt.** One reaction request per eligible message through the
  existing service (which makes one ``SendReactionRequest``). No retry, no second
  client, no second loop, no scheduler, no provider import.
"""
from __future__ import annotations

import asyncio
import json
import logging
import random
import time
from collections import OrderedDict
from typing import Any, Callable

from backend.helper.input_state import get_pending
from backend.services import reaction_service
from backend.services import reaction_source_service
from backend.services import settings_service
from backend.telegram_api.reactions import normalize_emoji

logger = logging.getLogger(__name__)

#: Hard bound on the AI round trip: long enough for a real provider answer,
#: short enough that a stalled provider cannot hold the update path.
AI_TIMEOUT_S = 20.0

#: Bounded context: a reaction is a signal about one message, not a transcript.
MAX_MESSAGE_CHARS = 1000
MAX_REPLIED_CHARS = 500

#: Deduplication registry bound (a re-delivered update must not evaluate twice).
GUARD_MAX = 512
GUARD_TTL_S = 900.0

CHAT_TYPES = ("private", "group", "supergroup", "channel", "unknown")

STATUS_REACTED = "reacted"
STATUS_TELEGRAM_FAILED = "telegram_send_failed"
STATUS_INVALID = "skipped_invalid"
STATUS_NOT_OWNER = "skipped_not_owner_authored"
STATUS_INLINE_ORIGIN = "skipped_inline_origin"
STATUS_PENDING_INPUT = "skipped_pending_input"
STATUS_DUPLICATE = "skipped_duplicate"
STATUS_DISABLED = "skipped_disabled"
STATUS_AUTO_REACTION_ACTIVE = "skipped_auto_reaction_active"
STATUS_NO_TEXT = "skipped_no_text"
STATUS_PROBABILITY_ZERO = "skipped_probability_zero"
STATUS_PROBABILITY_MISS = "skipped_probability_miss"
STATUS_CATALOG_UNAVAILABLE = "skipped_catalog_unavailable"
STATUS_AI_TIMEOUT = "skipped_ai_timeout"
STATUS_AI_FAILURE = "skipped_ai_failure"
STATUS_INVALID_AI_RESPONSE = "skipped_invalid_ai_response"
STATUS_NO_SEMANTIC_REACTION = "skipped_no_semantic_reaction"
STATUS_UNSUPPORTED_REACTION = "skipped_unsupported_reaction"

_handled: "OrderedDict[tuple[int, int], float]" = OrderedDict()


def reset_loop_guard() -> None:
    """Clear the bounded, in-memory deduplication registry (tests/recovery)."""
    _handled.clear()


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


# ── Deterministic gates ──

def _valid_owner(owner_id: Any) -> bool:
    return isinstance(owner_id, int) and not isinstance(owner_id, bool) and owner_id > 0


def _valid_id(value: Any, *, allow_zero: bool = False, allow_negative: bool = False) -> bool:
    if not isinstance(value, int) or isinstance(value, bool):
        return False
    if value == 0:
        return allow_zero
    return value > 0 or allow_negative


def probability_passes(
    percent: Any, *, draw: Callable[[], float] | None = None,
) -> bool:
    """The ONE probability decision for one message.

    ``0`` never passes and ``100`` always passes without consuming a draw, so
    the boundaries are exact and a 0% setting costs no randomness. Any other
    value performs exactly one independent draw in ``[0, 1)`` and passes when it
    lands below ``percent / 100``. An unusable setting fails closed.
    """
    if not isinstance(percent, int) or isinstance(percent, bool):
        return False
    if percent < 0 or percent > 100:
        return False
    if percent == 0:
        return False
    if percent == 100:
        return True
    value = random.random() if draw is None else draw()
    try:
        numeric = float(value)
    except (TypeError, ValueError):
        return False
    return numeric * 100.0 < percent


def classify_chat_type(
    *, is_private: bool = False, is_group: bool = False,
    is_channel: bool = False, is_supergroup: bool = False,
) -> str:
    """Bounded chat classification — never a title, id or member list."""
    if is_private:
        return "private"
    if is_channel:
        return "channel"
    if is_group:
        return "supergroup" if is_supergroup else "group"
    return "unknown"


def normalize_chat_type(value: Any) -> str:
    return value if isinstance(value, str) and value in CHAT_TYPES else "unknown"


def build_context(
    message: dict[str, Any],
    *,
    chat_type: str,
    is_reply: bool,
    replied_text: str = "",
) -> dict[str, Any]:
    """The exact, bounded AI context. No ids, names or metadata cross over."""
    text = message.get("text")
    if not isinstance(text, str):
        text = ""
    replied = replied_text if isinstance(replied_text, str) else ""
    return {
        "message": text[:MAX_MESSAGE_CHARS],
        "chat_type": normalize_chat_type(chat_type),
        "is_reply": bool(is_reply),
        "replied_text": replied[:MAX_REPLIED_CHARS] if is_reply else "",
    }


def build_ai_messages(
    context: dict[str, Any], catalog: tuple[str, ...] | list[str],
) -> list[dict[str, str]]:
    """The one-shot semantic-reaction prompt. Semantic only — no probability."""
    system = (
        "You choose ONE Telegram reaction emoji for the user's own message "
        "below. Reply with ONLY a JSON object: {\"reaction\": \"<emoji>\"} or "
        "{\"reaction\": null}. The emoji MUST be copied exactly from the allowed "
        "list; never invent, modify or combine emoji. Reply null when no "
        "allowed emoji fits. Never return a probability, a confidence, an "
        "explanation or any other field."
    )
    payload = {
        "message": context.get("message", ""),
        "chat_type": context.get("chat_type", "unknown"),
        "is_reply": bool(context.get("is_reply")),
        "replied_text": context.get("replied_text", ""),
        "allowed_reactions": list(catalog),
    }
    return [
        {"role": "system", "content": system},
        {"role": "user", "content": json.dumps(payload, ensure_ascii=False)},
    ]


def _reject_duplicate_keys(pairs: list[tuple[str, Any]]) -> dict[str, Any]:
    """A repeated key is ambiguous input, never a later value winning."""
    result: dict[str, Any] = {}
    for key, value in pairs:
        if key in result:
            raise ValueError(f"duplicate key: {key}")
        result[key] = value
    return result


def parse_ai_reaction(
    raw: Any, catalog: tuple[str, ...] | list[str],
) -> tuple[str | None, str | None]:
    """Strictly parse the AI answer against the SAME catalog snapshot.

    Returns ``(status, emoji)`` where ``status is None`` means "send this emoji"
    and every non-``None`` status means "send nothing". A legacy
    ``{"probability": …, "reaction": …}`` answer is an INVALID response — the
    probability field can never reach the decision path.
    """
    if not isinstance(raw, str) or not raw.strip():
        return STATUS_INVALID_AI_RESPONSE, None
    try:
        data = json.loads(raw.strip(), object_pairs_hook=_reject_duplicate_keys)
    except (TypeError, ValueError):
        return STATUS_INVALID_AI_RESPONSE, None
    if not isinstance(data, dict) or set(data.keys()) != {"reaction"}:
        return STATUS_INVALID_AI_RESPONSE, None
    value = data["reaction"]
    if value is None:
        return STATUS_NO_SEMANTIC_REACTION, None
    if not isinstance(value, str):
        return STATUS_INVALID_AI_RESPONSE, None
    emoji = normalize_emoji(value)
    if emoji is None:
        return STATUS_INVALID_AI_RESPONSE, None
    if emoji not in tuple(catalog):
        return STATUS_UNSUPPORTED_REACTION, emoji
    return None, emoji


async def catalog_snapshot(client: Any) -> tuple[str, ...] | None:
    """The current Telegram reaction catalog, or ``None`` when unavailable.

    Served cache-first by the existing source service (its own TTL and
    ``AvailableReactionsNotModified`` handling are preserved); a failure or an
    empty catalog yields ``None`` and the caller fails closed.
    """
    try:
        result = await reaction_source_service.load_reactions(client)
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("[INTELLIGENT_REACTION] catalog read raised: %s", type(exc).__name__)
        return None
    if not isinstance(result, dict) or not result.get("ok"):
        return None
    emojis: list[str] = []
    for option in result.get("options") or []:
        reaction = option.get("reaction") if isinstance(option, dict) else None
        emoji = normalize_emoji(reaction.get("emoji")) if isinstance(reaction, dict) else None
        if emoji is not None and emoji not in emojis:
            emojis.append(emoji)
    return tuple(emojis) if emojis else None


def _resolve_manager(manager: Any) -> Any:
    if manager is not None:
        return manager
    try:
        from backend.ai.engine.engine import get_engine
        engine = get_engine()
    except Exception:
        return None
    return getattr(engine, "provider_manager", None) if engine is not None else None


def _outcome(
    status: str, *, error: str | None = None, reaction: Any = None,
) -> dict[str, Any]:
    if status == STATUS_REACTED:
        logger.info("[INTELLIGENT_REACTION] outcome=%s", status)
    else:
        logger.info(
            "[INTELLIGENT_REACTION] outcome=%s reason=%s", status, error or "-",
        )
    return {"status": status, "error": error, "reaction": reaction}


async def process_outgoing_message(
    *,
    owner_id: int,
    client: Any,
    message: dict[str, Any],
    chat_type: str = "unknown",
    is_reply: bool = False,
    replied_text: str = "",
    via_bot_id: int | None = None,
    provider_manager: Any = None,
    manager: Any = None,
    random_fn: Callable[[], float] | None = None,
    draw: Callable[[], float] | None = None,
    timeout_s: float = AI_TIMEOUT_S,
) -> dict[str, Any]:
    """Evaluate ONE owner-authored outgoing message and react at most once.

    ``message`` is the ``serialize_message`` representation of the owner's
    outgoing message. ``provider_manager``/``manager`` is the existing
    ProviderManager (resolved from the running engine when omitted);
    ``random_fn``/``draw`` is the independent per-message draw (``random.random``
    when omitted, injectable for tests). Returns ``{"status", "error",
    "reaction"}`` with exactly one stable outcome. Never raises for a condition
    it can describe; cancellation is always re-raised.
    """
    if not _valid_owner(owner_id) or not isinstance(message, dict):
        return _outcome(STATUS_INVALID, error="invalid owner or message")

    chat_id = message.get("chat_id")
    msg_id = message.get("id")
    # Telegram chat ids are negative for groups/channels; message ids are positive.
    if not _valid_id(chat_id, allow_negative=True) or not _valid_id(msg_id):
        return _outcome(STATUS_INVALID, error="message has no usable chat/message id")

    if message.get("out") is not True or message.get("sender_id") != owner_id:
        return _outcome(STATUS_NOT_OWNER, error="message is not owner-authored")

    if via_bot_id is None:
        via_bot_id = message.get("via_bot_id")
    if _valid_id(via_bot_id):
        return _outcome(STATUS_INLINE_ORIGIN, error="message was sent through an inline bot")

    now = time.monotonic()
    key = (chat_id, msg_id)
    if _guard_has(key, now):
        return _outcome(STATUS_DUPLICATE, error="message was already evaluated")

    pending = get_pending(owner_id)
    if isinstance(pending, dict) and pending.get("chat_id") == chat_id:
        return _outcome(STATUS_PENDING_INPUT, error="message is input for a pending panel flow")

    # The settings are read per message, never once at startup.
    if not settings_service.is_intelligent_reaction_enabled():
        return _outcome(STATUS_DISABLED, error="intelligent reaction is disabled")

    # The two reaction modes are mutually exclusive owners of the same message;
    # the fixed Auto-Reaction setting always wins while it is ON.
    if settings_service.is_auto_reaction_enabled():
        return _outcome(STATUS_AUTO_REACTION_ACTIVE, error="auto-reaction is enabled")

    text = message.get("text")
    if not isinstance(text, str) or not text.strip():
        return _outcome(STATUS_NO_TEXT, error="message has no text to evaluate")

    percent = settings_service.intelligent_reaction_probability()
    if percent <= 0:
        # 0% must cost nothing: no catalog read and no AI call.
        return _outcome(STATUS_PROBABILITY_ZERO, error="probability is 0%")
    if not probability_passes(percent, draw=draw or random_fn):
        return _outcome(STATUS_PROBABILITY_MISS, error="probability draw did not pass")

    if client is None:
        return _outcome(STATUS_CATALOG_UNAVAILABLE, error="the self client is not connected")

    catalog = await catalog_snapshot(client)
    if not catalog:
        return _outcome(STATUS_CATALOG_UNAVAILABLE, error="telegram catalog unavailable")

    # Marked handled before the AI call so a duplicate update cannot evaluate
    # the same message twice while the first evaluation is in flight.
    _guard_add(key, now)

    manager_obj = _resolve_manager(manager if manager is not None else provider_manager)
    if manager_obj is None:
        return _outcome(STATUS_AI_FAILURE, error="the AI engine is not running")

    context = build_context(
        message, chat_type=chat_type, is_reply=is_reply, replied_text=replied_text,
    )
    messages = build_ai_messages(context, catalog)
    try:
        response = await asyncio.wait_for(
            manager_obj.chat(
                messages,
                temperature=0.3,
                max_tokens=64,
                response_format={"type": "json_object"},
            ),
            timeout=timeout_s,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        return _outcome(STATUS_AI_TIMEOUT, error=f"provider did not answer in {timeout_s:g}s")
    except Exception as exc:
        logger.warning("[INTELLIGENT_REACTION] provider call raised: %s", type(exc).__name__)
        return _outcome(STATUS_AI_FAILURE, error="provider call failed")

    if not getattr(response, "success", False):
        return _outcome(STATUS_AI_FAILURE, error="provider returned no successful response")

    status, emoji = parse_ai_reaction(getattr(response, "text", ""), catalog)
    if status is not None:
        return _outcome(
            status,
            error="ai response rejected" if emoji is None else "emoji not in catalog",
        )

    reaction = {"kind": "emoji", "emoji": emoji}
    try:
        result = await reaction_service.react_to_message(
            client, owner_id, chat_id, msg_id, reaction,
        )
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("[INTELLIGENT_REACTION] send raised: %s", type(exc).__name__)
        return _outcome(STATUS_TELEGRAM_FAILED, error="reaction send raised", reaction=reaction)

    if not isinstance(result, dict) or not result.get("ok"):
        detail = result.get("detail") if isinstance(result, dict) else None
        logger.warning(
            "[INTELLIGENT_REACTION] telegram rejected the reaction: %s",
            detail or "unknown error",
        )
        return _outcome(
            STATUS_TELEGRAM_FAILED, error="telegram rejected the reaction",
            reaction=reaction,
        )
    return _outcome(STATUS_REACTED, reaction=result.get("reaction", reaction))
