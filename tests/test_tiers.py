from __future__ import annotations

import pytest
from fastmcp.server.auth import AuthContext

from zammad_mcp.client.errors import MissingTokenError
from zammad_mcp.tiers import (
    Tier,
    community_tier_resolver,
    credential_tier_resolver,
    has_permission,
    no_filtering,
    require_tier,
    required_tier,
    tier_for,
)


class FakeComponent:
    def __init__(self, *tags: str) -> None:
        self.tags = set(tags)


def fixed(tier: Tier | None):
    async def resolve(_token):
        return tier

    return resolve


@pytest.mark.parametrize(
    ("held", "wanted", "expected"),
    [
        ({"ticket.agent"}, "ticket.agent", True),
        ({"ticket"}, "ticket.agent", True),
        ({"ticket.customer"}, "ticket.agent", False),
        ({"ticket.agentx"}, "ticket.agent", False),
        (set(), "ticket.agent", False),
    ],
)
def test_has_permission(held, wanted, expected):
    assert has_permission(held, wanted) is expected


def test_tier_for():
    assert tier_for({"ticket.agent", "admin"}) is Tier.AGENT
    assert tier_for({"ticket.customer"}) is Tier.CUSTOMER
    assert tier_for(set()) is Tier.CUSTOMER


def test_required_tier_reads_tags():
    assert required_tier({"tier:agent", "module:users"}) is Tier.AGENT
    assert required_tier({"tier:customer"}) is Tier.CUSTOMER
    assert required_tier({"tier:admin", "read"}) is None
    assert Tier.AGENT.tag == "tier:agent"


async def test_community_check_allows_with_no_token():
    """Regression: community HTTP has no auth provider, so FastMCP passes token=None."""
    check = require_tier(no_filtering)
    for tags in ({"tier:agent"}, {"tier:customer"}, set()):
        assert await check(AuthContext(token=None, component=FakeComponent(*tags))) is True


@pytest.mark.parametrize(
    ("caller", "tags", "expected"),
    [
        (Tier.CUSTOMER, {"tier:agent"}, False),
        (Tier.CUSTOMER, {"tier:customer"}, True),
        (Tier.AGENT, {"tier:agent"}, True),
        (None, {"tier:agent"}, True),
        (Tier.CUSTOMER, set(), True),
    ],
)
async def test_require_tier_compares_caller_with_tag(caller, tags, expected):
    check = require_tier(fixed(caller))
    assert await check(AuthContext(token=None, component=FakeComponent(*tags))) is expected


class FailingProvider:
    async def resolve(self):
        raise MissingTokenError("none")


async def test_credential_resolver_never_raises():
    assert await credential_tier_resolver(FailingProvider())(None) is None
    assert community_tier_resolver(FailingProvider(), filter_by_role=False) is no_filtering
