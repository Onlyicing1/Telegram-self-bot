"""Regression tests for the save-code grammar used by deterministic routing.

``backend/db/client.py::get_next_save_code`` emits the short form
``_SHORT_CODE_PREFIX`` (``"S"``) + four characters from
``_SHORT_CODE_ALPHABET`` (``A-Z`` + ``0-9``): a zero-padded sequential code
(``S0001``) or a random alphanumeric one (``SAXCK``). An all-letter code
therefore carries NO digit.

The extraction in ``backend/ai/actions.py`` additionally required a digit, so
a real code such as ``SAXCK`` was not recognized: replying to that item's
preview message with "send this" fell through the deterministic route and was
answered with ``❌ Unsupported action: send``. These tests pin the corrected
grammar, the exclusions that keep ordinary words out of it, and the fact that
resolution stays deterministic (no reply content ever reaches a provider).

No live Telegram, no database, no providers: the parser and the existing
preview formatter are exercised directly.
"""
from __future__ import annotations

import json
import random

import pytest

from backend.ai.actions import (
    _extract_save_code,
    _extract_single_save_code,
    _tokenize,
    parse_command_intent,
)
from backend.db.client import (
    _SHORT_CODE_ALPHABET,
    _SHORT_CODE_NUM_LEN,
    _SHORT_CODE_PREFIX,
)
from backend.services.retrieve_service import format_preview

# A stored item as the generator writes it: an all-letter random short code
# plus the preview's own field labels ("Size" / "Sender" / "Saved") — the
# exact shape of the message the owner replied to in the reported incident.
ROW = {
    "id": 41,
    "save_code": "SAXCK",
    "owner_id": 777,
    "media_type": "Photo",
    "mime_type": "image/jpeg",
    "file_size": 173_800,
    "sender_name": "Sarah",
    "created_at": "2026-09-15T10:08:00+00:00",
}
PREVIEW_TEXT = format_preview(ROW)
NUMERIC_ROW = dict(ROW, save_code="S0001", sender_name="Owner Name")

SEND_REQUESTS = ("send this", "send it here", "بفرست", "اینو بفرست")
DELETE_REQUESTS = ("delete this", "delete it", "اینو پاک کن", "این پیام رو پاک کن")


# ── the generator's grammar is what the parser accepts ──────────────────────


def test_the_generators_own_alphabet_produces_codes_the_parser_accepts():
    """Every short code the generator can mint must be a code to the parser.

    ``get_next_save_code`` draws exactly ``k=4`` characters from the same
    alphabet for its random branch, so the sample below is drawn from the
    real distribution rather than from a hand-written list of examples.
    """
    rng = random.Random(20260915)
    checked = 0
    for _ in range(250):
        tail = "".join(rng.choices(_SHORT_CODE_ALPHABET, k=4))
        code = f"{_SHORT_CODE_PREFIX}{tail}"
        if code == "S0001":  # sequential branch, covered separately
            continue
        assert _extract_single_save_code(f"**LifeOS** `{code}`") == code, code
        checked += 1
    assert checked > 200


def test_the_generator_prefix_and_length_are_four_characters():
    """The accepted all-letter shape is the generator's own: S + 4 chars."""
    assert _SHORT_CODE_PREFIX == "S"
    assert _SHORT_CODE_NUM_LEN == 4
    assert len(f"{_SHORT_CODE_PREFIX}{1:0{_SHORT_CODE_NUM_LEN}d}") == 5


def test_an_all_letter_short_code_is_recognized():
    assert _extract_save_code(_tokenize("SAXCK")) == "SAXCK"
    assert _extract_single_save_code("**LifeOS** `SAXCK`") == "SAXCK"


def test_numeric_short_codes_keep_working():
    assert _extract_save_code(_tokenize("S0001")) == "S0001"
    assert _extract_save_code(_tokenize("s0001")) == "S0001"
    assert _extract_single_save_code("**LifeOS** `S0001`") == "S0001"
    assert _extract_single_save_code("codes S0001 and S0002 are close") is None


# ── the reported live flow: reply to the item's preview message ─────────────


def test_reply_to_the_preview_message_of_an_all_letter_code_sends_it():
    """Live misroute: "send this" on the ``SAXCK`` preview → "unsupported"."""
    for request_text in SEND_REQUESTS:
        result = parse_command_intent(request_text, has_reply=True, reply_text=PREVIEW_TEXT)

        assert result.kind == "executable", request_text
        assert result.action == "retrieve_save", request_text
        assert result.save_code == "SAXCK", request_text
        assert result.tool_calls == [
            {"name": "retrieve_save", "arguments": {"save_code": "SAXCK"}}
        ], request_text


def test_reply_to_the_preview_message_of_an_all_letter_code_deletes_the_item():
    for request_text in DELETE_REQUESTS:
        result = parse_command_intent(request_text, has_reply=True, reply_text=PREVIEW_TEXT)

        assert result.action == "delete_saved_item", request_text
        assert result.save_code == "SAXCK", request_text
        assert result.tool_calls == [
            {"name": "delete_save", "arguments": {"save_code": "SAXCK"}}
        ], request_text


def test_the_previews_own_labels_and_sender_name_are_never_read_as_the_code():
    """The replied body carries "Size"/"Sender"/"Saved" and the sender name."""
    for word in ("Photo", "Type", "Format", "Size", "Sender", "Sarah", "Saved"):
        assert _extract_single_save_code(word) is None, word
    assert _extract_single_save_code(PREVIEW_TEXT) == "SAXCK"


def test_a_numeric_code_reply_still_resolves_when_the_sender_name_looks_like_one():
    """A 5-letter sender name can never shadow a digit-bearing code."""
    preview = format_preview(dict(NUMERIC_ROW, sender_name="Sarah"))

    result = parse_command_intent("send this", has_reply=True, reply_text=preview)
    assert result.save_code == "S0001"

    # A digit-bearing code wins over an all-letter candidate (deterministic
    # precedence, not a guess), and two all-letter candidates resolve to
    # nothing at all rather than to an arbitrary one.
    assert _extract_single_save_code("codes `SAXCK` and `S0001`") == "S0001"
    assert _extract_single_save_code("codes `SAXCK` and `STILL`") is None


# ── every saved-item route accepts the real grammar ────────────────────────


def test_an_all_letter_code_works_in_every_saved_item_route():
    send = parse_command_intent("سیو SAXCK رو اینجا بفرست", has_reply=False)
    assert send.action == "retrieve_save" and send.save_code == "SAXCK"

    delete = parse_command_intent("سیو SAXCK رو پاک کن", has_reply=False)
    assert delete.action == "delete_saved_item" and delete.save_code == "SAXCK"

    preview = parse_command_intent("مشخصات سیو SAXCK رو بده", has_reply=False)
    assert preview.action == "preview_saved_item" and preview.save_code == "SAXCK"


def test_the_reply_resolution_carries_only_the_save_code():
    """Nothing from the replied message is handed to the execution layer."""
    result = parse_command_intent("send this", has_reply=True, reply_text=PREVIEW_TEXT)

    serialized = json.dumps(result.tool_calls)
    assert serialized == '[{"name": "retrieve_save", "arguments": {"save_code": "SAXCK"}}]'
    for leaked in ("Sender", "Sarah", "LifeOS", "Saved", "Size", "mime", "173"):
        assert leaked not in serialized, leaked


# ── ordinary words are still never item codes ──────────────────────────────


def test_ordinary_words_are_not_short_codes():
    assert _extract_save_code(["save", "saved", "semantic", "s4h"]) == "S4H"
    assert _extract_save_code(["save", "saved", "semantic"]) is None
    assert _extract_save_code(["store", "share", "state", "saves"]) is None
    for word in ("size", "sender", "semantic", "save", "saved", "send"):
        assert _extract_save_code([word]) is None, word


def test_an_ordinary_word_is_never_guessed_into_a_target():
    """No item is ever invented for a request that names none."""
    assert parse_command_intent("salam, send it", has_reply=False).kind == "unsupported"

    # The existing save/list vocabulary keeps its own routing; what matters
    # here is that no saved-item target is invented for it.
    for text in ("send me the saved item", "send me that saved item"):
        for has_reply in (False, True):
            result = parse_command_intent(text, has_reply=has_reply)
            assert not any(
                call["name"] in ("retrieve_save", "delete_save", "preview_save")
                for call in result.tool_calls
            ), (text, has_reply, result.tool_calls)

    for reply in ("salam, how are you?", "just a photo caption", "", "share the link"):
        result = parse_command_intent("delete this", has_reply=True, reply_text=reply)
        assert result.action == "delete_messages", reply
        assert result.tool_calls == [{"name": "delete_replied", "arguments": {}}], reply


def test_a_code_shaped_token_outside_the_generator_shape_is_not_a_code():
    """An all-letter token must be exactly ``S`` + 4 characters."""
    for reply in ("**LifeOS** `SABCDE`", "**LifeOS** `SABC`", "**LifeOS** `S`"):
        result = parse_command_intent("send this", has_reply=True, reply_text=reply)
        assert result.kind == "unsupported", reply
        assert _extract_single_save_code(reply) is None, reply


def test_a_digit_bearing_token_keeps_its_existing_permissive_rule():
    """Unchanged: ``S`` + 1..11 alphanumerics containing a digit is a code.

    That branch is what resolves legacy/short digit codes (``S0001``,
    ``s4h``) and is deliberately left as it was.
    """
    assert _extract_single_save_code("**LifeOS** `SAXCK1`") == "SAXCK1"
    assert _extract_single_save_code("**LifeOS** `S4H`") == "S4H"


@pytest.mark.parametrize("reply", ["`SAXCK`", "`SABCD`", "`S0001`"])
def test_one_canonical_code_in_a_reply_resolves(reply):
    result = parse_command_intent("delete this", has_reply=True, reply_text=reply)
    assert result.action == "delete_saved_item"
    assert result.save_code == reply.strip("`")
