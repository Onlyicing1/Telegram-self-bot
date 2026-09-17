"""Media Analysis — the AI surfaces for text recognition (OCR) and speech-to-text.

This module owns ONE AI sub-surface and everything behind it:

    AI
    └── Media Analysis            (``ai_media``)
        ├── Text recognition       (``ai_media_ocr``)
        └── Speech-to-Text         (``ai_media_stt``)

Speech-to-Text used to be three free-form controls inside **AI → Settings →
Advanced** (a model identifier the owner typed, a language code and a pass
count). A model identifier is no longer something the owner types: the
SPEECH-TO-TEXT CONTROL PLANE (``backend/ai/stt_control_plane.py``) registers the
candidates this project knows, and the owner PICKS one from the panel. The
capability registry, the ordering and the persistence mapping live there; this
module only renders them and turns a tap into a persisted selection.

The two remaining behavioral settings (language, recognition passes) and the
candidate selection are owner settings persisted on the owner's existing
``ai_config`` row through ``backend/ai/config_store.py`` — the same store every
other AI setting uses, no second store, no new table, no new column. API keys
stay in ENV: nothing here reads or writes a credential, and no label or prompt
names an environment variable.

The engine seam is untouched. ``apply_stt_settings_now`` reads the store,
converts the persisted selection through the control plane into the three plain
values the EXISTING media boundary understands and hands them to
``gemini_media_engine.apply_stt_settings`` — so no Telegram object, owner id,
chat id, message id or caption can reach the engine, and a Telegram change is
effective on the next media operation with no redeploy and no restart.

Kept in its own module because the panels, the inputs and their validation are
one cohesive unit and the AI panel module is already at the file-tool size
limit — exactly the reason ``ai_test_progress.py`` exists.
"""
from __future__ import annotations

import logging
import re

from backend.ai.stt_control_plane import (
    DEFAULT_CANDIDATE_ID,
    STORAGE_KEY_ACTIVE,
    STORAGE_KEY_LANGUAGE,
    STORAGE_KEY_PASSES,
    all_candidates,
    get_candidate,
    parse_stt_config,
    storage_value,
)
from backend.helper import (
    InlinePanelBuilder,
    register_action,
    register_inline_builder,
    register_input,
    register_panel,
    render,
)

logger = logging.getLogger(__name__)

#: What the owner may type instead of a value to go back to the default.
_STT_RESET_WORDS = frozenset({"reset", "clear", "default", "none"})

#: A BCP-47 language tag: a 2–3 letter primary subtag plus optional subtags
#: (``fa``, ``fa-IR``, ``en-US``). Deterministic shape check only — no fuzzy
#: parsing and no language list to drift.
_STT_LANGUAGE_RE = re.compile(r"^[A-Za-z]{2,3}(?:-[A-Za-z0-9]{2,8})*$")


def stt_selection_line(config: dict) -> str:
    """The speech-to-text state as ONE line, never raising on a bad value."""
    if _unreadable(config):
        return "Speech-to-Text · unavailable (database read failed)"
    plane = parse_stt_config(config)
    candidate = plane.active_candidate
    if plane.is_legacy:
        return f"Speech-to-Text · legacy model `{plane.legacy_model}` · unresolved"
    label = candidate.label if candidate else DEFAULT_CANDIDATE_ID
    passes = plane.passes
    return (
        f"Speech-to-Text · {label} · {plane.language or 'auto'} · "
        f"{passes} pass{'es' if passes > 1 else ''}"
    )


def _unreadable(config: dict) -> bool:
    from backend.ai.config_store import DEGRADED_READ_KEY

    return bool(config.get(DEGRADED_READ_KEY))


async def apply_stt_settings_now(owner_id: int) -> bool:
    """Hand the owner's persisted STT selection to the live media engine.

    The HANDLER reads the store and converts the persisted selection through the
    control plane; the engine never learns an owner id and never sees a Telegram
    object. Returns whether a live STT engine was reconfigured (a runtime with no
    credential stays fail-closed).
    """
    from backend.ai.config_store import get_config
    from backend.ai.stt_control_plane import engine_settings
    from backend.services.gemini_media_engine import apply_stt_settings

    status = apply_stt_settings(engine_settings(await get_config(owner_id)))
    return bool(status.get("configured"))


async def _owner_id() -> int:
    from backend.bot.handlers.ai import _get_owner_id

    return await _get_owner_id()


async def _saved_config() -> tuple[int, dict]:
    from backend.bot.handlers.ai import _get_owner_id, _get_saved_config

    owner = await _get_owner_id()
    return owner, await _get_saved_config(owner)


# ── AI → Media Analysis ────────────────────────────────────────────────


async def _ai_media_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    """The Media Analysis hub: the two capabilities this subsystem exposes."""
    from backend.bot.handlers.ai import _nav_buttons

    _owner, config = await _saved_config()
    ocr_ready = _ocr_ready()

    lines = [
        "**Media Analysis**",
        "",
        "Reads what the messages you reply to actually contain.",
        "",
        f"Text recognition · {'Ready' if ocr_ready else 'Not configured'}",
        stt_selection_line(config),
    ]

    builder = InlinePanelBuilder()
    builder.add_row("Text recognition (OCR)", "panel:ai_media_ocr")
    builder.add_row("Speech-to-Text", "panel:ai_media_stt")
    _nav_buttons(builder)
    return "Media Analysis", "\n".join(lines), builder.build()


async def _ai_media_inline_builder(event, extra: str) -> list:
    result = await _ai_media_panel_handler(event, extra)
    if result is None:
        return [render("Media Analysis", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


def _ocr_ready() -> bool:
    try:
        from backend.services import media_service

        return bool(media_service.ocr_available())
    except Exception as exc:  # noqa: BLE001 — a status line is never fatal
        logger.warning("MEDIA_ANALYSIS_OCR_STATUS_FAILED error=%s", type(exc).__name__)
        return False


async def _ai_media_ocr_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    """Read-only OCR state. OCR configuration stays a deployment concern."""
    from backend.bot.handlers.ai import _nav_buttons

    ready = _ocr_ready()
    lines = ["**Text recognition**", ""]
    if ready:
        lines.append("Images are read by the OCR engine this runtime provisions.")
        lines.append("")
        lines.append(f"Engine · Gemini · {_ocr_model()}")
        lines.append("")
        lines.append("_No owner controls — the engine and its credential are deployment configuration._")
    else:
        lines.append("! No OCR engine on this runtime.")
        lines.append("")
        lines.append("_Images are reported as unsupported until an engine is provisioned._")

    builder = InlinePanelBuilder()
    _nav_buttons(builder)
    return "OCR", "\n".join(lines), builder.build()


async def _ai_media_ocr_inline_builder(event, extra: str) -> list:
    result = await _ai_media_ocr_panel_handler(event, extra)
    if result is None:
        return [render("OCR", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


def _ocr_model() -> str:
    try:
        from backend.services.gemini_media_engine import resolve_media_model

        return resolve_media_model()[0] or "default"
    except Exception as exc:  # noqa: BLE001 — a status line is never fatal
        logger.warning("MEDIA_ANALYSIS_OCR_MODEL_FAILED error=%s", type(exc).__name__)
        return "default"


async def _media_stt_body_and_buttons(config: dict) -> tuple[str, list]:
    """The Speech-to-Text section: active candidate, pool, bounded settings."""
    from backend.bot.handlers.ai import _nav_buttons

    unreadable = _unreadable(config)
    plane = parse_stt_config({} if unreadable else config)

    lines = ["**Speech-to-Text**", ""]
    if unreadable:
        lines.append("! Current selection unavailable (database read failed).")
    elif plane.is_legacy:
        lines.append(f"! Legacy model `{plane.legacy_model}` is not a registered candidate.")
        lines.append("_It keeps running unchanged until you pick one below._")
    else:
        active = plane.active_candidate
        lines.append(f"Active · {active.label if active else DEFAULT_CANDIDATE_ID}")
        if plane.active_unavailable:
            lines.append(
                f"! {active.label} is registered but not available on this runtime "
                "— the default route is used."
            )
    lines.append(f"Language · {plane.language or 'Auto'}")
    lines.append(
        f"Recognition passes · {plane.passes}"
        f"{' (single pass)' if plane.passes == 1 else ''}"
    )
    lines.append("")
    ranked = plane.ordered_candidates if not plane.is_legacy else all_candidates()
    lines.append("Fallback order" if not plane.is_legacy else "Registered candidates")
    for index, candidate in enumerate(ranked, start=1):
        role = plane.candidate_role(candidate.candidate_id)
        suffix = " · active" if role == "active" else ""
        if not candidate.implemented:
            suffix += f" · {candidate.status_word().lower()}"
        lines.append(f"{index}. {candidate.label}{suffix}")
    lines.append("")
    lines.append("_Pick a registered candidate — no model names to type._")

    builder = InlinePanelBuilder()
    for candidate in all_candidates():
        if not candidate.implemented or candidate.candidate_id == plane.active_id:
            continue
        builder.add_row(f"Use {candidate.label}", f"action:ai_stt_select_candidate:{candidate.candidate_id}")
    builder.add_row("Language…", f"input:ai_media_stt:{STORAGE_KEY_LANGUAGE}")
    builder.add_row("Recognition passes…", f"input:ai_media_stt:{STORAGE_KEY_PASSES}")
    _nav_buttons(builder)
    return "\n".join(lines), builder.build()


async def _ai_media_stt_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    _owner, config = await _saved_config()
    body, buttons = await _media_stt_body_and_buttons(config)
    return "Speech-to-Text", body, buttons


async def _ai_media_stt_inline_builder(event, extra: str) -> list:
    result = await _ai_media_stt_panel_handler(event, extra)
    if result is None:
        return [render("Speech-to-Text", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


async def _stt_panel_with_notice(notice: str) -> tuple[str, str, list]:
    """Re-render the Speech-to-Text panel with a notice on top (one edit)."""
    result = await _ai_media_stt_panel_handler(None, "")
    if result is None:
        return "Speech-to-Text", notice, []
    title, body, buttons = result
    return title, f"{notice}\n\n{body}", buttons


# ── Candidate selection (an explicit finite action, never typed input) ──


async def _ai_stt_select_candidate_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Make a REGISTERED candidate the active one.

    The payload is a candidate id from the registry the panel rendered; an
    unknown or not-yet-implemented id changes nothing and says so, and no stored
    value is ever reinterpreted as another candidate.
    """
    candidate = get_candidate(extra)
    if candidate is None:
        return await _stt_panel_with_notice("× Unknown transcription candidate — nothing changed.")
    if not candidate.implemented:
        return await _stt_panel_with_notice(
            f"× {candidate.label} is registered but not available on this runtime yet."
        )

    from backend.ai.config_store import update_setting

    owner = await _owner_id()
    await update_setting(owner, STORAGE_KEY_ACTIVE, storage_value(candidate.candidate_id))
    await apply_stt_settings_now(owner)
    return await _stt_panel_with_notice(f"✓ Speech-to-Text now uses {candidate.label}")


# ── Behavioral settings (bounded, owner-editable) ──────────────────────


async def _ai_stt_language_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    """Set the transcription language hint (empty/auto → automatic detection)."""
    from backend.ai.config_store import update_setting
    from backend.bot.handlers.ai import _finish_input
    from backend.services.gemini_media_engine import STT_LANGUAGE_AUTO

    owner = await _owner_id()
    value = text.strip()
    if (
        not value
        or value.lower() in _STT_RESET_WORDS
        or value.lower() == STT_LANGUAGE_AUTO
    ):
        await update_setting(owner, STORAGE_KEY_LANGUAGE, "")
        await apply_stt_settings_now(owner)
        result = "✓ Transcription language set to automatic."
    elif not _STT_LANGUAGE_RE.match(value):
        result = "× Enter a language code like fa-IR, or 'auto'."
    else:
        await update_setting(owner, STORAGE_KEY_LANGUAGE, value)
        await apply_stt_settings_now(owner)
        result = f"✓ Transcription language set to `{value}`"
    await _finish_input(
        result, _ai_media_stt_panel_handler,
        chat_id, msg_id, inline_chat_id, inline_msg_id,
    )


async def _ai_stt_passes_input(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
    """Set the bounded recognition-pass count: an integer in ``1..3``.

    An out-of-range or non-integer value is REFUSED (the existing controls
    reject rather than clamp), because silently turning a typed 9 into 3 would
    hide a mistaken request; ``1`` remains the single-pass default.
    """
    from backend.ai.config_store import update_setting
    from backend.bot.handlers.ai import _finish_input
    from backend.services.gemini_media_engine import STT_MAX_PASSES

    owner = await _owner_id()
    try:
        passes = int(text.strip())
    except ValueError:
        result = "× Enter a whole number: 1, 2 or 3."
    else:
        if 1 <= passes <= STT_MAX_PASSES:
            await update_setting(owner, STORAGE_KEY_PASSES, passes)
            await apply_stt_settings_now(owner)
            result = f"✓ Voice recognition passes set to {passes}"
        else:
            result = f"× Passes must be 1, 2 or 3 — refused {passes}. 1 is the fastest."
    await _finish_input(
        result, _ai_media_stt_panel_handler,
        chat_id, msg_id, inline_chat_id, inline_msg_id,
    )


def register(client=None, owner_id: int = 0) -> None:
    """Attach the Media Analysis panels and the STT controls to the ONE registry.

    Same scope and mechanism as every other AI panel (``panel:*`` /
    ``input:*`` / ``action:*``) — no parallel UI framework, no second registry,
    no second store. Labels never name an environment variable.
    """
    try:
        register_panel("ai_media", _ai_media_panel_handler, parent="ai", title="Media Analysis")
        register_inline_builder("ai_media", _ai_media_inline_builder)
        register_panel("ai_media_ocr", _ai_media_ocr_panel_handler, parent="ai_media", title="Text recognition")
        register_inline_builder("ai_media_ocr", _ai_media_ocr_inline_builder)
        register_panel("ai_media_stt", _ai_media_stt_panel_handler, parent="ai_media", title="Speech-to-Text")
        register_inline_builder("ai_media_stt", _ai_media_stt_inline_builder)
        register_action("ai_stt_select_candidate", _ai_stt_select_candidate_action)
        register_input("ai_media_stt", STORAGE_KEY_LANGUAGE, {
            "handler": _ai_stt_language_input,
            "prompt": (
                "**Voice transcription language**\n\n"
                "A code like fa-IR, or 'auto' to detect it.\n\n_Reply below._"
            ),
        })
        register_input("ai_media_stt", STORAGE_KEY_PASSES, {
            "handler": _ai_stt_passes_input,
            "prompt": (
                "**Voice recognition passes**\n\n"
                "1, 2 or 3 — how many independent readings of the same voice "
                "message to compare.\n1 is the fastest.\n\n_Reply below._"
            ),
        })
    except Exception as exc:  # noqa: BLE001 — registration is never fatal
        logger.error("Media Analysis registration FAILED: %s", exc)
