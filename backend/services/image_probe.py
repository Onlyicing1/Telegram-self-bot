"""Dependency-free image validation for owner-uploaded preview images.

Telegram hands the self client a downloaded byte stream. The DECLARED MIME
type (or a file name) is never trusted: the bytes themselves are parsed and
the real format, frame dimensions and content type are derived from them.
Only the three raster formats Telegram renders reliably as a link preview are
accepted (JPEG, PNG, WebP) — SVG/HTML and anything malformed are refused.

The project has no image-processing dependency (no Pillow), so the small
amount of header parsing needed for PNG/JPEG/WebP lives here rather than
pulling a heavyweight library in for one feature.
"""
from __future__ import annotations

import struct
from dataclasses import dataclass

MAX_IMAGE_BYTES = 5 * 1024 * 1024
MIN_DIMENSION = 16
MAX_DIMENSION = 4096

_MIME_BY_EXT = {
    "png": "image/png",
    "jpg": "image/jpeg",
    "webp": "image/webp",
}


@dataclass(frozen=True)
class ProbeResult:
    mime: str
    ext: str
    width: int
    height: int


def _bounds_ok(width: int, height: int) -> bool:
    return (
        MIN_DIMENSION <= width <= MAX_DIMENSION
        and MIN_DIMENSION <= height <= MAX_DIMENSION
    )


def _probe_png(data: bytes) -> ProbeResult | None:
    if len(data) < 24 or not data.startswith(b"\x89PNG\r\n\x1a\n"):
        return None
    if data[12:16] != b"IHDR":
        return None
    width, height = struct.unpack(">II", data[16:24])
    return ProbeResult("image/png", "png", int(width), int(height))


def _probe_jpeg(data: bytes) -> ProbeResult | None:
    if len(data) < 4 or not data.startswith(b"\xff\xd8"):
        return None
    index = 2
    length = len(data)
    while index + 9 < length:
        if data[index] != 0xFF:
            index += 1
            continue
        marker = data[index + 1]
        index += 2
        if marker in (0xD8, 0xD9) or 0xD0 <= marker <= 0xD7:
            continue
        if index + 2 > length:
            return None
        segment_length = struct.unpack(">H", data[index:index + 2])[0]
        if segment_length < 2 or index + segment_length > length:
            return None
        if marker in (0xC0, 0xC1, 0xC2, 0xC3, 0xC5, 0xC6, 0xC7,
                      0xC9, 0xCA, 0xCB, 0xCD, 0xCE, 0xCF):
            if segment_length < 7:
                return None
            height, width = struct.unpack(">HH", data[index + 3:index + 7])
            return ProbeResult("image/jpeg", "jpg", int(width), int(height))
        index += segment_length
    return None


def _probe_webp(data: bytes) -> ProbeResult | None:
    if len(data) < 30 or data[:4] != b"RIFF" or data[8:12] != b"WEBP":
        return None
    chunk = data[12:16]
    if chunk == b"VP8X":
        width_minus_one = int.from_bytes(data[24:27], "little")
        height_minus_one = int.from_bytes(data[27:30], "little")
        return ProbeResult("image/webp", "webp", width_minus_one + 1, height_minus_one + 1)
    if chunk == b"VP8L":
        if len(data) < 25:
            return None
        bits = int.from_bytes(data[21:25], "little")
        width = (bits & 0x3FFF) + 1
        height = ((bits >> 14) & 0x3FFF) + 1
        return ProbeResult("image/webp", "webp", width, height)
    if chunk == b"VP8 ":
        if len(data) < 30:
            return None
        # Lossy VP8: 3-byte frame tag, 3-byte start code, then 16-bit W/H.
        width, height = struct.unpack("<HH", data[26:30])
        return ProbeResult("image/webp", "webp", width & 0x3FFF, height & 0x3FFF)
    return None


def probe_image(data: bytes | None) -> ProbeResult | None:
    """Validate raw bytes and return their real format/dimensions.

    Returns ``None`` for empty, oversized, malformed or unsupported data.
    """
    if not isinstance(data, (bytes, bytearray)) or not data:
        return None
    if len(data) > MAX_IMAGE_BYTES:
        return None
    payload = bytes(data)
    for probe in (_probe_png, _probe_jpeg, _probe_webp):
        try:
            result = probe(payload)
        except (struct.error, ValueError):
            result = None
        if result is not None:
            if not _bounds_ok(result.width, result.height):
                return None
            return result
    return None


def mime_for_extension(ext: str) -> str:
    return _MIME_BY_EXT.get(str(ext or "").lower(), "application/octet-stream")
