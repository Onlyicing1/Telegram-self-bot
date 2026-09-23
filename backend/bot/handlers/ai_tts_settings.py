"""Media Analysis — the AI surface for text-to-speech, and its control plane.

This module owns ONE AI sub-surface:

    AI
    └── Media Analysis            (``ai_media``)
        ├── Text recognition       (``ai_media_ocr``)
        ├── Speech-to-Text         (``ai_media_stt``)
        └── Text-to-Speech         (``ai_media_tts``)   ← this module

Text-to-Speech lives HERE, beside the other media capabilities, and not in AI
Settings or AI Advanced: it is a media capability, and the hub that already lists
text recognition and speech-to-text is where the owner looks for it.

The screen is a COMPACT CONTROL PANEL, not a questionnaire. It reports the
provisioned selection and the facts that are true about it (provider, model,
voice, output format, the Persian capability state, the credential pool's bounded
counts, the input limit), and it offers exactly three finite choices:

  * **Provider** — one button per REGISTERED provider. A registered-but-
    unimplemented provider is shown with its state and is NOT selectable, so the
    panel never promises an execution this build cannot perform.
  * **Model…** — a nested panel listing the models the CURRENT provider offers.
  * **Voice…** — a nested panel listing the voices the CURRENT model offers.

Every choice comes from the capability registry
(``backend/ai/tts_control_plane.py``); nothing here accepts a typed model or voice
identifier, so a value outside the registry can never become the selection. A
change persists a CONSISTENT provider/model/voice triple on the owner's existing
``ai_config`` row through ``backend/ai/config_store.py`` (the same store every
other AI setting uses — no second store, no new table) and is applied to the live
boundary immediately, so it is effective on the next request with no redeploy and
no restart.

SELECTION SEMANTICS: choosing a provider selects that provider's default model and
that model's default voice IN THE SAME WRITE, and choosing a model selects that
model's default voice — so changing one level can never leave an incompatible
value from another level persisted. Changing the voice leaves provider and model
alone.

The panel never claims provider health, never prints or hints at a credential
value, and never names an environment variable: the credential pool is reported as
bounded counts (ready/total) and its management stays in the existing API
Credentials surface, which this screen only links to.
"""
from __future__ import annotations

import logging

from backend.ai.tts_control_plane import (
    TTS_PROVIDERS,
    TtsSelection,
    get_provider,
    parse_tts_config,
    resolve,
)
from backend.helper import (
    InlinePanelBuilder,
    register_action,
    register_inline_builder,
    register_panel,
    render,
)

logger = logging.getLogger(__name__)


def _capability_state(reason: str) -> str:
    """The bounded owner-facing state for a capability reason (never a secret)."""
    if not reason:
        return "Ready"
    if reason == "missing_credential":
        return "No credential on this runtime"
    return "Unavailable on this runtime"


def _unreadable(config: dict) -> bool:
    from backend.ai.config_store import DEGRADED_READ_KEY

    return bool(config.get(DEGRADED_READ_KEY))


def _selection_of(config: dict) -> TtsSelection:
    """The selection the panel renders for a config mapping (defaults when empty)."""
    return parse_tts_config({} if _unreadable(config) else config)


async def owner_and_config() -> tuple[int, dict]:
    """The owner id and their persisted AI config, through the existing surface."""
    from backend.bot.handlers.ai import _get_owner_id, _get_saved_config

    owner = await _get_owner_id()
    return owner, await _get_saved_config(owner)


# ── Persistence + runtime application (ONE write, ONE apply) ─────────────────


async def persist_selection(owner_id: int, selection: TtsSelection) -> bool:
    """Persist a CONSISTENT triple on the owner's existing ``ai_config`` row.

    ONE read and ONE write through the existing store (never a key-by-key update
    loop that could leave a half-applied triple behind), so the stored triple is
    always a valid combination for the provider it names.
    """
    from backend.ai.config_store import get_config, save_config

    config = await get_config(owner_id)
    config.update(selection.storage_values())
    return await save_config(owner_id, config)


async def apply_tts_settings_now(owner_id: int) -> bool:
    """Hand the owner's persisted selection to the live boundary.

    The HANDLER reads the store; the boundary resolves the selection and loads the
    credential pools through the same call, so a change is effective for BOTH the
    provider rotation and the credential rotation on the very next request. The
    boundary never learns an owner id and never sees a Telegram object. Returns
    whether synthesis is actually runnable (a missing credential and an
    unimplemented provider stay fail-closed).
    """
    from backend.ai.config_store import get_config
    from backend.services.tts_service import apply_tts_settings_async

    status = await apply_tts_settings_async(await get_config(owner_id))
    return bool(status.get("configured"))


# ── Presentation ────────────────────────────────────────────────────────────


def tts_status_line() -> str:
    """The text-to-speech state as ONE line for the Media Analysis hub.

    Never raises: a status line is decoration, and a broken capability must show
    as an honest state rather than take the hub down.
    """
    try:
        from backend.services import tts_service

        capability = tts_service.describe()
        state = _capability_state(capability["reason"])
        return f"Text-to-Speech · {capability['provider_label']} {capability['model_label']} · {state}"
    except Exception as exc:  # noqa: BLE001 — a status line is never fatal
        logger.warning("MEDIA_ANALYSIS_TTS_STATUS_FAILED error=%s", type(exc).__name__)
        return "Text-to-Speech · Unavailable on this runtime"


def _pool_line(provider: str) -> str:
    """The credential pool's bounded counts — never a credential."""
    try:
        from backend.services import tts_credential_pool

        counts = tts_credential_pool.summarise(provider)
    except Exception as exc:  # noqa: BLE001 — a status line is never fatal
        logger.warning("MEDIA_ANALYSIS_TTS_POOL_LINE_FAILED error=%s", type(exc).__name__)
        return "Credentials · unavailable"
    total = counts.get("total", 0)
    if not total:
        return "Credentials · none stored for this provider"
    return f"Credentials · {counts.get('ready', 0)} ready of {total}"


def _selection_lines(selection: TtsSelection, capability: dict) -> list[str]:
    """The state block: what is selected, what it produces, how it is keyed."""
    from backend.services import tts_service

    lines = [
        f"Provider · {selection.provider_label}",
        f"Model · {selection.model_label}",
        f"Voice · {selection.voice_label}",
    ]
    if capability.get("mime_type"):
        lines.append(f"Output · {capability.get('format') or '-'} ({capability['mime_type']})")
        if capability.get("voice_note") == "not_documented":
            lines.append(
                "! Telegram documents voice messages as OGG/Opus, MP3 or M4A — "
                "this container needs live delivery verification."
            )
    lines.append(f"Persian · {selection.persian_label()}")
    lines.append(_pool_line(selection.provider))
    lines.append(
        f"Limit · {tts_service.MAX_TTS_INPUT_CHARS} characters, "
        f"{tts_service.TTS_TIMEOUT_S:g}s per request"
    )
    if selection.adjusted:
        lines.append("")
        lines.append(f"! Saved selection was adjusted — {selection.adjusted}.")
    return lines


def _provider_lines(selection: TtsSelection) -> list[str]:
    """Every REGISTERED provider and its state, as body text (never a fake button).

    A provider that cannot run on this build is reported as a state and offered no
    control at all, so the owner can never select something that cannot execute.
    """
    lines = ["Providers"]
    for index, entry in enumerate(TTS_PROVIDERS, start=1):
        if not entry.implemented:
            lines.append(f"{index}. {entry.label} · not available yet")
        elif entry.provider == selection.provider:
            lines.append(f"{index}. {entry.label} · current")
        else:
            lines.append(f"{index}. {entry.label} · available")
    return lines


def _provider_buttons(selection: TtsSelection) -> list[tuple[str, str]]:
    """Selection buttons for the providers the owner may actually switch to."""
    return [
        (f"Use {entry.label}", f"action:ai_tts_select:{entry.provider}")
        for entry in TTS_PROVIDERS
        if entry.implemented and entry.provider != selection.provider
    ]


def _tts_body_and_buttons(selection: TtsSelection, config_unreadable: bool) -> tuple[str, list]:
    from backend.bot.handlers.ai import _nav_buttons
    from backend.services import tts_service

    try:
        capability = tts_service.describe()
    except Exception as exc:  # noqa: BLE001 — a status screen is never fatal
        logger.warning("MEDIA_ANALYSIS_TTS_PANEL_FAILED error=%s", type(exc).__name__)
        capability = {"reason": tts_service.FAILURE_UNAVAILABLE}

    lines = ["**Text-to-Speech**", ""]
    if config_unreadable:
        lines.append("! Current selection unavailable (database read failed).")
        lines.append("")
    if not capability.get("reason"):
        lines.append("Speaks text as a voice message when you ask for it.")
        lines.append("")
        lines.extend(_selection_lines(selection, capability))
    else:
        lines.append(f"! {_capability_state(capability['reason'])}.")
        lines.append("")
        lines.append(
            "_Speech synthesis is reported as unavailable until a provider "
            "credential is configured. Nothing is sent._"
        )
        lines.append("")
        lines.extend(_selection_lines(selection, capability))

    lines.append("")
    lines.extend(_provider_lines(selection))

    builder = InlinePanelBuilder()
    builder.add_row("Model…", "panel:ai_media_tts_model")
    builder.add_row("Voice…", "panel:ai_media_tts_voice")
    _add_rows(builder, _two_column_rows(_provider_buttons(selection)))
    builder.add_row("API Credentials", "panel:ai_cred")
    _nav_buttons(builder)
    return "\n".join(lines), builder.build()


async def _ai_media_tts_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    """The Text-to-Speech control panel: selection, capability state, choices."""
    try:
        _owner, config = await owner_and_config()
    except Exception as exc:  # noqa: BLE001 — a status screen is never fatal
        logger.warning("MEDIA_ANALYSIS_TTS_CONFIG_FAILED error=%s", type(exc).__name__)
        config = {}
    unreadable = _unreadable(config)
    selection = _selection_of(config)
    body, buttons = _tts_body_and_buttons(selection, unreadable)
    return "Text-to-Speech", body, buttons


async def _ai_media_tts_inline_builder(event, extra: str) -> list:
    result = await _ai_media_tts_panel_handler(event, extra)
    if result is None:
        return [render("Text-to-Speech", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


async def _tts_panel_with_notice(notice: str) -> tuple[str, str, list]:
    """Re-render the Text-to-Speech panel with a notice on top (one edit)."""
    result = await _ai_media_tts_panel_handler(None, "")
    if result is None:
        return "Text-to-Speech", notice, []
    title, body, buttons = result
    return title, f"{notice}\n\n{body}", buttons


def _two_column_rows(buttons: list[tuple[str, str]]) -> list[list[tuple[str, str]]]:
    """Chunk buttons into deterministic two-column rows (a pure function of order)."""
    return [buttons[index:index + 2] for index in range(0, len(buttons), 2)]


def _add_rows(builder, rows: list[list[tuple[str, str]]]) -> None:
    """Render deterministic rows onto the shared panel builder."""
    for row in rows:
        if len(row) == 1:
            builder.add_row(*row[0])
        else:
            builder.add_buttons(*row)


# ── Model selection (a nested panel: only this provider's models) ────────────


async def _ai_media_tts_model_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.bot.handlers.ai import _nav_buttons

    _owner, config = await owner_and_config()
    selection = _selection_of(config)
    entry = get_provider(selection.provider)
    lines = ["**Model**", "", f"Provider · {selection.provider_label}", ""]
    builder = InlinePanelBuilder()
    if entry is None:  # pragma: no cover - the selection is always registered
        lines.append("! No such provider.")
    else:
        lines.append(f"Model · {selection.model_label}")
        lines.append("")
        lines.append("Models")
        for index, model in enumerate(entry.models, start=1):
            if not model.implemented:
                lines.append(f"{index}. {model.label} · not available yet")
            elif model.model_id == selection.model:
                lines.append(f"{index}. {model.label} · current")
            else:
                lines.append(f"{index}. {model.label} · available")
                builder.add_row(
                    f"Use {model.label}", f"action:ai_tts_select_model:{model.model_id}"
                )
    _nav_buttons(builder)
    return "Model", "\n".join(lines), builder.build()


async def _ai_media_tts_model_inline_builder(event, extra: str) -> list:
    result = await _ai_media_tts_model_panel_handler(event, extra)
    if result is None:
        return [render("Model", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


# ── Voice selection (a nested panel: only this model's voices) ───────────────


def _voice_button(voice) -> tuple[str, str]:
    """``(text, callback)`` for ONE voice's selection button.

    The callback payload is the registered voice id and is NEVER derived from the
    button text, so shortening a label can never change which voice is selected.
    """
    persian = " · Persian verified" if voice.persian == "verified" else ""
    return f"Use {voice.label}{persian}", f"action:ai_tts_select_voice:{voice.voice_id}"


async def _ai_media_tts_voice_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    from backend.bot.handlers.ai import _nav_buttons

    _owner, config = await owner_and_config()
    selection = _selection_of(config)
    model = selection.model_entry
    lines = [
        "**Voice**", "",
        f"Provider · {selection.provider_label}",
        f"Model · {selection.model_label}",
        "",
        f"Voice · {selection.voice_label}",
        f"Persian · {selection.persian_label()}",
    ]
    builder = InlinePanelBuilder()
    if model is None:  # pragma: no cover - the selection is always registered
        lines.append("! No such model.")
    else:
        selectable = [
            _voice_button(voice) for voice in model.voices
            if voice.voice_id != selection.voice
        ]
        lines.append("")
        lines.append("_Voice is a provider capability; Persian is never claimed unless verified._")
        _add_rows(builder, _two_column_rows(selectable))
    _nav_buttons(builder)
    return "Voice", "\n".join(lines), builder.build()


async def _ai_media_tts_voice_inline_builder(event, extra: str) -> list:
    result = await _ai_media_tts_voice_panel_handler(event, extra)
    if result is None:
        return [render("Voice", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


# ── Selection actions (an explicit finite choice, never typed input) ─────────


def _outcome(saved: bool, success: str) -> str:
    """The notice for ONE selection write — honest about durable persistence.

    ``persist_selection`` reports whether the durable row was actually written.
    Reporting ``success`` unconditionally is what let a surface announce
    "Text-to-Speech now uses Speechmatics" while every re-read of the store still
    said OpenAI, so an unwritten selection is stated as exactly what it is: active
    for this process, not saved.
    """
    if saved:
        return success
    return (
        f"{success}\n⚠ Saved only for this session — the settings row was not "
        "updated, so the choice is lost on restart."
    )


async def _ai_tts_select_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Make a REGISTERED provider the active one, with ITS default model/voice.

    The payload is a provider token from the registry the panel rendered. The
    model and the voice are resolved from that provider IN THE SAME WRITE, so a
    provider change can never leave the previous provider's model or voice
    persisted. An unknown or unimplemented provider changes nothing and says so.
    """
    entry = get_provider(extra)
    if entry is None:
        return await _tts_panel_with_notice("× Unknown speech provider — nothing changed.")
    if not entry.implemented:
        return await _tts_panel_with_notice(
            f"× {entry.label} is registered but not available on this runtime yet."
        )

    owner, _config = await owner_and_config()
    model = entry.model(entry.default_model_id)
    candidate = resolve(
        entry.provider,
        entry.default_model_id,
        model.default_voice_id if model is not None else "",
    )
    saved = await persist_selection(owner, candidate)
    await apply_tts_settings_now(owner)
    return await _tts_panel_with_notice(_outcome(
        saved,
        f"✓ Text-to-Speech now uses {entry.label} · {candidate.model_label} · "
        f"{candidate.voice_label}",
    ))


async def _ai_tts_select_model_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Select a model OF THE CURRENT PROVIDER, with that model's default voice."""
    owner, config = await owner_and_config()
    selection = _selection_of(config)
    entry = get_provider(selection.provider)
    model = entry.model(extra) if entry is not None else None
    if model is None:
        return await _tts_panel_with_notice("× Unknown model for this provider — nothing changed.")
    if not model.implemented:
        return await _tts_panel_with_notice(
            f"× {model.label} is registered but not available on this runtime yet."
        )
    candidate = resolve(selection.provider, model.model_id, model.default_voice_id)
    saved = await persist_selection(owner, candidate)
    await apply_tts_settings_now(owner)
    return await _tts_panel_with_notice(_outcome(
        saved, f"✓ Text-to-Speech now uses {entry.label} · {model.label} · {candidate.voice}",
    ))


async def _ai_tts_select_voice_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Select a voice OF THE CURRENT MODEL (provider and model stay unchanged)."""
    owner, config = await owner_and_config()
    selection = _selection_of(config)
    model = selection.model_entry
    voice = model.voice(extra) if model is not None else None
    if voice is None:
        return await _tts_panel_with_notice("× Unknown voice for this model — nothing changed.")
    candidate = resolve(selection.provider, selection.model, voice.voice_id)
    saved = await persist_selection(owner, candidate)
    await apply_tts_settings_now(owner)
    return await _tts_panel_with_notice(_outcome(
        saved, f"✓ Voice set to {voice.label} · Persian {candidate.persian_label()}",
    ))


def register(client=None, owner_id: int = 0) -> None:
    """Attach the Media Analysis TTS panels and their controls to the ONE registry.

    Same scope and mechanism as every other AI panel (``panel:*`` / ``action:*``)
    — no parallel UI framework, no second registry, no second store. Labels never
    name an environment variable.
    """
    try:
        register_panel("ai_media_tts", _ai_media_tts_panel_handler, parent="ai_media", title="Text-to-Speech")
        register_inline_builder("ai_media_tts", _ai_media_tts_inline_builder)
        register_panel(
            "ai_media_tts_model", _ai_media_tts_model_panel_handler,
            parent="ai_media_tts", title="Model",
        )
        register_inline_builder("ai_media_tts_model", _ai_media_tts_model_inline_builder)
        register_panel(
            "ai_media_tts_voice", _ai_media_tts_voice_panel_handler,
            parent="ai_media_tts", title="Voice",
        )
        register_inline_builder("ai_media_tts_voice", _ai_media_tts_voice_inline_builder)
        register_action("ai_tts_select", _ai_tts_select_action)
        register_action("ai_tts_select_model", _ai_tts_select_model_action)
        register_action("ai_tts_select_voice", _ai_tts_select_voice_action)
    except Exception as exc:  # noqa: BLE001 — registration is never fatal
        logger.error("Text-to-Speech registration FAILED: %s", exc)
