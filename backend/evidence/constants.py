"""Central owner-level Evidence policy (G4-A/F1).

These are account-wide safety ceilings, not per-Workspace configuration.  The
owner selects only the Active Run TTL at creation; every other value here is a
fixed policy boundary.
"""
from __future__ import annotations

DEFAULT_TTL_DAYS = 7
MIN_TTL_DAYS = 1
MAX_TTL_DAYS = 30

MAX_EXCERPT_CHARS = 1_000
MAX_REFS_PER_RUN = 50
MAX_REFS_PER_FOLLOWUP = 20
MAX_TEXT_BYTES_PER_RUN = 32 * 1024
MAX_ACTIVE_RUNS_PER_OWNER = 20

MAX_ARCHIVE_RECORDS_PER_OWNER = 5_000
MAX_ARCHIVE_BYTES_PER_OWNER = 50 * 1024 * 1024

# A generated summary is bounded, but it is not a second evidence store.  The
# same 32 KiB ceiling used by scheduled History summaries keeps one run row
# finite without silently truncating model output.
MAX_SUMMARY_BYTES = 32 * 1024
MAX_SUMMARY_CHARS = 16_000
MAX_PEER_NAME_CHARS = 128

# One Cited Summary request is one bounded single-source run.
DEFAULT_CITED_COUNT = 50
MAX_CITED_COUNT = MAX_REFS_PER_RUN

MAX_ARCHIVE_SUGGESTIONS = 10

# Provider calls are bounded by the caller's request budget.  This is a local
# fallback only when a direct service caller supplies no budget.
DEFAULT_PROVIDER_TIMEOUT_S = 60.0
MIN_PROVIDER_TIMEOUT_S = 5.0
