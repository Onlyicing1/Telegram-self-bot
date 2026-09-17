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

The engine seam is untouched. ``apply_stt_settings_now`` reads the store and
hands the persisted selection to the ONE candidate → engine seam
(``backend/services/stt_engine_factory``), which builds the engine of the
SELECTED candidate's own provider and installs it through the EXISTING media
boundary — so no Telegram object, owner id, chat id, message id or caption can
reach an engine, and a Telegram change is effective on the next media operation
with no redeploy and no restart.

Each candidate also shows its PROVIDER-TEST state (``backend/ai/stt_provider_probe``)
and can be probed from the panel with one bounded request. The panel never claims
health from the mere existence of a credential: an untested candidate says so, a
missing credential is its own state, and only a request that returned a non-empty
transcript is reported as passed.

Kept in its own module because the panels, the inputs and their validation are
one cohesive unit and the AI panel module is already at the file-tool size
limit — exactly the reason ``ai_test_progress.py`` exists.
"""
from __future__ import annotations

import logging
import re

from backend.ai import stt_provider_probe
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
    """The speech-to-text state as ONE line, never raising on a bad value.

    Carries the active candidate's PROVIDER-TEST state as well as the selection,
    so the hub distinguishes "selected but never tested" from "selected and
    verified" without opening the panel — and never implies health from a
    configuration value alone.
    """
    if _unreadable(config):
        return "Speech-to-Text · unavailable (database read failed)"
    plane = parse_stt_config(config)
    candidate = plane.active_candidate
    if plane.is_legacy:
        return f"Speech-to-Text · legacy model `{plane.legacy_model}` · unresolved"
    label = candidate.label if candidate else DEFAULT_CANDIDATE_ID
    state = (            stt_provider_probe.candidate_state_row(candidate)
        if candidate else stt_provider_probe.state_label("")
    )
    passes = plane.passes
    return (
        f"Speech-to-Text · {label} · {plane.language or 'auto'} · "
        f"{passes} pass{'es' if passes > 1 else ''} · {state}"
    )


def _unreadable(config: dict) -> bool:
    from backend.ai.config_store import DEGRADED_READ_KEY

    return bool(config.get(DEGRADED_READ_KEY))


async def apply_stt_settings_now(owner_id: int) -> bool:
    """Hand the owner's persisted STT selection to the live media engine.

    The HANDLER reads the store; the engine factory converts the persisted
    selection into the SELECTED candidate's own provider engine — the engine
    never learns an owner id and never sees a Telegram object. Returns whether a
    live STT engine was provisioned (a missing credential, and a candidate with
    no execution path, stay fail-closed).
    """
    from backend.ai.config_store import get_config
    from backend.services.stt_engine_factory import apply_stt_config

    status = apply_stt_config(await get_config(owner_id))
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
        suffix += f" · {    stt_provider_probe.candidate_state_row(candidate)}"
        lines.append(f"{index}. {candidate.label}{suffix}")
    lines.append("")
    lines.append("_Pick a registered candidate — no model names to type._")
    lines.append("_Test runs one bounded request to that candidate and reports the result._")

    builder = InlinePanelBuilder()
    for candidate in all_candidates():
        if not candidate.implemented:
            continue
        buttons: list[tuple[str, str]] = []
        if plane.is_legacy or candidate.candidate_id != plane.active_id:
            buttons.append((
                f"Use {candidate.label}",
                f"action:ai_stt_select_candidate:{candidate.candidate_id}",
            ))
        buttons.append(("Test", f"action:ai_stt_test_candidate:{candidate.candidate_id}"))
        builder.add_buttons(*buttons)
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


# ── Provider probe (one bounded request per candidate) ─────────────────


def test_notice(result: "stt_provider_probe.SttTestResult") -> str:
    """ONE bounded owner-facing notice for a finished probe.

    Never carries a credential, a transcript or a Telegram identifier: the state
    wording, the bounded failure class, the elapsed time and the probe's own
    bounded explanation are all that is shown.
    """
    candidate = get_candidate(result.candidate_id)
    name = candidate.label if candidate else (result.candidate_id or "candidate")
    state = stt_provider_probe.SttTestState
    if result.state == state.PASSED.value:
        return f"\u2713 {name} · {result.summary()}"
    if result.state == state.CREDENTIAL_MISSING.value:
        return f"! {name} · {result.summary()} — nothing was sent."
    if result.state == state.NOT_IMPLEMENTED.value:
        return f"! {name} · {result.state_label()} on this runtime."
    detail = f" — {result.detail}" if result.detail else ""
    return f"\u00d7 {name} · {result.summary()}{detail}"


async def _ai_stt_test_candidate_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Probe ONE registered candidate with one bounded request.

    The payload is the provider test's own bounded in-process audio; a candidate
    the registry does not know is refused without a request, and an unimplemented
    or credential-less candidate is reported honestly instead of being probed into
    a misleading success. The refreshed panel then shows the recorded state.
    """
    candidate = get_candidate(extra)
    if candidate is None:
        return await _stt_panel_with_notice(
            "\u00d7 Unknown transcription candidate — nothing was tested."
        )
    result = await stt_provider_probe.test_candidate(candidate.candidate_id)
    return await _stt_panel_with_notice(test_notice(result))


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
        register_action("ai_stt_test_candidate", _ai_stt_test_candidate_action)
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
