"""``list_ticket_tags``. Adding and removing tags lands with the next toolset."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from zammad_mcp.client import ZammadError, to_tool_error
from zammad_mcp.tiers import Tier
from zammad_mcp.tools.context import READ, ToolContext

MODULE = "tags"


def register(mcp: FastMCP, context: ToolContext) -> None:
    @mcp.tool(**context.tool("List ticket tags", READ, module=MODULE, tier=Tier.CUSTOMER))
    async def list_ticket_tags(ticket_id: Annotated[int, Field(ge=1)]) -> dict[str, Any] | str:
        """List the tags on a ticket."""
        try:
            session = await context.session()
            body = await session.get("/tags", {"object": "Ticket", "o_id": ticket_id}) or {}
        except ZammadError as error:
            return to_tool_error(error)
        return {"ticket_id": ticket_id, "tags": list(body.get("tags") or [])}
