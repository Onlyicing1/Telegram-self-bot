"""History AI-pass capacity — the execution model behind the 1,000-message refusal.

Contract this file pins:

  1. Token estimation is script-aware: Latin-only text uses the documented
     4-chars/token ratio; any non-Latin character keeps the 2-chars/token worst
     case for the whole string (mixed text is never weighted).
  2. The summarization chunk budget stays at the application's verified context
     policy. No model-context registry is invented: the fallback chain cannot be
     enumerated with a verified minimum context, so a larger chunk is NOT safe.
  3. A pass count is the number of provider calls the operation makes: one per
     map chunk plus one reduce call when there is more than one chunk.
  4. Capacity is the smaller of the pacing bound and the concurrency bound:
     ``MAP_CONCURRENCY`` calls per planned call latency, because scheduled start
     times alone do not mean completions by the deadline.
  5. A workload that cannot finish is refused BEFORE any provider call, and the
     refusal names the time budget, the call count and how much work fits.

Deterministic: a scripted provider inside the real ``ProviderManager``; no
network, no Telegram, no wall-clock sleeping.
"""
from __future__ import annotations

import asyncio
import re
from datetime import datetime, timezone
from typing import Any

import pytest
from backend.helper import symbols as sym

from backend.ai.prompt.budget import (
    DEFAULT_MAX_CONTEXT_TOKENS,
    estimate_tokens_script_aware,
)
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.providers.registry.registry import ProviderRegistry
from backend.ai.tools.context import ToolContext
from backend.ai.tools.history_ai import SummarizeHistoryTool, TranslateHistoryTool
from backend.services import history_ai_service
from backend.telegram_api.api import TelegramAPI

CHAT = -100555000
OWNER = 7770001
NOW = datetime(2026, 9, 14, 14, 2, tzinfo=timezone.utc)

PERSIAN = ("سلام دنیا، این یک پیام آزمایشی برای سنجش ظرفیت خلاصه‌سازی است. " * 20)[:700]
ARABIC = ("مرحبا بالعالم، هذه رسالة اختبار لقياس سعة التلخيص. " * 20)[:700]


@pytest.fixture(autouse=True)
def _fast_pacing(monkeypatch):
    """Pacing has its own tests; here the call schedule must not slow the suite."""
    monkeypatch.setattr(history_ai_service, "call_spacing", lambda calls, budget: 0.0)


# ── Fakes ─────────────────────────────────────────────────────────────────


class _FakeMessage:
    def __init__(self, mid: int, text: str = "") -> None:
        self.id = mid
        self.chat_id = CHAT
        self.sender_id = OWNER
        self.text = text
        self.date = NOW
        self.media = None
        self.reply_to = None
        self.out = True


class _FakeClient:
    def __init__(self, messages: list[_FakeMessage]) -> None:
        self._messages = list(messages)

    def iter_messages(self, chat_id: Any, **kwargs: Any):
        limit = kwargs.get("limit")
        min_id = kwargs.get("min_id")
        max_id = kwargs.get("max_id")
        selected = [
            m for m in self._messages
            if (max_id is None or m.id < max_id) and (min_id is None or m.id > min_id)
        ]
        selected.sort(key=lambda m: m.id, reverse=True)
        if limit is not None:
            selected = selected[:limit]

        async def _aiter():
            for item in selected:
                yield item

        return _aiter()


def _conversation(count: int, *, text: str = "message") -> list[_FakeMessage]:
    return [_FakeMessage(mid, f"{text} {mid}") for mid in range(1, count + 1)]


def _summarizing_provider(messages: list[dict[str, Any]], payload: str) -> str:
    if "merge partial summaries" in messages[0]["content"]:
        return "FINAL"
    return f"SUMMARY({len(payload.splitlines())})"


class _ScriptedProvider(BaseProvider):
    def __init__(self, responder: Any = _summarizing_provider, *, fail: bool = False) -> None:
        super().__init__(
            ProviderConfig(provider_name="scripted", enabled=True, default_model="m1")
        )
        self.calls = 0
        self.payloads: list[str] = []
        self.prompts: list[list[dict[str, Any]]] = []
        self._responder = responder
        self._fail = fail

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True)

    async def chat(self, messages, **kwargs) -> ProviderResponse:
        self.calls += 1
        self.prompts.append([dict(m) for m in messages])
        if self._fail:
            return ProviderResponse(
                text="", provider_name="scripted", success=False,
                metadata={"failure_type": "server"},
            )
        payload = messages[-1]["content"] if messages else ""
        self.payloads.append(payload)
        return ProviderResponse(
            text=self._responder(messages, payload),
            provider_name="scripted", success=True,
        )

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True, "ready": True}


def _manager(provider: _ScriptedProvider) -> ProviderManager:
    registry = ProviderRegistry()
    registry.register(provider)
    return ProviderManager(registry)


def _context(manager: ProviderManager, client: Any, *, timeout_s: Any = None) -> ToolContext:
    extra: dict[str, Any] = {
        "chat_id": CHAT, "provider_manager": manager, "request_id": "req-capacity",
    }
    if timeout_s is not None:
        extra["request_timeout_s"] = timeout_s
    return ToolContext(
        telegram=TelegramAPI(client), owner_id=OWNER, tz_str="UTC",
        client=client, extra=extra,
    )


async def _summarize(provider: _ScriptedProvider, client: Any, arguments: dict[str, Any], *, timeout_s: Any = None):
    context = _context(_manager(provider), client, timeout_s=timeout_s)
    return await SummarizeHistoryTool(context).execute(context, arguments)


# ── 1. Script-aware estimation ────────────────────────────────────────────


def test_english_only_text_uses_the_documented_four_char_ratio():
    text = "a" * 4000
    assert estimate_tokens_script_aware(text) == 1000


def test_persian_text_keeps_the_conservative_two_char_ratio():
    assert len(PERSIAN) == 700
    assert estimate_tokens_script_aware(PERSIAN) == 350


def test_arabic_text_keeps_the_conservative_two_char_ratio():
    assert len(ARABIC) == 700
    assert estimate_tokens_script_aware(ARABIC) == 350


def test_mixed_script_text_uses_the_conservative_ratio_for_the_whole_string():
    mixed = "hello " + "سلام"
    assert not mixed.isascii()
    # Not weighted: one non-Latin character keeps the 2-chars/token worst case,
    # so the mixed string can never be underestimated by combining ratios.
    assert estimate_tokens_script_aware(mixed) == (len(mixed) + 1) // 2


def test_empty_text_costs_no_tokens_and_accented_latin_stays_conservative():
    assert estimate_tokens_script_aware("") == 0
    assert estimate_tokens_script_aware("café") == 2  # non-ASCII -> 2 chars/token


def test_latin_history_is_chunked_with_fewer_chunks_than_non_latin():
    latin = _conversation(200, text="x" * 700)
    non_latin = _conversation(200, text=PERSIAN[:700])

    # The public chunker is exercised through the service's own rendering path.
    latin_count = len(history_ai_service._chunk_history(
        [_as_message(m) for m in latin],
        with_attribution=False, token_budget=history_ai_service.SUMMARIZE_CHUNK_TOKEN_BUDGET,
        with_ids=False,
    ))
    non_latin_count = len(history_ai_service._chunk_history(
        [_as_message(m) for m in non_latin],
        with_attribution=False, token_budget=history_ai_service.SUMMARIZE_CHUNK_TOKEN_BUDGET,
        with_ids=False,
    ))
    assert latin_count == non_latin_count // 2 or abs(latin_count * 2 - non_latin_count) <= 1


def _as_message(raw: _FakeMessage):
    return history_ai_service.HistoryMessage(
        message_id=raw.id, chat_id=CHAT, sender_id=raw.sender_id, out=raw.out,
        date=raw.date, text=raw.text, reply_to_msg_id=None, has_media=False,
        ai_provenance=False,
    )


# ── 2. The chunk budget is not raised without verified context metadata ──


def test_the_chunk_budget_stays_at_the_application_context_policy():
    """No speculative model-context registry: the safe budget is unchanged.

    ``model_discovery.get_model_context_length`` exists, but it is per model and
    returns 0 for anything not cached; ``ProviderManager``'s candidate chain is
    not enumerable with a verified minimum, so a fallback could land on a model
    smaller than a raised chunk. The budget therefore stays at the policy value.
    """
    assert history_ai_service.SUMMARIZE_CHUNK_TOKEN_BUDGET == DEFAULT_MAX_CONTEXT_TOKENS == 4000
    source = __import__("inspect").getsource(history_ai_service)
    assert "get_model_context_length" not in source


def test_a_single_oversized_message_is_its_own_chunk_and_never_split(monkeypatch):
    monkeypatch.setattr(history_ai_service, "SUMMARIZE_CHUNK_TOKEN_BUDGET", 100)
    huge = "y" * 20000
    messages = [_as_message(_FakeMessage(1, huge)), _as_message(_FakeMessage(2, "small"))]
    chunks = history_ai_service._chunk_history(
        messages, with_attribution=False, token_budget=100, with_ids=False,
    )

    assert len(chunks) == 2
    assert chunks[0].lines == (huge,)  # preserved whole, not truncated


# ── 3. Map / reduce accounting ────────────────────────────────────────────


def test_required_calls_count_one_map_per_chunk_and_one_merge():
    assert history_ai_service.required_ai_calls(0) == 0
    assert history_ai_service.required_ai_calls(1) == 1  # no fake reduce
    assert history_ai_service.required_ai_calls(2) == 3
    assert history_ai_service.required_ai_calls(45) == 46


def test_the_reduce_call_is_reserved_before_the_map_phase_begins():
    budget = history_ai_service.llm_budget(300.0)
    reserved_map_budget = budget - history_ai_service.ASSUMED_CALL_LATENCY_S

    assert budget == 280.0
    assert reserved_map_budget == 250.0
    # The map phase deadline is strictly earlier than the operation deadline.
    assert reserved_map_budget < budget


# ── 4. Concurrency-aware capacity ─────────────────────────────────────────


def test_capacity_is_bounded_by_concurrency_not_only_by_start_spacing():
    budget = history_ai_service.llm_budget(300.0) - history_ai_service.ASSUMED_CALL_LATENCY_S
    paced = history_ai_service.max_map_calls(budget)
    capacity = history_ai_service.map_call_capacity(budget)

    assert paced == 45
    assert capacity == 16  # 2 * floor(250 / 30) -- the review's 16.7
    assert capacity == history_ai_service.MAP_CONCURRENCY * int(
        budget // history_ai_service.ASSUMED_CALL_LATENCY_S
    )


def test_capacity_never_exceeds_pacing_and_degrades_to_one_call():
    for budget in (0.0, 5.0, 30.0, 100.0, 250.0, 280.0, 1000.0):
        assert history_ai_service.map_call_capacity(budget) <= history_ai_service.max_map_calls(budget)
    assert history_ai_service.map_call_capacity(0.0) == 1
    assert history_ai_service.map_call_capacity(10.0) == 1


# ── 5. Refusal before any provider call, with honest wording ──────────────


@pytest.mark.asyncio
async def test_the_1000_message_class_is_refused_before_any_provider_call():
    """The observed 1,000-message history: ~91 chunks, 16 calls of capacity."""
    provider = _ScriptedProvider()
    result = await _summarize(provider, _FakeClient(_conversation(1000, text=PERSIAN)), {"count": 1000})

    assert result.success is False
    assert result.data["error"] == history_ai_service.ERROR_TOO_LARGE
    assert provider.calls == 0  # refused BEFORE any provider work
    assert "AI calls" in result.message
    assert "to cover the parts" in result.message
    assert "to merge them" in result.message
    assert "AI budget" in result.message
    assert "About" in result.message
    assert "rate limits" not in result.message


@pytest.mark.asyncio
async def test_the_reported_affordable_count_is_what_the_gate_would_accept():
    provider = _ScriptedProvider()
    result = await _summarize(provider, _FakeClient(_conversation(1000, text=PERSIAN)), {"count": 1000})

    match = re.search(r"About (\d+) messages fit", result.message)
    assert match, result.message
    affordable = int(match.group(1))
    assert 0 < affordable < 1000
    # Re-running exactly that many messages is accepted (and does real work).
    provider_two = _ScriptedProvider()
    accepted = await _summarize(
        provider_two, _FakeClient(_conversation(affordable, text=PERSIAN)), {"count": affordable},
    )
    assert accepted.success is True
    assert provider_two.calls == history_ai_service.required_ai_calls(
        len(history_ai_service._chunk_history(
            [_as_message(m) for m in _conversation(affordable, text=PERSIAN)],
            with_attribution=False,
            token_budget=history_ai_service.SUMMARIZE_CHUNK_TOKEN_BUDGET,
            with_ids=False,
        ))
    )


@pytest.mark.asyncio
async def test_an_accepted_workload_makes_exactly_its_counted_calls():
    monkeypatch_budget = 5
    provider = _ScriptedProvider()
    context = _context(_manager(provider), _FakeClient(_conversation(6)), timeout_s=300.0)
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(history_ai_service, "SUMMARIZE_CHUNK_TOKEN_BUDGET", monkeypatch_budget)
        result = await SummarizeHistoryTool(context).execute(context, {"count": 6})

    assert result.success is True
    assert result.message == "FINAL"
    chunks = len(history_ai_service._chunk_history(
        [_as_message(m) for m in _conversation(6)],
        with_attribution=False, token_budget=monkeypatch_budget, with_ids=False,
    ))
    assert provider.calls == history_ai_service.required_ai_calls(chunks)
    assert chunks <= history_ai_service.map_call_capacity(
        history_ai_service.llm_budget(300.0) - history_ai_service.ASSUMED_CALL_LATENCY_S
    )


# ── 6. Provider ownership and unchanged behavior ──────────────────────────


@pytest.mark.asyncio
async def test_provider_failures_are_reported_without_a_second_retry_loop():
    """History makes ONE logical call; retries/fallbacks stay in ProviderManager."""

    class _CountingManager(ProviderManager):
        def __init__(self, registry: ProviderRegistry) -> None:
            super().__init__(registry)
            self.chat_calls = 0

        async def chat(self, messages, **kwargs):  # type: ignore[override]
            self.chat_calls += 1
            return await super().chat(messages, **kwargs)

    provider = _ScriptedProvider(fail=True)
    registry = ProviderRegistry()
    registry.register(provider)
    manager = _CountingManager(registry)
    context = _context(manager, _FakeClient(_conversation(2)))
    result = await SummarizeHistoryTool(context).execute(context, {"count": 2})

    assert result.success is False
    assert result.data["error"] == history_ai_service.ERROR_PROVIDER
    assert manager.chat_calls == 1  # no History-level retry loop
    assert provider.calls >= 1  # whatever retries happened were the manager's


@pytest.mark.asyncio
async def test_translation_output_and_identity_are_unchanged():
    def _translator(messages, payload):
        return "\n".join(
            f"[{m.group(1)}] TR:{m.group(2)}"
            for line in payload.splitlines()
            if (m := re.match(r"^\s*\[(\d+)\]\s*(.*)$", line))
        )

    provider = _ScriptedProvider(_translator)
    context = _context(_manager(provider), _FakeClient(_conversation(3)))
    result = await TranslateHistoryTool(context).execute(context, {"count": 3, "language": "English"})

    assert result.success is True
    assert result.message.splitlines() == [
        "[1] TR:message 1", "[2] TR:message 2", "[3] TR:message 3",
    ]


@pytest.mark.asyncio
async def test_the_owner_instruction_reaches_every_prompt_unchanged():
    provider = _ScriptedProvider()
    with pytest.MonkeyPatch.context() as patch:
        patch.setattr(history_ai_service, "SUMMARIZE_CHUNK_TOKEN_BUDGET", 5)
        result = await _summarize(
            provider, _FakeClient(_conversation(4)),
            {"count": 4, "instruction": "Focus on decisions"},
        )

    assert result.success is True
    assert provider.calls >= 2  # map calls + the merge
    # The instruction rides the system prompt of every call, map and reduce.
    assert all("Focus on decisions" in p[0]["content"] for p in provider.prompts)
