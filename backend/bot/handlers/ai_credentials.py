"""Media Analysis — API Credentials: the owner-facing credential surface.

This module owns ONE AI sub-surface, beside the media capabilities:

    AI
    └── Media Analysis               (``ai_media``)
        ├── Text recognition          (``ai_media_ocr``)
        ├── Speech-to-Text            (``ai_media_stt``)
        ├── Text-to-Speech            (``ai_media_tts``)
        └── API Credentials           (``ai_cred``)   ← this module
            └── one provider          (``ai_cred_prov``)
                └── one credential    (``ai_cred_one``)

It lives under Media Analysis because the credentials are shared media
infrastructure — the same pool serves the Speech-to-Text providers and the
Text-to-Speech provider — and NOT inside either capability's panel.

WHAT THIS MODULE MAY DO

  * authorize (every handler is owner-only through the existing guard),
  * collect input,
  * call the credential management service,
  * render safe metadata and a bounded result.

WHAT IT MUST NEVER DO

  * touch the database or the secret store — there is no SQL, no database client
    and no direct secret-store call in this file; everything goes through
    ``backend/services/credential_service``,
  * read, print, log, cache or return a secret — the only function that ever
    receives one is the write path, and it hands it straight to the service,
  * put a secret into a callback payload: the surface addresses a credential by a
    short non-secret HANDLE derived from its id, so callback data carries an
    address at most, never a value,
  * claim provider health. A credential existing is not health; only the bounded
    test result is shown, and it says what it actually measured.

SECRET INPUT LIFECYCLE

The key is collected through the project's EXISTING pending-input mechanism
(``backend/helper/input_state``), so it inherits that mechanism's behavior
instead of inventing a second one:

  * a pending input expires after the mechanism's own 120 s bound and is replaced
    by any newer input request;
  * the key arrives as the reply text, is used for ONE service call, and is not
    stored — the module holds no module-level state, no cache and no session;
  * the reply message is DELETED by this module (the shared input machinery does
    the same best-effort), and when the deletion cannot be performed the owner is
    told so plainly instead of being told the flow succeeded cleanly;
  * if the process restarts between the prompt and the reply, the pending input is
    simply gone: the reply is never consumed, never stored and not deleted —
    remove it manually, which the prompt already asks for;
  * an empty, over-long or whitespace-containing reply is refused with a bounded
    reason and nothing is stored;
  * a refused or failed store write leaves nothing behind: the service reports a
    bounded reason and no partial credential is created.

Dispatch, registration and navigation all use the ONE shared helper registry —
no parallel UI framework and no second registry.
"""
from __future__ import annotations

import logging
from typing import Any, Awaitable, Callable

from backend.helper import (
    InlinePanelBuilder,
    register_action,
    register_inline_builder,
    register_input,
    register_panel,
    render,
)
from backend.services import credential_service

logger = logging.getLogger(__name__)

#: The one input scope this surface registers under. The input ID carries the
#: target (``secret_add_<provider>``, ``secret_replace_<handle>``,
#: ``label_<handle>``, ``priority_<handle>``) because the shared input listener
#: hands the handler the reply text only — and because every one of those tokens is
#: non-secret metadata, never a value.
_INPUT_SCOPE = "ai_cred"

#: Presentation-only ceiling for a button label; shortening it can never change
#: which credential a button addresses, because the callback payload is the handle.
_BUTTON_LABEL_MAX = 30

#: The bounded wording of a credential test, so the panel never implies provider
#: health and never implies recognition quality.
_TEST_CAVEAT = (
    "_One bounded request with a synthetic tone: it proves the provider accepted "
    "this key, not recognition quality._"
)

#: A `key_env_var` label is never shown and never named — the panel says whether a
#: deployment credential EXISTS, nothing more.
_DEPLOYMENT_KEY_PRESENT = "present"
_DEPLOYMENT_KEY_ABSENT = "not present"


# ── Shared plumbing ────────────────────────────────────────────────────────


async def _owner_id() -> int:
    from backend.bot.handlers.ai import _get_owner_id

    return await _get_owner_id()


def _nav_buttons(builder: InlinePanelBuilder) -> None:
    from backend.bot.handlers.ai import _nav_buttons as nav

    nav(builder)


def _truncate(text: str, limit: int = _BUTTON_LABEL_MAX) -> str:
    value = str(text or "")
    return value if len(value) <= limit else value[: limit - 1] + "…"


def _ensure_input(
    input_id: str,
    handler: Callable[..., Awaitable[None]],
    prompt: str,
    back: str = "",
) -> None:
    """Register ONE input once (re-rendering a panel must not re-register it).

    The ID encodes the target, so an already-registered ID has an equivalent
    handler and prompt; re-registering would only add a log line per render.
    """
    from backend.helper.panels import get_input

    if get_input(_INPUT_SCOPE, input_id) is not None:
        return
    config: dict[str, Any] = {"handler": handler, "prompt": prompt}
    if back:
        config["back"] = back
    register_input(_INPUT_SCOPE, input_id, config)


async def _delete_message(chat_id: int, msg_id: int) -> bool:
    """Delete the owner's own message; ``False`` when it could not be done."""
    from backend.helper.inline_engine import _self_client

    if not _self_client or not chat_id or not msg_id:
        return False
    try:
        await _self_client.delete_messages(chat_id, [msg_id])
        return True
    except Exception as exc:  # noqa: BLE001 — the flow continues either way
        logger.warning("CREDENTIAL_SECRET_MESSAGE_DELETE_FAILED error=%s", type(exc).__name__)
        return False


async def _finish_secret_input(
    notice: str,
    restore_panel,
    chat_id: int,
    msg_id: int,
    inline_chat_id: int,
    inline_msg_id: int,
) -> None:
    """Close a secret flow: delete the key message FIRST, then edit the panel.

    The deletion is attempted before anything else so the message that carried the
    key spends as little time in the chat as possible, and its outcome is reported
    honestly: a failed deletion is a safe, named warning — never a silent one and
    never a reason to report the operation as clean.
    """
    from backend.bot.handlers.ai import _finish_input

    deleted = await _delete_message(chat_id, msg_id)
    if not deleted:
        notice = f"{notice}\n\n! Your message could not be deleted — remove it manually."
    await _finish_input(notice, restore_panel, chat_id, msg_id, inline_chat_id, inline_msg_id)


# ── Panels ─────────────────────────────────────────────────────────────────


async def _credentials_root(owner_id: int) -> tuple[str, str, list]:
    """AI → Media Analysis → API Credentials: one row per executable provider."""
    listed = await credential_service.list_credentials(owner_id)
    lines = ["**API Credentials**", ""]
    if not listed.ok:
        lines.append(f"! {listed.reason_label}.")
        lines.append("")
        lines.append(
            "_Managed keys need the credential store; a provider that already has "
            "a deployment key keeps working without one._"
        )
        builder = InlinePanelBuilder()
        _nav_buttons(builder)
        return "API Credentials", "\n".join(lines), builder.build()

    grouped = credential_service.group_by_provider(listed.credentials)
    providers = credential_service.registered_providers()
    lines.append("Managed API keys, shared by every media capability.")
    lines.append("")
    if not providers:
        lines.append("_No provider with a runtime adapter is registered on this build._")
    for provider in providers:
        counts = credential_service.summarise(grouped.get(provider, ()))
        label = credential_service.provider_label(provider)
        if counts["total"]:
            lines.append(
                f"{label} · {counts['enabled']} enabled of {counts['total']}"
            )
        else:
            lines.append(f"{label} · no managed keys")

    builder = InlinePanelBuilder()
    for provider in providers:
        builder.add_row(
            credential_service.provider_label(provider),
            f"panel:ai_cred_prov:{provider}",
        )
    _nav_buttons(builder)
    return "API Credentials", "\n".join(lines), builder.build()


async def _provider_panel(owner_id: int, provider: str) -> tuple[str, str, list]:
    """ONE provider's credentials, plus the bounded way in to add another."""
    label = credential_service.provider_label(provider)
    listed = await credential_service.list_credentials(owner_id, provider)
    lines = [f"**{label}**", ""]
    builder = InlinePanelBuilder()

    if not listed.ok:
        lines.append(f"! {listed.reason_label}.")
        lines.append("")
        _nav_buttons(builder)
        return label, "\n".join(lines), builder.build()

    counts = credential_service.summarise(listed.credentials)
    lines.append(f"Managed keys · {counts['enabled']} enabled of {counts['total']}")
    deployment = (
        _DEPLOYMENT_KEY_PRESENT
        if credential_service.env_credential_present(provider)
        else _DEPLOYMENT_KEY_ABSENT
    )
    lines.append(f"Deployment key · {deployment}")
    lines.append("")
    if listed.credentials:
        lines.append(f"{counts['total']} credential{'s' if counts['total'] != 1 else ''}")
        for index, item in enumerate(listed.credentials, start=1):
            lines.append(
                f"{index}. {item.label} — {item.state_word()} · "
                f"priority {item.priority} · {credential_service.last_test_label(item.credential_id)}"
            )
        for index, item in enumerate(listed.credentials, start=1):
            builder.add_row(
                f"{index}. {_truncate(item.label)} · {item.state_word()}",
                f"panel:ai_cred_one:{item.handle}",
            )
    else:
        lines.append("_No managed keys — the deployment key, if any, is used._")

    _ensure_input(
        f"secret_add_{provider}",
        _make_secret_add_handler(provider),
        (
            f"**Add {label} key**\n\n"
            f"Send the API key for {label} as ONE message.\n\n"
            "_It is stored encrypted and is never shown again. Your message is "
            "deleted after it is stored._"
        ),
        back=f"panel:ai_cred_prov:{provider}",
    )
    builder.add_row("➕ Add credential", f"input:{_INPUT_SCOPE}:secret_add_{provider}")
    _nav_buttons(builder)
    return label, "\n".join(lines), builder.build()


async def _credential_panel(owner_id: int, handle: str) -> tuple[str, str, list]:
    """ONE credential: its metadata and every bounded action on it."""
    builder = InlinePanelBuilder()
    item = await credential_service.resolve_handle(owner_id, handle)
    if item is None:
        _nav_buttons(builder)
        return (
            "Credential",
            "**Credential**\n\n× That credential is not there any more — reopen the "
            "provider to refresh.",
            builder.build(),
        )

    label = credential_service.provider_label(item.provider)
    lines = [
        "**Credential**",
        "",
        f"{label} · {item.label}",
        f"{item.state_word()} · priority {item.priority}",
    ]
    testable = credential_service.provider_testable(item.provider)
    if testable:
        lines.append(f"Key test · {credential_service.last_test_label(item.credential_id)}")
    else:
        lines.append("Key test · not supported for this provider")
    lines.append("")
    lines.append(f"Id · {item.credential_id}")
    if item.created_at:
        lines.append(f"Added · {item.created_at}")
    if item.updated_at:
        lines.append(f"Updated · {item.updated_at}")

    builder.add_row(
        "Disable" if item.enabled else "Enable",
        f"action:ai_cred_toggle:{item.handle}",
    )
    _ensure_input(
        f"label_{item.handle}",
        _make_label_handler(item.credential_id),
        (
            "**Rename credential**\n\n"
            f"Current label · {item.label}\n\n"
            f"Send a new label (1–{credential_service.MAX_LABEL_CHARS} characters)."
        ),
        back=f"panel:ai_cred_one:{item.handle}",
    )
    builder.add_row("Rename…", f"input:{_INPUT_SCOPE}:label_{item.handle}")
    builder.add_buttons(
        ("▲ Move earlier", f"action:ai_cred_up:{item.handle}"),
        ("▼ Move later", f"action:ai_cred_down:{item.handle}"),
    )
    _ensure_input(
        f"priority_{item.handle}",
        _make_priority_handler(item.credential_id),
        (
            "**Set priority**\n\n"
            f"Current priority · {item.priority}\n\n"
            f"Send a whole number ({credential_service.MIN_PRIORITY}–"
            f"{credential_service.MAX_PRIORITY}). A LOWER number is tried first."
        ),
        back=f"panel:ai_cred_one:{item.handle}",
    )
    builder.add_row("Set priority…", f"input:{_INPUT_SCOPE}:priority_{item.handle}")
    _ensure_input(
        f"secret_replace_{item.handle}",
        _make_secret_replace_handler(item.credential_id),
        (
            "**Replace key**\n\n"
            f"Send the NEW API key for {label} · {item.label} as ONE message.\n\n"
            "_The stored key is replaced and never shown. Your message is deleted "
            "after it is stored._"
        ),
        back=f"panel:ai_cred_one:{item.handle}",
    )
    builder.add_row("Replace key…", f"input:{_INPUT_SCOPE}:secret_replace_{item.handle}")
    if testable:
        builder.add_row("Test key", f"action:ai_cred_test:{item.handle}")
    builder.add_row("🗑 Delete", f"action:ai_cred_delete:{item.handle}")
    _nav_buttons(builder)
    return "Credential", "\n".join(lines), builder.build()


async def hub_line(owner_id: int) -> str:
    """The credential state as ONE line for the Media Analysis hub.

    A count of MANAGED keys — never a claim that a provider works. A store that
    cannot be reached says so instead of reporting zero keys, because "none
    configured" and "cannot tell" are different facts.
    """
    listed = await credential_service.list_credentials(owner_id)
    if not listed.ok:
        return "API Credentials · store not available"
    counts = credential_service.summarise(listed.credentials)
    if not counts["total"]:
        return "API Credentials · no managed keys"
    return f"API Credentials · {counts['enabled']} enabled of {counts['total']}"


async def _ai_cred_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    return await _credentials_root(await _owner_id())


async def _ai_cred_inline_builder(event, extra: str) -> list:
    result = await _ai_cred_panel_handler(event, extra)
    if result is None:
        return [render("API Credentials", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


async def _ai_cred_prov_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    """ONE provider's panel. An unknown provider falls back to the root panel."""
    provider = str(extra or "").strip()
    if not credential_service.is_registered_provider(provider):
        title, body, buttons = await _credentials_root(await _owner_id())
        return title, f"× Unknown provider — nothing changed.\n\n{body}", buttons
    return await _provider_panel(await _owner_id(), provider)


async def _ai_cred_prov_inline_builder(event, extra: str) -> list:
    result = await _ai_cred_prov_panel_handler(event, extra)
    if result is None:
        return [render("Credential", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


async def _ai_cred_one_panel_handler(event, extra: str) -> tuple[str, str, list] | None:
    return await _credential_panel(await _owner_id(), str(extra or ""))


async def _ai_cred_one_inline_builder(event, extra: str) -> list:
    result = await _ai_cred_one_panel_handler(event, extra)
    if result is None:
        return [render("Credential", "Error.", [])]
    title, body, buttons = result
    return [render(title, body, buttons)]


# ── Re-rendering with a bounded notice (ONE edit, never a new message) ─────


def _restore(factory: Callable[[], Awaitable[tuple[str, str, list]]]):
    """Adapt a zero-argument panel coroutine to the shared input-completion shape.

    ``_finish_input`` calls the restore callable as ``restore_panel(None, "")``, so
    the panel is re-rendered from LIVE state at that moment rather than from
    whatever was rendered before the owner replied.
    """

    async def _panel(*_ignored):
        return await factory()

    return _panel


async def _provider_with_notice(provider: str, notice: str) -> tuple[str, str, list]:
    owner = await _owner_id()
    title, body, buttons = await _provider_panel(owner, provider)
    return title, f"{notice}\n\n{body}", buttons


async def _credential_with_notice(handle: str, notice: str) -> tuple[str, str, list]:
    owner = await _owner_id()
    title, body, buttons = await _credential_panel(owner, handle)
    return title, f"{notice}\n\n{body}", buttons


# ── Actions ────────────────────────────────────────────────────────────────


def _handle_from_callback(event, extra: str) -> str:
    """The non-secret handle a callback carried (never a credential value)."""
    return str(extra or "").strip()


async def _ai_cred_toggle_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Enable or disable ONE credential — metadata only, never the secret."""
    handle = _handle_from_callback(event, extra)
    owner = await _owner_id()
    item = await credential_service.resolve_handle(owner, handle)
    if item is None:
        return await _credential_with_notice(handle, "× That credential is gone — reopen the provider.")

    outcome = await credential_service.update_metadata(
        owner, item.credential_id, enabled=not item.enabled,
    )
    if not outcome.ok:
        return await _credential_with_notice(handle, f"× {outcome.reason_label}.")
    await credential_service.refresh_provider(item.provider)
    word = "enabled" if not item.enabled else "disabled"
    return await _credential_with_notice(handle, f"✓ {item.label} {word}")


async def _move_priority(handle: str, delta: int) -> tuple[str, str, list]:
    """Shift ONE credential's priority deterministically by one step."""
    owner = await _owner_id()
    item = await credential_service.resolve_handle(owner, handle)
    if item is None:
        return await _credential_with_notice(handle, "× That credential is gone — reopen the provider.")

    target = max(
        credential_service.MIN_PRIORITY,
        min(credential_service.MAX_PRIORITY, item.priority + delta),
    )
    if target == item.priority:
        bound = "first" if delta < 0 else "last"
        return await _credential_with_notice(handle, f"! Already {bound} — priority {item.priority}.")

    outcome = await credential_service.update_metadata(owner, item.credential_id, priority=target)
    if not outcome.ok:
        return await _credential_with_notice(handle, f"× {outcome.reason_label}.")
    await credential_service.refresh_provider(item.provider)
    return await _credential_with_notice(handle, f"✓ {item.label} · priority {target}")


async def _ai_cred_up_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    return await _move_priority(_handle_from_callback(event, extra), -1)


async def _ai_cred_down_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    return await _move_priority(_handle_from_callback(event, extra), 1)


async def _ai_cred_test_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Test whether the provider ACCEPTS this key, with ONE bounded request."""
    handle = _handle_from_callback(event, extra)
    owner = await _owner_id()
    item = await credential_service.resolve_handle(owner, handle)
    if item is None:
        return await _credential_with_notice(handle, "× That credential is gone — reopen the provider.")

    result = await credential_service.test_credential(owner, item.credential_id)
    return await _credential_with_notice(handle, _test_notice(result))


def _test_notice(result: credential_service.CredentialTestResult) -> str:
    """ONE bounded owner-facing notice for a finished key test."""
    mark = "✓" if result.passed else "×"
    line = f"{mark} Key test · {result.state_label}"
    if result.failure_class:
        line += f" · {result.failure_class}"
    if result.passed and result.latency_ms:
        line += f" · {result.latency_ms} ms"
    return f"{line}\n\n{_TEST_CAVEAT}"


async def _ai_cred_delete_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Ask before deleting: the removal takes the Vault secret with it."""
    handle = _handle_from_callback(event, extra)
    owner = await _owner_id()
    item = await credential_service.resolve_handle(owner, handle)
    if item is None:
        return await _credential_with_notice(handle, "× That credential is gone — reopen the provider.")

    title = credential_service.provider_label(item.provider)
    body = (
        "**Delete credential**\n\n"
        f"Delete {item.label} ({item.state_word()} · priority {item.priority})?\n\n"
        "_The stored key is removed from the credential store with it. This cannot "
        "be undone._"
    )
    builder = InlinePanelBuilder()
    builder.add_row("🗑 Delete now", f"action:ai_cred_delete_yes:{item.handle}")
    _nav_buttons(builder)
    return title, body, builder.build()


async def _ai_cred_delete_yes_action(event, extra: str, chat_id: int) -> tuple[str, str, list] | None:
    """Delete ONE credential and its stored key, then show the provider."""
    handle = _handle_from_callback(event, extra)
    owner = await _owner_id()
    item = await credential_service.resolve_handle(owner, handle)
    if item is None:
        return await _credential_with_notice(handle, "× That credential is gone — reopen the provider.")

    outcome = await credential_service.delete_credential(owner, item.credential_id)
    if not outcome.ok:
        return await _credential_with_notice(handle, f"× Nothing was deleted — {outcome.reason_label}.")
    await credential_service.refresh_provider(item.provider)
    return await _provider_with_notice(item.provider, f"✓ Deleted {item.label} and its stored key")


# ── Inputs ─────────────────────────────────────────────────────────────────


async def _auto_label_and_priority(owner: int, provider: str) -> tuple[str, int]:
    """A deterministic default label and the next priority for a new credential."""
    label = credential_service.provider_label(provider)
    listed = await credential_service.list_credentials(owner, provider)
    if not listed.ok:
        return label, credential_service.MIN_PRIORITY
    priorities = [item.priority for item in listed.credentials]
    next_priority = min(
        credential_service.MAX_PRIORITY,
        (max(priorities) + 1) if priorities else credential_service.MIN_PRIORITY,
    )
    return f"{label} {len(listed.credentials) + 1}", next_priority


def _make_secret_add_handler(provider: str):
    """The write path for ONE provider's new key (the handler never sees a handle)."""

    async def _handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
        owner = await _owner_id()
        label, priority = await _auto_label_and_priority(owner, provider)
        outcome = await credential_service.create_credential(
            owner, provider, label, text, priority=priority,
        )
        if outcome.ok and outcome.credential is not None:
            await credential_service.refresh_provider(provider)
            item = outcome.credential
            notice = (
                f"✓ Key added\n\n{credential_service.provider_label(item.provider)} · "
                f"{item.label}\n{item.state_word()} · priority {item.priority}"
            )
        else:
            notice = f"× Nothing was stored — {outcome.reason_label}."
        await _finish_secret_input(
            notice,
            _restore(lambda: _provider_panel(owner, provider)),
            chat_id, msg_id, inline_chat_id, inline_msg_id,
        )

    return _handler


def _make_secret_replace_handler(credential_id: str):
    """The replace path for ONE credential (never returns the old or new key)."""

    async def _handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
        owner = await _owner_id()
        item = await credential_service.credential(owner, credential_id)
        handle = item.handle if item is not None else ""
        outcome = await credential_service.replace_secret(owner, credential_id, text)
        if outcome.ok:
            await credential_service.refresh_provider(item.provider if item else "")
            notice = "✓ Key replaced — the previous key is gone"
        else:
            notice = f"× Nothing was changed — {outcome.reason_label}."
        await _finish_secret_input(
            notice,
            _restore(lambda: _credential_panel(owner, handle)),
            chat_id, msg_id, inline_chat_id, inline_msg_id,
        )

    return _handler


def _make_label_handler(credential_id: str):
    async def _handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
        from backend.bot.handlers.ai import _finish_input

        owner = await _owner_id()
        item = await credential_service.credential(owner, credential_id)
        if item is None:
            await _finish_input(
                "× That credential is gone — reopen the provider.",
                _restore(lambda: _credentials_root(owner)),
                chat_id, msg_id, inline_chat_id, inline_msg_id,
            )
            return
        outcome = await credential_service.update_metadata(owner, credential_id, label=text)
        if outcome.ok and outcome.credential is not None:
            notice = f"✓ Renamed to {outcome.credential.label}"
        else:
            notice = f"× Nothing was changed — {outcome.reason_label}."
        await _finish_input(
            notice,
            _restore(lambda: _credential_panel(owner, item.handle)),
            chat_id, msg_id, inline_chat_id, inline_msg_id,
        )

    return _handler


def _make_priority_handler(credential_id: str):
    async def _handler(text, chat_id, msg_id, inline_chat_id, inline_msg_id):
        from backend.bot.handlers.ai import _finish_input

        owner = await _owner_id()
        item = await credential_service.credential(owner, credential_id)
        if item is None:
            await _finish_input(
                "× That credential is gone — reopen the provider.",
                _restore(lambda: _credentials_root(owner)),
                chat_id, msg_id, inline_chat_id, inline_msg_id,
            )
            return
        try:
            priority = int(str(text).strip())
        except ValueError:
            notice = (
                f"× Enter a whole number ({credential_service.MIN_PRIORITY}–"
                f"{credential_service.MAX_PRIORITY})."
            )
        else:
            outcome = await credential_service.update_metadata(
                owner, credential_id, priority=priority,
            )
            if outcome.ok and outcome.credential is not None:
                await credential_service.refresh_provider(item.provider)
                notice = f"✓ {item.label} · priority {outcome.credential.priority}"
            else:
                notice = f"× Nothing was changed — {outcome.reason_label}."
        await _finish_input(
            notice,
            _restore(lambda: _credential_panel(owner, item.handle)),
            chat_id, msg_id, inline_chat_id, inline_msg_id,
        )

    return _handler


# ── Registration ───────────────────────────────────────────────────────────


def register(client=None, owner_id: int = 0) -> None:
    """Attach the API Credentials surface to the ONE shared registry."""
    try:
        register_panel(
            "ai_cred", _ai_cred_panel_handler, parent="ai_media", title="API Credentials",
        )
        register_inline_builder("ai_cred", _ai_cred_inline_builder)
        register_panel(
            "ai_cred_prov", _ai_cred_prov_panel_handler,
            parent="ai_cred", title="Provider credentials",
        )
        register_inline_builder("ai_cred_prov", _ai_cred_prov_inline_builder)
        register_panel(
            "ai_cred_one", _ai_cred_one_panel_handler,
            parent="ai_cred_prov", title="Credential",
        )
        register_inline_builder("ai_cred_one", _ai_cred_one_inline_builder)
        register_action("ai_cred_toggle", _ai_cred_toggle_action)
        register_action("ai_cred_up", _ai_cred_up_action)
        register_action("ai_cred_down", _ai_cred_down_action)
        register_action("ai_cred_test", _ai_cred_test_action)
        register_action("ai_cred_delete", _ai_cred_delete_action)
        register_action("ai_cred_delete_yes", _ai_cred_delete_yes_action)
    except Exception as exc:  # noqa: BLE001 — registration is never fatal
        logger.error("API Credentials registration FAILED: %s", exc)
