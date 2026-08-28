# Implementation Report — Markdown Entity Delivery: Implementation Blocked (No Safe Deterministic Rule Evidenced)

## Date
2026-08-28

## Repository / Branch / Base commit
- Repository: `https://github.com/Onlyicing1/Telegram-self-bot` (`origin`)
- Branch: `main`
- Base commit (start of chunk): `89c7135023ee986fb5bf36dd8d0305f91cea1769` (synced with `origin/main`)

## Task objective
Determine whether the centralized AI→Telegram delivery boundary
(`backend/ai/tools/delivery.py`) can safely render supported Markdown
(bold, italic, inline code, fenced code, links, and optionally underline /
strikethrough) as real Telegram entities, instead of the current safe
plain-text degradation — and, if the evidence gate passes, implement only the
smallest deterministic, meaning-preserving subset justified by the source.

## Evidence gate
This chunk ran Phase 1 (targeted investigation only) and assessed each
question against the actual source and direct execution of that source.

### Confirmed
- `process_output()` returns `.text = rendered` (the string actually
  delivered), but computes `.entities` via `_render_entities(text)` against
  the **raw input**, not the rendered string. These are different strings.
- Direct execution of the current source:
  - `process_output("hi **bold**")` → delivered `.text = 'hi bold'`, but the
    produced `MessageEntityBold(offset=5, length=4)` is **out of bounds**
    against the delivered text (`offset+length = 9 > utf16_len 7`) and slices
    the wrong substring `'ld'`.
  - `process_output("🙂 *سلام*")` → delivered `.text = '🙂 سلام'`, but
    `MessageEntityItalic(offset=4, length=4)` slices `'لام'` and is out of
    bounds (`8 > 7`).
  - `process_output("🙂 **bold**")` → delivered `.text = '🙂 bold'`, entity
    slices `'ld'`, out of bounds.
- No production code consumes `.entities`/`entity_count`. `deliver_response()`
  reads only `processed.text` and passes plain text to `event.edit` /
  `event.reply`. The entity path is dead output today (which is the only
  reason the offset bug has not caused live corruption).
- `deliver_response()` calls `event.edit(...)` and `event.reply(...)` with text
  only — no `entities=` argument.

### Likely
- `_format_chunks()` interleaves a `header` prefix on the first message and
  `\n\n_(i/n)_` continuation markers on subsequent messages. Mapping any
  body-relative entity to chunk-relative offsets therefore requires adding the
  header's UTF-16 length and stripping/re-clamping per continuation — with no
  existing precedent in the source and real risk to the just-hardened UTF-16
  splitting.

### Unknown
- Whether users actually require Telegram native formatting (as opposed to the
  current faithful plain-text rendering). No issue/report in the repository
  evidences a user-facing formatting defect.

## Exact defect / limitation addressed
**None implemented.** Investigation found that naive Markdown-entity delivery
is provably unsafe in the current code: the existing `_render_entities`
produces entity offsets against the *raw* input while the delivered message is
the *rendered* (normalized + Markdown-degraded) text, so those entities are
out of bounds / point at the wrong substring when applied to the real message.

## Root cause
Two independent causes make entity delivery unsafe today:
1. Offset basis mismatch — `_render_entities(text)` runs before/against the raw
   input, whereas `.text` is the transformed `rendered` string. Any entity
   derived from the raw string is wrong for the delivered string.
2. No span tracking — `_render_markdown` strips emphasis with `re.sub(...)`
   and no source-span records. Offsets cannot be recovered from the delivered
   string without re-parsing, which requires deciding which `*`/`_`/`__` were
   actually stripped vs. left literal (intraword punctuation). That decision is
   Markdown-intent inference, which the repair/entity rules forbid.

## Exact files changed
**None (production).** `backend/ai/tools/delivery.py` and
`tests/test_67_ai_output_pipeline.py` are unchanged.

Documentation changed:
- `IMPLEMENTATION_REPORT.md` — completely replaced with this single
  investigation/blocked report.

## Exact implementation changes
None. This is an investigation + blocked-delivery report chunk.

## Behavior changed
- FIXED: none.
- MITIGATED: none.
- PROTECTED: all existing delivery behavior is preserved unchanged — central
  normalization, intraword emphasis repair (`2*3*4`, `some_word_here`,
  `some__word__here`), protected URLs/usernames/commands/inline+fenced code,
  UTF-16-aware splitting, formatter-failure fallback, empty-output rejection.
- PRESERVED: `Provider → ProviderManager → Dispatcher → Engine → EngineResult
  → ai_unified._execute_ai → deliver_response → process_output → _format_chunks
  → Telegram edit/reply`.
- NOT PROVEN: safe deterministic Markdown-entity rendering (evidence gate
  failed — see Evidence gate and Root cause).

## Intentionally not changed
`ai_unified.py`, `dispatcher.py`, `engine.py`, `result.py`, `ProviderManager`,
Telegram execution boundary, `ghost_seen_v2`, Supabase, migrations,
`DATABASE_ARCHITECTURE.md`, `INVESTIGATION.md`, and unrelated tests.

## Tests added / updated
None. Per the fail-closed rule, no speculative regression tests were added for
behavior that is not implemented.

## Tests actually executed
No production or test files were modified, so no focused/full test suite was
re-run for a code change. Source behavior was verified by direct interpreter
execution of the current `process_output`/entity path (commands run this
chunk), which demonstrated the out-of-bounds entity defect. The full suite
state from the last committed chunk remains: `1044 passed, 23 skipped,
1 warning`.

## Validation
- Direct execution confirmed the offset-mismatch/out-of-bounds entity defect
  (evidence above).
- `git status` / `git rev-parse HEAD` confirmed a clean tree at base commit
  `89c7135…`, synced with `origin/main`.
- No code was changed, so `compileall`/`git diff --check` for code edits are
  not applicable beyond confirming the tree was clean at start.

## Security / architecture boundaries
Unchanged and preserved: centralized single delivery boundary; Self Bot as
Telegram execution authority; `deliver_response()` as the sole caller-facing
delivery entry; no provider/AI/network/database/Telegram-RPC calls added; no
message-content, credential, API-key, or session logging introduced. No second
delivery path was created.

## Database / schema impact
- `DATABASE_ARCHITECTURE.md`: unchanged.
- No schema changes, no migrations, no Supabase writes, no SQL executed.

## Current limitations
- Telegram formatting is delivered as faithful plain text, not native entities.
- The existing `_render_entities`/`RenderedOutput.entities` machinery is
  latent, incorrect against the delivered string, and unused. It should not be
  wired into delivery without a correct span-tracking rewrite.

## Remaining work / blockers
- To ever deliver entities safely, a future chunk would need: (a) correct
  entity construction against the **rendered** string with explicit source-span
  tracking through normalization + Markdown stripping (no re-guessing whether a
  marker was stripped or literal), (b) UTF-16 chunk-relative offset remapping in
  `_format_chunks` (header prefix + continuation markers), and (c)
  `deliver_response()` passing `entities=`. That is a substantial, delicate
  change beyond this chunk's scope and is not justified by current evidence.
- Removed dead/incorrect entity code from `RenderedOutput` (smaller `entity_*`
  fields) is an optional cleanup; currently left in place to avoid unrelated
  churn.

## Commit / delivery
- Primary commit (report body): `de0cd3e991d5a1f41d2a87f1ddd3cf60861234fe` — `docs: record markdown entity delivery blocked investigation`
- Final delivered commit (on `origin/main`): `f7f59043f0788c97b4417dbf593860bf9f41020a` — report truthfulness corrections
- Push result: pushed to `origin/main` (fast-forwards up to `f7f5904`)
- Remote verification: local HEAD == `origin/main` == remote `main` == `f7f59043f0788c97b4417dbf593860bf9f41020a`; verified via `git fetch origin main`
- Final working-tree state: clean