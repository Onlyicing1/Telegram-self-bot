"""
Reusable paginator for inline panels.

Provides a standard Previous / Page X / Y / Next row builder.
No other module may implement pagination logic.

Single button API (Option A):
  Returns tuples: ("Text", "callback_data")
  The renderer converts tuples to Button objects.
"""


def build_pagination_row(
    current_page: int,
    total_pages: int,
    panel_id: str,
    extra_prefix: str = "",
) -> list:
    """Build a single pagination row [Prev] [Page X/Y] [Next].

    Returns a list of (text, callback_data) tuples for one keyboard row.
    """
    buttons = []

    if current_page > 1:
        prev_data = f"panel:{panel_id}:page:{current_page - 1}"
        if extra_prefix:
            prev_data = f"panel:{panel_id}:{extra_prefix}:page:{current_page - 1}"
        buttons.append(("‹ Prev", prev_data))

    buttons.append((f"{current_page}/{total_pages}", f"panel:{panel_id}:noop"))

    if current_page < total_pages:
        next_data = f"panel:{panel_id}:page:{current_page + 1}"
        if extra_prefix:
            next_data = f"panel:{panel_id}:{extra_prefix}:page:{current_page + 1}"
        buttons.append(("Next ›", next_data))

    return buttons


def paginate(items: list, page: int, per_page: int) -> tuple[list, int, int]:
    """Slice items into a page. Returns (page_items, current_page, total_pages)."""
    if per_page < 1:
        per_page = 1
    total = len(items)
    total_pages = max(1, (total + per_page - 1) // per_page)
    if page < 1:
        page = 1
    if page > total_pages:
        page = total_pages
    start = (page - 1) * per_page
    end = start + per_page
    return items[start:end], page, total_pages
