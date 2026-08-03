"""
AI Interface Layer — the stable contract between the Telegram Self-Bot
core and every future AI provider.

This package defines ONLY the interface. It contains:
  - Request / Response / Context data objects
  - Runtime state for AI sessions
  - Tool request / result placeholders
  - A provider protocol (the adapter contract)
  - A provider registry (dependency-injected, no globals)
  - AIInterface — the ONE public entrypoint

Currently the interface does NOTHING. ``AIInterface.handle(...)`` returns
``AI_DISABLED``. No model, no API calls, no prompts, no background tasks.

Future providers implement one adapter (the ``AIProvider`` protocol) and
register it with the registry. The interface and all callers stay unchanged.
"""
from backend.ai.interface import AIInterface
from backend.ai.context import AIContext
from backend.ai.state import AIRuntimeState, AISessionState
from backend.ai.provider import AIProvider, ToolRequest, ToolResult
from backend.ai.registry import ProviderRegistry

__all__ = [
    "AIInterface",
    "AIContext",
    "AIRuntimeState",
    "AISessionState",
    "AIProvider",
    "ToolRequest",
    "ToolResult",
    "ProviderRegistry",
]
