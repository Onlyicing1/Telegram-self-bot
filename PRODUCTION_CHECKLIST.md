# Production Deployment Checklist — LifeOS Telegram Self-Bot

> Run through this checklist before **every** production deployment.
> Each item must be verified or explicitly marked as N/A.

---

## 1. Environment Variables

- [ ] `API_ID` — set, positive integer (from my.telegram.org)
- [ ] `API_HASH` — set, non-empty string
- [ ] `SESSION_STRING` — set, valid Telethon StringSession (≥50 chars)
- [ ] `BOT_OWNER_ID` — set, positive integer (your Telegram user ID)
- [ ] `BOT_TOKEN` — set if Inline Glass UI is needed; empty otherwise
- [ ] `TZ` — set (default: `Asia/Tehran`)
- [ ] `PORT` — set by Render automatically
- [ ] `LOG_LEVEL` — set (default: `INFO`)
- [ ] `BIO_UPDATE_ENABLED` — set to `true` only if auto-start is desired
- [ ] `GHOST_ROOM_ID` — set if Ghost Room feature is used
- [ ] `DEST_CHANNEL_ID` — set if destination channel feature is used

## 2. Supabase

- [ ] `SUPABASE_URL` — set, valid project URL
- [ ] `SUPABASE_SERVICE_ROLE_KEY` — set, valid service role key
- [ ] Core tables exist: `saved_items`, `bio_state`, `bot_logs`, `username_state`, `panel_settings`
- [ ] AI tables exist: `ai_sessions`, `ai_messages`, `ai_memories`, `ai_tool_history`
- [ ] RLS enabled on all tables
- [ ] SELECT policies granted to `anon` + `authenticated`
- [ ] No write policies for `anon`/`authenticated` (writes via service-role key only)
- [ ] `panel_settings` singleton row exists (key = `"global"`)
- [ ] Connection test passes (startup check logs "Supabase reachable")

## 3. AI Configuration

- [ ] `AI_ENABLED` — set to `true` to activate AI subsystem
- [ ] `AI_PROVIDER` — set to active provider name (e.g. `gemini`, `openai`, `dummy`)
- [ ] At least one provider API key configured (e.g. `AI_GEMINI_API_KEY`)
- [ ] `AI_PROVIDER_FALLBACK` — comma-separated fallback chain (e.g. `gemini,openai,dummy`)
- [ ] `AI_MODEL` — set or using provider default
- [ ] `AI_TEMPERATURE` — in [0.0, 2.0]
- [ ] `AI_MAX_TOKENS` — positive integer
- [ ] `AI_TIMEOUT` — positive integer (seconds)
- [ ] `AI_RETRY_COUNT` — non-negative integer
- [ ] Dummy provider always available as last-resort fallback
- [ ] Provider health check passes (startup check logs provider list)

## 4. Diagnostics

- [ ] Diagnostics event ring functional (`.logs` command works)
- [ ] `.kill` diagnostic snapshot produces output
- [ ] `.health` dashboard shows all subsystems
- [ ] No stale loops detected (check health snapshot)
- [ ] Memory cleanup worker started (check logs for "Memory cleanup worker started")

## 5. Runtime

- [ ] RuntimeSupervisor reaches `READY` state
- [ ] No duplicate supervisor instances
- [ ] Supervisor healthy (health check shows `supervisor_ok: true`)
- [ ] Heartbeat running (health check shows `process_alive: true`)
- [ ] Keepalive running (RPC latency reported in health)
- [ ] Failsafe monitor running
- [ ] Diagnostics loop running
- [ ] All immortal tasks wrapped (crashes don't kill the process)
- [ ] Signal handlers installed (SIGTERM/SIGINT trigger clean shutdown)

## 6. Telegram

- [ ] Self-bot client connects and authorizes
- [ ] Helper bot connects (if `BOT_TOKEN` set)
- [ ] `flood_sleep_threshold=60` configured
- [ ] `auto_reconnect=True` configured
- [ ] `connection_retries=5` configured
- [ ] All command handlers registered (check logs for "registered OK")
- [ ] Inline callback handlers registered (if helper bot active)
- [ ] No unbounded Telegram RPC awaits (all use bounded timeouts)
- [ ] FloodWait handling in bio/username scheduler
- [ ] Centralized retry utility available (`backend.runtime.tg_retry`)

## 7. Resource Usage (Render Free)

- [ ] Memory < 512 MB (check `.health` dashboard)
- [ ] No orphaned asyncio tasks (check diagnostics dump)
- [ ] No duplicate timers (check `lifecycle.timer_count`)
- [ ] No duplicate schedulers (single profile scheduler instance)
- [ ] BytesIO buffers closed in `finally` blocks (deep save)
- [ ] httpx clients closed on shutdown (provider `shutdown()`)
- [ ] Conversation history bounded (max 20 entries per session)
- [ ] Idle sessions auto-cleaned (30-minute timeout)
- [ ] Memory cleanup worker purges expired memories every 6 hours
- [ ] No fire-and-forget `asyncio.ensure_future` calls (use `guarded_create_task`)

## 8. Health Checks

- [ ] `/health` endpoint returns 200
- [ ] Unified health snapshot includes all subsystems
- [ ] `overall_healthy` field reflects true state
- [ ] Health endpoint does not block the event loop
- [ ] Startup validation ran and passed (check logs)

## 9. Startup Validation

- [ ] `run_startup_checks()` executed before bot becomes operational
- [ ] All CRITICAL checks passed (env vars, session, core tables, directories)
- [ ] WARNING checks logged (Supabase optional, AI tables optional, ghost room optional)
- [ ] If any CRITICAL check fails → process exits cleanly (no partial start)

## 10. Deployment Verification

- [ ] `npm run build` passes (frontend compiles)
- [ ] `dist/` directory exists (if serving dashboard)
- [ ] `python -m backend.main` starts without errors
- [ ] Health check returns `"status": "ok"` within 60 seconds
- [ ] Send `.ping` in Telegram → receives `PONG`
- [ ] Send `.health` in Telegram → dashboard renders
- [ ] Send `.save f` (replying to a message) → save succeeds
- [ ] Send `.bio show` → bio state displays
- [ ] Send `.ai hello` → AI responds (or dummy placeholder)
- [ ] No errors in Render logs during first 5 minutes
- [ ] Process survives Render's 15-minute idle sleep (if applicable)

---

## Post-Deployment Monitoring

- [ ] Check Render logs for `[TRACE]` events — no unexpected errors
- [ ] Check `[STARTUP CHECK]` log lines — all critical checks passed
- [ ] Monitor memory usage over first hour — no upward trend
- [ ] Monitor RPC latency — should stay under 1000ms
- [ ] Verify recovery was not unexpectedly triggered (check `restart_count`)

## Rollback Plan

If deployment fails:
1. Render automatically restarts on `sys.exit(1)` (supervisor exhaustion)
2. Manually revert the deploy in Render dashboard
3. Check `SESSION_STRING` validity — regenerate if expired
4. Verify Supabase migrations are applied
5. Re-deploy after fixing the root cause

---

_This checklist is the single source of truth for deployment readiness.
Update it when new subsystems or checks are added._
