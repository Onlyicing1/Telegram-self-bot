"""G4-A/F1 Evidence service — the sole owner of Evidence policy execution.

Responsibilities:

* build bounded Active Run snapshots from the existing History retrieval service;
* enforce database-time TTL, run/ref/text limits and owner capacity;
* project only opaque keys + bounded excerpts to the provider;
* validate strict cited-summary JSON and fail closed;
* create immutable archive revisions only after explicit owner approval;
* enforce archive quotas and dependent-aware deletion.

The service never sends a Telegram message, never creates a scheduler or
executor, and never puts a link, message id or hash into a provider prompt.
"""
from __future__ import annotations

import asyncio
import hashlib
import json
import logging
import re
import unicodedata
import uuid
from datetime import timedelta
from typing import Any

from backend.evidence import constants
from backend.evidence.links import telegram_message_link
from backend.evidence.models import (
    ArchiveResult,
    ArchiveSuggestion,
    ArchiveUsage,
    CitedSummaryResult,
    EvidenceCitation,
    EvidenceConfirmationRequired,
    EvidenceDependencyBlocked,
    EvidenceEmpty,
    EvidenceExpired,
    EvidenceLimitError,
    EvidenceNotFound,
    EvidenceProviderError,
    EvidenceQuotaError,
    EvidenceRef,
    EvidenceRevision,
    EvidenceRun,
    EvidenceRunBundle,
    EvidenceSource,
    EvidenceStorageError,
    EvidenceStructuredOutputError,
    EvidenceValidationError,
)
from backend.evidence.repository import EvidenceRepository
from backend.services import history_ai_service, history_policy, history_service

logger = logging.getLogger(__name__)

_KEY_RE = re.compile(r"^E([1-9][0-9]?)$")
_CITATION_FIELDS = frozenset({"summary", "citations"})
_SYSTEM_PROMPT = (
    "You summarize only the numbered evidence excerpts supplied by the "
    "application. Do not use outside knowledge. Do not output URLs, message "
    "ids, user ids, chat ids, hashes, or any metadata. Return STRICT JSON only, "
    "with exactly two keys: {\"summary\": string, \"citations\": [\"E1\", ...]}. "
    "Use only citation keys that appear in the supplied excerpts. Cite the "
    "evidence used by the summary. If the excerpts cannot support a useful "
    "summary, say that plainly and cite the closest supporting excerpt. Never "
    "wrap the JSON in markdown fences or add prose outside it."
)


def normalize_excerpt(text: Any) -> tuple[str, bool]:
    """Return a bounded, Unicode-normalized excerpt and whether it was cut.

    The snapshot is intentionally bounded at ``MAX_EXCERPT_CHARS``.  A cut is
    recorded on the ref (``excerpt_truncated``), never silently presented as the
    complete source text.
    """
    value = unicodedata.normalize("NFC", str(text or ""))
    value = value.replace("\r\n", "\n").replace("\r", "\n").strip()
    if len(value) <= constants.MAX_EXCERPT_CHARS:
        return value, False
    return value[: constants.MAX_EXCERPT_CHARS], True


def content_hash(excerpt: str) -> str:
    return hashlib.sha256(str(excerpt).encode("utf-8")).hexdigest()


def build_refs(
    messages: Any,
    *,
    run_id: str,
    owner_id: int,
) -> tuple[EvidenceRef, ...]:
    """Build bounded refs from History messages, enforcing F1 limits.

    This function refuses an over-bound request rather than silently dropping
    evidence.  The Cited Summary caller chooses the requested window; a
    follow-up surface will enforce its own smaller bound.
    """
    items = tuple(messages or ())
    if not items:
        raise EvidenceEmpty("no eligible messages were available")
    if len(items) > constants.MAX_REFS_PER_RUN:
        raise EvidenceLimitError(
            f"one evidence run accepts at most {constants.MAX_REFS_PER_RUN} references"
        )
    refs: list[EvidenceRef] = []
    total_bytes = 0
    for ordinal, message in enumerate(items, start=1):
        # HistoryMessage exposes ``message_id``; lightweight test/adaptor
        # records may expose the raw Telegram ``id``.  Accept both, but never
        # invent an id.
        message_id = int(
            getattr(message, "message_id", 0)
            or getattr(message, "id", 0)
            or 0
        )
        if message_id <= 0:
            raise EvidenceValidationError("a history message has no usable message id")
        excerpt, truncated = normalize_excerpt(getattr(message, "text", ""))
        total_bytes += len(excerpt.encode("utf-8"))
        if total_bytes > constants.MAX_TEXT_BYTES_PER_RUN:
            raise EvidenceLimitError(
                "the requested evidence exceeds the per-run normalized text limit"
            )
        refs.append(
            EvidenceRef(
                run_id=str(run_id),
                owner_id=int(owner_id),
                ordinal=ordinal,
                message_id=message_id,
                excerpt=excerpt,
                content_hash=content_hash(excerpt),
                has_media=bool(getattr(message, "has_media", False)),
                excerpt_truncated=truncated,
            )
        )
    return tuple(refs)


def _parse_key(key: Any) -> int:
    match = _KEY_RE.match(str(key or "").strip())
    if not match:
        raise EvidenceValidationError("evidence reference key must be E1…E50")
    ordinal = int(match.group(1))
    if ordinal > constants.MAX_REFS_PER_RUN:
        raise EvidenceValidationError("evidence reference key is out of range")
    return ordinal


def _validate_ttl(value: Any) -> int:
    try:
        days = int(value)
    except (TypeError, ValueError) as exc:
        raise EvidenceValidationError("TTL must be a whole number of days") from exc
    if days < constants.MIN_TTL_DAYS or days > constants.MAX_TTL_DAYS:
        raise EvidenceValidationError(
            f"TTL must be between {constants.MIN_TTL_DAYS} and "
            f"{constants.MAX_TTL_DAYS} days"
        )
    return days


def _clean_peer_name(value: Any) -> str:
    text = " ".join(str(value or "").split())
    return text[: constants.MAX_PEER_NAME_CHARS]


def _citation(run: EvidenceRun, ref: EvidenceRef) -> EvidenceCitation:
    return EvidenceCitation(
        key=ref.key,
        ordinal=ref.ordinal,
        message_id=ref.message_id,
        link=telegram_message_link(run.peer_id, ref.message_id),
        excerpt=ref.excerpt,
    )


def _extract_json(text: str) -> dict[str, Any]:
    """Strict JSON only: no fence stripping, no prose extraction, no fallback."""
    try:
        value = json.loads(text)
    except (TypeError, ValueError) as exc:
        raise EvidenceStructuredOutputError(
            "cited summary output was not strict JSON"
        ) from exc
    if not isinstance(value, dict):
        raise EvidenceStructuredOutputError("cited summary output must be a JSON object")
    if set(value) != _CITATION_FIELDS:
        raise EvidenceStructuredOutputError(
            "cited summary output must contain exactly summary and citations"
        )
    summary = value.get("summary")
    citations = value.get("citations")
    if not isinstance(summary, str) or not summary.strip():
        raise EvidenceStructuredOutputError("cited summary output has no summary")
    if len(summary) > constants.MAX_SUMMARY_CHARS:
        raise EvidenceStructuredOutputError("cited summary output is too long")
    if len(summary.encode("utf-8")) > constants.MAX_SUMMARY_BYTES:
        raise EvidenceStructuredOutputError("cited summary output exceeds the byte limit")
    if not isinstance(citations, list) or not citations:
        raise EvidenceStructuredOutputError("cited summary output has no citations")
    keys: list[str] = []
    for item in citations:
        if not isinstance(item, str):
            raise EvidenceStructuredOutputError("citation keys must be strings")
        key = item.strip()
        if not re.match(r"^E[1-9][0-9]?$", key):
            raise EvidenceStructuredOutputError("citation key is not an opaque local key")
        if key not in keys:
            keys.append(key)
    return {"summary": summary.strip(), "citations": keys}


class EvidenceService:
    def __init__(self, repository: EvidenceRepository | None = None) -> None:
        self.repository = repository or EvidenceRepository()

    # ── active runs ─────────────────────────────────────────────────────────

    async def create_run(
        self,
        *,
        owner_id: int,
        peer_id: int,
        peer_name: str,
        ttl_days: int,
        messages: Any,
        summary: str = "",
    ) -> EvidenceRunBundle:
        owner = int(owner_id or 0)
        peer = int(peer_id or 0)
        if owner <= 0:
            raise EvidenceValidationError("an owner is required")
        if peer == 0:
            raise EvidenceValidationError("a source chat is required")
        ttl = _validate_ttl(ttl_days)
        summary_text = str(summary or "")
        if len(summary_text.encode("utf-8")) > constants.MAX_SUMMARY_BYTES:
            raise EvidenceLimitError("the summary exceeds the per-run byte limit")

        run_id = str(uuid.uuid4())
        refs = build_refs(messages, run_id=run_id, owner_id=owner)
        now = await self.repository.database_now()

        text_bytes = sum(ref.excerpt_bytes for ref in refs)
        run = EvidenceRun(
            id=run_id,
            owner_id=owner,
            peer_id=peer,
            peer_name=_clean_peer_name(peer_name),
            ttl_days=ttl,
            first_message_id=min(ref.message_id for ref in refs),
            last_message_id=max(ref.message_id for ref in refs),
            ref_count=len(refs),
            text_bytes=text_bytes,
            summary=summary_text,
            created_at=now,
            expires_at=now + timedelta(days=ttl),
        )
        try:
            # Admission is atomic: the expired sweep, the owner quota and the
            # insert are one serialized operation, so concurrent requests can
            # never both pass the check and exceed the approved cap. A valid
            # run is still never evicted.
            stored = await self.repository.create_run(
                run,
                max_active=constants.MAX_ACTIVE_RUNS_PER_OWNER,
                now=now,
            )
        except EvidenceQuotaError as exc:
            raise EvidenceQuotaError(
                f"active evidence run limit reached ({constants.MAX_ACTIVE_RUNS_PER_OWNER}); "
                "expired runs may be removed by an existing operation, but a valid run "
                "is never evicted"
            ) from exc
        try:
            await self.repository.add_refs(stored, list(refs))
        except Exception:
            # There is no cross-table transaction in the REST boundary.  Remove
            # the parent rather than leave an Active Run with no snapshots.
            await self.repository.delete_run(owner, stored.id)
            raise
        return EvidenceRunBundle(run=stored, refs=refs)

    async def get_run(self, owner_id: int, run_id: str) -> EvidenceRunBundle:
        owner = int(owner_id or 0)
        run = await self.repository.get_run(owner, str(run_id))
        if run is None:
            raise EvidenceNotFound("that evidence run does not exist for this owner")
        now = await self.repository.database_now()
        if run.expires_at <= now:
            # Logical expiry is immediate; there is no archive/Telegram/chat
            # reconstruction path and no TTL extension.
            raise EvidenceExpired(
                "that evidence run has expired; its snapshots are no longer accessible"
            )
        refs = tuple(await self.repository.list_refs(owner, run.id))
        return EvidenceRunBundle(run=run, refs=refs)

    async def list_active_runs(self, owner_id: int) -> list[EvidenceRun]:
        owner = int(owner_id or 0)
        now = await self.repository.database_now()
        runs = await self.repository.list_runs(owner, now)
        return [run for run in runs if run.expires_at > now][: constants.MAX_ACTIVE_RUNS_PER_OWNER]

    async def active_run_count(self, owner_id: int) -> int:
        now = await self.repository.database_now()
        return await self.repository.count_active_runs(int(owner_id or 0), now)

    # ── cited summary ───────────────────────────────────────────────────────

    async def run_cited_summary(
        self,
        source: Any,
        *,
        owner_id: int,
        peer_id: int,
        peer_name: str,
        count: int = constants.DEFAULT_CITED_COUNT,
        ttl_days: int = constants.DEFAULT_TTL_DAYS,
        instruction: str = "",
        language: str = "",
        provider_manager: Any = None,
        current_message_id: int | None = None,
        timeout_s: float | None = None,
        request_id: str = "",
        peer_type: Any = None,
        opt_in: bool = False,
    ) -> CitedSummaryResult:
        owner = int(owner_id or 0)
        peer = int(peer_id or 0)
        if not opt_in:
            raise EvidenceValidationError(
                "Cited Summary requires explicit opt-in for this request"
            )
        ttl = _validate_ttl(ttl_days)
        try:
            requested = int(count)
        except (TypeError, ValueError) as exc:
            raise EvidenceValidationError("count must be a whole number") from exc
        if requested < 1:
            requested = constants.DEFAULT_CITED_COUNT
        if requested > constants.MAX_CITED_COUNT:
            raise EvidenceLimitError(
                f"Cited Summary accepts at most {constants.MAX_CITED_COUNT} references"
            )
        focus = " ".join(str(instruction or "").split())
        if len(focus) > history_ai_service.MAX_INSTRUCTION_CHARS:
            raise EvidenceValidationError(
                f"instruction must be {history_ai_service.MAX_INSTRUCTION_CHARS} "
                "characters or fewer"
            )
        if source is None:
            raise EvidenceValidationError("Telegram history is not available")
        if provider_manager is None:
            raise EvidenceProviderError("the AI provider manager is not available")

        if peer_type is None:
            try:
                peer_type = await history_service.classify_chat(source, peer)
            except history_service.HistoryError as exc:
                raise EvidenceValidationError(str(exc)) from exc
        refusal = history_policy.validate_count(peer_type, requested)
        if refusal is not None:
            raise EvidenceValidationError(refusal)

        before_id = int(current_message_id) if current_message_id else None
        try:
            slice_ = await history_service.fetch_recent_history(
                source,
                peer,
                count=requested,
                before_id=before_id,
            )
        except history_service.HistoryError as exc:
            raise EvidenceValidationError(str(exc)) from exc
        if not slice_.messages:
            raise EvidenceEmpty("no eligible messages were available for Cited Summary")
        if not any(str(getattr(message, "text", "")).strip() for message in slice_.messages):
            raise EvidenceEmpty(
                "the selected messages have no text evidence to summarize"
            )

        bundle = await self.create_run(
            owner_id=owner,
            peer_id=peer,
            peer_name=peer_name,
            ttl_days=ttl,
            messages=slice_.messages,
        )
        try:
            projection = [
                {"key": ref.key, "excerpt": ref.excerpt or "(media only)"}
                for ref in bundle.refs
            ]
            prompt_lines = [
                f"Evidence language: {str(language or 'same as source')[:32]}",
                f"Owner focus: {focus or 'none'}",
                "",
                "Numbered excerpts:",
            ]
            prompt_lines.extend(
                f"[{item['key']}] {item['excerpt']}" for item in projection
            )
            messages = [
                {"role": "system", "content": _SYSTEM_PROMPT},
                {"role": "user", "content": "\n".join(prompt_lines)},
            ]
            raw = await self._call_provider(
                provider_manager,
                messages,
                timeout_s=timeout_s,
                request_id=request_id,
            )
            parsed = _extract_json(raw)
            by_key = {ref.key: ref for ref in bundle.refs}
            citations: list[EvidenceCitation] = []
            for key in parsed["citations"]:
                ordinal = _parse_key(key)
                ref = by_key.get(f"E{ordinal}")
                if ref is None:
                    raise EvidenceStructuredOutputError(
                        "the provider cited a key that is not in this run"
                    )
                citations.append(_citation(bundle.run, ref))
            if not citations:
                raise EvidenceStructuredOutputError("no valid citations resolved")
            summary = str(parsed["summary"])
            stored = await self.repository.update_run_summary(
                owner, bundle.run.id, summary,
            )
        except (EvidenceStructuredOutputError, EvidenceLimitError, EvidenceValidationError):
            await self.repository.delete_run(owner, bundle.run.id)
            raise
        except Exception:
            # Provider/storage failures are honest and leave no half-useful run.
            await self.repository.delete_run(owner, bundle.run.id)
            raise

        citations.sort(key=lambda item: item.ordinal)
        data = {
            "operation": "cited_summary",
            "run_id": stored.id,
            "requested": requested,
            "processed": len(bundle.refs),
            "truncated": bool(slice_.truncated),
            "capped": False,
            "first_message_id": stored.first_message_id,
            "last_message_id": stored.last_message_id,
            "refs": stored.ref_count,
            "citations": len(citations),
            "ttl_days": stored.ttl_days,
            "expires_at": stored.expires_at.isoformat(),
            "language": str(language or ""),
            "language_source": "request" if language else "global",
            "language_mode": "auto" if str(language or "") == "auto" else "",
            "language_detected": False,
        }
        return CitedSummaryResult(
            run_id=stored.id,
            summary=stored.summary,
            citations=tuple(citations),
            data=data,
        )

    async def _call_provider(
        self,
        manager: Any,
        messages: list[dict[str, str]],
        *,
        timeout_s: float | None,
        request_id: str,
    ) -> str:
        timeout = float(timeout_s or constants.DEFAULT_PROVIDER_TIMEOUT_S)
        timeout = max(timeout, constants.MIN_PROVIDER_TIMEOUT_S)
        try:
            response = await asyncio.wait_for(
                manager.chat(
                    messages,
                    tools=[],
                    response_format={"type": "json_object"},
                ),
                timeout=timeout,
            )
        except asyncio.CancelledError:
            raise
        except asyncio.TimeoutError as exc:
            raise EvidenceProviderError("the AI provider timed out") from exc
        except Exception as exc:  # noqa: BLE001 — provider mesh boundary
            logger.warning(
                "AI_EXEC_TRACE request_id=%s stage=evidence_provider_failed error=%s",
                request_id or "-",
                type(exc).__name__,
            )
            raise EvidenceProviderError(
                f"the AI provider failed ({type(exc).__name__})"
            ) from exc
        if not getattr(response, "success", False):
            raise EvidenceProviderError("the AI provider returned an unsuccessful response")
        text = str(getattr(response, "text", "") or "").strip()
        if not text:
            raise EvidenceProviderError("the AI provider returned an empty response")
        return text

    # ── archive / suggestions ──────────────────────────────────────────────

    async def archive_candidates(
        self, owner_id: int, run_id: str,
    ) -> list[ArchiveSuggestion]:
        bundle = await self.get_run(owner_id, run_id)
        suggestions: list[ArchiveSuggestion] = []
        for ref in bundle.refs:
            if len(suggestions) >= constants.MAX_ARCHIVE_SUGGESTIONS:
                break
            source = await self.repository.find_source(
                int(owner_id), bundle.run.peer_id, ref.message_id,
            )
            existing = (
                await self.repository.find_revision_by_hash(
                    int(owner_id), source.id, ref.content_hash,
                )
                if source is not None
                else None
            )
            if existing is not None:
                continue
            if source is None:
                reason = (
                    "Reviewed in this active evidence run; an owner-approved "
                    "immutable copy preserves the exact bounded excerpt."
                )
            else:
                reason = (
                    "This source has a different archived revision; confirming "
                    "creates a new immutable revision and never overwrites the old one."
                )
            suggestions.append(
                ArchiveSuggestion(
                    key=ref.key,
                    ordinal=ref.ordinal,
                    excerpt=ref.excerpt,
                    reason=reason,
                    already_archived=False,
                )
            )
        return suggestions

    async def archive_ref(
        self,
        owner_id: int,
        run_id: str,
        key: str,
        *,
        approved: bool = False,
    ) -> ArchiveResult:
        """Create one immutable revision after explicit owner approval.

        ``approved=False`` is the safe default: it returns an explanation and
        writes nothing.  A same-hash request is a no-op even when approved.
        """
        owner = int(owner_id or 0)
        ordinal = _parse_key(key)
        bundle = await self.get_run(owner, run_id)
        ref = next((item for item in bundle.refs if item.ordinal == ordinal), None)
        if ref is None:
            raise EvidenceNotFound("that reference is not part of this evidence run")
        source = await self.repository.find_source(
            owner, bundle.run.peer_id, ref.message_id,
        )
        existing = (
            await self.repository.find_revision_by_hash(owner, source.id, ref.content_hash)
            if source is not None
            else None
        )
        if existing is not None:
            await self.repository.link_ref_revision(
                owner, bundle.run.id, ref.ordinal, existing.id,
            )
            return ArchiveResult(
                status="unchanged",
                source_id=existing.source_id,
                revision_id=existing.id,
                revision_no=existing.revision_no,
                content_hash=existing.content_hash,
                reason="the exact excerpt is already archived; no new revision was written",
                citation=_citation(bundle.run, ref),
            )
        if not approved:
            raise EvidenceConfirmationRequired(
                "archiving creates an immutable owner-approved revision; confirm this "
                "source explicitly before it is written"
            )

        now = await self.repository.database_now()
        write = await self.repository.archive_atomic(
            owner_id=owner,
            peer_id=bundle.run.peer_id,
            message_id=ref.message_id,
            source_id=str(uuid.uuid4()),
            revision_id=str(uuid.uuid4()),
            content_hash=ref.content_hash,
            text=ref.excerpt,
            approval_reference="owner-confirmed",
            now=now,
            max_records=constants.MAX_ARCHIVE_RECORDS_PER_OWNER,
            max_bytes=constants.MAX_ARCHIVE_BYTES_PER_OWNER,
        )
        if write.status == "quota":
            raise EvidenceQuotaError(
                "archive storage limit reached "
                f"({constants.MAX_ARCHIVE_RECORDS_PER_OWNER} records or "
                f"{constants.MAX_ARCHIVE_BYTES_PER_OWNER} bytes); "
                "no archived revision was overwritten or evicted"
            )
        revision = await self.repository.get_revision(owner, write.revision_id)
        if revision is None:
            raise EvidenceStorageError(
                "archive revision disappeared before the reference was linked"
            )
        await self._link_ref(bundle.run, ref, revision)
        if write.status == "unchanged":
            return ArchiveResult(
                status="unchanged",
                source_id=revision.source_id,
                revision_id=revision.id,
                revision_no=revision.revision_no,
                content_hash=revision.content_hash,
                reason="the exact excerpt is already archived; no new revision was written",
                citation=_citation(bundle.run, ref),
            )
        return ArchiveResult(
            status="created",
            source_id=revision.source_id,
            revision_id=revision.id,
            revision_no=revision.revision_no,
            content_hash=revision.content_hash,
            reason=(
                "owner-approved immutable revision created; previous revisions "
                "remain unchanged"
            ),
            citation=_citation(bundle.run, ref),
        )

    async def _link_ref(
        self,
        run: EvidenceRun,
        ref: EvidenceRef,
        revision: EvidenceRevision,
    ) -> None:
        """Bind this reference to the exact revision, after validating scope."""
        source = await self.repository.get_source(run.owner_id, revision.source_id)
        if (
            source is None
            or source.peer_id != run.peer_id
            or source.message_id != ref.message_id
        ):
            raise EvidenceValidationError(
                "archived revision does not belong to this evidence reference"
            )
        await self.repository.link_ref_revision(
            run.owner_id, run.id, ref.ordinal, revision.id,
        )

    async def archive_usage(self, owner_id: int) -> ArchiveUsage:
        return await self.repository.archive_usage(int(owner_id or 0))

    async def list_archive_sources(self, owner_id: int) -> list[EvidenceSource]:
        return await self.repository.list_sources(int(owner_id or 0))

    async def list_revisions(
        self, owner_id: int, source_id: str,
    ) -> list[EvidenceRevision]:
        return await self.repository.list_revisions(int(owner_id or 0), source_id)

    async def get_source(self, owner_id: int, source_id: str) -> EvidenceSource:
        source = await self.repository.get_source(int(owner_id or 0), str(source_id))
        if source is None:
            raise EvidenceNotFound("that archived source does not exist for this owner")
        return source

    # ── deletion ────────────────────────────────────────────────────────────

    async def delete_revision(
        self,
        owner_id: int,
        revision_id: str,
        *,
        confirm: bool = False,
        cascade_confirmed: bool = False,
    ) -> bool:
        owner = int(owner_id or 0)
        revision = await self.repository.get_revision(owner, str(revision_id))
        if revision is None:
            raise EvidenceNotFound("that archive revision does not exist for this owner")
        if not confirm:
            raise EvidenceConfirmationRequired(
                "deleting an archived revision is destructive and requires explicit "
                "owner confirmation"
            )
        dependents = await self.repository.count_revision_dependents(owner, revision.id)
        if dependents and not cascade_confirmed:
            raise EvidenceDependencyBlocked(
                f"{dependents} dependent record(s) reference this revision. "
                "Deletion is blocked by default; a separate explicit cascade "
                "confirmation is required. Active-run excerpt snapshots are not "
                "part of this archive deletion and remain until their own TTL."
            )
        deleted = await self.repository.delete_revision(owner, revision.id)
        if not deleted:
            raise EvidenceNotFound("that archive revision disappeared before deletion")
        return True

    async def delete_source(
        self,
        owner_id: int,
        source_id: str,
        *,
        confirm: bool = False,
        cascade_confirmed: bool = False,
    ) -> bool:
        owner = int(owner_id or 0)
        source = await self.repository.get_source(owner, str(source_id))
        if source is None:
            raise EvidenceNotFound("that archived source does not exist for this owner")
        if not confirm:
            raise EvidenceConfirmationRequired(
                "deleting an archived source and all of its revisions is destructive "
                "and requires explicit owner confirmation"
            )
        dependents = await self.repository.count_source_dependents(owner, source.id)
        revisions = await self.repository.list_revisions(owner, source.id)
        dependent_revisions = 0
        for revision in revisions:
            dependent_revisions += await self.repository.count_revision_dependents(
                owner, revision.id,
            )
        total_dependents = dependents + dependent_revisions
        if total_dependents and not cascade_confirmed:
            raise EvidenceDependencyBlocked(
                f"{total_dependents} dependent record(s) reference this source or its "
                "revisions. Deletion is blocked by default; a separate explicit "
                "cascade confirmation is required. Active-run excerpt snapshots are "
                "not deleted and remain until their own TTL."
            )
        deleted = await self.repository.delete_source(owner, source.id)
        if not deleted:
            raise EvidenceNotFound("that archived source disappeared before deletion")
        return True


_SERVICE: EvidenceService | None = None


def get_evidence_service() -> EvidenceService:
    """The process-wide service (one repository, no second lifecycle owner)."""
    global _SERVICE
    if _SERVICE is None:
        _SERVICE = EvidenceService()
    return _SERVICE


def reset_evidence_service_for_tests(
    repository: EvidenceRepository | None = None,
) -> EvidenceService:
    """Replace the process-wide service with an explicit test repository."""
    global _SERVICE
    _SERVICE = EvidenceService(
        repository or EvidenceRepository(allow_memory=True)
    )
    return _SERVICE
