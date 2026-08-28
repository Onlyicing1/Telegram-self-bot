"""Centralized, language-agnostic AI output normalization and Telegram delivery."""
from __future__ import annotations

import logging
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

from telethon.tl import types as tg_types

logger = logging.getLogger(__name__)
SAFE_LIMIT = 4000
_MIN_SPLIT_CHUNK = 100

@dataclass(frozen=True)
class OutputProfile:
    scripts: tuple[str, ...]
    direction: str
    mixed_direction: bool
    markdown_detected: bool

@dataclass(frozen=True)
class RenderedOutput:
    text: str
    profile: OutputProfile
    changed: bool
    entity_count: int
    entities: tuple[Any, ...] = ()

_RTL_SCRIPTS = {"ARABIC", "HEBREW"}
_MARKDOWN_RE = re.compile(r"(?:\*\*|__|(?<!\\)[*_`]\S|\[[^\]]+\]\([^)]*\))")
_PROTECTED_RE = re.compile(r"```.*?```|`[^`\n]*`|https?://[^\s<>]+|www\.[^\s<>]+|@[A-Za-z0-9_]{1,64}|/\w+(?:@[A-Za-z0-9_]+)?", re.S)


def _script(char: str) -> str:
    name = unicodedata.name(char, "")
    for candidate in ("ARABIC", "HEBREW", "CYRILLIC", "GREEK", "HIRAGANA", "KATAKANA", "HANGUL", "CJK", "LATIN"):
        if candidate in name:
            return candidate
    return "OTHER"


def _profile(text: str) -> OutputProfile:
    scripts: set[str] = set()
    rtl = ltr = False
    for char in text:
        if not char.isalpha():
            continue
        script = _script(char)
        scripts.add(script)
        if script in _RTL_SCRIPTS:
            rtl = True
        else:
            ltr = True
    direction = "rtl" if rtl and not ltr else "ltr" if ltr and not rtl else "neutral"
    return OutputProfile(tuple(sorted(scripts)), direction, rtl and ltr, bool(_MARKDOWN_RE.search(text)))


def _protect(text: str) -> tuple[str, list[str]]:
    tokens: list[str] = []
    def hold(match: re.Match[str]) -> str:
        tokens.append(match.group(0))
        return f"\u0000{len(tokens) - 1}\u0000"
    return _PROTECTED_RE.sub(hold, text), tokens


def _restore(text: str, tokens: list[str]) -> str:
    for index, token in enumerate(tokens):
        text = text.replace(f"\u0000{index}\u0000", token)
    return text


def _normalize_plain(text: str) -> str:
    text = unicodedata.normalize("NFC", text).replace("\r\n", "\n").replace("\r", "\n")
    persian_markers = "پچژگ"
    if any(char in persian_markers for char in text):
        text = text.replace("ي", "ی").replace("ك", "ک")
    text, tokens = _protect(text)
    text = re.sub(r"[ \t]+", " ", text)
    text = re.sub(r" *\n[ \t]*", "\n", text)
    text = re.sub(r"\n{3,}", "\n\n", text).strip()
    text = re.sub(r" +([,.;:!?،؛؟])", r"\1", text)
    # Sentence spacing after punctuation only. `.` and `:` are excluded so
    # filenames, extensions, bare domains, and abbreviations stay intact
    # (e.g. main.py, report.txt, example.com, e.g.). ",";"/"!"/"?" and the
    # Arabic marks are sentence/clause punctuation and keep their space.
    text = re.sub(r"([,;!?،؛؟])(?=[A-Za-zА-Яа-яء-ي])", r"\1 ", text)
    return _restore(text, tokens)


def _render_markdown(text: str) -> str:
    text = re.sub(r"\[([^\]]+)\]\((https?://[^)\s]+)\)", r"\1 (\2)", text)
    text, tokens = _protect(text)
    # Emphasis is stripped only at word boundaries; intraword delimiters
    # (snake_case, math like 2*3*4) are ambiguous and must stay literal.
    text = re.sub(r"(?<!\w)\*\*(?!\s)(.+?)(?<!\s)\*\*(?!\w)|(?<!\w)__(?!\s)(.+?)(?<!\s)__(?!\w)", lambda m: m.group(1) or m.group(2), text, flags=re.S)
    text = re.sub(r"(?<!\w)\*(?!\s)(.+?)(?<!\s)\*(?!\w)|(?<!\w)_(?!\s)(.+?)(?<!\s)_(?!\w)", lambda m: m.group(1) or m.group(2), text, flags=re.S)
    text = re.sub(r"^\s{0,3}#{1,6}\s+", "", text, flags=re.M)
    text = re.sub(r"^\s*[-*+]\s+", "• ", text, flags=re.M)
    text = re.sub(r"^\s*>\s?", "▎ ", text, flags=re.M)
    text = text.replace("\\\\", "\\")
    return _restore(text, tokens)


def _utf16_units(text: str) -> int:
    return len(text.encode("utf-16-le")) // 2


def _utf16_offset(text: str, index: int) -> int:
    return _utf16_units(text[:index])


def _entity_valid(entity: Any, text: str) -> bool:
    start = entity.offset
    end = start + entity.length
    total = _utf16_units(text)
    return 0 <= start <= end <= total


def _render_entities(text: str) -> tuple[Any, ...]:
    entities: list[Any] = []
    for match in re.finditer(r"\*\*(.+?)\*\*|(?<!\*)\*(.+?)(?<!\*)\*|`([^`\n]+)`", text, re.S):
        value = match.group(1) or match.group(2) or match.group(3)
        start = match.start(1) if match.group(1) else match.start(2) if match.group(2) else match.start(3)
        cls = tg_types.MessageEntityBold if match.group(1) else tg_types.MessageEntityItalic if match.group(2) else tg_types.MessageEntityCode
        entities.append(cls(_utf16_offset(text, start), _utf16_units(value)))
    return tuple(entity for entity in entities if _entity_valid(entity, text))


def process_output(text: str) -> RenderedOutput:
    if not isinstance(text, str) or not text.strip():
        raise ValueError("AI output must be non-empty text")
    rendered = _render_markdown(_normalize_plain(text))
    if not rendered.strip():
        raise ValueError("AI output became empty after rendering")
    entities = _render_entities(text)
    return RenderedOutput(rendered, _profile(rendered), rendered != text, len(entities), entities)

@dataclass(frozen=True)
class DeliveryResult:
    success: bool
    chunks_delivered: int
    total_chunks: int
    error: str = ""


def _format_message(user_message: str, trigger_label: str, response: str) -> str:
    return f"{user_message}\n────────────\n🤖 {trigger_label}\n{response}"


def _format_continuation(response: str, part: int, total: int) -> str:
    return f"{response}\n\n_({part}/{total})_"


def _find_split_point(chunk: str) -> int | None:
    for marker in ("\n\n", "\n", " "):
        index = chunk.rfind(marker)
        if index > _MIN_SPLIT_CHUNK:
            return index + len(marker)
    return None


def _split_text(text: str, limit: int) -> list[str]:
    """Split ``text`` so every chunk is at most ``limit`` UTF-16 code units.

    `limit` is a Telegram text-size boundary measured in UTF-16 code units,
    matching Telegram/entity offset accounting. Supplementary-plane
    characters (e.g. many emoji) occupy 2 UTF-16 units, so the limit is
    enforced against ``_utf16_units``, never Python character count.
    Surrogate pairs are never split. The concatenation of the returned
    chunks preserves the complete content.
    """
    chunks: list[str] = []
    rest = text
    while _utf16_units(rest) > limit:
        # Locate the UTF-16 boundary; drop to a character boundary when
        # needed so a leading surrogate of a supplementary pair is never
        # cut from the chunk and left orphaned.
        point = _split_point_at_utf16(rest, limit)
        chunks.append(rest[:point])
        rest = rest[point:]
    if rest:
        chunks.append(rest)
    return chunks or [""]


def _split_point_at_utf16(text: str, limit: int) -> int:
    """Return the character index at which a legal split occurs.

    First attempts paragraph/newline/word-boundary splits within the UTF-16
    budget (mirroring the existing preference order); if none fits, falls
    back to a character boundary that does not split a surrogate pair.
    """
    candidate = _find_split_point(_upto_utf16(text, limit)) or len(_upto_utf16(text, limit))
    candidate = _align_to_character(text, candidate)
    if candidate <= 0 or candidate >= len(text):
        candidate = _align_to_character(text, limit)
    return candidate


def _upto_utf16(text: str, units: int) -> str:
    """Return the longest prefix of ``text`` whose UTF-16 length <= ``units``.

    Never splits a surrogate pair. Falls back to at least one character so
    progress always occurs.
    """
    if _utf16_units(text) <= units:
        return text
    prefix = text
    # Over-allocate then trim by units so a complete maximum-length prefix is
    # always found without widening the scan repeatedly.
    approx = min(len(text), (units // 2) + 4)
    prefix = prefix[:approx]
    while _utf16_units(prefix) > units:
        prefix = prefix[:_align_to_character(prefix, len(prefix) - 1)]
    if not prefix:
        prefix = text[:1]
    return prefix


def _align_to_character(text: str, units: int) -> int:
    """Return an index into ``text`` closest to ``units`` that does not split
    a supplementary surrogate pair, and at minimum splits after one char."""
    index = max(1, min(units, len(text)))
    while index < len(text) and 0xD800 <= ord(text[index]) <= 0xDFFF:
        index += 1
    if index == 0 and text:
        index = 1
    return index


def _format_chunks(user_message: str, trigger_label: str, response_text: str) -> list[str]:
    full = _format_message(user_message, trigger_label, response_text)
    if _utf16_units(full) <= SAFE_LIMIT:
        return [full]
    header = f"{user_message}\n────────────\n🤖 {trigger_label}\n"
    body = _split_text(response_text, max(_MIN_SPLIT_CHUNK, SAFE_LIMIT - _utf16_units(header)))
    if len(body) == 1:
        return _split_text(full, SAFE_LIMIT)
    return [header + body[0]] + [_format_continuation(part, i + 1, len(body)) for i, part in enumerate(body[1:], 1)]


async def deliver_response(event: Any, user_message: str, trigger_label: str, response_text: str) -> DeliveryResult:
    if not response_text:
        try:
            await event.edit(f"{user_message}\n────────────\n🤖 {trigger_label}\n❌ Error\nAI returned no response.")
        except Exception as exc:
            logger.warning("delivery: empty-response edit failed: %s", exc)
            return DeliveryResult(False, 0, 0, str(exc))
        return DeliveryResult(True, 1, 1)
    try:
        processed = process_output(response_text)
        response_text = processed.text
        logger.info("AI_OUTPUT_NORMALIZED scripts=%s direction=%s mixed=%s markdown=%s changed=%s length=%d", ",".join(processed.profile.scripts) or "none", processed.profile.direction, processed.profile.mixed_direction, processed.profile.markdown_detected, processed.changed, len(response_text))
    except Exception as exc:
        logger.warning("AI_OUTPUT_NORMALIZATION_FALLBACK error_type=%s", type(exc).__name__)
    messages = _format_chunks(user_message, trigger_label, response_text)
    delivered = 0
    try:
        await event.edit(messages[0])
        delivered += 1
    except Exception as exc:
        logger.warning("delivery: first chunk edit failed: %s", exc)
        try:
            await event.reply(messages[0])
            delivered += 1
        except Exception as exc2:
            return DeliveryResult(False, delivered, len(messages), f"edit failed: {exc}; reply failed: {exc2}")
    for index, message in enumerate(messages[1:], 1):
        try:
            await event.reply(message)
            delivered += 1
        except Exception as exc:
            return DeliveryResult(False, delivered, len(messages), f"chunk {index + 1}/{len(messages)} reply failed: {exc}")
    return DeliveryResult(True, delivered, len(messages))
