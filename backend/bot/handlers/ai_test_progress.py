"""
Test Modules progress renderer — compact Unicode panel with coalesced edits.

Five-segment Unicode progress bar (▰ filled / ▱ empty, 20 percentage
points per segment, ALWAYS exactly five segments) driven by the streaming
model tester. Rapid completions are coalesced by a focused edit guardian:

  - at most one progress edit per window (``_MIN_PROGRESS_INTERVAL``),
  - always rendering the NEWEST submitted state (never stale),
  - identical renders skipped (no duplicate Telegram edits),
  - edits strictly serialized (responses can never overtake requests),
  - the terminal render is immediate, retried a bounded number of times
    on Telegram errors, and can never be dropped for a progress render,
  - guardian death can never strand the running flag (done-callback).

Pure rendering + rate math — no execution path, no persistence, no second
registry, no second tester. The panel edited is the SAME Glass UI message
whose button launched the run; edits go through ``_safe_edit`` (the
production callback edit path with its unchanged-content skip) so behavior
matches every other panel, with or without a separate helper bot.
"""
from __future__ import annotations

import asyncio
import logging
import time

from backend.ai.model_tester import test_all_models_streaming
from backend.runtime.task_guard import guarded_create_task

logger = logging.getLogger(__name__)

_SEGMENTS = 5
_MIN_PROGRESS_INTERVAL = 12.0
_TERMINAL_EDIT_RETRIES = 2
_TERMINAL_RETRY_BACKOFF = 2.0


def render_progress_bar(fraction: float) -> str:
    """Exactly five segments; each segment covers 20 percentage points."""
    fraction = min(max(fraction, 0.0), 1.0)
    filled = min(_SEGMENTS, int(fraction * _SEGMENTS + 1e-9))
    return "".join("▰" if i < filled else "▱" for i in range(_SEGMENTS))


def render_progress_view(
    done: int,
    total: int,
    last_item: dict | None,
    current_line: str = "",
) -> str:
    """Compact primary message: current runtime pair, bar, counts, latest.

    ``current_line`` is the authoritative runtime provider/model the AI
    request path would use right now (resolved by the caller from the
    ProviderManager, never from rendered text), so a run always shows what
    it is testing AGAINST.
    """
    fraction = (done / total) if total > 0 else 0.0
    lines: list[str] = [current_line] if current_line else []
    lines.append(f"{render_progress_bar(fraction)}  {done}/{total}")
    if last_item:
        mark = "✓" if last_item.get("status") == "AVAILABLE" else "×"
        lines.append(
            f"◇ {last_item.get('provider', '?')} · {last_item.get('model', '?')} {mark}"
        )
    return "\n".join(lines)


def _render_key(text: str, buttons: list) -> tuple:
    labels: list[str] = []
    for row in buttons or []:
        for btn in row or []:
            labels.append(str(getattr(btn, "text", "")))
    return (text, tuple(labels))


class _PanelEditGuardian:
    """Coalesce rapid panel updates into bounded, ordered Telegram edits.

    Submits are state updates, not edit requests. The single chain task
    renders at most one progress edit per ``interval`` window — always the
    newest submitted state — and skips renders identical to the last one
    actually applied. A terminal render is never delayed behind a progress
    window and never lost; it is retried a bounded number of times when
    Telegram rejects the edit. ``done`` is set when the terminal render
    settles OR when the chain dies, so awaiting callers can never hang and
    the running flag can never be stranded by a dead background task.
    """

    def __init__(self, edit, interval: float = _MIN_PROGRESS_INTERVAL) -> None:
        self._edit = edit  # async (text, buttons) -> bool (applied)
        self._interval = interval
        self._wake = asyncio.Event()
        self._pending: tuple[str, list, bool] | None = None
        self._last_key: tuple | None = None
        # Epoch 0: the FIRST progress edit renders immediately (fast
        # feedback after the launch view); subsequent edits are window-gated.
        self._last_edit_ts = 0.0
        self._chain: asyncio.Task | None = None
        self._terminal_retries = 0
        self.done = asyncio.Event()

    def submit(self, text: str, buttons: list, *, terminal: bool = False) -> None:
        key = _render_key(text, buttons)
        if key == self._last_key:
            return
        if (
            self._pending is not None
            and _render_key(self._pending[0], self._pending[1]) == key
        ):
            # Same content resubmitted — keep it, upgrade terminal if needed.
            self._pending = (text, buttons, terminal or self._pending[2])
            self._wake.set()
            return
        self._pending = (text, buttons, terminal)
        self._wake.set()
        if self._chain is None or self._chain.done():
            self._chain = guarded_create_task(
                self._run(), name="ai_test_progress:guardian"
            )
            self._chain.add_done_callback(lambda _t: self.done.set())

    async def wait(self) -> None:
        await self.done.wait()

    async def _run(self) -> None:
        try:
            while True:
                if self._pending is None:
                    return
                text, buttons, terminal = self._pending
                self._pending = None
                self._wake.clear()
                if not terminal:
                    delay = self._last_edit_ts + self._interval - time.monotonic()
                    if delay > 0:
                        try:
                            await asyncio.wait_for(self._wake.wait(), timeout=delay)
                            continue  # newer state arrived — render that instead
                        except asyncio.TimeoutError:
                            pass
                applied = await self._edit(text, buttons)
                self._last_edit_ts = time.monotonic()
                if applied:
                    self._last_key = _render_key(text, buttons)
                if terminal:
                    if applied or self._terminal_retries >= _TERMINAL_EDIT_RETRIES:
                        if not applied:
                            logger.error(
                                "AI_TEST_PROGRESS terminal render failed after %d retries",
                                self._terminal_retries,
                            )
                        return
                    self._terminal_retries += 1
                    self._pending = (text, buttons, True)
                    await asyncio.sleep(_TERMINAL_RETRY_BACKOFF)
        finally:
            self._chain = None


async def run_streaming_test(owner_id: int, event) -> None:
    """Run the streaming test and edit the launching panel via the guardian.

    ``event`` is the CallbackQuery event whose button launched the run; its
    message is the panel. The final edit carries the canonical results view
    shared with the batch action path, so the two entry points never drift.
    The running flag is cleared in ``finally`` on success, failure, and
    cancellation alike — even when a helper import dies inside the body.
    """
    import backend.bot.handlers.ai as _ai_mod

    try:
        from backend.helper import render_edit
        from backend.helper.panels import _edit_panel_message, _progress_buttons

        chat_id = getattr(event, "chat_id", None) or 0
        msg_id = getattr(event, "message_id", None) or 0

        async def _edit(text: str, buttons: list) -> bool:
            try:
                return await _edit_panel_message(event, chat_id, msg_id, text, buttons)
            except Exception as exc:
                logger.warning("AI_TEST_PROGRESS edit failed: %s", exc)
                return False

        guardian = _PanelEditGuardian(_edit)
        state: dict = {"last": None}
        try:
            current_line = _ai_mod._runtime_pair_line()
        except Exception:
            current_line = "Current: unavailable"

        def on_result(item: dict) -> None:
            state["last"] = item

        def on_progress(done: int, total: int) -> None:
            text = render_progress_view(done, total, state["last"], current_line)
            title, built = render_edit("Test Modules", text, _progress_buttons())
            guardian.submit(title, built)

        try:
            payload = await test_all_models_streaming(
                owner_id=owner_id, on_progress=on_progress, on_result=on_result,
            )
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logger.exception("Streaming model test run failed")
            title, built = render_edit(
                "Test Modules",
                f"× Test run failed — {type(exc).__name__}",
                _progress_buttons(),
            )
            guardian.submit(title, built, terminal=True)
            await guardian.wait()
            return

        _ai_mod._last_test_payload = payload

        from backend.bot.handlers.ai import _render_test_results

        body, buttons = _render_test_results(payload)
        title, built = render_edit("", body, buttons)
        guardian.submit(title, built, terminal=True)
        await guardian.wait()
    finally:
        _ai_mod._test_running = False
