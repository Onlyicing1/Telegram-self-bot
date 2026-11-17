"""G4-A/F1 — Single-Source Evidence Foundation behavioral tests.

These are unit/mocked-repository tests.  No live Supabase, Telegram, Render or
provider is used.  The fakes exercise the real service, history retrieval facade,
strict JSON parser, local link builder and tool boundary.
"""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from typing import Any

import pytest

from backend.ai.providers.base.contract import ProviderResponse
from backend.ai.tools.base import PermissionLevel, requires_owner_confirmation
from backend.ai.tools.context import ToolContext
from backend.ai.tools.evidence import ArchiveEvidenceTool, CitedSummaryTool
from backend.evidence import constants
from backend.evidence.links import telegram_message_link
from backend.evidence.models import (
    EvidenceConfirmationRequired,
    EvidenceDependencyBlocked,
    EvidenceExpired,
    EvidenceLimitError,
    EvidenceNotFound,
    EvidenceQuotaError,
    EvidenceStructuredOutputError,
    EvidenceValidationError,
)
from backend.evidence.repository import EvidenceRepository
from backend.evidence.service import (
    EvidenceService,
    build_refs,
    reset_evidence_service_for_tests,
)
from backend.telegram_api.peers import PeerType

OWNER = 7770001
OTHER_OWNER = 8880002
CHAT = -100555000
NOW = datetime(2026, 11, 16, 12, 0, tzinfo=timezone.utc)


class _Clock:
    def __init__(self, now: datetime = NOW) -> None:
        self.now = now

    def __call__(self) -> datetime:
        return self.now

    def advance(self, **kwargs: Any) -> None:
        self.now = self.now + timedelta(**kwargs)


class _FakeMessage:
    def __init__(self, mid: int, text: str = "", *, media: bool = False) -> None:
        self.id = mid
        self.chat_id = CHAT
        self.sender_id = OWNER
        self.text = text
        self.date = NOW
        self.media = object() if media else None
        self.reply_to = None
        self.out = True


class _FakeSource:
    def __init__(self, messages: list[_FakeMessage]) -> None:
        self.messages = list(messages)
        self.calls: list[dict[str, Any]] = []
        self.entity_calls: list[Any] = []

    async def get_entity(self, chat_id: Any):
        from telethon.tl import types

        self.entity_calls.append(chat_id)
        return types.User(id=abs(int(chat_id)) or 1, first_name="Owner")

    def iter_messages(self, chat_id: Any, **kwargs: Any):
        self.calls.append({"chat_id": chat_id, **kwargs})
        limit = kwargs.get("limit")
        min_id = kwargs.get("min_id")
        max_id = kwargs.get("max_id")
        selected = [
            m for m in self.messages
            if (max_id is None or m.id < max_id) and (min_id is None or m.id > min_id)
        ]
        selected.sort(key=lambda m: m.id, reverse=True)
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
            text=self.text,
            provider_name="fake",
            success=self.success,
        )


def _repo(clock: _Clock | None = None) -> EvidenceRepository:
    return EvidenceRepository(db=None, clock=clock or _Clock())


def _service(clock: _Clock | None = None) -> tuple[EvidenceService, EvidenceRepository, _Clock]:
    clock = clock or _Clock()
    repo = _repo(clock)
    return EvidenceService(repo), repo, clock


def _messages(count: int, text: str = "evidence text") -> list[_FakeMessage]:
    return [_FakeMessage(index, f"{text} {index}") for index in range(1, count + 1)]


async def _make_run(
    service: EvidenceService,
    *,
    owner: int = OWNER,
    messages: list[_FakeMessage] | None = None,
    text: str = "evidence text",
    ttl: int = constants.DEFAULT_TTL_DAYS,
    peer_id: int = CHAT,
):
    return await service.create_run(
        owner_id=owner,
        peer_id=peer_id,
        peer_name="Test chat",
        ttl_days=ttl,
        messages=messages or _messages(2, text),
    )


# ── owner isolation and TTL ─────────────────────────────────────────────────


@pytest.mark.asyncio
async def test_owner_isolation_and_no_cross_owner_archive_access():
    service, _repo_obj, _clock_obj = _service()
    bundle = await _make_run(service, text="owner one")
    with pytest.raises(EvidenceNotFound):
        await service.get_run(OTHER_OWNER, bundle.run.id)
    assert await service.list_active_runs(OTHER_OWNER) == []
    with pytest.raises(EvidenceNotFound):
        await service.archive_ref(OTHER_OWNER, bundle.run.id, "E1", approved=True)
    assert (await service.archive_usage(OTHER_OWNER)).records == 0


@pytest.mark.asyncio
async def test_ttl_defaults_to_seven_days_and_accepts_only_one_to_thirty():
    service, _repo_obj, clock = _service()
    bundle = await _make_run(service, ttl=constants.DEFAULT_TTL_DAYS)
    assert bundle.run.ttl_days == 7
    assert bundle.run.expires_at == bundle.run.created_at + timedelta(days=7)
    one = await _make_run(service, ttl=1)
    thirty = await _make_run(service, ttl=30)
    assert one.run.expires_at == one.run.created_at + timedelta(days=1)
    assert thirty.run.expires_at == thirty.run.created_at + timedelta(days=30)
    for invalid in (0, -1, 31, 100):
        with pytest.raises(EvidenceValidationError):
            await _make_run(service, ttl=invalid)


@pytest.mark.asyncio
async def test_database_time_expiry_denies_access_and_never_reconstructs():
    service, _repo_obj, clock = _service()
    source = _FakeSource(_messages(3))
    manager = _FakeManager('{"summary":"ok","citations":["E1"]}')
    result = await service.run_cited_summary(
        source,
        owner_id=OWNER,
        peer_id=CHAT,
        peer_name="Test chat",
        count=3,
        ttl_days=1,
        provider_manager=manager,
        peer_type=PeerType.PRIVATE_USER,
    )
    clock.advance(days=1, seconds=1)
    with pytest.raises(EvidenceExpired):
        await service.get_run(OWNER, result.run_id)
    # A second access is the same honest failure: there is no Telegram, archive
    # or conversation reconstruction path.
    with pytest.raises(EvidenceExpired):
        await service.get_run(OWNER, result.run_id)
    assert await service.list_active_runs(OWNER) == []
    assert not source.entity_calls


# ── active-run bounds and opportunistic deletion ───────────────────────────


@pytest.mark.asyncio
async def test_excerpt_and_reference_bounds_are_enforced():
    service, _repo_obj, _clock_obj = _service()
    long_text = "x" * (constants.MAX_EXCERPT_CHARS + 500)
    bundle = await _make_run(service, messages=[_FakeMessage(1, long_text)])
    assert len(bundle.refs[0].excerpt) == constants.MAX_EXCERPT_CHARS
    assert bundle.refs[0].excerpt_truncated is True

    with pytest.raises(EvidenceLimitError):
        build_refs(
            _messages(constants.MAX_REFS_PER_RUN + 1),
            run_id="run",
            owner_id=OWNER,
        )

    huge = [_FakeMessage(i, "x" * constants.MAX_EXCERPT_CHARS) for i in range(1, 34)]
    with pytest.raises(EvidenceLimitError):
        build_refs(huge, run_id="run", owner_id=OWNER)


@pytest.mark.asyncio
async def test_active_run_quota_rejects_and_expired_cleanup_never_evicts_valid_runs():
    service, repo, clock = _service()
    for index in range(constants.MAX_ACTIVE_RUNS_PER_OWNER):
        await _make_run(service, messages=[_FakeMessage(index + 1)])
    with pytest.raises(EvidenceQuotaError):
        await _make_run(service, messages=[_FakeMessage(100)])
    assert await service.active_run_count(OWNER) == constants.MAX_ACTIVE_RUNS_PER_OWNER

    # Expired runs are deleted opportunistically by the next authorized
    # admission; no scheduler or cleanup loop is involved.
    clock.advance(days=constants.DEFAULT_TTL_DAYS + 1)
    replacement = await _make_run(service, messages=[_FakeMessage(101)])
    assert replacement.run.ttl_days == 7
    assert await service.active_run_count(OWNER) == 1
    assert [run.id for run in await service.list_active_runs(OWNER)] == [replacement.run.id]


# ── archive immutability, approval and quotas ──────────────────────────────


@pytest.mark.asyncio
async def test_archive_requires_approval_and_same_hash_is_idempotent():
    service, _repo_obj, _clock_obj = _service()
    bundle = await _make_run(service, messages=[_FakeMessage(1, "archive me")])
    with pytest.raises(EvidenceConfirmationRequired):
        await service.archive_ref(OWNER, bundle.run.id, "E1")
    assert (await service.archive_usage(OWNER)).records == 0

    first = await service.archive_ref(OWNER, bundle.run.id, "E1", approved=True)
    second = await service.archive_ref(OWNER, bundle.run.id, "E1", approved=True)
    assert first.status == "created"
    assert second.status == "unchanged"
    assert first.revision_id == second.revision_id
    assert (await service.archive_usage(OWNER)).records == 1
    revisions = await service.list_revisions(OWNER, first.source_id)
    assert len(revisions) == 1
    assert revisions[0].text == "archive me"


@pytest.mark.asyncio
async def test_changed_hash_creates_a_new_revision_and_never_overwrites():
    service, _repo_obj, _clock_obj = _service()
    first_run = await _make_run(service, messages=[_FakeMessage(7, "old text")])
    first = await service.archive_ref(OWNER, first_run.run.id, "E1", approved=True)

    changed_run = await _make_run(service, messages=[_FakeMessage(7, "new text")])
    with pytest.raises(EvidenceConfirmationRequired):
        await service.archive_ref(OWNER, changed_run.run.id, "E1")
    second = await service.archive_ref(OWNER, changed_run.run.id, "E1", approved=True)

    revisions = await service.list_revisions(OWNER, first.source_id)
    assert [revision.text for revision in revisions] == ["old text", "new text"]
    assert revisions[0].id != second.revision_id
    assert revisions[0].text == "old text"


@pytest.mark.asyncio
async def test_archive_record_and_byte_quota_fail_without_eviction(monkeypatch):
    service, _repo_obj, _clock_obj = _service()
    run_one = await _make_run(service, messages=[_FakeMessage(1, "one")])
    first = await service.archive_ref(OWNER, run_one.run.id, "E1", approved=True)

    monkeypatch.setattr(constants, "MAX_ARCHIVE_RECORDS_PER_OWNER", 1)
    run_two = await _make_run(service, messages=[_FakeMessage(2, "two")])
    with pytest.raises(EvidenceQuotaError):
        await service.archive_ref(OWNER, run_two.run.id, "E1", approved=True)
    assert (await service.archive_usage(OWNER)).records == 1
    assert len(await service.list_revisions(OWNER, first.source_id)) == 1

    monkeypatch.setattr(constants, "MAX_ARCHIVE_RECORDS_PER_OWNER", 5000)
    monkeypatch.setattr(constants, "MAX_ARCHIVE_BYTES_PER_OWNER", 2)
    run_three = await _make_run(service, messages=[_FakeMessage(3, "three")])
    with pytest.raises(EvidenceQuotaError):
        await service.archive_ref(OWNER, run_three.run.id, "E1", approved=True)


@pytest.mark.asyncio
async def test_archive_suggestions_are_bounded_to_reviewed_refs_and_explain_why():
    service, _repo_obj, _clock_obj = _service()
    bundle = await _make_run(service, messages=_messages(15))
    suggestions = await service.archive_candidates(OWNER, bundle.run.id)
    assert len(suggestions) == constants.MAX_ARCHIVE_SUGGESTIONS
    assert all(item.reason for item in suggestions)
    assert [item.key for item in suggestions] == [f"E{i}" for i in range(1, 11)]


# ── deletion and active snapshot retention ──────────────────────────────────


@pytest.mark.asyncio
async def test_dependency_blocked_deletion_needs_separate_cascade_confirmation():
    service, repo, _clock_obj = _service()
    bundle = await _make_run(service, messages=[_FakeMessage(1, "keep")])
    archived = await service.archive_ref(OWNER, bundle.run.id, "E1", approved=True)
    repo.set_revision_dependents_for_tests(archived.revision_id, 2)

    with pytest.raises(EvidenceConfirmationRequired):
        await service.delete_revision(OWNER, archived.revision_id)
    with pytest.raises(EvidenceDependencyBlocked):
        await service.delete_revision(OWNER, archived.revision_id, confirm=True)
    assert await service.delete_revision(
        OWNER, archived.revision_id, confirm=True, cascade_confirmed=True,
    ) is True


@pytest.mark.asyncio
async def test_archive_deletion_does_not_delete_active_run_snapshots():
    service, _repo_obj, _clock_obj = _service()
    bundle = await _make_run(service, messages=[_FakeMessage(1, "snapshot")])
    archived = await service.archive_ref(OWNER, bundle.run.id, "E1", approved=True)
    await service.delete_source(OWNER, archived.source_id, confirm=True)
    assert await service.list_revisions(OWNER, archived.source_id) == []
    surviving = await service.get_run(OWNER, bundle.run.id)
    assert surviving.refs[0].excerpt == "snapshot"


# ── cited summary projection, strict JSON and links ─────────────────────────


@pytest.mark.asyncio
async def test_provider_receives_only_opaque_keys_and_bounded_excerpts():
    service, _repo_obj, _clock_obj = _service()
    source = _FakeSource([
        _FakeMessage(101, "secret-free excerpt one"),
        _FakeMessage(102, "secret-free excerpt two"),
    ])
    manager = _FakeManager('{"summary":"A short summary.","citations":["E1","E2"]}')
    result = await service.run_cited_summary(
        source,
        owner_id=OWNER,
        peer_id=CHAT,
        peer_name="Private chat name",
        count=2,
        ttl_days=3,
        instruction="focus",
        language="en",
        provider_manager=manager,
        current_message_id=103,
        peer_type=PeerType.PRIVATE_USER,
    )
    prompt = "\n".join(
        str(message["content"]) for message in manager.calls[0]["messages"]
    )
    assert "[E1]" in prompt and "[E2]" in prompt
    assert "secret-free excerpt one" in prompt
    for forbidden in (
        str(CHAT), "101", "102", str(OWNER),
        "t.me/", "tg://", "Private chat name",
    ):
        assert forbidden not in prompt
    assert result.summary == "A short summary."
    assert [citation.key for citation in result.citations] == ["E1", "E2"]
    assert "tg://" in result.citations[0].link
    assert source.calls[0]["chat_id"] == CHAT
    assert source.calls[0]["max_id"] == 103


@pytest.mark.asyncio
async def test_invalid_structured_output_fails_closed_and_cleans_up_the_run():
    service, _repo_obj, _clock_obj = _service()
    source = _FakeSource(_messages(2))
    for bad in (
        "not json",
        '```json\n{"summary":"x","citations":["E1"]}\n```',
        '{"summary":"x","citations":[]}',
        '{"summary":"x","citations":["E9"]}',
        '{"summary":"x","citations":["not-a-key"]}',
        '{"summary":"x","citations":["E1"],"extra":1}',
    ):
        with pytest.raises(EvidenceStructuredOutputError):
            await service.run_cited_summary(
                source,
                owner_id=OWNER,
                peer_id=CHAT,
                peer_name="Test chat",
                count=2,
                provider_manager=_FakeManager(bad),
                peer_type=PeerType.PRIVATE_USER,
            )
    assert await service.list_active_runs(OWNER) == []


@pytest.mark.asyncio
async def test_provider_failure_is_honest_and_leaves_no_partial_run():
    service, _repo_obj, _clock_obj = _service()
    source = _FakeSource(_messages(2))
    with pytest.raises(Exception) as raised:
        await service.run_cited_summary(
            source,
            owner_id=OWNER,
            peer_id=CHAT,
            peer_name="Test chat",
            count=2,
            provider_manager=_FakeManager("", success=False),
            peer_type=PeerType.PRIVATE_USER,
        )
    assert "provider" in str(raised.value).lower()
    assert await service.list_active_runs(OWNER) == []


def test_links_are_constructed_locally_for_users_channels_and_basic_groups():
    assert telegram_message_link(123, 45) == (
        "tg://openmessage?user_id=123&message_id=45"
    )
    assert telegram_message_link(-1001234567890, 45) == (
        "https://t.me/c/1234567890/45"
    )
    assert telegram_message_link(-555, 45) == (
        "tg://openmessage?chat_id=-555&message_id=45"
    )


# ── generic tool boundary and confirmation gate ────────────────────────────


@pytest.mark.asyncio
async def test_cited_summary_tool_result_has_no_evidence_envelope():
    service, repo, _clock_obj = _service()
    reset_evidence_service_for_tests(repo)
    source = _FakeSource(_messages(2, "tool excerpt"))
    manager = _FakeManager('{"summary":"Tool summary.","citations":["E1"]}')
    context = ToolContext(
        telegram=source,
        owner_id=OWNER,
        tz_str="UTC",
        extra={
            "chat_id": CHAT,
            "chat_name": "Tool chat",
            "provider_manager": manager,
            "global_language": "en",
        },
    )
    result = await CitedSummaryTool(context).execute(context, {"count": 2, "ttl_days": 7})
    assert result.success is True
    assert result.message == "Tool summary."
    assert set(result.data) == {
        "operation", "run_id", "requested", "processed", "truncated",
        "refs", "citations", "ttl_days",
    }
    serialized = repr(result.data) + result.message
    assert "message_id" not in serialized
    assert "content_hash" not in serialized
    for forbidden in (str(CHAT), "t.me/", "tg://", "Tool chat"):
        assert forbidden not in serialized


def test_archive_tool_uses_the_existing_confirmation_gate():
    context = ToolContext(telegram=None, owner_id=OWNER, tz_str="UTC", extra={})
    tool = ArchiveEvidenceTool(context)
    assert tool.permission_level is PermissionLevel.CONFIRMATION_REQUIRED
    assert requires_owner_confirmation(tool) is True
    assert tool.required_arguments == ("run_id", "ref")


def test_evidence_panel_is_registered_under_history():
    from backend.bot.handlers import evidence
    from backend.helper.panel_registry import get_panel_parent

    evidence.register(owner_id=OWNER)
    assert get_panel_parent("evidence") == "history"
