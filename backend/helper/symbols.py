"""Central functional Unicode symbol registry for the Glass UI.

ONE authoritative, deterministic registry of the compact Unicode glyphs the
panels and the Taskloom task system render. Every handler that needs a status,
schedule, operation, panel-structure or wizard glyph reads it from here instead
of hard-coding a literal, so the same semantic always has the same symbol and
a symbol never silently means two different things.

Design rules
------------
* **Functional, not decorative.** Glyphs are simple abstract Unicode marks
  (geometric shapes, arrows, math signs), never emoji. This keeps the UI
  compact, monochrome and consistent, and avoids variation-selector bytes
  (``U+FE0F``) that break the display-font pipeline and Telegram rendering.
* **Category uniqueness.** Within a semantic category no two members share a
  glyph. Cross-category collisions are forbidden too, with ONE deliberate,
  documented exception: the common-operation group re-uses ``SAVE`` = ``✓``
  (which equals ``STATE_COMPLETED``) because saving/confirming *is* the
  completion of an action — the same meaning everywhere it appears.
* **Deterministic + dependency-free.** Plain module-level data and pure lookup
  helpers; no runtime configuration, no external resources.

The display-font pipeline (``backend.helper.font_style``) transforms only Latin
letters and standalone digit runs, so every glyph below passes through
untouched — see ``tests/test_symbols.py`` for the enforced guarantee.
"""
from __future__ import annotations

from dataclasses import dataclass

# ── task states (Category A) ────────────────────────────────────────────────
STATE_ACTIVE = "●"       # U+25CF BLACK CIRCLE
STATE_PAUSED = "∥"       # U+2225 PARALLEL TO
STATE_COMPLETED = "✓"    # U+2713 CHECK MARK
STATE_FAILED = "×"       # U+00D7 MULTIPLICATION SIGN
STATE_EXPIRED = "⌛"     # U+231B HOURGLASS
STATE_DELETED = "⊘"      # U+2298 CIRCLED DIVISION SLASH

# ── schedule types (Category B) ─────────────────────────────────────────────
SCHEDULE_ONCE = "○"       # U+25CB WHITE CIRCLE
SCHEDULE_INTERVAL = "⟳"   # U+27F3 CLOCKWISE GAPPED CIRCLE ARROW
SCHEDULE_DAILY = "◷"      # U+25F7 WHITE CIRCLE WITH UPPER RIGHT QUADRANT
SCHEDULE_WEEKLY = "▦"     # U+25A6 SQUARE WITH ORTHOGONAL CROSSHATCH FILL
SCHEDULE_EVENT = "⌁"      # U+2301 ELECTRIC ARROW

# ── destination + content (Category C) ──────────────────────────────────────
DESTINATION = "⌖"         # U+2316 POSITION INDICATOR
CONTENT_STATIC = "≡"      # U+2261 IDENTICAL TO
CONTENT_AI = "◇"          # U+25C7 WHITE DIAMOND

# ── common operations (Category D) ──────────────────────────────────────────
OP_CREATE = "＋"          # U+FF0B FULLWIDTH PLUS SIGN
OP_SAVE = "✓"             # U+2713 — deliberate reuse of STATE_COMPLETED
OP_EDIT = "✎"             # U+270E LOWER RIGHT PENCIL
OP_DELETE = "⌫"           # U+232B ERASE TO THE LEFT
OP_REFRESH = "↻"          # U+21BB CLOCKWISE OPEN CIRCLE ARROW
OP_PAUSE = "∥"            # U+2225 — same glyph as STATE_PAUSED (open-by-design)
OP_RESUME = "▶"           # U+25B6 BLACK RIGHT-POINTING TRIANGLE
OP_BACK = "❮"             # U+276E HEAVY LEFT-POINTING ANGLE QUOTATION MARK
OP_NEXT = "❯"             # U+276F HEAVY RIGHT-POINTING ANGLE QUOTATION MARK
OP_HOME = "⌂"             # U+2302 HOUSE
OP_CLOSE = "×"            # U+00D7 — same glyph as STATE_FAILED (open-by-design)
OP_PREV = "❮"             # U+276E — same glyph as OP_BACK (open-by-design)

# ── panel structure + wizard navigation (Category E) ────────────────────────
PANEL_HEADING = "▣"       # U+25A3 WHITE SQUARE CONTAINING BLACK SMALL SQUARE
SEPARATOR = "·"           # U+00B7 MIDDLE DOT
STEP_CURRENT = "◆"        # U+25C6 BLACK DIAMOND
STEP_INCOMPLETE = "◇"     # U+25C7 — same glyph as CONTENT_AI (open-by-design)
STEP_COMPLETED = "✓"      # U+2713 — same glyph as STATE_COMPLETED
PAGE_CURRENT = "▰"        # U+25B0 BLACK PARALLELOGRAM


@dataclass(frozen=True)
class SymbolSpec:
    """One registry entry: semantic key → glyph, meaning and reuse scope."""

    key: str
    glyph: str
    meaning: str
    reuse_scope: str = ""


#: Category A — one glyph per task state, unique within the category.
TASK_STATE_SYMBOLS: dict[str, SymbolSpec] = {
    "active": SymbolSpec("state_active", STATE_ACTIVE, "task is active"),
    "paused": SymbolSpec("state_paused", STATE_PAUSED, "task is paused"),
    "completed": SymbolSpec("state_completed", STATE_COMPLETED, "task completed"),
    "failed": SymbolSpec("state_failed", STATE_FAILED, "task failed"),
    "expired": SymbolSpec("state_expired", STATE_EXPIRED, "task expired"),
    "deleted": SymbolSpec("state_deleted", STATE_DELETED, "task deleted"),
}

#: Category B — one glyph per schedule type.
SCHEDULE_SYMBOLS: dict[str, SymbolSpec] = {
    "once": SymbolSpec("schedule_once", SCHEDULE_ONCE, "one-time schedule"),
    "interval": SymbolSpec("schedule_interval", SCHEDULE_INTERVAL, "fixed-interval schedule"),
    "daily": SymbolSpec("schedule_daily", SCHEDULE_DAILY, "daily schedule"),
    "weekly": SymbolSpec("schedule_weekly", SCHEDULE_WEEKLY, "weekly schedule"),
    "event": SymbolSpec("schedule_event", SCHEDULE_EVENT, "event-triggered schedule"),
}

#: Category C — destination and the two content modes.
CONTENT_SYMBOLS: dict[str, SymbolSpec] = {
    "destination": SymbolSpec("destination", DESTINATION, "notification destination"),
    "static": SymbolSpec("content_static", CONTENT_STATIC, "static content (same every run)"),
    "ai": SymbolSpec("content_ai", CONTENT_AI, "AI-generated content (fresh each run)"),
}

#: Category D — common operations. A symbol here may be reused across panels
#: because the same operation keeps the same meaning everywhere.
OPERATION_SYMBOLS: dict[str, SymbolSpec] = {
    "create": SymbolSpec("op_create", OP_CREATE, "create a new item"),
    "save": SymbolSpec("op_save", OP_SAVE, "save / confirm", "reused by STATE_COMPLETED"),
    "edit": SymbolSpec("op_edit", OP_EDIT, "edit an item"),
    "delete": SymbolSpec("op_delete", OP_DELETE, "delete an item"),
    "refresh": SymbolSpec("op_refresh", OP_REFRESH, "refresh the view"),
    "pause": SymbolSpec("op_pause", OP_PAUSE, "pause a task", "same glyph as STATE_PAUSED"),
    "resume": SymbolSpec("op_resume", OP_RESUME, "resume / continue a task"),
    "back": SymbolSpec("op_back", OP_BACK, "go back"),
    "next": SymbolSpec("op_next", OP_NEXT, "next page / step"),
    "home": SymbolSpec("op_home", OP_HOME, "go home"),
    "close": SymbolSpec("op_close", OP_CLOSE, "close / cancel", "same glyph as STATE_FAILED"),
    "previous": SymbolSpec("op_previous", OP_PREV, "previous page / step", "same glyph as OP_BACK"),
}

#: Category E — panel structure and wizard/navigation glyphs.
STRUCTURE_SYMBOLS: dict[str, SymbolSpec] = {
    "heading": SymbolSpec("panel_heading", PANEL_HEADING, "panel heading"),
    "separator": SymbolSpec("separator", SEPARATOR, "inline text separator"),
    "step_current": SymbolSpec("step_current", STEP_CURRENT, "current wizard step"),
    "step_incomplete": SymbolSpec("step_incomplete", STEP_INCOMPLETE, "incomplete wizard step",
                                  "same glyph as CONTENT_AI"),
    "step_completed": SymbolSpec("step_completed", STEP_COMPLETED, "completed wizard step",
                                 "same glyph as STATE_COMPLETED"),
    "page_current": SymbolSpec("page_current", PAGE_CURRENT, "current page / pagination"),
}

#: The single merged view used for auditability and the collision test.
ALL_SYMBOLS: dict[str, SymbolSpec] = {
    **TASK_STATE_SYMBOLS,
    **SCHEDULE_SYMBOLS,
    **CONTENT_SYMBOLS,
    **OPERATION_SYMBOLS,
    **STRUCTURE_SYMBOLS,
}

#: Deliberate cross-category glyph reuses. Each entry maps glyph → set of keys
#: that are ALLOWED to share it; any other duplicate is a defect.
DOCUMENTED_REUSE: dict[str, frozenset[str]] = {
    STATE_COMPLETED: frozenset({"completed", "save", "step_completed"}),
    STATE_FAILED: frozenset({"failed", "close"}),
    STATE_PAUSED: frozenset({"paused", "pause"}),
    CONTENT_AI: frozenset({"ai", "step_incomplete"}),
    OP_BACK: frozenset({"back", "previous"}),
}


def status_glyph(status: str, default: str = STATE_FAILED) -> str:
    """Glyph for a task/occurrence status, failing safe to ``default``."""
    spec = TASK_STATE_SYMBOLS.get(str(status or ""))
    return spec.glyph if spec else default


def schedule_glyph(schedule_type: str, default: str = SEPARATOR) -> str:
    """Glyph for a schedule type, failing safe to ``default``."""
    spec = SCHEDULE_SYMBOLS.get(str(schedule_type or ""))
    return spec.glyph if spec else default


def content_glyph(mode: str, default: str = CONTENT_STATIC) -> str:
    """Glyph for a content mode (``ai`` / ``static``)."""
    spec = CONTENT_SYMBOLS.get(str(mode or ""))
    return spec.glyph if spec else default


def label(glyph: str, text: str) -> str:
    """``"<glyph> <text>"`` — the canonical button-label composition.

    Keeps symbol selection separate from the label text and guarantees a
    single, consistent separator so labels never drift between handlers.
    """
    return f"{glyph} {text}".strip()
