"""
font_style — enumerated display-font registry for the Glass UI.

ONE authoritative allow-list of font keys. Every key maps to a fixed,
deterministic transform; arbitrary user strings never become fonts.

Design rules:
- Letters AND standalone digit runs are restyled when the font provides
  glyphs for them. Punctuation, emoji, markdown markers and Persian/Arabic
  text pass through untouched, so mixed sentences stay readable.
- Pure-alpha runs only for letters: a token like ``S0001`` or ``@user123``
  is never partially restyled (the digit part of an alphanumeric token is
  machine-ish data and stays intact).
- Standalone numbers (clock ``12:34``, counters, timestamps) ARE styled
  when the font has styled numerals; fonts without digit glyphs leave
  digits in the system font.
- Inline ``code`` spans and URLs are excluded from styling entirely.
- No external font resources are required; every glyph below renders
  from standard Unicode blocks.

Script support (honest metadata):
- Every decorative style transforms LATIN letters (+digits where noted)
  only. There is no Unicode block that decorates Arabic/Persian script,
  so Persian text ALWAYS renders in the system font — readable and
  uncorrupted, never falsely claimed as styled.
- ``supports_persian_styling(key)`` reports this per key: only the
  default font styles Persian (trivially, by leaving it as-is).
"""
from __future__ import annotations

import re
from typing import Callable

DEFAULT_FONT_KEY = "default"

# A run of ASCII letters, or a standalone run of digits, not adjacent to
# any other alphanumeric character (so IDs like ``S0001`` stay intact).
_STYLE_RUN = re.compile(
    r"(?<![A-Za-z0-9])[A-Za-z]+(?![A-Za-z0-9])"
    r"|(?<![A-Za-z0-9])[0-9]+(?![A-Za-z0-9])"
)
_CODE_SPAN = re.compile(r"`[^`]*`")
_URL = re.compile(r"https?://\S+")


def _math_map(caps_base: int, lower_base: int,
              caps_holes: dict[str, int] | None = None,
              lower_holes: dict[str, int] | None = None,
              digit_map: dict[str, str] | None = None) -> dict[str, str]:
    holes_caps = caps_holes or {}
    holes_lower = lower_holes or {}
    table: dict[str, str] = dict(digit_map or {})
    for i in range(26):
        up = chr(ord("A") + i)
        low = chr(ord("a") + i)
        table[up] = chr(holes_caps.get(up, caps_base + i))
        table[low] = chr(holes_lower.get(low, lower_base + i))
    return table


_SMALL_CAPS = dict(zip(
    "abcdefghijklmnopqrstuvwxyz",
    "ᴀʙᴄᴅᴇꜰɢʜɪᴊᴋʟᴍɴᴏᴘǫʀꜱᴛᴜᴠᴡxʏᴢ",
))


def _small_caps_char(ch: str) -> str:
    return _SMALL_CAPS.get(ch.lower(), ch)


def _offset_family(caps_base: int, lower_base: int) -> Callable[[str], str]:
    def convert(ch: str) -> str:
        o = ord(ch)
        if ord("A") <= o <= ord("Z"):
            return chr(caps_base + o - ord("A"))
        if ord("a") <= o <= ord("z"):
            return chr(lower_base + o - ord("a"))
        return ch
    return convert


def _table_family(table: dict[str, str]) -> Callable[[str], str]:
    def convert(ch: str) -> str:
        return table.get(ch, ch)
    return convert


def _combining(mark: str) -> Callable[[str], str]:
    def convert(ch: str) -> str:
        return ch + mark
    return convert


class FontDef:
    __slots__ = ("key", "label", "convert", "has_digit_glyphs")

    def __init__(self, key: str, label: str, convert: Callable[[str], str],
                 has_digit_glyphs: bool = False):
        self.key = key
        self.label = label
        self.convert = convert
        # True when the transform maps digits to styled numerals (or a
        # generic per-character mark, which applies to digits equally).
        self.has_digit_glyphs = has_digit_glyphs


def _register(registry: dict[str, FontDef], key: str, label: str,
              caps_base: int, lower_base: int,
              caps_holes: dict[str, int] | None = None,
              lower_holes: dict[str, int] | None = None,
              digits_base: int | None = None) -> None:
    digit_map = (
        {str(d): chr(digits_base + d) for d in range(10)}
        if digits_base is not None else {}
    )
    registry[key] = FontDef(
        key, label,
        _table_family(_math_map(caps_base, lower_base, caps_holes, lower_holes, digit_map)),
        has_digit_glyphs=digits_base is not None,
    )


_FONT_REGISTRY: dict[str, FontDef] = {
    DEFAULT_FONT_KEY: FontDef(DEFAULT_FONT_KEY, "Default (system)", lambda ch: ch),
}

# Mathematical serif/sans/script/fraktur families (with their reserved
# codepoint holes mapped to the correct substitute glyphs). Digit bases
# follow the Unicode mathematical-alphanumeric digit runs; families the
# standard reserves no digits for (italic/script/fraktur) leave digits
# in the system font.
_register(_FONT_REGISTRY, "serif_bold", "Serif Bold", 0x1D400, 0x1D41A,
          digits_base=0x1D7CE)
_register(_FONT_REGISTRY, "serif_italic", "Serif Italic", 0x1D434, 0x1D44E,
          lower_holes={"h": 0x210E})
_register(_FONT_REGISTRY, "serif_bold_italic", "Serif Bold Italic", 0x1D468, 0x1D482)
_register(_FONT_REGISTRY, "sans", "Sans", 0x1D5A0, 0x1D5BA,
          digits_base=0x1D7E2)
_register(_FONT_REGISTRY, "sans_bold", "Sans Bold", 0x1D5D4, 0x1D5EE,
          digits_base=0x1D7EC)
_register(_FONT_REGISTRY, "sans_italic", "Sans Italic", 0x1D608, 0x1D622)
_register(_FONT_REGISTRY, "sans_bold_italic", "Sans Bold Italic", 0x1D63C, 0x1D656)
_register(_FONT_REGISTRY, "script", "Script",
          0x1D49C, 0x1D4B6,
          caps_holes={"B": 0x212C, "E": 0x2130, "F": 0x2131, "H": 0x210B,
                      "I": 0x2110, "L": 0x2112, "M": 0x2133, "R": 0x211B},
          lower_holes={"e": 0x212F, "g": 0x210A, "o": 0x2134})
_register(_FONT_REGISTRY, "script_bold", "Script Bold", 0x1D4D0, 0x1D4EA)
_register(_FONT_REGISTRY, "fraktur", "Fraktur", 0x1D504, 0x1D51E,
          caps_holes={"C": 0x212D, "H": 0x210C, "I": 0x2111,
                      "R": 0x211C, "Z": 0x2128})
_register(_FONT_REGISTRY, "fraktur_bold", "Fraktur Bold", 0x1D56C, 0x1D586)
_register(_FONT_REGISTRY, "double_struck", "Double-Struck", 0x1D538, 0x1D552,
          caps_holes={"C": 0x2102, "H": 0x210D, "N": 0x2115, "P": 0x2119,
                      "Q": 0x211A, "R": 0x211D, "Z": 0x2124},
          digits_base=0x1D7D8)
_register(_FONT_REGISTRY, "mono", "Monospace", 0x1D670, 0x1D68A,
          digits_base=0x1D7F6)

def _letter_offset_map(caps_base: int, lower_base: int) -> dict[str, str]:
    table: dict[str, str] = {}
    for i in range(26):
        table[chr(ord("A") + i)] = chr(caps_base + i)
        table[chr(ord("a") + i)] = chr(lower_base + i)
    return table


_CIRCLED_DIGITS = {
    "0": "\u24EA", "1": "\u2460", "2": "\u2461", "3": "\u2462",
    "4": "\u2463", "5": "\u2464", "6": "\u2465", "7": "\u2466",
    "8": "\u2467", "9": "\u2468",
}
_DARK_CIRCLED_DIGITS = {
    "0": "\u24FF", "1": "\u2776", "2": "\u2777", "3": "\u2778",
    "4": "\u2779", "5": "\u277A", "6": "\u277B", "7": "\u277C",
    "8": "\u277D", "9": "\u277E",
}


def _digit_offset_map(digits_base: int) -> dict[str, str]:
    return {str(d): chr(digits_base + d) for d in range(10)}


_FONT_REGISTRY["small_caps"] = FontDef(
    "small_caps", "Small Caps", _table_family({
        ch: _SMALL_CAPS[ch.lower()] for ch in
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        if ch.lower() in _SMALL_CAPS
    }),
)
_FONT_REGISTRY["circled"] = FontDef(
    "circled", "Circled",
    _table_family({**_letter_offset_map(0x24B6, 0x24D0), **_CIRCLED_DIGITS}),
    has_digit_glyphs=True,
)
_FONT_REGISTRY["circled_dark"] = FontDef(
    "circled_dark", "Dark Circled",
    _table_family({**_letter_offset_map(0x1F150, 0x1F150), **_DARK_CIRCLED_DIGITS}),
    has_digit_glyphs=True,
)
_FONT_REGISTRY["fullwidth"] = FontDef(
    "fullwidth", "Wide",
    _table_family({**_letter_offset_map(0xFF21, 0xFF41), **_digit_offset_map(0xFF10)}),
    has_digit_glyphs=True,
)
_FONT_REGISTRY["parenthesized"] = FontDef(
    "parenthesized", "Parenthesized", _offset_family(0x249C, 0x249C),
)
# Combining-mark styles apply their mark per character, so digits are
# underlined/struck equally — readable and consistent.
_FONT_REGISTRY["underline"] = FontDef(
    "underline", "Underline", _combining("\u0332"), has_digit_glyphs=True,
)
_FONT_REGISTRY["strikethrough"] = FontDef(
    "strikethrough", "Strikethrough", _combining("\u0336"), has_digit_glyphs=True,
)
_FONT_REGISTRY["overline"] = FontDef(
    "overline", "Overline", _combining("\u0305"), has_digit_glyphs=True,
)
_FONT_REGISTRY["wavy_underline"] = FontDef(
    "wavy_underline", "Wavy Underline", _combining("\u0330"), has_digit_glyphs=True,
)

#: All valid font keys, in registry order (default first).
FONT_KEYS: tuple[str, ...] = tuple(_FONT_REGISTRY.keys())

_FONT_BY_KEY = _FONT_REGISTRY


def font_has_digit_glyphs(key: str) -> bool:
    """Whether the named style renders styled numerals (else system digits)."""
    font = _FONT_BY_KEY.get(normalize_font_key(key))
    return bool(font is not None and font.has_digit_glyphs)


def font_option_label(key: str) -> str:
    """Self-demonstrating picker label rendered in the option's own style.

    Lives here (not in handlers) so panels stay render-time-only consumers
    of the font system.
    """
    font = _FONT_BY_KEY.get(normalize_font_key(key))
    if font is None:
        return str(key)
    return f"{font.label} · {apply_font('Abc 123', key)}"


def supports_persian_styling(key: str) -> bool:
    """Honest per-key script capability.

    No Unicode decorative block covers Arabic/Persian script, so only the
    default font 'styles' Persian (by leaving it untouched). Decorative
    keys always render Persian via the system font — never corrupted.
    """
    return normalize_font_key(key) == DEFAULT_FONT_KEY


def is_valid_font(key: object) -> bool:
    """Deterministic allow-list check."""
    return isinstance(key, str) and key in _FONT_BY_KEY


def normalize_font_key(key: object) -> str:
    """Return *key* when it is on the allow-list, else the default key."""
    return key if is_valid_font(key) else DEFAULT_FONT_KEY


def style_char(text: str, font_key: str) -> str:
    """Apply the font transform character-by-character (no context rules)."""
    font = _FONT_BY_KEY.get(normalize_font_key(font_key))
    if font is None:
        return text
    return "".join(font.convert(ch) for ch in text)


def apply_font(text: str, font_key: str) -> str:
    """Style *text* with the named font, preserving structure.

    Letters are styled when the font provides glyphs; standalone digit
    runs are styled when the font has styled numerals. Skipped entirely:
    inline ``code`` spans, URLs, punctuation, emoji/markdown markers, and
    any alphanumeric token mixing letters with digits (IDs stay intact).
    Persian/Arabic characters have no decorative equivalents and always
    pass through to the system font.
    """
    if not text:
        return text
    font = _FONT_BY_KEY.get(normalize_font_key(font_key))
    if font is None:
        return text

    def _style_segment(segment: str) -> str:
        return _STYLE_RUN.sub(lambda m: "".join(font.convert(ch) for ch in m.group(0)), segment)

    # Protect code spans and URLs by walking segments directly — no
    # placeholder substitution, so protected bytes can never be restyled
    # or corrupted mid-transform (a sentinel containing digits would be).
    spans = [(m.start(), m.end()) for m in _CODE_SPAN.finditer(text)]
    spans += [(m.start(), m.end()) for m in _URL.finditer(text)]
    if not spans:
        return _style_segment(text)

    parts: list[str] = []
    pos = 0
    for start, end in sorted(spans):
        if start < pos:  # nested/overlapping match — already preserved
            continue
        parts.append(_style_segment(text[pos:start]))
        parts.append(text[start:end])
        pos = end
    parts.append(_style_segment(text[pos:]))
    return "".join(parts)
