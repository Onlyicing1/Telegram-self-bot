"""Bounded local-resource cooldown for the task repository.

Production evidence: the same runtime that successfully inserted a task
occurrence then failed the NEXT Supabase operation with
``[Errno 11] Resource temporarily unavailable``, and every incoming Telegram
event re-ran ``TaskEventDispatcher.handle_event`` → ``list_event_tasks`` →
another doomed Supabase call, each emitting a warning pair
(``Supabase event task query failed; using fallback`` +
``TASK_FALLBACK_CLASSIFIED reason=local_resource``).

Contract under test:

- A confirmed local-resource failure arms ONE bounded cooldown window; inside
  it the durable call is not re-issued (no request storm) and the existing
  in-memory fallback is served with the SAME non-durable semantics.
- The window expires on its own and the durable path is attempted again.
- A successful durable operation clears the protection immediately.
- The truthful classification is unchanged: local-resource stays
  ``local_resource``, a genuine store failure stays ``unavailable`` AND keeps
  being attempted/logged (never hidden by the cooldown).
- One episode produces one warning pair, not one per caller.

In-process only: no live Supabase, no live Telegram.
"""
from __future__ import annotations

import logging

import pytest

from backend.ai.database.task_repository import (
    FALLBACK_REASON_LOCAL_RESOURCE,
    FALLBACK_REASON_UNAVAILABLE,
    InMemoryTaskRepository,
    SupabaseTaskRepository,
)
from tests.test_task_fallback_classification import (
    _local_resource_transport_error,
    _unreachable_transport_error,
)
from tests.test_task_repository import FakeClient, row_task

OWNER = 10


def _repo(client) -> SupabaseTaskRepository:
    return SupabaseTaskRepository(client, InMemoryTaskRepository())


def _durable_attempts(client) -> int:
    return len(client.queries)


# ── no storm ───────────────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_repeated_local_resource_failures_do_not_reissue_the_durable_call(caplog):
    client = FakeClient([row_task()], error=_local_resource_transport_error())
    repo = _repo(client)

    with caplog.at_level(logging.WARNING, logger="backend.ai.database.task_repository"):
        for _ in range(25):
            assert await repo.list_event_tasks(OWNER, 20) == []

    # One durable attempt for the whole burst, not twenty-five.
    assert _durable_attempts(client) == 1
    assert repo.fallback_active is True
    assert repo.fallback_reason == FALLBACK_REASON_LOCAL_RESOURCE
    # One warning pair for the episode — not one per caller.
    assert caplog.text.count("Supabase event task query failed") == 1
    assert caplog.text.count("TASK_FALLBACK_CLASSIFIED") == 1
    assert "reason=local_resource" in caplog.text
    assert "Supabase unavailable" not in caplog.text


@pytest.mark.asyncio
async def test_event_dispatch_stops_storming_the_store_under_eagain():
    """The exact production pressure path: Telegram event → list_event_tasks."""
    from backend.ai.task_event_dispatcher import TaskEventDispatcher

    client = FakeClient([row_task()], error=_local_resource_transport_error())
    repo = _repo(client)
    dispatcher = TaskEventDispatcher(repo, OWNER)

    for message_id in range(1, 11):
        assert await dispatcher.handle_event(
            {"chat_id": -100, "message_id": message_id, "text": "hi"}
        ) == 0

    assert _durable_attempts(client) == 1


@pytest.mark.asyncio
async def test_local_resource_failure_keeps_the_truthful_classification():
    client = FakeClient([row_task()], error=_local_resource_transport_error())
    repo = _repo(client)
    assert await repo.get_occurrence(OWNER, 7, "k1") is None
    assert repo.fallback_reason == FALLBACK_REASON_LOCAL_RESOURCE
    # A cooldown-skipped call must not be re-labelled as a store outage.
    assert await repo.get_occurrence(OWNER, 7, "k1") is None
    assert repo.fallback_reason == FALLBACK_REASON_LOCAL_RESOURCE


# ── bounded window + recovery ──────────────────────────────────────────────


@pytest.mark.asyncio
async def test_cooldown_expires_and_the_durable_path_is_attempted_again():
    client = FakeClient([row_task()], error=_local_resource_transport_error())
    repo = _repo(client)

    assert await repo.list_tasks(OWNER) == []
    assert _durable_attempts(client) == 1
    assert repo._in_local_resource_cooldown() is True
    # Still inside the window: no new attempt.
    assert await repo.list_tasks(OWNER) == []
    assert _durable_attempts(client) == 1

    # The bounded window elapsed (deterministic, no sleeping).
    repo._local_resource_until = 0.0
    assert repo._in_local_resource_cooldown() is False
    assert await repo.list_tasks(OWNER) == []
    assert _durable_attempts(client) == 2


@pytest.mark.asyncio
async def test_a_skipped_call_never_extends_the_window():
    """A stream of events inside the window must not push the deadline out."""
    client = FakeClient([row_task()], error=_local_resource_transport_error())
    repo = _repo(client)
    await repo.list_tasks(OWNER)
    first_deadline = repo._local_resource_until

    for _ in range(10):
        await repo.list_tasks(OWNER)

    assert repo._local_resource_until == first_deadline


@pytest.mark.asyncio
async def test_successful_durable_read_resets_the_protection_state():
    client = FakeClient([row_task()], error=_local_resource_transport_error())
    repo = _repo(client)
    await repo.list_tasks(OWNER)
    assert repo._in_local_resource_cooldown() is True

    repo._local_resource_until = 0.0
    client.error = None
    assert len(await repo.list_tasks(OWNER)) == 1
    assert repo.fallback_active is False
    assert repo.fallback_reason == ""
    assert repo._in_local_resource_cooldown() is False


# ── genuine failures stay observable ───────────────────────────────────────


@pytest.mark.asyncio
async def test_genuine_store_failure_never_backs_off_and_keeps_unavailable(caplog):
    client = FakeClient([row_task()], error=_unreachable_transport_error())
    repo = _repo(client)

    with caplog.at_level(logging.WARNING, logger="backend.ai.database.task_repository"):
        for _ in range(3):
            assert await repo.list_tasks(OWNER) == []

    assert repo.fallback_reason == FALLBACK_REASON_UNAVAILABLE
    assert repo._in_local_resource_cooldown() is False
    # A genuine store failure is never suppressed: every attempt stays visible.
    assert _durable_attempts(client) == 3
    assert caplog.text.count("Supabase task list failed") == 3


@pytest.mark.asyncio
async def test_a_store_failure_clears_a_stale_local_episode():
    """Once the window elapses, a genuine failure is observed and never hidden.

    Inside the bounded window the durable call is not issued at all, so a
    different failure class cannot be observed yet — that is the point of the
    window. The moment it expires the real failure is attempted, reported with
    its own reason, and the local-resource protection state is cleared.
    """
    client = FakeClient([row_task()], error=_local_resource_transport_error())
    repo = _repo(client)
    await repo.list_tasks(OWNER)
    assert repo._in_local_resource_cooldown() is True

    repo._local_resource_until = 0.0
    client.error = _unreachable_transport_error()
    assert await repo.list_tasks(OWNER) == []
    assert repo.fallback_reason == FALLBACK_REASON_UNAVAILABLE
    assert repo._in_local_resource_cooldown() is False
    # The genuine failure keeps being attempted (no backoff for store errors).
    assert await repo.list_tasks(OWNER) == []
    assert _durable_attempts(client) == 3
