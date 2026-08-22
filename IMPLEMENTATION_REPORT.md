# Implementation Report — LifeOS Telegram Self-Bot

## Execution 18 — Provider Reset / Cooldown Surfacing

### Task / Result

Expose the provider layer's *proven* rate-limit recovery information
(cooldown/Retry-After) to the existing AI Health UX — without ever
fabricating quota/reset times, since providers expose no such metadata.
**Result: IMPLEMENTED.**

### Exact files changed

- `backend/ai/providers/manager/health.py` — `ProviderHealthTracker` now
  remembers the normalized failure category that triggered the current
  recovery state (`_last_failure_category`, set in `record_failure`,
  cleared on `record_success`/`mark_healthy`) and exposes it via
  `last_failure_category(name)` (empty when unknown — callers must not
  guess).
- `backend/ai/providers/manager/manager.py` — new read-only accessor
  `reset_state(provider) -> {provider, available, state, reason,
  cooldown_remaining_s, quarantine_remaining_s}`. Only what the tracker
  can prove; never a quota/credit reset window.
- `backend/bot/handlers/ai.py` — Health panel appends a compact recovery
  line when the active provider is actually cooling down:
  `Rate limited · retry in ~45s` (reason == `rate_limited`) or
  `Recovering · retry in ~3m` (other proven categories). New helpers
  `_get_provider_recovery` (defensive, ignores non-dict stubs) and
  `_format_cooldown` (rounded up so the wait is never understated).
  Edit-in-place only — zero new messages.
- `tests/test_41_reset_cooldown.py` — **NEW**, 10 tests.
- `IMPLEMENTATION_REPORT.md` — replaced with only this report.

### Exact implementation behavior

- Rate-limited provider with a real short cooldown → Health shows
  `Rate limited · retry in ~Ns/Mm` only while the tracker's monotonic
  cooldown is actually running; after success the reason and line clear.
- No reset information (healthy/unknown provider, expired cooldown) →
  `reset_state` reports `available=True`, zero remaining; the panel shows
  no recovery line. Nothing is fabricated.
- Existing bounded retry policy untouched: short Retry-After windows
  (≤5s) still get exactly one bounded retry; long windows still fail
  over without waiting; cooldowns still clamp to the documented 300s
  maximum. No new retry loop, no background polling, no provider-specific
  reset guessing.

### Intentionally untouched

Provider retry policy · provider implementations · memory · ai_usage /
ai_provider_stats persistence · telemetry contract · token-source
semantics · Save · Ghost Room · model selector · frontend ·
RuntimeSupervisor · watchdog · unrelated handlers · database schema ·
migrations.

### Database / schema impact

None. No SQL, no migrations, no schema changes — this chunk is purely
in-memory runtime state + UI.

### Tests actually run and exact results

- `tests/test_41_reset_cooldown.py` — **10 passed** (rate-limit cooldown
  honesty, accessor shape + no-quota-key guard, unknown/healthy
  available, short Retry-After one-retry unchanged, long Retry-After
  failover unchanged + 300s clamp, retry counts correct + reason cleared
  on recovery, Health panel rate-limit line, Health panel never
  fabricates a time, MagicMock engine safety, cooldown formatting).
- Full suite — **688 passed, 0 failed, 1 warning** (baseline 678 + 10;
  the warning is the pre-existing multipart deprecation).
- `python3 -m compileall -q backend` — PASS.
- `git diff --check` — PASS.
- Stale-call-site search: only the new accessor references; no duplicate
  functions/handlers.

### Validation limitations / known remaining work

- The Health panel surfaces cooldown only for the active provider;
  per-candidate mesh cooldowns remain visible via existing telemetry
  (Details), not this line.
- Providers still expose no quota/reset metadata — nothing here can or
  will claim account-level resets.

### Commit / push / remote verification

- **Commit:** `bd00db9`
- **Push:** pushed to `origin/main`; `git fetch origin` → local HEAD ==
  origin/main.
- **Final working-tree status:** clean.
