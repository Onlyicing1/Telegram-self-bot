"""
Provider system-instruction contract (Stage 6 — Workstream A3).

`Dispatcher._build_messages` legitimately emits SEVERAL system messages
(merged rules, runtime context, conversation state, tool contract). The
investigation found that the Gemini adapter assigned ``system_text`` per
system message, so only the LAST one survived as ``systemInstruction``: every
Gemini-routed request lost its rules and output contract.

These tests prove, at the adapter boundary, that:

  - Gemini forwards EVERY system section, in order, as one systemInstruction;
  - Gemini preserves the complete tool contract (required/enums/nested/arrays)
    with its provider's Type spelling;
  - OpenAI-compatible providers forward all messages and all tools verbatim;
  - every chat-capable provider uses one of those two paths — none silently
    drops instructions.
"""
from __future__ import annotations

import json
from typing import Any
from unittest.mock import AsyncMock, MagicMock

import pytest

from backend.ai.providers.base.config import ProviderConfig
from backend.ai.providers.gemini import GeminiProvider


def _gemini() -> GeminiProvider:
    provider = GeminiProvider(
        ProviderConfig(api_key="k", enabled=True, default_model="gemini-pro")
    )
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "candidates": [{"content": {"parts": [{"text": "ok"}]}, "finishReason": "STOP"}],
        "usageMetadata": {},
    }
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(return_value=response)
    return provider


def _payload(provider) -> dict[str, Any]:
    return provider._http_client.post.call_args.kwargs["json"]


@pytest.mark.asyncio
async def test_gemini_keeps_every_system_section_in_order():
    provider = _gemini()
    messages = [
        {"role": "system", "content": "SYSTEM-RULES-CANARY"},
        {"role": "system", "content": "RUNTIME-CANARY"},
        {"role": "system", "content": "CONVERSATION-CANARY"},
        {"role": "system", "content": "TOOL-CONTRACT-CANARY"},
        {"role": "user", "content": "hello"},
    ]

    await provider.chat(messages)

    payload = _payload(provider)
    system_text = payload["systemInstruction"]["parts"][0]["text"]
    for canary in (
        "SYSTEM-RULES-CANARY",
        "RUNTIME-CANARY",
        "CONVERSATION-CANARY",
        "TOOL-CONTRACT-CANARY",
    ):
        assert canary in system_text, canary
    positions = [
        system_text.index(canary)
        for canary in (
            "SYSTEM-RULES-CANARY",
            "RUNTIME-CANARY",
            "CONVERSATION-CANARY",
            "TOOL-CONTRACT-CANARY",
        )
    ]
    assert positions == sorted(positions)
    # No instructions were duplicated into (or dropped from) the contents.
    assert len(payload["contents"]) == 1
    assert payload["contents"][0] == {"role": "user", "parts": [{"text": "hello"}]}
    assert "CANARY" not in json.dumps(payload["contents"])


@pytest.mark.asyncio
async def test_gemini_maps_non_system_turns_in_order():
    provider = _gemini()
    messages = [
        {"role": "system", "content": "SYS"},
        {"role": "user", "content": "first"},
        {
            "role": "assistant",
            "content": "",
            "tool_calls": [{
                "id": "c1",
                "type": "function",
                "function": {"name": "search", "arguments": json.dumps({"query": "x"})},
            }],
        },
        {"role": "tool", "tool_call_id": "c1", "name": "search", "content": json.dumps({"success": True})},
        {"role": "user", "content": "second"},
    ]

    await provider.chat(messages)

    contents = _payload(provider)["contents"]
    assert [c["role"] for c in contents] == ["user", "model", "model", "user"]
    assert contents[0]["parts"][0]["text"] == "first"
    assert "functionCall" in contents[1]["parts"][0]
    assert "functionResponse" in contents[2]["parts"][0]
    assert contents[3]["parts"][0]["text"] == "second"


@pytest.mark.asyncio
async def test_gemini_preserves_the_complete_tool_contract():
    provider = _gemini()
    tools = [{
        "type": "function",
        "function": {
            "name": "web_search",
            "description": "Search the live web.",
            "parameters": {
                "type": "object",
                "properties": {
                    "query": {"type": "string", "description": "the query"},
                    "freshness": {"type": "string", "enum": ["day", "week"]},
                    "count": {"type": "integer", "default": 10},
                    "include_domains": {"type": "array", "items": {"type": "string"}},
                    "semantic": {
                        "type": "object",
                        "properties": {"word_count": {"type": "integer", "minimum": 1}},
                    },
                },
                "required": ["query"],
            },
        },
    }]

    await provider.chat([{"role": "user", "content": "hi"}], tools=tools)

    declarations = _payload(provider)["tools"][0]["functionDeclarations"]
    assert len(declarations) == 1
    declaration = declarations[0]
    assert declaration["name"] == "web_search"
    assert declaration["description"] == "Search the live web."
    params = declaration["parameters"]
    assert params["type"] == "OBJECT"
    assert params["required"] == ["query"]
    assert params["properties"]["freshness"]["enum"] == ["day", "week"]
    assert params["properties"]["count"]["default"] == 10
    assert params["properties"]["include_domains"]["type"] == "ARRAY"
    assert params["properties"]["include_domains"]["items"]["type"] == "STRING"
    assert params["properties"]["semantic"]["type"] == "OBJECT"
    assert params["properties"]["semantic"]["properties"]["word_count"]["type"] == "INTEGER"
    assert params["properties"]["semantic"]["properties"]["word_count"]["minimum"] == 1


@pytest.mark.asyncio
async def test_openai_compatible_providers_forward_messages_and_tools_verbatim():
    from backend.ai.providers.groq import GroqProvider

    provider = GroqProvider(
        ProviderConfig(api_key="k", enabled=True, default_model="m")
    )
    response = MagicMock()
    response.status_code = 200
    response.json.return_value = {
        "choices": [{"message": {"content": "ok"}, "finish_reason": "stop"}],
        "usage": {},
    }
    provider._http_client = MagicMock()
    provider._http_client.post = AsyncMock(return_value=response)

    messages = [
        {"role": "system", "content": "SYSTEM-RULES-CANARY"},
        {"role": "system", "content": "RUNTIME-CANARY"},
        {"role": "system", "content": "CONVERSATION-CANARY"},
        {"role": "system", "content": "TOOL-CONTRACT-CANARY"},
        {"role": "user", "content": "hello"},
    ]
    tools = [{
        "type": "function",
        "function": {
            "name": "save",
            "description": "d",
            "parameters": {"type": "object", "properties": {}, "required": []},
        },
    }]

    await provider.chat(messages, tools=tools)

    payload = provider._http_client.post.call_args.kwargs["json"]
    # Every system message arrives, unchanged and in order.
    assert payload["messages"] == messages
    system_messages = [m for m in payload["messages"] if m["role"] == "system"]
    assert len(system_messages) == 4
    assert payload["tools"] == tools
    assert payload["tool_choice"] == "auto"


def test_every_chat_provider_uses_a_verified_instruction_path():
    """Pin the adapter coverage: no third message-handling implementation."""
    from backend.ai.providers import (
        cerebras,
        cohere,
        fireworks,
        groq,
        mistral,
        nararouter,
        nvidia,
        openai,
        openrouter,
        sambanova,
        siliconflow,
        zai,
    )
    from backend.ai.providers.openai_compat import OpenAICompatProvider

    openai_compatible = [
        cerebras.CerebrasProvider,
        cohere.CohereProvider,
        fireworks.FireworksProvider,
        groq.GroqProvider,
        mistral.MistralProvider,
        nararouter.NaraRouterProvider,
        nvidia.NVIDIAProvider,
        openai.OpenAIProvider,
        openrouter.OpenRouterProvider,
        sambanova.SambaNovaProvider,
        siliconflow.SiliconFlowProvider,
        zai.ZaiProvider,
    ]
    for provider_cls in openai_compatible:
        assert issubclass(provider_cls, OpenAICompatProvider), provider_cls

    # Gemini is the ONLY custom chat mapping; it is covered above.
    assert not issubclass(GeminiProvider, OpenAICompatProvider)
    assert GeminiProvider.CAPABILITY_KIND == "chat"

    # The retrieval capability is not a chat engine and never receives prompts.
    from backend.ai.providers.you_search import YouSearchProvider

    assert YouSearchProvider.CAPABILITY_KIND == "web_search"
