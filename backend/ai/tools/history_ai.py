"""
History AI tools — owner-requested translation and summarization of Telegram
history.

Two separate tools, because the repository registers one tool per capability
(and ``AI_MASTER_DESIGN.md`` §6.9 lists ``translate`` and ``summarize`` as
distinct Read Only tools). Both are thin orchestration shims:

    1. validate their arguments,
    2. take the chat from the request-scoped ``ToolContext``,
    3. call ``backend/services/history_ai_service``,
    4. return a structured ``ToolResult``.

They contain NO Telegram retrieval logic, NO paging, NO provenance filtering and
no provider plumbing — all of that is owned by the history service, the history
AI service and the existing provider architecture.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext
from backend.services import history_ai_service
from backend.services import history_service

logger = logging.getLogger(__name__)

_MAX_COUNT = history_service.MAX_HISTORY_MESSAGES


def _chat_id(context: ToolContext) -> int | None:
    extra = context.extra if context is not None else None
    value = extra.get("chat_id") if extra else None
    try:
        chat_id = int(value)
    except (TypeError, ValueError):
        return None
    return chat_id or None


def _source(context: ToolContext) -> Any:
    """The history service source: the TelegramAPI facade, else the raw client."""
    return getattr(context, "telegram", None) or getattr(context, "client", None)


def _provider_manager(context: ToolContext) -> Any:
    extra = context.extra if context is not None else None
    return extra.get("provider_manager") if extra else None


def _current_message_id(context: ToolContext) -> int | None:
    """The Telegram message that triggered this request, from the request scope.

    Used as the history service's exclusive ``before_id`` so the owner's own
    triggering command can never appear inside the history it just commanded.
    The key is set by ``Dispatcher._build_tool_context``; no text matching is
    involved (an AI-provenance or command-shaped message is still a real
    message — only the request's own id is excluded).
    """
    extra = context.extra if context is not None else None
    if not extra:
        return None
    for key in ("request_message_id", "current_message_id", "message_id"):
        try:
            value = int(extra.get(key))
        except (TypeError, ValueError):
            continue
        if value:
            return value
    return None


def _request_id(context: ToolContext) -> str:
    """The owning request's id, for the shared ``AI_EXEC_TRACE`` stage logs."""
    extra = context.extra if context is not None else None
    value = extra.get("request_id") if extra else None
    return str(value) if value else ""


def _request_timeout(context: ToolContext) -> Any:
    """The caller's wall-clock envelope for this request.

    Set by ``Dispatcher._build_tool_context`` from ``AIRequest.timeout_s``. The
    history AI service derives its pacing budget from it, so the tool layer owns
    no second timeout constant. ``None`` means "use the service default".
    """
    extra = context.extra if context is not None else None
    return extra.get("request_timeout_s") if extra else None


def _count(arguments: dict[str, Any]) -> int:
    """Coerce the requested count; the history bound is applied by the service.

    The upper bound is deliberately NOT applied here: the history service owns
    ``MAX_HISTORY_MESSAGES`` and reports an over-bound request honestly (with the
    note in the result), which clamping in the tool would silently hide.
    """
    try:
        value = int(arguments.get("count") or history_ai_service.DEFAULT_COUNT)
    except (TypeError, ValueError):
        value = history_ai_service.DEFAULT_COUNT
    return max(1, value)


class TranslateHistoryTool(Tool):
    """Translate the most recent Telegram messages into a target language."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "translate_history"

    @property
    def description(self) -> str:
        return (
            "Translate the most recent Telegram messages of this chat (default "
            "100, up to 1000) into a target language. Returns one translated "
            "line per message, in order, keyed by the original message id. Use "
            "this for requests like 'translate the last 500 messages' or "
            "'translate the last 100 messages to English'."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_COUNT,
                "default": history_ai_service.DEFAULT_COUNT,
                "description": "How many of the most recent messages to translate.",
            },
            "language": {
                "type": "string",
                "description": (
                    "Target language, e.g. 'English' or 'Persian'. Pass it when "
                    "the owner named one; otherwise omit it."
                ),
            },
            "instruction": {
                "type": "string",
                "description": (
                    "Optional owner instruction to follow (tone, formality, "
                    "terminology, or which parts to translate)."
                ),
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_ONLY

    @property
    def safe(self) -> bool:
        return True

    @property
    def long_running(self) -> bool:
        """Chunked provider calls legitimately exceed the generic tool timeout."""
        return True

    @property
    def return_type(self) -> str:
        return (
            "ToolResult whose message is the translated history, one line per "
            "message as '[id] translation' in chronological order; data carries "
            "operation/requested/processed/truncated/capped/language."
        )

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        chat_id = _chat_id(context)
        request_id = _request_id(context)
        if not chat_id:
            return ToolResult(success=False, message="No chat context available.")

        try:
            ok, text, data = await history_ai_service.translate_history(
                _source(context),
                chat_id,
                count=_count(arguments),
                language=arguments.get("language"),
                instruction=arguments.get("instruction"),
                provider_manager=_provider_manager(context),
                current_message_id=_current_message_id(context),
                request_id=request_id,
                timeout_s=_request_timeout(context),
            )
        except Exception as exc:  # noqa: BLE001 — boundary: never crash a request
            logger.warning(
                "AI_EXEC_TRACE request_id=%s stage=tool_result tool=%s success=False error=%s",
                request_id or "-", self.name, type(exc).__name__,
            )
            return ToolResult(
                success=False,
                message=f"❌ Translation failed: {type(exc).__name__}: {exc}",
            )
        logger.info(
            "AI_EXEC_TRACE request_id=%s stage=tool_result tool=%s success=%s",
            request_id or "-", self.name, ok,
        )
        return ToolResult(success=ok, message=text, data=data)


class SummarizeHistoryTool(Tool):
    """Summarize the most recent Telegram messages with the configured model."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "summarize_history"

    @property
    def description(self) -> str:
        return (
            "Summarize the most recent Telegram messages of this chat (default "
            "100, up to 1000) with the AI model. Long histories are summarized "
            "in parts and then merged into one coherent summary. Use this for "
            "requests like 'summarize the last 500 messages'."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "count": {
                "type": "integer",
                "minimum": 1,
                "maximum": _MAX_COUNT,
                "default": history_ai_service.DEFAULT_COUNT,
                "description": "How many of the most recent messages to summarize.",
            },
            "instruction": {
                "type": "string",
                "description": (
                    "Optional owner instruction to follow (focus, length, "
                    "language, or which parts matter)."
                ),
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_ONLY

    @property
    def safe(self) -> bool:
        return True

    @property
    def long_running(self) -> bool:
        """Chunked provider calls legitimately exceed the generic tool timeout."""
        return True

    @property
    def return_type(self) -> str:
        return (
            "ToolResult whose message is the generated summary text; data "
            "carries operation/requested/processed/truncated/capped."
        )

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        chat_id = _chat_id(context)
        request_id = _request_id(context)
        if not chat_id:
            return ToolResult(success=False, message="No chat context available.")

        try:
            ok, text, data = await history_ai_service.summarize_history(
                _source(context),
                chat_id,
                count=_count(arguments),
                instruction=arguments.get("instruction"),
                provider_manager=_provider_manager(context),
                current_message_id=_current_message_id(context),
                request_id=request_id,
                timeout_s=_request_timeout(context),
            )
        except Exception as exc:  # noqa: BLE001 — boundary: never crash a request
            logger.warning(
                "AI_EXEC_TRACE request_id=%s stage=tool_result tool=%s success=False error=%s",
                request_id or "-", self.name, type(exc).__name__,
            )
            return ToolResult(
                success=False,
                message=f"❌ Summarization failed: {type(exc).__name__}: {exc}",
            )
        logger.info(
            "AI_EXEC_TRACE request_id=%s stage=tool_result tool=%s success=%s",
            request_id or "-", self.name, ok,
        )
        return ToolResult(success=ok, message=text, data=data)
