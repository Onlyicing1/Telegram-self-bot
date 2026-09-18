"""
Text-to-speech tool — the deterministic bridge to the TTS service boundary.

The AI may REQUEST speech; it may not perform it. This tool is the entire
request surface:

  - it accepts ONLY a bounded ``text`` argument — never a destination, chat id,
    recipient, voice, model, format, provider or raw audio/RPC instruction;
  - it synthesizes through ``backend/services/tts_service`` (the ONE boundary,
    which owns validation, the input/output bounds, the timeout and the closed
    failure taxonomy) and then delivers ONE voice message through the existing
    ``TelegramAPI`` facade — the same bounded transfer ``download_media``
    already uses in the other direction;
  - the destination is resolved from TRUSTED runtime context (the chat the
    request came from), never from model output, mirroring ``SendMessageTool``
    and ``RetrieveSaveTool``.

Nothing Telegram-related reaches the provider: the service is called with the
text and the request's remaining budget only, and this tool never passes a chat
id, message id, sender, caption, reply text or any other context into it. The
tool result carries the bounded synthesis facts (character count, format, voice)
and deliberately NOT the destination chat, so no Telegram identifier can leak
back into the model's conversation either.

A failure surfaces as a failed ``ToolResult`` carrying the boundary's bounded,
already-sanitized reason — never a raw provider error and never a credential.
"""
from __future__ import annotations

import logging
from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult
from backend.ai.tools.context import ToolContext

logger = logging.getLogger(__name__)


class SpeakTool(Tool):
    """Synthesize bounded text into ONE Telegram voice message."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "text_to_speech"

    @property
    def required_arguments(self) -> tuple[str, ...]:
        return ("text",)

    @property
    def description(self) -> str:
        return (
            "Speak text out loud as a Telegram voice message in the current "
            "chat. Use this when the owner asks for something to be said, read "
            "aloud or turned into audio, and pass exactly the words to speak. "
            "Accepts a single 'text' argument only; the destination voice and "
            "the destination chat are fixed by the runtime, never supplied."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        from backend.services.tts_service import MAX_TTS_INPUT_CHARS

        return {
            "text": {
                "type": "string",
                "minLength": 1,
                "maxLength": MAX_TTS_INPUT_CHARS,
                "description": (
                    "The exact words to speak, in their own language. Pass the "
                    "text verbatim — it is never translated, summarized or "
                    "rewritten."
                ),
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        # Sending a voice note to the owner's own chat is a benign,
        # owner-authorized side effect in this single-owner self-bot.
        return PermissionLevel.READ_WRITE

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with the bounded synthesis facts (characters, format, voice)"

    @property
    def requires_reply_context(self) -> bool:
        """False: synthesis speaks the text it is GIVEN.

        This phase deliberately does not read a replied message aloud — the text
        to speak is the argument, so a scheduled occurrence (which never carries
        reply context) can run the same bounded action.
        """
        return False

    @property
    def long_running(self) -> bool:
        """True: one synthesis plus one upload legitimately exceeds 10 seconds.

        The tool stays bounded — the service owns a finite synthesis timeout and
        the transfer has its own finite ceiling — so this is not an unbounded
        exemption, only an exemption from the generic short tool timeout.
        """
        return True

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import tts_service
        from backend.services.tts_service import TTS_TIMEOUT_S, TtsError

        text = arguments.get("text")
        if not isinstance(text, str) or not text.strip():
            return ToolResult(
                success=False,
                message="There is no text to speak; nothing was sent.",
            )

        # The destination comes from TRUSTED runtime context, never arguments.
        extra = getattr(context, "extra", None) or {}
        chat_id = extra.get("chat_id")
        if not isinstance(chat_id, int) or chat_id == 0:
            chat_id = getattr(context, "owner_id", 0)
        if not isinstance(chat_id, int) or chat_id == 0:
            return ToolResult(
                success=False,
                message="No trusted destination chat is available; nothing was sent.",
            )

        telegram = getattr(context, "telegram", None)
        if telegram is None:
            return ToolResult(
                success=False,
                message="Telegram transport is unavailable; nothing was sent.",
            )

        # The synthesis budget is the request's own wall-clock envelope, capped by
        # the boundary's finite ceiling — so no second, contradictable timeout is
        # invented in the tool layer.
        budget = TTS_TIMEOUT_S
        try:
            envelope = float(extra.get("request_timeout_s") or 0)
        except (TypeError, ValueError):
            envelope = 0.0
        if envelope > 0:
            budget = min(TTS_TIMEOUT_S, envelope)

        request_id = str(extra.get("request_id") or "")
        try:
            clip = await tts_service.synthesize(
                text, request_id=request_id, timeout_s=budget,
            )
        except TtsError as exc:
            return ToolResult(
                success=False,
                message=str(exc),
                data={
                    "failure_class": tts_service.failure_class_of(exc),
                    "stage": str(getattr(exc, "stage", "") or ""),
                },
            )
        except Exception as exc:  # noqa: BLE001 — the transport/service boundary
            logger.warning(
                "TTS_TOOL_FAILED request_id=%s error=%s",
                request_id or "-", type(exc).__name__,
            )
            return ToolResult(
                success=False,
                message="Speech synthesis failed; nothing was sent.",
            )

        try:
            await telegram.send_voice(chat_id, clip.audio, clip.mime_type)
        except Exception as exc:  # noqa: BLE001 — surfaced honestly to the owner
            logger.warning(
                "TTS_DELIVERY_FAILED request_id=%s error=%s",
                request_id or "-", type(exc).__name__,
            )
            return ToolResult(
                success=False,
                message="The voice message could not be sent to Telegram.",
                data={"error_class": type(exc).__name__},
            )

        logger.info(
            "TTS_DELIVERED request_id=%s chars=%d bytes=%d voice=%s model=%s",
            request_id or "-", clip.characters, len(clip.audio),
            clip.voice or "-", clip.model or "-",
        )
        # The result carries bounded synthesis facts and NOT the destination: no
        # chat identifier can travel back into the model's conversation.
        return ToolResult(
            success=True,
            message=(
                f"🔊 Spoke {clip.characters} character"
                f"{'s' if clip.characters != 1 else ''} as a voice message."
            ),
            data={
                "characters": clip.characters,
                "mime_type": clip.mime_type,
                "voice": clip.voice,
                "model": clip.model,
            },
        )
