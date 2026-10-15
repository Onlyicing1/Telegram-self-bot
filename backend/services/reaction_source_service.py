"""
Reaction source service — Emoji Stage 2 (ROADMAP §27).

The single source of selectable reactions for the Auto-Reaction setting. The
catalog comes from Telegram itself (``messages.GetAvailableReactionsRequest``
via ``backend.telegram_api.reactions.get_available_reactions``) — never a
hardcoded list and never a Unicode catalog substitute.

Contracts:

* **Telegram is the only source.** Every option is an emoji string Telegram
  returned in its available-reaction catalog. Nothing is invented, and the
  option carries the exact value the existing reaction execution path
  (``backend.telegram_api.reactions.send_reaction``) expects
  (``{"kind": "emoji", "emoji": <value>}``), so a selected option is always
  usable by that path.
* **Safe options only.** A record with no usable emoji value, or one the
  send path would refuse (``normalize_emoji`` — non-canonical/over-long), is
  dropped; a record Telegram marks ``inactive`` is not offered. ``premium``
  reactions ARE offered (labeled) — the API exposes them and the send path
  can apply them; only unusable/absent data is withheld.
* **Bounded refresh + cache (no auto-update claim).** The catalog is cached
  in-process with a TTL. Opening the selector serves the cache when it is
  fresh and otherwise re-reads Telegram; the explicit Refresh action always
  re-reads. ``AvailableReactionsNotModified`` keeps the existing catalog
  (the ``hash`` proves it unchanged). The list is never claimed to update on
  its own — it changes only on open-when-stale or on an explicit Refresh.
* **Honest failure.** An API error, an empty catalog, or an unsupported
  response is reported as such — never a fabricated success.
* **No second infrastructure.** The client is passed in by the caller; there
  is no client/loop/scheduler/executor/AI of this module's own.
"""
from __future__ import annotations
from backend.helper import symbols as sym

import logging
import time
from typing import Any

from backend.telegram_api.exceptions import TelegramAPIError
from backend.telegram_api.reactions import (
    KIND_EMOJI,
    get_available_reactions,
    normalize_emoji,
)

logger = logging.getLogger(__name__)

#: How long a fetched catalog stays fresh. Opening the selector inside this
#: window serves the cache; past it, the selector re-reads Telegram. An
#: explicit Refresh always re-reads regardless of age.
CACHE_TTL_S = 300.0

#: Defensive bound on the number of options rendered/handled at once.
MAX_OPTIONS = 200

ERROR_NO_CLIENT = "E_NO_CLIENT"
ERROR_TELEGRAM = "E_TELEGRAM"
ERROR_EMPTY = "E_EMPTY"

_cache: dict[str, Any] = {"options": [], "hash": 0, "loaded_at": 0.0, "loaded": False}


def reset_cache() -> None:
    """Drop the cached catalog (used by tests and after a client change)."""
    _cache.update(options=[], hash=0, loaded_at=0.0, loaded=False)


def _is_fresh() -> bool:
    return bool(_cache.get("loaded")) and (
        time.monotonic() - float(_cache.get("loaded_at") or 0.0) < CACHE_TTL_S
    )


def cached_options() -> list[dict[str, Any]] | None:
    """The cached options WITHOUT any network call, or ``None`` when nothing
    has been loaded yet. Lets a panel render the current selection/label
    cache-first, never on a button click."""
    if not _cache.get("loaded"):
        return None
    return list(_cache.get("options") or [])


def _build_options(records: list[dict[str, Any]]) -> list[dict[str, Any]]:
    """Telegram records → selectable options, dropping anything unusable."""
    options: list[dict[str, Any]] = []
    for record in records[:MAX_OPTIONS]:
        if not isinstance(record, dict):
            continue
        if record.get("inactive"):
            continue
        emoji = normalize_emoji(record.get("reaction"))
        if emoji is None:
            continue
        options.append({
            "reaction": {"kind": KIND_EMOJI, "emoji": emoji},
            "emoji": emoji,
            "title": str(record.get("title") or ""),
            "premium": bool(record.get("premium")),
        })
    return options


def _failure(error: str, detail: str) -> dict[str, Any]:
    return {"ok": False, "error": error, "detail": detail, "options": [], "cached": False}


async def load_reactions(
    client: Any, *, force: bool = False,
) -> dict[str, Any]:
    """Return the selectable reaction options.

    ``force=True`` (the Refresh action) always re-reads Telegram; otherwise a
    fresh cache is served without a request. Returns
    ``{"ok", "error", "detail", "options", "cached"}`` — never raises for an
    operational failure and never fabricates a catalog.
    """
    if client is None:
        return _failure(ERROR_NO_CLIENT, "the self client is not connected")
    if not force and _is_fresh():
        return {
            "ok": True, "error": None, "detail": None,
            "options": list(_cache.get("options") or []), "cached": True,
        }
    try:
        response = await get_available_reactions(client, hash=int(_cache.get("hash") or 0))
    except TelegramAPIError as exc:
        logger.warning("[REACTION_SOURCE] catalog read failed: %s", exc)
        return _failure(ERROR_TELEGRAM, str(exc))

    if response.get("modified"):
        # Telegram proved the catalog unchanged — keep what we already have.
        options = list(_cache.get("options") or [])
        _cache["loaded_at"] = time.monotonic()
        _cache["loaded"] = True
        if not options:
            return _failure(ERROR_EMPTY, "Telegram reported no reaction changes and none are cached")
        return {"ok": True, "error": None, "detail": None, "options": options, "cached": False}

    options = _build_options(response.get("reactions") or [])
    _cache.update(
        options=options,
        hash=int(response.get("hash") or 0),
        loaded_at=time.monotonic(),
        loaded=True,
    )
    if not options:
        return _failure(ERROR_EMPTY, "Telegram returned no usable reactions for this account")
    return {"ok": True, "error": None, "detail": None, "options": options, "cached": False}


def option_label(option: dict[str, Any]) -> str:
    """Owner-facing label for one option: the emoji itself plus a premium
    marker. Telegram's ``title`` is a localized name, not a distinct glyph,
    so the emoji is the reliable visual."""
    emoji = str(option.get("emoji") or "?")
    return f"{emoji} {sym.ICON_MAPPING}" if option.get("premium") else emoji


def find_option(options: list[dict[str, Any]], emoji: str) -> dict[str, Any] | None:
    """The option whose reaction matches ``emoji`` exactly, or ``None``."""
    for option in options or []:
        reaction = option.get("reaction") or {}
        if reaction.get("emoji") == emoji:
            return option
    return None
