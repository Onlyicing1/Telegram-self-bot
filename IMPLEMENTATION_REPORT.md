# IMPLEMENTATION REPORT — History Peer Policy Phase 2 (per-peer counts)

Base commit: `ffe72fd0bce9286e3d3d6583614b3b243bfd4fdf` (`origin/main`, Phase 1;
fetched and verified as an ancestor before any edit). This report replaces the
Phase 1 report; that delivery remains in the repository history and its
classifier is unchanged.

## 1. What this delivery is

A deterministic History product policy layered on the existing `PeerType`
classifier, dynamic count choices for the History panel, rejection of stale or
invalid count callbacks, and enforcement at every manual History summarization
entry point. The policy answers only *how many messages one manual History
request may cover for this chat*. It does not decide AI feasibility, retrieval
order, scheduling or delivery.

Nothing from later phases was started: no incremental retrieval, no cursor change,
no persistence, no database object, no scheduled History, no admission change, no
provider or delivery change, no new client/scheduler/listener/executor/loop.

## 2. Exact policy contract

New **`backend/services/history_policy.py`** — pure, synchronous, no RPC/database/
AI/UI imports. It consumes `PeerType` from `backend/telegram_api/peers.py`; the
enum and classifier are not duplicated or modified.

| PeerType | Hard maximum | Permitted panel choices |
| --- | ---: | --- |
| `PRIVATE_USER` | 1000 | 100, 250, 500, 1000 |
| `PRIVATE_BOT` | 300 | 100, 250, 300 |
| `BASIC_GROUP` | 1000 | 100, 250, 500, 1000 |
| `SUPERGROUP` | 1000 | 100, 250, 500, 1000 |
| `BROADCAST_CHANNEL` | 300 | 100, 250, 300 |
| `UNKNOWN` | 0 | none — refuse |

API:

- `maximum_for(peer_type) -> int`
- `choices_for(peer_type) -> tuple[int, ...]`
- `validate_count(peer_type, requested) -> str | None` — hard-cap validation used
  at the service boundary. Any positive `int` up to the maximum is accepted (AI
  tools may compute a small literal count); negative/zero, booleans, strings,
  floats, non-`PeerType` values and `UNKNOWN` are refused. An over-cap request is
  **refused, never clamped with `min()`**.
- `validate_choice(peer_type, requested) -> str | None` — the panel's narrower
  validation: the value must be one of that peer's exact offered choices.
- `UNKNOWN_REFUSAL` — deterministic “chat type could not be determined” text.

The 300-message caps are hard product limits. `UNKNOWN` never falls back to the
1,000-message policy, and refusal reasons are deterministic strings that name the
actual limit and the offered choices.

## 3. Classify the actual current entity

`backend/services/history_service.py` gained:

```python
async def classify_chat(source, chat_id) -> PeerType
```

- Resolves the **currently returned Telethon entity** through the existing
  `telegram_api.entities.get_entity` facade, bounded by `rpc_await` and the
  History RPC timeout, then calls the Phase 1 `classify_entity`.
- Accepts the runtime `TelegramAPI` source or a raw client with `get_entity`; it
  does not require the full retrieval surface just to classify.
- Raises `HistoryError` when resolution fails. Callers fail closed.
- Nothing is cached or persisted, so a migrated group is classified correctly on
  the next call.

Peer type is never inferred from dialog flags, titles, usernames, id patterns,
serialized display dictionaries, previous selections or a stale cached value.

## 4. Panel changes

`backend/bot/handlers/history.py`:

- `_State` carries an ephemeral `peer_type: PeerType | None`; there is no
  persistence and no new authoritative peer cache. `_pick_chat` classifies the
  entity Telegram returns for the selected id, clears the selection on a
  resolution failure or `UNKNOWN`, and resets a count that the new peer does not
  offer to the shared default (100) **as part of a new selection**, not as a clamp
  of a request.
- `_panel_buttons` renders only `history_policy.choices_for(peer_type)`. A
  broadcast channel and a bot show 100/250/300; the three large peer types show
  100/250/500/1000. Invalid choices are **hidden**, not rendered disabled. With no
  peer selected there is no honest choice set, so the count rows wait; the rest of
  the panel (Select Chat, Focus, Run, Link) is unchanged.
- `_panel_body` additionally states the current chat's limit when a peer is
  selected.
- `_count_action` re-resolves and re-classifies the **current** entity on every
  callback instead of trusting callback data or the stored type. It then validates
  the value against `choices_for`/`validate_count`: a stale 1,000 or 500 callback
  arriving after the selection became a 300-cap peer is refused with the actual
  limit (“A broadcast channel is limited to 300 messages per request; choose 100,
  250 or 300.”) and the count is unchanged. It never clamps and never starts AI.
- `_validated_chat` (used by `_run_action` before any manager lookup or summary
  task) re-resolves the selected chat, re-classifies it, re-applies `validate_count`
  and `validate_choice`, and refuses on `UNKNOWN`/resolution failure.
- `COUNTS` now comes from the policy's large-peer choice set; `DEFAULT_COUNT`
  remains 100.

Existing rendering, navigation, inline-edit delivery, Helper Bot ownership, focus
and link input flows are unchanged.

## 5. Service-boundary enforcement

`backend/services/history_ai_service._prepare` — shared by `summarize_history`
and `translate_history` — now runs, before **any** retrieval or provider call:

1. Resolve and classify the actual peer (`history_service.classify_chat`).
2. Validate the effective count with `history_policy.validate_count`.
3. Refuse with the new `ERROR_POLICY = "history_peer_policy"` and the policy text.
4. Only then perform the existing bounded retrieval.
5. The existing AI capacity gate still decides whether the workload fits; a
   300-message product limit does not promise AI feasibility.

A new `_prepare_failure` keeps the truthful message for a policy refusal instead
of labelling it “Couldn't read the Telegram history”. The pre-existing global
1,000-message bound keeps its explicit `capped` note (it is stated, not silent);
per-peer caps are never clamped. Chunk size, concurrency, call spacing, deadlines,
token estimation, provider routing and capacity arithmetic are untouched.

## 6. Linked-message / `ending_at` path

`_run_link` classifies the linked message's own chat from the entity Telegram
returned with it, or resolves `actual_chat` when that entity is absent. It
validates the count against the linked peer's policy **before** painting a
summary or starting `_run_summary`. A broadcast-channel link with a stale
1,000-message count is refused with the same honest limit. The exclusive
`linked_id + 1` upper bound and the existing resolver/delivery are unchanged.

## 7. Files changed

| File | Change |
| --- | --- |
| `backend/services/history_policy.py` | **new** — pure per-peer cap/choice policy |
| `backend/services/history_service.py` | `classify_chat` using the existing entity facade + classifier |
| `backend/services/history_ai_service.py` | policy gate in `_prepare`, `ERROR_POLICY`, honest policy refusal message |
| `backend/bot/handlers/history.py` | ephemeral peer type, dynamic choices, stale-callback and run/link revalidation |
| `tests/test_history_policy.py` | **new** — 78 policy tests |
| `tests/test_history_panel.py` | real Telethon fixtures, `get_entity`, dynamic-choice, stale-callback, UNKNOWN, no-provider and linked-cap tests |
| `tests/test_history_ai_tools.py` | fake-client entity resolution, service-bypass and UNKNOWN refusal tests |
| `tests/test_history_capacity.py` | fake-client entity resolution only |
| `IMPLEMENTATION_REPORT.md`, `INVESTIGATION.md`, `ROADMAP.md` | this delivery's reports |

`backend/telegram_api/peers.py` was **not** modified. No protected architecture
document was modified.

## 8. Tests and exact results

Environment: `/tmp/iv` virtualenv from `backend/requirements.txt` plus `pytest` and
`pytest-asyncio`, `PYTHONPATH=.`, no network and no live Telegram.

| Check | Result |
| --- | --- |
| `tests/test_history_policy.py` | **78 passed** |
| Policy + panel + AI tools + capacity + service + Phase 1 classifier (6 files) | **300 passed** |
| Full suite (`pytest -q -p no:cacheprovider tests/`) | **7,750 passed, 6 failed, 27 skipped** in 145.51 s |
| Baseline at Phase 1 base `ffe72fd` | 7,659 passed, **the same 6 failed**, 27 skipped |
| `py_compile` on all changed Python files | exit 0 |
| `git diff --check` | clean |

The six failures are the previously documented pre-existing ones
(`test_aed_db_security`, `test_canonical_schema_reconciliation`,
`test_database_setup_order` ×2, `test_private_message_db_security`,
`test_task_scheduler::test_recovered_interrupted_occurrence_is_picked_up_by_run_once`),
reproduced identically at the implementation base. No new regression.

Policy tests cover the exact mapping for every `PeerType`, every accepted choice,
500/1000 rejection for bots and channels, 300 for both capped types, 1000 for the
large types, `UNKNOWN` refusal for every value, non-integers/booleans/strings/
zero/negative/over-cap rejection, no clamping and deterministic refusal text.
Panel tests cover the dynamic choices for all five real peer types, stale
capped callbacks after a selection change, refusal without any provider call,
`UNKNOWN` selection/run refusal, and linked-channel cap refusal. Service tests
prove that bypassing the panel cannot bypass the policy: the refusal happens with
zero `iter_messages` calls and zero provider calls.

## 9. Scope compliance

Not implemented: incremental/oldest-first retrieval, cursor changes, pagination,
persistence or peer-type caching, database tables/migrations/SQL/Supabase,
scheduled History, TaskScheduler/TaskExecutionCoordinator/RuntimeSupervisor/
ToolExecutor changes, a new ToolExecutor action, ProviderManager/routing/pacing/
capacity changes, another client/listener/executor/update loop, delivery
destination changes, unrelated refactors or API renames. Nothing was deployed.

## 10. Limitations and live status

- **No live Telegram validation**: no real dialogs, bots, channels or migrations
  were exercised against an account. Tests construct real Telethon entity objects
  locally; classification correctness rests on Telethon's entity flags, which is
  what the Phase 1 classifier reads.
- The policy is evaluated on Telegram's currently resolved entity. A peer that
  Telegram cannot resolve is refused (`UNKNOWN`/`HistoryError`); it is never
  guessed from title, id or dialog flags.
- A 300-message product allowance can still be refused by the existing AI
  capacity gate; that refusal remains truthful and independent.
- Phase 3 (schema/repository investigation and implementation planning) and later
  work remain unimplemented; see `INVESTIGATION.md` and `ROADMAP.md`. The
  oldest-first incremental retrieval defect is still unfixed and is a prerequisite
  for scheduled processing.
