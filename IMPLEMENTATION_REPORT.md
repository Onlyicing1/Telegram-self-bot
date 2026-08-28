# Implementation Report — Preserve Dot and Colon Literals in AI Output

## Date
2026-08-28

## Repository / Branch / Base commit
- Repository: `https://github.com/Onlyicing1/Telegram-self-bot` (`origin`)
- Branch: `main`
- Base commit (start of chunk): `61c7ab26fdae2af7664847cbe2b9c153cf4dfe5c` (synced with `origin/main`)

## Task objective
Implement exactly ONE deterministic delivery repair rule: stop
`_normalize_plain()` from inserting sentence-spacing after `.` and `:` when
those characters belong to ordinary technical/literal text (filenames,
extensions, bare domains, abbreviations). This is the single PASSed defect from
the preceding deterministic audit.

## Exact defect addressed
The sentence-spacing rule in `_normalize_plain` inserted a space after `.` / `:`
(and other punctuation) when a letter followed, corrupting technical literals:

| Input | Previously output | Now |
|---|---|---|
| `main.py` | `main. py` | `main.py` |
| `report.txt` | `report. txt` | `report.txt` |
| `example.com` | `example. com` | `example.com` |
| `data.v1.csv` | `data. v1. csv` | `data.v1.csv` |
| `e.g.` | `e. g.` | `e.g.` |
| `U.S.A` | `U. S. A` | `U.S.A` |
| `run main.py now` | `run main. py now` | `run main.py now` |

## Root cause
`_normalize_plain` used
`re.sub(r"([,.;:!?،؛؟])(?=[A-Za-zА-Яа-яء-ي])", r"\1 ", text)`.
`.` and `:` are ubiquitous in filenames, extensions, bare domains, and
abbreviations, but were being treated as sentence/clause punctuation by the
space-splice rule. The audit confirmed the corruption directly against the
current source.

## Exact production change
File: `backend/ai/tools/delivery.py`, inside `_normalize_plain`.

The splice character class was narrowed from `[,.;:!?،؛؟]` to `[,;!?،؛؟]`
(dropping `.` and `:`):

```python
# previous
text = re.sub(r"([,.;:!?،؛؟])(?=[A-Za-zА-Яа-яء-ي])", r"\1 ", text)
# now
text = re.sub(r"([,;!?،؛؟])(?=[A-Za-zА-Яа-яء-ي])", r"\1 ", text)
```

A short comment documents the rationale. This is the minimal deterministic rule;
no parser, no AI, no heuristic filename/domain detection was added. The separate
space-before-punctuation rule on the prior line is unchanged.

## Behavior changed
- FIXED: `.` and `:` literals are no longer split (filenames, extensions, bare
  domains, abbreviations) — verified unchanged: `main.py`, `report.txt`,
  `example.com`, `data.v1.csv`, `e.g.`, `U.S.A`, `run main.py now`,
  `config.json`, `12:30`, `v2.3.1`, `node:18`.
- PRESERVED: intended sentence/clause spacing for `,` `;` `!` `?` and Arabic
  `،` `؛` `؟`: `hello,world` → `hello, world`, `سلام،world` → `سلام، world`,
  `note;see` → `note; see`, `تشکر!انجام` → `تشکر! انجام`, `x,y` → `x, y`.
- PRESERVED: intraword emphasis repair (`2*3*4`, `some_word_here`,
  `some__word__here`), clearly-delimited emphasis degradation, protected
  regions (URLs, `@user`, `/cmd`, inline/fenced code), UTF-16-safe splitting,
  chunk reconstruction, formatter-failure fallback.

## Intentionally not changed
Markdown entity work (`_render_entities`/entities), UTF-16 splitting logic,
chunk formatting logic, `ProviderManager`, providers, `Dispatcher`, `Engine`,
`EngineResult`, `ai_unified`, Telegram execution authority, Supabase/database/
migrations, and all other normalization rules.

## Exact files changed
- `backend/ai/tools/delivery.py` — the splice class fix (+ comment).
- `tests/test_67_ai_output_pipeline.py` — added focused regression tests.
- `IMPLEMENTATION_REPORT.md` — completely replaced with this current-state
  report.

## Tests added / updated
Appended to `tests/test_67_ai_output_pipeline.py`:
- `test_dot_extension_and_domain_literals_are_preserved` — `main.py`,
  `report.txt`, `example.com`, `data.v1.csv`, `e.g.`, `U.S.A`, `run main.py
  now`, `see config.json and data.json now` unchanged.
- `test_colon_in_technical_literals_left_intact` — `12:30`, `ratio 3:2`,
  `v2.3.1`, `node:18` unchanged.
- `test_intended_sentence_spacing_is_preserved` — `hello,world`,
  `سلام،world`, `note;see`, `تشکر!انجام`, `x,y` still spaced.
- `test_dot_repair_preserves_protected_regions` — URL, `@user`, `/cmd`,
  inline/fenced code intact alongside dot literals.
- `test_dot_repair_is_idempotent` — `process_output(process_output(x).text)`
  idempotent for the regression corpus.
- `test_dot_and_emphasis_coexist` — `.py` intact with nearby emphasis.
- `test_delivery_delivers_dot_preserved_text` — `deliver_response()` delivers
  the repaired text unchanged.

No existing assertions were weakened or removed.

## Tests actually executed / validation
- `python3 -m pytest tests/test_67_ai_output_pipeline.py -q --no-header`
  → **61 passed**
- `python3 -m pytest tests/ -q --no-header`
  → **1051 passed, 23 skipped, 1 warning** (pre-existing
  PendingDeprecationWarning re multipart import)
- `python3 -m compileall -q backend tests` → passed
- `git diff --check` → passed
- Direct regression verification (interpreter) confirmed every required
  unchanged/spaced case and that previous fixes + protected regions are intact.

All results are real and observed during this chunk; no counts are invented.

## Security / architecture boundaries
The Self Bot remains the Telegram execution authority. `deliver_response()`
remains the centralized delivery boundary. No new delivery path, no arbitrary
Telegram RPC, no provider/AI/network/database change. The repair is synchronous,
local, deterministic, meaning-preserving, and idempotent — confined to a single
regex character class inside `_normalize_plain`.

## Database / schema impact
None. `DATABASE_ARCHITECTURE.md`, migrations, Supabase, and no SQL were
changed/executed.

## Current limitations
Dot/colon literals are now preserved, but a `.`/`:` used as genuine sentence
termination with no following space (e.g. `Done.now`) will similarly be left
unexseparated — an intentional, conservative consequence of this minimal rule
(no intent inference). The audit classified the adjacent mixed `*`/`_` emphasis
output and the harmless whitespace-insertion idempotence as not defects; they
remain so.

## Remaining work / blockers
None for this repair. Markdown entity delivery remains blocked (per prior
report); this chunk intentionally did not reopen it.

## Commit / delivery
- Primary implementation commit: `0e816ec8785b283e22c710cb19593f7b59bf1c28` — `fix: preserve dot and colon literals in ai output` (delivery.py + tests + report)
- Push result: pushed to `origin/main` (`61c7ab2..a8d0210` — implementation commit `0e816ec` + facts commit)
- Remote verification: local HEAD == `origin/main` == remote `main` == `a8d02102a3628c10b33466da0b3e36a771bdbd52`; verified via `git fetch origin main`
- Final working-tree state: clean