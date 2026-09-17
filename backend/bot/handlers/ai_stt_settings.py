"""Gemini STT settings — the Voice transcription controls of AI → Settings.

The three behavioral speech-to-text settings (model, language hint, recognition
passes) are OWNER settings, not deployment configuration: they are persisted on
the owner's existing ``ai_config`` row through ``backend/ai/config_store.py``
(the same store every other AI setting uses — no second store), edited from
Telegram, and handed to the live media engine so a change is effective on the
next voice message with no env edit and no redeploy. API keys stay in ENV; only
these behavior values live here.

Kept in its own module because the three input handlers plus their validation
are one cohesive unit and the AI panel module is already at the file-tool size
limit — exactly the reason ``ai_test_progress.py`` exists. The controls attach
to the EXISTING ``ai_settings`` Advanced panel; no new panel or category is
created, and the input registry stays the single one.
"""
from __future__ import annotations

import logging
import re

from backend.helper import register_input

logger = logging.getLogger(__name__)

#: A transcription model id is ONE opaque token — the same free-form value the
#: engine's own ENV resolution accepts. Deliberately not a model registry: an id
#: this project does not know is stored and passed through unchanged, never
#: silently rewritten into another model.
_STT_MODEL_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9._:/-]{0,63}$")

#: A BCP-47 language tag: a 2–3 letter primary subtag plus optional subtags
#: (``fa``, ``fa-IR``, ``en-US``). Deterministic shape check only — no fuzzy
#: parsing and no language list to drift.
_STT_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")

#: What the owner may type instead of a value to go back to the default.
_STT_RESET_WORDS = frozenset({"reset", "clear", "default", "none"})


def stt_settings_display(config: dict) -> tuple[str, str, int]:
    """``(model, language, passes)`` for display; never raises on a bad value."""
    from backend.services.gemini_media_engine import STT_MAX_PASSES

    model = str(config.get("stt_model") or "").strip()
    language = str(config.get("stt_language") or "").strip()
    try:
        passes = int(config.get("stt_passes"))
    except (TypeError, ValueError):
        passes = 1
    return model, language, max(1, min(passes, STT_MAX_PASSES))


def stt_summary_line(config: dict) -> str:
    """The transcription state as ONE line of text on the Settings surface."""
    model, language, passes = stt_settings_display(config)
    return (
        f"Voice transcription · {model or 'default model'} · "
        f"{language or 'auto'} · {passes} pass{'es' if passes > 1 else ''}"
    )


async def apply_stt_settings_now(owner_id: int) -> bool:
    """Hand the owner's persisted STT settings to the live media engine.

    The HANDLER reads the store — the engine never does — and passes plain
    values in, so no Telegram object, chat id, message id or caption can reach
    the engine, and a Telegram change is effective on the NEXT media operation
    with no redeploy and no restart. Returns whether a live STT engine was
    reconfigured (a runtime with no credential stays fail-closed).
    """
    from backend.ai.config_store import get_config
    from backend.services.gemini_media_engine import apply_stt_settings

    status = apply_stt_settings(await get_config(owner_id))
    return bool(status.get("configured"))


async def _owner_id() -> int:
    from backend.bot.handlers.ai import _get_owner_id

    return await _get_owner_id()


async def _finish(notice, chat_id, msg_id, inline_chat_id, inline_msg_id):
    """Close the flow with ONE edit: notice on top of the refreshed panel."""
    from backend.bot.handlers.ai import _ai_settings_adv_panel_handler, _finish_input

    await _finish_input(
        notice, _ai_settings_adv_panel_handler,
        chat_id, msg_id, inline_chat_id, inline_msg_id,
    )


async def _ai_stt_model_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    """Set the dedicated transcription model (reset → the default model).

    Durable and owner-specific through the existing AI config path; the new
    value is pushed to the live STT engine in the same turn, so the next voice
    message uses it without an env edit and without a redeploy.
    """
    from backend.ai.config_store import update_setting

    owner = await _owner_id()
    value = text.strip()
    if value.lower() in _STT_RESET_WORDS:
        await update_setting(owner, "stt_model", "")
        await apply_stt_settings_now(owner)
        result = "✓ Transcription model reset to the default."
    elif not _STT_MODEL_RE.match(value):
        result = "× Enter a model name like gemini-2.5-flash (no spaces)."
    else:
        await update_setting(owner, "stt_model", value)
        await apply_stt_settings_now(owner)
        result = f"✓ Transcription model set to `{value}`"
    await _finish(result, chat_id, msg_id, inline_chat_id, inline_msg_id)


async def _ai_stt_language_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    """Set the transcription language hint (empty/auto → automatic detection)."""
    from backend.ai.config_store import update_setting
    from backend.services.gemini_media_engine import STT_LANGUAGE_AUTO

    owner = await _owner_id()
    value = text.strip()
    if (
        not value
        or value.lower() in _STT_RESET_WORDS
        or value.lower() == STT_LANGUAGE_AUTO
    ):
        await update_setting(owner, "stt_language", "")
        await apply_stt_settings_now(owner)
        result = "✓ Transcription language set to automatic."
    elif not _STT_LANGUAGE_RE.match(value):
        result = "× Enter a language code like fa-IR, or 'auto'."
    else:
        await update_setting(owner, "stt_language", value)
        await apply_stt_settings_now(owner)
        result = f"✓ Transcription language set to `{value}`"
    await _finish(result, chat_id, msg_id, inline_chat_id, inline_msg_id)


async def _ai_stt_passes_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    """Set the bounded recognition-pass count: an integer in ``1..3``.

    An out-of-range or non-integer value is REFUSED (the existing Advanced
    controls reject rather than clamp), because silently turning a typed 9 into
    3 would hide a mistaken request; ``1`` remains the single-pass default.
    """
    from backend.ai.config_store import update_setting
    from backend.services.gemini_media_engine import STT_MAX_PASSES

    owner = await _owner_id()
    try:
        passes = int(text.strip())
    except ValueError:
        result = "× Enter a whole number: 1, 2 or 3."
    else:
        if 1 <= passes <= STT_MAX_PASSES:
            await update_setting(owner, "stt_passes", passes)
            await apply_stt_settings_now(owner)
            result = f"✓ Voice recognition passes set to {passes}"
        else:
            result = f"× Passes must be 1, 2 or 3 — refused {passes}. 1 is the fastest."
    await _finish(result, chat_id, msg_id, inline_chat_id, inline_msg_id)


def register(client=None, owner_id: int = 0) -> None:
    """Attach the three Voice transcription inputs to the EXISTING AI Settings.

    Same scope and mechanism as every other AI setting (``input:ai_settings:*``)
    — no new panel, no new registry, no new store. Labels never name an env
    variable.
    """
    try:
        register_input("ai_settings", "stt_model", {
            "handler": _ai_stt_model_input,
            "prompt": (
                "**Voice transcription model**\n\n"
                "The model that hears your voice messages.\n"
                "Send 'reset' for the default model.\n\n_Reply below._"
            ),
        })
        register_input("ai_settings", "stt_language", {
            "handler": _ai_stt_language_input,
            "prompt": (
                "**Voice transcription language**\n\n"
                "A code like fa-IR, or 'auto' to detect it.\n\n_Reply below._"
            ),
        })
        register_input("ai_settings", "stt_passes", {
            "handler": _ai_stt_passes_input,
            "prompt": (
                "**Voice recognition passes**\n\n"
                "1, 2 or 3 — how many independent readings of the same voice "
                "message to compare.\n1 is the fastest.\n\n_Reply below._"
            ),
        })
    except Exception as exc:  # noqa: BLE001 — registration is never fatal
        logger.error("STT settings registration FAILED: %s", exc)
