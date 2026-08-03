"""
Pipeline — the single execution pipeline for AI requests.

The pipeline wires every layer in the correct order. No layer is
bypassed. Each stage receives an immutable object and produces an
immutable object.

Pipeline stages (from AI_MASTER_DESIGN.md §4):

    Stage 1: Conversation Layer
        AIRequest → ConversationContext
        (via ConversationManager.build_context)

    Stage 2: Prompt Builder Layer
        ConversationContext → PromptPackage
        (via PromptBuilder.build)

    Stage 3: Provider Layer
        PromptPackage → ProviderResponse
        (via ProviderRegistry.default_provider.generate)

    Stage 4: Response Assembly
        ProviderResponse → AIResponse
        (wraps provider output with timing, tokens, metadata)

The pipeline is stateless. It receives a ``ConversationManager`` and a
``ProviderRegistry`` as constructor arguments (dependency injection).
No globals, no singletons, no side effects beyond the injected objects.

Currently, the DummyProvider is always the default, so every pipeline
run returns a deterministic ``AI_DISABLED`` response. No network call
is ever made.
"""
from __future__ import annotations

import logging
import time
from typing import Any

from backend.ai.conversation.context_builder import (
    ConversationContext,
    RuntimeContext,
)
from backend.ai.conversation.conversation import ConversationManager
from backend.ai.prompt.builder import PromptBuilder, PromptPackage
from backend.ai.providers.base import ProviderResponse
from backend.ai.providers.registry import ProviderRegistry
from backend.ai.session.request import AIRequest
from backend.ai.session.response import AIResponse

logger = logging.getLogger(__name__)

DUMMY_RESPONSE_TEXT = (
    "AI layer is operational.\n"
    "No external provider configured."
)


class Pipeline:
    """The single execution pipeline for AI requests.

    Wires Conversation → Prompt → Provider → Response. Each stage is
    independent and receives/produces immutable objects.

    Usage::

        pipeline = Pipeline(conversation_mgr, provider_registry)
        response = pipeline.execute(request)
        if response.success:
            # use response.text
            ...
    """

    __slots__ = ("_conversation", "_registry", "_prompt_builder")

    def __init__(
        self,
        conversation: ConversationManager,
        registry: ProviderRegistry,
        prompt_builder: PromptBuilder | None = None,
    ) -> None:
        self._conversation = conversation
        self._registry = registry
        self._prompt_builder = prompt_builder or PromptBuilder()

    def execute(self, request: AIRequest) -> AIResponse:
        """Run the full pipeline. Returns an immutable ``AIResponse``.

        This is the ONLY public method. It runs all four stages in
        order. If any stage fails, the pipeline returns an error
        ``AIResponse`` — it never raises to the caller.
        """
        start = time.monotonic()
        try:
            context = self._stage_conversation(request)
            package = self._stage_prompt(context)
            provider_response = self._stage_provider(package)
            response = self._stage_response(
                provider_response,
                request,
                package,
                start,
            )
            return response
        except Exception as exc:
            elapsed = time.monotonic() - start
            logger.error("Pipeline: error after %.3fs: %s", elapsed, exc)
            return AIResponse(
                success=False,
                error=str(exc),
                provider="",
                text="",
                execution_time=elapsed,
            )

    # ── Stage 1: Conversation Layer ──

    def _stage_conversation(self, request: AIRequest) -> ConversationContext:
        """Build the immutable ``ConversationContext`` from the request.

        Uses the ``ConversationManager`` to assemble session state,
        history, reply context, and runtime info into one frozen object.
        """
        runtime = RuntimeContext(
            ai_enabled=True,
            active_provider=self._registry.default_provider().name,
        )
        context = self._conversation.build_context(
            session_id=request.session_id,
            user_text=request.user_message,
            message_id=request.message_id,
            reply=request.reply_context,
            runtime=runtime,
        )
        logger.debug(
            "Pipeline stage 1 (conversation): session=%s state=%s",
            context.session_id,
            context.state.value,
        )
        return context

    # ── Stage 2: Prompt Builder Layer ──

    def _stage_prompt(self, context: ConversationContext) -> PromptPackage:
        """Build the immutable ``PromptPackage`` from the context.

        Uses the ``PromptBuilder`` to assemble all 9 prompt sections in
        fixed deterministic order.
        """
        package = self._prompt_builder.build(context)
        logger.debug(
            "Pipeline stage 2 (prompt): sections=%d tokens=%d",
            len(package.sections),
            package.estimated_tokens.estimated_total,
        )
        return package

    # ── Stage 3: Provider Layer ──

    def _stage_provider(self, package: PromptPackage) -> ProviderResponse:
        """Call the default provider with the prompt package.

        Currently always routes to ``DummyProvider``, which returns
        ``AI_DISABLED``. No network call is made.
        """
        provider = self._registry.default_provider()
        response = provider.generate(package)
        logger.debug(
            "Pipeline stage 3 (provider): name=%s success=%s text=%s",
            response.provider_name,
            response.success,
            response.text[:80],
        )
        return response

    # ── Stage 4: Response Assembly ──

    def _stage_response(
        self,
        provider_response: ProviderResponse,
        request: AIRequest,
        package: PromptPackage,
        start: float,
    ) -> AIResponse:
        """Assemble the final immutable ``AIResponse``.

        Wraps the provider's output with timing, token estimates, and
        metadata. The DummyProvider returns a deterministic message
        so the caller always gets a predictable result.
        """
        elapsed = time.monotonic() - start
        estimated_tokens = package.estimated_tokens.estimated_total

        if provider_response.success:
            text = provider_response.text
        else:
            text = DUMMY_RESPONSE_TEXT

        return AIResponse(
            success=True,
            error="",
            provider=provider_response.provider_name,
            text=text,
            estimated_tokens=estimated_tokens,
            execution_time=elapsed,
            tool_calls=list(provider_response.tool_calls),
            metadata={
                "session_id": request.session_id,
                "language": request.language,
                "provider_success": provider_response.success,
                "prompt_sections": len(package.sections),
            },
        )
