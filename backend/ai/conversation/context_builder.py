"""
Context Builder — assembles one immutable ``ConversationContext`` object.

The Context Builder is the single source of runtime context for the
AI layer. It merges six context sources into one frozen dataclass:

    Conversation Context  (session state, current flow, language, TZ)
    Runtime Context       (AI state, turn count, request count)
    Telegram Context      (chat ID, message ID, owner ID, chat title)
    Reply Context         (replied message metadata, if any)
    Tool Context          (current tool, last tool, tool result)
    Settings Context      (owner's current bot settings)

The resulting ``ConversationContext`` is the ONLY object consumed
later by the Prompt Builder. The Prompt Builder never reads session
state, runtime state, or settings directly — it receives this object.

This module contains no I/O. It receives already-fetched data and
assembles it. The caller (e.g. a handler) is responsible for fetching
reply metadata and settings before calling ``build()``.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from backend.ai.conversation.history import HistoryEntry
from backend.ai.conversation.session import ConversationSession
from backend.ai.conversation.state import ConversationState


@dataclass(frozen=True)
class ReplyContext:
    """Metadata about the message the owner replied to.

    All fields are optional — if the owner did not reply to a message,
    ``exists`` is False and all other fields are empty/zero.

    Attributes:
        exists:        Whether a reply target exists at all.
        message_id:    Telegram message ID of the replied message.
        sender_id:     Telegram user/channel ID of the sender.
        sender_name:   Display name of the sender.
        chat_id:       Chat ID where the replied message lives.
        chat_title:    Human-readable chat title.
        media_type:    Media type label (e.g. ``"Photo"``) or empty.
        text_preview:  First 200 characters of the message text.
        timestamp:     ISO string of the message timestamp, or empty.
        is_ai_message:   True when the replied message is a known AI response.
        ai_session_id:   Session ID that produced the AI message (when known).
        ai_role:         Role of the AI message (``"assistant"``).
        ai_content:      Full AI response text (untruncated when available).
        ai_provider:     Provider that generated the AI message.
        ai_model:        Model that generated the AI message.
        ai_timestamp:    UTC ISO string when the AI message was registered.
    """

    exists: bool = False
    message_id: int = 0
    sender_id: int = 0
    sender_name: str = ""
    chat_id: int = 0
    chat_title: str = ""
    media_type: str = ""
    text_preview: str = ""
    timestamp: str = ""
    is_ai_message: bool = False
    ai_session_id: str = ""
    ai_role: str = ""
    ai_content: str = ""
    ai_provider: str = ""
    ai_model: str = ""
    ai_timestamp: str = ""


@dataclass(frozen=True)
class SettingsContext:
    """Snapshot of the owner's current bot settings.

    Attributes:
        settings: Dict of key-value pairs (e.g. ``{"bio": "on"}``).
        raw:      Optional raw representation for debugging.
    """

    settings: dict[str, str] = field(default_factory=dict)
    raw: str = ""


@dataclass(frozen=True)
class ToolContext:
    """Tool-related context for the current request.

    Attributes:
        current_tool:    Name of the tool currently executing, or empty.
        last_tool:       Name of the last tool that executed, or empty.
        last_tool_result: Result text of the last tool execution, or empty.
    """

    current_tool: str = ""
    last_tool: str = ""
    last_tool_result: str = ""


@dataclass(frozen=True)
class RuntimeContext:
    """Process-wide runtime context for the AI layer.

    Attributes:
        ai_enabled:       Whether AI is enabled at all.
        active_provider:  Name of the active model provider, or empty.
        total_requests:   Lifetime AI request count.
        total_responses:  Lifetime AI response count.
        turn_count:       Turns in the current session.
    """

    ai_enabled: bool = False
    active_provider: str = ""
    total_requests: int = 0
    total_responses: int = 0
    turn_count: int = 0


@dataclass(frozen=True)
class ConversationContext:
    """The ONE immutable context object consumed by the Prompt Builder.

    Assembled by ``ContextBuilder.build()`` from six sources. The
    Prompt Builder receives this object and nothing else.

    Attributes:
        session_id:     Current session ID.
        owner_id:       Telegram user ID of the owner.
        chat_id:        Telegram chat ID where the conversation lives.
        message_id:     Telegram message ID of the triggering message.
        state:          Current conversation state (enum).
        current_menu:   Current top-level menu (e.g. ``"main"``).
        current_panel:  Current panel ID (e.g. ``"ai:new"``).
        current_category: Current menu category (e.g. ``"ai"``).
        current_flow:   Active user flow name (e.g. ``"save"``).
        pending_action: Pending action description, or empty.
        language:       Owner's language (e.g. ``"English"``).
        timezone:       Owner's timezone string (e.g. ``"Asia/Tehran"``).
        current_time:   Current time in the owner's timezone (ISO string).
        user_text:       The raw text the owner typed (the prompt).
        reply:           Reply context (replied message metadata).
        tool:            Tool context (current/last tool info).
        settings:        Settings context (owner's bot settings snapshot).
        runtime:         Runtime context (AI state, counters).
        history:         Recent conversation history entries.
        created_at:      UTC timestamp when this context was assembled.
    """

    session_id: str
    owner_id: int
    chat_id: int
    message_id: int
    state: ConversationState
    current_menu: str
    current_panel: str
    current_category: str
    current_flow: str
    pending_action: str
    language: str
    timezone: str
    current_time: str
    user_text: str
    reply: ReplyContext
    tool: ToolContext
    settings: SettingsContext
    runtime: RuntimeContext
    history: list[HistoryEntry]
    created_at: datetime = field(default_factory=lambda: datetime.now(timezone.utc))


class ContextBuilder:
    """Assembles ``ConversationContext`` objects from injected data.

    The builder itself is stateless. It receives all inputs as
    arguments to ``build()`` and returns an immutable context. No
    globals, no side effects.

    Usage::

        builder = ContextBuilder()
        ctx = builder.build(
            session=session,
            user_text="Save this message",
            message_id=12345,
            reply=reply_ctx,
            settings=settings_ctx,
            runtime=runtime_ctx,
            history=history_entries,
        )
        # Pass ctx to Prompt Builder (future)
    """

    __slots__ = ()

    def build(
        self,
        session: ConversationSession,
        user_text: str,
        message_id: int,
        current_menu: str = "main",
        reply: ReplyContext | None = None,
        tool: ToolContext | None = None,
        settings: SettingsContext | None = None,
        runtime: RuntimeContext | None = None,
        history: list[HistoryEntry] | None = None,
    ) -> ConversationContext:
        """Assemble an immutable ``ConversationContext``.

        Args:
            session:       The active conversation session.
            user_text:     The raw text the owner typed.
            message_id:    Telegram message ID of the triggering message.
            current_menu:  Current top-level menu name.
            reply:         Reply context (or None for no reply).
            tool:          Tool context (or None for defaults).
            settings:      Settings context (or None for empty).
            runtime:       Runtime context (or None for defaults).
            history:       Recent history entries (or None for empty).

        Returns:
            A frozen ``ConversationContext`` ready for the Prompt Builder.
        """
        now = datetime.now(timezone.utc)
        try:
            from zoneinfo import ZoneInfo
            local_now = now.astimezone(ZoneInfo(session.timezone))
            current_time = local_now.strftime("%Y-%m-%d %H:%M")
        except Exception:
            current_time = now.strftime("%Y-%m-%d %H:%M")

        return ConversationContext(
            session_id=session.session_id,
            owner_id=session.owner_id,
            chat_id=session.chat_id,
            message_id=message_id,
            state=session.state,
            current_menu=current_menu,
            current_panel=session.current_panel,
            current_category=session.current_category,
            current_flow=session.current_flow,
            pending_action=session.pending_action,
            language=session.language,
            timezone=session.timezone,
            current_time=current_time,
            user_text=user_text,
            reply=reply or ReplyContext(),
            tool=tool or ToolContext(
                current_tool=session.current_tool,
                last_tool=session.last_tool,
            ),
            settings=settings or SettingsContext(),
            runtime=runtime or RuntimeContext(),
            history=history or [],
            created_at=now,
        )
