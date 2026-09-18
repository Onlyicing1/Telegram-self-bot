"""Media Analysis — the AI surfaces for text recognition (OCR) and speech-to-text.

This module owns ONE AI sub-surface and everything behind it:

    AI
    └── Media Analysis            (``ai_media``)
        ├── Text recognition       (``ai_media_ocr``)
        ├── Speech-to-Text         (``ai_media_stt``)
        │   └── STT Settings       (``ai_media_stt_settings``)
        └── Text-to-Speech         (``ai_media_tts``, see ``ai_tts_settings``)

The Speech-to-Text screen is a compact control panel: the active candidate and
its state, the ordered provider list with each candidate's own state, ONE global
provider test, the candidate selection as two-column buttons, and a single way
into the bounded behavioral settings. The behavioral settings (language,
recognition passes) live in the nested **STT Settings** panel so they do not
occupy the main screen; they keep their existing storage keys, input ids and
validation, and the nested panel uses the shared panel/navigation registry, so
Back returns to Speech-to-Text and Home returns to the usual navigation.

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

Each candidate shows its PROVIDER-TEST state (``backend/ai/stt_provider_probe``),
and the panel offers ONE bounded **Test all providers** control that probes every
implemented candidate in the registry's canonical order and re-renders the panel
once. The panel never claims health from the mere existence of a credential: an
untested candidate says so, a missing credential is its own state, and only a
request that returned a non-empty transcript is reported as passed. The probe is
a capability/transport check — its payload is a synthetic tone, so it is never
presented as a recognition-quality measurement.

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

#: Presentation-only button text for the two-column candidate grid: the registry
#: labels are too wide for a row of two on a phone. A candidate id this map does
#: not know falls back to its registry label, so a newly registered capability is
#: still offered (and still selectable) without touching this table. The callback
#: payload — and therefore the selection semantics — never depends on the text.
_CANDIDATE_BUTTON_LABELS: dict[str, str] = {
    "gemini:default": "Gemini",
    "gemini:gemini-3.5-transcribe": "Gemini Transcribe",
    "groq:whisper-large-v3": "Groq v3",
    "groq:whisper-large-v3-turbo": "Groq Turbo",
    "speechmatics:standard": "Speechmatics",
}


def _candidate_button(candidate) -> tuple[str, str]:
    """``(text, callback)`` for ONE candidate's selection button.

    The callback payload is the registered candidate id and is NEVER derived from
    the button text, so shortening a label can never change which candidate is
    selected.
    """
    label = _CANDIDATE_BUTTON_LABELS.get(candidate.candidate_id, candidate.label)
    return f"Use {label}", f"action:ai_stt_select_candidate:{candidate.candidate_id}"


def _two_column_rows(
    buttons: list[tuple[str, str]],
) -> list[list[tuple[str, str]]]:
    """Chunk candidate buttons into deterministic two-column rows.

    Order is the caller's (the registry's canonical order) and the chunk size is
    fixed, so the layout is a pure function of the candidate list — a changed
    registry re-flows the rows without any hard-coded wrapping. An odd trailing
    button keeps its own row.
    """
    return [buttons[index:index + 2] for index in range(0, len(buttons), 2)]


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
    never learns an owner id and never sees a Telegram object. The same call
    reloads the provider credential pools (M2.4), so a selection saved from the
    panel is effective for BOTH the provider fallback order and the credential
    rotation on the very next media operation, with no redeploy and no restart.
    Returns whether a live STT engine was provisioned (a missing credential, and
    a candidate with no execution path, stay fail-closed).
    """
    from backend.ai.config_store import get_config
    from backend.services.stt_engine_factory import apply_stt_config_async

    status = await apply_stt_config_async(await get_config(owner_id))
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

    from backend.bot.handlers.ai_tts_settings import tts_status_line

    lines = [
        "**Media Analysis**",
        "",
        "Reads what the messages you reply to actually contain — and speaks text back.",
        "",
        f"Text recognition · {'Ready' if ocr_ready else 'Not configured'}",
        stt_selection_line(config),
        tts_status_line(),
    ]

    builder = InlinePanelBuilder()
    builder.add_row("Text recognition (OCR)", "panel:ai_media_ocr")
    builder.add_row("Speech-to-Text", "panel:ai_media_stt")
    builder.add_row("Text-to-Speech", "panel:ai_media_tts")
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


def _media_stt_lines(plane, unreadable: bool) -> list[str]:
    """The compact owner-facing state block: what is active and what is available.

    Only real runtime states are shown — a failed database read, an unresolved
    legacy selection and a registered-but-unavailable active candidate all stay
    visible, because hiding one for visual cleanliness would misreport the
    runtime. Nothing here claims provider health: each candidate carries its own
    probe state and the probe reports credential presence separately.
    """
    lines = ["**Speech-to-Text**", ""]
    if unreadable:
        lines.append("! Current selection unavailable (database read failed).")
    elif plane.is_legacy:
        lines.append(
            f"! Legacy model `{plane.legacy_model}` is not a registered "
            "candidate — it keeps running unchanged."
        )
    else:
        active = plane.active_candidate
        lines.append(f"Active · {active.label if active else DEFAULT_CANDIDATE_ID}")
        if plane.active_unavailable:
            lines.append(
                f"! {active.label} is registered but not available on this runtime "
                "— the default route is used."
            )
    lines.append(f"Language · {plane.language or 'Auto'}")
    lines.append(f"Passes · {plane.passes}")
    lines.append("")
    ranked = plane.ordered_candidates if not plane.is_legacy else all_candidates()
    lines.append("Providers")
    for index, candidate in enumerate(ranked, start=1):
        role = plane.candidate_role(candidate.candidate_id)
        suffix = " · active" if role == "active" else ""
        suffix += f" · {stt_provider_probe.candidate_state_row(candidate)}"
        lines.append(f"{index}. {candidate.label}{suffix}")
    return lines


async def _media_stt_body_and_buttons(config: dict) -> tuple[str, list]:
    """The Speech-to-Text screen: active candidate, provider states, controls.

    Deliberately compact — the state block says only what the owner needs to pick
    a candidate, the provider list carries each candidate's own state, candidate
    selection is a two-column grid built from the registry, and the bounded
    behavioral settings live one level down in the nested STT Settings panel.
    """
    from backend.bot.handlers.ai import _nav_buttons

    unreadable = _unreadable(config)
    plane = parse_stt_config({} if unreadable else config)

    lines = _media_stt_lines(plane, unreadable)
    lines.append("")
    lines.append("_Test all = synthetic capability probe, not a quality benchmark._")

    builder = InlinePanelBuilder()
    builder.add_row("Test all providers", "action:ai_stt_test_all")
    selectable = [
        _candidate_button(candidate)
        for candidate in all_candidates()
        if candidate.implemented
        and (plane.is_legacy or candidate.candidate_id != plane.active_id)
    ]
    for row in _two_column_rows(selectable):
        if len(row) == 1:
            builder.add_row(*row[0])
        else:
            builder.add_buttons(*row)
    builder.add_row("\u2699 STT Settings", "panel:ai_media_stt_settings")
    _nav_buttons(builder)
    return "\n".join(lines), builder.build()


async def _ai_media_stt_settings_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    """The bounded behavioral STT settings: language and recognition passes.

    Only presentation moved here. The inputs are the SAME registered inputs the
    main screen used to render (``input:ai_media_stt:stt_language`` /
    ``input:ai_media_stt:stt_passes``), so their ids, prompts, validation and the
    persistence keys they write are unchanged.
    """
    from backend.bot.handlers.ai import _nav_buttons

    _owner, config = await _saved_config()
    unreadable = _unreadable(config)
    plane = parse_stt_config({} if unreadable else config)

    lines = ["**\u2699 STT Settings**", ""]
    if unreadable:
        lines.append("! Current settings unavailable (database read failed).")
    lines.append(f"Language · {plane.language or 'Auto'}")
    lines.append(f"Passes · {plane.passes}")

    builder = InlinePanelBuilder()
    builder.add_row("Language…", f"input:ai_media_stt:{STORAGE_KEY_LANGUAGE}")
    builder.add_row("Recognition passes…", f"input:ai_media_stt:{STORAGE_KEY_PASSES}")
    _nav_buttons(builder)
    return "STT Settings", "\n".join(lines), builder.build()


async def _ai_media_stt_settings_inline_builder(event, extra: str) -> list:
    result = await _ai_media_stt_settings_panel_handler(event, extra)
    if result is None:
        return [render("STT Settings", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


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


# ── Provider probe (ONE global test action, one bounded request per candidate) ──


def _candidate_line(result: "stt_provider_probe.SttTestResult") -> str:
    """ONE bounded owner-facing line for a finished probe.

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


def test_notice(result: "stt_provider_probe.SttTestResult") -> str:
    """ONE bounded owner-facing notice for a finished probe."""
    return _candidate_line(result)


def test_all_notice(results: list) -> str:
    """ONE bounded owner-facing summary of a whole provider-test run.

    Shows every candidate's own state — including the credential-less and
    not-implemented ones, which were never sent a request — and states once, in
    the same notice, that this is a capability probe rather than a
    recognition-quality measurement. It never implies that a passing probe means
    good transcription of real speech.
    """
    if not results:
        return "\u00d7 No registered candidate could be tested."
    count = len(results)
    lines = [f"Provider test · {count} candidate{'s' if count != 1 else ''}", ""]
    lines.extend(_candidate_line(result) for result in results)
    lines.append("")
    lines.append(
        "_Capability probe only — one bounded request per candidate with a "
        "synthetic tone. It does not measure recognition quality._"
    )
    return "\n".join(lines)


async def _ai_stt_test_all_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Probe EVERY registered candidate with one bounded request each.

    The candidates are the registry's own and the provider-aware order is the
    probe's: only implemented capabilities are executed, an unimplemented one is
    reported as such WITHOUT a request, and a provider with no credential is
    reported separately from a provider that answered. The panel is re-rendered
    ONCE with every result — never one message per provider.
    """
    results = await stt_provider_probe.test_candidates()
    return await _stt_panel_with_notice(test_all_notice(results))


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
        register_panel(
            "ai_media_stt_settings", _ai_media_stt_settings_panel_handler,
            parent="ai_media_stt", title="STT Settings",
        )
        register_inline_builder("ai_media_stt_settings", _ai_media_stt_settings_inline_builder)
        register_action("ai_stt_select_candidate", _ai_stt_select_candidate_action)
        register_action("ai_stt_test_all", _ai_stt_test_all_action)
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
