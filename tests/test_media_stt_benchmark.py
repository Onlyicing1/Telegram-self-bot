"""
Media Processing — the repeat-run STT BENCHMARK harness (``backend/tools/stt_benchmark.py``).

The harness itself is an operator tool: it makes real API calls and is never
imported by the bot. Its MEASUREMENT code, however, is pure and injectable, so
this suite covers it hermetically — no credential, no network, no audio:

  * the metric functions (edit counts, WER, CER, script buckets, repeat-run
    consistency) over known inputs, including Persian;
  * the API-call accounting, which must count real ``httpx`` requests per leg;
  * one configuration run and the full multi-configuration benchmark over a
    SCRIPTED engine, so the report shape, the pass counts requested and the
    failure recording are pinned without any live call;
  * the local-file loader's bounds (empty file, unsupported container, size).

Nothing here produces a quality number, and nothing here may claim one: the WER
of a scripted engine's fabricated strings is an arithmetic property of the metric,
not a recognition result.
"""
from __future__ import annotations

import inspect
import json

import httpx
import pytest

from backend.services import media_service
from backend.services.media_service import MediaError
from backend.tools import stt_benchmark
from backend.tools.stt_benchmark import (
    ApiCallCounter,
    PassRun,
    _leg_name,
    benchmark,
    character_error_rate,
    edit_counts,
    load_audio,
    repeat_run_consistency,
    run_pass_set,
    script_counts,
    word_error_rate,
)
from tests.test_media_stt_reliability import _wav

PERSIAN = "دیدم اتفاقا تو گپ چیز باحالیه خلاصه چت"


class _FakeEngine:
    """A scripted engine: no HTTP, one text per call, optional failure."""

    def __init__(self, texts: list[str], *, raises: Exception | None = None) -> None:
        self.texts = list(texts)
        self.raises = raises
        self.calls = 0

    def transcribe(self, audio: bytes) -> str:
        self.calls += 1
        if self.raises is not None:
            raise self.raises
        return self.texts.pop(0) if self.texts else ""


# ── 1. The metrics ──


def test_identical_texts_error_free():
    assert edit_counts(["a", "b"], ["a", "b"]) == (0, 0, 0)
    assert word_error_rate("یک دو سه", "یک دو سه") == 0.0
    assert character_error_rate("سلام دنیا", "سلام دنیا") == 0.0


def test_a_substitution_is_counted_once():
    assert edit_counts(["یک", "دو", "سه"], ["یک", "دو", "چهار"]) == (1, 0, 0)
    assert word_error_rate("یک دو سه", "یک دو چهار") == pytest.approx(1 / 3)


def test_a_deletion_and_an_insertion_are_distinguished():
    assert edit_counts(["یک", "دو", "سه"], ["یک", "سه"]) == (0, 1, 0)
    assert edit_counts(["یک", "سه"], ["یک", "دو", "سه"]) == (0, 0, 1)


def test_mixed_edits_are_all_reported():
    substitutions, deletions, insertions = edit_counts(
        ["a", "b", "c", "d"], ["a", "x", "c"],
    )

    assert (substitutions, deletions, insertions) == (1, 1, 0)


def test_the_error_rate_is_errors_over_reference_length():
    assert word_error_rate("a b c d", "a b c d") == 0.0
    assert word_error_rate("a b c d", "a x c d") == pytest.approx(0.25)


def test_cer_ignores_spacing_differences():
    assert character_error_rate("یک دو", "یک  دو") == 0.0
    assert character_error_rate("یک دو", "یک  سه") > 0.0


def test_a_persian_transcript_is_mostly_arabic_script():
    counts = script_counts(PERSIAN)

    assert counts["arabic"] > counts["latin"]
    assert counts["other"] == 0


def test_a_romanized_transcript_is_mostly_latin_script():
    """The one failure that needs no reference: non-Latin speech in Latin letters."""
    counts = script_counts("didam etefagha to gap chiz bahaliye")

    assert counts["latin"] > counts["arabic"]


def test_consistency_is_the_share_of_the_most_common_transcript():
    assert repeat_run_consistency(["a", "a", "a"]) == 1.0
    assert repeat_run_consistency(["a", "a", "b"]) == pytest.approx(2 / 3)
    assert repeat_run_consistency(["a", "b", "c"]) == pytest.approx(1 / 3)
    assert repeat_run_consistency([]) == 0.0


# ── 2. API-call accounting ──


@pytest.mark.parametrize(
    "url,method,expected",
    [
        ("https://x/upload/v1beta/files", "POST", "upload_start"),
        ("https://upload.example/files?upload_id=1", "POST", "upload_finalize"),
        ("https://x/v1beta/files/abc", "DELETE", "delete"),
        ("https://x/v1beta/files/abc", "GET", "file_status"),
        ("https://x/v1beta/interactions", "POST", "interaction"),
        ("https://x/v1beta/models/m:generateContent", "POST", "generate"),
    ],
)
def test_every_leg_of_the_engine_is_named(url, method, expected):
    assert _leg_name(url, method) == expected


def test_the_counter_counts_real_requests_per_leg():
    def handler(request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={}, request=request)

    with ApiCallCounter() as counter:
        with httpx.Client(transport=httpx.MockTransport(handler)) as client:
            client.get("https://x/v1beta/files/abc")
            client.delete("https://x/v1beta/files/abc")
            client.post("https://x/v1beta/interactions", json={})

    assert counter.snapshot() == {"delete": 1, "file_status": 1, "interaction": 1}


def test_the_counter_restores_httpx_when_it_leaves():
    original = httpx.Client

    with ApiCallCounter():
        assert httpx.Client is not original

    assert httpx.Client is original


# ── 3. One configuration, and the whole benchmark, over a scripted engine ──


def test_the_benchmark_defaults_to_the_single_pass_and_three():
    assert stt_benchmark.DEFAULT_PASS_COUNTS == (1, 3)
    assert max(stt_benchmark.DEFAULT_PASS_COUNTS) <= 3


def test_a_pass_run_reports_text_timing_and_call_counts():
    run = run_pass_set(lambda passes: _FakeEngine([PERSIAN]), _wav(), 3)

    assert isinstance(run, PassRun)
    assert run.passes == 3
    assert run.transcript == PERSIAN
    assert run.elapsed_ms >= 0
    assert run.api_calls == {}, "a scripted engine makes no HTTP request"
    assert run.error == ""


def test_a_failed_run_is_recorded_as_data_not_raised():
    run = run_pass_set(lambda passes: _FakeEngine([], raises=MediaError("boom")), _wav(), 1)

    assert run.transcript == ""
    assert run.error.startswith("MediaError: boom")
    assert run.empty


def test_the_benchmark_runs_every_configured_pass_count_and_its_repeats():
    requested: list[int] = []

    def factory(passes: int) -> _FakeEngine:
        requested.append(passes)
        return _FakeEngine([f"passes-{passes}"])

    payload = benchmark(factory, b"audio", pass_counts=[1, 2, 3], repeat=4)

    assert requested == [1, 2, 3, 1, 1, 1, 1]
    assert [run["passes"] for run in payload["configurations"]] == [1, 2, 3]
    assert [run["transcript"] for run in payload["configurations"]] == [
        "passes-1", "passes-2", "passes-3",
    ]
    assert len(payload["repeat_runs"]) == 4
    assert payload["consistency"] == 1.0
    assert payload["empty_output_rate"] == 0.0
    assert payload["audio_bytes"] == 5


def test_the_benchmark_reports_a_reference_based_quality_block():
    def factory(passes: int) -> _FakeEngine:
        return _FakeEngine([PERSIAN])

    payload = benchmark(factory, b"audio", pass_counts=[1, 3], reference=PERSIAN, repeat=1)

    quality = payload["quality"]
    assert set(quality) == {"passes=1", "passes=3"}
    assert quality["passes=1"]["wer"] == 0.0
    assert quality["passes=1"]["substitutions"] == 0
    assert quality["passes=1"]["script"]["arabic"] > 0


def test_no_quality_block_is_invented_without_a_reference():
    payload = benchmark(lambda passes: _FakeEngine([PERSIAN]), b"audio", repeat=1)

    assert "quality" not in payload
    assert payload["reference"] == ""


def test_an_empty_output_is_reported_as_empty():
    payload = benchmark(lambda passes: _FakeEngine([""]), b"audio", pass_counts=[1, 2, 3])

    assert payload["empty_output_rate"] == 1.0
    assert all(run["empty_output"] for run in payload["configurations"])


def test_the_json_report_is_serializable():
    payload = benchmark(lambda passes: _FakeEngine([PERSIAN]), b"audio", reference=PERSIAN)

    assert json.loads(json.dumps(payload, ensure_ascii=False))["consistency"] == 1.0


def test_the_runner_never_wait_for_an_engine_that_returns_a_non_string():
    run = run_pass_set(lambda passes: _FakeEngine([]), _wav(), 1)

    assert run.transcript == ""


# ── 4. The local-file loader and its bounds ──


def test_loading_a_real_container_returns_its_bytes(tmp_path):
    path = tmp_path / "voice.ogg"
    payload = _wav(0.2)
    path.write_bytes(payload)

    assert load_audio(str(path)) == payload


def test_an_empty_file_is_refused(tmp_path):
    path = tmp_path / "empty.ogg"
    path.write_bytes(b"")

    with pytest.raises(ValueError):
        load_audio(str(path))


def test_a_non_audio_container_is_refused(tmp_path):
    path = tmp_path / "image.png"
    path.write_bytes(b"\x89PNG\r\n\x1a\n" + b"\x00" * 32)

    with pytest.raises(ValueError):
        load_audio(str(path))


def test_an_undocumented_container_is_refused(tmp_path):
    path = tmp_path / "image.bmp"
    path.write_bytes(b"BM" + b"\x00" * 32)

    with pytest.raises(MediaError):
        load_audio(str(path))


def test_a_file_past_the_stt_bound_is_refused(tmp_path, monkeypatch):
    monkeypatch.setattr(media_service, "MAX_STT_INPUT_BYTES", 16)
    path = tmp_path / "voice.ogg"
    path.write_bytes(_wav(0.2))

    with pytest.raises(ValueError):
        load_audio(str(path))


# ── 5. The tool's own scope ──


def test_the_harness_is_not_wired_into_the_runtime():
    source = inspect.getsource(stt_benchmark)

    assert "telethon" not in source and "ProviderManager" not in source
    assert "backend.bot" not in source
