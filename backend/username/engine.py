"""
Username Engine — renders the Telegram first_name using a template.

This is a thin wrapper over the shared ``ProfileEngine``
(``backend.profile.engine``). Username controls ONLY the "first_name" field and
its own ``username_state`` table. It shares the single per-minute Profile
Scheduler with the Bio engine.

Public interface preserved for callers:
- ``render_username(template, mood, text, tz_str)``
- ``start_cron(client, owner_id, tz_str)``
- ``update_client(client)``
- ``stop_cron()`` (async)
- ``is_running()``
"""

from backend.db import client as db_client
from backend.profile.engine import ProfileEngine

_engine = ProfileEngine(
    name="username",
    field="first_name",
    state_key="last_name",
    default_template="{time} | {mood}",
    get_state=db_client.get_username_state,
    update_state=db_client.update_username_state,
)

render_username = _engine.render
_get_tz = _engine.get_tz
_username_updater = _engine.updater
start_cron = _engine.start_cron
update_client = _engine.update_client
stop_cron = _engine.stop_cron
is_running = _engine.is_running
