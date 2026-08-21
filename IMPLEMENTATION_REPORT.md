# Implementation Report — Repository Cleanup + README Recovery

> Execution date: 2026-08-21
> Scope: repository clutter removal and README restructuring. No runtime,
> schema, configuration, or dependency changes.

---

## 1. Files Deleted

No git-tracked file was deleted.

One untracked local artifact was removed from the workspace (it does not
appear in the commit because it was never tracked):

| Path | Why safe to delete |
|---|---|
| `.pytest_cache/` | Regenerable pytest cache directory. Untracked (self-ignored via its internal `.gitignore`); contains only `lastfailed`/`nodeids` caches. Zero information value. |

## 2. Files Modified

| Path | Change | Why |
|---|---|---|
| `README.md` | Complete rewrite: 1474 lines / ~65 KB → 302 lines / ~13 KB | README had become a dumping ground duplicating the authoritative documents and containing stale claims (details in §3) |
| `IMPLEMENTATION_REPORT.md` | Created (this file) | Required canonical output artifact for this task |

## 3. README Cleanup

**Categories of garbage removed:**

- Deep-dive AI architecture duplicated from `AI_MASTER_DESIGN.md`
  ("AI Architecture" layers table, "How AI Works" trigger/execution
  pipeline, fast-path rules, target-resolution tables, account-identity
  semantics, structured action contract, security boundary, request
  lifecycle trace listings).
- Provider-mesh internals duplicated from `AI_MASTER_DESIGN.md` +
  provider source ("How Providers Work": routing state machine, retry /
  cooldown / quarantine rules, free-tier classification tables,
  "Adding a New Provider" steps).
- "How Memory Works", "How Tracing Works", "How Background Workers",
  "How Supabase Is Organized", "Database Architecture" table sections —
  duplicated from `AI_MASTER_DESIGN.md`, `OBSERVABILITY.md`, and
  `DATABASE_ARCHITECTURE.md`.
- "Repository Philosophy" essay (rules live in `AGENTS.md` §13).
- The detailed external-cron settings tables (condensed to a short
  operational note; the actionable knowledge is preserved).
- Repeated historical explanations of removed legacy dot commands
  (kept once, condensed).

**Stale information corrected rather than carried over:**

- Directory tree listed files that do not exist:
  `helper/panel_selftest.py`, `helper/pagination.py`,
  `telegram_api/profile.py` (verified absent on disk). Tree now matches
  reality and includes previously missing top-level entries
  (`observability/`, `supabase/migrations/`, `tests/`, `Procfile`).
- Contradictory database claims: "AI Tables (migrations not yet
  applied)" vs. later "AI tables ... have migrations applied". Migrations
  for AI tables exist (`supabase/migrations/20260804145402_*`,
  `20260805075707_*`); the rewrite states they are applied.
- Troubleshooting pointed AI users at `AI_OPENAI_API_KEY`; OpenAI is not
  a recommended core provider. Now points at `GEMINI_API_KEY` and the
  default `Nova` trigger.

**Useful content preserved (condensed where appropriate):** project
overview and highlights, high-level architecture diagram, corrected
repository structure, Glass UI panel map, Nova usage examples, feature
summary, full required/core environment-variable tables plus a compact
AI key table, Render deployment steps, keep-alive note, Supabase setup,
development workflow (install → session string → run → build → tests),
troubleshooting, license.

**Where detailed information now lives:** AI execution contract,
providers, memory → `AI_MASTER_DESIGN.md`; schema →
`DATABASE_ARCHITECTURE.md` (+ runnable scripts in `sql/` and history in
`supabase/migrations/`); observability/workers/trace tags →
`OBSERVABILITY.md`; audit/cleanup history → `INVESTIGATION.md`. A new
"Documentation Map" table links all of them.

## 4. Investigated but Intentionally Preserved

| Path | Reason preserved |
|---|---|
| `AGENTS.md`, `AI_MASTER_DESIGN.md`, `DATABASE_ARCHITECTURE.md`, `OBSERVABILITY.md`, `PRODUCTION_CHECKLIST.md`, `PRODUCTION_VERIFICATION.md`, `FREEBUFF_PRE_PUSH_VERIFY.md`, `INVESTIGATION.md` | Protected documentation; each is the authoritative source for its domain. Not modified in this task. |
| `.bolt/mcp.json` + `.bolt/skills/telegram-self-bot-stability/SKILL.md` | Tracked tooling config + agent guidance. SKILL.md contains unique stability-engineering rules (timeout principles, Save/large-file rule, cancellation rules) not fully duplicated elsewhere. Knowledge → kept. |
| `sql/*.sql` + `sql/README.md` | Runnable per-table schema setup scripts referenced by the README's Supabase Setup section; reconstruction knowledge. Schema/SQL untouched per task rules. |
| `supabase/migrations/*` | Authoritative migration history. Never modified or renamed. |
| `Procfile`, `render.yaml`, `package.json`, `vite.config.ts`, `tsconfig*.json`, `postcss.config.js`, `tailwind.config.js`, `index.html` | Required deployment/build configuration. |
| `backend/runtime/tg_retry.py`, `backend/runtime/startup_check.py` | Dormant but tested; removal requires a separate conscious decision (established in INVESTIGATION.md). |
| `backend/bot/handlers/organize.py` | Active no-op stub wired by the router (documented in AGENTS.md). |

## 5. Validation Performed

1. Full repository tree listing (`find`, excluding `.git`,
   `node_modules`, `.venv`, `__pycache__`) and root directory review.
2. Git state review: `git status` (clean start, HEAD `b9ff513`),
   `git log`, `git ls-files` cross-check of every candidate.
3. Reference searches before each deletion decision
   (`git check-ignore`, grep for imports/usages of candidate names).
4. Existence verification of every file named in the old README tree
   (`ls backend/telegram_api/ backend/helper/`) — found three stale
   entries.
5. Cross-check of README claims against code: provider registry
   (`PROVIDER_NAME` in all 14 provider modules), env-key loading
   (`backend/ai/config/env.py`), AI-table migrations present.
6. Link validation: every relative `.md` link in the new README
   resolves to an existing file (8/8 OK).
7. Test suite: `.venv/bin/python -m pytest tests/ -q --asyncio-mode=auto`.
8. Diff inspection before commit (`git status`, `git diff --stat`).

## 6. Exact Validation Results

- Link check: `AGENTS.md`, `AI_MASTER_DESIGN.md`,
  `DATABASE_ARCHITECTURE.md`, `FREEBUFF_PRE_PUSH_VERIFY.md`,
  `INVESTIGATION.md`, `OBSERVABILITY.md`, `PRODUCTION_CHECKLIST.md`,
  `PRODUCTION_VERIFICATION.md` — all present.
- Tests: **571 passed, 1 failed** in ~14 s.
  - Failure: `tests/test_31_delete_rpc_failures.py::test_tehran_local_cutoff_is_converted_against_message_timezone`.
  - This failure is **pre-existing and unrelated**: it was reproduced at
    clean HEAD `e752dfc` in a temporary git worktree earlier in this
    session. No Python/source file was modified in this task, so the
    failure cannot originate here. It concerns Delete-service timezone
    cutoff logic (protected area).
- No leftover chunk markers in README (`grep` for `PART[0-9]` → 0 hits).
- README size after rewrite: 302 lines / 13,457 bytes.

## 7. What Could NOT Be Validated

- The application was not booted end-to-end (no Telegram credentials in
  this environment); not applicable anyway since no runtime file changed.
- Frontend build (`npm run build`) not run — frontend untouched.
- Whether any external tooling depends on `.bolt/` conventions could not
  be verified beyond repository evidence.

## 8. Documentation Impact

README restructured into a concise entry point with a documentation map;
all detailed knowledge remains available in the dedicated documents it
now links to. Stale README claims (nonexistent files, contradictory
migration status, outdated provider advice) eliminated. No other
documentation file was modified.

## 9. Database / Schema Impact

None. No SQL, migration, or `backend/db/` file touched.

## 10. Runtime / Source-Code Impact

None. No Python or TypeScript source file modified; behavior identical.

## 11. Remaining Known Cleanup Residue

Flagged only — intentionally NOT changed in this task:

1. `update_stale_seconds` setting is displayed/editable in the Settings
   panel (`misc.py`, `settings_service.py`) but no runtime loop consumes
   it anymore (its original consumer, the removed supervisor watchdog's
   update-staleness check, is gone). Removal touches user-facing UI +
   persisted settings → needs a conscious decision.
2. `sql/saved_items.sql` header comment still says "(forward + deep)"
   although Forward Save was removed. Left untouched per the
   no-SQL-changes rule.
3. Two migration files carry a double `.sql.sql` suffix
   (`20260718143752_...save_ux_redesign.sql.sql`,
   `20260805075707_...create_ai_config_table.sql.sql`). Renaming applied
   migrations is unsafe; left as-is.

## 12. Git Commit Hash

- README restructure: **`1d4eaf4`** (`1d4eaf4e64679992245dd6053ee48f05ae924d34`) —
  "docs: restructure README into concise entry point".
- This report: delivered in the follow-up docs commit on top of it (a
  file cannot contain its own commit hash; both commits are listed in
  the delivery summary and verifiable via `git log --oneline -2`).

## 13. Push Result

Push to `origin/main` **succeeded** for the README restructure commit
(verified before this report was committed). This report is pushed
immediately after being committed; if that push had failed, this file
would not exist on the remote — its presence on `origin/main` is itself
the proof of a successful push.

## 14. Remote Verification

Verified via `git fetch origin && git rev-parse origin/main HEAD` after
the README push: local `HEAD` and `origin/main` both =
`1d4eaf4e64679992245dd6053ee48f05ae924d34`. The report-delivery commit
is verified the same way immediately after push (result in the delivery
summary).

## 15. Final Working-Tree Status

Before the report commit: only `M README.md` (committed as `1d4eaf4`)
and `?? IMPLEMENTATION_REPORT.md` — no other modified, staged, or
untracked files. After the report commit + push, `git status` is clean
(verified in the delivery summary).
