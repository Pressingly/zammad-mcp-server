from __future__ import annotations

import dataclasses

import httpx
import pytest
from fastmcp import Client
from fastmcp.exceptions import ToolError
from fastmcp.server.auth import AuthContext
from fastmcp.server.context import reset_transport, set_transport
from starlette.testclient import TestClient

from tests.conftest import CUSTOMER_ME, FakeZammad
from tests.helpers import rpc, tool_names
from zammad_mcp.client.errors import MissingTokenError
from zammad_mcp.server import _warm, build_http_app, build_server
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

AGENT_ONLY = {"search_users", "search_organizations", "get_ticket_history"}


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
    assert tier_for({"ticket"}) is Tier.AGENT


@pytest.mark.parametrize("permissions", [set(), {"admin", "knowledge_base.editor", "report", "user_preferences"}])
def test_admin_without_ticket_permissions_is_not_called_a_customer(permissions):
    assert tier_for(permissions) is Tier.NONE
    assert Tier.NONE.label == "an account without ticket permissions"
    assert required_tier({"tier:none"}) is Tier.NONE


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


async def list_names(settings, fake: FakeZammad) -> set[str]:
    async with Client(build_server(settings, transport=fake)) as client:
        return {tool.name for tool in await client.list_tools()}


async def test_community_mode_shows_agent_tools_to_customers(settings, customer_fake):
    assert AGENT_ONLY <= await list_names(settings, customer_fake)


async def test_filter_by_role_hides_agent_tools_from_customers(settings, customer_fake):
    filtered = dataclasses.replace(settings, filter_tools_by_role=True)
    names = await list_names(filtered, customer_fake)
    assert not AGENT_ONLY & names
    assert {"get_me", "search_tickets", "get_ticket"} <= names
    async with Client(build_server(filtered, transport=customer_fake)) as client:
        with pytest.raises(ToolError, match="Unknown tool"):
            await client.call_tool("search_users", {"query": "a"})


async def test_filter_by_role_shows_agent_tools_to_agents(settings, fake):
    assert AGENT_ONLY <= await list_names(dataclasses.replace(settings, filter_tools_by_role=True), fake)


async def test_stdio_short_circuits_filtering(settings):
    async def always_customer(_token):
        return Tier.CUSTOMER

    mcp = build_server(settings, transport=FakeZammad(), tier_resolver=always_customer)
    assert not AGENT_ONLY & {tool.name for tool in await mcp.list_tools()}
    token = set_transport("stdio")
    try:
        assert AGENT_ONLY <= {tool.name for tool in await mcp.list_tools()}
    finally:
        reset_transport(token)


@pytest.mark.parametrize(("filter_by_role", "visible"), [(False, True), (True, False)])
def test_http_tools_list_without_auth_provider(shared_settings, filter_by_role, visible):
    """Regression for the token=None path over real streamable HTTP."""
    configured = dataclasses.replace(shared_settings, filter_tools_by_role=filter_by_role)
    server = build_server(configured, transport=FakeZammad(me=CUSTOMER_ME))
    with TestClient(build_http_app(server, configured)) as client:
        names = tool_names(rpc(client, "/mcp", "tools/list", {}))
    assert (AGENT_ONLY <= names) is visible
    assert "get_me" in names


class CrashingProvider:
    async def resolve(self):
        raise RuntimeError("not a ZammadError")


async def test_resolver_survives_a_non_zammad_exception():
    assert await credential_tier_resolver(CrashingProvider())(None) is None


async def test_a_crashing_resolver_does_not_blank_the_tool_list(settings):
    async def crash(_token):
        raise RuntimeError("bug in a resolver")

    check = require_tier(crash)
    assert await check(AuthContext(token=None, component=FakeComponent("tier:agent"))) is True
    mcp = build_server(settings, transport=FakeZammad(), tier_resolver=crash)
    assert AGENT_ONLY <= {tool.name for tool in await mcp.list_tools()}


async def test_startup_warm_up_survives_any_exception():
    class Exploding:
        async def warm(self):
            raise RuntimeError("odd Zammad")

    await _warm(Exploding())


async def test_server_starts_when_users_me_has_an_odd_shape(settings):
    fake = FakeZammad()
    fake.routes[("GET", "/api/v1/users/me")] = httpx.Response(200, content=b'"just a string"')
    async with Client(build_server(settings, transport=fake)) as client:
        assert (await client.call_tool("get_me", {})).data.startswith("Error: Zammad returned an unexpected")
