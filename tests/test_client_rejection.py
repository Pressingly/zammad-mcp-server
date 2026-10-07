from __future__ import annotations

import httpx
import pytest

from tests.conftest import RecordingTransport
from zammad_mcp.client import ZammadClient
from zammad_mcp.client.errors import AuthError, NotFoundError, PermissionDenied


def by_token(statuses: dict[str, int]) -> RecordingTransport:
    def handler(request: httpx.Request) -> httpx.Response:
        token = request.headers["authorization"].removeprefix("Token token=")
        status = statuses.get(token, 200)
        return httpx.Response(status, json={"error": "nope"} if status >= 400 else {"ok": token})

    return RecordingTransport(handler)


def hook(replacement: str | None, seen: list):
    async def on_rejected(error, token):
        seen.append((type(error), token))
        return replacement

    return on_rejected


async def test_a_401_is_retried_once_with_the_replacement_token():
    seen: list = []
    transport = by_token({"old": 401})
    client = ZammadClient("https://z.test/api/v1", transport=transport, on_rejected=hook("new", seen))

    assert await client.request("POST", "/ticket_articles", token="old", json={"body": "x"}) == {"ok": "new"}
    assert seen == [(AuthError, "old")]
    assert [r.headers["authorization"] for r in transport.requests] == ["Token token=old", "Token token=new"]


async def test_a_failing_replacement_is_not_retried_again():
    seen: list = []
    transport = by_token({"old": 401, "new": 401})
    client = ZammadClient("https://z.test/api/v1", transport=transport, on_rejected=hook("new", seen))

    with pytest.raises(AuthError):
        await client.get("/users/me", token="old")

    assert len(transport.requests) == 2
    assert len(seen) == 1


@pytest.mark.parametrize("replacement", [None, "old"])
async def test_no_replacement_raises_the_original_error(replacement):
    seen: list = []
    transport = by_token({"old": 403})
    client = ZammadClient("https://z.test/api/v1", transport=transport, on_rejected=hook(replacement, seen))

    with pytest.raises(PermissionDenied):
        await client.get("/users/search", token="old")

    assert seen == [(PermissionDenied, "old")]
    assert len(transport.requests) == 1


async def test_other_errors_never_reach_the_hook():
    seen: list = []
    transport = by_token({"old": 404})
    client = ZammadClient("https://z.test/api/v1", transport=transport, on_rejected=hook("new", seen))

    with pytest.raises(NotFoundError):
        await client.get("/tickets/1", token="old")

    assert seen == []


async def test_without_a_hook_a_401_is_raised_as_before():
    client = ZammadClient("https://z.test/api/v1", transport=by_token({"old": 401}))

    with pytest.raises(AuthError):
        await client.get("/users/me", token="old")
