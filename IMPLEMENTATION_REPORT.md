# Implementation Report — Span-Tracking Design for Telegram Markdown Entities: FAIL / BLOCKED

## Date
2026-08-28

## Repository / Branch / Base commit
- Repository: `https://github.com/Onlyicing1/Telegram-self-bot` (`origin`)
- Branch: `main`
- Base commit: `0c5be2755dfa335466a807cdb569a37aa8a9d960` (synced with `origin/main`)

## Task objective
Determine, by investigation only, whether there is a technically safe,
deterministic, meaning-preserving way to introduce **source-span tracking**
into the existing Markdown normalization/rendering pipeline so Telegram
entities could eventually be generated against the **actual rendered text**.
End with exactly one evidence-gate conclusion (PASS or FAIL/BLOCKED). No
production code or tests may be modified.

## Investigation scope
Only the delivery boundary and its tests were inspected:
- `backend/ai/tools/delivery.py`
- `tests/test_67_ai_output_pipeline.py`

Only deterministic constructs already relevant to the current code were
investigated: `**bold**`, `__bold__`, `*italic*`, `_italic_`, inline code,
fenced code, Markdown links (underline/strikethrough rejected — no existing
deterministic handling). No Markdown library, no AI parser, no intent
inference.

## Evidence gate

### Confirmed
- `process_output()` returns `.text = rendered` (the string actually
  delivered) while `_render_entities(text)` builds entities against the
  **raw** input. For `"hi **bold**"` this yields
  `MessageEntityBold(offset=5, length=4)`; the delivered text `'hi bold'` has
  UTF-16 length 7, so `offset+length = 9 > 7` (out of bounds) and the slice is
  the wrong substring. Same for `"🙂 **bold**"` (slices `'ld'`) and
  `"🙂 *سلام*"` (slices `'لام'`). [direct interpreter run]
- No production code consumes `.entities`/`entity_count`; `deliver_response()`
  reads only `processed.text` and passes plain text to `event.edit` /
  `event.reply`. The entity path is dead output; the only reason the
  offset-basis bug has not caused live corruption.
- `_render_markdown()` is a sequence of `re.sub(...)` passes with **no source
  span records**. `_normalize_plain()` likewise. Both run before any entity
  could be bound.
- Scanning the **rendered** string cannot recover stripped emphasis:
  `_render_entities('hi bold') => []`. Entity delivery therefore requires
  tracking spans **through** the strip, not re-scanning the output.
- Several cases are **inherently ambiguous** under the deterministic rules:
  - `***bold***` → rendered `'bold'` (all three stars stripped first by `**`
    then by `*`); the bold-vs-italic class is unknowable. `_render_entities`
    currently guesses `MessageEntityBold(2,5)`, which is corrupt against the
    4-unit rendered string.
  - `*x **bold** y*` → `'x bold y'`: nested/overlapping ranges collapse, so a
    single deterministic bold-and-italic entity set cannot be recovered.
- Link flattening changes the surface: `[label](https://x.com)` →
  `'label (https://x.com)'` (a synthesized space + moved `(url)`). Converting
  that into a native link entity would change the visible presentation
  (Telegram would render a tappable link over just `label`, hiding the URL),
  i.e. a **semantics/presentation change**, not offset-correctness.
- UTF-16 accounting across transforms is non-trivial:
  - NFC: `'e\u0301'` (2 UTF-16 units raw) → `'é'` (1 unit); decompositions
    change unit counts per code point.
  - Synthesized punctuation space: `'سلام,world'` → `'سلام, world'` inserts a
    code point with no raw counterpart (all subsequent offsets must shift).
  - `.strip()` removes leading/trailing code points.
  - `_protect()`/`_restore()` replace opaque tokens with short `\0{i}\0`
    placeholders and back — a unique 1:1 map, but it runs simultaneously with
    surrounding substitutions and must be re-entangled in any map.

### Likely
- A narrow subset (`**bold**`, `*italic*`, `_italic_`, `__bold__`, inline
  code, and preserved-literal intraword delimiters like `some_word_here`,
  `2*3*4`) could be made span-trackable with a focused rewrite of just the
  emphasis formatting pass, because the word-boundary rule is an explicit,
  deterministic predicate (not intent inference) and the plain-text output
  would be byte-identical.

### Unknown
- Whether users actually need native Telegram formatting at all. The
  repository contains no issue, failure log, or report evidencing a
  user-facing formatting defect; plain-text rendering is correct and complete.

## Exact defect / limitation investigated
Entity generation is unsafe because offsets are computed on the raw input while
delivery uses the rendered string. Correct entity delivery requires a
**deterministic raw→rendered span map** through normalization + Markdown
degradation + chunking, plus a safe gate so uncertain entities are omitted.

## Root cause
1. Offset-basis mismatch: `_render_entities(text)` vs. delivered `.text =
   rendered`.
2. Span loss: `re.sub`-based `_normalize_plain`/`_render_markdown` record no
   source spans, and emphasis delimiters are deleted during stripping, so they
   cannot be recovered from the output.
3. Inherent ambiguity for `***bold***`, nesting, and overlapping constructs:
   the rendered text no longer encodes which delimiter class applied.
4. Links require a presentation change (showing the URL text today) to become
   native link entities.

## Span-tracking feasibility analysis
Span-tracking **is** technically achievable for the unambiguous emphasis/code
subset: each `re.sub` is segment-preserving outside matches and can be replayed
with `re.finditer` to build a per-segment raw→rendered map; the word-boundary
predicate is deterministic and byte-identical output is preserved. However, a
complete implementation must also thread NFC decomposition
(`unicodedata.normalize`), synthesized-space insertion, `.strip()`, the
`_protect`/`_restore` token map, link flattening, heading/list/quote prefix
rewrites, and then map entities (in UTF-16 units on the rendered string) into
chunk-relative offsets across `_format_chunks` headers, continuation markers,
and split boundaries. Each piece is individually deterministic, but *all* must
compose without any ambiguity, on the already-correct, hardened pipeline.

## Transformation analysis
| Transformation | Input | Output | Spans preservable | Ambiguity / failure |
|---|---|---|---|---|
| NFC normalize | code points | recomposed | yes (codepoint-level, lengths change) | low, but must be mapped |
| newline normalize | CRLF/CR | LF | yes (1:1 per run) | none |
| Persian char swap | ي→ی, ك→ک | same UTF-16 width | yes | none |
| `_protect`/`_restore` | token→`\0i\0`→token | same | yes (unique 1:1) | must re-entangle after other subs |
| whitespace collapse | runs→1 space | shorter | yes | none (entities never start inside runs) |
| `.strip()` | drop edge whitespace | shorter | yes | drop is 1:0 |
| punctuation space splice | `a,b`→`a, b` | longer | yes (insertion) | insertion shifts offsets |
| link flatten | `[t](u)`→`t (u)` | longer/reordered | yes | native link = presentation change |
| `**`/`__` strip | `**x**`→`x` | delimiters deleted | yes | `***bold***` class ambiguous → omit |
| `*`/`_` strip | `*x*`→`x` | delimiters deleted | yes | intraword literal rule preserved |
| headings/lists/quotes | → rewrites | shorter/symbol | yes | none |
| inline code | unchanged text | unchanged | yes (entity over inner) | backticks remain visible |
| fenced code | unchanged text | unchanged | yes (entity over inner) | fences remain visible |

## Chunk / UTF-16 analysis
Entity offsets must use UTF-16 units (`_utf16_units`). After chunking:
- First message = `header + body[0]`: entity offset += `_utf16_units(header)`,
  then drop any entity not fully inside `body[0]`.
- Continuations = `body[i] + "\n\n_(i/n)_"`: entity offsets remain
  body-relative; the appended marker does not affect leading offsets; drop
  entities that would cross the split.
- Entities crossing a chunk boundary are **dropped (fail closed)**, or (if the
  split falls on a paragraph/newline/word boundary) they naturally don't cross.
This is deterministic bookkeeping using the existing UTF-16 primitive. It is
feasible but adds a real, error-prone surface on top of the hardened
`_split_text`/`_format_chunks`.

## Security / architecture analysis
No security or execution boundary would need to change. `deliver_response()`
remains the sole delivery entry; the Self Bot stays the Telegram execution
authority; `_protect` token contents remain opaque; no provider/AI/network/
database/Telegram-RPC calls are added; no message-content or credential logging
is introduced. A future PAWS check on entities (`_entity_valid`) plus fail-closed
omission keeps uncertain entities from corrupting text.

## Evidence / experiments
Read-only interpreter probes against the actual source (no files modified):
- `_render_entities('hi **bold**')` → `MessageEntityBold(5,4)`, invalid vs
  delivered `'hi bold'` (5+4>7). Its raw-basis slice is the wrong substring.
- `_render_entities('🙂 **bold**')` → `(5,4)`, slice `'ld'`; invalid.
- `_render_entities('🙂 *سلام*')` → `(4,4)`, slice `'لام'` on the delivered
  `'🙂 سلام'`; invalid.
- `***bold***` → rendered `'bold'`; `_render_entities` guesses `(2,5)` (invalid
  on 4-unit rendered). Correct class unknowable.
- `_render_entities('hi bold')` → `[]` (stripped emphasis not recoverable by
  re-scan).
- `[label](https://x.com)` → `'label (https://x.com)'`; `'سلام,world'` →
  `'سلام, world'` (synthesized space); NFC `'e\u0301'(2u)`→`'é'(1u)`;
  `'  **bold**'` → `'bold'` (strip + emphasis delimiters gone); fenced code
  keeps its fences.

## Final evidence-gate decision
**FAIL / BLOCKED** — a safe deterministic implementation is not currently
justified.

Reasons:
1. Delivering entities requires a span-aware rewrite of the already-correct,
   hardened `_normalize_plain` + `_render_markdown`, threading NFC, synthesized
   insertions, strip, protection, link flattening, and block rewrites into one
   coherent raw→rendered map, plus chunk-relative remapping — an
   **architectural rewrite**, not a small rule. The task explicitly asks
   whether this crossed into "architectural rewrite that should remain
   blocked"; it has.
2. Several mandated cases are **inherently ambiguous** (`***bold***`, nesting,
   overlapping). The only deterministic outcome is **omission**, which yields
   essentially zero user-visible benefit over the current, already-safe plain
   text.
3. Links and fence retention would require **changing rendered presentation
   semantics** — out of scope and risky.
4. There is **no repository evidence of a current user-facing formatting
   defect**; this is a feature, not a repair. The evidence gate governs safe
   repair, and nothing is broken to repair — plain-text delivery is correct and
   complete.

## Next-step recommendation
Remain blocked. The existing plain-text delivery (normalization, intraword
emphasis preservation, protected tokens, UTF-16 splitting, formatter fallback)
is correct and must not change. The dormant `_render_entities`/`RenderedOutput.
entities` path is broken and unused; it must not be wired into delivery.

**Evidence that would be required before reopening:** a concrete, repository-
documented user-facing need for native formatting (issue/report/saved example),
a decision to accept the presentation change for links and to omit
`***bold***`/nested cases, and acceptance of a dedicated span-tracking rewrite
as a distinct feature work-stream (not a repair). Absent that, still safe to
omit.

## Files changed
- `IMPLEMENTATION_REPORT.md` — completely replaced with this single
  investigation report (the only permitted repository modification).
- `backend/ai/tools/delivery.py` — **unchanged**.
- `tests/test_67_ai_output_pipeline.py` — **unchanged**.
- `DATABASE_ARCHITECTURE.md`, `INVESTIGATION.md`, migrations, Supabase —
  **unchanged**.

## Tests
No production/test modifications were made. Only read-only interpreter probes
(recorded under Evidence/experiments) were executed. The last committed suite
remains `1044 passed, 23 skipped, 1 warning` (unchanged from the previous
delivered chunk; not re-run because no code changed).

## Database / schema impact
None. No schema changes, no migrations, no Supabase writes, no SQL executed.

## Commit / delivery
- Base commit (start of chunk): `0c5be2755dfa335466a807cdb569a37aa8a9d960`
- Local HEAD == `origin/main` == remote `main` at start of chunk.
- This investigation produces a report-only change; commit/push facts are
  recorded after delivery.
- Working tree at end of investigation (before optional report commit): report
  changed only; no production/test code changed.