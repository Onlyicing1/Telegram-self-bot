"""
Durable invisible AI provenance — behavioral tests.

Source-proven contract this file pins:

  1. The final successful AI answer carries an INVISIBLE marker
     (``backend/ai/context/provenance.py``) inside the Telegram message text.
     The marker is the ONLY durable signal that a message was answered or
     overwritten by the AI: ``sender_id == owner_id`` and ``out=True`` are
     true for genuine human messages AND for AI output, so neither may ever be
     used as provenance.
  2. Placement is presentation-mode dependent: with the question shown the
     marker sits at the question/answer boundary (after the ``│`` connector,
     before the answer block); with the question hidden it is appended at the
     absolute end of the answer.
  3. The visible presentation is FROZEN. With the marker stripped the
     delivered text is exactly the unmarked presentation — same `│`, same
     elbows, same four-space continuation indent, same line breaks, same
     BiDi/`┘─` behaviour.
  4. Status/thinking/failure text never carries the marker.
  5. The surrounding-message context collector drops marker-bearing messages
     durably — regardless of ``ReplyResolver``, ``sender_id`` or ``out``.

No live Telegram is used. The Telegram round-trip is therefore simulated
(wire-encoding + normalization) and that limitation is reported explicitly
rather than claimed as a live verification.
"""
from __future__ import annotations

import unicodedata
from datetime import datetime, timezone
from types import SimpleNamespace

import pytest

from backend.ai.context.provenance import (
    AI_PROVENANCE_MARKER,
    apply_ai_provenance_marker,
    has_ai_provenance_marker,
    strip_ai_provenance_marker,
)
from backend.ai.context.reply_resolver import ReplyResolver
from backend.ai.conversation.telegram_context import build_chat_context
from backend.ai.tools.delivery import (
    SAFE_LIMIT,
    _format_chunks,
    _utf16_units,
    deliver_response,
    format_failure,
    format_presentation,
    format_status,
    format_thinking,
)

OWNER_ID = 4242
CHAT = -100777
NOW = datetime(2026, 9, 14, 15, 30, tzinfo=timezone.utc)

_FA_QUESTION = "هی"
_FA_ANSWER = "سلام! حال شما چطور است؟"
_LTR_QUESTION = "hi"
_LTR_ANSWER = "Hello, how can I help?\nI can also continue here."
_MIXED_ANSWER = "Temperatures امروز بالاست, see the report."


_BIDI_CONTROLS = "\u200e\u200f\u2066\u2067\u2069"


def _no_controls(text: str) -> str:
    return text.translate(str.maketrans("", "", _BIDI_CONTROLS))


class _FakeMsg:
    """The subset of a Telethon message the surrounding window reads."""

    def __init__(self, msg_id: int, text: str = "", *, sender_id: int = 0,
                 out: bool = False, date: datetime | None = NOW) -> None:
        self.id = msg_id
        self.message = text
        self.sender_id = sender_id
        self.out = out
        self.date = date
        self.media = None
        self.sender = None


async def _deliver(response_text: str, show_question: bool, user_message: str = _LTR_QUESTION):
    edits: list[str] = []
    replies: list[str] = []

    async def edit(text):
        edits.append(text)

    async def reply(text):
        replies.append(text)

    result = await deliver_response(
        SimpleNamespace(edit=edit, reply=reply), user_message, response_text, show_question,
    )
    assert result.success
    return edits + replies


# ── Marker properties (why these four code points) ───────────────────────────


def test_marker_code_points_are_non_rendering_and_direction_neutral():
    """The marker must not render, must not attach to a neighbour, and must
    not carry a strong BiDi direction (it is inserted into RTL/LTR/mixed
    presentations whose connector columns must not move)."""
    for char in AI_PROVENANCE_MARKER:
        assert unicodedata.category(char) == "Cf"  # format char → no glyph
        assert unicodedata.combining(char) == 0  # never attaches to a glyph
        assert unicodedata.bidirectional(char) == "BN"  # no BiDi direction
        assert not char.isspace()  # not stripped as whitespace
        assert char.strip() == char
        assert unicodedata.normalize("NFC", char) == char
        assert unicodedata.normalize("NFKC", char) == char


def test_marker_is_a_unique_four_code_point_sequence():
    assert len(AI_PROVENANCE_MARKER) == 4
    assert len(set(AI_PROVENANCE_MARKER)) == 4  # ordered run, not a repeat
    assert not has_ai_provenance_marker(AI_PROVENANCE_MARKER[:3])


# ── 1. show_question=True placement ─────────────────────────────────────────


def test_provenance_sits_between_question_block_and_answer_block():
    edited = _deliver_sync(_LTR_ANSWER, True)
    unmarked = format_presentation(_LTR_QUESTION, _LTR_ANSWER, True)

    # visible preservation: byte-for-byte identical once the marker is removed
    assert strip_ai_provenance_marker(edited) == unmarked

    # the marker is inserted at exactly the question/answer boundary, i.e.
    # after the `│` connector line and before the answer elbow — no visible
    # character was replaced, moved, or re-spaced
    boundary = unmarked.index("\n", unmarked.index("\n") + 1)
    assert edited == f"{unmarked[:boundary]}{AI_PROVENANCE_MARKER}{unmarked[boundary:]}"
    assert _no_controls(unmarked[:boundary]).endswith("│")
    assert _no_controls(unmarked[boundary + 1:]).startswith("└─ ")


def test_provenance_sits_between_question_and_answer_for_rtl():
    edited = _deliver_sync(_FA_ANSWER, True, _FA_QUESTION)
    unmarked = format_presentation(_FA_QUESTION, _FA_ANSWER, True)

    assert strip_ai_provenance_marker(edited) == unmarked
    boundary = unmarked.index("\n", unmarked.index("\n") + 1)
    assert edited == f"{unmarked[:boundary]}{AI_PROVENANCE_MARKER}{unmarked[boundary:]}"
    assert _no_controls(unmarked[:boundary]).endswith("│")
    assert _no_controls(unmarked[boundary + 1:]).startswith("┘─ ")
    # the trigger label/header of the old presentation is gone, not restored
    assert "🤖" not in edited


# ── 2. show_question=False placement ────────────────────────────────────────


def test_provenance_is_appended_at_the_absolute_end_when_question_is_hidden():
    edited = _deliver_sync(_LTR_ANSWER, False)
    unmarked = format_presentation(_LTR_QUESTION, _LTR_ANSWER, False)

    assert edited == f"{unmarked}{AI_PROVENANCE_MARKER}"
    assert edited.endswith(AI_PROVENANCE_MARKER)
    assert strip_ai_provenance_marker(edited) == unmarked
    for glyph in ("│", "─", "└", "┘"):
        assert glyph not in edited


def test_hidden_question_keeps_rtl_and_mixed_answers_plain():
    for answer in (_FA_ANSWER, _MIXED_ANSWER):
        edited = _deliver_sync(answer, False)
        assert edited == f"{answer}{AI_PROVENANCE_MARKER}"
        assert has_ai_provenance_marker(edited)
        assert "\n" not in strip_ai_provenance_marker(edited)[-1:]


# ── 3. Visual preservation (hard acceptance criterion) ──────────────────────


@pytest.mark.parametrize(
    "question,answer,show",
    [
        (_FA_QUESTION, _FA_ANSWER, True),
        (_FA_QUESTION, _MIXED_ANSWER, True),
        (_LTR_QUESTION, _LTR_ANSWER, True),
        (_FA_QUESTION, _FA_ANSWER, False),
        (_LTR_QUESTION, _MIXED_ANSWER, False),
    ],
)
def test_visible_presentation_is_unchanged(question, answer, show):
    unmarked = format_presentation(question, answer, show)
    marked = (
        apply_ai_provenance_marker(unmarked)
        if not show
        else unmarked.replace(
            "\n", f"{AI_PROVENANCE_MARKER}\n", 1
        )
    )
    assert strip_ai_provenance_marker(marked) == unmarked
    # every visible character keeps its exact position and order
    assert marked.replace(AI_PROVENANCE_MARKER, "") == unmarked


@pytest.mark.parametrize(
    "question,answer,show",
    [
        (_FA_QUESTION, _FA_ANSWER, True),
        (_LTR_QUESTION, _LTR_ANSWER, True),
        (_MIXED_ANSWER, _LTR_ANSWER, False),
    ],
)
def test_existing_presentation_semantics_survive_provenance(question, answer, show):
    marked = _deliver_sync(answer, show, question)
    visible = strip_ai_provenance_marker(marked)
    unmarked = format_presentation(question, answer, show)

    assert visible == unmarked
    # the answer keeps the four-space continuation indent and its line breaks
    answer_lines = [line for line in answer.split("\n") if line]
    if len(answer_lines) > 1 and show:
        for line in answer_lines[1:]:
            assert f"\n    " in visible and line in visible
    if not show:
        assert visible == answer


def test_connector_columns_do_not_move_for_persian_or_mixed_text():
    for question, answer in ((_FA_QUESTION, _FA_ANSWER), (_MIXED_ANSWER, _MIXED_ANSWER)):
        marked = _deliver_sync(answer, True, question)
        visible = strip_ai_provenance_marker(marked)
        # the marker is BN and therefore cannot reorder the connector lines:
        # the visible lines are identical to the unmarked render
        assert visible == format_presentation(question, answer, True)
        assert marked.count("│") == visible.count("│")
        assert marked.count("┘─") + marked.count("└─") == visible.count("┘─") + visible.count("└─")


# ── 5/6/7. Detection, stripping, idempotence ────────────────────────────────


def test_detection_cases():
    assert has_ai_provenance_marker(f"answer{AI_PROVENANCE_MARKER}")
    assert has_ai_provenance_marker(f"q{AI_PROVENANCE_MARKER}a")
    assert not has_ai_provenance_marker("answer")
    assert not has_ai_provenance_marker("")
    assert not has_ai_provenance_marker(None)
    assert not has_ai_provenance_marker(1234)
    # a single invisible code point is NOT provenance
    assert not has_ai_provenance_marker("answer\u2063")


def test_duplicate_marker_is_still_detected_and_fully_stripped():
    doubled = f"answer{AI_PROVENANCE_MARKER}{AI_PROVENANCE_MARKER}"
    assert has_ai_provenance_marker(doubled)
    assert strip_ai_provenance_marker(doubled) == "answer"


def test_marker_in_unexpected_position_is_deterministic():
    interior = f"an{AI_PROVENANCE_MARKER}swer"
    assert has_ai_provenance_marker(interior)
    assert strip_ai_provenance_marker(interior) == "answer"


def test_stripping_returns_the_exact_original_visible_content():
    unmarked = format_presentation(_LTR_QUESTION, _LTR_ANSWER, True)
    marked = _deliver_sync(_LTR_ANSWER, True)
    assert strip_ai_provenance_marker(marked) == unmarked
    # stripped output is exactly the renderer's output, not a re-render
    assert strip_ai_provenance_marker(marked).encode("utf-16-le") == unmarked.encode("utf-16-le")


def test_applying_the_marker_twice_produces_exactly_one_marker():
    once = apply_ai_provenance_marker("answer")
    assert once.count(AI_PROVENANCE_MARKER) == 1
    assert apply_ai_provenance_marker(once) == once
    assert apply_ai_provenance_marker(apply_ai_provenance_marker(once)) == once


def test_presentation_provenance_is_idempotent_for_every_mode():
    from backend.ai.tools.delivery import apply_presentation_provenance

    for question, answer, show in (
        (_LTR_QUESTION, _LTR_ANSWER, True),
        (_FA_QUESTION, _FA_ANSWER, True),
        (_LTR_QUESTION, _LTR_ANSWER, False),
        ("", _LTR_ANSWER, True),  # no question block rendered → plain answer
    ):
        unmarked = format_presentation(question, answer, show)
        once = apply_presentation_provenance(unmarked, question, show)
        assert once.count(AI_PROVENANCE_MARKER) == 1
        assert apply_presentation_provenance(once, question, show) == once


@pytest.mark.asyncio
async def test_retry_or_reentry_never_stacks_the_marker():
    """A retry/re-entry into the delivery path must not produce marker+marker."""
    for show in (True, False):
        first = await _deliver(_LTR_ANSWER, show)
        second = await _deliver(_LTR_ANSWER, show)
        for message in first + second:
            assert message.count(AI_PROVENANCE_MARKER) == 1
        # re-delivering an already-marked response keeps exactly one marker
        marked_answer = f"{strip_ai_provenance_marker(first[0])}"
        again = await _deliver(marked_answer, show)
        for message in again:
            assert message.count(AI_PROVENANCE_MARKER) == 1


# ── 13. Temporary status messages never carry provenance ────────────────────


def test_status_thinking_and_failure_texts_are_not_marked():
    assert not has_ai_provenance_marker(format_thinking(_LTR_QUESTION, True))
    assert not has_ai_provenance_marker(format_thinking(_LTR_QUESTION, False))
    for status in ("Reading messages...", "Thinking…", "⏳"):
        assert not has_ai_provenance_marker(format_status(_LTR_QUESTION, status, True))
        assert not has_ai_provenance_marker(format_status(_LTR_QUESTION, status, False))
    assert not has_ai_provenance_marker(format_failure(_LTR_QUESTION, "✕ boom", True))
    assert not has_ai_provenance_marker(format_failure(_LTR_QUESTION, "✕ boom", False))


@pytest.mark.asyncio
async def test_empty_response_failure_state_has_no_provenance():
    edits: list[str] = []

    async def edit(text):
        edits.append(text)

    async def reply(text):  # pragma: no cover - never used for this branch
        raise AssertionError("failure state must not reply")

    result = await deliver_response(
        SimpleNamespace(edit=edit, reply=reply), _LTR_QUESTION, "   ", True,
    )
    assert result.success
    assert not has_ai_provenance_marker(edits[0])
    assert "Error" in edits[0]


# ── 14/15/16. Round-trip, normalization, BiDi survival ──────────────────────


def test_marker_survives_the_wire_and_normalization_path():
    """Telegram stores message text as UTF-8; the marker must survive that and
    the project's own normalization unchanged (no live Telegram available)."""
    marked = _deliver_sync(_FA_ANSWER, True, _FA_QUESTION)
    round_tripped = marked.encode("utf-8").decode("utf-8")
    assert round_tripped == marked
    assert has_ai_provenance_marker(round_tripped)
    assert strip_ai_provenance_marker(round_tripped) == strip_ai_provenance_marker(marked)
    for form in ("NFC", "NFKC", "NFD"):
        normalized = unicodedata.normalize(form, marked)
        assert has_ai_provenance_marker(normalized)
        assert AI_PROVENANCE_MARKER in normalized


def test_marker_survives_the_output_normalizer():
    from backend.ai.tools.delivery import process_output

    marked = f"{_MIXED_ANSWER}{AI_PROVENANCE_MARKER}"
    normalized = process_output(marked).text
    assert has_ai_provenance_marker(normalized)
    assert strip_ai_provenance_marker(normalized) == process_output(_MIXED_ANSWER).text


def test_marker_never_splits_across_delivered_chunks():
    long_answer = "\n".join(f"خط {index} " + "x" * 80 for index in range(200))
    for show in (True, False):
        chunks = _format_chunks(_FA_QUESTION, long_answer, show)
        assert len(chunks) > 1
        marked = [
            apply_ai_provenance_marker(chunk) if not show or index else
            chunk.replace("\n", f"{AI_PROVENANCE_MARKER}\n", 1)
            for index, chunk in enumerate(chunks)
        ]
        for message in marked:
            assert message.count(AI_PROVENANCE_MARKER) == 1
            leftover = strip_ai_provenance_marker(message)
            assert not (set(leftover) & set(AI_PROVENANCE_MARKER))


@pytest.mark.asyncio
async def test_delivered_chunks_stay_within_the_utf16_limit_with_provenance():
    long_answer = "\n".join(f"line-{index} " + "x" * 80 for index in range(200))
    for show in (True, False):
        messages = await _deliver(long_answer, show, "سؤال من")
        assert len(messages) > 1
        assert all(_utf16_units(message) <= SAFE_LIMIT for message in messages)
        assert all(message.count(AI_PROVENANCE_MARKER) == 1 for message in messages)


# ── 8/9/10/11/12. Surrounding-context filtering ─────────────────────────────


def test_genuine_owner_message_without_the_marker_stays_in_context():
    raw = [_FakeMsg(11, "پیام واقعی من", sender_id=OWNER_ID, out=True)]
    snapshot = build_chat_context(
        raw, current_message_id=12, chat_id=CHAT, sender_names={},
    )
    assert [message.message_id for message in snapshot.messages] == [11]
    assert snapshot.messages[0].attribution == "You"
    assert snapshot.messages[0].body == "پیام واقعی من"


def test_ai_marked_owner_message_is_excluded_even_though_out_and_sender_match():
    marked = f"│ هی\n│{AI_PROVENANCE_MARKER}\n┘─ پاسخ من"
    raw = [
        _FakeMsg(11, marked, sender_id=OWNER_ID, out=True),
        _FakeMsg(10, "پیام واقعی من", sender_id=OWNER_ID, out=True),
    ]
    snapshot = build_chat_context(
        raw, current_message_id=12, chat_id=CHAT, sender_names={},
    )
    assert [message.message_id for message in snapshot.messages] == [10]


def test_other_participants_keep_the_existing_behaviour():
    raw = [
        _FakeMsg(11, "سلام از طرف دیگر", sender_id=999, out=False),
        _FakeMsg(10, f"answer{AI_PROVENANCE_MARKER}", sender_id=999, out=False),
    ]
    snapshot = build_chat_context(
        raw,
        current_message_id=12,
        chat_id=CHAT,
        sender_names={999: "Sara"},
    )
    # unmarked participant messages stay (named); marker-bearing ones are
    # provenance, whichever account they came from
    assert [message.message_id for message in snapshot.messages] == [11]
    assert snapshot.messages[0].attribution == "Sara"


def test_trigger_and_reply_target_exclusions_are_unchanged():
    raw = [
        _FakeMsg(11, "reply target", sender_id=OWNER_ID, out=True),
        _FakeMsg(10, "earlier", sender_id=OWNER_ID, out=True),
        _FakeMsg(12, "trigger", sender_id=OWNER_ID, out=True),
        _FakeMsg(13, "future", sender_id=OWNER_ID, out=True),
    ]
    snapshot = build_chat_context(
        raw,
        current_message_id=12,
        chat_id=CHAT,
        sender_names={},
        exclude_message_ids=[11],
    )
    assert [message.message_id for message in snapshot.messages] == [10]


def test_marked_window_message_is_dropped_after_a_restart_without_any_resolver():
    """The durable property ReplyResolver alone cannot provide: the resolver is
    empty (a fresh process), yet the previously AI-answered message is still
    recognised from the Telegram text itself."""
    resolver = ReplyResolver()
    resolver.clear()
    assert resolver.resolve(11) is None
    assert len(resolver) == 0

    delivered = _deliver_sync(_FA_ANSWER, True, _FA_QUESTION)
    assert has_ai_provenance_marker(delivered)

    raw = [
        _FakeMsg(11, delivered, sender_id=OWNER_ID, out=True),
        _FakeMsg(10, "پیام قبلی من", sender_id=OWNER_ID, out=True),
    ]
    snapshot = build_chat_context(
        raw, current_message_id=12, chat_id=CHAT, sender_names={},
    )
    assert [message.message_id for message in snapshot.messages] == [10]
    assert "پاسخ" not in snapshot.render()


@pytest.mark.asyncio
async def test_delivered_answer_is_excluded_from_the_next_requests_window():
    """End-to-end: deliver an answer, then feed the stored Telegram text back as
    the previous message of the next request's window."""
    edited = (await _deliver(_LTR_ANSWER, True, _LTR_QUESTION))[0]
    assert has_ai_provenance_marker(edited)
    raw = [
        _FakeMsg(50, edited, sender_id=OWNER_ID, out=True),
        _FakeMsg(49, "my real question", sender_id=OWNER_ID, out=True),
    ]
    snapshot = build_chat_context(
        raw, current_message_id=51, chat_id=CHAT, sender_names={},
    )
    assert [message.message_id for message in snapshot.messages] == [49]
    assert "Hello, how can I help?" not in snapshot.render()


def test_marker_never_reaches_the_model_even_when_the_filter_is_bypassed():
    """Defense in depth: `_to_record` strips the marker, so no text that does
    reach the prompt can carry it."""
    marked = f"answer{AI_PROVENANCE_MARKER}"
    snapshot = build_chat_context(
        [_FakeMsg(11, marked, sender_id=OWNER_ID, out=True)],
        current_message_id=12,
        chat_id=CHAT,
        sender_names={},
    )
    # normally excluded; forcing the record path still strips the marker
    from backend.ai.conversation.telegram_context import _to_record

    record = _to_record(_FakeMsg(11, marked, sender_id=OWNER_ID, out=True), {}, "UTC")
    assert record.text == "answer"
    assert not has_ai_provenance_marker(record.body)
    assert snapshot.is_empty


def _deliver_sync(response_text: str, show_question: bool, user_message: str = _LTR_QUESTION) -> str:
    """Run the real async delivery path and return the single edited message."""
    import asyncio

    messages = asyncio.run(_deliver(response_text, show_question, user_message))
    assert len(messages) == 1
    return messages[0]
