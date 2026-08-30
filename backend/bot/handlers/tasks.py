"""Owner-scoped natural-language task creation command."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.ai.task_creation import TaskCreationError, TaskCreationService
from backend.ai.task_interpreter import TaskInterpretationError, TaskInterpreter
from backend.ai.database.manager import get_repository_manager

logger = logging.getLogger(__name__)
_COMMAND = ".task"
_TIMEOUT = 45.0


def _feedback(text: str, status: str) -> str:
    return f"{text}\n────────────\n🗓 Task\n{status}"


def _provider_manager():
    from backend.ai.engine.engine import get_engine
    engine = get_engine()
    if engine is None:
        raise TaskInterpretationError("AI engine is unavailable")
    return engine.provider_manager


def register(client, owner_id: int, tz_str: str):
    @client.on(events.NewMessage(outgoing=True))
    async def task_creation_handler(event):
        if not is_owner(event, owner_id):
            return
        raw = (event.raw_text or "").strip()
        if not raw.startswith(_COMMAND):
            return
        request = raw[len(_COMMAND):].strip()
        if not request:
            await event.edit(_feedback(raw, "❌ Describe the task after `.task`."))
            return
        try:
            await event.edit(_feedback(raw, "⏳ Interpreting..."))
            candidate = await asyncio.wait_for(
                TaskInterpreter(_provider_manager()).interpret(request),
                timeout=_TIMEOUT,
            )
            service = TaskCreationService(
                get_repository_manager().task,
                owner_id,
            )
            task = await service.create(candidate, datetime.now(timezone.utc))
            await event.edit(_feedback(raw, f"✅ Created `{task.id}` — {task.label}"))
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError:
            await event.edit(_feedback(raw, "❌ Task interpretation timed out."))
        except (TaskInterpretationError, TaskCreationError) as exc:
            logger.info("Task creation rejected: %s", type(exc).__name__)
            await event.edit(_feedback(raw, "❌ Task was not created: invalid or ambiguous request."))
        except Exception:
            logger.exception("Task creation handler failed")
            await event.edit(_feedback(raw, "❌ Task was not created because persistence failed."))
