"""
Tool base contract — the deterministic bridge between AI and the Self-Bot.

Every tool is a thin wrapper around an existing service function. The AI
never imports services directly; it calls tools. Tools never contain
business logic — they delegate to services.

This module defines:
  - ``PermissionLevel``  — safety classification for tools
  - ``ToolResult``       — the structured return value of every tool
  - ``Tool``             — the abstract base class every tool inherits

The ``Tool`` contract (from AI_MASTER_DESIGN.md §6.1):

    name: str              # unique identifier
    description: str      # what it does (shown to the model)
    parameters: dict       # JSON schema for arguments
    permission_level: str # safety classification
    safe: bool             # safe / dangerous shorthand
    return_type: str       # description of the return data shape

    async def execute(self, context, arguments) -> ToolResult

Adding a new tool takes under five minutes:

    1. Create a class inheriting from ``Tool``.
    2. Set the metadata fields (name, description, etc.).
    3. Implement ``execute(context, arguments)`` — call the existing
       service function and wrap its return value in a ``ToolResult``.
    4. Register it in ``registry.create_default_registry()``.

No other file needs to change. The Prompt Builder picks up tools from
the registry automatically.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Protocol, runtime_checkable


class PermissionLevel(str, Enum):
    """Safety classification for tools.

    The AI Core checks this before executing a tool:
      - READ_ONLY / READ_WRITE  → AI can call autonomously.
      - DANGEROUS               → AI must ask the owner first.
      - ADMIN_ONLY              → AI must ask the owner first.
      - CONFIRMATION_REQUIRED   → Always ask, regardless of base level.
    """
    READ_ONLY = "read_only"
    READ_WRITE = "read_write"
    DANGEROUS = "dangerous"
    ADMIN_ONLY = "admin_only"
    CONFIRMATION_REQUIRED = "confirmation_required"


@dataclass(frozen=True)
class ToolResult:
    """Structured result returned by every tool execution.

    Attributes:
        success:  Whether the action completed without error.
        message:  Human-readable result text (for the AI to relay).
        data:      Optional structured payload for the AI to reason over.
    """
    success: bool
    message: str
    data: dict[str, Any] = field(default_factory=dict)


# Service-layer failure prefixes. Services communicate failures as
# "❌ ..." / "⚠️ ..." result strings instead of raising. Tools must
# derive ``success`` from these prefixes — a service returning a string
# is NOT proof the operation succeeded.
_SERVICE_FAILURE_PREFIXES = ("❌", "⚠️", "🚫")


def result_from_service(result: str, *, data: dict[str, Any] | None = None) -> ToolResult:
    """Wrap a service-layer result string, deriving success from the outcome.

    Services communicate failures as "❌ ..." / "⚠️ ..." strings. A tool
    that merely returned without raising must never report success: the AI
    answers from the REAL result, so failures are surfaced as
    ``success=False`` with the actual service message.
    """
    text = str(result)
    failed = text.startswith(_SERVICE_FAILURE_PREFIXES)
    return ToolResult(success=not failed, message=text, data=data or {})


@runtime_checkable
class Tool(Protocol):
    """The contract every tool must implement.

    Tools are stateless wrappers. They receive a ``ToolContext`` (which
carries the Telethon client, owner ID, and timezone) and a dict of
arguments (matching the tool's parameter schema). They delegate to
the existing service layer and return a ``ToolResult``.

    Tools MUST NOT:
      - Import or use global state.
      - Hold references to the client beyond a single execute call.
      - Re-implement logic that already lives in a service.
      - Send Telegram messages directly (use the service layer).
    """

    @property
    def name(self) -> str:
        """Globally unique tool identifier (e.g. ``"save"``)."""
        ...

    @property
    def description(self) -> str:
        """Short description shown to the AI model in the prompt."""
        ...

    @property
    def parameters(self) -> dict[str, Any]:
        """JSON-schema-style dict describing the expected arguments."""
        ...

    @property
    def permission_level(self) -> PermissionLevel:
        """Safety classification — determines if AI can call autonomously."""
        ...

    @property
    def safe(self) -> bool:
        """Shorthand: True if permission_level is READ_ONLY or READ_WRITE."""
        ...

    @property
    def return_type(self) -> str:
        """Human-readable description of the structured data shape."""
        ...

    async def execute(self, context: "ToolContext", arguments: dict[str, Any]) -> ToolResult:
        """Perform the action and return a structured result.

        Args:
            context:   Injected runtime context (client, owner_id, tz_str).
            arguments: Parsed arguments matching the tool's parameter schema.
        """
        ...
