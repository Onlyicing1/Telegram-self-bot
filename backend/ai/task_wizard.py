"""Deterministic Taskloom task-creation wizard contract.

The wizard is a CREATION UX, not a second task system. Structured owner
choices are mapped to the SAME ``TaskCandidate`` the natural-language path
produces, and the candidate is persisted through the SAME
``TaskCreationService`` — no second scheduler, executor, task repository, or
persistence path.

Only actions already valid for SCHEDULED execution are offered, and each
action's editable fields follow its registered tool contract:

    bio_set_text        per-occurrence generated content allowed
    username_set_text   per-occurrence generated content allowed
    send_message        static text only — the candidate contract requires a
                        bounded non-blank message body, so it cannot be
                        AI-generated per occurrence

For generated content the wizard composes the durable ``ai_instruction``
from the selected fields in a canonical phrasing that the existing
``preparation_policy.derive_policy`` parses deterministically (language,
inclusive maximum length, named source, source display). Source display is
an explicitly OPT-IN presentation flag whose default is OFF: a named source
constrains generation without putting its name on screen, and the instruction
only carries the show clause when the owner asked for it. The composition is
verified by round-tripping it through ``derive_policy`` before a candidate is
ever returned, so the review screen can only show constraints that are
actually enforced at occurrence time.

The wizard NEVER generates content at creation time: the occurrence path
generates and validates fresh content per run.

Module status: STATELESS. Every function is pure over an immutable draft;
the single per-owner in-progress draft lives in the Telegram handler.
"""
from __future__ import annotations

from dataclasses import dataclass, replace
from datetime import datetime
from typing import Any
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from backend.ai.preparation_policy import derive_policy

MAX_TEXT_CHARS = 4096
MAX_SOURCE_CHARS = 64
MAX_LABEL_CHARS = 256
MAX_INTERVAL_MINUTES = 7 * 24 * 60
MAX_LENGTH_VALUE = 1000

STEP_ACTION = "action"
STEP_CONTENT = "content"
STEP_DETAILS = "details"
STEP_SCHEDULE = "schedule"
STEP_REVIEW = "review"
# EDIT-mode hub: the entry step of the editor, where each row jumps straight to
# the field it names (never used by the creation flow).
STEP_EDIT = "edit"

STATIC_MODE = "static"
AI_MODE = "ai"

WEEKDAYS = ("Monday", "Tuesday", "Wednesday", "Thursday", "Friday", "Saturday", "Sunday")

# Wizard language choice -> the word the deterministic policy recognizes.
# ("" = no language constraint; the existing "no language named" semantics.)
LANGUAGE_NAMES = (
    ("en", "English"),
    ("fa", "Persian"),
    ("ar", "Arabic"),
    ("zh", "Chinese"),
)
LANGUAGE_POLICY = {"en": "english", "fa": "persian", "ar": "arabic", "zh": "chinese"}
LANGUAGE_WORDS = dict(LANGUAGE_NAMES)

# The durable clause that turns the opt-in source display ON. The
# deterministic policy recognizes it, so the flag cannot drift from what is
# actually enforced at occurrence time.
SHOW_SOURCE_CLAUSE = "show the source name"


class TaskWizardError(ValueError):
    """The wizard draft cannot produce a safe, deterministic task candidate."""


@dataclass(frozen=True)
class ActionDefinition:
    key: str
    button: str
    title: str
    tool: str
    instruction_verb: str
    ai_clause: str
    ai_noun: str

    @property
    def supports_ai(self) -> bool:
        return bool(self.ai_clause)


ACTION_DEFINITIONS: dict[str, ActionDefinition] = {
    "bio": ActionDefinition(
        "bio", "🧬 Bio update", "Update Bio", "bio_set_text",
        "update my bio", "with a randomly generated dialogue", "dialogue",
    ),
    "username": ActionDefinition(
        "username", "👤 Username update", "Update Username", "username_set_text",
        "update my username", "with a randomly generated line", "line",
    ),
    "message": ActionDefinition(
        "message", "📨 Write a message", "Write a message", "send_message",
        "write a message", "", "",
    ),
}
ACTION_ORDER = ("bio", "username", "message")
# Canonical registered tool -> wizard action key (edit-mode prefill).
_ACTION_BY_TOOL = {definition.tool: key for key, definition in ACTION_DEFINITIONS.items()}
SCHEDULE_TYPES = ("once", "interval", "daily", "weekly")
SCHEDULE_BUTTONS = (
    ("once", "Once…"),
    ("interval", "Every N minutes…"),
    ("daily", "Daily…"),
    ("weekly", "Weekly…"),
)


@dataclass(frozen=True)
class TaskDraft:
    """One in-progress wizard draft (per-owner UI state, no authority)."""

    action: str = ""
    step: str = STEP_ACTION
    content_mode: str = ""
    # Display label of the task being EDITED. Creation derives the label from
    # the action; an edit keeps the stored one so an untouched field (and an
    # untouched label) are never rewritten behind the owner's back.
    label: str = ""
    text: str = ""
    source: str = ""
    language: str = ""
    max_length: int | None = None
    # Presentation flag, default OFF: the source constrains generation but is
    # not displayed unless the owner explicitly asked for it.
    show_source: bool = False
    schedule_type: str = ""
    interval_minutes: int | None = None
    clock: str = ""
    weekday: int | None = None
    once_at: str = ""
    timezone: str = "UTC"
    notice: str = ""
    # Display-font key for a static message action (the canonical
    # ``backend.helper.font_style`` allow-list; no second Unicode system).
    font: str = "default"
    # Non-zero when this draft is EDITING an existing task: the same draft
    # contract and the same CAS update path, never a second task definition.
    editing_task_id: int = 0
    editing_version: int = 0

    def updated(self, **changes: Any) -> "TaskDraft":
        return replace(self, **changes)


# ── field parsing (deterministic, bounded) ──────────────────────────────────


def valid_timezone(name: str) -> bool:
    if not isinstance(name, str) or not name.strip():
        return False
    if name.strip() == "UTC":
        return True
    try:
        ZoneInfo(name.strip())
    except (ZoneInfoNotFoundError, ValueError, KeyError):
        return False
    return True


def clean_source(text: str) -> str:
    raw = " ".join((text or "").split())
    if raw.lower() in {"none", "any", "clear", "off", "-"}:
        return ""
    if len(raw) > MAX_SOURCE_CHARS:
        raise TaskWizardError(f"the source must be at most {MAX_SOURCE_CHARS} characters")
    return raw


def parse_clock(text: str) -> tuple[int, int]:
    raw = " ".join((text or "").split()).replace("٫", ":")
    if not raw:
        raise TaskWizardError("enter a time as HH:MM")
    parts = raw.split(":")
    if len(parts) > 2:
        raise TaskWizardError("enter a time as HH:MM")
    try:
        hour = int(parts[0])
        minute = int(parts[1]) if len(parts) == 2 else 0
    except ValueError as exc:
        raise TaskWizardError("enter a time as HH:MM") from exc
    if not (0 <= hour <= 23 and 0 <= minute <= 59):
        raise TaskWizardError("the time must be between 00:00 and 23:59")
    return hour, minute


def parse_interval_minutes(text: str) -> int:
    try:
        minutes = int(" ".join((text or "").split()))
    except ValueError as exc:
        raise TaskWizardError("enter the interval as a whole number of minutes") from exc
    if not 1 <= minutes <= MAX_INTERVAL_MINUTES:
        raise TaskWizardError(f"the interval must be 1–{MAX_INTERVAL_MINUTES} minutes")
    return minutes


def parse_max_length(text: str) -> int | None:
    raw = " ".join((text or "").split()).lower()
    if raw in {"none", "any", "clear", "off", "-"}:
        return None
    try:
        value = int(raw)
    except ValueError as exc:
        raise TaskWizardError("enter the maximum length as a whole number") from exc
    if not 1 <= value <= MAX_LENGTH_VALUE:
        raise TaskWizardError(f"the maximum length must be 1–{MAX_LENGTH_VALUE} characters")
    return value


def parse_once_at(text: str) -> str:
    raw = " ".join((text or "").split())
    for fmt in (
        "%Y-%m-%d %H:%M",
        "%Y-%m-%dT%H:%M",
        "%Y-%m-%d %H:%M:%S",
        "%Y-%m-%dT%H:%M:%S",
    ):
        try:
            return datetime.strptime(raw, fmt).isoformat()
        except ValueError:
            continue
    raise TaskWizardError("enter the start as YYYY-MM-DD HH:MM")


# ── instruction composition (the durable content contract) ──────────────────


def valid_font_key(key: str) -> bool:
    """Whether *key* is on the canonical display-font allow-list."""
    from backend.helper.font_style import is_valid_font

    return is_valid_font((key or "").strip())


def font_preview(key: str) -> str:
    """The selected font's own deterministic preview (canonical transform)."""
    from backend.helper.font_style import apply_font, normalize_font_key

    return apply_font("Abc 123", normalize_font_key(key))


def font_label(key: str) -> str:
    """Human label for the selected font key ("Default" when unset)."""
    from backend.helper.font_style import DEFAULT_FONT_KEY, normalize_font_key

    normalized = normalize_font_key(key)
    return "Default" if normalized == DEFAULT_FONT_KEY else normalized


def build_instruction(draft: TaskDraft) -> str:
    """Compose the canonical ``ai_instruction`` for the selected fields.

    The composition is what the deterministic preparation policy parses at
    occurrence time; it is verified by ``_instruction_problem`` before any
    candidate is returned, so a value the policy cannot represent is a hard
    error instead of a silently unenforced review line.
    """
    definition = ACTION_DEFINITIONS.get(draft.action)
    if definition is None:
        raise TaskWizardError("choose an action first")
    if not definition.supports_ai:
        raise TaskWizardError(f"{definition.title} tasks require static text")
    clauses = [definition.instruction_verb, definition.ai_clause]
    if draft.language in LANGUAGE_WORDS:
        clauses.append(f"in {LANGUAGE_WORDS[draft.language]}")
    if draft.max_length is not None:
        clauses.append(f"at most {draft.max_length} characters")
    if draft.source and draft.show_source:
        clauses.append(SHOW_SOURCE_CLAUSE)
    if draft.source:
        clauses.append(f"from {draft.source}")
    return ", ".join(clause for clause in clauses if clause)


def _instruction_problem(draft: TaskDraft, instruction: str) -> str | None:
    """Round-trip guard: the policy must derive exactly what was selected."""
    policy = derive_policy(instruction)
    expected_language = LANGUAGE_POLICY.get(draft.language) if draft.language else None
    if policy.language != expected_language:
        return "the selected language cannot be represented in the task contract"
    if policy.max_length != draft.max_length:
        return "the selected maximum length cannot be represented in the task contract"
    if policy.show_source != draft.show_source:
        return "the source display preference cannot be represented in the task contract"
    if draft.source:
        if policy.source.casefold() != draft.source.casefold():
            return "the selected source cannot be represented in the task contract"
    elif policy.source:
        return "the task contract derived an unexpected source"
    return None


def instruction_problem(draft: TaskDraft) -> str | None:
    """Why the composed instruction cannot represent the selected fields.

    Independent of schedule completeness, so a single field (source,
    language, maximum length) can be validated the moment it is entered.
    """
    try:
        instruction = build_instruction(draft)
    except TaskWizardError as exc:
        return str(exc)
    return _instruction_problem(draft, instruction)


# ── candidate construction (no persistence) ─────────────────────────────────


def _schedule_payload(
    draft: TaskDraft, timezone: str, reference: datetime | None,
) -> dict[str, Any]:
    kind = draft.schedule_type
    if kind == "interval":
        minutes = draft.interval_minutes
        if not isinstance(minutes, int) or minutes <= 0:
            raise TaskWizardError("enter the interval in minutes")
        return {"seconds": minutes * 60}
    if kind == "daily":
        hour, minute = parse_clock(draft.clock)
        return {"hour": hour, "minute": minute, "timezone": timezone}
    if kind == "weekly":
        weekday = draft.weekday
        if not isinstance(weekday, int) or not 0 <= weekday <= 6:
            raise TaskWizardError("choose a weekday")
        hour, minute = parse_clock(draft.clock)
        return {"weekday": weekday, "hour": hour, "minute": minute, "timezone": timezone}
    if kind == "once":
        at = parse_once_at(draft.once_at)
        if reference is not None:
            moment = datetime.fromisoformat(at).replace(tzinfo=ZoneInfo(timezone))
            if reference.tzinfo is None:
                raise TaskWizardError("reference datetime must be timezone-aware")
            if moment <= reference:
                raise TaskWizardError("the start time must be in the future")
        return {"at": at, "timezone": timezone}
    raise TaskWizardError("choose a schedule (once, every N minutes, daily, or weekly)")


def _label(draft: TaskDraft, definition: ActionDefinition) -> str:
    if draft.editing_task_id and draft.label.strip():
        return draft.label.strip()[:MAX_LABEL_CHARS]
    if definition.key == "message":
        return (draft.text.strip() or "Message")[:MAX_LABEL_CHARS]
    return definition.title


def build_candidate(
    draft: TaskDraft, chat_id: int = 0, reference: datetime | None = None,
) -> dict[str, Any]:
    """Map a complete draft to the existing task candidate contract.

    Raises ``TaskWizardError`` with the first missing/invalid requirement, so
    the wizard can never create a task without a valid schedule or with a
    constraint the deterministic policy would not enforce.
    """
    definition = ACTION_DEFINITIONS.get(draft.action)
    if definition is None:
        raise TaskWizardError("choose an action (Bio, Username, or Message) first")
    mode = draft.content_mode
    if mode not in (AI_MODE, STATIC_MODE):
        raise TaskWizardError("choose a content mode")
    if mode == AI_MODE and not definition.supports_ai:
        raise TaskWizardError(f"{definition.title} tasks require static text")
    text = draft.text.strip()
    if mode == STATIC_MODE:
        if not text:
            raise TaskWizardError("static content text is required")
        if len(text) > MAX_TEXT_CHARS:
            raise TaskWizardError(f"static content must be at most {MAX_TEXT_CHARS} characters")
    timezone = " ".join((draft.timezone or "").split())
    if not valid_timezone(timezone):
        raise TaskWizardError("the timezone must be a valid IANA name")
    schedule = _schedule_payload(draft, timezone, reference)
    arguments: dict[str, Any] = {"text": "" if mode == AI_MODE else text}
    if mode == STATIC_MODE and definition.key == "message":
        # The display font travels as a bounded allow-listed key on the
        # action; the send tool applies the canonical transform at execution
        # time, so the stored definition stays the RAW text (re-editable and
        # never double-styled).
        from backend.helper.font_style import DEFAULT_FONT_KEY, normalize_font_key

        font = normalize_font_key(draft.font)
        if font != DEFAULT_FONT_KEY:
            arguments["font"] = font
    candidate: dict[str, Any] = {
        "label": _label(draft, definition),
        "schedule_type": draft.schedule_type,
        "schedule": schedule,
        "timezone": timezone,
        "actions": [{"name": definition.tool, "arguments": arguments}],
        "notification_destination": (
            {"chat_id": int(chat_id)}
            if definition.key == "message" and isinstance(chat_id, int) and chat_id != 0
            else {}
        ),
    }
    if mode == AI_MODE:
        instruction = build_instruction(draft)
        problem = _instruction_problem(draft, instruction)
        if problem:
            raise TaskWizardError(problem)
        candidate["ai_instruction"] = instruction
    return candidate


def missing_requirement(
    draft: TaskDraft, chat_id: int = 0, reference: datetime | None = None,
) -> str | None:
    """The first reason this draft cannot create a task, or ``None``."""
    try:
        build_candidate(draft, chat_id, reference)
    except TaskWizardError as exc:
        return str(exc)
    return None


# ── edit-mode prefill (from a STORED task definition) ───────────────────────

def draft_from_task(task: Any, *, step: str = STEP_SCHEDULE) -> TaskDraft:
    """Prefill a draft from the owner's OWN stored task definition.

    Only persisted, already-validated values are used — never a value inferred
    from an untrusted request. The returned draft carries ``editing_task_id`` /
    ``editing_version`` so the SAME ``build_candidate`` output is persisted
    through the existing CAS update instead of a create.

    Raises ``TaskWizardError`` for a definition this wizard cannot faithfully
    represent (an event trigger, an unknown action). It never silently
    converts one shape into another.
    """
    actions = getattr(task, "actions", None) or []
    first = actions[0] if actions and isinstance(actions[0], dict) else {}
    tool = str(first.get("name") or "")
    key = _ACTION_BY_TOOL.get(tool)
    if key is None:
        raise TaskWizardError("this task's action cannot be edited here")
    definition = ACTION_DEFINITIONS[key]
    arguments = first.get("arguments") if isinstance(first.get("arguments"), dict) else {}
    instruction = getattr(task, "ai_instruction", None)
    if isinstance(instruction, str) and instruction.strip():
        if not definition.supports_ai:
            raise TaskWizardError("this task's action requires static text")
        policy = derive_policy(instruction)
        # A definition this editor cannot FAITHFULLY reproduce is refused
        # instead of silently rewritten: recomposing the instruction would drop
        # a constraint the editor has no field for, changing a value the owner
        # never touched.
        if policy.quote_exact:
            raise TaskWizardError(
                "this task asks for an exact canonical quote, a contract that fails "
                "closed and cannot be represented by this editor"
            )
        if policy.exact_length is not None:
            raise TaskWizardError(
                "this task requires an EXACT character length; this editor can only "
                "express a maximum, so editing it would change the requirement"
            )
        mode = AI_MODE
        language = next(
            (code for code, word in LANGUAGE_POLICY.items() if word == policy.language), ""
        )
        source = policy.source or ""
        max_length = policy.max_length
        show_source = bool(policy.source) and policy.show_source
    else:
        if not definition.supports_ai and not str(arguments.get("text") or "").strip():
            raise TaskWizardError("this task has no editable text")
        mode, language, source, max_length, show_source = STATIC_MODE, "", "", None, False
    schedule_type = str(getattr(task, "schedule_type", "") or "")
    if schedule_type not in SCHEDULE_TYPES:
        raise TaskWizardError("this task's schedule cannot be edited here")
    schedule = getattr(task, "schedule", None) or {}
    interval_minutes: int | None = None
    clock = ""
    weekday: int | None = None
    once_at = ""
    if schedule_type == "interval":
        seconds = schedule.get("seconds")
        if isinstance(seconds, (int, float)) and int(seconds) > 0 and int(seconds) % 60 == 0:
            interval_minutes = int(seconds) // 60
    elif schedule_type in ("daily", "weekly"):
        if "hour" in schedule:
            clock = f"{int(schedule['hour']):02d}:{int(schedule.get('minute', 0)):02d}"
        if schedule_type == "weekly" and isinstance(schedule.get("weekday"), int):
            weekday = int(schedule["weekday"])
    elif schedule_type == "once":
        once_at = str(schedule.get("at") or "")
    from backend.helper.font_style import DEFAULT_FONT_KEY, normalize_font_key

    return TaskDraft(
        action=key,
        step=step,
        content_mode=mode,
        label=str(getattr(task, "label", "") or ""),
        text=str(arguments.get("text") or ""),
        font=normalize_font_key(arguments.get("font")) if arguments.get("font") else DEFAULT_FONT_KEY,
        source=source,
        language=language,
        max_length=max_length,
        show_source=show_source,
        schedule_type=schedule_type,
        interval_minutes=interval_minutes,
        clock=clock,
        weekday=weekday,
        once_at=once_at,
        timezone=str(getattr(task, "timezone", "") or "UTC"),
        editing_task_id=int(getattr(task, "id", 0) or 0),
        editing_version=int(getattr(task, "version", 1) or 1),
    )


# ── review rendering (derived from the candidate, never from the draft) ─────


def schedule_summary(schedule_type: str, schedule: dict[str, Any]) -> str:
    if schedule_type == "interval":
        seconds = int(schedule.get("seconds") or 0)
        if seconds and seconds % 60 == 0:
            minutes = seconds // 60
            return f"Every {minutes} minute" + ("s" if minutes != 1 else "")
        return f"Every {seconds} seconds"
    if schedule_type == "daily":
        return f"Daily at {int(schedule.get('hour', 0)):02d}:{int(schedule.get('minute', 0)):02d}"
    if schedule_type == "weekly":
        weekday = int(schedule.get("weekday", 0))
        clock = f"{int(schedule.get('hour', 0)):02d}:{int(schedule.get('minute', 0)):02d}"
        return f"Weekly on {WEEKDAYS[weekday]} at {clock}"
    if schedule_type == "once":
        return "Once at " + str(schedule.get("at", "")).replace("T", " ")[:16]
    return "Not set"


def first_run_summary(schedule_type: str, schedule: dict[str, Any]) -> str:
    """Truthful statement of the schedule contract's first-run semantics."""
    if schedule_type == "interval":
        return "one interval from now"
    if schedule_type == "once":
        return f"{str(schedule.get('at', '')).replace('T', ' ')[:16]} ({schedule.get('timezone', '')})"
    if schedule_type == "daily":
        return f"next {int(schedule.get('hour', 0)):02d}:{int(schedule.get('minute', 0)):02d} ({schedule.get('timezone', '')})"
    if schedule_type == "weekly":
        weekday = int(schedule.get("weekday", 0))
        clock = f"{int(schedule.get('hour', 0)):02d}:{int(schedule.get('minute', 0)):02d}"
        return f"next {WEEKDAYS[weekday]} {clock} ({schedule.get('timezone', '')})"
    return "—"


def review_lines(
    draft: TaskDraft, chat_id: int = 0, reference: datetime | None = None,
) -> list[tuple[str, str]]:
    """The review screen: every row derived from the ACTUAL candidate."""
    candidate = build_candidate(draft, chat_id, reference)
    definition = ACTION_DEFINITIONS[draft.action]
    lines: list[tuple[str, str]] = [("Action", definition.title)]
    instruction = candidate.get("ai_instruction")
    if isinstance(instruction, str):
        policy = derive_policy(instruction)
        lines.append(("Content", f"Random generated {definition.ai_noun} (fresh each run)"))
        lines.append(("Source", policy.source or "Any"))
        lines.append(("Language", policy.language.title() if policy.language else "Any"))
        lines.append((
            "Maximum length",
            f"at most {policy.max_length} characters" if policy.max_length else "Any",
        ))
        if policy.source:
            lines.append(("Show source", "Yes" if policy.show_source else "No"))
    else:
        lines.append(("Content", f'Static text: "{candidate["actions"][0]["arguments"]["text"]}"'))
        font = candidate["actions"][0]["arguments"].get("font")
        if font:
            lines.append(("Font", f"{font_label(font)} · {font_preview(font)}"))
    lines.append(("Schedule", schedule_summary(candidate["schedule_type"], candidate["schedule"])))
    lines.append(("Timezone", candidate["timezone"]))
    lines.append(("First run", first_run_summary(candidate["schedule_type"], candidate["schedule"])))
    return lines
