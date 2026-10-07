"""``list_ticket_tags``. Adding and removing tags lands with the next toolset."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from zammad_mcp.client import ZammadError, to_tool_error
from zammad_mcp.shaping import untrusted_field
from zammad_mcp.tiers import Tier
from zammad_mcp.tools.context import READ, ToolContext, as_dict, as_list

MODULE = "tags"


def register(mcp: FastMCP, context: ToolContext) -> None:
    @mcp.tool(**context.tool("List ticket tags", READ, module=MODULE, tier=Tier.CUSTOMER))
    async def list_ticket_tags(ticket_id: Annotated[int, Field(ge=1)]) -> dict[str, Any] | str:
        """List the tags on a ticket."""
        try:
            session = await context.session()
            body = as_dict(await session.get("/tags", {"object": "Ticket", "o_id": ticket_id}))
        except ZammadError as error:
            return to_tool_error(error)
        tags = [untrusted_field(str(tag), source="ticket_tag", item_id=ticket_id) for tag in as_list(body.get("tags"))]
        return {"ticket_id": ticket_id, "tags": tags}
