"""History per-peer count policy — exact limits, choices, and refusals.

The policy consumes the shared Phase 1 ``PeerType`` taxonomy. It is pure: these
tests need no Telegram client, no database and no provider. They pin the exact
product mapping, the HARD 300-message caps for bots and broadcast channels, the
refusal of unidentifiable peers, and the rule that an over-limit request is
NEVER silently clamped.
"""
from __future__ import annotations

import pytest

from backend.services import history_policy
from backend.telegram_api.peers import PeerType

#: The exact contract from the roadmap: peer type -> (hard max, choices).
CASES = [
    (PeerType.PRIVATE_USER, 1000, (100, 250, 500, 1000)),
    (PeerType.PRIVATE_BOT, 300, (100, 250, 300)),
    (PeerType.BASIC_GROUP, 1000, (100, 250, 500, 1000)),
    (PeerType.SUPERGROUP, 1000, (100, 250, 500, 1000)),
    (PeerType.BROADCAST_CHANNEL, 300, (100, 250, 300)),
    (PeerType.UNKNOWN, 0, ()),
]

CAPPED = (PeerType.PRIVATE_BOT, PeerType.BROADCAST_CHANNEL)
LARGE = (PeerType.PRIVATE_USER, PeerType.BASIC_GROUP, PeerType.SUPERGROUP)


def test_the_policy_covers_every_peer_type_exactly_once():
    assert set(history_policy.MAX_MESSAGES) == set(PeerType)
    assert set(history_policy.COUNT_CHOICES) == set(PeerType)
    assert len(CASES) == len(PeerType)


@pytest.mark.parametrize(("peer_type", "maximum", "choices"), CASES)
def test_the_exact_limit_and_choices_for_every_peer_type(peer_type, maximum, choices):
    assert history_policy.maximum_for(peer_type) == maximum
    assert history_policy.choices_for(peer_type) == choices


@pytest.mark.parametrize(("peer_type", "maximum", "choices"), CASES)
def test_every_offered_choice_is_accepted(peer_type, maximum, choices):
    for value in choices:
        assert history_policy.validate_count(peer_type, value) is None
        assert history_policy.validate_choice(peer_type, value) is None


def test_1000_is_accepted_for_the_large_peer_types():
    for peer_type in LARGE:
        assert history_policy.validate_count(peer_type, 1000) is None
        assert history_policy.validate_choice(peer_type, 1000) is None


@pytest.mark.parametrize("peer_type", CAPPED)
def test_300_is_accepted_for_the_capped_peer_types(peer_type):
    assert history_policy.validate_count(peer_type, 300) is None
    assert history_policy.validate_choice(peer_type, 300) is None


@pytest.mark.parametrize("peer_type", CAPPED)
@pytest.mark.parametrize("requested", [301, 500, 1000, 5000])
def test_500_and_1000_are_refused_for_the_capped_peer_types(peer_type, requested):
    refusal = history_policy.validate_count(peer_type, requested)

    assert refusal is not None
    assert "limited to 300 messages per request" in refusal
    assert "100, 250 or 300" in refusal
    # The request is refused, not rewritten into a smaller count.
    assert str(requested) != "300"


@pytest.mark.parametrize("peer_type", CAPPED)
def test_the_panel_choice_validator_refuses_500_and_1000_for_capped_peers(peer_type):
    assert history_policy.validate_choice(peer_type, 500) is not None
    assert history_policy.validate_choice(peer_type, 1000) is not None
    assert history_policy.validate_choice(peer_type, 300) is None


def test_1000_is_refused_for_a_bot_and_a_broadcast_channel():
    assert history_policy.validate_count(PeerType.PRIVATE_BOT, 1000) is not None
    assert history_policy.validate_count(PeerType.BROADCAST_CHANNEL, 1000) is not None


@pytest.mark.parametrize("requested", [None, 0, 1, 100, 300, 1000, 1001, -5, "100"])
def test_unknown_always_refuses(requested):
    refusal = history_policy.validate_count(PeerType.UNKNOWN, requested)

    assert refusal == history_policy.UNKNOWN_REFUSAL
    assert history_policy.validate_choice(PeerType.UNKNOWN, requested) == (
        history_policy.UNKNOWN_REFUSAL
    )


@pytest.mark.parametrize("peer_type", [None, "private_user", 1, object()])
def test_an_unrecognized_peer_type_refuses(peer_type):
    assert history_policy.maximum_for(peer_type) == 0
    assert history_policy.choices_for(peer_type) == ()
    assert history_policy.validate_count(peer_type, 100) == (
        history_policy.UNKNOWN_REFUSAL
    )


@pytest.mark.parametrize("requested", [True, False, "500", "abc", 500.0, 1.5, None])
@pytest.mark.parametrize("peer_type", LARGE)
def test_non_integer_counts_are_refused_not_coerced(peer_type, requested):
    assert history_policy.validate_count(peer_type, requested) is not None
    assert history_policy.validate_choice(peer_type, requested) is not None


@pytest.mark.parametrize("requested", [0, -1, -1000])
@pytest.mark.parametrize("peer_type", LARGE)
def test_zero_and_negative_counts_are_refused(peer_type, requested):
    assert history_policy.validate_count(peer_type, requested) is not None


@pytest.mark.parametrize("peer_type", LARGE)
def test_counts_beyond_the_hard_maximum_are_refused(peer_type):
    assert history_policy.validate_count(peer_type, 1001) is not None
    assert history_policy.validate_count(peer_type, 5000) is not None


@pytest.mark.parametrize("peer_type", LARGE)
def test_counts_between_offered_choices_are_not_panel_choices(peer_type):
    # The service accepts any positive int up to the hard max (AI tools may ask
    # for a small literal count), but the panel only offers its exact choices.
    assert history_policy.validate_count(peer_type, 999) is None
    assert history_policy.validate_choice(peer_type, 999) is not None


def test_no_clamping_changes_the_requested_or_the_maximum():
    for peer_type in CAPPED:
        assert history_policy.maximum_for(peer_type) == 300
        assert history_policy.validate_count(peer_type, 1000) is not None
        assert history_policy.validate_count(peer_type, 301) is not None
    assert history_policy.maximum_for(PeerType.BROADCAST_CHANNEL) == 300
    assert history_policy.maximum_for(PeerType.PRIVATE_BOT) == 300


def test_refusal_reasons_are_deterministic():
    first = history_policy.validate_count(PeerType.PRIVATE_BOT, 1000)
    second = history_policy.validate_count(PeerType.PRIVATE_BOT, 1000)
    assert first == second

    channel = history_policy.validate_count(PeerType.BROADCAST_CHANNEL, 500)
    assert channel == (
        "A broadcast channel is limited to 300 messages per request; "
        "choose 100, 250 or 300."
    )
    assert history_policy.validate_count(PeerType.PRIVATE_BOT, 500) == (
        "A bot chat is limited to 300 messages per request; choose 100, 250 or 300."
    )
    assert history_policy.validate_choice(PeerType.PRIVATE_USER, 999) == (
        "Choose one of the offered message counts: 100, 250, 500 or 1000."
    )
