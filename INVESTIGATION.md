# INVESTIGATION — Peer classification, History policy, and later phases

Base: `ffe72fd0bce9286e3d3d6583614b3b243bfd4fdf` (Phase 1, verified ancestor).
Phase 2 is implemented on top of it: `backend/services/history_policy.py`, dynamic
panel choices, stale-callback rejection, and policy enforcement in the History
services and linked-message path. Later phases are recorded here as unresolved
work, not as behaviour that exists today.

## 1. Why a shared classifier exists (verified before Phase 1)

The same question — "what kind of peer is this?" — was answered four different
ways, and each answer was partly wrong:

| Site | Old rule | Defect |
| --- | --- | --- |
| `history._chat_kind` | `type(entity).__name__ == "User"`, `Dialog.is_channel` | class-name string; `Dialog.is_channel` is true for broadcast channels **and** supergroups, so correctness depended on check order; no `private_bot`, no gigagroup |
| `misc._general_id_action` | `hasattr(entity, "broadcast")` first | every real `Channel` has the attribute, so a supergroup displayed as “Group” and the megagroup branch was unreachable |
| `ghost_seen_v2._is_telegram_user` | class-name / `SimpleNamespace` / dialog heuristics | no entity-flag authority; bots handled only by a later attribute check |
| `anti_edit_delete_service.is_bot_entity` | class-name + `bot is True` | no shared taxonomy; another copy of the same question |

`telegram_api.entities.get_dialogs` returns only Dialog booleans
(`is_user`/`is_group`/`is_channel`), which cannot separate a broadcast channel
from a supergroup. Entity flags are the only authority.

## 2. Phase 1 decisions (implemented, unchanged)

1. **Pure classifier, no resolver wrapper.** `classify_entity(entity)` takes a
   resolved entity and performs no RPC.
2. **Strict flag precedence.** `User.bot` only; `Channel.broadcast` first, then
   `megagroup`/`gigagroup`; `Chat` is a basic group; everything else `UNKNOWN`.
   A gigagroup is a supergroup variant with no separate policy type.
3. **No persistence and no authoritative cache.** A peer type is derived from the
   currently resolved entity every time.
4. **Feature policy stays with the feature.** Ghost Seen keeps owner/self/deleted
   exclusion; Anti-Edit keeps its safe `False` fallback; History keeps delivery.
5. **Compatibility is explicit.** Non-Telethon doubles keep the legacy branches in
   the migrated call sites; the shared classifier itself never accepts a duck-typed
   object.
6. **No new architecture.** No client, listener, update loop, scheduler, service,
   database object, provider or delivery change.

## 3. Phase 2 decisions (implemented)

1. **Policy is separate from taxonomy.** `history_policy` maps `PeerType` to a hard
   per-request maximum and the exact panel choices. It is pure and owns no client,
   database, AI or rendering.
2. **300-message caps are hard.** Bots and broadcast channels refuse 500/1000 and
   any value above 300. There is no `min(requested, cap)` rewrite. `UNKNOWN` is
   refused; it never inherits the 1,000-message policy.
3. **Classification is of the actual current entity.** `history_service.classify_chat`
   resolves through the existing `entities.get_entity` facade and calls the Phase 1
   classifier. Dialog flags, titles, usernames, id patterns, prior selections and
   cached types are not evidence. Resolution failure fails closed.
4. **The panel hides invalid choices.** Bots and channels show 100/250/300; users,
   basic groups and supergroups show 100/250/500/1000. Hidden, not disabled.
5. **Callbacks are untrusted.** Every count callback re-resolves and re-classifies
   the current peer, recomputes the policy and refuses a stale/over-limit value
   with the actual limit. The selected count is not changed and no AI starts.
6. **The service is the enforcement boundary.** `_prepare` (shared by summarize and
   translate) resolves → classifies → validates → only then retrieves; the existing
   AI capacity gate remains independent. Bypassing the panel cannot bypass policy.
7. **The linked-message path is covered.** The linked message's own chat is
   classified (from its entity, or by resolving `actual_chat`) and its count is
   validated before the summary starts.
8. **The global 1,000 bound is unchanged.** Its existing explicit `capped` note is
   preserved; only per-peer caps refuse rather than clamp.

## 4. Verified facts for later phases (read at this base, not implemented)

1. **Incremental retrieval can skip messages — STILL UNFIXED.** The collector
   scans newest→oldest and keeps the newest `target` after the cursor
   (`history_service.py`, `_collect`). With 60 eligible messages after a cursor and
   `count=10`, it returns 51–60 and reports `truncated=True`, silently dropping
   1–50. This is a **prerequisite for scheduled processing** and was deliberately
   not changed in Phase 2. Correct catch-up needs oldest-first retrieval (Telethon
   `reverse=True`, not exposed by the facade today), a stable upper bound and
   exclusive cursors.
2. **`ai_task_occurrences.result_metadata` cannot store summaries.** Hard CHECK
   `octet_length(result_metadata::text) <= 8192`, repository
   `MAX_METADATA_BYTES = 8192`, and the column is owned by the occurrence's
   action/park/terminal lifecycle.
3. **Scheduled actions receive only `notification_destination.chat_id`.**
   `TaskExecutionCoordinator` injects it as `extra["chat_id"]` together with the
   `scheduled_occurrence` marker. That field is the delivery destination, so it
   cannot double as “the chat to read”.
4. **The deterministic owner path already exists.** The Taskloom wizard builds a
   `TaskCandidate` and persists through `TaskCreationService`; pause/resume/delete
   use `TaskManagementService`. AI-created tasks are a separate path.
5. **Pacing is per operation, not global.** `MAP_CONCURRENCY = 2` and 5 s spacing
   apply inside one History operation, while the scheduler runs up to four
   occurrences concurrently (`MAX_CONCURRENT_EXECUTIONS = 4`). No shared History
   admission exists.
6. **Recovery semantics are already honest.** A `running` occurrence becomes
   `failed/restart_side_effect_uncertain`; unstarted future occurrences are left
   untouched; claim CAS and deterministic `occurrence_key` prevent duplicates.
7. **Capacity arithmetic stands.** At 240–300 s, capacity is 12–16 calls, roughly
   352 Latin / 176 non-Latin messages of the observed size. A 300-message product
   allowance is not automatically feasible.

## 5. Phase 3 scope (next: schema/repository investigation and planning)

Investigate, then plan the implementation of a durable, owner-applied History
state table (peer, both cursors, bounded summary, revision, status, CAS version)
and its repository contract. No migration may be written or applied without the
owner's explicit approval, and `ai_task_occurrences.result_metadata` stays out of
summary storage. Phase 3 is investigation/planning first; it does not fix
retrieval or add scheduling by itself.

## 6. Unresolved for later phases

- Oldest-first window retrieval and the exact scanned-vs-summarized checkpoint
  contract, including windows with no eligible text. **Prerequisite for Phase 5.**
- Durable History state table and its manual migration; no SQL has been written or
  applied.
- One new registered ToolExecutor action with no model-supplied identifiers and an
  atomic summary + checkpoint commit.
- Shared History admission/pacing across manual and scheduled work.
- Notification/visibility of scheduled summaries, retention policy, and whether a
  peer may have more than one schedule.
