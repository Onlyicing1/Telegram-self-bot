"""Typed Evidence records and honest failure types.

The model deliberately separates active-run snapshots from archive revisions:

* ``EvidenceRun`` + ``EvidenceRef`` are temporary, owner-scoped snapshots.
* ``EvidenceSource`` + ``EvidenceRevision`` are owner-approved, append-only
  archive records.  A reference never points at a mutable "latest" row.

No dataclass here contains a provider URL, a Telegram link, or a provider
payload.  Links are constructed only when an owner-facing surface renders a
record, from the locally validated peer/message ids stored on the run/source.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from typing import Any


class EvidenceError(Exception):
    """Base class for an explicit, user-reportable Evidence failure."""

    code = "evidence_error"


class EvidenceValidationError(EvidenceError):
    code = "validation"


class EvidenceLimitError(EvidenceValidationError):
    code = "limit"


class EvidenceQuotaError(EvidenceError):
    code = "quota"


class EvidenceNotFound(EvidenceError):
    code = "not_found"


class EvidenceExpired(EvidenceError):
    code = "expired"


class EvidenceEmpty(EvidenceError):
    code = "empty"


class EvidenceConfirmationRequired(EvidenceError):
    code = "confirmation_required"


class EvidenceDependencyBlocked(EvidenceError):
    code = "dependency_blocked"


class EvidenceStructuredOutputError(EvidenceError):
    code = "structured_output"


class EvidenceStorageError(EvidenceError):
    code = "storage"


class EvidenceProviderError(EvidenceError):
    code = "provider"


@dataclass(frozen=True)
class EvidenceRun:
    id: str
    owner_id: int
    peer_id: int
    peer_name: str
    ttl_days: int
    first_message_id: int
    last_message_id: int
    ref_count: int
    text_bytes: int
    summary: str
    created_at: datetime
    expires_at: datetime

    @property
    def summary_bytes(self) -> int:
        return len(self.summary.encode("utf-8"))


@dataclass(frozen=True)
class EvidenceRef:
    run_id: str
    owner_id: int
    ordinal: int
    message_id: int
    excerpt: str
    content_hash: str
    has_media: bool
    excerpt_truncated: bool = False
    revision_id: str | None = None

    @property
    def key(self) -> str:
        return f"E{self.ordinal}"

    @property
    def excerpt_bytes(self) -> int:
        return len(self.excerpt.encode("utf-8"))


@dataclass(frozen=True)
class EvidenceRunBundle:
    run: EvidenceRun
    refs: tuple[EvidenceRef, ...]


@dataclass(frozen=True)
class EvidenceSource:
    id: str
    owner_id: int
    peer_id: int
    message_id: int
    observed_state: str
    first_seen_at: datetime
    last_observed_at: datetime


@dataclass(frozen=True)
class EvidenceRevision:
    id: str
    source_id: str
    owner_id: int
    revision_no: int
    content_hash: str
    text: str
    approval_reference: str
    captured_at: datetime

    @property
    def text_bytes(self) -> int:
        return len(self.text.encode("utf-8"))


@dataclass(frozen=True)
class EvidenceCitation:
    key: str
    ordinal: int
    message_id: int
    link: str
    excerpt: str


@dataclass(frozen=True)
class ArchiveSuggestion:
    key: str
    ordinal: int
    excerpt: str
    reason: str
    already_archived: bool = False


@dataclass(frozen=True)
class ArchiveResult:
    status: str
    source_id: str
    revision_id: str
    revision_no: int
    content_hash: str
    reason: str
    citation: EvidenceCitation | None = None


@dataclass(frozen=True)
class CitedSummaryResult:
    run_id: str
    summary: str
    citations: tuple[EvidenceCitation, ...]
    data: dict[str, Any] = field(default_factory=dict)

    @property
    def citation_keys(self) -> tuple[str, ...]:
        return tuple(citation.key for citation in self.citations)


@dataclass(frozen=True)
class ArchiveUsage:
    records: int
    bytes: int
