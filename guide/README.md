# LifeOS Guide Website

Persian-first (RTL) documentation site for LifeOS. It is a **standalone static
site**: no build step, no bundler, no package, no framework, and no access to
the Telegram session, helper bot, Supabase or any other server secret. It can
be read with JavaScript disabled.

```
guide/
├── index.html   # all copy: hero, category grid, six articles, FAQ, footer
├── styles.css   # dark neon-purple/cyan theme, responsive, RTL-first
├── search.js    # pure search helpers (no DOM) — unit-tested in Node
├── guide.js     # DOM wiring: search, filtered view, mobile nav, shortcuts
└── README.md
```

## Viewing it

Any static file server works. From the repository root:

```bash
python -m http.server 8080        # then open http://localhost:8080/guide/
```

When the running application serves the dashboard it also serves this site at
`/guide/` (read-only `StaticFiles` mount in `backend/web/app.py`). The Vite dev
server serves it at `/guide/` too, because it serves the project root.

ES modules require HTTP; opening `index.html` straight from disk (`file://`)
will render the articles but not run the search script.

## Content policy

Every article carries `data-evidence` — the source files that back its claims —
and `tests/test_69_guide_site.py` fails if one of those paths does not exist.
Nothing is documented that the code does not do; features that are implemented
but not exercised against live Telegram are labelled as such in the text.

Panel names are quoted exactly as they appear in the UI (`Menu → Settings →
Font`), while the prose is Persian. Identifiers, commands and code samples stay
Latin and are wrapped in `<code dir="ltr">`.

## Changing it

* **New article** — add an `<article class="guide-article" id="cat-…">` with
  `data-title`, `data-category`, `data-keywords` (the search terms Persian
  users are likely to type) and `data-evidence`; then add a card to the
  category grid and a link to the header nav. The search index is built from
  those attributes, so no JavaScript change is needed.
* **Search behaviour** — edit `search.js` only. Persian normalisation
  (Arabic yeh/kaf, alef variants, diacritics, ZWNJ, Persian digits) lives there.
* **Theme** — the palette is the CSS custom-property block at the top of
  `styles.css`.

## Tests

```bash
.venv/bin/python -m pytest tests/test_69_guide_site.py -q
```

The suite checks the document structure (language, direction, landmarks, unique
ids, headings), that every in-page link resolves, that no external resource or
credential leaks in, that the served pages are reachable over HTTP, and it runs
the shipped `search.js` in Node against the real article set.
