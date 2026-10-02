"""
Immediate (non-scheduled) text-write execution.

An immediate text write ("بنویس سلام" / "write hello") is now interpreted by
the MODEL, which emits the registered ``send_message`` tool with the text
argument; the deterministic parser that used to recognise the imperative verb
is gone. The execution boundary is unchanged:

    model emits send_message -> ToolExecutor -> SendMessageTool
      -> TelegramAPI.send_message(owner_id, text)  (trusted owner destination)

The architecture still never lets the model choose a destination: recipient
and reference sends remain unsupported because ``send_message`` resolves the
owner from trusted runtime context only.
"""
from __future__ import annotations

from typing import Any
from unittest.mock import AsyncMock, MagicMock, patch

import pytest

from backend.ai.actions import KIND_EXECUTABLE, KIND_INVALID, validate_action


# ── Deterministic intent recognition ──


# ── Immediate vs scheduled vs historical separation ──


# ── Structured action contract (provider JSON path) ──


def test_validate_send_requires_text():
    ok = validate_action({"action": "send", "text": "hello"})
    assert ok.kind == KIND_EXECUTABLE
    assert ok.text == "hello"
    missing = validate_action({"action": "send"})
    assert missing.kind == KIND_INVALID
    empty = validate_action({"action": "send", "content": "   "})
    assert empty.kind == KIND_INVALID


def test_validate_send_accepts_content_alias():
    ok = validate_action({"action": "send", "content": "سلام"})
    assert ok.kind == KIND_EXECUTABLE
    assert ok.text == "سلام"


def test_validate_send_rejects_recipient():
    r = validate_action({"action": "send", "text": "hi", "recipient": "ali"})
    assert r.kind == KIND_INVALID
    assert "recipient" in r.error


# ── Dispatcher fast path reaches SendMessageTool (real registry) ──


class _FakeProvider:
    """Provider that must NOT be called for the deterministic write path."""

    def __init__(self) -> None:
        self.calls = 0

    @property
    def name(self) -> str:
        return "fake"

    @property
    def capabilities(self):
        from backend.ai.providers.base.capabilities import ProviderCapabilities
        return ProviderCapabilities(supports_tools=True, supports_function_call=True)

    async def chat(self, messages, **kwargs):
        self.calls += 1
        from backend.ai.providers.base.contract import ProviderResponse
        return ProviderResponse(text="unexpected", provider_name="fake", success=True)

    def initialize(self) -> None:
        return None

    def shutdown(self) -> None:
        return None

    def count_tokens(self, text: str) -> int:
        return max(1, len(text) // 4)

    def health(self) -> dict[str, Any]:
        return {"healthy": True}
