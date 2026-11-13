# INVESTIGATION — Peer classification and the History intelligence roadmap

Base: `ecc3779814f6bc0a45588cb35629c3aa5288550c`. Phase 1 is implemented
(`backend/telegram_api/peers.py`); later phases are recorded here as unresolved
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

## 2. Phase 1 decisions (implemented)

1. **Pure classifier, no resolver wrapper.** `classify_entity(entity)` takes a
   resolved entity and performs no RPC. Every call site already resolves through
   `client.get_entity`, so a second resolution boundary would be redundant.
2. **Strict flag precedence.** `User.bot` only; `Channel.broadcast` first, then
   `megagroup`/`gigagroup`; `Chat` is a basic group; everything else `UNKNOWN`.
   A gigagroup is a supergroup variant with no separate policy type.
3. **No persistence and no authoritative cache.** A peer type is derived from the
   currently resolved entity every time. This avoids a stale type after Telegram
   migrates a basic group to a supergroup. In-process caches that already exist
   elsewhere (Anti-Edit's resolution reliability cache) remain feature policy and
   were not turned into a type source.
4. **Feature policy stays with the feature.** Ghost Seen keeps owner/self/deleted
   exclusion; Anti-Edit keeps its safe `False` fallback for unresolved entities;
   History keeps its label vocabulary and panel behaviour.
5. **Compatibility is explicit.** Non-Telethon `User`/`SimpleNamespace` doubles
   that existing tests use keep the legacy branches in the call sites. The shared
   classifier itself never accepts a duck-typed object.
6. **No new architecture.** No client, listener, update loop, scheduler, service,
   database object, provider or delivery change.

## 3. Verified facts for later phases (read at this base, not implemented)

1. **Incremental retrieval can skip messages.** `history_service._collect` scans
   newest→oldest and keeps the newest `target` (`history_service.py:295`). With
   60 eligible messages after a cursor and `count=10`, it returns the
   newest ten (51–60) and reports `truncated=True`, silently dropping 1–50. The
   existing test only covers a range smaller than the requested count. Correct
   catch-up needs oldest-first retrieval (Telethon `reverse=True`, not exposed by
   the facade today), a stable upper bound, and exclusive cursors.
2. **`ai_task_occurrences.result_metadata` cannot store summaries.** Hard CHECK
   `octet_length(result_metadata::text) <= 8192`, repository
   `MAX_METADATA_BYTES = 8192`, and the column is owned by the occurrence's
   action/park/terminal lifecycle.
3. **Scheduled actions receive only `notification_destination.chat_id`.**
   `TaskExecutionCoordinator` injects it as `extra["chat_id"]` together with the
   `scheduled_occurrence` marker. That field is the delivery destination, so it
   cannot double as “the chat to read” without sending summaries into the
   monitored chat.
4. **The deterministic owner path already exists.** The Taskloom wizard builds a
   `TaskCandidate` and persists through `TaskCreationService`; pause/resume/delete
   use `TaskManagementService`. AI-created tasks are a separate path.
5. **Pacing is per operation, not global.** `MAP_CONCURRENCY = 2` and 5 s spacing
   apply inside one History operation, while the scheduler runs up to four
   occurrences concurrently (`MAX_CONCURRENT_EXECUTIONS = 4`). The dispatcher's
   10 requests/minute limiter covers interactive AI requests, not History map
   loops. No shared History admission exists.
6. **Recovery semantics are already honest.** A `running` occurrence becomes
   `failed/restart_side_effect_uncertain`; unstarted future occurrences are left
   untouched; claim CAS and deterministic `occurrence_key` prevent duplicates.
7. **Capacity arithmetic stands.** At 240–300 s, capacity is 12–16 calls, roughly
   352 Latin / 176 non-Latin messages of the observed size. 300 messages is not
   automatically feasible.

## 4. Phase 2 scope (next, not started)

`history_policy` mapping `PeerType` → product caps (channel 300, bot 300, user
1000, basic group 1000, supergroup 1000, unknown refuses), resolved before the
count is applied; no silent clamping; explicit over-cap explanation; the AI
feasibility gate remains independent; panel count choices reflect the cap without
changing retrieval, cursors, pagination or delivery.

## 5. Unresolved for later phases

- Durable History state table (peer, both cursors, bounded summary, revision,
  status, CAS version) and its manual migration; no SQL was written or applied.
- Oldest-first window retrieval and the exact scanned-vs-summarized checkpoint
  contract, including windows with no eligible text.
- One new registered ToolExecutor action with no model-supplied identifiers and
  an atomic summary + checkpoint commit.
- Shared History admission/pacing across manual and scheduled work.
- Notification/visibility of scheduled summaries, retention policy, and whether a
  peer may have more than one schedule.
