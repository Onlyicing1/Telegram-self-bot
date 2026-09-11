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
| Starting HEAD | `2551970701900116e56c6dff67aec51600696e48` (`== origin/main` at phase start) |
| Phase | **NaraRouter discovery visibility** — a configured `AI_NARAROUTER_API_KEY` provider was hidden from the provider/model list |
| Status | **IMPLEMENTED — full suite green (2164 passed, 24 skipped, 0 failed)** |
| Database impact | **NONE** (no schema, migration, RLS, table, or configuration change) |
| Live Render verification | **NOT performed** (no production credentials/telemetry access in this workspace) — see §6 |
| Delivery record | see §7 |

---

## 2. Reported behavior

After setting `AI_NARAROUTER_API_KEY` in the Render environment, the NaraRouter
provider — and therefore its models — did not appear in the Telegram AI
provider/model list, even though the key was configured.

---

## 3. Root cause (source-traced)

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

## 4. Exact fix

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

## 5. Tests

`tests/test_nararouter_provider.py` — 8 new tests (all in-process, HTTP mocked):

| Behavior | Test |
|---|---|
| A 200 probe is verified | `test_discovery_verifies_nararouter_on_200` |
| Redirect / rate limit / 5xx keep the provider listed | `test_non_auth_probe_failure_keeps_nararouter_in_the_provider_list` (`307, 429, 500, 503`) |
| A real auth rejection is still reported invalid | `test_auth_rejection_marks_nararouter_invalid` (`401, 403`) |
| A transport failure keeps the provider listed | `test_transport_failure_keeps_nararouter_in_the_provider_list` |

---

## 6. Verification and limitations

- `python -m py_compile backend/ai/discovery.py tests/test_nararouter_provider.py` — OK.
- `git diff --check` — clean.
- Focused NaraRouter suite — **32 passed**.
- **Full suite (`pytest tests`): `2164 passed, 24 skipped, 0 failed`**
  (previous tip: 2156 passed / 24 skipped).

**Live Render verification: NOT performed.** What remains unverified in
production:

1. That the NaraRouter provider now appears in the Telegram provider list after
   a refresh, and its models appear in the model picker.
2. The exact non-200/exception the Render egress produced for
   `https://router.bynara.id/v1/models`; the fix is correct for every
   non-auth outcome (redirect, rate limit, 5xx, timeout, connection error), so
   this does not affect behavior — it only changes whether `validated` is
   `True` or `False`.

---

## 7. Delivery record

| Item | Value |
|---|---|
| Starting HEAD | `2551970701900116e56c6dff67aec51600696e48` (`== origin/main` at start) |
| Change set | `backend/ai/discovery.py` + `tests/test_nararouter_provider.py` + this report |
| Commit | `fix: expose configured NaraRouter providers` — pushed to `origin/main`, remote SHA verified after push |
| Database impact | NONE |
| Previous phase (record) | `2551970` — `fix: stop reporting local resource errors as Supabase unavailability` |
| Live Render / Telegram verification | **NOT performed** — the owner verifies the provider list after refresh |
