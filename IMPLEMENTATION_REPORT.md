# IMPLEMENTATION REPORT

**Reporting date:** 2026-11-26

**Task:** Real, independent 4 GB monthly outbound-egress hard limit

**Branch:** `main`

**Base commit:** `0ae8434bed4ffa65ce7c5f94a7b8b75b49d5e0c9`

**Implementation + tests commit:** `c140f92d571a733efe6ff338744f1e8e6a7d48b3`

**Status:** **IMPLEMENTED · TESTED OFFLINE · PUSHED · NOT LIVE VERIFIED.**

No live Render, Supabase, Telegram or AI-provider request was made from this
workspace. No SQL was executed and no schema, environment variable or Render
setting was changed.

---

## 1. Objective

Add a genuine 4 GB monthly egress stop to the existing outbound governor. The
stop had to be independent of the staged 50/60/70/80/90 % policy, had to use the
effective monthly measurement rather than only the local reservation ledger,
had to work when a proposed reservation would cross the target, and had to fail
closed when Render telemetry is missing, stale or partial.

PostgREST request-volume work and Anti-Edit optimisation were explicitly out of
scope and were not touched.

## 2. Root cause

At `0ae8434` the governor enforced two different things:

1. A durable reservation ledger with a 4 GB monthly refusal
   (`_precheck_limits`, `_reserve_locked`), which only counts payloads that
   already call `reserve_outbound_bytes` / `reserve_outbound_bytes_sync`.
2. A staged monthly policy whose `_policy_tier()` stops at 90 %
   (`3_600_000_000` bytes). `4_000_000_000` bytes therefore reported tier 90,
   exactly like any larger value.

`ensure_action_allowed()` denied non-text actions when the active tier reached
their minimum, so tier 90 happened to deny governed non-text payloads. But
there was no independent decision that read the effective monthly egress and
refused at `>= MONTHLY_TARGET_BYTES`; a ledger below 4 GB could not be
distinguished from a Render month above it, and tier 90 was the only reason an
upload stopped. There was also no reservation-aware check that a proposed
reservation would reach the target, and a partial Render month was still
treated as a complete, trusted measurement.

## 3. Exact files changed

| File | Change |
|---|---|
| `backend/services/outbound_bandwidth_service.py` | Independent hard-limit decision, explicit measurement status, live-ledger reservation check, structured refusal error/codes |
| `tests/test_outbound_bandwidth_policy.py` | 29 focused hard-limit, trust, tier-distinction, text and entry-point tests |
| `tests/test_outbound_upload_governance.py` | 2 real-seam tests (voice send, Deep Save upload seam) |
| `tests/conftest.py` | Suite snapshot declares the new `measurement_status` metadata explicitly |
| `IMPLEMENTATION_REPORT.md` | This report (full replacement) |

No other file was modified. The staged policy thresholds, `_policy_tier`, the
daily limit, the panel, render service, migrations, `render.yaml` and
`DATABASE_ARCHITECTURE.md` are unchanged.

## 4. Hard-limit decision semantics

The new decision lives in `_assert_monthly_hard_limit()` and runs **before**
`_assert_policy_allows()` in both the async and sync action paths. It is not a
new tier and does not read or modify `_policy_tier()`.

For a governed non-text action with proposed `byte_count`:

```
baseline = max(snapshot.effective_monthly_bytes, live_governor_month)
if baseline >= 4_000_000_000:              refuse hard_limit_reached
elif measurement_status != trusted:        refuse the matching measurement code
elif baseline + byte_count >= 4_000_000_000:
                                           refuse reservation_would_exceed_limit
else:                                      continue to the staged tier policy
```

Consequences:

- At exactly 4 GB and above, a governed non-text payload is refused even when
  the local ledger is at zero. Tier 90 is not consulted as permission.
- At 3.6–3.999… GB, an ordinary tier-90 refusal still occurs; at 4 GB the
  hard-limit refusal wins. The two decisions are separate and separately
  testable.
- At 3.999999999 GB, a one-byte reservation is refused because it would reach
  the target. The comparison is `>=`, not `>`.
- Text up to the existing 32 KiB exemption returns before the decision and is
  unchanged, including a `text` reservation at the target. Text larger than
  the exemption keeps its existing policy refusal.
- `_reserve_locked()` repeats a ledger-side `>=` check as the last line of
  defence and keeps text exempt. A ledger reservation that exceeds 4 GB still
  raises the pre-existing `OutboundBandwidthExceededError`, preserving that
  error contract.

## 5. Effective monthly usage and reservation interaction

`refresh_policy_snapshot()` already computes:

```
effective = max(render_monthly_bytes, governor_monthly_bytes, policy_floor_bytes)
```

Render and the governor ledger are **never added**. They overlap: a payload
counted by the governor may later appear in Render's service metric. The
persisted floor only prevents a delayed/lower reading from lowering the tier.

The hard limit preserves that rule. `_precheck_limits()` now also returns the
live ledger month, and the decision folds it in with `max`, not addition:

```
baseline = max(effective_snapshot, live_governor_month)
refuse if baseline + proposed_reservation >= 4_000_000_000
```

The proposed reservation is the only value added because it is not yet part of
either measurement. Bytes already reserved earlier in the month are already in
the governor month; bytes already transmitted may also be in Render. Taking the
maximum avoids counting the same byte twice while still refusing when either
source proves the target is reached.

The reservation ledger is deliberately conservative: reservations are never
refunded, so a failed upload can keep the governor month higher than actual
wire traffic. That can refuse a later reservation earlier than the wire total
would, which is the safe direction for a protection target.

## 6. Telemetry trust, missing, stale and partial behavior

Snapshots now carry `measurement_status` and `measurement_detail`:

| Status | Produced when | Hard-limit behavior for non-text |
|---|---|---|
| `trusted` | Render validated (available, current month, finite bytes, fresh `latest_data_at`), governor readable, floor persisted, `monthly_is_partial` false | Normal target/reservation check |
| `partial` | Same as trusted but Render's 30-day lookback cannot cover the whole calendar month (`monthly_is_partial`) | Refused with `monthly_egress_measurement_partial` |
| `untrusted` | Render reported data but it is stale (>3 h), from another month, future-dated, malformed, or the floor write could not be confirmed | Refused with `monthly_egress_measurement_untrusted` |
| `unavailable` | No API key/service id, HTTP/parse failure, no datapoints, or the governor ledger is unreadable | Refused with `monthly_egress_measurement_unavailable` |

A stale cached snapshot (`get_policy_snapshot`) becomes `unavailable`. Missing
telemetry is never converted to zero: the refusal carries
`effective_monthly_bytes = 0` only as a diagnostic value, and the operation is
still denied. Partial is never reported as complete; `trusted` and
`render_partial` keep their previous meanings for display and the staged
policy.

Text up to 32 KiB remains available under every status above. No new polling
loop, scheduler, collector or per-operation Render request was added: the
decision consumes the existing five-minute snapshot and the existing fresh
ledger read.

If the Render series has internal hourly gaps, the service already reports
`missing_hours`/`coverage_complete`, but the authority remains Render's own
monthly total and the existing three-hour freshness bound. `coverage_complete`
is routinely false for the in-progress hour, so it is not a usable hard-limit
gate without an arbitrary gap threshold; that limitation is recorded in
section 12 rather than hidden.

## 7. Refusal reasons and observability

`OutboundBandwidthHardLimitError` extends `OutboundBandwidthPolicyError` (so
existing callers keep their bounded refusal path) and adds:

- `reason_code`: one of
  `monthly_egress_hard_limit_reached`,
  `monthly_egress_measurement_unavailable`,
  `monthly_egress_measurement_untrusted`,
  `monthly_egress_measurement_partial`,
  `monthly_egress_reservation_would_exceed_limit`
- `reserved_bytes` (when applicable), `effective_monthly_bytes`, `tier`,
  `hard_limit_bytes`

One warning line is emitted per refusal:

```
OUTBOUND_MONTHLY_HARD_LIMIT_DENIED reason=... action=... effective=... reserved=... limit=...
```

It contains no message text, media, filename, chat id, credential, token or
session data, and it does not claim the value is an exact wire-byte
measurement.

## 8. Enforcement coverage

All four public governor entry points enforce the decision:

- `reserve_outbound_bytes()` (async)
- `reserve_outbound_bytes_sync()` (worker-thread path)
- `ensure_action_allowed()` (async preflight / action path)
- `ensure_action_allowed_sync()` (sync preflight / action path)

The private `_reserve_locked()` path repeats the ledger-side check. Verified
production seams that reach one or both entry points:

| Seam | Entry point |
|---|---|
| Deep Save single upload and album member uploads | `reserve_outbound_bytes` |
| Rename / re-upload | `ensure_action_allowed` + `reserve_outbound_bytes` |
| Voice notes | `ensure_action_allowed` + `reserve_outbound_bytes` |
| AI result attachments | `ensure_action_allowed` + `reserve_outbound_bytes` |
| Gemini OCR/STT (including base64-size upper bound) | `reserve_outbound_bytes_sync` |
| Groq / Speechmatics STT | `ensure_action_allowed_sync` + `reserve_outbound_bytes_sync` |
| Anti-Edit Storage snapshot | `reserve_outbound_bytes` |
| Appearance Storage upload | `ensure_action_allowed` + `reserve_outbound_bytes_sync` |
| Scheduled/automatic media | same paths, classified `automatic_media` |

Album operations are checked before transfer and per member; a hard-limit
refusal therefore refuses the whole operation before the first upload. No
alternative governor entry point was found that performs a governed non-text
payload without passing through one of the four functions above.

## 9. Tests actually executed and results

Focused governor suites (all passed):

```
.venv/bin/python -m pytest -q \
  tests/test_outbound_bandwidth_policy.py \
  tests/test_outbound_bandwidth_governor.py \
  tests/test_outbound_upload_governance.py \
  tests/test_render_bandwidth_service.py
→ 133 passed
```

Per file: policy 73, governor 35, upload governance 18, Render service 7.

The new tests cover: below-limit permission and a recorded reservation; exactly
4 GB and above; Render above 4 GB with the ledger below; ledger plus proposed
reservation reaching 4 GB; below-limit usage with a crossing reservation; all
governed action classes; all four public entry points; the locked reservation
path; missing, stale, untrusted and partial telemetry; missing data not read as
zero; text availability under every status; tier-90 versus hard-limit
distinction; and two real seams (voice send and Deep Save).

Relevant attribution/reservation suites:

```
.venv/bin/python -m pytest -q \
  tests/test_egress_attribution.py tests/test_bandwidth_panel.py \
  tests/test_render_bandwidth_panel.py tests/test_save_album_view_once_progress.py \
  tests/test_save_v2_telegram_sync.py tests/test_12_save_engine.py \
  tests/test_render_bandwidth_sources_diagnostic.py
→ 254 passed, 1 failed
```

The single failure is
`test_render_bandwidth_sources_diagnostic.py::test_maybe_run_is_bounded_to_six_hours`.

Full suite:

```
.venv/bin/python -m pytest -q
→ 8221 passed, 27 skipped, 5 failed in 180.49s
```

The five failures are pre-existing and were reproduced on an untouched
extraction of `origin/main` (`0ae8434`) before this delivery's changes:

1. `tests/test_database_architecture_doc.py::test_every_migration_file_is_classified_in_the_audit_section`
2. `tests/test_database_setup_order.py::test_the_audited_order_is_recorded_in_the_audit_table`
3. `tests/test_private_message_db_security.py::test_the_documented_counts_match_the_repository`
4. `tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
5. `tests/test_render_bandwidth_sources_diagnostic.py::test_maybe_run_is_bounded_to_six_hours`

None of them touches the governor, and the same five fail on the pristine base.

## 10. Compilation and diff checks

```
.venv/bin/python -m compileall -q backend   → exit 0
git diff --check <base> <commit>            → exit 0 (no whitespace errors)
```

`py_compile` was also run on the modified test files during development.

## 11. Files intentionally left untouched

- `backend/services/render_bandwidth_service.py` (metric ingestion unchanged)
- `backend/observability/egress_attribution.py`
- All Telegram, Supabase, provider and Render integration code
- `backend/bot/handlers/misc.py` and the bandwidth panel
- `render.yaml`, `Procfile`, `Dockerfile`, environment configuration
- Supabase migrations, SQL and `DATABASE_ARCHITECTURE.md`
- `INVESTIGATION.md`, `AGENTS.md`, `ROADMAP.md`, `LIFEOS_IDEAS.md`
- All other services, handlers, tests and documentation

## 12. Limitations and unresolved issues

1. **Not an exact wire-byte meter.** The target is enforced against Render's
   service metric and the governor's payload ledger. HTTP headers, TLS/TCP,
   MTProto framing, retransmissions, ungoverned PostgREST/Telegram/helper
   traffic and other egress are not measured by the governor.
2. **Out-of-scope traffic remains out of scope.** The hard limit refuses
   governed non-text payload operations only. It does not block PostgREST,
   ordinary Telegram RPCs, HTTP responses or other traffic; the task explicitly
   excluded them.
3. **Partial months block governed media.** Render's lookback cannot cover the
   first day(s) of a 31-day month. During that window the month total cannot be
   established, so non-text payloads are refused with
   `monthly_egress_measurement_partial`. Text remains available. This is
   intentional and deterministic, not a claim that the missing day was large.
4. **Missing telemetry blocks governed media.** Without a Render key/service id
   or a readable ledger, the governor already failed closed; the new decision
   keeps that behavior with an explicit reason.
5. **Internal hourly gaps are not separately gated.** `missing_hours` and
   `coverage_complete` are reported by the Render service, but the in-progress
   hour makes `coverage_complete` routinely false, so the hard limit relies on
   Render's monthly total and the existing three-hour freshness bound.
6. **The ledger is conservative.** Failed reservations are never refunded, so
   the effective value can exceed actual wire bytes. Refusing early is the safe
   direction.
7. **The persisted floor is still a high-water mark.** It prevents a delayed or
   lower reading from lowering enforcement within the UTC month; it is not a
   second byte ledger.

## 13. Live Render verification

**NOT PERFORMED.** This workspace has no `RENDER_API_KEY`, no
`RENDER_SERVICE_ID` and no Render shell/API access. The actual deployed metric
shape, the deployed commit and a live hard-limit refusal were not observed.
Production verification requires the deployment to pick up this commit and the
owner to inspect the bounded `OUTBOUND_MONTHLY_HARD_LIMIT_DENIED` and existing
`OUTBOUND_POLICY_SNAPSHOT` lines.

## 14. Git delivery

- Base: `0ae8434bed4ffa65ce7c5f94a7b8b75b49d5e0c9`
- Implementation + tests commit: `c140f92d571a733efe6ff338744f1e8e6a7d48b3`
- Push result: `0ae8434..c140f92  HEAD -> main` (normal fast-forward; no force,
  no rebase, no history rewrite)
- Verified remote `origin/main`: `c140f92d571a733efe6ff338744f1e8e6a7d48b3`
  (`git ls-remote` and a fresh fetch both returned it; `git merge-base
  --is-ancestor c140f92 origin/main` exited 0)
- Cumulative diff of that commit against the base: the four implementation/test
  files listed in section 3, nothing else.

This report is delivered in the immediate child commit of
`c140f92d571a733efe6ff338744f1e8e6a7d48b3`; the final remote HEAD after that
report commit is reported in the delivery summary.

The shared chat worktree's local branch remains at its pre-existing
`c55efcdfd1905d72df35810cb5669e9d5c66866e` with unrelated modifications
(`INVESTIGATION.md`, `LIFEOS_IDEAS.md`, `ROADMAP.md`, three handler/service
files and two test files). Those changes overlap newer upstream content, so
they were preserved untouched and the delivery was built and tested from an
isolated extraction of `origin/main`. No unrelated file was staged, committed,
stashed, reset or discarded.
