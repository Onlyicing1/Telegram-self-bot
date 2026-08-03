"""
AIInspector — the single public entry point for AI runtime diagnostics.

The inspector is a temporary developer-only layer. It reads the live
AI runtime objects (AISession and its components) and produces one
immutable ``RuntimeSnapshot`` containing only diagnostic information.

Constraints:
  - NEVER calls Telegram APIs.
  - No polling, no loops, no scheduler.
  - No network, no provider execution, no SDK.
  - No secrets, no API keys, no prompts, no user messages.
  - Does NOT modify any existing runtime — read-only.

The single public method is ``get_ai_runtime_snapshot()``, which
returns a frozen ``RuntimeSnapshot``.

The snapshot is designed to be displayed inside the AI menu (future)
or logged during development. After the first real provider is
integrated and verified, this inspector can be disabled or hidden
behind Developer Mode.
"""
from __future__ import annotations

import logging
from datetime import datetime, timezone

from backend.ai.conversation.conversation import ConversationManager
from backend.ai.inspector.health import HealthChecker
from backend.ai.inspector.snapshot import LayerInfo, RuntimeSnapshot
from backend.ai.inspector.status import HealthState, LayerStatus
from backend.ai.prompt.builder import PromptBuilder
from backend.ai.providers.registry import ProviderRegistry
from backend.ai.session.manager import SessionManager
from backend.ai.session.session import AISession

logger = logging.getLogger(__name__)


class AIInspector:
    """Read-only inspector for the AI runtime.

    Constructed with a reference to the live ``AISession`` (or its
    components). The inspector never modifies any runtime object — it
    only reads safe, public attributes to assemble a diagnostics
    snapshot.

    Usage::

        inspector = AIInspector(ai_session)
        snapshot = inspector.get_ai_runtime_snapshot()
        # snapshot.health → HealthState.HEALTHY
        # snapshot.current_provider → "dummy"
    """

    __slots__ = ("_ai_session", "_health")

    def __init__(self, ai_session: AISession) -> None:
        self._ai_session = ai_session
        self._health = HealthChecker(
            conversation=ai_session.get_conversation(),
            registry=ai_session.get_registry(),
            session_mgr=ai_session._session_mgr,  # noqa: SLF001
            prompt_builder=ai_session._pipeline._prompt_builder,  # noqa: SLF001
        )

    def get_ai_runtime_snapshot(self) -> RuntimeSnapshot:
        """Return one immutable ``RuntimeSnapshot`` of the AI runtime.

        This is the ONLY public method. It probes each layer, checks
        context/prompt validity, reads the current provider and session
        state, and assembles everything into a frozen snapshot.

        The snapshot contains only diagnostics — no secrets, no
        prompts, no user messages.
        """
        conv_info = self._health.check_conversation()
        prompt_info = self._health.check_prompt_builder()
        registry_info = self._health.check_provider_registry()
        pipeline_info = self._health.check_pipeline()
        session_info = self._health.check_session()

        layers = [
            conv_info,
            prompt_info,
            registry_info,
            pipeline_info,
            session_info,
        ]

        overall_health = self._health.compute_overall_health(layers)

        current_provider = self._get_current_provider()
        provider_loaded = "YES" if current_provider else "NO"
        session_state = self._get_session_state(session_info)
        context_valid = self._check_context_validity()
        prompt_valid = self._check_prompt_validity()
        estimated_size = self._get_estimated_prompt_size()
        last_exec = self._get_last_execution()

        return RuntimeSnapshot(
            conversation_layer=conv_info,
            prompt_builder=prompt_info,
            provider_registry=registry_info,
            pipeline=pipeline_info,
            session=session_info,
            current_provider=current_provider,
            provider_loaded=provider_loaded,
            session_state=session_state,
            context_object=context_valid,
            prompt_package=prompt_valid,
            estimated_prompt_size=estimated_size,
            last_execution=last_exec,
            health=overall_health,
            layers=layers,
            metadata={
                "inspector": "AIInspector v1 (temporary)",
                "single_session": True,
            },
        )

    def _get_current_provider(self) -> str:
        """Return the name of the current default provider."""
        try:
            provider = self._ai_session.get_registry().default_provider()
            return provider.name
        except Exception:
            return ""

    def _get_session_state(self, session_info: LayerInfo) -> str:
        """Return a human-readable session state."""
        if session_info.status == LayerStatus.ERROR:
            return "Error"
        if session_info.detail:
            return session_info.detail.capitalize()
        return "Idle"

    def _check_context_validity(self) -> str:
        """Check whether a ConversationContext can be built.

        Does NOT actually build one — just checks that the conversation
        manager and registry are operational.
        """
        try:
            conv = self._ai_session.get_conversation()
            registry = self._ai_session.get_registry()
            if conv is None or registry is None:
                return "INVALID"
            provider = registry.default_provider()
            if provider is None:
                return "INVALID"
            return "VALID"
        except Exception:
            return "INVALID"

    def _check_prompt_validity(self) -> str:
        """Check whether a PromptPackage can be built.

        Does NOT actually build one — just checks that the prompt
        builder exists and is callable.
        """
        try:
            builder = self._ai_session._pipeline._prompt_builder  # noqa: SLF001
            if builder is None or not callable(getattr(builder, "build", None)):
                return "INVALID"
            return "VALID"
        except Exception:
            return "INVALID"

    def _get_estimated_prompt_size(self) -> int:
        """Return the estimated prompt size from the last pipeline run.

        Returns 0 if no pipeline run has occurred yet.
        """
        try:
            session_id = self._ai_session.active_session()
            if session_id is None:
                return 0
            session = self._ai_session.get_session(session_id)
            if session is None:
                return 0
            return session.get("last_estimated_tokens", 0)
        except Exception:
            return 0

    def _get_last_execution(self) -> str:
        """Return ISO timestamp of the last pipeline run, or empty string."""
        try:
            session_id = self._ai_session.active_session()
            if session_id is None:
                return ""
            session = self._ai_session.get_session(session_id)
            if session is None:
                return ""
            ts = session.get("last_execution", "")
            return ts
        except Exception:
            return ""
