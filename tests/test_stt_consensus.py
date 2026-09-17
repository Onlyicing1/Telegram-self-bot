"""
Media Processing — the STT-ONLY consensus reconciler of the bounded multi-pass
transcription seam (``INVESTIGATION.md`` §19's recognition-quality class).

This suite pins the RECONCILIATION RULE, not recognition quality. Everything here
runs over three (or two, or one) transcript HYPOTHESES of the same audio; no
model, no network and no audio is involved, and nothing below claims that
multi-pass transcription improves Persian accuracy — that is a live measurement
(`backend/tools/stt_benchmark.py`), and the tests say so where it matters.

What is pinned:

  * unanimity is byte-exact (three agreeing passes return the first pass, byte for
    byte — the single-pass behaviour is the consensus's floor);
  * a position changes only on a STRICT MAJORITY, so a lone dissenting reading,
    a tie, an all-different column and a 2-hypothesis disagreement all keep the
    scaffold's own token;
  * insertions are never emitted and no token is ever invented: every emitted
    token is one of the hypotheses' own surface tokens;
  * substitutions, deletions and majority deletions resolve exactly as documented;
  * the Persian specifics (ZWNJ, the Arabic/Persian letter variants, the digits,
    the punctuation, code-switching with Latin words) are compared leniently and
    emitted VERBATIM — the comparison key is never emitted;
  * the input is the hypotheses and nothing else — no chat id, sender, filename,
    caption, reply or history exists in the signature;
  * the rule is pure and deterministic (same input, same output, no I/O).
"""
from __future__ import annotations

import inspect

import pytest

from backend.services import stt_consensus
from backend.services.stt_consensus import (
    ConsensusResult,
    comparison_key,
    reconcile_hypotheses,
)

ZWNJ = "\u200c"

#: The owner's own §19.1 observation: the same Persian speech, three readings.
_SPEECH = "دیدم اتفاقا تو گپ چیز باحالیه خلاصه چت"
_READING_A = "دیه اتفاقا تو کپ چیز باحالیه حالا سید چت"
_READING_B = "دیدم اتفاقا تو گپ چیز باحالیه خلاصه چت"


def _tokens(text: str) -> list[str]:
    return text.split()


# ── 1. The trivial and the unanimous cases ──


def test_no_hypothesis_yields_the_empty_transcript():
    result = reconcile_hypotheses([])

    assert result.text == ""
    assert result.hypotheses == 0 and result.positions == 0


def test_one_hypothesis_is_returned_byte_exact():
    """A single pass IS the single-pass behaviour — spacing and all."""
    text = f"  {_READING_B}  \n"

    result = reconcile_hypotheses([text])

    assert result.text == text
    assert result.unanimous and result.hypotheses == 1


def test_identical_hypotheses_return_the_first_one_byte_exact():
    result = reconcile_hypotheses([_SPEECH, _SPEECH, _SPEECH])

    assert result.text == _SPEECH
    assert result.unanimous
    assert (result.hypotheses, result.positions) == (3, len(_tokens(_SPEECH)))


def test_unanimity_preserves_newlines_and_double_spaces():
    """Nothing is re-rendered when nothing was overridden."""
    text = "خلاصه  چت\nچیز باحالیه"

    result = reconcile_hypotheses([text, text, text])

    assert result.text == text


def test_majority_agreement_never_reorders_the_scaffold():
    """A unanimous but differently-spaced hypothesis set is not re-spaced."""
    result = reconcile_hypotheses(["a b c", "a b c", "a b   c"])

    assert result.text == "a b c"


# ── 2. The majority rule: what changes and what never can ──


def test_the_owners_persian_example_resolves_to_the_majority_reading():
    """The §19.1 example, with two independent readings — no word is invented."""
    result = reconcile_hypotheses([_SPEECH, _READING_A, _READING_B])

    assert result.text == _SPEECH
    assert result.unanimous, "the minority reading alone changes nothing"
    assert set(_tokens(result.text)) <= set(_tokens(_SPEECH)) | set(_tokens(_READING_A))


def test_a_two_of_three_substitution_overrides_the_scaffold():
    result = reconcile_hypotheses(["من رفتم خانه", "من رفتم خونه", "من رفتم خونه"])

    assert result.text == "من رفتم خونه"
    assert (result.changed, result.dropped) == (1, 0)
    assert not result.unanimous


def test_the_substituted_surface_token_is_the_providers_own_token():
    """The emitted word is a hypothesis' surface, never the stripped comparison key."""
    result = reconcile_hypotheses(["تو گپ", "تو کپ.", "تو کپ."])

    assert result.text == "تو کپ.", "the winner's punctuation travels with it"
    assert result.changed == 1


def test_a_digit_variant_is_not_a_disagreement_to_override():
    """Digits compare equal, so a unanimous column keeps the scaffold's surface."""
    result = reconcile_hypotheses(["ساعت ۳ است", "ساعت 3 است", "ساعت 3 است"])

    assert result.text == "ساعت ۳ است"
    assert result.unanimous


def test_a_lone_dissenting_substitution_never_overrides():
    result = reconcile_hypotheses(["اتفاقا", "اتفقا", "اتفاقا"])

    assert result.text == "اتفاقا"
    assert result.unanimous


def test_all_three_hypotheses_disagreeing_keeps_the_scaffold():
    result = reconcile_hypotheses(["دیدم", "دیه", "بدیدم"])

    assert result.text == "دیدم"
    assert result.unanimous


def test_a_two_hypothesis_disagreement_is_a_tie_and_keeps_the_scaffold():
    """The documented reason two passes cannot improve anything."""
    result = reconcile_hypotheses(["دیدم گپ", "دیه کپ"])

    assert result.text == "دیدم گپ"
    assert result.changed == 0 and result.dropped == 0


@pytest.mark.parametrize(
    "pair",
    [
        ("سلام", "سلام"),
        ("سلام", "درود"),
        ("یک دو سه", "یک دو"),            ("یک دو", "یک دو سه چهار"),
            ("سلام", ""),
        ],
    )
def test_two_passes_are_byte_identical_to_the_first(pair):
    """Whatever the pair is, consensus over two hypotheses cannot change a word."""
    first, second = pair

    result = reconcile_hypotheses([first, second])

    assert result.text == first


# ── 3. Deletions, insertions and the no-invention guarantee ──


def test_a_minority_deletion_is_ignored():
    result = reconcile_hypotheses(["الف ب ج", "الف ج", "الف ب ج"])

    assert result.text == "الف ب ج"
    assert result.unanimous


def test_a_word_only_the_scaffold_heard_is_dropped():
    """The drop path: a majority of readings have no word at the position."""
    result = reconcile_hypotheses(["الف ب ج", "", ""])

    assert result.dropped == 3, "a majority of readings heard nothing at all"
    assert result.text == ""


def test_three_readings_never_lose_a_word_to_the_alignment():
    """Length differences are resolved by the grid, not by dropping content.

    With three readings the scaffold is the MEDIAN-length one, so a word the
    majority heard is inside the grid and is never an insertion, and a position
    can only be removed when a majority of readings genuinely lacks it.
    """
    assert reconcile_hypotheses(["الف ب ج", "الف ج", "الف ج"]).text == "الف ج"
    assert reconcile_hypotheses(["الف ب ج", "الف ب ج", "الف ج"]).text == "الف ب ج"
    assert reconcile_hypotheses(["الف ب ج", "الف ج", "الف ب ج"]).text == "الف ب ج"


def test_an_insertion_in_one_hypothesis_is_never_emitted():
    result = reconcile_hypotheses(["خلاصه چت", "خلاصه باحال چت", "خلاصه چت"])

    assert result.text == "خلاصه چت"
    assert "باحال" not in result.text


def test_a_word_two_readings_heard_is_never_treated_as_an_insertion():
    """The median grid contains it, so a word the majority produced is kept."""
    result = reconcile_hypotheses(["خلاصه چت", "خلاصه باحال چت", "خلاصه باحال چت"])

    assert result.text == "خلاصه باحال چت"
    assert "باحال" in result.text


def test_every_emitted_token_exists_in_at_least_one_hypothesis():
    hypotheses = [
        "دیدم اتفاقا تو گپ",
        "دیه اتفاقا تو کپ",
        "دیدم اتفاقا در گپ",
    ]

    result = reconcile_hypotheses(hypotheses)

    allowed = {token for text in hypotheses for token in _tokens(text)}
    assert set(_tokens(result.text)) <= allowed, "no word may be invented"


def test_the_reconciler_never_lengthens_the_grid_it_chose():
    hypotheses = ["یک دو", "یک دو سه چهار پنج", "یک دو سه"]

    result = reconcile_hypotheses(hypotheses)

    assert result.text == "یک دو سه", "the median grid, never the longest reading"
    assert len(_tokens(result.text)) <= max(len(_tokens(text)) for text in hypotheses)


# ── 4. Empty hypotheses ──


def test_a_first_pass_with_a_mangled_length_cannot_set_the_word_grid():
    """The §19.1 minority reading FIRST: the median grid keeps the majority words.

    This is the one way a multi-pass run could be WORSE than a single pass: a
    reading whose word count was mangled would otherwise define the grid, the
    other two readings' words would land as insertions, and a word both of them
    heard would be lost. The scaffold is therefore the median-length reading.
    """
    result = reconcile_hypotheses([_READING_A, _SPEECH, _SPEECH])

    assert result.text == _SPEECH
    assert "خلاصه" in result.text


def test_the_grid_is_the_median_length_and_never_the_longest():
    result = reconcile_hypotheses(["یک", "یک دو", "یک دو سه"])

    assert result.text == "یک دو", "neither the shortest nor the longest reading"


def test_an_empty_first_pass_uses_the_first_reading_that_heard_speech():
    """Scaffold selection, not length selection: an empty pass has no grid."""
    result = reconcile_hypotheses(["", "سلام دنیا", "سلام دنیا"])

    assert result.text == "سلام دنیا"
    assert (result.hypotheses, result.positions) == (3, 2)


def test_a_majority_of_empty_hypotheses_drops_the_speech():
    result = reconcile_hypotheses(["سلام", "", ""])

    assert result.text == ""
    assert result.dropped == 1


def test_all_hypotheses_empty_yields_the_empty_transcript():
    result = reconcile_hypotheses(["", "   ", ""])

    assert result.text == ""
    assert result.positions == 0


# ── 5. Persian specifics: lenient comparison, verbatim emission ──


def test_zwnj_differences_compare_equal_and_the_scaffold_surface_is_kept():
    spaced = f"می{ZWNJ}کند"
    result = reconcile_hypotheses([spaced, "میکند", "میکند"])

    assert result.text == spaced
    assert ZWNJ in result.text, "the scaffold's own ZWNJ is preserved"
    assert result.unanimous


def test_arabic_persian_letter_variants_compare_equal():
    """The two mappings the delivery layer already applies, and the hamza alefs."""
    result = reconcile_hypotheses(["يك كوچه", "یک کوچه", "یک کوچه"])

    assert result.text == "يك كوچه", "the scaffold's surface is emitted verbatim"
    assert result.unanimous


def test_alef_madda_is_not_folded_into_alef():
    """آ is a distinct Persian letter: a variant must not merge آب and اب."""
    assert comparison_key("آب") != comparison_key("اب")


def test_persian_and_arabic_digits_compare_equal():
    assert comparison_key("۳") == comparison_key("3") == comparison_key("٣")


def test_harakat_and_tatweel_compare_equal():
    assert comparison_key("سَلام") == comparison_key("سـلام") == comparison_key("سلام")


def test_punctuation_differences_compare_equal():
    result = reconcile_hypotheses(["باحالیه،", "باحالیه.", "باحالیه"])

    assert result.text == "باحالیه،"
    assert result.unanimous


def test_punctuation_only_tokens_only_match_themselves():
    assert comparison_key("...") == "..."
    assert comparison_key("...") != comparison_key("?")


def test_punctuation_around_a_different_word_is_still_a_disagreement():
    result = reconcile_hypotheses(["گپ.", "کپ.", "کپ!"])

    assert result.text == "کپ.", "the majority reading wins, with its own surface"
    assert result.changed == 1


def test_code_switching_words_are_compared_like_any_other_word():
    result = reconcile_hypotheses(["خلاصه chat", "خلاصه chat", "خلاصه چت"])

    assert result.text == "خلاصه chat"
    assert result.unanimous


def test_case_differences_are_left_alone():
    """Casing is not folded: an uncertain override must not change US into us."""
    result = reconcile_hypotheses(["the US market", "the us market", "the us market"])

    assert result.text == "the us market"


# ── 6. Determinism, purity, and the absence of any Telegram input ──


def test_the_rule_is_deterministic():
    hypotheses = [_SPEECH, _READING_A, "دیدم اتفاقا در گپ چیز باحالیه خلاصه سید چت"]

    results = [reconcile_hypotheses(hypotheses) for _ in range(5)]

    assert {result.text for result in results} == {results[0].text}


def test_the_signature_accepts_hypotheses_and_nothing_else():
    """Context isolation is a property of the interface, not a convention."""
    parameters = list(inspect.signature(reconcile_hypotheses).parameters)

    assert parameters == ["hypotheses"]


def test_the_module_imports_no_io_network_or_model_layer():
    source = inspect.getsource(stt_consensus)
    for forbidden in (
        "httpx", "telethon", "asyncio", "logging", "openai", "backend.ai",
        "backend.services.media_service", "backend.services.gemini_media_engine",
    ):
        assert forbidden not in source, forbidden


def test_a_result_carries_counts_and_never_the_audio_or_context():
    result = reconcile_hypotheses([_SPEECH, _READING_A, _READING_B])

    assert isinstance(result, ConsensusResult)
    fields = set(ConsensusResult.__dataclass_fields__)
    assert fields == {"text", "hypotheses", "positions", "changed", "dropped"}
    assert not fields & {"chat_id", "sender", "filename", "caption", "reply", "history"}


def test_non_string_hypotheses_are_ignored_rather_than_coerced():
    result = reconcile_hypotheses(["سلام", None, 17])  # type: ignore[list-item]

    assert result.text == "سلام"
    assert result.hypotheses == 1
