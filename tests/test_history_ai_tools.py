"""
History AI tools — translation and summarization of Telegram history.

Contract this file pins:

  1. Telegram history comes only from ``backend/services/history_service.py``;
     the new tools and the history AI service contain no Telethon access, no
     paging and no provenance filtering of their own.
  2. Translation preserves message identity (``[id]``) and chronological order,
     sends every requested text message to the LLM, never loses or merges
     messages, and never truncates text.
  3. Summarization is genuine LLM work through the existing ProviderManager,
     hierarchical for long histories (map per chunk -> reduce), with a single
     chunk short-circuiting the reduce step.
  4. Chunking is bounded by the project's own token estimator and the provider's
     configured output budget; a single oversized message is never split.
  5. Failures (retrieval, provider, budget, too-large) are honest and
     distinguishable from an empty-but-successful result.

No network and no live Telegram: a scripted provider is registered in the REAL
``ProviderRegistry``/``ProviderManager``, and the Telegram client is a fake
shaped like the Telethon surface the facade consumes.
"""
from __future__ import annotations

import asyncio
import inspect
import re
from datetime import datetime, timezone
from typing import Any

import pytest

from backend.ai.context.provenance import AI_PROVENANCE_MARKER
from backend.ai.conversation.telegram_context import MAX_CONTEXT_MESSAGES
from backend.ai.engine.dispatcher import _VERBATIM_READ_TOOLS, Dispatcher
from backend.ai.prompt.budget import DEFAULT_MAX_CONTEXT_TOKENS, estimate_tokens
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.providers.registry.registry import ProviderRegistry
from backend.ai.tools.base import PermissionLevel, requires_owner_confirmation
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutor
from backend.ai.tools.history_ai import SummarizeHistoryTool, TranslateHistoryTool
from backend.ai.tools.registry import create_default_registry
from backend.services import history_ai_service, history_service
from backend.telegram_api.api import TelegramAPI

CHAT = -100555000
OWNER = 7770001
NOW = datetime(2026, 9, 14, 14, 2, tzinfo=timezone.utc)


# ── Fake Telegram surface ──


class _FakeMessage:
    def __init__(self, mid: int, text: str = "", *, media: bool = False, out: bool = True) -> None:
        self.id = mid
        self.chat_id = CHAT
        self.sender_id = OWNER if out else 4242
        self.text = text
        self.date = NOW
        self.media = object() if media else None
        self.reply_to = None
        self.out = out


class _FakeClient:
    def __init__(self, messages: list[_FakeMessage]) -> None:
        self._messages = list(messages)
        self.calls: list[dict[str, Any]] = []

    def iter_messages(self, chat_id: Any, **kwargs: Any):
        self.calls.append({"chat_id": chat_id, **kwargs})
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


class _FailingClient:
    def iter_messages(self, chat_id: Any, **kwargs: Any):
        async def _boom():
            raise ConnectionError("mtproto down")
            yield  # pragma: no cover

        return _boom()


# ── Scripted provider (registered in the REAL manager) ──


def _translating_provider(messages: list[dict[str, Any]], payload: str) -> str:
    """Echo every ``[id] text`` line back as ``[id] TR:text``."""
    out = []
    for line in payload.splitlines():
        match = re.match(r"^\s*\[(\d+)\]\s*(.*)$", line)
        if match:
            out.append(f"[{match.group(1)}] TR:{match.group(2)}")
    return "\n".join(out)


def _summarizing_provider(messages: list[dict[str, Any]], payload: str) -> str:
    if "merge partial summaries" in messages[0]["content"]:
        return "FINAL"
    return f"SUMMARY({len(payload.splitlines())})"


class _ScriptedProvider(BaseProvider):
    def __init__(
        self,
        responder: Any = None,
        *,
        fail_on_calls: set[int] | None = None,
        delay: float = 0.0,
    ) -> None:
        super().__init__(
            ProviderConfig(provider_name="scripted", enabled=True, default_model="m1")
        )
        self.calls = 0
        self.prompts: list[list[dict[str, Any]]] = []
        self._responder = responder
        self._fail_on = set(fail_on_calls or ())
        self._delay = delay

    @property
    def name(self) -> str:
        return "scripted"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True)

    async def chat(self, messages, **kwargs) -> ProviderResponse:
        self.prompts.append([dict(m) for m in messages])
        self.calls += 1
        if self._delay:
            await asyncio.sleep(self._delay)
        if self.calls in self._fail_on:
            return ProviderResponse(
                text="", provider_name="scripted", success=False,
                metadata={"failure_type": "rate_limit"},
            )
        payload = messages[-1]["content"] if messages else ""
        text = self._responder(messages, payload) if self._responder else "ok"
        return ProviderResponse(text=text, provider_name="scripted", success=True)

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


def _context(manager: ProviderManager, client: Any, *, use_facade: bool = True) -> ToolContext:
    return ToolContext(
        telegram=TelegramAPI(client) if use_facade else None,
        owner_id=OWNER,
        tz_str="UTC",
        client=client,
        extra={"chat_id": CHAT, "provider_manager": manager},
    )


async def _run_translate(manager: ProviderManager, client: Any, arguments: dict[str, Any]):
    """Execute the translate tool the way ToolExecutor does (same context)."""
    context = _context(manager, client)
    return await TranslateHistoryTool(context).execute(context, arguments)


async def _run_summarize(manager: ProviderManager, client: Any, arguments: dict[str, Any]):
    context = _context(manager, client)
    return await SummarizeHistoryTool(context).execute(context, arguments)


def _conversation(count: int, *, text: str = "message") -> list[_FakeMessage]:
    return [_FakeMessage(mid, f"{text} {mid}") for mid in range(1, count + 1)]


def _payloads(provider: _ScriptedProvider) -> list[str]:
    return [p[-1]["content"] for p in provider.prompts]


def _user_payload_ids(payload: str) -> list[int]:
    return [int(m.group(1)) for m in re.finditer(r"\[(\d+)\]", payload)]


def _sent_ids(provider: _ScriptedProvider) -> list[int]:
    """Ids the provider saw in map calls only (merge prompts carry summaries)."""
    return [
        mid
        for prompt in provider.prompts
        if "merge partial summaries" not in prompt[0]["content"]
        for mid in _user_payload_ids(prompt[-1]["content"])
    ]


# ── 1. Translation ──


@pytest.mark.asyncio
async def test_translate_returns_one_line_per_message_in_order():
    provider = _ScriptedProvider(_translating_provider)
    result = await _run_translate(_manager(provider), _FakeClient(_conversation(5)), {"count": 5})

    assert result.success is True
    assert result.message.splitlines() == [
        f"[{mid}] TR:message {mid}" for mid in range(1, 6)
    ]


@pytest.mark.asyncio
async def test_translate_message_identity_and_order_are_preserved():
    provider = _ScriptedProvider(_translating_provider)
    result = await _run_translate(_manager(provider), _FakeClient(_conversation(40)), {"count": 40})

    ids = [int(re.match(r"^\[(\d+)\]", line).group(1)) for line in result.message.splitlines()]
    assert ids == list(range(1, 41))
    assert len(ids) == len(set(ids))


@pytest.mark.asyncio
async def test_translate_requests_exactly_the_requested_count_oldest_to_newest():
    provider = _ScriptedProvider(_translating_provider)
    result = await _run_translate(_manager(provider), _FakeClient(_conversation(50)), {"count": 10})

    assert _user_payload_ids(_payloads(provider)[0]) == list(range(41, 51))
    assert len(result.message.splitlines()) == 10


@pytest.mark.asyncio
async def test_translate_pages_through_multiple_pages_of_history():
    provider = _ScriptedProvider(_translating_provider)
    client = _FakeClient(_conversation(250))
    result = await _run_translate(_manager(provider), client, {"count": 250})

    assert len(client.calls) == 3  # history_service paged backwards
    assert [call.get("max_id") for call in client.calls] == [None, 151, 51]
    assert sorted(set(_sent_ids(provider))) == list(range(1, 251))
    assert len(result.message.splitlines()) == 250


@pytest.mark.asyncio
async def test_translate_empty_history_succeeds_without_calling_the_provider():
    provider = _ScriptedProvider(_translating_provider)
    result = await _run_translate(_manager(provider), _FakeClient([]), {"count": 10})

    assert result.success is True
    assert provider.calls == 0
    assert "no messages" in result.message.lower()
    assert result.data["processed"] == 0


@pytest.mark.asyncio
async def test_translate_media_only_and_empty_messages_keep_their_place():
    provider = _ScriptedProvider(_translating_provider)
    client = _FakeClient([
        _FakeMessage(1, "hello"),
        _FakeMessage(2, "", media=True),
        _FakeMessage(3, "   "),
        _FakeMessage(4, "bye"),
    ])
    result = await _run_translate(_manager(provider), client, {"count": 4})

    assert result.message.splitlines() == [
        "[1] TR:hello", "[2] [media]", "[3] (empty message)", "[4] TR:bye",
    ]
    assert _user_payload_ids(_payloads(provider)[0]) == [1, 4]
    assert result.data["text_messages"] == 2


@pytest.mark.asyncio
async def test_translate_never_truncates_message_text():
    long_text = "x" * 3000
    provider = _ScriptedProvider(_translating_provider)
    client = _FakeClient([_FakeMessage(1, long_text)])
    await _run_translate(_manager(provider), client, {"count": 1})

    assert long_text in _payloads(provider)[0]
    assert "…" not in _payloads(provider)[0]


@pytest.mark.asyncio
async def test_translate_history_failure_is_reported_not_treated_as_empty():
    provider = _ScriptedProvider(_translating_provider)
    result = await _run_translate(_manager(provider), _FailingClient(), {"count": 10})

    assert result.success is False
    assert result.message.startswith("❌")
    assert result.data["error"] == history_ai_service.ERROR_HISTORY
    assert provider.calls == 0


@pytest.mark.asyncio
async def test_translate_reports_truncated_history_honestly():
    provider = _ScriptedProvider(_translating_provider)
    result = await _run_translate(_manager(provider), _FakeClient(_conversation(5)), {"count": 50})

    assert result.success is True
    assert "Only 5 of the requested 50" in result.message
    assert result.data["processed"] == 5


@pytest.mark.asyncio
async def test_translate_count_beyond_the_bound_is_capped_and_stated(monkeypatch):
    requested: list[int] = []
    real = history_service.fetch_recent_history

    async def _record(source, chat_id, **kwargs):
        requested.append(kwargs["count"])
        return await real(source, chat_id, **kwargs)

    monkeypatch.setattr(history_service, "fetch_recent_history", _record)
    provider = _ScriptedProvider(_translating_provider)
    result = await _run_translate(
        _manager(provider), _FakeClient(_conversation(5)), {"count": 50_000},
    )

    assert requested == [history_service.MAX_HISTORY_MESSAGES]
    assert result.data["capped"] is True
    assert str(history_service.MAX_HISTORY_MESSAGES) in result.message


@pytest.mark.asyncio
async def test_translate_provider_failure_is_honest():
    provider = _ScriptedProvider(_translating_provider, fail_on_calls={1})
    result = await _run_translate(_manager(provider), _FakeClient(_conversation(3)), {"count": 3})

    assert result.success is False
    assert result.message.startswith("❌")
    assert result.data["error"] == history_ai_service.ERROR_PROVIDER
    assert "TR:" not in result.message


@pytest.mark.asyncio
async def test_translate_incomplete_model_output_fails_instead_of_dropping_messages():
    def _partial(messages, payload):  # omits every message after the first
        first = re.match(r"^\s*\[(\d+)\]\s*(.*)$", payload.splitlines()[0])
        return f"[{first.group(1)}] TR:{first.group(2)}"

    provider = _ScriptedProvider(_partial)
    result = await _run_translate(_manager(provider), _FakeClient(_conversation(3)), {"count": 3})

    assert result.success is False
    assert "no translation" in result.message
    assert result.data["error"] == history_ai_service.ERROR_PROVIDER


@pytest.mark.asyncio
async def test_translate_excludes_ai_provenance_messages_centrally():
    provider = _ScriptedProvider(_translating_provider)
    client = _FakeClient([
        _FakeMessage(1, "human one"),
        _FakeMessage(2, f"an AI answer{AI_PROVENANCE_MARKER}"),
        _FakeMessage(3, "human two"),
    ])
    result = await _run_translate(_manager(provider), client, {"count": 3})

    assert _sent_ids(provider) == [1, 3]
    assert "[2]" not in result.message
    assert AI_PROVENANCE_MARKER not in result.message
    assert AI_PROVENANCE_MARKER not in "".join(_payloads(provider))


@pytest.mark.asyncio
async def test_translate_chunks_large_histories_without_losing_messages(monkeypatch):
    monkeypatch.setattr(history_ai_service, "CHUNK_TOKEN_BUDGET", 6)
    provider = _ScriptedProvider(_translating_provider)
    result = await _run_translate(_manager(provider), _FakeClient(_conversation(6)), {"count": 6})

    assert provider.calls == 6
    assert len(result.message.splitlines()) == 6
    assert _sent_ids(provider) == list(range(1, 7))


@pytest.mark.asyncio
async def test_translate_works_through_a_raw_client_and_a_facade_identically():
    via_facade = _ScriptedProvider(_translating_provider)
    via_client = _ScriptedProvider(_translating_provider)
    context_facade = _context(_manager(via_facade), _FakeClient(_conversation(4)), use_facade=True)
    context_raw = _context(_manager(via_client), _FakeClient(_conversation(4)), use_facade=False)

    first = await TranslateHistoryTool(context_facade).execute(context_facade, {"count": 4})
    second = await TranslateHistoryTool(context_raw).execute(context_raw, {"count": 4})

    assert first.success is True
    assert first.message == second.message


@pytest.mark.asyncio
async def test_translate_without_chat_context_fails_clearly():
    context = ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC")
    result = await TranslateHistoryTool(context).execute(context, {"count": 5})
    assert result.success is False
    assert "No chat context" in result.message


# ── 2. Summarization ──


@pytest.mark.asyncio
async def test_summarize_small_history_uses_one_llm_call_and_no_reduce():
    provider = _ScriptedProvider(_summarizing_provider)
    result = await _run_summarize(_manager(provider), _FakeClient(_conversation(5)), {"count": 5})

    assert result.success is True
    assert provider.calls == 1
    assert result.message == "SUMMARY(5)"


@pytest.mark.asyncio
async def test_summarize_multi_chunk_history_aggregates_hierarchically(monkeypatch):
    monkeypatch.setattr(history_ai_service, "CHUNK_TOKEN_BUDGET", 6)
    provider = _ScriptedProvider(_summarizing_provider)
    result = await _run_summarize(_manager(provider), _FakeClient(_conversation(3)), {"count": 3})

    assert provider.calls == 4  # 3 chunk summaries + 1 final merge
    assert result.message == "FINAL"
    reduce_prompt = provider.prompts[-1]
    assert "merge partial summaries" in reduce_prompt[0]["content"]
    assert reduce_prompt[-1]["content"].count("SUMMARY(1)") == 3


@pytest.mark.asyncio
async def test_summarize_orders_history_before_summarizing():
    provider = _ScriptedProvider(_summarizing_provider)
    await _run_summarize(_manager(provider), _FakeClient(_conversation(60)), {"count": 60})

    chunk_ids = [
        _user_payload_ids(prompt[-1]["content"]) for prompt in provider.prompts
    ]
    assert all(ids == sorted(ids) for ids in chunk_ids)
    assert [mid for ids in chunk_ids for mid in ids] == list(range(1, 61))


@pytest.mark.asyncio
async def test_summarize_500_messages_map_reduces_within_the_budget():
    provider = _ScriptedProvider(_summarizing_provider)
    result = await _run_summarize(_manager(provider), _FakeClient(_conversation(500)), {"count": 500})

    assert result.success is True
    assert result.data["processed"] == 500
    chunks = provider.calls - 1
    assert chunks > 1
    assert result.message == "FINAL"
    assert sorted(set(_sent_ids(provider))) == list(range(1, 501))


@pytest.mark.asyncio
async def test_summarize_1000_messages_uses_the_service_boundary():
    provider = _ScriptedProvider(_summarizing_provider)
    result = await _run_summarize(
        _manager(provider), _FakeClient(_conversation(1000)), {"count": 1000},
    )

    assert result.success is True
    assert result.data["processed"] == 1000
    assert result.data["capped"] is False
    sent = _sent_ids(provider)
    assert len(sent) == 1000
    assert sorted(set(sent)) == list(range(1, 1001))


@pytest.mark.asyncio
async def test_no_chunk_exceeds_the_token_budget():
    provider = _ScriptedProvider(_summarizing_provider)
    await _run_summarize(_manager(provider), _FakeClient(_conversation(300)), {"count": 300})

    for payload in _payloads(provider):
        conservative = max(
            estimate_tokens(payload, "English"), estimate_tokens(payload, "Persian")
        )
        assert conservative <= history_ai_service.CHUNK_TOKEN_BUDGET + 40


@pytest.mark.asyncio
async def test_oversized_single_message_is_its_own_chunk_and_never_split(monkeypatch):
    monkeypatch.setattr(history_ai_service, "CHUNK_TOKEN_BUDGET", 4)
    huge = "y" * 2000
    provider = _ScriptedProvider(_summarizing_provider)
    client = _FakeClient([_FakeMessage(1, huge), _FakeMessage(2, "small")])
    await _run_summarize(_manager(provider), client, {"count": 2})

    payloads = _payloads(provider)
    assert huge in payloads[0]
    assert "[1]" in payloads[0]
    assert "[2]" not in payloads[0]


@pytest.mark.asyncio
async def test_summarize_excludes_ai_provenance_messages():
    provider = _ScriptedProvider(_summarizing_provider)
    client = _FakeClient([
        _FakeMessage(1, "human one"),
        _FakeMessage(2, f"AI answer{AI_PROVENANCE_MARKER}"),
    ])
    result = await _run_summarize(_manager(provider), client, {"count": 2})

    assert result.success is True
    assert _sent_ids(provider) == [1]
    assert AI_PROVENANCE_MARKER not in result.message


@pytest.mark.asyncio
async def test_summarize_empty_history_succeeds_without_llm_calls():
    provider = _ScriptedProvider(_summarizing_provider)
    result = await _run_summarize(_manager(provider), _FakeClient([]), {"count": 10})

    assert result.success is True
    assert provider.calls == 0
    assert "no messages" in result.message.lower()


@pytest.mark.asyncio
async def test_summarize_media_only_history_succeeds_without_llm_calls():
    provider = _ScriptedProvider(_summarizing_provider)
    client = _FakeClient([_FakeMessage(1, "", media=True), _FakeMessage(2, "")])
    result = await _run_summarize(_manager(provider), client, {"count": 2})

    assert result.success is True
    assert provider.calls == 0
    assert result.data["text_messages"] == 0


@pytest.mark.asyncio
async def test_summarize_history_failure_is_reported_not_treated_as_empty():
    provider = _ScriptedProvider(_summarizing_provider)
    result = await _run_summarize(_manager(provider), _FailingClient(), {"count": 10})

    assert result.success is False
    assert result.message.startswith("❌")
    assert result.data["error"] == history_ai_service.ERROR_HISTORY


@pytest.mark.asyncio
async def test_summarize_reports_truncated_history_honestly():
    provider = _ScriptedProvider(_summarizing_provider)
    result = await _run_summarize(_manager(provider), _FakeClient(_conversation(4)), {"count": 400})

    assert result.success is True
    assert "Only 4 of the requested 400" in result.message


@pytest.mark.asyncio
async def test_summarize_chunk_failure_is_not_presented_as_a_summary(monkeypatch):
    monkeypatch.setattr(history_ai_service, "CHUNK_TOKEN_BUDGET", 6)
    provider = _ScriptedProvider(_summarizing_provider, fail_on_calls={2})
    result = await _run_summarize(_manager(provider), _FakeClient(_conversation(3)), {"count": 3})

    assert result.success is False
    assert "SUMMARY" not in result.message
    assert result.data["error"] == history_ai_service.ERROR_PROVIDER


@pytest.mark.asyncio
async def test_summarize_final_aggregation_failure_is_honest(monkeypatch):
    monkeypatch.setattr(history_ai_service, "CHUNK_TOKEN_BUDGET", 6)
    provider = _ScriptedProvider(_summarizing_provider, fail_on_calls={4})
    result = await _run_summarize(_manager(provider), _FakeClient(_conversation(3)), {"count": 3})

    assert result.success is False
    assert "SUMMARY" not in result.message
    assert result.data["error"] == history_ai_service.ERROR_PROVIDER


@pytest.mark.asyncio
async def test_summarize_budget_timeout_is_an_honest_failure(monkeypatch):
    monkeypatch.setattr(history_ai_service, "LLM_BUDGET_S", 0.01)
    provider = _ScriptedProvider(_summarizing_provider, delay=0.2)
    result = await _run_summarize(_manager(provider), _FakeClient(_conversation(5)), {"count": 5})

    assert result.success is False
    assert "did not finish" in result.message
    assert result.data["error"] == history_ai_service.ERROR_PROVIDER


@pytest.mark.asyncio
async def test_history_too_large_for_one_request_is_refused_honestly(monkeypatch):
    monkeypatch.setattr(history_ai_service, "CHUNK_TOKEN_BUDGET", 1)
    monkeypatch.setattr(history_ai_service, "MAX_MAP_CALLS", 2)
    provider = _ScriptedProvider(_summarizing_provider)
    result = await _run_summarize(_manager(provider), _FakeClient(_conversation(5)), {"count": 5})

    assert result.success is False
    assert result.data["error"] == history_ai_service.ERROR_TOO_LARGE
    assert provider.calls == 0
    assert "fewer messages" in result.message


@pytest.mark.asyncio
async def test_missing_ai_engine_is_reported():
    ok, text, data = await history_ai_service.summarize_history(
        _FakeClient(_conversation(2)), CHAT, count=2, provider_manager=None,
    )

    assert ok is False
    assert text.startswith("❌")
    assert data["error"] in {
        history_ai_service.ERROR_ENGINE, history_ai_service.ERROR_PROVIDER,
    }


# ── 3. Architecture ──


def test_chunk_budget_is_derived_from_existing_architecture():
    assert history_ai_service.CHUNK_TOKEN_BUDGET == min(
        DEFAULT_MAX_CONTEXT_TOKENS, ProviderConfig().max_tokens // 2
    )
    assert history_ai_service.MAP_CONCURRENCY == 4
    assert history_ai_service.MAX_MAP_CALLS == 40
    assert history_ai_service.PER_CALL_TIMEOUT_S == 30.0


@pytest.mark.asyncio
async def test_both_tools_retrieve_history_through_the_history_service(monkeypatch):
    calls: list[dict[str, Any]] = []
    real = history_service.fetch_recent_history

    async def _record(source, chat_id, **kwargs):
        calls.append({"chat_id": chat_id, **kwargs})
        return await real(source, chat_id, **kwargs)

    monkeypatch.setattr(history_service, "fetch_recent_history", _record)
    provider = _ScriptedProvider(_translating_provider)

    await _run_translate(_manager(provider), _FakeClient(_conversation(3)), {"count": 3})
    await _run_summarize(_manager(provider), _FakeClient(_conversation(3)), {"count": 3})

    assert [c["count"] for c in calls] == [3, 3]
    assert all(c["chat_id"] == CHAT for c in calls)


def test_new_modules_contain_no_telegram_paging_or_provenance_logic():
    from backend.ai.tools import history_ai as history_ai_tools

    for module in (history_ai_service, history_ai_tools):
        source = inspect.getsource(module)
        assert re.search(r"^\s*(import|from)\s+telethon", source, re.MULTILINE) is None
        assert ".iter_messages(" not in source
        assert "has_ai_provenance_marker" not in source
        assert "AI_PROVENANCE_MARKER" not in source
        assert "ProviderManager(" not in source
        assert "get_engine()" in source or module is history_ai_tools


def test_tools_are_registered_as_read_only_and_long_running():
    registry = create_default_registry(
        ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC")
    )
    for name in ("translate_history", "summarize_history"):
        tool = registry.get(name)
        assert tool is not None
        assert tool.permission_level is PermissionLevel.READ_ONLY
        assert tool.safe is True
        assert tool.long_running is True
        assert requires_owner_confirmation(tool) is False

    schema = {s["name"]: s for s in registry.list_schemas()}
    assert schema["translate_history"]["parameters"]["count"]["maximum"] == 1000
    assert schema["summarize_history"]["parameters"]["count"]["maximum"] == 1000
    assert "language" in schema["translate_history"]["parameters"]


@pytest.mark.asyncio
async def test_tool_executor_executes_them_without_confirmation():
    provider = _ScriptedProvider(_translating_provider)
    client = _FakeClient(_conversation(3))
    context = _context(_manager(provider), client)
    executor = ToolExecutor(create_default_registry(context), context)

    results = await executor.execute_calls([
        {"id": "c1", "name": "translate_history", "arguments": {"count": 3}},
    ])

    assert len(results) == 1
    assert results[0].success is True
    assert results[0].message.splitlines()[0] == "[1] TR:message 1"


def test_dispatcher_delivers_both_tools_verbatim():
    assert {"translate_history", "summarize_history"} <= _VERBATIM_READ_TOOLS

    class _Exec:
        def __init__(self, name: str, success: bool = True) -> None:
            self.tool_name = name
            self.success = success

    calls = [{"name": "translate_history"}]
    assert Dispatcher._read_results_authoritative(calls, [_Exec("translate_history")]) is True
    assert Dispatcher._read_results_authoritative(
        calls, [_Exec("translate_history", success=False)]
    ) is False


def test_bounded_request_scoped_context_is_unchanged():
    assert MAX_CONTEXT_MESSAGES == 10


# ── 4. Request-scoped trigger exclusion (live "خلاصه ۳۰ پیام آخر رو بده") ──


def _context_with_request(
    manager: ProviderManager,
    client: Any,
    message_id: int,
    *,
    request_id: str = "req-1",
) -> ToolContext:
    """The dispatcher's request scope: chat, triggering message id, manager.

    ``request_message_id`` is the key ``Dispatcher._build_tool_context`` sets,
    so this is the real production context shape.
    """
    return ToolContext(
        telegram=TelegramAPI(client),
        owner_id=OWNER,
        tz_str="UTC",
        client=client,
        extra={
            "chat_id": CHAT,
            "request_message_id": message_id,
            "request_id": request_id,
            "provider_manager": manager,
        },
    )


@pytest.mark.asyncio
async def test_triggering_message_is_excluded_by_id_from_the_retrieved_history():
    provider = _ScriptedProvider(_translating_provider)
    client = _FakeClient(_conversation(30))
    context = _context_with_request(_manager(provider), client, 30)

    result = await TranslateHistoryTool(context).execute(context, {"count": 30})

    assert result.success is True
    assert client.calls[0]["max_id"] == 30          # exclusive cursor, not a text filter
    assert _sent_ids(provider) == list(range(1, 30))
    assert 30 not in _sent_ids(provider)
    assert "[30]" not in result.message
    assert result.data["processed"] == 29


@pytest.mark.asyncio
async def test_trigger_exclusion_is_identity_based_not_text_based():
    """The same text is kept when it is not the triggering message."""
    provider = _ScriptedProvider(_translating_provider)
    command = "خلاصه ۳۰ پیام آخر رو بده"
    client = _FakeClient([
        _FakeMessage(1, command),   # older message with identical text
        _FakeMessage(2, "hello"),
        _FakeMessage(3, command),   # the triggering command itself
    ])
    context = _context_with_request(_manager(provider), client, 3)

    result = await TranslateHistoryTool(context).execute(context, {"count": 3})

    assert result.success is True
    assert _sent_ids(provider) == [1, 2]
    assert "[1]" in result.message and "[3]" not in result.message


@pytest.mark.asyncio
async def test_summarize_30_message_request_completes_through_the_tool_path():
    provider = _ScriptedProvider(_summarizing_provider)
    client = _FakeClient(_conversation(30))
    context = _context_with_request(_manager(provider), client, 30)

    result = await SummarizeHistoryTool(context).execute(context, {"count": 30})

    assert result.success is True
    assert provider.calls >= 1
    assert result.message.strip()
    assert result.data["operation"] == "summarize"
    assert result.data["processed"] == 29
    assert 30 not in _sent_ids(provider)


@pytest.mark.asyncio
async def test_request_without_a_scoped_message_id_keeps_the_full_window():
    """An internal caller with no triggering message is not cursor-bounded."""
    provider = _ScriptedProvider(_summarizing_provider)
    client = _FakeClient(_conversation(5))

    result = await _run_summarize(_manager(provider), client, {"count": 5})

    assert result.success is True
    assert client.calls[0].get("max_id") is None
    assert result.data["processed"] == 5


@pytest.mark.asyncio
async def test_stage_traces_distinguish_retrieval_provider_and_tool_result(caplog):
    import logging

    provider = _ScriptedProvider(_summarizing_provider)
    client = _FakeClient(_conversation(5))
    context = _context_with_request(_manager(provider), client, 5, request_id="req-trace")

    with caplog.at_level(logging.INFO):
        result = await SummarizeHistoryTool(context).execute(context, {"count": 5})

    traces = [r.getMessage() for r in caplog.records if "AI_EXEC_TRACE" in r.getMessage()]
    assert result.success is True
    assert any("stage=history_retrieval_started" in t and "req-trace" in t for t in traces)
    assert any("stage=history_retrieval_completed" in t for t in traces)
    assert any("before_id=5" in t for t in traces)
    assert any("stage=provider_call_started" in t for t in traces)
    assert any("stage=provider_call_completed" in t for t in traces)
    assert any("stage=tool_result" in t and "tool=summarize_history" in t for t in traces)
    assert any("success=True" in t and "stage=tool_result" in t for t in traces)


@pytest.mark.asyncio
async def test_history_retrieval_failure_is_traced_and_never_a_silent_empty_result(caplog):
    import logging

    provider = _ScriptedProvider(_summarizing_provider)
    context = _context_with_request(
        _manager(provider), _FailingClient(), 3, request_id="req-fail",
    )

    with caplog.at_level(logging.INFO):
        result = await SummarizeHistoryTool(context).execute(context, {"count": 3})

    traces = [r.getMessage() for r in caplog.records if "AI_EXEC_TRACE" in r.getMessage()]
    assert result.success is False
    assert result.data["error"] == history_ai_service.ERROR_HISTORY
    assert provider.calls == 0
    assert any("stage=history_retrieval_failed" in t for t in traces)
    assert any("stage=tool_result" in t and "success=False" in t for t in traces)


@pytest.mark.asyncio
async def test_provider_failure_is_traced_and_surfaced_honestly(caplog):
    import logging

    provider = _ScriptedProvider(_summarizing_provider, fail_on_calls={1})
    client = _FakeClient(_conversation(3))
    context = _context_with_request(_manager(provider), client, 3, request_id="req-pfail")

    with caplog.at_level(logging.INFO):
        result = await SummarizeHistoryTool(context).execute(context, {"count": 3})

    traces = [r.getMessage() for r in caplog.records if "AI_EXEC_TRACE" in r.getMessage()]
    assert result.success is False
    assert result.data["error"] == history_ai_service.ERROR_PROVIDER
    assert any("stage=provider_call_failed" in t for t in traces)
    assert any("stage=tool_result" in t and "success=False" in t for t in traces)


@pytest.mark.asyncio
async def test_summary_result_remains_deliverable_through_the_delivery_path():
    from types import SimpleNamespace

    from backend.ai.context.provenance import strip_ai_provenance_marker
    from backend.ai.tools.delivery import deliver_response

    provider = _ScriptedProvider(_summarizing_provider)
    client = _FakeClient(_conversation(30))
    context = _context_with_request(_manager(provider), client, 30)
    result = await SummarizeHistoryTool(context).execute(context, {"count": 30})

    edits: list[str] = []
    replies: list[str] = []

    async def edit(text):
        edits.append(text)

    async def reply(text):
        replies.append(text)

    delivered = await deliver_response(
        SimpleNamespace(edit=edit, reply=reply),
        "خلاصه ۳۰ پیام آخر رو بده",
        result.message,
    )

    assert result.success is True
    assert delivered.success is True
    assert len(edits) == 1 and replies == []
    assert strip_ai_provenance_marker(edits[0]) == result.message.strip()


# ── 5. Routing: analysis requests reach the provider ──


def test_history_analysis_requests_are_not_routed_to_the_review_listing():
    from backend.ai.actions import KIND_CONVERSATIONAL, parse_command_intent

    for text in (
        "خلاصه ۳۰ پیام آخر رو بده",
        "ترجمه ۱۰ پیام آخر",
        "summarize the last 500 messages",
        "translate the last 100 messages to English",
    ):
        result = parse_command_intent(text, has_reply=False)
        assert result.kind == KIND_CONVERSATIONAL, text
        assert result.action != "list_recent_messages", text


def test_review_requests_still_resolve_deterministically():
    from backend.ai.actions import parse_command_intent

    for text, count in (("ده پیام آخر رو بررسی کن", 10), ("last 10 messages", 10)):
        result = parse_command_intent(text, has_reply=False)
        assert result.action == "list_recent_messages", text
        assert result.count == count, text


def test_analysis_routing_does_not_divert_delete_or_save_commands():
    from backend.ai.actions import parse_command_intent

    result = parse_command_intent("پیام‌های خلاصه رو پاک کن", has_reply=False)
    assert result.action == "delete_messages"


def test_trigger_exclusion_adds_no_text_heuristic_to_the_ai_path():
    from backend.ai.tools import history_ai as history_ai_tools

    tool_source = inspect.getsource(history_ai_tools)
    service_source = inspect.getsource(history_ai_service)
    assert "request_message_id" in tool_source          # request-scoped identity
    assert "before_id=before_id" in service_source      # cursor, not matching
    for source in (tool_source, service_source):
        assert "startswith(" not in source
        assert "has_ai_provenance_marker" not in source
        assert "re.compile(" not in source
