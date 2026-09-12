"""Taskloom task-list reliability tests — part B of the editor/list repair.

Symptom under repair: "sometimes Taskloom does not show my existing tasks".

Root cause (source-traced): a degraded durable read substitutes the in-memory
fallback store for the owner's list. ``SupabaseTaskRepository.list_tasks``
returns the fallback rows (usually empty) on ANY failure, and inside the
bounded local-resource cooldown the durable call is skipped entirely. The
Taskloom list additionally derived its ROWS and its STATUS COUNTERS from two
independent repository reads, so a degradation that began between them produced
a torn view (rows from the durable read, all-zero counters from the degraded
one) that could render as the genuine "No tasks yet." empty state.

Contract under test:

- One render = ONE repository read; the rows and the counters describe the same
  read (no torn list/count view).
- A degraded read is never rendered as the genuine empty state and never as an
  authoritative list: it carries the truthful degraded note.
- A successful durable read recovers the list and clears the degraded marker.
- Pagination never hides tasks: an out-of-range page clamps onto the nearest
  valid page instead of rendering empty.
- Owner isolation, deletion and empty-store behaviour are unchanged.
"""
from __future__ import annotations

import asyncio
from datetime import datetime, timezone

import pytest

from backend.ai.database.task_repository import (
    FALLBACK_REASON_LOCAL_RESOURCE,
    FALLBACK_REASON_UNAVAILABLE,
    InMemoryTaskRepository,
    SupabaseTaskRepository,
)
from backend.ai.task_management_interface import (
    FALLBACK_NOTE,
    FALLBACK_RESOURCE_NOTE,
)
from tests.test_task_repository import FakeClient, row_task

OWNER = 606
OTHER = 909
BASE = datetime(2026, 9, 12, 12, 0, tzinfo=timezone.utc)


def _run(coro):
    return asyncio.new_event_loop().run_until_complete(coro)


def _task_data(**overrides):
    value = {
        "label": "Recurring",
        "schedule_type": "interval",
        "schedule": {"seconds": 300},
        "timezone": "UTC",
        "next_run_at": BASE,
        "actions": [{"name": "send_message", "arguments": {"text": "hello"}}],
        "notification_destination": {"chat_id": 1},
    }
    value.update(overrides)
    return value


def _labels(buttons) -> list[str]:
    labels = []
    for row in buttons:
        for button in (row if isinstance(row, list) else [row]):
            labels.append(str(getattr(button, "text", "")))
    return labels


def _data(buttons) -> list[str]:
    values = []
    for row in buttons:
        for button in (row if isinstance(row, list) else [row]):
            raw = getattr(button, "data", button)
            values.append(raw.decode("utf-8") if isinstance(raw, bytes) else str(raw))
    return values


def _task_rows(buttons) -> list[str]:
    return [label for label in _labels(buttons) if label.startswith("Task ")]


class _FlappingRepository(InMemoryTaskRepository):
    """Read #1 is durable; later reads observe the store as degraded.

    That is exactly the ordering the live bug needed: the store degrades (or
    the bounded local-resource cooldown engages) between the panel's two
    independent reads. The OLD panel rendered read #1's rows together with
    read #2's all-zero counters.
    """

    def __init__(self):
        super().__init__()
        self.reads = 0
        self._degraded = False

    @property
    def fallback_active(self) -> bool:
        return self._degraded

    @property
    def fallback_reason(self) -> str:
        return FALLBACK_REASON_LOCAL_RESOURCE if self._degraded else ""

    async def list_tasks(self, owner_id):
        self.reads += 1
        if self.reads > 1:
            self._degraded = True
            return []
        return await super().list_tasks(owner_id)


class _DegradedRepository(InMemoryTaskRepository):
    """A store whose reads all degrade, with a chosen truthful reason."""

    def __init__(self, reason: str = FALLBACK_REASON_UNAVAILABLE):
        super().__init__()
        self._reason = reason

    @property
    def fallback_active(self) -> bool:
        return True

    @property
    def fallback_reason(self) -> str:
        return self._reason


@pytest.fixture()
def panel(monkeypatch):
    """Taskloom registered against a repository the test chooses per-case."""
    import backend.ai.database.manager as manager
    import backend.bot.handlers.taskloom as handler
    from backend.helper import inline_engine

    holder: dict[str, object] = {}

    class _Manager:
        @property
        def task(self):
            return holder["repo"]

    monkeypatch.setattr(manager, "get_repository_manager", lambda: _Manager())
    inline_engine.set_owner_id(OWNER)
    handler._drafts.clear()
    handler.register(client=None, owner_id=OWNER, tz_str="UTC")

    def _use(repo):
        holder["repo"] = repo
        return repo

    yield handler, _use
    handler._drafts.clear()


def _render(handler, extra: str = ""):
    result = _run(handler._taskloom_panel(None, extra))
    assert result is not None
    return result


# ── one read per render: rows and counters agree ───────────────────────────

def test_one_render_reads_the_list_once_so_rows_and_counters_agree(panel):
    handler, use = panel
    repo = use(_FlappingRepository())
    for index in range(3):
        _run(repo.create_task(OWNER, _task_data(label=f"job-{index}")))

    _title, body, buttons = _render(handler)

    # ONE repository read for the whole render (rows + counters + marker).
    assert repo.reads == 1
    # The counters describe the SAME read as the rows — never a torn view.
    assert "● 3 active" in body
    assert len(_task_rows(buttons)) == 3


def test_a_degradation_between_reads_cannot_hide_the_durable_rows(panel):
    handler, use = panel
    repo = use(_FlappingRepository())
    for index in range(4):
        _run(repo.create_task(OWNER, _task_data(label=f"job-{index}")))

    _title, body, buttons = _render(handler)

    assert "No tasks yet" not in body
    assert "Task list unavailable" not in body
    assert len(_task_rows(buttons)) == 4
    assert "● 4 active" in body


def test_a_healthy_list_reads_the_durable_store(panel):
    handler, use = panel
    repo = use(InMemoryTaskRepository())
    created = [
        _run(repo.create_task(OWNER, _task_data(label=f"job-{index}"))) for index in range(2)
    ]

    _title, body, buttons = _render(handler)

    assert "● 2 active" in body
    assert len(_task_rows(buttons)) == 2
    assert f"Task {created[0].id}: job-0" in _labels(buttons)


def test_a_durable_read_recovers_the_list_after_a_store_failure(panel):
    handler, use = panel
    client = FakeClient([row_task(id=7, owner_id=OWNER, label="durable job")])
    repo = use(SupabaseTaskRepository(client, InMemoryTaskRepository()))

    # The durable store is unreachable: the read degrades, nothing is shown as
    # the owner's authoritative list.
    client.error = RuntimeError("database unavailable")
    _title, body, _buttons_ = _render(handler)
    assert "Task list unavailable" in body
    assert repo.fallback_active is True

    # The store comes back: the SAME render path shows the durable rows again.
    client.error = None
    _title, body, buttons = _render(handler)

    assert "Task list unavailable" not in body
    assert any(label.endswith(": durable job") for label in _labels(buttons))
    assert repo.fallback_active is False
    assert repo.fallback_reason == ""


# ── honest degraded rendering (never a false empty list) ───────────────────

def test_a_degraded_read_is_never_rendered_as_the_genuine_empty_state(panel):
    handler, use = panel
    use(_DegradedRepository(FALLBACK_REASON_UNAVAILABLE))

    _title, body, buttons = _render(handler)

    assert "No tasks yet" not in body
    assert "Task list unavailable" in body
    assert FALLBACK_NOTE in body
    # The list surface stays usable: the owner can still create a task.
    assert f"panel:{handler.WIZARD_PANEL_QUERY}:new" in _data(buttons)


def test_local_resource_degradation_never_claims_supabase_is_unavailable(panel):
    handler, use = panel
    use(_DegradedRepository(FALLBACK_REASON_LOCAL_RESOURCE))

    _title, body, _buttons_ = _render(handler)

    assert FALLBACK_RESOURCE_NOTE in body
    assert FALLBACK_NOTE not in body
    assert "Supabase unavailable" not in body


def test_a_genuine_store_failure_is_distinguishable_from_zero_tasks(panel):
    handler, use = panel
    use(_DegradedRepository(FALLBACK_REASON_UNAVAILABLE))

    _title, body, _buttons_ = _render(handler)

    assert FALLBACK_NOTE in body
    assert "Supabase unavailable" in FALLBACK_NOTE
    assert "No tasks found" not in body


def test_a_degraded_list_with_rows_is_marked_non_durable(panel):
    handler, use = panel
    repo = use(_DegradedRepository(FALLBACK_REASON_LOCAL_RESOURCE))
    _run(repo.create_task(OWNER, _task_data(label="memory only")))

    _title, body, buttons = _render(handler)

    # The rows are real, but they are NOT the durable list — say so.
    assert any(label.endswith(": memory only") for label in _labels(buttons))
    assert FALLBACK_RESOURCE_NOTE in body


def test_a_healthy_empty_store_still_shows_the_genuine_empty_state(panel):
    handler, use = panel
    use(InMemoryTaskRepository())

    _title, body, _buttons_ = _render(handler)

    assert "No tasks yet." in body
    assert "Task list unavailable" not in body
    assert FALLBACK_NOTE not in body and FALLBACK_RESOURCE_NOTE not in body


def test_a_healthy_empty_durable_store_shows_the_genuine_empty_state(panel):
    handler, use = panel
    use(SupabaseTaskRepository(FakeClient([]), InMemoryTaskRepository()))

    _title, body, _buttons_ = _render(handler)

    assert "No tasks yet." in body
    assert "Task list unavailable" not in body


# ── owner scope, deletion, pagination ──────────────────────────────────────

def test_the_list_is_owner_scoped(panel):
    handler, use = panel
    repo = use(InMemoryTaskRepository())
    _run(repo.create_task(OWNER, _task_data(label="mine")))
    _run(repo.create_task(OTHER, _task_data(label="theirs")))

    _title, body, buttons = _render(handler)

    assert "● 1 active" in body
    assert any(label.endswith(": mine") for label in _labels(buttons))
    assert not any(": theirs" in label for label in _labels(buttons))


def test_a_deleted_task_leaves_the_list_and_the_counters(panel):
    handler, use = panel
    repo = use(InMemoryTaskRepository())
    keep = _run(repo.create_task(OWNER, _task_data(label="keep")))
    drop = _run(repo.create_task(OWNER, _task_data(label="drop")))
    _run(repo.delete_task(OWNER, drop.id, drop.version))

    _title, body, buttons = _render(handler)

    assert "● 1 active" in body
    assert f"Task {keep.id}: keep" in _labels(buttons)
    assert not any(f"Task {drop.id}:" in label for label in _labels(buttons))


def test_an_out_of_range_page_clamps_onto_the_last_valid_page(panel):
    handler, use = panel
    repo = use(InMemoryTaskRepository())
    for index in range(5):
        _run(repo.create_task(OWNER, _task_data(label=f"job-{index}")))

    _title, body, buttons = _render(handler, "9")

    assert "No tasks yet" not in body
    assert len(_task_rows(buttons)) == 1
    assert "2 / 2" in _labels(buttons)


def test_pagination_survives_a_shrink_so_no_page_renders_empty(panel):
    handler, use = panel
    repo = use(InMemoryTaskRepository())
    created = [
        _run(repo.create_task(OWNER, _task_data(label=f"job-{index}"))) for index in range(9)
    ]
    _title, _body, buttons = _render(handler, "2")
    assert len(_task_rows(buttons)) == 1

    for task in created[2:]:
        _run(repo.delete_task(OWNER, task.id, task.version))

    _title, body, buttons = _render(handler, "2")

    assert "No tasks yet" not in body
    # Clamped onto the (only) valid page: both survivors, no stale pager.
    assert _task_rows(buttons) == ["Task 1: job-0", "Task 2: job-1"]
    assert not any(label.startswith("1 / ") for label in _labels(buttons))


def test_paging_reaches_every_task_exactly_once(panel):
    handler, use = panel
    repo = use(InMemoryTaskRepository())
    created = [
        _run(repo.create_task(OWNER, _task_data(label=f"job-{index}"))) for index in range(9)
    ]

    seen: list[str] = []
    for page in range(3):
        _title, _body, buttons = _render(handler, str(page))
        seen.extend(_task_rows(buttons))

    assert len(seen) == 9
    assert len(set(seen)) == 9
    for task in created:
        assert any(label.startswith(f"Task {task.id}:") for label in seen)
