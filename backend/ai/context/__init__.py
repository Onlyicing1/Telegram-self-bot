"""
Context Resolution Layer — maps Telegram reply targets to AI messages.

This package provides two complementary provenance mechanisms:

  * ``ReplyResolver`` — an in-memory registry that maps Telegram message IDs to
    the AI-generated content that was edited into them. When the owner replies
    to an AI message, the resolver deterministically retrieves the full AI
    content — not a truncated preview. Process-scoped.
  * ``provenance`` — the durable, invisible AI provenance marker carried inside
    the Telegram message text itself. It survives restarts, which is what lets
    the surrounding-message context collector recognise AI-answered/AI-modified
    messages after the process restarted.
"""
from backend.ai.context.provenance import (
    AI_PROVENANCE_MARKER,
    apply_ai_provenance_marker,
    has_ai_provenance_marker,
    strip_ai_provenance_marker,
)
from backend.ai.context.reply_resolver import ReplyResolver, get_resolver

__all__ = [
    "AI_PROVENANCE_MARKER",
    "ReplyResolver",
    "apply_ai_provenance_marker",
    "get_resolver",
    "has_ai_provenance_marker",
    "strip_ai_provenance_marker",
]
