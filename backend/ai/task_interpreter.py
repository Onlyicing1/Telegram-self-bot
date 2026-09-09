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
        "schedule_type": {"type": "string", "enum": ["once", "interval", "daily", "weekly", "event"]},
        "schedule": {
            "type": "object",
            "description": (
                "interval: {'seconds': <positive number>}; "
                "once: {'at': '<naive local ISO datetime>', 'timezone': '...'}; "
                "daily: {'hour': 0-23, 'minute': 0-59, 'timezone': '...'}; "
                "weekly: {'weekday': 0-6 (0=Monday), 'hour': 0-23, 'minute': 0-59, 'timezone': '...'}; "
                "event: {'trigger': {'type': 'telegram_message', ...}} — fires when a "
                "matching Telegram message arrives (no wall-clock time)"
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
        "ai_instruction": {
            "type": "string",
            "description": (
                "For AI-generated per-run content ONLY: the user's request VERBATIM "
                "(never paraphrased or translated) so source/character/language/length "
                "requirements are enforced at each occurrence. Omit for static content."
            ),
        },
    },
}


class TaskInterpretationError(ValueError):
    """Natural-language interpretation did not yield a safe candidate."""


class TaskUnsupportedError(TaskInterpretationError):
    """The request is semantically clear but asks for a capability the
    current task/schedule/event model cannot represent (e.g. monthly/yearly
    calendar recurrence). Distinct from ambiguity so the caller can answer
    honestly instead of with the generic ambiguity rejection.
    """

    def __init__(self, capability: str) -> None:
        super().__init__(f"unsupported capability: {capability}")
        self.capability = capability


def _response_shape(value: Any) -> str:
    """Content-free classification of the provider's raw JSON for diagnostics.

    Lets one live reproduction distinguish null / object / array / string /
    unsupported-envelope responses WITHOUT logging any content: a single
    word in the AI_TASK_TRACE candidate_rejected / candidate_parsed lines.
    """
    if value is None:
        return "null"
    if isinstance(value, dict):
        if set(value) == {"unsupported"} and isinstance(value.get("unsupported"), str):
            return "unsupported"
        return "object"
    if isinstance(value, list):
        return "array"
    if isinstance(value, str):
        return "string"
    return f"other:{type(value).__name__}"


def _outer_object_span(raw: str) -> tuple[int, int] | None:
    """Locate the outermost balanced {...} span in prose, string-aware.

    Deterministic character scan (no regex): tracks JSON string literals and
    escapes so braces INSIDE string values (e.g. inside the verbatim
    ai_instruction) never confuse the depth counter. Returns None when no
    balanced object exists.
    """
    start = raw.find("{")
    if start < 0:
        return None
    depth = 0
    in_string = False
    escaped = False
    for i in range(start, len(raw)):
        ch = raw[i]
        if in_string:
            if escaped:
                escaped = False
            elif ch == "\\":
                escaped = True
            elif ch == '"':
                in_string = False
            continue
        if ch == '"':
            in_string = True
        elif ch == "{":
            depth += 1
        elif ch == "}":
            depth -= 1
            if depth == 0:
                return (start, i + 1)
    return None


def _load_candidate_json(raw: str) -> Any:
    """Parse the model's JSON, tolerating the wrapper shapes the provider
    contract legitimately permits.

    The provider adapters deliver the model's output as ONE plain text
    string (Gemini joins text parts, OpenAI-compat uses message content) and
    the interpreter prompt does not forbid prose around the object, so the
    following deterministic tolerances are applied — each one still feeds
    the FULL candidate validation afterwards, so nothing is weakened:

    1. the entire raw response (control characters like literal newlines in
       strings accepted via ``strict=False`` — the common multi-line
       ai_instruction escape failure);
    2. one markdown-fenced JSON block (with or without the ``json`` tag);
    3. prose-wrapped UNFENCED JSON ("Here is the JSON: {...}") via a
       string-aware outer-brace span;
    4. double-encoded JSON (a JSON string whose content is itself JSON).

    Anything else re-raises the real JSONDecodeError against the raw text so
    the caller can classify it with the parser's own line/column metadata.
    """
    if not isinstance(raw, str) or not raw.strip():
        raise json.JSONDecodeError("empty response", raw if isinstance(raw, str) else "", 0)
    try:
        decoded = json.loads(raw, strict=False)
    except json.JSONDecodeError:
        decoded = None
    if isinstance(decoded, (dict, list)):
        return decoded
    if isinstance(decoded, str) and decoded.strip():
        # Double-encoded JSON: the top level is a JSON string whose content
        # is itself the candidate object. Unwrap exactly one level.
        try:
            inner = json.loads(decoded, strict=False)
        except json.JSONDecodeError:
            inner = None
        if isinstance(inner, (dict, list)):
            return inner
    match = _JSON_BLOCK_RE.search(raw)
    if match:
        try:
            return json.loads(match.group(1), strict=False)
        except json.JSONDecodeError:
            pass
    span = _outer_object_span(raw)
    if span is not None:
        try:
            return json.loads(raw[span[0]:span[1]], strict=False)
        except json.JSONDecodeError:
            pass
    # Re-parse without tolerance so the raised error is the real
    # JSONDecodeError for the raw text (with lineno/colno/pos metadata).
    return json.loads(raw)


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
            "SEMANTIC INTERPRETATION: interpret the user's INTENT in Persian or English; "
            "never require exact phrasing, a fixed template, or a specific word order. "
            "Multi-line requests and colloquial phrasings are normal. A clear action plus "
            "any recognizable schedule/trigger expression is a valid task — unfamiliar "
            "wording is not ambiguity. "
            "NULL RULE: return JSON null ONLY when the message has NO recognizable "
            "schedule/trigger expression AND no clear action (pure chit-chat). Never "
            "return null for a clear interval, time, or event phrase just because the "
            "wording is unusual, the number is written as a word, 'every'/'هر' is omitted, "
            "or the request spans multiple lines. "
            "UNSUPPORTED CAPABILITY: if the request is semantically CLEAR but asks for a "
            "capability this schema cannot express — calendar month/year recurrence "
            "('هر ماه', 'ماهانه', 'monthly', 'هر سال', 'سالانه', 'yearly', 'every year', "
            "'اول هر ماه', 'first of month', '15th of month', 'every year on January 1', "
            "'هر آخر هفته', 'every weekend') — return EXACTLY the single JSON object "
            "{\"unsupported\": \"<short capability name>\"} and nothing else. Do not "
            "fabricate seconds for months/years and do not return null for them. "
            "ACTION OBJECT CONTRACT: every element of 'actions' MUST be an object of the "
            "exact form {'name': <action name>, 'arguments': <object>} — a 'name' string "
            "plus an 'arguments' object, no other keys inside the action object. Example: "
            "{'name': 'send_message', 'arguments': {'text': 'hello'}}. For any "
            "message-writing action (e.g. 'بنویس', 'بفرست', 'write', 'send'), use "
            "exactly the action name 'send_message' with arguments carrying a single "
            "bounded 'text' key containing the exact message content; the destination "
            "is fixed by the runtime and must not be included. Use no other action name "
            "for message writing. "
            "PROFILE ACTIONS: for bio/profile updates ('تو بیو بزارش', 'توی بیو بزاری', "
            "'بیو رو عوض کن', 'بیو پروفایلم رو آپدیت کن', 'update my bio', 'آپدیت کن بیو "
            "پروفایلم'), use exactly the REGISTERED action "
            "name 'bio_set_text' with arguments {'text': ''} — EMPTY, because the content is "
            "AI-generated per occurrence under ai_instruction; never bake a finished sentence "
            "into the arguments. For first_name/username changes use exactly 'username_set_text' "
            "with {'text': ''}. Never invent other action names: an action name that is not "
            "registered is rejected. Keep exactly one action object in 'actions'. "
            "AI-GENERATED CONTENT CONTRACT: when the task's content must be generated "
            "or varied at each run — random dialogues or quotes from a specific "
            "person/character/source, a fresh bio each time, any request like 'random X "
            "from Y' or 'change my bio to ...' — DO NOT bake one fixed text into the "
            "action arguments. Instead keep the content arguments minimal (empty text) "
            "and add the field 'ai_instruction' at the TOP LEVEL of the candidate with "
            "the user's request VERBATIM — never paraphrased, never translated, never "
            "shortened — so the exact source/person/character, language and length "
            "requirements survive word-for-word; the runtime generates and validates "
            "the content at each occurrence and rejects unrelated characters. A request "
            "for 'a dialogue from <X>' must keep '<X>' inside ai_instruction exactly as "
            "spoken. When you embed the request in 'ai_instruction', escape line breaks "
            "as \\n inside the JSON string so the JSON stays valid. Only truly static "
            "one-time content (say exactly 'hello') omits "
            "ai_instruction. "
            "SCHEDULE CONTRACT: 'schedule' must match the schedule_type exactly — "
            "interval: {'seconds': <positive number>} (every X minutes = X*60 seconds, "
            "e.g. 'هر سه دقیقه' or 'every 3 minutes' = {'seconds': 180}); "
            "once: {'at': '<naive local ISO datetime>', 'timezone': '<IANA tz>'}; "
            "daily: {'hour': 0-23, 'minute': 0-59, 'timezone': '<IANA tz>'}; "
            "weekly: {'weekday': 0-6 (0=Monday), 'hour': 0-23, 'minute': 0-59, "
            "'timezone': '<IANA tz>'}. Do not put unit names like 'minutes' inside "
            "the schedule object — convert them to seconds yourself. "
            "COMPOUND INTERVALS: 'every 1 hour and 30 minutes', 'هر 1 ساعت و 30 دقیقه', "
            "'هر یک ساعت و نیم' → compute the total yourself ({'seconds': 5400}); you "
            "may also emit the structured compound form {'hours': 1, 'minutes': 30} — "
            "the runtime sums the known units. 'هر نیم ساعت' / 'every half hour' = "
            "{'seconds': 1800}. Never use month or year as a duration unit (see "
            "UNSUPPORTED CAPABILITY). "
            "TIME-OF-DAY & CALENDAR TRIGGERS: 'today at 5' / 'امروز ساعت 5' → once at "
            "today 17:00; 'tomorrow at 8 AM' / 'فردا ساعت 8 صبح' → once tomorrow 08:00; "
            "'every day at 9' / 'هر روز ساعت 9' / 'روزانه' → daily; 'every night at "
            "11' / 'هر شب ساعت 11' → daily 23:00; 'every Monday at 10' / 'هر دوشنبه "
            "ساعت 10' → weekly. WEEKDAY NUMBERS (0=Monday .. 6=Sunday): دوشنبه=0، "
            "سه‌شنبه=1، چهارشنبه=2، پنجشنبه=3، جمعه=4، شنبه=5، یکشنبه=6؛ English: "
            "Monday=0 .. Sunday=6. Time-of-day uses the 24-hour clock. Monthly/yearly "
            "calendar triggers are UNSUPPORTED (see UNSUPPORTED CAPABILITY). "
            "EVENT SCHEDULES (trigger type): use schedule_type 'event' ONLY when the user "
            "explicitly asks for an automation that reacts to a Telegram message "
            "(e.g. 'وقتی جان پیام داد جوابش بده', 'when John sends me a message reply using X', "
            "'هر وقت از این چت پیام اومد', 'when I receive a message from this chat containing "
            "urgent'). The schedule must be {'trigger': {'type': 'telegram_message', ...}} with "
            "the allowed trigger fields: 'sender' (a display NAME such as 'John' or 'علی' — "
            "never a numeric id), 'chat' (a chat name, or 'this chat'/'همین چت' for the current "
            "conversation — never a numeric id; channel names work for 'وقتی کانال X پست گذاشت' / "
            "'when channel X posts' — a channel post arrives as a message in that chat), "
            "'contains' (list of substrings, all must appear), "
            "'text_equals', 'starts_with', 'has_media' (boolean — ANY media), 'media_type' "
            "(one of photo, video, voice, audio, document, sticker, animation — 'عکس'/'photo' "
            "→ 'photo', 'ویدیو'/'video' → 'video', 'فایل'/'file' → 'document'), 'is_reply' "
            "(boolean — 'وقتی جواب دادن' / 'when someone replies'), 'is_mention' (boolean — "
            "'وقتی کسی منو منشن کرد' / 'when someone mentions me'), and "
            "'direction' ('incoming' default for 'وقتی X بهم پیام داد' / 'when X messages me'; "
            "'outgoing' for 'وقتی من نوشتم X' / 'when I write X'; 'any'). Include at least one "
            "condition. Never invent sender/chat ids — the runtime resolves names. "
            "DO NOT use schedule_type 'event' for time-based requests; those stay once/interval/"
            "daily/weekly. "
            "\n\n"
            "INTERVAL RECOGNITION (semantic — do NOT require one fixed sentence shape): any "
            "expression of 'every N <unit>', 'once every N <unit>', 'N <unit> once', or Persian "
            "'هر N <unit>' / 'N <unit> یک بار' / 'N <unit> یه بار' with N written as digits "
            "(Latin, Persian, or Arabic-Indic) OR a number word (Persian: یک، دو، سه، چهار، "
            "پنج، شش، هفت، هشت، نه، ده، بیست، سی، شصت / English: one..ten, fifteen, twenty, "
            "thirty, sixty) and a unit (دقیقه/ساعت/روز/هفته/ثانیه or minute(s)/hour(s)/day(s)/"
            "week(s)/second(s)) is a CLEAR recurring interval. 'هر پنج دقیقه' and 'every five "
            "minutes' are exactly as valid as 'هر 5 دقیقه'. Requests may span multiple lines and "
            "the interval may appear on its own line ('هر ۵ دقیقه' alone) with the action in "
            "following lines. Convert to {'seconds': N} yourself (5 minutes = 300; 300 seconds = "
            "300). Only return null when NO schedule expression exists at all — never for a "
            "clear interval phrase. The words 'هر' (every), 'بار' (time/occasion), 'دقیقه' "
            "(minute), 'ساعت' (hour), 'روز' (day), 'هفته' (week), 'ماه' (month) together with "
            "an action verb indicate a recurring task. "
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
            "\n\n"
            "EXAMPLE (mirrors the multi-line Persian structure): user writes:\n"
            "هر ۵ دقیقه\n"
            "میخوام بیو پروفایلم رو آپدیت کنید\n"
            "یه دیالوگ رندوم از کاراکتر آیانامی ری از انیمه نئون جنسیس بزاری\n"
            "که زیر 60 کاراکتر باشه\n"
            "Return: {\"label\": \"Bio update\", \"schedule_type\": \"interval\", "
            "\"schedule\": {\"seconds\": 300}, \"timezone\": \"Asia/Tehran\", "
            "\"actions\": [{\"name\": \"bio_set_text\", \"arguments\": {\"text\": \"\"}}], "
            "\"notification_destination\": {}, \"ai_instruction\": \"<the user's request "
            "VERBATIM, including the Persian text exactly as written>\"}."
        )
        if isinstance(timezone, str) and timezone.strip():
            instructions += (
                f" Use the IANA timezone '{timezone.strip()}' for the candidate's "
                "TOP-LEVEL 'timezone' field — it is REQUIRED by the schema for every "
                "schedule type. Only the SCHEDULE OBJECT itself carries no timezone "
                "field for interval schedules."
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
            raise TaskInterpretationError(
                "task interpretation returned no structured output (empty response)"
            )
        value: Any = None
        shape = "unknown"
        try:
            value = _load_candidate_json(raw)
            shape = _response_shape(value)
            # Semantically clear but unrepresentable capability: the model
            # returns {"unsupported": "..."} — surfaced distinctly from
            # ambiguity so the caller can answer honestly.
            if (
                isinstance(value, dict)
                and set(value) == {"unsupported"}
                and isinstance(value.get("unsupported"), str)
                and value["unsupported"].strip()
            ):
                raise TaskUnsupportedError(value["unsupported"].strip()[:200])
            candidate = parse_candidate_output(value)
        except TaskUnsupportedError:
            raise
        except (json.JSONDecodeError, TaskCandidateError) as exc:
            if shape == "unknown":
                shape = "malformed"
            json_truncated = False
            if isinstance(exc, json.JSONDecodeError) and isinstance(raw, str):
                json_truncated = (
                    "Unterminated" in str(exc)
                    or (exc.pos >= max(0, len(raw) - 8) and len(raw) >= 20)
                )
                finish = meta.get("finish_reason")
                provider_truncated = (
                    isinstance(finish, str)
                    and finish.upper() in ("MAX_TOKENS", "LENGTH")
                )
                logger.info(
                    "AI_TASK_TRACE request_id=%s stage=candidate_rejected "
                    "response_shape=malformed json_error=%s line=%s col=%s "
                    "pos=%s raw_len=%s truncated=%s provider_finish_reason=%s",
                    request_id or "-", type(exc).__name__, exc.lineno,
                    exc.colno, exc.pos, len(raw),
                    bool(json_truncated or provider_truncated),
                    finish if isinstance(finish, str) else "-",
                )
            elif isinstance(value, dict):
                actions = value.get("actions")
                logger.info(
                    "AI_TASK_TRACE request_id=%s stage=candidate_rejected "
                    "response_shape=%s reason=%s "
                    "candidate_type=object action_count=%s "
                    "action_field_names=%s schedule_type=%s",
                    request_id or "-", shape, str(exc)[:260],
                    len(actions) if isinstance(actions, list) else "-",
                    (",".join(sorted(actions[0])) if isinstance(actions, list) and actions
                     and isinstance(actions[0], dict) else "-"),
                    value.get("schedule_type", "-"),
                )
            else:
                logger.info(
                    "AI_TASK_TRACE request_id=%s stage=candidate_rejected "
                    "response_shape=%s reason=%s",
                    request_id or "-", shape, str(exc)[:260],
                )
            logger.info(
                "TASK_INTERPRET_REJECTED reason=candidate_invalid detail=%s",
                str(exc)[:200],
            )
            json_suffix = (
                f" json_truncated={json_truncated}"
                if isinstance(exc, json.JSONDecodeError)
                else ""
            )
            raise TaskInterpretationError(
                f"task interpretation did not return a valid candidate "
                f"(response_shape={shape}{json_suffix})"
            ) from exc
        except Exception as exc:  # noqa: BLE001
            logger.warning(
                "TASK_INTERPRET_REJECTED reason=candidate_parse_error response_shape=%s detail=%r",
                shape, exc,
            )
            raise TaskInterpretationError("task interpretation did not return a valid candidate") from exc
        finish = meta.get("finish_reason")
        logger.info(
            "AI_TASK_TRACE request_id=%s stage=candidate_parsed response_shape=%s "
            "candidate_type=%s "
            "action_count=%s action_field_names=%s schedule_type=%s timezone=%s "
            "destination_keys=%s provider_finish_reason=%s",
            request_id or "-", shape, "object",
            len(candidate.actions),
            ",".join(sorted(candidate.actions[0])) if candidate.actions else "-",
            candidate.schedule_type, candidate.timezone,
            ",".join(sorted(candidate.notification_destination)) or "-",
            finish if isinstance(finish, str) else "-",
        )
        logger.info(
            "AI_TASK_TRACE request_id=%s stage=interpretation_end success=true "
            "schedule_type=%s action_count=%s provider=%s latency_ms=%s",
            request_id or "-", candidate.schedule_type, len(candidate.actions),
            response.provider_name or "unknown", int((time.perf_counter() - started) * 1000),
        )
        return candidate
