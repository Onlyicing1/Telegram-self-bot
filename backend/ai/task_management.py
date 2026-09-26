"""Owner-scoped operational management for durable tasks."""
from __future__ import annotations

import asyncio
import logging
import re
import unicodedata
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

from backend.ai.database.task_repository import (
    TASK_STATUSES,
    TaskDeletionResult,
    TaskRecord,
    TaskRepository,
    is_todo_schedule_type,
)
from backend.ai.scheduling import ScheduleError, advance_interval, next_occurrence, parse_schedule
from backend.ai.task_creation import MAX_TODO_TITLE_CHARS, initial_next_run

logger = logging.getLogger(__name__)

# next_run_at must never advertise an execution for these states: the
# scheduler only runs active tasks, so a paused/completed/deleted/... task
# with a future next_run_at would lie in the UI. Resume recomputes the next
# occurrence from the stored schedule.
_NEXT_RUN_CLEAR_STATUSES = frozenset({"paused", "completed", "failed", "expired", "deleted"})

# ── The ONE deterministic Todo title resolver ────────────────────────────────
#
# A Todo is addressed EITHER by its id or by the owner's own words. The
# resolver turns those words into 0, 1 or N owner-scoped candidates and never
# into an action: an ambiguous reference is answered with the candidate list
# (never a guess), so no mutation can ever reach a todo the owner did not
# uniquely name. It is deterministic — no scoring, no ranking, no model, no
# embeddings — and the first tier that matches anything decides the outcome:
#   1. the whole query is an id ("12", "۱۲") -> that ONE todo, by identity
#   2. every query token is a substring of the todo's normalized title
#   3. the todo's whole normalized title occurs inside the query — the owner's
#      sentence names the todo within it (a title shorter than
#      ``_MIN_TITLE_IN_QUERY_CHARS`` is ignored here: a one- or two-character
#      title would otherwise match almost any sentence)
TODO_RESOLUTION_NOT_FOUND = "not_found"
TODO_RESOLUTION_UNIQUE = "unique"
TODO_RESOLUTION_AMBIGUOUS = "ambiguous"

# Outcomes of resolving ONE todo for a mutation; only ``ok`` carries a task.
TODO_TARGET_OK = "ok"
TODO_TARGET_NOT_FOUND = "not_found"
TODO_TARGET_AMBIGUOUS = "ambiguous"
TODO_TARGET_INVALID = "invalid"

# The candidate bound every surface shares (the AI tool's rendering and the
# panel's clarification rows), mirroring the saved-item resolver's cap.
MAX_TODO_CANDIDATES = 8
# A title reference is a short phrase, never a document.
MAX_TODO_QUERY_CHARS = 128
MAX_TODO_QUERY_TOKENS = 12
_MIN_TITLE_IN_QUERY_CHARS = 3


def _normalize_todo_text(value: Any) -> str:
    """Deterministic matching form for a title or a reference (never stored).

    NFKC plus the project's established Persian/Arabic normalization
    (``semantic_delete.normalize_text``: digit folding, script-variant folding
    e.g. ي→ی and ك→ک, diacritic and zero-width removal, casefold, whitespace
    collapse). Stored titles are never rewritten.
    """
    from backend.ai.semantic_delete import normalize_text

    text = unicodedata.normalize("NFKC", str(value or ""))
    return normalize_text(text)


def normalize_todo_query(value: Any) -> str:
    """The public matching form of a title reference (comparison only)."""
    return _normalize_todo_text(value)


@dataclass(frozen=True)
class TodoCandidate:
    """One todo the owner can act on: identity, title, status, CAS version."""

    task_id: int
    label: str
    status: str
    version: int
    created_at: datetime | None = None


@dataclass(frozen=True)
class TodoResolution:
    """The resolver's complete answer: 0, 1 or N candidate todos.

    ``status`` is one of ``not_found`` / ``unique`` / ``ambiguous``.
    ``overflowed`` means more matches exist than the bounded list shows, so
    the shown candidates are explicitly not the only matches.
    """

    status: str
    query: str
    candidates: tuple[TodoCandidate, ...] = ()
    overflowed: bool = False


@dataclass(frozen=True)
class TodoTarget:
    """The resolver's answer for a mutation: one todo, or why there is none.

    Only ``ok`` carries a ``task`` (the freshly read record, so the caller's
    CAS update uses the CURRENT version). ``message`` is the owner-facing
    rendering of a refusal and is empty on success.
    """

    status: str
    task: TaskRecord | None = None
    message: str = ""
    resolution: TodoResolution | None = None


def todo_candidate_label(candidate: TodoCandidate) -> str:
    """One candidate's display title (never invented, never truncated)."""
    return " ".join(str(candidate.label or "").split()) or f"Todo #{candidate.task_id}"


def format_todo_resolution(resolution: TodoResolution) -> str:
    """Owner-facing rendering of a 0- or N-candidate resolution."""
    if resolution.status == TODO_RESOLUTION_NOT_FOUND:
        return f"No todo found matching '{resolution.query}'."
    lines = [
        f"{len(resolution.candidates)} todos match '{resolution.query}' — "
        "which one do you mean?",
    ]
    for index, candidate in enumerate(resolution.candidates, start=1):
        lines.append(
            f"{index}. {todo_candidate_label(candidate)} "
            f"· #{candidate.task_id} · {candidate.status}"
        )
    if resolution.overflowed:
        lines.append("…and more")
    return "\n".join(lines)


def _coerce_positive_int(value: Any) -> int | None:
    from backend.ai.persian import coerce_int

    number = coerce_int(value)
    if number is None or number <= 0:
        return None
    return number


@dataclass(frozen=True)
class TaskView:
    task: TaskRecord
    occurrences: list[Any]


@dataclass(frozen=True)
class TaskListSnapshot:
    """One authoritative read of the owner's task list.

    ``tasks`` and ``fallback_active`` come from the SAME repository call with
    no await in between, so a concurrent operation can never clear the
    degraded marker while the content it describes is still rendered. Callers
    that both render and count the list must use one snapshot instead of two
    independent reads.

    ``fallback_active`` is the AUTHORITATIVE-READ signal: the repository sets
    it when the caller could not read the durable store (a real failure, or a
    bounded local-resource cooldown that skipped the durable call). A surface
    must therefore never present a degraded snapshot as the owner's task list.
    """

    tasks: list[TaskRecord]
    fallback_active: bool
    fallback_reason: str = ""

    def counts(self) -> dict[str, int]:
        """Per-status counts of THIS snapshot's tasks (one logical read).

        The rows and the counters a surface renders must describe the same
        read: a second repository call could observe a different state (the
        local-resource cooldown expiring, a recovery, a concurrent write) and
        produce a torn list/count view. ``deleted`` is counted here for
        completeness, but the task collection this snapshot came from already
        excludes terminal deleted rows.
        """
        result = {status: 0 for status in TASK_STATUSES}
        for task in self.tasks:
            status = str(getattr(task, "status", "") or "")
            if status in result:
                result[status] += 1
        return result


class TaskManagementService:
    def __init__(self, repository: TaskRepository, owner_id: int) -> None:
        self.repository = repository
        self.owner_id = owner_id

    async def list_tasks(self, status: str | None = None) -> list[TaskRecord]:
        """List the owner's tasks, optionally filtered by status.

        Filtering happens here (owner-scoped) so repository interfaces stay
        unchanged; task volumes are small. Status values are the record's
        canonical strings (active / paused / completed / ...).

        Deletion is a real row removal, so a deleted task is simply absent
        from the repository. The unfiltered list still excludes a
        ``deleted`` status defensively: that status is no longer produced,
        but a pre-existing legacy row that carries it must not reappear in
        the normal collection. An explicit ``status`` filter matches the
        record's exact status and is never widened.
        """
        tasks = await self.repository.list_tasks(self.owner_id)
        if status is None:
            return [
                t for t in tasks if str(getattr(t, "status", "") or "") != "deleted"
            ]
        return [t for t in tasks if str(getattr(t, "status", "") or "") == status]

    async def list_todos(self, status: str | None = None) -> list[TaskRecord]:
        """List the owner's TODOS, optionally filtered by status.

        A todo is the unscheduled kind of task (``schedule_type='todo'``): a
        title the owner manages by hand. Tasks with a real schedule are NOT
        todos and stay on the Taskloom surface; filtering happens here so the
        repository interface is unchanged.
        """
        tasks = await self.list_tasks(status=status)
        return [t for t in tasks if is_todo_schedule_type(t.schedule_type)]

    async def snapshot(
        self, status: str | None = None, *, todos_only: bool = False
    ) -> TaskListSnapshot:
        """Read the owner's task list once, with the fallback marker bound to it.

        ``todos_only`` binds the marker to the SAME read that produced the
        todos, so a Todo surface never pairs its rows with another read's
        degraded state.
        """
        tasks = (
            await self.list_todos(status=status)
            if todos_only
            else await self.list_tasks(status=status)
        )
        return TaskListSnapshot(
            tasks=tasks,
            fallback_active=bool(getattr(self.repository, "fallback_active", False)),
            fallback_reason=str(getattr(self.repository, "fallback_reason", "") or ""),
        )

    async def counts(self) -> dict[str, int]:
        """Per-status counts of the owner's durable tasks.

        ``deleted`` is a terminal state: it is never part of the normal task
        collection, so every normal summary (active / paused / completed /
        failed / expired totals) derives from this method and can never
        inflate with deleted tasks. The ``deleted`` key is still reported so
        diagnostics can see the terminal population separately.
        """
        tasks = await self.repository.list_tasks(self.owner_id)
        result = {status: 0 for status in TASK_STATUSES}
        for task in tasks:
            status = str(getattr(task, "status", "") or "")
            if status in result:
                result[status] += 1
        return result

    async def inspect(self, task_id: int, occurrence_limit: int = 100) -> TaskView | None:
        task = await self.repository.get_task(self.owner_id, task_id)
        if task is None:
            return None
        occurrences = await self.repository.list_occurrences(self.owner_id, task_id, occurrence_limit)
        return TaskView(task, occurrences)

    async def set_status(self, task_id: int, status: str, expected_version: int) -> TaskRecord | None:
        task = await self.repository.get_task(self.owner_id, task_id)
        if task is None:
            return None
        if (
            status == "active"
            and str(task.status) == "completed"
            and not is_todo_schedule_type(task.schedule_type)
        ):
            # ``completed -> active`` is the REOPEN edge, and reopening is a
            # TODO operation: a completed SCHEDULED task has no trustworthy
            # new boundary (its occurrence was cleared on completion, and
            # recomputing a past boundary would fire an overdue execution the
            # owner never asked for). It stays terminal, exactly as before.
            raise ValueError("only a todo can be reopened")
        if status == "paused" and is_todo_schedule_type(task.schedule_type):
            # A todo's lifecycle is ACTIVE <-> COMPLETED. Pausing one would
            # hide it from every todo surface while leaving it un-finishable,
            # so the pause verb is refused here instead of producing it.
            raise ValueError("a todo is completed or reopened, never paused")
        updates: dict[str, Any] = {"status": status}
        if (
            status in _NEXT_RUN_CLEAR_STATUSES
            or is_todo_schedule_type(task.schedule_type)
        ) and task.next_run_at is not None:
            # Pause and terminal states must not advertise another run; the
            # scheduler only executes active tasks anyway.
            updates["next_run_at"] = None
        elif status == "active" and task.status == "paused" and task.next_run_at is None:
            # Resume: recompute the next occurrence from the stored schedule
            # so the task actually runs again. Interval tasks have no anchor
            # (the previous occurrence was never created), so schedule the
            # first run one interval from now.
            recomputed = await self._resume_next_run(task)
            if recomputed is not None:
                updates["next_run_at"] = recomputed
        return await self.repository.update_task(
            self.owner_id, task_id, expected_version, updates
        )

    async def _resume_next_run(self, task: TaskRecord) -> datetime | None:
        if task.schedule_type == "event" or is_todo_schedule_type(task.schedule_type):
            # Event-triggered tasks have no wall-clock schedule and a todo is
            # unscheduled by definition; resuming either never fabricates a
            # next run time.
            return None
        try:
            schedule = parse_schedule(task.schedule_type, task.schedule)
            now = datetime.now(timezone.utc)
            if task.schedule_type == "interval":
                interval = getattr(schedule, "interval", None)
                if interval is None or interval.total_seconds() <= 0:
                    return None
                return advance_interval(now, interval, now)
            return next_occurrence(schedule, now)
        except (ScheduleError, TypeError, ValueError):
            logger.warning(
                "Task %s could not be rescheduled on resume; next_run_at stays unset",
                task.id,
            )
            return None

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

    async def update_definition(
        self,
        task_id: int,
        expected_version: int,
        candidate: dict[str, Any],
        reference: datetime,
    ) -> TaskRecord | None:
        """Edit an existing task's DEFINITION through the SAME CAS update.

        ``candidate`` is the same shape the wizard and the natural-language
        path produce, so a definition edit cannot introduce a second task
        shape or a second persistence path. The boundary is recomputed from the
        NEW schedule with the SAME helper creation uses, occurrences are never
        rewritten, and every future occurrence that was created (prepare-ahead)
        but never started is discarded so the next boundary runs the new
        definition version instead of a stale snapshot.

        Returns ``None`` for a missing task or a stale ``expected_version`` —
        nothing is written in either case.
        """
        current = await self.repository.get_task(self.owner_id, task_id)
        if current is None or current.version != expected_version:
            return None
        schedule_type = str(candidate["schedule_type"])
        updates: dict[str, Any] = {
            "label": candidate["label"],
            "schedule_type": schedule_type,
            "schedule": dict(candidate["schedule"]),
            "timezone": candidate["timezone"],
            "actions": [dict(action) for action in candidate["actions"]],
            "ai_instruction": candidate.get("ai_instruction"),
        }
        destination = dict(candidate.get("notification_destination") or {})
        if destination.get("chat_id"):
            # The destination is replaced only when the edit explicitly chose
            # one; otherwise the task keeps its trusted stored destination.
            updates["notification_destination"] = destination
        # The boundary is recomputed ONLY when the SCHEDULE really changed: an
        # edit that corrects the content must not silently push the next run a
        # whole interval out, and a paused task's cleared boundary stays clear.
        schedule_changed = (
            schedule_type != str(getattr(current, "schedule_type", "") or "")
            or dict(updates["schedule"]) != dict(getattr(current, "schedule", None) or {})
            or str(updates["timezone"]) != str(getattr(current, "timezone", "") or "")
        )
        if schedule_changed:
            try:
                updates["next_run_at"] = initial_next_run(schedule_type, updates["schedule"], reference)
            except ScheduleError:
                raise
        task = await self.repository.update_task(
            self.owner_id, task_id, expected_version, updates
        )
        if task is None:
            return None
        try:
            await self.repository.discard_unstarted_occurrences(
                self.owner_id, task_id, reference
            )
        except asyncio.CancelledError:
            raise
        except Exception:
            # The definition is durably updated either way; a stale unstarted
            # occurrence is rejected at its boundary by the version check, so
            # this is best-effort cleanup, not a correctness dependency.
            logger.warning(
                "Task %s: future unstarted occurrences could not be discarded after edit",
                task_id,
            )
        return task

    async def reopen(self, task_id: int, expected_version: int) -> TaskRecord | None:
        """Return ONE completed todo to active (completed -> active) under CAS.

        Reopening reuses the SAME ``set_status`` transition every other
        lifecycle change uses. It is restricted to a todo: a completed
        scheduled task is terminal in this architecture (see ``set_status``).
        Returns ``None`` for a missing task, a non-todo, or a stale
        ``expected_version`` — nothing is written in any of those cases.
        """
        task = await self.repository.get_task(self.owner_id, task_id)
        if task is None or task.version != expected_version:
            return None
        if str(task.status) != "completed" or not is_todo_schedule_type(task.schedule_type):
            return None
        return await self.set_status(task_id, "active", expected_version)

    async def edit_todo_title(
        self, task_id: int, expected_version: int, label: str
    ) -> TaskRecord | None:
        """Rename ONE todo through the SAME CAS update the repository exposes.

        Only the title changes: a todo has no schedule, no action and no
        occurrence, so nothing else can be invalidated and no execution
        boundary has to be recomputed. The version check is required — a stale
        ``expected_version`` fails and nothing is written. Rejects a blank or
        over-long title instead of storing a placeholder.
        """
        text = " ".join(str(label or "").split())
        if not text or len(text) > MAX_TODO_TITLE_CHARS:
            raise ValueError(
                "a todo needs a title of at most "
                f"{MAX_TODO_TITLE_CHARS} characters"
            )
        task = await self.repository.get_task(self.owner_id, task_id)
        if task is None or task.version != expected_version:
            return None
        if not is_todo_schedule_type(task.schedule_type):
            return None
        return await self.repository.update_task(
            self.owner_id, task_id, expected_version, {"label": text}
        )

    async def resolve_todos(self, query: str, limit: int = MAX_TODO_CANDIDATES) -> TodoResolution:
        """Resolve a title reference into 0, 1 or N owner-scoped todos.

        Read-only by contract: it performs no mutation and returns no action,
        only the candidates a caller may then act on. See the module header
        for the three deterministic tiers.
        """
        raw = " ".join(str(query or "").split())
        if not raw or len(raw) > MAX_TODO_QUERY_CHARS or limit <= 0:
            return TodoResolution(status=TODO_RESOLUTION_NOT_FOUND, query=raw)
        todos = await self.list_todos()
        if not todos:
            return TodoResolution(status=TODO_RESOLUTION_NOT_FOUND, query=raw)

        normalized = _normalize_todo_text(raw)
        identifier = _coerce_positive_int(normalized)
        if identifier is not None:
            matched = [t for t in todos if int(t.id) == identifier]
        else:
            tokens = [token for token in normalized.split() if token][:MAX_TODO_QUERY_TOKENS]
            if not tokens:
                return TodoResolution(status=TODO_RESOLUTION_NOT_FOUND, query=raw)
            matched = [
                t for t in todos
                if all(token in _normalize_todo_text(t.label) for token in tokens)
            ]
            if not matched:
                # The owner's sentence may contain the title verbatim; a title
                # too short to be a reference is not considered.
                matched = [
                    t for t in todos
                    if len(_normalize_todo_text(t.label)) >= _MIN_TITLE_IN_QUERY_CHARS
                    and _normalize_todo_text(t.label) in normalized
                ]
        if not matched:
            return TodoResolution(status=TODO_RESOLUTION_NOT_FOUND, query=raw)

        ordered = sorted(matched, key=lambda t: (int(t.id),))
        ordered.sort(
            key=lambda t: getattr(t, "created_at", None) or datetime.min.replace(tzinfo=timezone.utc),
            reverse=True,
        )
        overflowed = len(ordered) > limit
        candidates = tuple(
            TodoCandidate(
                task_id=int(t.id),
                label=str(t.label),
                status=str(t.status),
                version=int(t.version),
                created_at=getattr(t, "created_at", None),
            )
            for t in ordered[:limit]
        )
        return TodoResolution(
            status=TODO_RESOLUTION_UNIQUE if len(candidates) == 1 else TODO_RESOLUTION_AMBIGUOUS,
            query=raw,
            candidates=candidates,
            overflowed=overflowed,
        )

    async def resolve_todo_target(
        self, *, task_id: Any = 0, query: Any = ""
    ) -> TodoTarget:
        """Resolve ONE owner-scoped todo for a mutation, or refuse explicitly.

        A todo id goes through the owner-scoped read; a title reference goes
        through the SAME deterministic resolver above. Either answer is one
        todo or an explicit refusal — an ambiguous reference is never narrowed
        by a guess, and nothing is written here.
        """
        identifier = _coerce_positive_int(task_id)
        raw_query = str(query or "").strip()
        if identifier is not None and raw_query:
            return TodoTarget(
                status=TODO_TARGET_INVALID,
                message="Provide either a todo id or a title — not both.",
            )
        if identifier is None and not raw_query:
            return TodoTarget(
                status=TODO_TARGET_INVALID,
                message="A todo id or a title is required.",
            )
        if identifier is not None:
            task = await self.repository.get_task(self.owner_id, identifier)
            if task is None or not is_todo_schedule_type(task.schedule_type):
                return TodoTarget(
                    status=TODO_TARGET_NOT_FOUND,
                    message=f"No todo found for #{identifier}.",
                )
            return TodoTarget(status=TODO_TARGET_OK, task=task)

        resolution = await self.resolve_todos(raw_query)
        if resolution.status == TODO_RESOLUTION_UNIQUE:
            task = await self.repository.get_task(
                self.owner_id, resolution.candidates[0].task_id
            )
            if task is None:
                return TodoTarget(
                    status=TODO_TARGET_NOT_FOUND,
                    message=f"No todo found matching '{raw_query}'.",
                    resolution=resolution,
                )
            return TodoTarget(status=TODO_TARGET_OK, task=task, resolution=resolution)
        if resolution.status == TODO_RESOLUTION_AMBIGUOUS:
            return TodoTarget(
                status=TODO_TARGET_AMBIGUOUS,
                message=format_todo_resolution(resolution),
                resolution=resolution,
            )
        return TodoTarget(
            status=TODO_TARGET_NOT_FOUND,
            message=format_todo_resolution(resolution),
            resolution=resolution,
        )

    async def delete(self, task_id: int, expected_version: int) -> TaskDeletionResult:
        """Physically delete the owner's task row (never a status write).

        Deletion is a real repository removal (``delete_task``): the durable
        row and its occurrences are gone, so the task leaves every list
        because it no longer exists. The CAS ``expected_version`` still
        guards stale writers, and the result distinguishes a durable removal
        from a missing task, a stale version, and a degraded (in-memory)
        deletion that must never be reported as durable.
        """
        return await self.repository.delete_task(
            self.owner_id, task_id, expected_version
        )
