# IMPLEMENTATION_REPORT.md — bounded `[SAVE_TRACE]` instrumentation for the private-link Deep Save path

> This file describes ONLY the current state of the repository for this task and
> **replaces every previous report in full**. The Save pipeline's behaviour is
> unchanged: this phase adds observability, not a fix. Reply Mode's early
> trigger deletion and its single atomic finished-panel edit (the previous
> report) still hold, as does every album / metadata / retrieval / delete /
> Save-by-Link contract.

| Item | Value |
|---|---|
| Stage | Diagnostic instrumentation: opt-in, bounded, line-level tracing of one Save attempt (link parsing → peer resolution → Deep Save → transfer → persistence) |
| Date | 2026-10-22 |
| Project / remote | LifeOS Telegram self-bot, `Onlyicing1/Telegram-self-bot` (`origin`) |
| Base commit | `be1351def5a41e685331791a970e9bfd64d7247a` (`perf(save): delete reply early and combine final panel edit`) |
| Classification logic | **NOT changed.** `VIEW_ONCE_TTL_SECONDS`, `media_ttl_seconds`, `ephemeral_media_kind`, `ephemeral_refusal` and every guard keep their exact source semantics |
| Live Telegram / Supabase / Render | **NOT touched.** No session, no RPC, no SQL, no migration, no schema change, no `.env`, no credentials, no deployment, no Render shell |
| Documents not modified | `DATABASE_ARCHITECTURE.md`, `README.md`, `INVESTIGATION.md` (unchanged — its conclusions about inline-panel identifiers are untouched by this phase) |
| Interpreter used for verification | CPython 3.12 venv with `backend/requirements.txt` (**pinned Telethon 1.43.0**) + `pytest 9.1.1` + `pytest-asyncio` |

---

## 1. What was added

Two layers, both OFF by default and both scoped to ONE attempt:

1. **Stage events** (`ENTER`, `BRANCH`, `CALL_START`, `CALL_END`, `RETURN`,
   `EXCEPTION`, `CLEANUP`, `NOT_REACHED`, `TRUNCATED`) emitted by the Save code
   itself at the handler, parsing, resolution, Deep Save, transfer and
   persistence boundaries.
2. **Line events** — real `sys.settrace` records for the executed statements of
   the Save path only (whitelisted functions, whitelisted module, same asyncio
   task), so a refusal can be pinned to the exact line and condition.

Nothing in the Save algorithm, the view-once guard, the panel architecture or
the status-message behaviour was changed.

## 2. How it is enabled and bounded

| Concern | Implementation |
|---|---|
| Switch | `SAVE_TRACE` (values `1/true/yes/on`, case-insensitive). Read per attempt in `backend/runtime/save_trace.py::save_trace_enabled`. No other env var is invented; the project had no equivalent tracer switch |
| OFF behaviour | `SaveTrace.begin()` returns a disabled instance: `event()` returns immediately, `capturing()` installs nothing. No `[SAVE_TRACE]` record, no `sys.settrace`, no measurable overhead |
| Event bound | `DEFAULT_MAX_EVENTS = 3000` per attempt |
| Duration bound | `DEFAULT_MAX_DURATION_S = 120.0` per attempt |
| At a bound | exactly ONE `TRUNCATED` record (`reason=events` / `reason=duration`), the tracer is restored at that moment, and no further record is written |
| Concurrency | one line-traced attempt at a time: a second concurrent attempt keeps its stage events but never replaces the installed tracer (a thread-wide tracer would otherwise be stolen and restored to the wrong state) |
| Restoration | `with trace.capturing():` in `execute_link_save` / `execute_save` and in `_save_link_input_handler`, so the previous tracer is reinstated on success, exception, **cancellation** and **timeout** (verified by tests) |
| Failure containment | every trace call is inside the tracer's own `try/except`; a logging failure is swallowed. A trace can never turn a successful save into a failure |
| Cancellation | `asyncio.CancelledError` is re-raised from the instrumented transfer blocks; the trace records `outcome=inconclusive` first |

## 3. Coverage — stages that are traced

| Stage | Where |
|---|---|
| A. Input handler | `_save_link_input_handler` (entry, empty-link branch, status creation, service call+result classification, result render, trigger cleanup), `_save_reply_wait_handler` (entry, panel editability, trigger read, deletion, target lookup, save call, terminal render), `_start_status`, `_delete_trigger_message` |
| B. Link parsing | `parse_link_target`, `_parse_tg_openmessage`, `_parse_http_link`, `_parse_positive_id` — scheme branch, `tg://openmessage` match, peer-parameter count **and name**, id validity, `LinkTarget` construction, every failure branch |
| C. Peer resolution + retrieval | `_resolve_link_peer` (username / cache / explicit entity / dialog scan), `_dialog_entity` (bounded scan, per-attempt outcome), `_fetch_linked_message` (peer read, peer-less lookup, each returned-id check), then `execute_link_save`'s `message_id_match` and `peer_match` branches |
| D. Deep Save entry | `execute_save` (metadata branch, album resolution, single-vs-album), `_deep_save_one` (media presence + class, the ephemeral guard with its evidence, save-code issuance, size limit) |
| E. Transfer + persistence | `download_media`, download validation (`file_exists`, `file_non_empty`), `send_file`, `insert_save` — each with `CALL_START` and a `CALL_END` that is written only after the real postcondition (byte counts / returned message / row) |

`_deep_save_album` is traced at entry and at its pre-flight ephemeral decision
only; the per-member transfer loop keeps its existing `record_event` telemetry.

## 4. The view-once evidence this exists to produce

`_deep_save_one` now records, before the guard runs:

```
stage=deep_save kind=BRANCH name=media_present   matched=true media_class=MessageMediaDocument
stage=deep_save kind=BRANCH name=ephemeral_guard matched=view_once ttl_field=present ttl=view_once_sentinel
```

and, when the guard fires:

```
stage=deep_save kind=RETURN     func=_deep_save_one outcome=refused media_kind=view_once ttl=view_once_sentinel
stage=download  kind=NOT_REACHED func=_deep_save_one outcome=not_reached reason=ephemeral_refusal
stage=upload    kind=NOT_REACHED func=_deep_save_one outcome=not_reached reason=ephemeral_refusal
stage=persist   kind=NOT_REACHED func=_deep_save_one outcome=not_reached reason=ephemeral_refusal
```

`ephemeral_evidence(media)` (new, pure, decides nothing) reports:
`media_class`, whether the TL object actually carries a `ttl_seconds` field, and
the category of that value — `absent`, `no_positive_ttl`, `expiring_positive`,
`view_once_sentinel` — plus the resulting kind. An absent, non-int, zero or
negative TTL can therefore never be reported as view-once, and ordinary private
media reports `ttl_field=present ttl=no_positive_ttl` (confirmed in a local run).

The exact lines that execute are in the `LINE` records, so the terminating
statement is identified by its own line number in `backend/services/save_service.py`
(the guard `if guard_kind is not None:` and, one level down, the
`ttl == VIEW_ONCE_TTL_SECONDS` comparison inside `ephemeral_media_kind`).

**No conclusion is drawn here about the live reference message.** Without a real
`SAVE_TRACE=1` run of the owner's own `tg://openmessage?…` link for that item
there is no evidence about its TTL field, and this phase deliberately does not
change the classification to "test" it.

## 5. Sensitive data excluded

* The private link itself is never logged: `[LINK_SAVE] resolving link: fp:…`.
  The previous `[LINK_SAVE] resolving link: <full link>` line is gone (a
  `tg://` link names a private chat and a message).
* Chat/user/message ids in the touched functions are logged as `fp:<8 hex>` —
  a keyed BLAKE2s digest with a per-process random key, so two records of one
  render instance are comparable and nothing is reversible.
* No message text, caption, file bytes, media ids, object `repr()`, `f_locals`,
  full filesystem path or credential is ever written. `LINE` records carry only
  `mod=<repo-relative module>`, `func`, `line`.
* Every value is collapsed to one whitespace-free token and truncated to 120
  chars, so a record is always one greppable `key=value` line.
* Peer-resolution failures log the reason kind plus a fingerprint instead of the
  Telegram error text (which can echo the peer id). The owner-facing panel text
  is unchanged.

## 6. Limits of line-level tracing (stated, not glossed over)

* `sys.settrace` is **thread-wide**. This VM/process is single-threaded
  asyncio, so the tracer is installed on the event-loop thread; that is why the
  scope filter is *mandatory and threefold*: module whitelist, function
  whitelist, and "same `asyncio.current_task()` as the attempt". Callbacks of
  other tasks in the same thread are filtered out at `call` time, before any
  line is recorded.
* The global tracer is invoked for every *call* in the thread while installed
  (a memoised code-object lookup each), which is why the duration bound and the
  one-traced-attempt rule exist.
* Only the modules/functions in `LINE_SCOPE` produce line events. Telethon
  internals and the rest of the application never do.
* Reply Mode installs line capture at `execute_save` (the panel/trigger stages
  before it are stage events only), because that handler was not restructured
  for this diagnostic phase.
* Tracing is application-level evidence. It measures nothing about Telegram's
  network behaviour and adds no latency claims.

## 7. Changed files

| File | Change |
|---|---|
| `backend/runtime/save_trace.py` | **new**: `SaveTrace` (stage events + scoped `sys.settrace`), `fingerprint`, `ephemeral`-free constants, bounds, restoration, `LINE_SCOPE` |
| `backend/services/save_service.py` | `ephemeral_evidence`; optional `trace=` on `parse_link_target`, `_parse_tg_openmessage`, `_parse_http_link`, `_resolve_link_peer`, `_dialog_entity`, `_fetch_linked_message`, `execute_link_save`, `execute_save`, `_deep_save_one`, `_deep_save_album`; stage events at every discovered boundary; ids in the touched logs fingerprinted |
| `backend/bot/handlers/save.py` | `_save_result_outcome`; trace lifecycle for the link and reply handlers; `trace=` into `_start_status` / `_delete_trigger_message` / `execute_*`; trigger cleanup now uses the shared `_delete_trigger_message` (one delete site, same behaviour) |
| `tests/test_save_trace.py` | **new**: 20 focused tests |
| `AGENTS.md` | §6 trace bullet, §11 `SAVE_TRACE`, runtime layout entry |
| `IMPLEMENTATION_REPORT.md` | this report |

No dependency was added. `backend.runtime.__init__` is empty, so importing
`backend.runtime.save_trace` from the service and the handler introduces no
import cycle.

## 8. Tests (executed — exact commands and results)

| Command | Result |
|---|---|
| `pytest tests/test_save_trace.py -q` | **20 passed** |
| `pytest tests/test_save_live_status_and_private_link.py tests/test_save_album_view_once_progress.py tests/test_save_trace.py tests/test_save_reply_panel_edit.py tests/test_save_success_panel.py tests/test_save_v2_wiring.py tests/test_20_advanced_execution.py -q` | **216 passed** |
| Save/panel batch (the 14 Save suites + `test_save_trace.py`) | **457 passed** |
| `pytest tests -q` (full suite) | **6703 passed, 27 skipped, 1 failed** (137 s) |
| `python -m compileall -q backend tests/test_save_trace.py` | OK |
| `git diff --check` | clean |

The single failure is
`tests/test_task_scheduler.py::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`
("interval advancement exceeded bound"). It is **pre-existing and unrelated**:
it reproduces identically on a clean `git archive HEAD` checkout of the base
commit `be1351d` (10 passed, 1 failed), with no Save or trace code involved.

Focused coverage added in `tests/test_save_trace.py`:

1. off by default (no record, no tracer, save still succeeds);
2. enabled tracing records real executed lines of `execute_link_save`,
   `parse_link_target` and `_deep_save_one` with line numbers;
3. unrelated code and a *different asyncio task* calling the same scoped
   function produce no line events for the traced attempt;
4. one trace id, strictly increasing `seq`, non-decreasing `elapsed_ms`;
5. event bound (`reason=events`) and duration bound (`reason=duration`) both
   truncate and restore;
6. restoration after success, exception, cancellation and `wait_for` timeout,
   plus "a second attempt does not steal the active tracer";
7. a logging sink that always raises leaves the save successful;
8. a valid private link resolves, is id- and peer-validated, and is saved;
9. a wrong message id and a wrong peer stay refused (nothing saved, no
   download) and record `matched=false`;
10. view-once classification records `ttl_field`/`ttl` category and `matched`;
11. a view-once refusal records the early return and marks
    download/upload/persist `NOT_REACHED`, with no transfer and no row;
12. ordinary private media traces download → upload → persist with `ok`
    postconditions and the stored row;
13. the existing protection still refuses (no download, no send, no row);
14. no record contains the link, the peer id, the message id, the caption or a
    full path, and fingerprints are stable within the process.

## 9. What still requires live runtime verification

* The reference message has **not** been traced. A real `SAVE_TRACE=1` run on
  Render is what settles whether that media really carries
  `VIEW_ONCE_TTL_SECONDS` or whether something upstream (link→message mapping)
  hands `_deep_save_one` a different message.
* One ordinary, non-ephemeral private-link save in the same run is needed to
  prove the download/upload/persist stages on live Telegram (they are proven
  only against the fakes here).
* Whether Render's log pipeline shows every `[SAVE_TRACE]` record at
  `LOG_LEVEL=INFO` is a deployment observation, not something this phase can
  assert.
* No deployment of this commit was verified. `render.yaml` is unchanged and no
  Render action was taken.

## 10. How to produce the real trace (owner action)

1. Set `SAVE_TRACE=1` for the Render service (or the `.env` of a local run) and
   redeploy so the process sees it.
2. Save the reference item once through `Menu` → `Save` → `Deep Save` →
   `Save using a link`, and once an ordinary non-ephemeral private media item.
3. Filter the logs with `[SAVE_TRACE]`, group by `id=`, read `matched=`/`ttl=`
   at `name=ephemeral_guard`, then the last `LINE` record before the `RETURN`.
4. Unset `SAVE_TRACE` afterwards: with the flag off the tracer is never
   installed and the Save path is byte-for-byte the production path again
   (apart from the fingerprinted ids in its own log lines).
