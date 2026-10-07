"""Ticket tags: ``list_ticket_tags`` for everyone, ``add_ticket_tag`` and ``remove_ticket_tag`` for agents.

Zammad answers a tag write by a customer with 403, so the write tools refuse
customers (and callers whose tier is unknown) before calling it.

Creating a tag that does not exist yet needs Zammad's ``tag_new`` setting (or
``admin.tag``). Without it Zammad answers the same bare 403 it uses for "no
change access to this ticket", so the add tool looks the tag up to say which
of the two it was.
"""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from zammad_mcp.client import ZammadError, to_tool_error
from zammad_mcp.client.errors import PermissionDenied
from zammad_mcp.shaping import untrusted_field
from zammad_mcp.tiers import Tier
from zammad_mcp.tools.context import ADD, OVERWRITE, READ, ToolContext, ZammadSession, as_dict, as_list, tier_error

MODULE = "tags"
TAG_SEARCH_LIMIT = 50

TicketId = Annotated[int, Field(ge=1)]
TagName = Annotated[str, Field(min_length=1, max_length=100, description="The tag, e.g. 'billing'")]


def tag_params(ticket_id: int, tag: str) -> dict[str, Any]:
    return {"object": "Ticket", "o_id": ticket_id, "item": tag}


def framed_tag(tag: str, ticket_id: int) -> str | None:
    return untrusted_field(tag, source="ticket_tag", item_id=ticket_id)


async def tag_exists(session: ZammadSession, tag: str) -> bool | None:
    """Whether Zammad already knows ``tag``; ``None`` when the lookup itself fails.

    ``/tag_search`` is a substring match, so only an exact name counts.
    """
    try:
        rows = as_list(await session.get("/tag_search", {"term": tag, "limit": TAG_SEARCH_LIMIT}))
    except ZammadError:
        return None
    return any(isinstance(row, dict) and row.get("value") == tag for row in rows)


async def explain_add_refusal(session: ZammadSession, error: PermissionDenied, tag: str) -> str:
    exists = await tag_exists(session, tag)
    if exists is False:
        return (
            "Error: Zammad refused the tag (HTTP 403): it does not exist yet and this Zammad does not let agents "
            "create new tags (admin setting 'tag_new'); use an existing tag or ask an admin to create it"
        )
    if exists is True:
        return to_tool_error(error)
    return (
        f"Error: Zammad refused the tag (HTTP 403: {error.detail}); either you cannot change this ticket, or the tag "
        "does not exist and this Zammad does not let agents create new tags (admin setting 'tag_new')"
    )


def is_plain_permission_denial(error: ZammadError) -> bool:
    """A role 403, not one of the gateway, inactive-account or maintenance variants."""
    return type(error) is PermissionDenied


def register(mcp: FastMCP, context: ToolContext) -> None:
    @mcp.tool(**context.tool("List ticket tags", READ, module=MODULE, tier=Tier.CUSTOMER))
    async def list_ticket_tags(ticket_id: TicketId) -> dict[str, Any] | str:
        """List the tags on a ticket."""
        try:
            session = await context.session()
            body = as_dict(await session.get("/tags", {"object": "Ticket", "o_id": ticket_id}))
        except ZammadError as error:
            return to_tool_error(error)
        tags = [framed_tag(str(tag), ticket_id) for tag in as_list(body.get("tags"))]
        return {"ticket_id": ticket_id, "tags": tags}

    if not context.writes_enabled:
        return

    @mcp.tool(**context.tool("Add ticket tag", ADD, module=MODULE, tier=Tier.AGENT))
    async def add_ticket_tag(ticket_id: TicketId, tag: TagName) -> dict[str, Any] | str:
        """Add a tag to a ticket. Agents only; adding a tag the ticket already has changes nothing.

        A tag that does not exist in Zammad yet is created only if Zammad's
        'tag_new' setting allows it.
        """
        name = tag.strip()
        if not name:
            return "Error: the tag must not be blank"
        try:
            session = await context.session()
        except ZammadError as error:
            return to_tool_error(error)
        if session.lacks(Tier.AGENT):
            return tier_error(Tier.AGENT, session.tier)
        try:
            await session.post("/tags/add", tag_params(ticket_id, name))
        except ZammadError as error:
            if is_plain_permission_denial(error):
                return await explain_add_refusal(session, error, name)
            return to_tool_error(error)
        return {"ticket_id": ticket_id, "added": framed_tag(name, ticket_id)}

    @mcp.tool(**context.tool("Remove ticket tag", OVERWRITE, module=MODULE, tier=Tier.AGENT))
    async def remove_ticket_tag(ticket_id: TicketId, tag: TagName) -> dict[str, Any] | str:
        """Remove a tag from a ticket. Agents only; removing a tag the ticket does not have changes nothing."""
        name = tag.strip()
        if not name:
            return "Error: the tag must not be blank"
        try:
            session = await context.session()
            if session.lacks(Tier.AGENT):
                return tier_error(Tier.AGENT, session.tier)
            await session.delete("/tags/remove", tag_params(ticket_id, name))
        except ZammadError as error:
            return to_tool_error(error)
        return {"ticket_id": ticket_id, "removed": framed_tag(name, ticket_id)}
