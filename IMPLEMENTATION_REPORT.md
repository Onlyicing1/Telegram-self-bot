# Implementation Report — Telegram-Friendly Markdown Table Rendering

## Date
2026-08-29

## Repository / Branch / Base commit
- Repository: `https://github.com/Onlyicing1/Telegram-self-bot` (`origin`)
- Branch: `main`
- Base commit (start of chunk): `b305c97a7ba723d38b69773719e26f4085570b8d` (synced with `origin/main`, clean tree)

## Task objective
Make Markdown tables produced by the AI render as properly aligned, readable
tables inside Telegram messages, using only the existing centralized plain-text
delivery boundary (`backend/ai/tools/delivery.py`). This is **not** general
Markdown entity delivery (still blocked) — it is a smallest-possible,
deterministic table renderer that composes with the existing pipeline.

## Current implementation state
`deliver_response()` → `process_output()` → `_format_chunks()` → Telegram
edit/reply remains the single delivery boundary. `process_output()` now runs
`_render_tables(_render_markdown(_normalize_plain(text)))`. The new
`_render_tables()` stage detects real Markdown tables and renders them as
aligned fixed-width monospace blocks wrapped in balanced triple-backtick
fences. Telethon's default `parse_mode='md'` (verified empirically, no
`parse_mode` is set anywhere in the backend) parses the balanced fence into a
`MessageEntityPre`, so Telegram renders the block in a monospace font where the
padded columns line up. No entity-offset code, no chunk-relative remapping, no
second delivery path.

## Exact defect / limitation addressed
AI output containing Markdown tables was delivered as raw pipe-delimited text,
which is visually misaligned inside Telegram messages (columns do not line up,
especially with Persian/RTL text, English, numbers, and mixed-length cells).
Inserting spaces into normal Telegram text is insufficient because normal text
is not a reliable fixed-width surface.

## Root cause
The delivery pipeline normalized and degraded Markdown but had no table
recognition: pipe-delimited rows were passed through unchanged, and Telegram's
default font is proportional, so naive space padding cannot align columns. The
fix gives the pipeline a deterministic table detector and a width-aware
renderer, and uses Telegram's native monospace (pre) rendering for alignment.

## Exact implementation changes (`backend/ai/tools/delivery.py`)
Added (all local to the delivery module, synchronous, deterministic):

1. `_display_width(char)` — monospace display width: combining marks, ZWJ/ZWNJ,
   zero-width space, and variation selectors (VS15/VS16) = 0; East Asian
   wide/fullwidth and supplementary-plane characters (emoji) = 2; everything
   else (Latin, Persian, Arabic, digits) = 1. Width is computed per code point,
   never a multiplier over `len()`.
2. `_cell_display_width(cell)` / `_pad_cell(cell, width)` — display-width-aware
   padding (plain `str.ljust` is wrong for wide characters).
3. `_split_table_row(line)` — pipe split, leading/trailing outer empty cells
   dropped, cells stripped. Returns `None` for non-pipe lines.
4. `_is_table_separator(line)` — every cell must match `:?-+:?` (at least one
   dash), with at least one cell.
5. `_build_table_block(rows)` — column widths = max display width per column;
   header + `-` separator (`-` × (width+2) per column joined with ` | `) +
   body rows; cells joined with `" | "`. Returns `None` (fail closed) if any
   cell contains a triple backtick, so fences can never nest.
6. `_render_tables(text)` — line scan: a header row immediately followed by a
   separator row with the **same column count** starts a table; contiguous body
   rows with the same column count are consumed; a ragged row (different column
   count) fails the whole candidate closed and leaves the original lines
   untouched. The stage calls `_protect()`/`_restore()` itself, so tables
   inside inline code, fenced code, URLs, usernames, or commands are masked and
   never parsed. Output is wrapped in balanced ` ``` ` fences.
7. Wired into `process_output()`: `rendered = _render_tables(_render_markdown(_normalize_plain(text)))`.

Detection grammar (per task): header row + valid separator row + consistent
column count across body rows. `A | B` alone, `condition: x | y`, and a bare
`| A | B |` are **not** tables (no separator row) and pass through unchanged.

## Behavior changed
- FIXED: real Markdown tables now render as aligned monospace blocks
  (e.g. the task's Persian model table and the English Model/Speed/Capacity
  table; verified byte-exact alignment).
- PROTECTED: table syntax inside inline code, fenced code, URLs, `@user`,
  `/cmd` is never transformed.
- PROTECTED: ragged/ambiguous pipe structures fail closed and are left
  byte-for-byte as-is.
- PRESERVED: all previous delivery behavior — intraword emphasis (`2*3*4`,
  `some_word_here`, `some__word__here`), dot/colon literals (`main.py`,
  `example.com`, `e.g.`), intended punctuation spacing (`hello,world` →
  `hello, world`, `سلام،world` → `سلام، world`), protected tokens, UTF-16
  splitting, chunk formatting, reconstruction, formatter-failure fallback,
  empty-output rejection.
- PRESERVED: `_render_entities()` dead code and the blocked entity delivery
  project were not touched.

## Files changed
- `backend/ai/tools/delivery.py` — +106 lines (table renderer + one-line wiring)
- `tests/test_67_ai_output_pipeline.py` — +94 lines (8 new regression tests)
- `IMPLEMENTATION_REPORT.md` — completely replaced with this current report

No other files changed. `backend/bot/handlers/ai_unified.py`, providers,
Dispatcher, Engine, `_format_chunks`, `_split_text`, Supabase, migrations, and
`DATABASE_ARCHITECTURE.md` are untouched.

## Tests added
Eight focused tests in `tests/test_67_ai_output_pipeline.py`:

1. `test_table_renders_aligned_fenced_block` — English table byte-exact
   expected rendered block (header/separator/body padded identically).
2. `test_table_persian_renders_aligned` — Persian task example; consistent
   column structure and dash separator inside fences.
3. `test_table_detection_guards` — `A | B`, `condition: x | y`, bare
   `| A | B |`, ragged body row, and header/separator column mismatch all
   unchanged.
4. `test_table_protected_regions_untouched` — inline code, fenced code, URL
   with `|`, `@user|…`, `/cmd|…` untouched.
5. `test_table_display_width_unicode` — widths: ASCII/Persian = 1, CJK/emoji =
   2, combining mark = 0, ZWJ family sequence = 4.
6. `test_table_idempotent` — `process_output(process_output(x).text).text ==
   process_output(x).text` for table and mixed corpora.
7. `test_table_content_preserved_and_chunked` — 200-row table: every
   `_format_chunks` message ≤ `SAFE_LIMIT` UTF-16 units, all rows preserved.
8. `test_delivery_delivers_rendered_table` — `deliver_response()` edits the
   request message with the fenced aligned table (mock edit/reply; no live
   Telegram).

## Tests actually executed
- `python3 -m pytest tests/test_67_ai_output_pipeline.py -q --no-header` →
  **69 passed** (61 existing + 8 new) in 0.28s.
- `python3 -m pytest tests/ -q --no-header` → **1059 passed, 23 skipped,
  1 warning** in 56.51s (the warning is a pre-existing capture warning, not a
  failure; 23 skips are pre-existing).
- `python3 -m compileall -q backend tests` → OK.
- `git diff --check` → OK.

## Direct regression probes (read-only, against actual source)
- Task's Persian table → aligned fenced block; English table byte-exact
  aligned; guards unchanged; protected regions untouched; ragged/mismatched
  input fail closed; 200-row table → 5 messages all ≤ 4000 UTF-16 units with
  all 200 rows preserved; idempotent.
- Previous fixes all verified intact: `main.py`, `report.txt`, `example.com`,
  `data.v1.csv`, `e.g.`, `U.S.A`, `run main.py now` unchanged; `hello,world` →
  `hello, world`; `سلام،world` → `سلام، world`; `note;see` → `note; see`;
  `تشکر!انجام` → `تشکر! انجام`; `x,y` → `x, y`; `2*3*4`, `some_word_here`,
  `some__word__here` literal; `hi **bold**` → `hi bold`.
- Telethon probe (read-only): balanced ` ``` ` fence parses to
  `MessageEntityPre` with markers stripped (the delivery mechanism); an
  unbalanced fence (oversized table split mid-fence) degrades gracefully with
  no exception and no content loss.

## Validation results
- Focused suite: **69 passed**.
- Full suite: **1059 passed, 23 skipped, 1 warning** (no new failures; +8 over
  the previous 1051).
- compileall: clean. `git diff --check`: clean. Final diff inspected: only the
  two intended files, no unrelated changes; existing delivery/architecture
  boundaries intact (`deliver_response()` remains the sole delivery path).

## Security / architecture boundaries
- `deliver_response()` remains the centralized delivery boundary; Self Bot is
  the Telegram execution authority.
- No provider/AI/network/database changes; no new delivery path; no arbitrary
  RPC; no SQL; no schema changes; no credentials/session involvement; no
  message-content logging added.
- The table stage is synchronous, local, deterministic, meaning-preserving,
  and idempotent; it never infers intent and never calls an AI model.
- Tables inside protected regions are structurally impossible to transform
  (masked before scanning).

## Database / schema impact
None. No database code, no Supabase writes, no SQL/migrations, no manual schema
work required.

## Current limitations
- A table larger than the per-message UTF-16 budget is split by the existing
  `_split_text` (unchanged) and may split mid-fence; the unbalanced fence
  degrades gracefully (verified) and the full content is preserved across the
  continuation chunks.
- Fully RTL rows may be visually bidi-reordered inside the monospace block;
  logical order, padding, and determinism are unaffected.
- A cell containing a triple backtick fails the whole block closed (left as
  original text) to guarantee fences never nest.
- Protected URLs inside cells are masked during width computation, so their
  column may be slightly narrower than the restored URL (cosmetic only).

## Remaining work / blockers
- General Markdown entity delivery remains blocked by the earlier evidence
  gate and is out of scope.
- No other known delivery defect; no blocker for this chunk.

## Commit / delivery
- Implementation commit: `e972b5cbaaa113e1366343c204f8b5f378c65682` —
  `feat: render ai markdown tables as aligned telegram blocks`
  (contains the production change, the 8 tests, and this report).
- Base commit: `b305c97a7ba723d38b69773719e26f4085570b8d`.
- Push result: pushed to `origin/main` (`b305c97..e972b5c`).
- Remote verification: after `git fetch origin main`, local HEAD ==
  `origin/main` == remote `main` == `e972b5cbaaa113e1366343c204f8b5f378c65682`;
  remote `backend/ai/tools/delivery.py` contains `_render_tables`; remote test
  file contains the new table tests; remote `IMPLEMENTATION_REPORT.md` contains
  exactly one current report.
- Final working-tree state: clean (`## main...origin/main`, no changes).
- Files in the delivery commit: `backend/ai/tools/delivery.py`,
  `tests/test_67_ai_output_pipeline.py`, `IMPLEMENTATION_REPORT.md` only.
