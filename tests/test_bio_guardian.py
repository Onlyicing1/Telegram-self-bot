"""Global Bio mutation guardian regression tests.

Contract under test: at most ONE successful ``UpdateProfileRequest(about=...)``
within any rolling 60-second window, across EVERY bio mutation path. The
guardian is concurrency-safe, a rejected mutation fails honestly (never
reported as success), and a failed mutation never starts the window.
"""
from __future__ import annotations

import asyncio

import pytest

from backend.services import bio_guardian
from backend.services.bio_guardian import (
    BioMutationGuarded,
    guard_bio_mutation,
    seconds_until_bio_mutation_allowed,
)


@pytest.fixture(autouse=True)
def _reset_window():
    bio_guardian.reset_window_for_tests()
    yield
    bio_guardian.reset_window_for_tests()


@pytest.mark.asyncio
async def test_first_mutation_runs_and_opens_window():
    calls: list[str] = []

    async def mutate() -> str:
        calls.append("x")
        return "bio"

    assert await guard_bio_mutation(mutate) == "bio"
    assert calls == ["x"]
    assert seconds_until_bio_mutation_allowed() > 0


@pytest.mark.asyncio
async def test_second_mutation_within_window_is_rejected_and_honest():
    async def mutate() -> str:
        return "bio"

    await guard_bio_mutation(mutate)
    with pytest.raises(BioMutationGuarded):
        await guard_bio_mutation(mutate)


@pytest.mark.asyncio
async def test_failed_mutation_does_not_start_window():
    attempts: list[int] = []

    async def failing() -> str:
        attempts.append(1)
        raise RuntimeError("telegram down")

    with pytest.raises(RuntimeError):
        await guard_bio_mutation(failing)

    # The failed mutation consumed no window: a retry is allowed immediately.
    async def succeed() -> str:
        return "ok"

    assert await guard_bio_mutation(succeed) == "ok"
    assert len(attempts) == 1


@pytest.mark.asyncio
async def test_concurrent_mutations_exactly_one_reaches_telegram():
    calls: list[str] = []

    async def mutate() -> str:
        await asyncio.sleep(0.01)
        calls.append("hit")
        return "bio"

    results = await asyncio.gather(
        *(guard_bio_mutation(mutate) for _ in range(6)),
        return_exceptions=True,
    )
    successes = [r for r in results if not isinstance(r, BaseException)]
    rejections = [r for r in results if isinstance(r, BioMutationGuarded)]
    assert len(successes) == 1
    assert len(rejections) == 5
    assert calls == ["hit"]


@pytest.mark.asyncio
async def test_window_expires_and_allows_next_mutation(monkeypatch):
    async def mutate() -> str:
        return "bio"

    await guard_bio_mutation(mutate)

    real_monotonic = bio_guardian.time.monotonic
    monkeypatch.setattr(
        bio_guardian.time, "monotonic", lambda: real_monotonic() + bio_guardian.WINDOW_SECONDS + 1
    )
    assert seconds_until_bio_mutation_allowed() == 0.0
    assert await guard_bio_mutation(mutate) == "bio"


@pytest.mark.asyncio
async def test_cancelled_error_propagates_and_leaves_no_window():
    async def cancelled() -> str:
        raise asyncio.CancelledError()

    with pytest.raises(asyncio.CancelledError):
        await guard_bio_mutation(cancelled)
    assert seconds_until_bio_mutation_allowed() == 0.0


def test_window_seconds_constant_is_60():
    assert bio_guardian.WINDOW_SECONDS == 60.0
