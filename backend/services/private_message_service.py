"""Private Message Management — application-side policy for incoming private messages.

What this module owns
---------------------
Whether ONE incoming **private** message is authorized, and what the owner
configured the Self Bot to do with the ones that are not. The decision is a
pure function of (now, persisted state), so it is evaluated on the existing
Telegram update path and rendered by the panel without any second loop.

Boundaries kept on purpose
--------------------------
* **Application-side only.** Nothing here changes the account's Telegram
  privacy settings, and nothing here can stop a third party from *sending* a
  message. ``closed`` / ``whitelist_only`` describe what the Self Bot does with
  a message Telegram has already delivered — delivery is never blocked at the
  transport level, and this module never claims otherwise.
* **No Telegram I/O.** The actual send/delete/answer happens in
  ``backend/bot/handlers/private_message.py``; this module only produces the
  decision.
* **No second scheduler.** A mode is a pure function of time, so a schedule
  boundary needs no ticker: there is nothing to change at a boundary except how
  the *next* message is handled. Recomputing on every message and every panel
  render is also what makes a restart correct without replaying missed ticks.
* **Dedicated persistence.** The state lives in the feature's own tables
  (``private_message_config`` / ``private_message_senders``,
  DATABASE_ARCHITECTURE.md §33) instead of the generic ``bot_settings`` JSON
  document. A document written by Phase 1 is still understood and is imported
  into the dedicated row on first read (no configured rule is ever discarded).
* **Hard scope.** The scope gate (:func:`classify_scope`) admits ONLY genuine
  human users in direct private chats with the owner's account: a bot, a
  group, a supergroup, a channel, a linked discussion group, an unresolved or
  malformed entity and any contradictory event are rejected BEFORE any action
  (delete, fixed response, AI response, counter claim, enforcement record).
  No configuration can widen it.
* **Independent actions.** Deleting the message, sending the configured fixed
  response and generating an AI response are three separately enabled actions,
  executed in that fixed order; enabling one never enables another.
* **No second AI runtime.** The AI response is generated through the existing
  shared ``ProviderManager`` (``tools=[]``), like the durable-task preparation
  path, and receives NOTHING but the incoming message text. Both required
  disclosures are plain-Unicode application text appended after generation, so
  the model can neither remove nor override them.
* **Deterministic reply envelope.** The model's own body is validated before it
  can be delivered (:func:`validate_generated_reply`): empty, over-long,
  letter-less, non-Persian-script, Latin-dominant and clearly Arabic bodies are
  refused, and an echoed notice line is de-duplicated so each disclosure lands
  exactly once. The verdict is computed from the GENERATED text alone — never
  from the incoming message — and a refused body is dropped, never repaired.
* **Durable allowance.** The per-person AI reply limit and interval are
  enforced by ONE conditional (CAS) write per reply into
  ``private_message_senders`` (with the documented in-memory fallback when
  Supabase is absent, which keeps enforcing the SAME quota). Counters are never
  reset implicitly — a restart cannot hand anyone a fresh allowance.

Precedence (deterministic, and shown in the panel)
--------------------------------------------------
``temporary`` (while not expired) beats ``schedule`` (when enabled and a rule
matches the local wall clock) beats the manually stored ``mode``. The master
``enabled`` switch is not a mode: OFF means the feature performs no policy
action at all, and every incoming message is handled exactly as before it
existed.
"""
from __future__ import annotations

import asyncio
import copy
import json
import logging
import os
import time
import unicodedata
from collections import OrderedDict
from dataclasses import dataclass
from datetime import date, datetime, time as clock_time, timedelta, timezone
from typing import Any, Callable
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

logger = logging.getLogger(__name__)

# ── Vocabulary ──

MODE_OPEN = "open"
MODE_WHITELIST_ONLY = "whitelist_only"
MODE_CLOSED = "closed"
MODES: tuple[str, ...] = (MODE_OPEN, MODE_WHITELIST_ONLY, MODE_CLOSED)

#: The three INDEPENDENT closing actions. Each has its own enable flag, its own
#: control and its own execution step; enabling one never enables another.
ACTION_DELETE = "delete_incoming"
ACTION_FIXED = "fixed_response_enabled"
ACTION_AI = "ai_response_enabled"
ACTION_FIELDS: tuple[str, ...] = (ACTION_DELETE, ACTION_FIXED, ACTION_AI)

ACTION_LABELS: dict[str, str] = {
    ACTION_DELETE: "Delete incoming message",
    ACTION_FIXED: "Send fixed response",
    ACTION_AI: "Send AI response",
}

#: Legacy Phase-1 mutually exclusive policy values. They are read ONLY to import
#: an existing document into the independent flags (backfill), never again as a
#: control surface.
LEGACY_POLICY_IGNORE = "ignore"
LEGACY_POLICY_DELETE = "delete"
LEGACY_POLICY_AUTOREPLY = "autoreply"

# ── Scope verdicts (the hard execution boundary) ──

SCOPE_ELIGIBLE = "eligible"
SCOPE_NOT_INCOMING = "not_incoming"
SCOPE_GROUP = "group_conversation"
SCOPE_CHANNEL = "channel_conversation"
SCOPE_CHAT_UNKNOWN = "chat_type_unknown"
SCOPE_NOT_PRIVATE = "not_private_chat"
SCOPE_INCONSISTENT = "inconsistent_event"
SCOPE_CHAT_UNRESOLVED = "chat_unresolved"
SCOPE_SENDER_UNRESOLVED = "sender_unresolved"
SCOPE_SENDER_NOT_USER = "sender_not_a_user"
SCOPE_SENDER_TYPE_UNKNOWN = "sender_type_unknown"
SCOPE_BOT = "bot_sender"
SCOPE_DELETED = "deleted_sender"
SCOPE_SELF = "own_account_chat"

SOURCE_MANUAL = "manual"
SOURCE_TEMPORARY = "temporary"
SOURCE_SCHEDULE = "schedule"

MODE_LABELS: dict[str, str] = {
    MODE_OPEN: "Open (everyone)",
    MODE_WHITELIST_ONLY: "Whitelist only",
    MODE_CLOSED: "Closed (exceptions only)",
}

SOURCE_LABELS: dict[str, str] = {
    SOURCE_MANUAL: "manual mode",
    SOURCE_TEMPORARY: "temporary override",
    SOURCE_SCHEDULE: "schedule",
}

WEEKDAY_LABELS: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

MAX_FIXED_RESPONSE_LENGTH = 512
MAX_LIST_ENTRIES = 200
MAX_SCHEDULE_RULES = 12
_TRANSITION_HORIZON_DAYS = 8
_STATE_VERSION = 2

#: Per-person AI allowance bounds (the panel validates against these and the
#: dedicated table's constraints mirror them).
MAX_AI_REPLIES_PER_PERSON = 10000
MAX_AI_INTERVAL_SECONDS = 86400

#: Bounded generation envelope for ONE AI reply, and Telegram's practical text
#: ceiling: the disclosures are appended by the application, so a reply that
#: would not fit is REFUSED instead of being truncated.
AI_REPLY_TIMEOUT_S = 60.0
MAX_DELIVERED_TEXT_LENGTH = 4000

#: The legacy Phase-1 ``bot_settings`` row. It is READ ONLY, once, to import an
#: existing configuration into the dedicated tables; the feature never writes it
#: again (nothing is deleted either — the document stays as the owner left it).
LEGACY_STATE_ROW_KEY = "private_message_management"

#: The two required disclosures. Application-controlled plain Unicode text —
#: appended by :func:`compose_ai_reply` AFTER generation, so the model can never
#: remove, weaken or override them.
CLOSED_NOTICE = "این پی‌وی در حال حاضر بسته است."
AI_NOTICE = "این پیام توسط هوش مصنوعی تولید و ارسال شده است."

#: Deterministic pre-send envelope for the model's OWN reply body (the two
#: disclosures excluded). Anything longer is not the configured "short reply"
#: and is REFUSED — never truncated — so an over-long answer takes the ordinary
#: safe-failure path (nothing is sent, the claimed reply is given back).
MAX_AI_GENERATED_LENGTH = 1200

#: Script evidence for the Persian-vs-Arabic check. Persian and Arabic share
#: most of their code points, so the check NEVER rejects on a shared letter: it
#: looks for the letters/forms and the vocabulary each language alone uses, and
#: classifies a body as Arabic only when it carries Arabic evidence AND no
#: Persian evidence at all (:func:`is_clearly_arabic`). A false NEGATIVE
#: (Arabic text that passes) is tolerated; a false POSITIVE (valid Persian
#: refused, leaving the person unanswered) is what these lists are shaped to
#: avoid.
_PERSIAN_ONLY_LETTERS = frozenset("پچژگ")
#: Persian's kaf/yeh (U+06A9 / U+06CC). Arabic orthography writes U+0643 /
#: U+064A instead, so either form is direct evidence the body is Persian.
_PERSIAN_FORMS = frozenset("کی")
#: The Arabic forms of those two letters, plus teh marbuta and alef maksura —
#: forms Persian orthography does not use.
_ARABIC_FORMS = frozenset("كيةى")
#: Persian function words, matched as WHOLE tokens (Persian writes them
#: separately). Exonerating only: a hit stops the Arabic verdict.
_PERSIAN_TOKENS = frozenset({
    "را", "که", "این", "از", "شما", "هست", "نیست", "چی", "یه", "دارم",
    "دارید", "خوبی", "خوبم", "سلامت", "باشه", "حتما", "کنم", "کنید",
    "کنیم", "بگو", "رسید", "پاسخ", "ممنون", "مرسی", "چطور", "چقدر",
})
#: Morphology Persian attaches to a stem ("میتونم", "نمی‌دونم"). Spelled with
#: Persian letters, so Arabic orthography can never match them.
_PERSIAN_TOKEN_PREFIXES = ("می", "نمی", "چطور", "ممنون")
#: The Persian copula as a token SUFFIX ("کجاست", "چطوراست"). Arabic words
#: containing this sequence ("استقلال") do not END with it.
_PERSIAN_COPULA_SUFFIX = "است"
#: Hamza-bearing alef forms folded onto the plain alef for token matching, so
#: an Arabic word spelled with `أ`/`إ`/`آ` still matches its vocabulary entry.
_ALEF_FOLD = {
    "\u0622": "\u0627", "\u0623": "\u0627", "\u0625": "\u0627", "\u0671": "\u0627",
}
#: Arabic-only vocabulary, matched as WHOLE tokens (diacritics and tatweel are
#: removed first). An Arabic sentence almost always carries two of these; a
#: Persian sentence written in Persian orthography carries none of them.
_ARABIC_TOKENS = frozenset({
    "في", "على", "هذا", "هذه", "ذلك", "التي", "الذي", "لكن", "انا",
    "انت", "كان", "يكون", "كيف", "حالك", "اهلا", "مرحبا", "شكرا", "بك",
    "لكم", "السلام", "عليكم", "وعليكم", "ماذا", "كثيرا", "ايضا", "هو",
    "هي",
})

#: Bounded duplicate-update guard: an exact (chat, message) pair is enforced
#: once, so a Telegram redelivery can never delete twice, answer twice or spend
#: two allowances.
_HANDLED_MAX = 512
_HANDLED_TTL_S = 900.0
_handled: "OrderedDict[tuple[int, int], float]" = OrderedDict()

#: One lock per (owner, sender) serialises the enforcement of ONE person, so two
#: concurrent messages from the same person cannot run two generations or spend
#: one allowance twice.
_LOCKS_MAX = 512
_locks: dict[tuple[int, int], asyncio.Lock] = {}

#: Set by ``configure_owner`` (the handler's ``register``); the fallback reads
#: the runtime's configured owner so a service call outside registration still
#: knows whose row it is writing.
_owner: int | None = None




# ── State shape ──


def default_timezone() -> str:
    """The runtime timezone, using the same env contract as the rest of the bot."""
    tz_str = os.getenv("TZ") or "Asia/Tehran"
    try:
        ZoneInfo(tz_str)
        return tz_str
    except (ZoneInfoNotFoundError, ValueError, Exception):
        return "UTC"


def resolve_zone(name: Any):
    """A ZoneInfo for *name*, or None when it is not a usable IANA name."""
    if not isinstance(name, str) or not name.strip():
        return None
    try:
        return ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError, Exception):
        return None


def _default_state() -> dict[str, Any]:
    return {
        "version": _STATE_VERSION,
        "enabled": True,
        "mode": MODE_OPEN,
        "delete_incoming": False,
        "fixed_response_enabled": False,
        "fixed_response_text": "",
        "ai_response_enabled": False,
        "ai_max_replies_per_person": 10,
        "ai_unlimited_replies": False,
        "ai_interval_seconds": 0,
        "allowlist": [],
        "exceptions": [],
        "temporary": None,
        "schedule": {
            "enabled": False,
            "timezone": default_timezone(),
            "rules": [],
        },
    }


def parse_instant(value: Any) -> datetime | None:
    """A timezone-aware datetime from stored ISO-8601 text (or None)."""
    if isinstance(value, datetime):
        return value if value.tzinfo else value.replace(tzinfo=timezone.utc)
    if not isinstance(value, str) or not value.strip():
        return None
    try:
        parsed = datetime.fromisoformat(value.strip().replace("Z", "+00:00"))
    except ValueError:
        return None
    return parsed if parsed.tzinfo else parsed.replace(tzinfo=timezone.utc)


def parse_hhmm(value: Any) -> clock_time | None:
    """A 24-hour wall-clock time from strict ``HH:MM`` text (or None)."""
    if not isinstance(value, str):
        return None
    parts = value.strip().split(":")
    if len(parts) != 2 or len(parts[0]) != 2 or len(parts[1]) != 2:
        return None
    if not (parts[0].isdigit() and parts[1].isdigit()):
        return None
    hour, minute = int(parts[0]), int(parts[1])
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        return None
    return clock_time(hour, minute)


def format_hhmm(value: clock_time) -> str:
    return f"{value.hour:02d}:{value.minute:02d}"


def _sanitize_list(value: Any) -> list[int]:
    """Positive, unique user ids — never a name, phone number or profile field."""
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    seen: set[int] = set()
    result: list[int] = []
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int) or item <= 0:
            continue
        if item in seen:
            continue
        seen.add(item)
        result.append(item)
        if len(result) >= MAX_LIST_ENTRIES:
            break
    return sorted(result)


def _sanitize_days(value: Any) -> list[int]:
    if not isinstance(value, (list, tuple, set, frozenset)):
        return []
    days = set()
    for item in value:
        if isinstance(item, bool) or not isinstance(item, int):
            continue
        if 0 <= item <= 6:
            days.add(item)
    return sorted(days)


def _sanitize_rule(raw: Any, index: int) -> dict[str, Any] | None:
    """One canonical schedule rule, or None when the input is not usable."""
    if not isinstance(raw, dict):
        return None
    start = parse_hhmm(raw.get("start"))
    end = parse_hhmm(raw.get("end"))
    mode = raw.get("mode")
    days = _sanitize_days(raw.get("days"))
    if start is None or end is None or start == end or mode not in MODES or not days:
        return None
    rule_id = raw.get("id")
    if not isinstance(rule_id, str) or not rule_id.strip():
        rule_id = f"rule-{index + 1}"
    return {
        "id": rule_id.strip()[:32],
        "days": days,
        "start": format_hhmm(start),
        "end": format_hhmm(end),
        "mode": str(mode),
    }


def _sanitize_schedule(raw: Any, fallback_timezone: str) -> dict[str, Any]:
    if not isinstance(raw, dict):
        return {"enabled": False, "timezone": fallback_timezone, "rules": []}
    timezone_name = raw.get("timezone")
    if resolve_zone(timezone_name) is None:
        timezone_name = fallback_timezone
    rules: list[dict[str, Any]] = []
    for index, rule in enumerate(raw.get("rules") or []):
        if len(rules) >= MAX_SCHEDULE_RULES:
            break
        cleaned = _sanitize_rule(rule, index)
        if cleaned is not None:
            rules.append(cleaned)
    return {
        "enabled": bool(raw.get("enabled", False)),
        "timezone": str(timezone_name),
        "rules": rules,
    }


def _sanitize_temporary(raw: Any) -> dict[str, Any] | None:
    if not isinstance(raw, dict):
        return None
    mode = raw.get("mode")
    expires_at = parse_instant(raw.get("expires_at"))
    if mode not in MODES or expires_at is None:
        return None
    activated_at = parse_instant(raw.get("activated_at")) or expires_at
    return {
        "mode": str(mode),
        "activated_at": activated_at.isoformat(),
        "expires_at": expires_at.isoformat(),
    }


def sanitize_state(raw: Any) -> dict[str, Any]:
    """Build a canonical state from arbitrary stored JSON.

    Every field is validated independently: a malformed value falls back to its
    safe default instead of poisoning the whole document, and an absent document
    is the backward-compatible default (open, no policy action).
    """
    state = _default_state()
    if not isinstance(raw, dict):
        return state
    if raw.get("mode") in MODES:
        state["mode"] = str(raw["mode"])
    if isinstance(raw.get("enabled"), bool):
        state["enabled"] = raw["enabled"]
    for field_name in ACTION_FIELDS:
        if isinstance(raw.get(field_name), bool):
            state[field_name] = raw[field_name]
    text = raw.get("fixed_response_text")
    if isinstance(text, str):
        state["fixed_response_text"] = text.strip()[:MAX_FIXED_RESPONSE_LENGTH]
    limit = raw.get("ai_max_replies_per_person")
    if isinstance(limit, int) and not isinstance(limit, bool) and 1 <= limit <= MAX_AI_REPLIES_PER_PERSON:
        state["ai_max_replies_per_person"] = limit
    if isinstance(raw.get("ai_unlimited_replies"), bool):
        state["ai_unlimited_replies"] = raw["ai_unlimited_replies"]
    interval = raw.get("ai_interval_seconds")
    if isinstance(interval, int) and not isinstance(interval, bool) and 0 <= interval <= MAX_AI_INTERVAL_SECONDS:
        state["ai_interval_seconds"] = interval
    state["allowlist"] = _sanitize_list(raw.get("allowlist"))
    state["exceptions"] = _sanitize_list(raw.get("exceptions"))
    state["temporary"] = _sanitize_temporary(raw.get("temporary"))
    state["schedule"] = _sanitize_schedule(raw.get("schedule"), state["schedule"]["timezone"])
    _import_legacy_actions(state, raw)
    return state


def _import_legacy_actions(state: dict[str, Any], raw: dict[str, Any]) -> None:
    """Backfill from a Phase-1 document, whose single ``policy`` value chose ONE
    action for every unauthorized sender.

    The mapping preserves what the owner configured: ``delete`` enables the
    delete action, ``autoreply`` enables the fixed response with its text
    (including the empty-text case, which Phase 1 itself treated as ignore) and
    ``ignore`` enables nothing. An explicit Phase-2 flag always wins, so a
    document that has already been migrated is never re-interpreted.
    """
    if any(field_name in raw for field_name in ACTION_FIELDS):
        return
    legacy = raw.get("policy")
    if legacy == LEGACY_POLICY_DELETE:
        state[ACTION_DELETE] = True
    elif legacy == LEGACY_POLICY_AUTOREPLY:
        text = raw.get("autoreply_text")
        if isinstance(text, str) and text.strip():
            state[ACTION_FIXED] = True
            state["fixed_response_text"] = text.strip()[:MAX_FIXED_RESPONSE_LENGTH]


# ── Scope (the hard execution boundary) ──


@dataclass(frozen=True)
class ScopeVerdict:
    eligible: bool
    reason: str
    chat_id: int = 0
    sender_id: int = 0
    message_id: int = 0

    @property
    def identity(self) -> tuple[int, int]:
        return (self.chat_id, self.message_id)


def _valid_owner(owner_id: Any) -> bool:
    """A real Telegram user id — never a bool, never zero/negative."""
    return isinstance(owner_id, int) and not isinstance(owner_id, bool) and owner_id > 0


def _positive_id(value: Any) -> int | None:
    """A positive Telegram id, or None (bools and non-ints are never ids)."""
    if isinstance(value, int) and not isinstance(value, bool) and value > 0:
        return value
    return None


def _bot_flag(entity: Any) -> bool | None:
    """Telegram's authoritative bot flag as a real bool, or None when the
    entity carries no usable value — an unknown entity is never assumed human."""
    flag = getattr(entity, "bot", None)
    return flag if isinstance(flag, bool) else None


def classify_scope(
    *,
    owner_id: Any,
    is_private: Any,
    is_group: Any,
    is_channel: Any,
    chat_id: Any,
    sender_id: Any,
    message_id: Any,
    peer_user_id: Any,
    chat_is_user: bool,
    sender_is_user: bool,
    chat_entity: Any = None,
    sender_entity: Any = None,
) -> ScopeVerdict:
    """The ONE scope gate: is this event a genuine human private message?

    Both halves are proven independently and must agree — the chat half from the
    event's peer type plus the resolved chat entity, the sender half from the
    resolved sender entity's own bot flag. ``is_private`` alone is NOT enough:
    a private chat with a BOT is a private chat, and a linked discussion group
    or a megagroup is a ``PeerChannel``. Everything unresolved, malformed or
    contradictory fails closed, and the caller performs NO action whatsoever on
    a rejection (no delete, no response, no AI request, no counter, no record).
    """
    if not _valid_owner(owner_id):
        return ScopeVerdict(False, SCOPE_INCONSISTENT)
    if is_group is True:
        return ScopeVerdict(False, SCOPE_GROUP)
    if is_channel is True:
        return ScopeVerdict(False, SCOPE_CHANNEL)
    if is_private is not True or is_group is not False or is_channel is not False:
        return ScopeVerdict(False, SCOPE_CHAT_UNKNOWN)

    chat = _positive_id(chat_id)
    sender = _positive_id(sender_id)
    message = _positive_id(message_id)
    peer = _positive_id(peer_user_id)
    if chat is None or sender is None or message is None or peer is None:
        return ScopeVerdict(False, SCOPE_INCONSISTENT)
    if not (peer == chat == sender):
        return ScopeVerdict(False, SCOPE_INCONSISTENT)
    if chat == owner_id:
        return ScopeVerdict(False, SCOPE_SELF)
    if not chat_is_user:
        return ScopeVerdict(False, SCOPE_NOT_PRIVATE)
    if chat_entity is None:
        return ScopeVerdict(False, SCOPE_CHAT_UNRESOLVED)
    if sender_entity is None:
        return ScopeVerdict(False, SCOPE_SENDER_UNRESOLVED)
    if not sender_is_user:
        return ScopeVerdict(False, SCOPE_SENDER_NOT_USER)
    if _positive_id(getattr(chat_entity, "id", None)) != chat:
        return ScopeVerdict(False, SCOPE_INCONSISTENT)
    if _positive_id(getattr(sender_entity, "id", None)) != chat:
        return ScopeVerdict(False, SCOPE_INCONSISTENT)
    for entity in (chat_entity, sender_entity):
        flag = _bot_flag(entity)
        if flag is None:
            return ScopeVerdict(False, SCOPE_SENDER_TYPE_UNKNOWN)
        if flag:
            return ScopeVerdict(False, SCOPE_BOT)
        if getattr(entity, "deleted", False) is True:
            return ScopeVerdict(False, SCOPE_DELETED)
    return ScopeVerdict(True, SCOPE_ELIGIBLE, chat, sender, message)


# ── In-memory cache + persistence ──

_state: dict[str, Any] = _default_state()
_loaded = False
_load_task: asyncio.Task | None = None
_load_task_loop: Any = None
_state_lock: asyncio.Lock | None = None
_state_lock_loop: Any = None
_locally_mutated = False


def _mutation_lock() -> asyncio.Lock:
    """The serialization lock for this event loop.

    Bound to the RUNNING loop rather than created once at import: the module is
    process-global, while a loop is not (tests, and any rebuild that installs a
    new loop). One lock per loop keeps mutations of ONE process serialized
    without ever handing a coroutine a lock owned by a different loop.
    """
    global _state_lock, _state_lock_loop
    loop = asyncio.get_running_loop()
    if _state_lock is None or _state_lock_loop is not loop:
        _state_lock = asyncio.Lock()
        _state_lock_loop = loop
    return _state_lock


def state() -> dict[str, Any]:
    """A copy of the current state. Never performs I/O."""
    return copy.deepcopy(_state)


def persistence_available() -> bool:
    """False when the runtime has no database (state is process-local)."""
    try:
        from backend.db.client import get_db
        return get_db() is not None
    except Exception:
        return False


def reset_in_memory() -> None:
    """Drop all in-memory state (tests, and a full runtime rebuild)."""
    global _loaded, _load_task, _load_task_loop, _locally_mutated
    _state.clear()
    _state.update(_default_state())
    _loaded = False
    _load_task = None
    _load_task_loop = None
    _locally_mutated = False
    _handled.clear()
    _locks.clear()


def configure_owner(owner_id: Any) -> None:
    """Bind the owner this process serves (called by the handler's register)."""
    global _owner
    _owner = owner_id if _valid_owner(owner_id) else None


def owner_id() -> int | None:
    """The owner whose row the feature reads/writes, or None when unknown."""
    if _owner is not None:
        return _owner
    try:
        from backend.helper.inline_engine import get_owner_id
        value = get_owner_id()
    except Exception:
        return None
    return value if _valid_owner(value) else None


async def _read_state_document() -> tuple[dict[str, Any] | None, bool]:
    """``(document, is_legacy)`` for this owner.

    The DEDICATED row is authoritative. Only when it does not exist yet is the
    legacy Phase-1 ``bot_settings`` document read — once — as the backfill
    source; :func:`sanitize_state` maps its single ``policy`` value onto the
    independent flags, so an existing configuration is imported, never lost.
    """
    from backend.db import client as db_client

    current = owner_id()
    if current is None:
        return None, False
    row = await db_client.get_private_message_config(current)
    if isinstance(row, dict):
        return row, False
    legacy = await db_client.get_legacy_private_message_state(current)
    if not isinstance(legacy, str) or not legacy.strip():
        return None, False
    try:
        parsed = json.loads(legacy)
    except Exception as exc:
        logger.warning("private_message: legacy configuration is not usable JSON: %s", exc)
        return None, False
    if not isinstance(parsed, dict):
        return None, False
    logger.info(
        "private_message: importing the legacy settings document into the dedicated tables"
    )
    return parsed, True


async def _load_task_body() -> None:
    global _loaded, _locally_mutated
    _loaded = True
    try:
        raw, is_legacy = await _read_state_document()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("private_message: state load failed: %s", exc)
        return
    if raw is None:
        return
    loaded = sanitize_state(raw)
    async with _mutation_lock():
        # A local change made while the read was in flight has already been
        # written, so the stored value it produced is newer than this snapshot.
        if _locally_mutated:
            return
        _state.clear()
        _state.update(loaded)
    if is_legacy:
        # One-time backfill: the imported configuration is written to the
        # dedicated row, so the legacy document is read at most once and the
        # feature is durable from here on. A failed write keeps the imported
        # state in memory and the read is retried on the next start.
        try:
            await _write_state_row(loaded)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("private_message: legacy import write failed: %s", exc)


async def ensure_loaded_async() -> None:
    """Load the persisted state exactly once; concurrent callers share the read.

    Called by the panel render path and by the incoming-message gate, so a
    restart resolves the persisted policy on FIRST USE instead of relying on a
    startup race: an unauthorized message arriving during startup must not be
    judged against the in-memory default.
    """
    global _load_task, _load_task_loop
    loop = asyncio.get_running_loop()
    if _load_task is None or _load_task.done() or _load_task_loop is not loop:
        if _loaded and _load_task_loop is loop:
            return
        _load_task = asyncio.ensure_future(_load_task_body())
        _load_task_loop = loop
    await asyncio.shield(_load_task)


async def reload_async() -> None:
    """Force a fresh read of the persisted state (the panel's Refresh action)."""
    global _load_task, _loaded, _locally_mutated
    _load_task = None
    _loaded = False
    _locally_mutated = False
    await ensure_loaded_async()


async def _write_state_row(candidate: dict[str, Any]) -> bool:
    """Persist the whole configuration document into the dedicated table.

    ``False`` means the durable write did not land; :func:`_mutate` then leaves
    the in-memory state untouched, so memory and storage can never disagree.
    """
    from backend.db import client as db_client

    current = owner_id()
    if current is None:
        logger.warning("private_message: no owner configured — state kept in memory only")
        return True
    if not persistence_available():
        # No database: the change applies to this process only. The panel shows
        # that explicitly instead of claiming durable storage.
        logger.warning("private_message: no database — state kept in memory only")
        return True
    return await db_client.save_private_message_config(current, candidate)


async def _mutate(mutator: Callable[[dict[str, Any]], None]) -> bool:
    """Validate + persist a change, and keep it only when the write succeeded.

    The candidate is built and validated as a COPY: a failed write leaves the
    in-memory state exactly as it was, so memory and storage cannot conflict.
    """
    global _locally_mutated
    async with _mutation_lock():
        candidate = sanitize_state(copy.deepcopy(_state))
        try:
            mutator(candidate)
        except Exception as exc:
            logger.warning("private_message: rejected mutation: %s", exc)
            return False
        candidate = sanitize_state(candidate)
        try:
            persisted = await _write_state_row(candidate)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning("private_message: persistence failed: %s", exc)
            return False
        if not persisted:
            return False
        _state.clear()
        _state.update(candidate)
        _locally_mutated = True
        return True


# ── Effective policy (pure) ──


@dataclass(frozen=True)
class EffectivePolicy:
    mode: str
    source: str
    enforced: bool
    expires_at: datetime | None = None
    next_transition_at: datetime | None = None
    next_transition_label: str = ""

    def remaining_seconds(self, now: datetime) -> int | None:
        if self.expires_at is None:
            return None
        return max(0, int((self.expires_at - now).total_seconds()))


def rule_matches(rule: dict[str, Any], local_now: datetime) -> bool:
    """Whether *rule* covers the given local wall-clock instant.

    A range whose end is at or before its start spans midnight and belongs to
    the day it STARTS on: it covers ``[start, 24:00)`` of every listed day plus
    ``[00:00, end)`` of the following morning.
    """
    start = parse_hhmm(rule.get("start"))
    end = parse_hhmm(rule.get("end"))
    if start is None or end is None or start == end:
        return False
    days = set(_sanitize_days(rule.get("days")))
    if not days:
        return False
    weekday = local_now.weekday()
    current = local_now.time().replace(tzinfo=None)
    if start < end:
        return weekday in days and start <= current < end
    if weekday in days and current >= start:
        return True
    return ((weekday - 1) % 7) in days and current < end


def schedule_mode(schedule: dict[str, Any], now: datetime) -> str | None:
    """The mode the schedule applies at *now*, or None when it applies none.

    When several rules match, the LAST matching rule wins — rules keep the
    owner's order (the panel shows that order), so a later rule deliberately
    overrides an earlier one.
    """
    if not isinstance(schedule, dict) or not schedule.get("enabled"):
        return None
    zone = resolve_zone(schedule.get("timezone"))
    local_now = now.astimezone(zone) if zone is not None else now.astimezone(timezone.utc)
    matched: str | None = None
    for rule in schedule.get("rules") or []:
        if isinstance(rule, dict) and rule_matches(rule, local_now):
            mode = rule.get("mode")
            if mode in MODES:
                matched = str(mode)
    return matched


def temporary_mode(state_doc: dict[str, Any], now: datetime) -> dict[str, Any] | None:
    """The ACTIVE temporary override, or None (absent, malformed or expired)."""
    temporary = state_doc.get("temporary")
    if not isinstance(temporary, dict):
        return None
    expires_at = parse_instant(temporary.get("expires_at"))
    if expires_at is None or expires_at <= now:
        return None
    if temporary.get("mode") not in MODES:
        return None
    return temporary


def resolve_mode(state_doc: dict[str, Any], now: datetime) -> tuple[str, str]:
    """The mode that applies at *now* and which of the three sources set it."""
    temporary = temporary_mode(state_doc, now)
    if temporary is not None:
        return str(temporary["mode"]), SOURCE_TEMPORARY
    scheduled = schedule_mode(state_doc.get("schedule") or {}, now)
    if scheduled is not None:
        return scheduled, SOURCE_SCHEDULE
    return str(state_doc.get("mode") or MODE_OPEN), SOURCE_MANUAL


def _boundary_candidates(schedule: dict[str, Any], now: datetime) -> list[tuple[datetime, str]]:
    """Every rule boundary in a bounded window around *now* (schedule tz)."""
    zone = resolve_zone(schedule.get("timezone")) or timezone.utc
    local_today = now.astimezone(zone).date()
    candidates: list[tuple[datetime, str]] = []
    for offset in range(-1, _TRANSITION_HORIZON_DAYS):
        day: date = local_today + timedelta(days=offset)
        for index, rule in enumerate(schedule.get("rules") or []):
            if not isinstance(rule, dict):
                continue
            for key in ("start", "end"):
                boundary = parse_hhmm(rule.get(key))
                if boundary is None:
                    continue
                instant = datetime.combine(day, boundary, tzinfo=zone).astimezone(timezone.utc)
                candidates.append((instant, f"rule {index + 1} {key}"))
    return candidates


def next_transition(state_doc: dict[str, Any], now: datetime) -> tuple[datetime | None, str]:
    """The next instant at which the EFFECTIVE mode changes, and what it becomes.

    Every candidate boundary is confirmed against the effective mode on either
    side of it, so a boundary that changes nothing (e.g. a schedule transition
    hidden under an active temporary override) is never reported.
    """
    if not state_doc.get("enabled", True):
        return None, ""

    # (instant, priority, label): priority 0 is the temporary override's own
    # expiry, so when a schedule boundary lands on the exact same second the
    # reported cause is the one that actually ends the override.
    candidates: list[tuple[datetime, int, str]] = []
    temporary = state_doc.get("temporary")
    if isinstance(temporary, dict):
        expires_at = parse_instant(temporary.get("expires_at"))
        if expires_at is not None and expires_at > now:
            candidates.append((expires_at, 0, "temporary override ends"))
    schedule = state_doc.get("schedule") or {}
    if schedule.get("enabled") and schedule.get("rules"):
        candidates.extend(
            (instant, 1, label) for instant, label in _boundary_candidates(schedule, now)
        )

    for instant, _priority, label in sorted(candidates):
        if instant <= now:
            continue
        before, _ = resolve_mode(state_doc, instant - timedelta(seconds=1))
        after, _ = resolve_mode(state_doc, instant + timedelta(seconds=1))
        if before == after:
            continue
        return instant, f"{label} → {MODE_LABELS.get(after, after)}"
    return None, ""


def resolve_policy(state_doc: dict[str, Any], now: datetime) -> EffectivePolicy:
    """The effective mode, its source, its expiry and the next boundary."""
    mode, source = resolve_mode(state_doc, now)
    temporary = temporary_mode(state_doc, now) if source == SOURCE_TEMPORARY else None
    next_at, next_label = next_transition(state_doc, now)
    return EffectivePolicy(
        mode=mode,
        source=source,
        enforced=bool(state_doc.get("enabled", True)),
        expires_at=parse_instant(temporary.get("expires_at")) if temporary else None,
        next_transition_at=next_at,
        next_transition_label=next_label,
    )


# ── Authorization + decision (pure) ──


def is_authorized(state_doc: dict[str, Any], sender_id: Any, mode: str) -> bool:
    """Whether *sender_id* may bypass the restricted policy for *mode*.

    ``whitelist_only`` consults the allow-list and ``closed`` consults the
    explicit exceptions (per the documented model); ``open`` restricts nobody.
    """
    if isinstance(sender_id, bool) or not isinstance(sender_id, int) or sender_id <= 0:
        return False
    if mode == MODE_WHITELIST_ONLY:
        return sender_id in set(state_doc.get("allowlist") or [])
    if mode == MODE_CLOSED:
        return sender_id in set(state_doc.get("exceptions") or [])
    return True


@dataclass(frozen=True)
class IncomingDecision:
    """What the owner configured for ONE incoming private message.

    The three actions are NOT alternatives: each one is present exactly when its
    own flag is enabled, and the handler executes them in the documented order
    (delete, then the fixed response, then the AI response).
    """

    permitted: bool
    mode: str
    source: str
    delete: bool = False
    fixed_response: bool = False
    fixed_response_text: str = ""
    ai_response: bool = False
    reason: str = ""


def decide_incoming(
    state_doc: dict[str, Any],
    *,
    sender_id: Any,
    is_private: bool,
    is_owner: bool,
    now: datetime,
) -> IncomingDecision:
    """What to do with one incoming message — the whole authorization rule.

    Non-private chats and the owner's own messages are always permitted: this
    feature manages *incoming private* messages only, so the owner can still be
    answered by the bot and the owner's replies keep reaching the existing
    handlers untouched. Every rejection by the scope gate happens BEFORE this
    function is consulted (:func:`classify_scope`), so a bot, group, channel or
    unverifiable event never reaches an action at all.
    """
    policy = resolve_policy(state_doc, now)
    if not is_private:
        return IncomingDecision(True, policy.mode, policy.source, reason="not a private chat")
    if is_owner:
        return IncomingDecision(True, policy.mode, policy.source, reason="owner")
    if not policy.enforced:
        return IncomingDecision(True, policy.mode, policy.source, reason="feature disabled")
    if policy.mode == MODE_OPEN:
        return IncomingDecision(True, policy.mode, policy.source, reason="open mode")
    if is_authorized(state_doc, sender_id, policy.mode):
        return IncomingDecision(True, policy.mode, policy.source, reason="authorized sender")

    text = str(state_doc.get("fixed_response_text") or "").strip()
    fixed_enabled = bool(state_doc.get(ACTION_FIXED)) and bool(text)
    return IncomingDecision(
        False,
        policy.mode,
        policy.source,
        delete=bool(state_doc.get(ACTION_DELETE)),
        fixed_response=fixed_enabled,
        # An enabled fixed response with no configured text has nothing to send,
        # so the panel's text input is the only way to make it reachable.
        fixed_response_text=text if fixed_enabled else "",
        ai_response=bool(state_doc.get(ACTION_AI)),
        reason="unauthorized sender",
    )


# ── Durable per-person allowance (one CAS claim per AI reply) ──


def _sender_lock(owner: int, sender_id: int) -> asyncio.Lock:
    key = (owner, sender_id)
    lock = _locks.get(key)
    if lock is None:
        while len(_locks) >= _LOCKS_MAX:
            _locks.pop(next(iter(_locks)))
        lock = asyncio.Lock()
        _locks[key] = lock
    return lock


def already_handled(chat_id: int, message_id: int, monotonic_now: float | None = None) -> bool:
    """True when this exact (chat, message) pair was already enforced.

    A Telegram redelivery of the SAME update must never delete twice, answer
    twice or spend two allowances; the registry is bounded and time-limited.
    """
    now = time.monotonic() if monotonic_now is None else monotonic_now
    for stale in [key for key, stamp in _handled.items() if now - stamp > _HANDLED_TTL_S]:
        _handled.pop(stale, None)
    key = (chat_id, message_id)
    if key in _handled:
        _handled[key] = now
        return True
    _handled[key] = now
    while len(_handled) > _HANDLED_MAX:
        _handled.popitem(last=False)
    return False


def release_handled(chat_id: int, message_id: int) -> bool:
    """Forget ONE (chat, message) mark, returning whether one was removed.

    Used when the enforcement of that message failed BEFORE its actions ran: a
    message the feature never actually handled must not be remembered as done,
    so the redelivery of it may still be enforced (and is then bounded by the
    ordinary quota/interval rules).
    """
    return _handled.pop((chat_id, message_id), None) is not None


async def claim_ai_reply(owner: int, sender_id: int, config: dict[str, Any]) -> dict[str, Any]:
    """Claim ONE AI reply from this person's durable allowance.

    The claim is ONE atomic conditional write performed BEFORE the model is
    asked anything, so concurrent/duplicate updates can never spend the same
    reply. The returned envelope is honest: ``ok=False`` means the store could
    not answer and the caller must send nothing (fail closed), ``claimed=False``
    means the configured limit or interval vetoed it.
    """
    from backend.db import client as db_client

    now_iso = datetime.now(timezone.utc).isoformat()
    return await db_client.claim_private_message_ai_reply(
        owner,
        sender_id,
        max_replies=int(config.get("ai_max_replies_per_person") or 0),
        unlimited=bool(config.get("ai_unlimited_replies")),
        interval_seconds=int(config.get("ai_interval_seconds") or 0),
        now_iso=now_iso,
    )


async def release_ai_reply(owner: int, sender_id: int, token: Any) -> bool:
    """Give a claimed reply back when generation or delivery failed, so a reply
    that was never delivered can never consume the person's allowance."""
    from backend.db import client as db_client

    return await db_client.release_private_message_ai_reply(owner, sender_id, token)


async def reset_counters(owner: int) -> bool:
    """The owner's explicit reset — the ONLY edge that clears the counters.

    Counters never reset implicitly (no TTL, no restart, no configuration
    change), so a process restart can never hand anyone a fresh allowance.
    """
    from backend.db import client as db_client

    if not _valid_owner(owner):
        return False
    return await db_client.reset_private_message_counters(owner)


async def counter_rows(owner: int) -> list[dict[str, Any]] | None:
    """Bounded per-person counter rows (newest first), or None when the store
    could not be read — an unreadable store is never shown as an empty one."""
    from backend.db import client as db_client

    if not _valid_owner(owner):
        return None
    return await db_client.list_private_message_senders(owner)


def _is_arabic_script(ch: str) -> bool:
    code = ord(ch)
    return (
        0x0600 <= code <= 0x06FF      # Arabic + the Persian letter extensions
        or 0x0750 <= code <= 0x077F   # Arabic Supplement
        or 0xFB50 <= code <= 0xFDFF   # Arabic Presentation Forms-A
        or 0xFE70 <= code <= 0xFEFF   # Arabic Presentation Forms-B
    )


def _letters(text: str) -> list[str]:
    """Every LETTER of the text — digits, punctuation, emoji and combining
    marks are not letters and never count as language evidence."""
    return [ch for ch in text if unicodedata.category(ch).startswith("L")]


def _arabic_script_letters(letters: list[str]) -> list[str]:
    return [ch for ch in letters if _is_arabic_script(ch)]


def _latin_letters(letters: list[str]) -> list[str]:
    return [ch for ch in letters if "LATIN" in unicodedata.name(ch, "")]


def _tokens(text: str) -> set[str]:
    """The Arabic-script WORD tokens of the text.

    Combining marks (harakat) and the tatweel are treated as separators, the
    hamza-bearing alef forms are folded onto the plain alef (`أنا` == `انا` for
    vocabulary matching), and a token is a maximal run of Arabic-script letters
    — so a Persian word never matches an Arabic-only word from inside, and a
    joined prefix ("میکنم") or suffix ("کجاست") still leaves a clean token.
    """
    cleaned = "".join(
        " "
        if unicodedata.combining(ch) or ch == "\u0640"
        else _ALEF_FOLD.get(ch, ch)
        for ch in text
    )
    tokens: set[str] = set()
    current: list[str] = []
    for ch in cleaned:
        if _is_arabic_script(ch):
            current.append(ch)
        else:
            if current:
                tokens.add("".join(current))
                current = []
    if current:
        tokens.add("".join(current))
    return tokens


def _arabic_tokens(text: str) -> set[str]:
    return {token for token in _tokens(text) if token in _ARABIC_TOKENS}


def _persian_evidence(text: str) -> bool:
    """True when the text could plausibly be Persian.

    Any single hit is enough: the check exists to STOP the Arabic verdict, so
    the safe failure direction is "this might be Persian, accept it".
    """
    if any(ch in _PERSIAN_ONLY_LETTERS or ch in _PERSIAN_FORMS for ch in text):
        return True
    for token in _tokens(text):
        if token in _PERSIAN_TOKENS:
            return True
        if token.startswith(_PERSIAN_TOKEN_PREFIXES):
            return True
        if token.endswith(_PERSIAN_COPULA_SUFFIX):
            return True
    return False


def is_clearly_arabic(text: str) -> bool:
    """True only when the text carries Arabic evidence and NO Persian evidence.

    A bounded, deterministic heuristic — NOT language detection: `سلام` alone
    shares every code point with Arabic and is never classified as Arabic here,
    and any Persian letter form, Persian function word or Persian-only letter
    stops the verdict outright. It deliberately errs towards accepting text, so
    some Arabic sentences pass; that is documented, not hidden.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    if _persian_evidence(text):
        return False
    arabic = _arabic_script_letters(_letters(text))
    if not arabic:
        return False
    arabic_forms = sum(1 for ch in arabic if ch in _ARABIC_FORMS)
    tokens = _arabic_tokens(text)
    if len(tokens) >= 2:
        return True
    if tokens and arabic_forms:
        return True
    return arabic_forms * 2 >= len(arabic)


def _strip_owned_notices(text: str) -> tuple[str, bool]:
    """Remove the application-owned notice LINES from a model body.

    The two notices belong to the application: when a model echoes one, it is
    dropped here (deterministically, line-exact) so the delivered reply carries
    each of them exactly once. Only those known lines are removed — the model's
    own words are never rewritten, translated or invented.
    """
    dropped = False
    kept: list[str] = []
    for line in text.splitlines():
        if line.strip() in (CLOSED_NOTICE, AI_NOTICE):
            dropped = True
            continue
        kept.append(line)
    return "\n".join(kept).strip(), dropped


def validate_generated_reply(generated: Any) -> tuple[str, str]:
    """The deterministic pre-send verdict for ONE model reply body.

    Returns ``(reason, body)``; an empty reason means the body may be delivered.
    The checks, in order: a non-empty plain string, the application-owned notice
    lines removed, a length inside the short-reply envelope, actual letters
    present, Persian/Arabic script present (an English answer is refused), not
    Latin-dominant, and not clearly Arabic. The incoming text is NEVER consulted
    — the required output language is always Persian, whatever arrived — and a
    refused body is dropped, never repaired into a fabricated reply.
    """
    if not isinstance(generated, str) or not generated.strip():
        return "empty", ""
    text, notices_removed = _strip_owned_notices(generated)
    if notices_removed:
        logger.info(
            "private_message: the model echoed an application-owned notice — removed before composing"
        )
    if not text:
        return "notices_only", ""
    if len(text) > MAX_AI_GENERATED_LENGTH:
        return "too_long", ""
    letters = _letters(text)
    if not letters:
        return "no_letters", ""
    script = _arabic_script_letters(letters)
    if not script:
        return "not_persian_script", ""
    if len(_latin_letters(letters)) > len(script):
        return "not_persian_script", ""
    if is_clearly_arabic(text):
        return "arabic", ""
    return "", text


def compose_ai_reply(generated: Any) -> str | None:
    """The generated text PLUS both required disclosures, or None when the
    result is not deliverable (see :func:`validate_generated_reply`).

    The disclosures are appended here, after generation, as
    application-controlled plain Unicode: the model can never remove, weaken or
    override them, and a body that already echoes them is de-duplicated so each
    notice appears EXACTLY once. An over-long reply is refused rather than
    truncated.
    """
    reason, text = validate_generated_reply(generated)
    if reason:
        logger.warning(
            "PRIVATE_MESSAGE_TRACE stage=ai_reply_rejected reason=%s", reason
        )
        return None
    body = f"{text}\n\n{CLOSED_NOTICE}\n{AI_NOTICE}"
    if len(body) > MAX_DELIVERED_TEXT_LENGTH:
        logger.warning(
            "private_message: AI reply refused — %d chars exceeds the delivery ceiling", len(body)
        )
        return None
    return body


def sender_lock(owner: int, sender_id: int) -> asyncio.Lock:
    """The per-person lock serialising ONE person's AI enforcement."""
    return _sender_lock(owner, sender_id)


# ── Mutations (validated, persisted) ──


async def set_enabled(value: bool) -> bool:
    return await _mutate(lambda candidate: candidate.__setitem__("enabled", bool(value)))


async def set_mode(mode: str) -> bool:
    if mode not in MODES:
        return False
    return await _mutate(lambda candidate: candidate.__setitem__("mode", mode))


async def set_action(field_name: str, value: bool) -> bool:
    """Enable/disable ONE action. Independent by construction: this writes a
    single flag and never touches another action's flag."""
    if field_name not in ACTION_FIELDS or not isinstance(value, bool):
        return False
    return await _mutate(lambda candidate: candidate.__setitem__(field_name, value))


async def set_fixed_response_text(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) > MAX_FIXED_RESPONSE_LENGTH:
        return False
    return await _mutate(
        lambda candidate: candidate.__setitem__("fixed_response_text", cleaned)
    )


async def set_ai_max_replies_per_person(value: Any) -> bool:
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    if not (1 <= value <= MAX_AI_REPLIES_PER_PERSON):
        return False
    return await _mutate(
        lambda candidate: candidate.__setitem__("ai_max_replies_per_person", value)
    )


async def set_ai_unlimited_replies(value: bool) -> bool:
    if not isinstance(value, bool):
        return False
    return await _mutate(
        lambda candidate: candidate.__setitem__("ai_unlimited_replies", value)
    )


async def set_ai_interval_seconds(value: Any) -> bool:
    """Minimum seconds between two AI replies to the SAME person (0 = no delay)."""
    if isinstance(value, bool) or not isinstance(value, int):
        return False
    if not (0 <= value <= MAX_AI_INTERVAL_SECONDS):
        return False
    return await _mutate(
        lambda candidate: candidate.__setitem__("ai_interval_seconds", value)
    )


async def add_list_entry(field_name: str, user_id: Any) -> bool:
    if field_name not in ("allowlist", "exceptions"):
        return False
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return False
    if len(_state.get(field_name) or []) >= MAX_LIST_ENTRIES:
        return False

    def _apply(candidate: dict[str, Any]) -> None:
        entries = _sanitize_list(candidate.get(field_name))
        if user_id not in entries:
            entries.append(user_id)
        candidate[field_name] = entries[:MAX_LIST_ENTRIES]

    return await _mutate(_apply)


async def remove_list_entry(field_name: str, user_id: Any) -> bool:
    if field_name not in ("allowlist", "exceptions"):
        return False
    if isinstance(user_id, bool) or not isinstance(user_id, int) or user_id <= 0:
        return False

    def _apply(candidate: dict[str, Any]) -> None:
        candidate[field_name] = [
            entry for entry in _sanitize_list(candidate.get(field_name)) if entry != user_id
        ]

    return await _mutate(_apply)


async def set_temporary(mode: str, minutes: int) -> bool:
    """Start a temporary override that expires on its own (UTC wall clock)."""
    if mode not in MODES:
        return False
    if isinstance(minutes, bool) or not isinstance(minutes, int) or not (1 <= minutes <= 7 * 24 * 60):
        return False
    now = datetime.now(timezone.utc)

    def _apply(candidate: dict[str, Any]) -> None:
        candidate["temporary"] = {
            "mode": mode,
            "activated_at": now.isoformat(),
            "expires_at": (now + timedelta(minutes=minutes)).isoformat(),
        }

    return await _mutate(_apply)


async def clear_temporary() -> bool:
    return await _mutate(lambda candidate: candidate.__setitem__("temporary", None))


async def drop_expired_temporary(now: datetime | None = None) -> bool:
    """Persist the removal of an override that has already expired."""
    moment = now or datetime.now(timezone.utc)
    if temporary_mode(_state, moment) is not None:
        return False
    temporary = _state.get("temporary")
    if not isinstance(temporary, dict):
        return False
    expires_at = parse_instant(temporary.get("expires_at"))
    if expires_at is None or expires_at > moment:
        return False
    return await _mutate(lambda candidate: candidate.__setitem__("temporary", None))


async def set_schedule_enabled(value: bool) -> bool:
    def _apply(candidate: dict[str, Any]) -> None:
        candidate["schedule"]["enabled"] = bool(value)

    return await _mutate(_apply)


async def set_schedule_timezone(name: str) -> bool:
    if resolve_zone(name) is None:
        return False

    def _apply(candidate: dict[str, Any]) -> None:
        candidate["schedule"]["timezone"] = name.strip()

    return await _mutate(_apply)


async def save_schedule_rule(rule: dict[str, Any], index: int | None = None) -> bool:
    """Validate ONE rule and store it: replace an existing index, else append."""
    rules = _state["schedule"]["rules"]
    is_edit = isinstance(index, int) and 0 <= index < len(rules)
    if not is_edit and len(rules) >= MAX_SCHEDULE_RULES:
        return False
    cleaned = _sanitize_rule(rule, index if is_edit else len(rules))
    if cleaned is None:
        return False

    def _apply(candidate: dict[str, Any]) -> None:
        stored = list(candidate["schedule"]["rules"])
        if is_edit:
            cleaned["id"] = stored[index].get("id") or cleaned["id"]
            stored[index] = cleaned
        else:
            stored.append(cleaned)
        candidate["schedule"]["rules"] = stored

    return await _mutate(_apply)


async def delete_schedule_rule(index: int) -> bool:
    if isinstance(index, bool) or not isinstance(index, int) or index < 0:
        return False
    if index >= len(_state["schedule"]["rules"]):
        return False

    def _apply(candidate: dict[str, Any]) -> None:
        stored = list(candidate["schedule"]["rules"])
        if 0 <= index < len(stored):
            del stored[index]
        candidate["schedule"]["rules"] = stored

    return await _mutate(_apply)


# ── Panel-facing summary + formatting ──


@dataclass(frozen=True)
class StatusSummary:
    enabled: bool
    mode: str
    source: str
    delete_incoming: bool
    fixed_response_enabled: bool
    fixed_response_text: str
    ai_response_enabled: bool
    ai_max_replies_per_person: int
    ai_unlimited_replies: bool
    ai_interval_seconds: int
    allowlist: tuple[int, ...] = ()
    exceptions: tuple[int, ...] = ()
    temporary: dict[str, Any] | None = None
    temporary_remaining_seconds: int | None = None
    schedule_enabled: bool = False
    schedule_timezone: str = "UTC"
    schedule_rules: tuple[dict[str, Any], ...] = ()
    next_transition_at: datetime | None = None
    next_transition_label: str = ""


def summarize(state_doc: dict[str, Any], now: datetime) -> StatusSummary:
    policy = resolve_policy(state_doc, now)
    schedule = state_doc.get("schedule") or {}
    return StatusSummary(
        enabled=bool(state_doc.get("enabled", True)),
        mode=policy.mode,
        source=policy.source,
        delete_incoming=bool(state_doc.get(ACTION_DELETE)),
        fixed_response_enabled=bool(state_doc.get(ACTION_FIXED)),
        fixed_response_text=str(state_doc.get("fixed_response_text") or ""),
        ai_response_enabled=bool(state_doc.get(ACTION_AI)),
        ai_max_replies_per_person=int(state_doc.get("ai_max_replies_per_person") or 0),
        ai_unlimited_replies=bool(state_doc.get("ai_unlimited_replies")),
        ai_interval_seconds=int(state_doc.get("ai_interval_seconds") or 0),
        allowlist=tuple(state_doc.get("allowlist") or []),
        exceptions=tuple(state_doc.get("exceptions") or []),
        temporary=temporary_mode(state_doc, now),
        temporary_remaining_seconds=policy.remaining_seconds(now),
        schedule_enabled=bool(schedule.get("enabled")),
        schedule_timezone=str(schedule.get("timezone") or "UTC"),
        schedule_rules=tuple(
            dict(rule) for rule in (schedule.get("rules") or []) if isinstance(rule, dict)
        ),
        next_transition_at=policy.next_transition_at,
        next_transition_label=policy.next_transition_label,
    )


def format_remaining(seconds: int | None) -> str:
    if seconds is None:
        return "—"
    if seconds <= 0:
        return "expiring now"
    hours, remainder = divmod(int(seconds), 3600)
    minutes, secs = divmod(remainder, 60)
    if hours:
        return f"{hours}h {minutes:02d}m"
    if minutes:
        return f"{minutes}m {secs:02d}s"
    return f"{secs}s"


def format_instant(value: datetime | None, timezone_name: str = "") -> str:
    if value is None:
        return "—"
    zone = resolve_zone(timezone_name)
    local = value.astimezone(zone) if zone is not None else value.astimezone(timezone.utc)
    return local.strftime("%Y-%m-%d %H:%M %Z")


def describe_days(days: Any) -> str:
    values = _sanitize_days(days)
    if not values:
        return "—"
    if values == [0, 1, 2, 3, 4]:
        return "Mon–Fri"
    if values == list(range(7)):
        return "Every day"
    if values == [5, 6]:
        return "Sat–Sun"
    return " ".join(WEEKDAY_LABELS[day] for day in values)


def describe_rule(rule: dict[str, Any]) -> str:
    mode = MODE_LABELS.get(str(rule.get("mode")), str(rule.get("mode")))
    return f"{describe_days(rule.get('days'))} {rule.get('start')}–{rule.get('end')} → {mode}"
