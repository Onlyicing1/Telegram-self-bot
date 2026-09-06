"""
Opt-in LIVE Supabase integration test for the AI memory subsystem.

REQUIRES real Supabase credentials (``SUPABASE_URL`` + ``SUPABASE_SERVICE_ROLE_KEY``).
The test SKIPS honestly when either is absent, so the normal suite never
performs live database operations. Run it explicitly with credentials set:

    SUPABASE_URL=... SUPABASE_SERVICE_ROLE_KEY=... \
        pytest tests/test_live_supabase_memory.py -m live_supabase -v

The test drives the PRODUCTION path end to end against the real existing
``public.ai_memories`` table (9 columns, fixed schema — never modified here):

    SupabaseMemoryRepository → backend.ai.persistence
        → backend.db.client.get_db() → supabase.create_client(...)
        (service-role key — RLS bypass is the production design; no client
        policies are created, RLS is never weakened)

It performs REAL inserts/selects/deletes against test-only rows identified
by a runtime-generated NEGATIVE owner_id (never a real Telegram id) and a
unique content marker, so production data cannot collide. Cleanup runs in
``finally`` and is strictly limited to rows created by this test.

The one intentional direct-client insert (NULL-importance row, section C)
exists because the repository path ALWAYS serializes ``importance`` (a float,
never NULL) — a NULL row cannot be produced through ``repo.save()``. The same
production service-role client creates that controlled row; reconstruction
is still verified through the repository read path.

Secret values are never printed or asserted on.
"""
from __future__ import annotations

import os
import uuid
from datetime import datetime, timedelta, timezone

import pytest

from backend.ai.database.memory_repository import SupabaseMemoryRepository
from backend.ai.memory.types import MemoryCategory, MemoryEntry, MemoryQuery, MemoryTier

pytestmark = [
    pytest.mark.live_supabase,
    pytest.mark.skipif(
        not (os.getenv("SUPABASE_URL") and os.getenv("SUPABASE_SERVICE_ROLE_KEY")),
        reason="live Supabase test requires SUPABASE_URL and SUPABASE_SERVICE_ROLE_KEY",
    ),
]


def _unique_owner_id() -> int:
    """Runtime-generated, clearly test-only owner id (negative bigint)."""
    return -(1_000_000_000 + int(uuid.uuid4().int % 900_000_000))


def test_live_supabase_memory_roundtrip():
    from backend.db.client import get_db

    owner_a = _unique_owner_id()
    owner_b = _unique_owner_id()
    marker = f"live-supabase-memory-{uuid.uuid4().hex}"
    content = f"{marker}: owner prefers concise replies"
    null_content = f"{marker}: null-importance controlled row"

    repo = SupabaseMemoryRepository()
    db = get_db()
    assert db is not None, "Supabase client unavailable despite credentials being set"

    created_ids: list[str] = []

    def _cleanup() -> list[str]:
        failures: list[str] = []
        for entry_id in list(created_ids):
            try:
                repo.delete(entry_id)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"delete(id={entry_id}) raised: {exc}")
        # Fallback: owner + content-scoped removal for any row created but
        # not tracked (never tier/category-broad, never other owners).
        for owner in (owner_a, owner_b):
            try:
                leftovers = repo.query(
                    MemoryQuery(owner_id=owner, query_text=marker, limit=50)
                )
                for entry in leftovers:
                    if entry.content in (content, null_content) and entry.id not in created_ids:
                        repo.delete(entry.id)
            except Exception as exc:  # noqa: BLE001
                failures.append(f"fallback cleanup owner={owner} raised: {exc}")
        return failures

    body_error: BaseException | None = None
    try:
        # ── A. REAL INSERT through the production repository path ──────────
        expires_at = datetime.now(timezone.utc) + timedelta(days=1)
        entry = MemoryEntry(
            id="",  # database-generated; never written by save()
            owner_id=owner_a,
            tier=MemoryTier.LONG,
            category=MemoryCategory.PREFERENCE,
            content=content,
            importance=0.75,
            expires_at=expires_at,
            metadata={"source": "live-supabase-test", "tags": ["audit"]},
        )
        assert repo.save(entry) is True

        # ── B. REAL SELECT through the production repository path ──────────
        rows = repo.query(
            MemoryQuery(owner_id=owner_a, tier=MemoryTier.LONG, query_text=marker, limit=10)
        )
        matches = [e for e in rows if e.content == content]
        assert len(matches) == 1, f"expected exactly one stored row, got {len(matches)}"
        stored = matches[0]
        assert stored.id and stored.id.isdigit(), "expected a real database-generated bigint id"
        created_ids.append(stored.id)
        assert stored.owner_id == owner_a
        assert stored.tier is MemoryTier.LONG
        assert stored.category is MemoryCategory.PREFERENCE
        assert stored.content == content
        assert stored.importance == 0.75
        assert stored.expires_at is not None
        assert abs((stored.expires_at - expires_at).total_seconds()) < 5
        assert stored.metadata == {"source": "live-supabase-test", "tags": ["audit"]}
        assert stored.created_at is not None, "expected database-generated created_at"

        # ── D. REAL DUPLICATE IDEMPOTENCY ──────────────────────────────────
        assert repo.save(entry) is True  # identical (owner, tier, content) write
        assert repo.count(owner_a, MemoryTier.LONG) == 2  # content + null row, no duplicate

        # ── C. REAL NULL / DEFAULT BEHAVIOR ────────────────────────────────
        # Repository save() always serializes importance (never NULL); create
        # the controlled NULL row with the same production service-role client
        # and verify reconstruction through the repository read path.
        inserted = (
            db.table("ai_memories")
            .insert({
                "owner_id": owner_a,
                "tier": "long",
                "category": "context",
                "content": null_content,
                "importance": None,
                "expires_at": None,
                "metadata": None,
            })
            .execute()
        )
        assert inserted.data and inserted.data[0].get("id") is not None
        created_ids.append(str(inserted.data[0]["id"]))
        null_rows = repo.query(
            MemoryQuery(owner_id=owner_a, query_text=marker, limit=50)
        )
        null_match = [e for e in null_rows if e.content == null_content]
        assert len(null_match) == 1
        assert null_match[0].importance == 0.5  # schema default, never coerced to 0.0
        assert null_match[0].expires_at is None
        assert null_match[0].metadata == {}

        # ── E. REAL OWNER ISOLATION (repository scoping, not just RLS) ─────
        other = repo.query(MemoryQuery(owner_id=owner_b, query_text=marker, limit=50))
        assert all(e.owner_id == owner_b for e in other)
        assert not any(e.content in (content, null_content) for e in other)

        # ── F. REAL DELETE by the database-generated id ────────────────────
        assert repo.delete(stored.id) is True
        remaining = [
            e for e in repo.query(
                MemoryQuery(owner_id=owner_a, query_text=marker, limit=50)
            )
            if e.content == content
        ]
        assert remaining == [], "deleted record still retrievable"
        created_ids.remove(stored.id)
    except BaseException as exc:  # noqa: BLE001
        body_error = exc
        raise
    finally:
        cleanup_failures = _cleanup()
        if cleanup_failures:
            if body_error is None:
                pytest.fail(f"live test cleanup FAILED: {cleanup_failures}")
            print(f"[live test] NOTE: cleanup FAILED: {cleanup_failures}")