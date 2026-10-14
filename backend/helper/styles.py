"""Single source of truth for Telegram's official inline-button colors.

Telegram supports exactly three named styles — ``primary`` (blue),
``success`` (green) and ``danger`` (red). There is no arbitrary color
support. Panels pass one of these names (or ``None``) to the existing
``InlinePanelBuilder``; this module only records the intended semantics so
the mapping stays consistent and restrained:

* :data:`PRIMARY` — blue; ordinary navigation and neutral actions (default).
* :data:`SUCCESS` — green; safe positive actions (Save, approve, confirm-safe).
* :data:`DANGER` — red; destructive actions (Delete, confirm-delete).

Assignment rule: a button that performs a destructive operation must never be
blue or green, and green is reserved for genuinely positive/safe actions — so
neither green nor red is applied merely because a button sits in a category.
"""

PRIMARY = "primary"
SUCCESS = "success"
DANGER = "danger"
