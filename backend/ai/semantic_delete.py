"""
Deterministic predicate MATCHING for Delete.

Pure, stateless text normalization and word counting. This module deliberately
does NOT contain:

  - embeddings / vector search / an external semantic service;
  - provider calls or model autonomy;
  - Telegram access, ownership checks, or deletion;
  - any interpretation of the OWNER'S request.

It never decides that a request is a delete, or what it should match. The
model chooses the capability and proposes the predicate as a STRUCTURED
argument (``{"query": ..., "word_count": ...}``); this module validates that
argument (``spec_from_dict``) and answers one question about already-fetched
message text: does this message satisfy the proposed predicate after
deterministic normalization/tokenization (``build_matcher``)?

The natural-language parser that used to read "دو کلمه انگلیسی" out of the
owner's message and build the predicate itself is gone — that was semantic
intent routing competing with the model.

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

# A predicate word-count must be sane: messages with more than 100 lexical
# words are effectively essays, and matching them is ambiguous enough to
# warrant refusing the predicate rather than guessing. This is a BOUNDS check
# on a value the model already proposed.
_MAX_WORD_COUNT = 100

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
