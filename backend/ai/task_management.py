"""Owner-scoped operational management for durable tasks."""
from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from backend.ai.database.task_repository import TaskRecord, TaskRepository


@dataclass(frozen=True)
class TaskView:
    task: TaskRecord
    occurrences: list[Any]


class TaskManagementService:
    def __init__(self, repository: TaskRepository, owner_id: int) -> None:
        self.repository = repository
        self.owner_id = owner_id

    async def list_tasks(self) -> list[TaskRecord]:
        return await self.repository.list_tasks(self.owner_id)

    async def inspect(self, task_id: int, occurrence_limit: int = 100) -> TaskView | None:
        task = await self.repository.get_task(self.owner_id, task_id)
        if task is None:
            return None
        occurrences = await self.repository.list_occurrences(self.owner_id, task_id, occurrence_limit)
        return TaskView(task, occurrences)

    async def set_status(self, task_id: int, status: str, expected_version: int) -> TaskRecord | None:
        return await self.repository.transition_task(
            self.owner_id, task_id, status, expected_version=expected_version
        )

    async def pause(self, task_id: int, expected_version: int) -> TaskRecord | None:
        return await self.set_status(task_id, "paused", expected_version)

    async def resume(self, task_id: int, expected_version: int) -> TaskRecord | None:
        return await self.set_status(task_id, "active", expected_version)

    async def complete(self, task_id: int, expected_version: int) -> TaskRecord | None:
        return await self.set_status(task_id, "completed", expected_version)

    async def fail(self, task_id: int, expected_version: int) -> TaskRecord | None:
        return await self.set_status(task_id, "failed", expected_version)

    async def expire(self, task_id: int, expected_version: int) -> TaskRecord | None:
        return await self.set_status(task_id, "expired", expected_version)

    async def delete(self, task_id: int, expected_version: int) -> TaskRecord | None:
        return await self.set_status(task_id, "deleted", expected_version)
