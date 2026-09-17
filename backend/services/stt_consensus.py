"""
Media Processing — the STT-ONLY consensus reconciler of the bounded multi-pass
transcription seam (the recognition-accuracy experiment of the explicit
Voice/Audio path).

``INVESTIGATION.md`` keeps the two speech-to-text failure classes apart: §19 is
the recognition-QUALITY class (a transcript WAS produced, with wrong words) and
§20 is the transport/timeout class. This module belongs to §19 only, and it is the
smallest mechanism that can act on it: a PURE function of the hypotheses the
SAME audio produced.

    reconcile_hypotheses(["...", "...", "..."]) -> ConsensusResult

Every rule below is a deliberate anti-hallucination decision, and every one of
them is pinned by tests:

* **STT-only (design A).** The function's entire input is the list of hypothesis
  strings — there is no parameter for a chat id, a sender, a username, a
  filename, a caption, a reply, a previous message, a memory or a session, so no
  Telegram or conversational context can reach it. No second model, no prompt and
  no language model of any kind takes part, so nothing can "clean up", rephrase
  or translate the transcript.
* **The scaffold is never chosen to make the transcript longer.** An empty
  hypothesis is never the scaffold (it has no word grid to vote on). With TWO
  readings the scaffold is the first one that heard something, because two votes
  can never override a position and the two-pass result must therefore stay
  byte-identical to the first pass. With THREE or more the scaffold is the
  MEDIAN word count instead — the grid's token count decides which positions can
  be voted on at all, so an outlier-length reading (which is exactly what a
  length-mangling mis-recognition is) must not be allowed to define it. Length
  picks the GRID only: a strict majority still decides every position, and the
  longest reading is never favoured.
* **A position changes only on a STRICT MAJORITY** of the hypotheses' votes for
  that position, where "this hypothesis has no word here" is itself a vote. A
  tie, a lone dissenting reading, or a 2-of-2 disagreement all resolve to the
  scaffold's own token: the "insufficient evidence" answer.
* **Insertions are never emitted.** A word the scaffold has no position for is
  dropped, so the reconciler can only ever substitute or delete inside the
  scaffold's own word grid; it can never lengthen the transcript with new words.
* **The emitted token is always a provider's own surface token**, copied out of
  one of the hypotheses. :func:`comparison_key` decides EQUALITY only and is never
  emitted, so no normalization, transliteration, punctuation or spacing rule of
  this module can reach the transcript.
* **Unanimity is byte-exact.** When no position is overridden, the scaffold's
  string is returned unchanged — three agreeing passes return exactly the bytes
  the single-pass engine returns today.

Why the pass COUNT decides whether this can help at all: under a strict-majority
rule a two-hypothesis disagreement is a 1-1 tie, which resolves to the scaffold —
so consensus over two passes is byte-identical to the first pass and buys nothing
but latency. Three passes are the smallest count that can outvote one dissenting
reading (2 of 3). That is a property of the rule, not a tuning parameter, and it
is asserted directly by this module's tests.

Persian specifics handled by the COMPARISON key only: the ي/ی and ك/ک variants,
the alef-hamza forms, teh-marbuta/heh, Arabic-Indic and Persian digits, the
harakat and tatweel, the zero-width characters (including ZWNJ, so "میکند" and
"میکند" agree) and the Arabic/Latin punctuation and spacing around a token. The
alef-madda آ is deliberately NOT folded into ا: unlike the hamza forms it is a
distinct letter and folding it could rewrite a real word.

Known limits, stated rather than hidden. Repeated passes come from the SAME
model over the SAME audio, so their errors are correlated: consensus can only
correct a mis-recognition the other passes did not repeat, and it can make a
transcript WORSE when a wrong reading is the majority. The alignment is a
word-level diff, so a repeated token can be paired with the wrong occurrence —
which is exactly why a single dissenting vote never changes anything. Whether
multi-pass consensus improves Persian transcripts is a LIVE measurement
(``backend/tools/stt_benchmark.py``), never a property this module can claim.
"""
from __future__ import annotations

import difflib
import unicodedata
from dataclasses import dataclass
from typing import Sequence

#: Arabic → Persian letter variants, applied to the COMPARISON key only. Each
#: entry is a documented orthographic variant of the same Persian letter; the
#: same two mappings the project's delivery layer already applies (ي→ی, ك→ک) are
#: included here for consistency. Alef-madda (آ) is intentionally absent.
_VARIANT_TRANSLATION = str.maketrans({
    "\u064a": "\u06cc",   # ARABIC YEH              → FARSI YEH
    "\u0649": "\u06cc",   # ARABIC ALEF MAKSURA     → FARSI YEH
    "\u0643": "\u06a9",   # ARABIC KAF              → KEHEH
    "\u06aa": "\u06a9",   # SWASH KAF               → KEHEH
    "\u0629": "\u0647",   # TEH MARBUTA             → HEH
    "\u06c0": "\u0647",   # HEH WITH YEH ABOVE      → HEH
    "\u0623": "\u0627",   # ALEF WITH HAMZA ABOVE   → ALEF
    "\u0625": "\u0627",   # ALEF WITH HAMZA BELOW   → ALEF
    "\u0671": "\u0627",   # ALEF WASLA              → ALEF
})

#: Characters that carry no lexical value for Persian/Arabic speech comparison:
#: the zero-width family (ZWNJ included, so "میکند" and "میکند" compare equal),
#: the tatweel and the harakat/diacritics.
_IGNORED_TRANSLATION = str.maketrans({
    "\u200b": "", "\u200c": "", "\u200d": "", "\u2060": "", "\ufeff": "",
    "\u0640": "",
    "\u064b": "", "\u064c": "", "\u064d": "", "\u064e": "", "\u064f": "",
    "\u0650": "", "\u0651": "", "\u0652": "", "\u0653": "", "\u0654": "",
    "\u0655": "", "\u0670": "",
})

#: Persian (۰-۹) and Arabic-Indic (٠-٩) digits compare equal to their ASCII form;
#: a digit and its ASCII spelling denote the same number.
_DIGIT_TRANSLATION = str.maketrans("۰۱۲۳۴۵۶۷۸۹", "0123456789") | str.maketrans(
    "٠١٢٣٤٥٦٧٨٩", "0123456789",
)

#: Punctuation stripped from the ENDS of a comparison key only (Latin, Arabic and
#: Persian marks), never from the emitted token.
_PUNCTUATION = (
    ".,;:!?…؛،؟٪٫٬«»\"'`´()[]{}<>-_/\\|*#@&+~^%$="
    "\u2018\u2019\u201c\u201d\u2013\u2014\u2015\u2026"
)


@dataclass(frozen=True)
class ConsensusResult:
    """The reconciled transcript plus the bounded evidence of how it was reached.

    ``hypotheses`` is how many transcripts took part, ``positions`` is the word
    grid the scaffold pass produced, and ``changed``/``dropped`` count the
    positions a strict majority actually overrode (substituted / removed). All
    four are bounded integers and carry no transcript content, so they are safe
    to log — they are what makes a live multi-pass run measurable.
    """

    text: str
    hypotheses: int
    positions: int
    changed: int
    dropped: int

    @property
    def unanimous(self) -> bool:
        """``True`` when no position was overridden (the scaffold stands as-is)."""
        return self.changed == 0 and self.dropped == 0


def comparison_key(token: str) -> str:
    """The equality key of ONE token — for ALIGNMENT ONLY, never emitted.

    Two tokens with the same key are treated as the same spoken word despite an
    orthographic variant, a zero-width character, a digit spelling, the harakat,
    the tatweel or the punctuation/spacing a provider added around it. A token
    that is nothing but punctuation keeps its own text as its key, so distinct
    marks never collapse into each other.
    """
    if not token:
        return ""
    text = unicodedata.normalize("NFC", token).strip()
    if not text:
        return ""
    text = text.translate(_IGNORED_TRANSLATION)
    text = text.translate(_VARIANT_TRANSLATION)
    text = text.translate(_DIGIT_TRANSLATION)
    text = text.strip(_PUNCTUATION).strip()
    if not text:
        return token
    return text


def reconcile_hypotheses(hypotheses: Sequence[str]) -> ConsensusResult:
    """One deterministic transcript from N hypotheses of the SAME audio.

    Pure, synchronous and side-effect free: the same input always yields the same
    output, no I/O happens, and the only information used is the hypotheses
    themselves. Zero hypotheses yield the empty transcript, one hypothesis is
    returned byte-exact, and a set with no resolvable disagreement returns the
    scaffold string unchanged.
    """
    texts = [text for text in hypotheses if isinstance(text, str)]
    if not texts:
        return ConsensusResult("", 0, 0, 0, 0)
    if len(texts) == 1:
        return ConsensusResult(texts[0], 1, len(_pieces(texts[0])), 0, 0)

    scaffold_index, scaffold = _scaffold(texts)
    pieces = _pieces(scaffold)
    if not pieces:
        # No hypothesis heard a word: the honest answer is the empty transcript.
        return ConsensusResult("", len(texts), 0, 0, 0)
    scaffold_keys = [comparison_key(token) for _, token in pieces]

    # One column per scaffold position, each carrying one vote per hypothesis:
    # ``(key, surface)`` when that hypothesis has a word aligned there, ``None``
    # when it does not (a deletion is evidence too).
    columns: list[list[tuple[str, str] | None]] = [
        [(scaffold_keys[index], pieces[index][1])] for index in range(len(pieces))
    ]
    for index, other in enumerate(texts):
        if index == scaffold_index:
            continue
        other_pieces = _pieces(other)
        other_keys = [comparison_key(token) for _, token in other_pieces]
        for position, vote in enumerate(_align(scaffold_keys, other_keys, other_pieces)):
            columns[position].append(vote)

    separators: list[str] = []
    emitted: list[str] = []
    changed = 0
    dropped = 0
    for position, column in enumerate(columns):
        winner, votes = _majority(column)
        keeps_scaffold = winner == scaffold_keys[position] or votes * 2 <= len(column)
        if keeps_scaffold:
            separators.append(pieces[position][0])
            emitted.append(pieces[position][1])
            continue
        if winner is None:
            # A strict majority has no word here: the position is removed.
            dropped += 1
            continue
        separators.append(pieces[position][0])
        emitted.append(_surface_of(column, winner))
        changed += 1

    if changed == 0 and dropped == 0:
        # Unanimity: the scaffold's own bytes, untouched.
        return ConsensusResult(scaffold, len(texts), len(pieces), 0, 0)
    rebuilt = "".join(
        separator + token for separator, token in zip(separators, emitted)
    ).strip()
    return ConsensusResult(rebuilt, len(texts), len(pieces), changed, dropped)


def _scaffold(texts: Sequence[str]) -> tuple[int, str]:
    """``(index, text)`` of the alignment scaffold, chosen deterministically.

    Two rules, and the reason for each:

    * an EMPTY hypothesis is never the scaffold — it has no word grid, so no
      evidence could be applied to it at all (when nothing was heard, the first
      reading is used and the result is the empty transcript);
    * with FEWER THAN THREE readings the scaffold is the first one that heard
      something, because two votes can never override a position (they are at
      best a 1-1 tie) and the two-pass result must therefore remain
      byte-identical to the first pass;
    * with THREE OR MORE the scaffold is the MEDIAN word count (the lowest index
      wins a tie), because the grid's token count decides which positions can be
      voted on at all — a word the scaffold does not have is an insertion and can
      never be emitted, so the one reading whose length was mangled by the
      mis-recognition must not be allowed to define the grid.

    This is scaffold SELECTION, never output selection: the strict-majority rule
    still decides every position of the grid.
    """
    indexes = [index for index, text in enumerate(texts) if text.strip()]
    if len(indexes) < 3:
        index = indexes[0] if indexes else 0
        return index, texts[index]
    lengths = sorted(len(_pieces(texts[index])) for index in indexes)
    median = lengths[(len(lengths) - 1) // 2]
    for index in indexes:
        if len(_pieces(texts[index])) == median:
            return index, texts[index]  # pragma: no cover - median always exists
    raise AssertionError("unreachable: the median is one of the lengths")


def _pieces(text: str) -> list[tuple[str, str]]:
    """``[(separator_before, token), ...]`` — the original string, split only.

    The separators travel with their tokens so a reconciled transcript is rebuilt
    from the scaffold's OWN spacing: no whitespace is invented, no token is
    rewritten and no re-rendering happens unless a position was overridden.
    """
    pieces: list[tuple[str, str]] = []
    separator = ""
    token = ""
    for char in text:
        if char.isspace():
            if token:
                pieces.append((separator, token))
                separator, token = "", ""
            separator += char
        else:
            token += char
    if token:
        pieces.append((separator, token))
    return pieces


def _align(
    scaffold_keys: Sequence[str],
    other_keys: Sequence[str],
    other_pieces: Sequence[tuple[str, str]],
) -> list[tuple[str, str] | None]:
    """One vote per scaffold position from ONE other hypothesis (a word-level diff).

    Equal blocks and same-length replacements pair one-to-one; a replacement of a
    DIFFERENT length is ambiguous, so it votes nothing (the position then needs a
    majority from the other hypotheses); a scaffold word the other hypothesis does
    not have votes "no word here"; words the other hypothesis has and the scaffold
    does not are insertions, which are deliberately unrepresentable.
    """
    votes: list[tuple[str, str] | None] = [None] * len(scaffold_keys)
    matcher = difflib.SequenceMatcher(None, list(scaffold_keys), list(other_keys), autojunk=False)
    for tag, i1, i2, j1, j2 in matcher.get_opcodes():
        if tag == "equal" or (tag == "replace" and (i2 - i1) == (j2 - j1)):
            for offset in range(i2 - i1):
                key = other_keys[j1 + offset]
                votes[i1 + offset] = (key, other_pieces[j1 + offset][1])
    return votes


def _majority(column: Sequence[tuple[str, str] | None]) -> tuple[str | None, int]:
    """The most-voted key of ONE column, and its count (ties: first-seen order).

    ``None`` is the "no word here" key, so a deletion can win a column outright.
    """
    counts: dict[str | None, int] = {}
    for cell in column:
        key = cell[0] if cell is not None else None
        counts[key] = counts.get(key, 0) + 1
    winner: str | None = None
    winner_votes = 0
    for cell in column:
        key = cell[0] if cell is not None else None
        if counts[key] > winner_votes:
            winner, winner_votes = key, counts[key]
    return winner, winner_votes


def _surface_of(column: Sequence[tuple[str, str] | None], key: str) -> str:
    """The provider's own surface token for ``key``, in reading order."""
    for cell in column:
        if cell is not None and cell[0] == key:
            return cell[1]
    return key  # pragma: no cover - the winner always comes from a vote
