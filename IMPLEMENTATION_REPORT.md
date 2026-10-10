# IMPLEMENTATION_REPORT.md — Alternative Premium Custom-Emoji Route: Controlled Test for the Owner-Authored Self-Chat Send

This file describes **only** the current state of the repository after this task
and **replaces every previous report in full**. The inline entity-loss
investigation it supersedes (`98c2825`, `a54877b`, `9633c0b`, `b463552`,
`219c873`, `ac55042`) stays readable in git history.

| Item | Value |
|---|---|
| Task | Investigate — and controlled-test — a supported route that can deliver a genuine Telegram Premium custom emoji to the owner's Saved Messages **other than** the failing helper-bot inline-result path |
| Project / branch | LifeOS Telegram self-bot (`Onlyicing1/Telegram-self-bot`), `main` |
| Base revision | `ac55042` — fetched and verified `= origin/main = git ls-remote` at task start |
| Telethon | **`telethon==1.34.0`**, layer 173 (`backend/requirements.txt:1`) |
| Route selected | **owner-authored `messages.sendMessage` with `peer = inputPeerSelf` and `entities = [MessageEntityCustomEmoji]`** — no bot in the path, therefore no bot entitlement consulted |
| Production code changed | **None.** The route exists only as an isolated, opt-in experiment in the test suite |
| Live Telegram verification | **Not performed** — this workspace holds no `API_ID`/`API_HASH`/`SESSION_STRING` (`freebuff-env list`). The exact opt-in command and the blocker are recorded in §6 |
| Supabase / SQL | untouched; `DATABASE_ARCHITECTURE.md` unchanged |

---

## 1. Route investigated

`messages.sendMessage` from the existing authenticated owner account, with
`peer = inputPeerSelf` (the user's own chat = Saved Messages) and
`entities = [messageEntityCustomEmoji(offset, length, document_id)]`, read back
by exact message id.

Selected because it is the only route in which **the owner authors the entity
and no bot participates**: the documented bot-side entitlement (Fragment
username; Bot API 9.4 owner-Premium for messages a bot sends directly) is never
consulted, and the destination is the one Telegram documents as allowing custom
emoji for every account ("Everyone can also use all custom emoji for free in
their Saved Messages chat…"). Rejected alternatives — the helper bot sending
directly (out of the approved behaviour, ROADMAP §28/§34-D), the inline route
(the failing one), and forwarding the source message (delivers the original
message with forward metadata, and the repository forbids `forward_messages` in
its Save pipeline) — are recorded with reasons in `INVESTIGATION.md` §2.

---

## 2. Evidence

| Kind | What was verified | Where |
|---|---|---|
| Official documentation | entity construction for a user-authored message ("create and attach messageEntityCustomEmoji entities to a message"); the single documented *ignore* rule (the entity must wrap exactly the document's `alt`); the `free` (non-Premium-usable) document flag; the Saved-Messages allowance for every account; `inputPeerSelf` = "Defines the current user"; `via_bot_id` is an **optional** field present only for bot-sent messages | `core.telegram.org/api/custom-emoji`, `telegram.org/blog/custom-emoji`, `core.telegram.org/constructor/inputPeerSelf`, `core.telegram.org/constructor/message` — all read in this task and quoted in `INVESTIGATION.md` §3 |
| Pinned Telethon (1.34.0) | `send_message(…, formatting_entities=…)` accepts `MessageEntityCustomEmoji` and forwards it as `entities=` into the real `SendMessageRequest` (signature + installed source); that request round-trips the entity through its own serialized bytes; a fresh `random_id` per request prevents two identical sends collapsing into one | offline tests + source inspection, `INVESTIGATION.md` §4 |
| Actual Telegram results | **None yet.** No live session exists in this workspace, so the decisive observation — whether the stored message keeps the entity — was **not** made | §6 |

---

## 3. Implementation

| File | Change | Why |
|---|---|---|
| `tests/test_premium_emoji_self_send_route.py` | **New, isolated experiment.** The route's payload builder (reusing the production geometry/validator), the real `SendMessageRequest` builder, the stored-message verifier (every acceptance criterion, fail-closed), the one-send/no-retry procedure, the live harness (identity → bounded source discovery → glyph resolution → one send → exact-id read-back), 41 offline tests, and ONE opt-in live test | The task asked for the smallest reversible test of the alternative route, isolated from production and following the existing test conventions. Nothing here is wired into the bot |
| `tests/conftest.py` | Registers the `live_telegram` marker (mirroring the existing `live_supabase` registration) | The live test is opt-in and marked, exactly like the repository's other live integration test |

**Unchanged on purpose:** every production module — the inline route, the
fail-closed gate, entity validation, UTF-16 geometry, the Saved-Messages-only
rule, `via_bot_id` verification, the helper bridge, the probe service. No new
client, listener, update loop, scheduler, executor, dependency or persistent
infrastructure; no Unicode fallback; no retries; no Supabase/SQL; no
`DATABASE_ARCHITECTURE.md` / `ROADMAP.md` / `tests/test_stage13.py` change.

---

## 4. Tests

| Check | Command | Result |
|---|---|---|
| Compile | `.venv/bin/python -m py_compile tests/test_premium_emoji_self_send_route.py tests/conftest.py` | **PY_COMPILE_OK**, exit 0 |
| Focused experiment (**mocked/offline**) | `.venv/bin/python -m pytest tests/test_premium_emoji_self_send_route.py -q` | **41 passed, 1 skipped**, exit 0 (the skipped one is the live test) |
| Live marker selection | `.venv/bin/python -m pytest tests/test_premium_emoji_self_send_route.py -m live_telegram -v -rs` | 1 selected, **SKIPPED** with the honest reason "the live Telegram self-send test is opt-in: set `LIFEOS_LIVE_PREMIUM_EMOJI_SELF_SEND=1`…", exit 0 |
| Relevant regressions | `pytest tests/test_premium_emoji_self_send_route.py tests/test_premium_emoji_inline.py tests/test_premium_emoji_probe.py tests/test_emoji_ui.py tests/test_reaction_phase6.py tests/test_bridge_delivery.py -q` | **302 passed, 1 skipped**, exit 0 |
| Full suite | `timeout 560 .venv/bin/python -m pytest tests/ -q` | **5838 passed, 27 skipped, 3 warnings in 124.32s**, exit 0 (baseline 5797 passed / 26 skipped → +41 new offline tests, +1 skipped live test, **0 regressions**; re-measured on the final revision, identical counts) |
| Whitespace / diff sanity | `git diff --check` | exit 0; complete diff reviewed (one new test module + 6 lines in `tests/conftest.py`) |

### Mocked vs live — kept strictly separate

**Mocked/offline (green above, all in this task):** payload geometry in UTF-16
units (including non-BMP, variation-selector and ZWJ glyphs), fail-closed
payload validation, the real request round-trip, the Telethon forwarding pin,
the duplicate-protection pin, every verification criterion (entity present,
document id, span, text, destination, attribution anomaly, missing message), the
procedure's guarantees (exactly one send; no retry after a failed send or failed
read-back; nothing sent when the source is unusable; no glyph-only fallback),
the live harness's own sequence against a recording fake client (discovery,
glyph preference `document_alt` → `source_span`, foreign session refused), and
the isolation pins (no client/listener/loop/surface added; the production
service does not reference this experiment).

**Live (NOT executed):** `test_live_self_send_preserves_entity_in_saved_messages`
— opt-in, skipped without `LIFEOS_LIVE_PREMIUM_EMOJI_SELF_SEND=1` **and**
credentials. **No live Telegram behaviour is claimed by any test in this task**;
a mocked test cannot establish server-side behaviour, and none of these pretend
to.

---

## 5. Acceptance criteria

| Criterion | Independently verified? |
|---|---|
| Target is the owner's Saved Messages | **Offline:** by construction (`inputPeerSelf` only — the route cannot address another chat) and by the stored-message check `chat_id == owner_id`, incl. a test where a foreign chat fails closed. **Live: NO** |
| Stored message contains a genuine `MessageEntityCustomEmoji` | **Offline only** (no entity → fail closed; the visible glyph is never accepted). **Live: NO** |
| Stored document id matches the source emoji | **Offline only.** **Live: NO** |
| Offsets/lengths correct in UTF-16 units | **Offline only** (unit-counted, not character-counted). **Live: NO** |
| Visible text and entity span consistent | **Offline only.** **Live: NO** |
| Inspected after Telegram accepted and stored it | Implemented as an exact-id read-back after the send; **never executed against Telegram** |
| Required `via_bot_id` attribution verified independently | **Structurally unavailable on this route** (`via_bot_id` is an optional bot-only field). The experiment asserts its **absence** and fails closed on an anomaly; the inline route's attribution criterion is therefore **not** satisfied by this route — stated, not relaxed |

**No acceptance criterion is claimed as live-verified.** The decisive artifact —
the actual stored Telegram message — has not been observed.

---

## 6. Production status and the live-run blocker

* **Nothing was deployed or exposed.** No production file changed, so there is
  no deployed behaviour to describe beyond "the inline route is exactly as it
  was: blocked at `INLINE_RESULT_ENTITY_MISSING`".
* **The route is NOT verified live.** Blocker: this workspace has no Telegram
  credentials (`freebuff-env list` → no `API_ID`/`API_HASH`/`SESSION_STRING`).
  Per the task's rule, the blocker is reported instead of improvised around.
* **The one command that resolves it** (run where the session lives):

```
LIFEOS_LIVE_PREMIUM_EMOJI_SELF_SEND=1 \
  API_ID=… API_HASH=… SESSION_STRING=… BOT_OWNER_ID=… \
  pytest tests/test_premium_emoji_self_send_route.py -m live_telegram -v -s
```

  Side effects: exactly ONE message is sent to Saved Messages (prefix
  `LifeOS premium emoji self-send test: `) and read back by exact id; nothing is
  sent anywhere else; no retries; the message is left in place as the evidence.
* If no source custom-emoji message exists in Saved Messages, the test skips
  honestly and says how to create one (reply there with a Premium emoji) or how
  to pin a document id via `LIFEOS_LIVE_PREMIUM_EMOJI_DOC_ID`.

---

## 7. Remaining uncertainty and the smallest next experiment

1. **Does Telegram keep the entity on a self-authored Saved Messages message?**
   Unknown; the implemented opt-in test is the smallest experiment that answers
   it. `INVESTIGATION.md` §8 gives the interpretation table for every possible
   outcome (kept → route works; stripped → the only documented lever left is the
   account-level entitlement, an owner decision; mismatch → a reportable
   platform difference).
2. **The inline route's blocker is unchanged** — Telegram's undocumented drop of
   a bot-supplied inline entity. This task deliberately did not re-attempt it.
3. **Attribution**: a self-authored artifact can never carry `via_bot_id`; any
   future product use must state that limitation.
4. **Nothing else is pending in this task**: no production change to deploy, no
   migration, no schema, no new dependency.

---

## 8. Git status

| Step | Value |
|---|---|
| Files in this task's commit | `tests/test_premium_emoji_self_send_route.py` (new), `tests/conftest.py` (marker registration), `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md` |
| Commit | one commit for the task (`type: description` style, matching the repository) |
| Push | `git push origin main`, fast-forward only — no rebase, no force-push, no history rewrite |
| Remote verification | after the push: `git fetch origin main` → `git rev-parse HEAD` = `git rev-parse origin/main` = `git ls-remote origin refs/heads/main`; `git merge-base --is-ancestor <commit> origin/main` exit 0; `git status --short` clean |
| Working tree | clean apart from nothing — no pre-existing unrelated changes were present or discarded |

A commit cannot contain its own hash: the commit SHA and the verified
`origin/main` SHA are reported in this task's delivery response, re-checked with
the commands above after the push.
