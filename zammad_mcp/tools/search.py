"""``search``: Zammad's global search across tickets, users and organizations."""

from __future__ import annotations

from typing import Annotated, Any, Literal

from fastmcp import FastMCP
from pydantic import Field

from zammad_mcp.client import ZammadError, to_tool_error
from zammad_mcp.shaping import UNTRUSTED_NOTICE, Links, frame_untrusted
from zammad_mcp.tiers import Tier
from zammad_mcp.tools.context import READ, ToolContext

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
            "title": frame_untrusted(asset.get("title"), source="ticket_title", item_id=item_id),
            "url": links.ticket(item_id),
        }
    if kind == "User":
        return {
            "type": kind,
            "id": item_id,
            "name": _full_name(asset),
            "email": asset.get("email"),
            "url": links.user(item_id),
        }
    if kind == "Organization":
        return {"type": kind, "id": item_id, "name": asset.get("name"), "url": links.organization(item_id)}
    return {"type": kind, "id": item_id}


def shape_results(body: dict[str, Any], links: Links) -> list[dict[str, Any]]:
    assets = body.get("assets") or {}
    return [
        shape_hit(hit["type"], hit["id"], (assets.get(hit["type"]) or {}).get(str(hit["id"])) or {}, links)
        for hit in body.get("result") or []
        if "type" in hit and "id" in hit
    ]


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
            body = await session.get("/search", params) or {}
        except ZammadError as error:
            return to_tool_error(error)
        return {"results": shape_results(body, context.links), "notice": UNTRUSTED_NOTICE}
