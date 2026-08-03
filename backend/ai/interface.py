"""
AIInterface — the ONE public entrypoint for all AI interactions.

This is the only object the rest of the codebase should import:

    from backend.ai import AIInterface

Everything else in the ``backend.ai`` package is internal. The interface
delegates to the active provider (if any) and manages runtime state.

Currently the interface does NOTHING. ``handle()`` returns ``AI_DISABLED``.
No model, no API calls, no prompts, no background tasks, no timers.

Architecture:

    ┌──────────────┐
    │  Caller      │  (command handler, panel, etc.)
    │  (anywhere)  │
    └──────┬───────┘
           │ AIInterface.handle(context)
           ▼
    ┌──────────────────────────────────────────────┐
    │  AIInterface                                 │
    │  ├─ checks runtime_state.enabled             │
    │  ├─ resolves active provider from registry   │
    │  ├─ delegates to provider.generate()        │
    │  └─ updates runtime/session state           │
    └──────────────────────────────────────────────┘
           │
           ▼ (future, when a provider is registered)
    ┌──────────────┐
    │  AIProvider  │  (adapter: OpenAI, Gemini, local, etc.)
    └──────────────┘

Dependency injection:
    The interface receives ``runtime_state`` and ``registry`` as
    constructor arguments. No globals, no module-level singletons.
    The runtime supervisor constructs one ``AIInterface`` and passes
    it to whoever needs it.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass, field
from typing import Any

from backend.ai.context import AIContext
from backend.ai.provider import AIProvider, ToolRequest, ToolResult
from backend.ai.registry import ProviderRegistry
from backend.ai.state import AIRuntimeState

logger = logging.getLogger(__name__)

AI_DISABLED = "AI_DISABLED"


@dataclass(frozen=True)
class AIResponse:
    """The response object returned by ``AIInterface.handle``.

    Attributes:
        text:          The human-readable response text. When AI is
                        disabled, this is ``"AI_DISABLED"``.
        enabled:        Whether AI was enabled for this request.
        provider_name:  Name of the provider that produced this response,
                        or ``""`` if no provider was used.
        tool_requests:  List of tool requests the provider wants the
                        caller to resolve. Empty if none.
        metadata:       Arbitrary provider-specific metadata.
    """

    text: str
    enabled: bool = False
    provider_name: str = ""
    tool_requests: list[ToolRequest] = field(default_factory=list)
    metadata: dict[str, Any] = field(default_factory=dict)


class AIInterface:
    """The single public entrypoint for AI interactions.

    Constructed once by the runtime supervisor and injected wherever
    needed. Callers should never import the registry or state directly —
    they go through this interface.

    Usage:
        ai = AIInterface(runtime_state, registry)
        response = ai.handle(context)
        if response.enabled:
            # use response.text
            ...
    """

    __slots__ = ("_runtime_state", "_registry")

    def __init__(self, runtime_state: AIRuntimeState, registry: ProviderRegistry) -> None:
        self._runtime_state = runtime_state
        self._registry = registry

    def handle(
        self,
        context: AIContext,
        tool_results: list[ToolResult] | None = None,
    ) -> AIResponse:
        """Process an AI request and return a response.

        This is the ONE method the rest of the codebase calls. It:
          1. Increments the request counter.
          2. Checks if AI is enabled in runtime state.
          3. If disabled, returns ``AI_DISABLED`` immediately.
          4. If enabled, resolves the active provider from the registry.
          5. If no provider is active, returns ``AI_DISABLED``.
          6. Delegates to ``provider.generate(context, tool_results)``.
          7. Updates session state (turn count, last response).
          8. Returns the ``AIResponse``.

        Currently always returns ``AI_DISABLED`` because no provider
        is registered and ``runtime_state.enabled`` defaults to False.
        """
        self._runtime_state.total_requests += 1

        if not self._runtime_state.enabled:
            return AIResponse(text=AI_DISABLED, enabled=False)

        provider = self._registry.get_active()
        if provider is None:
            return AIResponse(text=AI_DISABLED, enabled=False)

        try:
            response = provider.generate(context, tool_results)
        except Exception as exc:
            logger.error("AIInterface: provider '%s' raised: %s", provider.name, exc)
            return AIResponse(text=f"AI_ERROR: {exc}", enabled=True, provider_name=provider.name)

        self._runtime_state.total_responses += 1
        session = self._runtime_state.get_or_create_session(context.chat_id)
        session.turn_count += 1
        session.last_response_text = response.text
        session.last_tool_calls = [tr.request_id for tr in response.tool_requests]
        session.touch()

        return response

    def is_enabled(self) -> bool:
        """Check if AI is enabled and a provider is active."""
        return self._runtime_state.enabled and self._registry.get_active() is not None

    def enable(self) -> None:
        """Enable AI at the runtime level."""
        self._runtime_state.enabled = True
        logger.info("AIInterface: enabled")

    def disable(self) -> None:
        """Disable AI at the runtime level."""
        self._runtime_state.enabled = False
        logger.info("AIInterface: disabled")

    def get_session(self, chat_id: int):
        """Return the session state for a chat, or None."""
        return self._runtime_state.sessions.get(chat_id)

    def reset_session(self, chat_id: int) -> None:
        """Clear session state for a chat."""
        self._runtime_state.sessions.pop(chat_id, None)
