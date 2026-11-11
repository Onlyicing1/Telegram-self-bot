# ROADMAP — Appearance Composition Deduplication

## Current task

**Make every configured text fragment render exactly once on LifeOS Glass UI
main pages.** The owner reported identical text repeated consecutively in one
panel. `appearance_service.compose_text()` joined the global header body, the
per-page body override and the page handler's own body without checking whether
two composition layers carried the same text, so identical configuration
produced the same paragraph two or three times.

The fix is confined to composition at the render boundary: the same layers are
still applied, the same order is kept, and handler/dynamic text is never
line-filtered. Only whole fragments supplied by DIFFERENT composition layers are
collapsed when identical.

## Verified execution state

| Phase | State | Evidence |
|---|---|---|
| Read `appearance_service` composition/rendering functions | Done | `PAGE_DEFAULTS`, `compose_text`, `with_preview`, `apply_to_inline_results`, `_split_rendered_text`, `_compose_rendered` |
| Read both render boundaries and the Appearance handler | Done | `panels._apply_appearance`/`_handle_panel`/`_render_and_edit`, `inline_engine` inline router, `handlers/appearance.py` header/page persistence |
| Trace duplication in initial inline rendering | Done | One composition per built result; builder output is raw, `apply_to_inline_results` composes once |
| Trace duplication on callback navigation | Done | `_handle_panel` composes once; `_render_and_edit` and `_finalize_panel` do not compose again |
| Implement fragment-aware composition | Done | `_distinct_fragments` + `page_fragments` in `compose_text` |
| Add regression tests | Done | 10 new tests in `tests/test_appearance_ui.py` |
| Run focused Appearance/panel tests | Done | `165 passed` across the six Appearance/panel-delivery modules |
| Run the full test suite | Done | `7383 passed, 27 skipped, 1 failed` — the one failure is the pre-existing scheduler test |
| Compilation / whitespace checks | Done | `py_compile` exit 0; `git diff --check` exit 0 |
| Commit and push | Done | Single commit `fix(appearance): render composition-layer text once`, pushed to `origin/main` |

## Behaviour contract pinned by this task

- Identical non-empty text configured as both the global header body and a page
  body renders once.
- Identical text arriving from the page override and the handler's live body
  renders once.
- Different fragments keep their order: page title, header description, page
  override, live handler body.
- Page overrides never leak to another page; identical overrides on two pages
  stay isolated per page.
- Pages with an empty default body and pages with a non-empty default body keep
  their existing default output.
- Live/dynamic content is never dropped, shortened or line-filtered; only a
  duplicate whole fragment from another layer is collapsed.
- Sub-views, confirmations, pickers, pagination, action results and progress
  messages never pass through composition.
- Repeated rendering starts from the handler's raw body each time and does not
  accumulate text.
- The initial inline result and the callback-rendered page produce the same
  composed text.

## Explicitly out of scope

- Removing the global header or disabling Appearance customization.
- Changing where Appearance settings are stored or how they are written.
- Database schema, migrations, SQL or Supabase changes.
- Deduplicating identical lines that are legitimately repeated inside one
  handler's own dynamic output.
- Any new rendering framework or parallel composition system.
