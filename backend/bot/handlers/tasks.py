"""Owner-scoped durable task commands."""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone
import logging

from telethon import events

from backend.bot.handlers.guard import is_owner
from backend.ai.task_creation import TaskCreationError, TaskCreationService
from backend.ai.task_interpreter import TaskInterpretationError, TaskInterpreter
from backend.ai.task_management import TaskManagementService
from backend.ai.task_management_interface import inspect_text, list_text
from backend.ai.database.manager import get_repository_manager

logger = logging.getLogger(__name__)
_COMMAND = ".task"
_TIMEOUT = 45.0
_MANAGEMENT_COMMANDS = {"list", "inspect", "pause", "resume", "complete", "fail", "expire", "delete"}


def _feedback(text: str, status: str) -> str:
    return f"{text}\n────────────\n🗓 Task\n{status}"


def _provider_manager():
    from backend.ai.engine.engine import get_engine
    engine = get_engine()
    if engine is None:
        raise TaskInterpretationError("AI engine is unavailable")
    return engine.provider_manager


def _management(owner_id: int) -> TaskManagementService:
    return TaskManagementService(get_repository_manager().task, owner_id)


def _parse_task_id(value: str) -> int:
    task_id = int(value)
    if task_id <= 0:
        raise ValueError("task id must be positive")
    return task_id


def _parse_version(value: str) -> int:
    version = int(value)
    if version <= 0:
        raise ValueError("version must be positive")
    return version


def _management_help() -> str:
    return "Usage: `.task list` · `.task inspect <id>` · `.task <pause|resume|complete|fail|expire|delete> <id> <version>`"


async def _handle_management(event, owner_id: int, args: list[str], raw: str) -> bool:
    if not args or args[0].lower() not in _MANAGEMENT_COMMANDS:
        return False
    command = args[0].lower()
    service = _management(owner_id)
    try:
        if command == "list":
            if len(args) != 1:
                raise ValueError("list takes no arguments")
            result = await list_text(service)
        elif command == "inspect":
            if len(args) != 2:
                raise ValueError("inspect requires a task id")
            result = await inspect_text(service, _parse_task_id(args[1]))
        else:
            if len(args) != 3:
                raise ValueError("mutation requires task id and version")
            task_id = _parse_task_id(args[1])
            version = _parse_version(args[2])
            task = await getattr(service, command)(task_id, version)
            result = (
                f"✅ {command}d task `{task.id}` · version {task.version}."
                if task is not None
                else "❌ Task not found, ownership check failed, or version is stale."
            )
        await event.edit(_feedback(raw, result))
    except (ValueError, TypeError):
        await event.edit(_feedback(raw, f"❌ Invalid task command. {_management_help()}"))
    except asyncio.CancelledError:
        raise
    except Exception:
        logger.exception("Task management handler failed")
        await event.edit(_feedback(raw, "❌ Task operation failed; no change was confirmed."))
    return True


def register(client, owner_id: int, tz_str: str):
    @client.on(events.NewMessage(outgoing=True))
    async def task_handler(event):
        if not is_owner(event, owner_id):
            return
        raw = (event.raw_text or "").strip()
        if not raw.startswith(_COMMAND):
            return
        args = raw[len(_COMMAND):].strip().split()
        if await _handle_management(event, owner_id, args, raw):
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
            service = TaskCreationService(get_repository_manager().task, owner_id)
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
