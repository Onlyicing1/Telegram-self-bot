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

CREDENTIALS (M2.4). An engine is always built for ONE credential, and the pool
(`backend/services/stt_credential_pool.py`) — never this module — decides which.
``build_engine`` builds the candidate's engine from the provider's OWN
configured credential (the pre-existing route, unchanged byte for byte);
``build_engine_with_credential`` builds it for one explicit pool entry, so the
provider adapter receives exactly the credential of the current attempt and
nothing else. A credential that came from the deployment's environment is passed
as "resolve your own", which keeps the adapter's existing resolution and its
truthful ``key_env_var`` label; only a pooled credential is handed over as an
explicit value. Provisioning records WHICH credential the selected engine was
built from, so the runtime's first attempt can reuse the engine it already has
instead of building an equivalent one.

Provider adapters and runtime fallback are NOT this module's concern: it resolves
the ONE selected candidate (for one credential). The fallback layers — provider
(`stt_fallback`) and credential (`stt_credential_pool`) — consult this seam
rather than duplicate it.
"""
from __future__ import annotations

import logging
from typing import Any, Mapping

from backend.ai.credential_source import CredentialRecord
from backend.ai.stt_control_plane import SttCandidate, parse_stt_config
from backend.services import (
    groq_stt_engine,
    media_service,
    speechmatics_stt_engine,
    stt_credential_pool,
)

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
    return _build(candidate, language=language, passes=passes, credential=None)


def build_engine_with_credential(
    candidate: SttCandidate | None,
    credential: CredentialRecord | None,
    *,
    language: str = "",
    passes: int = 1,
) -> tuple[Any | None, str]:
    """``(engine, reason)`` for ONE credential of ONE registered candidate.

    The credential-aware face of the seam above: same routing, same validation,
    same fail-closed reasons — the only difference is the credential the engine is
    built with, so an attempt carries exactly the pool entry the caller selected.
    With no credential this delegates to :func:`build_engine` verbatim, which is
    the pre-existing single-credential route.
    """
    if credential is None:
        return build_engine(candidate, language=language, passes=passes)
    if candidate is None or not candidate.implemented:
        return None, REASON_NOT_IMPLEMENTED
    return _build(candidate, language=language, passes=passes, credential=credential)


def _explicit_key(credential: CredentialRecord | None) -> str:
    """The key to hand an adapter, or ``""`` to let the adapter resolve its ENV.

    A pooled credential is passed explicitly; the deployment's OWN credential is
    not, because the adapter already resolves it — and doing so preserves its
    truthful ``key_env_var`` label and its existing precedence order instead of
    duplicating that resolution here.
    """
    if credential is None or credential.is_env:
        return ""
    return credential.secret


def _gemini_credential(credential: CredentialRecord | None) -> tuple[str, str]:
    """``(api_key, label)`` for the Gemini leg — its existing shape."""
    from backend.services import gemini_media_engine as gemini

    if credential is None or credential.is_env:
        return gemini.resolve_api_key()
    return credential.secret, credential.credential_id


def _build(
    candidate: SttCandidate,
    *,
    language: str,
    passes: int,
    credential: CredentialRecord | None,
) -> tuple[Any | None, str]:
    """Construct the candidate's OWN provider engine for ONE credential."""
    if candidate.provider == groq_stt_engine.PROVIDER_NAME:
        return groq_stt_engine.build_engine(
            candidate.model, language=language, passes=passes,
            api_key=_explicit_key(credential),
        )
    if candidate.provider == speechmatics_stt_engine.PROVIDER_NAME:
        return speechmatics_stt_engine.build_engine(
            candidate.model, language=language, passes=passes,
            api_key=_explicit_key(credential),
        )
    if candidate.provider == "gemini":
        from backend.services import gemini_media_engine as gemini

        api_key, key_env_var = _gemini_credential(credential)
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


def _provisioning_credential(plane: Any) -> CredentialRecord | None:
    """The credential the SELECTED candidate is provisioned with (or ``None``).

    The pool's own first entry — the same order the runtime's rotation uses — so
    provisioning and execution can never disagree about which credential the
    selected engine carries. ``None`` means the pool was never loaded for this
    provider, and the pre-existing environment resolution then applies unchanged.
    """
    candidate = plane.active_candidate
    if candidate is None:
        return None
    return stt_credential_pool.first_for(candidate.provider)


def apply_stt_config(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Install the owner's persisted STT selection onto the live STT engine.

    The ONE entry point called by the places that own this state: the runtime
    supervisor at startup (so the persisted values are in effect from the first
    transcription) and the Telegram handler immediately after a save (so a change
    is effective on the NEXT media operation, with no redeploy and no restart).
    The caller reads the store; this module only receives plain values.

    The rotation and the engine are derived from the SAME parsed configuration, so
    the credential the selected engine was built with is exactly the credential the
    runtime's first attempt will report — hence the recorded
    ``provisioned_credential_id``.

    Never raises — a settings change must not be able to break either the panel or
    startup. Returns a sanitized status dict for the caller's trace; it contains
    the provider, the model and the bounded reason, never a credential.
    """
    try:
        plane = parse_stt_config(config)
        credential = _provisioning_credential(plane)
        # The fallback rotation is derived from the SAME parsed plane that
        # provisions the selected engine — never from a second read of the
        # store. An unresolved legacy value deactivates fallback entirely.
        from backend.services import stt_fallback

        stt_fallback.register_plan(
            plane,
            provisioned_credential_id=(
                credential.credential_id if credential is not None else ""
            ),
        )
        candidate = plane.active_candidate
        if candidate is not None and candidate.provider in _ADAPTER_PROVIDERS:
            return _apply_adapter(candidate, plane, credential)
        # Gemini candidates, registered-but-unimplemented candidates (which keep
        # the default route, as the control plane documents) and legacy values.
        from backend.services.gemini_media_engine import apply_stt_settings

        return apply_stt_settings(
            plane.engine_settings(), credential=_gemini_credential(credential),
        )
    except Exception as exc:  # noqa: BLE001 — a settings apply is never fatal
        logger.warning("STT_ENGINE_APPLY_FAILED error=%s", type(exc).__name__)
        return {"configured": False, "reason": type(exc).__name__}


async def apply_stt_config_async(config: Mapping[str, Any] | None) -> dict[str, Any]:
    """Credential-aware settings apply: load the pools, then provision.

    The ONE addition M2.4 makes to the two places that already own this state.
    Ordered deliberately: the pools are read FIRST, so the engine and the rotation
    are provisioned against the credentials that will actually be attempted, and a
    secret backend that is slow or unavailable delays provisioning by at most its
    own bounded read instead of failing it. Both halves are already never-fatal,
    so this adds no new failure mode to startup or to the settings panel.
    """
    try:
        counts = await stt_credential_pool.prepare()
        logger.info(
            "STT_CREDENTIAL_PREPARE providers=%s credentials=%s",
            len(counts), sum(counts.values()),
        )
    except Exception as exc:  # noqa: BLE001 — an optional pool is never fatal
        logger.warning("STT_CREDENTIAL_PREPARE_FAILED error=%s", type(exc).__name__)
    return apply_stt_config(config)


def _apply_adapter(
    candidate: SttCandidate, plane: Any, credential: CredentialRecord | None,
) -> dict[str, Any]:
    """Provision (or clear) the SELECTED candidate's own adapter engine.

    The resolver is the ONE place an engine is constructed, so this path can
    never reach a provider other than the candidate's own: whichever adapter
    ``build_engine_with_credential`` returns is installed verbatim, and when it
    returns none NOTHING is provisioned.
    """
    engine, reason = build_engine_with_credential(
        candidate, credential, language=plane.language, passes=plane.passes,
    )
    if engine is None:
        # No engine provisioned → the boundary stays fail-closed; a rotation
        # whose FIRST candidate cannot run must not silently arm substitutes.
        from backend.services import stt_fallback

        stt_fallback.clear_registration()
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
