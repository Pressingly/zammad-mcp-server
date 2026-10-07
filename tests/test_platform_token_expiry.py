"""Upstream ``expires_in`` correction — the guard against hourly MCP re-auth.

``mpass-auth-proxy`` reports the browser session's cookie lifetime (days) as
``expires_in`` while the Cognito token underneath still expires in an hour.
FastMCP gates transparent refresh on that value, so left uncorrected the refresh
never fires and the client is bounced through a full re-authorization every hour.
"""

from __future__ import annotations

import asyncio
import time

import httpx
import jwt
import pytest
from authlib.integrations.httpx_client import AsyncOAuth2Client
from fastmcp.server.auth.providers.aws import AWSCognitoProvider

from zammad_mcp.platform.cognito import (
    MIN_EXPIRES_IN_SECONDS,
    ZammadCognitoProvider,
    _already_truthful,
    _correct_expires_in,
    _true_expires_in,
)

_SIGNING_KEY = "k" * 32
_INFLATED_EXPIRES_IN = 604800  # what mpass-auth-proxy reports (7 days)


def _access_token(expires_in: int) -> str:
    return jwt.encode({"exp": int(time.time()) + expires_in}, _SIGNING_KEY)


def _token_response(body: dict, status_code: int = 200) -> httpx.Response:
    return httpx.Response(
        status_code=status_code,
        json=body,
        request=httpx.Request("POST", "https://auth.example.test/token"),
    )


def test_true_expires_in_reads_the_access_token_exp():
    assert _true_expires_in({"access_token": _access_token(3600)}) == pytest.approx(3600, abs=2)


def test_true_expires_in_floors_an_already_expired_token():
    """A negative lifetime would make FastMCP store an expiry in the past."""
    assert _true_expires_in({"access_token": _access_token(-500)}) == MIN_EXPIRES_IN_SECONDS


@pytest.mark.parametrize(
    "body",
    [{}, {"access_token": ""}, {"access_token": "not-a-jwt"}, {"access_token": jwt.encode({}, _SIGNING_KEY)}],
)
def test_true_expires_in_gives_up_on_unreadable_tokens(body):
    assert _true_expires_in(body) is None


def test_correct_expires_in_replaces_the_inflated_lifetime():
    corrected = _correct_expires_in(
        _token_response({"access_token": _access_token(3600), "expires_in": _INFLATED_EXPIRES_IN})
    ).json()
    assert corrected["expires_in"] == pytest.approx(3600, abs=2)


def test_correct_expires_in_preserves_the_rest_of_the_response():
    body = {
        "access_token": _access_token(3600),
        "id_token": "id",
        "refresh_token": "r",
        "expires_in": _INFLATED_EXPIRES_IN,
    }
    corrected = _correct_expires_in(_token_response(body)).json()
    assert corrected["id_token"] == "id"
    assert corrected["refresh_token"] == "r"


def test_correct_expires_in_hands_back_an_already_truthful_response_untouched():
    """Talking straight to Cognito already yields a real lifetime — leave it be.

    ``_true_expires_in`` truncates fractional seconds, so the honest value comes
    back a second short; that drift must not count as a mismatch or every
    direct-to-Cognito exchange would be needlessly rebuilt.
    """
    response = _token_response({"access_token": _access_token(3600), "expires_in": 3600})

    assert _correct_expires_in(response) is response


@pytest.mark.parametrize("reported", ["3600", None, True, {"n": 3600}])
def test_already_truthful_rejects_non_numeric_values(reported):
    """A non-numeric `expires_in` is not a match — the response must be corrected."""
    assert _already_truthful(reported, 3600) is False


@pytest.mark.parametrize(
    "response",
    [
        _token_response({"error": "invalid_grant"}, status_code=400),
        httpx.Response(200, content=b"not json", request=httpx.Request("POST", "https://x.test/token")),
        httpx.Response(200, json=["unexpected"], request=httpx.Request("POST", "https://x.test/token")),
    ],
)
def test_correct_expires_in_passes_through_responses_it_cannot_parse(response):
    assert _correct_expires_in(response) is response


def test_provider_registers_the_hook_on_both_grants(monkeypatch):
    """Correcting only the exchange would re-inflate the lifetime on first refresh."""
    monkeypatch.setattr(
        AWSCognitoProvider,
        "_create_upstream_oauth_client",
        lambda self: AsyncOAuth2Client(client_id="stub"),
    )
    # __init__ performs live OIDC discovery against Cognito; only the override matters here.
    provider = object.__new__(ZammadCognitoProvider)

    hooks = provider._create_upstream_oauth_client().compliance_hook

    assert _correct_expires_in in hooks["access_token_response"]
    assert _correct_expires_in in hooks["refresh_token_response"]


def test_authlib_invokes_the_hook_on_both_grants():
    """Guards the hook contract: authlib calls compliance hooks as ``resp -> resp``.

    Membership alone can't catch a signature change upstream (authlib's
    ``refresh_token_request`` hook, for instance, takes ``(url, headers, body)``),
    so drive a real client through both grants.
    """
    token_url = "https://auth.example.test/token"
    body = {
        "access_token": _access_token(3600),
        "id_token": "id",
        "refresh_token": "r",
        "token_type": "Bearer",
        "expires_in": _INFLATED_EXPIRES_IN,
    }

    async def _run():
        client = AsyncOAuth2Client(
            client_id="stub",
            transport=httpx.MockTransport(lambda request: httpx.Response(200, json=body)),
        )
        client.register_compliance_hook("access_token_response", _correct_expires_in)
        client.register_compliance_hook("refresh_token_response", _correct_expires_in)
        async with client:
            exchanged = await client.fetch_token(url=token_url, grant_type="authorization_code", code="c")
            refreshed = await client.refresh_token(url=token_url, refresh_token="r")
        return exchanged, refreshed

    exchanged, refreshed = asyncio.run(_run())

    assert exchanged["expires_in"] == pytest.approx(3600, abs=2)
    assert refreshed["expires_in"] == pytest.approx(3600, abs=2)
    assert exchanged["id_token"] == "id"
