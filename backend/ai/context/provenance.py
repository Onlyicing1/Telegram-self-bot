"""
Durable AI provenance — the invisible marker carried inside the message text.

The AI answers by editing the owner's own Telegram message in place, so a
message the AI produced still carries ``out=True``/``sender_id=owner`` and is
therefore indistinguishable from a genuinely human-authored owner message by
Telegram metadata alone. ``ReplyResolver`` closes that gap only while the
process lives (RAM-only, LRU-capped).

This module carries the DURABLE half of the same distinction: a short sequence
of invisible Unicode code points appended to the FINAL successful AI
presentation. Because the marker lives in the Telegram message text itself, it
survives process restarts, edits, and re-reads, and it is the only signal the
surrounding-message context collector may trust (``sender_id``/``out`` are
explicitly not provenance).

Marker choice — every code point is U+2061..U+2064 (the "invisible operator"
block). Evidence for that block:

  * General category ``Cf`` (format) and non-combining, so nothing renders and
    no glyph is attached to a neighbouring character;
  * BiDi class ``BN`` (boundary neutral), so the marker cannot set or change
    paragraph direction or reorder adjacent text — required because the
    presentation mixes Persian/Arabic and Latin with directional isolates;
  * not ``White_Space`` and not stripped by ``str.strip()``, so the delivery
    normalizer cannot silently drop it;
  * unchanged by NFC/NFKC-style composition, so a normalized read still finds
    it;
  * four distinct adjacent code points in ascending order — a sequence that
    does not occur in ordinary human or model text.

The marker is metadata, never content: it is appended only to the final
successful answer, it is stripped before any text reaches the model, and it is
idempotent so a retry or a re-entry into the delivery path can never stack it.
"""
from __future__ import annotations

#: The single authoritative marker sequence. Never inline this literal
#: anywhere else: import it (or the helpers below) instead.
AI_PROVENANCE_MARKER = "\u2061\u2062\u2063\u2064"


def has_ai_provenance_marker(text: object) -> bool:
    """True when ``text`` carries the durable AI provenance marker.

    Detection is a plain substring test on the exact sequence, so a partial or
    reordered run of invisible characters is never mistaken for provenance.
    Non-string input (``None``, a media message with no text) is not marked.
    """
    return isinstance(text, str) and AI_PROVENANCE_MARKER in text


def strip_ai_provenance_marker(text: str) -> str:
    """Return ``text`` without the marker — the exact visible content.

    Every occurrence is removed so a duplicated marker cannot survive either.
    Non-string input is returned unchanged (callers may hold ``None``).
    """
    if not isinstance(text, str):
        return text
    return text.replace(AI_PROVENANCE_MARKER, "")


def apply_ai_provenance_marker(text: str) -> str:
    """Append the marker to ``text`` unless it is already there.

    Idempotent by construction: applying it twice yields exactly one marker,
    which is what makes retries and error paths safe.
    """
    if not isinstance(text, str):
        return text
    if has_ai_provenance_marker(text):
        return text
    return f"{text}{AI_PROVENANCE_MARKER}"
