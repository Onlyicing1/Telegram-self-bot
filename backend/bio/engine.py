"""
Bio Engine — renders the Telegram profile bio ("about") using a template.

This is a thin wrapper over the shared ``ProfileEngine``
(``backend.profile.engine``). Bio controls ONLY the "about" field and its own
``bio_state`` table. It shares the single per-minute Profile Scheduler with the
Username engine.

Public interface preserved for callers:
- ``render_bio(template, mood, text, tz_str)``
- ``start_cron(client, owner_id, tz_str)``
- ``update_client(client)``
- ``stop_cron()`` (async)
- ``is_running()``
"""

from backend.db import client as db_client
from backend.profile.engine import ProfileEngine

_engine = ProfileEngine(
    name="bio",
    field="about",
    state_key="last_bio",
    default_template="🕒 {time} | 💭 {mood}",
    get_state=db_client.get_bio_state,
    update_state=db_client.update_bio_state,
)

render_bio = _engine.render
_get_tz = _engine.get_tz
_bio_updater = _engine.updater
start_cron = _engine.start_cron
update_client = _engine.update_client
stop_cron = _engine.stop_cron
is_running = _engine.is_running
