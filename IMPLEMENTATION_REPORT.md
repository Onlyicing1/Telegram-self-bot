# IMPLEMENTATION_REPORT.md — the `view_once` refusal on a `MessageMediaPhoto`

> This file describes ONLY the current state of the repository for this task and
> **replaces every previous report in full**. The previous phase (bounded
> `[SAVE_TRACE]` instrumentation) is unchanged and still holds; its records are
> what made this question answerable.

| Item | Value |
|---|---|
| Question | A saved photo was refused as view-once with download/upload/persist `NOT_REACHED`. Is Deep Save misclassifying ordinary media, or is the item genuinely one-time? |
| Verdict | **B — genuine view-once media.** The gate is correct; the value came from Telegram, not from a local assumption. **No bypass and no behavioral change were made.** |
| Root cause of the reported *change in behavior* | Deliberate: the ephemeral-media gate was introduced on 2026-10-21 in `577a65e` (`feat(save): save full albums, refuse ephemeral media, report live progress`). Before that commit no gate existed, which is why the same item used to be saved. |
| Media gate | **Unchanged.** `VIEW_ONCE_TTL_SECONDS`, `media_ttl_seconds`, `ephemeral_media_kind`, `ephemeral_refusal` and both guards keep their exact source semantics |
| Date | 2026-10-22 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`) |
| Base commit | `586d073` (`feat(save): add bounded opt-in SAVE_TRACE instrumentation`) |
| Live Telegram / Supabase / Render | **NOT touched.** No session, no RPC, no SQL, no migration, no `.env`, no credentials, no deployment, no Render shell |
| Documents not modified | `DATABASE_ARCHITECTURE.md`, `README.md`, `INVESTIGATION.md` |
| Interpreter used for verification | CPython 3.12 venv with `backend/requirements.txt` (**pinned Telethon 1.43.0**) + `pytest` + `pytest-asyncio` |

---

## 1. The trace, and the one code path that can produce it

The production trace is the reply path (`_save_reply_wait_handler` →
`execute_save` → `_deep_save_one`), with `media_class=MessageMediaPhoto`,
`ttl_field=present`, `ttl=view_once_sentinel`, the guard matching, and
`download` / `upload` / `persist` recorded as `NOT_REACHED`.

Only one comparison in the codebase yields that category
(`backend/services/save_service.py`, `ephemeral_media_kind`):

```python
return EPHEMERAL_VIEW_ONCE if ttl == VIEW_ONCE_TTL_SECONDS else EPHEMERAL_EXPIRING
```

`VIEW_ONCE_TTL_SECONDS = 2147483647` (`0x7FFFFFFF`), and `ttl` comes from
`getattr(media, "ttl_seconds", None)` with a strict `int` check. So the trace is
evidence that `reply_msg.media.ttl_seconds` was **exactly** `2147483647` — not
"a large number", not "a photo", not "a present field".

One correction about reading that trace: `ttl_field=present` proves nothing on
its own, because every Telethon TL object carries the attribute (it is `None`
when Telegram sent no value). The **`ttl=view_once_sentinel` category** is the
evidence, and it cannot be produced by `None`, `0`, a bool, a string or any
other integer.

## 2. Where that value can and cannot come from

Repository audit (the whole `backend/` tree):

* Nothing constructs `MessageMediaPhoto` / `MessageMediaDocument`; there is no
  serializer that rebuilds media, and `backend/telegram_api/_helpers.py`
  serializes messages for AI context only, never for Save.
* The reply path passes the object returned by `client.get_messages(chat_id,
  ids=target_id)` straight into `execute_save`; the link path passes the object
  returned by `_fetch_linked_message`. Neither rewrites `media`.
* `_deep_save_one` reads `reply_msg.media` (`_extract_source_media` reads the
  same object), so classification and transfer see one object.

Therefore `ttl_seconds` is Telethon's parsed wire value. A local default,
fixture or cache cannot produce the sentinel.

## 3. Pinned Telethon: the field and its parsing

`backend/requirements.txt` pins `telethon==1.43.0`, which ships TL **layer 224**.
In that wheel (`telethon/tl/types/__init__.py`):

```python
class MessageMediaPhoto(TLObject):
    CONSTRUCTOR_ID = 0xe216eb63
    def __init__(self, spoiler=None, live_photo=None, photo=None, ttl_seconds=None, video=None): ...
    @classmethod
    def from_reader(cls, reader):
        flags = reader.read_int()
        ...
        if flags & 4:
            _ttl_seconds = reader.read_int()
```

Executed against the installed package (real constructor bytes parsed by
Telethon's own reader):

| Wire flags | Parsed `ttl_seconds` | `ephemeral_media_kind` |
|---|---|---|
| `photo` only | `None` | `None` (ordinary) |
| `photo` + `ttl_seconds = 0` | `0` | `None` (ordinary) |
| `photo` + `ttl_seconds = 30` | `30` | `expiring` |
| `photo` + `ttl_seconds = 2147483646` | `2147483646` | `expiring` |
| `photo` + `ttl_seconds = 2147483647` | `2147483647` | `view_once` |
| `photo` + `ttl_seconds = 2147483647` + `spoiler` | `2147483647` | `view_once` (spoiler is an independent flag) |

`MessageMediaDocument` carries the same flag bit and the same semantics.

## 4. Telegram's documented semantics (the decisive evidence)

* `https://core.telegram.org/constructor/messageMediaPhoto` — `ttl_seconds`:
  “Time to live of self-destructing photo, can be 0 to disable
  self-destruction, `0x7FFFFFFF` to self-destruct the photo immediately after
  it's played; otherwise self-destructs `ttl_seconds` after it's played.”
* `https://core.telegram.org/api/views` — expiring media is
  `0 < media.ttl_seconds < 0x7FFFFFFF`; **view-once media is exactly
  `media.ttl_seconds == 0x7FFFFFFF`**, and the client is told to start no
  timer and delete it after the viewer closes.
* Chat auto-delete is a different field (`message.ttl_period`,
  `flags.25?int`), and `spoiler` is a separate flag. Neither can put the
  sentinel on an ordinary photo.
* Secret Chats use a different schema (`decryptedMessage.ttl`), so they are not
  a source of `media.ttl_seconds` either.

So the mapping used by `ephemeral_media_kind` is exactly the documented one:
no value or `0` → ordinary, the exact sentinel → view-once, anything else
positive → expiring.

## 5. Git history — what actually changed

| Item | Commit | Date |
|---|---|---|
| `VIEW_ONCE_TTL_SECONDS`, `media_ttl_seconds`, `ephemeral_media_kind`, `ephemeral_refusal`, the guard in `_deep_save_one`, the album pre-flight | `577a65e` | 2026-10-21 |
| `ephemeral_evidence` (trace only; decides nothing) | `586d073` | 2026-10-22 |
| Telethon `1.34.0` → `1.43.0` | `3370eae` | earlier |

* `git log -S "VIEW_ONCE_TTL_SECONDS"` and `git log -S "ttl_seconds"` return
  only those two Save commits; `git blame` on the whole classification block
  attributes every line to them.
* **No commit changed media resolution, downloading or Save execution in a way
  that could fabricate a TTL.** The only dependency change in the window is the
  Telethon bump, and it cannot explain this: `telethon/client/messageparse.py`
  is byte-identical between `1.34.0` and `1.43.0` (`diff` produced no output),
  and `ttl_seconds` sits on the same flag bit (`flags.2`) in both versions. The
  layer-224 addition to `messageMediaPhoto` is `live_photo`/`video` — Live
  Photos — not a TTL change.
* The behavior the owner remembers is the **pre-`577a65e`** pipeline: there was
  no gate, so a one-time photo was downloaded and re-uploaded. `577a65e` added
  the gate on purpose (its stated requirement: “one-time / expiring media must
  be refused deterministically BEFORE any download or upload, with no success
  record and no bypass”). That is the entire regression story.

## 6. Why this is not case A (a false positive)

To call this a false positive, an **ordinary** photo would have to arrive with
`ttl_seconds == 0x7FFFFFFF`. Every link in that chain is closed:

1. the value is Telethon's parsed wire field (no local construction, §2);
2. Telegram documents the sentinel as view-once, and documents `0`/absent as
   the ordinary cases (§3–§4);
3. no documented mechanism (chat auto-delete, spoilers, Live Photos, paid
   media, secret chats) places the sentinel on ordinary cloud media (§4);
4. the production item was a **photo**, and photos are fully covered by the
   same documented field — the sentinel is not document-only.

Therefore the item was sent as view-once, and refusing it is correct.
Removing or narrowing the guard would not fix a defect; it would bypass a
genuine Telegram one-time restriction, which this phase does not do.

## 7. What changed in this phase

**Behavior: nothing.** The gate is byte-for-byte the same logic, with the same
pre-transfer position and the same refusal text, for photos and documents,
single items and albums.

**Documentation (comments only, no logic):** the classification's own
description called every other positive TTL a “secret-chat self-destruct
timer”. Telegram documents those as expiring media in cloud chats too (the
timer starts when the media is opened), so the comment, the
`ephemeral_media_kind` docstring and `AGENTS.md` §6 now state the verified
split (`0` disables; `0x7FFFFFFF` view-once; otherwise expiring) and record that
the value always comes from Telegram.

**Tests:** `tests/test_save_view_once_photo_semantics.py` (new, 9 tests) pins
the semantics to the wire instead of to the implementation:

1. the pinned Telethon's `MessageMediaPhoto.CONSTRUCTOR_ID` is
   `0xe216eb63`, and its reader parses `photo`/`ttl_seconds` from the
   **documented** flag bits — `None`, `0`, `30`, `2147483646`,
   `2147483647`, with and without `spoiler`;
2. wire-parsed values map to the documented categories (`None`/`0` →
   ordinary, finite → expiring, exact sentinel → view-once);
3. **the production case**: a wire-parsed view-once **photo** is refused with
   zero client calls, zero rows and no forward, and the trace records
   `matched=view_once`, `ttl=view_once_sentinel`, `media_class=MessageMediaPhoto`
   plus `download`/`upload`/`persist` `NOT_REACHED` with
   `reason=ephemeral_refusal`;
4. that refusal traces no caption, chat id, message id or peer id;
5. an ordinary photo with no TTL still saves end to end (download → upload →
   row, `media_type=Photo`, no forward);
6. an ordinary photo whose TTL is Telegram's documented `0`
   (“disable self-destruction”) still saves;
7. a photo with a finite TTL (`30`) is refused with its own reason before any
   transfer;
8. only the exact sentinel is view-once — `None`, missing attribute, `0`,
   negative, bool, string and `2147483648` are never view-once;
9. link Save and reply Save both hand the resolved target to the single
   `execute_save` pipeline (`execute_link_save` → the link's message; Reply
   Mode → the replied-to message, never the trigger), with no forwarding.

## 8. Tests (executed — exact commands and results)

| Command | Result |
|---|---|
| `.venv/bin/python -m pytest tests/test_save_view_once_photo_semantics.py -q` | **9 passed** |
| `.venv/bin/python -m pytest tests/test_save_view_once_photo_semantics.py tests/test_save_trace.py tests/test_save_album_view_once_progress.py tests/test_save_live_status_and_private_link.py -q` | **107 passed** |
| `.venv/bin/python -m pytest tests -q` | **6712 passed, 27 skipped, 1 failed** (148.91 s) |
| `.venv/bin/python -m compileall -q backend/services/save_service.py tests/test_save_view_once_photo_semantics.py` | OK |
| `git diff --check` | clean |

The single failure is
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
(“interval advancement exceeded bound”). It is **pre-existing and unrelated**:
it reproduces identically on a clean `git archive 586d073` checkout
(`1 failed`), with no Save, media or trace code involved.

## 9. What remains unverified (stated, not glossed over)

* **Live behavior is unverified.** No Telegram session was used, so this phase
  proves classification and pipeline behavior against the pinned library and
  fakes; it does not re-run the owner's save.
* The trace recorded the **category**, not the raw integer or the message's
  `ttl_period`. The category can only be produced by exact equality with
  `2147483647`, but a trace line carrying the raw `ttl_seconds` value would
  remove the last step of inference. It was not added in this phase because it
  changes the documented `[SAVE_TRACE]` output and its tests.
* A negative `ttl_seconds` is treated as ordinary (fail-open). Telegram's
  documented space is `0 … 0x7FFFFFFF`, so there is no evidence this occurs;
  tightening it would be an unrequested behavior change.
* The trace is from the **reply** path. The link path is proven to share the
  same `execute_save` pipeline by test (§7.9), not by a production trace.
* **Deployment: NOT VERIFIED.** `render.yaml` is unchanged and no Render action
  was taken.

## 10. If the owner wants one-time media saved

That is a **policy change**, not a bug fix: it means deliberately removing or
narrowing the `577a65e` gate (and its `AGENTS.md` contract). It must be an
explicit decision by the owner of the account, and it is not made here.
