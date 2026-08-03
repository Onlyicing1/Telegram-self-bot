"""
AIProvider protocol and tool placeholders.

The ``AIProvider`` protocol is the single contract every future AI
adapter must implement. The interface layer calls ``provider.generate()``
and receives an ``AIResponse``. The provider never interacts with
Telegram directly — it receives an ``AIContext`` and returns data.

Tool placeholders define the shape of tool calls. When a provider wants
to invoke a tool (e.g. search the database, send a message), it returns
a ``ToolRequest``. The caller resolves it and passes back a
``ToolResult``. This indirection keeps providers sandboxed: they never
touch Telegram or the database directly.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Protocol, runtime_checkable

from backend.ai.context import AIContext


# ── Tool placeholders ──

@dataclass(frozen=True)
class ToolRequest:
    """A request from the AI provider to invoke a tool.

    The provider returns this object when it wants the caller to perform
    an action (e.g. "search saved items", "send a message"). The caller
    resolves the request and returns a ``ToolResult``.

    Attributes:
        tool_name:   Identifier of the requested tool (e.g. ``"search_saves"``).
        arguments:   Tool-specific arguments as a dict. The caller
                     interprets these based on ``tool_name``.
        request_id:  Unique ID for this request, used to correlate
                     with the ``ToolResult``.
    """

    tool_name: str
    arguments: dict[str, Any] = field(default_factory=dict)
    request_id: str = ""


@dataclass(frozen=True)
class ToolResult:
    """The result of a tool invocation, returned to the provider.

    Attributes:
        request_id:  Matches the ``ToolRequest.request_id``.
        success:     Whether the tool call succeeded.
        data:         Tool-specific result payload.
        error:        Error message if ``success`` is False.
    """

    request_id: str
    success: bool
    data: dict[str, Any] = field(default_factory=dict)
    error: str = ""


# ── Provider protocol ──

@runtime_checkable
class AIProvider(Protocol):
    """The contract every AI adapter must implement.

    A provider is a stateless adapter that receives a context and an
    optional list of tool results, and returns a response. It never
    touches Telegram, the database, or any global state directly.

    Implementations are registered with ``ProviderRegistry`` and
    activated by name. The ``AIInterface`` delegates to the active
    provider.
    """

    @property
    def name(self) -> str:
        """Unique provider identifier (e.g. ``"openai"``, ``"gemini"``)."""
        ...

    def generate(
        self,
        context: AIContext,
        tool_results: list[ToolResult] | None = None,
    ) -> "AIResponse":
        """Process the context and return an AI response.

        If ``tool_results`` is provided, the provider should incorporate
        them into its reasoning and produce a final (or follow-up) response.

        This method MUST be synchronous and non-blocking. If the underlying
        model requires network I/O, the adapter should wrap it in an async
        helper and expose a sync wrapper, or the protocol should be
        extended to ``async def`` in a future revision. For now, the
        contract is sync to keep the interface simple.
        """
        ...
