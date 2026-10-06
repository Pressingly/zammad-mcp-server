from __future__ import annotations

import dataclasses
from typing import Any

import httpx
from fastmcp import Client

from tests.conftest import FakeZammad
from zammad_mcp.config import Settings
from zammad_mcp.server import build_server

ANNOTATION_HINTS = ("readOnlyHint", "destructiveHint", "idempotentHint", "openWorldHint")
ALL_TOOLS = {"get_me"}


async def tools(settings: Settings, fake: FakeZammad) -> dict[str, Any]:
    async with Client(build_server(settings, transport=fake)) as client:
        return {tool.name: tool for tool in await client.list_tools()}


async def call(settings: Settings, fake: FakeZammad, name: str, arguments: dict[str, Any] | None = None):
    async with Client(build_server(settings, transport=fake)) as client:
        return await client.call_tool(name, arguments or {})


async def data(settings: Settings, fake: FakeZammad, name: str, arguments: dict[str, Any] | None = None) -> Any:
    return (await call(settings, fake, name, arguments)).data


def failing(status: int, error: str = "nope") -> httpx.Response:
    return httpx.Response(status, json={"error": error})


# --- registration and annotations ---


async def test_every_tool_is_registered(settings, fake):
    assert set(await tools(settings, fake)) == ALL_TOOLS


async def test_every_tool_has_annotations_tier_and_module_tags(settings, fake):
    for name, tool in (await tools(settings, fake)).items():
        assert tool.annotations is not None, name
        hints = tool.annotations.model_dump()
        missing = [hint for hint in ANNOTATION_HINTS if hints.get(hint) is None]
        assert not missing, f"{name} is missing {missing}"
        tags = set((tool.meta or {}).get("fastmcp", {}).get("tags", []))
        assert {"tier:customer", "tier:agent"} & tags, f"{name} has no tier tag"
        assert any(tag.startswith("module:") for tag in tags), f"{name} has no module tag"


# --- get_me ---


async def test_get_me_returns_shaped_user_with_tier(settings, fake):
    shaped = await data(settings, fake, "get_me")
    assert shaped["login"] == "agent@example.com"
    assert shaped["tier"] == "agent"
    assert shaped["url"] == "https://zammad.test/#user/profile/7"
    assert "preferences" not in shaped
    request = fake.calls("GET", "/users/me")[-1]
    assert request.headers["authorization"] == "Token token=secret-token"
    assert request.url.params["expand"] == "true"


async def test_get_me_on_401(settings, fake):
    fake.on("GET", "/users/me", status=401, json={"error": "Invalid token"})
    assert await data(settings, fake, "get_me") == (
        "Error: Zammad rejected the token (HTTP 401); it is invalid, expired or revoked"
    )


async def test_tools_without_token_return_an_error(settings, fake):
    shaped = await data(dataclasses.replace(settings, http_token=None), fake, "get_me")
    assert shaped == "Error: no Zammad API token is configured for this request"
    assert fake.requests == []


async def test_get_me_on_unreachable_zammad(settings):
    def refuse(request: httpx.Request) -> httpx.Response:
        raise httpx.ConnectError("refused", request=request)

    async with Client(build_server(settings, transport=httpx.MockTransport(refuse))) as client:
        result = await client.call_tool("get_me", {})
    assert result.data == "Error: could not reach Zammad (ConnectError)"


async def test_public_url_drives_links(settings, fake):
    shaped = await data(dataclasses.replace(settings, public_url="https://help.example.com"), fake, "get_me")
    assert shaped["url"] == "https://help.example.com/#user/profile/7"
