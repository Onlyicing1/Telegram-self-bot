"""
Persian/Arabic digit normalization helpers.

Providers may emit Persian digits (۰-۹) or Arabic-Indic digits (٠-٩) inside
numeric arguments. These helpers normalize them to ASCII so the existing
tool validators stay deterministic. Exact-value fields (usernames, URLs,
quoted text) are NEVER normalized by this module — only digit characters are
translated, so ``@SomeUser123`` is preserved verbatim.
"""
from __future__ import annotations

from typing import Any

_PERSIAN_DIGITS = "۰۱۲۳۴۵۶۷۸۹"
_ARABIC_DIGITS = "٠١٢٣٤٥٦٧٨٩"
_ASCII_DIGITS = "0123456789"

_TRANS = str.maketrans(
    _PERSIAN_DIGITS + _ARABIC_DIGITS,
    _ASCII_DIGITS + _ASCII_DIGITS,
)


def normalize_digits(text: str) -> str:
    """Translate Persian/Arabic-Indic digits in *text* to ASCII digits."""
    if not isinstance(text, str):
        return text
    return text.translate(_TRANS)


def coerce_int(value: Any) -> int | None:
    """Coerce a value to int, accepting Persian/Arabic digit strings.

    Returns ``None`` when the value cannot be safely interpreted as an
    integer (so callers fail safe rather than guessing).
    """
    if isinstance(value, bool):
        return None
    if isinstance(value, int):
        return value
    if isinstance(value, float) and value.is_integer():
        return int(value)
    if isinstance(value, str):
        normalized = normalize_digits(value).strip()
        if not normalized:
            return None
        try:
            return int(normalized)
        except ValueError:
            return None
    return None
