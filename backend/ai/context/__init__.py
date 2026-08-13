"""
Context Resolution Layer — maps Telegram reply targets to AI messages.

This package provides the ReplyResolver, an in-memory registry that maps
Telegram message IDs to the AI-generated content that was edited into them.
When the owner replies to an AI message, the resolver deterministically
retrieves the full AI content — not a truncated preview.
"""
from backend.ai.context.reply_resolver import ReplyResolver, get_resolver

__all__ = ["ReplyResolver", "get_resolver"]
