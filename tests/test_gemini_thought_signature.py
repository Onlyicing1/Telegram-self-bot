"""Gemini thought-signature preservation across the tool continuation.

Google's generateContent API returns ``thoughtSignature`` as a SIBLING of
``functionCall`` inside the same ``Part`` and requires it back, byte for byte,
in that exact part; for parallel calls only the FIRST ``functionCall`` part
carries it. Dropping it during normalization is what production rejected with
``400 INVALID_ARGUMENT ... missing a thought_signature``.

No network access: provider HTTP is mocked.
"""
from __future__ import annotations

import json
import logging
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.ai import continuation_diagnostic as diag
from backend.ai.engine.dispatcher import Dispatcher
from backend.ai.providers.base.capabilities import ProviderCapabilities
from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.base.contract import BaseProvider, ProviderResponse
from backend.ai.providers.gemini import GeminiProvider
from backend.ai.providers.groq import GroqProvider
from backend.ai.providers.manager.manager import ProviderManager
from backend.ai.providers.openai import OpenAIProvider
from backend.ai.providers.registry.registry import ProviderRegistry
from backend.ai.tools.executor import ToolExecutionResult

#: Opaque, base64-shaped values: the code must never decode or rewrite them.
SIG_A = "CpcBAbCd+/efGH12=="
SIG_B = "AY89xyzQRS/tuV34=="


# ── Harness ──


def _http_response(payload: dict[str, Any]) -> MagicMock:
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = payload
    return response


def _gemini(payload: dict[str, Any]) -> GeminiProvider:
    provider = GeminiProvider(
        ProviderConfig(api_key="k", enabled=True, default_model="gemini-2.5-pro")
    )
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(return_value=_http_response(payload))
    return provider


def _openai_like(cls, base_url: str) -> Any:
    provider = cls(
        ProviderConfig(
            api_key="k", enabled=True, default_model="m", base_url=base_url
        )
    )
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(
        return_value=_http_response(
            {"choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}]}
        )
    )
    return provider


def _gemini_payload(*parts: dict[str, Any]) -> dict[str, Any]:
    return {
        "candidates": [{"content": {"role": "model", "parts": list(parts)}, "finishReason": "STOP"}],
        "usageMetadata": {},
    }


def _function_part(name: str, args: dict[str, Any], signature: str | None = None,
                   call_id: str = "") -> dict[str, Any]:
    call: dict[str, Any] = {"name": name, "args": args}
    if call_id:
        call["id"] = call_id
    part: dict[str, Any] = {"functionCall": call}
    if signature is not None:
        part["thoughtSignature"] = signature
    return part


def _empty_dispatcher() -> Dispatcher:
    # _build_continuation_messages reads no instance state.
    return Dispatcher.__new__(Dispatcher)


def _continuation_messages(response: ProviderResponse) -> list[dict[str, Any]]:
    results = [
        ToolExecutionResult(tool_name=tc.get("name", ""), success=True, message="ok")
        for tc in response.tool_calls
    ]
    return _empty_dispatcher()._build_continuation_messages(
        [{"role": "user", "content": "do it"}], response, results,
    )


def _sent_contents(provider: Any) -> list[dict[str, Any]]:
    return provider._http_client.post.call_args.kwargs["json"]["contents"]


def _sent_messages(provider: Any) -> list[dict[str, Any]]:
    return provider._http_client.post.call_args.kwargs["json"]["messages"]


# ── 1-2: parsing preserves the exact opaque value ──


@pytest.mark.asyncio
async def test_function_call_thought_signature_is_parsed():
    provider = _gemini(_gemini_payload(
        _function_part("web_search", {"query": "weather"}, SIG_A, call_id="call-1"),
    ))

    response = await provider.chat([{"role": "user", "content": "x"}])

    assert response.success is True
    call = response.tool_calls[0]
    assert call["id"] == "call-1"
    assert call["name"] == "web_search"
    assert call["arguments"] == {"query": "weather"}
    assert call["thought_signature"] == SIG_A


@pytest.mark.asyncio
async def test_signature_is_preserved_byte_for_byte():
    provider = _gemini(_gemini_payload(
        _function_part("delete", {"count": 1}, SIG_A),
    ))

    response = await provider.chat([{"role": "user", "content": "x"}])

    signature = response.tool_calls[0]["thought_signature"]
    assert isinstance(signature, str)
    assert signature == SIG_A
    assert signature == "CpcBAbCd+/efGH12=="


@pytest.mark.asyncio
async def test_text_only_response_carries_no_tool_signature():
    provider = _gemini(_gemini_payload({"text": "hello"}))

    response = await provider.chat([{"role": "user", "content": "x"}])

    assert response.tool_calls == []
    assert response.text == "hello"


# ── 3: the dispatcher keeps it on the call that produced it ──


def test_dispatcher_preserves_signature_on_the_assistant_call():
    response = ProviderResponse(
        text="",
        provider_name="gemini",
        success=True,
        tool_calls=[{
            "id": "call-1",
            "name": "web_search",
            "arguments": {"query": "weather"},
            "thought_signature": SIG_A,
        }],
    )

    messages = _continuation_messages(response)

    assistant = messages[1]
    assert assistant["role"] == "assistant"
    call = assistant["tool_calls"][0]
    assert call["id"] == "call-1"
    assert call["type"] == "function"
    assert call["function"] == {
        "name": "web_search",
        "arguments": json.dumps({"query": "weather"}),
    }
    assert call["thought_signature"] == SIG_A
    assert set(call) == {"id", "type", "function", "thought_signature"}


def test_dispatcher_leaves_openai_shaped_calls_untouched():
    response = ProviderResponse(
        text="",
        provider_name="openai",
        success=True,
        tool_calls=[{"id": "call-1", "name": "web_search", "arguments": {"q": "x"}}],
    )

    messages = _continuation_messages(response)

    assert set(messages[1]["tool_calls"][0]) == {"id", "type", "function"}


# ── 4, 10: the outgoing Gemini request replays it in the exact part ──


@pytest.mark.asyncio
async def test_gemini_request_replays_exact_signature():
    response = ProviderResponse(
        text="",
        provider_name="gemini",
        success=True,
        tool_calls=[{
            "id": "call-1",
            "name": "web_search",
            "arguments": {"query": "weather"},
            "thought_signature": SIG_A,
        }],
    )
    messages = _continuation_messages(response)
    provider = _gemini(_gemini_payload({"text": "done"}))

    await provider.chat(messages)

    contents = _sent_contents(provider)
    assert [c["role"] for c in contents] == ["user", "model", "user"]
    model_part = contents[1]["parts"][0]
    assert model_part == {
        "functionCall": {
            "name": "web_search",
            "args": {"query": "weather"},
            "id": "call-1",
        },
        "thoughtSignature": SIG_A,
    }
    responses = contents[2]["parts"]
    assert len(responses) == 1
    assert responses[0]["functionResponse"]["id"] == "call-1"


@pytest.mark.asyncio
async def test_parallel_calls_keep_exact_association_and_grouping():
    """Google signs only the FIRST parallel call; the rest must stay unsigned."""
    provider = _gemini(_gemini_payload(
        _function_part("web_search", {"query": "a"}, SIG_A, call_id="call-1"),
        _function_part("list_saves", {"limit": 5}, None, call_id="call-2"),
    ))

    response = await provider.chat([{"role": "user", "content": "x"}])
    assert response.tool_calls[0]["thought_signature"] == SIG_A
    assert "thought_signature" not in response.tool_calls[1]

    messages = _continuation_messages(response)
    outbound = _gemini(_gemini_payload({"text": "done"}))
    await outbound.chat(messages)

    contents = _sent_contents(outbound)
    parts = contents[1]["parts"]
    assert [p["functionCall"]["id"] for p in parts] == ["call-1", "call-2"]
    assert parts[0]["thoughtSignature"] == SIG_A
    assert "thoughtSignature" not in parts[1]
    # Function responses stay grouped in ONE user Content, in call order.
    response_parts = contents[2]["parts"]
    assert len(contents) == 3
    assert [p["functionResponse"]["name"] for p in response_parts] == [
        "web_search", "list_saves",
    ]
    assert [p["functionResponse"]["id"] for p in response_parts] == ["call-1", "call-2"]


@pytest.mark.asyncio
async def test_distinct_signatures_stay_with_their_own_calls():
    provider = _gemini(_gemini_payload(
        _function_part("web_search", {"query": "a"}, SIG_A, call_id="call-1"),
        _function_part("delete", {"count": 1}, SIG_B, call_id="call-2"),
    ))

    response = await provider.chat([{"role": "user", "content": "x"}])
    messages = _continuation_messages(response)
    outbound = _gemini(_gemini_payload({"text": "done"}))
    await outbound.chat(messages)

    parts = _sent_contents(outbound)[1]["parts"]
    assert parts[0]["functionCall"]["id"] == "call-1"
    assert parts[0]["thoughtSignature"] == SIG_A
    assert parts[1]["functionCall"]["id"] == "call-2"
    assert parts[1]["thoughtSignature"] == SIG_B


# ── 6: a missing signature is never invented or reused ──


@pytest.mark.asyncio
async def test_missing_signature_is_not_fabricated(caplog):
    caplog.set_level(logging.WARNING)
    provider = _gemini(_gemini_payload(
        _function_part("web_search", {"query": "a"}, None, call_id="call-1"),
    ))

    response = await provider.chat([{"role": "user", "content": "x"}])
    assert "thought_signature" not in response.tool_calls[0]

    messages = _continuation_messages(response)
    outbound = _gemini(_gemini_payload({"text": "done"}))
    await outbound.chat(messages)

    part = _sent_contents(outbound)[1]["parts"][0]
    assert "thoughtSignature" not in part
    assert "GEMINI_THOUGHT_SIGNATURE_MISSING" in caplog.text
    assert "calls=1" in caplog.text
    # The signature value is never logged.
    assert SIG_A not in caplog.text


@pytest.mark.asyncio
async def test_signature_is_never_copied_onto_another_call():
    provider = _gemini(_gemini_payload(
        _function_part("web_search", {"query": "a"}, None, call_id="call-1"),
        _function_part("delete", {"count": 1}, SIG_B, call_id="call-2"),
    ))

    response = await provider.chat([{"role": "user", "content": "x"}])
    messages = _continuation_messages(response)
    outbound = _gemini(_gemini_payload({"text": "done"}))
    await outbound.chat(messages)

    parts = _sent_contents(outbound)[1]["parts"]
    assert "thoughtSignature" not in parts[0]
    assert parts[1]["thoughtSignature"] == SIG_B


# ── 7-8: other providers never receive Gemini-only metadata ──


@pytest.mark.asyncio
async def test_openai_serialization_drops_gemini_metadata():
    response = ProviderResponse(
        text="",
        provider_name="gemini",
        success=True,
        tool_calls=[{
            "id": "call-1",
            "name": "web_search",
            "arguments": {"query": "a"},
            "thought_signature": SIG_A,
        }],
    )
    messages = _continuation_messages(response)
    provider = _openai_like(OpenAIProvider, "https://api.openai.com/v1")

    await provider.chat(messages)

    sent = _sent_messages(provider)
    assert set(sent[1]["tool_calls"][0]) == {"id", "type", "function"}
    assert SIG_A not in json.dumps(sent)
    assert "thought_signature" not in json.dumps(sent)


@pytest.mark.asyncio
async def test_fallback_provider_gets_no_gemini_metadata():
    response = ProviderResponse(
        text="",
        provider_name="gemini",
        success=True,
        tool_calls=[{
            "id": "call-1",
            "name": "delete",
            "arguments": {"count": 1},
            "thought_signature": SIG_A,
        }],
    )
    messages = _continuation_messages(response)
    provider = _openai_like(GroqProvider, "https://api.groq.com/openai/v1")

    await provider.chat(messages)

    sent = _sent_messages(provider)
    assert set(sent[1]["tool_calls"][0]) == {"id", "type", "function"}
    assert SIG_A not in json.dumps(sent)
    # The ordinary OpenAI envelope is unchanged.
    assert sent[0] == {"role": "user", "content": "do it"}
    assert sent[2]["role"] == "tool"
    assert sent[2]["tool_call_id"] == "call-1"


# ── 9: the synthetic Gemini probe is honestly UNSUPPORTED ──


class _RegisteredGemini(BaseProvider):
    def __init__(self, config: ProviderConfig) -> None:
        super().__init__(config)

    @property
    def name(self) -> str:
        return "gemini"

    @property
    def capabilities(self) -> ProviderCapabilities:
        return ProviderCapabilities(supports_tools=True, supports_function_call=True)

    async def chat(self, messages: list[dict[str, Any]], **kwargs: Any) -> ProviderResponse:
        return ProviderResponse(text="ok", provider_name="gemini", success=True)

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return len(text) // 4

    def health(self) -> dict[str, Any]:
        return {"healthy": True}


class _SignatureRequiringProbe:
    REQUIRES_NATIVE_TOOL_SIGNATURE = True

    def __init__(self) -> None:
        self.calls = 0

    async def chat(self, *args: Any, **kwargs: Any) -> ProviderResponse:
        self.calls += 1
        raise AssertionError("the synthetic Gemini probe must not be sent")

    def shutdown(self) -> None:
        return None


class _ProbeFactory:
    def __init__(self) -> None:
        self.instance = _SignatureRequiringProbe()

    def create_provider(self, name: str, config: ProviderConfig | None = None) -> Any:
        return self.instance


def _gemini_manager() -> ProviderManager:
    config = ProviderConfig(
        provider_name="gemini", api_key="k", default_model="gemini-2.5-pro", enabled=True,
    )
    registry = ProviderRegistry()
    registry.register(_RegisteredGemini(config))
    registry.switch_provider("gemini")
    return ProviderManager(registry)


@pytest.mark.asyncio
async def test_gemini_probe_is_unsupported_not_rejected(monkeypatch, caplog):
    diag.reset_state()
    factory = _ProbeFactory()
    monkeypatch.setattr(diag, "ProviderFactory", factory)
    # An UNSUPPORTED probe is routine detail now: DEBUG carries it.
    caplog.set_level(logging.DEBUG, logger="backend")

    result = await diag.run_continuation_probe(
        _gemini_manager(), "gemini", "gemini-2.5-pro", request_id="rid",
    )

    assert result == diag.UNSUPPORTED
    assert "category=native_thought_signature_required" in caplog.text
    assert "CONTINUATION_REJECTED" not in caplog.text
    assert factory.instance.calls == 0


def test_real_gemini_adapter_declares_the_requirement():
    assert GeminiProvider.REQUIRES_NATIVE_TOOL_SIGNATURE is True
    assert getattr(OpenAIProvider, "REQUIRES_NATIVE_TOOL_SIGNATURE", False) is False


def test_signature_rejection_from_the_api_is_unsupported():
    """Defensive: even if a signature-less call is ever sent, it is not called a rejection."""
    response = ProviderResponse(
        text="API error (400)",
        provider_name="gemini",
        success=False,
        metadata={
            "failure_type": "request",
            "http_status": 400,
            "provider_error_message": (
                "Function call is missing a thought_signature in functionCall parts."
            ),
        },
    )

    assert diag.classify_probe_response(response) == (
        diag.UNSUPPORTED, "native_thought_signature_required",
    )
