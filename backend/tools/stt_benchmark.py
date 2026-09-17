"""
Media Processing — the repeat-run STT benchmark (the LIVE measurement of the
recognition-quality seam).

The runtime never imports this module. It exists because of a question the
repository cannot answer from code (``INVESTIGATION.md`` §19.4): does the SAME
audio, transcribed repeatedly on the SAME route, actually produce DIFFERENT
hypotheses — and if it does, does the bounded multi-pass consensus
(``backend/services/stt_consensus.py``, ``AI_GEMINI_STT_PASSES``) improve the
transcript? Only a real provider call can answer that, so this tool makes the
measurement reproducible instead of subjective:

    python -m backend.tools.stt_benchmark --audio voice.ogg --reference ref.txt \
        --passes 1,2,3 --repeat 3 --json out.json

It uploads nothing, touches no Telegram object and reads exactly one local audio
file. It prints, per configuration: the transcript, the elapsed time, the exact
number of API calls per leg, and — when a human-verified reference transcript is
supplied — WER, CER and the substitution/deletion/insertion counts.

Honesty rules this tool follows:

* it reports REAL numbers only; every number comes from an actual call, and a
  failure or an empty output is reported as such, never smoothed over;
* one sample is not an accuracy claim, and this tool says so in its own output:
  WER/CER over a handful of utterances describes those utterances;
* it measures CODE correctness (the request really is sent, once per pass, with
  one upload and one cleanup) separately from PROVIDER behaviour (repeat-run
  consistency) and from RECOGNITION QUALITY (WER/CER against the reference);
* the transcript is printed deliberately — it is the operator's own audio, on the
  operator's own machine. The bot's own logging never does this.
"""
from __future__ import annotations

import argparse
import difflib
import json
import sys
import time
import unicodedata
from collections import Counter
from dataclasses import dataclass, field
from typing import Callable, Iterable, Sequence

import httpx

from backend.services import gemini_media_engine, media_service

#: The pass counts the seam accepts, in the order the benchmark runs them. The
#: single pass is the production baseline, three is the smallest count the
#: consensus can act on, and two is measured because it is the control that
#: shows whether a second call adds any information at all.
DEFAULT_PASS_COUNTS = (1, 3)


# ── Metrics (pure, dependency-free, hermetically tested) ──


def edit_counts(reference: Sequence[str], hypothesis: Sequence[str]) -> tuple[int, int, int]:
    """``(substitutions, deletions, insertions)`` from ``reference`` to ``hypothesis``."""
    substitutions = deletions = insertions = 0
    matcher = difflib.SequenceMatcher(None, list(reference), list(hypothesis), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "replace":
            shared = min(i2 - i1, j2 - j1)
            substitutions += shared
            deletions += (i2 - i1) - shared
            insertions += (j2 - j1) - shared
        elif tag == "delete":
            deletions += i2 - i1
        elif tag == "insert":
            insertions += j2 - j1
    return substitutions, deletions, insertions


def word_error_rate(reference: str, hypothesis: str) -> float:
    """Word error rate of ``hypothesis`` against a human-verified reference."""
    reference_words = reference.split()
    if not reference_words:
        return 0.0 if not hypothesis.split() else 1.0
    substitutions, deletions, insertions = edit_counts(reference_words, hypothesis.split())
    return (substitutions + deletions + insertions) / len(reference_words)


def character_error_rate(reference: str, hypothesis: str) -> float:
    """Character error rate, whitespace-normalized so spacing is not punished."""
    reference_chars = " ".join(reference.split())
    if not reference_chars:
        return 0.0 if not " ".join(hypothesis.split()) else 1.0
    substitutions, deletions, insertions = edit_counts(
        list(reference_chars), list(" ".join(hypothesis.split())),
    )
    return (substitutions + deletions + insertions) / len(reference_chars)


def script_counts(text: str) -> dict[str, int]:
    """How many letters of ``text`` belong to each script family (bounded buckets).

    This is the cheap, deterministic check for the one failure that needs no
    reference transcript: non-Latin speech coming back in Latin letters.
    """
    counts = {"latin": 0, "arabic": 0, "other": 0}
    for char in text:
        if not char.isalpha():
            continue
        name = unicodedata.name(char, "")
        if "LATIN" in name:
            counts["latin"] += 1
        elif "ARABIC" in name:
            counts["arabic"] += 1
        else:
            counts["other"] += 1
    return counts


def repeat_run_consistency(transcripts: Sequence[str]) -> float:
    """The share of repeated runs that produced the most common transcript.

    ``1.0`` means every repeated run agreed byte for byte (the consensus then has
    nothing to work with); a low value means the provider IS returning different
    hypotheses for the same bytes, which is the precondition the multi-pass seam
    needs.
    """
    if not transcripts:
        return 0.0
    counts = Counter(transcripts)
    return counts.most_common(1)[0][1] / len(transcripts)


# ── API-call accounting (real clients, real network, only counted) ──


def _leg_name(url: str, method: str) -> str:
    if method == "POST" and url.endswith("/upload/v1beta/files"):
        return "upload_start"
    if method == "POST" and "/files" in url:
        # The resumable upload URL the API itself returned, on its upload host.
        return "upload_finalize"
    if method == "DELETE":
        return "delete"
    if method == "GET":
        return "file_status"
    if url.endswith("/interactions"):
        return "interaction"
    if url.endswith(":generateContent"):
        return "generate"
    return "other"


class ApiCallCounter:
    """Counts the engine's HTTP requests per leg by wrapping ``httpx.Client``.

    Nothing else is changed: the real transport, the real timeouts and the real
    requests are used, so the counts describe the code that runs in production.
    """

    def __init__(self) -> None:
        self.calls: Counter[str] = Counter()
        self._original: Callable[..., httpx.Client] | None = None

    def __enter__(self) -> "ApiCallCounter":
        original = httpx.Client
        calls = self.calls

        class _CountingClient(original):  # type: ignore[misc, valid-type]
            def send(self, request: httpx.Request, **kwargs: object) -> httpx.Response:
                calls[_leg_name(str(request.url), request.method)] += 1
                return super().send(request, **kwargs)

        self._original = original
        httpx.Client = _CountingClient  # type: ignore[assignment]
        return self

    def __exit__(self, *exc_info: object) -> None:
        if self._original is not None:
            httpx.Client = self._original  # type: ignore[assignment]

    def snapshot(self) -> dict[str, int]:
        return dict(sorted(self.calls.items()))


# ── The benchmark itself ──


@dataclass
class PassRun:
    """ONE configuration's result: code facts, provider output and timing."""

    passes: int
    transcript: str = ""
    elapsed_ms: int = 0
    api_calls: dict[str, int] = field(default_factory=dict)
    error: str = ""

    @property
    def empty(self) -> bool:
        return not self.transcript.strip()

    def as_dict(self) -> dict[str, object]:
        return {
            "passes": self.passes,
            "transcript": self.transcript,
            "elapsed_ms": self.elapsed_ms,
            "api_calls": dict(self.api_calls),
            "empty_output": self.empty,
            "error": self.error,
        }


def run_pass_set(
    engine_factory: Callable[[int], object], audio: bytes, passes: int,
) -> PassRun:
    """Run ONE configuration (``passes`` recognition passes) over ``audio``.

    The engine factory keeps this measurable without a live credential: the tests
    inject a scripted engine, the CLI injects the real one.
    """
    engine = engine_factory(passes)
    run = PassRun(passes=passes)
    with ApiCallCounter() as counter:
        started = time.monotonic()
        try:
            text = engine.transcribe(audio)  # type: ignore[attr-defined]
            run.transcript = text if isinstance(text, str) else ""
        except Exception as exc:  # noqa: BLE001 — a failed run is DATA, not a crash
            run.error = f"{type(exc).__name__}: {exc}"
        run.elapsed_ms = int((time.monotonic() - started) * 1000)
    run.api_calls = counter.snapshot()
    return run


def benchmark(
    engine_factory: Callable[[int], object],
    audio: bytes,
    *,
    pass_counts: Iterable[int] = DEFAULT_PASS_COUNTS,
    reference: str = "",
    repeat: int = 0,
) -> dict[str, object]:
    """The whole measurement: each pass count, plus repeated single passes.

    ``repeat`` runs the single-pass configuration that many times to measure
    repeat-run consistency (the precondition the consensus needs). It defaults to
    the number of configured pass counts, so a 1/2/3 run measures three repeats.
    """
    counts = [int(value) for value in pass_counts]
    repeats = repeat or max(len(counts), 1)
    repeats = max(1, min(repeats, 10))

    runs = [run_pass_set(engine_factory, audio, value) for value in counts]
    repeat_runs = [run_pass_set(engine_factory, audio, 1) for _ in range(repeats)]

    payload: dict[str, object] = {
        "audio_bytes": len(audio),
        "reference": reference,
        "configurations": [run.as_dict() for run in runs],
        "repeat_runs": [run.as_dict() for run in repeat_runs],
        "consistency": repeat_run_consistency([run.transcript for run in repeat_runs]),
        "empty_output_rate": sum(run.empty for run in runs) / len(runs) if runs else 0.0,
    }
    if reference:
        payload["quality"] = {
            f"passes={run.passes}": {
                "wer": word_error_rate(reference, run.transcript),
                "cer": character_error_rate(reference, run.transcript),
                "substitutions": edit_counts(reference.split(), run.transcript.split())[0],
                "deletions": edit_counts(reference.split(), run.transcript.split())[1],
                "insertions": edit_counts(reference.split(), run.transcript.split())[2],
                "script": script_counts(run.transcript),
            }
            for run in runs
        }
    return payload


def load_audio(path: str) -> bytes:
    """Read ONE local audio file, under the boundary's own input bound."""
    with open(path, "rb") as handle:
        data = handle.read()
    if not data:
        raise ValueError("the audio file is empty")
    if len(data) > media_service.MAX_STT_INPUT_BYTES:
        raise ValueError(
            f"the audio is larger than the {media_service.MAX_STT_INPUT_BYTES} byte STT bound"
        )
    mime_type = gemini_media_engine.gemini_mime_type(data)  # refuses an undocumented container
    if not mime_type.startswith("audio/"):
        raise ValueError(f"{mime_type} is not an audio container this benchmark can transcribe")
    return data


def _engine_factory() -> Callable[[int], object]:
    """A factory over the REAL engine, resolved from the deployment's own ENV."""
    engine, model, reason = gemini_media_engine.build_gemini_media_engine()
    if engine is None:
        raise SystemExit(f"no Gemini media engine: {reason}")
    stt_model, stt_language = engine.stt_model, engine.stt_language

    def factory(passes: int) -> gemini_media_engine.GeminiMediaEngine:
        return gemini_media_engine.GeminiMediaEngine(
            engine._api_key,  # noqa: SLF001 — the factory reads the credential once
            model,
            key_env_var=engine.key_env_var,
            stt_model=stt_model,
            stt_language=stt_language,
            stt_passes=passes,
        )

    return factory


def _describe(payload: dict[str, object], passes: Sequence[int]) -> str:
    lines = [
        f"audio_bytes={payload['audio_bytes']}",
        f"route={'interactions' if gemini_media_engine.resolve_stt_model()[0] else 'generate_content'}",
        f"model={gemini_media_engine.resolve_stt_model()[0] or gemini_media_engine.resolve_media_model()[0]}",
        f"language={gemini_media_engine.resolve_stt_language()[0] or 'auto'}",
        f"consistency={payload['consistency']:.2f}  empty_output_rate={payload['empty_output_rate']:.2f}",
        "",
    ]
    for run in payload["configurations"]:  # type: ignore[union-attr]
        lines.append(
            f"passes={run['passes']} elapsed_ms={run['elapsed_ms']} "
            f"api_calls={run['api_calls']} empty={run['empty_output']} error={run['error'] or '-'}"
        )
        lines.append(f"    transcript: {run['transcript']}")
    quality = payload.get("quality")
    if isinstance(quality, dict):
        lines.append("")
        lines.append("quality against the supplied reference (one sample, not a claim):")
        for label, metrics in quality.items():
            lines.append(
                f"    {label}: WER={metrics['wer']:.3f} CER={metrics['cer']:.3f} "
                f"S={metrics['substitutions']} D={metrics['deletions']} "
                f"I={metrics['insertions']} script={metrics['script']}"
            )
    return "\n".join(lines)


def main(argv: Sequence[str] | None = None) -> int:
    parser = argparse.ArgumentParser(
        prog="python -m backend.tools.stt_benchmark",
        description=(
            "Repeat-run STT benchmark for the bounded multi-pass seam. Requires a "
            "Gemini credential in the environment and a local audio file; it makes "
            "real API calls."
        ),
    )
    parser.add_argument("--audio", required=True, help="path to ONE audio file (ogg/wav/flac)")
    parser.add_argument("--reference", default="", help="file with the human-verified transcript")
    parser.add_argument(
        "--passes", default=",".join(str(value) for value in DEFAULT_PASS_COUNTS),
        help=f"comma-separated pass counts, 1..{gemini_media_engine.STT_MAX_PASSES}",
    )
    parser.add_argument("--repeat", type=int, default=0, help="single-pass repeats (default: 3)")
    parser.add_argument("--json", default="", help="also write the raw JSON report here")
    args = parser.parse_args(argv)

    counts = []
    for token in str(args.passes).split(","):
        token = token.strip()
        if not token:
            continue
        value = int(token)
        if not 1 <= value <= gemini_media_engine.STT_MAX_PASSES:
            raise SystemExit(f"pass count {value} is outside 1..{gemini_media_engine.STT_MAX_PASSES}")
        counts.append(value)
    if not counts:
        raise SystemExit("no pass count given")

    audio = load_audio(args.audio)
    reference = ""
    if args.reference:
        with open(args.reference, encoding="utf-8") as handle:
            reference = handle.read().strip()

    payload = benchmark(
        _engine_factory(), audio, pass_counts=counts, reference=reference,
        repeat=args.repeat,
    )
    print(_describe(payload, counts))
    if args.json:
        with open(args.json, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, ensure_ascii=False, indent=2)
        print(f"\nwrote {args.json}")
    return 0


if __name__ == "__main__":  # pragma: no cover - operator entry point
    sys.exit(main())
