"""``search``: Zammad's global search across tickets, users and organizations."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from zammad_mcp.client import ZammadError, to_tool_error
from zammad_mcp.shaping import UNTRUSTED_NOTICE, Links, untrusted_field
from zammad_mcp.tiers import Tier
from zammad_mcp.tools.context import READ, ToolContext, as_dict, as_list

MODULE = "search"
SearchObject = Literal["Ticket", "User", "Organization"]
DEFAULT_OBJECTS: tuple[SearchObject, ...] = ("Ticket", "User", "Organization")


def _full_name(asset: dict[str, Any]) -> str | None:
    name = " ".join(part for part in (asset.get("firstname"), asset.get("lastname")) if part)
    return name or asset.get("login")


def shape_hit(kind: str, item_id: int, asset: dict[str, Any], links: Links) -> dict[str, Any]:
    if kind == "Ticket":
        return {
            "type": kind,
            "id": item_id,
            "number": asset.get("number"),
            "title": untrusted_field(asset.get("title"), source="ticket_title", item_id=item_id),
            "url": links.ticket(item_id),
        }
    if kind == "User":
        return {
            "type": kind,
            "id": item_id,
            "name": untrusted_field(_full_name(asset), source="user_name", item_id=item_id),
            "email": untrusted_field(asset.get("email"), source="user_email", item_id=item_id),
            "url": links.user(item_id),
        }
    if kind == "Organization":
        name = untrusted_field(asset.get("name"), source="organization_name", item_id=item_id)
        return {"type": kind, "id": item_id, "name": name, "url": links.organization(item_id)}
    return {"type": kind, "id": item_id}


def shape_results(body: dict[str, Any], links: Links) -> list[dict[str, Any]]:
    assets = as_dict(body.get("assets"))
    return [
        shape_hit(hit["type"], hit["id"], as_dict(as_dict(assets.get(hit["type"])).get(str(hit["id"]))), links)
        for hit in as_list(body.get("result"))
        if _is_hit(hit)
    ]


def _is_hit(hit: Any) -> bool:
    return isinstance(hit, dict) and isinstance(hit.get("type"), str) and isinstance(hit.get("id"), int)


def register(mcp: FastMCP, context: ToolContext) -> None:
    @mcp.tool(**context.tool("Search Zammad", READ, module=MODULE, tier=Tier.CUSTOMER))
    async def search(
        query: Annotated[str, Field(min_length=1)],
        objects: Annotated[list[SearchObject] | None, Field(description="Object types to search; default all")] = None,
        limit: Annotated[int, Field(ge=1, le=50, description="Results per object type")] = 10,
    ) -> dict[str, Any] | str:
        """Search tickets, users and organizations at once, the way the Zammad search bar does.

        Without Elasticsearch this is a substring match with no ranking; for
        filtered ticket lists use search_tickets.
        """
        params = {"query": query.strip(), "limit": limit, "objects": "-".join(objects or DEFAULT_OBJECTS)}
        try:
            session = await context.session()
            body = as_dict(await session.get("/search", params))
        except ZammadError as error:
            return to_tool_error(error)
        return {"results": shape_results(body, context.links), "notice": UNTRUSTED_NOTICE}
