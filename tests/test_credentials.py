from __future__ import annotations

import hashlib
import json

import pytest
from starlette.requests import Request
from starlette.testclient import TestClient

from tests.conftest import FakeZammad
from tests.helpers import rpc, tool_text
from zammad_mcp.client import ZammadClient
from zammad_mcp.client.errors import MissingTokenError
from zammad_mcp.credentials import header as header_module
from zammad_mcp.credentials.base import Profile, ProfileMemo, ZammadCredential, fetch_profile, token_identity
from zammad_mcp.credentials.header import HeaderCredentialProvider, api_key_mount, on_api_key_mount, request_token
from zammad_mcp.credentials.static import StaticCredentialProvider
from zammad_mcp.server import build_http_app, build_server
from zammad_mcp.tiers import Tier


def client_for(fake: FakeZammad) -> ZammadClient:
    return ZammadClient("https://zammad.test/api/v1", transport=fake)


class Clock:
    def __init__(self) -> None:
        self.now = 0.0

    def __call__(self) -> float:
        return self.now


def test_identity_is_a_short_token_hash():
    assert token_identity("abc") == hashlib.sha256(b"abc").hexdigest()[:16]
    assert len(token_identity("abc")) == 16


def test_credential_repr_hides_the_token():
    credential = ZammadCredential(token="s3cret", identity="id", tier=Tier.AGENT, permissions=frozenset())
    assert "s3cret" not in repr(credential)


async def test_fetch_profile_reads_role_permissions(fake):
    profile = await fetch_profile(client_for(fake), "t")
    assert profile.tier is Tier.AGENT
    assert "ticket.agent" in profile.permissions


async def test_fetch_profile_for_customer(customer_fake):
    profile = await fetch_profile(client_for(customer_fake), "t")
    assert profile.tier is Tier.CUSTOMER


async def test_static_without_token_raises(fake):
    with pytest.raises(MissingTokenError):
        await StaticCredentialProvider(client_for(fake), None).resolve()
    await StaticCredentialProvider(client_for(fake), None).warm()
    assert fake.requests == []


async def test_static_profile_is_read_once(fake):
    provider = StaticCredentialProvider(client_for(fake), "tok")
    await provider.warm()
    first = await provider.resolve()
    second = await provider.resolve()
    assert first == second
    assert first.identity == token_identity("tok")
    assert first.tier is Tier.AGENT
    assert len(fake.calls("GET", "/users/me")) == 1


async def test_profile_failure_is_tolerated_and_retried_later():
    fake = FakeZammad()
    fake.on("GET", "/users/me", status=503, json={"error": "down"})
    clock = Clock()
    memo = ProfileMemo(ttl_seconds=None, failure_ttl_seconds=60, clock=clock)
    provider = StaticCredentialProvider(client_for(fake), "tok", memo=memo)
    credential = await provider.resolve()
    assert credential.tier is None
    await provider.resolve()
    calls_while_cached = len(fake.calls("GET", "/users/me"))
    clock.now = 61
    fake.on("GET", "/users/me", json={"id": 1, "role_ids": [2]})
    assert (await provider.resolve()).tier is Tier.AGENT
    assert len(fake.calls("GET", "/users/me")) == calls_while_cached + 1


async def test_profile_memo_expires_and_evicts():
    clock = Clock()
    memo = ProfileMemo(ttl_seconds=60, max_entries=2, clock=clock)
    loads = []

    async def load(tier: Tier):
        loads.append(tier)
        return Profile(tier=tier)

    await memo.get("a", lambda: load(Tier.AGENT))
    await memo.get("a", lambda: load(Tier.AGENT))
    assert loads == [Tier.AGENT]
    clock.now = 61
    await memo.get("b", lambda: load(Tier.CUSTOMER))
    await memo.get("c", lambda: load(Tier.CUSTOMER))
    assert set(memo._entries) == {"b", "c"}
    await memo.get("a", lambda: load(Tier.AGENT))
    assert loads == [Tier.AGENT, Tier.CUSTOMER, Tier.CUSTOMER, Tier.AGENT]


async def capture_scope(headers: list[tuple[bytes, bytes]]) -> dict:
    captured = {}

    async def app(scope, receive, send):
        captured.update(scope)

    await api_key_mount(app)({"type": "http", "headers": headers, "path": "/mcp"}, None, None)
    return captured


async def test_api_key_mount_strips_identity_headers_and_marks_the_scope():
    scope = await capture_scope(
        [
            (b"x-zammad-token", b"user-token"),
            (b"x-auth-request-email", b"victim@example.com"),
            (b"X-Auth-Request-User", b"v"),
        ]
    )
    assert scope["headers"] == [(b"x-zammad-token", b"user-token")]
    assert scope["zammad_mcp.api_key_mount"] is True


async def test_header_provider_ignores_identity_headers(monkeypatch, fake):
    scope = await capture_scope([(b"x-zammad-token", b"user-token"), (b"x-auth-request-email", b"victim@example.com")])
    monkeypatch.setattr(header_module, "get_http_request", lambda: Request(scope))
    assert on_api_key_mount() is True
    assert request_token() == "user-token"
    credential = await HeaderCredentialProvider(client_for(fake)).resolve()
    assert credential.identity == token_identity("user-token")
    assert all("x-auth-request" not in name.lower() for r in fake.requests for name in r.headers)


async def test_header_provider_without_header_raises(monkeypatch, fake):
    monkeypatch.setattr(header_module, "get_http_request", lambda: Request({"type": "http", "headers": []}))
    with pytest.raises(MissingTokenError, match="X-Zammad-Token"):
        await HeaderCredentialProvider(client_for(fake)).resolve()


def test_outside_a_request_nothing_is_on_the_mount():
    assert on_api_key_mount() is False
    assert request_token() is None


def call_get_me(client: TestClient, path: str, headers: dict[str, str]) -> str:
    return tool_text(rpc(client, path, "tools/call", {"name": "get_me", "arguments": {}}, headers))


def test_api_key_mount_uses_the_callers_token_end_to_end(settings, fake):
    headers = {"X-Zammad-Token": "user-token", "X-Auth-Request-Email": "victim@example.com"}
    with TestClient(build_http_app(build_server(settings, transport=fake))) as client:
        shaped = json.loads(call_get_me(client, "/http/api-key/mcp", headers))
    assert shaped["login"] == "agent@example.com"
    me_calls = fake.calls("GET", "/users/me")
    assert me_calls[-1].headers["authorization"] == "Token token=user-token"
    assert all("x-auth-request" not in name.lower() for r in fake.requests for name in r.headers)


def test_plain_mount_ignores_the_header_and_uses_the_static_token(settings, fake):
    with TestClient(build_http_app(build_server(settings, transport=fake))) as client:
        call_get_me(client, "/mcp", {"X-Zammad-Token": "user-token"})
    assert {r.headers["authorization"] for r in fake.requests} == {"Token token=secret-token"}


def test_api_key_mount_without_header_returns_an_error(settings, fake):
    with TestClient(build_http_app(build_server(settings, transport=fake))) as client:
        text = call_get_me(client, "/http/api-key/mcp", {})
    assert text == "Error: send your personal Zammad API token in the X-Zammad-Token header"
