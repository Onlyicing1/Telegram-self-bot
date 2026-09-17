"""STT candidate → engine resolution — the minimal execution seam.

The control plane (``backend/ai/stt_control_plane.py``) says WHAT is selected;
this module turns that selection into the engine the EXISTING media boundary
already accepts (``media_service.set_stt_engine``). It is the ONE place a
provider-specific engine is constructed, which keeps three contracts intact:

  * the media boundary stays provider-independent (it owns only the seam),
  * the control plane stays configuration-only (no execution imports),
  * no Telegram object, owner id, chat id, message id, caption or conversation
    state can reach an engine — the only inputs here are the persisted selection
    and the two plain behavioral settings.

Routing is deterministic and fail-closed:

  * a REGISTERED candidate whose provider has an execution path on this build
    (``gemini``, ``groq``, ``speechmatics``) is built for ITS OWN provider and
    model — a candidate is never substituted by a different provider's model;
  * a candidate with no execution path yet, or a provider with no credential, is
    reported honestly and the boundary is left with NO STT engine, so the media
    path keeps its documented fail-closed behavior instead of quietly transcribing
    with something else;
  * a LEGACY stored value (a model id that matches no registered candidate) keeps
    the pre-existing behavior: it goes to the Gemini route VERBATIM, exactly as it
    did before the control plane existed.

Provider adapters and runtime fallback are NOT this module's concern: it resolves
the ONE selected candidate. The later active → fallback → cooldown manager must
consult this seam rather than duplicate it.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from backend.ai.stt_control_plane import SttCandidate, parse_stt_config
from backend.services import groq_stt_engine, media_service, speechmatics_stt_engine

logger = logging.getLogger(__name__)

#: The bounded reason a candidate cannot run on this build. Reported to the
#: caller (and to the provider-test seam) so an unavailable candidate is never
#: confused with a tested-and-failed one.
REASON_NOT_IMPLEMENTED = "not_implemented"

#: The bounded reason a provider has no usable credential. Deliberately the SAME
#: token the provider adapters' own classifications use, so "the credential is
#: missing" reads identically whichever leg discovered it.
REASON_MISSING_CREDENTIAL = groq_stt_engine.FAILURE_MISSING_CREDENTIAL

#: The providers whose selection is provisioned by their OWN adapter rather than
#: by the Gemini media route. Anything else (the Gemini candidates, a
#: registered-but-unimplemented capability, a legacy value) stays on the existing
#: Gemini leg.
_ADAPTER_PROVIDERS = frozenset({
    groq_stt_engine.PROVIDER_NAME,
    speechmatics_stt_engine.PROVIDER_NAME,
})


def build_engine(
    candidate: SttCandidate | None, *, language: str = "", passes: int = 1,
) -> tuple[Any | None, str]:
    """``(engine, reason)`` for ONE registered candidate.

    ``engine`` is ``None`` when this build cannot run the candidate and ``reason``
    is then a bounded token (``not_implemented`` / ``missing_credential`` /
    ``unsupported_model``). No engine is ever built for an unregistered, an
    unimplemented or a differently-named candidate, and a missing credential
    never causes a substitute provider to be selected.
    """
    if candidate is None or not candidate.implemented:
        return None, REASON_NOT_IMPLEMENTED
    if candidate.provider == groq_stt_engine.PROVIDER_NAME:
        return groq_stt_engine.build_engine(
            candidate.model, language=language, passes=passes,
        )
    if candidate.provider == speechmatics_stt_engine.PROVIDER_NAME:
        return speechmatics_stt_engine.build_engine(
            candidate.model, language=language, passes=passes,
        )
    if candidate.provider == "gemini":
        from backend.services import gemini_media_engine as gemini

        api_key, key_env_var = gemini.resolve_api_key()
        if not api_key:
            return None, REASON_MISSING_CREDENTIAL
        model, _model_env_var = gemini.resolve_media_model()
        return (
            gemini.GeminiMediaEngine(
                api_key,
                model,
                key_env_var=key_env_var,
                stt_model=candidate.model,
                stt_language=language,
                stt_passes=passes,
            ),
            "",
        )
    return None, REASON_NOT_IMPLEMENTED


def apply_stt_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Install the owner's persisted STT selection onto the live STT engine.

    The ONE entry point called by the places that own this state: the runtime
    supervisor at startup (so the persisted values are in effect from the first
    transcription) and the Telegram handler immediately after a save (so a change
    is effective on the NEXT media operation, with no redeploy and no restart).
    The caller reads the store; this module only receives plain values.

    Never raises — a settings change must not be able to break either the panel or
    startup. Returns a sanitized status dict for the caller's trace; it contains
    the provider, the model and the bounded reason, never a credential.
    """
    try:
        plane = parse_stt_config(config)
        candidate = plane.active_candidate
        if candidate is not None and candidate.provider in _ADAPTER_PROVIDERS:
            return _apply_adapter(candidate, plane)
        # Gemini candidates, registered-but-unimplemented candidates (which keep
        # the default route, as the control plane documents) and legacy values.
        from backend.services.gemini_media_engine import apply_stt_settings

        return apply_stt_settings(plane.engine_settings())
    except Exception as exc:  # noqa: BLE001 — a settings apply is never fatal
        logger.warning("STT_ENGINE_APPLY_FAILED error=%s", type(exc).__name__)
        return {"configured": False, "reason": type(exc).__name__}


def _apply_adapter(candidate: SttCandidate, plane: Any) -> dict[str, Any]:
    """Provision (or clear) the SELECTED candidate's own adapter engine.

    The resolver is the ONE place an engine is constructed, so this path can
    never reach a provider other than the candidate's own: whichever adapter
    ``build_engine`` returns is installed verbatim, and when it returns none
    NOTHING is provisioned.
    """
    engine, reason = build_engine(
        candidate, language=plane.language, passes=plane.passes,
    )
    if engine is None:
        media_service.set_stt_engine(None)
        logger.warning(
            "STT_ENGINE_UNPROVISIONED provider=%s model=%s reason=%s",
            candidate.provider, candidate.model, reason,
        )
        return {
            "configured": False,
            "provider": candidate.provider,
            "stt_model": candidate.model,
            "stt_language": plane.language,
            "stt_passes": plane.passes,
            "reason": reason,
        }
    media_service.set_stt_engine(engine)
    logger.info(
        "STT_ENGINE_APPLIED provider=%s model=%s language=%s stt_passes=%d "
        "key_env_var=%s",
        candidate.provider, candidate.model, plane.language or "auto",
        plane.passes, engine.key_env_var or "-",
    )
    return {
        "configured": True,
        "provider": candidate.provider,
        "stt_model": candidate.model,
        "stt_language": plane.language,
        "stt_passes": plane.passes,
        "reason": "",
    }
