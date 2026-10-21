"""One five-segment Unicode progress bar for every Glass UI progress surface.

The model tester and the Save engine both render progress with the SAME bar:
exactly five segments, each covering 20 percentage points, ``▰`` filled and
``▱`` empty (both from the central symbol registry). Keeping the renderer here
means the two surfaces can never drift into visually different bars.
"""
from __future__ import annotations

from backend.helper import symbols as sym

SEGMENTS = 5


def render_progress_bar(fraction: float) -> str:
    """Exactly five segments; each segment covers 20 percentage points."""
    fraction = min(max(fraction, 0.0), 1.0)
    filled = min(SEGMENTS, int(fraction * SEGMENTS + 1e-9))
    return "".join(sym.PAGE_CURRENT if i < filled else sym.PAGE_EMPTY for i in range(SEGMENTS))
