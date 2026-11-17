"""G4-A/F1 remediation regression tests (M1–M6).

These exercise the real service, repository, strict-JSON parser and Explorer
render functions with the explicit in-memory double.  They prove application
semantics only: the PostgreSQL advisory locks, RLS, grants and triggers in the
migration are NOT executed by this file.
"""
from __future__ import annotations

import asyncio
import uuid
from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from backend.ai.providers.base.contract import ProviderResponse
from backend.ai.tools.base import PermissionLevel, requires_owner_confirmation
from backend.ai.tools.context import ToolContext
from backend.ai.tools.evidence import CitedSummaryTool
from backend.bot.handlers import evidence as explorer
from backend.evidence import constants
from backend.evidence.models import (
    EvidenceQuotaError,
    EvidenceNotFound,
    EvidenceRef,
    EvidenceRun,
    EvidenceStorageError,
    EvidenceStorageUnavailable,
    EvidenceValidationError,
)
from backend.evidence.repository import EvidenceRepository, in_memory_repository
from backend.evidence.service import EvidenceService, reset_evidence_service_for_tests
from backend.telegram_api.peers import PeerType

OWNER = 7770001
OTHER_OWNER = 8880002
CHAT = -100555000
NOW = datetime(2026, 11, 17, 12, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> None:
        self.now = self.now + timedelta(**kwargs)


class _FakeMessage:
    def __init__(self, mid: int, text: str = "") -> None:
        self.id = mid
        self.chat_id = CHAT
        self.sender_id = OWNER
        self.text = text
        self.date = NOW
        self.media = None
        self.reply_to = None
        self.out = True


class _FakeSource:
    def __init__(self, messages: list[_FakeMessage]) -> None:
        self.messages = list(messages)

    async def get_entity(self, chat_id: Any):
        from telethon.tl import types

        return types.User(id=abs(int(chat_id)) or 1, first_name="Owner")

    def iter_messages(self, chat_id: Any, **kwargs: Any):
        limit = kwargs.get("limit")
        max_id = kwargs.get("max_id")
        min_id = kwargs.get("min_id")
        selected = [
            item for item in self.messages
            if (max_id is None or item.id < max_id)
            and (min_id is None or item.id > min_id)
        ]
        selected.sort(key=lambda item: item.id, reverse=True)
        if limit is not None:
            selected = selected[:limit]

        async def _aiter():
            for item in selected:
                yield item

        return _aiter()


class _FakeManager:
    def __init__(self, text: str, *, success: bool = True) -> None:
        self.text = text
        self.success = success
        self.calls: list[dict[str, Any]] = []

    async def chat(self, messages, **kwargs):
        self.calls.append({"messages": messages, **kwargs})
        return ProviderResponse(
            text=self.text, provider_name="fake", success=self.success,
        )


class _Event:
    sender_id = OWNER


class _ExplodingDB:
    """Fails the test if the durable path is reached at all."""

    def __init__(self) -> None:
        self.used = False

    def table(self, _name: str):
        self.used = True
        raise AssertionError("mismatched refs must not reach the database")

    def rpc(self, _name: str, _params=None):
        self.used = True
        raise AssertionError("mismatched refs must not reach the database")


class _RecordingDB:
    """Minimal insert-only Supabase stand-in that records the payload."""

    def __init__(self) -> None:
        self.inserted: list[dict[str, Any]] = []

    def table(self, _name: str):
        outer = self

        class _Table:
            def insert(self, rows):
                outer.inserted = list(rows)
                return self

            def execute(self):
                from types import SimpleNamespace

                return SimpleNamespace(data=outer.inserted)

        return _Table()


def _ref(run: EvidenceRun, *, owner: int | None = None, ordinal: int = 1) -> EvidenceRef:
    return EvidenceRef(
        run_id=run.id,
        owner_id=run.owner_id if owner is None else owner,
        ordinal=ordinal,
        message_id=ordinal,
        excerpt="excerpt",
        content_hash="a" * 64,
        has_media=False,
        excerpt_truncated=False,
    )


def _repo(clock: _Clock | None = None) -> EvidenceRepository:
    return in_memory_repository(clock or _Clock())


def _service(clock: _Clock | None = None) -> tuple[EvidenceService, EvidenceRepository, _Clock]:
    clock = clock or _Clock()
    repository = _repo(clock)
    return EvidenceService(repository), repository, clock


async def _run(
    service: EvidenceService,
    messages: list[_FakeMessage],
    *,
    owner: int = OWNER,
):
    return await service.create_run(
        owner_id=owner,
        peer_id=CHAT,
        peer_name="Test chat",
        ttl_days=constants.DEFAULT_TTL_DAYS,
        messages=messages,
    )


def _run_stub() -> EvidenceRun:
    return EvidenceRun(
        id=str(uuid.uuid4()),
        owner_id=OWNER,
        peer_id=CHAT,
        peer_name="Test chat",
        ttl_days=7,
        first_message_id=1,
        last_message_id=1,
        ref_count=1,
        text_bytes=4,
        summary="",
        created_at=NOW,
        expires_at=NOW + timedelta(days=7),
    )


# ── M1: refs identify the exact immutable revision ─────────────────────────


@pytest.mark.asyncio
async def test_archive_links_the_ref_to_the_exact_revision():
    service, repository, _clock = _service()
    bundle = await _run(service, [_FakeMessage(1, "alpha"), _FakeMessage(2, "beta")])

    created = await service.archive_ref(OWNER, bundle.run.id, "E1", approved=True)

    stored = await service.get_run(OWNER, bundle.run.id)
    assert stored.refs[0].revision_id == created.revision_id
    assert stored.refs[1].revision_id is None

    unchanged = await service.archive_ref(OWNER, bundle.run.id, "E1", approved=True)
    assert unchanged.status == "unchanged"
    again = await service.get_run(OWNER, bundle.run.id)
    assert again.refs[0].revision_id == created.revision_id
    assert (await service.list_revisions(OWNER, created.source_id))[0].id == created.revision_id


@pytest.mark.asyncio
async def test_changed_content_links_a_new_revision_and_never_repoints_old_refs():
    service, _repository, _clock = _service()
    first_run = await _run(service, [_FakeMessage(7, "old text")])
    first = await service.archive_ref(OWNER, first_run.run.id, "E1", approved=True)

    second_run = await _run(service, [_FakeMessage(7, "new text")])
    second = await service.archive_ref(OWNER, second_run.run.id, "E1", approved=True)

    assert first.revision_id != second.revision_id
    old_refs = (await service.get_run(OWNER, first_run.run.id)).refs
    new_refs = (await service.get_run(OWNER, second_run.run.id)).refs
    assert old_refs[0].revision_id == first.revision_id
    assert new_refs[0].revision_id == second.revision_id


@pytest.mark.asyncio
async def test_ref_revision_link_rejects_missing_foreign_and_mismatched_revisions():
    service, repository, _clock = _service()
    bundle = await _run(service, [_FakeMessage(1, "one"), _FakeMessage(2, "two")])
    created = await service.archive_ref(OWNER, bundle.run.id, "E1", approved=True)

    with pytest.raises(EvidenceStorageError):
        await repository.link_ref_revision(
            OWNER, bundle.run.id, 1, str(uuid.uuid4()),
        )
    with pytest.raises(EvidenceStorageError):
        await repository.link_ref_revision(
            OTHER_OWNER, bundle.run.id, 1, created.revision_id,
        )
    # Ref ordinal 2 is message 2; the revision belongs to message 1.
    with pytest.raises(EvidenceStorageError):
        await repository.link_ref_revision(
            OWNER, bundle.run.id, 2, created.revision_id,
        )


@pytest.mark.asyncio
async def test_ref_revision_link_is_write_once():
    service, repository, _clock = _service()
    bundle = await _run(service, [_FakeMessage(9, "first")])
    first = await service.archive_ref(OWNER, bundle.run.id, "E1", approved=True)

    changed = await _run(service, [_FakeMessage(9, "second")])
    second = await service.archive_ref(OWNER, changed.run.id, "E1", approved=True)

    await repository.link_ref_revision(
        OWNER, bundle.run.id, 1, first.revision_id,
    )
    with pytest.raises(EvidenceStorageError):
        await repository.link_ref_revision(
            OWNER, bundle.run.id, 1, second.revision_id,
        )


# ── M2: no silent in-memory fallback for durable Evidence ──────────────────


def test_repository_requires_explicit_memory_opt_in(monkeypatch):
    monkeypatch.setattr("backend.db.client.get_db", lambda: None)
    durable = EvidenceRepository()
    assert durable.is_memory is False
    assert in_memory_repository().is_memory is True


@pytest.mark.asyncio
async def test_unavailable_supabase_fails_closed_without_writing(monkeypatch):
    monkeypatch.setattr("backend.db.client.get_db", lambda: None)
    repository = EvidenceRepository()

    with pytest.raises(EvidenceStorageUnavailable):
        await repository.database_now()
    with pytest.raises(EvidenceStorageError):
        await repository.create_run(_run_stub(), max_active=20, now=NOW)
    with pytest.raises(EvidenceStorageError):
        await repository.archive_atomic(
            owner_id=OWNER,
            peer_id=CHAT,
            message_id=1,
            source_id=str(uuid.uuid4()),
            revision_id=str(uuid.uuid4()),
            content_hash="a" * 64,
            text="text",
            approval_reference="owner-confirmed",
            now=NOW,
            max_records=5000,
            max_bytes=50 * 1024 * 1024,
        )
    assert repository._memory.runs == {}
    assert repository._memory.revisions == {}


@pytest.mark.asyncio
async def test_service_never_reports_a_run_when_storage_is_unavailable(monkeypatch):
    monkeypatch.setattr("backend.db.client.get_db", lambda: None)
    service = EvidenceService(EvidenceRepository())

    with pytest.raises(EvidenceStorageError):
        await _run(service, [_FakeMessage(1, "text")])
    # The failure is explicit, and nothing was persisted in a fallback store.
    assert service.repository.is_memory is False


# ── M3: atomic owner quotas ────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_concurrent_run_admission_never_exceeds_the_cap(monkeypatch):
    monkeypatch.setattr(constants, "MAX_ACTIVE_RUNS_PER_OWNER", 5)
    service, _repository, _clock = _service()

    async def admit(index: int):
        return await _run(service, [_FakeMessage(1000 + index)])

    results = await asyncio.gather(
        *(admit(index) for index in range(12)), return_exceptions=True,
    )
    created = [item for item in results if not isinstance(item, BaseException)]
    refused = [item for item in results if isinstance(item, EvidenceQuotaError)]
    assert len(created) == 5
    assert len(refused) == 7
    assert await service.active_run_count(OWNER) == 5


@pytest.mark.asyncio
async def test_concurrent_archives_never_exceed_the_record_quota(monkeypatch):
    monkeypatch.setattr(constants, "MAX_ARCHIVE_RECORDS_PER_OWNER", 4)
    service, _repository, _clock = _service()
    bundles = [
        await _run(service, [_FakeMessage(index, f"text {index}")])
        for index in range(1, 9)
    ]

    async def archive(bundle):
        return await service.archive_ref(OWNER, bundle.run.id, "E1", approved=True)

    results = await asyncio.gather(
        *(archive(bundle) for bundle in bundles), return_exceptions=True,
    )
    created = [item for item in results if getattr(item, "status", "") == "created"]
    refused = [item for item in results if isinstance(item, EvidenceQuotaError)]
    assert len(created) == 4
    assert len(refused) == 4
    assert (await service.archive_usage(OWNER)).records == 4


@pytest.mark.asyncio
async def test_concurrent_same_hash_archive_is_one_immutable_revision():
    service, _repository, _clock = _service()
    bundle = await _run(service, [_FakeMessage(5, "same content")])

    async def archive():
        return await service.archive_ref(OWNER, bundle.run.id, "E1", approved=True)

    results = await asyncio.gather(*(archive() for _ in range(6)))
    created = [item for item in results if item.status == "created"]
    unchanged = [item for item in results if item.status == "unchanged"]
    assert len(created) == 1
    assert len(unchanged) == 5
    assert len(await service.list_revisions(OWNER, created[0].source_id)) == 1


# ── M4: Cited Summary opt-in at the execution boundary ─────────────────────


@pytest.mark.asyncio
async def test_cited_summary_refuses_without_explicit_opt_in():
    service, _repository, _clock = _service()
    source = _FakeSource([_FakeMessage(1, "alpha"), _FakeMessage(2, "beta")])
    manager = _FakeManager('{"summary":"x","citations":["E1"]}')

    with pytest.raises(EvidenceValidationError):
        await service.run_cited_summary(
            source,
            owner_id=OWNER,
            peer_id=CHAT,
            peer_name="Test chat",
            count=2,
            provider_manager=manager,
            peer_type=PeerType.PRIVATE_USER,
        )
    assert manager.calls == []
    assert await service.list_active_runs(OWNER) == []


@pytest.mark.asyncio
async def test_cited_summary_opt_in_is_per_request_not_remembered():
    service, _repository, _clock = _service()
    source = _FakeSource([_FakeMessage(1, "alpha"), _FakeMessage(2, "beta")])
    manager = _FakeManager('{"summary":"cited","citations":["E1"]}')

    first = await service.run_cited_summary(
        source,
        owner_id=OWNER,
        peer_id=CHAT,
        peer_name="Test chat",
        count=2,
        provider_manager=manager,
        peer_type=PeerType.PRIVATE_USER,
        opt_in=True,
    )
    assert first.summary == "cited"

    with pytest.raises(EvidenceValidationError):
        await service.run_cited_summary(
            source,
            owner_id=OWNER,
            peer_id=CHAT,
            peer_name="Test chat",
            count=2,
            provider_manager=manager,
            peer_type=PeerType.PRIVATE_USER,
        )
    assert len(await service.list_active_runs(OWNER)) == 1


def test_cited_summary_tool_requires_the_confirmation_gate():
    context = ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC", extra={})
    tool = CitedSummaryTool(context)
    assert tool.permission_level is PermissionLevel.CONFIRMATION_REQUIRED
    assert requires_owner_confirmation(tool) is True
    assert tool.safe is False


# ── M6: Explorer holds no global result state and re-checks ownership ──────


def test_explorer_has_no_module_level_result_cache():
    for name in (
        "_run_bundle_cache",
        "_runs_sync_cache",
        "_suggestions_cache",
        "_candidate_cache",
        "_sources_cache",
        "_source_cache",
        "_revisions_cache",
    ):
        assert not hasattr(explorer, name), name


@pytest.mark.asyncio
async def test_explorer_views_render_only_the_data_they_receive():
    service, repository, _clock = _service()
    first = await _run(service, [_FakeMessage(1, "alpha excerpt")])
    second = await _run(service, [_FakeMessage(2, "beta excerpt")])
    repository_bundle_a = await service.get_run(OWNER, first.run.id)
    repository_bundle_b = await service.get_run(OWNER, second.run.id)

    body_a = explorer._run_view(repository_bundle_a, 0)[1]
    body_b = explorer._run_view(repository_bundle_b, 0)[1]

    assert "alpha excerpt" in body_a and "beta excerpt" not in body_a
    assert "beta excerpt" in body_b and "alpha excerpt" not in body_b


@pytest.mark.asyncio
async def test_explorer_rejects_another_owners_opaque_ids():
    service, repository, _clock = _service()
    reset_evidence_service_for_tests(repository)
    explorer.register(owner_id=OWNER)
    mine = await _run(service, [_FakeMessage(1, "mine")])
    theirs = await _run(service, [_FakeMessage(2, "theirs")], owner=OTHER_OWNER)

    assert (await explorer._load_run(mine.run.id)).run.id == mine.run.id
    with pytest.raises(EvidenceNotFound):
        await explorer._load_run(theirs.run.id)

    archived = await service.archive_ref(OWNER, mine.run.id, "E1", approved=True)
    reset_evidence_service_for_tests(repository)
    with pytest.raises(EvidenceNotFound):
        await explorer._load_source(str(uuid.uuid4()))
    assert archived.source_id


@pytest.mark.asyncio
async def test_stale_explorer_callback_fails_closed_after_run_deletion():
    service, repository, _clock = _service()
    reset_evidence_service_for_tests(repository)
    explorer.register(owner_id=OWNER)
    bundle = await _run(service, [_FakeMessage(1, "temporary")])
    await repository.delete_run(OWNER, bundle.run.id)

    _title, body, _buttons = await explorer._open_action(_Event(), bundle.run.id, 1)
    assert "no longer exists" in body


# ── Ref/Run ownership integrity (migration guard + insert path) ────────────


@pytest.mark.asyncio
async def test_ref_whose_owner_differs_from_its_run_is_rejected():
    repository = _repo()
    run = _run_stub()
    repository._memory.runs[run.id] = run
    repository._memory.refs[run.id] = []

    with pytest.raises(EvidenceStorageError):
        await repository.add_refs(run, [_ref(run, owner=OTHER_OWNER)])
    assert repository._memory.refs[run.id] == []


@pytest.mark.asyncio
async def test_durable_add_refs_rejects_mismatched_owner_before_writing():
    db = _ExplodingDB()
    repository = EvidenceRepository(db=db)
    run = _run_stub()

    with pytest.raises(EvidenceStorageError):
        await repository.add_refs(run, [_ref(run, owner=OTHER_OWNER)])
    assert db.used is False


@pytest.mark.asyncio
async def test_durable_add_refs_accepts_a_same_owner_ref():
    db = _RecordingDB()
    repository = EvidenceRepository(db=db)
    run = _run_stub()

    await repository.add_refs(run, [_ref(run)])

    assert len(db.inserted) == 1
    assert db.inserted[0]["owner_id"] == OWNER
    assert db.inserted[0]["run_id"] == run.id


def test_migration_guard_covers_insert_and_checks_the_run_owner():
    from pathlib import Path

    sql = Path(
        "supabase/migrations/20261117123943_create_evidence_foundation.sql"
    ).read_text(encoding="utf-8")

    assert "BEFORE INSERT OR UPDATE ON ai_evidence_refs" in sql
    assert "FROM ai_evidence_runs run" in sql
    assert "SELECT run.owner_id, run.peer_id" in sql
    assert "run_owner <> NEW.owner_id" in sql
    assert "evidence ref owner does not match its run owner" in sql
    # Existing approved guarantees stay in the same guard.
    assert "an evidence ref revision link is write-once" in sql
    assert "revision_owner <> NEW.owner_id" in sql
    assert "revision_peer IS DISTINCT FROM run_peer" in sql
    assert "revision_message <> NEW.message_id" in sql
    # The deliberate no-FK policy for a historical unresolved reference.
    assert "revision_id         uuid," in sql
    assert "revision_id uuid REFERENCES" not in sql
