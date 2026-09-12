"""Semantic-completeness boundary for natural-language task creation."""
from __future__ import annotations

import json
from unittest.mock import patch

import pytest

from backend.ai.database import manager as dbm
from backend.ai.task_creation import TaskCreationService, TaskSemanticCompletenessError
from backend.ai.task_candidate import TaskCandidate
from backend.ai.task_interpreter import TaskInterpreter
from backend.ai.tools.context import ToolContext
from backend.ai.tools.task import CreateTaskTool
from backend.ai.database.task_repository import InMemoryTaskRepository

OWNER = 777
REQUEST = "update my bio every 5 minutes"


def _candidate(*, action="bio_set_text", text="", instruction=None):
    value = {
        "label": "Bio update",
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "UTC",
        "actions": [{"name": action, "arguments": {"text": text}}],
        "notification_destination": {},
    }
    if instruction is not None:
        value["ai_instruction"] = instruction
    return value


class _Provider:
    def __init__(self, text: str):
        self.text = text
        self.calls = 0
        self.last_messages = None
        self.name = "fake"
        self.config = type("Config", (), {"default_model": "m"})()
        self.capabilities = type("Capabilities", (), {"supports_tools": True, "supports_function_call": True})()
        self.initialize = lambda: None
        self.shutdown = lambda: None

    def count_tokens(self, text):
        return max(1, len(text) // 4)

    def health(self):
        return {"healthy": True}

    async def chat(self, messages, **kwargs):
        from backend.ai.providers.base.contract import ProviderResponse
        self.calls += 1
        self.last_messages = messages
        return ProviderResponse(text=self.text, provider_name="fake", success=True)


def _manager(provider):

    from backend.ai.providers.manager.manager import ProviderManager

    manager = ProviderManager()
    manager.register_provider(provider)
    manager.switch_provider("fake")
    manager._fallback_chain = []
    return manager


def _context(provider_manager):
    return ToolContext(
        telegram=None,
        owner_id=OWNER,
        tz_str="UTC",
        client=None,
        extra={"provider_manager": provider_manager, "chat_id": -1001},
    )


@pytest.mark.asyncio
async def test_schema_valid_empty_profile_candidate_is_not_persisted():
    provider = _Provider(json.dumps(_candidate()))
    provider_manager = _manager(provider)
    repository_manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=repository_manager):
        result = await CreateTaskTool(_context(provider_manager)).execute(
            _context(provider_manager), {"request": REQUEST}
        )

    assert result.success is False
    assert result.data == {
        "open_taskloom_wizard": True,
        "wizard_reason": "candidate_semantically_incomplete",
    }
    assert await repository_manager.task.list_tasks(OWNER) == []


@pytest.mark.asyncio
async def test_fully_specified_generated_profile_candidate_creates_directly():
    provider = _Provider(json.dumps(_candidate(instruction=REQUEST)))
    provider_manager = _manager(provider)
    repository_manager = dbm.RepositoryManager(supabase_available=False)
    with patch.object(dbm, "get_repository_manager", return_value=repository_manager):
        result = await CreateTaskTool(_context(provider_manager)).execute(
            _context(provider_manager), {"request": REQUEST}
        )

    assert result.success is True
    tasks = await repository_manager.task.list_tasks(OWNER)
    assert len(tasks) == 1
    assert tasks[0].ai_instruction == REQUEST
    assert provider.calls == 1


@pytest.mark.asyncio
async def test_direct_creation_service_rejects_empty_profile_candidate_before_repository():
    repository = InMemoryTaskRepository()
    candidate = TaskCandidate.from_untrusted(_candidate())
    with pytest.raises(TaskSemanticCompletenessError):
        await TaskCreationService(repository, OWNER).create(candidate, __import__("datetime").datetime.now(__import__("datetime").timezone.utc))
    assert await repository.list_tasks(OWNER) == []


@pytest.mark.asyncio
async def test_interpreter_prompt_forbids_inventing_missing_requirements():
    from backend.ai.task_interpreter import CANDIDATE_SCHEMA

    provider = _Provider(json.dumps(_candidate(instruction=REQUEST)))
    await TaskInterpreter(_manager(provider)).interpret(REQUEST, timezone="UTC")
    system = " ".join(str(item["content"]) for item in provider.last_messages if item["role"] == "system")
    assert "Never invent missing schedule" in system
    assert "schedule expression alone does not authorize invented content" in system
    assert CANDIDATE_SCHEMA["required"]
