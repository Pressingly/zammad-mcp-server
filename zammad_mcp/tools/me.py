"""``get_me``: the smoke tool that proves the token and the connection work."""

from __future__ import annotations

from typing import Any

from fastmcp import FastMCP
from mcp.types import ToolAnnotations

from zammad_mcp.client import ZammadError, to_tool_error
from zammad_mcp.tools.context import ToolContext

_USER_FIELDS = ("id", "login", "firstname", "lastname", "email", "organization", "roles", "active")


def shape_user(user: dict[str, Any]) -> dict[str, Any]:
    return {field: user[field] for field in _USER_FIELDS if field in user}


def register(mcp: FastMCP, context: ToolContext) -> None:
    @mcp.tool(
        annotations=ToolAnnotations(
            title="Get current Zammad user",
            readOnlyHint=True,
            destructiveHint=False,
            idempotentHint=True,
            openWorldHint=True,
        ),
    )
    async def get_me() -> dict[str, Any] | str:
        """Return the Zammad user the current token belongs to (id, login, name, email, roles)."""
        try:
            user = await context.client.get("/users/me", token=context.token(), params={"expand": "true"})
        except ZammadError as error:
            return to_tool_error(error)
        return shape_user(user)
