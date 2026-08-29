"""Notification wiring for persisted occurrence outcomes."""
from __future__ import annotations

from typing import Any

from backend.ai.database.task_repository import OccurrenceRecord, TaskRepository
from backend.ai.notifications import TaskNotification, TaskNotificationService


class TaskOutcomeNotifier:
    """Notify only after the repository confirms the requested outcome."""

    def __init__(self, repository: TaskRepository, notifications: TaskNotificationService, owner_id: int) -> None:
        self.repository = repository
        self.notifications = notifications
        self.owner_id = owner_id

    async def notify_persisted(self, task_id: int, occurrence_key: str, expected_status: str) -> bool:
        occurrence = await self.repository.get_occurrence(self.owner_id, task_id, occurrence_key)
        if occurrence is None or occurrence.status != expected_status:
            return False
        message = self._message(occurrence)
        return await self.notifications.send(TaskNotification(
            owner_id=self.owner_id,
            task_id=task_id,
            occurrence_key=occurrence_key,
            kind=expected_status,
            message=message,
        ))

    @staticmethod
    def _message(occurrence: OccurrenceRecord) -> str:
        if occurrence.status == "succeeded":
            return f"Task occurrence {occurrence.occurrence_key} succeeded."
        if occurrence.status == "retry_pending":
            return f"Task occurrence {occurrence.occurrence_key} will retry at {occurrence.retry_at.isoformat() if occurrence.retry_at else 'a later time'}."
        if occurrence.status == "cancelled":
            return f"Task occurrence {occurrence.occurrence_key} was cancelled."
        error = str((occurrence.error_metadata or {}).get("error_class", "execution failed"))[:200]
        return f"Task occurrence {occurrence.occurrence_key} failed: {error}."
