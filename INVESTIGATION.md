# INVESTIGATION — Test Modules diagnostic visibility for model failures

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch / HEAD at start | `main` at `8873b4bbafb96c206d937ff420671e9948b7ed27` |
| Date | 2026-11-04 |
| Scope | the failure path of one model test: `GeminiProvider.chat()` → `test_single_model()` → `_classify_failure()` → Test Modules payload → All Results renderer |
| Out of scope | model-ID migration, provider architecture, `ProviderManager`, Supabase/SQL, Render/env, unrelated providers |
| Live account / API key used | **no** — no Telegram session, no Gemini key, no network call |
| Status | Diagnostic gaps CONFIRMED in source and closed; Gemini's live failure cause remains UNKNOWN (not observable here) |

## 1. Symptom

The owner selects models manually from Test Modules, so a row is only useful if
it says WHY it failed. The reported row (`gemini-2.0-flash — INVALID_MODEL`)
carried no HTTP status, no Google error code/status, no endpoint and no
provider message in the detailed view, so an invalid model, a wrong endpoint,
a key restriction and a bad request were indistinguishable. The earlier Gemini
catalog work fixed which model is tested; it did not make the failure itself
diagnosable.

## 2. Method and its limits

* Every claim below was re-read from the working tree at `8873b4b`.
* Deterministic unit tests exercise the chain with mocked provider responses
  (structured Google bodies, missing bodies, hostile text). No live call was
  made: there is no API key in this workspace and no Telegram session.
* Consequence: the display, attribution and classification behaviour is
  proven; the actual cause of any specific production failure is **not**
  established by this work.

## 3. The path that was reviewed

```
GeminiProvider.chat()            # Google response -> ProviderResponse.metadata
  -> test_single_model()         # resolve id, classify, build result dict
     -> _classify_failure()      # status + human error
        -> _last_test_payload   # cached run payload
           -> _ai_test_details_action()   # "All Results"
              -> _render_test_results()   # compact grid (unchanged contract)
```

## 4. Confirmed findings

**F1 — The detailed view discarded captured fields.** `_ai_test_details_action`
rendered status, model, latency, `HTTP`, `retry-after` and `error[:160]` only.
`provider_code` and `provider_error_type` were already collected but never
shown, so Google's `NOT_FOUND` / `INVALID_ARGUMENT` / `UNAUTHENTICATED` /
`RESOURCE_EXHAUSTED` distinction was invisible.

**F2 — Two different facts shared one name.** The tester stored
`error_type = metadata["error_type"] or metadata["provider_error_type"]`, so a
local transport class and a provider's own status were conflated and the
provider type was overwritten by whichever arrived first.

**F3 — 404 was treated as a model verdict unconditionally.** `_classify_failure`
returned `INVALID_MODEL` for any HTTP 404, and also for any error text
containing "not found" at ANY status. A 400 request-validation error whose body
said "method not found" would therefore be reported as an invalid model,
blaming the model for a request/endpoint defect.

**F4 — Gemini parsed structured errors but threw the evidence away.**
`GeminiProvider.chat()` read `error.message`, `error.code` and `error.status`,
then dropped `error.details[].reason` (e.g. `API_KEY_INVALID`), hardcoded
`retry_after = 5` while ignoring Google's `Retry-After` header, and recorded
neither the operation (`generateContent`), the API version (`v1beta`) nor the
endpoint path. The raw message was truncated to 200 chars and logged with no
redaction, even though a provider can echo a credential into an error body.

**F5 — No bounded, sanitized provider message existed in the payload.** The
panel rendered `error[:160]` (already a classification string), never the
provider's own message, and nothing bounded what a provider could contribute.

## 5. What is established — and what is not

Established by source and deterministic tests:

* The failure chain preserves what the provider returns (status, code, type,
  message, retry-after) and the tester classifies from those facts.
* A 404 is now a model verdict only with model-not-found evidence; otherwise it
  is an honest provider/endpoint error.
* Auth, request-validation, rate-limit, quota and server failures never become
  `INVALID_MODEL`.
* Credentials are redacted at the provider (before logging) and messages are
  bounded before they can reach the panel.

Not established:

* The real cause of the owner's Gemini `INVALID_MODEL` observation. It could be
  a genuinely unavailable model, an endpoint restriction, or a key/project
  limitation. Only a live run with the owner's key can answer it — and the new
  All Results view is what makes that answer readable.
* Google's live response shapes for this account (the tests use documented
  shapes, not captured traffic).
