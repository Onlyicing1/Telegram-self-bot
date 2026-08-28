# Implementation Report — Deterministic AI Output Delivery Audit

## Date
2026-08-28

## Repository / Branch / Base commit
- Repository: `https://github.com/Onlyicing1/Telegram-self-bot` (`origin`)
- Branch: `main`
- Base commit (start of audit): `ceb9c797d4b1db5dfa63e7c9aada00b6992952dc` (synced with `origin/main`)

## Task objective
Audit the complete centralized plain-text AI output delivery path to determine
whether any other deterministic, source-proven delivery defect exists, without
reopening the (blocked) Markdown entity work. Investigation only — no
production/test/database changes.

Audited path (exact):
```
ProviderManager → Dispatcher → Engine → EngineResult
→ ai_unified._execute_ai → deliver_response → process_output
→ _format_chunks → Telegram edit/reply
```

## Investigation scope
Source inspected:
- `backend/ai/tools/delivery.py` (full)
- `backend/bot/handlers/ai_unified.py` (the `deliver_response` call site,
  empty/failure branches, silent-delete guard)
- `tests/test_67_ai_output_pipeline.py` (full)

Read-only interpreter probes were run against the actual source (no file
modifications). Focused test baseline was executed and passed.

## Evidence gate

### Confirmed
- **`_normalize_plain` corrupts dot/colon-delimited literals.** The splice rule
  `re.sub(r"([,.;:!?،؛؟])(?=[A-Za-zА-Яа-яء-ي])", r"\1 ", text)` inserts a space
  after `.`/`:` (and other punctuation) when a letter follows, splitting
  filenames, extensions, bare domains, and abbreviations.
  Reproduced against current source: `main.py` → `main. py`; `report.txt` →
  `report. txt`; `example.com` → `example. com`; `data.v1.csv` → `data. v1.
  csv`; `e.g.` → `e. g.`; `U.S.A` → `U. S. A`; `run main.py now` → `run main.
  py now`.
  Expected output: unchanged (literal identifiers/domains/abbreviations).
  Root cause: `.`/`:` are sentence/clause punctuation in the intended rule but
  are ubiquitous in technical literals.
- Protected regions are unaffected: URLs (`https://x.com/...`), inline code
  `` `main.py` ``, fenced code ```` ```main.py``` ````, `@user`, `/cmd` all
  survive intact.
- The intended punctuation-spacing behavior works and is preserved: `hello,world`
  → `hello, world`; `سلام،world` → `سلام، world`; `note;see` → `note; see`;
  `تشکر!انجام` → `تشکر! انجام`; `x,y` → `x, y`.
- Baseline focused suite passes unchanged: `tests/test_67_ai_output_pipeline.py`
  → `54 passed`.

### Likely
- Dropping `.` and `:` from the splice set is the minimal fix and, in a read-only
  replication of `_normalize_plain`, restores all 8 corrupted cases while leaving
  every intended-spacing case identical (see Next-step recommendation).
- No user-facing formatting-entity defect reopens; the adjacent/touching mixed
  `*`/`_` emphasis outputs (`*a*_b_` → `'*a*b'`) are the deterministic
  consequence of the documented `\w`-based word-boundary rule (which treats `_`
  as a word character) and are consistent with it — a limitation, not a defect.

### Unknown
- Whether the `.`-splice has caused a user-visible problem in practice (no
  repository log/issue evidences it; the corruption is purely text-shaping and
  deterministic).

## Audit results

### Unicode normalization
`_normalize_plain` performs NFC normalization, CRLF→LF, a Persian letter swap
(`ي`→`ی`, `ك`→`ک` when Persian markers present), protected-token masking,
whitespace collapse, punctuation spacing, and a final space-after-punctuation
pass. NFC is deterministic and meaning-preserving (`é` ≡ decomposed `e\u0301`);
it can change UTF-16 length per code point but that is only relevant once
entities are involved (blocked). Persian/CJK/emoji content is unaltered.
**Defect found: the punctuation-spacing pass (dot/colon split) — see Confirmed
defects.**

### Protected-region handling
`_protect`/`_restore` mask URLs, `@user`, `/cmd`, inline `` ` `` and fenced
```` ``` ```` regions with unique `\0{i}\0` placeholders, then restore 1:1.
Confirmed no collision (placeholders use NUL + index, unreserved), no reorder,
no content loss for URLs/usernames/commands/inline+fenced code regardless of
internal `*`/`_`/emoji/Persian/Arabic/CJK. Adjacent protected regions restored
correctly. No failed-restoration path observed. **No defect.**

### Markdown degradation
Emphasis stripping uses `(?<!\w)`…`(?!\w)` boundaries where `\w` includes `_`,
so intraword `2*3*4`, `some_word_here`, `some__word__here`, `foo_bar_baz`,
Arabic/CJK word-adjacent delimiters (`و*علی*کم`) all stay literal — confirmed.
Clearly delimited `*italic*`, `**bold**`, `_em_`, `__under__` degrade. Adjacent
mixed echoes (`*a*_b_` → `'*a*b'`, `**a**_b_` → `'*a*b'`) are the deterministic
rule output (a late `*`/`_` gets read as a word char); classified as consistent
rule behavior, not a rule violation, and no unambiguous literal is deleted.
Bare list/heading/quote markers `- x`→`• x`, `# x`→`x`, `> `→`▎ ` are by-design
Markdown block conversions. **No new defect beyond the confirmed one.**

### Empty-output handling
`process_output` raises `ValueError` for empty/whitespace-only input and when
rendering yields empty. Standalone `*`, `**`, `_`, `__`, `` ` ``, `####` etc.
remain literal (not turned empty). `>` → `▎ ` by blockquote rule. In
`ai_unified`, delivery runs only when `result.success and result.response`;
`deliver_response` handles empty `response_text` with an explicit error edit. No
silent success / accidental empty-delivery path found. **No defect.**

### Chunk formatting
`_format_chunks`: header prefix on message 1; continuation `\n\n_(p/n)_` markers
on the rest, with correct numbering and totals tied to `len(body)`; deterministic;
reconstruction `"".join(parts)` with continuation markers stripped equals the body
across ASCII/BMP/supplementary/Persian/Arabic/CJK/long-unbroken/newline-heavy
input. Surrogate pairs never split; every chunk ≤ `SAFE_LIMIT` (4000 UTF-16
units). **No defect.**

### Formatter failure containment
If `process_output`/rendering raises, `deliver_response` catches it, logs only
`type(exc).__name__`, and continues with the original validated text. Confirmed:
a raised `RuntimeError` still delivers the full original text unchanged, with
`success=True` and no sensitive detail logged. **No defect.** (This is the
documented `AI_OUTPUT_NORMALIZATION_FALLBACK` path.)

### Message edit/reply semantics
First chunk → `event.edit`, subsequent chunks → `event.reply`. Multi-chunk
partial failure returns `success=False` with honest `chunks_delivered/total` and
the error set; no false success, no silent duplication (no retry loop). Empty
response → an explicit error edit. The handler's silent-delete guard reverts the
request message rather than emitting a confirmation. **No defect**; only
inherent multi-message partial-delivery semantics are present (in a multi-chunk
response, an error after chunk 1 leaves a partially delivered response plus a
`success=False` — deterministic, not silently inconsistent).

### Idempotence
A 18-case corpus plus a 4000-trial deterministic fuzzer (`random.seed(7)`):
`process_output(process_output(x).text)` is idempotent for all literal/protected
content. The only non-idempotence observed is a **harmless whitespace
insertion**: when emphasis-stripping leaves a letter adjacent to punctuation, a
second pass adds the sentence-space (`سلام؟_y_` → `سلام؟y` → `سلام؟ y`). This
**never deletes or alters literal characters and never corrupts URLs/protected
tokens** — classified as harmless normalization, not corruption, given the
already-adopted punctuation-spacing rule.

### Reconstruction invariant
Verified for ASCII, BMP, supplementary-plane, emoji, Persian, Arabic, CJK,
mixed scripts, long unbroken strings, paragraph boundaries, newline-heavy
content, and Markdown punctuation at/over the `SAFE_LIMIT` boundary:
`delivered_body == intended_processed_body` after stripping only continuation
metadata (`\n\n_(p/n)_`). **Holds — no defect.**

### Boundary fuzzing
Deterministic fuzzing found no silent truncation, no chunk-limit violation, no
surrogate splitting, and no protected-region corruption. The two observed
effects were (a) the harmless whitespace-insertion non-idempotence (above) and
(b) the confirmed `.`/`:` literal split. **No additional defects.**

## Confirmed defects
### Defect 1 — Punctuation-space rule splits dot/colon literals
- **Exact input:** `main.py` (also `report.txt`, `example.com`, `data.v1.csv`,
  `e.g.`, `U.S.A`, `run main.py now`).
- **Actual output:** `main. py`, `report. txt`, `example. com`,
  `data. v1. csv`, `e. g.`, `U. S. A`, `run main. py now`.
- **Expected output:** unchanged (`main.py`, `report.txt`, `example.com`, …)
  — these are literal identifiers/domains/abbreviations, not sentence/clause
  punctuation.
- **Root cause:** in `_normalize_plain`,
  `re.sub(r"([,.;:!?،؛؟])(?=[A-Za-zА-Яа-яء-ي])", r"\1 ", text)`. Including
  `.` and `:` treats filename/domain/extension boundaries as sentence
  punctuation.
- **Reproduction:** direct interpreter run against current source (above).
- **Architectural location:** `backend/ai/tools/delivery.py`, `_normalize_plain`
  (single substring substitution; no provider/AI/database/Telegram change).
- **Deterministic fixability:** Yes — local, deterministic, no intent inference.
- **Proposed minimal deterministic rule (documented only, NOT implemented in
  this audit chunk):** narrow the splice class to sentence punctuation only —
  `re.sub(r"([,;!?،؛؟])(?=[A-Za-zА-Яа-яء-ي])", r"\1 ", text)` (drop `.` and
  `:`). A read-only replication (`_normalize_plain` with only this change) fixed
  all 8 corrupted cases above while leaving every intended-spacing case
  (`hello,world`, `سلام،world`, `note;see`, `تشکر!انجام`, `x,y`) identical and
  the existing focused tests green.

## Not defects (explicitly tested, confirmed intentional/rule-consistent)
- Intraword emphasis preservation (`2*3*4`, `some_word_here`,
  `some__word__here`, `foo_bar_baz`, Arabic/CJK word-adjacent delimiters).
- Word-boundary emphasis degradation (`*italic*`, `**bold**`, `_em_`,
  `__under__`).
- Adjacent/touching mixed `*`/`_` output (`*a*_b_` → `*a*b`) — deterministic
  rule consequence of the `\w`-includes-`_` boundary.
- Block conversions: `- x` → `• x`, `# x` → `x`, `> ` → `▎ `.
- `.strip()` trailing/leading whitespace; `<span> ` collapse.
- Chunk continuation markers, numbering, first-message header, reconstruction.
- Formatter-failure fallback preserving original text; partial-delivery
  `success=False` semantics.
- Harmless whitespace-insertion idempotence (never deletes literals/URLs).

## Unknowns
- Practical user-visible impact of the `.`/`:` literal split (no repo evidence;
  defect is deterministic and content-altering, so it stands regardless).
- Whether native Markdown entities would be desired if ever unblocked.

## Security / architecture impact
No security or execution boundary changes. `deliver_response` remains the sole
delivery entry; Self Bot stays the Telegram execution authority; no
provider/AI/network/database/Telegram-RPC change. The confirmed defect and its
proposed fix are confined to `_normalize_plain` inside the existing centralized
boundary. Protected-token content remains opaque (fix only affects the plain
splice pass, which runs on protected-masked text).

## Database / schema impact
None. No schema changes, no migrations, no Supabase writes, no SQL executed.

## Tests / experiments actually executed
- `python3 -m pytest tests/test_67_ai_output_pipeline.py -q --no-header`
  → **54 passed** (read-only baseline; no file edits).
No other suite was run because no code was modified. Only read-only interpreter
probes (reproduction + a `_normalize_plain` replication of the proposed fix)
were executed, all documented above. No test counts are invented.

## Final evidence-gate decision
**PASS — actionable deterministic delivery defect(s) identified.**
Exactly one confirmed defect: `_normalize_plain`'s punctuation-space splice
splits dot/colon literals (filenames, extensions, bare domains, abbreviations).
It is reproducible, deterministic, meaning-altering, and has a small local fix
inside the existing architecture without intent inference, protected-region
change, or UTF-16/chunking change.

## Next-step recommendation
Implement exactly ONE rule in the next implementation chunk:
**Prevent the punctuation-space splice from splitting `.`/`:` literals.**
- Files expected: `backend/ai/tools/delivery.py` (`_normalize_plain`) and
  `tests/test_67_ai_output_pipeline.py` (regression) + `IMPLEMENTATION_REPORT.md`.
- Minimal change: splice set `[,.;:!?،؛؟]` → `[,;!?،؛؟]` (drop `.` and `:`).
- Deterministic, idempotent, meaning-preserving; protected regions and existing
  tests unaffected.
- Focused tests: `main.py`/`report.txt`/`example.com`/`e.g.`/`U.S.A`/`data.v1.csv`
  preserved; `hello,world`/`سلام،world`/`note;see`/`x,y` still spaced; protected
  URL/`@user`/`/cmd`/inline+fenced code unchanged; idempotence; malformed/
  ambiguous input unchanged; integration through `deliver_response`.
- DO NOT touch the entity work, UTF-16 splitter, chunk formatting, or provider
  code.

## Files changed
- `IMPLEMENTATION_REPORT.md` — completely replaced with this single audit report
  (the only repository modification this investigation chunk).
- Production files: **None**.
- Test files: **None**.
- `backend/ai/tools/delivery.py`, `backend/bot/handlers/ai_unified.py`,
  `tests/test_67_ai_output_pipeline.py`, `DATABASE_ARCHITECTURE.md`,
  `INVESTIGATION.md`: **unchanged**.

## Commit / delivery
- Base commit (start of audit): `ceb9c797d4b1db5dfa63e7c9aada00b6992952dc`
- Audit report commit: `250829400682d8d2d7650ecc1c5e2a2a741f9ae2` — `docs: record deterministic ai output delivery audit (pass)`
- Push result: pushed to `origin/main` (`ceb9c79..2508294`)
- Remote verification: local HEAD == `origin/main` == remote `main` == `250829400682d8d2d7650ecc1c5e2a2a741f9ae2` (verified via `git fetch origin main`)
- Final working-tree state: clean
- Investigation-only: no production/test/database code changed.