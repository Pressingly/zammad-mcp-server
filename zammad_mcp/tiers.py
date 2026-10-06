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

import logging
from collections.abc import Awaitable, Callable, Iterable
from enum import IntEnum
from typing import TYPE_CHECKING

from fastmcp.server.auth import AuthCheck, AuthContext

from zammad_mcp.client.errors import ZammadError

if TYPE_CHECKING:
    from fastmcp.server.auth import AccessToken

    from zammad_mcp.credentials.base import CredentialProvider

logger = logging.getLogger(__name__)

TIER_TAG_PREFIX = "tier:"
AGENT_PERMISSION = "ticket.agent"
CUSTOMER_PERMISSION = "ticket.customer"


class Tier(IntEnum):
    """``NONE`` is a user with no ``ticket.*`` permission at all, such as an Admin-only account."""

    NONE = 0
    CUSTOMER = 1
    AGENT = 2

    @property
    def label(self) -> str:
        return {Tier.NONE: "an account without ticket permissions", Tier.CUSTOMER: "a customer"}.get(self, "an agent")

    @property
    def tag(self) -> str:
        return f"{TIER_TAG_PREFIX}{self.name.lower()}"


TierResolver = Callable[["AccessToken | None"], Awaitable[Tier | None]]


def has_permission(permissions: Iterable[str], wanted: str) -> bool:
    """Zammad semantics: holding ``ticket`` also grants ``ticket.agent``."""
    return any(held == wanted or wanted.startswith(f"{held}.") for held in permissions)


def tier_for(permissions: Iterable[str]) -> Tier:
    held = frozenset(permissions)
    if has_permission(held, AGENT_PERMISSION):
        return Tier.AGENT
    if has_permission(held, CUSTOMER_PERMISSION):
        return Tier.CUSTOMER
    return Tier.NONE


_TIERS_BY_TAG = {tier.tag: tier for tier in Tier}


def required_tier(tags: Iterable[str]) -> Tier | None:
    return max((_TIERS_BY_TAG[tag] for tag in tags if tag in _TIERS_BY_TAG), default=None)


def tier_allows(caller: Tier | None, needed: Tier | None) -> bool:
    return needed is None or caller is None or caller >= needed


def require_tier(resolver: TierResolver) -> AuthCheck:
    """Build the ``auth=`` check shared by every tool.

    An unknown caller tier (``None``) means "do not filter", and so does a
    resolver that fails: FastMCP would read an exception as a denial and blank
    the tool list.
    """

    async def check(ctx: AuthContext) -> bool:
        needed = required_tier(ctx.component.tags)
        if needed is None:
            return True
        try:
            caller = await resolver(ctx.token)
        except Exception:
            logger.warning("tier resolver failed; showing the tool unfiltered", exc_info=True)
            caller = None
        return tier_allows(caller, needed)

    return check


async def no_filtering(_token: AccessToken | None) -> Tier | None:
    return None


def credential_tier_resolver(credentials: CredentialProvider) -> TierResolver:
    async def resolve(_token: AccessToken | None) -> Tier | None:
        try:
            return (await credentials.resolve()).tier
        except ZammadError:
            return None
        except Exception:
            logger.warning("could not resolve the caller's tier; showing every tool", exc_info=True)
            return None

    return resolve


def community_tier_resolver(credentials: CredentialProvider, *, filter_by_role: bool) -> TierResolver:
    """Community mode shows every tool unless ``ZAMMAD_FILTER_TOOLS_BY_ROLE`` is on."""
    return credential_tier_resolver(credentials) if filter_by_role else no_filtering
