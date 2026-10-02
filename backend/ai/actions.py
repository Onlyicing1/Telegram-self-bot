"""
Structured AI action contract — validate and resolve an AI-proposed action.

The AI model is the INTENT interpreter, and it is the only component that
decides what the owner means. This module never interprets natural-language
intent: there is no command vocabulary, no recurrence/cadence detector, no
action-verb heuristic, and no local fast path that turns a user message into a
tool call. A word like "هفتگی" / "weekly" carries no routing power here — its
meaning depends on the complete request, which only the model reads as a whole.

What this module does is take the model's structured output (a native tool call,
or a JSON action object embedded in the text response) and make it safe and
executable:

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

from backend.ai.database.task_repository import MAX_STEPS_PER_ADD
from backend.ai.persian import coerce_int, normalize_digits
from backend.ai.semantic_delete import spec_from_dict
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
    "task_delete",
    "todo_add",
    "todo_find",
    "todo_edit",
    "todo_step_add",
    "todo_step_list",
    "todo_step_transition",
    "todo_step_edit",
    "todo_step_delete",
    "retrieve_save",
    "preview_saved_item",
    "delete_saved_item",
    "rename_saved_item",
    "update_saved_item_tags",
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
    "task_delete",
    "todo_add",
    "todo_find",
    "todo_edit",
    "todo_step_add",
    "todo_step_list",
    "todo_step_transition",
    "todo_step_edit",
    "todo_step_delete",
    "retrieve_save",
    "preview_saved_item",
    "delete_saved_item",
    "rename_saved_item",
    "update_saved_item_tags",
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
    "display_name", "file_name", "tags", "title", "steps", "complete_steps",
    "step", "step_query",
})

# The Save actions — the ones that may carry the owner's saved-item metadata
# (``display_name``/``tags``) at creation time.
_SAVE_ACTIONS = ("save", "deep_save", "save_link")

# The two management actions that own the same metadata AFTER creation
# (Save V2 Part 4): a rename carries ``display_name`` (the item's label) and/or
# ``file_name`` (the ACTUAL Telegram file name) and a tag edit carries
# ``tags``. Every other action rejects those fields, so a model can never
# smuggle saved-item metadata into an unrelated execution — and neither of
# these two accepts the other's field.
_METADATA_ACTIONS = (*_SAVE_ACTIONS, "rename_saved_item", "update_saved_item_tags")

# Identity fields the account_status action may request from account_show.
# Everything else (phone, account ID, session data, credentials) is rejected.
_ACCOUNT_IDENTITY_FIELDS = frozenset({"first_name", "last_name", "full_name", "username"})

_MIN_DELETE_COUNT = 1
_MAX_DELETE_COUNT = 500

# A saved-item name/tag query is a short phrase, never a document (Save V2
# Part 3 — the resolver's query bound).
_MAX_SAVE_QUERY_CHARS = 128

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
    # The owner's optional saved-item metadata, carried verbatim from the
    # validated action into the resolved save tool call. ``""``/``None`` mean
    # "the owner supplied none" — never "an empty name"/"no tags requested".
    # ``file_name`` is the ACTUAL Telegram file name (a different layer from
    # ``display_name``) and is only ever set by an explicit rename request.
    display_name: str = ""
    file_name: str = ""
    tags: list[str] | None = None
    # Multi-step Todos: the ordered step titles a create/add carries, the step
    # reference (its 1-based number, or its own words), and the explicit flag
    # that completes a todo TOGETHER with the steps it still has.
    steps: list[str] | None = None
    step: int | None = None
    step_query: str = ""
    complete_steps: bool = False
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


def _validate_save_metadata(
    raw: dict[str, Any],
) -> tuple[str | None, list[str] | None] | ActionParseResult:
    """Validate the OPTIONAL saved-item metadata fields — shape only.

    Lengths, whitespace, duplicates, the tag count and the name bound are
    enforced by the SHARED ``save_service`` normalizer, which every surface
    goes through; duplicating them here would create a second rule that can
    drift. This layer only guarantees the model sent the right TYPES, so a
    bound violation surfaces as the service's honest refusal instead of a
    silently dropped field.

    Returns ``(display_name, tags)`` — ``None`` meaning "the owner supplied
    none" — or an ``ActionParseResult`` rejection.
    """
    display_name: str | None = None
    if "display_name" in raw:
        value = raw.get("display_name")
        if not isinstance(value, str):
            return ActionParseResult(
                kind=KIND_INVALID, error="Invalid 'display_name' field (must be text)."
            )
        display_name = value

    tags: list[str] | None = None
    if "tags" in raw:
        value = raw.get("tags")
        if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
            return ActionParseResult(
                kind=KIND_INVALID,
                error="Invalid 'tags' field (must be a list of strings).",
            )
        tags = list(value)
    return display_name, tags


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

    # ``display_name``/``tags`` are the owner's own saved-item metadata. They
    # are only meaningful for the Save actions (at creation) and for the two
    # management actions that own them afterwards — and only when the owner
    # asked for them.
    if ("display_name" in raw or "tags" in raw) and action not in _METADATA_ACTIONS:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=(
                "'display_name'/'tags' are only valid for the save, deep_save, "
                "save_link, rename_saved_item and update_saved_item_tags actions."
            ),
        )

    # ``file_name`` is the item's ACTUAL Telegram file name — a separate
    # layer the owner must ask for explicitly, so it is valid for the rename
    # action alone (never for a save at creation time and never for a tag
    # edit, which cannot carry a file name at all).
    if "file_name" in raw and action != "rename_saved_item":
        return ActionParseResult(
            kind=KIND_INVALID,
            error="'file_name' is only valid for the rename_saved_item action.",
        )

    # ``title`` is the owner's own words for a todo — a create or a rename.
    # Nothing else can carry it: a task's own content lives in its actions.
    if "title" in raw and action not in (
        "todo_add",
        "todo_edit",
        "todo_step_add",
        "todo_step_edit",
    ):
        return ActionParseResult(
            kind=KIND_INVALID,
            error=(
                "'title' is only valid for the todo_add/todo_edit and "
                "todo_step_add/todo_step_edit actions."
            ),
        )

    # ``save_code`` is only meaningful for the saved-item actions. It is
    # validated as a bounded string here; the canonical `S####` shape is
    # enforced by the tool (the code travels verbatim, upper-cased at the
    # service boundary).
    if "save_code" in raw and action not in _SAVE_ITEM_ACTIONS:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=(
                "'save_code' is only valid for the saved-item actions "
                "(retrieve_save, preview_saved_item, delete_saved_item, "
                "rename_saved_item, update_saved_item_tags)."
            ),
        )

    # ``task_id``/``expected_version``/``action`` status fields are only
    # meaningful for the task lifecycle actions.
    _TASK_ONLY_FIELDS = ("task_id", "expected_version")
    # The multi-step Todo actions address their PARENT todo by the same id, so
    # ``task_id`` is valid for them too (``expected_version`` is not: a step
    # carries its own CAS version, read by the resolver).
    if action not in (
        "task_inspect",
        "task_transition",
        "task_delete",
        "todo_edit",
        "todo_step_add",
        "todo_step_list",
        "todo_step_transition",
        "todo_step_edit",
        "todo_step_delete",
    ):
        for field_name in _TASK_ONLY_FIELDS:
            if field_name in raw:
                return ActionParseResult(
                    kind=KIND_INVALID,
                    error=(
                        f"'{field_name}' is only valid for the "
                        "task_inspect/task_transition/todo_edit actions."
                    ),
                )
    # A step mutation never carries a todo version: the resolver reads the
    # todo and the step it belongs to, and the write is guarded by the STEP's
    # own version.
    if "expected_version" in raw and action.startswith("todo_step_"):
        return ActionParseResult(
            kind=KIND_INVALID,
            error="'expected_version' is not used by the todo_step actions.",
        )

    # ``status`` is only meaningful for the task_list action (an optional
    # status filter on the read). The lifecycle actions express their target
    # status via ``action_status`` — never via ``status``.
    if "status" in raw and action != "task_list":
        return ActionParseResult(
            kind=KIND_INVALID,
            error="'status' is only valid for the task_list action.",
        )

    if action in ("task_inspect", "task_transition", "task_delete"):
        return _validate_task_lifecycle_action(action, raw)

    if action in ("todo_add", "todo_find", "todo_edit"):
        return _validate_todo_action(action, raw)

    if action in (
        "todo_step_add",
        "todo_step_list",
        "todo_step_transition",
        "todo_step_edit",
        "todo_step_delete",
    ):
        return _validate_todo_step_action(action, raw)

    if action in _SAVE_ITEM_ACTIONS:
        return _validate_saved_item_action(action, raw)

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
                    or status_value.strip().lower() not in _TASK_LIST_STATUS_VOCABULARY
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
        metadata = _validate_save_metadata(raw)
        if isinstance(metadata, ActionParseResult):
            return metadata
        display_name, tags = metadata
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action=action,
            target="telegram_link",
            link=url,
            display_name=display_name or "",
            tags=tags,
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

    # Save's optional owner metadata travels with the validated action so
    # ``resolve_tool_calls`` (and therefore the SaveTool) receives it. Every
    # other action keeps the field defaults: the guard above already rejects
    # metadata on a non-save action.
    display_name, tags = "", None
    if action in ("save", "deep_save"):
        metadata = _validate_save_metadata(raw)
        if isinstance(metadata, ActionParseResult):
            return metadata
        display_name, tags = metadata[0] or "", metadata[1]

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
        display_name=display_name,
        tags=tags,
    )


# ── Task lifecycle + saved-item retrieval actions ──

_TASK_INSPECT_FIELDS = frozenset({"action", "task_id"})
# ``complete_steps`` completes a todo TOGETHER with the steps it still has —
# never silently, and never without the explicit flag.
_TASK_TRANSITION_FIELDS = frozenset({"action", "task_id", "action_status", "expected_version", "query", "complete_steps"})
_TASK_DELETE_FIELDS = frozenset({"action", "task_id", "expected_version", "query"})
# The basic Todo actions. ``title`` is the owner's own words for the todo (a
# create or a rename) and ``query`` is a TITLE REFERENCE the deterministic
# resolver turns into 0/1/N candidates — never a guess. ``steps`` creates the
# todo's ordered steps in the SAME operation (all or nothing).
_TODO_ADD_FIELDS = frozenset({"action", "title", "steps"})
_TODO_FIND_FIELDS = frozenset({"action", "query"})
_TODO_EDIT_FIELDS = frozenset({"action", "title", "task_id", "expected_version", "query"})
# The step actions. The PARENT todo is addressed exactly like the todo actions
# above (``task_id`` or the owner's own words in ``query``); the STEP is
# addressed by its 1-based number (``step``) or by its own words
# (``step_query``) — scoped to that parent only.
_TODO_STEP_ADD_FIELDS = frozenset({"action", "steps", "title", "task_id", "query"})
_TODO_STEP_LIST_FIELDS = frozenset({"action", "task_id", "query"})
_TODO_STEP_TRANSITION_FIELDS = frozenset({"action", "action_status", "step", "step_query", "task_id", "query"})
_TODO_STEP_EDIT_FIELDS = frozenset({"action", "title", "step", "step_query", "task_id", "query"})
_TODO_STEP_DELETE_FIELDS = frozenset({"action", "step", "step_query", "task_id", "query"})
# The step vocabulary: a step is completed or reopened, nothing else.
_STEP_STATUS_VOCABULARY = frozenset({"completed", "active"})
# The todo title bound — the SAME 256-character bound the task service and the
# repository enforce for every label.
_MAX_TODO_TITLE_CHARS = 256
# A title reference is a short phrase (mirrors the task service's bound).
_MAX_TODO_QUERY_CHARS = 128
# task_list's optional status filter (the normal list excludes a legacy
# ``deleted`` row, see TaskManagementService).
_TASK_LIST_STATUS_VOCABULARY = frozenset({"paused", "active", "completed"})
# task_transition targets, mirroring the registered tool's action enum.
# ``deleted`` is NOT one of them: deleting a task is the dedicated
# ``task_delete`` action, which removes the durable row (never a status write).
_TASK_TRANSITION_STATUS_VOCABULARY = frozenset({"paused", "active", "completed"})
# The save-code value shape: canonical uppercase ``S0001`` form with an
# optional short lowercase/loose variant accepted from model output before
# normalization. This is the ONE declared shape contract — the token
# classifiers below (``_SAVE_CODE_TOKEN_RE`` / ``_SAVE_CODE_RANDOM_TOKEN_RE``)
# are its tokenized twin, and the retrieval resolver keeps its own
# owner-typography variants (``retrieve_service._SAVE_CODE_SHAPE``).
_SAVE_CODE_RE = re.compile(r"^[A-Z0-9]{1,12}$")

# Actions that address ONE stored item.
_SAVE_ITEM_ACTIONS = (
    "retrieve_save",
    "preview_saved_item",
    "delete_saved_item",
    "rename_saved_item",
    "update_saved_item_tags",
)

# The saved-item actions that ALSO accept a name/tag ``query`` INSTEAD of a
# code. They all go through the SAME deterministic resolver (Save V2 Parts
# 3–4), which turns the owner's words into 0/1/N candidates and lets the tool
# act only on an exact unique match. ``preview_saved_item`` and
# ``delete_saved_item`` stay code-only — previewing or deleting one row out of
# a fuzzy multi-match is deliberately not reachable from a model string.
_SAVE_ITEM_QUERY_ACTIONS = (
    "retrieve_save",
    "rename_saved_item",
    "update_saved_item_tags",
)

# The tag operations ``update_saved_item_tags`` accepts. The operation is
# ALWAYS explicit — never inferred from the tag list — so "add these tags" and
# "set the tags to exactly this" cannot be confused, and clearing every tag is
# ``replace`` with an empty list (the one documented empty vs. no-tags form).
_SAVE_TAG_MODES = ("add", "replace", "remove")


def _validate_task_lifecycle_action(action: str, raw: dict[str, Any]) -> ActionParseResult:
    """Validate one task_lifecycle action object.

    The status field is read from ``action_status`` so it can never collide
    with the action name itself. The model supplies only the task id, the
    target status, and the CAS version it learned from task_list/task_inspect
    — ownership, persistence, and transition legality stay in the existing
    TaskManagementService/TaskRepository boundary.
    """
    if action == "task_inspect":
        allowed = _TASK_INSPECT_FIELDS
    elif action == "task_delete":
        allowed = _TASK_DELETE_FIELDS
    else:
        allowed = _TASK_TRANSITION_FIELDS
    unknown = sorted(set(raw) - allowed)
    if unknown:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=f"Unknown field(s) for {action}: {', '.join(unknown)}",
        )

    # ``complete_steps`` finishes a todo together with its remaining steps; it
    # is an explicit boolean and is only meaningful for a completion, so it can
    # never silently ride along with a pause/resume/reopen.
    complete_steps = raw.get("complete_steps")
    if complete_steps is not None and not isinstance(complete_steps, bool):
        return ActionParseResult(
            kind=KIND_INVALID,
            error="'complete_steps' must be true or false.",
        )

    # A todo can be addressed by the OWNER'S OWN WORDS instead of an id: the
    # tool's deterministic resolver then decides which todo (0 matches ->
    # nothing was found, 2 or more -> the candidate list). No id or version is
    # guessed here, and an ambiguous reference can never reach a mutation.
    query = raw.get("query")
    query = query.strip() if isinstance(query, str) else ""
    if query:
        task_id = coerce_int(raw.get("task_id"))
        version = coerce_int(raw.get("expected_version"))
        if task_id is not None or version is not None:
            return ActionParseResult(
                kind=KIND_INVALID,
                error=(
                    f"Provide either 'task_id' with 'expected_version' or "
                    f"'query' for {action}, not both."
                ),
            )
        if len(query) > _MAX_TODO_QUERY_CHARS:
            return ActionParseResult(
                kind=KIND_INVALID,
                error=f"'query' for {action} must be at most {_MAX_TODO_QUERY_CHARS} characters.",
            )
        if action == "task_delete":
            return ActionParseResult(
                kind=KIND_EXECUTABLE, action=action, target="schedule", query=query
            )
        status = raw.get("action_status")
        if (
            not isinstance(status, str)
            or status.strip().lower() not in _TASK_TRANSITION_STATUS_VOCABULARY
        ):
            return ActionParseResult(
                kind=KIND_INVALID,
                error=(
                    "Invalid 'action_status' for task_transition "
                    "(allowed: paused, active, completed)."
                ),
            )
        if complete_steps and status.strip().lower() != "completed":
            return ActionParseResult(
                kind=KIND_INVALID,
                error="'complete_steps' is only valid with action_status 'completed'.",
            )
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action=action,
            target="schedule",
            query=query,
            action_status=status.strip().lower(),
            complete_steps=bool(complete_steps),
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

    if action == "task_delete":
        version = coerce_int(raw.get("expected_version"))
        if version is None or version <= 0:
            return ActionParseResult(
                kind=KIND_INVALID,
                error="Missing or invalid 'expected_version' for task_delete.",
            )
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action=action,
            target="schedule",
            task_id=task_id,
            expected_version=version,
        )

    status = raw.get("action_status")
    if not isinstance(status, str) or status.strip().lower() not in _TASK_TRANSITION_STATUS_VOCABULARY:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=(
                "Invalid 'action_status' for task_transition "
                "(allowed: paused, active, completed)."
            ),
        )
    if complete_steps and status.strip().lower() != "completed":
        return ActionParseResult(
            kind=KIND_INVALID,
            error="'complete_steps' is only valid with action_status 'completed'.",
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
        complete_steps=bool(complete_steps),
    )


def _validate_todo_action(action: str, raw: dict[str, Any]) -> ActionParseResult:
    """Validate one basic Todo action object.

    The todo's title (``title``) is the owner's own content and travels
    verbatim into the tool; a title REFERENCE (``query``) is resolved by the
    tool's deterministic resolver, which refuses to pick among several
    matches. Ownership, persistence and the CAS version stay in the existing
    TaskCreationService/TaskManagementService boundary.
    """
    allowed = {
        "todo_add": _TODO_ADD_FIELDS,
        "todo_find": _TODO_FIND_FIELDS,
        "todo_edit": _TODO_EDIT_FIELDS,
    }[action]
    unknown = sorted(set(raw) - allowed)
    if unknown:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=f"Unknown field(s) for {action}: {', '.join(unknown)}",
        )

    if action == "todo_find":
        query = raw.get("query")
        if not isinstance(query, str) or not query.strip():
            return ActionParseResult(
                kind=KIND_INVALID,
                error="Missing or invalid 'query' for todo_find.",
            )
        query = query.strip()
        if len(query) > _MAX_TODO_QUERY_CHARS:
            return ActionParseResult(
                kind=KIND_INVALID,
                error=f"'query' for todo_find must be at most {_MAX_TODO_QUERY_CHARS} characters.",
            )
        return ActionParseResult(kind=KIND_EXECUTABLE, action=action, query=query)

    title = raw.get("title")
    if not isinstance(title, str) or not title.strip():
        return ActionParseResult(
            kind=KIND_INVALID,
            error=f"Missing or invalid 'title' for {action}.",
        )
    title = " ".join(title.split())
    if len(title) > _MAX_TODO_TITLE_CHARS:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=f"'title' for {action} must be at most {_MAX_TODO_TITLE_CHARS} characters.",
        )
    if action == "todo_add":
        # A multi-step request creates the todo AND its ordered steps in ONE
        # validated action; the steps are the owner's own words (never
        # invented) and there is no half-created structure (see the tool).
        steps = _step_titles(raw.get("steps"), action)
        if isinstance(steps, ActionParseResult):
            return steps
        return ActionParseResult(
            kind=KIND_EXECUTABLE, action=action, text=title, steps=steps
        )

    # A rename addresses ONE todo: by id with the CAS version, or by a title
    # reference — never by both and never by neither.
    query = raw.get("query")
    query = query.strip() if isinstance(query, str) else ""
    task_id = coerce_int(raw.get("task_id"))
    version = coerce_int(raw.get("expected_version"))
    if query:
        if task_id is not None or version is not None:
            return ActionParseResult(
                kind=KIND_INVALID,
                error="Provide either 'task_id' with 'expected_version' or 'query' for todo_edit, not both.",
            )
        if len(query) > _MAX_TODO_QUERY_CHARS:
            return ActionParseResult(
                kind=KIND_INVALID,
                error=f"'query' for todo_edit must be at most {_MAX_TODO_QUERY_CHARS} characters.",
            )
        return ActionParseResult(kind=KIND_EXECUTABLE, action=action, text=title, query=query)
    if task_id is None or task_id <= 0 or version is None or version <= 0:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=(
                "Missing or invalid 'task_id'/'expected_version' for todo_edit "
                "(or provide 'query')."
            ),
        )
    return ActionParseResult(
        kind=KIND_EXECUTABLE,
        action=action,
        text=title,
        task_id=task_id,
        expected_version=version,
    )


def _step_titles(raw_steps: Any, action: str) -> list[str] | ActionParseResult:
    """Validate the ordered step titles ONE create/add action carries.

    Returns the normalized titles, or the refusal to return unchanged, so both
    the create and the add path share ONE bound and one wording for every
    rejection. Step titles are the owner's own words — never invented, never
    filled in with a placeholder — and the list is applied all-or-nothing.
    """
    if raw_steps is None:
        return []
    if not isinstance(raw_steps, list):
        return ActionParseResult(
            kind=KIND_INVALID,
            error=f"'steps' for {action} must be a list of titles.",
        )
    # An explicitly empty list means "no steps asked for": for a CREATE that is
    # the same todo a request without steps makes (the caller's own emptiness
    # check below decides whether the action needs titles at all).
    if len(raw_steps) > MAX_STEPS_PER_ADD:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=(
                f"'steps' for {action} must hold at most "
                f"{MAX_STEPS_PER_ADD} entries."
            ),
        )
    titles: list[str] = []
    for entry in raw_steps:
        if not isinstance(entry, str) or not entry.strip():
            return ActionParseResult(
                kind=KIND_INVALID,
                error=(
                    f"Every entry of 'steps' for {action} must be a "
                    "nonblank title."
                ),
            )
        text = " ".join(entry.split())
        if len(text) > _MAX_TODO_TITLE_CHARS:
            return ActionParseResult(
                kind=KIND_INVALID,
                error=(
                    f"Every entry of 'steps' for {action} must be at most "
                    f"{_MAX_TODO_TITLE_CHARS} characters."
                ),
            )
        titles.append(text)
    return titles


def _validate_todo_step_action(action: str, raw: dict[str, Any]) -> ActionParseResult:
    """Validate one multi-step Todo action object.

    The PARENT todo is addressed exactly like every other Todo action — its id
    (``task_id``) or the owner's own words (``query``), which the shared
    deterministic resolver turns into 0/1/N candidates. The STEP is addressed
    INSIDE that todo by its 1-based number (``step``) or by its own words
    (``step_query``), so a step of another todo is never in scope. Nothing is
    guessed, no id is invented, and an ambiguous reference can never reach a
    mutation: the tools read the real rows and refuse to choose.
    """
    allowed = {
        "todo_step_add": _TODO_STEP_ADD_FIELDS,
        "todo_step_list": _TODO_STEP_LIST_FIELDS,
        "todo_step_transition": _TODO_STEP_TRANSITION_FIELDS,
        "todo_step_edit": _TODO_STEP_EDIT_FIELDS,
        "todo_step_delete": _TODO_STEP_DELETE_FIELDS,
    }[action]
    unknown = sorted(set(raw) - allowed)
    if unknown:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=f"Unknown field(s) for {action}: {', '.join(unknown)}",
        )

    query = raw.get("query")
    query = query.strip() if isinstance(query, str) else ""
    if len(query) > _MAX_TODO_QUERY_CHARS:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=f"'query' for {action} must be at most {_MAX_TODO_QUERY_CHARS} characters.",
        )
    task_id = coerce_int(raw.get("task_id"))
    if task_id is not None and (task_id <= 0 or query):
        return ActionParseResult(
            kind=KIND_INVALID,
            error=f"Provide either 'task_id' or 'query' for {action}, not both.",
        )

    step = coerce_int(raw.get("step"))
    step_query = raw.get("step_query")
    step_query = step_query.strip() if isinstance(step_query, str) else ""
    if step is not None and (step <= 0 or step_query):
        return ActionParseResult(
            kind=KIND_INVALID,
            error=f"Provide either 'step' or 'step_query' for {action}, not both.",
        )
    if len(step_query) > _MAX_TODO_QUERY_CHARS:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=f"'step_query' for {action} must be at most {_MAX_TODO_QUERY_CHARS} characters.",
        )
    if (
        action in ("todo_step_transition", "todo_step_edit", "todo_step_delete")
        and step is None
        and not step_query
    ):
        return ActionParseResult(
            kind=KIND_INVALID,
            error=(
                f"Which step? 'step' (its number) or 'step_query' is required "
                f"for {action}."
            ),
        )

    if action == "todo_step_add":
        titles: list[str] = []
        title = raw.get("title")
        if title is not None:
            if not isinstance(title, str) or not title.strip():
                return ActionParseResult(
                    kind=KIND_INVALID,
                    error="Missing or invalid 'title' for todo_step_add.",
                )
            text = " ".join(title.split())
            if len(text) > _MAX_TODO_TITLE_CHARS:
                return ActionParseResult(
                    kind=KIND_INVALID,
                    error=(
                        f"'title' for todo_step_add must be at most "
                        f"{_MAX_TODO_TITLE_CHARS} characters."
                    ),
                )
            titles.append(text)
        from_list = _step_titles(raw.get("steps"), action)
        if isinstance(from_list, ActionParseResult):
            return from_list
        titles.extend(from_list)
        if not titles:
            return ActionParseResult(
                kind=KIND_INVALID,
                error="Missing 'steps' (or 'title') for todo_step_add.",
            )
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action=action,
            steps=titles,
            task_id=task_id,
            query=query,
        )

    if action == "todo_step_list":
        return ActionParseResult(
            kind=KIND_EXECUTABLE, action=action, task_id=task_id, query=query
        )

    if action == "todo_step_edit":
        title = raw.get("title")
        if not isinstance(title, str) or not title.strip():
            return ActionParseResult(
                kind=KIND_INVALID,
                error="Missing or invalid 'title' for todo_step_edit.",
            )
        text = " ".join(title.split())
        if len(text) > _MAX_TODO_TITLE_CHARS:
            return ActionParseResult(
                kind=KIND_INVALID,
                error=(
                    f"'title' for todo_step_edit must be at most "
                    f"{_MAX_TODO_TITLE_CHARS} characters."
                ),
            )
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action=action,
            text=text,
            step=step,
            step_query=step_query,
            task_id=task_id,
            query=query,
        )

    if action == "todo_step_delete":
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action=action,
            step=step,
            step_query=step_query,
            task_id=task_id,
            query=query,
        )

    status = raw.get("action_status")
    if (
        not isinstance(status, str)
        or status.strip().lower() not in _STEP_STATUS_VOCABULARY
    ):
        return ActionParseResult(
            kind=KIND_INVALID,
            error=(
                "Invalid 'action_status' for todo_step_transition "
                "(allowed: completed, active)."
            ),
        )
    return ActionParseResult(
        kind=KIND_EXECUTABLE,
        action=action,
        action_status=status.strip().lower(),
        step=step,
        step_query=step_query,
        task_id=task_id,
        query=query,
    )


def _validate_saved_item_action(action: str, raw: dict[str, Any]) -> ActionParseResult:
    """Validate one saved-item action object (exact-field-set rule).

    Every saved-item action addresses one stored item through the same code
    validation; only the resolved target differs (re-sending happens in the
    current chat, previewing/deleting/renaming/re-tagging addresses the item
    itself).

    The three query-capable actions (``retrieve_save``, ``rename_saved_item``,
    ``update_saved_item_tags``) ADDITIONALLY accept a name/tag ``query``
    INSTEAD of a code: the deterministic resolver turns it into 0/1/N
    candidates and the tool only ever acts on an exact unique match.

    A rename carries ``display_name`` (the item's logical label) and/or
    ``file_name`` (the ACTUAL Telegram file name); a tag edit carries ``tags``
    plus the explicit ``mode``. Neither accepts the other's payload field, and
    an unknown field is rejected for all five actions.
    """
    allowed = {"action", "save_code"}
    if action in _SAVE_ITEM_QUERY_ACTIONS:
        allowed.add("query")
    if action == "rename_saved_item":
        allowed.update({"display_name", "file_name"})
    if action == "update_saved_item_tags":
        allowed.update({"tags", "mode"})
    unknown = sorted(set(raw) - allowed)
    if unknown:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=f"Unknown field(s) for {action}: {', '.join(unknown)}",
        )

    # The payload of the two management actions is validated ONCE, before the
    # target is read and independently of how that target is addressed.
    display_name = ""
    file_name = ""
    tags: list[str] | None = None
    mode = ""
    if action == "rename_saved_item":
        # Either half may be asked for on its own; at least one is required.
        value = raw.get("display_name")
        if value is not None and (not isinstance(value, str) or not value.strip()):
            return ActionParseResult(
                kind=KIND_INVALID,
                error=f"Invalid 'display_name' for {action}.",
            )
        display_name = value.strip() if isinstance(value, str) else ""
        raw_file_name = raw.get("file_name")
        if raw_file_name is not None and (
            not isinstance(raw_file_name, str) or not raw_file_name.strip()
        ):
            return ActionParseResult(
                kind=KIND_INVALID,
                error=f"Invalid 'file_name' for {action}.",
            )
        file_name = raw_file_name.strip() if isinstance(raw_file_name, str) else ""
        if not display_name and not file_name:
            return ActionParseResult(
                kind=KIND_INVALID,
                error=f"Missing 'display_name' or 'file_name' for {action}.",
            )
    elif action == "update_saved_item_tags":
        value = raw.get("tags")
        if not isinstance(value, list) or not all(isinstance(t, str) for t in value):
            return ActionParseResult(
                kind=KIND_INVALID,
                error=f"Invalid 'tags' for {action} (must be a list of strings).",
            )
        tags = list(value)
        mode_value = raw.get("mode")
        if not isinstance(mode_value, str) or mode_value.strip().lower() not in _SAVE_TAG_MODES:
            return ActionParseResult(
                kind=KIND_INVALID,
                error=f"Invalid 'mode' for {action} (allowed: add, replace, remove).",
            )
        mode = mode_value.strip().lower()
        if mode != "replace" and not tags:
            return ActionParseResult(
                kind=KIND_INVALID,
                error=(
                    f"'tags' must not be empty for {action} mode '{mode}' — "
                    "use mode 'replace' with [] to clear every tag."
                ),
            )

    has_code = "save_code" in raw
    has_query = "query" in raw
    if has_code and has_query:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=f"Provide either 'save_code' or 'query' for {action} — not both.",
        )
    if has_query:
        query = raw.get("query")
        if not isinstance(query, str) or not query.strip():
            return ActionParseResult(
                kind=KIND_INVALID,
                error="Invalid 'query' (expected the saved item's name or a tag).",
            )
        query = query.strip()
        if len(query) > _MAX_SAVE_QUERY_CHARS:
            return ActionParseResult(kind=KIND_INVALID, error="Saved-item query is too long.")
        return ActionParseResult(
            kind=KIND_EXECUTABLE,
            action=action,
            target="current_chat" if action == "retrieve_save" else "saved_item",
            query=query,
            display_name=display_name,
            file_name=file_name,
            tags=tags,
            mode=mode,
        )
    if action in _SAVE_ITEM_QUERY_ACTIONS and not has_code:
        return ActionParseResult(
            kind=KIND_INVALID,
            error=f"Missing 'save_code' or 'query' for {action}.",
        )
    save_code = raw.get("save_code")
    if not isinstance(save_code, str):
        return ActionParseResult(
            kind=KIND_INVALID,
            error=f"Missing or invalid 'save_code' for {action}.",
        )
    normalized = save_code.strip().upper()
    if not normalized or not _SAVE_CODE_RE.match(normalized):
        return ActionParseResult(
            kind=KIND_INVALID,
            error="Invalid 'save_code' (expected the item's save code, e.g. S0001).",
        )
    return ActionParseResult(
        kind=KIND_EXECUTABLE,
        action=action,
        target="current_chat" if action == "retrieve_save" else "saved_item",
        save_code=normalized,
        display_name=display_name,
        file_name=file_name,
        tags=tags,
        mode=mode,
    )


# ── Target resolution ──


def _default_target(action: str) -> str:
    if action in ("save", "deep_save"):
        return "replied_message"
    if action == "retrieve_save":
        return "current_chat"
    if action in (
        "preview_saved_item",
        "delete_saved_item",
        "rename_saved_item",
        "update_saved_item_tags",
    ):
        return "saved_item"
    if action in ("task_list", "task_inspect", "task_transition", "task_delete"):
        return "schedule"
    return "recent_messages"


def _save_metadata_arguments(result: ActionParseResult) -> dict[str, Any]:
    """The metadata keys a Save tool call carries.

    Absent means "the owner supplied none" — the save service then stores
    ``NULL``/``'{}'``. An explicitly declined tag list arrives as ``[]`` and is
    carried as ``[]``, which the shared normalizer turns into no owner tags.
    """
    arguments: dict[str, Any] = {}
    if result.display_name:
        arguments["display_name"] = result.display_name
    if result.tags is not None:
        arguments["tags"] = list(result.tags)
    return arguments


def _parent_arguments(result: ActionParseResult) -> dict[str, Any]:
    """The parent-todo addressing arguments (its id, or the owner's own words).

    A title reference travels verbatim; the tool's deterministic resolver then
    answers 0/1/N and refuses to choose among several matches.
    """
    if result.query:
        return {"query": result.query}
    if result.task_id:
        return {"task_id": int(result.task_id)}
    return {}


def _step_arguments(result: ActionParseResult) -> dict[str, Any]:
    """The parent todo PLUS the step reference (1-based number or own words)."""
    arguments = _parent_arguments(result)
    if result.step is not None:
        arguments["step"] = int(result.step)
    if result.step_query:
        arguments["step_query"] = result.step_query
    return arguments


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
        # preserved by the existing deep-save pipeline. The owner's optional
        # metadata travels as the tool's own optional arguments.
        return [{"name": "save", "arguments": _save_metadata_arguments(result)}]

    if action == "save_link":
        # The existing execute_link_save() resolves the link and reuses the
        # SAME Deep Save pipeline. The URL is passed through verbatim.
        return [
            {
                "name": "save_by_link",
                "arguments": {"link": result.link, **_save_metadata_arguments(result)},
            }
        ]

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
        # A title reference resolves inside the tool (deterministically, and
        # only for a todo); an id keeps its explicit CAS version. The explicit
        # ``complete_steps`` flag travels with the completion (never implied).
        if result.query:
            transition_arguments: dict[str, Any] = {
                "query": result.query,
                "action": result.action_status,
            }
            if result.complete_steps:
                transition_arguments["complete_steps"] = True
            return [{"name": "task_transition", "arguments": transition_arguments}]
        transition_arguments = {
            "task_id": result.task_id,
            "action": result.action_status,
            "expected_version": result.expected_version,
        }
        if result.complete_steps:
            transition_arguments["complete_steps"] = True
        return [{"name": "task_transition", "arguments": transition_arguments}]

    if action == "task_delete":
        # Deletion is a REAL row removal through the dedicated tool/service
        # operation — never a task_transition status write. A todo addressed
        # by title travels as the resolver's query input; the tool then
        # resolves at most ONE todo and refuses an ambiguous reference.
        if result.query:
            return [{"name": "task_delete", "arguments": {"query": result.query}}]
        return [{
            "name": "task_delete",
            "arguments": {
                "task_id": result.task_id,
                "expected_version": result.expected_version,
            },
        }]

    if action == "todo_add":
        # A multi-step create travels as ONE call: the title AND the ordered
        # steps, so the todo and its steps are created together (all or none).
        add_arguments = {"title": result.text}
        if result.steps:
            add_arguments["steps"] = list(result.steps)
        return [{"name": "todo_add", "arguments": add_arguments}]

    if action == "todo_find":
        return [{"name": "todo_find", "arguments": {"query": result.query}}]

    if action == "todo_step_add":
        step_add_arguments: dict[str, Any] = {"steps": list(result.steps or [])}
        step_add_arguments.update(_parent_arguments(result))
        return [{"name": "todo_step_add", "arguments": step_add_arguments}]

    if action == "todo_step_list":
        return [{"name": "todo_step_list", "arguments": _parent_arguments(result)}]

    if action == "todo_step_transition":
        step_arguments = _step_arguments(result)
        step_arguments["action"] = result.action_status
        return [{"name": "todo_step_transition", "arguments": step_arguments}]

    if action == "todo_step_edit":
        step_edit_arguments = _step_arguments(result)
        step_edit_arguments["title"] = result.text
        return [{"name": "todo_step_edit", "arguments": step_edit_arguments}]

    if action == "todo_step_delete":
        return [{"name": "todo_step_delete", "arguments": _step_arguments(result)}]

    if action == "todo_edit":
        # The new title travels verbatim; the target is either the id with its
        # CAS version or the owner's own words, which the tool resolves
        # deterministically (never picking among several matches).
        arguments: dict[str, Any] = {"title": result.text}
        if result.query:
            arguments["query"] = result.query
        else:
            arguments["task_id"] = result.task_id
            arguments["expected_version"] = result.expected_version
        return [{"name": "todo_edit", "arguments": arguments}]

    if action == "retrieve_save":
        # A name/tag request travels as the resolver's query input; the tool
        # resolves it deterministically and retrieves only a unique match.
        if result.query:
            return [{"name": "retrieve_save", "arguments": {"query": result.query}}]
        return [{"name": "retrieve_save", "arguments": {"save_code": result.save_code}}]

    if action == "preview_saved_item":
        return [{"name": "preview_save", "arguments": {"save_code": result.save_code}}]

    if action == "delete_saved_item":
        # Saved-items management through the registered tool; the service
        # boundary enforces owner scoping immediately before the DB row and
        # the Saved Messages copy are removed.
        return [{"name": "delete_save", "arguments": {"save_code": result.save_code}}]

    if action == "rename_saved_item":
        # The requested halves travel verbatim — the display name (the item's
        # label) and/or the actual Telegram file name. The target is the
        # resolved item (a code, or the owner's own words resolved by the
        # shared deterministic resolver, which refuses to pick among multiple
        # matches).
        args: dict[str, Any] = {}
        if result.display_name:
            args["display_name"] = result.display_name
        if result.file_name:
            args["file_name"] = result.file_name
        if result.query:
            args["query"] = result.query
        else:
            args["save_code"] = result.save_code
        return [{"name": "rename_save", "arguments": args}]

    if action == "update_saved_item_tags":
        # ``mode`` is the explicit tag operation (add / replace / remove); an
        # empty list is only meaningful with ``replace``, where it clears
        # every tag. The validated payload travels unchanged.
        args = {"tags": list(result.tags or []), "mode": result.mode}
        if result.query:
            args["query"] = result.query
        else:
            args["save_code"] = result.save_code
        return [{"name": "update_save_tags", "arguments": args}]

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
            display_name=result.display_name,
            tags=result.tags,
            tool_calls=tool_calls,
        )
    return result



# ── Authorization / provenance vocabulary (NOT intent routing) ──
#
# Everything below exists for exactly ONE purpose: deciding whether an
# AI-PROPOSED task may carry a generated-content instruction, and grounding
# that instruction to the owner's verbatim request (see
# ``backend/ai/task_contract.py::ground_ai_instruction``).
#
# It is NOT an intent router. By the time any of it runs, the model has
# already proposed ``create_task``; nothing here selects a tool, chooses a
# capability, or converts a user message into a tool call. This is the
# provenance/authorization half of the boundary — the other half is
# ``validate_action`` — and it FAILS CLOSED: an instruction the owner did not
# ask for is dropped rather than repaired from a model paraphrase.
#
# Removing it would weaken authorization, so it is deliberately retained.
# It is kept in its own section, apart from the removed command vocabulary, so
# the boundary stays auditable.

_TOKEN_RE = re.compile(r"[a-z0-9\u0621-\u06ff]+")


def _tokenize(text: str) -> list[str]:
    """Lowercase, normalize digits, and split Persian/English into word tokens.

    Character-level tokenization for provenance matching — it never selects a
    tool.
    """
    s = normalize_digits(text)
    s = s.replace("\u200c", " ").replace("\u200b", " ")
    s = s.replace("'", "").replace("’", "")
    s = s.lower()
    # \u0600-\u061f is Arabic/Persian *punctuation* (، ؛ ؟) and diacritics,
    # not letters; including it made "چیه؟" tokenize as "چیه؟" and miss
    # dictionary matches. Letters start at \u0621 (hamza) onward.
    return [t for t in _TOKEN_RE.findall(s) if t]


# An imperative write verb ("بنویس", "write") proves the owner asked for
# generated CONTENT, as opposed to a static task that only runs an action.
_WRITE_TOKENS = frozenset({"بنویس", "نویس", "write", "writing"})


def _is_write_token(tok: str) -> bool:
    """True when *tok* is an imperative write verb (بنویس/بنویسید/write)."""
    if tok in _WRITE_TOKENS:
        return True
    return tok.startswith("بنویسید") or tok.startswith("بنویسین") or tok.startswith("بنویسش")


def _write_text_present(words: list[str]) -> bool:
    return any(_is_write_token(w) for w in words)


# Bio words are stem-matched so possessive/colloquial forms ("بیوم",
# "بیوی", "بایوم") match the plain form.
_BIO_STEMS = ("بیو", "بایو")


def _has_bio_mention(words: list[str]) -> bool:
    """True when any token looks like a bio word (Persian stem or English)."""
    return any(
        w == "bio" or w.startswith(_BIO_STEMS[0]) or w.startswith(_BIO_STEMS[1])
        for w in words
    )


# Bio WRITE verbs: "change/update/set my bio to …", "بیو رو عوض کن",
# "بیو رو تغییر بده", "آپدیت کن بیو پروفایلم".
_BIO_CHANGE_WORDS = frozenset({
    "change", "update", "replace", "edit", "put",
    "عوض", "تغییر", "آپدیت", "تغییر بده",
})


def _has_bio_change_intent(words: list[str]) -> bool:
    return any(w in _BIO_CHANGE_WORDS for w in words)


# A username mention is the second provenance signal for profile-content
# generation.
_USERNAME_WORDS = frozenset({"یوزرنیم", "یوزرنیمم", "یوزر", "username"})