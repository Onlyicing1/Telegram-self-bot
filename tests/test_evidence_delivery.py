"""G4-A/F2 — Evidence delivery tests (Explorer, revalidation, RPC contracts).

These are mocked-repository and contract tests.  No live Supabase/PostgREST,
Telegram, Render or provider service is used.  The durable-path tests exercise
the repository's real request/response handling against a recording stand-in,
and the migration file is the single source of the RPC argument names; the SQL
itself is NOT executed here.
"""
from __future__ import annotations

import re
import uuid
from datetime import datetime, timedelta, timezone
from pathlib import Path
from types import SimpleNamespace
from typing import Any

import pytest

from backend.bot.handlers import evidence as explorer
from backend.evidence import constants
from backend.evidence.models import (
    ArchiveUsage,
    EvidenceConfirmationRequired,
    EvidenceQuotaError,
    EvidenceRevision,
    EvidenceSource,
    EvidenceStorageError,
    EvidenceValidationError,
)
from backend.evidence.repository import EvidenceRepository, in_memory_repository
from backend.evidence.service import EvidenceService

OWNER = 7770001
OTHER_OWNER = 8880002
CHAT = -100555000
NOW = datetime(2026, 11, 18, 12, 0, tzinfo=timezone.utc)
MIGRATION = Path(
    "supabase/migrations/20261117123943_create_evidence_foundation.sql"
)
MAX_CALLBACK_BYTES = 64


class _Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now


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


class _Event:
    def __init__(self, sender_id: int = OWNER) -> None:
        self.sender_id = sender_id


class _TelegramClient:
    """The real Telegram boundary is called; only Telethon is replaced."""

    def __init__(self, message: Any = None, *, error: Exception | None = None) -> None:
        self._message = message
        self._error = error
        self.calls: list[tuple[Any, Any]] = []

    async def get_messages(self, chat_id: Any, ids: Any = None):
        self.calls.append((chat_id, ids))
        if self._error is not None:
            raise self._error
        return self._message


def _repo() -> EvidenceRepository:
    return in_memory_repository(_Clock())


def _service(clock: _Clock | None = None) -> tuple[EvidenceService, EvidenceRepository]:
    repository = in_memory_repository(clock or _Clock())
    return EvidenceService(repository), repository


async def _archived_source(
    service: EvidenceService, *, message_id: int = 42, text: str = "archived text",
) -> tuple[EvidenceSource, EvidenceRevision, Any]:
    bundle = await service.create_run(
        owner_id=OWNER,
        peer_id=CHAT,
        peer_name="Delivery chat",
        ttl_days=constants.DEFAULT_TTL_DAYS,
        messages=[_FakeMessage(message_id, text)],
    )
    result = await service.archive_ref(OWNER, bundle.run.id, "E1", approved=True)
    source = await service.get_source(OWNER, result.source_id)
    revision = await service.get_revision(OWNER, result.revision_id)
    return source, revision, bundle


def _callbacks(buttons: list) -> list[str]:
    values: list[str] = []
    for row in buttons:
        for button in row:
            data = getattr(button, "data", b"") or b""
            values.append(data.decode("utf-8"))
    return values


# ── Explorer rendering: no crash and no silently truncated callbacks ───────


def _run_bundle():
    from backend.evidence.models import EvidenceRef, EvidenceRun, EvidenceRunBundle

    run = EvidenceRun(
        id=str(uuid.uuid4()),
        owner_id=OWNER,
        peer_id=CHAT,
        peer_name="Delivery chat",
        ttl_days=7,
        first_message_id=1,
        last_message_id=50,
        ref_count=50,
        text_bytes=500,
        summary="summary",
        created_at=NOW,
        expires_at=NOW + timedelta(days=7),
    )
    refs = tuple(
        EvidenceRef(
            run_id=run.id,
            owner_id=OWNER,
            ordinal=index,
            message_id=index,
            excerpt=f"excerpt {index}",
            content_hash="a" * 64,
            has_media=False,
        )
        for index in range(1, 51)
    )
    return EvidenceRunBundle(run=run, refs=refs)


def _suggestion():
    from backend.evidence.models import ArchiveSuggestion

    return ArchiveSuggestion(
        key="E50", ordinal=50, excerpt="excerpt", reason="reason",
    )


def _source_pair(revision_count: int = 25):
    source = EvidenceSource(
        id=str(uuid.uuid4()),
        owner_id=OWNER,
        peer_id=CHAT,
        message_id=42,
        observed_state="edited",
        first_seen_at=NOW,
        last_observed_at=NOW,
    )
    revisions = [
        EvidenceRevision(
            id=str(uuid.uuid4()),
            source_id=source.id,
            owner_id=OWNER,
            revision_no=index,
            content_hash="b" * 64,
            text=f"revision {index}",
            approval_reference="owner-confirmed",
            captured_at=NOW,
        )
        for index in range(1, revision_count + 1)
    ]
    return source, revisions


@pytest.mark.asyncio
async def test_every_explorer_view_renders_and_fits_telegram_callback_limit():
    from backend.evidence.service import reset_evidence_service_for_tests

    _service_obj, repository = _service()
    reset_evidence_service_for_tests(repository)
    bundle = _run_bundle()
    source, revisions = _source_pair()
    usage = ArchiveUsage(records=5000, bytes=constants.MAX_ARCHIVE_BYTES_PER_OWNER)

    views = {
        "overview": await explorer._overview(),
        "runs": explorer._runs_view([bundle.run]),
        "run": explorer._run_view(bundle, 0),
        "run_last_page": explorer._run_view(bundle, 9),
        "suggest": explorer._suggest_view(bundle.run.id, [_suggestion()]),
        "confirm": explorer._confirm_view(bundle.run.id, _suggestion()),
        "archive": explorer._archive_view([source], usage, 20, True),
        "source_first_page": explorer._source_view(source, revisions, 0),
        "source": explorer._source_view(source, revisions, 2),
        "delete_source": explorer._delete_source_confirm(source.id),
        "delete_revision": explorer._delete_revision_confirm(
            source.id, revisions[-1].id,
        ),
    }

    expected_ids = {
        bundle.run.id,
        source.id,
        revisions[0].id,
        revisions[-1].id,
    }
    seen_payloads: list[str] = []
    for name, (_title, body, buttons) in views.items():
        assert isinstance(body, str) and body, name
        payloads = _callbacks(buttons)
        assert payloads, name
        for payload in payloads:
            assert len(payload.encode("utf-8")) <= MAX_CALLBACK_BYTES, (name, payload)
            seen_payloads.append(payload)

    joined = "\n".join(seen_payloads)
    for opaque_id in expected_ids:
        # A truncated id would be unresolvable at click time.
        assert opaque_id in joined, opaque_id
    assert "E50" in joined
    assert not any("…" in payload for payload in seen_payloads)


def test_confirm_view_targets_the_exact_run_and_reference_key():
    bundle = _run_bundle()
    _title, body, buttons = explorer._confirm_view(bundle.run.id, _suggestion())
    payloads = _callbacks(buttons)
    assert f"action:evidence_commit:{bundle.run.id}:E50" in payloads
    assert f"action:evidence_open:{bundle.run.id}" in payloads
    assert "E50" in body and "excerpt" in body


def test_archive_and_revision_pages_expose_only_reachable_navigation():
    service, _repository = _service()
    source, revisions = _source_pair(25)
    usage = ArchiveUsage(records=1, bytes=10)

    first = _callbacks(
        explorer._archive_view([source], usage, 0, True)[2]
    )
    middle = _callbacks(
        explorer._archive_view([source], usage, 10, True)[2]
    )
    last = _callbacks(
        explorer._archive_view([source], usage, 20, False)[2]
    )
    assert "action:evidence_archive:10" in first
    assert "action:evidence_archive:0" not in first
    assert "action:evidence_archive:20" in middle
    assert "action:evidence_archive:0" in middle
    assert "action:evidence_archive:10" in last
    assert "action:evidence_archive:30" not in last

    page_two = _callbacks(explorer._source_view(source, revisions, 1)[2])
    assert f"action:evidence_source:{source.id}:0" in page_two
    assert f"action:evidence_source:{source.id}:2" in page_two
    # Page two shows revisions 11..20 and their exact ids only.
    assert f"action:evidence_delrev:{revisions[10].id}" in page_two
    assert f"action:evidence_delrev:{revisions[20].id}" not in page_two


def test_owner_gate_uses_the_canonical_guard():
    explorer.register(owner_id=OWNER)
    assert explorer._owner_ok(_Event(OWNER)) is True
    assert explorer._owner_ok(_Event(OTHER_OWNER)) is False
    assert explorer._owner_ok(_Event(0)) is False


# ── exact-source revalidation ─────────────────────────────────────────────


@pytest.mark.asyncio
async def test_revalidation_records_live_edited_deleted_and_inaccessible():
    service, _repository = _service()
    source, revision, _bundle = await _archived_source(service)

    live = await service.revalidate_source(
        OWNER,
        source.id,
        source=_TelegramClient(SimpleNamespace(id=42, text="archived text")),
    )
    assert live.observed_state == "live"

    client = _TelegramClient(SimpleNamespace(id=42, text="changed on telegram"))
    edited = await service.revalidate_source(OWNER, source.id, source=client)
    assert edited.observed_state == "edited"
    assert client.calls == [(CHAT, 42)]

    deleted = await service.revalidate_source(
        OWNER, source.id, source=_TelegramClient(None),
    )
    assert deleted.observed_state == "deleted"

    inaccessible = await service.revalidate_source(
        OWNER,
        source.id,
        source=_TelegramClient(error=RuntimeError("CHANNEL_PRIVATE")),
    )
    assert inaccessible.observed_state == "inaccessible"

    # No revalidation can rewrite the immutable revision.
    stored = await service.list_revisions(OWNER, source.id)
    assert len(stored) == 1
    assert stored[0].id == revision.id
    assert stored[0].text == "archived text"
    assert stored[0].content_hash == revision.content_hash
    assert stored[0].revision_no == revision.revision_no


@pytest.mark.asyncio
async def test_revalidation_is_owner_scoped_and_requires_telegram():
    service, _repository = _service()
    source, _revision, _bundle = await _archived_source(service)

    with pytest.raises(Exception):
        await service.revalidate_source(
            OTHER_OWNER,
            source.id,
            source=_TelegramClient(SimpleNamespace(id=42, text="archived text")),
        )
    with pytest.raises(EvidenceValidationError):
        await service.revalidate_source(OWNER, source.id, source=None)
    unchanged = await service.get_source(OWNER, source.id)
    assert unchanged.observed_state == "live"


@pytest.mark.asyncio
async def test_recheck_action_returns_the_source_view_without_mutating_it():
    service, repository = _service()
    source, revision, _bundle = await _archived_source(service)
    from backend.evidence.service import reset_evidence_service_for_tests

    reset_evidence_service_for_tests(repository)
    explorer.register(owner_id=OWNER)
    monkeypatch = pytest.MonkeyPatch()
    monkeypatch.setattr(
        explorer,
        "get_self_client",
        lambda: _TelegramClient(SimpleNamespace(id=42, text="archived text")),
    )
    try:
        _title, body, buttons = await explorer._recheck_action(
            _Event(), source.id, 1,
        )
    finally:
        monkeypatch.undo()
    assert "live" in body
    assert "revision 1" in body.lower()
    assert f"action:evidence_delrev:{revision.id}" in _callbacks(buttons)


@pytest.mark.asyncio
async def test_explorer_commit_and_delete_paths_report_success():
    """The write paths must render a success notice, not a post-write crash."""
    from backend.evidence.service import reset_evidence_service_for_tests

    service, repository = _service()
    bundle = await service.create_run(
        owner_id=OWNER,
        peer_id=CHAT,
        peer_name="Delivery chat",
        ttl_days=constants.DEFAULT_TTL_DAYS,
        messages=[_FakeMessage(1, "alpha"), _FakeMessage(2, "beta")],
    )
    reset_evidence_service_for_tests(repository)
    explorer.register(owner_id=OWNER)

    _title, body, _buttons = await explorer._commit_action(
        _Event(), f"{bundle.run.id}:E1", 1,
    )
    assert "immutable revision 1" in body

    suggestions = await explorer._load_suggestions(bundle.run.id)
    _title, confirm_body, _buttons = await explorer._confirm_action(
        _Event(), f"{bundle.run.id}:E2", 1,
    )
    assert "E2" in confirm_body and suggestions

    archived = await service.get_run(OWNER, bundle.run.id)
    linked = await service.get_revision(OWNER, archived.refs[0].revision_id)
    source, revisions = await explorer._load_source(linked.source_id)
    assert [item.id for item in revisions] == [linked.id]

    _title, source_body, source_buttons = await explorer._source_action(
        _Event(), source.id, 1,
    )
    assert "Observed status" in source_body
    assert f"action:evidence_recheck:{source.id}" in _callbacks(source_buttons)

    _title, confirm_del, del_buttons = await explorer._delete_revision_action(
        _Event(), linked.id, 1,
    )
    assert "destructive" in confirm_del
    payloads = _callbacks(del_buttons)
    assert f"action:evidence_delrev_ok:{linked.id}" in payloads
    assert f"action:evidence_delrev_all:{linked.id}" in payloads

    _title, after_delete, _buttons = await explorer._delete_revision_commit_action(
        _Event(), linked.id, 1,
    )
    assert "revision was deleted" in after_delete
    assert await service.list_revisions(OWNER, source.id) == []

    _title, src_after, _buttons = await explorer._delete_source_action(
        _Event(), source.id, 1,
    )
    assert "destructive" in src_after
    _title, archive_body, _buttons = await explorer._delete_source_commit_action(
        _Event(), source.id, 1,
    )
    assert "source was deleted" in archive_body
    assert (await service.archive_usage(OWNER)).records == 0


# ── explicit approval writes nothing before consent ───────────────────────


@pytest.mark.asyncio
async def test_unapproved_archive_neither_writes_nor_links_a_revision():
    service, repository = _service()
    bundle = await service.create_run(
        owner_id=OWNER,
        peer_id=CHAT,
        peer_name="Delivery chat",
        ttl_days=constants.DEFAULT_TTL_DAYS,
        messages=[_FakeMessage(9, "consent text")],
    )
    created = await service.archive_ref(OWNER, bundle.run.id, "E1", approved=True)
    second = await service.create_run(
        owner_id=OWNER,
        peer_id=CHAT,
        peer_name="Delivery chat",
        ttl_days=constants.DEFAULT_TTL_DAYS,
        messages=[_FakeMessage(9, "consent text")],
    )

    with pytest.raises(EvidenceConfirmationRequired):
        await service.archive_ref(OWNER, second.run.id, "E1")
    # The refused request wrote nothing at all, including the ref link.
    refs = (await service.get_run(OWNER, second.run.id)).refs
    assert refs[0].revision_id is None
    assert (await service.archive_usage(OWNER)).records == 1
    assert created.revision_id
    assert repository.is_memory is True


# ── archive pagination ────────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_archive_pages_are_bounded_and_reachable(monkeypatch):
    monkeypatch.setattr(constants, "MAX_ACTIVE_RUNS_PER_OWNER", 50)
    service, _repository = _service()
    for index in range(1, 26):
        bundle = await service.create_run(
            owner_id=OWNER,
            peer_id=CHAT,
            peer_name="Delivery chat",
            ttl_days=constants.DEFAULT_TTL_DAYS,
            messages=[_FakeMessage(index, f"archived {index}")],
        )
        await service.archive_ref(OWNER, bundle.run.id, "E1", approved=True)

    first = await service.list_archive_sources(OWNER, limit=10, offset=0)
    second = await service.list_archive_sources(OWNER, limit=10, offset=10)
    third = await service.list_archive_sources(OWNER, limit=10, offset=20)
    assert len(first) == 10
    assert len(third) == 5
    assert not ({item.id for item in first} & {item.id for item in second})
    assert len({item.id for item in (*first, *second, *third)}) == 25

    # The Explorer's look-ahead row only decides whether a next page exists.
    lookahead = await service.list_archive_sources(OWNER, limit=11, offset=0)
    assert len(lookahead) == 11
    assert len(await service.list_archive_sources(OWNER, limit=11, offset=20)) == 5


# ── durable RPC contracts (arguments and response shapes) ────────────────


def _sql_params(sql: str, function: str) -> list[str]:
    match = re.search(
        r"CREATE OR REPLACE FUNCTION\s+" + function + r"\s*\((.*?)\)\s*RETURNS",
        sql,
        re.S,
    )
    assert match is not None, function
    return [
        part.strip().split()[0]
        for part in match.group(1).split(",")
        if part.strip()
    ]


class _RpcRecorder:
    """Records the exact PostgREST RPC name and argument map."""

    def __init__(self, responses: dict[str, Any]) -> None:
        self.responses = responses
        self.calls: list[tuple[str, dict[str, Any]]] = []

    def rpc(self, name: str, params: dict[str, Any] | None = None):
        self.calls.append((name, dict(params or {})))
        data = self.responses[name]

        class _Query:
            def execute(self):
                return SimpleNamespace(data=data)

        return _Query()

    def table(self, _name: str):
        class _Table:
            def select(self, *_args, **_kwargs):
                return self

            def delete(self):
                return self

            def eq(self, *_args, **_kwargs):
                return self

            def lte(self, *_args, **_kwargs):
                return self

            def order(self, *_args, **_kwargs):
                return self

            def limit(self, *_args, **_kwargs):
                return self

            def range(self, *_args, **_kwargs):
                return self

            def execute(self):
                return SimpleNamespace(data=[])

        return _Table()


def _run_row() -> dict[str, Any]:
    return {
        "id": str(uuid.uuid4()),
        "owner_id": OWNER,
        "peer_id": CHAT,
        "peer_name": "Delivery chat",
        "ttl_days": 7,
        "first_message_id": 1,
        "last_message_id": 2,
        "ref_count": 2,
        "text_bytes": 10,
        "summary": "",
        "created_at": "2026-11-18T12:00:00+00:00",
        "expires_at": "2026-11-25T12:00:00+00:00",
    }


def _durable_repository(responses: dict[str, Any]) -> tuple[EvidenceRepository, _RpcRecorder]:
    recorder = _RpcRecorder(responses)
    return EvidenceRepository(db=recorder), recorder


@pytest.mark.asyncio
async def test_repository_rpc_arguments_match_the_applied_migration():
    sql = MIGRATION.read_text(encoding="utf-8")
    revision_id = str(uuid.uuid4())
    repository, recorder = _durable_repository({
        "ai_evidence_now": "2026-11-18T12:00:00+00:00",
        "ai_evidence_create_run": {"status": "created", "run": _run_row()},
        "ai_evidence_archive_revision": {
            "status": "created",
            "source_id": str(uuid.uuid4()),
            "revision_id": revision_id,
            "revision_no": 1,
            "content_hash": "a" * 64,
        },
        "ai_evidence_archive_usage": {"record_count": 0, "byte_count": 0},
    })
    from backend.evidence.models import EvidenceRun

    run = EvidenceRun(
        id=str(uuid.uuid4()),
        owner_id=OWNER,
        peer_id=CHAT,
        peer_name="Delivery chat",
        ttl_days=7,
        first_message_id=1,
        last_message_id=2,
        ref_count=2,
        text_bytes=10,
        summary="",
        created_at=NOW,
        expires_at=NOW + timedelta(days=7),
    )
    await repository.database_now()
    stored = await repository.create_run(run, max_active=20, now=NOW)
    assert stored.id
    write = await repository.archive_atomic(
        owner_id=OWNER,
        peer_id=CHAT,
        message_id=42,
        source_id=str(uuid.uuid4()),
        revision_id=str(uuid.uuid4()),
        content_hash="a" * 64,
        text="text",
        approval_reference="owner-confirmed",
        now=NOW,
        max_records=constants.MAX_ARCHIVE_RECORDS_PER_OWNER,
        max_bytes=constants.MAX_ARCHIVE_BYTES_PER_OWNER,
    )
    assert write.status == "created" and write.revision_id == revision_id
    await repository.archive_usage(OWNER)

    expected = {
        "ai_evidence_now": _sql_params(sql, "ai_evidence_now"),
        "ai_evidence_create_run": _sql_params(sql, "ai_evidence_create_run"),
        "ai_evidence_archive_revision": _sql_params(
            sql, "ai_evidence_archive_revision",
        ),
        "ai_evidence_archive_usage": _sql_params(
            sql, "ai_evidence_archive_usage",
        ),
    }
    sent = {name: params for name, params in recorder.calls}
    assert set(sent) == set(expected)
    for name, params in sent.items():
        assert sorted(params) == sorted(expected[name]), name
    # The approved limits are supplied by the application, never re-declared
    # as SQL constants.
    assert sent["ai_evidence_create_run"]["p_max_active"] == (
        constants.MAX_ACTIVE_RUNS_PER_OWNER
    )
    assert sent["ai_evidence_archive_revision"]["p_max_records"] == (
        constants.MAX_ARCHIVE_RECORDS_PER_OWNER
    )


@pytest.mark.asyncio
async def test_repository_fails_closed_on_unexpected_rpc_responses():
    repository, _recorder = _durable_repository({
        "ai_evidence_create_run": {"status": "quota"},
    })
    from backend.evidence.models import EvidenceRun

    run = EvidenceRun(
        id=str(uuid.uuid4()),
        owner_id=OWNER,
        peer_id=CHAT,
        peer_name="",
        ttl_days=7,
        first_message_id=1,
        last_message_id=1,
        ref_count=1,
        text_bytes=1,
        summary="",
        created_at=NOW,
        expires_at=NOW + timedelta(days=7),
    )
    with pytest.raises(EvidenceQuotaError):
        await repository.create_run(run, max_active=20, now=NOW)

    missing_row, _recorder = _durable_repository({
        "ai_evidence_create_run": {"status": "created"},
    })
    with pytest.raises(EvidenceStorageError):
        await missing_row.create_run(run, max_active=20, now=NOW)

    bad_archive, _recorder = _durable_repository({
        "ai_evidence_archive_revision": {"status": "unknown"},
    })
    with pytest.raises(EvidenceStorageError):
        await bad_archive.archive_atomic(
            owner_id=OWNER,
            peer_id=CHAT,
            message_id=1,
            source_id=str(uuid.uuid4()),
            revision_id=str(uuid.uuid4()),
            content_hash="a" * 64,
            text="t",
            approval_reference="owner-confirmed",
            now=NOW,
            max_records=1,
            max_bytes=1,
        )

    no_revision, _recorder = _durable_repository({
        "ai_evidence_archive_revision": {"status": "created", "source_id": "s"},
    })
    with pytest.raises(EvidenceStorageError):
        await no_revision.archive_atomic(
            owner_id=OWNER,
            peer_id=CHAT,
            message_id=1,
            source_id=str(uuid.uuid4()),
            revision_id=str(uuid.uuid4()),
            content_hash="a" * 64,
            text="t",
            approval_reference="owner-confirmed",
            now=NOW,
            max_records=1,
            max_bytes=1,
        )

    bad_usage, _recorder = _durable_repository({
        "ai_evidence_archive_usage": {"unexpected": True},
    })
    with pytest.raises(EvidenceStorageError):
        await bad_usage.archive_usage(OWNER)
