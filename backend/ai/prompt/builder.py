"""
Prompt Builder — assembles a ``PromptPackage`` from a ``ConversationContext``.

The Prompt Builder is the SOLE consumer of ``ConversationContext`` and
the SOLE producer of ``PromptPackage``. It receives exactly ONE object
(the context) and produces exactly ONE object (the package).

The prompt is assembled in a FIXED, deterministic order (from
AI_MASTER_DESIGN.md §7.1):

    1. System Rules
    2. Platform Constraints
    3. Runtime Rules
    4. Current Context
    5. Conversation State
    6. Current Tool Metadata
    7. Tool Results (future placeholder)
    8. User Message
    9. Output Instructions

This order NEVER changes.

The builder does NOT:
  - Call any model or provider
  - Execute tools
  - Access Telegram, Supabase, or any external service
  - Modify any existing feature
  - Use globals or singletons

Everything is dependency-injected. The builder is stateless.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any

from backend.ai.conversation.context_builder import ConversationContext
from backend.ai.prompt.budget import TokenBudget, compute_budget
from backend.ai.prompt.template import (
    OUTPUT_INSTRUCTIONS_TEMPLATE,
    PLATFORM_CONSTRAINTS_TEMPLATE,
    RUNTIME_RULES_TEMPLATE,
    SYSTEM_RULES_TEMPLATE,
    PromptSection,
)
from backend.ai.prompt.validator import validate_prompt_package


@dataclass(frozen=True)
class PromptPackage:
    """The immutable output of the Prompt Builder.

    This is the ONLY object a future provider will receive. It contains
    everything needed to construct a model request, with no AI-specific
    logic inside it.

    Attributes:
        system_prompt:          Merged system rules + platform constraints + runtime rules.
        runtime_context:         Text block of runtime context (time, settings, AI state).
        conversation_context:    Text block of conversation state (panel, flow, reply, history).
        tool_context:            Text block of current/last tool metadata.
        user_input:             The owner's raw text message.
        metadata:               Dict with builder metadata (section count, format, timestamps).
        estimated_tokens:       TokenBudget with all token estimates.
        sections:               Dict mapping each ``PromptSection`` to its rendered text.
                                 This is the raw material formatters consume.
    """

    system_prompt: str
    runtime_context: str
    conversation_context: str
    tool_context: str
    user_input: str
    metadata: dict[str, Any]
    estimated_tokens: TokenBudget
    sections: dict[PromptSection, str] = field(default_factory=dict)


class PromptBuilder:
    """Assembles ``PromptPackage`` objects from ``ConversationContext``.

    Stateless. No globals. No I/O. Receives a context, produces a
    package. If the context changes, the caller builds a new context
    and calls ``build()`` again — the package is immutable.

    Usage::

        builder = PromptBuilder()
        package = builder.build(context)
        # package is frozen — pass to a future provider
    """

    __slots__ = ()

    def build(self, context: ConversationContext) -> PromptPackage:
        """Assemble an immutable ``PromptPackage`` from a conversation context.

        Args:
            context: The ``ConversationContext`` produced by the Conversation Layer.

        Returns:
            A frozen ``PromptPackage`` with all sections in fixed order.
        """
        sections = self._render_sections(context)
        budget = compute_budget(
            sections,
            language=context.language,
        )

        package = PromptPackage(
            system_prompt=self._merge_system(sections),
            runtime_context=sections.get(PromptSection.CURRENT_CONTEXT, ""),
            conversation_context=sections.get(PromptSection.CONVERSATION_STATE, ""),
            tool_context=sections.get(PromptSection.TOOL_METADATA, ""),
            user_input=sections.get(PromptSection.USER_MESSAGE, ""),
            metadata={
                "section_count": len(sections),
                "language": context.language,
                "timezone": context.timezone,
                "session_id": context.session_id,
                "state": context.state.value,
                "built_at": datetime.now(timezone.utc).isoformat(),
            },
            estimated_tokens=budget,
            sections=sections,
        )

        validate_prompt_package(package)
        return package

    def _render_sections(
        self, ctx: ConversationContext
    ) -> dict[PromptSection, str]:
        """Render all 9 sections from the context, in fixed order."""
        sections: dict[PromptSection, str] = {}

        sections[PromptSection.SYSTEM_RULES] = SYSTEM_RULES_TEMPLATE
        sections[PromptSection.PLATFORM_CONSTRAINTS] = PLATFORM_CONSTRAINTS_TEMPLATE
        sections[PromptSection.RUNTIME_RULES] = RUNTIME_RULES_TEMPLATE
        sections[PromptSection.CURRENT_CONTEXT] = self._render_current_context(ctx)
        sections[PromptSection.CONVERSATION_STATE] = self._render_conversation_state(ctx)
        sections[PromptSection.TOOL_METADATA] = self._render_tool_metadata(ctx)
        sections[PromptSection.TOOL_RESULTS] = self._render_tool_results(ctx)
        sections[PromptSection.USER_MESSAGE] = ctx.user_text
        sections[PromptSection.OUTPUT_INSTRUCTIONS] = OUTPUT_INSTRUCTIONS_TEMPLATE

        return sections

    def _merge_system(self, sections: dict[PromptSection, str]) -> str:
        """Merge the three system-level sections into one string."""
        parts = [
            sections.get(PromptSection.SYSTEM_RULES, ""),
            sections.get(PromptSection.PLATFORM_CONSTRAINTS, ""),
            sections.get(PromptSection.RUNTIME_RULES, ""),
        ]
        return "\n\n".join(p for p in parts if p)

    def _render_current_context(self, ctx: ConversationContext) -> str:
        """Render the runtime/context block (§25.1 fields)."""
        lines: list[str] = ["[Runtime Context]"]
        lines.append(f"Menu: {ctx.current_menu}")
        lines.append(f"Panel: {ctx.current_panel}")
        lines.append(f"Category: {ctx.current_category}")
        lines.append(f"Pending Action: {ctx.pending_action or 'None'}")
        lines.append(f"Timezone: {ctx.timezone}")
        lines.append(f"Language: {ctx.language}")
        lines.append(f"Current Time: {ctx.current_time}")

        if ctx.settings.settings:
            settings_str = ", ".join(
                f"{k}={v}" for k, v in sorted(ctx.settings.settings.items())
            )
            lines.append(f"Settings: {settings_str}")
        else:
            lines.append("Settings: None")

        if ctx.runtime.ai_enabled:
            lines.append(
                f"AI: enabled (provider={ctx.runtime.active_provider or 'none'}, "
                f"requests={ctx.runtime.total_requests}, "
                f"responses={ctx.runtime.total_responses}, "
                f"turn={ctx.runtime.turn_count})"
            )
        else:
            lines.append("AI: disabled")

        return "\n".join(lines)

    def _render_conversation_state(self, ctx: ConversationContext) -> str:
        """Render the conversation state block."""
        lines: list[str] = ["[Conversation State]"]
        lines.append(f"State: {ctx.state.value}")
        lines.append(f"Flow: {ctx.current_flow or 'None'}")

        if ctx.reply.exists:
            lines.append("[Reply Context]")
            lines.append(f"Message ID: {ctx.reply.message_id}")
            lines.append(f"Sender: {ctx.reply.sender_name or 'Unknown'}")
            lines.append(f"Chat: {ctx.reply.chat_title or 'Unknown'} ({ctx.reply.chat_id})")
            if ctx.reply.media_type:
                lines.append(f"Media: {ctx.reply.media_type}")
            if ctx.reply.text_preview:
                lines.append(f"Text: {ctx.reply.text_preview}")
            if ctx.reply.timestamp:
                lines.append(f"Timestamp: {ctx.reply.timestamp}")
        else:
            lines.append("Reply: None")

        if ctx.history:
            lines.append(f"[History] ({len(ctx.history)} entries)")
            for i, entry in enumerate(ctx.history):
                label = entry.role
                if entry.tool_name:
                    label += f" ({entry.tool_name})"
                lines.append(f"  {i + 1}. [{label}] {entry.content}")
        else:
            lines.append("History: None")

        return "\n".join(lines)

    def _render_tool_metadata(self, ctx: ConversationContext) -> str:
        """Render the current tool metadata block."""
        lines: list[str] = ["[Tool Context]"]
        if ctx.tool.current_tool:
            lines.append(f"Current Tool: {ctx.tool.current_tool}")
        else:
            lines.append("Current Tool: None")
        if ctx.tool.last_tool:
            lines.append(f"Last Tool: {ctx.tool.last_tool}")
        else:
            lines.append("Last Tool: None")
        return "\n".join(lines)

    def _render_tool_results(self, ctx: ConversationContext) -> str:
        """Render the tool results block (future placeholder).

        Currently returns empty string — tool results will be injected
        here when the tool execution layer is built. The section exists
        to maintain the fixed section order.
        """
        if ctx.tool.last_tool_result:
            return f"[Tool Results]\n{ctx.tool.last_tool_result}"
        return ""
