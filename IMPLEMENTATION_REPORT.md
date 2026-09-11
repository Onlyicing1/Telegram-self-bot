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
| Starting HEAD | `76e988c2f6f2edad3299e07c33446e5a15b3d5fd` (== `origin/main` at phase start) |
| Phase | **Test Modules** — show the runtime-active model, adaptive 2/3-column usable-model grid, production fallback fed only by models Test Modules proved usable, and a provider model-discovery audit (OpenRouter free models) |
| Status | **IMPLEMENTED — full suite green (2146 passed, 24 skipped, 0 failed)** |
| Database impact | **NONE** |
| Live provider / Telegram verification | **NOT performed** (no credentials in this workspace) — see §7 |
| Delivery record | see §8 |

---

## 2. Source-verified starting state (before this phase)

Traced end to end in the current source:

```
Menu → AI → Test Modules
  → _ai_test_models_action (backend/bot/handlers/ai.py)
  → ai_test_progress.run_streaming_test (background, guarded)
  → model_tester.test_all_models_streaming
  → _build_targets  (discovery → per-provider targets + display payload)
  → _render_test_results  (single shared results view)
```

- The results view had **no current-model line**; it rendered a flat
  `usable_sorted[:12]` list, one button per row, with **no pagination**.
  A usable set larger than 12 was silently truncated in the UI.
- `test_all_models_streaming` fed the production model-level pool
  (`ProviderManager.set_model_candidates`) with the **complete raw discovery
  set** per provider — discovery alone made a model eligible, even though
  discovery does not prove a model can answer.
- Display ordering was `provider's raw /models order` (alphabetical by
  name). The response payload was capped at `_MODELS_IN_RESPONSE = 30` per
  provider and the diagnostic target list was truncated by
  `_GLOBAL_TEST_BUDGET` (default 60) — both applied to that raw order.
- `_fetch_gemini_models` read **only the first page** of Gemini's `/models`
  contract and ignored `nextPageToken`.
- `is_free` (OpenRouter-style pricing metadata) already existed in
  `ModelInfo`/`order_models_for_selector` but was never surfaced in the UI
  and was never used to order what gets tested.

---

## 3. Exact changes

### 3.1 Show the CURRENT runtime model

| Piece | Location |
|---|---|
| `_runtime_pair()` | resolves `(provider, model)` from `_get_engine_info()` — the ProviderManager (`get_active().name` + `get_provider_config(name).default_model`), the **same authoritative pair** the dispatcher's runtime context reads (`dispatcher._build_context`). Never the persisted config. |
| `_current_model_line()` / `_runtime_pair_line()` | compact `Current: <provider> · <model>` (Unicode only, no emoji); honest `Current: unavailable` when the runtime reports nothing. |

Surfaced in every Test Modules surface:

- results view (`_render_test_results`),
- launch view and the "already running" notice (`_ai_test_models_action`),
- the streaming progress view (`ai_test_progress.render_progress_view` — the
  current line is the **first** line, above the preserved five-segment bar).

The active runtime model keeps its normal position in the grid and is marked
in place (`◉`) in both the body list and its button — never duplicated — and
records that carry authoritative pricing metadata show a `·free` tag.

### 3.2 Adaptive Test Modules grid

`backend/bot/handlers/ai.py`:

```python
_TEST_GRID_2COL_MAX = 12
_TEST_GRID_ROWS_PER_PAGE = 6
_test_grid_columns(n)   # 2 columns while n <= 12, 3 columns once it exceeds one full 2-column page
_test_grid_page_size(c) # c * 6 buttons per page
```

- 12 is the **historical single-view capacity** (the previous hard
  `[:12]` slice with no paging); the panel keeps 6 rows and **widens**
  instead of growing taller — 2×6 normally, 3×6 once the usable set no
  longer fits a two-column page.
- `_usable_results()` is the single shared ordering (provider-grouped, then
  fastest first) used by both the renderer and the pager, so the two can
  never disagree.
- Pagination renders only when the usable set spans more than one page:
  `‹ Prev` / `n/N` / `Next ›`, wired to `action:ai_test_page:<n>` →
  `_ai_test_page_action`, which re-renders the **cached** last payload. It
  never re-runs the tests, never mutates the cached results, and clamps an
  out-of-range page instead of rendering an empty view.
- Summary line, existing actions (`↻ Re-run Tests`, `⌕ All Results`,
  `≡ Pick Model`, Overview, nav) and the no-emoji visual language are
  unchanged.

### 3.3 Production fallback = models Test Modules proved usable

`backend/ai/model_tester.py`:

- `annotate_is_free(results, complete_models, providers_status)` — tags each
  result's `is_free` from discovery pricing metadata only ($0 prompt **and**
  $0 completion). A name containing "free" never sets it; absent metadata
  means **not** free.
- `proven_usable_candidates(...)` — provider → model ids whose status is
  exactly `AVAILABLE`, ordered by the same deterministic free-first selector
  order, with the provider's configured/default model dropped (the router
  already tries it first) and `dummy` never a candidate.
- `_feed_production_candidates(...)` — publishes that map through the
  **existing** `ProviderManager.set_model_candidates` contract (runtime
  owned). A provider with no `AVAILABLE` model gets an **empty** list, never
  the raw discovery set; providers without a key and `dummy` are skipped.
- The old discovery-fed pool in `test_all_models_streaming` is replaced, and
  the same feed was added to the batch `test_all_models` path so both entry
  points agree. The display cap and the diagnostic budget bound **test
  execution and display only** — never what production may fall back to.
- `_build_targets` orders discovered models **free-first** (shared
  `order_models_for_selector`) for both the display payload and the test
  target list, so a genuinely free catalog is reachable within the same
  explicit budget instead of sitting behind an alphabetically earlier paid
  catalog.

No second executor, scheduler, provider manager, discovery system, or
fallback system was added.

### 3.4 Provider model-discovery audit (requirement 4)

| Provider | `/models` behaviour | Paginates? | Finding |
|---|---|---|---|
| **OpenRouter** | complete catalog in one `{data:[...]}` response, per-model `pricing` | no | **Not** a discovery defect — `:free` ids were already retained (pinned by a test). The free models appeared "missing" because the raw alphabetical order + the 30-model display cap + the diagnostic budget decided what was tested/shown, and free candidates sorted late. Fixed centrally in `_build_targets` (free-first). |
| OpenAI, Groq, Mistral, Cerebras, zai, sambaNova, NVIDIA, Cohere (`compatibility/v1`), SiliconFlow, Fireworks, NaraRouter | complete `{data:[...]}` in one response | no | Same ordering/cap class as OpenRouter — no discovery-side truncation; fixed by the same free-first ordering. |
| **Gemini** | `nextPageToken` pagination | **yes** | Genuine discovery truncation: only page 1 was read. Fixed by following the chain (bounded by `_MAX_DISCOVERY_PAGES = 10`, deduplicated by model name). |
| `you` | search capability, not chat (`capability_kind="web_search"`) | n/a | Never a chat provider: excluded from discovery, testing and fallback. |

`is_free` remains **authoritative-metadata only**; no provider without a
pricing block is guessed to be free.

---

## 4. Database impact

**None.** No schema, table, migration, RLS, policy, or configuration change.
All state involved (discovery cache, last test payload, ProviderManager
candidate pool) is runtime/in-memory, exactly as before.

---

## 5. Tests

| File | Change |
|---|---|
| `tests/test_model_discovery.py` | +4 — OpenRouter `:free` retention & metadata-only `is_free`; free-first ordering is deterministic/lossless/non-mutating; Gemini follows every `nextPageToken` page; a single-response provider is fetched exactly once. |
| `tests/test_model_tester.py` | +5, 4 updated — only `AVAILABLE` results enter the pool; a provider with no usable model gets an empty list; `dummy` is never a candidate; free models are tested before paid under the budget; `is_free` annotation is metadata-driven. The two former "complete discovery set" assertions now assert the proven-`AVAILABLE` contract. |
| `tests/test_test_modules_grid.py` | **new**, 16 tests |

New UI coverage (`tests/test_test_modules_grid.py`) maps directly to the
requirements:

| Requirement | Test |
|---|---|
| Current pair from the runtime | `test_current_runtime_pair_is_rendered_from_the_provider_manager` |
| Never the persisted config | `test_current_line_ignores_a_divergent_persisted_config` |
| Honest unavailable state | `test_current_line_is_honest_when_the_runtime_reports_nothing` |
| Launch + progress surfaces | `test_launch_view_shows_the_current_runtime_model`, `test_progress_view_shows_the_current_runtime_model_first` |
| Active model marked in place (not duplicated) | `test_active_runtime_model_is_marked_in_place_not_duplicated`, `test_non_active_results_keep_the_plain_usable_marker` |
| Adaptive 2/3 columns + threshold | `test_normal_usable_set_uses_two_columns`, `test_large_usable_set_uses_three_columns`, `test_threshold_is_the_historical_single_view_capacity` |
| Paging keeps every model exactly once | `test_pagination_covers_every_model_exactly_once` |
| Pager is presentation-only | `test_pager_is_pure_presentation_and_never_reruns_tests`, `test_pager_clamps_an_out_of_range_page` |
| Stable unique callbacks / registration | `test_model_callbacks_are_unique_and_deterministic`, `test_the_pager_action_is_registered` |
| Existing actions + no-emoji rails preserved | `test_grid_keeps_the_summary_and_existing_actions_without_emoji` |

---

## 6. Preserved architecture (still current)

- **One discovery pass**, one ProviderManager, one executor, one scheduler,
  one recovery authority — unchanged.
- **Dummy is never in production routing or the model-level pool.**
- **Degraded-store honesty**, Taskloom, Bio/Username engines, Deep Save,
  delete semantics — untouched by this phase.
- Real task deletion + database-owned task ids from the previous phase remain
  as delivered.

---

## 7. Verification

- `python -m py_compile` on all changed Python files — OK.
- `git diff --check` — clean.
- **Full suite (`pytest tests`): `2146 passed, 24 skipped, 0 failed`**
  (previous tip: 2121 passed / 24 skipped; +25 from this phase).

**Live provider / Telegram verification: NOT performed** (no credentials in
this workspace). The owner's manual probes:

1. Open `Menu → AI → Test Modules`; the first line must show the same
   provider/model the AI request path is currently using.
2. Run the tests with more than 12 usable models; page two must appear, the
   grid must use three columns past the first page capacity, and every model
   must be reachable exactly once.
3. Confirm a `:free` OpenRouter model now appears (tagged `·free`) instead of
   being cut behind the paid catalog.
4. Pick a proven model, then confirm production fallback only ever tries
   models Test Modules reported as usable.

---

## 8. Delivery record

| Item | Value |
|---|---|
| Starting HEAD | `76e988c2f6f2edad3299e07c33446e5a15b3d5fd` (== `origin/main` at start) |
| Change set | `backend/ai/model_discovery.py`, `backend/ai/model_tester.py`, `backend/bot/handlers/ai.py`, `backend/bot/handlers/ai_test_progress.py`, `tests/test_model_discovery.py`, `tests/test_model_tester.py` (modified) + `tests/test_test_modules_grid.py` (new) + this report |
| Commit | `feat: improve model testing visibility and proven fallback pool` — pushed to `origin/main`, remote SHA verified after push |
| Previous phase (record) | `76e988c` — `fix: make task deletion durable and keep task IDs database-owned` |
| Working tree | pre-existing untracked stray clone `telegram-self-bot/` deliberately left untouched; no schema/migration files changed |
| Live Telegram / provider verification | **NOT performed** (no credentials in this workspace) — the owner runs the §7 probes manually |
