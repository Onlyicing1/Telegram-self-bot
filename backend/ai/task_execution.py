"""Execute claimed durable occurrences through the registered tool boundary."""
from __future__ import annotations

import asyncio
import json
import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import re

from backend.ai.database.task_repository import OccurrenceRecord, TaskRepository
from backend.ai.task_contract import (
    ACTION_RUNS_KEY,
    SCHEDULED_OCCURRENCE_EXTRA,
    PreparedAction,
    TaskContractError,
    action_reference_error,
    action_runs_from_metadata,
    bounded_action_output,
    build_action_run,
    resolve_action_arguments,
    validate_prepared_action,
)
from backend.ai.tools.base import declared_consumable_output_fields
from backend.ai.tools.context import ToolContext
from backend.ai.tools.executor import ToolExecutionResult, ToolExecutor
from backend.ai.tools.registry import ToolRegistry
from backend.ai.preparation_policy import (
    CONTENT_FIELDS,
    PreparationPolicy,
    PreparationPolicyError,
    derive_policy,
    strip_attribution_prefix,
    validate_prepared_arguments,
)
from backend.ai.retry import FailureClass, classify_failure, retry_delay, can_retry

logger = logging.getLogger(__name__)
MAX_EXECUTION_SECONDS = 60.0
MAX_METADATA_BYTES = 8192
MAX_RESULT_DELIVERY_CHARS = 4000
RESULT_DELIVERY_TIMEOUT_SECONDS = 10.0
MAX_PREPARATION_SECONDS = 45.0
MAX_PREPARATION_ATTEMPTS = 3

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)


class TaskPreparationError(ValueError):
    """Occurrence-time AI argument preparation failed; the action must not run."""


def present_calls(calls: list[dict[str, Any]], instruction: str) -> list[dict[str, Any]]:
    """Apply the task's PRESENTATION contract to validated calls.

    Separation of the two concepts a named-source task carries: source
    IDENTITY is validated on the generated line (preparation, persisted
    metadata, boundary) and the owner-visible FORMATTING is applied here —
    after the last validation and immediately before the ToolExecutor — so a
    source task executes "Don't be afraid…" while still having proven the line
    was attributed to the requested source. Displaying the source is opt-in:
    the label is kept ONLY when the instruction explicitly asked for it.

    The validated calls are never mutated: they remain what is persisted as
    the occurrence's prepared action, so every later re-validation sees the
    same deterministic attributed text. Returns the input unchanged when the
    task requested the visible-source presentation.
    """
    policy = derive_policy(instruction)
    if policy.show_source or not policy.source:
        return calls
    presented: list[dict[str, Any]] = []
    for call in calls:
        arguments = call.get("arguments")
        if not isinstance(arguments, dict):
            presented.append(call)
            continue
        rewritten: dict[str, Any] | None = None
        for field in CONTENT_FIELDS:
            value = arguments.get(field)
            if not isinstance(value, str):
                continue
            visible = strip_attribution_prefix(value, policy.source)
            if visible is None or not visible.strip():
                continue
            if rewritten is None:
                rewritten = dict(arguments)
            rewritten[field] = visible
        presented.append(
            {"name": call.get("name"), "arguments": rewritten}
            if rewritten is not None else call
        )
    return presented


def _load_preparation_json(raw: str) -> Any:
    """Parse the model's JSON, tolerating the common markdown-fence wrapper."""
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK_RE.search(raw)
    if not match:
        return json.loads(raw)
    return json.loads(match.group(1))


class AIActionPreparator:
    """Resolve AI-generated arguments for one occurrence, fail-closed.

    The model NEVER becomes an execution authority: it receives the stored
    instruction plus the task's own action templates (tool names fixed by the
    task definition) and must return complete arguments for those SAME tools.
    No tool definitions are attached (``tools=[]``), so the provider cannot
    trigger any execution — it can only emit arguments, and only the
    ToolRegistry/ToolExecutor boundary decides what actually runs.

    Generated CONTENT is additionally validated against the deterministic
    policy derived from the instruction (language, length, garbage checks);
    invalid output is regenerated within a bounded attempt count, never
    truncated, never executed.
    """

    def __init__(self, provider_manager: Any) -> None:
        self._providers = provider_manager

    async def prepare(
        self,
        instruction: str,
        templates: list[dict[str, Any]],
        *,
        owner_id: int,
        tz_str: str,
        policy: PreparationPolicy | None = None,
    ) -> list[dict[str, Any]]:
        from backend.ai.providers.base import ProviderResponse

        policy = policy or derive_policy(instruction)
        now = datetime.now(timezone.utc).isoformat()
        messages = [
            {
                "role": "system",
                "content": (
                    "You prepare final arguments for a scheduled task's actions. "
                    "Return ONLY one JSON object: "
                    '{"actions": [{"name": "<tool name>", "arguments": {...}}, ...]} — '
                    "exactly one entry per listed action, in the same order, with the "
                    "SAME tool names. Arguments must be complete and final for THIS "
                    "run. Never include owner ids, chat ids, message ids, phone "
                    "numbers, or any destination. Never invent new tools. No prose, "
                    "no markdown beyond a JSON code fence."
                ),
            },
            {
                "role": "user",
                "content": (
                    f"Task instruction: {instruction}\n"
                    + (
                        f"Content requirements (ENFORCED, output is rejected otherwise): {policy.describe()}.\n"
                        if policy.active
                        else ""
                    )
                    + f"Current time (UTC): {now}\n"
                    f"Owner timezone: {tz_str}\n"
                    f"Actions to prepare: {json.dumps(templates, ensure_ascii=False)}"
                ),
            },
        ]
        try:
            response: ProviderResponse = await asyncio.wait_for(
                self._providers.chat(messages, tools=[]),
                timeout=MAX_PREPARATION_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            # Transient provider failure — retryable under the existing contract.
            raise TimeoutError("task preparation provider timed out") from exc
        except Exception as exc:
            raise TaskPreparationError(f"task preparation provider failed: {type(exc).__name__}") from exc
        if not response.success:
            raise TaskPreparationError(
                f"task preparation provider failed: {response.provider_name or 'unknown'}"
            )
        raw = response.text
        if not isinstance(raw, str) or not raw.strip():
            raise TaskPreparationError("task preparation returned no structured output")
        try:
            value = _load_preparation_json(raw)
        except json.JSONDecodeError as exc:
            raise TaskPreparationError("task preparation returned invalid JSON") from exc
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            raise TaskPreparationError("task preparation returned invalid JSON") from exc
        if not isinstance(value, dict) or set(value) != {"actions"}:
            raise TaskPreparationError("task preparation output must be {'actions': [...]}")
        actions = value["actions"]
        if not isinstance(actions, list) or len(actions) != len(templates):
            raise TaskPreparationError("task preparation returned the wrong number of actions")
        prepared: list[dict[str, Any]] = []
        for template, action in zip(templates, actions, strict=True):
            if not isinstance(action, dict):
                raise TaskPreparationError("each prepared action must be an object")
            # The model may fill arguments for the task's own tools ONLY; a
            # swapped or invented tool name fails closed here.
            if action.get("name") != template["name"]:
                raise TaskPreparationError("prepared action tool name does not match the task definition")
            arguments = action.get("arguments")
            if not isinstance(arguments, dict):
                raise TaskPreparationError("prepared action arguments must be an object")
            try:
                validate_prepared_action({"name": template["name"], "arguments": arguments})
            except TaskContractError as exc:
                raise TaskPreparationError(str(exc)) from exc
            try:
                validate_prepared_arguments(arguments, policy)
            except PreparationPolicyError as exc:
                raise TaskPreparationError(f"prepared content violates the task policy: {exc}") from exc
            prepared.append({"name": template["name"], "arguments": dict(arguments)})
        return prepared

    async def prepare_validated(
        self,
        instruction: str,
        templates: list[dict[str, Any]],
        *,
        owner_id: int,
        tz_str: str,
    ) -> list[dict[str, Any]]:
        """Prepare with bounded regeneration until the policy accepts.

        Each provider round validates structure AND content policy; invalid
        output is regenerated (the enforced policy is stated in every
        round's prompt), up to ``MAX_PREPARATION_ATTEMPTS`` total rounds.
        A TimeoutError still propagates (retryable under the existing
        contract); policy/structure rejections consume rounds and fail
        closed when exhausted.
        """
        policy = derive_policy(instruction)
        last_error: Exception | None = None
        for attempt in range(1, MAX_PREPARATION_ATTEMPTS + 1):
            try:
                return await self.prepare(
                    instruction,
                    templates,
                    owner_id=owner_id,
                    tz_str=tz_str,
                    policy=policy,
                )
            except TaskPreparationError as exc:
                last_error = exc
                # A structural violation that regeneration cannot fix (tool
                # name swap, wrong action count) must not burn more rounds:
                # only content-policy rejections are worth regenerating.
                if "tool name" in str(exc) or "number of actions" in str(exc):
                    raise
            except TimeoutError:
                raise
        raise TaskPreparationError(f"task preparation failed after {attempt} attempts: {last_error}")


def _default_preparator() -> AIActionPreparator | None:
    """Resolve the process ProviderManager for the default preparator."""
    try:
        from backend.ai.engine.engine import get_engine
        return AIActionPreparator(get_engine().provider_manager)
    except Exception:
        return None


@dataclass(frozen=True)
class TaskExecutionResult:
    success: bool
    status: str
    action_count: int
    successful_actions: int
    error: str = ""
    metadata: dict[str, Any] | None = None


@dataclass
class _ChainOutcome:
    """The result of executing one occurrence's ordered action chain."""
    results: list[ToolExecutionResult]
    runs: list[dict[str, Any]]
    failed_position: int | None = None
    failure: Any = ""
    skipped: int = 0

    @property
    def succeeded(self) -> int:
        return sum(1 for run in self.runs if run["status"] == "succeeded")


def _ordered_runs(state: dict[int, dict[str, Any]]) -> list[dict[str, Any]]:
    """The recorded runs in action order (positions are validated 1-based)."""
    return [state[position] for position in sorted(state)]


def _recorded_runs(occurrence: OccurrenceRecord) -> tuple[list[dict[str, Any]], str]:
    """``(runs, error)`` — the durable per-action state of THIS occurrence.

    The record is occurrence-scoped by construction (it lives in the
    occurrence's own bounded metadata), so one task can never read another
    task's, another occurrence's, or the owner's previous run's result. A
    malformed record is reported so the caller fails closed instead of
    replaying side effects it cannot account for.
    """
    for metadata in (occurrence.error_metadata, occurrence.result_metadata):
        if isinstance(metadata, dict) and ACTION_RUNS_KEY in metadata:
            try:
                return action_runs_from_metadata(metadata)[0], ""
            except (TaskContractError, TypeError, ValueError) as exc:
                return [], f"invalid_action_record: {exc}"
    return [], ""


def _run_record_mismatch(
    runs: list[dict[str, Any]], calls: list[dict[str, Any]]
) -> str:
    """The recorded runs must describe THIS action snapshot, position by
    position: a record that names another tool is a stale definition, and the
    occurrence fails closed instead of trusting it."""
    for run in runs:
        position = run["position"]
        if position > len(calls) or calls[position - 1]["name"] != run["tool"]:
            return (
                f"invalid_action_record: action {position} was recorded for a "
                "different tool than the occurrence's action"
            )
    return ""


def _bounded_metadata(value: dict[str, Any]) -> dict[str, Any]:
    encoded = json.dumps(value, ensure_ascii=False, separators=(",", ":"))
    if len(encoded.encode()) > MAX_METADATA_BYTES:
        raise ValueError("execution metadata exceeds bounded size")
    return value


class TaskExecutionCoordinator:
    """Coordinates one claimed occurrence; it never owns scheduling."""

    def __init__(
        self,
        repository: TaskRepository,
        executor: ToolExecutor,
        owner_id: int,
        context: ToolContext,
        client_provider=None,
        preparator: AIActionPreparator | None = None,
    ) -> None:
        self.repository = repository
        self.executor = executor
        self.owner_id = owner_id
        self.context = context
        self.client_provider = client_provider
        self.preparator = preparator

    def _fresh_context(self) -> ToolContext:
        """Return a context bound to the CURRENT self client when a provider
        is available (the supervisor's recovery path rebuilds the client; a
        stale captured client must never execute a scheduled action)."""
        if self.client_provider is None:
            return self.context
        try:
            client = self.client_provider()
        except Exception:
            client = None
        if client is None or client is self.context.client:
            return self.context
        from backend.telegram_api import TelegramAPI
        return ToolContext(
            telegram=TelegramAPI(client),
            owner_id=self.context.owner_id,
            tz_str=self.context.tz_str,
            client=client,
            extra=self.context.extra,
        )

    async def execute(self, occurrence: OccurrenceRecord) -> TaskExecutionResult:
        if occurrence.owner_id != self.owner_id:
            return TaskExecutionResult(False, "failed", 0, 0, "owner_mismatch")
        if occurrence.status != "running":
            return TaskExecutionResult(False, occurrence.status, 0, 0, "occurrence_not_running")

        # Resolve the trusted destination for this task's actions.
        # For send_message tools, the chat_id comes from the task's
        # notification_destination (set at creation time from trusted
        # runtime context), never from the model.
        execution_context = self._fresh_context()
        # Trusted origin marker: this context belongs to a CLAIMED SCHEDULED
        # occurrence, not to an interactive owner request. It is written here
        # from runtime state (never from a model or a candidate) and is what
        # lets a tool tell the two apart — durable task creation is refused
        # from this context (see backend.ai.task_contract).
        occurrence_extra = dict(execution_context.extra) if execution_context.extra else {}
        occurrence_extra[SCHEDULED_OCCURRENCE_EXTRA] = True
        task = await self.repository.get_task(self.owner_id, occurrence.task_id)
        if task is not None:
            dest = task.notification_destination or {}
            chat_id = dest.get("chat_id")
            if isinstance(chat_id, int) and chat_id != 0:
                occurrence_extra["chat_id"] = chat_id
        execution_context = ToolContext(
            telegram=execution_context.telegram,
            owner_id=execution_context.owner_id,
            tz_str=execution_context.tz_str,
            client=execution_context.client,
            extra=occurrence_extra,
        )

        actions = occurrence.action_snapshot
        if not isinstance(actions, list) or not actions or len(actions) > 5:
            return await self._fail(occurrence, "invalid_action_snapshot", 0, 0)
        calls: list[dict[str, Any]] = []
        execution_calls: list[dict[str, Any]] = []
        for action in actions:
            if not isinstance(action, dict):
                return await self._fail(occurrence, "invalid_action", 0, 0)
            name = action.get("name") or action.get("tool")
            arguments = action.get("arguments", action.get("parameters", {}))
            if not isinstance(name, str) or not name or not isinstance(arguments, dict):
                return await self._fail(occurrence, "invalid_action", 0, 0)
            if self.executor._registry.get(name) is None:
                return await self._fail(occurrence, "unregistered_action", 0, 0)
            calls.append({"name": name, "arguments": arguments})
        execution_calls = calls

        # AI-assisted occurrences: only tasks that persist an ai_instruction
        # pay for a provider round. Static tasks keep the exact deterministic
        # path with zero provider calls. The prepared calls still flow through
        # the SAME ToolExecutor below; the model is never an execution
        # authority and can never change the task's tool names.
        instruction = getattr(task, "ai_instruction", None) if task is not None else None
        generation_authorized = isinstance(instruction, str) and bool(instruction.strip())

        # Action-chain contract, re-proved BEFORE anything executes: a stored
        # reference that cannot resolve deterministically fails the whole
        # occurrence closed instead of running a partial chain with a guess.
        reference_error = action_reference_error(
            actions,
            self.executor._registry,
            generation_authorized=generation_authorized,
        )
        if reference_error:
            return await self._fail(
                occurrence, f"invalid_action_reference: {reference_error}", 0, 0
            )

        # Durable per-action state: written as each action completes, read back
        # on a retry so a succeeded side effect is never repeated and a failed
        # action resumes where it stopped.
        prior_runs, record_error = _recorded_runs(occurrence)
        if record_error:
            return await self._fail(occurrence, record_error, 0, 0)
        mismatch = _run_record_mismatch(prior_runs, calls)
        if mismatch:
            return await self._fail(occurrence, mismatch, 0, 0)

        if generation_authorized:
            try:
                prepared = self._prepared_from_metadata(occurrence, task)
                if prepared is not None:
                    calls = prepared  # durably prepared ahead of time — no provider call at the boundary
                else:
                    prepared = await self._prepare_calls(calls, instruction, execution_context)
                    self._validate_prepared_calls(calls, prepared)
                    calls = prepared
                # Deterministic content policy is re-proven AT the boundary for
                # every AI-assisted occurrence: the metadata path validated in
                # _prepared_from_metadata, this closes the occurrence-time path.
                self._enforce_content_policy(calls, instruction)
                # Presentation only: the validated calls stay intact for the
                # audit metadata below; the executor receives the visible form.
                execution_calls = present_calls(calls, instruction)
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                return await self.handle_failure(occurrence, exc, action_count=len(calls))

        # The live per-action state of THIS attempt: seeded from the durable
        # record, updated as each action completes, and readable by the failure
        # path even when the chain is cut short by a timeout or an exception.
        state: dict[int, dict[str, Any]] = {run["position"]: run for run in prior_runs}
        started = time.perf_counter()
        try:
            outcome = await asyncio.wait_for(
                self._execute_chain(
                    occurrence, calls, execution_calls, state, execution_context
                ),
                timeout=MAX_EXECUTION_SECONDS,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            return await self.handle_failure(
                occurrence, exc, action_count=len(calls),
                runs=_ordered_runs(state) or None,
            )

        successful = outcome.succeeded
        duration_ms = round((time.perf_counter() - started) * 1000, 1)
        metadata = _bounded_metadata({
            "action_count": len(calls),
            "successful_action_count": successful,
            "duration_ms": duration_ms,
            "terminal_status": "succeeded" if outcome.failed_position is None else "failed",
            "actions": outcome.runs,
        })
        if outcome.failed_position is not None:
            return await self.handle_failure(
                occurrence,
                outcome.failure,
                successful=successful,
                action_count=len(calls),
                metadata=metadata,
                runs=outcome.runs,
            )
        updated = await self.repository.transition_occurrence(
            self.owner_id, occurrence.task_id, occurrence.occurrence_key,
            "succeeded", result_metadata=metadata,
            **self._preparation_updates(task, calls),
        )
        if updated is None:
            return TaskExecutionResult(False, "unknown", len(calls), successful, "state_persist_failed", metadata)
        await self._deliver_result(task, outcome.results, execution_context, occurrence.occurrence_key)
        return TaskExecutionResult(True, "succeeded", len(calls), successful, metadata=metadata)

    async def _execute_chain(
        self,
        occurrence: OccurrenceRecord,
        calls: list[dict[str, Any]],
        execution_calls: list[dict[str, Any]],
        state: dict[int, dict[str, Any]],
        execution_context: ToolContext,
    ) -> _ChainOutcome:
        """Run ONE occurrence's ordered actions as a durable chain.

        Sequential by construction: action N+1 is not even resolved until
        action N has SUCCEEDED, so a failure stops the chain (later actions are
        recorded ``pending`` and never run). Each action's call still goes
        through the single ToolExecutor (never ``tool.execute()`` directly),
        its arguments may reference a declared output field of an EARLIER
        action of this same occurrence (resolved here, deterministically), and
        its bounded result/state is persisted through the occurrence's own
        metadata before the next action starts. An action already recorded
        ``succeeded`` in an earlier attempt is NOT re-executed: its recorded
        output stays available to later references.
        """
        registry = self.executor._registry
        results: list[ToolExecutionResult] = []
        skipped = 0
        failed_position: int | None = None
        failure: Any = ""
        for position, call in enumerate(calls, start=1):
            name = call["name"]
            recorded = state.get(position)
            if recorded is not None and recorded["status"] == "succeeded":
                skipped += 1
                continue
            arguments = execution_calls[position - 1].get("arguments", {})
            resolved, reason = resolve_action_arguments(arguments, _ordered_runs(state))
            if reason:
                state[position] = build_action_run(position, name, "failed", error=reason)
                failed_position, failure = position, reason
                break
            state[position] = build_action_run(position, name, "running")
            await self._persist_runs(occurrence, state)
            result = (await self.executor.execute_calls(
                [{"name": name, "arguments": resolved}],
                owner_id=self.owner_id,
                session_id=f"task:{occurrence.task_id}:{occurrence.occurrence_key}",
                context_override=execution_context,
            ))[0]
            results.append(result)
            if not result.success:
                failure = self._action_failure(result)
                state[position] = build_action_run(
                    position, name, "failed", error=str(failure)
                )
                failed_position = position
                break
            tool = registry.get(name)
            state[position] = build_action_run(
                position, name, "succeeded",
                output=bounded_action_output(
                    result.data,
                    declared_consumable_output_fields(tool) if tool is not None else (),
                ),
            )
            await self._persist_runs(occurrence, state)
        # Actions the chain never reached are recorded explicitly pending, so
        # the occurrence always carries the full per-action state.
        for position, call in enumerate(calls, start=1):
            state.setdefault(position, build_action_run(position, call["name"], "pending"))
        if skipped:
            logger.info(
                "TASK_CHAIN_RESUMED task_id=%s occurrence_key=%s attempt=%s "
                "already_succeeded=%s executed=%s",
                occurrence.task_id, occurrence.occurrence_key, occurrence.attempt,
                skipped, len(results),
            )
        return _ChainOutcome(
            results=results,
            runs=_ordered_runs(state),
            failed_position=failed_position,
            failure=failure,
            skipped=skipped,
        )

    @staticmethod
    def _action_failure(result: ToolExecutionResult) -> str | BaseException:
        """The failure reason of ONE action, classified as before.

        A timeout keeps its retryable classification; every other tool failure
        keeps the existing deterministic (non-retryable) outcome.
        """
        failure: str | BaseException = result.error or result.message or "action_failed"
        if failure == "timeout" or str(result.message or "").lower().startswith("timeout"):
            return TimeoutError(result.message or "tool timed out")
        return failure

    async def _persist_runs(
        self, occurrence: OccurrenceRecord, state: dict[int, dict[str, Any]]
    ) -> None:
        """Persist the bounded per-action record while the chain runs.

        Written on the occurrence's OWN bounded metadata (no schema change) so
        a restart or retry can never replay a side effect the record already
        accounts for. A failed audit write is logged and never invents
        success; the next action's write — or the terminal transition, which
        always carries the complete record — retries it.
        """
        try:
            updated = await self.repository.transition_occurrence(
                self.owner_id, occurrence.task_id, occurrence.occurrence_key,
                "running", result_metadata=_bounded_metadata({
                    ACTION_RUNS_KEY: _ordered_runs(state),
                }),
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "TASK_ACTION_RECORD_PERSIST_FAILED task_id=%s occurrence_key=%s exception=%s",
                occurrence.task_id, occurrence.occurrence_key, type(exc).__name__,
            )
            return
        if updated is None:
            logger.warning(
                "TASK_ACTION_RECORD_PERSIST_SKIPPED task_id=%s occurrence_key=%s status=%s",
                occurrence.task_id, occurrence.occurrence_key, occurrence.status,
            )

    async def prepare_ahead(self, occurrence: OccurrenceRecord) -> list[dict[str, Any]] | None:
        """Prepare AI arguments BEFORE the occurrence is due (no side effects).

        Runs the bounded AI preparation for an AI-assisted occurrence and
        persists the validated result as a version-stamped ``PreparedAction``
        in the occurrence's existing ``preparation_metadata`` column (no
        schema change). Pure preparation: this method NEVER executes a tool
        and NEVER touches Telegram — the ToolExecutor below remains the only
        execution authority, and only at the occurrence boundary.

        Returns the prepared calls, or ``None`` when preparation is not
        applicable (static task), already durably prepared, or failed (the
        failure is logged; execution will then fall back to the documented
        occurrence-time preparation path).
        """
        if occurrence.owner_id != self.owner_id:
            return None
        task = await self.repository.get_task(self.owner_id, occurrence.task_id)
        if task is None:
            return None
        instruction = getattr(task, "ai_instruction", None)
        if not isinstance(instruction, str) or not instruction.strip():
            return None  # static task — nothing to prepare
        if self._prepared_from_metadata(occurrence, task) is not None:
            return None  # already durably prepared for this occurrence
        actions = occurrence.action_snapshot
        if not isinstance(actions, list) or not actions or len(actions) > 5:
            return None
        templates = []
        for action in actions:
            if not isinstance(action, dict):
                return None
            name = action.get("name") or action.get("tool")
            arguments = action.get("arguments", action.get("parameters", {}))
            if not isinstance(name, str) or not name or not isinstance(arguments, dict):
                return None
            templates.append({"name": name, "arguments": arguments})
        try:
            prepared = await self._prepare_calls(templates, instruction)
            self._validate_prepared_calls(templates, prepared)
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "TASK_PREPARE_AHEAD_FAILED task_id=%s occurrence_key=%s exception=%s detail=%s",
                occurrence.task_id, occurrence.occurrence_key, type(exc).__name__, str(exc)[:200],
            )
            return None
        prepared_at = datetime.now(timezone.utc).isoformat()
        if len(prepared) != 1:
            # The metadata column holds exactly ONE PreparedAction; multi-action
            # AI tasks keep execution-time preparation (existing behavior).
            return prepared
        try:
            metadata = PreparedAction(
                definition_version=int(getattr(task, "version", 1) or 1),
                action=prepared[0],
                prepared_at=prepared_at,
            ).as_dict()
        except (TaskContractError, TypeError, ValueError):
            return prepared  # metadata contract rejected it; execution still prepares then
        try:
            await self.repository.transition_occurrence(
                self.owner_id, occurrence.task_id, occurrence.occurrence_key,
                occurrence.status if occurrence.status in {"claimed", "retry_pending", "interrupted"} else "claimed",
                preparation_metadata=metadata,
            )
        except (ValueError, TypeError):
            return prepared  # persistence failed; execution still prepares then
        logger.info(
            "TASK_PREPARED_AHEAD task_id=%s occurrence_key=%s scheduled_for=%s prepared_at=%s",
            occurrence.task_id, occurrence.occurrence_key,
            occurrence.scheduled_for.isoformat() if occurrence.scheduled_for else "-", prepared_at,
        )
        return prepared

    def _prepared_from_metadata(
        self, occurrence: OccurrenceRecord, task: Any
    ) -> list[dict[str, Any]] | None:
        """Return a durably persisted, still-valid prepared action, or None.

        Validity is re-proven from the persisted record: correct kind, SAME
        definition version, tool name identical to the occurrence's stored
        action snapshot, contract-valid arguments, and content that still
        satisfies the deterministic policy. Anything stale or invalid is
        ignored — never executed blindly (restart safety).
        """
        metadata = occurrence.preparation_metadata or {}
        if not isinstance(metadata, dict) or not metadata:
            return None
        instruction = getattr(task, "ai_instruction", None)
        if not isinstance(instruction, str) or not instruction.strip():
            return None
        try:
            if metadata.get("kind") != "prepared_action":
                return None
            if int(metadata.get("definition_version", -1)) != int(task.version):
                return None  # task was edited after preparation — stale
            action = validate_prepared_action(metadata.get("action"))
        except (TaskContractError, TypeError, ValueError):
            return None
        snapshot = occurrence.action_snapshot
        if not isinstance(snapshot, list) or len(snapshot) != 1 or not isinstance(snapshot[0], dict):
            return None
        snapshot_name = snapshot[0].get("name") or snapshot[0].get("tool")
        if action["name"] != snapshot_name:
            return None
        policy = derive_policy(instruction)
        try:
            validate_prepared_arguments(action["arguments"], policy)
        except PreparationPolicyError:
            return None
        return [action]

    async def _prepare_calls(
        self,
        templates: list[dict[str, Any]],
        instruction: str,
        execution_context: ToolContext | None = None,
    ) -> list[dict[str, Any]]:
        """Bounded preparation loop owned by the coordinator.

        Each round runs the preparation seam ONCE and re-proves BOTH the
        structure (task's own tools, same order, same count) and the
        deterministic content policy. Content-policy rejections are
        regenerated (the enforced policy is stated in every round's prompt),
        up to ``MAX_PREPARATION_ATTEMPTS`` total rounds, then fail closed.
        Structural violations (tool-name swap, wrong action count) raise
        immediately — regeneration cannot fix them. TimeoutError propagates
        (retryable under the existing failure contract).
        """
        preparator = self.preparator
        if preparator is None:
            preparator = _default_preparator()
        if preparator is None:
            raise TaskPreparationError("task preparation authority is unavailable")
        owner_id = self.owner_id
        tz_str = execution_context.tz_str if execution_context is not None else "UTC"
        # Single-round seam preferred: the coordinator owns the attempt
        # budget. Preparators implementing only the legacy self-looping
        # ``prepare_validated`` interface stay supported through the fallback.
        prepare = getattr(preparator, "prepare", None)
        if prepare is None:
            prepare = preparator.prepare_validated
        policy = derive_policy(instruction)
        last_error: Exception | None = None
        attempt = 0
        for attempt in range(1, MAX_PREPARATION_ATTEMPTS + 1):
            try:
                prepared = await prepare(
                    instruction,
                    [{"name": c["name"], "arguments": c["arguments"]} for c in templates],
                    owner_id=owner_id,
                    tz_str=tz_str,
                )
            except TaskPreparationError as exc:
                message = str(exc)
                if "tool name" in message or "number of actions" in message:
                    raise  # structural — regeneration cannot fix it
                last_error = exc
                continue
            # Structural re-proof for the coordinator (defense-in-depth for
            # preparators that do not validate themselves).
            self._validate_prepared_calls(templates, prepared)
            try:
                for call in prepared:
                    validate_prepared_arguments(call.get("arguments", {}), policy)
            except PreparationPolicyError as exc:
                last_error = TaskPreparationError(
                    f"prepared content violates the task policy: {exc}"
                )
                continue
            return prepared
        raise TaskPreparationError(
            f"task preparation failed after {attempt} attempts: {last_error}"
        )

    @staticmethod
    def _enforce_content_policy(calls: list[dict[str, Any]], instruction: str) -> None:
        """Fail closed when prepared content violates the task's policy.

        Defense-in-depth at the execution boundary: even a buggy or hostile
        preparator cannot push content that misses the deterministic
        language/length contract into the ToolExecutor.
        """
        policy = derive_policy(instruction)
        if not policy.active:
            return
        for call in calls:
            try:
                validate_prepared_arguments(call.get("arguments", {}), policy)
            except PreparationPolicyError as exc:
                raise TaskPreparationError(f"prepared content violates the task policy: {exc}") from exc

    @staticmethod
    def _validate_prepared_calls(
        templates: list[dict[str, Any]],
        prepared: list[dict[str, Any]],
    ) -> None:
        """Re-validate prepared calls at the execution boundary (defense-in-depth).

        The coordinator never trusts the preparator: even a buggy or hostile
        preparator cannot swap tool names, add or drop actions, or smuggle a
        non-object argument past this check — only the task's own registered
        tools, in the task's own order, may run.
        """
        if not isinstance(prepared, list) or len(prepared) != len(templates):
            raise TaskPreparationError("prepared actions do not match the task definition")
        for template, action in zip(templates, prepared, strict=True):
            try:
                normalized = validate_prepared_action(action)
            except TaskContractError as exc:
                raise TaskPreparationError(str(exc)) from exc
            if normalized["name"] != template["name"]:
                raise TaskPreparationError("prepared action tool name does not match the task definition")

    @staticmethod
    def _preparation_updates(task: Any, calls: list[dict[str, Any]]) -> dict[str, Any]:
        """Audit record for a successful AI preparation.

        The occurrence ``preparation_metadata`` schema holds exactly one
        PreparedAction, so single-action occurrences record their prepared
        action; multi-action AI tasks keep execution results only. Diagnostic
        only — never part of the success/failure decision.
        """
        instruction = getattr(task, "ai_instruction", None) if task is not None else None
        if not isinstance(instruction, str) or not instruction.strip() or len(calls) != 1:
            return {}
        try:
            metadata = PreparedAction(
                definition_version=int(getattr(task, "version", 1) or 1),
                action=calls[0],
                prepared_at=datetime.now(timezone.utc).isoformat(),
            ).as_dict()
        except (TaskContractError, TypeError, ValueError):
            return {}
        return {"preparation_metadata": metadata}

    async def _deliver_result(
        self,
        task: Any,
        results: list[Any],
        execution_context: ToolContext,
        occurrence_key: str,
    ) -> None:
        """Deliver the execution result to the task's destination chat when
        the task definition explicitly asked for it (``deliver_result``).

        Best-effort and isolated: a delivery failure is logged and never
        changes the occurrence outcome (the actions already succeeded), and
        the destination always comes from the trusted task definition —
        never from the model.
        """
        if task is None:
            return
        destination = task.notification_destination or {}
        if destination.get("deliver_result") is not True:
            return
        chat_id = destination.get("chat_id")
        if not isinstance(chat_id, int) or chat_id == 0:
            chat_id = self.owner_id
        parts = [str(getattr(item, "message", "") or "").strip() for item in results]
        text = "\n\n".join(part for part in parts if part)
        if not text:
            return
        if len(text) > MAX_RESULT_DELIVERY_CHARS:
            text = text[: MAX_RESULT_DELIVERY_CHARS - 3] + "..."
        telegram = getattr(execution_context, "telegram", None)
        client = getattr(execution_context, "client", None)
        if telegram is None and client is not None:
            from backend.telegram_api import TelegramAPI
            telegram = TelegramAPI(client)
        if telegram is None:
            logger.warning(
                "TASK_RESULT_DELIVERY_UNAVAILABLE task_id=%s occurrence_key=%s",
                task.id, occurrence_key,
            )
            return
        try:
            await asyncio.wait_for(
                telegram.send_message(chat_id, text), timeout=RESULT_DELIVERY_TIMEOUT_SECONDS
            )
            logger.info(
                "TASK_RESULT_DELIVERED task_id=%s occurrence_key=%s chat_id=%s",
                task.id, occurrence_key, chat_id,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.warning(
                "TASK_RESULT_DELIVERY_FAILED task_id=%s occurrence_key=%s exception=%s",
                task.id, occurrence_key, type(exc).__name__,
            )

    async def handle_failure(
        self,
        occurrence: OccurrenceRecord,
        error: BaseException | str,
        *,
        successful: int = 0,
        action_count: int = 0,
        metadata: dict[str, Any] | None = None,
        runs: list[dict[str, Any]] | None = None,
    ) -> TaskExecutionResult:
        decision = classify_failure(error)
        error_metadata = _bounded_metadata({
            "error_class": decision.reason,
            "attempt": occurrence.attempt,
            "action_count": action_count,
            "successful_action_count": successful,
            **({ACTION_RUNS_KEY: runs} if runs else {}),
        })
        if decision.classification == FailureClass.RETRYABLE and can_retry(occurrence.attempt):
            retry_at = occurrence.updated_at + retry_delay(occurrence.attempt)
            updated = await self.repository.transition_occurrence(
                self.owner_id,
                occurrence.task_id,
                occurrence.occurrence_key,
                "retry_pending",
                retry_at=retry_at,
                attempt=occurrence.attempt + 1,
                error_metadata=error_metadata,
            )
            return TaskExecutionResult(
                False,
                "retry_pending" if updated else "unknown",
                action_count,
                successful,
                decision.reason,
                metadata,
            )
        return await self._fail(
            occurrence, decision.reason, successful, action_count, metadata, runs
        )

    async def _fail(
        self,
        occurrence: OccurrenceRecord,
        error: str,
        successful: int,
        count: int,
        metadata: dict[str, Any] | None = None,
        runs: list[dict[str, Any]] | None = None,
    ) -> TaskExecutionResult:
        safe_error = str(error)[:512]
        error_metadata = _bounded_metadata({
            "error_class": safe_error,
            "attempt": occurrence.attempt,
            "action_count": count,
            "successful_action_count": successful,
            **({ACTION_RUNS_KEY: runs} if runs else {}),
        })
        updated = await self.repository.transition_occurrence(
            self.owner_id, occurrence.task_id, occurrence.occurrence_key,
            "failed", error_metadata=error_metadata,
        )
        return TaskExecutionResult(False, "failed" if updated else "unknown", count, successful, safe_error, metadata or error_metadata)
