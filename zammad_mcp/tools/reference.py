"""``list_ticket_options``: the states, priorities and groups a ticket can use."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP

from zammad_mcp.client import ZammadError, to_tool_error
from zammad_mcp.tiers import Tier
from zammad_mcp.tools.context import READ, ToolContext, ZammadSession, as_list

EXPAND = {"expand": "true"}
_SECTIONS = {
    "states": ("/ticket_states", ("id", "name", "state_type")),
    "priorities": ("/ticket_priorities", ("id", "name")),
    "groups": ("/groups", ("id", "name")),
}


def _active_options(rows: Any, fields: tuple[str, ...]) -> list[dict[str, Any]]:
    return [
        {field: row.get(field) for field in fields if field in row}
        for row in as_list(rows)
        if isinstance(row, dict) and row.get("active", True)
    ]


async def _section(session: ZammadSession, path: str, fields: tuple[str, ...]) -> list[dict[str, Any]] | str:
    try:
        return _active_options(await session.get(path, EXPAND), fields)
    except ZammadError as error:
        return to_tool_error(error)


def register(mcp: FastMCP, context: ToolContext) -> None:
    @mcp.tool(**context.tool("List ticket options", READ, module="reference", tier=Tier.CUSTOMER))
    async def list_ticket_options() -> dict[str, Any] | str:
        """List the active ticket states, priorities and groups, with ids and names.

        Use the names when creating or updating tickets and the ids as
        search_tickets filters. A section the token cannot read holds an error
        string instead of a list.
        """
        try:
            session = await context.session()
        except ZammadError as error:
            return to_tool_error(error)
        return {name: await _section(session, path, fields) for name, (path, fields) in _SECTIONS.items()}
