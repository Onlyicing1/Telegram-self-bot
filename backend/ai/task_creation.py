"""Deterministic task creation boundary for authorized callers."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.ai.database.task_repository import (
    MAX_STEPS_PER_ADD,
    MAX_STEPS_PER_TODO,
    TaskRecord,
    TaskRepository,
    TODO_SCHEDULE_TYPE,
    normalize_step_title,
)
from backend.ai.preparation_policy import CONTENT_FIELDS
from backend.ai.scheduling import (
    ScheduleError,
    advance_interval,
    next_occurrence,
    parse_schedule,
)
from backend.ai.task_candidate import TaskCandidate
from backend.ai.task_contract import (
    AIInstruction,
    action_reference_error,
    is_action_reference,
    validate_ai_instruction,
)
from backend.ai.task_trace import bound_text, task_trace
from backend.ai.tools.base import (
    declared_any_arguments,
    declared_required_arguments,
    requires_owner_confirmation,
    requires_reply_context,
)

logger = logging.getLogger(__name__)


def _creation_trace(stage: str, **fields: Any) -> None:
    # Correlated AI_TASK_TRACE record when a create_task request is bound;
    # silent for direct service callers (tests, .task command without a trace).
    task_trace(stage, **fields)


class TaskCreationError(ValueError):
    """Candidate task data is invalid or cannot be scheduled."""


class TaskSemanticCompletenessError(TaskCreationError):
    """A schema-valid candidate lacks grounded user task semantics."""


_PROFILE_CONTENT_ACTIONS = frozenset({"bio_set_text", "username_set_text"})

# The Todo title bound: the SAME 256-character bound the repository enforces
# for every task label (``_validate_task_input``). Declared here so the todo
# creation boundary refuses an over-long title with an honest message instead
# of relying on the storage layer's error.
MAX_TODO_TITLE_CHARS = 256


def _attached_tool_registry() -> Any | None:
    """The ToolRegistry the runtime has attached, or ``None``.

    Resolved through the ALREADY-constructed Engine (never constructing one):
    a service-only caller has no authoritative registry to consult, and the
    occurrence-time check in ``TaskExecutionCoordinator`` remains the
    defense-in-depth backstop there.
    """
    try:
        from backend.ai.engine.engine import active_engine

        engine = active_engine()
    except Exception:  # noqa: BLE001
        return None
    if engine is None:
        return None
    return getattr(engine, "tool_registry", None)


def _argument_missing(value: Any) -> bool:
    """True when a value cannot satisfy a required action argument."""
    if value is None:
        return True
    if isinstance(value, str):
        return not value.strip()
    if isinstance(value, (list, tuple, set, frozenset, dict)):
        return not value
    return False


def _declared_constraint_error(
    action_name: str, argument: str, value: Any, spec: dict[str, Any]
) -> str | None:
    """Enforce the Tool's OWN declared enum/minimum for a required argument."""
    enum = spec.get("enum")
    if isinstance(enum, (list, tuple)) and enum:
        if isinstance(value, str):
            candidate = value.strip().lower()
            allowed: set[Any] = {str(item).strip().lower() for item in enum}
        else:
            candidate = value
            allowed = set(enum)
        if candidate not in allowed:
            return f"action '{action_name}' has an unsupported '{argument}' value"
    if spec.get("type") == "integer":
        # The Tool itself declares the integer contract and coerces with
        # ``coerce_int``; a value it cannot read is missing for its purposes.
        from backend.ai.persian import coerce_int

        number = coerce_int(value)
        if number is None:
            return f"action '{action_name}' requires an integer '{argument}'"
        minimum = spec.get("minimum")
        if isinstance(minimum, (int, float)) and not isinstance(minimum, bool) and number < minimum:
            return f"action '{action_name}' requires '{argument}' >= {minimum}"
    return None


def _action_eligibility_error(
    action: Any, registry: Any | None, *, generation_authorized: bool
) -> str | None:
    """Reject an action a scheduled occurrence could never execute.

    A durable task's actions run later through the registered ToolExecutor
    with no owner present, so each one must be registered, runnable without
    an owner confirmation round-trip or an immediate replied message, and
    carry the arguments its ``Tool.execute()`` contract requires. A content
    argument may be absent only when the task's ``ai_instruction`` authorizes
    per-occurrence generation (the preparation path supplies and validates it
    at execution time).

    The checks read the Tool's OWN declarations (``parameters``,
    ``required_arguments``, ``required_any_arguments``,
    ``requires_reply_context``, ``permission_level``) so there is no second
    action matrix to keep in sync.
    """
    if not isinstance(action, dict):
        return "each action must be an object"
    name = action.get("name")
    if not isinstance(name, str) or not name.strip():
        return "each action requires a tool name"
    arguments = action.get("arguments")
    if arguments is None:
        arguments = {}
    if not isinstance(arguments, dict):
        return "action arguments must be objects"
    if registry is None or not hasattr(registry, "get"):
        # No authoritative registry in this process: registration cannot be
        # judged here, and the coordinator's own registry check still fails
        # the occurrence closed. Never invent a registry to fail against.
        return None
    tool = registry.get(name)
    if tool is None:
        return f"action '{name}' is not a registered tool"
    if requires_owner_confirmation(tool):
        return (
            f"action '{name}' requires owner confirmation that a scheduled "
            "occurrence cannot provide"
        )
    if requires_reply_context(tool):
        return (
            f"action '{name}' requires the immediate replied message that a "
            "scheduled occurrence does not have"
        )
    required = declared_required_arguments(tool)
    required_any = declared_any_arguments(tool)
    if required_any and all(_argument_missing(arguments.get(item)) for item in required_any):
        return f"action '{name}' requires one of: {', '.join(required_any)}"
    schema = getattr(tool, "parameters", None) or {}
    checked = list(required)
    checked.extend(item for item in required_any if item not in set(required))
    for argument in checked:
        value = arguments.get(argument)
        if _argument_missing(value):
            if argument not in set(required):
                continue  # an unchosen alternative of a required-any group
            if generation_authorized and argument in CONTENT_FIELDS:
                continue
            return f"action '{name}' requires the '{argument}' argument"
        if is_action_reference(value):
            # A reference is resolved at execution from a PREVIOUS action's
            # recorded result, so the tool's declared enum/minimum applies to
            # the resolved value then — never to the placeholder. The
            # reference shape itself is validated by ``action_reference_error``
            # over the whole action list right after this loop.
            continue
        spec = schema.get(argument) if isinstance(schema, dict) else None
        if isinstance(spec, dict):
            error = _declared_constraint_error(name, argument, value, spec)
            if error:
                return error
    return None


def _semantic_completeness_error(candidate: dict[str, Any]) -> str | None:
    """Reject empty profile content when no generation contract exists.

    The candidate schema proves shape only. Profile tools deliberately accept
    empty text for generated-per-occurrence content, but that mode is only
    complete when an ``ai_instruction`` is present. A nonblank static profile
    value remains valid through the existing candidate/action contract.
    """
    actions = candidate.get("actions")
    if not isinstance(actions, list):
        return None
    instruction = candidate.get("ai_instruction")
    if instruction is not None and (not isinstance(instruction, str) or not instruction.strip()):
        return "AI instruction is invalid"
    for action in actions:
        if not isinstance(action, dict) or action.get("name") not in _PROFILE_CONTENT_ACTIONS:
            continue
        arguments = action.get("arguments")
        if not isinstance(arguments, dict):
            return "content action arguments are invalid"
        if not (isinstance(instruction, str) and instruction.strip()) and not str(arguments.get("text") or "").strip():
            return "profile content requires explicit content or an AI instruction"
    return None


def initial_next_run(
    schedule_type: str, schedule_payload: dict[str, Any], reference: datetime
) -> datetime | None:
    """Resolve the first boundary of a schedule (ONE implementation).

    Shared by task creation and the task-definition edit path so an edit can
    never compute a different next run than a create for the same schedule.
    Event schedules have no wall-clock time (``None``); a brand-new interval
    schedule has no previous occurrence, so its first run is one interval
    after the reference and the scheduler anchors later occurrences itself.
    """
    parsed = parse_schedule(schedule_type, schedule_payload)
    if schedule_type == "interval":
        interval = getattr(parsed, "interval", None)
        if not isinstance(interval, timedelta) or interval <= timedelta(0):
            raise ScheduleError("interval must be positive")
        return advance_interval(reference, interval, reference)
    return next_occurrence(parsed, reference)


class TaskCreationService:
    def __init__(
        self,
        repository: TaskRepository,
        owner_id: int,
        tool_registry: Any | None = None,
    ) -> None:
        if not isinstance(owner_id, int) or owner_id <= 0:
            raise TaskCreationError("owner identity is required")
        self.repository = repository
        self.owner_id = owner_id
        #: The authoritative action registry. Left unset, the service resolves
        #: the runtime-attached registry; a caller with no runtime registry
        #: (unit/service harness) may inject the real one explicitly.
        self._tool_registry = tool_registry

    async def create_todo(
        self,
        label: str,
        timezone: str,
        reference: datetime,
        steps: list[Any] | tuple[Any, ...] = (),
    ) -> TaskRecord:
        """Create ONE basic Todo through the SAME creation/persistence path.

        A todo is the ONE unscheduled row this table stores: a title, the
        owner's timezone, and nothing the scheduler could ever run (no
        schedule payload, no action, no destination, no next run). It is
        persisted by ``create`` -> ``TaskRepository.create_task`` exactly like
        a scheduled task, so there is no second creation path, no second
        candidate shape and no second persistence call.

        ``steps`` creates the todo's ordered step list in the SAME operation:
        the steps are appended in the order given, in one repository call, and
        a failure removes the todo again instead of leaving a half-created
        structure (there is no generalized transaction framework here — the
        compensating delete is the existing CAS-guarded row removal).

        The title is REQUIRED and never invented: a blank title is rejected
        here instead of being filled with a placeholder. The same holds for
        every step title.
        """
        text = " ".join(str(label or "").split())
        if not text or len(text) > MAX_TODO_TITLE_CHARS:
            raise TaskCreationError(
                "a todo needs a title of at most "
                f"{MAX_TODO_TITLE_CHARS} characters"
            )
        try:
            cleaned = [normalize_step_title(step) for step in steps]
        except ValueError as exc:
            raise TaskCreationError(str(exc)) from exc
        if len(cleaned) > MAX_STEPS_PER_ADD:
            raise TaskCreationError(
                f"at most {MAX_STEPS_PER_ADD} steps can be created at once"
            )
        if len(cleaned) > MAX_STEPS_PER_TODO:
            raise TaskCreationError(
                f"a todo holds at most {MAX_STEPS_PER_TODO} steps"
            )
        zone = str(timezone or "").strip() or "UTC"
        candidate = {
            "label": text,
            "schedule_type": TODO_SCHEDULE_TYPE,
            "schedule": {},
            "timezone": zone,
            "actions": [],
            "notification_destination": {},
            "next_run_at": None,
        }
        task = await self.create(candidate, reference)
        if not cleaned:
            return task
        try:
            await self.repository.create_steps(self.owner_id, task.id, cleaned)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            # ALL-OR-NOTHING: the todo and its steps are ONE structure. The
            # todo row is removed again under its own CAS version rather than
            # leaving a todo without the steps the owner asked for.
            try:
                await self.repository.delete_task(
                    self.owner_id, task.id, task.version
                )
            except asyncio.CancelledError:
                raise
            except Exception:  # noqa: BLE001
                logger.warning(
                    "TODO_STEPS_ROLLBACK_FAILED todo_id=%s — the todo may exist "
                    "without its steps",
                    task.id,
                )
            raise TaskCreationError(
                "the todo was not created: its steps could not be saved"
            ) from exc
        return task

    async def create(self, candidate: dict[str, Any], reference: datetime) -> TaskRecord:
        started = time.perf_counter()

        def _invalid(reason: str) -> TaskCreationError:
            _creation_trace(
                "rejected", reason=reason,
                elapsed_s=round(time.perf_counter() - started, 2),
            )
            return TaskCreationError(reason)

        _creation_trace(
            "task_validation_start", repo_type=type(self.repository).__name__,
            candidate_fields=len(candidate) if isinstance(candidate, dict) else 0,
        )
        if isinstance(candidate, TaskCandidate):
            candidate = candidate.as_creation_candidate()
        if not isinstance(candidate, dict):
            raise _invalid("task candidate must be an object")
        if not isinstance(reference, datetime) or reference.tzinfo is None:
            raise TaskCreationError("reference datetime must be timezone-aware")
        required = {"label", "schedule_type", "schedule", "timezone", "actions", "notification_destination"}
        allowed = required | {"next_run_at", "ai_instruction"}
        if set(candidate) - allowed:
            raise _invalid(f"unsupported task fields: {sorted(set(candidate) - allowed)}")
        if required - set(candidate):
            raise _invalid(f"missing required task fields: {sorted(required - set(candidate))}")
        semantic_error = _semantic_completeness_error(candidate)
        if semantic_error:
            _creation_trace("semantic_incomplete", reason=semantic_error)
            raise TaskSemanticCompletenessError(semantic_error)
        actions = candidate.get("actions")
        if isinstance(actions, list) and actions:
            registry = (
                self._tool_registry
                if self._tool_registry is not None
                else _attached_tool_registry()
            )
            instruction = candidate.get("ai_instruction")
            generation_authorized = isinstance(instruction, str) and bool(instruction.strip())
            for action in actions:
                eligibility_error = _action_eligibility_error(
                    action, registry, generation_authorized=generation_authorized
                )
                if eligibility_error:
                    _creation_trace("action_ineligible", reason=eligibility_error)
                    raise _invalid(eligibility_error)
            reference_error = action_reference_error(
                actions, registry, generation_authorized=generation_authorized
            )
            if reference_error:
                _creation_trace("action_ineligible", reason=reference_error)
                raise _invalid(reference_error)
        if (
            candidate.get("timezone") != candidate["schedule"].get("timezone")
            and candidate["schedule_type"] not in ("interval", "event", TODO_SCHEDULE_TYPE)
        ):
            raise _invalid(
                "task and schedule timezones must match "
                f"(task={candidate.get('timezone')} schedule={candidate['schedule'].get('timezone')})"
            )
        try:
            initial = candidate.get("next_run_at")
            if candidate["schedule_type"] == TODO_SCHEDULE_TYPE:
                # A todo is UNSCHEDULED: there is no boundary to resolve and
                # none may be fabricated (its ``schedule`` is empty on
                # purpose). ``next_run_at`` stays None, and the repository
                # rejects a todo that ever carries one, so the scheduler
                # cannot run it.
                initial = None
            elif candidate["schedule_type"] == "event":
                # Event-triggered tasks have no wall-clock time: next_run_at
                # stays None (the event handler drives executions) and the
                # UI reports the trigger, never a fake run time.
                parse_schedule("event", candidate["schedule"])
                initial = None
            elif initial is None:
                initial = initial_next_run(
                    candidate["schedule_type"], candidate["schedule"], reference
                )
        except (ScheduleError, TypeError, ValueError) as exc:
            _creation_trace("schedule_invalid", schedule_type=str(candidate.get("schedule_type")), error=str(exc)[:120])
            raise TaskCreationError(str(exc)) from exc
        payload = {key: candidate[key] for key in required}
        if candidate.get("ai_instruction") is not None:
            instruction = candidate["ai_instruction"]
            if isinstance(instruction, dict):
                if instruction.get("kind") != "ai_instruction":
                    raise TaskCreationError("AI instruction kind is invalid")
                instruction = instruction.get("text")
            payload["ai_instruction"] = validate_ai_instruction(instruction)
        logger.info(
            "TASK_CREATE_PERSIST_ATTEMPT repository=%s owner_id=%s has_ai_instruction=%s",
            type(self.repository).__name__, self.owner_id, bool(payload.get("ai_instruction")),
        )
        payload["next_run_at"] = initial.astimezone(timezone.utc) if isinstance(initial, datetime) and initial.tzinfo else initial
        _creation_trace(
            "task_validation_result", success="true", schema_version="1",
            schedule_type=str(candidate["schedule_type"]),
            actions=len(candidate["actions"]),
            payload_bytes=len(json.dumps(payload, ensure_ascii=False, default=str)),
            next_run_at=(payload["next_run_at"].isoformat() if isinstance(payload["next_run_at"], datetime) else "none"),
        )
        _creation_trace(
            "repository_call", repo_type=type(self.repository).__name__,
            schedule_type=str(candidate["schedule_type"]),
            payload_bytes=len(json.dumps(payload, ensure_ascii=False, default=str)),
        )
        try:
            task = await self.repository.create_task(self.owner_id, payload)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            _creation_trace(
                "repository_error", repo_type=type(self.repository).__name__,
                error_type=type(exc).__name__, error=str(exc)[:200],
            )
            raise
        _creation_trace(
            "persisted", repo_type=type(self.repository).__name__,
            task_id=int(task.id), version=int(task.version),
            next_run_at=(task.next_run_at.isoformat() if task.next_run_at else "none"),
            elapsed_s=round(time.perf_counter() - started, 2),
        )
        return task
