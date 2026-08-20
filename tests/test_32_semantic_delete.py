"""
Task 32 — Deterministic bounded semantic Delete.

Natural Persian/English Delete requests with clearly defined structural
predicates must be parsed deterministically and executed through the
existing bounded, self-only Delete pipeline:

    "پاک کن پیام‌های دو کلمه‌ای انگلیسی رو"  → exactly two ENGLISH words
    "delete my exact 3-word English messages" → exactly three English words
    "پیام‌های مربوط به فوتبال رو پاک کن"     → normalized topic matching
    "پاک کن ۱۰ پیام دو کلمه‌ای انگلیسی"      → predicate + count scope
    "پیام‌های دو کلمه‌ای انگلیسی امروز رو پاک کن" → predicate + time scope

Guarantees locked in here:

  - "دو کلمه‌ای" is a WORD COUNT, never a positional deletion count;
  - matching is deterministic normalization/tokenization — no embeddings,
    no external semantic service, no provider autonomy for these rules;
  - every deletion still passes ``delete_verified_self_messages()``;
  - history retrieval stays bounded and RPC-guarded;
  - successful pure Delete stays silent; failures stay visible;
  - ambiguous requests return a controlled clarification, never a guessed
    broad range.
"""
from __future__ import annotations

from datetime import datetime, timezone
from types import SimpleNamespace
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.ai.actions import (
    KIND_CLARIFY,
    KIND_EXECUTABLE,
    KIND_INVALID,
    parse_action_text,
    parse_command_intent,
)
from backend.ai.semantic_delete import (
    build_matcher,
    build_matcher_from_dict,
    count_words,
    english_word_count,
    normalize_text,
    parse_structural_predicate,
    spec_from_dict,
    tokenize,
    total_word_count,
)
from backend.ai.tools.context import ToolContext
from backend.ai.tools.delete import DeleteTool
from backend.services import delete_service


# ── Fake Telegram client (records bounds, removes deleted messages) ────────

class Message:
    def __init__(
        self,
        mid: int,
        *,
        out: bool,
        sender_id: int | None,
        text: str = "",
        date: datetime | None = None,
    ) -> None:
        self.id = mid
        self.out = out
        self.sender_id = sender_id
        self.message = text
        self.text = text
        self.date = date
        self.media = None


class Client:
    me = SimpleNamespace(id=111)

    def __init__(self, messages: list[Message]) -> None:
        self.messages = {message.id: message for message in messages}
        self.deleted: list[int] = []
        self.iter_kwargs: list[dict] = []

    async def iter_messages(self, chat_id, **kwargs):
        self.iter_kwargs.append(dict(kwargs))
        for message in sorted(self.messages.values(), key=lambda item: item.id, reverse=True):
            yield message

    async def get_messages(self, chat_id, ids):
        if isinstance(ids, (list, tuple)):
            return [self.messages.get(mid) for mid in ids]
        return self.messages.get(ids)

    async def delete_messages(self, chat_id, ids):
        batch = list(ids) if isinstance(ids, (list, tuple)) else [ids]
        self.deleted.extend(batch)
        for message_id in batch:
            self.messages.pop(message_id, None)


def context(client: Client, *, request_id: int | None = None) -> ToolContext:
    extra = {"chat_id": -100}
    if request_id is not None:
        extra["request_message_id"] = request_id
    return ToolContext(client=client, telegram=None, owner_id=111, tz_str="UTC", extra=extra)


def _utc(hour: int, day: int = 20) -> datetime:
    return datetime(2026, 8, day, hour, tzinfo=timezone.utc)


# ── Normalization / tokenization ────────────────────────────────────────────

def test_normalize_arabic_script_variants_to_persian():
    assert normalize_text("كتاب و يادگيري") == "کتاب و یادگیری"
    assert normalize_text("دقيقه") == "دقیقه"


def test_normalize_digits_and_zero_width():
    assert normalize_text("قیمت ۱۲۳۴") == "قیمت 1234"
    assert normalize_text("پیام\u200cهای") == "پیامهای"
    assert normalize_text("a\u200bb\u200dc") == "abc"


def test_tokenize_zero_width_is_separator():
    assert tokenize("پیام\u200cهای دو کلمه\u200cای") == ["پیام", "های", "دو", "کلمه", "ای"]
    assert tokenize("می\u200cخواهم") == ["می", "خواهم"]


def test_tokenize_punctuation_and_whitespace():
    assert tokenize("Hello, world!   foo") == ["hello", "world", "foo"]
    assert tokenize("سلام، دنیا!") == ["سلام", "دنیا"]
    assert tokenize("don't stop") == ["dont", "stop"]


def test_word_counts_mixed_persian_english():
    total, english, persian = count_words("hello world سلام دنیا")
    assert total == 4
    assert english == 2
    assert persian == 2
    # Pure digits and emoji are never words.
    assert english_word_count("I have 2 cats 🐈") == 3
    assert total_word_count("I have 2 cats 🐈") == 3


# ── Structural predicate parsing (unit) ─────────────────────────────────────

@pytest.mark.parametrize(
    "text,expected",
    [
        ("دو کلمه‌ای", {"word_count": 2}),
        ("دوکلمهای", {"word_count": 2}),
        ("سه کلمه ای", {"word_count": 3}),
        ("دو کلمه‌ای انگلیسی", {"english_word_count": 2}),
        ("انگلیسی دو کلمه‌ای", {"english_word_count": 2}),
        ("2-word English", {"english_word_count": 2}),
        ("two-word English", {"english_word_count": 2}),
        ("three words English", {"english_word_count": 3}),
        ("exact 3-word English messages", {"english_word_count": 3}),
        ("دو کلمه فارسی", {"word_count": 2}),
    ],
)
def test_parse_structural_predicate_forms(text, expected):
    spec = parse_structural_predicate(text)
    assert spec is not None, text
    assert spec.to_dict() == expected, text


def test_parse_structural_predicate_none_for_other_requests():
    for text in ("پیام‌های مربوط به فوتبال", "delete the last 5 messages",
                 "سلام", "پیام‌های آخر"):
        assert parse_structural_predicate(text) is None, text


def test_parse_structural_predicate_ambiguous_large_count_is_none():
    # 150 words is not a clearly defined predicate → caller must clarify.
    assert parse_structural_predicate("پیام‌های صد و پنجاه کلمه‌ای") is None
    # In-range compounds still parse (بیست و پنج = 25).
    assert parse_structural_predicate("بیست و پنج کلمه‌ای").word_count == 25


def test_spec_from_dict_validation():
    assert spec_from_dict({"english_word_count": 2}).to_dict() == {"english_word_count": 2}
    assert spec_from_dict({"query": "فوتبال", "word_count": 2}).to_dict() == {
        "query": "فوتبال", "word_count": 2,
    }
    assert spec_from_dict({"query": "x"}).to_dict() == {"query": "x"}  # topic-only is valid
    assert spec_from_dict({}) is None  # empty predicate is invalid
    assert spec_from_dict({"evil": 1}) is None
    assert spec_from_dict({"word_count": 0}) is None
    assert spec_from_dict({"word_count": 500}) is None
    assert spec_from_dict("nope") is None
    assert spec_from_dict({"word_count": "۱۰"}) is not None  # Persian digit coercion


# ── Matcher semantics ───────────────────────────────────────────────────────

def test_matcher_exact_english_word_count_with_punctuation_and_whitespace():
    matcher = build_matcher_from_dict({"english_word_count": 2})
    assert matcher is not None
    assert matcher("hello world") is True
    assert matcher("Hello, world!") is True
    assert matcher("hello   world\n") is True
    assert matcher("hello") is False
    assert matcher("hello world foo") is False
    assert matcher("hello سلام") is False  # only one English word
    assert matcher("سلام دنیا") is False  # zero English words


def test_matcher_mixed_persian_english_exactly_two_english_words():
    matcher = build_matcher_from_dict({"english_word_count": 2})
    assert matcher("سلام hello world") is True
    assert matcher("hello world سلام دنیا") is True
    assert matcher("hello one دو three") is False


def test_matcher_total_word_count():
    matcher = build_matcher_from_dict({"word_count": 2})
    assert matcher("hello world") is True
    assert matcher("سلام دنیا") is True
    assert matcher("hello") is False
    assert matcher("hello world foo") is False


def test_matcher_persian_char_normalization_makes_topics_match():
    matcher = build_matcher_from_dict({"query": "کتاب"})
    assert matcher("این کتاب خوبیه") is True
    assert matcher("این كتاب خوبیه") is True  # Arabic kaf variant
    assert matcher("این فیلم خوبیه") is False
    assert matcher("کتابی درباره فوتبال") is True  # substring within a word


def test_matcher_query_and_word_count_are_anded():
    matcher = build_matcher_from_dict({"query": "فوتبال", "english_word_count": 2})
    assert matcher("فوتبال hello world") is True
    assert matcher("فوتبال hello world foo") is False
    assert matcher("تنیس hello world") is False


# ── Deterministic intent parsing (integration with parse_command_intent) ───

def test_persian_two_word_english_predicate_is_not_deletion_count():
    """The core regression: 'دو کلمه‌ای' must be a word count, never count=2."""
    r = parse_command_intent("پاک کن پیام‌های دو کلمه‌ای انگلیسی رو", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.action == "delete_messages"
    assert r.count is None
    assert r.mode == "filtered"
    assert r.tool_calls == [{
        "name": "delete",
        "arguments": {"mode": "filtered", "semantic": {"english_word_count": 2}},
    }]


def test_persian_exact_two_english_words_alt_wording():
    r = parse_command_intent(
        "پیام‌هایی که دقیقاً دو کلمه انگلیسی دارن رو پاک کن", has_reply=False
    )
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{
        "name": "delete",
        "arguments": {"mode": "filtered", "semantic": {"english_word_count": 2}},
    }]


def test_persian_two_word_any_language():
    r = parse_command_intent("پاک کن پیام‌های دو کلمه‌ای رو", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{
        "name": "delete",
        "arguments": {"mode": "filtered", "semantic": {"word_count": 2}},
    }]


def test_english_exact_n_word_predicate():
    r = parse_command_intent("delete my exact 3-word English messages", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.count is None
    assert r.tool_calls == [{
        "name": "delete",
        "arguments": {"mode": "filtered", "semantic": {"english_word_count": 3}},
    }]


def test_count_plus_structural_predicate():
    r = parse_command_intent("پاک کن ۱۰ پیام دو کلمه‌ای انگلیسی", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.count == 10
    assert r.tool_calls == [{
        "name": "delete",
        "arguments": {
            "mode": "filtered",
            "semantic": {"english_word_count": 2},
            "count": 10,
        },
    }]


def test_time_plus_structural_predicate():
    r = parse_command_intent("پیام‌های دو کلمه‌ای انگلیسی امروز رو پاک کن", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.mode == "filtered"
    assert r.after_time == "today"
    assert r.tool_calls == [{
        "name": "delete",
        "arguments": {
            "mode": "filtered",
            "semantic": {"english_word_count": 2},
            "after_time": "today",
        },
    }]


def test_boundary_plus_structural_predicate():
    r = parse_command_intent(
        "تا این پیام پیام‌های دو کلمه‌ای انگلیسی رو پاک کن", has_reply=False
    )
    assert r.kind == KIND_EXECUTABLE
    assert r.mode == "until_message"
    assert r.tool_calls == [{
        "name": "delete",
        "arguments": {"mode": "until_message", "semantic": {"english_word_count": 2}},
    }]


def test_topic_plus_structural_predicate():
    r = parse_command_intent(
        "پیام‌های دو کلمه‌ای انگلیسی مربوط به فوتبال رو پاک کن", has_reply=False
    )
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{
        "name": "delete",
        "arguments": {
            "mode": "filtered",
            "semantic": {"query": "فوتبال", "english_word_count": 2},
        },
    }]


def test_topic_only_request_keeps_plain_query_path():
    r = parse_command_intent("پیام‌های مربوط به فوتبال رو پاک کن", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.mode == "filtered"
    assert r.query == "فوتبال"
    assert r.tool_calls == [{
        "name": "delete",
        "arguments": {"mode": "filtered", "query": "فوتبال"},
    }]


def test_ambiguous_semantic_request_never_guesses_a_range():
    for text in ("پاک کن", "پیام‌ها رو پاک کن", "delete messages"):
        r = parse_command_intent(text, has_reply=False)
        assert r.kind != KIND_EXECUTABLE, text
        assert r.tool_calls == [], text


def test_structural_delete_accepts_remove_wording():
    """'حذف کن' is the same delete imperative as 'پاک کن' for structural rules."""
    r = parse_command_intent("پیام‌های دو کلمه‌ای انگلیسی رو حذف کن", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{
        "name": "delete",
        "arguments": {"mode": "filtered", "semantic": {"english_word_count": 2}},
    }]


def test_positional_count_after_word_marker_still_parses():
    """'۵ پیام آخر' is a positional count even when a word marker follows later."""
    r = parse_command_intent("پاک کن ۵ پیام دو کلمه‌ای انگلیسی", has_reply=False)
    assert r.kind == KIND_EXECUTABLE
    assert r.count == 5
    assert r.tool_calls[0]["arguments"]["count"] == 5


# ── Structured JSON action path (provider fallback) ─────────────────────────

def test_structured_json_semantic_delete_resolves():
    r = parse_action_text(
        '{"action": "delete_messages", "mode": "filtered", '
        '"semantic": {"english_word_count": 2}}'
    )
    assert r.kind == KIND_EXECUTABLE
    assert r.tool_calls == [{
        "name": "delete",
        "arguments": {"mode": "filtered", "semantic": {"english_word_count": 2}},
    }]


def test_structured_json_invalid_semantic_rejected():
    for bad in ('{"action": "delete_messages", "semantic": {"evil": 1}}',
                '{"action": "delete_messages", "semantic": {"word_count": 0}}',
                '{"action": "delete_messages", "semantic": "two"}',
                '{"action": "delete_messages", "semantic": {}}'):
        r = parse_action_text(bad)
        assert r.kind == KIND_INVALID, bad


def test_structured_json_semantic_only_for_delete():
    r = parse_action_text('{"action": "save", "semantic": {"word_count": 2}}')
    assert r.kind == KIND_INVALID


# ── Execution through DeleteTool + the ownership chokepoint ─────────────────

@pytest.mark.asyncio
async def test_semantic_delete_deletes_only_matching_self_messages():
    client = Client([
        Message(10, out=True, sender_id=111, text="درخواست پاک کردن"),  # request: 0 EN words
        Message(9, out=True, sender_id=111, text="hello world"),        # 2 EN words
        Message(8, out=False, sender_id=222, text="hello world"),       # foreign match
        Message(7, out=True, sender_id=111, text="hello world foo"),    # 3 EN words
        Message(6, out=True, sender_id=111, text="سلام دنیا"),          # 0 EN words
        Message(5, out=True, sender_id=111, text="hi there"),           # 2 EN words
    ])
    result = await DeleteTool(context(client, request_id=10)).execute(
        context(client, request_id=10),
        {"mode": "filtered", "semantic": {"english_word_count": 2}},
    )
    assert result.success is True
    assert result.data["count"] == 2
    assert client.deleted == [9, 5]
    # Foreign matching messages never reach the delete API.
    assert 8 not in client.deleted


@pytest.mark.asyncio
async def test_semantic_delete_current_request_in_scope_stays_eligible():
    client = Client([
        Message(20, out=True, sender_id=111, text="hello world"),  # request itself
        Message(19, out=True, sender_id=111, text="hello world foo"),
        Message(18, out=True, sender_id=111, text="hi there"),
    ])
    result = await DeleteTool(context(client, request_id=20)).execute(
        context(client, request_id=20),
        {"mode": "filtered", "semantic": {"english_word_count": 2}},
    )
    assert result.success is True
    assert client.deleted == [20, 18]


@pytest.mark.asyncio
async def test_semantic_delete_ai_generated_self_message_included():
    """AI-generated messages are ordinary self-owned outgoing messages and
    stay eligible whenever their content matches the predicate."""
    client = Client([
        Message(30, out=True, sender_id=111, text="hello world"),
        Message(29, out=True, sender_id=111, text="hi there"),  # AI-generated self msg
    ])
    matcher = build_matcher_from_dict({"english_word_count": 2})
    considered, deleted, error = await delete_service.do_del_self_filtered(
        client, -100, match=matcher, request_id="req-ai",
    )
    assert error is None
    assert deleted == 2
    assert client.deleted == [30, 29]


@pytest.mark.asyncio
async def test_semantic_delete_count_cap_applied():
    messages = [Message(1000 - i, out=True, sender_id=111, text="hello world") for i in range(12)]
    client = Client(messages)
    result = await DeleteTool(context(client)).execute(
        context(client),
        {"mode": "filtered", "semantic": {"english_word_count": 2}, "count": 10},
    )
    assert result.success is True
    assert result.data["count"] == 10
    assert len(client.deleted) == 10


@pytest.mark.asyncio
async def test_semantic_delete_time_scope_applied():
    client = Client([
        Message(40, out=True, sender_id=111, text="hello world", date=_utc(19)),
        Message(39, out=True, sender_id=111, text="hello world", date=_utc(16)),
        Message(38, out=True, sender_id=111, text="hello world", date=_utc(12)),
        Message(37, out=True, sender_id=111, text="hello world", date=_utc(18)),
    ])
    matcher = build_matcher_from_dict({"english_word_count": 2})
    considered, deleted, error = await delete_service.do_del_self_filtered(
        client, -100, until_time="2026-08-20T17:00:00+00:00", match=matcher,
        request_id="req-time",
    )
    assert error is None
    assert deleted == 2
    assert client.deleted == [39, 38]


@pytest.mark.asyncio
async def test_semantic_delete_boundary_scope_applied():
    """The boundary itself stays eligible when it is self-owned and matches;
    messages newer than the boundary are outside the requested range."""
    client = Client([
        Message(51, out=True, sender_id=111, text="hello world"),   # newer: excluded
        Message(50, out=True, sender_id=111, text="hello world"),   # boundary (request)
        Message(49, out=True, sender_id=111, text="hello world"),
        Message(48, out=False, sender_id=222, text="hello world"),  # foreign: excluded
        Message(47, out=True, sender_id=111, text="hello world"),
    ])
    matcher = build_matcher_from_dict({"english_word_count": 2})
    considered, deleted, error = await delete_service.do_del_self_filtered(
        client, -100, boundary_id=50, match=matcher, request_id="req-boundary",
    )
    assert error is None
    assert deleted == 3
    assert client.deleted == [50, 49, 47]


@pytest.mark.asyncio
async def test_semantic_delete_unknown_identity_fails_closed():
    client = Client([Message(60, out=True, sender_id=111, text="hello world")])
    client.me = None
    matcher = build_matcher_from_dict({"english_word_count": 2})
    considered, deleted, error = await delete_service.do_del_self_filtered(
        client, -100, match=matcher, request_id="req-no-me",
    )
    assert deleted == 0
    assert error is not None
    assert client.deleted == []


@pytest.mark.asyncio
async def test_semantic_delete_missing_sender_fails_closed():
    client = Client([Message(61, out=True, sender_id=None, text="hello world")])
    deleted, rejected = await delete_service.delete_verified_self_messages(client, -100, [61])
    assert deleted == []
    assert rejected == [61]


@pytest.mark.asyncio
async def test_provider_supplied_foreign_id_rejected_by_final_verification():
    from backend.ai.tools.semantic import DeleteMessagesByIdsTool

    client = Client([
        Message(70, out=True, sender_id=111, text="hello world"),
        Message(71, out=False, sender_id=222, text="hello world"),
    ])
    result = await DeleteMessagesByIdsTool(context(client)).execute(
        context(client), {"message_ids": [70, 71, 999]}
    )
    assert result.success is True
    assert result.data["deleted"] == [70]
    assert 71 in result.data["rejected"]
    assert 999 in result.data["rejected"]
    assert client.deleted == [70]


# ── Boundedness / controlled failure / silence ──────────────────────────────

@pytest.mark.asyncio
async def test_semantic_delete_history_iteration_is_bounded():
    client = Client([Message(80, out=True, sender_id=111, text="hello world")])
    result = await DeleteTool(context(client)).execute(
        context(client), {"mode": "filtered", "semantic": {"english_word_count": 2}}
    )
    assert result.success is True
    # The selector never asks for an unbounded history.
    assert all(kwargs.get("limit") == 1000 for kwargs in client.iter_kwargs)


@pytest.mark.asyncio
async def test_semantic_delete_hanging_history_returns_controlled_timeout(monkeypatch):
    monkeypatch.setattr(delete_service, "_DELETE_RPC_TIMEOUT_SECONDS", 0.01)

    class SlowClient:
        me = SimpleNamespace(id=111)
        deleted: list[int] = []

        async def iter_messages(self, chat_id, **kwargs):
            await __import__("asyncio").sleep(0.1)
            yield Message(1, out=True, sender_id=111, text="hello world")

        async def get_messages(self, chat_id, ids):
            return [Message(mid, out=True, sender_id=111, text="hello world") for mid in ids]

        async def delete_messages(self, chat_id, ids):
            self.deleted.extend(ids)

    client = SlowClient()
    result = await DeleteTool(context(client, request_id=1)).execute(
        context(client, request_id=1),
        {"mode": "filtered", "semantic": {"english_word_count": 2}},
    )
    assert result.success is False
    assert "timed out" in result.message.lower()
    assert client.deleted == []


@pytest.mark.asyncio
async def test_invalid_semantic_filter_is_controlled_failure():
    client = Client([Message(90, out=True, sender_id=111, text="hello world")])
    result = await DeleteTool(context(client)).execute(
        context(client), {"mode": "filtered", "semantic": {"word_count": "abc"}}
    )
    assert result.success is False
    assert "Invalid semantic filter" in result.message
    assert client.deleted == []


@pytest.mark.asyncio
async def test_semantic_delete_success_is_silent_at_handler_contract():
    from backend.bot.handlers.ai_unified import _is_silent_delete

    result = SimpleNamespace(metadata={
        "tool_results": [
            {"tool_name": "delete", "success": True,
             "message": "Deleted 2 outgoing message(s).", "data": {"count": 2}},
        ],
    })
    assert _is_silent_delete(result) is True

    failed = SimpleNamespace(metadata={
        "tool_results": [
            {"tool_name": "delete", "success": False,
             "message": "Delete timed out while reading Telegram history.", "data": {}},
        ],
    })
    assert _is_silent_delete(failed) is False


@pytest.mark.asyncio
async def test_repeated_semantic_delete_does_not_duplicate_deletion():
    client = Client([
        Message(100, out=True, sender_id=111, text="hello world"),
        Message(99, out=True, sender_id=111, text="hello world"),
        Message(98, out=True, sender_id=111, text="hello world foo"),
    ])
    ctx = context(client)
    first = await DeleteTool(ctx).execute(
        ctx, {"mode": "filtered", "semantic": {"english_word_count": 2}}
    )
    second = await DeleteTool(ctx).execute(
        ctx, {"mode": "filtered", "semantic": {"english_word_count": 2}}
    )
    assert first.success is True
    assert second.success is True
    assert first.data["count"] == 2
    # The second run sees the messages already removed by the first run.
    assert second.data["count"] == 0
    assert sorted(client.deleted) == [99, 100]
    assert len(client.deleted) == len(set(client.deleted))


# ── Fast path: deterministic structural delete needs no provider ────────────

class _FakeProvider:
    def __init__(self, name: str = "test"):
        self._name = name
        self.calls = 0
        self.config = type("Cfg", (), {"default_model": "m", "model": "m"})()

    @property
    def name(self) -> str:
        return self._name

    async def chat(self, messages, **kwargs):
        self.calls += 1
        from backend.ai.providers.base.contract import ProviderResponse
        return ProviderResponse(text="ok", provider_name=self._name, success=True)

    def health(self):
        return {"healthy": True}


def _make_dispatcher(mock_te, provider):
    from backend.ai.engine.dispatcher import Dispatcher
    from backend.ai.engine.hooks import NOOP_HOOKS
    from backend.ai.engine.metrics import EngineMetrics
    from backend.ai.providers.manager.manager import ProviderManager
    from backend.ai.session.request import AIRequest

    pm = ProviderManager()
    pm.register_provider(provider)
    pm.switch_provider(provider.name)
    pm._fallback_chain = []

    mock_conv = MagicMock()
    mock_sess = MagicMock()
    mock_sess.session_id = "s"
    mock_sess.owner_id = 123
    mock_sess.active_provider = provider.name
    mock_conv.get_session.return_value = mock_sess
    mock_conv.restore_history = AsyncMock()
    mock_conv.get_history.return_value = []

    mock_pb = MagicMock()
    pp = MagicMock()
    pp.system_prompt = "sys"
    pp.runtime_context = ""
    pp.conversation_context = ""
    pp.tool_context = ""
    pp.user_input = "do it"
    pp.estimated_tokens.estimated_input_tokens = 50
    pp.estimated_tokens.prompt_size_chars = 100
    mock_pb.build.return_value = pp

    return Dispatcher(mock_conv, mock_pb, pm, NOOP_HOOKS, EngineMetrics(), tool_executor=mock_te)


@pytest.mark.asyncio
async def test_semantic_delete_runs_fast_path_without_provider():
    from backend.ai.session.request import AIRequest
    from backend.ai.tools.executor import ToolExecutionResult

    mock_te = MagicMock()
    mock_te.execute_calls = AsyncMock(return_value=[
        ToolExecutionResult(tool_name="delete", success=True,
                            message="Deleted 2 outgoing message(s).", data={"count": 2}),
    ])
    c = MagicMock()
    c.extra = {}
    c.telegram = None
    c.tz_str = "UTC"
    c.client = None
    mock_te._context = c

    provider = _FakeProvider()
    d = _make_dispatcher(mock_te, provider)

    result = await d.dispatch(AIRequest(
        session_id="s1", message_id=1, owner_id=123,
        user_message="پاک کن پیام‌های دو کلمه‌ای انگلیسی رو", chat_id=456,
    ))

    assert result.success is True
    assert result.metadata["finish_state"] == "local_fast_path"
    tool_calls = mock_te.execute_calls.call_args.args[0]
    assert tool_calls == [{
        "name": "delete",
        "arguments": {"mode": "filtered", "semantic": {"english_word_count": 2}},
    }]
    # Provider-independent: deterministic structural predicates never depend
    # on a provider being available.
    assert provider.calls == 0
