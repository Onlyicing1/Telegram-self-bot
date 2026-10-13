"""
Premium custom emoji — the ONE traceable diagnostic workflow for the Emoji panel.

The Emoji panel's primary action runs the REAL production inline route
(``premium_emoji_inline_service.send_premium_emoji_via_inline``) and renders a
step-by-step trace of what actually happened. This module owns the trace — the
stage vocabulary, the bounded per-run record, and the renderer — while the
service stays the sole executor of every Telegram operation.

Contracts:

* **The trace is generated from real evidence, never simulated.** Every stage's
  status, timing, explanation and evidence come from the service's own outcome
  record (``ok``/``error``/``diagnosis``/``inline_result``/``readback``/
  ``eligibility``). No stage is marked succeeded unless the evidence says so.
* **Submission is never conflated with verified success.** The trace keeps four
  separate facts distinct: the payload was validated locally, the request was
  submitted, Telegram accepted it, and — only after a read-back — the expected
  custom-emoji entity (with the helper bot's ``via_bot_id``) was found in the
  stored message. A retained entity is still ``…_RENDER_UNVERIFIED``.
* **One run per owner at a time.** A single-slot per-owner record; a second run
  cannot start while one is in flight, so duplicate callbacks cannot drive
  duplicate Telegram operations.
* **No secrets, bounded output.** The renderer emits stage names, statuses,
  short durations, bounded explanations and non-sensitive evidence only —
  never session strings, tokens, access hashes or whole messages.
* **No new infrastructure.** Pure functions plus one in-memory dict; no client,
  loop, scheduler, executor or listener of its own.
"""
from __future__ import annotations

import time
from typing import Any

# ── stage statuses ───────────────────────────────────────────────────────────

STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCEEDED = "succeeded"
STATUS_FAILED = "failed"
STATUS_SKIPPED = "skipped"
STATUS_BLOCKED = "blocked"
STATUS_INCONCLUSIVE = "inconclusive"

#: The fixed stage vocabulary, in execution order. Stages that do not apply to
#: a given run are reported as ``skipped`` with the honest reason.
STAGES: tuple[tuple[str, str], ...] = (
    ("run_started", "Run started"),
    ("target_resolved", "Target resolved"),
    ("entity_extracted", "Emoji entity extracted"),
    ("payload_validated", "Payload validated"),
    ("route_entitlement", "Route & entitlement checked"),
    ("request_initiated", "Telegram request initiated"),
    ("response_received", "Telegram response received"),
    ("result_inspected", "Resulting message inspected"),
    ("run_completed", "Run completed"),
)
_STAGE_NAMES = dict(STAGES)

#: Maps the service's diagnosis constants to the earliest stage the run
#: actually stopped at. Anything unlisted fails at the first evidence-bearing
#: stage and is classified honestly there.
_DIAGNOSIS_FAIL_STAGE: dict[str, str] = {
    # source / payload / destination refusals (before any Telegram call)
    "SOURCE_ENTITY_MISSING": "entity_extracted",
    "UNSUPPORTED_DESTINATION": "target_resolved",
    "OUTBOUND_PAYLOAD_INVALID": "payload_validated",
    # inline availability / query
    "INLINE_UNAVAILABLE": "route_entitlement",
    "INLINE_RESULT_REJECTED": "response_received",
    "INLINE_RESULT_EMPTY": "response_received",
    # stored-result inspection
    "INLINE_RESULT_UNSUPPORTED": "result_inspected",
    "INLINE_RESULT_NO_SEND_MESSAGE": "result_inspected",
    "INLINE_RESULT_ENTITY_MISSING": "result_inspected",
    "INLINE_BOT_NOT_ENTITLED": "result_inspected",
    # send
    "INLINE_SEND_FAILED": "request_initiated",
    # read-back
    "READBACK_FAILED": "result_inspected",
    "STORED_ENTITY_STRIPPED": "result_inspected",
    "STORED_ENTITY_MISMATCH": "result_inspected",
    "STORED_ATTRIBUTION_MISSING": "result_inspected",
}

#: The outcome a fully verified run reaches (a retained entity, verified in the
#: read-back, with the helper bot's own attribution).
_DIAGNOSIS_VERIFIED = "STORED_ENTITY_VERIFIED_RENDER_UNVERIFIED"

#: Diagnoses that are a BLOCKED prerequisite (a missing/unavailable condition)
#: rather than an execution failure — the interface reports them as blocked.
_BLOCKED_DIAGNOSES = frozenset(
    {"UNSUPPORTED_DESTINATION", "INLINE_UNAVAILABLE", "INLINE_BOT_NOT_ENTITLED"}
)

#: A short, non-sensitive classification for the run's final line.
_DIAGNOSIS_CLASS: dict[str, str] = {
    "SOURCE_ENTITY_MISSING": "source_entity_missing",
    "UNSUPPORTED_DESTINATION": "unsupported_destination",
    "OUTBOUND_PAYLOAD_INVALID": "payload_invalid",
    "INLINE_UNAVAILABLE": "inline_unavailable",
    "INLINE_RESULT_REJECTED": "query_failed",
    "INLINE_RESULT_EMPTY": "empty_result",
    "INLINE_RESULT_UNSUPPORTED": "unsupported_result",
    "INLINE_RESULT_NO_SEND_MESSAGE": "no_send_message",
    "INLINE_RESULT_ENTITY_MISSING": "entity_missing",
    "INLINE_BOT_NOT_ENTITLED": "bot_not_entitled",
    "INLINE_SEND_FAILED": "send_failed",
    "READBACK_FAILED": "readback_failed",
    "STORED_ENTITY_STRIPPED": "stored_entity_stripped",
    "STORED_ENTITY_MISMATCH": "stored_entity_mismatch",
    "STORED_ATTRIBUTION_MISSING": "attribution_missing",
    _DIAGNOSIS_VERIFIED: "verified_stored",
}


# ── per-owner run record ─────────────────────────────────────────────────────

_RUNS: dict[int, dict[str, Any]] = {}


def begin_run(owner_id: Any, run_id: str | None = None, started_at: float | None = None) -> bool:
    """Open the single-slot run for ``owner_id``.

    Returns ``False`` when a run is already in flight for this owner — the
    caller then refuses the duplicate instead of driving a second Telegram
    operation. Never a queue: the slot is released by :func:`end_run`.
    """
    key = _owner_key(owner_id)
    if key is None:
        return False
    if _RUNS.get(key, {}).get("in_flight"):
        return False
    _RUNS[key] = {
        "run_id": run_id or "?",
        "started_at": started_at if started_at is not None else time.monotonic(),
        "in_flight": True,
        "stage": "run_started",
        "outcome": None,
    }
    return True


def is_running(owner_id: Any) -> bool:
    key = _owner_key(owner_id)
    if key is None:
        return False
    return bool(_RUNS.get(key, {}).get("in_flight"))


def end_run(
    owner_id: Any, *, outcome: dict[str, Any], stage: str | None = None
) -> None:
    """Close the run, keeping its final trace available (no longer in flight)."""
    key = _owner_key(owner_id)
    if key is None:
        return
    record = _RUNS.get(key)
    if not isinstance(record, dict):
        record = {"run_id": (outcome or {}).get("run_id") or "?", "started_at": time.monotonic()}
        _RUNS[key] = record
    record["in_flight"] = False
    record["outcome"] = outcome
    if stage:
        record["stage"] = stage


def current_run(owner_id: Any) -> dict[str, Any] | None:
    key = _owner_key(owner_id)
    if key is None:
        return None
    return _RUNS.get(key)


def reset_runs() -> None:
    """Clear all run records — test support only."""
    _RUNS.clear()


def _owner_key(owner_id: Any) -> int | None:
    if isinstance(owner_id, int) and not isinstance(owner_id, bool) and owner_id:
        return owner_id
    return None


# ── the trace ────────────────────────────────────────────────────────────────


def build_trace(
    outcome: Any,
    *,
    started_at: float,
    finished_at: float | None = None,
    helper_bot_id: Any = None,
) -> dict[str, Any]:
    """Turn ONE real service outcome into an ordered, evidence-based trace.

    Returns ``{run_id, outcome, outcome_class, summary, stage, stages}`` where
    ``stages`` is one record per :data:`STAGES` entry with ``name``, ``label``,
    ``status``, ``detail``, ``evidence``, ``classification`` and ``elapsed_s``.
    """
    finished_at = finished_at if finished_at is not None else time.monotonic()
    record = outcome if isinstance(outcome, dict) else {}
    run_id = record.get("run_id") or "?"
    diagnosis = record.get("diagnosis")
    ok = bool(record.get("ok"))
    verified = bool(record.get("verified"))

    reached = _reached_stage(record, diagnosis, ok, verified)
    outcome_class = _outcome_class(record, diagnosis, ok, verified)
    summary = _summary(record, ok, verified)

    stages: list[dict[str, Any]] = []
    for name, label in STAGES:
        stages.append(
            _stage_record(
                name,
                label,
                record,
                diagnosis,
                ok,
                verified,
                reached,
                started_at,
                finished_at,
                helper_bot_id,
            )
        )
    return {
        "run_id": run_id,
        "outcome": outcome_class,
        "outcome_class": outcome_class,
        "summary": summary,
        "stage": reached,
        "stages": stages,
    }


def _reached_stage(
    record: dict[str, Any], diagnosis: Any, ok: bool, verified: bool
) -> str:
    """The LAST stage the run actually reached (its success or its failure)."""
    if ok and verified:
        return "run_completed"
    if isinstance(diagnosis, str) and diagnosis in _DIAGNOSIS_FAIL_STAGE:
        return _DIAGNOSIS_FAIL_STAGE[diagnosis]
    # Fall back on the record's own shape: the highest evidence it carries.
    if record.get("readback", {}).get("attempted"):
        return "result_inspected"
    if record.get("message_id"):
        return "request_initiated"
    if record.get("inline_result", {}).get("attempted"):
        return "response_received"
    if record.get("payload"):
        return "payload_validated"
    return "run_started"


def _outcome_class(
    record: dict[str, Any], diagnosis: Any, ok: bool, verified: bool
) -> str:
    if ok and verified:
        return "verified"
    if isinstance(diagnosis, str) and diagnosis in _DIAGNOSIS_CLASS:
        return _DIAGNOSIS_CLASS[diagnosis]
    return "failed"


def _summary(record: dict[str, Any], ok: bool, verified: bool) -> str:
    detail = record.get("detail")
    if isinstance(detail, str) and detail:
        return detail
    return "the run completed and the stored entity was verified." if verified else (
        "the run completed." if ok else "the run did not complete."
    )


def _classification(record: dict[str, Any], stage_name: str, diagnosis: Any) -> str:
    """The safe error classification for a stage, from the run's own diagnosis."""
    if isinstance(diagnosis, str) and stage_name == _DIAGNOSIS_FAIL_STAGE.get(diagnosis):
        return _DIAGNOSIS_CLASS.get(diagnosis, "failed")
    return ""


def _status(
    record: dict[str, Any],
    name: str,
    diagnosis: Any,
    ok: bool,
    verified: bool,
    reached: str,
) -> str:
    order = [s for s, _ in STAGES]
    idx = order.index(name)
    reached_idx = order.index(reached)
    fail_stage = _DIAGNOSIS_FAIL_STAGE.get(diagnosis) if isinstance(diagnosis, str) else None
    fail_idx = order.index(fail_stage) if fail_stage in order else None

    if ok and verified:
        return STATUS_SUCCEEDED
    if fail_idx is not None:
        if idx < fail_idx:
            return STATUS_SUCCEEDED
        if idx == fail_idx:
            return (
                STATUS_BLOCKED
                if isinstance(diagnosis, str) and diagnosis in _BLOCKED_DIAGNOSES
                else STATUS_FAILED
            )
        return STATUS_SKIPPED
    # No explicit failure stage (e.g. an inconclusive read-back): stages up to
    # the reached point are succeeded, the reached point is inconclusive, the
    # rest are skipped.
    if idx < reached_idx:
        return STATUS_SUCCEEDED
    if idx == reached_idx:
        return STATUS_INCONCLUSIVE
    return STATUS_SKIPPED


def _stage_record(
    name: str,
    label: str,
    record: dict[str, Any],
    diagnosis: Any,
    ok: bool,
    verified: bool,
    reached: str,
    started_at: float,
    finished_at: float,
    helper_bot_id: Any,
) -> dict[str, Any]:
    status = _status(record, name, diagnosis, ok, verified, reached)
    detail, evidence = _stage_detail(name, record, status, helper_bot_id)
    return {
        "name": name,
        "label": label,
        "status": status,
        "detail": detail,
        "evidence": evidence,
        "classification": _classification(record, name, diagnosis),
        "elapsed_s": round(max(0.0, finished_at - started_at), 3),
    }


def _stage_detail(
    name: str, record: dict[str, Any], status: str, helper_bot_id: Any
) -> tuple[str, dict[str, Any]]:
    """The concise explanation + non-sensitive evidence for ONE stage."""
    inline = record.get("inline_result") or {}
    readback = record.get("readback") or {}
    eligibility = record.get("eligibility") or {}
    payload = record.get("payload") or {}
    entity = payload.get("entity") or {}
    error = record.get("error")
    detail_text = record.get("detail")
    skipped = status == STATUS_SKIPPED

    if name == "run_started":
        route = record.get("send_path") or "messages.sendInlineBotResult"
        return (
            f"Diagnostic run started on the production inline route ({route}).",
            {"route": route, "run_id": record.get("run_id")},
        )
    if name == "target_resolved":
        dest = record.get("destination_chat_id")
        if skipped or dest is None:
            return ("Not reached — the run stopped before a destination was used.", {})
        return (
            f"Destination resolved to the owner's Saved Messages (chat #{dest}).",
            {"destination_chat_id": dest},
        )
    if name == "entity_extracted":
        if skipped or not entity:
            return ("Not reached — no source entity was extracted.", {})
        return (
            f"Source custom-emoji entity read: document #{entity.get('document_id')}"
            f" (UTF-16 offset {entity.get('offset')}, length {entity.get('length')}).",
            {
                "document_id": entity.get("document_id"),
                "offset": entity.get("offset"),
                "length": entity.get("length"),
                "glyph_source": eligibility.get("glyph_source") or None,
            },
        )
    if name == "payload_validated":
        if skipped or not payload:
            return ("Not reached — no outbound payload was validated.", {})
        return (
            "Outbound payload validated: the custom-emoji entity wraps exactly "
            "the glyph at its real UTF-16 offset.",
            {
                "document_id": entity.get("document_id"),
                "offset": entity.get("offset"),
                "length": entity.get("length"),
                "entity_count": len(payload.get("entities") or []),
            },
        )
    if name == "route_entitlement":
        helper_id = eligibility.get("helper_bot_id")
        helper_user = eligibility.get("helper_bot_username")
        owner_premium = eligibility.get("owner_premium")
        free = (eligibility.get("document") or {}).get("free")
        ev = {
            "helper_bot_id": helper_id,
            "helper_bot_username": helper_user or None,
            "owner_premium": owner_premium,
            "document_free": free,
        }
        if skipped:
            return ("Not reached — the run stopped before the route was checked.", ev)
        if helper_id or helper_user:
            return (
                "Helper bot available for the inline route"
                f" (id {helper_id}, @{helper_user or '?'}).",
                ev,
            )
        return ("The helper bot was not available for the inline route.", ev)
    if name == "request_initiated":
        if skipped:
            return ("Not initiated — the run stopped before any send.", {})
        msg = record.get("message_id")
        if msg:
            return (
                f"Telegram request initiated; the inline send returned message #{msg}.",
                {"message_id": msg, "send_path": record.get("send_path")},
            )
        if inline.get("attempted"):
            return (
                "The inline query was initiated; no send request was made because "
                "the response carried no usable result.",
                {"send_path": record.get("send_path")},
            )
        return (
            "The Telegram request was not initiated (no message was sent).",
            {"send_path": record.get("send_path")},
        )
    if name == "response_received":
        if skipped or not inline.get("attempted"):
            return ("Not reached — no inline response was received.", {})
        return (
            "Telegram returned the stored inline result "
            f"({inline.get('result_count')} result(s)).",
            {
                "result_count": inline.get("result_count"),
                "reason": inline.get("reason") or None,
            },
        )
    if name == "result_inspected":
        if skipped:
            return ("Not reached — nothing was inspected.", {})
        if readback.get("attempted"):
            if readback.get("ok"):
                if readback.get("entity_present"):
                    via = readback.get("via_bot_id")
                    return (
                        "Stored message inspected: a real custom-emoji entity is "
                        f"present (document #{readback.get('document_id')}), "
                        f"via_bot_id={via!r}.",
                        {
                            "entity_present": True,
                            "document_id": readback.get("document_id"),
                            "offset": readback.get("offset"),
                            "length": readback.get("length"),
                            "document_id_match": readback.get("document_id_match"),
                            "span_match": readback.get("span_match"),
                            "via_bot_id": via,
                            "via_bot_match": readback.get("via_bot_match"),
                            "expected_via_bot_id": helper_bot_id or None,
                        },
                    )
                return (
                    "Stored message inspected: NO custom-emoji entity — it was "
                    "stripped or ignored.",
                    {"entity_present": False, "via_bot_id": readback.get("via_bot_id")},
                )
            return (
                f"Read-back failed: {readback.get('error')}.",
                {"readback_ok": False, "error": str(readback.get("error"))[:120]},
            )
        if inline.get("attempted"):
            return (
                "Stored inline result inspected before the send: "
                + (
                    "the custom-emoji entity is present."
                    if inline.get("entity_present")
                    else "NO custom-emoji entity is present."
                ),
                {
                    "entity_present": inline.get("entity_present"),
                    "entity_count": inline.get("entity_count"),
                    "reason": inline.get("reason") or None,
                },
            )
        return ("Not reached — nothing was inspected.", {})
    if name == "run_completed":
        if status == STATUS_SUCCEEDED:
            return (
                "Run completed: the stored entity and the helper bot's "
                "attribution were verified (display remains for you to confirm).",
                {"verified": True},
            )
        reason = detail_text if isinstance(detail_text, str) and detail_text else error
        return (
            f"Run stopped before verified success. {reason or ''}".strip(),
            {"error": error, "diagnosis": record.get("diagnosis")},
        )
    return ("", {})


# ── rendering ────────────────────────────────────────────────────────────────

_STATUS_MARK = {
    STATUS_SUCCEEDED: "✅",
    STATUS_FAILED: "❌",
    STATUS_SKIPPED: "⏭️",
    STATUS_BLOCKED: "⛔",
    STATUS_INCONCLUSIVE: "◌",
    STATUS_RUNNING: "…",
    STATUS_PENDING: "·",
}

_OUTCOME_HEADLINE = {
    "verified": "✓ Verified stored — genuine custom-emoji entity with the helper bot's attribution",
    "verified_stored": "✓ Verified stored — genuine custom-emoji entity with the helper bot's attribution",
    "bot_not_entitled": "⛔ Blocked — Telegram entitlement restriction (the helper bot may not use custom-emoji entities)",
    "entity_missing": "❌ Failed — the stored inline result lost the custom-emoji entity",
    "unsupported_result": "❌ Failed — the inline result shape could not be inspected safely",
    "no_send_message": "❌ Failed — the stored inline result carries no send-message payload",
    "empty_result": "❌ Failed — the helper bot returned no inline result",
    "query_failed": "❌ Failed — the inline query did not return a usable result",
    "send_failed": "❌ Failed — the inline send was not accepted",
    "readback_failed": "◌ Inconclusive — the send was accepted but could not be read back",
    "stored_entity_stripped": "❌ Failed — Telegram stored the message WITHOUT the entity",
    "stored_entity_mismatch": "❌ Failed — the stored entity does not match the one sent",
    "attribution_missing": "❌ Failed — the stored entity lacks the helper bot's attribution",
    "source_entity_missing": "❌ Failed — the reply carried no genuine custom-emoji entity",
    "unsupported_destination": "⛔ Blocked — only your Saved Messages is a supported destination",
    "payload_invalid": "❌ Failed — the outbound payload failed its own validation",
    "inline_unavailable": "⛔ Blocked — the helper bot is not available for the inline route",
    "failed": "❌ Failed — the run did not complete",
}


def render_trace(trace: dict[str, Any]) -> str:
    """A compact, English, evidence-based trace body for the panel."""
    lines: list[str] = []
    outcome_class = trace.get("outcome_class") or trace.get("outcome") or "failed"
    headline = _OUTCOME_HEADLINE.get(outcome_class, _OUTCOME_HEADLINE["failed"])
    lines.append(headline)
    run_id = trace.get("run_id")
    if run_id and run_id != "?":
        lines.append(f"Run `{run_id}` · route: inline bot result")
    lines.append("")
    for stage in trace.get("stages") or []:
        mark = _STATUS_MARK.get(stage.get("status"), "·")
        lines.append(f"{mark} **{stage.get('label')}** — {stage.get('status')}")
        detail = stage.get("detail")
        if detail:
            lines.append(f"   {detail}")
        classification = stage.get("classification")
        if classification:
            lines.append(f"   classification: `{classification}`")
    summary = trace.get("summary")
    if summary:
        lines.append("")
        lines.append(f"_{summary}_")
    return "\n".join(lines)
