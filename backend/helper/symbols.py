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
STATE_WARNING = "◬"      # U+25EC WHITE UP-POINTING TRIANGLE WITH DOT

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
OP_SEARCH = "⌕"           # U+2315 TELEPHONE RECORDER (magnifier-like)
OP_MANAGE = "☰"           # U+2630 TRIGRAM FOR HEAVEN (settings/list-like)
OP_CHAT = "▭"             # U+25AD WHITE RECTANGLE
OP_STOP = "■"             # U+25A0 BLACK SQUARE (stop / disable)

# ── panel structure + wizard navigation (Category E) ────────────────────────
PANEL_HEADING = "▣"       # U+25A3 WHITE SQUARE CONTAINING BLACK SMALL SQUARE
SEPARATOR = "·"           # U+00B7 MIDDLE DOT
STEP_CURRENT = "◆"        # U+25C6 BLACK DIAMOND
STEP_INCOMPLETE = "◇"     # U+25C7 — same glyph as CONTENT_AI (open-by-design)
STEP_COMPLETED = "✓"      # U+2713 — same glyph as STATE_COMPLETED
PAGE_CURRENT = "▰"        # U+25B0 BLACK PARALLELOGRAM
PAGE_EMPTY = "▱"          # U+25B1 WHITE PARALLELOGRAM (empty progress segment)
BULLET = "•"             # U+2022 BULLET (plain list / non-active marker)
SELECT_EMPTY = "⊡"       # U+22A1 SQUARED DOT OPERATOR (unmarked selection item)
SELECT_MARKED = "⊠"      # U+22A0 SQUARED TIMES (selected item)

# ── media types (Category F) ────────────────────────────────────────────────
MEDIA_PHOTO = "▢"         # U+25A2 WHITE SQUARE WITH ROUNDED CORNERS
MEDIA_VIDEO = "▷"         # U+25B7 WHITE RIGHT-POINTING TRIANGLE
MEDIA_ANIMATION = "◧"     # U+25E7 SQUARE WITH LEFT HALF BLACK
MEDIA_AUDIO = "♪"         # U+266A EIGHTH NOTE
MEDIA_VOICE = "◉"         # U+25C9 FISHEYE
MEDIA_STICKER = "◈"       # U+25C8 WHITE DIAMOND CONTAINING BLACK SMALL DIAMOND
MEDIA_DOCUMENT = "▤"      # U+25A4 SQUARE WITH HORIZONTAL FILL
MEDIA_TEXT = "▥"          # U+25A5 SQUARE WITH VERTICAL FILL
MEDIA_ALBUM = "◰"         # U+25F0 WHITE SQUARE WITH UPPER LEFT QUADRANT
MEDIA_UNKNOWN = "◌"       # U+25CC DOTTED CIRCLE

# ── domain icons (Category H) ───────────────────────────────────────────────
# One glyph per product domain, so a panel and its notices share a single mark.
ICON_PROFILE = "◎"        # U+25CE BULLSEYE
ICON_MOOD = "◡"           # U+25E1 LOWER HALF CIRCLE
ICON_PROVIDER = "⬡"       # U+2B21 WHITE HEXAGON
ICON_HEALTH = "✚"         # U+271A HEAVY GREEK CROSS
ICON_STATS = "▧"          # U+25A7 SQUARE WITH UPPER LEFT TO LOWER RIGHT FILL
ICON_SPEAK = "♬"          # U+266C BEAMED SIXTEENTH NOTES
ICON_SEND = "➤"           # U+27A4 BLACK RIGHTWARDS ARROWHEAD
ICON_ASK = "⁇"            # U+2047 DOUBLE QUESTION MARK
ICON_MEMORY = "⎔"         # U+2394 SOFTWARE-FUNCTION SYMBOL
ICON_TRANSLATE = "⇄"      # U+21C4 RIGHTWARDS ARROW OVER LEFTWARDS ARROW
ICON_SUMMARIZE = "⁂"      # U+2042 ASTERISM
ICON_WEB = "⊛"            # U+229B CIRCLED ASTERISK OPERATOR
ICON_ORGANIZE = "⊞"       # U+229E SQUARED PLUS
ICON_VARIABLE = "⎇"       # U+2387 ALTERNATIVE KEY SYMBOL
ICON_LIBRARY = "▦"        # U+25A6 — documented reuse of SCHEDULE_WEEKLY glyph
ICON_CATEGORY = "▨"       # U+25A8 SQUARE WITH UPPER RIGHT TO LOWER LEFT FILL
ICON_MAPPING = "⊹"        # U+22B9 HERMITIAN CONJUGATE MATRIX
ICON_REACTION = "⇋"       # U+21CB LEFTWARDS HARPOON OVER RIGHTWARDS HARPOON
ICON_PREMIUM = "✪"        # U+272A CIRCLED WHITE STAR
ICON_DIAGNOSTIC = "⊙"     # U+2299 CIRCLED DOT OPERATOR
ICON_SELECT = "◒"         # U+25D2 CIRCLE WITH LOWER HALF BLACK
ICON_PIN = "⦿"            # U+29BF CIRCLED BULLET
ICON_MODEL = "◇"        # U+25C7 WHITE DIAMOND (AI model — same glyph as CONTENT_AI)
ICON_CREDENTIAL = "⚿"   # U+26BF SQUARED KEY

# ── AI provider icons (Category I) ──────────────────────────────────────────
# One abstract mark per provider (never a coloured emoji), so the provider
# list and the model-test grid stay monochrome and consistent.
ICON_P_OPENROUTER = "◐"   # U+25D0 CIRCLE WITH LEFT HALF BLACK
ICON_P_GROQ = "↯"         # U+21AF DOWNWARDS ZIGZAG ARROW (fast inference)
ICON_P_GEMINI = "✦"       # U+2726 BLACK FOUR POINTED STAR
ICON_P_OPENAI = "⊚"       # U+229A CIRCLED RING OPERATOR
ICON_P_MISTRAL = "≋"      # U+224B TRIPLE TILDE
ICON_P_CEREBRAS = "✳"     # U+2733 EIGHT SPOKED ASTERISK
ICON_P_ZAI = "❊"          # U+274A EIGHT TEARDROP-SPOKED PROPELLER ASTERISK
ICON_P_SAMBANOVA = "⬢"    # U+2B22 BLACK HEXAGON
ICON_P_NVIDIA = "⬣"       # U+2B23 HORIZONTAL BLACK HEXAGON
ICON_P_COHERE = "✧"       # U+2727 WHITE FOUR POINTED STAR
ICON_P_SILICONFLOW = "≈"  # U+2248 ALMOST EQUAL TO
ICON_P_FIREWORKS = "✺"    # U+273A SIXTEEN POINTED ASTERISK
ICON_P_NARAROUTER = "⊕"   # U+2295 CIRCLED PLUS
ICON_P_CLOUDFLARE = "⌬"   # U+232C BENZENE RING (edge network)
ICON_P_YOU = "⌘"          # U+2318 PLACE OF INTEREST SIGN (search capability)

# ── saved-item caption metadata (Category G) ────────────────────────────────
META_SENDER = "◍"         # U+25CD CIRCLE WITH VERTICAL FILL
META_TIME = "◔"           # U+25D4 CIRCLE WITH UPPER RIGHT QUADRANT BLACK
META_ID = "⌗"             # U+2317 VIEWDATA SQUARE
META_MEDIA = "◫"          # U+25EB WHITE SQUARE WITH VERTICAL BISECTING LINE
META_TAGS = "▩"           # U+25A9 SQUARE WITH DIAGONAL CROSSHATCH FILL
FILE_NAME = MEDIA_DOCUMENT  # a file name is a document; same glyph (documented)

# ── composite templates (glyph + placeholder) ───────────────────────────────
# The default profile templates, composed from the registry so a glyph change
# is made in exactly one place. The ``{time}``/``{mood}`` tokens are literal
# placeholders the profile engine substitutes at render time.
BIO_DEFAULT_TEMPLATE = f"{META_TIME} {{time}} | {ICON_MOOD} {{mood}}"
USERNAME_DEFAULT_TEMPLATE = "{time} | {mood}"


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
    "warning": SymbolSpec("state_warning", STATE_WARNING, "warning / attention"),
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
    "search": SymbolSpec("op_search", OP_SEARCH, "search / filter"),
    "manage": SymbolSpec("op_manage", OP_MANAGE, "manage / settings list"),
    "chat": SymbolSpec("op_chat", OP_CHAT, "chat / conversation"),
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
    "page_empty": SymbolSpec("page_empty", PAGE_EMPTY, "empty pagination / progress segment"),
    "bullet": SymbolSpec("bullet", BULLET, "plain list / non-active marker"),
    "select_empty": SymbolSpec("select_empty", SELECT_EMPTY, "unselected multi-select item"),
    "select_marked": SymbolSpec("select_marked", SELECT_MARKED, "selected multi-select item"),
}

#: Category F — one glyph per media type (Saved-item captions + DB statistics).
MEDIA_SYMBOLS: dict[str, SymbolSpec] = {
    "photo": SymbolSpec("media_photo", MEDIA_PHOTO, "photo media"),
    "video": SymbolSpec("media_video", MEDIA_VIDEO, "video media"),
    "animation": SymbolSpec("media_animation", MEDIA_ANIMATION, "animation media"),
    "audio": SymbolSpec("media_audio", MEDIA_AUDIO, "audio media"),
    "voice": SymbolSpec("media_voice", MEDIA_VOICE, "voice message"),
    "sticker": SymbolSpec("media_sticker", MEDIA_STICKER, "sticker media"),
    "document": SymbolSpec("media_document", MEDIA_DOCUMENT, "document media"),
    "text": SymbolSpec("media_text", MEDIA_TEXT, "plain text"),
    "album": SymbolSpec("media_album", MEDIA_ALBUM, "album (grouped media)"),
    "unknown": SymbolSpec("media_unknown", MEDIA_UNKNOWN, "unknown media"),
}

#: Category G — saved-item caption metadata line prefixes.
META_SYMBOLS: dict[str, SymbolSpec] = {
    "sender": SymbolSpec("meta_sender", META_SENDER, "caption sender line"),
    "time": SymbolSpec("meta_time", META_TIME, "caption timestamp line"),
    "id": SymbolSpec("meta_id", META_ID, "caption chat/message id line"),
    "media": SymbolSpec("meta_media", META_MEDIA, "caption media-type line"),
    "tags": SymbolSpec("meta_tags", META_TAGS, "caption additional-tags line"),
    "file_name": SymbolSpec("file_name", FILE_NAME, "caption file-name line",
                            "same glyph as MEDIA_DOCUMENT"),
}

#: Category H — product-domain icons (Bio, AI, Discover, …). One glyph per
#: domain so a panel heading and its notices stay visually consistent.
DOMAIN_SYMBOLS: dict[str, SymbolSpec] = {
    "profile": SymbolSpec("icon_profile", ICON_PROFILE, "bio / username profile"),
    "mood": SymbolSpec("icon_mood", ICON_MOOD, "mood value"),
    "provider": SymbolSpec("icon_provider", ICON_PROVIDER, "AI provider"),
    "health": SymbolSpec("icon_health", ICON_HEALTH, "health / diagnostics"),
    "stats": SymbolSpec("icon_stats", ICON_STATS, "statistics / breakdown"),
    "speak": SymbolSpec("icon_speak", ICON_SPEAK, "speech output"),
    "send": SymbolSpec("icon_send", ICON_SEND, "message sent"),
    "ask": SymbolSpec("icon_ask", ICON_ASK, "question asked"),
    "memory": SymbolSpec("icon_memory", ICON_MEMORY, "AI memory"),
    "translate": SymbolSpec("icon_translate", ICON_TRANSLATE, "translation"),
    "summarize": SymbolSpec("icon_summarize", ICON_SUMMARIZE, "summarization"),
    "web": SymbolSpec("icon_web", ICON_WEB, "web search"),
    "organize": SymbolSpec("icon_organize", ICON_ORGANIZE, "organizer"),
    "variable": SymbolSpec("icon_variable", ICON_VARIABLE, "template variable"),
    "library": SymbolSpec("icon_library", ICON_LIBRARY, "emoji library",
                          "same glyph as SCHEDULE_WEEKLY"),
    "category": SymbolSpec("icon_category", ICON_CATEGORY, "emoji category"),
    "mapping": SymbolSpec("icon_mapping", ICON_MAPPING, "emoji mapping"),
    "reaction": SymbolSpec("icon_reaction", ICON_REACTION, "reaction / swap"),
    "premium": SymbolSpec("icon_premium", ICON_PREMIUM, "premium artifact"),
    "diagnostic": SymbolSpec("icon_diagnostic", ICON_DIAGNOSTIC, "diagnostic run"),
    "select": SymbolSpec("icon_select", ICON_SELECT, "selection / option"),
    "pin": SymbolSpec("icon_pin", ICON_PIN, "target chat / pin"),
    "model": SymbolSpec("icon_model", ICON_MODEL, "AI model", "same glyph as CONTENT_AI"),
    "credential": SymbolSpec("icon_credential", ICON_CREDENTIAL, "API credential"),
}

#: The single merged view used for auditability and the collision test.
ALL_SYMBOLS: dict[str, SymbolSpec] = {
    **TASK_STATE_SYMBOLS,
    **SCHEDULE_SYMBOLS,
    **CONTENT_SYMBOLS,
    **OPERATION_SYMBOLS,
    **STRUCTURE_SYMBOLS,
    **MEDIA_SYMBOLS,
    **META_SYMBOLS,
    **DOMAIN_SYMBOLS,
}

#: Deliberate cross-category glyph reuses. Each entry maps glyph → set of keys
#: that are ALLOWED to share it; any other duplicate is a defect.
DOCUMENTED_REUSE: dict[str, frozenset[str]] = {
    STATE_COMPLETED: frozenset({"completed", "save", "step_completed"}),
    STATE_FAILED: frozenset({"failed", "close"}),
    STATE_PAUSED: frozenset({"paused", "pause"}),
    CONTENT_AI: frozenset({"ai", "step_incomplete", "model"}),
    OP_BACK: frozenset({"back", "previous"}),
    MEDIA_DOCUMENT: frozenset({"document", "file_name"}),
    SCHEDULE_WEEKLY: frozenset({"weekly", "library"}),
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


def media_glyph(media_type: str, default: str = MEDIA_UNKNOWN) -> str:
    """Glyph for a media type name (``Photo``, ``Document``, …), fail-safe.

    Media type names are the human labels the save/database layers already use
    (``Photo``/``Video``/…); matching is case-insensitive.
    """
    spec = MEDIA_SYMBOLS.get(str(media_type or "").strip().lower())
    return spec.glyph if spec else default


def label(glyph: str, text: str) -> str:
    """``"<glyph> <text>"`` — the canonical button-label composition.

    Keeps symbol selection separate from the label text and guarantees a
    single, consistent separator so labels never drift between handlers.
    """
    return f"{glyph} {text}".strip()
