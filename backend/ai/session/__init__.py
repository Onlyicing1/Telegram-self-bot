"""
AI Session Layer — request and response types for the AI pipeline.

Only AIRequest is actively used (by both the Engine and the Telegram
handler). The session lifecycle is managed by the Engine itself.

Public API:
    AIRequest — the immutable input object for every AI execution
"""
from backend.ai.session.request import AIRequest

__all__ = ["AIRequest"]
