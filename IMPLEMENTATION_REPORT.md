# Implementation Report — UTF-16-Aware Telegram Text Splitting

**Date:** 2026-08-28
**Repository:** https://github.com/Onlyicing1/Telegram-self-bot
**Branch:** `main`
**Base commit:** `a25c140cee215466d0b4de3a88f844b4d3963931` (`docs: replace implementation report with current-state record`)

## Task objective

Make the centralized AI output delivery splitter enforce Telegram's text-size
boundary using UTF-16 code units instead of Python Unicode code-point length,
so supplementary-plane characters (e.g. many emoji) never push a delivered
message over the configured safe Telegram limit.

## Current implementation state

The successful general AI delivery path is unchanged in shape:

```text
Provider → ProviderManager.chat() → Dispatcher.dispatch() → Engine.execute()
→ EngineResult → ai_unified._execute_ai() → deliver_response()
→ process_output() → _format_chunks() → Telegram edit/reply
```

`backend/ai/tools/delivery.py` remains the single centralized delivery
boundary. No provider, Dispatcher, Engine, EngineResult, or Telegram
execution change was made. The previous word-boundary emphasis repair is
preserved intact.

## Exact defect addressed

`_split_text` and `_format_chunks` used Python character count (`len()`) as
the Telegram length metric. Supplementary-plane characters such as `🙂` are
1 Python character but 2 UTF-16 code units, so Telegram's effective text
limit was being exceeded. Reproduced against the pre-fix source: a string of
3999 emoji reported by Python as length 3999 (under `SAFE_LIMIT` 4000) has a
UTF-16 length of 7998 and was emitted by the old splitter as **one** 7998-unit
chunk.

## Root cause

The splitter compared `len(text)` (Python code points) against `SAFE_LIMIT`
and chose Python-character split boundaries. Telegram's text/entity offset
accounting and relevant message-size limits operate in UTF-16 code units, so
the Python metric under-counted supplementary-plane content and permitted
over-limit chunks.

## Exact files changed

- `backend/ai/tools/delivery.py` — UTF-16-aware measurement and splitting
  (the only production file changed).
- `tests/test_67_ai_output_pipeline.py` — focused UTF-16 splitting tests
  added and one pre-existing test's fragile contiguity assertion corrected.
- `IMPLEMENTATION_REPORT.md` — replaced with this single current-state report.

## Exact implementation changes

- `_split_text(text, limit)` now splits while `_utf16_units(rest) > limit`,
  and trims the remainder by character (`rest = rest[point:]`) so the full
  content is preserved across chunks (leading-newline stripping was removed:
  concatenation now exactly reconstructs the intended output).
- `_upto_utf16(text, units)` returns the longest valid UTF-16 prefix without
  splitting a surrogate pair, over-allocating then trimming for a single-pass
  scan.
- `_align_to_character(text, units)` rounds a boundary to a whole Unicode
  character, walking past a lone-surrogate index so a surrogate pair is never
  split; it always returns at least one character so the loop always makes
  progress.
- `_split_point_at_utf16` keeps the existing boundary preference
  (paragraph → newline → word) within the UTF-16 budget, then falls back to a
  character boundary.
- `_format_chunks` now compares `_format_message(...)` against `SAFE_LIMIT`
  using UTF-16 and sizes the body budget as
  `SAFE_LIMIT - _utf16_units(header)`.
- `_utf16_units` (already present) is the single measurement primitive reused
  throughout; no approximate byte or multiplier logic was introduced.

## Why the implementation is correct

- Every emitted chunk and delivered message is capped by **UTF-16 code units**,
  matching Telegram's metric.
- A single character occupies at most 2 UTF-16 units (a surrogate pair or a
  BMP character), so the configured limit always accommodates complete
  characters; surrogate pairs are never split.
- The concatenation of `_split_text` chunks, and of the response body across
  delivered messages after stripping the continuation labels, reconstructs the
  complete intended output — nothing is silently truncated.
- The existing paragraph → newline → word → character fallback hierarchy is
  preserved where boundaries fit within the budget.
- The safe Telegram limit (`SAFE_LIMIT = 4000`) is unchanged; only the
  measurement is corrected.

## Tests added / updated

Focused coverage added to `tests/test_67_ai_output_pipeline.py`:

- ASCII near / exactly at / just over the limit.
- BMP Unicode chunks.
- `python len() <= limit` while UTF-16 length exceeds the limit **must** split.
- Many emoji, mixed ASCII + emoji, Persian + emoji, Arabic + emoji,
  CJK + emoji, mixed RTL/LTR + emoji.
- Long mixed-script Unicode output.
- Every emitted chunk ≤ the UTF-16 limit.
- Complete content preserved after splitting.
- No surrogate pair split.
- Paragraph / newline / word preference preserved.
- `_split_text` idempotent on realistic bodies.
- `_format_chunks` body reconstructs the response across messages; every
  delivered message ≤ the UTF-16 limit.
- `deliver_response()` integration delivers the correctly split output.
- The pre-existing `test_utf16_safe_chunking_and_no_truncation` assertion was
  corrected from a fragile contiguous-substring check to an accurate
  reconstruct-across-messages check (the old check only held by coincidence of
  chunk count).

No test was added for semantic correction, translation, or intent inference.

## Tests actually executed

- `python3 -m pytest tests/test_67_ai_output_pipeline.py -q --no-header`
  — **54 passed** in 0.24s.
- `python3 -m pytest tests/ -q --no-header` — **1044 passed, 23 skipped,
  1 warning** in 56.79s. The skips are the pre-existing legacy
  `ghost_seen_service` tests; the warning is a pre-existing
  `python_multipart` deprecation in starlette.
- `python3 -m compileall -q backend tests` — passed.
- `git diff --check` — passed.

## Validation results

- A randomized stress check over mixed scripts (500 trials, limits 4–60)
  confirmed full content preservation and every chunk within the UTF-16 limit.
- Previous-repair regression verified: `2*3*4`, `some_word_here`,
  `some__word__here` remain literal; `*italic*` / `**bold**` still degrade;
  protected URLs/usernames/commands/code unchanged.
- Final diff inspected: only `backend/ai/tools/delivery.py`,
  `tests/test_67_ai_output_pipeline.py`, and `IMPLEMENTATION_REPORT.md`
  changed.

## Security / architecture boundaries

- No change to ProviderManager, provider fallback, Dispatcher, Engine,
  EngineResult, tool allowlists, owner authorization, or Telegram execution
  authority.
- No new delivery path; `deliver_response()` remains the public boundary.
- The splitter is synchronous, local, deterministic, and idempotent; it makes
  no network, database, provider, or AI call.
- Failure containment unchanged; no message content, credentials, or session
  data are logged.

## Database / schema impact

None. `DATABASE_ARCHITECTURE.md`, migrations, SQL, and Supabase were not
modified. No live Telegram or live Supabase access was performed.

## Current limitations

- Markdown is still rendered as safe plain text rather than Telegram entities.
- When no paragraph/newline/word boundary fits within the UTF-16 budget, the
  splitter falls back to a character boundary inside the budget (standard for
  long unbroken runs), while never splitting a surrogate pair.
- A single character cannot exceed 2 UTF-16 units, so the conservative
  `SAFE_LIMIT = 4000` always fits at least one full character per chunk.

## Remaining work / blockers

- No remaining blockers for this chunk. Only this UTF-16 splitting concern was
  implemented; additional repair rules (e.g. Markdown entity rendering) remain
  separate future chunks.

## Commit / delivery

- Commit: `6fa491dc861830072f87bcd02f89c1782e059fc1` —
  `fix: enforce telegram utf-16 text limit in ai output splitting`
  (3 files changed, +432/−153).
- Push result: pushed to `origin/main` (`a25c140..6fa491d`), exit 0.
- Remote verification: after `git fetch origin main`, local HEAD ==
  `origin/main` == `git ls-remote origin refs/heads/main` ==
  `6fa491dc861830072f87bcd02f89c1782e059fc1`. Remote
  `IMPLEMENTATION_REPORT.md` contains exactly one report section.
- Final working-tree state: `main` in sync with `origin/main`; only the
  intended implementation, tests, and report were committed.