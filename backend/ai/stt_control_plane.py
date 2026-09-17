"""Speech-to-Text control plane — the capability-specific STT configuration.

This is the CONFIGURATION half of the STT subsystem, deliberately separate from
its EXECUTION half:

    Telegram UI (AI -> Media Analysis -> Speech-to-Text)
        ↓
    this module: registered candidates + the owner's persisted selection
        ↓
    (a later phase: the provider/model test + fallback manager)
        ↓
    the EXISTING ``media_service.set_stt_engine`` seam

Nothing here touches ``media_service``, a provider adapter, Telegram, the
database or the network. The module is STATELESS: it describes what CAN be
selected and it reads/writes plain values through the owner's existing
``ai_config`` row (``backend/ai/config_store.py``) — no second store, no new
table, no new column.

Two concepts are kept apart on purpose:

* **Capability** — a registered candidate (:class:`SttCandidate`) is a
  provider + model pair this project KNOWS how to run. The registry is the
  single reason the owner never types a model identifier again: the UI offers
  registered candidates, and an unregistered value can never become a
  candidate.
* **Credential** — where the provider's API key lives (ENV) is NOT part of a
  candidate and is NOT part of its status here. A key existing is not evidence
  that a candidate works; only a real test can say that, and the test manager
  belongs to a later phase.

Storage contract (the smallest backward-compatible migration):

The owner has exactly one existing text column that used to hold a free-form
model identifier: ``ai_config.stt_model``. Its meaning changes from "an opaque
model string the owner typed" to "the REGISTERED candidate the owner selected",
which keeps the schema unchanged:

    stored value        meaning
    ------------        -------
    (empty)             the default candidate (``gemini:default``)
    a registered id     that candidate is the active one
    anything else       LEGACY/UNRESOLVED — see below

A stored value that is not a registered candidate is NEVER silently re-pointed
at a registered one (that would silently change which model transcribes the
owner's voice messages). It is reported as legacy/unresolved and, for
execution, passed through to the existing engine VERBATIM — exactly the
behavior this value had before this phase, so a pre-existing configuration
keeps working unchanged until the owner picks a registered candidate.
"""
from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import Any, Mapping

from backend.services.gemini_media_engine import (
    DEDICATED_TRANSCRIPTION_MODEL,
    STT_LANGUAGE_AUTO,
    STT_MAX_PASSES,
)

logger = logging.getLogger(__name__)

#: The candidate that is active when the owner has configured nothing. It maps
#: to an EMPTY stored value and to an empty engine model, i.e. exactly the
#: pre-existing "the general media model answers the speech instruction" route.
DEFAULT_CANDIDATE_ID = "gemini:default"

#: The documented pass count when the owner has configured nothing: one pass,
#: i.e. the pre-existing single-pass route.
STT_DEFAULT_PASSES = 1

#: ``provider:model`` — the candidate identity. A registered id is the ONLY
#: thing the owner can select; the identity always names both halves so a
#: future test/fallback manager can address a candidate without guessing.
CANDIDATE_SEPARATOR = ":"

#: The three ``ai_config`` keys this capability owns. The same keys the
#: previous surface used, so no schema change and no data migration.
STORAGE_KEY_ACTIVE = "stt_model"
STORAGE_KEY_LANGUAGE = "stt_language"
STORAGE_KEY_PASSES = "stt_passes"

STORAGE_KEYS: tuple[str, str, str] = (
    STORAGE_KEY_ACTIVE,
    STORAGE_KEY_LANGUAGE,
    STORAGE_KEY_PASSES,
)


@dataclass(frozen=True)
class SttCandidate:
    """A registered transcription engine: a provider + model pair.

    ``implemented`` distinguishes "this project knows the capability exists" from
    "this project can actually run it today". A registered-but-unimplemented
    candidate is data for the later test/fallback phase; it can never be
    selected as active, so the UI can never promise an execution that does not
    exist.
    """

    candidate_id: str
    provider: str
    model: str
    label: str
    implemented: bool
    note: str = ""

    @property
    def is_provider_default(self) -> bool:
        """True for the route that uses the provider's own general media model."""
        return not self.model

    def status_word(self) -> str:
        return "Ready" if self.implemented else "Not available yet"


def _build_candidates() -> tuple[SttCandidate, ...]:
    """The registry, in CANONICAL order (the deterministic fallback order).

    Deterministic by construction: a tuple literal, never a set or a dict scan,
    so every consumer sees the same sequence on every process.
    """
    return (
        SttCandidate(
            candidate_id=DEFAULT_CANDIDATE_ID,
            provider="gemini",
            model="",
            label="Gemini · media model",
            implemented=True,
            note="the existing Gemini media route",
        ),
        SttCandidate(
            candidate_id=f"gemini:{DEDICATED_TRANSCRIPTION_MODEL}",
            provider="gemini",
            model=DEDICATED_TRANSCRIPTION_MODEL,
            label="Gemini Transcribe",
            implemented=True,
            note="the dedicated Gemini transcription route",
        ),
        SttCandidate(
            candidate_id="groq:whisper-large-v3",
            provider="groq",
            model="whisper-large-v3",
            label="Groq Whisper Large-v3",
            implemented=False,
            note="capability registered — adapter not implemented yet",
        ),
        SttCandidate(
            candidate_id="groq:whisper-large-v3-turbo",
            provider="groq",
            model="whisper-large-v3-turbo",
            label="Groq Whisper Large-v3 Turbo",
            implemented=False,
            note="capability registered — adapter not implemented yet",
        ),
        SttCandidate(
            candidate_id="speechmatics:standard",
            provider="speechmatics",
            model="standard",
            label="Speechmatics",
            implemented=False,
            note="capability registered — adapter not implemented yet",
        ),
    )


#: Every registered candidate, in canonical (fallback) order.
STT_CANDIDATES: tuple[SttCandidate, ...] = _build_candidates()

_BY_ID: dict[str, SttCandidate] = {c.candidate_id: c for c in STT_CANDIDATES}


def all_candidates() -> tuple[SttCandidate, ...]:
    """The registry in canonical order — the deterministic pool."""
    return STT_CANDIDATES


def get_candidate(candidate_id: str) -> SttCandidate | None:
    """The registered candidate for an id, or ``None`` when unregistered."""
    return _BY_ID.get(str(candidate_id or "").strip())


def candidate_ids() -> tuple[str, ...]:
    return tuple(c.candidate_id for c in STT_CANDIDATES)


def default_candidate() -> SttCandidate:
    return _BY_ID[DEFAULT_CANDIDATE_ID]


def is_selectable(candidate_id: str) -> bool:
    """True when the candidate may become the ACTIVE one (implemented only)."""
    candidate = get_candidate(candidate_id)
    return bool(candidate and candidate.implemented)


def storage_value(candidate_id: str) -> str:
    """The ``ai_config.stt_model`` value for a REGISTERED candidate.

    The default candidate is stored as the empty string, so "nothing
    configured" and "the default" stay one single state. An unregistered id
    raises: silently storing it would create a candidate the registry does not
    know, which is exactly the manual-model-entry behavior this phase removes.
    """
    candidate = get_candidate(candidate_id)
    if candidate is None:
        raise ValueError(f"unregistered STT candidate: {candidate_id!r}")
    return "" if candidate.candidate_id == DEFAULT_CANDIDATE_ID else candidate.candidate_id


def fallback_ids(active_id: str) -> tuple[str, ...]:
    """The registered candidates after ``active_id``, in canonical order."""
    if not active_id:
        return ()
    return tuple(c.candidate_id for c in STT_CANDIDATES if c.candidate_id != active_id)


@dataclass(frozen=True)
class SttControlPlane:
    """The owner's speech-to-text configuration, resolved.

    ``active_id`` is the effective active candidate (or ``""`` for an
    unresolved legacy value), ``fallback_ids`` the deterministic tail of the
    pool, and ``legacy_model`` the raw stored value that matched no registered
    candidate. ``language`` and ``passes`` are the bounded behavioral settings.
    """

    active_id: str
    fallback_ids: tuple[str, ...]
    language: str
    passes: int
    legacy_model: str = ""

    @property
    def is_legacy(self) -> bool:
        """True when the stored value is not a registered candidate."""
        return bool(self.legacy_model)

    @property
    def active_candidate(self) -> SttCandidate | None:
        return get_candidate(self.active_id) if self.active_id else None

    @property
    def active_unavailable(self) -> bool:
        """True when the selected candidate exists but cannot run on this build.

        A registered-but-unimplemented candidate is valid DATA for the later
        test/fallback phase and is never executed: the boundary keeps its
        fail-closed default route, and the panel says so instead of pretending
        the selection works.
        """
        candidate = self.active_candidate
        return bool(candidate and not candidate.implemented)

    @property
    def ordered_candidate_ids(self) -> tuple[str, ...]:
        """The ordered active-first candidate list (empty when unresolved)."""
        if self.is_legacy:
            return ()
        return (self.active_id,) + self.fallback_ids

    @property
    def ordered_candidates(self) -> tuple[SttCandidate, ...]:
        return tuple(
            candidate
            for candidate in (get_candidate(cid) for cid in self.ordered_candidate_ids)
            if candidate is not None
        )

    def candidate_role(self, candidate_id: str) -> str:
        """``active`` / ``fallback`` / ``candidate`` for one registered id."""
        if candidate_id == self.active_id and not self.is_legacy:
            return "active"
        if candidate_id in self.fallback_ids:
            return "fallback"
        return "candidate"

    def engine_model(self) -> str:
        """The model value the EXISTING STT seam understands.

        A legacy value is returned VERBATIM (never substituted, so a legacy
        configuration keeps transcribing with the same model it used before); a
        non-Gemini provider has no execution path yet and maps to the empty
        value, the boundary's already fail-closed "no dedicated STT model" state.
        """
        if self.legacy_model:
            return self.legacy_model
        candidate = self.active_candidate
        if candidate is None or candidate.provider != "gemini":
            return ""
        return candidate.model

    def engine_settings(self) -> dict[str, Any]:
        """The three values for ``apply_stt_settings`` — plain data, no context."""
        return {
            STORAGE_KEY_ACTIVE: self.engine_model(),
            STORAGE_KEY_LANGUAGE: self.language,
            STORAGE_KEY_PASSES: self.passes,
        }


def parse_stt_config(config: Mapping[str, Any] | None) -> SttControlPlane:
    """Resolve the owner's persisted keys into an :class:`SttControlPlane`.

    Pure: reads only the three keys, never ENV, the database or Telegram. An
    unregistered stored value is reported as legacy/unresolved — never mapped
    onto a different registered candidate — and the pass count is held inside
    ``1..STT_MAX_PASSES`` as the engine's own invariant.
    """
    config = config or {}
    raw = str(config.get(STORAGE_KEY_ACTIVE) or "").strip()
    legacy_model = ""
    if not raw:
        active_id = DEFAULT_CANDIDATE_ID
    elif get_candidate(raw) is not None:
        active_id = raw
    else:
        active_id, legacy_model = "", raw

    language = str(config.get(STORAGE_KEY_LANGUAGE) or "").strip()
    if language.lower() == STT_LANGUAGE_AUTO:
        language = ""

    try:
        passes = int(config.get(STORAGE_KEY_PASSES))
    except (TypeError, ValueError):
        passes = STT_DEFAULT_PASSES
    passes = max(1, min(passes, STT_MAX_PASSES))

    return SttControlPlane(
        active_id=active_id,
        fallback_ids=fallback_ids(active_id),
        language=language,
        passes=passes,
        legacy_model=legacy_model,
    )


def engine_settings(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """``apply_stt_settings``-shaped values for a persisted config mapping.

    The ONE control-plane → execution conversion. Callers (the runtime
    supervisor at startup, the Telegram handlers after a save) pass the owner's
    stored mapping in and never hand a Telegram object, owner id, chat id or
    message id to the engine.
    """
    return parse_stt_config(config).engine_settings()
