"""
STT chunking — the deterministic division of ONE already-validated audio payload
into ordered parts the existing STT seam can transcribe one at a time.

Why this module exists at all: the media boundary's ``MAX_STT_DURATION_S`` is the
longest audio ONE recognition may be asked to handle, and until now it doubled as
the longest audio the boundary would accept — a 14-minute recording was refused
outright. The fix belongs to the ORCHESTRATION boundary, not to a provider: an
engine must keep receiving ONE already-bounded payload through the unchanged
``transcribe(audio: bytes) -> str`` seam, so the division has to happen here, above
the seam and below the model.

Nothing in this module knows about a provider, a model, a prompt, a credential, a
chat, a message id, a filename or a caption: its entire input is a validated audio
payload and two numeric ceilings. That is what keeps the media path's zero-context
rule intact — the only thing a chunk can carry is audio the boundary already
validated.

How a container is divided, and why THIS way:

* **OGG/Opus (and OGG/Vorbis)** — at OGG *page* boundaries. A page is the
  container's own unit of framing: it carries a lacing table, a granule position
  and a CRC over itself, so complete pages are the smallest pieces that are still
  valid container data. Each chunk is therefore the stream's own codec header
  pages followed by a run of complete pages, CONCATENATED BYTE-FOR-BYTE — not one
  byte of the source is rewritten, so no page CRC can be invalidated and no
  packet can be cut in half.
* **RIFF/WAVE** — at frame boundaries of its ``data`` chunk, which is plain
  uncompressed PCM. A chunk is the source's own pre-``data`` chunks with the RIFF
  and ``data`` size fields repatched for that chunk's payload, so every chunk is
  a complete, independently readable WAVE stream (its duration is genuinely its
  own, which is why it can be re-validated by ``media_service`` directly).
* **Anything else** — refused (``None``). FLAC frames are the concrete case: a
  frame boundary cannot be found without decoding subframes, and a re-headed FLAC
  would need STREAMINFO's total-sample count and MD5 rewritten. Rather than guess,
  the boundary reports the same honest refusal it reported before this module
  existed. Nothing is ever split on an arbitrary byte offset.

Known, deliberate limits, each pinned by tests rather than hidden:

* **Granule positions stay the source's own ABSOLUTE values.** Rewriting them
  (and renumbering page sequence numbers) would mean recomputing the OGG page
  CRC, a variant the standard library does not provide (``zlib.crc32`` is the
  reflected ISO-HDLC CRC) and which cannot be verified in this repository without
  a real Ogg fixture. The decoded audio of a chunk is exactly that chunk's own
  packets; only the container's *declared* duration is its position in the
  source. Recorded as the exact next step in ``IMPLEMENTATION_REPORT.md``.
* **Header pages are the leading pages whose granule position is 0**, which is how
  every real Ogg Opus/Vorbis encoder writes the identification, comment and setup
  pages. If an encoder ever packed an audio packet onto a header page, that one
  page's audio would be re-emitted with each chunk — bounded (one page), visible
  in ``AudioChunkPlan.chunk``, and never a reason to cut a page in half.
* **A page that alone spans more than the per-chunk ceiling cannot be divided**,
  so the payload is refused instead of producing an over-long chunk.
"""
from __future__ import annotations

from typing import NamedTuple, Sequence

#: The OGG containers this module may divide. Kept in step with the boundary's
#: own ``STT_AUDIO_MIME_TYPES``: a container the boundary accepts but this module
#: cannot divide is refused honestly by the caller, never split on a guess.
OGG_MIME_TYPES = frozenset({"audio/ogg", "audio/opus", "application/ogg"})

#: The RIFF/WAVE aliases Telegram declares. Same ruling as above.
WAV_MIME_TYPES = frozenset({
    "audio/wav", "audio/x-wav", "audio/wave", "audio/vnd.wave",
})

_OGG_MAGIC = b"OggS"
_OPUS_HEADER = b"OpusHead"
_VORBIS_HEADER = b"\x01vorbis"

#: The OGG page header-type flag the first page must carry: that is where the
#: codec identification header lives, which is what :func:`_ogg_granule_rate` reads.
#: Deliberately NOT required on the walker's part is an end-of-stream page: a
#: missing one means an incomplete payload, not an unknown boundary — every chunk
#: boundary is a PAGE boundary the stream itself declares, never the stream's end.
_OGG_BOS = 0x02

#: Opus granule positions are ALWAYS 48 kHz units, whatever input rate the stream
#: declares (identical to the boundary's own reader); Vorbis granules are samples
#: at the rate its identification header declares.
_OPUS_GRANULE_RATE = 48_000

_WAV_RIFF = b"RIFF"
_WAV_WAVE = b"WAVE"
_WAV_FMT = b"fmt "
_WAV_DATA = b"data"

#: Bound for the page walk of ONE payload — the same order as the boundary's own
#: container bound, so a deliberately fragmented stream cannot be walked without
#: limit. A 20 MiB Opus note is a few thousand pages.
_MAX_OGG_PAGES = 200_000

#: Bound for the RIFF chunk walk: a well-formed WAVE stream has a handful of
#: chunks before ``data``; a stream that declares more is not one this module reads.
_MAX_WAV_CHUNKS = 64

#: The separator between two chunks' transcripts: ONE newline, never a blank line
#: and never an invented word. The chunks are CONTIGUOUS, so the join adds a line
#: break and nothing else — no bridging text, no inferred words, no translation,
#: no summarization and no second model.
TRANSCRIPT_SEPARATOR = "\n"


class _OggPage(NamedTuple):
    """One complete OGG page: its byte range and the fields the plan needs."""

    start: int
    end: int
    granule: int
    header_type: int
    serial: int


class AudioChunkPlan:
    """One ordered, deterministic division of ONE validated audio payload.

    Holds the payload and the byte ranges of each chunk rather than a materialized
    list of chunks, so peak memory stays the payload plus ONE chunk: the caller
    builds a chunk right before it transcribes it and drops the previous one. The
    ranges are byte offsets into the SAME payload the boundary already validated,
    so every chunk is a slice of bytes that passed the container and duration
    checks — never a payload assembled from somewhere else.
    """

    __slots__ = (
        "mime_type", "container", "duration_s", "durations", "count",
        "_data", "_header", "_spans", "_data_size_offset",
    )

    def __init__(
        self,
        *,
        mime_type: str,
        container: str,
        duration_s: float,
        durations: Sequence[float],
        data: bytes,
        header: bytes,
        spans: Sequence[tuple[int, int]],
        data_size_offset: int = -1,
    ) -> None:
        self.mime_type = mime_type
        self.container = container
        self.duration_s = duration_s
        self.durations = tuple(durations)
        self.count = len(spans)
        self._data = data
        self._header = header
        self._spans = tuple(spans)
        self._data_size_offset = data_size_offset

    def chunk(self, index: int) -> bytes:
        """The bytes of chunk ``index``, built on demand (never all chunks at once).

        For OGG the chunk is the codec header pages plus the run's own complete
        pages, byte-for-byte. For WAVE the run's PCM is prefixed with the source's
        own pre-``data`` chunks whose RIFF and ``data`` sizes are repatched for
        this chunk's payload. Nothing else is ever synthesized.
        """
        start, end = self._spans[index]
        payload = self._data[start:end]
        if self._data_size_offset < 0:
            return self._header + payload
        header = bytearray(self._header)
        header[4:8] = (len(header) + len(payload) - 8).to_bytes(4, "little")
        header[self._data_size_offset:self._data_size_offset + 4] = (
            len(payload).to_bytes(4, "little")
        )
        return bytes(header) + payload


def join_transcripts(parts: Sequence[str]) -> str:
    """ONE transcript from the ordered chunk transcripts.

    Deterministic and content-preserving: the parts are joined in source order with
    :data:`TRANSCRIPT_SEPARATOR`, a part that transcribed to nothing contributes no
    text (silence is not a gap marker and not a failure), and nothing else is added,
    rewritten, reordered or removed. No deduplication happens because contiguous
    chunks have no overlap to remove.
    """
    return TRANSCRIPT_SEPARATOR.join(text for text in parts if text)


def plan(
    data: bytes,
    mime_type: str,
    *,
    max_chunk_duration_s: float,
    max_chunks: int,
) -> AudioChunkPlan | None:
    """``AudioChunkPlan`` for ``data``, or ``None`` when it cannot be divided.

    ``None`` is the fail-closed answer and covers every case this module will not
    guess about: an unknown container, a payload that is not a clean single-stream
    container, a payload that would need more than ``max_chunks`` chunks, a
    container whose declared duration does not match the chunks it can produce, and
    a single indivisible unit (one OGG page / a FLAC frame) that already exceeds
    ``max_chunk_duration_s``. The caller turns it into the boundary's existing
    deterministic refusal, so an unsupported split is reported, never approximated.
    """
    if not data or max_chunk_duration_s <= 0 or max_chunks < 2:
        return None
    value = str(mime_type or "").strip().lower()
    if value in OGG_MIME_TYPES:
        return _plan_ogg(
            data, value,
            max_chunk_duration_s=max_chunk_duration_s, max_chunks=max_chunks,
        )
    if value in WAV_MIME_TYPES:
        return _plan_wav(
            data, value,
            max_chunk_duration_s=max_chunk_duration_s, max_chunks=max_chunks,
        )
    return None


# ── OGG ──


def _ogg_pages(data: bytes) -> list[_OggPage] | None:
    """Every complete OGG page in ``data``, or ``None`` when it is not a clean stream.

    One bounded forward walk: a page declares its own segment count and lacing
    table, so the next page starts exactly where the payload of the current one
    ends. Trailing bytes, an unknown capture pattern, a page whose payload runs
    past the end of the file, or more than ``_MAX_OGG_PAGES`` pages all mean "not
    a stream this module will divide" — never a partially parsed one.
    """
    total = len(data)
    pages: list[_OggPage] = []
    index = 0
    while index + 27 <= total:
        if data[index:index + 4] != _OGG_MAGIC or data[index + 4] != 0:
            return None
        segment_count = data[index + 26]
        table = index + 27
        if table + segment_count > total:
            return None
        end = table + segment_count + sum(data[table:table + segment_count])
        if end > total:
            return None
        pages.append(_OggPage(
            start=index,
            end=end,
            granule=int.from_bytes(data[index + 6:index + 14], "little"),
            header_type=data[index + 5],
            serial=int.from_bytes(data[index + 14:index + 18], "little"),
        ))
        if len(pages) > _MAX_OGG_PAGES:
            return None
        index = end
    if index != total or len(pages) < 2:
        return None
    if pages[0].header_type & _OGG_BOS == 0:
        return None
    return pages


def _ogg_granule_rate(data: bytes, page: _OggPage) -> float:
    """The granule rate of the stream's codec, or ``0`` when it is neither.

    Read from the first page's own identification header, exactly as the media
    boundary's reader does: Opus granules are always 48 kHz units, Vorbis granules
    are samples at the rate the header declares. A stream that is neither codec is
    not divisible here.
    """
    payload = page.start + 27 + data[page.start + 26]
    if data[payload:payload + 8] == _OPUS_HEADER:
        return float(_OPUS_GRANULE_RATE)
    if data[payload:payload + 7] == _VORBIS_HEADER:
        return float(int.from_bytes(data[payload + 12:payload + 16], "little"))
    return 0.0


def _plan_ogg(
    data: bytes, mime_type: str, *, max_chunk_duration_s: float, max_chunks: int,
) -> AudioChunkPlan | None:
    pages = _ogg_pages(data)
    if pages is None:
        return None
    serial = pages[0].serial
    if any(page.serial != serial for page in pages):
        # A multiplexed or chained stream: the serial the plan would repeat as a
        # header belongs to one logical stream only, so this is not divisible here.
        return None
    granule_rate = _ogg_granule_rate(data, pages[0])
    if granule_rate <= 0:
        return None

    # Codec header pages: the leading pages whose granule position is 0.
    header_count = 0
    for page in pages:
        if page.granule > 0:
            break
        header_count += 1
    if header_count == 0 or header_count >= len(pages):
        return None

    per_chunk = max_chunk_duration_s * granule_rate
    groups: list[list[_OggPage]] = []
    starts: list[float] = []
    current: list[_OggPage] = []
    start_granule = 0.0
    for page in pages[header_count:]:
        # A chunk is closed on the page BEFORE the one that would push it past the
        # ceiling, so every chunk covers a contiguous granule range and no page is
        # ever divided. A single page always joins a chunk, even when it alone
        # spans more than the ceiling — that case is refused below, not truncated.
        if current and page.granule - start_granule > per_chunk:
            groups.append(current)
            starts.append(start_granule)
            start_granule = current[-1].granule
            current = []
        current.append(page)
    if current:
        groups.append(current)
        starts.append(start_granule)

    if len(groups) < 2 or len(groups) > max_chunks:
        return None
    durations: list[float] = []
    spans: list[tuple[int, int]] = []
    for group, chunk_start in zip(groups, starts):
        span = (group[-1].granule - chunk_start) / granule_rate
        if span <= 0 or span > max_chunk_duration_s:
            return None
        durations.append(span)
        spans.append((group[0].start, group[-1].end))
    # The header pages are re-emitted with EVERY chunk: they carry OpusHead (the
    # channels, pre-skip and input rate) and OpusTags, without which a chunk of the
    # middle of the stream is not a decodable Ogg Opus stream at all.
    header = data[pages[0].start:pages[header_count - 1].end]
    return AudioChunkPlan(
        mime_type=mime_type, container="ogg", duration_s=sum(durations),
        durations=durations, data=data, header=header, spans=spans,
    )


# ── RIFF/WAVE ──


def _wav_layout(data: bytes) -> "tuple[int, int, int, int, int, int] | None":
    """``(channels, sample_rate, block_align, byte_rate, data_start, data_bytes)``.

    Reads only the chunks' own headers, so the walk is bounded by the declared
    sizes and never by the payload's contents (the same shape as the boundary's
    ``_wav_audio_info``). ``data_bytes`` is what the file ACTUALLY holds, so a
    truncated stream is divided at its real end and the chunks are still honest.
    """
    total = len(data)
    if total < 44 or data[:4] != _WAV_RIFF or data[8:12] != _WAV_WAVE:
        return None
    channels = 0
    sample_rate = 0
    block_align = 0
    byte_rate = 0
    data_start = 0
    data_bytes = 0
    index = 12
    walked = 0
    while index + 8 <= total:
        walked += 1
        if walked > _MAX_WAV_CHUNKS:
            return None
        chunk_id = data[index:index + 4]
        chunk_size = int.from_bytes(data[index + 4:index + 8], "little")
        body = index + 8
        if chunk_id == _WAV_FMT:
            if body + 16 > total:
                return None
            channels = int.from_bytes(data[body + 2:body + 4], "little")
            sample_rate = int.from_bytes(data[body + 4:body + 8], "little")
            block_align = int.from_bytes(data[body + 12:body + 14], "little")
            byte_rate = int.from_bytes(data[body + 8:body + 12], "little")
        elif chunk_id == _WAV_DATA:
            data_start = body
            data_bytes = min(chunk_size, max(0, total - body))
            break
        index = body + chunk_size + (chunk_size % 2)
    if channels <= 0 or sample_rate <= 0 or block_align <= 0 or data_start <= 12:
        return None
    if byte_rate <= 0:
        byte_rate = block_align * sample_rate
    if data_bytes <= 0:
        return None
    return channels, sample_rate, block_align, byte_rate, data_start, data_bytes


def _plan_wav(
    data: bytes, mime_type: str, *, max_chunk_duration_s: float, max_chunks: int,
) -> AudioChunkPlan | None:
    layout = _wav_layout(data)
    if layout is None:
        return None
    _channels, sample_rate, block_align, byte_rate, data_start, data_bytes = layout
    frames_per_chunk = int(max_chunk_duration_s * sample_rate)
    bytes_per_chunk = frames_per_chunk * block_align
    if frames_per_chunk <= 0 or bytes_per_chunk <= 0:
        return None

    # Frame-aligned ranges only: a chunk boundary always lands on a whole frame,
    # so no channel's sample is ever cut away from its neighbours.
    spans: list[tuple[int, int]] = []
    offset = 0
    while offset < data_bytes:
        size = min(bytes_per_chunk, data_bytes - offset)
        spans.append((data_start + offset, data_start + offset + size))
        offset += size
    if len(spans) < 2 or len(spans) > max_chunks:
        return None
    durations = [(end - start) / byte_rate for start, end in spans]
    if any(duration <= 0 or duration > max_chunk_duration_s for duration in durations):
        return None
    return AudioChunkPlan(
        mime_type=mime_type, container="wav", duration_s=sum(durations),
        durations=durations, data=data, header=data[:data_start], spans=spans,
        data_size_offset=data_start - 4,
    )
