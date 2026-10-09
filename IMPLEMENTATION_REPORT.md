# IMPLEMENTATION_REPORT.md — Current Execution Report

> **This file describes ONLY the current state after the latest task.** It is not
> a changelog and no older report is kept below it.
>
> **This task changed no SQL and contacted no database.** Supabase stays
> manual-only, the Emoji & Reaction schema is untouched, and no migration was
> added or edited.

---

## 1. Task / stage covered

**Premium Custom Emoji render boundary — diagnostic trace + exact-message
read-back (2026-10-09).**

The owner reports that the POC's helper-bot message displays only the ordinary
Unicode glyph. The decisive question — *where does the custom-emoji entity stop
working?* — could not be answered before because the POC never looked at what
Telegram actually stored. This task makes that observable **from the POC
itself**, without redesigning the Emoji Library, the reaction system, the
bridge or the runtime:

* a nine-stage structured trace of the whole send pipeline;
* an exact-message read-back through the **helper bot's own session**;
* one honest diagnostic outcome per run (seven defined outcomes);
* the document-`alt` comparison that separates "our span was wrong" from
  "the server dropped it anyway".

**Result: the instrumentation is IMPLEMENTED AND PROVEN OFFLINE.** No live
Telegram request was made (no credentials in this workspace), so the *live*
verdict is produced by the owner's next run — the POC now reports it
explicitly. **No code defect was demonstrated, so none was "fixed"**; the send
algorithm is unchanged (§10).

## 2. What the POC now measures, stage by stage

`backend/services/premium_emoji_probe_service.py` logs one structured line per
stage (`[PREMIUM_PROBE] <STAGE> key=value …`); the conversion stage is logged by
the bridge (`[BRIDGE] BRIDGE_ENTITY_CONVERTED …`). Nothing logged is a session
string, token, access hash, whole message or unrelated history.

| Stage | What it records |
|---|---|
| `SOURCE_ENTITY_FOUND` | whether the owner's reply carried a real `MessageEntityCustomEmoji` (and whether its id is usable) |
| `SOURCE_ENTITY_VALIDATED` | that entity's `document_id`, `offset`, `length` and a bounded span |
| `OUTBOUND_ENTITY_BUILT` | the exact outbound entity (type, id, offset, length), the text length, and `used_placeholder` |
| `BRIDGE_ENTITY_CONVERTED` | that the dict→TL conversion produced `MessageEntityCustomEmoji` objects, with `offset:length:document_id` |
| `SEND_STARTED` | `path=helper_bot_bridge`, `via=helper_bot_client`, the geometry and entity count |
| `SEND_ACCEPTED` | whether Telegram accepted the send, the returned message id, or the refusal error |
| `READBACK_STARTED` | that the read-back targets THE EXACT sent id through the helper bot's client |
| `READBACK_RESULT` | what Telegram stored: entity present?, same `document_id`?, same offset/length/span?, stored text length |
| `DIAGNOSIS` | the one outcome the evidence establishes (§5) |

`DOCUMENT_ALT` is logged additionally when the read-back did not retain a
matching entity.

## 3. The read-back (new)

After a successful send, `deliver_proof` reads the **exact** message id back
through the helper bot's own client:

* `bot.get_messages(peer, ids=<send result id>)` — no recent-messages scan, no
  "latest message" inference, no unrelated history; `peer` is the same
  bot-session peer the send used;
* bounded by the project's existing watchdog convention
  (`guarded_await`, `telegram:premium_probe:readback`, 30 s);
* a read-back failure (error, timeout, or no message returned) is reported
  **separately** from a send failure — `ok` stays the send fact and the
  diagnosis becomes `READBACK_FAILED`;
* the returned object's **raw** text (`.message`) and real `entities` are
  inspected with the same entity-only scan used on the source;
* when the entity was not retained and matching, the expected document is
  resolved through the **existing** typed wrapper
  (`telegram_api.custom_emoji.get_custom_emoji_documents`) and its `alt` is
  compared with the span that was sent;
* `readback` (a stable dict with every key present) and `readback_summary()`
  carry the evidence; the Glass panel prints the summary and the diagnosis.

## 4. Outbound validation (new)

`validate_proof_payload(payload)` checks the payload against itself before
anything is sent: entity type `MessageEntityCustomEmoji`, a real document id,
usable UTF-16 `offset`/`length`, the span covering exactly the fallback text,
and exactly one entity. A payload that fails is **never sent** and is
classified `OUTBOUND_ENTITY_INVALID`. In every tested input the builder
produces a valid payload (real span text and the `▪` placeholder branch alike).

## 5. Diagnostic outcomes

| Outcome | Meaning | What it does not claim |
|---|---|---|
| `SOURCE_ENTITY_MISSING` | the reply had no real custom-emoji entity (or an unusable id); nothing was sent | anything about Telegram |
| `OUTBOUND_ENTITY_INVALID` | the payload failed its own validation; nothing was sent | rendering |
| `SEND_FAILED` | the helper bot was unavailable, has no chat with the owner, or Telegram refused the send (its error text is in `detail`) | whether the entity would have been retained |
| `READBACK_FAILED` | the exact-message fetch errored, timed out, or returned nothing | whether the entity was retained |
| `ENTITY_STRIPPED_OR_MISSING` | Telegram stored the message **without** the custom-emoji entity; `document_alt` / `document_alt_match` say whether the sent span matched the document's alt | *why* it was dropped, beyond that comparison |
| `ENTITY_MISMATCH` | an entity was stored, but with a different id or an invalid/mismatched span | rendering |
| `ENTITY_RETAINED_RENDER_UNVERIFIED` | the entity was retained with the **expected** id and span | **that a Premium emoji was displayed** — read-back can prove retention, never display |

`ENTITY_RETAINED_RENDER_UNVERIFIED` is deliberately not labelled a successful
visual render; the panel keeps the explicit caveat ("only if the helper bot may
use custom-emoji entities (Fragment-purchased username). A plain glyph there is
a failure, not a success.").

## 6. Exact files changed

| File | Change |
|---|---|
| `backend/services/premium_emoji_probe_service.py` | trace helper + stage calls; `_scan_custom_emoji`; `validate_proof_payload`; `_fetch_sent_message`; `_resolve_document_alt`; `_read_back`; `classify_diagnosis`; `readback_summary`; `_failed`/`_empty_readback` result shape |
| `backend/bot/handlers/emoji.py` | POC panel prints the read-back summary and `Diagnosis <OUTCOME>` (and `SOURCE_ENTITY_MISSING` on a non-entity reply); the UI still holds no send logic |
| `backend/telegram_api/bridge.py` | import + ONE conditional INFO line when a custom-emoji entity was reconstructed; default behavior, return value and exceptions for every caller are unchanged |
| `tests/test_premium_emoji_probe.py` | +19 tests (read-back retained/stripped/mismatch, alt comparison, read-back failure/timeout/no-message/no-id, exact-id targeting, classification table, outbound validation, panel texts, full-trace stage presence) |
| `tests/test_bridge_delivery.py` | +1 test pinning the trace's custom-emoji-only blast radius |
| `INVESTIGATION.md` | rewritten as the canonical instrumented investigation record |
| `IMPLEMENTATION_REPORT.md` | this current-state report |

Explicitly **not** changed: the Emoji Library import/scanning pipeline, sticker
set scanning, library pagination, categories/mappings, the Phase 3–6
replacement/state/composition/reaction code (`emoji_replacement_service.py`,
`emoji_state_service.py`, `emoji_transformer.py`, `reaction_service.py`),
`backend/db/client.py`, `DATABASE_ARCHITECTURE.md`, `ROADMAP.md`, `AGENTS.md`,
every migration, the `RuntimeSupervisor`, the dispatcher, the provider manager,
the `ToolExecutor`, the AI architecture, media processing and the helper-bot
client/lifecycle modules.

## 7. Tests executed

| Check | Command | Result |
|---|---|---|
| Compile | `python -m py_compile` on the four changed Python files | clean |
| Focused POC suite | `python -m pytest tests/test_premium_emoji_probe.py -q` | **62 passed**, exit 0 (was 43) |
| Bridge suite | `python -m pytest tests/test_bridge_delivery.py -q` | **23 passed**, exit 0 (was 22) |
| Emoji/Reaction regression | `python -m pytest tests/test_emoji_ui.py tests/test_emoji_ui_phase2.py tests/test_emoji_state_phase3.py tests/test_emoji_replacement_phase4.py tests/test_emoji_composition_phase5.py tests/test_reaction_phase6.py tests/test_bridge_delivery.py tests/test_emoji_library_import.py tests/test_emoji_category_service.py tests/test_emoji_set_enumeration.py tests/test_premium_emoji_probe.py -q` | **555 passed**, exit 0 |
| Full suite | `python -m pytest tests -q --ignore=telegram-self-bot` | **5715 passed, 26 skipped**, exit 0 (119.73 s) |
| Whitespace | `git diff --check` | exit 0 |

The full-suite arithmetic is exact: pre-task baseline 5695 passed + 19 new POC
tests + 1 new bridge test = 5715. `--ignore=telegram-self-bot` excludes the
pre-existing untracked mirror clone in the workspace (never touched); its
`tests/conftest.py` otherwise interferes with collection.

Each required test case is pinned by name: retained read-back
(`test_a_retained_entity_is_never_reported_as_a_rendered_emoji`,
`test_the_readback_targets_the_exact_sent_message_and_never_scans`); read-back
with no entity and the alt comparison
(`test_a_stripped_entity_is_reported_with_the_document_alt_comparison`,
`test_a_stripped_entity_with_a_non_matching_alt_is_still_stripped`,
`test_a_failed_alt_lookup_never_changes_the_stripped_diagnosis`); mismatched
id/invalid span (`test_a_different_document_id_in_the_stored_entity_is_a_mismatch`,
`test_an_invalid_span_in_the_stored_entity_is_a_mismatch`); send failure
(`test_a_rejected_send_is_reported_without_any_glyph_substitution`,
`test_the_failure_panel_carries_the_send_failed_diagnosis`); read-back failure
and timeout (`test_a_readback_failure_is_reported_separately_from_the_send`,
`test_a_readback_timeout_is_a_readback_failure`,
`test_a_readback_that_returns_no_message_is_a_readback_failure`); exact-id
targeting with no scan; classification
(`test_classify_diagnosis_covers_every_readback_state`); and the trace
(`test_the_trace_reports_every_stage_and_no_secret`). Unrelated bridge callers
are pinned unchanged by
`test_bridge_conversion_trace_covers_custom_emoji_only`.

No check was skipped, weakened or suppressed; nothing is reported as passing
that was not run.

## 8. Live Telegram verification — NOT performed

**Live rendering was not verified and is not claimed.** This workspace has no
`SESSION_STRING`, no `BOT_TOKEN` and no Telegram account, so no message was
sent and the read-back never executed against Telegram. All tests fake the
Telegram surface at the exact calls the probe consumes and prove the pipeline,
not the server's rendering.

What the owner's next run will produce, without any further code work:

1. the same POC flow (`Menu` → Emoji → `💬 Set Reaction Emoji` → reply to the
   selection message with a real Premium emoji from Telegram's picker);
2. a panel line with the read-back summary and `Diagnosis <OUTCOME>`;
3. the nine trace stages in the deployment logs;
4. the decision: `ENTITY_RETAINED_RENDER_UNVERIFIED` with a plain glyph still
   shown ⇒ sender eligibility / rendering limitation (owner account decision —
   Telegram Premium for the bot owner per Bot API 9.4, or a Fragment-purchased
   username for the bot); `ENTITY_STRIPPED_OR_MISSING` with
   `document_alt_match=false` ⇒ the sent span did not match the document's
   `alt` (a constructive follow-up, proven before it is touched);
   `SEND_FAILED` ⇒ Telegram's own error text names the restriction.

## 9. Known limitations (honest)

1. **Live rendering remains unverified** (§8) — only Telegram can prove it.
2. **Account eligibility is unresolved.** The owner has no Telegram Premium;
   the bot's Fragment status is unknown. The Bot API 9.4 owner-Premium rule and
   the earlier Fragment rule are documented, but which governs this exact
   MTProto bot send is not verified. **No entitlement probing was added** (it
   would need new credentials/privileged access).
3. **The read-back proves retention, not display.** A client-side render
   decision (H4) cannot be separated from a server-side entitlement gate (H1)
   by any API read; that final step is the owner looking at the message.
4. **No code defect was demonstrated**, so no corrective code change was made;
   only instrumentation. If `OUTBOUND_ENTITY_INVALID` ever fires in the live
   run, the payload validation has found the defect and it will fail closed.
5. **The helper bot still needs a private chat with the owner** (press Start);
   otherwise the run reports `E_NO_BOT_CHAT` / `SEND_FAILED` honestly.
6. The existing 120 s pending-input state expiry and the 90 s handler backstop
   are unchanged.

## 10. Success / failure statement against the request

| Requirement | State |
|---|---|
| Trace distinguishes all nine stages | ✅ `SOURCE_ENTITY_FOUND`, `SOURCE_ENTITY_VALIDATED`, `OUTBOUND_ENTITY_BUILT`, `BRIDGE_ENTITY_CONVERTED`, `SEND_STARTED`, `SEND_ACCEPTED`, `READBACK_STARTED`, `READBACK_RESULT`, `DIAGNOSIS` |
| Structured fields, no secrets/dumps | ✅ ids/offsets/lengths/bounded spans/reason strings only; pinned by test |
| Read-back of the exact sent message via the helper bot's client | ✅ `get_messages(peer, ids=<id>)` under `guarded_await`; no scan; pinned by test |
| Entity type/id/offset/length/span comparison | ✅ recorded in `readback` and logged in `READBACK_RESULT` |
| Read-back failure handled separately from send failure | ✅ `READBACK_FAILED` with `ok` (send) unchanged |
| Diagnostic outcomes | ✅ all seven, one per run, classified by `classify_diagnosis` |
| Payload validation before send | ✅ `validate_proof_payload`, fail-closed |
| Document-`alt` comparison when obtainable | ✅ via the existing typed wrapper; failure is recorded, never fatal |
| Minimal bridge change, other callers unchanged | ✅ one conditional log line; pinned by test |
| No speculative changes (no second client/loop, no API switch, no fallback, no guessed ids, no Unicode-as-Premium) | ✅ architecture pins still pass |
| Focused offline tests | ✅ 19 new POC tests + 1 bridge test; no credentials required |
| `py_compile`, focused tests, bridge tests, full suite, `git diff --check` | ✅ all passed (§7) |
| No fabricated live result | ✅ §8 states plainly that no live run was performed |

## 11. Delivery metadata

| Item | Value |
|---|---|
| Repository | `Onlyicing1/Telegram-self-bot` (connected workspace remote; never hardcoded into code) |
| Branch | `main` |
| Baseline before this pass | `06eeb8d` (`docs: record premium custom emoji rendering investigation`) — the workspace was fast-forwarded to `origin/main` first |
| This pass's commit | the scoped `diag(emoji): …` commit (service + handler + bridge + tests + both documents) |
| Push | `git push origin main` — fast-forward only; no rebase, no force-push, no history rewrite |
| Working tree | clean except the pre-existing untracked `telegram-self-bot/` mirror (never touched) |

## 12. Push verification

Verified against the live remote after the push with the four commands the task
names (`git fetch origin`, `git rev-parse HEAD`, `git rev-parse origin/main`,
`git ls-remote origin refs/heads/main`) plus `git status --short`; the result is
recorded in the final report of this pass.
