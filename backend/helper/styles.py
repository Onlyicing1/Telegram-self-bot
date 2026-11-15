"""Single source of truth for Telegram's official inline-button colors.

Telegram supports exactly three named styles — ``primary`` (blue),
``success`` (green) and ``danger`` (red). There is no arbitrary color
support. Panels pass one of these names (or ``None``) to the existing
``InlinePanelBuilder``; this module only records the intended semantics so
the mapping stays consistent and restrained:

* :data:`PRIMARY` — blue; the default for ordinary actions and navigation,
  including **Save** and **Delete**. A positive action is not green merely for
  being positive, and a destructive action is not red merely for being
  destructive.
* :data:`SUCCESS` — green; reserved for explicit confirmation actions
  (**Confirm**, **Approve**, or an equivalent final confirmation step).
* :data:`DANGER` — red; reserved for explicit cancellation actions
  (**Cancel**, **Abort**, **Discard changes**) and the final confirm action of
  a destructive bulk operation (**Delete selected**), where the red marks the
  irreversible consequence rather than the action category.

Assignment rule: colour encodes *intent to confirm, cancel, or accept an
irreversible consequence*, not the category of the underlying operation. An
ordinary confirmation receives green, and cancellation or a destructive final
confirmation receives red; every other button — Save, Delete, Clear, Reset,
Apply, Complete, and all navigation — is blue (``PRIMARY``).
"""

PRIMARY = "primary"
SUCCESS = "success"
DANGER = "danger"
