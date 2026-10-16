# LifeOS Guide Website — Phase 1 (repository audit + first implementation)

Status: **COMPLETE (Phase 1)** — site, tests and documentation delivered.
Visual/browser verification and deployment were **not** performed (see
*Not verified*).

Canonical status record referenced from `ROADMAP.md` §37.

---

## 1. Repository verified before editing

| Fact | Value |
|---|---|
| Repository root | `/home/daytona/codebase` |
| Remote | `origin` → `https://github.com/Onlyicing1/Telegram-self-bot.git` (unchanged, never rewritten) |
| Branch at start | `premium-emoji-attribution`, HEAD `9db6f73` |
| `origin/main` at start | `9db6f7318574aa24106dd1773c525451348a329c` — `git rev-list --left-right --count origin/main...HEAD` = `0 0` (identical, no divergence) |
| Working tree at start | `M backend/services/premium_emoji_inline_service.py`, untracked `backend/test_patch_story.md`, `scratchpad/`, `telegram-self-bot/` — all pre-existing, all left untouched and unstaged |

## 2. Inspection findings (what the repository actually is)

* **Runtime**: one headless Python `asyncio` process (`python -m backend.main`),
  Telethon `StringSession` self-bot + FastAPI/Uvicorn in the same loop.
* **Existing web surface**: FastAPI in `backend/web/app.py` (325 lines) —
  `/health`, `/api/*`, and a React SPA. `mount_static()` mounts
  `dist/assets` and installs a `/{full_path:path}` catch-all that answers
  **every** path with `dist/index.html`, so anything new must be registered
  *before* it.
* **Existing frontend**: `src/` — React 18 + Vite 5 + Tailwind 3 (dashboard
  SPA), built by `npm run build` (`tsc -b && vite build`) into `dist/`.
  `tsconfig.json` `include` is `["src"]` only.
* **Deployment**: `render.yaml` + `Procfile` run `python -m backend.main`;
  there is no `buildCommand`, and `dist/` is gitignored (`ls dist` shows a
  stale local build from 2026-08-22). The web API is therefore always served,
  while the SPA is not.
* **Docs conventions**: `AGENTS.md` (§2 layout, §12 endpoints), `README.md`
  (stale in places — see §7), `docs/implementation/*.md` feature records,
  `ROADMAP.md` bounded status sections (§36 is the most recent).
* **Test conventions**: `pytest`, 225 files under `tests/`, heavy use of
  source/contract assertions; web tests use `fastapi.testclient.TestClient`.
* **Guide prototype**: **not present.** `git ls-files` has no guide asset, and
  the only `.html` files in the repository are `index.html` (dashboard entry)
  and the stale `dist/index.html`. The nested untracked `telegram-self-bot/`
  directory is a separate clone in the workspace, not part of this repository,
  so it was not inspected as a source of design assets. Nothing was copied from
  an unavailable prototype; the site was built from the brief.
* **Toolchain present**: Python 3.10 venv with `fastapi 0.111`, `starlette
  0.37`, `httpx 0.27`; Node v22.23.1 (`node --check`, ESM).

## 3. Chosen integration (smallest safe option)

A **standalone static site in `guide/`** — no build step, no framework, no new
dependency, no package — plus a **small additive read-only static mount** in
`backend/web/app.py` (one path constant + one `StaticFiles` mount, six added
lines) registered before the SPA catch-all.

Why not the alternatives:

* *A second Vite entry* would require editing `vite.config.ts`
  (`build.rollupOptions.input`) and mixing public docs into the owner
  dashboard SPA — explicitly discouraged by the task brief.
* *A React route inside the dashboard* would couple public documentation to the
  owner dashboard's API/auth surface.
* *A new server/static host* would add an operational component for a
  documentation site that needs none.

The mount adds no API, no auth, no database access and no runtime behaviour
change: it serves four static files read-only. Because it needs no build step,
`/guide` is reachable wherever the FastAPI app already runs from this checkout.

## 4. Implemented

| File | Purpose |
|---|---|
| `guide/index.html` (NEW) | Persian-first, RTL document: skip link, sticky header (brand, category nav, working search), hero with CTAs, 6-card category grid, six articles, FAQ with `<details>/<summary>`, footer, `<noscript>` note |
| `guide/styles.css` (NEW) | Dark theme with purple `#a855f7` / cyan `#22d3ee` accents, RTL-first logical properties, responsive breakpoints (880 px / 520 px), `:focus-visible`, `prefers-reduced-motion`, mobile nav |
| `guide/search.js` (NEW) | Pure, DOM-free search: Persian/Arabic normalisation (Arabic yeh/kaf, alef variants, heh, diacritics, tatweel, ZWNJ/ZWJ, Persian + Arabic-Indic digits, case), index building, AND-token scoring, Persian digit/status formatting |
| `guide/guide.js` (NEW) | DOM wiring: live search, filtered article/card view, results list, empty state, clear/reset, `/` and `Esc` shortcuts, mobile nav toggle, `aria-expanded` handling |
| `guide/README.md` (NEW) | What the site is, how to view it (plain static host, the app at `/guide`, Vite dev), the content/evidence policy, and how to extend it |
| `backend/web/app.py` | `_GUIDE` path + read-only `StaticFiles(html=True)` mount at `/guide`, registered before the SPA catch-all |
| `tests/test_69_guide_site.py` (NEW, 38 tests) | Structure, link/integrity, content-evidence, no-secret/no-network, styling, HTTP delivery and Node-executed search behaviour |
| `AGENTS.md` | Layout entry for `guide/`, `/guide/*` row in §12, and a short paragraph making the site's runtime independence explicit |
| `ROADMAP.md` | Bounded status section §37 (the §36 font record and the Emoji roadmap are untouched) |

Search works without any backend: the index is built in the browser from the
`data-title` / `data-keywords` / article text of the shipped HTML.

## 5. Content evidence (what is documented, and why it is true)

Every article carries `data-evidence` with the source files that back it, and
`tests/test_69_guide_site.py` fails if a listed path does not exist.

| Category | Documented, source-verified facts |
|---|---|
| Getting started | Required env vars (`backend/config.py`); single textual command `Menu` by exact equality on raw text, legacy dot commands removed, owner gate; the nine real mother-panel buttons from `_build_menu_buttons`; `/health` |
| Fonts | `Menu → Settings → Font`; **27** registry keys and their exact rendered samples generated from `backend/helper/font_style.py`; panel pages of 8; styling applied through `panel_render._style`/`render_edit` to titles, bodies and button labels; callback data/codes/URLs never styled; Latin + standalone digits only |
| Media & files | Deep Save pipeline (download → validate → upload as a NEW Saved Messages message → metadata → DB record, after upload); the three Save sources; `max_deep_save_mb` 50 (1–500); 10 tags / 40 chars; 120-char display name and file name; `S####` save codes; Retrieve/Delete surfaces |
| AI | Trigger + reply-aware + reply-to-AI activation, default trigger `Nova`; native tool calls validated and executed through the shared executor; the real AI sub-panels; the provider module list; `you` as a web-search capability gated by `YDC_API_KEY` |
| Workflows | Shared minute-boundary profile scheduler and both default templates (`{time}` / `{mood}`), one engine stopping never affects the other; `BIO_UPDATE_ENABLED` / `USERNAME_UPDATE_ENABLED`; durable tasks (deterministic occurrences, ≤3 attempts, prepare-ahead, ordered chains, conditions, waits, owner questions); the Todo panel |
| Troubleshooting | Helper-disabled text fallback; Supabase-less in-memory fallback and its durability consequence; DB failures never crash the bot; font limitations; 50 MB limit; supervisor-owned reconnect; `/health` |

Deliberately **not** documented: the `List`/`Find` panels (registered as
`panel:list` / `panel:find`, but no button anywhere emits those callbacks — see
§7), deployment URLs, and any capability whose only support is prose.

## 6. Validation (commands actually run)

| Check | Command | Result |
|---|---|---|
| New suite | `.venv/bin/python -m pytest tests/test_69_guide_site.py -q` | **38 passed**, exit 0 |
| Web-affected suites | `… pytest tests/test_42_dashboard_font.py tests/test_ai_menu_state_consistency.py tests/test_model_tester.py tests/test_13_model_selection.py tests/test_settings_unknown_key.py tests/test_69_guide_site.py -q` | **108 passed**, exit 0 |
| Full suite | `.venv/bin/python -m pytest tests -q` | **6091 passed, 27 skipped, EXIT 0** in 120.73 s (6053 previous + 38 new, 0 regressions) |
| Guide served through the app | `TestClient(app).get("/guide/")` and `/guide/{styles.css,search.js,guide.js}` | 200 with correct content types; `/guide/missing-page` → 404; `/health` → `{"status":"ok"}` (mount does not shadow the API) |
| Guide served by a plain static host | in-test `http.server` on a random port, `urllib` GET | 200 for `/` and `/styles.css` |
| Search behaviour, real module | Node executes the shipped `guide/search.js` against the article set parsed from `guide/index.html` | empty query → all 6 articles; `فونت` / `فونت‌هاي` (ZWNJ + Arabic yeh) → `cat-fonts`; `ذخیره` → `cat-media`; `Nova`/`NOVA` → `cat-ai`; `بیو` → `cat-workflows`; `supabase` → `cat-troubleshooting`; `۲۷` → `cat-fonts`; `zzzznope` → `[]`; normalisation and Persian-digit outputs match |
| JS syntax | `node --check guide/search.js`, `node --check guide/guide.js` | both clean |
| RTL/responsive review | structural assertions over `styles.css` (logical properties, `text-align: start`, breakpoints, `:focus-visible`, reduced motion) | passed |
| Link/navigation review | every `#…` href resolved against the document's ids; each category reachable from nav **and** card | passed |
| No secrets / no network from assets | regex scan of all four assets for credential assignments, `fetch`/`XHR`/`WebSocket`/storage/cookies | clean |
| TypeScript | `npx tsc -p tsconfig.app.json` | exit 0 (no `src/` change; run to prove the shared tsconfig is unaffected) |

## 7. Inspection findings recorded, not fixed (out of scope)

1. `README.md` documents `.menu` as the command and lists `List`/`Find` under
   the mother panel. Source says otherwise: `misc.py` matches the literal
   `Menu` by exact equality and the mother panel has nine buttons
   (Save, Delete, Profile, Database, General, Settings, Ghost Seen, Todo, AI).
   `panel:list` / `panel:find` exist as registered panels but **no callback
   anywhere emits them**, and `all_panels()`/`get_panel_parent()` have no
   callers, so those panels are currently unreachable from the UI. The guide
   documents the code, not the stale README; rewriting `README.md` was out of
   scope for this task.
2. `render.yaml` has no `buildCommand` and `dist/` is gitignored, so the
   deployed service serves the API but not the dashboard SPA. This pre-existing
   deployment fact is untouched; it is also why the guide was built to need no
   build step.

## 8. Not verified

* **Browser/visual rendering** — no headless browser is available in this
  workspace. Layout, animation, hover/focus appearance and the mobile nav were
  verified structurally (HTML contract, CSS assertions, JS parse) and through
  real HTTP responses, not visually.
* **Live deployment** — nothing was deployed. `/guide` is proven to work
  through the app's own HTTP surface in-process; no live Render URL was checked,
  and no deployment claim is made.
* Real-device RTL typography (font availability for the Persian system stack)
  was not measured on an actual device.

## 9. Deferred (later phases)

* Wiring the guide into a public production URL (e.g. a Render static site, or
  keeping the `/guide` mount) — an owner decision requiring deployment changes.
* Extending content beyond the verified set (screenshots, per-panel walkthroughs,
  English mirror) and documenting Taskloom in depth.
* Optional `README.md` correction of the `.menu` / `List` / `Find` drift.

## 10. Risks

* The `/guide` mount is additive and read-only, but it does add one route to the
  FastAPI app; regression coverage for the rest of the surface is the existing
  web test suites plus the new `/health` assertion in `test_69`.
* `guide/search.js` and `guide/guide.js` are ES modules, so opening
  `index.html` over `file://` will render the articles but not run search; the
  README states this and every article remains readable without JavaScript.
