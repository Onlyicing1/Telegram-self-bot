"""Media Analysis — the AI surface for text-to-speech.

This module owns ONE AI sub-surface:

    AI
    └── Media Analysis            (``ai_media``)
        ├── Text recognition       (``ai_media_ocr``)
        ├── Speech-to-Text         (``ai_media_stt``)
        └── Text-to-Speech         (``ai_media_tts``)   ← this module

Text-to-Speech lives HERE, beside the other media capabilities, and not in AI
Settings or AI Advanced: it is a media capability, and the hub that already lists
text recognition and speech-to-text is where the owner looks for it.

The screen is READ-ONLY, and deliberately so. The first TTS phase has exactly ONE
registered capability (one provider, one model, one fixed voice) and no
owner-facing setting that changes runtime behavior, so there is nothing to
persist and nothing to toggle — which also means this phase adds NO new
``ai_config`` column and NO migration. The screen therefore reports only facts
that are TRUE:

  * the registered provider, model, voice and output format this build would use;
  * the bounded input limit and the synthesis timeout;
  * whether a credential is present on this runtime.

It never claims provider health (a credential existing is not evidence that a
provider answers — only a real request can say that), never prints or hints at a
credential value, and never names an environment variable. A capability that
cannot run says so plainly, in the same fail-closed spirit as the STT surface,
instead of offering a control that would not work.

Registered through the ONE shared panel/navigation registry, exactly like every
other AI panel — no parallel UI framework and no second registry.
"""
from __future__ import annotations

import logging

from backend.helper import InlinePanelBuilder, register_inline_builder, register_panel, render

logger = logging.getLogger(__name__)


def _capability_state(reason: str) -> str:
    """The bounded owner-facing state for a capability reason (never a secret)."""
    if not reason:
        return "Ready"
    if reason == "missing_credential":
        return "No credential on this runtime"
    return "Unavailable on this runtime"


def tts_status_line() -> str:
    """The text-to-speech state as ONE line for the Media Analysis hub.

    Never raises: a status line is decoration, and a broken capability must show
    as an honest state rather than take the hub down.
    """
    try:
        from backend.services import tts_service

        capability = tts_service.describe()
        return f"Text-to-Speech · {_capability_state(capability['reason'])}"
    except Exception as exc:  # noqa: BLE001 — a status line is never fatal
        logger.warning("MEDIA_ANALYSIS_TTS_STATUS_FAILED error=%s", type(exc).__name__)
        return "Text-to-Speech · Unavailable on this runtime"


async def _ai_media_tts_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    """Read-only Text-to-Speech state — no owner controls exist in this phase."""
    from backend.bot.handlers.ai import _nav_buttons

    from backend.services import tts_service

    try:
        capability = tts_service.describe()
        limit = tts_service.MAX_TTS_INPUT_CHARS
        timeout = tts_service.TTS_TIMEOUT_S
    except Exception as exc:  # noqa: BLE001 — a status screen is never fatal
        logger.warning("MEDIA_ANALYSIS_TTS_PANEL_FAILED error=%s", type(exc).__name__)
        builder = InlinePanelBuilder()
        _nav_buttons(builder)
        return (
            "Text-to-Speech",
            "**Text-to-Speech**\n\n! State unavailable.",
            builder.build(),
        )

    lines = ["**Text-to-Speech**", ""]
    if not capability["reason"]:
        lines.append("Speaks text as a voice message when you ask for it.")
        lines.append("")
        lines.append(f"Provider · {capability['provider']}")
        lines.append(f"Model · {capability['model']}")
        lines.append(f"Voice · {capability['voice']}")
        lines.append(f"Output · {capability['format']} ({capability['mime_type']})")
        lines.append(f"Limit · {limit} characters, {timeout:g}s per request")
        lines.append("")
        lines.append(
            "_No owner controls — the provider, model and voice are deployment "
            "configuration in this phase._"
        )
    else:
        lines.append(f"! {_capability_state(capability['reason'])}.")
        lines.append("")
        lines.append(
            "_Speech synthesis is reported as unavailable until a provider "
            "credential is configured. Nothing is sent._"
        )

    builder = InlinePanelBuilder()
    _nav_buttons(builder)
    return "Text-to-Speech", "\n".join(lines), builder.build()


async def _ai_media_tts_inline_builder(event, extra: str) -> list:
    result = await _ai_media_tts_panel_handler(event, extra)
    if result is None:
        return [render("Text-to-Speech", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


def register(client=None, owner_id: int = 0) -> None:
    """Attach the Text-to-Speech panel to the ONE registry (no second surface)."""
    try:
        register_panel(
            "ai_media_tts", _ai_media_tts_panel_handler,
            parent="ai_media", title="Text-to-Speech",
        )
        register_inline_builder("ai_media_tts", _ai_media_tts_inline_builder)
    except Exception as exc:  # noqa: BLE001 — registration is never fatal
        logger.error("Text-to-Speech registration FAILED: %s", exc)
