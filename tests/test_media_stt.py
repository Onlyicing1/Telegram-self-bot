"""
Media Processing M1.4 — the bounded speech-to-text boundary for Voice/Audio.

The M1 boundary (``backend/services/media_service.py``) is unchanged in shape;
this file pins the M1.4 addition to it:

  1. STT is a SEAM, not a dependency. Transcription is supplied through
     ``SttEngine`` and provisioned with ``set_stt_engine``; this phase ships no
     engine, so Voice and Audio are reported ``UNSUPPORTED`` *without* being
     transferred and the project never silently acquires a heavy native stack or
     an undisclosed model artifact.
  2. Every bound is finite and enforced at the point of use: the STT input bound
     (applied BEFORE the transfer), the MIME/container signature match, the
     channel / sample-rate / duration guards (all pre-decode), the transcription
     timeout, and the shared character ceiling.
  3. The declared duration is derived from the container itself: the OGG page
     granule position, the RIFF/WAVE ``fmt `` + ``data`` headers, or FLAC's
     STREAMINFO total-sample count — never guessed, and an indeterminate stream
     is refused rather than transcribed unbounded.
  4. Transcription runs off the event loop (the same ``asyncio.to_thread``
     pattern M1.2 uses for document parsing) and never blocks it.
  5. Honest outcomes only: no speech reports empty content with a reason; a
     malformed/mismatched container, an over-bound stream, a timeout and an
     engine failure all raise ``MediaError`` instead of fabricated text.
  6. The model-facing rendering (``as_context_text``) still carries no caption,
     sender, chat id, message id or filename.

No live Telegram, no network and no STT dependency: the Telegram boundary is a
scripted fake shaped like the Telethon client surface the facade consumes, the
media types are real ``telethon.tl.types`` objects so the existing classifier
runs unmodified, and the audio fixtures are real container bytes built in
process (a genuine RIFF/WAVE stream via the stdlib ``wave`` module, and
byte-accurate OGG and FLAC containers).
"""
from __future__ import annotations

import asyncio
import io
import os
import tempfile
import threading
import time
import wave
from typing import Any

import pytest
from telethon.tl.types import (
    Document,
    DocumentAttributeAudio,
    DocumentAttributeFilename,
    MessageMediaDocument,
    MessageMediaPhoto,
    Photo,
    PhotoSize,
)

from backend.services import media_service
from backend.services.media_service import (
    MAX_EXTRACTED_CHARS,
    MAX_STT_CHARS,
    MAX_STT_CHANNELS,
    MAX_STT_DURATION_S,
    MAX_STT_INPUT_BYTES,
    MAX_STT_SAMPLE_RATE,
    MediaError,
    MediaStatus,
)

OWNER = 7770001
CHAT = -1007778889999
MESSAGE_ID = 574942
CAPTION = "caption-that-must-never-reach-the-model"
FILE_NAME = "note.ogg"

#: One sample rate above the project's STT sample-rate bound.
MIN_ABOVE_RATE = MAX_STT_SAMPLE_RATE + 8_000

_PERSIAN = "سلام، این یک آزمایش است"
_ZWNJ = "\u200c"
_PERSIAN_ZWNJ = "می" + _ZWNJ + "کند"
_ENGLISH = "Hello, this is a test transcript"
_MIXED = "این متن فارسی است\nand this line is English 12345"


# ── Fake Telegram surface (mirrors tests/test_media_processing.py) ──


class _FakeMessage:
    """The attribute surface ``backend/ai/media.classify_message`` inspects."""

    def __init__(self, media: Any, *, caption: str = CAPTION, mid: int = MESSAGE_ID,
                 chat_id: int = CHAT) -> None:
        self.media = media
        self.message = caption
        self.text = caption
        self.id = mid
        self.chat_id = chat_id


class _FakeClient:
    """A scripted, bounded-transfer-shaped Telethon client."""

    def __init__(self, payload: bytes = b"", *, error: Exception | None = None) -> None:
        self.payload = payload
        self.error = error
        self.calls: list[dict[str, Any]] = []

    async def download_media(self, message: Any, file: Any = None,
                             progress_callback: Any = None, **kwargs: Any) -> Any:
        self.calls.append({"op": "download_media", "file": file})
        if self.error is not None:
            raise self.error
        if file:
            with open(file, "wb") as handle:
                handle.write(self.payload)
            return file
        return None

    async def get_messages(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        self.calls.append({"op": "get_messages"})
        raise AssertionError("the media boundary must never search for a message")

    async def iter_messages(self, *args: Any, **kwargs: Any) -> Any:  # pragma: no cover
        self.calls.append({"op": "iter_messages"})
        raise AssertionError("the media boundary must never scan for a message")


def _media_temp_dirs() -> set[str]:
    try:
        entries = os.listdir(tempfile.gettempdir())
    except OSError:  # pragma: no cover - defensive
        return set()
    return {name for name in entries if name.startswith("lifeos_media_")}


# ── Real audio fixtures (stdlib encoders, no dependency) ──


def _opus_head(channels: int = 1, sample_rate: int = 48_000) -> bytes:
    """A 19-byte Opus identification header (Ogg Opus spec)."""
    return (
        b"OpusHead" + bytes([1, channels])
        + (312).to_bytes(2, "little") + sample_rate.to_bytes(4, "little")
        + (0).to_bytes(2, "little") + bytes([0])
    )


def _vorbis_head(channels: int = 1, sample_rate: int = 44_100) -> bytes:
    """A Vorbis identification header, which declares rate at offset 12."""
    return (
        b"\x01vorbis" + (0).to_bytes(4, "little") + bytes([channels])
        + sample_rate.to_bytes(4, "little")
    )


def _ogg_page(header_type: int, granule: int, sequence: int, packet: bytes) -> bytes:
    lacing: list[int] = []
    remaining = len(packet)
    while remaining >= 255:
        lacing.append(255)
        remaining -= 255
    lacing.append(remaining)
    return (
        b"OggS" + bytes([0, header_type])
        + granule.to_bytes(8, "little")
        + (1).to_bytes(4, "little") + sequence.to_bytes(4, "little")
        + (0).to_bytes(4, "little")
        + bytes([len(lacing)]) + bytes(lacing) + packet
    )


def _ogg_opus(duration_s: float, *, channels: int = 1, sample_rate: int = 48_000) -> bytes:
    """A structurally valid OGG/Opus stream whose granule position IS the duration."""
    granule = int(duration_s * 48_000)
    return (
        _ogg_page(0x02, 0, 0, _opus_head(channels, sample_rate))
        + _ogg_page(0x04, granule, 1, b"\x00" * 40)
    )


def _ogg_vorbis(duration_s: float, *, channels: int = 1, sample_rate: int = 44_100) -> bytes:
    granule = int(duration_s * sample_rate)
    return (
        _ogg_page(0x02, 0, 0, _vorbis_head(channels, sample_rate))
        + _ogg_page(0x04, granule, 1, b"\x00" * 40)
    )


def _wav(duration_s: float, *, channels: int = 1, sample_rate: int = 16_000) -> bytes:
    """A genuinely valid RIFF/WAVE stream written by the stdlib ``wave`` module."""
    buffer = io.BytesIO()
    handle = wave.open(buffer, "wb")
    handle.setnchannels(channels)
    handle.setsampwidth(2)
    handle.setframerate(sample_rate)
    handle.writeframes(b"\x00\x00" * int(duration_s * sample_rate) * channels)
    handle.close()
    return buffer.getvalue()


def _flac(duration_s: float, *, channels: int = 1, sample_rate: int = 16_000) -> bytes:
    """A FLAC stream whose STREAMINFO block declares rate, channels and samples."""
    total_samples = int(duration_s * sample_rate)
    packed = (sample_rate << 44) | ((channels - 1) << 41) | total_samples
    streaminfo = b"\x00" * 10 + packed.to_bytes(8, "big") + b"\x00" * 16
    return b"fLaC" + b"\x00" + (34).to_bytes(3, "big") + streaminfo


def _flac_without_total_samples() -> bytes:
    packed = (16_000 << 44) | (0 << 41) | 0  # total_samples = 0 means "unknown"
    streaminfo = b"\x00" * 10 + packed.to_bytes(8, "big") + b"\x00" * 16
    return b"fLaC" + b"\x00" + (34).to_bytes(3, "big") + streaminfo


def _voice_message(payload: bytes, *, mime: str = "audio/ogg",
                   duration: int = 2, name: str = FILE_NAME) -> _FakeMessage:
    """A ``Voice`` message (classifier: voice=True -> media_type ``Voice``)."""
    doc = Document(
        id=1, access_hash=1, file_reference=b"", date=None, mime_type=mime,
        size=len(payload), dc_id=1,
        attributes=[DocumentAttributeAudio(duration=duration, voice=True)],
    )
    return _FakeMessage(MessageMediaDocument(document=doc))


def _audio_message(payload: bytes, *, mime: str = "audio/wav",
                   name: str = "track.wav", size: int | None = None) -> _FakeMessage:
    """An ``Audio`` message (classifier: voice=False -> media_type ``Audio``)."""
    doc = Document(
        id=2, access_hash=2, file_reference=b"", date=None, mime_type=mime,
        size=len(payload) if size is None else size, dc_id=1,
        attributes=[
            DocumentAttributeAudio(duration=2, voice=False),
            DocumentAttributeFilename(file_name=name),
        ],
    )
    return _FakeMessage(MessageMediaDocument(document=doc))


# ── Deterministic scripted engines (the seam, not the pipeline) ──


class _ScriptedEngine:
    """Returns a fixed string; records how and on which thread it was called."""

    def __init__(self, text: str = "", *, error: Exception | None = None,
                 delay: float = 0.0) -> None:
        self.text = text
        self.error = error
        self.delay = delay
        self.calls: list[bytes] = []
        self.thread_names: list[str] = []

    def transcribe(self, audio: bytes) -> str:
        self.calls.append(audio)
        self.thread_names.append(threading.current_thread().name)
        if self.delay:
            time.sleep(self.delay)
        if self.error is not None:
            raise self.error
        return self.text


@pytest.fixture(autouse=True)
def _reset_engine():
    """No global engine leaks between tests — the default is 'unprovisioned'."""
    previous = media_service.get_stt_engine()
    media_service.set_stt_engine(None)
    yield
    media_service.set_stt_engine(previous)


# ── 1. The seam itself ──


def test_no_stt_engine_is_provisioned_by_default():
    assert media_service.stt_available() is False
    assert media_service.get_stt_engine() is None


def test_stt_engine_registry_round_trips():
    media_service.set_stt_engine(_ScriptedEngine("x"))
    assert media_service.stt_available() is True
    assert isinstance(media_service.get_stt_engine(), _ScriptedEngine)
    media_service.set_stt_engine(None)
    assert media_service.stt_available() is False


@pytest.mark.parametrize("mime,present", [
    ("audio/ogg", True), ("audio/opus", True), ("application/ogg", True),
    ("audio/wav", True), ("audio/x-wav", True), ("audio/wave", True),
    ("audio/vnd.wave", True), ("audio/flac", True), ("audio/x-flac", True),
    ("AUDIO/OGG", True),
    ("audio/mpeg", False), ("audio/mp3", False), ("audio/mp4", False),
    ("audio/x-m4a", False), ("audio/webm", False), ("audio/aac", False),
    ("video/mp4", False), ("image/jpeg", False), ("text/plain", False), ("", False),
])
def test_stt_mime_set_is_explicit(mime, present):
    assert media_service.is_stt_mime(mime) is present


def test_is_extractable_mime_stays_engine_free_for_audio():
    # The M1/M1.2 predicate keeps describing dependency-free extractors only;
    # audio is gated by is_stt_mime + stt_available, never folded into it.
    assert media_service.is_extractable_mime("audio/ogg") is False
    assert media_service.is_extractable_mime("audio/wav") is False
    assert media_service.is_extractable_mime("image/jpeg") is False
    assert media_service.is_extractable_mime("application/pdf") is True


# ── 2. No engine: fail closed, and NOTHING is transferred ──


@pytest.mark.asyncio
async def test_unprovisioned_runtime_reports_voice_unsupported_without_transfer():
    payload = _ogg_opus(2.0)
    client = _FakeClient(payload=payload)
    analysis = await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert analysis.media_type == "Voice"
    assert analysis.status == MediaStatus.UNSUPPORTED
    assert analysis.content == ""
    assert analysis.has_content is False
    assert "Speech-to-text" in analysis.reason
    assert client.calls == [], "voice must not be transferred without an STT engine"


@pytest.mark.asyncio
async def test_unprovisioned_runtime_reports_audio_unsupported_without_transfer():
    payload = _wav(1.0)
    client = _FakeClient(payload=payload)
    analysis = await media_service.analyze_media(client, OWNER, _audio_message(payload))

    assert analysis.media_type == "Audio"
    assert analysis.status == MediaStatus.UNSUPPORTED
    assert "Speech-to-text" in analysis.reason
    assert client.calls == []


# ── 3. Provisioned engine: the real end-to-end pipeline ──


@pytest.mark.asyncio
async def test_voice_input_is_transcribed_through_the_boundary():
    payload = _ogg_opus(2.0)
    engine = _ScriptedEngine(_ENGLISH)
    media_service.set_stt_engine(engine)
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == _ENGLISH
    assert analysis.has_content is True
    assert analysis.truncated is False
    assert analysis.media_type == "Voice"
    assert analysis.mime_type == "audio/ogg"
    assert len(engine.calls) == 1, "transcription must run exactly once"
    assert len(client.calls) == 1


@pytest.mark.asyncio
async def test_audio_document_input_is_transcribed_through_the_boundary():
    payload = _wav(1.0)
    engine = _ScriptedEngine(_ENGLISH)
    media_service.set_stt_engine(engine)
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _audio_message(payload))

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == _ENGLISH
    assert analysis.media_type == "Audio"
    assert analysis.mime_type == "audio/wav"


@pytest.mark.asyncio
async def test_ogg_vorbis_and_flac_containers_are_supported_too():
    for mime, payload in (
        ("audio/ogg", _ogg_vorbis(2.0)),
        ("audio/flac", _flac(1.0)),
    ):
        engine = _ScriptedEngine("ok")
        media_service.set_stt_engine(engine)
        client = _FakeClient(payload=payload)
        message = _voice_message(payload, mime=mime) if mime == "audio/ogg" \
            else _audio_message(payload, mime=mime, name="track.flac")

        analysis = await media_service.analyze_media(client, OWNER, message)

        assert analysis.content == "ok", mime
        assert len(engine.calls) == 1, mime


@pytest.mark.asyncio
async def test_persian_transcript_survives_the_boundary_unchanged():
    payload = _ogg_opus(2.0)
    engine = _ScriptedEngine(f"{_PERSIAN_ZWNJ} {_PERSIAN}")
    media_service.set_stt_engine(engine)
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert analysis.content == f"{_PERSIAN_ZWNJ} {_PERSIAN}"
    assert _ZWNJ in analysis.content, "ZWNJ must survive normalization"
    assert analysis.content.count(_ZWNJ) == 1
    assert _PERSIAN in analysis.as_context_text()


@pytest.mark.asyncio
async def test_mixed_direction_transcript_keeps_its_order():
    engine = _ScriptedEngine(_MIXED)
    media_service.set_stt_engine(engine)
    client = _FakeClient(payload=_ogg_opus(2.0))

    analysis = await media_service.analyze_media(client, OWNER, _voice_message(_ogg_opus(2.0)))

    assert analysis.content.split("\n") == _MIXED.split("\n")


@pytest.mark.asyncio
async def test_transcript_normalization_collapses_whitespace_but_keeps_lines():
    engine = _ScriptedEngine("  Hello   world  \n\n\n\n  second   line\there  \n\n")
    media_service.set_stt_engine(engine)
    payload = _ogg_opus(2.0)
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert analysis.content == "Hello world\n\nsecond line here"


# ── 4. Honest empty result (no fabricated text, no wasted provider round) ──


@pytest.mark.asyncio
async def test_no_speech_is_reported_honestly_without_content():
    payload = _ogg_opus(2.0)
    engine = _ScriptedEngine("")
    media_service.set_stt_engine(engine)
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == ""
    assert analysis.has_content is False
    assert "no speech" in analysis.reason.lower()
    rendered = analysis.as_context_text()
    assert "Content:" not in rendered
    assert analysis.reason in rendered


@pytest.mark.asyncio
async def test_whitespace_only_transcript_counts_as_no_speech():
    engine = _ScriptedEngine("  \n\n \t \n ")
    media_service.set_stt_engine(engine)
    payload = _ogg_opus(2.0)
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert analysis.content == ""
    assert analysis.has_content is False


@pytest.mark.asyncio
async def test_a_non_string_engine_result_is_treated_as_no_speech():
    class _BadEngine:
        def transcribe(self, audio: bytes) -> Any:
            return None

    media_service.set_stt_engine(_BadEngine())
    payload = _ogg_opus(2.0)
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == ""


# ── 5. Fail-closed container validation ──


@pytest.mark.asyncio
async def test_malformed_audio_payload_fails_honestly():
    media_service.set_stt_engine(_ScriptedEngine("never reached"))
    client = _FakeClient(payload=b"this is plainly not an ogg stream")
    message = _voice_message(b"x" * 31)
    before = _media_temp_dirs()

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(client, OWNER, message)

    assert "not a readable audio stream" in str(error.value)
    assert _media_temp_dirs() == before


@pytest.mark.asyncio
async def test_mime_container_mismatch_is_refused():
    # Declares OGG, carries a real WAV: the signature must win over the label.
    media_service.set_stt_engine(_ScriptedEngine("never reached"))
    payload = _wav(1.0)
    client = _FakeClient(payload=payload)

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(client, OWNER, _voice_message(payload, mime="audio/ogg"))

    assert "not a readable audio stream of the declared type" in str(error.value)


@pytest.mark.parametrize("mime,payload", [
    ("audio/ogg", _wav(1.0)),
    ("audio/wav", _ogg_opus(2.0)),
    ("audio/flac", _ogg_opus(2.0)),
    ("audio/opus", _flac(1.0)),
])
@pytest.mark.asyncio
async def test_every_declared_audio_type_corroborates_its_signature(mime, payload):
    media_service.set_stt_engine(_ScriptedEngine("never reached"))
    client = _FakeClient(payload=payload)
    message = _audio_message(payload, mime=mime, name="x.bin")

    with pytest.raises(MediaError):
        await media_service.analyze_media(client, OWNER, message)


@pytest.mark.asyncio
async def test_an_unsupported_audio_container_is_refused_never_transcribed():
    # MP3 is deliberately outside this phase; it must not reach the engine.
    engine = _ScriptedEngine("never reached")
    media_service.set_stt_engine(engine)
    client = _FakeClient(payload=b"\xff\xfb\x90\x00" + b"\x00" * 400)
    message = _audio_message(b"\xff\xfb\x90\x00" + b"\x00" * 400, mime="audio/mpeg", name="song.mp3")

    analysis = await media_service.analyze_media(client, OWNER, message)

    assert analysis.status == MediaStatus.UNSUPPORTED
    assert client.calls == []
    assert engine.calls == []


# ── 6. Bounds ──


@pytest.mark.asyncio
async def test_audio_beyond_the_stt_input_bound_is_never_transferred():
    engine = _ScriptedEngine("never reached")
    media_service.set_stt_engine(engine)
    client = _FakeClient(payload=_wav(1.0))
    message = _audio_message(_wav(1.0), size=MAX_STT_INPUT_BYTES + 1)

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(client, OWNER, message)

    assert "exceeds" in str(error.value)
    assert client.calls == [], "audio beyond the STT bound must not be downloaded"
    assert engine.calls == [], "transcription must never start on a refused asset"


@pytest.mark.asyncio
async def test_audio_longer_than_the_duration_bound_is_refused_before_transcription():
    engine = _ScriptedEngine("never reached")
    media_service.set_stt_engine(engine)
    payload = _ogg_opus(MAX_STT_DURATION_S + 30.0)
    client = _FakeClient(payload=payload)

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert "speech-to-text bound" in str(error.value)
    assert engine.calls == []


@pytest.mark.asyncio
async def test_audio_inside_the_duration_bound_is_accepted():
    engine = _ScriptedEngine("ok")
    media_service.set_stt_engine(engine)
    payload = _ogg_opus(MAX_STT_DURATION_S - 1.0)
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert analysis.content == "ok"


def test_the_derived_duration_matches_the_container_exactly():
    for duration in (0.5, 2.0, 12.5, 300.0):
        payload = _ogg_opus(duration)
        channels, rate, derived = media_service._validate_audio_payload(payload, "audio/ogg")
        assert channels == 1 and rate == 48_000
        assert abs(derived - duration) < 0.001, duration

    channels, rate, derived = media_service._validate_audio_payload(_flac(3.0), "audio/flac")
    assert (channels, rate) == (1, 16_000)
    assert abs(derived - 3.0) < 0.001


@pytest.mark.asyncio
async def test_a_stream_with_an_indeterminate_duration_is_refused():
    # FLAC with total_samples = 0: the duration cannot be bounded, so it is not
    # handed to an engine on a guess.
    engine = _ScriptedEngine("never reached")
    media_service.set_stt_engine(engine)
    payload = _flac_without_total_samples()
    client = _FakeClient(payload=payload)

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(
            client, OWNER, _audio_message(payload, mime="audio/flac", name="x.flac"),
        )

    assert "could not be determined" in str(error.value)
    assert engine.calls == []


@pytest.mark.asyncio
async def test_more_channels_than_the_bound_is_refused():
    engine = _ScriptedEngine("never reached")
    media_service.set_stt_engine(engine)
    payload = _wav(0.2, channels=MAX_STT_CHANNELS + 1)
    client = _FakeClient(payload=payload)

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(client, OWNER, _audio_message(payload))

    assert "channels" in str(error.value)
    assert engine.calls == []


@pytest.mark.asyncio
async def test_a_sample_rate_beyond_the_bound_is_refused():
    engine = _ScriptedEngine("never reached")
    media_service.set_stt_engine(engine)
    payload = _flac(1.0, sample_rate=MIN_ABOVE_RATE)
    client = _FakeClient(payload=payload)

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(
            client, OWNER, _audio_message(payload, mime="audio/flac", name="x.flac"),
        )

    assert "Hz" in str(error.value)
    assert engine.calls == []


@pytest.mark.asyncio
async def test_transcript_is_capped_at_the_shared_character_ceiling():
    engine = _ScriptedEngine("ا" * (MAX_STT_CHARS + 5_000))
    media_service.set_stt_engine(engine)
    payload = _ogg_opus(2.0)
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert MAX_STT_CHARS == MAX_EXTRACTED_CHARS
    assert analysis.truncated is True
    assert len(analysis.content) == MAX_STT_CHARS
    assert analysis.content.endswith("…")
    assert analysis.content.startswith("ا")


@pytest.mark.asyncio
async def test_short_transcript_is_not_reported_as_truncated():
    engine = _ScriptedEngine("short transcript")
    media_service.set_stt_engine(engine)
    payload = _ogg_opus(2.0)
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert analysis.truncated is False
    assert analysis.content == "short transcript"


# ── 7. Failure paths ──


@pytest.mark.asyncio
async def test_stt_timeout_fails_honestly(monkeypatch):
    monkeypatch.setattr(media_service, "STT_TIMEOUT_S", 0.02)
    media_service.set_stt_engine(_ScriptedEngine("late", delay=0.4))
    payload = _ogg_opus(2.0)
    client = _FakeClient(payload=payload)
    before = _media_temp_dirs()

    started = time.monotonic()
    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert "did not finish" in str(error.value)
    assert time.monotonic() - started < 5.0
    assert _media_temp_dirs() == before


@pytest.mark.asyncio
async def test_engine_failure_is_wrapped_as_a_media_error():
    media_service.set_stt_engine(_ScriptedEngine(error=RuntimeError("engine exploded")))
    payload = _ogg_opus(2.0)
    client = _FakeClient(payload=payload)
    before = _media_temp_dirs()

    with pytest.raises(MediaError) as error:
        await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert "Speech-to-text failed" in str(error.value)
    assert _media_temp_dirs() == before


@pytest.mark.asyncio
async def test_missing_download_is_refused_before_transcription():
    engine = _ScriptedEngine("never reached")
    media_service.set_stt_engine(engine)
    client = _FakeClient(payload=b"")
    message = _audio_message(b"", size=40)

    with pytest.raises(MediaError):
        await media_service.analyze_media(client, OWNER, message)

    assert engine.calls == []


# ── 8. Execution model: off the event loop, bounded cleanup ──


@pytest.mark.asyncio
async def test_transcription_runs_off_the_event_loop():
    engine = _ScriptedEngine("threaded", delay=0.25)
    media_service.set_stt_engine(engine)
    payload = _ogg_opus(2.0)
    client = _FakeClient(payload=payload)

    ticks = 0

    async def _ticker() -> None:
        nonlocal ticks
        while True:
            await asyncio.sleep(0.01)
            ticks += 1

    ticker = asyncio.create_task(_ticker())
    try:
        await media_service.analyze_media(client, OWNER, _voice_message(payload))
    finally:
        ticker.cancel()
        with pytest.raises(asyncio.CancelledError):
            await ticker

    assert ticks >= 10, "the event loop must keep running during transcription"
    assert engine.thread_names and engine.thread_names[0] != threading.main_thread().name


@pytest.mark.asyncio
async def test_temp_directory_is_removed_on_success_failure_and_timeout(monkeypatch):
    before = _media_temp_dirs()

    payload = _ogg_opus(2.0)

    media_service.set_stt_engine(_ScriptedEngine("ok"))
    ok_client = _FakeClient(payload=payload)
    result = await media_service.analyze_media(ok_client, OWNER, _voice_message(payload))
    assert result.content == "ok"
    assert not os.path.exists(ok_client.calls[0]["file"])

    media_service.set_stt_engine(_ScriptedEngine(error=RuntimeError("boom")))
    with pytest.raises(MediaError):
        await media_service.analyze_media(_FakeClient(payload=payload), OWNER,
                                          _voice_message(payload))

    monkeypatch.setattr(media_service, "STT_TIMEOUT_S", 0.02)
    media_service.set_stt_engine(_ScriptedEngine("late", delay=0.4))
    with pytest.raises(MediaError):
        await media_service.analyze_media(_FakeClient(payload=payload), OWNER,
                                          _voice_message(payload))

    assert _media_temp_dirs() == before


@pytest.mark.asyncio
async def test_cancellation_cleans_up_and_propagates():
    # The delay only has to outlive the cancel below: a worker thread cannot be
    # force-stopped, so a longer sleep would keep the whole suite waiting.
    media_service.set_stt_engine(_ScriptedEngine("slow", delay=3.0))
    payload = _ogg_opus(2.0)
    client = _FakeClient(payload=payload)
    before = _media_temp_dirs()

    task = asyncio.create_task(
        media_service.analyze_media(client, OWNER, _voice_message(payload))
    )
    await asyncio.sleep(0.1)
    task.cancel()
    with pytest.raises(asyncio.CancelledError):
        await task

    assert _media_temp_dirs() == before


# ── 9. Zero Telegram context ──


@pytest.mark.asyncio
async def test_model_facing_rendering_excludes_every_telegram_identifier():
    engine = _ScriptedEngine("recognized transcript")
    media_service.set_stt_engine(engine)
    payload = _ogg_opus(2.0)
    client = _FakeClient(payload=payload)

    analysis = await media_service.analyze_media(client, OWNER, _voice_message(payload))
    rendered = analysis.as_context_text()

    assert "recognized transcript" in rendered
    assert CAPTION not in rendered
    assert str(MESSAGE_ID) not in rendered
    assert str(abs(CHAT)) not in rendered
    assert FILE_NAME not in rendered
    assert "Caption" not in rendered


@pytest.mark.asyncio
async def test_engine_sees_only_audio_bytes_never_telegram_metadata():
    engine = _ScriptedEngine("x")
    media_service.set_stt_engine(engine)
    payload = _ogg_opus(2.0)
    client = _FakeClient(payload=payload)

    await media_service.analyze_media(client, OWNER, _voice_message(payload))

    assert engine.calls == [payload], "the engine receives the validated bytes and nothing else"


# ── 10. Existing behaviour is untouched ──


@pytest.mark.asyncio
async def test_stt_engine_is_never_used_for_non_audio_media():
    engine = _ScriptedEngine("must not be used")
    media_service.set_stt_engine(engine)

    doc = Document(
        id=9, access_hash=9, file_reference=b"", date=None,
        mime_type="text/plain", size=5, dc_id=1,
        attributes=[DocumentAttributeFilename(file_name="notes.txt")],
    )
    client = _FakeClient(payload=b"plain")
    analysis = await media_service.analyze_media(
        client, OWNER, _FakeMessage(MessageMediaDocument(document=doc)),
    )

    assert analysis.content == "plain"
    assert engine.calls == []


@pytest.mark.asyncio
async def test_ocr_path_is_unaffected_by_the_stt_seam():
    # A provisioned STT engine must not change Photo behaviour: with no OCR
    # engine, a photo is still UNSUPPORTED and still never transferred.
    media_service.set_stt_engine(_ScriptedEngine("must not be used"))
    photo = MessageMediaPhoto(photo=Photo(
        id=3, access_hash=3, file_reference=b"", date=None,
        sizes=[PhotoSize(type="y", w=8, h=8, size=128)], dc_id=1,
    ))
    client = _FakeClient(payload=b"\xff\xd8\xff\xd9")

    analysis = await media_service.analyze_media(client, OWNER, _FakeMessage(photo))

    assert analysis.media_type == "Photo"
    assert analysis.status == MediaStatus.UNSUPPORTED
    assert "OCR" in analysis.reason
    assert client.calls == []


@pytest.mark.asyncio
async def test_video_and_sticker_remain_unsupported_regardless_of_engine():
    from telethon.tl.types import (
        DocumentAttributeAnimated,
        DocumentAttributeSticker,
        DocumentAttributeVideo,
        InputStickerSetEmpty,
    )

    media_service.set_stt_engine(_ScriptedEngine("must not be used"))
    for mime, attributes in (
        ("video/mp4", [DocumentAttributeVideo(duration=1, w=1, h=1)]),
        ("image/webp", [DocumentAttributeSticker(alt="x", stickerset=InputStickerSetEmpty())]),
        ("image/gif", [DocumentAttributeAnimated()]),
    ):
        doc = Document(
            id=4, access_hash=4, file_reference=b"", date=None, mime_type=mime,
            size=4096, dc_id=1, attributes=attributes,
        )
        client = _FakeClient(payload=b"x")
        analysis = await media_service.analyze_media(
            client, OWNER, _FakeMessage(MessageMediaDocument(document=doc)),
        )
        assert analysis.status == MediaStatus.UNSUPPORTED, mime
        assert client.calls == [], mime


@pytest.mark.asyncio
async def test_text_extraction_path_is_unaffected_by_the_stt_seam():
    media_service.set_stt_engine(_ScriptedEngine("ignored"))
    doc = Document(
        id=8, access_hash=8, file_reference=b"", date=None,
        mime_type="text/markdown", size=6, dc_id=1,
        attributes=[DocumentAttributeFilename(file_name="readme.md")],
    )
    client = _FakeClient(payload=b"# hi\n")
    analysis = await media_service.analyze_media(
        client, OWNER, _FakeMessage(MessageMediaDocument(document=doc)),
    )

    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == "# hi\n"


@pytest.mark.asyncio
async def test_the_document_container_paths_still_route_to_their_own_extractors():
    # PDF/DOCX are handled by _extract_content; the STT path must not intercept.
    assert media_service.is_pdf_mime("application/pdf") is True
    assert media_service.is_docx_mime(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ) is True
    assert media_service.is_stt_mime("application/pdf") is False
    assert media_service.is_stt_mime(
        "application/vnd.openxmlformats-officedocument.wordprocessingml.document"
    ) is False

    media_service.set_stt_engine(_ScriptedEngine("ignored"))

    import io as _io
    from pypdf import PdfWriter

    writer = PdfWriter()
    writer.add_blank_page(width=100, height=100)
    buffer = _io.BytesIO()
    writer.write(buffer)
    payload = buffer.getvalue()

    doc = Document(
        id=7, access_hash=7, file_reference=b"", date=None,
        mime_type="application/pdf", size=len(payload), dc_id=1,
        attributes=[DocumentAttributeFilename(file_name="doc.pdf")],
    )
    client = _FakeClient(payload=payload)
    analysis = await media_service.analyze_media(
        client, OWNER, _FakeMessage(MessageMediaDocument(document=doc)),
    )

    assert analysis.mime_type == "application/pdf"
    assert analysis.status == MediaStatus.EXTRACTED
    assert analysis.content == ""


def test_the_stt_boundary_adds_no_import_of_an_stt_library():
    from pathlib import Path

    source = Path(media_service.__file__).read_text(encoding="utf-8")
    for forbidden in ("vosk", "whisper", "ctranslate2", "speech_recognition",
                      "pytorch", "torch", "soundfile", "librosa", "ffmpeg"):
        assert forbidden not in source, forbidden
