from __future__ import annotations

import hashlib

import pytest
from fastmcp.server.auth import AccessToken

from zammad_mcp.platform.cognito import UPSTREAM_CLAIMS_KEY
from zammad_mcp.platform.identity import (
    Identity,
    current_identity,
    identity_email_for,
    identity_for,
    stored_access_token,
    upstream_access_token_for,
)


def claims(**upstream) -> dict:
    return {UPSTREAM_CLAIMS_KEY: {"id_token": "id", **upstream}}


def test_identity_prefers_an_email_shaped_email():
    assert identity_email_for(claims(email="ada@example.com", **{"cognito:username": "sid"})) == "ada@example.com"


def test_identity_falls_back_to_the_cognito_username():
    assert identity_email_for(claims(email="not-an-email", **{"cognito:username": "sid-1"})) == "sid-1"
    assert identity_email_for(claims(**{"cognito:username": " sid-2 "})) == "sid-2"


@pytest.mark.parametrize(
    "value",
    [None, {}, {UPSTREAM_CLAIMS_KEY: "x"}, {UPSTREAM_CLAIMS_KEY: {"email": "a@b.c"}}, claims(), claims(email=" ")],
)
def test_no_identity_off_the_cognito_path(value):
    assert identity_email_for(value) is None


def test_upstream_access_token_is_read_from_the_claims():
    assert upstream_access_token_for(claims(access_token="tok")) == "tok"
    assert upstream_access_token_for(claims()) is None
    assert upstream_access_token_for(None) is None


def test_identity_for_lowercases_like_the_middleware():
    token = AccessToken(
        token="t", client_id="c", scopes=[], claims=claims(email="Ada@Example.COM", access_token="secret-upstream")
    )

    found = identity_for(token)

    assert found.email == "ada@example.com"
    assert found.access_token == "secret-upstream"
    assert found.key == hashlib.sha256(b"ada@example.com").hexdigest()
    assert found.short_key == found.key[:16]
    assert "secret-upstream" not in repr(found)


def test_identity_for_needs_a_token():
    assert identity_for(None) is None
    assert identity_for(AccessToken(token="t", client_id="c", scopes=[], claims={})) is None


@pytest.mark.parametrize(
    ("email", "default_domain", "synthetic"),
    [
        ("ada@example.com", "askii.ai", False),
        ("9990000000000001@askii.ai", "askii.ai", True),
        ("x@askii.ai", " ASKII.AI ", True),
        ("x@sub.askii.ai", "askii.ai", False),
        ("9990000000000001", "askii.ai", True),
        ("9990000000000001", None, True),
        ("@askii.ai", "askii.ai", True),
        ("x@", "askii.ai", True),
        ("x@askii.ai", "", False),
    ],
)
def test_synthetic_identities(email, default_domain, synthetic):
    assert Identity(email=email).is_synthetic(default_domain) is synthetic


def test_no_request_means_no_identity():
    assert stored_access_token() is None
    assert current_identity() is None


def test_stored_access_token_treats_a_missing_request_scope_as_none(monkeypatch):
    def outside_request():
        raise RuntimeError("no active request")

    monkeypatch.setattr("zammad_mcp.platform.identity.get_access_token", outside_request)

    assert stored_access_token() is None
