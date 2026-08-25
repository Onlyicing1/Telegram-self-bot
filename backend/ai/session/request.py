"""
AIRequest — the immutable input object for the AI Session pipeline.

A ``AIRequest`` is constructed by the caller (eventually a command handler
or panel callback) and passed into the session pipeline. It carries
everything the pipeline needs to build context, assemble a prompt, and
call a provider — without the caller knowing anything about those layers.

This object is frozen: once created, it cannot be modified. Each pipeline
stage receives it read-only.

Fields (from AI_MASTER_DESIGN.md §25):
  session_id:   Unique AI session identifier.
  user_message:  The raw text the owner typed.
  owner_id:      Telegram user ID of the bot owner.
  chat_id:       Telegram chat ID where the conversation lives.
  message_id:    Telegram message ID of the triggering message.
  reply_context: Optional ``ReplyContext`` (replied message metadata).
  tool_request:  Optional tool request name (future, empty for now).
  timestamp:     UTC datetime when this request was created.
  language:      Owner's language (e.g. ``"English"``).
  timezone:      Owner's timezone string (e.g. ``"Asia/Tehran"``).
  metadata:      Arbitrary extra metadata (future use).
  allow_tools:   Whether this request may expose or execute AI tools.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from backend.ai.conversation.context_builder import ReplyContext


@dataclass(frozen=True)
class AIRequest:
    """The immutable input object for the AI Session pipeline.

    Attributes:
        session_id:    Unique AI session identifier.
        user_message:  The raw text the owner typed.
        owner_id:       Telegram user ID of the bot owner.
        chat_id:        Telegram chat ID where the conversation lives.
        message_id:    Telegram message ID of the triggering message.
        reply_context:  Reply metadata, or a default (no reply).
        tool_request:   Tool name to invoke (future — empty for now).
        timestamp:      UTC datetime when this request was created.
        language:       Owner's language.
        timezone:       Owner's timezone string.
        metadata:       Arbitrary extra metadata for future use.
        allow_tools:    Whether this request may expose or execute AI tools.
    """

    session_id: str
    user_message: str
    owner_id: int
    chat_id: int
    message_id: int
    reply_context: ReplyContext = field(default_factory=ReplyContext)
    tool_request: str = ""
    timestamp: datetime = field(default_factory=lambda: datetime.now(timezone.utc))
    language: str = "English"
    timezone: str = "UTC"
    metadata: dict[str, Any] = field(default_factory=dict)
    request_id: str = ""
    allow_tools: bool = True
