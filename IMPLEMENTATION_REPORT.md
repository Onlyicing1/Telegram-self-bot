# IMPLEMENTATION REPORT — CURRENT STATE

> **This is a CURRENT-STATE document.** It describes the repository as it
> exists at the tip of this phase. If code changes invalidate any section,
> update this document in the same commit.

---

## 1. Implementation metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Starting HEAD | `1edb0e989a45f67aef1e86f01a0016777e82b8db` (`== origin/main` at phase start) |
| Phase | **NaraRouter probe diagnostic instrumentation** — the discovery fix shipped in `1edb0e9` is preserved unchanged; this phase adds a sanitized probe/classification trace so a live "Invalid Key" report can be explained |
| Status | **IMPLEMENTED — full suite green (2174 passed, 24 skipped, 0 failed)** |
| Database impact | **NONE** (no schema, migration, RLS, table, or configuration change) |
| Live Render verification | **NOT performed** (no production credentials/telemetry access in this workspace) — see §7 |
| Delivery record | see §8 |

---

## 2. Reported behavior

Original report (previous phase): after setting `AI_NARAROUTER_API_KEY` in the
Render environment, the NaraRouter provider — and therefore its models — did
not appear in the Telegram AI provider/model list, even though the key was
configured.

Current report (this phase): NaraRouter now appears in the provider panel but
**under "Invalid Key"** in the running Render instance. The HTTP status (or
transport exception) that production actually produced is **not yet known**;
this phase adds the trace that will reveal it.

---

## 3. Root cause (source-traced, previous phase)

NaraRouter is wired end-to-end and was never the missing piece. The first and
only point where it disappeared was **provider discovery classification**.

1. `backend/ai/discovery.py::_PROVIDERS` declares `nararouter` with
   `env_vars = ["AI_NARAROUTER_API_KEY", "NARAROUTER_API_KEY"]`, default base URL
   `https://router.bynara.id/v1`, and default model `deepseek-v4-flash`.
2. `_scan_provider` detects the key and returns status `"detected"`.
3. `_validate_provider` probes `GET {base_url}/models` with `Bearer` auth. The
   client is created **without** `follow_redirects`, and httpx defaults
   `follow_redirects=False` (verified in this workspace).
4. **Old** classification: any status other than `200` → `_make_invalid`; any
   exception (timeout, connection error, ...) → `_make_invalid`. Nothing
   distinguished a genuine auth rejection from a redirect, rate limit, server
   error, or transport failure.
5. `backend/bot/handlers/ai.py::_ai_provider_panel_handler` renders selectable
   rows **only** for `status == "available"`; `invalid` providers are listed
   separately under "Invalid Key" with no selection row.

So a configured key whose `/models` probe could not complete cleanly was
reported as a bad key and removed from the selectable provider list — the exact
reported symptom. Everything downstream (factory `_ENV_KEY_MAP` /
`_ENV_MODEL_MAP` / `_ENV_BASE_URL_MAP`, `_PROVIDER_DEFAULTS`, the model
fallback catalog, the display-name map) already registered NaraRouter.

Supporting source facts:

- `GET https://router.bynara.id/v1/models` returns **401** unauthenticated —
  the endpoint exists and requires auth; a 200 is expected only for a valid key.
- httpx `AsyncClient` default `follow_redirects=False` — a 3xx from the gateway
  was therefore surfaced as `status_code=3xx` and misread as an invalid key.

---

## 4. Exact fix (previous phase, preserved unchanged)

`backend/ai/discovery.py` only — no key, base URL, or model value was changed.

- Added `_AUTH_REJECT_STATUSES = frozenset({401, 403})` — the only probe
  outcomes that genuinely mean "this key is rejected".
- Added `_classify_probe(status, http_status)`:
  - `200` → `available`, `validated=True`;
  - `401`/`403` → `invalid` (the owner must fix the key);
  - every other status → `available`, `validated=False`.
- `_validate_provider`'s `except Exception` path now keeps the provider
  `available` with `validated=False` instead of marking it `invalid`.
- `_make_available(status, verified: bool = True)` propagates the honest
  "probe could not confirm" signal through `ProviderStatus.validated`, so the
  provider stays visible and selectable without claiming verification.

The fallback/degradation semantics are unchanged: only a real authentication
rejection is reported as invalid.

---

## 5. Diagnostic instrumentation (this phase)

**Where "invalid" can come from (re-verified at HEAD).** For chat providers,
`ProviderStatus.status == "invalid"` is produced in exactly one place:
`discovery._make_invalid`, and that is reachable only from
`discovery._classify_probe` when the probe returns 401/403. The "Invalid Key"
section in `backend/bot/handlers/ai.py` renders exclusively from that status.
Therefore a live "Invalid Key: NaraRouter" line means either the probe really
returned 401/403 from the gateway, or the running instance predates `1edb0e9`.
The trace below distinguishes the two cases without guessing.

**Added to `backend/ai/discovery.py` (logging only — classification semantics,
timeouts, clients, retries, env names, and provider configuration are all
unchanged).** Each chat-provider probe now emits exactly one INFO line tagged
`PROVIDER_DISCOVERY_PROBE` (`LOG_LEVEL` default is `INFO`, so it reaches the
Render logs on every discovery refresh):

| Field | Meaning |
|---|---|
| `provider` | provider name (e.g. `nararouter`) |
| `probe_url` | **sanitized** probe URL: scheme, host, port, path only — query string and credentials dropped |
| `http_status` | the received status code, or `none` when no response arrived |
| `classification` | `available` / `available_unverified` / `invalid` |
| `validated` | `True` only for HTTP 200 |
| `detail` | `probe_ok` · `auth_rejected` · `non_auth_http_status` · `transport_error …` |

For a transport failure the detail additionally carries
`exc_type=<ExceptionClass>` (the actual failure class, e.g. `ConnectTimeout`,
`ConnectError`, `OSError`), `cause=<Class>[>Class]…` — the type-only chain of
`__cause__`/`__context__` (bounded to 4, so the underlying cause that
determines the failure is visible) — and a bounded, sanitized
`message='…'`.

**Sanitization guarantees (never logged):** API keys (the exact configured key
is replaced with `***`; `?…` query strings are replaced with `?<redacted>`,
covering the Gemini `key=` parameter), Authorization headers, request headers,
cookies, response bodies, and arbitrary user content. Messages are
whitespace-collapsed and truncated to 200 characters. Only exception class
names — never nested exception messages — are traced for the cause chain.

**Reading the trace after deploy:**

| Observed trace | Conclusion |
|---|---|
| `http_status=401` / `403`, `classification=invalid`, `detail=auth_rejected` | The gateway genuinely rejected the key; the key/env value is wrong or expired (and the provider panel is behaving correctly). |
| `http_status=200`, `validated=True` | The probe succeeds; any remaining display issue is elsewhere. |
| `http_status=<other>`, `classification=available_unverified` | Non-auth outcome; the provider stays listed (post-`1edb0e9` behavior). |
| `http_status=none`, `detail=transport_error …` | No response arrived; `exc_type`/`cause` identify timeout vs connection/redirect/transport failure. |
| **No `provider=nararouter` line at all** | The probe never ran — no key was detected in that process (stale env/deploy) or the instance predates `1edb0e9`. |

No claim is made here about the production cause until that trace exists.

---

## 6. Tests

`tests/test_nararouter_provider.py` — the 8 discovery tests from the previous
phase are preserved, plus 10 new instrumentation tests (all in-process, HTTP
mocked, no live credentials):

| Behavior | Test |
|---|---|
| Probe URL/text sanitizers strip credentials and query strings | `test_probe_url_and_text_sanitizers_strip_credentials` |
| `200` trace: `http_status=200`, `classification=available`, `validated=True`, `detail=probe_ok` | `test_probe_trace_reports_verified_classification` |
| `401`/`403` trace: `classification=invalid`, `validated=False`, `detail=auth_rejected` | `test_probe_trace_shows_auth_rejection_caused_invalid` |
| `302`/`429`/`500`/`503` trace: `classification=available_unverified`, `detail=non_auth_http_status` | `test_probe_trace_shows_non_auth_status_available_unverified` |
| Transport failure trace: `http_status=none`, `exc_type` and nested `cause` classes | `test_probe_trace_shows_transport_exception_type_and_cause` |
| The API key never appears in the trace (including when an exception message echoes it) | `test_probe_trace_never_contains_credentials` |

---

## 7. Verification and limitations

- `python -m py_compile backend/ai/discovery.py tests/test_nararouter_provider.py` — OK.
- `git diff --check` — clean.
- Focused NaraRouter suite — **42 passed** (32 before; +10 instrumentation tests).
- **Full suite (`pytest tests`): `2174 passed, 24 skipped, 0 failed`**
  (previous tip: 2164 passed / 24 skipped).
- Diff scope: `backend/ai/discovery.py` + `tests/test_nararouter_provider.py` +
  this report only.

**Live Render verification: NOT performed.** What remains unverified in
production:

1. The exact HTTP status or exception the Render egress produces for
   `https://router.bynara.id/v1/models`, and therefore which classification the
   live instance reaches.
2. That the deployed revision contains `1edb0e9` (the previous phase) and this
   instrumentation commit.

This phase deliberately does not change classification behavior, so it cannot
alter the production outcome — it only makes the production outcome observable.

---

## 8. Delivery record

| Item | Value |
|---|---|
| Starting HEAD | `1edb0e989a45f67aef1e86f01a0016777e82b8db` (`== origin/main` at start) |
| Change set | `backend/ai/discovery.py` + `tests/test_nararouter_provider.py` + this report |
| Commit | `feat: trace NaraRouter discovery probe classification` — pushed to `origin/main`, remote SHA verified after push |
| Database impact | NONE |
| Previous phase (record) | `1edb0e9` — `fix: expose configured NaraRouter providers` |
| Live Render / Telegram verification | **NOT performed** — the owner reads the `PROVIDER_DISCOVERY_PROBE` line for `nararouter` after the next deploy |
