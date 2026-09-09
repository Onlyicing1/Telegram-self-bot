"""Deterministic content policy for AI-prepared task actions.

A task's ``ai_instruction`` may constrain the content the model generates
(e.g. "50-character Persian bio"). Those constraints must be enforced by
DETERMINISTIC code, not trusted to the model's self-report: preparation
output is validated against this policy before it can ever be executed,
and rejected output is regenerated — never truncated, never guessed.

Named-source requests are carried through the contract as GENERATED
in-character dialogue: the content must open with the requested source's
name followed by a separator (a deterministic token match — never a regex,
never a corpus lookup), so a different speaker, a short form, or
unattributed generic text is rejected and regenerated. The system never
claims canonical-quote verification: an explicit exact-quote request fails
closed because no trusted source corpus or independent verifier is
configured, and a speaker label is enforced only as self-attribution of the
generated line — never presented as an authenticated quotation.

Language policy is DERIVED FROM THE INSTRUCTION ONLY. If the instruction
does not name a language, no language constraint is imposed. Detection is
script-based, not ASCII-based: digits, punctuation, whitespace, emoji and
language-appropriate marks never count as violations.
"""
from __future__ import annotations

import json
import re
import unicodedata
from dataclasses import dataclass
from typing import Any

# String argument fields that carry generated CONTENT. Template fields are
# excluded: a template legitimately contains {token} placeholders whose
# rendered length/script differ from the literal string.
CONTENT_FIELDS = frozenset({"text", "message", "content", "body"})
MAX_PREPARATION_ATTEMPTS = 3

_PERSIAN_WORDS = ("فارسی", "پارسی", "persian", "farsi")
_CHINESE_WORDS = ("چینی", "chinese", "中文", "汉语", "mandarin")
_ARABIC_WORDS = ("عربی", "arabic")

# "dialogue from <X>" — the source/person/character constraint. Marker
# TOKENS in the languages the owner actually uses; matching is token-based
# (a marker only matches a standalone word), so prose like "استفاده" or
# "because" never falsely triggers. The extracted source name is the text
# AFTER the marker up to the next delimiter. This is a fixed-vocabulary
# marker scan, NOT a sentence-pattern parser: everything about the CONTENT
# itself stays the AI's semantic job.
_SOURCE_MARKER_TOKENS = ("از", "از طرف", "from", "aus", "の", "의")

# Source extraction stops at these tokens (whole-word, lowercased): they
# introduce the NEXT clause/requirement, not the source name. Kept as a
# fixed vocabulary so Persian compound names like "آیانامی ری" survive while
# verbs/delimiters terminate the phrase.
_SOURCE_STOP_TOKENS = frozenset({
    # conjunctives / prepositions / next-requirement words
    "و", "با", "زیر", "باید", "در", "روی", "هر", "دقیقه", "ثانیه", "به", "رو",
    # change-verbs that follow the name ("... از آیانامی ری تغییر بده")
    "تغییر", "عوض", "بده", "کن", "بکن", "بذار", "ست", "کنه", "بشه", "شده",
    "and", "with", "under", "must", "below", "every", "each", "to",
    "change", "set", "update", "make", "put",
})

# "X از Y" where X is one of these means INSTRUMENTAL "using X from Y",
# not attribution ("استفاده از متن ذخیره شده" = using the saved text).
# Such a marker is skipped, so an instrumental از never pins a bogus
# source — a wrong pin would fail every generation attempt.
_INSTRUMENTAL_TOKENS = frozenset({
    "استفاده", "بر", "اساس", "با", "طبق", "مطابق",
    "using", "based", "per", "according", "with",
})

# Content head nouns: "<head noun> از <name>" marks the ATTRIBUTIVE marker
# ("یه دیالوگ رندوم از آیانامی ری"). When several "از" markers compete
# ("... از آیانامی ری از انیمه نئون جنسیس ..."), the marker nearest a head
# noun names the requested source; the later clause is the source's own
# context (anime), not the source. Preference is bounded to the 3 tokens
# before a marker so unrelated clauses cannot hijack it.
_SOURCE_HEAD_NOUNS = frozenset({
    "دیالوگ", "تکست", "متن", "گفتگو", "جمله", "خط", "کلام", "سخن",
    "مونولوگ", "جمله‌ای", "عبارت",
    "quote", "dialogue", "line", "text", "speech", "sentence",
    "monologue", "phrase", "remark", "saying", "dialog",
})

# Descriptors that may PREFIX the name after the marker ("از کاراکتر
# آیانامی ری") — skipped so the pinned source is the name itself.
_SOURCE_DESCRIPTORS = frozenset({
    "کاراکتر", "شخصیت", "نقش", "شخص",
    "character", "person", "figure", "بازیگر",
})

# Explicit EXACT-QUOTE requests ("نقل قول دقیق", "verbatim quote"). For
# these, generated self-attributed content is NOT acceptable: without a
# trusted corpus the system cannot authenticate a canonical quotation, so
# the occurrence fails closed instead of presenting a guess as a quote.
_EXACT_QUOTE_MARKERS = (
    "نقل قول", "نقل‌قول", "عین جمله", "کلمه به کلمه",
    "exact quote", "verbatim", "word for word", "word-for-word",
)

# Deterministic self-attribution tokens. A generated dialogue line from a
# named source must OPEN with the source's full name (all tokens, spoken
# order, case-insensitive) followed by a dialogue separator — the only
# deterministic signal that the line is attributed to the requested source.
# This rejects a different speaker, a short form, an in-text mention, and
# unattributed generic text. It does NOT authenticate canon: the system
# never claims the line is an exact quotation.
_ATTRIBUTION_SEPARATORS = frozenset({":", "؛", "،", ",", "-", "—", "–", "(", "«", "「", "（"})
_ATTRIBUTION_NAME_SUFFIX = frozenset(":؛،,;.؟?!-—–»”」)'\"")
_ATTRIBUTION_OPENERS = frozenset({'"', "'", "«", "“", "「", "("})

# "below/under N" length words — a MAXIMUM, never an exact requirement.
# Persian "زیر" and its English siblings are matched as whole tokens.
_MAX_LENGTH_TOKENS = ("زیر", "کمتر", "below", "under", "less", "fewer", "max", "maximum")

# A "mixed unrelated script" violation means LETTERS of another script, not
# punctuation/digits/emoji. Letter categories: L* (Lu, Ll, Lt, Lm, Lo).
_LatinLetter = re.compile(r"[A-Za-z]")
_ArabicLetter = re.compile(r"[\u0600-\u06FF\u0750-\u077F\uFB50-\uFDFF\uFE70-\uFEFF]")
_HanLetter = re.compile(r"[\u3400-\u4DBF\u4E00-\u9FFF\U00020000-\U0002A6DF]")

# Provider-failure wrappers and JSON payloads are never the requested content.
_JSON_OPENERS = ('{"', "[{", '[ "', "{\n", "[\n")
_FAILURE_MARKERS = ("error", "failed", "exception", "traceback", "api key", "unauthorized")


class PreparationPolicyError(ValueError):
    """Prepared content violates the task's deterministic content policy."""


@dataclass(frozen=True)
class PreparationPolicy:
    """Validated content constraints derived from one task instruction."""

    language: str | None = None  # None | "persian" | "chinese" | "arabic"
    exact_length: int | None = None
    max_length: int | None = None
    length_text: str = ""
    source: str = ""       # required source/person/character, "" = unconstrained
    source_text: str = ""  # the raw spoken source phrase (diagnostics)
    quote_exact: bool = False  # explicit exact-canonical-quote request: fails closed

    @property
    def active(self) -> bool:
        return (
            self.language is not None
            or self.exact_length is not None
            or self.max_length is not None
            or bool(self.source)
        )

    def describe(self) -> str:
        parts = []
        if self.language is not None:
            parts.append(f"language={self.language}")
        if self.exact_length is not None:
            parts.append(f"exactly {self.exact_length} characters")
        elif self.max_length is not None:
            parts.append(f"at most {self.max_length} characters")
        if self.source:
            if self.quote_exact:
                parts.append(
                    f"requested source/person/character={self.source}; EXACT CANONICAL "
                    f"QUOTE requested — no trusted source corpus/verifier is configured, "
                    f"so content fails closed (never presented as an authenticated quote)"
                )
            else:
                parts.append(
                    f"requested source/person/character={self.source}; content MUST BE "
                    f"a GENERATED dialogue from that source: open with "
                    f"'{self.source}:' followed by the line — never another speaker, "
                    f"never a short form, never generic text"
                )
        return ", ".join(parts) if parts else "no content constraints"


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in words)


def _extract_source(text: str) -> tuple[str, str]:
    """Extract the spoken source/person/character from the instruction.

    Returns ``(source, raw_phrase)`` — or ``("", "")`` when no source
    marker appears. This is a fixed-vocabulary marker scan over whitespace
    tokens, not a sentence-pattern parser. It preserves the source identity
    for the durable contract; it does not authenticate generated content.

    Attribution markers are ranked: instrumental "X از Y" (استفاده/using) is
    skipped; among the remaining markers, the LAST one whose 3-token prefix
    window contains a content head noun (دیالوگ/quote/…) wins — that is the
    "<content> از <source>" construct — and otherwise the LAST marker wins
    (it sits closest to the actual source in a plain single-clause request).
    The name phrase after the winning marker stops at delimiters, another
    marker, or a change-verb/clause token.
    """
    if not isinstance(text, str) or not text.strip():
        return "", ""
    raw_words = text.split()
    lowered_tokens = [w.lower() for w in raw_words]
    attributive: list[int] = []
    for i, tok in enumerate(lowered_tokens):
        if tok not in _SOURCE_MARKER_TOKENS:
            continue
        # "X از Y" with instrumental X (استفاده/using/…) is not attribution.
        if i > 0 and lowered_tokens[i - 1] in _INSTRUMENTAL_TOKENS:
            continue
        attributive.append(i)
    if not attributive:
        return "", ""
    marker_idx = attributive[-1]
    for i in reversed(attributive):
        window = lowered_tokens[max(0, i - 3):i]
        if any(tok in _SOURCE_HEAD_NOUNS for tok in window):
            marker_idx = i
            break
    collected = []
    for word in raw_words[marker_idx + 1:marker_idx + 7]:
        stripped = word.strip("\u060c،,.؛:!؟?\"'")
        if not stripped:
            break
        lowered = stripped.lower()
        if lowered in _SOURCE_STOP_TOKENS or lowered in _SOURCE_MARKER_TOKENS:
            break
        if not collected and lowered in _SOURCE_DESCRIPTORS:
            continue
        collected.append(stripped)
        if len(collected) >= 4:
            break
    raw_phrase = " ".join(raw_words[marker_idx + 1:marker_idx + 7]).strip()
    source = " ".join(collected).strip()
    if not source or len(source) > 64:
        return "", ""
    return source, raw_phrase


def derive_policy(instruction: str) -> PreparationPolicy:
    """Derive the content policy from the task instruction (deterministic).

    Only EXPLICIT requirements constrain the output: a named language, a
    numeric character requirement, and a named source/person/character
    written into the instruction. Persian and Arabic-Indic digits are
    accepted in the numeric expressions.
    """
    if not isinstance(instruction, str) or not instruction.strip():
        return PreparationPolicy()

    language: str | None = None
    if _contains_any(instruction, _PERSIAN_WORDS):
        language = "persian"
    elif _contains_any(instruction, _CHINESE_WORDS):
        language = "chinese"
    elif _contains_any(instruction, _ARABIC_WORDS):
        language = "arabic"

    exact_length: int | None = None
    max_length: int | None = None
    length_text = ""
    source_text = ""

    normalized = instruction.translate(str.maketrans("۰۱۲۳۴۵۶۷۸۹٠١٢٣٤٥٦٧٨٩", "01234567890123456789"))
    exact_patterns = (
        r"(?:دقیقا|دقیق|همان|exactly)\D{0,12}(\d{1,5})\s*(?:کاراکتر|نویسه|character|char|حرف)",
        r"(\d{1,5})\s*(?:کاراکتر|نویسه|character|char|حرف)\s*(?:دقیق|exactly|exact)",
    )
    max_patterns = (
        r"(?:حداکثر|نهایتا|at most|no more than|maximum of|max of|max)\D{0,12}(\d{1,5})\s*(?:کاراکتر|نویسه|character|char|حرف)",
        r"(\d{1,5})\s*(?:کاراکتر|نویسه|character|char|حرف)\s*(?:حداکثر|at most|or fewer|or less)",
    )
    for pattern in exact_patterns:
        match = re.search(pattern, normalized, re.IGNORECASE)
        if match:
            exact_length = int(match.group(1))
            length_text = match.group(0)
            break
    if exact_length is None:
        for pattern in max_patterns:
            match = re.search(pattern, normalized, re.IGNORECASE)
            if match:
                max_length = int(match.group(1))
                length_text = match.group(0)
                break
    # A bare "N-character" statement (e.g. "50-character dialogue") is an
    # EXACT requirement — UNLESS a below/under word governs the number
    # ("زیر 60 کاراکتر", "below 60 characters"), which is a MAXIMUM of N-1:
    # "under 60" is satisfied by 59, and exact-60 semantics would wrongly
    # reject every valid shorter bio.
    if exact_length is None and max_length is None:
        match = re.search(r"(\d{1,5})[-\s]*(?:کاراکتری|کاراکتر|character|char|حرف)", normalized, re.IGNORECASE)
        if match:
            prefix = normalized[max(0, match.start() - 12):match.start()].lower()
            if any(token in prefix for token in _MAX_LENGTH_TOKENS):
                max_length = int(match.group(1)) - 1
                length_text = match.group(0)
            else:
                exact_length = int(match.group(1))
                length_text = match.group(0)

    source, source_phrase = _extract_source(instruction)
    quote_exact = bool(source) and _contains_any(instruction, _EXACT_QUOTE_MARKERS)

    return PreparationPolicy(
        language=language,
        exact_length=exact_length if exact_length and exact_length > 0 else None,
        max_length=max_length if max_length and max_length > 0 else None,
        length_text=length_text,
        source=source,
        source_text=source_phrase,
        quote_exact=quote_exact,
    )


def _letters(text: str) -> str:
    return "".join(ch for ch in text if unicodedata.category(ch).startswith("L"))


def _check_language(text: str, language: str) -> None:
    letters = _letters(text)
    if not letters:
        # Punctuation-only output is rejected by the garbage check; here a
        # language constraint with no letters at all cannot prove compliance.
        raise PreparationPolicyError(f"content contains no letters to satisfy the {language} language requirement")
    if language == "persian":
        if _LatinLetter.search(letters):
            raise PreparationPolicyError("content must be Persian-only but contains Latin letters")
        if not _ArabicLetter.search(letters):
            raise PreparationPolicyError("content must be Persian but contains no Persian/Arabic script")
    elif language == "arabic":
        if _LatinLetter.search(letters):
            raise PreparationPolicyError("content must be Arabic-only but contains Latin letters")
        if not _ArabicLetter.search(letters):
            raise PreparationPolicyError("content must be Arabic but contains no Arabic script")
    elif language == "chinese":
        if _ArabicLetter.search(letters) or _LatinLetter.search(letters):
            raise PreparationPolicyError("content must be Chinese-only but contains Persian/Arabic or Latin letters")
        if not _HanLetter.search(letters):
            raise PreparationPolicyError("content must be Chinese but contains no Han characters")


def _check_length(text: str, policy: PreparationPolicy) -> None:
    # Unicode-aware character count: len() counts code points, which is the
    # clearly defined rule for "N characters" here. NEVER truncate.
    count = len(text)
    if policy.exact_length is not None and count != policy.exact_length:
        raise PreparationPolicyError(
            f"content must be exactly {policy.exact_length} characters but is {count} (rejected, not truncated)"
        )
    if policy.max_length is not None and count > policy.max_length:
        raise PreparationPolicyError(
            f"content must be at most {policy.max_length} characters but is {count} (rejected, not truncated)"
        )


def _check_garbage(text: str) -> None:
    stripped = text.strip()
    if not stripped:
        raise PreparationPolicyError("content is empty")
    lowered = stripped.lower()
    if stripped.startswith(_JSON_OPENERS) or stripped.startswith("```"):
        try:
            json.loads(stripped.strip("` \n").replace("json\n", "", 1)) if not stripped.startswith("{") else json.loads(stripped)
            raise PreparationPolicyError("content is a JSON payload, not the requested content")
        except json.JSONDecodeError:
            pass
    if any(marker in lowered for marker in _FAILURE_MARKERS) and len(stripped) < 200:
        raise PreparationPolicyError("content looks like a provider failure payload, not the requested content")


def _check_attribution(text: str, source: str) -> None:
    """Deterministic self-attribution: the line must OPEN with the requested
    source's name (all tokens, spoken order, case-insensitive) followed by a
    dialogue separator and the line itself.

    Token-based — no regex, no corpus, no model trust beyond the exact name
    match. A different speaker, a short form, an in-text mention, and
    unattributed generic text are all rejected.
    """
    source_tokens = source.split()
    if not source_tokens:
        return
    stripped = text.lstrip()
    while stripped and stripped[0] in _ATTRIBUTION_OPENERS:
        stripped = stripped[1:].lstrip()
    parts = stripped.split(maxsplit=len(source_tokens))
    if len(parts) < len(source_tokens):
        raise PreparationPolicyError(
            f"content must be a dialogue attributed to {source!r}: open with "
            f"'{source}:' followed by the line"
        )
    name_tokens = parts[: len(source_tokens)]
    last = name_tokens[-1]
    if last.lower() == source_tokens[-1].lower():
        separator_attached = False
    else:
        core = last.rstrip("".join(sorted(_ATTRIBUTION_NAME_SUFFIX)))
        if core.lower() != source_tokens[-1].lower():
            raise PreparationPolicyError(
                f"content is attributed to a different speaker; expected the "
                f"requested source {source!r} as the opening speaker"
            )
        separator_attached = True
    for expected, actual in zip(source_tokens[:-1], name_tokens[:-1], strict=True):
        if expected.lower() != actual.lower():
            raise PreparationPolicyError(
                f"content is attributed to a different speaker; expected the "
                f"requested source {source!r} as the opening speaker"
            )
    rest = " ".join(parts[len(source_tokens):]).strip()
    if separator_attached:
        line = rest
    else:
        if not rest or rest[0] not in _ATTRIBUTION_SEPARATORS:
            raise PreparationPolicyError(
                f"content must open with {source!r} followed by a separator "
                f"and the dialogue line"
            )
        line = rest[1:].strip()
    if not line:
        raise PreparationPolicyError(
            f"content must include the dialogue line after the {source!r} attribution"
        )


def validate_content(text: Any, policy: PreparationPolicy) -> str:
    """Validate one generated content string; return it unchanged when valid.

    Fail-closed: raises PreparationPolicyError on any violation. The text is
    never modified, truncated, or repaired.
    """
    if not isinstance(text, str):
        raise PreparationPolicyError("content must be a string")
    _check_garbage(text)
    if policy.source:
        if policy.quote_exact:
            # An explicit exact-canonical-quote request cannot be honored
            # without a trusted corpus/verifier: a generated line — even
            # correctly self-attributed — is not an authenticated quotation.
            raise PreparationPolicyError(
                f"exact canonical quote from {policy.source!r} cannot be "
                "independently verified: no trusted source corpus or verifier "
                "is configured"
            )
        # Generated in-character dialogue: deterministic self-attribution is
        # enforced (a provider label is never treated as canon, only as the
        # line's own opening attribution).
        _check_attribution(text, policy.source)
    if policy.language is not None:
        _check_language(text, policy.language)
    _check_length(text, policy)
    return text


def validate_prepared_arguments(arguments: dict[str, Any], policy: PreparationPolicy) -> None:
    """Validate the CONTENT fields of one prepared action's arguments.

    Non-content fields (ids, toggles, templates) are not constrained here;
    structural safety is enforced by the task contract and the coordinator.
    """
    if not isinstance(arguments, dict):
        return
    found_content = False
    for field in CONTENT_FIELDS:
        value = arguments.get(field)
        if isinstance(value, str):
            found_content = True
            validate_content(value, policy)
    if policy.source and not found_content:
        raise PreparationPolicyError(
            f"source-specific content for {policy.source!r} has no verifiable content field"
        )
