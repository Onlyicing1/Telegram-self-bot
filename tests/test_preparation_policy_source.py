"""Deterministic preparation-policy regression tests: source attribution.

Contract under test: a task instruction that names a source/person/character
("dialogue from Ayanami Rei" / "دیالوگ ... از آیانامی ری") preserves that
source in the derived policy. Source requests are GENERATED in-character
dialogue: the content must deterministically SELF-ATTRIBUTE to the requested
source (open with the full name and a separator) — a different speaker, a
short form, an in-text mention, or unattributed generic text is rejected,
never executed, never truncated. Exact-canonical-quote requests fail closed:
no trusted source corpus or verifier is configured, so a generated line is
never presented as an authenticated quotation. Also pins the "below/under N
characters" semantics as a MAXIMUM of N-1, not an exact-length requirement.
"""
from __future__ import annotations

import pytest

from backend.ai.preparation_policy import (
    PreparationPolicy,
    PreparationPolicyError,
    derive_policy,
    validate_content,
)

PERSIAN_TASK = (
    "هر 5 دقیقه تکست بیو من رو به یه دیالوگ رندوم از آیانامی ری تغییر بده "
    "باید زیر 60 کاراکتر باشه"
)
ENGLISH_TASK = (
    "every minute change my bio to a random dialogue from Ayanami Rei, "
    "below 60 characters"
)


# ── Source extraction ────────────────────────────────────────────────────────


def test_persian_source_is_pinned():
    policy = derive_policy(PERSIAN_TASK)
    assert policy.source == "آیانامی ری"
    assert policy.active


def test_english_source_is_pinned():
    policy = derive_policy(ENGLISH_TASK)
    assert policy.source == "Ayanami Rei"


def test_last_marker_wins_earlier_از_ignored():
    # The first از belongs to "from my saved texts", the LAST one to the source.
    policy = derive_policy(
        "تکست بیو رو از متن ذخیره شده بردار و یه دیالوگ از آیانامی ری بذار"
    )
    assert policy.source == "آیانامی ری"


def test_instrumental_استفاده_از_never_pins_a_source():
    policy = derive_policy("تکست بیو من رو تغییر بده استفاده از متن ذخیره شده")
    assert policy.source == ""


def test_no_marker_means_no_source_constraint():
    policy = derive_policy("هر دقیقه بیو را با یک دیالوگ ۵۰ کاراکتری فارسی عوض کن")
    assert policy.source == ""
    assert policy.exact_length == 50 and policy.language == "persian"


def test_compound_persian_name_survives_change_verb():
    # "… از آیانامی ری تغییر بده" — the change verb must not join the name.
    policy = derive_policy(PERSIAN_TASK)
    assert "تغییر" not in policy.source
    assert "بده" not in policy.source


# ── Speaker attribution enforcement (the live drift bug) ─────────────────────


@pytest.mark.parametrize(
    "drifted",
    [
        "Ayumi: Ready for adventure!",       # live-observed drift
        "Rei: different romanization",        # short form of the name
        "آیانامی ری",                          # name alone, no line at all
        "آیانامی ری:",                         # name + separator, no line
        "hello آیانامی ری: world",            # source mentioned, not the speaker
        "نوا: سلام",                          # another speaker entirely
        "Every star begins as a dream!",     # generic motivational text
        "دنیا زیباست و ستاره‌ها می‌درخشند",     # Persian generic text, no attribution
    ],
)
def test_wrong_speaker_or_unattributed_content_is_rejected(drifted):
    policy = derive_policy(PERSIAN_TASK)
    with pytest.raises(PreparationPolicyError):
        validate_content(drifted, policy)


def test_matching_attributed_line_accepted_as_generated_dialogue():
    """A line that opens with the requested source IS the requested kind of
    content (generated in-character dialogue). Acceptance is deterministic
    self-attribution — never a canonical-quote claim."""
    policy = derive_policy(PERSIAN_TASK)
    line = "آیانامی ری: " + "د" * 40  # 51 chars < 60, Persian script
    assert validate_content(line, policy) == line


def test_matching_attributed_line_accepted_for_english_source_too():
    policy = derive_policy(ENGLISH_TASK)
    line = "Ayanami Rei: Ready!"
    assert validate_content(line, policy) == line


@pytest.mark.parametrize(
    "line",
    [
        "آیانامی ری : سلام",      # spaced separator
        "آیانامی ری - سلام",      # dash separator
        "«آیانامی ری»: سلام",    # quote-wrapped name
    ],
)
def test_attribution_format_variants_are_accepted(line):
    policy = derive_policy(PERSIAN_TASK)
    assert validate_content(line, policy) == line


@pytest.mark.parametrize(
    "instruction,line",
    [
        ("یه نقل قول دقیق از آیانامی ری بذار", "آیانامی ری: سلام دنیا"),
        ("put a verbatim quote from Ayanami Rei", "Ayanami Rei: Ready!"),
        ("عین جمله از آیانامی ری", "آیانامی ری: سلام دنیا"),
    ],
)
def test_exact_quote_request_fails_closed(instruction, line):
    """An explicit exact-canonical-quote request cannot be honored without a
    trusted corpus: even a correctly attributed generated line fails closed
    instead of being presented as an authenticated quotation."""
    policy = derive_policy(instruction)
    assert policy.quote_exact is True
    with pytest.raises(PreparationPolicyError, match="cannot be independently verified"):
        validate_content(line, policy)


def test_exact_quote_flag_requires_a_named_source():
    policy = derive_policy("یه نقل قول دقیق بذار")
    assert policy.quote_exact is False  # no source -> nothing to authenticate


def test_policy_without_source_imposes_no_speaker_prefix():
    policy = derive_policy("هر دقیقه بیو را با یک دیالوگ ۵۰ کاراکتری فارسی عوض کن")
    valid = "د" * 50  # satisfies the exact-50 requirement; no prefix needed
    assert validate_content(valid, policy) == valid


# ── "below/under N characters" is a MAXIMUM (N-1), never exact ───────────────


def test_under_60_means_max_59_not_exactly_60():
    policy = derive_policy(PERSIAN_TASK)
    assert policy.exact_length is None
    assert policy.max_length == 59


def test_english_below_means_max_too():
    policy = derive_policy(ENGLISH_TASK)
    assert policy.max_length == 59 and policy.exact_length is None


def test_under_60_accepts_59_and_rejects_61():
    policy = derive_policy("bio باید زیر 60 کاراکتر باشه")
    assert validate_content("a" * 59, policy) == "a" * 59
    with pytest.raises(PreparationPolicyError):
        validate_content("a" * 61, policy)


def test_exact_and_at_most_requirements_unchanged():
    assert derive_policy("دقیقا 50 کاراکتر").exact_length == 50
    assert derive_policy("حداکثر 60 کاراکتر").max_length == 60
    assert derive_policy("50-character dialogue").exact_length == 50


# ── Policy description feeds the enforced prompt ─────────────────────────────


def test_describe_states_attribution_contract():
    policy = derive_policy(PERSIAN_TASK)
    text = policy.describe()
    assert "آیانامی ری" in text
    assert "GENERATED dialogue" in text


def test_describe_states_exact_quote_fails_closed():
    policy = derive_policy("یه نقل قول دقیق از آیانامی ری")
    text = policy.describe()
    assert "EXACT CANONICAL QUOTE" in text
    assert "fails closed" in text


def test_inactive_policy_is_inert():
    policy = PreparationPolicy()
    assert not policy.active
    assert policy.describe() == "no content constraints"
    assert validate_content("anything", policy) == "anything"
