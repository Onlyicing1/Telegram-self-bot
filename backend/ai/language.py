"""One validated language catalog and resolver for generated AI content.

The Glass UI language (``panel_settings.language``) is a different concept and
is never read here: this module owns the language of GENERATED CONTENT only.

Precedence is deterministic and total:

    explicit request > explicit task > global ``ai_preferences.language`` > English

``auto`` is an explicit selection, never inferred from what the owner typed. It
has one documented contract:

* a source-based operation (History summary) uses the dominant language of the
  source text when it can be resolved;
* generated content with no source falls back to English, and the decision
  reports that fallback instead of pretending detection succeeded.

Language detection is a script/word heuristic, not a capability guarantee, and
a provider is never assumed to have complied with the requested language.
"""
from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Any, Iterable, Sequence

#: The explicit Auto selection. Never a concrete language code.
AUTO = "auto"

#: The documented fallback and the historical default.
DEFAULT_CODE = "en"


@dataclass(frozen=True)
class Language:
    code: str
    label: str
    direction: str
    provider_hint: str


#: The ONE catalog. Adding a language is a data change, not a new code path.
CATALOG: tuple[Language, ...] = (
    Language(
        code="en",
        label="English",
        direction="ltr",
        provider_hint="Write natural English with no translation artifacts.",
    ),
    Language(
        code="fa",
        label="Persian",
        direction="rtl",
        provider_hint="Write natural Persian (Farsi) in Persian script.",
    ),
    Language(
        code="ar",
        label="Arabic",
        direction="rtl",
        provider_hint="Write Modern Standard Arabic in Arabic script.",
    ),
    Language(
        code="zh",
        label="Chinese",
        direction="ltr",
        provider_hint="Write Simplified Chinese.",
    ),
)

BY_CODE: dict[str, Language] = {entry.code: entry for entry in CATALOG}

#: Taskloom's existing vocabulary, expressed once (no second language list).
TASK_CODES: tuple[str, ...] = tuple(entry.code for entry in CATALOG)
TASK_LANGUAGE_NAMES: tuple[tuple[str, str], ...] = tuple(
    (entry.code, entry.label) for entry in CATALOG
)

#: Accepted external spellings. Labels, codes and the policy words the
#: deterministic preparation policy recognizes all normalize to one code.
_ALIASES: dict[str, str] = {}
for _entry in CATALOG:
    _ALIASES[_entry.code] = _entry.code
    _ALIASES[_entry.label.casefold()] = _entry.code
_ALIASES["farsi"] = "fa"
_ALIASES["auto"] = AUTO
_ALIASES["automatic"] = AUTO
_ALIASES["any"] = ""


class LanguageError(ValueError):
    """An externally supplied language value is not in the catalog."""


def normalize(value: Any) -> str:
    """Return a catalog code, :data:`AUTO`, or ``""`` for "not specified".

    Unsupported non-empty values raise :class:`LanguageError` so a caller can
    reject them deterministically; nothing is silently coerced to English.
    """
    if value is None:
        return ""
    if not isinstance(value, str):
        raise LanguageError("language must be text")
    text = " ".join(value.split())
    if not text:
        return ""
    code = _ALIASES.get(text.casefold())
    if code is None:
        raise LanguageError(
            "unsupported language; choose one of: "
            + ", ".join(entry.label for entry in CATALOG)
        )
    return code


def is_auto(value: Any) -> bool:
    return normalize(value) == AUTO


def get(code: str) -> Language | None:
    return BY_CODE.get(code)


def label(code: str) -> str:
    entry = BY_CODE.get(code)
    return entry.label if entry is not None else "English"


def provider_hint(code: str) -> str:
    entry = BY_CODE.get(code)
    return entry.provider_hint if entry is not None else ""


#: Script ranges used by the conservative source detector.
_ARABIC_SCRIPT = re.compile(r"[\u0600-\u06FF]")
_PERSIAN_LETTERS = re.compile(r"[\u067E\u0686\u0698\u06AF]")
_CHINESE = re.compile(r"[\u4E00-\u9FFF]")
_LATIN = re.compile(r"[A-Za-z]")


def detect_source_language(texts: Iterable[str]) -> str:
    """The dominant language of the source, or ``""`` when it cannot be told.

    Deliberately conservative: an ambiguous split or a source with no letters
    returns ``""`` so the caller uses the documented fallback. Persian and
    Arabic share a script, so Arabic script counts as Persian only when a
    Persian-specific letter appears. This is a heuristic for Auto mode only —
    it never selects Auto by itself.
    """
    arabic = persian_letters = chinese = latin = 0
    for text in texts:
        if not isinstance(text, str) or not text:
            continue
        arabic += len(_ARABIC_SCRIPT.findall(text))
        persian_letters += len(_PERSIAN_LETTERS.findall(text))
        chinese += len(_CHINESE.findall(text))
        latin += len(_LATIN.findall(text))
    # Persian and Arabic share a script. Any Persian-specific letter marks the
    # Arabic-script portion as Persian; otherwise it is Arabic. Mixed Arabic
    # script without Persian letters is never guessed as Persian.
    persian = arabic if persian_letters else 0
    plain_arabic = 0 if persian_letters else arabic
    total = arabic + chinese + latin
    if not total:
        return ""
    counts = {"fa": persian, "ar": plain_arabic, "zh": chinese, "en": latin}
    ordered = sorted(counts.items(), key=lambda item: item[1], reverse=True)
    best_code, best = ordered[0]
    runner_up = ordered[1][1]
    if best <= 0 or best < total * 0.6 or best == runner_up:
        return ""
    return best_code


@dataclass(frozen=True)
class LanguageDecision:
    """The resolved language plus WHY it was chosen."""

    #: The value the winning scope asked for (a code, ``auto``, or ``""``).
    selected: str
    #: The effective concrete catalog code.
    code: str
    #: ``"fixed"`` or ``"auto"``.
    mode: str
    #: ``request`` | ``task`` | ``global`` | ``default`` | ``source`` |
    #: ``auto_fallback``.
    source: str
    #: True only when Auto resolved from the source text.
    detected: bool

    @property
    def label(self) -> str:
        return label(self.code)

    @property
    def is_auto(self) -> bool:
        return self.mode == "auto"

    @property
    def prompt_label(self) -> str:
        """The language line rendered into a prompt: never an unverified claim."""
        if self.mode == "auto" and self.detected:
            return f"{self.label} (auto-detected from the source)"
        if self.mode == "auto":
            return f"{self.label} (auto fallback: source language unresolved)"
        return self.label

    def as_metadata(self) -> dict[str, Any]:
        return {
            "language": self.label,
            "language_code": self.code,
            "language_mode": self.mode,
            "language_source": self.source,
            "language_detected": self.detected,
        }


def resolve(
    *,
    request: Any = None,
    task: Any = None,
    global_value: Any = None,
    source_texts: Sequence[str] | None = None,
) -> LanguageDecision:
    """Resolve one language deterministically.

    A higher-priority explicit value is never replaced by a lower one. ``auto``
    is honored only when a scope literally selected it.
    """
    for value, source in (
        (request, "request"),
        (task, "task"),
        (global_value, "global"),
    ):
        selected = normalize(value)
        if not selected:
            continue
        if selected != AUTO:
            return LanguageDecision(selected, selected, "fixed", source, False)
        detected = detect_source_language(source_texts or ())
        if detected:
            return LanguageDecision(AUTO, detected, "auto", "source", True)
        return LanguageDecision(AUTO, DEFAULT_CODE, "auto", "auto_fallback", False)
    return LanguageDecision("", DEFAULT_CODE, "fixed", "default", False)


def global_preference(owner_id: int) -> str:
    """The owner's stored global content language (honest about the fallback).

    Reads the ONE existing preferences repository. When it cannot be read, the
    documented English default is used rather than inventing a second store.
    """
    try:
        from backend.ai.database.manager import get_repository_manager

        record = get_repository_manager().preferences.get_or_create(int(owner_id))
        return normalize(getattr(record, "language", "")) or DEFAULT_CODE
    except LanguageError:
        return DEFAULT_CODE
    except Exception:  # noqa: BLE001 — preferences must never break a request
        return DEFAULT_CODE
