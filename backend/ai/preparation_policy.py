"""Deterministic content policy for AI-prepared task actions.

A task's ``ai_instruction`` may constrain the content the model generates
(e.g. "50-character Persian bio"). Those constraints must be enforced by
DETERMINISTIC code, not trusted to the model's self-report: preparation
output is validated against this policy before it can ever be executed,
and rejected output is regenerated — never truncated, never guessed.

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

# Persian "head noun" + possessive ezâfe: "دیالوگی از آیانامی ری" — a
# dialogue/quote word near the marker confirms the attributive reading.
_SOURCE_HEAD_NOUNS = ("دیالوگ", "دیالوگی", "جمله", "جمله‌ای", "نقل", "قول", "دیالوگها", "دیالوگ‌ها")
_SOURCE_HEAD_NOUNS_EN = ("dialogue", "dialogues", "quote", "quotes", "line", "lines", "saying")

# "X از Y" where X is one of these means INSTRUMENTAL "using X from Y",
# not attribution ("استفاده از متن ذخیره شده" = using the saved text).
# Such a marker is skipped, so an instrumental از never pins a bogus
# source — a wrong pin would fail every generation attempt.
_INSTRUMENTAL_TOKENS = frozenset({
    "استفاده", "بر", "اساس", "با", "طبق", "مطابق",
    "using", "based", "per", "according", "with",
})

# A generated line in "<Speaker>: <text>" form attributes itself to a
# speaker. When the task pins the source, deterministic validation enforces
# this form (bounded prefix), because a bare line cannot be proven to come
# from the required source by any deterministic means — the AI prompt says
# so, and the enforcement boundary makes drift fail closed.
_SPEAKER_PREFIX_MAX = 48

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
    source: str = ""           # required source/person/character, "" = unconstrained
    source_text: str = ""      # the raw spoken source phrase (diagnostics)
    speaker_prefix_required: bool = False  # demand "<Source>: <line>" form

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
            parts.append(
                f"content MUST BE a dialogue/quote SPOKEN BY {self.source} "
                f"(or a narration line ABOUT them from their story) — never "
                f"another character, never generic dialogue"
            )
            if self.speaker_prefix_required:
                parts.append(
                    f"the line MUST start with the speaker prefix "
                    f"\"{self.source}: \" (then the dialogue text)"
                )
        return ", ".join(parts) if parts else "no content constraints"


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in words)


def _extract_source(text: str) -> tuple[str, bool, str]:
    """Extract the spoken source/person/character from the instruction.

    Returns ``(source, head_noun_present, raw_phrase)`` — or
    ``("", False, "")`` when no source marker appears. This is a
    fixed-vocabulary MARKER scan over whitespace tokens, not a sentence
    parser: it identifies the construct "content FROM <name>" in the
    owner's languages and reads the name that follows. Everything the model
    must KNOW about the source stays its semantic job; only the name is
    pinned so it can be enforced deterministically.

    The LAST marker wins: later markers sit closer to the actual source;
    earlier ones typically belong to an unrelated clause. The head-noun
    window looks back a few tokens so "یه دیالوگ رندوم از آیانامی ری"
    ("a random dialogue from Ayanami Rei") still sees the noun.
    """
    if not isinstance(text, str) or not text.strip():
        return "", False, ""
    raw_words = text.split()
    lowered_tokens = [w.lower() for w in raw_words]
    marker_idx = -1
    for i, tok in enumerate(lowered_tokens):
        if tok not in _SOURCE_MARKER_TOKENS:
            continue
        # "X از Y" with instrumental X (استفاده/using/…) is not attribution:
        # skip it so the last ATTRIBUTIVE marker decides the source.
        if i > 0 and lowered_tokens[i - 1] in _INSTRUMENTAL_TOKENS:
            continue
        marker_idx = i
    if marker_idx < 0:
        return "", False, ""
    head_noun = any(
        tok in _SOURCE_HEAD_NOUNS or tok in _SOURCE_HEAD_NOUNS_EN
        for tok in lowered_tokens[max(0, marker_idx - 3):marker_idx]
    )
    collected = []
    for word in raw_words[marker_idx + 1:marker_idx + 7]:
        stripped = word.strip("\u060c،,.؛:!؟?\"'")
        if not stripped:
            break
        if stripped.lower() in _SOURCE_STOP_TOKENS:
            break
        collected.append(stripped)
        if len(collected) >= 4:
            break
    raw_phrase = " ".join(raw_words[marker_idx + 1:marker_idx + 7]).strip()
    source = " ".join(collected).strip()
    if not source or len(source) > 64:
        return "", False, ""
    return source, head_noun, raw_phrase


def _speaker_prefix_ok(text: str, source: str) -> bool:
    """True when the line carries a "<Speaker>:" prefix naming the source.

    Case variants (Persian and Latin) are tolerated; the prefix must appear
    within a bounded head of the line so a random mention of the source
    deep in the text cannot satisfy it.
    """
    source_normalized = " ".join(source.split()).casefold()
    head = text[:_SPEAKER_PREFIX_MAX]
    colon = head.find(":")
    if colon <= 0 or colon > _SPEAKER_PREFIX_MAX - 2:
        return False
    speaker = head[:colon].strip(" \t\u200c\u0640\u00ab\u00bb\"'")
    if not speaker:
        return False
    return " ".join(speaker.split()).casefold() == source_normalized


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

    source, head_noun, source_phrase = _extract_source(instruction)

    return PreparationPolicy(
        language=language,
        exact_length=exact_length if exact_length and exact_length > 0 else None,
        max_length=max_length if max_length and max_length > 0 else None,
        length_text=length_text,
        source=source,
        source_text=source_phrase,
        speaker_prefix_required=bool(source),
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


def validate_content(text: Any, policy: PreparationPolicy) -> str:
    """Validate one generated content string; return it unchanged when valid.

    Fail-closed: raises PreparationPolicyError on any violation. The text is
    never modified, truncated, or repaired.
    """
    if not isinstance(text, str):
        raise PreparationPolicyError("content must be a string")
    _check_garbage(text)
    if policy.language is not None:
        _check_language(text, policy.language)
    _check_length(text, policy)
    if policy.speaker_prefix_required and not _speaker_prefix_ok(text, policy.source):
        raise PreparationPolicyError(
            f"content must be a dialogue attributed to {policy.source} as "
            f"\"{policy.source}: <text>\" but no such speaker prefix was found"
        )
    return text


def validate_prepared_arguments(arguments: dict[str, Any], policy: PreparationPolicy) -> None:
    """Validate the CONTENT fields of one prepared action's arguments.

    Non-content fields (ids, toggles, templates) are not constrained here;
    structural safety is enforced by the task contract and the coordinator.
    """
    if not isinstance(arguments, dict):
        return
    for field in CONTENT_FIELDS:
        value = arguments.get(field)
        if isinstance(value, str):
            validate_content(value, policy)
