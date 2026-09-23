"""TTS provider → engine resolution — the ONE provider-specific construction seam.

The control plane (``backend/ai/tts_control_plane.py``) says WHAT is selected;
this module turns that selection into the engine the TTS boundary
(``backend/services/tts_service.py``) executes. It is the ONE place a
provider-specific engine is constructed, which keeps three contracts intact:

  * the TTS boundary stays provider-independent,
  * the control plane stays configuration-only (no execution imports),
  * no Telegram object, owner id, chat id, message id, caption, reply text or
    conversation state can reach an engine — the only inputs here are the
    resolved selection, one credential and the provider's own base URL.

Routing is deterministic and fail-closed:

  * a REGISTERED provider whose execution path exists on this build is built for
    ITS OWN model and voice — a selection is never substituted by another
    provider's model, and the model/voice are re-validated against the registry
    here so neither can be typed or computed into a request;
  * a registered-but-unimplemented provider, or an unregistered model/voice, is
    reported honestly and the boundary is left with NO engine, so the speech path
    keeps its documented fail-closed behavior instead of quietly synthesizing with
    something else.

CREDENTIALS. An engine is always built for ONE credential, and the pool
(``backend/services/tts_credential_pool.py``) — never this module — decides which.
A credential that came from the deployment's environment is passed as "resolve
your own", which keeps the adapter's existing resolution and its truthful
``key_env_var`` label; only a pooled credential is handed over as an explicit
value.

Provider fallback is NOT this module's concern: it resolves ONE provider (for one
credential). The fallback layer consults this seam rather than duplicate it.
"""
from __future__ import annotations

from typing import Any

from backend.ai.credential_source import CredentialRecord
from backend.ai.tts_control_plane import (
    TtsSelection,
    get_model,
    get_provider,
    get_voice,
)
from backend.services.tts_service import (
    FAILURE_UNAVAILABLE,
    FAILURE_UNSUPPORTED_MODEL,
    FAILURE_UNSUPPORTED_VOICE,
)

#: The bounded reason a provider has no execution path on this build.
REASON_NOT_IMPLEMENTED = FAILURE_UNAVAILABLE


def explicit_key(credential: CredentialRecord | None) -> str:
    """The key to hand an adapter, or ``""`` to let the adapter resolve its ENV.

    A pooled credential is passed explicitly; the deployment's OWN credential is
    not, because the adapter already resolves it — and doing so preserves its
    truthful ``key_env_var`` label and its existing precedence order instead of
    duplicating that resolution here.
    """
    if credential is None or credential.is_env:
        return ""
    return credential.secret


def build_engine_for(
    provider: str,
    model: str,
    voice: str,
    credential: CredentialRecord | None = None,
) -> tuple[Any | None, str]:
    """``(engine, reason)`` for ONE registered provider/model/voice + credential.

    ``engine`` is ``None`` when this build cannot run the selection and ``reason``
    is then a bounded token from the TTS boundary's own closed taxonomy. No engine
    is ever built for an unregistered provider, an unimplemented provider, a model
    of another provider or a voice of another model.
    """
    entry = get_provider(provider)
    if entry is None or not entry.implemented:
        return None, REASON_NOT_IMPLEMENTED
    if get_model(provider, model) is None:
        return None, FAILURE_UNSUPPORTED_MODEL
    if get_voice(provider, model, voice) is None:
        return None, FAILURE_UNSUPPORTED_VOICE

    # ONE bounded dispatch over the registered providers, so a provider's adapter
    # is the only place its request shape lives. An entry without a branch here
    # resolves to no engine and the boundary keeps its fail-closed behavior
    # instead of synthesizing with a provider the owner did not select.
    if entry.provider == "openai":
        from backend.services import openai_tts_engine

        return openai_tts_engine.build_engine(
            model, voice=voice, api_key=explicit_key(credential),
        )
    if entry.provider == "gemini":
        from backend.services import gemini_tts_engine

        return gemini_tts_engine.build_engine(
            model, voice=voice, api_key=explicit_key(credential),
        )
    if entry.provider == "grok":
        from backend.services import grok_tts_engine

        return grok_tts_engine.build_engine(
            model, voice=voice, api_key=explicit_key(credential),
        )
    if entry.provider == "speechmatics":
        from backend.services import speechmatics_tts_engine

        return speechmatics_tts_engine.build_engine(
            model, voice=voice, api_key=explicit_key(credential),
        )
    return None, REASON_NOT_IMPLEMENTED


def build_engine(selection: TtsSelection) -> tuple[Any | None, str]:
    """``(engine, reason)`` for a resolved selection.

    The credential is the provider's FIRST pooled entry (the same order the
    runtime's rotation uses), so a capability probe and a real request resolve the
    engine from the same configuration. With no loaded pool — the pre-existing
    single-credential route and every unconfigured installation — the adapter's own
    environment resolution applies unchanged.
    """
    from backend.services import tts_credential_pool

    return build_engine_for(
        selection.provider,
        selection.model,
        selection.voice,
        tts_credential_pool.first_for(selection.provider),
    )


def provisioning_credential(selection: TtsSelection) -> CredentialRecord | None:
    """The credential the SELECTED provider is provisioned with (or ``None``)."""
    from backend.services import tts_credential_pool

    return tts_credential_pool.first_for(selection.provider)
