"""Community mode: one operator-supplied token from ``ZAMMAD_HTTP_TOKEN``.

The token's tier is read once (first use, or the lifespan warm-up) and kept
for the life of the process. It only hides tools when
``ZAMMAD_FILTER_TOOLS_BY_ROLE`` is on.
"""

from __future__ import annotations

from zammad_mcp.client import ZammadClient
from zammad_mcp.client.errors import MissingTokenError
from zammad_mcp.credentials.base import ProfileMemo, ZammadCredential, fetch_profile, token_identity


class StaticCredentialProvider:
    def __init__(self, client: ZammadClient, token: str | None, *, memo: ProfileMemo | None = None) -> None:
        self._client = client
        self._token = token
        self._memo = memo or ProfileMemo(ttl_seconds=None)

    async def resolve(self) -> ZammadCredential:
        if not self._token:
            raise MissingTokenError("no Zammad API token is configured for this request")
        token = self._token
        profile = await self._memo.get(token_identity(token), lambda: fetch_profile(self._client, token))
        return ZammadCredential.for_token(token, profile)

    async def warm(self) -> None:
        if self._token:
            await self.resolve()
