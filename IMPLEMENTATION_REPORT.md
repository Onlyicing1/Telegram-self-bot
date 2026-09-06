# IMPLEMENTATION REPORT

## 1. IMPLEMENTATION METADATA

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` |
| Branch | `main` |
| Audited commit | `8b734976a2e1b9a5df463dad051c1dad4c3482b3` |
| Commit subject | `fix: expose canonical settings key contract to the AI (ai_model vs model)` |
| Parent commit | `1dda645b57068e5cba11c26bdde6ffde3313c384` (`test: isolate RC-5 regression tests from the shared settings cache`) |
| Audit date | 2026-09-06 |
| Implementation status | Delivered — `8b73497` verified on `origin/main` via `git ls-remote` during this audit |
| Report type | Implementation audit / documentation replacement (no production code changed by this report) |
| Audit basis | `git show` / `git diff 8b73497^ 8b73497`, current source files, tests executed during this audit. Commit messages and INVESTIGATION.md were NOT used as evidence sources; they were only compared against the diff. |

## 2. EXECUTIVE SUMMARY

Commit `8b73497` fixes the **model-facing settings key contract** (investigation finding RC-7 / F-3). A live AI request to change the model produced `settings_set {key: "ai_model", value: "gpt-oss-1200"}` and the backend correctly rejected it with `Unknown setting key 'ai_model'` — the RC-5 fail-closed behavior worked. The commit does not touch that routing, which was already correct. Instead it fixes the two source-level reasons the model invented the key:

1. `backend/ai/prompt/builder.py` rendered the replied-to AI message metadata as `AI Model: <model>` / `AI Provider: <provider>` — the only occurrence of the token sequence "AI Model" in the model-facing prompt. The commit relabels these lines to `Model:` / `Provider:`, matching the canonical setting keys and the existing `[Runtime Context]` block.
2. `backend/ai/tools/settings.py` — `SettingsGetTool` / `SettingsSetTool` described the `key` argument with no key enumeration ("The setting key to read/write."), so the model had to guess. The commit adds `_setting_key_contract()`, which derives the full valid-key list from the two pre-existing authorities (`_AI_CONFIG_KEYS` and `settings_service.known_keys()`) and carries it in both tools' `description` and `parameters["key"].description`, plus an explicit disambiguation: the AI model setting is key `'model'`, never `'ai_model'`.

It also adds `tests/test_settings_model_key_contract.py` (7 regression tests, 316 lines) and updates `INVESTIGATION.md` (RC-7) and `IMPLEMENTATION_REPORT.md` (§17). No alias is introduced — `ai_model` remains rejected as unknown. No routing, confirmation-boundary, schema, or runtime-authority change.

## 3. EXACT FILES CHANGED

`git show --numstat 8b73497` (authoritative):

| Path | Category | Added | Removed | Purpose of change |
|---|---|---|---|---|
| `backend/ai/tools/settings.py` | production | 33 | 4 | Add `_setting_key_contract()` + module key lists; embed the contract in `SettingsGetTool`/`SettingsSetTool` descriptions and `key` parameter descriptions |
| `backend/ai/prompt/builder.py` | production | 6 | 2 | Relabel reply-block metadata lines `AI Provider:`→`Provider:`, `AI Model:`→`Model:`; add a 4-line why-comment |
| `tests/test_settings_model_key_contract.py` | test (NEW file) | 316 | 0 | 7 regression tests pinning the repaired model-facing contract |
| `INVESTIGATION.md` | documentation | 27 | 0 | RC-7 finding, confirmation-invariant note, F-3 index line |
| `IMPLEMENTATION_REPORT.md` | documentation | 109 | 1 | §17 (this commit's report) and filling the `<SHA_AFTER_COMMIT>` placeholder in §16.8 |

No other files were touched. No configuration, schema, migration, or dependency files changed.

## 4. EXACT IMPLEMENTATION CHANGES

### 4.1 `backend/ai/tools/settings.py` — `_setting_key_contract()` and schema enrichment

**Old behavior (parent `1dda645`):**

- `SettingsGetTool.description` → `"Read a bot setting value by key."`
- `SettingsGetTool.parameters["key"]["description"]` → `"The setting key to read."`
- `SettingsSetTool.description` → `"Set a bot setting value by key. Requires owner confirmation."`
- `SettingsSetTool.parameters["key"]["description"]` → `"The setting key to write."`

No valid-key vocabulary existed anywhere in the model-facing surface. The key authorities (`_AI_CONFIG_KEYS` frozenset; `settings_service.known_keys()`, derived from `_DEFAULTS` at `backend/services/settings_service.py:66`) were purely internal enforcement sets, consulted only *after* the model chose a key.

**New behavior (`8b73497`):**

- Module-level `_AI_KEY_LIST` (sorted `", ".join` of `_AI_CONFIG_KEYS`: `history_budget, max_tokens, model, provider, system_prompt, temperature, trigger_en, trigger_fa`) and a lazily-cached `_PANEL_KEY_LIST`.
- `_setting_key_contract()` — lazily imports `settings_service` (avoids import cycles at module load), joins `sorted(settings_service.known_keys())`, and returns: `Valid keys — AI runtime: <list>; panel settings: <list>. The AI model setting is key 'model', never 'ai_model'; the AI provider setting is key 'provider', never 'ai_provider'.`
- All four description/parameter surfaces above now embed `_setting_key_contract()`. These are `@property` accessors, so the contract string is rendered on access.

**Architectural effect:** the contract reaches the model through both existing channels — the dispatcher's native tool-call schema construction and the text `[Available Tools]` block — because both consume the same `Tool.description` / `Tool.parameters`. There is no new key list, no alias table, no second configuration authority: the contract is *derived at runtime from* `_AI_CONFIG_KEYS` and `settings_service.known_keys()`, so it cannot drift from the enforcement sets.

**Explicitly unchanged in this file:** `_AI_CONFIG_KEYS` contents; `_set_ai_config()` routing (`provider`/`model`/`temperature`/`max_tokens`/`history_budget`/`system_prompt`/`trigger_en`/`trigger_fa` branches); the `provider` registration check against the runtime `ProviderManager`; `_apply_runtime_selection()` → `engine.apply_runtime_selection`; the RC-5 fail-closed branch (`if not settings_service.is_valid_key(key): return ToolResult(success=False, "Unknown setting key '...'")`); `SettingsSetTool.permission_level = ADMIN_ONLY`; `SettingsGetTool.permission_level = READ_ONLY`.

### 4.2 `backend/ai/prompt/builder.py` — reply-block labels

**Old behavior (parent):** inside the `[Reply to AI Message]` block, conditional lines rendered `AI Provider: {ctx.reply.ai_provider}` and `AI Model: {ctx.reply.ai_model}` (when those fields are non-empty).

**New behavior:** the same two lines render `Provider: {ctx.reply.ai_provider}` and `Model: {ctx.reply.ai_model}`. A 4-line comment records why (the old label taught the model a non-existent `ai_model` key).

**Architectural effect:** the model-facing prompt now contains only the canonical key names (`provider`, `model`), consistent with the `[Runtime Context]` block. This is a prompt-text change only — no data flow, field, or state change. The internal `ReplyContext.ai_model` / `ReplyContext.ai_provider` fields are untouched (internal context plumbing, never shown as a key name to the model after this commit).

### 4.3 `tests/test_settings_model_key_contract.py` — new regression file

7 tests (detailed in §7). They pin: the schema exposes canonical keys and never offers `ai_model` as valid; `ai_model` is rejected directly and through `ToolExecutor.execute_confirmed`; the ADMIN_ONLY confirmation gate still gates `settings_set`; a confirmed `model` change persists to `config_store`, applies to the runtime `ProviderManager`, and the NEXT `Engine.execute()` request is served by the new model (verified via a recording stub provider); the prompt renders `Model:`/`Provider:` and never `AI Model:`/`AI Provider:`.

### 4.4 Documentation changes in the commit

- `INVESTIGATION.md`: adds the RC-7 section (root cause, fix, test pointer), a one-line confirmation-invariant note in the settings-tools matrix, and the F-3 line in the findings index.
- `IMPLEMENTATION_REPORT.md`: adds §17 documenting this fix and fills §16.8's `<SHA_AFTER_COMMIT>` placeholder with the RC-5 delivery SHAs.

## 5. ROOT CAUSE → FIX MAPPING

Findings are from `INVESTIGATION.md` (read AFTER the commit diff was inspected, per audit protocol).

| Finding | Root cause | Change in 8b73497 | Status | Evidence |
|---|---|---|---|---|
| F-3 / RC-7 — model-facing settings key contract gap (`ai_model` vs `model`) | Prompt label `AI Model:` + unenumerated `key` parameter made the model invent a non-existent key; backend routing was correct and fail-closed | `_setting_key_contract()` embedded in both settings tools' schemas; prompt labels renamed to canonical `Model:`/`Provider:`; 7 regression tests | **FIXED (in source + in-process tests); LIVE VERIFIED: NO** | `git diff 8b73497^ 8b73497 -- backend/ai/tools/settings.py backend/ai/prompt/builder.py`; `tests/test_settings_model_key_contract.py` (7 passed during this audit) |
| F-1 / RC-5 — `settings_set` phantom success on unknown keys | `settings_service.set_setting()` had no allowlist; cached arbitrary keys and returned `True` | **Not this commit** — fixed earlier by `4a226a0` + `1dda645`; `8b73497` *preserves* the fail-closed branch untouched | NOT RELATED TO THIS COMMIT (already FIXED) | RC-5 branch present in `8b73497`'s `SettingsSetTool.execute` (source-inspected); `tests/test_settings_unknown_key.py` passes |
| F-2 / RC-6 — DANGEROUS permission docstring drift | `base.py`/`delete.py`/`organize.py` docstrings contradict executor auto-execute behavior | None — those docstrings are untouched by `8b73497` | NOT FIXED (documentation-only finding, out of this commit's scope) | no diff hunks in those files |
| RC-1 — `❌ Unsupported action: send` | fixed in `1285cdf` (pre-existing) | None | NOT RELATED TO THIS COMMIT | earlier commit |
| RC-2 — `settings_set` could not execute through AI | fixed in `c5d29f7` (pre-existing) | None | NOT RELATED TO THIS COMMIT | earlier commit |
| RC-3 — event request miscreated as interval task | fixed in `1285cdf` (pre-existing) | None | NOT RELATED TO THIS COMMIT | earlier commit |
| RC-4 — documentation drift | documented, unfixed | None | NOT RELATED TO THIS COMMIT | INVESTIGATION.md §7 |
| A-1 / A-2 / A-3 — P3 notes (`delete_messages_by_ids` description overclaim; empty-value `settings_get`; `web_search` error swallow) | minor, documented | None — untouched by `8b73497` | NOT FIXED / NOT RELATED TO THIS COMMIT | no diff hunks in those tools |

## 6. BEHAVIOR CHANGED

**Newly added behavior:**
- The model-facing tool schema (both native provider schemas and the text `[Available Tools]` block) now enumerates every valid settings key across both stores and explicitly names `model`/`provider` as canonical with `ai_model`/`ai_provider` as invalid.

**Corrected behavior:**
- The `[Reply to AI Message]` block labels are `Model:`/`Provider:` instead of `AI Model:`/`AI Provider:` — the prompt no longer teaches a non-existent key. Reply-metadata data flow is unchanged.

**Preserved behavior (verified in the commit's diff and current source):**
- All settings routing: AI-runtime keys → `config_store` (+ `_apply_runtime_selection` for `provider`/`model`); valid panel keys → `settings_service`.
- RC-5 fail-closed rejection of unknown panel keys, including the exact `Unknown setting key '...'` message the live failure produced — `ai_model` is still rejected (now listed in the schema text only as a forbidden alias).
- ADMIN_ONLY confirmation gate on `settings_set` (needs_confirmation → owner confirmation → `execute_confirmed`).
- `ToolRegistry` / `ToolExecutor` / `ProviderManager` / dispatcher plumbing.

**Intentionally unchanged:**
- No alias layer for `ai_model`; no schema/migration; no provider/model runtime logic; no Glass UI changes (the `panel:ai_model` internal UI namespace is unchanged — it is a Glass panel name, not a model-facing key); no other tool's schema.
## 7. TESTS

**Added by the commit** — `tests/test_settings_model_key_contract.py` (new file, 316 lines, 7 tests):

1. `test_settings_tool_schema_exposes_canonical_keys_and_never_ai_model` — contract text/derived lists contain canonical keys, never `ai_model` as valid; `'model'` appears before the forbidden alias.
2. `test_settings_set_and_get_descriptions_carry_the_contract` — both tools' `description` and `parameters["key"]["description"]` embed the contract.
3. `test_ai_model_key_rejected_as_unknown` — direct `execute()` with `key="ai_model"` fails with `Unknown setting key 'ai_model'`; nothing persisted to `config_store`, nothing cached in `settings_service`.
4. `test_ai_model_key_rejected_even_after_owner_confirmation` — `ToolExecutor.execute_confirmed` with `ai_model` still fails closed.
5. `test_model_change_still_requires_confirmation` — `settings_set {key: "model"}` through `execute_calls` returns `needs_confirmation=True`; nothing persisted before confirmation.
6. `test_confirmed_model_change_persists_applies_and_serves_next_request` — the full integration path: confirmed `model` change → persisted to `config_store` → applied to the runtime `ProviderManager` → the NEXT `Engine.execute()` request is served by the new model (recording stub provider).
7. `test_prompt_builder_renders_canonical_provider_model_labels` — prompt renders `Model: model-a` / `Provider: prov-a`; `AI Model:`/`AI Provider:` absent.

Test types: 1–2 unit/schema-level; 3–4 executor-integration fail-closed; 5 confirmation-gate; 6 full AI-path + runtime-state integration; 7 prompt-construction unit. Owner IDs use a dedicated 904xxx range to avoid cross-suite `config_store` pollution (a collision with `test_ai_state_consistency.py`'s 902xxx range was fixed before delivery).

**Executed during THIS audit** (exact commands, actual results — Python 3.10.12, pytest 9.1.1, asyncio 1.4.0 strict mode):

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_settings_model_key_contract.py -v -p no:cacheprovider` | **7 passed in 0.26s** |
| `.venv/bin/python -m pytest tests/test_settings_model_key_contract.py tests/test_settings_unknown_key.py tests/test_settings_runtime_switch.py tests/test_confirmation_roundtrip.py -q -p no:cacheprovider` | **93 passed in 0.62s** |
| `.venv/bin/python -m pytest tests/test_ai_menu_state_consistency.py tests/test_tool_health_audit.py tests/test_37_ai_memory_db.py tests/test_09_reply_to_ai.py -q -p no:cacheprovider` | **95 passed, 1 warning in 6.17s** |
| `.venv/bin/python -m pytest tests/ -q -p no:cacheprovider` (full suite) | **1710 passed, 23 skipped, 1 warning in 62.58s** |

The audit-time results match the results recorded in the commit's own §17.5 (1710 passed, 23 skipped).

**Not covered by tests:** live Telegram end-to-end behavior (trigger-word model change in a reply-to-AI context, owner confirmation in a real chat) and real-provider schema rendering (tests use a stub registry/provider; a live provider's schema translation is exercised only through the shared dispatcher plumbing).

## 8. DATABASE / SUPABASE IMPACT

- **No database/schema code changed.** `git show --name-status 8b73497` lists no file under `backend/db/`, `backend/ai/database/`, `backend/ai/persistence.py`, or `supabase/`. No migration was added or modified.
- Runtime data flow through existing stores is unchanged: `config_store` writes (`update_model`, `update_provider`, `update_setting`) and `settings_service` writes keep their pre-commit behavior and tables (`ai_config`, `panel_settings`).
- **Live Supabase verification: NOT performed** (neither at commit time nor during this audit). The in-memory fallback is what the test suite exercises.

## 9. TELEGRAM / LIVE VERIFICATION

**NOT performed.** No live Telegram connection exists in this workspace (no credentials). The original failure was reported from a live session; the fix is source-verified and in-process-test-verified only. The commit's own report (§17.7) states the same. Required manual live check (unchanged): in a chat replying to an AI message, ask the AI to change the model — the tool call must use `key="model"` (not `ai_model`), and after owner confirmation the next request must be served by the new model.

## 10. SECURITY / ARCHITECTURE

| Boundary | Changed by 8b73497? | Detail |
|---|---|---|
| Self Bot execution authority | Preserved | No new executor path; tools still run only via `ToolExecutor` |
| ToolRegistry | Preserved | No registration, permission, or schema-plumbing change; only description text |
| ToolExecutor / confirmation flow | Preserved | `settings_set` remains `ADMIN_ONLY` (`needs_confirmation` before persistence); pinned by `test_model_change_still_requires_confirmation` |
| ProviderManager / runtime selection | Preserved | `_apply_runtime_selection` → `engine.apply_runtime_selection` → `ProviderManager.apply_selection` unchanged; the provider-registration check is unchanged |
| Database authority boundaries | Preserved | Same two stores, same owners, no new store |
| RuntimeSupervisor / TelegramAPI | Preserved | No hunks in `backend/runtime/` or `backend/telegram_api/` |
| AI execution boundaries | Preserved | AI still cannot execute arbitrary Telegram RPC / SQL / shell; the commit only changes *what the model is told* |

**Security-relevant nuance (called out explicitly):** the commit *discloses information to the model* — the complete valid-key list is now visible in tool schemas. This is bounded to keys that already exist in the two authoritative stores, grants no new capability (every key was accepted-or-rejected exactly the same way at execution time before and after), and the disambiguation is the point of the fix. The forbidden-alias mention of `ai_model` in the contract text is informational only; the enforced rejection path is unchanged.

## 11. LIMITATIONS / UNVERIFIED

- **Live Telegram verification:** not performed (no credentials in this workspace). The fix's real-world effect on model behavior (no more `ai_model` generations) is therefore UNVERIFIED live.
- **Live Supabase verification:** not performed; DB-path behavior is verified only through the in-memory fallback and stub-backed store assertions.
- **Real-provider schema rendering:** the contract text is unit-tested at the `Tool` property level; its rendering through each provider's specific schema translation (e.g. Gemini `functionDeclarations`) is not provider-tested.
- **Documentation inconsistency found by this audit:** the delivered commit's own §17.8 recorded commit `5453752` as its delivery SHA. `5453752` exists in the object store with the same subject, same parent (`1dda645`), and a diff against `8b73497` of exactly one line (the §17.8 SHA cell itself) — i.e. a pre-`--amend` artifact, unreferenced by any branch. The actual delivered commit is `8b73497`. This report supersedes that record.
- The commit does not address F-2/RC-6 (stale DANGEROUS docstrings) or the P3 notes A-1/A-2/A-3; they remain open as documented in `INVESTIGATION.md`.

## 12. GIT DELIVERY

| Item | Value |
|---|---|
| Audited commit | `8b734976a2e1b9a5df463dad051c1dad4c3482b3` |
| Current HEAD | `8b734976a2e1b9a5df463dad051c1dad4c3482b3` (= `main`) |
| Present locally | Yes (`git cat-file -t` → commit; ancestor of HEAD per `git merge-base --is-ancestor`) |
| Present on origin/main (local ref) | Yes (`git merge-base --is-ancestor 8b73497 origin/main` → yes) |
| Remote verification | `git ls-remote origin refs/heads/main` → `8b734976a2e1b9a5df463dad051c1dad4c3482b3  refs/heads/main` (executed during this audit; matches HEAD exactly) |
| Working tree at audit start | Clean except pre-existing untracked `telegram-self-bot/` nested clone (untouched, excluded from all operations) |
| Pre-amend artifact | `5453752b4e816f8093f266b7b6e294251d4be64e` — same change pre-amend; unreferenced by any branch; superseded by `8b73497` (see §11) |

This report documents only commit `8b73497` and the verified repository state around it. It replaces all prior report content.
