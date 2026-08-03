"""
AI Tool Registry package.

This package contains the deterministic bridge between the AI layer
and the existing Self-Bot features. Every tool is a thin wrapper around
an existing service function. The AI never imports services directly.

Public API:
    from backend.ai.tools import ToolRegistry, ToolContext, create_default_registry

To add a new tool:
    1. Create a class inheriting from ``Tool`` in a new file here.
    2. Set metadata (name, description, parameters, permission_level, etc.).
    3. Implement ``execute(context, arguments)`` — delegate to the service.
    4. Register it in ``registry.create_default_registry()``.

No other file needs to change.
"""
from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext
from backend.ai.tools.registry import ToolRegistry, create_default_registry

__all__ = [
    "PermissionLevel",
    "Tool",
    "ToolResult",
    "ToolContext",
    "ToolRegistry",
    "create_default_registry",
]
