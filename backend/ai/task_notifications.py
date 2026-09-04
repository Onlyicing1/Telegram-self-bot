"""Notification wiring for persisted occurrence outcomes."""
from __future__ import annotations

import logging

from backend.ai.database.task_repository import OccurrenceRecord, TaskRepository
from backend.ai.notifications import TaskNotification, TaskNotificationService

logger = logging.getLogger(__name__)


class TaskOutcomeNotifier:
    """Notify only after the repository confirms the requested outcome AND
    the task definition explicitly opted into outcome notifications.

    Scheduled execution is silent by default: internal diagnostics and the
    durable occurrence record carry the outcome. A Telegram notification is
    sent only when the task's ``notification_destination.notify_on_outcome``
    is ``true`` (set at creation time when the user asked to be notified),
    so Saved Messages can never become an execution-log sink.
    """

    def __init__(self, repository: TaskRepository, notifications: TaskNotificationService, owner_id: int) -> None:
        self.repository = repository
        self.notifications = notifications
        self.owner_id = owner_id

    async def notify_persisted(self, task_id: int, occurrence_key: str, expected_status: str) -> bool:
        occurrence = await self.repository.get_occurrence(self.owner_id, task_id, occurrence_key)
        if occurrence is None or occurrence.status != expected_status:
            return False
        task = await self.repository.get_task(self.owner_id, task_id)
        if task is None:
            return False
        # A deleted task's lifecycle is over; its occurrences never notify.
        if str(getattr(task, "status", "") or "") == "deleted":
            return False
        destination = task.notification_destination or {}
        if destination.get("notify_on_outcome") is not True:
            logger.debug(
                "TASK_OUTCOME_SILENT task_id=%s occurrence_key=%s status=%s",
                task_id, occurrence_key, expected_status,
            )
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
