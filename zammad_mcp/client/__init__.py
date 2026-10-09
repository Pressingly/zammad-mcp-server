"""Zammad REST client and its error types."""

from zammad_mcp.client.errors import ZammadError, to_tool_error
from zammad_mcp.client.http import Download, RetryPolicy, ZammadClient

__all__ = ["Download", "RetryPolicy", "ZammadClient", "ZammadError", "to_tool_error"]
