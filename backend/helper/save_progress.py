"""Save transfer progress — real bytes rendered on the EXISTING status message.

One reporter is bound to one status message for one save operation. It turns
the service's real transfer observations (``SaveProgressEvent``) into bounded
edits of that message:

* the five-segment bar is the shared Glass UI renderer
  (``backend.helper.progress.render_progress_bar``), so Save and the model
  tester can never drift apart;
* the cadence depends on the transfer's actual size — ``50/100`` for a file of
  at most 10 MiB, ``20/40/60/80/100`` for a larger one, applied independently
  to the download phase and the upload phase;
* a known size emits its phase-start line (0%, which is what is actually
  known) as soon as the phase begins, then each milestone crossed;
* an unknown size emits ONE phase-start line without a percentage and a
  truthful 100% at the phase boundary — an unknown file is never treated as a
  small one and no percentage is invented;
* a repeated milestone, a repeated callback value or an identical render never
  produces another edit;
* an edit failure is logged and swallowed — the transfer continues;
* ``close`` marks the reporter terminal BEFORE rendering the final result, so a
  late progress event can never overwrite it.

Rendering lives in the UI layer; the Save service only reports bytes.
"""
from __future__ import annotations

import logging
from typing import Awaitable, Callable

from backend.helper import symbols as sym
from backend.helper.progress import render_progress_bar

logger = logging.getLogger(__name__)

#: Telegram-sized cadence boundary. A file AT OR BELOW this is "small".
SMALL_FILE_LIMIT_BYTES = 10 * 1024 * 1024
SMALL_FILE_MILESTONES: tuple[int, ...] = (50, 100)
LARGE_FILE_MILESTONES: tuple[int, ...] = (20, 40, 60, 80, 100)

_PHASE_LABELS = {"download": "Downloading", "upload": "Uploading"}


def milestones_for_size(size: int | None) -> tuple[int, ...]:
    """The emitting thresholds for a transfer of ``size`` bytes.

    Empty for an unknown size: percentages cannot be derived from bytes that
    are not known, so the only honest updates are the phase start (no percent)
    and the phase boundary (100% complete).
    """
    try:
        value = int(size)
    except (TypeError, ValueError):
        return ()
    if value <= 0:
        return ()
    return SMALL_FILE_MILESTONES if value <= SMALL_FILE_LIMIT_BYTES else LARGE_FILE_MILESTONES


def progress_percentage(completed: int | None, total: int | None) -> int | None:
    """Whole percentage from the transfer's OWN byte counts, or ``None``."""
    try:
        done = int(completed or 0)
        whole = int(total or 0)
    except (TypeError, ValueError):
        return None
    if whole <= 0:
        return None
    return max(0, min(100, done * 100 // whole))


class SaveProgressReporter:
    """Milestone-capped progress edits for ONE status message.

    ``edit`` is an async callable that edits the status message and returns
    ``True`` when Telegram applied the change. Every failure is contained here.
    """

    def __init__(
        self,
        edit: Callable[[str], Awaitable[bool]],
        *,
        title: str = "Deep Save",
    ) -> None:
        self._edit = edit
        self._title = title
        self._closed = False
        self._emitted: set[tuple[int, str, int | None]] = set()

    @property
    def closed(self) -> bool:
        return self._closed

    async def handle(self, event) -> None:
        """Report one transfer observation (a ``SaveProgressEvent``)."""
        if self._closed:
            return

        if event.finished:
            await self._emit(event, 100)
            return

        percentage = progress_percentage(event.completed_bytes, event.total_bytes)
        milestones = milestones_for_size(event.total_bytes)
        if not milestones:
            await self._emit(event, None)
            return

        # The phase boundary's own observation (0 bytes moved) is a REAL
        # update, and it is what makes progress visible the moment a phase
        # starts instead of only once a milestone is crossed. Later callbacks
        # cannot repeat it: the key is already claimed.
        await self._emit(event, 0)
        if percentage is None:
            return
        for milestone in milestones:
            if milestone <= percentage:
                await self._emit(event, milestone)

    async def close(self, text: str | None = None) -> bool:
        """Make the reporter terminal, then render ``text`` as the last edit.

        Once closed, every later progress event is ignored, so a terminal
        success/failure result can never be replaced by a stale update.
        """
        self._closed = True
        if text is None:
            return False
        try:
            return bool(await self._edit(text))
        except Exception as exc:  # noqa: BLE001 — the result was already produced
            logger.warning("[SAVE_PROGRESS] terminal edit failed: %s", exc)
            return False

    async def _emit(self, event, percentage: int | None) -> None:
        key = (int(event.item_index or 1), str(event.phase or ""), percentage)
        if key in self._emitted:
            return
        # Claim the milestone BEFORE the edit: a Telegram error must not cause
        # a retry storm across every later callback of the same phase.
        self._emitted.add(key)
        if self._closed:
            return
        text = self._render(event, percentage)
        try:
            await self._edit(text)
        except Exception as exc:  # noqa: BLE001 — progress never aborts a save
            logger.warning("[SAVE_PROGRESS] progress edit failed: %s", exc)

    def _render(self, event, percentage: int | None) -> str:
        label = _PHASE_LABELS.get(str(event.phase or ""), "Saving")
        lines: list[str] = [f"**{self._title}**", ""]
        if int(event.item_count or 1) > 1:
            lines.append(
                f"{sym.PANEL_HEADING} File {int(event.item_index or 1)}/{int(event.item_count)}"
            )
        if percentage is None:
            lines.append(f"{label}…")
            lines.append(render_progress_bar(0.0))
        else:
            lines.append(f"{label} {percentage}%")
            lines.append(render_progress_bar(percentage / 100))
        return "\n".join(lines)
