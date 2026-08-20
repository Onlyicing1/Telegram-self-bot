"""
Deterministic semantic predicates for Delete.

Pure, stateless text normalization + structural predicate parsing for
natural Persian/English Delete requests. This module deliberately does NOT
contain:

  - embeddings / vector search / an external semantic service;
  - provider calls or model autonomy;
  - Telegram access, ownership checks, or deletion.

It only answers two questions deterministically:

  1. ``parse_structural_predicate(text)`` — does the request contain a
     clearly defined structural predicate (exact N words / exact N English
     words), and what is it?
  2. ``build_matcher(spec)`` — given a predicate, does a *message text*
     satisfy it after deterministic normalization/tokenization?

``normalize_text`` is the matching form: Persian/Arabic character variants
are folded to Persian, digits are normalized, Arabic diacritics are
removed, apostrophes are stripped, and zero-width characters are removed so
variant spellings of the same word compare equal.

``tokenize`` is the counting form: the same character normalization applies,
but zero-width characters (ZWNJ/ZWJ/ZWSP/BOM) act as word separators — the
conventional Persian word-segmentation treatment — so ``پیام‌های`` and
``پیام های`` both count as two lexical segments.

Word-count semantics (defined precisely so tests and users agree):

  - an *English lexical word* is a token consisting only of ASCII letters
    (after normalization/casefold);
  - a *Persian lexical word* is a token containing any Persian/Arabic
    letter;
  - a *lexical word* (total) is a token containing at least one letter in
    either script;
  - pure digit runs, emoji, and punctuation are never words.

The final Delete authority is unchanged: the service layer re-fetches and
re-verifies ownership before any Telegram deletion. This module can never
authorize deletion; it only filters candidate text.

Every function here is deterministic and side-effect free. Stateless — not
a singleton, no module-level mutable state.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Callable

from backend.ai.persian import coerce_int, normalize_digits

# ── Persian/Arabic character normalization ──────────────────────────────────
# Arabic glyph variants are folded to their Persian forms so variant
# spellings of the same word match (e.g. ك/ک, ي/ی, ة/ه, أ/إ/آ → ا).
_FA_CHAR_TRANS = str.maketrans({
    "\u064a": "ی",  # ARABIC LETTER YEH → PERSIAN YEH
    "\u0649": "ی",  # ARABIC LETTER ALEF MAKSURA → PERSIAN YEH
    "\u0643": "ک",  # ARABIC LETTER KAF → PERSIAN KEHEH
    "\u0629": "ه",  # ARABIC LETTER TEH MARBUTA → HEH
    "\u0623": "ا",  # ALEF WITH HAMZA ABOVE
    "\u0625": "ا",  # ALEF WITH HAMZA BELOW
    "\u0622": "ا",  # ALEF WITH MADDA
    "\u0624": "و",  # WAW WITH HAMZA ABOVE
    "\u0626": "ی",  # YEH WITH HAMZA ABOVE → PERSIAN YEH
})

# Arabic diacritics (fatha, damma, kasra, shadda, sukun, tanwin, superscript
# alef). They are marks, not letters — stripped before matching/counting.
_DIACRITICS_RE = re.compile(r"[\u064b-\u065f\u0670]")

# Zero-width characters. Removed for matching; treated as separators for
# tokenization (ZWNJ is the standard Persian non-joiner).
_ZERO_WIDTH = "\u200b\u200c\u200d\ufeff"

# Lexical tokens: ASCII letters/digits plus Persian/Arabic letters.
_TOKEN_RE = re.compile(r"[a-z0-9\u0621-\u06ff]+")
_EN_WORD_RE = re.compile(r"^[a-z]+$")
_FA_LETTER_RE = re.compile(r"[\u0621-\u06ff]")
_HAS_LETTER_RE = re.compile(r"[a-z\u0621-\u06ff]")

# Persian number words (same vocabulary as the action parser).
_FA_NUMBER_WORDS = {
    "یک": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5, "شش": 6, "هفت": 7,
    "هشت": 8, "نه": 9, "ده": 10, "یازده": 11, "دوازده": 12, "سیزده": 13,
    "چهارده": 14, "پانزده": 15, "شانزده": 16, "هفده": 17, "هجده": 18,
    "نوزده": 19, "بیست": 20, "سی": 30, "چهل": 40, "پنجاه": 50, "شصت": 60,
    "هفتاد": 70, "هشتاد": 80, "نود": 90, "صد": 100, "دویست": 200,
    "سیصد": 300, "چهارصد": 400, "پانصد": 500,
}

# English number words used inside structural predicates ("two-word",
# "three words"). Limited to the common range; digits are the primary form.
_EN_NUMBER_WORDS = {
    "one": 1, "two": 2, "three": 3, "four": 4, "five": 5, "six": 6,
    "seven": 7, "eight": 8, "nine": 9, "ten": 10, "eleven": 11,
    "twelve": 12, "thirteen": 13, "fourteen": 14, "fifteen": 15,
    "sixteen": 16, "seventeen": 17, "eighteen": 18, "nineteen": 19,
    "twenty": 20,
}

_NUMBER_WORDS = {**_FA_NUMBER_WORDS, **_EN_NUMBER_WORDS}

# A structural predicate word-count must be sane: messages with more than
# 100 lexical words are effectively essays, and requesting them is
# ambiguous enough to warrant a controlled clarification instead of a guess.
_MAX_WORD_COUNT = 100

# Token-level "word" markers for the number + word adjacency pattern. The
# Persian marker check also accepts any token starting with "کلمه"/"واژه"
# (کلمه‌ای, کلمهی, واژه‌ها after ZWNJ separation).
_WORD_MARKERS = ("word", "words", "واژه")
_COMPOUND_EN_RE = re.compile(r"^(\d+)(word|words)$")
_COMPOUND_FA_RE = re.compile(r"^(.+?)(کلمه)(ای|ی)?$")

_LANG_ENGLISH_TOKENS = ("english", "انگلیسی")
_LANG_PERSIAN_TOKENS = ("persian", "فارسی")

# Allowed keys in the serialized predicate (tool argument / structured
# action). Anything else is rejected so the model can never smuggle an
# unexpected semantic field through to the Delete service.
_ALLOWED_SPEC_KEYS = frozenset({"query", "word_count", "english_word_count"})


def _char_normalize(text: str) -> str:
    """Apply digit, script-variant, diacritic, apostrophe, case normalization."""
    s = normalize_digits(text)
    s = s.translate(_FA_CHAR_TRANS)
    s = _DIACRITICS_RE.sub("", s)
    s = s.replace("'", "").replace("’", "")
    return s.casefold()


def normalize_text(text: str) -> str:
    """Matching form: normalized text with zero-width characters removed."""
    s = _char_normalize(text)
    for ch in _ZERO_WIDTH:
        s = s.replace(ch, "")
    return re.sub(r"\s+", " ", s).strip()


def tokenize(text: str) -> list[str]:
    """Counting form: zero-width characters act as word separators."""
    s = _char_normalize(text)
    for ch in _ZERO_WIDTH:
        s = s.replace(ch, " ")
    return _TOKEN_RE.findall(s)


def _classify_tokens(tokens: list[str]) -> tuple[int, int, int]:
    """Return (total_lexical_words, english_words, persian_words)."""
    total = 0
    english = 0
    persian = 0
    for tok in tokens:
        is_en = _EN_WORD_RE.match(tok) is not None
        is_fa = _FA_LETTER_RE.search(tok) is not None
        if is_en or is_fa:
            total += 1
        if is_en:
            english += 1
        if is_fa:
            persian += 1
    return total, english, persian


def count_words(text: str) -> tuple[int, int, int]:
    """Return (total_lexical_words, english_words, persian_words) in *text*."""
    return _classify_tokens(tokenize(text))


def total_word_count(text: str) -> int:
    return count_words(text)[0]


def english_word_count(text: str) -> int:
    return count_words(text)[1]


def persian_word_count(text: str) -> int:
    return count_words(text)[2]


@dataclass(frozen=True)
class StructuralPredicate:
    """A deterministic, serializable content predicate for Delete selection.

    All predicates are ANDed when more than one is present:

      - ``query`` — normalized topic substring ('' means "any content");
      - ``word_count`` — the message contains exactly N lexical words;
      - ``english_word_count`` — the message contains exactly N English
        lexical words.
    """

    query: str = ""
    word_count: int | None = None
    english_word_count: int | None = None

    def is_empty(self) -> bool:
        return not (self.query or self.word_count is not None
                    or self.english_word_count is not None)

    def to_dict(self) -> dict[str, Any]:
        out: dict[str, Any] = {}
        if self.query:
            out["query"] = self.query
        if self.word_count is not None:
            out["word_count"] = self.word_count
        if self.english_word_count is not None:
            out["english_word_count"] = self.english_word_count
        return out


def _number_value(tok: str) -> int | None:
    """ASCII/Persian digit string or a Persian/English number word → int."""
    if tok.isdigit():
        try:
            return int(tok)
        except ValueError:
            return None
    return _NUMBER_WORDS.get(tok)


def _compound_number_value(tokens: list[str], idx: int) -> int | None:
    """Resolve a Persian compound number ending at ``idx`` ("بیست و پنج")."""
    total = _number_value(tokens[idx])
    if total is None:
        return None
    j = idx - 1
    while j - 1 >= 0 and tokens[j] == "و" and _number_value(tokens[j - 1]) is not None:
        total += _number_value(tokens[j - 1])
        j -= 2
    return total


def _is_word_marker(tok: str) -> bool:
    return tok in _WORD_MARKERS or tok.startswith("کلمه") or tok.startswith("واژه")


def _detect_language(tokens: list[str]) -> str:
    for tok in tokens:
        if tok in _LANG_ENGLISH_TOKENS or tok.startswith("انگلیسی"):
            return "english"
        if tok in _LANG_PERSIAN_TOKENS or tok.startswith("فارسی"):
            return "persian"
    return ""


def parse_structural_predicate(text: str) -> StructuralPredicate | None:
    """Extract an exact N-word / N-English-word predicate from a request.

    Recognized forms (Persian and English, digits or number words):

      - ``دو کلمه‌ای`` / ``دوکلمهای`` / ``2-word`` / ``two word`` /
        ``three words`` (number immediately before a word marker)
      - ``دقیقاً دو کلمه انگلیسی`` (number + word marker anywhere)

    Returns ``None`` when the request does not contain a clearly defined
    word-count predicate, so callers keep their existing behavior. The
    number must be 1..``_MAX_WORD_COUNT``; anything else is deliberately
    ambiguous and returns ``None`` (never a guessed range).
    """
    if not isinstance(text, str) or not text.strip():
        return None

    tokens = tokenize(text)
    word_count: int | None = None

    # Adjacency pattern: NUMBER immediately before a word marker.
    for i, tok in enumerate(tokens):
        if i == 0:
            continue
        if not _is_word_marker(tok):
            continue
        n = _compound_number_value(tokens, i - 1)
        if n is not None:
            word_count = n
            break

    # Compound tokens: ``دوکلمهای`` (no ZWNJ/space), ``3word``, ...
    if word_count is None:
        for tok in tokens:
            m = _COMPOUND_EN_RE.match(tok)
            if m:
                word_count = int(m.group(1))
                break
            m = _COMPOUND_FA_RE.match(tok)
            if m:
                n = _number_value(m.group(1))
                if n is not None:
                    word_count = n
                    break

    if word_count is None or not (1 <= word_count <= _MAX_WORD_COUNT):
        return None

    lang = _detect_language(tokens)
    if lang == "english":
        return StructuralPredicate(english_word_count=word_count)
    return StructuralPredicate(word_count=word_count)


def spec_from_dict(raw: Any) -> StructuralPredicate | None:
    """Validate a serialized predicate dict; return None when malformed.

    Fail-closed: unknown keys, wrong types, out-of-range counts, or an
    empty predicate are all rejected. The Delete tool re-validates through
    this function before anything reaches Telegram.
    """
    if not isinstance(raw, dict):
        return None
    if not set(raw).issubset(_ALLOWED_SPEC_KEYS):
        return None

    query = raw.get("query")
    if query is None:
        query = ""
    if not isinstance(query, str):
        return None

    counts: dict[str, int | None] = {}
    for key in ("word_count", "english_word_count"):
        value = raw.get(key)
        if value is None:
            counts[key] = None
            continue
        n = coerce_int(value)
        if n is None or n < 1 or n > _MAX_WORD_COUNT:
            return None
        counts[key] = n

    spec = StructuralPredicate(
        query=query.strip(),
        word_count=counts["word_count"],
        english_word_count=counts["english_word_count"],
    )
    if spec.is_empty():
        return None
    return spec


def build_matcher(spec: StructuralPredicate) -> Callable[[str], bool]:
    """Return a pure text predicate implementing *spec*.

    The matcher normalizes message text and compares exactly:

      - topic: normalized substring containment;
      - word counts: exact equality of lexical words per the definitions in
        this module.

    It never touches Telegram and never decides ownership.
    """
    needle = normalize_text(spec.query)

    def _match(text: str) -> bool:
        if needle and needle not in normalize_text(text):
            return False
        total, english, _persian = _classify_tokens(tokenize(text))
        if spec.word_count is not None and total != spec.word_count:
            return False
        if spec.english_word_count is not None and english != spec.english_word_count:
            return False
        return True

    return _match


def build_matcher_from_dict(raw: Any) -> Callable[[str], bool] | None:
    """Validate *raw* and build a matcher; None when the predicate is invalid."""
    spec = spec_from_dict(raw)
    if spec is None:
        return None
    return build_matcher(spec)
