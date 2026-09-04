"""
Dispatcher — the execution spine of the AI Engine.

The dispatcher receives an ``AIRequest`` and drives it through every
layer in the exact, fixed order:

    1. Conversation Runtime  → ConversationContext
    2. Prompt Builder        → PromptPackage
    3. Provider Manager      → active Provider name
    4. Provider              → ProviderResponse
    5. Conversation Update   → history + tokens recorded
    6. Result                → EngineResult

No layer is skipped. Any exception raised inside a layer is caught and
converted into an ``EngineResult(success=False)`` — the engine never
propagates an uncaught exception.

The dispatcher measures wall-clock latency for the whole run, invokes
hooks at each lifecycle point, and records metrics. It owns no state
of its own beyond what is injected (conversation manager, prompt
builder, provider manager, hooks, metrics).
"""
from __future__ import annotations

import asyncio
import json
import logging
import time
from collections.abc import Awaitable, Callable
from dataclasses import replace
from typing import Any

from backend.ai.actions import parse_action_text, parse_command_intent
from backend.ai.confirmation import (
    CONFIRMATION_ALREADY_PENDING_TEXT,
    PendingConfirmationStore,
    _expired_text,
    confirmation_request_text,
    is_explicit_confirmation,
)
from backend.ai.engine.hooks import NOOP_HOOKS, EngineHooks, safe_call
from backend.ai.engine.metrics import EngineMetrics
from backend.ai.engine.result import EngineResult
from backend.ai.engine.telemetry import telemetry
from backend.ai.memory.limits import MEMORY_READ_TIMEOUT_S
from backend.ai.prompt.builder import PromptBuilder
from backend.ai.providers.base import ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.providers.registry.registry import ProviderRegistry
from backend.ai.runtime.manager import ConversationManager
from backend.ai.session.request import AIRequest
from backend.ai.tools.executor import ToolExecutor, ToolExecutionResult
from backend.ai.tools.registry import ToolRegistry, create_default_registry
from backend.ai.tools.context import ToolContext

logger = logging.getLogger(__name__)

MAX_TOOL_ROUNDS = 3

# Deterministic READ tools whose successful results are authoritative and
# must be delivered to the user EXACTLY as the tool produced them — never
# re-interpreted, stylized, or replaced by a continuation provider round.
_VERBATIM_READ_TOOLS = frozenset({"get_bio"})


_BLOCKED_FINISH_TOKENS = ("SAFETY", "RECITATION", "CONTENT_FILTER", "BLOCKED")
_TRUNCATED_FINISH_TOKENS = ("MAX_TOKENS", "LENGTH", "TRUNCATED")

# Appended as an extra user turn for the bounded recovery retry. It nudges the
# model to emit a structured tool call / JSON action instead of prose, without
# altering conversation history. Any action still passes through the local
# parser + validator before execution, so the nudge never weakens safety.
_ENFORCE_ACTION_NUDGE = (
    "If the user's request requires an action (save, delete, list/search saved "
    "items, retrieve a saved item, database status, bio/username status, task "
    "list/inspect/transition, or reviewing recent Telegram messages), respond "
    "with ONLY a native tool call or a single JSON action object — no prose, "
    "no questions, no permission explanations. If the request is purely "
    "conversational, answer normally."
)


def _is_blocked_finish(reason: str) -> bool:
    """True when the provider finish reason indicates a blocked response."""
    upper = (reason or "").upper()
    return any(tok in upper for tok in _BLOCKED_FINISH_TOKENS)


def _is_truncated_finish(reason: str) -> bool:
    """True when the provider finish reason indicates token truncation."""
    upper = (reason or "").upper()
    return any(tok in upper for tok in _TRUNCATED_FINISH_TOKENS)


def _accumulate_usage(target: dict[str, int], source: dict[str, Any]) -> None:
    """Add ``source``'s reported usage into ``target`` once.

    Missing/zero fields contribute nothing; nothing is ever invented.
    """
    for key in target:
        target[key] += int(source.get(key, 0) or 0)


class Dispatcher:
    """Drives an ``AIRequest`` through every AI layer and returns an ``EngineResult``."""

    __slots__ = (
        "_conversation",
        "_prompt_builder",
        "_providers",
        "_provider_manager",
        "_hooks",
        "_metrics",
        "_tool_registry",
        "_tool_executor",
        "_memory_manager",
        "_confirmation_store",
    )

    def __init__(
        self,
        conversation: ConversationManager,
        prompt_builder: PromptBuilder,
        providers: ProviderRegistry | ProviderManager,
        hooks: EngineHooks | None = None,
        metrics: EngineMetrics | None = None,
        tool_registry: ToolRegistry | None = None,
        tool_executor: ToolExecutor | None = None,
        memory_manager: Any | None = None,
    ) -> None:
        self._conversation = conversation
        self._prompt_builder = prompt_builder
        if isinstance(providers, ProviderManager):
            self._provider_manager = providers
            self._providers = providers.registry
        else:
            self._provider_manager = ProviderManager(providers)
            self._providers = providers
        self._hooks = hooks or NOOP_HOOKS
        self._metrics = metrics or EngineMetrics()
        self._tool_registry = tool_registry
        self._tool_executor = tool_executor
        self._memory_manager = memory_manager
        # Bounded in-memory pending-owner-approval state (see
        # backend/ai/confirmation.py). Lives on the Dispatcher because the
        # Dispatcher is the orchestration boundary that creates and consumes
        # confirmations; per-instance ownership also isolates tests.
        self._confirmation_store = PendingConfirmationStore()

    @property
    def metrics(self) -> EngineMetrics:
        return self._metrics

    def set_tool_executor(self, executor: ToolExecutor | None) -> None:
        """Attach or replace the ToolExecutor used by the tool loop.

        Called by the Engine when tools are wired at runtime so the
        dispatcher never runs with a stale ``None`` executor reference.
        """
        self._tool_executor = executor

    def set_tool_registry(self, registry: ToolRegistry | None) -> None:
        """Attach the registry used for provider tool definitions."""
        self._tool_registry = registry

    async def dispatch(
        self,
        request: AIRequest,
        status_callback: Callable[[str], Awaitable[None]] | None = None,
    ) -> EngineResult:
        """Execute ``request`` through the full pipeline. Never raises."""
        start = time.perf_counter()
        warnings: list[str] = []
        errors: list[str] = []
        metadata: dict[str, Any] = {"stages": []}

        rid = getattr(request, "request_id", "") or ""

        def _stage(stage: str) -> None:
            if not rid:
                return
            try:
                from backend.ai import diagnostics as ai_diag
                ai_diag.set_stage(rid, stage)
            except Exception:
                pass

        def _mark_success(stage: str) -> None:
            try:
                from backend.ai import diagnostics as ai_diag
                ai_diag.mark_success(stage)
            except Exception:
                pass

        def _provider_failure_type(candidate: ProviderResponse) -> str:
            candidate_metadata = candidate.metadata or {}
            if candidate_metadata.get("failure_type"):
                return str(candidate_metadata["failure_type"])
            if candidate_metadata.get("fallback_exhausted"):
                return "fallback_exhausted"
            return "provider_failure" if not candidate.success else ""

        provider_call_count = 0
        provider_total_elapsed = 0.0
        provider_first_started_at = 0.0

        async def _provider_chat(
            provider_messages: list[dict[str, Any]],
            *,
            call_label: str,
            **kwargs: Any,
        ) -> ProviderResponse:
            """Call the existing provider mesh and update one request record."""
            nonlocal provider_call_count, provider_total_elapsed, provider_first_started_at
            provider_call_count += 1
            started_at = time.perf_counter()
            started_wall = time.time()
            if provider_first_started_at == 0.0:
                provider_first_started_at = started_wall
            try:
                from backend.ai import diagnostics as ai_diag
                ai_diag.update_request(
                    rid,
                    provider_started_at=provider_first_started_at,
                    provider_last_started_at=started_wall,
                    provider_call_count=provider_call_count,
                    provider_call_label=call_label,
                )
            except Exception:
                pass
            logger.info(
                "AI_PROVIDER_CALL_START id=%s call=%s attempt=%d",
                rid or "-", call_label, provider_call_count,
            )
            try:
                candidate = await self._provider_manager.chat(provider_messages, **kwargs)
            except asyncio.CancelledError:
                elapsed = time.perf_counter() - started_at
                provider_total_elapsed += elapsed
                try:
                    from backend.ai import diagnostics as ai_diag
                    ai_diag.update_request(
                        rid,
                        provider_completed=False,
                        provider_cancelled=True,
                        provider_cancellation_requested=True,
                        provider_cancellation_completed=True,
                        provider_last_elapsed_s=round(elapsed, 3),
                        provider_elapsed_s=round(provider_total_elapsed, 3),
                        provider_failure_type="cancelled",
                        provider_failed_at=time.time(),
                    )
                except Exception:
                    pass
                logger.warning(
                    "AI_PROVIDER_CALL_CANCELLED id=%s call=%s elapsed_s=%.3f",
                    rid or "-", call_label, elapsed,
                )
                raise
            except Exception as exc:
                elapsed = time.perf_counter() - started_at
                provider_total_elapsed += elapsed
                try:
                    from backend.ai import diagnostics as ai_diag
                    ai_diag.update_request(
                        rid,
                        provider_completed=False,
                        provider_failed=True,
                        provider_last_elapsed_s=round(elapsed, 3),
                        provider_elapsed_s=round(provider_total_elapsed, 3),
                        provider_failure_type="exception",
                        provider_exception_type=type(exc).__name__,
                        provider_failed_at=time.time(),
                    )
                except Exception:
                    pass
                logger.warning(
                    "AI_PROVIDER_CALL_FAILURE id=%s call=%s exception_type=%s elapsed_s=%.3f",
                    rid or "-", call_label, type(exc).__name__, elapsed,
                )
                raise

            elapsed = time.perf_counter() - started_at
            provider_total_elapsed += elapsed
            candidate_metadata = candidate.metadata or {}
            candidate_failure = _provider_failure_type(candidate)
            try:
                from backend.ai import diagnostics as ai_diag
                ai_diag.update_request(
                    rid,
                    provider_completed=True,
                    provider_failed=not candidate.success,
                    provider_last_completed_at=time.time(),
                    provider_last_elapsed_s=round(elapsed, 3),
                    provider_elapsed_s=round(provider_total_elapsed, 3),
                    provider_result_status="success" if candidate.success else "failure",
                    provider_failure_type=candidate_failure,
                    provider_fallback_used=bool(candidate_metadata.get("fallback")),
                    fallback_exhausted=bool(candidate_metadata.get("fallback_exhausted")),
                    provider_matrix_size=(
                        len(candidate_metadata.get("provider_matrix", []))
                        if isinstance(candidate_metadata.get("provider_matrix"), list)
                        else 0
                    ),
                )
            except Exception:
                pass
            logger.info(
                "AI_PROVIDER_CALL_COMPLETE id=%s call=%s provider=%s success=%s elapsed_s=%.3f failure_type=%s fallback=%s",
                rid or "-",
                call_label,
                candidate.provider_name or "-",
                candidate.success,
                elapsed,
                candidate_failure or "-",
                bool(candidate_metadata.get("fallback") or candidate_metadata.get("fallback_exhausted")),
            )
            return candidate

        safe_call(self._hooks, "before_execution", request)

        # Native tool definitions are computed once and passed to every
        # provider round only when this request permits tools. Providers
        # translate this OpenAI-format list into their own API shape (e.g.
        # Gemini functionDeclarations).
        tools_allowed = bool(getattr(request, "allow_tools", True))
        tool_definitions = self._build_tool_definitions() if tools_allowed else []
        chat_kwargs: dict[str, Any] = {"tools": tool_definitions} if tool_definitions else {}
        logger.info(
            "AI_TOOL_AVAILABILITY id=%s enabled=%s tools=%d names=%s",
            rid or "-",
            tools_allowed,
            len(tool_definitions),
            [d["function"]["name"] for d in tool_definitions],
        )

        # ── Stage 1: Conversation Runtime ──
        try:
            session = self._conversation.get_session(request.owner_id)
            if session is None:
                session = self._conversation.create_session(
                    owner_id=request.owner_id, session_id=request.session_id or None
                )
            await self._conversation.restore_history(
                owner_id=request.owner_id, session_id=session.session_id
            )
            if request.user_message:
                self._conversation.add_user_message(
                    owner_id=request.owner_id, content=request.user_message
                )
            metadata["stages"].append("conversation_runtime")
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "conversation_runtime", start, errors, metadata)

        # ── Pending owner confirmation (deterministic, BEFORE any provider ──
        # ── round) ──
        # An explicit confirmation reply to an earlier owner-only tool
        # request is consumed here. The ORIGINAL stored call (frozen tool
        # name + arguments) is re-issued through the SAME ToolExecutor. The
        # model never re-parses the confirmation message, so a pending
        # action can never be re-targeted or re-argued by prose. When no
        # pending action exists, normal (provider) flow continues untouched
        # — conversational yes/no exchanges are never hijacked.
        if tools_allowed and self._tool_executor is not None:
            confirmation = await self._try_consume_confirmation(
                request, rid, status_callback, start, metadata,
            )
            if confirmation is not None:
                return confirmation

        # ── Local deterministic fast path (BEFORE any provider round) ──
        # High-confidence command and scheduling intents (including durable
        # Taskloom creation) resolve WITHOUT a provider round. This keeps
        # recurring requests independent of provider tool selection.
        # Other command intents (status queries, last-N delete / review,
        # save/delete by reply, save-by-link) resolve WITHOUT a
        # provider round. This keeps deterministic operations working even
        # when every AI provider is down, rate-limited, or misconfigured —
        # the reason "وضعیت یوزرنیمم رو بگو" must not depend on Groq, and
        # "هر 1 دقیقه ..." must not depend on provider tool selection.
        # It is NOT a keyword command parser replacing the AI: only the
        # narrow, high-confidence command vocabulary resolves here; every
        # conversational and semantic request continues to the provider.
        if tools_allowed and self._tool_executor is not None:
            fast = await self._try_local_fast_path(
                request, rid, status_callback, start, metadata,
            )
            if fast is not None:
                return fast

        # ── Stage 2: Prompt Builder ──
        try:
            _stage("PROMPT_BUILD")
            logger.info("AI_PROMPT_BUILD id=%s", rid or "-")
            prompt_package = self._prompt_builder.build(await self._build_context(request, session))
            # Inject tool schemas into the prompt if a registry is available
            if tools_allowed and self._tool_registry and not self._tool_registry.is_empty():
                tool_schemas = self._tool_registry.list_schemas()
                tool_block = self._render_tool_schemas(tool_schemas)
                if tool_block:
                    prompt_package = self._inject_tool_schemas(prompt_package, tool_block)
            safe_call(self._hooks, "after_prompt", prompt_package)
            metadata["stages"].append("prompt_builder")
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "prompt_builder", start, errors, metadata)

        # ── Stage 3: Provider Manager ──
        try:
            provider_name = self._provider_manager.get_active_name()
            metadata["stages"].append("provider_manager")
            logger.info(
                "AI_EXEC_TRACE request_id=%s stage=provider_selected provider=%s",
                rid or "-", provider_name,
            )
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "provider_manager", start, errors, metadata)

        # Provider attempts superseded by a retry/recovery must still
        # contribute their reported usage. Accumulated here and merged into
        # the final usage dict so no provider response is ever lost or
        # double-counted.
        discarded_usage: dict[str, int] = {
            "prompt_tokens": 0, "completion_tokens": 0, "total_tokens": 0,
        }

        # ── Stage 4: Provider + Tool Loop ──
        try:
            messages = self._build_messages(prompt_package)
            _stage("PROVIDER_REQUEST")
            logger.info("AI_PROVIDER_REQUEST_START id=%s provider=%s", rid or "-", provider_name)
            logger.info(
                "AI_EXEC_TRACE request_id=%s stage=provider_request provider=%s",
                rid or "-", provider_name,
            )
            response: ProviderResponse = await _provider_chat(
                messages, call_label="initial", **chat_kwargs
            )
            # Execution-telemetry facts travel with the FINAL response object;
            # every later round is max-merged so a retry anywhere in the chain
            # is never lost.
            provider_usage_reported = any(
                int(response.usage.get(k, 0) or 0) > 0
                for k in ("prompt_tokens", "completion_tokens", "total_tokens")
            )
            retry_count = int(response.metadata.get("ai_retry_count", 0) or 0)
            fallback_used = bool(response.metadata.get("fallback"))
            failure_type = _provider_failure_type(response)

            def _reject_disabled_tool_calls(candidate: ProviderResponse) -> ProviderResponse:
                nonlocal failure_type
                if tools_allowed or not candidate.tool_calls:
                    return candidate
                errors.append("AI tools are disabled for this request")
                failure_type = "tools_disabled"
                blocked_metadata = dict(candidate.metadata or {})
                blocked_metadata["failure_type"] = "tools_disabled"
                blocked_metadata["tool_calls_blocked"] = len(candidate.tool_calls)
                metadata["tool_calls_blocked"] = blocked_metadata["tool_calls_blocked"]
                logger.warning(
                    "AI_TOOL_CALL_BLOCKED id=%s provider=%s count=%d",
                    rid or "-", candidate.provider_name or provider_name,
                    blocked_metadata["tool_calls_blocked"],
                )
                return replace(
                    candidate,
                    text="",
                    success=False,
                    tool_calls=[],
                    metadata=blocked_metadata,
                )

            response = _reject_disabled_tool_calls(response)
            _mark_success("PROVIDER_REQUEST")
            logger.info(
                "AI_PROVIDER_RESPONSE id=%s provider=%s success=%s",
                rid or "-", response.provider_name or provider_name, response.success,
            )
            logger.info(
                "AI_EXEC_TRACE request_id=%s stage=provider_response success=%s "
                "structured=%s tool_calls=%d",
                rid or "-", response.success, bool(response.tool_calls),
                len(response.tool_calls or []),
            )
            if not response.success:
                ftype = (response.metadata or {}).get("failure_type", "unknown")
                logger.warning(
                    "AI_PROVIDER_FAILURE_CATEGORY id=%s provider=%s category=%s",
                    rid or "-", response.provider_name or provider_name, ftype,
                )

            # Bounded retry for a transient EMPTY provider response (a
            # "thinking stall" where the model returns finish_reason=stop with
            # no text and no tool call). This is NOT a permanent-config retry
            # and never re-runs tools — no tool has executed at this point, so
            # a destructive save/delete cannot double-run. The retry appends
            # a format-enforcement nudge so a stall does not just repeat itself.
            finish_reason = (response.metadata or {}).get("finish_reason", "")
            if (
                response.success
                and not response.text
                and not response.tool_calls
                and not _is_blocked_finish(finish_reason)
                and not _is_truncated_finish(finish_reason)
            ):
                logger.info(
                    "AI_PROVIDER_EMPTY_RESPONSE_RETRY id=%s provider=%s",
                    rid or "-", response.provider_name or provider_name,
                )
                retry_messages = self._append_action_nudge(messages)
                retry_response: ProviderResponse = await _provider_chat(
                    retry_messages, call_label="empty_retry", **chat_kwargs
                )
                provider_usage_reported = provider_usage_reported or any(
                    int(retry_response.usage.get(k, 0) or 0) > 0
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                )
                retry_count = max(
                    retry_count, int(retry_response.metadata.get("ai_retry_count", 0) or 0)
                )
                fallback_used = fallback_used or bool(retry_response.metadata.get("fallback"))
                if not retry_response.success:
                    failure_type = _provider_failure_type(retry_response)
                if retry_response.success and (retry_response.text or retry_response.tool_calls):
                    # The superseded empty attempt still consumed provider
                    # usage — retain it before the response is replaced.
                    _accumulate_usage(discarded_usage, response.usage)
                    retry_meta = dict(retry_response.metadata or {})
                    retry_meta["ai_retry_count"] = int(retry_meta.get("ai_retry_count", 0)) + 1
                    retry_response = replace(retry_response, metadata=retry_meta)
                    # The nudge-retry above is a real retry for telemetry.
                    retry_count = max(
                        retry_count, int(retry_meta["ai_retry_count"])
                    )
                    response = _reject_disabled_tool_calls(retry_response)
                    logger.info("AI_PROVIDER_EMPTY_RESPONSE_RETRY_RECOVERED id=%s", rid or "-")

            safe_call(self._hooks, "after_provider", response)
            metadata["stages"].append("provider")
        except Exception as exc:  # noqa: BLE001
            return self._fail(exc, "provider", start, errors, metadata)

        # A provider retry must obey the same request capability boundary as
        # the first response. This final guard also covers custom providers
        # that attach tool calls outside the normal initial response path.
        if not tools_allowed and response.tool_calls:
            blocked_count = len(response.tool_calls)
            response = replace(
                response,
                text="",
                success=False,
                tool_calls=[],
                metadata={
                    **(response.metadata or {}),
                    "failure_type": "tools_disabled",
                    "tool_calls_blocked": blocked_count,
                },
            )
            metadata["tool_calls_blocked"] = blocked_count
            failure_type = "tools_disabled"
            errors.append("AI tools are disabled for this request")

        # ── Structured-action fallback: model prose/JSON → tool calls ──
        # When the provider does not emit a native tool call, try the
        # JSON-schema structured-output path. An executable intent becomes
        # concrete tool calls for the SAME ToolExecutor; clarification and
        # rejection outcomes become a deterministic text response.
        structured_action = False
        if tools_allowed and response.success and not response.tool_calls:
            response = self._apply_structured_action(response, request, rid)
            structured_action = bool(response.tool_calls)

            # Recovery: the model returned prose that neither the deterministic
            # command parser nor the JSON action parser resolved into an action.
            # Exactly ONE bounded retry asks the model to emit a structured
            # action. No tool has run yet, so a destructive save/delete can
            # never double-execute. If the retry still yields prose, the
            # original prose is kept as the conversational answer.
            if (
                not structured_action
                and not response.metadata.get("ai_action")
                and response.text
            ):
                logger.info(
                    "AI_ACTION_RECOVERY_RETRY id=%s provider=%s",
                    rid or "-", response.provider_name or provider_name,
                )
                try:
                    recovery_messages = self._append_action_nudge(messages)
                    recovery_response: ProviderResponse = await _provider_chat(
                        recovery_messages, call_label="action_recovery", **chat_kwargs
                    )
                except Exception as exc:  # noqa: BLE001
                    logger.warning(
                        "AI_ACTION_RECOVERY_RETRY_FAILED id=%s error=%s", rid or "-", exc
                    )
                    recovery_response = ProviderResponse(
                        text="", provider_name=provider_name, success=False,
                        metadata={"failure_type": "network"},
                    )

                recovery_usage_reported = any(
                    int(recovery_response.usage.get(k, 0) or 0) > 0
                    for k in ("prompt_tokens", "completion_tokens", "total_tokens")
                )
                if recovery_response.success and recovery_response.tool_calls:
                    # The superseded prose response still consumed provider
                    # usage — retain it before the response is replaced.
                    _accumulate_usage(discarded_usage, response.usage)
                    provider_usage_reported = provider_usage_reported or recovery_usage_reported
                    # Native tool call from the recovery response — use it
                    # directly (the nudge produced the structured output).
                    response = recovery_response
                    structured_action = True
                    logger.info("AI_ACTION_RECOVERY_RETRY_RECOVERED id=%s", rid or "-")
                elif recovery_response.success and recovery_response.text:
                    candidate = self._apply_structured_action(recovery_response, request, rid)
                    if candidate.tool_calls:
                        _accumulate_usage(discarded_usage, response.usage)
                        provider_usage_reported = provider_usage_reported or recovery_usage_reported
                        response = candidate
                        structured_action = True
                        logger.info("AI_ACTION_RECOVERY_RETRY_RECOVERED id=%s", rid or "-")
                    elif candidate.metadata.get("ai_action"):
                        # A clarify/invalid/unsupported action is a real
                        # deterministic outcome — prefer it over raw prose.
                        _accumulate_usage(discarded_usage, response.usage)
                        provider_usage_reported = provider_usage_reported or recovery_usage_reported
                        response = candidate

            if response.metadata.get("ai_action"):
                metadata["ai_action"] = {
                    "action": response.metadata.get("ai_action"),
                    "kind": response.metadata.get("ai_action_kind"),
                    "target": response.metadata.get("ai_action_target"),
                }

        all_tool_results: list[dict[str, Any]] = []
        completed_search_queries: set[str] = set()
        # Usage accumulation: the final response plus every superseded
        # retry/recovery attempt, merged with all continuation rounds. Each
        # provider response is counted exactly once — discarded attempts enter
        # through ``discarded_usage``, the final response through its own usage
        # dict, continuation rounds are added below — and nothing is
        # double-counted.
        usage: dict[str, int] = {
            "prompt_tokens": int(response.usage.get("prompt_tokens", 0)) + discarded_usage["prompt_tokens"],
            "completion_tokens": int(response.usage.get("completion_tokens", 0)) + discarded_usage["completion_tokens"],
            "total_tokens": int(response.usage.get("total_tokens", 0)) + discarded_usage["total_tokens"],
        }
        accumulated_finish_reasons: list[str] = []
        if response.metadata.get("finish_reason"):
            accumulated_finish_reasons.append(response.metadata["finish_reason"])

        tool_rounds_executed = 0
        rounds_exhausted = False
        # Distinguishes "the current round's calls were executed" from "a
        # continuation just produced fresh calls that have not run yet" so
        # the exhaustion handler never re-executes and never discards.
        last_round_executed = False
        round_execution_failed = False

        for round_num in range(MAX_TOOL_ROUNDS):
            if not response.success or not response.tool_calls:
                break
            if not self._tool_executor:
                warnings.append(
                    "tool_execution_unavailable: no ToolExecutor attached — "
                    "pending tool calls were not executed"
                )
                metadata["tool_executor_missing"] = True
                break

            try:
                _stage("TOOL_PARSE")
                logger.info(
                    "AI_TOOL_PARSE id=%s tools=%s",
                    rid or "-", [tc.get("name", "") for tc in response.tool_calls],
                )
                for tc in response.tool_calls:
                    args = tc.get("arguments") or {}
                    keys = sorted(args.keys()) if isinstance(args, dict) else []
                    logger.info(
                        "AI_TOOL_ARGS id=%s tool=%s args=%s",
                        rid or "-", tc.get("name", ""), keys,
                    )
                _stage("TOOL_EXECUTION")
                logger.info("AI_TOOL_EXECUTION_START id=%s", rid or "-")
                logger.info(
                    "AI_EXECUTION_START id=%s tools=%s",
                    rid or "-", [tc.get("name", "") for tc in response.tool_calls],
                )
                per_request_ctx = self._build_tool_context(request)
                exec_results = await self._tool_executor.execute_calls(
                    response.tool_calls,
                    owner_id=request.owner_id,
                    session_id=request.session_id,
                    status_callback=status_callback,
                    context_override=per_request_ctx,
                )
                logger.info("AI_TOOL_EXECUTION_END id=%s results=%d", rid or "-", len(exec_results))
                for er in exec_results:
                    if er.success:
                        logger.info(
                            "AI_EXECUTION_RESULT id=%s action=%s success=True",
                            rid or "-", er.tool_name,
                        )
                    else:
                        logger.info(
                            "AI_EXECUTION_ERROR id=%s action=%s error=%s",
                            rid or "-", er.tool_name, er.error or er.message,
                        )
                    result_dict = er.as_dict()
                    if er.tool_name == "web_search" and isinstance(er.data, dict):
                        result_dict["query"] = er.data.get("query", "")
                    all_tool_results.append(result_dict)
                    if er.needs_confirmation:
                        # Not an execution: the confirmation gate below turns
                        # it into a pending owner approval instead of a
                        # history failure.
                        continue
                    # Record EVERY tool outcome (success or failure) so tool
                    # failures never silently disappear from history.
                    marker = "✅" if er.success else "❌"
                    detail = er.message or er.error or "no message"
                    self._conversation.add_tool_result(
                        owner_id=request.owner_id,
                        tool_name=er.tool_name,
                        result=f"{marker} {detail}",
                    )
                metadata["tool_results"] = all_tool_results
                metadata["tool_rounds"] = round_num + 1
                metadata["stages"].append(f"tool_execution_round_{round_num + 1}")
                tool_rounds_executed = round_num + 1
                last_round_executed = True

                # Permission-gated outcomes: create the bounded pending
                # owner approval and stop the round (no continuation round
                # sees the blocked result and no follow-up can re-request it
                # on its own).
                confirmation_text = self._gate_confirmation_results(
                    request, response.tool_calls, exec_results,
                )
                if confirmation_text is not None:
                    metadata["confirmation_pending"] = True
                    response = replace(response, tool_calls=[], text=confirmation_text)
                    break

                # The structured-action path reports the real tool result
                # directly — it never needs a continuation provider round.
                if structured_action:
                    response = replace(response, tool_calls=[], text="")
                    break

                # Verbatim read-authority: a native tool-call round that
                # executed ONLY deterministic read tools must return the
                # real tool results exactly — a continuation round lets the
                # model paraphrase, stylize, or hallucinate the value
                # (production: a real bio was regenerated as unrelated text).
                # Same contract as the structured-action path above.
                if self._read_results_authoritative(response.tool_calls, exec_results):
                    logger.info(
                        "AI_EXEC_TRACE id=%s stage=verbatim_tool_result tools=%s",
                        rid or "-", [er.tool_name for er in exec_results],
                    )
                    response = replace(
                        response,
                        tool_calls=[],
                        text=self._summarize_tool_results(all_tool_results),
                    )
                    break
            except Exception as exc:  # noqa: BLE001
                round_execution_failed = True
                warnings.append(f"tool_execution_round_{round_num + 1}: {exc}")
                break

            # A successful search is now available to the model through the
            # normal tool-result continuation. Prevent only equivalent repeat
            # searches; the chat provider still gets one synthesis round so it
            # can answer naturally and evaluate freshness/relevance.
            if self._has_redundant_search_call(response.tool_calls, exec_results, completed_search_queries):
                response = replace(response, tool_calls=[], text=self._summarize_tool_results(all_tool_results))
                break
            completed_search_queries.update(
                str((tc.get("arguments") or {}).get("query") or "").strip().casefold()
                for tc in response.tool_calls
                if tc.get("name") == "web_search"
            )

            continuation_messages = self._build_continuation_messages(messages, response, exec_results)
            try:
                _stage("PROVIDER_REQUEST")
                logger.info("AI_PROVIDER_REQUEST_START id=%s round=%d", rid or "-", round_num + 2)
                cont_response: ProviderResponse = await _provider_chat(
                    continuation_messages,
                    call_label=f"continuation_{round_num + 1}",
                    **chat_kwargs,
                )
                _mark_success("PROVIDER_REQUEST")
                logger.info(
                    "AI_PROVIDER_RESPONSE id=%s round=%d success=%s",
                    rid or "-", round_num + 2, cont_response.success,
                )
                safe_call(self._hooks, "after_provider", cont_response)
                metadata["stages"].append(f"provider_continuation_{round_num + 1}")
            except Exception as exc:  # noqa: BLE001
                warnings.append(f"provider_continuation_{round_num + 1}: {exc}")
                break

            for k in ("prompt_tokens", "completion_tokens", "total_tokens"):
                usage[k] += int(cont_response.usage.get(k, 0))
            if cont_response.metadata.get("finish_reason"):
                accumulated_finish_reasons.append(cont_response.metadata["finish_reason"])
            provider_usage_reported = provider_usage_reported or any(
                int(cont_response.usage.get(k, 0) or 0) > 0
                for k in ("prompt_tokens", "completion_tokens", "total_tokens")
            )
            retry_count = max(
                retry_count, int(cont_response.metadata.get("ai_retry_count", 0) or 0)
            )
            fallback_used = fallback_used or bool(cont_response.metadata.get("fallback"))
            if not cont_response.success:
                failure_type = _provider_failure_type(cont_response)

            response = cont_response
            messages = continuation_messages
            last_round_executed = False

        # ── Tool-round exhaustion: pending tool calls are NEVER silently dropped ──
        # The round limit bounds PROVIDER continuation rounds, not the current
        # round's executable calls. When the loop terminates on a fresh
        # continuation response whose tool calls were never dispatched, those
        # calls are executed ONCE here through the same ToolExecutor — no
        # additional provider round is issued, so the loop still cannot grow
        # without bound. Calls that already executed in the final round are
        # never re-executed, and calls are never re-run after a failed round.
        if response.tool_calls:
            pending_names = [tc.get("name", "") for tc in response.tool_calls]
            logger.info(
                "AI_TOOL_ROUND_LIMIT id=%s limit=%d rounds_executed=%d pending=%d "
                "tools=%s last_round_executed=%s",
                rid or "-", MAX_TOOL_ROUNDS, tool_rounds_executed,
                len(response.tool_calls), pending_names, last_round_executed,
            )
            salvage_calls = list(response.tool_calls)
            if last_round_executed:
                # The final round's calls already ran; the loop ended on the
                # continuation outcome. Never re-execute them.
                response = replace(response, tool_calls=[])
                warnings.append(
                    "tool_round_limit_reached: final round tool call(s) already "
                    f"executed; no additional round started (MAX_TOOL_ROUNDS={MAX_TOOL_ROUNDS})"
                )
            elif round_execution_failed or self._tool_executor is None:
                # The calls just failed mid-execution, or no executor exists to
                # run them: either way they must NOT be salvaged (re-running a
                # failed/destructive call is never safe). They stay recorded
                # as pending for diagnostics only.
                rounds_exhausted = True
                metadata["tool_rounds_exhausted"] = True
                metadata["tool_rounds_executed"] = tool_rounds_executed
                metadata["pending_tool_calls"] = [
                    {"name": tc.get("name", ""), "id": tc.get("id", "")}
                    for tc in salvage_calls
                ]
                warnings.append(
                    f"tool_round_limit_reached: {len(salvage_calls)} pending tool call(s) "
                    f"after {tool_rounds_executed} round(s) (MAX_TOOL_ROUNDS={MAX_TOOL_ROUNDS})"
                )
            else:
                try:
                    _stage("TOOL_EXECUTION")
                    logger.info(
                        "AI_TOOL_PENDING_EXEC_START id=%s tools=%s rounds_executed=%d limit=%d",
                        rid or "-", pending_names, tool_rounds_executed, MAX_TOOL_ROUNDS,
                    )
                    per_request_ctx = self._build_tool_context(request)
                    salvage_results = await self._tool_executor.execute_calls(
                        salvage_calls,
                        owner_id=request.owner_id,
                        session_id=request.session_id,
                        status_callback=status_callback,
                        context_override=per_request_ctx,
                    )
                    for er in salvage_results:
                        all_tool_results.append(er.as_dict())
                        if er.needs_confirmation:
                            continue
                        marker = "✅" if er.success else "❌"
                        detail = er.message or er.error or "no message"
                        self._conversation.add_tool_result(
                            owner_id=request.owner_id,
                            tool_name=er.tool_name,
                            result=f"{marker} {detail}",
                        )
                        logger.info(
                            "AI_TOOL_PENDING_RESULT id=%s tool=%s success=%s",
                            rid or "-", er.tool_name, er.success,
                        )
                    metadata["tool_results"] = all_tool_results
                    metadata["stages"].append("pending_tool_execution_at_limit")
                    metadata["pending_calls_executed_at_limit"] = True
                    # Permission-gated salvage: turn the blocked call into a
                    # pending owner approval instead of claiming it ran.
                    salvage_confirmation = self._gate_confirmation_results(
                        request, salvage_calls, salvage_results,
                    )
                    if salvage_confirmation is not None:
                        metadata["confirmation_pending"] = True
                        response = replace(
                            response, tool_calls=[], text=salvage_confirmation,
                        )
                    else:
                        # Keep the continuation's own text if the model
                        # produced one; the real-result fallback below fills
                        # the summary only when the response has no text.
                        response = replace(response, tool_calls=[])
                        warnings.append(
                            f"tool_round_limit_reached: {len(salvage_calls)} pending tool call(s) "
                            f"executed without an additional round (MAX_TOOL_ROUNDS={MAX_TOOL_ROUNDS})"
                        )
                    logger.info(
                        "AI_TOOL_PENDING_EXEC_END id=%s executed=%d rounds_executed=%d limit=%d",
                        rid or "-", len(salvage_results), tool_rounds_executed, MAX_TOOL_ROUNDS,
                    )
                except Exception as exc:  # noqa: BLE001
                    rounds_exhausted = True
                    metadata["tool_rounds_exhausted"] = True
                    metadata["tool_rounds_executed"] = tool_rounds_executed
                    metadata["pending_tool_calls"] = [
                        {"name": tc.get("name", ""), "id": tc.get("id", "")}
                        for tc in salvage_calls
                    ]
                    warnings.append(f"pending_tool_execution_at_limit: {exc}")
                    warnings.append(
                        f"tool_round_limit_reached: {len(salvage_calls)} pending tool call(s) "
                        f"after {tool_rounds_executed} round(s) (MAX_TOOL_ROUNDS={MAX_TOOL_ROUNDS})"
                    )
                    logger.warning(
                        "AI_TOOL_PENDING_EXEC_FAILED id=%s error=%s", rid or "-", exc
                    )

        logger.info(
            "AI_TOOL_LOOP_END id=%s rounds_executed=%d limit=%d disposition=%s",
            rid or "-", tool_rounds_executed, MAX_TOOL_ROUNDS,
            (
                "executed_at_limit"
                if metadata.get("pending_calls_executed_at_limit")
                else ("skipped_round_limit" if rounds_exhausted else "completed")
            ),
        )

        if accumulated_finish_reasons:
            metadata["continuation_finish_reasons"] = accumulated_finish_reasons

        # ── Real-result response: never fabricate success ──
        # If tools executed but no final text was produced (the structured
        # path, or a continuation round that returned empty), build the
        # response from the ACTUAL tool results.
        if all_tool_results and not response.text:
            summary = self._summarize_tool_results(all_tool_results)
            if summary:
                response = replace(response, text=summary)

        # ── Stage 5: Conversation Update ──
        try:
            if response.success and response.text:
                self._conversation.add_assistant_message(
                    owner_id=request.owner_id, content=response.text
                )
            metadata["stages"].append("conversation_update")
        except Exception as exc:  # noqa: BLE001
            warnings.append(f"conversation_update: {exc}")

        # ── Stage 6: Result ──
        latency = time.perf_counter() - start
        prompt_tokens = usage["prompt_tokens"]
        completion_tokens = usage["completion_tokens"]
        total_tokens = usage["total_tokens"]
        if prompt_tokens == 0 and completion_tokens == 0 and total_tokens == 0:
            # Providers that omit usage: fall back to the prompt estimate —
            # but ONLY for a successful request. A failed request consumed
            # nothing, so its token fields stay 0 and telemetry reports
            # "unavailable" instead of dressing an estimate up as usage.
            if response.success:
                prompt_tokens = prompt_package.estimated_tokens.estimated_input_tokens
                total_tokens = prompt_tokens + completion_tokens
        elif total_tokens == 0:
            total_tokens = prompt_tokens + completion_tokens
        elif total_tokens < prompt_tokens + completion_tokens:
            total_tokens = prompt_tokens + completion_tokens
        prompt_chars = prompt_package.estimated_tokens.prompt_size_chars

        if not response.success and response.text:
            errors.append(response.text)

        # ── Empty-response / finish-state classification ──
        # The final result must distinguish every meaningful outcome so the
        # Telegram UI never masks a real condition behind "AI returned no
        # response."
        finish_reason = (
            (response.metadata or {}).get("finish_reason", "")
            or (accumulated_finish_reasons[-1] if accumulated_finish_reasons else "")
        )
        if not response.success:
            finish_state = "provider_failure"
        elif rounds_exhausted:
            finish_state = "tool_rounds_exhausted"
        elif response.text:
            finish_state = "text"
        elif response.tool_calls:
            finish_state = "tool_only"
        elif _is_blocked_finish(finish_reason):
            finish_state = "provider_blocked"
        elif _is_truncated_finish(finish_reason):
            finish_state = "token_truncated"
        else:
            finish_state = "empty"
        metadata["finish_state"] = finish_state
        metadata["finish_reason"] = finish_reason
        # ── Normalized execution-telemetry facts (single source of truth) ──
        # The telemetry store reads ONLY these keys; every user-facing AI
        # surface (Overview/Details/Usage/Health panels, compact chat line)
        # renders from the normalized record, never from provider internals.
        if provider_usage_reported:
            metadata["token_source"] = "actual"
        elif prompt_tokens > 0 or completion_tokens > 0:
            metadata["token_source"] = "estimated"
        else:
            metadata["token_source"] = "unavailable"
        metadata["retry_count"] = retry_count
        metadata["fallback_used"] = fallback_used
        metadata["provider_call_count"] = provider_call_count
        metadata["provider_elapsed_s"] = round(provider_total_elapsed, 3)
        metadata["provider_failure_type"] = failure_type
        response_metadata = response.metadata or {}
        metadata["fallback_exhausted"] = bool(response_metadata.get("fallback_exhausted"))
        if isinstance(response_metadata.get("provider_matrix"), list):
            metadata["provider_matrix_size"] = len(response_metadata["provider_matrix"])
        metadata["tool_call_count"] = len(all_tool_results)
        metadata["context_tokens"] = prompt_tokens
        if failure_type:
            metadata["failure_type"] = failure_type

        if all_tool_results:
            tool_errors = [r for r in all_tool_results if not r.get("success")]
            if tool_errors:
                for te in tool_errors:
                    err_type = te.get("error", "")
                    if err_type and err_type != "max_tools_exceeded":
                        warnings.append(f"tool_{te.get('tool_name', '?')}: {err_type}")

        result = EngineResult(
            success=bool(response.success),
            provider=response.provider_name or provider_name,
            # The model that ACTUALLY served: providers stamp the resolved
            # model into response metadata — authoritative even when a
            # fallback provider answered instead of the active one.
            model=(
                str((response.metadata or {}).get("model") or "")
                or self._provider_manager.get_active().config.model
                or response.provider_name
                or provider_name
            ),
            latency=latency,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            total_tokens=total_tokens,
            response=response.text if response.success else "",
            warnings=warnings,
            errors=errors,
            metadata=metadata,
        )

        safe_call(self._hooks, "after_response", result)

        self._metrics.record(
            success=result.success,
            provider=result.provider,
            owner_id=request.owner_id,
            latency=latency,
            prompt_chars=prompt_chars,
            prompt_tokens=prompt_tokens,
            completion_tokens=completion_tokens,
            error="" if result.success else (errors[-1] if errors else "provider_failed"),
        )

        record = None
        try:
            record = telemetry.record_execution(result, request.owner_id)
        except Exception:  # noqa: BLE001
            logger.debug("AI telemetry record failed", exc_info=True)
        self._persist_usage(record, request.session_id)

        return result

    # ── internal ──

    def _persist_usage(self, record: Any, session_id: str = "") -> None:
        """Persist the normalized execution record exactly once, off the loop.

        Mirrors the message/tool-history persistence convention
        (``guarded_create_task``): the async recorder runs the sync repository
        writes in a worker thread with a bounded timeout, and failures are
        logged — never raised, never affecting the AI response or telemetry.
        """
        if record is None:
            return
        try:
            # Only schedule where an event loop is actually running (the
            # normal dispatch path). Direct sync callers of the internal
            # result builders skip persistence rather than leak a task.
            asyncio.get_running_loop()
        except RuntimeError:
            return
        try:
            from backend.ai.database.usage_recorder import record_usage
            from backend.runtime.task_guard import guarded_create_task
            guarded_create_task(
                record_usage(record, session_id=session_id),
                name="ai:persist-usage",
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("AI usage persistence schedule failed: %r", exc)

    def _build_tool_context(self, request: AIRequest) -> ToolContext:
        """Build a per-request ToolContext from the executor's base context.

        Enriches the base context's ``extra`` dict with ``chat_id`` and
        ``reply_msg`` from the current AIRequest so tools (save, delete,
        etc.) can operate on the correct chat and replied-to message.
        """
        base = self._tool_executor._context
        extra: dict[str, Any] = dict(base.extra) if base.extra else {}
        # Make the registry's tool metadata available even when a provider
        # returns a prose response that is later converted into an action.
        extra["chat_id"] = request.chat_id
        extra["request_message_id"] = request.message_id
        extra["request_id"] = request.request_id
        # Capability tools must use the manager owned by this live Engine.
        # Looking up the process-global engine from inside a tool can route a
        # request through a stale/unconfigured provider mesh.
        extra["provider_manager"] = self._provider_manager
        if request.user_message:
            deterministic = parse_command_intent(
                request.user_message,
                has_reply=bool(request.reply_context and request.reply_context.exists),
            )
            if deterministic.action == "create_task" and deterministic.kind == "executable":
                candidate = self._build_deterministic_task_candidate(
                    request, deterministic.schedule_text,
                )
                if candidate is not None:
                    extra["deterministic_task_candidate"] = candidate
        if request.reply_context and request.reply_context.exists:
            extra["reply_msg"] = {
                "message_id": request.reply_context.message_id,
                "sender_id": request.reply_context.sender_id,
                "sender_name": request.reply_context.sender_name,
                "chat_id": request.reply_context.chat_id,
                "chat_title": request.reply_context.chat_title,
                "media_type": request.reply_context.media_type,
                "text_preview": request.reply_context.text_preview,
                "timestamp": request.reply_context.timestamp,
            }
        return ToolContext(
            telegram=base.telegram,
            owner_id=request.owner_id,
            tz_str=request.timezone or base.tz_str,
            client=base.client,
            extra=extra,
        )

    @staticmethod
    def _build_deterministic_task_candidate(request: AIRequest, schedule_text: str) -> dict[str, Any] | None:
        """Build a narrow interval/write candidate from a high-confidence request."""
        from backend.ai.actions import _parse_number, _tokenize, _TIME_UNITS

        words = _tokenize(schedule_text)
        interval_index = next(
            (i for i, word in enumerate(words) if word in {"هر", "every", "each"}),
            None,
        )
        if interval_index is None:
            return None
        unit_index = next(
            (
                i for i in range(interval_index + 1, min(len(words), interval_index + 6))
                if words[i] in _TIME_UNITS
            ),
            None,
        )
        if unit_index is None:
            return None
        number = _parse_number(words, unit_index - 1) or 1
        unit_seconds = {
            "ثانیه": 1, "second": 1, "seconds": 1, "sec": 1, "secs": 1,
            "دقیقه": 60, "دقیقه‌ای": 60, "دقیق": 60,
            "minute": 60, "minutes": 60, "min": 60, "mins": 60,
            "ساعت": 3600, "hour": 3600, "hours": 3600, "hr": 3600, "hrs": 3600,
        }.get(words[unit_index], 0)
        if unit_seconds <= 0:
            return None
        marker = next(
            (i for i, word in enumerate(words) if word in {"بنویس", "نویس", "write", "writing"}),
            None,
        )
        if marker is None or marker + 1 >= len(words):
            return None
        text = " ".join(words[marker + 1:]).strip()
        if not text:
            return None
        return {
            "label": text[:256],
            "schedule_type": "interval",
            "schedule": {"seconds": number * unit_seconds},
            "timezone": request.timezone or "UTC",
            "actions": [{"name": "send_message", "arguments": {"text": text}}],
            "notification_destination": {},
        }

    def _gate_confirmation_results(
        self,
        request: AIRequest,
        tool_calls: list[dict[str, Any]],
        exec_results: list["ToolExecutionResult"],
    ) -> str | None:
        """Turn needs_confirmation results into a bounded pending approval.

        Called after every tool-execution batch. Returns the deterministic
        confirmation-request text when at least one result was blocked by
        the permission gate, else None. Only the FIRST blocked call is
        stored — the store holds exactly one bounded pending confirmation
        per (owner, chat), and an existing pending action is never silently
        overwritten; additional blocked calls must be re-requested after
        the first is approved or expires.
        """
        blocked = [
            (call, er)
            for call, er in zip(tool_calls, exec_results, strict=False)
            if er.needs_confirmation
        ]
        if not blocked:
            return None
        call, first = blocked[0]
        tool_name = str(call.get("name") or first.tool_name or "")
        raw_arguments = call.get("arguments")
        arguments = raw_arguments if isinstance(raw_arguments, dict) else {}
        pending = self._confirmation_store.create(
            request.owner_id,
            request.chat_id,
            request.session_id,
            tool_name,
            dict(arguments),
        )
        extra_blocked = len(blocked) - 1
        if pending is None:
            text = CONFIRMATION_ALREADY_PENDING_TEXT
        else:
            text = confirmation_request_text(tool_name, arguments)
            if extra_blocked:
                text = (
                    f"{text}\n\n⚠️ {extra_blocked} additional owner-only "
                    "action(s) were requested at the same time and were NOT "
                    "scheduled — approve the action above first, then ask again."
                )
        logger.info(
            "AI_CONFIRMATION_PENDING id=%s owner=%d chat=%s tool=%s stored=%s extra=%d",
            getattr(request, "request_id", "") or "-",
            request.owner_id, request.chat_id, tool_name,
            pending is not None, extra_blocked,
        )
        return text

    async def _try_consume_confirmation(
        self,
        request: AIRequest,
        rid: str,
        status_callback: Callable[[str], Awaitable[None]] | None,
        start: float,
        metadata: dict[str, Any],
    ) -> EngineResult | None:
        """Consume an explicit owner confirmation and re-issue the stored call.

        Runs BEFORE any provider round and ONLY for an exact explicit
        confirmation reply. The reply never re-interprets anything: it may
        only consume a server-created pending action whose tool name and
        arguments are frozen at creation time. Returns a deterministic
        EngineResult when the message consumed (or found expired) a pending
        confirmation; returns None otherwise so normal flow continues — a
        conversational "yes" with nothing pending stays conversational.
        """
        message = (request.user_message or "").strip()
        if not is_explicit_confirmation(message):
            return None

        entry, expired = self._confirmation_store.take(request.owner_id, request.chat_id)
        if entry is None:
            if expired:
                logger.info(
                    "AI_CONFIRMATION_EXPIRED id=%s owner=%d chat=%s",
                    rid or "-", request.owner_id, request.chat_id,
                )
                text = _expired_text()
                return self._build_fast_path_result(
                    request, rid, start, metadata, success=True, text=text,
                    action="confirmation", kind="expired", target="",
                )
            return None

        logger.info(
            "AI_CONFIRMATION_CONSUMED id=%s owner=%d chat=%s tool=%s confirm=%s",
            rid or "-", request.owner_id, request.chat_id,
            entry.tool_name, entry.confirmation_id,
        )
        # Rebuild the call EXCLUSIVELY from the frozen pending record. The
        # confirmation message contributed nothing but intent.
        call = {"name": entry.tool_name, "arguments": dict(entry.arguments)}
        per_request_ctx = self._build_tool_context(request)
        try:
            er = await self._tool_executor.execute_confirmed(
                call,
                owner_id=request.owner_id,
                session_id=request.session_id,
                status_callback=status_callback,
                context_override=per_request_ctx,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "AI_CONFIRMATION_EXEC_FAILED id=%s tool=%s error=%s",
                rid or "-", entry.tool_name, exc,
            )
            text = f"❌ The confirmed action could not be executed: {exc}"
            return self._build_fast_path_result(
                request, rid, start, metadata, success=True, text=text,
                action=entry.tool_name, kind="confirmed_error", target="",
            )

        result_dict = er.as_dict()
        metadata["tool_results"] = [result_dict]
        metadata["confirmation_consumed"] = True
        marker = "✅" if er.success else "❌"
        detail = er.message or er.error or "no message"
        self._conversation.add_tool_result(
            owner_id=request.owner_id,
            tool_name=er.tool_name,
            result=f"{marker} {detail}",
        )
        self._conversation.add_assistant_message(
            owner_id=request.owner_id, content=detail,
        )
        logger.info(
            "AI_CONFIRMATION_RESULT id=%s tool=%s success=%s",
            rid or "-", er.tool_name, er.success,
        )
        return self._build_fast_path_result(
            request, rid, start, metadata, success=True, text=detail,
            action=er.tool_name, kind="confirmed", target="",
        )

    async def _try_local_fast_path(
        self,
        request: AIRequest,
        rid: str,
        status_callback: Callable[[str], Awaitable[None]] | None,
        start: float,
        metadata: dict[str, Any],
    ) -> EngineResult | None:
        """Deterministic fast path for high-confidence command intents.

        Runs BEFORE any provider round. Only the narrow, high-confidence
        command vocabulary resolves here (status queries, last-N delete /
        review, save/delete by reply, save-by-link); the AI still handles
        every conversational and semantic request. Returns None when the
        intent is conversational so the caller continues to the provider.

        This is the reliability guarantee that deterministic operations
        work even when every provider is rate-limited, misconfigured, or
        down — and it proves (via AI_EXEC_TRACE) that the live Telegram
        request reaches the real tool executor, not just a unit test.
        """
        if not getattr(request, "allow_tools", True):
            return None

        has_reply = bool(request.reply_context and request.reply_context.exists)
        result = parse_command_intent(request.user_message, has_reply=has_reply)

        if result.kind == "conversational":
            return None

        logger.info(
            "AI_EXEC_TRACE request_id=%s stage=intent_resolved intent=%s kind=%s",
            rid or "-", result.action or "none", result.kind,
        )

        # Unsupported is a deterministic safety outcome — return it directly
        # without spending a provider round.
        if result.kind == "unsupported":
            message = f"❌ Unsupported action: {result.action}"
            return self._build_fast_path_result(
                request, rid, start, metadata, success=True, text=message,
                action=result.action, kind=result.kind, target=result.target,
            )

        # Clarify is fast-pathed ONLY for save (a deterministic "reply to the
        # message" prompt). Delete clarification is left to the AI because the
        # deterministic parser cannot distinguish "delete the last message"
        # from a semantic request like "پیام‌های مربوط به X رو پاک کن".
        if result.kind == "clarify":
            if result.action in ("save", "deep_save", "save_link"):
                message = result.reason or "Could you clarify what you'd like me to do?"
                return self._build_fast_path_result(
                    request, rid, start, metadata, success=True, text=message,
                    action=result.action, kind=result.kind, target=result.target,
                )
            return None

        if result.kind != "executable" or not result.tool_calls:
            return None

        # Destructive delete is fast-pathed ONLY when the target is
        # unambiguous and deterministic: an explicit message ID, an explicit
        # multi-message count, the replied-to message, or the explicit last
        # message ("آخرین پیامم رو پاک کن"). Semantic deletes ("مربوط به X",
        # "دعوای اخیر") never resolve to last_message here — the deterministic
        # parser yields them to the AI (see _is_semantic_delete in actions.py).
        if result.action == "delete_messages":
            logger.info(
                "DELETE_INTENT request_id=%s target=%s count=%s",
                rid or "-", result.target or "none",
                result.count if result.count is not None else "-",
            )
            logger.info(
                "DELETE_ACTION_RESOLVED request_id=%s action=delete_messages tools=%s",
                rid or "-", [tc.get("name") for tc in result.tool_calls],
            )
            logger.info(
                "DELETE_TARGET_RESOLVED request_id=%s target=%s count=%s",
                rid or "-", result.target or "none",
                result.count if result.count is not None else "-",
            )
            safe_delete = (
                result.target in ("message_id", "replied_message", "current_message")
                or (
                    result.target == "recent_messages"
                    and result.count is not None
                    and result.count >= 2
                )
                or (result.target == "last_message" and result.count == 1)
                or result.mode in {"all", "until_time", "until_message", "filtered"}
            )
            if not safe_delete:
                logger.info(
                    "AI_EXEC_TRACE request_id=%s stage=fast_path_skipped reason=ambiguous_delete",
                    rid or "-",
                )
                return None

        # Execute the resolved tools through the SAME ToolExecutor used by
        # the provider tool loop — one canonical execution path.
        per_request_ctx = self._build_tool_context(request)
        tool_names = [tc.get("name", "") for tc in result.tool_calls]
        logger.info(
            "AI_EXEC_TRACE request_id=%s stage=tool_selected tools=%s",
            rid or "-", tool_names,
        )
        logger.info(
            "AI_EXEC_TRACE request_id=%s stage=tool_execute tools=%s",
            rid or "-", tool_names,
        )
        try:
            exec_results = await self._tool_executor.execute_calls(
                result.tool_calls,
                owner_id=request.owner_id,
                session_id=request.session_id,
                status_callback=status_callback,
                context_override=per_request_ctx,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "AI_EXEC_TRACE request_id=%s stage=tool_execute error=%s", rid or "-", exc
            )
            exec_results = []

        all_tool_results = [er.as_dict() for er in exec_results]
        for er in exec_results:
            logger.info(
                "AI_EXEC_TRACE request_id=%s stage=tool_result tool=%s success=%s message=%s",
                rid or "-", er.tool_name, er.success,
                (er.message or er.error or "no message").replace("\n", " ")[:160],
            )
            if er.needs_confirmation:
                continue
            self._conversation.add_tool_result(
                owner_id=request.owner_id,
                tool_name=er.tool_name,
                result=f"{'✅' if er.success else '❌'} {er.message or er.error or 'no message'}",
            )
        metadata["tool_results"] = all_tool_results
        metadata["stages"].append("local_fast_path_tool_execution")

        # A deterministic command resolving to a permission-gated tool (no
        # registered command does today) would surface the same pending
        # owner approval instead of pretending the action ran.
        confirmation_text = self._gate_confirmation_results(
            request, result.tool_calls, exec_results,
        )
        if confirmation_text is not None:
            metadata["confirmation_pending"] = True
            summary = confirmation_text
        else:
            summary = self._summarize_tool_results(all_tool_results) or "Action completed."
        self._conversation.add_assistant_message(owner_id=request.owner_id, content=summary)

        return self._build_fast_path_result(
            request, rid, start, metadata, success=True, text=summary,
            action=result.action, kind=result.kind, target=result.target,
        )

    def _build_fast_path_result(
        self,
        request: AIRequest,
        rid: str,
        start: float,
        metadata: dict[str, Any],
        *,
        success: bool,
        text: str,
        action: str,
        kind: str,
        target: str,
    ) -> EngineResult:
        """Build the EngineResult for a locally-resolved fast-path intent."""
        latency = time.perf_counter() - start
        meta = dict(metadata)
        meta["finish_state"] = "local_fast_path"
        meta["token_source"] = "unavailable"
        meta["retry_count"] = 0
        meta["fallback_used"] = False
        meta["tool_call_count"] = len(meta.get("tool_results") or [])
        meta["context_tokens"] = 0
        # Same shape as the provider structured-action path so diagnostics
        # and tests read a consistent ai_action object.
        meta["ai_action"] = {"action": action, "kind": kind, "target": target}
        logger.info(
            "AI_EXEC_TRACE request_id=%s stage=telegram_response success=%s",
            rid or "-", success,
        )
        result = EngineResult(
            success=success,
            provider="local",
            model="deterministic",
            latency=latency,
            prompt_tokens=0,
            completion_tokens=0,
            total_tokens=0,
            response=text if success else "",
            warnings=[],
            errors=[] if success else [text],
            metadata=meta,
        )
        safe_call(self._hooks, "after_response", result)
        self._metrics.record(
            success=success,
            provider="local",
            owner_id=request.owner_id,
            latency=latency,
            prompt_chars=len(request.user_message or ""),
            prompt_tokens=0,
            completion_tokens=0,
            error="" if success else text,
        )
        record = None
        try:
            record = telemetry.record_execution(result, request.owner_id)
        except Exception:  # noqa: BLE001
            logger.debug("AI telemetry record failed", exc_info=True)
        self._persist_usage(record, request.session_id)
        return result

    def _apply_structured_action(
        self,
        response: ProviderResponse,
        request: AIRequest,
        rid: str = "",
    ) -> ProviderResponse:
        """Bridge prose/JSON model output into the existing tool executor.

        When the provider did not emit a native tool call, the deterministic
        command parser runs FIRST over the original user message — the model's
        prose is never trusted to decide execution or permissions. Only when
        the deterministic parser finds nothing is the model's own JSON output
        parsed. Either way the result is validated locally, resolved into
        concrete tool calls for the SAME ToolExecutor, and clarification /
        rejection outcomes become a deterministic text response.
        """
        text = (response.text or "").strip()
        if not text:
            return response

        logger.info("AI_ACTION_PARSE_START id=%s", rid or "-")

        # Deterministic command intent is authoritative for the command
        # vocabulary. It resolves targets from the reply context — it never
        # depends on the model emitting JSON or a native tool call.
        has_reply = bool(request.reply_context and request.reply_context.exists)
        result = parse_command_intent(request.user_message, has_reply=has_reply)

        if result.kind == "conversational":
            # No deterministic command match — try the model's own JSON output.
            result = parse_action_text(text)

        if result.kind == "conversational":
            logger.info(
                "AI_ACTION_PARSE_RESULT id=%s action=none reason=no_action "
                "text_len=%d has_json=%s",
                rid or "-", len(text), "{" in text,
            )
            return response
        logger.info(
            "AI_ACTION_PARSE_RESULT id=%s action=%s count=%s",
            rid or "-",
            result.action or result.kind,
            result.count if result.count is not None else "",
        )

        meta = dict(response.metadata or {})
        meta["ai_action"] = result.action
        meta["ai_action_kind"] = result.kind
        meta["ai_action_target"] = result.target

        if result.kind == "clarify":
            logger.info("AI_ACTION_VALIDATION id=%s result=clarify", rid or "-")
            message = result.reason or "Could you clarify what you'd like me to do?"
            return replace(response, text=message, metadata=meta)

        if result.kind == "invalid":
            logger.info(
                "AI_ACTION_VALIDATION id=%s result=rejected error=%s",
                rid or "-", result.error,
            )
            return replace(response, text=f"❌ {result.error}", metadata=meta)

        if result.kind == "unsupported":
            logger.info(
                "AI_ACTION_VALIDATION id=%s result=unsupported action=%s",
                rid or "-", result.action,
            )
            return replace(response, text=f"❌ Unsupported action: {result.action}", metadata=meta)

        logger.info("AI_ACTION_VALIDATION id=%s result=ok action=%s", rid or "-", result.action)
        logger.info(
            "AI_TARGET_RESOLUTION id=%s action=%s target=%s tools=%s",
            rid or "-", result.action, result.target,
            [tc.get("name") for tc in result.tool_calls],
        )
        return replace(response, tool_calls=list(result.tool_calls), text="", metadata=meta)

    @staticmethod
    def _has_redundant_search_call(
        tool_calls: list[dict[str, Any]],
        exec_results: list[Any],
        prior_results: set[str],
    ) -> bool:
        """Stop only an equivalent successful search, not normal synthesis."""
        if not exec_results or not all(
            er.success and er.tool_name == "web_search" for er in exec_results
        ):
            return False
        queries = [
            str((tc.get("arguments") or {}).get("query") or "").strip().casefold()
            for tc in tool_calls
        ]
        if not queries or any(not query for query in queries):
            return False
        return bool(prior_results.intersection(queries))

    @staticmethod
    def _read_results_authoritative(
        tool_calls: list[dict[str, Any]],
        exec_results: list["ToolExecutionResult"],
    ) -> bool:
        """True when the round executed only successful deterministic READ
        tools whose results must reach the user verbatim.

        The tool result is authoritative data, not a suggestion: for these
        tools the dispatcher skips the continuation provider round entirely
        so the model can never paraphrase, stylize, or replace the value.
        Any failure, any additional tool in the round, or any unknown tool
        keeps the normal continuation behavior.
        """
        if not tool_calls or not exec_results:
            return False
        if len(tool_calls) != len(exec_results):
            return False
        for call, er in zip(tool_calls, exec_results):
            if call.get("name") != er.tool_name:
                return False
            if er.tool_name not in _VERBATIM_READ_TOOLS:
                return False
            if not er.success:
                return False
        return True

    @staticmethod
    def _summarize_tool_results(results: list[dict[str, Any]]) -> str:
        """Build a concise response from the REAL tool results.

        A failure is reported verbatim; multiple successes are joined. READ
        tools that carry structured message data (``list_recent_messages``)
        render the actual messages so "review the last N messages" shows the
        real Telegram conversation rather than a bare count. This is the
        deterministic final answer for the structured-action path and the
        fallback when a continuation provider round returns no text.
        """
        if not results:
            return ""
        failures = [r for r in results if not r.get("success")]
        if failures:
            return failures[0].get("message") or "Action failed."

        rendered: list[str] = []
        for r in results:
            data = r.get("data") or {}
            msgs = data.get("messages")
            if isinstance(msgs, list) and msgs:
                rendered.append(Dispatcher._render_message_list(msgs))
            elif r.get("message"):
                rendered.append(r.get("message"))
        return "\n".join(rendered) if rendered else "Action completed."

    @staticmethod
    def _render_message_list(messages: list[dict[str, Any]]) -> str:
        """Render real Telegram messages as a compact, chronological list."""
        lines: list[str] = []
        for m in messages:
            sender = m.get("sender_name") or m.get("sender_username") or ""
            if not sender:
                sender = f"id{m.get('sender_id') or '?'}"
            text = (m.get("text") or "").strip().replace("\n", " ")
            if text:
                lines.append(f"[{m.get('id')}] {sender}: {text}")
            else:
                media = " 📎" if m.get("has_media") else ""
                lines.append(f"[{m.get('id')}] {sender}: (no text){media}")
        return "\n".join(lines)

    @staticmethod
    def _append_action_nudge(messages: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return ``messages`` with a format-enforcement user turn appended.

        Used by the bounded recovery retries (empty response and prose-with-no-
        action). The nudge asks for a structured tool call / JSON action but
        never bypasses validation — whatever the model returns is still parsed
        and validated locally before any tool executes.
        """
        return list(messages) + [{"role": "user", "content": _ENFORCE_ACTION_NUDGE}]

    def _build_tool_definitions(self) -> list[dict[str, Any]]:
        """Build native OpenAI-format tool definitions from the registry.

        The registry's ``list_schemas()`` returns a flat ``parameters``
        dict (param name → JSON-schema-ish descriptor). Providers need a
        full function-calling schema, so we wrap it as
        ``{"type": "object", "properties": {...}}``. Params without a
        ``default`` are treated as required so providers surface them to
        the model.
        """
        if not self._tool_registry or self._tool_registry.is_empty():
            return []

        definitions: list[dict[str, Any]] = []
        for schema in self._tool_registry.list_schemas():
            raw_params = schema.get("parameters") or {}
            if isinstance(raw_params, dict):
                if "properties" in raw_params:
                    properties = raw_params.get("properties") or {}
                    required = raw_params.get("required") or []
                else:
                    properties = raw_params
                    required = [
                        name for name, info in raw_params.items()
                        if isinstance(info, dict) and "default" not in info
                    ]
            else:
                properties = {}
                required = []

            function_def: dict[str, Any] = {
                "name": schema["name"],
                "description": schema.get("description", ""),
                "parameters": {
                    "type": "object",
                    "properties": properties,
                },
            }
            if required:
                function_def["parameters"]["required"] = required
            definitions.append({"type": "function", "function": function_def})
        return definitions

    def _render_tool_schemas(self, schemas: list[dict[str, Any]]) -> str:
        """Render tool schemas into a compact text block for the prompt."""
        if not schemas:
            return ""
        lines = ["[Available Tools]"]
        for s in schemas:
            params = s.get("parameters", {})
            param_str = ""
            if isinstance(params, dict):
                props = params.get("properties", {})
                if props:
                    parts = []
                    for pname, pinfo in props.items():
                        ptype = pinfo.get("type", "any") if isinstance(pinfo, dict) else "any"
                        parts.append(f"{pname}({ptype})")
                    param_str = ", ".join(parts)
            level = s.get("permission_level", "")
            if level in ("admin_only", "confirmation_required"):
                safe_badge = "needs-confirm"
            elif level == "dangerous":
                safe_badge = "destructive-on-explicit-request"
            else:
                safe_badge = "safe"
            lines.append(
                f"  - {s['name']}({param_str}) — {s['description']} [{safe_badge}]"
            )
        return "\n".join(lines)

    def _inject_tool_schemas(self, package: Any, tool_block: str) -> Any:
        """Return a new PromptPackage with the tool context enriched."""
        from dataclasses import replace
        existing = package.tool_context or ""
        merged = f"{existing}\n\n{tool_block}" if existing else tool_block
        return replace(package, tool_context=merged)

    def _build_messages(self, prompt_package: Any) -> list[dict[str, Any]]:
        """Convert a PromptPackage into a messages list for ProviderManager.chat()."""
        messages: list[dict[str, Any]] = []
        if prompt_package.system_prompt:
            messages.append({"role": "system", "content": prompt_package.system_prompt})
        if prompt_package.runtime_context:
            messages.append({"role": "system", "content": prompt_package.runtime_context})
        if prompt_package.conversation_context:
            messages.append({"role": "system", "content": prompt_package.conversation_context})
        if prompt_package.tool_context:
            messages.append({"role": "system", "content": prompt_package.tool_context})
        if prompt_package.user_input:
            messages.append({"role": "user", "content": prompt_package.user_input})
        return messages

    def _build_continuation_messages(
        self,
        original_messages: list[dict[str, Any]],
        response: ProviderResponse,
        exec_results: list[Any],
    ) -> list[dict[str, Any]]:
        messages = list(original_messages)

        assistant_content = response.text or ""
        assistant_msg: dict[str, Any] = {"role": "assistant", "content": assistant_content}
        if response.tool_calls:
            assistant_msg["tool_calls"] = [
                {
                    "id": tc.get("id", ""),
                    "type": "function",
                    "function": {
                        "name": tc.get("name", ""),
                        "arguments": json.dumps(tc.get("arguments", {})),
                    },
                }
                for tc in response.tool_calls
            ]
        messages.append(assistant_msg)

        for tc, er in zip(response.tool_calls, exec_results, strict=False):
            tool_name = tc.get("name", er.tool_name)
            content = json.dumps({
                "tool": tool_name,
                "success": er.success,
                "message": er.message,
                "data": er.data,
                "error": er.error,
            })
            messages.append({
                "role": "tool",
                "tool_call_id": tc.get("id", ""),
                "name": tool_name,
                "content": content,
            })

        return messages

    async def _build_context(self, request: AIRequest, session: Any) -> Any:
        """Build a ConversationContext from the runtime session + request.

        Uses the Conversation Layer's ContextBuilder so the Prompt
        Builder receives the exact object type it expects.
        """
        from backend.ai.conversation.context_builder import (
            ContextBuilder,
            RuntimeContext,
            ToolContext,
        )

        history_items = self._conversation.get_history(
            owner_id=request.owner_id, n=20
        )
        from backend.ai.conversation.history import HistoryEntry
        history_entries: list[HistoryEntry] = []
        for item in history_items:
            history_entries.append(HistoryEntry(
                role=item.role,
                content=item.content,
                tool_name=item.role if item.role == "tool" else "",
            ))
        memory_data: dict[str, str] = {}
        if self._memory_manager is not None:
            try:
                # Bounded memory retrieval: off the event loop (repo calls are
                # synchronous) and hard-capped by time so a slow or hanging
                # store degrades to empty memory instead of stalling the
                # request. Cancellation always propagates.
                memory_data = await asyncio.wait_for(
                    asyncio.to_thread(
                        self._memory_manager.retrieve_for_prompt, request.owner_id
                    ),
                    timeout=MEMORY_READ_TIMEOUT_S,
                )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                logger.warning(
                    "Memory retrieval failed for owner %s (timed out or errored): %r",
                    request.owner_id, exc,
                )
        return ContextBuilder().build(
            session=self._adapt_session(session, request),
            user_text=request.user_message,
            message_id=request.message_id,
            current_menu="main",
            reply=request.reply_context,
            tool=ToolContext(),
            runtime=RuntimeContext(
                ai_enabled=True,
                active_provider=session.active_provider,
                total_requests=self._metrics.total_executions,
                total_responses=self._metrics.successful_executions,
                turn_count=len(history_items),
            ),
            history=history_entries,
            memory=memory_data,
            preferences=self._load_preferences(request.owner_id),
        )

    def _load_preferences(self, owner_id: int) -> Any:
        """Load the owner's AI preferences from the repository manager.

        Uses ``RepositoryManager.preferences.get_or_create()`` which
        returns an in-memory ``PreferencesRecord`` with defaults when
        the ``ai_preferences`` table does not exist yet.
        """
        from backend.ai.conversation.context_builder import PreferencesContext

        try:
            from backend.ai.database.manager import get_repository_manager

            repo = get_repository_manager().preferences
            rec = repo.get_or_create(owner_id)
            return PreferencesContext(
                language=rec.language,
                personality=rec.personality,
                response_style=rec.response_style,
                custom_instructions=rec.custom_instructions,
                auto_memory=rec.auto_memory,
                auto_tools=rec.auto_tools,
            )
        except Exception as exc:  # noqa: BLE001
            logger.warning("Preferences load failed for owner %s: %r", owner_id, exc)
            return PreferencesContext()

    def _adapt_session(self, session: Any, request: AIRequest | None = None) -> Any:
        """Adapt a RuntimeSession to the ConversationSession shape the
        ContextBuilder expects. We build a lightweight stand-in with
        the attributes ContextBuilder reads."""
        from backend.ai.conversation.state import ConversationState

        class _SessionView:
            __slots__ = (
                "session_id", "owner_id", "chat_id", "state",
                "current_panel", "current_category", "current_flow",
                "pending_action", "language", "timezone",
                "current_tool", "last_tool",
            )

            def __init__(self, s: Any, req: AIRequest | None = None) -> None:
                self.session_id = s.session_id
                self.owner_id = s.owner_id
                self.chat_id = req.chat_id if req else 0
                self.state = ConversationState.IDLE
                self.current_panel = ""
                self.current_category = ""
                self.current_flow = ""
                self.pending_action = ""
                self.language = req.language if req else "English"
                self.timezone = req.timezone if req else "UTC"
                self.current_tool = ""
                self.last_tool = ""

        return _SessionView(session, request)

    def _fail(
        self,
        exc: BaseException,
        stage: str,
        start: float,
        errors: list[str],
        metadata: dict[str, Any],
    ) -> EngineResult:
        """Build a failure EngineResult, record metrics AND telemetry."""
        latency = time.perf_counter() - start
        msg = f"{stage}: {exc}"
        errors.append(msg)
        metadata.setdefault("stages", []).append(stage)
        safe_call(self._hooks, "on_error", msg, stage)
        logger.warning("Engine dispatcher failure at %s: %r", stage, exc)
        # Early-stage failures are real executions too: they must surface in
        # the AI Details/Health surfaces, so they get a normalized record with
        # honest unavailable token facts (never values from another request).
        metadata["token_source"] = "unavailable"
        metadata["failure_type"] = "internal"
        metadata["retry_count"] = 0
        metadata["fallback_used"] = False
        metadata["tool_call_count"] = 0
        metadata["context_tokens"] = 0
        result = EngineResult(
            success=False,
            latency=latency,
            warnings=[],
            errors=errors,
            metadata=metadata,
        )
        self._metrics.record(
            success=False,
            provider="",
            owner_id=0,
            latency=latency,
            prompt_chars=0,
            prompt_tokens=0,
            completion_tokens=0,
            error=msg,
        )
        record = None
        try:
            record = telemetry.record_execution(result, 0)
        except Exception:  # noqa: BLE001
            logger.debug("AI telemetry record failed", exc_info=True)
        self._persist_usage(record)
        return result
