"""
Owner-confirmation state for ADMIN_ONLY / CONFIRMATION_REQUIRED tools.

The ToolExecutor refuses to auto-execute ADMIN_ONLY and
CONFIRMATION_REQUIRED tools and returns a ``needs_confirmation`` result.
This module is the bounded in-memory state that lets the Dispatcher turn
that result into an interactive owner approval:

    Dispatcher sees needs_confirmation
        → PendingConfirmationStore.create(...)     (server-side, frozen args)
        → confirmation prompt delivered to the owner
        → owner replies «تأیید» / «بله» / "yes"
        → Dispatcher take()s the entry (single-use)
        → original tool name + arguments re-issued through ToolExecutor

Security model:

  - Owner + chat scoped: a confirmation is keyed by ``(owner_id, chat_id)``
    and can never be consumed from another chat/owner.
  - Explicit intent only: recognition is a tiny exact-match phrase set
    (never a keyword router, never part of the AI prompt vocabulary).
    Ambiguous acknowledgements ("باشه، بعداً انجامش میدم") do not match and
    therefore never execute anything.
  - Frozen arguments: the store keeps the exact validated arguments from the
    original provider tool call. The confirmation reply can only *consume*
    the entry — it can never modify the tool name or arguments.
  - Single-use + bounded: ``take()`` removes the entry before it is
    returned, so a replay can never execute twice. Entries expire on a
    monotonic TTL and fail closed after expiry.
  - One pending per scope: ``create()`` returns None while an unexpired
    entry exists for the same (owner, chat). This is an explicit boundary —
    an existing pending action is never silently overwritten.

This follows the repository's existing in-memory bounded-state convention
(``backend/helper/input_state.py``): process memory, monotonic expiry, no
persistence, no background cleanup, ``clear_all()`` for tests. No database
table, no migration, no new persistence authority.
"""
from __future__ import annotations

import time
import unicodedata
import uuid
from dataclasses import dataclass
from typing import Any

# Interactive confirmation lifetime. Mirrors the pending-input convention
# (helper/input_state.py uses 120 s) — long enough for the owner to notice
# and reply, short enough that a stale approval cannot sit forever.
CONFIRMATION_TTL_S = 120.0

# The COMPLETE set of explicit confirmation replies. Exact normalized match
# only — deliberately tiny so ordinary conversation ("باشه، بعداً انجامش
# میدم", "بله چیزی بگو", "ok") can never auto-approve an owner-only action.
# Persian spellings of تأیید (تایید / تائید) are all accepted because users
# type them interchangeably.
_EXPLICIT_CONFIRMATIONS = frozenset({
    # Persian
    "بله",
    "آره",
    "اره",
    "بلی",
    "تایید",
    "تائید",
    "تأیید",
    "تایید میکنم",
    "تائید میکنم",
    "تأیید میکنم",
    "بله تایید میکنم",
    "بله تائید میکنم",
    "بله تأیید میکنم",
    "آره تایید میکنم",
    "آره تائید میکنم",
    "آره تأیید میکنم",
    # English
    "yes",
    "yeah",
    "yep",
    "confirm",
    "confirmed",
    "approve",
    "approved",
    "i confirm",
    "go ahead",
})

CONFIRMATION_ALREADY_PENDING_TEXT = (
    "⚠️ An earlier owner-only action is still waiting for your approval.\n\n"
    "Reply to that request with «تأیید» / «بله» / \"yes\" to approve it, or "
    "wait for it to expire. The new request was NOT scheduled — ask again "
    "after the first approval (or once it has expired)."
)


def _expired_text() -> str:
    return (
        "⏳ That approval request has expired and nothing was executed.\n\n"
        "Ask for the action again if you still want it — a new confirmation "
        "will be issued for you to approve."
    )


@dataclass(frozen=True)
class PendingConfirmation:
    """A single server-created owner approval awaiting explicit confirmation.

    ``arguments`` is a frozen copy of the ORIGINAL validated tool-call
    arguments. The confirmation reply can never alter them.
    """

    confirmation_id: str
    owner_id: int
    chat_id: int
    session_id: str
    tool_name: str
    arguments: dict[str, Any]
    created_at: float
    expires_at: float

    @property
    def expired(self) -> bool:
        """True once the monotonic TTL has elapsed. Fails closed."""
        return time.monotonic() >= self.expires_at


class PendingConfirmationStore:
    """Bounded in-memory store of pending owner confirmations.

    Exactly one pending confirmation per ``(owner_id, chat_id)``. All
    operations are synchronous and await-free, so they are atomic with
    respect to the asyncio event loop — no lock required for the
    create/take race.
    """

    def __init__(self, ttl_s: float = CONFIRMATION_TTL_S) -> None:
        self._ttl_s = ttl_s
        self._entries: dict[tuple[int, int], PendingConfirmation] = {}

    def create(
        self,
        owner_id: int,
        chat_id: int,
        session_id: str,
        tool_name: str,
        arguments: dict[str, Any],
    ) -> PendingConfirmation | None:
        """Store a pending confirmation, or None when one is already pending.

        An existing EXPIRED entry is replaced (it can no longer be
        confirmed); an existing ACTIVE entry is never overwritten — the
        caller must surface the already-pending state instead.
        """
        key = (owner_id, chat_id)
        existing = self._entries.get(key)
        if existing is not None and not existing.expired:
            return None
        now = time.monotonic()
        entry = PendingConfirmation(
            confirmation_id=uuid.uuid4().hex,
            owner_id=owner_id,
            chat_id=chat_id,
            session_id=session_id or "",
            tool_name=tool_name,
            arguments=dict(arguments or {}),
            created_at=now,
            expires_at=now + self._ttl_s,
        )
        self._entries[key] = entry
        return entry

    def take(self, owner_id: int, chat_id: int) -> tuple[PendingConfirmation | None, bool]:
        """Single-use consume of the active confirmation for the scope.

        Returns:
            (entry, False) — an ACTIVE entry existed and was removed
                BEFORE being returned, so a replay finds nothing.
            (None, True)   — an EXPIRED entry was found and purged; the
                caller answers accurately that the approval lapsed.
            (None, False)  — no entry existed for this scope.
        """
        key = (owner_id, chat_id)
        entry = self._entries.pop(key, None)
        if entry is None:
            return None, False
        if entry.expired:
            return None, True
        return entry, False

    def pending_count(self) -> int:
        """Number of stored entries (including not-yet-purged expired ones)."""
        return len(self._entries)

    def clear_all(self) -> None:
        self._entries.clear()


def normalize_confirmation_text(text: str) -> str:
    """Normalize a reply for exact confirmation matching.

    NFKC-unifies presentation forms, converts ZWNJ to a space (so
    تایید‌میکنم equals the stored تایید میکنم), removes tatweel,
    keeps only alphanumerics and whitespace (so trailing punctuation/emoji
    cannot break the match), collapses spaces, and casefolds ASCII.
    Persian is casefold-invariant.
    """
    if not isinstance(text, str):
        return ""
    value = unicodedata.normalize("NFKC", text)
    value = value.replace("\u200c", " ").replace("\u0640", "")
    value = "".join(ch for ch in value if ch.isalnum() or ch.isspace())
    return " ".join(value.split()).casefold()


def is_explicit_confirmation(text: str) -> bool:
    """True only for an exact, explicit confirmation reply.

    Full-message exact match against the bounded phrase set — never a
    substring/keyword scan. Anything ambiguous or conversational returns
    False and therefore can never consume a pending owner-only action.
    """
    return normalize_confirmation_text(text) in _EXPLICIT_CONFIRMATIONS


def _render_value(value: Any) -> str:
    rendered = str(value)
    if len(rendered) > 200:
        rendered = f"{rendered[:197]}..."
    return rendered


def confirmation_request_text(tool_name: str, arguments: dict[str, Any]) -> str:
    """Deterministic, user-facing confirmation prompt for one blocked action.

    Shows the exact frozen tool name and arguments so the owner approves
    precisely what will execute.
    """
    lines = [
        "⚠️ Owner approval required",
        "",
        "The AI wants to run an owner-only action:",
        "",
        f"  {tool_name}",
    ]
    for key, value in sorted((arguments or {}).items()):
        lines.append(f"    {key} = {_render_value(value)}")
    lines.extend([
        "",
        "Reply to this message with «تأیید», «بله», or \"yes\" to approve",
        "EXACTLY this action. Nothing runs until you approve it, and this",
        "approval expires in 2 minutes.",
    ])
    return "\n".join(lines)
