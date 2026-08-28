# INVESTIGATION

## INVESTIGATION METADATA

- Repository: Telegram-self-bot / LifeOS
- Branch: `main`
- Current HEAD: `89b303f661587101a31d4d3025cdc50f89fd2158` (`fix: harden ai telegram output pipeline`)
- Investigation date: 2026-08-28
- Scope: AI message repair after provider generation and before Telegram delivery
- Status: Investigation only. No production code, tests, schema, SQL, providers, or Telegram execution were modified.

## 1. PROBLEM

The investigated problem is whether malformed or awkward AI-generated messages still require a separate deterministic Repair stage after the existing centralized output pipeline.

The repository contains no source-backed incident, stored diagnostic, or failing regression that demonstrates a specific remaining malformed-output defect. Existing behavior already normalizes and safely degrades common formatting input. Hypothetical defects such as deeper Markdown nesting failures or provider-specific semantic oddities are not treated as confirmed problems.

## 2. ROOT CAUSE

### CONFIRMED

- The current production delivery boundary is `backend/ai/tools/delivery.py`, invoked by `backend/bot/handlers/ai_unified.py` after a successful `EngineResult`.
- The current processor intentionally renders Markdown to plain Telegram-safe text; it does not send Telegram formatting entities.
- The processor recognizes scripts and direction metadata but does not perform linguistic translation or semantic rewriting.
- Oversized responses are split deterministically using Python string length. No source evidence shows a currently failing Telegram-length case.

### LIKELY

- Some complex or ambiguous Markdown may lose formatting rather than preserve it, because rendering is conservative degradation rather than a full Markdown AST/entity renderer.
- Telegram's hard limit is measured in UTF-16 units while `_split_text` uses Python character count, so astral emoji-heavy messages could be split less conservatively than required.
- `_render_entities` computes entity metadata from the original Markdown but the delivery path sends only rendered plain text, so entity metadata is observational and not used for Telegram delivery.

These are implementation limitations indicated by source inspection, not demonstrated production failures.

### UNKNOWN

- Whether any real provider output currently causes user-visible semantic corruption after normalization.
- Whether live Telegram rejects any currently emitted oversized chunk.
- Whether users require preserved bold/italic/link entities instead of the current safe plain-text rendering.
- Whether the existing diagnostics contain an unreported repair candidate; message content is intentionally not logged and no live diagnostics were inspected.

## 3. CURRENT AI OUTPUT PIPELINE

The current path is:

```text
Provider implementation
  → ProviderManager.chat()
  → Dispatcher.dispatch()
  → Engine.execute()
  → EngineResult
  → ai_unified._execute_ai()
  → result.response extraction
  → backend.ai.tools.delivery.deliver_response()
  → process_output()
  → _normalize_plain() / _render_markdown() / profile + validation
  → _format_chunks()
  → event.edit() or event.reply()
  → Telegram
```

`Engine.execute()` delegates to `Dispatcher.dispatch()`. The dispatcher receives provider responses, processes tool rounds and failures, then returns the immutable `EngineResult`. In `ai_unified._execute_ai`, only a successful result with a response reaches `deliver_response`; failed and empty results use separate failure rendering. The delivery module is therefore the single existing boundary for successful general AI text before Telegram output.

Ghost Seen AI Reply uses its own execution boundary and does not call this general delivery function; it has separate Telegram reply semantics. No claim is made that a future Repair stage automatically covers Ghost Seen unless that path is deliberately integrated in a later implementation.

## 4. CURRENT NORMALIZATION BEHAVIOR

`process_output()` rejects non-string/empty output, then applies:

- NFC Unicode normalization and newline normalization.
- Conservative Arabic Yeh/Kaf conversion only when Persian marker characters are present.
- Protected-region substitution for fenced/inline code, URLs, usernames, and Telegram commands.
- Collapsing repeated spaces/tabs, trimming line whitespace, limiting blank lines, trimming outer whitespace.
- Removing spaces before common Latin and Arabic-script punctuation.
- Adding a space after punctuation only when the following character is Latin/Cyrillic/Arabic-script text.
- Markdown degradation: links become `label (URL)`, supported emphasis markers are removed, headings are stripped, list markers become bullets, and quote markers become a visual bar. Unsupported or malformed constructs remain as safe text where they cannot be structurally recognized.
- Script profiling based on Unicode character names, with RTL recognition for Arabic/Hebrew and metadata for Cyrillic, Greek, Japanese, Korean, CJK, and Latin scripts.
- Output metadata including scripts, direction, mixed-direction state, Markdown detection, changed state, and entity metadata.
- Deterministic splitting with paragraph/newline/space boundaries and continuation labels; it does not silently truncate.

`deliver_response()` logs only structured facts (script names, direction, mixed state, Markdown flag, changed flag, and output length). If processing raises, it logs only the exception type and falls back to the original response before delivery. It performs no provider, database, network, or Telegram operation itself; Telegram calls occur only after processing in the delivery function.

## 5. REPAIR GAP

Only the following possible gaps remain after current normalization:

1. **Complex Markdown preservation** — Evidence: `_render_markdown()` uses bounded deterministic transformations and emits plain text, not a Markdown AST or Telegram entities. Classification: LIKELY limitation, not a confirmed defect. Deterministic repair is safe only for unambiguous balanced constructs; arbitrary malformed nesting or intended emphasis is NOT safe to infer. Safety: safe degradation, but not safe to reconstruct uncertain formatting.
2. **UTF-16-aware length splitting** — Evidence: `_split_text()` compares Python character length; `_utf16_units()` exists for entity metadata but is not used for chunk limits. Classification: LIKELY technical gap. Deterministic repair/splitting is safe if implemented against UTF-16 units with protected boundaries. Safety: safe, provided no truncation and semantic order are preserved.
3. **Bidi readability edge cases** — Evidence: the current implementation records direction and protects opaque regions but does not inject isolates. Classification: UNKNOWN as a production defect. Directional controls must not be added automatically without a demonstrated case because they can alter copied text and semantics.
4. **Semantic repair** — No confirmed gap. Filling omitted words, correcting factual content, inferring intended Markdown boundaries, or rewriting unnatural language is NOT SAFE TO REPAIR AUTOMATICALLY and must remain outside a deterministic stage.

No confirmed malformed-output root cause was found.

## 6. RELEVANT FILES

- `backend/ai/providers/manager/manager.py`
- `backend/ai/providers/base.py`
- `backend/ai/engine/dispatcher.py`
- `backend/ai/engine/engine.py`
- `backend/ai/engine/result.py`
- `backend/bot/handlers/ai_unified.py`
- `backend/ai/tools/delivery.py`
- `backend/ai/diagnostics.py`
- `tests/test_67_ai_output_pipeline.py`
- `IMPLEMENTATION_REPORT.md`

## 7. RELEVANT FUNCTIONS / CLASSES

- `Engine.execute()` / `Engine`
- `Dispatcher.dispatch()` / `Dispatcher`
- `ProviderManager.chat()` / `ProviderManager`
- `EngineResult`
- `ai_unified._execute_ai()`
- `deliver_response()`
- `process_output()`
- `_profile()`
- `_normalize_plain()`
- `_render_markdown()`
- `_protect()` / `_restore()`
- `_format_chunks()` / `_split_text()`
- `_utf16_units()` / `_utf16_offset()` / `_entity_valid()`
- `ai_unified._format_error()` and failure handling

## 8. CURRENT BEHAVIOR

Successful general AI text is validated for non-empty content at the delivery boundary, normalized, conservatively rendered, split if oversized, and sent by editing the triggering Telegram message with `event.edit()` or falling back to `event.reply()`. Formatting failures do not become fake AI failures: the original validated response is delivered. Provider failures, empty results, and Telegram delivery failures remain separate paths.

The current output renderer does not execute AI text, tools, or Telegram operations. It does not call external services. Protected tokens are restored after normalization. The general path is centralized, while Ghost Seen AI Reply retains its separate specialized execution/delivery flow.

## 9. DESIRED BEHAVIOR

A future Repair stage should address only demonstrated deterministic defects that remain after `process_output()`. It should preserve semantic text, protected regions, provider and tool boundaries, and current delivery behavior. It should never invent content, translate, infer intent, execute operations, or become a second AI pipeline.

For safely repairable syntax defects, the stage should produce a validated output or explicitly degrade to the existing normalized output. For uncertain formatting or semantic issues, it should leave content unchanged. Any repair failure should fall back to the already validated normalized/original response and remain diagnostically distinguishable from provider and Telegram failures.

## 10. ARCHITECTURAL OPTIONS CONSIDERED

- **Provider layer:** rejected. It would duplicate behavior across providers and make Telegram concerns provider-specific.
- **ProviderManager:** rejected. It owns routing/fallback, not output presentation.
- **Dispatcher/Engine:** rejected. They own reasoning, tools, conversation, and `EngineResult`; adding Telegram formatting there would blur responsibilities and affect non-Telegram consumers.
- **Immediately after `EngineResult` in `ai_unified`:** possible, but would duplicate or bypass the existing centralized delivery processor and could diverge between delivery paths.
- **Inside Telegram-specific delivery code:** appropriate responsibility, but a new parallel boundary is unnecessary.

### Recommended location

Add a small deterministic Repair stage inside `backend/ai/tools/delivery.py`, immediately before or as a named substage of `process_output()`/`deliver_response()`, after response type/non-empty validation and before final chunk formatting. This is the smallest existing successful-AI-to-Telegram boundary, preserves provider/engine contracts, and keeps all delivery formatting centralized.

The stage must not be added to `ProviderManager`, `Dispatcher`, or individual providers.

## 11. RECOMMENDED FIX SURFACE

Only change `backend/ai/tools/delivery.py` for production behavior unless tests prove an integration adjustment is necessary. Add a small internal repair result/profile if needed; do not create a provider, database state, Telegram RPC, or second delivery path. Keep `deliver_response()` as the only caller-facing delivery entry point.

Add focused tests in `tests/test_67_ai_output_pipeline.py` or a narrowly scoped adjacent test file only for source-backed deterministic cases. Do not modify `DATABASE_ARCHITECTURE.md`, migrations, or SQL.

## 12. IMPLEMENTATION PLAN

1. Capture the existing normalized output and protected-region representation.
2. Define a deterministic repair function with a narrow contract: syntax-only, meaning-preserving, idempotent, and no network/database/AI calls.
3. Repair only balanced/uniquely recoverable delimiter or whitespace defects demonstrated by tests; preserve or safely drop ambiguous formatting.
4. Run repair before final Telegram chunk construction, then revalidate non-empty text and Telegram size.
5. Keep exception containment: record safe structured failure type and use the pre-repair normalized output.
6. Verify general AI integration through `ai_unified._execute_ai()` and preserve its error/tool/delete branches.
7. Add regression tests for positive repairs, no-op ambiguous cases, protected tokens, multilingual RTL/LTR/CJK text, idempotence, fallback, and delivery integration.

## 13. TEST PLAN

Add only deterministic tests justified by the selected repair rules:

- already-normalized output is unchanged;
- supported balanced Markdown remains safely rendered;
- unmatched emphasis/backticks degrade without content loss;
- malformed links and unsupported syntax do not corrupt URLs;
- URLs, usernames, commands, inline code, and fenced code remain byte-preserved;
- mixed RTL/LTR, numbers, emoji, Cyrillic, and CJK remain semantically unchanged;
- repeated application is idempotent;
- repair exceptions fall back to the prior normalized output;
- oversized emoji-containing output respects Telegram UTF-16 limits without truncation;
- actual `deliver_response()` integration edits/replies the processed output;
- no formatter test performs database, network, provider, tool, or Telegram calls.

Do not add tests claiming semantic language correction, translation, or inferred intent repair is safe.

## 14. DATABASE / SCHEMA IMPACT

No database/schema change required.

No SQL was executed, no migration was created, and no Supabase operation was performed.

## 15. HARD CONSTRAINTS

- Investigation-only in this chunk: no production or test implementation was performed.
- Preserve `ProviderManager`, provider fallback, `Dispatcher`, `EngineResult`, tool allowlists, owner authorization, and Telegram execution authority.
- Repair must be local, deterministic, lightweight, async-compatible, and cancellation-safe.
- No extra AI/provider/network/database/API calls.
- No arbitrary Telegram RPC, SQL, shell, or tool execution.
- Never log message content, credentials, API keys, session strings, or sensitive Telegram data.
- Never silently truncate or invent content.
- Preserve protected opaque regions and existing delivery semantics.
- Keep `DATABASE_ARCHITECTURE.md` unchanged for this output-only feature.

## 16. REMAINING WORK

The implementation agent must decide whether a concrete deterministic repair defect is sufficiently evidenced. If yes, implement only that narrow stage at the recommended delivery boundary, add the justified regression tests, update `IMPLEMENTATION_REPORT.md`, and run the required validation. If no confirmed defect can be demonstrated, retain the existing normalization pipeline and document that no Repair code is warranted rather than introducing speculative rewriting.

## 17. VALIDATION

The next implementation chunk should run:

```text
python3 -m pytest tests/test_67_ai_output_pipeline.py -q --no-header
python3 -m pytest tests/ -q --no-header
python3 -m compileall -q backend tests
git diff --check
```

It should also inspect the final diff and verify that only the intended implementation, focused tests, and implementation report changed. If implementation is delivered, verify the commit, push, remote SHA, and clean working tree separately. This investigation itself performed no test mutation, SQL execution, commit, or push.
