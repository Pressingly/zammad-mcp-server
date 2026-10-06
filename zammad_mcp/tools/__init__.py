"""MCP tool modules, one per Zammad domain.

Each module exposes ``register(mcp, context)``. Every tool carries all four
MCP ``ToolAnnotations`` hints and returns an error string instead of raising.

Phase 2 (FOSS-512) adds: reference, tickets, articles, attachments, search,
users, organizations, tags, kb and email. Phase 6 adds the flagged follow-up
toolsets (links, mentions, overviews, macros, ...).
"""

from __future__ import annotations

from fastmcp import FastMCP

from zammad_mcp.tools import me
from zammad_mcp.tools.context import ToolContext

__all__ = ["ToolContext", "register_tools"]


def register_tools(mcp: FastMCP, context: ToolContext) -> None:
    me.register(mcp, context)
