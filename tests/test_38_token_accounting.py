"""
TASK 38 — Token-accounting lifecycle for the two dispatcher retry paths.

Regression coverage for the confirmed divergences:

  1. The empty-response retry discards the first (empty) provider response
     without retaining its reported usage.
  2. The action-recovery retry discards the original prose response without
     retaining its reported usage.

Every provider attempt that occurs must contribute its available usage exactly
once — nothing lost, nothing double-counted — while normal single-attempt
accounting and the unavailable-usage semantics stay unchanged.
"""
from __future__ import annotations

import pytest

from backend.ai.engine.engine import Engine
from backend.ai.engine.telemetry import telemetry
from backend.ai.providers.base import ProviderResponse
from backend.ai.providers.manager.manager import ProviderManager


@pytest.fixture(autouse=True)
def _reset_telemetry():
    telemetry.reset_for_tests()
    yield
    telemetry.reset_for_tests()


class _SequenceProvider(ProviderManager):
    """Returns pre-scripted responses in order, repeating the last one."""

    def __init__(self, responses: list[ProviderResponse]) -> None:
        super().__init__()
        self._responses = list(responses)
        self.calls = 0

    def get_active_name(self) -> str:
        return "dummy"

    async def chat(self, messages: list, **kwargs):
        index = min(self.calls, len(self._responses) - 1)
        self.calls += 1
        return self._responses[index]


def _request(owner_id: int = 1):
    from backend.ai.session.request import AIRequest

    return AIRequest(
        session_id="token-accounting-test",
        user_message="hello",
        owner_id=owner_id,
        chat_id=1,
        message_id=1,
    )


def _response(
    text: str,
    usage: dict | None = None,
    success: bool = True,
    finish_reason: str = "",
    tool_calls: list | None = None,
) -> ProviderResponse:
    metadata: dict = {}
    if finish_reason:
        metadata["finish_reason"] = finish_reason
    return ProviderResponse(
        text=text,
        provider_name="dummy",
        success=success,
        usage=usage or {},
        metadata=metadata,
        tool_calls=tool_calls,
    )


@pytest.mark.asyncio
async def test_empty_response_retry_preserves_discarded_usage():
    """The superseded empty attempt's usage is retained in the final total."""
    provider = _SequenceProvider([
        _response("", usage={"prompt_tokens": 100, "completion_tokens": 0,
                             "total_tokens": 100}, finish_reason="stop"),
        _response("ok", usage={"prompt_tokens": 50, "completion_tokens": 20,
                               "total_tokens": 70}),
    ])
    result = await Engine(providers=provider).execute(_request())

    assert result.success is True
    # 100 (discarded empty attempt) + 50 (final attempt) — exactly once each.
    assert result.prompt_tokens == 150
    assert result.completion_tokens == 20
    assert result.total_tokens == 170
    assert result.metadata["token_source"] == "actual"
    assert provider.calls >= 2  # the empty-response retry actually ran


@pytest.mark.asyncio
async def test_action_recovery_retry_preserves_discarded_usage():
    """The superseded prose response's usage is retained when recovery returns a tool call."""
    provider = _SequenceProvider([
        _response("some prose", usage={"prompt_tokens": 10, "completion_tokens": 5,
                                       "total_tokens": 15}),
        _response(
            "",
            usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
            tool_calls=[{
                "id": "1", "type": "function",
                "function": {"name": "save", "arguments": {}},
            }],
        ),
    ])
    result = await Engine(providers=provider).execute(_request())

    assert result.success is True
    # 10 (discarded prose) + 20 (recovery attempt) — exactly once each.
    assert result.prompt_tokens == 30
    assert result.completion_tokens == 13
    assert result.total_tokens == 43
    assert result.metadata["token_source"] == "actual"


@pytest.mark.asyncio
async def test_action_recovery_candidate_preserves_discarded_usage():
    """The recovery-candidate path (JSON action parsed from recovery text) retains usage."""
    provider = _SequenceProvider([
        _response("some prose", usage={"prompt_tokens": 10, "completion_tokens": 5,
                                       "total_tokens": 15}),
        _response(
            '{"action": "save", "target": "replied_message"}',
            usage={"prompt_tokens": 20, "completion_tokens": 8, "total_tokens": 28},
        ),
    ])
    result = await Engine(providers=provider).execute(_request())

    assert result.success is True
    assert result.prompt_tokens == 30
    assert result.completion_tokens == 13
    assert result.total_tokens == 43
    assert result.metadata["token_source"] == "actual"


@pytest.mark.asyncio
async def test_normal_single_attempt_accounting_unchanged():
    """A request with no retry keeps its exact single-attempt accounting."""
    provider = _SequenceProvider([
        _response("ok", usage={"prompt_tokens": 5, "completion_tokens": 3,
                               "total_tokens": 8}),
    ])
    result = await Engine(providers=provider).execute(_request())

    assert result.success is True
    assert result.prompt_tokens == 5
    assert result.completion_tokens == 3
    assert result.total_tokens == 8
    assert result.metadata["token_source"] == "actual"


@pytest.mark.asyncio
async def test_unavailable_usage_remains_unavailable():
    """A failed request without usage never fabricates token counts."""
    provider = _SequenceProvider([
        _response("", success=False, usage={},
                  finish_reason=""),
    ])
    result = await Engine(providers=provider).execute(_request())

    assert result.success is False
    assert result.prompt_tokens == 0
    assert result.completion_tokens == 0
    assert result.total_tokens == 0
    assert result.metadata["token_source"] == "unavailable"

    record = telemetry.last()
    assert record is not None
    assert record.token_source == "unavailable"
    assert record.input_tokens == 0
    assert record.output_tokens == 0


@pytest.mark.asyncio
async def test_empty_response_retry_no_double_counting():
    """The final response is never counted twice across retry + recovery."""
    provider = _SequenceProvider([
        _response("", usage={"prompt_tokens": 100, "completion_tokens": 0,
                             "total_tokens": 100}, finish_reason="stop"),
        _response("ok", usage={"prompt_tokens": 50, "completion_tokens": 20,
                               "total_tokens": 70}),
    ])
    result = await Engine(providers=provider).execute(_request())

    # If either response were double-counted the total would exceed 170.
    assert result.total_tokens == 170
    record = telemetry.last()
    assert record is not None
    assert record.total_tokens == 170
    assert record.input_tokens == 150
    assert record.output_tokens == 20
