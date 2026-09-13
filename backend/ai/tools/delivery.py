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


def _display_width(char: str) -> int:
    """Monospace display width of a single character.

    Combining marks, zero-width joiners/non-joiners, variation selectors and
    zero-width spaces occupy no visual column; East Asian wide/fullwidth
    characters and supplementary-plane characters (emoji) occupy two;
    everything else (Latin, Persian, Arabic, digits) occupies one.
    """
    if unicodedata.combining(char) or char in "\u200d\u200c\u200b\ufe0e\ufe0f":
        return 0
    if unicodedata.east_asian_width(char) in ("W", "F") or ord(char) > 0xFFFF:
        return 2
    return 1


def _cell_display_width(cell: str) -> int:
    return sum(_display_width(char) for char in cell)


def _pad_cell(cell: str, width: int) -> str:
    return cell + " " * max(0, width - _cell_display_width(cell))


def _split_table_row(line: str) -> list[str] | None:
    """Split a table row on ``|``. Returns ``None`` when the line is not a
    pipe-delimited row (no pipe, or only a single empty cell)."""
    if "|" not in line:
        return None
    cells = line.split("|")
    if cells and cells[0].strip() == "":
        cells = cells[1:]
    if cells and cells[-1].strip() == "":
        cells = cells[:-1]
    if not cells:
        return None
    return [cell.strip() for cell in cells]


def _is_table_separator(line: str) -> bool:
    cells = _split_table_row(line)
    if not cells:
        return False
    return all(re.fullmatch(r":?-+:?", cell) for cell in cells)


def _build_table_block(rows: list[list[str]]) -> str | None:
    ncols = len(rows[0])
    widths = [0] * ncols
    for row in rows:
        for index, cell in enumerate(row):
            widths[index] = max(widths[index], _cell_display_width(cell))

    def fmt(cells: list[str]) -> str:
        return " | ".join(_pad_cell(cell, widths[index]) for index, cell in enumerate(cells))

    parts = [fmt(rows[0]), " | ".join("-" * (width + 2) for width in widths)]
    parts.extend(fmt(row) for row in rows[1:])
    block = "\n".join(parts)
    if "```" in block:
        return None
    return f"```\n{block}\n```"


def _render_tables(text: str) -> str:
    """Render pipe-delimited Markdown tables as aligned monospace blocks.

    Only lines satisfying real table structure are transformed: a header row
    followed by a dash separator row with the same column count, then body
    rows with the same column count. Protected regions (URLs, usernames,
    commands, inline/fenced code) are masked first, so table syntax inside
    them is never parsed. Ambiguous or ragged input fails closed and is left
    exactly as-is.
    """
    text, tokens = _protect(text)
    lines = text.split("\n")
    out: list[str] = []
    index = 0
    while index < len(lines):
        header = _split_table_row(lines[index])
        if header and index + 1 < len(lines):
            separator = _split_table_row(lines[index + 1])
            if separator and _is_table_separator(lines[index + 1]) and len(separator) == len(header):
                rows: list[list[str]] = [header]
                cursor = index + 2
                invalid = False
                while cursor < len(lines):
                    row = _split_table_row(lines[cursor])
                    if row is None:
                        break
                    if len(row) != len(header):
                        invalid = True
                        break
                    rows.append(row)
                    cursor += 1
                block = None if invalid else _build_table_block(rows)
                if block is not None:
                    out.append(block)
                    index = cursor
                    continue
        out.append(lines[index])
        index += 1
    return _restore("\n".join(out), tokens)


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
    rendered = _render_tables(_render_markdown(_normalize_plain(text)))
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


# ── Chat presentation ────────────────────────────────────────────────────────
# Presentation-only rendering. The owner's message is quoted behind `│`; the
# answer starts behind `└─ ` and every continuation line is indented by exactly
# four ASCII spaces so all answer text starts at the same visual column. There
# is no trigger label, header, emoji, or separator anywhere in the presentation.
# The renderer never alters the answer text itself and owns no preference —
# the caller decides whether the question is shown.

_QUESTION_MARK = "│"
_ANSWER_MARK = "└─"
_ANSWER_INDENT = "    "
_QUESTION_PREFIX = f"{_QUESTION_MARK} "
_ANSWER_PREFIX = f"{_ANSWER_MARK} "


def _presentation_lines(text: str) -> list[str]:
    """Split into lines WITHOUT touching line content: the renderer adds
    prefixes only, so the answer text (including significant indentation and
    table padding) survives byte for byte."""
    if not isinstance(text, str) or not text:
        return []
    normalized = text.replace("\r\n", "\n").replace("\r", "\n")
    return normalized.split("\n")


def question_block(user_message: str) -> str:
    """Quote every line of the owner's message behind ``│``.

    Leading/trailing blank lines are dropped (they would read as stray bars);
    an inner blank line renders as a bare ``│``.
    """
    lines = _presentation_lines(user_message)
    while lines and not lines[0].strip():
        lines.pop(0)
    while lines and not lines[-1].strip():
        lines.pop()
    return "\n".join(
        f"{_QUESTION_PREFIX}{line}" if line.strip() else _QUESTION_MARK for line in lines
    )


def answer_block(response_text: str) -> str:
    """Render an answer: ``└─ `` on the first line, four spaces afterwards."""
    lines = _presentation_lines(response_text)
    if not lines:
        return _ANSWER_MARK
    rendered = [f"{_ANSWER_PREFIX}{lines[0]}"]
    rendered.extend(f"{_ANSWER_INDENT}{line}" for line in lines[1:])
    return "\n".join(rendered)


def format_presentation(user_message: str, response_text: str, show_question: bool) -> str:
    """The single AI chat presentation shared by every delivery path.

    ``show_question`` is presentation state only: it changes nothing about the
    model input, history, prompts, providers, or tools.
    """
    answer = answer_block(response_text)
    if not show_question:
        return answer
    question = question_block(user_message)
    if not question:
        return answer
    return f"{question}\n{_QUESTION_MARK}\n{answer}"


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


def _rendered_cost(lines: list[str], *, boundary: bool) -> int:
    """UTF-16 cost of rendering ``lines`` as one answer page."""
    if not lines:
        return 0
    total = _utf16_units(_ANSWER_PREFIX) + _utf16_units(lines[0])
    for line in lines[1:]:
        total += 1 + _utf16_units(_ANSWER_INDENT) + _utf16_units(line)
    if boundary:
        # A page that is not the last one keeps its terminating newline, which
        # renders as one extra four-space continuation line.
        total += 1 + _utf16_units(_ANSWER_INDENT)
    return total


def _paginate(body: str, budget: int) -> list[str]:
    """Split ``body`` into pages whose rendered presentation fits ``budget``
    UTF-16 units, preferring line boundaries; a single line wider than the
    budget is split by the UTF-16 splitter. A page that is not the last one
    keeps its terminating newline so the delivered chunks reconstruct the
    original body exactly.
    """
    lines = body.split("\n")
    pages: list[str] = []
    current: list[str] = []

    def flush(*, boundary: bool) -> None:
        pages.append("\n".join(current + ([""] if boundary else [])))
        current.clear()

    for index, line in enumerate(lines):
        last = index == len(lines) - 1
        if _rendered_cost([line], boundary=not last) > budget:
            if current:
                flush(boundary=True)
            pieces = _split_text(line, max(_MIN_SPLIT_CHUNK, budget - len(_ANSWER_INDENT)))
            if not last:
                # Keep the newline that terminated the split line so no line
                # boundary is lost between two hard-split pages.
                pieces[-1] = pieces[-1] + "\n"
            pages.extend(pieces)
            continue
        if current and _rendered_cost(current + [line], boundary=not last) > budget:
            flush(boundary=True)
        current.append(line)
    if current:
        flush(boundary=False)
    return pages or [""]


def _format_chunks(user_message: str, response_text: str, show_question: bool = False) -> list[str]:
    full = format_presentation(user_message, response_text, show_question)
    if _utf16_units(full) <= SAFE_LIMIT:
        return [full]
    prefix = ""
    if show_question:
        candidate = f"{question_block(user_message)}\n{_QUESTION_MARK}\n"
        if _utf16_units(candidate) < SAFE_LIMIT - _MIN_SPLIT_CHUNK:
            prefix = candidate
    footer_reserve = _utf16_units("\n\n_(9/99)_") + 2
    budget = max(_MIN_SPLIT_CHUNK, SAFE_LIMIT - _utf16_units(prefix) - footer_reserve)
    pages = _paginate(response_text, budget)
    if len(pages) == 1:
        # The (rare) oversized question, not the answer, overflowed the limit.
        return _split_text(full, SAFE_LIMIT)
    chunks = [prefix + answer_block(pages[0])]
    for index, page in enumerate(pages[1:], 2):
        chunks.append(_format_continuation(answer_block(page), index, len(pages)))
    return chunks


async def deliver_response(
    event: Any,
    user_message: str,
    response_text: str,
    show_question: bool = False,
) -> DeliveryResult:
    if not isinstance(response_text, str) or not response_text.strip():
        # A whitespace-only response is NO response (live evidence: response
        # == " " passed the truthiness check, then normalization raised
        # ValueError and the header-only shell was delivered anyway).
        try:
            await event.edit(
                format_presentation(user_message, "Error\nAI returned no response.", show_question)
            )
        except Exception as exc:
            logger.warning("delivery: empty-response edit failed: %s", exc)
            return DeliveryResult(False, 0, 0, str(exc))
        return DeliveryResult(True, 1, 1)
    try:
        processed = process_output(response_text)
        response_text = processed.text
        logger.info("AI_OUTPUT_NORMALIZED scripts=%s direction=%s mixed=%s markdown=%s changed=%s length=%d", ",".join(processed.profile.scripts) or "none", processed.profile.direction, processed.profile.mixed_direction, processed.profile.markdown_detected, processed.changed, len(response_text))
    except Exception as exc:
        # Content-free classification only: the failure is never hidden, but
        # raw AI output is never echoed to logs either.
        logger.warning(
            "AI_OUTPUT_NORMALIZATION_FALLBACK error_type=%s nonempty_after_strip=%s",
            type(exc).__name__, bool(response_text and response_text.strip()),
        )
    messages = _format_chunks(user_message, response_text, show_question)
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
