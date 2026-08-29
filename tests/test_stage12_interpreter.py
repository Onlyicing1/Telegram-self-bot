import json
from unittest.mock import AsyncMock

import pytest

from backend.ai.providers.base.contract import ProviderResponse
from backend.ai.task_candidate import TaskCandidate
from backend.ai.task_interpreter import TaskInterpretationError, TaskInterpreter


def candidate():
    return {
        "label": "morning check", "schedule_type": "daily",
        "schedule": {"hour": 8, "timezone": "UTC"}, "timezone": "UTC",
        "actions": [{"name": "account_show", "arguments": {}}],
        "notification_destination": {"kind": "owner"},
    }


@pytest.mark.asyncio
async def test_provider_json_becomes_validated_candidate_only():
    manager = AsyncMock()
    manager.chat.return_value = ProviderResponse(text=json.dumps(candidate()), provider_name="dummy", success=True)
    result = await TaskInterpreter(manager).interpret("remind me every morning")
    assert isinstance(result, TaskCandidate)
    assert "owner_id" not in result.as_creation_candidate()
    manager.chat.assert_awaited_once()
    assert manager.chat.call_args.kwargs["tools"] == []


@pytest.mark.parametrize("text", ["not json", "null", json.dumps({**candidate(), "owner_id": 99}), json.dumps({k: v for k, v in candidate().items() if k != "actions"})])
@pytest.mark.asyncio
async def test_malformed_or_ambiguous_provider_output_is_rejected(text):
    manager = AsyncMock()
    manager.chat.return_value = ProviderResponse(text=text, provider_name="dummy", success=True)
    with pytest.raises(TaskInterpretationError):
        await TaskInterpreter(manager).interpret("set a task")


@pytest.mark.asyncio
async def test_provider_failure_is_not_persisted_or_executed():
    manager = AsyncMock()
    manager.chat.return_value = ProviderResponse(text="failure", provider_name="dummy", success=False)
    with pytest.raises(TaskInterpretationError):
        await TaskInterpreter(manager).interpret("do something later")
    manager.create_task.assert_not_called()
