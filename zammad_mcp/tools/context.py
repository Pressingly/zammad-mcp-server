"""What every tool module needs: the shared client and a token source."""

from __future__ import annotations

from collections.abc import Callable
from dataclasses import dataclass

from zammad_mcp.client import ZammadClient

TokenSource = Callable[[], str | None]


@dataclass(frozen=True)
class ToolContext:
    client: ZammadClient
    token: TokenSource
