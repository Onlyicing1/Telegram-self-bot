"""
AI Subsystem — the nested engine architecture for conversational AI.

This package contains the complete AI pipeline: engine, providers,
conversation management, prompt building, memory, tools, configuration,
and the runtime layer that manages conversation state.

The single public entry point for AI execution is the Engine:
    from backend.ai.engine import Engine, get_engine, engine_health

The AI subsystem is not wired into the main bot startup by default.
It is activated when AI_ENABLED is set and a provider API key is
configured. When no provider is available, the DummyProvider returns
a deterministic placeholder — no network calls are ever made.

Architecture overview (see AI_MASTER_DESIGN.md for full spec):

    AIRequest (immutable input)
        │
        ▼
    Engine (engine/engine.py) — the ONLY public entry point
        │
        ├── Dispatcher (engine/dispatcher.py) — 6-stage execution spine
        │     ├── Conversation Runtime (runtime/) — session, history, tokens
        │     ├── Prompt Builder (prompt/) — system prompt, context, budget
        │     ├── Provider Manager (providers/) — routing, fallback, metrics
        │     ├── Memory (memory/) — short, long, permanent tiers
        │     └── Tools (tools/) — registry, executor, context
        │
        └── EngineResult (engine/result.py) — immutable output

Dependencies:
    - backend.db.client — Supabase persistence (via persistence.py)
    - backend.diagnostics — event recording
    - backend.config — env-based configuration

What it does NOT do:
    - Modify Telegram command behavior
    - Change the bot's existing features
    - Run without explicit enabling
"""
from backend.ai.engine.engine import Engine, engine_health, get_engine
from backend.ai.session.request import AIRequest

__all__ = [
    "Engine",
    "get_engine",
    "engine_health",
    "AIRequest",
]
