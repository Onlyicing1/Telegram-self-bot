"""Editing ONE existing panel message from a background flow.

Deep Save runs after the CallbackQuery that armed it has been answered and
returned, so the flow has no event left to edit: it must address the panel
message the owner is already looking at. Two principals can do that, and
neither of them sends a message:

* the **Helper Bot**, through the inline-message identifier Telegram handed it
  when the owner clicked the panel's button (``messages.editInlineBotMessage``,
  ``Telethon.edit_message(entity=InputBotInlineMessageID…)``). This is the only
  way a bot may edit a message that was sent after a user picked its inline
  result.
* the **owner's own account**, through the panel's chat + message id
  (``messages.editMessage``), which is who sent the inline result in the first
  place.

Both paths keep the panel's inline keyboard: ``buttons`` is never passed, and
MTProto edits leave an omitted ``reply_markup`` untouched. That is deliberate —
the keyboard belongs to the panel's own design, and a progress edit must not
remove it (nor send the wrong markup type, which is what ``buttons=[]`` does).

The path is chosen from the identifiers that actually reached the handler,
never from guesswork. If the inline edit is refused, the SAME message is still
updated through its chat/message id instead of creating a second one; if no
identifier is usable at all, the caller runs without UI.

A generation token makes the newest Save operation on a panel its only writer,
so a stale or superseded save can never overwrite a newer panel state.
"""
from __future__ import annotations

import logging
from typing import Any

logger = logging.getLogger(__name__)

__all__ = [
    "ExistingPanelEditor",
    "begin_panel_operation",
    "finish_panel_operation",
    "inline_message_id_from_event",
    "panel_operation_is_current",
    "reset_panel_operations",
    "supersede_panel_operation",
]

#: The current writer token per panel, keyed by (chat id, message id). Bounded
#: by the number of panels an operation claims, and released by
#: :func:`finish_panel_operation` when the terminal edit is done.
_generations: dict[tuple[int, int], int] = {}
_counter = 0


def _key(chat_id: int | None, msg_id: int | None) -> tuple[int, int]:
    return (int(chat_id or 0), int(msg_id or 0))


def inline_message_id_from_event(event: Any) -> Any | None:
    """The editable inline handle Telegram gave the Helper Bot for one click.

    Only an inline-flavoured callback query carries it: there ``query.msg_id``
    is an ``InputBotInlineMessageID``/``InputBotInlineMessageID64``. For an
    ordinary message click it is a plain ``int`` and no inline identifier
    exists at all — it is never derived from chat/message ids, because Telegram
    offers no lookup that could produce it.
    """
    try:
        from telethon.tl import types

        candidate = getattr(getattr(event, "query", None), "msg_id", None)
    except Exception:
        return None
    if isinstance(
        candidate, (types.InputBotInlineMessageID, types.InputBotInlineMessageID64)
    ):
        return candidate
    return None


def begin_panel_operation(chat_id: int, msg_id: int) -> int:
    """Promote THIS operation to the panel's only writer; return its token."""
    global _counter
    _counter += 1
    _generations[_key(chat_id, msg_id)] = _counter
    return _counter


def panel_operation_is_current(
    chat_id: int, msg_id: int, generation: int
) -> bool:
    """Whether ``generation`` still owns this panel."""
    return _generations.get(_key(chat_id, msg_id)) == generation


def supersede_panel_operation(chat_id: int, msg_id: int) -> bool:
    """A newer interaction with the panel supersedes any background writer.

    Called for a click on a panel that a background flow (Deep Save) is still
    editing: the panel's new state belongs to that click, so the older flow
    must stop writing rather than overwrite it. A no-op unless an operation
    currently owns the panel, so ordinary navigation costs nothing and no
    state accumulates for panels that never run a background flow.
    """
    global _counter
    key = _key(chat_id, msg_id)
    if key not in _generations:
        return False
    _counter += 1
    _generations[key] = _counter
    return True


def finish_panel_operation(chat_id: int, msg_id: int, generation: int) -> None:
    """Release the token once the terminal edit has been rendered."""
    key = _key(chat_id, msg_id)
    if _generations.get(key) == generation:
        _generations.pop(key, None)


def reset_panel_operations() -> None:
    """Drop every token (process shutdown, and isolated tests)."""
    _generations.clear()


class ExistingPanelEditor:
    """Edit ONE existing panel message; never send a message.

    ``inline_id`` + ``helper`` select the Helper Bot's inline path;
    ``self_client`` + ``(chat_id, msg_id)`` select the owner's own edit path.
    Both may be supplied: the inline path is tried first and a refusal (an
    invalid identifier, a deleted panel, a permission error) degrades to the
    chat/message path on the same message.
    """

    def __init__(
        self,
        *,
        chat_id: int = 0,
        msg_id: int = 0,
        inline_id: Any = None,
        helper: Any = None,
        self_client: Any = None,
        generation: int = 0,
    ) -> None:
        self._chat_id = int(chat_id or 0)
        self._msg_id = int(msg_id or 0)
        self._inline_id = inline_id
        self._helper = helper
        self._self_client = self_client
        self._generation = int(generation or 0)
        self._applied: str | None = None
        self._inline_failed = False

    @property
    def path(self) -> str:
        """Which edit path the NEXT update would take."""
        if self._inline_id is not None and self._helper is not None and not self._inline_failed:
            return "helper-inline"
        if self._self_client is not None and self._chat_id and self._msg_id:
            return "self-chat"
        return "unavailable"

    @property
    def available(self) -> bool:
        return self.path != "unavailable"

    async def __call__(self, text: str) -> bool:
        return await self.edit(text)

    async def edit(self, text: str) -> bool:
        """Render ``text`` onto the panel in place. Errors never escape."""
        from backend.helper.panel_render import render_edit

        rendered, _buttons = render_edit("", text, [])
        if self._generation and not panel_operation_is_current(
            self._chat_id, self._msg_id, self._generation
        ):
            logger.info(
                "[PANEL_EDIT] stale operation dropped (chat=%s msg=%s gen=%s)",
                self._chat_id, self._msg_id, self._generation,
            )
            return False
        if rendered == self._applied:
            return True

        if self.path == "helper-inline":
            if await self._edit_inline(rendered):
                self._applied = rendered
                return True
            logger.warning(
                "[PANEL_EDIT] inline edit refused for chat=%s msg=%s — "
                "retrying the same message through its chat/message id",
                self._chat_id, self._msg_id,
            )

        if self._self_client is not None and self._chat_id and self._msg_id:
            if await self._edit_chat(rendered):
                self._applied = rendered
                return True

        return False

    async def _edit_inline(self, rendered: str) -> bool:
        try:
            # No ``buttons``: the panel keeps the keyboard it already has.
            await self._helper.edit_message(self._inline_id, text=rendered)
            return True
        except Exception as exc:  # noqa: BLE001 — progress never aborts a save
            if "not modified" in str(exc).lower():
                return True
            # One refusal is enough: stop paying for a failing RPC on every
            # later milestone and use the panel's own coordinates instead.
            self._inline_failed = True
            logger.warning("[PANEL_EDIT] inline panel edit failed: %s", exc)
            return False

    async def _edit_chat(self, rendered: str) -> bool:
        try:
            # No ``buttons``: a user edit cannot attach a bot keyboard, and an
            # omitted reply_markup keeps the existing one untouched.
            await self._self_client.edit_message(self._chat_id, self._msg_id, rendered)
            return True
        except Exception as exc:  # noqa: BLE001 — progress never aborts a save
            if "not modified" in str(exc).lower():
                return True
            logger.warning(
                "[PANEL_EDIT] panel edit failed chat=%s msg=%s: %s",
                self._chat_id, self._msg_id, exc,
            )
            return False
