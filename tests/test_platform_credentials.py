from __future__ import annotations

import httpx
import pytest
from fastmcp.server.auth import AccessToken
from starlette.testclient import TestClient

from tests.conftest import RecordingTransport
from tests.helpers import rpc, tool_names, tool_text
from tests.platform_fakes import AGENT_PERMISSIONS, CUSTOMER_PERMISSIONS, FakeSsoZammad, identity
from tests.test_platform_mint import AGENT, CUSTOMER, Clock, make_minter
from zammad_mcp.client import ZammadClient
from zammad_mcp.client.errors import (
    AuthError,
    GatewayDeniedError,
    MissingTokenError,
    PermissionDenied,
    ZammadAPIError,
)
from zammad_mcp.client.http import USER_AGENT, is_identity_header
from zammad_mcp.config import Settings
from zammad_mcp.credentials.base import ProfileMemo
from zammad_mcp.platform.cognito import UPSTREAM_CLAIMS_KEY
from zammad_mcp.platform.credentials import MintedCredentialProvider, is_role_denial, platform_tier_resolver
from zammad_mcp.platform.mint import RECHECK_KEY_PREFIX, SyntheticIdentityError
from zammad_mcp.platform.storage import CONFIRMATION_SALT, KeyValueConfirmationBackend, MemoryKeyValue, fernet_for
from zammad_mcp.server import build_server
from zammad_mcp.tiers import Tier

AGENT_ONLY = {"search_users", "search_organizations", "get_ticket_history"}
INTERNAL = "http://zammad-nginx:8080"
SETTINGS = Settings(zammad_url=INTERNAL, public_url="https://support.example.com")


@pytest.fixture
def clock() -> Clock:
    return Clock()


@pytest.fixture
def zammad() -> FakeSsoZammad:
    fake = FakeSsoZammad()
    fake.add(AGENT, AGENT_PERMISSIONS)
    fake.add(CUSTOMER, CUSTOMER_PERMISSIONS)
    return fake


def provider_for(zammad, clock, who):
    store = MemoryKeyValue(clock)
    minter = make_minter(zammad, store, clock)
    return MintedCredentialProvider(minter, identity_source=lambda: who), store


def platform_app(zammad, clock, who):
    credentials, store = provider_for(zammad, clock, who)
    client = ZammadClient(f"{INTERNAL}/api/v1", transport=zammad, on_rejected=credentials.on_rejected)
    mcp = build_server(
        SETTINGS,
        client=client,
        credentials=credentials,
        tier_resolver=platform_tier_resolver(credentials, identity_of=lambda _token: who),
        confirmation_backend=KeyValueConfirmationBackend(store, fernet_for("s", CONFIRMATION_SALT)),
    )
    return TestClient(mcp.http_app(path="/mcp", stateless_http=True))


def listed(zammad, clock, who) -> set[str]:
    with platform_app(zammad, clock, who) as client:
        return tool_names(rpc(client, "/mcp", "tools/list", {}))


def test_agents_see_agent_tools(zammad, clock):
    names = listed(zammad, clock, identity(AGENT))

    assert AGENT_ONLY <= names
    assert {"get_me", "search_tickets"} <= names


def test_customers_never_see_agent_tools(zammad, clock):
    names = listed(zammad, clock, identity(CUSTOMER))

    assert not AGENT_ONLY & names
    assert {"get_me", "search_tickets"} <= names


def test_a_failed_mint_hides_every_tier_gated_tool(zammad, clock):
    zammad.users[CUSTOMER].token_access = False

    assert listed(zammad, clock, identity(CUSTOMER)) == {"get_me"}


def test_a_synthetic_user_sees_get_me_and_learns_why(zammad, clock):
    who = identity("9990000000000001@askii.ai")
    with platform_app(zammad, clock, who) as client:
        assert tool_names(rpc(client, "/mcp", "tools/list", {})) == {"get_me"}
        text = tool_text(rpc(client, "/mcp", "tools/call", {"name": "get_me", "arguments": {}}))

    assert "verify your email in the portal first" in text
    assert zammad.requests == []


def test_tools_list_resolves_the_tier_once_not_once_per_tool(zammad, clock):
    listed(zammad, clock, identity(AGENT))

    assert len(zammad.gets()) == 1


def steady_state(zammad: FakeSsoZammad) -> list[httpx.Request]:
    return [request for request in zammad.requests if "/user_access_token" not in request.url.path]


def test_steady_state_sends_only_the_token(zammad, clock):
    with platform_app(zammad, clock, identity(AGENT)) as client:
        text = tool_text(rpc(client, "/mcp", "tools/call", {"name": "get_me", "arguments": {}}))

    assert "ada@example.com" in text
    requests = steady_state(zammad)
    assert requests
    for request in requests:
        assert str(request.url).startswith(INTERNAL)
        assert request.headers["authorization"] == "Token token=minted-1"
        assert request.headers["user-agent"] == USER_AGENT
        assert "cookie" not in request.headers
        assert not [name for name in request.headers if is_identity_header(name)]


def test_a_revoked_token_is_reminted_once_and_the_call_succeeds(zammad, clock):
    with platform_app(zammad, clock, identity(AGENT)) as client:
        rpc(client, "/mcp", "tools/call", {"name": "get_me", "arguments": {}})
        zammad.revoked.add("minted-1")
        text = tool_text(rpc(client, "/mcp", "tools/call", {"name": "get_me", "arguments": {}}))

    assert "ada@example.com" in text
    assert len(zammad.posts()) == 2
    assert steady_state(zammad)[-1].headers["authorization"] == "Token token=minted-2"


async def test_a_401_after_a_remint_is_not_retried_again(zammad, clock):
    def always_unauthorized(request: httpx.Request) -> httpx.Response:
        if "/user_access_token" in request.url.path:
            return zammad._dispatch(request)
        return httpx.Response(401, json={"error": "Not authorized (token expired)!"})

    transport = RecordingTransport(always_unauthorized)
    credentials, _ = provider_for(transport, clock, identity(AGENT))

    client = ZammadClient(f"{INTERNAL}/api/v1", transport=transport, on_rejected=credentials.on_rejected)
    token = (await credentials.resolve()).token

    with pytest.raises(AuthError):
        await client.get("/users/me", token=token)

    assert len([r for r in transport.requests if r.url.path == "/api/v1/users/me"]) == 2


async def test_a_role_denial_arms_one_recheck(zammad, clock):
    credentials, store = provider_for(zammad, clock, identity(CUSTOMER))
    client = ZammadClient(f"{INTERNAL}/api/v1", transport=zammad, on_rejected=credentials.on_rejected)
    token = (await credentials.resolve()).token

    with pytest.raises(PermissionDenied):
        await client.get("/users/search", token=token)

    assert await store.get(RECHECK_KEY_PREFIX + identity(CUSTOMER).key) == "1"
    await credentials.resolve()
    assert len(zammad.gets()) == 2


async def test_on_rejected_without_an_identity_changes_nothing(zammad, clock):
    credentials = MintedCredentialProvider(
        make_minter(zammad, MemoryKeyValue(clock), clock), identity_source=lambda: None
    )

    assert await credentials.on_rejected(AuthError(401, "x"), "t") is None
    with pytest.raises(MissingTokenError):
        await credentials.resolve()


async def test_gateway_and_other_403s_never_arm_a_recheck(zammad, clock):
    credentials, store = provider_for(zammad, clock, identity(AGENT))

    assert await credentials.on_rejected(GatewayDeniedError(403, "access_denied"), "t") is None
    assert await store.get(RECHECK_KEY_PREFIX + identity(AGENT).key) is None
    assert is_role_denial(PermissionDenied(403, "Not authorized"))
    assert not is_role_denial(GatewayDeniedError(403, "access_denied"))
    assert not is_role_denial(ZammadAPIError(500, "x"))


async def test_a_broken_limiter_never_masks_the_403(zammad, clock):
    class Broken(MemoryKeyValue):
        async def set_if_absent(self, key, value, ttl_seconds):
            raise ConnectionError("down")

    minter = make_minter(zammad, Broken(clock), clock)
    credentials = MintedCredentialProvider(minter, identity_source=lambda: identity(AGENT))

    assert await credentials.on_rejected(PermissionDenied(403, "Not authorized"), "t") is None


async def test_credential_identity_is_stable_across_remints(zammad, clock):
    credentials, _ = provider_for(zammad, clock, identity(AGENT))
    first = await credentials.resolve()
    await credentials.on_rejected(AuthError(401, "expired"), first.token)
    second = await credentials.resolve()

    assert first.token != second.token
    assert first.identity == second.identity == identity(AGENT).short_key
    assert second.tier is Tier.AGENT


def access_token(claims: dict) -> AccessToken:
    return AccessToken(token="t", client_id="c", scopes=[], claims=claims)


async def test_resolver_reads_the_identity_from_the_access_token(zammad, clock):
    credentials, _ = provider_for(zammad, clock, None)
    resolve = platform_tier_resolver(credentials)
    token = access_token({UPSTREAM_CLAIMS_KEY: {"id_token": "id", "email": "Cara@Example.com"}})

    assert await resolve(token) is Tier.CUSTOMER
    assert await resolve(None) is Tier.NONE
    assert await resolve(access_token({})) is Tier.NONE


async def test_resolver_fails_closed_on_any_exception(zammad, clock):
    credentials, _ = provider_for(zammad, clock, None)

    def explode(_token):
        raise RuntimeError("boom")

    assert await platform_tier_resolver(credentials, identity_of=explode)(None) is Tier.NONE


async def test_resolver_fails_closed_when_the_profile_lookup_raises(zammad, clock):
    credentials, _ = provider_for(zammad, clock, None)

    async def broken(_identity):
        raise ValueError("not a ZammadError")

    credentials.profile_for = broken
    resolve = platform_tier_resolver(
        credentials, memo=ProfileMemo(ttl_seconds=60), identity_of=lambda _token: identity(AGENT)
    )

    assert await resolve(None) is Tier.NONE


async def test_resolver_hides_tools_for_a_refused_identity(zammad, clock):
    credentials, _ = provider_for(zammad, clock, None)
    resolve = platform_tier_resolver(credentials, identity_of=lambda _token: identity("x@askii.ai"))

    assert await resolve(None) is Tier.NONE
    with pytest.raises(SyntheticIdentityError):
        await credentials.profile_for(identity("x@askii.ai"))
