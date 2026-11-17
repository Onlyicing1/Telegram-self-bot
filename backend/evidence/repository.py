"""Persistence boundary for Active Runs and the immutable Evidence Archive.

The repository is deliberately dumb: owner filters and row CRUD live here; all
policy (TTL bounds, quotas, approval, projection) lives in ``service.py``.  This
keeps the database implementation replaceable and makes the in-memory test
double exercise the same service behavior as Supabase.

Database time is authoritative.  ``database_now()`` calls the SQL function
created by the migration; the service compares persisted ``expires_at`` against
that value instead of a process-local clock.
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, replace
from datetime import datetime, timedelta, timezone
from typing import Any

from backend.evidence.models import (
    ArchiveUsage,
    EvidenceRef,
    EvidenceRevision,
    EvidenceRun,
    EvidenceSource,
    EvidenceStorageError,
)


def _utc(value: datetime | None = None) -> datetime:
    dt = value or datetime.now(timezone.utc)
    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def _parse_dt(value: Any) -> datetime:
    if isinstance(value, datetime):
        return _utc(value)
    text = str(value or "").strip()
    if text.endswith("Z"):
        text = text[:-1] + "+00:00"
    try:
        return _utc(datetime.fromisoformat(text))
    except (TypeError, ValueError) as exc:
        raise EvidenceStorageError("stored evidence timestamp is invalid") from exc


def _parse_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _run_from_row(row: dict[str, Any]) -> EvidenceRun:
    return EvidenceRun(
        id=str(row["id"]),
        owner_id=_parse_int(row.get("owner_id")),
        peer_id=_parse_int(row.get("peer_id")),
        peer_name=str(row.get("peer_name") or ""),
        ttl_days=_parse_int(row.get("ttl_days")),
        first_message_id=_parse_int(row.get("first_message_id")),
        last_message_id=_parse_int(row.get("last_message_id")),
        ref_count=_parse_int(row.get("ref_count")),
        text_bytes=_parse_int(row.get("text_bytes")),
        summary=str(row.get("summary") or ""),
        created_at=_parse_dt(row.get("created_at")),
        expires_at=_parse_dt(row.get("expires_at")),
    )


def _ref_from_row(row: dict[str, Any]) -> EvidenceRef:
    return EvidenceRef(
        run_id=str(row["run_id"]),
        owner_id=_parse_int(row.get("owner_id")),
        ordinal=_parse_int(row.get("ordinal")),
        message_id=_parse_int(row.get("message_id")),
        excerpt=str(row.get("excerpt") or ""),
        content_hash=str(row.get("content_hash") or ""),
        has_media=bool(row.get("has_media")),
        excerpt_truncated=bool(row.get("excerpt_truncated")),
        revision_id=str(row["revision_id"]) if row.get("revision_id") else None,
    )


def _source_from_row(row: dict[str, Any]) -> EvidenceSource:
    return EvidenceSource(
        id=str(row["id"]),
        owner_id=_parse_int(row.get("owner_id")),
        peer_id=_parse_int(row.get("peer_id")),
        message_id=_parse_int(row.get("message_id")),
        observed_state=str(row.get("observed_state") or "live"),
        first_seen_at=_parse_dt(row.get("first_seen_at")),
        last_observed_at=_parse_dt(row.get("last_observed_at")),
    )


def _revision_from_row(row: dict[str, Any]) -> EvidenceRevision:
    return EvidenceRevision(
        id=str(row["id"]),
        source_id=str(row["source_id"]),
        owner_id=_parse_int(row.get("owner_id")),
        revision_no=_parse_int(row.get("revision_no")),
        content_hash=str(row.get("content_hash") or ""),
        text=str(row.get("text") or ""),
        approval_reference=str(row.get("approval_reference") or ""),
        captured_at=_parse_dt(row.get("captured_at")),
    )


@dataclass
class _MemoryData:
    runs: dict[str, EvidenceRun]
    refs: dict[str, list[EvidenceRef]]
    sources: dict[str, EvidenceSource]
    revisions: dict[str, EvidenceRevision]
    revision_dependents: dict[str, int]
    source_dependents: dict[str, int]


class EvidenceRepository:
    """Supabase-backed repository, with a deterministic in-memory fallback."""

    def __init__(self, db: Any | None = None, *, clock=None) -> None:
        if db is None:
            from backend.db.client import get_db

            db = get_db()
        self._db = db
        self._clock = clock or (lambda: datetime.now(timezone.utc))
        self._memory = _MemoryData({}, {}, {}, {}, {}, {})

    @property
    def is_memory(self) -> bool:
        return self._db is None

    # ── time and low-level transport ────────────────────────────────────────

    async def database_now(self) -> datetime:
        if self._db is None:
            return _utc(self._clock())
        try:
            from backend.db.client import run_sync_db

            data = await run_sync_db(
                lambda: self._db.rpc("ai_evidence_now").execute().data
            )
            if isinstance(data, list):
                data = data[0] if data else None
            if isinstance(data, dict):
                data = next(iter(data.values()), None)
            return _parse_dt(data)
        except EvidenceStorageError:
            raise
        except Exception as exc:  # noqa: BLE001 — persistence boundary
            raise EvidenceStorageError(
                f"could not read authoritative database time: {type(exc).__name__}"
            ) from exc

    async def _call(self, fn):
        from backend.db.client import run_sync_db

        try:
            return await run_sync_db(fn)
        except Exception as exc:  # noqa: BLE001 — persistence boundary
            raise EvidenceStorageError(
                f"evidence storage operation failed: {type(exc).__name__}"
            ) from exc

    # ── active runs ─────────────────────────────────────────────────────────

    async def create_run(self, run: EvidenceRun) -> EvidenceRun:
        if self._db is None:
            now = _utc(self._clock())
            stored = replace(run, created_at=now, expires_at=now + timedelta(days=run.ttl_days))
            self._memory.runs[stored.id] = stored
            self._memory.refs[stored.id] = []
            return stored
        row = {
            "id": run.id,
            "owner_id": run.owner_id,
            "peer_id": run.peer_id,
            "peer_name": run.peer_name,
            "ttl_days": run.ttl_days,
            "first_message_id": run.first_message_id,
            "last_message_id": run.last_message_id,
            "ref_count": run.ref_count,
            "text_bytes": run.text_bytes,
            "summary": run.summary,
        }
        data = await self._call(
            lambda: self._db.table("ai_evidence_runs").insert(row).execute().data
        )
        if not data:
            raise EvidenceStorageError("active run insert returned no row")
        return _run_from_row(data[0])

    async def get_run(self, owner_id: int, run_id: str) -> EvidenceRun | None:
        if self._db is None:
            run = self._memory.runs.get(str(run_id))
            return run if run is not None and run.owner_id == int(owner_id) else None
        data = await self._call(
            lambda: self._db.table("ai_evidence_runs")
            .select("*")
            .eq("owner_id", int(owner_id))
            .eq("id", str(run_id))
            .limit(1)
            .execute()
            .data
        )
        return _run_from_row(data[0]) if data else None

    async def list_runs(
        self, owner_id: int, now: datetime, *, limit: int | None = None,
    ) -> list[EvidenceRun]:
        if self._db is None:
            runs = [
                run for run in self._memory.runs.values()
                if run.owner_id == int(owner_id) and run.expires_at > now
            ]
            runs.sort(key=lambda item: item.created_at, reverse=True)
            return runs[:limit] if limit is not None else runs
        query = (
            self._db.table("ai_evidence_runs")
            .select("*")
            .eq("owner_id", int(owner_id))
            .gt("expires_at", now.isoformat())
            .order("created_at", desc=True)
        )
        if limit is not None:
            query = query.limit(limit)
        data = await self._call(lambda: query.execute().data)
        return [_run_from_row(row) for row in data]

    async def count_active_runs(self, owner_id: int, now: datetime) -> int:
        if self._db is None:
            return sum(
                1 for run in self._memory.runs.values()
                if run.owner_id == int(owner_id) and run.expires_at > now
            )
        data = await self._call(
            lambda: self._db.table("ai_evidence_runs")
            .select("id")
            .eq("owner_id", int(owner_id))
            .gt("expires_at", now.isoformat())
            .limit(21)
            .execute()
            .data
        )
        return len(data)

    async def add_refs(self, run: EvidenceRun, refs: list[EvidenceRef]) -> None:
        if self._db is None:
            if run.id not in self._memory.runs:
                raise EvidenceStorageError("active run disappeared before refs were stored")
            for ref in refs:
                if ref.run_id != run.id or ref.owner_id != run.owner_id:
                    raise EvidenceStorageError("ref does not belong to this run")
            self._memory.refs[run.id].extend(refs)
            return
        rows = [
            {
                "run_id": ref.run_id,
                "owner_id": ref.owner_id,
                "ordinal": ref.ordinal,
                "message_id": ref.message_id,
                "excerpt": ref.excerpt,
                "content_hash": ref.content_hash,
                "has_media": ref.has_media,
                "excerpt_truncated": ref.excerpt_truncated,
                "revision_id": ref.revision_id,
            }
            for ref in refs
        ]
        if rows:
            await self._call(
                lambda: self._db.table("ai_evidence_refs").insert(rows).execute()
            )

    async def list_refs(self, owner_id: int, run_id: str) -> list[EvidenceRef]:
        if self._db is None:
            return [
                ref for ref in self._memory.refs.get(str(run_id), [])
                if ref.owner_id == int(owner_id)
            ]
        data = await self._call(
            lambda: self._db.table("ai_evidence_refs")
            .select("*")
            .eq("owner_id", int(owner_id))
            .eq("run_id", str(run_id))
            .order("ordinal")
            .execute()
            .data
        )
        return [_ref_from_row(row) for row in data]

    async def update_run_summary(
        self, owner_id: int, run_id: str, summary: str,
    ) -> EvidenceRun:
        if self._db is None:
            run = self._memory.runs.get(str(run_id))
            if run is None or run.owner_id != int(owner_id):
                raise EvidenceStorageError("active run disappeared before summary was stored")
            updated = replace(run, summary=summary)
            self._memory.runs[run.id] = updated
            return updated
        data = await self._call(
            lambda: self._db.table("ai_evidence_runs")
            .update({"summary": summary})
            .eq("owner_id", int(owner_id))
            .eq("id", str(run_id))
            .execute()
            .data
        )
        if not data:
            raise EvidenceStorageError("active run summary update returned no row")
        return _run_from_row(data[0])

    async def delete_run(self, owner_id: int, run_id: str) -> bool:
        if self._db is None:
            run = self._memory.runs.get(str(run_id))
            if run is None or run.owner_id != int(owner_id):
                return False
            self._memory.runs.pop(run.id, None)
            self._memory.refs.pop(run.id, None)
            return True
        data = await self._call(
            lambda: self._db.table("ai_evidence_runs")
            .delete()
            .eq("owner_id", int(owner_id))
            .eq("id", str(run_id))
            .execute()
            .data
        )
        return bool(data)

    async def delete_expired_runs(self, owner_id: int, now: datetime) -> int:
        """Opportunistic physical deletion, never a scheduler.

        Logical expiry has already denied access; this only reclaims rows while
        an existing authorized operation is already touching the store.
        """
        if self._db is None:
            expired = [
                run.id for run in self._memory.runs.values()
                if run.owner_id == int(owner_id) and run.expires_at <= now
            ]
            for run_id in expired:
                self._memory.runs.pop(run_id, None)
                self._memory.refs.pop(run_id, None)
            return len(expired)
        data = await self._call(
            lambda: self._db.table("ai_evidence_runs")
            .delete()
            .eq("owner_id", int(owner_id))
            .lte("expires_at", now.isoformat())
            .execute()
            .data
        )
        return len(data or [])

    # ── archive sources and immutable revisions ─────────────────────────────

    async def find_source(
        self, owner_id: int, peer_id: int, message_id: int,
    ) -> EvidenceSource | None:
        if self._db is None:
            for source in self._memory.sources.values():
                if (
                    source.owner_id == int(owner_id)
                    and source.peer_id == int(peer_id)
                    and source.message_id == int(message_id)
                ):
                    return source
            return None
        data = await self._call(
            lambda: self._db.table("ai_evidence_sources")
            .select("*")
            .eq("owner_id", int(owner_id))
            .eq("peer_id", int(peer_id))
            .eq("message_id", int(message_id))
            .limit(1)
            .execute()
            .data
        )
        return _source_from_row(data[0]) if data else None

    async def create_source(
        self, owner_id: int, peer_id: int, message_id: int, *, now: datetime,
    ) -> EvidenceSource:
        source = EvidenceSource(
            id=str(uuid.uuid4()),
            owner_id=int(owner_id),
            peer_id=int(peer_id),
            message_id=int(message_id),
            observed_state="live",
            first_seen_at=now,
            last_observed_at=now,
        )
        if self._db is None:
            self._memory.sources[source.id] = source
            return source
        row = {
            "id": source.id,
            "owner_id": source.owner_id,
            "peer_id": source.peer_id,
            "message_id": source.message_id,
            "observed_state": "live",
        }
        data = await self._call(
            lambda: self._db.table("ai_evidence_sources").insert(row).execute().data
        )
        if not data:
            raise EvidenceStorageError("archive source insert returned no row")
        return _source_from_row(data[0])

    async def get_source(self, owner_id: int, source_id: str) -> EvidenceSource | None:
        if self._db is None:
            source = self._memory.sources.get(str(source_id))
            return source if source is not None and source.owner_id == int(owner_id) else None
        data = await self._call(
            lambda: self._db.table("ai_evidence_sources")
            .select("*")
            .eq("owner_id", int(owner_id))
            .eq("id", str(source_id))
            .limit(1)
            .execute()
            .data
        )
        return _source_from_row(data[0]) if data else None

    async def list_sources(
        self, owner_id: int, *, limit: int = 20,
    ) -> list[EvidenceSource]:
        if self._db is None:
            sources = [
                item for item in self._memory.sources.values()
                if item.owner_id == int(owner_id)
            ]
            sources.sort(key=lambda item: item.last_observed_at, reverse=True)
            return sources[:limit]
        data = await self._call(
            lambda: self._db.table("ai_evidence_sources")
            .select("*")
            .eq("owner_id", int(owner_id))
            .order("last_observed_at", desc=True)
            .limit(limit)
            .execute()
            .data
        )
        return [_source_from_row(row) for row in data]

    async def find_revision_by_hash(
        self, owner_id: int, source_id: str, content_hash: str,
    ) -> EvidenceRevision | None:
        if self._db is None:
            for revision in self._memory.revisions.values():
                if (
                    revision.owner_id == int(owner_id)
                    and revision.source_id == str(source_id)
                    and revision.content_hash == content_hash
                ):
                    return revision
            return None
        data = await self._call(
            lambda: self._db.table("ai_evidence_revisions")
            .select("*")
            .eq("owner_id", int(owner_id))
            .eq("source_id", str(source_id))
            .eq("content_hash", content_hash)
            .limit(1)
            .execute()
            .data
        )
        return _revision_from_row(data[0]) if data else None

    async def list_revisions(
        self, owner_id: int, source_id: str,
    ) -> list[EvidenceRevision]:
        if self._db is None:
            revisions = [
                item for item in self._memory.revisions.values()
                if item.owner_id == int(owner_id) and item.source_id == str(source_id)
            ]
            revisions.sort(key=lambda item: item.revision_no)
            return revisions
        data = await self._call(
            lambda: self._db.table("ai_evidence_revisions")
            .select("*")
            .eq("owner_id", int(owner_id))
            .eq("source_id", str(source_id))
            .order("revision_no")
            .execute()
            .data
        )
        return [_revision_from_row(row) for row in data]

    async def add_revision(
        self,
        *,
        owner_id: int,
        source_id: str,
        revision_no: int,
        content_hash: str,
        text: str,
        approval_reference: str,
        now: datetime,
    ) -> EvidenceRevision:
        revision = EvidenceRevision(
            id=str(uuid.uuid4()),
            source_id=str(source_id),
            owner_id=int(owner_id),
            revision_no=int(revision_no),
            content_hash=content_hash,
            text=text,
            approval_reference=approval_reference,
            captured_at=now,
        )
        if self._db is None:
            for existing in self._memory.revisions.values():
                if (
                    existing.owner_id == revision.owner_id
                    and existing.source_id == revision.source_id
                    and (
                        existing.content_hash == revision.content_hash
                        or existing.revision_no == revision.revision_no
                    )
                ):
                    raise EvidenceStorageError("archive revision uniqueness conflict")
            self._memory.revisions[revision.id] = revision
            return revision
        row = {
            "id": revision.id,
            "source_id": revision.source_id,
            "owner_id": revision.owner_id,
            "revision_no": revision.revision_no,
            "content_hash": revision.content_hash,
            "text": revision.text,
            "approval_reference": revision.approval_reference,
        }
        data = await self._call(
            lambda: self._db.table("ai_evidence_revisions").insert(row).execute().data
        )
        if not data:
            raise EvidenceStorageError("archive revision insert returned no row")
        return _revision_from_row(data[0])

    async def get_revision(
        self, owner_id: int, revision_id: str,
    ) -> EvidenceRevision | None:
        if self._db is None:
            revision = self._memory.revisions.get(str(revision_id))
            return revision if revision is not None and revision.owner_id == int(owner_id) else None
        data = await self._call(
            lambda: self._db.table("ai_evidence_revisions")
            .select("*")
            .eq("owner_id", int(owner_id))
            .eq("id", str(revision_id))
            .limit(1)
            .execute()
            .data
        )
        return _revision_from_row(data[0]) if data else None

    async def update_source_observation(
        self, owner_id: int, source_id: str, state: str, *, now: datetime,
    ) -> None:
        if self._db is None:
            source = self._memory.sources.get(str(source_id))
            if source is not None and source.owner_id == int(owner_id):
                self._memory.sources[source.id] = replace(
                    source, observed_state=state, last_observed_at=now,
                )
            return
        await self._call(
            lambda: self._db.table("ai_evidence_sources")
            .update({"observed_state": state, "last_observed_at": now.isoformat()})
            .eq("owner_id", int(owner_id))
            .eq("id", str(source_id))
            .execute()
        )

    async def archive_usage(self, owner_id: int) -> ArchiveUsage:
        if self._db is None:
            revisions = [
                item for item in self._memory.revisions.values()
                if item.owner_id == int(owner_id)
            ]
            return ArchiveUsage(
                records=len(revisions),
                bytes=sum(item.text_bytes for item in revisions),
            )
        data = await self._call(
            lambda: self._db.rpc(
                "ai_evidence_archive_usage", {"p_owner_id": int(owner_id)}
            ).execute().data
        )
        row = data[0] if isinstance(data, list) and data else (data or {})
        if isinstance(row, dict):
            return ArchiveUsage(
                records=_parse_int(row.get("record_count")),
                bytes=_parse_int(row.get("byte_count")),
            )
        return ArchiveUsage(records=0, bytes=0)

    async def delete_source(self, owner_id: int, source_id: str) -> bool:
        if self._db is None:
            source = self._memory.sources.get(str(source_id))
            if source is None or source.owner_id != int(owner_id):
                return False
            self._memory.sources.pop(source.id, None)
            revision_ids = [
                item.id for item in self._memory.revisions.values()
                if item.owner_id == int(owner_id) and item.source_id == source.id
            ]
            for revision_id in revision_ids:
                self._memory.revisions.pop(revision_id, None)
                self._memory.revision_dependents.pop(revision_id, None)
            self._memory.source_dependents.pop(source.id, None)
            return True
        data = await self._call(
            lambda: self._db.table("ai_evidence_sources")
            .delete()
            .eq("owner_id", int(owner_id))
            .eq("id", str(source_id))
            .execute()
            .data
        )
        return bool(data)

    async def delete_revision(self, owner_id: int, revision_id: str) -> bool:
        if self._db is None:
            revision = self._memory.revisions.get(str(revision_id))
            if revision is None or revision.owner_id != int(owner_id):
                return False
            self._memory.revisions.pop(revision.id, None)
            self._memory.revision_dependents.pop(revision.id, None)
            return True
        data = await self._call(
            lambda: self._db.table("ai_evidence_revisions")
            .delete()
            .eq("owner_id", int(owner_id))
            .eq("id", str(revision_id))
            .execute()
            .data
        )
        return bool(data)

    async def count_revision_dependents(self, owner_id: int, revision_id: str) -> int:
        # G4-C/G4-D dependent records do not exist in F1.  The method is the
        # stable extension point; tests can inject a non-zero count on the
        # in-memory repository to pin the deletion contract.
        if self._db is None:
            revision = self._memory.revisions.get(str(revision_id))
            if revision is None or revision.owner_id != int(owner_id):
                return 0
            return int(self._memory.revision_dependents.get(str(revision_id), 0))
        return 0

    async def count_source_dependents(self, owner_id: int, source_id: str) -> int:
        if self._db is None:
            source = self._memory.sources.get(str(source_id))
            if source is None or source.owner_id != int(owner_id):
                return 0
            return int(self._memory.source_dependents.get(str(source_id), 0))
        return 0

    # ── test seams (in-memory only) ─────────────────────────────────────────

    def set_revision_dependents_for_tests(self, revision_id: str, count: int) -> None:
        if self._db is not None:
            raise RuntimeError("test seams are only available on the memory repository")
        self._memory.revision_dependents[str(revision_id)] = max(0, int(count))

    def set_source_dependents_for_tests(self, source_id: str, count: int) -> None:
        if self._db is not None:
            raise RuntimeError("test seams are only available on the memory repository")
        self._memory.source_dependents[str(source_id)] = max(0, int(count))


def in_memory_repository(clock=None) -> EvidenceRepository:
    """Construct an explicitly memory-backed repository (test/reporting)."""
    return EvidenceRepository(db=None, clock=clock)
