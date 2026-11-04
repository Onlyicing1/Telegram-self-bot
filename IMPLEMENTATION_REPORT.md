# IMPLEMENTATION_REPORT.md — Test Modules failure diagnostics

> This file describes ONLY the current state of the repository for this task and
> replaces the previous report in full. Earlier unrelated reports remain in git.

| Item | Value |
|---|---|
| Task | Make every Test Modules failure diagnosable enough to choose a model honestly |
| Branch / base | `main` at `8873b4bbafb96c206d937ff420671e9948b7ed27` |
| Date | 2026-11-04 |
| Live Telegram / Gemini / Supabase / Render | **not touched** — no key, no RPC, no SQL, no migration, no `.env`, no deployment |
| Documents not modified | `DATABASE_ARCHITECTURE.md`, `README.md`, `AGENTS.md` |
| Interpreter | CPython 3.12 venv (`telethon==1.43.0` pinned) |
| New dependency | **none** |

## 1. What was missing

The result payload already carried `http_status`, `retry_after`, `error_type`
and `provider_code`, but the All Results view showed none of the provider
attribution: no provider code, no provider status, no operation/endpoint, no
provider message, and `error_type` was silently the provider type. Gemini also
captured Google's structured error and then discarded `details[].reason`,
ignored `Retry-After`, and logged an unredacted message. Classification
converted every HTTP 404 (and any "not found" text) into `INVALID_MODEL`.

## 2. What changed

**`backend/ai/redaction.py` (new, 30 lines)** — one redaction path shared by the
provider (before logging) and the tester (before the payload): `sk-`, `gsk_`,
`ms-key-`, `Bearer …`, `key=…` and `x-goog-api-key: …` become
`***REDACTED***`.

**`backend/ai/providers/gemini.py`** — on any failure it now records
`operation` (`generateContent`), `api_version` (`v1beta`), the endpoint path
(`models/<resolved-id>:generateContent`, never the URL/query/key), Google's
`code` and `status`, a bounded sanitized `provider_error_message`, up to five
bounded `provider_error_reasons` from `details[].reason`, and a
`failure_origin` (`model_lookup`, `authentication`, `request_validation`,
`rate_limit`, `server`, `endpoint`, `provider`). `Retry-After` is parsed
(0–3600s, 5s fallback). The same identity is attached to successful responses,
timeouts and network errors. Logs contain the sanitized bounded message only.

**`backend/ai/model_tester.py`** — `sanitize_error_message` now delegates to the
shared redactor; messages are bounded to 300 chars. `_classify_failure` turns a
404 into `INVALID_MODEL` only when the body/status actually names a missing
model; otherwise it reports `PROVIDER_ERROR` with an explicit
"no model-not-found evidence" reason. A 400 is never a model verdict. Result
dicts carry `requested_model` (what the owner selected) and `model` (the
deprecation-resolved id actually sent), plus `provider_error_type`,
`provider_error_message`, `provider_error_reasons`, `failure_origin`,
`operation`, `api_version`, `endpoint`; `error_type` is the failure/transport
type, no longer overwritten by the provider status. Timeout/exception paths
carry the same keys with honest nulls.

**`backend/bot/handlers/ai.py`** — All Results renders, per non-AVAILABLE model:
`HTTP <n> · code <c> · <PROVIDER_STATUS> · <origin>`, the endpoint with version,
`retry-after`, provider reasons, and one bounded sanitized message. A code/type
equal to the HTTP status is not repeated; missing metadata renders nothing; the
compact selection view, grid, pagination and callbacks are untouched.

**Tests** — extended `tests/test_gemini_model_discovery.py` (structured
404/400/401/403/429/500, resolved-vs-requested ids, Retry-After, redaction +
bound, non-JSON 429), `tests/test_model_tester.py` (bare-404 and 400
misclassification guards, payload attribution, missing metadata, sanitation)
and `tests/test_test_modules_grid.py` (All Results fields, retry-after/reason,
missing metadata, bounded message, only AVAILABLE selectable).

## 3. Contracts preserved

* Only `status == "AVAILABLE"` results are selectable and only they feed the
  existing `ProviderManager.set_model_candidates` pool.
* No automatic model selection; discovery alone still qualifies nothing.
* `_render_test_results` grid geometry, pagination, callback data, current-model
  line and the summary block are unchanged.
* Cohere, Groq and every other provider keep their existing target/fallback and
  classification behaviour; no model-ID migration.
* No second tester, workflow, scheduler or provider architecture.

## 4. Verification executed

| Check | Command | Result |
|---|---|---|
| Gemini discovery/tester tests | `pytest tests/test_gemini_model_discovery.py -q` | **27 passed** |
| Model tester tests | `pytest tests/test_model_tester.py -q` | **36 passed** |
| Test Modules grid/All Results | `pytest tests/test_test_modules_grid.py -q` | **21 passed** |
| Focused combined | the three files | **84 passed** |
| Full suite | `pytest tests -q --deselect tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once` | **6875 passed, 27 skipped, 1 deselected** (140.5s) |
| Compilation | `python -m py_compile` on the four changed modules | exit 0 |
| Whitespace | `git diff --check` | clean |

The deselected test is the pre-existing date-bound scheduler failure
(`interval advancement exceeded bound`), unrelated to this change and failing
identically before it.

## 5. Not live-verified

No Gemini key was used, so the real `/v1beta/models`/`generateContent` responses
for the owner's account, the actual `Retry-After` header, Google's exact
`details[].reason` values, and the Telegram rendering of the panel are
unverified. Tests use documented Google shapes and fakes.

## 6. Residual limitations

* `failure_origin` is derived from HTTP status + Google status/message; it is a
  faithful heuristic, not a provider-declared field.
* Non-Gemini providers do not emit `operation`/`endpoint`; the renderer omits
  those lines rather than inventing them.
* Provider reasons are shown only when a provider supplies them (Gemini does).
* A live run is still required to determine why a specific Gemini model fails
  on the owner's key; this change makes that reason visible, it does not fix an
  unknown provider-side cause.
