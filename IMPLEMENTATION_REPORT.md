# IMPLEMENTATION_REPORT.md — Direct Owner-Authored Premium Custom-Emoji Send: Controlled POC

This file describes **only** the current state of the repository after this task
and **replaces every previous report in full**. The inline route audit and its
three live attempts (`98c2825` → … → `2eaaf93`) and the test-suite-only
self-send experiment (`f22894e`, `9b34e38`) stay readable in git history.

| Item | Value |
|---|---|
| Task | **Controlled Direct-Owner Premium Custom Emoji Send Test** — establish whether the owner's account can send a genuine Premium custom emoji **directly** to its own Saved Messages, preserving a real `MessageEntityCustomEmoji`, and verify the stored message from the read-back |
| Project / branch | LifeOS Telegram self-bot (`Onlyicing1/Telegram-self-bot`), `premium-emoji-attribution` (tracks `origin/main`) |
| Base revision | `2eaaf93` — freshly fetched and verified `= origin/main` at task start |
| Telethon | **`telethon==1.34.0`** (`backend/requirements.txt:1`) |
| **Stage completed** | The isolated, owner-triggered **direct-send diagnostic path** in the live runtime, with the full pre-send validation chain and exact-id read-back verification; offline test coverage; these reports |
| **Next stage** | **The live run in the deployed runtime**: `Menu` → Emoji → "🧪 Live test · direct self-chat" → reply to the selection message with the custom emoji → read the verdict (panel + `[PREMIUM_DIRECT] … DIAGNOSIS` log line). This is the only step that can answer the entity-preservation question |
| Real Telegram send/read-back in this workspace | **NO — not performed.** No `API_ID`/`API_HASH`/`SESSION_STRING`/`BOT_OWNER_ID` and no `.env`; `backend/config.load()` hard-fails without them and Render Free exposes no shell. **No Telegram request of any kind was made in this workspace** |
| Does the experiment prove the direct path works? | **NO — not yet.** What is proven is that the path is implemented, bounded and fail-closed offline. The decisive fact (Telegram's stored entity) is exactly what the live run must produce; until it runs, the question is **pending**, not answered |
| Production inline route | **Untouched** — `backend/services/premium_emoji_inline_service.py` has a **zero-line diff**; no attribution check, gate or panel behaviour of the inline route was changed or weakened |
| Supabase / SQL | untouched; `DATABASE_ARCHITECTURE.md` unchanged |

---

## 1. Files changed and why

| File | Change | Why |
|---|---|---|
| `backend/services/premium_emoji_direct_service.py` | **NEW** (~538 lines) — the isolated direct route: destination/owner/document/glyph/payload/entity validation, ONE `messages.sendMessage` to Saved Messages, exact-id read-back, evidence record and diagnoses | The direct path had no runtime implementation; this is it. It reuses the production entity builder/validator and the shared read-back/document/eligibility helpers from `premium_emoji_inline_service` instead of duplicating geometry or entitlement logic |
| `backend/bot/handlers/emoji.py` | **+275 lines, no deletions** — one import; one new panel row ("🧪 Live test · direct self-chat"); the direct section (action, report renderer, reply handler); one `register_action` call | The test must be **explicitly triggered** by the owner in the live runtime, and its verdict must be visible. The UI holds no send logic: it resolves the exact selection-message reply and delegates everything to the service |
| `tests/test_premium_emoji_direct.py` | **NEW** (~938 lines, **50 tests**) | Pins the whole contract: payload geometry, UTF-16 offsets, document id, the rebuilt TL entity, Saved-Messages-only destination, owner gate, no helper-bot inline query, no send on failed source validation, no Unicode fallback, read-back rejections (missing entity / mismatched id / wrong span / wrong chat), preservation-vs-attribution separation, and the UI wiring |
| `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md` | **replaced in full** | Canonical latest-only records for this task (the previous reports described a different task) |

**Deliberately unchanged:** `backend/services/premium_emoji_inline_service.py`
(zero diff), the helper bridge, `inline_engine`, the probe service, the
existing `tests/test_premium_emoji_inline.py` /
`tests/test_premium_emoji_probe.py` /
`tests/test_premium_emoji_self_send_route.py`, the database, the AI runtime,
the schema, and unrelated working-tree state (the pre-existing untracked
`telegram-self-bot/` directory is untouched and is **not** part of this
commit).

### What the service guarantees (the requirements, item by item)

1. **Existing configured self-client** — the handler passes the self client it
   is already running on; the service never creates a client, session or loop
   (architecture tests forbid `TelegramClient`/`create_task`/event loops in the
   module).
2. **No helper-bot inline path** — no `inline_query`, no `click`, no inline
   engine import; tests assert the fake client's `inline_query` is never called
   and the module contains no listener/inline registration.
3. **Genuine entity, real document id** — the id comes from the source reply's
   `MessageEntityCustomEmoji`; the outbound TL entity is rebuilt and matched
   against the validated payload (exactly one custom-emoji entity, same
   document id/offset/length) or nothing is sent.
4. **UTF-16 geometry** — offsets/lengths are computed in UTF-16 code units
   (`utf16_length`), pinned by tests for astral and BMP glyphs.
5. **Pre-send validation** — owner → destination → document id → glyph
   (`alt`, fallback source span) → payload → entity; each failure is a precise
   diagnosis with nothing sent.
6. **Saved Messages only** — any `chat_id != owner_id` is refused before
   anything is sent; the read-back additionally checks the stored message's own
   chat.
7. **One explicitly triggered message** — one action, one reply, one send, no
   retry loop, no recurring sender; the fake client records exactly one send
   and one read.
8. **Reused abstractions** — `get_self_client`/`get_owner_id`, the pending-input
   reply mode, `telegram_api.messages.send_message` (bounded
   `guarded_await`), the shared payload builder/validator, `_owner_premium`,
   `_document_facts`, `_fetch_stored_message`.
9. **Isolation** — no caller falls back to this path; the inline route is not
   rerouted (pinned by test).
10. **No fabrication / no Unicode fallback** — a plain Unicode reply is refused
    by the handler; a stripped entity is reported as stripped, never re-sent as
    a glyph.
11. **No security weakening** — owner-only action path as before; the
    Saved-Messages restriction and all inline-route checks untouched.
12. **Fail closed** — unusable document id/glyph/owner/destination all end in an
    honest diagnosis, nothing sent.
13. **Logs** — bounded `[PREMIUM_DIRECT]` stage lines: ids, offsets, lengths,
    booleans, bounded exception class/text; no session strings, tokens, access
    hashes or message bodies (asserted by test).

## 2. The live-run procedure (owner-triggered, one message)

1. `Menu` → **Emoji** → **"🧪 Live test · direct self-chat"**.
2. A selection message is sent to **Saved Messages**; reply to **that exact
   message** with the Premium custom emoji (only a reply to it is accepted).
3. The owner's own account then sends **one** labelled message
   (`Direct self-chat test: <glyph>`) directly to Saved Messages with the real
   entity, and that exact message is fetched back by id.
4. The panel shows the verdict; the application log carries the same verdict as
   `[PREMIUM_DIRECT] … DIAGNOSIS diagnosis=<…>` (plus the stage lines before
   it). The task's falsifier table (what each diagnosis means) lives in
   `INVESTIGATION.md` §7.

## 3. Tests and validation

| Check | Command | Result |
|---|---|---|
| Compile | `.venv/bin/python -m compileall -q backend/services/premium_emoji_direct_service.py backend/bot/handlers/emoji.py tests/test_premium_emoji_direct.py` | **COMPILE OK**, exit 0 |
| Focused (new file) | `.venv/bin/python -m pytest tests/test_premium_emoji_direct.py -q` | **50 passed**, exit 0 |
| Emoji-related suites | `.venv/bin/python -m pytest tests/test_premium_emoji_direct.py tests/test_premium_emoji_inline.py tests/test_premium_emoji_probe.py tests/test_premium_emoji_self_send_route.py tests/test_emoji_ui.py -q` | **255 passed, 1 skipped**, exit 0 (the skip is the pre-existing opt-in `live_telegram` test) |
| Full suite | `.venv/bin/python -m pytest tests -q` (exit status captured) | **5894 passed, 27 skipped, 2 warnings in 120.39s**, **EXIT=0** — baseline at `2eaaf93` was 5844 passed / 27 skipped, i.e. exactly the **+50** new tests, **0 regressions** |
| Whitespace / diff sanity | `git diff --check` | exit 0 |
| Full diff review | `git diff` + the two new files read end-to-end | done before commit |

### Offline (green) vs live (NOT executed) — kept strictly separate

**Offline:** every service decision (payload/UTF-16/document-id/entity
validation, destination gate, owner gate, one-send/no-retry, exact-id read-back,
all read-back rejections, attribution separation, trace shape) and every UI
wiring assertion, all against fakes at the Telegram boundary.

**Live:** the actual Telegram send and read-back of the stored message.
**Not executed here** (no credentials). No test in this task claims
server-side behaviour, and no statement anywhere claims the entity survives a
real send — that is precisely the open question.

## 4. Acceptance criteria vs. what is proven where

| Criterion (task) | Offline | Live |
|---|---|---|
| Outgoing message built with a genuine `MessageEntityCustomEmoji` from the source document id | ✅ (rebuilt TL entity matched to the validated payload) | pending |
| Correct UTF-16 offset/length | ✅ (unit-counted, pinned by tests) | pending |
| Source entity / document id / span / payload validated before send | ✅ | pending |
| Destination restricted to the owner's Saved Messages (no arbitrary chat) | ✅ (pre-send gate + stored-chat check) | pending |
| One explicitly triggered message, no retry/recurring sender | ✅ | pending |
| No helper-bot inline query/click anywhere in the direct path | ✅ (architecture + fake-client tests) | n/a |
| No fabricated entity, no Unicode fallback, no success on a glyph alone | ✅ | pending |
| Exact-id read-back: message exists in Saved Messages | ✅ (fetch by exact id from the owner's own peer) | pending |
| Read-back rejects: no entity / mismatched id / mismatched span / wrong chat | ✅ | pending |
| Entity preservation vs `via_bot_id` attribution judged separately; inline route's attribution checks unchanged | ✅ (zero-line diff on the inline service; explicit tests) | n/a |
| **The direct send preserves the entity on real Telegram** | **cannot be decided offline** | **PENDING — the live run** |

## 5. What was not verified, and the remaining blocker

* **Not verified:** the decisive live fact — whether Telegram's stored message
  keeps the custom-emoji entity on a direct owner-authored send to Saved
  Messages; and, consequently, whether the client renders it. No live request
  was made from this workspace.
* **Not claimed:** that the direct route works, that it fails, or that any
  Telegram-side rule (entitlement, Premium, `free` flag, `alt` rule) is the
  cause of the inline route's entity loss. The inline route's three attempts
  remain the only live evidence, and they are a *pre-send* observation.
* **Remaining blocker:** this workspace has no Telegram credentials and the
  deployment has no shell — so the live step must run in the deployed runtime
  through the owner-only action (§2). Until then, live verification remains
  **pending** by design, exactly as the task allows ("If the coding environment
  cannot trigger the live test, implement the safe diagnostic path and report
  that live verification remains pending").

## 6. Git status

| Step | Value |
|---|---|
| Files in this task's commit | `backend/services/premium_emoji_direct_service.py`, `backend/bot/handlers/emoji.py`, `tests/test_premium_emoji_direct.py`, `INVESTIGATION.md`, `IMPLEMENTATION_REPORT.md` |
| Commit | one commit for the task (`type: description` style, matching the repository) |
| Push | `git push origin HEAD:main` — fast-forward only; no rebase, no force-push, no history rewrite, no unrelated work discarded |
| Remote verification | after the push: `git fetch origin` → `git rev-parse HEAD` = `git rev-parse origin/main` = `git ls-remote origin refs/heads/main`; `git merge-base --is-ancestor <commit> origin/main` exit 0 |

A commit cannot contain its own hash: the commit SHA and the verified
`origin/main` SHA are reported in this task's delivery response, re-checked with
the commands above after the push. The pre-existing untracked
`telegram-self-bot/` directory is left exactly as found and is excluded from the
commit.
