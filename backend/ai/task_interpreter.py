"""Natural-language task interpretation without persistence or execution authority."""
from __future__ import annotations

import asyncio
import json
import logging
import re
from typing import Any

from backend.ai.providers.base.contract import ProviderResponse
from backend.ai.task_candidate import TaskCandidate, TaskCandidateError, parse_candidate_output
from backend.ai.task_contract import MAX_AI_INSTRUCTION_CHARS

logger = logging.getLogger(__name__)

INTERPRET_TIMEOUT_SECONDS = 30.0
MAX_REQUEST_CHARS = 2000

_JSON_BLOCK_RE = re.compile(r"```(?:json)?\s*(.*?)\s*```", re.DOTALL)

CANDIDATE_SCHEMA = {
    "type": "object",
    "additionalProperties": False,
    "required": ["label", "schedule_type", "schedule", "timezone", "actions", "notification_destination"],
    "properties": {
        "label": {"type": "string"},
        "schedule_type": {"type": "string", "enum": ["once", "interval", "daily", "weekly"]},
        "schedule": {"type": "object"},
        "timezone": {"type": "string"},
        "actions": {"type": "array", "maxItems": 5},
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
        raise
    return json.loads(match.group(1))


class TaskInterpreter:
    """Uses only ProviderManager.chat and returns validated candidate data."""

    def __init__(self, provider_manager: Any) -> None:
        self._providers = provider_manager

    async def interpret(self, request: str, timezone: str = "") -> TaskCandidate:
        if not isinstance(request, str) or not request.strip() or len(request) > MAX_REQUEST_CHARS:
            raise TaskInterpretationError("task request is empty or too long")
        instructions = (
            "Return exactly one JSON object matching the supplied task candidate schema. "
            "Do not include owner identity, chat ids, or raw Telegram destinations. Do not execute tools. "
            "If any required detail is ambiguous or missing, return JSON null. "
            "For any message-writing action (e.g. 'بنویس', 'بفرست', 'write', 'send'), use "
            "exactly the action name 'send_message' with a single 'text' argument containing "
            "the exact message content; the destination is fixed by the runtime and must not "
            "be included. Use no other action name for message writing. "
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
            "notification_destination to {} (empty). Never include numeric chat_ids."
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
        except Exception as exc:
            raise TaskInterpretationError("task interpretation provider failed") from exc
        if not response.success:
            raise TaskInterpretationError("task interpretation provider returned a failure")
        raw = response.text
        if not isinstance(raw, str) or not raw.strip():
            raise TaskInterpretationError("task interpretation returned no structured output")
        try:
            value = _load_candidate_json(raw)
            return parse_candidate_output(value)
        except (json.JSONDecodeError, TaskCandidateError) as exc:
            logger.info(
                "TASK_INTERPRET_REJECTED reason=candidate_invalid detail=%s",
                str(exc)[:200],
            )
            raise TaskInterpretationError("task interpretation did not return a valid candidate") from exc
        except Exception as exc:  # noqa: BLE001
            logger.warning("TASK_INTERPRET_REJECTED reason=candidate_parse_error detail=%r", exc)
            raise TaskInterpretationError("task interpretation did not return a valid candidate") from exc
