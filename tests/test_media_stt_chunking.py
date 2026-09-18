"""
Media Processing M1.8 — bounded long-audio STT chunking, inside the existing
media boundary.

``MAX_STT_DURATION_S`` used to be the longest audio the boundary would ACCEPT; it
is now the longest audio ONE recognition may be asked to handle, so a longer
recording is divided into ordered, bounded chunks by
``backend/services/stt_chunking.py`` and transcribed one chunk at a time through
the UNCHANGED ``SttEngine.transcribe(audio) -> str`` seam. This suite pins the
contract of that division and nothing else:

  1. the SHORT route is untouched — at or under the ceiling exactly ONE engine call
     happens, on the payload's own bytes, with no decode, re-encode or split;
  2. a longer recording is split at its container's OWN boundaries (OGG pages,
     RIFF/WAVE frames), the chunks partition the source exactly once, and every
     chunk satisfies the per-chunk ceiling;
  3. the transcripts merge into ONE value in strict source order — no second model,
     no LLM merge, no bridging text, no translation, no summary — and the result is
     still ONE ``MediaAnalysis`` with the existing character-ceiling semantics;
  4. ANY failing chunk fails the whole operation; no partial transcript is ever
     returned as a complete one, and no chunk is skipped silently;
  5. the selected engine serves every chunk (never a fallback), each chunk is an
     independent bounded payload, and the engine's own multi-pass behaviour stays
     the engine's — chunks multiply, they do not explode;
  6. every resource bound is finite and enforced: total duration, chunk count,
     aggregate deadline, and no temporary artefact left behind on any exit path;
  7. the zero-context rule is unchanged — no Telegram metadata reaches the engine
     or the model-facing rendering.

Nothing here claims anything about recognition QUALITY: the audio fixtures are
synthetic containers and the engines are scripted. Live Telegram verification of
this phase was NOT performed.
"""
from __future__ import annotations

import ast
import asyncio
import pathlib
import threading
from typing import Any

import pytest

from backend.services import media_service, stt_chunking
from backend.services.gemini_media_engine import (
    DEDICATED_TRANSCRIPTION_MODEL,
    GeminiMediaEngine,
)
from backend.services.media_service import (
    MAX_STT_CHARS,
    MAX_STT_CHUNKS,
    MAX_STT_DURATION_S,
    MAX_STT_TOTAL_DURATION_S,
    MediaError,
)
from tests.test_media_stt import (
    CAPTION,
    CHAT,
    FILE_NAME,
    MESSAGE_ID,
    OWNER,
    _FakeClient,
    _ScriptedEngine,
    _audio_message,
    _flac,
    _media_temp_dirs,
    _ogg_page,
    _opus_head,
    _voice_message,
    _wav,
)
from tests.test_media_stt_reliability import script  # noqa: F401 — the httpx fixture

#: The minimal two-page fixture the boundary's own suite uses declares its WHOLE
#: duration on its only audio page, so it is deliberately indivisible — which is a
#: case this suite pins as an honest refusal. Everything else here uses the shape a
#: real encoder writes: codec headers, then one audio page per interval.
#: The fixture's two codec header pages (granule position 0), which every chunk of
#: a paged note must re-emit unchanged to be decodable on its own.
_OPUS_HEAD_PAGE = _ogg_page(0x02, 0, 0, _opus_head(1, 48_000))
_TAGS_PAGE = _ogg_page(0x00, 0, 1, b"OpusTags" + b"\x00" * 8)
_HEADER_PAGES = _OPUS_HEAD_PAGE + _TAGS_PAGE


def _ogg_opus_paged(duration_s: float, *, seconds_per_page: float = 1.0) -> bytes:
    """A realistic OGG/Opus note: one page per ``seconds_per_page`` of speech."""
    step = max(1, int(round(seconds_per_page * 48_000)))
    total = int(duration_s * 48_000)
    pages = [_OPUS_HEAD_PAGE, _TAGS_PAGE]
    granule = 0
    sequence = 2
    while granule < total:
        granule = min(granule + step, total)
        header_type = 0x04 if granule >= total else 0x00
        pages.append(_ogg_page(header_type, granule, sequence, b"\x00" * 40))
        sequence += 1
    return b"".join(pages)


class _PerChunkEngine:
    """One distinct transcript per call, so chunk ORDER is provable.

    ``fail_at`` makes the N-th call fail, which is how the failure contract is
    tested for the first, a middle and the last chunk of the same recording.
    ``text`` (when set) is returned by every call instead of the per-call label.
    """

    def __init__(self, *, fail_at: int | None = None, text: str = "",
                 error: Exception | None = None) -> None:
        self.calls: list[bytes] = []
        self.fail_at = fail_at
        self.text = text
        self.error = error

    def transcribe(self, audio: bytes) -> str:
        self.calls.append(audio)
        index = len(self.calls)
        if self.fail_at is not None and index == self.fail_at:
            raise self.error or MediaError("scripted chunk failure")
        return self.text or f"chunk {index}"


@pytest.fixture(autouse=True)
def _reset_engine():
    previous = media_service.get_stt_engine()
    media_service.set_stt_engine(None)
    yield
    media_service.set_stt_engine(previous)


def _page_bytes(payload: bytes, *, skip: int = 0) -> list[bytes]:
    """The payload's own OGG pages, as the exact bytes it holds."""
    pages = stt_chunking._ogg_pages(payload)
    assert pages is not None, "the fixture must be a clean OGG page sequence"
    return [payload[page.start:page.end] for page in pages[skip:]]


def _without_flag(payload: bytes, page_index: int, flag: int) -> bytes:
    """The fixture with one OGG header-type flag cleared (no page CRC is read)."""
    pages = stt_chunking._ogg_pages(payload)
    assert pages is not None
    patched = bytearray(payload)
    patched[pages[page_index].start + 5] &= ~flag
    return bytes(patched)


# ── 1. SHORT audio: the single-pass route is untouched ──


@pytest.mark.asyncio
async def test_short_audio_is_one_call_on_its_own_unchanged_bytes():
    engine = _ScriptedEngine("کوتاه")
    media_service.set_stt_engine(engine)
    payload = _ogg_opus_paged(120.0)

    analysis = await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    assert analysis.content == "کوتاه"
    assert len(engine.calls) == 1
    assert engine.calls[0] == payload, "a short note is never split, decoded or re-encoded"


@pytest.mark.asyncio
async def test_exactly_one_chunk_worth_of_audio_stays_on_the_single_pass_route():
    engine = _ScriptedEngine("ok")
    media_service.set_stt_engine(engine)
    payload = _ogg_opus_paged(MAX_STT_DURATION_S)

    analysis = await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    assert analysis.content == "ok"
    assert len(engine.calls) == 1
    assert engine.calls[0] == payload


@pytest.mark.asyncio
async def test_one_second_past_the_bound_starts_the_chunked_route():
    engine = _PerChunkEngine()
    media_service.set_stt_engine(engine)
    payload = _ogg_opus_paged(MAX_STT_DURATION_S + 1.0)

    analysis = await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    assert len(engine.calls) == 2
    assert analysis.content == "chunk 1\nchunk 2"


# ── 2. The chunk plan: container boundaries, exact partition, per-chunk ceiling ──


def test_a_long_note_is_divided_at_ogg_page_boundaries_only():
    source = _ogg_opus_paged(840.0)

    plan = stt_chunking.plan(
        source, "audio/ogg",
        max_chunk_duration_s=MAX_STT_DURATION_S, max_chunks=MAX_STT_CHUNKS,
    )

    assert plan is not None
    assert plan.container == "ogg"
    assert plan.count == 3, "840s at a 300s ceiling is three chunks"
    assert plan.durations[0] == pytest.approx(300.0, abs=0.01)
    assert plan.durations[1] == pytest.approx(300.0, abs=0.01)
    assert plan.durations[2] == pytest.approx(240.0, abs=0.01)
    assert all(duration <= MAX_STT_DURATION_S for duration in plan.durations)
    # Every emitted chunk re-emits the source's OWN codec header pages, without
    # which the middle of a stream is not a decodable Ogg Opus stream at all.
    for index in range(plan.count):
        assert plan.chunk(index).startswith(_OPUS_HEAD_PAGE)


def test_the_chunks_partition_the_source_pages_exactly_once():
    source = _ogg_opus_paged(840.0)
    plan = stt_chunking.plan(
        source, "audio/ogg",
        max_chunk_duration_s=MAX_STT_DURATION_S, max_chunks=MAX_STT_CHUNKS,
    )
    assert plan is not None

    source_pages = _page_bytes(source, skip=2)
    seen: list[bytes] = []
    for index in range(plan.count):
        chunk = plan.chunk(index)
        assert chunk.startswith(_HEADER_PAGES)
        seen.extend(_page_bytes(chunk, skip=2))

    assert seen == source_pages, "no page is duplicated, dropped or cut in half"
    assert plan.chunk(0) != plan.chunk(1), "chunks are distinct payloads"


def test_a_wav_is_divided_on_frame_boundaries_and_stays_valid_per_chunk():
    source = _wav(601.0, sample_rate=8_000)

    plan = stt_chunking.plan(
        source, "audio/wav",
        max_chunk_duration_s=MAX_STT_DURATION_S, max_chunks=MAX_STT_CHUNKS,
    )

    assert plan is not None and plan.count == 3, "601s at a 300s ceiling is three chunks"
    for index in range(plan.count):
        chunk = plan.chunk(index)
        # A WAVE chunk is a complete RIFF stream with its OWN honest header, so it
        # can be re-validated by the boundary's own reader.
        channels, rate, duration = media_service._validate_audio_payload(chunk, "audio/wav")
        assert (channels, rate) == (1, 8_000)
        assert 0 < duration <= MAX_STT_DURATION_S
        assert len(chunk) <= len(source)


def test_the_source_bytes_are_reused_verbatim_not_re_encoded():
    source = _ogg_opus_paged(420.0)
    plan = stt_chunking.plan(
        source, "audio/ogg",
        max_chunk_duration_s=MAX_STT_DURATION_S, max_chunks=MAX_STT_CHUNKS,
    )
    assert plan is not None

    # Every byte a chunk carries is a byte of the source: the header pages are
    # re-emitted unchanged and each body page is copied whole, so nothing was
    # decoded, re-encoded, or rebuilt from parts.
    source_pages = set(_page_bytes(source, skip=2))
    for index in range(plan.count):
        chunk = plan.chunk(index)
        assert chunk.startswith(_HEADER_PAGES)
        for page in _page_bytes(chunk, skip=2):
            assert page in source_pages


def test_the_splitter_refuses_anything_it_cannot_divide_cleanly():
    assert stt_chunking.plan(b"", "audio/ogg", max_chunk_duration_s=300.0, max_chunks=4) is None
    assert stt_chunking.plan(b"RIFF", "audio/mpeg", max_chunk_duration_s=300.0, max_chunks=4) is None
    # FLAC frames cannot be found without decoding subframes, and a re-headed FLAC
    # would need STREAMINFO rewritten: refused, never approximated.
    assert stt_chunking.plan(
        _flac(900.0), "audio/flac", max_chunk_duration_s=300.0, max_chunks=4,
    ) is None
    # One page that alone declares the whole duration is indivisible.
    assert stt_chunking.plan(
        _ogg_page(0x02, 0, 0, _opus_head(1, 48_000))
        + _ogg_page(0x04, 330 * 48_000, 1, b"\x00" * 40),
        "audio/ogg", max_chunk_duration_s=300.0, max_chunks=4,
    ) is None
    # Trailing bytes: not a clean single stream.
    assert stt_chunking.plan(
        _ogg_opus_paged(400.0) + b"garbage",
        "audio/ogg", max_chunk_duration_s=300.0, max_chunks=4,
    ) is None
    # More chunks than the cap allows.
    assert stt_chunking.plan(
        _ogg_opus_paged(1200.0), "audio/ogg",
        max_chunk_duration_s=MAX_STT_DURATION_S, max_chunks=3,
    ) is None


def test_the_splitter_requires_the_identification_header_where_it_must_be():
    source = _ogg_opus_paged(400.0)
    divisible = dict(max_chunk_duration_s=MAX_STT_DURATION_S, max_chunks=MAX_STT_CHUNKS)
    assert stt_chunking.plan(source, "audio/ogg", **divisible) is not None

    # Without the BOS page the first page is not the identification header, so the
    # codec and its granule rate cannot be established and nothing is divided.
    assert stt_chunking.plan(_without_flag(source, 0, 0x02), "audio/ogg", **divisible) is None
    # A missing end-of-stream page is NOT a refusal: every chunk boundary is a page
    # boundary the stream declares, so a truncated payload still divides honestly.
    assert stt_chunking.plan(_without_flag(source, -1, 0x04), "audio/ogg", **divisible) is not None


# ── 3. Ordering, merging and the output contract ──


@pytest.mark.parametrize("duration,chunks", [
    (301.0, 2), (480.0, 2), (840.0, 3), (1200.0, 4),
])
@pytest.mark.asyncio
async def test_a_long_recording_merges_its_chunks_in_source_order(duration, chunks):
    engine = _PerChunkEngine()
    media_service.set_stt_engine(engine)
    payload = _ogg_opus_paged(duration)

    analysis = await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    assert len(engine.calls) == chunks
    assert analysis.content == "\n".join(f"chunk {index}" for index in range(1, chunks + 1))
    assert analysis.status == media_service.MediaStatus.EXTRACTED.value
    assert analysis.truncated is False
    expected = stt_chunking.plan(
        payload, "audio/ogg",
        max_chunk_duration_s=MAX_STT_DURATION_S, max_chunks=MAX_STT_CHUNKS,
    )
    assert expected is not None and expected.count == chunks
    assert engine.calls == [expected.chunk(index) for index in range(chunks)]


@pytest.mark.asyncio
async def test_the_merged_transcript_is_one_normalized_value():
    engine = _PerChunkEngine(text="  سلام   دنیا  \n\n\n پایان ")
    media_service.set_stt_engine(engine)
    payload = _ogg_opus_paged(400.0)

    analysis = await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    # ONE analysis rendered by the EXISTING normalization: horizontal runs and
    # blank-line runs collapse exactly as they always have (a single blank line is
    # kept — that is the documented rule), and the chunk join adds one newline and
    # nothing else.
    assert analysis.content == "سلام دنیا\n\nپایان\nسلام دنیا\n\nپایان"
    assert "\n\n\n" not in analysis.content


@pytest.mark.asyncio
async def test_the_existing_output_ceiling_stays_honest_for_a_chunked_transcript():
    engine = _PerChunkEngine(text="ا" * (MAX_STT_CHARS // 2))
    media_service.set_stt_engine(engine)
    payload = _ogg_opus_paged(840.0)

    analysis = await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    # The audio itself WAS fully transcribed; only the presented text hits the
    # project's prompt ceiling, and that is reported instead of hidden.
    assert analysis.truncated is True
    assert len(analysis.content) <= MAX_STT_CHARS
    assert analysis.content.endswith("…")
    assert "truncated at the processing limit" in analysis.as_context_text()


@pytest.mark.asyncio
async def test_the_chunked_route_keeps_the_zero_context_rule():
    engine = _PerChunkEngine()
    media_service.set_stt_engine(engine)
    payload = _ogg_opus_paged(400.0)

    analysis = await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    rendered = analysis.as_context_text()
    for forbidden in (CAPTION, FILE_NAME, str(CHAT), str(MESSAGE_ID), "OpusTags"):
        assert forbidden not in rendered
    assert rendered == (
        "[Media Content]\nType: Voice\nMIME: audio/ogg\n"
        f"Size: {len(payload) / 1024:.1f} KB\nStatus: extracted\n"
        "Content:\nchunk 1\nchunk 2"
    )


def test_the_chunk_transcripts_join_without_inventing_anything():
    assert stt_chunking.join_transcripts(["a", "b", "c"]) == "a\nb\nc"
    # A chunk that heard nothing contributes no text: silence is not a gap marker.
    assert stt_chunking.join_transcripts(["a", "", "", "b"]) == "a\nb"
    assert stt_chunking.join_transcripts(["", ""]) == ""
    assert stt_chunking.join_transcripts([]) == ""
    assert stt_chunking.TRANSCRIPT_SEPARATOR == "\n"


# ── 4. Failure: any chunk, no partial result, no silent skip ──


@pytest.mark.parametrize("fail_at", [1, 2, 3])
@pytest.mark.asyncio
async def test_any_failing_chunk_fails_the_whole_operation(fail_at):
    engine = _PerChunkEngine(fail_at=fail_at)
    media_service.set_stt_engine(engine)
    payload = _ogg_opus_paged(840.0)

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(
            _FakeClient(payload=payload), OWNER, _voice_message(payload),
        )

    assert "scripted chunk failure" in str(error.value)
    # No partial transcript exists anywhere: the operation raised instead of
    # returning the chunks that happened to succeed, and no later chunk was
    # attempted after the failure.
    assert len(engine.calls) == fail_at


@pytest.mark.asyncio
async def test_a_failing_engine_is_never_retried_on_another_provider():
    engine = _PerChunkEngine(fail_at=2)
    media_service.set_stt_engine(engine)
    payload = _ogg_opus_paged(840.0)

    with pytest.raises(MediaError):
        await media_service.analyze_media(
            _FakeClient(payload=payload), OWNER, _voice_message(payload),
        )

    # The ONE selected engine served every attempted chunk and its failure
    # surfaced; nothing consulted a second engine and no retry loop ran.
    assert len(engine.calls) == 2
    assert media_service.get_stt_engine() is engine


@pytest.mark.asyncio
async def test_an_indivisible_over_long_container_is_refused_honestly():
    engine = _ScriptedEngine("never reached")
    media_service.set_stt_engine(engine)
    payload = _ogg_opus_paged(400.0, seconds_per_page=400.0)

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(
            _FakeClient(payload=payload), OWNER, _voice_message(payload),
        )

    assert "speech-to-text bound" in str(error.value)
    assert "cannot be divided" in str(error.value)
    assert error.value.stage == media_service.MEDIA_STAGE_VALIDATION
    assert engine.calls == []


@pytest.mark.asyncio
async def test_a_long_flac_is_refused_rather_than_split_on_a_guess():
    engine = _ScriptedEngine("never reached")
    media_service.set_stt_engine(engine)
    payload = _flac(900.0)

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(
            _FakeClient(payload=payload), OWNER, _audio_message(payload, mime="audio/flac"),
        )

    assert "cannot be divided" in str(error.value)
    assert engine.calls == []


# ── 5. Bounds: total duration, chunk count, aggregate deadline ──


@pytest.mark.asyncio
async def test_audio_beyond_the_total_duration_bound_is_refused_before_transcription():
    engine = _ScriptedEngine("never reached")
    media_service.set_stt_engine(engine)
    payload = _ogg_opus_paged(MAX_STT_TOTAL_DURATION_S + 60.0)
    client = _FakeClient(payload=payload)

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert f"{MAX_STT_TOTAL_DURATION_S:.0f}s speech-to-text bound" in str(error.value)
    assert engine.calls == []


def test_the_total_bound_is_derived_from_the_per_chunk_ceiling():
    assert MAX_STT_CHUNKS == 4
    assert MAX_STT_TOTAL_DURATION_S == MAX_STT_CHUNKS * MAX_STT_DURATION_S == 1200.0
    assert media_service.STT_TOTAL_TIMEOUT_S == 120.0


@pytest.mark.asyncio
async def test_the_chunk_count_cap_is_enforced_by_the_route(monkeypatch):
    # Duration itself stays inside the validated total bound: it is the CHUNK CAP
    # that refuses the payload, so the two ceilings are separately enforced.
    monkeypatch.setattr(media_service, "MAX_STT_CHUNKS", 2)
    engine = _PerChunkEngine()
    media_service.set_stt_engine(engine)
    payload = _ogg_opus_paged(840.0)

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(
            _FakeClient(payload=payload), OWNER, _voice_message(payload),
        )

    assert "cannot be divided" in str(error.value)
    assert engine.calls == []


@pytest.mark.asyncio
async def test_a_spent_aggregate_deadline_fails_the_operation(monkeypatch):
    monkeypatch.setattr(media_service, "STT_TOTAL_TIMEOUT_S", 0.0001)
    engine = _PerChunkEngine()
    media_service.set_stt_engine(engine)
    payload = _ogg_opus_paged(840.0)

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(
            _FakeClient(payload=payload), OWNER, _voice_message(payload),
        )

    assert error.value.stage in (
        media_service.MEDIA_STAGE_STT_TIMEOUT, media_service.MEDIA_STAGE_STT_ENGINE,
    )
    # The failure is the aggregate budget's, never a partial transcript.
    assert len(engine.calls) <= 1


@pytest.mark.asyncio
async def test_the_per_chunk_timeout_is_the_smaller_of_the_bound_and_the_budget(monkeypatch):
    timeouts: list[float] = []
    real = media_service._run_stt

    async def wrapper(engine, data, timeout_s, request_id=""):
        timeouts.append(timeout_s)
        return await real(engine, data, timeout_s, request_id=request_id)

    monkeypatch.setattr(media_service, "_run_stt", wrapper)
    monkeypatch.setattr(media_service, "STT_TOTAL_TIMEOUT_S", 75.0)
    engine = _PerChunkEngine()
    media_service.set_stt_engine(engine)
    payload = _ogg_opus_paged(840.0)

    await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    assert len(timeouts) == 3
    assert all(0 < timeout <= min(media_service.STT_TIMEOUT_S, 75.0) for timeout in timeouts)


# ── 6. Resources: off-loop, cleanup on every exit path ──


@pytest.mark.asyncio
async def test_the_container_division_runs_off_the_event_loop(monkeypatch):
    threads: list[str] = []
    real = stt_chunking.plan

    def wrapper(*args: Any, **kwargs: Any):
        threads.append(threading.current_thread().name)
        return real(*args, **kwargs)

    monkeypatch.setattr(stt_chunking, "plan", wrapper)
    engine = _PerChunkEngine()
    media_service.set_stt_engine(engine)
    payload = _ogg_opus_paged(400.0)

    await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    assert threads, "the chunk plan must be built"
    assert threads[0] != threading.main_thread().name


@pytest.mark.asyncio
async def test_no_temporary_artefact_is_left_behind_on_success_or_failure():
    payload = _ogg_opus_paged(400.0)
    before = _media_temp_dirs()

    media_service.set_stt_engine(_PerChunkEngine())
    await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )
    assert _media_temp_dirs() == before

    media_service.set_stt_engine(_PerChunkEngine(fail_at=2))
    with pytest.raises(MediaError):
        await media_service.analyze_media(
            _FakeClient(payload=payload), OWNER, _voice_message(payload),
        )
    assert _media_temp_dirs() == before


@pytest.mark.asyncio
async def test_cancelling_a_chunked_transcription_cleans_up_and_propagates():
    media_service.set_stt_engine(_ScriptedEngine("slow", delay=1.0))
    payload = _ogg_opus_paged(840.0)
    before = _media_temp_dirs()

    task = asyncio.create_task(
        media_service.analyze_media(
            _FakeClient(payload=payload), OWNER, _voice_message(payload),
        )
    )
    await asyncio.sleep(0.2)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _media_temp_dirs() == before


# ── 7. Multi-pass: the engine's own seam stays the engine's ──


@pytest.mark.asyncio
async def test_a_three_pass_engine_multiplies_per_chunk_and_stays_bounded(script):
    transport = script()
    engine = GeminiMediaEngine(
        "chunking-suite-key-not-a-credential", "media-model",
        key_env_var="AI_GEMINI_API_KEY",
        stt_model=DEDICATED_TRANSCRIPTION_MODEL, stt_passes=3,
    )
    media_service.set_stt_engine(engine)
    payload = _ogg_opus_paged(400.0)

    analysis = await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _voice_message(payload),
    )

    plan = stt_chunking.plan(
        payload, "audio/ogg",
        max_chunk_duration_s=MAX_STT_DURATION_S, max_chunks=MAX_STT_CHUNKS,
    )
    assert plan is not None and plan.count == 2
    # chunks x passes: the boundary calls the engine once per CHUNK and the
    # engine's own consensus seam runs its configured passes inside that call, so
    # the multiplication is bounded by two caps that already exist.
    assert len(transport.leg_requests("upload_finalize")) == plan.count
    assert len(transport.leg_requests("interaction")) == plan.count * 3
    assert [request.content for request in transport.leg_requests("upload_finalize")] == [
        plan.chunk(index) for index in range(plan.count)
    ]
    assert len(transport.deleted) == plan.count
    assert analysis.content.count("\n") == plan.count - 1


# ── 8. The route never touches an unrelated extraction path ──


@pytest.mark.asyncio
async def test_a_long_wav_reaches_the_chunked_route_end_to_end():
    engine = _PerChunkEngine()
    media_service.set_stt_engine(engine)
    payload = _wav(301.0, sample_rate=4_000)

    analysis = await media_service.analyze_media(
        _FakeClient(payload=payload), OWNER, _audio_message(payload),
    )

    assert len(engine.calls) == 2
    assert analysis.content == "chunk 1\nchunk 2"
    assert all(len(chunk) < len(payload) for chunk in engine.calls)
    # Each payload the engine received is a complete WAVE stream that the
    # boundary's own reader accepts, with an honest duration of its own.
    for chunk in engine.calls:
        _channels, _rate, duration = media_service._validate_audio_payload(chunk, "audio/wav")
        assert 0 < duration <= MAX_STT_DURATION_S


def test_the_splitter_is_standard_library_only():
    # No decoder, no ffmpeg, no pydub, no per-format native stack: the division is
    # container framing and byte ranges, nothing more.
    source = pathlib.Path(stt_chunking.__file__).read_text(encoding="utf-8")
    imported: set[str] = set()
    for node in ast.walk(ast.parse(source)):
        if isinstance(node, ast.Import):
            imported.update(alias.name.split(".")[0] for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.add(node.module.split(".")[0])
    assert imported == {"__future__", "typing"}
    for forbidden in ("subprocess", "ffmpeg", "pydub", "soundfile", "numpy"):
        assert forbidden not in source
