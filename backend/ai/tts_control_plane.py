"""Text-to-Speech control plane — the capability registry and the owner's persisted
selection.

This is the CONFIGURATION half of the speech-synthesis subsystem, deliberately
separate from its EXECUTION half (``backend/services/tts_service.py``):

    Telegram UI (AI -> Media Analysis -> Text-to-Speech)
        ↓
    this module: the registered providers/models/voices + the owner's selection
        ↓
    ``backend/services/tts_engine_factory``: the ONE provider → engine seam
        ↓
    the provider adapters (``openai_tts_engine``, ``speechmatics_tts_engine``)
        ↓
    the EXISTING TTS boundary (``backend/services/tts_service.py``)

Nothing here talks to a provider, reads a credential, touches Telegram, the
database or the network. The module is STATELESS: it describes what CAN be
selected and it resolves plain values that the callers persist on the owner's
existing ``ai_config`` row (``backend/ai/config_store.py``) — no second store, no
new table.

Two concepts are kept apart on purpose, exactly as the Speech-to-Text control
plane keeps them apart:

* **Capability** — a registered provider/model/voice triple this project KNOWS.
  The registry is the single reason the owner never types a model or a voice
  identifier: the UI offers registered values, and an unregistered value can
  never become a selection. ``implemented`` distinguishes "this project knows the
  capability exists" from "this project can run it today"; a registered-but-
  unimplemented provider is DATA (a documented deferral) and can never be
  selected as active.
* **Credential** — where the provider's API key lives is NOT part of the
  registry and is NOT part of its state. A key existing is not evidence that a
  provider answers; only a real request can say that.

PERSIAN IS NEVER OVERCLAIMED. Each voice carries an explicit, deterministic
capability state:

    ``not_verified``  — the provider publishes no per-language guarantee for this
                        voice; only a live request can say (the honest default)
    ``unsupported``   — the provider's OWN documentation states the capability is
                        not offered (recorded with the evidence beside the entry)
    ``verified``      — a recorded, reproducible live verification exists

No entry claims ``verified`` without that evidence, and the renderer states the
state rather than a claim, so "Persian supported" is never printed for a
capability that was not verified.

INVALID STORED COMBINATIONS NEVER SURVIVE. A persisted provider/model/voice
triple is resolved level by level: an unregistered provider degrades to the
default provider, a model that does not belong to the resolved provider degrades
to that provider's default model, and a voice that does not belong to the
resolved model degrades to that model's default voice. The degradation is
deterministic, is reported (``TtsSelection.adjusted``), and the Telegram writes
always persist a CONSISTENT triple — so changing the provider cannot leave a
stale, incompatible model or voice behind in the store.
"""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any, Mapping

# ── The default selection (what an unconfigured owner gets) ──────────────────

DEFAULT_PROVIDER_ID = "openai"
DEFAULT_MODEL_ID = "gpt-4o-mini-tts"
DEFAULT_VOICE_ID = "alloy"

#: The three ``ai_config`` keys this capability owns. Empty string = "the
#: default", so "nothing configured" and "the default selection" stay one state.
STORAGE_KEY_PROVIDER = "tts_provider"
STORAGE_KEY_MODEL = "tts_model"
STORAGE_KEY_VOICE = "tts_voice"

STORAGE_KEYS: tuple[str, str, str] = (
    STORAGE_KEY_PROVIDER,
    STORAGE_KEY_MODEL,
    STORAGE_KEY_VOICE,
)

# ── The Persian capability states (closed) ───────────────────────────────────

#: A recorded, reproducible live verification exists for this voice.
PERSIAN_VERIFIED = "verified"
#: The provider's own documentation states the capability is not offered.
PERSIAN_UNSUPPORTED = "unsupported"
#: No per-language guarantee is published; only a live request can say.
PERSIAN_NOT_VERIFIED = "not_verified"

PERSIAN_STATES = frozenset({PERSIAN_VERIFIED, PERSIAN_UNSUPPORTED, PERSIAN_NOT_VERIFIED})

#: Owner-facing wording. A capability is never described as "supported" unless it
#: was actually verified — the other two states say what is true.
PERSIAN_LABELS: dict[str, str] = {
    PERSIAN_VERIFIED: "verified",
    PERSIAN_UNSUPPORTED: "not supported",
    PERSIAN_NOT_VERIFIED: "not verified — live request required",
}


def persian_label(state: str) -> str:
    """Owner-facing wording for one capability state (never a claim of support)."""
    return PERSIAN_LABELS.get(str(state or ""), PERSIAN_LABELS[PERSIAN_NOT_VERIFIED])


# ── The registry ─────────────────────────────────────────────────────────────


@dataclass(frozen=True)
class TtsVoice:
    """ONE voice a registered model offers.

    ``persian`` is the explicit capability state above; ``note`` carries the
    bounded evidence for it (never a claim).
    """

    voice_id: str
    label: str
    persian: str = PERSIAN_NOT_VERIFIED
    note: str = ""


@dataclass(frozen=True)
class TtsModel:
    """ONE model a registered provider offers, with the voices it accepts.

    ``model_id`` may be empty, which means "the provider's own default route"
    (the provider has no model parameter) — that is an explicit fact about the
    provider, never an invented model name.

    ``output_format``/``mime_type`` are the provider's documented output shape, so
    the panel can state it and a delivery incompatibility (a container Telegram
    voice notes cannot carry) is visible as a capability fact rather than a
    surprise at delivery time.
    """

    model_id: str
    label: str
    voices: tuple[TtsVoice, ...]
    implemented: bool = True
    note: str = ""
    output_format: str = ""
    mime_type: str = ""

    @property
    def is_provider_default(self) -> bool:
        return not self.model_id

    @property
    def default_voice_id(self) -> str:
        return self.voices[0].voice_id if self.voices else ""

    def voice(self, voice_id: str) -> TtsVoice | None:
        return next((v for v in self.voices if v.voice_id == voice_id), None)

    def voice_ids(self) -> tuple[str, ...]:
        return tuple(v.voice_id for v in self.voices)


@dataclass(frozen=True)
class TtsProvider:
    """ONE speech-synthesis provider this project registers.

    ``implemented`` is the deferral flag: a registered-but-unimplemented provider
    is offered as information and can NEVER become the active selection, so the
    UI never promises an execution that does not exist.
    """

    provider: str
    label: str
    models: tuple[TtsModel, ...]
    implemented: bool = True
    note: str = ""

    @property
    def default_model_id(self) -> str:
        return self.models[0].model_id if self.models else ""

    def model(self, model_id: str) -> TtsModel | None:
        return next((m for m in self.models if m.model_id == model_id), None)

    def model_ids(self) -> tuple[str, ...]:
        return tuple(m.model_id for m in self.models)

    def status_word(self) -> str:
        return "Ready" if self.implemented else "Not available yet"


def _openai_output_format() -> str:
    """The OpenAI adapter's OWN requested output format (one declaration)."""
    from backend.services import openai_tts_engine

    return openai_tts_engine.RESPONSE_FORMAT


def _openai_mime_type() -> str:
    """The MIME type the OpenAI adapter's output is delivered as."""
    from backend.services import openai_tts_engine

    return openai_tts_engine.AUDIO_MIME


def _openai_voices() -> tuple[TtsVoice, ...]:
    """The voices the speech endpoint documents, in its own documented order.

    The allowlist itself is the adapter's (``openai_tts_engine.SUPPORTED_VOICES``)
    so the two can never drift; the ORDER and the Persian state live here, and a
    voice the adapter does not accept is refused at import time rather than
    becoming an offer the adapter would reject.
    """
    from backend.services import openai_tts_engine

    order = tuple(openai_tts_engine.VOICE_ORDER)
    supported = openai_tts_engine.SUPPORTED_VOICES
    if set(order) != set(supported):
        raise RuntimeError(
            "openai_tts_engine.VOICE_ORDER must cover exactly SUPPORTED_VOICES"
        )
    #: No per-voice Persian guarantee is published by the provider, so every
    #: entry states exactly that. It is NEVER presented as support.
    return tuple(
        TtsVoice(
            voice_id=voice_id,
            label=voice_id.title(),
            persian=PERSIAN_NOT_VERIFIED,
            note="provider publishes no per-language guarantee for this voice",
        )
        for voice_id in order
    )


def _speechmatics_voices() -> tuple[TtsVoice, ...]:
    """The four voices Speechmatics' own TTS documentation lists.

    Recorded (with their accents) so the deferral is explicit and inspectable
    rather than a missing entry.
    """
    return (
        TtsVoice("sarah", "Sarah · English (UK)", PERSIAN_UNSUPPORTED, "documented as English (UK)"),
        TtsVoice("theo", "Theo · English (UK)", PERSIAN_UNSUPPORTED, "documented as English (UK)"),
        TtsVoice("megan", "Megan · English (US)", PERSIAN_UNSUPPORTED, "documented as English (US)"),
        TtsVoice("jack", "Jack · English (US)", PERSIAN_UNSUPPORTED, "documented as English (US)"),
    )


def _build_providers() -> tuple[TtsProvider, ...]:
    """The registry, in CANONICAL order (the deterministic fallback order).

    Deterministic by construction: a tuple literal, never a set or a dict scan,
    so every consumer sees the same sequence on every process. The active
    provider is always tried FIRST; this order is the tail.
    """
    default_model = DEFAULT_MODEL_ID
    return (
        TtsProvider(
            provider=DEFAULT_PROVIDER_ID,
            label="OpenAI",
            models=(
                TtsModel(
                    model_id=default_model,
                    label="gpt-4o-mini-tts",
                    voices=_openai_voices(),
                    implemented=True,
                    note="the documented speech model of the existing OpenAI adapter",
                    output_format=_openai_output_format(),
                    mime_type=_openai_mime_type(),
                ),
            ),
            implemented=True,
            note="the existing OpenAI speech adapter",
        ),
        TtsProvider(
            provider="speechmatics",
            label="Speechmatics",
            models=(
                TtsModel(
                    model_id="",
                    label="Speechmatics TTS (preview)",
                    voices=_speechmatics_voices(),
                    implemented=False,
                    note="the provider exposes no model parameter — this is its own default route",
                    output_format="wav_16000",
                    mime_type="audio/wav",
                ),
            ),
            implemented=False,
            note=(
                "DEFERRED: documented output is WAV/PCM only, which Telegram voice "
                "notes cannot carry (this project has no audio-transcoding path by "
                "policy), and its documented voices are English-only"
            ),
        ),
    )


#: Every registered provider, in canonical (fallback) order.
TTS_PROVIDERS: tuple[TtsProvider, ...] = _build_providers()

_BY_PROVIDER: dict[str, TtsProvider] = {p.provider: p for p in TTS_PROVIDERS}


def all_providers() -> tuple[TtsProvider, ...]:
    """The registry in canonical order — the deterministic pool."""
    return TTS_PROVIDERS


def provider_ids() -> tuple[str, ...]:
    return tuple(p.provider for p in TTS_PROVIDERS)


def implemented_provider_ids() -> tuple[str, ...]:
    """The providers this build can actually execute, in canonical order."""
    return tuple(p.provider for p in TTS_PROVIDERS if p.implemented)


def get_provider(provider: str) -> TtsProvider | None:
    return _BY_PROVIDER.get(str(provider or "").strip())


def get_model(provider: str, model: str) -> TtsModel | None:
    entry = get_provider(provider)
    return entry.model(str(model or "")) if entry is not None else None


def get_voice(provider: str, model: str, voice: str) -> TtsVoice | None:
    model_entry = get_model(provider, model)
    return model_entry.voice(str(voice or "")) if model_entry is not None else None


def model_ids(provider: str) -> tuple[str, ...]:
    entry = get_provider(provider)
    return entry.model_ids() if entry is not None else ()


def voice_ids(provider: str, model: str) -> tuple[str, ...]:
    model_entry = get_model(provider, model)
    return model_entry.voice_ids() if model_entry is not None else ()


def is_selectable(provider: str, model: str, voice: str) -> bool:
    """True when this exact triple may become the ACTIVE selection.

    Fail-closed on every leg: the provider must be registered AND implemented,
    the model must belong to that provider, and the voice must belong to that
    model. An unregistered or unimplemented value is therefore never selectable.
    """
    entry = get_provider(provider)
    if entry is None or not entry.implemented:
        return False
    model_entry = entry.model(model)
    if model_entry is None or not model_entry.implemented:
        return False
    return model_entry.voice(voice) is not None


def canonical_order(active_provider: str) -> tuple[str, ...]:
    """The implemented providers with ``active_provider`` FIRST.

    The deterministic tail the fallback layer walks: the owner's selection never
    loses priority, and the substitutes are the registered implementations in
    their canonical order — never a random or discovered set.
    """
    active = str(active_provider or "").strip()
    ordered = [active] if active in implemented_provider_ids() else []
    for provider in implemented_provider_ids():
        if provider not in ordered:
            ordered.append(provider)
    return tuple(ordered)


# ── The resolved selection ───────────────────────────────────────────────────


@dataclass(frozen=True)
class TtsSelection:
    """The owner's speech-synthesis selection, resolved.

    Always a VALID triple: ``provider``/``model``/``voice`` name registered,
    implemented values, and ``adjusted`` is non-empty exactly when an unusable
    stored value was degraded to a valid one (so the panel can say what happened
    instead of silently changing the selection).
    """

    provider: str
    model: str
    voice: str
    adjusted: str = ""

    @property
    def provider_entry(self) -> TtsProvider | None:
        return get_provider(self.provider)

    @property
    def model_entry(self) -> TtsModel | None:
        return get_model(self.provider, self.model)

    @property
    def voice_entry(self) -> TtsVoice | None:
        return get_voice(self.provider, self.model, self.voice)

    @property
    def provider_label(self) -> str:
        entry = self.provider_entry
        return entry.label if entry is not None else self.provider

    @property
    def model_label(self) -> str:
        entry = self.model_entry
        if entry is None:
            return self.model
        return entry.label

    @property
    def voice_label(self) -> str:
        entry = self.voice_entry
        return entry.label if entry is not None else self.voice

    @property
    def persian(self) -> str:
        entry = self.voice_entry
        return entry.persian if entry is not None else PERSIAN_NOT_VERIFIED

    def persian_label(self) -> str:
        return persian_label(self.persian)

    @property
    def is_valid(self) -> bool:
        return is_selectable(self.provider, self.model, self.voice)

    def storage_values(self) -> dict[str, str]:
        """The three ``ai_config`` values for this selection (defaults → empty)."""
        return {
            STORAGE_KEY_PROVIDER: "" if self.provider == DEFAULT_PROVIDER_ID else self.provider,
            STORAGE_KEY_MODEL: "" if self.model == DEFAULT_MODEL_ID else self.model,
            STORAGE_KEY_VOICE: "" if self.voice == DEFAULT_VOICE_ID else self.voice,
        }


def default_selection() -> TtsSelection:
    """The selection an unconfigured owner gets (always a valid triple)."""
    return TtsSelection(
        provider=DEFAULT_PROVIDER_ID, model=DEFAULT_MODEL_ID, voice=DEFAULT_VOICE_ID,
    )


def _resolve_provider(stored: str) -> tuple[str, str]:
    """``(provider, adjusted_reason)`` for a stored provider value."""
    token = str(stored or "").strip()
    if not token:
        return DEFAULT_PROVIDER_ID, ""
    entry = get_provider(token)
    if entry is None:
        return DEFAULT_PROVIDER_ID, f"unknown provider `{token}`"
    if not entry.implemented:
        return DEFAULT_PROVIDER_ID, f"provider `{token}` is not available on this runtime"
    return token, ""


def resolve(provider: str, model: str, voice: str) -> TtsSelection:
    """Resolve a stored triple into a VALID :class:`TtsSelection`.

    Deterministic degradation, one level at a time: provider → its default model
    → that model's default voice. Every degradation is recorded, so a caller can
    report it; nothing here ever invents a value outside the registry.
    """
    reasons: list[str] = []
    provider_id, reason = _resolve_provider(provider)
    if reason:
        reasons.append(reason)
    provider_entry = get_provider(provider_id)
    assert provider_entry is not None  # the default provider is always registered

    model_token = str(model or "").strip()
    model_entry = provider_entry.model(model_token)
    if model_entry is None:
        if model_token:
            reasons.append(f"model `{model_token}` is not offered by {provider_entry.label}")
        model_entry = provider_entry.model(provider_entry.default_model_id)
    if model_entry is None:  # pragma: no cover - registry guarantees a default
        raise RuntimeError(f"{provider_id} registers no model")

    voice_token = str(voice or "").strip()
    voice_entry = model_entry.voice(voice_token)
    if voice_entry is None:
        if voice_token:
            reasons.append(f"voice `{voice_token}` is not offered by {model_entry.label}")
        voice_entry = model_entry.voice(model_entry.default_voice_id)
    if voice_entry is None:  # pragma: no cover - registry guarantees a default
        raise RuntimeError(f"{model_entry.label} registers no voice")

    return TtsSelection(
        provider=provider_id,
        model=model_entry.model_id,
        voice=voice_entry.voice_id,
        adjusted="; ".join(reasons),
    )


def parse_tts_config(config: Mapping[str, Any] | None) -> TtsSelection:
    """Resolve the owner's persisted keys into a :class:`TtsSelection`.

    Pure: reads only the three keys, never ENV, the database or Telegram.
    """
    config = config or {}
    return resolve(
        str(config.get(STORAGE_KEY_PROVIDER) or ""),
        str(config.get(STORAGE_KEY_MODEL) or ""),
        str(config.get(STORAGE_KEY_VOICE) or ""),
    )


def storage_values(selection: TtsSelection) -> dict[str, str]:
    """``ai_config``-shaped values for a selection (defaults stored as empty)."""
    return selection.storage_values()
