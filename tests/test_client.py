from __future__ import annotations

import random

import httpx
import pytest

from zammad_mcp.client.errors import (
    AccountInactiveError,
    AuthError,
    ContentTooLargeError,
    GatewayDeniedError,
    IdentityHeaderError,
    MaintenanceModeError,
    MissingTokenError,
    NotFoundError,
    PermissionDenied,
    RateLimitedError,
    ServerError,
    UnprocessableError,
    ZammadAPIError,
    ZammadError,
    ZammadTransportError,
    error_for_status,
    to_tool_error,
)
from zammad_mcp.client.http import RetryPolicy, ZammadClient


def ok(_request: httpx.Request) -> httpx.Response:
    return httpx.Response(200, json={"ok": True})


class Sleeps:
    def __init__(self) -> None:
        self.delays: list[float] = []

    async def __call__(self, seconds: float) -> None:
        self.delays.append(seconds)


def make_client(transport: httpx.AsyncBaseTransport, sleeps: Sleeps | None = None) -> ZammadClient:
    return ZammadClient(
        "https://zammad.test/api/v1", transport=transport, sleep=sleeps or Sleeps(), rng=random.Random(1)
    )


def failing_then_ok(failure, times: int):
    state = {"calls": 0}

    def handler(request: httpx.Request) -> httpx.Response:
        state["calls"] += 1
        if state["calls"] <= times:
            if isinstance(failure, int):
                return httpx.Response(failure, json={"error": "upstream"})
            raise failure("boom", request=request)
        return httpx.Response(200, json={"ok": True})

    return handler


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


async def test_aclose_allows_reuse(recording_transport):
    transport = recording_transport(ok)
    client = make_client(transport)
    await client.get("/users/me", token="t")
    await client.aclose()
    await client.aclose()
    assert await client.get("/users/me", token="t") == {"ok": True}


RETRY_SAFE = ["GET", "PUT", "DELETE"]


@pytest.mark.parametrize("method", RETRY_SAFE)
@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ReadTimeout, 502, 503, 504])
async def test_retry_safe_methods_retry_transient_failures(recording_transport, method, failure):
    transport = recording_transport(failing_then_ok(failure, times=2))
    sleeps = Sleeps()
    assert await make_client(transport, sleeps).request(method, "/tickets/1", token="t") == {"ok": True}
    assert len(transport.requests) == 3
    assert len(sleeps.delays) == 2
    assert 0.25 <= sleeps.delays[0] <= 0.5
    assert 0.5 <= sleeps.delays[1] <= 1.0


@pytest.mark.parametrize("method", RETRY_SAFE)
async def test_retries_stop_after_two(recording_transport, method):
    transport = recording_transport(failing_then_ok(503, times=5))
    with pytest.raises(ServerError):
        await make_client(transport).request(method, "/tickets/1", token="t")
    assert len(transport.requests) == 3


async def test_transport_error_after_retries_maps_to_transport_error(recording_transport):
    transport = recording_transport(failing_then_ok(httpx.ConnectError, times=5))
    with pytest.raises(ZammadTransportError, match="ConnectError"):
        await make_client(transport).get("/users/me", token="t")
    assert len(transport.requests) == 3


async def test_500_is_not_retried(recording_transport):
    transport = recording_transport(failing_then_ok(500, times=1))
    with pytest.raises(ServerError):
        await make_client(transport).get("/tickets/1", token="t")
    assert len(transport.requests) == 1


@pytest.mark.parametrize("failure", [httpx.ConnectError, httpx.ConnectTimeout])
async def test_post_retries_when_the_request_never_left(recording_transport, failure):
    transport = recording_transport(failing_then_ok(failure, times=1))
    assert await make_client(transport).request("POST", "/ticket_articles", token="t", json={}) == {"ok": True}
    assert len(transport.requests) == 2


@pytest.mark.parametrize("failure", [httpx.ReadTimeout, 502, 503, 504])
async def test_post_is_never_retried_once_sent(recording_transport, failure):
    transport = recording_transport(failing_then_ok(failure, times=1))
    with pytest.raises(ZammadError):
        await make_client(transport).request("POST", "/ticket_articles", token="t", json={})
    assert len(transport.requests) == 1


async def test_read_only_post_can_opt_into_retries(recording_transport):
    transport = recording_transport(failing_then_ok(httpx.ReadTimeout, times=1))
    client = make_client(transport)
    assert await client.request("POST", "/tickets/search", token="t", json={}, retry_safe=True) == {"ok": True}
    assert len(transport.requests) == 2


@pytest.mark.parametrize(("retry_after", "expected"), [("3", 3.0), ("120", 10.0), ("", 1.0), ("Wed, 21 Oct 2026", 1.0)])
@pytest.mark.parametrize("method", ["GET", "POST"])
async def test_429_honours_retry_after_once(recording_transport, method, retry_after, expected):
    calls = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(request)
        if len(calls) == 1:
            return httpx.Response(429, headers={"Retry-After": retry_after})
        return httpx.Response(200, json={"ok": True})

    sleeps = Sleeps()
    assert await make_client(recording_transport(handler), sleeps).request(method, "/x", token="t") == {"ok": True}
    assert sleeps.delays == [expected]


async def test_second_429_is_an_error(recording_transport):
    transport = recording_transport(lambda _r: httpx.Response(429, headers={"Retry-After": "1"}))
    with pytest.raises(RateLimitedError):
        await make_client(transport).get("/x", token="t")
    assert len(transport.requests) == 2


@pytest.mark.parametrize(
    ("status", "error_type"),
    [
        (401, AuthError),
        (403, PermissionDenied),
        (404, NotFoundError),
        (422, UnprocessableError),
        (429, RateLimitedError),
        (500, ServerError),
        (503, ServerError),
        (409, ZammadAPIError),
    ],
)
def test_error_for_status(status, error_type):
    assert type(error_for_status(status, "x")) is error_type


@pytest.mark.parametrize(
    ("error", "expected"),
    [
        (AuthError(401, "x"), "Error: Zammad rejected the token (HTTP 401); it is invalid, expired or revoked"),
        (
            PermissionDenied(403, "Not authorized"),
            "Error: permission denied (HTTP 403: Not authorized); "
            "your Zammad role or token permissions do not allow this",
        ),
        (NotFoundError(404, "x"), "Error: not found (HTTP 404), or not visible to this user"),
        (
            UnprocessableError(422, "Title is missing"),
            "Error: Zammad rejected the request (HTTP 422): Title is missing",
        ),
        (RateLimitedError(429, "x"), "Error: Zammad is rate limiting requests (HTTP 429); try again shortly"),
        (ServerError(502, "x"), "Error: Zammad had a server error (HTTP 502); try again later"),
        (ZammadAPIError(409, "Conflict"), "Error: Zammad returned HTTP 409 (Conflict)"),
        (MissingTokenError("no token"), "Error: no token"),
    ],
)
def test_to_tool_error(error, expected):
    assert to_tool_error(error) == expected


@pytest.mark.parametrize(
    ("body", "expected"),
    [
        ({"error": "raw", "error_human": "Human readable"}, "Human readable"),
        ({"error": "raw only"}, "raw only"),
        (["not", "a", "dict"], "Unprocessable Entity"),
    ],
)
async def test_422_detail_prefers_error_human(recording_transport, body, expected):
    transport = recording_transport(lambda _r: httpx.Response(422, json=body))
    with pytest.raises(UnprocessableError) as caught:
        await make_client(transport).request("POST", "/tickets", token="t", json={})
    assert caught.value.detail == expected


async def test_html_error_page_falls_back_to_reason(recording_transport):
    transport = recording_transport(lambda _r: httpx.Response(404, text="<html>nginx</html>"))
    with pytest.raises(NotFoundError) as caught:
        await make_client(transport).get("/x", token="t")
    assert caught.value.detail == "Not Found"


async def test_download_returns_bytes_and_type(recording_transport):
    transport = recording_transport(
        lambda _r: httpx.Response(200, content=b"hello", headers={"Content-Type": "text/plain"})
    )
    download = await make_client(transport).download("/ticket_attachment/1/2/3", token="t", max_bytes=10)
    assert download.content == b"hello"
    assert download.content_type == "text/plain"


async def test_download_refuses_declared_oversize(recording_transport):
    transport = recording_transport(lambda _r: httpx.Response(200, content=b"x" * 20))
    with pytest.raises(ContentTooLargeError, match="20 bytes"):
        await make_client(transport).download("/a", token="t", max_bytes=10)


async def test_download_aborts_undeclared_oversize(recording_transport):
    def chunked(_request: httpx.Request) -> httpx.Response:
        async def body():
            for _ in range(4):
                yield b"x" * 8

        return httpx.Response(200, content=body())

    with pytest.raises(ContentTooLargeError, match="over the 10-byte limit"):
        await make_client(recording_transport(chunked)).download("/a", token="t", max_bytes=10)


async def test_download_maps_errors(recording_transport):
    transport = recording_transport(lambda _r: httpx.Response(403, json={"error": "Not authorized"}))
    with pytest.raises(PermissionDenied):
        await make_client(transport).download("/a", token="t", max_bytes=10)


def test_backoff_is_jittered_and_grows():
    policy = RetryPolicy()
    rng = random.Random(7)
    delays = [policy.backoff(attempt, rng) for attempt in range(3)]
    assert 0.25 <= delays[0] <= 0.5
    assert 0.5 <= delays[1] <= 1.0
    assert 1.0 <= delays[2] <= 2.0


async def test_identity_header_on_shared_client_defaults_is_still_refused(recording_transport):
    """Hard invariant: an X-Auth-Request-Email header overrides token auth in Zammad, so none may ever leave."""
    transport = recording_transport(ok)
    client = make_client(transport)
    client._client().headers["X-Auth-Request-Email"] = "agent@example.com"
    for method in ("GET", "POST", "PUT", "DELETE"):
        with pytest.raises(IdentityHeaderError):
            await client.request(method, "/users/me", token="customer-token")
    with pytest.raises(IdentityHeaderError):
        await client.download("/ticket_attachment/1/2/3", token="customer-token", max_bytes=10)
    assert transport.requests == []


@pytest.mark.parametrize(
    ("body", "error_type", "message"),
    [
        ({"error": "access_denied"}, GatewayDeniedError, "sign-in gateway"),
        ({"error": "User account is not active"}, AccountInactiveError, "not active"),
        ({"error": "Maintenance mode enabled!"}, MaintenanceModeError, "maintenance mode"),
        ({"error": "User authorization failed."}, PermissionDenied, "User authorization failed."),
        ({"error": "Token authorization failed."}, PermissionDenied, "Token authorization failed."),
    ],
)
async def test_403_bodies_are_told_apart(recording_transport, body, error_type, message):
    transport = recording_transport(lambda _r: httpx.Response(403, json=body))
    with pytest.raises(PermissionDenied) as caught:
        await make_client(transport).get("/tickets/1", token="t")
    assert type(caught.value) is error_type
    assert message in to_tool_error(caught.value)


async def test_empty_list_is_data_not_an_error(recording_transport):
    """A token with a blank permission list gets 200 [] from list endpoints, not 403."""
    transport = recording_transport(lambda _r: httpx.Response(200, json=[]))
    assert await make_client(transport).get("/tickets", token="t") == []
