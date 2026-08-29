from datetime import datetime, timezone

import pytest

from backend.ai.database.task_repository import InMemoryTaskRepository
from backend.ai.task_candidate import TaskCandidate, TaskCandidateError, parse_candidate_output
from backend.ai.task_creation import TaskCreationService


REF = datetime(2026, 1, 1, 12, 0, tzinfo=timezone.utc)


def raw():
    return {
        "label": "morning check",
        "schedule_type": "daily",
        "schedule": {"hour": 8, "minute": 30, "timezone": "UTC"},
        "timezone": "UTC",
        "actions": [{"name": "account_show", "arguments": {}}],
        "notification_destination": {"kind": "owner"},
    }


def test_candidate_is_explicit_and_does_not_contain_identity():
    candidate = parse_candidate_output(raw())
    assert isinstance(candidate, TaskCandidate)
    assert "owner_id" not in candidate.as_creation_candidate()


@pytest.mark.parametrize("mutator", [
    lambda x: x.pop("label"),
    lambda x: x.update(owner_id=99),
    lambda x: x.update(schedule_type="monthly"),
    lambda x: x.update(timezone="Not/AZone"),
    lambda x: x.update(actions=[]),
])
def test_malformed_candidate_rejected(mutator):
    value = raw()
    mutator(value)
    with pytest.raises(TaskCandidateError):
        parse_candidate_output(value)


@pytest.mark.asyncio
async def test_candidate_converts_deterministically_through_creation_service():
    repo = InMemoryTaskRepository()
    candidate = parse_candidate_output(raw())
    first = await TaskCreationService(repo, 42).create(candidate, REF)
    assert first.owner_id == 42
    assert first.next_run_at == datetime(2026, 1, 2, 8, 30, tzinfo=timezone.utc)
    assert await repo.get_task(99, first.id) is None
