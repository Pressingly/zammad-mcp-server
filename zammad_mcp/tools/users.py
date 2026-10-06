"""User tools: read one user, search users (agents)."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from zammad_mcp.client import ZammadError, to_tool_error
from zammad_mcp.client.models import User
from zammad_mcp.client.pagination import PageRequest, page_from_response
from zammad_mcp.shaping import shape_user
from zammad_mcp.tiers import Tier
from zammad_mcp.tools.context import READ, ToolContext, tier_error

MODULE = "users"
EXPAND = {"expand": "true"}


def register(mcp: FastMCP, context: ToolContext) -> None:
    links = context.links

    @mcp.tool(**context.tool("Get user", READ, module=MODULE, tier=Tier.CUSTOMER))
    async def get_user(user_id: Annotated[int, Field(ge=1)]) -> dict[str, Any] | str:
        """Get one Zammad user. Customers can read themselves and members of their organization."""
        try:
            session = await context.session()
            user = User.model_validate(await session.get(f"/users/{user_id}", EXPAND))
        except ZammadError as error:
            return to_tool_error(error)
        return shape_user(user, links)

    @mcp.tool(**context.tool("Search users", READ, module=MODULE, tier=Tier.AGENT))
    async def search_users(
        query: Annotated[str, Field(min_length=1, description="Name, login, email or phone fragment")],
        page: Annotated[int, Field(ge=1)] = 1,
        per_page: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> dict[str, Any] | str:
        """Search users by name, login, email or phone (substring match). Agents only."""
        paging = PageRequest.of(page, per_page)
        params = {**EXPAND, "with_total_count": "true", "query": query.strip(), **paging.params()}
        try:
            session = await context.session()
            if session.lacks(Tier.AGENT):
                return tier_error(Tier.AGENT)
            found = await session.get("/users/search", params)
        except ZammadError as error:
            return to_tool_error(error)
        return (
            page_from_response(found, paging)
            .map(lambda row: shape_user(User.model_validate(row), links))
            .to_dict("users")
        )
