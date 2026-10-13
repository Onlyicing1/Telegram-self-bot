# Investigation — Unified, Traceable Emoji Diagnostic Workflow (2026-10-13)

Canonical, latest-only record. **This file replaces every previous
`INVESTIGATION.md` in full.** The earlier premium-emoji inline entitlement
diagnosis and the direct-send POC remain readable in git history and are
retained here only where this task depends on them.

| Item | Value |
|---|---|
| Repository / branch | `Onlyicing1/Telegram-self-bot`, `main` (tracks `origin/main`) |
| Revision at task start | `6181d239dab693bae627f52d2b0d2038dabd0ce1` — freshly fetched; `= origin/main` (merge of PR #183) |
| Telethon | **pinned `telethon==1.34.0`** (`backend/requirements.txt:1`; installed copy inspected) |
| Task | Refactor the Emoji panel so its UI and backend form ONE traceable diagnostic workflow |
| Question | What are the panel's actual buttons, which are diagnostics vs product features, and where does a run really fail? |

---

## 1. State reconciliation (source-first)

At task start the previous task's work had already been **merged**: `origin/main`
was `6181d239` ("Merge pull request #183 …"), and both local commits `78d8744`
(the entitlement-diagnosis fix) and `798ff4b` (its report) were ancestors of
`origin/main`. Local `main` was fast-forwarded to `origin/main` before any edit.

The active roadmap file is **`ROADMAP.md`** ("Emoji & Reaction Feature
Roadmap"); it records Phases 0–6 as implemented/offline-tested and marks
media/caption reconstruction (§23), owner-visible failure surfacing (§28) and
live verification as open. `AGENTS.md`, `IMPLEMENTATION_REPORT.md` and
`INVESTIGATION.md` were read in full.

## 2. The panel's real buttons (traced to their handlers)

`backend/bot/handlers/emoji.py` registers the mother-menu panel `emoji`
(`_MAIN_PANEL`). Before this task the main panel exposed **three premium-emoji
buttons**, each wired to a distinct action and a distinct service:

| Button (before) | Action id | Handler | Service | Nature |
|---|---|---|---|---|
| `✨ Send Premium Emoji` | `emoji_premium_inline` | `_premium_inline_action` | `premium_emoji_inline_service` | **production feature** (inline route) |
| `🧪 POC · helper-bot sent emoji` | `emoji_react_premium` | `_react_premium_action` | `premium_emoji_probe_service` | **diagnostic** (helper-bot bridge) |
| `🧪 Live test · direct self-chat` | `emoji_premium_direct` | `_premium_direct_action` | `premium_emoji_direct_service` | **diagnostic** (owner-authored direct send) |

Non-premium product features on the same panel: `⬇ Import from Saved Messages`
(`emoji_import`), `💬 React to a message` (`emoji_react`), and the `📚 Library` /
`🗂 Categories` / `🔁 Replacement` sub-panels. The probe and direct actions are
**diagnostic** (their own docstrings call them POC / live-test entry points that
are "not a second production route"); the inline action is the **production
route** with a fail-closed gate.

## 3. The complete data path (inline route)

```
Menu → Emoji → (premium action)
  → self client sends a deterministic selection message to Saved Messages
  → pending-input reply mode armed (input_state.set_pending, 180s)
owner replies to THAT message with a genuine custom emoji
  → handler reads the EXACT reply; validates it targets the selection message
  → inspect_source_message(reply): document_id + UTF-16 offset/length + span text
  → premium_emoji_inline_service.send_premium_emoji_via_inline(client, chat, doc, glyph, owner)
      ROUTE_STARTED → eligibility (helper bot id/username, owner premium, document facts)
      PAYLOAD_VALIDATED (build_inline_payload + validate_inline_payload)
      helper bot answers the inline query (inline_engine.query_results)
      INLINE_RESULT inspected (Telegram's STORED result — entity present?)
        ├─ entity missing → fail-closed (no send)
        └─ entity present → inline_engine.click_result (sendInlineBotResult)
             → read back the EXACT stored message by id
             → classify_diagnosis (entity + span + via_bot_id)
```

Each stage already emits a bounded, correlated `[PREMIUM_EMOJI_ROUTE_DIAG]`
log line under one `run=<id>`, and the service returns a full outcome record
(`ok`/`error`/`diagnosis`/`inline_result`/`readback`/`eligibility`). This is the
**real** evidence the new trace is built from.

## 4. Confirmed failure stage (unchanged by this task)

The confirmed root cause of the *inline route's* failure remains the
**documented Telegram bot custom-emoji entitlement restriction** established in
the previous task: the helper bot's own correlated answer provably carried the
entity, yet Telegram's stored inline result carried none (`entity_count=0`, text
intact), so the fail-closed gate refuses before any send. The earlier
`reason=no_send_message` label was a pre-fix wrapper bug (reading `send_message`
off the `custom.InlineResult` wrapper instead of `.result.send_message`). The
current code inspects the stored result successfully and classifies the drop as
`INLINE_BOT_NOT_ENTITLED` when — and only when — the bot's own submission is
proven and correlated. This task does **not** re-diagnose or change that; it
surfaces the same outcome as a real, traceable panel workflow.

## 5. Design decision — which route the unified workflow runs

The panel's primary action runs the **production inline route**
(`send_premium_emoji_via_inline`), because:

* it is the route the panel already treats as primary (`_INLINE_ACTION` was
  `datas[0]` before this task);
* it exercises the **whole** pipeline the task's stage list names — target
  resolution, entity extraction, payload validation, route/entitlement check,
  request initiation, response, stored-message inspection and read-back;
* it keeps the Saved-Messages restriction, the genuine-entity requirement, the
  `document_id`/UTF-16 validation and the `via_bot_id` read-back requirement
  intact (all unchanged in the service);
* it distinguishes local validation, submission, Telegram acceptance and
  verified entity from the service's own separate evidence fields — so the trace
  never conflates "accepted" with "verified".

The redundant standalone diagnostic buttons are removed from the primary panel
(their diagnostic role is folded into the one workflow); the **production send
action and every non-diagnostic product feature are preserved**. The probe and
direct **actions stay registered** (their services and tests are untouched) so
no existing capability is lost, but they are no longer advertised as separate
buttons on the primary panel. This is the smallest change that satisfies "one
main diagnostic action" without deleting product capabilities.

## 6. Trace fidelity — real events only

`backend/services/premium_emoji_diagnostics.py` (new) owns the trace:

* a fixed stage vocabulary (`run_started` → `run_completed`), each stage with
  `status` (pending/running/succeeded/failed/skipped/blocked/inconclusive),
  `label`, `detail`, non-sensitive `evidence`, a safe `classification` and an
  `elapsed_s` duration;
* `build_trace(outcome, …)` derives every status **from the service's real
  outcome** — never a hardcoded success. The earliest failing stage is mapped
  from the service's own `diagnosis` constant; stages before it are `succeeded`,
  the failing stage is `failed` (or `blocked` for a missing prerequisite), and
  later stages are `skipped`;
* a single-slot per-owner run record (`begin_run`/`is_running`/`end_run`) makes
  a duplicate callback a refusal, never a duplicate Telegram operation;
* `render_trace` emits English stage lines only — stage names, statuses,
  bounded explanations and non-sensitive evidence (ids, offsets, lengths,
  counts, booleans). No session strings, tokens, access hashes or message
  bodies.

## 7. What was NOT verified here

No live Telegram request of any kind was made in this workspace: there is no
`.env` and no `API_ID`/`API_HASH`/`SESSION_STRING`/`BOT_OWNER_ID`, and
`backend/config.load()` hard-fails without them. The panel is exercised through
its real handlers with the Telegram boundary faked at the exact surface the
service consumes (`inline_query` / `click` / `get_messages` / `get_me`), as the
service's own suites already do. The trace's truthfulness is therefore verified
**offline against real outcome records**, not against a live Telegram send.
