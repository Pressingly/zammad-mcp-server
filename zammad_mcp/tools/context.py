"""What every tool module needs, and the per-call Zammad session.

Tools call ``await context.session()`` once, then talk to Zammad through the
returned :class:`ZammadSession`, which carries the caller's token and tier.
"""

from __future__ import annotations

from dataclasses import dataclass
from typing import Any

from fastmcp.server.auth import AuthCheck
from mcp.types import ToolAnnotations

from zammad_mcp.client import Download, ZammadClient
from zammad_mcp.confirmations import Confirmations
from zammad_mcp.credentials import CredentialProvider, ZammadCredential
from zammad_mcp.shaping import Links
from zammad_mcp.tiers import Tier, tier_allows


@dataclass(frozen=True)
class ZammadSession:
    client: ZammadClient
    credential: ZammadCredential

    @property
    def tier(self) -> Tier | None:
        return self.credential.tier

    def lacks(self, tier: Tier) -> bool:
        return not tier_allows(self.tier, tier)

    async def get(self, path: str, params: dict[str, Any] | None = None) -> Any:
        return await self.client.get(path, token=self.credential.token, params=params)

    async def search(self, path: str, params: dict[str, Any], body: dict[str, Any]) -> Any:
        return await self.client.request(
            "POST", path, token=self.credential.token, params=params, json=body, retry_safe=True
        )

    async def post(self, path: str, body: dict[str, Any], params: dict[str, Any] | None = None) -> Any:
        return await self.client.request("POST", path, token=self.credential.token, params=params, json=body)

    async def put(self, path: str, body: dict[str, Any], params: dict[str, Any] | None = None) -> Any:
        """PUT is retried on timeouts, so callers must never put an ``article`` in the body."""
        return await self.client.request("PUT", path, token=self.credential.token, params=params, json=body)

    async def download(self, path: str, *, max_bytes: int) -> Download:
        return await self.client.download(path, token=self.credential.token, max_bytes=max_bytes)


def tier_error(tier: Tier) -> str:
    return f"Error: this tool needs a Zammad {tier.name.lower()} account; your token belongs to a customer"


@dataclass(frozen=True)
class ToolSpec:
    """Annotation presets; every tool picks one so all four hints are always set."""

    read_only: bool
    destructive: bool
    idempotent: bool

    def annotations(self, title: str) -> ToolAnnotations:
        return ToolAnnotations(
            title=title,
            readOnlyHint=self.read_only,
            destructiveHint=self.destructive,
            idempotentHint=self.idempotent,
            openWorldHint=True,
        )


READ = ToolSpec(read_only=True, destructive=False, idempotent=True)
CREATE = ToolSpec(read_only=False, destructive=False, idempotent=False)
OVERWRITE = ToolSpec(read_only=False, destructive=True, idempotent=True)


@dataclass(frozen=True)
class ToolContext:
    client: ZammadClient
    credentials: CredentialProvider
    links: Links
    tier_check: AuthCheck
    confirmations: Confirmations
    read_only: bool = False

    async def session(self) -> ZammadSession:
        return ZammadSession(self.client, await self.credentials.resolve())

    def tool(self, title: str, spec: ToolSpec, *, module: str, tier: Tier) -> dict[str, Any]:
        """Keyword arguments for ``@mcp.tool``: annotations, tier and module tags, and the tier check."""
        kind = "read" if spec.read_only else "write"
        return {
            "annotations": spec.annotations(title),
            "tags": {tier.tag, f"module:{module}", kind},
            "auth": self.tier_check,
        }

    @property
    def writes_enabled(self) -> bool:
        return not self.read_only
