"""``get_me``: the smoke tool that proves the token and the connection work."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from zammad_mcp.client import ZammadError, to_tool_error
from zammad_mcp.client.models import User
from zammad_mcp.shaping import shape_user
from zammad_mcp.tiers import Tier
from zammad_mcp.tools.context import READ, ToolContext


def describe_tier(tier: Tier | None) -> str:
    return "unknown" if tier is None else tier.name.lower()


def register(mcp: FastMCP, context: ToolContext) -> None:
    @mcp.tool(**context.tool("Get current Zammad user", READ, module="me", tier=Tier.NONE))
    async def get_me() -> dict[str, Any] | str:
        """Return the Zammad user the current token belongs to: id, login, name, email, roles and tier.

        ``tier`` is ``agent``, ``customer``, ``none`` (no ticket permissions,
        e.g. an Admin-only account) or ``unknown`` (the role lookup failed).
        """
        try:
            session = await context.session()
            user = User.parse(await session.get("/users/me", {"expand": "true"}))
        except ZammadError as error:
            return to_tool_error(error)
        return {**shape_user(user, context.links), "tier": describe_tier(session.tier)}
