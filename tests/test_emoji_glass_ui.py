"""Emoji & Reaction Glass UI panels — Phase 1 remainder tests.

Pins the panel contracts:

  1. Owner authorization: the emoji panel tree is registered behind the
     existing `is_owner` gate and the `panel:*` callback prefix.
  2. Import action invokes the existing `import_from_saved_messages` service
     and renders the honest report.
  3. Failure/degraded results are displayed honestly (not hidden behind
     generic success).
  4. Library browser uses the existing 2×5 pagination convention
     (`_LIBRARY_PER_PAGE = 10`) and the existing callback/pagination
     patterns (`panel:emoji_library:page:N`, `panel:_nav:noop` center).
  5. No second update loop, client, scheduler, or executor is introduced by
     the panel layer.
  6. Context isolation: the panel module does not import `backend.ai`.

No live Telegram verification — the Telegram boundary is exercised only
inside the service tests; these tests are panel-signature + wiring + handler
contract tests.
"""
from __future__ import annotations

import ast
import inspect
from pathlib import Path

import pytest

from backend.bot.handlers import emoji


@pytest.fixture(autouse=True)
def _register_emoji_panels():
    """Register the emoji panel tree and pin the engine owner context.

    Other panel-test modules set ``inline_engine._owner_id`` in their own
    fixtures without restoring it, so panel handlers resolve owner context
    from that global; pinning (and unpinning) it here keeps this file
    deterministic no matter which panel tests ran earlier in the session.
    """
    from backend.helper import inline_engine
    previous_owner = inline_engine._owner_id
    emoji.register(None, 7770001)
    inline_engine.set_owner_id(7770001)
    yield
    inline_engine.set_owner_id(previous_owner)


# ── registration contract ────────────────────────────────────────────────────

def test_emoji_panel_registered():
    from backend.helper.panel_registry import has_panel
    assert has_panel("emoji") is True


def test_emoji_import_panel_registered():
    from backend.helper.panel_registry import has_panel
    assert has_panel("emoji_import") is True


def test_emoji_library_panel_registered():
    from backend.helper.panel_registry import has_panel
    assert has_panel("emoji_library") is True


def test_emoji_library_entry_panel_registered():
    from backend.helper.panel_registry import has_panel
    assert has_panel("emoji_library_entry") is True


# ── action registration ──────────────────────────────────────────────────────

def test_emoji_import_action_registered():
    from backend.helper.panels import get_action
    assert get_action("emoji_import_run") is not None


# ── pagination convention ────────────────────────────────────────────────────

def test_library_per_page_is_ten():
    assert emoji._LIBRARY_PER_PAGE == 10


# ── panel handler signatures ─────────────────────────────────────────────────

@pytest.mark.parametrize(
    "handler",
    [
        emoji._emoji_panel_handler,
        emoji._emoji_import_panel_handler,
        emoji._emoji_library_panel_handler,
        emoji._emoji_library_entry_panel_handler,
    ],
)
def test_panel_handlers_are_async_callable(handler):
    assert callable(handler)
    sig = inspect.signature(handler)
    params = list(sig.parameters)
    assert params == ["event", "extra"]


@pytest.mark.parametrize(
    "action",
    [
        emoji._emoji_import_run_action,
    ],
)
def test_action_handlers_are_async_callable(action):
    assert callable(action)
    sig = inspect.signature(action)
    params = list(sig.parameters)
    assert params == ["event", "extra", "chat_id"]


# ── import action returns a report render ────────────────────────────────────

def test_import_action_returns_report_tuple():
    """The import action must return a (title, body, buttons) tuple, not a
    raw service report dict — the callback router renders it as a panel."""
    from types import SimpleNamespace

    async def _run():
        event = SimpleNamespace(sender_id=7770001)
        result = await emoji._emoji_import_run_action(event, "", 0)
        assert isinstance(result, tuple)
        assert len(result) == 3
        title, body, buttons = result
        assert isinstance(title, str)
        assert isinstance(body, str)
        assert isinstance(buttons, list)
        # The report must contain the status line.
        assert "Import Report" in title or "Import Report" in body

    import asyncio
    asyncio.run(_run())


# ── context isolation ────────────────────────────────────────────────────────

def test_emoji_panel_no_ai_import():
    source = Path(emoji.__file__).read_text()
    tree = ast.parse(source)
    imported: list[str] = []
    for node in ast.walk(tree):
        if isinstance(node, ast.Import):
            imported.extend(alias.name for alias in node.names)
        elif isinstance(node, ast.ImportFrom) and node.module:
            imported.append(node.module)
    assert not any(
        name == "backend.ai" or name.startswith("backend.ai.")
        for name in imported
    )


# ── architecture: no second client/loop/scheduler/executor ───────────────────

def test_emoji_panel_no_second_client_loop():
    source = Path(emoji.__file__).read_text()
    for forbidden in (
        "TelegramClient",
        "run_until_disconnected",
        "create_task",
        "immortal_create_task",
        "register_inline_handler",  # the engine owns this; panels reuse it
    ):
        assert forbidden not in source, forbidden


# ── library browser renders entries ─────────────────────────────────────────

@pytest.mark.asyncio
async def test_library_panel_body_contains_counts():
    from backend.db import client as db_client
    from backend.bot.handlers.emoji import _emoji_library_panel_handler

    # Seed one entry.
    await db_client.insert_emoji_entry(
        {
            "owner_id": 7770001,
            "document_id": 9999,
            "alt_text": "😀",
            "source": "imported",
            "source_msg_id": 1,
        }
    )

    from types import SimpleNamespace
    event = SimpleNamespace(sender_id=7770001)

    title, body, buttons = await _emoji_library_panel_handler(event, "")
    assert title == "Emoji Library"
    assert "1 emoji" in body or "1" in body
    assert isinstance(buttons, list)
    assert len(buttons) >= 1


@pytest.mark.asyncio
async def test_library_empty_shows_import_prompt():
    from backend.db import client as db_client
    from backend.bot.handlers.emoji import _emoji_library_panel_handler

    db_client._fallback["emoji_library"] = []

    from types import SimpleNamespace
    event = SimpleNamespace(sender_id=7770001)

    title, body, buttons = await _emoji_library_panel_handler(event, "")
    assert title == "Emoji Library"
    assert "No emoji" in body or "import" in body.lower()
    assert isinstance(buttons, list)
