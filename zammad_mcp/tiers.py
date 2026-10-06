"""Who a tool is for, and the ``auth=`` check that hides it from everyone else.

Each tool declares the lowest tier it needs as a tag (``tier:customer`` or
``tier:agent``). :func:`require_tier` builds one FastMCP ``AuthCheck`` that
reads that tag and compares it with the caller's tier.

FastMCP 3.4.7 facts this relies on (``fastmcp/utilities/authorization.py``
and ``fastmcp/server/server.py``):

- A check receives ``AuthContext(token, component)`` and may be async.
- It runs for ``tools/list``, ``get_tool`` and ``tools/call``; a denied tool
  is left out of the list and reported as unknown when called.
- On stdio FastMCP skips every check. Over HTTP without an auth provider
  ``token`` is ``None``.
- Any exception other than ``AuthorizationError`` is logged and treated as
  a denial, so the resolver must never raise.

The filter is a convenience for the model. Zammad still enforces every
permission on every call.
"""

from __future__ import annotations

from collections.abc import Awaitable, Callable, Iterable
from enum import IntEnum
from typing import TYPE_CHECKING

from fastmcp.server.auth import AuthCheck, AuthContext

from zammad_mcp.client.errors import ZammadError

if TYPE_CHECKING:
    from fastmcp.server.auth import AccessToken

    from zammad_mcp.credentials.base import CredentialProvider

TIER_TAG_PREFIX = "tier:"
AGENT_PERMISSION = "ticket.agent"


class Tier(IntEnum):
    CUSTOMER = 1
    AGENT = 2

    @property
    def tag(self) -> str:
        return f"{TIER_TAG_PREFIX}{self.name.lower()}"


TierResolver = Callable[["AccessToken | None"], Awaitable[Tier | None]]


def has_permission(permissions: Iterable[str], wanted: str) -> bool:
    """Zammad semantics: holding ``ticket`` also grants ``ticket.agent``."""
    return any(held == wanted or wanted.startswith(f"{held}.") for held in permissions)


def tier_for(permissions: Iterable[str]) -> Tier:
    return Tier.AGENT if has_permission(permissions, AGENT_PERMISSION) else Tier.CUSTOMER


_TIERS_BY_TAG = {tier.tag: tier for tier in Tier}


def required_tier(tags: Iterable[str]) -> Tier | None:
    return max((_TIERS_BY_TAG[tag] for tag in tags if tag in _TIERS_BY_TAG), default=None)


def tier_allows(caller: Tier | None, needed: Tier | None) -> bool:
    return needed is None or caller is None or caller >= needed


def require_tier(resolver: TierResolver) -> AuthCheck:
    """Build the ``auth=`` check shared by every tool.

    An unknown caller tier (``None``) means "do not filter".
    """

    async def check(ctx: AuthContext) -> bool:
        needed = required_tier(ctx.component.tags)
        if needed is None:
            return True
        return tier_allows(await resolver(ctx.token), needed)

    return check


async def no_filtering(_token: AccessToken | None) -> Tier | None:
    return None


def credential_tier_resolver(credentials: CredentialProvider) -> TierResolver:
    async def resolve(_token: AccessToken | None) -> Tier | None:
        try:
            return (await credentials.resolve()).tier
        except ZammadError:
            return None

    return resolve


def community_tier_resolver(credentials: CredentialProvider, *, filter_by_role: bool) -> TierResolver:
    """Community mode shows every tool unless ``ZAMMAD_FILTER_TOOLS_BY_ROLE`` is on."""
    return credential_tier_resolver(credentials) if filter_by_role else no_filtering
