"""Organization tools: read one organization, search organizations (agents)."""

from __future__ import annotations

from typing import Annotated, Any

from fastmcp import FastMCP
from pydantic import Field

from zammad_mcp.client import ZammadError, to_tool_error
from zammad_mcp.client.models import Organization
from zammad_mcp.client.pagination import PageRequest, page_from_response
from zammad_mcp.shaping import shape_organization
from zammad_mcp.tiers import Tier
from zammad_mcp.tools.context import READ, ToolContext, tier_error

MODULE = "organizations"
EXPAND = {"expand": "true"}


def register(mcp: FastMCP, context: ToolContext) -> None:
    links = context.links

    @mcp.tool(**context.tool("Get organization", READ, module=MODULE, tier=Tier.CUSTOMER))
    async def get_organization(organization_id: Annotated[int, Field(ge=1)]) -> dict[str, Any] | str:
        """Get one organization. Customers can only read their own."""
        try:
            session = await context.session()
            organization = Organization.model_validate(await session.get(f"/organizations/{organization_id}", EXPAND))
        except ZammadError as error:
            return to_tool_error(error)
        return shape_organization(organization, links)

    @mcp.tool(**context.tool("Search organizations", READ, module=MODULE, tier=Tier.AGENT))
    async def search_organizations(
        query: Annotated[str, Field(min_length=1, description="Name or domain fragment")],
        page: Annotated[int, Field(ge=1)] = 1,
        per_page: Annotated[int, Field(ge=1, le=100)] = 25,
    ) -> dict[str, Any] | str:
        """Search organizations by name or domain (substring match). Agents only."""
        paging = PageRequest.of(page, per_page)
        params = {**EXPAND, "with_total_count": "true", "query": query.strip(), **paging.params()}
        try:
            session = await context.session()
            if session.lacks(Tier.AGENT):
                return tier_error(Tier.AGENT)
            found = await session.get("/organizations/search", params)
        except ZammadError as error:
            return to_tool_error(error)
        page_of_orgs = page_from_response(found, paging)
        return page_of_orgs.map(lambda row: shape_organization(Organization.model_validate(row), links)).to_dict(
            "organizations"
        )
