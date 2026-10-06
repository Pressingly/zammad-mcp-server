from __future__ import annotations

import httpx
import pytest

from zammad_mcp.client.errors import (
    IdentityHeaderError,
    MissingTokenError,
    ZammadAPIError,
    ZammadTransportError,
    describe_error,
)
from zammad_mcp.client.http import ZammadClient


def ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


def make_client(transport: httpx.AsyncBaseTransport) -> ZammadClient:
    return ZammadClient("https://zammad.test/api/v1", transport=transport)


async def test_sends_token_per_call(recording_transport):
    transport = recording_transport(ok)
    client = make_client(transport)
    await client.get("/users/me", token="alpha")
    await client.get("/users/me", token="beta")
    assert [r.headers["authorization"] for r in transport.requests] == ["Token token=alpha", "Token token=beta"]
    assert str(transport.requests[0].url) == "https://zammad.test/api/v1/users/me"


@pytest.mark.parametrize("header", ["X-Auth-Request-Email", "x-auth-request-access-token", "X-AUTH-REQUEST-User"])
async def test_identity_header_guard_raises(recording_transport, header):
    transport = recording_transport(ok)
    client = make_client(transport)
    with pytest.raises(IdentityHeaderError, match="identity header"):
        await client.request("GET", "/users/me", token="t", headers={header: "victim@example.com"})
    assert transport.requests == []


async def test_cookies_are_never_persisted(recording_transport):
    def set_cookie(_request: httpx.Request) -> httpx.Response:
        return httpx.Response(200, json={}, headers={"Set-Cookie": "_zammad_session=abc; Path=/; HttpOnly"})

    transport = recording_transport(set_cookie)
    client = make_client(transport)
    await client.get("/users/me", token="t")
    await client.get("/users/me", token="t")
    assert "cookie" not in transport.requests[1].headers
    assert len(client._client().cookies.jar) == 0


async def test_missing_token_raises_before_sending(recording_transport):
    transport = recording_transport(ok)
    with pytest.raises(MissingTokenError):
        await make_client(transport).get("/users/me", token=None)
    assert transport.requests == []


async def test_error_status_maps_to_api_error(recording_transport):
    transport = recording_transport(lambda _r: httpx.Response(422, json={"error": "bad input"}))
    with pytest.raises(ZammadAPIError) as caught:
        await make_client(transport).get("/tickets/1", token="t")
    assert caught.value.status_code == 422
    assert describe_error(caught.value) == "Error: Zammad returned HTTP 422 (bad input)"


async def test_redirect_is_an_error_not_followed(recording_transport):
    transport = recording_transport(lambda _r: httpx.Response(302, headers={"Location": "https://sso.test/login"}))
    with pytest.raises(ZammadAPIError) as caught:
        await make_client(transport).get("/users/me", token="t")
    assert caught.value.status_code == 302
    assert len(transport.requests) == 1


async def test_non_json_body_is_an_error(recording_transport):
    transport = recording_transport(lambda _r: httpx.Response(200, text="<html>login</html>"))
    with pytest.raises(ZammadAPIError, match="not JSON"):
        await make_client(transport).get("/users/me", token="t")


async def test_empty_body_returns_none(recording_transport):
    transport = recording_transport(lambda _r: httpx.Response(204))
    assert await make_client(transport).request("DELETE", "/tags/1", token="t") is None


async def test_transport_failure_maps_to_transport_error():
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    with pytest.raises(ZammadTransportError, match="ConnectError"):
        await make_client(httpx.MockTransport(refuse)).get("/users/me", token="t")


async def test_aclose_allows_reuse(recording_transport):
    transport = recording_transport(ok)
    client = make_client(transport)
    await client.get("/users/me", token="t")
    await client.aclose()
    await client.aclose()
    assert await client.get("/users/me", token="t") == {"ok": True}


def test_describe_error_hint_for_known_status():
    assert "invalid or expired" in describe_error(ZammadAPIError(401, "Unauthorized"))
    assert describe_error(MissingTokenError("no token")) == "Error: no token"
