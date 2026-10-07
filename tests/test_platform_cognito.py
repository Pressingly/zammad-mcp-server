from __future__ import annotations

from types import SimpleNamespace

import jwt
import pytest
from fastmcp.server.auth import AccessToken
from fastmcp.server.auth.providers.aws import AWSCognitoProvider

from zammad_mcp.platform.cognito import ACCESS_TOKEN_CLAIM, UPSTREAM_CLAIMS_KEY, ZammadCognitoProvider

ID_TOKEN = jwt.encode(
    {"email": "ada@example.com", "cognito:username": "sid-1", "preferred_username": "sid-1"}, "k" * 32
)


@pytest.fixture
def provider() -> ZammadCognitoProvider:
    return object.__new__(ZammadCognitoProvider)


class Store:
    def __init__(self, values: dict) -> None:
        self.values = values

    async def get(self, key):
        return self.values.get(key)


def wire(provider, *, jti="jti-1", mapping=None, token_set=None):
    provider._jwt_issuer = SimpleNamespace(verify_token=lambda _token: {"jti": jti})
    provider._jti_mapping_store = Store({"jti-1": mapping} if mapping is not None else {})
    provider._upstream_token_store = Store({"up-1": token_set} if token_set is not None else {})


async def test_extract_keeps_identity_and_gates_the_access_token(provider):
    tokens = {"id_token": ID_TOKEN, "access_token": "upstream"}

    sealed = await provider._extract_upstream_claims(tokens)
    resolved = await provider._extract_upstream_claims(tokens, include_access_token=True)

    assert sealed == {
        "id_token": ID_TOKEN,
        "email": "ada@example.com",
        "cognito:username": "sid-1",
        "preferred_username": "sid-1",
    }
    assert ACCESS_TOKEN_CLAIM not in sealed
    assert resolved[ACCESS_TOKEN_CLAIM] == "upstream"


@pytest.mark.parametrize("tokens", [{}, {"id_token": ""}, {"id_token": "not-a-jwt"}])
async def test_extract_gives_up_without_a_readable_id_token(provider, tokens):
    assert await provider._extract_upstream_claims(tokens) is None


async def test_extract_drops_empty_claims(provider):
    bare = jwt.encode({"email": ""}, "k" * 32)

    assert await provider._extract_upstream_claims({"id_token": bare}, include_access_token=True) == {"id_token": bare}


async def test_resolve_follows_the_jti_to_the_stored_token_set(provider):
    wire(
        provider,
        mapping=SimpleNamespace(upstream_token_id="up-1"),
        token_set=SimpleNamespace(raw_token_data={"id_token": ID_TOKEN, "access_token": "fresh"}),
    )

    resolved = await provider._resolve_upstream_claims("issued")

    assert resolved["email"] == "ada@example.com"
    assert resolved[ACCESS_TOKEN_CLAIM] == "fresh"


async def test_resolve_accepts_dict_shaped_records(provider):
    wire(provider, mapping={"upstream_token_id": "up-1"}, token_set={"raw_token_data": {"id_token": ID_TOKEN}})

    assert (await provider._resolve_upstream_claims("issued"))["cognito:username"] == "sid-1"


@pytest.mark.parametrize(
    "setup",
    [
        {"jti": None},
        {},
        {"mapping": SimpleNamespace(upstream_token_id=None)},
        {"mapping": SimpleNamespace(upstream_token_id="up-1")},
        {"mapping": SimpleNamespace(upstream_token_id="up-1"), "token_set": SimpleNamespace(raw_token_data=None)},
    ],
    ids=["no-jti", "no-mapping", "no-upstream-id", "no-token-set", "no-raw-data"],
)
async def test_resolve_returns_none_on_any_miss(provider, setup):
    wire(provider, **setup)

    assert await provider._resolve_upstream_claims("issued") is None


async def test_resolve_swallows_verification_errors(provider):
    def reject(_token):
        raise ValueError("bad signature")

    provider._jwt_issuer = SimpleNamespace(verify_token=reject)

    assert await provider._resolve_upstream_claims("issued") is None


@pytest.fixture
def validated(monkeypatch):
    token = AccessToken(token="t", client_id="c", scopes=["openid"], claims={"sub": "uuid-1"})

    async def upstream_load(self, _token):
        return self.next_validated

    monkeypatch.setattr(AWSCognitoProvider, "load_access_token", upstream_load)
    return token


async def test_load_access_token_attaches_the_current_identity(provider, validated):
    provider.next_validated = validated
    wire(
        provider,
        mapping=SimpleNamespace(upstream_token_id="up-1"),
        token_set=SimpleNamespace(raw_token_data={"id_token": ID_TOKEN, "access_token": "fresh"}),
    )

    loaded = await provider.load_access_token("issued")

    assert loaded.claims["sub"] == "uuid-1"
    assert loaded.claims[UPSTREAM_CLAIMS_KEY]["email"] == "ada@example.com"
    assert loaded.claims[UPSTREAM_CLAIMS_KEY][ACCESS_TOKEN_CLAIM] == "fresh"


async def test_load_access_token_fails_closed_without_an_id_token(provider, validated):
    provider.next_validated = validated
    wire(
        provider,
        mapping=SimpleNamespace(upstream_token_id="up-1"),
        token_set=SimpleNamespace(raw_token_data={"access_token": "only"}),
    )

    assert await provider.load_access_token("issued") is None


async def test_load_access_token_passes_an_upstream_rejection_through(provider, validated):
    provider.next_validated = None

    assert await provider.load_access_token("issued") is None
