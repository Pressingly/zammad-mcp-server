"""Two users at once, through the real Cognito provider, identity lookup, mint and steady-state client."""

from __future__ import annotations

import asyncio
import json
import time

import httpx
import jwt
import pytest
from fastmcp.server.auth.oauth_proxy.models import JTIMapping, UpstreamTokenSet
from fastmcp.server.auth.oidc_proxy import OIDCConfiguration, OIDCProxy
from fastmcp.server.auth.providers.jwt import RSAKeyPair
from key_value.aio.stores.memory import MemoryStore

from tests.helpers import MCP_HEADERS
from tests.platform_fakes import AGENT_PERMISSIONS, CUSTOMER_PERMISSIONS, FakeSsoZammad
from zammad_mcp.config import Settings
from zammad_mcp.platform.http import build_platform_server
from zammad_mcp.platform.settings import PlatformSettings
from zammad_mcp.platform.storage import MemoryKeyValue

ISSUER = "https://cognito-idp.ap-southeast-1.amazonaws.com/ap-southeast-1_test"
CLIENT_ID = "cognito-app-client"
ENV = {
    "MCP_BASE_URL": "https://support-mcp.example.test",
    "COGNITO_USER_POOL_ID": "ap-southeast-1_test",
    "COGNITO_AWS_REGION": "ap-southeast-1",
    "OIDC_CLIENT_ID": CLIENT_ID,
    "OIDC_CLIENT_SECRET": "cognito-app-secret",
    "ZAMMAD_INTERNAL_BASE_URL": "http://zammad-nginx:8080",
    "DEFAULT_EMAIL_DOMAIN": "askii.ai",
    "MCP_ALLOWED_CLIENT_REDIRECT_URIS": "https://claude.ai/api/mcp/auth_callback",
}
DISCOVERY = OIDCConfiguration(
    strict=False,
    issuer=ISSUER,
    authorization_endpoint="https://auth.example.test/oauth2/authorize",
    token_endpoint="https://auth.example.test/oauth2/token",
    jwks_uri=f"{ISSUER}/.well-known/jwks.json",
)
USERS = {"ada@example.com": ("sid-ada", AGENT_PERMISSIONS), "jane@askii.ai": ("sid-jane", CUSTOMER_PERMISSIONS)}


@pytest.fixture
def zammad() -> FakeSsoZammad:
    fake = FakeSsoZammad()
    for email, (_username, permissions) in USERS.items():
        fake.add(email, permissions)
    return fake


@pytest.fixture
def server(monkeypatch, zammad):
    monkeypatch.setattr(OIDCProxy, "get_oidc_configuration", lambda *_args, **_kwargs: DISCOVERY)
    mcp = build_platform_server(
        Settings(zammad_url=ENV["ZAMMAD_INTERNAL_BASE_URL"]),
        PlatformSettings.from_env(ENV),
        store=MemoryKeyValue(),
        oauth_storage=MemoryStore(),
        transport=zammad,
    )
    app = mcp.http_app(path="/mcp", stateless_http=True)
    keys = RSAKeyPair.generate()
    mcp.auth._token_validator.public_key = keys.public_key
    mcp.auth._token_validator.jwks_uri = None
    return app, mcp.auth, keys


async def sign_in(provider, keys: RSAKeyPair, email: str, username: str) -> str:
    """Store what a finished OAuth flow leaves behind, and return the client's FastMCP token."""
    access = keys.create_token(
        subject=f"uuid-{username}",
        issuer=ISSUER,
        scopes=["openid"],
        additional_claims={"token_use": "access", "username": f"uuid-{username}", "client_id": CLIENT_ID},
    )
    id_token = jwt.encode({"email": email, "cognito:username": username}, "k" * 32)
    now = time.time()
    upstream_id, jti = f"up-{username}", f"jti-{username}"
    await provider._upstream_token_store.put(
        key=upstream_id,
        value=UpstreamTokenSet(
            upstream_token_id=upstream_id,
            access_token=access,
            refresh_token=None,
            refresh_token_expires_at=None,
            expires_at=now + 3600,
            token_type="Bearer",
            scope="openid",
            client_id="mcp-client",
            created_at=now,
            raw_token_data={"access_token": access, "id_token": id_token},
        ),
    )
    await provider._jti_mapping_store.put(
        key=jti, value=JTIMapping(jti=jti, upstream_token_id=upstream_id, created_at=now)
    )
    return provider.jwt_issuer.issue_access_token(client_id="mcp-client", scopes=["openid"], jti=jti)


async def call_get_me(client: httpx.AsyncClient, bearer: str) -> str:
    message = {"jsonrpc": "2.0", "id": 1, "method": "tools/call", "params": {"name": "get_me", "arguments": {}}}
    response = await client.post("/mcp", json=message, headers={**MCP_HEADERS, "Authorization": f"Bearer {bearer}"})
    assert response.status_code == 200, response.text
    data = [line.removeprefix("data: ") for line in response.text.splitlines() if line.startswith("data: ")]
    return json.loads(data[-1])["result"]["content"][0]["text"]


async def test_two_concurrent_users_each_get_their_own_identity_and_token(server, zammad):
    app, provider, keys = server
    bearers = {email: await sign_in(provider, keys, email, username) for email, (username, _) in USERS.items()}

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp.test") as client:
            calls = [call_get_me(client, bearers[email]) for email in USERS for _ in range(3)]
            texts = await asyncio.gather(*calls)

    for email, text in zip([email for email in USERS for _ in range(3)], texts, strict=True):
        assert email in text
    assert sorted(request.headers["x-auth-request-email"] for request in zammad.gets()) == sorted(USERS)
    for request in zammad.gets():
        assert request.headers["x-auth-request-access-token"].count(".") == 2
    steady = [request for request in zammad.requests if request.url.path == "/api/v1/users/me"]
    assert len(steady) == 6
    owners = [zammad.minted[request.headers["authorization"].removeprefix("Token token=")][0] for request in steady]
    assert sorted(owners) == sorted(email for email in USERS for _ in range(3))
    assert len(zammad.posts()) == 2


async def test_a_session_without_a_stored_id_token_is_rejected(server, zammad):
    app, provider, keys = server
    bearer = await sign_in(provider, keys, "ada@example.com", "sid-ada")
    stored = await provider._upstream_token_store.get(key="up-sid-ada")
    await provider._upstream_token_store.put(
        key="up-sid-ada", value=stored.model_copy(update={"raw_token_data": {"access_token": stored.access_token}})
    )

    async with app.router.lifespan_context(app):
        async with httpx.AsyncClient(transport=httpx.ASGITransport(app=app), base_url="http://mcp.test") as client:
            response = await client.post(
                "/mcp",
                json={"jsonrpc": "2.0", "id": 1, "method": "tools/list"},
                headers={**MCP_HEADERS, "Authorization": f"Bearer {bearer}"},
            )

    assert response.status_code == 401
    assert zammad.requests == []
