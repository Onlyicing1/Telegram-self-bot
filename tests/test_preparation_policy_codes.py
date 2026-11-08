"""Bounded content-policy violation codes: precise, stable and content-free.

The deterministic policy already rejected non-compliant content; these tests
pin WHICH rule rejected it, through a closed vocabulary persisted as a
diagnostic. No message, generated text or provider payload may become a code.
"""
from __future__ import annotations

import pytest

from backend.ai.preparation_policy import (
    DEFAULT_VIOLATION_CODE,
    POLICY_VIOLATION_CODES,
    PreparationPolicyError,
    derive_policy,
    validate_content,
    validate_prepared_arguments,
)
from backend.ai.task_execution import TaskPreparationError

INSTRUCTION = (
    "update my bio, with a randomly generated dialogue, in English, "
    "at most 70 characters, from Shikamaru Nara"
)
LABEL = "Shikamaru Nara: "
DIALOGUE = "What a drag... I'd rather watch the clouds."


def _code(text, policy=None) -> str:
    policy = policy or derive_policy(INSTRUCTION)
    with pytest.raises(PreparationPolicyError) as caught:
        validate_content(text, policy)
    return caught.value.code


def test_vocabulary_is_closed_and_stable():
    assert POLICY_VIOLATION_CODES == frozenset({
        "attribution_missing",
        "attribution_mismatch",
        "language_mismatch",
        "visible_length_exceeded",
        "visible_length_mismatch",
        "empty_content",
        "invalid_content_type",
        "unverifiable_exact_quote",
        "other_policy_violation",
    })
    assert DEFAULT_VIOLATION_CODE in POLICY_VIOLATION_CODES


def test_a_compliant_shikamaru_dialogue_passes_unchanged():
    text = LABEL + DIALOGUE
    assert len(DIALOGUE) <= 70
    assert validate_content(text, derive_policy(INSTRUCTION)) == text


def test_seventy_visible_characters_pass_and_seventy_one_is_exceeded():
    policy = derive_policy(INSTRUCTION)
    exact = LABEL + "a" * 70
    assert validate_content(exact, policy) == exact
    assert _code(LABEL + "a" * 71, policy) == "visible_length_exceeded"


def test_exact_length_violation_is_a_mismatch_not_exceeded():
    policy = derive_policy("update my bio in English, exactly 10 characters")
    assert policy.exact_length == 10
    assert _code("a" * 9, policy) == "visible_length_mismatch"
    assert _code("a" * 11, policy) == "visible_length_mismatch"
    assert validate_content("a" * 10, policy) == "a" * 10


def test_missing_attribution_is_distinguished_from_a_wrong_speaker():
    policy = derive_policy(INSTRUCTION)
    # No speaker label was attempted at all.
    assert _code(DIALOGUE, policy) == "attribution_missing"
    # A label exists but names a different speaker.
    assert _code("Naruto Uzumaki: " + DIALOGUE, policy) == "attribution_mismatch"
    # A short form of the requested name is still a mismatch.
    assert _code("Shikamaru: " + DIALOGUE, policy) == "attribution_mismatch"


def test_label_without_a_dialogue_line_is_empty_content():
    assert _code(LABEL, derive_policy(INSTRUCTION)) == "empty_content"


def test_language_violation_is_its_own_code():
    policy = derive_policy(INSTRUCTION)
    assert _code(LABEL + "چه دردسر...", policy) == "language_mismatch"
    assert _code(LABEL + "12345", policy) == "language_mismatch"


@pytest.mark.parametrize("text", ["", "   \n\t ", '{"text": "hello"}', "API error: failed", 123, None])
def test_unusable_content_is_classified(text):
    expected = "empty_content" if text in ("", "   \n\t ") else "invalid_content_type"
    assert _code(text) == expected


def test_exact_quote_request_fails_closed_with_its_own_code():
    policy = derive_policy("give me an exact quote in English from Shikamaru Nara")
    assert policy.quote_exact
    assert _code("Shikamaru Nara: anything", policy) == "unverifiable_exact_quote"


def test_source_without_a_content_field_is_attribution_missing():
    with pytest.raises(PreparationPolicyError) as caught:
        validate_prepared_arguments({"template": "{text}"}, derive_policy(INSTRUCTION))
    assert caught.value.code == "attribution_missing"


def test_unknown_codes_fall_back_and_are_not_persisted():
    assert PreparationPolicyError("x", code="made_up").code == DEFAULT_VIOLATION_CODE
    assert PreparationPolicyError("x").code == DEFAULT_VIOLATION_CODE
    # A preparation diagnostic only accepts the bounded vocabulary.
    assert TaskPreparationError("x", policy_violation="made_up").policy_violation == ""
    assert (
        TaskPreparationError("x", policy_violation="language_mismatch").policy_violation
        == "language_mismatch"
    )


def test_violation_message_never_contains_the_generated_content():
    generated = LABEL + "a" * 71
    with pytest.raises(PreparationPolicyError) as caught:
        validate_content(generated, derive_policy(INSTRUCTION))
    assert caught.value.code == "visible_length_exceeded"
    assert "a" * 71 not in str(caught.value)
    assert DIALOGUE not in str(caught.value)
