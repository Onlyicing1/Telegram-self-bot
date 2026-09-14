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

from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext
from backend.services import history_ai_service
from backend.services import history_service

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
            )
        except Exception as exc:  # noqa: BLE001 — boundary: never crash a request
            return ToolResult(
                success=False,
                message=f"❌ Translation failed: {type(exc).__name__}: {exc}",
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
        if not chat_id:
            return ToolResult(success=False, message="No chat context available.")

        try:
            ok, text, data = await history_ai_service.summarize_history(
                _source(context),
                chat_id,
                count=_count(arguments),
                instruction=arguments.get("instruction"),
                provider_manager=_provider_manager(context),
            )
        except Exception as exc:  # noqa: BLE001 — boundary: never crash a request
            return ToolResult(
                success=False,
                message=f"❌ Summarization failed: {type(exc).__name__}: {exc}",
            )
        return ToolResult(success=ok, message=text, data=data)
