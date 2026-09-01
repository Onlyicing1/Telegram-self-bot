"""
Trusted chat-name resolution for Taskloom destinations.

The model may express a destination as a semantic chat NAME (e.g. "OskarBeam",
"oskar") — never as a raw numeric chat_id. This module resolves those names
against the chats available to the authenticated Self Bot.

Rules:
- Chat names are matched fuzzily (case-insensitive substring + normalized
  comparison). Exact string equality is NOT required.
- If exactly one strong match exists, resolve it without clarification.
- If multiple chats are sufficiently similar, fail with a numbered list of
  options — the user must clarify.
- If no sufficiently good match exists, fail with a clear "not found" message.
- The model can never provide an arbitrary chat_id or recipient.
- Owner isolation is preserved: only chats visible to the Self Bot are
  considered.

The numbering/selection mechanism is deterministic: options are sorted by
match score then by chat_id, and presented as a compact numbered list.
"""

from __future__ import annotations

import logging
import re
from typing import Any

logger = logging.getLogger(__name__)

_MAX_CHAT_NAME_CHARS = 256
_MATCH_THRESHOLD = 0.6  # minimum normalized score for a "good" match
_strong_match_threshold = 0.8  # score above which a single match is auto-resolved


def _normalize(s: str) -> str:
    """Normalize a chat name for comparison: lowercase, collapse whitespace,
    strip common separators."""
    if not isinstance(s, str):
        return ""
    s = s.strip().lower()
    s = re.sub(r"[\s_\-./]+", " ", s)
    s = re.sub(r"\s+", " ", s).strip()
    return s


def _score(name: str, candidate: str) -> float:
    """Return a similarity score in [0.0, 1.0] for name vs candidate.

    Uses a simple normalized containment + sequence similarity heuristic.
    - Exact normalized match → 1.0
    - candidate contains name (or name contains candidate) → high score
    - otherwise a character-level similarity estimate
    """
    n = _normalize(name)
    c = _normalize(candidate)
    if not n or not c:
        return 0.0
    if n == c:
        return 1.0
    # containment: one is a substring of the other
    if n in c or c in n:
        ratio = len(n) / len(c) if len(n) <= len(c) else len(c) / len(n)
        return 0.85 + 0.15 * ratio
    # character-level overlap heuristic
    common = sum(1 for ch in set(n) if ch in c) / max(len(set(n)), 1)
    length_ratio = min(len(n), len(c)) / max(len(n), len(c))
    return round(0.3 * common + 0.7 * length_ratio, 3)


def _is_strong_match(score: float, name: str, candidate: str) -> bool:
    """True when the score represents a strong, unambiguous match."""
    if score >= _strong_match_threshold:
        return True
    # A short query that exactly matches the start of a candidate is strong.
    n = _normalize(name)
    c = _normalize(candidate)
    if len(n) >= 3 and c.startswith(n):
        return True
    if len(c) >= 3 and n.startswith(c):
        return True
    return False


def resolve_chat_name(
    chat_name: str,
    chats: list[dict[str, Any]],
    *,
    owner_id: int = 0,
) -> dict[str, Any]:
    """Resolve a semantic chat NAME to a trusted chat_id.

    Args:
        chat_name: The chat name from the model (e.g. "OskarBeam", "oskar").
        chats: List of chat dicts from the Telegram client, each with at
            least "id", "title" (or "name"), and optionally "username".
        owner_id: The owner's user id, used to prefer the owner's own chats
            when relevant (e.g. Saved Messages).

    Returns:
        A dict with:
        - "resolved": True/False
        - "chat_id": the resolved chat id (only when resolved=True)
        - "chat_title": the resolved chat title
        - "matches": list of top matching chats (for clarification)
        - "error": human-readable error when not resolved

    The result never contains a model-supplied chat_id — only IDs from the
    trusted Telegram client.
    """
    if not isinstance(chat_name, str) or not chat_name.strip():
        return {"resolved": False, "error": "Chat name is empty."}
    if len(chat_name) > _MAX_CHAT_NAME_CHARS:
        return {"resolved": False, "error": "Chat name is too long."}

    name = chat_name.strip()
    scored: list[tuple[float, dict[str, Any]]] = []

    for chat in chats:
        if not isinstance(chat, dict):
            continue
        chat_id = chat.get("id")
        if not isinstance(chat_id, int) or chat_id == 0:
            continue
        title = str(chat.get("title") or chat.get("name") or "").strip()
        username = str(chat.get("username") or "").strip()
        if not title and not username:
            continue

        # Score against both title and username.
        best: float = 0.0
        best_label: str = title or username
        if title:
            best = max(best, _score(name, title))
        if username:
            # Also try the @ handle without the @ sign.
            handle = username.lstrip("@")
            best = max(best, _score(name, handle))
            best = max(best, _score(name, username))
            best_label = username if not title else f"{title} (@{username})"

        if best > 0:
            scored.append((best, {"chat_id": chat_id, "title": title, "username": username, "label": best_label}))

    if not scored:
        return {"resolved": False, "error": f"No chats found matching '{name}'."}

    scored.sort(key=lambda x: (-x[0], x[1]["chat_id"]))
    top_score, top_chat = scored[0]

    # A short query can exactly match one chat while also being a meaningful
    # prefix/substring of another. Prefer clarification over silently choosing
    # the exact result when the candidate set is genuinely ambiguous.
    normalized_name = _normalize(name)
    related_matches = [
        (s, c) for s, c in scored
        if s >= _MATCH_THRESHOLD
        and (
            normalized_name == _normalize(c["title"])
            or normalized_name in _normalize(c["title"])
            or _normalize(c["title"]) in normalized_name
        )
    ]
    if len(related_matches) >= 2:
        return {
            "resolved": False,
            "error": f"Multiple chats match '{name}'. Please specify which one:",
            "matches": [
                {"rank": i + 1, "chat_id": c["chat_id"], "title": c["title"] or str(c["chat_id"]), "score": s}
                for i, (s, c) in enumerate(related_matches[:8])
            ],
        }

    if top_score >= _strong_match_threshold:
        # One dominant match — resolve without clarification.
        return {
            "resolved": True,
            "chat_id": top_chat["chat_id"],
            "chat_title": top_chat["title"] or str(top_chat["chat_id"]),
            "matches": [],
        }

    # Check for multiple strong matches (ambiguity).
    strong_matches = [(s, c) for s, c in scored if s >= _MATCH_THRESHOLD]
    if len(strong_matches) >= 2:
        return {
            "resolved": False,
            "error": f"Multiple chats match '{name}'. Please specify which one:",
            "matches": [
                {"rank": i + 1, "chat_id": c["chat_id"], "title": c["title"] or str(c["chat_id"]), "score": s}
                for i, (s, c) in enumerate(strong_matches[:8])
            ],
        }

    # One moderate match but below strong threshold — present as a candidate
    # only if the score is decent; otherwise say not found.
    if top_score >= _MATCH_THRESHOLD:
        return {
            "resolved": True,
            "chat_id": top_chat["chat_id"],
            "chat_title": top_chat["title"] or str(top_chat["chat_id"]),
            "matches": [],
        }

    return {"resolved": False, "error": f"No clear match for '{name}'. Try a more specific chat name."}


def format_clarification_options(result: dict[str, Any]) -> str:
    """Format a multi-match result as a compact numbered clarification prompt.

    Example output:
        Multiple chats match 'oskar'. Please specify which one:
        1. OskarBeam
        2. Oskar
        3. Oskar Beam
    """
    if not result.get("matches"):
        return result.get("error", "Unknown error.")
    lines = [result["error"]]
    for m in result["matches"]:
        lines.append(f"{m['rank']}. {m['title']}")
    return "\n".join(lines)
