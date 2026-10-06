"""Zammad REST client and its error types."""

from zammad_mcp.client.errors import ZammadError, describe_error
from zammad_mcp.client.http import ZammadClient

__all__ = ["ZammadClient", "ZammadError", "describe_error"]
