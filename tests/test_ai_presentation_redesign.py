"""
AI reply presentation — focused regression tests.

Four distinct presentation states, rendered by `backend/ai/tools/delivery.py`:

  THINKING  format_thinking/format_status — question bars + plain text,
            NO answer connector (nothing may imply an answer exists)
  FAILURE   format_failure — notice only, never a successful-answer elbow
  ANSWER ON format_presentation(..., True)  — question bars + one blank `│`
            connector + directional elbow answer block
  ANSWER OFF format_presentation(..., False) — ONLY the answer block: no `│`,
            no connector lines, no structure pretending a question exists

The elbow follows the dominant direction of the rendered text
(LTR `└─ ` / RTL `─┘ `); continuation lines use exactly four ASCII spaces.
The "show my message in replies" preference is presentation-only and DURABLE:
it lives on the owner's `ai_config` row via `backend/ai/config_store.py`
(ExecutionTelemetry is never its source of truth), is threaded through the
already-loaded request snapshot, and never changes model input, history,
prompts, providers, or tools. The answer is always edited into the owner's
original Telegram message.
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
    deliver_response,
    format_failure,
    format_presentation,
    format_status,
    format_thinking,
)

_LTR_ANSWER = "Hello, how can I help?\nI can also continue here."
_RTL_ANSWER = "سلام، ممنونم. تو خوبی؟\nمن خوبم و آماده‌ام کمکت کنم.\nهر چیزی خواستی بپرس."
_FA_QUESTION = "هی"
_FA_ANSWER = "سلام!"


_BIDI_CONTROLS = "\u2066\u2067\u2069"


def _without_bidi_controls(text: str) -> str:
    return text.translate(str.maketrans("", "", _BIDI_CONTROLS))


# ── B. OFF mode: PLAIN answer text, no connector anywhere ────────────────────


@pytest.mark.parametrize(
    ("question", "answer", "rtl"),
    [
        ("سؤال فارسی", "پاسخ فارسی", True),
        ("English question", "English answer", False),
        ("سؤال فارسی English", "پاسخ فارسی English", True),
        ("English question فارسی", "answer فارسی", False),
        ("سؤال فارسی", "12345?! ---", False),
        ("سؤال فارسی", "خط اول فارسی\nEnglish continuation\nخط سوم فارسی", True),
        ("English question", "first line\nپاسخ دوم", False),
    ],
)
def test_each_connected_line_isolated_in_the_question_direction(question, answer, rtl):
    """The controls are the machine-verifiable part of the Telegram BiDi fix.

    The logical glyph order remains the public presentation contract; the
    isolate controls stop neutral box-drawing characters and an English line
    from resolving against the surrounding paragraph. Pixel placement still
    needs live Telegram clients (documented in IMPLEMENTATION_REPORT.md).
    """
    out = format_presentation(question, answer, True)
    lines = out.split("\n")
    question_rtl = any("\u2067" in char_line for char_line in lines[:1])
    answer_rtl = rtl
    question_opener = "\u2067" if question_rtl else "\u2066"
    answer_opener = "\u2067" if answer_rtl else "\u2066"
    closer = "\u2069"
    question_lines = len(question.splitlines())
    connector_index = question_lines
    answer_index = connector_index + 1
    assert all(line.startswith(question_opener) and line.endswith(closer) for line in lines[:question_lines])
    assert lines[connector_index] == f"{question_opener}│{closer}"
    assert lines[answer_index].startswith(answer_opener) and lines[answer_index].endswith(closer)
    expected_mark = "─┘" if answer_rtl else "└─"
    assert expected_mark in lines[answer_index]
    for line in lines[answer_index + 1:]:
        assert line.startswith("    " + answer_opener)
        assert line.endswith(closer)


def test_rtl_spacer_and_question_markers_share_right_to_left_isolation():
    out = format_presentation("سؤال\nادامه", "جواب", True)
    lines = out.split("\n")
    assert lines[0].startswith("\u2067│")
    assert lines[1].startswith("\u2067│")
    assert lines[2] == "\u2067│\u2069"
    assert lines[3].startswith("\u2067 ─┘")


def test_ltr_spacer_preserves_left_to_right_isolation():
    out = format_presentation("question\ncontinued", "answer", True)
    lines = out.split("\n")
    assert lines[0].startswith("\u2066│")
    assert lines[1].startswith("\u2066│")
    assert lines[2] == "\u2066│\u2069"
    assert lines[3].startswith("\u2066└─")


def test_off_mode_is_plain_answer_text_with_no_connector():
    out = format_presentation(_FA_QUESTION, _FA_ANSWER, False)
    assert out == "سلام!"
    for glyph in ("│", "─", "└", "┘"):
        assert glyph not in out
    assert _FA_QUESTION not in out


def test_off_mode_multiline_rtl_answer_has_no_connector_at_all():
    out = format_presentation(_FA_QUESTION, _RTL_ANSWER, False)
    for glyph in ("│", "─", "└", "┘"):
        assert glyph not in out
    assert out == _RTL_ANSWER


# ── C. ON mode: question bars + one blank connector + answer ─────────────────


def test_on_mode_renders_question_connector_and_answer():
    out = format_presentation(_FA_QUESTION, _RTL_ANSWER, True)
    assert _without_bidi_controls(out) == (
        "│ هی\n"
        "│\n"
        " ─┘ سلام، ممنونم. تو خوبی؟\n"
        "    من خوبم و آماده‌ام کمکت کنم.\n"
        "    هر چیزی خواستی بپرس."
    )


def test_on_mode_multiline_question_and_exactly_one_connector():
    out = _without_bidi_controls(format_presentation("این سؤال منه\nکه چند خطه و ادامه داره",
                              "این هم جواب منه که می‌تونه\nچند خط ادامه داشته باشه و\nظاهرش همچنان تمیز بمونه.",
                              True))
    lines = out.split("\n")
    assert lines[0] == "│ این سؤال منه"
    assert lines[1] == "│ که چند خطه و ادامه داره"
    assert lines[2] == "│"
    assert sum(1 for line in lines if line == "│") == 1
    assert lines[3].startswith(" ─┘ ")


# ── D. four-space continuation ───────────────────────────────────────────────


@pytest.mark.parametrize("answer", [_LTR_ANSWER, _RTL_ANSWER])
def test_continuation_lines_use_exactly_four_ascii_spaces(answer):
    out = _without_bidi_controls(format_presentation("q", answer, True))
    body = out.split("\n│\n", 1)[-1]  # drop the question block
    lines = body.split("\n")
    for line in lines[1:]:
        assert line.startswith("    ")
        assert not line.startswith("     ")
        assert line[:4] == "    "
        assert line[3] == " " and line[4] != " "


# ── E/F/G. directional elbow ─────────────────────────────────────────────────


def test_ltr_answer_uses_left_elbow():
    out = _without_bidi_controls(format_presentation("Hello", _LTR_ANSWER, True))
    assert "└─ Hello, how can I help?" in out
    assert "─┘" not in out


def test_rtl_answer_uses_mirrored_elbow():
    out = _without_bidi_controls(format_presentation(_FA_QUESTION, _RTL_ANSWER, True))
    assert " ─┘ سلام، ممنونم. تو خوبی؟" in out
    assert "└─" not in out
    # mirrored elbow: arm first, then the vertical stroke pointing at the text
    first = _without_bidi_controls(out).split("\n│\n", 1)[1].split("\n")[0]
    assert first[:4] == " ─┘ "


def test_mixed_direction_follows_the_first_strong_character():
    mixed_rtl_first = _without_bidi_controls(format_presentation("q", "سلام دنیا this is English بعد از فارسی", True)).split("\n│\n", 1)[1]
    mixed_ltr_first = _without_bidi_controls(format_presentation("q", "this is English سلام دنیا and فارسی", True)).split("\n│\n", 1)[1]
    assert mixed_rtl_first.startswith(" ─┘ ")
    assert mixed_ltr_first.startswith("└─ ")
    # deterministic: same input, same direction decision
    for text in ("سلام دنیا this is English بعد از فارسی", "this is English سلام دنیا"):
        once = _without_bidi_controls(format_presentation("q", text, True))
        assert _without_bidi_controls(format_presentation("q", text, True)) == once


def test_neutral_text_defaults_to_ltr():
    answer = _without_bidi_controls(format_presentation("q", "12345 **bold** ---", True)).split("\n│\n", 1)[1]
    assert answer.startswith("└─ ")


def test_direction_decided_from_the_rendered_text_not_any_language_setting():
    # An English answer with a Persian QUESTION must still render LTR.
    out = _without_bidi_controls(format_presentation("سلام این سؤال منه", _LTR_ANSWER, True))
    assert "└─ Hello, how can I help?" in out
    # A Persian answer with an English QUESTION must still render RTL.
    out = _without_bidi_controls(format_presentation("my question", _RTL_ANSWER, True))
    assert " ─┘ سلام، ممنونم. تو خوبی؟" in out


# ── H. multiline answers keep the elbow only on the first line ───────────────


@pytest.mark.parametrize("answer", [_LTR_ANSWER, _RTL_ANSWER])
def test_connector_appears_only_on_the_first_answer_line(answer):
    out = _without_bidi_controls(format_presentation("q", answer, True))
    first, *rest = out.split("\n│\n", 1)[1].split("\n")
    assert first.lstrip(" ").startswith(("└─", "─┘"))
    assert all("└─" not in line and "─┘" not in line for line in rest)
    assert all(line.startswith("    ") for line in rest)


# ── I. THINKING state: no answer connector, no fake answer structure ─────────


def test_thinking_state_never_shows_the_answer_connector():
    for show in (True, False):
        out = format_thinking(_FA_QUESTION, show)
        assert "└─" not in out and "─┘" not in out
        assert "└" not in out and "┘" not in out
    assert _without_bidi_controls(format_thinking(_FA_QUESTION, True)) == "│ هی\n│\nThinking…"
    assert format_thinking(_FA_QUESTION, False) == "Thinking…"


def test_status_note_is_part_of_the_thinking_state_only():
    out = format_status(_FA_QUESTION, "Reading context…", True)
    assert _without_bidi_controls(out) == "│ هی\n│\nReading context…"
    assert format_status(_FA_QUESTION, "Reading context…", False) == "Reading context…"
    # empty/whitespace status falls back to the plain thinking state
    assert format_status(_FA_QUESTION, "  ", True) == format_thinking(_FA_QUESTION, True)
    # a status never gains the answer elbow
    assert "└" not in out and "┘" not in out


# ── J. FAILURE state: never looks like a successful answer ───────────────────


def test_failure_state_has_no_answer_connector():
    for show in (True, False):
        out = format_failure(_FA_QUESTION, "✕ Couldn't get a response\nTimeout", show)
        assert "└─" not in out and "─┘" not in out
    assert _without_bidi_controls(format_failure(_FA_QUESTION, "✕ Couldn't get a response\nTimeout", True)) == (
        "│ هی\n│\n✕ Couldn't get a response\nTimeout"
    )
    assert format_failure(_FA_QUESTION, "✕ Couldn't get a response\nTimeout", False) == (
        "✕ Couldn't get a response\nTimeout"
    )


def test_handler_error_and_failure_formatters_use_the_failure_state():
    from backend.bot.handlers.ai_unified import _format_error, _format_failure

    for formatter in (_format_error, _format_failure):
        for show in (True, False):
            out = formatter("q", "boom", show)
            assert "└" not in out and "┘" not in out


# ── no emoji, no trigger label, no separator anywhere ────────────────────────


@pytest.mark.parametrize("render", [
    lambda: format_presentation(_FA_QUESTION, _RTL_ANSWER, True),
    lambda: format_presentation(_FA_QUESTION, _RTL_ANSWER, False),
    lambda: format_thinking(_FA_QUESTION, True),
    lambda: format_status(_FA_QUESTION, "x", True),
    lambda: format_failure(_FA_QUESTION, "✕ Timeout", True),
])
def test_no_emoji_trigger_label_or_separator(render):
    out = render()
    assert "🤖" not in out
    assert "────────────" not in out
    assert "Nova" not in out
    assert not any(_is_emoji(char) for char in out)


def _is_emoji(char: str) -> bool:
    """Emoji = supplementary pictographs; text symbols (✕, ─, └, arrows) and
    Arabic-script text are presentation glyphs, not emoji."""
    return ord(char) >= 0x1F000


# ── A. durable preference (ai_config via config_store) ───────────────────────


class _FakeTable:
    """Minimal Supabase-shaped table: select/eq/maybe_single/update/insert.

    ``update()`` with no pending payload returns the rows matched by the
    chained ``eq()`` filters (that is what the real client does), which is
    how ``_get_config_sync``'s ``select("*")`` probe reads the stored row.
    """

    def __init__(self, store: dict, payloads: list[dict]) -> None:
        self._store = store
        self._payloads = payloads
        self._owner = None
        self._payload = None

    def select(self, *_a):
        return self

    def eq(self, key, value):
        self._owner = value
        return self

    def maybe_single(self):
        return self

    def update(self, payload=None):
        self._payload = payload
        return self

    def insert(self, payload):
        self._payload = payload
        return self

    def execute(self):
        class _Result:
            data = None

        if self._payload is not None:
            self._payloads.append(dict(self._payload))
            owner = self._owner
            if owner is None:
                owner = self._payload.get("owner_id")  # insert path: no eq() chain
            self._store[owner] = dict(self._payload)
        else:
            row = self._store.get(self._owner)
            _Result.data = dict(row) if row else None
        return _Result()


class _FakeDB:
    def __init__(self) -> None:
        self.store: dict = {}
        self.payloads: list[dict] = []

    def table(self, _name) -> _FakeTable:
        return _FakeTable(self.store, self.payloads)


@pytest.mark.asyncio
async def test_preference_defaults_to_false_and_roundtrips_through_ai_config():
    from backend.ai import config_store

    db = _FakeDB()
    with patch.object(config_store, "_get_db", lambda: db):
        assert (await config_store.get_config(1))["show_question"] is False
        assert await config_store.update_setting(1, "show_question", True)
        assert (await config_store.get_config(1))["show_question"] is True
        assert await config_store.update_setting(1, "show_question", False)
        assert (await config_store.get_config(1))["show_question"] is False


@pytest.mark.asyncio
async def test_preference_is_written_to_the_durable_upsert_payload():
    from backend.ai import config_store

    db = _FakeDB()
    with patch.object(config_store, "_get_db", lambda: db):
        assert await config_store.update_setting(2, "show_question", True)
    assert any(payload.get("show_question") is True for payload in db.payloads)


@pytest.mark.asyncio
async def test_preference_survives_replacing_the_in_memory_state():
    """The preference lives in ai_config, not in any RAM store: resetting the
    in-memory fallback (or the telemetry store) cannot lose it."""
    from backend.ai import config_store
    from backend.ai.engine.telemetry import telemetry

    db = _FakeDB()
    with patch.object(config_store, "_get_db", lambda: db):
        assert await config_store.update_setting(3, "show_question", True)
        monkey_fallback = {}
        with patch.object(config_store, "_fallback_config", monkey_fallback), \
             patch.object(telemetry, "_records", []), \
             patch.object(telemetry, "_show_telemetry", {}):
            assert (await config_store.get_config(3))["show_question"] is True


def test_renderer_does_not_depend_on_execution_telemetry_as_source_of_truth():
    from backend.ai.engine import telemetry as telemetry_module
    from backend.ai.tools import delivery

    # The delivery renderer takes the preference as a plain argument; even a
    # fully broken telemetry store cannot change the rendering.
    class _Broken:
        def __getattr__(self, name):
            raise RuntimeError("no RAM preference store")

    broken = _Broken()
    with patch.object(telemetry_module, "telemetry", broken):
        assert delivery.format_presentation(_FA_QUESTION, _FA_ANSWER, False) == "سلام!"
        assert _without_bidi_controls(delivery.format_thinking(_FA_QUESTION, True)) == "│ هی\n│\nThinking…"


def test_telemetry_store_has_no_show_question_preference_anymore():
    from backend.ai.engine.telemetry import ExecutionTelemetry

    store = ExecutionTelemetry()
    assert not hasattr(store, "get_show_question_pref")
    assert not hasattr(store, "set_show_question_pref")


def test_show_question_pref_reads_the_threaded_config_snapshot():
    from backend.bot.handlers import ai_unified as module

    token = module._PREFETCHED_CONFIG.set({"show_question": True})  # noqa: F841
    try:
        assert module._show_question_pref(1) is True
    finally:
        module._PREFETCHED_CONFIG.reset(token)
    token = module._PREFETCHED_CONFIG.set({"show_question": False})
    try:
        assert module._show_question_pref(1) is False
    finally:
        module._PREFETCHED_CONFIG.reset(token)
    # no snapshot / snapshot without the key → the durable default
    token = module._PREFETCHED_CONFIG.set(None)
    try:
        assert module._show_question_pref(1) is False
    finally:
        module._PREFETCHED_CONFIG.reset(token)


# ── K/L. end to end: edit-in-place + context separation ──────────────────────


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


async def _drive_execute_ai(owner_id: int, prompt: str, result: EngineResult,
                            stored_pref: bool):
    """Run the real ``_execute_ai`` path; capture the model-facing request and
    every text edited into the message (thinking, status, answer).

    The preference is threaded from the ``ai_config`` snapshot the activation
    handler loads, so this helper drives ``register()``'s trigger resolution
    first — exactly the production sequence.
    """
    from backend.bot.handlers import ai_unified as module

    captured: dict = {}

    class _Engine:
        provider_manager = _FakePM()
        conversation_manager = MagicMock()

        async def execute(self, request, status_callback=None):
            captured["user_message"] = request.user_message
            captured["message_id"] = request.message_id
            await status_callback("Reading context…")
            return result

    event = MagicMock()
    event.chat_id = 123
    event.message = MagicMock(id=456)
    event.edit = AsyncMock()
    event.reply = AsyncMock()
    with (
        patch.object(module, "_engine", _Engine()),
        patch("backend.ai.config_store.get_config",
              new=AsyncMock(return_value={"show_question": stored_pref})),
        patch("backend.ai.config_store.record_request", new=AsyncMock(return_value=None)),
        patch(
            "backend.runtime.task_guard.guarded_create_task",
            new=lambda coro, **kw: asyncio.ensure_future(coro),
        ),
    ):
        # The trigger cache is TTL-based and shared across calls; force a fresh
        # config read so the snapshot (and the threaded preference) is exercised.
        module._trigger_cache.update({"en": "", "fa": "", "ts": 0.0})
        # Production sequence: the activation handler loads the config snapshot
        # (trigger words) BEFORE _execute_ai renders anything.
        _, _, snapshot = await module._load_triggers(owner_id)
        module._PREFETCHED_CONFIG.set(snapshot)
        await module._execute_ai(event, owner_id, prompt, "Nova", "UTC")

    captured["edits"] = [call.args[0] for call in event.edit.await_args_list]
    captured["replies"] = [call.args[0] for call in event.reply.await_args_list]
    return captured


@pytest.mark.asyncio
@pytest.mark.parametrize("stored_pref", [False, True])
async def test_answer_edits_the_original_message_with_stored_preference(stored_pref):
    result = EngineResult(
        success=True, provider="dummy", model="dummy", latency=0.1,
        response="پاسخ من", metadata={},
    )
    captured = await _drive_execute_ai(9, "هی", result, stored_pref)
    # edit-in-place: the answer goes into the original message, never a new one
    assert _without_bidi_controls(captured["edits"][-1]).endswith("پاسخ من")
    assert captured["replies"] == []
    final = _without_bidi_controls(captured["edits"][-1])

    if stored_pref:
        assert final == "│ هی\n│\n ─┘ پاسخ من"
    else:
        # hidden question → PLAIN answer text, no connector of any kind
        assert final == "پاسخ من"
        for glyph in ("│", "─", "└", "┘"):
            assert glyph not in final
    # thinking/status edits never contain the answer elbow
    for edit in captured["edits"][:-1]:
        assert "└" not in edit and "┘" not in edit


@pytest.mark.asyncio
async def test_preference_changes_presentation_but_not_the_model_request():
    result = EngineResult(
        success=True, provider="dummy", model="dummy", latency=0.1,
        response="پاسخ من", metadata={},
    )
    on = await _drive_execute_ai(9, "هی", result, True)
    off = await _drive_execute_ai(9, "هی", result, False)

    # the model sees exactly the same request either way
    assert on["user_message"] == off["user_message"] == "هی"
    assert on["message_id"] == off["message_id"] == 456
    # only the rendered presentation differs
    assert _without_bidi_controls(on["edits"][-1]) == "│ هی\n│\n ─┘ پاسخ من"
    assert off["edits"][-1] == "پاسخ من"


@pytest.mark.asyncio
async def test_failure_does_not_render_as_a_successful_answer():
    result = EngineResult(
        success=False, provider="dummy", model="dummy", latency=0.1,
        response="", errors=["connection reset by peer"],
        metadata={"failure_type": "network", "retry_count": 0, "fallback_used": False},
    )
    captured = await _drive_execute_ai(9, "هی", result, True)
    final = _without_bidi_controls(captured["edits"][-1])

    assert "└" not in final and "┘" not in final
    assert "temporarily unavailable" in final or "✕" in final


# ── delivery / chunking keep the contract ────────────────────────────────────


@pytest.mark.asyncio
async def test_delivery_edits_in_place_and_uses_the_answer_state():
    edits, replies = [], []

    async def edit(text):
        edits.append(text)

    async def reply(text):
        replies.append(text)

    result = await deliver_response(
        SimpleNamespace(edit=edit, reply=reply), _FA_QUESTION, _RTL_ANSWER, True,
    )
    assert result.success
    assert len(edits) == 1
    assert replies == []
    assert _without_bidi_controls(edits[0]) == _without_bidi_controls(
        format_presentation(_FA_QUESTION, _RTL_ANSWER, True)
    )


@pytest.mark.asyncio
async def test_empty_response_uses_the_failure_state():
    edits, replies = [], []

    async def edit(text):
        edits.append(text)

    async def reply(text):
        replies.append(text)

    result = await deliver_response(
        SimpleNamespace(edit=edit, reply=reply), "msg", "   ", True,
    )
    assert result.success
    assert _without_bidi_controls(edits[0]) == "│ msg\n│\nError\nAI returned no response."
    assert replies == []
    assert "└" not in _without_bidi_controls(edits[0]) and "┘" not in _without_bidi_controls(edits[0])


def test_chunked_delivery_keeps_rules_and_utf16_safety():
    response = "\n".join(f"line-{index} " + "x" * 80 for index in range(200))
    for show in (True, False):
        chunks = _format_chunks("سؤال من", response, show)
        assert len(chunks) > 1
        assert all(_utf16_units(chunk) <= SAFE_LIMIT for chunk in chunks)
        for chunk in chunks:
            assert "🤖" not in chunk and "────────────" not in chunk
        if not show:
            # plain-mode chunks: no connector glyphs anywhere, each page is
            # the raw answer text plus only the continuation footer
            for chunk in chunks:
                for glyph in ("│", "─", "└", "┘"):
                    assert glyph not in chunk
            body = re.sub(r"\n\n_\(\d+/\d+\)_$", "", _without_bidi_controls(chunks[0]))
            assert body == _paginate_answer_head(response)
        else:
            body = re.sub(r"\n\n_\(\d+/\d+\)_$", "", _without_bidi_controls(chunks[0]))

            lines = body.split("\n")
            elbows = [l for l in lines if l.lstrip(" ").startswith(("└─", "─┘"))]
            # exactly one directional elbow per chunk: its first answer line
            assert len(elbows) == 1
            for line in lines:
                if line is elbows[0] or line.startswith("│"):
                    continue
                assert line.startswith("    ")
            assert _without_bidi_controls(chunks[0]).startswith("│ سؤال من\n│\n")
            assert sum(1 for line in _without_bidi_controls(chunks[0]).split("\n") if line == "│") == 1


def _paginate_answer_head(response: str) -> str:
    """The raw answer text of the first plain-mode page — the exact payload
    delivered when the question is hidden."""
    from backend.ai.tools.delivery import _MIN_SPLIT_CHUNK, _paginate

    footer_reserve = _utf16_units("\n\n_(9/99)_") + 2
    budget = max(_MIN_SPLIT_CHUNK, SAFE_LIMIT - footer_reserve)
    pages = _paginate(response, budget)
    return pages[0]


# ── Toggle: durable round-trip, owner consistency, honest failure ────────────


class _ToggleTable:
    """Supabase-shaped ai_config table for the toggle round-trip.

    Mirrors the real client shape: select→eq→maybe_single probes, then
    update/insert + eq. ``fail_write`` makes every write raise (schema-cache
    miss) while reads keep working — the exact DB state that used to make
    the toggle silently not toggle.
    """

    def __init__(self, store: dict, fail_write: bool, payloads: list) -> None:
        self._store = store
        self._fail_write = fail_write
        self._payloads = payloads
        self._owner = None
        self._payload = None

    def select(self, *_a):
        return self

    def eq(self, _key, value):
        self._owner = value
        return self

    def maybe_single(self):
        return self

    def update(self, payload=None):
        if self._fail_write:
            raise RuntimeError("PGRST204: Could not find the 'show_question' column")
        self._payload = payload
        return self

    def insert(self, payload):
        if self._fail_write:
            raise RuntimeError("PGRST204: Could not find the 'show_question' column")
        self._payload = payload
        return self

    def execute(self):
        class _Result:
            data = None

        if self._payload is not None:
            self._payloads.append(dict(self._payload))
            owner = self._owner if self._owner is not None else self._payload.get("owner_id")
            self._store[owner] = dict(self._payload)
            self._payload = None
        else:
            row = self._store.get(self._owner)
            _Result.data = dict(row) if row else None
        return _Result()


class _ToggleDB:
    def __init__(self, fail_write: bool = False) -> None:
        self.store: dict = {}
        self.payloads: list[dict] = []
        self._fail_write = fail_write

    def table(self, _name):
        return _ToggleTable(self.store, self._fail_write, self.payloads)


async def _toggle_roundtrip(pressed_times: int):
    """Press the real Settings toggle N times against a healthy DB and return
    the persisted values after each press."""
    from backend.ai import config_store
    from backend.bot.handlers import ai as ai_mod

    config_store._fallback_config.clear()
    db = _ToggleDB()
    owner = 4242
    persisted_values = []
    with patch.object(config_store, "_get_db", lambda: db), \
         patch.object(ai_mod, "_get_owner_id", new=AsyncMock(return_value=owner)):
        # pressed_times = 0 → the initial panel must read the persisted state
        for _ in range(pressed_times):
            result = await ai_mod._ai_toggle_show_question_action(None, "", owner)
            assert result is not None
            body = result[1]
            assert "Couldn't save" not in body
            persisted_values.append((await config_store.get_config(owner))["show_question"])
    return persisted_values, db


@pytest.mark.asyncio
async def test_toggle_roundtrip_flips_the_persisted_value_each_press():
    from backend.bot.handlers import ai as ai_mod

    values, _ = await _toggle_roundtrip(2)
    # initial persisted default False → first press must flip to True,
    # second press must flip back to False
    assert values == [True, False]


@pytest.mark.asyncio
async def test_toggle_renders_the_new_state_immediately():
    from backend.ai import config_store
    from backend.bot.handlers import ai as ai_mod

    config_store._fallback_config.clear()
    db = _ToggleDB()
    owner = 4243
    with patch.object(config_store, "_get_db", lambda: db), \
         patch.object(ai_mod, "_get_owner_id", new=AsyncMock(return_value=owner)):
        first = await ai_mod._ai_toggle_show_question_action(None, "", owner)
        second = await ai_mod._ai_toggle_show_question_action(None, "", owner)
    assert "My message in replies · On" in first[1]
    texts = [getattr(btn, "text", "") for row in first[2] for btn in row]
    assert "Turn my message in replies off" in texts
    assert "My message in replies · Off" in second[1]
    texts = [getattr(btn, "text", "") for row in second[2] for btn in row]
    assert "Turn my message in replies on" in texts


@pytest.mark.asyncio
async def test_fresh_read_and_fresh_store_restore_the_persisted_value():
    from backend.ai import config_store

    _, db = await _toggle_roundtrip(1)
    # a fresh read through a brand-new config-store state restores the value
    with patch.object(config_store, "_get_db", lambda: db):
        config_store._fallback_config.clear()
        assert (await config_store.get_config(4242))["show_question"] is True


@pytest.mark.asyncio
async def test_toggle_failure_is_honest_neither_silent_nor_false_success():
    """When the durable write fails, the toggle must NOT claim success: the
    persisted value stays unchanged and the panel says so."""
    from backend.ai import config_store
    from backend.bot.handlers import ai as ai_mod

    config_store._fallback_config.clear()
    db = _ToggleDB(fail_write=True)
    owner = 4244
    with patch.object(config_store, "_get_db", lambda: db), \
         patch.object(ai_mod, "_get_owner_id", new=AsyncMock(return_value=owner)):
        result = await ai_mod._ai_toggle_show_question_action(None, "", owner)
        assert result is not None
        assert "Couldn't save" in result[1]
        # the durable row never changed
        assert (await config_store.get_config(owner))["show_question"] is False
        # and save_config honestly reports the failure
        assert await config_store.update_setting(owner, "show_question", True) is False


@pytest.mark.asyncio
async def test_toggle_uses_one_owner_and_the_authoritative_config_row():
    """The whole toggle path — read, write, re-render — must touch the SAME
    owner's ai_config row and nothing else."""
    from backend.ai import config_store
    from backend.bot.handlers import ai as ai_mod

    config_store._fallback_config.clear()
    db = _ToggleDB()
    owner = 4245
    other_owner = 9999
    with patch.object(config_store, "_get_db", lambda: db):
        with patch.object(ai_mod, "_get_owner_id", new=AsyncMock(return_value=owner)):
            await ai_mod._ai_toggle_show_question_action(None, "", owner)
    # exactly one durable row was written, for the resolved owner
    assert set(db.store) == {owner}
    payloads = [p for p in db.payloads if "owner_id" in p]
    assert len(payloads) == 1 and payloads[0]["owner_id"] == owner
    assert payloads[0]["show_question"] is True
    # the other owner's row is untouched by the toggle
    with patch.object(config_store, "_get_db", lambda: db):
        assert (await config_store.get_config(other_owner))["show_question"] is False
