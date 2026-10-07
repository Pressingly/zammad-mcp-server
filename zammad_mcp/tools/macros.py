"""Macros: ``list_macros``, then ``prepare_apply_macro`` and ``apply_macro``. Agents only, off by default.

Enabled by ``ZAMMAD_ENABLE_MACROS=true``. A macro can close, reassign and tag
many tickets at once, so applying one takes two calls, like email replies:
``prepare_apply_macro`` returns what the macro does and a confirmation token
bound to the caller, the macro and the exact ticket list; ``apply_macro``
spends the token and runs the macro.

- Macros are applied with ``POST /tickets/mass_macro``, which runs the whole
  macro and checks its group restriction and the caller's change access on
  every ticket, all in one transaction. ``PUT /tickets/:id`` with a macro id
  silently does nothing, so it is never used.
- Zammad runs inactive macros too, so this module lists only active ones and
  refuses an inactive macro at both steps.
- Macro names, notes and actions are written by Zammad admins, like group and
  state names, and are returned as they are.
"""

from __future__ import annotations

import logging
from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from zammad_mcp.client import ZammadError, to_tool_error
from zammad_mcp.client.errors import UnprocessableError
from zammad_mcp.client.models import Macro
from zammad_mcp.confirmations import ConfirmationError
from zammad_mcp.tiers import Tier
from zammad_mcp.tools.context import (
    IRREVERSIBLE,
    PREPARE,
    READ,
    ToolContext,
    ZammadSession,
    as_dict,
    as_list,
    tier_error,
)

MODULE = "macros"
ACTION = "apply_macro"
MAX_MACRO_TICKETS = 50
CONFIRMATION_STORE_DOWN = "Error: the confirmation store is unavailable, so nothing was changed; try again shortly"

logger = logging.getLogger(__name__)

MacroId = Annotated[int, Field(ge=1, description="From list_macros")]
TicketIds = Annotated[list[Annotated[int, Field(ge=1)]], Field(min_length=1, max_length=MAX_MACRO_TICKETS)]


class InactiveMacroError(Exception):
    pass


def macro_payload(macro_id: int, ticket_ids: list[int]) -> dict[str, Any]:
    """What a confirmation binds to; the ticket list is order- and duplicate-insensitive."""
    return {"macro_id": macro_id, "ticket_ids": sorted(set(ticket_ids))}


def shape_macro(macro: Macro) -> dict[str, Any]:
    shaped = {"id": macro.id, "name": macro.name, "note": macro.note, "group_ids": macro.group_ids}
    return {key: value for key, value in shaped.items() if value not in (None, "", [])}


async def active_macro(session: ZammadSession, macro_id: int) -> Macro:
    macro = Macro.parse(await session.get(f"/macros/{macro_id}"))
    if macro.active is not True:
        raise InactiveMacroError(f"macro {macro_id} ({macro.name}) is inactive; only active macros can be applied")
    return macro


def mass_macro_failure(error: ZammadError) -> str:
    if isinstance(error, UnprocessableError):
        return (
            f"Error: Zammad refused to apply the macro and changed nothing (HTTP 422: {error.detail}); "
            "the macro's group restriction may not cover a ticket, or you cannot change one of them"
        )
    return to_tool_error(error)


def register(mcp: FastMCP, context: ToolContext) -> None:
    @mcp.tool(**context.tool("List macros", READ, module=MODULE, tier=Tier.AGENT))
    async def list_macros() -> dict[str, Any] | str:
        """List the active macros you can apply, with the group ids each is limited to (none: every group)."""
        try:
            session = await context.session()
            if session.lacks(Tier.AGENT):
                return tier_error(Tier.AGENT, session.tier)
            rows = [Macro.parse(row) for row in as_list(await session.get("/macros")) if isinstance(row, dict)]
        except ZammadError as error:
            return to_tool_error(error)
        return {"macros": [shape_macro(macro) for macro in rows if macro.active is True]}

    if not context.writes_enabled:
        return
    confirmations = context.confirmations

    @mcp.tool(**context.tool("Prepare to apply macro", PREPARE, module=MODULE, tier=Tier.AGENT))
    async def prepare_apply_macro(macro_id: MacroId, ticket_ids: TicketIds) -> dict[str, Any] | str:
        """Preview applying a macro to tickets and get a confirmation token. Nothing changes yet.

        Show the preview to the user. Only once they approve it, call
        apply_macro with the token and the same macro_id and ticket_ids.
        """
        try:
            session = await context.session()
            if session.lacks(Tier.AGENT):
                return tier_error(Tier.AGENT, session.tier)
            macro = await active_macro(session, macro_id)
        except InactiveMacroError as error:
            return f"Error: {error}"
        except ZammadError as error:
            return to_tool_error(error)
        payload = macro_payload(macro_id, ticket_ids)
        try:
            token = await confirmations.issue(identity=session.credential.identity, action=ACTION, payload=payload)
        except Exception:
            logger.warning("could not store a macro confirmation", exc_info=True)
            return CONFIRMATION_STORE_DOWN
        return {
            "confirmation_token": token,
            "expires_in_seconds": confirmations.ttl_seconds,
            "macro": {**shape_macro(macro), "changes": macro.perform},
            "ticket_ids": payload["ticket_ids"],
            "next_step": (
                "Show this preview to the user. Only after they approve it, call apply_macro with "
                "confirmation_token and exactly these macro_id and ticket_ids."
            ),
        }

    @mcp.tool(**context.tool("Apply macro", IRREVERSIBLE, module=MODULE, tier=Tier.AGENT))
    async def apply_macro(
        confirmation_token: Annotated[str, Field(min_length=1, max_length=200)],
        macro_id: MacroId,
        ticket_ids: TicketIds,
    ) -> dict[str, Any] | str:
        """Apply a macro the user approved, from prepare_apply_macro's preview. The token works once."""
        try:
            session = await context.session()
        except ZammadError as error:
            return to_tool_error(error)
        if session.lacks(Tier.AGENT):
            return tier_error(Tier.AGENT, session.tier)
        payload = macro_payload(macro_id, ticket_ids)
        try:
            await confirmations.consume(
                confirmation_token, identity=session.credential.identity, action=ACTION, payload=payload
            )
        except ConfirmationError as error:
            return f"Error: {error}"
        except Exception:
            logger.warning("could not read a macro confirmation", exc_info=True)
            return CONFIRMATION_STORE_DOWN
        try:
            await active_macro(session, macro_id)
            applied = as_dict(await session.post("/tickets/mass_macro", payload))
        except InactiveMacroError as error:
            return f"Error: {error}"
        except ZammadError as error:
            return mass_macro_failure(error)
        changed = [item for item in as_list(applied.get("ticket_ids")) if isinstance(item, int)]
        return {
            "macro_id": macro_id,
            "ticket_ids": changed,
            "status": "applied",
            "urls": [context.links.ticket(ticket_id) for ticket_id in changed],
        }
