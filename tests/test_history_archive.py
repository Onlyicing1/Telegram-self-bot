"""Summary Archive: the read-only window onto durable Scheduled History state.

Everything here runs against an explicitly injected in-memory repository; no
live Supabase or Telegram is contacted. The panel is exercised through its real
registered handlers, not through a copied renderer.
"""
from __future__ import annotations

from types import SimpleNamespace

import pytest

import backend.ai.database.manager as manager_module
from backend.ai.database.task_repository import (
    MAX_HISTORY_LIST_LIMIT,
    HistoryStorageUnavailable,
    InMemoryTaskRepository,
)
from backend.bot.handlers import history
from backend.helper.panel_registry import get_panel_parent, get_panel_title
from backend.helper.panels import get_panel

OWNER = 88001
OTHER = 88002
PEER = -10088001


def _use(monkeypatch, repo, *, available=True):
    history._OWNER_ID = OWNER
    manager = SimpleNamespace(task=repo, supabase_available=available)
    monkeypatch.setattr(manager_module, "get_repository_manager", lambda: manager)
    if available:
        repo.supports_durable_history = True


def _callbacks(buttons) -> list[str]:
    values: list[str] = []
    for row in buttons:
        for button in row:
            data = getattr(button, "data", b"")
            values.append(data.decode() if isinstance(data, bytes) else str(data))
    return values


async def _seed(
    repo, *, owner=OWNER, label="History · Selected", peer=PEER,
    summary="", status="active",
):
    task = await repo.create_task(owner, {
        "label": label,
        "schedule_type": "daily",
        "schedule": {"hour": 9, "minute": 0},
        "timezone": "UTC",
        "actions": [{"name": "scheduled_history", "arguments": {}}],
        "notification_destination": {},
    })
    state = await repo.create_history_schedule(owner, task.id, peer, baseline_cursor=0)
    if summary:
        state = await repo.commit_history_success(
            owner, peer, expected_version=state.version,
            summary=summary, scanned_cursor=10, summarized_cursor=10,
        )
    if status != "active":
        state = await repo.record_history_failure(
            owner, peer, expected_version=state.version,
            error=f"{status} for test", status=status,
        )
    return task, state


def test_archive_panels_are_registered_under_history_once():
    history.register(None, OWNER, "Asia/Tehran")
    assert get_panel("history_archive") is history._archive_panel
    assert get_panel("history_archive_item") is history._archive_item_panel
    assert get_panel_parent("history_archive") == "history"
    assert get_panel_parent("history_archive_item") == "history_archive"
    assert get_panel_title("history_archive") == "Summary Archive"


def test_history_panel_exposes_exactly_one_summary_archive_entry():
    history._OWNER_ID = OWNER
    callbacks = [
        button.data.decode() if isinstance(button.data, bytes) else button.data
        for row in history._panel_buttons() for button in row
    ]
    assert callbacks.count("panel:history_archive") == 1


@pytest.mark.asyncio
async def test_archive_lists_only_the_current_owners_checkpoints(monkeypatch):
    repo = InMemoryTaskRepository()
    _use(monkeypatch, repo)
    await _seed(repo, label="Owner chat", peer=PEER, summary="owner summary")
    await _seed(repo, owner=OTHER, label="Other chat", peer=-10099002,
                summary="other secret summary")

    _title, body, buttons = await history._archive_list_view("")
    callbacks = _callbacks(buttons)

    assert any(str(PEER) in value for value in callbacks)
    assert not any(str(-10099002) in value for value in callbacks)
    texts = " ".join(
        str(getattr(button, "text", "")) for row in buttons for button in row
    )
    assert "Owner chat" in texts
    assert "Other chat" not in texts
    assert "other secret summary" not in body


@pytest.mark.asyncio
async def test_cross_owner_item_identifier_fails_closed(monkeypatch):
    repo = InMemoryTaskRepository()
    _use(monkeypatch, repo)
    await _seed(repo, owner=OTHER, label="Other chat", peer=-10099002,
                summary="other secret summary")

    _title, body, _buttons = await history._archive_item_view("-10099002")
    assert "no longer exists" in body
    assert "other secret summary" not in body


@pytest.mark.asyncio
async def test_empty_archive_is_honest(monkeypatch):
    repo = InMemoryTaskRepository()
    _use(monkeypatch, repo)
    _title, body, _buttons = await history._archive_list_view("")
    assert "No Scheduled History checkpoints exist yet" in body


@pytest.mark.asyncio
async def test_checkpoint_without_a_committed_summary_is_honest(monkeypatch):
    repo = InMemoryTaskRepository()
    _use(monkeypatch, repo)
    await _seed(repo, summary="")

    _title, body, buttons = await history._archive_list_view("")
    texts = " ".join(
        str(getattr(button, "text", "")) for row in buttons for button in row
    )
    assert "no summary yet" in texts
    assert "None of these checkpoints has committed a summary yet" in body

    _title, detail, _buttons = await history._archive_item_view(str(PEER))
    assert "No summary has been committed yet" in detail


@pytest.mark.asyncio
async def test_a_committed_summary_opens_and_renders_completely(monkeypatch):
    repo = InMemoryTaskRepository()
    _use(monkeypatch, repo)
    summary = " ".join(f"block-{index}" for index in range(900))
    await _seed(repo, label="Owner chat", peer=PEER, summary=summary)

    _title, body, _buttons = await history._archive_item_view(str(PEER))
    assert "Summary page 1 of" in body
    assert "block-0" in body

    chunks: list[str] = []
    page = 0
    while True:
        rendered = history._summary_page(summary, page)
        assert rendered is not None
        chunk, index, total = rendered
        chunks.append(chunk)
        _title, page_body, buttons = await history._archive_item_view(f"{PEER}:{page}")
        assert chunk in page_body
        assert f"Summary page {index + 1} of {total}" in page_body
        page += 1
        if page >= total:
            break

    assert "".join(chunks) == summary


@pytest.mark.asyncio
async def test_persian_and_english_summaries_are_preserved(monkeypatch):
    repo = InMemoryTaskRepository()
    _use(monkeypatch, repo)
    summary = "گزارش برنامه‌ریزی: تحویل نهایی تا فردا.\nRelease planning: ship tomorrow."
    await _seed(repo, summary=summary)

    _title, body, _buttons = await history._archive_item_view(str(PEER))
    assert "گزارش برنامه‌ریزی" in body
    assert "Release planning: ship tomorrow." in body


@pytest.mark.asyncio
async def test_pagination_is_bounded_and_deterministic(monkeypatch):
    repo = InMemoryTaskRepository()
    _use(monkeypatch, repo)
    peers = [-10000001 - index for index in range(history.ARCHIVE_PAGE_SIZE + 2)]
    for peer in peers:
        await _seed(repo, peer=peer, summary=f"summary {peer}")

    _title, body, buttons = await history._archive_list_view("")
    first = _callbacks(buttons)
    item_rows = [value for value in first if "history_archive_item" in value]
    assert len(item_rows) == history.ARCHIVE_PAGE_SIZE
    assert item_rows == [f"panel:history_archive_item:{peer}" for peer in peers[:history.ARCHIVE_PAGE_SIZE]]
    assert f"panel:history_archive:page:1" in first
    assert "Page 1 of" not in body and "Page 1 ·" in body

    _title, body, buttons = await history._archive_list_view("page:1")
    second = _callbacks(buttons)
    second_items = [value for value in second if "history_archive_item" in value]
    assert second_items == [f"panel:history_archive_item:{peer}" for peer in peers[history.ARCHIVE_PAGE_SIZE:]]
    assert "panel:history_archive:page:0" in second
    assert "panel:history_archive:page:2" not in second

    # The repository itself refuses an unbounded page.
    with pytest.raises(ValueError):
        await repo.list_history_schedules(OWNER, limit=MAX_HISTORY_LIST_LIMIT + 1, offset=0)
    with pytest.raises(ValueError):
        await repo.list_history_schedules(OWNER, limit=6, offset=-1)


@pytest.mark.asyncio
async def test_invalid_and_stale_tokens_fail_closed(monkeypatch):
    repo = InMemoryTaskRepository()
    _use(monkeypatch, repo)
    await _seed(repo, summary="only summary")

    _title, body, _buttons = await history._archive_list_view("page:-2")
    assert "not valid" in body
    _title, body, _buttons = await history._archive_list_view("not-a-page")
    assert "not valid" in body
    _title, body, _buttons = await history._archive_list_view("page:9")
    assert "no longer exists" in body

    _title, body, _buttons = await history._archive_item_view("not-a-peer")
    assert "not valid" in body
    _title, body, _buttons = await history._archive_item_view(f"{PEER}:bad")
    assert "not valid" in body
    _title, body, _buttons = await history._archive_item_view(f"{PEER}:99")
    assert "no longer available" in body
    _title, body, _buttons = await history._archive_item_view("-404404")
    assert "no longer exists" in body


@pytest.mark.asyncio
async def test_unavailable_storage_is_reported_not_fabricated(monkeypatch):
    repo = InMemoryTaskRepository()
    _use(monkeypatch, repo, available=False)

    _title, body, _buttons = await history._archive_list_view("")
    assert "Durable History storage is unavailable" in body
    _title, detail, _buttons = await history._archive_item_view(str(PEER))
    assert "Durable History storage is unavailable" in detail


@pytest.mark.asyncio
async def test_durable_storage_error_is_visible(monkeypatch):
    repo = InMemoryTaskRepository()
    _use(monkeypatch, repo)

    async def _boom(*args, **kwargs):
        raise HistoryStorageUnavailable("down")

    monkeypatch.setattr(repo, "list_history_schedules", _boom)
    _title, body, _buttons = await history._archive_list_view("")
    assert "Durable History storage is unavailable" in body

    async def _boom_read(*args, **kwargs):
        raise RuntimeError("connection reset")

    monkeypatch.setattr(repo, "get_history_schedule_by_peer", _boom_read)
    _title, detail, _buttons = await history._archive_item_view(str(PEER))
    assert "could not be read" in detail
    assert "RuntimeError" in detail


@pytest.mark.asyncio
async def test_opening_a_summary_never_calls_ai_or_telegram(monkeypatch):
    repo = InMemoryTaskRepository()
    _use(monkeypatch, repo)
    await _seed(repo, summary="durable summary text")

    def _must_not_run(*args, **kwargs):
        raise AssertionError("opening a stored summary must not call the AI")

    monkeypatch.setattr(history, "get_engine", _must_not_run)
    monkeypatch.setattr(history, "get_self_client", _must_not_run)

    _title, body, _buttons = await history._archive_item_view(str(PEER))
    assert "durable summary text" in body


@pytest.mark.asyncio
async def test_every_archive_callback_fits_telegrams_64_byte_budget(monkeypatch):
    repo = InMemoryTaskRepository()
    _use(monkeypatch, repo)
    peer = -(2 ** 63)
    summary = "summary " * 900
    await _seed(repo, peer=peer, summary=summary)

    callbacks = _callbacks((await history._archive_list_view(""))[2])
    rendered = history._summary_page(summary, 0)
    assert rendered is not None
    _chunk, _index, total = rendered
    callbacks += _callbacks(
        (await history._archive_item_view(f"{peer}:{total - 1}"))[2]
    )

    assert callbacks
    assert all(len(value.encode("utf-8")) <= 64 for value in callbacks)


@pytest.mark.asyncio
async def test_deleted_repair_rows_are_not_archive_entries(monkeypatch):
    repo = InMemoryTaskRepository()
    _use(monkeypatch, repo)
    _task, state = await _seed(repo, summary="stale summary")
    await repo.record_history_failure(
        OWNER, PEER, expected_version=state.version, error="legacy",
    )
    repo._history_schedules[(OWNER, PEER)].status = "deleted"
    repo._history_schedules[(OWNER, PEER)].summary = "stale summary"

    _title, body, buttons = await history._archive_list_view("")
    assert "No Scheduled History checkpoints exist yet" in body
    assert _callbacks(buttons) == []

    _title, detail, _buttons = await history._archive_item_view(str(PEER))
    assert "no longer exists" in detail
    assert "stale summary" not in detail
