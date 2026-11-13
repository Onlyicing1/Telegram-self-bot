# IMPLEMENTATION REPORT — Peer Classification Phase 1 (reusable foundation)

Base commit: `ecc3779814f6bc0a45588cb35629c3aa5288550c` (`origin/main`, fetched and
verified before any edit). This report replaces the previous History capacity
report; that delivery remains in the repository history and its behaviour is
unchanged.

## 1. What this delivery is

One reusable, provider-independent Telegram peer classifier plus the migration of
the four duplicated classification sites onto it. It answers only *what kind of
peer a resolved Telethon entity is*. Feature policy (Ghost Seen eligibility,
Anti-Edit/Delete bot exclusion, History limits, ownership, authorization) stays
with each feature.

Nothing from later phases was started: no History caps or count changes, no
retrieval/cursor change, no persistence, no scheduling, no ToolExecutor action,
no database object, no provider or delivery change.

## 2. Files changed

| File | Change |
| --- | --- |
| `backend/telegram_api/peers.py` | **new** — `PeerType` + pure `classify_entity` |
| `backend/bot/handlers/history.py` | `_chat_kind` now uses the classifier (legacy double fallbacks kept) |
| `backend/bot/handlers/misc.py` | General → Chat & Message IDs type label now uses the classifier |
| `backend/services/ghost_seen_v2.py` | `_is_telegram_user` / `is_private_user_entity` use the classifier; Ghost Seen's own exclusions untouched |
| `backend/services/anti_edit_delete_service.py` | `is_bot_entity` uses the classifier; documented double fallback kept |
| `tests/test_peer_classification.py` | **new** — 41 classifier + call-site tests |
| `IMPLEMENTATION_REPORT.md`, `INVESTIGATION.md`, `ROADMAP.md` | this delivery's reports |

No other file was touched, and no protected document was modified.

## 3. Classifier contract

```python
class PeerType(str, Enum):
    PRIVATE_USER = "private_user"
    PRIVATE_BOT = "private_bot"
    BASIC_GROUP = "basic_group"
    SUPERGROUP = "supergroup"
    BROADCAST_CHANNEL = "broadcast_channel"
    UNKNOWN = "unknown"

def classify_entity(entity: Any) -> PeerType
```

- Real Telethon types only, decided with `isinstance`: `User`, `Chat`, `Channel`.
  Never `type(...).__name__`, never a Dialog boolean, never a title/username/id
  pattern, never a service or model answer.
- `User`: `PRIVATE_BOT` only when `User.bot is True`, otherwise `PRIVATE_USER`.
  An absent flag is not read as a positive one.
- `Chat`: `BASIC_GROUP`.
- `Channel`: `BROADCAST_CHANNEL` when `broadcast is True` (checked first, so a
  contradictory megagroup flag cannot turn a channel into a group); otherwise
  `SUPERGROUP` when `megagroup is True` **or** `gigagroup is True`; otherwise
  `UNKNOWN`. A gigagroup is a supergroup variant and deliberately has no separate
  policy type.
- `None`, unexpected objects, dialogs, serialized dicts, partial objects and
  non-Telethon doubles: all `UNKNOWN`. No attribute is read for an object that
  failed the `isinstance` gate (tested with an exploding object).
- Pure and synchronous: no RPC, network, database, service, History, AI, panel or
  scheduling import; no mutation; deterministic. It is a module function, not a
  client, resolver, cache or update loop.
- Peer type is never persisted here. It is derived from the currently resolved
  entity every time, so a basic group migrated to a supergroup is classified
  correctly on the next resolution.

No resolver wrapper was added: every call site already resolves through
`client.get_entity` (the existing boundary), so a second resolution path would be
redundant.

## 4. Call sites

### Migrated

1. **`history._chat_kind`** — the chat picker's label. Real entities now map
   `private_user`/`private_bot` → `private`, `basic_group` → `group`,
   `supergroup` → `supergroup`, `broadcast_channel` → `channel`. The display
   vocabulary is unchanged. The legacy dialog/attribute branches are retained
   **only** for the non-Telethon doubles the panel tests use and cannot upgrade a
   real entity the classifier reported as `UNKNOWN` (that keeps the legacy
   `chat` bucket). A real supergroup is no longer at risk of being labelled a
   channel, and a real basic group is no longer labelled `chat`.
2. **`misc._general_id_action`** — “General → Chat & Message IDs”. The old code
   checked `hasattr(entity, "broadcast")` first, so a **supergroup was displayed
   as “Group”** and the megagroup branch was unreachable for real channels. It
   now shows `Channel`, `Supergroup`, `Group`, `Private` or `Bot` from the
   classifier, and `Unknown` when the entity cannot be classified. Failure of the
   lookup still leaves `Unknown` (never a guessed category).
3. **`ghost_seen_v2`** — `_is_telegram_user` uses the classifier for real
   entities, then keeps its long-standing double heuristics (`class name "User"`,
   `SimpleNamespace` with `first_name`, `dialog.is_user`).
   `is_private_user_entity` keeps Ghost Seen's own policy unchanged: bots, self,
   deleted and the owner are excluded, and an entity id is required. Bot
   exclusion is taken from the classifier for real entities and remains
   attribute-based for doubles, so no existing behaviour was lost.
4. **`anti_edit_delete_service.is_bot_entity`** — a real entity is a bot only when
   the classifier says `PRIVATE_BOT`. The legacy class-name + `bot is True` branch
   remains for the module's `User` test doubles; it can only confirm a bot, never
   a human, and an unresolved/non-User entity still returns `False` (the safe
   fallback the capture handlers rely on).

### Intentionally not changed

- `is_private_user_dialog` / `filter_private_dialogs`: their group/channel dialog
  flags are a feature-level scope rule, not taxonomy; they only consume the
  already-migrated helpers.
- `anti_edit_delete_service._resolve_peer_bot` / its bounded cache and cooldown:
  that is resolution reliability and feature policy (unknown → cooldown, never a
  human), not classification.
- `telegram_api.entities.get_dialogs`' serialized dict: unchanged, because
  adding peer types to a display dict would create a second, weaker source of
  truth. Callers that need the type resolve the entity, as these four sites do.
- History retrieval, counts, panel text, AI capacity and delivery: untouched.
- `AI_MASTER_DESIGN.md` and `DATABASE_ARCHITECTURE.md`: protected, untouched.

## 5. Tests

New `tests/test_peer_classification.py` (41 tests, all passing):

- Vocabulary: exact six values, `str` compatibility, round-trip.
- Real entities: human `User`, `User(bot=False)`, `User` with an absent bot flag,
  bot `User`, basic `Chat`, broadcast `Channel`, megagroup `Channel`, explicit
  `gigagroup=True` (with and without `megagroup`), channel with neither flag,
  contradictory broadcast+megagroup.
- Negative: `None`, `0`, `"user"`, `object()`, `{}`, `[]`, `SimpleNamespace`
  doubles, dialog-like objects, partial flag-only objects, a `Channel` subclass
  with missing flags.
- Purity: not a coroutine, single `entity` parameter, deterministic, does not
  mutate the entity, reads no attribute for an unknown object, and the module
  source contains no async/runtime-service dependencies.
- Call sites: History labels for every real type plus the double fallbacks;
  General ID label for channel/supergroup/gigagroup/group/private/bot/unknown and
  a failed lookup; Ghost Seen human/bot/group/channel plus owner and double
  exclusions; Anti-Edit bot detection for real bot/human/group/channel and the
  legacy double.

Existing suites were run unchanged — no assertion was weakened or removed.

## 6. Exact results

Environment: `/tmp/iv` virtualenv from `backend/requirements.txt` plus `pytest`
and `pytest-asyncio`, `PYTHONPATH=.`, no network and no Telegram.

| Check | Result |
| --- | --- |
| `pytest tests/test_peer_classification.py tests/test_46_general_chat_ids.py tests/test_52_ghost_seen_v2_stage1.py tests/test_57_ghost_seen_v2_stage5_6.py tests/test_anti_edit_delete.py tests/test_history_panel.py tests/test_history_service.py` | **311 passed** |
| Full suite (`pytest -q -p no:cacheprovider`) | **7,659 passed, 6 failed, 27 skipped** in 174.6 s |
| The same 6 tests on a clean detached worktree at `ecc3779` | **6 failed** — identical list |
| `py_compile` on all six changed Python files | exit 0 |
| `git diff --check` | clean |

The six failures are pre-existing and unrelated (documentation/DB-security/schema
audits and one scheduler test); they reproduce unchanged at the base commit, so
no new regression was introduced.

## 7. Scope compliance

Not implemented in this phase: History per-peer caps, `history_policy.py`, count
choices, retrieval/cursor changes, pagination, AI capacity, scheduled History,
Taskloom changes, a new ToolExecutor action, any table/migration/SQL/Supabase
change, peer-type persistence or cache, a second client/scheduler/worker/
listener/executor/update loop, ProviderManager/routing/pacing changes, delivery
changes, unrelated refactors or API renames. Nothing was deployed.

## 8. Limitations and live status

- **No live Telegram validation**: no real dialogs were resolved against a live
  account; every test uses real Telethon *entity objects* constructed locally.
  The classifier's correctness therefore rests on Telethon's documented flags,
  which is the authority it reads.
- A `Channel` whose `broadcast` and `megagroup`/`gigagroup` flags are all unset
  is `UNKNOWN` by design; callers must fail closed rather than pick a category.
  There is no dashboard/UI consumer of `PeerType` yet.
- `str(PeerType.X)` prints `PeerType.X` (standard `Enum` behaviour); compare
  identity or use `.value`. The type is `str`-compatible for storage/JSON, which
  no code does in this phase.
- Later phases (policy, incremental cursors, durable schedules, admission) remain
  unimplemented and are described in `INVESTIGATION.md` / `ROADMAP.md`.
