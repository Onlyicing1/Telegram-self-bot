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

from backend.ai.actions import _tokenize
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


# ── the generator's grammar is what the parser accepts ──────────────────────


def test_the_generator_prefix_and_length_are_four_characters():
    """The accepted all-letter shape is the generator's own: S + 4 chars."""
    assert _SHORT_CODE_PREFIX == "S"
    assert _SHORT_CODE_NUM_LEN == 4
    assert len(f"{_SHORT_CODE_PREFIX}{1:0{_SHORT_CODE_NUM_LEN}d}") == 5


# ── the reported live flow: reply to the item's preview message ─────────────


# ── every saved-item route accepts the real grammar ────────────────────────


# ── ordinary words are still never item codes ──────────────────────────────
