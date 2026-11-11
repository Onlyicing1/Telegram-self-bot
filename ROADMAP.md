# ROADMAP

## Current task: Persistent outbound bandwidth governor

Status: Implemented (pending live verification).

### Objective
Prevent high-volume outbound media transfers from exhausting Render's monthly
outbound bandwidth by enforcing persistent, fail-closed daily (150,000,000 bytes)
and monthly (4,000,000,000 bytes) UTC budgets.

### Execution state
- Core governor implemented in `backend/services/outbound_bandwidth_service.py`.
- Persistent state stored in `panel_settings.outbound_bandwidth_state` JSONB column.
- Repository access added through existing `panel_settings_repository`.
- Idempotent migration prepared: `supabase/migrations/20261112000000_add_panel_settings_outbound_bandwidth.sql`.
- Integrations: Gemini OCR/STT, Telegram `send_voice`, Deep Save re-uploads.
- Telegram downloads remain uncounted (inbound).
- AI per-owner request limiter audited and implemented at 10 requests/minute.
- ProviderManager default concurrency left at 4.

### Not yet verified live
- Supabase migration application.
- Render/deployment behavior.
- Live Telegram media transfers.
