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

    @property
    def active(self) -> bool:
        return self.language is not None or self.exact_length is not None or self.max_length is not None

    def describe(self) -> str:
        parts = []
        if self.language is not None:
            parts.append(f"language={self.language}")
        if self.exact_length is not None:
            parts.append(f"exactly {self.exact_length} characters")
        elif self.max_length is not None:
            parts.append(f"at most {self.max_length} characters")
        return ", ".join(parts) if parts else "no content constraints"


def _contains_any(text: str, words: tuple[str, ...]) -> bool:
    lowered = text.lower()
    return any(word in lowered for word in words)


def derive_policy(instruction: str) -> PreparationPolicy:
    """Derive the content policy from the task instruction (deterministic).

    Only EXPLICIT requirements constrain the output: a named language and a
    numeric character requirement written into the instruction. Persian and
    Arabic-Indic digits are accepted in the numeric expressions.
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
    # EXACT requirement; "up to N" / "at most N" variants were matched above.
    if exact_length is None and max_length is None:
        match = re.search(r"(\d{1,5})[-\s]*(?:کاراکتری|کاراکتر|character|char|حرف)", normalized, re.IGNORECASE)
        if match:
            exact_length = int(match.group(1))
            length_text = match.group(0)

    return PreparationPolicy(
        language=language,
        exact_length=exact_length if exact_length and exact_length > 0 else None,
        max_length=max_length if max_length and max_length > 0 else None,
        length_text=length_text,
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
