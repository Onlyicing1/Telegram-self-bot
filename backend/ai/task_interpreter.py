"""Natural-language task interpretation without persistence or execution authority."""
from __future__ import annotations

import asyncio
import json
import logging
import re
import time
from typing import Any

from backend.ai.providers.base.contract import ProviderResponse
from backend.ai.task_candidate import TaskCandidate, TaskCandidateError, parse_candidate_output
from backend.ai.task_contract import MAX_AI_INSTRUCTION_CHARS

logger = logging.getLogger(__name__)

INTERPRET_TIMEOUT_SECONDS = 30.0
MAX_REQUEST_CHARS = 2000

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

_SCHEDULE_INTERVAL = {
    "type": "object", "additionalProperties": False,
    "required": ["seconds"],
    "properties": {"seconds": {"type": "number", "exclusiveMinimum": 0}},
}
_SCHEDULE_ONCE = {
    "type": "object", "additionalProperties": False,
    "required": ["at", "timezone"],
    "properties": {
        "at": {"type": "string", "description": "naive local datetime, ISO 8601, e.g. 2026-09-03T09:00:00"},
        "timezone": {"type": "string"},
    },
}
_SCHEDULE_DAILY = {
    "type": "object", "additionalProperties": False,
    "required": ["hour", "timezone"],
    "properties": {
        "hour": {"type": "integer", "minimum": 0, "maximum": 23},
        "minute": {"type": "integer", "minimum": 0, "maximum": 59},
        "second": {"type": "integer", "minimum": 0, "maximum": 59},
        "timezone": {"type": "string"},
    },
}
_SCHEDULE_WEEKLY = {
    "type": "object", "additionalProperties": False,
    "required": ["weekday", "hour", "timezone"],
    "properties": {
        "weekday": {"type": "integer", "minimum": 0, "maximum": 6,
                     "description": "0=Monday .. 6=Sunday"},
        "hour": {"type": "integer", "minimum": 0, "maximum": 23},
        "minute": {"type": "integer", "minimum": 0, "maximum": 59},
        "second": {"type": "integer", "minimum": 0, "maximum": 59},
        "timezone": {"type": "string"},
    },
}

CANDIDATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["label", "schedule_type", "schedule", "timezone", "actions", "notification_destination"],
    "properties": {
        "label": {"type": "string"},
        "schedule_type": {"type": "string", "enum": ["once", "interval", "daily", "weekly"]},
        "schedule": {
            "type": "object",
            "description": (
                "interval: {'seconds': <positive number>}; "
                "once: {'at': '<naive local ISO datetime>', 'timezone': '...'}; "
                "daily: {'hour': 0-23, 'minute': 0-59, 'timezone': '...'}; "
                "weekly: {'weekday': 0-6 (0=Monday), 'hour': 0-23, 'minute': 0-59, 'timezone': '...'}"
            ),
        },
        "timezone": {"type": "string"},
        "actions": {
            "type": "array",
            "maxItems": 5,
            "minItems": 1,
            "items": {
                "type": "object",
                "additionalProperties": False,
                "required": ["name", "arguments"],
                "properties": {
                    "name": {"type": "string"},
                    "arguments": {"type": "object"},
                },
            },
        },
        "notification_destination": {"type": "object"},
    },
}


class TaskInterpretationError(ValueError):
    """Natural-language interpretation did not yield a safe candidate."""


def _load_candidate_json(raw: str) -> Any:
    """Parse the model's JSON, tolerating the common markdown-fence wrapper.

    Providers frequently wrap a compliant JSON object in ``` fences (with or
    without the ``json`` tag). The candidate itself is still validated by
    ``parse_candidate_output`` — this only fixes the extraction step.
    """
    try:
        return json.loads(raw)
    except json.JSONDecodeError:
        pass
    match = _JSON_BLOCK_RE.search(raw)
    if not match:
        # Re-parse without fences so the raised error is the real
        # JSONDecodeError (a bare `raise` here would surface as a generic
        # RuntimeError and hide the parse failure from diagnostics).
        return json.loads(raw)
    return json.loads(match.group(1))


class TaskInterpreter:
    """Uses only ProviderManager.chat and returns validated candidate data."""

    def __init__(self, provider_manager: Any) -> None:
        self._providers = provider_manager

    async def interpret(self, request: str, timezone: str = "", request_id: str = "") -> TaskCandidate:
        started = time.perf_counter()
        if not isinstance(request, str) or not request.strip() or len(request) > MAX_REQUEST_CHARS:
            raise TaskInterpretationError("task request is empty or too long")
        logger.info(
            "AI_TASK_TRACE request_id=%s stage=interpretation_start mode=provider request_len=%s",
            request_id or "-", len(request),
        )
        instructions = (
            "Return exactly one JSON object matching the supplied task candidate schema. "
            "Do not include owner identity, chat ids, or raw Telegram destinations. Do not execute tools. "
            "If any required detail is ambiguous or missing, return JSON null. "
            "ACTION OBJECT CONTRACT: every element of 'actions' MUST be an object of the "
            "exact form {'name': <action name>, 'arguments': <object>} — a 'name' string "
            "plus an 'arguments' object, no other keys inside the action object. Example: "
            "{'name': 'send_message', 'arguments': {'text': 'hello'}}. For any "
            "message-writing action (e.g. 'بنویس', 'بفرست', 'write', 'send'), use "
            "exactly the action name 'send_message' with arguments carrying a single "
            "bounded 'text' key containing the exact message content; the destination "
            "is fixed by the runtime and must not be included. Use no other action name "
            "for message writing. Keep exactly one action object in 'actions'. "
            "SCHEDULE CONTRACT: 'schedule' must match the schedule_type exactly — "
            "interval: {'seconds': <positive number>} (every X minutes = X*60 seconds, "
            "e.g. 'هر سه دقیقه' or 'every 3 minutes' = {'seconds': 180}); "
            "once: {'at': '<naive local ISO datetime>', 'timezone': '<IANA tz>'}; "
            "daily: {'hour': 0-23, 'minute': 0-59, 'timezone': '<IANA tz>'}; "
            "weekly: {'weekday': 0-6 (0=Monday), 'hour': 0-23, 'minute': 0-59, "
            "'timezone': '<IANA tz>'}. Do not put unit names like 'minutes' inside "
            "the schedule object — convert them to seconds yourself. "
            "\n\n"
            "PERSIAN INTERVAL RECOGNITION: Recognize common Persian interval phrases as scheduling requests. "
            "Examples: 'هر 1 دقیقه یک بار بنویس سلام' (every 1 minute write hello), "
            "'هر 5 دقیقه بنویس hello' (every 5 minutes write hello), "
            "'هر یک دقیقه برای من بنویس سلام' (every 1 minute for me write hello), "
            "'هر ده دقیقه یک پیام بفرست' (every 10 minutes send a message), "
            "'هر ساعت یادآوری کن' (every hour remind). "
            "The words 'هر' (every), 'bar' (بار = time/occasion), 'دقیقه' (minute), "
            "'ساعت' (hour), 'روز' (day), 'هفته' (week), 'ماه' (month) together with "
            "an action verb clearly indicate a recurring task. "
            "\n\n"
            "EXPLICIT DESTINATION (optional): If the user specifies a chat name for the destination "
            "(e.g. 'در OskarBeam بنویس سلام', 'in OskarBeam write hello'), include it in the "
            "notification_destination as {'chat_name': 'OskarBeam'}. Use the exact chat name as spoken. "
            "If no chat is specified, the destination is the current chat — set "
            "notification_destination to {} (empty). Never include numeric chat_ids. "
            "\n\n"
            "DESTINATION FLAGS (both optional, both default false): inside notification_destination "
            "set 'deliver_result': true ONLY when the user asks to see the task's result when it runs "
            "(e.g. 'show me the latest tasks', 'به من نشون بده', 'نمایش بده') and the action is a "
            "read/report action (task_list, list_saves, search, ...) — never for message-writing "
            "actions. Set 'notify_on_outcome': true ONLY when the user explicitly asks to be notified "
            "when the task runs or fails ('notify me', 'خبرم کن', 'به من اطلاع بده'). Otherwise omit "
            "both flags: scheduled execution must stay silent by default."
        )
        if isinstance(timezone, str) and timezone.strip():
            instructions += (
                f" Use the IANA timezone '{timezone.strip()}' for both the schedule "
                "timezone and the task timezone; interval schedules carry no timezone field."
            )
        messages = [
            {"role": "system", "content": instructions},
            {"role": "system", "content": json.dumps(CANDIDATE_SCHEMA, separators=(",", ":"))},
            {"role": "user", "content": request.strip()},
        ]
        try:
            response: ProviderResponse = await asyncio.wait_for(
                self._providers.chat(messages, tools=[]), timeout=INTERPRET_TIMEOUT_SECONDS
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            raise TaskInterpretationError("task interpretation provider timed out") from exc
        except Exception as exc:
            raise TaskInterpretationError("task interpretation provider failed") from exc
        if not response.success:
            # The concrete provider failure (rate limit, model-not-found,
            # exhausted fallback chain, …) must survive into the raised
            # error — a generic message here is what previously hid the
            # root cause from the create_task trace.
            meta = response.metadata or {}
            category = (
                "all_providers_failed" if meta.get("fallback_exhausted")
                else str(meta.get("failure_type") or meta.get("error_type") or "unknown")
            )
            detail = " ".join(str(response.text or "").split())[:200]
            matrix = meta.get("provider_matrix") or []
            failed_providers = ",".join(
                str(entry.get("provider")) for entry in matrix
                if isinstance(entry, dict) and entry.get("provider")
            )
            logger.warning(
                "AI_TASK_TRACE request_id=%s stage=provider_result success=false "
                "provider=%s attempted=%s category=%s providers_tried=%s "
                "fallback_exhausted=%s detail=%s",
                request_id or "-",
                response.provider_name or "unknown",
                failed_providers or response.provider_name or "unknown",
                category, len(matrix), bool(meta.get("fallback_exhausted")),
                detail,
            )
            raise TaskInterpretationError(
                f"task interpretation provider failed: provider={response.provider_name} "
                f"category={category} detail={detail}"
            )
        meta = response.metadata or {}
        logger.info(
            "AI_TASK_TRACE request_id=%s stage=provider_result success=true "
            "provider=%s model=%s fallback=%s latency_ms=%s",
            request_id or "-", response.provider_name or "unknown",
            meta.get("model") or "-", bool(meta.get("fallback")),
            int((time.perf_counter() - started) * 1000),
        )
        raw = response.text
        if not isinstance(raw, str) or not raw.strip():
            raise TaskInterpretationError("task interpretation returned no structured output")
        value: Any = None
        try:
            value = _load_candidate_json(raw)
            candidate = parse_candidate_output(value)
        except (json.JSONDecodeError, TaskCandidateError) as exc:
            if isinstance(value, dict):
                actions = value.get("actions")
                logger.info(
                    "AI_TASK_TRACE request_id=%s stage=candidate_rejected reason=%s "
                    "candidate_type=object action_count=%s "
                    "action_field_names=%s schedule_type=%s",
                    request_id or "-", str(exc)[:260],
                    len(actions) if isinstance(actions, list) else "-",
                    (",".join(sorted(actions[0])) if isinstance(actions, list) and actions
                     and isinstance(actions[0], dict) else "-"),
                    value.get("schedule_type", "-"),
                )
            logger.info(
                "TASK_INTERPRET_REJECTED reason=candidate_invalid detail=%s",
                str(exc)[:200],
            )
            raise TaskInterpretationError("task interpretation did not return a valid candidate") from exc
        except Exception as exc:  # noqa: BLE001
            logger.warning("TASK_INTERPRET_REJECTED reason=candidate_parse_error detail=%r", exc)
            raise TaskInterpretationError("task interpretation did not return a valid candidate") from exc
        logger.info(
            "AI_TASK_TRACE request_id=%s stage=candidate_parsed candidate_type=%s "
            "action_count=%s action_field_names=%s schedule_type=%s timezone=%s "
            "destination_keys=%s",
            request_id or "-", "object", len(candidate.actions),
            ",".join(sorted(candidate.actions[0])) if candidate.actions else "-",
            candidate.schedule_type, candidate.timezone,
            ",".join(sorted(candidate.notification_destination)) or "-",
        )
        logger.info(
            "AI_TASK_TRACE request_id=%s stage=interpretation_end success=true "
            "schedule_type=%s action_count=%s provider=%s latency_ms=%s",
            request_id or "-", candidate.schedule_type, len(candidate.actions),
            response.provider_name or "unknown", int((time.perf_counter() - started) * 1000),
        )
        return candidate
