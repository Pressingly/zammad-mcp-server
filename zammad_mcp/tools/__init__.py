"""MCP tool modules, one per Zammad domain.

Each module exposes ``register(mcp, context)``. Every tool carries all four
MCP ``ToolAnnotations`` hints, a ``tier:*`` tag and a ``module:*`` tag, and
returns an ``Error: ...`` string instead of raising.

A module whose ``ZAMMAD_ENABLE_<MODULE>`` flag is false never registers, and
``ZAMMAD_READ_ONLY`` keeps every write tool unregistered. ``get_me`` is
always on. Every module has one flag; ``email_replies`` is off by default.
The Phase 6 follow-ups each add a module and a flag.
"""

from __future__ import annotations

import dataclasses
from types import ModuleType

from fastmcp import FastMCP

from zammad_mcp.config import Settings
from zammad_mcp.tools import (
    articles,
    attachments,
    email,
    kb,
    me,
    organizations,
    reference,
    search,
    tags,
    tickets,
    users,
)
from zammad_mcp.tools.context import ToolContext

__all__ = ["MODULES", "ToolContext", "register_tools"]

MODULES: dict[str, ModuleType] = {
    "reference": reference,
    "tickets": tickets,
    "articles": articles,
    "attachments": attachments,
    "search": search,
    "users": users,
    "organizations": organizations,
    "tags": tags,
    "kb": kb,
    "email_replies": email,
}


def register_tools(mcp: FastMCP, context: ToolContext, settings: Settings) -> None:
    context = dataclasses.replace(context, email_allow_any_recipient=settings.email_allow_any_recipient)
    me.register(mcp, context)
    for name, module in MODULES.items():
        if settings.module_enabled(name):
            module.register(mcp, context)
