# Implementation Report — LifeOS Telegram Self-Bot

## Execution 20 — Remove ai_status + Per-Message Details

### Task / Result

Remove the obsolete Telegram `ai_status` surface (redundant with
Overview/Health/Usage) and add per-message AI Details so an individual
AI response can expose honest execution facts through the existing
Details panel. **Result: IMPLEMENTED.**

### Exact files changed

- `backend/bot/handlers/ai.py`
  - **Removed:** `_ai_status_panel_handler`, `_ai_status_inline_builder`,
    `_ai_status_refresh_action`, their registrations
    (`register_panel("ai_status")`, `register_inline_builder("ai_status")`,
    `register_action("ai_status_refresh")`), the module-docstring line,
    and the three `"📊 Status" → panel:ai_status` entry buttons (post-pick
    redirect, Test-Models error path, Test-Models success path) — all
    repointed to `Overview → panel:ai`.
  - **Added:** `_parse_msg_id(extra)` (positive msg-id parser) and
    `_render_per_message_details(resolved)` (one-message Details renderer);
    `_ai_details_panel_handler` now accepts `extra` = Telegram msg id
    (`panel:ai_details:<msg_id>`): when the ReplyResolver maps that id it
    renders THAT message's facts; otherwise it falls back to the existing
    latest-`AIExecutionRecord` behavior.
- `backend/ai/context/reply_resolver.py` — `ResolvedAIContent` and
  `ReplyResolver.register` extended with optional per-message fields:
  `input_tokens`, `output_tokens`, `total_tokens`, `token_source`,
  `latency_s`, `retry_count`, `fallback_used` (all defaulted, so every
  existing caller compiles unchanged).
- `backend/bot/handlers/ai_unified.py` — the register call site now
  populates those fields from the normalized `EngineResult` +
  `metadata` (`token_source`, `retry_count`, `fallback_used`).
- `tests/test_13_model_selection.py`, `tests/test_14_tool_honesty_glass.py`
  — pins updated: `panel:ai_status` → `panel:ai` (Overview) and
  `ai_status_refresh` removed from the preserved-actions list; the
  absence of the obsolete surface is now asserted (not weakened).
- `tests/test_43_ai_per_message_details.py` — **NEW**, 16 tests.
- `IMPLEMENTATION_REPORT.md` — replaced with only this report.

### ai_status removal behavior

The Telegram status panel is gone with zero dead call sites: the three
entry buttons point at the Overview, registrations are removed, and the
module no longer references `ai_status`. The **internal** observability
`ai_status` (runtime_status / crash_report / crash_diagnostics — runtime
execution state, unrelated to the Telegram surface) is intentionally
untouched and covered by a regression test.

### Per-message Details behavior

- Renders from the ReplyResolver mapping for the requested Telegram
  message: Model, Provider, Status (Ready), Tokens, Latency, Retries,
  Backup (fallback), When.
- **Honest fields:** `token_source=actual` shows exact in/out counts;
  `estimated` appends `≈`; `unavailable` (or an unknown/empty source
  with zero totals) renders `Unavailable` — never fabricated zeros.
  Latency `0` renders `—`. Retry count and fallback render only from the
  recorded execution facts.
- **No leaks:** no HTTP codes, tracebacks, API keys, provider bodies, or
  quota/reset claims appear; cooldown text is not part of this surface
  (Health owns cooldown, and only when proven).
- **Zero-spam:** the Details panel returns one render payload (edit-in-
  place); no extra messages, no background polling, no second Details
  architecture. When Details are not requested (no msg-id extra), the
  normal latest-record behavior is unchanged, and the AI response flow
  itself is untouched.

### Intentionally untouched

Provider retry/fallback · providers · token accounting · telemetry
contract · ai_usage/ai_provider_stats persistence · memory · Save ·
Ghost Room · model selector · dashboard/frontend · RuntimeSupervisor ·
watchdog · runtime observability (`runtime_status._ai_status`) ·
database schema/migrations.

### Database / schema impact

None. No SQL, no migrations, no schema changes — per-message Details
derive entirely from existing in-memory telemetry + ReplyResolver.

### Tests actually run and exact results

- `tests/test_43_ai_per_message_details.py` — **16 passed** (no
  production consumer of ai_status; internal observability intact;
  Overview entry replaces Status; per-message render for normal
  execution; provider/model correct; estimated marker; unavailable not
  fabricated; unknown source not claimed actual; retry/fallback
  represented; no fabricated cooldown/quota; no raw internals/secrets;
  edit-in-place single render; resolve-miss falls back to latest record;
  no-extra uses latest record; msg-id parsing; legacy register defaults).
- `tests/test_13_model_selection.py` + `tests/test_14_tool_honesty_glass.py`
  — **29 passed** (updated pins).
- Full suite — **713 passed, 0 failed, 1 warning** (baseline 697 + 16;
  pre-existing multipart deprecation).
- `python3 -m compileall -q backend` — PASS.
- `git diff --check` — PASS.
- Stale-reference search — only intentional absence-assertions in tests
  and the separate internal observability symbol remain.
- Duplicate-handler search — one Details panel handler + one render
  helper; no duplicates.

### Validation limitations / known remaining work

- Per-message mappings are RAM-only (existing bounded ReplyResolver
  design) — they survive the process, not restarts.
- The per-message entry point is available to the existing panel/callback
  infrastructure (`panel:ai_details:<msg_id>`); a reply-triggered
  "details" routing was deliberately NOT added in ai_unified to respect
  the project's no-text-commands rule. The compact chat telemetry line
  remains the primary per-message surface.

### Commit / push / remote verification

- **Commit:** (filled at delivery)
- **Push:** pushed to `origin/main`; `git fetch origin` → local HEAD ==
  origin/main.
- **Final working-tree status:** clean.
