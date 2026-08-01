# SQL Schema Scripts

Each file in this directory contains the `CREATE TABLE`, indexes, constraints,
and RLS policies for one database table. Run them in any order — they are
independent (no foreign keys exist between tables).

| File | Table | Purpose |
|---|---|---|
| `saved_items.sql` | `saved_items` | Media save records (forward + deep) |
| `bio_state.sql` | `bio_state` | Singleton-per-owner bio cron state |
| `username_state.sql` | `username_state` | Singleton-per-owner username cron state |
| `bot_logs.sql` | `bot_logs` | Structured activity log |
| `panel_settings.sql` | `panel_settings` | Global panel auto-close preference |

For the full schema reference, see [DATABASE_ARCHITECTURE.md](../DATABASE_ARCHITECTURE.md).
