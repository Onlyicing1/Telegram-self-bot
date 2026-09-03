"""
Structured AI action contract — parse, validate, and resolve executable intent.

The AI model is an INTENT interpreter, never an executor. This module turns
the model's structured output (a native tool call, or a JSON action object
embedded in the text response) into concrete ``tool_calls`` for the EXISTING
``ToolExecutor``. The model's output is never trusted as executable code:

  parse → validate (action/fields/count/target) → resolve target
        → existing tool call → existing service → real result

Unknown actions, unknown fields, invalid counts, and unsupported targets are
rejected locally. Only a narrow allowlist of actions reaches the executor, and
each mapped action delegates to an existing LifeOS tool/service — no new
executor, no direct Telegram access.
"""
from __future__ import annotations

import json
import re
from dataclasses import dataclass, field
from typing import Any

from backend.ai.persian import coerce_int, normalize_digits
from backend.ai.semantic_delete import parse_structural_predicate, spec_from_dict
from backend.ai.tools.message import MAX_SEND_TEXT_CHARS

# ── Action vocabulary ──

# Recognized action names. EXECUTABLE_ACTION_NAMES map to an existing tool;
# the others are recognized but deliberately have no executor wired.
ACTION_NAMES = frozenset({
    "save",
    "deep_save",
    "save_link",
    "delete_messages",
    "create_task",
    "list_saved_items",
    "search_saved_items",
    "list_recent_messages",
    "database_stats",
    "bio_status",
    "get_bio",
    "username_status",
    "account_status",
    "task_list",
    "task_inspect",
    "task_transition",
    "retrieve_save",
    "send",
    "clean_chat",
    "remember",
    "clarify",
})

EXECUTABLE_ACTION_NAMES = frozenset({
    "save",
    "deep_save",
    "save_link",
    "delete_messages",
    "create_task",
    "send",
    "list_saved_items",
    "search_saved_items",
    "list_recent_messages",
    "database_stats",
    "bio_status",
    "get_bio",
    "username_status",
    "account_status",
    "task_list",
    "task_inspect",
    "task_transition",
    "retrieve_save",
})

# Read-only status/query actions: no target — the mapped tool reads the
# owner's own saved-items DB, task list, profile-engine state, or REAL
# Telegram chat history. ``list_recent_messages`` additionally accepts an
# optional limit.
_STATUS_ACTIONS = frozenset({
    "list_saved_items",
    "search_saved_items",
    "list_recent_messages",
    "database_stats",
    "bio_status",
    "get_bio",
    "username_status",
    "account_status",
    "task_list",
})

TARGET_SCOPES = frozenset({
    "replied_message",
    "current_message",
    "last_message",
    "recent_messages",
    "saved_item",
    "message_id",
})

# Fields the schema accepts. Anything else is rejected so an LLM can never
# smuggle an unknown field through to execution.
ALLOWED_FIELDS = frozenset({
    "action", "target", "count", "mode", "caption", "recipient", "query",
    "content", "reason", "link", "message_id", "fields", "request",
    "until_time", "after_time", "boundary_id", "semantic", "text",
    "task_id", "action_status", "expected_version", "save_code", "status",
})

# Identity fields the account_status action may request from account_show.
# Everything else (phone, account ID, session data, credentials) is rejected.
_ACCOUNT_IDENTITY_FIELDS = frozenset({"first_name", "last_name", "full_name", "username"})

_MIN_DELETE_COUNT = 1
_MAX_DELETE_COUNT = 500

# A Telegram message link, with or without the https:// scheme. The URL is
# preserved verbatim — only trailing punctuation is stripped for parsing.
_TELEGRAM_LINK_RE = re.compile(r"(?:https?://)?(?:t|telegram)\.me/\S+")


def _extract_telegram_link(text: str) -> str | None:
    """Extract the first Telegram message link from *text* (exact URL)."""
    if not isinstance(text, str):
        return None
    m = _TELEGRAM_LINK_RE.search(text)
    if not m:
        return None
    url = m.group(0).strip()
    return url.rstrip(".,;:)!?]}>\"'") or None

# ── Parse outcome kinds ──

KIND_CONVERSATIONAL = "conversational"   # prose, no action
KIND_EXECUTABLE = "executable"           # validated + resolved to tool calls
KIND_CLARIFY = "clarify"                 # model asked for clarification
KIND_INVALID = "invalid"                 # rejected locally (unknown/field/count)
KIND_UNSUPPORTED = "unsupported"         # recognized action, no executor


@dataclass(frozen=True)
class ActionParseResult:
    """Result of parsing and validating one model output.

    ``tool_calls`` is populated only for ``executable`` results and always
    contains the concrete tool name + arguments understood by the existing
    ``ToolExecutor`` (e.g. ``{"name": "save", "arguments": {}}``).
    """

    kind: str
    action: str = ""
    target: str = ""
    count: int | None = None
    caption: bool = False
    reason: str = ""
    error: str = ""
    link: str = ""
    message_id: int | None = None
    query: str = ""
    fields: list[str] | None = None
    mode: str = ""
    until_time: str = ""
    after_time: str = ""
    boundary_id: int | None = None
    semantic: dict[str, Any] | None = None
    schedule_text: str = ""
    text: str = ""
    task_id: int | None = None
    action_status: str = ""
    expected_version: int | None = None
    save_code: str = ""
    status: str = ""
    tool_calls: list[dict[str, Any]] = field(default_factory=list)


# ── Parsing ──


def extract_json_object(text: str) -> dict[str, Any] | None:
    """Extract the first JSON object from model text, tolerating fences/prose.

    Returns ``None`` when no JSON object is present (conversational prose).
    """
    if not isinstance(text, str):
        return None
    text = text.strip()
    if not text:
        return None

    # Strip a markdown code fence if present.
    if text.startswith("```"):
        lines = text.splitlines()
        if lines and lines[0].strip().startswith("```"):
            lines = lines[1:]
        if lines and lines[-1].strip() == "```":
            lines = lines[:-1]
        text = "\n".join(lines).strip()

    candidates = [text]
    start = text.find("{")
    end = text.rfind("}")
    if start != -1 and end != -1 and end > start:
        candidates.append(text[start:end + 1])

    for candidate in candidates:
        if not candidate:
            continue
        try:
            obj = json.loads(candidate)
        except (json.JSONDecodeError, TypeError):
            continue
        if isinstance(obj, dict):
            return obj
    return None


# ── Validation ──


def validate_action(raw: dict[str, Any]) -> ActionParseResult:
    """Validate a raw action object. Never raises; never executes.

    Rejects unknown fields, unknown actions, invalid targets, and invalid
    counts. Returns a structured ``ActionParseResult`` with the normalized
    action, target, count, and (for executable actions) nothing yet — the
    target is resolved to tool calls by :func:`resolve_tool_calls`.
    """
    if not isinstance(raw, dict):
        return ActionParseResult(kind=KIND_INVALID, error="Action must be a JSON object.")

    unknown = sorted(set(raw) - ALLOWED_FIELDS)
    if unknown:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=f"Unknown field(s): {', '.join(unknown)}",
        )

    action = raw.get("action")
    if not isinstance(action, str) or not action.strip():
        return ActionParseResult(kind=KIND_INVALID, error="Missing 'action' field.")
    action = action.strip()

    if action not in ACTION_NAMES:
        return ActionParseResult(kind=KIND_INVALID, error=f"Unknown action: {action}")

    if action == "clarify":
        return ActionParseResult(
            kind=KIND_CLARIFY,
            action=action,
            reason=str(raw.get("reason", "") or ""),
        )

    if action not in EXECUTABLE_ACTION_NAMES:
        return ActionParseResult(kind=KIND_UNSUPPORTED, action=action)

    # ``fields`` is only meaningful for account_status (which fields of the
    # account identity to return). Reject it for every other action so the
    # model can never smuggle an unexpected field through to execution.
    if "fields" in raw and action != "account_status":
        return ActionParseResult(
            kind=KIND_INVALID,
            error="'fields' is only valid for the account_status action.",
        )

    # ``save_code`` is only meaningful for retrieve_save. It is validated as
    # a bounded string here; the canonical `S####` shape is enforced by the
    # tool (the code travels verbatim, upper-cased at the service boundary).
    if "save_code" in raw and action != "retrieve_save":
        return ActionParseResult(
            kind=KIND_INVALID,
            error="'save_code' is only valid for the retrieve_save action.",
        )

    # ``task_id``/``expected_version``/``action`` status fields are only
    # meaningful for the task lifecycle actions.
    _TASK_ONLY_FIELDS = ("task_id", "expected_version")
    if action not in ("task_inspect", "task_transition"):
        for field_name in _TASK_ONLY_FIELDS:
            if field_name in raw:
                return ActionParseResult(
                    kind=KIND_INVALID,
                    error=(
                        f"'{field_name}' is only valid for the "
                        "task_inspect/task_transition actions."
                    ),
                )

    # ``status`` is only meaningful for the task_list action (an optional
    # status filter on the read). The lifecycle actions express their target
    # status via ``action_status`` — never via ``status``.
    if "status" in raw and action != "task_list":
        return ActionParseResult(
            kind=KIND_INVALID,
            error="'status' is only valid for the task_list action.",
        )

    if action in ("task_inspect", "task_transition"):
        return _validate_task_lifecycle_action(action, raw)

    if action == "retrieve_save":
        return _validate_retrieve_save_action(raw)

    # Read-only status/query actions map directly to an existing tool. They
    # take no target; ``search_saved_items`` requires a query,
    # ``list_recent_messages`` accepts an optional limit, and
    # ``account_status`` accepts an optional ``fields`` allowlist.
    if action in _STATUS_ACTIONS:
        # task_list accepts an OPTIONAL status filter so natural-language
        # retrieval semantics ("show completed tasks") can be expressed as a
        # validated argument instead of per-phrase vocabulary. Filtering is
        # owner-scoped inside TaskManagementService; this layer only
        # validates the enum.
        if action == "task_list":
            status = ""
            if "status" in raw:
                status_value = raw.get("status")
                if (
                    not isinstance(status_value, str)
                    or status_value.strip().lower() not in _TASK_STATUS_VOCABULARY
                ):
                    return ActionParseResult(
                        kind=KIND_INVALID,
                        error=(
                            "Invalid 'status' for task_list "
                            "(allowed: paused, active, completed)."
                        ),
                    )
                status = status_value.strip().lower()
            return ActionParseResult(kind=KIND_EXECUTABLE, action=action, status=status)
        if action == "search_saved_items":
            query = raw.get("query")
            if not isinstance(query, str) or not query.strip():
                return ActionParseResult(
                    kind=KIND_INVALID,
                    error="Missing 'query' field.",
                )
            return ActionParseResult(kind=KIND_EXECUTABLE, action=action, query=query.strip())
        if action == "list_recent_messages":
            count: int | None = None
            if "count" in raw:
                count = coerce_int(raw.get("count"))
                if count is None or count < _MIN_DELETE_COUNT or count > _MAX_DELETE_COUNT:
                    return ActionParseResult(
                        kind=KIND_INVALID,
                        error=f"Invalid count: {raw.get('count')!r} (must be 1-{_MAX_DELETE_COUNT}).",
                    )
            return ActionParseResult(kind=KIND_EXECUTABLE, action=action, count=count)
        if action == "account_status":
            fields: list[str] | None = None
            if "fields" in raw:
                raw_fields = raw.get("fields")
                if (
                    not isinstance(raw_fields, list)
                    or not raw_fields
                    or not all(isinstance(f, str) and f in _ACCOUNT_IDENTITY_FIELDS for f in raw_fields)
                ):
                    return ActionParseResult(
                        kind=KIND_INVALID,
                        error="Invalid 'fields' for account_status (allowed: first_name, last_name, full_name, username).",
                    )
                fields = list(dict.fromkeys(raw_fields))
            return ActionParseResult(kind=KIND_EXECUTABLE, action=action, fields=fields)
        return ActionParseResult(kind=KIND_EXECUTABLE, action=action)

    # Save-by-link: the link is the target. The URL is preserved verbatim and
    # validated only for the Telegram-link shape — the tool re-validates it
    # authoritatively before any Telegram call.

    # Create a durable scheduled task. The model never controls execution or
    # owner identity — the request text flows through the deterministic
    # TaskInterpreter -> TaskCreationService boundary, which validates the
    # schedule, actions, and persistence under the trusted owner.
    if action == "create_task":
        req = raw.get("request")
        if not isinstance(req, str) or not req.strip():
            return ActionParseResult(kind=KIND_INVALID, error="Missing 'request' field.")
        if len(req.strip()) > 2000:
            return ActionParseResult(kind=KIND_INVALID, error="Task request is too long.")
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action=action,
            target="schedule",
            schedule_text=req.strip(),
        )

    if action == "send":
        # Immediate text-write: the model supplies ONLY the text; the
        # destination is resolved from trusted runtime context (the current
        # request chat, or the task's creation chat for scheduled sends).
        # A recipient field is a hard rejection — the model can never choose
        # where the message goes.
        if "recipient" in raw:
            return ActionParseResult(
                kind=KIND_INVALID,
                error="'recipient' is not supported for send; the destination comes from trusted runtime context.",
            )
        text = raw.get("text", raw.get("content", ""))
        if not isinstance(text, str) or not text.strip():
            return ActionParseResult(kind=KIND_INVALID, error="Missing 'text' field.")
        if len(text.strip()) > MAX_SEND_TEXT_CHARS:
            return ActionParseResult(kind=KIND_INVALID, error="Message text is too long.")
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action=action,
            target="current_chat",
            text=text.strip(),
        )

    if action == "save_link":
        link = raw.get("link", "")
        if not isinstance(link, str):
            return ActionParseResult(kind=KIND_INVALID, error="Invalid 'link' field.")
        url = _extract_telegram_link(link)
        if not url:
            return ActionParseResult(
                kind=KIND_INVALID,
                error="Invalid or missing Telegram link.",
            )
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action=action,
            target="telegram_link",
            link=url,
        )

    target = raw.get("target", "")
    if target:
        if not isinstance(target, str):
            return ActionParseResult(kind=KIND_INVALID, error="Invalid 'target' field.")
        target = target.strip()
        if target not in TARGET_SCOPES:
            return ActionParseResult(kind=KIND_INVALID, error=f"Unknown target: {target}")

    count: int | None = None
    if "count" in raw:
        count = coerce_int(raw.get("count"))
        if count is None or count < _MIN_DELETE_COUNT or count > _MAX_DELETE_COUNT:
            return ActionParseResult(
                kind=KIND_INVALID,
                error=f"Invalid count: {raw.get('count')!r} (must be 1-{_MAX_DELETE_COUNT}).",
            )

    mode = str(raw.get("mode", "") or "").strip().lower()
    if mode and mode not in {"last_n", "all", "until_time", "until_message", "filtered"}:
        return ActionParseResult(kind=KIND_INVALID, error=f"Unknown delete mode: {mode}")
    until_time = raw.get("until_time", "")
    after_time = raw.get("after_time", "")
    if until_time and not isinstance(until_time, str):
        return ActionParseResult(kind=KIND_INVALID, error="Invalid 'until_time' field.")
    if after_time and not isinstance(after_time, str):
        return ActionParseResult(kind=KIND_INVALID, error="Invalid 'after_time' field.")
    query = raw.get("query", "")
    if query and not isinstance(query, str):
        return ActionParseResult(kind=KIND_INVALID, error="Invalid 'query' field.")
    semantic: dict[str, Any] | None = None
    if "semantic" in raw:
        if action != "delete_messages":
            return ActionParseResult(
                kind=KIND_INVALID,
                error="'semantic' is only valid for the delete_messages action.",
            )
        semantic = raw.get("semantic")
        if not isinstance(semantic, dict) or spec_from_dict(semantic) is None:
            return ActionParseResult(kind=KIND_INVALID, error="Invalid 'semantic' field.")
    boundary_id = None
    if "boundary_id" in raw:
        boundary_id = coerce_int(raw.get("boundary_id"))
        if boundary_id is None or boundary_id <= 0:
            return ActionParseResult(kind=KIND_INVALID, error="Invalid 'boundary_id' field.")
    if action == "delete_messages":
        if mode == "all" and count is not None:
            return ActionParseResult(kind=KIND_INVALID, error="'all' mode cannot include a count.")
        if mode == "until_time" and not until_time:
            return ActionParseResult(kind=KIND_INVALID, error="until_time is required for until_time mode.")
        if mode == "until_message" and boundary_id is None:
            # A replied-to boundary is injected by the runtime context, so a
            # missing boundary is valid only when the tool can resolve it.
            pass
        # An explicit single-message target must carry a valid message ID.
        if target == "message_id":
            message_id = coerce_int(raw.get("message_id"))
            if message_id is None or message_id <= 0:
                return ActionParseResult(
                    kind=KIND_INVALID,
                    error="Invalid 'message_id' field.",
                )
            return ActionParseResult(
                kind=KIND_EXECUTABLE,
                action=action,
                target=target,
                message_id=message_id,
            )
        # A recent_messages deletion (explicit or implied by a bare count)
        # must carry a deterministic count. A bare "delete" with no count and
        # no target is genuinely ambiguous → ask.
        effective_target = target or ("recent_messages" if count else "")
        if effective_target == "recent_messages" and count is None and not mode:
            return ActionParseResult(
                kind=KIND_CLARIFY,
                action=action,
                reason="How many messages should I delete?",
            )
        if not target and not count and not mode and not until_time and not boundary_id and not query:
            return ActionParseResult(
                kind=KIND_CLARIFY,
                action=action,
                reason="Which message(s) should I delete?",
            )
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action=action,
            target=target,
            count=count,
            caption=bool(raw.get("caption", False)),
            mode=mode,
            until_time=str(until_time or ""),
            after_time=str(after_time or ""),
            boundary_id=boundary_id,
            query=str(query or ""),
            semantic=semantic,
        )

    return ActionParseResult(
        kind=KIND_EXECUTABLE,
        action=action,
        target=target,
        count=count,
        caption=bool(raw.get("caption", False)),
        mode=mode,
        until_time=str(until_time or ""),
        after_time=str(after_time or ""),
        boundary_id=boundary_id,
        query=str(query or ""),
        semantic=semantic,
    )


# ── Task lifecycle + saved-item retrieval actions ──

_TASK_INSPECT_FIELDS = frozenset({"action", "task_id"})
_TASK_TRANSITION_FIELDS = frozenset({"action", "task_id", "action_status", "expected_version"})
_TASK_STATUS_VOCABULARY = frozenset({"paused", "active", "completed"})
_SAVE_CODE_RE = re.compile(r"^[A-Z0-9]{1,12}$")


def _validate_task_lifecycle_action(action: str, raw: dict[str, Any]) -> ActionParseResult:
    """Validate one task_lifecycle action object.

    The status field is read from ``action_status`` so it can never collide
    with the action name itself. The model supplies only the task id, the
    target status, and the CAS version it learned from task_list/task_inspect
    — ownership, persistence, and transition legality stay in the existing
    TaskManagementService/TaskRepository boundary.
    """
    allowed = _TASK_INSPECT_FIELDS if action == "task_inspect" else _TASK_TRANSITION_FIELDS
    unknown = sorted(set(raw) - allowed)
    if unknown:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=f"Unknown field(s) for {action}: {', '.join(unknown)}",
        )

    task_id = coerce_int(raw.get("task_id"))
    if task_id is None or task_id <= 0:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=f"Missing or invalid 'task_id' for {action}.",
        )

    if action == "task_inspect":
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action=action,
            target="schedule",
            task_id=task_id,
        )

    status = raw.get("action_status")
    if not isinstance(status, str) or status.strip().lower() not in _TASK_STATUS_VOCABULARY:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=(
                "Invalid 'action_status' for task_transition "
                "(allowed: paused, active, completed)."
            ),
        )
    version = coerce_int(raw.get("expected_version"))
    if version is None or version <= 0:
        return ActionParseResult(
            kind=KIND_INVALID,
            error="Missing or invalid 'expected_version' for task_transition.",
        )
    return ActionParseResult(
        kind=KIND_EXECUTABLE,
        action=action,
        target="schedule",
        task_id=task_id,
        action_status=status.strip().lower(),
        expected_version=version,
    )


def _validate_retrieve_save_action(raw: dict[str, Any]) -> ActionParseResult:
    """Validate one retrieve_save action object (exact-field-set rule)."""
    unknown = sorted(set(raw) - {"action", "save_code"})
    if unknown:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=f"Unknown field(s) for retrieve_save: {', '.join(unknown)}",
        )
    save_code = raw.get("save_code")
    if not isinstance(save_code, str):
        return ActionParseResult(
            kind=KIND_INVALID,
            error="Missing or invalid 'save_code' for retrieve_save.",
        )
    normalized = save_code.strip().upper()
    if not normalized or not _SAVE_CODE_RE.match(normalized):
        return ActionParseResult(
            kind=KIND_INVALID,
            error="Invalid 'save_code' (expected the item's save code, e.g. S0001).",
        )
    return ActionParseResult(
        kind=KIND_EXECUTABLE,
        action="retrieve_save",
        target="current_chat",
        save_code=normalized,
    )


# ── Target resolution ──


def _default_target(action: str) -> str:
    if action in ("save", "deep_save"):
        return "replied_message"
    if action == "retrieve_save":
        return "current_chat"
    if action in ("task_list", "task_inspect", "task_transition"):
        return "schedule"
    return "recent_messages"


def resolve_tool_calls(result: ActionParseResult) -> list[dict[str, Any]]:
    """Resolve a validated action into concrete tool calls for the ToolExecutor.

    Each returned call maps to an EXISTING tool (save / delete / delete_replied)
    which in turn delegates to the existing service layer. Telegram identity is
    resolved by those tools from the runtime context — never fabricated here.
    """
    if result.kind != KIND_EXECUTABLE:
        return []

    action = result.action
    target = result.target or _default_target(action)

    if action in ("save", "deep_save"):
        # Save is Deep Save only; the SaveTool resolves the replied-to message
        # from runtime context and calls execute_save(). Captions are always
        # preserved by the existing deep-save pipeline.
        return [{"name": "save", "arguments": {}}]

    if action == "save_link":
        # The existing execute_link_save() resolves the link and reuses the
        # SAME Deep Save pipeline. The URL is passed through verbatim.
        return [{"name": "save_by_link", "arguments": {"link": result.link}}]

    if action == "create_task":
        # Routes into the registered create_task tool, which reuses the
        # deterministic TaskInterpreter -> TaskCreationService boundary.
        return [{"name": "create_task", "arguments": {"request": result.schedule_text}}]

    if action == "send":
        # Immediate and scheduled text-write both reuse the SAME registered
        # execution tool — one send implementation, one TelegramAPI transport,
        # one executor. The destination is resolved from trusted runtime
        # context (current chat for immediate sends, task creation chat for
        # scheduled sends), never from the model.
        return [{"name": "send_message", "arguments": {"text": result.text}}]

    if action == "list_saved_items":
        return [{"name": "list_saves", "arguments": {}}]

    if action == "search_saved_items":
        return [{"name": "search", "arguments": {"query": result.query}}]

    if action == "list_recent_messages":
        args: dict[str, Any] = {"limit": result.count} if result.count else {}
        return [{"name": "list_recent_messages", "arguments": args}]

    if action == "database_stats":
        return [{"name": "database_stats", "arguments": {}}]

    if action in ("bio_status", "get_bio"):
        # bio_status / get_bio both read the CURRENT Telegram bio through the
        # self client (get_bio). The bio ENGINE state (template/mood/status)
        # remains available via the bio_show tool for explicit engine queries.
        return [{"name": "get_bio", "arguments": {}}]

    if action == "username_status":
        return [{"name": "username_show", "arguments": {}}]

    if action == "account_status":
        args: dict[str, Any] = {"fields": list(result.fields)} if result.fields else {}
        return [{"name": "account_show", "arguments": args}]

    if action == "task_list":
        args: dict[str, Any] = {"status": result.status} if result.status else {}
        return [{"name": "task_list", "arguments": args}]

    if action == "task_inspect":
        return [{"name": "task_inspect", "arguments": {"task_id": result.task_id}}]

    if action == "task_transition":
        return [{
            "name": "task_transition",
            "arguments": {
                "task_id": result.task_id,
                "action": result.action_status,
                "expected_version": result.expected_version,
            },
        }]

    if action == "retrieve_save":
        return [{"name": "retrieve_save", "arguments": {"save_code": result.save_code}}]

    if action == "delete_messages":
        if target == "message_id":
            return [{"name": "delete_message_by_id", "arguments": {"message_id": result.message_id}}]
        if (
            result.mode or result.until_time or result.after_time
            or result.boundary_id is not None or result.query or result.semantic
        ):
            args: dict[str, Any] = {}
            if result.count is not None:
                args["count"] = result.count
            if result.mode:
                args["mode"] = result.mode
            if result.until_time:
                args["until_time"] = result.until_time
            if result.after_time:
                args["after_time"] = result.after_time
            if result.boundary_id is not None:
                args["boundary_id"] = result.boundary_id
            if result.query:
                args["query"] = result.query
            if result.semantic:
                args["semantic"] = result.semantic
            return [{"name": "delete", "arguments": args}]
        if target in ("replied_message", "current_message"):
            return [{"name": "delete_replied", "arguments": {}}]
        if target == "last_message":
            return [{"name": "delete", "arguments": {"count": 1}}]
        if target == "recent_messages":
            return [{"name": "delete", "arguments": {"count": result.count or 1}}]

    return []


def parse_action_text(text: str) -> ActionParseResult:
    """Parse, validate, and resolve one model text output.

    Prose with no JSON → conversational. JSON action → validated and, when
    executable, resolved into tool calls. Unknown/unsupported/ambiguous
    outcomes are returned without ever reaching the executor.
    """
    raw = extract_json_object(text)
    if raw is None:
        return ActionParseResult(kind=KIND_CONVERSATIONAL)

    result = validate_action(raw)
    if result.kind == KIND_EXECUTABLE:
        tool_calls = resolve_tool_calls(result)
        if not tool_calls:
            return ActionParseResult(
                kind=KIND_UNSUPPORTED,
                action=result.action,
                error=f"Unsupported action: {result.action}",
            )
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action=result.action,
            target=result.target or _default_target(result.action),
            count=result.count,
            caption=result.caption,
            fields=result.fields,
            mode=result.mode,
            until_time=result.until_time,
            after_time=result.after_time,
            boundary_id=result.boundary_id,
            query=result.query,
            semantic=result.semantic,
            text=result.text,
            task_id=result.task_id,
            action_status=result.action_status,
            expected_version=result.expected_version,
            save_code=result.save_code,
            status=result.status,
            tool_calls=tool_calls,
        )
    return result


# ── Deterministic command intent (Persian/English) ──
#
# Safety net for when a provider returns prose instead of a structured action.
# It recognizes the narrow, high-confidence command vocabulary (save / deep
# save / delete N) directly from the ORIGINAL user message and resolves targets
# from the reply context — never from the model's prose. Only deterministic,
# high-confidence matches are produced; everything else stays conversational.

_DELETE_STEMS = ("پاک", "حذف")
_SAVE_STEMS = ("سیو", "ذخیره", "ذخیر")
_SEND_STEMS = ("بفرست", "ارسال", "فوروارد")

_IMPERATIVE_SUFFIXES = frozenset({"کن", "کنی", "کنید", "کنین"})

_EN_DELETE = frozenset({"delete", "remove", "deleting", "removing", "deleted", "removed"})
_EN_SAVE = frozenset({"save", "saving", "saved", "store", "storing"})
_EN_SEND = frozenset({"send", "sending", "forward", "forwarding"})
_EN_NEGATION = frozenset({"not", "never", "dont", "didnt"})

# ── Deterministic text-write intent ──
# "بنویس سلام" / "write hello" is an IMMEDIATE text-write: it reuses the
# registered ``send_message`` tool (owner's own Saved Messages chat). Only
# high-confidence imperative writes resolve here; desire markers ("میخوام",
# "want"), recipients ("برای X", "to X"), references ("اینو", "this"),
# and future-time words ("فردا", "tomorrow") are never treated as an
# immediate text-write — those stay on the provider path.
_WRITE_TOKENS = frozenset({"بنویس", "نویس", "write", "writing"})
_WRITE_INTENT_BLOCKERS = frozenset({
    "میخوام", "میخواهم", "خوام", "میخوایم", "میخوای",
    "want", "wanna", "would",
})
_WRITE_RECIPIENT_MARKERS = frozenset({"برای", "به", "to", "for"})
_WRITE_REFERENCE_MARKERS = frozenset({
    "این", "اینو", "اینم", "همین", "همینو", "this", "that", "it",
})
_WRITE_FUTURE_MARKERS = frozenset({"فردا", "امشب", "tomorrow", "tonight"})


def _is_write_token(tok: str) -> bool:
    """True when *tok* is an imperative write verb (بنویس/بنویسید/write)."""
    if tok in _WRITE_TOKENS:
        return True
    if tok.startswith("بنویسید") or tok.startswith("بنویسین") or tok.startswith("بنویسش"):
        return True
    return False


def _write_text_present(words: list[str]) -> bool:
    return any(_is_write_token(w) for w in words)


def _extract_write_text(words: list[str]) -> str | None:
    """Extract bounded text for a deterministic immediate text-write.

    Returns None when the request is not a safe text-write: no imperative
    write verb, no remaining text, a desire marker ("I want to write..."),
    a recipient/reference ("write this to Ali"), or a future-time word.
    """
    if any(w in _WRITE_INTENT_BLOCKERS for w in words):
        return None
    verb_index = -1
    for i, tok in enumerate(words):
        if _is_write_token(tok):
            verb_index = i
            break
    if verb_index < 0 or verb_index + 1 >= len(words):
        return None
    rest = words[verb_index + 1:]
    if any(
        w in _WRITE_RECIPIENT_MARKERS or w in _WRITE_REFERENCE_MARKERS or w in _WRITE_FUTURE_MARKERS
        for w in rest
    ):
        return None
    text = " ".join(rest).strip()
    if not text or len(text) > MAX_SEND_TEXT_CHARS:
        return None
    return text

_THIS_TOKENS = frozenset({"این", "اینو", "اینم", "همین", "همینو", "this", "that", "it"})
_LAST_TOKENS = frozenset({"آخر", "آخرین", "آخری", "آخریه", "اخیر", "last", "latest", "recent"})
# Semantic delete qualifiers: a delete request that references a topic or
# context ("پیام‌های مربوط به دعوای اخیر رو پاک کن", "delete messages about
# X") is a SEMANTIC request for the AI — the deterministic parser must never
# collapse it into "delete the last message" (count=1). Only scope-free
# positional deletes (this / last / last-N / explicit ID) are deterministic.
_SEMANTIC_DELETE_WORDS = frozenset({
    "مربوط", "درباره", "دربارش", "راجع", "راجب",
    "دعوا", "بحث", "موضوع", "موضوعات",
    "about", "regarding", "related", "argument", "topic", "subject",
    "discussion", "conversation",
})
_SEMANTIC_DELETE_STEMS = ("مربوط", "دعوا", "بحث", "موضوع")
_SEMANTIC_SEARCH_WORDS = frozenset({
    "پیدا", "جستجو", "لیست", "ببین", "find", "search", "inspect", "list",
})
_SEMANTIC_QUERY_STOP_WORDS = frozenset({
    "به", "را", "رو", "که", "و", "از", "تا", "این", "اون", "اینم", "همین",
    "پیام", "پیامها", "messages", "message", "my", "mine", "own",
    "خودم", "من", "مربوط", "درباره", "دربارش", "راجع", "راجب",
    "related", "about", "regarding", "to", "the",
})
_DEEP_TOKENS = frozenset({"عمیق", "deep", "کامل"})
_MESSAGE_TOKENS = frozenset({"پیام", "پیامها", "message", "messages", "msg", "msgs"})
_COUNT_CONTEXT = _MESSAGE_TOKENS | _LAST_TOKENS
_ALL_DELETE_WORDS = frozenset({"همه", "تمام", "هرچی", "هرچه", "all", "every", "everything"})
_TODAY_WORDS = frozenset({"امروز", "today"})
_ID_TOKENS = frozenset({"id", "msgid", "message_id", "ایدی", "آیدی", "شناسه"})

_DEFAULT_LIST_LIMIT = 50

# Read-only status/query intent keywords (matched only after the imperative
# save/delete/review paths fall through, so "اینو سیو کن" and "پیام آخر رو
# پاک کن" always take precedence).
_SAVE_LIST_WORDS = frozenset({
    "چه", "لیست", "لیستش", "وضعیت", "وضعیتش", "چیزا", "چیزایی", "دارم",
    "داریم", "داره", "دارن", "دارید", "شدن", "شده", "شد", "نشون", "بده",
    "چند", "چندتا", "موجود", "مشاهده",
})
_DB_WORDS = frozenset({"دیتابیس", "دیتابیسم", "دیتا", "دیتابس", "database", "db", "آمار"})
_DB_STATUS_WORDS = frozenset({
    "وضعیت", "وضعیتش", "چیه", "چی", "چه", "بگو", "نشون", "ببین", "چطور",
    "چطوره", "هست", "هستن", "آمار", "status", "stats", "state", "statistics",
    "show", "what", "info",
})
_USERNAME_WORDS = frozenset({"یوزرنیم", "یوزرنیمم", "یوزر", "username"})
# Explicit qualifiers that make "username" mean the REAL Telegram @username
# handle ("یوزرنیم واقعی تلگرامم", "username تلگرامم", "what is my Telegram
# username?"). Without one of these, casual Persian "یوزرنیم" means the
# account FIRST NAME — the LifeOS "username engine" updates first_name.
_REAL_USERNAME_WORDS = frozenset({"واقعی", "تلگرام", "telegram", "real", "handle"})


def _has_real_username_qualifier(words: list[str]) -> bool:
    """True when an explicit real-@username qualifier is present.

    Persian qualifiers are stem-matched so possessive forms ("تلگرامم",
    "تلگرامیم") count as "تلگرام"; English qualifiers match exactly.
    """
    for w in words:
        if w in ("telegram", "real", "handle"):
            return True
        if w.startswith("واقعی") or w.startswith("تلگرام"):
            return True
    return False
_ACCOUNT_WORDS = frozenset({
    "اسم", "اسمم", "نام", "نامم", "اکانت", "اکانتم", "اکانتی",
    "حساب", "حسابم", "حسابی", "پروفایل", "پروفایلم",
    "name", "account", "profile", "first", "last", "identity",
})
# Bio words are stem-matched so possessive/colloquial forms ("بیوم",
# "بیوی", "بایوم") resolve to bio retrieval like the plain form.
_BIO_STEMS = ("بیو", "بایو")
# Extra qualifiers that make a bio mention a read query even without an
# explicit status word: "بیو الانم", "بیوی فعلیم", "my bio", "bio now".
_BIO_QUERY_WORDS = frozenset({
    "الان", "الانم", "حالا", "فعلی", "فعلیم", "فعلیه", "my", "من", "now",
})
# Fused pronoun show-forms: Persian attaches the object pronoun directly to
# the verb ("نشونم بده" = نشون + م, "show me"), producing single tokens that
# never equal the plain "نشون" in _STATUS_WORDS. Stem-matching with the
# trailing clitic stripped keeps "بیو رو نشونم بده" deterministic instead of
# sending it to the provider (where the result was hallucinated/stylized).
_BIO_SHOW_VERB_STEMS = ("نشون", "نمایش")
_PRONOUN_CLITICS = ("مون", "تون", "شون", "مان", "تان", "شان", "م", "ت", "ش")


def _is_show_verb_token(tok: str) -> bool:
    """True for a show verb, bare or with a trailing pronoun clitic.

    Matches نشون / نمایش (+ optional م/ت/ش/مون/تون/شون/مان/تان/شان) so
    "نشونم", "نمایشش", and plain "نشون" all count as show verbs. Only
    meaningful when a bio word is also present (checked by the caller).
    """
    for stem in _BIO_SHOW_VERB_STEMS:
        if tok == stem or tok.startswith(stem):
            remainder = tok[len(stem):]
            if not remainder or remainder in _PRONOUN_CLITICS:
                return True
    return False


def _has_bio_mention(words: list[str]) -> bool:
    """True when any token looks like a bio word (Persian stem or English)."""
    return any(w == "bio" or w.startswith(_BIO_STEMS[0]) or w.startswith(_BIO_STEMS[1]) for w in words)
_STATUS_WORDS = frozenset({
    "وضعیت", "وضعیتش", "چیه", "چی", "چه", "بگو", "نشون", "ببین", "چطور",
    "چطوره", "هست", "هستن", "status", "show", "what", "current", "state", "info",
})
_EN_SAVED_WORDS = frozenset({"saved", "saves"})
_EN_LIST_SAVED_WORDS = frozenset({"list", "items", "item", "my", "mine", "show", "status"})

# Persian number words → int, so "ده پیام" / "بیست و پنج پیام" parse like "۱۰ پیام".
_FA_NUMBER_WORDS = {
    "یک": 1, "دو": 2, "سه": 3, "چهار": 4, "پنج": 5, "شش": 6, "هفت": 7, "هشت": 8, "نه": 9,
    "ده": 10, "یازده": 11, "دوازده": 12, "سیزده": 13, "چهارده": 14, "پانزده": 15,
    "شانزده": 16, "هفده": 17, "هجده": 18, "نوزده": 19,
    "بیست": 20, "سی": 30, "چهل": 40, "پنجاه": 50, "شصت": 60, "هفتاد": 70, "هشتاد": 80, "نود": 90,
    "صد": 100, "دویست": 200, "سیصد": 300, "چهارصد": 400, "پانصد": 500,
}


def _tokenize(text: str) -> list[str]:
    """Lowercase, normalize digits, and split Persian/English into word tokens."""
    s = normalize_digits(text)
    s = s.replace("\u200c", " ").replace("\u200b", " ")
    s = s.replace("'", "").replace("’", "")
    s = s.lower()
    # \u0600-\u061f is Arabic/Persian *punctuation* (، ؛ ؟) and diacritics,
    # not letters; including it made "چیه؟" tokenize as "چیه؟" and miss
    # dictionary matches. Letters start at \u0621 (hamza) onward.
    return [t for t in re.findall(r"[a-z0-9\u0621-\u06ff]+", s) if t]


def _is_stem_token(tok: str, stems: tuple[str, ...]) -> bool:
    """True when *tok* is a verb stem, optionally with an attached -sh clitic."""
    for stem in stems:
        if tok == stem:
            return True
        if tok.startswith(stem + "ش"):
            return True
    return False


def _imperative_present(words: list[str], stems: tuple[str, ...]) -> bool:
    """True when a Persian imperative (stem + کن/کنی/کنید) is present."""
    for i, tok in enumerate(words):
        if _is_stem_token(tok, stems):
            if i + 1 < len(words) and words[i + 1] in _IMPERATIVE_SUFFIXES:
                return True
    return False


def _negated_present(words: list[str], stems: tuple[str, ...]) -> bool:
    """True when a Persian imperative is negated (stem + نکن...)."""
    for i, tok in enumerate(words):
        if _is_stem_token(tok, stems):
            if i + 1 < len(words) and words[i + 1].startswith("نکن"):
                return True
    return False


def _english_action(words: list[str], verbs: frozenset[str]) -> tuple[bool, bool]:
    """Return (present, negated) for an English verb token."""
    for i, tok in enumerate(words):
        if tok in verbs:
            negated = any(words[j] in _EN_NEGATION for j in range(max(0, i - 2), i))
            return True, negated
    return False, False


def _parse_number(words: list[str], i: int) -> int | None:
    """Parse an ASCII/Persian digit or a Persian number word at index *i*.

    Handles compounds like "بیست و پنج" (20 + 5 = 25). Returns None when
    the token is not a number.
    """
    if i >= len(words):
        return None
    tok = words[i]
    if tok.isdigit():
        try:
            return int(tok)
        except ValueError:
            return None
    if tok not in _FA_NUMBER_WORDS:
        return None
    total = _FA_NUMBER_WORDS[tok]
    j = i + 1
    while j + 1 < len(words) and words[j] == "و" and words[j + 1] in _FA_NUMBER_WORDS:
        total += _FA_NUMBER_WORDS[words[j + 1]]
        j += 2
    return total


def _is_word_count_marker(tok: str) -> bool:
    """True when *tok* makes a preceding number a word-count predicate.

    In "پیام‌های دو کلمه‌ای رو پاک کن" the "دو" is a WORD COUNT (two-word
    messages), not a deletion count — it must never be parsed as
    "delete 2 messages". English digits/hyphen forms tokenize the same way
    ("2-word" → ["2", "word"]), so the same guard covers both.
    """
    return tok in ("word", "words") or tok.startswith("کلمه") or tok.startswith("واژه")


def _extract_count(words: list[str]) -> int | None:
    """Extract a 1..500 count near a message/last word.

    Accepts Persian/Arabic-Indic digits (normalized), ASCII digits, and
    Persian number words ("ده", "بیست و پنج", ...). A number that is
    immediately followed by a word-count marker ("کلمه", "word", ...) is
    a structural predicate, never a deletion count, and is skipped.
    """
    for i in range(len(words)):
        if i + 1 < len(words) and _is_word_count_marker(words[i + 1]):
            continue
        n = _parse_number(words, i)
        if n is not None and 1 <= n <= _MAX_DELETE_COUNT:
            window = words[max(0, i - 2):i + 4]
            if any(w in _COUNT_CONTEXT for w in window):
                return n
    return None


def _extract_until_time(words: list[str]) -> str | None:
    """Extract a conservative local HH:MM cutoff from natural language."""
    markers = {"ساعت", "until", "before", "by"}
    for i, word in enumerate(words[:-1]):
        if word == "ساعت" and i > 0 and words[i - 1] in {"از", "from"}:
            continue
        if word not in markers:
            continue
        value = _parse_number(words, i + 1)
        if value is not None and 0 <= value <= 23:
            return f"{value:02d}:00"
    return None


def _extract_after_time(words: list[str]) -> str | None:
    """Extract a local HH:MM range start from "from ... onwards" wording."""
    for i, word in enumerate(words[:-1]):
        if word not in {"از", "from"}:
            continue
        j = i + 1
        if j < len(words) and words[j] == "ساعت":
            j += 1
        value = _parse_number(words, j)
        if value is not None and 0 <= value <= 23:
            tail = set(words[j + 1:])
            if tail & {"بعد", "به", "بعدش", "onwards", "after", "forward"}:
                return f"{value:02d}:00"
    return None


def _has_message_boundary(words: list[str], has_reply: bool) -> bool:
    """True for an explicit up-to-message request.

    With a reply, the replied-to message is the boundary. Without a reply,
    the active request message is captured by the handler and injected as the
    boundary at execution time.
    """
    joined = " ".join(words)
    return (
        "تا این پیام" in joined
        or "از این پیام" in joined
        or "تا اینجا" in joined
        or "until this message" in joined
        or "from this message" in joined
        or "up to this message" in joined
        or "up through this message" in joined
    )


def _is_all_delete(words: list[str]) -> bool:
    return any(word in _ALL_DELETE_WORDS for word in words) and any(
        word in _MESSAGE_TOKENS or word in {"پیامام", "پیامهام", "خودم", "my", "mine", "own"}
        for word in words
    )


def _extract_message_id(words: list[str]) -> int | None:
    """Extract an explicit message ID near an id/شناسه token (positive int)."""
    for i, tok in enumerate(words):
        if tok in _ID_TOKENS:
            for j in range(max(0, i - 2), min(len(words), i + 3)):
                if words[j].isdigit() and int(words[j]) > 0:
                    return int(words[j])
    return None


def _has_save_mention(words: list[str]) -> bool:
    """True when any token looks like the Persian save stem (سیو/ذخیره...)."""
    return any(
        w.startswith("سیو") or w.startswith("ذخیره") or w.startswith("ذخیر")
        for w in words
    )


def _is_semantic_delete(words: list[str]) -> bool:
    """True when a delete request references a topic/context (semantic)."""
    for w in words:
        if w in _SEMANTIC_DELETE_WORDS:
            return True
        if any(w.startswith(stem) for stem in _SEMANTIC_DELETE_STEMS):
            return True
    return False


def _extract_semantic_query(words: list[str]) -> str:
    """Extract an explicit topic for a local, self-only text filter.

    Direct requests such as ``delete my messages about script`` contain
    enough information to avoid another provider round. The service still
    performs the authoritative Telegram ownership check; this helper only
    extracts a bounded text predicate from the owner's wording.
    """
    marker_index = None
    for i, word in enumerate(words):
        if word in _SEMANTIC_DELETE_WORDS or any(
            word.startswith(stem) for stem in _SEMANTIC_DELETE_STEMS
        ):
            marker_index = i
            break
    if marker_index is None:
        return ""

    query_words: list[str] = []
    for offset, word in enumerate(words[marker_index:]):
        if (
            offset > 0
            and word in {"از", "ساعت", "from", "until", "before", "تا"}
        ):
            break
        if word in _SEMANTIC_SEARCH_WORDS or _is_stem_token(word, _DELETE_STEMS):
            break
        if word in {"کن", "کنی", "کنید", "کنین", "do", "delete", "remove"}:
            break
        if word in _SEMANTIC_QUERY_STOP_WORDS:
            continue
        query_words.append(word)
    return " ".join(query_words).strip()


def _en_list_saved(words: list[str]) -> bool:
    """High-confidence English "list/show my saved items" signal.

    Only the noun/past forms ("saved"/"saves") count — the bare verb
    "save" is an instruction, not a listing request, so "what does save
    mean?" stays conversational.
    """
    if not any(w in _EN_SAVED_WORDS for w in words):
        return False
    return any(w in _EN_LIST_SAVED_WORDS for w in words)


def _parse_status_intent(words: list[str], *, has_at: bool = False) -> ActionParseResult | None:
    """Deterministically recognize read-only status/query intents.

    Returns an executable tool call (list_saves / database_stats /
    get_bio / account_show) or None. Called only after the imperative
    save/delete/review paths fall through.

    Account-identity semantics: casual Persian "یوزرنیم"/"username" means
    the account FIRST NAME in this project (the username engine manages
    first_name). Only an explicit qualifier — an "@" prefix, or words like
    "واقعی" / "تلگرام" / "telegram" / "real" — selects the REAL Telegram
    @username handle. Both resolve to ``account_show`` with a minimal
    ``fields`` allowlist so unrelated account data is never returned.
    """
    wordset = set(words)

    # Saved items: "چه چیزایی سیو دارم" / "لیست سیوها رو بده" / "وضعیت سیوها چیه".
    if _has_save_mention(words) and (wordset & _SAVE_LIST_WORDS):
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action="list_saved_items",
            target="saved_items",
            tool_calls=[{"name": "list_saves", "arguments": {}}],
        )
    if _en_list_saved(words):
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action="list_saved_items",
            target="saved_items",
            tool_calls=[{"name": "list_saves", "arguments": {}}],
        )

    # Database stats: "وضعیت دیتابیس چیه" / "database status".
    if (wordset & _DB_WORDS) and (wordset & _DB_STATUS_WORDS):
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action="database_stats",
            tool_calls=[{"name": "database_stats", "arguments": {}}],
        )

    # REAL Telegram @username — ONLY when explicitly qualified:
    # "@username", "یوزرنیم واقعی تلگرامم", "username تلگرامم رو بگو",
    # "what is my Telegram username?", "show my @username".
    if (
        (wordset & _USERNAME_WORDS)
        and (has_at or _has_real_username_qualifier(words))
        and (wordset & _STATUS_WORDS)
    ):
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action="account_status",
            fields=["username"],
            tool_calls=[{"name": "account_show", "arguments": {"fields": ["username"]}}],
        )

    # Bio retrieval: "وضعیت بایو چیه", "بیوم الان چیه؟", "بیوی فعلیم",
    # "بیو رو نشونم بده", "what is my bio?", "current bio", "my bio". This
    # reads the ACTUAL Telegram bio via get_bio — never a hallucinated or
    # engine-state value. It runs BEFORE the account branch so
    # "بیو اکانتم رو بگو" resolves to bio retrieval, not account identity.
    if _has_bio_mention(words) and (
        (wordset & _STATUS_WORDS)
        or (wordset & _BIO_QUERY_WORDS)
        or any(_is_show_verb_token(w) for w in words)
    ):
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action="get_bio",
            tool_calls=[{"name": "get_bio", "arguments": {}}],
        )

    # Account identity / FIRST NAME: "وضعیت اسم اکانتم رو بگو",
    # "اسم اکانتم چیه؟", "what is my account name?", "show my first name",
    # AND the casual form "وضعیت یوزرنیمم رو بگو" (Persian "یوزرنیم" =
    # first name in this project). Only the first name is requested — phone,
    # account ID, and the @username handle are never included.
    if (wordset & (_ACCOUNT_WORDS | _USERNAME_WORDS)) and (wordset & _STATUS_WORDS):
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action="account_status",
            fields=["first_name"],
            tool_calls=[{"name": "account_show", "arguments": {"fields": ["first_name"]}}],
        )

    return None


# ── Scheduling-intent detection ──
# A natural-language scheduling request (a recurring interval, a daily/weekly
# cadence, or a planned reminder/time) must reach the durable task boundary
# instead of being diverted into the send/delete/save command vocabulary.
# Detection is deliberately conservative: a strong cadence OR an explicit
# plan/reminder verb AND an action verb. Ambiguous at-time deletes stay on
# the provider path (which now also exposes the create_task tool) rather than
# being guessed as scheduled.

_FA_RECUR_WORDS = frozenset({
    "روزانه", "هفتگی", "ماهانه", "سالانه", "تکرار", "تکرارش",
    "مداوم", "همیشه", "دائمی", "مرتب",
})
_EN_RECUR_WORDS = frozenset({
    "daily", "weekly", "monthly", "yearly", "recurring", "repeat",
    "repeated", "always", "continuously", "regularly",
})
_INTERVAL_INTRO = frozenset({"هر", "every", "each", "once"})
_TIME_UNITS = frozenset({
    "دقیقه", "دقیقه‌ای", "دقیق", "ساعت", "ثانیه", "روز", "شب",
    "هفته", "ماه", "سال",
    "minute", "minutes", "min", "mins", "hour", "hours", "hr", "hrs",
    "second", "seconds", "sec", "secs", "day", "days", "week", "weeks",
    "month", "months", "year", "years",
})
_FA_PLAN_WORDS = frozenset({"برنامه", "تنظیم", "یادآوری", "زمان‌بندی", "زنگ"})
_EN_PLAN_WORDS = frozenset({"plan", "schedule", "remind", "reminder", "alarm", "timer"})
_FUTURE_REF_WORDS = frozenset({"فردا", "امشب", "امروز", "tomorrow", "tonight", "today"})
_FA_ACTION_VERBS = frozenset({
    "بنویس", "بفرست", "بگو", "بزن", "پاک", "حذف", "سیو",
    "ذخیره", "ارسال", "یادآوری", "خبر", "نویس",
    "کن", "کنی", "کنید", "کنیم", "کنه", "کند",
})
_EN_ACTION_VERBS = frozenset({
    "write", "say", "send", "post", "delete", "remove", "clean", "clear",
    "greet", "tell", "notify", "print", "repeat", "remind", "set",
    "create", "share", "ask", "deliver", "repeat",
})


def _has_action_verb(words: list[str]) -> bool:
    return any(w in _FA_ACTION_VERBS or w in _EN_ACTION_VERBS for w in words)


def _has_time_unit(words: list[str], start: int) -> bool:
    return any(w in _TIME_UNITS for w in words[max(0, start):min(len(words), start + 5)])


def _is_scheduling_intent(words: list[str]) -> bool:
    """True when the message clearly requests a recurring/planned task.

    Detects natural-language interval expressions in Persian and English,
    recurring cadence words, and explicit plan/reminder wording. Detection
    is deliberately tolerant of common phrasing but remains conservative:
    ambiguous requests do NOT match here and stay on the provider path.

    Interval patterns recognised (tokens after normalisation):
      - "هر 1 دقیقه ...", "هر 5 دقیقه ...", "every minute ...",
        "every 5 minutes ...", "each hour ..."
      - "هر یک دقیقه ...", "هر ده دقیقه ...", "every hour ..."
      - "once a minute", "once every hour"
    """
    if not words or not _has_action_verb(words):
        return False
    if any(w in _FA_RECUR_WORDS or w in _EN_RECUR_WORDS for w in words):
        return True
    for i, w in enumerate(words):
        if w in _INTERVAL_INTRO and _has_time_unit(words, i + 1):
            return True
    # "once a minute" / "once every hour" style: "once" followed (within a
    # few tokens) by an interval intro + time unit.
    for i, w in enumerate(words):
        if w == "once" and i + 1 < len(words):
            nxt = words[i + 1]
            if nxt in _INTERVAL_INTRO or nxt in _TIME_UNITS:
                return True
            # "once a minute": the token after "once" is "a" then time unit
            if nxt == "a" and i + 2 < len(words) and words[i + 2] in _TIME_UNITS:
                return True
    # Persian "bar" (بار) after a number+time-unit pair strengthens the
    # interval reading: "هر 1 دقیقه یک بار" — already caught above, but
    # "۱ دقیقه روزی ۱ بار" (once a day) also resolves here.
    if any(w == "bar" or w == "بار" for w in words):
        if any(w in _INTERVAL_INTRO for w in words) and any(w in _TIME_UNITS for w in words):
            return True
    if any(w in _FA_PLAN_WORDS or w in _EN_PLAN_WORDS for w in words):
        if _has_time_unit(words, 0) or any(w in _FUTURE_REF_WORDS for w in words):
            return True
    return False


# ── Task-management routing ──
#
# Task-management requests (list / inspect / pause / resume / complete) are
# intentionally NOT hardcoded here. There is deliberately no Persian/English
# task vocabulary in this deterministic parser: selecting the registered
# task tools (task_list / task_inspect / task_transition) is the AI's
# semantic job, driven by the provider-visible tool schemas and the prompt's
# JSON-action contract. The Self Bot stays the execution authority:
# whichever route the AI output takes (native tool call, or a JSON action
# object validated by validate_action / resolve_tool_calls) ends in the SAME
# registered tool -> ToolExecutor -> TaskManagementService chain. Requests
# the deterministic command vocabulary cannot resolve (including every
# task-management sentence) fall through as conversational and reach the
# provider, where tool selection happens.



def parse_command_intent(text: str, *, has_reply: bool = True) -> ActionParseResult:
    """Deterministically parse a Persian/English executable command.

    Called when the model returned prose (no structured action). It never
    trusts the model's prose: it reads the original user message and resolves
    the target from the reply context. Only the narrow command vocabulary is
    recognized — everything else is conversational.
    """
    if not isinstance(text, str) or not text.strip():
        return ActionParseResult(kind=KIND_CONVERSATIONAL)

    words = _tokenize(text)
    if not words:
        return ActionParseResult(kind=KIND_CONVERSATIONAL)

    # A clear scheduling request routes to the deterministic task boundary
    # before the send/delete/save command vocabulary can divert it. The action
    # runs only through the create_task tool (TaskInterpreter ->
    # TaskCreationService); the interpreter fabricates nothing when ambiguous.
    if _is_scheduling_intent(words):
        request_text = text.strip()
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action="create_task",
            target="schedule",
            schedule_text=request_text,
            tool_calls=[{"name": "create_task", "arguments": {"request": request_text}}],
        )

    is_deep = any(w in _DEEP_TOKENS for w in words)
    is_this = any(w in _THIS_TOKENS for w in words)
    is_last = any(w in _LAST_TOKENS for w in words)
    has_message_word = any(w in _MESSAGE_TOKENS for w in words)
    count = _extract_count(words)

    delete_pos = _imperative_present(words, _DELETE_STEMS)
    delete_neg = _negated_present(words, _DELETE_STEMS)
    save_pos = _imperative_present(words, _SAVE_STEMS)
    save_neg = _negated_present(words, _SAVE_STEMS)
    send_pos = _imperative_present(words, _SEND_STEMS) or any(
        _is_stem_token(w, _SEND_STEMS) for w in words
    )

    en_delete, en_delete_neg = _english_action(words, _EN_DELETE)
    en_save, en_save_neg = _english_action(words, _EN_SAVE)
    en_send, _ = _english_action(words, _EN_SEND)

    # A bare English verb with no target/count/reply is likely a question
    # ("what does save mean?") rather than a command.
    en_has_target = has_reply or is_this or is_last or count is not None or has_message_word
    if not en_has_target:
        en_delete = en_save = en_send = False

    if en_delete:
        delete_pos, delete_neg = True, en_delete_neg
    if en_save:
        save_pos, save_neg = True, en_save_neg
    if en_send:
        send_pos = True

    do_delete = delete_pos and not delete_neg
    do_save = save_pos and not save_neg

    delete_mentioned = delete_pos or delete_neg
    save_mentioned = save_pos or save_neg
    send_mentioned = send_pos

    link_url = _extract_telegram_link(text)

    write_pos = _write_text_present(words)

    # Immediate text-write ("بنویس سلام", "write hello") resolves
    # deterministically to the registered send_message tool — the owner's
    # current request chat. Recipient/reference/forward sends ("اینو برای
    # علی بفرست", "forward this") stay unsupported: the architecture never
    # lets the model choose a destination.
    if (send_pos or write_pos) and not do_delete and not do_save:
        if write_pos:
            text = _extract_write_text(words)
            if text:
                return ActionParseResult(
                    kind=KIND_EXECUTABLE,
                    action="send",
                    target="current_chat",
                    text=text,
                    tool_calls=[{"name": "send_message", "arguments": {"text": text}}],
                )
        return ActionParseResult(kind=KIND_UNSUPPORTED, action="send")

    if do_delete and do_save:
        return ActionParseResult(
            kind=KIND_CLARIFY,
            reason="Do you want me to save or delete?",
        )

    # Save-by-link takes priority over replied-message save when a Telegram
    # link is present — the URL is preserved exactly.
    if do_save and link_url:
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action="save_link",
            target="telegram_link",
            link=link_url,
            tool_calls=[{"name": "save_by_link", "arguments": {"link": link_url}}],
        )

    if do_save:
        action = "deep_save" if is_deep else "save"
        if has_reply:
            return ActionParseResult(
                kind=KIND_EXECUTABLE,
                action=action,
                target="replied_message",
                tool_calls=[{"name": "save", "arguments": {}}],
            )
        return ActionParseResult(
            kind=KIND_CLARIFY,
            action=action,
            reason="Reply to the message you want me to save, then I can save it.",
        )

    if do_delete:
        # A deterministic structural predicate (exact N words / exact N
        # English words) is parsed from the ORIGINAL user message and can
        # never be confused with a positional deletion count: "دو کلمه‌ای"
        # means two-word messages, not "delete 2 messages".
        structural = parse_structural_predicate(text)
        has_structural = structural is not None
        semantic = _is_semantic_delete(words)
        semantic_query = _extract_semantic_query(words) if semantic else ""
        # A direct topic predicate or a structural predicate is deterministic:
        # execute the existing self-owned selector locally rather than making
        # Delete depend on a second provider round. Requests that explicitly
        # ask to search/list a topic remain on the existing semantic tool path.
        if (
            has_structural
            or (semantic and semantic_query and not (set(words) & _SEMANTIC_SEARCH_WORDS))
        ):
            until_time = _extract_until_time(words)
            after_time = "today" if any(word in _TODAY_WORDS for word in words) else ""
            after_clock = _extract_after_time(words)
            if after_clock and not after_time:
                after_time = after_clock
            boundary = _has_message_boundary(words, has_reply)
            if boundary:
                mode = "until_message"
            elif until_time is not None:
                mode = "until_time"
            else:
                mode = "filtered"
            arguments: dict[str, Any] = {"mode": mode}
            if has_structural:
                # The normalized topic (if any) travels inside the semantic
                # predicate so it is matched with the same deterministic
                # normalization as the structural rule.
                semantic_args: dict[str, Any] = structural.to_dict()
                if semantic_query:
                    semantic_args["query"] = semantic_query
                arguments["semantic"] = semantic_args
            else:
                arguments["query"] = semantic_query
            if count is not None:
                arguments["count"] = count
            if until_time is not None:
                arguments["until_time"] = until_time
            if after_time or after_clock:
                arguments["after_time"] = after_time or after_clock
            return ActionParseResult(
                kind=KIND_EXECUTABLE,
                action="delete_messages",
                target=("replied_message" if has_reply and boundary else "current_message" if boundary else "recent_messages"),
                count=count,
                mode=mode,
                until_time=until_time or "",
                after_time=after_time or after_clock or "",
                query=semantic_query,
                semantic=arguments.get("semantic"),
                tool_calls=[{"name": "delete", "arguments": arguments}],
            )
        if semantic:
            # Ambiguous semantic requests and explicit search/list workflows
            # remain on the existing provider-backed semantic tool path; they
            # must never fall through to positional last-message deletion.
            return ActionParseResult(kind=KIND_CONVERSATIONAL)

        # Explicit whole-range, time, and replied-boundary requests are
        # deterministic scope instructions; the service still enforces
        # ownership immediately before Telegram deletion.
        if (
            _is_all_delete(words)
            and _extract_until_time(words) is None
            and not _has_message_boundary(words, has_reply)
        ):
            return ActionParseResult(
                kind=KIND_EXECUTABLE,
                action="delete_messages",
                target="recent_messages",
                mode="all",
                tool_calls=[{"name": "delete", "arguments": {"mode": "all"}}],
            )
        until_time = _extract_until_time(words)
        after_time = "today" if any(word in _TODAY_WORDS for word in words) else ""
        after_clock = _extract_after_time(words)
        if after_clock and not after_time:
            after_time = after_clock
        if until_time is not None:
            arguments = {"mode": "until_time", "until_time": until_time}
            if after_time:
                arguments["after_time"] = after_time
            return ActionParseResult(
                kind=KIND_EXECUTABLE,
                action="delete_messages",
                target="recent_messages",
                mode="until_time",
                until_time=until_time,
                after_time=after_time,
                tool_calls=[{"name": "delete", "arguments": arguments}],
            )
        if after_clock:
            return ActionParseResult(
                kind=KIND_EXECUTABLE,
                action="delete_messages",
                target="recent_messages",
                mode="filtered",
                after_time=after_clock,
                tool_calls=[{"name": "delete", "arguments": {"mode": "filtered", "after_time": after_clock}}],
            )
        if _has_message_boundary(words, has_reply):
            boundary_target = "replied_message" if has_reply else "current_message"
            return ActionParseResult(
                kind=KIND_EXECUTABLE,
                action="delete_messages",
                target=boundary_target,
                mode="until_message",
                tool_calls=[{"name": "delete", "arguments": {"mode": "until_message"}}],
            )

        # Explicit message-ID target: "پیام با ID 123 رو پاک کن".
        message_id = _extract_message_id(words)
        if message_id is not None:
            return ActionParseResult(
                kind=KIND_EXECUTABLE,
                action="delete_messages",
                target="message_id",
                message_id=message_id,
                tool_calls=[{"name": "delete_message_by_id", "arguments": {"message_id": message_id}}],
            )
        if is_last or count is not None:
            n = count or 1
            target = "last_message" if (is_last and n == 1) else "recent_messages"
            return ActionParseResult(
                kind=KIND_EXECUTABLE,
                action="delete_messages",
                target=target,
                count=n,
                tool_calls=[{"name": "delete", "arguments": {"count": n}}],
            )
        if is_this:
            if has_reply:
                return ActionParseResult(
                    kind=KIND_EXECUTABLE,
                    action="delete_messages",
                    target="replied_message",
                    count=1,
                    tool_calls=[{"name": "delete_replied", "arguments": {}}],
                )
            return ActionParseResult(
                kind=KIND_CLARIFY,
                action="delete_messages",
                reason=(
                    "Reply to the message you want me to delete, or tell me "
                    "how many of your last messages to delete."
                ),
            )
        return ActionParseResult(
            kind=KIND_CLARIFY,
            action="delete_messages",
            reason="Which message(s) should I delete?",
        )

    # "Review / show / tell me the last N messages" → list REAL Telegram
    # history from the current chat (all participants). This is the AI-session
    # vs Telegram-chat distinction: inspection always reads Telegram.
    if (
        has_message_word
        and (is_last or count is not None)
        and not delete_mentioned
        and not save_mentioned
        and not send_mentioned
    ):
        limit = count if count is not None else _DEFAULT_LIST_LIMIT
        args: dict[str, Any] = {"limit": limit} if count is not None else {}
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action="list_recent_messages",
            target="recent_messages",
            count=limit,
            tool_calls=[{"name": "list_recent_messages", "arguments": args}],
        )

    status = _parse_status_intent(words, has_at="@" in text)
    if status is not None:
        return status

    # Task-management requests deliberately fall through as conversational:
    # selecting a task tool is the AI's semantic job (see the
    # "Task-management routing" note above), never per-phrase vocabulary in
    # this parser. The provider path then emits either a native task tool
    # call or a JSON action object that validate_action/resolve_tool_calls
    # accept.
    return ActionParseResult(kind=KIND_CONVERSATIONAL)
