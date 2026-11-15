"""Per-owner, per-feature multi-select state for Glass UI lists.

Multi-select is UI state, never persisted data: one bounded, insertion-ordered
mapping of an exact item identity to the label the browser presented for it.
The identity — a save code, a durable task id — is the only value an action
ever uses, so a bulk operation cannot touch an item the owner did not tick.

The store is deliberately synchronous and process-local, like
``input_state``: the single owner bot has one browser at a time, every
read/write happens on the event loop (nothing here awaits), and each
owner/feature scope is bounded (``MAX_SELECTION``) and expires on its own
(``SELECTION_TTL_SECONDS``) so an abandoned panel cannot keep stale identities
alive. ``reset``/``clear`` return the identities they dropped so a caller can
report honestly what a cancellation discarded.

Scopes are feature-isolated: the existing Stash calls use ``DEFAULT_SCOPE``
and keep their original signatures, while another list passes its own scope
(for example ``"taskloom"``). A selection in one scope is never visible to,
cleared by, or applied to another.
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

#: The scope Stash has always used. Callers that existed before scopes did not
#: pass one, so this default is the backward-compatible behaviour.
DEFAULT_SCOPE = "stash"

_sessions: dict[tuple[int, str], dict] = {}


def _now() -> float:
    return time.monotonic()


def _owner_key(owner_id) -> int | None:
    try:
        return int(owner_id)
    except (TypeError, ValueError):
        return None


def _scope_key(scope) -> str:
    return str(scope or DEFAULT_SCOPE).strip() or DEFAULT_SCOPE


def _entry(owner_id, scope=DEFAULT_SCOPE, *, create: bool = False) -> dict | None:
    owner = _owner_key(owner_id)
    if owner is None:
        return None
    key = (owner, _scope_key(scope))
    entry = _sessions.get(key)
    if entry is not None and _now() - entry["touched"] > SELECTION_TTL_SECONDS:
        _sessions.pop(key, None)
        entry = None
    if entry is None and create:
        entry = {"items": {}, "page": 1, "touched": _now()}
        _sessions[key] = entry
    return entry


def normalize_code(value) -> str:
    """The canonical comparison form of an exact identity (never destructive)."""
    return str(value or "").strip().upper()


def _version_value(value) -> int | None:
    try:
        version = int(value)
    except (TypeError, ValueError):
        return None
    return version if version > 0 else None


def is_selected(owner_id, code: str, scope=DEFAULT_SCOPE) -> bool:
    entry = _entry(owner_id, scope)
    return bool(entry and normalize_code(code) in entry["items"])


def toggle(
    owner_id,
    code: str,
    label: str = "",
    scope=DEFAULT_SCOPE,
    *,
    version: int | None = None,
) -> tuple[bool, str]:
    """Toggle one exact identity in one scope.

    Returns ``(selected_now, refusal_reason)``. A refusal reason is only set
    when the identity could not be stored (empty identity or the cap is
    reached); the caller reports it instead of pretending the tick happened.

    ``version`` is optional metadata a feature may attach to the exact
    identity (Taskloom uses the CAS version it rendered). It is never used as
    authorization by this store; consumers must re-read the authoritative
    record before acting.
    """
    normalized = normalize_code(code)
    if not normalized:
        # Keep the Stash-visible wording byte-identical for its existing tests
        # and notices; other scopes get the neutral wording.
        if _scope_key(scope) == DEFAULT_SCOPE:
            return False, "no saved item was named"
        return False, "no item was named"
    entry = _entry(owner_id, scope, create=True)
    if entry is None:
        return False, "the selection could not be stored"
    items = entry["items"]
    if normalized in items:
        items.pop(normalized)
        entry["touched"] = _now()
        return False, ""
    if len(items) >= MAX_SELECTION:
        return False, f"at most {MAX_SELECTION} items can be selected at once"
    stored_version = _version_value(version)
    items[normalized] = {
        "label": str(label or normalized),
        "version": stored_version,
    }
    entry["touched"] = _now()
    return True, ""


def selected(owner_id, scope=DEFAULT_SCOPE) -> tuple[tuple[str, str], ...]:
    """The selected ``(identity, label)`` pairs, in the order they were ticked."""
    entry = _entry(owner_id, scope)
    if not entry:
        return ()
    return tuple(
        (code, str(item.get("label") or code))
        for code, item in entry["items"].items()
    )


def entries(owner_id, scope=DEFAULT_SCOPE) -> tuple[tuple[str, str, int | None], ...]:
    """The selected ``(identity, label, version)`` triples, in tick order.

    ``version`` is the optional metadata supplied at toggle time. It is a
    snapshot for change detection only — never permission to act.
    """
    entry = _entry(owner_id, scope)
    if not entry:
        return ()
    return tuple(
        (
            code,
            str(item.get("label") or code),
            item.get("version"),
        )
        for code, item in entry["items"].items()
    )


def codes(owner_id, scope=DEFAULT_SCOPE) -> tuple[str, ...]:
    return tuple(code for code, _label in selected(owner_id, scope))


def count(owner_id, scope=DEFAULT_SCOPE) -> int:
    return len(selected(owner_id, scope))


def clear(owner_id, scope=DEFAULT_SCOPE) -> tuple[str, ...]:
    """Drop one owner/scope selection and return the dropped identities."""
    owner = _owner_key(owner_id)
    if owner is None:
        return ()
    entry = _sessions.pop((owner, _scope_key(scope)), None)
    if entry is None:
        return ()
    return tuple(entry["items"])


def set_page(owner_id, page, scope=DEFAULT_SCOPE) -> int:
    """Remember the page a scoped selection view is on (1 is the safe default)."""
    try:
        value = max(1, int(page))
    except (TypeError, ValueError):
        value = 1
    entry = _entry(owner_id, scope, create=True)
    if entry is not None:
        entry["page"] = value
        entry["touched"] = _now()
    return value


def current_page(owner_id, scope=DEFAULT_SCOPE) -> int:
    entry = _entry(owner_id, scope)
    return int(entry["page"]) if entry else 1


def clear_all() -> None:
    """Forget every owner/scope selection (shutdown/test hygiene)."""
    _sessions.clear()
