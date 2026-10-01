"""Per-request proactive-initiative authorization — ONE shared detector.

The OWNER'S OWN message is the only authorization source. A small, explicit
phrase vocabulary (Persian + English) decides whether THIS request authorizes
useful additional work toward the stated goal. Everything about the design is
fail-closed:

  - token-based matching (the same discipline as the token clock anchor — no
    regex ever matches a command/intent pattern, INVESTIGATION.md §24);
  - unknown, ambiguous, or ordinary phrasing is NOT authorization;
  - authorization is never persisted, never a preference, never global — it
    lives exactly as long as the request that carried it.

There is no persisted proactive/initiative preference in this repository
(``PreferencesRecord`` holds language/personality/response_style/custom
instructions/auto_memory/auto_tools only), so this detector is the single
permission mechanism; it must not be duplicated.

Two consumers share it:

  - the conversational Dispatcher appends ``PROACTIVE_AUTHORIZED_RULES`` as a
    per-request system message and flags ``ToolContext.extra``;
  - the durable create_task path (``TaskInterpreter``) appends its bounded
    expansion contract to the planning instructions.

No scheduler, executor, dispatcher, tool registry, or schema is created or
changed by this module.
"""
from __future__ import annotations

import re
from typing import Any

from backend.ai.persian import normalize_digits

_TOKEN_RE = re.compile(r"[a-z0-9\u0621-\u06ff]+")

#: Explicit initiative vocabulary. Each phrase is a contiguous token sequence
#: after normalization and independently carries "decide / do what is needed /
#: handle related additional work" semantics. Deliberately conservative: bare
#: delegation ("خودت انجام بده", "do it yourself"), opinion prompts
#: ("به نظر خودت چیه؟"), and permission-adjacent social phrasing
#: ("feel free to ask", "if anything else, let me know") are NOT included.
_AUTHORIZATION_PHRASES: tuple[str, ...] = (
    # Persian — additional-work / autonomous-decision authorization.
    "چیزهای مرتبط دیگه‌ای",
    "چیزای مرتبط دیگه",
    "کارهای مرتبط دیگه",
    "کارای مرتبط دیگه",
    "مرتبط دیگه‌ای",
    "مرتبط دیگه",
    "هر کار کوچیکی",
    "هر کار کوچک",
    "هر کار لازمه",
    "هر کاری لازمه",
    "هر کاری که لازمه",
    "لازم نیست بپرسم",
    "لازم نیست بپرسی",
    "لزومی نیست بپرسم",
    "نیازی نیست بپرسم",
    "نیازی نیست بپرسی",
    "خودت تصمیم بگیر",
    "خودت اقدام کن",
    "خودت اقدام بکن",
    "اختیار داری",
    "به عهده تو",
    "به عهده خودت",
    "به سلیقه خودت",
    "خودت بهتر میدونی",
    "خودت میدونی چی",
    # English — initiative / autonomous-decision authorization.
    "as you see fit",
    "as you think best",
    "use your judgment",
    "use your better judgment",
    "at your discretion",
    "on your own initiative",
    "your own initiative",
    "show initiative",
    "take the initiative",
    "be proactive",
    "be more proactive",
    "whatever else is needed",
    "whatever else is necessary",
    "whatever else you need",
    "anything else is needed",
    "anything else needed",
    "anything else necessary",
    "anything else related",
    "anything else you think",
    "do what is needed",
    "do what's needed",
    "do whatever is needed",
    "handle the rest",
    "do the rest",
    "you decide what",
)

def _tokenize(text: str) -> list[str]:
    """Lowercase, normalize digits, and split Persian/English into word tokens.

    Differs from ``actions._tokenize`` in exactly one place: ZWNJ is REMOVED
    rather than replaced by a space, so the very common colloquial spelling
    "دیگه‌ای" and the split spelling "دیگه ای" cannot disagree about whether a
    phrase is present. Everything else (digit normalization, apostrophe
    removal, the Unicode letter class) matches the shared tokenizer.
    """
    s = normalize_digits(text)
    s = s.replace("\u200c", "").replace("\u200b", "").replace("\ufeff", "")
    s = s.replace("'", "").replace("\u2019", "").lower()
    return _TOKEN_RE.findall(s)


#: The same phrases reduced to normalized token tuples once, at import time.
_PHRASE_TOKENS: tuple[tuple[str, ...], ...] = tuple(
    tuple(tokens)
    for tokens in (_tokenize(phrase) for phrase in _AUTHORIZATION_PHRASES)
    if tokens
)


def has_proactive_authorization(text: Any) -> bool:
    """True only when *text* carries EXPLICIT proactive-initiative consent.

    Fail-closed by construction: non-string, empty, unrecognized, or merely
    ordinary phrasing returns False. Detection is per-request — no state is
    stored and no later request inherits this result.
    """
    if not isinstance(text, str) or not text.strip():
        return False
    tokens = _tokenize(text)
    count = len(tokens)
    if not count:
        return False
    for phrase in _PHRASE_TOKENS:
        size = len(phrase)
        if size > count:
            continue
        first = phrase[0]
        for i in range(count - size + 1):
            if tokens[i] == first and tuple(tokens[i:i + size]) == phrase:
                return True
    return False


#: The per-request system message the conversational Dispatcher inserts — and
#: ONLY inserts — when ``has_proactive_authorization`` proves consent. The
#: bound (5) is the already-enforced ``MAX_TOOLS_PER_TURN`` / ``MAX_ACTIONS``
#: value; nothing here raises any existing limit.
PROACTIVE_AUTHORIZED_RULES = (
    "Proactive authorization: AUTHORIZED — the owner's current message explicitly "
    "authorizes additional useful work. Plan ONE coherent bounded sequence for this "
    "single request: at most 5 tool calls in total, executed in order through the "
    "registered tools. Derive only actions that directly serve the owner's stated "
    "goal — inspect, identify what is missing, make the needed change, verify or "
    "update state, then report — using deterministic targets and only facts returned "
    "by real tool results. Never invent unrelated work merely to demonstrate "
    "initiative; never call create_task repeatedly (timed or recurring work stays "
    "ONE task whose ordered actions are defined at creation); never introduce a new "
    "side-effect category (no raw Telegram RPC, SQL, filesystem, HTTP, or shell); "
    "never touch unrelated saved items, messages, or account state; and never bypass "
    "a confirmation gate — an action that requires owner confirmation still asks. If "
    "an earlier action failed, report the failure instead of pretending it succeeded. "
    "If the request is genuinely ambiguous, ask ONE clarifying question instead of "
    "guessing."
)
