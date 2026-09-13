"""
AI response presentation redesign — focused regression tests.

The chat presentation is a renderer only:

    │ <owner message line>
    │
    └─ <first answer line>
        <every later answer line, exactly four ASCII spaces>

There is no trigger label, AI name, emoji, header, or separator anywhere in
the presentation, and the "show my message in replies" preference is
presentation-only: it must never change the model-facing request, the
conversation history, prompts, providers, or tools. The answer is always
delivered into the owner's original message (edit in place).
"""
from __future__ import annotations

import asyncio
import re
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.engine.result import EngineResult
from backend.ai.tools.delivery import (
    SAFE_LIMIT,
    _format_chunks,
    _utf16_units,
    answer_block,
    deliver_response,
    format_presentation,
    question_block,
)

_ANSWER = "سلام، ممنونم. تو خوبی؟\nمن خوبم و آماده‌ام کمکت کنم.\nهر چیزی خواستی بپرس."
_QUESTION = "هی"


# ── A. show-question ON ──────────────────────────────────────────────────────


def test_show_question_on_renders_quoted_question_connector_and_answer():
    out = format_presentation(_QUESTION, _ANSWER, True)
    assert out == (
        "│ هی\n"
        "│\n"
        "└─ سلام، ممنونم. تو خوبی؟\n"
        "    من خوبم و آماده‌ام کمکت کنم.\n"
        "    هر چیزی خواستی بپرس."
    )
    assert out.split("\n")[0] == "│ هی"
    assert out.split("\n")[2].startswith("└─ ")


# ── B. show-question OFF ─────────────────────────────────────────────────────


def test_show_question_off_omits_the_original_message():
    out = format_presentation(_QUESTION, _ANSWER, False)
    assert _QUESTION not in out
    assert "│" not in out
    assert out == (
        "└─ سلام، ممنونم. تو خوبی؟\n"
        "    من خوبم و آماده‌ام کمکت کنم.\n"
        "    هر چیزی خواستی بپرس."
    )


def test_answer_body_is_identical_with_and_without_the_question():
    # Presentation only: hiding the question removes the wrapper, never a
    # single character of the answer.
    shown = format_presentation(_QUESTION, _ANSWER, True)
    hidden = format_presentation(_QUESTION, _ANSWER, False)
    assert shown == f"{question_block(_QUESTION)}\n│\n{hidden}"


# ── C/D. multiline question + exactly one blank connector ────────────────────


def test_multiline_question_quotes_every_line_with_one_connector():
    question = "این سؤال منه\nکه چند خطه و ادامه داره"
    out = format_presentation(
        question,
        "این هم جواب منه که می‌تونه\nچند خط ادامه داشته باشه و\nظاهرش همچنان تمیز بمونه.",
        True,
    )
    lines = out.split("\n")
    assert lines[0] == "│ این سؤال منه"
    assert lines[1] == "│ که چند خطه و ادامه داره"
    assert lines[2] == "│"
    assert lines[3].startswith("└─ ")
    # exactly ONE bare connector line between question and answer
    assert lines[:4].count("│") == 1
    assert sum(1 for line in lines if line == "│") == 1


def test_inner_blank_question_line_renders_as_a_bare_bar():
    assert question_block("a\n\nb") == "│ a\n│\n│ b"


# ── E/F. multiline answer alignment ──────────────────────────────────────────


def test_only_first_answer_line_has_the_marker_and_rest_use_four_spaces():
    lines = answer_block(_ANSWER).split("\n")
    assert lines[0] == "└─ سلام، ممنونم. تو خوبی؟"
    assert lines[0].startswith("└─ ")
    for line in lines[1:]:
        assert line.startswith("    ")
        assert not line.startswith("     ")
        assert not line.startswith("└─")
        assert not line.startswith("│")


def test_continuation_indent_is_exactly_four_ascii_spaces():
    lines = answer_block("one\ntwo").split("\n")
    assert lines[1].encode("utf-8").startswith(b"    ")
    assert len(lines[1]) - len(lines[1].lstrip(" ")) == 4
    assert lines[1][:4] == "    "
    assert lines[1][3] == " " and lines[1][4] != " "


def test_answer_indent_does_not_depend_on_the_question():
    assert answer_block("x\ny") == format_presentation("very long question " * 5, "x\ny", False)


# ── G. no trigger label / AI name / emoji / separator ────────────────────────


def test_presentation_has_no_trigger_label_name_emoji_or_separator():
    for show in (True, False):
        out = format_presentation(_QUESTION, _ANSWER, show)
        assert "🤖" not in out
        assert "────────────" not in out
        assert "Nova" not in out
        assert "❌" not in out
        assert "⏳" not in out
        # no emoji at all: the presentation is Unicode-first and minimal
        assert not any(_is_emoji(char) for char in out)


def _is_emoji(char: str) -> bool:
    """True for emoji/pictographs; box drawing (│ └ ─), zero-width marks, and
    Arabic-script letters are presentation glyphs/text, not emoji."""
    code = ord(char)
    if 0x0600 <= code <= 0x06FF or 0x200C <= code <= 0x200F:
        return False
    if 0x2500 <= code <= 0x257F:  # box drawing
        return False
    return code >= 0x2190


# ── H. edit-in-place delivery ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_answer_is_edited_into_the_original_message():
    edits, replies = [], []

    async def edit(text):
        edits.append(text)

    async def reply(text):
        replies.append(text)

    result = await deliver_response(
        SimpleNamespace(edit=edit, reply=reply), _QUESTION, _ANSWER, True,
    )
    assert result.success
    assert len(edits) == 1  # the answer is edited into the original message
    assert replies == []    # never sent as a separate AI message
    assert edits[0].startswith("│ هی\n│\n└─ ")


@pytest.mark.asyncio
async def test_empty_response_uses_the_same_presentation():
    edits, replies = [], []

    async def edit(text):
        edits.append(text)

    async def reply(text):
        replies.append(text)

    result = await deliver_response(
        SimpleNamespace(edit=edit, reply=reply), "msg", "   ", True,
    )
    assert result.success
    assert edits == ["│ msg\n│\n└─ Error\n    AI returned no response."]
    assert replies == []
    assert "🤖" not in edits[0] and "────────────" not in edits[0]


# ── chunked delivery keeps the same rules ────────────────────────────────────


def test_chunked_presentation_keeps_rules_and_utf16_safety():
    response = "\n".join(f"line-{index} " + "x" * 80 for index in range(200))
    chunks = _format_chunks("سؤال من", response, True)
    assert len(chunks) > 1
    assert all(_utf16_units(chunk) <= SAFE_LIMIT for chunk in chunks)
    assert chunks[0].startswith("│ سؤال من\n│\n└─ ")
    for chunk in chunks:
        body = re.sub(r"\n\n_\(\d+/\d+\)_$", "", chunk)
        body = body.split("│\n", 1)[-1]  # drop the question block from chunk 0
        assert body.startswith("└─ ")
        assert all(
            line.startswith("    ") or line == "    " for line in body.split("\n")[1:]
        )
        assert "🤖" not in chunk and "────────────" not in chunk


def test_no_question_setting_keeps_chunks_without_any_bar():
    chunks = _format_chunks("q", "x" * (SAFE_LIMIT + 500), False)
    assert len(chunks) > 1
    assert not any("│" in chunk for chunk in chunks)


# ── settings toggle ──────────────────────────────────────────────────────────


def _button_datas(buttons) -> list[str]:
    out: list[str] = []
    for row in buttons:
        cells = row if isinstance(row, list) else [row]
        for btn in cells:
            data = getattr(btn, "data", None)
            if isinstance(data, bytes):
                data = data.decode("utf-8", errors="replace")
            out.append(str(data or ""))
    return out


@pytest.mark.asyncio
async def test_settings_toggle_flips_presentation_preference_only():
    from backend.ai.engine.telemetry import telemetry
    from backend.bot.handlers import ai as ai_module

    telemetry.reset_for_tests()
    try:
        with patch.object(ai_module, "_get_owner_id", AsyncMock(return_value=1)), \
             patch.object(ai_module, "_get_saved_config", AsyncMock(return_value={})):
            _, body, buttons = await ai_module._ai_settings_panel_handler(None, "")
            assert "My message in replies · Off" in body
            datas = _button_datas(buttons)
            assert "action:ai_toggle_show_question" in datas
            assert "action:ai_toggle_telemetry" in datas

            await ai_module._ai_toggle_show_question_action(None, "", 100)
            assert telemetry.get_show_question_pref(1) is True

            _, body_on, _ = await ai_module._ai_settings_panel_handler(None, "")
            assert "My message in replies · On" in body_on
    finally:
        telemetry.reset_for_tests()


# ── I. presentation / context separation (end to end) ────────────────────────


class _FakeProviderCfg:
    default_model = "dummy"
    model = "dummy"


class _FakeProvider:
    config = _FakeProviderCfg()


class _FakePM:
    def get_active_name(self) -> str:
        return "dummy"

    def get_active(self) -> _FakeProvider:
        return _FakeProvider()


async def _drive_execute_ai(owner_id: int, prompt: str, result: EngineResult):
    """Run the real ``_execute_ai`` path with a fake engine and capture the
    model-facing request plus the text actually edited into the message."""
    from backend.bot.handlers import ai_unified as module

    captured: dict = {}

    class _Engine:
        provider_manager = _FakePM()
        conversation_manager = MagicMock()

        async def execute(self, request, status_callback=None):
            captured["user_message"] = request.user_message
            captured["message_id"] = request.message_id
            return result

    event = MagicMock()
    event.chat_id = 123
    event.message = MagicMock(id=456)
    event.edit = AsyncMock()
    event.reply = AsyncMock()

    with (
        patch.object(module, "_engine", _Engine()),
        patch("backend.ai.config_store.get_config", new=AsyncMock(return_value={})),
        patch("backend.ai.config_store.record_request", new=AsyncMock(return_value=None)),
        patch(
            "backend.runtime.task_guard.guarded_create_task",
            new=lambda coro, **kw: asyncio.ensure_future(coro),
        ),
    ):
        await module._execute_ai(event, owner_id, prompt, "Nova", "UTC")

    captured["delivered"] = event.edit.await_args_list[-1][0][0]
    return captured


@pytest.mark.asyncio
async def test_preference_changes_presentation_but_not_the_model_request():
    from backend.ai.engine.telemetry import telemetry

    result = EngineResult(
        success=True, provider="dummy", model="dummy", latency=0.1,
        response="پاسخ من", metadata={},
    )
    telemetry.reset_for_tests()
    try:
        telemetry.set_show_question_pref(9, True)
        on = await _drive_execute_ai(9, "هی", result)
        telemetry.set_show_question_pref(9, False)
        off = await _drive_execute_ai(9, "هی", result)
    finally:
        telemetry.reset_for_tests()

    # the model sees exactly the same request either way
    assert on["user_message"] == off["user_message"] == "هی"
    assert on["message_id"] == off["message_id"] == 456
    # only the rendered presentation differs
    assert on["delivered"] == "│ هی\n│\n└─ پاسخ من"
    assert off["delivered"] == "└─ پاسخ من"


@pytest.mark.asyncio
async def test_thinking_state_uses_the_same_presentation():
    from backend.ai.tools.delivery import format_presentation as render

    from backend.bot.handlers.ai_unified import _format_thinking

    assert _format_thinking("هی", True) == render("هی", "Thinking…", True)
    assert _format_thinking("هی", False) == "└─ Thinking…"
    assert "🤖" not in _format_thinking("هی", True)
