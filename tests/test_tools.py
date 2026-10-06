from __future__ import annotations

import dataclasses

import httpx
import pytest
from fastmcp import Client

from zammad_mcp.server import build_server

ANNOTATION_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")

ME = {
    "id": 7,
    "login": "agent@example.com",
    "firstname": "Ada",
    "lastname": "Agent",
    "email": "agent@example.com",
    "roles": ["Agent"],
    "organization": "Example",
    "active": True,
    "password": "",
    "preferences": {"locale": "en-us"},
}


def me_handler(request: httpx.Request) -> httpx.Response:
    if request.url.path == "/api/v1/users/me":
        return httpx.Response(200, json=ME)
    return httpx.Response(404, json={"error": "Not Found"})


async def call_get_me(settings, transport) -> object:
    async with Client(build_server(settings, transport=transport)) as client:
        result = await client.call_tool("get_me", {})
    return result.data


async def test_every_tool_has_all_four_annotations(settings):
    async with Client(build_server(settings, transport=httpx.MockTransport(me_handler))) as client:
        tools = await client.list_tools()
    assert tools, "no tools registered"
    for tool in tools:
        assert tool.annotations is not None, tool.name
        hints = tool.annotations.model_dump()
        missing = [hint for hint in ANNOTATION_HINTS if hints.get(hint) is None]
        assert not missing, f"{tool.name} is missing {missing}"


async def test_get_me_returns_shaped_user(settings, recording_transport):
    transport = recording_transport(me_handler)
    data = await call_get_me(settings, transport)
    assert data == {
        key: ME[key] for key in ("id", "login", "firstname", "lastname", "email", "organization", "roles", "active")
    }
    request = transport.requests[0]
    assert request.headers["authorization"] == "Token token=secret-token"
    assert request.url.params["expand"] == "true"


@pytest.mark.parametrize(
    ("status", "expected"),
    [(401, "HTTP 401 (the Zammad token is invalid or expired)"), (403, "HTTP 403"), (500, "HTTP 500")],
)
async def test_get_me_returns_error_string_on_http_error(settings, status, expected):
    transport = httpx.MockTransport(lambda _r: httpx.Response(status, json={"error": "nope"}))
    data = await call_get_me(settings, transport)
    assert isinstance(data, str)
    assert data.startswith("Error:")
    assert expected in data


async def test_get_me_without_token_returns_error_string(settings, recording_transport):
    transport = recording_transport(me_handler)
    data = await call_get_me(dataclasses.replace(settings, http_token=None), transport)
    assert data == "Error: no Zammad API token is configured for this request"
    assert transport.requests == []


async def test_get_me_on_unreachable_zammad_returns_error_string(settings):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectTimeout("timed out", request=request)

    data = await call_get_me(settings, httpx.MockTransport(refuse))
    assert data == "Error: could not reach Zammad (ConnectTimeout)"
