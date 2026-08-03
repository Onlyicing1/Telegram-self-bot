"""
HealthChecker — checks each AI layer's readiness without executing the pipeline.

The HealthChecker probes each layer by calling safe, read-only methods.
It never calls ``generate()``, ``process()``, or any Telegram API. It
never builds a real prompt or sends a real request. Each probe is
wrapped in a try/except so a broken layer produces ``LayerStatus.ERROR``
instead of crashing the inspector.

Probes:
  - Conversation Layer:  Can ``ConversationManager`` list sessions?
  - Prompt Builder:      Can ``PromptBuilder`` be instantiated?
  - Provider Registry:    Does ``default_provider()`` return a provider?
  - Pipeline:             Does the pipeline object exist and have an ``execute`` method?
  - Session:             Is there an active session, and what state is it in?
"""
from __future__ import annotations

import logging

from backend.ai.conversation.conversation import ConversationManager
from backend.ai.inspector.snapshot import LayerInfo
from backend.ai.inspector.status import LayerStatus
from backend.ai.prompt.builder import PromptBuilder
from backend.ai.providers.registry import ProviderRegistry
from backend.ai.session.manager import SessionManager
from backend.ai.session.state import AISessionState

logger = logging.getLogger(__name__)


class HealthChecker:
    """Probes each AI layer for readiness. No side effects, no I/O.

    Constructed with references to the live runtime objects (injected,
    not globals). Each ``check_*`` method returns a ``LayerInfo`` with
    the layer's status and optional detail string.
    """

    __slots__ = (
        "_conversation",
        "_registry",
        "_session_mgr",
        "_prompt_builder",
    )

    def __init__(
        self,
        conversation: ConversationManager,
        registry: ProviderRegistry,
        session_mgr: SessionManager,
        prompt_builder: PromptBuilder | None = None,
    ) -> None:
        self._conversation = conversation
        self._registry = registry
        self._session_mgr = session_mgr
        self._prompt_builder = prompt_builder or PromptBuilder()

    def check_conversation(self) -> LayerInfo:
        """Probe the Conversation Layer."""
        try:
            sessions = self._conversation.list_sessions()
            return LayerInfo(
                name="Conversation Layer",
                status=LayerStatus.READY,
                detail=f"{len(sessions)} session(s)",
            )
        except Exception as exc:
            logger.warning("HealthChecker: conversation layer error: %s", exc)
            return LayerInfo(
                name="Conversation Layer",
                status=LayerStatus.ERROR,
                detail=str(exc)[:100],
            )

    def check_prompt_builder(self) -> LayerInfo:
        """Probe the Prompt Builder Layer."""
        try:
            builder = self._prompt_builder
            if builder is None:
                return LayerInfo(
                    name="Prompt Builder",
                    status=LayerStatus.OFFLINE,
                )
            return LayerInfo(
                name="Prompt Builder",
                status=LayerStatus.READY,
            )
        except Exception as exc:
            logger.warning("HealthChecker: prompt builder error: %s", exc)
            return LayerInfo(
                name="Prompt Builder",
                status=LayerStatus.ERROR,
                detail=str(exc)[:100],
            )

    def check_provider_registry(self) -> LayerInfo:
        """Probe the Provider Registry."""
        try:
            provider = self._registry.default_provider()
            names = self._registry.list()
            return LayerInfo(
                name="Provider Registry",
                status=LayerStatus.READY,
                detail=f"providers={names}, default={provider.name}",
            )
        except Exception as exc:
            logger.warning("HealthChecker: provider registry error: %s", exc)
            return LayerInfo(
                name="Provider Registry",
                status=LayerStatus.ERROR,
                detail=str(exc)[:100],
            )

    def check_pipeline(self) -> LayerInfo:
        """Probe the Pipeline.

        The pipeline is not directly injected here — we infer its
        readiness from the conversation manager and registry being
        READY. The pipeline object itself is checked by the inspector.
        """
        conv = self.check_conversation()
        prov = self.check_provider_registry()
        if conv.status == LayerStatus.READY and prov.status == LayerStatus.READY:
            return LayerInfo(
                name="Pipeline",
                status=LayerStatus.READY,
            )
        return LayerInfo(
            name="Pipeline",
            status=LayerStatus.NOT_READY,
            detail="depends on conversation + provider",
        )

    def check_session(self) -> LayerInfo:
        """Probe the AI Session."""
        try:
            active = self._session_mgr.active_session()
            if active is None:
                return LayerInfo(
                    name="Session",
                    status=LayerStatus.READY,
                    detail="Idle",
                )
            session = self._session_mgr.get_session(active)
            if session is None:
                return LayerInfo(
                    name="Session",
                    status=LayerStatus.NOT_READY,
                    detail="active ID but no session",
                )
            state = session.get("state", AISessionState.CREATED)
            return LayerInfo(
                name="Session",
                status=LayerStatus.READY,
                detail=state.value,
            )
        except Exception as exc:
            logger.warning("HealthChecker: session error: %s", exc)
            return LayerInfo(
                name="Session",
                status=LayerStatus.ERROR,
                detail=str(exc)[:100],
            )

    def compute_overall_health(
        self,
        layers: list[LayerInfo],
    ) -> "HealthState":
        """Compute overall pipeline health from individual layer statuses."""
        from backend.ai.inspector.status import HealthState

        if any(l.status == LayerStatus.ERROR for l in layers):
            return HealthState.ERROR
        if any(l.status == LayerStatus.OFFLINE for l in layers):
            return HealthState.OFFLINE
        if any(l.status == LayerStatus.NOT_READY for l in layers):
            return HealthState.DEGRADED
        return HealthState.HEALTHY
