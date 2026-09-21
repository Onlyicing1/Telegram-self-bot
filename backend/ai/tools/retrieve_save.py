"""Saved-item management tools — wrap the existing ``retrieve_service``.

Five thin wrappers over the SAME service operations the retrieve panels
already use, so the AI-facing management surface is the panel surface:

  - ``retrieve_save``    — re-send the item (media + metadata caption)
  - ``preview_save``     — read the item's stored metadata by save code
  - ``delete_save``      — remove the item (its saved copy + its DB row)
  - ``rename_save``      — change the item's display name (Save V2 Part 4)
  - ``update_save_tags`` — add / replace / remove the owner's tags

Destinations are resolved from TRUSTED runtime context — the chat the AI
request came from — never from model output, mirroring ``SendMessageTool``.
The save code is owner-scoped through the existing service/DB contract:
the authenticated owner identity always comes from ``context.owner_id``,
never from tool arguments. A name/tag target is resolved by the shared
deterministic resolver (``resolve_management_target``); an ambiguous target
is NEVER narrowed by the model — it is reported back and the owner chooses.
"""
from __future__ import annotations

from typing import Any

from backend.ai.tools.base import PermissionLevel, Tool, ToolResult, result_from_service
from backend.ai.tools.context import ToolContext


# The tag operations ``update_save_tags`` accepts (the service's own enum).
_TAG_MODES = ("add", "replace", "remove")


def _resolve_management_target(
    tool_name: str,
    action_word: str,
    target,
) -> ToolResult | None:
    """Turn a non-``ok`` management target into the tool result to return.

    ``None`` means the caller may proceed. An ambiguous target returns the
    resolver's owner-facing candidate list plus an explicit instruction never
    to choose — the tool performs NO write in that case.
    """
    from backend.services import retrieve_service

    if target.status == retrieve_service.TARGET_AMBIGUOUS:
        lines = [target.message]
        lines.append(
            f"NOTHING was {action_word}. Ask the owner which one they mean — "
            f"never choose for them. When they answer, call {tool_name} again "
            "with that item's exact save_code."
        )
        return ToolResult(
            success=True,
            message="\n".join(lines),
            data={
                "outcome": "ambiguous",
                "query": getattr(target.resolution, "query", ""),
                "candidates": [
                    {
                        "save_code": c.save_code,
                        "label": retrieve_service.candidate_label(c),
                    }
                    for c in (target.resolution.candidates if target.resolution else ())
                ],
            },
        )
    if target.status == retrieve_service.TARGET_NOT_FOUND:
        return ToolResult(
            success=False,
            message=target.message or "No saved item matches that request.",
            data={"outcome": "not_found"},
        )
    if target.status != retrieve_service.TARGET_OK:
        return ToolResult(
            success=False,
            message=target.message or "The saved item could not be resolved.",
        )
    return None


class RenameSaveTool(Tool):
    """Give ONE saved item a new display name (metadata only).

    Delegates to ``retrieve_service.do_rename`` — the same owner-scoped
    operation (and the same shared name rule) the retrieve panel's Rename
    action uses. The item's saved Telegram message, its files and its tags are
    untouched: only ``saved_items.display_name`` changes.
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "rename_save"

    @property
    def required_arguments(self) -> tuple[str, ...]:
        return ("display_name",)

    @property
    def required_any_arguments(self) -> tuple[str, ...]:
        return ("save_code", "query")

    @property
    def description(self) -> str:
        return (
            "Give ONE saved item a new display name. Pass save_code when the "
            "owner gives a code or a previous result listed one; otherwise "
            "pass query with the owner's own words (a name or tag) and the "
            "system resolves it deterministically. With query, EXACTLY one "
            "match is renamed immediately and MULTIPLE matches are never "
            "renamed — the result lists them and asks the owner to choose, so "
            "never pick one yourself. The name is the item's own label; the "
            "saved file, its Telegram message and its tags are unchanged."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "save_code": {
                "type": "string",
                "default": "",
                "description": "The item's save code (e.g. S0001). Never invent a code.",
            },
            "query": {
                "type": "string",
                "default": "",
                "description": (
                    "The owner's own words describing the item by name or tag "
                    "when no code was given. Resolved deterministically; "
                    "multiple matches always ask the owner to choose."
                ),
            },
            "display_name": {
                "type": "string",
                "description": (
                    "The new name for the item, exactly as the owner gave it "
                    "(e.g. 'University Weekly Schedule Semester 2'). Never "
                    "invent or embellish a name."
                ),
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_WRITE

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with the rename confirmation or honest failure"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import retrieve_service

        raw_name = arguments.get("display_name")
        if not isinstance(raw_name, str) or not raw_name.strip():
            return ToolResult(
                success=False,
                message="A new name is required. Nothing was renamed.",
            )

        target = await retrieve_service.resolve_management_target(
            context.owner_id,
            save_code=str(arguments.get("save_code") or ""),
            query=str(arguments.get("query") or ""),
        )
        blocked = _resolve_management_target("rename_save", "renamed", target)
        if blocked is not None:
            return blocked

        try:
            result = await retrieve_service.do_rename(
                context.owner_id, target.save_code, raw_name
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Rename failed: {exc}")
        return result_from_service(
            result,
            data={"save_code": target.save_code, "display_name": raw_name.strip()},
        )


class UpdateSaveTagsTool(Tool):
    """Add, replace or remove the OWNER tags of ONE saved item.

    Delegates to ``retrieve_service.do_edit_tags`` — the same owner-scoped
    operation and the same shared tag rules a save uses. The operation is
    explicit (never inferred): ``add`` merges, ``replace`` sets exactly the
    given list (``[]`` clears every tag) and ``remove`` deletes the named ones.
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "update_save_tags"

    @property
    def required_arguments(self) -> tuple[str, ...]:
        return ("tags", "mode")

    @property
    def required_any_arguments(self) -> tuple[str, ...]:
        return ("save_code", "query")

    @property
    def description(self) -> str:
        return (
            "Change the tags of ONE saved item. Pass mode='add' to add tags, "
            "mode='replace' to set the tags to exactly the given list (an "
            "empty list removes every tag when the owner asks for no tags), "
            "or mode='remove' to delete the given tags. Pass save_code when "
            "the owner gives a code or a previous result listed one; "
            "otherwise pass query with the owner's own words and the system "
            "resolves it deterministically. With query, EXACTLY one match is "
            "updated and MULTIPLE matches are never updated — the result "
            "lists them and asks the owner to choose, so never pick one "
            "yourself. Tags are normalized by the shared rules; never invent "
            "a tag and never turn a sentence into tags."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "save_code": {
                "type": "string",
                "default": "",
                "description": "The item's save code (e.g. S0001). Never invent a code.",
            },
            "query": {
                "type": "string",
                "default": "",
                "description": (
                    "The owner's own words describing the item by name or tag "
                    "when no code was given. Resolved deterministically; "
                    "multiple matches always ask the owner to choose."
                ),
            },
            "tags": {
                "type": "array",
                "items": {"type": "string"},
                "description": (
                    "The tags to apply, e.g. ['university', 'semester-2']. "
                    "Pass [] with mode='replace' to remove every tag. Never "
                    "invent a tag the owner did not give."
                ),
            },
            "mode": {
                "type": "string",
                "description": (
                    "Required, explicit: 'add' (merge), 'replace' (set "
                    "exactly this list) or 'remove' (delete these tags)."
                ),
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_WRITE

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with the updated tag list or honest failure"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import retrieve_service

        mode = str(arguments.get("mode") or "").strip().lower()
        if mode not in _TAG_MODES:
            return ToolResult(
                success=False,
                message=(
                    "The tag mode must be one of add, replace or remove. "
                    "Nothing was changed."
                ),
            )
        tags = arguments.get("tags")
        if not isinstance(tags, list) or not all(isinstance(t, str) for t in tags):
            return ToolResult(
                success=False,
                message="Tags must be a list of strings. Nothing was changed.",
            )

        target = await retrieve_service.resolve_management_target(
            context.owner_id,
            save_code=str(arguments.get("save_code") or ""),
            query=str(arguments.get("query") or ""),
        )
        blocked = _resolve_management_target("update_save_tags", "updated", target)
        if blocked is not None:
            return blocked

        try:
            result = await retrieve_service.do_edit_tags(
                context.owner_id, target.save_code, mode, tags
            )
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Tag update failed: {exc}")
        return result_from_service(
            result,
            data={"save_code": target.save_code, "mode": mode, "tags": list(tags)},
        )


class RetrieveSaveTool(Tool):
    """Re-send a saved item to the current chat (the panel's Retrieve action)."""

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "retrieve_save"

    @property
    def required_arguments(self) -> tuple[str, ...]:
        """No single argument is required — see ``required_any_arguments``."""
        return ()

    @property
    def required_any_arguments(self) -> tuple[str, ...]:
        """Exactly one of ``save_code`` / ``query`` (enforced in execute)."""
        return ("save_code", "query")

    @property
    def description(self) -> str:
        return (
            "Re-send a saved item into the current chat with its metadata "
            "caption. Pass save_code when the owner gives a concrete code "
            "(e.g. S0001) — never invent one. Pass query with the owner's "
            "own words when they refer to an item by name or tag (e.g. "
            "'university schedule'): the system resolves it deterministically "
            "against the stored display names and tags. With query, EXACTLY "
            "one match is sent immediately; MULTIPLE matches are never sent — "
            "the result lists them and asks the owner to choose, so never "
            "pick one yourself. The destination is always the chat the "
            "request came from — never a user-supplied chat."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "save_code": {
                "type": "string",
                "default": "",
                "description": (
                    "The item's save code (e.g. S0001). Use when the owner "
                    "gave a code or a previous result listed one. Never invent "
                    "a code."
                ),
            },
            "query": {
                "type": "string",
                "default": "",
                "description": (
                    "The owner's own words describing the saved item by name "
                    "or tag (e.g. 'university schedule'). Resolved "
                    "deterministically; multiple matches always ask the owner "
                    "to choose instead of sending one."
                ),
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_WRITE

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with the retrieval confirmation or honest failure"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import retrieve_service

        # Model/provider output is normalized at the tool boundary: the
        # service looks codes up verbatim and DB codes are stored upper-case,
        # so a lower-cased echo ("s0012") must be canonicalized here. Codes
        # are `S` + alphanumerics (see db.client._SHORT_CODE_ALPHABET).
        save_code = str(arguments.get("save_code") or "").strip().upper()
        query = str(arguments.get("query") or "").strip()

        if save_code and query:
            return ToolResult(
                success=False,
                message="Provide either a save code or a name/tag query — not both. Nothing was retrieved.",
            )
        if not save_code and not query:
            return ToolResult(
                success=False,
                message="A save code (e.g. S0001) or a name/tag query is required. Nothing was retrieved.",
            )

        chat_id = context.extra.get("chat_id") if context.extra else None
        if not isinstance(chat_id, int) or chat_id == 0:
            return ToolResult(
                success=False,
                message="No trusted destination chat is available; nothing was retrieved.",
            )

        client = None
        if context.telegram is not None:
            client = getattr(context.telegram, "client", None)
        if client is None:
            client = context.client
        if client is None:
            return ToolResult(success=False, message="No Telegram client available.")

        # ── Name/tag resolution (Save V2 Part 3) ──
        # The resolver never performs Telegram work. 0 matches is an honest
        # not-found; N matches are returned to the model as an explicit
        # owner-choice prompt and NOTHING is retrieved; exactly one match
        # falls through to the SAME do_retrieve path as a save code.
        resolved_from = "code"
        if query:
            try:
                resolution = await retrieve_service.resolve_saved_items(context.owner_id, query)
            except Exception as exc:  # noqa: BLE001
                return ToolResult(success=False, message=f"Saved-item search failed: {exc}")

            if resolution.status == retrieve_service.RESOLUTION_NOT_FOUND:
                return ToolResult(
                    success=False,
                    message=retrieve_service.format_resolution(resolution),
                    data={"outcome": "not_found", "query": resolution.query},
                )

            if resolution.status == retrieve_service.RESOLUTION_AMBIGUOUS:
                lines = [retrieve_service.format_resolution(resolution)]
                lines.append(
                    "NOTHING was retrieved. Ask the owner which one they mean — "
                    "never choose for them. When they answer, call retrieve_save "
                    "again with that item's exact save_code."
                )
                return ToolResult(
                    success=True,
                    message="\n".join(lines),
                    data={
                        "outcome": "ambiguous",
                        "query": resolution.query,
                        "candidates": [
                            {
                                "save_code": c.save_code,
                                "label": retrieve_service.candidate_label(c),
                            }
                            for c in resolution.candidates
                        ],
                        "displayed": len(resolution.candidates),
                        "more_matches_exist": resolution.overflowed,
                    },
                )

            save_code = resolution.candidates[0].save_code
            resolved_from = "query"

        if not save_code or not all(ch.isalnum() for ch in save_code):
            return ToolResult(
                success=False,
                message="A valid save code is required (e.g. S0001).",
            )

        try:
            result = await retrieve_service.do_retrieve(client, context.owner_id, save_code, chat_id)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Retrieve failed: {exc}")
        data: dict[str, Any] = {"save_code": save_code, "chat_id": chat_id}
        if resolved_from == "query":
            # The code path's structured data stays byte-identical to the
            # pre-resolver contract; only a name/tag resolution adds a key.
            data["resolved_from"] = "query"
        return result_from_service(result, data=data)


class PreviewSaveTool(Tool):
    """Read ONE saved item's stored metadata by its save code.

    Delegates to ``retrieve_service.do_preview`` — the exact function the
    retrieve panel's manual code entry already uses. Read-only: no Telegram
    side effect, no re-send, no DB write.
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "preview_save"

    @property
    def required_arguments(self) -> tuple[str, ...]:
        return ("save_code",)

    @property
    def description(self) -> str:
        return (
            "Show the stored metadata of ONE saved item by its save code "
            "(e.g. S0001): type, format, size, sender and save date. Use this "
            "when the owner asks for the details of a specific saved item. It "
            "never re-sends the file — use retrieve_save for that."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "save_code": {
                "type": "string",
                "description": "The save code of the item to preview (from search/list_saves).",
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.READ_ONLY

    @property
    def safe(self) -> bool:
        return True

    @property
    def return_type(self) -> str:
        return "ToolResult with the item's metadata text in message"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import retrieve_service

        save_code = str(arguments.get("save_code") or "").strip().upper()
        if not save_code or not all(ch.isalnum() for ch in save_code):
            return ToolResult(
                success=False,
                message="A valid save code is required (e.g. S0001).",
            )

        client = None
        if context.telegram is not None:
            client = getattr(context.telegram, "client", None)
        if client is None:
            client = context.client

        try:
            # ``do_preview`` reads only the owner's saved-items row; the
            # signature keeps the client for symmetry with the other item
            # operations and never uses it.
            result = await retrieve_service.do_preview(client, context.owner_id, save_code)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Preview failed: {exc}")
        return result_from_service(result, data={"save_code": save_code})


class DeleteSaveTool(Tool):
    """Delete a saved item: its Saved Messages copy and its DB row.

    Delegates to ``retrieve_service.do_delete`` — the same owner-scoped
    operation the retrieve panel's Delete action uses. This is Saved-Items
    management, NOT message deletion: the original message in the origin
    chat is never touched.
    """

    def __init__(self, context: ToolContext) -> None:
        self._context = context

    @property
    def name(self) -> str:
        return "delete_save"

    @property
    def required_arguments(self) -> tuple[str, ...]:
        return ("save_code",)

    @property
    def description(self) -> str:
        return (
            "Delete a saved item by its save code (e.g. S0001): removes the "
            "item from Saved Items and deletes its Saved Messages copy. Use "
            "this for 'delete saved item S0001' — the original message in the "
            "source chat is never touched."
        )

    @property
    def parameters(self) -> dict[str, Any]:
        return {
            "save_code": {
                "type": "string",
                "description": "The save code of the item to delete (from search/list_saves).",
            },
        }

    @property
    def permission_level(self) -> PermissionLevel:
        return PermissionLevel.DANGEROUS

    @property
    def safe(self) -> bool:
        return False

    @property
    def return_type(self) -> str:
        return "ToolResult with the deletion confirmation or honest failure"

    async def execute(self, context: ToolContext, arguments: dict[str, Any]) -> ToolResult:
        from backend.services import retrieve_service

        save_code = str(arguments.get("save_code") or "").strip().upper()
        if not save_code or not all(ch.isalnum() for ch in save_code):
            return ToolResult(
                success=False,
                message="A valid save code is required (e.g. S0001). Nothing was deleted.",
            )

        client = None
        if context.telegram is not None:
            client = getattr(context.telegram, "client", None)
        if client is None:
            client = context.client
        if client is None:
            return ToolResult(
                success=False,
                message="No Telegram client available; the saved item was not deleted.",
            )

        try:
            result = await retrieve_service.do_delete(client, context.owner_id, save_code)
        except Exception as exc:  # noqa: BLE001
            return ToolResult(success=False, message=f"Delete failed: {exc}")
        return result_from_service(result, data={"save_code": save_code})
