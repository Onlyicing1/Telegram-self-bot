"""
font_style — enumerated display-font registry for the Glass UI.

ONE authoritative allow-list of font keys. Every key maps to a fixed,
deterministic transform; arbitrary user strings never become fonts.

Design rules:
- Only ``[A-Za-z]`` letters are restyled. Digits, punctuation, emoji,
  markdown markers and Persian/Arabic text pass through untouched, so
  IDs (``S0001``, chat IDs), code spans and URLs stay copy-friendly and
  mixed Persian/English text stays readable.
- Pure-alpha runs only: a token like ``S0001`` or ``@user123`` is never
  partially restyled.
- Inline ``code`` spans and URLs are excluded from styling entirely.
- No external font resources are required; every glyph below renders
  from standard Unicode blocks.

Persian note (honest): no Unicode mathematical/alphanumeric block covers
Arabic/Persian script, so every style leaves Persian characters in the
system font while Latin text is styled. Mixed sentences remain fully
readable — this is disclosed rather than claimed as Persian support.
"""
from __future__ import annotations

import re
from typing import Callable

DEFAULT_FONT_KEY = "default"

# A run of ASCII letters not adjacent to any other alphanumeric character.
_ALPHA_RUN = re.compile(r"(?<![A-Za-z0-9])[A-Za-z]+(?![A-Za-z0-9])")
_CODE_SPAN = re.compile(r"`[^`]*`")
_URL = re.compile(r"https?://\S+")


def _math_map(caps_base: int, lower_base: int,
              caps_holes: dict[str, int] | None = None,
              lower_holes: dict[str, int] | None = None) -> dict[str, str]:
    holes_caps = caps_holes or {}
    holes_lower = lower_holes or {}
    table: dict[str, str] = {}
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
    __slots__ = ("key", "label", "convert")

    def __init__(self, key: str, label: str, convert: Callable[[str], str]):
        self.key = key
        self.label = label
        self.convert = convert


def _register(registry: dict[str, FontDef], key: str, label: str,
              caps_base: int, lower_base: int,
              caps_holes: dict[str, int] | None = None,
              lower_holes: dict[str, int] | None = None) -> None:
    registry[key] = FontDef(
        key, label,
        _table_family(_math_map(caps_base, lower_base, caps_holes, lower_holes)),
    )


_FONT_REGISTRY: dict[str, FontDef] = {
    DEFAULT_FONT_KEY: FontDef(DEFAULT_FONT_KEY, "Default (system)", lambda ch: ch),
}

# Mathematical serif/sans/script/fraktur families (with their reserved
# codepoint holes mapped to the correct substitute glyphs).
_register(_FONT_REGISTRY, "serif_bold", "Serif Bold", 0x1D400, 0x1D41A)
_register(_FONT_REGISTRY, "serif_italic", "Serif Italic", 0x1D434, 0x1D44E,
          lower_holes={"h": 0x210E})
_register(_FONT_REGISTRY, "serif_bold_italic", "Serif Bold Italic", 0x1D468, 0x1D482)
_register(_FONT_REGISTRY, "sans", "Sans", 0x1D5A0, 0x1D5BA)
_register(_FONT_REGISTRY, "sans_bold", "Sans Bold", 0x1D5D4, 0x1D5EE)
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
                      "Q": 0x211A, "R": 0x211D, "Z": 0x2124})
_register(_FONT_REGISTRY, "mono", "Monospace", 0x1D670, 0x1D68A)

_FONT_REGISTRY["small_caps"] = FontDef(
    "small_caps", "Small Caps", _table_family({
        ch: _SMALL_CAPS[ch.lower()] for ch in
        "ABCDEFGHIJKLMNOPQRSTUVWXYZabcdefghijklmnopqrstuvwxyz"
        if ch.lower() in _SMALL_CAPS
    }),
)
_FONT_REGISTRY["circled"] = FontDef(
    "circled", "Circled", _offset_family(0x24B6, 0x24D0),
)
_FONT_REGISTRY["circled_dark"] = FontDef(
    "circled_dark", "Dark Circled", _offset_family(0x1F150, 0x1F150),
)
_FONT_REGISTRY["fullwidth"] = FontDef(
    "fullwidth", "Wide", _offset_family(0xFF21, 0xFF41),
)
_FONT_REGISTRY["parenthesized"] = FontDef(
    "parenthesized", "Parenthesized", _offset_family(0x249C, 0x249C),
)
_FONT_REGISTRY["underline"] = FontDef(
    "underline", "Underline", _combining("\u0332"),
)
_FONT_REGISTRY["strikethrough"] = FontDef(
    "strikethrough", "Strikethrough", _combining("\u0336"),
)
_FONT_REGISTRY["overline"] = FontDef(
    "overline", "Overline", _combining("\u0305"),
)
_FONT_REGISTRY["wavy_underline"] = FontDef(
    "wavy_underline", "Wavy Underline", _combining("\u0330"),
)

#: All valid font keys, in registry order (default first).
FONT_KEYS: tuple[str, ...] = tuple(_FONT_REGISTRY.keys())

_FONT_BY_KEY = _FONT_REGISTRY


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

    Skipped from styling: inline ``code`` spans, URLs, digits,
    punctuation, emoji/markdown markers, and any token that mixes letters
    with digits (IDs stay intact).
    """
    if not text:
        return text
    font = _FONT_BY_KEY.get(normalize_font_key(font_key))
    if font is None:
        return text

    protected: list[tuple[int, str]] = []

    def _protect(match: re.Match) -> str:
        idx = len(protected)
        protected.append((idx, match.group(0)))
        return f"\x00{idx}\x00"

    masked = _CODE_SPAN.sub(_protect, text)
    masked = _URL.sub(_protect, masked)

    def _style_run(match: re.Match) -> str:
        return "".join(font.convert(ch) for ch in match.group(0))

    styled = _ALPHA_RUN.sub(_style_run, masked)

    for idx, original in protected:
        styled = styled.replace(f"\x00{idx}\x00", original)
    return styled
