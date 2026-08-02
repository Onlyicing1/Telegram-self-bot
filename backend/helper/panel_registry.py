"""
Centralized Panel Registry.

Every panel in the system is registered here with its navigation metadata
(parent, callback prefix, title) and render function. This eliminates
scattered navigation metadata (like PARENT_MAP) and makes adding new panels
a single-call operation.

Adding a new panel requires only:
  1. register_panel("new_id", handler, parent="parent_id", title="Title")
  2. Implement the render function

No navigation edits anywhere else.
"""
import logging
from dataclasses import dataclass
from typing import Awaitable, Callable

from telethon import events

logger = logging.getLogger(__name__)

PanelHandler = Callable[[events.CallbackQuery.Event, str], Awaitable[tuple[str, str, list] | None]]


@dataclass
class PanelDef:
    panel_id: str
    render_function: PanelHandler
    parent_panel: str = "menu"
    callback_prefix: str = "panel"
    title: str = ""


class PanelRegistry:
    __slots__ = ("_panels",)

    def __init__(self) -> None:
        self._panels: dict[str, PanelDef] = {}

    def register(
        self,
        panel_id: str,
        handler: PanelHandler,
        parent: str = "menu",
        title: str = "",
        callback_prefix: str = "panel",
    ) -> PanelDef:
        entry = PanelDef(
            panel_id=panel_id,
            render_function=handler,
            parent_panel=parent,
            callback_prefix=callback_prefix,
            title=title,
        )
        self._panels[panel_id] = entry
        logger.info(
            "[REGISTRY] Registered panel: id='%s' parent='%s' title='%s' (total=%d)",
            panel_id, parent, title, len(self._panels),
        )
        return entry

    def get(self, panel_id: str) -> PanelDef | None:
        return self._panels.get(panel_id)

    def get_handler(self, panel_id: str) -> PanelHandler | None:
        entry = self._panels.get(panel_id)
        return entry.render_function if entry else None

    def get_parent(self, panel_id: str) -> str:
        entry = self._panels.get(panel_id)
        if entry is None:
            return "menu"
        return entry.parent_panel

    def get_title(self, panel_id: str) -> str:
        entry = self._panels.get(panel_id)
        if entry is None:
            return ""
        return entry.title

    def has(self, panel_id: str) -> bool:
        return panel_id in self._panels

    def all_ids(self) -> list[str]:
        return list(self._panels.keys())

    def all_panels(self) -> list[PanelDef]:
        return list(self._panels.values())

    def clear(self) -> None:
        self._panels.clear()


_registry = PanelRegistry()


def register_panel(
    panel_id: str,
    handler: PanelHandler,
    parent: str = "menu",
    title: str = "",
) -> PanelDef:
    return _registry.register(panel_id, handler, parent=parent, title=title)


def get_panel_def(panel_id: str) -> PanelDef | None:
    return _registry.get(panel_id)


def get_panel_handler(panel_id: str) -> PanelHandler | None:
    return _registry.get_handler(panel_id)


def get_panel_parent(panel_id: str) -> str:
    return _registry.get_parent(panel_id)


def get_panel_title(panel_id: str) -> str:
    return _registry.get_title(panel_id)


def has_panel(panel_id: str) -> bool:
    return _registry.has(panel_id)


def all_panel_ids() -> list[str]:
    return _registry.all_ids()


def registry() -> PanelRegistry:
    return _registry
