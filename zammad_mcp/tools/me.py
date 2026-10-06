"""``get_me``: the smoke tool that proves the token and the connection work."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from zammad_mcp.client import ZammadError, to_tool_error
from zammad_mcp.client.models import User
from zammad_mcp.shaping import shape_user
from zammad_mcp.tiers import Tier
from zammad_mcp.tools.context import READ, ToolContext


def register(mcp: FastMCP, context: ToolContext) -> None:
    @mcp.tool(**context.tool("Get current Zammad user", READ, module="me", tier=Tier.CUSTOMER))
    async def get_me() -> dict[str, Any] | str:
        """Return the Zammad user the current token belongs to: id, login, name, email, roles and tier."""
        try:
            session = await context.session()
            user = User.model_validate(await session.get("/users/me", {"expand": "true"}))
        except ZammadError as error:
            return to_tool_error(error)
        shaped = shape_user(user, context.links)
        if session.tier is not None:
            shaped["tier"] = session.tier.name.lower()
        return shaped
