"""Speech-to-text provider test — the capability-specific health probe.

Answers, for ONE registered candidate, the questions the configuration alone
cannot:

  * is a credential configured for this provider?
  * can the candidate actually be built on this build?
  * does the provider accept the audio container this project sends?
  * does it return a valid, NON-EMPTY transcript?
  * how long did the request take?
  * what deterministic failure class occurred when it failed?

Two facts are kept strictly apart: **a credential existing is NOT health.** Only
a completed request that returned a non-empty transcript is ``PASSED``; a missing
credential is its OWN state (``CREDENTIAL_MISSING``) and never a successful
check, and an unimplemented candidate is ``NOT_IMPLEMENTED`` without any request
being made at all.

The result is PROCESS-LOCAL and deliberately not persisted: it is a diagnostic
observation, not configuration — there is no ``ai_config`` column and no new
table for it (this phase adds none), and a restart honestly returns every
candidate to ``NOT_TESTED`` rather than replaying a stale claim of health. The
active → fallback → cooldown manager of a later phase may consume these
observations; it must not treat them as durable state.

The probe never invents a test target: the candidate must be REGISTERED (an
unknown id is refused, never resolved to something else), the engine is built by
the existing resolution seam (``backend/services/stt_engine_factory.py``) so the
provider/model pair is the registered one, and the payload is a deterministic,
bounded WAV generated in-process (no bundled binary, no dependency, inside the
existing audio contract). It reports the provider, model, elapsed time, state and
failure class — never the transcript, the audio, the credential or a Telegram
identifier.
"""
from __future__ import annotations

import array
import asyncio
import logging
import math
import struct
import sys
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from enum import Enum
from typing import Any, Iterable

from backend.ai.stt_control_plane import SttCandidate, all_candidates, get_candidate
from backend.services.media_service import MediaError

logger = logging.getLogger(__name__)

#: Outer bound of ONE probe. The engine it drives carries its own tighter
#: operation deadline, so the classified engine failure normally wins; this bound
#: only guarantees the probe itself can never hang a handler or the event loop.
TEST_TIMEOUT_S = 50.0

#: The synthetic probe payload: one second of a low-amplitude tone as 16 kHz mono
#: 16-bit PCM WAV — inside every bound the media boundary enforces for speech.
TEST_SAMPLE_RATE = 16_000
TEST_DURATION_S = 1.0
TEST_TONE_HZ = 440.0
TEST_TONE_AMPLITUDE = 0.2
_MAX_TEST_DURATION_S = 5.0

#: The failure classes the probe itself can report, plus the neutral defaults it
#: uses when an engine's own classification is unavailable. The probe is
#: PROVIDER-AGNOSTIC: an adapter attaches its bounded ``failure_class`` to the
#: failure it raises, and this module reports that token verbatim without
#: importing any provider module.
FAILURE_UNKNOWN_CANDIDATE = "unknown_candidate"
FAILURE_TEST_TIMEOUT = "test_timeout"
FAILURE_EMPTY = "empty_transcription"
FAILURE_UNKNOWN = "unknown"


def _failure_class_of(error: BaseException) -> str:
    """The bounded failure class an engine attached to its failure (or a default)."""
    return str(getattr(error, "failure_class", "") or FAILURE_UNKNOWN)


class SttTestState(str, Enum):
    """Honest outcome of ONE provider probe.

    ``NOT_TESTED``        — no probe has run in this process (the default).
    ``NOT_IMPLEMENTED``   — registered capability with no execution path here.
    ``CREDENTIAL_MISSING``— the provider has no credential; nothing was sent.
    ``PASSED``            — the provider answered with a non-empty transcript.
    ``FAILED``            — the provider was reached and failed, or the probe
                            could not be completed; ``failure_class`` says how.
    """

    NOT_TESTED = "not_tested"
    NOT_IMPLEMENTED = "not_implemented"
    CREDENTIAL_MISSING = "credential_missing"
    PASSED = "passed"
    FAILED = "failed"


#: The owner-facing wording of each state. Kept here so every surface that shows a
#: candidate's state (the hub line and the Speech-to-Text panel) says the same
#: thing and no surface can invent a stronger claim than the state supports.
STATE_LABELS: dict[str, str] = {
    SttTestState.NOT_TESTED.value: "not tested",
    SttTestState.NOT_IMPLEMENTED.value: "not available",
    SttTestState.CREDENTIAL_MISSING.value: "no credential",
    SttTestState.PASSED.value: "test passed",
    SttTestState.FAILED.value: "test failed",
}


@dataclass(frozen=True)
class SttTestResult:
    """The bounded, non-sensitive outcome of ONE provider probe.

    ``latency_ms`` is the measured wall-clock time of the completed probe,
    ``transcript_chars`` the LENGTH of what came back (never its content), and
    ``failure_class`` a closed token from the engine's own classification.
    """

    candidate_id: str
    provider: str
    model: str
    state: str
    failure_class: str = ""
    latency_ms: int = 0
    transcript_chars: int = 0
    detail: str = ""
    tested_at: str = ""

    @property
    def passed(self) -> bool:
        return self.state == SttTestState.PASSED.value

    def state_label(self) -> str:
        """The owner-facing wording of this result's state."""
        return STATE_LABELS.get(self.state, self.state)

    def summary(self) -> str:
        """ONE bounded owner-facing line: state, class and elapsed time."""
        parts = [self.state_label()]
        if self.state == SttTestState.FAILED.value and self.failure_class:
            parts.append(self.failure_class)
        if self.state == SttTestState.PASSED.value and self.latency_ms:
            parts.append(f"{self.latency_ms} ms")
        return " · ".join(parts)

    def to_dict(self) -> dict[str, Any]:
        return {
            "candidate_id": self.candidate_id,
            "provider": self.provider,
            "model": self.model,
            "state": self.state,
            "failure_class": self.failure_class,
            "latency_ms": self.latency_ms,
            "transcript_chars": self.transcript_chars,
            "detail": self.detail,
            "tested_at": self.tested_at,
        }


#: Process-local probe results, keyed by candidate id. Never persisted — see the
#: module docstring: a health observation is not durable configuration.
_results: dict[str, SttTestResult] = {}


def last_result(candidate_id: str) -> SttTestResult | None:
    """The most recent probe result for a candidate, or ``None`` when untested."""
    return _results.get(str(candidate_id or ""))


def result_state(candidate_id: str) -> str:
    """The probe state of a candidate (``not_tested`` when none has run)."""
    result = last_result(candidate_id)
    return result.state if result else SttTestState.NOT_TESTED.value


def state_label(candidate_id: str) -> str:
    """The owner-facing state wording for a candidate."""
    return STATE_LABELS.get(result_state(candidate_id), result_state(candidate_id))


def clear_results() -> None:
    """Drop every process-local probe result (tests and explicit resets)."""
    _results.clear()


def _record(result: SttTestResult) -> SttTestResult:
    _results[result.candidate_id] = result
    logger.info(
        "STT_PROVIDER_TEST candidate=%s provider=%s model=%s state=%s "
        "failure_class=%s elapsed_ms=%d chars=%d",
        result.candidate_id, result.provider or "-", result.model or "-",
        result.state, result.failure_class or "-", result.latency_ms,
        result.transcript_chars,
    )
    return result


def _now() -> str:
    return datetime.now(timezone.utc).isoformat()


def _wav_container(samples: bytes) -> bytes:
    """Wrap raw mono PCM16 samples in a RIFF/WAVE container (standard library)."""
    return (
        b"RIFF"
        + struct.pack("<I", 36 + len(samples))
        + b"WAVE"
        + b"fmt "
        + struct.pack("<IHHIIHH", 16, 1, 1, TEST_SAMPLE_RATE, TEST_SAMPLE_RATE * 2, 2, 16)
        + b"data"
        + struct.pack("<I", len(samples))
        + samples
    )


def test_audio_payload(duration_s: float = TEST_DURATION_S) -> bytes:
    """A bounded, deterministic WAV payload inside the existing audio contract.

    Generated in-process from the standard library only: no bundled binary, no
    dependency and no network. The duration is clamped, so the payload can never
    exceed the media boundary's own speech-to-text bounds.
    """
    seconds = max(0.1, min(float(duration_s or TEST_DURATION_S), _MAX_TEST_DURATION_S))
    count = int(TEST_SAMPLE_RATE * seconds)
    samples = array.array(
        "h",
        (
            int(
                TEST_TONE_AMPLITUDE
                * 32767
                * math.sin(2 * math.pi * TEST_TONE_HZ * index / TEST_SAMPLE_RATE)
            )
            for index in range(count)
        ),
    )
    if sys.byteorder == "big":  # WAV is little-endian PCM
        samples.byteswap()
    return _wav_container(samples.tobytes())


async def test_candidate(
    candidate_id: str,
    *,
    language: str = "",
    audio: bytes | None = None,
    timeout: float = TEST_TIMEOUT_S,
) -> SttTestResult:
    """Probe ONE registered candidate and record the result.

    The candidate must be registered: an unknown identifier is refused honestly
    rather than resolved to another candidate. A candidate with no execution path
    is reported ``NOT_IMPLEMENTED`` and a provider without a credential
    ``CREDENTIAL_MISSING`` — in both cases NO request is made, so an untestable
    candidate can never be reported as healthy. Only a completed request that
    returned a non-empty transcript is ``PASSED``.

    ``audio`` overrides the synthetic payload, which is how a live operator runs
    the probe against real speech; the default payload proves credential
    acceptance, transport and response parsing.
    """
    requested = str(candidate_id or "")
    candidate = get_candidate(requested)
    if candidate is None:
        return _record(
            SttTestResult(
                candidate_id=requested,
                provider="",
                model="",
                state=SttTestState.FAILED.value,
                failure_class=FAILURE_UNKNOWN_CANDIDATE,
                detail=(
                    "This identifier is not a registered speech-to-text candidate "
                    "— nothing was tested."
                ),
                tested_at=_now(),
            )
        )
    if not candidate.implemented:
        return _record(
            SttTestResult(
                candidate_id=candidate.candidate_id,
                provider=candidate.provider,
                model=candidate.model,
                state=SttTestState.NOT_IMPLEMENTED.value,
                detail=(
                    f"{candidate.label} is registered but has no execution path on "
                    "this build — no request was made."
                ),
                tested_at=_now(),
            )
        )

    engine, reason = _build_engine(candidate, language)
    if engine is None:
        from backend.services.stt_engine_factory import REASON_MISSING_CREDENTIAL

        missing = reason == REASON_MISSING_CREDENTIAL
        return _record(
            SttTestResult(
                candidate_id=candidate.candidate_id,
                provider=candidate.provider,
                model=candidate.model,
                state=(
                    SttTestState.CREDENTIAL_MISSING.value if missing
                    else SttTestState.FAILED.value
                ),
                failure_class="" if missing else reason,
                detail=(
                    "No credential is configured for this provider on this "
                    "deployment — nothing was sent."
                    if missing else
                    "This candidate cannot run on this build — nothing was sent."
                ),
                tested_at=_now(),
            )
        )

    payload = audio if audio else test_audio_payload()
    started = time.monotonic()
    failure_class = ""
    transcript_chars = 0
    state = SttTestState.PASSED.value
    detail = ""
    try:
        text = await asyncio.wait_for(
            asyncio.to_thread(engine.transcribe, payload), timeout=timeout,
        )
    except asyncio.CancelledError:
        raise
    except asyncio.TimeoutError:
        state = SttTestState.FAILED.value
        failure_class = FAILURE_TEST_TIMEOUT
        detail = f"The probe did not finish within {timeout:g}s."
    except MediaError as exc:
        state = SttTestState.FAILED.value
        failure_class = _failure_class_of(exc)
        detail = "The provider rejected or could not complete the transcription."
    except Exception as exc:  # noqa: BLE001 — the engine boundary
        state = SttTestState.FAILED.value
        failure_class = _failure_class_of(exc)
        detail = f"The probe failed ({type(exc).__name__})."
    else:
        text = text if isinstance(text, str) else ""
        if not text.strip():
            # A reachable provider with NO transcript is a failure, never a
            # successful empty transcription — the payload's own speech content
            # is reported here, not a claim about the provider.
            state = SttTestState.FAILED.value
            failure_class = FAILURE_EMPTY
            detail = (
                "The provider accepted the audio but returned no transcript. "
                "A synthetic probe payload contains no speech; run the probe with "
                "real speech audio to verify recognition."
            )
        else:
            transcript_chars = len(text)

    return _record(
        SttTestResult(
            candidate_id=candidate.candidate_id,
            provider=candidate.provider,
            model=candidate.model,
            state=state,
            failure_class=failure_class,
            latency_ms=int((time.monotonic() - started) * 1000),
            transcript_chars=transcript_chars,
            detail=detail,
            tested_at=_now(),
        )
    )


async def test_candidates(
    candidate_ids: Iterable[str] | None = None,
    *,
    language: str = "",
    audio: bytes | None = None,
) -> list[SttTestResult]:
    """Probe several candidates SEQUENTIALLY, in the registry's canonical order.

    Defaults to every IMPLEMENTED candidate, so the deterministic pool order of
    the control plane is also the probe order. Sequential on purpose: the project
    budget allows exactly one provider request in flight, and a probe must never
    make the runtime's own media path contend with a burst of requests.
    """
    if candidate_ids is None:
        targets: list[str] = [
            candidate.candidate_id for candidate in all_candidates() if candidate.implemented
        ]
    else:
        targets = [str(candidate_id) for candidate_id in candidate_ids]
    results: list[SttTestResult] = []
    for candidate_id in targets:
        results.append(await test_candidate(candidate_id, language=language, audio=audio))
    return results


def candidate_state_row(candidate: SttCandidate) -> str:
    """The owner-facing state of ONE registered candidate (never a strong claim).

    An unimplemented candidate is reported as such whatever was probed before, so
    a stale observation can never make an unexecutable capability look usable.
    """
    if not candidate.implemented:
        return STATE_LABELS[SttTestState.NOT_IMPLEMENTED.value]
    return state_label(candidate.candidate_id)


def _build_engine(candidate: SttCandidate, language: str) -> tuple[Any | None, str]:
    """The existing resolution seam — imported lazily to keep module load light."""
    from backend.services.stt_engine_factory import build_engine

    return build_engine(candidate, language=language)
