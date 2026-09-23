"""
AI Config Store — persists user AI configuration.

Stores the user's selected provider, model, temperature, max_tokens,
and other settings. Uses Supabase when available, with an in-memory
fallback so the AI config survives across callbacks even when the
database is unreachable.

The in-memory fallback is a DOCUMENTED DEGRADATION and never a durable store:
a value it serves is labelled through ``SESSION_ONLY_KEY``, and the settings that
arrive as their own additive migration (the Text-to-Speech trio,
``TTS_STORAGE_KEYS``) are written in their own statement so a database without
those columns can only reject that one write.

All operations are async and use asyncio.to_thread with bounded
timeouts, matching the pattern in backend/db/client.py.
"""
from __future__ import annotations

import asyncio
import logging
from datetime import datetime, timezone
from typing import Any

logger = logging.getLogger(__name__)

_DB_TIMEOUT = 10.0

#: How many times a failed durable read is retried before it is reported as
#: failed. A transient failure (contention, ``EAGAIN``, thread/socket pressure)
#: must never be reported as if the database had said "no row".
_READ_ATTEMPTS = 2

#: Present on a returned config ONLY when the durable ``ai_config`` row could
#: not be read AND no in-process value is known: the stored state is UNKNOWN,
#: so a consumer that displays durable state must not present the compiled
#: defaults as if the database had reported them (that is how a persisted
#: ``show_question = true`` silently became an effective ``false`` after a
#: restart, when the in-memory fallback is empty). Never persisted —
#: ``_save_config_sync`` builds an explicit column payload — and never part of
#: ``_DEFAULTS``, so it can never be written back as a real value.
DEGRADED_READ_KEY = "durable_read_failed"

#: Present on a returned config ONLY when the durable row was read successfully
#: but does not CARRY some keys, so their value can only come from this process's
#: in-memory fallback. Those values stay visible (the documented in-memory
#: degradation) yet are explicitly labelled, so no consumer — and no surface that
#: displays durable state — can mistake RAM for the database. Absent whenever
#: every key came from the row, so an all-durable config is unchanged. Never
#: persisted (``_save_config_sync`` and ``_save_tts_sync`` build explicit column
#: payloads) and never part of ``_DEFAULTS``, so it can never be written back as
#: a real value.
SESSION_ONLY_KEY = "session_only_keys"

_DEFAULTS: dict[str, Any] = {
    "provider": "",
    "model": "",
    "temperature": 1.0,
    "max_tokens": 4096,
    "system_prompt": "",
    "history_budget": 4000,
    "is_configured": False,
    "trigger_en": "Nova",
    "trigger_fa": "",
    "show_question": False,
    #: Speech-to-text behavior (AI -> Media Analysis -> Speech-to-Text). These are
    #: the owner's persisted values and the ONLY source the media engine is told
    #: about; the empty/one defaults are themselves meaningful, so "not
    #: configured" and "the default" behave identically by design.
    #: ``stt_model`` now holds the REGISTERED control-plane candidate the owner
    #: picked (``backend/ai/stt_control_plane.py``), not a typed model id: empty =
    #: the default candidate (the general media model), a registered candidate id
    #: = that candidate, anything else = legacy/unresolved, which is passed
    #: through to the engine unchanged rather than re-pointed at another model.
    #: Language empty = automatic detection; passes one = the single-pass route.
    "stt_model": "",
    "stt_language": "",
    "stt_passes": 1,
    #: Text-to-speech behavior (AI -> Media Analysis -> Text-to-Speech). The
    #: REGISTERED provider/model/voice triple the owner picked
    #: (``backend/ai/tts_control_plane.py``), stored exactly like ``stt_model``:
    #: the DEFAULT selection is stored as the empty string, so "nothing
    #: configured" and "the default" stay one state. An empty triple is itself
    #: meaningful (the default provider, its default model, that model's default
    #: voice), and an unusable stored combination is degraded deterministically by
    #: the control plane rather than left stale.
    "tts_provider": "",
    "tts_model": "",
    "tts_voice": "",
}

_fallback_config: dict[int, dict[str, Any]] = {}

#: The three ``ai_config`` keys that persist the owner's Text-to-Speech selection
#: (``backend/ai/tts_control_plane.py``). They are written by their OWN statement
#: instead of inside the shared payload on purpose: they arrive as a separate
#: additive migration, so on a database where that migration has not been applied
#: PostgREST rejects any payload naming them — and a SHARED payload would take
#: every other AI setting in the same save down with it. Isolating the trio bounds
#: that rejection to the trio, where it is reported instead of silently losing the
#: owner's provider, model, triggers and STT settings too.
#: ``tests/test_tts_settings_persistence.py`` pins the agreement with
#: ``tts_control_plane.STORAGE_KEYS`` so the two can never drift.
TTS_STORAGE_KEYS: tuple[str, str, str] = ("tts_provider", "tts_model", "tts_voice")


def _tts_values(config: dict[str, Any]) -> dict[str, Any]:
    """The trio's column values — the writer's existing empty-string → NULL rule.

    Empty is the DEFAULT selection (the default provider, its default model, that
    model's default voice), so "nothing configured" and "the default" stay one
    stored state rather than two.
    """
    return {key: str(config.get(key) or "").strip() or None for key in TTS_STORAGE_KEYS}


def _get_db():
    from backend.db.client import get_db
    return get_db()


async def _run_sync(fn, *args, **kwargs):
    return await asyncio.wait_for(
        asyncio.to_thread(fn, *args, **kwargs),
        timeout=_DB_TIMEOUT,
    )


def _is_missing_config_response(exc: Exception) -> bool:
    return (
        str(getattr(exc, "code", "")) == "204"
        and getattr(exc, "message", "") == "Missing response"
    )


def _get_config_sync(owner_id: int) -> tuple[dict[str, Any] | None, bool]:
    """Read the owner's durable ``ai_config`` row → ``(row, read_failed)``.

    ``row is None`` with ``read_failed=False`` means the database
    authoritatively reported NO row for this owner — never that a read
    failed. A failed read is retried (``_READ_ATTEMPTS``) so transient
    contention cannot masquerade as a stored value; if every attempt fails it
    is reported as ``read_failed=True`` together with the last value this
    process actually knows (the in-memory fallback, or ``None``).
    """
    db = _get_db()
    if not db:
        logger.info("[AI_CONFIG] DB unavailable — using fallback for owner_id=%s", owner_id)
        return _fallback_config.get(owner_id), False
    last_exc: Exception | None = None
    for _attempt in range(_READ_ATTEMPTS):
        try:
            result = db.table("ai_config").select("*").eq("owner_id", owner_id).maybe_single().execute()
            return (result.data if result else None), False
        except Exception as exc:
            if _is_missing_config_response(exc):
                return None, False
            last_exc = exc
    logger.warning(
        "[AI_CONFIG] durable read FAILED for owner_id=%s after %d attempt(s): %s — "
        "durable state unknown (NOT reporting defaults as stored values)",
        owner_id, _READ_ATTEMPTS, last_exc,
    )
    return _fallback_config.get(owner_id), True


async def get_config(owner_id: int) -> dict[str, Any]:
    """Get the AI config for an owner. Returns defaults if not found.

    A stored row always wins for every key the row actually carries. When the
    durable read FAILS and this process knows no value for the owner, the result
    carries ``DEGRADED_READ_KEY`` so callers can tell "the database could not be
    read" apart from "the row says the default" — a persisted preference must
    never be silently downgraded to the compiled default by a read error.

    A key the row does NOT CARRY is a different case, and the one that bound a
    selected Text-to-Speech provider to OpenAI: the owner's ``ai_config`` row
    exists, but this deployment has no column for that setting yet, so the write
    was rejected and the value reached only this process. Reporting the compiled
    default there silently discards an explicit owner selection, and a surface
    that re-reads the store then disagrees with the notice it just showed. So a
    key absent from an EXISTING row falls back to what this process last wrote,
    then to the compiled default — the documented in-memory degradation, applied
    where it actually applies. With no row at all there is no owner state to
    preserve, and the compiled default remains the answer.
    """
    local = _fallback_config.get(owner_id) or {}
    try:
        row, read_failed = await _run_sync(_get_config_sync, owner_id)
        if row:
            merged = {
                k: (row[k] if k in row else local.get(k, v))
                for k, v in _DEFAULTS.items()
            }
            # A key the row does not carry can only be answered from this
            # process's RAM — there is nothing durable behind it — so the value
            # stays visible AND is reported as session-only. That is the whole
            # difference between a stored setting and a value that merely looks
            # stored until the next restart.
            session_only = tuple(k for k in _DEFAULTS if k not in row and k in local)
            if session_only:
                merged[SESSION_ONLY_KEY] = session_only
                logger.warning(
                    "[AI_CONFIG] get_config owner_id=%s: %d key(s) are session-only "
                    "(the row does not carry them): %s",
                    owner_id, len(session_only), ",".join(session_only),
                )
            logger.info("[AI_CONFIG] get_config OK owner_id=%s provider='%s' model='%s'", owner_id, merged.get("provider", ""), merged.get("model", ""))
            return merged
        if read_failed:
            logger.warning(
                "[AI_CONFIG] get_config owner_id=%s → defaults after a FAILED durable read "
                "(stored state unknown)",
                owner_id,
            )
            degraded = dict(_DEFAULTS)
            degraded[DEGRADED_READ_KEY] = True
            return degraded
    except Exception as exc:
        logger.warning("[AI_CONFIG] get_config failed for owner_id=%s: %s", owner_id, exc)
    logger.info("[AI_CONFIG] get_config → defaults owner_id=%s", owner_id)
    return dict(_DEFAULTS)


def _save_config_sync(owner_id: int, config: dict[str, Any]) -> bool:
    db = _get_db()
    if not db:
        logger.info("[AI_CONFIG] DB unavailable — saving to fallback owner_id=%s provider='%s'", owner_id, config.get("provider", ""))
        _fallback_config[owner_id] = dict(config)
        return True
    try:
        existing = db.table("ai_config").select("id").eq("owner_id", owner_id).maybe_single().execute()
        payload = {
            "owner_id": owner_id,
            "provider": config.get("provider", ""),
            "model": config.get("model", ""),
            "temperature": config.get("temperature", 1.0),
            "max_tokens": config.get("max_tokens", 4096),
            "system_prompt": config.get("system_prompt", ""),
            "history_budget": config.get("history_budget", 4000),
            "is_configured": config.get("is_configured", False),
            "trigger_en": config.get("trigger_en", "") or None,
            "trigger_fa": config.get("trigger_fa", "") or None,
            "show_question": bool(config.get("show_question", False)),
            # STT behavior: an unset/empty selection or language is stored as NULL
            # (the writer's existing "empty string -> NULL" convention), and the
            # passes value stays an integer because 1 is a real, meaningful
            # value rather than an absence marker. The column set is unchanged:
            # the control plane reuses these three keys, so no schema change.
            "stt_model": str(config.get("stt_model") or "").strip() or None,
            "stt_language": str(config.get("stt_language") or "").strip() or None,
            "stt_passes": int(config.get("stt_passes") or _DEFAULTS["stt_passes"]),
            # TTS behavior is deliberately ABSENT here: the trio is written in its
            # own statement below (``_save_tts_sync`` / ``TTS_STORAGE_KEYS``), so
            # this payload stays writable on a database whose ``ai_config``
            # predates the TTS migration.
            "last_request_at": config.get("last_request_at") or None,
            "last_latency_ms": config.get("last_latency_ms", 0),
            "updated_at": datetime.now(timezone.utc).isoformat(),
        }
        if existing and existing.data:
            db.table("ai_config").update(payload).eq("owner_id", owner_id).execute()
        else:
            payload["created_at"] = datetime.now(timezone.utc).isoformat()
            db.table("ai_config").insert(payload).execute()
        # The TTS trio rides in its own statement, never in ``payload`` above, so
        # a schema without those columns rejects only the trio. This return value
        # keeps its exact meaning — the durable ``ai_config`` ROW was written —
        # because that is what every other caller's honesty depends on (a toggle
        # that reports "not saved" while the row really was written would be its
        # own lie). The trio's own durability is reported by ``save_tts_settings``
        # and, on the read path, by ``SESSION_ONLY_KEY``.
        if any(key in config for key in TTS_STORAGE_KEYS):
            if not _save_tts_sync(owner_id, config):
                logger.warning(
                    "[AI_CONFIG] save_config owner_id=%s: the ai_config row was written, "
                    "but the TTS selection was NOT stored — it stays session-only",
                    owner_id,
                )
        _fallback_config[owner_id] = dict(config)
        logger.info("[AI_CONFIG] save_config OK owner_id=%s provider='%s' model='%s'", owner_id, payload["provider"], payload["model"])
        return True
    except Exception as exc:
        # The durable row was NOT updated. The in-memory fallback keeps the
        # value available for this process (documented degradation), but this
        # must be reported as a FAILED save: callers that re-read the config
        # (which prefers the DB) would otherwise render a state the durable
        # store never accepted — e.g. a Settings toggle that "doesn't toggle".
        logger.warning("[AI_CONFIG] DB save failed for owner_id=%s: %s — fallback only (NOT durable)", owner_id, exc)
        _fallback_config[owner_id] = dict(config)
        return False


async def save_config(owner_id: int, config: dict[str, Any]) -> bool:
    """Save AI config for an owner. Upserts the row.

    Returns True only when the durable ``ai_config`` row was written. When
    the write fails (or no DB is available) the value is kept in the
    in-memory fallback for this process and False is returned, so callers
    can honestly distinguish durable persistence from temporary RAM state.
    """
    try:
        return await _run_sync(_save_config_sync, owner_id, config)
    except Exception as exc:
        logger.warning("[AI_CONFIG] save_config failed for owner_id=%s: %s — fallback only (NOT durable)", owner_id, exc)
        _fallback_config[owner_id] = dict(config)
        return False


def _save_tts_sync(owner_id: int, config: dict[str, Any]) -> bool:
    """Write ONLY the TTS trio, in ONE statement, on the owner's ``ai_config`` row.

    Never names another column, so this is the one write a database without the
    TTS columns can reject without touching anything else in the row. Returns
    whether the durable row accepted it.
    """
    db = _get_db()
    if not db:
        logger.info(
            "[AI_CONFIG] DB unavailable — TTS selection not stored owner_id=%s (NOT durable)",
            owner_id,
        )
        return False
    payload = _tts_values(config)
    payload["updated_at"] = datetime.now(timezone.utc).isoformat()
    try:
        existing = db.table("ai_config").select("id").eq("owner_id", owner_id).maybe_single().execute()
        if existing and existing.data:
            db.table("ai_config").update(payload).eq("owner_id", owner_id).execute()
        else:
            payload["owner_id"] = owner_id
            payload["created_at"] = datetime.now(timezone.utc).isoformat()
            db.table("ai_config").insert(payload).execute()
        return True
    except Exception as exc:
        logger.warning(
            "[AI_CONFIG] TTS settings write REJECTED for owner_id=%s: %s — the trio is "
            "session-only until the ai_config TTS columns exist "
            "(20260923000001_add_ai_config_tts_settings.sql)",
            owner_id, exc,
        )
        return False


async def save_tts_settings(owner_id: int, provider: str, model: str, voice: str) -> bool:
    """Persist the Text-to-Speech selection trio, and nothing else.

    ONE atomic statement carrying exactly ``tts_provider``/``tts_model``/
    ``tts_voice`` (plus ``updated_at``), so the stored triple is always one valid
    combination rather than three independently-racing keys, and so a schema
    without those columns can only reject THIS write.

    Returns True only when the durable row accepted the write. False means the
    values are session-only: still visible to this process through the in-memory
    fallback (the documented degradation), reported by ``get_config`` through
    ``SESSION_ONLY_KEY``, and lost on restart. RAM is never presented as durable
    state by this call.
    """
    config = dict(zip(TTS_STORAGE_KEYS, (provider, model, voice)))
    try:
        saved = await _run_sync(_save_tts_sync, owner_id, config)
    except Exception as exc:
        logger.warning(
            "[AI_CONFIG] save_tts_settings failed for owner_id=%s: %s — fallback only (NOT durable)",
            owner_id, exc,
        )
        saved = False
    _fallback_config.setdefault(owner_id, {}).update(config)
    return saved


async def update_provider(owner_id: int, provider: str, model: str = "") -> bool:
    """Update just the provider and model."""
    config = await get_config(owner_id)
    config["provider"] = provider
    if model:
        config["model"] = model
    config["is_configured"] = True
    return await save_config(owner_id, config)


async def update_model(owner_id: int, model: str) -> bool:
    """Update just the model."""
    config = await get_config(owner_id)
    config["model"] = model
    return await save_config(owner_id, config)


async def update_setting(owner_id: int, key: str, value: Any) -> bool:
    """Update a single setting."""
    config = await get_config(owner_id)
    config[key] = value
    return await save_config(owner_id, config)


def _record_request_sync(owner_id: int, latency_ms: float) -> bool:
    db = _get_db()
    if not db:
        return False
    payload = {
        "last_request_at": datetime.now(timezone.utc).isoformat(),
        "last_latency_ms": latency_ms,
        "updated_at": datetime.now(timezone.utc).isoformat(),
    }
    try:
        db.table("ai_config").update(payload).eq("owner_id", owner_id).execute()
        return True
    except Exception as exc:
        logger.warning("[AI_CONFIG] record_request DB update failed for owner_id=%s: %s", owner_id, exc)
        return False


async def record_request(owner_id: int, latency_ms: float) -> bool:
    """Record request telemetry via a targeted update (NOT a full config rewrite).

    Normal inference must never rewrite the user's AI configuration (provider,
    model, system prompt, triggers). This writes only the latency stats columns
    so the dashboard still sees request activity without persisting config on
    every AI message. If the owner has no config row yet, this is a no-op.
    """
    try:
        return await _run_sync(_record_request_sync, owner_id, latency_ms)
    except Exception as exc:
        logger.warning("[AI_CONFIG] record_request failed for owner_id=%s: %s", owner_id, exc)
        return False


async def is_configured(owner_id: int) -> bool:
    """Check if the user has completed the setup wizard."""
    config = await get_config(owner_id)
    return config.get("is_configured", False)


def validate_triggers(trigger_en: str, trigger_fa: str) -> tuple[bool, str]:
    """Validate trigger word configuration.

    Rules:
      - Both fields are optional individually.
      - At least one must be non-empty.
      - The two values must not be identical (case-insensitive).
      - Triggers must be single words (no spaces).

    Returns (is_valid, error_message).
    """
    en = (trigger_en or "").strip()
    fa = (trigger_fa or "").strip()

    if not en and not fa:
        return False, "At least one trigger word is required."

    if en and " " in en:
        return False, "English trigger must be a single word (no spaces)."

    if fa and " " in fa:
        return False, "Persian trigger must be a single word (no spaces)."

    if en and fa and en.lower() == fa.lower():
        return False, "English and Persian triggers must be different values."

    return True, ""


async def update_triggers(owner_id: int, trigger_en: str, trigger_fa: str) -> tuple[bool, str]:
    """Update trigger words with validation.

    Returns (success, message).
    """
    is_valid, error = validate_triggers(trigger_en, trigger_fa)
    if not is_valid:
        return False, error

    config = await get_config(owner_id)
    config["trigger_en"] = (trigger_en or "").strip()
    config["trigger_fa"] = (trigger_fa or "").strip()
    ok = await save_config(owner_id, config)
    if ok:
        return True, "✅ Triggers updated."
    return False, "❌ Failed to save triggers."


async def get_triggers(owner_id: int) -> dict[str, str]:
    """Return the configured trigger words for an owner."""
    config = await get_config(owner_id)
    return {
        "trigger_en": config.get("trigger_en", "") or "",
        "trigger_fa": config.get("trigger_fa", "") or "",
    }


def match_trigger(first_word: str, trigger_en: str, trigger_fa: str) -> bool:
    """Check if the first word of a message matches either trigger.

    Rules:
      - English trigger: case-insensitive comparison.
      - Persian trigger: exact comparison.
      - Empty triggers never match.
    """
    if not first_word:
        return False

    en = (trigger_en or "").strip()
    fa = (trigger_fa or "").strip()

    if en and first_word.lower() == en.lower():
        return True

    if fa and first_word == fa:
        return True

    return False
