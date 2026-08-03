"""
AI Engine — the single public entry point for AI execution.

Public API::

    from backend.ai.engine import Engine, EngineResult, engine_health
    from backend.ai.session.request import AIRequest

    engine = Engine()
    result = engine.execute(request)
    health = engine.engine_health()   # "READY" or "FAILED: <reason>"

Execution flow (fixed, no shortcuts)::

    Conversation Runtime → Prompt Builder → Provider Factory
    → Provider → Response → Conversation Update → Result

The active provider is always the DummyProvider. No HTTP, no SDK, no
external API. Everything executes offline and deterministically.
"""
from backend.ai.engine.engine import Engine, engine_health, get_engine
from backend.ai.engine.hooks import EngineHooks
from backend.ai.engine.metrics import EngineMetrics
from backend.ai.engine.result import EngineResult

__all__ = [
    "Engine",
    "EngineResult",
    "EngineMetrics",
    "EngineHooks",
    "engine_health",
    "get_engine",
]
