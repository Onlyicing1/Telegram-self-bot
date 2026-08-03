"""
AI Configuration Layer — provider-independent, RAM-only configuration.

Public API::

    from backend.ai.config import ConfigManager, ConfigSnapshot, AIConfig

    manager = ConfigManager()
    manager.set("temperature", 0.7)
    snapshot = manager.snapshot()   # immutable — pass to Engine
    manager.load_default()           # reset to factory defaults

The Engine receives only ``ConfigSnapshot`` objects. No downstream
layer ever sees the mutable ``AIConfig``.
"""
from backend.ai.config.config import AIConfig
from backend.ai.config.manager import ConfigManager
from backend.ai.config.schema import ConfigSnapshot
from backend.ai.config.validation import ConfigValidationError

__all__ = [
    "AIConfig",
    "ConfigManager",
    "ConfigSnapshot",
    "ConfigValidationError",
]
