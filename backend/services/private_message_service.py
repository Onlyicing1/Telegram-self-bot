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
* **No new table.** State is ONE JSON document in the existing ``bot_settings``
  key/value row (the store ``ghost_seen_v2`` already uses), with the documented
  in-memory fallback when Supabase is absent.

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
import threading
import time
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

POLICY_IGNORE = "ignore"
POLICY_DELETE = "delete"
POLICY_AUTOREPLY = "autoreply"
POLICIES: tuple[str, ...] = (POLICY_IGNORE, POLICY_DELETE, POLICY_AUTOREPLY)

ACTION_ALLOW = "allow"

SOURCE_MANUAL = "manual"
SOURCE_TEMPORARY = "temporary"
SOURCE_SCHEDULE = "schedule"

MODE_LABELS: dict[str, str] = {
    MODE_OPEN: "Open (everyone)",
    MODE_WHITELIST_ONLY: "Whitelist only",
    MODE_CLOSED: "Closed (exceptions only)",
}

POLICY_LABELS: dict[str, str] = {
    POLICY_IGNORE: "Ignore (do nothing)",
    POLICY_DELETE: "Delete the message",
    POLICY_AUTOREPLY: "Auto-reply once (rate limited)",
}

SOURCE_LABELS: dict[str, str] = {
    SOURCE_MANUAL: "manual mode",
    SOURCE_TEMPORARY: "temporary override",
    SOURCE_SCHEDULE: "schedule",
}

WEEKDAY_LABELS: tuple[str, ...] = ("Mon", "Tue", "Wed", "Thu", "Fri", "Sat", "Sun")

#: One automatic answer per unauthorized sender per window. The mapping stays
#: bounded by the distinct senders inside the window (pruned on every decision).
AUTOREPLY_COOLDOWN_SECONDS = 300.0
MAX_AUTOREPLY_LENGTH = 512
MAX_LIST_ENTRIES = 200
MAX_SCHEDULE_RULES = 12
_TRANSITION_HORIZON_DAYS = 8
_STATE_VERSION = 1

#: The ``bot_settings`` row this feature owns (created on first write, like the
#: Ghost Seen allow-list row — deliberately not seeded by the canonical schema).
STATE_ROW_KEY = "private_message_management"

#: Every database round trip is bounded so a slow/unreachable store can never
#: stall the Telegram event path (the load is awaited once, on first use).
DB_TIMEOUT_SECONDS = 5.0


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
        "policy": POLICY_IGNORE,
        "autoreply_text": "",
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
    if raw.get("policy") in POLICIES:
        state["policy"] = str(raw["policy"])
    if isinstance(raw.get("enabled"), bool):
        state["enabled"] = raw["enabled"]
    text = raw.get("autoreply_text")
    if isinstance(text, str):
        state["autoreply_text"] = text.strip()[:MAX_AUTOREPLY_LENGTH]
    state["allowlist"] = _sanitize_list(raw.get("allowlist"))
    state["exceptions"] = _sanitize_list(raw.get("exceptions"))
    state["temporary"] = _sanitize_temporary(raw.get("temporary"))
    state["schedule"] = _sanitize_schedule(raw.get("schedule"), state["schedule"]["timezone"])
    return state


# ── In-memory cache + persistence ──

_state: dict[str, Any] = _default_state()
_loaded = False
_load_task: asyncio.Task | None = None
_load_task_loop: Any = None
_state_lock: asyncio.Lock | None = None
_state_lock_loop: Any = None
_persist_lock = threading.Lock()
_locally_mutated = False
_autoreply_slots: dict[int, float] = {}


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
    _autoreply_slots.clear()


async def _read_state_row() -> str | None:
    from backend.db.client import get_db

    db = get_db()
    if db is None:
        return None

    def _read_sync() -> Any:
        row = (
            db.table("bot_settings")
            .select("value")
            .eq("key", STATE_ROW_KEY)
            .maybe_single()
            .execute()
        )
        return getattr(row, "data", None)

    data = await asyncio.wait_for(asyncio.to_thread(_read_sync), timeout=DB_TIMEOUT_SECONDS)
    if isinstance(data, dict):
        value = data.get("value")
        if isinstance(value, str) and value.strip():
            return value
    return None


async def _load_task_body() -> None:
    global _loaded
    _loaded = True
    try:
        raw = await _read_state_row()
    except asyncio.CancelledError:
        raise
    except Exception as exc:
        logger.warning("private_message: state load failed: %s", exc)
        return
    if raw is None:
        return
    try:
        loaded = sanitize_state(json.loads(raw))
    except Exception as exc:
        logger.warning("private_message: stored state is not usable JSON: %s", exc)
        return
    async with _mutation_lock():
        # A local change made while the read was in flight has already been
        # written, so the stored value it produced is newer than this snapshot.
        if _locally_mutated:
            return
        _state.clear()
        _state.update(loaded)


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
    from backend.db.client import get_db

    db = get_db()
    if db is None:
        # No database: the change applies to this process only. The panel shows
        # that explicitly instead of claiming durable storage.
        logger.warning("private_message: no database — state kept in memory only")
        return True

    def _write_sync() -> None:
        with _persist_lock:
            value = json.dumps(candidate, sort_keys=True)
            existing = (
                db.table("bot_settings").select("key").eq("key", STATE_ROW_KEY).execute()
            )
            rows = getattr(existing, "data", None) or []
            if rows:
                db.table("bot_settings").update({"value": value}).eq("key", STATE_ROW_KEY).execute()
            else:
                db.table("bot_settings").insert({
                    "key": STATE_ROW_KEY,
                    "value": value,
                    "value_type": "str",
                }).execute()

    await asyncio.wait_for(asyncio.to_thread(_write_sync), timeout=DB_TIMEOUT_SECONDS)
    return True


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
    permitted: bool
    mode: str
    source: str
    action: str
    autoreply_text: str = ""
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
    handlers untouched.
    """
    policy = resolve_policy(state_doc, now)
    if not is_private:
        return IncomingDecision(True, policy.mode, policy.source, ACTION_ALLOW, reason="not a private chat")
    if is_owner:
        return IncomingDecision(True, policy.mode, policy.source, ACTION_ALLOW, reason="owner")
    if not policy.enforced:
        return IncomingDecision(True, policy.mode, policy.source, ACTION_ALLOW, reason="feature disabled")
    if policy.mode == MODE_OPEN:
        return IncomingDecision(True, policy.mode, policy.source, ACTION_ALLOW, reason="open mode")
    if is_authorized(state_doc, sender_id, policy.mode):
        return IncomingDecision(True, policy.mode, policy.source, ACTION_ALLOW, reason="authorized sender")

    configured = str(state_doc.get("policy") or POLICY_IGNORE)
    text = str(state_doc.get("autoreply_text") or "").strip()
    if configured == POLICY_AUTOREPLY and text:
        action = POLICY_AUTOREPLY
    elif configured == POLICY_DELETE:
        action = POLICY_DELETE
    else:
        # An auto-reply with no text configured falls back to ignoring: an
        # empty message is never sent to anyone.
        action = POLICY_IGNORE
    return IncomingDecision(
        False, policy.mode, policy.source, action, autoreply_text=text, reason="unauthorized sender"
    )


def consume_autoreply_slot(sender_id: int, monotonic_now: float | None = None) -> bool:
    """Whether this unauthorized sender may receive an auto-reply right now."""
    now = time.monotonic() if monotonic_now is None else monotonic_now
    cutoff = now - AUTOREPLY_COOLDOWN_SECONDS
    for key in [key for key, seen in _autoreply_slots.items() if seen < cutoff]:
        _autoreply_slots.pop(key, None)
    if _autoreply_slots.get(sender_id, 0.0) > cutoff:
        return False
    _autoreply_slots[sender_id] = now
    return True


def autoreply_seconds_remaining(sender_id: int, monotonic_now: float | None = None) -> int:
    now = time.monotonic() if monotonic_now is None else monotonic_now
    seen = _autoreply_slots.get(sender_id)
    if seen is None:
        return 0
    return max(0, int(AUTOREPLY_COOLDOWN_SECONDS - (now - seen)))


# ── Mutations (validated, persisted) ──


async def set_enabled(value: bool) -> bool:
    return await _mutate(lambda candidate: candidate.__setitem__("enabled", bool(value)))


async def set_mode(mode: str) -> bool:
    if mode not in MODES:
        return False
    return await _mutate(lambda candidate: candidate.__setitem__("mode", mode))


async def set_policy(policy: str) -> bool:
    if policy not in POLICIES:
        return False
    return await _mutate(lambda candidate: candidate.__setitem__("policy", policy))


async def set_autoreply_text(text: str) -> bool:
    cleaned = (text or "").strip()
    if not cleaned or len(cleaned) > MAX_AUTOREPLY_LENGTH:
        return False
    return await _mutate(lambda candidate: candidate.__setitem__("autoreply_text", cleaned))


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
    policy: str
    autoreply_text: str
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
        policy=str(state_doc.get("policy") or POLICY_IGNORE),
        autoreply_text=str(state_doc.get("autoreply_text") or ""),
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
