"""
AI Session Layer — the single visible AI runtime.

This package is the first layer that makes AI *visible* as a runtime
object. It wires every previous layer (Conversation, Prompt Builder,
Provider) into a single execution pipeline and exposes a simple
``AISession.process(request) → response`` API.

Currently, the DummyProvider is always the default. Every ``process()``
call returns a deterministic response:

    "AI layer is operational.
    No external provider configured."

No network call is ever made. No SDK, no secrets, no environment
variables. No existing feature (menu, panel, save, delete, bio,
username, retrieve) is modified or affected.

Public API::

    from backend.ai.session import (
        AISession,
        AIRequest,
        AIResponse,
        AISessionState,
        SessionManager,
        Pipeline,
    )

Architecture (from AI_MASTER_DESIGN.md §4)::

    AIRequest (immutable)
        │
        ▼
    ┌──────────────────────────────────────────────┐
    │                Pipeline                        │
    │                                               │
    │  Stage 1: Conversation Layer                  │
    │    AIRequest → ConversationContext            │
    │    (via ConversationManager.build_context)    │
    │                                               │
    │  Stage 2: Prompt Builder Layer                 │
    │    ConversationContext → PromptPackage         │
    │    (via PromptBuilder.build)                   │
    │                                               │
    │  Stage 3: Provider Layer                       │
    │    PromptPackage → ProviderResponse            │
    │    (via ProviderRegistry.default_provider)     │
    │                                               │
    │  Stage 4: Response Assembly                    │
    │    ProviderResponse → AIResponse               │
    │    (wraps with timing, tokens, metadata)       │
    └──────────────────────────────────────────────┘
        │
        ▼
    AIResponse (immutable)

Execution lifecycle::

    1. Caller creates AISession (once, at startup).
    2. Caller creates a session: ai.create_session(owner_id, chat_id).
    3. Caller builds an AIRequest with the user's message.
    4. Caller calls ai.process(request).
    5. Pipeline runs all 4 stages in order.
    6. Caller receives an AIResponse.
    7. Session stays open for the next request (repeat from step 3).
    8. When done: ai.destroy_session(session_id).
"""
from backend.ai.session.manager import SessionManager
from backend.ai.session.pipeline import Pipeline
from backend.ai.session.request import AIRequest
from backend.ai.session.response import AIResponse
from backend.ai.session.session import AISession
from backend.ai.session.state import AISessionState

__all__ = [
    "AISession",
    "AIRequest",
    "AIResponse",
    "AISessionState",
    "SessionManager",
    "Pipeline",
]
