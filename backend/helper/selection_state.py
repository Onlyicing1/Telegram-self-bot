"""Per-owner multi-select state for the Saved Items (Stash) browser.

Multi-select is UI state, never persisted data: one bounded, insertion-ordered
mapping of save code → the exact label the browser presented for that item.
The save CODE is the only identity an action ever uses, so a bulk operation
cannot touch an item the owner did not tick.

Deliberately synchronous and process-local, like ``input_state``: the single
owner bot has one browser at a time, every read/write happens on the event
loop (nothing here awaits), and the store is bounded (``MAX_SELECTION``) and
expires on its own (``SELECTION_TTL_SECONDS``) so an abandoned panel cannot
keep stale codes alive. ``reset``/``clear`` return the codes they dropped so a
caller can report honestly what a cancellation discarded.
"""
from __future__ import annotations

import logging
import time

logger = logging.getLogger(__name__)

#: Hard bound on one selection: a bulk action processes at most this many
#: items, mirroring ``retrieve_service.MAX_BULK_ITEMS``.
MAX_SELECTION = 40

#: How long an untouched selection survives. Browsing spans pages, so this is
#: deliberately much longer than the 120 s pending-input expiry.
SELECTION_TTL_SECONDS = 900.0

_sessions: dict[int, dict] = {}


def _now() -> float:
    return time.monotonic()


def _owner_key(owner_id) -> int | None:
    try:
        return int(owner_id)
    except (TypeError, ValueError):
        return None


def _entry(owner_id, *, create: bool = False) -> dict | None:
    owner = _owner_key(owner_id)
    if owner is None:
        return None
    entry = _sessions.get(owner)
    if entry is not None and _now() - entry["touched"] > SELECTION_TTL_SECONDS:
        _sessions.pop(owner, None)
        entry = None
    if entry is None and create:
        entry = {"codes": {}, "page": 1, "touched": _now()}
        _sessions[owner] = entry
    return entry


def normalize_code(value) -> str:
    """The canonical comparison form of a save code (never destructive)."""
    return str(value or "").strip().upper()


def is_selected(owner_id, code: str) -> bool:
    entry = _entry(owner_id)
    return bool(entry and normalize_code(code) in entry["codes"])


def toggle(owner_id, code: str, label: str = "") -> tuple[bool, str]:
    """Toggle one exact code.

    Returns ``(selected_now, refusal_reason)``. A refusal reason is only set
    when the code could not be stored (empty code or the cap is reached); the
    caller reports it instead of pretending the tick happened.
    """
    normalized = normalize_code(code)
    if not normalized:
        return False, "no saved item was named"
    entry = _entry(owner_id, create=True)
    if entry is None:
        return False, "the selection could not be stored"
    codes = entry["codes"]
    if normalized in codes:
        codes.pop(normalized)
        entry["touched"] = _now()
        return False, ""
    if len(codes) >= MAX_SELECTION:
        return False, f"at most {MAX_SELECTION} items can be selected at once"
    codes[normalized] = str(label or normalized)
    entry["touched"] = _now()
    return True, ""


def selected(owner_id) -> tuple[tuple[str, str], ...]:
    """The selected ``(code, label)`` pairs, in the order they were ticked."""
    entry = _entry(owner_id)
    if not entry:
        return ()
    return tuple(entry["codes"].items())


def codes(owner_id) -> tuple[str, ...]:
    return tuple(code for code, _label in selected(owner_id))


def count(owner_id) -> int:
    return len(selected(owner_id))


def clear(owner_id) -> tuple[str, ...]:
    """Drop this owner's selection (and its page) and return the dropped codes."""
    owner = _owner_key(owner_id)
    if owner is None:
        return ()
    entry = _sessions.pop(owner, None)
    if entry is None:
        return ()
    return tuple(entry["codes"])


def set_page(owner_id, page) -> int:
    """Remember the page the selection view is on (1 is the safe default)."""
    try:
        value = max(1, int(page))
    except (TypeError, ValueError):
        value = 1
    entry = _entry(owner_id, create=True)
    if entry is not None:
        entry["page"] = value
        entry["touched"] = _now()
    return value


def current_page(owner_id) -> int:
    entry = _entry(owner_id)
    return int(entry["page"]) if entry else 1


def clear_all() -> None:
    """Forget every owner's selection (shutdown/test hygiene)."""
    _sessions.clear()
